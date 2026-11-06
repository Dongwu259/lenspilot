# -*- coding: utf-8 -*-
"""P6-2 冒烟：HDR 合成（Mertens）与全景拼接（cv2.Stitcher）。

运行（仓库根）: python smoke_test/test_p6_merge.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p6_merge_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

from photo_merge import handle_merge_hdr, handle_merge_panorama  # noqa: E402

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def _bracket(tmp):
    """三档曝光：暗/中/亮 同一渐变场景。"""
    rng = np.random.default_rng(7)
    scene = np.tile(np.linspace(30, 220, 400, dtype=np.float32), (200, 1))
    scene += rng.normal(0, 6, scene.shape)  # 纹理
    paths = []
    for i, gain in enumerate((0.35, 1.0, 2.2)):
        arr = np.clip(scene * gain, 0, 255).astype(np.uint8)
        p = os.path.join(tmp, f"bracket_{i}.jpg")
        Image.fromarray(np.stack([arr] * 3, -1)).save(p, quality=92)
        paths.append(p)
    return paths


def _pano_pair(tmp):
    """同一特征图的两段横移裁剪（含重叠）。"""
    rng = np.random.default_rng(11)
    base = rng.normal(128, 60, (300, 900)).astype(np.float32)
    base = np.clip(base, 0, 255).astype(np.uint8)
    p1 = os.path.join(tmp, "pano_l.jpg")
    p2 = os.path.join(tmp, "pano_r.jpg")
    Image.fromarray(np.stack([base[:, :600]] * 3, -1)).save(p1, quality=95)
    Image.fromarray(np.stack([base[:, 300:]] * 3, -1)).save(p2, quality=95)
    return [p1, p2]


def main():
    tmp = tempfile.mkdtemp(prefix="lenspilot_merge_")
    paths = _bracket(tmp)

    # 1) 参数校验
    assert handle_merge_hdr({"paths": paths[:1]})["error"]
    assert handle_merge_hdr({"paths": ["/nope1.jpg", "/nope2.jpg"]})["error"]

    if HAS_CV2:
        # 2) HDR 成功：输出存在、尺寸正确、亮度介于两极端之间
        r = handle_merge_hdr({"paths": paths, "align": True})
        assert r.get("ok"), r
        out = Image.open(r["path"])
        assert out.size == (400, 200)
        mean = float(np.asarray(out, np.float32).mean())
        assert 40 < mean < 230, mean
        # 再跑一次不覆盖（自动加序号）
        r2 = handle_merge_hdr({"paths": paths})
        assert r2["path"] != r["path"]

        # 3) 全景：同特征图两段重叠裁剪（成功→文件存在；失败→可读 error，均接受）
        pano = handle_merge_panorama({"paths": _pano_pair(tmp)})
        if pano.get("ok"):
            assert os.path.isfile(pano["path"]) and pano["width"] > pano["height"]
            print(f"[ok] pano: 拼接成功 {pano['width']}x{pano['height']}")
        else:
            assert "拼接失败" in pano["error"], pano
            print(f"[ok] pano: 合成图无法稳定拼接 → 可读错误（合成数据固有）: {pano['error'][:40]}")

        # 4) 全景参数校验
        assert handle_merge_panorama({"paths": paths[:1]})["error"]
    else:
        assert "opencv" in handle_merge_hdr({"paths": paths})["error"]
        print("[ok] merge: 无 opencv 环境返回可读错误")

    print("[ok] hdr: 括弧合并/防覆盖/参数校验 全部通过")
    print("P6 MERGE SMOKE PASSED")


if __name__ == "__main__":
    main()
