"""风格化自动调色实验

8 种风格 × 9 字段偏置向量：

风格设计原则：
- 偏置值在 [-1, 1] 范围（与训练归一化空间一致）
- 每个风格有 3-5 个非零偏置，避免过度修改
- 基于真实摄影后期风格知识

应用公式：
  final_options[field] = base_options[field] + bias[field] * strength * (range_max - range_min) / 2

注意：wb_temp 范围 [2000, 10000]，单位为 K，需要特殊处理
"""

# 风格偏置表（手工设计，基于摄影后期知识）
STYLE_BIASES = {
    # 1. 忧郁蓝调（cool blue, low saturation, slight haze）
    "melancholy_blue": {
        "exposure": -0.05,
        "contrast": -0.05,
        "saturation": -0.25,
        "vibrance": -0.15,
        "wb_temp": -0.35,  # 偏冷
        "wb_tint": 0.0,
        "clarity": -0.10,
        "texture": -0.05,
        "dehaze": +0.15,  # 雾感
    },

    # 2. 复古胶片（warm yellow tint, faded blacks）
    "vintage_film": {
        "exposure": -0.05,
        "contrast": -0.20,
        "saturation": -0.20,
        "vibrance": -0.10,
        "wb_temp": +0.20,  # 偏暖（vs 日光 5500K 升到 6500K 反而偏黄）
        "wb_tint": -0.15,
        "clarity": -0.20,
        "texture": -0.30,
        "dehaze": +0.10,
    },

    # 3. 清新自然（light, vibrant greens）
    "fresh_natural": {
        "exposure": +0.10,
        "contrast": +0.05,
        "saturation": +0.10,
        "vibrance": +0.20,
        "wb_temp": -0.10,
        "wb_tint": 0.0,
        "clarity": +0.10,
        "texture": +0.05,
        "dehaze": -0.10,
    },

    # 4. 电影感（teal & orange, high contrast, slight crop）
    "cinematic": {
        "exposure": -0.05,
        "contrast": +0.15,
        "saturation": -0.15,
        "vibrance": -0.10,
        "wb_temp": -0.15,  # 整体偏冷
        "wb_tint": +0.05,
        "clarity": +0.05,
        "texture": -0.10,
        "dehaze": +0.15,
    },

    # 5. 高对比黑白
    "high_contrast_bw": {
        "exposure": 0.0,
        "contrast": +0.30,
        "saturation": -2.00,  # 超出 [-1, 1] 以确保完全黑白
        "vibrance": -1.00,
        "wb_temp": 0.0,
        "wb_tint": 0.0,
        "clarity": +0.15,
        "texture": +0.10,
        "dehaze": -0.10,
    },

    # 6. 暖色黄昏（golden hour）
    "golden_hour": {
        "exposure": 0.0,
        "contrast": +0.10,
        "saturation": +0.15,
        "vibrance": +0.20,
        "wb_temp": +0.40,  # 明显偏暖（5500K→7000K 视觉等效）
        "wb_tint": -0.05,
        "clarity": +0.10,
        "texture": -0.05,
        "dehaze": +0.05,
    },

    # 7. 冷色清晨（blue hour, pre-sunrise）
    "cool_dawn": {
        "exposure": +0.10,
        "contrast": -0.05,
        "saturation": -0.05,
        "vibrance": -0.05,
        "wb_temp": -0.40,
        "wb_tint": 0.0,
        "clarity": -0.10,
        "texture": 0.0,
        "dehaze": -0.05,
    },

    # 8. 黑白纪实（news/documentary style）
    "docu_bw": {
        "exposure": 0.0,
        "contrast": +0.20,
        "saturation": -2.00,  # 强制完全黑白
        "vibrance": -1.00,
        "wb_temp": 0.0,
        "wb_tint": 0.0,
        "clarity": +0.10,
        "texture": +0.05,
        "dehaze": 0.0,
    },
}


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


def apply_style_bias(base_options, bias, strength=1.0):
    """应用风格偏置

    字段缩放规则（参考实际摄影后期经验）：
    - wb_temp: [-1, 1] → ±2000K（实际很少超过 ±1500K）
    - wb_tint: [-1, 1] → ±30（±30 magenta/green）
    - 其他: [-1, 1] → 实际范围全宽（如 exposure ±2EV, contrast ±0.5）

    Args:
        base_options: SigLIP 基础预测 dict
        bias: 风格偏置 dict
        strength: 风格强度 0-1

    Returns:
        修改后的 options dict
    """
    # 字段缩放因子（相对于 [0, 1] 归一化空间）
    SCALE = {
        "exposure": 1.0,    # full range
        "contrast": 0.5,    # ±0.5 of range = ±0.25 contrast
        "saturation": 0.5,
        "vibrance": 1.0,
        "wb_temp": 0.3,     # ±0.3 of [2000, 10000] = ±1200K
        "wb_tint": 0.3,     # ±0.3 of [-100, 100] = ±30
        "clarity": 1.0,
        "texture": 1.0,
        "dehaze": 1.0,
    }
    RANGES = {
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
    result = dict(base_options)
    for field, bias_val in bias.items():
        if field not in result:
            continue
        lo, hi = RANGES[field]
        scale = SCALE[field]
        # 偏置作用在归一化空间 [-1, 1]，按 scale 缩放
        delta = bias_val * strength * scale * (hi - lo) / 2
        result[field] = result[field] + delta
        # 实际值范围 clip
        result[field] = max(lo, min(hi, result[field]))
    return result


if __name__ == '__main__':
    # 测试
    base = {
        "exposure": -0.5,
        "contrast": 0.85,
        "saturation": 1.0,
        "vibrance": 0.0,
        "wb_temp": 5500.0,
        "wb_tint": 0.0,
        "clarity": -0.1,
        "texture": -0.1,
        "dehaze": 0.0,
    }
    for style_name, bias in STYLE_BIASES.items():
        result = apply_style_bias(base, bias, strength=1.0)
        print(f'{style_name}:')
        for f in ['exposure', 'contrast', 'saturation', 'wb_temp']:
            print(f'  {f}: {base[f]:.3f} -> {result[f]:.3f}')
        print()