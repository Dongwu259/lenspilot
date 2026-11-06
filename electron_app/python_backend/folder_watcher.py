"""
folder_watcher.py — Watch-folder source for tethered shooting

联机拍摄软件会持续把新照片写进电脑上的某个文件夹。本模块轮询该文件夹，
判定文件写完（尺寸稳定）后，按用户选择的处理模式（realtime/batch/manual）发事件，
由前端触发 score_files 评分并入工作流队列。

镜像 copy_card._monitor_loop 的轮询线程 + service.send_event 模式。
去重用内存 _seen 集合；启动时默认把已有文件种入 _seen（只处理启动后的新文件）。
"""

import os
import time
import threading

from service import send_event, send_log, register_handler
from utils import IMAGE_EXTS, FILE_TYPE_FILTERS

# ── Global state ──
_watch_thread = None
_watch_stop = threading.Event()
_seen = set()          # 已处理文件绝对路径（内存去重）
_pending = {}          # path -> (size, first_seen_ts, stable_once)  尺寸稳定性跟踪
_batch_queue = []      # batch 模式累积的稳定文件 [{path,name,size}]
_last_new_time = 0.0   # batch 模式：最近一次发现新文件的时间
_config = {}           # 当前监听配置
_batch_counter = 0
_counter_lock = threading.Lock()
_MAX_SEEN = 100000     # cap memory usage for long-running watchers
_MAX_PENDING = 10000   # cap pending files to prevent memory leak
_PENDING_TIMEOUT = 300 # remove files stuck in pending > 5 minutes


def _new_batch_id():
    global _batch_counter
    with _counter_lock:
        _batch_counter += 1
        return f"watch_{int(time.time())}_{_batch_counter}"


def _resolve_exts(file_filter):
    """解析文件类型过滤。watch 默认只看图片（含 RAW），排除视频（视频无法评分）。"""
    fi = FILE_TYPE_FILTERS.get(file_filter, FILE_TYPE_FILTERS["all"])
    exts = fi.get("exts")
    if exts is None:
        exts = IMAGE_EXTS  # 'all' → 限定为图片
    return exts


def _under_dir(path, parent):
    """path 是否位于 parent 目录之下（用于跳过输出目录）。"""
    if not parent:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        return False


def _should_skip(path):
    """跳过自产目录（精选输出 / 水印 / 去重），防被重复检测。"""
    out = _config.get("output_folder", "")
    if _under_dir(path, out):
        return True
    parts = {p.lower() for p in path.replace("\\", "/").split("/")}
    if "_watermarked" in parts or "_duplicates" in parts:
        return True
    return False


def _scan_folder(folder, exts, recursive):
    """枚举监听文件夹中的匹配文件，返回 [(path, size), ...]。"""
    result = []
    if recursive:
        walker = os.walk(folder)
    else:
        try:
            names = os.listdir(folder)
        except OSError:
            return result
        walker = [(folder, [], names)]
    for root, _dirs, fnames in walker:
        for fname in fnames:
            if exts and os.path.splitext(fname)[1].lower() not in exts:
                continue
            src = os.path.join(root, fname)
            try:
                size = os.path.getsize(src)
            except OSError:
                continue
            result.append((src, size))
    return result


def _emit_stable(path, size):
    """文件判定稳定后，按模式发事件并加入 _seen。"""
    global _last_new_time
    mode = _config.get("mode", "realtime")
    name = os.path.basename(path)
    if len(_seen) < _MAX_SEEN:
        _seen.add(path)
    _last_new_time = time.time()

    if mode == "realtime" or mode == "manual":
        # realtime：前端收到后立即单独 score_files
        # manual：前端只入待筛选列表，用户点「开始筛选」才评分
        send_event("watch_file_received", {
            "path": path,
            "name": name,
            "size": size,
            "batch_id": _new_batch_id() if mode == "realtime" else "",
        })
    elif mode == "batch":
        _batch_queue.append({"path": path, "name": name, "size": size})


def _watch_loop():
    """后台轮询线程。"""
    folder = _config.get("folder", "")
    interval = max(0.5, float(_config.get("interval", 2.0)))
    stability_ms = float(_config.get("stability_ms", 1500)) / 1000.0
    quiet_ms = float(_config.get("quiet_ms", 5000)) / 1000.0
    mode = _config.get("mode", "realtime")
    exts = _resolve_exts(_config.get("filter", "all"))
    recursive = bool(_config.get("recursive", True))
    process_existing = bool(_config.get("process_existing", False))

    send_event("watch_started", {"folder": folder, "mode": mode})
    send_log(f"开始监听文件夹: {folder}（模式={mode}）", "info")

    # 启动时处理已有文件
    if not process_existing:
        # 默认：种入 _seen，只处理启动后的新文件
        for path, size in _scan_folder(folder, exts, recursive):
            if _should_skip(path):
                continue
            _seen.add(path)
        send_log(f"已跳过 {len(_seen)} 个已有文件（仅监听新文件）", "info")
    else:
        # 处理已有文件：按模式发事件
        existing = [(p, s) for p, s in _scan_folder(folder, exts, recursive) if not _should_skip(p)]
        for path, size in existing:
            _emit_stable(path, size)

    while not _watch_stop.is_set():
        try:
            now = time.time()
            current_files = _scan_folder(folder, exts, recursive)
            current_paths = set()

            for path, size in current_files:
                current_paths.add(path)
                if _should_skip(path):
                    continue
                if path in _seen:
                    continue

                # 尺寸稳定性跟踪
                prev = _pending.get(path)
                if prev is None:
                    if len(_pending) < _MAX_PENDING:
                        _pending[path] = (size, now, False)
                    continue
                prev_size, first_seen, stable_once = prev
                # Drop files stuck in pending too long (likely deleted or broken)
                if now - first_seen > _PENDING_TIMEOUT:
                    del _pending[path]
                    continue
                prev_size, first_seen, stable_once = prev
                if prev_size != size:
                    # 还在写，重置计时
                    _pending[path] = (size, now, False)
                    continue
                if not stable_once:
                    # 第一轮尺寸不变，标记稳定一次，下一轮再确认
                    _pending[path] = (size, first_seen, True)
                    continue
                # 已稳定一次 + 本轮仍不变 + 达到最小 age → 判定写完
                if now - first_seen >= stability_ms:
                    _emit_stable(path, size)
                    _pending.pop(path, None)

            # 清理已删除文件的 pending 记录
            for path in list(_pending.keys()):
                if path not in current_paths:
                    _pending.pop(path, None)

            # batch 模式：停顿 quiet_ms 无新文件 → 一次性发批次
            if mode == "batch" and _batch_queue and (now - _last_new_time) >= quiet_ms:
                batch = list(_batch_queue)
                _batch_queue.clear()
                send_event("watch_batch_ready", {
                    "files": batch,
                    "folder": folder,
                    "batch_id": _new_batch_id(),
                })

        except Exception as exc:
            send_log(f"文件夹监听错误: {exc}", "error")

        # 分段 sleep 以便及时响应停止
        _watch_stop.wait(interval)

    send_event("watch_stopped", {"folder": folder})
    send_log("文件夹监听已停止", "info")


# ══════════════════════════════════════════════════════════════════════
# IPC Handlers
# ══════════════════════════════════════════════════════════════════════

def handle_start_folder_watch(params):
    """启动文件夹监听。"""
    global _watch_thread, _config, _seen, _pending, _batch_queue, _last_new_time

    folder = params.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        raise ValueError(f"无效的监听文件夹: {folder}")

    # 已在监听则先停
    if _watch_thread and _watch_thread.is_alive():
        stop_folder_watch_internal()

    mode = params.get("mode", "realtime")
    if mode not in ("realtime", "batch", "manual"):
        mode = "realtime"

    _config = {
        "folder": folder,
        "mode": mode,
        "filter": params.get("filter", "all"),
        "interval": float(params.get("interval", 2.0)),
        "stability_ms": float(params.get("stability_ms", 1500)),
        "quiet_ms": float(params.get("quiet_ms", 5000)),
        "output_folder": params.get("output_folder", "").strip(),
        "threshold": float(params.get("threshold", 6.0)),
        "autoUpload": bool(params.get("autoUpload", False)),
        "process_existing": bool(params.get("process_existing", False)),
        "recursive": bool(params.get("recursive", True)),
    }

    _seen = set()
    _pending = {}
    _batch_queue = []
    _last_new_time = 0.0
    _watch_stop.clear()

    _watch_thread = threading.Thread(target=_watch_loop, daemon=True)
    _watch_thread.start()
    return {"ok": True, "folder": folder, "mode": mode}


def stop_folder_watch_internal():
    _watch_stop.set()
    if _watch_thread:
        _watch_thread.join(timeout=5)


def handle_stop_folder_watch(params):
    stop_folder_watch_internal()
    return {"ok": True}


def handle_folder_watch_status(params):
    running = _watch_thread is not None and _watch_thread.is_alive()
    return {
        "running": running,
        "folder": _config.get("folder", ""),
        "mode": _config.get("mode", ""),
        "seen_count": len(_seen),
    }


def register_handlers():
    from service import register_cleanup

    register_handler("start_folder_watch", handle_start_folder_watch)
    register_handler("stop_folder_watch", handle_stop_folder_watch)
    register_handler("folder_watch_status", handle_folder_watch_status)

    # Shutdown cleanup: stop folder watcher
    register_cleanup(lambda: stop_folder_watch_internal())
    send_log("folder_watcher module loaded", "info")
