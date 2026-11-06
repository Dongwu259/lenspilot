"""
pipeline_templates.py — 用户自定义管线模板 CRUD

模板以 JSON 文件形式存储在 <config_dir>/pipeline_templates/ 目录。
内置模板（source="builtin"）只读，不允许修改或删除。
"""
import os
import json
import time
import shutil

from service import _get_config_dir, send_log

TEMPLATES_DIR_NAME = "pipeline_templates"

# ── 注意：BUILTIN_TEMPLATES 在 pipeline_runner 导入完成后由 step_registry
#    或 pipeline_runner 本身设置。这里只存一个引用。
BUILTIN_IDS = {"cull_pipeline"}  # 内置模板 ID 白名单


def _templates_dir():
    """获取/创建模板存储目录。"""
    d = os.path.join(_get_config_dir(), TEMPLATES_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def list_templates(builtin_templates=None):
    """返回所有模板：内置在前，自定义在后（按文件名排序）。

    Args:
        builtin_templates: 可选，来自 pipeline_runner.TEMPLATES 的内置模板 dict。
                           如果传入，会作为 "builtin" 源模板加入结果列表。
    Returns:
        list[dict]: 每个模板 dict 含 id, title, description, source, phases 等。
    """
    templates = []

    # ── 内置模板 ──
    if builtin_templates:
        for tid, tpl in builtin_templates.items():
            d = {
                "id": tpl.get("id", tid),
                "title": tpl.get("title", tid),
                "description": tpl.get("description", ""),
                "source": "builtin",
                "version": 1,
                "phases": [
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "steps": [{"name": s["name"]} for s in p.get("steps", [])],
                    }
                    for p in tpl.get("phases", [])
                ],
            }
            templates.append(d)

    # ── 自定义模板（从磁盘加载）──
    td = _templates_dir()
    try:
        fnames = sorted(os.listdir(td))
    except OSError:
        fnames = []

    for fname in fnames:
        if not fname.endswith(".json"):
            continue
        path = os.path.join(td, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                tpl = json.load(f)
            tpl["source"] = "custom"
            if "id" not in tpl:
                tpl["id"] = fname[:-5]  # fallback: derive from filename
            templates.append(tpl)
        except (json.JSONDecodeError, OSError) as e:
            send_log(f"pipeline_templates: 跳过损坏模板 {fname}: {e}", "warn")

    return templates


def save_template(template_dict, original_id=None):
    """验证并持久化一个自定义模板。

    Args:
        template_dict: 模板数据（含 id, title, phases 等）
        original_id: 可选，编辑器打开时的原模板 ID。改 ID 保存时删除旧文件；
                     缺省（clone/新建）行为不变。
    Returns:
        dict: {"ok": True} 或 {"error": str, "errors": [...], "warnings": [...]}
    """
    from step_registry import validate_template

    # 内置模板不可覆盖
    if template_dict.get("source") == "builtin":
        return {"error": "内置模板不可覆盖"}

    tid = template_dict.get("id", "")
    if not tid:
        return {"error": "模板 ID 不能为空"}

    # 验证
    errors, warnings = validate_template(template_dict)
    if errors:
        return {"error": "validation_failed", "errors": errors, "warnings": warnings}

    # 设置元数据
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    template_dict["updated_at"] = now
    if "created_at" not in template_dict:
        template_dict["created_at"] = now
    template_dict["version"] = template_dict.get("version", 0) + 1
    template_dict["source"] = "custom"

    # 原子写入
    td = _templates_dir()
    path = os.path.join(td, f"{tid}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(template_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        return {"error": f"保存失败: {e}"}

    # 改 ID 保存：删除原 ID 的旧文件，避免磁盘上新旧两份并存
    import re
    if original_id and original_id != tid and re.match(r'^[a-z0-9_]{2,64}$', original_id):
        old_path = os.path.join(td, f"{original_id}.json")
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
                send_log(f"pipeline_templates: 模板 ID 变更，已删除旧文件 '{original_id}.json'", "info")
            except OSError as e:
                send_log(f"pipeline_templates: 删除旧模板 '{original_id}' 失败: {e}", "warn")

    send_log(f"pipeline_templates: 已保存模板 '{tid}'", "info")
    return {"ok": True, "warnings": warnings}


def delete_template(template_id):
    """删除一个自定义模板。拒绝删除内置模板。

    Args:
        template_id: 模板 ID
    Returns:
        bool: 是否成功删除
    """
    if template_id in BUILTIN_IDS:
        send_log(f"pipeline_templates: 拒绝删除内置模板 '{template_id}'", "warn")
        return False

    path = os.path.join(_templates_dir(), f"{template_id}.json")
    if not os.path.isfile(path):
        return False

    try:
        os.remove(path)
        send_log(f"pipeline_templates: 已删除模板 '{template_id}'", "info")
        return True
    except OSError:
        return False


def load_custom_template(template_id):
    """从磁盘加载自定义模板并解析步骤函数。

    Args:
        template_id: 模板 ID
    Returns:
        dict | None: 解析后的模板（含 fn 引用），若不存在则返回 None
    """
    if template_id in BUILTIN_IDS:
        return None  # 内置模板由 pipeline_runner.TEMPLATES 直接提供

    path = os.path.join(_templates_dir(), f"{template_id}.json")
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            tpl = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    from step_registry import resolve_template
    try:
        return resolve_template(tpl)
    except ValueError as e:
        send_log(f"pipeline_templates: 解析模板 '{template_id}' 失败: {e}", "error")
        return None


def clone_template(source_id, new_id, new_title=None):
    """克隆一个模板（内置或自定义）为新的自定义模板。

    Args:
        source_id: 源模板 ID
        new_id: 新模板 ID
        new_title: 新模板标题（可选，默认在原名后加 " (副本)"）
    Returns:
        dict: {"ok": True, "template": ...} 或 {"error": str}
    """
    import re
    if not re.match(r'^[a-z0-9_]{2,64}$', new_id):
        return {"error": "新模板ID格式无效（仅允许小写字母、数字、下划线，2-64字符）"}

    # 检查 ID 不冲突
    existing_path = os.path.join(_templates_dir(), f"{new_id}.json")
    if os.path.isfile(existing_path):
        return {"error": f"模板 ID '{new_id}' 已存在"}

    # 加载源模板
    from pipeline_runner import TEMPLATES
    source = None
    if source_id in TEMPLATES:
        # 内置模板
        src_tpl = TEMPLATES[source_id]
        source = {
            "id": new_id,
            "title": new_title or f"{src_tpl.get('title', source_id)} (副本)",
            "description": src_tpl.get("description", ""),
            "source": "custom",
            "base_template": source_id,
            "version": 1,
            "phases": [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "steps": [{"name": s["name"]} for s in p.get("steps", [])],
                }
                for p in src_tpl.get("phases", [])
            ],
        }
    else:
        # 自定义模板
        path = os.path.join(_templates_dir(), f"{source_id}.json")
        if not os.path.isfile(path):
            return {"error": f"源模板 '{source_id}' 不存在"}
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"读取源模板失败: {e}"}
        source["id"] = new_id
        source["title"] = new_title or f"{source.get('title', source_id)} (副本)"
        source["source"] = "custom"
        source["base_template"] = source_id
        source["version"] = 1
        if "created_at" in source:
            del source["created_at"]
        if "updated_at" in source:
            del source["updated_at"]

    # 保存
    result = save_template(source)
    if result.get("error"):
        return result
    return {"ok": True, "template": source}
