"""edit_journal.py — 追加式编辑操作日志（P2-2，LightCraft crash-safe 目录日志思想）

每次编辑落盘（save_edits / reset_edits / batch_apply_edits）向前追加一条
人可读 JSON 行（只追加、不改写；超限整文件轮转为 .1）。崩溃最多丢最后一
行，绝不损坏既有历史；日志可 diff / 备份 / 对账（LightCraft 的
"append-only log of operations you can diff, back up or replay"）。

记录的是参数键清单而非完整参数——完整参数本就在各图 sidecar 里，
日志定位是「谁在什么时候动过什么」的审计与回溯线索。
"""

import json
import os
import threading
import time

from service import send_log
from utils import get_user_data_dir

_LOCK = threading.Lock()
_PATH = os.path.join(get_user_data_dir(), "edit_journal.jsonl")
_MAX_BYTES = 8 * 1024 * 1024


def _rotate_if_needed():
    try:
        if os.path.exists(_PATH) and os.path.getsize(_PATH) > _MAX_BYTES:
            os.replace(_PATH, _PATH + ".1")
    except OSError:
        pass


def append(op, path, keys=None, count=None):
    """追加一条操作记录。best-effort：任何失败只告警，绝不影响编辑主流程。"""
    try:
        rec = {"ts": int(time.time()), "op": str(op), "path": str(path)}
        if keys is not None:
            rec["keys"] = list(keys)[:40]
        if count is not None:
            rec["count"] = int(count)
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            _rotate_if_needed()
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        try:
            send_log(f"edit_journal append failed: {e}", "warn")
        except Exception:
            pass


def query(path=None, limit=50):
    """按（可选）path 过滤，返回最近 limit 条（含 .1 轮转文件，时间正序）。"""
    out = []
    try:
        files = [_PATH + ".1", _PATH]  # 轮转文件在前，保证时间正序
        for fp in files:
            if not os.path.isfile(fp):
                continue
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # 崩溃留下的半行 → 跳过
                    if path and rec.get("path") != path:
                        continue
                    out.append(rec)
    except Exception as e:
        try:
            send_log(f"edit_journal query failed: {e}", "warn")
        except Exception:
            pass
    try:
        limit = max(1, min(500, int(limit or 50)))
    except (TypeError, ValueError):
        limit = 50
    return out[-limit:]


# ── IPC（P0-1 注册表） ──────────────────────────────────────────────────────

import command_registry  # noqa: E402
from command_registry import command  # noqa: E402


@command("edit_journal_list", "editor",
         "[编辑器] 查询追加式编辑操作日志（谁在何时动过哪些参数键）。只读。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "按照片路径过滤（可省略=全部）"},
             "limit": {"type": "integer", "description": "最近 N 条，默认 50，上限 500"}},
          "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_edit_journal_list(params):
    entries = query((params or {}).get("path"), (params or {}).get("limit"))
    return {"ok": True, "count": len(entries), "entries": entries}


def register_handlers():
    command_registry.register_all()
    send_log("edit_journal module loaded (IPC via command_registry)", "info")
