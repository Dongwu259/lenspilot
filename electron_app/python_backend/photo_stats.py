"""photo_stats.py — 照片统计卡（JitRL 记忆层与参考匹配的共享状态表示）

把一张图压缩成一组可比较的统计量：曝光 key / 对比度 / 剪裁 /
中性区白平衡色偏 / 8 色相饱和谱 / 阴影高光分色。三处共用：
  1. reference_match — 参考图与本图的差异 → 初始参数（反向拟合）
  2. edit_memory     — 情景记忆的"状态 s"与 kNN 检索特征
  3. 未来 E0-E3 回放实验（r730）——口径保持一致

8 色域定义与 photo_s/grade.py 完全对齐（red 0° … magenta 320°，
σ=14°），保证统计谱里的 "red" 就是 apply_hsl 调的那个 "red"。

只用 numpy + PIL，不依赖 cv2（任何环境都能跑）。
"""

import numpy as np
from PIL import Image

# 与 photo_s/grade.py._HSL_CENTERS 一致（键序即输出序）
HUE_BANDS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")
_HSL_CENTERS = np.array([0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 280.0, 320.0])
_HSL_SIGMA = 14.0

_MAX_SIDE = 256          # 统计不需要全分辨率，降采样保速度
_MIN_NEUTRAL_PIX = 50    # 中性区至少这么多像素才信 WB 估计
_MIN_BAND_PIX = 30       # 饱和谱至少这么多彩色像素才算数
_SAT_FLOOR = 0.08        # 低于此饱和度视为中性色，不参与色相谱

# kNN 特征定义：(特征名(点号取嵌套), 尺度)。除以尺度 → 无量纲。
FEATURE_SPECS = [
    ("key", 0.25), ("contrast", 0.25),
    ("cast_warm", 0.06), ("cast_green", 0.04), ("chroma", 0.12),
] + [("sat_profile." + b, 0.15) for b in HUE_BANDS] + [
    ("shadow_cast.0", 0.06), ("shadow_cast.1", 0.04),
    ("highlight_cast.0", 0.06), ("highlight_cast.1", 0.04),
]


def _downscale(img):
    img = img.convert("RGB")
    w, h = img.size
    m = max(w, h)
    if m > _MAX_SIDE:
        s = _MAX_SIDE / float(m)
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR)
    return img


def _rgb_to_hsv(flat):
    """flat: N×3 (0..1) → (hue_deg N, sat N, val N)。标准 HSV。"""
    r, g, b = flat[:, 0], flat[:, 1], flat[:, 2]
    vmax = flat.max(axis=1)
    vmin = flat.min(axis=1)
    delta = vmax - vmin
    s = np.where(vmax > 1e-9, delta / np.maximum(vmax, 1e-9), 0.0)
    hue = np.zeros_like(vmax)
    m = delta > 1e-9
    rm = m & (flat[:, 0] >= vmax)
    gm = m & ~rm & (flat[:, 1] >= vmax)
    bm = m & ~rm & ~gm
    d = np.maximum(delta, 1e-9)
    hue[rm] = ((g[rm] - b[rm]) / d[rm]) % 6.0
    hue[gm] = (b[gm] - r[gm]) / d[gm] + 2.0
    hue[bm] = (r[bm] - g[bm]) / d[bm] + 4.0
    return hue * 60.0, np.clip(s, 0.0, 1.0), vmax


def _zone_cast(r, g, b, luma, mask):
    """某个亮度分区内的色偏：(warm = R−B, green = G−(R+B)/2)。灰也能表示。"""
    if int(mask.sum()) < _MIN_NEUTRAL_PIX:
        return 0.0, 0.0
    rr = float(r[mask].mean())
    gg = float(g[mask].mean())
    bb = float(b[mask].mean())
    return rr - bb, gg - (rr + bb) / 2.0


def compute_stats(img):
    """PIL Image → 统计卡 dict。纯函数，不修改传入图像。"""
    img = _downscale(img)
    arr = np.asarray(img, dtype=np.float64) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    rf, gf, bf = r.reshape(-1), g.reshape(-1), b.reshape(-1)
    lf = luma.reshape(-1)

    key = float(np.median(lf))
    p01, p99 = np.percentile(lf, 1.0), np.percentile(lf, 99.0)
    stats = {
        "key": key,
        "mean": float(lf.mean()),
        "contrast": float(p99 - p01),
        "clip_hi": float((lf > 0.98).mean()),
        "clip_lo": float((lf < 0.02).mean()),
    }

    # ── HSV（在像素平面上做一次，供中性区 / 色相谱共用） ──
    flat = arr.reshape(-1, 3)
    hue_deg, sat, val = _rgb_to_hsv(flat)

    # ── 白平衡：中位色差（抗彩色区域污染；暖化后的灰不再被阈值踢出） ──
    win = (lf > 0.15) & (lf < 0.95)
    if int(win.sum()) >= _MIN_NEUTRAL_PIX:
        mr, mg, mb = float(np.median(rf[win])), float(np.median(gf[win])), float(np.median(bf[win]))
    else:
        mr, mg, mb = float(np.median(rf)), float(np.median(gf)), float(np.median(bf))
    stats["cast_warm"] = mr - mb                        # 正 = 偏暖，负 = 偏冷
    stats["cast_green"] = mg - (mr + mb) / 2.0          # 正 = 偏绿，负 = 偏品红

    # ── 8 色相饱和谱：高斯软分带 → 逐像素分区归一（各带质量和为 1，
    #    纯色不会均摊到邻带），再按彩色像素数平均 ──
    colorful = sat > _SAT_FLOOR
    profile = {}
    if int(colorful.sum()) >= _MIN_BAND_PIX:
        hd, sd = hue_deg[colorful], sat[colorful]
        d = (hd[:, None] - _HSL_CENTERS[None, :] + 180.0) % 360.0 - 180.0
        wgt = np.exp(-(d ** 2) / (2.0 * _HSL_SIGMA ** 2))
        p = wgt / np.maximum(wgt.sum(axis=1, keepdims=True), 1e-6)
        prof = (p * sd[:, None]).sum(axis=0) / float(len(sd))
        profile = {band: float(prof[i]) for i, band in enumerate(HUE_BANDS)}
    else:
        profile = {band: 0.0 for band in HUE_BANDS}
    stats["sat_profile"] = profile
    stats["chroma"] = float((sat * val).mean())    # 感知彩量（sat×亮度）

    # ── 阴影 / 高光分色（RGB 域，sat=0 的灰也能带色调）＋分区像素占比 ──
    shadow_mask = lf < 0.33
    hl_mask = lf > 0.66
    stats["shadow_frac"] = float(shadow_mask.mean())
    stats["highlight_frac"] = float(hl_mask.mean())
    stats["shadow_cast"] = list(_zone_cast(rf, gf, bf, lf, shadow_mask))
    stats["highlight_cast"] = list(_zone_cast(rf, gf, bf, lf, hl_mask))
    return stats


def feature_vector(stats):
    """统计卡 → 定长 kNN 特征（已按 FEATURE_SPECS 尺度归一）。"""
    vec = []
    for name, scale in FEATURE_SPECS:
        v = stats
        for part in name.split("."):
            if isinstance(v, (list, tuple)):
                v = v[int(part)]
            else:
                v = v[part]
        vec.append(float(v) / scale)
    return vec


def stats_distance(a, b):
    """两张统计卡的无量纲欧氏距离（1.0 ≈ 累计差了一个典型单位）。"""
    va, vb = feature_vector(a), feature_vector(b)
    return float(np.sqrt(sum((x - y) ** 2 for x, y in zip(va, vb))))
