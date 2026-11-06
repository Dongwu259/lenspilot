# -*- coding: utf-8 -*-
"""test_p2_memory_mgmt.py — 软件侧 P2 冒烟

P2-4 stats 复用：save_edits 有可复用建议快照时不再二次解码（mtime 陈旧才重算）
P2-5 批量轻量落定：batch_apply_edits 逐张 settle（entries 预读复用，无 stats 审计链完整）
P2-6 记忆层管理：enabled 总开关 / latest_suggested / clear 全部与按目录
全部临时目录打桩，不污染真实记忆库与 app_config。
"""
import json
import os
import sys
import tempfile
import time

from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
import edit_memory as em
import photo_editor as pe
import service

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


tmp = tempfile.mkdtemp(prefix="p2_mem_")
em._PATH = os.path.join(tmp, "edit_memory.jsonl")


def stats_for(key):
    prof = {b: 0.0 for b in em.HUE_BANDS}
    return {"key": key, "mean": key, "contrast": 0.5, "clip_hi": 0.0, "clip_lo": 0.0,
            "cast_warm": 0.1, "cast_green": 0.0, "chroma": 0.1, "sat_profile": prof,
            "shadow_cast": [0.05, 0.0], "highlight_cast": [0.05, 0.0]}


# ── P2-6 latest_suggested / _suggested_stats_reusable ──
p1 = os.path.join(tmp, "a.jpg")
Image.new("RGB", (64, 64), (120, 120, 130)).save(p1)
check("latest_suggested none", em.latest_suggested(p1) is None)
em.suggest(p1, stats_for(0.4), {"exposure": 0.3})
sug = em.latest_suggested(p1)
check("latest_suggested found", sug is not None and sug["kind"] == "suggested")
check("reusable fresh", pe._suggested_stats_reusable(p1, sug))
stale = dict(sug, ts=int(time.time()) - 3600)
os.utime(p1)  # mtime=now > 陈旧 ts
check("reusable stale mtime", not pe._suggested_stats_reusable(p1, stale))
check("reusable no stats", not pe._suggested_stats_reusable(p1, {"ts": time.time()}))
check("reusable no sug", not pe._suggested_stats_reusable(p1, None))

# ── P2-4 save_edits 快照复用：有可复用快照 → 不再加载图像 ──
loads = {"n": 0}
_orig_load = pe._load_image_rgb
pe._load_image_rgb = lambda p: (loads.__setitem__("n", loads["n"] + 1) or _orig_load(p))
try:
    r = pe.handle_save_edits({"path": p1, "edits": {"exposure": 0.31}})
    check("save ok", r.get("ok") is True, r)
    check("P2-4 skips image load", loads["n"] == 0, loads["n"])
    # 快照陈旧（建议早于文件替换）→ 重算路径触发
    entries = em.load()
    for e in entries:
        if e.get("kind") == "suggested":
            e["ts"] = int(time.time()) - 3600
    with open(em._PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    r2 = pe.handle_save_edits({"path": p1, "edits": {"exposure": 0.2}})
    check("save ok (stale)", r2.get("ok") is True, r2)
    check("P2-4 stale recomputes", loads["n"] >= 1, loads["n"])
finally:
    pe._load_image_rgb = _orig_load
# 复用快照落定的经验带建议时统计（stats 来自快照, 非现场计算）
last_edit = [e for e in em.load() if e.get("kind") == "edit"][-1]
check("settled uses snapshot stats", last_edit.get("stats") is not None
      and "key" in last_edit["stats"])

# ── P2-6 总开关：关 = save/auto_tone 两处接线旁路 ──
cfg_backup = dict(service._app_config)
try:
    service._app_config["edit_memory_enabled"] = False
    check("enabled() off", em.enabled() is False)
    before = len(em.load())
    r3 = pe.handle_save_edits({"path": p1, "edits": {"exposure": 0.1}})
    check("save ok (off)", r3.get("ok") is True)
    check("P2-6 off: no settle", len(em.load()) == before, len(em.load()) - before)
    del service._app_config["edit_memory_enabled"]
    check("enabled() default on", em.enabled() is True)
finally:
    service._app_config.clear()
    service._app_config.update(cfg_backup)

# ── P2-5 批量轻量落定 ──
em.clear()
d_x = os.path.join(tmp, "dirX")
d_y = os.path.join(tmp, "dirY")
os.makedirs(d_x, exist_ok=True)
os.makedirs(d_y, exist_ok=True)
px = os.path.join(d_x, "x.jpg")       # 有建议 → 批量落定配对
py = os.path.join(d_y, "y.jpg")       # 无建议 → 仅审计（无 stats）
Image.new("RGB", (64, 64), (100, 110, 120)).save(px)
Image.new("RGB", (64, 64), (140, 120, 100)).save(py)
em.suggest(px, stats_for(0.4), {"exposure": 0.4})

r = pe.handle_batch_apply_edits({"paths": [px, py], "edits": {"exposure": 0.4}})
check("batch ok", r.get("ok") is True and r.get("processed") == 2, r)
after = em.load()
edits_rec = [e for e in after if e.get("kind") == "edit"]
check("batch settled both", len(edits_rec) == 2, len(edits_rec))
paired = [e for e in edits_rec if e.get("path") == px][0]
audit = [e for e in edits_rec if e.get("path") == py][0]
check("batch pairs suggestion", paired.get("suggested") == {"exposure": 0.4})
check("batch paired has stats(from snapshot)", paired.get("stats") is not None)
check("batch audit no stats", audit.get("stats") is None)
# 无 stats 条目检索自动跳过（kNN 只用带 stats 的）
nbrs = em._neighbors(stats_for(0.4), em.load(), k=12)
check("retrieval skips statless", all(e.get("stats") is not None for e, _ in nbrs))

# ── P2-6 clear：按目录 / 全部 ──
check("clear dir keeps other", em.clear(batch=d_x) == 2)  # x 的 suggested+edit
left = em.load()
check("only dirY left", all(e.get("batch") != d_x for e in left) and len(left) == 1, len(left))
check("clear all", em.clear() == len(left))
check("empty after clear", em.load() == [])
check("status after clear", em.handle_memory_status({})["count"] == 0)

# ── P3-10 memory_status 单照片摘要（历史面板持久记录区数据源）──
em.suggest(p1, stats_for(0.4), {"exposure": 0.1})
em.settle(p1, {"exposure": 0.2})
em.suggest(os.path.join(tmp, "other.jpg"), stats_for(0.5), {"exposure": 0.0})
st_path = em.handle_memory_status({"path": p1})
check("status path summary", [e["kind"] for e in st_path.get("entries", [])]
      == ["edit", "suggested"], st_path.get("entries"))
check("status path excludes others", all(
    e["kind"] != "suggested" or len(st_path["entries"]) == 2 for e in st_path["entries"]))
check("status no path = no entries key", "entries" not in em.handle_memory_status({}))

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
