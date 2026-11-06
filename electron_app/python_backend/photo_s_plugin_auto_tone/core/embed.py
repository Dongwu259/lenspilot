"""SigLIP 嵌入（v2.5 语义搜索 embed provider 槽位）。

图像/文本双编码，供核心 ``photo_s.search`` 的 index/find/auto-tag 使用。
塔与 tokenizer 完全复用既有基建：``models.get_shared_clip``（HF 缓存 /
modelstore 下载 + ModelScope 国内回落，与风格化/verifier 同一份权重，
零新增下载）+ StyleAutoTone 同源的 tokenizer 解析链。

进程内单例——塔 2.6GB 只加载一次。
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional, Sequence

import numpy as np

EMBED_NAME = "siglip:ViT-L-16-SigLIP-384"
EMBED_DIM = 1024
_SIGLIP_HUB = os.environ.get("PHOTOS_AUTO_TONE_SIGLIP_TOKENIZER",
                             "timm/ViT-L-16-SigLIP-384")


class SigLIPEmbedder:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._loaded = False
                    cls._instance = inst
        return cls._instance

    # -- 资源加载 ----------------------------------------------------------

    def _ensure(self):
        if self._loaded:
            return
        try:
            import torch  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "SigLIP embedder needs torch (pip install torch)") from e
        from .. import models

        self.device = models.pick_device()
        self.model, self.preprocess = models.get_shared_clip(
            "ViT-L-16-SigLIP-384", "webli", self.device)
        self.tokenizer = self._load_tokenizer()
        self._loaded = True

    @staticmethod
    def _load_tokenizer():
        """与 StyleAutoTone 同链：modelscope 源直连镜像目录；auto 先 HF
        在线再回落 ModelScope；全失败返回 None（文本编码不可用，图像编码
        不受影响）。"""
        from .. import models
        src = os.environ.get("PHOTOS_AUTO_TONE_TOWER_SOURCE",
                             "auto").strip().lower()

        def _try(source_id):
            try:
                from transformers import AutoTokenizer
                return AutoTokenizer.from_pretrained(source_id)
            except Exception:
                return None

        tok = None
        if src == "modelscope":
            local = models.ensure_siglip_tokenizer_dir()
            if local:
                tok = _try(local)
        else:
            tok = _try(_SIGLIP_HUB)
            if tok is None:
                local = models.ensure_siglip_tokenizer_dir()
                if local:
                    tok = _try(local)
        return tok

    # -- 编码 --------------------------------------------------------------
    # torch import 在 _ensure 之后（缺 torch 的环境里 import 本模块不炸）

    def embed_images(self, paths: Sequence[str], batch_size: int = 8
                     ) -> np.ndarray:
        """图像路径列表 → (N, dim) L2 归一 float32（SigLIP = 1024）。"""
        self._ensure()
        import torch
        from PIL import Image

        chunks = []
        for start in range(0, len(paths), batch_size):
            chunk = paths[start:start + batch_size]
            # open_clip preprocess 返回单图 (C,H,W)——stack 出批维
            batch = torch.stack(
                [self.preprocess(Image.open(p).convert("RGB"))
                 for p in chunk]).to(self.device)
            with torch.no_grad():
                feats = self.model.encode_image(batch).float()
            feats = feats / feats.norm(dim=-1, keepdim=True)
            chunks.append(feats.cpu().numpy())
        if not chunks:
            raise RuntimeError("embed_images called with no paths")
        return np.concatenate(chunks, axis=0)

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """文本列表 → (M, 1024) L2 归一 float32。tokenizer 不可用时
        RuntimeError（fail-loud，search 层不静默降级）。"""
        self._ensure()
        if self.tokenizer is None:
            raise RuntimeError(
                "SigLIP tokenizer unavailable (HF and ModelScope both "
                "failed) — text embedding disabled; check "
                "PHOTOS_AUTO_TONE_TOWER_SOURCE / network")
        import torch

        tokens = self.tokenizer(
            list(texts), padding="max_length", max_length=64,
            return_tensors="pt", truncation=True)
        with torch.no_grad():
            feats = self.model.encode_text(
                tokens.input_ids.to(self.device)).float()
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy()


def get_embedder() -> SigLIPEmbedder:
    return SigLIPEmbedder()
