# -*- coding: utf-8 -*-
"""verify_planner_bridge.py — 0.8.5 pipeline_planner + agent 桥接的离线验证

不起 Electron、无真实 LLM/网络：
  项1: planner 一次成功（假 client 直接返回合法模板 JSON）→ template 返回、
       validate_template 被调用、LLM 只调 1 次、不落盘
  项2: 自我修复路径（第一次输出非法 JSON，第二次合法）→ 修复成功，LLM 调 2 次
  项3: 两次都校验失败（未知步骤）→ template=None 且 errors 非空，LLM 调 2 次
  项4: pipeline_save_template 确认门：批准→保存；拒绝→denied；校验失败→不确认不保存
  项5: pipeline_start_run 确认门 + _from_agent=True 传递（捕获 pipeline_runner 入参）
  项6: handle_agent_run 的 pipeline_busy 互斥（pipeline_runner 有活跃 run 时拒绝）

用法: python smoke_test/verify_planner_bridge.py   (仓库根目录下运行)
"""
import importlib
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 所有需要 stub 的依赖模块名（每个用例开始前都会重建，保证用例间隔离）
STUB_NAMES = ["service", "photo_scorer", "score_cache", "utils", "model_loader",
              "feedback_queue", "skill_loader", "mcp_client_manager",
              "copy_card", "folder_watcher", "pipeline_runner", "pipeline_templates"]
TARGET_NAMES = ["pipeline_planner", "agent_module", "step_registry"]

# ── 通用测试素材 ──
VALID_TEMPLATE = {
    "id": "gen_test",
    "title": "AI 生成测试",
    "description": "测试用",
    "phases": [
        {"id": "import", "title": "导入编目", "steps": [{"name": "scan"}]},
        {"id": "score", "title": "AI 评分", "steps": [{"name": "score"}, {"name": "threshold"}]},
        {"id": "select", "title": "出 keeper", "steps": [{"name": "select"}]},
    ],
}
VALID_JSON = json.dumps(VALID_TEMPLATE, ensure_ascii=False)

INVALID_STEP_TEMPLATE = dict(VALID_TEMPLATE, id="gen_bad")
INVALID_STEP_JSON = json.dumps({
    "id": "gen_bad", "title": "坏模板",
    "phases": [{"id": "p1", "title": "阶段1", "steps": [{"name": "fly_to_moon"}]}],
}, ensure_ascii=False)


def _reset_modules():
    """清掉目标模块与 stub 模块的缓存，保证每个用例拿到干净状态。"""
    for name in STUB_NAMES + TARGET_NAMES:
        sys.modules.pop(name, None)


def _make_service_stub(cfg_dir):
    svc = types.ModuleType("service")
    svc._app_config = {"agent": {"model": "test-model", "base_url": "http://x", "api_key": "k"}}
    svc.send_event = lambda *a, **k: None
    svc.send_log = lambda *a, **k: None
    svc._handlers = {}
    svc.register_handler = lambda cmd, fn: svc._handlers.__setitem__(cmd, fn)
    svc.subscribe_events = lambda *a, **k: None
    svc.register_cleanup = lambda *a, **k: None
    svc._save_config_to_disk = lambda: None
    svc._get_config_dir = lambda: cfg_dir
    return svc


def _make_utils_stub():
    utils = types.ModuleType("utils")
    utils.IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
    utils.RAW_EXTS = set()

    def atomic_write_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    utils.atomic_write_json = atomic_write_json
    return utils


def _install_base_stubs(cfg_dir):
    sys.modules["service"] = _make_service_stub(cfg_dir)
    sys.modules["utils"] = _make_utils_stub()
    sys.modules["photo_scorer"] = types.ModuleType("photo_scorer")
    sys.modules["score_cache"] = types.ModuleType("score_cache")
    sys.modules["model_loader"] = types.ModuleType("model_loader")
    sys.modules["copy_card"] = types.ModuleType("copy_card")
    sys.modules["folder_watcher"] = types.ModuleType("folder_watcher")
    sl = types.ModuleType("skill_loader")
    sl.get_skill_loader = lambda: types.SimpleNamespace(scan=lambda: None, match=lambda p: None)
    sys.modules["skill_loader"] = sl
    mm = types.ModuleType("mcp_client_manager")
    mm.get_mcp_manager = lambda: types.SimpleNamespace(available=False, tools={})
    sys.modules["mcp_client_manager"] = mm


class _FakeClient:
    """假 OpenAI client：按队列依次吐出 content，记录调用次数。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        content = self._outputs.pop(0) if self._outputs else ""
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20,
                                      total_tokens=30, prompt_tokens_details=None)
        msg = types.SimpleNamespace(content=content, reasoning_content=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)


def _import_planner_with_fake_client(outputs):
    """导入 agent_module + pipeline_planner，并把 _make_client 换成假 client。返回 (planner, am, client)。"""
    am = importlib.import_module("agent_module")
    client = _FakeClient(outputs)
    am._make_client = lambda cfg: (client, None)
    planner = importlib.import_module("pipeline_planner")
    return planner, am, client


def _count_validate():
    """包装 step_registry.validate_template 计数。返回计数器 dict。"""
    import step_registry
    counter = {"n": 0}
    real = step_registry.validate_template

    def wrapper(tpl, *a, **k):
        counter["n"] += 1
        return real(tpl, *a, **k)
    step_registry.validate_template = wrapper
    return counter


def _assert_no_template_on_disk(cfg_dir, tid):
    p = os.path.join(cfg_dir, "pipeline_templates", tid + ".json")
    assert not os.path.exists(p), f"planner 不应落盘，但发现了 {p}"


# ═══════════════════════════════════════════════════════════════════════
# 项1: planner 一次成功
# ═══════════════════════════════════════════════════════════════════════
def test_item1_planner_happy_path():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner1_")
    _install_base_stubs(cfg_dir)

    planner, am, client = _import_planner_with_fake_client([VALID_JSON])
    vcount = _count_validate()

    resp = planner.generate_template("婚礼初筛")

    assert resp.get("template"), f"应返回模板，实际: {resp}"
    assert resp["template"]["id"] == "gen_test"
    assert resp["errors"] == [], f"errors 应为空: {resp['errors']}"
    assert client.calls == 1, f"合法输出不应触发修复轮，实际 LLM 调用 {client.calls} 次"
    assert vcount["n"] == 1, f"validate_template 应被调用 1 次，实际 {vcount['n']}"
    _assert_no_template_on_disk(cfg_dir, "gen_test")

    # IPC 注册 + llm_not_configured 路径
    svc = sys.modules["service"]
    planner.register_handlers()
    assert "generate_pipeline_template" in svc._handlers, "IPC 命令 generate_pipeline_template 未注册"
    am._make_client = lambda cfg: (None, "未配置")
    resp2 = planner.generate_template("婚礼初筛")
    assert resp2.get("error") == "llm_not_configured", f"未配置时应报 llm_not_configured: {resp2}"
    print("[项1] OK: planner 一次成功，validate 调用 1 次，不落盘，llm_not_configured 正确")


# ═══════════════════════════════════════════════════════════════════════
# 项2: 先非法后合法 → 自我修复
# ═══════════════════════════════════════════════════════════════════════
def test_item2_planner_self_repair():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner2_")
    _install_base_stubs(cfg_dir)

    planner, am, client = _import_planner_with_fake_client(
        ["```json\n这不是合法JSON\n```", VALID_JSON])
    vcount = _count_validate()

    resp = planner.generate_template("婚礼初筛")

    assert resp.get("template"), f"修复后应返回模板，实际: {resp}"
    assert resp["errors"] == []
    assert client.calls == 2, f"修复路径应调 LLM 2 次，实际 {client.calls}"
    # 第一轮输出不是 JSON（validate 不跑），第二轮合法（validate 跑 1 次）
    assert vcount["n"] == 1, f"validate_template 应被调用 1 次，实际 {vcount['n']}"
    _assert_no_template_on_disk(cfg_dir, "gen_test")
    print("[项2] OK: 非法 JSON → 自我修复一轮 → 成功，LLM 共 2 次调用")


# ═══════════════════════════════════════════════════════════════════════
# 项3: 两次都校验失败 → errors 返回
# ═══════════════════════════════════════════════════════════════════════
def test_item3_planner_repair_still_fails():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner3_")
    _install_base_stubs(cfg_dir)

    planner, am, client = _import_planner_with_fake_client(
        [INVALID_STEP_JSON, INVALID_STEP_JSON])
    vcount = _count_validate()

    resp = planner.generate_template("婚礼初筛")

    assert resp.get("template") is None, f"两次都失败时 template 应为 None: {resp}"
    assert resp["errors"], "errors 应非空"
    assert any("fly_to_moon" in e for e in resp["errors"]), f"errors 应含未知步骤信息: {resp['errors']}"
    assert client.calls == 2, f"应只自我修复一次（共 2 次调用），实际 {client.calls}"
    assert vcount["n"] == 2, f"validate_template 应被调用 2 次，实际 {vcount['n']}"
    _assert_no_template_on_disk(cfg_dir, "gen_bad")
    print("[项3] OK: 修复后仍有错 → template=None + errors，未第三次调用 LLM")


# ═══════════════════════════════════════════════════════════════════════
# 项4: pipeline_save_template 确认门（批准/拒绝/校验失败）
# ═══════════════════════════════════════════════════════════════════════
def test_item4_save_template_gate():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner4_")
    _install_base_stubs(cfg_dir)

    # 假 pipeline_templates：捕获 save_template 调用，不写盘
    saved = []
    pt = types.ModuleType("pipeline_templates")
    pt.save_template = lambda tpl: saved.append(tpl) or {"ok": True}
    sys.modules["pipeline_templates"] = pt

    am = importlib.import_module("agent_module")
    confirms = []
    am._request_confirmation = lambda *a, **k: confirms.append(a) or _gate["answer"]
    fn = am.TOOL_REGISTRY["pipeline_save_template"]["fn"]
    session = types.SimpleNamespace(auto_approve=False, work_folder=cfg_dir,
                                    prompt="任务", cloud_token="")

    # (a) 批准 → 保存
    _gate = {"answer": True}
    r = fn(session, {"template": dict(VALID_TEMPLATE)})
    assert r.get("ok"), f"批准路径应保存成功: {r}"
    assert len(confirms) == 1, "应触发一次确认门"
    assert len(saved) == 1, "save_template 应被调用一次"

    # (b) 拒绝 → denied_by_user，不保存
    _gate["answer"] = False
    r = fn(session, {"template": dict(VALID_TEMPLATE)})
    assert r.get("error") == "denied_by_user", f"拒绝路径应 denied_by_user: {r}"
    assert len(saved) == 1, "拒绝时不应再调 save_template"

    # (c) 校验失败 → 直接返回，不触发确认、不保存
    bad = json.loads(INVALID_STEP_JSON)
    n_confirm = len(confirms)
    r = fn(session, {"template": bad})
    assert r.get("error") == "validation_failed", f"校验失败应 validation_failed: {r}"
    assert r.get("errors"), "validation_failed 应带 errors"
    assert len(confirms) == n_confirm, "校验失败时不应触发确认门"
    assert len(saved) == 1, "校验失败时不应调 save_template"
    print("[项4] OK: pipeline_save_template 确认门批准/拒绝/校验失败三路径正确")


# ═══════════════════════════════════════════════════════════════════════
# 项5: pipeline_start_run 确认门 + _from_agent=True
# ═══════════════════════════════════════════════════════════════════════
def test_item5_start_run_from_agent():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner5_")
    _install_base_stubs(cfg_dir)

    # 假 pipeline_runner：捕获 handle_pipeline_start 的入参
    captured = {}
    pr = types.ModuleType("pipeline_runner")

    def fake_start(params, _from_agent=False):
        captured["params"] = params
        captured["_from_agent"] = _from_agent
        return {"ok": True, "run_id": "r1"}
    pr.handle_pipeline_start = fake_start
    sys.modules["pipeline_runner"] = pr

    am = importlib.import_module("agent_module")
    confirms = []
    am._request_confirmation = lambda *a, **k: confirms.append(a) or _gate["answer"]
    fn = am.TOOL_REGISTRY["pipeline_start_run"]["fn"]
    session = types.SimpleNamespace(auto_approve=False, work_folder=cfg_dir,
                                    prompt="婚礼初筛", cloud_token="tok123")

    # (a) 批准 → 启动，_from_agent=True 传递
    _gate = {"answer": True}
    r = fn(session, {"folder": cfg_dir, "template": "cull_pipeline",
                     "goal": "婚礼初筛", "pipeline_params": {"threshold": 7.0}})
    assert r.get("ok"), f"批准路径应启动成功: {r}"
    assert len(confirms) == 1, "应触发一次确认门"
    assert captured.get("_from_agent") is True, \
        f"硬契约：_from_agent=True 必须传给 handle_pipeline_start，实际 {captured.get('_from_agent')!r}"
    assert captured["params"]["work_folder"] == cfg_dir
    assert captured["params"]["template"] == "cull_pipeline"
    assert captured["params"]["goal"] == "婚礼初筛"
    assert captured["params"]["pipeline_params"] == {"threshold": 7.0}
    assert captured["params"]["cloud_token"] == "tok123"

    # (b) 拒绝 → denied_by_user，不启动
    captured.clear()
    _gate["answer"] = False
    r = fn(session, {"folder": cfg_dir, "template": "cull_pipeline"})
    assert r.get("error") == "denied_by_user", f"拒绝路径应 denied_by_user: {r}"
    assert not captured, "拒绝时不应调 handle_pipeline_start"
    print("[项5] OK: pipeline_start_run 确认门正确，_from_agent=True 已传递")


# ═══════════════════════════════════════════════════════════════════════
# 项6: handle_agent_run 的 pipeline_busy 互斥
# ═══════════════════════════════════════════════════════════════════════
def test_item6_agent_run_pipeline_busy():
    _reset_modules()
    cfg_dir = tempfile.mkdtemp(prefix="verify_planner6_")
    _install_base_stubs(cfg_dir)

    # 假 pipeline_runner：_get_active_run 可控
    state = {"run": object()}
    pr = types.ModuleType("pipeline_runner")
    pr._get_active_run = lambda: state["run"]
    sys.modules["pipeline_runner"] = pr

    am = importlib.import_module("agent_module")
    am._run_agent = lambda session: None  # 不真的跑 agent 循环

    # (a) 管线活跃 → pipeline_busy
    r = am.handle_agent_run({"prompt": "处理一下"})
    assert r.get("error") == "pipeline_busy", f"管线活跃时应拒绝启动 agent: {r}"
    assert am._get_active() is None, "被拒绝时不应创建 session"

    # (b) 管线空闲 → 放行
    state["run"] = None
    r = am.handle_agent_run({"prompt": "处理一下"})
    assert r.get("ok"), f"管线空闲时应放行: {r}"
    assert r.get("session_id"), "放行后应返回 session_id"
    print("[项6] OK: handle_agent_run 的 pipeline_busy 互斥两路径正确")


def main():
    tests = [
        test_item1_planner_happy_path,
        test_item2_planner_self_repair,
        test_item3_planner_repair_still_fails,
        test_item4_save_template_gate,
        test_item5_start_run_from_agent,
        test_item6_agent_run_pipeline_busy,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            import traceback
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} 项失败")
        sys.exit(1)
    print(f"\n全部 {len(tests)} 项通过")


if __name__ == "__main__":
    main()
