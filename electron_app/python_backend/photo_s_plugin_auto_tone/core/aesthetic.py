"""美学评分器（Qwen3-VL + LoRA，可选功能）

需要 qwen extra: pip install 'photo-s-plugin-auto-tone[qwen]'
基座模型默认从 HuggingFace 拉 Qwen/Qwen3-VL-2B-Instruct（约 4.3GB），
可用 PHOTOS_AUTO_TONE_QWEN_BASE 指向本地快照目录。
"""

import re
from typing import Optional

from PIL import Image

from .. import models


class AestheticScorer:
    """Qwen 美学评分 1-10

    输入：图像路径或 PIL.Image
    输出：dict {score, bucket, confidence, raw, loaded}
    """

    def __init__(self, lora_path: Optional[str] = None,
                 base_path: Optional[str] = None):
        self.lora_path = lora_path    # None → modelstore 解析
        self.base_path = base_path or models.QWEN_BASE_MODEL
        self.model = None
        self.processor = None
        self.loaded = False
        self.error = None

    def load(self):
        if self.loaded:
            return
        try:
            import torch
            from transformers import Qwen3VLForConditionalGeneration, AutoProcessor
            from peft import PeftModel
        except ImportError as e:
            self.error = (
                f"{e} — pip install 'photo-s-plugin-auto-tone[qwen]' "
                "并配置 PHOTOS_AUTO_TONE_QWEN_BASE"
            )
            self.loaded = False
            return

        try:
            device = models.pick_device()
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            kwargs = {"dtype": dtype}
            if device == "cuda":
                kwargs["device_map"] = "cuda"

            lora = self.lora_path or models.ensure_lora_dir("aesthetic")
            self.lora_path = lora

            self.model = Qwen3VLForConditionalGeneration.from_pretrained(
                self.base_path, **kwargs)
            self.processor = AutoProcessor.from_pretrained(self.base_path)
            self.model = PeftModel.from_pretrained(self.model, lora, is_trainable=False)
            if device != "cuda":
                self.model = self.model.to(device)
            self.model.eval()
            self.loaded = True
        except Exception as e:
            self.error = str(e)
            self.loaded = False

    def score(self, image) -> dict:
        """评分 1-10"""
        self.load()

        if not self.loaded:
            return {
                "score": None,
                "bucket": "unknown",
                "confidence": 0.0,
                "raw": f"load failed: {self.error}",
                "loaded": False,
            }

        if isinstance(image, str):
            img = Image.open(image).convert("RGB")
        else:
            img = image.convert("RGB")

        prompt = (
            "<image>\n"
            "请评估这张照片的美学质量，给出 1-10 分的评分。"
            "1=极差，10=惊艳。直接输出分数，格式：分数=X.XX"
        )

        try:
            import torch

            device = models.pick_device()
            messages = [
                {"role": "user", "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": prompt},
                ]}
            ]
            inputs = self.processor.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                return_dict=True, return_tensors='pt'
            ).to(device)

            with torch.no_grad():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=30,
                    do_sample=False,
                    pad_token_id=self.processor.tokenizer.pad_token_id or 0,
                )
            gen = output[0, inputs['input_ids'].shape[1]:]
            reply = self.processor.tokenizer.decode(gen, skip_special_tokens=True)

            score = self._parse_score(reply)
            bucket = self._bucketize(score)

            return {
                "score": score,
                "bucket": bucket,
                "confidence": 0.85 if score else 0.0,
                "raw": reply,
                "loaded": True,
            }
        except Exception as e:
            return {
                "score": None,
                "bucket": "unknown",
                "confidence": 0.0,
                "raw": f"error: {e}",
                "loaded": False,
            }

    @staticmethod
    def _parse_score(text: str):
        m = re.search(r'(\d+\.?\d*)', text)
        if m:
            try:
                val = float(m.group(1))
                return max(1.0, min(10.0, val))
            except ValueError:
                pass
        return None

    @staticmethod
    def _bucketize(score: float) -> str:
        if score is None:
            return "unknown"
        if score < 4.0:
            return "low"
        elif score < 5.5:
            return "medium-low"
        elif score < 6.5:
            return "medium"
        elif score < 7.5:
            return "medium-high"
        else:
            return "high"
