"""风格描述 → 参数偏置（Qwen 解析）

通过 Qwen VL + few-shot prompt，将自然语言风格描述转换为 9-dim 偏置向量。

对比两种实现：
1. 手工预设（style_biases.py）- 8 个固定风格
2. Qwen 解析（style_qwen.py）- 任意自然语言描述
"""
import json
import re
from typing import Dict, List

# 字段范围（实际值）
FIELD_RANGES = {
    "exposure": (-2.0, 2.0),
    "contrast": (0.5, 1.5),
    "saturation": (0.0, 2.0),
    "vibrance": (-1.0, 1.0),
    "wb_temp": (2000.0, 10000.0),
    "wb_tint": (-100.0, 100.0),
    "clarity": (-1.0, 1.0),
    "texture": (-1.0, 1.0),
    "dehaze": (-1.0, 1.0),
}

# 风格 prompt 设计
STYLE_PROMPT = """你是摄影后期调色专家。给定一个风格描述，输出对应的调色参数偏置（9 个字段，归一化到 [-1, 1] 空间，saturation 范围 [-2, 2]）。

字段说明：
- exposure: 曝光 [-1, 1]（-1 = 暗，+1 = 亮）
- contrast: 对比度 [-1, 1]（-1 = 平，+1 = 强）
- saturation: 饱和度 [-2, 2]（⚠ 重要：-2 = 强制黑白，-1 = 完全无饱和度，0 = 不变，+1 = 浓，+2 = 过饱和）
  - 含"黑白"、"黑白色"、"monochrome"等 → 必须给 -2（强制黑白）
  - 含"复古"、"胶片"、"电影感"等 → 给 -0.2 左右（保留彩色）
  - 注意区分：黑白照片 ≠ 复古（复古仍保留少量颜色）
- vibrance: 自然饱和度 [-1, 1]
- wb_temp: 色温 [-1, 1]（-1 = 冷蓝，+1 = 暖黄）
- wb_tint: 色调 [-1, 1]（-1 = 绿，+1 = 品红）
- clarity: 清晰度 [-1, 1]
- texture: 纹理 [-1, 1]
- dehaze: 去雾 [-1, 1]（-1 = 加雾，+1 = 去雾）

示例：
风格："忧郁蓝调" → {"exposure": -0.1, "contrast": -0.1, "saturation": -0.3, "vibrance": -0.2, "wb_temp": -0.4, "wb_tint": 0.0, "clarity": -0.1, "texture": -0.05, "dehaze": 0.2}
风格："复古胶片" → {"exposure": -0.1, "contrast": -0.2, "saturation": -0.2, "vibrance": -0.1, "wb_temp": 0.2, "wb_tint": -0.1, "clarity": -0.2, "texture": -0.3, "dehaze": 0.1}
风格："清新自然" → {"exposure": 0.1, "contrast": 0.05, "saturation": 0.1, "vibrance": 0.2, "wb_temp": -0.1, "wb_tint": 0.0, "clarity": 0.1, "texture": 0.05, "dehaze": -0.1}
风格："电影感" → {"exposure": -0.1, "contrast": 0.2, "saturation": -0.15, "vibrance": -0.1, "wb_temp": -0.2, "wb_tint": 0.05, "clarity": 0.1, "texture": -0.1, "dehaze": 0.2}
风格："暖色黄昏" → {"exposure": 0.0, "contrast": 0.1, "saturation": 0.15, "vibrance": 0.2, "wb_temp": 0.4, "wb_tint": -0.05, "clarity": 0.1, "texture": -0.05, "dehaze": 0.05}
风格："高对比黑白" → {"exposure": 0.0, "contrast": 0.3, "saturation": -2.0, "vibrance": -1.0, "wb_temp": 0.0, "wb_tint": 0.0, "clarity": 0.15, "texture": 0.1, "dehaze": -0.1}
风格："黑白纪实" → {"exposure": 0.0, "contrast": 0.2, "saturation": -2.0, "vibrance": -1.0, "wb_temp": 0.0, "wb_tint": 0.0, "clarity": 0.1, "texture": 0.05, "dehaze": 0.0}

现在请输出：
风格："%s"

只输出 JSON（不要其他文字）："""


def parse_qwen_style(qwen_model, tokenizer, style_description: str) -> Dict[str, float]:
    """用 Qwen 解析风格描述 → 9-dim 偏置

    Args:
        qwen_model: 已加载的 Qwen 模型
        tokenizer: Qwen tokenizer
        style_description: 风格描述

    Returns:
        9 字段偏置 dict
    """
    import torch

    prompt = STYLE_PROMPT % style_description
    messages = [{'role': 'user', 'content': prompt}]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    inputs = tokenizer(text, return_tensors='pt').to(qwen_model.device)
    with torch.no_grad():
        outputs = qwen_model.generate(
            **inputs,
            max_new_tokens=200,
            temperature=0.3,
            do_sample=True,
            top_p=0.9,
            pad_token_id=tokenizer.eos_token_id,
        )
    response = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
    return parse_json_bias(response, style_description)


def parse_json_bias(response: str, style_description: str) -> Dict[str, float]:
    """从 Qwen 输出中解析 JSON 偏置"""
    # 找 {...} JSON
    match = re.search(r'\{[^{}]+\}', response)
    if not match:
        print(f'  ⚠ no JSON found in response: {response[:200]}')
        return get_zero_bias()

    try:
        bias = json.loads(match.group(0))
        # 验证所有字段都存在
        for f in FIELD_RANGES:
            if f not in bias:
                bias[f] = 0.0
        return bias
    except json.JSONDecodeError as e:
        print(f'  ⚠ JSON parse error: {e}, response: {response[:200]}')
        return get_zero_bias()


def get_zero_bias():
    return {f: 0.0 for f in FIELD_RANGES}


# 风格关键词 → 手工偏置（兜底，不需 Qwen）
KEYWORD_BIASES = {
    "忧郁": {"saturation": -0.3, "wb_temp": -0.4, "dehaze": 0.2, "clarity": -0.1},
    "蓝调": {"wb_temp": -0.4, "saturation": -0.3, "dehaze": 0.2},
    "复古": {"saturation": -0.2, "contrast": -0.2, "texture": -0.3, "wb_temp": 0.2, "wb_tint": -0.1},
    "胶片": {"saturation": -0.2, "contrast": -0.2, "texture": -0.3},
    "清新": {"vibrance": 0.2, "saturation": 0.1, "exposure": 0.1, "clarity": 0.1},
    "自然": {"vibrance": 0.15, "saturation": 0.05, "exposure": 0.05},
    "电影": {"contrast": 0.2, "wb_temp": -0.2, "saturation": -0.15, "dehaze": 0.2},
    "暖": {"wb_temp": 0.4, "vibrance": 0.15, "saturation": 0.1},
    "黄昏": {"wb_temp": 0.4, "vibrance": 0.2, "saturation": 0.15},
    "夕阳": {"wb_temp": 0.5, "saturation": 0.2, "vibrance": 0.2},
    "冷": {"wb_temp": -0.4},
    "清晨": {"wb_temp": -0.4, "exposure": 0.1},
    "黑夜": {"exposure": -0.5, "contrast": 0.2, "clarity": 0.1},
    "黑": {"wb_temp": 0.0},
    "黑白": {"saturation": -2.0, "contrast": 0.2, "vibrance": -1.0},
    "纪实": {"saturation": -2.0, "contrast": 0.15, "vibrance": -0.5},
    "日系": {"exposure": 0.15, "saturation": -0.15, "contrast": -0.1, "wb_temp": 0.05, "vibrance": 0.1},
    "糖果": {"saturation": 0.4, "vibrance": 0.3, "contrast": 0.1},
    "赛博": {"wb_temp": -0.5, "saturation": 0.2, "contrast": 0.3, "vibrance": 0.2},
    "莫兰迪": {"saturation": -0.3, "vibrance": -0.1, "contrast": -0.1, "clarity": -0.1},
}


def keyword_to_bias(style_description: str) -> Dict[str, float]:
    """基于关键词的简单偏置生成（无需 Qwen）

    Args:
        style_description: 风格描述

    Returns:
        9 字段偏置 dict（合并所有匹配关键词）
    """
    bias = get_zero_bias()
    desc_lower = style_description
    for kw, kw_bias in KEYWORD_BIASES.items():
        if kw in desc_lower:
            for f, v in kw_bias.items():
                # 取最大值（避免冲突叠加过度）
                bias[f] = max(bias[f], v)
    return bias