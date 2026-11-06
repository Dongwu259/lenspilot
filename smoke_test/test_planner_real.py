# -*- coding: utf-8 -*-
"""test_planner_real.py — 用 smoke_agent_key.txt 的真实 LLM 实测 pipeline_planner.generate_template

读 smoke_test/smoke_agent_key.txt（3 行：base_url/api_key/model，# 为注释），
monkeypatch agent_module._load_agent_config 注入配置，对真实模型发 1-2 轮请求，
打印生成的模板草稿 + 独立复核结果。不落盘、不启动管线。

用法: python smoke_test/test_planner_real.py ["自定义目标描述"]
"""
import io
import json
import os
import sys
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


import tempfile  # noqa: E402
CFG_DIR = tempfile.mkdtemp(prefix="planner_real_")

# 与 verify_planner_bridge.py 同款 stub（不起 Electron、不碰评分模型）
_svc = types.ModuleType("service")
_svc._app_config = {"agent": {}}
_svc.send_event = lambda *a, **k: None
_svc.send_log = lambda *a, **k: None
_svc._handlers = {}
_svc.register_handler = lambda cmd, fn: _svc._handlers.__setitem__(cmd, fn)
_svc.subscribe_events = lambda *a, **k: None
_svc.register_cleanup = lambda *a, **k: None
_svc._save_config_to_disk = lambda: None
_svc._get_config_dir = lambda: CFG_DIR
sys.modules["service"] = _svc

_utils = types.ModuleType("utils")
_utils.IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
_utils.RAW_EXTS = set()
_utils.atomic_write_json = lambda p, d: json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sys.modules["utils"] = _utils

_stub("photo_scorer")
_stub("score_cache")
_stub("model_loader")
_stub("copy_card")
_stub("folder_watcher")
_stub("feedback_queue")
_sl = types.ModuleType("skill_loader")
_sl.get_skill_loader = lambda: types.SimpleNamespace(scan=lambda: None, match=lambda p: None)
sys.modules["skill_loader"] = _sl
_mm = types.ModuleType("mcp_client_manager")
_mm.get_mcp_manager = lambda: types.SimpleNamespace(available=False, tools={})
sys.modules["mcp_client_manager"] = _mm
_stub("pipeline_runner")
_stub("pipeline_templates", list_templates=lambda **kw: [])


def load_key():
    lines = []
    with open(os.path.join(ROOT, "smoke_test", "smoke_agent_key.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    if len(lines) < 3:
        raise SystemExit("smoke_agent_key.txt 格式错误：需要 3 行 base_url/api_key/model")
    return lines[0], lines[1], lines[2]


base_url, api_key, model = load_key()
print(f"[real-test] LLM: base_url={base_url} model={model} key={api_key[:6]}...(已隐藏)")

import agent_module  # noqa: E402
import pipeline_planner  # noqa: E402
import step_registry  # noqa: E402

# 注入真实配置（只进内存，不写任何配置文件）
agent_module._load_agent_config = lambda: {
    "base_url": base_url, "api_key": api_key, "model": model,
}

GOAL = sys.argv[1] if len(sys.argv) > 1 else (
    "婚礼跟拍初筛：先AI评分，连拍去重前要我确认，废片过滤后选出约30%做精选，"
    "把精选导出到本地文件夹并加文字水印，最后上传云端建一个带密码的交付相册")

print(f"[real-test] 目标: {GOAL}")
print("[real-test] 调用 generate_template（真实 LLM，可能需要 10-60 秒）...")

result = pipeline_planner.generate_template(GOAL, folder_stats={"photo_count": 800})

print("\n===== 原始返回 =====")
print(json.dumps(result, ensure_ascii=False, indent=2))

tpl = result.get("template")
if not tpl:
    print("\n[FAIL] 未生成模板, errors:", result.get("errors"))
    sys.exit(1)

# 独立复核（不走 planner 内部路径）
errors, warnings = step_registry.validate_template(tpl)
print("\n===== 独立复核 =====")
print("errors:", errors)
print("warnings:", warnings)

# 统计 LLM 用到的新特性
uses = {"params": 0, "condition": 0, "gate": 0}
steps = []
for ph in tpl.get("phases", []):
    for st in ph.get("steps", []):
        steps.append(st.get("name"))
        for k in uses:
            if st.get(k):
                uses[k] += 1
print("\n===== 结构摘要 =====")
print("步骤序列:", " → ".join(steps))
print("新字段使用:", uses)
print("deliver 类步骤:", [s for s in steps if s in (
    "export_keepers", "watermark_keepers", "upload_keepers", "create_cloud_album")])

if errors:
    print("\n[FAIL] 模板校验仍有错误")
    sys.exit(1)
print("\n[PASS] 真实 LLM 生成的模板合法可用")
