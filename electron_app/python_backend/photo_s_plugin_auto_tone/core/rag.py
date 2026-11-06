"""RAG 检索增强：基于训练集最近邻加权融合

训练集 embedding 与对应 target 值都打包在 clip_train_rag.npz 中
（feats / paths / targets / field_names），无需外部 jsonl。
"""

from typing import Optional, Tuple

import numpy as np
from PIL import Image

from .. import models

RAG_FIELDS = ['exposure', 'contrast', 'saturation', 'vibrance',
              'wb_temp', 'wb_tint', 'clarity', 'texture', 'dehaze']


class RAGEnhancer:
    """RAG 检索增强：基于训练集最近邻加权融合"""

    def __init__(
        self,
        clip_npz: Optional[str] = None,
        device: Optional[str] = None,
        top_k: int = 5,
        temperature: float = 10.0,
        alpha: float = 0.85,
    ):
        self.device = device or models.pick_device()
        self.top_k = top_k
        self.temperature = temperature
        self.alpha = alpha  # 模型预测权重
        self.clip_npz = clip_npz  # None → 从 modelstore 解析

        self.clip_model = None
        self.preprocess = None
        self.train_clip = None
        self.train_paths = None
        self.train_targets = None

    def load(self):
        if self.clip_model is not None:
            return

        npz_path = self.clip_npz or models.core_path("clip_train_rag.npz")
        self.clip_npz = npz_path

        self.clip_model, self.preprocess = models.get_shared_clip(
            'ViT-L-14', 'openai', self.device)

        d = np.load(npz_path, allow_pickle=False)
        self.train_clip = d['feats']      # (N, 768) 已归一化
        self.train_paths = d['paths']
        # targets 与 feats 同序打包在 npz 里（旧版需外部 jsonl，已废弃）
        self.train_targets = d['targets'].astype(np.float32)  # (N, 9) 实际值

    def _clip_encode(self, img: Image.Image) -> np.ndarray:
        import torch

        x = self.preprocess(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.clip_model.encode_image(x).float()
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.cpu().numpy()[0]

    def retrieve(self, query_feat: np.ndarray, top_k: int = None):
        """检索最近邻

        Returns:
            (idx, weights, similarities)
        """
        if self.train_clip is None or self.train_targets is None:
            return np.array([]), np.array([]), np.array([])

        k = min(top_k or self.top_k, len(self.train_clip))
        sims = self.train_clip @ query_feat  # (N,)
        top_idx = np.argsort(-sims)[:k]
        top_sims = sims[top_idx]

        # softmax 加权（temperature）
        weights = np.exp(self.temperature * top_sims)
        weights = weights / weights.sum()

        return top_idx, weights, top_sims

    def enhance(
        self,
        image: Image.Image,
        pred: np.ndarray,
        pred_ranges: dict,
        pred_targets: list,
    ) -> Tuple[np.ndarray, dict]:
        """RAG 增强

        Args:
            image: PIL.Image
            pred: 模型预测 (9,) 归一化空间 [-1, 1]
            pred_ranges: {field: (lo, hi)}
            pred_targets: 字段顺序

        Returns:
            (fused_pred, info)
        """
        self.load()

        if self.train_clip is None:
            return pred, {"rag_used": False}

        query = self._clip_encode(image)
        idx, weights, sims = self.retrieve(query)
        if len(idx) == 0:
            return pred, {"rag_used": False}

        # RAG 目标加权平均（实际值空间）
        rag_target_actual = (self.train_targets[idx] * weights[:, None]).sum(0)

        # 融合（在归一化空间 [-1, 1]）
        fused = pred.copy()
        for j, f in enumerate(pred_targets):
            if f in ('wb_temp', 'wb_tint'):
                # 白平衡字段训练噪声大，不参与 RAG 融合
                continue
            lo, hi = pred_ranges[f]
            pred_norm = (pred[j] + 1) / 2
            rag_norm = np.clip((rag_target_actual[j] - lo) / (hi - lo), 0.0, 1.0)
            fused_norm = self.alpha * pred_norm + (1 - self.alpha) * rag_norm
            fused[j] = fused_norm * 2 - 1

        return fused, {
            "rag_used": True,
            "top_sims": sims.tolist(),
            "max_sim": float(sims.max()),
            "alpha": self.alpha,
        }
