# -*- coding: utf-8 -*-
"""P1-3 冒烟：局部调整蒙版（方向性断言 + amount/invert/全类型覆盖）。

运行（仓库根）: python smoke_test/test_p2_masks.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p2_mask_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import photo_editor  # noqa: E402
from local_masks import apply_masks  # noqa: E402

H, W = 200, 300


def _gray():
    return Image.fromarray(np.full((H, W), 128, np.uint8))


def _mean(img, box):
    a = np.asarray(img, np.float32)
    x0, y0, x1, y1 = box
    return float(a[y0:y1, x0:x1].mean())


def main():
    base = _gray()

    # 1) 线性渐变 exposure+1：沿渐变方向单调变亮（右端远亮于左端）
    m = [{"id": 1, "type": "linear", "amount": 1.0, "invert": False,
          "params": {"x1": 0.0, "y1": 0.5, "x2": 1.0, "y2": 0.5},
          "adjustments": {"exposure": 1.0}}]
    out = apply_masks(base, m)
    assert _mean(out, (0, 0, 30, H)) + 30 < _mean(out, (W - 30, 0, W, H))
    assert _mean(out, (W // 2 - 15, 0, W // 2 + 15, H)) < _mean(out, (W - 30, 0, W, H))

    # 2) invert：方向反转（左端远亮于右端）
    m2 = [dict(m[0], invert=True)]
    out2 = apply_masks(base, m2)
    assert _mean(out2, (W - 30, 0, W, H)) + 30 < _mean(out2, (0, 0, 30, H))

    # 3) amount=0 无变化；amount 缩放偏移量
    out3 = apply_masks(base, [dict(m[0], amount=0.0)])
    assert abs(_mean(out3, (0, 0, W, H)) - 128) < 1e-6
    half = apply_masks(base, [dict(m[0], amount=0.5)])
    full = apply_masks(base, m)
    assert abs(_mean(half, (W - 30, 0, W, H)) - 128) < abs(_mean(full, (W - 30, 0, W, H)) - 128)

    # 4) 径向：中心亮于角落
    mr = [{"id": 1, "type": "radial", "amount": 1.0, "invert": False,
           "params": {"cx": 0.5, "cy": 0.5, "rx": 0.4, "ry": 0.4,
                      "angle": 0.0, "feather": 0.3},
           "adjustments": {"exposure": 1.5}}]
    outr = apply_masks(base, mr)
    assert _mean(outr, (W // 2 - 15, H // 2 - 15, W // 2 + 15, H // 2 + 15)) > \
        _mean(outr, (0, 0, 20, 20))

    # 5) 笔刷：盖章处亮于远处
    mb = [{"id": 1, "type": "brush", "amount": 1.0, "invert": False,
           "params": {"stamps": [{"x": 0.25, "y": 0.5, "r": 0.12}], "hardness": 0.6},
           "adjustments": {"exposure": 1.5}}]
    outb = apply_masks(base, mb)
    assert _mean(outb, (W // 4 - 8, H // 2 - 8, W // 4 + 8, H // 2 + 8)) > \
        _mean(outb, (W - 30, 0, W, H))

    # 6) 亮度范围：亮部(上半)被提亮、暗部(下半)几乎不变
    arr_lr = np.full((H, W, 3), 60, np.uint8)
    arr_lr[:100] = 200
    img_lr = Image.fromarray(arr_lr)
    ml = [{"id": 1, "type": "luminance", "amount": 1.0, "invert": False,
           "params": {"lo": 0.6, "hi": 1.0, "feather": 0.1},
           "adjustments": {"exposure": 0.8}}]
    outl = apply_masks(img_lr, ml)
    lift_bright = _mean(outl, (0, 10, W, 90)) - 200
    lift_dark = _mean(outl, (0, 110, W, 190)) - 60
    assert lift_bright > lift_dark + 20, (lift_bright, lift_dark)

    # 7) 颜色范围：红块提亮、灰底不动
    arr_c = np.full((H, W, 3), 128, np.uint8)
    arr_c[60:140, 100:200] = [200, 30, 30]
    img_c = Image.fromarray(arr_c)
    mc = [{"id": 1, "type": "color", "amount": 1.0, "invert": False,
           "params": {"rgb": [200, 30, 30], "tolerance": 0.15, "soft": 0.08},
           "adjustments": {"exposure": 1.0}}]
    outc = apply_masks(img_c, mc)
    assert _mean(outc, (120, 80, 180, 120)) > _mean(img_c, (120, 80, 180, 120))
    assert abs(_mean(outc, (0, 0, 60, 60)) - 128) < 2

    # 8) 天空启发式：上半蓝色区被提亮，下半灰区几乎不动
    img_s = np.full((H, W, 3), 128, np.uint8)
    img_s[:100] = [40, 70, 200]
    ms = [{"id": 1, "type": "sky", "amount": 1.0, "invert": False,
           "params": {}, "adjustments": {"exposure": 1.0}}]
    outs = apply_masks(Image.fromarray(img_s), ms)
    assert _mean(outs, (0, 0, W, 60)) - 128 > _mean(outs, (0, H - 60, W, H)) - 128 + 5

    # 9) apply_edits 全链路（masks 键经 photo_editor）+ 无调整蒙版是 no-op
    full = photo_editor.apply_edits(base, {"masks": m})
    assert _mean(full, (W - 30, 0, W, H)) > 128
    noop = photo_editor.apply_edits(base, {"masks": [dict(m[0], adjustments={})]})
    assert abs(_mean(noop, (0, 0, W, H)) - 128) < 1e-6

    # 10) 蒙版叠加是 LR 式顺序复合：同向两层 = 效果强化（非抵消）
    mm = [{"id": 1, "type": "linear", "amount": 1.0, "invert": False,
           "params": {"x1": 0.0, "y1": 0.5, "x2": 1.0, "y2": 0.5},
           "adjustments": {"exposure": 0.3}}]
    once = apply_masks(base, mm)
    twice = apply_masks(once, mm)
    assert _mean(twice, (W - 30, 0, W, H)) > _mean(once, (W - 30, 0, W, H)) + 8

    print("[ok] masks: 线性/反转/amount/径向/笔刷/亮度/颜色/天空/全链路/叠加 全部通过")
    print("P2 MASKS SMOKE PASSED")


if __name__ == "__main__":
    main()
