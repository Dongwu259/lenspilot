"""
PhotoS - Automatic horizon straightening (OpenCV, optional dependency)

Detects the dominant near-horizontal line (horizon) via Canny + Hough and
rotates the image to level it. Confidence-gated: only rotates when a strong,
consistent near-horizontal line is found, otherwise leaves the image alone
(avoids tilting portraits / close-ups).

Requires the optional `enhance` extra: `pip install 'photo-s-tools[enhance]'`.
"""

import math

import numpy as np
from PIL import Image


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        raise RuntimeError(
            "auto-straighten requires the optional dependency: "
            "pip install 'photo-s-tools[enhance]' (opencv-python-headless)")


def detect_horizon_angle(img: Image.Image, max_angle: float = 10.0):
    """Detect the dominant near-horizontal line angle, or None if not confident.

    Confidence rules: the longest candidate line must span ≥ 0.35× the image
    min dimension, and the combined length of lines agreeing with it (within
    1.5°) must be ≥ 0.45× the image max dimension, OR ≥ 2 long lines agree.
    """
    cv2 = _cv2()
    w, h = img.size
    gray = np.array(img.convert("L"))
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, math.pi / 180, 100,
                            minLineLength=max(80, int(min(w, h) * 0.35)),
                            maxLineGap=10)
    if lines is None:
        return None

    cands = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        dx = x2 - x1
        if abs(dx) < 20:
            continue
        ang = math.degrees(math.atan2(y2 - y1, dx))
        if abs(ang) > max_angle:
            continue  # too steep — not a horizon candidate
        cands.append((math.hypot(dx, y2 - y1), ang))
    if not cands:
        return None

    # anchor = longest candidate line
    cands.sort(key=lambda c: -c[0])
    main_len, main_ang = cands[0]
    if main_len < min(w, h) * 0.35:
        return None

    agreeing = [ang for ln, ang in cands if abs(ang - main_ang) < 1.5]
    agree_len = sum(ln for ln, ang in cands if abs(ang - main_ang) < 1.5)
    if agree_len < max(w, h) * 0.45 and len(agreeing) < 2:
        return None
    return sum(agreeing) / len(agreeing)


def _rotate_expand(img: Image.Image, degrees: float) -> Image.Image:
    if img.mode == "P":
        img = img.convert("RGB")
    if img.mode not in ("RGB", "RGBA", "CMYK"):
        # single-band / LA modes: PIL rotate requires a scalar fill color
        fill = 0
    else:
        fill = (0, 0, 0, 255) if img.mode == "RGBA" else (0, 0, 0)
    out = img.rotate(degrees, expand=True, resample=Image.BICUBIC,
                     fillcolor=fill)
    out.info = dict(img.info)
    return out


def apply_auto_straighten(img: Image.Image, max_angle: float = 10.0):
    """Rotate to level a detected horizon.

    Returns (image, straightened: bool). Unchanged + False when no confident
    horizon is found. Raises RuntimeError if OpenCV is missing.

    IMPORTANT: callers must unpack the 2-tuple — ``img, ok = ...``. Do not
    feed the return value straight into np.array()/further PIL calls.
    """
    angle = detect_horizon_angle(img, max_angle)
    if angle is None or abs(angle) < 0.05:
        return img, False  # no confident horizon, or effectively already level
    return _rotate_expand(img, angle), True
