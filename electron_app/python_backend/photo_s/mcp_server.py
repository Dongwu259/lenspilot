"""
PhotoS - MCP (Model Context Protocol) server.

Exposes PhotoS tools
(process/info/exif/dedup/cull/select/hdr/blurfaces/hash/plugin) to MCP
clients (Claude Desktop, AI agents) over stdio. Tools call photo_s module
functions directly (no CLI subprocess) and return JSON-serializable dicts
whose shapes mirror the `--json` CLI contract.

Requires the optional `mcp` extra: `pip install 'photo-s-tools[mcp]'`
(mcp>=1.20,<2 — the SDK requires Python >= 3.10).

IMPORTANT: this module must never import mcp at module level. photo-s
supports Python 3.9 while the mcp SDK needs 3.10+; the lazy import keeps 3.9
runs harmless and produces a clear install/version hint instead of an
ImportError. Also: MCP stdio owns stdout (JSON-RPC), so every tool here is
print-free — diagnostics go to stderr only.
"""

import asyncio
import contextlib
import functools
import json
import os
import secrets
import sys
import threading
import time
import uuid
from typing import List, Optional

from . import __version__
from .contract import versioned
from .engine import (ProcessOptions, batch_process, apply_exif_tags,
                     read_exif_metadata)

# Config-file base (set by create_server from --config); tool args win.
_base_options: ProcessOptions = ProcessOptions()

# ── Background watch state (option A: daemon thread + module-level state) ──
# Mirrors server._TASKS: MCP tools are short-lived calls, so a long-running
# watcher lives in a daemon thread and its progress is polled via
# watch_status / watch_stop. Daemon threads die with the MCP process, so a
# watch session ends when the MCP session ends.
_WATCH_LOCK = threading.Lock()
_WATCHES: dict = {}  # id -> {dir, recursive, timeout, opts, stop_event,
                     #          results:list, error, started_at, thread, stopped}
_MAX_WATCHES = 20


def _versioned(fn):
    """Attach the additive ``schema_version`` marker to a tool's dict return.

    ``functools.wraps`` preserves the signature and type hints, so FastMCP's
    inputSchema derivation sees the original tool parameters (verified
    against mcp 1.28.1).
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        return versioned(out) if isinstance(out, dict) else out
    return _wrapper


def _mcp():
    """Lazily import FastMCP (mirrors denoise._cv2's pattern)."""
    try:
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("photo-s",
                         instructions="PhotoS MCP server - batch image "
                                      "processing. Returns JSON-only results.")
        # FastMCP has no version kwarg in mcp>=1.20; without this the
        # initialize handshake reports the *SDK* version (e.g. 1.29.0)
        # as serverInfo instead of the PhotoS version.
        server._mcp_server.version = __version__
        return server
    except ImportError:
        raise RuntimeError(
            "MCP server requires the optional dependency: "
            "pip install 'photo-s-tools[mcp]' (mcp>=1.20,<2)")


# ── Tool implementations (module-level, individually testable) ──────────────


@_versioned
def process_tool(
    paths: list,
    recursive: bool = False,
    quality: Optional[int] = None,
    output_format: Optional[str] = None,
    output_dir: Optional[str] = None,
    resize: Optional[str] = None,
    scale: Optional[int] = None,
    suffix: Optional[str] = None,
    target_size: Optional[str] = None,
    strip_gps: Optional[bool] = None,
    denoise: Optional[float] = None,
    auto_tone: Optional[float] = None,
    ev: Optional[float] = None,
    log_curve: Optional[str] = None,
    wb_temp: Optional[int] = None,
    lut_file: Optional[str] = None,
    brightness: Optional[float] = None,
    contrast: Optional[float] = None,
    saturation: Optional[float] = None,
    auto_straighten: Optional[bool] = None,
    # Lightroom-direction grading (v1.6.0)
    wb_tint: Optional[float] = None,
    levels: Optional[str] = None,
    curves: Optional[str] = None,
    vibrance: Optional[float] = None,
    color_grading: Optional[str] = None,
    hsl: Optional[str] = None,
    clarity: Optional[float] = None,
    texture: Optional[float] = None,
    dehaze: Optional[float] = None,
    vignette: Optional[str] = None,
    grain: Optional[str] = None,
    # Local adjustments + lens correction (v1.7.0)
    masks: Optional[str] = None,
    mask_adjust: Optional[str] = None,
    point_color: Optional[str] = None,
    lens_distort: Optional[float] = None,
    lens_vignette: Optional[str] = None,
    lens_ca: Optional[str] = None,
    # naming / organization / watermark / multi-size / RAW decode options
    # (parity with the CLI batch surface)
    rename_pattern: Optional[str] = None,
    organize: Optional[str] = None,
    watermark_text: Optional[str] = None,
    watermark_image: Optional[str] = None,
    watermark_position: Optional[str] = None,
    watermark_opacity: Optional[int] = None,
    sizes: Optional[list] = None,
    raw_half_size: Optional[bool] = None,
    raw_no_auto_bright: Optional[bool] = None,
    raw_demosaic: Optional[str] = None,
    raw_color_space: Optional[str] = None,
    raw_16bit: Optional[bool] = None,
    jobs: Optional[int] = None,
    dry_run: bool = False,
    evaluate: bool = False,
) -> dict:
    """Batch process images: quality/format/resize/tone/exposure/denoise.

    ``paths`` accepts files, directories or globs (e.g. ["/shoot/*.jpg"]).
    Returns a BatchResult JSON with per-file status (same shape as
    ``photo-s batch --json``), plus ``ok``. With ``evaluate=True`` each
    result also carries ``ssim`` (input vs output, same as ``--evaluate``).
    """
    from .server import _options_from_dict
    from .cli import _collect_files, _parse_dimensions

    data = {
        "quality": quality, "output_format": output_format,
        "output_dir": output_dir, "scale_percent": scale,
        "suffix": suffix, "target_size": target_size,
        "strip_gps": strip_gps, "denoise": denoise,
        "auto_tone": auto_tone, "ev": ev,
        "log_curve": log_curve, "wb_temp": wb_temp,
        "lut_file": lut_file, "brightness": brightness,
        "contrast": contrast, "saturation": saturation,
        "auto_straighten": auto_straighten, "jobs": jobs,
        "evaluate": evaluate,
        "wb_tint": wb_tint, "levels": levels, "curves": curves,
        "vibrance": vibrance, "color_grading": color_grading,
        "hsl": hsl, "clarity": clarity, "texture": texture,
        "dehaze": dehaze, "vignette": vignette, "grain": grain,
        "masks": masks, "mask_adjust": mask_adjust,
        "point_color": point_color, "lens_distort": lens_distort,
        "lens_vignette": lens_vignette, "lens_ca": lens_ca,
        "watermark_text": watermark_text, "watermark_image": watermark_image,
        "watermark_position": watermark_position,
        "watermark_opacity": watermark_opacity,
        "rename_pattern": rename_pattern,
        "raw_half_size": raw_half_size,
        "raw_demosaic": raw_demosaic, "raw_color_space": raw_color_space,
        "raw_16bit": raw_16bit,
    }
    data = {k: v for k, v in data.items() if v is not None}
    if organize:
        from .engine import _resolve_folder_pattern
        data["folder_pattern"] = _resolve_folder_pattern(organize)
    if raw_no_auto_bright is not None:
        data["raw_auto_bright"] = not raw_no_auto_bright
    if sizes:
        # accept [["thumb", 480, None], ...] like the REST endpoint
        data["output_sizes"] = sizes
    if resize:
        w, h = _parse_dimensions(resize)
        data["max_width"], data["max_height"] = w, h

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False,
                "error": "no supported image files found",
                "paths": list(paths)}

    if dry_run:
        return {
            "dry_run": True, "count": len(files), "files": files,
            "settings": {
                "output_format": data.get("output_format"),
                "quality": data.get("quality"),
                "target_size": data.get("target_size"),
                "output_dir": data.get("output_dir"),
                "max_width": data.get("max_width"),
                "max_height": data.get("max_height"),
                "scale_percent": data.get("scale_percent"),
                "suffix": data.get("suffix"),
                "jobs": data.get("jobs"),
            },
        }

    opts = _options_from_dict(data, base=_base_options)
    result = batch_process(files, opts)
    payload = result.to_dict()
    payload["ok"] = result.fail_count == 0
    return payload


@_versioned
def bench_tool(
    dir: str,
    jobs: Optional[list] = None,
    images: Optional[int] = None,
    denoise: Optional[float] = None,
    evaluate: bool = False,
) -> dict:
    """Benchmark the pipeline at several worker counts to pick optimal
    concurrency (mirrors ``photo-s bench``).

    Runs over real images in ``dir`` (temp-dir outputs, sources untouched)
    and reports wall time, speedup, per-stage breakdown and — with
    ``evaluate=True`` — PSNR/SSIM of the first run against the sources.
    """
    from .cli import _collect_files
    from .bench import run_benchmark
    from .engine import ProcessOptions

    if not os.path.isdir(dir):
        return {"ok": False, "error": f"not a directory: {dir}", "dir": dir}
    files = _collect_files([dir], recursive=True)
    if not files:
        return {"ok": False,
                "error": f"no supported image files found in {dir}",
                "dir": dir}
    if images:
        files = files[:images]
    job_list = [int(x) for x in (jobs or [1, 2, 4, 8])]
    job_list = list(dict.fromkeys(job_list))  # dedupe, keep order
    if not job_list or any(j < 1 for j in job_list):
        return {"ok": False, "error": "jobs must be positive integers",
                "jobs": list(jobs) if jobs else None}
    base = ProcessOptions(quality=85, output_format="JPEG", denoise=denoise)
    report = run_benchmark(files, job_list, base, evaluate=evaluate)
    return {"ok": True, "dir": dir, "files": len(files),
            "runs": report["runs"], "evaluate": report["evaluate"]}


@_versioned
def info_tool() -> dict:
    """Environment probe: version, supported formats, writable formats,
    optional-feature status and installed plugins (same shape as
    ``photo-s info --json``)."""
    from .engine import ALL_INPUT_EXTENSIONS, SUPPORTED_FORMATS, PIL_WRITABLE
    from .envinfo import optional_features, plugins
    return {
        "version": __version__,
        "input_extensions": sorted(ALL_INPUT_EXTENSIONS),
        "formats": sorted(SUPPORTED_FORMATS),
        "writable": sorted(PIL_WRITABLE),
        "optional_features": optional_features(),
        "plugins": plugins(),
    }


@_versioned
def exif_tool(
    action: str = "show",
    paths: Optional[list] = None,
    recursive: bool = False,
    rating_min: Optional[int] = None,
    rating: Optional[int] = None,
    keywords: Optional[str] = None,
    camera: Optional[str] = None,
    tags: Optional[dict] = None,
    gps: Optional[str] = None,
) -> dict:
    """Read/filter or write EXIF metadata.

    ``action="show"`` scans ``paths`` and returns per-file metadata, filtered
    by rating/keywords/camera. ``action="write"`` takes ``tags`` as a
    {"<path>": {"rating": 4, "keywords": "keep", ...}} map. ``gps="lat,lon"``
    writes the same coordinates to every file in ``paths`` (batch geotag).
    """
    from .cli import _collect_files

    if action == "write":
        written, errors = 0, []
        # batch GPS: same coordinates applied to every path
        if gps:
            for path in _collect_files(list(paths or []), recursive=recursive):
                try:
                    apply_exif_tags(path, {"gps": gps})
                    written += 1
                except Exception as e:  # per-file errors, don't abort the batch
                    errors.append({"path": path, "error": str(e)})
            return {"action": "write", "written": written, "errors": errors}
        for path, tagdict in (tags or {}).items():
            try:
                apply_exif_tags(path, tagdict)
                written += 1
            except Exception as e:  # per-file errors, don't abort the batch
                errors.append({"path": path, "error": str(e)})
        return {"action": "write", "written": written, "errors": errors}

    # show / filter (mirrors `photo-s exif --show`)
    files = _collect_files(list(paths or []), recursive=recursive)
    results = []
    for p in files:
        try:
            m = read_exif_metadata(p)
        except Exception:
            continue
        if rating_min is not None and (m.get("rating") or 0) < rating_min:
            continue
        if rating is not None and m.get("rating") != rating:
            continue
        if keywords:
            kws = m.get("keywords") or []
            if not any(k in kws for k in
                       [k.strip() for k in keywords.split(",") if k.strip()]):
                continue
        if camera and (camera.lower() not in (m.get("camera") or "").lower()):
            continue
        results.append({"path": p, **m})
    return {"action": "show", "count": len(results), "results": results}


@_versioned
def dedup_tool(
    paths: list,
    recursive: bool = False,
    threshold: int = 5,
    action: str = "report",
    dry_run: bool = True,
) -> dict:
    """Find duplicate images by perceptual hash.

    ``action="report"`` lists duplicate groups. ``action="keep-sharpest"``
    keeps the sharpest of each burst and removes the rest — SAFETY: deletion
    requires ``dry_run=False`` explicitly (default True).
    """
    from .cli import _collect_files
    from .dedup import find_duplicates, handle_duplicates

    files = _collect_files(list(paths), recursive=recursive)
    groups = find_duplicates(files, threshold=threshold)

    if action == "keep-sharpest":
        kept, removed = handle_duplicates(groups, action="keep-sharpest",
                                          dry_run=dry_run)
        return {"action": "keep-sharpest", "kept": kept, "removed": removed,
                "dry_run": dry_run}

    savings = 0
    group_list = []
    for h, paths_in_group in groups.items():
        for extra in paths_in_group[1:]:
            try:
                savings += os.path.getsize(extra)
            except OSError:
                pass
        group_list.append({"hash": h, "paths": list(paths_in_group)})
    return {"count": len(group_list),
            "duplicate_count": sum(len(g["paths"]) - 1 for g in group_list),
            "savings_bytes": savings, "groups": group_list}


@_versioned
def cull_tool(
    paths: list,
    recursive: bool = False,
    overexposed_max: Optional[float] = None,
    underexposed_max: Optional[float] = None,
    luminance_min: Optional[float] = None,
    luminance_max: Optional[float] = None,
    sharpness_min: Optional[float] = None,
) -> dict:
    """Filter images by exposure and sharpness thresholds.

    Returns per-file stats plus a ``kept`` flag (mirrors ``photo-s cull``).
    """
    from .cli import _collect_files
    from .metrics import compute_exposure_stats, compute_blur_score

    files = _collect_files(list(paths), recursive=recursive)
    results, kept = [], 0
    for p in files:
        try:
            s = compute_exposure_stats(p)
        except Exception:
            continue
        if not s.get("ok"):
            continue
        blur = None
        if sharpness_min is not None:
            blur = round(compute_blur_score(p), 1)
        ok = True
        if overexposed_max is not None and s["overexposed_pct"] > overexposed_max:
            ok = False
        if underexposed_max is not None and s["underexposed_pct"] > underexposed_max:
            ok = False
        if luminance_min is not None and s["luminance"] < luminance_min:
            ok = False
        if luminance_max is not None and s["luminance"] > luminance_max:
            ok = False
        if sharpness_min is not None and (blur or 0) < sharpness_min:
            ok = False
        results.append({
            "path": p,
            "luminance": s["luminance"],
            "overexposed_pct": s["overexposed_pct"],
            "underexposed_pct": s["underexposed_pct"],
            "blur_score": blur,
            "kept": ok,
        })
        if ok:
            kept += 1
    return {"count": len(results), "kept": kept, "results": results}


@_versioned
def select_tool(
    paths: list,
    recursive: bool = False,
    keep_min: int = 4,
    reject_max: int = 2,
    selects_dir: Optional[str] = None,
    rejects_dir: Optional[str] = None,
    mode: str = "move",
    dry_run: bool = False,
) -> dict:
    """Sort rated photos into selects/rejects folders (keeper workflow).

    Reads EXIF ratings (the PhotoS: UserComment payload the review flow
    writes): rating >= keep_min → keeper (moved to ``selects_dir``),
    rating <= reject_max → reject (moved to ``rejects_dir``), everything in
    between stays in place. ``mode="copy"`` keeps originals; ``dry_run``
    reports would-moves with zero filesystem writes.
    """
    from .cli import _collect_files
    from .select import select_files

    files = _collect_files(list(paths), recursive=recursive)
    try:
        results = select_files(
            files, keep_min=keep_min, reject_max=reject_max,
            selects_dir=selects_dir, rejects_dir=rejects_dir,
            mode=mode, dry_run=dry_run,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    kept = sum(1 for r in results if r["status"] == "keep")
    rejected = sum(1 for r in results if r["status"] == "reject")
    moved = sum(1 for r in results if r["action"].startswith(("move", "copy", "would")))
    return {"ok": True, "count": len(results), "kept": kept,
            "rejected": rejected, "moved": moved, "dry_run": dry_run,
            "results": results}


@_versioned
def hdr_tool(
    paths: list,
    output: str,
    align: bool = False,
) -> dict:
    """Merge bracketed exposures into one HDR image (exposure fusion).

    Requires the optional opencv extra (``pip install photo-s-tools[enhance]``
    when missing). ``align=True`` runs AlignMTB so handheld brackets merge
    without ghosting.
    """
    from .hdr import merge_hdr
    try:
        result = merge_hdr(list(paths), align=align)
        result.save(output, quality=95)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "output": os.path.abspath(output),
            "count": len(paths), "align": align, "dims": list(result.size)}


@_versioned
def blurfaces_tool(
    paths: list,
    recursive: bool = False,
    mode: str = "blur",
    margin: int = 20,
    output_dir: Optional[str] = None,
) -> dict:
    """Detect and blur/pixelate faces in a batch of images (privacy).

    Runs the standard batch pipeline with ``blur_faces`` set, so EXIF/ICC are
    preserved and naming follows batch rules. Requires the optional opencv
    extra; per-file failures are reported, not fatal.
    """
    from .cli import _collect_files
    from .engine import batch_process, ProcessOptions

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found"}
    opts = ProcessOptions(
        output_dir=output_dir,
        blur_faces=mode,
        blur_faces_margin=margin,
        suffix="_blurred",
        preserve_exif=True,
    )
    batch = batch_process(files, opts)
    results = batch.results
    ok = [r for r in results if r.success]
    return {"ok": len(ok) == len(results),
            "count": len(results), "success": len(ok),
            "results": [r.to_dict() for r in results]}


@_versioned
def hash_tool(
    paths: Optional[list] = None,
    recursive: bool = False,
    output: Optional[str] = None,
    verify: Optional[str] = None,
) -> dict:
    """Generate or verify a SHA-256 checksum manifest (any file type).

    Generate: ``paths`` + optional ``output`` (default "manifest.csv");
    returns the entries inline so agents needn't read the CSV.
    Verify: pass ``verify`` = manifest path.
    """
    from .check import (collect_files, compute_checksums, write_manifest,
                        verify_manifest)

    if verify:
        report = verify_manifest(verify)
        report["ok"] = not report.get("missing") and \
            not report.get("mismatched")
        return report

    files = collect_files(list(paths or []), recursive=recursive)
    entries = compute_checksums(files)
    if output:
        out = os.path.abspath(output)
    else:
        # default used to be "manifest.csv" resolved against the SERVER's
        # cwd — a surprise write wherever the MCP process happens to run.
        import tempfile
        fd, out = tempfile.mkstemp(prefix="photos-manifest-", suffix=".csv")
        os.close(fd)
    write_manifest(out, entries)
    return {"output": out, "count": len(files), "entries": entries}


@_versioned
def plugin_tool(
    action: str = "list",
    name: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Manage official plugins: ``list`` installed/available, ``install`` or
    ``uninstall`` by name (shells to pip; ``dry_run`` prints the pip argv)."""
    from .registry import OFFICIAL_PLUGINS, get_official, to_dict
    from .plugin import discover_plugins
    from .plugincmd import _pip_run, _installed_version

    if action == "list":
        installed_objs = {}
        for p in discover_plugins():
            installed_objs[p.name] = p
        installed = []
        for n, p in installed_objs.items():
            installed.append({
                "name": n,
                "provides": list(getattr(p, "provides", ())),
                "version": _installed_version("photo-s-plugin-" + n),
            })
        available = []
        for n, official in OFFICIAL_PLUGINS.items():
            entry = to_dict(official)
            entry["installed"] = n in installed_objs
            available.append(entry)
        return {"installed": installed, "available": available}

    if action not in ("install", "uninstall"):
        return {"ok": False, "name": name,
                "error": f"unknown action: {action}",
                "actions": ["list", "install", "uninstall"]}

    official = get_official(name) if name else None
    if official is None:
        return {"ok": False, "name": name,
                "error": "not in official registry"}

    dist = official.pypi_distribution
    argv = (["install", "--quiet", dist] if action == "install"
            else ["uninstall", "-y", dist])
    if dry_run:
        # argv preview only — no pip run, safe without the opt-in
        return {"ok": True, "name": name, "dry_run": True,
                "pip_argv": [sys.executable, "-m", "pip", *argv]}
    if not os.environ.get("PHOTO_S_ALLOW_REMOTE_PLUGINS"):
        # pip install + auto-import on the next discover() = remote code
        # execution; requires an explicit operator opt-in.
        return {"ok": False, "name": name,
                "error": ("plugin install/uninstall is disabled; set "
                          "PHOTO_S_ALLOW_REMOTE_PLUGINS=1 in the server "
                          "environment to enable")}
    try:
        proc = _pip_run(argv)
    except FileNotFoundError:
        return {"ok": False, "name": name, "error": "pip not available"}
    if proc.returncode != 0:
        return {"ok": False, "name": name,
                "error": "pip {} failed".format(action),
                "detail": (proc.stderr or "").strip()[-400:]}
    payload = {"ok": True, "name": name, "action": action,
               "distribution": dist}
    ver = _installed_version(dist)
    if ver:
        payload["version"] = ver
    return payload


@_versioned
def contact_sheet_tool(
    paths: list,
    output: str,
    recursive: bool = False,
    cols: int = 4,
    thumb: int = 240,
    captions: bool = True,
    bg: str = "#000000",
) -> dict:
    """Build a contact sheet (grid montage) from images.

    ``output`` is the destination image path. Returns ``{"output", "count"}``
    (same shape as ``photo-s contact-sheet --json``).
    """
    from .adjust import hex_to_rgb
    from .cli import _collect_files
    from .contact import build_contact_sheet

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    try:
        bg_rgb = hex_to_rgb(bg)
    except ValueError:
        bg_rgb = (0, 0, 0)
    out = build_contact_sheet(files, output, cols=cols,
                              thumb_size=(thumb, thumb), captions=captions,
                              bg=bg_rgb)
    return {"ok": True, "output": out, "count": len(files)}


@_versioned
def gallery_tool(
    paths: list,
    out_dir: str,
    recursive: bool = False,
    title: str = "PhotoS Gallery",
    thumb: int = 360,
) -> dict:
    """Generate an HTML gallery from images.

    Returns ``{"output", "count"}`` (same shape as ``photo-s gallery --json``).
    """
    from .cli import _collect_files
    from .gallery import build_gallery

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    res = build_gallery(files, out_dir, title=title, thumb_size=thumb)
    res["ok"] = True
    return res


@_versioned
def watermark_tool(
    paths: list,
    text: str = "",
    image: str = "",
    position: str = "BOTTOM_RIGHT",
    opacity: int = 50,
    output_format: Optional[str] = None,
    quality: Optional[int] = None,
    output_dir: Optional[str] = None,
    recursive: bool = False,
) -> dict:
    """Overlay a text or image watermark on images via the batch pipeline.

    Returns a BatchResult JSON with per-file status, plus ``ok``.
    """
    from .server import _options_from_dict
    from .cli import _collect_files

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False,
                "error": "no supported image files found",
                "paths": list(paths)}

    data = {
        "watermark_text": text, "watermark_image": image,
        "watermark_position": position, "watermark_opacity": opacity,
        "output_format": output_format, "quality": quality,
        "output_dir": output_dir,
    }
    data = {k: v for k, v in data.items() if v is not None}
    opts = _options_from_dict(data, base=_base_options)
    result = batch_process(files, opts)
    payload = result.to_dict()
    payload["ok"] = result.fail_count == 0
    return payload


@_versioned
def preset_tool(
    action: str,
    name: str = "",
    description: str = "",
    options: dict = {},
) -> dict:
    """Manage processing presets: list / save / load / delete.

    ``load`` returns the preset's ProcessOptions as a JSON dict that can be
    fed straight back into ``process``. ``save`` takes an ``options`` dict.
    """
    from .presets import (delete_preset, list_presets, load_preset,
                          save_preset)

    if action == "list":
        return {"ok": True, "presets": list_presets()}
    if not name:
        return {"ok": False, "error": "name is required for this action"}

    if action == "save":
        from .server import _options_from_dict
        opts = _options_from_dict(options or {}, base=_base_options)
        save_preset(name, opts, description=description)
        return {"ok": True, "name": name, "action": "save"}

    if action == "load":
        opts = load_preset(name)
        if opts is None:
            return {"ok": False, "name": name, "error": "preset not found"}
        return {"ok": True, "name": name, "options": opts.__dict__}

    if action == "delete":
        ok = delete_preset(name)
        return {"ok": ok, "name": name, "action": "delete",
                "deleted": ok}
    return {"ok": False, "error": f"unknown action: {action}",
            "actions": ["list", "save", "load", "delete"]}


# ── Watch tools (background thread + module-level state) ────────────────────


def _prune_watches() -> None:
    """Drop dead/stopped records beyond the cap (mirrors server._prune_tasks)."""
    if len(_WATCHES) <= _MAX_WATCHES:
        return
    dead = [k for k, v in _WATCHES.items()
            if not v["thread"].is_alive()
            and (v["stop_event"].is_set() or v.get("stopped"))]
    for k in sorted(dead)[: len(_WATCHES) - _MAX_WATCHES]:
        _WATCHES.pop(k, None)


@_versioned
def watch_tool(
    dir: str,
    recursive: bool = False,
    quality: Optional[int] = None,
    output_format: Optional[str] = None,
    output_dir: Optional[str] = None,
    resize: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Watch a directory and auto-process new images in the background.

    Returns immediately with a watch ``id``; poll ``watch_status`` for
    progress (per-file results) and call ``watch_stop`` to end it. Needs
    the optional ``watch`` extra (watchdog).
    """
    import importlib.util
    from .server import _options_from_dict
    from .cli import _parse_dimensions

    if not os.path.isdir(dir):
        return {"started": False, "error": f"not a directory: {dir}",
                "dir": dir}
    if importlib.util.find_spec("watchdog") is None:
        # Pre-check so the watcher's stdout "install me" hint never prints.
        return {"started": False,
                "error": "watchdog not installed. Run: "
                         "pip install photo-s-tools[watch]",
                "install": "pip install photo-s-tools[watch]"}
    if timeout is not None and timeout <= 0:
        return {"started": False,
                "error": "timeout must be a positive number of seconds"}

    data = {"quality": quality, "output_format": output_format,
            "output_dir": output_dir}
    data = {k: v for k, v in data.items() if v is not None}
    if resize:
        w, h = _parse_dimensions(resize)
        data["max_width"], data["max_height"] = w, h
    opts = _options_from_dict(data, base=_base_options)

    wid = secrets.token_urlsafe(8)   # mirrors server start_task ids
    stop_event = threading.Event()
    record = {
        "dir": dir, "recursive": recursive, "timeout": timeout,
        "opts": opts, "stop_event": stop_event, "results": [],
        "error": None, "started_at": time.time(), "stopped": False,
    }

    def runner():
        from .watcher import start_watching
        try:
            # MCP stdio owns stdout (JSON-RPC) — route the watcher's
            # diagnostics to stderr so frames stay clean.
            with contextlib.redirect_stdout(sys.stderr):
                start_watching(
                    record["dir"], record["opts"],
                    recursive=record["recursive"],
                    on_process=lambda r: record["results"].append(r.to_dict()),
                    stop_event=record["stop_event"],
                )
        except Exception as e:
            record["error"] = str(e)

    record["thread"] = threading.Thread(target=runner, daemon=True)
    with _WATCH_LOCK:
        _prune_watches()
        _WATCHES[wid] = record
    record["thread"].start()
    if timeout:
        threading.Timer(timeout, stop_event.set).start()  # auto-stop safety

    return {
        "started": True, "id": wid, "dir": dir, "recursive": recursive,
        "options": {"quality": quality, "output_format": output_format,
                    "output_dir": output_dir, "resize": resize},
        "timeout": timeout,
    }


@_versioned
def watch_status_tool(id: str) -> dict:
    """Report the state of a background watch (running, processed count,
    per-file results, error)."""
    with _WATCH_LOCK:
        rec = _WATCHES.get(id)
    if rec is None:
        return {"ok": False, "error": f"no such watch: {id}"}
    return {
        "ok": True, "id": id, "dir": rec["dir"],
        "recursive": rec["recursive"],
        "running": rec["thread"].is_alive(),
        "stopped": rec["stop_event"].is_set() or rec.get("stopped", False),
        "processed_count": len(rec["results"]),
        "results": list(rec["results"]),   # shallow copy; append is single-writer
        "error": rec["error"],
        "started_at": rec["started_at"],
    }


@_versioned
def watch_stop_tool(id: str) -> dict:
    """Stop a background watch; results so far stay visible via watch_status."""
    with _WATCH_LOCK:
        rec = _WATCHES.get(id)
    if rec is None:
        return {"ok": False, "error": f"no such watch: {id}"}
    rec["stop_event"].set()
    rec["stopped"] = True
    return {"ok": True, "id": id, "stopped": True,
            "processed_count": len(rec["results"])}


# ── autopilot（v2.5 无人值守闭环：watch → suggest/auto-tone → audit → 分流）──

_AUTOPILOT_LOCK = threading.Lock()
_AUTOPILOTS: dict = {}
_MAX_AUTOPILOTS = 10


def _prune_autopilots() -> None:
    if len(_AUTOPILOTS) <= _MAX_AUTOPILOTS:
        return
    dead = [k for k, v in _AUTOPILOTS.items()
            if not v["thread"].is_alive()
            and (v["stop_event"].is_set() or v.get("stopped"))]
    for k in sorted(dead)[: len(_AUTOPILOTS) - _MAX_AUTOPILOTS]:
        _AUTOPILOTS.pop(k, None)


@_versioned
def autopilot_start_tool(
    dir: str,
    out_dir: Optional[str] = None,
    mode: str = "suggest",
    auto_tone: Optional[float] = None,
    scale: float = 1.0,
    aesthetic: Optional[float] = None,
    overexposed_max: Optional[float] = None,
    underexposed_max: Optional[float] = None,
    blur_min: Optional[float] = None,
    write_xmp: bool = False,
    recursive: bool = False,
    scan_existing: bool = False,
    quality: Optional[int] = None,
    output_format: Optional[str] = None,
    resize: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Run the unattended pipeline on a directory: watch -> suggest/auto-tone
    -> process -> audit -> route outputs to passed/ or review/ (JSONL trace).

    Returns immediately with an ``id``; poll ``autopilot_status`` for per-file
    records (params, audit verdict, routed path); ``autopilot_stop`` ends it.
    mode=suggest needs nothing; auto_tone/both need the auto-tone plugin;
    aesthetic needs its verifier. Needs watchdog (photo-s-tools[watch]).
    """
    import importlib.util
    from .autopilot import AutopilotConfig, validate_config

    if not os.path.isdir(dir):
        return {"started": False, "error": f"not a directory: {dir}",
                "dir": dir}
    if importlib.util.find_spec("watchdog") is None:
        return {"started": False,
                "error": "watchdog not installed. Run: "
                         "pip install photo-s-tools[watch]",
                "install": "pip install photo-s-tools[watch]"}
    if timeout is not None and timeout <= 0:
        return {"started": False,
                "error": "timeout must be a positive number of seconds"}

    thresholds = {k: v for k, v in
                  (("overexposed_max", overexposed_max),
                   ("underexposed_max", underexposed_max),
                   ("blur_min", blur_min)) if v is not None}
    cfg = AutopilotConfig(
        watch_dir=os.path.abspath(dir),
        out_dir=os.path.abspath(out_dir) if out_dir else None,
        mode=mode,
        auto_tone_strength=auto_tone if auto_tone is not None else 1.0,
        scale=scale,
        thresholds=thresholds,
        aesthetic=aesthetic,
        write_xmp=write_xmp,
        recursive=recursive,
        scan_existing=scan_existing,
        quality=quality,
        output_format=output_format,
        resize=resize,
    )
    try:
        validate_config(cfg)
    except RuntimeError as e:
        return {"started": False, "error": str(e)}

    aid = secrets.token_urlsafe(8)
    stop_event = threading.Event()
    record = {
        "dir": cfg.watch_dir, "out_dir": cfg.out_root, "mode": mode,
        "config": {"mode": mode, "scale": cfg.scale, "aesthetic": aesthetic,
                   "write_xmp": write_xmp, "recursive": recursive,
                   "scan_existing": scan_existing,
                   "thresholds": thresholds},
        "stop_event": stop_event, "results": [], "passed": 0, "review": 0,
        "errors": 0, "error": None, "started_at": time.time(),
        "stopped": False,
    }

    def on_event(rec):
        record["results"].append(rec)
        if rec.get("error"):
            record["errors"] += 1
        elif rec.get("audit", {}).get("passed"):
            record["passed"] += 1
        else:
            record["review"] += 1

    def runner():
        from .autopilot import run_autopilot
        try:
            with contextlib.redirect_stdout(sys.stderr):
                run_autopilot(cfg, on_event=on_event,
                              stop_event=record["stop_event"])
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"

    record["thread"] = threading.Thread(target=runner, daemon=True)
    with _AUTOPILOT_LOCK:
        _prune_autopilots()
        _AUTOPILOTS[aid] = record
    record["thread"].start()
    if timeout:
        threading.Timer(timeout, stop_event.set).start()

    return {"started": True, "id": aid, "dir": cfg.watch_dir,
            "out_dir": cfg.out_root, "mode": mode,
            "log": os.path.join(cfg.out_root, "autopilot.jsonl"),
            "timeout": timeout}


@_versioned
def autopilot_status_tool(id: str) -> dict:
    """State of a background autopilot: counters (passed/review/errors),
    per-file records, error."""
    with _AUTOPILOT_LOCK:
        rec = _AUTOPILOTS.get(id)
    if rec is None:
        return {"ok": False, "error": f"no such autopilot: {id}"}
    return {
        "ok": True, "id": id, "dir": rec["dir"], "out_dir": rec["out_dir"],
        "mode": rec["mode"], "config": rec["config"],
        "running": rec["thread"].is_alive(),
        "stopped": rec["stop_event"].is_set() or rec.get("stopped", False),
        "processed_count": len(rec["results"]),
        "passed": rec["passed"], "review": rec["review"],
        "errors": rec["errors"],
        "results": list(rec["results"]),
        "error": rec["error"],
        "started_at": rec["started_at"],
    }


@_versioned
def autopilot_stop_tool(id: str) -> dict:
    """Stop a background autopilot; records so far stay visible via status."""
    with _AUTOPILOT_LOCK:
        rec = _AUTOPILOTS.get(id)
    if rec is None:
        return {"ok": False, "error": f"no such autopilot: {id}"}
    rec["stop_event"].set()
    rec["stopped"] = True
    return {"ok": True, "id": id, "stopped": True,
            "processed_count": len(rec["results"]),
            "passed": rec["passed"], "review": rec["review"]}


# ── 语义搜索（v2.5：index/find；embed 插件 = SigLIP 文本+图像，无插件 =
#    内置 84 维仅图像）──────────────────────────────────────────────────────

@_versioned
def index_tool(paths: list, recursive: bool = False,
               index: Optional[str] = None, rebuild: bool = False,
               tags: Optional[list] = None, min_score: float = 0.2,
               max_tags: int = 5, write_xmp: bool = False) -> dict:
    """Build (or incrementally update) a semantic embedding index for images.

    With the auto-tone plugin installed the index uses SigLIP (text + image,
    English queries work best); without it a built-in 84-dim histogram
    extractor is used (image-only queries). ``tags`` auto-tags every image
    whose cosine similarity to a tag clears ``min_score`` (writes EXIF
    keywords; ``write_xmp`` also writes an LR-visible dc:subject sidecar).
    Returns the index path — pass it to ``find`` as ``index``.
    """
    from .search import auto_tag, build_index
    try:
        summary = build_index(paths, recursive=recursive, index_path=index,
                              rebuild=rebuild)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    payload = dict(summary)
    if tags:
        try:
            res = auto_tag(summary["index"], tags, min_score=min_score,
                           max_tags=max_tags, write_xmp=write_xmp)
        except RuntimeError as e:
            payload["tags_error"] = str(e)
        else:
            payload["tags"] = {"tagged": res["tagged"],
                               "untouched": res["untouched"],
                               "assigned": res["assigned"]}
    payload["ok"] = True
    return payload


@_versioned
def find_tool(query: Optional[str] = None, image: Optional[str] = None,
              index: Optional[str] = None, k: int = 10,
              min_score: Optional[float] = None) -> dict:
    """Semantic search over an index built by ``index``: text query (English
    works best — SigLIP is English-centric) or search-by-image (any extractor). Returns ranked hits with
    cosine scores."""
    from .search import INDEX_FILENAME, find_similar
    index_path = index or os.path.join(".", INDEX_FILENAME)
    try:
        res = find_similar(index_path, text=query, image=image, k=k,
                           min_score=min_score)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    res["ok"] = True
    return res


@_versioned
def analyze_tool(paths: list, recursive: bool = False,
                 sample_size: int = 256, grid: int = 0) -> dict:
    """Perceptual analysis: histograms / channel stats / WB lean / exposure.

    The feedback half of the agent grading loop - call after ``process``
    to judge the result, adjust the grading params, process again.
    ``grid`` (4|8) adds per-cell luminance/saturation/color sampling plus
    sky/skin region ratios and over/underexposed bounding boxes - the
    input local adjustments need.
    """
    from .cli import _collect_files
    from .metrics import analyze_image

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    sample_size = max(16, min(2048, int(sample_size)))
    grid = int(grid or 0)
    if grid not in (4, 8):
        grid = 0
    results = [analyze_image(p, sample_size=sample_size, grid=grid)
               for p in files]
    return {"ok": all(r.get("ok") for r in results),
            "count": len(results), "results": results}


@_versioned
def suggest_tool(paths: list, recursive: bool = False,
                 scale: float = 1.0) -> dict:
    """Rule-based parameter suggestions: analyze stats → ProcessOptions fields.

    The bridge half of the agent grading loop - 'analyze' tells what is off,
    'suggest' maps it to conservative fix params (each with a reason and the
    metric it is based on). Output 'suggested' keys are engine field names
    (ev / wb_temp / wb_tint / contrast / vibrance / clarity /
    highlight_recovery / levels) that 'process' accepts directly. Neutral
    images return an empty dict - nothing objectively wrong. Zero models,
    offline; the auto-tone plugin is the personal-style AI layer on top.
    ``scale`` (0-1) shrinks the magnitude for gentler fixes.
    """
    from .cli import _collect_files
    from .suggest import suggest_file

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    scale = max(0.0, min(1.0, float(scale or 1.0)))
    results = [suggest_file(p, scale=scale) for p in files]
    return {"ok": all(r.get("ok") for r in results),
            "count": len(results),
            "neutral": sum(1 for r in results if r.get("neutral")),
            "results": results}


@_versioned
def diff_tool(path_a: str, path_b: str, sample_size: int = 256) -> dict:
    """Numeric before/after comparison: PSNR / SSIM / mean-abs-diff.

    Judgement for the grading loop - is the new version better or worse?
    """
    from .metrics import compare_images

    return compare_images(path_a, path_b,
                          sample_size=max(16, min(1024, int(sample_size))))


@_versioned
def audit_tool(paths: list, recursive: bool = False,
               overexposed_max: Optional[float] = None,
               underexposed_max: Optional[float] = None,
               blur_min: Optional[float] = None,
               aesthetic: Optional[float] = None) -> dict:
    """Quality gate: pass/fail + reasons (overexposure/blur/luminance...).

    The agent's stop condition - a photo that passes audit is "good enough".
    ``aesthetic`` (1-10) adds the model-based aesthetic gate (v2.4) —
    needs the auto-tone plugin (SigLIP head or the qwen extra).
    """
    from .cli import _collect_files
    from .audit import audit_image

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    thresholds = {}
    if overexposed_max is not None:
        thresholds["overexposed_max"] = overexposed_max
    if underexposed_max is not None:
        thresholds["underexposed_max"] = underexposed_max
    if blur_min is not None:
        thresholds["blur_min"] = blur_min
    verifier = None
    if aesthetic is not None:
        from .plugin import find_provider
        verifier = find_provider("verify")
    results = [audit_image(p, aesthetic=aesthetic, verifier=verifier,
                           **thresholds) for p in files]
    return {"ok": True, "count": len(results),
            "passed": sum(1 for r in results if r.get("passed")),
            "results": results}


@_versioned
def preview_tool(path: str, max_dim: int = 1024,
                 include_histogram: bool = True) -> dict:
    """Visual snapshot: downscaled JPEG + histogram PNG (base64).

    Gives multimodal agents actual pixels to look at - complement the
    numeric stats from ``analyze``.
    """
    from .metrics import snapshot_image

    return snapshot_image(path, max_dim=max(64, min(4096, int(max_dim))),
                          include_histogram=include_histogram)


# ── Async batch jobs (v1.9.0: directory-level process + poll/cancel) ────────
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


_MAX_JOBS = 50  # terminal records kept; oldest finished pruned first


def _prune_jobs() -> None:
    """Drop terminal job records beyond the cap (call with the lock held)."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    terminal = sorted(
        ((k, v) for k, v in _JOBS.items() if v.get("phase") in
         ("done", "error", "cancelled")),
        key=lambda kv: kv[1].get("finished_at", 0))
    for k, _v in terminal[: len(_JOBS) - _MAX_JOBS]:
        _JOBS.pop(k, None)


def _job_worker(job_id: str, files: list, options, audit: bool = False,
                  aesthetic=None):
    from .engine import batch_process
    state = _JOBS[job_id]

    def cb(idx, total, path, status=""):
        with _JOBS_LOCK:
            state.update({"done": idx, "total": total,
                          "current": os.path.basename(path) if path else "",
                          "phase": status or "processing"})

    def cancel_checker():
        with _JOBS_LOCK:
            return bool(state.get("cancelled"))

    try:
        result = batch_process(files, options, progress_callback=cb,
                               cancel_checker=cancel_checker)
    except Exception as e:  # noqa: BLE001 — a crashed worker used to leave
        # the job stuck in "processing" forever with no result at all
        with _JOBS_LOCK:
            state["phase"] = "error"
            state["error"] = f"{type(e).__name__}: {e}"
            state["finished_at"] = time.time()
            _prune_jobs()
        return
    rows = [r.to_dict() for r in result.results]
    audit_summary = None
    if audit and not cancel_checker():
        # v2.3: the quality gate rides inside the job — the agent's stop
        # condition without a separate audit round-trip
        from .audit import audit_image
        verifier = None
        if aesthetic is not None:
            from .plugin import find_provider
            verifier = find_provider("verify")
        audited = passed = 0
        try:
            for row in rows:
                out = row.get("output") or ""
                if row.get("status") != "ok" or not out \
                        or not os.path.exists(out):
                    continue
                try:
                    a = audit_image(out, aesthetic=aesthetic,
                                    verifier=verifier)
                except RuntimeError:
                    # aesthetic gate requested but plugin missing — fail the
                    # job loudly instead of silently passing the stop
                    # condition (an error job, not a hung one)
                    raise
                except Exception:
                    continue
                row["audit"] = {"passed": bool(a.get("passed")),
                                "reason": a.get("reason", "")}
                audited += 1
                passed += 1 if a.get("passed") else 0
        except RuntimeError as e:
            with _JOBS_LOCK:
                state["phase"] = "error"
                state["error"] = str(e)
                state["finished_at"] = time.time()
                _prune_jobs()
            return
        audit_summary = {
            "audited": audited, "passed": passed,
            "failed": audited - passed,
            "pass_rate": round(passed / audited, 3) if audited else None,
        }
    with _JOBS_LOCK:
        # read the flag directly — cancel_checker() re-acquires THIS lock
        # (threading.Lock is not reentrant) and self-deadlocked the worker,
        # hanging every batch_status poll forever (found by test_v23_loop)
        cancelled = bool(state.get("cancelled"))
        state["phase"] = "cancelled" if cancelled else "done"
        state["results"] = rows
        state["audit_summary"] = audit_summary
        state["fail_count"] = result.fail_count
        state["finished_at"] = time.time()
        _prune_jobs()


@_versioned
def batch_start_tool(paths: list, options: dict, recursive: bool = False,
                     jobs: int = 4, audit: bool = False,
                     aesthetic: Optional[float] = None) -> dict:
    """Start an async directory-level batch job. Returns ``job_id`` to poll
    with ``batch_status`` / cancel with ``batch_cancel``. ``options`` uses the
    same keys as ``process``; options apply to every file (masks/point_color/
    lens_* included - shared specs work across a whole batch).
    ``audit=True`` (v2.3) audits the outputs after processing and attaches
    per-file ``{passed, reason}`` + an overall pass rate to the finished
    job — the grading loop's stop condition inside the task itself.
    ``aesthetic`` (1-10, v2.4) adds the model-based aesthetic gate to that
    audit (needs the auto-tone plugin).
    """
    from .cli import _collect_files
    from .engine import ProcessOptions

    files = _collect_files(list(paths), recursive=recursive)
    if not files:
        return {"ok": False, "error": "no supported image files found",
                "paths": list(paths)}
    try:
        # shared validator: type coercion, numeric ranges, traversal-safe
        # prefix/suffix — the old bare ProcessOptions(**options) accepted
        # arbitrary unvalidated values
        from .server import _options_from_dict
        opts = _options_from_dict(dict(options or {}),
                                  base=_base_options)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"invalid options: {e}"}
    opts.jobs = max(1, min(64, int(jobs)))
    job_id = secrets.token_urlsafe(12)  # 96-bit, same width as /tasks ids
    with _JOBS_LOCK:
        _JOBS[job_id] = {"job_id": job_id, "phase": "starting",
                         "total": len(files), "done": 0, "current": "",
                         "cancelled": False, "results": None, "fail_count": 0}
    threading.Thread(target=_job_worker,
                     args=(job_id, files, opts, audit, aesthetic),
                     daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": len(files)}


@_versioned
def batch_status_tool(job_id: str) -> dict:
    """Poll a batch job: phase (starting/processing/done/cancelled),
    progress (done/total) and full per-file results when finished.
    """
    with _JOBS_LOCK:
        state = _JOBS.get(job_id)
    if state is None:
        return {"ok": False, "error": f"unknown job {job_id}"}
    out = {k: state[k] for k in ("job_id", "phase", "total", "done",
                                 "fail_count", "current")}
    if state.get("error"):
        out["error"] = state["error"]
    out["ok"] = True
    if state.get("results") is not None:
        out["results"] = state["results"]
    if state.get("audit_summary") is not None:
        out["audit_summary"] = state["audit_summary"]
    return out


@_versioned
def batch_cancel_tool(job_id: str) -> dict:
    """Cancel a running batch job (in-flight images finish, pending are
    skipped as cancelled)."""
    with _JOBS_LOCK:
        state = _JOBS.get(job_id)
        if state is not None:
            state["cancelled"] = True
    if state is None:
        return {"ok": False, "error": f"unknown job {job_id}"}
    return {"ok": True, "job_id": job_id, "cancelled": True}


# ── Server assembly ─────────────────────────────────────────────────────────


def create_server(config_path: Optional[str] = None):
    """Build a fresh FastMCP server with all tools registered.

    ``config_path`` (from ``photo-s mcp --config``) supplies ProcessOptions
    defaults merged under tool arguments (explicit tool args win).
    Testable without stdio via ``await server.call_tool(...)``.
    """
    global _base_options
    _base_options = ProcessOptions()
    if config_path:
        from .config import load_config, apply_config
        _base_options = apply_config(load_config(config_path), _base_options)

    mcp = _mcp()
    mcp.add_tool(process_tool, name="process",
                 description="Batch process images: quality/format/resize/"
                             "tone/exposure/denoise. Accepts files, "
                             "directories or globs. Returns BatchResult JSON "
                             "with per-file status.")
    mcp.add_tool(info_tool, name="info",
                 description="Environment probe: version, supported formats, "
                             "optional-feature and plugin status.")
    mcp.add_tool(exif_tool, name="exif",
                 description="Read/filter or write EXIF metadata "
                             "(rating/keywords/camera/title/...). "
                             "action='show' filters files; action='write' "
                             "takes a per-path tag map.")
    mcp.add_tool(dedup_tool, name="dedup",
                 description="Find duplicate images by perceptual hash. "
                             "action='report' lists groups; "
                             "action='keep-sharpest' keeps the sharpest of a "
                             "burst (deletion requires dry_run=False).")
    mcp.add_tool(cull_tool, name="cull",
                 description="Filter images by exposure and sharpness "
                             "thresholds; returns per-file stats and a kept "
                             "list.")
    mcp.add_tool(select_tool, name="select",
                 description="Sort rated photos into selects/rejects folders "
                             "(rating >= keep_min → selects_dir, <= reject_max "
                             "→ rejects_dir, else in place). dry_run reports "
                             "would-moves without writing.")
    mcp.add_tool(hdr_tool, name="hdr",
                 description="Merge bracketed exposures into one HDR image "
                             "(exposure fusion). align=True runs AlignMTB for "
                             "handheld brackets.")
    mcp.add_tool(blurfaces_tool, name="blurfaces",
                 description="Detect and blur/pixelate faces in a batch "
                             "(privacy). mode=blur|pixelate, margin expands the "
                             "face box; runs the batch pipeline so EXIF is kept.")
    mcp.add_tool(hash_tool, name="hash",
                 description="Generate or verify a SHA-256 checksum manifest "
                             "(any file type).")
    mcp.add_tool(plugin_tool, name="plugin",
                 description="Manage official plugins: list installed/"
                             "available, install, uninstall (shells to pip).")
    mcp.add_tool(contact_sheet_tool, name="contact_sheet",
                 description="Build a contact sheet (grid montage) from "
                             "images; returns the output path and count.")
    mcp.add_tool(gallery_tool, name="gallery",
                 description="Generate an HTML gallery from images; returns "
                             "the output directory and count.")
    mcp.add_tool(watermark_tool, name="watermark",
                 description="Overlay a text or image watermark on images "
                             "via the batch pipeline.")
    mcp.add_tool(preset_tool, name="preset",
                 description="Manage processing presets: list / save / load "
                             "/ delete. 'load' returns options JSON for "
                             "'process'.")
    mcp.add_tool(bench_tool, name="bench",
                 description="Benchmark the pipeline at several worker counts "
                             "(mirrors 'photo-s bench') to pick optimal "
                             "concurrency. Temp-dir outputs, sources untouched.")
    mcp.add_tool(watch_tool, name="watch",
                 description="Watch a directory and auto-process new images "
                             "in the background; returns immediately with an "
                             "id — poll 'watch_status' / stop via "
                             "'watch_stop'. Needs photo-s-tools[watch].")
    mcp.add_tool(watch_status_tool, name="watch_status",
                 description="Report the state of a background watch "
                             "(running, processed count, per-file results).")
    mcp.add_tool(watch_stop_tool, name="watch_stop",
                 description="Stop a background watch; results so far stay "
                             "visible via 'watch_status'.")
    mcp.add_tool(autopilot_start_tool, name="autopilot_start",
                 description="Unattended pipeline on a directory: watch -> "
                             "suggest/auto-tone -> process -> audit -> route "
                             "to passed/ or review/ (JSONL trace). Returns an "
                             "id — poll 'autopilot_status' / stop via "
                             "'autopilot_stop'.")
    mcp.add_tool(autopilot_status_tool, name="autopilot_status",
                 description="State of a background autopilot: passed/review/"
                             "error counters + per-file records.")
    mcp.add_tool(autopilot_stop_tool, name="autopilot_stop",
                 description="Stop a background autopilot; records so far stay "
                             "visible via 'autopilot_status'.")
    mcp.add_tool(index_tool, name="index",
                 description="Build a semantic embedding index for images "
                             "(SigLIP with the auto-tone plugin, built-in "
                             "histogram without); optional auto-tagging.")
    mcp.add_tool(find_tool, name="find",
                 description="Semantic search over an 'index'-built index: "
                             "text query or search-by-image, cosine-ranked.")
    mcp.add_tool(batch_start_tool, name="batch_start",
                 description=batch_start_tool.__doc__)
    mcp.add_tool(batch_status_tool, name="batch_status",
                 description=batch_status_tool.__doc__)
    mcp.add_tool(batch_cancel_tool, name="batch_cancel",
                 description=batch_cancel_tool.__doc__)
    mcp.add_tool(diff_tool, name="diff",
                 description=diff_tool.__doc__)
    mcp.add_tool(audit_tool, name="audit",
                 description=audit_tool.__doc__)
    mcp.add_tool(preview_tool, name="preview",
                 description=preview_tool.__doc__)
    mcp.add_tool(analyze_tool, name="analyze",
                 description="Perceptual analysis of images: per-channel + "
                             "luma histograms, channel stats, contrast, "
                             "saturation, white-balance lean, exposure and "
                             "blur score. The feedback half of the "
                             "'analyze -> adjust params -> process -> "
                             "analyze' grading loop.")
    mcp.add_tool(suggest_tool, name="suggest",
                 description="Rule-based parameter suggestions: analyze "
                             "stats → conservative ProcessOptions fields "
                             "(ev/wb_temp/wb_tint/contrast/vibrance/clarity/"
                             "highlight_recovery/levels), each with a reason "
                             "and the metric behind it. Zero models, offline. "
                             "The bridge between 'analyze' and 'process' in "
                             "the grading loop; the auto-tone plugin is the "
                             "personal-style layer on top.")
    _register_plugin_tools(mcp)
    return mcp


def _register_plugin_tools(mcp) -> int:
    """Let installed plugins surface their own MCP tools (v2.3 wiring).

    A plugin defining ``register_mcp_tools(mcp)`` (photo_s.hooks.PhotoSPlugin
    hook) is called with the live FastMCP instance — e.g. auto-tone adds
    auto_tone / aesthetic_score / tone_advisor / batch_auto_tone /
    auto_tone_with_style / analyze_visual_style. A broken
    plugin must not take the server down: failures are logged and skipped.
    """
    registered = 0
    try:
        from .plugin import discover_plugins
        from .hooks import PhotoSPlugin
        plugins = discover_plugins()
    except Exception as e:
        print("plugin discovery failed: {}".format(e), file=sys.stderr)
        return 0
    for plugin in plugins:
        fn = getattr(plugin, "register_mcp_tools", None)
        # only plugins that actually override the hook (the base class
        # default is a no-op — counting it would report phantom wiring)
        if not callable(fn) or \
                getattr(type(plugin), "register_mcp_tools", None) is \
                PhotoSPlugin.register_mcp_tools:
            continue
        try:
            fn(mcp)
            registered += 1
        except Exception as e:
            print("plugin '{}' MCP registration failed: {}".format(
                getattr(plugin, "name", "?"), e), file=sys.stderr)
    if registered:
        print("plugin MCP tools registered by {} plugin(s)".format(
            registered), file=sys.stderr)
    return registered


def run_stdio(config_path: Optional[str] = None) -> None:
    """Blocking stdio entry point for ``photo-s mcp``."""
    asyncio.run(create_server(config_path=config_path).run_stdio_async())


def list_tools_json() -> List[dict]:
    """Tool names + JSON input schemas (for ``photo-s mcp --list-tools``)."""
    tools = asyncio.run(create_server().list_tools())
    return [{
        "name": t.name,
        "description": t.description,
        "input_schema": t.model_dump(by_alias=True)["inputSchema"],
    } for t in tools]


def _call_tool_json(name: str, args: dict) -> dict:
    """Call a tool in-process and normalize the result to a dict.

    ``call_tool`` returns ``list[TextContent]`` (or a raw dict on newer
    versions) — this helper handles both. Used by tests.
    """
    result = asyncio.run(create_server().call_tool(name, args))
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        return json.loads(result[0].text)
    return result
