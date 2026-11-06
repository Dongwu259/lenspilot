"""
step_registry.py — 管线步骤注册表

集中管理所有可用的管线步骤（step）以及它们的元数据。
前端通过 IPC `get_available_steps` 获取元数据来构建工作流编辑器。
运行时通过 `resolve_template()` 将模板 JSON 中的步骤名解析为实际函数。

Schema v2（0.8.5）：模板 step 除 name 外支持三个可选字段——
  params    步骤级参数（合并优先级：门 edited_params > step.params > run.params）
  condition 条件执行 {"key": "artifacts.x|params.x|goal", "op": "...", "value": ...}
  gate      声明式动作门 {"type": "action", "message": "...", "timeout": 秒}

注意：fn 引用采用延迟加载——首次调用 resolve_template() 时才从 pipeline_runner
导入实际函数，避免模块级循环导入。
"""
import re

# ═══════════════════════════════════════════════════════════════════════
# Step Registry（元数据，不含 fn）
# ═══════════════════════════════════════════════════════════════════════

STEP_REGISTRY = {
    "scan": {
        "name": "scan",
        "title": "扫描编目",
        "description": "扫描文件夹中的所有图片文件，建立索引",
        "category": "import",
        "produces_artifacts": ["scanned"],
        "requires_artifacts": [],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": [],
    },
    "score": {
        "name": "score",
        "title": "AI 评分",
        "description": "使用本地 AI 模型对所有图片进行质量评分（1-10分）",
        "category": "score",
        "produces_artifacts": ["scored", "threshold_used"],
        "requires_artifacts": ["scanned"],
        "requires_steps_before": ["scan"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["threshold"],
    },
    "threshold": {
        "name": "threshold",
        "title": "确定阈值",
        "description": "根据分数分布和用户目标，通过 AI 或规则确定筛选阈值",
        "category": "score",
        "produces_artifacts": ["threshold_used"],
        "requires_artifacts": ["scored"],
        "requires_steps_before": ["score"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["threshold", "model"],
    },
    "dedup_preview": {
        "name": "dedup_preview",
        "title": "连拍去重(预览)",
        "description": "按时间窗口+感知哈希分组连拍照片，预演去重结果（需人工确认）",
        "category": "dedup",
        "produces_artifacts": ["dedup_groups", "dedup_to_move", "dedup_group_details"],
        "requires_artifacts": ["scored"],
        "requires_steps_before": ["scan"],
        "requires_steps_after": ["dedup_apply"],
        "is_destructive": False,
        "user_configurable_params": ["dedup_time_window", "dedup_similarity", "gate_timeout"],
    },
    "dedup_apply": {
        "name": "dedup_apply",
        "title": "执行去重",
        "description": "将连拍组中非最佳照片移动到 _duplicates/ 目录",
        "category": "dedup",
        "produces_artifacts": ["dedup_applied_count"],
        "requires_artifacts": ["dedup_to_move"],
        "requires_steps_before": ["dedup_preview"],
        "requires_steps_after": [],
        "is_destructive": True,
        "user_configurable_params": ["dedup_time_window", "dedup_similarity"],
    },
    "cull": {
        "name": "cull",
        "title": "废片过滤",
        "description": "基于分数和闭眼检测淘汰明显差片，标记 borderline 待裁决",
        "category": "cull",
        "produces_artifacts": ["reject", "borderline"],
        "requires_artifacts": ["scored", "threshold_used"],
        "requires_steps_before": ["threshold"],
        "requires_steps_after": [],
        "is_destructive": True,
        "user_configurable_params": ["eye_reject"],
    },
    "cull_borderline": {
        "name": "cull_borderline",
        "title": "LLM 裁决 borderline",
        "description": "将边界分数照片交给 LLM 批量裁决 keep/reject/uncertain",
        "category": "cull",
        "produces_artifacts": ["borderline_kept", "borderline_rejected", "uncertain_count", "uncertain_paths"],
        "requires_artifacts": ["borderline", "threshold_used"],
        "requires_steps_before": ["cull"],
        "requires_steps_after": [],
        "is_destructive": True,
        "user_configurable_params": ["model"],
    },
    "select": {
        "name": "select",
        "title": "出 keeper",
        "description": "汇总所有筛选结果，输出最终 keep/reject 列表和缩略图预览",
        "category": "output",
        "produces_artifacts": ["keep", "keep_paths", "keep_avg_score", "summary"],
        "requires_artifacts": ["scored"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": [],
    },
    # ── Schema v2: 交付链步骤（pipeline_tool_steps.py）──
    "export_keepers": {
        "name": "export_keepers",
        "title": "导出精选",
        "description": "将 keep 列表批量导出到本地目录（格式/质量/最大边长可调）",
        "category": "deliver",
        "produces_artifacts": ["exported_count", "export_dir"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["format", "quality", "max_size", "output_dir"],
    },
    "watermark_keepers": {
        "name": "watermark_keepers",
        "title": "水印精选",
        "description": "给 keep 列表照片批量加文字水印（输出到 _watermarked/ 副本）",
        "category": "deliver",
        "produces_artifacts": ["watermarked_count"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["text", "position", "opacity"],
    },
    "compress_keepers": {
        "name": "compress_keepers",
        "title": "压缩交付",
        "description": "用 PhotoS 引擎把 keep 列表压缩到目标体积（平台交付尺寸），上传前自动改传压缩版",
        "category": "deliver",
        "produces_artifacts": ["compress_count", "compress_dir", "deliver_paths"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["target_size", "format", "max_size", "strip_gps", "output_dir"],
    },
    "grade_keepers": {
        "name": "grade_keepers",
        "title": "批量风格化",
        "description": "给 keep 列表批量套 LUT(.cube)风格化，输出 _风格化交付/ 副本（交付统一色调）",
        "category": "deliver",
        "produces_artifacts": ["graded_count", "grade_dir"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["lut_file", "format", "quality", "output_dir"],
    },
    "auto_retouch_keepers": {
        "name": "auto_retouch_keepers",
        "title": "AI 自动修图",
        "description": "给 keep 列表逐张预测 LR 式调色参数并自动应用，输出 _自动修图/ 副本（AI 权重未就绪自动降级规则层）",
        "category": "deliver",
        "produces_artifacts": ["auto_retouch_count", "auto_retouch_dir",
                               "auto_retouched_paths", "auto_retouch_engine"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["strength", "engine", "format", "quality", "output_dir"],
    },
    "multi_size_keepers": {
        "name": "multi_size_keepers",
        "title": "多尺寸交付",
        "description": "把 keep 列表输出为多尺寸（如 thumb/screen/full），输出 _多尺寸交付/",
        "category": "deliver",
        "produces_artifacts": ["sizes_count", "sizes_dir"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["sizes", "format", "output_dir"],
    },
    "build_customer_preview": {
        "name": "build_customer_preview",
        "title": "客户预览",
        "description": "生成客户预览：HTML gallery（缩略+原图链接）或联系表网格拼图，输出 客户预览/",
        "category": "deliver",
        "produces_artifacts": ["preview_path", "preview_mode"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["mode", "title", "cols", "output_dir"],
    },
    "delivery_report": {
        "name": "delivery_report",
        "title": "交付报告",
        "description": "生成交付清单 CSV（文件名/路径/体积 + 汇总），默认 交付报告.csv",
        "category": "deliver",
        "produces_artifacts": ["report_path", "report_count"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["report_path", "source"],
    },
    "tag_keepers": {
        "name": "tag_keepers",
        "title": "批量打标",
        "description": "给 keep 列表批量写 EXIF 评分/关键词/标题（就地修改原文件），建立可搜索照片库",
        "category": "deliver",
        "produces_artifacts": ["tagged_count"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": True,
        "user_configurable_params": ["keywords", "rating", "title"],
    },
    "upload_keepers": {
        "name": "upload_keepers",
        "title": "上传精选",
        "description": "将 keep 列表上传到云端（需已登录，若有压缩交付产物则上传压缩版），产出云端照片 ID 供建相册",
        "category": "deliver",
        "produces_artifacts": ["uploaded_count", "cloud_photo_ids"],
        "requires_artifacts": ["keep_paths", "deliver_paths"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["category"],
    },
    "create_cloud_album": {
        "name": "create_cloud_album",
        "title": "建云端相册",
        "description": "用已上传的云端照片创建可分享相册（可设访问密码）",
        "category": "deliver",
        "produces_artifacts": ["album_code", "album_url"],
        "requires_artifacts": ["cloud_photo_ids"],
        "requires_steps_before": [],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["title", "password"],
    },
    # ── Lightroom 两轮筛选（round-2）步骤 ──
    "export_working_set": {
        "name": "export_working_set",
        "title": "导出工作集(写星标)",
        "description": "将粗筛 keeper 的评分映射为 LR 星级写入 XMP sidecar，供 Lightroom 修图使用（含 crs 保护，不覆盖 LR 开发数据）",
        "category": "deliver",
        "produces_artifacts": ["working_set_count", "working_set_paths"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": ["select"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["star5", "star4", "star3", "star2", "star1"],
    },
    "wait_retouch": {
        "name": "wait_retouch",
        "title": "Lightroom 修图交接",
        "description": "挂起管线，提示在 Lightroom 中修图并标注星级，完成后点「继续」",
        "category": "round2",
        "produces_artifacts": [],
        "requires_artifacts": ["working_set_count"],
        "requires_steps_before": ["export_working_set"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": [],
    },
    "import_retouched": {
        "name": "import_retouched",
        "title": "导入修图结果",
        "description": "扫描 XMP sidecar 识别 Lightroom 修图结果，并重评变化文件（未变化文件走缓存，速度快）",
        "category": "round2",
        "produces_artifacts": ["retouched_count", "retouched_paths", "round2_done", "round2_stats"],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": ["wait_retouch"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["re_score", "deliver_same_stem"],
    },
    "final_select": {
        "name": "final_select",
        "title": "二次终选",
        "description": "按 LR 星级（LR 修过的照片以星级为准）与二次评分阈值汇总终选 keeper，覆盖 keep_paths 供交付链使用",
        "category": "round2",
        "produces_artifacts": ["keep", "keep_paths", "keep_avg_score", "final_summary", "summary"],
        "requires_artifacts": ["retouched_paths"],
        "requires_steps_before": ["import_retouched"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": ["threshold", "lr_min_rating", "deliver_same_stem"],
    },
    "review_final": {
        "name": "review_final",
        "title": "终选确认",
        "description": "在交付前挂起，人工确认终选 keeper 网格，批准后进入交付链",
        "category": "round2",
        "produces_artifacts": [],
        "requires_artifacts": ["keep_paths"],
        "requires_steps_before": ["final_select"],
        "requires_steps_after": [],
        "is_destructive": False,
        "user_configurable_params": [],
    },
}

# ── 步骤名 → 函数引用的映射（延迟加载）──
_STEP_FN_MAP = None


def _ensure_fns_loaded():
    """延迟加载步骤函数引用（首次调用时从 pipeline_runner 导入）。"""
    global _STEP_FN_MAP
    if _STEP_FN_MAP is not None:
        return
    from pipeline_runner import (
        _scan_step, _score_step, _threshold_step,
        _dedup_preview_step, _dedup_apply_step,
        _cull_step, _cull_borderline_step, _select_step,
    )
    # 交付链步骤（pipeline_tool_steps 内部再延迟引 pipeline_runner，无循环导入）
    from pipeline_tool_steps import (
        export_keepers, watermark_keepers, compress_keepers, upload_keepers,
        create_cloud_album,
        grade_keepers, multi_size_keepers, build_customer_preview, delivery_report,
        tag_keepers, auto_retouch_keepers,
        export_working_set, wait_retouch, import_retouched, final_select, review_final,
    )
    _STEP_FN_MAP = {
        "scan": _scan_step,
        "score": _score_step,
        "threshold": _threshold_step,
        "dedup_preview": _dedup_preview_step,
        "dedup_apply": _dedup_apply_step,
        "cull": _cull_step,
        "cull_borderline": _cull_borderline_step,
        "select": _select_step,
        "export_keepers": export_keepers,
        "auto_retouch_keepers": auto_retouch_keepers,
        "watermark_keepers": watermark_keepers,
        "compress_keepers": compress_keepers,
        "grade_keepers": grade_keepers,
        "multi_size_keepers": multi_size_keepers,
        "build_customer_preview": build_customer_preview,
        "delivery_report": delivery_report,
        "tag_keepers": tag_keepers,
        "upload_keepers": upload_keepers,
        "create_cloud_album": create_cloud_album,
        "export_working_set": export_working_set,
        "wait_retouch": wait_retouch,
        "import_retouched": import_retouched,
        "final_select": final_select,
        "review_final": review_final,
    }


def get_available_steps():
    """返回所有步骤的元数据（不含函数引用）。安全用于 IPC 传输。"""
    return {
        name: {k: v for k, v in meta.items()}
        for name, meta in STEP_REGISTRY.items()
    }


def resolve_template(template_dict):
    """将模板字典（从 JSON 加载或内置定义）解析为可运行形式。

    对每个 phase 的每个 step，从 STEP_REGISTRY 查找并绑定 fn。
    返回完整的模板 dict（含 phases 列表，每个 step 含 fn）。
    """
    _ensure_fns_loaded()

    phases = []
    for phase in template_dict.get("phases", []):
        resolved_steps = []
        for step in phase.get("steps", []):
            name = step.get("name", "")
            if name not in STEP_REGISTRY:
                raise ValueError(f"未知步骤: '{name}'")
            fn = _STEP_FN_MAP.get(name)
            if fn is None:
                raise ValueError(f"步骤 '{name}' 的函数引用未找到")
            resolved_step = {
                "name": name,
                "fn": fn,
            }
            # 保留用户自定义参数（主循环按 edited_params > step.params > run.params 合并）
            if step.get("params"):
                resolved_step["params"] = step["params"]
            # 保留 Schema v2 字段：条件执行 / 声明式动作门
            if step.get("condition"):
                resolved_step["condition"] = step["condition"]
            if step.get("gate"):
                resolved_step["gate"] = step["gate"]
            resolved_steps.append(resolved_step)
        phases.append({
            "id": phase.get("id", ""),
            "title": phase.get("title", ""),
            "steps": resolved_steps,
        })

    return {
        "id": template_dict.get("id", ""),
        "title": template_dict.get("title", ""),
        "phases": phases,
    }


# ═══════════════════════════════════════════════════════════════════════
# Template Validation
# ═══════════════════════════════════════════════════════════════════════

def validate_template(template_dict, step_registry=None):
    """验证用户自定义模板。返回 (errors: list[str], warnings: list[str])。

    errors 非空时模板不应保存；warnings 仅做提示。
    """
    if step_registry is None:
        step_registry = STEP_REGISTRY

    errors = []
    warnings = []

    tid = template_dict.get("id", "")
    title = template_dict.get("title", "")
    phases = template_dict.get("phases", [])

    # V1: ID 格式
    if not re.match(r'^[a-z0-9_]{2,64}$', tid):
        errors.append("模板ID格式无效（仅允许小写字母、数字、下划线，2-64字符）")

    # V11: 内置模板不可通过此接口覆盖
    if template_dict.get("source") == "builtin":
        errors.append("内置模板不可覆盖，请先克隆为自定义模板再编辑")

    # V2: 至少 1 个阶段，每阶段至少 1 个步骤
    if not phases:
        errors.append("至少需要一个阶段")
    else:
        for pi, phase in enumerate(phases):
            step_list = phase.get("steps", [])
            if not step_list:
                errors.append(f"阶段 {pi+1}（{phase.get('title', '未命名')}）不能为空")

    # V13: 阶段 ID 唯一
    phase_ids = [p.get("id", "") for p in phases]
    if len(phase_ids) != len(set(phase_ids)):
        errors.append("阶段ID不能重复")

    # V3: 所有步骤名在注册表中
    step_names = []
    for phase in phases:
        for step in phase.get("steps", []):
            name = step.get("name", "")
            if not name:
                errors.append(f"阶段 '{phase.get('title', '?')}' 中存在无名步骤")
            elif name not in step_registry:
                errors.append(f"未知步骤: '{name}'")
            step_names.append(name)

    # V14-V16: Schema v2 字段校验（condition / gate / params 键）
    _COND_OPS = ("gt", "ge", "lt", "le", "eq", "ne", "exists", "not_exists", "contains")
    _GATE_TYPES = ("action",)
    for phase in phases:
        for step in phase.get("steps", []):
            name = step.get("name", "")
            meta = step_registry.get(name)
            label = meta["title"] if meta else (name or "?")

            # V14: condition 结构
            cond = step.get("condition")
            if cond is not None:
                if not isinstance(cond, dict):
                    errors.append(f"步骤 '{label}' 的 condition 必须是对象")
                else:
                    ck = cond.get("key")
                    cop = cond.get("op")
                    if not isinstance(ck, str) or not (
                            ck == "goal" or ck.startswith("artifacts.") or ck.startswith("params.")):
                        errors.append(f"步骤 '{label}' 的 condition.key 仅支持 artifacts.*/params.*/goal")
                    if cop not in _COND_OPS:
                        errors.append(f"步骤 '{label}' 的 condition.op 非法: {cop!r}")
                    elif cop not in ("exists", "not_exists") and "value" not in cond:
                        errors.append(f"步骤 '{label}' 的 condition.op={cop} 需要 value")

            # V15: gate 结构
            gate = step.get("gate")
            if gate is not None:
                if not isinstance(gate, dict):
                    errors.append(f"步骤 '{label}' 的 gate 必须是对象")
                else:
                    gtype = gate.get("type", "action")
                    if gtype not in _GATE_TYPES:
                        errors.append(f"步骤 '{label}' 的 gate.type 非法: {gtype!r}（支持: {', '.join(_GATE_TYPES)}）")
                    gto = gate.get("timeout")
                    if gto is not None and (isinstance(gto, bool)
                                            or not isinstance(gto, (int, float)) or gto <= 0):
                        errors.append(f"步骤 '{label}' 的 gate.timeout 必须是正数")

            # V16: params 键应在该步骤 user_configurable_params 内（warning）
            sp = step.get("params")
            if sp and meta:
                if not isinstance(sp, dict):
                    errors.append(f"步骤 '{label}' 的 params 必须是对象")
                else:
                    allowed = set(meta.get("user_configurable_params", []))
                    for k in sp:
                        if k not in allowed:
                            warnings.append(f"步骤 '{label}' 的参数 '{k}' 不在 user_configurable_params 内")

    if not step_names:
        return errors, warnings

    # V4-V8: 顺序约束
    for ni, name in enumerate(step_names):
        meta = step_registry.get(name)
        if not meta:
            continue

        for req_before in meta.get("requires_steps_before", []):
            if req_before not in step_names[:ni]:
                errors.append(f"步骤 '{meta['title']}' 必须在 '{step_registry.get(req_before, {}).get('title', req_before)}' 之后")

        for req_after in meta.get("requires_steps_after", []):
            if req_after not in step_names[ni + 1:]:
                errors.append(f"步骤 '{meta['title']}' 必须在 '{step_registry.get(req_after, {}).get('title', req_after)}' 之前")

    # V10: 产物链路可满足（传递闭包）
    produced = set()
    for name in step_names:
        meta = step_registry.get(name)
        if not meta:
            continue
        for req in meta.get("requires_artifacts", []):
            if req not in produced:
                warnings.append(f"步骤 '{meta['title']}' 需要产物 '{req}'，但该产物可能尚未生成")
        produced.update(meta.get("produces_artifacts", []))

    return errors, warnings
