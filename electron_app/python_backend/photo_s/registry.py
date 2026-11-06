"""
PhotoS - Official Plugin Registry

The catalog of plugins maintained by the PhotoS project. Each official plugin
is a separate PyPI distribution ``photo-s-plugin-<name>`` so it can version
independently of the core package.

Weight metadata (url / sha256 / size) deliberately does NOT live here — it
changes with each plugin release and is exposed by the installed plugin via
``PhotoSPlugin.weight_specs()``. This registry only maps official names to
their distribution + catalog fields, so ``photo-s plugin list --json`` can show
available-but-not-installed plugins.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import __version__


@dataclass(frozen=True)
class OfficialPlugin:
    """A single official plugin catalog entry."""
    name: str
    pypi_distribution: str       # e.g. "photo-s-plugin-scunet"
    description: str
    min_photo_s_version: str     # core version requirement
    requires: Optional[Tuple[str, ...]] = None  # pip names the plugin needs


OFFICIAL_PLUGINS: Dict[str, OfficialPlugin] = {
    "scunet": OfficialPlugin(
        name="scunet",
        pypi_distribution="photo-s-plugin-scunet",
        description="SCUNet 强降噪 Strong denoising (ONNX via onnxruntime)",
        min_photo_s_version="1.0.0",
        requires=("onnxruntime>=1.16.0",),
    ),
    "lut": OfficialPlugin(
        name="lut",
        pypi_distribution="photo-s-plugin-lut",
        description="LUT 调色: 四面体插值 + 电影预设 Tetrahedral .cube "
                    "grading + film presets (pure numpy)",
        min_photo_s_version="1.3.0",
    ),
    "auto-tone": OfficialPlugin(
        name="auto-tone",
        pypi_distribution="photo-s-plugin-auto-tone",
        description="AI 自动调色: CLIP+MLP 预测 9 字段 Lightroom 参数，"
                    "RAG 增强 + 可选 Qwen3-VL 美学评分/修图建议",
        min_photo_s_version="1.7.0",
        requires=("numpy", "pillow", "torch>=2.1", "open_clip_torch>=2.20"),
    ),
}


def get_official(name: str) -> Optional[OfficialPlugin]:
    """Registry lookup by name (case-sensitive). None if unknown."""
    return OFFICIAL_PLUGINS.get(name)


def to_dict(o: OfficialPlugin) -> dict:
    """JSON-serializable catalog dict for ``plugin list`` / ``plugin info``."""
    return {
        "name": o.name,
        "pypi_distribution": o.pypi_distribution,
        "description": o.description,
        "min_photo_s_version": o.min_photo_s_version,
        "requires": list(o.requires) if o.requires else None,
    }


def _version_tuple(text: str):
    """'1.9.0' → (1, 9, 0); None for non-numeric parts (non-semver)."""
    parts = str(text).strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def version_ok(o: OfficialPlugin) -> bool:
    """True iff the running core version meets the plugin's requirement.

    Non-semver requirement strings fail CLOSED (used to pass open) — a
    malformed requirement the registry can't interpret must not silently
    wave an incompatible plugin through.
    """
    cur = _version_tuple(__version__)
    req = _version_tuple(o.min_photo_s_version)
    if cur is None or req is None:
        return False
    return cur >= req
