"""Tk-free workflow seams (v2.0.0 — extracted verbatim from the former
photo_s/gui.py so logic is unit-testable without a display).

These are synchronous, headless functions: dialogs in app.py delegate to
them (app._cull_scan → workflows.cull_scan), background threads call them
and marshal UI updates back via the queue + after-drain convention.
"""

import os

def gallery_build(paths, out_dir, title="PhotoS Gallery",
                   thumb_size=360):
    """Sync: build the HTML gallery (thin wrapper so tests can call it
    without touching Tk)."""
    from ..gallery import build_gallery
    return build_gallery(list(paths), out_dir, title=title,
                         thumb_size=thumb_size)


def preview_render(path, options):
    """Sync: render one file through the real engine pipeline."""
    from ..engine import process_image
    return process_image(path, options)


def preview_options(opts, tempdir):
    """Sync: options for a preview render. NEVER deletes the source:
    remove_original is force-set to False, and naming/output are pinned
    so the result lands predictably inside the temp dir."""
    from dataclasses import replace
    return replace(opts, output_dir=tempdir, overwrite=True,
                   remove_original=False, suffix="", prefix="",
                   rename_pattern="", folder_pattern=None,
                   output_sizes=None)


def contact_sheet_build(files, output, cols=4,
                         thumb_size=(240, 240), captions=True,
                         bg=(0, 0, 0)):
    """Sync: build a contact sheet (thin wrapper so tests can call it
    without touching Tk)."""
    from ..contact import build_contact_sheet
    return build_contact_sheet(files, output, cols=cols,
                               thumb_size=thumb_size, captions=captions,
                               bg=bg)


def cull_scan(paths, thresholds, progress_cb=None):
    """Sync: classify files against exposure/sharpness thresholds."""
    from ..cull import cull_files
    return cull_files(list(paths), progress_callback=progress_cb,
                      **thresholds)


def hash_generate(paths, output, algorithm="sha256",
                   progress_cb=None):
    """Sync: hash files and write a manifest (returns the output path)."""
    from ..check import compute_checksums, write_manifest
    entries = compute_checksums(list(paths), algorithm=algorithm,
                                progress_callback=progress_cb)
    write_manifest(output, entries, algorithm=algorithm)
    return output


def hash_verify(path):
    """Sync: verify a manifest (returns the verify_manifest report)."""
    from ..check import verify_manifest
    return verify_manifest(path)


def hdr_merge(paths, output, align=False):
    """Sync: merge bracketed exposures into an HDR image.

    Thin wrapper so tests can call it without touching Tk. Returns the
    output path on success, raises (RuntimeError/ValueError) on failure.
    """
    from ..hdr import merge_hdr
    result = merge_hdr(list(paths), align=align)
    result.save(output, quality=95)
    return output


def dedup_scan(paths, threshold=5, progress_cb=None):
    """Sync: find duplicate groups + per-image blur scores.

    Returns (groups, scores): groups is a list of path-lists (each
    >= 2 members), scores maps path -> blur score (0.0 on error).
    Tk-free so tests can call it directly.
    """
    from ..dedup import find_duplicates
    from ..metrics import compute_blur_score

    dup_groups = find_duplicates(list(paths), threshold=threshold,
                                 progress_callback=progress_cb)
    groups = [list(g) for g in dup_groups.values() if len(g) >= 2]
    scores = {}
    for group in groups:
        for p in group:
            try:
                scores[p] = compute_blur_score(p)
            except Exception:
                scores[p] = 0.0
    return groups, scores


def dedup_trash_path(path, trash_dir):
    """Trash destination: trash_dir/basename with a numeric suffix if
    the name is taken (mirrors dedup.py move collision logic)."""
    dest = os.path.join(trash_dir, os.path.basename(path))
    stem, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = "{}_{}{}".format(stem, n, ext)
        n += 1
    return dest


def dedup_move_to_trash(paths, trash_dir, progress_cb=None):
    """Sync: move ``paths`` into ``trash_dir`` (created if needed).
    Returns (moved, failed, moved_map) where moved_map maps
    original -> trash destination (for undo). Tk-free so tests can
    call it directly."""
    moved, failed = 0, 0
    moved_map = {}
    try:
        os.makedirs(trash_dir, exist_ok=True)
    except OSError:
        return 0, len(paths), moved_map
    for i, p in enumerate(paths):
        try:
            dest = dedup_trash_path(p, trash_dir)
            os.rename(p, dest)
            moved_map[p] = dest
            moved += 1
        except OSError:
            failed += 1
        if progress_cb:
            progress_cb(i + 1, len(paths))
    return moved, failed, moved_map


def review_scan(paths, progress_cb=None):
    """Sync: read EXIF metadata for all paths. Returns {path: meta}.
    Tk-free so tests can call it directly."""
    from ..engine import read_exif_metadata
    meta = {}
    total = len(paths)
    for i, p in enumerate(paths):
        try:
            meta[p] = read_exif_metadata(p)
        except Exception:
            meta[p] = {"rating": None, "keywords": [], "title": "",
                       "caption": "", "date": "", "time": "",
                       "camera": "", "make": "", "iso": "",
                       "focal": "", "lens": "", "fnumber": "",
                       "shutter": ""}
        if progress_cb:
            progress_cb(i + 1, total)
    return meta


def select_move(paths, selects_dir, rejects_dir,
                 keep_min=4, reject_max=2, mode="move"):
    """Sync: sort rated files into selects/rejects folders.

    Tk-free seam (mirrors _cull_scan) so the review lightbox can call it
    directly; ratings are read from EXIF — the ones the review flow wrote.
    Returns (results, ok_count, error_count).
    """
    from ..select import select_files
    try:
        results = select_files(
            list(paths), keep_min=keep_min, reject_max=reject_max,
            selects_dir=selects_dir, rejects_dir=rejects_dir,
            mode=mode, dry_run=False,
        )
    except ValueError as e:
        return None, 0, 0, str(e)
    ok_count = sum(1 for r in results if r["ok"] and r["action"]
                   in ("move", "copy"))
    error_count = sum(1 for r in results if not r["ok"])
    return results, ok_count, error_count, ""
