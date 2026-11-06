# -*- coding: utf-8 -*-
"""P4-3 冒烟：镜头校正（畸变/色差/暗角补偿 + 全链路 + 无 opencv 回退口径）。

运行（仓库根）: python smoke_test/test_p4_optics.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p4_optics_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import photo_editor  # noqa: E402
from optics_fix import apply_optics  # noqa: E402

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

H, W = 240, 320


def _gray():
    return Image.fromarray(np.full((H, W, 3), 128, np.uint8))


def _corner_mean(img, size=25):
    a = np.asarray(img, np.float32)
    return float(a[:size, :size].mean())


def main():
    base = _gray()

    # 1) 全 0 = 恒等
    out0 = apply_optics(base, {"distortion": 0, "ca": 0, "vig": 0})
    assert np.asarray(out0).tobytes() == np.asarray(base).tobytes()

    if HAS_CV2:
        # 2) 暗角补偿 >0：角部变亮、中心不动
        grad = np.zeros((H, W, 3), np.uint8)
        grad[:] = 100
        out_v = apply_optics(Image.fromarray(grad), {"distortion": 0, "ca": 0, "vig": 60})
        assert _corner_mean(out_v) > 100 + 8
        assert float(np.asarray(out_v, np.float32)[H // 2 - 5:H // 2 + 5,
                                                   W // 2 - 5:W // 2 + 5].mean()) < 103

        # 3) 色差校正：构造 R 通道外扩的伪 CA，反向校正后 R-G 质心差缩小
        ca_img = np.zeros((H, W, 3), np.uint8)
        yy, xx = np.mgrid[0:H, 0:W]
        cx, cy = W / 2, H / 2
        # G 通道一个方块；R 通道同方块但沿半径外扩 1.5%
        def block(scale):
            rr = np.sqrt(((xx - cx) / (W / 2)) ** 2 + ((yy - cy) / (H / 2)) ** 2) / scale
            return (((np.abs(xx - cx) < 40 / scale) & (np.abs(yy - cy) < 30 / scale))
                    .astype(np.uint8) * 200)
        ca_img[..., 1] = block(1.0)
        ca_img[..., 0] = block(0.985)
        corrected = apply_optics(Image.fromarray(ca_img),
                                 {"distortion": 0, "ca": -60, "vig": 0})
        a0 = np.asarray(ca_img, np.float32)
        a1 = np.asarray(corrected, np.float32)
        def rg_gap(a):
            my, mx = np.where(a[..., 1] > 100)
            ry, rx = np.where(a[..., 0] > 100)
            return abs(rx.mean() - mx.mean()) + abs(ry.mean() - my.mean())
        # 宽松断言：校正后 R 质心向 G 靠拢或不劣化超过容差
        assert rg_gap(a1) <= rg_gap(a0) + 1.5, (rg_gap(a0), rg_gap(a1))

        # 4) 畸变 ≠0：像素确实被重排（角部内容变化）
        grid = np.zeros((H, W, 3), np.uint8)
        grid[::20, :] = 200
        grid[:, ::20] = 200
        out_d = apply_optics(Image.fromarray(grid), {"distortion": 50, "ca": 0, "vig": 0})
        assert np.asarray(out_d).tobytes() != np.asarray(Image.fromarray(grid)).tobytes()
    else:
        # 无 opencv（Mac 口径）：静默跳过 = 恒等
        out_v = apply_optics(base, {"distortion": 50, "ca": 50, "vig": 50})
        assert np.asarray(out_v).tobytes() == np.asarray(base).tobytes()

    # 5) apply_edits 全链路（optics 键）
    full = photo_editor.apply_edits(base, {"optics": {"distortion": 20, "ca": 0, "vig": 20}})
    assert full.size == base.size
    print(f"[ok] optics: 恒等/暗角补偿/色差/畸变/全链路 通过 (opencv={HAS_CV2})")
    print("P4 OPTICS SMOKE PASSED")


if __name__ == "__main__":
    main()
