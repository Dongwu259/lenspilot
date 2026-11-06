"""optics_fix.py — 镜头校正（P4-3）：手动畸变 / 倍率色差 / 暗角补偿

optics 协议（photo_editor.DEFAULT_EDITS["optics"]，None = 关）：
  {"distortion": -100..100,   # 负=收桶形 正=收枕形（0.25 系数封顶，宁欠勿过）
   "ca":        -100..100,   # 倍率色差：R/B 通道沿半径反向缩放
   "vig":       -100..100}   # 暗角补偿：亮度 × (1 + vig/100 · r²)

需 opencv（remap）；缺失（Mac 构建无 opencv，与 HANDOVER 回退口径一致）
时静默跳过。应用位置：几何段之后、一切影调之前（光学缺陷先修）。
DNG 内嵌镜头档案（OpcodeList）不在 v1 范围。
"""

import numpy as np


def apply_optics(img, optics):
    if not optics or not isinstance(optics, dict):
        return img
    d = float(optics.get("distortion", 0) or 0)
    ca = float(optics.get("ca", 0) or 0)
    vg = float(optics.get("vig", 0) or 0)
    if abs(d) < 0.5 and abs(ca) < 0.5 and abs(vg) < 0.5:
        return img
    try:
        import cv2
    except ImportError:
        return img  # 无 opencv → 跳过（Mac 回退口径）

    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    nx = (np.arange(w, dtype=np.float32) - cx) / (w / 2.0)
    ny = (np.arange(h, dtype=np.float32) - cy) / (h / 2.0)
    X, Y = np.meshgrid(nx, ny)
    R2 = X * X + Y * Y

    changed = False
    # 1) 畸变（逆向映射：src = dst · (1 + k·r²)），与 CA 合成一次 remap
    k = max(-0.25, min(0.25, d / 100.0 * 0.25))
    ca_amt = max(-0.02, min(0.02, ca / 100.0 * 0.01))
    if abs(k) > 1e-4 or abs(ca_amt) > 1e-4:
        factor = 1.0 + k * R2
        map_x = (cx + X * factor * (w / 2.0)).astype(np.float32)
        map_y = (cy + Y * factor * (h / 2.0)).astype(np.float32)
        if abs(ca_amt) > 1e-4:
            # 色差：R/B 通道沿半径相对 G 反向缩放（分子像素级采样）
            out = np.empty_like(arr)
            for ch, scale in ((0, 1.0 + ca_amt), (1, 1.0), (2, 1.0 - ca_amt)):
                f = 1.0 + (scale - 1.0)
                mx = (cx + X * (factor * f) * (w / 2.0)).astype(np.float32)
                my = (cy + Y * (factor * f) * (h / 2.0)).astype(np.float32)
                out[..., ch] = cv2.remap(arr[..., ch], mx, my, cv2.INTER_LINEAR)
            arr = out
        else:
            arr = cv2.remap(arr, map_x, map_y, cv2.INTER_LINEAR)
        changed = True

    # 2) 暗角补偿（r² 衰减模型，中心不变角部增强/减弱）
    if abs(vg) > 0.5:
        gain = 1.0 + (vg / 100.0) * np.clip(R2, 0.0, 4.0)
        arr = np.clip(arr.astype(np.float32) * gain[..., None], 0, 255).astype(np.uint8)
        changed = True

    if not changed:
        return img
    from PIL import Image
    return Image.fromarray(arr)
