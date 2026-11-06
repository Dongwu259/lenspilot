"""
PhotoS - Batch Rename

Rename (in place) or copy-rename image files using smart rename templates
({date}, {camera}, {seq}, ...) without re-compressing the image.
"""

import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .engine import (_extract_exif_metadata, _has_path_traversal,
                     _render_rename_pattern, _sanitize_stem)


def _load_meta(path: str) -> dict:
    """Extract EXIF metadata for rename templates, tolerating any open failure."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return _extract_exif_metadata(img, path)
    except Exception:
        return {}


def _unique_target(target: str, overwrite: bool) -> str:
    """Return a target path that won't clobber an existing file.

    Collisions append a clean counter to the ORIGINAL stem
    (photo.jpg → photo_1.jpg → photo_2.jpg) — never re-suffixed onto an
    already-suffixed name (the old photo_1_2.jpg bug).
    """
    if overwrite:
        return target
    p = Path(target)
    base = p
    counter = 1
    while p.exists():
        p = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    return str(p)


def _claim(target: str) -> bool:
    """Atomically reserve ``target`` (multi-process safe).

    The exists()-loop in _unique_target races when two workers rename into
    the same directory: both see "free", both os.replace, one file silently
    disappears. An O_CREAT|O_EXCL placeholder reserves the name; the winner
    overwrites its own placeholder at the end. Returns False when the name
    is already taken.
    """
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        # e.g. permission issues — fall back to the non-claiming path
        return True


def rename_files(
    paths: List[str],
    pattern: str,
    output_dir: Optional[str] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict]:
    """Rename (in place) or copy-rename a list of image files.

    Args:
        paths: Source image paths.
        pattern: Rename template (see _render_rename_pattern placeholders).
        output_dir: If set, copy each file to this directory with the new name
                    instead of renaming in place (copy2 preserves mtime).
        overwrite: Allow clobbering an existing target (otherwise _N suffix).
        dry_run: Compute the mapping without touching any file.
        progress_callback: Optional callback(current, total, path).

    Returns:
        List of dicts: {"input", "output", "status": "ok"|"error", "error"}.
    """
    results: List[Dict] = []
    total = len(paths)
    seq = 1

    for idx, path in enumerate(paths):
        meta = _load_meta(path)
        new_stem = _render_rename_pattern(pattern, meta, seq)
        seq += 1

        # Windows reserved device names (CON/PRN/COM1...) and trailing
        # dots/spaces can't exist on NTFS — sanitize before use.
        new_stem = _sanitize_stem(new_stem)

        if _has_path_traversal(new_stem):
            # Defense-in-depth: EXIF-derived values are sanitized at the
            # source, but never let a rendered stem escape the target dir.
            results.append({"input": path, "output": "",
                            "status": "error",
                            "error": "pattern produced an unsafe filename"})
            continue

        src = Path(path)
        if not new_stem.strip():
            # A pure-EXIF template ({date}, {year}{month}{day}, ...) on a
            # file without EXIF renders an empty stem — that would produce
            # hidden ".png"/".png_1" files reported as ok. Fall back to the
            # original name (same fallback semantics as engine rename mode).
            new_stem = src.stem

        if output_dir:
            target = str(Path(output_dir) / f"{new_stem}{src.suffix}")
        else:
            target = str(src.with_name(f"{new_stem}{src.suffix}"))

        # Avoid clobbering (and never map a file onto itself via collision).
        # With an O_EXCL claim the unique-name pick is race-safe across
        # processes; on claim failure the next _N variant is tried.
        if overwrite or dry_run:
            target = _unique_target(target, overwrite)
        else:
            for _attempt in range(1000):
                target = _unique_target(target, overwrite=False)
                if _claim(target):
                    break
            else:  # pathological collision storm — give up loudly
                results.append({"input": path, "output": "",
                                "status": "error",
                                "error": "could not reserve a unique target"})
                continue

        if progress_callback:
            progress_callback(idx + 1, total, path)

        if dry_run:
            results.append({"input": path, "output": target,
                            "status": "ok", "error": ""})
            continue

        try:
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                shutil.copy2(path, target)  # copy2 preserves mtime
            else:
                # os.replace: same-FS atomic, and overwrites an existing
                # target on every platform (os.rename raises FileExistsError
                # on Windows, silently breaking --overwrite there).
                os.replace(path, target)
            results.append({"input": path, "output": target,
                            "status": "ok", "error": ""})
        except Exception as e:
            # release the placeholder we claimed so the name is free again
            if not overwrite:
                try:
                    if os.path.exists(target):
                        os.unlink(target)
                except OSError:
                    pass
            results.append({"input": path, "output": target,
                            "status": "error", "error": str(e)})

    return results
