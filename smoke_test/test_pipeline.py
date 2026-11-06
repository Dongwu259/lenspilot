#!/usr/bin/env python
"""
数字员工 Pipeline 诊断测试脚本
用法: cd electron_app/python_backend && python ..\..\smoke_test\test_pipeline.py

目的: 跑完整 pipeline 并对每步计时,定位瓶颈
"""
import os, sys, json, time, threading, io

# Fix Unicode output on Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Path setup
HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
BACKEND = os.path.join(PROJ, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

FOLDER = r"D:\photoDatebase\4-2春游"
KEY_FILE = os.path.join(HERE, "smoke_agent_key.txt")

def load_key():
    lines = []
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return {"base_url": lines[0], "api_key": lines[1], "model": lines[2]}

def main():
    import service
    import photo_scorer
    import score_cache

    # ── Suppress noisy logs ──
    import model_loader as ml
    import agent_module

    key = load_key()
    print(f"=== API: {key['base_url']} / model={key['model']} ===")

    # ── Inject API key into agent config ──
    agent_module._official_config = {
        "base_url": key["base_url"],
        "api_key": key["api_key"],
        "model": key["model"],
    }
    # Also set disk config so _load_agent_config returns something
    service._app_config["agent"] = {
        "base_url": key["base_url"],
        "api_key": key["api_key"],
        "model": key["model"],
    }

    # ── Pre-load model (ONNX) so scoring doesn't hang ──
    print("=== 加载 ONNX 模型... ===")
    t0 = time.time()
    onnx_path = os.path.join(PROJ, "electron_app", "model", "best_model.onnx")
    onnx_data = os.path.join(PROJ, "electron_app", "model", "best_model.onnx.data")
    if os.path.exists(onnx_path):
        ml._load_onnx(onnx_path)
        print(f"  ONNX 模型加载: {time.time()-t0:.1f}s")
    else:
        # Try user data dir
        alt = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                          "LensPilot", "model", "best_model.onnx")
        if os.path.exists(alt):
            ml._load_onnx(alt)
            print(f"  ONNX 模型加载(用户目录): {time.time()-t0:.1f}s")
        else:
            print("  WARNING: 未找到 ONNX 模型")

    # ── Test 1: Time a single HEIC load ──
    print("\n=== Test 1: HEIC 单张加载+pHASH 速度 ===")
    import glob as _g
    images = sorted(_g.glob(os.path.join(FOLDER, "*.*")))
    images = [p for p in images if os.path.splitext(p)[1].lower() in (".heic", ".heif", ".jpg", ".jpeg", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".orf", ".rw2")]
    print(f"  总图片: {len(images)}")

    if images:
        sample = images[0]
        ext = os.path.splitext(sample)[1].lower()
        print(f"  测试文件: {os.path.basename(sample)} ({ext})")

        # A) Full _load_image_rgb
        t0 = time.time()
        try:
            img = ml._load_image_rgb(sample)
            dt = time.time() - t0
            print(f"  _load_image_rgb 全分辨率: {dt:.2f}s (size={img.size})")
        except Exception as e:
            print(f"  _load_image_rgb FAIL: {e}")

        # B) PIL thumbnail 256px
        from PIL import Image as PILImage
        t0 = time.time()
        try:
            thumb = PILImage.open(sample)
            thumb.thumbnail((256, 256), PILImage.LANCZOS)
            dt = time.time() - t0
            print(f"  PIL 256px thumbnail:     {dt:.2f}s (size={thumb.size})")
        except Exception as e:
            print(f"  PIL 256px FAIL: {e}")

        # C) pHash on thumbnail
        import imagehash
        try:
            thumb = PILImage.open(sample)
            thumb.thumbnail((256, 256), PILImage.LANCZOS)
            t0 = time.time()
            h = imagehash.phash(thumb.convert('L'))
            dt = time.time() - t0
            print(f"  pHash on 256px thumb:     {dt:.3f}s (hash={h})")
        except Exception as e:
            print(f"  pHash FAIL: {e}")

    # ── Test 2: Time dedup full run ──
    print("\n=== Test 2: handle_dedup_burst(dry_run) 全量计时 ===")
    t0 = time.time()
    try:
        result = photo_scorer.handle_dedup_burst({"folder": FOLDER, "dry_run": True})
        dt = time.time() - t0
        print(f"  dedup 总耗时: {dt:.1f}s")
        print(f"  结果: total={result.get('total')} groups={result.get('groups')} duplicates={result.get('duplicates')}")
    except Exception as e:
        import traceback
        print(f"  dedup FAIL after {time.time()-t0:.1f}s: {e}")
        traceback.print_exc()

    # ── Test 3: Time handle_score_photos ──
    print("\n=== Test 3: 评分 75 张(前 5 张实测,后 70 估计) ===")
    if photo_scorer._model_loaded:
        t0 = time.time()
        result = photo_scorer.handle_score_photos({
            "folder": FOLDER, "threshold": 6.0, "copy_on_score": False})
        dt = time.time() - t0
        print(f"  评分 总耗时: {dt:.1f}s ({result.get('total',0)}张)")
    else:
        print("  模型未加载,跳过")

    # ── Test 4: Full pipeline with timing per step ──
    print("\n=== Test 4: 完整 pipeline(每步计时) ===")
    import pipeline_runner as pr

    # Subscribe to pipeline_log for timing
    log_entries = []
    def _on_log(name, data):
        if name == "pipeline_log":
            log_entries.append((time.time(), data.get("message", "")))
        elif name == "pipeline_phase_start":
            log_entries.append((time.time(), f"PHASE: {data.get('title','')}"))
    service.subscribe_events(_on_log)

    # Clear stale state
    state_path = os.path.join(FOLDER, ".lenspilot", "run_state.json")
    if os.path.exists(state_path):
        os.remove(state_path)

    t_start = time.time()
    params = {
        "work_folder": FOLDER,
        "template": "cull_pipeline",
        "goal": "春游精选",
        "pipeline_params": {"threshold": None, "eye_reject": 0.15,
                           "budget_token_cap": 500000, "gate_timeout": 300},
    }
    res = pr.handle_pipeline_start(params)
    print(f"  started: run_id={res.get('run_id')}")

    last_t = t_start
    done = False
    for i in range(300):
        time.sleep(2)
        now = time.time()
        st = pr.handle_pipeline_get_state({"work_folder": FOLDER})
        status = st.get("status", "?")
        # Print new log entries
        for ts, msg in log_entries:
            if ts > last_t:
                print(f"  [+{ts-t_start:.0f}s] {msg}")
        last_t = now
        log_entries.clear()
        # Check gate
        run = pr._get_active_run()
        if run and run.pause_event.is_set():
            gates = list(run.pending_gates.keys())
            if gates:
                print(f"  [+{now-t_start:.0f}s] GATE: {gates[0]} → 自动批准")
                pr.handle_pipeline_gate_resolve({"gate_id": gates[0], "decision": "approve"})
                time.sleep(0.5)
                pr.handle_pipeline_resume({"work_folder": FOLDER})
                continue
        if status in ("done", "error"):
            done = True
            print(f"\n  FINAL [{status}]: +{now-t_start:.0f}s artifacts={st.get('artifacts',{})}")
            break
        if i % 15 == 14:
            print(f"  [+{now-t_start:.0f}s] status={status} phase={st.get('phase_idx')} step={st.get('step_idx')}")

    if not done:
        print("  TIMEOUT: pipeline 300s未完成")
        pr.handle_pipeline_cancel({"work_folder": FOLDER})

    print(f"\n=== 总耗时: {time.time()-t_start:.0f}s ===")

    # ── Test 5: LLM Borderline 批量裁决 ──
    print("\n=== Test 5: LLM Borderline 批量裁决 ===")
    if not photo_scorer._model_loaded:
        print("  模型未加载,跳过")
    else:
        # Create a mock run with scoring artifacts
        run2 = pr.PipelineRun("diag_borderline", FOLDER, "cull_pipeline", "L3", "春游精选", {
            "budget_token_cap": 500000, "budget_wall_s": 1800, "budget_steps": 200,
        })
        run2.status = "running"
        run2.artifacts = {"threshold_used": 6.0, "scanned": 71, "scored": 71}
        t0 = time.time()
        try:
            result = pr._cull_borderline_step(run2)
            dt = time.time() - t0
            print(f"  borderline 耗时: {dt:.1f}s")
            print(f"  结果: keep={result.get('kept')} reject={result.get('rejected')} uncertain={result.get('uncertain')}")
            print(f"  uncertain_count in artifacts: {run2.artifacts.get('uncertain_count', 'N/A')}")
            up = run2.artifacts.get('uncertain_paths', [])
            if up:
                print(f"  uncertain 详情: {len(up)} 项, 含字段: {list(up[0].keys())}")
        except Exception as e:
            import traceback
            print(f"  borderline FAIL: {e}")
            traceback.print_exc()

    # ── Test 6: Restore + Override ──
    print("\n=== Test 6: 恢复原样 + Override 追踪 ===")
    import tempfile, shutil as _shutil
    tmp = tempfile.mkdtemp(prefix="lp_diag_")
    try:
        dup_dir = os.path.join(tmp, "_duplicates")
        os.makedirs(dup_dir, exist_ok=True)
        for i in range(3):
            with open(os.path.join(dup_dir, f"dup_{i}.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01" + b"\x00" * 100)
        rp = os.path.join(tmp, ".lenspilot", "run_state.json")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            json.dump({"status": "done"}, f)
        r = pr.handle_pipeline_restore({"work_folder": tmp})
        print(f"  restore: {r.get('message', r)}")
        print(f"  _duplicates exists: {os.path.exists(dup_dir)}")
        print(f"  run_state exists: {os.path.exists(rp)}")
        # Test pipeline_busy rejection
        _orig = pr._get_active_run
        run_fake = pr.PipelineRun("x", tmp, "cull_pipeline", "L3", "t", {})
        pr._get_active_run = lambda: run_fake
        r2 = pr.handle_pipeline_restore({"work_folder": tmp})
        print(f"  busy rejection: {r2.get('error', 'no error')}")
        pr._get_active_run = _orig
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    # ── Test 7: feedback_queue override column ──
    print("\n=== Test 7: feedback_queue override 字段验证 ===")
    try:
        import feedback_queue, sqlite3
        from feedback_queue import _db_path
        conn = sqlite3.connect(_db_path, timeout=10)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_queue)").fetchall()}
        conn.close()
        print(f"  override 列存在: {'override' in cols}")
        # Enqueue with override
        r = feedback_queue.enqueue({
            "model_version": "diag_test",
            "feature": [0.0] * 128,
            "label": 0, "source": "pipeline_override",
            "override": True,
        })
        print(f"  enqueue override=True: {r}")
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT override FROM feedback_queue WHERE source='pipeline_override' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        print(f"  override stored: {dict(row) if row else 'NOT FOUND'}")
    except Exception as e:
        print(f"  feedback_queue test error: {e}")

    print(f"\n=== 诊断完成 ===")

if __name__ == "__main__":
    main()
