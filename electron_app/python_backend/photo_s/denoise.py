"""
PhotoS - Denoising (OpenCV non-local means, optional dependency)

Classic NLM denoise for high-ISO / low-light photos. Requires the optional
`enhance` extra: `pip install 'photo-s-tools[enhance]'` (opencv-python-headless).
"""

import numpy as np
from PIL import Image


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        raise RuntimeError(
            "denoise requires the optional dependency: "
            "pip install 'photo-s-tools[enhance]' (opencv-python-headless)")


def apply_denoise(img: Image.Image, strength: float = 10.0) -> Image.Image:
    """Non-local means denoise (colored). ``strength`` ~3-20 (typical 10).

    Alpha is preserved; EXIF/ICC in ``img.info`` is copied onto the result.
    Raises RuntimeError if OpenCV is not installed.
    """
    cv2 = _cv2()
    alpha = None
    if img.mode in ("RGBA", "LA", "P"):
        # Any alpha-carrying mode: keep the alpha band, denoise the RGB view,
        # then re-attach (LA previously lost its alpha band entirely).
        rgba = img.convert("RGBA")
        alpha = rgba.split()[-1]
        rgb = rgba.convert("RGB")
    elif img.mode == "L":
        rgb = img
    else:
        rgb = img.convert("RGB")

    arr = np.array(rgb)
    h = float(strength)
    if img.mode == "L":
        out = cv2.fastNlMeansDenoising(arr, None, h, 7, 21)
    else:
        out = cv2.fastNlMeansDenoisingColored(arr, None, h, h, 7, 21)

    result = Image.fromarray(out)
    result.info = dict(img.info)
    if alpha is not None:
        result = result.convert("RGB")
        result.putalpha(alpha)
    return result
