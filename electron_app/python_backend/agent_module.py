"""
agent_module.py — LLM Agent that operates photos via the existing photo_scorer tools.

Design:
- An OpenAI-compatible function-calling loop runs in a daemon thread.
- The LLM is given a fixed set of tools (TOOL_REGISTRY). Each tool is a thin
  wrapper around an existing photo_scorer.handle_* function, called IN-PROCESS.
- A sandbox validator (_assert_in_workfolder) enforces that every file the
  agent touches is inside the designated work folder. The LLM never has path
  authority — the wrapper layer rejects anything outside.
- Destructive tools (delete/rename/dedup_burst) pass through a confirmation
  gate (threading.Event per request) unless the session is auto_approve.
- Progress streams to the renderer as agent_* events.

See C:\\Users\\16341\\.claude\\plans\\...md for the full design.
"""

import os
import sys
import json
import time
import uuid
import threading
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse

from service import send_event, send_log, register_handler, subscribe_events, register_cleanup
from skill_loader import get_skill_loader
from mcp_client_manager import get_mcp_manager
import photo_scorer
import copy_card
import folder_watcher

# openai is optional — guard the import so the rest of the backend still loads
try:
    from openai import OpenAI
    _OPENAI_OK = True
except Exception as _e:  # pragma: no cover
    OpenAI = None
    _OPENAI_OK = False
    _OPENAI_ERR = str(_e)


# ── 官方 API 已停用(本地模型替代)──
# 保留空 _official_config 与函数签名以兼容历史 import, 但不再恢复/使用官方 key。
_official_config = {}
OFFICIAL_SERVER_URL = "https://studio.dwphoto.top"

def _set_official_config(base_url, api_key, model):
    """官方 API 已停用: 仅清空内存配置。"""
    global _official_config
    _official_config.clear()

def _restore_official_config():
    """官方 API 已停用: 不恢复任何内容, 清空内存配置。"""
    global _official_config
    _official_config.clear()

# ── Constants ──
# Token budget is unlimited (the user pays the LLM provider). Iterations are
# capped only as a runaway-loop backstop — raised well above a normal task.
MAX_ITERATIONS = 100
MAX_TOKENS = 64000       # default context limit (conservative for most models)
CTX_SAFETY_RATIO = 0.80   # trim when estimated tokens exceed 80% of limit

# 本地模型(内置 llama-server):上下文大、生成不设上限, 裁剪阈值取极大值
from local_llm import LOCAL_CONTEXT_LIMIT, LOCAL_MODEL_NAME, local_llm  # noqa: E402


def _context_limit():
    """上下文裁剪阈值: 本地模型用大值(256K 上下文), 云端保持保守默认。"""
    try:
        if local_llm.is_model_present() and cfg_use_local_default():
            return LOCAL_CONTEXT_LIMIT
    except Exception:
        pass
    return MAX_TOKENS


def cfg_use_local_default():
    """读取 agent 配置中的 use_local(默认 True)。供上下文裁剪等轻量场景使用。"""
    try:
        return bool(_load_agent_config().get("use_local", True))
    except Exception:
        return True


def resolve_model(cfg, default=""):
    """解析要用的模型名: 优先用户自定义(更强云端模型); 否则本地模型。"""
    m = (cfg.get("model") or "").strip()
    if m:
        return m
    if cfg.get("use_local", True) and local_llm.is_model_present():
        return LOCAL_MODEL_NAME
    return default or ""


def vision_available(cfg=None):
    """是否可用视觉输入。显式开启多模态 或 使用本地 Qwen3-VL(原生多模态)。

    本地模型天然支持视觉，无需用户手动开「多模态」开关；个人云端模型才受
    该开关控制(部分云端模型不支持图像输入，开启可能导致请求被拒)。
    """
    if cfg is None:
        cfg = _load_agent_config()
    if cfg.get("multimodal") == "on":
        return True
    return bool(cfg.get("use_local", True)) and local_llm.is_model_present()
DEFAULT_APPROVAL_TIMEOUT = 120  # seconds
RESULT_TRUNCATE = 4096  # max chars of a tool result embedded in the event
USAGE_FILE = "agent_usage.json"  # written into the user-data dir


# ══════════════════════════════════════════════════════════════════════
# Session management
# ══════════════════════════════════════════════════════════════════════

class _Session:
    __slots__ = (
        "id", "work_folder", "prompt", "auto_approve", "model", "headless",
        "cancel_event", "pending_approvals", "pending_questions",
        "iteration", "tokens_used",
        "prompt_tokens", "completion_tokens", "cached_tokens",
        "approval_timeout", "messages", "messages_lock", "start_time", "task_name",
        "reasoning_effort", "cloud_token",
        "undo_stack",  # list of {tool, args, undo_info}
        "recent_calls",  # list of (tool_name, args_hash, ok) — 死循环检测
    )

    def __init__(self, session_id, work_folder, prompt, auto_approve, model, headless):
        self.id = session_id
        self.work_folder = work_folder
        self.prompt = prompt
        self.auto_approve = bool(auto_approve)
        self.model = model or None
        self.headless = bool(headless)
        self.cancel_event = threading.Event()
        self.pending_approvals = {}  # request_id -> {"event":Event,"approved":None|bool}
        self.pending_questions = {}  # request_id -> {"event":Event,"answer":None|str}
        self.iteration = 0
        self.tokens_used = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.approval_timeout = DEFAULT_APPROVAL_TIMEOUT
        self.messages = []
        self.messages_lock = threading.Lock()
        self.start_time = time.time()
        self.task_name = ""
        self.reasoning_effort = None
        self.undo_stack = []
        self.cloud_token = ""


_active_session = None
_active_lock = threading.Lock()
_active_skill = None  # {name, allow_tools: list|None} — set by skill_activate


def _get_active():
    with _active_lock:
        return _active_session


# ══════════════════════════════════════════════════════════════════════
# Config (LLM credentials live in app_config.json under the "agent" key)
# ══════════════════════════════════════════════════════════════════════

import service as _service


def _load_agent_config():
    return _service._app_config.get("agent", {}) or {}

# Alias — multiple locations call _get_agent_cfg for brevity
_get_agent_cfg = _load_agent_config


def _save_agent_config(agent_cfg):
    _service._app_config["agent"] = agent_cfg
    _service._save_config_to_disk()


def handle_agent_get_config(params):
    cfg = _load_agent_config()
    llm_info = local_llm.get_config()
    return {"agent": {
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "api_key_set": bool(cfg.get("api_key", "")),
        "use_local": bool(cfg.get("use_local", True)),
        "local_model_present": llm_info.get("model_present", False),
        "local_running": bool(llm_info.get("running", False)),
        "local_reason": llm_info.get("reason", ""),
        "default_auto_approve": bool(cfg.get("default_auto_approve", False)),
        "reasoning_effort": cfg.get("reasoning_effort", "off"),
        "approval_timeout": cfg.get("approval_timeout", DEFAULT_APPROVAL_TIMEOUT),
        "preferences": cfg.get("preferences", ""),
        "temperature": cfg.get("temperature", 0.3),
        "multimodal": cfg.get("multimodal", "off"),
        "official_key_set": False,      # 官方 API 已停用
        "official_model": "",
    }}


def handle_agent_download_local_model(params):
    """下载本地 LLM 模型(GGUF + mmproj)到用户数据目录, 带进度事件。

    同步在 IPC 工作线程执行(service 的线程池), 不会阻塞消息循环;
    Electron 侧把它列入 longCmds(下载 ~1.5GB 可能耗时较长)。
    """
    if not local_llm._server_exe():
        return {"error": "本地推理引擎(llama-server)未内置，请升级到含本地模型的版本"}
    if local_llm.is_model_present():
        return {"ok": True, "already_present": True}
    def _progress(cur, total, name):
        send_event("agent_local_download", {
            "file": name, "current": cur, "total": total or 0,
            "done": bool(total and cur >= total),
        })
    try:
        out = local_llm.download_model_files(progress_cb=_progress)
    except Exception as e:
        send_event("agent_local_download", {"file": "", "current": 0, "total": 0, "done": True})
        return {"error": f"本地模型下载失败: {e}"}
    send_event("agent_local_download", {"file": "", "current": 0, "total": 0, "done": True})
    return {"ok": True, "files": {k: os.path.basename(v) for k, v in out.items()}}


def handle_agent_save_config(params):
    agent = params.get("agent", {}) or {}
    cfg = _load_agent_config()
    if "base_url" in agent:
        cfg["base_url"] = agent["base_url"]
    if "model" in agent:
        cfg["model"] = agent["model"]
    if agent.get("api_key"):  # only overwrite when a non-empty key is supplied
        # 静态加密（Windows DPAPI，见 secure_store）：api_key 不再明文落盘
        import secure_store
        cfg["api_key"] = secure_store.protect(str(agent["api_key"]))
    if "default_auto_approve" in agent:
        cfg["default_auto_approve"] = bool(agent["default_auto_approve"])
    if "reasoning_effort" in agent:
        val = agent["reasoning_effort"]
        if val in ("off", "low", "medium", "mid", "high", "max"):
            cfg["reasoning_effort"] = val
    if "approval_timeout" in agent:
        try:
            cfg["approval_timeout"] = int(agent["approval_timeout"])
        except Exception:
            pass
    if "multimodal" in agent:
        cfg["multimodal"] = agent["multimodal"]
    if "temperature" in agent:
        try:
            cfg["temperature"] = float(agent["temperature"])
        except Exception:
            pass
    if "use_local" in agent:
        cfg["use_local"] = bool(agent["use_local"])
    if "preferences" in agent:
        cfg["preferences"] = agent["preferences"]
    _save_agent_config(cfg)
    send_log("已保存 AI 助手配置", "info")
    return {"ok": True}


def handle_agent_test_llm(params):
    """Probe an OpenAI-compatible endpoint with a trivial completion."""
    if not _OPENAI_OK:
        return {"error": f"openai 库未安装: {_OPENAI_ERR}"}
    base_url = (params.get("base_url") or "").strip()
    api_key = (params.get("api_key") or "").strip()
    model = (params.get("model") or "").strip()
    if not base_url or not api_key or not model:
        return {"error": "base_url / api_key / model 不能为空"}
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=30)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复 ok"}],
            max_tokens=8,
            temperature=0,
        )
        reply = ""
        try:
            reply = resp.choices[0].message.content or ""
        except Exception:
            pass
        return {"ok": True, "model": model, "reply": reply[:200]}
    except Exception as e:
        return {"error": str(e)}


# ── Official API Key ──

def handle_agent_fetch_official_key(params):
    """官方 API 已停用: 统一返回禁用提示(客户端应隐藏入口)。"""
    return {"ok": False, "error": "官方 API 已停用，请使用本地模型或在设置中配置个人云端 Key"}


def handle_agent_get_quota(params):
    """官方 API 已停用: 返回 0 配额。"""
    return {"quota_total": 0, "tokens_used": 0, "remaining": 0}


def _report_official_usage(cloud_token, tokens_used):
    """Report token consumption to the server (fire-and-forget)."""
    if not cloud_token or tokens_used <= 0:
        return
    try:
        body = json.dumps({"tokens": {"total": tokens_used}}).encode("utf-8")
        url = f"{OFFICIAL_SERVER_URL}/api/llm/usage-report"
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "x-studio-token": cloud_token,
        }, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        remaining = data.get("remaining", -1)
        if remaining == 0:
            send_log("[agent] 官方 API 配额已用完", "warn")
    except Exception:
        pass  # reporting failure should not block


# ══════════════════════════════════════════════════════════════════════
# Token usage tracking (persisted to agent_usage.json in the user-data dir)
# ══════════════════════════════════════════════════════════════════════

_usage_lock = threading.Lock()


def _usage_file():
    d = _service._get_config_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, USAGE_FILE)


def _load_usage():
    try:
        with open(_usage_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"runs": data}
        return data or {"runs": []}
    except Exception:
        return {"runs": []}


def _save_usage(data):
    try:
        from utils import atomic_write_json
        atomic_write_json(_usage_file(), data)
    except Exception as e:
        send_log(f"[agent] 保存 usage 失败: {e}", "warn")


def _extract_usage(usage_obj):
    """Pull prompt/completion/total/cached token counts from an OpenAI usage
    object, tolerating providers that omit fields. Accepts both Pydantic
    objects (openai SDK) and plain dicts (some providers / llama-server paths)."""
    u = usage_obj
    if u is None:
        return {"prompt": 0, "completion": 0, "total": 0, "cached": 0}
    def _get(attr, dflt=0):
        try:
            v = u.get(attr) if isinstance(u, dict) else getattr(u, attr, None)
            return int(v or dflt)
        except Exception:
            return dflt
    prompt = _get("prompt_tokens")
    completion = _get("completion_tokens")
    total = _get("total_tokens") or (prompt + completion)
    cached = 0
    try:
        det = u.get("prompt_tokens_details") if isinstance(u, dict) else getattr(u, "prompt_tokens_details", None)
        if det is not None:
            cached = det.get("cached_tokens", 0) if isinstance(det, dict) else int(getattr(det, "cached_tokens", 0) or 0)
    except Exception:
        pass
    return {"prompt": prompt, "completion": completion, "total": total, "cached": cached}


def _record_run(session):
    """Append a finished run's token usage to the persisted history."""
    rec = {
        "ts": int(time.time()),
        "session_id": session.id,
        "model": session.model or "",
        "task_name": session.task_name or "",
        "headless": session.headless,
        "prompt": session.prompt[:120],
        "iterations": session.iteration,
        "prompt_tokens": session.prompt_tokens,
        "completion_tokens": session.completion_tokens,
        "cached_tokens": session.cached_tokens,
        "total_tokens": session.tokens_used,
        "duration_ms": int((time.time() - session.start_time) * 1000),
    }
    with _usage_lock:
        data = _load_usage()
        runs = data.get("runs", [])
        runs.append(rec)
        # keep last 500 runs
        if len(runs) > 500:
            runs = runs[-500:]
        data["runs"] = runs
        _save_usage(data)

    # Report to server if official key was used
    # proxy 模式下服务端已按真实 usage 扣量, 客户端不再上报(避免双计);
    # 仅旧版非代理官方 key 才需要客户端上报
    cloud_token = getattr(session, 'cloud_token', '') or ''
    if (cloud_token and _official_config.get("api_key")
            and not _official_config.get("proxy") and session.tokens_used > 0):
        _report_official_usage(cloud_token, session.tokens_used)

    return rec


def handle_agent_get_usage(params):
    data = _load_usage()
    runs = data.get("runs", [])
    agg = {"prompt": 0, "completion": 0, "cached": 0, "total": 0, "runs": len(runs)}
    for r in runs:
        agg["prompt"] += r.get("prompt_tokens", 0)
        agg["completion"] += r.get("completion_tokens", 0)
        agg["cached"] += r.get("cached_tokens", 0)
        agg["total"] += r.get("total_tokens", 0)
    return {"total": agg, "history": runs[-100:]}


def handle_agent_clear_usage(params):
    with _usage_lock:
        _save_usage({"runs": []})
    return {"ok": True}


def handle_agent_compact(params):
    """Summarize a conversation transcript into a compact context string.
    Called from the frontend `/compact` slash command. Uses a single
    lightweight LLM call — no tool loop, no streaming, short max_tokens.
    """
    transcript = params.get("transcript", [])
    if not transcript:
        return {"error": "empty_transcript", "detail": "对话记录为空"}

    cfg = _get_agent_cfg()
    model = params.get("model") or resolve_model(cfg, "deepseek-chat")

    client, err = _make_client(cfg)
    if not client:
        return {"error": "no_api_key", "detail": err or "LLM 未配置"}

    # Serialize transcript — keep only essential fields to save tokens
    compact_transcript = []
    for r in transcript:
        t = r.get("type", "")
        if t == "user":
            compact_transcript.append({"role": "user", "content": (r.get("text") or "")[:600]})
        elif t == "assistant":
            compact_transcript.append({"role": "assistant", "content": (r.get("text") or "")[:400]})
        elif t == "tool":
            compact_transcript.append({
                "role": "tool",
                "tool": r.get("tool", ""),
                "result": str(r.get("ok", "")),
                "summary": (r.get("summary") or "")[:200],
            })
        elif t == "done":
            compact_transcript.append({
                "role": "done",
                "summary": (r.get("summary") or "")[:300],
                "stats": r.get("stats", {}),
            })

    sys_prompt = (
        "你是一个对话摘要器。将以下用户与 AI 摄影助手的对话压缩为一段精简的上下文。\n"
        "保留以下内容：\n"
        "1) 用户的身份和偏好（如有）\n"
        "2) 已完成的操作和结果（选片、拷卡、上传、相册操作等）\n"
        "3) 当前工作文件夹路径（如有）\n"
        "4) 重要的未完成事项或后续需要跟进的任务\n"
        "输出纯文本，不超过 500 字。用中文。"
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(compact_transcript, ensure_ascii=False)},
            ],
            max_tokens=800,
            temperature=0.3,
        )
        summary = resp.choices[0].message.content or ""
        # Record token usage
        if resp.usage:
            usage = _extract_usage(resp.usage)
            send_log(f"[agent] compact: {usage['total']} tokens", "info")
        return {"ok": True, "summary": summary.strip()}
    except Exception as e:
        return {"error": "compact_failed", "detail": str(e)}
# ══════════════════════════════════════════════════════════════════════

def _kb_dir():
    d = os.path.join(_service._get_config_dir(), "agent_kb")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _kb_safe_name(name):
    """Sanitize a KB doc name to a bare .md filename."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if os.path.sep in name or "/" in name or "\\" in name:
        raise ValueError("name 不能含路径分隔符")
    if not name.lower().endswith(".md"):
        name = name + ".md"
    return name


def handle_agent_kb_list(params):
    d = _kb_dir()
    docs = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.lower().endswith(".md"):
                try:
                    preview = open(p, "r", encoding="utf-8", errors="replace").read(120).replace("\n", " ")
                except Exception:
                    preview = ""
                docs.append({"name": name, "size": os.path.getsize(p), "preview": preview})
    return {"documents": docs}


def handle_agent_kb_get(params):
    name = _kb_safe_name(params.get("name"))
    p = os.path.join(_kb_dir(), name)
    if not os.path.isfile(p):
        return {"error": "not_found"}
    return {"name": name, "content": open(p, "r", encoding="utf-8", errors="replace").read()}


def handle_agent_kb_save(params):
    name = _kb_safe_name(params.get("name"))
    content = params.get("content") or ""
    p = os.path.join(_kb_dir(), name)
    # Atomic write to prevent corruption on process kill
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, p)
    return {"ok": True, "name": name}


def handle_agent_kb_delete(params):
    name = _kb_safe_name(params.get("name"))
    p = os.path.join(_kb_dir(), name)
    if os.path.isfile(p):
        os.remove(p)
    return {"ok": True}


def _kb_doc_by_name(name):
    """Read a KB document by exact name. Returns dict or None."""
    try:
        safe = _kb_safe_name(name)
        p = os.path.join(_kb_dir(), safe)
        if os.path.isfile(p):
            return {"name": safe, "content": open(p, "r", encoding="utf-8", errors="replace").read()}
    except Exception:
        pass
    return None


def _kb_search(query, top_k=3):
    """Keyword-overlap search over KB markdown docs. Returns ranked snippets."""
    import re
    d = _kb_dir()
    if not os.path.isdir(d):
        return []
    terms = [t.lower() for t in re.findall(r"\w+", query or "") if t]
    if not terms:
        return []
    scored = []
    for name in os.listdir(d):
        if not name.lower().endswith(".md"):
            continue
        p = os.path.join(d, name)
        try:
            text = open(p, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        low = text.lower()
        score = sum(low.count(t) for t in terms)
        if score <= 0:
            continue
        # snippet around first term hit
        snippet = text[:200]
        for t in terms:
            idx = low.find(t)
            if idx >= 0:
                start = max(0, idx - 60)
                snippet = ("…" if start > 0 else "") + text[start:start + 240]
                break
        scored.append({"name": name, "score": score, "snippet": snippet})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def handle_agent_get_prompts(params):
    cfg = _load_agent_config()
    return {"prompts": cfg.get("prompts", []) or []}


def handle_agent_save_prompts(params):
    prompts = params.get("prompts", [])
    if not isinstance(prompts, list):
        return {"error": "prompts 必须是数组"}
    cfg = _load_agent_config()
    cfg["prompts"] = prompts
    _save_agent_config(cfg)
    return {"ok": True}


def handle_agent_list_new_files(params):
    """List image files in a folder with mtime > since_ts (for incremental
    'only new files' scheduled tasks). Returns paths + count."""
    folder = params.get("folder") or ""
    since_ts = float(params.get("since_ts") or 0)
    if not folder or not os.path.isdir(folder):
        return {"files": [], "count": 0}
    images = photo_scorer.find_images(folder)
    new = []
    for p in images:
        try:
            if os.path.getmtime(p) > since_ts:
                new.append(p)
        except OSError:
            continue
    return {"files": new, "count": len(new)}


# ══════════════════════════════════════════════════════════════════════
# Sandbox
# ══════════════════════════════════════════════════════════════════════

class SandboxViolation(PermissionError):
    pass


def _assert_in_workfolder(path, work_folder):
    """Raise SandboxViolation unless path resolves inside work_folder."""
    if not path:
        raise SandboxViolation("empty path")
    try:
        rp = os.path.realpath(path)
        wf = os.path.realpath(work_folder)
    except Exception as e:
        raise SandboxViolation(f"cannot resolve path: {path} ({e})")
    if rp != wf and not rp.startswith(wf + os.sep):
        raise SandboxViolation(f"路径不在工作文件夹内: {path}")
    return rp


def _assert_in_workfolder_or_approved(path, session, tool, summary):
    """工作文件夹约束 + 确认门逃生舱。

    复制/交付类任务天然跨文件夹（"把照片库的精选复制到交付盘"），
    一刀切硬拒会让任务在单会话内无解（2026-09-12 实测）。工作夹外的
    路径改为弹确认门：人工批准一次即放行；headless(auto_approve) 会话
    仍维持硬拒——无人值守时绝不允许越出工作夹。
    """
    try:
        return _assert_in_workfolder(path, session.work_folder)
    except SandboxViolation:
        if session.auto_approve:
            raise
        approved = _request_confirmation(
            session, tool, {"path": path},
            f"{summary}（路径在工作文件夹之外: {path}）")
        if not approved:
            raise SandboxViolation(f"用户未批准工作文件夹之外的路径: {path}")
        return os.path.realpath(path)


def _validate_path_list(paths, work_folder):
    for p in paths:
        _assert_in_workfolder(p, work_folder)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _ensure_model_loaded():
    """Best-effort load the scoring model if not already loaded."""
    try:
        if not photo_scorer._model_loaded:
            photo_scorer.handle_load_model({})
    except Exception as e:
        send_log(f"[agent] 模型加载失败: {e}", "warn")


def _emit(session, name, data):
    d = {"session_id": session.id}
    d.update(data or {})
    send_event(name, d)


def _truncate(obj, limit=RESULT_TRUNCATE):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) <= limit:
        return obj
    return {"_truncated": True, "preview": s[:limit], "length": len(s)}


def _summarize_score_result(r):
    """Summarize score_photos result.  Use get_scores tool for per-photo details."""
    return {
        "total": r.get("total", 0),
        "selected": r.get("selected", 0),
        "failed": r.get("failed", 0),
        "avg_score": r.get("avg_score", 0),
        "cached": r.get("cached", 0),
        "hint": "使用 get_scores 工具查看每张照片的具体评分",
    }


# ══════════════════════════════════════════════════════════════════════
# Confirmation gate (destructive ops when auto_approve=False)
# ══════════════════════════════════════════════════════════════════════

def _request_confirmation(session, tool, args, summary):
    """Block until the user approves/denies. Returns True if approved."""
    import uuid as _u
    request_id = _u.uuid4().hex
    entry = {"event": threading.Event(), "approved": None}
    session.pending_approvals[request_id] = entry
    _emit(session, "agent_permission_request", {
        "request_id": request_id, "tool": tool, "args": args, "summary": summary,
    })
    got = entry["event"].wait(timeout=session.approval_timeout)
    approved = entry["approved"] is True
    _emit(session, "agent_permission_resolved", {
        "request_id": request_id, "approved": approved,
        "timeout": not got,
    })
    session.pending_approvals.pop(request_id, None)
    return approved


def handle_agent_approve(params):
    request_id = params.get("request_id")
    approved = bool(params.get("approved", False))
    session = _get_active()
    if not session:
        return {"error": "no active session"}
    entry = session.pending_approvals.get(request_id)
    if not entry:
        return {"error": "unknown request_id"}
    entry["approved"] = approved
    entry["event"].set()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# Tool registry
# Each entry: {"description": str, "schema": dict, "destructive": bool,
#              "fn": (session, args) -> dict}
# ══════════════════════════════════════════════════════════════════════

# ── Undo helpers ──

def _record_undo(session, tool, args, undo_info):
    """Push an undo record. Tools call this after making changes."""
    session.undo_stack.append({"tool": tool, "args": args, "undo_info": undo_info})


def _do_undo(session):
    """Pop and execute the last undo record. Returns result dict."""
    if not session.undo_stack:
        return {"error": "nothing_to_undo", "detail": "没有可撤销的操作"}
    rec = session.undo_stack.pop()
    info = rec["undo_info"]
    action = info.get("action", "")
    try:
        if action == "move_back":
            files = info.get("files", [])
            for pair in files:
                src, dst = pair[0], pair[1]
                if os.path.exists(dst):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    os.replace(dst, src)
            return {"ok": True, "restored": len(files)}
        elif action == "restore_from_trash":
            files = info.get("files", [])
            for pair in files:
                trash_path, original = pair[0], pair[1]
                if os.path.exists(trash_path):
                    os.makedirs(os.path.dirname(original), exist_ok=True)
                    os.replace(trash_path, original)
            return {"ok": True, "restored": len(files)}
        elif action == "rename_back":
            pairs = info.get("pairs", [])
            for old, new in pairs:
                if os.path.exists(new):
                    os.replace(new, old)
            return {"ok": True, "renamed": len(pairs)}
        else:
            return {"error": f"unknown undo action: {action}"}
    except Exception as e:
        session.undo_stack.append(rec)  # Put it back on failure
        return {"error": "undo_failed", "detail": str(e)}


def _trash_dir(work_folder):
    d = os.path.join(work_folder, ".agent_trash")
    os.makedirs(d, exist_ok=True)
    return d


TOOL_REGISTRY = {}


def _tool(name, description, params_schema, destructive=False):
    def deco(fn):
        TOOL_REGISTRY[name] = {
            "description": description,
            "schema": params_schema,
            "destructive": destructive,
            "fn": fn,
        }
        return fn
    return deco


@_tool("list_photos",
       "[照片分析] 列出工作文件夹（或指定子文件夹）中的所有图片路径。只读。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "子文件夹路径，省略则用工作文件夹"}},
        "additionalProperties": False})
def _t_list_photos(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    res = photo_scorer.handle_list_images_in_folder({"folder": folder})
    images = res.get("images", [])
    if len(images) > 200:
        return {"count": len(images), "images": images[:200],
                "note": f"共 {len(images)} 张，仅显示前 200 张路径。"}
    return {"count": len(images), "images": images}


@_tool("scan_count",
       "[照片分析] 扫描文件夹中的图片数量与总大小（不评分）。只读。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "子文件夹路径，省略则用工作文件夹"}},
        "additionalProperties": False})
def _t_scan_count(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    return photo_scorer.handle_scan_images({"folder": folder})


@_tool("get_model_status",
       "[AI评分] 查询 AI 评分模型是否已加载。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_model_status(session, args):
    return photo_scorer.handle_get_model_status({})


@_tool("get_scores",
       "[照片分析] 获取工作文件夹中已缓存的评分结果（如未评分则返回空）。只读。",
       {"type": "object", "properties": {
           "folder": {"type": "string"}},
        "additionalProperties": False})
def _t_get_scores(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    res = photo_scorer.handle_get_scored_results({"folder": folder})
    results = res.get("results", [])
    # Compact: top entries by score
    summary = []
    for r in results[:50]:
        filename = r.get("filename") or (os.path.basename(r.get("path", "")) if r.get("path") else "")
        summary.append({"path": r.get("path"), "score": r.get("score"),
                        "filename": filename})
    return {"count": len(results), "model_version": res.get("model_version"),
            "results": summary,
            "note": (f"共 {len(results)} 条，仅返回前 50 条。" if len(results) > 50 else "")}


@_tool("get_photo_info",
       "[照片分析] 获取单张照片的详细信息：尺寸、模糊度、眼部状态、缓存评分、EXIF。只读。",
       {"type": "object", "properties": {
           "path": {"type": "string", "description": "照片绝对路径（须在工作文件夹内）"}},
        "required": ["path"], "additionalProperties": False})
def _t_photo_info(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    if not os.path.exists(path):
        return {"error": "文件不存在", "path": path}
    info = {"path": path}
    try:
        from PIL import Image as PILImage, ExifTags
        img = PILImage.open(path)
        info["width"], info["height"] = img.size
        info["format"] = img.format
        try:
            exif = img._getexif()
            if exif:
                info["exif"] = {ExifTags.TAGS.get(k, k): str(v)
                                for k, v in list(exif.items())[:15]}
        except Exception:
            pass
    except Exception as e:
        info["image_error"] = str(e)
    try:
        info["blur"] = photo_scorer.handle_detect_blur({"paths": [path]})
    except Exception as e:
        info["blur_error"] = str(e)
    try:
        info["eyes"] = photo_scorer.handle_detect_eyes({"path": path})
    except Exception as e:
        info["eyes_error"] = str(e)
    try:
        from model_loader import model_version
        import score_cache
        mv = model_version()
        st = os.stat(path)
        cached = score_cache.get(path, mv, st.st_mtime, st.st_size)
        if cached:
            info["score"] = cached.get("score")
    except Exception:
        pass
    return info


@_tool("get_heatmap",
       "[照片分析] 生成单张照片的注意力热力图路径（解释模型为何给高分）。只读。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_heatmap(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    _ensure_model_loaded()
    return photo_scorer.handle_get_heatmap({"path": path})


@_tool("detect_blur",
       "[AI评分] 批量检测照片模糊度（0=清晰，1=模糊）。只读。",
       {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["paths"], "additionalProperties": False})
def _t_detect_blur(session, args):
    paths = args.get("paths", []) or []
    _validate_path_list(paths, session.work_folder)
    return photo_scorer.handle_detect_blur({"paths": paths})


@_tool("detect_eyes",
       "[AI评分] 检测单张照片的人脸与眼部状态（睁眼/闭眼/半闭）。只读。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_detect_eyes(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    return photo_scorer.handle_detect_eyes({"path": path})


@_tool("deep_dedup",
       "[AI评分] 深度去重分析：返回重复照片分组（不移文件）。只读。",
       {"type": "object", "properties": {
           "paths": {"type": "array", "items": {"type": "string"}},
           "similarity_threshold": {"type": "number", "default": 0.95}},
        "required": ["paths"], "additionalProperties": False})
def _t_deep_dedup(session, args):
    paths = args.get("paths", []) or []
    _validate_path_list(paths, session.work_folder)
    thr = args.get("similarity_threshold", 0.95)
    return photo_scorer.handle_deep_dedup({"paths": paths, "similarity_threshold": thr})


@_tool("score_photos",
       "[AI评分] 对文件夹中的所有照片进行 AI 美学评分，并把分数≥阈值的复制到输出子文件夹。需要模型已加载（未加载会自动加载）。不删除原图。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "子文件夹，省略则用工作文件夹"},
           "threshold": {"type": "number", "default": 6.0, "description": "筛选阈值 1-10"},
           "output_name": {"type": "string", "default": "优秀照片"},
           "copy_on_score": {"type": "boolean", "default": True,
                             "description": "是否把选中照片复制到输出子文件夹"}},
        "additionalProperties": False})
def _t_score_photos(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    _ensure_model_loaded()
    if not photo_scorer._model_loaded:
        return {"error": "model_not_loaded", "detail": "AI 评分模型加载失败"}
    threshold = float(args.get("threshold", 6.0))
    output_name = (args.get("output_name") or "优秀照片").strip() or "优秀照片"
    copy_on_score = bool(args.get("copy_on_score", True))
    res = photo_scorer.handle_score_photos({
        "folder": folder, "threshold": threshold,
        "output_name": output_name, "copy_on_score": copy_on_score,
    })
    return _summarize_score_result(res)


@_tool("score_files",
       "[AI评分] 对指定的文件列表进行 AI 美学评分（1-10分），只评分给定文件而非整个文件夹。需要模型已加载。",
       {"type": "object", "properties": {
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "要评分的文件绝对路径列表"},
           "threshold": {"type": "number", "default": 6.0, "description": "筛选阈值 1-10"},
           "output_folder": {"type": "string", "description": "优秀照片输出目录，省略则不复制"}},
        "required": ["files"], "additionalProperties": False})
def _t_score_files(session, args):
    _ensure_model_loaded()
    if not photo_scorer._model_loaded:
        return {"error": "model_not_loaded", "detail": "模型加载失败"}
    files = args["files"]
    # Sandbox: every file must be inside work folder
    for f in files:
        _assert_in_workfolder(f, session.work_folder)
    threshold = float(args.get("threshold", 6.0))
    output_folder = args.get("output_folder") or ""
    if output_folder:
        _assert_in_workfolder(output_folder, session.work_folder)
    res = photo_scorer.handle_score_files({
        "files": files, "threshold": threshold,
        "output_folder": output_folder, "batch_id": uuid.uuid4().hex,
    })
    return _summarize_score_result(res)


@_tool("select_above_threshold",
       "[筛选导出] 把已评分照片中分数≥阈值的，导出到输出子文件夹（基于缓存评分，不重新评分）。不删原图。",
       {"type": "object", "properties": {
           "folder": {"type": "string"},
           "threshold": {"type": "number", "default": 6.0},
           "output_name": {"type": "string", "default": "优秀照片"}},
        "additionalProperties": False})
def _t_select_above(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    threshold = float(args.get("threshold", 6.0))
    output_name = (args.get("output_name") or "优秀照片").strip() or "优秀照片"
    res = photo_scorer.handle_get_scored_results({"folder": folder})
    results = res.get("results", [])
    keep = [r["path"] for r in results
            if r.get("score", 0) >= threshold and r.get("path")]
    if not keep:
        return {"error": "no_scores",
                "detail": "没有缓存的评分。请先调用 score_photos。"}
    out = photo_scorer.handle_apply_selection({
        "folder": folder, "output_name": output_name, "keep_paths": keep,
    })
    return {"selected": len(keep), "threshold": threshold,
            "copied": out.get("copied"), "removed": out.get("removed"),
            "output_folder": out.get("output_folder")}


@_tool("apply_selection",
       "[筛选导出] 按指定 keep_paths 列表导出到输出子文件夹（同步：拷入新增、移除未选）。动输出副本非原图。",
       {"type": "object", "properties": {
           "folder": {"type": "string"},
           "output_name": {"type": "string", "default": "优秀照片"},
           "keep_paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["keep_paths"], "additionalProperties": False})
def _t_apply_selection(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    keep = args.get("keep_paths", []) or []
    _validate_path_list(keep, session.work_folder)
    output_name = (args.get("output_name") or "优秀照片").strip() or "优秀照片"
    return photo_scorer.handle_apply_selection({
        "folder": folder, "output_name": output_name, "keep_paths": keep,
    })


@_tool("apply_watermark",
       "[筛选导出] 给指定照片加水印，输出到 _watermarked 子文件夹（不覆盖原图）。不删原图。",
       {"type": "object", "properties": {
           "file_paths": {"type": "array", "items": {"type": "string"}},
           "text": {"type": "string", "default": "© LensPilot"},
           "position": {"type": "string", "default": "bottom-right",
                        "enum": ["bottom-right", "bottom-left", "top-right", "top-left", "center"]},
           "opacity": {"type": "number", "default": 0.3}},
        "required": ["file_paths"], "additionalProperties": False})
def _t_watermark(session, args):
    file_paths = args.get("file_paths", []) or []
    _validate_path_list(file_paths, session.work_folder)
    if not file_paths:
        return {"error": "no files"}
    return photo_scorer.handle_apply_watermark({
        "file_paths": file_paths,
        "text": args.get("text", "© LensPilot"),
        "position": args.get("position", "bottom-right"),
        "opacity": float(args.get("opacity", 0.3)),
    })


@_tool("dedup_burst",
       "[文件操作⚠] 连拍选最佳：按时间+相似度分组，每组保留质量最佳的一张（美学分+清晰度加权），其余移入 _duplicates/。⚠️会移动原图。可先用 dry_run=true 预览分组与推荐保留项再确认。",
       {"type": "object", "properties": {
           "folder": {"type": "string"},
           "time_window": {"type": "integer", "default": 3, "description": "连拍时间窗口(秒)"},
           "similarity": {"type": "integer", "default": 5, "description": "汉明距离阈值"},
           "score_weight": {"type": "number", "default": 0.6, "description": "美学分权重"},
           "sharp_weight": {"type": "number", "default": 0.4, "description": "清晰度权重"},
           "dry_run": {"type": "boolean", "default": False, "description": "true=只预览分组不移动"}},
        "additionalProperties": False},
       destructive=True)
def _t_dedup_burst(session, args):
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    dry_run = bool(args.get("dry_run", False))
    summary = (f"连拍选最佳：扫描 {folder}，时间窗口 {args.get('time_window',3)}s，"
               f"相似度阈值 {args.get('similarity',5)}，每组保留最佳、其余移入 _duplicates/")
    # dry_run is read-only (no move) → skip the destructive confirmation gate
    if not dry_run and not session.auto_approve:
        if not _request_confirmation(session, "dedup_burst", args, summary):
            return {"error": "denied_by_user"}
    if session.cancel_event.is_set():
        return {"error": "cancelled"}
    return photo_scorer.handle_dedup_burst({
        "folder": folder,
        "time_window": int(args.get("time_window", 3)),
        "similarity": int(args.get("similarity", 5)),
        "score_weight": float(args.get("score_weight", 0.6)),
        "sharp_weight": float(args.get("sharp_weight", 0.4)),
        "dry_run": dry_run,
    })


@_tool("rename_photo",
       "[文件操作⚠] 重命名一张照片（新名字仅文件名，不能含路径）。⚠️会修改原图文件名。",
       {"type": "object", "properties": {
           "old_path": {"type": "string"},
           "new_name": {"type": "string", "description": "仅文件名，如 IMG_0002.jpg"}},
        "required": ["old_path", "new_name"], "additionalProperties": False},
       destructive=True)
def _t_rename(session, args):
    old_path = args.get("old_path", "")
    new_name = args.get("new_name", "").strip()
    _assert_in_workfolder(old_path, session.work_folder)
    if not new_name or os.path.sep in new_name or "/" in new_name or "\\" in new_name:
        return {"error": "invalid new_name", "detail": "new_name 必须是纯文件名"}
    new_path = os.path.join(os.path.dirname(os.path.realpath(old_path)), new_name)
    _assert_in_workfolder(new_path, session.work_folder)
    summary = f"重命名：{os.path.basename(old_path)} → {new_name}"
    if not session.auto_approve:
        if not _request_confirmation(session, "rename_photo", args, summary):
            return {"error": "denied_by_user"}
    if session.cancel_event.is_set():
        return {"error": "cancelled"}
    result = photo_scorer.handle_rename_file({"oldPath": old_path, "newPath": new_path})
    if "error" not in result:
        _record_undo(session, "rename_photo", args,
                     {"action": "rename_back", "pairs": [(old_path, new_path)]})
    return result


@_tool("delete_photo",
       "[文件操作⚠] 将照片移入回收站（.agent_trash/）。可通过 undo_last_action 恢复。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False},
       destructive=True)
def _t_delete(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    summary = f"删除照片（移入回收站）：{path}"
    if not session.auto_approve:
        if not _request_confirmation(session, "delete_photo", args, summary):
            return {"error": "denied_by_user"}
    if session.cancel_event.is_set():
        return {"error": "cancelled"}
    trash = _trash_dir(session.work_folder)
    trash_path = os.path.join(trash, os.path.basename(path))
    # Avoid collision
    if os.path.exists(trash_path):
        base, ext = os.path.splitext(os.path.basename(path))
        trash_path = os.path.join(trash, f"{base}_{int(time.time())}{ext}")
    os.replace(path, trash_path)
    _record_undo(session, "delete_photo", args,
                 {"action": "restore_from_trash", "files": [(trash_path, path)]})
    return {"ok": True, "trashed": path}


@_tool("get_config",
       "[设置] 查看当前应用的全局配置（拷贝目标路径、机型预设、文件过滤、FTP设置等）。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_get_config(session, args):
    import service as _svc
    cfg = _svc.handle_get_config({})
    # Return a safe subset (no API keys)
    safe = {}
    for key in ("copy_target_path", "copy_folder_name", "current_preset",
                "file_filter", "presets", "ftp_port", "ftp_receive_folder",
                "last_copy_dest", "model_type"):
        if key in cfg:
            safe[key] = cfg[key]
    return safe


@_tool("save_config",
       "[设置⚠] 修改应用的全局配置项。可修改拷贝目标、机型预设、文件过滤等。需用户确认。",
       {"type": "object", "properties": {
           "updates": {"type": "object", "description": "要更新的配置项，如 {\"copy_target_path\": \"D:\\Photos\", \"file_filter\": \"raw\"}"}},
        "required": ["updates"], "additionalProperties": False},
       destructive=True)
def _t_save_config(session, args):
    if not session.auto_approve:
        if not _request_confirmation(session, "save_config", {},
                f"修改配置: {json.dumps(args['updates'], ensure_ascii=False)[:200]}"):
            return {"error": "denied_by_user"}
    import service as _svc
    updates = args["updates"]
    if not isinstance(updates, dict):
        return {"error": "updates 必须是一个对象"}
    allowed_keys = {"copy_target_path", "copy_folder_name", "current_preset",
                    "file_filter", "presets", "ftp_port", "ftp_receive_folder"}
    safe = {k: v for k, v in updates.items() if k in allowed_keys}
    if not safe:
        return {"error": f"没有有效的配置项。允许的项: {', '.join(sorted(allowed_keys))}"}
    res = _svc.handle_save_config(safe)
    send_log(f"[agent] config updated: {list(safe.keys())}", "info")
    return res


@_tool("set_work_folder",
       "[系统] 设置/切换当前工作文件夹。当用户在指令里给了照片所在路径、或当前还没有工作文件夹时调用。设置后所有文件操作都限制在该文件夹内（沙箱）。需用户确认。",
       {"type": "object", "properties": {
           "path": {"type": "string", "description": "目标文件夹的绝对路径"}},
        "required": ["path"], "additionalProperties": False})
def _t_set_work_folder(session, args):
    path = (args.get("path") or "").strip()
    if not path:
        return {"error": "path 不能为空"}
    if not os.path.isdir(path):
        return {"error": "not_a_dir", "detail": f"路径不存在或不是文件夹: {path}"}
    real = os.path.realpath(path)
    summary = f"将工作文件夹设为：{real}"
    # Changing the sandbox boundary requires user confirmation (unless auto).
    if not session.auto_approve:
        if not _request_confirmation(session, "set_work_folder", args, summary):
            return {"error": "denied_by_user"}
    if session.cancel_event.is_set():
        return {"error": "cancelled"}
    session.work_folder = real
    # 沙箱边界变了 → 死循环计数失效(相同参数此刻指向不同结果),必须清零
    session.recent_calls = []
    _emit(session, "agent_work_folder_changed", {"work_folder": real})
    _emit(session, "agent_log", {"level": "info", "message": f"工作文件夹已设为 {real}"})
    return {"ok": True, "work_folder": real}


# ══════════════════════════════════════════════════════════════════════
# P1: Document tools (write/read/list/append — 修图报告等)
# ══════════════════════════════════════════════════════════════════════

_DOC_EXTS = (".md", ".txt", ".docx")


def _resolve_doc_path(session, filename, folder=None):
    """Resolve a document path inside work_folder (default 报告/ subfolder).
    filename must be a bare name (no path separators)."""
    if not filename:
        raise SandboxViolation("filename 不能为空")
    if os.path.sep in filename or "/" in filename or "\\" in filename:
        raise SandboxViolation("filename 必须是纯文件名，不能含路径分隔符")
    base = folder or os.path.join(session.work_folder, "报告")
    base = _assert_in_workfolder(base, session.work_folder)
    full = os.path.join(base, filename)
    full = _assert_in_workfolder(full, session.work_folder)
    return full


def _write_docx(path, content):
    try:
        from docx import Document
    except ImportError:
        return False
    doc = Document()
    for line in (content or "").split("\n"):
        doc.add_paragraph(line)
    doc.save(path)
    return True


def _read_docx(path):
    try:
        from docx import Document
    except ImportError:
        return None
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


@_tool("write_document",
       "[文档] 写一个文档（修图报告/说明等）。支持 .md / .txt（纯文本）/ .docx（需 python-docx）。默认写到工作文件夹的「报告」子文件夹。同名文件已存在时需用户确认覆盖。",
       {"type": "object", "properties": {
           "filename": {"type": "string", "description": "文件名，如 修图报告.md"},
           "content": {"type": "string", "description": "文档内容（markdown 或纯文本）"},
           "folder": {"type": "string", "description": "目标子文件夹，省略则用「报告」"}},
        "required": ["filename", "content"], "additionalProperties": False})
def _t_write_document(session, args):
    filename = (args.get("filename") or "").strip()
    content = args.get("content") or ""
    folder = args.get("folder") or None
    path = _resolve_doc_path(session, filename, folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(filename)[1].lower()
    if os.path.exists(path) and not session.auto_approve:
        if not _request_confirmation(session, "write_document", args, f"覆盖已有文档：{path}"):
            return {"error": "denied_by_user"}
    if ext == ".docx":
        if not _write_docx(path, content):
            return {"error": "docx_unavailable", "detail": "未安装 python-docx，请用 .md 或 .txt"}
    else:
        # Atomic write to prevent corruption on process kill
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    _emit(session, "agent_log", {"level": "info", "message": f"已写入文档 {path}"})
    return {"ok": True, "path": path, "bytes": os.path.getsize(path)}


@_tool("read_document",
       "[文档] 读取一个文档的内容（.md/.txt/.docx）。只读。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_read_document(session, args):
    path = args.get("path", "")
    path = _assert_in_workfolder(path, session.work_folder)
    if not os.path.isfile(path):
        return {"error": "not_found", "detail": f"文件不存在: {path}"}
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        text = _read_docx(path)
        if text is None:
            return {"error": "docx_unavailable", "detail": "未安装 python-docx"}
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    return {"path": path, "format": ext.lstrip("."), "content": text[:20000],
            "truncated": len(text) > 20000}


@_tool("list_documents",
       "[文档] 列出文件夹中的文档（.md/.txt/.docx）。默认工作文件夹。只读。",
       {"type": "object", "properties": {"folder": {"type": "string"}},
        "additionalProperties": False})
def _t_list_documents(session, args):
    folder = args.get("folder") or session.work_folder
    folder = _assert_in_workfolder(folder, session.work_folder)
    docs = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and os.path.splitext(name)[1].lower() in _DOC_EXTS:
                docs.append({"name": name, "path": p, "size": os.path.getsize(p)})
    return {"documents": docs, "count": len(docs)}


@_tool("append_document",
       "[文档] 向已有文档追加内容（.md/.txt；.docx 追加段落）。不覆盖原内容。",
       {"type": "object", "properties": {
           "filename": {"type": "string"}, "content": {"type": "string"},
           "folder": {"type": "string"}},
        "required": ["filename", "content"], "additionalProperties": False})
def _t_append_document(session, args):
    filename = (args.get("filename") or "").strip()
    content = args.get("content") or ""
    folder = args.get("folder") or None
    path = _resolve_doc_path(session, filename, folder)
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".docx":
        try:
            from docx import Document as _D
        except ImportError:
            return {"error": "docx_unavailable"}
        d = _D(path) if os.path.exists(path) else _D()
        for line in content.split("\n"):
            d.add_paragraph(line)
        d.save(path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    return {"ok": True, "path": path}


# ══════════════════════════════════════════════════════════════════════
# P2: Other tools (organize / exif / compare / export / upload / ask_user)
# ══════════════════════════════════════════════════════════════════════

@_tool("copy_photo",
       "[文件操作] 复制照片到工作文件夹内的子文件夹（整理用，不动原图）。",
       {"type": "object", "properties": {
           "src": {"type": "string"}, "dest_folder": {"type": "string"},
           "new_name": {"type": "string", "description": "可选新文件名"}},
        "required": ["src", "dest_folder"], "additionalProperties": False})
def _t_copy_photo(session, args):
    import shutil
    src = args.get("src", "")
    _src = _assert_in_workfolder_or_approved(src, session, "copy_photo", "复制源路径")
    if not os.path.isfile(_src):
        return {"error": "not_found", "detail": src}
    dest_folder = _assert_in_workfolder_or_approved(
        args.get("dest_folder", ""), session, "copy_photo", "复制目标文件夹")
    os.makedirs(dest_folder, exist_ok=True)
    name = args.get("new_name") or os.path.basename(_src)
    dest = _assert_in_workfolder_or_approved(
        os.path.join(dest_folder, name), session, "copy_photo", "复制目标路径")
    shutil.copy2(_src, dest)
    photo_scorer.invalidate_dir_cache(dest_folder)
    return {"ok": True, "dest": dest}


@_tool("move_photo",
       "[文件操作⚠] 移动原图到工作文件夹内的子文件夹。⚠️会移动原图。",
       {"type": "object", "properties": {
           "src": {"type": "string"}, "dest_folder": {"type": "string"},
           "new_name": {"type": "string"}},
        "required": ["src", "dest_folder"], "additionalProperties": False},
       destructive=True)
def _t_move_photo(session, args):
    import shutil
    src = args.get("src", "")
    _assert_in_workfolder(src, session.work_folder)
    if not os.path.isfile(src):
        return {"error": "not_found", "detail": src}
    dest_folder = _assert_in_workfolder(args.get("dest_folder", ""), session.work_folder)
    name = args.get("new_name") or os.path.basename(src)
    dest = _assert_in_workfolder(os.path.join(dest_folder, name), session.work_folder)
    summary = f"移动 {os.path.basename(src)} → {dest_folder}"
    if not session.auto_approve:
        if not _request_confirmation(session, "move_photo", args, summary):
            return {"error": "denied_by_user"}
    if session.cancel_event.is_set():
        return {"error": "cancelled"}
    os.makedirs(dest_folder, exist_ok=True)
    shutil.move(src, dest)
    photo_scorer.invalidate_dir_cache(src, dest_folder)
    _record_undo(session, "move_photo", args,
                 {"action": "move_back", "files": [(src, dest)]})
    return {"ok": True, "dest": dest}


@_tool("create_folder",
       "[文件操作] 在工作文件夹内创建子文件夹。",
       {"type": "object", "properties": {"name": {"type": "string", "description": "子文件夹名（可含相对子路径）"}},
        "required": ["name"], "additionalProperties": False})
def _t_create_folder(session, args):
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name 不能为空"}
    path = _assert_in_workfolder(os.path.join(session.work_folder, name), session.work_folder)
    os.makedirs(path, exist_ok=True)
    return {"ok": True, "path": path}


@_tool("organize_by_score",
       "[筛选导出] 按评分段把照片分入子文件夹（如 优秀/良好/一般）。基于缓存评分。mode=copy 不动原图，mode=move 移动原图(需确认)。",
       {"type": "object", "properties": {
           "thresholds": {"type": "object", "description": "如 {\"优秀\":7,\"良好\":5}；低于最低档归入「一般」"},
           "folder": {"type": "string", "description": "源文件夹，省略用工作文件夹"},
           "mode": {"type": "string", "enum": ["copy", "move"], "default": "copy"}},
        "required": ["thresholds"], "additionalProperties": False})
def _t_organize_by_score(session, args):
    import shutil
    folder = args.get("folder") or session.work_folder
    _assert_in_workfolder(folder, session.work_folder)
    thresholds = args.get("thresholds") or {}
    if not isinstance(thresholds, dict) or not thresholds:
        return {"error": "thresholds 必须是非空对象，如 {\"优秀\":7,\"良好\":5}"}
    mode = args.get("mode", "copy")
    move = (mode == "move")
    if move and not session.auto_approve:
        if not _request_confirmation(session, "organize_by_score", args,
                                     f"按分数移动照片到子文件夹（{list(thresholds.keys())}）"):
            return {"error": "denied_by_user"}
    # buckets sorted by threshold desc
    buckets = sorted(thresholds.items(), key=lambda kv: float(kv[1]), reverse=True)
    res = photo_scorer.handle_get_scored_results({"folder": folder})
    results = res.get("results", [])
    counts = {name: 0 for name, _ in buckets}
    counts["一般"] = 0
    moved = 0
    mutated_dirs = set()
    for r in results:
        p = r.get("path")
        score = r.get("score", 0) or 0
        if not p or not os.path.isfile(p):
            continue
        target_name = "一般"
        for name, thr in buckets:
            if score >= float(thr):
                target_name = name
                break
        dest_folder = os.path.join(folder, target_name)
        _assert_in_workfolder(dest_folder, session.work_folder)
        os.makedirs(dest_folder, exist_ok=True)
        dest = os.path.join(dest_folder, os.path.basename(p))
        try:
            if move:
                shutil.move(p, dest)
            else:
                shutil.copy2(p, dest)
            counts[target_name] += 1
            moved += 1
            mutated_dirs.add(os.path.dirname(p))
            mutated_dirs.add(dest_folder)
        except Exception as e:
            send_log(f"[agent] organize 失败 {p}: {e}", "warn")
    if mutated_dirs:
        photo_scorer.invalidate_dir_cache(*mutated_dirs)
    return {"ok": True, "mode": mode, "counts": counts, "total": moved}


@_tool("get_exif",
       "[照片分析] 读取照片完整 EXIF（机型/镜头/焦距/光圈/ISO/曝光时间/拍摄时间等）。只读。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_get_exif(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    if not os.path.isfile(path):
        return {"error": "not_found"}
    try:
        from PIL import Image, ExifTags
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return {"path": path, "exif": {}}
        out = {}
        for k, v in exif.items():
            try:
                out[ExifTags.TAGS.get(k, str(k))] = str(v)
            except Exception:
                out[str(k)] = str(v)
        return {"path": path, "exif": out}
    except Exception as e:
        return {"error": str(e)}


@_tool("view_photo",
       "[系统] 查看一张照片的图像内容（需要启用多模态模型）。将照片编码为 base64 发送给视觉模型进行分析。仅当多模态已开启且使用支持视觉的模型时有效。",
       {"type": "object", "properties": {
           "path": {"type": "string", "description": "照片路径"},
           "question": {"type": "string", "default": "请描述这张照片的内容",
                        "description": "你想问关于这张照片的问题"}},
        "required": ["path"], "additionalProperties": False})
def _t_view_photo(session, args):
    path = args.get("path", "")
    question = args.get("question", "请描述这张照片的内容")
    _assert_in_workfolder(path, session.work_folder)
    if not os.path.isfile(path):
        return {"error": "not_found"}
    # Check if multimodal is enabled (本地 Qwen3-VL 自动可用视觉，无需开关)
    cfg = _load_agent_config()
    if not vision_available(cfg):
        return {"error": "multimodal_disabled",
                "detail": "多模态功能未开启。请在 AI 助手设置中启用「多模态（视觉/音频）」，或启用本地模型。",
                "note": "部分模型不支持多模态输入，开启可能导致请求被拒。"}
    try:
        import base64
        import io as _io
        size_bytes = os.path.getsize(path)
        b64 = None
        mime = "image/jpeg"
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        # PIL 直读分支:小体积的常规格式
        if (size_bytes <= 10 * 1024 * 1024
                and ext in ("jpg", "jpeg", "png", "webp", "gif", "bmp")):
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}.get(ext, "image/jpeg")
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        if b64 is None:
            # RAW(65MB ARW 等)或超 10MB:解码 → 压到 1024px JPEG。
            # 多模态看内容不需要原始分辨率——此前 >10MB 一律拒看,
            # 视觉大脑在 RAW 工作流里形同虚设(2026-09-12 实测发现)
            from photo_scorer import _load_image_rgb
            img = _load_image_rgb(path)
            if img is None:
                return {"error": "decode_failed", "detail": "无法解码该图像文件"}
            img.thumbnail((1024, 1024))
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            size_bytes = buf.getbuffer().nbytes
        # Inject a user message with the image + question so the LLM can "see" it
        image_block = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}},
                {"type": "text", "text": question},
            ],
        }
        with session.messages_lock:
            session.messages.append(image_block)
        return {"ok": True, "path": path, "size_bytes": size_bytes,
                "mime": mime, "note": "照片已发送给视觉模型，将根据你的问题进行分析"}
    except Exception as e:
        return {"error": str(e)}


@_tool("compare_photos",
       "[照片分析] 比较两张照片的相似度（深度特征余弦）+ 各自尺寸/分数。只读。",
       {"type": "object", "properties": {"path_a": {"type": "string"}, "path_b": {"type": "string"}},
        "required": ["path_a", "path_b"], "additionalProperties": False})
def _t_compare_photos(session, args):
    import numpy as np
    a = args.get("path_a", ""); b = args.get("path_b", "")
    _assert_in_workfolder(a, session.work_folder); _assert_in_workfolder(b, session.work_folder)
    fa = photo_scorer.extract_deep_feature(photo_scorer._load_image_rgb(a))
    fb = photo_scorer.extract_deep_feature(photo_scorer._load_image_rgb(b))
    if fa is None or fb is None:
        return {"error": "feature_unavailable", "detail": "模型未加载或无特征头"}
    sim = float(np.dot(fa, fb))
    return {"similarity": round(sim, 4), "path_a": a, "path_b": b}


@_tool("export_image",
       "[筛选导出] 缩放/转格式导出图片到子文件夹（不覆盖原图）。Pillow 处理。",
       {"type": "object", "properties": {
           "path": {"type": "string"}, "max_width": {"type": "integer", "description": "最大宽度像素，按比例缩放"},
           "format": {"type": "string", "enum": ["JPEG", "PNG", "WEBP"], "default": "JPEG"},
           "quality": {"type": "integer", "default": 95},
           "folder": {"type": "string", "description": "输出子文件夹，默认「导出」"}},
        "required": ["path"], "additionalProperties": False})
def _t_export_image(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    if not os.path.isfile(path):
        return {"error": "not_found"}
    try:
        from PIL import Image
        img = Image.open(path)
        if img.mode in ("RGBA", "P") and (args.get("format", "JPEG") or "JPEG") == "JPEG":
            img = img.convert("RGB")
        mw = int(args.get("max_width") or 0)
        if mw and img.width > mw:
            h = int(img.height * mw / img.width)
            img = img.resize((mw, h), Image.LANCZOS)
        folder = _assert_in_workfolder(args.get("folder") or os.path.join(session.work_folder, "导出"), session.work_folder)
        os.makedirs(folder, exist_ok=True)
        fmt = (args.get("format") or "JPEG").upper()
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(fmt, ".jpg")
        base = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(folder, base + "_export" + ext)
        out = _assert_in_workfolder(out, session.work_folder)
        img.save(out, format=fmt, quality=int(args.get("quality", 95)))
        return {"ok": True, "path": out, "size": os.path.getsize(out)}
    except Exception as e:
        return {"error": str(e)}


@_tool("upload_to_cloud",
       "[云端] 上传照片到云端相册（需已登录）。批量上传 file_paths。",
       {"type": "object", "properties": {
           "file_paths": {"type": "array", "items": {"type": "string"}},
           "category": {"type": "string", "default": "pending"}},
        "required": ["file_paths"], "additionalProperties": False})
def _t_upload_to_cloud(session, args):
    paths = args.get("file_paths", []) or []
    _validate_path_list(paths, session.work_folder)
    if not paths:
        return {"error": "no files"}
    token = session.cloud_token
    if not token:
        return {"error": "not_logged_in", "detail": "未登录云端，请在软件中登录后再上传"}
    try:
        from cloud_uploader import upload_photo_auto
    except Exception as e:
        return {"error": "uploader_unavailable", "detail": str(e)}
    server = "https://studio.dwphoto.top"
    category = args.get("category", "pending")
    success = 0; failed = 0; uploaded = []
    for p in paths:
        try:
            upload_photo_auto(server, token, p, category=category)
            success += 1; uploaded.append(p)
        except Exception as e:
            failed += 1
            send_log(f"[agent] 上传失败 {p}: {e}", "warn")
    return {"total": len(paths), "success": success, "failed": failed, "uploaded": uploaded}


@_tool("set_keep",
       "[筛选导出] 标记一张照片为「保留」（人工覆盖评分筛选）。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_set_keep(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    photo_scorer.handle_set_manual_keep({"path": path, "keep": 1})
    return {"ok": True, "path": path, "keep": 1}


@_tool("set_reject",
       "[筛选导出] 标记一张照片为「排除」（人工覆盖评分筛选）。",
       {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False})
def _t_set_reject(session, args):
    path = args.get("path", "")
    _assert_in_workfolder(path, session.work_folder)
    photo_scorer.handle_set_manual_keep({"path": path, "keep": -1})
    return {"ok": True, "path": path, "keep": -1}


@_tool("search_knowledge_base",
       "[文档] 检索本地知识库（摄影师的规范/风格指南/客户偏好等 markdown 文档），返回最相关的片段。处理任务前可先查规范。",
       {"type": "object", "properties": {
           "query": {"type": "string", "description": "检索关键词"},
           "top_k": {"type": "integer", "default": 3}},
        "required": ["query"], "additionalProperties": False})
def _t_search_kb(session, args):
    query = args.get("query") or ""
    top_k = int(args.get("top_k", 3) or 3)
    results = _kb_search(query, top_k)
    return {"results": results, "total": len(results)}


@_tool("collect_preference",
       "[审美] 记录摄影师的个性化审美偏好，保存到知识库供后续选片复用。",
       {"type": "object", "properties": {
           "style": {"type": "string", "description": "偏好风格描述，如喜欢的色调、亮度、氛围"},
           "priorities": {"type": "string", "description": "筛选优先级，如'人像 > 构图 > 创意'"},
           "threshold_hint": {"type": "number", "description": "建议的默认评分阈值 1-10"},
           "notes": {"type": "string", "description": "其他备注"}},
        "additionalProperties": False})
def _t_collect_preference(session, args):
    style = (args.get("style") or "").strip()
    priorities = (args.get("priorities") or "").strip()
    threshold_hint = args.get("threshold_hint")
    notes = (args.get("notes") or "").strip()
    lines = ["# 摄影师审美偏好", ""]
    if style:
        lines.append(f"## 风格偏好\n{style}")
    if priorities:
        lines.append(f"\n## 筛选优先级\n{priorities}")
    if threshold_hint is not None:
        lines.append(f"\n## 默认阈值\n{threshold_hint}")
    if notes:
        lines.append(f"\n## 备注\n{notes}")
    if not any([style, priorities]):
        return {"error": "至少需要 style 或 priorities"}
    content = "\n".join(lines) + "\n"
    result = handle_agent_kb_save({"name": "user-aesthetic-preferences", "content": content})
    return {"ok": True, "saved": result, "preview": content[:200]}


@_tool("get_preference_profile",
       "[审美] 查看当前保存的个性化审美偏好和评分校准状态。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_get_preference_profile(session, args):
    profile = {"preferences_configured": False, "feedback_count": 0, "calibration": None}
    # Read KB preference doc
    doc = _kb_doc_by_name("user-aesthetic-preferences")
    if doc and doc.get("content", "").strip():
        profile["preferences_configured"] = True
        profile["preference_doc"] = doc["content"][:500]
    # Read agent config preferences
    cfg = _get_agent_cfg()
    prefs_text = (cfg.get("preferences") or "").strip()
    if prefs_text:
        profile["preferences_text"] = prefs_text
        profile["preferences_configured"] = True
    # Count manual feedback signals
    try:
        import score_cache
        all_scores = score_cache.get_all()
        keeps = sum(1 for s in all_scores if s.get("manual_keep", 0) == 1)
        rejects = sum(1 for s in all_scores if s.get("manual_keep", 0) == -1)
        profile["feedback_count"] = keeps + rejects
        profile["keeps"] = keeps
        profile["rejects"] = rejects
    except Exception:
        pass
    return profile


@_tool("analyze_feedback",
       "[审美] 分析用户的手动纠偏记录（set_keep/set_reject），从中提取潜在审美偏好模式。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_analyze_feedback(session, args):
    try:
        import score_cache
        all_scores = score_cache.get_all()
        keeps = [s for s in all_scores if s.get("manual_keep", 0) == 1]
        rejects = [s for s in all_scores if s.get("manual_keep", 0) == -1]
        # Stats
        keep_avg_score = sum(s.get("score", 0) for s in keeps) / max(len(keeps), 1)
        reject_avg_score = sum(s.get("score", 0) for s in rejects) / max(len(rejects), 1)
        # Pattern: if user rejects high-scored photos, they may prefer a different style
        high_score_rejects = [s for s in rejects if s.get("score", 0) >= 7.0]
        low_score_keeps = [s for s in keeps if s.get("score", 0) <= 4.0]
        patterns = []
        if high_score_rejects:
            patterns.append(f"用户拒绝了 {len(high_score_rejects)} 张高分照片（≥7分），说明 AI 评分与用户偏好有偏差")
        if low_score_keeps:
            patterns.append(f"用户保留了 {len(low_score_keeps)} 张低分照片（≤4分），说明这些照片有 AI 未捕捉的价值")
        if keeps and rejects:
            if keep_avg_score < reject_avg_score:
                patterns.append("用户更看重非评分因素：保留的照片平均分反而低于拒绝的照片")
            else:
                patterns.append(f"用户评分倾向与 AI 基本一致（保留均分 {keep_avg_score:.1f} vs 拒绝均分 {reject_avg_score:.1f}）")
        return {
            "total_feedback": len(keeps) + len(rejects),
            "keeps": len(keeps), "rejects": len(rejects),
            "keep_avg_score": round(keep_avg_score, 2),
            "reject_avg_score": round(reject_avg_score, 2),
            "high_score_rejects": len(high_score_rejects),
            "low_score_keeps": len(low_score_keeps),
            "patterns": patterns,
            "suggestion": "建议与用户确认：是否需要调整评分阈值或偏好设置？" if patterns else "暂无足够的反馈数据来发现模式",
        }
    except Exception as e:
        return {"error": "analyze_failed", "detail": str(e)}


@_tool("record_comparison",
       "[审美] 记录一次成对比较：用户从两张照片中选择了更好的那张。用于学习个人审美偏好。",
       {"type": "object", "properties": {
           "photo_a": {"type": "string", "description": "照片 A 的路径"},
           "photo_b": {"type": "string", "description": "照片 B 的路径"},
           "winner": {"type": "string", "enum": ["a", "b", "draw"], "description": "用户认为更好的照片"},
           "comment": {"type": "string", "description": "用户备注（可选）"}},
        "required": ["photo_a", "photo_b", "winner"], "additionalProperties": False})
def _t_record_comparison(session, args):
    from preference_engine import get_profile_manager
    pm = get_profile_manager()
    profile = pm.get_active()
    elo = profile["elo_engine"]
    elo.compare(args["photo_a"], args["photo_b"], args["winner"])
    pm.save()
    return {"ok": True, "comparison_count": elo.comparison_count(),
            "ratings_count": len(elo.get_ratings())}


@_tool("get_calibration_status",
       "[审美] 查看当前的个性化评分校准状态：已收集多少反馈、校准参数、Elo 评分等信息。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_get_calibration_status(session, args):
    from preference_engine import get_profile_manager
    pm = get_profile_manager()
    profile = pm.get_active()
    cal = profile["calibration"]
    elo = profile["elo_engine"]
    fw = profile["feature_weights"]
    result = {
        "profile": profile.get("name", "default"),
        "calibration": {
            "ready": cal.is_ready(),
            "pairs": cal.pair_count(),
            "params": cal.to_dict().get("params"),
        },
        "elo": {
            "comparisons": elo.comparison_count(),
            "rated_photos": len(elo.get_ratings()),
            "top10": elo.get_top(10),
        },
        "feature_weights": {
            "samples": fw.sample_count(),
            "ready": fw.sample_count() >= 10,
        },
    }
    # Add feedback stats from score_cache
    try:
        import score_cache
        all_scores = score_cache.get_all()
        keeps = sum(1 for s in all_scores if s.get("manual_keep", 0) == 1)
        rejects = sum(1 for s in all_scores if s.get("manual_keep", 0) == -1)
        result["feedback"] = {"keeps": keeps, "rejects": rejects}
    except Exception:
        result["feedback"] = {"keeps": 0, "rejects": 0}
    return result


@_tool("list_preference_profiles",
       "[审美] 列出所有已保存的审美偏好 profile。每个 profile 有独立的校准参数和 Elo 评分。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_list_profiles(session, args):
    from preference_engine import get_profile_manager
    pm = get_profile_manager()
    return {"profiles": pm.list_profiles(), "active": pm._active}


@_tool("switch_preference_profile",
       "[审美] 切换到指定的审美偏好 profile。不同场景（婚礼/商业/旅行）可使用不同 profile。",
       {"type": "object", "properties": {
           "profile_id": {"type": "string", "description": "要切换到的 profile ID"}},
        "required": ["profile_id"], "additionalProperties": False})
def _t_switch_profile(session, args):
    from preference_engine import get_profile_manager
    pm = get_profile_manager()
    pid = args["profile_id"]
    try:
        profile = pm.load(pid)
        return {"ok": True, "profile": profile.get("name", pid),
                "calibration_pairs": profile["calibration"].pair_count()}
    except Exception as e:
        return {"error": "switch_failed", "detail": str(e)}


@_tool("ask_user",
       "[流程] 向用户提问以澄清需求（仅交互模式可用）。问一个明确的问题，等待用户回答。无人值守任务不要调用。",
       {"type": "object", "properties": {"question": {"type": "string"}},
        "required": ["question"], "additionalProperties": False})
def _t_ask_user(session, args):
    if session.headless:
        return {"error": "cannot_ask_in_headless", "detail": "无人值守任务无法提问，请自行合理决策"}
    question = (args.get("question") or "").strip()
    if not question:
        return {"error": "question 不能为空"}
    request_id = uuid.uuid4().hex
    entry = {"event": threading.Event(), "answer": None}
    session.pending_questions[request_id] = entry
    _emit(session, "agent_ask_user", {"request_id": request_id, "question": question})
    entry["event"].wait(timeout=300)  # 5 min timeout
    answer = entry["answer"]
    session.pending_questions.pop(request_id, None)
    if answer is None:
        return {"error": "no_answer", "detail": "用户未回答（超时）"}
    return {"answer": answer}


# ══════════════════════════════════════════════════════════════════════
# Cloud API helper — shared by all cloud-management tools
# ══════════════════════════════════════════════════════════════════════

CLOUD_SERVER = OFFICIAL_SERVER_URL  # "https://studio.dwphoto.top"

def _cloud_api(token, method, path, body=None):
    """Generic cloud API call. Returns (status_code, data_dict)."""
    url = f"{CLOUD_SERVER}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("x-studio-token", token or "")
    req.add_header("Content-Type", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
        except Exception:
            err = {"error": str(e)}
        return e.code, err


def _require_cloud_token(session):
    """Return (token, error_dict). If logged in, error_dict is None."""
    token = getattr(session, 'cloud_token', '') or ''
    if not token:
        return '', {"error": "not_logged_in", "detail": "请先登录云端账号（在软件首页右上角登录）"}
    return token, None


# ══════════════════════════════════════════════════════════════════════
# Cloud album tools
# ══════════════════════════════════════════════════════════════════════

@_tool("list_cloud_albums",
       "[云端相册] 列出当前用户在云端的所有相册。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_list_cloud_albums(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "GET", "/api/albums")
    if status != 200:
        return {"error": f"http_{status}", "detail": data}
    albums = data.get("albums", []) or []
    return {
        "count": len(albums),
        "albums": [{k: a.get(k) for k in ("id","title","photoCount","createdAt","accessCode") if k in a}
                    for a in albums],
    }


@_tool("create_cloud_album",
       "[云端相册] 创建新的云端相册，可指定标题、初始照片列表、访问密码和过期时间。",
       {"type": "object", "properties": {
           "title": {"type": "string", "description": "相册标题"},
           "photoIds": {"type": "array", "items": {"type": "string"}, "description": "初始照片 ID 列表（可选）"},
           "password": {"type": "string", "description": "访问密码（可选，不设则无需密码）"},
           "expiresAt": {"type": "string", "description": "过期时间 ISO 8601 格式（可选）"}},
        "required": ["title"], "additionalProperties": False})
def _t_create_cloud_album(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    body = {"title": args.get("title", "")}
    if args.get("photoIds"):
        body["photoIds"] = args["photoIds"]
    if args.get("password"):
        body["password"] = args["password"]
    if args.get("expiresAt"):
        body["expiresAt"] = args["expiresAt"]
    status, data = _cloud_api(token, "POST", "/api/albums", body)
    if status == 201 or status == 200:
        return {"ok": True, "album": data.get("album", data)}
    return {"error": f"http_{status}", "detail": data}


@_tool("get_cloud_album",
       "[云端相册] 获取单个云端相册的详细信息，包含照片列表。",
       {"type": "object", "properties": {
           "albumId": {"type": "string", "description": "相册 ID"}},
        "required": ["albumId"], "additionalProperties": False})
def _t_get_cloud_album(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "GET", f"/api/albums/{args['albumId']}")
    if status != 200:
        return {"error": f"http_{status}", "detail": data}
    album = data.get("album", data)
    photos = album.get("photos", []) or []
    # Simplify photo list for LLM readability
    album["photos"] = [{k: p.get(k) for k in ("id","filename","category","score","tags") if k in p}
                       for p in photos[:50]]
    album["photoCount"] = len(photos)
    return {"album": album}


@_tool("update_cloud_album",
       "[云端相册] 更新云端相册中的照片列表（替换为新的 photoIds 列表）。",
       {"type": "object", "properties": {
           "albumId": {"type": "string", "description": "相册 ID"},
           "photoIds": {"type": "array", "items": {"type": "string"}, "description": "要放入相册的照片 ID 列表"}},
        "required": ["albumId", "photoIds"], "additionalProperties": False})
def _t_update_cloud_album(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "PUT", f"/api/albums/{args['albumId']}/photos",
                               {"photoIds": args["photoIds"]})
    if status == 200:
        return {"ok": True, "album": data.get("album", data)}
    return {"error": f"http_{status}", "detail": data}


@_tool("delete_cloud_album",
       "[云端相册⚠] 删除一个云端相册。⚠️ 不可撤销。只有相册所有者可以删除。",
       {"type": "object", "properties": {
           "albumId": {"type": "string", "description": "要删除的相册 ID"}},
        "required": ["albumId"], "additionalProperties": False},
       destructive=True)
def _t_delete_cloud_album(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    if not session.auto_approve:
        summary = f"删除云端相册 {args['albumId']}"
        if not _request_confirmation(session, "delete_cloud_album", args, summary):
            return {"error": "denied_by_user"}
    status, data = _cloud_api(token, "DELETE", f"/api/albums/{args['albumId']}")
    if status == 200:
        return {"ok": True, "deleted": args["albumId"]}
    return {"error": f"http_{status}", "detail": data}


@_tool("share_cloud_album",
       "[云端相册] 为云端相册生成分享链接和访问码，可发送给客户查看。",
       {"type": "object", "properties": {
           "albumId": {"type": "string", "description": "要分享的相册 ID"}},
        "required": ["albumId"], "additionalProperties": False})
def _t_share_cloud_album(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "POST", f"/api/albums/{args['albumId']}/share")
    if status == 200:
        return {"ok": True, "shareUrl": data.get("shareUrl", ""),
                "accessCode": data.get("accessCode", "")}
    return {"error": f"http_{status}", "detail": data}


# ══════════════════════════════════════════════════════════════════════
# Cloud photo tools
# ══════════════════════════════════════════════════════════════════════

@_tool("list_cloud_photos",
       "[云端] 搜索/列出云端照片。支持按分类、评分、关键词筛选和分页。",
       {"type": "object", "properties": {
           "page": {"type": "integer", "default": 1},
           "limit": {"type": "integer", "default": 20},
           "category": {"type": "string", "description": "按分类筛选（如 wedding, portrait）"},
           "minScore": {"type": "number", "description": "最低评分筛选"},
           "maxScore": {"type": "number", "description": "最高评分筛选"},
           "search": {"type": "string", "description": "文件名搜索关键词"},
           "sort": {"type": "string", "enum": ["newest","oldest","highest","lowest"], "default": "newest"}},
        "additionalProperties": False})
def _t_list_cloud_photos(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    qs_parts = [f"page={args.get('page',1)}", f"limit={min(int(args.get('limit',20)),50)}"]
    if args.get("category"):
        qs_parts.append(f"category={urllib.parse.quote(args['category'])}")
    if args.get("minScore") is not None:
        qs_parts.append(f"minScore={args['minScore']}")
    if args.get("maxScore") is not None:
        qs_parts.append(f"maxScore={args['maxScore']}")
    if args.get("search"):
        qs_parts.append(f"search={urllib.parse.quote(args['search'])}")
    if args.get("sort"):
        qs_parts.append(f"sort={args['sort']}")
    path = f"/api/photos?{'&'.join(qs_parts)}"
    status, data = _cloud_api(token, "GET", path)
    if status != 200:
        return {"error": f"http_{status}", "detail": data}
    photos = data.get("photos", []) or []
    return {
        "count": len(photos),
        "total": data.get("total", len(photos)),
        "page": args.get("page", 1),
        "photos": [{k: p.get(k) for k in ("id","filename","category","score","tags","width","height","createdAt") if k in p}
                    for p in photos],
    }


@_tool("update_cloud_photo",
       "[云端] 修改云端照片的元数据：分类(category)、标签(tags)、评分(score)。",
       {"type": "object", "properties": {
           "photoId": {"type": "string", "description": "照片 ID"},
           "category": {"type": "string", "description": "新分类"},
           "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
           "score": {"type": "number", "description": "新评分"}},
        "required": ["photoId"], "additionalProperties": False})
def _t_update_cloud_photo(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    body = {}
    for k in ("category", "tags", "score"):
        if args.get(k) is not None:
            body[k] = args[k]
    if not body:
        return {"error": "nothing_to_update", "detail": "请至少指定 category、tags 或 score 之一"}
    status, data = _cloud_api(token, "PUT", f"/api/photos/{args['photoId']}", body)
    if status == 200:
        return {"ok": True, "photo": data.get("photo", data)}
    return {"error": f"http_{status}", "detail": data}


@_tool("delete_cloud_photo",
       "[云端⚠] 从云端删除一张照片。⚠️ 不可撤销。",
       {"type": "object", "properties": {
           "photoId": {"type": "string", "description": "要删除的照片 ID"}},
        "required": ["photoId"], "additionalProperties": False},
       destructive=True)
def _t_delete_cloud_photo(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    if not session.auto_approve:
        summary = f"删除云端照片 {args['photoId']}"
        if not _request_confirmation(session, "delete_cloud_photo", args, summary):
            return {"error": "denied_by_user"}
    status, data = _cloud_api(token, "DELETE", f"/api/photos/{args['photoId']}")
    if status == 200:
        return {"ok": True, "deleted": args["photoId"]}
    return {"error": f"http_{status}", "detail": data}


@_tool("batch_categorize_photos",
       "[云端] 批量将多张云端照片归类到同一分类。",
       {"type": "object", "properties": {
           "photoIds": {"type": "array", "items": {"type": "string"}, "description": "照片 ID 列表"},
           "category": {"type": "string", "description": "目标分类名称"}},
        "required": ["photoIds", "category"], "additionalProperties": False})
def _t_batch_categorize_photos(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "POST", "/api/photos/batch",
                               {"action": "categorize", "ids": args["photoIds"],
                                "category": args["category"]})
    if status == 200:
        return {"ok": True, "affected": data.get("affected", len(args["photoIds"]))}
    return {"error": f"http_{status}", "detail": data}


# ══════════════════════════════════════════════════════════════════════
# Cloud stats tool
# ══════════════════════════════════════════════════════════════════════

@_tool("get_cloud_stats",
       "[云端相册] 获取云端存储统计信息：总照片数、存储用量、平均评分等。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_get_cloud_stats(session, args):
    token, err = _require_cloud_token(session)
    if err: return err
    status, data = _cloud_api(token, "GET", "/api/stats")
    if status == 200:
        return {"stats": data}
    if status == 403:
        # /api/stats is admin-only — fall back to the user-scoped view
        s2, me = _cloud_api(token, "GET", "/api/auth/me")
        if s2 == 200:
            sub = me.get("subscription") or {}
            return {"stats": {"scope": "user", "plan": sub.get("plan"),
                              "storageUsed": sub.get("storageUsed"),
                              "storageLimit": sub.get("storageLimit"),
                              "expiresAt": sub.get("expiresAt")}}
    return {"error": f"http_{status}", "detail": data}


# ══════════════════════════════════════════════════════════════════════
# Self-extension tools — let the LLM manage MCP servers & skills
# ══════════════════════════════════════════════════════════════════════

@_tool("add_mcp_server",
       "[系统] 添加或更新一个 MCP 服务器配置（如可灵 AI 生图服务）。配置后重启 Agent 会话即可使用新工具。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "服务器名称，如 kling-ai"},
           "command": {"type": "string", "description": "启动命令，如 npx"},
           "args": {"type": "array", "items": {"type": "string"}, "description": "命令参数，如 [\"-y\", \"mcp-kling\"]"},
           "env": {"type": "object", "description": "环境变量，如 {\"KLING_API_KEY\": \"...\"}" },
           "type": {"type": "string", "enum": ["stdio", "http"], "default": "stdio"}},
        "required": ["name", "command"], "additionalProperties": False})
def _t_add_mcp_server(session, args):
    name = args["name"].strip()
    if not session.auto_approve:
        if not _request_confirmation(session, "add_mcp_server", args,
                f"添加 MCP 服务器: {name} ({args.get('command', '')})"):
            return {"error": "denied_by_user"}
    cfg = {"command": args.get("command", ""), "args": args.get("args") or [],
           "type": args.get("type", "stdio")}
    if args.get("env"):
        cfg["env"] = args["env"]
    agent_cfg = _get_agent_cfg()
    servers = agent_cfg.get("mcpServers", {}) or {}
    servers[name] = cfg
    agent_cfg["mcpServers"] = servers
    _service._app_config["agent"] = agent_cfg
    _service._save_config_to_disk()
    send_log(f"[agent] MCP server '{name}' added by LLM", "info")
    # Try connecting immediately
    mcp = get_mcp_manager()
    if mcp.available:
        result = mcp.connect_all(agent_cfg)
        return {"ok": True, "name": name, "connection": result.get(name, "unknown")}
    return {"ok": True, "name": name, "note": "已保存，重启 Agent 会话后生效"}


@_tool("remove_mcp_server",
       "[系统⚠] 删除一个 MCP 服务器配置。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "要删除的服务器名称"}},
        "required": ["name"], "additionalProperties": False},
       destructive=True)
def _t_remove_mcp_server(session, args):
    name = args["name"].strip()
    if not session.auto_approve:
        if not _request_confirmation(session, "remove_mcp_server", args,
                f"删除 MCP 服务器: {name}"):
            return {"error": "denied_by_user"}
    agent_cfg = _get_agent_cfg()
    servers = agent_cfg.get("mcpServers", {}) or {}
    if name not in servers:
        return {"error": f"服务器 '{name}' 不存在", "available": list(servers.keys())}
    del servers[name]
    agent_cfg["mcpServers"] = servers
    _service._app_config["agent"] = agent_cfg
    _service._save_config_to_disk()
    send_log(f"[agent] MCP server '{name}' removed by LLM", "info")
    return {"ok": True, "removed": name}


@_tool("list_mcp_servers",
       "[系统] 列出已配置的 MCP 服务器及其工具。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_list_mcp_servers(session, args):
    agent_cfg = _get_agent_cfg()
    servers = agent_cfg.get("mcpServers", {}) or {}
    mcp = get_mcp_manager()
    result = []
    for name, cfg in servers.items():
        item = {"name": name, "type": cfg.get("type", "stdio"),
                "command": cfg.get("command", ""), "args": cfg.get("args", [])}
        # Include discovered tool names for connected servers
        if mcp.available:
            item["tools"] = [t.split("__", 2)[-1] for t in mcp.tools if t.startswith(f"mcp__{name}__")]
        result.append(item)
    return {"servers": result, "mcp_available": mcp.available}


@_tool("create_skill",
       "[流程] 创建或更新一个技能（工作流模板）。技能会在下次对话中自动匹配触发。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "技能名称（英文 slug，如 evening-selection）"},
           "description": {"type": "string", "description": "一句话描述"},
           "trigger": {"type": "string", "description": "触发词正则，如 选片|精选|筛选"},
           "body": {"type": "string", "description": "技能内容（Markdown），包含工作流步骤和偏好"}},
        "required": ["name", "body"], "additionalProperties": False})
def _t_create_skill(session, args):
    loader = get_skill_loader()
    loader.scan()
    imports = [s.strip() for s in (args.get("imports") or "").split(",") if s.strip()] if args.get("imports") else None
    allow_tools = [s.strip() for s in (args.get("allow_tools") or "").split(",") if s.strip()] if args.get("allow_tools") else None
    skill = loader.save(
        name=args["name"].strip(),
        description=args.get("description", ""),
        trigger=args.get("trigger", ""),
        body=args.get("body", ""),
        imports=imports,
        allow_tools=allow_tools,
    )
    send_log(f"[agent] skill '{skill.name}' created by LLM", "info")
    return {"ok": True, "name": skill.name, "description": skill.description}


@_tool("delete_skill",
       "[流程⚠] 删除一个技能。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "要删除的技能名称"}},
        "required": ["name"], "additionalProperties": False},
       destructive=True)
def _t_delete_skill(session, args):
    loader = get_skill_loader()
    loader.scan()
    name = args["name"].strip()
    if not session.auto_approve:
        if not _request_confirmation(session, "delete_skill", args,
                f"删除技能: {name}"):
            return {"error": "denied_by_user"}
    if name not in loader.skills:
        return {"error": f"技能 '{name}' 不存在", "available": list(loader.skills.keys())}
    loader.delete(name)
    send_log(f"[agent] skill '{name}' deleted by LLM", "info")
    return {"ok": True, "deleted": name}


@_tool("list_skills",
       "[流程] 列出所有已安装的技能（工作流模板）。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_list_skills(session, args):
    loader = get_skill_loader()
    loader.scan()
    return {"skills": loader.list_all()}


@_tool("activate_skill",
       "[系统] 激活一个预设技能（工作流模板），加载其完整指令到对话上下文。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "技能名称"}},
        "required": ["name"], "additionalProperties": False})
def _t_activate_skill(session, args):
    loader = get_skill_loader()
    loader.scan()
    skill = loader.get(args["name"])
    if not skill:
        names = list(loader.skills.keys())
        return {"error": f"未知技能: {args['name']}",
                "available": names}
    with session.messages_lock:
        session.messages.append(skill.system_message(loader))  # includes imports
    return {"ok": True, "name": skill.name, "description": skill.description}


# ── 拷卡 (Copy Card) tools ──

@_tool("list_drives",
       "[拷卡⚠] 扫描当前连接的移动存储设备（SD卡/读卡器/U盘），返回驱动器列表和照片文件夹信息。只读。",
       {"type": "object", "properties": {
           "scan_path": {"type": "string", "description": "要查找的文件夹名，默认 DCIM"}},
        "additionalProperties": False})
def _t_list_drives(session, args):
    scan_path = args.get("scan_path", "DCIM")
    res = copy_card.handle_list_drives({"scan_path": scan_path})
    drives = res.get("drives", [])
    if not drives:
        return {"drives": [], "message": "未检测到含有照片文件夹的移动设备"}
    summary = []
    for d in drives:
        summary.append({
            "drive": d["letter"],
            "label": d.get("label", ""),
            "has_photos": d.get("has_scan_path", False),
            "photo_path": d.get("src_path", ""),
        })
    return {"drives": summary, "total": len(summary)}


@_tool("start_monitor",
       "[拷卡] 启动后台驱动器监控，有新存储卡插入时自动检测照片文件夹并开始拷贝。",
       {"type": "object", "properties": {
           "scan_path": {"type": "string", "description": "要监控的文件夹名，如 DCIM"},
           "target_path": {"type": "string", "description": "照片拷贝目标路径，默认用工作文件夹"},
           "file_filter": {"type": "string", "description": "文件过滤: all/raw/jpeg/video，默认 all"}},
        "additionalProperties": False})
def _t_start_monitor(session, args):
    target = args.get("target_path") or session.work_folder
    if not target:
        return {"error": "请指定 target_path 或先设置工作文件夹 (set_work_folder)"}
    params = {
        "scan_path": args.get("scan_path", "DCIM"),
        "target_path": target,
        "preset_name": args.get("preset_name", ""),
        "file_filter": args.get("file_filter", "all"),
    }
    res = copy_card.handle_start_monitor(params)
    return {"ok": True, "scan_path": res.get("scan_path"), "target": target,
            "message": f"监控已启动，检测到存储卡插入时将自动拷贝到 {target}"}


@_tool("stop_monitor",
       "[拷卡] 停止后台驱动器监控。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_stop_monitor(session, args):
    return copy_card.handle_stop_monitor({})


@_tool("start_copy",
       "[拷卡⚠] 从指定驱动器拷贝照片到目标文件夹。建议先用 list_drives 确认驱动器信息。需要用户确认。",
       {"type": "object", "properties": {
           "drive": {"type": "string", "description": "驱动器标识符（从 list_drives 获取）"},
           "source_path": {"type": "string", "description": "源照片文件夹路径（从 list_drives 获取的 photo_path）"},
           "target_path": {"type": "string", "description": "拷贝目标路径，默认用工作文件夹"},
           "file_filter": {"type": "string", "description": "文件过滤: all/raw/jpeg/video，默认 all"}},
        "required": ["drive", "source_path"],
        "additionalProperties": False},
       destructive=True)
def _t_start_copy(session, args):
    target = args.get("target_path") or session.work_folder
    if not target:
        return {"error": "请指定 target_path 或先设置工作文件夹 (set_work_folder)"}
    if not session.auto_approve:
        if not _request_confirmation(session, "start_copy", args,
                f"从 {args['drive']} ({args['source_path']}) 拷贝照片到 {target}"):
            return {"error": "denied_by_user"}
    params = {
        "letter": args["drive"],
        "src_path": args["source_path"],
        "target_path": target,
        "folder_name": args.get("folder_name", ""),
        "file_filter": args.get("file_filter", "all"),
        "label": args.get("label", ""),
    }
    res = copy_card.handle_start_copy(params)
    if res.get("error"):
        return res
    return {"ok": True, "file_count": res.get("file_count", 0),
            "total_size_mb": round(res.get("total_size", 0) / (1024 * 1024), 1),
            "destination": res.get("dest", ""),
            "message": f"开始拷贝 {res.get('file_count', 0)} 个文件 → {res.get('dest', '')}"}


@_tool("cancel_copy",
       "[拷卡⚠] 取消当前正在进行的照片拷贝。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_cancel_copy(session, args):
    return copy_card.handle_cancel_copy({})


@_tool("undo_last_action",
       "[文件操作] 撤销上一步破坏性操作（删除、重命名、移动等）。可多次调用逐步回退。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_undo(session, args):
    return _do_undo(session)


# ── Automation (folder watch + scheduled tasks) ──

# Task storage helpers (shared JSON file in user data dir, mirrors frontend localStorage)
def _tasks_path():
    import service as _svc
    return os.path.join(_svc._get_config_dir(), "agent_tasks.json")

def _load_tasks():
    p = _tasks_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def _save_tasks(tasks):
    try:
        os.makedirs(os.path.dirname(_tasks_path()), exist_ok=True)
        with open(_tasks_path(), "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@_tool("list_tasks",
       "[自动化] 列出所有定时任务。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_list_tasks(session, args):
    tasks = _load_tasks()
    return {"tasks": [{"id": t.get("id",""), "name": t.get("name",""), "enabled": t.get("enabled",False),
                       "schedule": t.get("schedule",""), "prompt": (t.get("prompt","") or "")[:100]}
                      for t in tasks], "total": len(tasks)}


@_tool("create_task",
       "[自动化⚠] 创建一个定时任务（如每晚自动选片）。需用户确认。",
       {"type": "object", "properties": {
           "name": {"type": "string", "description": "任务名称"},
           "schedule": {"type": "string", "description": "cron 表达式，如 0 20 * * * 表示每晚8点"},
           "prompt": {"type": "string", "description": "任务提示词（Agent 执行的操作）"},
           "enabled": {"type": "boolean", "default": True}},
        "required": ["name", "schedule", "prompt"], "additionalProperties": False},
       destructive=True)
def _t_create_task(session, args):
    if not session.auto_approve:
        if not _request_confirmation(session, "create_task", args,
                f"创建定时任务: {args['name']} ({args['schedule']})"):
            return {"error": "denied_by_user"}
    tasks = _load_tasks()
    task = {"id": uuid.uuid4().hex, "name": args["name"], "schedule": args["schedule"],
            "prompt": args["prompt"], "enabled": args.get("enabled", True),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "auto_approve_destructive": session.auto_approve,
            "model": session.model or "",
            "work_folder": session.work_folder}
    tasks.append(task)
    _save_tasks(tasks)
    send_log(f"[agent] task created: {args['name']} ({args['schedule']})", "info")
    return {"ok": True, "task": task}


@_tool("update_task",
       "[自动化⚠] 修改一个已存在的定时任务。需用户确认。",
       {"type": "object", "properties": {
           "task_id": {"type": "string"}, "name": {"type": "string"},
           "schedule": {"type": "string"}, "prompt": {"type": "string"},
           "enabled": {"type": "boolean"}},
        "required": ["task_id"], "additionalProperties": False},
       destructive=True)
def _t_update_task(session, args):
    tasks = _load_tasks()
    tid = args["task_id"]
    for t in tasks:
        if t.get("id") == tid:
            if not session.auto_approve:
                if not _request_confirmation(session, "update_task", args,
                        f"修改定时任务: {t.get('name','')}"):
                    return {"error": "denied_by_user"}
            for key in ("name", "schedule", "prompt", "enabled"):
                if key in args and args[key] is not None:
                    t[key] = args[key]
            _save_tasks(tasks)
            send_log(f"[agent] task updated: {tid}", "info")
            return {"ok": True, "task": t}
    return {"error": "not_found", "detail": f"任务 {tid} 不存在"}


@_tool("delete_task",
       "[自动化⚠] 删除一个定时任务。需用户确认。",
       {"type": "object", "properties": {
           "task_id": {"type": "string", "description": "任务 ID（从 list_tasks 获取）"}},
        "required": ["task_id"], "additionalProperties": False},
       destructive=True)
def _t_delete_task(session, args):
    tasks = _load_tasks()
    tid = args["task_id"]
    for i, t in enumerate(tasks):
        if t.get("id") == tid:
            if not session.auto_approve:
                if not _request_confirmation(session, "delete_task", args,
                        f"删除定时任务: {t.get('name','')}"):
                    return {"error": "denied_by_user"}
            tasks.pop(i)
            _save_tasks(tasks)
            send_log(f"[agent] task deleted: {tid}", "info")
            return {"ok": True, "deleted": tid}
    return {"error": "not_found", "detail": f"任务 {tid} 不存在"}


@_tool("start_watch",
       "[自动化] 启动文件夹监听，有新照片时自动触发 AI 评分筛选。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "要监听的文件夹路径"},
           "output_folder": {"type": "string", "description": "精选照片输出目录"},
           "threshold": {"type": "number", "default": 6.0},
           "prompt": {"type": "string", "description": "触发提示词，默认'处理监听到的新照片'"}},
        "required": ["folder", "output_folder"], "additionalProperties": False})
def _t_start_watch(session, args):
    params = {"folder": args["folder"], "mode": "batch",
              "output_folder": args["output_folder"], "filter": "all",
              "threshold": float(args.get("threshold", 6.0)),
              "prompt": args.get("prompt", ""),
              "interval": 2.0, "stability_ms": 1500, "quiet_ms": 5000,
              "process_existing": False, "recursive": True,
              "auto_approve": session.auto_approve}
    res = folder_watcher.handle_start_folder_watch(params)
    if isinstance(res, dict) and res.get("ok"):
        send_log(f"[agent] folder watch started: {args['folder']}", "info")
    return res


@_tool("stop_watch",
       "[自动化] 停止文件夹监听。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_stop_watch(session, args):
    return folder_watcher.handle_stop_folder_watch({})


@_tool("watch_status",
       "[自动化] 查询文件夹监听状态。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_watch_status(session, args):
    return folder_watcher.handle_folder_watch_status({})


# ══════════════════════════════════════════════════════════════════════
# Pipeline bridge tools (数字员工管线编排, 0.8.5)
# 延迟 import pipeline_runner / pipeline_planner / pipeline_templates，
# 避免模块级循环依赖（pipeline_runner 也反向 import agent_module）。
# ══════════════════════════════════════════════════════════════════════

@_tool("pipeline_list_templates",
       "[流程] 列出所有数字员工管线模板（内置+自定义），含各阶段步骤摘要。只读。",
       {"type": "object", "properties": {}, "additionalProperties": False})
def _t_pipeline_list_templates(session, args):
    import pipeline_runner
    from pipeline_templates import list_templates as _list
    templates = _list(builtin_templates=pipeline_runner.TEMPLATES)
    out = []
    for tpl in templates:
        phases = []
        for p in tpl.get("phases", []):
            steps = "、".join(s.get("name", "?") for s in p.get("steps", []))
            phases.append(f"{p.get('title', p.get('id', '?'))}({steps})")
        out.append({
            "id": tpl.get("id", ""),
            "title": tpl.get("title", ""),
            "source": tpl.get("source", ""),
            "description": tpl.get("description", ""),
            "phases": phases,
        })
    return {"templates": out, "count": len(out)}


@_tool("pipeline_generate_template",
       "[流程] 用 LLM 按目标描述生成/修改管线模板草稿（不落盘）。返回草稿和校验警告；"
       "草稿需经用户确认后用 pipeline_save_template 保存，再 pipeline_start_run 启动。",
       {"type": "object", "properties": {
           "goal": {"type": "string", "description": "工作流目标描述，如'婚礼整场初筛，留约30%，导出 keeper'"},
           "base_template": {"type": "string", "description": "可选，基底模板ID，在其基础上修改"}},
        "required": ["goal"], "additionalProperties": False})
def _t_pipeline_generate_template(session, args):
    goal = (args.get("goal") or "").strip()
    if not goal:
        return {"error": "goal 不能为空"}
    import pipeline_planner
    # 附带工作文件夹照片数作为生成上下文（失败不影响主流程）
    folder_stats = None
    try:
        if session.work_folder:
            folder_stats = {"photo_count": len(photo_scorer.find_images(session.work_folder))}
    except Exception:
        folder_stats = None
    base = (args.get("base_template") or "").strip() or None
    return pipeline_planner.generate_template(goal, base_template_id=base,
                                              folder_stats=folder_stats)


@_tool("pipeline_save_template",
       "[流程⚠] 保存一个数字员工管线模板（写操作，需确认）。保存前会做结构校验，有错误则拒绝保存。",
       {"type": "object", "properties": {
           "template": {"type": "object", "description": "完整模板 JSON（id/title/phases...）"}},
        "required": ["template"], "additionalProperties": False},
       destructive=True)
def _t_pipeline_save_template(session, args):
    tpl = args.get("template")
    if not isinstance(tpl, dict) or not tpl:
        return {"error": "template 必须是非空对象"}
    from step_registry import validate_template
    errors, warnings = validate_template(tpl)
    if errors:
        return {"error": "validation_failed", "errors": errors, "warnings": warnings}
    if not session.auto_approve:
        summary = f"保存管线模板：{tpl.get('title') or tpl.get('id', '?')}（id={tpl.get('id', '?')}）"
        if not _request_confirmation(session, "pipeline_save_template", args, summary):
            return {"error": "denied_by_user"}
    import pipeline_templates
    result = pipeline_templates.save_template(tpl)
    if isinstance(result, dict) and warnings and "warnings" not in result:
        result["warnings"] = warnings
    return result


@_tool("pipeline_start_run",
       "[流程⚠] 启动数字员工管线（启动后按计划自动处理照片；破坏性步骤管线会自己弹门等人批准）。需确认。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "工作文件夹绝对路径"},
           "template": {"type": "string", "description": "模板ID"},
           "goal": {"type": "string", "description": "目标描述（省略则用当前任务指令）"},
           "autonomy": {"type": "string", "enum": ["L2", "L3"],
                        "description": "自主度：L3 受控自动（默认）/ L2 逐步确认"},
           "pipeline_params": {"type": "object",
                               "description": "可选运行参数（threshold/eye_reject/budget_token_cap 等）"}},
        "required": ["folder", "template"], "additionalProperties": False},
       destructive=True)
def _t_pipeline_start_run(session, args):
    folder = (args.get("folder") or "").strip()
    template = (args.get("template") or "").strip()
    if not folder:
        return {"error": "folder 不能为空"}
    if not template:
        return {"error": "template 不能为空"}
    if not session.auto_approve:
        summary = f"启动数字员工管线：模板={template}，文件夹={folder}"
        if not _request_confirmation(session, "pipeline_start_run", args, summary):
            return {"error": "denied_by_user"}
    import pipeline_runner
    # _from_agent=True：进程内调用，跳过 pipeline 侧的 agent 活跃互斥检查
    autonomy = args.get("autonomy") if args.get("autonomy") in ("L2", "L3") else "L3"
    return pipeline_runner.handle_pipeline_start({
        "work_folder": folder,
        "template": template,
        "goal": (args.get("goal") or session.prompt or "").strip(),
        "autonomy": autonomy,
        "cloud_token": getattr(session, "cloud_token", "") or "",
        "pipeline_params": args.get("pipeline_params") or {},
    }, _from_agent=True)


@_tool("pipeline_get_status",
       "[流程] 查询数字员工管线当前状态/进度/产物摘要。只读。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "可选，工作文件夹（无活跃运行时查磁盘残留状态）"}},
        "additionalProperties": False})
def _t_pipeline_get_status(session, args):
    import pipeline_runner
    params = {}
    folder = (args.get("folder") or session.work_folder or "").strip()
    if folder:
        params["work_folder"] = folder
    st = pipeline_runner.handle_pipeline_get_state(params)
    if isinstance(st, dict) and isinstance(st.get("artifacts"), dict):
        # 大列表截断，防止撑爆上下文
        st["artifacts"] = _truncate(st["artifacts"])
    return st


@_tool("pipeline_cancel_run",
       "[流程⚠] 取消正在运行的数字员工管线。需确认。",
       {"type": "object", "properties": {
           "folder": {"type": "string", "description": "可选，工作文件夹（用于清理残留状态）"}},
        "additionalProperties": False},
       destructive=True)
def _t_pipeline_cancel_run(session, args):
    if not session.auto_approve:
        if not _request_confirmation(session, "pipeline_cancel_run", args,
                                     "取消正在运行的数字员工管线"):
            return {"error": "denied_by_user"}
    import pipeline_runner
    params = {}
    folder = (args.get("folder") or session.work_folder or "").strip()
    if folder:
        params["work_folder"] = folder
    return pipeline_runner.handle_pipeline_cancel(params)


@_tool("finish",
       "[流程] 任务完成时调用，给出简明总结。",
       {"type": "object", "properties": {"summary": {"type": "string"}},
        "required": ["summary"], "additionalProperties": False})
def _t_finish(session, args):
    # handled specially in the loop; this fn is a placeholder
    return {"summary": args.get("summary", "")}


# ── OpenAI tool specs ──
def _tool_specs():
    # P0-1: 注册表工具惰性补齐——service 引导顺序里 agent_module 先于
    # 编辑器等后迁移模块加载，启动时生成会扑空；首次构建 specs 时
    # （必然晚于全部模块加载）幂等补齐，启动顺序无关。
    _register_registry_tools()
    specs = []
    for name, t in TOOL_REGISTRY.items():
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t["description"],
                "parameters": t["schema"],
            },
        })
    # Merge MCP tools if connected
    try:
        mcp = get_mcp_manager()
        if mcp.available:
            specs.extend(mcp.get_tool_schemas())
    except Exception:
        pass
    return specs


def _system_prompt(work_folder):
    wf_line = (f"你只能操作工作文件夹内的文件：{work_folder}\n"
               if work_folder
               else "当前尚未指定工作文件夹。如果用户在指令里给出了照片所在路径，先调用 set_work_folder(path) 设置（需用户确认）。\n")
    return (
        "你是一个照片工作室助手（LensPilot），运行在摄影师的工作机上。你的职责是帮助摄影师高效管理、筛选、处理照片。\n\n"
        + wf_line +
        "任何尝试访问工作文件夹之外的路径都会被系统沙箱拒绝。\n\n"
        # ── Workflow ──
        "== 工作流程建议 ==\n"
        "1. 先用只读工具了解情况：list_photos / scan_count / get_scores / get_photo_info。\n"
        "2. 若无评分，调用 score_photos 评分并导出优秀照片。\n"
        "3. 用 select_above_threshold 或 apply_selection 导出精选。\n"
        "4. 需要时用 detect_blur / detect_eyes / deep_dedup 辅助判断。\n"
        "5. 破坏性工具（delete_photo / rename_photo / move_photo / dedup_burst）会触发用户确认。\n"
        "6. 若用户在指令里给了新路径，或当前没有工作文件夹，调用 set_work_folder(path) 切换/设置（需用户确认）。\n"
        "7. 遇到「整场拍摄初筛 / 批量处理 / 选片交付」类目标时，优先用 pipeline_* 工具编排数字员工管线，"
        "而不是逐张调用工具：先 pipeline_list_templates 看现有模板；需要新流程时 pipeline_generate_template "
        "生成草稿，给用户看过后 pipeline_save_template 保存，再 pipeline_start_run 启动；"
        "运行中用 pipeline_get_status 查进度，pipeline_cancel_run 可取消。"
        "管线里的破坏性步骤会自己弹门等用户批准，你不需要逐步确认。\n"
        "导出子文件夹（如「优秀照片」「_watermarked」「_duplicates」）会自动创建在工作文件夹内。\n"
        "8. 批量压缩/格式转换/加水印/降噪/LUT/LOG 还原/联系表等照片处理，优先用内置 photo_s 工具服务器"
        "（mcp__photo_s__ 前缀，如 mcp__photo_s__process / mcp__photo_s__watermark / mcp__photo_s__contact_sheet），"
        "一次处理多张：process 的 options 支持 target_size_bytes / output_format / output_dir / watermark_text 等。"
        "只传工作文件夹内的路径。\n\n"
        # ── Safety & security ──
        "== 安全与操作规范 ==\n"
        "- 你只能在工作文件夹内操作。任何读取/写入外部路径的尝试都会被沙箱拦截。\n"
        "- 误操作可撤销：每次 delete_photo / rename_photo / move_photo 都会记录，可调用 undo_last_action 逐步回退。必要时主动告知用户这个能力。\n"
        "- 批量删除前先列出将受影响的文件清单，让用户知情。不要静默删除大量照片。\n"
        "- 遇到错误不要静默重试：工具失败时报告具体错误，让用户决定是否重试，而不是反复调用同一参数。\n"
        "- MCP 外部工具（mcp__ 前缀）来自第三方，调用前确认来自可信来源。不要将工作文件夹内的文件路径传给未经验证的 MCP 服务。\n"
        "- 不要读取、导出或泄露工作文件夹之外的配置文件、密钥、系统文件。\n"
        "- 如果你不确定某个操作的安全性，先用只读工具探查，不要直接执行破坏性操作。\n"
        "- 不要主动建议用户把大量照片移到回收站以外的位置；不要主动建议用户清空 .agent_trash 回收站目录。\n\n"
        # ── Capability honesty ──
        "== 能力边界 ==\n"
        "- 你基于 AI 模型运行，不具备真正的视觉能力（除非用户开启了多模态功能并有兼容的视觉模型）。不要假装看到了照片内容。\n"
        "- 评分依赖本地 AI 模型（ResNet 美学评分），不是真正的人类审美判断。分数供参考，最终筛选由用户决定。\n"
        "- 你无法访问互联网（除非通过 MCP 工具或云端 API），无法自主搜索在线信息。\n"
        "- 你无法修改软件设置、安装软件、访问系统配置。这些操作需要用户手动完成。\n"
        "- 不要声称软件有它不具备的功能。如果不确定某项能力是否存在，诚实告知用户。\n\n"
        # ── Legal & ethical ──
        "== 法律与伦理 ==\n"
        "- 本软件是摄影辅助工具，AI 评分和建议仅供参考，不构成专业摄影判断。最终创作决策由摄影师（用户）负责。\n"
        "- 处理客户照片时需尊重隐私：不要将照片路径、文件名、评分结果等通过 MCP 或云端工具泄露给第三方。\n"
        "- 处理照片时请遵守当地法律法规：不要删除或修改他人版权信息（EXIF 中的版权/作者字段不应被清除）。\n"
        "- 如果用户请求处理明显涉及他人隐私、肖像权、或可能违法的内容，应婉拒并建议咨询专业人士。\n"
        "- 不要生成、修改或传播虚假、误导性的图像内容（深度伪造）。不要帮助用户冒充他人拍摄风格或伪造拍摄参数。\n"
        "- AI 生成的内容（如通过 MCP 生图工具）应明确标注为 AI 生成，不要冒充真实拍摄。\n\n"
        # ── Practical notes ──
        "注意：score_photos 的结果只返回摘要（总数/选中数/平均分），不会列出每张照片。"
        "如需具体某张照片的分数，用 get_photo_info 或 get_scores。\n"
        "若当前没有工作文件夹且用户也没给路径，调用 finish(summary=...) 提示用户选择文件夹。\n"
        "完成所有任务后，必须调用 finish(summary=...) 给出中文总结，说明做了什么、结果放在哪里。\n"
        "可用 write_document 生成修图报告（.md，含处理统计、保留/删除清单、后续建议），默认存到「报告」子文件夹。\n"
        "遇到歧义或缺信息时（仅交互模式）可调用 ask_user 向用户提问；无人值守任务不要调用 ask_user，自行合理决策。\n"
        "处理前如需参考摄影规范/客户偏好，可调用 search_knowledge_base 检索本地知识库。\n"
        "== 审美个性化 ==\n"
        "- 每位摄影师的审美偏好不同（明亮/暗调、暖色/冷色、人像优先/风景优先等）。\n"
        "- 首次协作时，主动了解用户的审美偏好：喜欢的风格、重视的维度、常用的筛选标准。\n"
        "- 可以用 collect_preference 工具记录用户的审美偏好，保存到知识库以便后续会话复用。\n"
        "- 每次评分/选片前，查阅用户偏好，据此调整阈值、筛选策略和推荐理由。\n"
        "- 留意用户的手动纠偏（set_keep/set_reject），这些操作隐含了用户的真实审美信号。\n"
        "用中文回复用户。（工具名前缀含义：[照片分析]/[AI评分]/[文件操作]/[筛选导出]/[云端]/[云端相册]/[文档]/[系统]/[流程]，⚠=破坏性操作会触发确认或记录 undo）"
    )


# ══════════════════════════════════════════════════════════════════════
# Tool execution (sandbox + confirm gate already inside each tool fn)
# ══════════════════════════════════════════════════════════════════════

def _execute_tool(session, tool_name, args):
    # 0. Active skill tool allowlist check
    global _active_skill
    if _active_skill:
        allowed = _active_skill.get("allow_tools")
        if allowed is not None and tool_name not in allowed:
            return {"error": "skill_blocked",
                    "detail": f"当前技能不允许使用工具 '{tool_name}'"}

    # 1. Native tools
    t = TOOL_REGISTRY.get(tool_name)
    if t:
        try:
            return t["fn"](session, args)
        except SandboxViolation as e:
            _emit(session, "agent_log", {"level": "warn", "message": f"沙箱拒绝: {e}"})
            return {"error": "sandbox_violation", "detail": str(e)}
        except Exception as e:
            send_log(f"[agent] 工具 {tool_name} 异常: {e}", "error")
            return {"error": "tool_error", "detail": str(e)}

    # 2. MCP tools
    try:
        mcp = get_mcp_manager()
        if mcp.available and tool_name in mcp.tools:
            result = mcp.call_tool(tool_name, args)
            return result
    except Exception as e:
        send_log(f"[agent] MCP 工具 {tool_name} 异常: {e}", "error")
        return {"error": "mcp_tool_error", "detail": str(e)}

    return {"error": "unknown_tool", "detail": tool_name}


# ══════════════════════════════════════════════════════════════════════
# Agent loop
# ══════════════════════════════════════════════════════════════════════

def _make_client(cfg):
    if not _OPENAI_OK:
        return None, "openai 库未安装: " + getattr(_OPENAI_ERR, "", "")
    import secure_store
    base_url = (cfg.get("base_url") or "").strip()
    api_key = secure_store.unprotect(cfg.get("api_key") or "").strip()
    use_local = bool(cfg.get("use_local", True))

    # 本地模型优先: 内置 llama-server 就绪则直接走本地端点(零成本、离线)
    if use_local:
        local_base, err = local_llm.ensure_running()
        if local_base:
            try:
                # 本地推理可能较慢(思考型模型 + CPU), 超时给足。
                # http_client trust_env=False: 回环端点不走系统/环境代理——
                # 终端代理(HTTP_PROXY 无 NO_PROXY=localhost)会把 127.0.0.1
                # 请求路由进代理导致连接失败
                import httpx
                return OpenAI(base_url=local_base, api_key="local-llm", timeout=600,
                              http_client=httpx.Client(trust_env=False)), None
            except Exception as e:
                return None, str(e)
        # 本地不可用: 若用户配了个人云端 Key 则降级, 否则报错引导
        if not api_key:
            hint = "（请先在设置中下载本地模型，或配置个人云端 Key）"
            return None, "本地模型不可用: " + (err or "未就绪") + hint

    # 个人云端 Key(用户自配更强模型)
    if not base_url or not api_key:
        return None, "LLM 未配置 base_url/api_key（请在设置中配置个人云端 Key，或改用本地模型）"
    try:
        return OpenAI(base_url=base_url, api_key=api_key, timeout=120), None
    except Exception as e:
        return None, str(e)


def _run_agent(session):
    global _active_session
    # ── preflight ──
    # work_folder may be empty (model will set it via set_work_folder); only
    # fail if a non-empty path doesn't exist.
    if session.work_folder and not os.path.isdir(session.work_folder):
        _emit(session, "agent_error", {"error": f"工作文件夹不存在: {session.work_folder}", "phase": "config"})
        _clear_active(session)
        return
    if not session.work_folder:
        _emit(session, "agent_log", {"level": "info",
               "message": "未指定工作文件夹，等待模型调用 set_work_folder 设置"})

    cfg = _load_agent_config()
    session.approval_timeout = int(cfg.get("approval_timeout", DEFAULT_APPROVAL_TIMEOUT))
    client, err = _make_client(cfg)
    if not client:
        _emit(session, "agent_error", {"error": err or "LLM 未配置", "phase": "config"})
        _clear_active(session)
        return

    model = session.model or resolve_model(cfg)
    if not model:
        _emit(session, "agent_error", {"error": "未配置 model（请下载本地模型或配置云端 Key）", "phase": "config"})
        _clear_active(session)
        return
    reasoning_effort = (session.reasoning_effort or cfg.get("reasoning_effort", "off") or "off")
    if reasoning_effort not in ("off", "low", "medium", "mid", "high", "max"):
        reasoning_effort = "off"
    temperature = float(cfg.get("temperature", 0.3) or 0.3)

    _emit(session, "agent_log", {"level": "info",
           "message": f"Agent 启动：模型={model}，工作文件夹={session.work_folder}"})

    # ── Skills: seed built-ins (幂等) + scan and auto-trigger ──
    skill_loader = get_skill_loader()
    try:
        skill_loader.seed_builtin_skills()
    except Exception as _seed_e:
        send_log(f"[agent] 内置技能写入失败: {_seed_e}", "warn")
    skill_loader.scan()
    skill = skill_loader.match(session.prompt)
    skill_body = ""
    if skill:
        skill_body = f"\n\n[已激活技能: {skill.name}]\n{skill.body}"
        send_event("agent_log", {"level": "info", "message": f"技能触发: {skill.name}"})

    # ── MCP: 确保本地 photo_s 工具服务器已注册（幂等）──
    # 数字员工自带 photo_s 批量处理 MCP 服务器，缺失时自动注册并持久化；
    # 通过 service.exe --mcp-server photo_s 自我拉起，开发/打包均可用。
    try:
        import os as _os, sys as _sys
        cfg = _get_agent_cfg()
        servers = cfg.get("mcpServers", {}) or {}
        if "photo_s" not in servers:
            # 打包(frozen): service.exe --mcp-server photo_s;
            # 开发: python service.py --mcp-server photo_s
            if getattr(_sys, "frozen", False):
                _cmd, _args = _sys.executable, ["--mcp-server", "photo_s"]
            else:
                _cmd = _sys.executable
                _args = [_sys.argv[0], "--mcp-server", "photo_s"]
            servers["photo_s"] = {
                "type": "stdio",
                "command": _cmd,
                "args": _args,
            }
            cfg["mcpServers"] = servers
            _save_agent_config(cfg)
            send_log("[agent] 已注册本地 photo_s MCP 工具服务器", "info")
    except Exception as _mcp_e:
        send_log(f"[agent] photo_s MCP 注册失败: {_mcp_e}", "warn")
        cfg = _get_agent_cfg()

    # ── MCP: connect servers ──
    mcp = get_mcp_manager()
    if mcp.available:
        mcp.connect_all(cfg)
        send_event("agent_log", {"level": "info", "message": f"MCP 已连接: {mcp.connected_servers}"})

    # ── Build system prompt with skills list + aesthetic preferences ──
    # User-provided content is wrapped in XML delimiters to prevent prompt injection
    sys_content = _system_prompt(session.work_folder) + skill_loader.get_prompt_list() + skill_body
    prefs = cfg.get("preferences", "").strip()
    if prefs:
        sys_content += f"\n\n<photographer_preferences>\n{prefs}\n</photographer_preferences>"
    # Auto-inject KB aesthetic preferences (first session / new preferences)
    try:
        kb_doc = _kb_doc_by_name("user-aesthetic-preferences")
        if kb_doc:
            body = kb_doc.get("content", "") or kb_doc.get("body", "")
            if body.strip() and body.strip() not in sys_content:
                sys_content += f"\n\n<saved_aesthetic_preferences>\n{body.strip()}\n</saved_aesthetic_preferences>"
    except Exception:
        pass

    session.messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": session.prompt},
    ]
    tools = _tool_specs()

    # Subscribe to score_progress so we can forward as agent_tool_progress
    current_call_id = {"id": None}
    unsub = None

    def _on_event(name, data):
        if name == "score_progress" and current_call_id["id"]:
            _emit(session, "agent_tool_progress", {
                "call_id": current_call_id["id"],
                "current": data.get("current"),
                "total": data.get("total"),
                "last_file": data.get("last_file"),
            })

    try:
        unsub = subscribe_events(_on_event)

        # ── Loop detection state ──
        session.recent_calls = []  # (tool_name, args_hash, ok) — 死循环检测只数成功调用
        _LOOP_THRESHOLD = 3  # same tool+args 3 times = detected loop

        for iteration in range(1, MAX_ITERATIONS + 1):
            if session.cancel_event.is_set():
                _record_run(session)
                _emit(session, "agent_done", {"summary": "已取消",
                       "iterations": iteration - 1,
                       "prompt_tokens": session.prompt_tokens,
                       "completion_tokens": session.completion_tokens,
                       "cached_tokens": session.cached_tokens,
                       "tokens_used": session.tokens_used,
                       "duration_ms": int((time.time() - session.start_time) * 1000)})
                return

            session.iteration = iteration
            # ── crash recovery: save session state after each iteration ──
            if session.work_folder:
                try:
                    state = {"session_id": session.id, "work_folder": session.work_folder,
                             "iteration": iteration, "undo_stack": session.undo_stack[-20:],
                             "message_count": len(session.messages)}
                    with open(os.path.join(session.work_folder, ".agent_session.json"), "w", encoding="utf-8") as _sf:
                        json.dump(state, _sf, ensure_ascii=False)
                except Exception:
                    pass

            _emit(session, "agent_iteration", {
                "iteration": iteration,
                "prompt_tokens": session.prompt_tokens,
                "completion_tokens": session.completion_tokens,
                "cached_tokens": session.cached_tokens,
                "tokens_used": session.tokens_used})

            # ── context window management ──
            limit = _context_limit()
            if limit:
                budget = int(limit * CTX_SAFETY_RATIO)
                with session.messages_lock:
                    session.messages = _trim_messages(session.messages, budget)

            # ── call LLM (streaming, fallback to non-stream) ──
            assistant_msg, usage = _call_llm(client, model, session, tools, reasoning_effort, temperature)
            if assistant_msg is None:
                return  # error already emitted

            if usage:
                session.prompt_tokens += usage.get("prompt", 0)
                session.completion_tokens += usage.get("completion", 0)
                session.cached_tokens += usage.get("cached", 0)
                session.tokens_used += usage.get("total", 0)
            with session.messages_lock:
                session.messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls") or []
            text = assistant_msg.get("content") or ""

            if not tool_calls:
                _record_run(session)
                _emit(session, "agent_done", {
                    "summary": text or "(无输出)",
                    "iterations": iteration,
                    "prompt_tokens": session.prompt_tokens,
                    "completion_tokens": session.completion_tokens,
                    "cached_tokens": session.cached_tokens,
                    "tokens_used": session.tokens_used,
                    "duration_ms": int((time.time() - session.start_time) * 1000),
                })
                return

            # ── execute tool calls (parallel for independent calls) ──
            def _execute_one(tc):
                """Execute a single tool call.  Returns (tc, result_dict)."""
                fn_name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "") or "{}"
                try:
                    t_args = json.loads(raw_args) if raw_args else {}
                except Exception:
                    t_args = {"_raw_arguments": raw_args}
                call_id = tc.get("id") or uuid.uuid4().hex

                # finish is intercepted before parallel dispatch
                if fn_name == "finish":
                    return (tc, {"__finish__": True, "summary": t_args.get("summary", "")})

                # Loop detection: same tool + same SUCCESSFUL args 3+ times → stop
                # 只数成功调用:失败后的重试常是合法的(如沙箱拒绝后切换工作夹
                # 再查同一目录);set_work_folder 会重置计数(状态变化使"相同
                # 参数"不再代表"相同结果")
                args_hash = hashlib.md5(json.dumps(t_args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                same_count = sum(1 for n, h, okf in session.recent_calls
                                 if n == fn_name and h == args_hash and okf)
                if same_count >= _LOOP_THRESHOLD:
                    # 死循环必须终止会话:只返回 error 会被模型无视并继续重试,
                    # 2026-09-12 实测曾烧掉 50 万 token 直到上下文爆掉(400)
                    _emit(session, "agent_log", {
                        "level": "warn",
                        "message": f"检测到死循环: 工具 {fn_name} 相同参数已调用 "
                                   f"{same_count} 次,自动结束会话"})
                    return (tc, {"__finish__": True,
                                 "summary": f"自动终止: 工具 {fn_name} 以相同参数重复调用 "
                                            f"{same_count} 次(疑似死循环),会话已停止;"
                                            f"已完成的部分结果保留"})

                session.iteration = iteration
                t0 = time.time()
                _emit(session, "agent_tool_call", {
                    "call_id": call_id, "tool": fn_name,
                    "args": t_args, "iteration": iteration,
                })
                current_call_id["id"] = call_id
                result = _execute_tool(session, fn_name, t_args)
                # Auto-retry once on transient failures only (timeout, network, OSError)
                if isinstance(result, dict) and result.get("error") in ("tool_timeout", "tool_crash"):
                    retry_result = _execute_tool(session, fn_name, t_args)
                    if not (isinstance(retry_result, dict) and retry_result.get("error")):
                        result = retry_result
                current_call_id["id"] = None
                # 死循环检测记账:成功调用才算数(见上方 same_count 统计)
                ok_flag = not (isinstance(result, dict) and result.get("error"))
                session.recent_calls.append((fn_name, args_hash, ok_flag))
                if len(session.recent_calls) > 10:
                    session.recent_calls[:] = session.recent_calls[-10:]
                return (tc, result)

            # Parallel execution: up to 4 independent tool calls at once
            if len(tool_calls) == 1:
                tc, result = _execute_one(tool_calls[0])
                all_results = [(tc, result)]
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                all_results = []
                TOOL_TIMEOUT = 300  # 5 minutes per tool
                with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
                    futures = {pool.submit(_execute_one, tc): tc for tc in tool_calls}
                    for future in as_completed(futures):
                        if session.cancel_event.is_set():
                            pool.shutdown(wait=False, cancel_futures=True)
                            _record_run(session)
                            _emit(session, "agent_done", {"summary": "已取消",
                                   "iterations": iteration,
                                   "prompt_tokens": session.prompt_tokens,
                                   "completion_tokens": session.completion_tokens,
                                   "cached_tokens": session.cached_tokens,
                                   "tokens_used": session.tokens_used,
                                   "duration_ms": int((time.time() - session.start_time) * 1000)})
                            return
                        tc = futures[future]
                        try:
                            all_results.append(future.result(timeout=TOOL_TIMEOUT))
                        except TimeoutError:
                            all_results.append((tc, {"error": "tool_timeout", "detail": f"工具执行超过 {TOOL_TIMEOUT}s 限制"}))
                        except Exception as e:
                            all_results.append((tc, {"error": "tool_crash", "detail": str(e)}))

            # Process results in order
            for tc, result in all_results:
                t0 = time.time()
                fn_name = tc.get("function", {}).get("name", "")
                call_id = tc.get("id") or uuid.uuid4().hex

                # handle finish specially
                if result.get("__finish__"):
                    summary = result.get("summary", "")
                    _emit(session, "agent_tool_result",
                           {"call_id": call_id, "tool": "finish", "ok": True,
                            "result": {"summary": summary}, "duration_ms": 0})
                    _record_run(session)
                    _emit(session, "agent_done", {
                        "summary": summary,
                        "iterations": iteration,
                        "prompt_tokens": session.prompt_tokens,
                        "completion_tokens": session.completion_tokens,
                        "cached_tokens": session.cached_tokens,
                        "tokens_used": session.tokens_used,
                        "duration_ms": int((time.time() - session.start_time) * 1000),
                    })
                    return
                dur = int((time.time() - t0) * 1000)
                ok = not (isinstance(result, dict) and result.get("error"))
                _emit(session, "agent_tool_result", {
                    "call_id": call_id, "tool": fn_name, "ok": ok,
                    "result": _truncate(result), "duration_ms": dur,
                })
                # feed the full result back to the model
                with session.messages_lock:
                    session.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result, ensure_ascii=False),
                    })

        # exhausted iterations (safety backstop)
        _record_run(session)
        _emit(session, "agent_done", {"summary": f"(达到迭代上限 {MAX_ITERATIONS})",
               "iterations": MAX_ITERATIONS,
               "prompt_tokens": session.prompt_tokens,
               "completion_tokens": session.completion_tokens,
               "cached_tokens": session.cached_tokens,
               "tokens_used": session.tokens_used,
               "duration_ms": int((time.time() - session.start_time) * 1000)})

    finally:
        if unsub:
            unsub()
        _clear_active(session)


def _estimate_tokens(messages):
    """Rough token estimation: ~2 chars per token for mixed CJK/Latin text."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total += max(len(content) // 2, 1)
        elif isinstance(content, list):
            # Multimodal content blocks
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += max(len(block.get("text", "")) // 2, 1)
    return total


def _trim_messages(messages, max_tokens):
    """Trim oldest assistant+tool interaction pairs until estimated tokens fit.

    System and user messages are always preserved. Assistant+tool messages are
    trimmed in complete groups (assistant msg + its tool_call results) to keep
    the role sequence valid and tool_call_id references intact."""
    if _estimate_tokens(messages) <= max_tokens:
        return messages

    # Collect system + user messages (always kept)
    head = [m for m in messages if m.get("role") in ("system", "user")]

    # Build interaction groups from the tail: each group is [assistant, tool*]
    tail_msgs = [m for m in messages if m.get("role") in ("assistant", "tool")]
    groups = []
    i = 0
    while i < len(tail_msgs):
        m = tail_msgs[i]
        if m.get("role") == "assistant":
            group = [m]
            # Collect all tool messages that belong to this assistant's tool_calls
            call_ids = set()
            for tc in (m.get("tool_calls") or []):
                call_ids.add(tc.get("id"))
            i += 1
            while i < len(tail_msgs) and tail_msgs[i].get("role") == "tool":
                tid = tail_msgs[i].get("tool_call_id", "")
                if tid and tid in call_ids:
                    group.append(tail_msgs[i])
                    i += 1
                else:
                    break
            groups.append(group)
        else:
            # Orphaned tool message (assistant was trimmed in a previous pass)
            groups.append([m])
            i += 1

    # Keep the most recent groups that fit within the token budget
    kept_groups = []
    for group in reversed(groups):
        candidate = head + [m for g in kept_groups for m in g] + group
        if _estimate_tokens(candidate) > max_tokens:
            break
        kept_groups.insert(0, group)

    result = head + [m for g in kept_groups for m in g]
    send_log(f"[agent] context trimmed: {len(messages)} → {len(result)} messages "
             f"({_estimate_tokens(messages)} → {_estimate_tokens(result)} est. tokens)", "warn")
    return result


def _call_llm(client, model, session, tools, reasoning_effort="off", temperature=0.3):
    """One streaming round-trip. Returns (assistant_msg, usage_dict) or (None, None).
    usage_dict = {"prompt","completion","total","cached"}."""
    text_buf = []
    tc_accum = {}  # index -> {"id":,"name":,"args":[]}
    usage_obj = {"id": None}  # holder so the closure can reassign

    # Build kwargs once; reasoning_effort is only passed when enabled. Some
    # non-reasoning models reject it, so default "off" omits it entirely.
    extra = {"temperature": temperature}
    if reasoning_effort and reasoning_effort != "off":
        extra["reasoning_effort"] = reasoning_effort

    def build_msg():
        content = "".join(text_buf) or None
        calls = None
        if tc_accum:
            calls = []
            for idx in sorted(tc_accum.keys()):
                e = tc_accum[idx]
                calls.append({
                    "id": e["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": e["name"], "arguments": "".join(e["args"])},
                })
        return {"role": "assistant", "content": content, "tool_calls": calls}

    import random as _random
    _MAX_RETRIES = 3
    _BASE_DELAY = 1.0  # seconds
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            stream = client.chat.completions.create(
                model=model, messages=session.messages, tools=tools,
                tool_choice="auto", stream=True,
                stream_options={"include_usage": True}, **extra,
            )
            break  # success — proceed to streaming loop below
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            # 400/422 且请求带可选参数 → 剥掉可选参数原样重试一次
            # （lumen-raw 模式：reasoning_effort/temperature 等可选字段会被
            #   部分 OpenAI 兼容端点直接拒绝而非忽略）
            status = getattr(e, "status_code", 0) or 0
            if status in (400, 422) and extra and attempt == 0:
                stripped = {k: v for k, v in extra.items()
                            if k not in ("reasoning_effort", "temperature")}
                if stripped != extra:
                    extra = stripped
                    continue
            # Distinguish transient vs permanent
            is_rate_limit = "429" in err_str or "rate" in err_str
            is_transient = is_rate_limit or "timeout" in err_str or "server" in err_str or "5" in str(getattr(e, "status_code", 0)) or "connection" in err_str
            if attempt >= _MAX_RETRIES or not is_transient:
                # Try non-streaming as last resort if streaming failed
                try:
                    retry_extra = {k: v for k, v in extra.items() if k != "reasoning_effort"} if "reasoning_effort" in extra else extra
                    resp = client.chat.completions.create(
                        model=model, messages=session.messages, tools=tools,
                        tool_choice="auto", **retry_extra,
                    )
                    msg = resp.choices[0].message
                    am = {"role": "assistant", "content": msg.content,
                          "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None}
                    return am, _extract_usage(getattr(resp, "usage", None))
                except Exception as e2:
                    _emit(session, "agent_error", {"error": f"LLM 请求失败: {e2}", "phase": "api"})
                    return None, None
            # Exponential backoff with jitter
            delay = _BASE_DELAY * (2 ** attempt) + _random.uniform(0, 1)
            send_log(f"[agent] LLM 请求失败 (attempt {attempt+1}/{_MAX_RETRIES}), {delay:.1f}s 后重试: {e}", "warn")
            time.sleep(delay)

    try:
        for chunk in stream:
            if session.cancel_event.is_set():
                break
            if not getattr(chunk, "choices", None):
                # usage-only chunk (some providers send a final chunk with no choices)
                if getattr(chunk, "usage", None):
                    usage_obj["id"] = chunk.usage
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                text_buf.append(delta.content)
                _emit(session, "agent_thinking", {"text": delta.content})
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    idx = tc.index if tc.index is not None else 0
                    if idx not in tc_accum:
                        tc_accum[idx] = {"id": "", "name": "", "args": []}
                    if tc.id:
                        tc_accum[idx]["id"] = tc.id
                    if getattr(tc.function, "name", None):
                        tc_accum[idx]["name"] = tc.function.name
                    if getattr(tc.function, "arguments", None):
                        tc_accum[idx]["args"].append(tc.function.arguments)
            # usage sometimes rides on the last choice chunk
            if getattr(chunk, "usage", None):
                usage_obj["id"] = chunk.usage
    except Exception as e:
        send_log(f"[agent] 流式解析异常: {e}", "warn")
        # If we got partial tool_calls, try to proceed; else error
        if not tc_accum and not text_buf:
            _emit(session, "agent_error", {"error": f"流式解析失败: {e}", "phase": "api"})
            return None, None

    return build_msg(), _extract_usage(usage_obj["id"])


def _clear_active(session):
    global _active_session
    with _active_lock:
        if _active_session is session:
            _active_session = None
    # Clean up crash-recovery file on normal exit
    if session and session.work_folder:
        try:
            sf = os.path.join(session.work_folder, ".agent_session.json")
            if os.path.exists(sf):
                os.remove(sf)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# IPC handlers
# ══════════════════════════════════════════════════════════════════════

def handle_agent_run(params):
    global _active_session
    session_id = params.get("session_id") or uuid.uuid4().hex
    work_folder = (params.get("work_folder") or "").strip()
    prompt = (params.get("prompt") or "").strip()
    auto_approve = bool(params.get("auto_approve_destructive", False))
    model = params.get("model")
    headless = bool(params.get("headless", False))

    # work_folder may be empty — the model can set it via set_work_folder
    # (useful when the user gives a path inside the prompt). Only reject a
    # non-empty path that doesn't resolve to a directory.
    if work_folder and not os.path.isdir(work_folder):
        return {"error": f"工作文件夹无效: {work_folder}"}
    if not prompt:
        return {"error": "提示词不能为空"}

    # 互斥：数字员工管线运行中时不允许启动 Agent（延迟 import 避免循环依赖）
    try:
        import pipeline_runner
        if pipeline_runner._get_active_run() is not None:
            return {"error": "pipeline_busy",
                    "detail": "数字员工管线正在运行，请先取消或等它结束后再启动 Agent"}
    except ImportError:
        pass

    with _active_lock:
        if _active_session is not None:
            return {"error": "agent_busy", "detail": "已有 Agent 任务在运行"}
        session = _Session(session_id, work_folder, prompt, auto_approve, model, headless)
        session.task_name = (params.get("task_name") or "").strip()
        session.cloud_token = (params.get("cloud_token") or "").strip()
        re = (params.get("reasoning_effort") or "").strip().lower()
        session.reasoning_effort = re if re in ("low", "medium", "mid", "high", "max") else None
        _active_session = session

    # ── Crash recovery: check for orphaned session from previous run ──
    recovery = None
    if work_folder:
        recovery_path = os.path.join(work_folder, ".agent_session.json")
        if os.path.isfile(recovery_path):
            try:
                with open(recovery_path, "r", encoding="utf-8") as _rf:
                    prev = json.load(_rf)
                if prev.get("iteration", 0) > 0:
                    recovery = {"prev_session_id": prev.get("session_id", ""),
                                "iteration": prev.get("iteration", 0),
                                "message_count": prev.get("message_count", 0),
                                "undo_stack_size": len(prev.get("undo_stack", []))}
                    # Restore undo stack if available
                    session.undo_stack = prev.get("undo_stack", [])
                os.remove(recovery_path)  # consumed — won't reappear next time
            except Exception:
                pass

    t = threading.Thread(target=_run_agent, args=(session,), daemon=True)
    t.start()
    resp = {"ok": True, "session_id": session_id}
    if recovery:
        resp["recovery"] = recovery
    return resp


def handle_agent_cancel(params):
    session = _get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    session.cancel_event.set()
    # unblock any pending approval / question
    for entry in list(session.pending_approvals.values()):
        entry["approved"] = False
        entry["event"].set()
    for entry in list(session.pending_questions.values()):
        entry["answer"] = None
        entry["event"].set()
    # cancel an in-flight scoring too (both flags: folder scan + watch/files)
    try:
        photo_scorer._score_cancel.set()
        photo_scorer._score_files_cancel.set()
    except Exception:
        pass
    return {"ok": True}


def handle_agent_reply(params):
    """User answered an ask_user question."""
    request_id = params.get("request_id")
    answer = params.get("answer") or ""
    session = _get_active()
    if not session:
        return {"error": "no active session"}
    entry = session.pending_questions.get(request_id)
    if not entry:
        return {"error": "unknown request_id"}
    entry["answer"] = answer
    entry["event"].set()
    return {"ok": True}


def handle_agent_get_status(params):
    s = _get_active()
    if not s:
        return {"active": False}
    return {
        "active": True, "session_id": s.id, "iteration": s.iteration,
        "tokens_used": s.tokens_used, "work_folder": s.work_folder,
        "headless": s.headless,
    }


def register_handlers():
    register_handler("agent_run", handle_agent_run)
    register_handler("agent_cancel", handle_agent_cancel)
    register_handler("agent_get_status", handle_agent_get_status)
    register_handler("agent_approve", handle_agent_approve)
    register_handler("agent_reply", handle_agent_reply)
    register_handler("agent_test_llm", handle_agent_test_llm)
    register_handler("agent_get_config", handle_agent_get_config)
    register_handler("agent_save_config", handle_agent_save_config)
    register_handler("agent_get_usage", handle_agent_get_usage)
    register_handler("agent_clear_usage", handle_agent_clear_usage)
    register_handler("agent_download_local_model", handle_agent_download_local_model)
    register_handler("agent_compact", handle_agent_compact)

    # MCP handlers
    from mcp_client_manager import get_mcp_manager as _get_mcp

    def handle_mcp_list_servers(params):
        m = _get_mcp()
        return {"available": m.available, "connected": m.connected_servers,
                "tool_count": len(m.tools)}

    def handle_mcp_add_server(params):
        name = params.get("name", "").strip()
        cfg = params.get("config", {})
        if not name: return {"error": "缺少 name"}
        agent_cfg = _get_agent_cfg()
        servers = agent_cfg.get("mcpServers", {}) or {}
        servers[name] = cfg
        agent_cfg["mcpServers"] = servers
        _service._app_config["agent"] = agent_cfg
        _service._save_config_to_disk()
        send_log(f"[agent] MCP server '{name}' saved", "info")
        return {"ok": True, "name": name}

    def handle_mcp_remove_server(params):
        name = params.get("name", "").strip()
        if not name: return {"error": "缺少 name"}
        agent_cfg = _get_agent_cfg()
        servers = agent_cfg.get("mcpServers", {}) or {}
        servers.pop(name, None)
        agent_cfg["mcpServers"] = servers
        _service._app_config["agent"] = agent_cfg
        _service._save_config_to_disk()
        send_log(f"[agent] MCP server '{name}' removed", "info")
        return {"ok": True, "name": name}

    def handle_mcp_test_connection(params):
        m = _get_mcp()
        ok, msg, tools = m.test_connection(params.get("config", {}))
        return {"ok": ok, "message": msg, "tools": tools}

    def handle_mcp_list_tools(params):
        m = _get_mcp()
        return {"available": m.available, "servers": m.connected_servers,
                "tools": list(m.tools.keys())}

    register_handler("mcp_list_servers", handle_mcp_list_servers)
    register_handler("mcp_add_server", handle_mcp_add_server)
    register_handler("mcp_remove_server", handle_mcp_remove_server)
    register_handler("mcp_test_connection", handle_mcp_test_connection)
    register_handler("mcp_list_tools", handle_mcp_list_tools)

    # Skill handlers
    from skill_loader import get_skill_loader as _get_sl

    def handle_skill_list(params):
        sl = _get_sl(); sl.scan()
        return {"skills": sl.list_all()}

    def handle_skill_save(params):
        sl = _get_sl()
        imports_raw = params.get("imports", "")
        imports = [s.strip() for s in imports_raw.split(",") if s.strip()] if imports_raw else None
        allow_raw = params.get("allow_tools", "")
        allow_tools = [s.strip() for s in allow_raw.split(",") if s.strip()] if allow_raw else None
        s = sl.save(
            name=params.get("name", "").strip(),
            description=params.get("description", ""),
            trigger=params.get("trigger", ""),
            model=params.get("model") or None,
            auto_approve=params.get("auto_approve") if params.get("auto_approve") is not None else None,
            work_folder=params.get("work_folder") or None,
            body=params.get("body", ""),
            imports=imports,
            allow_tools=allow_tools,
        )
        return {"ok": True, "skill": s.to_dict()}

    def handle_skill_delete(params):
        sl = _get_sl()
        sl.delete(params.get("name", "").strip())
        return {"ok": True}

    def handle_skill_activate(params):
        global _active_skill
        sl = _get_sl(); sl.scan()
        name = params.get("name", "").strip()
        if not name:
            # Deactivate current skill
            _active_skill = None
            return {"ok": True, "skill": None}
        skill = sl.get(name)
        if not skill:
            return {"error": f"未知技能: {name}", "available": [s.name for s in sl.skills.values()]}
        # Set active skill with allow_tools whitelist
        allow = skill.allow_tools if hasattr(skill, 'allow_tools') and skill.allow_tools else None
        _active_skill = {"name": skill.name, "allow_tools": allow}
        return {"ok": True, "skill": skill.to_dict()}

    register_handler("skill_list", handle_skill_list)
    register_handler("skill_save", handle_skill_save)
    register_handler("skill_delete", handle_skill_delete)
    register_handler("skill_activate", handle_skill_activate)

    # MCP cleanup on shutdown
    register_cleanup(lambda: _get_mcp().disconnect_all())

    register_handler("agent_kb_list", handle_agent_kb_list)
    register_handler("agent_kb_get", handle_agent_kb_get)
    register_handler("agent_kb_save", handle_agent_kb_save)
    register_handler("agent_kb_delete", handle_agent_kb_delete)
    register_handler("agent_get_prompts", handle_agent_get_prompts)
    register_handler("agent_save_prompts", handle_agent_save_prompts)
    register_handler("agent_list_new_files", handle_agent_list_new_files)
    register_handler("agent_fetch_official_key", handle_agent_fetch_official_key)
    register_handler("agent_get_quota", handle_agent_get_quota)
    # 官方 API 已停用: 仅清空旧配置
    _restore_official_config()

    if not _OPENAI_OK:
        send_log(f"agent_module loaded (openai 未安装: {_OPENAI_ERR}) — LLM 功能不可用", "warn")
    else:
        send_log("agent_module loaded", "info")


# ══ P0-1 统一命令注册表 → Agent 工具自动生成 ══

def _register_registry_tools():
    """把 command_registry 中标记 agent_tool=True 的命令生成为 LLM 工具。

    单一事实源：描述 / 参数 schema / 沙箱路径清单都在注册表维护
    （command_registry.py + 各模块 @command 装饰器），Agent 侧只做包装——
    工作夹沙箱校验 + 调用转发，不再手写第三份登记。
    与手写 @_tool 同名时手写工具优先（TOOL_REGISTRY 已存在则跳过）。
    """
    try:
        import command_registry
    except Exception as e:
        send_log(f"command_registry tools not loaded: {e}", "warn")
        return
    n = 0
    for spec in command_registry.describe():
        if not spec.get("agent_tool") or spec["name"] in TOOL_REGISTRY:
            continue
        fn = command_registry.get(spec["name"])["fn"]
        sandbox_paths = spec.get("workfolder_paths") or []

        def _wrapper(session, args, _fn=fn, _paths=sandbox_paths):
            for key in _paths:
                v = args.get(key)
                if not v:
                    continue
                for item in (v if isinstance(v, list) else [v]):
                    _assert_in_workfolder(str(item), session.work_folder)
            return _fn(args)

        TOOL_REGISTRY[spec["name"]] = {
            "description": spec["description"],
            "schema": spec["params_schema"],
            "destructive": bool(spec.get("destructive")),
            "fn": _wrapper,
        }
        n += 1
    if n:
        send_log(f"command_registry → {n} agent tools generated (editor pilot)", "info")
