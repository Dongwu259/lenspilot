"""Plugin 钩子实现（photo_s.plugins entry-point）"""
import os
from typing import List, Optional

from photo_s.hooks import PhotoSPlugin, PluginContext
from photo_s.modelstore import WeightSpec

from . import models


class AutoTonePlugin(PhotoSPlugin):
    """photo-s 插件：AI 自动调色 + 美学验证

    注册方式（pyproject.toml）：
        [project.entry-points."photo_s.plugins"]
        auto_tone = "photo_s_plugin_auto_tone:AutoTonePlugin"

    提供操作：
        - auto_tone_params: 参数协议（v2.4——引擎真实管线应用 9 字段 + local）
        - auto_tone: 像素协议（兼容旧宿主）
        - auto_tone_with_style: 风格化调色（v2.1）
        - verify: 美学验证（v2.4——audit 的 reward 闸门）
        - embed: SigLIP 图像/文本嵌入（v2.5——核心 photo-s index/find 的
          语义搜索槽位，塔与风格化共用零新增下载）
    """

    name = "auto_tone"

    provides = ("auto_tone", "auto_tone_with_style", "verify", "embed")

    # photo_s.search 的 _PluginExtractor 直接读取（索引记录抽取器身份）
    embed_name = "siglip:ViT-L-16-SigLIP-384"
    embed_dim = 1024

    def embed_images(self, paths, batch_size: int = 8):
        """provider 槽位 ``embed``（v2.5）：图像路径列表 → (N, 1024) float32。"""
        from .core.embed import get_embedder

        return get_embedder().embed_images(paths, batch_size=batch_size)

    def embed_texts(self, texts):
        """provider 槽位 ``embed``：文本列表 → (M, 1024) float32。"""
        from .core.embed import get_embedder

        return get_embedder().embed_texts(texts)

    def register_mcp_tools(self, mcp) -> None:
        """v2.3 wiring: photo-s mcp 启动时调用（hooks.PhotoSPlugin 协议）。"""
        from .api.mcp_tools import register_mcp_tools
        register_mcp_tools(mcp)

    def register_rest(self, handler_class) -> None:
        """v2.3 wiring: photo-s serve 启动时调用（hooks.PhotoSPlugin 协议）。"""
        from .api.rest import register_routes
        register_routes(handler_class)

    def verify(self, image, ctx: Optional[PluginContext] = None) -> dict:
        """provider 槽位 ``verify``（v2.4）：美学验证，audit 的 reward 闸门。

        Args:
            image: 图像路径（str）或 PIL.Image
            ctx: PluginContext（未用，协议对齐）

        Returns:
            ``{score 1-10, bucket, source, confidence, loaded}``；
            无可用 verifier 时 ``score=None`` + 指引（不静默给分）。
        """
        from .core.verifier import verify_aesthetic

        return verify_aesthetic(image)

    def weight_specs(self) -> List[WeightSpec]:
        return models.weight_specs()

    def auto_tone_params(
        self,
        strength: float = 1.0,
        ctx: Optional[PluginContext] = None,
    ) -> dict:
        """v2.4 参数协议：返回预测参数（不渲染），由引擎真实管线应用。

        旧 auto_tone() 像素协议经插件 numpy 简化渲染，9 个预测字段只落
        3 个；本方法把 {options, local, confidence} 交回引擎，经
        photo_s.autotone.apply_auto_tone_params 全字段应用，局部调整过
        蒙版管线。options 已施加 strength。
        """
        from .core.pipeline import run_auto_tone

        input_path = ctx.input_path if ctx else None
        if not input_path:
            return {"options": {}, "local": [], "confidence": 0.0,
                    "warnings": ["no input_path in plugin context"]}

        result = run_auto_tone(
            image_path=input_path,
            strength=strength,
            render=False,
            use_rag=True,
            use_advisor=False,
        )
        return {
            "options": result.get("options", {}),
            "local": result.get("local", []),
            "confidence": result.get("confidence", 0.0),
            "warnings": result.get("warnings", []),
        }

    def auto_tone(
        self,
        img,
        strength: float = 1.0,
        ctx: Optional[PluginContext] = None,
    ):
        """在 photo_s 引擎中调用：返回调整后的 PIL.Image

        v2.4 起引擎优先走 auto_tone_params（真实管线）；本方法保留给
        未升级的宿主（像素协议），内部同样委托真实管线渲染。

        Args:
            img: PIL.Image
            strength: 调色强度 0-1
            ctx: PluginContext（包含 input_path, output_path 等）
        """
        from PIL import Image

        from .core.pipeline import run_auto_tone
        from .core.render import render_options

        input_path = ctx.input_path if ctx else None
        if not input_path:
            return img

        result = run_auto_tone(
            image_path=input_path,
            strength=strength,
            render=False,  # 渲染由本方法完成并返回 PIL Image
            use_rag=True,
            use_advisor=False,
        )

        options = result.get("options", {})
        if not options:
            return img

        import tempfile
        # Always render through JPEG: the temp file only ferries pixels back
        # into a PIL Image (the engine re-saves in the target format). A RAW
        # input suffix (.cr2/.nef/...) used to be kept and PIL cannot WRITE
        # RAW — every RAW input died with "unknown file extension".
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
        try:
            render_options(img, options, tmp_path, strength=1.0,
                           local=result.get("local"))
            return Image.open(tmp_path).copy()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def auto_tone_with_style(
        self,
        img,
        style_desc: Optional[str] = None,
        strength: float = 1.0,
        use_qwen: bool = True,
        ctx: Optional[PluginContext] = None,
    ):
        """v2.1: 风格化调色（在 photo_s 引擎 batch_process 中调用）

        Args:
            img: PIL.Image
            style_desc: 风格描述（None=SigLIP 自动视觉分析）
            strength: 风格强度 0-1
            use_qwen: 是否用 Qwen 解析（False=手工预设）
            ctx: PluginContext

        Returns:
            修改后的 PIL.Image
        """
        from PIL import Image

        from .core.render import render_options
        from .core.style import auto_tone_with_style as _style_tone

        input_path = ctx.input_path if ctx else None
        if not input_path:
            return img

        # strength 已在偏置叠加阶段生效，渲染时不再重复施加
        result = _style_tone(
            image_path=input_path,
            style_desc=style_desc,
            strength=strength,
            use_qwen=use_qwen,
            render=False,  # 渲染由本方法完成并返回 PIL Image
        )

        options = result.get("options", {})
        if not options:
            return img

        import tempfile
        # 同 auto_tone：恒走 JPEG 中转（PIL 写不了 RAW 后缀）
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
        try:
            render_options(img, options, tmp_path, strength=1.0)
            return Image.open(tmp_path).copy()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
