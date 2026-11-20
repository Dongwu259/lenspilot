#!/usr/bin/env python3
"""Generate MSIX tile/splash/logo assets from assets/icon.png.

Outputs packaging/msix/Assets/ with the MRT naming convention
(Name.scale-100.png / Name.targetsize-N.png / ..._altform-unplated.png).
No PRI file needed — Windows resolves scale files by naming convention.

Usage:  python packaging/msix/gen-msix-assets.py
"""
import json
import os
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.abspath(os.path.join(HERE, "..", ".."))
ICON = os.path.join(APP, "assets", "icon.png")
OUT = os.path.join(HERE, "Assets")

BG = (15, 18, 22, 255)  # 与 AppxManifest.template.xml 的 BackgroundColor 保持一致

# (基础文件名, 尺寸表: scale -> (宽, 高), 是否铺底色)
TILES = [
    ("StoreLogo",        {100: (50, 50), 125: (63, 63), 150: (75, 75), 200: (100, 100), 400: (200, 200)}, True),
    ("Square44x44Logo",  {100: (44, 44), 125: (55, 55), 150: (66, 66), 200: (88, 88), 400: (176, 176)}, False),
    ("Square150x150Logo", {100: (150, 150), 125: (188, 188), 150: (225, 225), 200: (300, 300), 400: (600, 600)}, True),
    ("Wide310x150Logo",  {100: (310, 150), 125: (388, 188), 150: (465, 225), 200: (620, 300), 400: (1240, 600)}, True),
    ("Square310x310Logo", {100: (310, 310), 125: (388, 388), 150: (465, 465), 200: (620, 620), 400: (1240, 1240)}, True),
    ("SplashScreen",     {100: (620, 300), 125: (775, 375), 150: (930, 450), 200: (1240, 600), 400: (2480, 1200)}, True),
]

TARGETSIZES = [16, 24, 30, 36, 48, 60, 72, 96, 256]


def compose(icon, size, plated):
    """居中放置图标；plated=True 时铺主题底色（磁贴/启动画面）。"""
    canvas = Image.new("RGBA", size, BG if plated else (0, 0, 0, 0))
    # plated 时图标占短边 62%，留白呼吸感； unplated 全尺寸
    ratio = 0.62 if plated else 1.0
    target = int(min(size) * ratio)
    src = icon.copy()
    src.thumbnail((target, target), Image.LANCZOS)
    x = (size[0] - src.width) // 2
    y = (size[1] - src.height) // 2
    canvas.alpha_composite(src, (x, y))
    return canvas


def main():
    icon = Image.open(ICON).convert("RGBA")
    os.makedirs(OUT, exist_ok=True)
    count = 0
    for name, scales, plated in TILES:
        for scale, (w, h) in scales.items():
            compose(icon, (w, h), plated).save(
                os.path.join(OUT, f"{name}.scale-{scale}.png"))
            count += 1
        # manifest 引用的是无后缀基础名，必须实际存在（等同于 scale-100）
        w, h = scales[100]
        compose(icon, (w, h), plated).save(os.path.join(OUT, f"{name}.png"))
        count += 1
    for ts in TARGETSIZES:
        compose(icon, (ts, ts), False).save(
            os.path.join(OUT, f"Square44x44Logo.targetsize-{ts}.png"))
        compose(icon, (ts, ts), False).save(
            os.path.join(OUT, f"Square44x44Logo.targetsize-{ts}_altform-unplated.png"))
        count += 2
    print(f"wrote {count} assets -> {OUT}")


if __name__ == "__main__":
    main()
