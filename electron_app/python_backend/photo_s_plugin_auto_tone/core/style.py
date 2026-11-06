"""风格化自动调色

组合三种能力：
1. SigLIP 视觉分析（看图给风格）
2. Qwen3-VL 文本解析（自然语言 → 偏置；不可用时回退手工预设）
3. SigLIP base 预测（基础调色）+ 风格偏置叠加

API:
- StyleAutoTone: 主类（进程内单例）
- auto_tone_with_style: 便捷入口
- analyze_visual_style: 视觉风格分析
- list_styles: 16 种风格中英文映射

权重：auto_tone_siglip_h192_d03.pt（首次使用时经 photo_s.modelstore
从 GitHub Release 下载）；SigLIP 视觉塔与 text tokenizer 由 open_clip /
transformers 从 HuggingFace 拉取。Qwen 解析需 qwen extra。
"""
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from .. import models
from .predictor import AutoTonePredictor
from .render import render_options
from .style_biases import STYLE_BIASES, apply_style_bias
from .style_qwen import STYLE_PROMPT, parse_json_bias, get_zero_bias


# SigLIP text tokenizer（HF id；离线场景用 PHOTOS_AUTO_TONE_SIGLIP_TOKENIZER
# 指向本地快照目录）
SIGLIP_TOKENIZER = os.environ.get(
    "PHOTOS_AUTO_TONE_SIGLIP_TOKENIZER", "timm/ViT-L-16-SigLIP-384")


# 风格中文映射
STYLE_CN = {
    'melancholy_blue': '忧郁蓝调', 'vintage_film': '复古胶片', 'fresh_natural': '清新自然',
    'cinematic': '电影感', 'high_contrast_bw': '高对比黑白', 'golden_hour': '暖色黄昏',
    'cool_dawn': '冷色清晨', 'docu_bw': '黑白纪实', 'low_key': '低调暗调', 'high_key': '高调明亮',
    'urban_night': '都市夜景', 'portrait_warm': '暖色人像', 'landscape_vivid': '鲜艳风景',
    'film_noir': '黑色电影', 'pastel': '粉彩梦幻', 'minimalist': '极简主义',
}

# 中文→预设 key 反查
CN_TO_KEY = {v: k for k, v in STYLE_CN.items()}

# 风格视觉描述（用于 SigLIP 匹配）
STYLE_VISUAL_DESCS = {
    'melancholy_blue': ['melancholy blue tone', 'sad lonely atmosphere'],
    'vintage_film': ['vintage film look', 'faded retro photograph'],
    'fresh_natural': ['fresh natural daylight', 'clean bright scene'],
    'cinematic': ['cinematic dramatic scene', 'movie still aesthetic'],
    'high_contrast_bw': ['high contrast black and white', 'dramatic monochrome'],
    'golden_hour': ['golden hour sunset warmth', 'warm golden light'],
    'cool_dawn': ['cool blue dawn light', 'cold morning mist'],
    'docu_bw': ['documentary black and white', 'news photo style'],
    'low_key': ['low key dark moody', 'dark shadow photography'],
    'high_key': ['high key bright airy', 'soft overexposed lighting'],
    'urban_night': ['urban night cityscape', 'neon downtown after dark'],
    'portrait_warm': ['warm portrait photography', 'skin tone portrait'],
    'landscape_vivid': ['vivid landscape nature', 'saturated outdoor colors'],
    'film_noir': ['classic film noir', '1940s detective movie'],
    'pastel': ['soft pastel colors', 'dreamy pastel tone'],
    'minimalist': ['minimalist clean composition', 'sparse simple photograph'],
}


class StyleAutoTone:
    """风格化自动调色器

    Usage:
        >>> st = StyleAutoTone()  # 单例懒加载
        >>> result = st.auto_tone_with_style("photo.jpg", "忧郁蓝调")
        >>> result = st.auto_tone_with_style("photo.jpg", style_desc=None)  # 自动视觉分析
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        # 进程内单例：SigLIP 塔 + 预编码风格描述只加载一次
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self, model_path: Optional[str] = None):
        if self._initialized:
            return
        self._initialized = True

        # SigLIP base：显式解析 auto_tone_siglip_h192_d03.pt——
        # AutoTonePredictor(model_path=None) 的默认是 v7_clean（CLIP 底座，
        # 77 上下文文本塔），配 SigLIP tokenizer（64）会在 encode_text
        # 崩溃；此前仅因 CN 环境 tokenizer 拉取失败被静默掩盖（视觉分析
        # 禁用 + 底座退化为 v7）。PHOTOS_AUTO_TONE_SIGLIP_MODEL 仍可覆盖。
        from .. import models as _pkg_models
        if model_path is None:
            model_path = (os.environ.get("PHOTOS_AUTO_TONE_SIGLIP_MODEL")
                          or _pkg_models.core_path(
                              "auto_tone_siglip_h192_d03.pt"))
        self.predictor = AutoTonePredictor(model_path=model_path)
        self.predictor.load()
        self.device = self.predictor.device
        self.clip_model = self.predictor.clip_model
        self.preprocess = self.predictor.preprocess

        # SigLIP tokenizer：modelscope 源直连镜像组装的本地目录；
        # 其余先 HF 在线、失败后回落 ModelScope（惰性——auto 模式 HF
        # 可用时零镜像流量，测试环境也不会碰网络之外的源）。
        # 全部失败仅禁用视觉分析（不阻断风格化），但打一条可见告警——
        # 静默降级会让用户以为 top_styles 为空是模型判断。
        self.tokenizer = None
        from .. import models as _models
        _src = os.environ.get("PHOTOS_AUTO_TONE_TOWER_SOURCE",
                              "auto").strip().lower()

        def _try_load(source_id):
            try:
                from transformers import AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained(source_id)
                return True
            except Exception:
                return False

        if _src == "modelscope":
            _local = _models.ensure_siglip_tokenizer_dir()
            if _local:
                _try_load(_local)
        else:
            if not _try_load(SIGLIP_TOKENIZER):
                _local = _models.ensure_siglip_tokenizer_dir()
                if _local:
                    _try_load(_local)
        if self.tokenizer is None:
            import sys
            print("auto-tone: SigLIP tokenizer 不可用（HF 与 ModelScope 均"
                  "失败）——视觉风格分析禁用，风格化仍可用预设偏置",
                  file=sys.stderr)

        # 预编码所有风格描述
        self._prepare_text_features()

        # Qwen 缓存
        self._qwen_model = None
        self._qwen_processor = None

    def _prepare_text_features(self):
        """预编码风格描述（用于视觉风格分析）"""
        if self.tokenizer is None:
            self.text_feats = None
            self.all_descs = []
            return

        import torch

        self.all_descs = []  # [(style_key, text)]
        for style_key, descs in STYLE_VISUAL_DESCS.items():
            for d in descs:
                self.all_descs.append((style_key, d))
        texts = [d[1] for d in self.all_descs]

        tokens = self.tokenizer(
            texts, padding='max_length', max_length=64, return_tensors='pt', truncation=True,
        )
        with torch.no_grad():
            feat = self.clip_model.encode_text(tokens.input_ids.to(self.device)).float()
        self.text_feats = feat / feat.norm(dim=-1, keepdim=True)

    def _encode_image(self, img: Image.Image):
        """SigLIP 编码图像"""
        import torch

        x = self.preprocess(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.clip_model.encode_image(x).float()
        return feat / feat.norm(dim=-1, keepdim=True)

    def analyze_visual_style(self, image_path: str, top_k: int = 3) -> List[Tuple[str, float]]:
        """视觉风格分析：返回 top-K 风格及置信度

        Args:
            image_path: 图像路径
            top_k: 返回前 K 个风格

        Returns:
            [(style_key, confidence), ...]
        """
        if self.text_feats is None:
            raise RuntimeError("SigLIP tokenizer not loaded, visual analysis unavailable")

        img = Image.open(image_path).convert('RGB')
        img_feat = self._encode_image(img)
        sims = (img_feat @ self.text_feats.T).cpu().numpy()[0]

        # Softmax（temp=100 模拟锐化）
        sims_exp = np.exp(sims * 100)
        sims_softmax = sims_exp / sims_exp.sum()

        # 按风格聚合
        style_scores = {}
        for i, (style_key, _) in enumerate(self.all_descs):
            style_scores.setdefault(style_key, []).append(sims_softmax[i])
        style_avg = {k: float(np.mean(v)) for k, v in style_scores.items()}

        sorted_styles = sorted(style_avg.items(), key=lambda x: x[1], reverse=True)
        return sorted_styles[:top_k]

    def _load_qwen(self):
        """懒加载 Qwen3-VL（基座，无 LoRA——训练侧实验 LoRA 反而退化）"""
        if self._qwen_model is None:
            import torch
            from transformers import Qwen3VLForConditionalGeneration, AutoProcessor

            device = models.pick_device()
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            base = models.QWEN_BASE_MODEL
            self._qwen_processor = AutoProcessor.from_pretrained(base)
            self._qwen_model = Qwen3VLForConditionalGeneration.from_pretrained(
                base, dtype=dtype,
            ).to(device).eval()
        return self._qwen_model, self._qwen_processor

    def parse_style_with_qwen(self, style_desc: str) -> Dict[str, float]:
        """Qwen3-VL 解析风格描述 → 9-dim 偏置

        Args:
            style_desc: 风格描述（任意自然语言）

        Returns:
            9 字段偏置 dict
        """
        import torch

        model, processor = self._load_qwen()
        device = next(model.parameters()).device
        prompt = STYLE_PROMPT % style_desc
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], return_tensors='pt', padding=True).to(device)

        with torch.no_grad():
            gen_ids = model.generate(
                **inputs, max_new_tokens=200, do_sample=False,
                pad_token_id=processor.tokenizer.eos_token_id,
            )
        response = processor.batch_decode(
            gen_ids[:, inputs.input_ids.shape[1]:], skip_special_tokens=True
        )[0]
        return parse_json_bias(response, style_desc)

    def _get_bias(self, style_desc: str, use_qwen: bool = True) -> Tuple[Dict[str, float], str]:
        """获取风格偏置（preset 或 qwen）

        Returns:
            (bias_dict, source)
        """
        if use_qwen:
            try:
                bias = self.parse_style_with_qwen(style_desc)
                return bias, 'qwen'
            except Exception:
                # fallback to preset（Qwen 未安装 / 基座缺失 / 生成失败）
                pass

        # 手工预设
        preset_key = CN_TO_KEY.get(style_desc, 'fresh_natural')
        bias = STYLE_BIASES.get(preset_key, get_zero_bias())
        return bias, 'preset'

    def auto_tone_with_style(
        self,
        image_path: str,
        style_desc: Optional[str] = None,
        strength: float = 1.0,
        use_qwen: bool = True,
        render: bool = True,
        output_path: Optional[str] = None,
    ) -> Dict:
        """风格化自动调色

        Args:
            image_path: 图像绝对路径
            style_desc: 风格描述（如"忧郁蓝调"）。None 时自动视觉分析
            strength: 风格强度 0-1
            use_qwen: True=用 Qwen 解析，False=用预设
            render: 是否渲染输出
            output_path: 自定义输出路径（None 时自动生成）

        Returns:
            dict {
                'schema_version': 2,
                'options': final 9 字段 dict,
                'bias': 风格偏置 dict,
                'bias_source': 'qwen' | 'preset',
                'style_desc': 实际使用的风格描述,
                'visual_styles': [{style_key, style_cn, confidence}, ...],
                'rendered_path': 输出图路径或 None,
                'warnings': [str, ...],
                'metadata': {...},
            }
        """
        t0 = time.time()
        img = Image.open(image_path).convert('RGB')

        # 1. SigLIP 基础预测
        base_options = self.predictor.predict(img)

        # 2. 视觉风格分析（始终执行；失败不阻断）
        try:
            visual_styles = self.analyze_visual_style(image_path, top_k=3)
        except Exception:
            visual_styles = []

        # 3. 自动风格（如果用户没指定）
        if style_desc is None:
            if visual_styles:
                top_key, _top_conf = visual_styles[0]
                style_desc = STYLE_CN.get(top_key, top_key)
            else:
                style_desc = '清新自然'  # fallback

        # 4. 获取偏置
        bias, bias_source = self._get_bias(style_desc, use_qwen=use_qwen)

        # 5. 应用偏置
        final_options = apply_style_bias(base_options, bias, strength=strength)

        # 6. 渲染
        warnings = []
        rendered_path = None
        if render:
            try:
                if output_path is None:
                    stem = Path(image_path).stem
                    safe_style = style_desc.replace('/', '_').replace(' ', '_')
                    output_path = str(Path(image_path).parent / f'{stem}_styled_{safe_style}.jpg')
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                render_options(img, final_options, output_path=output_path)
                rendered_path = output_path
            except Exception as e:
                warnings.append(f"render failed: {e}")

        return {
            'schema_version': 2,  # 增加 bias/style_desc/visual_styles 字段
            'options': final_options,
            'bias': bias,
            'bias_source': bias_source,
            'style_desc': style_desc,
            'visual_styles': [
                {'style_key': k, 'style_cn': STYLE_CN.get(k, k), 'confidence': round(c, 4)}
                for k, c in visual_styles
            ],
            'rendered_path': rendered_path,
            'warnings': warnings,
            'metadata': {
                'strength': strength,
                'base_model_version': 'siglip_h192_d03',
                'duration_ms': int((time.time() - t0) * 1000),
            },
        }


# 便捷入口
def auto_tone_with_style(image_path: str, style_desc: Optional[str] = None, **kwargs) -> Dict:
    """便捷入口：单图风格化自动调色

    Args:
        image_path: 图像绝对路径
        style_desc: 风格描述（如"忧郁蓝调"）。None 时自动视觉分析
        **kwargs: 透传给 StyleAutoTone.auto_tone_with_style

    Returns:
        dict（见 StyleAutoTone.auto_tone_with_style）
    """
    return StyleAutoTone().auto_tone_with_style(image_path, style_desc, **kwargs)


def analyze_visual_style(image_path: str, top_k: int = 3) -> List[Tuple[str, float]]:
    """便捷入口：视觉风格分析

    Args:
        image_path: 图像绝对路径
        top_k: 返回前 K 个风格

    Returns:
        [(style_key, confidence), ...]
    """
    return StyleAutoTone().analyze_visual_style(image_path, top_k=top_k)


def list_styles() -> Dict[str, str]:
    """列出所有支持的风格（中英文映射）"""
    return dict(STYLE_CN)
