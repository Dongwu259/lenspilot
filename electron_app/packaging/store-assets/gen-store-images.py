#!/usr/bin/env python3
"""Generate Microsoft Store listing images (Partner Center assets).

Produces (packaging/store-assets/):
  store-logo-300x300.png   — required store logo
  hero-1920x1080-zh.png    — 16:9 promotional hero, Chinese
  hero-1920x1080-en.png    — 16:9 promotional hero, English

Usage:  python packaging/store-assets/gen-store-images.py
"""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.abspath(os.path.join(HERE, "..", ".."))
ICON = os.path.join(APP, "assets", "icon.png")

BG_TOP = (10, 14, 20)
BG_BOT = (18, 24, 34)
ACCENT = (59, 130, 246)   # --brand blue
ACCENT2 = (139, 92, 246)  # purple


def font(name, size):
    path = os.path.join("C:\\Windows\\Fonts", name)
    return ImageFont.truetype(path, size)


def gradient_bg(w, h):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img.convert("RGBA")


def glow(img, center, radius, color, alpha):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([center[0] - radius, center[1] - radius,
               center[0] + radius, center[1] + radius], fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius * 0.7))
    out = Image.alpha_composite(img, layer)
    return out


def icon_with_glow(base, icon, cx, cy, size, glow_radius=260):
    base = glow(base, (cx, cy), glow_radius, ACCENT, 46)
    base = glow(base, (cx + glow_radius * 0.3, cy + glow_radius * 0.4), glow_radius * 0.6, ACCENT2, 34)
    ic = icon.copy()
    ic.thumbnail((size, size), Image.LANCZOS)
    base.alpha_composite(ic, (cx - ic.width // 2, cy - ic.height // 2))
    return base


def chip(draw, x, y, text, fnt):
    pad_x, pad_y = 18, 9
    bbox = draw.textbbox((0, 0), text, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    w, h = tw + pad_x * 2, th + pad_y * 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                           fill=(255, 255, 255, 14), outline=(90, 130, 200, 90), width=1)
    draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), text, font=fnt, fill=(214, 226, 245))
    return x + w + 14, y


def store_logo(icon):
    img = Image.new("RGBA", (300, 300), BG_TOP + (255,))
    ic = icon.copy()
    ic.thumbnail((176, 176), Image.LANCZOS)
    img.alpha_composite(ic, ((300 - ic.width) // 2, (300 - ic.height) // 2))
    return img.convert("RGB")


def hero(icon, lang):
    W, H = 1920, 1080
    img = gradient_bg(W, H)
    zh = lang == "zh"

    # ── 左栏文案 ──
    name_fnt = font("arialbd.ttf", 118)
    tag_fnt = font("msyh.ttc" if zh else "segoeuil.ttf", 44)
    chip_fnt = font("msyh.ttc" if zh else "segoeui.ttf", 28)
    foot_fnt = font("msyh.ttc" if zh else "segoeui.ttf", 26)

    d = ImageDraw.Draw(img)
    x0 = 170
    name_y = 250
    d.text((x0, name_y), "LensPilot", font=name_fnt, fill=(236, 242, 250))
    d.text((x0 + 4, name_y + 165), "AI 摄影师工作流" if zh else "The AI workflow for photographers",
           font=tag_fnt, fill=(168, 185, 210))
    d.text((x0 + 4, name_y + 240),
           "选片 · 修图 · 调色 · 交付，一站式完成" if zh else "Cull, retouch, grade and deliver in one place",
           font=font("msyh.ttc" if zh else "segoeuil.ttf", 36), fill=(140, 158, 185))

    chips = ["AI 选片", "一键修图", "批量调色", "客户交付"] if zh else \
            ["AI Culling", "Auto Retouch", "Batch Grading", "Delivery"]
    cx = x0
    cy = name_y + 360
    for c in chips:
        cx, cy = chip(d, cx, cy, c, chip_fnt)

    d.text((x0, H - 130), "开源免费 · GPL-3.0 · 模型本地运行，照片不出你的电脑" if zh else
           "Free & open source (GPL-3.0) · local AI — photos never leave your PC",
           font=foot_fnt, fill=(120, 138, 165))

    # ── 右侧大图标 ──
    img = icon_with_glow(img, icon, 1520, 520, 560)
    return img.convert("RGB")


def main():
    icon = Image.open(ICON).convert("RGBA")
    store_logo(icon).save(os.path.join(HERE, "store-logo-300x300.png"))
    hero(icon, "zh").save(os.path.join(HERE, "hero-1920x1080-zh.png"))
    hero(icon, "en").save(os.path.join(HERE, "hero-1920x1080-en.png"))
    print("wrote store-logo-300x300.png, hero-1920x1080-{zh,en}.png")


if __name__ == "__main__":
    main()
