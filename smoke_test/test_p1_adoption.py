# -*- coding: utf-8 -*-
"""test_p1_adoption.py — 软件侧 P1 三件套冒烟

P1-1 AI 建议卡：_dedupe_variants 变体去重组装（记忆融合/原始AI/纯个人风格）
P1-2 采纳率仪表盘：stats_summary 口径与 settle 实时判定一致（北极星指标）
P1-3 参考匹配 v2：handle 响应带 before/after 匹配距离，建议参数显著拉近
全部临时目录打桩，不污染真实记忆库。
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
import edit_memory as em
import reference_match as rm
from photo_editor import _dedupe_variants, apply_edits

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# ── P1-2 采纳率：临时记忆库打桩 ──
tmp = tempfile.mkdtemp(prefix="p1_adoption_")
em._PATH = os.path.join(tmp, "edit_memory.jsonl")


def stats_for(key):
    prof = {b: 0.0 for b in em.HUE_BANDS}
    return {"key": key, "mean": key, "contrast": 0.5, "clip_hi": 0.0, "clip_lo": 0.0,
            "cast_warm": 0.1, "cast_green": 0.0, "chroma": 0.1, "sat_profile": prof,
            "shadow_cast": [0.05, 0.0], "highlight_cast": [0.05, 0.0]}


SUG = {"exposure": 0.5, "contrast": 1.2, "warmth": 0.3, "shadows": 0.2, "highlights": 0.2}
FAR = {"exposure": -1.5, "contrast": 0.4, "warmth": -1.0, "shadows": -1.0, "highlights": -1.0}

em.suggest(r"C:\p1\a.jpg", stats_for(0.4), SUG)
ok1, acc1 = em.settle(r"C:\p1\a.jpg", dict(SUG))
check("settle identical accepted", ok1 and acc1)
_, acc2 = em.settle(r"C:\p1\a.jpg", dict(FAR))
check("settle far not accepted", not acc2)
em.settle(r"C:\p1\nosug.jpg", dict(SUG))  # 无建议配对 → 不计落定配对

st = em.handle_memory_stats({})
check("stats suggestions=1", st.get("suggestions") == 1, st)
check("stats settled=2", st.get("settled") == 2, st.get("settled"))
check("stats accepted=1", st.get("accepted") == 1, st.get("accepted"))
check("stats rate=0.5", st.get("adoption_rate") == 0.5, st.get("adoption_rate"))
check("stats kinds", st["kinds"].get("suggested") == 1 and st["kinds"].get("edit") == 3)

# 空库：rate=None（UI 显示 —，不算除零）
em._PATH = os.path.join(tmp, "empty.jsonl")
st0 = em.handle_memory_stats({})
check("stats empty rate None", st0.get("adoption_rate") is None and st0.get("count") == 0)
em._PATH = os.path.join(tmp, "edit_memory.jsonl")

# ── P1-1 建议变体去重 ──
v = _dedupe_variants([("memory", {"exposure": 0.4}), ("prior", {"exposure": 0.1}),
                      ("personal", {"exposure": -0.2})])
check("variants 3 kept order", v and len(v) == 3 and
      [x["label"] for x in v] == ["memory", "prior", "personal"])
check("variants first=applied", v[0]["options"] == {"exposure": 0.4})
v2 = _dedupe_variants([("memory", {"exposure": 0.4}), ("prior", {"exposure": 0.4}),
                       ("personal", None)])
check("variants all-dup -> None", v2 is None)
v3 = _dedupe_variants([("memory", {"a": 1}), ("prior", {"a": 1}), ("personal", {"a": 2})])
check("variants partial dedup", v3 and len(v3) == 2 and
      [x["label"] for x in v3] == ["memory", "personal"])
v4 = _dedupe_variants([("memory", None), ("prior", {})])
check("variants empty -> None", v4 is None)

# ── P1-3 参考匹配 v2：响应带匹配距离 ──


def make_base(w=320, h=320):
    """灰渐变 + 蓝饱和区 + 暖肤色区（与 test_reference_match 同构）。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    xx = np.arange(w)[None, :].repeat(h, 0)
    base = 60 + (xx * 80 // w)
    arr[..., 0] = base
    arr[..., 1] = base
    arr[..., 2] = base
    arr[0:100, 0:140] = (60, 90, 190)
    arr[200:300, 180:320] = (190, 150, 120)
    return Image.fromarray(arr)


base_img = make_base()
p_base = os.path.join(tmp, "base.jpg")
p_ref = os.path.join(tmp, "ref.jpg")     # 提亮 0.6EV 的参考
p_same = os.path.join(tmp, "same.jpg")   # 与本图相同 → 无需调整
base_img.save(p_base, quality=95)
apply_edits(base_img, {"exposure": 0.6}).save(p_ref, quality=95)
base_img.save(p_same, quality=95)

r = rm.handle_reference_suggest_params(
    {"path": p_base, "reference_path": p_ref, "strength": 1.0})
check("refmatch ok", bool(r.get("ok")), r.get("error", ""))
check("refmatch exposure suggested", abs(r["edits"].get("exposure", 0) - 0.6) < 0.15,
      r["edits"].get("exposure"))
d = r.get("distance") or {}
check("refmatch distance present", d.get("before") is not None and d.get("after") is not None, d)
check("refmatch distance improves", d.get("after", 9) < d.get("before", 0), d)
check("refmatch stats cards returned",
      "key" in (r.get("my_stats") or {}) and "key" in (r.get("reference_stats") or {}))

r2 = rm.handle_reference_suggest_params(
    {"path": p_base, "reference_path": p_same, "strength": 1.0})
check("refmatch same -> no edits", not (r2.get("edits") or {}), r2.get("edits"))
check("refmatch same -> after None", r2["distance"]["after"] is None, r2.get("distance"))

# reference_stats 直传路径不受影响
r3 = rm.handle_reference_suggest_params(
    {"path": p_base, "reference_stats": r["reference_stats"], "strength": 0.5})
check("refmatch stats-direct works", bool(r3.get("ok")) and r3.get("distance", {}).get("before") is not None)

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
