# COMMAND_REGISTRY.md — 统一命令注册表（P0-1）

> 2026-10-13 落地。借鉴 ArtCraft（storytold/photocraft・lightcraft）的
> "one registry of commands" 架构：命令只登记一次，UI / IPC / Agent / 未来 MCP 多端共享。

## 解决什么问题

此前同一能力要登记三份、各自漂移：

| 体系 | 位置 | 数量 |
|---|---|---|
| Electron IPC 白名单 | `main.js` `ALLOWED_COMMANDS` | ~130 条 |
| Python IPC handler | 各模块 `register_handler(...)` 手工逐条 | ~130 条 |
| Agent 工具 | `agent_module.py` `@_tool` 手写包装 | 82 个 |

三份签名不一致时（改名/加参）只能靠人肉对齐，且 Agent 永远摸不到没手写过包装的能力
（比如编辑器的每个功能）。注册表把「命令是什么」收敛为**单一事实源**。

## 架构

```
@command(name, category, description, params_schema,
         destructive, agent_tool, workfolder_paths)   ← 各模块唯一登记点
        │
        ├─ register_all() ──────────────→ service.register_handler（IPC 自动挂接）
        ├─ agent_module._register_registry_tools() ──→ TOOL_REGISTRY（LLM 工具自动生成，
        │                                               workfolder_paths 自动套沙箱）
        └─ IPC `registry_list` ────────→ 全表元数据（UI 命令面板 / photo_s MCP /
                                         外部自动化的发现接口）
```

- 骨架：`electron_app/python_backend/command_registry.py`
- Electron 侧仅在 `main.js` 白名单加一条 `registry_list`（安全边界不变：执行仍走原白名单）。

## 怎么加一个命令（新命令一律走这里）

```python
# my_module.py
import command_registry
from command_registry import command

@command("my_thing_do", "my_domain",
         "[我的域] 干什么事。只读/写清楚。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"}},
          "required": ["path"], "additionalProperties": False},
         destructive=False,          # 不可逆/批量写 → True
         agent_tool=True,            # 要暴露给 AI 助手 → True
         workfolder_paths=["path"])  # 这些参数在 Agent 侧自动套工作夹沙箱
def handle_my_thing_do(params):
    ...
```

模块的 `register_handlers()` 里把逐条 `register_handler(...)` 换成
`command_registry.register_all()`（重复注册无害，可多模块共用）。

## 迁移状态

| 域 | 状态 | 文件 |
|---|---|---|
| editor | ✅ 已迁移（2026-10-19：photo_editor 13 条 + lr_xmp_import 3 条 + edit_journal 1 条） | photo_editor.py / lr_xmp_import.py / edit_journal.py |
| selection | ✅ 2026-10-13 已迁移（search_photos） | search_query.py |
| system (registry_list) | ✅ 随骨架 | command_registry.py |
| editor_presets | 候选 | editor_presets.py |
| photo_tagger | 候选 | photo_tagger.py |
| tether / pipeline / 其它 | 按域渐进，不动线上行为 | — |

已自动生成的 Agent 工具（共 8 条，工具总数 82→90）：`get_edits`、`apply_edits`、
`save_edits`、`auto_tone_editor`、`get_image_info`、`search_photos`、
`import_lr_develop`、`edit_journal_list` —— AI 助手可以读取/修改编辑参数、
驱动 AI 修图、按字段查询照片库、导入 LR 修改参数、审计编辑历史
（数字员工 MCP 调色实测的底层通路）。与手写 `@_tool`
同名时手写优先（`TOOL_REGISTRY` 已存在则跳过），存量行为零改变。

## 预设域命令（2026-10-19，注册表 38 条）

| 命令 | 属性 | 用途 |
|---|---|---|
| `apply_preset_thumbnails` | 非 destructive、非 Agent | 预设卡片墙缩略图：一次解码/几何，逐预设像素段，LRU4 |
| `batch_apply_preset` | destructive、非 Agent | 选区批量套预设；固定参数 + 自适应基线 + 参考统计卡；`task_id` 可取消 |
| `import_xmp_presets` | 非 Agent | 导入 LR `.xmp` 预设，映射后保存为用户预设 |
| `export_xmp_preset` | 非 Agent | 导出固定参数预设为 LR `.xmp`；只写用户选择文件，不写照片 sidecar |

这 4 条均为 UI 命令（`agent_tool=False`），Agent 工具列表不变。预设卡片墙的
读取仍走既有 `list_editor_presets` / `save_editor_preset` / `delete_editor_preset`
（在 `editor_presets.py`，尚未迁入统一注册表）。

## 一致性不变量（2026-10-16 起，冒烟 `smoke_test/test_command_consistency.py` 强制）

同一类漏配咬了四次后上的机制防线，回归必跑：

1. **签名**：所有 `@command` handler 必须是单参 `fn(params)`（service 按
   `handler(params)` 分发）。教训：装饰器贴到 `(path, edits)` 签名的内部函数上，
   apply_edits / save_edits 两条核心 IPC 自 P0-1 起必 TypeError。
2. **白名单覆盖**：前端 `src/js` 全部字面量 `sendCommand('name')` ⊆
   `main.js ALLOWED_COMMANDS`。教训：auto_tone 三命令、edit_journal_list、
   import_lr_develop、scan_retouched、agent_download_local_model 漏白名单，
   UI 按钮打不通后端。
3. **白名单存在性**：`ALLOWED_COMMANDS` 每个名字必须在注册表、后端
   `register_handler('name'` 字面量、或 `("name", handle_x)` 元组注册中
   真实存在——防 typo 死条目静默膨胀。

**加命令的 checklist**：`@command` 装饰器必须直接贴在 `def handler(params)` 上
（内部 helper 不许拦在中间）→ `main.js` 白名单加一条 → 跑一致性测试。

## 后续路线

1. photo_s MCP server（`photo_s/mcp_server.py`）改为从 `command_registry.describe()`
   取 schema，替代第二份登记。
2. UI 命令面板 / 快捷键自定义可读 `registry_list` 生成。
3. `destructive` 元数据接入 Agent 确认门与 Electron `DESTRUCTIVE_CMDS`，
   替代第三份硬编码清单。
