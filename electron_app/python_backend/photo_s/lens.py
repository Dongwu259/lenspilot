"""Lens correction (manual parameters): distortion / vignette / CA.

No lens-profile database (lensfun-class dependency) - the three knobs are
manual, compact-string modeled and batch-safe:

* distortion  ``k1`` (float): Brown-Conrady-style radial correction.
  ``k1 > 0`` pulls edge content outward (corrects barrel bulge),
  ``k1 < 0`` squeezes it inward (corrects pincushion).
* vignette    ``"amount[,midpoint]"``: brighten the corners by a radial
  gain (removes lens darkening; the inverse of ``grade.apply_vignette``).
* CA          ``"r_scale,b_scale"``: per-channel radial scale for the R
  and B channels about the image centre (e.g. ``"0.999,1.001"``), which
  cancels red/blue fringing at high-contrast edges.

Pure numpy + PIL bilinear resampling, no optional deps. ``img.info``
(EXIF/ICC) is copied onto results; alpha is preserved.
"""

import numpy as np
from PIL import Image

__all__ = [
    "LensError", "apply_distortion", "apply_vignette_fix", "apply_ca_fix",
]


class LensError(ValueError):
    """Invalid or unparseable lens-correction spec."""


def _bilinear(arr: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Sample ``arr`` (h, w, c|1) at float coords (clamped to edges)."""
    h, w = arr.shape[:2]
    xs = np.clip(xs, 0.0, w - 1.0)
    ys = np.clip(ys, 0.0, h - 1.0)
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (xs - x0)[..., np.newaxis] if arr.ndim == 3 else (xs - x0)
    fy = (ys - y0)[..., np.newaxis] if arr.ndim == 3 else (ys - y0)

    def at(yi, xi):
        return arr[yi, xi]

    top = at(y0, x0) * (1.0 - fx) + at(y0, x1) * fx
    bot = at(y1, x0) * (1.0 - fx) + at(y1, x1) * fx
    return top * (1.0 - fy) + bot * fy


def _normalized_grid(w: int, h: int):
    """Pixel grids plus normalized coords in [-1, 1] about the centre."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    xn = (xs - cx) / cx if cx else np.zeros_like(xs)
    yn = (ys - cy) / cy if cy else np.zeros_like(ys)
    return xs, ys, xn, yn


def apply_distortion(img: Image.Image, k1: float = 0.0) -> Image.Image:
    """Radial distortion correction, one knob.

    ``k1 > 0``: each output pixel samples *further out* than its own
    position (edge content pulls outward, correcting a barrel bulge);
    ``k1 < 0`` samples inward (pincushion). Edges are clamped so no black
    corners appear at moderate values.
    """
    if abs(k1) < 1e-6:
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    xs, ys, xn, yn = _normalized_grid(w, h)
    r2 = xn * xn + yn * yn
    scale = 1.0 + k1 * r2
    # Sample position = centre-origin coordinate stretched by the scale.
    sx = (w - 1) / 2.0 + (xs - (w - 1) / 2.0) * scale
    sy = (h - 1) / 2.0 + (ys - (h - 1) / 2.0) * scale
    out = _bilinear(arr, sx, sy)
    result = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    result.info = img.info.copy()
    return result


def parse_vignette_fix(s: str):
    """Parse ``"amount[,midpoint]"`` -> ``(amount, midpoint)``."""
    parts = [p.strip() for p in s.split(",")]
    try:
        amount = float(parts[0]) if parts[0] else 0.0
        midpoint = float(parts[1]) if len(parts) > 1 and parts[1] else 0.5
    except ValueError:
        raise LensError(f"bad lens_vignette spec {s!r}") from None
    if len(parts) > 2:
        raise LensError(f"bad lens_vignette spec {s!r}")
    amount = max(-1.0, min(1.0, amount))
    midpoint = max(0.05, min(1.0, midpoint))
    return amount, midpoint


def apply_vignette_fix(img: Image.Image, amount: float = 0.0,
                       midpoint: float = 0.5) -> Image.Image:
    """Remove lens darkening: radial gain that lifts the corners.

    ``amount`` in 0..1 (0 = off): gain grows from 1 at ``midpoint`` radius
    to ``1 + amount`` at the corners. Negative amount darkens instead
    (adds vignetting).
    """
    if abs(amount) < 1e-6:
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    _, _, xn, yn = _normalized_grid(w, h)
    r = np.sqrt(xn * xn + yn * yn) / np.sqrt(2.0)   # 0 centre, 1 corner
    r = r[..., np.newaxis]
    # Smoothstep falloff from the midpoint out to the corner.
    t = np.clip((r - midpoint) / max(1e-6, 1.0 - midpoint), 0.0, 1.0)
    gain = 1.0 + amount * (t * t * (3.0 - 2.0 * t))
    out = np.clip(arr * gain, 0, 255)
    result = Image.fromarray(out.astype(np.uint8), "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    result.info = img.info.copy()
    return result


def parse_ca(s: str):
    """Parse ``"r_scale,b_scale"`` -> ``(r_scale, b_scale)`` (default 1,1)."""
    parts = [p.strip() for p in s.split(",")]
    if not parts or not parts[0]:
        return (1.0, 1.0)
    if len(parts) > 2:
        raise LensError(f"bad lens_ca spec {s!r}")
    try:
        r_scale = float(parts[0]) if parts[0] else 1.0
        b_scale = float(parts[1]) if len(parts) > 1 and parts[1] else 1.0
    except ValueError:
        raise LensError(f"bad lens_ca spec {s!r}") from None
    return (r_scale, b_scale)


def apply_ca_fix(img: Image.Image, r_scale: float = 1.0,
                 b_scale: float = 1.0) -> Image.Image:
    """Cancel chromatic aberration: rescale R and B about the centre.

    ``r_scale``/``b_scale`` around 1.0 (e.g. 0.999 / 1.001): each channel
    is bilinear-resampled at its own radial scale so red/blue fringes
    realign with green.
    """
    if abs(r_scale - 1.0) < 1e-6 and abs(b_scale - 1.0) < 1e-6:
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    xs, ys, _, _ = _normalized_grid(w, h)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    def sample_channel(ch: int, scale: float) -> np.ndarray:
        sx = cx + (xs - cx) * scale
        sy = cy + (ys - cy) * scale
        return _bilinear(arr[..., ch], sx, sy)

    out = arr.copy()
    if abs(r_scale - 1.0) > 1e-6:
        out[..., 0] = sample_channel(0, r_scale)
    if abs(b_scale - 1.0) > 1e-6:
        out[..., 2] = sample_channel(2, b_scale)
    result = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    result.info = img.info.copy()
    return result
