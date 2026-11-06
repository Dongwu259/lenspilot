"""edit_versions.py — 命名版本快照（LR Virtual Copies 的轻量实现，P6-1）

存储：<照片>.vcopies.json —— {"version": 1, "copies": [{id, name, ts, edits}]}，
与 .edit.json sidecar 同目录同生命周期（拷卡/备份随照片走）。

语义：版本 = 具名的持久编辑快照——「存当前为版本」抓取此刻编辑参数；
「应用版本」把该版本的参数装回编辑器（后续保存进主 sidecar）。
与历史面板的区别：版本跨会话持久、用户命名管理；历史是会话内保存点流。
"""

import json
import os
import time

import command_registry
from command_registry import command
from service import send_log

MAX_COPIES = 20
MAX_NAME_LEN = 30


def _vc_path(img_path):
    return img_path + ".vcopies.json"


def _load(img_path):
    try:
        with open(_vc_path(img_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("copies"), list):
            return [c for c in data["copies"]
                    if isinstance(c, dict) and c.get("id") and isinstance(c.get("edits"), dict)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception as e:
        send_log(f"edit_versions load failed: {e}", "warn")
    return []


def _save(img_path, copies):
    path = _vc_path(img_path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "copies": copies}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@command("vcopy_list", "editor",
         "[编辑器] 列出照片的命名版本快照（LR Virtual Copies 轻量版）。只读。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_vcopy_list(params):
    path = params.get("path", "")
    if not path:
        return {"error": "Invalid path"}
    copies = _load(path)
    return {"ok": True, "count": len(copies),
            "copies": [{"id": c["id"], "name": c.get("name", ""),
                        "ts": c.get("ts"), "keys": sorted(c["edits"].keys())}
                       for c in copies]}


@command("vcopy_create", "editor",
         "[编辑器] 把当前编辑参数存为命名版本快照（同名覆盖）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"},
             "name": {"type": "string", "description": "1-30 字符"},
             "edits": {"type": "object", "description": "要快照的编辑参数"}},
          "required": ["path", "name", "edits"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_vcopy_create(params):
    path = params.get("path", "")
    name = str(params.get("name") or "").strip()
    edits = params.get("edits")
    if not path:
        return {"error": "Invalid path"}
    if not name or len(name) > MAX_NAME_LEN:
        return {"error": f"Invalid name (1-{MAX_NAME_LEN} chars)"}
    if not isinstance(edits, dict) or not edits:
        return {"error": "Invalid edits"}
    copies = _load(path)
    for c in copies:
        if c.get("name") == name:
            c["edits"] = edits
            c["ts"] = int(time.time())
            _save(path, copies)
            return {"ok": True, "copy": c, "overwritten": True}
    if len(copies) >= MAX_COPIES:
        return {"error": f"Version limit reached ({MAX_COPIES})"}
    copy = {"id": f"v{int(time.time() * 1000):x}", "name": name,
            "ts": int(time.time()), "edits": edits}
    copies.append(copy)
    _save(path, copies)
    return {"ok": True, "copy": copy}


@command("vcopy_apply", "editor",
         "[编辑器] 读取指定版本快照的编辑参数（装回编辑器由前端完成；后续保存进主 sidecar）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"}, "vcid": {"type": "string"}},
          "required": ["path", "vcid"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_vcopy_apply(params):
    path = params.get("path", "")
    vcid = str(params.get("vcid") or "")
    if not path or not vcid:
        return {"error": "Invalid path/vcid"}
    for c in _load(path):
        if c["id"] == vcid:
            return {"ok": True, "name": c.get("name", ""), "edits": c["edits"]}
    return {"error": "Version not found"}


@command("vcopy_delete", "editor",
         "[编辑器] 删除指定命名版本快照（不影响主编辑与原图）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"}, "vcid": {"type": "string"}},
          "required": ["path", "vcid"], "additionalProperties": False},
         destructive=True)
def handle_vcopy_delete(params):
    path = params.get("path", "")
    vcid = str(params.get("vcid") or "")
    if not path or not vcid:
        return {"error": "Invalid path/vcid"}
    copies = _load(path)
    remaining = [c for c in copies if c["id"] != vcid]
    if len(remaining) == len(copies):
        return {"error": "Version not found"}
    _save(path, remaining)
    return {"ok": True}


def register_handlers():
    command_registry.register_all()
    send_log("edit_versions module loaded (IPC via command_registry)", "info")
