"""
agent_watcher.py — P5 热文件夹监听触发

监听一个文件夹，当累计 N 张新稳定照片到达（且静默期过）时自动跑一个
headless agent 任务。每个监听任务一条独立轮询线程，自带尺寸稳定性判断，
不依赖 folder_watcher（后者是单实例、面向联机拍摄）。

触发时直接调用 agent_module.handle_agent_run 进程内启动 agent。
若此时已有 agent 在跑（agent_busy），保留这批新文件，下一轮重试。

前端把监听任务存 localStorage（type='watch'），app 启动时对 enabled 的
监听任务调 agent_watch_start 武装；改/删调 agent_watch_stop。
"""

import os
import time
import threading

from service import send_event, send_log, register_handler
import photo_scorer

_IMG_EXTS = photo_scorer.IMAGE_EXTENSIONS
_POLL_INTERVAL = 2.0  # seconds
_STABLE_POLLS = 2     # size unchanged across N consecutive polls → stable


class _Watch:
    __slots__ = ("id", "name", "folder", "threshold", "quiet_seconds",
                 "prompt", "auto_approve", "model", "cloud_token",
                 "stop_event", "thread", "seen", "pending", "batch",
                 "last_new", "last_fire")

    def __init__(self, params):
        self.id = params.get("id") or ""
        self.name = params.get("name") or self.id
        self.folder = params.get("watch_folder") or params.get("folder") or ""
        self.threshold = int(params.get("threshold", 10) or 10)
        self.quiet_seconds = float(params.get("quiet_seconds", 8) or 8)
        self.prompt = params.get("prompt") or ""
        self.auto_approve = bool(params.get("auto_approve_destructive", True))
        self.model = params.get("model") or None
        self.cloud_token = params.get("cloud_token") or ""
        self.stop_event = threading.Event()
        self.thread = None
        self.seen = set()        # known file paths (skip on next poll)
        self.pending = {}        # path -> [size, stable_count]
        self.batch = []          # accumulated new stable files awaiting fire
        self.last_new = 0.0
        self.last_fire = 0.0


_watches = {}        # id -> _Watch
_lock = threading.Lock()


def _is_image(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in _IMG_EXTS


def _scan(folder):
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    try:
        for root, dirs, files in os.walk(folder):
            for f in files:
                p = os.path.join(root, f)
                if _is_image(p):
                    out.append(p)
    except Exception:
        pass
    return out


def _emit(name, data):
    send_event(name, data)


def _watch_loop(w):
    # Seed seen with existing files so only post-start files count.
    try:
        for p in _scan(w.folder):
            w.seen.add(p)
    except Exception:
        pass
    send_log(f"[agent_watch] 监听启动: {w.folder} (阈值 {w.threshold} 张, 静默 {w.quiet_seconds}s)", "info")
    _emit("agent_watch_started", {"id": w.id, "folder": w.folder, "threshold": w.threshold})

    while not w.stop_event.is_set():
        try:
            current = set(_scan(w.folder))
            now = time.time()
            # new files not yet seen
            new_paths = [p for p in current if p not in w.seen]
            for p in new_paths:
                w.seen.add(p)
                if p not in w.pending:
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        sz = -1
                    w.pending[p] = [sz, 0]
            # update pending stability
            ready = []
            for p in list(w.pending.keys()):
                if p not in current:
                    w.pending.pop(p, None)
                    continue
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                rec = w.pending[p]
                if rec[0] != sz:
                    rec[0] = sz; rec[1] = 0
                else:
                    rec[1] += 1
                if rec[1] >= _STABLE_POLLS and sz > 0:
                    ready.append(p)
                    w.pending.pop(p, None)
            if ready:
                w.batch.extend(ready)
                w.last_new = now
                _emit("agent_watch_progress", {
                    "id": w.id, "count": len(w.batch), "threshold": w.threshold,
                    "folder": w.folder, "new": len(ready)})

            # fire condition: enough files AND quiet period elapsed
            if w.batch and len(w.batch) >= w.threshold and (now - w.last_new) >= w.quiet_seconds:
                _try_fire(w)
        except Exception as e:
            send_log(f"[agent_watch] 轮询异常 {w.id}: {e}", "warn")

        w.stop_event.wait(_POLL_INTERVAL)

    _emit("agent_watch_stopped", {"id": w.id})
    send_log(f"[agent_watch] 监听停止: {w.folder}", "info")


def _try_fire(w):
    """Try to launch an agent run on the accumulated batch."""
    import agent_module
    # Don't fire if an agent is already running — hold the batch and retry.
    if agent_module._get_active() is not None:
        return
    batch = list(w.batch)
    w.batch.clear()
    w.last_fire = time.time()
    file_list = "\n".join(batch[:50])
    extra = (f"\n\n[本次监听到的新增照片: {len(batch)} 张，路径如下。请用 score_files 工具只评分这些文件，不要用 score_photos 扫描整个文件夹]\n{file_list}"
             + ("…" if len(batch) > 50 else ""))
    params = {
        "session_id": f"watch_{w.id}_{int(time.time())}",
        "work_folder": w.folder,
        "prompt": (w.prompt or "对新增照片进行AI评分，用score_files工具只评分下面列出的文件") + extra,
        "auto_approve_destructive": w.auto_approve,
        "model": w.model,
        "headless": True,
        "task_name": w.name,
        "cloud_token": w.cloud_token,
    }
    _emit("agent_watch_fired", {"id": w.id, "count": len(batch), "folder": w.folder})
    send_log(f"[agent_watch] 触发 agent: {w.name} ({len(batch)} 张新照片)", "info")
    try:
        r = agent_module.handle_agent_run(params)
        if not (r and r.get("ok")):
            # busy or error — put the batch back to retry next cycle
            w.batch = batch + w.batch
            send_log(f"[agent_watch] 触发未成功 ({r}): 保留批次重试", "warn")
    except Exception as e:
        w.batch = batch + w.batch
        send_log(f"[agent_watch] 触发异常: {e}", "error")


# ── IPC handlers ──

def handle_agent_watch_start(params):
    if not os.path.isdir(params.get("watch_folder") or params.get("folder") or ""):
        return {"error": "watch_folder 无效"}
    wid = params.get("id") or f"w{int(time.time())}"
    with _lock:
        if wid in _watches:
            _stop_watch(_watches[wid])
        w = _Watch(params)
        w.id = wid
        t = threading.Thread(target=_watch_loop, args=(w,), daemon=True)
        w.thread = t
        _watches[wid] = w
    t.start()
    return {"ok": True, "id": wid}


def _stop_watch(w):
    w.stop_event.set()


def handle_agent_watch_stop(params):
    wid = params.get("id")
    with _lock:
        w = _watches.pop(wid, None)
    if w:
        _stop_watch(w)
        return {"ok": True}
    return {"ok": False, "error": "not watching"}


def handle_agent_watch_status(params):
    with _lock:
        out = []
        for wid, w in _watches.items():
            out.append({"id": wid, "name": w.name, "folder": w.folder,
                        "threshold": w.threshold, "count": len(w.batch)})
    return {"watches": out}


def register_handlers():
    from service import register_cleanup

    register_handler("agent_watch_start", handle_agent_watch_start)
    register_handler("agent_watch_stop", handle_agent_watch_stop)
    register_handler("agent_watch_status", handle_agent_watch_status)

    # Shutdown cleanup: stop all agent watches
    def _cleanup():
        for w in list(_watches.values()):
            try:
                w.stop_event.set()
            except Exception:
                pass
    register_cleanup(_cleanup)
    send_log("agent_watcher module loaded", "info")
