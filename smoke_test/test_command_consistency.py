# -*- coding: utf-8 -*-
"""test_command_consistency.py — 注册表/白名单一致性防漂移

本会话抓到三个同类漏配后上的机制性防线（同一类 bug 不允许咬第四次）：
  1. 注册表签名：所有 @command handler 必须是单参 fn(params)——
     教训：apply_edits 装饰器误贴在 (path, edits) 签名的函数上，IPC 必 TypeError。
  2. 白名单覆盖：前端 src/js 全部字面量 sendCommand('name') ⊆ main.js
     ALLOWED_COMMANDS——教训：auto_tone 三命令 / edit_journal_list 漏白名单，
     UI 按钮打不通后端。
  3. 白名单存在性：ALLOWED_COMMANDS 每个名字必须在注册表或后端
     register_handler 字面量中真实存在——防 typo 死条目静默膨胀。
纯静态 + 导入级检查，绝不调用任何 handler（有破坏性命令）。
"""
import glob
import inspect
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# ── 打桩 service（导入所有后端模块前必须就位） ──
stub = types.ModuleType("service")
stub.send_event = lambda *a, **k: None
stub.send_log = lambda *a, **k: None
stub.register_handler = lambda *a, **k: None
sys.modules["service"] = stub

import command_registry as cr  # noqa: E402

# ── 1) 注册表签名：导入全部 @command 模块，逐条检查 fn(params) ──
cmd_mods = sorted(os.path.basename(f)[:-3]
                  for f in glob.glob(os.path.join(BACKEND, "*.py"))
                  if "@command(" in open(f, encoding="utf-8").read())
check("@command modules found", len(cmd_mods) >= 10, f"{len(cmd_mods)}: {cmd_mods}")
for mod in cmd_mods:
    try:
        __import__(mod)
    except Exception as e:
        check(f"import {mod}", False, str(e)[:80])

bad_sig = []
for name, spec in sorted(cr._HANDLERS.items()):
    fn = spec["fn"]
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        continue
    pos = [p for p in sig.parameters.values()
           if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    varargs = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
    if len(pos) != 1 and not varargs:
        bad_sig.append(f"{name}({', '.join(sig.parameters)})")
check("registry handlers are fn(params)", not bad_sig, "; ".join(bad_sig[:5]))
check("registry non-empty", len(cr._HANDLERS) >= 30, len(cr._HANDLERS))

# ── 2) 前端字面量 sendCommand ⊆ main.js ALLOWED_COMMANDS ──
main_js = open(os.path.join(ROOT, "electron_app", "main.js"), encoding="utf-8").read()
m = re.search(r"const ALLOWED_COMMANDS = new Set\(\[(.*?)\]\);", main_js, re.S)
check("main.js whitelist block found", bool(m))
whitelist = set(re.findall(r"'([a-z_0-9]+)'", m.group(1))) if m else set()

used, dynamic = set(), 0
for f in glob.glob(os.path.join(ROOT, "electron_app", "src", "js", "*.js")):
    src = open(f, encoding="utf-8").read()
    for mm in re.finditer(r"\.sendCommand\(\s*(['\"])([a-zA-Z_0-9]+)\1", src):
        used.add(mm.group(2))
    # 非字面量调用（变量/模板串）：只计数提示，人工确认其最终取值均有字面量路径
    dynamic += len(re.findall(r"\.sendCommand\(\s*[a-zA-Z_$`][^'\"]*?[,)]", src))
missing = sorted(used - whitelist)
check("frontend sendCommand ⊆ whitelist", not missing, f"missing: {missing}")
check("frontend literal commands sane", len(used) >= 50,
      f"{len(used)} literal / {dynamic} dynamic calls")

# ── 3) 白名单 ⊆ 后端存在（注册表 ∪ 遗留 register_handler 字面量 ∪ 元组循环注册） ──
legacy = set()
for f in glob.glob(os.path.join(BACKEND, "*.py")):
    src = open(f, encoding="utf-8").read()
    legacy |= set(re.findall(r"register_handler\(\s*['\"]([a-z_0-9]+)['\"]", src))
    # 元组循环注册（pipeline_runner.register_handlers 的 cmds 列表形如
    # ("pipeline_start", handle_xxx)）——静态可解析，不引入人工 allowlist
    legacy |= set(re.findall(r"\(\s*['\"]([a-z_0-9]+)['\"]\s*,\s*handle_[a-z_0-9]+\s*\)", src))
known = set(cr._HANDLERS) | legacy
dead = sorted(whitelist - known)
check("whitelist entries exist in backend", not dead, f"unknown: {dead}")

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
