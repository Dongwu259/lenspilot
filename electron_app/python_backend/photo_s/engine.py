"""
PhotoS - Core Image Processing Engine

Handles image compression, format conversion, resizing, and batch processing.
Uses Pillow (PIL) as the primary backend. HEIC support via pillow-heif (cross-platform)
with sips fallback on macOS.
"""

import os
import re
import sys
import threading
import warnings
import subprocess
import tempfile
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Optional, Callable, List, Tuple
from dataclasses import dataclass, replace

from PIL import Image, UnidentifiedImageError

from .adjust import (
    apply_color_management,
    apply_tone_adjustments,
    apply_white_balance,
    apply_exposure,
    apply_auto_levels,
    apply_crop,
    apply_crop_ratio,
    apply_rotate,
    apply_flip,
    apply_pad,
)

try:
    import piexif
    _HAS_PIEXIF = True
except ImportError:
    _HAS_PIEXIF = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HAS_PILLOW_HEIF = True
except ImportError:
    _HAS_PILLOW_HEIF = False

try:
    import pillow_avif  # noqa: F401 — registers AVIF support with Pillow
    _HAS_PILLOW_AVIF = True
except ImportError:
    _HAS_PILLOW_AVIF = False


class RawOptionError(ValueError):
    """A user-chosen RAW decode option this build/rawpy can't honor.

    Distinct from decode failures: option errors must surface as clear
    per-file errors and MUST NOT trigger the sips fallback (which would swap
    in a different renderer and silently change brightness/colors).
    """


# ── Supported formats ──────────────────────────────────────────────────────

SUPPORTED_FORMATS = {
    "JPEG":  {"ext": ".jpg",  "save_kwargs": ["quality", "optimize", "progressive"]},
    "PNG":   {"ext": ".png",  "save_kwargs": ["optimize"]},
    "WebP":  {"ext": ".webp", "save_kwargs": ["quality", "method"]},
    "TIFF":  {"ext": ".tiff", "save_kwargs": ["compression"]},
    "BMP":   {"ext": ".bmp",  "save_kwargs": []},
    "HEIC":  {"ext": ".heic", "save_kwargs": ["quality"]},
    "ICO":   {"ext": ".ico",  "save_kwargs": []},
    "AVIF":  {"ext": ".avif", "save_kwargs": ["quality"]},
}

# JPEG chroma subsampling → PIL `subsampling` save param (0=4:4:4, 1=4:2:2,
# 2=4:2:0). PIL's default at typical qualities is 4:2:0, which halves color
# resolution vs luma — visible as soft color edges. 4:4:4 keeps full color
# detail at the cost of larger files; 4:2:2 is the middle ground.
_JPEG_SUBSAMPLING = {"444": 0, "422": 1, "420": 2}
_SUBSAMPLING_WARNED = False  # warn once per process, not once per file

INPUT_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif",
    ".bmp", ".heic", ".heif", ".ico", ".gif", ".psd", ".avif",
}

# Camera RAW file extensions (processed via rawpy or sips)
RAW_EXTENSIONS = {
    ".cr2", ".cr3",   # Canon
    ".nef", ".nrw",   # Nikon
    ".arw", ".srf", ".sr2",  # Sony
    ".dng",           # Adobe / Leica / smartphone
    ".orf",           # Olympus / OM System
    ".rw2",           # Panasonic
    ".raf",           # Fujifilm
    ".pef",           # Pentax
    ".srw",           # Samsung
    ".crw",           # Canon (old)
    ".mrw",           # Minolta
    ".3fr",           # Hasselblad
    ".erf",           # Epson
    ".kdc",           # Kodak
    ".mef",           # Mamiya
    ".mos",           # Leaf
    ".x3f",           # Sigma
    ".raw",           # generic
    ".rwl",           # Leica
}

# All supported input extensions (standard + RAW)
ALL_INPUT_EXTENSIONS = INPUT_EXTENSIONS | RAW_EXTENSIONS

# PIL can't write HEIC directly unless pillow-heif is installed
PIL_WRITABLE = {"JPEG", "PNG", "WebP", "TIFF", "BMP", "ICO"}
if _HAS_PILLOW_HEIF:
    PIL_WRITABLE.add("HEIC")
if _HAS_PILLOW_AVIF:
    PIL_WRITABLE.add("AVIF")


# ── Data classes ────────────────────────────────────────────────────────────

def auto_jobs() -> int:
    """Smart default for parallel workers: capped CPU count.

    The heavy pipeline stages (RAW decode, OpenCV NLM/straighten, onnxruntime,
    Pillow resize/encode) all release the GIL, so a thread pool at
    ``min(cpu_count, 8)`` typically scales near-linearly without oversubscribing.
    Explicit user choice always wins — this is only the fallback default.
    """
    try:
        return max(1, min(os.cpu_count() or 2, 8))
    except Exception:  # noqa: BLE001 — never let the default computation break a run
        return 1


@dataclass
class ProcessOptions:
    """Options for batch image processing."""
    quality: int = 85            # 1-100, applies to JPEG/WebP/HEIC
                                  # When target_size_bytes is set, this is the quality ceiling
    output_format: str = "JPEG"  # target format (JPEG, PNG, WebP, ...)
    output_dir: Optional[str] = None   # output directory (None = same as source)
    max_width: Optional[int] = None    # resize: max width in pixels
    max_height: Optional[int] = None   # resize: max height in pixels
    scale_percent: Optional[int] = None  # resize: scale percentage (1-100)
    preserve_exif: bool = True   # keep EXIF metadata
    optimize: bool = True        # run PIL's optimize pass
    progressive: bool = False    # progressive JPEG
    jpeg_subsampling: str = "420"  # JPEG chroma subsampling: 444/422/420
                                   # (444 = full color, larger files)
    overwrite: bool = False      # overwrite existing files
    prefix: str = ""             # output filename prefix
    suffix: str = "_compressed"  # output filename suffix
    target_size_bytes: Optional[int] = None  # target output size in bytes
                                              # If set, auto-tune quality to fit this size
    # RAW processing options
    raw_half_size: bool = False    # decode RAW at half resolution (faster)
    raw_auto_bright: bool = True   # auto brightness adjustment (rawpy default)
    raw_demosaic: str = "auto"     # demosaic algorithm: auto/ahd/vng/ppg/dcb/dht/amaze
                                   # (amaze = highest quality, slowest)
    raw_color_space: str = "sRGB"  # output color space: sRGB/AdobeRGB/ProPhotoRGB
                                   # (sRGB auto-tagged with ICC; wider gamuts
                                   # are untagged — tag in your editor)
    raw_16bit: bool = False        # decode at 16-bit; TIFF output written as
                                   # 16-bit (via tifffile). Only meaningful for
                                   # pure RAW→TIFF conversion (JPEG is 8-bit;
                                   # any pipeline transform drops the 16-bit
                                   # array → falls back to 8-bit).
    # Post-processing options
    auto_rotate: bool = True       # auto-rotate by EXIF Orientation tag
    remove_original: bool = False  # delete original after successful processing
    # Rename pattern (empty = use prefix/suffix)
    rename_pattern: str = ""       # e.g. "{date}_{camera}_{seq}"
    # Folder organization (empty = no subfolder)
    folder_pattern: str = ""       # e.g. "{year}/{month}" or "date-camera"
    # Watermark options
    watermark_text: str = ""       # text to overlay
    watermark_image: str = ""      # path to overlay image
    watermark_position: str = "BOTTOM_RIGHT"
    watermark_opacity: int = 50    # 0-100
    # Multi-size output
    output_sizes: list = None      # list of (label, max_w, max_h) tuples
    # Parallel processing
    jobs: int = 1                  # number of parallel workers (1 = sequential)
    # Privacy & metadata
    strip_gps: bool = False        # remove GPS EXIF IFD on save
    keep_mtime: bool = False       # preserve source file modification time
    max_pixels: Optional[int] = None  # resize: cap longest side (downscale only)
    evaluate: bool = False         # compute SSIM between input and output
    # Tone & color (multipliers, 1.0 = no change)
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    sharpen: float = 1.0
    export_sharpen: Optional[float] = None  # LR-style output-stage USM
                                            # (0/None off; radius scales with
                                            # final output resolution)
    grayscale: bool = False        # convert to black & white
    sepia: bool = False            # sepia toning
    wb_temp: Optional[int] = None  # white balance in Kelvin (None = no change)
    wb_reference: Optional[str] = None  # white balance from a reference image
    auto_levels: bool = False      # auto histogram stretch (2% clip)
    highlight_recovery: Optional[float] = None  # LR-style highlight recovery
                                                # (0-1; compress clipped
                                                # highlights back to gradient)
    ev: Optional[float] = None     # exposure compensation in stops (2^EV gain)
    auto_exposure: Optional[float] = None  # normalize mean luminance to target (0-1)
    # Lightroom-direction grading (v1.6.0). Compact string/scalar fields so
    # REST (_scalar_groups) / MCP / CLI / preset inherit them without glue.
    wb_tint: float = 0.0        # green(-)/magenta(+) G-M axis, ~[-100, 100]
    levels: str = ""            # manual levels "black,white[,gamma]"
    curves: str = ""            # point curves "ch:x,y;x,y|ch:..."
    vibrance: float = 0.0       # natural saturation [-1, 1], 0 = off
    color_grading: str = ""     # 3-way "zone:hue,sat;zone:hue,sat"
    # P1 stylize (v1.6.0)
    hsl: str = ""               # per-color "color:h,s,l;color:..."
    point_color: str = ""       # sampled-color targets "r,g,b:h,s,l[,range];..."
    clarity: float = 0.0        # local contrast, large radius (0 = off)
    texture: float = 0.0        # fine detail, small radius (0 = off)
    dehaze: float = 0.0         # dark-channel dehaze [-1, 1], 0 = off
    vignette: str = ""          # radial "amount[,midpoint[,feather]]"
    grain: str = ""             # film grain "amount[,size]"
    # Local adjustments under masks (v1.7.0). Named masks + per-mask
    # scalar adjustments, relative 0-1 coords so one spec fits a batch.
    masks: str = ""             # "name:type:params;..." (linear/radial/color)
    mask_adjust: str = ""       # "name:key=value,...;..." referencing masks
    # Lens correction (v1.7.0, manual params - no lens database)
    lens_distort: float = 0.0   # radial distortion k1 (+ = barrel fix)
    lens_vignette: str = ""     # corner-lift "amount[,midpoint]"
    lens_ca: str = ""           # channel scales "r_scale,b_scale" (~1.0)
    lens_profile: Optional[str] = None  # named profile in ~/.photos/lens_profiles.json
                                        # (resolved at pipeline start; explicit
                                        # lens_* values win)
    lut_file: Optional[str] = None  # .cube 3D/1D LUT color grade (provider or built-in)
    log_curve: Optional[str] = None  # LOG recovery curve name (SLOG3, CLOG3, ...)
    denoise: Optional[float] = None  # denoise strength; SCUNet plugin provider
    #                                # preferred when installed, else NLM
    #                                # (NLM needs opencv extra)
    auto_tone: Optional[float] = None  # AI auto-tone strength 0-1 (auto-tone
    #                                # plugin provider; no built-in fallback —
    #                                # without the plugin this is an error)
    auto_straighten: bool = False  # auto-level the horizon (needs opencv extra)
    max_straighten_angle: float = 10.0  # max horizon tilt to correct (degrees)
    # Composition
    crop: Optional[str] = None     # "WxH+X+Y" (offsets optional → centered)
    crop_ratio: Optional[str] = None  # "16:9" center crop
    rotate_degrees: float = 0.0    # arbitrary rotation, positive = clockwise
    rotate_bg: Optional[str] = None   # rotation corner fill (#RRGGBB; None = black)
    flip: Optional[str] = None     # "h" horizontal / "v" vertical mirror
    pad_ratio: Optional[str] = None   # letterbox to aspect ratio e.g. "16:9"
    pad_bg: str = "#000000"        # letterbox background color
    # Metadata handling
    date_shift: Optional[str] = None  # EXIF datetime offset, e.g. "-5h30m"
    scrub: bool = False            # strip ALL metadata (EXIF+ICC+comment)
    sync_date: bool = False        # set output mtime from EXIF DateTimeOriginal
    # Quality & color management
    blur_score: bool = False       # compute a blur heuristic for the input
    srgb: bool = False             # re-tag output with sRGB ICC profile
    flatten_cmyk: bool = False     # convert CMYK input to RGB
    # Print export: center-crop to WxH and resize to exact print pixels
    print_size: Optional[str] = None  # e.g. "8x10@300dpi"
    # Workflow
    resume: bool = False           # skip inputs whose output already exists
    gpx_trace: Optional[str] = None  # GPX track path for GPS geo-tagging
    blur_faces: Optional[str] = None  # "blur"|"pixelate" privacy mask (opencv)
    blur_faces_margin: Optional[int] = None  # face-box expansion % (default 20)
    # Cutout / background removal (v2.1.0): subject | person | object:label |
    # color:R,G,B[,tol=30][,feather=0][,invert] — mask -> alpha channel.
    # Needs a transparency-capable output (PNG/WebP/TIFF/AVIF/HEIC); JPEG
    # raises a per-file error rather than silently flattening to white.
    cutout: Optional[str] = None


@dataclass
class ProcessResult:
    """Result of processing a single image."""
    input_path: str
    output_path: str
    input_size: int       # bytes
    output_size: int      # bytes
    input_format: str
    output_format: str
    input_dims: Tuple[int, int]
    output_dims: Tuple[int, int]
    success: bool
    error: str = ""
    achieved_quality: int = 0  # actual quality used (differs from options.quality
                                # when target_size_bytes is set)
    ssim: Optional[float] = None  # structural similarity score (--evaluate)
    blur_score: Optional[float] = None  # blur heuristic for the input (--blur-score)
    auto_straightened: bool = False  # horizon auto-leveled (--auto-straighten)

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict (for --json output)."""
        return {
            "input": self.input_path,
            "output": self.output_path,
            "input_size": self.input_size,
            "output_size": self.output_size,
            "input_format": self.input_format,
            "output_format": self.output_format,
            "input_dims": list(self.input_dims),
            "output_dims": list(self.output_dims),
            "status": "ok" if self.success else "error",
            "error": self.error,
            "quality": self.achieved_quality,
            "ssim": self.ssim,
            "blur_score": self.blur_score,
            "auto_straightened": self.auto_straightened,
        }


@dataclass
class BatchResult:
    """Aggregated result of a batch operation."""
    results: List[ProcessResult]
    total_input_size: int
    total_output_size: int
    success_count: int
    fail_count: int

    @property
    def savings_percent(self) -> float:
        if self.total_input_size == 0:
            return 0.0
        return (1 - self.total_output_size / self.total_input_size) * 100

    @property
    def savings_bytes(self) -> int:
        return self.total_input_size - self.total_output_size

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict (for --json output)."""
        return {
            "summary": {
                "total": self.success_count + self.fail_count,
                "success": self.success_count,
                "failed": self.fail_count,
                "total_input_size": self.total_input_size,
                "total_output_size": self.total_output_size,
                "saved_bytes": self.savings_bytes,
                "saved_percent": round(self.savings_percent, 1),
            },
            "results": [r.to_dict() for r in self.results],
        }


# ── Format helpers ──────────────────────────────────────────────────────────

def _format_from_path(path: str) -> str:
    """Infer image format from file extension."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".jpg": "JPEG", ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WebP",
        ".tiff": "TIFF", ".tif": "TIFF",
        ".bmp": "BMP",
        ".heic": "HEIC", ".heif": "HEIC",
        ".ico": "ICO",
        ".avif": "AVIF",
    }
    if ext in RAW_EXTENSIONS:
        return "RAW"
    return mapping.get(ext, "JPEG")


def _canonical_format(fmt) -> str:
    """Normalize a format name to SUPPORTED_FORMATS canonical case.

    Case-insensitive: 'png' → 'PNG', 'webp' → 'WebP', 'jpeg' → 'JPEG'.
    Unknown values are returned unchanged (callers validate / fail later).
    """
    if not fmt:
        return fmt
    lowered = str(fmt).lower()
    for key in SUPPORTED_FORMATS:
        if key.lower() == lowered:
            return key
    return fmt


def _parse_print_size(spec: str) -> Tuple[float, float, int]:
    """Parse a print size spec like '8x10', '8x10@300', '8x10@300dpi'.

    Returns (width_in, height_in, dpi); dpi defaults to 300.
    Raises ValueError on unparseable input.
    """
    text = str(spec).strip().lower()
    dpi = 300
    if "@" in text:
        text, _, dpi_str = text.partition("@")
        dpi_str = dpi_str.replace("dpi", "").strip()
        if not dpi_str:
            raise ValueError(f"cannot parse print size {spec!r}")
        dpi = int(dpi_str)
    if "x" not in text:
        raise ValueError(f"cannot parse print size {spec!r} (expected WxH[@DPI])")
    w_str, _, h_str = text.partition("x")
    w = float(w_str.strip())
    h = float(h_str.strip())
    if w <= 0 or h <= 0 or dpi <= 0:
        raise ValueError(f"invalid print size {spec!r}")
    return w, h, dpi


def _get_output_path(input_path: str, output_format: str, output_dir: Optional[str],
                     prefix: str, suffix: str, overwrite: bool,
                     rename_pattern: str = "",
                     exif_meta: Optional[dict] = None,
                     seq_counter: Optional[List[int]] = None,
                     folder_pattern: str = "",
                     preassigned: Optional[str] = None,
                     reserved: Optional[set] = None) -> str:
    """Generate output file path.

    If rename_pattern is set, use template-based naming instead of prefix/suffix.
    A ``preassigned`` path (reserved by batch_process) wins over deriving one.
    """
    # Canonicalize here too: process_image already does this, but the resume
    # pre-pass in batch_process calls this directly with the raw option — a
    # lowercase format would otherwise raise a bare KeyError on the whole batch.
    fmt = _canonical_format(output_format)
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported output format {output_format!r}; "
            f"supported: {sorted(SUPPORTED_FORMATS)}")

    # batch_process pre-assigns a unique path per input up front — parallel
    # workers can't race the exists() dedup below, so trust it as-is.
    if preassigned:
        return preassigned

    fmt_info = SUPPORTED_FORMATS[fmt]
    in_path = Path(input_path)

    # prefix/suffix are user/REST-supplied strings joined into the output
    # filename — a suffix like "/../../../tmp/pwned" must not escape the
    # output dir (same defense as the smart-rename branch below).
    if _has_path_traversal(prefix) or _has_path_traversal(suffix):
        raise ValueError(
            "output filename prefix/suffix must not contain path separators "
            f"or traversal segments (got prefix={prefix!r}, suffix={suffix!r})")

    if rename_pattern and exif_meta is not None:
        # Smart rename mode
        seq = seq_counter[0] if seq_counter else 0
        if seq_counter:
            seq_counter[0] += 1
        new_stem = _render_rename_pattern(rename_pattern, exif_meta, seq)
        if not new_stem.strip() or _has_path_traversal(new_stem):
            # An all-empty render (e.g. "{date}" on an EXIF-less photo) would
            # produce a hidden ".jpg" — same fallback as the traversal guard.
            new_stem = f"{prefix}{in_path.stem}{suffix}"
    else:
        new_stem = f"{prefix}{in_path.stem}{suffix}"
    new_stem = _sanitize_stem(new_stem)

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = in_path.parent

    # Apply folder pattern (organize into subfolders)
    if folder_pattern and exif_meta is not None:
        rendered = _render_rename_pattern(folder_pattern, exif_meta, 0)
        segments = [s.strip() for s in rendered.split("/") if s.strip()]
        # Sanitize: prevent path traversal (incl. Windows drive-relative "C:..")
        segments = [s for s in segments if s not in ("..", ".") and ":" not in s
                    and not s.startswith("~")]
        if segments:
            out_dir = out_dir.joinpath(*segments)

    out_path = out_dir / f"{new_stem}{fmt_info['ext']}"

    # Avoid overwriting unless explicitly allowed. `reserved` holds the paths
    # batch_process already assigned to earlier inputs this run — the plain
    # exists() check races when workers run in parallel.
    if not overwrite:
        counter = 1
        while (out_path.exists()
               or (reserved is not None and str(out_path) in reserved)):
            out_path = out_dir / f"{new_stem}_{counter}{fmt_info['ext']}"
            counter += 1
        if reserved is not None:
            reserved.add(str(out_path))

    return str(out_path)


def _extract_exif_metadata(img: Image.Image, path: str) -> dict:
    """Extract metadata from image EXIF for smart renaming.

    Returns dict with keys: date, time, camera, original, iso, focal, make
    """
    meta = {
        "date": "", "time": "", "camera": "", "original": Path(path).stem,
        "iso": "", "focal": "", "make": "",
        "year": "", "month": "", "day": "",
        "tz_offset": "",  # EXIF OffsetTime("+08:00") — bridges local↔UTC
    }

    try:
        # PIL warns "Corrupt EXIF data" on the Exif sub-IFD when it meets tags
        # it doesn't know (e.g. Rating 0x4746) — it is NOT corruption, so
        # silence it here; the pipeline must not spam stderr on tagged photos.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Corrupt EXIF data.*")
            exif = img.getexif()
            if not exif:
                return meta

            # DateTimeOriginal lives in the Exif sub-IFD (0x8769), which
            # img.getexif() doesn't surface directly — pull it via get_ifd
            try:
                exif_sub = exif.get_ifd(0x8769)
            except Exception:
                exif_sub = {}
        dt_str = (exif_sub.get(0x9003)
                  or exif_sub.get(0x9004)
                  or exif.get(0x0132)
                  or "")
        if isinstance(dt_str, bytes):
            dt_str = dt_str.decode("utf-8", "replace")
        if dt_str:
            # Format: "2024:07:30 14:30:00". Only trust all-digit segments:
            # a crafted DateTimeOriginal could otherwise smuggle path
            # separators / ".." into {year}/{date}/... rename filenames.
            parts = dt_str.replace(" ", ":").split(":")
            if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                meta["date"] = f"{parts[0]}-{parts[1]}-{parts[2]}"
                meta["year"] = parts[0]
                meta["month"] = parts[1]
                meta["day"] = parts[2]
            if len(parts) >= 6 and all(p.isdigit() for p in parts[3:6]):
                meta["time"] = f"{parts[3]}-{parts[4]}-{parts[5]}"
            elif len(parts) >= 5 and all(p.isdigit() for p in parts[3:5]):
                meta["time"] = f"{parts[3]}-{parts[4]}"

        # Camera model (sanitize: remove null bytes, non-printable chars)
        model = exif.get(0x0110) or ""
        if isinstance(model, str):
            model = model.replace("\x00", "").strip()
            # Remove characters unsafe for filenames
            safe = ""
            for ch in model:
                if ch.isalnum() or ch in " _-":
                    safe += ch
                else:
                    safe += "_"
            meta["camera"] = safe.strip("_") or "Unknown"

        # Make (sanitize like camera: unsafe chars → "_"; a crafted Make tag
        # must never escape the output dir via {make} in rename patterns)
        make = exif.get(0x010F) or ""
        if isinstance(make, str):
            make = make.replace("\x00", "").strip()
            safe = ""
            for ch in make:
                if ch.isalnum() or ch in " _-":
                    safe += ch
                else:
                    safe += "_"
            meta["make"] = safe.strip("_")

        # ISO (digits only — a crafted tag must not carry separators into
        # {iso} rename filenames). Like DateTimeOriginal above, ISO lives in
        # the Exif sub-IFD — a 0th IFD lookup never sees it.
        iso = exif_sub.get(0x8827)
        if iso:
            meta["iso"] = "".join(c for c in str(iso) if c.isdigit())

        # Focal length (Exif sub-IFD as well)
        focal = exif_sub.get(0x920A)
        if focal:
            if isinstance(focal, tuple):  # (numerator, denominator)
                meta["focal"] = f"{focal[0] // focal[1]}mm" if focal[1] else ""
            else:
                meta["focal"] = f"{int(float(focal))}mm"

        # Timezone offset (OffsetTimeOriginal 0x9011 / OffsetTime 0x9010,
        # "+08:00" format) — needed to match camera-local EXIF datetimes
        # against UTC GPX timestamps when geo-tagging.
        tz = exif_sub.get(0x9011) or exif_sub.get(0x9010) \
            or exif.get(0x9010) or ""
        if isinstance(tz, bytes):
            tz = tz.decode("ascii", "ignore")
        meta["tz_offset"] = str(tz).strip() if tz else ""

    except Exception:
        pass

    return meta


def _has_path_traversal(name: str) -> bool:
    """True if `name` can escape its directory when joined into a path.

    Defense-in-depth on top of _extract_exif_metadata sanitization: rejects
    path separators, dot-segments, and Windows drive-relative prefixes (a
    ``C:name`` segment makes ntpath.join drop the base directory).
    """
    if name in (".", ".."):
        return True
    return "/" in name or "\\" in name or ":" in name


# Windows forbids these bare stems (plus COM1-9 / LPT1-9) and any name ending
# in a dot or space — such files cannot exist on NTFS, so makedirs/save would
# fail late with a confusing error (or Explorer would mangle them).
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def _sanitize_stem(stem: str) -> str:
    """Make a filename stem safe on Windows as well as POSIX.

    Strips trailing dots/spaces and prefixes reserved device names with "_".
    """
    stem = stem.rstrip(". ")
    if stem.split(".")[0].upper() in _WIN_RESERVED:
        stem = "_" + stem
    return stem


def _render_rename_pattern(pattern: str, meta: dict, seq: int = 0) -> str:
    """Render a rename template with metadata values.

    Supported variables:
      {year}   → 2024
      {month}  → 08
      {day}    → 15
      {date}   → 2024-07-30
      {time}   → 14-30-00
      {camera} → ILCE-7M4
      {make}   → SONY
      {original} → DSC00001
      {iso}    → 400
      {focal}  → 50mm
      {seq}    → 001 (zero-padded 3-digit counter)
    """
    result = pattern
    replacements = {
        "{year}": meta.get("year", ""),
        "{month}": meta.get("month", ""),
        "{day}": meta.get("day", ""),
        "{date}": meta.get("date", ""),
        "{time}": meta.get("time", ""),
        "{camera}": meta.get("camera", ""),
        "{make}": meta.get("make", ""),
        "{original}": meta.get("original", ""),
        "{iso}": meta.get("iso", ""),
        "{focal}": meta.get("focal", ""),
        "{seq}": f"{seq:03d}",
    }
    for key, val in replacements.items():
        result = result.replace(key, val)
    return result


def _resolve_folder_pattern(pattern: str) -> str:
    """Resolve a folder pattern shorthand or return the template as-is.

    Shorthand values:
      "date"        → "{year}/{month}"
      "camera"      → "{camera}"
      "date-camera" → "{year}/{month}/{camera}"

    Any other value (including custom templates like "{year}/{camera}")
    is returned unchanged. An empty string is returned as-is (no subfolders).
    """
    if not pattern or not pattern.strip():
        return ""

    PRESET_MAP = {
        "date":        "{year}/{month}",
        "camera":      "{camera}",
        "date-camera": "{year}/{month}/{camera}",
    }
    return PRESET_MAP.get(pattern.strip(), pattern)


# ── HEIC support (pillow-heif cross-platform, sips macOS fallback) ──────────

def _heic_to_png_via_sips(heic_path: str) -> Optional[str]:
    """Convert HEIC to temporary PNG using macOS sips (fallback when no pillow-heif)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        subprocess.run(
            ["sips", "-s", "format", "png", heic_path, "--out", tmp.name],
            check=True, capture_output=True, timeout=30,
        )
        return tmp.name
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        return None


# ── Target-size quality auto-tuning ──────────────────────────────────────

# Formats where quality parameter actually affects file size
_QUALITY_AWARE_FORMATS = {"JPEG", "WebP", "HEIC", "AVIF"}


def _compress_to_temp(img: Image.Image, fmt: str, quality: int,
                      options: ProcessOptions) -> int:
    """Compress image to a temp file at the given quality, return file size in bytes.

    Used during binary search to test quality→size outcomes without
    cluttering the output directory.
    """
    tmp = tempfile.NamedTemporaryFile(
        suffix=SUPPORTED_FORMATS.get(fmt, {}).get("ext", ".jpg"),
        delete=False,
    )
    tmp.close()
    try:
        # replace() carries every ProcessOptions field automatically — no
        # per-field copy to keep in sync when new options are added.
        trial_opts = replace(options, quality=quality, output_format=fmt)
        _save_image(img, tmp.name, fmt, trial_opts)
        return os.path.getsize(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _find_quality_for_target(img: Image.Image, fmt: str,
                             options: ProcessOptions) -> Tuple[int, Optional[int]]:
    """Binary search to find the highest quality ∈ [5, quality_ceiling]
    that produces output ≤ target_size_bytes.

    Returns ``(quality, min_size)``: the found quality (best-effort if the
    target can't be met) and, when even q=5 overshoots, the q=5 size so the
    caller can report the unreachable-target warning WITHOUT re-encoding
    (the old flow encoded q=5 twice for every such file).

    Algorithm:
      - low=5, high=options.quality (the user's quality setting is the ceiling)
      - Binary search for ~8 iterations
      - Stops early if we land within 5% of the target
    """
    target = options.target_size_bytes
    ceiling = max(options.quality, 5)  # quality ceiling from user setting

    # Quick check: is even the ceiling already small enough?
    if _compress_to_temp(img, fmt, ceiling, options) <= target:
        return ceiling, None  # No additional compression needed

    # Quick check: can even the lowest quality meet the target?
    size_at_5 = _compress_to_temp(img, fmt, 5, options)
    if size_at_5 > target:
        return 5, size_at_5  # Best effort — target is too aggressive

    low, high = 5, ceiling
    best_q = low
    max_iterations = 8

    for _ in range(max_iterations):
        mid = (low + high) // 2
        size = _compress_to_temp(img, fmt, mid, options)

        if size <= target:
            # This quality meets the target — try higher
            best_q = mid
            low = mid + 1
        else:
            # Too large — go lower
            high = mid - 1

        # Early exit: if range is exhausted
        if low > high:
            break

        # Early exit: if we're under target and within 5%
        if size <= target and (target - size) / target < 0.05:
            best_q = mid
            break

    return best_q, None


# ── Auto-rotate by EXIF Orientation ──────────────────────────────────────

# EXIF orientation → PIL transpose operation
_EXIF_ORIENTATION_MAP = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,       # 90° CW + flip H
    6: Image.Transpose.ROTATE_270,      # 90° CW
    7: Image.Transpose.TRANSVERSE,      # 90° CCW + flip H
    8: Image.Transpose.ROTATE_90,       # 90° CCW
}


def _apply_auto_rotate(img: Image.Image) -> Image.Image:
    """Read EXIF Orientation and rotate/flip the image to normal orientation.

    Returns a new Image (does not modify the original).
    """
    exif_data = img.getexif()
    if not exif_data:
        return img

    orientation = exif_data.get(0x0112)  # EXIF tag for Orientation
    if not orientation or orientation == 1:
        return img

    transpose_op = _EXIF_ORIENTATION_MAP.get(orientation)
    if transpose_op is None:
        return img

    rotated = img.transpose(transpose_op)
    _normalize_exif_orientation(rotated)
    return rotated


def _normalize_exif_orientation(img: Image.Image) -> None:
    """Reset the EXIF Orientation tag to 1 after pixel rotation is applied.

    Without this, outputs carry a stale Orientation value and get
    double-rotated by downstream viewers. Best-effort.
    """
    if not _HAS_PIEXIF:
        # PIL fallback: rewrite the tag via getexif() and hand the bytes to
        # the save path through img.info, same as the piexif branch does.
        try:
            exif = img.getexif()
            if 0x0112 in exif:
                exif[0x0112] = 1
                img.info["exif"] = exif.tobytes()
        except Exception:
            pass  # best-effort — never fail rotation for a metadata fix
        return
    try:
        exif_bytes = img.info.get("exif")
        if not exif_bytes:
            return
        exif_dict = piexif.load(exif_bytes)
        if 0x0112 in exif_dict.get("0th", {}):
            exif_dict["0th"][0x0112] = 1
            img.info["exif"] = piexif.dump(exif_dict)
    except Exception:
        pass  # best-effort — never fail rotation for a metadata fix


# ── RAW image loading ─────────────────────────────────────────────────────

# Demosaic algorithm name → rawpy.DemosaicAlgorithm (resolved lazily since
# rawpy is imported inside _load_raw_via_rawpy). "auto" omits the parameter
# and lets libraw pick its default (AHD for Bayer, X-Trans aware for Fuji).
_DEMOSAIC_NAMES = {
    "ahd": "AHD",
    "vng": "VNG",
    "ppg": "PPG",
    "dcb": "DCB",
    "dht": "DHT",
    "amaze": "AMAZE",
}

# Output color space name → rawpy.ColorSpace member. Wider gamuts than sRGB
# (AdobeRGB / ProPhotoRGB) preserve more color from the sensor for editing
# workflows; they are NOT auto-ICC-tagged (PIL cannot fabricate those
# profiles) — tag in your editor. Case-insensitive.
_RAW_COLOR_SPACES = {
    "srgb": "sRGB",
    "adobergb": "Adobe",
    "prophotorgb": "ProPhoto",
}

_SRGB_ICC: Optional[bytes] = None
_SRGB_ICC_LOCK = threading.Lock()


def _sRGB_icc_bytes() -> bytes:
    """Lazy-cached sRGB ICC profile bytes (used to tag RAW decode output).

    rawpy decodes to sRGB pixels (output_color=ColorSpace.sRGB) but the numpy
    → Image.fromarray result carries no profile, so outputs were untagged and
    viewers assumed a profile — inconsistent color across apps. Tagging the
    decode makes every downstream save carry the profile automatically
    (engine._save_image forwards img.info['icc_profile']); --scrub still
    strips it.
    """
    global _SRGB_ICC
    if _SRGB_ICC is None:
        with _SRGB_ICC_LOCK:
            if _SRGB_ICC is None:
                from PIL import ImageCms
                _SRGB_ICC = ImageCms.ImageCmsProfile(
                    ImageCms.createProfile("sRGB")).tobytes()
    return _SRGB_ICC


def _load_raw_via_rawpy(path: str, options: ProcessOptions) -> Image.Image:
    """Decode a RAW file using rawpy (libraw). Returns a PIL Image."""
    import rawpy
    import numpy as np

    cs = (getattr(options, "raw_color_space", "sRGB") or "sRGB").lower()
    try:
        output_color = rawpy.ColorSpace[_RAW_COLOR_SPACES[cs]]
    except (KeyError, AttributeError):
        raise RawOptionError(
            f"unknown raw color space {cs!r}; "
            f"choose from sRGB/AdobeRGB/ProPhotoRGB")

    bps = 16 if getattr(options, "raw_16bit", False) else 8
    kwargs = dict(
        use_camera_wb=True,
        half_size=options.raw_half_size,
        no_auto_bright=not options.raw_auto_bright,
        output_color=output_color,
        gamma=(2.222, 4.5),
        output_bps=bps,
    )
    name = (getattr(options, "raw_demosaic", "auto") or "auto").lower()
    if name != "auto":
        try:
            kwargs["demosaic_algorithm"] = rawpy.DemosaicAlgorithm[
                _DEMOSAIC_NAMES[name]]
        except (KeyError, AttributeError):
            raise RawOptionError(
                f"unknown raw demosaic algorithm {name!r}; "
                f"choose from auto/ahd/vng/ppg/dcb/dht/amaze")

    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(**kwargs)
    except Exception as e:
        # A demosaic/color-space choice that this rawpy build can't honor
        # (e.g. AMAZE needs the GPL3 demosaic pack) must surface as a CLEAR
        # error — silently falling back to the sips decoder would swap in a
        # different renderer and change brightness/colors without telling
        # anyone (the old broad except swallowed it).
        if type(e).__name__ == "NotSupportedError" or "requires" in str(e):
            raise RawOptionError(
                f"raw option not supported by this rawpy build: {e}") from e
        raise
    if bps == 16:
        # PIL cannot represent 16-bit RGB — show the 8-bit view (>> 8 is
        # byte-identical to rawpy's own 8-bit output) and park the uint16
        # array on the image for the 16-bit TIFF save path.
        img = Image.fromarray((rgb >> 8).astype(np.uint8))
        img._raw_16bit = rgb
    else:
        img = Image.fromarray(rgb)
    if cs == "srgb" and not getattr(options, "scrub", False):
        # the decoded pixels ARE sRGB — tag them so the output carries a
        # profile instead of being untagged (wider gamuts are left untagged)
        img.info["icc_profile"] = _sRGB_icc_bytes()
    return img


def _load_raw_via_sips(path: str) -> Image.Image:
    """Decode a RAW file using macOS sips. Returns a PIL Image."""
    tmp = tempfile.NamedTemporaryFile(suffix=".tiff", delete=False)
    tmp.close()
    try:
        subprocess.run(
            ["sips", "-s", "format", "tiff", path, "--out", tmp.name],
            check=True, capture_output=True, timeout=120,
        )
        img = Image.open(tmp.name)
        img._temp_raw_tiff = tmp.name  # keep reference for cleanup
        return img
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, UnidentifiedImageError):
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        raise


def _load_raw(path: str, options: ProcessOptions) -> Image.Image:
    """Load a RAW image via rawpy, falling back to macOS sips.

    Decode-level failures (bad file, missing rawpy) fall back to sips, but
    RawOptionError (a user-chosen option this build can't honor, e.g. AMAZE
    demosaic) propagates — silently re-rendering with sips would change
    brightness/colors behind the user's back.
    """
    # Try rawpy first (most capable)
    rawpy_error: Optional[BaseException] = None
    try:
        return _load_raw_via_rawpy(path, options)
    except RawOptionError:
        raise
    except Exception as rawpy_err:
        # Keep the real rawpy failure — the final error used to be a bare
        # "Cannot decode RAW file" that hid import errors / corrupt files.
        rawpy_error = rawpy_err

    # Fallback: macOS sips
    try:
        return _load_raw_via_sips(path)
    except Exception as sips_err:
        raise UnidentifiedImageError(
            f"Cannot decode RAW file: {path}. "
            f"rawpy: {rawpy_error}; sips: {sips_err}. "
            f"Install rawpy (pip install rawpy) for best RAW support."
        ) from sips_err


# ── Core processing ─────────────────────────────────────────────────────────

def _get_image(input_path: str, options: Optional[ProcessOptions] = None) -> Image.Image:
    """Open an image, handling RAW and HEIC with fallbacks."""
    ext = Path(input_path).suffix.lower()

    # ── RAW files ────────────────────────────────────────────────────
    if ext in RAW_EXTENSIONS:
        if options is None:
            options = ProcessOptions()
        return _load_raw(input_path, options)

    # ── Standard / HEIC via PIL ──────────────────────────────────────
    try:
        return Image.open(input_path)
    except UnidentifiedImageError:
        # HEIC fallback: try sips (macOS) when pillow-heif not installed
        if ext in (".heic", ".heif"):
            if _HAS_PILLOW_HEIF:
                raise  # pillow-heif is installed but failed — propagate error
            if sys.platform == "darwin":
                png_path = _heic_to_png_via_sips(input_path)
                if png_path:
                    try:
                        img = Image.open(png_path)
                        img._temp_png = png_path
                        return img
                    except Exception:
                        os.unlink(png_path)
                        raise
            raise UnidentifiedImageError(
                f"Cannot open HEIC file: {input_path}. "
                f"Install pillow-heif: pip install pillow-heif"
            )
        raise


def _save_image(img: Image.Image, output_path: str, fmt: str,
                options: ProcessOptions):
    """Save image with format-specific options."""
    save_kwargs = {}

    # ── 16-bit RAW decode → 16-bit TIFF ────────────────────────────────────
    # PIL cannot represent/save 16-bit RGB, so the uint16 array parked at
    # decode is written directly via tifffile (optional dep; clear error
    # guidance when missing). Only a PURE RAW→TIFF conversion reaches this
    # branch — any pipeline transform replaces the image and drops the
    # attribute, falling back to the 8-bit save (which is correct: JPEG is
    # 8-bit anyway).
    raw16 = getattr(img, "_raw_16bit", None)
    if raw16 is not None and fmt == "TIFF" and not options.scrub:
        try:
            import tifffile
        except ImportError:
            tifffile = None
        if tifffile is not None:
            icc = img.info.get("icc_profile")
            tifffile.imwrite(output_path, raw16, photometric="rgb",
                             iccprofile=icc)
            return
        raise RuntimeError(
            "16-bit TIFF output needs tifffile. "
            "Install: pip install tifffile")

    if fmt in ("JPEG", "WebP", "HEIC", "AVIF"):
        save_kwargs["quality"] = options.quality

    if fmt == "JPEG":
        save_kwargs["optimize"] = options.optimize
        save_kwargs["progressive"] = options.progressive
        # chroma subsampling: explicit choice, PIL default (4:2:0) otherwise
        if options.jpeg_subsampling not in _JPEG_SUBSAMPLING:
            global _SUBSAMPLING_WARNED
            if not _SUBSAMPLING_WARNED:
                _SUBSAMPLING_WARNED = True
                print(f"⚠️  unknown jpeg_subsampling "
                      f"{options.jpeg_subsampling!r}; falling back to 4:2:0 "
                      f"(valid: 444/422/420)", file=sys.stderr)
        save_kwargs["subsampling"] = _JPEG_SUBSAMPLING.get(
            options.jpeg_subsampling, 2)
        # Convert RGBA → RGB for JPEG (JPEG doesn't support alpha).
        # The fresh background starts with an empty info dict — copy img.info
        # (EXIF/ICC) onto it so metadata survives the alpha flatten.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            background.info = dict(img.info)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
    elif fmt == "PNG":
        save_kwargs["optimize"] = options.optimize
    elif fmt == "TIFF":
        save_kwargs["compression"] = "lzw"

    # Preserve EXIF if requested and moving between compatible formats.
    # Precedence: scrub > preserve_exif > strip_gps/date_shift/gpx.
    exif = None
    gpx_pos = getattr(options, "_gpx_pos", None)
    if (not options.scrub and options.preserve_exif
            and hasattr(img, 'info') and 'exif' in img.info):
        exif = img.info['exif']
        if options.strip_gps or options.date_shift or gpx_pos:
            if _HAS_PIEXIF:
                try:  # one load/dump handles GPS strip + date shift + geo-tag
                    exif_dict = piexif.load(exif)
                    if options.strip_gps:
                        exif_dict.pop("GPS", None)
                    if options.date_shift:
                        _shift_exif_dict(exif_dict, parse_date_shift(options.date_shift))
                    if gpx_pos:
                        from .gpx import to_dms_rational
                        lat, lon = gpx_pos
                        exif_dict.setdefault("GPS", {})[piexif.GPSIFD.GPSLatitude] = \
                            to_dms_rational(lat)
                        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = \
                            b"N" if lat >= 0 else b"S"
                        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = \
                            to_dms_rational(lon)
                        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = \
                            b"E" if lon >= 0 else b"W"
                    exif = piexif.dump(exif_dict)
                except Exception:
                    exif = None  # can't rewrite EXIF — drop all to be safe
            else:
                exif = None  # no piexif — drop all EXIF rather than leak GPS
        if exif is not None and fmt in ("JPEG", "TIFF", "HEIC", "WebP"):
            save_kwargs["exif"] = exif
        # PNG is deliberately not in that list: Pillow's PNG encoder ignores
        # the exif kwarg, so "preserving" it there would be a silent lie.
        # PNG outputs keep ICC (via icc_profile below) but drop EXIF.

    if options.scrub:
        # strip ALL metadata segments regardless of preserve_exif
        for key in ("exif", "icc_profile", "comment"):
            img.info.pop(key, None)

    # JPEG doesn't auto-write icc_profile from img.info — pass it explicitly
    # (scrub already popped it above, so this only fires for kept profiles)
    if img.info.get("icc_profile"):
        save_kwargs["icc_profile"] = img.info["icc_profile"]

    if fmt in PIL_WRITABLE:
        img.save(output_path, format=fmt, **save_kwargs)
    elif fmt == "HEIC" and sys.platform == "darwin":
        # HEIC fallback: use sips on macOS when pillow-heif not installed
        tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_png.close()
        try:
            img.save(tmp_png.name, format="PNG")
            subprocess.run(
                ["sips", "-s", "format", "heic", tmp_png.name, "--out", output_path],
                check=True, capture_output=True, timeout=30,
            )
            if exif is not None:
                # sips rebuilds the container from the PNG — every metadata
                # segment is gone. Say so instead of silently dropping EXIF
                # the user asked to preserve (install pillow-heif to keep it).
                print("⚠️  sips HEIC conversion drops all metadata "
                      "(preserve_exif ineffective; install pillow-heif)",
                      file=sys.stderr)
        finally:
            if os.path.exists(tmp_png.name):
                os.unlink(tmp_png.name)
    elif fmt == "AVIF":
        raise RuntimeError(
            f"Cannot save as {fmt}. "
            f"Install pillow-avif for AVIF support: pip install pillow-avif"
        )
    else:
        raise RuntimeError(
            f"Cannot save as {fmt}. "
            f"Install pillow-heif for cross-platform HEIC support: pip install pillow-heif"
        )


def process_image(input_path: str, options: ProcessOptions) -> ProcessResult:
    """
    Process a single image: compress, resize, and/or convert format.

    Args:
        input_path: Absolute path to the source image.
        options: ProcessOptions specifying quality, format, resize, etc.

    Returns:
        ProcessResult with before/after stats.
    """
    # Canonicalize output format once, library-path-safe: CLI already
    # normalizes ('jpeg' → 'JPEG') but direct `batch_process` callers pass
    # whatever case they like. A fresh replace() copy avoids mutating the
    # caller's object and keeps every downstream read canonical. Unsupported
    # formats get a clear error instead of a bare KeyError leaking into the
    # per-file error text.
    fmt = _canonical_format(options.output_format)
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported output format {options.output_format!r}; "
            f"supported: {sorted(SUPPORTED_FORMATS)}")
    # replace() only carries dataclass fields — batch_process attaches the
    # {seq} counter and the reserved output path dynamically, so hand them
    # over explicitly (same dynamic-attribute hand-off as _gpx_pos below).
    seq_counter = getattr(options, '_seq_counter', None)
    preassigned_output = getattr(options, '_preassigned_output', None)
    preassigned_sized = getattr(options, '_preassigned_sized_outputs', None)
    options = replace(options, output_format=fmt)
    options._seq_counter = seq_counter
    options._preassigned_output = preassigned_output
    options._preassigned_sized_outputs = preassigned_sized

    input_fmt = _format_from_path(input_path)
    input_size = 0
    src_stat = None    # for --keep-mtime
    temp_files = []    # sips-fallback temp files parked on the loaded image

    try:
        # ── Resolve a named lens profile into the manual lens_* params ─────
        # Inside the try so an unknown profile surfaces as a clear per-file
        # error rather than killing the batch. Runs before the lens-correction
        # block; explicit lens_* values (nonzero / non-empty) win over the
        # profile.
        if options.lens_profile:
            from .lensprofile import load_lens_profile
            prof = load_lens_profile(options.lens_profile)
            if prof is None:
                raise ValueError(
                    f"unknown lens profile {options.lens_profile!r}; "
                    f"see 'photo-s lens-profile list'")
            if not options.lens_distort and prof.get("distort"):
                options.lens_distort = float(prof["distort"])
            if not options.lens_vignette and prof.get("vignette"):
                options.lens_vignette = prof["vignette"]
            if not options.lens_ca and prof.get("ca"):
                options.lens_ca = prof["ca"]

        # Stat inside the try: an input deleted after the batch scan must
        # surface as a per-file error, not kill the whole sequential batch.
        input_size = os.path.getsize(input_path)
        src_stat = os.stat(input_path)
        img = _get_image(input_path, options)
        # Record sips-fallback temp paths NOW: pipeline transforms swap in
        # fresh Image objects that no longer carry the attribute, so cleanup
        # that only inspects the final image would leak the file.
        for attr in ('_temp_png', '_temp_raw_tiff'):
            parked = getattr(img, attr, None)
            if parked:
                temp_files.append(parked)
        input_dims = img.size  # (width, height)

        # ── Plugin hook: pre_process ────────────────────────────────────────
        from .plugin import run_pre_process
        from .hooks import PluginContext
        ctx = PluginContext(input_path=input_path, options=options)
        run_pre_process(img, options, ctx)

        # ── Auto-rotate ─────────────────────────────────────────────────────
        if options.auto_rotate:
            img = _apply_auto_rotate(img)

        # ── Lens correction (v1.7.0): geometry first, before any pixel
        # grading, so every later step sees undistorted coordinates.
        if options.lens_distort:
            from .lens import apply_distortion
            img = apply_distortion(img, options.lens_distort)
        if options.lens_ca:
            from .lens import apply_ca_fix, parse_ca
            img = apply_ca_fix(img, *parse_ca(options.lens_ca))
        if options.lens_vignette:
            from .lens import apply_vignette_fix, parse_vignette_fix
            img = apply_vignette_fix(img, *parse_vignette_fix(
                options.lens_vignette))

        # ── Auto-straighten (horizon leveling, optional opencv) ────────────
        straightened = False
        if options.auto_straighten:
            from .straighten import apply_auto_straighten
            img, straightened = apply_auto_straighten(
                img, options.max_straighten_angle)

        # ── LOG / flat-profile recovery (decode source before grading) ─────
        if options.log_curve:
            from .logcurve import apply_log_recovery
            img = apply_log_recovery(img, options.log_curve.upper())

        # ── Color management (sRGB profile / CMYK flatten) ──────────────────
        img = apply_color_management(img, options.srgb, options.flatten_cmyk)

        # ── AI auto-tone (plugin provider only, v2.3) ────────────────────────
        # Runs before the manual tone block so explicit flags refine on top
        # of the predicted grade. No built-in fallback — silently skipping a
        # requested AI pass would hide a missing plugin from the user.
        # v2.4: providers exposing auto_tone_params hand back parameters
        # (9 global fields + local adjustments) which go through the REAL
        # engine pipeline via photo_s.autotone — the pixel-returning path
        # silently dropped 6 of the 9 predicted fields (simplified render).
        if options.auto_tone:
            from .plugin import find_provider
            provider = find_provider("auto_tone")
            if provider is None:
                raise RuntimeError(
                    "--auto-tone needs the auto-tone plugin "
                    "(pip install photo-s-plugin-auto-tone[model]; "
                    "zero-model rule-based alternative: 'photo-s suggest')")
            if hasattr(provider, "auto_tone_params"):
                from .autotone import apply_auto_tone_params
                img = apply_auto_tone_params(
                    img, provider.auto_tone_params(options.auto_tone, ctx))
            else:
                img = provider.auto_tone(img, options.auto_tone, ctx)

        # ── Tone & color ─────────────────────────────────────────────────────
        img = apply_tone_adjustments(
            img,
            brightness=options.brightness,
            contrast=options.contrast,
            saturation=options.saturation,
            gamma=options.gamma,
            sharpen=options.sharpen,
            grayscale=options.grayscale,
            sepia=options.sepia,
        )

        # ── LUT color grade (plugin provider preferred, else built-in .cube)
        if options.lut_file:
            from .plugin import find_provider
            provider = find_provider("lut")
            if provider is not None:
                img = provider.lut(img, options.lut_file, ctx)
            else:
                from .lut import apply_lut
                img = apply_lut(img, options.lut_file)

        # ── White balance (Kelvin / reference image / G-M tint) ─────────────
        img = apply_white_balance(
            img, temp=options.wb_temp, reference=options.wb_reference,
            tint=options.wb_tint)

        # ── Exposure: EV compensation + auto-exposure normalization ─────────
        img = apply_exposure(img, ev=options.ev,
                             auto_exposure=options.auto_exposure)

        # ── Lightroom-direction grading (v1.6.0) ────────────────────────────
        # Manual levels → point curves → vibrance → 3-way color grading,
        # inserted after WB/exposure (LR reference order) and before denoise.
        # The existing tone/LUT steps stay in place (backward compatibility).
        if options.levels:
            from .grade import apply_levels, _parse_levels
            img = apply_levels(img, *_parse_levels(options.levels))
        if options.curves:
            from .grade import apply_curves, _parse_curves
            img = apply_curves(img, _parse_curves(options.curves))
        if options.clarity:
            from .grade import apply_clarity
            img = apply_clarity(img, options.clarity)
        if options.texture:
            from .grade import apply_texture
            img = apply_texture(img, options.texture)
        if options.dehaze:
            from .grade import apply_dehaze
            img = apply_dehaze(img, options.dehaze)
        if options.vibrance:
            from .grade import apply_vibrance
            img = apply_vibrance(img, options.vibrance)
        if options.hsl:
            from .grade import apply_hsl, _parse_hsl
            img = apply_hsl(img, _parse_hsl(options.hsl))
        if options.point_color:
            from .grade import apply_point_color, _parse_point_color
            img = apply_point_color(img, _parse_point_color(options.point_color))
        if options.color_grading:
            from .grade import apply_color_grading, _parse_color_grading
            z = _parse_color_grading(options.color_grading)
            img = apply_color_grading(
                img, shadows=z.get("shadows"), midtones=z.get("midtones"),
                highlights=z.get("highlights"))

        # ── Local adjustments under masks (v1.7.0) ─────────────────────────
        # After global grading, before denoise: the mask selects pixels,
        # apply_local blends the adjusted result back onto the graded image.
        if options.mask_adjust:
            from .mask import (MaskError, apply_local, parse_mask_adjust,
                               parse_masks, render_mask)
            adjusts = parse_mask_adjust(options.mask_adjust)
            specs = {s.name: s for s in parse_masks(options.masks)}
            for name, adjust in adjusts.items():
                spec = specs.get(name)
                if spec is None:
                    raise MaskError(
                        f"mask_adjust references unknown mask {name!r} "
                        f"(defined: {','.join(specs) or 'none'})")
                # refs: combo masks (v1.8) resolve their referenced names
                m = render_mask(spec, img.width, img.height, img=img,
                                refs=specs)
                img = apply_local(img, m, adjust)

        # ── Denoise (SCUNet plugin provider preferred, else opencv NLM) ─────
        if options.denoise:
            from .plugin import find_provider
            provider = find_provider("denoise")
            if provider is not None:
                img = provider.denoise(img, options.denoise, ctx)
            else:
                from .denoise import apply_denoise
                img = apply_denoise(img, options.denoise)

        # ── Auto levels (histogram stretch) ─────────────────────────────────
        if options.auto_levels:
            img = apply_auto_levels(img)

        # ── Highlight recovery (LR-style, after grading so it catches the
        # final tones; before geometry) ─────────────────────────────────────
        if options.highlight_recovery:
            from .grade import apply_highlight_recovery
            img = apply_highlight_recovery(img, options.highlight_recovery)

        # ── Finishing looks (vignette/grain) before geometry ────────────────
        if options.vignette:
            from .grade import apply_vignette, _parse_vignette
            img = apply_vignette(img, *_parse_vignette(options.vignette))
        if options.grain:
            from .grade import apply_grain, _parse_grain
            img = apply_grain(img, *_parse_grain(options.grain))

        # ── Composition: crop → rotate → flip (before resize) ───────────────
        img = apply_crop(img, options.crop)
        img = apply_crop_ratio(img, options.crop_ratio)
        img = apply_rotate(img, options.rotate_degrees, fill=options.rotate_bg)
        img = apply_flip(img, options.flip)

        # ── Resize ──────────────────────────────────────────────────────────
        if options.scale_percent and options.scale_percent != 100:
            w, h = img.size
            new_w = int(w * options.scale_percent / 100)
            new_h = int(h * options.scale_percent / 100)
            if new_w < 1 or new_h < 1:
                raise ValueError(
                    f"scale_percent {options.scale_percent}% would shrink "
                    f"{w}x{h} to {new_w}x{new_h}; use a larger percentage")
            img = img.resize((new_w, new_h), Image.LANCZOS)
        elif options.max_width or options.max_height or options.max_pixels:
            w, h = img.size
            ratio = 1.0
            if options.max_width and w > options.max_width:
                ratio = min(ratio, options.max_width / w)
            if options.max_height and h > options.max_height:
                ratio = min(ratio, options.max_height / h)
            if options.max_pixels and max(w, h) > options.max_pixels:
                ratio = min(ratio, options.max_pixels / max(w, h))
            if ratio < 1.0:
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)

        # ── Letterbox pad (after resize so the ratio applies to final dims) ─
        img = apply_pad(img, options.pad_ratio, bg=options.pad_bg)

        # ── Print export: center-crop to aspect ratio, exact print pixels ───
        if options.print_size:
            p_w, p_h, p_dpi = _parse_print_size(options.print_size)
            print_px = (int(round(p_w * p_dpi)), int(round(p_h * p_dpi)))
            img = apply_crop_ratio(img, f"{p_w}:{p_h}")
            img = img.resize(print_px, Image.LANCZOS)

        output_dims = img.size

        # ── Watermark ───────────────────────────────────────────────────────
        if options.watermark_text or options.watermark_image:
            from .watermark import apply_text_watermark, apply_image_watermark
            if options.watermark_image:
                img = apply_image_watermark(
                    img, options.watermark_image,
                    position=options.watermark_position,
                    opacity=options.watermark_opacity,
                )
            if options.watermark_text:
                img = apply_text_watermark(
                    img, options.watermark_text,
                    position=options.watermark_position,
                    opacity=options.watermark_opacity,
                )

        # ── Face blur (privacy mask, optional opencv) ───────────────────────
        # After watermark so the mask covers the final saved pixels; before
        # EXIF extraction so the EXIF the pipeline writes is untouched. Only
        # pixels change (.info is copied by the module), so the EXIF
        # preservation in _save_image is unaffected.
        if options.blur_faces:
            from .faceblur import apply_face_blur
            img, _faces = apply_face_blur(
                img, mode=options.blur_faces,
                margin=options.blur_faces_margin or 20)

        # ── Export sharpening (LR-style output stage) ───────────────────────
        # After resize/pad/print-size so the USM radius scales with the FINAL
        # output resolution; after watermark/blur so it sharpens the saved
        # pixels (the lr-look preset uses this instead of mid-pipeline
        # sharpen). Multi-size outputs copy this already-sharpened full-res
        # image and downscale — acceptable (sharpen-then-downscale).
        if options.export_sharpen:
            from .grade import apply_export_sharpen
            img = apply_export_sharpen(img, options.export_sharpen)

        # ── Cutout / background removal (v2.1.0) ────────────────────────────
        # Last pixel stage: after resize/watermark/sharpen so the alpha cuts
        # the FINAL saved pixels; before EXIF extraction (reads .info only)
        # and _save_image (RGBA-safe: PNG/WebP/TIFF write alpha natively).
        # JPEG output is rejected here (fail fast, before any segmentation)
        # — flattening the cutout to white would be a silent failure.
        if options.cutout:
            from .cutout import apply_cutout, parse_cutout
            if _canonical_format(options.output_format) == "JPEG":
                raise ValueError(
                    f"cutout ({options.cutout}) needs a format that supports "
                    f"transparency (PNG/WebP/TIFF/AVIF/HEIC)")
            img = apply_cutout(img, parse_cutout(options.cutout))

        # ── Extract EXIF metadata for smart rename ──────────────────────────
        exif_meta = _extract_exif_metadata(img, input_path)

        # ── GPS geo-tag from a GPX track (matches input EXIF datetime) ──────
        if options.gpx_trace:
            from .gpx import position_at
            gpx_pos = None
            if exif_meta.get("date") and exif_meta.get("time"):
                try:
                    dt_str = (f"{exif_meta['date']} "
                              f"{exif_meta['time'].replace('-', ':')}")
                    ts = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    # EXIF datetimes are camera-local; GPX timestamps are UTC.
                    # OffsetTime (when present) bridges the two frames.
                    gpx_pos = position_at(options.gpx_trace, ts,
                                          tz_offset=exif_meta.get("tz_offset"))
                except ValueError:
                    gpx_pos = None
            else:
                # A photo with no readable EXIF datetime silently skipped
                # geo-tagging before — the user never learned why the output
                # had no GPS data.
                print(f"⚠️  geotag: {input_path} 没有可读的 EXIF 拍摄时间, "
                      f"跳过 GPS 标记 (no EXIF datetime; GPS not written)",
                      file=sys.stderr)
            options._gpx_pos = gpx_pos  # consumed by _save_image

        # ── Output path ─────────────────────────────────────────────────────
        # Seq counter is passed from batch_process for {seq} support; a
        # batch-reserved output path (parallel-collision-safe) wins when set.
        seq_counter = getattr(options, '_seq_counter', None)
        output_path = _get_output_path(
            input_path, options.output_format, options.output_dir,
            options.prefix, options.suffix, options.overwrite,
            rename_pattern=options.rename_pattern,
            exif_meta=exif_meta,
            seq_counter=seq_counter,
            folder_pattern=options.folder_pattern,
            preassigned=getattr(options, '_preassigned_output', None),
        )

        # Ensure output directory exists. A bare filename (no directory
        # component) has dirname == "" — makedirs("") raises a baffling
        # "[Errno 2] No such file or directory: ''", so only mkdir when there
        # is something to create (the CWD already exists).
        out_parent = os.path.dirname(output_path)
        if out_parent:
            os.makedirs(out_parent, exist_ok=True)

        # ── Auto-tune quality if target size is set ─────────────────────────
        achieved_quality = options.quality
        target_warning = ""

        if (options.target_size_bytes
                and options.output_format in _QUALITY_AWARE_FORMATS):
            achieved_quality, min_possible = _find_quality_for_target(
                img, options.output_format, options,
            )
            if (min_possible is not None
                    and min_possible > options.target_size_bytes):
                target_warning = (
                    f" (目标 {format_size(options.target_size_bytes)} "
                    f"无法达到, 最小可能体积 {format_size(min_possible)})"
                    f" (target {format_size(options.target_size_bytes)} "
                    f"unreachable, min possible {format_size(min_possible)})"
                )

        # ── Save (with optional multi-size output) ──────────────────────────
        def _do_save(img_obj, path, fmt, opts):
            _save_image(img_obj, path, fmt, opts)
            if options.sync_date:
                # EXIF datetime wins over keep_mtime; falls back to keep_mtime
                # when the saved file has no readable datetime.
                ts = _exif_datetime_timestamp(path)
                if ts is not None:
                    os.utime(path, (ts, ts))
                    return
            if options.keep_mtime and src_stat is not None:
                os.utime(path, (src_stat.st_atime, src_stat.st_mtime))

        # _save_image only reads the save-affecting fields; replace() keeps
        # them all in sync automatically (quality is swapped for the achieved
        # value when target-size tuning ran).
        save_options_base = replace(options, quality=achieved_quality)
        # GPS lookup result travels through the save options (consumed in
        # _save_image alongside strip_gps/date_shift)
        save_options_base._gpx_pos = getattr(options, "_gpx_pos", None)

        if options.output_sizes:
            # Multi-size mode: generate one file per size
            preassigned_sized = (options._preassigned_sized_outputs
                                 if options._preassigned_sized_outputs else [])
            for size_idx, (label, mw, mh) in enumerate(options.output_sizes):
                sized_img = img.copy()
                orig_w, orig_h = sized_img.size
                ratio = 1.0
                if mw and orig_w > mw:
                    ratio = min(ratio, mw / orig_w)
                if mh and orig_h > mh:
                    ratio = min(ratio, mh / orig_h)
                if ratio < 1.0:
                    new_w, new_h = int(orig_w * ratio), int(orig_h * ratio)
                    sized_img = sized_img.resize((new_w, new_h), Image.LANCZOS)

                size_suffix = f"{options.suffix}_{label}"
                size_path = _get_output_path(
                    input_path, options.output_format, options.output_dir,
                    options.prefix, size_suffix, options.overwrite,
                    folder_pattern=options.folder_pattern,
                    exif_meta=exif_meta,
                    # batch_process reserved one path per size up front —
                    # same parallel-collision protection as the main output.
                    preassigned=(preassigned_sized[size_idx]
                                 if size_idx < len(preassigned_sized)
                                 else None),
                )
                _do_save(sized_img, size_path, options.output_format,
                         save_options_base)

            # Main output is the "full" size
            _do_save(img, output_path, options.output_format, save_options_base)
        else:
            _do_save(img, output_path, options.output_format, save_options_base)

        output_size = os.path.getsize(output_path)

        # ── Optional SSIM evaluation (input vs output) ──────────────────────
        ssim = None
        if options.evaluate and output_size > 0:
            try:
                from .metrics import compute_ssim
                ssim = compute_ssim(input_path, output_path)
            except Exception:
                ssim = None  # evaluation must never fail the whole process

        # ── Optional blur heuristic (scored on the INPUT) ────────────────────
        blur = None
        if options.blur_score and output_size > 0:
            try:
                from .metrics import compute_blur_score
                blur = compute_blur_score(input_path)
            except Exception:
                blur = None  # scoring must never fail the whole process

        # ── Remove original if requested ────────────────────────────────────
        if (options.remove_original
                and os.path.abspath(input_path) != os.path.abspath(output_path)
                and output_size > 0):
            try:
                os.unlink(input_path)
            except OSError:
                pass  # Can't delete — don't fail the whole operation

        result = ProcessResult(
            input_path=input_path,
            output_path=output_path,
            input_size=input_size,
            output_size=output_size,
            input_format=input_fmt,
            output_format=options.output_format,
            input_dims=input_dims,
            output_dims=output_dims,
            success=True,
            achieved_quality=achieved_quality,
            error=target_warning if target_warning else "",
            ssim=ssim,
            blur_score=blur,
            auto_straightened=straightened,
        )

        # ── Plugin hook: post_process ───────────────────────────────────────
        ctx.output_path = output_path
        from .plugin import run_post_process
        run_post_process(result, ctx)

        return result

    except Exception as e:
        return ProcessResult(
            input_path=input_path,
            output_path="",
            input_size=input_size,
            output_size=0,
            input_format=input_fmt,
            output_format=options.output_format,
            input_dims=(0, 0),
            output_dims=(0, 0),
            success=False,
            error=str(e),
        )
    finally:
        # sips-fallback temp files (recorded right after _get_image) — the
        # finally also covers mid-pipeline exceptions, not just the happy path.
        for temp_file in temp_files:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


def batch_process(
    input_paths: List[str],
    options: ProcessOptions,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
    trace_callback: Optional[Callable[[str, "ProcessResult"], None]] = None,
    per_file_options: Optional[Callable[[str, ProcessOptions],
                                        ProcessOptions]] = None,
) -> BatchResult:
    """
    Process multiple images in batch, with optional parallel execution.

    Args:
        input_paths: List of absolute paths to source images.
        options: ProcessOptions for all images.
        progress_callback: Optional callback(current_index, total, current_path, status)
                           for progress reporting.
        cancel_checker: Optional callable returning True when cancellation has
                        been requested. In-flight images finish; images not yet
                        started are skipped (marked as cancelled failures in
                        parallel mode, omitted from results in sequential mode).
        per_file_options: Optional callable(path, options) -> options called
                          per file BEFORE output-path reservation, so per-file
                          fields (e.g. GUI per-photo masks) are consistent with
                          the pre-assigned output paths. Must be thread-safe.
                          The hook always receives the original (pre-loop)
                          options, never the previous file's result, so
                          returning it unchanged for a path is always safe.

    Returns:
        BatchResult aggregating all individual results.
    """
    import threading

    # ── Resume: skip inputs whose output already exists ─────────────────────
    if options.resume:
        if options.rename_pattern or options.folder_pattern:
            # predicting {date}/{camera}/{seq} names or {year}/{month}
            # subfolders requires per-file EXIF and a fragile seq model —
            # resume is only reliable without rename/organize patterns
            print("⚠️  --resume 与智能重命名/子文件夹分类不兼容，已忽略 resume "
                  "(ignored, incompatible with --rename/--organize)", file=sys.stderr)
        else:
            kept = []
            for path in input_paths:
                predicted = _get_output_path(
                    path, options.output_format, options.output_dir,
                    options.prefix, options.suffix, overwrite=True)
                if os.path.exists(predicted):
                    continue
                kept.append(path)
            skipped = len(input_paths) - len(kept)
            if skipped:
                print(f"⏭️  resume: 跳过 {skipped} 个已存在输出 "
                      f"(skipped existing outputs)", file=sys.stderr)
            input_paths = kept

    total = len(input_paths)
    if total == 0:
        return BatchResult(results=[], total_input_size=0, total_output_size=0,
                           success_count=0, fail_count=0)

    # ── Pre-allocate sequence numbers and output paths (avoids races) ──────
    # Each image gets its own options copy with pre-assigned seq counter.
    # replace() carries every field automatically — a new ProcessOptions field
    # reaches per-image processing without touching this code (the historical
    # silent-loss trap: fields added but not copied here).
    per_image_options = []
    seq = 1
    # Reserve output paths up front against this set: the exists() dedup in
    # _get_output_path alone races — two same-stem inputs from different
    # source dirs both see "not exists" and collide under parallel workers.
    # Paths depending on per-file EXIF (rename/folder patterns) can't be
    # predicted here, so those keep their per-file computation.
    reserved_paths = set()
    predictable = not options.rename_pattern and not options.folder_pattern
    base_options = options
    for path in input_paths:
        if per_file_options is not None:
            # 钩子始终收到未变异的 base options：本函数逐文件复用 options
            # 变量，若传上一文件的结果，未注入的字段会跨文件泄漏（GUI
            # per-photo 蒙版曾因此让无蒙版照片继承上一张的蒙版）。
            options = per_file_options(path, base_options)
        opts_copy = replace(options, jobs=1)
        # Pre-assign sequence number (mutable list for process_image compatibility)
        opts_copy._seq_counter = [seq]
        seq += 1
        if not options.overwrite and predictable:
            opts_copy._preassigned_output = _get_output_path(
                path, options.output_format, options.output_dir,
                options.prefix, options.suffix, overwrite=False,
                reserved=reserved_paths)
            # Multi-size derivatives collide the same way (two same-stem
            # inputs both derive "photo_small.jpg" before either writes),
            # so reserve one path per labeled size up front too.
            if options.output_sizes:
                opts_copy._preassigned_sized_outputs = [
                    _get_output_path(
                        path, options.output_format, options.output_dir,
                        options.prefix, f"{options.suffix}_{label}",
                        overwrite=False, reserved=reserved_paths)
                    for label, _mw, _mh in options.output_sizes
                ]
        per_image_options.append(opts_copy)

    # ── Process images ─────────────────────────────────────────────────────
    # Worker count comes from the batch-level options — after the per-file
    # hook loop above, the local `options` variable holds the LAST file's
    # options, so a hook overriding jobs used to leak into this line.
    workers = max(1, min(base_options.jobs, total))
    results: List[Optional[ProcessResult]] = [None] * total
    progress_lock = threading.Lock()
    completed_count = [0]  # mutable for closure

    def process_one(idx: int) -> ProcessResult:
        path = input_paths[idx]

        # Skip work if cancellation was requested (parallel mode: pending
        # futures drain quickly instead of processing every remaining file)
        if cancel_checker is not None and cancel_checker():
            return ProcessResult(
                input_path=path, output_path="",
                input_size=0, output_size=0,
                input_format=_format_from_path(path),
                output_format=options.output_format,
                input_dims=(0, 0), output_dims=(0, 0),
                success=False, error="已取消 Cancelled",
            )

        opts = per_image_options[idx]
        result = process_image(path, opts)

        if trace_callback is not None:
            trace_callback(path, result)

        if progress_callback:
            with progress_lock:
                completed_count[0] += 1
                try:
                    progress_callback(
                        completed_count[0], total, path,
                        "tuning" if opts.target_size_bytes else "",
                    )
                except TypeError:
                    progress_callback(completed_count[0], total, path)

        return result

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {executor.submit(process_one, i): i for i in range(total)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    results[idx] = ProcessResult(
                        input_path=input_paths[idx], output_path="",
                        input_size=0, output_size=0,
                        input_format="", output_format=options.output_format,
                        input_dims=(0, 0), output_dims=(0, 0),
                        success=False, error=str(e),
                    )
    else:
        for i in range(total):
            if cancel_checker is not None and cancel_checker():
                break  # Remaining images are omitted from results entirely
            results[i] = process_one(i)

    # Filter out None (shouldn't happen, but be safe)
    final_results = [r for r in results if r is not None]

    if progress_callback:
        try:
            progress_callback(total, total, "", "")
        except TypeError:
            progress_callback(total, total, "")

    total_in = sum(r.input_size for r in final_results)
    total_out = sum(r.output_size for r in final_results)
    success = sum(1 for r in final_results if r.success)

    return BatchResult(
        results=final_results,
        total_input_size=total_in,
        total_output_size=total_out,
        success_count=success,
        fail_count=len(final_results) - success,
    )


# ── Utility functions ───────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    """Format byte count to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def scan_directory(directory: str, recursive: bool = False) -> List[str]:
    """
    Scan a directory for supported image files.

    Args:
        directory: Directory path to scan.
        recursive: If True, scan subdirectories too.

    Returns:
        List of absolute paths to image files found.
    """
    image_paths = []
    base = Path(directory)

    if recursive:
        # One os.walk pass with a case-insensitive extension check. The old
        # code rglob'd once per extension × case (44 tree walks) — quadratic
        # pain on big NAS trees.
        for root, _dirs, files in os.walk(directory):
            for name in files:
                if os.path.splitext(name)[1].lower() in ALL_INPUT_EXTENSIONS:
                    image_paths.append(os.path.abspath(os.path.join(root, name)))
    else:
        for entry in base.iterdir():
            if entry.is_file() and entry.suffix.lower() in ALL_INPUT_EXTENSIONS:
                image_paths.append(str(entry.absolute()))

    # Deduplicate and sort
    return sorted(set(image_paths))


# ── EXIF date shifting & reports ────────────────────────────────────────────

_DATE_SHIFT_RE = re.compile(r"([+-]?)(\d+(?:\.\d+)?)([dhms])")


def parse_date_shift(spec: str) -> timedelta:
    """Parse a compound offset like "-5h30m", "+2h", "1d" into a timedelta.

    A leading "-"/"+" sign applies to the whole spec ("-5h30m" = -5.5 hours).
    Raises ValueError if nothing parseable is present.
    """
    text = str(spec).strip()
    if not text:
        raise ValueError("empty date shift")
    sign = -1 if text.startswith("-") else 1
    total = 0.0
    matched = False
    for m in _DATE_SHIFT_RE.finditer(text):
        value = float(m.group(2))
        unit = m.group(3)
        multiplier = {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        total += sign * value * multiplier
        matched = True
    if not matched:
        raise ValueError(f"cannot parse date shift {spec!r}")
    return timedelta(seconds=total)


def _shift_exif_datetime(value, delta: timedelta) -> Optional[bytes]:
    """Shift a b"YYYY:MM:DD HH:MM:SS" EXIF value; None if unparseable."""
    try:
        text = value.decode("utf-8", errors="ignore").strip()
        dt = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError, TypeError):
        return None
    return (dt + delta).strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")


def _shift_exif_dict(exif_dict: dict, delta: timedelta) -> None:
    """Shift DateTimeOriginal/DateTimeDigitized/DateTime in an exif dict in place."""
    if not _HAS_PIEXIF:
        return
    targets = [
        (exif_dict.get("Exif", {}), piexif.ExifIFD.DateTimeOriginal),
        (exif_dict.get("Exif", {}), piexif.ExifIFD.DateTimeDigitized),
        (exif_dict.get("0th", {}), piexif.ImageIFD.DateTime),
    ]
    for ifd, tag_id in targets:
        if tag_id in ifd and ifd[tag_id]:
            shifted = _shift_exif_datetime(ifd[tag_id], delta)
            if shifted is not None:
                ifd[tag_id] = shifted


def _exif_datetime_timestamp(path: str) -> Optional[float]:
    """Epoch timestamp from a file's EXIF DateTimeOriginal (fallback DateTime)."""
    if not _HAS_PIEXIF:
        return None
    try:
        exif_dict = piexif.load(path)
    except Exception:
        return None
    value = None
    if exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal):
        value = exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal]
    elif exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime):
        value = exif_dict["0th"][piexif.ImageIFD.DateTime]
    if not value:
        return None
    shifted = _shift_exif_datetime(value, timedelta(0))
    if shifted is None:
        return None
    try:
        return datetime.strptime(shifted.decode("utf-8"),
                                 "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _write_report(path: str, result: "BatchResult") -> None:
    """Write a CSV report of a batch result (one row per file, incl. failures)."""
    import csv

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["input", "output", "status", "input_format",
                         "output_format", "input_width", "input_height",
                         "output_width", "output_height", "input_size",
                         "output_size", "quality", "ssim", "blur_score",
                         "error"])
        for r in result.results:
            iw, ih = r.input_dims or (0, 0)
            ow, oh = r.output_dims or (0, 0)
            writer.writerow([
                r.input_path, r.output_path,
                "ok" if r.success else "error",
                r.input_format, r.output_format,
                iw, ih, ow, oh,
                r.input_size, r.output_size,
                r.achieved_quality,
                f"{r.ssim:.4f}" if r.ssim is not None else "",
                f"{r.blur_score:.1f}" if r.blur_score is not None else "",
                r.error,
            ])


# ── EXIF editing ──────────────────────────────────────────────────────────

# Rating / keywords / title are stored in EXIF UserComment (0x9286, a
# standard field piexif supports natively) as a machine-readable payload:
#     PhotoS: rating=4 keywords=beach,trip title=...
# The UserComment 8-byte charset header ("ASCII\0\0\0") is included so
# ExifTool / viewers render it as plain text. Human text in an existing
# UserComment is preserved before the PhotoS segment.
_USERCOMMENT_PREFIX = "PhotoS:"

# Eager init for thread safety. rating/keywords/title are NOT in this map —
# they live in the UserComment payload (handled in apply_exif_tags).
_EXIF_TAG_MAP = {} if not _HAS_PIEXIF else {
    "artist":       ("0th", piexif.ImageIFD.Artist),
    "copyright":    ("0th", piexif.ImageIFD.Copyright),
    "description":  ("0th", piexif.ImageIFD.ImageDescription),
    "caption":      ("0th", piexif.ImageIFD.ImageDescription),  # alias
    "make":         ("0th", piexif.ImageIFD.Make),
    "model":        ("0th", piexif.ImageIFD.Model),
    "software":     ("0th", piexif.ImageIFD.Software),
    "datetime":     ("Exif", piexif.ExifIFD.DateTimeOriginal),
    "date":         ("Exif", piexif.ExifIFD.DateTimeOriginal),  # CLI alias for --date
}

# Non-ASCII EXIF fields: name → (kind, ifd_name, tag_id). kind is one of
# "ascii" (bytes), "short" (int), "rational" ((num, den) parsed from text).
# Handled by _apply_typed_exif_tag; '' / None clears the tag.
_EXIF_TYPED_TAGS = {} if not _HAS_PIEXIF else {
    "lens":     ("ascii",    "Exif", piexif.ExifIFD.LensModel),
    "iso":      ("short",    "Exif", piexif.ExifIFD.ISOSpeedRatings),
    "fnumber":  ("rational", "Exif", piexif.ExifIFD.FNumber),
    "aperture": ("rational", "Exif", piexif.ExifIFD.FNumber),   # alias
    "shutter":  ("rational", "Exif", piexif.ExifIFD.ExposureTime),
    "focal":    ("rational", "Exif", piexif.ExifIFD.FocalLength),
}


def _get_exif_tag_map() -> dict:
    """Return the EXIF tag name → (ifd_name, tag_id) mapping."""
    return _EXIF_TAG_MAP


def _exif_bytes(value) -> bytes:
    """piexif returns BYTE/UNDEFINED values as int tuples — normalize to bytes."""
    if isinstance(value, (tuple, list)):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    return b""


def _parse_rational_str(value):
    """Parse '2.8', 'f/2.8', '1/250', '50' → (num, den) ints, or None.

    Used for RATIONAL EXIF fields (FNumber / ExposureTime / FocalLength).
    Zero, negative, non-numeric and out-of-uint32-range values are rejected
    so a bad input silently skips the field instead of corrupting the file.
    """
    s = str(value).strip().lower()
    if s.startswith("f/"):
        s = s[2:].strip()
    if not s:
        return None
    try:
        frac = Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None
    if frac <= 0:
        return None
    if frac.numerator > 0xFFFFFFFF or frac.denominator > 0xFFFFFFFF:
        return None
    return (frac.numerator, frac.denominator)


def _fmt_fnumber(value) -> str:
    """RATIONAL (num, den) → one-decimal f-number ('2.8'); '' on bad data."""
    try:
        num, den = value
        f = float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    return f"{f:.1f}" if f > 0 else ""


def _fmt_shutter(value) -> str:
    """RATIONAL (num, den) → '2' (≥1 s) or '1/250' (<1 s); '' on bad data."""
    try:
        num, den = float(value[0]), float(value[1])
        if num <= 0 or den <= 0:
            return ""
        secs = num / den
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return ""
    if secs >= 1:
        return str(int(round(secs)))
    return f"1/{int(round(den / num))}"


def _parse_usercomment(text: str) -> dict:
    """Extract rating/keywords/title from a 'PhotoS: ...' UserComment segment."""
    out = {"rating": None, "keywords": [], "title": ""}
    if _USERCOMMENT_PREFIX not in text:
        return out
    seg = text.split(_USERCOMMENT_PREFIX, 1)[1]
    tokens = seg.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" not in tok:
            i += 1
            continue
        k, _, v = tok.partition("=")
        v = v.strip()
        if k == "rating":
            try:
                out["rating"] = int(v)
            except ValueError:
                pass
        elif k == "keywords":
            out["keywords"] = [x.strip() for x in v.split(",") if x.strip()]
        elif k == "title":
            # title is written last and may contain spaces: join the rest
            out["title"] = " ".join([v] + tokens[i + 1:]).strip()
            break
        i += 1
    return out


def _write_usercomment(exif_dict: dict, rating=None, keywords=None,
                       title=None, existing_text: str = "") -> None:
    """Merge rating/keywords/title into the UserComment payload, preserving
    any pre-existing human text. Mutates exif_dict in place."""
    seg = _USERCOMMENT_PREFIX
    if rating is not None:
        seg += f" rating={int(rating)}"
    if keywords:
        seg += f" keywords={','.join(keywords)}"
    if title:
        seg += f" title={title}"
    human = existing_text.split(_USERCOMMENT_PREFIX, 1)[0].strip(" ,|")
    if seg.strip() == _USERCOMMENT_PREFIX:
        seg = ""  # all fields cleared — drop the PhotoS: segment
    parts = [p for p in (human, seg) if p]
    if parts:
        payload = (" | ".join(parts)).encode("utf-8")
        exif_dict["Exif"][piexif.ExifIFD.UserComment] = (
            b"ASCII\x00\x00\x00" + payload)
    else:
        # nothing left at all — remove the tag entirely
        exif_dict["Exif"].pop(piexif.ExifIFD.UserComment, None)


def _apply_typed_exif_tag(exif_dict: dict, spec, value) -> bool:
    """Write one typed (non-ASCII-map) EXIF tag. Mutates exif_dict.

    ``""`` / None pops the tag — same clear semantics as
    rating/keywords/title. Unparseable values are skipped silently.
    Returns True when the tag was written or cleared.
    """
    kind, ifd_name, tag_id = spec
    if value is None or str(value).strip() == "":
        exif_dict[ifd_name].pop(tag_id, None)
        return True
    if kind == "ascii":
        exif_dict[ifd_name][tag_id] = str(value).encode("utf-8")
        return True
    if kind == "short":
        try:
            v = int(value)
        except (TypeError, ValueError):
            return False
        if not 0 <= v <= 0xFFFF:
            return False
        exif_dict[ifd_name][tag_id] = v
        return True
    if kind == "rational":
        parsed = _parse_rational_str(value)
        if parsed is None:
            return False
        exif_dict[ifd_name][tag_id] = parsed
        return True
    return False


def _apply_gps_tag(exif_dict: dict, spec) -> bool:
    """Write GPS coordinates ("lat,lon") into the GPS IFD. Mutates exif_dict.

    Mirrors the GPX branch in ``_save_image`` (same N/S/E/W refs + DMS
    rationals). Returns True when written; False when the spec is unparseable
    or out of range — silently skipped, matching the typed-tag bad-value
    convention so one bad value can't crash a whole batch.
    """
    if not _HAS_PIEXIF:
        return False
    try:
        lat, lon = (float(x.strip()) for x in str(spec).split(",", 1))
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    from .gpx import to_dms_rational
    gps = exif_dict.setdefault("GPS", {})
    gps[piexif.GPSIFD.GPSLatitude] = to_dms_rational(lat)
    gps[piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
    gps[piexif.GPSIFD.GPSLongitude] = to_dms_rational(lon)
    gps[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
    return True


def apply_exif_tags(image_path: str, tags: dict) -> str:
    """Write EXIF tags to an existing image file. Modifies the file in-place.

    Requires piexif. tags is a dict of {name: value} where name is one of:
    artist, copyright, description/caption, make, model, software,
    datetime/date, title, keywords (comma list), rating (int 0-5),
    lens (ASCII), iso (int), fnumber/aperture ('2.8' / 'f/2.8'),
    shutter ('1/250' / '2'), focal ('50'), gps ('lat,lon' — both files get
    the same coordinates; out-of-range/unparseable values are skipped).
    rating/keywords/title are packed into EXIF UserComment (PhotoS: payload);
    the rest are standard EXIF fields. All tags are written in a single
    load/dump/insert pass. ``rating=None`` / ``keywords=""`` / ``title=""``
    explicitly CLEAR the corresponding field (the PhotoS: segment is
    dropped entirely when all three end up empty); ``""``/None on a typed
    field (lens/iso/fnumber/shutter/focal) removes that EXIF tag. Typed
    fields with unparseable values are skipped.

    Returns a message string describing what was written.
    """
    if not _HAS_PIEXIF:
        return "⚠️  piexif not installed — cannot write EXIF."

    if not tags:
        return ""

    exif_dict = piexif.load(image_path)

    # rating / keywords / title → UserComment payload
    meta = _parse_usercomment(_usercomment_text_from_dict(exif_dict))
    if "rating" in tags:
        if tags["rating"] is None:
            meta["rating"] = None  # explicit clear (undo etc.)
        else:
            try:
                meta["rating"] = int(tags["rating"])
            except (TypeError, ValueError):
                pass
    if "keywords" in tags:
        meta["keywords"] = [k.strip() for k in str(tags["keywords"]).split(",")
                            if k.strip()]
    if "title" in tags:
        meta["title"] = str(tags["title"])
    if any(k in tags for k in ("rating", "keywords", "title")):
        human = _usercomment_text_from_dict(exif_dict).split(
            _USERCOMMENT_PREFIX, 1)[0].strip(" ,|")
        _write_usercomment(exif_dict, meta["rating"], meta["keywords"],
                           meta["title"], existing_text=human)

    # remaining tags → standard EXIF fields (ASCII map + typed fields)
    written = [n for n in tags if n in ("rating", "keywords", "title")]
    tag_map = _get_exif_tag_map()
    for name, value in tags.items():
        if name in ("rating", "keywords", "title"):
            continue
        if name in tag_map:
            ifd_name, tag_id = tag_map[name]
            exif_dict[ifd_name][tag_id] = str(value).encode("utf-8")
            written.append(name)
        elif name in _EXIF_TYPED_TAGS:
            if _apply_typed_exif_tag(exif_dict, _EXIF_TYPED_TAGS[name], value):
                written.append(name)

    # GPS coordinates ("lat,lon") → GPS IFD (same refs/rationals as gpx path)
    if "gps" in tags and _apply_gps_tag(exif_dict, tags["gps"]):
        written.append("gps")

    piexif.insert(piexif.dump(exif_dict), image_path)

    return f"EXIF written: {', '.join(written)}" if written else ""


def _usercomment_text_from_dict(exif_dict: dict) -> str:
    """UserComment text from an already-loaded piexif dict (no re-load)."""
    raw = _exif_bytes(exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment))
    if not raw:
        return ""
    # The 8-byte charset header (EXIF spec) must be detected at BYTES level —
    # the old post-decode check could never match, so every apply_exif_tags
    # rewrite piled another "ASCII\0\0\0" prefix onto the comment.
    if raw[:8] in (b"ASCII\x00\x00\x00", b"UNICODE\x00",
                   b"JIS\x00\x00\x00\x00\x00", b"\x00" * 8):
        raw = raw[8:]
    text = raw.decode("utf-8", errors="ignore").strip("\x00")
    return text.strip()


def read_exif_metadata(path: str) -> dict:
    """Read key metadata from an image file (best-effort, never raises).

    Returns dict with keys: date, time, year, month, day, camera, make, iso,
    focal, lens, fnumber, shutter, original (stem), rating (int or None),
    keywords (list[str]), title, caption. Missing values are '' / None / [].
    """
    base = {
        "date": "", "time": "", "year": "", "month": "", "day": "",
        "camera": "", "make": "", "iso": "", "focal": "",
        "lens": "", "fnumber": "", "shutter": "",
        "original": Path(path).stem,
        "rating": None, "keywords": [], "title": "", "caption": "",
    }
    try:
        with Image.open(path) as img:
            base.update(_extract_exif_metadata(img, path))
    except Exception:
        pass

    if _HAS_PIEXIF:
        try:
            d = piexif.load(path)
            base.update(_parse_usercomment(_usercomment_text_from_dict(d)))
            cap = d.get("0th", {}).get(piexif.ImageIFD.ImageDescription)
            if cap:
                base["caption"] = _exif_bytes(cap).decode(
                    "utf-8", errors="ignore").strip("\x00")
            exif_ifd = d.get("Exif", {})
            lens = _exif_bytes(exif_ifd.get(piexif.ExifIFD.LensModel))
            if lens:
                base["lens"] = lens.decode(
                    "utf-8", errors="ignore").strip("\x00 ")
            fnumber = _fmt_fnumber(exif_ifd.get(piexif.ExifIFD.FNumber))
            if fnumber:
                base["fnumber"] = fnumber
            shutter = _fmt_shutter(exif_ifd.get(piexif.ExifIFD.ExposureTime))
            if shutter:
                base["shutter"] = shutter
        except Exception:
            pass

    return base
