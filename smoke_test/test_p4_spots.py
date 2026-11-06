# -*- coding: utf-8 -*-
"""P4-2 冒烟：污点修复 / 仿制 / 红眼（协议 + 方向性断言 + 旧格式兼容）。

运行（仓库根）: python smoke_test/test_p4_spots.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p4_spot_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import photo_editor  # noqa: E402
from local_spots import apply_spots  # noqa: E402

H, W = 200, 300


def _spot_img():
    """灰底 + 中心黑污点。"""
    arr = np.full((H, W, 3), 128, np.uint8)
    cv = H // 2, W // 2
    arr[cv[0] - 8:cv[0] + 8, cv[1] - 8:cv[1] + 8] = 20
    return Image.fromarray(arr)


def _mean(img, box):
    a = np.asarray(img, np.float32)
    x0, y0, x1, y1 = box
    return float(a[y0:y1, x0:x1].mean())


def main():
    base = _spot_img()

    # 1) heal：黑污点被填补（中心亮度显著回升、接近周边灰）
    out = apply_spots(base, [{"id": 1, "mode": "heal", "x": 0.5, "y": 0.5,
                              "r": 10 / min(H, W)}], None)
    center = _mean(out, (W // 2 - 6, H // 2 - 6, W // 2 + 6, H // 2 + 6))
    assert center > 100, f"heal 后中心仍偏暗: {center}"
    assert abs(_mean(out, (0, 0, 30, 30)) - 128) < 1  # 远处不动

    # 2) clone：从右侧干净区复制纹理覆盖污点（方向性：源区 ≈ 目标区）
    src = _mean(base, (W // 2 + 40, H // 2 - 5, W // 2 + 50, H // 2 + 5))
    out2 = apply_spots(base, [{"id": 1, "mode": "clone", "x": 0.5, "y": 0.5,
                               "r": 10 / min(H, W),
                               "sx": (W // 2 + 45) / W, "sy": 0.5}], None)
    tgt = _mean(out2, (W // 2 - 5, H // 2 - 5, W // 2 + 5, H // 2 + 5))
    assert tgt > 100 and abs(tgt - src) < 30, (tgt, src)

    # 3) redeye：红色瞳孔被压暗去红
    arr = np.full((H, W, 3), 110, np.uint8)
    arr[90:110, 140:160] = [220, 40, 40]
    eye = Image.fromarray(arr)
    out3 = apply_spots(eye, None, [{"id": 1, "x": 0.5, "y": 0.5, "r": 14 / min(H, W)}])
    a3 = np.asarray(out3, np.float32)
    redness_before = float((a3[95:105, 145:155, 0] - a3[95:105, 145:155, 1]).mean())
    redness_after = redness_before  # 占位，下面重算
    before = float((np.asarray(eye, np.float32)[95:105, 145:155, 0]
                    - np.asarray(eye, np.float32)[95:105, 145:155, 1]).mean())
    redness_after = float((a3[95:105, 145:155, 0] - a3[95:105, 145:155, 1]).mean())
    assert redness_after < before - 30, (before, redness_after)

    # 4) 出界源点 clone 安全跳过（不崩）
    out4 = apply_spots(base, [{"id": 1, "mode": "clone", "x": 0.02, "y": 0.02,
                               "r": 0.1, "sx": 0.98, "sy": 0.98}], None)
    assert out4.size == base.size

    # 5) apply_edits 全链路（spots/redeye 键）+ 旧格式兼容（mode 缺省=heal）
    full = photo_editor.apply_edits(_spot_img(), {
        "spots": [{"id": 1, "x": 0.5, "y": 0.5, "r": 10 / min(H, W)}]})  # 无 mode → heal
    assert _mean(full, (W // 2 - 6, H // 2 - 6, W // 2 + 6, H // 2 + 6)) > 100
    chained = photo_editor.apply_edits(_spot_img(), {
        "spots": [{"id": 1, "mode": "heal", "x": 0.5, "y": 0.5, "r": 10 / min(H, W)}],
        "redeye": [{"id": 2, "x": 0.5, "y": 0.5, "r": 14 / min(H, W)}],
        "masks": [{"id": 3, "type": "linear", "amount": 1.0, "invert": False,
                   "params": {"x1": 0, "y1": 0.5, "x2": 1, "y2": 0.5},
                   "adjustments": {"exposure": 0.3}}]})
    assert chained.size == base.size
    print("[ok] spots: heal/clone/redeye/出界安全/全链路/旧格式 全部通过")
    print("P4 SPOTS SMOKE PASSED")


if __name__ == "__main__":
    main()
