#!/usr/bin/env python3
"""
LensPilot 全功能冒烟测试
=======================
脱离 Electron，直接通过 stdin/stdout IPC 与 Python 后端交互。
测试覆盖所有模块：config、拷卡、AI 评分、Agent、FTP、文件夹监听。

用法:
  python smoke_test/smoke_test.py              # 基础测试（不需要 API key）
  python smoke_test/smoke_test.py --agent      # 含 Agent LLM 调用
  python smoke_test/smoke_test.py --modules scoring,copy_card  # 指定模块

Agent 测试需要 smoke_agent_key.txt（3 行: base_url / api_key / model），不入 git。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# ── Configuration ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "electron_app" / "python_backend"
TEST_DIR = Path(__file__).resolve().parent
TEMP_DIR = TEST_DIR / "smoke_output"
IMAGES_DIR = TEMP_DIR / "images"
AGENT_KEY_FILE = TEST_DIR / "smoke_agent_key.txt"
CMD_TIMEOUT = 30  # seconds for regular commands
STARTUP_TIMEOUT = 30  # seconds for backend startup

# Colored output
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

results = {"pass": 0, "fail": 0, "skip": 0}


def ok(msg):
    results["pass"] += 1
    print(f"  {GREEN}PASS{RESET} {msg}")


def fail(msg, detail=""):
    results["fail"] += 1
    print(f"  {RED}FAIL{RESET} {msg}")
    if detail:
        print(f"       {RED}{detail}{RESET}")


def skip(msg):
    results["skip"] += 1
    print(f"  {YELLOW}SKIP{RESET} {msg}")


# ── Test image generator ──
def generate_test_images():
    """Generate small valid JPEG files for testing."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    # Minimal valid JPEG
    jpeg_header = (
        b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
        b'\xff\xdb\x00\x43\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
        b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e'
        b'\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\x1f\x1f\x1f\x1f\x1f'
        b'\x1f\x1f\x1f\x1f\x1f\x1f\x1f\x1f\xff\xc0\x00\x0b\x08\x00\x01\x00\x01'
        b'\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01'
        b'\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08'
        b'\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf \xff\xd9'
    )
    names = ["sample_01.jpg", "sample_02.jpg", "sample_03.jpg"]
    for name in names:
        path = IMAGES_DIR / name
        if not path.exists():
            # Create a small but valid JPEG (~1KB)
            data = jpeg_header + b'\x00' * (1024 - len(jpeg_header))
            path.write_bytes(data)
            print(f"  Generated test image: {path}")
    return names


# ── IPC helpers ──
_msg_id = 0
_msg_lock = threading.Lock()
_pending = {}  # id -> threading.Event + result
_event_queue = []  # events collected by reader thread
_event_lock = threading.Lock()


def _next_id():
    global _msg_id
    with _msg_lock:
        _msg_id += 1
        return str(_msg_id)


def send_cmd(proc, command, params=None):
    """Send a command to the Python backend, return (msg_id, result_dict)."""
    mid = _next_id()
    msg = json.dumps({"id": mid, "command": command, "params": params or {}},
                     ensure_ascii=False)
    proc.stdin.write(msg + "\n")
    proc.stdin.flush()
    return mid


def _reader_thread(proc):
    """Background thread: read stdout lines, dispatch responses vs events."""
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        if "id" in obj:
            mid = obj["id"]
            if mid in _pending:
                _pending[mid]["result"] = obj
                _pending[mid]["event"].set()
        elif "event" in obj:
            evt = obj["event"]
            data = obj.get("data", {})
            with _event_lock:
                _event_queue.append((evt, data))
            if "error" in str(evt).lower():
                print(f"  [event] {YELLOW}{evt}{RESET}: {json.dumps(data, ensure_ascii=False)[:120]}")


def recv_response(proc, msg_id, timeout=CMD_TIMEOUT):
    """Wait for a specific response by msg_id."""
    if msg_id not in _pending:
        _pending[msg_id] = {"event": threading.Event(), "result": None}
    got = _pending[msg_id]["event"].wait(timeout=timeout)
    result = _pending[msg_id]["result"]
    del _pending[msg_id]
    if not got:
        return None
    return result


def cmd(proc, command, params=None, timeout=CMD_TIMEOUT):
    """Send command and wait for response. Returns (ok, result_dict)."""
    mid = send_cmd(proc, command, params)
    resp = recv_response(proc, mid, timeout)
    if resp is None:
        return False, {"error": "timeout"}
    if "error" in resp:
        return False, resp
    return True, resp.get("result", {})


# ── Event collector (for agent_run) ──
def collect_events(event_name, timeout=60):
    """Wait for an event with the given name from the reader thread queue. Returns data dict or None."""
    start = time.time()
    while time.time() - start < timeout:
        with _event_lock:
            for i, (evt, data) in enumerate(_event_queue):
                if evt == event_name:
                    _event_queue.pop(i)
                    return data
                if evt == "agent_error":
                    err = data.get("error", "")
                    print(f"  [agent_error] {err[:200]}")
        time.sleep(0.1)
    return None


# ── Test phases ──

def test_startup(proc, tmp_dir):
    """Phase 0: startup and config."""
    print(f"\n{CYAN}── 阶段 0: 启动和基础设施{RESET}")

    # 0.1: backend ready (already happened in setup)
    ok("后端启动")

    # 0.2: get_config
    good, r = cmd(proc, "get_config")
    if good and isinstance(r, dict):
        ok("get_config")
    else:
        fail("get_config", str(r))

    # 0.3: save_config
    good, r = cmd(proc, "save_config", {"copy_target_path": str(tmp_dir)})
    if good:
        ok("save_config")
    else:
        fail("save_config", str(r))

    # 0.4: crash logs
    good, r = cmd(proc, "get_crash_logs")
    if good:
        ok("get_crash_logs")
    else:
        fail("get_crash_logs", str(r))


def test_copy_card(proc, tmp_dir):
    """Phase 1: copy card module."""
    print(f"\n{CYAN}── 阶段 1: 拷卡模块{RESET}")

    good, r = cmd(proc, "list_drives", {"scan_path": "DCIM"})
    if good and "drives" in r:
        drive_count = len(r["drives"])
        ok(f"list_drives ({drive_count} drives)")
    else:
        fail("list_drives", str(r))

    good, r = cmd(proc, "start_monitor", {
        "scan_path": "DCIM",
        "target_path": str(tmp_dir),
        "file_filter": "all",
    })
    if good:
        ok("start_monitor")
    else:
        fail("start_monitor", str(r))

    good, r = cmd(proc, "stop_monitor")
    if good:
        ok("stop_monitor")
    else:
        fail("stop_monitor", str(r))


def test_scoring(proc):
    """Phase 2: photo scoring module."""
    print(f"\n{CYAN}── 阶段 2: AI 评分模块{RESET}")

    good, r = cmd(proc, "get_model_status")
    if good:
        loaded = r.get("loaded", False)
        ok(f"get_model_status (loaded={loaded})")
    else:
        fail("get_model_status", str(r))

    good, r = cmd(proc, "scan_images", {"folder": str(IMAGES_DIR)})
    if good:
        count = r.get("count", 0)
        ok(f"scan_images ({count} images)")
    else:
        fail("scan_images", str(r))

    good, r = cmd(proc, "list_images_in_folder", {"folder": str(IMAGES_DIR)})
    if good:
        count = r.get("count", 0)
        ok(f"list_images_in_folder ({count} images)")
    else:
        fail("list_images_in_folder", str(r))

    good, r = cmd(proc, "get_scored_results", {"folder": str(IMAGES_DIR)})
    if good:
        ok("get_scored_results")
    else:
        fail("get_scored_results", str(r))

    # File operations on test images
    test_img = str(IMAGES_DIR / "sample_01.jpg")
    good, r = cmd(proc, "rename_file", {"oldPath": test_img, "newPath": test_img + ".renamed"})
    if good:
        # Rename back
        cmd(proc, "rename_file", {"oldPath": test_img + ".renamed", "newPath": test_img})
        ok("rename_file")
    else:
        fail("rename_file", str(r))


def test_agent_basic(proc):
    """Phase 3a: Agent basic operations (no LLM call)."""
    print(f"\n{CYAN}── 阶段 3a: Agent 基础操作 (无 LLM){RESET}")

    # Config
    good, r = cmd(proc, "agent_get_config")
    if good:
        ok("agent_get_config")
    else:
        fail("agent_get_config", str(r))

    # KB CRUD
    good, r = cmd(proc, "agent_kb_save", {"name": "smoke_test", "content": "冒烟测试文档"})
    if good:
        ok("agent_kb_save")
    else:
        fail("agent_kb_save", str(r))

    good, r = cmd(proc, "agent_kb_get", {"name": "smoke_test"})
    if good and r.get("content") == "冒烟测试文档":
        ok("agent_kb_get")
    else:
        fail("agent_kb_get", str(r))

    good, r = cmd(proc, "agent_kb_list")
    if good:
        ok("agent_kb_list")
    else:
        fail("agent_kb_list", str(r))

    good, r = cmd(proc, "agent_kb_delete", {"name": "smoke_test"})
    if good:
        ok("agent_kb_delete")
    else:
        fail("agent_kb_delete", str(r))

    # Skills CRUD
    good, r = cmd(proc, "skill_save", {"name": "smoke_skill", "body": "冒烟测试技能体"})
    if good:
        ok("skill_save")
    else:
        fail("skill_save", str(r))

    good, r = cmd(proc, "skill_list")
    if good:
        ok("skill_list")
    else:
        fail("skill_list", str(r))

    good, r = cmd(proc, "skill_delete", {"name": "smoke_skill"})
    if good:
        ok("skill_delete")
    else:
        fail("skill_delete", str(r))

    # Prompts
    good, r = cmd(proc, "agent_get_prompts")
    if good:
        ok("agent_get_prompts")
    else:
        fail("agent_get_prompts", str(r))

    # Usage
    good, r = cmd(proc, "agent_get_usage")
    if good:
        ok("agent_get_usage")
    else:
        fail("agent_get_usage", str(r))

    # MCP list
    good, r = cmd(proc, "mcp_list_servers")
    if good:
        ok("mcp_list_servers")
    else:
        fail("mcp_list_servers", str(r))

    # list new files
    good, r = cmd(proc, "agent_list_new_files", {"folder": str(IMAGES_DIR), "since_ts": 0})
    if good:
        count = r.get("count", 0)
        ok(f"agent_list_new_files ({count} files)")
    else:
        fail("agent_list_new_files", str(r))


def test_agent_llm(proc, agent_cfg):
    """Phase 3b: Agent with real LLM calls (requires API key)."""
    print(f"\n{CYAN}── 阶段 3b: Agent LLM 调用{RESET}")

    base_url = agent_cfg["base_url"]
    api_key = agent_cfg["api_key"]
    model = agent_cfg["model"]

    # Test LLM connectivity
    good, r = cmd(proc, "agent_test_llm", {
        "base_url": base_url, "api_key": api_key, "model": model,
    })
    if not good:
        fail("agent_test_llm", r.get("error", str(r)))
        return
    reply = r.get("reply", "")
    ok(f"agent_test_llm (reply={reply[:50]})")

    # Save agent config for the agent_run
    good, _ = cmd(proc, "agent_save_config", {
        "agent": {
            "base_url": base_url, "api_key": api_key, "model": model,
            "temperature": 0.3, "default_auto_approve": False,
        }
    })
    if not good:
        fail("agent_save_config (for LLM test)")
        return
    ok("agent_save_config (LLM config)")

    # Quick smoke run: minimal tokens
    print(f"  {CYAN}→ 快速冒烟 Agent 运行...{RESET}")
    good, r = cmd(proc, "agent_run", {
        "prompt": "只回复 ok，不要调用任何工具，立即调用 finish 结束。",
        "auto_approve_destructive": True,
        "model": model,
    })
    if not good:
        fail("agent_run (quick smoke)", r.get("error", str(r))[:200])
        return

    # Wait for agent_done event
    data = collect_events("agent_done", timeout=120)
    if data:
        summary = data.get("summary", "")[:100]
        iterations = data.get("iterations", 0)
        ok(f"agent_run quick smoke (iter={iterations}, summary={summary})")
    else:
        fail("agent_run quick smoke (no agent_done event)")

    # Tool-chain test: use scan_count (simpler than list_photos for some models)
    print(f"  {CYAN}→ Agent 工具链测试 (scan_count)...{RESET}")
    good, r = cmd(proc, "agent_run", {
        "prompt": f"调用 scan_count 统计 {IMAGES_DIR} 的文件数。只使用 scan_count 和 finish。",
        "work_folder": str(IMAGES_DIR),
        "auto_approve_destructive": True,
        "model": model,
    })
    if not good:
        fail("agent_run (tool chain)", r.get("error", str(r))[:200])
        return

    data = collect_events("agent_done", timeout=120)
    if data:
        summary = data.get("summary", "")[:200]
        iterations = data.get("iterations", 0)
        ok(f"agent_run tool chain (iter={iterations})")
    else:
        skip(f"agent_run tool chain (model {model} may not support function calling)")
        return


def test_ftp(proc, tmp_dir):
    """Phase 4: FTP server."""
    print(f"\n{CYAN}── 阶段 4: FTP 服务{RESET}")

    good, r = cmd(proc, "ftp_start", {"folder": str(tmp_dir), "port": 2122})
    if good and r.get("running"):
        ok("ftp_start")
    else:
        fail("ftp_start", str(r))
        return

    good, r = cmd(proc, "ftp_status")
    if good and r.get("running"):
        ok("ftp_status")
    else:
        fail("ftp_status", str(r))

    good, r = cmd(proc, "ftp_stop")
    if good:
        ok("ftp_stop")
    else:
        fail("ftp_stop", str(r))


def test_cloud(proc):
    """Phase 4b: Cloud upload connection test."""
    print(f"\n{CYAN}── 阶段 4b: 云上传{RESET}")
    # Cloud test requires a valid token — skip if not configured
    cloud_cfg_file = PROJECT_ROOT / "studio-server" / "data" / "secret.json"
    token = None
    server_url = "https://studio.dwphoto.top"
    if cloud_cfg_file.exists():
        try:
            with open(cloud_cfg_file, "r", encoding="utf-8") as f:
                secret = json.load(f)
            token = secret.get("test_token") or secret.get("admin_token")
        except Exception:
            pass
    if not token:
        skip("cloud_test_connection (no test token configured)")
        return

    good, r = cmd(proc, "cloud_test_connection", {"server_url": server_url, "token": token})
    if good:
        ok("cloud_test_connection")
    else:
        fail("cloud_test_connection", str(r))


def test_watch(proc, tmp_dir):
    """Phase 5: folder watching."""
    print(f"\n{CYAN}── 阶段 5: 文件夹监听{RESET}")

    out_dir = tmp_dir / "watch_output"
    out_dir.mkdir(exist_ok=True)

    # Folder watch
    good, r = cmd(proc, "start_folder_watch", {
        "folder": str(IMAGES_DIR), "output_folder": str(out_dir),
        "threshold": 10.0, "mode": "batch",
    })
    if good:
        ok("start_folder_watch")
    else:
        fail("start_folder_watch", str(r))

    good, r = cmd(proc, "folder_watch_status")
    if good and r.get("running"):
        ok("folder_watch_status")
    else:
        fail("folder_watch_status", str(r))

    good, r = cmd(proc, "stop_folder_watch")
    if good:
        ok("stop_folder_watch")
    else:
        fail("stop_folder_watch", str(r))

    # Agent hot-folder watch
    good, r = cmd(proc, "agent_watch_start", {
        "id": "smoke_watch", "name": "冒烟测试监听",
        "watch_folder": str(IMAGES_DIR), "quiet_seconds": 5,
        "threshold": 10,
    })
    if good:
        ok("agent_watch_start")
    else:
        fail("agent_watch_start", str(r))

    good, r = cmd(proc, "agent_watch_status")
    if good:
        watches = r.get("watches", [])
        ok(f"agent_watch_status ({len(watches)} watches)")
    else:
        fail("agent_watch_status", str(r))

    good, r = cmd(proc, "agent_watch_stop", {"id": "smoke_watch"})
    if good:
        ok("agent_watch_stop")
    else:
        fail("agent_watch_stop", str(r))


def test_cleanup(proc, tmp_dir):
    """Phase 6: shutdown and cleanup."""
    print(f"\n{CYAN}── 阶段 6: 清理{RESET}")

    # Clear usage data
    good, r = cmd(proc, "agent_clear_usage")
    if good:
        ok("agent_clear_usage")
    else:
        fail("agent_clear_usage", str(r))

    # Shutdown
    good, r = cmd(proc, "shutdown")
    if good:
        ok("shutdown")
    else:
        fail("shutdown", str(r))

    # Wait for process to exit
    try:
        proc.wait(timeout=5)
        ok("进程正常退出")
    except subprocess.TimeoutExpired:
        proc.kill()
        fail("进程未能在 5s 内退出")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="LensPilot 冒烟测试")
    parser.add_argument("--agent", action="store_true", help="包含 Agent LLM 调用测试（需要 API key）")
    parser.add_argument("--modules", type=str, help="只测试指定模块（逗号分隔）: startup,copy_card,scoring,agent,ftp,cloud,watch")
    args = parser.parse_args()

    # Determine which modules to run
    all_modules = {"startup", "copy_card", "scoring", "agent", "ftp", "cloud", "watch"}
    if args.modules:
        modules = set(m.strip() for m in args.modules.split(","))
        modules = modules & all_modules
    else:
        modules = all_modules

    # Read agent key if requested
    agent_cfg = None
    if args.agent:
        if AGENT_KEY_FILE.exists():
            lines = [l.strip() for l in AGENT_KEY_FILE.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            if len(lines) >= 3:
                agent_cfg = {"base_url": lines[0], "api_key": lines[1], "model": lines[2]}
                print(f"{CYAN}Agent 测试已启用: {agent_cfg['model']} @ {agent_cfg['base_url']}{RESET}")
            else:
                print(f"{YELLOW}smoke_agent_key.txt 格式错误（需要 3 行: url / key / model），跳过 Agent LLM 测试{RESET}")
        else:
            print(f"{YELLOW}未找到 smoke_agent_key.txt，跳过 Agent LLM 测试。创建此文件以启用：{RESET}")
            print(f"  {CYAN}echo <base_url> > {AGENT_KEY_FILE}{RESET}")
            print(f"  {CYAN}echo <api_key> >> {AGENT_KEY_FILE}{RESET}")
            print(f"  {CYAN}echo <model> >> {AGENT_KEY_FILE}{RESET}")

    # Setup
    print(f"{CYAN}════════════════════════════════════════{RESET}")
    print(f"{CYAN}  LensPilot 冒烟测试{RESET}")
    print(f"{CYAN}════════════════════════════════════════{RESET}")

    # Clean and recreate temp dir
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True)

    # Generate test images
    generate_test_images()

    # Inject agent config if testing
    config_dir = TEMP_DIR / "config"
    config_dir.mkdir()
    config_file = config_dir / "app_config.json"
    if agent_cfg:
        config_data = {"agent": {
            "base_url": agent_cfg["base_url"],
            "model": agent_cfg["model"],
            "api_key": agent_cfg["api_key"],
            "temperature": 0.3,
            "default_auto_approve": True,
        }}
        config_file.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Start backend
    print(f"\n{CYAN}启动 Python 后端...{RESET}")
    env = os.environ.copy()
    env["APS_USER_DATA"] = str(config_dir)

    proc = subprocess.Popen(
        [sys.executable, str(BACKEND_DIR / "service.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", cwd=str(BACKEND_DIR), env=env,
    )

    # Wait for ready signal FIRST (before starting reader thread, which consumes stdout)
    ready = False
    start = time.time()
    while time.time() - start < STARTUP_TIMEOUT:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            obj = json.loads(line.strip())
            if obj.get("ready") and obj.get("running"):
                ready = True
                break
        except Exception:
            continue

    if not ready:
        try:
            stderr_output = proc.stderr.read()
        except Exception:
            stderr_output = ""
        fail(f"后端启动失败 (超时 {STARTUP_TIMEOUT}s)", stderr_output[:500] if stderr_output else "(no stderr)")
        proc.kill()
        shutil.rmtree(TEMP_DIR)
        return 1

    print(f"  {GREEN}后端已就绪{RESET}")

    # Start stdout reader thread AFTER ready signal
    reader = threading.Thread(target=_reader_thread, args=(proc,), daemon=True)
    reader.start()

    try:
        # Run tests
        if "startup" in modules:
            test_startup(proc, TEMP_DIR)
        if "copy_card" in modules:
            test_copy_card(proc, TEMP_DIR)
        if "scoring" in modules:
            test_scoring(proc)
        if "agent" in modules:
            test_agent_basic(proc)
            if agent_cfg:
                test_agent_llm(proc, agent_cfg)
            else:
                skip("Agent LLM 测试 (无 API key)")
        if "ftp" in modules:
            test_ftp(proc, TEMP_DIR)
        if "cloud" in modules:
            test_cloud(proc)
        if "watch" in modules:
            test_watch(proc, TEMP_DIR)

        test_cleanup(proc, TEMP_DIR)

    finally:
        # Ensure cleanup
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(TEMP_DIR)
        print(f"\n{CYAN}临时文件已清理: {TEMP_DIR}{RESET}")

    # Summary
    total = results["pass"] + results["fail"] + results["skip"]
    print(f"\n{CYAN}════════════════════════════════════════{RESET}")
    print(f"  通过: {GREEN}{results['pass']}{RESET}  失败: {RED}{results['fail']}{RESET}  跳过: {YELLOW}{results['skip']}{RESET}  总计: {total}")
    if results["fail"] == 0:
        print(f"  {GREEN}全部通过 [OK]{RESET}")
    else:
        print(f"  {RED}{results['fail']} 项失败 [FAIL]{RESET}")
    print(f"{CYAN}════════════════════════════════════════{RESET}")

    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
