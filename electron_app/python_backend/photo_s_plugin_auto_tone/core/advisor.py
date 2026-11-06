"""修图建议器（Qwen3-VL + Advisor LoRA，可选功能）

需要 qwen extra（同 aesthetic）。低置信度图片调用 advisor 修正预测。
"""

import json
import re
from typing import Optional

from PIL import Image

from .. import models


class ToneAdvisor:
    """修图建议器：基于当前预测输出修正建议"""

    ADVISOR_PROMPT = (
        "<image>\n"
        "v7_clean 预测: {pred_text}{exif_text}\n"
        "请分析预测误差，给出 7 字段（exposure/contrast/saturation/vibrance/clarity/texture/dehaze）"
        "的修正建议，格式：修正建议: {{...}}\\n原因: ..."
    )

    SKIP_FIELDS = {"wb_temp", "wb_tint"}

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

            lora = self.lora_path or models.ensure_lora_dir("advisor")
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

    def advise(
        self,
        image,
        current_options: dict,
        exif_text: str = "",
    ) -> dict:
        """给出修正建议

        Returns:
            {current_options, suggested_delta, corrected_options, reason, raw}
        """
        if isinstance(image, str):
            img = Image.open(image).convert("RGB")
        else:
            img = image.convert("RGB")

        if not self.loaded:
            self.load()

        if not self.loaded:
            return {
                "current_options": current_options,
                "suggested_delta": {},
                "corrected_options": current_options,
                "reason": f"advisor not loaded: {self.error or ''}".strip(),
                "raw": "",
            }

        prompt = self.ADVISOR_PROMPT.format(
            pred_text=json.dumps(current_options, ensure_ascii=False),
            exif_text=exif_text,
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
                    max_new_tokens=200,
                    do_sample=False,
                    pad_token_id=self.processor.tokenizer.pad_token_id or 0,
                )
            gen = output[0, inputs['input_ids'].shape[1]:]
            reply = self.processor.tokenizer.decode(gen, skip_special_tokens=True)

            delta, reason = self._parse_reply(reply)

            return {
                "current_options": current_options,
                "suggested_delta": delta,
                "corrected_options": current_options,  # 调用方负责 apply delta
                "reason": reason,
                "raw": reply,
            }
        except Exception as e:
            return {
                "current_options": current_options,
                "suggested_delta": {},
                "corrected_options": current_options,
                "reason": f"error: {e}",
                "raw": "",
            }

    @staticmethod
    def _parse_reply(reply: str) -> tuple:
        """解析 advisor 输出 → (delta_dict, reason_str)"""
        m = re.search(r'修正建议[:：]\s*(\{[^}]*\})', reply)
        if not m:
            return {}, reply.strip()

        try:
            delta = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}, reply.strip()

        m2 = re.search(r'原因[:：]\s*(.+)', reply)
        reason = m2.group(1).strip() if m2 else ""

        return delta, reason
