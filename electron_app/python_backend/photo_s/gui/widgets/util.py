"""Tk-adjacent helper functions (photo_s.gui.widgets.util, v2.0).

_open_image_safe     GUI display loader with RAW/HEIC engine fallback
_mask_spec_string    mask spec tuple → compact string serialization
_exif_datetime_str   meta date/time → EXIF DateTimeOriginal form
canvas_unbind_safe   drop stale global mousewheel bindings

Extracted verbatim from the former photo_s/gui.py.
"""

def _open_image_safe(path):
    """Open a PhotoS-supported image for GUI display (PIL cannot open
    RAW — falls back to the engine loader which handles rawpy/HEIC
    with fallbacks). Raises on anything unreadable; callers catch."""
    from PIL import Image
    try:
        return Image.open(path)
    except Exception:
        from ...engine import _get_image
        return _get_image(path)


def _mask_spec_string(name, kind, params, feather, invert) -> str:
    """Serialize one mask spec tuple -> compact string (shared by the
    mask workflow OK handler and the v1.7 dialog — they duplicated this
    and drifted: combo crashed both, _masks_ok missed object/color).
    """
    def _n(v):
        v = round(float(v), 4)
        return str(int(v)) if v == int(v) else str(v)

    if kind == "brush":
        # 减模式点存 (x, y, -r)：序列化成 -x,y,r（负号在 x 位，与
        # MaskSpec.to_string 一致；-r 在半径位 parser 不认）
        seg = f"{name}:brush:" + "|".join(
            (f"-{_n(x)},{_n(y)},{_n(-r)}" if r < 0 else
             f"{_n(x)},{_n(y)},{_n(r)}")
            for x, y, r in params)
    elif kind in ("subject", "person"):
        seg = f"{name}:{kind}"
    elif kind == "object":
        seg = f"{name}:object:{params[0] if params else 'car'}"
    elif kind == "color":
        p = [int(round(float(v))) for v in params[:3]]
        seg = f"{name}:color:{_n(p[0])},{_n(p[1])},{_n(p[2])}"
        if len(params) > 3:
            seg += f",tol={_n(float(params[3]))}"
    elif kind == "combo":
        a, op, b = params
        seg = f"{name}:combo:{a}{op}{b}"
    else:
        seg = f"{name}:{kind}:" + ",".join(_n(p) for p in params)
    if feather:
        seg += f",feather={_n(feather)}"
    if invert:
        seg += ",invert"
    return seg


def _exif_datetime_str(meta):
    """Normalize meta['date']/['time'] ('YYYY-MM-DD' / 'HH-MM-SS' as read
    by read_exif_metadata) into the EXIF DateTimeOriginal form
    'YYYY:MM:DD HH:MM:SS' shown in the review editor's date field."""
    d = (meta.get("date") or "").replace("-", ":")
    t = (meta.get("time") or "").replace("-", ":")
    return (d + " " + t).strip()


def canvas_unbind_safe(widget):
    """Drop any leftover global mousewheel binding from a destroyed panel.

    bind_all is interp-global: destroying the settings card does not remove
    its handler, and a stale one would target a destroyed canvas. Called at
    the top of every settings-panel build.
    """
    try:
        widget.unbind_all("<MouseWheel>")
    except Exception:
        pass
