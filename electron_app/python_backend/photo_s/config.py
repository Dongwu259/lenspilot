"""
PhotoS - Configuration File Support

Loads default options from a TOML config file (photo-s.toml).

Precedence: command-line args > config file > built-in defaults.

Search order for the config file:
  1. Explicit --config PATH
  2. ./photo-s.toml, walking up parent directories
  3. $XDG_CONFIG_HOME/photo-s/config.toml
  4. ~/.config/photo-s/config.toml
"""

import os
import sys
from pathlib import Path
from typing import Optional

from .engine import ProcessOptions, _resolve_folder_pattern, _canonical_format

CONFIG_FILENAME = "photo-s.toml"

# Fields in [options] mapped 1:1 onto ProcessOptions attributes.
_SIMPLE_FIELDS = {
    "quality": "quality",
    "output_format": "output_format",
    "output_dir": "output_dir",
    "max_width": "max_width",
    "max_height": "max_height",
    "scale_percent": "scale_percent",
    "max_pixels": "max_pixels",
    "strip_gps": "strip_gps",
    "keep_mtime": "keep_mtime",
    "evaluate": "evaluate",
    "progressive": "progressive",
    "jpeg_subsampling": "jpeg_subsampling",
    "raw_demosaic": "raw_demosaic",
    "raw_color_space": "raw_color_space",
    "raw_16bit": "raw_16bit",
    "overwrite": "overwrite",
    "prefix": "prefix",
    "suffix": "suffix",
    "remove_original": "remove_original",
    "auto_rotate": "auto_rotate",
    "optimize": "optimize",
    "rename_pattern": "rename_pattern",
    "watermark_text": "watermark_text",
    "watermark_image": "watermark_image",
    "watermark_position": "watermark_position",
    "watermark_opacity": "watermark_opacity",
    "jobs": "jobs",
    # Sprint 3 transform/metadata options
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "gamma": "gamma",
    "sharpen": "sharpen",
    "export_sharpen": "export_sharpen",
    "grayscale": "grayscale",
    "sepia": "sepia",
    "auto_levels": "auto_levels",
    "highlight_recovery": "highlight_recovery",
    "wb": "wb_temp",
    "wb_from": "wb_reference",
    "ev": "ev",
    "auto_exposure": "auto_exposure",
    "log_curve": "log_curve",
    "denoise": "denoise",
    "auto_straighten": "auto_straighten",
    "max_straighten_angle": "max_straighten_angle",
    "print_size": "print_size",
    "crop": "crop",
    "crop_ratio": "crop_ratio",
    "rotate_degrees": "rotate_degrees",
    "rotate_bg": "rotate_bg",
    "flip": "flip",
    "pad": "pad_ratio",          # config key "pad" → field pad_ratio
    "pad_bg": "pad_bg",
    "date_shift": "date_shift",
    "scrub": "scrub",
    "sync_date": "sync_date",
    "blur_score": "blur_score",
    "resume": "resume",
    "gpx_trace": "gpx_trace",
    "srgb": "srgb",
    "flatten_cmyk": "flatten_cmyk",
    "lens_distort": "lens_distort",
    "lens_vignette": "lens_vignette",
    "lens_ca": "lens_ca",
    "lens_profile": "lens_profile",
}


def _parse_size(size_str) -> Optional[int]:
    """Lenient size parser: '5MB'/'800KB'/int → bytes. Returns None if unparseable."""
    if size_str is None:
        return None
    if isinstance(size_str, (int, float)):
        return int(size_str)
    s = str(size_str).strip().upper()
    if not s:
        return None
    multiplier = 1
    for suffix, mult in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3),
                         ("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3)):
        if s.endswith(suffix):
            multiplier = mult
            s = s[:-len(suffix)]
            break
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return None


def find_config() -> Optional[str]:
    """Locate a config file via the search order, or return None."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        candidate = parent / CONFIG_FILENAME
        if candidate.is_file():
            return str(candidate)

    for home in (os.environ.get("XDG_CONFIG_HOME"),
                 str(Path.home() / ".config")):
        if not home:
            continue
        candidate = Path(home) / "photo-s" / "config.toml"
        if candidate.is_file():
            return str(candidate)
    return None


def load_config(path: str) -> dict:
    """Load a TOML config file into a dict.

    Uses stdlib tomllib on Python ≥3.11, falls back to tomli on older Pythons.
    """
    if sys.version_info >= (3, 11):
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    try:
        import tomli
    except ImportError:
        raise RuntimeError(
            "Python < 3.11 needs tomli to read config files. "
            "Install: pip install tomli"
        )
    with open(path, "rb") as f:
        return tomli.load(f)


def save_config(path: str, text: str) -> None:
    """Write config text to path atomically (temp file + rename).

    A direct write truncated the existing config first — a crash mid-write
    destroyed the user's settings. The rename is atomic on POSIX and Windows.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def default_config_text() -> str:
    """Return a commented default photo-s.toml template."""
    return """# PhotoS 配置文件 Configuration file
# 优先级 Priority: 命令行参数 CLI > 配置文件 config > 默认值 defaults
#
# 用法 Usage:
#   photo-s compress *.jpg --config ./photo-s.toml
#   或把本文件命名为 photo-s.toml 放在工作目录, 自动生效
#   (or name this file photo-s.toml in the working dir for auto-discovery)

[options]
# 界面语言 UI language: "en" | "zh" | "auto" (default auto)
#language = "auto"
# 输出质量 Output quality 1-100
#quality = 85
# 目标格式 Target format: JPEG PNG WebP TIFF HEIC AVIF
#output_format = "JPEG"
# 输出目录 Output directory
#output_dir = "compressed"
# 目标文件体积 Target file size (auto-tune quality): 500KB, 2MB
#target_size = "5MB"
# 最长边像素上限 Max pixels on longest side (downscale only)
#max_pixels = 8000
# 保留 EXIF metadata (true/false)
#preserve_exif = true
# 移除 GPS 位置信息 Strip GPS location data
#strip_gps = false
# 保留原文件修改时间 Preserve source modification time
#keep_mtime = false
# 计算 SSIM 质量评分 Compute SSIM score
#evaluate = false
# 并行线程数 Parallel workers
#jobs = 4
# 输出文件名后缀 Output suffix
#suffix = "_compressed"
# 文字水印 Text watermark
#watermark_text = ""
#watermark_position = "BOTTOM_RIGHT"
#watermark_opacity = 50
# 子文件夹模板 Folder pattern: date, camera, date-camera, 或 {year}/{month}
#folder_pattern = ""
# ── 影调与构图 Tone & composition ──
# 亮度/对比度/饱和度/伽马/锐化 multipliers (1.0 = 不变)
#brightness = 1.0
#contrast = 1.0
#saturation = 1.0
#gamma = 1.0
#sharpen = 1.0
#grayscale = false
#sepia = false
# 自动色阶 (2% 裁切直方图拉伸)
#auto_levels = false
# 白平衡色温 Kelvin (如 5600)
#wb = 5600
# 白平衡参考图路径
#wb_from = "gray-card.jpg"
# 曝光补偿 EV (档位, 2^EV 增益)
#ev = -0.5
# 自动曝光目标均值亮度 (0-1)
#auto_exposure = 0.45
# LOG/平面文件还原曲线 (SLOG3 CLOG3 LOGC3 DLOG VLOG HLG)
#log_curve = "SLOG3"
# NLM 降噪强度 3-20 (需安装 photo-s-tools[enhance])
#denoise = 10
# 自动扶正地平线 (需安装 photo-s-tools[enhance])
#auto_straighten = false
#max_straighten_angle = 10
# 打印尺寸 (中心裁剪 + 精确像素)
#print_size = "8x10@300dpi"
#crop = "800x600+100+50"
#crop_ratio = "16:9"
#rotate_degrees = 90
#flip = "h"
#pad = "16:9"
#pad_bg = "#000000"
# ── 元数据 Metadata ──
# EXIF 日期偏移 Date shift, e.g. "-5h30m"
#date_shift = "-5h30m"
# 清除全部元数据 Strip ALL metadata
#scrub = false
# 输出时间设为 EXIF 拍摄时间 Set mtime from EXIF
#sync_date = false
"""


_SIMPLE_TYPES = (int, float, bool, str)


def _annotation_target(field: str):
    """Resolve a ProcessOptions field annotation to a plain type.

    Unwraps Optional[...]; returns None for anything outside int/float/bool/str
    (those values are passed through without coercion).
    """
    anno = ProcessOptions.__dataclass_fields__[field].type
    if getattr(anno, "__origin__", None) is None:
        return anno if anno in _SIMPLE_TYPES else None
    args = [a for a in getattr(anno, "__args__", ()) if a is not type(None)]
    if len(args) == 1 and args[0] in _SIMPLE_TYPES:
        return args[0]
    return None


def _coerce_value(key: str, field: str, value):
    """Coerce a config value to the ProcessOptions field's annotated type.

    Quoted TOML scalars (jobs = "4") are converted; values that cannot
    convert raise a ValueError naming the config key, instead of crashing
    deep in the processing pipeline.
    """
    target = _annotation_target(field)
    if target is None:
        return value
    try:
        if target is bool:
            if isinstance(value, bool):
                return value
            v = str(value).strip().lower()
            if v in ("true", "yes", "on", "1"):
                return True
            if v in ("false", "no", "off", "0"):
                return False
            raise ValueError
        if target is int:
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                if value.is_integer():
                    return int(value)
                raise ValueError
            return int(str(value).strip())
        if target is float:
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, (int, float)):
                return float(value)
            return float(str(value).strip())
        return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"配置项 '{key}' 的值 {value!r} 无效 (应为 {target.__name__}) — "
            f"invalid value for config key '{key}' (expected {target.__name__})"
        ) from None


def config_language(cfg) -> Optional[str]:
    """Read the ``language`` key from a config dict (en/zh/auto → en|zh|None).

    ``language`` is NOT a ProcessOptions field, so it lives outside
    ``_SIMPLE_FIELDS`` and is consumed by ``i18n.resolve_language`` rather
    than ``apply_config``.
    """
    opts = cfg.get("options", {}) if isinstance(cfg, dict) else {}
    value = opts.get("language")
    if value is None:
        return None
    lang = str(value).strip().lower()
    return lang if lang in ("en", "zh") else None


def apply_config(cfg, options: ProcessOptions) -> ProcessOptions:
    """Apply the [options] section of a config dict onto a ProcessOptions.

    Only overrides fields present in the config; unknown keys are ignored
    (including ``language``, which is read by ``config_language``).
    Mutates and returns the same options object.
    """
    opts = cfg.get("options", {}) if isinstance(cfg, dict) else {}

    for key, field in _SIMPLE_FIELDS.items():
        if key in opts and opts[key] is not None:
            setattr(options, field, _coerce_value(key, field, opts[key]))

    if "preserve_exif" in opts and opts["preserve_exif"] is not None:
        options.preserve_exif = _coerce_value(
            "preserve_exif", "preserve_exif", opts["preserve_exif"])

    if "raw_auto_bright" in opts and opts["raw_auto_bright"] is not None:
        options.raw_auto_bright = _coerce_value(
            "raw_auto_bright", "raw_auto_bright", opts["raw_auto_bright"])

    if "target_size" in opts:
        size = _parse_size(opts["target_size"])
        if size is not None:
            options.target_size_bytes = size

    if "folder_pattern" in opts and opts["folder_pattern"]:
        options.folder_pattern = _resolve_folder_pattern(str(opts["folder_pattern"]))

    # case-insensitive format from config ("png" / "WEBP" → "PNG" / "WebP")
    options.output_format = _canonical_format(options.output_format)

    return options
