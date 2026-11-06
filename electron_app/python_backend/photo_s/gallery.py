"""
PhotoS - HTML Gallery Generator

Builds a self-contained HTML gallery: index.html + downscaled JPEG
thumbnails, linking through to the original files via relative paths.
"""

import html
import os
from pathlib import Path
from typing import List

from PIL import Image


def _make_thumb(src: str, out_path: str, thumb_size: int = 360,
                quality: int = 80) -> bool:
    """Downscale an image to a JPEG thumbnail. Returns True on success."""
    try:
        with Image.open(src) as img:
            img = img.copy()
            img.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(out_path, "JPEG", quality=quality)
        return True
    except Exception:
        return False


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 0; background: #111; color: #eee; font: 14px/1.5 system-ui, sans-serif; }}
  h1 {{ padding: 16px 20px 0; font-weight: 600; }}
  .count {{ padding: 0 20px; color: #888; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
          gap: 12px; padding: 20px; }}
  .card {{ display: block; background: #1c1c1c; border-radius: 8px; overflow: hidden;
          text-decoration: none; color: #ccc; }}
  .card img {{ width: 100%; height: 200px; object-fit: cover; display: block; }}
  .cap {{ display: block; padding: 8px 10px; font-size: 12px;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="count">{count} 张 images</div>
<div class="grid">
{cards}
</div>
</body>
</html>
"""


def build_gallery(paths: List[str], out_dir: str, title: str = "PhotoS Gallery",
                  thumb_size: int = 360) -> dict:
    """Generate an HTML gallery at out_dir/index.html.

    Thumbnails land in out_dir/thumbs/; each card links to the original via a
    relative path (works when the relative structure is preserved on deploy).
    When no relative path exists (Windows: source on a different drive than
    out_dir) the card falls back to an absolute file:// link instead of
    aborting the whole build. Unreadable sources are skipped.
    Returns {"output", "count"}.
    """
    out = Path(out_dir)
    thumbs = out / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    items = []
    for i, src in enumerate(paths, 1):
        thumb = thumbs / f"{i}.jpg"
        if _make_thumb(src, str(thumb), thumb_size):
            try:
                rel = os.path.relpath(src, str(out))
            except ValueError:
                # Windows: different drives have no relative path between
                # them — link the original absolutely rather than crash.
                rel = Path(src).absolute().as_uri()
            items.append((rel, html.escape(Path(src).name), f"thumbs/{i}.jpg"))

    cards = "\n".join(
        f'    <a class="card" href="{html.escape(rel)}">'
        f'<img loading="lazy" src="{html.escape(tsrc)}" alt="{name}">'
        f'<span class="cap">{name}</span></a>'
        for rel, name, tsrc in items
    )
    index = out / "index.html"
    index.write_text(_TEMPLATE.format(title=html.escape(title),
                                      count=len(items), cards=cards),
                     encoding="utf-8")
    return {"output": str(index), "count": len(items)}
