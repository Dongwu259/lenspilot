"""
PhotoS - Watermark Rendering

Text and image watermark overlay on PIL Images.
"""

import os
import sys
from PIL import Image, ImageDraw, ImageFont


def _get_system_fonts():
    """Return platform-specific system font paths for watermark text rendering."""
    if sys.platform == "darwin":
        return [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
        ]
    elif sys.platform == "win32":
        windir = os.environ.get("WINDIR", "C:\\Windows")
        return [
            f"{windir}\\Fonts\\segoeui.ttf",
            f"{windir}\\Fonts\\msyh.ttc",
            f"{windir}\\Fonts\\simhei.ttf",
        ]
    else:  # Linux and others
        return [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]


# ── Position constants ────────────────────────────────────────────────────

POSITIONS = {
    "CENTER":        ("center", "center"),
    "TOP_LEFT":      ("left", "top"),
    "TOP_RIGHT":     ("right", "top"),
    "BOTTOM_LEFT":   ("left", "bottom"),
    "BOTTOM_RIGHT":  ("right", "bottom"),
    "TOP":           ("center", "top"),
    "BOTTOM":        ("center", "bottom"),
}

DEFAULT_POSITION = "BOTTOM_RIGHT"


def _get_position_xy(img_w: int, img_h: int, wm_w: int, wm_h: int,
                     position: str, margin: int = 20):
    """Calculate (x, y) for watermark placement."""
    # Case-insensitive: library callers may pass "top_left"; the constants and
    # CLI choices are uppercase. Unknown values still fall back to bottom-right.
    h_align, v_align = POSITIONS.get(str(position).upper(),
                                     POSITIONS["BOTTOM_RIGHT"])

    if h_align == "left":
        x = margin
    elif h_align == "right":
        x = img_w - wm_w - margin
    else:  # center
        x = (img_w - wm_w) // 2

    if v_align == "top":
        y = margin
    elif v_align == "bottom":
        y = img_h - wm_h - margin
    else:  # center
        y = (img_h - wm_h) // 2

    return x, y


def _apply_opacity(img: Image.Image, opacity: int) -> Image.Image:
    """Apply opacity (0-100) to an RGBA image overlay."""
    if opacity >= 100:
        return img
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[3]
    alpha = alpha.point(lambda p: p * opacity // 100)
    img.putalpha(alpha)
    return img


def apply_text_watermark(
    img: Image.Image,
    text: str,
    position: str = DEFAULT_POSITION,
    font_path: str = "",
    font_size: int = 36,
    color: str = "#FFFFFF",
    opacity: int = 50,
    margin: int = 20,
) -> Image.Image:
    """Overlay semi-transparent text watermark on an image.

    Returns a new RGBA Image with the watermark applied.
    """
    if not text:
        return img

    # Ensure RGBA for compositing
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Load font
    try:
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            # Try cross-platform system font fallback
            for sys_font in _get_system_fonts():
                try:
                    font = ImageFont.truetype(sys_font, font_size)
                    break
                except Exception:
                    continue
            else:
                font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # Measure text
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x, y = _get_position_xy(img.width, img.height, tw, th, position, margin)

    # Draw text on overlay
    draw.text((x, y), text, font=font, fill=color)

    # Apply opacity to overlay
    overlay = _apply_opacity(overlay, opacity)

    # Composite
    return Image.alpha_composite(img, overlay)


def apply_image_watermark(
    img: Image.Image,
    overlay_path: str,
    position: str = DEFAULT_POSITION,
    scale: int = 15,  # overlay width as % of image width
    opacity: int = 50,
    margin: int = 20,
) -> Image.Image:
    """Overlay an image watermark (logo) on an image.

    Returns a new RGBA Image with the watermark applied.
    """
    if not overlay_path:
        return img

    # Ensure RGBA
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Load overlay image
    try:
        wm = Image.open(overlay_path).convert("RGBA")
    except Exception:
        return img

    # Scale overlay (clamp to >= 1px; a 0-size resize raises in Pillow)
    if scale <= 0:
        return img
    target_w = max(1, int(img.width * scale / 100))
    ratio = target_w / wm.width
    target_h = max(1, int(wm.height * ratio))
    wm = wm.resize((target_w, target_h), Image.LANCZOS)

    # Apply opacity
    wm = _apply_opacity(wm, opacity)

    # Position
    x, y = _get_position_xy(img.width, img.height, wm.width, wm.height,
                            position, margin)

    # Composite
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay.paste(wm, (x, y), wm)
    return Image.alpha_composite(img, overlay)
