"""command_registry.py — 统一命令注册表（P0-1 骨架；编辑器域已迁移为试点）

借鉴 ArtCraft（photocraft/lightcraft）的 "one registry of commands" 架构：
每个命令只登记一次（分类 / 描述 / 参数 schema / 破坏性标记），多端共享——

  1. IPC：register_all() 把表内命令批量挂到 service.register_handler；
  2. Agent：agent_module._register_registry_tools() 把标记 agent_tool=True 的
     命令自动生成为 LLM 工具（workfolder_paths 参数自动套工作夹沙箱）；
  3. 发现：IPC `registry_list` 返回全表元数据——未来 UI 命令面板 / photo_s
     MCP server / 外部自动化的单一事实源。

迁移策略：新命令一律走 @command 装饰器；存量命令按域渐进迁移。
已迁移：editor（photo_editor.py）。下一个候选：editor_presets / photo_tagger。
"""

_HANDLERS = {}  # name -> spec


def command(name, category, description, params_schema=None,
            destructive=False, agent_tool=False, workfolder_paths=None):
    """把 IPC / Agent 工具 handler 登记进统一注册表（装饰器）。

    params_schema:     JSON-Schema 风格对象（渐进补全，至少 {"type":"object"}）。
    destructive:       不可逆 / 批量写操作标记（元数据，供确认门与 UI 使用）。
    agent_tool:        True 时由 agent_module 自动生成为 LLM 工具。
    workfolder_paths:  需要工作夹沙箱校验的参数名列表（agent 包装层自动执行）。
    """
    def deco(fn):
        _HANDLERS[name] = {
            "name": name,
            "fn": fn,
            "category": category,
            "description": description,
            "params_schema": params_schema or {"type": "object", "properties": {}},
            "destructive": bool(destructive),
            "agent_tool": bool(agent_tool),
            "workfolder_paths": list(workfolder_paths or []),
        }
        return fn
    return deco


def get(name):
    return _HANDLERS.get(name)


def describe():
    """全表元数据（不含 fn）——registry_list 与 Agent 工具生成共用。"""
    return [{k: v for k, v in s.items() if k != "fn"}
            for _, s in sorted(_HANDLERS.items())]


def register_all(category=None):
    """把注册表命令批量挂到 service.register_handler。重复注册无害。"""
    from service import register_handler
    n = 0
    for name, s in _HANDLERS.items():
        if category and s["category"] != category:
            continue
        register_handler(name, s["fn"])
        n += 1
    return n


# ── registry 自身也作为一个命令域 ──

@command("registry_list", "system",
         "[系统] 列出统一命令注册表全部命令（分类/描述/参数 schema/破坏性标记）。只读。",
         params_schema={"type": "object", "properties": {
             "category": {"type": "string", "description": "按分类过滤，如 editor"}},
          "additionalProperties": False})
def handle_registry_list(params):
    cat = (params or {}).get("category")
    items = [d for d in describe() if not cat or d.get("category") == cat]
    return {"ok": True, "count": len(items), "commands": items}
