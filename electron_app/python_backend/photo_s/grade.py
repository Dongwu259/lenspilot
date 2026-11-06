"""Color grading primitives (Lightroom-direction batch adjustments).

P0 set from the LensPilot backend survey: manual levels, point curves,
vibrance, and 3-way color grading. Pure numpy + PIL — no optional deps.

Conventions (match ``adjust.py`` / ``lut.py``):
* every function returns a new Image and never mutates the input;
* ``img.info`` (EXIF/ICC/DPI) is copied onto the result — the fromarray /
  merge / point paths build fresh images that would otherwise drop metadata
  (same regression class as the LUT info bug);
* alpha is preserved on RGBA inputs;
* the engine-facing options are compact strings (``levels="10,240,1.1"``),
  parsed here by the ``_parse_*`` helpers so REST/MCP/CLI/preset inherit the
  fields with zero hand-wired glue.
"""

import numpy as np
from PIL import Image, ImageFilter

__all__ = [
    "_parse_levels", "apply_levels",
    "_parse_curves", "apply_curves",
    "apply_vibrance",
    "_parse_color_grading", "apply_color_grading",
    "_parse_hsl", "apply_hsl",
    "_parse_point_color", "apply_point_color",
    "apply_clarity", "apply_texture",
    "apply_dehaze",
    "_parse_vignette", "apply_vignette",
    "_parse_grain", "apply_grain",
]


# ── Manual levels ────────────────────────────────────────────────────────────

def _parse_levels(s: str):
    """Parse ``"black,white[,gamma]"`` (e.g. ``"10,240,1.1"``).

    Missing fields fall back to identity (0 / 255 / 1.0). Returns
    ``(black, white, gamma)`` floats in 0..255 / 0..255 / >0.
    """
    parts = [p.strip() for p in s.split(",")]
    black = float(parts[0]) if len(parts) > 0 and parts[0] else 0.0
    white = float(parts[1]) if len(parts) > 1 and parts[1] else 255.0
    gamma = float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
    black = max(0.0, min(254.0, black))
    white = max(black + 1.0, min(255.0, white))
    gamma = max(0.1, min(8.0, gamma)) if gamma else 1.0
    return black, white, gamma


def apply_levels(img: Image.Image, black: float = 0.0, white: float = 255.0,
                 gamma: float = 1.0) -> Image.Image:
    """Manual levels: black/white-point remap + midtone gamma (three-point).

    ``out = ((in - black) / (white - black)) ** (1/gamma) * 255``, clipped.
    Built as a 256-entry LUT so it is one pass per band and keeps alpha.
    """
    if (black <= 0.0 and white >= 255.0 and gamma == 1.0):
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    black, white, gamma = _parse_levels(f"{black},{white},{gamma}")
    span = white - black
    inv = 1.0 / gamma
    table = []
    for v in range(256):
        t = 0.0 if v <= black else (1.0 if v >= white
                                    else (v - black) / span)
        table.append(int(round(255 * (t ** inv))))
    if img.mode == "RGBA":
        out = img.point(table * 3 + list(range(256)))  # alpha untouched
    elif img.mode == "L":
        out = img.point(table)
    else:
        out = img.point(table * 3)
    out.info = img.info.copy()
    return out


# ── Point curves (PCHIP monotone cubic) ─────────────────────────────────────

def _monotone_cubic(xs, ys, xq):
    """Monotone cubic Hermite (PCHIP / Fritsch–Carlson) interpolation.

    Returns y at query points ``xq``. ``xs`` must be strictly increasing,
    len >= 2; the interpolant is monotone between the control points, so a
    user curve can never overshoot (unlike a plain cubic or B-spline).
    """
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    xq = np.asarray(xq, dtype=np.float64)
    n = len(xs)
    dx = np.diff(xs)
    dy = np.diff(ys)
    m = np.zeros(n)
    if n == 2:  # linear
        slope = dy[0] / dx[0]
        m[:] = slope
    else:
        m[1:-1] = 0.5 * (dy[:-1] / dx[:-1] + dy[1:] / dx[1:])
        for i in range(1, n - 1):  # enforce monotonicity
            if dy[i - 1] * dy[i] <= 0:
                m[i] = 0.0
            else:
                a, b = dy[i - 1] / dx[i - 1], dy[i] / dx[i]
                w1, w2 = 2.0 * dx[i] + dx[i - 1], dx[i] + 2.0 * dx[i - 1]
                denom = w1 / a + w2 / b
                m[i] = (w1 + w2) / denom if denom else 0.0
        m[0] = dy[0] / dx[0]
        m[-1] = dy[-1] / dx[-1]
    xq_c = np.clip(xq, xs[0], xs[-1])
    out = np.empty_like(xq_c)
    idxs = np.clip(np.searchsorted(xs, xq_c) - 1, 0, n - 2)
    for k in range(len(xq_c)):
        i = int(idxs[k])
        h = dx[i]
        t = (xq_c[k] - xs[i]) / h
        h00 = (1.0 + 2.0 * t) * (1.0 - t) ** 2
        h10 = t * (1.0 - t) ** 2
        h01 = t * t * (3.0 - 2.0 * t)
        h11 = t * t * (t - 1.0)
        out[k] = (h00 * ys[i] + h10 * h * m[i]
                  + h01 * ys[i + 1] + h11 * h * m[i + 1])
    return np.clip(out, 0.0, 255.0)


def _build_curve_lut(points) -> list:
    """256-entry LUT from control points ``[(x, y), ...]`` (x, y in 0..255).

    Duplicate x values are collapsed to the last occurrence (later points
    win), so ``"128,100;128,140"`` degrades to a constant curve instead of
    hitting division-by-zero in the interpolator.
    """
    pts = sorted((float(x), float(y)) for x, y in points)
    deduped = []
    for x, y in pts:
        if deduped and x == deduped[-1][0]:
            deduped[-1] = (x, y)
        else:
            deduped.append((x, y))
    pts = deduped
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if xs[0] > 0.0:  # pad to the full 0..255 domain
        xs = [0.0] + xs
        ys = [ys[0]] + ys
    if xs[-1] < 255.0:
        xs = xs + [255.0]
        ys = ys + [ys[-1]]
    xq = np.linspace(0.0, 255.0, 256)
    return [int(round(v)) for v in _monotone_cubic(xs, ys, xq)]


def _parse_curves(s: str) -> dict:
    """Parse a curves spec into ``{channel: [(x, y), ...]}``.

    Format: pipe-separated channel segments, each ``channel:x,y;x,y;...``
    with channel ∈ ``rgb|r|g|b``. The first segment may omit the channel
    prefix (defaults to ``rgb``). Example:
    ``"0,0;128,140;255,255"`` or ``"r:0,0;128,140;255,255|b:0,0;128,120;255,255"``.
    """
    out: dict = {}
    for seg in s.split("|"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" in seg:
            ch, pts = seg.split(":", 1)
            ch = ch.strip().lower()
        else:
            ch, pts = "rgb", seg
        if ch not in ("rgb", "r", "g", "b"):
            raise ValueError(
                f"unknown curve channel {ch!r} (expected rgb/r/g/b)")
        points = []
        for tok in pts.split(";"):
            tok = tok.strip()
            if not tok:
                continue
            x, y = tok.split(",")
            points.append((float(x), float(y)))
        if len(points) >= 2:
            out[ch] = points
    return out


def apply_curves(img: Image.Image, channel_points: dict) -> Image.Image:
    """Apply per-channel point curves (PCHIP monotone spline → 256 LUT).

    ``channel_points`` maps ``"rgb"|"r"|"g"|"b"`` → control points. An
    ``"rgb"`` entry sets the base curve for all bands; per-channel entries
    override that band. Unlisted bands stay identity.
    """
    if not channel_points:
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    luts = {"r": list(range(256)), "g": list(range(256)), "b": list(range(256))}
    if "rgb" in channel_points:
        base = _build_curve_lut(channel_points["rgb"])
        luts = {"r": base, "g": base, "b": base}
    for ch in ("r", "g", "b"):
        if ch in channel_points:
            luts[ch] = _build_curve_lut(channel_points[ch])
    if img.mode == "L":
        out = img.point(luts["r"])  # single band → the rgb/base curve
    else:
        alpha = img.getchannel("A") if img.mode == "RGBA" else None
        r, g, b = img.convert("RGB").split()
        out = Image.merge("RGB", (r.point(luts["r"]), g.point(luts["g"]),
                                  b.point(luts["b"])))
        if alpha is not None:
            out.putalpha(alpha)
    out.info = img.info.copy()
    return out


# ── Vibrance (natural saturation) ───────────────────────────────────────────

def _to_hsv(rgb: np.ndarray):
    """RGB float [0,1] array → (hue[0,1), sat[0,1], val[0,1]) arrays."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    delta = mx - mn
    sat = np.where(mx > 0, delta / np.maximum(mx, 1e-9), 0.0)
    d = np.maximum(delta, 1e-9)
    hue = np.zeros_like(mx)
    # 三个 sel 必须互斥：r=g=b 的中性像素会同时命中所有条件，
    # hue 被连续覆写、最终停在第三段的 4/6 = 240°（正蓝）——
    # 此后蓝带/HSL 对所有灰像素满权重生效（2026-10-14 修复，见上游 bug 清单）。
    sel_r = mx == r
    sel_g = (mx == g) & ~sel_r
    sel_b = (mx == b) & ~sel_r & ~sel_g
    hue[sel_r] = np.mod((g - b) / d, 6.0)[sel_r]
    hue[sel_g] = ((b - r) / d + 2.0)[sel_g]
    hue[sel_b] = ((r - g) / d + 4.0)[sel_b]
    hue = np.mod(hue, 6.0) / 6.0
    return hue, sat, mx  # mx == value


def _from_hsv(hue, sat, val) -> np.ndarray:
    """HSV arrays → RGB float [0,1] array."""
    h6 = hue * 6.0
    sector = np.floor(h6).astype(np.int32)
    f = h6 - sector
    c = val * sat
    x = c * (1.0 - np.abs(np.mod(sector.astype(np.float64), 2.0) + f - 1.0))
    m = val - c
    z = np.zeros_like(c)
    R = np.choose(sector, [c, x, z, z, x, c]) + m
    G = np.choose(sector, [x, c, c, x, z, z]) + m
    B = np.choose(sector, [z, z, x, c, c, x]) + m
    return np.stack([R, G, B], axis=-1)


def _normalize_grade_input(img: Image.Image):
    """→ (rgb_float_array, alpha_or_None). Never mutates the input."""
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return arr, alpha


def _finish_grade(img: Image.Image, arr, alpha):
    """Float [0,1] RGB array → new RGB(A) image with info + alpha preserved."""
    out = Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8), "RGB")
    if alpha is not None:
        out.putalpha(alpha)
    out.info = img.info.copy()
    return out


def apply_vibrance(img: Image.Image, amount: float = 0.0) -> Image.Image:
    """Natural saturation: boost/soften inversely weighted by current sat.

    Muted colors get the most change; deep/skin colors are left alone —
    unlike a global saturation multiply. ``amount > 0`` boosts (→1),
    ``amount < 0`` softens (× (1+amount)), 0 = no change. Clamped to [-1, 1].
    """
    amount = max(-1.0, min(1.0, float(amount or 0.0)))
    if abs(amount) < 1e-4:
        return img
    arr, alpha = _normalize_grade_input(img)
    hue, sat, val = _to_hsv(arr)
    new_sat = sat + (1.0 - sat) * amount if amount > 0 \
        else sat * (1.0 + amount)
    new_sat = np.clip(new_sat, 0.0, 1.0)
    # Truly neutral pixels (sat ≈ 0) have an undefined hue — boosting their
    # saturation would round-trip to a red/cyan tint via the HSV→RGB
    # conversion and darken them. Real vibrance leaves neutrals alone.
    new_sat[sat < 1e-3] = 0.0
    out_arr = _from_hsv(hue, new_sat, val)
    return _finish_grade(img, out_arr, alpha)


# ── 3-way color grading ─────────────────────────────────────────────────────

def _parse_color_grading(s: str) -> dict:
    """Parse ``"zone:hue,sat[,lum];zone:..."`` into ``{zone: (hue, sat, lum)}``.

    zone ∈ shadows|midtones|highlights; hue ∈ [-180, 180] target hue for the
    zone; sat ∈ [-1, 1] tint strength (sign drives the hue-pull direction,
    magnitude drives both the pull amount and the saturation shift);
    lum ∈ [-1, 1] optional additive luminance shift for the zone (0 = none).
    """
    zones: dict = {}
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            raise ValueError(
                f"color_grading segment {seg!r} must be 'zone:hue,sat[,lum]'")
        zone, rest = seg.split(":", 1)
        zone = zone.strip().lower()
        if zone not in ("shadows", "midtones", "highlights"):
            raise ValueError(
                f"unknown color grading zone {zone!r} "
                f"(shadows/midtones/highlights)")
        parts = [p.strip() for p in rest.split(",")]
        hue = float(parts[0]) if parts[0] else 0.0
        sat = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        lum = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        zones[zone] = (hue, sat, lum)
    return zones


def _zone_mask(zone: str, val: np.ndarray) -> np.ndarray:
    """Smooth luminance mask for a zone (smoothstep edges, no banding)."""
    if zone == "shadows":
        return 1.0 - _smoothstep(0.25, 0.5, val)
    if zone == "highlights":
        return _smoothstep(0.5, 0.75, val)
    # midtones: bell centred on 0.5
    return np.exp(-((val - 0.5) ** 2) / (2.0 * 0.18 ** 2))


def _smoothstep(e0: float, e1: float, x) -> np.ndarray:
    if e1 <= e0:
        # degenerate range (e.g. vignette midpoint=1.0 clamps the falloff to
        # a zero-width band) — 0/0 used to produce NaN that ate the whole
        # mask; degrade to a hard step instead.
        return (np.asarray(x, dtype=np.float64) >= e1).astype(np.float64)
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def apply_color_grading(img: Image.Image, shadows=None, midtones=None,
                        highlights=None) -> Image.Image:
    """3-way color grading: hue-pull + saturation + luminance per zone.

    Each zone is ``(hue_deg, sat[, lum])``: the zone's hues are pulled
    toward ``hue_deg`` by ``|sat|`` of the way, its saturation is shifted
    by ``sat`` (positive boosts, negative desaturates), and its luminance
    by ``lum`` (additive, 0 = none) — all masked by a smooth luminance mask
    so zone boundaries don't band. ``None`` = zone untouched. Two-element
    tuples (no luminance) stay accepted for backward compatibility.
    """
    zones = {"shadows": shadows, "midtones": midtones, "highlights": highlights}
    if not any(z is not None for z in zones.values()):
        return img
    arr, alpha = _normalize_grade_input(img)
    hue, sat, val = _to_hsv(arr)
    hue_shift = np.zeros_like(hue)
    sat_shift = np.zeros_like(sat)
    lum_shift = np.zeros_like(val)
    for zone, zval in zones.items():
        if zval is None:
            continue
        hue_deg, sat_strength = zval[0], zval[1]
        lum_strength = zval[2] if len(zval) > 2 else 0.0
        strength = max(-1.0, min(1.0, float(sat_strength)))
        lum_strength = max(-1.0, min(1.0, float(lum_strength)))
        if abs(strength) < 1e-4 and abs(lum_strength) < 1e-4:
            continue
        target = ((float(hue_deg) % 360.0) / 360.0) % 1.0
        mask = _zone_mask(zone, val)
        if abs(strength) >= 1e-4:
            # signed shortest angular distance to the target hue
            delta = ((target - hue + 0.5) % 1.0) - 0.5
            hue_shift += delta * abs(strength) * mask
            sat_shift += strength * mask
        if abs(lum_strength) >= 1e-4:
            lum_shift += lum_strength * mask
    new_hue = np.mod(hue + hue_shift, 1.0)
    new_sat = np.clip(sat + sat_shift, 0.0, 1.0)
    new_val = np.clip(val + lum_shift, 0.0, 1.0)
    out_arr = _from_hsv(new_hue, new_sat, new_val)
    return _finish_grade(img, out_arr, alpha)


# ── HSL per-color domains ────────────────────────────────────────────────────

_HSL_CENTERS = {
    "red": 0.0, "orange": 30.0, "yellow": 60.0, "green": 120.0,
    "aqua": 180.0, "blue": 240.0, "purple": 280.0, "magenta": 320.0,
}
# Gaussian sigma (degrees) for soft domain transitions — a hue exactly on a
# domain boundary is barely affected, so bands don't cut hard edges.
_HSL_SIGMA = 14.0


def _hue_distance_deg(a, b):
    """Signed shortest angular distance a→b (array-safe)."""
    d = (a - b) % 360.0
    return np.where(d > 180.0, d - 360.0, d)


def _parse_hsl(s: str) -> dict:
    """Parse ``"color:hue,sat,lum;color:..."`` → ``{color: (h, s, l)}``.

    color ∈ red|orange|yellow|green|aqua|blue|purple|magenta; hue_shift in
    degrees (-180..180), sat/lum additive (-1..1).
    """
    out: dict = {}
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            raise ValueError(
                f"hsl segment {seg!r} must be 'color:hue,sat,lum'")
        color, rest = seg.split(":", 1)
        color = color.strip().lower()
        if color not in _HSL_CENTERS:
            raise ValueError(
                f"unknown hsl color {color!r} "
                f"(expected {','.join(_HSL_CENTERS)})")
        parts = [p.strip() for p in rest.split(",")]
        hue = float(parts[0]) if parts[0] else 0.0
        sat = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        lum = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        out[color] = (hue, sat, lum)
    return out


def apply_hsl(img: Image.Image, adjustments: dict) -> Image.Image:
    """HSL per-color adjustments across 8 hue domains with soft transitions.

    Each adjustment is ``(hue_shift_deg, sat_shift, lum_shift)`` applied
    under a gaussian hue mask centred on that domain, so neighbouring bands
    blend instead of banding. Shifts are additive in HSV space.
    """
    if not adjustments:
        return img
    arr, alpha = _normalize_grade_input(img)
    hue, sat, val = _to_hsv(arr)
    hue_deg = hue * 360.0
    hue_shift = np.zeros_like(hue)
    sat_shift = np.zeros_like(sat)
    lum_shift = np.zeros_like(val)
    for color, (dh, ds, dl) in adjustments.items():
        center = _HSL_CENTERS[color]
        dist = np.abs(_hue_distance_deg(hue_deg, center))
        mask = np.exp(-(dist ** 2) / (2.0 * _HSL_SIGMA ** 2))
        hue_shift += (dh / 360.0) * mask
        sat_shift += ds * mask
        lum_shift += dl * mask
    new_hue = np.mod(hue + hue_shift, 1.0)
    new_sat = np.clip(sat + sat_shift, 0.0, 1.0)
    new_val = np.clip(val + lum_shift, 0.0, 1.0)
    return _finish_grade(img, _from_hsv(new_hue, new_sat, new_val), alpha)


# ── Point color (sampled-color targeted adjustment) ─────────────────────────

def _parse_point_color(s: str) -> list:
    """Parse ``"r,g,b:hue,sat,lum[,range];..."``.

    Each target is a sampled color (r,g,b 0-255) plus hue/sat/lum shifts
    (hue in degrees -180..180, sat/lum additive -1..1) and an optional
    range (tolerance 0-1, default 0.1). Unlike :func:`apply_hsl` the mask
    is centred on the *sampled* color, not a fixed hue domain.
    """
    targets = []
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            raise ValueError(
                f"point_color segment {seg!r} must be 'r,g,b:hue,sat,lum[,range]'")
        color, rest = seg.split(":", 1)
        rgb = [p.strip() for p in color.split(",")]
        if len(rgb) != 3:
            raise ValueError(
                f"point_color target {color!r} must be 'r,g,b' (0-255)")
        try:
            r = max(0, min(255, int(round(float(rgb[0])))))
            g = max(0, min(255, int(round(float(rgb[1])))))
            b = max(0, min(255, int(round(float(rgb[2])))))
        except ValueError:
            raise ValueError(
                f"point_color target {color!r} must be numeric") from None
        parts = [p.strip() for p in rest.split(",")]
        if len(parts) > 4:
            raise ValueError(
                f"point_color shifts {rest!r} must be 'hue,sat,lum[,range]'")
        try:
            hue = float(parts[0]) if parts[0] else 0.0
            sat = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
            lum = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
            rng = float(parts[3]) if len(parts) > 3 and parts[3] else 0.1
        except ValueError:
            raise ValueError(
                f"point_color shifts {rest!r} must be numeric") from None
        rng = max(0.02, min(1.0, rng))
        targets.append((r, g, b, hue, sat, lum, rng))
    return targets


def _point_color_mask(hue_deg, sat, val, r, g, b, rng):
    """Gaussian soft mask around a sampled color (HSV distance)."""
    import colorsys
    th, ts, tv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    d = (hue_deg - th * 360.0) % 360.0
    dh = np.where(d > 180.0, d - 360.0, d) / 180.0
    dist = np.sqrt(dh * dh + (sat - ts) ** 2 + (val - tv) ** 2)
    mask = np.exp(-(dist ** 2) / (2.0 * rng * rng))
    if ts > 0.1:  # gray pixels carry no meaningful hue
        mask = mask * np.clip(sat / 0.1, 0.0, 1.0)
    return mask


def apply_point_color(img: Image.Image, targets: list) -> Image.Image:
    """Hue/sat/lum shifts centred on sampled colors with soft range falloff.

    Same additive HSV math as :func:`apply_hsl`, but each target's mask is
    centred on its sampled color instead of a fixed hue-domain centre, so
    e.g. only the sampled teal of a jacket shifts without dragging the
    whole blue-green domain along.
    """
    if not targets:
        return img
    arr, alpha = _normalize_grade_input(img)
    hue, sat, val = _to_hsv(arr)
    hue_deg = hue * 360.0
    hue_shift = np.zeros_like(hue)
    sat_shift = np.zeros_like(sat)
    lum_shift = np.zeros_like(val)
    for r, g, b, dh, ds, dl, rng in targets:
        if dh == 0.0 and ds == 0.0 and dl == 0.0:
            continue
        mask = _point_color_mask(hue_deg, sat, val, r, g, b, rng)
        hue_shift += (dh / 360.0) * mask
        sat_shift += ds * mask
        lum_shift += dl * mask
    if not hue_shift.any() and not sat_shift.any() and not lum_shift.any():
        return img
    new_hue = np.mod(hue + hue_shift, 1.0)
    new_sat = np.clip(sat + sat_shift, 0.0, 1.0)
    new_val = np.clip(val + lum_shift, 0.0, 1.0)
    return _finish_grade(img, _from_hsv(new_hue, new_sat, new_val), alpha)


# ── Clarity / texture (local contrast) ──────────────────────────────────────

def _usm_local_contrast(img: Image.Image, amount: float,
                        radius: float) -> Image.Image:
    """Luminance-driven unsharp-mask local contrast.

    ``delta = L - blur(L)`` is added to every channel: hue/sat ratios are
    preserved and only local luminance detail changes (clarity = large
    radius, texture = small radius).
    """
    # amount is pre-clamped by the callers (clarity/texture → [-1,1],
    # export sharpen → its own range); clamping here would silently cap
    # export-sharpen strengths above 1.0.
    if abs(float(amount)) < 1e-4:
        return img
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    lum = np.asarray(img.convert("L"), dtype=np.float32)
    blurred = np.asarray(
        Image.fromarray(lum.astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(radius=radius)),
        dtype=np.float32)
    delta = lum - blurred
    if img.mode == "L":
        out = Image.fromarray(
            np.clip(lum + amount * delta, 0, 255).astype(np.uint8), "L")
    else:
        rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
        out_arr = rgb + amount * delta[..., np.newaxis]
        out = Image.fromarray(
            np.clip(out_arr, 0, 255).astype(np.uint8), "RGB")
        if alpha is not None:
            out.putalpha(alpha)
    out.info = img.info.copy()
    return out


def apply_clarity(img: Image.Image, amount: float = 0.0,
                  radius: float = 60.0) -> Image.Image:
    """Local contrast with a large radius (Lightroom-style clarity)."""
    return _usm_local_contrast(img, max(-1.0, min(1.0, float(amount or 0.0))),
                               radius)


def apply_texture(img: Image.Image, amount: float = 0.0,
                  radius: float = 4.0) -> Image.Image:
    """Fine-detail enhancement with a small radius (texture)."""
    return _usm_local_contrast(img, max(-1.0, min(1.0, float(amount or 0.0))),
                               radius)


def apply_export_sharpen(img: Image.Image, amount: float = 1.0) -> Image.Image:
    """Output-stage USM with the radius scaled to the image's resolution.

    Lightroom-style export sharpening: runs on the FINAL (post-resize) pixels
    with a radius proportional to output size — a 4000px-wide export wants a
    larger radius than an 800px thumbnail. ``amount`` 0 = off; 0.5 gentle,
    1.0 standard, 2.0 strong. The lr-look preset uses this instead of the
    mid-pipeline ``sharpen``.
    """
    amount = float(amount or 0.0)
    if abs(amount) < 1e-4:
        return img
    max_dim = max(img.size)
    radius = min(3.0, max(0.3, 0.5 + max_dim / 4000.0))
    return _usm_local_contrast(img, amount, radius)


def apply_highlight_recovery(img: Image.Image,
                             amount: float = 1.0) -> Image.Image:
    """LR-style highlight recovery: softly compress the top of the tonal range.

    Hard-clipped highlights (flat 255) regain a visible gradient: values above
    ``thr`` roll off toward a ceiling below 255 instead of piling up at the
    white point. Monotone — midtones and shadows are untouched. ``amount``
    0-1 scales both the compression power and the ceiling drop; 0 = off.
    """
    amount = float(amount or 0.0)
    if abs(amount) < 1e-4:
        return img
    amount = max(0.0, min(1.0, amount))
    thr = 200.0                      # compression begins here
    p = 1.0 + amount                 # power: stronger pull with more amount
    max_out = 255.0 - 18.0 * amount  # ceiling sinks with amount
    lut = list(range(256))
    for x in range(int(thr) + 1, 256):
        t = (x - thr) / (255.0 - thr)
        lut[x] = int(round(thr + (max_out - thr) * (t ** p)))
    if img.mode == "RGBA":
        return img.point(lut * 3 + list(range(256)))  # alpha untouched
    if img.mode == "L":
        return img.point(lut)
    return img.point(lut * 3)


# ── Dehaze ──────────────────────────────────────────────────────────────────

def apply_dehaze(img: Image.Image, amount: float = 0.0) -> Image.Image:
    """Dehaze via dark-channel prior with a blurred transmission estimate.

    Batch/delivery grade: the transmission map is refined with a gaussian
    blur rather than a full guided filter (no opencv dependency) — adequate
    for uniform haze, not dense local haze. ``amount > 0`` removes haze,
    ``< 0`` adds haze back toward the atmospheric light; 0 = unchanged.
    """
    amount = max(-1.0, min(1.0, float(amount)))
    if abs(amount) < 1e-4:
        return img
    arr, alpha = _normalize_grade_input(img)
    dark = np.min(arr, axis=-1)
    flat = dark.reshape(-1)
    k = max(1, int(flat.size * 0.001))
    idx = np.argpartition(flat, -k)[-k:]
    A = arr.reshape(-1, 3)[idx].mean(axis=0) + 1e-6
    w = 0.95
    t = 1.0 - w * dark / max(float(A.max()), 1e-6)
    t_img = Image.fromarray((np.clip(t, 0, 1) * 255).astype(np.uint8), "L")
    t_ref = np.asarray(
        t_img.filter(ImageFilter.GaussianBlur(radius=8)),
        dtype=np.float32) / 255.0
    t_ref = np.clip(t_ref, 0.1, 1.0)
    if amount > 0:
        J = (arr - A) / t_ref[..., np.newaxis] + A
        out_arr = arr + (J - arr) * amount  # scale the effect by amount
    else:
        out_arr = arr * (1.0 + amount) + A * (-amount)  # add haze
    return _finish_grade(img, out_arr, alpha)


# ── Vignette ────────────────────────────────────────────────────────────────

def _parse_vignette(s: str):
    """Parse ``"amount[,midpoint[,feather]]"`` (defaults 0.5/0.5/0.5)."""
    parts = [p.strip() for p in s.split(",")]
    amount = float(parts[0]) if parts[0] else 0.5
    midpoint = float(parts[1]) if len(parts) > 1 and parts[1] else 0.5
    feather = float(parts[2]) if len(parts) > 2 and parts[2] else 0.5
    return (max(-1.0, min(1.0, amount)),
            max(0.0, min(1.0, midpoint)),
            max(0.05, min(1.0, feather)))


def apply_vignette(img: Image.Image, amount: float = 0.5,
                   midpoint: float = 0.5,
                   feather: float = 0.5) -> Image.Image:
    """Radial vignette: darken (``amount > 0``) or lift (``amount < 0``).

    ``midpoint`` = where the falloff begins (0 = centre, 1 = corner);
    ``feather`` = softness of the transition. The radial mask multiplies
    every channel, so hue is preserved.
    """
    if not amount:
        return img
    arr, alpha = _normalize_grade_input(img)
    h, w = arr.shape[:2]
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    nx = (x - (w - 1) / 2.0) / max(w / 2.0, 1.0)
    ny = (y - (h - 1) / 2.0) / max(h / 2.0, 1.0)
    r = np.sqrt(nx ** 2 + ny ** 2)
    e1 = min(1.0, midpoint + feather)
    mask = 1.0 - amount * _smoothstep(midpoint, e1, r)
    return _finish_grade(img, arr * mask[..., np.newaxis], alpha)


# ── Grain ───────────────────────────────────────────────────────────────────

def _parse_grain(s: str):
    """Parse ``"amount[,size[,seed]]"`` (defaults 0.1 / 1.0 / derived)."""
    parts = [p.strip() for p in s.split(",")]
    amount = float(parts[0]) if parts[0] else 0.1
    size = float(parts[1]) if len(parts) > 1 and parts[1] else 1.0
    seed = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return (max(0.0, min(1.0, amount)),
            max(0.1, min(4.0, size)),
            seed)


def apply_grain(img: Image.Image, amount: float = 0.1,
                size: float = 1.0, seed: int = None) -> Image.Image:
    """Film grain: luminance-weighted monochrome gaussian noise.

    The noise pattern (optionally blurred for coarser ``size``) is weighted
    by a midtone curve so highlights/shadows stay clean — film-like. The
    generator is seeded from the image dimensions unless ``seed`` is given,
    so re-running the same command reproduces the same grain.
    """
    if not amount:
        return img
    arr, alpha = _normalize_grade_input(img)
    h, w = arr.shape[:2]
    if seed is None:
        # deterministic per image size — same input + params ⇒ same output
        seed = (w * 73856093) ^ (h * 19349663)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal((h, w)).astype(np.float32)
    if size > 1.0:
        noise_img = Image.fromarray(
            (np.clip(noise * 127.5 + 127.5, 0, 255)).astype(np.uint8), "L")
        noise_img = noise_img.filter(ImageFilter.GaussianBlur(radius=size - 1.0))
        noise = (np.asarray(noise_img, dtype=np.float32) - 127.5) / 127.5
    lum = np.clip(np.max(arr, axis=-1), 0.0, 1.0)
    weight = np.clip(1.0 - np.abs(lum - 0.5) * 1.4, 0.0, 1.0)
    out_arr = arr + (noise * weight)[..., np.newaxis] * amount * 0.15
    return _finish_grade(img, out_arr, alpha)
