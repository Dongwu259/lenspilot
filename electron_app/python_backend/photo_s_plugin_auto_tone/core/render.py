"""渲染模块：把 9 字段 options 应用到图像（numpy 简化渲染）

只渲染 exposure / contrast / saturation 三个核心字段；完整渲染
（vibrance/wb/clarity/...）建议把 options 交给 photo_s 引擎管线
（engine.batch_process），字段名与 ProcessOptions 对齐。
"""

import os

import numpy as np
from PIL import Image

# 各字段的"中性值"（等于不改图）
NEUTRAL = {
    "exposure": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "vibrance": 0.0,
    # 5250 = the LR baseline lrxmp trains against (_NEUTRAL there) — the
    # old 5500 pulled strength<1 renders systematically warm
    "wb_temp": 5250.0,
    "wb_tint": 0.0,
    "clarity": 0.0,
    "texture": 0.0,
    "dehaze": 0.0,
}


def apply_strength(options: dict, strength: float) -> dict:
    """把 options 向中性值插值（strength=0 → 原图，1 → 完全采用预测）"""
    if strength >= 1.0:
        return dict(options)
    out = {}
    for f, v in options.items():
        neutral = NEUTRAL.get(f, 0.0)
        out[f] = float(neutral + strength * (v - neutral))
    return out


def render_options(
    img: Image.Image,
    options: dict,
    output_path: str,
    strength: float = 1.0,
    local: list = None,
) -> str:
    """根据 options（+ 局部调整）渲染调整后的图

    v2.4 起委托 photo_s.autotone.apply_auto_tone_params 走引擎真实管线
    （9 字段全量 + 蒙版局部调整；旧的 numpy 简化渲染只落 3 个字段，
    且无法表达 local）。photo_s 不可用时回落旧简化渲染（独立使用场景）。

    Args:
        img: 输入 PIL.Image
        options: 9 字段 dict（实际值空间）
        output_path: 输出文件路径
        strength: 调色强度 0-1（options 未经 apply_strength 预缩放时用）
        local: [{region, params}] 局部调整（应已施加 strength）

    Returns:
        output_path

    Raises:
        失败时直接抛出异常（由调用方决定如何降级），
        不再静默保存原图冒充结果。
    """
    opts = apply_strength(options, strength)

    out = None
    try:
        from photo_s.autotone import apply_auto_tone_params
        out = apply_auto_tone_params(img, {"options": opts, "local": local})
        out = out.convert("RGB") if out.mode != "RGB" else out
    except ImportError:
        out = None  # photo_s 核心不可用（独立安装）→ 走旧简化渲染

    if out is None:
        import numpy as np

        arr = np.asarray(img.convert("RGB")).astype(np.float32)

        # 曝光（线性 EV）
        if 'exposure' in opts:
            ev = opts['exposure']
            arr = arr * (2 ** ev)

        # 对比度（围绕图像均值；contrast 在 [0.5, 1.5]，1.0 = 不变）
        if 'contrast' in opts:
            gain = opts['contrast'] - 1.0
            mean = arr.mean(axis=(0, 1), keepdims=True)
            arr = mean + (arr - mean) * (1 + gain)

        # 饱和度（saturation 在 [0, 2]，1.0 = 不变）
        if 'saturation' in opts:
            gain = opts['saturation'] - 1.0
            luma = (0.299 * arr[..., 0] + 0.587 * arr[..., 1]
                    + 0.114 * arr[..., 2])[..., None]
            arr = luma + (arr - luma) * (1 + gain)

        arr = np.clip(arr, 0, 255).astype(np.uint8)
        out = Image.fromarray(arr)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or '.', exist_ok=True)
    out.save(output_path, quality=95)

    return output_path
