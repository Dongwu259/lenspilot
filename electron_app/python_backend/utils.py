"""
utils.py — Shared utilities for file operations, hashing, and formatting
Extracted from photo_classification/main.py
"""

import os
import sys
import hashlib
import string
import ctypes

HASH_BUF = 64 * 1024


def get_user_data_dir():
    """Resolve the writable per-user data directory.

    Packaged apps expose APS_USER_DATA (Electron app.getPath('userData')).
    Falls back to a platform-default per-user dir. Used for app_config.json,
    scores.db, and the thumbnail cache so nothing is written into the
    read-only packaged resources directory.
    """
    env_dir = os.environ.get("APS_USER_DATA", "").strip()
    if env_dir:
        return env_dir
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LensPilot")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LensPilot")
    return os.path.expanduser("~/.lenspilot")

# Windows drive type constants
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

SKIP_DRIVES = frozenset({"C"})

# ── RAW formats (decoded via rawpy / LibRaw ≥ 0.22) ──
# Single source of truth: IMAGE_EXTS and the "raw" file filter are BOTH derived
# from this set (see below) so they can never drift apart. LibRaw 0.22 decodes
# all major current brands plus many legacy / medium-format bodies. Adding an
# extension here only ever helps: files route to rawpy, and anything LibRaw
# can't decode fails the same way it already did (fell through to PIL).
RAW_EXTS = {
    # 主流当代品牌
    ".arw", ".srf", ".sr2", ".arq",            # Sony（arq = 像素位移合成）
    ".cr2", ".cr3", ".crw",                    # Canon（crw = CR2 之前的 CIFF）
    ".nef", ".nrw",                            # Nikon
    ".raf",                                    # Fujifilm
    ".rw2",                                    # Panasonic
    ".orf", ".ori",                            # Olympus / OM System
    ".pef", ".ptx",                            # Pentax
    ".raw", ".rwl", ".rwz",                    # Leica / Panasonic
    ".dng",                                    # Adobe 通用（Leica / Ricoh / DJI 等）
    ".srw",                                    # Samsung
    ".x3f",                                    # Sigma Foveon
    ".gpr",                                    # GoPro
    # 中画幅
    ".iiq", ".cap", ".eip",                    # Phase One
    ".3fr", ".fff",                            # Hasselblad / Imacon
    ".mef",                                    # Mamiya
    ".mos",                                    # Leaf
    # 传统 / 其它
    ".mrw", ".mdc",                            # Minolta / Agfa
    ".erf",                                    # Epson
    ".dcr", ".kdc", ".k25", ".dcs", ".dc2",    # Kodak
    ".bay",                                    # Casio
    ".cs1", ".sti",                            # Sinar
}

# Non-RAW still-image formats Pillow (with pillow-heif) can open directly.
_NON_RAW_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".tiff", ".tif",
    ".webp", ".tga", ".gif",
    ".heic", ".heif",  # Apple HEIF formats (needs pillow-heif)
}

IMAGE_EXTS = _NON_RAW_IMAGE_EXTS | RAW_EXTS

FILE_TYPE_FILTERS = {
    "all":   {"label": "全部文件", "exts": None},
    "raw":   {"label": "仅 RAW",  "exts": set(RAW_EXTS)},
    "jpeg":  {"label": "仅 JPEG", "exts": {".jpg", ".jpeg", ".jpe", ".jfif"}},
    "video": {"label": "仅视频",  "exts": {".mp4", ".mov", ".mxf", ".avi", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv", ".mkv", ".webm", ".m4v", ".braw", ".r3d"}},
}


def get_drive_type(letter):
    """Get Windows drive type for a given drive letter.
    On non-Windows platforms, always returns DRIVE_UNKNOWN.
    """
    if sys.platform != 'win32':
        return DRIVE_UNKNOWN
    return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")


def get_volume_label(letter):
    """Get Windows volume label for a given drive letter.
    On non-Windows platforms, always returns an empty string.
    """
    if sys.platform != 'win32':
        return ""
    drive_path = f"{letter}:\\"
    label = ctypes.create_unicode_buffer(256)
    ctypes.windll.kernel32.GetVolumeInformationW(
        drive_path, label, 256, None, None, None, None, 256
    )
    return label.value or ""


def get_available_drives():
    """Get all available drives with their types.
    On non-Windows platforms, always returns an empty dict.
    """
    if sys.platform != 'win32':
        return {}
    drives = {}
    for letter in string.ascii_uppercase:
        if letter in SKIP_DRIVES:
            continue
        if os.path.exists(f"{letter}:\\"):
            drives[letter] = get_drive_type(letter)
    return drives


def scan_files(folder_path, exts=None):
    """Recursively scan a folder for files, optionally filtered by extension.
    Returns list of (full_path, relative_path, size) tuples.
    """
    files = []
    for root, _dirs, fnames in os.walk(folder_path):
        for fname in fnames:
            if exts and os.path.splitext(fname)[1].lower() not in exts:
                continue
            src = os.path.join(root, fname)
            try:
                size = os.path.getsize(src)
            except OSError:
                size = 0
            files.append((src, os.path.relpath(src, folder_path), size))
    return files


def file_md5(path):
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_BUF)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def format_size(bytes_val):
    """Format bytes to human-readable string."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"


def format_speed(bps):
    """Format bytes-per-second to human-readable string."""
    if bps < 1024:
        return f"{bps:.1f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.1f} MB/s"


def format_eta(seconds):
    """Format seconds to human-readable ETA string."""
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)} 秒"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m} 分 {s} 秒"
    else:
        h, rem = divmod(int(seconds), 3600)
        m, _ = divmod(rem, 60)
        return f"{h} 小时 {m} 分"


def atomic_write_json(path, data):
    """Atomically write JSON data to `path`.

    Writes to a .tmp file in the same directory, then uses os.replace()
    (atomic rename on POSIX and Windows) to swap it in.  This prevents
    partial/corrupt files when the process is killed mid-write.
    """
    import json as _json
    import logging as _logging
    _log = _logging.getLogger("atomic_write_json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        _log.warning("atomic write %s failed (%s), trying direct fallback", path, e)
        # Clean up stale .tmp file
        try:
            os.remove(tmp)
        except OSError:
            pass
        # Best-effort: if os.replace fails, try direct write as fallback
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            _log.warning("direct fallback write to %s also failed: %s", path, e2)
