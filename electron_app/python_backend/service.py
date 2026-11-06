"""
service.py — IPC message loop for Electron ↔ Python communication
Reads JSON commands from stdin, routes to handlers, writes JSON responses/events to stdout.
"""

import sys
import json
import threading
import traceback
import os
import time
import atexit
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ── Global state ──
_thread_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='ipc-handler')
_cleanup_done = False
_handlers = {}
_running = True
_event_queue = []  # thread-safe: only modified from main thread, read from send loop
_event_lock = threading.Lock()
_stdout_lock = threading.Lock()
_cleanup_handlers = []  # registered via register_cleanup(), called on shutdown
_cleanup_lock = threading.Lock()

# Internal event subscribers (in-process). send_event notifies these in
# addition to writing to stdout. Used by agent_module to observe tool progress
# (e.g. score_progress) without a round-trip through the renderer.
_event_subscribers = []
_event_sub_lock = threading.Lock()


def subscribe_events(fn):
    """Register an in-process callback fn(event_name, data). Returns unsubscribe."""
    with _event_sub_lock:
        _event_subscribers.append(fn)

    def _unsubscribe():
        with _event_sub_lock:
            try:
                _event_subscribers.remove(fn)
            except ValueError:
                pass
    return _unsubscribe


def register_handler(command, fn):
    """Register a function to handle a command."""
    _handlers[command] = fn


def register_cleanup(fn):
    """Register a function to call on shutdown (IPC or OS signal).
    Cleanup handlers are called in LIFO order. Each should be idempotent
    and never raise (exceptions are logged and swallowed)."""
    with _cleanup_lock:
        _cleanup_handlers.append(fn)


def _do_cleanup():
    """Run all registered cleanup handlers in LIFO order. Idempotent — runs at most once."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    with _cleanup_lock:
        handlers = list(_cleanup_handlers)
    for fn in reversed(handlers):
        try:
            fn()
        except Exception as e:
            try:
                send_log(f"cleanup handler error: {e}", "error")
            except Exception:
                pass


def _shutdown():
    """Graceful shutdown: run cleanup, then signal the message loop to exit."""
    global _running
    if not _running:
        return
    _do_cleanup()
    _running = False


def _on_os_signal(signum, frame):
    """Handle SIGTERM/SIGINT as a best-effort graceful exit."""
    try:
        send_log(f"received signal {signum}, shutting down", "warn")
    except Exception:
        pass
    _shutdown()
    # Give cleanup a moment, then force exit
    import os as _os
    _os._exit(0)


def send_response(msg_id, result=None, error=None):
    """Send a response for a specific command id."""
    resp = {"id": msg_id}
    if error:
        resp["error"] = str(error)
    else:
        resp["result"] = result or {}
    _write(resp)


def send_event(event_name, data=None, noblock=False):
    """Send an unsolicited event to Electron.

    When noblock=True, the write is buffered (no fsync/flush) — safe for
    high-frequency progress events where losing a few on crash is acceptable.
    Gate/error/done events should use noblock=False (the default).
    """
    _write({"event": event_name, "data": data or {}}, noblock=noblock)
    # Notify in-process subscribers (best-effort, never blocks stdout path)
    with _event_sub_lock:
        subs = list(_event_subscribers)
    for fn in subs:
        try:
            fn(event_name, data or {})
        except Exception:
            pass


def send_log(message, level="info"):
    """Send a log event."""
    send_event("log", {"message": message, "level": level})


def _write(obj, noblock=False):
    """Thread-safe write to stdout. When noblock=True, skip flush (safe for
    high-frequency progress events; buffered until OS flushes naturally)."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # Sanitize lone surrogates from Windows filenames (invalid in UTF-8)
    line = line.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        if not noblock:
            sys.stdout.flush()


# ── Message Loop ──

def run():
    """Main message loop — reads from stdin, dispatches to handlers."""
    global _running

    # Force UTF-8 for both stdin and stdout to avoid garbled Chinese characters
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    # Register OS signal handlers + atexit as a defense-in-depth fallback
    # for when Electron kills the subprocess without sending the IPC "shutdown"
    # command. Cleanup handlers run before exit regardless of how we're killed.
    atexit.register(_do_cleanup)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_os_signal)
        except Exception:
            pass  # SIGTERM/SIGINT not available on this platform (e.g. Windows has no SIGTERM)

    # Signal ready
    _write({"ready": True, "running": True})
    send_log("Python backend started", "info")

    while _running:
        try:
            line = sys.stdin.readline()
        except EOFError:
            break
        except Exception:
            break

        if not line:
            break

        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send_log(f"Invalid JSON: {line[:80]}", "error")
            continue

        msg_id = msg.get("id")
        command = msg.get("command", "")
        params = msg.get("params", {})

        if command == "shutdown":
            send_response(msg_id, {"ok": True})
            _shutdown()
            break

        handler = _handlers.get(command)
        if handler:
            try:
                # Run handler in a thread pool so long operations don't block the message loop
                # Bounded pool (max 16 workers) prevents thread exhaustion under load
                _thread_pool.submit(_run_handler, msg_id, handler, params)
            except Exception as e:
                send_response(msg_id, error=f"Handler error: {e}")
        else:
            send_response(msg_id, error=f"Unknown command: {command}")

    # Cleanup when stdin closes (Electron process exited without IPC shutdown).
    # _shutdown() already ran if we got the IPC command; _do_cleanup is idempotent.
    _do_cleanup()
    send_log("Python backend shutting down", "info")


def _run_handler(msg_id, handler, params):
    """Execute a handler and send response back."""
    try:
        result = handler(params)
        send_response(msg_id, result=result if isinstance(result, dict) else {"value": result})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        send_response(msg_id, error=str(e))


# ══════════════════════════════════════════════════════════════════════
# Built-in handlers
# ══════════════════════════════════════════════════════════════════════

_config_file = os.path.join(os.path.dirname(__file__), "app_config.json")
_app_config = {}


def _get_config_dir():
    """P0-1: resolve a writable user-data directory for app_config.json.

    Packaged apps expose APS_USER_DATA (Electron app.getPath('userData')).
    Falls back to a per-user dir under the home folder so the file is never
    written into the read-only packaged resources directory.
    """
    env_dir = os.environ.get("APS_USER_DATA", "").strip()
    if env_dir:
        return env_dir
    # Platform-default user data dir
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/LensPilot")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LensPilot")
    else:
        base = os.path.expanduser("~/.lenspilot")
    return base


def _resolve_config_file():
    """Pick the config path in the writable user dir, migrating any legacy
    config that previously lived next to the script."""
    cfg_dir = _get_config_dir()
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except Exception:
        return os.path.join(os.path.dirname(__file__), "app_config.json")
    new_path = os.path.join(cfg_dir, "app_config.json")
    legacy = os.path.join(os.path.dirname(__file__), "app_config.json")
    # One-time migration: copy legacy config into the user dir
    if not os.path.exists(new_path) and os.path.exists(legacy):
        try:
            import shutil
            shutil.copy2(legacy, new_path)
        except Exception:
            pass
    return new_path


_config_file = _resolve_config_file()

# Default presets — all major camera brands and cinema cameras
_DEFAULT_PRESETS = {
    # ── 微单/单反（通用）──
    "微单照片+视频": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    # ── 索尼（照片/视频分目录）──
    "索尼微单": {
        "photo_scan_path": "DCIM",
        "video_scan_path": os.path.join("PRIVATE", "M4ROOT", "CLIP"),
    },
    "索尼电影机(FX3/FX6/FX9)": {
        "photo_scan_path": "DCIM",
        "video_scan_path": os.path.join("PRIVATE", "M4ROOT", "CLIP"),
    },
    # ── 佳能 ──
    "佳能 EOS R 系列": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    "佳能 电影机(C70/C300/C500)": {
        "photo_scan_path": "DCIM",
        "video_scan_path": os.path.join("CONTENTS", "CLIPS001"),
    },
    "佳能 R5C": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    # ── 尼康 ──
    "尼康 Z/单反": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    # ── 富士 ──
    "富士 X/GFX": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    # ── 松下 ──
    "松下 Lumix": {
        "photo_scan_path": "DCIM",
        "video_scan_path": os.path.join("PRIVATE", "AVCHD"),
    },
    # ── 电影机 ──
    "Blackmagic PCC/Ursa": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "",
    },
    # ── 大疆 ──
    "DJI (Ronin 4D/无人机)": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    # ── 其他品牌 ──
    "徕卡": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    "宾得": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    "奥林巴斯/OM": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
    "适马 fp": {
        "photo_scan_path": "DCIM",
        "video_scan_path": "DCIM",
    },
}


def _load_config():
    global _app_config
    try:
        if os.path.exists(_config_file):
            with open(_config_file, "r", encoding="utf-8") as f:
                _app_config = json.load(f)
    except Exception:
        _app_config = {}

    # Ensure default presets always exist
    if "presets" not in _app_config or not _app_config["presets"]:
        _app_config["presets"] = dict(_DEFAULT_PRESETS)
    else:
        # Merge defaults into existing presets
        for name, paths in _DEFAULT_PRESETS.items():
            if name not in _app_config["presets"]:
                _app_config["presets"][name] = dict(paths)

    if "current_preset" not in _app_config or not _app_config["current_preset"]:
        _app_config["current_preset"] = next(iter(_app_config["presets"]), "")


def _save_config_to_disk():
    try:
        from utils import atomic_write_json
        atomic_write_json(_config_file, _app_config)
    except Exception as e:
        send_log(f"Failed to save config: {e}", "error")


def handle_get_config(params):
    return {"config": _app_config}


def handle_save_config(params):
    config = params.get("config", {})
    _app_config.update(config)
    _save_config_to_disk()
    return {"ok": True}


# Load config at startup
_load_config()
register_handler("get_config", handle_get_config)
register_handler("save_config", handle_save_config)


def handle_clear_user_data(params):
    """Delete all user data: models, cache, config, usage records.
    Returns {ok: true, deleted_paths: [...]} so the frontend can report."""
    import shutil
    from utils import get_user_data_dir
    data_dir = get_user_data_dir()
    deleted = []
    errors = []

    # Files to delete
    for fn in ['app_config.json', 'agent_usage.json', 'backend_pref.json', 'scores.db',
               'scores.db-wal', 'scores.db-shm',
               'feedback_queue.db', 'feedback_queue.db-wal', 'feedback_queue.db-shm']:
        p = os.path.join(data_dir, fn)
        if os.path.exists(p):
            try: os.remove(p); deleted.append(p)
            except Exception as e: errors.append(str(e))

    # Directories to delete (recursively)
    for sub in ['model', 'thumbs', 'heatmaps', 'preferences', 'agent_kb', 'skills']:
        d = os.path.join(data_dir, sub)
        if os.path.isdir(d):
            try: shutil.rmtree(d); deleted.append(d)
            except Exception as e: errors.append(str(e))

    # Also clean the temp image cache
    import tempfile
    cache_dir = os.path.join(tempfile.gettempdir(), 'lenspilot_cache')
    if os.path.isdir(cache_dir):
        try: shutil.rmtree(cache_dir); deleted.append(cache_dir)
        except Exception as e: errors.append(str(e))

    if errors:
        send_log(f"clear_user_data: {len(errors)} errors — {errors[:3]}", "warn")
    send_log(f"clear_user_data: deleted {len(deleted)} items", "info")
    return {"ok": True, "deleted_paths": deleted, "errors": errors}

register_handler("clear_user_data", handle_clear_user_data)


# ── Main ──

if __name__ == "__main__":
    # ── 提权 helper 子命令（打包模式）──
    # 「一键安装驱动」要 UAC 提权跑独立进程；打包后磁盘上没有 .py 可跑，
    # 故把 service.exe 本身当 helper 启动器: service.exe --install-camera-driver。
    # 静态 import 同时让 PyInstaller 分析时自动把 install_camera_driver 打进包。
    if '--install-camera-driver' in sys.argv:
        import install_camera_driver
        try:
            _drv_code = install_camera_driver.main() or 0
        except Exception as _drv_e:
            print(f'[!] 驱动安装器异常: {_drv_e}')
            _drv_code = 1
        try:
            input('\n按回车关闭此窗口...')
        except Exception:
            pass
        sys.exit(_drv_code)

    # ── MCP server 子命令（打包模式）──
    # 数字员工的 MCP 客户端管理器用 stdio 拉起本进程当工具服务器:
    #   service.exe --mcp-server photo_s → 运行 photo_s.mcp_server 的阻塞 stdio 主循环。
    # 开发模式同理: python service.py --mcp-server photo_s。
    # 静态 import 让 PyInstaller 分析时自动把 photo_s.mcp_server 打进包。
    if '--mcp-server' in sys.argv:
        try:
            _mcp_name = sys.argv[sys.argv.index('--mcp-server') + 1]
        except IndexError:
            _mcp_name = ''
        if _mcp_name == 'photo_s':
            import photo_s.mcp_server
            # 冻结环境 entry_points 不可靠 → 手动注册 vendored 插件（MCP 工具随之挂载）
            import photo_s_plugins
            photo_s_plugins.ensure_auto_tone_plugin()
            try:
                photo_s.mcp_server.run_stdio()
            except KeyboardInterrupt:
                pass
            sys.exit(0)
        print(f'[!] 未知 MCP server: {_mcp_name!r}', file=sys.stderr)
        sys.exit(2)

    # ── Crash Reporting ──
    import traceback
    CRASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash_logs')
    os.makedirs(CRASH_DIR, exist_ok=True)

    def _crash_handler(exc_type, exc_value, exc_tb):
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        crash_file = os.path.join(CRASH_DIR, f'crash_{int(time.time())}.log')
        with open(crash_file, 'w', encoding='utf-8') as f:
            f.write(f'Time: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'Type: {exc_type.__name__}\n')
            f.write(f'Message: {str(exc_value)}\n')
            f.write('Traceback:\n')
            f.write(''.join(tb_lines))
        # Also print to stderr so Electron can capture it
        print(f'[CRASH] Log written to {crash_file}', file=sys.stderr)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_handler

    # IPC: read crash logs
    def handle_get_crash_logs(params):
        logs = []
        if os.path.isdir(CRASH_DIR):
            for f in sorted(os.listdir(CRASH_DIR), reverse=True)[:5]:
                path = os.path.join(CRASH_DIR, f)
                with open(path, 'r', encoding='utf-8') as fh:
                    logs.append({'file': f, 'content': fh.read()[:2000]})
        return {'logs': logs}
    register_handler('get_crash_logs', handle_get_crash_logs)

    # IPC: clear crash logs
    def handle_clear_crash_logs(params):
        if os.path.isdir(CRASH_DIR):
            for f in os.listdir(CRASH_DIR):
                os.remove(os.path.join(CRASH_DIR, f))
        return {'ok': True}
    register_handler('clear_crash_logs', handle_clear_crash_logs)

    # Fix: make __main__ accessible as 'service' for imports from sub-modules
    sys.modules['service'] = sys.modules['__main__']

    # Import other modules to register their handlers.
    # Broad catch on purpose: a module-level failure (not just ImportError)
    # must not kill the whole backend — its commands just return Unknown command.
    try:
        from copy_card import register_handlers as register_copy_handlers
        register_copy_handlers()
    except Exception as e:
        send_log(f"Warning: copy_card module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from photo_scorer import register_handlers as register_scorer_handlers
        register_scorer_handlers()
    except Exception as e:
        send_log(f"Warning: photo_scorer module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from cloud_uploader import register_handlers as register_upload_handlers
        register_upload_handlers()
    except Exception as e:
        send_log(f"Warning: cloud_uploader module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from ftp_server import register_handlers as register_ftp_handlers
        register_ftp_handlers()
    except Exception as e:
        send_log(f"Warning: ftp_server module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from folder_watcher import register_handlers as register_watch_handlers
        register_watch_handlers()
    except Exception as e:
        send_log(f"Warning: folder_watcher module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from agent_module import register_handlers as register_agent_handlers
        register_agent_handlers()
    except Exception as e:
        send_log(f"Warning: agent_module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from agent_watcher import register_handlers as register_agent_watch_handlers
        register_agent_watch_handlers()
    except Exception as e:
        send_log(f"Warning: agent_watcher not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from photo_editor import register_handlers as register_editor_handlers
        register_editor_handlers()
    except Exception as e:
        send_log(f"Warning: photo_editor not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from feedback_uploader import register_handlers as register_feedback_handlers
        register_feedback_handlers()
    except Exception as e:
        send_log(f"Warning: feedback_uploader not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from pipeline_runner import register_handlers as register_pipeline_handlers
        register_pipeline_handlers()
    except Exception as e:
        send_log(f"Warning: pipeline_runner not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from pipeline_planner import register_handlers as register_planner_handlers
        register_planner_handlers()
    except Exception as e:
        send_log(f"Warning: pipeline_planner not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from shooting_coach import register_handlers as register_coach_handlers
        register_coach_handlers()
    except Exception as e:
        send_log(f"Warning: shooting_coach not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from reference_library import register_handlers as register_reference_handlers
        register_reference_handlers()
    except Exception as e:
        send_log(f"Warning: reference_library not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from photo_tagger import register_handlers as register_tagger_handlers
        register_tagger_handlers()
    except Exception as e:
        send_log(f"Warning: photo_tagger module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from search_query import register_handlers as register_search_handlers
        register_search_handlers()
    except Exception as e:
        send_log(f"Warning: search_query module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from lr_xmp_import import register_handlers as register_lr_import_handlers
        register_lr_import_handlers()
    except Exception as e:
        send_log(f"Warning: lr_xmp_import module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from edit_journal import register_handlers as register_journal_handlers
        register_journal_handlers()
    except Exception as e:
        send_log(f"Warning: edit_journal module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from segment_models import register_handlers as register_segment_handlers
        register_segment_handlers()
    except Exception as e:
        send_log(f"Warning: segment_models module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from edit_versions import register_handlers as register_versions_handlers
        register_versions_handlers()
    except Exception as e:
        send_log(f"Warning: edit_versions module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from reference_match import register_handlers as register_reference_match_handlers
        register_reference_match_handlers()
    except Exception as e:
        send_log(f"Warning: reference_match module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from edit_memory import register_handlers as register_edit_memory_handlers
        register_edit_memory_handlers()
    except Exception as e:
        send_log(f"Warning: edit_memory module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from photo_merge import register_handlers as register_merge_handlers
        register_merge_handlers()
    except Exception as e:
        send_log(f"Warning: photo_merge module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from dng_lens import register_handlers as register_dng_handlers
        register_dng_handlers()
    except Exception as e:
        send_log(f"Warning: dng_lens module not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from photo_retouch import register_handlers as register_retouch_handlers
        register_retouch_handlers()
    except Exception as e:
        send_log(f"Warning: photo_retouch module not loaded: {e}\n{traceback.format_exc()}", "error")

    # photo_s vendored 插件注册（frozen 下 entry_points 不可靠；失败静默走 suggest 降级）
    try:
        import photo_s_plugins
        photo_s_plugins.ensure_auto_tone_plugin()
    except Exception as e:
        send_log(f"Warning: photo_s_plugins not loaded: {e}\n{traceback.format_exc()}", "error")

    try:
        from auto_tone_manager import register_handlers as register_auto_tone_handlers
        register_auto_tone_handlers()
    except Exception as e:
        send_log(f"Warning: auto_tone_manager not loaded: {e}\n{traceback.format_exc()}", "error")

    run()
