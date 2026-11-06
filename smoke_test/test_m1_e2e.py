#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
M1 数字员工管线 — 全面集成测试
================================
脱离 Electron，直接 import 后端模块运行完整管线。
覆盖: 全流程/LLM阈值+LLM borderline/门控决议/override/恢复/进度事件/崩溃续跑

用法:
  cd electron_app/python_backend
  python ..\..\smoke_test\test_m1_e2e.py              # 全部测试
  python ..\..\smoke_test\test_m1_e2e.py --quick      # 快速测试(跳过LLM)
  python ..\..\smoke_test\test_m1_e2e.py --phase dedup # 只测去重
"""
import os, sys, json, time, threading, shutil, tempfile, argparse, glob as _g, io

# Fix Unicode output on Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
BACKEND = os.path.join(PROJ, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

TEST_FOLDER = r"D:\photoDatebase\4-2春游"
KEY_FILE = os.path.join(HERE, "smoke_agent_key.txt")

PASS = FAIL = SKIP = 0

# ── Helpers ──
def check(cond, msg):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {msg}")
    else:    FAIL += 1; print(f"  ❌ {msg}")

def skip(msg):
    global SKIP; SKIP += 1; print(f"  ⏭ {msg}")

def section(title):
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

def load_key():
    lines = []
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            lines.append(line)
    return {"base_url": lines[0], "api_key": lines[1], "model": lines[2]}


# ═══════════════════════════════════════════════════════════════
# Phase 0: Bootstrap
# ═══════════════════════════════════════════════════════════════
def bootstrap():
    """Initialize backend modules outside Electron."""
    section("Phase 0: 后端初始化")

    import service
    import photo_scorer
    import score_cache
    import model_loader as ml
    import agent_module
    import pipeline_runner as pr

    # Suppress logs during test
    service.send_log = lambda msg, level="info": None
    # Keep send_event working for pipeline events
    _events = []
    def _capture_event(name, data):
        _events.append((time.time(), name, data))
    service.send_event = _capture_event

    # Inject API key
    key = load_key()
    print(f"  API: {key['base_url']} / model={key['model']}")
    agent_module._official_config = {
        "base_url": key["base_url"], "api_key": key["api_key"], "model": key["model"],
    }
    service._app_config["agent"] = {
        "base_url": key["base_url"], "api_key": key["api_key"], "model": key["model"],
    }

    # Load ONNX model
    print("  加载模型...")
    t0 = time.time()
    model_loaded = False

    # Try known ONNX model locations
    candidates = [
        os.path.join(PROJ, "electron_app", "model", "best_model.onnx"),
        os.path.join(PROJ, "electron_app", "model", "best_model.onnx.data"),
    ]
    # Also check with .data extension
    onnx_path = None
    onnx_data = None
    for c in candidates:
        d = os.path.dirname(c)
        for f in os.listdir(d) if os.path.isdir(d) else []:
            fp = os.path.join(d, f)
            if f == "best_model.onnx" and os.path.isfile(fp):
                onnx_path = fp
            if f == "best_model.onnx.data" and os.path.isfile(fp):
                onnx_data = fp

    if onnx_path:
        try:
            ml._load_onnx(onnx_path)
            photo_scorer._model_loaded = True
            model_loaded = True
            print(f"  ONNX 模型: {onnx_path}")
        except Exception as e:
            print(f"  _load_onnx 失败: {e}")

    if not model_loaded:
        # Fallback: try the full load_model flow
        try:
            r = photo_scorer.handle_load_model({})
            model_loaded = photo_scorer._model_loaded
            if model_loaded:
                print(f"  handle_load_model: {r.get('model_name', '?')}")
        except Exception as e:
            print(f"  handle_load_model 失败: {e}")

    check(model_loaded, f"模型加载 ({time.time()-t0:.1f}s)")

    # Register pipeline handlers
    try:
        pr.register_handlers()
    except Exception:
        pass  # may already be registered

    return {
        "service": service, "photo_scorer": photo_scorer,
        "score_cache": score_cache, "model_loader": ml,
        "agent_module": agent_module, "pipeline_runner": pr,
        "key": key,
    }


# ═══════════════════════════════════════════════════════════════
# Phase 1: Basic pipeline (scan + score, no LLM)
# ═══════════════════════════════════════════════════════════════
def test_basic_pipeline(mods):
    section("Phase 1: 基础管线 (scan → score, 无LLM)")

    pr = mods["pipeline_runner"]
    folder = TEST_FOLDER

    # Clear prior state
    rp = os.path.join(folder, ".lenspilot", "run_state.json")
    if os.path.exists(rp): os.remove(rp)

    t0 = time.time()
    params = {
        "work_folder": folder, "template": "cull_pipeline",
        "goal": "春游精选",
        "pipeline_params": {
            "threshold": 6.5,  # user-specified → skip LLM threshold
            "eye_reject": 0.15, "budget_token_cap": 500000, "gate_timeout": 0,
        },
    }
    res = pr.handle_pipeline_start(params)
    check(res.get("ok"), f"pipeline_start → run_id={res.get('run_id','?')}")
    run_id = res.get("run_id", "")

    # Wait for pipeline to complete (or hit a gate)
    status_log = []
    for i in range(180):  # max 6 min (2s * 180)
        time.sleep(2)
        st = pr.handle_pipeline_get_state({"work_folder": folder})
        status = st.get("status", "?")
        if status not in [s for s, _ in status_log]:
            status_log.append((status, time.time() - t0))
        if status in ("done", "error", "canceled"):
            break
        # Auto-resolve any gate
        run = pr._get_active_run()
        if run and run.pause_event.is_set():
            gates = list(run.pending_gates.keys())
            if gates:
                gid = gates[0]
                print(f"  自动批准门: {gid}")
                pr.handle_pipeline_gate_resolve({"gate_id": gid, "decision": "approve"})
                time.sleep(0.3)
                pr.handle_pipeline_resume({"work_folder": folder})

    elapsed = time.time() - t0
    final_st = pr.handle_pipeline_get_state({"work_folder": folder})
    artifacts = final_st.get("artifacts", {})

    check(final_st.get("status") == "done", f"管线完成 ({elapsed:.0f}s)")
    check(artifacts.get("scanned", 0) > 0, f"扫描: {artifacts.get('scanned')} 张")
    check(artifacts.get("scored", 0) > 0, f"评分: {artifacts.get('scored')} 张")
    check(artifacts.get("keep", 0) > 0, f"keeper: {artifacts.get('keep')} 张")

    # Verify dedup didn't run (we skipped due to dry_run gate auto-approve? No — gate auto-approve causes apply)
    dg = artifacts.get("dedup_groups", 0)
    dm = artifacts.get("dedup_to_move", 0)
    print(f"  去重组数: {dg}, 移走: {dm}")

    return artifacts


# ═══════════════════════════════════════════════════════════════
# Phase 2: LLM threshold
# ═══════════════════════════════════════════════════════════════
def test_llm_threshold(mods, quick=False):
    section("Phase 2: LLM 阈值决策")

    if quick:
        skip("快速模式跳过 LLM 调用")
        return None

    from pipeline_runner import _threshold_step, _score_histogram
    pr = mods["pipeline_runner"]

    # Create a minimal run for testing threshold step
    folder = TEST_FOLDER
    rp = os.path.join(folder, ".lenspilot", "run_state.json")
    if os.path.exists(rp): os.remove(rp)

    run = pr.PipelineRun("t1", folder, "cull_pipeline", "L3", "婚礼精选", {
        "budget_token_cap": 500000, "budget_wall_s": 1800, "budget_steps": 200,
    })
    run.status = "running"
    # Pre-populate scores (reuse existing cache if present)
    histo = _score_histogram(folder)
    check(histo["total"] > 0, f"分数直方图: {histo['total']} 张")

    t0 = time.time()
    result = _threshold_step(run)
    elapsed = time.time() - t0
    threshold = result.get("threshold", 0)
    source = result.get("source", "?")

    check(1.0 <= threshold <= 10.0, f"LLM 阈值={threshold} (source={source}, {elapsed:.1f}s)")
    check(run.budget.get("spent_tokens", 0) > 0, f"LLM token 消耗: {run.budget.get('spent_tokens')}")

    return threshold


# ═══════════════════════════════════════════════════════════════
# Phase 3: Dedup
# ═══════════════════════════════════════════════════════════════
def test_dedup(mods):
    section("Phase 3: 连拍去重")

    photo_scorer = mods["photo_scorer"]

    t0 = time.time()
    result = photo_scorer.handle_dedup_burst({
        "folder": TEST_FOLDER, "dry_run": True, "time_window": 3, "similarity": 5,
    })
    elapsed = time.time() - t0

    total = result.get("total", 0)
    groups = result.get("groups", 0)
    dups = result.get("duplicates", 0)
    skipped = result.get("skipped", "")

    check(total > 0, f"图片总数: {total}")
    if skipped:
        skip(f"跳过原因: {skipped}")
    else:
        check(groups >= 0, f"连拍组: {groups}, 重复: {dups} ({elapsed:.1f}s)")

    # Verify group_details format
    gd = result.get("group_details", [])
    if gd:
        check("kept" in gd[0], "group_detail 含 kept 字段")
        check("moved" in gd[0], "group_detail 含 moved 字段")

    # Verify hash engine doesn't import scipy (regression check)
    import sys
    check("scipy" not in sys.modules or "imagehash" not in sys.modules,
          "pHash 引擎不依赖 scipy/imagehash (防卡死)")

    return result


# ═══════════════════════════════════════════════════════════════
# Phase 4: LLM Borderline
# ═══════════════════════════════════════════════════════════════
def test_llm_borderline(mods, quick=False):
    section("Phase 4: LLM 批量裁决 borderline")

    if quick:
        skip("快速模式跳过 LLM 调用")
        return None

    from pipeline_runner import _cull_borderline_step
    pr = mods["pipeline_runner"]
    folder = TEST_FOLDER

    # Create a test run with artifacts from a prior scoring phase
    run = pr.PipelineRun("b1", folder, "cull_pipeline", "L3", "春游精选", {
        "budget_token_cap": 500000, "budget_wall_s": 1800, "budget_steps": 200,
    })
    run.status = "running"
    # Need threshold in artifacts
    run.artifacts["threshold_used"] = 6.5
    run.artifacts["scanned"] = 71
    run.artifacts["scored"] = 71

    t0 = time.time()
    result = _cull_borderline_step(run)
    elapsed = time.time() - t0

    kept = result.get("kept", 0)
    rejected = result.get("rejected", 0)
    uncertain = result.get("uncertain", 0)

    check(kept + rejected + uncertain > 0, f"LLM borderline: keep={kept} reject={rejected} uncertain={uncertain} ({elapsed:.1f}s)")
    check(run.artifacts.get("uncertain_count", -1) == uncertain, "uncertain_count 正确写入 artifacts")

    if uncertain > 0:
        up = run.artifacts.get("uncertain_paths", [])
        check(len(up) == uncertain, f"uncertain_paths 数量匹配 ({len(up)})")
        if up:
            check("thumb" in up[0], "uncertain item 含 thumb")
            check("score" in up[0], "uncertain item 含 score")

    return result


# ═══════════════════════════════════════════════════════════════
# Phase 5: Gate resolution & Override
# ═══════════════════════════════════════════════════════════════
def test_gates_and_override(mods):
    section("Phase 5: 门控决议 + override 追踪")

    pr = mods["pipeline_runner"]
    folder = TEST_FOLDER

    # Create run with a mock final review gate
    run = pr.PipelineRun("g1", folder, "cull_pipeline", "L3", "测试", {
        "budget_token_cap": 500000, "budget_wall_s": 1800, "budget_steps": 200,
    })
    run.status = "running"
    run.artifacts = {
        "scanned": 71, "scored": 71, "threshold_used": 6.5,
        "dedup_to_move": 5, "reject": 10, "borderline": 3,
        "keep": 50, "keep_avg_score": 7.2, "uncertain_count": 2,
    }

    # Simulate the final review gate being set
    import threading
    run.pause_event.set()
    _orig = pr._get_active_run
    pr._get_active_run = lambda: run  # mock

    # Resolve with approve
    t0 = time.time()
    r1 = pr.handle_pipeline_gate_resolve({"gate_id": "final_review", "decision": "approve"})
    check(r1.get("ok"), "final_review approve → ok")
    check(not run.pause_event.is_set(), "pause_event cleared after approve")
    check(run.artifacts.get("review_approved") == True, "review_approved=True")

    # Reset and test reject
    run.pause_event.set()
    r2 = pr.handle_pipeline_gate_resolve({"gate_id": "final_review", "decision": "reject"})
    check(r2.get("ok"), "final_review reject → ok")
    check(not run.pause_event.is_set(), "pause_event cleared after reject")
    check(run.artifacts.get("review_approved") == False, "review_approved=False")
    check(run.artifacts.get("review_rejected") == True, "review_rejected 标记已设置")

    # Test action gate (dedup confirm)
    run.pause_event.clear()
    from pipeline_runner import _await_gate
    # Start await_gate in a thread
    gate_result = {}
    def _waiter():
        gate_result["r"] = _await_gate(run, "action", "测试动作门", {"groups": 3, "to_move": 10})
    tw = threading.Thread(target=_waiter, daemon=True)
    tw.start()
    time.sleep(0.2)

    # Gate should be pending
    gates = list(run.pending_gates.keys())
    check(len(gates) > 0, f"动作门已创建: {gates[0][:8]}...")

    # Resolve the action gate
    if gates:
        r3 = pr.handle_pipeline_gate_resolve({"gate_id": gates[0], "decision": "approve"})
        check(r3.get("ok"), "动作门 approve → ok")
        tw.join(timeout=2)
        check("r" in gate_result, "动作门 _await_gate 返回")
        if gate_result.get("r"):
            check(gate_result["r"].get("approved") == True, "动作门 approved=True")

    pr._get_active_run = _orig  # restore


# ═══════════════════════════════════════════════════════════════
# Phase 6: Restore
# ═══════════════════════════════════════════════════════════════
def test_restore(mods):
    section("Phase 6: 恢复原样功能")

    pr = mods["pipeline_runner"]
    score_cache = mods["score_cache"]

    # Create temp folder with _duplicates subfolder
    tmp = tempfile.mkdtemp(prefix="m1_restore_")
    try:
        dup_dir = os.path.join(tmp, "_duplicates")
        os.makedirs(dup_dir, exist_ok=True)

        # Create dummy files
        test_files = []
        for i in range(3):
            fp = os.path.join(dup_dir, f"test_dup_{i}.jpg")
            with open(fp, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 200)
            test_files.append(fp)

        # Create run_state.json
        rp = os.path.join(tmp, ".lenspilot", "run_state.json")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            json.dump({"status": "done", "run_id": "abc"}, f)

        # Execute restore
        r = pr.handle_pipeline_restore({"work_folder": tmp})
        check(r.get("ok"), f"restore ok: {r.get('message', '')}")
        check(r.get("moved_back") == 3, f"移回 {r.get('moved_back')} 张")

        # Verify files moved back
        for i in range(3):
            src = os.path.join(tmp, f"test_dup_{i}.jpg")
            check(os.path.exists(src), f"文件 {i} 已移回原目录")

        # Verify _duplicates deleted
        check(not os.path.exists(dup_dir), "_duplicates 文件夹已删除")

        # Verify run_state deleted
        check(not os.path.exists(rp), "run_state.json 已删除")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Test restore with active pipeline → should reject
    run = pr.PipelineRun("r_active", tmp, "cull_pipeline", "L3", "test", {})
    _orig = pr._get_active_run
    pr._get_active_run = lambda: run
    r2 = pr.handle_pipeline_restore({"work_folder": tmp})
    check(r2.get("error") == "pipeline_busy", "运行中拒绝恢复 → pipeline_busy")
    pr._get_active_run = _orig


# ═══════════════════════════════════════════════════════════════
# Phase 7: Progress events
# ═══════════════════════════════════════════════════════════════
def test_progress_events(mods):
    section("Phase 7: 进度事件")

    pr = mods["pipeline_runner"]
    service = mods["service"]
    photo_scorer = mods["photo_scorer"]
    folder = TEST_FOLDER

    # Capture events
    captured = []
    _orig_send = service.send_event
    def _cap(name, data):
        captured.append((name, data))
        _orig_send(name, data)
    service.send_event = _cap

    # Run a quick dedup and verify progress events fire
    try:
        rp = os.path.join(folder, ".lenspilot", "run_state.json")
        if os.path.exists(rp): os.remove(rp)

        params = {
            "work_folder": folder, "template": "cull_pipeline",
            "goal": "test",
            "pipeline_params": {"threshold": 9.9, "budget_token_cap": 500000, "gate_timeout": 0},
        }
        res = pr.handle_pipeline_start(params)
        check(res.get("ok"), "pipeline started for event test")

        # Wait for completion
        for i in range(120):
            time.sleep(1)
            run = pr._get_active_run()
            if not run: break
            if run.pause_event.is_set():
                gates = list(run.pending_gates.keys())
                if gates:
                    pr.handle_pipeline_gate_resolve({"gate_id": gates[0], "decision": "approve"})
                    time.sleep(0.3)
                    pr.handle_pipeline_resume({"work_folder": folder})
            st = pr.handle_pipeline_get_state({"work_folder": folder})
            if st.get("status") in ("done", "error"): break

        pipeline_events = [e for e in captured if e[0].startswith("pipeline_")]
        event_names = set(e[0] for e in pipeline_events)

        check("pipeline_started" in event_names, "pipeline_started event")
        check("pipeline_phase_start" in event_names, "pipeline_phase_start event(s)")
        check("pipeline_progress" in event_names, "pipeline_progress event(s)")
        check("pipeline_log" in event_names, "pipeline_log event(s)")

        # Verify progress events have current/total fields
        progress_events = [e for e in pipeline_events if e[0] == "pipeline_progress"]
        if progress_events:
            pe = progress_events[0][1]
            has_phase = "phase" in pe
            has_total = "total" in pe and pe["total"] > 0
            check(has_phase or has_total, f"progress 事件含 phase/total 字段")
        else:
            skip("无 progress 事件(管线太快?)")

        # Verify done event
        check("pipeline_done" in event_names, "pipeline_done event")

        print(f"  共捕获 {len(pipeline_events)} 个 pipeline 事件, {len(event_names)} 种类型")

    finally:
        service.send_event = _orig_send


# ═══════════════════════════════════════════════════════════════
# Phase 8: Crash recovery (run_state resume)
# ═══════════════════════════════════════════════════════════════
def test_crash_recovery(mods):
    section("Phase 8: 崩溃恢复 (run_state 续跑)")

    pr = mods["pipeline_runner"]
    folder = TEST_FOLDER

    # Write a fake interrupted run_state at phase 2 (score done, about to start dedup)
    sd = os.path.join(folder, ".lenspilot")
    os.makedirs(sd, exist_ok=True)
    rp = os.path.join(sd, "run_state.json")

    fake_state = {
        "schema": 1,
        "run_id": "crash_test_001",
        "template": "cull_pipeline",
        "autonomy": "L3",
        "goal": "恢复测试",
        "params": {"threshold": 9.9, "budget_token_cap": 500000},
        "status": "paused",
        "phase_idx": 2,   # dedup phase
        "step_idx": 0,    # dedup_preview step
        "budget": {"token_cap": 500000, "wall_cap_s": 1800, "step_cap": 200,
                   "spent_tokens": 5000, "spent_s": 45, "steps": 5, "_downgraded": False},
        "stall": {"last_progress_sig": "scored:71", "no_progress_rounds": 0,
                  "last_error": "", "err_repeat": 0, "error_hash": ""},
        "artifacts": {"scanned": 71, "scored": 71, "threshold_used": 9.9},
        "pending_gate": False,
        "updated_at": "2026-07-14T00:00:00Z",
    }
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(fake_state, f, ensure_ascii=False, indent=2)

    # Load state → verify resume point
    st = pr._load_state(folder)
    check(st is not None, "run_state 读取成功")
    check(st["phase_idx"] == 2, "续跑起点: phase_idx=2 (dedup)")
    check(st["step_idx"] == 0, "续跑起点: step_idx=0 (dedup_preview)")
    check(st["artifacts"]["scanned"] == 71, "artifacts 保留: scanned=71")
    check(st["artifacts"]["scored"] == 71, "artifacts 保留: scored=71")

    # Verify resume from state works
    r = pr.handle_pipeline_resume({"work_folder": folder})
    check(r.get("ok") or r.get("resumed"), f"续跑启动: {r}")

    # Wait briefly and cancel (don't run full pipeline twice)
    time.sleep(1)
    if pr._get_active_run():
        pr.handle_pipeline_cancel({"work_folder": folder})

    # Cleanup
    if os.path.exists(rp):
        os.remove(rp)

    # Verify _resume_from_state with invalid folder
    r2 = pr._resume_from_state({"work_folder": "/nonexistent/path"})
    check(r2.get("error"), "无效文件夹续跑 → 报错")


# ═══════════════════════════════════════════════════════════════
# Phase 9: Template completeness
# ═══════════════════════════════════════════════════════════════
def test_template_completeness(mods):
    section("Phase 9: 模板完整性")

    from pipeline_runner import TEMPLATES, CULL_PIPELINE

    tmpl = CULL_PIPELINE
    check(tmpl["id"] == "cull_pipeline", "模板 id")
    check(len(tmpl["phases"]) == 5, "5 个阶段")

    step_count = sum(len(p["steps"]) for p in tmpl["phases"])
    check(step_count == 8, f"共 {step_count} 个步骤 (预期 8)")

    # Verify phase order
    phase_order = [p["id"] for p in tmpl["phases"]]
    check(phase_order == ["import", "score", "dedup", "cull", "select"],
          f"阶段顺序正确: {phase_order}")

    # Verify all step fns are callable and importable
    for p in tmpl["phases"]:
        for s in p["steps"]:
            fn = s["fn"]
            check(callable(fn), f"{p['id']}/{s['name']} callable")
            check(fn.__doc__ is not None and len(fn.__doc__) > 10,
                  f"{p['id']}/{s['name']} 有文档字符串")


# ═══════════════════════════════════════════════════════════════
# Phase 10: Feedback queue integrity
# ═══════════════════════════════════════════════════════════════
def test_feedback_integrity(mods):
    section("Phase 10: 飞轮反馈队列完整性")

    try:
        import feedback_queue
    except ImportError:
        skip("feedback_queue 不可用")
        return

    import sqlite3
    from feedback_queue import _db_path

    conn = sqlite3.connect(_db_path, timeout=10)
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(feedback_queue)").fetchall()}
    conn.close()

    # Required columns
    required = ["model_version", "feature_dim", "feature", "label", "source",
                "category", "camera_model", "score", "blur", "eye_open",
                "override", "created_at", "uploaded"]
    for col in required:
        check(col in cols, f"列 {col} 存在 (类型={cols.get(col, 'MISSING')})")

    # Verify enqueue with all fields
    r = feedback_queue.enqueue({
        "model_version": "test_m1",
        "feature": [0.1] * 512,
        "label": 1,
        "source": "pipeline_select_keep",
        "category": "wedding",
        "camera_model": "ILCE-7M4",
        "score": 7.5,
        "blur": 0.2,
        "eye_open": 0.9,
        "override": False,
    })
    check(r, "enqueue 全字段成功")

    # Verify with override=True
    r2 = feedback_queue.enqueue({
        "model_version": "test_m1",
        "feature": [0.1] * 128,
        "label": 0,
        "source": "pipeline_override",
        "override": True,
    })
    check(r2, "enqueue override=True 成功")

    # Verify count
    cnt = feedback_queue.count_pending()
    check(cnt >= 2, f"feedback_queue 有 {cnt} 条待上传记录")

    # Verify take_pending returns correct fields
    rows = feedback_queue.take_pending(2)
    if rows:
        check("override" in rows[0], "take_pending 返回 override 字段")
        check("source" in rows[0], "take_pending 返回 source 字段")
        check(isinstance(rows[0]["override"], int), "override 字段为整数")
        # Mark as uploaded to clean up
        ids = [row["id"] for row in rows]
        feedback_queue.mark_uploaded(ids)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="M1 Pipeline E2E Test")
    parser.add_argument("--quick", action="store_true", help="跳过 LLM 调用")
    parser.add_argument("--phase", type=str, help="只跑指定阶段 (basic/llm_threshold/dedup/borderline/gates/restore/progress/crash/template/feedback)")
    args = parser.parse_args()

    print("=" * 60)
    print("  M1 数字员工 — 全面集成测试")
    print("=" * 60)

    if not os.path.exists(TEST_FOLDER):
        print(f"⚠ 测试文件夹不存在: {TEST_FOLDER}")
        print("  请修改 TEST_FOLDER 变量指向包含照片的文件夹")
        sys.exit(1)

    # Bootstrap
    mods = bootstrap()

    # Select phases to run
    phases = args.phase.split(",") if args.phase else None

    all_phases = {
        "basic":      lambda: test_basic_pipeline(mods),
        "llm_threshold": lambda: test_llm_threshold(mods, args.quick),
        "dedup":      lambda: test_dedup(mods),
        "borderline": lambda: test_llm_borderline(mods, args.quick),
        "gates":      lambda: test_gates_and_override(mods),
        "restore":    lambda: test_restore(mods),
        "progress":   lambda: test_progress_events(mods),
        "crash":      lambda: test_crash_recovery(mods),
        "template":   lambda: test_template_completeness(mods),
        "feedback":   lambda: test_feedback_integrity(mods),
    }

    if phases:
        for p in phases:
            p = p.strip()
            if p in all_phases:
                all_phases[p]()
            else:
                print(f"  未知阶段: {p}")
    else:
        for name, fn in all_phases.items():
            fn()

    # Report
    print(f"\n{'=' * 60}")
    total = PASS + FAIL + SKIP
    print(f"  Results: {PASS} passed, {FAIL} failed, {SKIP} skipped ({total} total)")
    print(f"{'=' * 60}")

    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
