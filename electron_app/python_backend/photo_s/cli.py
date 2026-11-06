"""
PhotoS - Command Line Interface

Batch compress and convert images from the terminal.

Examples:
    # Compress all JPEGs in current directory to quality 80
    photo-s compress *.jpg -q 80

    # Convert PNGs to WebP format
    photo-s convert *.png -f webp

    # Batch process a directory recursively
    photo-s batch ~/Pictures/ -r -f JPEG -q 70 --resize 1920x1080

    # Target size mode: auto-tune quality to fit within 500KB
    photo-s compress *.jpg --target-size 500KB

    # Target size + quality ceiling: max quality 90, fit within 2MB
    photo-s batch ~/Pictures/ -r --target-size 2MB -q 90

    # Preview what would happen without actually processing
    photo-s batch ~/Pictures/ --dry-run
"""

import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .engine import (
    ProcessOptions,
    ProcessResult,
    BatchResult,
    batch_process,
    auto_jobs,
    scan_directory,
    format_size,
    apply_exif_tags,
    _resolve_folder_pattern,
    _canonical_format,
    SUPPORTED_FORMATS,
    ALL_INPUT_EXTENSIONS,
    PIL_WRITABLE,
)
from . import __version__
from .i18n import _t  # reads i18n.CURRENT_LANG at call time; set in run_cli
from .contract import versioned  # additive schema_version on every --json output


def _no_files_exit(parsed, msg_key: str = "msg_no_images") -> int:
    """'No input files found' exit path (always exit 1).

    --json mode must keep stdout a single JSON document (schema_version
    contract) — the old human-readable ❌ line on stdout broke every agent
    consumer. Human text stays on stdout for interactive use.
    """
    if getattr(parsed, 'json', False):
        import json
        print(json.dumps(versioned({"ok": False, "error": _t(msg_key),
                                    "files": []}),
                         indent=2, ensure_ascii=False))
    else:
        print(_t(msg_key))
    return 1


def _collect_files(patterns: List[str], recursive: bool = False) -> List[str]:
    """Collect image files from glob patterns and/or directories."""
    all_files = set()

    for pattern in patterns:
        # Check if pattern is a directory
        p = Path(pattern)
        if p.is_dir():
            all_files.update(scan_directory(pattern, recursive=recursive))
        else:
            # Treat as glob pattern
            matches = glob.glob(pattern, recursive=recursive)
            for match in matches:
                mp = Path(match)
                if mp.is_file() and mp.suffix.lower() in ALL_INPUT_EXTENSIONS:
                    all_files.add(str(mp.absolute()))
                elif mp.is_dir():
                    all_files.update(scan_directory(str(mp), recursive=recursive))

    return sorted(all_files)


def _parse_dimensions(dim_str: str) -> tuple:
    """Parse dimension string like '1920x1080' or '1920' or 'x1080'."""
    dim_str = dim_str.strip().lower()
    if 'x' in dim_str:
        parts = dim_str.split('x')
        w = int(parts[0]) if parts[0] else None
        h = int(parts[1]) if parts[1] else None
        return w, h
    else:
        return int(dim_str), None


def _parse_size(size_str: str) -> int:
    """Parse human-readable size string to bytes.

    Examples: '500' → 500, '500KB' → 512000, '2MB' → 2097152, '1.5MB' → 1572864
    """
    original = str(size_str)
    size_str = size_str.strip().upper()
    if not size_str:
        return 0

    # Check for suffix
    multiplier = 1
    for suffix, mult in [("KB", 1024), ("MB", 1024**2), ("GB", 1024**3),
                          ("K", 1024), ("M", 1024**2), ("G", 1024**3)]:
        if size_str.endswith(suffix):
            multiplier = mult
            size_str = size_str[:-len(suffix)]
            break

    try:
        value = float(size_str)
    except ValueError:
        # report the user's original spelling, not the .upper()'ed copy
        raise argparse.ArgumentTypeError(
            _t("val_size", val=original, supported="500, 500KB, 2MB, 1.5MB")
        )

    return int(value * multiplier)


def _target_size_arg(size_str: str) -> int:
    """argparse type for --target-size: clean error instead of a traceback."""
    return _parse_size(size_str)


def _dimensions_arg(dim_str: str) -> tuple:
    """argparse type for WxH args (--resize/--thumb): validate up front.

    '' / non-numeric / non-positive parts are rejected with an argparse
    error — the old deferred _parse_dimensions call raised a bare ValueError
    traceback from deep inside the command handler.
    """
    try:
        w, h = _parse_dimensions(dim_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid dimensions {dim_str!r} (expected WxH, Wx, xH)")
    if (w is not None and w <= 0) or (h is not None and h <= 0):
        raise argparse.ArgumentTypeError(
            f"invalid dimensions {dim_str!r} (W/H must be positive)")
    return (w, h)


def _parse_sizes(sizes_str: Optional[str]) -> Optional[List[Tuple[str, Optional[int], Optional[int]]]]:
    """Parse --sizes string like 'thumb:480x,screen:1920x1080'.

    Returns list of (label, max_w, max_h) tuples, or None.
    """
    if not sizes_str:
        return None

    result = []
    for part in sizes_str.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        label, dims = part.split(":", 1)
        label = label.strip()
        dims = dims.strip()
        w, h = _parse_dimensions(dims)
        result.append((label, w, h))

    return result if result else None


def _add_advanced_args(parser):
    """Add shared advanced options (privacy, pixel cap, mtime, evaluate, config).

    Config-capable flags use ``default=argparse.SUPPRESS`` so the config layer
    can tell "explicitly set" from "not set" via ``hasattr(parsed, attr)``
    (plain defaults would be indistinguishable when the user passes a value
    equal to the default, e.g. ``-q 85``).
    """
    parser.add_argument(
        "--strip-gps", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___strip_gps'),
    )
    parser.add_argument(
        "--keep-mtime", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___keep_mtime'),
    )
    parser.add_argument(
        "--max-pixels", type=int, default=argparse.SUPPRESS, metavar="N",
        help=_t('help___max_pixels'),
    )
    parser.add_argument(
        "--evaluate", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___evaluate'),
    )
    parser.add_argument(
        "--resume", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___resume'),
    )
    parser.add_argument(
        "--config", type=str, default=None, metavar="PATH",
        help=_t('help___config'),
    )


def _date_shift_arg(spec: str) -> str:
    """argparse type for --date-shift: validates the offset syntax."""
    from .engine import parse_date_shift
    try:
        parse_date_shift(spec)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return spec


def _format_arg(fmt: str) -> str:
    """argparse type for -f/--format: case-insensitive canonical format name.

    Accepts 'png'/'PNG'/'webp' → returns the canonical SUPPORTED_FORMATS key.
    """
    fmt = fmt.strip()
    canonical = _canonical_format(fmt)
    if canonical not in SUPPORTED_FORMATS:
        raise argparse.ArgumentTypeError(
            _t("val_format", fmt=fmt, formats=", ".join(sorted(SUPPORTED_FORMATS)))
        )
    return canonical


def _add_transform_args(parser):
    """Add shared transform options (tone, composition, metadata, quality).

    Config-capable args use default=argparse.SUPPRESS (see _add_advanced_args).
    """
    parser.add_argument(
        "--brightness", type=float, default=argparse.SUPPRESS,
        metavar="0-2", help=_t('help___brightness'),
    )
    parser.add_argument(
        "--contrast", type=float, default=argparse.SUPPRESS,
        metavar="0-2", help=_t('help___contrast'),
    )
    parser.add_argument(
        "--saturation", type=float, default=argparse.SUPPRESS,
        metavar="0-2", help=_t('help___saturation'),
    )
    parser.add_argument(
        "--gamma", type=float, default=argparse.SUPPRESS,
        metavar="0.1-3", help=_t('help___gamma'),
    )
    parser.add_argument(
        "--sharpen", type=float, default=argparse.SUPPRESS,
        metavar="0-3", help=_t('help___sharpen'),
    )
    parser.add_argument(
        "--export-sharpen", type=float, default=argparse.SUPPRESS,
        metavar="0-2", help=_t('help___export_sharpen'),
    )
    parser.add_argument(
        "--grayscale", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___grayscale'),
    )
    parser.add_argument(
        "--sepia", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___sepia'),
    )
    parser.add_argument(
        "--auto-levels", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___auto_levels'),
    )
    parser.add_argument(
        "--highlight-recovery", type=float, default=argparse.SUPPRESS,
        metavar="0-1", help=_t('help___highlight_recovery'),
    )
    parser.add_argument(
        "--wb", type=float, default=argparse.SUPPRESS, metavar="KELVIN",
        help=_t('help___wb'),
    )
    parser.add_argument(
        "--wb-from", type=str, default=argparse.SUPPRESS, metavar="REF.jpg",
        help=_t('help___wb_from'),
    )
    parser.add_argument(
        "--ev", type=float, default=argparse.SUPPRESS, metavar="STOPS",
        help=_t('help___ev'),
    )
    parser.add_argument(
        "--auto-exposure", type=float, default=argparse.SUPPRESS, metavar="0-1",
        help=_t('help___auto_exposure'),
    )
    # Lightroom-direction grading (v1.6.0)
    parser.add_argument(
        "--wb-tint", type=float, default=argparse.SUPPRESS, metavar="-100-100",
        help=_t('help___wb_tint'),
    )
    parser.add_argument(
        "--levels", type=str, default=argparse.SUPPRESS,
        metavar="BLACK,WHITE[,GAMMA]",
        help=_t('help___levels'),
    )
    parser.add_argument(
        "--curves", type=str, default=argparse.SUPPRESS,
        metavar="CH:X,Y;X,Y[|CH:...]",
        help=_t('help___curves'),
    )
    parser.add_argument(
        "--vibrance", type=float, default=argparse.SUPPRESS, metavar="-1-1",
        help=_t('help___vibrance'),
    )
    parser.add_argument(
        "--color-grading", type=str, default=argparse.SUPPRESS,
        metavar="ZONE:HUE,SAT;...",
        help=_t('help___color_grading'),
    )
    parser.add_argument(
        "--hsl", type=str, default=argparse.SUPPRESS,
        metavar="COLOR:H,S,L;...",
        help=_t('help___hsl'),
    )
    parser.add_argument(
        "--point-color", type=str, default=argparse.SUPPRESS,
        metavar="R,G,B:H,S,L[,RANGE];...",
        help=_t('help___point_color'),
    )
    parser.add_argument(
        "--clarity", type=float, default=argparse.SUPPRESS, metavar="-1-1",
        help=_t('help___clarity'),
    )
    parser.add_argument(
        "--texture", type=float, default=argparse.SUPPRESS, metavar="-1-1",
        help=_t('help___texture'),
    )
    parser.add_argument(
        "--dehaze", type=float, default=argparse.SUPPRESS, metavar="-1-1",
        help=_t('help___dehaze'),
    )
    parser.add_argument(
        "--vignette", type=str, default=argparse.SUPPRESS,
        metavar="AMOUNT[,MID[,FEATHER]]",
        help=_t('help___vignette'),
    )
    parser.add_argument(
        "--grain", type=str, default=argparse.SUPPRESS,
        metavar="AMOUNT[,SIZE]",
        help=_t('help___grain'),
    )
    parser.add_argument(
        "--masks", type=str, default=argparse.SUPPRESS,
        metavar="NAME:TYPE:PARAMS;...",
        help=_t('help___masks'),
    )
    parser.add_argument(
        "--mask-adjust", type=str, default=argparse.SUPPRESS,
        metavar="NAME:KEY=VAL,...;...",
        help=_t('help___mask_adjust'),
    )
    parser.add_argument(
        "--lens-distort", type=float, default=argparse.SUPPRESS,
        metavar="K1",
        help=_t('help___lens_distort'),
    )
    parser.add_argument(
        "--lens-vignette", type=str, default=argparse.SUPPRESS,
        metavar="AMOUNT[,MIDPOINT]",
        help=_t('help___lens_vignette'),
    )
    parser.add_argument(
        "--lens-ca", type=str, default=argparse.SUPPRESS,
        metavar="R_SCALE,B_SCALE",
        help=_t('help___lens_ca'),
    )
    parser.add_argument(
        "--lens-profile", type=str, default=argparse.SUPPRESS,
        metavar="NAME",
        help=_t('help___lens_profile'),
    )
    parser.add_argument(
        "--log-curve", type=str.upper, default=argparse.SUPPRESS, metavar="NAME",
        choices=["SLOG3", "CLOG3", "LOGC3", "DLOG", "VLOG", "HLG"],
        help=_t('help___log_curve'),
    )
    parser.add_argument(
        "--denoise", type=float, default=argparse.SUPPRESS, metavar="N",
        help=_t('help___denoise'),
    )
    parser.add_argument(
        "--auto-tone", type=float, default=argparse.SUPPRESS,
        metavar="0-1", help=_t('help___auto_tone'),
    )
    parser.add_argument(
        "--lut", type=str, default=argparse.SUPPRESS, metavar="FILE|PRESET",
        help=_t('help___lut'),
    )
    parser.add_argument(
        "--auto-straighten", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___auto_straighten'),
    )
    parser.add_argument(
        "--max-straighten-angle", type=float, default=argparse.SUPPRESS,
        metavar="DEG",
        help=_t('help___max_straighten_angle'),
    )
    parser.add_argument(
        "--print-size", type=str, default=argparse.SUPPRESS, metavar="WxH@DPI",
        help=_t('help___print_size'),
    )
    parser.add_argument(
        "--crop", type=str, default=argparse.SUPPRESS, metavar="WxH+X+Y",
        help=_t('help___crop'),
    )
    parser.add_argument(
        "--crop-ratio", type=str, default=argparse.SUPPRESS, metavar="16:9",
        help=_t('help___crop_ratio'),
    )
    parser.add_argument(
        "--rotate", type=float, default=argparse.SUPPRESS, metavar="DEG",
        help=_t('help___rotate'),
    )
    parser.add_argument(
        "--rotate-bg", type=str, default=argparse.SUPPRESS, metavar="#RRGGBB",
        help=_t('help___rotate_bg'),
    )
    parser.add_argument(
        "--flip", type=str, default=argparse.SUPPRESS, choices=["h", "v"],
        help=_t('help___flip'),
    )
    parser.add_argument(
        "--pad", type=str, default=argparse.SUPPRESS, metavar="16:9",
        help=_t('help___pad'),
    )
    parser.add_argument(
        "--pad-bg", type=str, default=argparse.SUPPRESS, metavar="#RRGGBB",
        help=_t('help___pad_bg'),
    )
    parser.add_argument(
        "--date-shift", type=_date_shift_arg, default=argparse.SUPPRESS,
        metavar="OFFSET",
        help=_t('help___date_shift'),
    )
    parser.add_argument(
        "--scrub", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___scrub'),
    )
    parser.add_argument(
        "--sync-date", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___sync_date'),
    )
    parser.add_argument(
        "--srgb", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___srgb'),
    )
    parser.add_argument(
        "--flatten-cmyk", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___flatten_cmyk'),
    )
    parser.add_argument(
        "--jpeg-subsampling", type=str, default=argparse.SUPPRESS,
        choices=["444", "422", "420"], metavar="444|422|420",
        help=_t('help___jpeg_subsampling'),
    )
    parser.add_argument(
        "--blur-score", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___blur_score'),
    )
    parser.add_argument(
        "--report", type=str, default=argparse.SUPPRESS, metavar="OUT.csv",
        help=_t('help___report'),
    )
    parser.add_argument(
        "--gpx-trace", type=str, default=argparse.SUPPRESS, metavar="TRACK.gpx",
        help=_t('help___gpx_trace'),
    )
    parser.add_argument(
        "--preset", type=str, default=argparse.SUPPRESS, metavar="NAME",
        help=_t('help___preset'),
    )
    parser.add_argument(
        "--blur-faces", type=str, default=argparse.SUPPRESS,
        choices=["blur", "pixelate"], metavar="blur|pixelate",
        help=_t('help___blur_faces'),
    )
    parser.add_argument(
        "--blur-faces-margin", type=int, default=argparse.SUPPRESS,
        metavar="PCT", help=_t('help___blur_faces_margin'),
    )
    parser.add_argument(
        "--cutout", type=str, default=argparse.SUPPRESS, metavar="SPEC",
        help=_t('help___cutout'),
    )


def _add_raw_args(parser):
    """Add shared RAW-decode options (shared by batch/compress/convert).

    Config-capable args use default=argparse.SUPPRESS (see _add_advanced_args).
    """
    parser.add_argument(
        "--raw-half-size", action="store_true",
        help=_t('help___raw_half_size'),
    )
    parser.add_argument(
        "--raw-no-auto-bright", action="store_true",
        help=_t('help___raw_no_auto_bright'),
    )
    parser.add_argument(
        "--raw-demosaic", type=str, default=argparse.SUPPRESS,
        choices=["auto", "ahd", "vng", "ppg", "dcb", "dht", "amaze"],
        metavar="auto|ahd|vng|ppg|dcb|dht|amaze",
        help=_t('help___raw_demosaic'),
    )
    parser.add_argument(
        "--raw-color-space", type=str, default=argparse.SUPPRESS,
        choices=["sRGB", "AdobeRGB", "ProPhotoRGB"],
        metavar="sRGB|AdobeRGB|ProPhotoRGB",
        help=_t('help___raw_color_space'),
    )
    parser.add_argument(
        "--raw-16bit", action="store_true",
        help=_t('help___raw_16bit'),
    )


# Config keys the CLI applies outside the generic field mapping
# (inverse booleans; handled below). Everything else in
# config._SIMPLE_FIELDS is applied by the generic loop in
# _apply_config_defaults.
_SEPARATE_CONFIG_KEYS = {"auto_rotate", "optimize"}

# Config key → CLI dest name when they differ (most are identical).
# This is the CLI half of the config mapping — the key set must stay in
# sync with config._SIMPLE_FIELDS (guarded by a parity test in
# tests/test_config.py).
_CONFIG_CLI_DESTS = {
    "output_format": "format",
    "watermark_position": "watermark_pos",
    "rotate_degrees": "rotate",
    "scale_percent": "scale",
    "max_width": "resize",
    "max_height": "resize",
    "rename_pattern": "rename",
}


def _apply_config_defaults(options: ProcessOptions, parsed, cfg: dict) -> ProcessOptions:
    """Apply config-file defaults for options the user did not set on the CLI.

    Precedence: explicit CLI > config file > built-in defaults.
    A CLI option counts as "set" when it appears in the parsed namespace —
    config-capable argparse args use ``default=argparse.SUPPRESS`` so absent
    attributes mean "not passed" (an equals-the-default value like ``-q 85``
    is still a real value and wins over the config file).
    """
    from .config import _SIMPLE_FIELDS, _coerce_value
    opts = cfg.get("options", {}) if isinstance(cfg, dict) else {}

    # Generic mapping: config key → ProcessOptions field (config._SIMPLE_FIELDS
    # is the single source of truth for the key→field direction). A config key
    # is applied only when its CLI counterpart was not explicitly passed —
    # checked via the CLI dest name (_CONFIG_CLI_DESTS handles renames).
    for config_key, field in _SIMPLE_FIELDS.items():
        if config_key in _SEPARATE_CONFIG_KEYS:
            continue
        if config_key not in opts or opts[config_key] is None:
            continue
        cli_dest = _CONFIG_CLI_DESTS.get(config_key, config_key)
        if not hasattr(parsed, cli_dest):
            setattr(options, field, _coerce_value(config_key, field,
                                                  opts[config_key]))

    # inverse boolean flags (CLI uses --no-*, config uses positive form).
    # Values go through _coerce_value so a quoted TOML string like
    # preserve_exif = "false" resolves the SAME way here as it does in
    # `config show` / `serve` (bool("false") used to be True — the two
    # code paths disagreed about the same file).
    for cfg_key, cli_flag, field in (
            ("preserve_exif", "no_exif", "preserve_exif"),
            ("auto_rotate", "no_auto_rotate", "auto_rotate"),
            ("optimize", "no_optimize", "optimize"),
            ("raw_auto_bright", "raw_no_auto_bright", "raw_auto_bright")):
        if cfg_key in opts and opts[cfg_key] is not None \
                and not getattr(parsed, cli_flag, False):
            try:
                setattr(options, field,
                        _coerce_value(cfg_key, field, opts[cfg_key]))
            except ValueError as e:
                print(f"⚠️  {e}", file=sys.stderr)

    if "target_size" in opts and not getattr(parsed, "target_size", None):
        from .config import _parse_size as _cfg_parse_size
        size = _cfg_parse_size(opts["target_size"])
        if size is not None:
            options.target_size_bytes = size

    if "folder_pattern" in opts and not getattr(parsed, "organize", None):
        options.folder_pattern = _resolve_folder_pattern(str(opts["folder_pattern"]))

    # case-insensitive format from config ("png" / "WEBP" → "PNG" / "WebP")
    options.output_format = _canonical_format(options.output_format)

    return options


# ProcessOptions field → CLI dest name when they differ, for the --preset
# merge (extends _CONFIG_CLI_DESTS with renames only preset application needs).
_PRESET_CLI_DESTS = {
    **_CONFIG_CLI_DESTS,
    "pad_ratio": "pad",
    "output_sizes": "sizes",
    "lut_file": "lut",
    "wb_temp": "wb",
    "folder_pattern": "organize",
}


def _build_process_options(parsed) -> ProcessOptions:
    """Build a ProcessOptions from parsed CLI args.

    Shared by the batch/compress/convert shared handler and ``preset save``
    (which captures the full option set as a named preset).
    """
    # Parse target size if provided (the argparse type already returns
    # bytes for CLI flags; plain strings here come from direct callers/tests)
    target_size_val = getattr(parsed, 'target_size', None)
    if isinstance(target_size_val, int):
        target_size_bytes = target_size_val
    elif target_size_val:
        target_size_bytes = _parse_size(target_size_val)
    else:
        target_size_bytes = None

    # auto-jobs: explicit -j / config wins, else smart default (CPU count)
    _jobs = getattr(parsed, 'jobs', None)
    if _jobs is None:
        _jobs = auto_jobs()

    options = ProcessOptions(
        quality=getattr(parsed, 'quality', 85),
        output_format=getattr(parsed, 'format', 'JPEG'),
        output_dir=getattr(parsed, 'output_dir', None),
        preserve_exif=not getattr(parsed, 'no_exif', False),
        optimize=not getattr(parsed, 'no_optimize', False),
        progressive=getattr(parsed, 'progressive', False),
        jpeg_subsampling=getattr(parsed, 'jpeg_subsampling', '420'),
        overwrite=getattr(parsed, 'overwrite', False),
        prefix=getattr(parsed, 'prefix', ''),
        suffix=getattr(parsed, 'suffix', None),  # resolved per-command below
        target_size_bytes=target_size_bytes,
        raw_half_size=getattr(parsed, 'raw_half_size', False),
        raw_auto_bright=not getattr(parsed, 'raw_no_auto_bright', False),
        raw_demosaic=getattr(parsed, 'raw_demosaic', 'auto'),
        raw_color_space=getattr(parsed, 'raw_color_space', 'sRGB'),
        raw_16bit=getattr(parsed, 'raw_16bit', False),
        auto_rotate=not getattr(parsed, 'no_auto_rotate', False),
        remove_original=getattr(parsed, 'remove_original', False),
        rename_pattern=getattr(parsed, 'rename', None) or "",
        folder_pattern=_resolve_folder_pattern(getattr(parsed, 'organize', None) or ""),
        watermark_text=getattr(parsed, 'watermark_text', None) or "",
        watermark_image=getattr(parsed, 'watermark_image', None) or "",
        watermark_position=getattr(parsed, 'watermark_pos', None) or "BOTTOM_RIGHT",
        watermark_opacity=getattr(parsed, 'watermark_opacity', 50),
        output_sizes=_parse_sizes(getattr(parsed, 'sizes', None)),
        strip_gps=getattr(parsed, 'strip_gps', False),
        keep_mtime=getattr(parsed, 'keep_mtime', False),
        max_pixels=getattr(parsed, 'max_pixels', None),
        evaluate=getattr(parsed, 'evaluate', False),
        brightness=getattr(parsed, 'brightness', 1.0),
        contrast=getattr(parsed, 'contrast', 1.0),
        saturation=getattr(parsed, 'saturation', 1.0),
        gamma=getattr(parsed, 'gamma', 1.0),
        sharpen=getattr(parsed, 'sharpen', 1.0),
        export_sharpen=getattr(parsed, 'export_sharpen', None),
        grayscale=getattr(parsed, 'grayscale', False),
        sepia=getattr(parsed, 'sepia', False),
        auto_levels=getattr(parsed, 'auto_levels', False),
        highlight_recovery=getattr(parsed, 'highlight_recovery', None),
        wb_temp=getattr(parsed, 'wb', None),
        wb_reference=getattr(parsed, 'wb_from', None),
        ev=getattr(parsed, 'ev', None),
        auto_exposure=getattr(parsed, 'auto_exposure', None),
        log_curve=getattr(parsed, 'log_curve', None),
        denoise=getattr(parsed, 'denoise', None),
        auto_tone=getattr(parsed, 'auto_tone', None),
        lut_file=getattr(parsed, 'lut', None),
        auto_straighten=getattr(parsed, 'auto_straighten', False),
        max_straighten_angle=getattr(parsed, 'max_straighten_angle', 10.0),
        print_size=getattr(parsed, 'print_size', None),
        crop=getattr(parsed, 'crop', None),
        crop_ratio=getattr(parsed, 'crop_ratio', None),
        rotate_degrees=float(getattr(parsed, 'rotate', 0) or 0),
        rotate_bg=getattr(parsed, 'rotate_bg', None),
        flip=getattr(parsed, 'flip', None),
        pad_ratio=getattr(parsed, 'pad', None),
        pad_bg=getattr(parsed, 'pad_bg', None) or "#000000",
        date_shift=getattr(parsed, 'date_shift', None),
        scrub=getattr(parsed, 'scrub', False),
        sync_date=getattr(parsed, 'sync_date', False),
        blur_score=getattr(parsed, 'blur_score', False),
        srgb=getattr(parsed, 'srgb', False),
        flatten_cmyk=getattr(parsed, 'flatten_cmyk', False),
        resume=getattr(parsed, 'resume', False),
        gpx_trace=getattr(parsed, 'gpx_trace', None),
        blur_faces=getattr(parsed, 'blur_faces', None),
        blur_faces_margin=getattr(parsed, 'blur_faces_margin', None),
        cutout=getattr(parsed, 'cutout', None),
        # Lightroom-direction grading (v1.6.0)
        wb_tint=getattr(parsed, 'wb_tint', 0.0),
        levels=getattr(parsed, 'levels', ""),
        curves=getattr(parsed, 'curves', ""),
        vibrance=getattr(parsed, 'vibrance', 0.0),
        color_grading=getattr(parsed, 'color_grading', ""),
        hsl=getattr(parsed, 'hsl', ""),
        point_color=getattr(parsed, 'point_color', ""),
        clarity=getattr(parsed, 'clarity', 0.0),
        texture=getattr(parsed, 'texture', 0.0),
        dehaze=getattr(parsed, 'dehaze', 0.0),
        vignette=getattr(parsed, 'vignette', ""),
        grain=getattr(parsed, 'grain', ""),
        # Local adjustments under masks (v1.7.0)
        masks=getattr(parsed, 'masks', ""),
        mask_adjust=getattr(parsed, 'mask_adjust', ""),
        # Lens correction (v1.7.0)
        lens_distort=getattr(parsed, 'lens_distort', 0.0),
        lens_vignette=getattr(parsed, 'lens_vignette', ""),
        lens_ca=getattr(parsed, 'lens_ca', ""),
        lens_profile=getattr(parsed, 'lens_profile', None),
        jobs=_jobs,
    )

    # Handle --resize (the argparse type already returns a validated
    # (w, h) tuple; plain strings here come from direct callers/tests)
    resize_val = getattr(parsed, 'resize', None)
    if isinstance(resize_val, tuple):
        options.max_width, options.max_height = resize_val
    elif resize_val:
        w, h = _parse_dimensions(resize_val)
        options.max_width = w
        options.max_height = h

    # Handle --scale
    options.scale_percent = getattr(parsed, 'scale', None)

    # Fix suffix for 'compress' command
    if parsed.command == "compress":
        if not getattr(parsed, 'suffix', None) or parsed.suffix == '_processed':
            options.suffix = "_compressed"
    elif parsed.command == "convert":
        options.suffix = getattr(parsed, 'suffix', '')
        options.output_format = getattr(parsed, 'format', 'JPEG')
    else:  # batch
        options.suffix = getattr(parsed, 'suffix', None) or "_processed"

    return options


def _apply_preset_defaults(options: ProcessOptions, parsed) -> ProcessOptions:
    """Apply a --preset's values for options the user did not set on the CLI.

    Precedence: explicit CLI > --preset > config file > built-in defaults.
    A CLI option counts as "set" when it appears in the parsed namespace
    (config-capable args use ``default=argparse.SUPPRESS``). Runs AFTER the
    config layer so a preset overrides config-file defaults.

    ``jobs`` and ``output_dir`` are deliberately not applied from a preset:
    job count is machine-specific and output dir is job-specific.

    Returns None (after printing an error) when the named preset doesn't exist
    — the caller turns that into a nonzero exit.
    """
    preset_name = getattr(parsed, "preset", None)
    if not preset_name:
        return options
    from .presets import load_preset
    preset_opts = load_preset(preset_name)
    if preset_opts is None:
        # stderr so a --json stdout payload stays clean
        print(f"{_t('msg_preset_not_found')}: {preset_name}", file=sys.stderr)
        return None
    defaults = ProcessOptions()
    for field in ProcessOptions.__dataclass_fields__:
        if field in ("jobs", "output_dir"):
            continue
        cli_dest = _PRESET_CLI_DESTS.get(field, field)
        if not hasattr(parsed, cli_dest):
            value = getattr(preset_opts, field)
            # A field at its dataclass default carries no intent — applying
            # it would clobber command-level defaults (batch's
            # suffix="_processed") and config values with the ProcessOptions
            # default (suffix="_compressed"). Only non-default values from the
            # preset are real intent.
            if value == getattr(defaults, field):
                continue
            setattr(options, field, value)
    return options


def _summarize_preset_options(opts: ProcessOptions) -> list:
    """List non-default ProcessOptions fields (what a preset actually carries).

    Lets ``preset save`` report how many settings were captured and ``preset
    load`` show the option set it would apply. jobs/output_dir are job-specific
    and never summarized.
    """
    defaults = ProcessOptions()
    out = []
    for f in ProcessOptions.__dataclass_fields__:
        if f in ("jobs", "output_dir"):
            continue
        v = getattr(opts, f)
        if v != getattr(defaults, f):
            out.append(f"{f}={v}")
    return out


def _print_result(result: ProcessResult):
    """Print a single processing result."""
    if result.success:
        savings = format_size(result.input_size - result.output_size)
        pct = ((result.input_size - result.output_size) / result.input_size * 100
               if result.input_size > 0 else 0)
        print(f"  ✅ {os.path.basename(result.input_path)}")
        if result.input_format == "RAW":
            print(f"     📷 RAW → {result.output_format}")
        else:
            print(f"     {result.input_format} → {result.output_format}")
        if result.achieved_quality:
            print(f"     {_t('msg_quality')}: {result.achieved_quality}")
        if result.ssim is not None:
            print(f"     SSIM: {result.ssim:.3f}")
        print(f"     {result.input_dims[0]}×{result.input_dims[1]} → "
              f"{result.output_dims[0]}×{result.output_dims[1]}")
        print(f"     {format_size(result.input_size)} → {format_size(result.output_size)} "
              f"(-{savings}, -{pct:.1f}%)")
        print(f"     → {result.output_path}")
        if result.error:  # target-size warning
            print(f"     ⚠️  {result.error}")
    else:
        print(f"  ❌ {os.path.basename(result.input_path)}: {result.error}")


def _print_batch_summary(result: BatchResult):
    """Print batch processing summary."""
    print()
    print("─" * 60)
    print(f"📊 {_t('msg_summary')}")
    print(f"   {_t('msg_success')}: {result.success_count}")
    print(f"   {_t('msg_failed')}:  {result.fail_count}")
    print(f"   {_t('msg_total_original')}: {format_size(result.total_input_size)}")
    print(f"   {_t('msg_total_compressed')}: {format_size(result.total_output_size)}")
    savings = format_size(result.savings_bytes)
    print(f"   {_t('msg_saved')}: {savings} ({result.savings_percent:.1f}%)")
    print("─" * 60)


def _pre_parse_language(args):
    """Peek at --language/--lang, --json and --config before the real parse.

    argparse bakes help= strings at parser-construction time, so the language
    must be resolved BEFORE the parser tree is built. A throwaway parser with
    ``parse_known_args`` tolerates the full real CLI, handles both
    ``--language zh`` and ``--language=zh``, and also surfaces ``--config`` so
    an explicit config file's ``language`` key takes effect. ``--json`` is
    captured for the same reason: a subparser's own ``--json`` default used
    to overwrite a top-level ``photo-s --json <cmd>`` back to False.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=False)
    p.add_argument("--language", "--lang", default=None)
    p.add_argument("--config", default=None)
    try:
        ns, _ = p.parse_known_args(args)
        return ns.language, ns.config, ns.json
    except SystemExit:
        return None, None, False


def run_cli(args: List[str] = None) -> int:
    """
    Parse CLI arguments and execute the requested operation.

    Returns exit code (0 = success, 1 = error).
    """
    if args is None:
        args = sys.argv[1:]

    # Resolve language first: all help= / message strings below are rendered
    # via i18n._t, which reads i18n.CURRENT_LANG at call time.
    lang_explicit, cfg_override, json_flag = _pre_parse_language(args)
    from . import i18n
    i18n.CURRENT_LANG = i18n.resolve_language(
        explicit=lang_explicit, config_path=cfg_override,
        use_config=True, use_persisted=False)

    parser = argparse.ArgumentParser(
        prog="photo-s",
        description=_t("desc"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_t("epilog"),
    )
    parser.add_argument(
        "--json", action="store_true",
        help=_t("help___json"),
    )
    parser.add_argument(
        "--language", "--lang", choices=["en", "zh", "auto"],
        default="auto", help=_t("help___language"),
    )
    _version_str = f"photo-s {__version__}"
    if not _gui_module_available():
        # Lite build (no GUI module bundled) — mark it so bug reports can
        # tell the two editions apart.
        _version_str += " (lite)"
    parser.add_argument(
        "--version", action="version", version=_version_str,
        help=_t("help___version"),
    )

    subparsers = parser.add_subparsers(dest="command", help=_t("cmd_commands"))

    # ── compress subcommand ──────────────────────────────────────────────────
    compress_parser = subparsers.add_parser(
        "compress", help=_t('cmd_compress'),
    )
    compress_parser.add_argument(
        "files", nargs="+", help=_t('help___files'),
    )
    compress_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    compress_parser.add_argument(
        "-q", "--quality", type=int, default=argparse.SUPPRESS,
        help=_t('help___quality'),
    )
    compress_parser.add_argument(
        "-o", "--output-dir", type=str, default=argparse.SUPPRESS,
        help=_t('help___output_dir'),
    )
    compress_parser.add_argument(
        "--suffix", type=str, default=argparse.SUPPRESS,
        help=_t('help___suffix'),
    )
    compress_parser.add_argument(
        "--no-exif", action="store_true",
        help=_t('help___no_exif'),
    )
    compress_parser.add_argument(
        "--resize", type=_dimensions_arg, default=argparse.SUPPRESS,
        metavar="WxH",
        help=_t('help___resize'),
    )
    compress_parser.add_argument(
        "--scale", type=int, default=argparse.SUPPRESS, metavar="PCT",
        help=_t('help___scale'),
    )
    compress_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )
    compress_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    compress_parser.add_argument(
        "-j", "--jobs", type=int, default=argparse.SUPPRESS, metavar="N",
        help=_t('help___jobs'),
    )
    compress_parser.add_argument(
        "--target-size", type=_target_size_arg, default=argparse.SUPPRESS,
        metavar="SIZE",
        help=_t('help___target_size'),
    )
    _add_raw_args(compress_parser)
    compress_parser.add_argument(
        "--no-auto-rotate", action="store_true",
        help=_t('help___no_auto_rotate'),
    )
    compress_parser.add_argument(
        "--remove-original", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___remove_original'),
    )
    compress_parser.add_argument(
        "-y", "--yes", action="store_true",
        help=_t('help___yes'),
    )
    compress_parser.add_argument(
        "--rename", type=str, default=argparse.SUPPRESS, metavar="PATTERN",
        help=_t('help___rename'),
    )
    compress_parser.add_argument(
        "--organize", type=str, default=argparse.SUPPRESS, metavar="PRESET|PATTERN",
        help=_t('help___organize'),
    )
    compress_parser.add_argument(
        "--sizes", type=str, default=None, metavar="LABEL:WxH,...",
        help=_t('help___sizes'),
    )
    compress_parser.add_argument(
        "--watermark-text", type=str, default=argparse.SUPPRESS, metavar="TEXT",
        help=_t('help___watermark_text'),
    )
    compress_parser.add_argument(
        "--watermark-pos", type=str, default=argparse.SUPPRESS,
        choices=["CENTER", "TOP_LEFT", "TOP_RIGHT", "BOTTOM_LEFT",
                 "BOTTOM_RIGHT", "TOP", "BOTTOM"],
        help=_t('help___watermark_pos'),
    )
    compress_parser.add_argument(
        "--watermark-opacity", type=int, default=argparse.SUPPRESS,
        help=_t('help___watermark_opacity'),
    )
    _add_advanced_args(compress_parser)
    _add_transform_args(compress_parser)

    # ── convert subcommand ───────────────────────────────────────────────────
    convert_parser = subparsers.add_parser(
        "convert", help=_t('cmd_convert'),
    )
    convert_parser.add_argument(
        "files", nargs="+", help=_t('help___files'),
    )
    convert_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    convert_parser.add_argument(
        "-f", "--format", type=_format_arg, default=argparse.SUPPRESS,
        help=_t('help___format'),
    )
    convert_parser.add_argument(
        "-q", "--quality", type=int, default=argparse.SUPPRESS,
        help=_t('help___quality'),
    )
    convert_parser.add_argument(
        "-o", "--output-dir", type=str, default=argparse.SUPPRESS,
        help=_t('help___output_dir'),
    )
    convert_parser.add_argument(
        "--prefix", type=str, default=argparse.SUPPRESS,
        help=_t('help___prefix'),
    )
    convert_parser.add_argument(
        "--suffix", type=str, default=argparse.SUPPRESS,
        help=_t('help___suffix'),
    )
    convert_parser.add_argument(
        "--no-exif", action="store_true",
        help=_t('help___no_exif'),
    )
    convert_parser.add_argument(
        "--resize", type=_dimensions_arg, default=argparse.SUPPRESS,
        metavar="WxH",
        help=_t('help___resize'),
    )
    convert_parser.add_argument(
        "--scale", type=int, default=argparse.SUPPRESS, metavar="PCT",
        help=_t('help___scale'),
    )
    convert_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )
    convert_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    convert_parser.add_argument(
        "--overwrite", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___overwrite'),
    )
    convert_parser.add_argument(
        "--target-size", type=_target_size_arg, default=argparse.SUPPRESS,
        metavar="SIZE",
        help=_t('help___target_size'),
    )
    _add_raw_args(convert_parser)
    convert_parser.add_argument(
        "--no-auto-rotate", action="store_true",
        help=_t('help___no_auto_rotate'),
    )
    convert_parser.add_argument(
        "--remove-original", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___remove_original'),
    )
    convert_parser.add_argument(
        "-y", "--yes", action="store_true",
        help=_t('help___yes'),
    )
    _add_advanced_args(convert_parser)
    _add_transform_args(convert_parser)

    # ── batch subcommand (combined) ──────────────────────────────────────────
    batch_parser = subparsers.add_parser(
        "batch", help=_t('cmd_batch'),
    )
    batch_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    batch_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    batch_parser.add_argument(
        "--trace", type=str, default=None, metavar="DIR",
        help=_t('help___trace'),
    )
    batch_parser.add_argument(
        "--write-xmp", action="store_true",
        help=_t('help___write_xmp'),
    )
    batch_parser.add_argument(
        "-f", "--format", type=_format_arg, default=argparse.SUPPRESS,
        help=_t('help___format'),
    )
    batch_parser.add_argument(
        "-q", "--quality", type=int, default=argparse.SUPPRESS,
        help=_t('help___quality'),
    )
    batch_parser.add_argument(
        "-o", "--output-dir", type=str, default=argparse.SUPPRESS,
        help=_t('help___output_dir'),
    )
    batch_parser.add_argument(
        "--prefix", type=str, default=argparse.SUPPRESS,
        help=_t('help___prefix'),
    )
    batch_parser.add_argument(
        "--suffix", type=str, default=argparse.SUPPRESS,
        help=_t('help___suffix'),
    )
    batch_parser.add_argument(
        "--resize", type=_dimensions_arg, default=argparse.SUPPRESS,
        metavar="WxH",
        help=_t('help___resize'),
    )
    batch_parser.add_argument(
        "--scale", type=int, default=argparse.SUPPRESS, metavar="PCT",
        help=_t('help___scale'),
    )
    batch_parser.add_argument(
        "--no-exif", action="store_true",
        help=_t('help___no_exif'),
    )
    batch_parser.add_argument(
        "--progressive", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___progressive'),
    )
    batch_parser.add_argument(
        "--no-optimize", action="store_true",
        help=_t('help___no_optimize'),
    )
    batch_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    batch_parser.add_argument(
        "-j", "--jobs", type=int, default=argparse.SUPPRESS, metavar="N",
        help=_t('help___jobs'),
    )
    batch_parser.add_argument(
        "--overwrite", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___overwrite'),
    )
    batch_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )
    batch_parser.add_argument(
        "--target-size", type=_target_size_arg, default=argparse.SUPPRESS,
        metavar="SIZE",
        help=_t('help___target_size'),
    )
    _add_raw_args(batch_parser)
    batch_parser.add_argument(
        "--no-auto-rotate", action="store_true",
        help=_t('help___no_auto_rotate'),
    )
    batch_parser.add_argument(
        "--remove-original", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___remove_original'),
    )
    batch_parser.add_argument(
        "-y", "--yes", action="store_true",
        help=_t('help___yes'),
    )
    batch_parser.add_argument(
        "--rename", type=str, default=argparse.SUPPRESS, metavar="PATTERN",
        help=_t('help___rename'),
    )
    batch_parser.add_argument(
        "--organize", type=str, default=argparse.SUPPRESS, metavar="PRESET|PATTERN",
        help=_t('help___organize'),
    )
    batch_parser.add_argument(
        "--watermark-text", type=str, default=argparse.SUPPRESS, metavar="TEXT",
        help=_t('help___watermark_text'),
    )
    batch_parser.add_argument(
        "--watermark-image", type=str, default=argparse.SUPPRESS, metavar="PATH",
        help=_t('help___watermark_image'),
    )
    batch_parser.add_argument(
        "--watermark-pos", type=str, default=argparse.SUPPRESS,
        choices=["CENTER", "TOP_LEFT", "TOP_RIGHT", "BOTTOM_LEFT",
                 "BOTTOM_RIGHT", "TOP", "BOTTOM"],
        help=_t('help___watermark_pos'),
    )
    batch_parser.add_argument(
        "--watermark-opacity", type=int, default=argparse.SUPPRESS, metavar="0-100",
        help=_t('help___watermark_opacity'),
    )
    batch_parser.add_argument(
        "--sizes", type=str, default=None, metavar="LABEL:WxH,...",
        help=_t('help___sizes'),
    )
    _add_advanced_args(batch_parser)
    _add_transform_args(batch_parser)
    batch_parser.add_argument(
        "--profiles", type=str, default=None, metavar="P1,P2",
        help=_t('help___profiles'),
    )

    # ── exif subcommand ─────────────────────────────────────────────────────
    exif_parser = subparsers.add_parser(
        "exif", help=_t('cmd_exif'),
    )
    exif_parser.add_argument(
        "files", nargs="*", help=_t('help___image_files'),
    )
    exif_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    exif_parser.add_argument(
        "--show", action="store_true",
        help=_t('help___show'),
    )
    exif_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    exif_parser.add_argument(
        "--list", action="store_true",
        help=_t('help___list'),
    )
    # ── write tags ──
    exif_parser.add_argument(
        "--artist", type=str, default=None, help=_t('help___artist'),
    )
    exif_parser.add_argument(
        "--copyright", type=str, default=None, help=_t('help___copyright'),
    )
    exif_parser.add_argument(
        "--description", type=str, default=None, help=_t('help___description'),
    )
    exif_parser.add_argument(
        "--caption", type=str, default=None, help=_t('help___caption'),
    )
    exif_parser.add_argument(
        "--title", type=str, default=None, help=_t('help___title'),
    )
    exif_parser.add_argument(
        "--rating", type=int, default=None, metavar="0-5",
        help=_t('help___rating'),
    )
    exif_parser.add_argument(
        "--keywords", type=str, default=None, metavar="A,B",
        help=_t('help___keywords'),
    )
    exif_parser.add_argument(
        "--date", type=str, default=None, metavar="DATETIME",
        help=_t('help___date'),
    )
    exif_parser.add_argument(
        "--software", type=str, default=None, help=_t('help___software'),
    )
    exif_parser.add_argument(
        "--lens", type=str, default=None,
        help=_t('help___lens'),
    )
    exif_parser.add_argument(
        "--iso", type=int, default=None, metavar="N",
        help=_t('help___iso'),
    )
    exif_parser.add_argument(
        "--shutter", type=str, default=None, metavar="SEC",
        help=_t('help___shutter'),
    )
    exif_parser.add_argument(
        "--aperture", type=str, default=None, metavar="F",
        help=_t('help___aperture'),
    )
    exif_parser.add_argument(
        "--focal", type=str, default=None, metavar="MM",
        help=_t('help___focal'),
    )
    exif_parser.add_argument(
        "--make", type=str, default=None, help=_t('help___make'),
    )
    exif_parser.add_argument(
        "--model", type=str, default=None, help=_t('help___model'),
    )
    exif_parser.add_argument(
        "--gps", type=str, default=None, metavar="LAT,LON",
        help=_t('help___gps'),
    )
    exif_parser.add_argument(
        "--date-from-mtime", action="store_true",
        help=_t('help___date_from_mtime'),
    )
    # ── filter (with --show) ──
    exif_parser.add_argument(
        "--rating-min", type=int, default=None, metavar="N",
        help=_t('help___rating_min'),
    )
    exif_parser.add_argument(
        "--camera", type=str, default=None, metavar="MODEL",
        help=_t('help___camera'),
    )
    exif_parser.add_argument(
        "--date-from", type=str, default=None, metavar="YYYY-MM-DD",
        help=_t('help___date_from'),
    )
    exif_parser.add_argument(
        "--date-to", type=str, default=None, metavar="YYYY-MM-DD",
        help=_t('help___date_to'),
    )
    # ── batch import ──
    exif_parser.add_argument(
        "--from-csv", type=str, default=None, metavar="meta.csv",
        help=_t('help___from_csv'),
    )
    exif_parser.add_argument(
        "--from-json", type=str, default=None, metavar="meta.json",
        help=_t('help___from_json'),
    )

    # ── preset subcommand ───────────────────────────────────────────────────
    preset_parser = subparsers.add_parser(
        "preset", help=_t('cmd_preset'),
    )
    preset_subs = preset_parser.add_subparsers(dest="preset_action")

    preset_save = preset_subs.add_parser("save", help=_t('cmd_save'))
    preset_save.add_argument("name", help=_t('help___preset_name'))
    preset_save.add_argument("-f", "--format", type=str, default="JPEG")
    preset_save.add_argument("-q", "--quality", type=int, default=85)
    preset_save.add_argument("--resize", type=str, default=None)
    preset_save.add_argument("--suffix", type=str, default="_compressed")
    preset_save.add_argument("--desc", type=str, default="", help=_t('help___desc'))
    # Full option capture: the shared transform args let a CLI-created
    # preset carry the whole "look" (tone, composition, denoise, LUT, ...),
    # not just quality/format/resize — parity with GUI-saved presets.
    _add_transform_args(preset_save)

    preset_list = preset_subs.add_parser("list", help=_t('cmd_list'))
    preset_load = preset_subs.add_parser("load", help=_t('cmd_load'))
    preset_load.add_argument("name", help=_t('help___preset_name'))
    preset_delete = preset_subs.add_parser("delete", help=_t('cmd_delete'))
    preset_delete.add_argument("name", help=_t('help___preset_name'))

    # ── lens-profile subcommand (user-maintained lens registry) ─────────────
    lens_prof_parser = subparsers.add_parser(
        "lens-profile", help=_t('cmd_lens_profile'),
    )
    lens_prof_subs = lens_prof_parser.add_subparsers(dest="lens_profile_action")
    lp_save = lens_prof_subs.add_parser("save", help=_t('cmd_save'))
    lp_save.add_argument("name", help=_t('help___lens_profile_name'))
    lp_save.add_argument("--distort", type=float, default=None,
                         metavar="K1", help=_t('help___lens_distort'))
    lp_save.add_argument("--vignette", type=str, default=None,
                         metavar="AMOUNT[,MIDPOINT]",
                         help=_t('help___lens_vignette'))
    lp_save.add_argument("--ca", type=str, default=None,
                         metavar="R_SCALE,B_SCALE",
                         help=_t('help___lens_ca'))
    lp_save.add_argument("--desc", type=str, default="", help=_t('help___desc'))
    lens_prof_subs.add_parser("list", help=_t('cmd_list'))
    lp_delete = lens_prof_subs.add_parser("delete", help=_t('cmd_delete'))
    lp_delete.add_argument("name", help=_t('help___lens_profile_name'))

    # ── plugin subcommand ───────────────────────────────────────────────────
    plugin_parser = subparsers.add_parser(
        "plugin", help=_t('cmd_plugin'),
    )
    plugin_subs = plugin_parser.add_subparsers(dest="plugin_action")

    plugin_list = plugin_subs.add_parser(
        "list", help=_t('cmd_list'),
    )
    plugin_list.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    plugin_install = plugin_subs.add_parser(
        "install", help=_t('cmd_install'),
    )
    plugin_install.add_argument("name", help=_t('help___plugin_name_eg'))
    plugin_install.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    plugin_install.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )

    plugin_uninstall = plugin_subs.add_parser(
        "uninstall", help=_t('cmd_uninstall'),
    )
    plugin_uninstall.add_argument("name", help=_t('help___plugin_name'))
    plugin_uninstall.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    plugin_uninstall.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )

    plugin_info = plugin_subs.add_parser(
        "info", help=_t('cmd_info'),
    )
    plugin_info.add_argument("name", help=_t('help___plugin_name'))
    plugin_info.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    plugin_fetch = plugin_subs.add_parser(
        "fetch", help=_t('cmd_fetch'),
    )
    plugin_fetch.add_argument("name", help=_t('help___plugin_name'))
    plugin_fetch.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    plugin_scaffold = plugin_subs.add_parser(
        "scaffold", help=_t('cmd_scaffold'),
    )
    plugin_scaffold.add_argument(
        "name", help=_t('help___plugin_name_alnum'),
    )
    plugin_scaffold.add_argument(
        "--dir", type=str, default=None, metavar="DIR",
        help=_t('help___dir'),
    )
    plugin_scaffold.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── watch subcommand ────────────────────────────────────────────────────
    watch_parser = subparsers.add_parser(
        "watch", help=_t('cmd_watch'),
    )
    watch_parser.add_argument(
        "directory", help=_t('help___directory'),
    )
    watch_parser.add_argument(
        "-f", "--format", type=_format_arg, default="JPEG",
        help=_t('help___format'),
    )
    watch_parser.add_argument(
        "-q", "--quality", type=int, default=85,
        help=_t('help___quality'),
    )
    watch_parser.add_argument(
        "-o", "--output-dir", type=str, default=None,
        help=_t('help___output_dir'),
    )
    watch_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    watch_parser.add_argument(
        "--remove-original", action="store_true",
        help=_t('help___remove_original'),
    )
    watch_parser.add_argument(
        "--resize", type=_dimensions_arg, default=None, metavar="WxH",
        help=_t('help___resize'),
    )
    watch_parser.add_argument(
        "-y", "--yes", action="store_true",
        help=_t('help___yes'),
    )

    # ── dedup subcommand ────────────────────────────────────────────────────
    dedup_parser = subparsers.add_parser(
        "dedup", help=_t('cmd_dedup'),
    )
    dedup_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    dedup_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    dedup_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    dedup_parser.add_argument(
        "--threshold", type=int, default=5, metavar="N",
        help=_t('help___threshold'),
    )
    dedup_parser.add_argument(
        "--action", type=str, default="report",
        choices=["report", "move", "delete", "keep-sharpest"],
        help=_t('help___action'),
    )
    dedup_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )

    # ── info subcommand ─────────────────────────────────────────────────────
    info_parser = subparsers.add_parser(
        "info", help=_t('cmd_info'),
    )
    info_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── rename subcommand ───────────────────────────────────────────────────
    rename_parser = subparsers.add_parser(
        "rename", help=_t('cmd_rename'),
    )
    rename_parser.add_argument(
        "files", nargs="+", help=_t('help___files'),
    )
    rename_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    rename_parser.add_argument(
        "--pattern", type=str, required=True, metavar="PATTERN",
        help=_t('help___pattern'),
    )
    rename_parser.add_argument(
        "-o", "--output-dir", type=str, default=None,
        help=_t('help___output_dir'),
    )
    rename_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )
    rename_parser.add_argument(
        "--overwrite", action="store_true",
        help=_t('help___overwrite'),
    )
    rename_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )

    # ── check subcommand ────────────────────────────────────────────────────
    check_parser = subparsers.add_parser(
        "check", help=_t('cmd_check'),
    )
    check_parser.add_argument(
        "files", nargs="+", help=_t('help___files'),
    )
    check_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    check_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── contact-sheet subcommand ────────────────────────────────────────────
    sheet_parser = subparsers.add_parser(
        "contact-sheet", help=_t('cmd_contact_sheet'),
    )
    sheet_parser.add_argument(
        "files", nargs="+", help=_t('help___files'),
    )
    sheet_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    sheet_parser.add_argument(
        "-o", "--output", type=str, required=True,
        help=_t('help___output'),
    )
    sheet_parser.add_argument(
        "--cols", type=int, default=4,
        help=_t('help___cols'),
    )
    sheet_parser.add_argument(
        "--thumb", type=_dimensions_arg, default=(240, 240), metavar="WxH",
        help=_t('help___thumb'),
    )
    sheet_parser.add_argument(
        "--caption", action="store_true",
        help=_t('help___caption'),
    )
    sheet_parser.add_argument(
        "--bg", type=str, default="#000000",
        help=_t('help___bg'),
    )
    sheet_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )

    # ── cull subcommand ─────────────────────────────────────────────────────
    cull_parser = subparsers.add_parser(
        "cull", help=_t('cmd_cull'),
    )
    cull_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    cull_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    cull_parser.add_argument(
        "--overexposed-max", type=float, default=None, metavar="PCT",
        help=_t('help___overexposed_max'),
    )
    cull_parser.add_argument(
        "--underexposed-max", type=float, default=None, metavar="PCT",
        help=_t('help___underexposed_max'),
    )
    cull_parser.add_argument(
        "--luminance-min", type=float, default=None, metavar="0-1",
        help=_t('help___luminance_min'),
    )
    cull_parser.add_argument(
        "--luminance-max", type=float, default=None, metavar="0-1",
        help=_t('help___luminance_max'),
    )
    cull_parser.add_argument(
        "--sharpness-min", type=float, default=None, metavar="N",
        help=_t('help___sharpness_min'),
    )
    cull_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )
    cull_parser.add_argument(
        "--list", action="store_true",
        help=_t('help___list'),
    )
    cull_parser.add_argument(
        "--score", action="store_true",
        help=_t('help___cull_score'),
    )
    cull_parser.add_argument(
        "--burst", action="store_true",
        help=_t('help___cull_burst'),
    )
    cull_parser.add_argument(
        "--gap", type=float, default=2.0, metavar="SEC",
        help=_t('help___cull_gap'),
    )

    # ── select subcommand (keeper workflow: sort by rating) ────────────────
    select_parser = subparsers.add_parser(
        "select", help=_t('cmd_select'),
    )
    select_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    select_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    select_parser.add_argument(
        "--keep-min", type=int, default=4, metavar="N",
        help=_t('help___keep_min'),
    )
    select_parser.add_argument(
        "--reject-max", type=int, default=2, metavar="N",
        help=_t('help___reject_max'),
    )
    select_parser.add_argument(
        "--selects-dir", type=str, default=None, metavar="DIR",
        help=_t('help___selects_dir'),
    )
    select_parser.add_argument(
        "--rejects-dir", type=str, default=None, metavar="DIR",
        help=_t('help___rejects_dir'),
    )
    select_parser.add_argument(
        "--copy", action="store_true",
        help=_t('help___select_copy'),
    )
    select_parser.add_argument(
        "--dry-run", action="store_true",
        help=_t('help___dry_run'),
    )
    select_parser.add_argument(
        "-y", "--yes", action="store_true",
        help=_t('help___yes'),
    )
    select_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── hdr subcommand (exposure fusion of bracketed shots) ────────────────
    hdr_parser = subparsers.add_parser(
        "hdr", help=_t('cmd_hdr'),
    )
    hdr_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    hdr_parser.add_argument(
        "-o", "--output", type=str, default=None, metavar="OUT.jpg",
        help=_t('help___output'),
    )
    hdr_parser.add_argument(
        "--align", action="store_true",
        help=_t('help___hdr_align'),
    )
    hdr_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── blurfaces subcommand (privacy masking, optional opencv) ────────────
    blurfaces_parser = subparsers.add_parser(
        "blurfaces", help=_t('cmd_blurfaces'),
    )
    blurfaces_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    blurfaces_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    blurfaces_parser.add_argument(
        "--mode", type=str, default="blur", choices=["blur", "pixelate"],
        help=_t('help___blur_faces'),
    )
    blurfaces_parser.add_argument(
        "--margin", type=int, default=20, metavar="PCT",
        help=_t('help___blur_faces_margin'),
    )
    blurfaces_parser.add_argument(
        "-o", "--output-dir", type=str, default=None, metavar="DIR",
        help=_t('help___output_dir'),
    )
    blurfaces_parser.add_argument(
        "--overwrite", action="store_true", default=argparse.SUPPRESS,
        help=_t('help___overwrite'),
    )
    blurfaces_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── hash subcommand ─────────────────────────────────────────────────────
    hash_parser = subparsers.add_parser(
        "hash", help=_t('cmd_hash'),
    )
    hash_parser.add_argument(
        "paths", nargs="*", help=_t('help___paths'),
    )
    hash_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    hash_parser.add_argument(
        "-o", "--output", type=str, default=None, metavar="manifest.csv",
        help=_t('help___output'),
    )
    hash_parser.add_argument(
        "--verify", type=str, default=None, metavar="manifest.csv",
        help=_t('help___verify'),
    )
    hash_parser.add_argument(
        "--sha256", action="store_true", help=_t('help___sha256'),
    )
    hash_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── gallery subcommand ──────────────────────────────────────────────────
    gallery_parser = subparsers.add_parser(
        "gallery", help=_t('cmd_gallery'),
    )
    gallery_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    gallery_parser.add_argument(
        "-o", "--output", type=str, required=True, metavar="DIR",
        help=_t('help___output_dir'),
    )
    gallery_parser.add_argument(
        "--title", type=str, default="PhotoS Gallery", metavar="TITLE",
        help=_t('help___title'),
    )
    gallery_parser.add_argument(
        "--thumb", type=int, default=360, metavar="PX",
        help=_t('help___thumb'),
    )
    gallery_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    gallery_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── analyze subcommand (perceptual feedback for agents/models) ─────────
    analyze_parser = subparsers.add_parser(
        "analyze", help=_t('cmd_analyze'),
    )
    analyze_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    analyze_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    analyze_parser.add_argument(
        "--sample-size", type=int, default=256, metavar="N",
        help=_t('help___sample_size'),
    )
    analyze_parser.add_argument(
        "--grid", type=int, default=0, choices=[0, 4, 8], metavar="N",
        help=_t('help___grid'),
    )
    analyze_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── lr-scan subcommand (Lightroom data bridge report, v1.9.0) ──────────
    lr_scan_parser = subparsers.add_parser(
        "lr-scan", help=_t('cmd_lr_scan'),
    )
    lr_scan_parser.add_argument(
        "paths", nargs="*", help=_t('help___lr_paths'),
    )
    lr_scan_parser.add_argument(
        "--export-dir", type=str, default=None, metavar="DIR",
        help=_t('help___lr_export'),
    )
    lr_scan_parser.add_argument(
        "--render-dir", type=str, default=None, metavar="DIR",
        help=_t('help___lr_render'),
    )
    lr_scan_parser.add_argument(
        "--sanitize", action="store_true",
        help=_t('help___lr_sanitize'),
    )
    lr_scan_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── lr-* data-layer subcommands (v1.9.0 phase 2, zero new deps) ─────────
    lr_train_parser = subparsers.add_parser(
        "lr-train", help=_t('cmd_lr_train'),
    )
    lr_train_parser.add_argument(
        "--data", type=str, required=True, metavar="JSONL",
        help=_t('help___lr_data'),
    )
    lr_train_parser.add_argument(
        "--images", type=str, default=None, metavar="DIR",
        help=_t('help___lr_images'),
    )
    lr_train_parser.add_argument(
        "--out", type=str, default="auto_tone.npz", metavar="NPZ",
        help=_t('help___lr_out'),
    )
    lr_train_parser.add_argument(
        "--lambda", type=float, default=1.0, dest="ridge_lambda",
        metavar="L", help=_t('help___lr_lambda'),
    )

    lr_predict_parser = subparsers.add_parser(
        "lr-predict", help=_t('cmd_lr_predict'),
    )
    lr_predict_parser.add_argument(
        "image", type=str, help=_t('help___lr_image'),
    )
    lr_predict_parser.add_argument(
        "--model", type=str, default="auto_tone.npz", metavar="NPZ",
        help=_t('help___lr_model'),
    )
    lr_predict_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    lr_recipes_parser = subparsers.add_parser(
        "lr-recipes", help=_t('cmd_lr_recipes'),
    )
    lr_recipes_parser.add_argument(
        "--data", type=str, required=True, metavar="JSONL",
        help=_t('help___lr_data'),
    )
    lr_recipes_parser.add_argument(
        "-k", type=int, default=6, metavar="N",
        help=_t('help___lr_k'),
    )
    lr_recipes_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    lr_similar_parser = subparsers.add_parser(
        "lr-similar", help=_t('cmd_lr_similar'),
    )
    lr_similar_parser.add_argument(
        "image", type=str, help=_t('help___lr_image'),
    )
    lr_similar_parser.add_argument(
        "--data", type=str, required=True, metavar="JSONL",
        help=_t('help___lr_data'),
    )
    lr_similar_parser.add_argument(
        "--images", type=str, default=None, metavar="DIR",
        help=_t('help___lr_images'),
    )
    lr_similar_parser.add_argument(
        "-k", type=int, default=5, metavar="N",
        help=_t('help___lr_k'),
    )
    lr_similar_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    lr_eval_parser = subparsers.add_parser(
        "lr-eval", help=_t('cmd_lr_eval'),
    )
    lr_eval_parser.add_argument(
        "--data", type=str, required=True, metavar="JSONL",
        help=_t('help___lr_data'),
    )
    lr_eval_parser.add_argument(
        "--images", type=str, default=None, metavar="DIR",
        help=_t('help___lr_images'),
    )
    lr_eval_parser.add_argument(
        "--out", type=str, default="eval.json", metavar="JSON",
        help=_t('help___lr_out'),
    )
    lr_eval_parser.add_argument(
        "--sample", type=int, default=200, metavar="N",
        help=_t('help___lr_sample'),
    )

    lr_merge_parser = subparsers.add_parser(
        "lr-merge", help=_t('cmd_lr_merge'),
    )
    lr_merge_parser.add_argument(
        "packages", nargs="+", metavar="PKG",
        help=_t('help___lr_packages'),
    )
    lr_merge_parser.add_argument(
        "--out", type=str, default="merged", metavar="DIR",
        help=_t('help___lr_merge_out'),
    )
    lr_merge_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── diff / audit / preview (v1.9.0 tool layer, agent feedback) ─────────
    diff_parser = subparsers.add_parser(
        "diff", help=_t('cmd_diff'),
    )
    diff_parser.add_argument(
        "paths", nargs=2, metavar="A B",
        help=_t('help___diff_paths'),
    )
    diff_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    audit_parser = subparsers.add_parser(
        "audit", help=_t('cmd_audit'),
    )
    audit_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    audit_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    audit_parser.add_argument(
        "--over-max", type=float, default=None, metavar="PCT",
        help=_t('help___over_max'),
    )
    audit_parser.add_argument(
        "--under-max", type=float, default=None, metavar="PCT",
        help=_t('help___under_max'),
    )
    audit_parser.add_argument(
        "--blur-min", type=float, default=None, metavar="SCORE",
        help=_t('help___blur_min'),
    )
    audit_parser.add_argument(
        "--aesthetic", type=float, default=None, metavar="1-10",
        help=_t('help___aesthetic'),
    )
    audit_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    suggest_parser = subparsers.add_parser(
        "suggest", help=_t('cmd_suggest'),
    )
    suggest_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    suggest_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    suggest_parser.add_argument(
        "--scale", type=float, default=1.0, metavar="0-1",
        help=_t('help___scale'),
    )
    suggest_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    xmp_parser = subparsers.add_parser(
        "xmp-export", help=_t('cmd_xmp_export'),
    )
    xmp_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    xmp_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    xmp_parser.add_argument(
        "--preset", type=str, default=None, metavar="NAME",
        help=_t('help_xmp_preset'),
    )
    xmp_parser.add_argument(
        "--options", type=str, default=None, metavar="JSON",
        help=_t('help_xmp_options'),
    )
    xmp_parser.add_argument(
        "--auto-tone", type=float, default=None, metavar="0-1",
        help=_t('help___auto_tone'),
    )
    xmp_parser.add_argument(
        "--rating", type=int, default=None, metavar="0-5",
        help=_t('help_xmp_rating'),
    )
    xmp_parser.add_argument(
        "--keywords", type=str, default=None, metavar="A,B",
        help=_t('help_xmp_keywords'),
    )
    xmp_parser.add_argument(
        "--title", type=str, default=None, metavar="TEXT",
        help=_t('help_xmp_title'),
    )
    xmp_parser.add_argument(
        "--out-dir", type=str, default=None, metavar="DIR",
        help=_t('help_xmp_out_dir'),
    )
    xmp_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    autopilot_parser = subparsers.add_parser(
        "autopilot", help=_t('cmd_autopilot'),
    )
    autopilot_parser.add_argument(
        "dir", type=str, help=_t('help_autopilot_dir'),
    )
    autopilot_parser.add_argument(
        "--out-dir", type=str, default=None, metavar="DIR",
        help=_t('help_autopilot_out_dir'),
    )
    autopilot_parser.add_argument(
        "--mode", type=str, default="suggest",
        choices=["suggest", "auto_tone", "both"],
        help=_t('help_autopilot_mode'),
    )
    autopilot_parser.add_argument(
        "--auto-tone", type=float, default=1.0, metavar="0-1",
        help=_t('help___auto_tone'),
    )
    autopilot_parser.add_argument(
        "--scale", type=float, default=1.0, metavar="0-1",
        help=_t('help___scale'),
    )
    autopilot_parser.add_argument(
        "--write-xmp", action="store_true",
        help=_t('help___write_xmp'),
    )
    autopilot_parser.add_argument(
        "--aesthetic", type=float, default=None, metavar="1-10",
        help=_t('help___aesthetic'),
    )
    autopilot_parser.add_argument(
        "--over-max", type=float, default=argparse.SUPPRESS, metavar="PCT",
        help=_t('help___over_max'),
    )
    autopilot_parser.add_argument(
        "--under-max", type=float, default=argparse.SUPPRESS, metavar="PCT",
        help=_t('help___under_max'),
    )
    autopilot_parser.add_argument(
        "--blur-min", type=float, default=argparse.SUPPRESS, metavar="SCORE",
        help=_t('help___blur_min'),
    )
    autopilot_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    autopilot_parser.add_argument(
        "--scan-existing", action="store_true",
        help=_t('help_autopilot_scan_existing'),
    )
    autopilot_parser.add_argument(
        "--log", type=str, default=None, metavar="JSONL",
        help=_t('help_autopilot_log'),
    )
    autopilot_parser.add_argument(
        "-q", "--quality", type=int, default=None, metavar="0-100",
        help=_t('help___quality'),
    )
    autopilot_parser.add_argument(
        "-f", "--format", type=_format_arg, default=None,
        help=_t('help___format'),
    )
    autopilot_parser.add_argument(
        "--resize", type=str, default=None, metavar="WxH",
        help=_t('help___resize'),
    )

    index_parser = subparsers.add_parser(
        "index", help=_t('cmd_index'),
    )
    index_parser.add_argument(
        "paths", nargs="+", help=_t('help___paths'),
    )
    index_parser.add_argument(
        "-r", "--recursive", action="store_true",
        help=_t('help___recursive'),
    )
    index_parser.add_argument(
        "--index", type=str, default=None, metavar="NPZ",
        help=_t('help_index_index'),
    )
    index_parser.add_argument(
        "--rebuild", action="store_true",
        help=_t('help_index_rebuild'),
    )
    index_parser.add_argument(
        "--extractor", type=str, default=None, metavar="NAME",
        help=_t('help_index_extractor'),
    )
    index_parser.add_argument(
        "--tags", type=str, default=None, metavar="A,B",
        help=_t('help_index_tags'),
    )
    index_parser.add_argument(
        "--min-score", type=float, default=0.2, metavar="F",
        help=_t('help_index_min_score'),
    )
    index_parser.add_argument(
        "--max-tags", type=int, default=5, metavar="N",
        help=_t('help_index_max_tags'),
    )
    index_parser.add_argument(
        "--write-xmp", action="store_true",
        help=_t('help_index_write_xmp'),
    )
    index_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    find_parser = subparsers.add_parser(
        "find", help=_t('cmd_find'),
    )
    find_parser.add_argument(
        "query", nargs="*", help=_t('help_find_query'),
    )
    find_parser.add_argument(
        "--image", type=str, default=None, metavar="PATH",
        help=_t('help_find_image'),
    )
    find_parser.add_argument(
        "--index", type=str, default=None, metavar="NPZ",
        help=_t('help_find_index'),
    )
    find_parser.add_argument(
        "-k", type=int, default=10, metavar="N",
        help=_t('help_find_k'),
    )
    find_parser.add_argument(
        "--min-score", type=float, default=None, metavar="F",
        help=_t('help_find_min_score'),
    )
    find_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    preview_parser = subparsers.add_parser(
        "preview", help=_t('cmd_preview'),
    )
    preview_parser.add_argument(
        "path", type=str, help=_t('help___lr_image'),
    )
    preview_parser.add_argument(
        "--max-dim", type=int, default=1024, metavar="N",
        help=_t('help___max_dim'),
    )
    preview_parser.add_argument(
        "--no-histogram", action="store_true",
        help=_t('help___no_histogram'),
    )
    preview_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── bench subcommand ────────────────────────────────────────────────────
    bench_parser = subparsers.add_parser(
        "bench", help=_t('cmd_bench'),
    )
    bench_parser.add_argument(
        "--dir", type=str, required=True, metavar="DIR",
        help=_t('help___dir'),
    )
    bench_parser.add_argument(
        "-j", "--jobs", type=str, default="1,2,4,8", metavar="N,N,...",
        help=_t('help___jobs'),
    )
    bench_parser.add_argument(
        "--images", type=int, default=None, metavar="N",
        help=_t('help___images'),
    )
    bench_parser.add_argument(
        "--denoise", type=float, default=None, metavar="N",
        help=_t('help___denoise'),
    )
    bench_parser.add_argument(
        "--evaluate", action="store_true",
        help=_t('help___evaluate'),
    )
    bench_parser.add_argument(
        "--json", action="store_true",
        help=_t('help___json'),
    )

    # ── config subcommand ───────────────────────────────────────────────────
    config_parser = subparsers.add_parser(
        "config", help=_t('cmd_config'),
    )
    config_subs = config_parser.add_subparsers(dest="config_action")
    config_init = config_subs.add_parser(
        "init", help=_t('cmd_init'),
    )
    config_init.add_argument(
        "--path", type=str, default=None,
        help=_t('help___path'),
    )
    config_init.add_argument(
        "--force", action="store_true",
        help="overwrite an existing config file",
    )
    config_show = config_subs.add_parser(
        "show", help=_t('cmd_show'),
    )
    config_show.add_argument(
        "--path", type=str, default=None,
        help=_t('help___path'),
    )

    # ── serve subcommand ────────────────────────────────────────────────────
    serve_parser = subparsers.add_parser(
        "serve", help=_t('cmd_serve'),
    )
    serve_parser.add_argument(
        "--host", type=str, default="127.0.0.1",
        help=_t('help___host'),
    )
    serve_parser.add_argument(
        "--port", type=int, default=8787,
        help=_t('help___port'),
    )
    serve_parser.add_argument(
        "--token", type=str, default=None, metavar="TOKEN|auto",
        help=_t('help___token'),
    )
    serve_parser.add_argument(
        "--ready-file", type=str, default=None, metavar="PATH",
        help=_t('help___ready_file'),
    )
    serve_parser.add_argument(
        "--config", type=str, default=None,
        help=_t('help___path'),
    )

    # ── mcp subcommand ───────────────────────────────────────────────────────
    mcp_parser = subparsers.add_parser(
        "mcp", help=_t('cmd_mcp'),
    )
    mcp_parser.add_argument(
        "--config", type=str, default=None,
        help=_t('help___path'),
    )
    mcp_parser.add_argument(
        "--list-tools", action="store_true",
        help=_t('help___list_tools'),
    )

    # Accept --language anywhere (before or after a subcommand). Resolution
    # already happened in _pre_parse_language; these copies just let
    # parse_args tolerate the flag after a subcommand, hidden from --help.
    for _sub in (compress_parser, convert_parser, batch_parser, exif_parser,
                 preset_parser, plugin_parser, watch_parser, dedup_parser,
                 info_parser, rename_parser, check_parser, sheet_parser,
                 cull_parser, select_parser, hdr_parser, blurfaces_parser,
                 hash_parser, gallery_parser, bench_parser, config_parser,
                 serve_parser, mcp_parser, analyze_parser, lr_scan_parser,
                 lr_train_parser, lr_predict_parser, lr_recipes_parser,
                 lr_similar_parser, lr_eval_parser, lr_merge_parser,
                 diff_parser, audit_parser, preview_parser, lens_prof_parser,
                 preset_save, preset_list, preset_load, preset_delete,
                 plugin_install, plugin_uninstall, plugin_info, plugin_fetch,
                 plugin_list, plugin_scaffold, lp_save, lp_delete,
                 config_init, config_show):
        _sub.add_argument("--language", "--lang",
                          choices=["en", "zh", "auto"],
                          default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parsed = parser.parse_args(args)

    # A top-level `photo-s --json info` gets silently undone by the
    # subparser's own `--json` default (argparse re-writes the namespace).
    # The pre-parsed flag restores the user's intent either way.
    if json_flag:
        try:
            parsed.json = True
        except AttributeError:
            pass

    if not parsed.command:
        parser.print_help()
        return 1

    # ── Handle 'watch' command ──────────────────────────────────────────────
    if parsed.command == "watch":
        from .watcher import start_watching

        watch_dir = parsed.directory
        if not os.path.isdir(watch_dir):
            print(f"{_t('msg_dir_not_found')}: {watch_dir}")
            return 1

        # Deleting originals from a long-running watcher is irreversible and
        # easy to leave running unattended — confirm unless -y.
        if getattr(parsed, 'remove_original', False) \
                and not getattr(parsed, 'yes', False):
            print("⚠️  watch --remove-original 会在处理成功后删除原图 "
                  "(will DELETE originals after processing).")
            try:
                confirm = input("   继续请输入 y (type y to continue): ").strip().lower()
            except EOFError:
                confirm = ""
            if confirm not in ("y", "yes"):
                print(_t('msg_cancelled'))
                return 0

        if isinstance(parsed.resize, tuple):
            w, h = parsed.resize
        else:
            w, h = _parse_dimensions(parsed.resize) if parsed.resize else (None, None)
        options = ProcessOptions(
            quality=parsed.quality,
            output_format=parsed.format,
            output_dir=parsed.output_dir,
            max_width=w, max_height=h,
            remove_original=getattr(parsed, 'remove_original', False),
        )
        start_watching(watch_dir, options, recursive=parsed.recursive)
        return 0

    # ── Handle 'dedup' command ──────────────────────────────────────────────
    if parsed.command == "dedup":
        from .dedup import find_duplicates, handle_duplicates

        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        dedup_json = getattr(parsed, 'json', False)
        print(_t("msg_scanning", n=len(files)), file=sys.stderr)
        print(file=sys.stderr)

        def progress_cb(current, total):
            print(f"  {_t('msg_hashing_progress')}: [{current}/{total}]",
                  end="\r", file=sys.stderr)

        dup_groups = find_duplicates(files, threshold=parsed.threshold,
                                     progress_callback=progress_cb)
        print(file=sys.stderr)

        total_dupes = sum(len(paths) - 1 for paths in dup_groups.values())
        savings = sum(os.path.getsize(p)
                      for g in dup_groups.values() for p in g[1:])

        # JSON mode prints ONE document (summary + action result merged).
        # The old flow printed two concatenated documents and any consumer's
        # json.loads() on the output raised.
        json_payload = {
            "count": len(dup_groups),
            "duplicate_count": total_dupes,
            "savings_bytes": savings,
            "groups": [{"hash": h, "paths": ps}
                       for h, ps in dup_groups.items()],
        }

        if parsed.action in ("move", "delete", "keep-sharpest"):
            if parsed.dry_run:
                if not dedup_json:
                    print()
                    print(_t("msg_dry_run"))
                kept, removed = handle_duplicates(dup_groups,
                                                   action=parsed.action,
                                                   dry_run=True)
                json_payload["action"] = parsed.action
                json_payload["dry_run"] = True
                json_payload["kept"] = kept
                json_payload["removed"] = removed
            elif not dedup_json:
                verb = (_t("msg_verb_keep_sharpest")
                        if parsed.action == "keep-sharpest"
                        else (_t("msg_verb_move")
                              if parsed.action == "move" else _t("msg_verb_delete")))
                confirm = input(_t("msg_confirm_dedup", verb=verb,
                                   n=total_dupes)).strip().lower()
                if confirm not in ("y", "yes"):
                    print(_t("msg_cancelled"))
                    return 0
                kept, removed = handle_duplicates(dup_groups,
                                                  action=parsed.action,
                                                  dry_run=False)
            else:
                # JSON callers have no stdin; requesting the action explicitly
                # IS the confirmation (same rule as --remove-original --json).
                kept, removed = handle_duplicates(dup_groups,
                                                  action=parsed.action,
                                                  dry_run=False)
                json_payload["action"] = parsed.action
                json_payload["kept"] = kept
                json_payload["removed"] = removed

        if dedup_json:
            import json
            print(json.dumps(versioned(json_payload),
                             indent=2, ensure_ascii=False))
        else:
            if not dup_groups:
                print(_t("msg_no_dupes"))
            else:
                print(_t("msg_dup_groups", n=len(dup_groups)))
                print()
                for i, (h, paths) in enumerate(dup_groups.items(), 1):
                    print(_t("msg_group_files", i=i, n=len(paths)))
                    for p in paths:
                        print(f"    {'⭐' if p == paths[0] else '📎'} {p} "
                              f"({format_size(os.path.getsize(p))})")
                    print()
                print(_t("msg_dupes_total", n=total_dupes, savings=savings))
            if parsed.action in ("move", "delete", "keep-sharpest") \
                    and not parsed.dry_run:
                _will = {
                    "move": _t("msg_will_move", removed=removed, kept=kept),
                    "delete": _t("msg_will_delete", removed=removed, kept=kept),
                    "keep-sharpest": _t("msg_will_keep", removed=removed, kept=kept),
                }[parsed.action]
                _done = {
                    "move": _t("msg_done_move", removed=removed, kept=kept),
                    "delete": _t("msg_done_delete", removed=removed, kept=kept),
                    "keep-sharpest": _t("msg_done_keep", removed=removed, kept=kept),
                }[parsed.action]
                print(_done if not parsed.dry_run else _will)

        # report mode: exit 1 when duplicates were found — agents can branch on
        # the exit code, consistent with `check` (1 = problems found)
        if parsed.action == "report":
            return 0 if not dup_groups else 1
        return 0

    # ── Handle 'preset' command ─────────────────────────────────────────────
    if parsed.command == "preset":
        from .presets import save_preset, load_preset, list_presets, delete_preset

        if parsed.preset_action == "save":
            opts = _build_process_options(parsed)
            save_preset(parsed.name, opts, parsed.desc)
            n = len(_summarize_preset_options(opts))
            print(f"{_t('msg_preset_saved')}: {parsed.name} "
                  f"({_t('msg_preset_captured', n=n)})")

        elif parsed.preset_action == "list":
            presets = list_presets()
            if presets:
                print(_t("msg_presets_available"))
                for p in presets:
                    print(f"   {p}")
            else:
                print(_t("msg_no_presets"))

        elif parsed.preset_action == "load":
            opts = load_preset(parsed.name)
            if opts:
                print(_t("msg_preset_show", name=parsed.name))
                for c in _summarize_preset_options(opts):
                    print(f"   {c}")
                print(_t("msg_preset_apply_hint", name=parsed.name))
            else:
                print(f"{_t('msg_preset_not_found')}: {parsed.name}")
                return 1

        elif parsed.preset_action == "delete":
            if delete_preset(parsed.name):
                print(f"{_t('msg_preset_deleted')}: {parsed.name}")
            else:
                print(f"{_t('msg_preset_not_found')}: {parsed.name}")
                return 1

        else:
            preset_parser.print_help()
            return 1

        return 0

    # ── Handle 'lens-profile' command (user-maintained lens registry) ───────
    if parsed.command == "lens-profile":
        from .lensprofile import (save_lens_profile, load_lens_profile,
                                  list_lens_profiles, delete_lens_profile)
        is_json = getattr(parsed, 'json', False)

        if parsed.lens_profile_action == "save":
            prof = save_lens_profile(
                parsed.name, distort=parsed.distort, vignette=parsed.vignette,
                ca=parsed.ca, description=parsed.desc)
            if is_json:
                import json
                print(json.dumps(versioned(
                    {"ok": True, "name": parsed.name, "profile": prof}),
                    indent=2, ensure_ascii=False))
            else:
                print(f"{_t('msg_lens_profile_saved')}: {parsed.name} "
                      f"({len(prof)} 参数)")

        elif parsed.lens_profile_action == "list":
            profs = list_lens_profiles()
            if is_json:
                import json
                print(json.dumps(versioned({"profiles": profs}),
                                 indent=2, ensure_ascii=False))
            elif profs:
                print(_t("msg_lens_profiles_available"))
                for p in profs:
                    print(f"   {p}")
            else:
                print(_t("msg_no_lens_profiles"))

        elif parsed.lens_profile_action == "delete":
            if delete_lens_profile(parsed.name):
                print(f"{_t('msg_lens_profile_deleted')}: {parsed.name}")
            else:
                print(f"{_t('msg_lens_profile_not_found')}: {parsed.name}")
                return 1

        else:
            lens_prof_parser.print_help()
            return 1

        return 0

    # ── Handle 'plugin' command ──────────────────────────────────────────────
    if parsed.command == "plugin":
        from .plugincmd import run as _plugin_run
        return _plugin_run(parsed)

    # ── Handle 'exif' command ────────────────────────────────────────────────
    if parsed.command == "exif":
        from .engine import apply_exif_tags, read_exif_metadata
        exif_json = getattr(parsed, 'json', False)

        _TAG_FLAGS = [("artist", "artist"), ("copyright", "copyright"),
                      ("description", "description"), ("caption", "caption"),
                      ("title", "title"), ("date", "date"),
                      ("software", "software"), ("rating", "rating"),
                      ("keywords", "keywords"), ("lens", "lens"),
                      ("iso", "iso"), ("shutter", "shutter"),
                      ("aperture", "aperture"), ("focal", "focal"),
                      ("make", "make"), ("model", "model"), ("gps", "gps")]

        def _apply_batch_meta(rows, source):
            """Write metadata for a list of {path, tag...} dicts."""
            tags_cols = [n for _, n in _TAG_FLAGS]
            done = 0
            failed = 0
            for row in rows:
                p = (row.get("path") or "").strip()
                if not p:
                    continue
                if not os.path.isabs(p):
                    p = os.path.abspath(p)
                tags = {k: row.get(k) for k in tags_cols
                        if row.get(k) not in (None, "")}
                if not tags:
                    continue
                try:
                    apply_exif_tags(p, tags)
                except Exception as e:  # per-file: keep going, report at end
                    failed += 1
                    print(f"  ❌ {os.path.basename(p)}: {e}", file=sys.stderr)
                    continue
                done += 1
            return done, failed

        # ── Batch import from CSV / JSON (paths come from the file) ──
        if getattr(parsed, 'from_csv', None):
            import csv
            with open(parsed.from_csv, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            done, failed = _apply_batch_meta(rows, parsed.from_csv)
            print(f"{_t('msg_written_csv')}: {done} {_t('msg_files_suffix')}")
            if failed:
                print(_t("msg_write_failed", n=failed), file=sys.stderr)
            return 0 if failed == 0 else 1
        if getattr(parsed, 'from_json', None):
            import json
            rows = json.loads(Path(parsed.from_json).read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = [rows]
            done, failed = _apply_batch_meta(rows, parsed.from_json)
            print(f"{_t('msg_written_json')}: {done} {_t('msg_files_suffix')}")
            if failed:
                print(_t("msg_write_failed", n=failed), file=sys.stderr)
            return 0 if failed == 0 else 1

        # ── Read / filter mode ──
        if getattr(parsed, 'show', False):
            files = _collect_files(parsed.files,
                                   recursive=getattr(parsed, 'recursive', False))
            if not files:
                return _no_files_exit(parsed)

            rating_min = parsed.rating_min
            rating_exact = parsed.rating          # --rating = exact filter in --show
            kw_wanted = {k.strip() for k in (parsed.keywords or "").split(",")
                         if k.strip()}
            camera = (parsed.camera or "").lower()
            d_from, d_to = parsed.date_from, parsed.date_to

            results = []
            for f in files:
                m = read_exif_metadata(f)
                if rating_min is not None and (m["rating"] or 0) < rating_min:
                    continue
                if rating_exact is not None and m["rating"] != rating_exact:
                    continue
                if kw_wanted and not (kw_wanted & set(m["keywords"])):
                    continue
                if camera and camera not in (m["camera"] or "").lower():
                    continue
                if d_from and m["date"] and m["date"] < d_from:
                    continue
                if d_to and m["date"] and m["date"] > d_to:
                    continue
                results.append({"path": f, **m})

            if getattr(parsed, 'list', False):
                for r in results:
                    print(r["path"])
            elif exif_json:
                import json
                print(json.dumps(versioned({"count": len(results), "results": results}),
                                 indent=2, ensure_ascii=False))
            else:
                for r in results:
                    print(f"  {r['path']}")
                    print(f"     {_t('msg_date_label')}: {r['date']} {r['time']}"
                          f" | camera: {r['camera'] or '-'}"
                          f" | ISO: {r['iso'] or '-'} | focal: {r['focal'] or '-'}")
                    print(f"     {_t('msg_rating_label')}: {r['rating']}"
                          f" | keywords: {', '.join(r['keywords']) or '-'}"
                          f" | title: {r['title'] or '-'}"
                          f" | caption: {r['caption'] or '-'}")
            return 0

        # ── Write mode: collect tags from flags ──
        tags = {}
        for flag, name in _TAG_FLAGS:
            val = getattr(parsed, flag, None)
            if val is not None:
                tags[name] = val

        if getattr(parsed, 'date_from_mtime', False):
            # reverse sync: DateTimeOriginal ← file mtime (per file)
            failed = 0
            for f in _collect_files(parsed.files,
                                    recursive=getattr(parsed, 'recursive', False)):
                try:
                    ts = os.path.getmtime(f)
                except OSError:
                    continue
                dt = datetime.fromtimestamp(ts)
                try:
                    apply_exif_tags(f, {"date": dt.strftime("%Y:%m:%d %H:%M:%S")})
                except Exception as e:  # per-file: keep going, report at end
                    failed += 1
                    print(f"  ❌ {os.path.basename(f)}: {e}", file=sys.stderr)
                    continue
            print(_t("msg_synced_date"))
            if failed:
                print(_t("msg_write_failed", n=failed), file=sys.stderr)
            return 0 if failed == 0 else 1

        if not tags:
            print(_t("msg_no_exif_tags"))
            return 1

        files = _collect_files(parsed.files,
                               recursive=getattr(parsed, 'recursive', False))
        if not files:
            return _no_files_exit(parsed)

        jout = sys.stderr if exif_json else sys.stdout
        print(f"{_t('msg_editing_exif')} ({len(files)} {_t('msg_files_suffix')})...",
              file=jout)
        for tag, val in tags.items():
            print(f"   {tag}: {val}", file=jout)
        print(file=jout)

        write_results = []
        failed = 0
        for f in files:
            try:
                msg = apply_exif_tags(f, tags)
            except Exception as e:  # per-file: keep going, report at end
                failed += 1
                print(f"  ❌ {os.path.basename(f)}: {e}", file=jout)
                write_results.append({"path": f, "ok": False, "error": str(e)})
                continue
            print(f"  {os.path.basename(f)}: {msg}", file=jout)
            write_results.append({"path": f, "ok": True, "message": msg})

        if exif_json:
            # write mode used to print pure human text and ignore --json
            import json
            print(json.dumps(versioned({
                "count": len(files),
                "ok": len(files) - failed,
                "failed": failed,
                "results": write_results,
            }), indent=2, ensure_ascii=False))
        else:
            print(f"\n{_t('msg_exif_done')}")
        if failed:
            print(_t("msg_write_failed", n=failed), file=sys.stderr)
        return 0 if failed == 0 else 1

    # ── Handle 'info' command ────────────────────────────────────────────────
    if parsed.command == "info":
        from .engine import _HAS_PILLOW_HEIF
        from .envinfo import optional_features, plugins

        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({
                "version": __version__,
                "input_extensions": sorted(ALL_INPUT_EXTENSIONS),
                "formats": sorted(SUPPORTED_FORMATS),
                "writable": sorted(PIL_WRITABLE),
                "optional_features": optional_features(),
                "plugins": plugins(),
            }), indent=2, ensure_ascii=False))
            return 0
        print(_t("msg_formats_title"))
        print("=" * 50)
        print()
        print(_t("msg_input_formats"))
        for ext in sorted(ALL_INPUT_EXTENSIONS):
            print(f"  {ext}")
        print()
        print(_t("msg_output_formats"))
        for fmt, info in SUPPORTED_FORMATS.items():
            if fmt in PIL_WRITABLE:
                pil = "✅"
            elif fmt == "HEIC":
                pil = "🔄 (via sips, macOS only)"
            else:
                pil = "❌"
            print(f"  {fmt}  {info['ext']}  {pil}")
        if not _HAS_PILLOW_HEIF:
            print()
            print(_t("msg_heic_hint"))
            print("   pip install pillow-heif")
        print()
        print(_t("msg_optional_features"))
        for name, installed in optional_features().items():
            print(f"  {'✅' if installed else '·'} {name}")
        plugins = plugins()
        print()
        print(_t("msg_installed_plugins"))
        if plugins:
            for p in plugins:
                provides = ", ".join(p["provides"]) or "-"
                print(f"  {p['name']}  [{provides}]")
        else:
            print(f"  {_t('msg_none')}")
        return 0

    # ── Handle 'config' command ─────────────────────────────────────────────
    if parsed.command == "config":
        from .config import (find_config, load_config, default_config_text,
                             save_config, apply_config)
        if parsed.config_action == "init":
            path = parsed.path or "photo-s.toml"
            if os.path.exists(path) and not getattr(parsed, "force", False):
                # Never clobber a live config silently — it may carry
                # carefully tuned options (and auto-discovery makes it
                # load-bearing for every run in that directory tree).
                print(f"❌ {_t('msg_config_load_err')}: "
                      f"{os.path.abspath(path)} already exists "
                      "(use --force to overwrite)")
                return 1
            save_config(path, default_config_text())
            print(f"{_t('msg_config_created')}: {os.path.abspath(path)}")
            print(f"   {_t('msg_config_hint')}")
            return 0
        elif parsed.config_action == "show":
            path = parsed.path or find_config()
            if not path:
                print(_t("msg_no_config"))
                print(f"   {_t('msg_config_init_hint')}")
                return 0
            try:
                cfg = load_config(path)
                apply_config(cfg, ProcessOptions())
            except Exception as e:
                print(f"{_t('msg_config_load_err')}: {e}")
                return 1
            print(f"{_t('msg_config_file')} {path}")
            opts_dict = cfg.get("options", {})
            if not opts_dict:
                print(f"   {_t('msg_no_custom_opts')}")
            for key, value in opts_dict.items():
                print(f"   {key} = {value}")
            return 0
        else:
            config_parser.print_help()
            return 1

    # ── Handle 'serve' command ──────────────────────────────────────────────
    if parsed.command == "serve":
        from .server import run_server, generate_token
        base_options = ProcessOptions()
        if parsed.config:
            from .config import load_config, apply_config
            try:
                cfg = load_config(parsed.config)
                base_options = apply_config(cfg, base_options)
            except Exception as e:
                print(f"{_t('msg_config_load_err')}: {e}")
                return 1
        token = parsed.token
        if token == "auto":
            token = generate_token()
            print(f"{_t('msg_token_generated')}: {token}", file=sys.stderr)
        run_server(parsed.host, parsed.port, options=base_options,
                   token=token, ready_file=parsed.ready_file)
        return 0

    # ── Handle 'mcp' command ─────────────────────────────────────────────────
    if parsed.command == "mcp":
        # The mcp SDK requires Python >= 3.10 — check before any import so
        # py3.9 gets a clear message instead of an ImportError.
        if sys.version_info < (3, 10):
            print(_t("msg_mcp_py310"), file=sys.stderr)
            return 1
        try:
            if parsed.list_tools:
                import json
                from .mcp_server import list_tools_json
                print(json.dumps(versioned({"tools": list_tools_json()}),
                                 indent=2, ensure_ascii=False))
                return 0
            from .mcp_server import run_stdio
            run_stdio(config_path=parsed.config)
        except RuntimeError as e:
            # missing mcp extra / bad config
            print(f"❌ {e}", file=sys.stderr)
            return 1
        return 0

    # ── Handle 'rename' command ─────────────────────────────────────────────
    if parsed.command == "rename":
        from .rename import rename_files

        files = _collect_files(parsed.files, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        rename_json = getattr(parsed, 'json', False)
        if rename_json:
            pass  # machine output only — no human preamble on stdout
        elif parsed.dry_run:
            print(_t("msg_dry_run_settings"))
        elif parsed.output_dir:
            print(f"{_t('msg_copy_to')}: {parsed.output_dir}")
        else:
            print(_t("msg_rename_in_place"))
        if not rename_json:
            print()

        results = rename_files(
            files, parsed.pattern,
            output_dir=parsed.output_dir,
            overwrite=parsed.overwrite,
            dry_run=parsed.dry_run,
        )
        ok = sum(1 for r in results if r["status"] == "ok")
        if rename_json:
            import json
            print(json.dumps(versioned({"total": len(results), "ok": ok,
                              "results": results}),
                             indent=2, ensure_ascii=False))
        else:
            for r in results:
                extra = f": {r['error']}" if r["error"] else ""
                print(f"  {'→' if r['status'] == 'ok' else '❌'} "
                      f"{os.path.basename(r['input'])} → "
                      f"{os.path.basename(r['output'])}{extra}")
            print()
            print(_t("msg_rename_ok", ok=ok, total=len(results)))
            if not parsed.dry_run and not parsed.output_dir:
                print(f"   {_t('msg_rename_note')}")
        return 0 if ok == len(results) else 1

    # ── Handle 'check' command ──────────────────────────────────────────────
    if parsed.command == "check":
        from .check import verify_images

        files = _collect_files(parsed.files, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        results = verify_images(files)
        corrupt = [r for r in results if not r["ok"]]

        if parsed.json:
            import json
            print(json.dumps(versioned({
                "checked": len(results),
                "ok": len(results) - len(corrupt),
                "corrupt": [{"path": r["path"], "error": r["error"]}
                            for r in corrupt],
            }), indent=2, ensure_ascii=False))
        else:
            for r in results:
                mark = "✅" if r["ok"] else "❌"
                extra = "" if r["ok"] else f" — {r['error']}"
                print(f"  {mark} {r['path']}{extra}")
            print(f"\n{_t('msg_check_done')}: {len(results)} {_t('msg_files_suffix')}, "
                  f"{len(corrupt)} {_t('msg_check_corrupt')}")

        return 0 if not corrupt else 1

    # ── Handle 'contact-sheet' command ──────────────────────────────────────
    if parsed.command == "contact-sheet":
        from .contact import build_contact_sheet
        from .adjust import hex_to_rgb

        files = _collect_files(parsed.files, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        tw, th = parsed.thumb if isinstance(parsed.thumb, tuple) else (
            _parse_dimensions(parsed.thumb) if parsed.thumb else (240, 240))
        try:
            bg = hex_to_rgb(parsed.bg)
        except ValueError:
            bg = (0, 0, 0)
        out = build_contact_sheet(
            files, parsed.output, cols=parsed.cols,
            thumb_size=(tw or 240, th or 240),
            captions=parsed.caption, bg=bg,
        )
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({"output": out, "count": len(files)}),
                             indent=2, ensure_ascii=False))
        else:
            print(f"{_t('msg_contact_sheet')}: {out}  "
                  f"({_t('msg_images_count', n=len(files))})")
        return 0

    # ── Handle 'cull' command ───────────────────────────────────────────────
    if parsed.command == "cull":
        from .cull import cull_files
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        # v2.3 ranking mode: scores rank (never reject), bursts keep one
        if getattr(parsed, 'score', False) or getattr(parsed, 'burst', False):
            import json as _json
            from .cull import best_of_bursts, score_files
            gap = max(0.1, float(parsed.gap or 2.0))
            if getattr(parsed, 'burst', False):
                rows = best_of_bursts(files, gap_seconds=gap)
            else:
                rows = score_files(files)
            if getattr(parsed, 'list', False):
                # --list: machine-friendly keeper candidates, one per line —
                # burst bests when --burst, the full ranking otherwise
                for r in rows:
                    if getattr(parsed, 'burst', False) and not r.get("burst_best"):
                        continue
                    print(r["path"])
            elif getattr(parsed, 'json', False):
                print(_json.dumps(versioned({"count": len(rows), "results": rows}),
                                  indent=2, ensure_ascii=False))
            else:
                for r in rows:
                    star = " ★" if r.get("burst_best") else ""
                    print(f"  {r.get('score', 0):5.1f}  {r['path']}{star}")
                best_n = sum(1 for r in rows if r.get("burst_best"))
                if best_n:
                    print(f"\n{_t('msg_cull_burst_best')}: {best_n}")
            return 0

        results = cull_files(
            files,
            overexposed_max=parsed.overexposed_max,
            underexposed_max=parsed.underexposed_max,
            luminance_min=parsed.luminance_min,
            luminance_max=parsed.luminance_max,
            sharpness_min=parsed.sharpness_min,
        )

        kept_paths = [r["path"] for r in results if r["kept"]]
        if getattr(parsed, 'list', False):
            for p in kept_paths:
                print(p)
        elif getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({"count": len(results), "kept": len(kept_paths),
                              "results": results}),
                             indent=2, ensure_ascii=False))
        else:
            for r in results:
                mark = "✅" if r["kept"] else "❌"
                extra = (f"  sharp={r['blur_score']}"
                         if "blur_score" in r else "")
                print(f"  {mark} {r['path']}  lum={r['luminance']} "
                      f"over={r['overexposed_pct']}% "
                      f"under={r['underexposed_pct']}%{extra}")
            print(f"\n{_t('msg_cull_kept')}: {len(kept_paths)}/{len(results)}")
        return 0

    # ── Handle 'select' command (keeper workflow by rating) ────────────────
    if parsed.command == "select":
        from .select import select_files, _rated_paths
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)

        mode = "copy" if parsed.copy else "move"
        # Moving files off their source directory is destructive; classify
        # first (no writes) so the confirmation happens BEFORE any move.
        # Interactive sessions get a prompt unless -y/--copy/--dry-run.
        if mode == "move" and not parsed.dry_run \
                and not getattr(parsed, 'yes', False) and sys.stdin.isatty():
            pre = _rated_paths(files, parsed.keep_min, parsed.reject_max,
                               parsed.selects_dir, parsed.rejects_dir,
                               mode, True)
            n_move = sum(1 for r in pre if r["action"] == "move")
            if n_move:
                print(f"⚠️  select 将移动 {n_move} 个文件（默认 move，原位置不保留）。"
                      f" (will MOVE {n_move} files; originals leave the source dir)")
                try:
                    confirm = input("   继续请输入 y (type y to continue): ").strip().lower()
                except EOFError:
                    confirm = ""
                if confirm not in ("y", "yes"):
                    print(_t('msg_cancelled'))
                    return 0

        try:
            results = select_files(
                files,
                keep_min=parsed.keep_min,
                reject_max=parsed.reject_max,
                selects_dir=parsed.selects_dir,
                rejects_dir=parsed.rejects_dir,
                mode=mode,
                dry_run=parsed.dry_run,
            )
        except ValueError as e:  # keep_min <= reject_max
            print(f"❌ {e}", file=sys.stderr)
            return 1

        moved = [r for r in results if r["action"] in
                 ("move", "copy", "would_move", "would_copy")]
        kept = [r for r in results if r["status"] == "keep"]
        rejected = [r for r in results if r["status"] == "reject"]
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({
                "count": len(results),
                "kept": len(kept), "rejected": len(rejected),
                "moved": len(moved), "dry_run": parsed.dry_run,
                "results": results,
            }), indent=2, ensure_ascii=False))
        else:
            for r in results:
                if r["status"] == "skip":
                    mark = "⏸"
                elif r["action"].startswith("would"):
                    mark = "🔮"
                else:
                    mark = "📦"
                print(f"  {mark} [{r['status']}] {r['path']}"
                      f"  rating={r['rating']}"
                      + (f"  → {r['dest']}" if r["dest"] else ""))
            summary = _t('msg_select_summary', kept=len(kept),
                         rejected=len(rejected), moved=len(moved))
            print(f"\n{summary}")
        return 0

    # ── Handle 'hdr' command (exposure fusion) ─────────────────────────────
    if parsed.command == "hdr":
        from .hdr import merge_hdr
        out = getattr(parsed, 'output', None) or "hdr.jpg"
        try:
            result = merge_hdr(list(parsed.paths), align=parsed.align)
            result.save(out, quality=95)
        except RuntimeError as e:  # opencv missing — clear install hint
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except ValueError as e:    # <2 files / unreadable
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({
                "ok": True,
                "output": os.path.abspath(out),
                "count": len(parsed.paths),
                "align": parsed.align,
                "dims": list(result.size),
            }), indent=2, ensure_ascii=False))
        else:
            print(f"{_t('msg_hdr_done', n=len(parsed.paths), out=out)}"
                  f"  ({result.size[0]}×{result.size[1]})")
        return 0

    # ── Handle 'blurfaces' command (privacy masking) ───────────────────────
    if parsed.command == "blurfaces":
        # module-qualified: a plain from-import here would shadow the shared
        # handler's `batch_process` local for the rest of run_cli
        from . import engine as _engine
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        opts = _engine.ProcessOptions(
            output_dir=getattr(parsed, 'output_dir', None),
            blur_faces=parsed.mode,
            blur_faces_margin=parsed.margin,
            suffix="_blurred",
            preserve_exif=True,
            overwrite=getattr(parsed, 'overwrite', False),
        )
        batch = _engine.batch_process(files, opts)
        results = batch.results
        ok = [r for r in results if r.success]
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({
                "count": len(results), "ok": len(ok),
                "results": [r.to_dict() for r in results],
            }), indent=2, ensure_ascii=False))
        else:
            for r in results:
                if r.success:
                    print(f"  ✅ {r.output_path}")
                else:
                    print(f"  ❌ {r.input_path}: {r.error}", file=sys.stderr)
            print(f"\n{_t('msg_blurfaces_done', ok=len(ok), n=len(results))}")
        return 0 if len(ok) == len(results) else 1

    # ── Handle 'hash' command ───────────────────────────────────────────────
    if parsed.command == "hash":
        from .check import (collect_files, compute_checksums, write_manifest,
                            verify_manifest)
        import json

        if parsed.verify:
            report = verify_manifest(parsed.verify)
            if parsed.json:
                print(json.dumps(versioned(report), indent=2, ensure_ascii=False))
            else:
                print(f"{_t('msg_manifest')}: {parsed.verify}  ({report['algorithm']})")
                print(_t("msg_total_ok", total=report["total"], ok=report["ok"]))
                if report["missing"]:
                    print(f"  {_t('msg_missing')} ({len(report['missing'])}):")
                    for p in report["missing"]:
                        print(f"    {p}")
                if report["mismatched"]:
                    print(f"  {_t('msg_mismatched')} ({len(report['mismatched'])}):")
                    for m in report["mismatched"]:
                        print(f"    {m['path']}")
                        print(f"     {_t('msg_expected')}: {m['expected']}")
                        print(f"     {_t('msg_actual')}:   {m['actual']}")
            return 0 if (not report["missing"] and not report["mismatched"]) else 1

        # Hash every file (not just images — for archives)
        files = collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed, msg_key="msg_no_files")

        print(_t("msg_hashing_start", n=len(files)), file=sys.stderr)
        entries = compute_checksums(files)
        out = parsed.output or "manifest.csv"
        write_manifest(out, entries)
        if parsed.json:
            print(json.dumps(versioned({"output": os.path.abspath(out),
                              "count": len(files)}), indent=2, ensure_ascii=False))
        else:
            print(f"{_t('msg_manifest_written')}: {os.path.abspath(out)} "
                  f"({len(files)} 项)")
        return 0

    # ── Handle 'bench' command ───────────────────────────────────────────────
    if parsed.command == "analyze":
        from .metrics import analyze_image
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        results = [analyze_image(p, sample_size=parsed.sample_size,
                                 grid=getattr(parsed, 'grid', 0))
                   for p in files]
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned({"count": len(results),
                                        "results": results}),
                             indent=2, ensure_ascii=False))
        else:
            for r in results:
                if not r.get("ok"):
                    print(f"  ❌ {r['path']}  {r.get('error', '')}")
                    continue
                s, wb, ex = r["stats"], r["white_balance"], r["exposure"]
                print(f"  {r['path']}  {r['size'][0]}x{r['size'][1]}"
                      f"  lum={ex['luminance']}"
                      f"  contrast={s['contrast']}"
                      f"  sat={s['saturation_mean']}"
                      f"  kelvin~{wb['kelvin_estimate']}"
                      f"  over={ex['overexposed_pct']}%"
                      f"  under={ex['underexposed_pct']}%")
        # unreadable inputs must surface in the exit code (agent termination
        # condition per the JSON contract)
        return 0 if all(r.get("ok") for r in results) else 1

    if parsed.command == "lr-scan":
        from .lrxmp import render_before_images, scan_and_report, write_export
        import json
        import time
        t0 = time.monotonic()
        report, records = scan_and_report(parsed.paths)
        export_path = None
        render_path = None
        images = {}
        if getattr(parsed, 'render_dir', None):
            render_path = parsed.render_dir
            r = render_before_images(records, render_path)
            images = r["images"]
        if getattr(parsed, 'export_dir', None):
            export_path = write_export(records, parsed.export_dir, images=images,
                                       sanitize=bool(getattr(parsed, 'sanitize', False)))
        if getattr(parsed, 'json', False):
            if render_path:
                report["summary"]["rendered"] = r["rendered"]
                report["summary"]["render_failed"] = r["failed"]
            print(json.dumps(versioned(report), indent=2, ensure_ascii=False))
        else:
            s = report["summary"]
            print(_t("msg_lr_header").format(sec=round(time.monotonic() - t0, 1)))
            print(_t("msg_lr_catalogs"))
            for c in report["catalogs"]:
                print(f"  {c['name']:<10}{c['photos']:>6}{c['edited']:>7}"
                      f"{c['v1_8']:>6}{c['point_color']:>7}")
            print(_t("msg_lr_summary").format(
                photos=s["photos"], xmp=s["xmp_photos"], edited=s["edited"]))
            if render_path:
                print(_t("msg_lr_rendered").format(
                    n=r["rendered"], skip=r["skipped"], failed=r["failed"]))
            print(_t("msg_lr_params"))
            for k, v in list(report["param_usage"].items())[:15]:
                print(f"  {k:<30} {v:>4}")
            if report["mask_kinds"]:
                print(_t("msg_lr_masks"))
                for k, v in report["mask_kinds"].items():
                    print(f"  {k:<10} {v:>4}")
            print(_t("msg_lr_tools"))
            for k, v in list(report["tool_usage"].items())[:12]:
                print(f"  {k:<26} {v:>5}")
            if report["unmapped"]:
                print(_t("msg_lr_unmapped"))
                for k, v in report["unmapped"].items():
                    print(f"  {k:<28} {v:>4}")
            if export_path:
                print(_t("msg_lr_export").format(path=export_path))
        return 0

    if parsed.command == "lr-merge":
        import json
        from .lrxmp import merge_packages
        try:
            res = merge_packages(parsed.packages, parsed.out)
        except OSError as e:
            print(f"❌ {e}")
            return 1
        if getattr(parsed, 'json', False):
            print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
        else:
            print(_t("msg_lr_merged").format(
                p=res["packages"], n=res["records"], e=res["edited"],
                img=res["images_copied"], out=os.path.abspath(parsed.out)))
            for d in res["duplicates"][:10]:
                print(f"  重复跳过: {d}")
            for c in res["conflicts"]:
                print(f"  ⚠ {c}")
        return 0

    if parsed.command in ("lr-train", "lr-predict", "lr-recipes",
                          "lr-similar", "lr-eval"):
        import json
        from .lrxmp import (cluster_recipes, load_training_data,
                            predict_auto_tone, prep_eval_set,
                            similar_photos, train_auto_tone)
        import photo_s.lrxmp as _lrx

        def _load_records():
            with open(parsed.data, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

        if parsed.command == "lr-train":
            recs = _load_records()
            try:
                res = train_auto_tone(recs, parsed.out,
                                      image_dir=parsed.images,
                                      ridge_lambda=parsed.ridge_lambda)
            except _lrx.LrError as e:
                print(f"❌ {e}")
                return 1
            print(_t("msg_lr_trained").format(
                n=res["samples"], r2=res["r2"], out=res["out"]))

        elif parsed.command == "lr-predict":
            try:
                res = predict_auto_tone(parsed.image, parsed.model)
            except _lrx.LrError as e:
                print(f"❌ {e}")
                return 1
            if getattr(parsed, 'json', False):
                print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
            else:
                print(f"{parsed.image}")
                for k, v in res["options"].items():
                    print(f"  {k:<12} {v}")

        elif parsed.command == "lr-recipes":
            recs = _load_records()
            try:
                res = cluster_recipes(recs, k=parsed.k)
            except _lrx.LrError as e:
                print(f"❌ {e}")
                return 1
            if getattr(parsed, 'json', False):
                print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
            else:
                print(_t("msg_lr_recipes").format(k=res["k"], n=res["samples"]))
                for i, c in enumerate(res["clusters"], 1):
                    opts = ",".join(f"{k}={v}" for k, v in c["options"].items()
                                    if v not in (0, 1.0, 5250))
                    print(f"  #{i} [{c['ratio'] * 100:.0f}%] {c['size']} 张  {opts}")
                    if c["examples"][0]:
                        print(f"      例: {c['examples'][0]}")

        elif parsed.command == "lr-similar":
            recs = _load_records()
            try:
                hits = similar_photos(parsed.image, recs,
                                      image_dir=parsed.images, k=parsed.k)
            except _lrx.LrError as e:
                print(f"❌ {e}")
                return 1
            if getattr(parsed, 'json', False):
                print(json.dumps(versioned({"query": parsed.image,
                                            "hits": hits}), indent=2,
                       ensure_ascii=False))
            else:
                print(_t("msg_lr_similar").format(
                    img=parsed.image, k=len(hits)))
                for h in hits:
                    opts = ",".join(f"{k}={v}"
                                    for k, v in (h.get("options") or {}).items())
                    print(f"  d={h['distance']:.3f}  {h['path']}  {opts}")

        else:  # lr-eval
            recs = _load_records()
            try:
                res = prep_eval_set(recs, parsed.out,
                                    image_dir=parsed.images,
                                    sample=parsed.sample)
            except _lrx.LrError as e:
                print(f"❌ {e}")
                return 1
            print(_t("msg_lr_eval").format(n=res["count"], out=res["out"]))
        return 0

    if parsed.command == "diff":
        import json
        from .metrics import compare_images
        res = compare_images(parsed.paths[0], parsed.paths[1])
        if getattr(parsed, 'json', False):
            print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
        elif res.get("ok"):
            print(f"{res['path_a']}  vs  {res['path_b']}")
            print(f"  PSNR={res['psnr']}dB  SSIM={res['ssim']}  "
                  f"MAD={res['mean_abs_diff']}")
        else:
            print(f"❌ {res.get('error', '')}")
        return 0 if res.get("ok") else 1

    if parsed.command == "audit":
        import json
        from .audit import audit_image
        from . import engine as _engine
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        verifier = None
        if parsed.aesthetic is not None:
            from .plugin import find_provider
            verifier = find_provider("verify")
        results = [audit_image(p, overexposed_max=parsed.over_max,
                               underexposed_max=parsed.under_max,
                               blur_min=parsed.blur_min,
                               aesthetic=parsed.aesthetic,
                               verifier=verifier)
                   for p in files]
        if getattr(parsed, 'json', False):
            print(json.dumps(versioned({"count": len(results),
                                        "passed": sum(1 for r in results
                                                      if r.get("passed")),
                                        "results": results}),
                             indent=2, ensure_ascii=False))
        else:
            for r in results:
                mark = "✅" if r.get("passed") else "❌"
                print(f"  {mark} {r['path']}  {r.get('reason', r.get('error', ''))}")
        # agents branch on the exit code ("termination condition" per the
        # JSON contract) — a failed audit must not exit 0
        return 0 if all(r.get("passed") for r in results) else 1

    if parsed.command == "suggest":
        import json
        from .suggest import suggest_file
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        scale = max(0.0, min(1.0, float(parsed.scale or 1.0)))
        results = [suggest_file(p, scale=scale) for p in files]
        if getattr(parsed, 'json', False):
            print(json.dumps(versioned({
                "count": len(results),
                "neutral": sum(1 for r in results if r.get("neutral")),
                "results": results}), indent=2, ensure_ascii=False))
        else:
            for r in results:
                if not r.get("ok"):
                    print(f"  ❌ {r['path']}  {r.get('error', '')}")
                    continue
                if r["neutral"]:
                    print(f"  ✔ {r['path']}  {_t('suggest_neutral')}")
                    continue
                fields = "  ".join(
                    "{}={}".format(k, v)
                    for k, v in sorted(r["suggested"].items()))
                print(f"  💡 {r['path']}  {fields}")
                for rs in r["reasons"]:
                    print(f"      · {rs['advice']}")
        return 0

    if parsed.command == "xmp-export":
        import json as _json
        from dataclasses import replace as _dc_replace
        from .lrxmp import write_xmp_sidecar
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        base = ProcessOptions()
        if parsed.preset:
            from .presets import load_preset
            preset_opts = load_preset(parsed.preset)
            if preset_opts is None:
                print(f"{_t('msg_preset_not_found')}: {parsed.preset}",
                      file=sys.stderr)
                return 2
            base = preset_opts
        if parsed.options:
            try:
                extra = _json.loads(parsed.options)
            except ValueError as e:
                print(f"❌ --options: {e}", file=sys.stderr)
                return 2
            if not isinstance(extra, dict):
                print("❌ --options must be a JSON object", file=sys.stderr)
                return 2
            unknown = [k for k in extra if k not in
                       ProcessOptions.__dataclass_fields__]
            if unknown:
                print(f"❌ --options unknown fields: {unknown}",
                      file=sys.stderr)
                return 2
            base = _dc_replace(base, **extra)
        if parsed.auto_tone is not None:
            base.auto_tone = parsed.auto_tone
        keywords = [k.strip() for k in (parsed.keywords or "").split(",")
                    if k.strip()] or None
        if not (parsed.preset or parsed.options or parsed.auto_tone is not None
                or parsed.rating is not None or keywords or parsed.title):
            print(f"❌ {_t('msg_xmp_no_source')}", file=sys.stderr)
            return 2
        rows = []
        fail = 0
        for p in files:
            opts = base
            params_summary = None
            if base.auto_tone:
                from .autotone import resolve_auto_tone_options
                try:
                    opts, params = resolve_auto_tone_options(base, p)
                    params_summary = params.get("options")
                except (RuntimeError, ValueError) as e:
                    print(f"  ❌ {p}  {e}", file=sys.stderr)
                    fail += 1
                    continue
            try:
                sidecar, warns = write_xmp_sidecar(
                    p, opts, rating=parsed.rating, keywords=keywords,
                    title=parsed.title, out_dir=parsed.out_dir)
            except Exception as e:
                print(f"  ❌ {p}  {e}", file=sys.stderr)
                fail += 1
                continue
            rows.append({"path": p, "sidecar": sidecar,
                         "warnings": warns, "auto_tone_params": params_summary})
            if not getattr(parsed, 'json', False):
                note = f"  ⚠️ {'; '.join(warns)}" if warns else ""
                print(f"  📄 {sidecar}{note}")
        if getattr(parsed, 'json', False):
            print(_json.dumps(versioned({"count": len(rows), "failed": fail,
                                         "results": rows}),
                              indent=2, ensure_ascii=False))
        return 0 if fail == 0 else 1

    if parsed.command == "autopilot":
        from .autopilot import AutopilotConfig, run_autopilot, validate_config
        cfg = AutopilotConfig(
            watch_dir=os.path.abspath(parsed.dir),
            out_dir=os.path.abspath(parsed.out_dir)
            if getattr(parsed, 'out_dir', None) else None,
            mode=parsed.mode,
            auto_tone_strength=parsed.auto_tone,
            scale=parsed.scale,
            thresholds={k: getattr(parsed, k) for k in
                        ("over_max", "under_max", "blur_min")
                        if hasattr(parsed, k)},
            aesthetic=parsed.aesthetic,
            write_xmp=getattr(parsed, 'write_xmp', False),
            recursive=parsed.recursive,
            scan_existing=getattr(parsed, 'scan_existing', False),
            log_path=os.path.abspath(parsed.log) if parsed.log else None,
            quality=parsed.quality,
            output_format=getattr(parsed, 'format', None),
            resize=parsed.resize,
        )
        try:
            validate_config(cfg)
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2

        def on_event(rec):
            mark = "✅" if rec.get("routed") and "passed" in rec["routed"] \
                else ("❌" if rec.get("error") else "👀")
            name = os.path.basename(rec.get("input") or "?")
            detail = rec.get("error") or rec.get("audit", {}).get("reason", "")
            print(f"  {mark} {name}  {detail}")

        print(_t("msg_autopilot_start", dir=cfg.watch_dir,
                 mode=cfg.mode, out=cfg.out_root))
        try:
            run_autopilot(cfg, on_event=on_event)
        except KeyboardInterrupt:
            print(_t("msg_autopilot_stopped"))
            return 0
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        return 0

    if parsed.command == "index":
        import json as _json
        from .search import auto_tag, build_index
        try:
            summary = build_index(
                parsed.paths, recursive=parsed.recursive,
                index_path=parsed.index, rebuild=parsed.rebuild,
                extractor_name=parsed.extractor)
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        payload = dict(summary)
        tags = [t.strip() for t in (parsed.tags or "").split(",")
                if t.strip()] or None
        if tags:
            try:
                tag_res = auto_tag(summary["index"], tags,
                                   min_score=parsed.min_score,
                                   max_tags=parsed.max_tags,
                                   write_xmp=getattr(parsed, 'write_xmp',
                                                     False))
            except RuntimeError as e:
                print(f"❌ {e}", file=sys.stderr)
                return 2
            payload["tags"] = {"tagged": tag_res["tagged"],
                               "untouched": tag_res["untouched"],
                               "assigned": tag_res["assigned"]}
        if getattr(parsed, 'json', False):
            print(_json.dumps(versioned(payload), indent=2,
                              ensure_ascii=False))
        else:
            print(_t("msg_index_done", total=summary["total"],
                     indexed=summary["indexed"], kept=summary["kept"],
                     removed=summary["removed"],
                     extractor=summary["extractor"],
                     index=summary["index"]))
            if tags and payload.get("tags"):
                print(_t("msg_index_tags", n=payload["tags"]["tagged"]))
        return 0

    if parsed.command == "find":
        import json as _json
        from .search import INDEX_FILENAME, find_similar
        query = " ".join(parsed.query).strip()
        if not query and not parsed.image:
            print(f"❌ {_t('msg_find_no_query')}", file=sys.stderr)
            return 2
        index_path = parsed.index or os.path.join(".", INDEX_FILENAME)
        try:
            res = find_similar(index_path, text=query or None,
                               image=parsed.image, k=parsed.k,
                               min_score=parsed.min_score)
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        if getattr(parsed, 'json', False):
            print(_json.dumps(versioned(res), indent=2, ensure_ascii=False))
        else:
            for hit in res["hits"]:
                print(f"  {hit['score']:.4f}  {hit['path']}")
        return 0

    if parsed.command == "preview":
        import json
        from .metrics import snapshot_image
        res = snapshot_image(parsed.path, max_dim=parsed.max_dim,
                             include_histogram=not parsed.no_histogram)
        if getattr(parsed, 'json', False):
            print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
        elif res.get("ok"):
            print(f"{res['path']}  {res['size'][0]}x{res['size'][1]}  "
                  f"jpeg={res['jpeg_bytes']}B"
                  f"{' +histogram' if 'histogram_png_base64' in res else ''}")
        else:
            print(f"❌ {res.get('error', '')}")
        return 0 if res.get("ok") else 1

    if parsed.command == "bench":
        import json as _json
        from .bench import run_benchmark
        files = _collect_files([parsed.dir], recursive=True)
        if not files:
            print(_t("msg_no_images_dir"), file=sys.stderr)
            return 1
        if parsed.images:
            files = files[:parsed.images]
        try:
            job_list = [int(x.strip()) for x in parsed.jobs.split(",") if x.strip()]
        except ValueError:
            print(_t("msg_bad_jobs"), file=sys.stderr)
            return 1
        if not job_list or any(j < 1 for j in job_list):
            print(_t("msg_jobs_ints"), file=sys.stderr)
            return 1
        job_list = list(dict.fromkeys(job_list))  # dedupe, keep order

        base = ProcessOptions(
            quality=getattr(parsed, 'quality', 85),
            output_format=getattr(parsed, 'format', 'JPEG'),
            denoise=parsed.denoise,
        )
        # Outputs go to a temp dir cleaned up by run_benchmark — the
        # source directory is never polluted.
        report = run_benchmark(files, job_list, base, evaluate=parsed.evaluate)
        out = {"dir": parsed.dir, "files": len(files), **report}
        if getattr(parsed, 'json', False):
            print(_json.dumps(versioned(out), indent=2, ensure_ascii=False))
        else:
            print(f"bench: {len(files)} files in {parsed.dir}")
            for r in report["runs"]:
                st = r["stages"]
                print(f"  jobs={r['jobs']:<3} {r['seconds']:>6.2f}s  "
                      f"speedup={r['speedup']:>4.2f}x  errors={r['errors']}  "
                      f"load={st['load']:.2f}s process={st['process']:.2f}s "
                      f"save={st['save']:.2f}s")
            ev = report.get("evaluate")
            if ev is not None:
                if ev["files"]:
                    psnr = (f"{ev['psnr_db']:.2f}dB"
                            if ev["psnr_db"] is not None else "inf")
                    print(f"  evaluate: {ev['files']} files  "
                          f"PSNR={psnr}  SSIM={ev['ssim']:.4f}")
                else:
                    print(f"  {_t('msg_eval_no_output')}")
            print(_t("msg_bench_tip"))
        return 0

    # ── Handle 'gallery' command ────────────────────────────────────────────
    if parsed.command == "gallery":
        from .gallery import build_gallery
        files = _collect_files(parsed.paths, recursive=parsed.recursive)
        if not files:
            return _no_files_exit(parsed)
        res = build_gallery(files, parsed.output, title=parsed.title,
                            thumb_size=parsed.thumb)
        if getattr(parsed, 'json', False):
            import json
            print(json.dumps(versioned(res), indent=2, ensure_ascii=False))
        else:
            print(f"{_t('msg_gallery')}: {res['output']}  "
                  f"({_t('msg_images_count', n=res['count'])})")
        return 0

    # ── Build options from parsed args (shared builder; also used by
    #    ``preset save`` so a CLI preset captures the full option set) ──────
    options = _build_process_options(parsed)

    # ── Apply config file defaults (explicit CLI args take precedence) ──────
    config_path = getattr(parsed, "config", None)
    explicit_config = config_path is not None
    if config_path is None:
        from .config import find_config
        config_path = find_config()
    if config_path:
        try:
            from .config import load_config
            cfg = load_config(config_path)
            _apply_config_defaults(options, parsed, cfg)
        except Exception as e:
            if explicit_config:
                print(f"{_t('msg_config_load_err')}: {e}")
                return 1
            print(f"⚠️  {_t('msg_config_load_err')}: {e}", file=sys.stderr)

    # ── Apply --preset (runs after config so a preset overrides config) ─────
    options = _apply_preset_defaults(options, parsed)
    if options is None:  # --preset named a nonexistent preset
        return 1

    # ── Collect files ───────────────────────────────────────────────────────
    file_patterns = getattr(parsed, 'files', None) or getattr(parsed, 'paths', [])
    recursive = getattr(parsed, 'recursive', False)
    files = _collect_files(file_patterns, recursive=recursive)

    if not files:
        return _no_files_exit(parsed)

    is_json = getattr(parsed, 'json', False)
    jout = sys.stderr if is_json else sys.stdout

    print(_t("msg_files_found", n=len(files)), file=jout)
    for f in files:
        sz = format_size(os.path.getsize(f))
        print(f"    {f} ({sz})", file=jout)
    print(file=jout)

    # ── Dry run ─────────────────────────────────────────────────────────────
    if getattr(parsed, 'dry_run', False):
        if is_json:
            import json
            print(json.dumps(versioned({
                "dry_run": True,
                "count": len(files),
                "files": files,
                "settings": {
                    "output_format": options.output_format,
                    "quality": options.quality,
                    "target_size": (format_size(options.target_size_bytes)
                                    if options.target_size_bytes else None),
                    "max_width": options.max_width,
                    "max_height": options.max_height,
                    "scale_percent": options.scale_percent,
                    "preserve_exif": options.preserve_exif,
                    "optimize": options.optimize,
                    "output_dir": options.output_dir,
                    "folder_pattern": options.folder_pattern,
                },
            }), indent=2, ensure_ascii=False))
            return 0
        print(_t("msg_dry_run_settings"))
        print()
        print(_t("msg_settings_applied"))
        print(f"  {_t('msg_target_format')}: {options.output_format}")
        if options.target_size_bytes:
            print(f"  {_t('msg_target_size')}: {format_size(options.target_size_bytes)} {_t('msg_auto_tune')}")
            print(f"  {_t('msg_quality_max')}: {options.quality} {_t('msg_ceiling')}")
        else:
            print(f"  {_t('msg_quality_label')}: {options.quality}")
        if options.max_width or options.max_height:
            print(f"  {_t('msg_resize_label')}: {options.max_width or 'auto'}×{options.max_height or 'auto'}")
        if options.scale_percent:
            print(f"  {_t('msg_scale_label')}: {options.scale_percent}%")
        print(f"  {_t('msg_exif_label')}: {_t('msg_yes') if options.preserve_exif else _t('msg_no')}")
        print(f"  {_t('msg_optimize_label')}: {_t('msg_yes') if options.optimize else _t('msg_no')}")
        print(f"  {_t('msg_output_dir_label')}: {options.output_dir or _t('msg_same_as_source')}")
        if options.folder_pattern:
            print(f"  {_t('msg_subfolder')}: {options.folder_pattern}")
        return 0

    def progress_callback(current, total, path):
        if path:
            name = os.path.basename(path)
            print(f"  {_t('msg_processing', i=current, n=total, name=name)}",
                  end="\r", file=jout)
        else:
            print(f"  {_t('msg_done', n=total)}" + " " * 20, file=jout)

    # ── Safety check for --remove-original ──────────────────────────────────
    # Rules:
    #   * interactive: always confirm (unless -y).
    #   * --json + flag on the command line: the explicit flag IS the
    #     confirmation (agents have no stdin to answer a prompt).
    #   * --json + value sourced from a CONFIG FILE (auto-discovered from the
    #     cwd!): refuse. A photo-s.toml someone left in a parent directory
    #     must never turn an agent's batch into silent mass deletion with
    #     exit 0.
    if options.remove_original and not getattr(parsed, 'yes', False):
        remove_flag_on_cli = hasattr(parsed, "remove_original")  # SUPPRESS default
        if is_json:
            if not remove_flag_on_cli:
                import json
                print(json.dumps(versioned({
                    "ok": False,
                    "error": (
                        "remove_original=true is set in a config file; "
                        "--json mode refuses implicit deletion of originals. "
                        "Pass --remove-original explicitly (or -y) to confirm."),
                }), indent=2, ensure_ascii=False))
                return 1
        else:
            print(_t("msg_confirm_delete", n=len(files)))
            try:
                confirm = input(f"   {_t('msg_confirm_continue')}").strip().lower()
            except EOFError:
                # stdin closed (pipe/agent): treat as refusal, not a traceback
                print(_t("msg_cancel_input_err"), file=sys.stderr)
                return 1
            if confirm not in ("y", "yes"):
                print(f"   {_t('msg_cancelled')}")
                return 0

    # ── Multi-profile batch run (--profiles web,thumb) ──────────────────────
    if getattr(parsed, 'profiles', None):
        from .presets import load_preset
        from dataclasses import asdict, replace

        names = [n.strip() for n in parsed.profiles.split(",") if n.strip()]
        if not names:
            print(_t("msg_profiles_need"))
            return 1

        if getattr(parsed, 'report', None):
            print(_t("msg_report_profiles_conflict"), file=sys.stderr)

        profile_results = {}
        failed_total = 0
        for name in names:
            base = load_preset(name)
            if base is None:
                print(_t("msg_preset_not_found_generic", name=name))
                return 1
            # Preset values win over CLI/config EXCEPT output_dir/suffix
            # (presets serialize stale directories — those come from CLI/config),
            # runtime fields (jobs/target_size_bytes), and serialized None
            # defaults (must not clobber explicit CLI/config values)
            fields = {k: v for k, v in asdict(base).items()
                      if k in ProcessOptions.__dataclass_fields__
                      and k not in ("output_dir", "suffix", "jobs",
                                    "target_size_bytes")
                      and v is not None}
            prof_opts = replace(options, **fields)
            prof_result = batch_process(files, prof_opts,
                                        progress_callback=progress_callback)
            profile_results[name] = prof_result
            failed_total += prof_result.fail_count
            if not is_json:
                print(f"\n{_t('msg_profile_start', name=name, n=len(files))}")
                _print_batch_summary(prof_result)

        if is_json:
            import json
            print(json.dumps(versioned({"profiles": {n: r.to_dict() for n, r in profile_results.items()}}),
                indent=2, ensure_ascii=False))
        return 0 if failed_total == 0 else 1

    # ── Execute ──────────────────────────────────────────────────────────────
    if not is_json:
        print(_t("msg_start_processing"))
        if options.target_size_bytes:
            lossy_fmts = {"JPEG", "WebP", "HEIC"}
            if options.output_format not in lossy_fmts:
                print(_t("msg_lossless_note", fmt=options.output_format))
            print(_t("msg_target_size_header", size=format_size(options.target_size_bytes)))
            print(f"   {_t('msg_quality_range', q=options.quality)}")
        print()

    # ── Trace log (v1.9.0 数据管线：before-analyze → params → after-analyze) ──
    trace_file = None
    trace_lock = None
    if getattr(parsed, 'trace', None):
        import dataclasses
        import json as _json
        import threading as _th
        os.makedirs(parsed.trace, exist_ok=True)
        trace_file = open(os.path.join(parsed.trace, "photo_s_trace.jsonl"),
                          "a", encoding="utf-8")
        trace_lock = _th.Lock()

        def trace_cb(src, result):
            from .metrics import analyze_image
            try:
                before = analyze_image(src, 128)
                after = (analyze_image(result.output_path, 128)
                         if result.success and result.output_path else None)
            except Exception:
                before = after = None
            entry = {
                "path": src,
                "success": result.success,
                "error": result.error,
                "output": result.output_path,
                "before": before,
                "after": after,
                "options": dataclasses.asdict(options),
            }
            with trace_lock:
                trace_file.write(_json.dumps(entry, ensure_ascii=False) + "\n")
                trace_file.flush()
    else:
        trace_cb = None

    # ── XMP sidecar 导出（v2.5 LR 双向互通）：--auto-tone 组合时逐图先
    # 预测一次并把参数并进 per-file options——sidecar 记录的是真实应用的
    # 参数，且引擎不再经槽位二次推理 ──────────────────────────────────────
    resolved_xmp_opts: dict = {}
    per_file_for_xmp = None
    if getattr(parsed, 'write_xmp', False) and getattr(parsed, 'auto_tone', None):
        from .plugin import find_provider
        if find_provider("auto_tone") is None:
            print("--write-xmp --auto-tone needs the auto-tone plugin "
                  "(pip install photo-s-plugin-auto-tone)", file=sys.stderr)
            return 2

        def per_file_for_xmp(path, base_opts):
            from .autotone import resolve_auto_tone_options
            try:
                merged, _ = resolve_auto_tone_options(base_opts, path)
                resolved_xmp_opts[path] = merged
                return merged
            except Exception:
                # 单图预测失败回落槽位路径（引擎自身有清晰的 per-file 报错）
                resolved_xmp_opts[path] = base_opts
                return base_opts

    result = batch_process(files, options, progress_callback=progress_callback,
                           trace_callback=trace_cb,
                           per_file_options=per_file_for_xmp)
    if trace_file:
        trace_file.close()

    xmp_rows = []
    if getattr(parsed, 'write_xmp', False):
        from .lrxmp import write_xmp_sidecar
        for r in result.results:
            if not r.success:
                continue
            eff = resolved_xmp_opts.get(r.input_path) or options
            try:
                sidecar, warns = write_xmp_sidecar(r.input_path, eff)
                xmp_rows.append({"input": r.input_path, "sidecar": sidecar,
                                 "warnings": warns})
            except Exception as e:
                xmp_rows.append({"input": r.input_path, "error": str(e)})
        if not is_json:
            for row in xmp_rows:
                if row.get("error"):
                    print(f"  ⚠️ XMP {row['input']}: {row['error']}",
                          file=sys.stderr)
                else:
                    print(f"  📄 XMP {row['sidecar']}")

    # ── Write CSV report if requested ───────────────────────────────────────
    if getattr(parsed, 'report', None):
        from .engine import _write_report
        try:
            _write_report(parsed.report, result)
        except OSError as e:
            print(f"{_t('msg_report_err')}: {e}", file=sys.stderr)

    # ── Print results ───────────────────────────────────────────────────────
    if is_json:
        import json
        payload = result.to_dict()
        if xmp_rows:
            payload["xmp_sidecars"] = xmp_rows
        print(json.dumps(versioned(payload), indent=2, ensure_ascii=False))
    else:
        print()
        for r in result.results:
            _print_result(r)
            print()
        _print_batch_summary(result)

    return 0 if result.fail_count == 0 else 1


def _no_gui_msg() -> str:
    """Lite-build hint — rendered in the current language."""
    return (
        f"{_t('msg_no_gui')}\n"
        f"   {_t('msg_full_edition')}: "
        "https://github.com/Dongwu259/photo_s/releases")


def _gui_module_available() -> bool:
    """Whether the GUI module exists in this build.

    The lite executable excludes ``photo_s.gui`` at PyInstaller level, so
    this is False there; full wheels/exes always ship the module.
    """
    import importlib.util
    try:
        return importlib.util.find_spec("photo_s.gui") is not None
    except (ImportError, ValueError):
        return False


def _run_gui() -> bool:
    """Launch the GUI; print a hint and return False when this build /
    system has no GUI (lite exe, or tkinter missing)."""
    from . import i18n
    i18n.CURRENT_LANG = i18n.resolve_language(use_config=True, use_persisted=False)
    try:
        from .gui import run_gui
    except ImportError:
        print(_no_gui_msg(), file=sys.stderr)
        return False
    run_gui()
    return True


def main():
    """Entry point for the photo-s command.

    Dispatches to GUI if no args are given or first arg is 'gui',
    otherwise runs CLI mode. Builds without the GUI (lite) print a hint
    and fall back to --help / exit 1 instead of crashing on ImportError.
    """
    # Force UTF-8 console output: CLI status lines use emoji, which crashes
    # on Windows' default cp1252 code page (e.g. redirected/piped output).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) <= 1:
        if _run_gui():
            return 0
        try:
            run_cli(["--help"])
        except SystemExit as e:  # argparse --help raises SystemExit(0)
            return int(e.code or 0)
        return 0
    if sys.argv[1] == "gui":
        return 0 if _run_gui() else 1
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
