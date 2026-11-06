# -*- coding: utf-8 -*-
"""test_reference_match.py — 反向拟合冒烟：用已知编辑造参考图，检验能反推回来

闭环：base --已知编辑--> ref --compute_stats--> 统计卡 --suggest_edits--> 反推参数
判据：反推值方向正确、量级在容差内；匹配距离显著低于不编辑。
"""
import sys

import numpy as np
from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
from photo_editor import apply_edits
from photo_stats import compute_stats
from reference_match import suggest_edits, verify_match

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


def make_base(w=320, h=320):
    """底图：灰渐变 60..140（含暗部供 color_grading 检验）+ 蓝色饱和区 + 暖肤色区。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 60 + (xx * 80 // w)                       # 60..140 灰渐变
    arr[..., 0] = base
    arr[..., 1] = base
    arr[..., 2] = base
    arr[0:100, 0:140] = (60, 90, 190)                # 蓝区
    arr[200:300, 180:320] = (190, 150, 120)          # 暖肤色区
    return Image.fromarray(arr)


def make_blue_base(w=320, h=320):
    """纯蓝单彩色区底图（HSL 分带反推专用）；蓝取精确 hue 240°（r=g）避免带缘 mask 衰减。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 60 + (yy * 80 // h)
    arr[..., 0] = base
    arr[..., 1] = base
    arr[..., 2] = base
    arr[0:160, 0:200] = (60, 60, 220)
    return Image.fromarray(arr)


base = make_base()
base_stats = compute_stats(base)

# 1) 曝光（线性亮度响应闭式反解，应接近精确）
known = {"exposure": 0.5}
ref = apply_edits(base, known)
sug, _ = suggest_edits(base, compute_stats(ref))
check("exposure recovered", abs(sug.get("exposure", 0) - 0.5) < 0.1, sug.get("exposure"))
check("no color params from pure exposure",
      not ({"warmth", "wb_tint", "shadows", "highlights"} & set(sug)), sorted(sug))

# 2) 曝光负方向
sug_n, _ = suggest_edits(base, compute_stats(apply_edits(base, {"exposure": -0.8})))
check("exposure neg recovered", abs(sug_n.get("exposure", 0) + 0.8) < 0.2, sug_n.get("exposure"))

# 3) 对比度
ref_c = apply_edits(base, {"contrast": 1.3})
sug_c, _ = suggest_edits(base, compute_stats(ref_c))
check("contrast recovered", abs(sug_c.get("contrast", 1.0) - 1.3) < 0.12, sug_c.get("contrast"))

# 4) 暖度（经验常数 3.3，容差放宽）
ref_w = apply_edits(base, {"warmth": 0.3})
sug_w, _ = suggest_edits(base, compute_stats(ref_w))
check("warmth recovered", abs(sug_w.get("warmth", 0) - 0.3) < 0.12, sug_w.get("warmth"))

# 5) wb_tint 方向：品红 tint（正）→ 反推应为正
ref_t = apply_edits(base, {"wb_tint": 25})
sug_t, _ = suggest_edits(base, compute_stats(ref_t))
t = sug_t.get("wb_tint", 0)
check("wb_tint sign", t > 5, t)

# 6) HSL 分带：blue ds=0.3 → 纯蓝底图上反推 blue 带残差 ≈ 0.3，其它带不误报
blue_base = make_blue_base()
ref_h = apply_edits(blue_base, {"hsl": {"blue": [0, 0.3, 0]}})
sug_h, _ = suggest_edits(blue_base, compute_stats(ref_h))
hsl = sug_h.get("hsl", {})
check("hsl blue ds≈0.3", abs(hsl.get("blue", [0, 0, 0])[1] - 0.3) < 0.1,
      {k: v[1] for k, v in hsl.items()})
check("hsl others quiet", not any(abs(v[1]) > 0.15 for k, v in hsl.items() if k != "blue"),
      {k: v[1] for k, v in hsl.items()})

# 7) color_grading：暗部偏暖 → shadows 区间被反推出来（方向检查，hue 落暖区）
ref_g = apply_edits(base, {"color_grading": {"shadows": [30, 0.45]}})
sug_g, _ = suggest_edits(base, compute_stats(ref_g))
cg = sug_g.get("color_grading", {}).get("shadows")
check("color_grading shadows emitted",
      bool(cg) and cg[1] > 0.05 and (cg[0] <= 60 or cg[0] >= 300),
      sug_g.get("color_grading"))

# 8) 组合编辑 + 匹配距离应显著下降
known_full = {"exposure": 0.4, "contrast": 1.2, "warmth": 0.25, "vibrance": 0.15}
ref_f = apply_edits(base, known_full)
sug_f, _ = suggest_edits(base, compute_stats(ref_f))
d_before = verify_match(base, compute_stats(ref_f))
matched = apply_edits(base, sug_f)
d_after = verify_match(matched, compute_stats(ref_f))
check("match improves", d_after < d_before * 0.8, (round(d_before, 3), round(d_after, 3)))

# 9) strength 缩放：0.5 强度下反推曝光约为一半
sug_s, _ = suggest_edits(base, compute_stats(apply_edits(base, {"exposure": 0.8})), strength=0.5)
check("strength scales", abs(sug_s.get("exposure", 0) - 0.4) < 0.12, sug_s.get("exposure"))

# 10) 平图退化：参考 key 超界（接近黑）不产生 NaN
flat = Image.new("RGB", (160, 160), (3, 3, 3))
sug_x, _ = suggest_edits(base, compute_stats(flat))
check("degenerate safe", isinstance(sug_x, dict))

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
