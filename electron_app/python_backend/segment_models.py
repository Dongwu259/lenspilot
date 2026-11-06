"""segment_models.py — 主体/背景 AI 分割（P5，u²-netp ONNX 一步到位）

模型：u²-netp（U²-Net 轻量版，4.5MB，Apache-2.0，输入 320×320 显著性图，
CPU 推理 ~0.2-0.5s）——lumen-raw 同款路线。背景 = 1 − 主体。

权重策略（沿用 modelstore 红线 + 小体积例外）：
  u2netp 仅 4.5MB（非 GB 级塔权重），首次使用 subject/background 蒙版时
  自动经 photo_s.modelstore.ensure 下载（GitHub release 主源 → gh-proxy
  镜像回落，sha256 钉死校验）；下载失败回退「中心显著色差」启发式蒙版，
  功能不中断，并做 1h 退避避免每次渲染都撞网络。
  URL / sha256 可用环境变量覆盖（离线/镜像/测试）：
    LENSPILOT_SEGMENT_U2NETP_URL     （支持 file://）
    LENSPILOT_SEGMENT_U2NETP_SHA256

缓存：显著性图按「32×32 缩略内容哈希」缓存（LRU 3）——滑杆拖动引起像素
微变不改主体，命中缓存只需一次 resize；跨分辨率复用同一 320 显著性图。
"""

import hashlib
import os
import threading
import time

import numpy as np

from service import send_log

_U2NETP_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
_U2NETP_URL = ("https://github.com/danielgatis/rembg/releases/download/"
               "v0.0.0/u2netp.onnx")
_U2NETP_URL_MIRROR = ("https://gh-proxy.com/https://github.com/danielgatis/"
                      "rembg/releases/download/v0.0.0/u2netp.onnx")
_WORK_SIZE = 320

_lock = threading.Lock()
_session = None          # ort.InferenceSession | None
_session_err = None
_download_backoff_until = 0.0   # 下载失败退避（含校验失败）
_sal_cache = {}          # md5(32x32 bytes) -> 320x320 float32（LRU 3）
_SAL_CACHE_MAX = 3
_last_error = None


def _urls():
    url = os.environ.get("LENSPILOT_SEGMENT_U2NETP_URL", "").strip() or _U2NETP_URL
    urls = [url]
    mirror = os.environ.get("LENSPILOT_SEGMENT_U2NETP_URL_MIRROR", "").strip()
    if mirror:
        urls.append(mirror)
    elif url == _U2NETP_URL:
        urls.append(_U2NETP_URL_MIRROR)
    return urls


def _sha256_expected():
    return (os.environ.get("LENSPILOT_SEGMENT_U2NETP_SHA256", "").strip()
            or _U2NETP_SHA256).lower()


def _ensure_weights():
    """下载/校验权重（modelstore：断点续传 + sha256 + 原子落盘）。返回路径或抛错。"""
    from photo_s.modelstore import WeightSpec, ensure
    last = None
    for url in _urls():
        spec = WeightSpec(name="u2netp.onnx", url=url, sha256=_sha256_expected())
        try:
            path = ensure(spec, timeout=90)
            return path
        except Exception as e:  # 换下一个来源
            last = e
    raise RuntimeError(f"u2netp download failed: {last}")


def _load_session():
    """懒加载 ONNX 会话（CPU）。成功返回 session，失败返回 None 并记录原因。"""
    global _session, _session_err, _download_backoff_until
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        if time.time() < _download_backoff_until:
            return None
        try:
            import onnxruntime as ort
            path = _ensure_weights()
            opts = None
            try:
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = max(2, (os.cpu_count() or 4) // 2)
            except Exception:
                opts = None
            _session = (ort.InferenceSession(path, sess_opts=opts,
                                             providers=["CPUExecutionProvider"])
                        if opts else
                        ort.InferenceSession(path, providers=["CPUExecutionProvider"]))
            _session_err = None
            _last_error = None
            send_log("segment: u2netp loaded (subject/background masks ready)", "info")
        except Exception as e:
            _session = None
            _session_err = str(e)
            _last_error = str(e)
            _download_backoff_until = time.time() + 3600  # 1h 退避
            send_log(f"segment: u2netp unavailable ({e}) — fallback to heuristic "
                     f"for 1h", "warn")
            return None
    return _session


def _content_key(pil_img):
    tiny = pil_img.convert("RGB").resize((32, 32))
    return hashlib.md5(tiny.tobytes()).hexdigest()


def _saliency_320(pil_img):
    """320×320 显著性图（带内容哈希缓存）。失败返回 None。"""
    key = _content_key(pil_img)
    with _lock:
        if key in _sal_cache:
            _sal_cache[key] = _sal_cache.pop(key)  # LRU touch
            return _sal_cache[key]
    sess = _load_session()
    if sess is None:
        return None
    from PIL import Image
    img = pil_img.convert("RGB").resize((_WORK_SIZE, _WORK_SIZE), Image.BILINEAR)
    arr = np.asarray(img, np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    x = ((arr - mean) / std).transpose(2, 0, 1)[None]
    inp_name = sess.get_inputs()[0].name
    out = sess.run(None, {inp_name: x})[0]
    sal = np.asarray(out, np.float32).squeeze()
    if sal.ndim != 2:
        raise RuntimeError(f"unexpected u2netp output shape {sal.shape}")
    mn, mx = float(sal.min()), float(sal.max())
    sal = (sal - mn) / max(1e-6, mx - mn)
    with _lock:
        _sal_cache[key] = sal.astype(np.float32)
        while len(_sal_cache) > _SAL_CACHE_MAX:
            _sal_cache.pop(next(iter(_sal_cache)))
    return _sal_cache[key]


def get_subject_mask(pil_img):
    """主体显著性蒙版 → (h, w) float32 0-1，或 None（模型不可用）。"""
    if pil_img is None:
        return None
    try:
        sal = _saliency_320(pil_img)
        if sal is None:
            return None
        from PIL import Image
        w, h = pil_img.size
        m = Image.fromarray((np.clip(sal, 0, 1) * 255).astype(np.uint8))
        return (np.asarray(m.resize((w, h), Image.BILINEAR),
                           np.float32) / 255.0)
    except Exception as e:
        global _last_error
        _last_error = str(e)
        send_log(f"segment: inference failed: {e}", "warn")
        return None


def status():
    """供 IPC / 设置页展示的就绪状态。"""
    with _lock:
        cached = bool(_session is not None)
    path = None
    try:
        from photo_s.modelstore import WeightSpec, cached_path
        spec = WeightSpec(name="u2netp.onnx", url=_U2NETP_URL,
                          sha256=_sha256_expected())
        path = cached_path(spec)
    except Exception:
        pass
    return {
        "model": "u2netp",
        "loaded": cached,
        "cached": bool(path),
        "path": path,
        "error": _session_err or _last_error,
        "backoff_until": _download_backoff_until,
    }


# ── IPC（P0-1 注册表） ──────────────────────────────────────────────────────

import command_registry  # noqa: E402
from command_registry import command  # noqa: E402


@command("segment_model_status", "editor",
         "[编辑器/AI] 查询主体分割模型（u2netp）的就绪状态（已加载/已缓存/错误）。只读。",
         params_schema={"type": "object", "properties": {},
                        "additionalProperties": False})
def handle_segment_model_status(params):
    s = status()
    return {"ok": True, **s}


@command("segment_warmup", "editor",
         "[编辑器/AI] 预热主体分割：如权重缺失则后台下载（约 4.5MB，一次性）。"
         "编辑器打开蒙版面板时由前端触发，避免首次点击主体蒙版时等待。",
         params_schema={"type": "object", "properties": {},
                        "additionalProperties": False})
def handle_segment_warmup(params):
    sess = _load_session()
    return {"ok": True, "ready": sess is not None,
            **{k: v for k, v in status().items() if k != "backoff_until"}}


def register_handlers():
    command_registry.register_all()
    send_log("segment_models module loaded (IPC via command_registry)", "info")
