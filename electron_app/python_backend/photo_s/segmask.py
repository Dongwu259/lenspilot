"""AI segmentation masks (v1.8) - subject / person / object via cv2.dnn.

Three small ONNX models (all < 10 MB, downloaded once via ``modelstore``
with sha256 verification, then cached):

    u2netp.onnx        - salient subject (U2Netp, 320x320, ImageNet norm)
    pphumanseg.onnx    - people (PP-HumanSeg, 192x192, NHWC)
    yolov8n-seg.onnx   - any COCO class (YOLOv8n-seg, 640x640)

All return a float32 ``h x w`` soft mask in 0..1 at full image resolution,
so they plug into :mod:`photo_s.mask` render_mask with zero extra glue.

Dependency policy: cv2 is optional (``pip install 'photo-s-tools[enhance]'``)
and lazily imported - missing cv2 or missing weights raise a clear
``RuntimeError``, never a silent passthrough (a silent no-op mask would
quietly drop the user's local adjustments).

OpenCV 5.x notes: the new graph engine fails on some Paddle-exported ONNX
(pphumanseg) - we fall back to the classic engine per-model on the first
forward, so the same wheel works on OpenCV 4.x and 5.x.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .modelstore import WeightSpec, ensure

__all__ = ["segment", "WEIGHTS", "weight_status"]

# All weights are hosted on the PhotoS GitHub release for v1.8.0 (each
# < 10 MB, inside the user's known network-drop threshold). sha256 digests
# are pinned here; modelstore verifies on every use and refuses mismatches.
WEIGHTS: Dict[str, WeightSpec] = {
    "subject": WeightSpec(
        name="u2netp.onnx",
        url="https://github.com/Dongwu259/photo_s/releases/download/v1.8.0/u2netp.onnx",
        sha256="309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
        size=4574861,
    ),
    "person": WeightSpec(
        name="pphumanseg.onnx",
        url="https://github.com/Dongwu259/photo_s/releases/download/v1.8.0/pphumanseg.onnx",
        sha256="552d8a984054e59b5d773d24b9b12022b22046ceb2bbc4c9aaeaceb36a9ddf24",
        size=6163938,
    ),
    "object": WeightSpec(
        name="yolov8n-seg.onnx",   # fp16 (7 MB; fp32 is 13.9 MB, over the
        url="https://github.com/Dongwu259/photo_s/releases/download/v1.8.0/yolov8n-seg-fp16.onnx",
        sha256="4dd9f29ae29a19bf43949efcb6437166933d83ada397e757bdab3f89488bbb83",
        size=6984752,
    ),
}

# COCO 80 class names in YOLOv8 order (for object:label).
COCO_CLASSES: Tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

# model input sizes
_SIZES = {"subject": 320, "person": 192, "object": 640}

# per-model nets, lazily loaded and cached (forward is locked: batch
# workers may call segment() concurrently). _CLASSIC[kind] = True once the
# new graph engine failed on forward (OpenCV 5.x residual bug on
# Paddle-exported ONNX) and we re-loaded with the classic engine.
_NETS: Dict[str, Any] = {}
_CLASSIC: Dict[str, bool] = {}
# RLock: _load_net acquires it internally, and segment() may call it while
# already holding it (re-entrant by design).
_LOCK = __import__("threading").RLock()


def _cv2():
    """Lazily import cv2; a missing install raises a clear RuntimeError."""
    try:
        import cv2
        return cv2
    except ImportError:
        raise RuntimeError(
            "AI masks (subject/person/object) need opencv: "
            "pip install 'photo-s-tools[enhance]'") from None


def _model_path(kind: str) -> str:
    spec = WEIGHTS[kind]
    if not spec.sha256:
        raise RuntimeError(
            f"AI mask weights not yet pinned (v1.8.0 release pending); "
            f"expected sha256 for {spec.url}")
    return ensure(spec)


def weight_status() -> Dict[str, dict]:
    """Machine-readable weight cache state (for ``photo-s info`` etc)."""
    from .modelstore import status
    return {kind: status(spec) for kind, spec in WEIGHTS.items()}


def _load_net(kind: str, force_classic: bool = False):
    """Load + cache the cv2.dnn.Net for ``kind``.

    OpenCV 5.x new graph engine *loads* some ONNX but fails on forward
    (residual shape bug on Paddle-exported pphumanseg) - the caller
    catches that and re-loads with ``force_classic=True`` (classic engine,
    which also reads more models reliably).
    """
    cv2 = _cv2()
    with _LOCK:
        if kind in _NETS:
            return _NETS[kind]
    # 权重下载/校验（首次可达 30s）在锁外进行——持锁会让所有并行
    # worker 串行排队；modelstore.ensure 自身线程安全
    path = _model_path(kind)
    with _LOCK:
        if kind in _NETS:      # 另一 worker 可能已缓存
            return _NETS[kind]
        kwargs = {}
        if force_classic and hasattr(cv2.dnn, "ENGINE_CLASSIC"):
            kwargs["engine"] = cv2.dnn.ENGINE_CLASSIC
        try:
            net = cv2.dnn.readNetFromONNX(path, **kwargs)
        except Exception as e:
            if not kwargs and hasattr(cv2.dnn, "ENGINE_CLASSIC"):
                # classic-engine fallback on load failure too
                net = cv2.dnn.readNetFromONNX(
                    path, cv2.dnn.ENGINE_CLASSIC)
                _CLASSIC[kind] = True
            else:
                raise RuntimeError(
                    f"failed to load {kind} model {path} "
                    f"(OpenCV {cv2.__version__}): {e}") from None
        _NETS[kind] = net
        return net


def segment(img, kind: str, label: str = None) -> np.ndarray:
    """Full-resolution soft mask (float32 h x w, 0..1) for one image.

    ``kind``: subject | person | object. ``label`` (COCO class name) is
    required for object. Raises RuntimeError when cv2 or weights are
    missing - never silently returns an empty mask.
    """
    if kind == "object":
        if not label:
            raise RuntimeError("object segmentation needs a COCO label")
        lk = label.strip().lower()
        if lk not in COCO_CLASSES:
            raise RuntimeError(
                f"unknown COCO label {label!r} (see COCO_CLASSES)")
        class_id = COCO_CLASSES.index(lk)
    else:
        class_id = None
    if kind not in _SIZES:
        raise RuntimeError(f"unknown segmentation kind {kind!r}")
    cv2 = _cv2()
    h, w = img.height, img.width
    try:
        # _load_net 自管理锁（下载在锁外）；forward 全程持锁
        # （cv2 Net 非线程安全）
        net = _load_net(kind)
        with _LOCK:
            if kind == "subject":
                return _u2netp_forward(cv2, net, img, h, w)
            if kind == "person":
                return _pphumanseg_forward(cv2, net, img, h, w)
            return _yolo_forward(cv2, net, img, h, w, class_id)
    except cv2.error:
        # OpenCV 5.x new engine can load but fail on forward (residual
        # shape bug on Paddle-exported pphumanseg) - retry classic once.
        if _CLASSIC.get(kind) or not hasattr(cv2.dnn, "ENGINE_CLASSIC"):
            raise
        _CLASSIC[kind] = True
        _NETS.pop(kind, None)
        with _LOCK:
            net = _load_net(kind, force_classic=True)
            if kind == "subject":
                return _u2netp_forward(cv2, net, img, h, w)
            if kind == "person":
                return _pphumanseg_forward(cv2, net, img, h, w)
            return _yolo_forward(cv2, net, img, h, w, class_id)


# ── per-model forward passes ─────────────────────────────────────────────────

def _u2netp_forward(cv2, net, img, h, w) -> np.ndarray:
    """U2Netp: NCHW RGB 320x320, ImageNet norm; output d0 saliency 0..1."""
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    blob = cv2.dnn.blobFromImage(
        rgb, scalefactor=1.0, size=(320, 320),
        mean=(0.485, 0.456, 0.406), swapRB=False)
    net.setInput(blob)
    out = net.forward()
    if isinstance(out, (list, tuple)):
        out = out[0]  # d0 = fused saliency (single-output exports anyway)
    m = np.asarray(out[0, 0], dtype=np.float32)  # 320x320
    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(m, 0.0, 1.0)


def _pphumanseg_forward(cv2, net, img, h, w) -> np.ndarray:
    """PP-HumanSeg: NHWC BGR 192x192, (x/255-0.5)/0.5; out [1,2,192,192]."""
    bgr = np.asarray(img.convert("RGB"), dtype=np.float32)
    bgr = bgr[:, :, ::-1] / 255.0 - 0.5
    bgr = bgr / 0.5
    blob = cv2.dnn.blobFromImage(bgr, scalefactor=1.0, size=(192, 192),
                                 swapRB=False)
    # NHWC input: swap axes after resize
    blob = np.transpose(blob, (0, 2, 3, 1)).copy()
    net.setInput(blob)
    out = net.forward()
    if isinstance(out, (list, tuple)):
        out = out[0]
    prob = np.asarray(out[0], dtype=np.float32)  # [2, 192, 192] softmax
    person = prob[1]
    person = cv2.resize(person, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(person, 0.0, 1.0)


def _yolo_forward(cv2, net, img, h, w, class_id) -> np.ndarray:
    """YOLOv8n-seg: NCHW RGB 640x640 (letterboxed); decode masks in numpy.

    Outputs (no-NMS export): preds [1, 4+80+32, 8400] + proto [1, 32, 160,
    160]. Returns the union of all detected instances of ``class_id``.
    """
    size = _SIZES["object"]
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    # letterbox to size x size, record scale + offsets for un-warping
    scale = size / max(h, w)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[:nh, :nw] = resized
    blob = cv2.dnn.blobFromImage(canvas, scalefactor=1 / 255.0,
                                 size=(size, size), swapRB=False)
    net.setInput(blob)
    # Outputs are addressable by name (the OpenCV 5.x graph engine's plain
    # forward() may return only one of them); fall back to positional.
    try:
        preds = net.forward("output0")
        proto = net.forward("output1")
    except Exception:
        outs = net.forward()
        if not isinstance(outs, (list, tuple)):
            outs = [outs]
        if len(outs) < 2:
            raise RuntimeError(
                f"YOLO model has {len(outs)} output(s), expected 2 "
                f"(output0 + output1)") from None
        preds, proto = outs[0], outs[1]
    preds = np.asarray(preds, dtype=np.float32)  # [1, 116, 8400] (or [1,8400,116])
    if preds.ndim == 3 and preds.shape[1] == 8400 and preds.shape[2] > 4:
        preds = np.transpose(preds, (0, 2, 1))   # -> [1, 116, 8400]
    elif preds.ndim != 3:
        raise RuntimeError(f"unexpected YOLO output shape {preds.shape}")
    proto = np.asarray(proto, dtype=np.float32)  # [1, 32, 160, 160]
    if proto.ndim == 4:
        proto = proto[0]                          # [32, 160, 160]
    if proto.ndim != 3:
        raise RuntimeError(f"unexpected YOLO proto shape {proto.shape}")

    p = preds[0]                                  # [116, 8400]
    boxes = p[:4]                                 # xywh in 640 space
    scores = p[4:4 + len(COCO_CLASSES)]
    coeffs = p[4 + len(COCO_CLASSES):]            # [32, 8400]
    cls_scores = scores[class_id]                 # [8400]
    idxs = np.where(cls_scores > 0.25)[0]
    if len(idxs) == 0:
        return np.zeros((h, w), dtype=np.float32)

    # decode boxes to xyxy in canvas space, NMS
    xywh = boxes[:, idxs]                          # [4, n]
    cx, cy, bw, bh = xywh
    x1 = cx - bw / 2.0
    y1 = cy - bh / 2.0
    x2 = cx + bw / 2.0
    y2 = cy + bh / 2.0
    keep = _nms(x1, y1, x2, y2, cls_scores[idxs], iou_thr=0.45)
    keep = np.asarray(keep, dtype=np.int64)
    if len(keep) == 0:
        return np.zeros((h, w), dtype=np.float32)

    # instance masks: sigmoid(coeff @ proto) cropped to box, composited
    mask = np.zeros((size, size), dtype=np.float32)
    for k in keep:
        c = coeffs[:, idxs[k]]                     # [32]
        m = 1.0 / (1.0 + np.exp(-(proto.reshape(32, -1).T @ c)))
        m = m.reshape(160, 160)
        m = cv2.resize(m, (size, size), interpolation=cv2.INTER_LINEAR)
        # crop to box (x2 y2 clamped)
        bx1, by1, bx2, by2 = (
            max(0, int(x1[k])), max(0, int(y1[k])),
            min(size, int(x2[k])), min(size, int(y2[k])))
        if bx2 > bx1 and by2 > by1:
            crop = m[by1:by2, bx1:bx2]
            m = np.zeros((size, size), dtype=np.float32)
            m[by1:by2, bx1:bx2] = crop
        mask = np.maximum(mask, m)

    # unwarp: crop letterboxed area, resize back to original size
    mask = mask[:nh, :nw]
    mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(mask, 0.0, 1.0)


def _nms(x1, y1, x2, y2, scores, iou_thr=0.45) -> List[int]:
    """Pure-numpy NMS (few candidates - simple greedy sort is fine)."""
    order = np.argsort(-scores)
    keep: List[int] = []
    while len(order) > 0:
        i = order[0]
        keep.append(int(i))
        if len(order) == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        iw = np.maximum(0.0, xx2 - xx1)
        ih = np.maximum(0.0, yy2 - yy1)
        inter = iw * ih
        area_i = (x2[i] - x1[i]) * (y2[i] - y1[i])
        area_r = (x2[rest] - x1[rest]) * (y2[rest] - y1[rest])
        iou = inter / np.maximum(area_i + area_r - inter, 1e-9)
        order = rest[iou <= iou_thr]
    return keep
