"""
PhotoS - Face Blur (Privacy Masking)

Detect faces with OpenCV's Haar cascade and blur or pixelate each face
region. Useful for privacy before sharing photos (street shots, events,
crowds). The cascade XML ships inside the opencv wheel
(``cv2.data.haarcascades``).

Optional dependency: `pip install 'photo-s-tools[enhance]'`
(opencv-python-headless). When opencv or the cascade data is missing,
``apply_face_blur`` raises a clear RuntimeError — never silently returns the
unmasked image (that would be a privacy failure the user can't see).
"""

import os

import numpy as np
from PIL import Image


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        raise RuntimeError(
            "face blur requires the optional dependency: "
            "pip install 'photo-s-tools[enhance]' (opencv-python-headless)")


def _cascade_path() -> str:
    """Path to the frontal-face Haar cascade bundled with opencv."""
    cv2 = _cv2()
    path = os.path.join(cv2.data.haarcascades,
                        "haarcascade_frontalface_default.xml")
    if not os.path.isfile(path):
        raise RuntimeError(
            "face blur: Haar cascade data not bundled with this opencv "
            "install (cv2.data.haarcascades is empty). Install "
            "opencv-python-headless from PyPI to get the cascade files.")
    return path


def _mask_region(img, cv2, box, mode, margin, strength=None):
    """Blur or pixelate one face box (cv2 Mat, in place)."""
    x, y, w, h = box
    # expand by a margin (default 20% of the face width each side)
    mx = max(1, int(w * margin / 100))
    my = max(1, int(h * margin / 100))
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(img.shape[1], x + w + mx), min(img.shape[0], y + h + my)
    region = img[y0:y1, x0:x1]
    if mode == "pixelate":
        # mosaic: shrink 1/8 then nearest-upscale
        small = cv2.resize(region, (max(1, region.shape[1] // 8),
                                    max(1, region.shape[0] // 8)),
                           interpolation=cv2.INTER_LINEAR)
        region[...] = cv2.resize(small, (region.shape[1], region.shape[0]),
                                 interpolation=cv2.INTER_NEAREST)
    else:  # blur (default)
        ksize = max(1, (max(region.shape[:2]) // 8) | 1)  # odd kernel
        sigma = strength if strength else ksize / 4.0
        region[...] = cv2.GaussianBlur(region, (ksize, ksize), sigma)


def apply_face_blur(img: Image.Image, mode: str = "blur",
                    margin: int = 20, strength=None):
    """Blur or pixelate every detected face in a PIL image.

    Args:
        img: PIL image (any mode; RGB/RGBA/L preserved).
        mode: "blur" (Gaussian) or "pixelate" (mosaic).
        margin: face-box expansion percent (default 20).
        strength: blur sigma; None auto-scales with the face size.

    Returns:
        (new Image, faces_count). The result carries ``img.info`` (EXIF/ICC
        preserved) so downstream EXIF handling is unaffected.

    Raises:
        RuntimeError when opencv or the cascade data is missing.
    """
    cv2 = _cv2()
    if mode not in ("blur", "pixelate"):
        raise ValueError(f"mode must be 'blur' or 'pixelate', got {mode!r}")

    rgba = img.convert("RGBA")
    rgb = Image.merge("RGB", rgba.split()[:3])
    mat = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)

    # Resolve the cascade path BEFORE touching CascadeClassifier: Python
    # evaluates the callee expression first, so a stripped cv2 build missing
    # CascadeClassifier would otherwise shadow the friendlier missing-cascade
    # error with a bare AttributeError.
    cascade_path = _cascade_path()
    if not hasattr(cv2, "CascadeClassifier"):
        raise RuntimeError(
            "face blur: this OpenCV build has no CascadeClassifier (stripped "
            "build?). Install opencv-python-headless from PyPI.")
    cascade = cv2.CascadeClassifier(cascade_path)
    gray = cv2.cvtColor(mat, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1,
                                     minNeighbors=5, minSize=(30, 30))

    for box in faces:
        _mask_region(mat, cv2, box, mode, margin, strength)

    # mat is in BGR channel order (opencv convention); convert back to RGB
    # before handing the buffer to PIL, otherwise R and B swap.
    out_bgr = Image.fromarray(cv2.cvtColor(mat, cv2.COLOR_BGR2RGB), "RGB")
    out = Image.merge("RGBA", (*out_bgr.split(), rgba.split()[3]))
    if img.mode != "RGBA":
        out = out.convert(img.mode if img.mode in ("RGB", "L") else "RGB")
    out.info = dict(img.info)
    return out, len(faces)
