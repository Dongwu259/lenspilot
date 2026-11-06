"""异常检测：综合 hand features + CLIP 相似度"""

import os
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from .. import models

# hand features 84-d 布局中的关键下标（与 photo_s.lrxmp._content_features 对齐）
_IDX_LUMA_HIST = slice(0, 32)
_IDX_LUMA_MEAN = 80
_IDX_LUMA_STD = 81
_IDX_COLOR_RANGE = 82


class AnomalyDetector:
    """图像异常检测器

    输入：PIL Image 或 path
    输出：anomaly_score (0-1，越高越异常)
    """

    def __init__(
        self,
        clip_train_npz: Optional[str] = None,
        hand_features_npz: Optional[str] = None,
        device: Optional[str] = None,
    ):
        self.device = device or models.pick_device()
        self.clip_model = None
        self.preprocess = None
        self.train_clip = None
        self.train_paths = None
        self.train_hand = None

        self.clip_train_npz = clip_train_npz
        self.hand_features_npz = hand_features_npz

    def load(self):
        if self.clip_model is not None:
            return

        self.clip_model, self.preprocess = models.get_shared_clip(
            'ViT-L-14', 'openai', self.device)

        if self.clip_train_npz is None:
            self.clip_train_npz = models.core_path("clip_train_rag.npz")
        if os.path.exists(self.clip_train_npz):
            d = np.load(self.clip_train_npz, allow_pickle=False)
            self.train_paths = d['paths']
            self.train_clip = d['feats']

        if self.hand_features_npz is None:
            self.hand_features_npz = models.core_path("hand_features.npz")
        if os.path.exists(self.hand_features_npz):
            d = np.load(self.hand_features_npz, allow_pickle=False)
            self.train_hand = d['feats']

    def _clip_encode(self, img: Image.Image) -> np.ndarray:
        """CLIP 编码 → 768-d L2 归一化"""
        import torch

        x = self.preprocess(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.clip_model.encode_image(x).float()
        feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.cpu().numpy()[0]

    def _hand_score(self, img: Image.Image) -> float:
        """hand features 异常分"""
        from photo_s.lrxmp import _content_features

        feats = np.array(_content_features(img), dtype=np.float32)
        luma_hist = feats[_IDX_LUMA_HIST]
        luma_mean = feats[_IDX_LUMA_MEAN]
        luma_std = feats[_IDX_LUMA_STD]
        color_range = feats[_IDX_COLOR_RANGE]

        score = 0.0
        # 极端曝光
        if luma_mean < 0.20:
            score += 2.0
        if luma_mean > 0.85:
            score += 2.0
        # 低对比度
        if luma_std < 0.05:
            score += 1.5
        if luma_std > 0.30:
            score += 1.5
        # 低颜色丰富度
        if color_range < 0.10:
            score += 1.0
        # 暗部堆积
        if luma_hist[:5].sum() > 0.6:
            score += 1.5
        # 亮部堆积
        if luma_hist[27:32].sum() > 0.5:
            score += 1.5
        return score

    def _clip_outlier_score(self, img: Image.Image) -> float:
        """CLIP 离群分：与训练集的最高相似度越低越异常"""
        if self.train_clip is None:
            return 0.5
        feat = self._clip_encode(img)
        sim = self.train_clip @ feat
        max_sim = sim.max()
        return float(max(0.0, (0.7 - max_sim) / 0.7))

    def score(self, image) -> Tuple[float, dict]:
        """综合异常评分

        Returns:
            (score, details)
            score: 0-1，越高越异常
            details: {hand_score, clip_outlier, anomaly_breakdown}
        """
        self.load()

        if isinstance(image, str):
            img = Image.open(image).convert("RGB")
        elif isinstance(image, Image.Image):
            img = image.convert("RGB")
        else:
            raise TypeError(f"image must be path or PIL.Image, got {type(image)}")

        hand_raw = self._hand_score(img)
        clip_outlier = self._clip_outlier_score(img)

        # hand 异常（raw 0-5+）归一化到 0-1，加权合成
        hand_norm = min(1.0, hand_raw / 4.5)
        combined = 0.6 * hand_norm + 0.4 * clip_outlier

        return float(combined), {
            "hand_raw": float(hand_raw),
            "hand_norm": float(hand_norm),
            "clip_outlier": float(clip_outlier),
            "combined": float(combined),
        }


_default_detector: Optional[AnomalyDetector] = None


def detect_anomaly(image) -> float:
    """便捷函数：仅返回异常分数 0-1"""
    global _default_detector
    if _default_detector is None:
        _default_detector = AnomalyDetector()
    score, _ = _default_detector.score(image)
    return score
