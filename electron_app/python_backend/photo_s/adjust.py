"""
PhotoS - Image Adjustments (pure Pillow, no engine dependency)

Tone & color, composition (crop / rotate / flip / pad), and color management.
Every function takes and returns a PIL Image, so they slot into the
process_image pipeline without coupling to ProcessOptions.
"""

import re
from typing import Optional, Tuple

from PIL import Image, ImageCms, ImageEnhance


def _flattened(img):
    """Pixel data with Pillow version compat (get_flattened_data in Pillow 12+,
    getdata fallback for older Pillows / py3.9 → Pillow 11)."""
    gfd = getattr(img, "get_flattened_data", None)
    if gfd is not None:
        return gfd()
    return img.getdata()


# Sepia conversion matrix (classic R=0.393R+0.769G+0.189B coefficients).
_SEPIA_MATRIX = (0.393, 0.769, 0.189, 0,
                 0.349, 0.686, 0.168, 0,
                 0.272, 0.534, 0.131, 0)


# ── Helpers ──────────────────────────────────────────────────────────────────

def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    """Parse "#RRGGBB" or "RRGGBB" into an (r, g, b) tuple."""
    s = str(value).strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"invalid color {value!r} (expected #RRGGBB)")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _parse_ratio(ratio: Optional[str]) -> Optional[Tuple[float, float]]:
    """Parse "16:9" → (16.0, 9.0); None/empty/invalid → None."""
    if not ratio:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", str(ratio))
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return (w, h)


# ── Color management ─────────────────────────────────────────────────────────

def apply_color_management(img: Image.Image, srgb: bool = False,
                           flatten_cmyk: bool = False) -> Image.Image:
    """Convert CMYK→RGB and/or re-tag the image with an sRGB ICC profile.

    ImageCms.profileToProfile raises on CMYK images without a usable embedded
    profile, so the CMYK path falls back to a plain convert("RGB").
    """
    if flatten_cmyk and img.mode == "CMYK":
        # Trust an embedded CMYK profile when present; without one there is
        # no honest source profile (sRGB is not CMYK), so use PIL's plain
        # conversion instead of pretending sRGB described the source.
        icc = img.info.get("icc_profile")
        converted = False
        if icc:
            try:
                from io import BytesIO
                src_profile = ImageCms.ImageCmsProfile(BytesIO(icc))
                img = ImageCms.profileToProfile(
                    img, src_profile, ImageCms.createProfile("sRGB"),
                    outputMode="RGB")
                converted = True
            except Exception:
                converted = False
        if not converted:
            img = img.convert("RGB")

    if srgb:
        img = img.copy()
        img.info.pop("icc_profile", None)
        img.info["icc_profile"] = ImageCms.ImageCmsProfile(
            ImageCms.createProfile("sRGB")).tobytes()

    return img


# ── Tone & color ─────────────────────────────────────────────────────────────

def apply_tone_adjustments(
    img: Image.Image,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    gamma: float = 1.0,
    sharpen: float = 1.0,
    grayscale: bool = False,
    sepia: bool = False,
) -> Image.Image:
    """Apply brightness/contrast/saturation/gamma/sharpen multipliers
    (1.0 = no change) plus optional grayscale/sepia conversion.

    Alpha is preserved on RGBA inputs; gamma uses a 256-entry LUT with an
    identity slice for the alpha band. P/PA/LA/I;16 modes are normalized to
    RGB(A) first (the LUT is only valid for 8-bit modes).
    """
    if (brightness == 1.0 and contrast == 1.0 and saturation == 1.0
            and gamma == 1.0 and sharpen == 1.0
            and not grayscale and not sepia):
        return img  # fast path: nothing to do

    # ImageEnhance.Brightness/Contrast return a fresh image with an EMPTY
    # .info — snapshot EXIF/ICC/DPI and restore it on the result, or any
    # brightness/contrast pass would silently strip metadata (same
    # regression class grade.py documents for fromarray).
    info = dict(img.info)

    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")

    if grayscale:
        if img.mode == "RGBA":  # flatten alpha onto white before grayscale
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg.convert("L")
        else:
            img = img.convert("L")
        img = _final_tone(img, brightness, contrast, gamma, sharpen)
        img.info.update(info)
        return img

    if img.mode == "L" and (saturation != 1.0 or sepia):
        img = img.convert("RGB")

    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)

    if sepia:
        alpha = None
        if img.mode == "RGBA":
            alpha = img.split()[-1]
            img = img.convert("RGB")
        img = img.convert("RGB", matrix=_SEPIA_MATRIX)
        if alpha is not None:
            img.putalpha(alpha)

    img = _final_tone(img, brightness, contrast, gamma, sharpen)
    img.info.update(info)
    return img


def _final_tone(img: Image.Image, brightness: float, contrast: float,
                gamma: float, sharpen: float) -> Image.Image:
    """Brightness → contrast → gamma → sharpen, in that order."""
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if gamma > 0 and gamma != 1.0:
        lut = [min(255, int(round(255 * ((i / 255) ** (1.0 / gamma)))))
               for i in range(256)]
        if img.mode == "L":
            img = img.point(lut)
        elif img.mode == "RGBA":
            img = img.point(lut * 3 + list(range(256)))  # alpha stays untouched
        else:
            img = img.point(lut * 3)  # RGB: 256 entries per band
    if sharpen != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(sharpen)
    return img


# ── White balance & auto levels ──────────────────────────────────────────────

def _kelvin_rgb(kelvin: float) -> Tuple[float, float, float]:
    """Approximate sRGB of a blackbody at ``kelvin`` (Tanner Helland alg).

    Returns (r, g, b) each in [0, 255]; used to derive WB correction gains.
    """
    import math
    k = max(1000.0, min(40000.0, float(kelvin))) / 100.0
    if k <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(k) - 161.1195681661
        b = 0.0 if k <= 19 else 138.5177312231 * math.log(k - 10) - 305.0447927307
    else:
        r = 329.698727446 * (k - 60.0) ** -0.1332047592
        g = 288.1221695283 * (k - 60.0) ** -0.0755148492
        b = 255.0
    # clamp to >= 1.0: k <= 19 (~1900K, candlelight) gives b = 0.0 and
    # _temperature_gains would divide by zero
    return (max(1.0, min(255.0, r)),
            max(1.0, min(255.0, g)),
            max(1.0, min(255.0, b)))


def _temperature_gains(kelvin: float, neutral: float = 6500.0):
    """Per-channel multipliers correcting ``kelvin`` light back to ``neutral``."""
    r, g, b = _kelvin_rgb(kelvin)
    nr, ng, nb = _kelvin_rgb(neutral)
    return (nr / r, ng / g, nb / b)


def _reference_gains(reference_path: str):
    """Gains that make a (supposedly neutral gray) reference image neutral.

    Samples the mean RGB of a small downscale; target = mean of the three
    channels (the gray value a neutral patch should have had).
    """
    with Image.open(reference_path) as ref:
        ref = ref.convert("RGB").copy()
        ref.thumbnail((128, 128))
    # _flattened yields (r, g, b) tuples per pixel for RGB
    px = list(_flattened(ref))
    n = len(px)
    if n == 0:
        return (1.0, 1.0, 1.0)
    mr = sum(p[0] for p in px) / n
    mg = sum(p[1] for p in px) / n
    mb = sum(p[2] for p in px) / n
    target = (mr + mg + mb) / 3.0
    gain = lambda c: (target / c) if c > 0 else 1.0  # noqa: E731
    return (gain(mr), gain(mg), gain(mb))


def _apply_gains(img: Image.Image, gains) -> Image.Image:
    """Multiply R/G/B channels by gains, clipped to 255. RGB/RGBA/LA/L kept.

    Alpha-carrying modes apply the gains to the color bands and re-attach the
    original alpha — WB/EV used to silently no-op on RGBA/LA/P inputs.
    """
    gr, gg, gb = gains
    if img.mode == "L":
        if abs(gg - 1.0) < 1e-6:
            return img
        return img.point(lambda v: min(255, int(round(v * gg))))
    if img.mode == "LA":
        if abs(gg - 1.0) < 1e-6:
            return img
        lum, alpha = img.split()
        lum = lum.point(lambda v: min(255, int(round(v * gg))))
        out = Image.merge("LA", (lum, alpha))
        out.info = img.info.copy()
        return out
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode == "RGBA":
        alpha = img.split()[-1]
        rgb = img.convert("RGB")
        out = _apply_gains(rgb, gains)
        out.putalpha(alpha)
        out.info = img.info.copy()
        return out
    if img.mode != "RGB":
        return img
    if max(abs(gr - 1.0), abs(gg - 1.0), abs(gb - 1.0)) < 1e-6:
        return img
    r, g, b = img.split()
    if abs(gr - 1.0) >= 1e-6:
        r = r.point(lambda v: min(255, int(round(v * gr))))
    if abs(gg - 1.0) >= 1e-6:
        g = g.point(lambda v: min(255, int(round(v * gg))))
    if abs(gb - 1.0) >= 1e-6:
        b = b.point(lambda v: min(255, int(round(v * gb))))
    out = Image.merge("RGB", (r, g, b))
    out.info = img.info.copy()
    return out


def apply_white_balance(img: Image.Image, temp: Optional[float] = None,
                        reference: Optional[str] = None,
                        tint: float = 0.0) -> Image.Image:
    """Correct white balance from a Kelvin temperature or a reference image.

    ``temp`` (Kelvin) shifts R/B so the scene light is corrected back toward
    neutral 6500K. ``reference`` (path) samples a neutral-gray area and
    equalizes the channels. ``tint`` (range ~[-100, 100], 0 = none) adds a
    green(-) / magenta(+) tilt on the G/M axis, applied on top of the temp/
    reference gains (or alone). Neither temp nor reference nor tint →
    unchanged; non-RGB/L modes unchanged.
    """
    gains = None
    if reference:
        try:
            gains = _reference_gains(reference)
        except Exception:
            return img  # unreadable reference → never fail the pipeline
    elif temp:
        try:
            gains = _temperature_gains(float(temp))
        except (TypeError, ValueError):
            return img
    if tint:
        t = max(-1.0, min(1.0, float(tint) / 100.0))
        gr, gg, gb = gains if gains is not None else (1.0, 1.0, 1.0)
        # magenta(+t): R/B up, G down; green(-t): the reverse
        gains = (gr * (1.0 + 0.35 * t),
                 gg * (1.0 - 0.35 * t),
                 gb * (1.0 + 0.35 * t))
    if gains is None:
        return img
    return _apply_gains(img, gains)


def _mean_luminance(img: Image.Image) -> float:
    """Mean luminance (0-1) from a small grayscale sample."""
    sample = img.convert("L").copy()
    sample.thumbnail((128, 128))
    px = list(_flattened(sample))
    return (sum(px) / len(px) / 255.0) if px else 0.5


def apply_exposure(img: Image.Image, ev: Optional[float] = None,
                   auto_exposure: Optional[float] = None) -> Image.Image:
    """EV compensation (2^EV gain) and/or auto-exposure normalization.

    ``auto_exposure`` scales the current mean luminance up/down to the target
    (0-1); ``ev`` is a relative offset on top of that (or standalone, if no
    auto-exposure). Both are a single per-channel multiply, so they combine
    into one pass.
    """
    gain = 1.0
    if auto_exposure is not None:
        target = max(0.01, min(0.99, float(auto_exposure)))
        cur = _mean_luminance(img)
        if cur > 0:
            gain *= target / cur
    if ev:
        gain *= 2.0 ** float(ev)
    if abs(gain - 1.0) < 1e-6:
        return img
    return _apply_gains(img, (gain, gain, gain))


def apply_auto_levels(img: Image.Image, clip_percent: float = 2.0) -> Image.Image:
    """Auto levels: linear histogram stretch with a low/high clip.

    The clip percentiles are measured on a small grayscale sample; the same
    mapping is applied per-channel via a 256-entry point table (a global
    stretch, so white balance is preserved). Degenerate histograms are
    returned unchanged.
    """
    if img.mode not in ("RGB", "L", "RGBA"):
        img = img.convert("RGB")
    sample = img.convert("L").copy()
    sample.thumbnail((256, 256))
    hist = sample.histogram()
    total = sum(hist)
    if total == 0:
        return img
    cut = total * clip_percent / 100.0
    lo = 0
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= cut:
            lo = i
            break
    hi = 255
    acc = 0
    for i in range(255, -1, -1):
        acc += hist[i]
        if acc >= cut:
            hi = i
            break
    if hi <= lo:
        return img
    table = [0 if v <= lo else (255 if v >= hi
                                else int(round(255 * (v - lo) / (hi - lo))))
             for v in range(256)]
    if img.mode == "RGBA":
        return img.point(table * 3 + list(range(256)))  # alpha untouched
    if img.mode == "L":
        return img.point(table)  # single band → 256 entries
    return img.point(table * 3)  # RGB: 256 entries per band (Pillow >= 10.4)


# ── Composition: crop ────────────────────────────────────────────────────────

def apply_crop(img: Image.Image, crop: Optional[str]) -> Image.Image:
    """Crop to "WxH+X+Y" (offsets optional → centered crop).

    Out-of-bounds requests are clamped; unparseable specs are ignored.
    """
    if not crop:
        return img
    m = re.fullmatch(
        r"\s*(\d+)\s*x\s*(\d+)(?:\s*\+\s*(\d+)\s*\+\s*(\d+))?\s*", str(crop))
    if not m:
        return img
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return img
    if m.group(3) is not None:
        x, y = int(m.group(3)), int(m.group(4))
    else:
        x = max(0, (img.width - w) // 2)
        y = max(0, (img.height - h) // 2)
    # Clamp the crop box to the image bounds (PIL would otherwise pad black)
    x = max(0, x)
    y = max(0, y)
    x1, y1 = min(x + w, img.width), min(y + h, img.height)
    if x >= x1 or y >= y1:
        return img
    return img.crop((x, y, x1, y1))


def apply_crop_ratio(img: Image.Image, ratio: Optional[str]) -> Image.Image:
    """Center-crop to an aspect ratio like "16:9". Invalid → unchanged."""
    r = _parse_ratio(ratio)
    if not r:
        return img
    target = r[0] / r[1]
    current = img.width / img.height if img.height else 0
    if abs(current - target) < 1e-6:
        return img
    if current > target:  # too wide → crop width
        new_w = max(1, min(img.width, int(round(img.height * target))))
        x = (img.width - new_w) // 2
        return img.crop((x, 0, x + new_w, img.height))
    new_h = max(1, min(img.height, int(round(img.width / target))))
    y = (img.height - new_h) // 2
    return img.crop((0, y, img.width, y + new_h))


# ── Composition: rotate / flip / pad ─────────────────────────────────────────

def apply_rotate(img: Image.Image, degrees: float,
                 fill: Optional[str] = None) -> Image.Image:
    """Rotate by degrees; positive = clockwise (PIL rotates CCW).

    expand=True grows the canvas; corners are filled with ``fill`` (hex)
    or black. Non-color modes (P/L/1/LA/I/F/I;16) are converted so a fill
    color is meaningful; without a fill they keep their mode (scalar fill).
    """
    if not degrees:
        return img
    if img.mode == "P":
        img = img.convert("RGB")
    elif fill and img.mode not in ("RGB", "RGBA", "CMYK"):
        img = img.convert("RGB")

    if fill is not None:
        try:
            r, g, b = hex_to_rgb(fill)
        except ValueError:
            r, g, b = 0, 0, 0
        fillcolor = (r, g, b, 255) if img.mode == "RGBA" else (r, g, b)
    elif img.mode not in ("RGB", "RGBA", "CMYK"):
        # single-band / LA modes: PIL rotate requires a scalar fill color
        fillcolor = 0
    else:
        fillcolor = (0, 0, 0, 255) if img.mode == "RGBA" else (0, 0, 0)

    return img.rotate(-degrees, expand=True, resample=Image.BICUBIC,
                      fillcolor=fillcolor)


def apply_flip(img: Image.Image, flip: Optional[str]) -> Image.Image:
    """Mirror horizontally ("h") or vertically ("v"). Anything else: unchanged."""
    if flip == "h":
        return img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip == "v":
        return img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img


def apply_pad(img: Image.Image, ratio: Optional[str],
              bg: str = "#000000") -> Image.Image:
    """Letterbox the image inside a canvas with the given aspect ratio.

    EXIF/ICC live in ``img.info`` and ``Image.new`` starts with an empty
    ``info`` — the canvas copies it explicitly so metadata survives padding.
    """
    r = _parse_ratio(ratio)
    if not r:
        return img
    target = r[0] / r[1]
    try:
        r_, g_, b_ = hex_to_rgb(bg)
    except ValueError:
        r_, g_, b_ = 0, 0, 0

    current = img.width / img.height if img.height else 0
    if abs(current - target) < 1e-6:
        return img

    if current < target:  # too narrow → widen the canvas
        canvas_w = int(round(img.height * target))
        canvas_h = img.height
    else:  # too wide → heighten the canvas
        canvas_w = img.width
        canvas_h = int(round(img.width / target))

    mode = "RGBA" if img.mode in ("RGBA", "LA") else "RGB"
    canvas = Image.new(mode, (canvas_w, canvas_h), (r_, g_, b_, 255) if mode == "RGBA" else (r_, g_, b_))
    canvas.info = dict(img.info)  # keep EXIF/ICC through padding

    x = (canvas_w - img.width) // 2
    y = (canvas_h - img.height) // 2
    if img.mode in ("RGBA", "LA"):
        canvas.paste(img, (x, y), img)
    else:
        canvas.paste(img, (x, y))
    return canvas
