"""
PhotoS - Preset Configuration Management

Save, load, list, and delete named preset configurations.
Presets are stored as JSON files in ~/.photos/presets/.
"""

import json
import os
from pathlib import Path
from typing import List, Optional
from dataclasses import asdict

from .engine import ProcessOptions

PRESETS_DIR = Path.home() / ".photos" / "presets"

# Built-in presets (name → options dict). Always available with zero setup —
# no user files written. A user-saved preset of the same name overrides the
# builtin (user wins), and delete_preset on a builtin is a no-op.
BUILTIN_PRESETS = {
    "lr-look": {
        "_description": (
            "LR 风格出片：S 曲线（提黑位/压高光）+ 自然饱和 + 导出锐化，"
            "在 rawpy 平淡基线之上接近 Lightroom 默认渲染"),
        # gentle S-curve: raised blacks, lifted midtones, soft highlight
        # compression — the contrast lives in the curve, not a linear slider
        "curves": "0,0;24,30;64,76;128,138;192,204;236,244;255,255",
        "contrast": 1.0,
        "saturation": 1.06,
        "vibrance": 0.08,
        # LR-style output-stage USM (radius scales with output resolution),
        # instead of the mid-pipeline ImageEnhance sharpen
        "export_sharpen": 1.0,
    },
}


# Fields that destroy or overwrite user data. Presets are SHARED artifacts
# (import_presets_from_json is an official flow), so a preset file must never
# be able to turn "here's my grading look" into "delete the originals".
# These stay controlled by explicit CLI flags / interactive confirmation only.
_DESTRUCTIVE_FIELDS = frozenset({
    "remove_original",   # deletes source files after processing
    "overwrite",         # clobbers existing outputs
})


def _filter_preset_fields(data: dict) -> dict:
    """Keep only known ProcessOptions fields minus the destructive ones."""
    return {
        k: v for k, v in data.items()
        if k in ProcessOptions.__dataclass_fields__
        and k not in _DESTRUCTIVE_FIELDS
    }


def _builtin_options(name: str) -> Optional[ProcessOptions]:
    """Resolve a built-in preset to ProcessOptions, or None if unknown."""
    data = BUILTIN_PRESETS.get(name)
    if data is None:
        return None
    return ProcessOptions(**{
        k: v for k, v in data.items()
        if k in ProcessOptions.__dataclass_fields__
    })


def _ensure_dir():
    """Ensure the presets directory exists."""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def _preset_path(name: str) -> Path:
    """Get the file path for a preset name."""
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    return PRESETS_DIR / f"{safe}.json"


def list_presets() -> List[str]:
    """Return sorted list of available preset names (user + built-in).

    A user-saved preset shadows a built-in of the same name.
    """
    user = []
    if PRESETS_DIR.exists():
        for f in PRESETS_DIR.glob("*.json"):
            name = f.stem
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                desc = data.get("_description", "")
                user.append((name, desc))
            except Exception:
                pass
    user_names = {n for n, _ in user}
    builtin = [(n, d.get("_description", ""))
               for n, d in BUILTIN_PRESETS.items() if n not in user_names]
    return [f"{n} — {d}" if d else n for n, d in sorted(user + builtin)]


def save_preset(name: str, options: ProcessOptions, description: str = ""):
    """Save a ProcessOptions config as a named preset.

    Destructive fields (remove_original / overwrite) are not persisted — a
    preset is a reusable "look", and shared presets carrying delete flags
    are a data-loss trap.
    """
    _ensure_dir()
    data = asdict(options)
    for key in _DESTRUCTIVE_FIELDS:
        data.pop(key, None)
    data["_description"] = description
    data["_version"] = "1.0"
    # Remove non-serializable fields
    data.pop("output_sizes", None)  # handled separately
    if options.output_sizes:
        data["output_sizes"] = options.output_sizes
    _preset_path(name).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                  encoding="utf-8")


def load_preset(name: str) -> Optional[ProcessOptions]:
    """Load a named preset and return a ProcessOptions instance.

    User-saved presets win; built-in presets are the fallback. Returns None
    if neither exists.
    """
    path = _preset_path(name)
    if not path.exists():
        # Try fuzzy match
        for f in PRESETS_DIR.glob("*.json"):
            if f.stem.lower() == name.lower().replace(" ", "_"):
                path = f
                break
        else:
            return _builtin_options(name)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _builtin_options(name)

    if not isinstance(data, dict):
        return _builtin_options(name)

    # Extract known fields; ignore metadata
    data.pop("_description", None)
    data.pop("_version", None)

    # Shared/imported preset files are untrusted input: drop fields that
    # delete originals or overwrite outputs (#11). A "color preset" must not
    # arrive carrying remove_original=True.
    try:
        return ProcessOptions(**_filter_preset_fields(data))
    except TypeError:
        return _builtin_options(name)


def delete_preset(name: str) -> bool:
    """Delete a named preset. Returns True if deleted, False if not found."""
    path = _preset_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def import_presets_from_json(json_path: str) -> int:
    """Import presets from a JSON file (one object per preset name).

    Destructive fields (remove_original / overwrite) are stripped on import —
    an imported "look" must never silently delete originals when applied.
    """
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    count = 0
    for name, fields in data.items():
        if isinstance(fields, dict):
            save_preset(name, ProcessOptions(**_filter_preset_fields(fields)))
            count += 1
    return count
