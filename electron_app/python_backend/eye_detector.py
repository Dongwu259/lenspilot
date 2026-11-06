"""
eye_detector.py — geometric eye-open detection via facial landmarks (EAR).

Uses a MediaPipe FaceMesh landmark model (converted to ONNX, ~5MB, Apache-2.0)
run through the already-bundled onnxruntime. Pure geometry (Eye Aspect Ratio) —
no learned eye classifier, so it inherits no dataset bias (unlike the old
CEW/MRL-trained eye head). Returns eye_open ∈ [0,1], or None when no reliable
face is found (profiles / no face → don't guess).

Pipeline: Haar face detect → crop → 256×256 → landmark ONNX → 478 landmarks →
Eye Aspect Ratio of both eyes → map to eye_open.
"""
import os
import sys
import threading
import numpy as np

from service import send_log
from utils import get_user_data_dir

# ── model location (bundled ~5MB; searched in a few known spots) ──
_MODEL_NAME = "face_landmarks.onnx"
_here = os.path.dirname(os.path.abspath(__file__))
_SEARCH = [
    os.path.join(get_user_data_dir(), "model", _MODEL_NAME),
    os.path.join(_here, "..", "model", _MODEL_NAME),
    os.path.join(_here, "model", _MODEL_NAME),
    os.path.join(_here, "..", "..", "_eye_model_scratch", _MODEL_NAME),  # dev
]
# Frozen (PyInstaller) locations: --add-data lands the model under _MEIPASS/model
# (onedir) or next to the service exe. Search these first when packaged.
if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", "")
    _exedir = os.path.dirname(sys.executable)
    for _base in (_meipass, _exedir):
        if _base:
            _SEARCH.insert(0, os.path.join(_base, "model", _MODEL_NAME))
            _SEARCH.insert(1, os.path.join(_base, _MODEL_NAME))

# Standard MediaPipe FaceMesh EAR landmark indices (6 per eye).
_R = [33, 160, 158, 133, 153, 144]   # subject's right eye
_L = [362, 385, 387, 263, 373, 380]  # subject's left eye

# EAR → eye_open mapping (validated on real photos: closed≈0.01, half≈0.20, open≈0.31)
_EAR_CLOSED = 0.10
_EAR_OPEN = 0.28

_sess = None
_inp_name = None
_lock = threading.Lock()
_face_cc = None
_load_failed = False


def _model_path():
    for p in _SEARCH:
        if os.path.exists(p):
            return p
    return None


def _ensure_loaded():
    """Lazy-load the landmark ONNX + Haar cascade. Returns True if ready."""
    global _sess, _inp_name, _face_cc, _load_failed
    if _sess is not None:
        return True
    if _load_failed:
        return False
    with _lock:
        if _sess is not None:
            return True
        try:
            import onnxruntime as ort
            import cv2
            mp = _model_path()
            if not mp:
                _load_failed = True
                return False
            _sess = ort.InferenceSession(mp, providers=["CPUExecutionProvider"])
            _inp_name = _sess.get_inputs()[0].name
            _face_cc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            send_log("eye_detector: landmark model loaded", "info")
            return True
        except Exception as e:
            send_log(f"eye_detector load failed: {e}", "warn")
            _load_failed = True
            return False


def _ear(pts, idx):
    p = [pts[i] for i in idx]
    d = lambda a, b: float(np.linalg.norm(np.array(a[:2]) - np.array(b[:2])))
    horiz = 2.0 * d(p[0], p[3]) + 1e-6
    return (d(p[1], p[5]) + d(p[2], p[4])) / horiz


def eye_open_score(bgr_or_path):
    """Return eye_open ∈ [0,1] (1=wide open, 0=closed), or None if no reliable
    face. Accepts a BGR numpy image (cv2) or a file path. Never raises."""
    if not _ensure_loaded():
        return None
    try:
        import cv2
        if isinstance(bgr_or_path, str):
            bgr = cv2.imread(bgr_or_path)
        else:
            bgr = bgr_or_path
        if bgr is None:
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        faces = _face_cc.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        if len(faces) == 0:
            return None  # no face → unknown, don't guess
        x, y, w, h = sorted(faces, key=lambda f: -f[2] * f[3])[0]
        m = int(0.15 * w)
        x0, y0 = max(0, x - m), max(0, y - m)
        x1, y1 = min(bgr.shape[1], x + w + m), min(bgr.shape[0], y + h + m)
        crop = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2RGB)
        inp = cv2.resize(crop, (256, 256)).astype(np.float32) / 255.0
        out = _sess.run(None, {_inp_name: inp[None]})
        lm = np.array(out[0]).reshape(-1, 3)  # (478, 3) in 256-px space
        if lm.shape[0] < 468:
            return None
        ear = (_ear(lm, _R) + _ear(lm, _L)) / 2.0
        # map to [0,1]
        v = (ear - _EAR_CLOSED) / (_EAR_OPEN - _EAR_CLOSED)
        return float(max(0.0, min(1.0, v)))
    except Exception as e:
        send_log(f"eye_open_score failed: {e}", "warn")
        return None


def available():
    return _model_path() is not None
