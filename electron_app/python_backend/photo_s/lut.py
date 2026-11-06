"""3D / 1D .cube LUT support (color grading).

Parses Adobe ``.cube`` files (as exported by DaVinci Resolve, Photoshop,
Capture One, ...) and applies them to images. Pure ``numpy`` + PIL — no
optional dependencies. The official ``photo-s-plugin-lut`` overrides this
built-in trilinear engine with tetrahedral interpolation via the ``lut``
provider slot.
"""

import os

import numpy as np
from PIL import Image

# Row-chunk height for the 3D apply: bounds peak memory on very large images
# (a 112MP photo would otherwise need several GB of float intermediates).
_CHUNK_ROWS = 512

# Parsed-LUT cache keyed by (path, mtime_ns, size) — a 1000-image batch
# with --lut used to re-read and re-parse the same .cube 1000 times.
# Tables are consumed read-only, so sharing across threads is safe.
_CUBE_CACHE = {}


class LutError(ValueError):
    """Invalid or unparseable .cube LUT file."""


def load_cube(path):
    """Parse an Adobe .cube file (cached per path + mtime).

    Returns ``(kind, size, table)`` where ``kind`` is ``"3d"`` or ``"1d"``
    and ``table`` is float32:
      * 3d → shape ``(size, size, size, 3)``, ``table[b, g, r]`` = output RGB
      * 1d → shape ``(size, 3)`` for input levels ``0..size-1``

    .cube data rows are ordered with red varying fastest, then green, then
    blue (the Adobe convention), so ``table.reshape(size, size, size, 3)``
    lands on the ``[b, g, r]`` axes directly.
    """
    try:
        st = os.stat(path)
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError as e:
        raise LutError(f"cannot read .cube LUT {path}: {e}") from e
    cached = _CUBE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    kind = None
    size = 0
    rows = []
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        raise LutError(f"cannot read .cube LUT {path}: {e}") from e
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith("lut_3d_size"):
                kind = "3d"
                size = int(line.split()[-1])
            elif low.startswith("lut_1d_size"):
                kind = "1d"
                size = int(line.split()[-1])
            elif low.startswith(("domain_min", "domain_max",
                                 "lut_1d_input_range", "title", "type")):
                continue
            else:
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        rows.append([float(parts[0]), float(parts[1]),
                                     float(parts[2])])
                    except ValueError:
                        continue  # skip stray metadata we don't recognise

    if kind is None or size < 2 or not rows:
        raise LutError(f"not a valid .cube LUT: {path} "
                       f"(no LUT_3D_SIZE / LUT_1D_SIZE + data rows)")

    table = np.asarray(rows, dtype=np.float32)
    if kind == "3d":
        if table.shape != (size ** 3, 3):
            raise LutError(
                f"{path}: expected {size ** 3} rows for LUT_3D_SIZE {size}, "
                f"got {len(rows)}")
        table = table.reshape(size, size, size, 3)
        # clip to the domain implied by the spec (defensive)
        table = np.clip(table, 0.0, 1.0)
    else:
        if table.shape != (size, 3):
            raise LutError(
                f"{path}: expected {size} rows for LUT_1D_SIZE {size}, "
                f"got {len(rows)}")
    parsed = (kind, size, table)
    if len(_CUBE_CACHE) >= 8:
        _CUBE_CACHE.clear()
    _CUBE_CACHE[cache_key] = parsed
    return parsed


def _apply_1d(img, table):
    """Apply a 1D LUT via per-channel 256-entry point tables (C-fast).

    Handles L / RGB / RGBA / P: L gets the first channel's curve, RGBA keeps
    its alpha band, and palette images are converted to RGB. Any other mode
    (LA / CMYK / I;16 / F / 1) is converted to RGB as well.
    """
    if img.mode == "P":
        img = img.convert("RGB")
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        img = img.convert("RGB")
    else:
        alpha = None
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")

    n = table.shape[0]
    idx = np.linspace(0, n - 1, 256)
    luts = []
    for ch in range(3):
        lut = np.interp(idx, np.arange(n), table[:, ch])
        luts.append(np.clip(lut * 255, 0, 255).astype(np.uint8).tolist())

    if img.mode == "L":
        # single band → the GREEN channel curve (luts[1]) as the perceptual
        # stand-in; 1D LUT rows are RGB-ordered
        out = img.point(luts[1])
        return out
    r, g, b = img.split()
    out = Image.merge("RGB", (r.point(luts[0]), g.point(luts[1]),
                              b.point(luts[2])))
    if alpha is not None:
        out.putalpha(alpha)
    return out


def _apply_3d(img, table):
    """Apply a 3D LUT with trilinear interpolation, chunked by rows."""
    n = table.shape[0]
    alpha = None
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        img = img.convert("RGB")
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    h, w = arr.shape[:2]

    def _apply_chunk(chunk):
        pos = chunk * (n - 1)
        lo = np.floor(pos).astype(np.int32)
        hi = np.minimum(lo + 1, n - 1)
        frac = (pos - lo)[..., np.newaxis]
        r_lo, g_lo, b_lo = lo[..., 0], lo[..., 1], lo[..., 2]
        r_hi, g_hi, b_hi = hi[..., 0], hi[..., 1], hi[..., 2]
        fr, fg, fb = frac[..., 0, 0], frac[..., 1, 0], frac[..., 2, 0]
        # 8 corners of the unit cube
        c000 = table[b_lo, g_lo, r_lo]
        c001 = table[b_lo, g_lo, r_hi]
        c010 = table[b_lo, g_hi, r_lo]
        c011 = table[b_lo, g_hi, r_hi]
        c100 = table[b_hi, g_lo, r_lo]
        c101 = table[b_hi, g_lo, r_hi]
        c110 = table[b_hi, g_hi, r_lo]
        c111 = table[b_hi, g_hi, r_hi]
        # trilinear blend
        c00 = c000 + (c001 - c000) * fr[..., np.newaxis]
        c01 = c010 + (c011 - c010) * fr[..., np.newaxis]
        c10 = c100 + (c101 - c100) * fr[..., np.newaxis]
        c11 = c110 + (c111 - c110) * fr[..., np.newaxis]
        c0 = c00 + (c01 - c00) * fg[..., np.newaxis]
        c1 = c10 + (c11 - c10) * fg[..., np.newaxis]
        return c0 + (c1 - c0) * fb[..., np.newaxis]

    out = np.empty((h, w, 3), dtype=np.float32)
    for start in range(0, h, _CHUNK_ROWS):
        end = min(start + _CHUNK_ROWS, h)
        out[start:end] = np.clip(_apply_chunk(arr[start:end]), 0.0, 1.0)
    result = Image.fromarray((out * 255).astype(np.uint8), "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    return result


def apply_lut(img, path):
    """Load a .cube LUT from ``path`` and apply it to ``img``.

    Returns a new RGB(A) image. Raises ``LutError`` for unreadable/malformed
    files (handled by the engine as a per-file error).

    ``img.info`` (EXIF / ICC / DPI) is copied onto the result: the trilinear
    and 1D paths build fresh images via fromarray/merge/point, which would
    otherwise drop metadata and LUT-graded deliveries would lose copyright /
    shooting EXIF (v1.6.0 regression fix).
    """
    kind, _size, table = load_cube(path)
    if kind == "1d":
        out = _apply_1d(img, table)
    else:
        out = _apply_3d(img, table)
    out.info = img.info.copy()
    return out
