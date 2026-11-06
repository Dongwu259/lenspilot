"""
PhotoS - Cull (exposure / sharpness filtering)

Shared by the CLI `cull` command, the REST API, and the GUI: given a list of
image paths and optional thresholds, classify each as kept or rejected.

v2.3 adds the photographer-facing ranking layer on top:
    * ``score_files``   — weighted quality score (0-100) per image
    * ``group_bursts``  — EXIF-time clustering of burst sequences
    (the classic threshold pass below stays unchanged — thresholds reject,
    scores rank, bursts pick one per sequence).
"""

from typing import Callable, Dict, List, Optional, Tuple


def cull_files(
    files: List[str],
    overexposed_max: Optional[float] = None,
    underexposed_max: Optional[float] = None,
    luminance_min: Optional[float] = None,
    luminance_max: Optional[float] = None,
    sharpness_min: Optional[float] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[dict]:
    """Classify each image against exposure/sharpness thresholds.

    Args:
        files: Image paths.
        overexposed_max: Reject if % pixels >= 250 exceeds this.
        underexposed_max: Reject if % pixels <= 5 exceeds this.
        luminance_min/max: Reject if mean luminance (0-1) outside this range.
        sharpness_min: Reject if blur score below this (higher = sharper).
        progress_callback: Optional (current, total) callback per file.

    Returns:
        List of dicts: {"path", "ok", "luminance", "overexposed_pct",
        "underexposed_pct", ["blur_score"], "kept"}. ``blur_score`` is only
        present when ``sharpness_min`` is set. A file passes iff it is
        readable (``ok``) AND every provided threshold is satisfied.
    """
    from .metrics import compute_exposure_stats, compute_blur_score

    results: List[dict] = []
    total = len(files)
    for i, f in enumerate(files):
        s = compute_exposure_stats(f)
        row = {"path": f, **s}
        if sharpness_min is not None:
            row["blur_score"] = round(compute_blur_score(f), 1)
        keep = (s["ok"]
                and (overexposed_max is None
                     or s["overexposed_pct"] <= overexposed_max)
                and (underexposed_max is None
                     or s["underexposed_pct"] <= underexposed_max)
                and (luminance_min is None or s["luminance"] >= luminance_min)
                and (luminance_max is None or s["luminance"] <= luminance_max)
                and (sharpness_min is None or row["blur_score"] >= sharpness_min))
        row["kept"] = keep
        results.append(row)
        if progress_callback:
            progress_callback(i + 1, total)
    return results


# ── v2.3: quality ranking + burst grouping ───────────────────────────────────

# Weighted-score components (documented contract — agents may cite them):
#   exposure closeness to 0.5 .35 | contrast .25 | sharpness .25 | saturation .15
# minus 1.5 pts per over/under-exposed %. Deliberately conservative: the
# score ranks a set, it never claims absolute "goodness".
_SCORE_WEIGHTS = {"exposure": 0.35, "contrast": 0.25,
                  "sharpness": 0.25, "saturation": 0.15}


def score_files(files: List[str],
                progress_callback: Optional[Callable[[int, int], None]] = None,
                ) -> List[dict]:
    """Weighted quality score (0-100, higher = better) per image.

    One ``analyze_image`` per file (sample-capped, fast); rows carry the
    components so the ranking is explainable: ``{path, ok, score,
    luminance, contrast, saturation, blur_score, overexposed_pct,
    underexposed_pct}``, sorted best-first. Unreadable images score 0 and
    sort last.
    """
    from .metrics import analyze_image

    rows: List[dict] = []
    total = len(files)
    for i, f in enumerate(files):
        a = analyze_image(f)
        if not a.get("ok"):
            rows.append({"path": f, "ok": False, "score": 0})
        else:
            lum = float(a["exposure"]["luminance"])
            contrast = float(a["stats"]["contrast"])
            sat = float(a["stats"]["saturation_mean"])
            blur = float(a.get("blur_score", 0.0) or 0.0)
            over = float(a["exposure"]["overexposed_pct"])
            under = float(a["exposure"]["underexposed_pct"])
            exposure_q = 1.0 - min(abs(lum - 0.5) / 0.5, 1.0)
            w = _SCORE_WEIGHTS
            score = 100.0 * (
                w["exposure"] * exposure_q
                + w["contrast"] * min(contrast / 0.15, 1.0)
                + w["sharpness"] * min(blur / 0.15, 1.0)
                + w["saturation"] * min(sat / 0.3, 1.0))
            score -= 1.5 * (over + under)
            rows.append({
                "path": f, "ok": True,
                "score": round(max(0.0, min(100.0, score)), 1),
                "luminance": lum,
                "contrast": contrast,
                "saturation": sat,
                "blur_score": blur,
                "overexposed_pct": over,
                "underexposed_pct": under,
            })
        if progress_callback:
            progress_callback(i + 1, total)
    rows.sort(key=lambda r: (-r.get("score", 0.0), r["path"]))
    return rows


def _exif_epoch(path: str) -> Optional[float]:
    """EXIF DateTimeOriginal → epoch seconds; None when absent/unparseable."""
    from PIL import Image
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            sub = exif.get_ifd(0x8769)
            raw = (sub.get(0x9003) or sub.get(0x9004)
                   or exif.get(0x0132) or "")
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    # "2024:07:30 14:30:00" (strip a trailing sub-sec like ".12" first)
    raw = str(raw).split(".")[0].strip()
    import time as _time
    try:
        return _time.mktime(_time.strptime(raw, "%Y:%m:%d %H:%M:%S"))
    except ValueError:
        return None


def group_bursts(files: List[str],
                 gap_seconds: float = 2.0) -> List[Dict[str, object]]:
    """Cluster shot-to-shoot by EXIF time: consecutive frames closer than
    ``gap_seconds`` (default 2s) belong to one burst.

    Files without EXIF time fall back to mtime. Rows: ``{start, end, count,
    files, span_seconds}`` sorted by start. Single-file groups are normal
    (non-burst frames) — callers keep their best anyway.
    """
    import os
    import time as _time

    def stamp(p: str) -> float:
        e = _exif_epoch(p)
        if e is not None:
            return e
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    timed = sorted(((stamp(p), p) for p in files), key=lambda t: (t[0], t[1]))
    groups: List[Dict[str, object]] = []
    cur: List[str] = []
    cur_start = 0.0
    prev_t: Optional[float] = None
    gap = max(0.1, float(gap_seconds))
    for t, p in timed:
        if prev_t is None or (t - prev_t) <= gap:
            if not cur:
                cur_start = t
            cur.append(p)
        else:
            groups.append(cur)
            cur = [p]
            cur_start = t
        prev_t = t
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        ts = [stamp(p) for p in g]
        out.append({
            "start": _time.strftime("%Y-%m-%d %H:%M:%S",
                                    _time.localtime(min(ts))),
            "end": _time.strftime("%Y-%m-%d %H:%M:%S",
                                  _time.localtime(max(ts))),
            "count": len(g),
            "span_seconds": round(max(ts) - min(ts), 1),
            "files": g,
        })
    return out


def best_of_bursts(files: List[str], gap_seconds: float = 2.0,
                   progress_callback: Optional[Callable[[int, int], None]] = None,
                   ) -> List[dict]:
    """One keeper per burst: score every file, keep each group's best.

    Returns ``score_files``-shaped rows (sorted best-first overall) with
    ``burst_best`` (bool) + ``burst_index``/``burst_size``; plus a
    ``groups`` summary is NOT included — call ``group_bursts`` separately
    when the grouping itself matters.
    """
    rows = score_files(files, progress_callback=progress_callback)
    by_path = {r["path"]: r for r in rows}
    groups = group_bursts(files, gap_seconds=gap_seconds)
    for g_idx, g in enumerate(groups):
        best_path, best_score = None, -1.0
        for p in g["files"]:
            s = by_path[p].get("score", 0.0)
            if s > best_score:
                best_path, best_score = p, s
        for p in g["files"]:
            r = by_path[p]
            r["burst_best"] = (p == best_path)
            r["burst_index"] = g_idx
            r["burst_size"] = g["count"]
    return rows
