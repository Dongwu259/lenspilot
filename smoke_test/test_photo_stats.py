# -*- coding: utf-8 -*-
"""test_photo_stats.py — 统计卡冒烟：合成图上验证各统计量的方向与量级"""
import sys
import numpy as np
from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
from photo_stats import FEATURE_SPECS, HUE_BANDS, compute_stats, feature_vector, stats_distance

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


def solid(rgb, size=(160, 160)):
    return Image.new("RGB", size, rgb)


# 1) 中性灰：key≈0.5、色偏≈0、谱≈0
g = compute_stats(solid((128, 128, 128)))
check("gray key≈0.5", abs(g["key"] - 0.502) < 0.02, g["key"])
check("gray cast_warm≈0", abs(g["cast_warm"]) < 0.01, g["cast_warm"])
check("gray cast_green≈0", abs(g["cast_green"]) < 0.01, g["cast_green"])
check("gray contrast≈0", g["contrast"] < 0.02, g["contrast"])
check("gray profile≈0", max(g["sat_profile"].values()) < 0.03)

# 2) 暖色偏：R 抬 B 压 → cast_warm 显著为正
w = compute_stats(solid((143, 128, 113)))
check("warm cast_warm>0.1", w["cast_warm"] > 0.1, w["cast_warm"])
check("warm key≈gray key", abs(w["key"] - g["key"]) < 0.05, w["key"])

# 3) 高饱和红：red 带谱值全场最高
r = compute_stats(solid((200, 40, 40)))
prof = r["sat_profile"]
check("red band top", max(prof, key=prof.get) == "red", {k: round(v, 2) for k, v in prof.items()})
check("red chroma high", r["chroma"] > 0.2, r["chroma"])

# 4) 高饱和蓝：blue 带 top 且 aqua/purple 有外溢（σ=14° 的邻带衰减）
bl = compute_stats(solid((40, 60, 200)))
prof_b = bl["sat_profile"]
check("blue band top", max(prof_b, key=prof_b.get) == "blue")

# 5) 剪裁 / 对比度
img = Image.new("RGB", (160, 160), (128, 128, 128))
arr = np.asarray(img).copy()
arr[:40] = 255      # 死白
arr[40:80] = 0      # 死黑
clip = compute_stats(Image.fromarray(arr))
check("clip_hi≈0.25", abs(clip["clip_hi"] - 0.25) < 0.02, clip["clip_hi"])
check("clip_lo≈0.25", abs(clip["clip_lo"] - 0.25) < 0.02, clip["clip_lo"])
check("clip contrast>0.9", clip["contrast"] > 0.9, clip["contrast"])

# 6) 阴影/高光分色：暗部偏暖、亮部偏冷
arr2 = np.zeros((160, 160, 3), dtype=np.uint8)
arr2[:80] = (80, 65, 50)     # 暗部暖 (luma≈0.27)
arr2[80:] = (172, 187, 202)  # 亮部冷 (luma≈0.72)
split = compute_stats(Image.fromarray(arr2))
check("shadow warm>0", split["shadow_cast"][0] > 0.1, split["shadow_cast"])
check("highlight cool<0", split["highlight_cast"][0] < 0, split["highlight_cast"])

# 7) 特征向量与距离
check("feature len", len(feature_vector(g)) == len(FEATURE_SPECS), len(FEATURE_SPECS))
d_near = stats_distance(g, w)
d_far = stats_distance(g, r)
check("dist near<far", d_near < d_far, (round(d_near, 3), round(d_far, 3)))
check("dist symmetric", abs(stats_distance(g, r) - stats_distance(r, g)) < 1e-9)
check("bands count", len(HUE_BANDS) == 8)

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
