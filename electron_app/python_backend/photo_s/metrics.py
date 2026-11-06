"""
PhotoS - Image Quality Metrics

Pure-Python SSIM (Structural Similarity Index) and PSNR with zero third-party
dependencies. Both images are downscaled to a small sample before comparison,
so the scores stay fast even for very large photos.
"""

import math
import os

from PIL import Image, UnidentifiedImageError


def _flattened(img):
    """Pixel data with Pillow version compat.

    ``get_flattened_data`` exists in Pillow 12+; older Pillows (down to the
    ``>=10.4.0`` floor) only have ``getdata``. Both return identical data
    (per-pixel tuples for RGB, flat ints for L) — fall back to keep py3.9
    (which resolves to Pillow 11) working.
    """
    gfd = getattr(img, "get_flattened_data", None)
    if gfd is not None:
        return gfd()
    return img.getdata()


def _open_decodable(path: str):
    """Open any supported input, RAW included.

    PIL has no RAW decoder, so exposure stats / culling used to reject every
    .cr2/.nef/... outright (ok=False → "reject") even though RAW bursts are
    the primary culling target. Falls back to the engine's rawpy/sips decode
    (half-size: these calls only sample small thumbnails). Returns
    ``(img, temp_paths)`` — callers must unlink the temp paths after use
    (the macOS sips fallback decodes via a temp TIFF).
    """
    try:
        return Image.open(path), []
    except UnidentifiedImageError:
        from .engine import RAW_EXTENSIONS
        if os.path.splitext(path)[1].lower() not in RAW_EXTENSIONS:
            raise
        from .engine import ProcessOptions, _load_raw
        img = _load_raw(path, ProcessOptions(raw_half_size=True))
        temps = [p for p in (getattr(img, "_temp_png", None),
                             getattr(img, "_temp_raw_tiff", None)) if p]
        return img, temps


def _cleanup_temps(temp_paths) -> None:
    for t in temp_paths:
        try:
            os.unlink(t)
        except OSError:
            pass


def _load_sample(path: str, sample_size: int) -> Image.Image:
    """Open an image, downscale to ≤sample_size on the longest side, convert to grayscale."""
    img, temps = _open_decodable(path)
    try:
        with img:
            if img.size[0] > sample_size or img.size[1] > sample_size:
                img = img.copy()
                img.thumbnail((sample_size, sample_size), Image.LANCZOS)
            return img.convert("L")
    finally:
        _cleanup_temps(temps)


def _window_ssim(win_a, win_b, n, c1, c2):
    """SSIM for a single window using the standard formulas from Wang et al. (2004)."""
    mu_a = sum(win_a) / n
    mu_b = sum(win_b) / n
    var_a = sum((v - mu_a) ** 2 for v in win_a) / n
    var_b = sum((v - mu_b) ** 2 for v in win_b) / n
    cov = sum((win_a[i] - mu_a) * (win_b[i] - mu_b) for i in range(n)) / n

    num = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
    return num / den


def compute_ssim(path_a: str, path_b: str, sample_size: int = 64,
                 win_size: int = 7) -> float:
    """Structural Similarity Index between two image files, in [0.0, 1.0].

    Higher is more similar; 1.0 means perceptually identical at the sample
    resolution. Uses a sliding uniform window with the standard SSIM
    constants (C1, C2) from Wang et al. (2004).

    Args:
        path_a: Path to the first image.
        path_b: Path to the second image.
        sample_size: Max dimension of the grayscale sample (default 64).
        win_size: Sliding window size (default 7). Even values are bumped to
            the next odd size — an even window cannot be centered on a pixel
            and would mix win_size+1 rows with win_size columns, skewing the
            statistics.

    Returns:
        Mean SSIM over all windows, a float in [0, 1].
    """
    a = _load_sample(path_a, sample_size)
    b = _load_sample(path_b, sample_size)

    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)

    pixels_a = list(_flattened(a))
    pixels_b = list(_flattened(b))
    width, height = a.size

    if win_size % 2 == 0:
        win_size += 1
    if win_size < 3 or win_size > min(width, height):
        win_size = 3 if min(width, height) >= 3 else 1

    half = win_size // 2
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2

    total_ssim = 0.0
    count = 0

    for y in range(half, height - half):
        base_a = y * width
        base_b = y * width
        for x in range(half, width - half):
            win_a = []
            win_b = []
            for dy in range(-half, half + 1):
                row_start = (base_a + dy * width) + x - half
                win_a.extend(pixels_a[row_start:row_start + win_size])
                win_b.extend(pixels_b[row_start:row_start + win_size])
            total_ssim += _window_ssim(win_a, win_b, win_size * win_size, c1, c2)
            count += 1

    if count == 0:
        # Image too small for any window — fall back to a global comparison.
        n = len(pixels_a)
        return _window_ssim(pixels_a, pixels_b, n, c1, c2)

    return total_ssim / count


def _load_sample_rgb(path: str, sample_size: int) -> Image.Image:
    """Open an image, downscale to ≤sample_size on the longest side, convert to RGB."""
    img, temps = _open_decodable(path)
    try:
        with img:
            if img.size[0] > sample_size or img.size[1] > sample_size:
                img = img.copy()
                img.thumbnail((sample_size, sample_size), Image.LANCZOS)
            return img.convert("RGB")
    finally:
        _cleanup_temps(temps)


def compute_psnr(path_a: str, path_b: str, sample_size: int = 256) -> float:
    """Peak Signal-to-Noise Ratio between two image files, in dB.

    Higher means closer to the reference; identical images return
    ``float("inf")``. Computed over all three RGB channels of a downscaled
    sample (peak = 255), so it stays fast even for very large photos.

    Args:
        path_a: Path to the reference image.
        path_b: Path to the image to score against the reference.
        sample_size: Max dimension of the RGB sample (default 256).

    Returns:
        PSNR in dB (negative values are possible for very different
        images), or ``float("inf")`` when the samples are identical.
    """
    a = _load_sample_rgb(path_a, sample_size)
    b = _load_sample_rgb(path_b, sample_size)

    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)

    pixels_a = list(_flattened(a))
    pixels_b = list(_flattened(b))
    n = len(pixels_a) * 3
    if n == 0:
        return float("inf")

    sq = 0
    for pa, pb in zip(pixels_a, pixels_b):
        sq += ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
               + (pa[2] - pb[2]) ** 2)
    if sq == 0:
        return float("inf")

    mse = sq / n
    return 10 * math.log10((255 * 255) / mse)


def compute_blur_score(path: str, sample_size: int = 128) -> float:
    """A blur heuristic: the variance of a 3x3 Laplacian response.

    Higher values mean more high-frequency detail (sharper); a flat image
    scores 0.0. This is the classic variance-of-Laplacian focus measure,
    computed in pure Python on a downscaled grayscale sample. Unreadable or
    tiny images score 0.0.
    """
    try:
        img = _load_sample(path, sample_size)
    except Exception:
        return 0.0
    width, height = img.size
    if width < 3 or height < 3:
        return 0.0

    pixels = list(_flattened(img))
    # 3x3 Laplacian kernel [0,1,0; 1,-4,1; 0,1,0]
    values = []
    for y in range(1, height - 1):
        base = y * width
        for x in range(1, width - 1):
            center = pixels[base + x]
            lap = (pixels[base + x - 1] + pixels[base + x + 1]
                   + pixels[base - width + x] + pixels[base + width + x]
                   - 4 * center)
            values.append(lap)

    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return variance


def compute_exposure_stats(path: str, sample_size: int = 256) -> dict:
    """Exposure statistics for culling, from a small grayscale sample.

    Returns {"ok": bool, "luminance": 0-1 mean, "overexposed_pct": % pixels
    ≥ 250, "underexposed_pct": % pixels ≤ 5}. Unreadable images → all 0, ok=False.
    """
    try:
        img = _load_sample(path, sample_size)
    except Exception:
        return {"ok": False, "luminance": 0.0,
                "overexposed_pct": 0.0, "underexposed_pct": 0.0}
    pixels = list(_flattened(img))
    n = len(pixels)
    if n == 0:
        return {"ok": False, "luminance": 0.0,
                "overexposed_pct": 0.0, "underexposed_pct": 0.0}
    over = sum(1 for p in pixels if p >= 250) / n * 100
    under = sum(1 for p in pixels if p <= 5) / n * 100
    mean = sum(pixels) / n
    return {"ok": True,
            "luminance": round(mean / 255, 3),
            "overexposed_pct": round(over, 2),
            "underexposed_pct": round(under, 2)}


# ── Perceptual analysis (v1.7.0): the "eyes" for LLM closed-loop grading ─────

def _estimate_kelvin(r_mean: float, b_mean: float) -> int:
    """Crude color-temperature heuristic from R/B channel means.

    Maps the R/B ratio around neutral (6500K at ratio 1) monotonically to
    2000..12000K. Not a calibrated measurement - it tells an agent which
    way the white balance leans, not the exact CCT.
    """
    import math as _math
    t = r_mean / max(1.0, b_mean)
    kelvin = 6500.0 * (t ** -1.5)
    return int(round(max(2000.0, min(12000.0, kelvin))))


def _hue_deg_arr(r, g, b):
    """数组版 HSV 色相（0-360），无 colorsys 逐像素开销。"""
    import numpy as np
    mx = np.maximum.reduce([r, g, b])
    mn = np.minimum.reduce([r, g, b])
    d = mx - mn
    hue = np.zeros_like(mx)
    sel = mx == r
    hue[sel] = 60.0 * (((g[sel] - b[sel]) / np.maximum(d[sel], 1e-6)) % 6.0)
    sel = mx == g
    hue[sel] = 60.0 * ((b[sel] - r[sel]) / np.maximum(d[sel], 1e-6) + 2.0)
    sel = mx == b
    hue[sel] = 60.0 * ((r[sel] - g[sel]) / np.maximum(d[sel], 1e-6) + 4.0)
    return np.where(d > 0, hue, 0.0)


def _grid_cells(arr, grid: int) -> list:
    """grid×grid 区域采样：每格 {x, y, luma, sat, r, g, b}（相对坐标 0-1）。"""
    import numpy as np
    h, w = arr.shape[:2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    cells = []
    for gy in range(grid):
        row = []
        for gx in range(grid):
            y0, y1 = h * gy // grid, h * (gy + 1) // grid
            x0, x1 = w * gx // grid, w * (gx + 1) // grid
            c = arr[y0:y1, x0:x1]
            cc_r, cc_g, cc_b = c[..., 0], c[..., 1], c[..., 2]
            cc_l = 0.299 * cc_r + 0.587 * cc_g + 0.114 * cc_b
            cc_mx = np.maximum(np.maximum(cc_r, cc_g), cc_b)
            cc_mn = np.minimum(np.minimum(cc_r, cc_g), cc_b)
            cc_sat = np.where(cc_mx > 0, (cc_mx - cc_mn) /
                              np.maximum(cc_mx, 1e-6), 0.0)
            row.append({
                "x": round((x0 + x1) / 2.0 / w, 3),
                "y": round((y0 + y1) / 2.0 / h, 3),
                "luma": round(float(cc_l.mean()) / 255.0, 3),
                "sat": round(float(cc_sat.mean()), 3),
                "r": round(float(cc_r.mean()) / 255.0, 3),
                "g": round(float(cc_g.mean()) / 255.0, 3),
                "b": round(float(cc_b.mean()) / 255.0, 3),
            })
        cells.append(row)
    return cells


def _region_heuristics(arr) -> dict:
    """启发式分区：天空/肤色占比 + 过曝/欠曝区域框（16×16 块聚类，粗粒度）。"""
    import numpy as np
    h, w = arr.shape[:2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    out: dict = {"sky": None, "skin": None, "overexposed": [], "underexposed": []}

    def _stats(mask, luma_arr, sat_arr):
        if mask is None or not bool(mask.sum()):
            return None
        return {
            "pct": round(float(mask.sum()) / float(mask.size) * 100.0, 2),
            "luma": round(float(luma_arr[mask].mean()) / 255.0, 3),
            "sat": round(float(sat_arr[mask].mean()), 3),
        }

    # 天空：上 30% 行、高亮低饱和
    top = int(h * 0.3)
    sky = (luma[:top] > 120) & (sat[:top] < 0.35)
    if bool(sky.sum()):
        out["sky"] = _stats(sky, luma[:top], sat[:top])
    # 肤色：中饱和度 + 色相 10-45°
    hue = _hue_deg_arr(r, g, b)
    skin = (sat > 0.15) & (sat < 0.6) & (hue > 10) & (hue < 45)
    if bool(skin.sum()):
        out["skin"] = _stats(skin, luma, sat)

    # 过曝/欠曝区域框：16×16 块占比 → 4 连通合并
    blocks = 16
    def _clusters(mask):
        bh, bw = h // blocks, w // blocks
        if bh < 1 or bw < 1:
            return []
        pct = np.zeros((blocks, blocks))
        for y in range(blocks):
            for x in range(blocks):
                blk = mask[y * bh:(y + 1) * bh, x * bw:(x + 1) * bw]
                pct[y, x] = float(blk.sum()) / max(blk.size, 1)
        hot = pct > 0.5
        seen = np.zeros_like(hot)
        clusters = []
        for y in range(blocks):
            for x in range(blocks):
                if not hot[y, x] or seen[y, x]:
                    continue
                stack = [(y, x)]
                seen[y, x] = True
                ys, xs = [], []
                while stack:
                    cy, cx = stack.pop()
                    ys.append(cy)
                    xs.append(cx)
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < blocks and 0 <= nx < blocks \
                                and hot[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                y0, y1 = min(ys) * bh / h, (max(ys) + 1) * bh / h
                x0, x1 = min(xs) * bw / w, (max(xs) + 1) * bw / w
                clusters.append({"x": round(x0, 3), "y": round(y0, 3),
                                 "w": round(x1 - x0, 3), "h": round(y1 - y0, 3),
                                 "pct": round(float(pct[ys, xs].mean()), 2)})
        return clusters

    out["overexposed"] = _clusters(luma >= 250)
    out["underexposed"] = _clusters(luma <= 5)
    return out


def analyze_image(path: str, sample_size: int = 256, grid: int = 0) -> dict:
    """Perceptual image analysis for grading feedback loops.

    One call returns what an agent (or a future grading model) needs to
    judge a result: per-channel + luma histograms (32 bins), channel
    mean/median/std, contrast, saturation, a white-balance lean estimate,
    exposure stats and the blur score. Sampling is capped at
    ``sample_size`` on the longest side, so this stays fast on any photo.

    ``analyze -> adjust params -> process -> analyze`` is the intended
    loop (see docs/AGENT_API.md). Unreadable images return ``ok=False``.
    """
    try:
        img = _load_sample_rgb(path, sample_size)
    except Exception:
        return {"ok": False, "path": path, "error": "unreadable image"}

    import numpy as np
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape[:2]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    # Luma (Rec. 601) - matches what the exposure/blur heuristics see.
    luma = 0.299 * r + 0.587 * g + 0.114 * b

    def hist(channel):
        counts, _ = np.histogram(channel, bins=32, range=(0, 255))
        return [int(c) for c in counts]

    # Saturation mean via max/min per pixel (fast, no HSV roundtrip).
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)

    n = float(luma.size) or 1.0
    out = {
        "ok": True,
        "path": path,
        "size": [w, h],
        "histogram": {
            "r": hist(r), "g": hist(g), "b": hist(b), "luma": hist(luma),
        },
        "stats": {
            "mean": {"r": round(float(r.mean()), 1),
                     "g": round(float(g.mean()), 1),
                     "b": round(float(b.mean()), 1),
                     "luma": round(float(luma.mean()), 1)},
            "median": {"r": round(float(np.median(r)), 1),
                       "g": round(float(np.median(g)), 1),
                       "b": round(float(np.median(b)), 1),
                       "luma": round(float(np.median(luma)), 1)},
            "std": {"r": round(float(r.std()), 1),
                    "g": round(float(g.std()), 1),
                    "b": round(float(b.std()), 1),
                    "luma": round(float(luma.std()), 1)},
            "contrast": round(float(luma.std()) / 255.0, 3),
            "saturation_mean": round(float(sat.mean()), 3),
        },
        "white_balance": {
            "kelvin_estimate": _estimate_kelvin(float(r.mean()),
                                                float(b.mean())),
            "tint_gm": round(float(g.mean() - (r.mean() + b.mean()) / 2.0), 1),
        },
        "exposure": {
            "luminance": round(float(luma.mean()) / 255.0, 3),
            "overexposed_pct": round(float((luma >= 250).sum()) / n * 100, 2),
            "underexposed_pct": round(float((luma <= 5).sum()) / n * 100, 2),
        },
        "blur_score": round(compute_blur_score(path, sample_size=128), 2),
    }
    if grid in (4, 8):
        out["grid"] = _grid_cells(arr, grid)
        out["regions"] = _region_heuristics(arr)
    return out


# ---------------------------------------------------------------- v1.9.0 工具层

def compare_images(path_a: str, path_b: str,
                   sample_size: int = 256) -> dict:
    """before/after 对比：PSNR / SSIM / 平均绝对差（``photo-s diff`` 数据核心）。"""
    import numpy as np
    try:
        a = _load_sample_rgb(path_a, sample_size)
        b = _load_sample_rgb(path_b, sample_size)
        if a.size != b.size:
            b = b.resize(a.size)
        arr_a = np.asarray(a, dtype=np.float32)
        arr_b = np.asarray(b, dtype=np.float32)
        mad = float(np.abs(arr_a - arr_b).mean())
    except Exception:
        return {"ok": False, "path_a": path_a, "path_b": path_b,
                "error": "unreadable image"}
    psnr = compute_psnr(path_a, path_b, sample_size=min(sample_size, 256))
    ssim = compute_ssim(path_a, path_b, sample_size=64)
    return {
        "ok": True, "path_a": path_a, "path_b": path_b,
        "psnr": round(psnr, 2), "ssim": round(ssim, 4),
        "mean_abs_diff": round(mad, 2),
        "size": [a.size[0], a.size[1]],
    }


def _histogram_png(img) -> str:
    """直方图 PNG（256×128，luma 白 + RGB 三色线）→ base64。"""
    import base64
    import io
    import numpy as np
    from PIL import Image as _PILImage
    from PIL import ImageDraw
    arr = np.asarray(img, dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    im = _PILImage.new("RGB", (256, 128), (16, 16, 20))
    d = ImageDraw.Draw(im)

    def line(ch, color):
        counts, _ = np.histogram(ch, bins=256, range=(0, 255))
        m = float(counts.max()) or 1.0
        pts = [(x, 127 - int(c / m * 124)) for x, c in enumerate(counts)]
        d.line(pts, fill=color, width=1)

    line(luma, (255, 255, 255))
    line(r, (230, 60, 60))
    line(g, (60, 200, 90))
    line(b, (70, 110, 230))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def snapshot_image(path: str, max_dim: int = 1024,
                   include_histogram: bool = True) -> dict:
    """agent 视觉快照：缩放 JPEG base64 + 直方图 PNG base64。

    多模态 agent 的「眼睛」——analyze 给统计数字，preview 直接给图。
    ``jpeg_bytes`` = base64 长度（agent 可据此决定是否降采样）。
    """
    import base64
    import io
    try:
        img = _load_sample_rgb(path, max_dim)
    except Exception:
        return {"ok": False, "path": path, "error": "unreadable image"}
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    out = {"ok": True, "path": path, "size": list(img.size), "format": "JPEG",
           "jpeg_base64": b64, "jpeg_bytes": len(b64)}
    if include_histogram:
        out["histogram_png_base64"] = _histogram_png(img)
    return out
