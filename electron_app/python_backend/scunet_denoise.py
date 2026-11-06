"""
scunet_denoise.py — SCUNet 强降噪（LensPilot 内置，vendored from photo_s plugin）。

SCUNet 是无级盲降噪：一个模型覆盖一段 ISO/噪声范围，本身没有强度旋钮。
`--denoise N` 于是映射为原图与全降噪结果的线性混合：out = orig*(1-t) + denoised*t，
t = clip(N/15, 0, 1) —— N>=15 取全模型输出，N=0 原样（核心 NLM 的有用区间 3-20，
15+ = "强降噪" 感觉一致）。

模型权重不随包分发：首次使用经 photo_s.modelstore 从 HuggingFace 下载（sha256 校验，
两文件: 图 3.8MB + 外部数据 73MB）。onnxruntime 缺失或下载失败 → 抛异常，调用方回退 NLM。

合约与 photo_s/denoise.py 一致：保留 alpha，img.info(EXIF/ICC) 拷贝到结果。
"""

import numpy as np
from PIL import Image

_SESSIONS = {}


def _blend(orig, denoised, strength):
    """原图与降噪结果按强度线性混合。t = clip(strength/15, 0, 1)。"""
    t = min(max(strength / 15.0, 0.0), 1.0)
    return orig * (1.0 - t) + denoised * t


def _ort():
    try:
        import onnxruntime
        return onnxruntime
    except ImportError:
        raise RuntimeError("SCUNet 强降噪需要 onnxruntime：pip install onnxruntime")


def _session(path):
    ort = _ort()
    if path not in _SESSIONS:
        providers = ort.get_available_providers()
        try:
            sess = ort.InferenceSession(path, providers=providers)
        except Exception:
            # 外部数据模型在部分加速器(CoreML 等)初始化失败，CPU 兜底
            sess = ort.InferenceSession(path,
                                        providers=["CPUExecutionProvider"])
        _SESSIONS[path] = sess
    return _SESSIONS[path]


def run_scunet(img, strength, model_path):
    """用 SCUNet ONNX 模型对 img 降噪。strength 0=原样, >=15=全模型输出。

    SCUNet 内部多级 2x 下采样(最深 /32)后把特征图 view 成 (H/8, 8, W/8, 8, C)，
    因此输入 H/W 需能被 8 × 32 = 256 整除；任意尺寸先边缘填充到 256 的倍数
    推理，再裁回原尺寸。
    """
    alpha = None
    if img.mode == "RGBA":
        alpha = img.split()[-1]
    rgb = img.convert("RGB")

    arr = np.asarray(rgb, dtype=np.float32) / 255.0  # [H,W,3]
    h, w = arr.shape[:2]
    ph = (256 - h % 256) % 256
    pw = (256 - w % 256) % 256
    if ph or pw:
        arr = np.pad(arr, ((0, ph), (0, pw), (0, 0)), mode="edge")

    tensor = np.transpose(arr, (2, 0, 1))[None, ...]

    sess = _session(model_path)
    input_name = sess.get_inputs()[0].name
    out = sess.run(None, {input_name: tensor})[0]

    denoised = np.transpose(out[0], (1, 2, 0))[:h, :w, :]  # 裁回原尺寸
    mixed = _blend(arr[:h, :w, :], denoised, float(strength))
    out = np.clip(mixed, 0.0, 1.0) * 255.0
    result = Image.fromarray(out.astype(np.uint8), mode="RGB")
    result.info = dict(img.info)

    if alpha is not None:
        result.putalpha(alpha)
    return result


# ── 权重规范（与 photo_s 官方 scunet 插件一致，HuggingFace 托管） ──

HF_BASE = "https://huggingface.co/Heliosoph/scunet-onnx/resolve/main/"
_SPECS = [
    dict(name="scunet_color_25.onnx",
         url=HF_BASE + "scunet_color_25.onnx",
         sha256="76170f1fcb84d3b3d38c4e7133a658bbbb70504e3dc5e4e4d1f7b0c395c093be",
         size=3795638),
    dict(name="scunet_color_25.onnx.data",
         url=HF_BASE + "scunet_color_25.onnx.data",
         sha256="f46aa0f533e5bb8b2485a6531a205e270f4a7be2447880a12d97f5f00b1e70c9",
         size=73138176),
]


def _ensure_weights():
    """下载并校验 SCUNet 权重（缓存命中即返回）。失败抛异常。"""
    from photo_s.modelstore import WeightSpec, ensure
    paths = []
    for s in _SPECS:
        paths.append(ensure(WeightSpec(**s), timeout=120))
    return paths


def apply_scunet(img, strength):
    """LensPilot 入口：确保权重后执行 SCUNet 强降噪。缺失/失败抛异常。"""
    paths = _ensure_weights()
    return run_scunet(img, float(strength), paths[0])
