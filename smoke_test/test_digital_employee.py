#!/usr/bin/env python3
"""
test_digital_employee.py — 数字员工功能实测(本地大脑 Qwen3-VL)

进程内 import 真实 service + agent_module(非桩——IPC 循环在 __main__ guard,
import 无副作用),订阅事件流驱动一轮完整数字员工会话。

场景(--scenario):
  read    看+读:清点照片 → view_photo 多模态描述(感知)
  files   建夹+复制:文件操作工具(行动)
  gate    删除确认门:先拒后批(门控) + undo 撤销恢复
  preflight 只验证配置/模型选择/工具注册,不调 LLM

用法: python smoke_test/test_digital_employee.py [场景] [--photos DIR] [--workspace DIR]
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import service  # 真模块(非桩)
# MCP 子进程按 sys.argv[0] 自我拉起(service.py --mcp-server photo_s)。
# 在测试脚本进程里跑时必须伪装成 service.py,否则 MCP 起不来(开发环境同款机制)
sys.argv[0] = os.path.join(BACKEND, "service.py")
import agent_module  # noqa: E402


def _kill_llm_at_exit():
    """测试进程退出必须停掉 llama-server:宿主进程死了它不会跟着死,
    孤儿进程各占 ~2.5GB commit——多轮测试遗留曾把页面文件撑爆(2026-09-12)"""
    import atexit
    atexit.register(lambda: agent_module.local_llm.stop())

PASS = []
FAIL = []


def ok(name):
    PASS.append(name)
    print(f"  [OK] {name}", flush=True)


def bad(name, detail=""):
    FAIL.append(name)
    print(f"  [FAIL] {name}  {detail}", flush=True)


class Driver:
    """订阅事件流 + 审批脚本 + 会话等待。"""

    def __init__(self, approve_script=None):
        # approve_script: fn(tool, args) -> bool;None=默认批准
        self.approve_script = approve_script
        self.events = []
        self._unsub = service.subscribe_events(self._on_event)

    def _on_event(self, name, data):
        self.events.append((name, data or {}))
        if name == "agent_permission_request":
            verdict = (self.approve_script or (lambda t, a: True))(
                data.get("tool"), data.get("args"))
            print(f"  [门控] {data.get('tool')} → {'批准' if verdict else '拒绝'}"
                  f"  ({data.get('summary', '')[:60]})", flush=True)
            agent_module.handle_agent_approve(
                {"request_id": data.get("request_id"), "approved": verdict})

    def run(self, prompt, work_folder, timeout=600):
        os.makedirs(work_folder, exist_ok=True)  # handle_agent_run 要求已存在目录
        r = agent_module.handle_agent_run({
            "work_folder": work_folder, "prompt": prompt,
            "auto_approve_destructive": False})
        if "error" in r:
            return r, self.events
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = any(n == "agent_done" for n, _ in self.events)
            if done:
                break
            time.sleep(1.0)
        return r, self.events

    def tool_calls(self):
        return [(d.get("tool"), d.get("args"))
                for n, d in self.events if n == "agent_tool_call"]

    def finish(self):
        self._unsub()


def _session_overview(events):
    """从事件流提取助手文本/工具/结果摘要。"""
    lines = []
    for n, d in events:
        if n == "agent_tool_call":
            args = json.dumps(d.get("args"), ensure_ascii=False)[:160]
            lines.append(f"TOOL {d.get('tool')}({args})")
        elif n == "agent_tool_result":
            s = json.dumps(d.get("result"), ensure_ascii=False)[:160]
            lines.append(f"  → {s}")
        elif n == "agent_log":
            lines.append(f"LOG {d.get('message', '')[:160]}")
        elif n == "agent_error":
            lines.append(f"ERR {d.get('message', '')[:160]}")
        elif n == "agent_done":
            lines.append(f"DONE {json.dumps(d, ensure_ascii=False)[:200]}")
    return "\n".join(lines)


def scenario_preflight(args):
    print("\n[Preflight] 配置/模型选择/工具注册")
    cfg = agent_module._load_agent_config()
    model = agent_module.resolve_model(cfg)
    print(f"  agent config: use_local={cfg.get('use_local', True)}, "
          f"model={cfg.get('model') or '(默认)'}")
    print(f"  resolved model: {model} (LOCAL_MODEL_NAME={agent_module.LOCAL_MODEL_NAME})")
    if model == agent_module.LOCAL_MODEL_NAME and local_present():
        ok(f"本地大脑已选中: {model}")
    elif local_present():
        ok(f"模型已选(云端或自定义): {model}")
    else:
        bad("本地模型缺失", "GGUF/mmproj 不在本地")
    n_tools = len(agent_module.TOOL_REGISTRY)
    (ok if n_tools > 50 else bad)(f"工具注册: {n_tools} 个")
    v = agent_module.vision_available(cfg)
    (ok if v else bad)(f"多模态视觉可用: {v}")
    base, err = None, None
    if args.ping_llm:
        base, err = agent_module.local_llm.ensure_running(wait_s=120)
        if base:
            ok("llama-server 可拉起")
            agent_module.local_llm.stop()
        else:
            bad("llama-server 可拉起", err or "")
    return 0


def local_present():
    return agent_module.local_llm.is_model_present()


def scenario_read(args):
    print(f"\n[场景 read] 清点 + 多模态看图 ({args.photos})")
    d = Driver()
    try:
        prompt = (f"请清点 {args.photos} 文件夹里有多少张照片，"
                  f"然后用 view_photo 看第一张照片，告诉我照片里拍的是什么内容、"
                  f"是横幅还是竖幅。完成后简要汇报。")
        r, ev = d.run(prompt, args.workspace, timeout=600)
        calls = d.tool_calls()
        names = [c[0] for c in calls]
        print(_session_overview(ev))
        if "error" in r:
            bad("会话启动", r)
            return 1
        if not any("photo" in (n or "") or "scan" in (n or "") or "exif" in (n or "")
                   for n in names):
            bad("使用了照片清点类工具", names)
        else:
            ok(f"照片清点工具调用: {names}")
        (ok if "view_photo" in names else bad)(
            "view_photo 多模态看图" + ("" if "view_photo" in names else f" 实际: {names}"))
    finally:
        d.finish()
    return 0


def scenario_files(args):
    print(f"\n[场景 files] 建夹+复制 (工作区 {args.workspace})")
    os.makedirs(args.workspace, exist_ok=True)
    d = Driver()
    try:
        prompt = (f"请在 {args.workspace} 下创建一个名为'AI精选'的文件夹，"
                  f"然后从 {args.photos} 里挑前 3 张照片复制进去。完成后汇报复制了哪些文件。")
        r, ev = d.run(prompt, args.workspace, timeout=600)
        print(_session_overview(ev))
        calls = d.tool_calls()
        names = [c[0] for c in calls]
        target = os.path.join(args.workspace, "AI精选")
        made = os.path.isdir(target)
        (ok if made or "create_folder" in names else bad)("创建文件夹")
        copied = len([f for f in os.listdir(target) if os.path.isfile(os.path.join(target, f))]) \
            if made else 0
        copy_ok = [d_ for n, d_ in ev if n == "agent_tool_result"
                   and d_.get("tool") == "copy_photo" and d_.get("ok")]
        (ok if copied >= 3 and len(copy_ok) >= 3 else bad)(
            f"实际复制落盘 3 张(落盘 {copied},成功调用 {len(copy_ok)})")
    finally:
        d.finish()
    return 0


def scenario_gate(args):
    print(f"\n[场景 gate] 删除确认门:先拒后批 + undo")
    ws = args.workspace
    os.makedirs(ws, exist_ok=True)
    # 准备一个可删除的副本
    srcs = sorted([f for f in os.listdir(args.photos)
                   if f.lower().endswith(".arw")])[:1]
    if not srcs:
        bad("源照片缺失", args.photos)
        return 1
    import shutil
    victim = os.path.join(ws, "待删_副本.ARW")
    shutil.copy2(os.path.join(args.photos, srcs[0]), victim)

    # 第 1 轮:拒绝删除 → 文件应仍在
    d = Driver(approve_script=lambda t, a: False if t == "delete_photo" else True)
    try:
        r, ev = d.run(f"请删除文件 {victim}", ws, timeout=420)
        print(_session_overview(ev))
        time.sleep(0.5)
        still_there = os.path.exists(victim)
        (ok if still_there else bad)("拒绝后文件仍在(门控生效)")
        resolved = [d_ for n, d_ in ev if n == "agent_permission_resolved"]
        (ok if resolved and resolved[0].get("approved") is False else bad)(
            "permission_resolved(approved=False) 事件")
    finally:
        d.finish()

    # 第 2 轮:批准删除 → 文件进回收站 → undo 恢复
    d2 = Driver(approve_script=lambda t, a: True)
    try:
        r, ev = d2.run(f"请删除文件 {victim}，然后立刻用 undo_last_action 撤销这次删除。"
                       f"最后汇报文件是否已恢复。", ws, timeout=600)
        print(_session_overview(ev))
        restored = os.path.exists(victim)
        (ok if restored else bad)("批准删除 + undo 后文件恢复")
    finally:
        d2.finish()
    return 0


SCENARIOS = {
    "preflight": scenario_preflight,
    "read": scenario_read,
    "files": scenario_files,
    "gate": scenario_gate,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="preflight",
                    choices=list(SCENARIOS) + ["all"])
    ap.add_argument("--photos", default="D:/photoDatebase/3-19-扫街")
    ap.add_argument("--workspace", default="D:/tmp/agent_test_ws")
    ap.add_argument("--ping-llm", action="store_true",
                    help="preflight 场景下拉起/停止一次 llama-server 验证可用")
    args = ap.parse_args()

    print(f"数字员工实测 — 场景: {args.scenario}")
    print(f"照片源: {args.photos}\n工作区: {args.workspace}")
    _kill_llm_at_exit()

    todo = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    # gate 放最后(有删除动作);preflight 总是第一个
    todo.sort(key=lambda s: 0 if s == "preflight" else 2 if s == "gate" else 1)
    rc = 0
    for s in todo:
        rc |= SCENARIOS[s](args) or 0

    print(f"\n=== 汇总: {len(PASS)} 过 / {len(FAIL)} 挂 ===")
    for f in FAIL:
        print(f"  FAIL: {f}")
    return 1 if FAIL else rc


if __name__ == "__main__":
    sys.exit(main())
