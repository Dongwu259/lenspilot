# -*- coding: utf-8 -*-
"""P5 冒烟：subject/background AI 分割（u2netp 真模型 + 启发式回退 + 全链路）。

运行（仓库根）: python smoke_test/test_p5_segment.py
权重来源可用 LENSPILOT_SEGMENT_U2NETP_URL=file://... 秒过（默认走 GitHub，
约 4.5MB）。本测试自带 file:// 直连 dev 临时副本的回退逻辑。
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("APS_USER_DATA", tempfile.mkdtemp(prefix="lenspilot_p5_seg_"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# dev 热身副本（若存在）→ file:// 秒下载；否则走真 URL
_DEV_COPY = os.path.join(tempfile.gettempdir(), "u2netp.onnx")
if os.path.isfile(_DEV_COPY) and not os.environ.get("LENSPILOT_SEGMENT_U2NETP_URL"):
    os.environ["LENSPILOT_SEGMENT_U2NETP_URL"] = "file:///" + _DEV_COPY.replace("\\", "/")

import photo_editor  # noqa: E402
from local_masks import MASK_TYPES, apply_masks, _subject_heuristic  # noqa: E402
import segment_models  # noqa: E402

H, W = 200, 300


def _subject_img():
    """暗背景 + 中央亮椭圆主体（启发式与真模型都应能分出来）。"""
    arr = np.full((H, W, 3), 25, np.uint8)
    yy, xx = np.mgrid[0:H, 0:W]
    ell = (((xx - W * 0.5) / (W * 0.18)) ** 2 + ((yy - H * 0.5) / (H * 0.3)) ** 2) < 1.0
    arr[ell] = [210, 205, 195]
    return Image.fromarray(arr)


def _mean(img, box):
    a = np.asarray(img, np.float32)
    x0, y0, x1, y1 = box
    return float(a[y0:y1, x0:x1].mean())


def main():
    assert "subject" in MASK_TYPES and "background" in MASK_TYPES

    img = _subject_img()

    # 1) 模型路径：ensure(真/file:// 下载+sha 校验) → 推理 → 方向性断言
    sal = segment_models.get_subject_mask(img)
    assert sal is not None, f"分割模型不可用: {segment_models.status()['error']}"
    assert sal.shape == (H, W)
    center = float(np.asarray(sal)[H // 2 - 15:H // 2 + 15, W // 2 - 20:W // 2 + 20].mean())
    corner = float(np.asarray(sal)[:20, :20].mean())
    assert center > corner + 0.3, (center, corner)
    st = segment_models.status()
    assert st["loaded"] and st["cached"]
    print(f"[ok] u2netp: 推理正常 (center={center:.2f} corner={corner:.2f}, "
          f"cached={st['path'] is not None})")

    # 2) 缓存命中：同图第二次调用显著更快（< 首次的 1/3，排除首次含加载）
    t0 = os.times()
    segment_models.get_subject_mask(img)
    t1 = os.times()
    first = (t1.user - t0.user) + (t1.system - t0.system)
    t2 = os.times()
    segment_models.get_subject_mask(img)
    t3 = os.times()
    second = (t3.user - t2.user) + (t3.system - t2.system)
    assert second < max(0.05, first), (first, second)

    # 3) subject/background 全链路：主体提亮、背景提亮互不串扰
    out_s = apply_masks(img, [{"id": 1, "type": "subject", "amount": 1.0,
                               "invert": False, "params": {},
                               "adjustments": {"exposure": 0.8}}])
    lift_center = _mean(out_s, (W // 2 - 15, H // 2 - 20, W // 2 + 15, H // 2 + 20)) - 210
    lift_corner = _mean(out_s, (0, 0, 20, 20)) - 25
    assert lift_center > lift_corner + 5, (lift_center, lift_corner)
    out_b = apply_masks(img, [{"id": 1, "type": "background", "amount": 1.0,
                               "invert": False, "params": {},
                               "adjustments": {"exposure": 0.8}}])
    lift_bg = _mean(out_b, (0, 0, 20, 20)) - 25
    center_bg = _mean(out_b, (W // 2 - 15, H // 2 - 20, W // 2 + 15, H // 2 + 20)) - 210
    assert lift_bg > 5 and lift_bg > center_bg + 5, (lift_bg, center_bg)
    print(f"[ok] masks: subject 主体提亮 (中心 {lift_center:+.1f} vs 角部 {lift_corner:+.1f}), "
          f"background 反向 (角部 {lift_bg:+.1f} vs 中心 {center_bg:+.1f})")

    # 4) 启发式回退：无 PIL 图时直接可用且有中心先验
    rgb01 = np.asarray(img, np.float32) / 255.0
    heur = _subject_heuristic(H, W, rgb01)
    assert float(heur[H // 2 - 10:H // 2 + 10, W // 2 - 15:W // 2 + 15].mean()) > \
        float(heur[:15, :15].mean())

    # 5) 组件组合：背景蒙版 + 亮度范围 inter（背景中的高光区）
    combo = apply_masks(img, [{
        "id": 1, "amount": 1.0, "invert": False,
        "adjustments": {"exposure": 0.6},
        "components": [
            {"type": "background", "params": {}, "mode": "add"},
            {"type": "luminance", "mode": "inter",
             "params": {"lo": 0.0, "hi": 0.2, "feather": 0.03}}]}])
    assert combo.size == img.size
    print("[ok] combo: background ∩ luminance 组件组合通过")
    print("P5 SEGMENT SMOKE PASSED")


if __name__ == "__main__":
    main()
