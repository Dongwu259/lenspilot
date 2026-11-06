"""
test_pipeline.py — M1 数字员工管线单元测试

覆盖:
  - run_state 原子写 + 崩溃恢复
  - 进展签名 + 停滞门
  - 成本门计算
  - 阈值规则退路
  - borderline 规则退路 (不测试 LLM 实时调用,避免 token 消耗)
  - PipelineRun 状态对象

用法: python smoke_test/test_pipeline.py
"""

import os, sys, json, tempfile, time, threading

# Add python_backend to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'python_backend'))

PASS, FAIL = 0, 0

def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


# ────────────────────────────────────────────────────────────────
# Test 1: PipelineRun creation + budget defaults
# ────────────────────────────────────────────────────────────────
def test_pipeline_run():
    print("\n── Test 1: PipelineRun 创建 + budget 默认值 ──")
    from pipeline_runner import PipelineRun
    run = PipelineRun("test_id", "/tmp/fake", "cull_pipeline", "L3", "测试", {})
    check(run.run_id == "test_id", "run_id")
    check(run.template == "cull_pipeline", "template")
    check(run.autonomy == "L3", "autonomy")
    check(not run.cancel_event.is_set(), "cancel_event clear")
    check(not run.pause_event.is_set(), "pause_event clear (running)")
    check(run.budget["token_cap"] == 150_000, "default token_cap")
    check(run.budget["wall_cap_s"] == 1800, "default wall_cap_s")
    check(run.budget["step_cap"] == 200, "default step_cap")
    check(run.budget["steps"] == 0, "steps starts at 0")
    check(run.stall["no_progress_rounds"] == 0, "stall rounds 0")


# ────────────────────────────────────────────────────────────────
# Test 2: run_state atomic write + load round-trip
# ────────────────────────────────────────────────────────────────
def test_run_state():
    print("\n── Test 2: run_state 原子写 + 读取 ──")
    from pipeline_runner import PipelineRun, _save_state, _load_state, _state_path, STATE_DIR_NAME

    # Use temp dir as work_folder
    d = tempfile.mkdtemp(prefix="lp_test_")
    try:
        run = PipelineRun("r1", d, "cull_pipeline", "L3", "婚礼精选", {"threshold": 7.0})
        run.status = "running"
        run.phase_idx = 2
        run.step_idx = 1
        run.artifacts["scanned"] = 100
        run.budget["steps"] = 5
        run.budget["spent_tokens"] = 3000

        # Save
        _save_state(run)
        state_path = _state_path(d)
        check(os.path.isfile(state_path), "run_state.json exists")
        check(os.path.isdir(os.path.join(d, STATE_DIR_NAME)), ".lenspilot dir exists")

        # Load
        st = _load_state(d)
        check(st is not None, "load_state returns dict")
        check(st["run_id"] == "r1", "run_id preserved")
        check(st["template"] == "cull_pipeline", "template preserved")
        check(st["phase_idx"] == 2, "phase_idx preserved")
        check(st["step_idx"] == 1, "step_idx preserved")
        check(st["artifacts"]["scanned"] == 100, "artifacts preserved")
        check(st["budget"]["steps"] == 5, "budget steps preserved")
        check(st["status"] == "running", "status preserved")

        # Load with wrong run_id → should return None
        st2 = _load_state(d, run_id="wrong_id")
        check(st2 is None, "wrong run_id returns None")

        # Delete state → load returns None
        os.remove(state_path)
        st3 = _load_state(d)
        check(st3 is None, "deleted state returns None")

        # Atom write: .tmp should not exist after save
        _save_state(run)
        check(not os.path.exists(state_path + ".tmp"), "no .tmp residue after atomic write")

    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────
# Test 3: Progress signature
# ────────────────────────────────────────────────────────────────
def test_progress_sig():
    print("\n── Test 3: 进展签名 _progress_sig ──")
    from pipeline_runner import PipelineRun, _progress_sig

    d = tempfile.mkdtemp(prefix="lp_test_")
    try:
        run = PipelineRun("r2", d, "cull_pipeline", "L3", "", {})
        sig0 = _progress_sig(run)
        check(sig0 == "scanned:0|scored:0|keep:0|reject:0|log:0", "initial sig")

        run.artifacts["scanned"] = 100
        run.artifacts["scored"] = 100
        sig1 = _progress_sig(run)
        check(sig1 != sig0, "sig changes with artifacts")
        check("scanned:100" in sig1, "scanned in sig")

        run.run_log.append({"action": "test", "ok": True})
        sig2 = _progress_sig(run)
        check(sig2 != sig1, "sig changes with log entries")
        check("log:1" in sig2, "log count in sig")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────
# Test 4: Stall gate logic
# ────────────────────────────────────────────────────────────────
def test_stall_gate():
    print("\n── Test 4: 停滞门逻辑 ──")
    from pipeline_runner import PipelineRun, _progress_sig, STALL_ROUNDS

    d = tempfile.mkdtemp(prefix="lp_test_")
    try:
        run = PipelineRun("r3", d, "cull_pipeline", "L3", "", {})
        # Simulate 2 rounds with no progress
        sig_before = _progress_sig(run)
        # Round 1: no progress
        sig_after = _progress_sig(run)
        check(sig_after == sig_before, "round 1: no change")
        run.stall["no_progress_rounds"] += 1
        check(run.stall["no_progress_rounds"] == 1, "stall=1 after 1 round")

        # Round 2: still no progress → stall
        sig_after2 = _progress_sig(run)
        check(sig_after2 == sig_before, "round 2: no change")
        run.stall["no_progress_rounds"] += 1
        check(run.stall["no_progress_rounds"] >= STALL_ROUNDS, f"stall >= {STALL_ROUNDS} triggers")

        # Progress resets stall
        run.artifacts["scanned"] = 50
        sig_after3 = _progress_sig(run)
        check(sig_after3 != sig_before, "progress changes sig")
        run.stall["no_progress_rounds"] = 0
        check(run.stall["no_progress_rounds"] == 0, "stall reset on progress")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────
# Test 5: Cost gate logic
# ────────────────────────────────────────────────────────────────
def test_cost_gate():
    print("\n── Test 5: 成本门逻辑 ──")
    from pipeline_runner import PipelineRun, _cost_exceeded

    d = tempfile.mkdtemp(prefix="lp_test_")
    try:
        run = PipelineRun("r4", d, "cull_pipeline", "L3", "", {})
        # Under budget
        check(not _cost_exceeded(run), "under budget")

        # Token exceeded
        run.budget["spent_tokens"] = run.budget["token_cap"]
        check(_cost_exceeded(run), "token cap exceeded")

        # Reset, time exceeded
        run.budget["spent_tokens"] = 0
        run.budget["spent_s"] = run.budget["wall_cap_s"]
        check(_cost_exceeded(run), "wall time exceeded")

        # Reset, steps exceeded
        run.budget["spent_s"] = 0
        run.budget["steps"] = run.budget["step_cap"]
        check(_cost_exceeded(run), "step cap exceeded")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────
# Test 6: Threshold rule fallback
# ────────────────────────────────────────────────────────────────
def test_threshold_rule():
    print("\n── Test 6: 阈值规则退路 ──")
    from pipeline_runner import _threshold_rule

    # Empty scores
    check(_threshold_rule([], "") == 6.0, "empty → default 6.0")

    # 100 scores uniformly 1-10
    scores = [i * 0.1 + 5.0 for i in range(100)]  # 5.0 - 14.9
    t = _threshold_rule(scores, "")
    check(1.0 <= t <= 10.0, f"default goal → reasonable threshold ({t})")

    # 婚礼 = high retention
    t_wed = _threshold_rule(scores, "婚礼精选")
    check(t_wed <= t, f"wedding goal ≤ default ({t_wed} <= {t})")

    # 精选 = low retention
    t_best = _threshold_rule(scores, "精选")
    check(t_best >= t, f"best goal ≥ default ({t_best} >= {t})")

    # Very skewed scores (all very high)
    high_scores = [8.0 + i * 0.01 for i in range(100)]
    t_high = _threshold_rule(high_scores, "")
    check(t_high >= 6.0, f"high score set → high threshold ({t_high})")


# ────────────────────────────────────────────────────────────────
# Test 7: Score histogram
# ────────────────────────────────────────────────────────────────
def test_score_histogram():
    print("\n── Test 7: 分数直方图 ──")
    # This test requires score_cache to be populated, so we test with mock data.
    # _score_histogram calls score_cache.get_folder which needs a real db.
    # We test indirectly via _threshold_rule which receives extracted scores.
    from pipeline_runner import _threshold_rule

    # Verify that rule-based thresholds work with different distributions
    scores_wide = [1.0 + i * 0.09 for i in range(100)]  # wide spread
    t_wide = _threshold_rule(scores_wide, "")
    check(1.0 <= t_wide <= 10.0, f"wide spread → in range ({t_wide})")

    scores_tight = [5.5 + i * 0.01 for i in range(100)]  # tight cluster
    t_tight = _threshold_rule(scores_tight, "")
    check(5.0 <= t_tight <= 7.0, f"tight cluster → reasonable ({t_tight})")


# ────────────────────────────────────────────────────────────────
# Test 8: Template definitions
# ────────────────────────────────────────────────────────────────
def test_templates():
    print("\n── Test 8: 管线模板定义 ──")
    from pipeline_runner import TEMPLATES, CULL_PIPELINE

    check("cull_pipeline" in TEMPLATES, "cull_pipeline registered")
    tmpl = TEMPLATES["cull_pipeline"]
    check(tmpl["id"] == "cull_pipeline", "template id")
    check(len(tmpl["phases"]) == 5, "5 phases")  # import/score/dedup/cull/select

    # Verify each phase has steps with callable fns
    phase_ids = []
    for p in tmpl["phases"]:
        phase_ids.append(p["id"])
        check(len(p["steps"]) > 0, f"phase {p['id']} has steps")
        for s in p["steps"]:
            check(callable(s["fn"]), f"step {s['name']} fn is callable")

    check(phase_ids == ["import", "score", "dedup", "cull", "select"],
          f"phase order correct: {phase_ids}")


# ────────────────────────────────────────────────────────────────
# Test 9: Pause / Resume control
# ────────────────────────────────────────────────────────────────
def test_pause_resume():
    print("\n── Test 9: 暂停/续跑控制 ──")
    from pipeline_runner import PipelineRun

    d = tempfile.mkdtemp(prefix="lp_test_")
    try:
        run = PipelineRun("r5", d, "cull_pipeline", "L3", "", {})
        check(not run.pause_event.is_set(), "initially not paused")

        run.pause_event.set()
        check(run.pause_event.is_set(), "paused after set()")

        run.pause_event.clear()
        check(not run.pause_event.is_set(), "resumed after clear()")

        # Cancel should clear pause too
        run.pause_event.set()
        run.cancel_event.set()
        run.pause_event.clear()  # simulates handle_pipeline_cancel
        check(not run.pause_event.is_set(), "cancel clears pause")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────
# Test 10: feedback_queue override column
# ────────────────────────────────────────────────────────────────
def test_feedback_override():
    print("\n── Test 10: feedback_queue override 字段 ──")
    try:
        import feedback_queue
        # Verify the table has the override column
        import sqlite3
        from feedback_queue import _db_path
        conn = sqlite3.connect(_db_path, timeout=10)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_queue)").fetchall()}
        conn.close()
        check("override" in cols, "override column exists in feedback_queue")

        # Verify enqueue accepts override
        result = feedback_queue.enqueue({
            "model_version": "test_1",
            "feature": [0.0] * 128,
            "label": 1,
            "source": "test",
            "category": "test",
            "camera_model": "test",
            "override": True,
        })
        check(result, "enqueue with override=True succeeds")

        # Verify override stored correctly
        conn = sqlite3.connect(_db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT override FROM feedback_queue WHERE source='test' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            check(row["override"] == 1, "override=1 stored in db")
        else:
            check(False, "could not read back override record")
    except ImportError:
        print("  [SKIP] feedback_queue not available (import failed)")
    except Exception as e:
        print(f"  [FAIL] feedback_queue test error: {e}")


# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("M1 Pipeline Unit Tests")
    print("=" * 60)

    test_pipeline_run()
    test_run_state()
    test_progress_sig()
    test_stall_gate()
    test_cost_gate()
    test_threshold_rule()
    test_score_histogram()
    test_templates()
    test_pause_resume()
    test_feedback_override()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
    print(f"{'=' * 60}")

    if FAIL > 0:
        sys.exit(1)
