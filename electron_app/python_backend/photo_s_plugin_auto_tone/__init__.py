"""photo-s-plugin-auto-tone: AI 自动调色插件（photo-s 官方插件）

CLIP/SigLIP+MLP 预测 9 字段 Lightroom 调色参数，可选 RAG 增强、
Qwen3-VL 美学评分、修图建议、风格化调色（SigLIP 视觉分析 + Qwen
解析）与场景自适应偏置。所有重依赖（torch/open_clip/transformers）
懒加载——本包可安全地被 photo-s 的 entry-point 发现机制导入。

用法：
    from photo_s_plugin_auto_tone import auto_tone, auto_tone_with_style
    result = auto_tone("/path/to/img.jpg")
    styled = auto_tone_with_style("/path/to/img.jpg", "忧郁蓝调")
"""

__version__ = "2.3.0"

__all__ = [
    "AutoTonePlugin",
    # 便捷入口
    "auto_tone",
    "aesthetic_score",
    "tone_advisor",
    "auto_tone_with_style",
    "auto_tone_with_scene",
    "analyze_visual_style",
    "list_styles",
    # Schema
    "INPUT_SCHEMA",
    "OUTPUT_SCHEMA",
    "STYLE_INPUT_SCHEMA",
    "STYLE_OUTPUT_SCHEMA",
    "VISUAL_STYLE_OUTPUT_SCHEMA",
    "SCENE_INPUT_SCHEMA",
    "SCENE_OUTPUT_SCHEMA",
]


def __getattr__(name):
    # 懒加载：torch 未安装时 import 本包依然可用
    if name == "AutoTonePlugin":
        from .plugin import AutoTonePlugin
        return AutoTonePlugin
    if name in ("INPUT_SCHEMA", "OUTPUT_SCHEMA", "STYLE_INPUT_SCHEMA",
                "STYLE_OUTPUT_SCHEMA", "VISUAL_STYLE_OUTPUT_SCHEMA",
                "SCENE_INPUT_SCHEMA", "SCENE_OUTPUT_SCHEMA"):
        from .core import schema
        return getattr(schema, name)
    if name == "list_styles":
        from .core.style import list_styles
        return list_styles
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def auto_tone(image_path, strength=1.0, render=True, use_rag=True,
              output_path=None, **kwargs):
    """便捷入口：单图自动调色

    Returns:
        dict: options / confidence / warnings / rendered_path / metadata
    """
    from .core.pipeline import run_auto_tone
    return run_auto_tone(image_path, strength=strength, render=render,
                         use_rag=use_rag, output_path=output_path, **kwargs)


def aesthetic_score(image_path):
    """便捷入口：美学评分 1-10（需 qwen extra）"""
    from .core.aesthetic import AestheticScorer
    return AestheticScorer().score(image_path)


def tone_advisor(image_path, current_options=None):
    """便捷入口：修图建议（需 qwen extra）"""
    from .core.advisor import ToneAdvisor
    return ToneAdvisor().advise(image_path, current_options or {})


def auto_tone_with_style(image_path, style_desc=None, **kwargs):
    """便捷入口：单图风格化自动调色（需 model extra；Qwen 可选）

    Args:
        image_path: 图像绝对路径
        style_desc: 风格描述（任意自然语言；None 时自动视觉分析）

    Returns:
        dict: schema_version=2, options / bias / bias_source / style_desc /
              visual_styles / rendered_path / warnings / metadata
    """
    from .core.style import auto_tone_with_style as _impl
    return _impl(image_path, style_desc, **kwargs)


def auto_tone_with_scene(image_path, scene=None, **kwargs):
    """便捷入口：场景自适应自动调色（需 model extra）

    Args:
        image_path: 图像绝对路径
        scene: 场景 key（None 时为 'default'）

    Returns:
        dict: schema_version=1, options / scene / scene_bias /
              rendered_path / metadata
    """
    from .core.scene import auto_tone_with_scene as _impl
    return _impl(image_path, scene, **kwargs)


def analyze_visual_style(image_path, top_k=3):
    """便捷入口：SigLIP 视觉风格分析，返回 [(style_key, confidence), ...]"""
    from .core.style import analyze_visual_style as _impl
    return _impl(image_path, top_k=top_k)
