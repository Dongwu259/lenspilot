"""
PhotoS - HDR Merge (Exposure Fusion)

Merge bracketed exposures (same scene, different EV) into a single
high-dynamic-range image. Uses OpenCV's Mertens exposure fusion — an
automatic, parameter-free algorithm that weights each pixel by its
exposure quality, contrast, and saturation (no EV metadata needed).
``align=True`` first runs AlignMTB, so handheld brackets merge without
ghosting.

Optional dependency: `pip install 'photo-s-tools[enhance]'`
(opencv-python-headless). When missing, merge_hdr raises a clear
RuntimeError, matching the denoise/auto-straighten convention.
"""

import os
from typing import List

from PIL import Image


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        raise RuntimeError(
            "HDR merge requires the optional dependency: "
            "pip install 'photo-s-tools[enhance]' (opencv-python-headless)")


def merge_hdr(paths: List[str], align: bool = False) -> Image.Image:
    """Merge bracketed exposures into one HDR image.

    Args:
        paths: 2+ exposure image paths (any order).
        align: run AlignMTB first so handheld brackets don't ghost.

    Returns:
        A PIL RGB Image (carries the first source's EXIF in ``.info``).

    Raises:
        RuntimeError when opencv isn't installed.
        ValueError when fewer than 2 images are given or a file can't be read.
    """
    cv2 = _cv2()
    if len(paths) < 2:
        raise ValueError("HDR merge needs at least 2 bracketed exposures")

    stack = []
    for p in paths:
        if not os.path.isfile(p):
            raise ValueError(f"not a file: {p}")
        img = Image.open(p).convert("RGB")
        stack.append(img)
    exif = stack[0].info.get("exif")

    # list of uint8 RGB arrays (each H×W×3)
    import numpy as np
    imgs = [np.asarray(im) for im in stack]

    if align:
        try:
            aligner = cv2.createAlignMTB()
            aligned = []
            aligner.process(imgs, aligned)  # fills dst in place; returns None
            imgs = aligned
        except cv2.error as e:
            # Some OpenCV builds (e.g. Homebrew 5.x) ship a broken photo-module
            # binding that fails AlignMTB at the Mat-release step. Never fall
            # back to an unaligned merge silently — ghosting would surprise the
            # user. Surface it so they can retry without --align.
            raise RuntimeError(
                "AlignMTB failed in this OpenCV build — retry without "
                f"--align: {e}")

    merge = cv2.createMergeMertens()
    result = merge.process(imgs)            # float 0..1
    result = np.clip(result * 255.0, 0, 255).astype("uint8")

    out = Image.fromarray(result)
    if exif:
        out.info["exif"] = exif
    return out
