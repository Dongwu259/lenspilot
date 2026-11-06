# -*- coding: utf-8 -*-
"""test_edit_memory.py — JitRL 记忆层冒烟：存储/检索/加权中位数/优势重排/回退

两个风格簇：A 暖暗（室内人像味）、B 冷亮（通透风光味）。
检验：检索能分簇、中位数复原簇参数、重排偏向簇内候选、经验不足回退。
用临时 _PATH 打桩，绝不污染真实记忆库。
"""
import json
import sys
import tempfile

import numpy as np

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
import edit_memory as em

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# ── 打桩：临时存储 ──
tmp = tempfile.mkdtemp(prefix="edit_memory_test_")
em._PATH = em.os.path.join(tmp, "edit_memory.jsonl")


def stats_for(key, contrast, warm, sat_red):
    """手工构造统计卡（跳过图像，直接给簇特征）。"""
    prof = {b: 0.0 for b in em.HUE_BANDS}
    prof["red"] = sat_red
    return {
        "key": key, "mean": key, "contrast": contrast,
        "clip_hi": 0.0, "clip_lo": 0.0,
        "cast_warm": warm, "cast_green": 0.0, "chroma": 0.1,
        "sat_profile": prof,
        "shadow_cast": [warm / 2, 0.0], "highlight_cast": [warm / 2, 0.0],
    }


PARAMS_A = {"exposure": 0.4, "contrast": 1.15, "warmth": 0.3, "shadows": 0.2,
            "vibrance": 0.1, "hsl": {"orange": [0, 0.15, 0]}}
PARAMS_B = {"exposure": -0.3, "contrast": 1.3, "warmth": -0.25, "highlights": -0.2,
            "wb_temp": 5800}

# 簇 A 6 条 + 簇 B 6 条
for i in range(6):
    em.record("edit", stats_for(0.32 + 0.02 * (i % 3), 0.45, 0.14, 0.35),
              PARAMS_A, weight=1.0)
for i in range(6):
    em.record("edit", stats_for(0.62 + 0.02 * (i % 3), 0.75, -0.12, 0.15),
              PARAMS_B, weight=1.0)

# 1) 存储：条数 / kind 分布 / 轮转路径打桩生效
st = em.handle_memory_status({})
check("status count=12", st["count"] == 12, st["count"])
check("status kinds", st["kinds"].get("edit") == 12)

# 2) 检索分簇：A 簇统计 → 中位数复现 A 参数（k=6 正好取满 A 簇 6 条）
med, conf = em.memory_median(stats_for(0.33, 0.46, 0.15, 0.34), k=6)
check("median not fallback", med is not None, conf)
check("median exposure≈0.4", abs(med.get("exposure", 0) - 0.4) < 0.05, med.get("exposure"))
check("median warmth≈0.3", abs(med.get("warmth", 0) - 0.3) < 0.05, med.get("warmth"))
check("median hsl orange", abs(med.get("hsl", {}).get("orange", [0, 0, 0])[1] - 0.15) < 0.05,
      med.get("hsl"))
check("median confidence high", conf > 0.6, conf)

# 3) B 簇统计 → B 参数（含 wb_temp）
med_b, conf_b = em.memory_median(stats_for(0.63, 0.74, -0.11, 0.16))
check("median B exposure≈-0.3", abs(med_b.get("exposure", 0) + 0.3) < 0.05, med_b.get("exposure"))
check("median B wb_temp≈5800", abs(med_b.get("wb_temp", 0) - 5800) < 120, med_b.get("wb_temp"))

# 4) 优势重排：A 簇查询下，簇内候选排在簇外候选前
q = stats_for(0.33, 0.46, 0.15, 0.34)
rr = em.rerank([PARAMS_B, PARAMS_A], q, beta=1.0)
check("rerank not fallback", not rr["fallback"], rr["confidence"])
check("rerank A first", rr["ranked"][0]["params"] is PARAMS_A,
      [(r["params"] is PARAMS_A, r["score"]) for r in rr["ranked"]])

# 5) fallback：全新存储（条目不足）→ fallback=True、ranked 原样返回
em._PATH = em.os.path.join(tmp, "empty.jsonl")
rr2 = em.rerank([PARAMS_A], q)
check("fallback on empty", rr2["fallback"] is True and len(rr2["ranked"]) == 1)
med2, conf2 = em.memory_median(q)
check("median fallback on empty", med2 is None and conf2 == 0.0)

# 6) reject 条目不参与检索
em.record("reject", stats_for(0.33, 0.46, 0.15, 0.34), PARAMS_B)
med3, _ = em.memory_median(q)
check("reject ignored", med3 is None)

# 7) _get_param 语义：wb_temp 0 → 5500 中性
check("wb_temp off→5500", em._get_param({"wb_temp": 0}, "wb_temp") == 5500.0)
check("hsl ds path", em._get_param({"hsl": {"red": [0, 0.4, 0]}}, "hsl.red.1") == 0.4)

# 8) 命令层：record→rerank 走 IPC 句柄
em._PATH = em.os.path.join(tmp, "ipc.jsonl")
for i in range(6):
    em.handle_memory_record({"kind": "edit", "stats": stats_for(0.5, 0.5, 0.1, 0.2),
                             "params": {"exposure": 0.2}})
rr4 = em.handle_memory_rerank({"stats": stats_for(0.5, 0.5, 0.1, 0.2),
                               "candidates": [{"exposure": -0.5}, {"exposure": 0.25}],
                               "beta": 1.0})
check("ipc rerank first=+0.25", abs(rr4["ranked"][0]["params"]["exposure"] - 0.25) < 1e-9,
      [r["params"]["exposure"] for r in rr4["ranked"]])
check("ipc ranked score desc",
      rr4["ranked"][0]["score"] >= rr4["ranked"][1]["score"])

# 9) 坏行容差：半行 JSON 被跳过
with open(em._PATH, "a", encoding="utf-8") as f:
    f.write('{"kind": "edit", "stats": {"key": 0.5')  # 故意不闭合
check("torn line tolerated", len(em.load()) == 6)

# 10) batch 字段 + 同批折扣（E3: 同批距离 ×0.5, 直接测 _neighbors 翻转）
em._PATH = em.os.path.join(tmp, "batch.jsonl")

def perturb(base, k):
    s = json.loads(json.dumps(base))
    s["key"] += 0.004 * k
    s["contrast"] += 0.004 * k
    s["cast_warm"] += 0.001 * k
    s["sat_profile"]["red"] += 0.002 * k
    return s

qA = stats_for(0.33, 0.46, 0.15, 0.34)
eA = {"kind": "edit", "stats": perturb(qA, 2), "params": {"exposure": -0.3},
      "batch": "D:/shootA", "weight": 1.0}
eB = {"kind": "edit", "stats": perturb(qA, 1), "params": {"exposure": 0.4},
      "batch": "D:/shootB", "weight": 1.0}
nb = em._neighbors(qA, [eA, eB], k=2)
check("no-batch: closer B first", nb[0][0] is eB,
      [(x[0] is eA, round(x[1], 3)) for x in nb])
nb2 = em._neighbors(qA, [eA, eB], k=2, batch="D:/shootA")
check("batch discount flips to same-batch A", nb2[0][0] is eA,
      [(x[0] is eA, round(x[1], 3)) for x in nb2])

# 11) suggest → settle 闭环（数据飞轮）
em._PATH = em.os.path.join(tmp, "flywheel.jsonl")
qA = stats_for(0.33, 0.46, 0.15, 0.34)
em.suggest("D:/shootA/02.arw", qA, {"exposure": 0.4, "contrast": 1.2})
ok, accepted = em.settle("D:/shootA/02.arw", {"exposure": 0.42, "contrast": 1.2})
check("settle ok+accepted", ok and accepted, (ok, accepted))
ok2, accepted2 = em.settle("D:/shootA/03.arw", {"exposure": -0.8})  # 无建议 → 落定为手动经验
check("settle without suggestion", ok2 and accepted2 is False)
kinds = em.handle_memory_status({})["kinds"]
check("flywheel kinds", kinds.get("suggested") == 1 and kinds.get("edit") == 2, kinds)

# 12) blend_with_prior: 记忆一致时覆盖先验, 记忆不足回退先验
em._PATH = em.os.path.join(tmp, "blend.jsonl")
for i in range(6):
    em.record("edit", stats_for(0.33, 0.46, 0.15, 0.34), PARAMS_A)
bl = em.blend_with_prior({"exposure": -0.5, "clarity": 0.3},
                         stats_for(0.33, 0.46, 0.15, 0.34),
                         batch="D:/none", beta=0.5)
check("blend adopts memory", bl["used_memory"] and abs(bl["params"].get("exposure", 0) - 0.4) < 0.05,
      bl)
check("blend preserves prior extra keys", bl["params"].get("clarity") == 0.3,
      sorted(bl["params"]))
em._PATH = em.os.path.join(tmp, "blend_empty.jsonl")
bl2 = em.blend_with_prior({"exposure": -0.5}, qA)
check("blend falls back", bl2["fallback"] and bl2["params"]["exposure"] == -0.5)

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
