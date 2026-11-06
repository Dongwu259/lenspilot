"""AutoTonePredictor: v7_clean 推理核心（CLIP+MLP → 9 字段调色参数）"""

import threading
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from .. import models

_predictor_singleton: Optional["AutoTonePredictor"] = None
_lock = threading.Lock()

DEFAULT_TARGETS = [
    "exposure", "contrast", "saturation", "vibrance",
    "wb_temp", "wb_tint", "clarity", "texture", "dehaze",
]

DEFAULT_RANGES = {
    "exposure": (-2.0, 2.0),
    "contrast": (0.5, 1.5),
    "saturation": (0.0, 2.0),
    "vibrance": (-1.0, 1.0),
    "wb_temp": (2000.0, 10000.0),
    "wb_tint": (-100.0, 100.0),
    "clarity": (-1.0, 1.0),
    "texture": (-1.0, 1.0),
    "dehaze": (-1.0, 1.0),
}

# ── 局部调整头（v2.4 词汇表扩展，checkpoint 可选携带）──────────────────
# region 词汇表 = photo_s v1.8 AI 蒙版；params 词汇表 = mask.ADJUST_KEYS
# 的标量子集（中性值全为 0——mask_adjust 语义是围绕 0 的偏移）。
DEFAULT_LOCAL_REGIONS = ["subject", "person"]
DEFAULT_LOCAL_PARAMS = ["exposure", "contrast", "saturation", "vibrance",
                        "clarity"]
DEFAULT_LOCAL_RANGES = {
    "exposure": (-1.0, 1.0),
    "contrast": (-0.5, 0.5),
    "saturation": (-0.5, 0.5),
    "vibrance": (-1.0, 1.0),
    "clarity": (-1.0, 1.0),
}
# 归一化预测幅度低于该值视为"模型认为此处不用调"，不进输出
LOCAL_NEUTRAL_EPS = 0.05


def _need_torch():
    try:
        import torch  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "auto-tone 插件需要 torch: pip install 'photo-s-plugin-auto-tone[model]'"
        ) from e
    import torch
    return torch


class AutoTonePredictor:
    """CLIP+MLP 自动调色推理器

    输入：PIL Image
    输出：9 字段调整参数 dict（实际值范围）
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        device: Optional[str] = None,
    ):
        self.model_path = model_path  # None → 从 modelstore 解析
        self.device = device or models.pick_device()

        self.clip_model = None
        self.mlp = None
        self.preprocess = None
        self.checkpoint = None
        self.qwen_dim = 0
        self.hand_dim = None  # load() 时按 checkpoint 维度推断
        self.targets = DEFAULT_TARGETS
        self.ranges = DEFAULT_RANGES
        # 局部头（checkpoint 带 local_state_dict 时才有）
        self.local_mlp = None
        self.local_regions: List[str] = []
        self.local_params: List[str] = []
        self.local_ranges = DEFAULT_LOCAL_RANGES

    def load(self):
        """加载模型（懒加载；torch / open_clip 缺失时报清晰错误）"""
        if self.clip_model is not None:
            return

        try:
            import open_clip  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "auto-tone 插件需要 open_clip: "
                "pip install 'photo-s-plugin-auto-tone[model]'"
            ) from e

        torch = _need_torch()
        model_path = self.model_path or models.core_path("auto_tone_v7_clean.pt")
        self.model_path = model_path

        # weights_only=True: the checkpoint is a plain dict of tensors —
        # unpickling arbitrary objects from a downloaded file is an
        # arbitrary-code-execution vector if the weight source is ever
        # compromised.
        ck = torch.load(model_path, weights_only=True,
                        map_location=self.device)

        # SigLIP 风格 checkpoint 只存 sig_dim（无 model_name/pretrained）：
        # 按嵌入维度推断 open_clip 模型名
        model_name = ck.get('model_name')
        pretrained = ck.get('pretrained')
        sig_dim = ck.get('sig_dim', 0)
        if model_name is None and sig_dim:
            if sig_dim == 1024:
                model_name = 'ViT-L-16-SigLIP-384'
            elif sig_dim == 768:
                model_name = 'ViT-L-16-SigLIP-256'
            else:
                model_name = f'ViT-L-16-SigLIP-{sig_dim}'
            pretrained = 'webli'

        self.clip_model, self.preprocess = models.get_shared_clip(
            model_name, pretrained, self.device)

        # 维度推断（SigLIP checkpoint 用 sig_dim，CLIP 风格用 feat_dim）
        sd_keys = list(ck['state_dict'].keys())
        linear_keys = [k for k in sd_keys if k.endswith('.weight')
                       and ck['state_dict'][k].ndim == 2]
        actual_in = ck['state_dict'][linear_keys[0]].shape[1]

        feat_dim = ck.get('feat_dim', ck.get('sig_dim', 0))
        self.qwen_dim = ck.get('qwen_dim', 0)
        hand_dim = max(0, actual_in - feat_dim - self.qwen_dim)
        in_dim = feat_dim + hand_dim + self.qwen_dim
        self.hand_dim = hand_dim

        self.mlp = self._build_mlp(ck['state_dict'], in_dim).to(self.device)
        sd = self._remap_state_dict_keys(self.mlp, ck['state_dict'])
        self.mlp.load_state_dict(sd, strict=True)
        self.mlp.eval()

        if 'targets' in ck:
            self.targets = ck['targets']
        if 'ranges' in ck:
            self.ranges = ck['ranges']

        # 局部调整头（可选）：输入与全局 MLP 相同的特征向量，
        # 输出 regions×params 个归一化值（[-1,1] → local_ranges 反归一化）
        local_sd = ck.get('local_state_dict')
        if local_sd:
            self.local_regions = list(ck.get('local_regions')
                                      or DEFAULT_LOCAL_REGIONS)
            self.local_params = list(ck.get('local_params')
                                     or DEFAULT_LOCAL_PARAMS)
            self.local_ranges = dict(ck.get('local_ranges')
                                     or DEFAULT_LOCAL_RANGES)
            self.local_mlp = self._build_mlp(
                local_sd, in_dim).to(self.device)
            sd_local = self._remap_state_dict_keys(self.local_mlp, local_sd)
            self.local_mlp.load_state_dict(sd_local, strict=True)
            self.local_mlp.eval()

        self.checkpoint = ck

    @staticmethod
    def _build_mlp(state_dict, in_dim):
        """按 checkpoint 重建 MLP（与训练时一致）

        训练架构是 LayerNorm → Linear → GELU → Dropout → Linear
        （eval 时 Dropout 为恒等），因此重建规则是：开头可选 LayerNorm、
        相邻 Linear 之间恰好一个 GELU。checkpoint 的 state_dict 不区分
        无参数模块（GELU/Dropout 都不留键），不能按 net.<i> 索引直接填
        GELU——那会把 Dropout 槽位也变成 GELU（推理多过一次激活）。
        """
        torch = _need_torch()

        all_keys = [k for k in state_dict if k.endswith('.weight')]
        linear_keys = sorted(
            [k for k in all_keys if state_dict[k].ndim == 2],
            key=lambda k: int(k.split('.')[1]) if k.split('.')[1].isdigit() else 0,
        )
        layernorm_keys = sorted(
            [k for k in all_keys if state_dict[k].ndim == 1],
            key=lambda k: int(k.split('.')[1]) if k.split('.')[1].isdigit() else 0,
        )

        layers = []
        # 如果有 LayerNorm，加在最前面
        if layernorm_keys:
            ln_key = layernorm_keys[0]
            ln = torch.nn.LayerNorm(state_dict[ln_key].shape[0])
            with torch.no_grad():
                ln.weight.copy_(state_dict[ln_key])
                if f"{ln_key.replace('.weight', '.bias')}" in state_dict:
                    ln.bias.copy_(state_dict[f"{ln_key.replace('.weight', '.bias')}"])
            layers.append(ln)

        for k in linear_keys:
            w = state_dict[k]
            linear = torch.nn.Linear(w.shape[1], w.shape[0])
            with torch.no_grad():
                linear.weight.copy_(w)
                if f"{k.replace('.weight', '.bias')}" in state_dict:
                    linear.bias.copy_(state_dict[f"{k.replace('.weight', '.bias')}"])
            layers.append(linear)
            if k != linear_keys[-1]:
                layers.append(torch.nn.GELU())

        return torch.nn.Sequential(*layers)

    @staticmethod
    def _remap_state_dict_keys(model, sd):
        """重映射 state_dict 的 keys 以匹配重建后的 Sequential 索引

        训练保存的是 net.<orig_idx>.<param>（Dropout/GELU 占用索引），
        重建后的 Sequential 索引不同（如 [0,1,2,3] vs [0,1,4]）。
        参数数量一致时按顺序 zip；不一致时退化为只剥 net. 前缀。
        """
        model_keys = list(model.state_dict().keys())
        param_keys = [k for k in sd.keys()
                      if not k.endswith('running_mean') and not k.endswith('running_var')]

        if len(param_keys) != len(model_keys):
            return {(k[len('net.'):] if k.startswith('net.') else k): v
                    for k, v in sd.items()}

        return {dst: sd[src] for src, dst in zip(param_keys, model_keys)}

    def _extract_features(self, img: Image.Image):
        from photo_s.lrxmp import _content_features

        torch = _need_torch()
        x = self.preprocess(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            cf = self.clip_model.encode_image(x).float()
        hand = np.array(_content_features(img), dtype=np.float32)
        # photo_s.lrxmp._content_features 末尾带 ridge 截距列（85 维）；
        # 两个 checkpoint 训练时都是 84 维 hand 特征——按 checkpoint 的
        # hand_dim 截取，多出的列（截距）不能拼进网络输入
        if self.hand_dim is not None:
            hand = hand[:self.hand_dim]
        hand_t = torch.tensor(hand, dtype=torch.float32).unsqueeze(0).to(self.device)
        return torch.cat([cf, hand_t], dim=-1)

    def predict(self, image: Image.Image) -> Dict[str, float]:
        """单图预测 → 9 字段 dict（实际值范围）"""
        self.load()
        torch = _need_torch()
        feats = self._extract_features(image)
        with torch.no_grad():
            pred = self.mlp(feats).cpu().numpy()[0]

        result = {}
        for i, f in enumerate(self.targets):
            lo, hi = self.ranges[f]
            val = (pred[i] + 1) / 2 * (hi - lo) + lo
            result[f] = float(val)
        return result

    def predict_batch(self, images: List[Image.Image]) -> List[Dict[str, float]]:
        """批量预测"""
        self.load()
        torch = _need_torch()
        feats_list = [self._extract_features(img) for img in images]
        feats = torch.cat(feats_list, dim=0)
        with torch.no_grad():
            pred = self.mlp(feats).cpu().numpy()

        results = []
        for i in range(len(images)):
            r = {}
            for j, f in enumerate(self.targets):
                lo, hi = self.ranges[f]
                r[f] = float((pred[i, j] + 1) / 2 * (hi - lo) + lo)
            results.append(r)
        return results

    def predict_local(self, image: Image.Image) -> List[Dict[str, Any]]:
        """局部调整预测 → ``[{region, params}]``（无局部头或全中性 → []）。

        局部头输出布局为 regions×params 展平（region 主序）：
        ``[r0p0, r0p1, ..., r1p0, ...]``，每个值归一化到 [-1, 1]。
        幅度低于 :data:`LOCAL_NEUTRAL_EPS` 的参数视为"该区域此项不调"，
        整个 region 全中性则不出现在结果里（不产生空蒙版）。
        """
        if self.local_mlp is None:
            return []
        torch = _need_torch()
        feats = self._extract_features(image)
        with torch.no_grad():
            pred = self.local_mlp(feats).cpu().numpy()[0]

        out: List[Dict[str, Any]] = []
        n_p = len(self.local_params)
        for i, region in enumerate(self.local_regions):
            params: Dict[str, float] = {}
            for j, f in enumerate(self.local_params):
                norm = float(pred[i * n_p + j])
                if abs(norm) < LOCAL_NEUTRAL_EPS:
                    continue
                lo, hi = self.local_ranges.get(f, (-1.0, 1.0))
                params[f] = float((norm + 1) / 2 * (hi - lo) + lo)
            if params:
                out.append({"region": region, "params": params})
        return out


def get_predictor() -> AutoTonePredictor:
    """全局单例 predictor（线程安全）"""
    global _predictor_singleton
    if _predictor_singleton is None:
        with _lock:
            if _predictor_singleton is None:
                _predictor_singleton = AutoTonePredictor()
                _predictor_singleton.load()
    return _predictor_singleton
