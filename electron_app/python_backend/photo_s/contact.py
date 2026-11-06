"""
PhotoS - Contact Sheet

Builds a grid montage (contact sheet) of many images. Thumbnails decode each
original fully (PIL has no partial decode), then immediately downscale and
drop the reference — peak memory is one original at a time, not the whole set.
"""

import math
import os
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

CAPTION_H = 18  # pixels reserved per cell for the filename caption


def build_contact_sheet(
    files: List[str],
    output: str,
    cols: int = 4,
    thumb_size: Tuple[int, int] = (240, 240),
    captions: bool = True,
    bg: Tuple[int, int, int] = (0, 0, 0),
    padding: int = 8,
) -> str:
    """Build a contact sheet from a list of image files and save it.

    Args:
        files: Input image paths (opened lazily; unreadable files get a
               placeholder cell).
        output: Output image path (.jpg/.png/... — format from extension).
        cols: Number of thumbnails per row.
        thumb_size: Max thumbnail box (w, h) in pixels.
        captions: Draw the filename under each thumbnail.
        bg: Background color as an (r, g, b) tuple.
        padding: Gap (px) around each thumbnail.

    Returns:
        The absolute path of the saved contact sheet.

    Raises:
        ValueError: If cols < 1 (a zero/negative column count would crash
                    the grid math with ZeroDivisionError/ValueError).
    """
    if cols < 1:
        raise ValueError(f"cols must be >= 1, got {cols}")
    tw, th = thumb_size
    tw, th = max(1, tw), max(1, th)  # a 0-sized box crashes thumbnail()
    cell_w = tw + 2 * padding
    cell_h = th + 2 * padding + (CAPTION_H if captions else 0)
    rows = max(1, math.ceil(len(files) / max(1, cols)))
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), bg)
    draw = ImageDraw.Draw(sheet)
    font = None
    try:
        font = ImageFont.load_default()
    except Exception:
        pass

    for idx, path in enumerate(files):
        col = idx % cols
        row = idx // cols
        x0 = col * cell_w
        y0 = row * cell_h

        thumb = None
        try:
            with Image.open(path) as im:
                im.load()
                thumb = im.copy()
        except Exception:
            thumb = None

        if thumb is None:
            # placeholder cell for unreadable files
            draw.rectangle((x0, y0, x0 + cell_w - 1, y0 + cell_h - 1),
                           fill=(60, 60, 60))
        else:
            thumb.thumbnail((tw, th), Image.LANCZOS)
            if thumb.mode not in ("RGB", "RGBA"):
                thumb = thumb.convert("RGB")
            ox = x0 + (cell_w - thumb.width) // 2
            oy = y0 + (cell_h - thumb.height) // 2 - (CAPTION_H // 2 if captions else 0)
            if thumb.mode == "RGBA":
                placeholder = Image.new("RGB", thumb.size, bg)
                placeholder.paste(thumb, mask=thumb.split()[-1])
                sheet.paste(placeholder, (ox, oy))
            else:
                sheet.paste(thumb, (ox, oy))

        if captions:
            name = os.path.basename(path)
            if len(name) > 24:
                name = name[:21] + "..."
            if font is not None:
                draw.text((x0 + 2, y0 + cell_h - CAPTION_H), name,
                          fill=(230, 230, 230), font=font)
            else:
                draw.text((x0 + 2, y0 + cell_h - CAPTION_H), name,
                          fill=(230, 230, 230))

    out_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(out_dir, exist_ok=True)
    sheet.save(output)
    return os.path.abspath(output)
