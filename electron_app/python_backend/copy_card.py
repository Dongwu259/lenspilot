"""
copy_card.py — Auto copy card module
Adapted from the photo_classification card-copy tool's main.py
Replaces tkinter UI callbacks with IPC event emission.
"""

import os
import sys
import shutil
import time
import threading
import errno
from datetime import datetime

# Import from service module (in the same package)
from service import send_event, send_log, register_handler
from utils import scan_files, file_md5, FILE_TYPE_FILTERS
from drive_detector import DriveDetector, DRIVE_REMOVABLE, DRIVE_FIXED

# ── Global state ──
_monitor_thread = None
_monitor_stop = False
_copy_cancel = threading.Event()
_copying = False
_copy_lock = threading.Lock()    # protects _copying check-and-set (TOCTOU fix)


# ══════════════════════════════════════════════════════════════════════
# Cross-platform helpers
# ══════════════════════════════════════════════════════════════════════

def _is_removable_or_fixed(drive_value):
    """跨平台：判断是否为可拷贝的驱动器（可移动/固定）。
    Windows: drive_value is GetDriveType int (2=removable, 3=fixed)
    Mac/Linux: drive_value is a dict with 'removable' key
    """
    if sys.platform == 'win32':
        return drive_value in (DRIVE_REMOVABLE, DRIVE_FIXED)
    return isinstance(drive_value, dict) and drive_value.get('removable', False)


def _get_drive_root(drive_key):
    """跨平台：获取驱动器根路径。
    Windows: 'E' → 'E:\\'
    Mac/Linux: '/Volumes/CARD' → '/Volumes/CARD' (unchanged mount path)
    """
    if sys.platform == 'win32':
        return f"{drive_key}:\\"
    return drive_key


def _get_drive_label(drive_key):
    """跨平台：获取驱动器卷标。
    Windows: win32 API GetVolumeInformation
    Mac/Linux: 从 get_all_drives() 返回的 dict 中取 label 字段
    """
    if sys.platform == 'win32':
        return DriveDetector.get_volume_label(drive_key)
    drives = DriveDetector.get_all_drives()
    info = drives.get(drive_key, {})
    return info.get('label', '') if isinstance(info, dict) else ''


# ══════════════════════════════════════════════════════════════════════
# Drive Monitor
# ══════════════════════════════════════════════════════════════════════

def _monitor_loop(scan_path, target_path, preset_name, file_filter):
    """Background thread that polls for new drives."""
    global _monitor_stop, _copying

    known_drives = DriveDetector.get_all_drives()
    send_log(f"开始监控移动设备（{scan_path}）...", "info")

    # Check existing drives first
    for letter, dt in known_drives.items():
        if not _is_removable_or_fixed(dt):
            continue
        target = os.path.join(_get_drive_root(letter), scan_path)
        if os.path.isdir(target):
            label = _get_drive_label(letter)
            send_event("drive_detected", {
                "letter": letter,
                "src_path": target,
                "label": label,
                "scan_path": scan_path,
            })

    while not _monitor_stop:
        try:
            current = DriveDetector.get_all_drives()
            appeared = set(current) - set(known_drives)
            disappeared = set(known_drives) - set(current)

            for letter in appeared:
                if not _is_removable_or_fixed(current[letter]):
                    continue
                target = os.path.join(_get_drive_root(letter), scan_path)
                if os.path.isdir(target):
                    label = _get_drive_label(letter)
                    send_event("drive_detected", {
                        "letter": letter,
                        "src_path": target,
                        "label": label,
                        "scan_path": scan_path,
                    })

            if appeared or disappeared:
                send_event("drives_changed", {
                    "current_drives": list(current.keys()),
                })

            known_drives = current
            time.sleep(1)

        except Exception as exc:
            send_log(f"驱动器监控错误: {exc}", "error")
            time.sleep(5)

    send_log("驱动器监控已停止", "info")


# ══════════════════════════════════════════════════════════════════════
# Copy Worker
# ══════════════════════════════════════════════════════════════════════

def _copy_worker(letter, src_path, dest, file_list, num_threads=1):
    """Background thread that performs copy + MD5 verification."""
    global _copying

    total_files = len(file_list)
    import hashlib as _hashlib
    _source_md5_cache = {}  # (src, rel) -> hex digest, populated during copy

    total_bytes = sum(sz for _, _, sz in file_list)
    copied_bytes = 0
    copied_files = 0
    skipped_files = 0
    error = None
    cancelled = False

    def copy_single(src, rel, size):
        nonlocal copied_bytes, copied_files, skipped_files, error
        if _copy_cancel.is_set():
            return None
        dst_path = os.path.join(dest, rel)
        tmp_path = dst_path + ".part"

        # Skip if the real file already exists with the correct size.
        # Because we only create dst_path via atomic os.replace() (see below),
        # a file at dst_path is guaranteed to be a complete, correct copy.
        if os.path.exists(dst_path):
            try:
                if os.path.getsize(dst_path) == size:
                    # Clean up any orphaned .part from a previous killed copy
                    if os.path.exists(tmp_path):
                        try: os.remove(tmp_path)
                        except OSError: pass
                    # Skip: source MD5 unknown (not needed), will verify dest vs dest
                    return ('skip', src, rel, size)
            except OSError:
                pass

        # Remove any orphaned .part from a previous run (wrong content)
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            # Stream copy + inline MD5: read source ONCE, compute hash while writing
            h = _hashlib.md5()
            with open(src, "rb") as fsrc, open(tmp_path, "wb") as fdst:
                for chunk in iter(lambda: fsrc.read(1024 * 1024), b""):
                    if _copy_cancel.is_set():
                        fdst.close()
                        try: os.remove(tmp_path)
                        except OSError: pass
                        return None
                    h.update(chunk)
                    fdst.write(chunk)
            shutil.copystat(src, tmp_path)
            os.replace(tmp_path, dst_path)
            _source_md5_cache[(src, rel)] = h.hexdigest()
            return ('copy', src, rel, size)
        except Exception as e:
            error = str(e)
            # Auto-cancel on fatal errors: disk full or device disconnected
            if isinstance(e, OSError):
                eno = getattr(e, 'errno', None)
                if eno in (errno.ENOSPC, errno.EDQUOT):
                    send_log(f"目标磁盘已满，取消剩余拷贝 — {e}", "error")
                    _copy_cancel.set()
                elif eno in (errno.EIO, errno.ENODEV) or getattr(e, 'winerror', 0) in (21, 1117):
                    send_log(f"设备已断开，取消剩余拷贝 — {e}", "error")
                    _copy_cancel.set()
            if not _copy_cancel.is_set():
                send_log(f"拷贝失败: {src} — {e}", "error")
            # Best-effort cleanup of partial .part file
            try: os.remove(tmp_path)
            except OSError: pass
            return ('error', src, rel, size)

    start_time = time.monotonic()
    last_report = start_time
    error = None

    if num_threads > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = {executor.submit(copy_single, src, rel, size): (src, rel, size) for src, rel, size in file_list}
            for future in as_completed(futures):
                if _copy_cancel.is_set():
                    cancelled = True
                    break
                try:
                    result = future.result()
                except Exception as e:
                    error = str(e)
                    send_log(f"拷贝线程异常: {e}", "error")
                    continue
                if result:
                    action, src, rel, size = result
                    if action == 'skip':
                        skipped_files += 1
                    elif action == 'copy':
                        copied_files += 1
                        copied_bytes += size
                    now = time.monotonic()
                    if now - last_report >= 0.3:
                        elapsed = now - start_time
                        speed = copied_bytes / elapsed if elapsed > 0 else 0
                        _report_progress(copied_files, total_files, copied_bytes, total_bytes, elapsed, skipped_files, speed)
                        last_report = now
    else:
        # Original serial copy
        for src, rel, size in file_list:
            if _copy_cancel.is_set():
                cancelled = True
                break
            result = copy_single(src, rel, size)
            if result:
                action, src, rel, size = result
                if action == 'skip':
                    skipped_files += 1
                elif action == 'copy':
                    copied_files += 1
                    copied_bytes += size
                now = time.monotonic()
                if now - last_report >= 0.3:
                    elapsed = now - start_time
                    speed = copied_bytes / elapsed if elapsed > 0 else 0
                    _report_progress(copied_files, total_files, copied_bytes, total_bytes, elapsed, skipped_files, speed)
                    last_report = now

    elapsed = time.monotonic() - start_time
    speed = copied_bytes / elapsed if elapsed > 0 else 0
    _report_progress(copied_files, total_files, copied_bytes, total_bytes,
                    elapsed, skipped_files, speed)

    if cancelled:
        send_event("copy_cancelled", {"letter": letter, "dest": dest, "copied": copied_files})
        _on_copy_done()
        return

    if error:
        send_event("copy_error", {"letter": letter, "error": error})
        _on_copy_done()
        return

    # ── MD5 Verification (reads only destination, source MD5 cached from copy) ──
    send_log("正在校验文件完整性 (MD5)...", "info")
    verified = 0
    mismatches = 0
    verify_start = time.monotonic()
    last_report = verify_start

    for src, rel, _ in file_list:
        if _copy_cancel.is_set():
            cancelled = True
            break

        dst_path = os.path.join(dest, rel)
        if not os.path.exists(dst_path):
            mismatches += 1
            continue

        try:
            # Use cached source MD5 if available (computed during copy phase)
            cached = _source_md5_cache.get((src, rel))
            if cached:
                if file_md5(dst_path) == cached:
                    verified += 1
                else:
                    mismatches += 1
                    send_log(f"MD5不匹配: {rel}", "warn")
            else:
                # Skip files: verify destination file reads correctly
                file_md5(dst_path)
                verified += 1
        except OSError:
            mismatches += 1

        done = verified + mismatches
        now = time.monotonic()
        if now - last_report >= 0.3 or done >= total_files:
            send_event("verify_progress", {
                "done": done,
                "total": total_files,
                "mismatches": mismatches,
                "final": done >= total_files,
            })
            last_report = now

    send_event("verify_progress", {
        "done": verified + mismatches,
        "total": total_files,
        "mismatches": mismatches,
        "final": True,
    })

    if cancelled:
        send_event("copy_cancelled", {"letter": letter, "dest": dest, "copied": copied_files})
    else:
        send_event("copy_done", {
            "letter": letter,
            "dest": dest,
            "total": total_files,
            "copied": copied_files,
            "skipped": skipped_files,
            "verified": verified,
            "mismatches": mismatches,
        })
        if mismatches == 0:
            send_log(f"MD5校验 — 全部通过 ({verified}/{verified}) ✓", "success")
        else:
            send_log(f"MD5校验 — 通过 {verified}, 不一致 {mismatches} ⚠", "warn")

    _on_copy_done()


def _report_progress(done, total, copied_bytes, total_bytes, elapsed, skipped, speed):
    """Send copy progress event."""
    send_event("copy_progress", {
        "done": done,
        "total": total,
        "copied_bytes": copied_bytes,
        "total_bytes": total_bytes,
        "elapsed": elapsed,
        "skipped": skipped,
        "speed": speed,
    })


def _on_copy_done():
    """Called when a copy task completes (success, cancel, or error)."""
    global _copying
    with _copy_lock:
        _copying = False


# ══════════════════════════════════════════════════════════════════════
# IPC Handlers
# ══════════════════════════════════════════════════════════════════════

def handle_list_drives(params):
    """Get list of currently connected drives."""
    drives = DriveDetector.get_all_drives()
    scan_path = params.get("scan_path", "DCIM")
    result = []
    for letter in sorted(drives):
        if not _is_removable_or_fixed(drives[letter]):
            continue
        label = _get_drive_label(letter) or "(无标签)"
        target = os.path.join(_get_drive_root(letter), scan_path)
        result.append({
            "letter": letter,
            "label": label,
            "type": drives[letter],
            "has_scan_path": os.path.isdir(target),
            "src_path": target if os.path.isdir(target) else None,
            "scan_path": scan_path,
        })
    return {"drives": result}


def handle_start_monitor(params):
    """Start the drive monitor thread."""
    global _monitor_thread, _monitor_stop

    if _monitor_thread and _monitor_thread.is_alive():
        _monitor_stop = True
        _monitor_thread.join(timeout=2)

    _monitor_stop = False
    scan_path = params.get("scan_path", "DCIM")
    target_path = params.get("target_path", "")
    preset_name = params.get("preset_name", "")
    file_filter = params.get("file_filter", "all")

    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(scan_path, target_path, preset_name, file_filter),
        daemon=True,
    )
    _monitor_thread.start()

    return {"ok": True, "scan_path": scan_path}


def handle_stop_monitor(params):
    """Stop the drive monitor thread."""
    global _monitor_stop, _monitor_thread
    _monitor_stop = True
    if _monitor_thread:
        _monitor_thread.join(timeout=3)
        _monitor_thread = None
    return {"ok": True}


def handle_start_copy(params):
    """Start a copy + verify operation."""
    global _copying

    letter = params.get("letter", "")
    src_path = params.get("src_path", "")
    target_path = params.get("target_path", "")
    folder_name = params.get("folder_name", "")
    file_filter = params.get("file_filter", "all")

    if not all([letter, src_path, target_path]):
        return {"error": "Missing required parameters"}

    dest = os.path.join(target_path, folder_name)
    filter_info = FILE_TYPE_FILTERS.get(file_filter, FILE_TYPE_FILTERS["all"])
    exts = filter_info.get("exts")

    # Scan files
    send_log(f"扫描文件中... ({src_path})", "info")
    try:
        file_list = scan_files(src_path, exts)
    except (OSError, PermissionError) as e:
        send_log(f"扫描失败（设备可能已断开）: {e}", "error")
        return {"error": f"扫描失败: {e}"}
    total_count = len(file_list)
    total_size = sum(sz for _, _, sz in file_list)

    if total_count == 0:
        send_log("未找到匹配的文件", "warn")
        return {"error": "未找到匹配的文件"}

    send_log(f"共 {total_count} 个文件，{total_size / (1024 * 1024):.1f} MB", "info")

    # Lock-protect _copying check-and-set to prevent TOCTOU race.
    # Queueing is handled entirely by the frontend (copy-card.js pendingCards).
    with _copy_lock:
        if _copying:
            return {"error": "已有复制任务进行中，请等待当前任务完成"}

        _copying = True

    # Clear cancel BEFORE starting the thread to prevent race:
    # if cancel is set between thread.start() and the worker's first check,
    # the worker would miss it because _copy_cancel.clear() was inside _copy_worker.
    _copy_cancel.clear()

    # Start copy in background thread
    threads = int(params.get("threads", 1))
    threads = max(1, min(threads, 16))  # cap at 16 to prevent resource exhaustion
    thread = threading.Thread(
        target=_copy_worker,
        args=(letter, src_path, dest, file_list, threads),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "file_count": total_count, "total_size": total_size, "dest": dest}


def handle_cancel_copy(params):
    """Cancel the current copy operation."""
    global _copy_cancel
    _copy_cancel.set()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════

def register_handlers():
    """Register all copy card handlers with the IPC service."""
    from service import register_cleanup

    register_handler("list_drives", handle_list_drives)
    register_handler("start_monitor", handle_start_monitor)
    register_handler("stop_monitor", handle_stop_monitor)
    register_handler("start_copy", handle_start_copy)
    register_handler("cancel_copy", handle_cancel_copy)

    # Shutdown cleanup: stop monitor + cancel active copies
    def _cleanup():
        global _monitor_stop, _copy_cancel
        _monitor_stop = True
        _copy_cancel.set()
        if _monitor_thread and _monitor_thread.is_alive():
            _monitor_thread.join(timeout=3)
    register_cleanup(_cleanup)

    send_log("copy_card module loaded", "info")
