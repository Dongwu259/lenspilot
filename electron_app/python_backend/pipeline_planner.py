"""
pipeline_planner.py — LLM 生成/修改数字员工管线模板（0.8.5）

核心能力：给定自然语言目标（可选基底模板），调 LLM 产出一份管线模板 JSON 草稿。
- 步骤元数据动态取自 step_registry.get_available_steps()（新步骤自动进入提示词）
- 输出经 step_registry.validate_template 复核；有错则把错误清单反馈给 LLM 自我修复一次
- 只返回草稿，不落盘（保存走 pipeline_templates.save_template，由前端/Agent 触发）

LLM 客户端复用 agent_module 的 _load_agent_config / _make_client / resolve_model
（本地模型优先），token 用量写入 agent_usage.json（model 记 "pipeline_planner"）。
"""

import json
import re
import time

from service import send_log, register_handler

# condition / gate 的语法白名单（提示词与前端校验共用同一份语义）
_CONDITION_OPS = ["gt", "ge", "lt", "le", "eq", "ne", "exists", "not_exists", "contains"]
_GATE_TYPES = ["action"]

# 手写的完整模板 JSON 示例（不要从 CULL_PIPELINE 序列化——里面有 fn 引用）
_EXAMPLE_TEMPLATE = {
    "id": "wedding_cull_export",
    "title": "婚礼初筛+导出",
    "description": "扫描评分后按阈值淘汰差片，选出 keeper",
    "phases": [
        {"id": "import", "title": "导入编目",
         "steps": [{"name": "scan"}]},
        {"id": "score", "title": "AI 评分",
         "steps": [{"name": "score"},
                   {"name": "threshold", "params": {"threshold": 6.0}}]},
        {"id": "cull", "title": "废片过滤",
         "steps": [{"name": "cull",
                     "params": {"eye_reject": 0.15},
                     "gate": {"type": "action", "message": "即将标记废片，请确认", "timeout": 300}}]},
        {"id": "select", "title": "出 keeper",
         "steps": [{"name": "select",
                     "condition": {"key": "artifacts.scored", "op": "gt", "value": 0}}]},
    ],
}

_SYSTEM_PROMPT = (
    "你是摄影工作流程编排专家。根据用户目标，用给定的步骤集编排一条数字员工管线模板。"
    "只输出一个 JSON 对象，不要输出 markdown 代码块，不要输出任何解释文字。"
)


def _format_steps_doc():
    """把 step_registry 的全量步骤元数据格式化为提示词文本（动态获取，新步骤自动包含）。"""
    from step_registry import get_available_steps
    lines = []
    for name, meta in get_available_steps().items():
        parts = [
            f"- {name}（{meta.get('title', '')}）: {meta.get('description', '')}",
            f"  分类: {meta.get('category', '')}",
            f"  产物: {meta.get('produces_artifacts', [])}",
            f"  前置步骤: {meta.get('requires_steps_before', [])}",
            f"  后置步骤: {meta.get('requires_steps_after', [])}",
            f"  破坏性: {meta.get('is_destructive', False)}",
            f"  可配置参数(user_configurable_params): {meta.get('user_configurable_params', [])}",
        ]
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _load_base_template_json(template_id):
    """加载基底模板的纯 JSON（内置模板去掉 fn 引用）。找不到返回 None。"""
    try:
        import pipeline_runner
        from pipeline_templates import list_templates
        for tpl in list_templates(builtin_templates=pipeline_runner.TEMPLATES):
            if tpl.get("id") == template_id:
                return tpl
    except Exception:
        pass
    return None


def _build_prompt(goal, base_template_id=None, folder_stats=None):
    """构建生成管线模板的 user prompt（中文）。"""
    parts = []
    parts.append(f"用户目标：{goal}")

    if folder_stats and isinstance(folder_stats, dict):
        stat_lines = [f"  {k}: {v}" for k, v in folder_stats.items()]
        parts.append("文件夹情况：\n" + "\n".join(stat_lines))

    parts.append("可用步骤（只能使用以下步骤名）：\n" + _format_steps_doc())

    parts.append(
        "模板 schema v2 语法：\n"
        "- 模板: {\"id\": str, \"title\": str, \"description\": str, \"phases\": [...]}\n"
        "- 阶段: {\"id\": str, \"title\": str, \"steps\": [...]}，至少 1 个阶段，每阶段至少 1 个步骤，阶段 id 不重复\n"
        "- 步骤: {\"name\": str}，另有 3 个可选字段：\n"
        "  · params: 对象，覆盖该步骤的运行参数；键必须在该步骤的 user_configurable_params 内\n"
        "  · condition: 对象，条件满足才执行该步骤；"
        "{\"key\": \"artifacts.<产物名>\" | \"params.<参数名>\" | \"goal\", "
        "\"op\": \"" + "|".join(_CONDITION_OPS) + "\", \"value\": 比较值（exists/not_exists 可省略）}\n"
        "  · gate: 对象，执行前弹门等人工确认；"
        "{\"type\": \"" + "|".join(_GATE_TYPES) + "\", \"message\": \"给人看的确认文案\", \"timeout\": 秒数}\n"
        "  三个字段都是可选的，不需要就不要写。"
    )

    parts.append("完整示例：\n" + json.dumps(_EXAMPLE_TEMPLATE, ensure_ascii=False, indent=2))

    parts.append(
        "硬性规则：\n"
        "- id 仅允许小写字母、数字、下划线，2-64 字符；不要使用内置模板 id（如 cull_pipeline）\n"
        "- 步骤顺序必须满足各步骤的 前置步骤/后置步骤 约束\n"
        "- 破坏性步骤建议配 gate 或在流程中靠后放\n"
        "- 只输出模板 JSON 对象本身，不要 markdown 代码块，不要任何解释文字"
    )

    if base_template_id:
        base = _load_base_template_json(base_template_id)
        if base:
            parts.append(
                f"以下是要在此基础上修改的底稿模板（id={base_template_id}），"
                "请保留其整体结构，按用户目标调整（可改 id/title/增删步骤/改 params）：\n"
                + json.dumps(base, ensure_ascii=False, indent=2))
        else:
            parts.append(f"（基底模板 '{base_template_id}' 未找到，请从零生成。）")

    return "\n\n".join(parts)


def _extract_json(text):
    """从 LLM 输出中提取 JSON 对象：先去 markdown fence，再用 {.*} 兜底。失败返回 None。"""
    content = (text or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r'\{.*\}', content, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _record_usage(usage_box, goal, start_time):
    """把 planner 的 token 用量写进 agent_usage.json（与 agent 共用一个账本，best-effort）。"""
    total = usage_box.get("total", 0)
    if total <= 0:
        return
    try:
        import agent_module as _am
        rec = {
            "ts": int(time.time()),
            "session_id": "planner-" + str(int(start_time)),
            "model": "pipeline_planner",
            "task_name": goal[:60] if goal else "",
            "headless": False,
            "prompt": f"生成管线模板: {goal[:80]}",
            "iterations": usage_box.get("calls", 1),
            "prompt_tokens": usage_box.get("prompt", 0),
            "completion_tokens": usage_box.get("completion", 0),
            "cached_tokens": usage_box.get("cached", 0),
            "total_tokens": int(total),
            "duration_ms": int((time.time() - start_time) * 1000),
        }
        with _am._usage_lock:
            data = _am._load_usage()
            runs = data.get("runs", [])
            runs.append(rec)
            if len(runs) > 500:
                runs = runs[-500:]
            data["runs"] = runs
            _am._save_usage(data)
    except Exception:
        pass  # best-effort，记账失败不影响主流程


def _accumulate_usage(usage_box, usage_obj):
    """累加一次 LLM 调用的 token 用量到 usage_box。"""
    if usage_obj is None:
        return
    try:
        import agent_module as _am
        u = _am._extract_usage(usage_obj)
    except Exception:
        return
    usage_box["prompt"] += u["prompt"]
    usage_box["completion"] += u["completion"]
    usage_box["cached"] += u["cached"]
    usage_box["total"] += u["total"]
    usage_box["calls"] = usage_box.get("calls", 0) + 1


def _chat_once(client, model, messages):
    """发一轮非流式对话，返回 (content, usage)。异常向上抛。"""
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=4000,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=120)
    msg = resp.choices[0].message
    content = (getattr(msg, "content", None) or
               getattr(msg, "reasoning_content", None) or "")
    return content, getattr(resp, "usage", None)


def generate_template(goal, base_template_id=None, folder_stats=None):
    """LLM 生成/修改管线模板草稿。不落盘。

    返回 {"template": dict|None, "errors": [...], "warnings": [...]}；
    LLM 未配置时另含 "error": "llm_not_configured"，调用失败时含 "error": "llm_call_failed"。
    """
    from step_registry import validate_template
    import agent_module

    goal = (goal or "").strip()
    if not goal:
        return {"template": None, "errors": ["goal 不能为空"], "warnings": []}

    start_time = time.time()
    usage_box = {"prompt": 0, "completion": 0, "cached": 0, "total": 0, "calls": 0}

    # ── LLM 客户端（复用 agent_module 的配置与工厂）──
    cfg = agent_module._load_agent_config()
    client, err = agent_module._make_client(cfg)
    if not client:
        return {"template": None, "errors": [err or "LLM 未配置"], "warnings": [],
                "error": "llm_not_configured"}
    model = agent_module.resolve_model(cfg)
    if not model:
        return {"template": None, "errors": ["未配置模型名称"], "warnings": [],
                "error": "llm_not_configured"}

    prompt = _build_prompt(goal, base_template_id, folder_stats)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]

    # ── 第一轮：生成 ──
    try:
        content, usage = _chat_once(client, model, messages)
        _accumulate_usage(usage_box, usage)
    except Exception as e:
        send_log(f"pipeline_planner: LLM 调用失败: {e}", "error")
        return {"template": None, "errors": [f"LLM 调用失败: {e}"], "warnings": [],
                "error": "llm_call_failed"}

    tpl = _extract_json(content)
    errors, warnings = [], []
    if tpl is None:
        errors = ["输出不是合法的 JSON 对象"]
    else:
        errors, warnings = validate_template(tpl)

    # ── 第二轮（仅在有错时）：把错误清单反馈给 LLM 自我修复一次 ──
    if errors:
        repair_messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user",
             "content": ("你的输出有以下错误：\n" + "\n".join(f"- {e}" for e in errors) +
                         "\n请修正后重新输出完整的模板 JSON（只输出 JSON 对象本身）。")},
        ]
        try:
            content2, usage2 = _chat_once(client, model, repair_messages)
            _accumulate_usage(usage_box, usage2)
            tpl2 = _extract_json(content2)
            if tpl2 is None:
                errors = ["修复输出仍不是合法的 JSON 对象"]
            else:
                tpl = tpl2
                errors, warnings = validate_template(tpl)
        except Exception as e:
            send_log(f"pipeline_planner: LLM 修复轮调用失败: {e}", "error")
            errors = errors + [f"修复轮调用失败: {e}"]

    _record_usage(usage_box, goal, start_time)

    if errors:
        send_log(f"pipeline_planner: 模板生成失败（修复后仍有错）: {errors[:3]}", "warn")
        return {"template": None, "errors": errors, "warnings": warnings}

    send_log(f"pipeline_planner: 已生成模板草稿 '{tpl.get('id', '?')}' "
             f"（{usage_box['total']} tokens）", "info")
    return {"template": tpl, "errors": [], "warnings": warnings}


# ═══════════════════════════════════════════════════════════════════════
# IPC
# ═══════════════════════════════════════════════════════════════════════

def handle_generate_pipeline_template(params):
    """IPC: 生成管线模板草稿。params: goal 必填, base_template / folder_stats 可选。"""
    goal = (params.get("goal") or "").strip()
    if not goal:
        return {"error": "goal 不能为空"}
    base = (params.get("base_template") or "").strip() or None
    folder_stats = params.get("folder_stats")
    if folder_stats is not None and not isinstance(folder_stats, dict):
        folder_stats = None
    return generate_template(goal, base_template_id=base, folder_stats=folder_stats)


def register_handlers():
    register_handler("generate_pipeline_template", handle_generate_pipeline_template)
    send_log("pipeline_planner module loaded", "info")
