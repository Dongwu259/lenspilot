"""
PhotoS - Select (Keeper Workflow)

Sort rated photos after a review pass: keepers (rating >= keep_min) move to a
selects folder, rejects (rating <= reject_max) move to a rejects folder, and
everything in between stays in place. Ratings are read from EXIF (the PhotoS:
UserComment payload) — the same ratings the review lightbox writes.

Shared by the CLI `select` command and the MCP server.
"""

import os
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from .engine import read_exif_metadata


def _rated_paths(files: List[str], keep_min: int, reject_max: int,
                 selects_dir: Optional[str], rejects_dir: Optional[str],
                 mode: str, dry_run: bool) -> List[dict]:
    """Classify files into keep/reject/skip rows (no filesystem writes)."""
    rows = []
    for idx, path in enumerate(files):
        try:
            rating = read_exif_metadata(path).get("rating")
        except Exception:
            rating = None
        if rating is not None and rating >= keep_min:
            status, action = "keep", "none"
            if selects_dir:
                action = "copy" if mode == "copy" else "move"
            row = {"path": path, "rating": rating, "status": status,
                   "action": action, "dest": selects_dir, "ok": True,
                   "error": ""}
        elif rating is not None and rating <= reject_max:
            status, action = "reject", "none"
            if rejects_dir:
                action = "copy" if mode == "copy" else "move"
            row = {"path": path, "rating": rating, "status": status,
                   "action": action, "dest": rejects_dir, "ok": True,
                   "error": ""}
        else:
            # 3-star / unrated / unreadable: leave in place
            row = {"path": path, "rating": rating, "status": "skip",
                   "action": "none", "dest": None, "ok": True, "error": ""}
        rows.append(row)
    return rows


def _resolve_dest(path: str, dest_dir: str) -> str:
    """Absolute target path for a file, flattened to a basename.

    The destination directory is explicit (never derived from the source) and
    the filename is basename-only, so a hostile EXIF value or nested path can
    never escape the target directory.
    """
    dest = os.path.abspath(dest_dir)
    return os.path.join(dest, os.path.basename(path))


def _unique_target(target: str) -> str:
    """First free ``stem_N.ext`` variant — never silently overwrite.

    Two source dirs can each hold ``DSC_0001.jpg``; flattening to a basename
    collides, and the old code os.replace()'d the first file away and then
    removed its source — one photo gone for good.
    """
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(target)
    n = 1
    while True:
        candidate = f"{stem}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def select_files(
    files: List[str],
    keep_min: int = 4,
    reject_max: int = 2,
    selects_dir: Optional[str] = None,
    rejects_dir: Optional[str] = None,
    mode: str = "move",
    dry_run: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """Sort rated photos into selects/rejects folders (or leave in place).

    Args:
        files: Source image paths.
        keep_min: rating >= keep_min → keeper (moved to selects_dir).
        reject_max: rating <= reject_max → reject (moved to rejects_dir).
        3-star / unrated / unreadable files are left in place.
        selects_dir / rejects_dir: destination folders (each mandatory for
                    its bucket to move; either may be omitted to skip it).
        mode: "move" (default) or "copy" (originals stay in place).
        dry_run: classify and report only — zero filesystem writes.
        progress_callback: optional callback(current, total, path).

    Returns:
        List of dicts: {"path", "rating", "status" ("keep"|"reject"|"skip"),
                        "action" ("move"|"copy"|"none"/"would_*"), "dest",
                        "ok", "error"}.

    Move atomicity: copy to a temp name in the destination, os.replace
    (same-FS atomic), then remove the source only after a successful copy —
    a failure never leaves a half-moved file or deletes the original.
    """
    if keep_min <= reject_max:
        raise ValueError(f"keep_min ({keep_min}) must be > reject_max "
                         f"({reject_max})")
    if mode not in ("move", "copy"):
        raise ValueError(f"mode must be 'move' or 'copy', got {mode!r}")

    rows = _rated_paths(files, keep_min, reject_max, selects_dir,
                        rejects_dir, mode, dry_run)
    total = len(rows)

    for idx, row in enumerate(rows):
        if progress_callback:
            progress_callback(idx + 1, total, row["path"])
        if row["status"] == "skip" or row["action"] == "none" or not row["dest"]:
            continue
        src = row["path"]
        target = _resolve_dest(src, row["dest"])
        if os.path.abspath(src) == target:
            # dest == source dir: the replace-then-remove dance would delete
            # the photo outright. Nothing to sort — report and move on.
            row["ok"] = False
            row["error"] = "destination is the source path; skipped"
            continue
        if dry_run:
            row["action"] = "would_" + row["action"]
            row["dest"] = target
            continue
        os.makedirs(row["dest"], exist_ok=True)
        # Same-basename inputs from different folders must not clobber each
        # other — pick the first free _1/_2/... variant instead of replacing.
        target = _unique_target(target)
        tmp = target + ".photos-tmp"
        try:
            shutil.copy2(src, tmp)          # copy2 preserves mtime
            os.replace(tmp, target)         # atomic once complete
            if mode == "move":
                os.remove(src)              # only after the copy succeeded
            row["dest"] = target
            row["ok"] = True
        except Exception as e:
            row["ok"] = False
            row["error"] = str(e)
            try:  # never leave a half-written temp
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    return rows
