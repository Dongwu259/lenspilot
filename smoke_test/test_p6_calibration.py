# -*- coding: utf-8 -*-
"""P6-3 冒烟：相机校准（主色饱和缩放 / 暗部色调 / 恒等 / 全链路）。

运行（仓库根）: python smoke_test/test_p6_calibration.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p6_cal_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import photo_editor  # noqa: E402
from calibration import apply_calibration  # noqa: E402

H, W = 120, 200


def _patch_img():
    """灰底 + 纯红块(左) + 纯蓝块(右)，下半压暗（供暗部色调）。"""
    arr = np.full((H, W, 3), 128, np.uint8)
    arr[20:60, 20:80] = [200, 40, 40]
    arr[20:60, 120:180] = [40, 40, 200]
    arr[70:110] //= 3  # 下半暗部
    return Image.fromarray(arr)


def _sat_of(img, box):
    a = np.asarray(img, np.float32)
    x0, y0, x1, y1 = box
    r = a[y0:y1, x0:x1]
    return float(r.max(-1).mean() - r.min(-1).mean())


def main():
    img = _patch_img()

    # 1) 全 0 = 恒等
    out0 = apply_calibration(img, {k: 0 for k in
                                   ("shadows_tint", "r_hue", "r_sat",
                                    "g_hue", "g_sat", "b_hue", "b_sat")})
    assert np.asarray(out0).tobytes() == np.asarray(img).tobytes()

    # 2) 红原色饱和 +：红块更艳、蓝块基本不动、灰底几乎不动
    out_r = apply_calibration(img, {"r_hue": 0, "r_sat": 100})
    red_gain = _sat_of(out_r, (25, 25, 75, 55)) - _sat_of(img, (25, 25, 75, 55))
    blue_gain = _sat_of(out_r, (125, 25, 175, 55)) - _sat_of(img, (125, 25, 175, 55))
    assert red_gain > 5, red_gain
    assert abs(blue_gain) < 8, blue_gain

    # 3) 蓝原色饱和 +：蓝块更艳、红块不动
    out_b = apply_calibration(img, {"b_hue": 0, "b_sat": 100})
    assert _sat_of(out_b, (125, 25, 175, 55)) - _sat_of(img, (125, 25, 175, 55)) > 5
    assert abs(_sat_of(out_b, (25, 25, 75, 55)) - _sat_of(img, (25, 25, 75, 55))) < 8

    # 4) 阴影色调 −(绿)：下半暗部 G 相对 R/B 抬升；上半亮部不动
    out_t = apply_calibration(img, {"shadows_tint": -100})
    a0 = np.asarray(img, np.float32)
    a1 = np.asarray(out_t, np.float32)
    dark_before = float((a0[80:100, 90:110, 1] - (a0[80:100, 90:110, 0] + a0[80:100, 90:110, 2]) / 2).mean())
    dark_after = float((a1[80:100, 90:110, 1] - (a1[80:100, 90:110, 0] + a1[80:100, 90:110, 2]) / 2).mean())
    assert dark_after > dark_before + 1, (dark_before, dark_after)

    # 5) apply_edits 全链路
    full = photo_editor.apply_edits(img, {"calibration": {"r_sat": 80}})
    assert _sat_of(full, (25, 25, 75, 55)) > _sat_of(img, (25, 25, 75, 55))
    print("[ok] calibration: 恒等/主色饱和缩放(红蓝互不串扰)/暗部色调/全链路 通过")
    print("P6 CALIBRATION SMOKE PASSED")


if __name__ == "__main__":
    main()
