"""LangChain 工具封装"""
import json
from typing import Optional


def get_langchain_tool():
    """获取 LangChain Tool 实例

    Returns:
        langchain.tools.BaseTool
    """
    try:
        from langchain.tools import BaseTool
        from pydantic import BaseModel, Field
    except ImportError:
        raise ImportError(
            "LangChain not installed. Install with: pip install langchain"
        )

    class AutoToneInput(BaseModel):
        image_path: str = Field(..., description="图像绝对路径（jpg/png/raw）")
        strength: float = Field(
            default=1.0,
            description="调色强度 0-1，0=原图 1=完全采用模型预测",
        )
        render: bool = Field(default=True, description="是否渲染输出图")

    class AutoToneTool(BaseTool):
        name = "auto_tone"
        description = (
            "调用 AI 自动调色模型。基于 CLIP+MLP 的图像理解模型，"
            "返回 9 字段调整参数（曝光/对比度/饱和度/鲜艳度/白平衡/清晰度等）"
            "以及置信度评分。适用于批量修图场景。"
        )
        args_schema = AutoToneInput

        def _run(self, image_path: str, strength: float = 1.0, render: bool = True) -> str:
            from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone
            result = run_auto_tone(
                image_path, strength=strength, render=render, use_rag=True
            )
            return json.dumps(result, ensure_ascii=False)

        async def _arun(self, image_path: str, strength: float = 1.0, render: bool = True) -> str:
            return self._run(image_path, strength, render)

    return AutoToneTool()


def get_aesthetic_tool():
    """获取 LangChain 美学评分 Tool"""
    try:
        from langchain.tools import BaseTool
        from pydantic import BaseModel, Field
    except ImportError:
        raise ImportError("pip install langchain")

    class AestheticInput(BaseModel):
        image_path: str = Field(..., description="图像绝对路径")

    class AestheticTool(BaseTool):
        name = "aesthetic_score"
        description = (
            "评估图像美学质量（1-10 分）。基于 Qwen3-VL + AVA 美学数据集微调。"
            "返回分数 + bucket (low/medium-low/medium/medium-high/high) + 置信度。"
        )
        args_schema = AestheticInput

        def _run(self, image_path: str) -> str:
            from photo_s_plugin_auto_tone.core.aesthetic import AestheticScorer
            scorer = AestheticScorer()
            result = scorer.score(image_path)
            return json.dumps({
                "schema_version": 1,
                **result,
            }, ensure_ascii=False)

        async def _arun(self, image_path: str) -> str:
            return self._run(image_path)

    return AestheticTool()


def get_style_tool():
    """获取 LangChain 风格化 Tool"""
    try:
        from langchain.tools import BaseTool
        from pydantic import BaseModel, Field
    except ImportError:
        raise ImportError("pip install langchain")

    class StyleInput(BaseModel):
        image_path: str = Field(..., description="图像绝对路径")
        style_desc: Optional[str] = Field(
            default=None,
            description=(
                "风格描述（任意自然语言，如'忧郁蓝调'、'电影感'、"
                "'暖色夕阳中的城市'）。None 时自动视觉分析。"
            ),
        )
        strength: float = Field(
            default=1.0, ge=0.0, le=1.0,
            description="风格强度 0-1",
        )
        use_qwen: bool = Field(
            default=True,
            description="True=Qwen 解析（需 qwen extra），False=预设（快速）",
        )

    class StyleTool(BaseTool):
        name = "auto_tone_with_style"
        description = (
            "风格化自动调色。基于 SigLIP 视觉分析 + Qwen3-VL 风格解析，"
            "支持任意自然语言风格描述（如'忧郁蓝调'、'电影感'、'暖色夕阳中的城市'）。"
            "返回 9 字段调整参数、风格偏置、视觉 top-3 风格。"
            "适用于按风格统一处理的批量场景。"
        )
        args_schema = StyleInput

        def _run(self, image_path: str, style_desc: Optional[str] = None,
                 strength: float = 1.0, use_qwen: bool = True) -> str:
            from photo_s_plugin_auto_tone.core.style import auto_tone_with_style
            result = auto_tone_with_style(
                image_path, style_desc=style_desc, strength=strength,
                use_qwen=use_qwen, render=False,  # LangChain 调用通常只取参数
            )
            return json.dumps(result, ensure_ascii=False)

        async def _arun(self, image_path: str, style_desc: Optional[str] = None,
                        strength: float = 1.0, use_qwen: bool = True) -> str:
            return self._run(image_path, style_desc, strength, use_qwen)

    return StyleTool()


def get_visual_style_tool():
    """获取 LangChain 视觉风格分析 Tool"""
    try:
        from langchain.tools import BaseTool
        from pydantic import BaseModel, Field
    except ImportError:
        raise ImportError("pip install langchain")

    class VisualStyleInput(BaseModel):
        image_path: str = Field(..., description="图像绝对路径")
        top_k: int = Field(default=3, ge=1, le=16, description="返回前 K 个风格")

    class VisualStyleTool(BaseTool):
        name = "analyze_visual_style"
        description = (
            "分析图像的艺术风格倾向。用 SigLIP 计算图像与 16 种风格描述的相似度。"
            "返回 top-K 风格及置信度。适用于自动归类、风格迁移前奏等场景。"
        )
        args_schema = VisualStyleInput

        def _run(self, image_path: str, top_k: int = 3) -> str:
            from photo_s_plugin_auto_tone.api.mcp_tools import STYLE_CN_MAP
            from photo_s_plugin_auto_tone.core.style import analyze_visual_style
            top_styles = analyze_visual_style(image_path, top_k=top_k)
            return json.dumps({
                "schema_version": 1,
                "image_path": image_path,
                "top_styles": [
                    {"style_key": k, "style_cn": STYLE_CN_MAP.get(k, k),
                     "confidence": round(c, 4)}
                    for k, c in top_styles
                ],
            }, ensure_ascii=False)

        async def _arun(self, image_path: str, top_k: int = 3) -> str:
            return self._run(image_path, top_k)

    return VisualStyleTool()