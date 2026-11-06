"""JSON Schema 定义（input/output contract）"""

INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AutoToneInput",
    "type": "object",
    "properties": {
        "image_path": {"type": "string", "description": "图像绝对路径"},
        "strength": {
            "type": "number",
            "default": 1.0,
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "调色强度 0=原图 1=完全采用模型预测",
        },
        "render": {
            "type": "boolean",
            "default": True,
            "description": "是否渲染输出图",
        },
        "use_rag": {
            "type": "boolean",
            "default": True,
            "description": "是否启用 RAG 检索增强",
        },
        "use_advisor": {
            "type": "boolean",
            "default": False,
            "description": "低置信度时启用 advisor 修正",
        },
        "output_path": {
            "type": ["string", "null"],
            "default": None,
            "description": "自定义输出路径",
        },
    },
    "required": ["image_path"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AutoToneOutput",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "options": {
            "type": "object",
            "description": "9 字段调整参数",
            "properties": {
                "exposure": {"type": "number"},
                "contrast": {"type": "number"},
                "saturation": {"type": "number"},
                "vibrance": {"type": "number"},
                "wb_temp": {"type": "number"},
                "wb_tint": {"type": "number"},
                "clarity": {"type": "number"},
                "texture": {"type": "number"},
                "dehaze": {"type": "number"},
            },
            "required": ["exposure", "contrast", "saturation"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "0-1 置信度，<0.4 建议人工复审",
        },
        "local": {
            "type": "array",
            "description": (
                "局部调整（v2.4 词汇表扩展，可选键——模型带局部头且预测"
                "非中性时携带）。region ∈ subject/person/object:label"
                "（v1.8 AI 蒙版词汇表）；params 为 mask.ADJUST_KEYS 标量子集"
                "（中性值全 0，语义与手动 mask_adjust 一致）。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "params": {
                        "type": "object",
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["region", "params"],
            },
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "提示信息列表",
        },
        "rendered_path": {
            "type": ["string", "null"],
            "description": "渲染图保存路径，render=False 时为 null",
        },
        "metadata": {
            "type": "object",
            "properties": {
                "psnr_estimate": {"type": "number"},
                "rag_used": {"type": "boolean"},
                "anomaly_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "model_version": {"type": "string"},
                "duration_ms": {"type": "number"},
            },
        },
    },
    "required": ["schema_version", "options", "confidence", "warnings"],
}

# 风格化输入输出 schema
STYLE_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "StyleAutoToneInput",
    "type": "object",
    "properties": {
        "image_path": {"type": "string", "description": "图像绝对路径"},
        "style_desc": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "风格描述（任意自然语言，如'忧郁蓝调'、'电影感'、'暖色夕阳中的城市'）。"
                "为 None 时自动用 SigLIP 视觉分析选择风格。"
            ),
        },
        "strength": {
            "type": "number",
            "default": 1.0,
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "风格强度 0=原图 1=完全采用风格偏置",
        },
        "use_qwen": {
            "type": "boolean",
            "default": True,
            "description": "True=Qwen3-VL 解析风格描述，False=用预设偏置（无需 Qwen）",
        },
        "render": {
            "type": "boolean",
            "default": True,
            "description": "是否渲染输出图",
        },
        "output_path": {
            "type": ["string", "null"],
            "default": None,
            "description": "自定义输出路径",
        },
    },
    "required": ["image_path"],
    "additionalProperties": False,
}


STYLE_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "StyleAutoToneOutput",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 2},
        "options": {
            "type": "object",
            "description": "9 字段调整参数（已叠加风格偏置）",
            "properties": {
                "exposure": {"type": "number"},
                "contrast": {"type": "number"},
                "saturation": {"type": "number"},
                "vibrance": {"type": "number"},
                "wb_temp": {"type": "number"},
                "wb_tint": {"type": "number"},
                "clarity": {"type": "number"},
                "texture": {"type": "number"},
                "dehaze": {"type": "number"},
            },
            "required": ["exposure", "contrast", "saturation"],
        },
        "bias": {
            "type": "object",
            "description": "应用的 9 字段偏置（归一化空间 [-1, 1]，saturation 可为 [-2, 2]）",
            "properties": {
                "exposure": {"type": "number"},
                "contrast": {"type": "number"},
                "saturation": {"type": "number"},
                "vibrance": {"type": "number"},
                "wb_temp": {"type": "number"},
                "wb_tint": {"type": "number"},
                "clarity": {"type": "number"},
                "texture": {"type": "number"},
                "dehaze": {"type": "number"},
            },
        },
        "bias_source": {
            "type": "string",
            "enum": ["qwen", "preset"],
            "description": "偏置来源：'qwen' Qwen 解析 / 'preset' 手工预设",
        },
        "style_desc": {
            "type": "string",
            "description": "实际应用的风格描述（自动模式下为视觉分析 top-1）",
        },
        "visual_styles": {
            "type": "array",
            "description": "SigLIP 视觉风格分析 top-3（含 style_key/style_cn/confidence）",
            "items": {
                "type": "object",
                "properties": {
                    "style_key": {"type": "string"},
                    "style_cn": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "rendered_path": {
            "type": ["string", "null"],
            "description": "渲染图保存路径",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
        "metadata": {
            "type": "object",
            "properties": {
                "strength": {"type": "number"},
                "base_model_version": {"type": "string"},
                "duration_ms": {"type": "number"},
            },
        },
    },
    "required": ["schema_version", "options", "bias", "style_desc"],
}


# 视觉分析输出 schema
VISUAL_STYLE_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "VisualStyleOutput",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "image_path": {"type": "string"},
        "top_styles": {
            "type": "array",
            "description": "Top-K 视觉风格（含 style_key/style_cn/confidence）",
            "items": {
                "type": "object",
                "properties": {
                    "style_key": {"type": "string"},
                    "style_cn": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
    "required": ["schema_version", "top_styles"],
}


# 场景自适应输入输出 schema
SCENE_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SceneAutoToneInput",
    "type": "object",
    "properties": {
        "image_path": {"type": "string", "description": "图像绝对路径"},
        "scene": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "场景 key（None=自动检测）。支持: portrait/bw/bw_landscape/bw_high_contrast/"
                "soft/soft_haze/auto/warm/cool/cool_dramatic/split_tone/landscape_dramatic"
            ),
        },
        "strength": {
            "type": "number",
            "default": 0.5,
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "场景偏置强度 0-1（推荐 0.3-0.5）",
        },
        "render": {
            "type": "boolean",
            "default": True,
        },
        "output_path": {
            "type": ["string", "null"],
            "default": None,
        },
    },
    "required": ["image_path"],
    "additionalProperties": False,
}


SCENE_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SceneAutoToneOutput",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "options": {
            "type": "object",
            "description": "9 字段调整参数（已叠加场景偏置）",
            "properties": {
                "exposure": {"type": "number"},
                "contrast": {"type": "number"},
                "saturation": {"type": "number"},
                "vibrance": {"type": "number"},
                "wb_temp": {"type": "number"},
                "wb_tint": {"type": "number"},
                "clarity": {"type": "number"},
                "texture": {"type": "number"},
                "dehaze": {"type": "number"},
            },
            "required": ["exposure", "contrast", "saturation"],
        },
        "scene": {
            "type": "string",
            "description": "应用的场景 key",
        },
        "scene_bias": {
            "type": "object",
            "description": "应用的 9 字段场景偏置（归一化空间 [-1, 1]）",
        },
        "rendered_path": {
            "type": ["string", "null"],
        },
        "metadata": {
            "type": "object",
            "properties": {
                "strength": {"type": "number"},
                "duration_ms": {"type": "number"},
            },
        },
    },
    "required": ["schema_version", "options", "scene"],
}
