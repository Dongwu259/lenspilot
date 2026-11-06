#!/usr/bin/env python
"""
数字员工 M1 综合诊断+修复脚本
用法: python smoke_test/run_diag.py

测试项目:
  1. HEIC PIL.open 是否挂死(超时检测)
  2. 评分模型 + 缩略图生成
  3. 完整 pipeline 端到端(自动清缓存+注入API key)
  4. 每步耗时分析
"""
import os, sys, time, json, threading, signal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "electron_app", "python_backend"))

FOLDER = r"D:\photoDatebase\4-2春游"
KEY_FILE = os.path.join(HERE, "smoke_agent_key.txt")

# ── timeout helper (Windows compatible) ──
def with_timeout(fn, seconds):
    result = [None]; exc = [None]; ok = [True]
    def _run():
        try: result[0] = fn()
        except Exception as e: exc[0] = e
    t = threading.Thread(target=_run); t.start(); t.join(seconds)
    if t.is_alive(): return 'HUNG'
    if exc[0]: raise exc[0]
    return result[0]

# ── load API key ──
def load_key():
    lines = []
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            lines.append(line)
    return {"base_url": lines[0], "api_key": lines[1], "model": lines[2]}

def setup_api():
    import agent_module, service
    key = load_key()
    agent_module._official_config = {"base_url": key["base_url"], "api_key": key["api_key"], "model": key["model"]}
    service._app_config["agent"] = key
    return key

# ═══════════════════════════════════════════════════════════════

print("=" * 50)
print("LensPilot M1 综合诊断")
print("=" * 50)

# ── Test 1: HEIC sanity ──
print("\n[1/5] HEIC PIL.open 超时检测...")
import pillow_heif; pillow_heif.register_heif_opener()
from PIL import Image as PILImage
files = [f for f in os.listdir(FOLDER) if os.path.splitext(f)[1].lower() == '.heic']
sample = os.path.join(FOLDER, files[0]) if files else None
t0 = time.time()
r = with_timeout(lambda: PILImage.open(sample), 10)
dt = time.time() - t0
print(f"  {os.path.basename(sample)}: {r if r!='HUNG' else 'HUNG'} ({dt:.1f}s)")

# ── Test 2: Model load ──
print("\n[2/5] 加载AI模型 + 清评分缓存...")
import photo_scorer, score_cache
score_cache.clear()
r = photo_scorer.handle_load_model({})
print(f"  {r['model_name']} on {r['device']} | cache cleared")

# ── Test 3: Score all ──
print("\n[3/5] 评分75张(计时)...")
t0 = time.time()
r = photo_scorer.handle_score_photos({"folder": FOLDER, "threshold": 6.0, "copy_on_score": False})
dt_score = time.time() - t0
print(f"  完成: {r['total']}张 | {dt_score:.1f}s | avg={r.get('avg_score',0):.2f}")

# Check thumbnails
import glob, model_loader as ml
mv = ml.model_version()
rows = score_cache.get_folder(FOLDER, mv)
thumbs_ok = sum(1 for r in rows if r.get('thumb_path') and os.path.exists(r['thumb_path']))
print(f"  缩略图: {thumbs_ok}/{len(rows)} | scores: {min(r['score'] for r in rows):.1f}-{max(r['score'] for r in rows):.1f}")

# ── Test 4: Full pipeline ──
print("\n[4/5] 完整管线(跳过dedup,自动门)...")
key = setup_api()

# Clear stale state
state_path = os.path.join(FOLDER, ".lenspilot", "run_state.json")
if os.path.exists(state_path): os.remove(state_path)

import pipeline_runner as pr
import service

log_entries = []; last_log_t = time.time()
def _on_log(name, data):
    global last_log_t
    if name in ("pipeline_log", "pipeline_phase_start", "pipeline_phase_done",
                 "pipeline_done", "pipeline_error", "pipeline_gate"):
        now = time.time(); dt = now - last_log_t; last_log_t = now
        msg = data.get("message", data.get("title", data.get("summary", "")))
        log_entries.append((dt, name, msg[:100]))
service.subscribe_events(_on_log)

t_start = time.time()
params = {"work_folder": FOLDER, "template": "cull_pipeline", "goal": "春游精选",
          "pipeline_params": {"threshold": None, "eye_reject": 0.15,
                             "budget_token_cap": 500000, "gate_timeout": 300}}
res = pr.handle_pipeline_start(params)
print(f"  started: run_id={res.get('run_id')}")
if res.get("error"):
    print(f"  !! START ERROR: {res['error']} — {res.get('detail','')}")
    sys.exit(1)

# Wait, auto-approve gates
done = False
for i in range(180):
    time.sleep(1)
    run = pr._get_active_run()
    if run and run.pause_event.is_set():
        gates = list(run.pending_gates.keys())
        if gates:
            print(f"  [+{time.time()-t_start:.0f}s] GATE({gates[0]}) 自动批准")
            pr.handle_pipeline_gate_resolve({"gate_id": gates[0], "decision": "approve"})
            time.sleep(0.3)
            pr.handle_pipeline_resume({"work_folder": FOLDER})
            continue
    st = pr.handle_pipeline_get_state({"work_folder": FOLDER})
    if st.get("status") in ("done", "error"):
        done = True; break
    if i == 0 or i % 20 == 19:
        art = st.get("artifacts", {})
        print(f"  [+{time.time()-t_start:.0f}s] status={st['status']} phase={st.get('phase_idx','?')}")

if not done:
    print("  TIMEOUT! Canceling...")
    pr.handle_pipeline_cancel({"work_folder": FOLDER})

dt_total = time.time() - t_start
print(f"\n  总耗时: {dt_total:.1f}s")
st = pr.handle_pipeline_get_state({"work_folder": FOLDER})
art = st.get("artifacts", {})
print(f"  结果: scanned={art.get('scanned')} scored={art.get('scored')} "
      f"threshold={art.get('threshold_used')} keep={art.get('keep')} reject={art.get('reject')}")

# ── Test 5: Timing breakdown ──
print("\n[5/5] 阶段耗时分析:")
prev_t = 0
for dt, name, msg in log_entries:
    if dt > 0.5:
        print(f"  +{dt:.1f}s [{name}] {msg}")
    prev_t += dt

# ── Summary ──
print("\n" + "=" * 50)
print(f"总耗时: {dt_total:.0f}s (评分{dt_score:.0f}s + LLM + 管线开销)")
print(f"缩略图: {thumbs_ok}/{len(rows)} OK")
print(f"分数: {min(r['score'] for r in rows):.1f}-{max(r['score'] for r in rows):.1f}")
print(f"Keeper: {art.get('keep',0)} | Reject: {art.get('reject',0)}")
if dt_total < 120: print("PASS: 管线在2分钟内完成")
else: print("WARN: 管线超2分钟,检查瓶颈")
