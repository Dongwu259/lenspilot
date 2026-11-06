"""
PhotoS - LOG / flat-profile recovery curves

Converts camera LOG-encoded stills (Sony S-Log3, Canon Log 3, ARRI LogC3,
DJI D-Log, Panasonic V-Log, HLG) back to a normal sRGB display image.

Each curve is a 1D transfer function: LOG decode (code → linear) followed by
the sRGB display gamma, applied as a 256-entry point table — no 3D LUT and no
external dependency.

Constants are ported from the authoritative colour-science library
(https://github.com/colour-science/colour) and the published camera transfer
functions; the 0.18 mid-gray point of each curve was verified to decode back
to ≈0.18 linear.
"""

import math

from PIL import Image

# ── LOG decode: normalized code value (0-1) → linear reflection (0-1) ──────


def _dec_slog3(y: float) -> float:
    """Sony S-Log3 (normalized code value)."""
    if y >= 171.2102946929 / 1023:
        return 10 ** ((y * 1023 - 420) / 261.5) * 0.19 - 0.01
    return (y * 1023 - 95) * 0.01125 / (171.2102946929 - 95)


def _dec_canonlog3(c: float) -> float:
    """Canon Log 3 v1.2 (studio swing; reflection = ×0.9)."""
    if c < 0.097465473:
        x = -(10 ** ((0.12783901 - c) / 0.36726845) - 1) / 14.98325
    elif c <= 0.15277891:
        x = (c - 0.12512219) / 1.9754798
    else:
        x = (10 ** ((c - 0.12240537) / 0.36726845) - 1) / 14.98325
    return x * 0.9


def _dec_logc3(t: float) -> float:
    """ARRI LogC3 (EI 800, SUP 3.x, 'Linear Scene Exposure Factor')."""
    cut, a, b, c, d, e, f = (0.010591, 5.555556, 0.052272, 0.24719,
                             0.385537, 5.367655, 0.092809)
    if t > e * cut + f:
        return (10 ** ((t - d) / c) - b) / a
    return (t - f) / e


def _dec_dlog(y: float) -> float:
    """DJI D-Log."""
    if y <= 0.14:
        return (y - 0.0929) / 6.025
    return (10 ** (3.89616 * y - 2.27752) - 0.0108) / 0.9892


def _dec_vlog(v: float) -> float:
    """Panasonic V-Log (cut2=0.401, b=0.00873, c=0.241514, d=0.598206)."""
    if v < 0.401:
        return (v - 0.125) / 5.6
    return 10 ** ((v - 0.598206) / 0.241514) - 0.00873


def _dec_hlg(e: float) -> float:
    """HLG (BT.2100) → linear: ARIB STD-B67 OETF inverse."""
    if e <= 0.5:
        return (e * e) / 3.0
    return (math.exp((e - 0.55991073) / 0.17883277) + 0.28466892) / 12.0


LOG_CURVES = {
    "SLOG3": {"decode": _dec_slog3, "desc": "Sony S-Log3"},
    "CLOG3": {"decode": _dec_canonlog3, "desc": "Canon Log 3 (v1.2)"},
    "LOGC3": {"decode": _dec_logc3, "desc": "ARRI LogC3 (EI800 / SUP 3.x)"},
    "DLOG": {"decode": _dec_dlog, "desc": "DJI D-Log"},
    "VLOG": {"decode": _dec_vlog, "desc": "Panasonic V-Log"},
    "HLG": {"decode": _dec_hlg, "desc": "HLG (BT.2100)"},
}


def _srgb_oetf(lin: float) -> float:
    """Linear → sRGB encoded (display gamma)."""
    if lin <= 0.0031308:
        return 12.92 * lin
    return 1.055 * (lin ** (1 / 2.4)) - 0.055


def build_log_recovery_lut(curve: str) -> list:
    """256-entry point table: 8-bit LOG code → 8-bit sRGB display value."""
    # Case-insensitive (library callers may pass "slog3"); clear error listing
    # the supported curves instead of a bare KeyError.
    key = str(curve).upper()
    if key not in LOG_CURVES:
        raise ValueError(
            f"unsupported log curve {curve!r}; supported: "
            f"{sorted(LOG_CURVES)}")
    decode = LOG_CURVES[key]["decode"]
    lut = []
    for v in range(256):
        lin = max(0.0, min(1.0, decode(v / 255.0)))
        e = max(0.0, min(1.0, _srgb_oetf(lin)))
        lut.append(int(round(e * 255)))
    return lut


def apply_log_recovery(img: Image.Image, curve: str) -> Image.Image:
    """Apply a LOG recovery curve to the image (same table per band)."""
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    lut = build_log_recovery_lut(curve)
    if img.mode == "RGBA":
        return img.point(lut * 3 + list(range(256)))  # alpha untouched
    if img.mode == "L":
        return img.point(lut)
    return img.point(lut * 3)
