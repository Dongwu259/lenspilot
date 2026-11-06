"""美学验证器（v2.4 —— 全自动闭环的 stop 条件 / reward）

两级实现，按代价递增：

1. :class:`AestheticVerifier` —— SigLIP 图像嵌入 + MLP 回归头 → 1-10 分。
   单次前向（塔加载后毫秒级），可批量、可进 agent 候选排序循环。
   头权重 ``aesthetic_head.pt`` 由 tools/train_verifier.py 训练
   （数据源建议：本人 LR 星级评分 = 个人美学标注）；
   未训练/未放置时明确报不可用，不静默打分。
2. 既有 Qwen3-VL LoRA :class:`~.aesthetic.AestheticScorer` —— 更准但重
   （4.3GB 基座），适合终审。

checkpoint 格式（torch.save，weights_only 兼容——纯 tensor/内建类型）::

    {
      "schema": 1,
      "type": "aesthetic_head",
      "model_name": "ViT-L-16-SigLIP-384",   # 嵌入塔（open_clip 名）
      "pretrained": "webli",
      "sig_dim": 1024,
      "state_dict": {...},                    # MLP（输入 = L2 归一化嵌入）
      "norm": {"mean": float, "std": float},  # 训练分数归一化（可选）
    }

分数映射：head 输出经 norm 反归一化（若有）后 clamp 到 [1, 10]。
"""

import os
import threading
from typing import Optional

from PIL import Image

from .. import models

_VERIFIER_LOCK = threading.Lock()
_VERIFIER_SINGLETON: Optional["AestheticVerifier"] = None

SCORE_MIN, SCORE_MAX = 1.0, 10.0


def _need_torch():
    try:
        import torch  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "verifier 需要 torch: pip install 'photo-s-plugin-auto-tone[model]'"
        ) from e
    import torch
    return torch


def head_path() -> Optional[str]:
    """aesthetic_head.pt 的本地路径（无则 None）。

    查找顺序：PHOTOS_AUTO_TONE_AESTHETIC_HEAD 环境变量 → modelstore
    缓存（towers 同级）。不做网络下载——头权重尚未发布到 release，
    下载一个 404 只会浪费时间和报模糊错误；训练后随 release 分发。
    """
    env = os.environ.get("PHOTOS_AUTO_TONE_AESTHETIC_HEAD")
    if env and os.path.isfile(env):
        return env
    from photo_s.modelstore import cache_dir

    p = os.path.join(cache_dir(), "aesthetic_head.pt")
    return p if os.path.isfile(p) else None


class AestheticVerifier:
    """SigLIP 嵌入 + 回归头 → 1-10 美学分（快速级）"""

    def __init__(self, path: Optional[str] = None, device: Optional[str] = None):
        self.path = path  # None → head_path() 解析；解析不到 = 未训练
        self.device = device or models.pick_device()
        self.model_name = "ViT-L-16-SigLIP-384"
        self.pretrained = "webli"
        self.norm_mean = 0.0
        self.norm_std = 1.0
        self.head = None
        self.error = None

    @property
    def available(self) -> bool:
        return self.path is not None or head_path() is not None

    def load(self):
        if self.head is not None:
            return
        path = self.path or head_path()
        if path is None:
            raise RuntimeError(
                "aesthetic head 未训练/未放置：用 tools/train_verifier.py "
                "训练，或设 PHOTOS_AUTO_TONE_AESTHETIC_HEAD 指向 "
                "aesthetic_head.pt；Qwen 终审可用 verify_aesthetic(prefer='qwen')")
        self.path = path

        torch = _need_torch()
        ck = torch.load(path, weights_only=True, map_location=self.device)
        if ck.get("type") != "aesthetic_head":
            raise RuntimeError(f"not an aesthetic head checkpoint: {path}")
        self.model_name = ck.get("model_name", self.model_name)
        self.pretrained = ck.get("pretrained", self.pretrained)
        norm = ck.get("norm") or {}
        self.norm_mean = float(norm.get("mean", 0.0))
        self.norm_std = float(norm.get("std", 1.0)) or 1.0

        sd = ck["state_dict"]
        linear_keys = sorted(
            (k for k in sd if k.endswith(".weight") and sd[k].ndim == 2),
            key=lambda k: int(k.split(".")[1]) if k.split(".")[1].isdigit()
            else 0)
        if not linear_keys:
            raise RuntimeError(f"no linear layers in head checkpoint: {path}")
        in_dim = sd[linear_keys[0]].shape[1]
        self.head = AutoToneMLPProxy.build(sd, in_dim).to(self.device)
        self.head.eval()

    def score(self, image) -> dict:
        """图像（路径或 PIL.Image）→ {score, bucket, source, loaded}"""
        try:
            self.load()
        except RuntimeError as e:
            return {"score": None, "bucket": "unknown", "source": "siglip-head",
                    "confidence": 0.0, "raw": str(e), "loaded": False}

        torch = _need_torch()
        img = (Image.open(image).convert("RGB") if isinstance(image, str)
               else image.convert("RGB"))
        _, preprocess = models.get_shared_clip(
            self.model_name, self.pretrained, self.device)
        x = preprocess(img).unsqueeze(0).to(self.device)
        emb = self.head_tower_encode(x)
        with torch.no_grad():
            out = float(self.head(emb).cpu().numpy().reshape(-1)[0])
        score = out * self.norm_std + self.norm_mean
        score = max(SCORE_MIN, min(SCORE_MAX, score))
        return {"score": round(score, 3),
                "bucket": bucketize(score),
                "source": "siglip-head",
                "confidence": 0.7,
                "loaded": True}

    def head_tower_encode(self, x):
        """塔嵌入 + L2 归一化（与训练侧特征一致）。"""
        torch = _need_torch()
        model, _ = models.get_shared_clip(
            self.model_name, self.pretrained, self.device)
        with torch.no_grad():
            emb = model.encode_image(x).float()
            emb = emb / (emb.norm(dim=-1, keepdim=True) + 1e-8)
        return emb


def bucketize(score) -> str:
    """与 aesthetic.AestheticScorer._bucketize 同一分桶（单一事实源）。"""
    if score is None:
        return "unknown"
    s = float(score)
    if s < 4.0:
        return "low"
    if s < 5.5:
        return "medium-low"
    if s < 6.5:
        return "medium"
    if s < 7.5:
        return "medium-high"
    return "high"


class AutoToneMLPProxy:
    """按 state_dict 重建 MLP——与 predictor._build_mlp 相同的重建规则
    （开头可选 LayerNorm、相邻 Linear 间一个 GELU）。抽到这里避免
    verifier 依赖 predictor 的私有静态方法。"""

    @staticmethod
    def build(state_dict, in_dim):
        torch = _need_torch()
        all_keys = [k for k in state_dict if k.endswith(".weight")]
        linear_keys = sorted(
            (k for k in all_keys if state_dict[k].ndim == 2),
            key=lambda k: int(k.split(".")[1]) if k.split(".")[1].isdigit()
            else 0)
        layernorm_keys = sorted(
            (k for k in all_keys if state_dict[k].ndim == 1),
            key=lambda k: int(k.split(".")[1]) if k.split(".")[1].isdigit()
            else 0)

        layers = []
        if layernorm_keys:
            ln_key = layernorm_keys[0]
            ln = torch.nn.LayerNorm(state_dict[ln_key].shape[0])
            with torch.no_grad():
                ln.weight.copy_(state_dict[ln_key])
                bias_key = ln_key.replace(".weight", ".bias")
                if bias_key in state_dict:
                    ln.bias.copy_(state_dict[bias_key])
            layers.append(ln)
        for k in linear_keys:
            w = state_dict[k]
            linear = torch.nn.Linear(w.shape[1], w.shape[0])
            with torch.no_grad():
                linear.weight.copy_(w)
                bias_key = k.replace(".weight", ".bias")
                if bias_key in state_dict:
                    linear.bias.copy_(state_dict[bias_key])
            layers.append(linear)
            if k != linear_keys[-1]:
                layers.append(torch.nn.GELU())
        net = torch.nn.Sequential(*layers)
        # state_dict 键顺序 ↔ 重建索引重映射（参数量一致时按序 zip）
        model_keys = list(net.state_dict().keys())
        param_keys = [k for k in state_dict
                      if not k.endswith(("running_mean", "running_var"))]
        if len(param_keys) == len(model_keys):
            net.load_state_dict(
                {dst: state_dict[src] for src, dst in zip(param_keys,
                                                          model_keys)},
                strict=True)
        else:
            net.load_state_dict(
                {(k[len("net."):] if k.startswith("net.") else k): v
                 for k, v in state_dict.items()}, strict=False)
        return net


def get_verifier() -> AestheticVerifier:
    """全局单例（线程安全；available=False 时也缓存，避免反复 stat）。"""
    global _VERIFIER_SINGLETON
    if _VERIFIER_SINGLETON is None:
        with _VERIFIER_LOCK:
            if _VERIFIER_SINGLETON is None:
                _VERIFIER_SINGLETON = AestheticVerifier()
    return _VERIFIER_SINGLETON


def verify_aesthetic(image, prefer: str = "auto") -> dict:
    """组合入口：agent / audit 闸门统一走这里。

    prefer: ``auto``（head 可用→head，否则 Qwen）| ``head`` | ``qwen``。
    两者都不可用时返回 ``{"score": None, ...}`` 且带安装/训练指引——
    闸门语义上"没有 verifier"必须显式失败，不能静默给分。
    """
    prefer = (prefer or "auto").strip().lower()
    verifier = get_verifier()

    if prefer in ("auto", "head") and verifier.available:
        r = verifier.score(image)
        if r.get("loaded"):
            return r
        if prefer == "head":
            return r  # 显式要求 head：失败即失败

    if prefer in ("auto", "qwen"):
        try:
            from .aesthetic import AestheticScorer
            r = AestheticScorer().score(image)
            if r.get("loaded"):
                r["source"] = "qwen-vlm"
                return r
            if prefer == "qwen":
                return {"score": None, "bucket": "unknown",
                        "source": "qwen-vlm", "confidence": 0.0,
                        "raw": r.get("raw"), "loaded": False}
        except ImportError as e:
            if prefer == "qwen":
                return {"score": None, "bucket": "unknown",
                        "source": "qwen-vlm", "confidence": 0.0,
                        "raw": str(e), "loaded": False}

    return {"score": None, "bucket": "unknown", "source": "none",
            "confidence": 0.0,
            "raw": ("no verifier available: train the SigLIP head "
                    "(tools/train_verifier.py) or install the qwen extra "
                    "(pip install 'photo-s-plugin-auto-tone[qwen]')"),
            "loaded": False}
