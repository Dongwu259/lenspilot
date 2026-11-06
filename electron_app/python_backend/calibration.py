"""calibration.py — 相机校准（LR Calibration 面板近似，P6-3）

calibration 协议（photo_editor.DEFAULT_EDITS["calibration"]，None = 关）：
  {"shadows_tint": -100..100,  # 暗部色调：负=绿 正=品红
   "r_hue": -100..100, "r_sat": -100..100,   # 红原色 色相/饱和
   "g_hue": -100..100, "g_sat": -100..100,   # 绿原色
   "b_hue": -100..100, "b_sat": -100..100}   # 蓝原色

实现（近似口径，LR 真身是线性原色矩阵）：
  原色（R0°/G120°/B240°，高斯窗 σ=45° 比 HSL 宽——"全局原色"语义）：
    色相 hue += hue_deg·w（±15°）；饱和 sat ×= (1+sat_scale·w) 乘法缩放
    （与 HSL 的加法有本质区别，灰色高饱和无意义的像素不受影响）。
  暗部色调在 RGB 域直接加减通道（HSV 色相旋转对 sat=0 的灰部无效）：
    负=绿（+G−R−B），正=品红（+R+B−G），按 luma 阴影 mask 加权。
应用位置：白平衡之后、灰阶/曲线之前（原色先定，再做影调）。
"""

import numpy as np

from photo_s.grade import _from_hsv, _hue_distance_deg, _to_hsv

_SIGMA = 45.0
_HUE_SCALE = 15.0 / 100.0   # ±100 → ±15°
_SAT_SCALE = 0.5 / 100.0    # ±100 → ±0.5 倍乘
_PRIMARIES = (("r", 0.0), ("g", 120.0), ("b", 240.0))


def apply_calibration(img, cal):
    if not cal or not isinstance(cal, dict):
        return img
    try:
        arr = np.asarray(img.convert("RGB"), np.float32) / 255.0
        touched = False

        # ── 原色 色相/饱和（HSV 主色加权）──
        hue, sat, val = _to_hsv(arr)
        hue_deg = hue * 360.0
        hue_shift = np.zeros_like(hue)
        sat_mult = np.ones_like(sat)
        for key_prefix, center in _PRIMARIES:
            h100 = float(cal.get(f"{key_prefix}_hue", 0) or 0)
            s100 = float(cal.get(f"{key_prefix}_sat", 0) or 0)
            if abs(h100) < 0.5 and abs(s100) < 0.5:
                continue
            touched = True
            dist = np.abs(_hue_distance_deg(hue_deg, center))
            w = np.exp(-(dist ** 2) / (2.0 * _SIGMA ** 2))
            hue_shift += (h100 * _HUE_SCALE) * w
            sat_mult *= (1.0 + s100 * _SAT_SCALE * w)
        if touched:
            hue = np.mod(hue + hue_shift / 360.0, 1.0)
            sat = np.clip(sat * np.clip(sat_mult, 0.2, 3.0), 0.0, 1.0)
            arr = _from_hsv(hue, sat, val)

        # ── 暗部色调（RGB 域直接加色差，灰部同样生效）──
        t = float(cal.get("shadows_tint", 0) or 0)
        if abs(t) >= 0.5:
            luma = (0.299 * arr[..., 0] + 0.587 * arr[..., 1]
                    + 0.114 * arr[..., 2])
            shadow = np.clip(1.0 - luma * 2.2, 0.0, 1.0)[..., None]
            amt = (abs(t) / 100.0) * 0.25  # 最大通道偏移
            if t < 0:    # 绿：+G −(R+B)/2
                delta = np.stack([-0.5 * amt + np.zeros_like(arr[..., 0]),
                                  np.full_like(arr[..., 0], amt),
                                  -0.5 * amt + np.zeros_like(arr[..., 0])], -1)
            else:        # 品红：+(R+B)/2 −G
                delta = np.stack([np.full_like(arr[..., 0], amt),
                                  -amt + np.zeros_like(arr[..., 0]),
                                  np.full_like(arr[..., 0], amt)], -1)
            arr = np.clip(arr + delta * shadow, 0.0, 1.0)
            touched = True

        if not touched:
            return img
        from PIL import Image as _I
        return _I.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8))
    except Exception:
        return img
