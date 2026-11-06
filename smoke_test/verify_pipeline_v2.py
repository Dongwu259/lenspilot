# -*- coding: utf-8 -*-
"""verify_pipeline_v2.py — 0.8.5 工作流 Schema v2 离线验证(不连服务端/不加载模型)

覆盖:
  1. _eval_condition 全 op + 非法 key/op 抛 ValueError
  2. validate_template 的 V14(condition)/V15(gate)/V16(params 键) 正反例
  3. params 合并优先级: 门 edited_params > step.params > run.params
  4. 声明式 gate 三路径: approve 执行 / reject 跳过 / edit 改参执行
  5. condition_false 的步骤被跳过且不触发停滞门(连续全 skip 不 error)
  6. _progress_sig 泛化: 任意 artifact 变化都改变签名
  7. export_keepers/watermark_keepers 用假照片跑通;
     upload_keepers/create_cloud_album 无凭证时返回 error dict 不崩
  8. 附带契约: handle_pipeline_start(params, _from_agent=False) 签名、
     resolve_template 保留 condition/gate、resume 模板快照对比

用法: python smoke_test/verify_pipeline_v2.py   (仓库根目录下运行)
"""
import importlib
import inspect
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

EVENTS = []  # [(event_name, data)] 捕获 pipeline 事件


def _make_service_stub():
    svc = types.ModuleType("service")
    svc._app_config = {}
    svc.send_event = lambda name, data=None: EVENTS.append((name, data))
    svc.send_log = lambda *a, **k: None
    svc.register_handler = lambda *a, **k: None
    svc.subscribe_events = lambda *a, **k: None
    svc.register_cleanup = lambda *a, **k: None
    svc._save_config_to_disk = lambda: None
    svc._get_config_dir = lambda: tempfile.gettempdir()
    return svc


def _make_utils_stub():
    utils = types.ModuleType("utils")
    utils.IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
    utils.RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng"}
    return utils


# ── 基础 stub(管线骨架测试用轻 photo_scorer; 用例7会换成真模块) ──
for _n in ("service", "utils", "photo_scorer", "score_cache",
           "pipeline_runner", "step_registry", "pipeline_tool_steps"):
    sys.modules.pop(_n, None)
sys.modules["service"] = _make_service_stub()
sys.modules["utils"] = _make_utils_stub()
sys.modules["photo_scorer"] = types.ModuleType("photo_scorer")
sys.modules["score_cache"] = types.ModuleType("score_cache")

pr = importlib.import_module("pipeline_runner")
sr = importlib.import_module("step_registry")
pts = importlib.import_module("pipeline_tool_steps")


def _make_run(params=None, goal="", template="cull_pipeline", work_folder=None):
    """构造离线 PipelineRun(autonomy=L1: 跳过管线末阶段门)。"""
    wf = work_folder or tempfile.mkdtemp(prefix="verify_v2_run_")
    return pr.PipelineRun("rid-test", wf, template, "L1", goal, params or {})


def _events_named(name):
    return [d for n, d in EVENTS if n == name]


# ═══════════════════════════════════════════════════════════════════════
# 1. _eval_condition 全 op + 非法 key/op
# ═══════════════════════════════════════════════════════════════════════
def test_eval_condition():
    run = _make_run(params={"name": "abc"}, goal="婚礼跟拍")
    run.artifacts["dedup_groups"] = 3
    run.artifacts["tags"] = ["a", "b"]
    run.step_params = dict(run.params)

    # gt/ge/lt/le/eq/ne
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "gt", "value": 0}) is True
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "gt", "value": 3}) is False
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "ge", "value": 3}) is True
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "lt", "value": 5}) is True
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "le", "value": 3}) is True
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "eq", "value": 3}) is True
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "ne", "value": 3}) is False
    # exists/not_exists
    assert pr._eval_condition(run, {"key": "artifacts.dedup_groups", "op": "exists"}) is True
    assert pr._eval_condition(run, {"key": "artifacts.missing", "op": "exists"}) is False
    assert pr._eval_condition(run, {"key": "artifacts.missing", "op": "not_exists"}) is True
    # contains: 列表 / 字符串 / goal
    assert pr._eval_condition(run, {"key": "artifacts.tags", "op": "contains", "value": "a"}) is True
    assert pr._eval_condition(run, {"key": "artifacts.tags", "op": "contains", "value": "z"}) is False
    assert pr._eval_condition(run, {"key": "params.name", "op": "contains", "value": "b"}) is True
    assert pr._eval_condition(run, {"key": "goal", "op": "contains", "value": "婚礼"}) is True
    assert pr._eval_condition(run, {"key": "goal", "op": "contains", "value": "毕业"}) is False
    # params.* 读合并后的 step_params
    assert pr._eval_condition(run, {"key": "params.name", "op": "eq", "value": "abc"}) is True
    assert pr._eval_condition(run, {"key": "params.name", "op": "exists"}) is True
    # 值缺失/类型不符 → False 而非异常
    assert pr._eval_condition(run, {"key": "artifacts.missing", "op": "gt", "value": 1}) is False

    # 非法 key/op 抛 ValueError
    bad_cases = [
        {"key": "run.x", "op": "eq", "value": 1},          # 非法前缀
        {"key": "artifacts.x", "op": "mod", "value": 1},   # 非法 op
        {"key": "", "op": "eq", "value": 1},               # 空 key
        {"key": "artifacts.", "op": "exists"},             # 缺子键
        "not_a_dict",                                      # 非 dict
    ]
    for bad in bad_cases:
        try:
            pr._eval_condition(run, bad)
            raise AssertionError(f"非法 condition 未抛 ValueError: {bad!r}")
        except ValueError:
            pass
    print("[1] OK: _eval_condition 全 op 正确, 非法 key/op 抛 ValueError")


# ═══════════════════════════════════════════════════════════════════════
# 2. validate_template V14/V15/V16 正反例
# ═══════════════════════════════════════════════════════════════════════
def _tpl(step, extra_steps=None):
    steps = (extra_steps or []) + [step]
    return {"id": "t_v", "title": "t",
            "phases": [{"id": "p1", "title": "p1", "steps": steps}]}


def test_validate_v14_v15_v16():
    # V14 正: 合法 condition
    errs, _ = sr.validate_template(_tpl({
        "name": "scan",
        "condition": {"key": "artifacts.dedup_groups", "op": "gt", "value": 0}}))
    assert not errs, f"V14 正例不应报错: {errs}"
    # V14 反: 非法 key 前缀 / 非法 op / 比较 op 缺 value / condition 非 dict
    for bad_cond in ({"key": "run.x", "op": "eq", "value": 1},
                     {"key": "artifacts.x", "op": "mod", "value": 1},
                     {"key": "artifacts.x", "op": "gt"},
                     "bad"):
        errs, _ = sr.validate_template(_tpl({"name": "scan", "condition": bad_cond}))
        assert errs, f"V14 反例应报错: {bad_cond!r}"

    # V15 正: 合法 gate
    errs, _ = sr.validate_template(_tpl({
        "name": "scan", "gate": {"type": "action", "message": "m", "timeout": 600}}))
    assert not errs, f"V15 正例不应报错: {errs}"
    # V15 反: 非法 type / timeout 非正数 / gate 非 dict
    for bad_gate in ({"type": "confirm"}, {"timeout": -5}, {"timeout": "abc"}, "bad"):
        errs, _ = sr.validate_template(_tpl({"name": "scan", "gate": bad_gate}))
        assert errs, f"V15 反例应报错: {bad_gate!r}"

    # V16 正: params 键在白名单内(score 的 user_configurable_params 含 threshold)
    errs, warns = sr.validate_template(_tpl(
        {"name": "score", "params": {"threshold": 7.0}},
        extra_steps=[{"name": "scan"}]))
    assert not errs, f"V16 正例不应报错: {errs}"
    assert not any("threshold" in w for w in warns), f"V16 正例不应告警: {warns}"
    # V16 反: params 键不在白名单 → warning(非 error)
    errs, warns = sr.validate_template(_tpl(
        {"name": "score", "params": {"nope": 1}},
        extra_steps=[{"name": "scan"}]))
    assert not errs, f"V16 反例只应告警不应报错: {errs}"
    assert any("nope" in w for w in warns), f"V16 反例应告警: {warns}"
    print("[2] OK: validate_template V14/V15/V16 正反例符合预期")


# ═══════════════════════════════════════════════════════════════════════
# 3. params 合并优先级 + 4. 声明式 gate 三路径
# ═══════════════════════════════════════════════════════════════════════
def _run_tpl(run, template_dict, gate_decision=None):
    """注册测试模板并同步跑完主循环; gate_decision 非 None 时 monkeypatch _await_gate。"""
    pr.TEMPLATES[template_dict["id"]] = template_dict
    EVENTS.clear()
    orig_gate = pr._await_gate
    gate_calls = []
    if gate_decision is not None:
        def fake_gate(run_, gate_type, summary, payload=None, timeout=None):
            gate_calls.append({"type": gate_type, "summary": summary,
                               "payload": payload, "timeout": timeout})
            return gate_decision
        pr._await_gate = fake_gate
    try:
        pr._run_pipeline(run)
    finally:
        pr._await_gate = orig_gate
    return gate_calls


def test_params_merge_and_gate_paths():
    # ── _step_param 直接行为(无 step_params 回退 run.params) ──
    r0 = _make_run(params={"a": 1})
    assert pr._step_param(r0, "a") == 1
    assert pr._step_param(r0, "b", 7) == 7
    r0.step_params = {"a": 2}
    assert pr._step_param(r0, "a") == 2

    # ── gate edit: edited_params > step.params > run.params ──
    box = {}
    def fn_rec(run):
        box["step_params"] = dict(run.step_params)
        run.artifacts["done"] = True
        return {"ok": True}

    tpl_edit = {"id": "t_edit", "title": "t", "phases": [
        {"id": "p", "title": "p", "steps": [
            {"name": "s1", "fn": fn_rec,
             "params": {"p1": "step_val"},
             "gate": {"type": "action", "message": "确认?", "timeout": 5}},
        ]}]}
    run = _make_run(params={"p1": "run_val", "p2": "run_only"}, template="t_edit")
    calls = _run_tpl(run, tpl_edit,
                     gate_decision={"approved": True, "edited_params": {"p1": "edited_val"}})
    assert run.status == "done", f"edit 路径应跑完: {run.status}"
    assert box["step_params"]["p1"] == "edited_val", \
        f"edited_params 应最优先: {box['step_params']}"
    assert box["step_params"]["p2"] == "run_only", "run.params 应继承进 step_params"
    assert len(calls) == 1 and calls[0]["type"] == "action"
    assert calls[0]["timeout"] == 5, "gate.timeout 应覆盖默认"
    assert calls[0]["payload"]["step"] == "s1", "gate payload 应带 step 名"
    assert calls[0]["payload"]["message"] == "确认?", "gate payload 应带 message"

    # ── 无门: step.params > run.params ──
    box.clear()
    tpl_plain = {"id": "t_plain", "title": "t", "phases": [
        {"id": "p", "title": "p", "steps": [
            {"name": "s1", "fn": fn_rec, "params": {"p1": "step_val"}}]}]}
    run = _make_run(params={"p1": "run_val"}, template="t_plain")
    _run_tpl(run, tpl_plain)
    assert box["step_params"]["p1"] == "step_val", f"step.params 应覆盖 run.params: {box}"

    # ── gate approve: 正常执行, 无 step_skip ──
    box.clear()
    run = _make_run(params={"p1": "run_val"}, template="t_edit")
    _run_tpl(run, tpl_edit, gate_decision={"approved": True})
    assert box.get("step_params", {}).get("p1") == "step_val", "approve 应原样执行"
    assert not _events_named("pipeline_step_skip"), "approve 不应有 step_skip"

    # ── gate reject: 跳过, step_skip(reason=gate_rejected) ──
    box.clear()
    run = _make_run(params={"p1": "run_val"}, template="t_edit")
    _run_tpl(run, tpl_edit, gate_decision={"approved": False})
    assert "step_params" not in box, "reject 时 step fn 不应执行"
    skips = _events_named("pipeline_step_skip")
    assert len(skips) == 1 and skips[0]["reason"] == "gate_rejected", f"应发 gate_rejected: {skips}"
    assert run.status == "done", "reject 跳过不应中断管线"
    print("[3/4] OK: 合并优先级 edited>step>run, gate approve/reject/edit 三路径正确")


# ═══════════════════════════════════════════════════════════════════════
# 5. condition_false 跳过且不触发停滞门
# ═══════════════════════════════════════════════════════════════════════
def test_condition_skip_no_stall():
    counter = {"n": 0}
    def fn_cnt(run):
        counter["n"] += 1
        return {"ok": True}

    def fn_real(run):
        counter["n"] += 1
        run.artifacts["real_done"] = True   # 真步骤产 artifact(正常步骤都有产物)
        return {"ok": True}

    # 4 步全部 condition 不满足(连续全 skip 超过 STALL_ROUNDS=2, 不应被停滞门误杀)
    steps = [{"name": f"s{i}", "fn": fn_cnt,
              "condition": {"key": "artifacts.missing", "op": "exists"}}
             for i in range(4)]
    # 末尾加一步 condition 为真的, 验证真条件正常执行
    steps.append({"name": "s_real", "fn": fn_real,
                  "condition": {"key": "goal", "op": "contains", "value": "婚"}})
    tpl = {"id": "t_skip", "title": "t",
           "phases": [{"id": "p", "title": "p", "steps": steps}]}
    run = _make_run(goal="婚礼", template="t_skip")
    _run_tpl(run, tpl)

    assert counter["n"] == 1, f"只有 condition 为真的步应执行: {counter}"
    skips = _events_named("pipeline_step_skip")
    assert len(skips) == 4, f"应有 4 个 step_skip: {len(skips)}"
    assert all(s["reason"] == "condition_false" for s in skips)
    assert not _events_named("pipeline_error"), "连续 skip 不应触发停滞门"
    assert run.status == "done"
    assert run.stall["no_progress_rounds"] == 0, "skip 不应累计停滞轮数"
    print("[5] OK: condition_false 步骤跳过, 不触发停滞门/err_repeat")


# ═══════════════════════════════════════════════════════════════════════
# 6. _progress_sig 泛化
# ═══════════════════════════════════════════════════════════════════════
def test_progress_sig_generalized():
    run = _make_run()
    s1 = pr._progress_sig(run)
    run.artifacts["exported_count"] = 5        # 非传统四 key 的 artifact
    s2 = pr._progress_sig(run)
    assert s1 != s2, "任意 artifact 变化都应改变签名"
    run.artifacts["cloud_photo_ids"] = ["a", "b"]
    s3 = pr._progress_sig(run)
    assert s3 != s2
    run.artifacts["cloud_photo_ids"] = ["a"]   # list 长度变化也反映(不撑爆签名)
    s4 = pr._progress_sig(run)
    assert s4 != s3
    run.artifacts["exported_count"] = 6
    assert pr._progress_sig(run) != s4
    print("[6] OK: _progress_sig 对任意 artifact 变化敏感")


# ═══════════════════════════════════════════════════════════════════════
# 7. 交付链步骤(真 photo_scorer + 假照片; 云端步骤无凭证优雅报错)
# ═══════════════════════════════════════════════════════════════════════
def _write_jpeg(path, color):
    from PIL import Image
    Image.new("RGB", (32, 32), color).save(path, "JPEG")


def test_tool_steps():
    tmp = tempfile.mkdtemp(prefix="verify_v2_tools_")
    imgs = []
    for i, c in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        p = os.path.join(tmp, f"k{i}.jpg")
        _write_jpeg(p, c)
        imgs.append(p)

    # ── 换成真 photo_scorer(重依赖 model_loader stub 掉, 与 verify_p0_fixes 同法) ──
    for name in ("photo_scorer", "model_loader"):
        sys.modules.pop(name, None)
    ml = types.ModuleType("model_loader")
    for name in ["load_model", "predict_score", "handle_get_cache_info",
                 "handle_clear_raw_cache", "_load_image_rgb", "_infer",
                 "make_thumbnail", "make_heatmap", "get_backend_pref_info",
                 "_read_backend_pref", "_write_backend_pref", "_cuda_available",
                 "compute_blur_score", "extract_deep_feature",
                 "extract_context_feature", "detect_eyes", "load_eye_model"]:
        setattr(ml, name, lambda *a, **k: None)
    ml.model_version = lambda: "test_mv"
    ml.RAW_EXTS = set()
    sys.modules["model_loader"] = ml
    importlib.import_module("photo_scorer")  # 真模块入 sys.modules, 供 tool steps 延迟 import

    # ── export_keepers ──
    run = _make_run(work_folder=tmp)
    run.artifacts["keep_paths"] = [{"path": p, "score": 8} for p in imgs]
    res = pts.export_keepers(run)
    assert res.get("ok") and res["exported"] == 3, f"导出应成功 3 张: {res}"
    out_dir = run.artifacts["export_dir"]
    assert out_dir.endswith("_精选导出") and os.path.isdir(out_dir)
    assert len(os.listdir(out_dir)) == 3, "导出目录应有 3 个文件"
    assert run.artifacts["exported_count"] == 3

    # ── watermark_keepers ──
    res = pts.watermark_keepers(run)
    assert res.get("ok") and res["watermarked"] == 3, f"水印应成功 3 张: {res}"
    wm_dir = os.path.join(tmp, "_watermarked")
    assert os.path.isdir(wm_dir) and len(os.listdir(wm_dir)) == 3
    assert run.artifacts["watermarked_count"] == 3

    # ── upload_keepers 无凭证: 优雅 error, 不崩 ──
    run2 = _make_run(work_folder=tmp)
    run2.artifacts["keep_paths"] = imgs  # str 元素也应兼容
    res = pts.upload_keepers(run2)
    assert res.get("error") == "not_logged_in", f"未登录应返回 not_logged_in: {res}"
    assert run2.artifacts["upload_error"] == "not_logged_in"

    # ── create_cloud_album: 未上传 / 未登录 都优雅 error ──
    res = pts.create_cloud_album(run2)
    assert res.get("error") == "no_uploaded_photos", f"无上传产物应报错: {res}"
    run2.artifacts["cloud_photo_ids"] = ["x1"]
    res = pts.create_cloud_album(run2)
    assert res.get("error") == "not_logged_in", f"未登录应报错: {res}"
    assert run2.artifacts["album_error"] == "not_logged_in"
    print("[7] OK: export/watermark 假照片跑通, upload/album 无凭证优雅 error 不崩")


# ═══════════════════════════════════════════════════════════════════════
# 8. 附带契约: 签名 / resolve 保留字段 / resume 模板快照
# ═══════════════════════════════════════════════════════════════════════
def test_contracts():
    # handle_pipeline_start(params, _from_agent=False) 硬契约
    sig = inspect.signature(pr.handle_pipeline_start)
    assert list(sig.parameters) == ["params", "_from_agent"], f"签名不符: {sig}"
    assert sig.parameters["_from_agent"].default is False

    # resolve_template 保留 condition/gate/params
    tpl = {"id": "t_r", "title": "t", "phases": [{"id": "p", "title": "p", "steps": [
        {"name": "scan", "params": {"x": 1},
         "condition": {"key": "goal", "op": "contains", "value": "婚"},
         "gate": {"type": "action", "message": "m", "timeout": 30}},
    ]}]}
    st = sr.resolve_template(tpl)["phases"][0]["steps"][0]
    assert st["condition"]["op"] == "contains"
    assert st["gate"]["timeout"] == 30 and st["gate"]["message"] == "m"
    assert st["params"] == {"x": 1}
    assert callable(st["fn"])

    # resume 模板快照: _save_state 存 phases; 模板变更 → template_changed 且不清 state
    tmp = tempfile.mkdtemp(prefix="verify_v2_resume_")
    fn = lambda run: {"ok": True}
    pr.TEMPLATES["t_res"] = {"id": "t_res", "title": "t", "phases": [
        {"id": "p", "title": "p", "steps": [{"name": "s1", "fn": fn}]}]}
    run = _make_run(template="t_res", work_folder=tmp)
    pr._save_state(run)
    with open(pr._state_path(tmp), "r", encoding="utf-8") as f:
        st = json.load(f)
    assert st["phases"] == [["s1"]], f"state 应含 phases 快照: {st.get('phases')}"
    pr.TEMPLATES["t_res"]["phases"][0]["steps"].append({"name": "s2", "fn": fn})
    res = pr._resume_from_state({"work_folder": tmp})
    assert res.get("error") == "template_changed", f"模板变更应拒绝续跑: {res}"
    assert "重新开始" in res.get("detail", "")
    assert os.path.isfile(pr._state_path(tmp)), "拒绝续跑不应清除 state"
    print("[8] OK: 签名契约 / resolve 保留新字段 / resume 模板快照对比")


if __name__ == "__main__":
    test_eval_condition()
    test_validate_v14_v15_v16()
    test_params_merge_and_gate_paths()
    test_condition_skip_no_stall()
    test_progress_sig_generalized()
    test_tool_steps()
    test_contracts()
    print("全部 Schema v2 验证通过")
