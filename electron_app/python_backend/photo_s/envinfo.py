"""
PhotoS - Environment probe, shared by three consumers.

`photo-s info`, the MCP `info` tool and the GUI Settings dialog all report
the same environment capability data. Keeping it in one module prevents the
shapes from drifting apart.
"""

import importlib.util as _ilu


def optional_features() -> dict:
    """Installed optional-dependency status, for environment probing."""
    return {
        "enhance": _ilu.find_spec("cv2") is not None,
        "raw": _ilu.find_spec("rawpy") is not None,
        "heic": _ilu.find_spec("pillow_heif") is not None,
        "avif": _ilu.find_spec("pillow_avif") is not None,
        "exif": _ilu.find_spec("piexif") is not None,
        "watch": _ilu.find_spec("watchdog") is not None,
        "gui_dnd": _ilu.find_spec("tkinterdnd2") is not None,
        "mcp": _ilu.find_spec("mcp") is not None,
    }


def plugins() -> list:
    """Installed plugins (name + provided operations)."""
    from .plugin import discover_plugins
    return [{
        "name": p.name,
        "provides": list(getattr(p, "provides", ())),
    } for p in discover_plugins()]
