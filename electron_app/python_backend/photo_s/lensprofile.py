"""
PhotoS - Lens profile registry (user-maintained).

A lens profile bundles the three manual lens-correction parameters
(distortion k1 / vignette / CA) under a name — e.g. "RF 24-70mm f/2.8" — so
`--lens-profile NAME` applies them all at once instead of three separate
flags. The database is deliberately USER-maintained (~/.photos/lens_profiles
.json): PhotoS does not ship invented lens data (wrong distortion values are
worse than none). Follows the presets.py storage pattern.

Profile fields (all optional):
    distort:  float  — radial distortion k1 (positive = barrel fix)
    vignette: str    — "amount[,midpoint]"  corner lift
    ca:       str    — "r_scale,b_scale"    chromatic aberration fix
    desc:     str    — human-readable label
"""

import json
import os
from pathlib import Path
from typing import List, Optional

PROFILES_PATH = Path.home() / ".photos" / "lens_profiles.json"

_VALID_KEYS = {"distort", "vignette", "ca", "desc"}


def _load_raw() -> dict:
    if not PROFILES_PATH.exists():
        return {}
    try:
        data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(data: dict) -> None:
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_lens_profile(name: str, distort: Optional[float] = None,
                      vignette: Optional[str] = None,
                      ca: Optional[str] = None,
                      description: str = "") -> dict:
    """Save (or replace) a named lens profile. Returns the stored dict."""
    prof = {}
    if distort is not None:
        prof["distort"] = float(distort)
    if vignette:
        prof["vignette"] = vignette
    if ca:
        prof["ca"] = ca
    if description:
        prof["desc"] = description
    data = _load_raw()
    data[name] = prof
    _save_raw(data)
    return prof


def load_lens_profile(name: str) -> Optional[dict]:
    """Return the profile dict for a name, or None if unknown."""
    prof = _load_raw().get(name)
    if not isinstance(prof, dict):
        return None
    return {k: v for k, v in prof.items() if k in _VALID_KEYS}


def list_lens_profiles() -> List[str]:
    """Return sorted display strings "name — desc" (desc omitted if empty)."""
    out = []
    for name, prof in sorted(_load_raw().items()):
        if not isinstance(prof, dict):
            continue
        desc = prof.get("desc", "")
        out.append(f"{name} — {desc}" if desc else name)
    return out


def lens_profile_names() -> List[str]:
    """Return sorted bare profile names (for dropdown values)."""
    return sorted(n for n, p in _load_raw().items() if isinstance(p, dict))


def delete_lens_profile(name: str) -> bool:
    """Delete a profile. Returns True if deleted, False if not found."""
    data = _load_raw()
    if name not in data:
        return False
    del data[name]
    _save_raw(data)
    return True
