# -*- coding: utf-8 -*-
"""P4-1 冒烟：蒙版组件组合（add=并集 / sub=减去 / inter=相交 + 旧格式兼容）。

运行（仓库根）: python smoke_test/test_p4_combine.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p4_mc_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

from local_masks import apply_masks  # noqa: E402

H, W = 200, 300


def _gray():
    return Image.fromarray(np.full((H, W, 3), 128, np.uint8))


def _mean(img, box):
    a = np.asarray(img, np.float32)
    x0, y0, x1, y1 = box
    return float(a[y0:y1, x0:x1].mean())


LINEAR = {"type": "linear", "params": {"x1": 0.0, "y1": 0.5, "x2": 1.0, "y2": 0.5}}
# 该渐变 t = nx：左 0 → 右 1；左/中/右三段的平均 mask ≈ 0.05 / 0.5 / 0.95


def main():
    base = _gray()
    adj = {"exposure": 1.0}

    # 1) 旧格式（顶层 type/params）仍然工作 = 基线
    old = [{"id": 1, "type": "linear", "amount": 1.0, "invert": False,
            "params": LINEAR["params"], "adjustments": adj}]
    out_old = apply_masks(base, old)
    assert _mean(out_old, (W - 30, 0, W, H)) > _mean(out_old, (0, 0, 30, H)) + 20

    # 2) 新格式单组件 ≈ 旧格式（等价性）
    new1 = [{"id": 1, "amount": 1.0, "invert": False, "adjustments": adj,
             "components": [dict(LINEAR, mode="add")]}]
    out_new = apply_masks(base, new1)
    assert abs(_mean(out_new, (0, 0, W, H)) - _mean(out_old, (0, 0, W, H))) < 1.5

    # 3) sub 减去：linear(全宽提亮) - radial(中心) → 中心被"挖回"原亮度
    combo_sub = [{"id": 1, "amount": 1.0, "invert": False, "adjustments": adj,
                  "components": [
                      dict(LINEAR, mode="add"),
                      {"type": "radial", "mode": "sub",
                       "params": {"cx": 0.5, "cy": 0.5, "rx": 0.25, "ry": 0.25,
                                  "angle": 0, "feather": 0.1}}]}]
    out_sub = apply_masks(base, combo_sub)
    right = _mean(out_sub, (W - 30, 0, W, H))
    center = _mean(out_sub, (W // 2 - 15, H // 2 - 15, W // 2 + 15, H // 2 + 15))
    # 右端仍被提亮；中心处 linear mask≈0.5 本应提亮，被 sub 挖回后明显低于右端提亮量
    assert right > 128 + 20
    assert center < right - 10, (center, right)

    # 4) inter 相交：linear ∩ 亮度范围 → 只有同时满足处提亮。
    #    纵向渐变(底→顶, 顶部=1) 与 亮度蒙版(上半亮=1) 相交：顶部强、底部无。
    vert = {"type": "linear", "params": {"x1": 0.5, "y1": 1.0, "x2": 0.5, "y2": 0.0}}
    lum = {"type": "luminance", "params": {"lo": 0.4, "hi": 0.8, "feather": 0.05}}
    arr_l = np.full((H, W, 3), 60, np.uint8)
    arr_l[:100] = 200  # 上半亮 → lum mask 顶 1 底 0
    img_l = Image.fromarray(arr_l)
    combo_inter = [{"id": 1, "amount": 1.0, "invert": False,
                    "adjustments": {"exposure": 0.6},
                    "components": [dict(vert, mode="add"), dict(lum, mode="inter")]}]
    out_i = apply_masks(img_l, combo_inter)
    top_lift = _mean(out_i, (0, 10, W, 80)) - 200
    bottom = _mean(out_i, (0, 130, W, 190)) - 60
    # 垂直渐变在顶部 mask≈1 与 lum≈1 相乘 → 顶部提升；底部 linear≈0 → 不变
    assert top_lift > 20, top_lift
    assert abs(bottom) < 3, bottom

    # 5) 三组件链：linear + add radial(右半) + sub 全屏微降 → 组合仍有界 [0,1]
    combo3 = [{"id": 1, "amount": 1.0, "invert": False, "adjustments": adj,
               "components": [
                   dict(LINEAR, mode="add"),
                   {"type": "radial", "mode": "add",
                    "params": {"cx": 0.9, "cy": 0.5, "rx": 0.3, "ry": 0.3,
                               "angle": 0, "feather": 0.2}},
                   {"type": "linear", "mode": "sub",
                    "params": {"x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 0.0}}]}]
    out3 = apply_masks(base, combo3)
    assert out3.size == base.size  # 有界性与不崩即通过（细节由前述用例覆盖）

    print("[ok] combine: 旧格式兼容/单组件等价/sub 挖洞/inter 相交/三组件链 全部通过")
    print("P4 COMBINE SMOKE PASSED")


if __name__ == "__main__":
    main()
