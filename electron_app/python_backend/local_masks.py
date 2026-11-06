"""local_masks.py — 局部调整蒙版（P1-3，参照 LightCraft：无 AI 经典启发式路线）

蒙版类型 v1（全部经典算法，零模型依赖）：
  linear     线性渐变   params {x1,y1,x2,y2}（0-1 相对坐标，p1=0 端 p2=1 端）
  radial     径向渐变   params {cx,cy,rx,ry,angle,feather}（椭圆+旋转+羽化）
  brush      笔刷       params {stamps:[{x,y,r}], hardness}（r 相对短边）
  luminance  亮度范围   params {lo,hi,feather}（作用于当前全局调整后的亮度）
  color      颜色范围   params {rgb:[r,g,b], tolerance, soft}
  sky        天空(启发式) 蓝青色相 + 饱和/明度门限 + 上部位置权重

edits 协议（photo_editor.DEFAULT_EDITS["masks"]）：
  [{"id": 1, "type": "linear", "invert": false, "amount": 1.0,
    "params": {...}, "adjustments": {"exposure": 0.3, ...非默认项}}]

应用语义（LR 式）：每个蒙版 = 一层独立的局部调整——先整图应用该蒙版的
调整参数得到 adjusted，再按 mask(0-1) 与原图线性混合；蒙版间按列表顺序
叠加。amount 按「相对默认值的偏移」缩放调整强度（LR Amount 语义）。
任一蒙版失败静默跳过，绝不崩预览/导出。
"""

import numpy as np
from PIL import Image, ImageFilter

from photo_s.grade import _smoothstep, _to_hsv

MASK_TYPES = ("linear", "radial", "brush", "luminance", "color", "sky",
              "subject", "background")

# 局部调整词汇表（默认值 = 全局滑杆默认；只存非默认项）
ADJ_DEFAULTS = {
    "exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "warmth": 0.0,
    "shadows": 0.0, "highlights": 0.0, "sharpness": 0.0,
    "clarity": 0.0, "texture": 0.0, "dehaze": 0.0, "vibrance": 0.0,
}

_SOFT_TYPES = ("luminance", "color", "sky", "subject", "background")
_PIXEL_TYPES = _SOFT_TYPES  # 需要当前像素参与的蒙版类型


def _soft(e0, e1, x):
    return _smoothstep(float(e0), float(e1), x)


def _pixel_coords(h, w):
    ny = (np.arange(h, dtype=np.float32) + 0.5) / h
    nx = (np.arange(w, dtype=np.float32) + 0.5) / w
    return nx[None, :], ny[:, None]


def _mask_linear(h, w, p):
    x1, y1 = float(p.get("x1", 0.0)), float(p.get("y1", 0.0))
    x2, y2 = float(p.get("x2", 1.0)), float(p.get("y2", 0.0))
    dx, dy = x2 - x1, y2 - y1
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return np.ones((h, w), np.float32)
    nx, ny = _pixel_coords(h, w)
    t = ((nx - x1) * dx + (ny - y1) * dy) / denom
    return np.clip(t, 0.0, 1.0).astype(np.float32)


def _mask_radial(h, w, p):
    cx, cy = float(p.get("cx", 0.5)), float(p.get("cy", 0.5))
    rx = max(1e-4, float(p.get("rx", 0.35)))
    ry = max(1e-4, float(p.get("ry", 0.35)))
    ang = np.deg2rad(float(p.get("angle", 0.0) or 0.0))
    feather = min(0.95, max(0.0, float(p.get("feather", 0.45))))
    nx, ny = _pixel_coords(h, w)
    ox, oy = nx - cx, ny - cy
    ca, sa = np.float32(np.cos(ang)), np.float32(np.sin(ang))
    ex = (ox * ca + oy * sa) / rx
    ey = (-ox * sa + oy * ca) / ry
    r = np.sqrt(ex * ex + ey * ey)
    if feather <= 1e-3:
        return (r <= 1.0).astype(np.float32)
    return (1.0 - _soft(1.0 - feather, 1.0, r)).astype(np.float32)


def _mask_brush(h, w, p):
    stamps = p.get("stamps") or []
    if not stamps:
        return np.zeros((h, w), np.float32)
    hardness = min(0.98, max(0.0, float(p.get("hardness", 0.6))))
    scale = float(min(w, h))  # r 相对短边 → 分辨率无关（LightCraft 原则）
    mask = np.zeros((h, w), np.float32)
    ys = (np.arange(h, dtype=np.float32) + 0.5) / h
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w
    for s in stamps:
        try:
            sx, sy = float(s.get("x")), float(s.get("y"))
            r = max(1e-3, float(s.get("r", 0.05)))
        except (TypeError, ValueError):
            continue
        px, py = sx * w, sy * h
        rad_px = r * scale
        x0 = max(0, int(px - rad_px) - 1)
        x1 = min(w, int(px + rad_px) + 2)
        y0 = max(0, int(py - rad_px) - 1)
        y1 = min(h, int(py + rad_px) + 2)
        if x0 >= x1 or y0 >= y1:
            continue
        dx = (xs[x0:x1] * w - px) / rad_px
        dy = (ys[y0:y1] * h - py) / rad_px
        d = np.sqrt(dx[None, :] ** 2 + dy[:, None] ** 2)
        if hardness < 0.98:
            fall = (1.0 - _soft(hardness, 1.0, d)).astype(np.float32)
        else:
            fall = (d <= 1.0).astype(np.float32)
        np.maximum(mask[y0:y1, x0:x1], fall, out=mask[y0:y1, x0:x1])
    return mask


def _mask_luminance(h, w, p, rgb01):
    lo = float(p.get("lo", 0.25))
    hi = float(p.get("hi", 0.75))
    feather = max(0.02, float(p.get("feather", 0.15)))
    lo2, hi2 = min(lo, hi), max(lo, hi)
    lum = 0.299 * rgb01[..., 0] + 0.587 * rgb01[..., 1] + 0.114 * rgb01[..., 2]
    m = _soft(lo2 - feather, lo2, lum) * (1.0 - _soft(hi2, hi2 + feather, lum))
    return m.astype(np.float32)


def _mask_color(h, w, p, rgb01):
    t = p.get("rgb") or [128, 128, 128]
    target = np.asarray(t, np.float32) / 255.0
    tol = max(0.02, float(p.get("tolerance", 0.28)))
    soft = max(0.02, float(p.get("soft", 0.18)))
    dist = np.sqrt(((rgb01 - target[None, None, :]) ** 2).sum(-1)) / float(np.sqrt(3.0))
    return (1.0 - _soft(tol, tol + soft, dist)).astype(np.float32)


def _mask_sky(h, w, p, rgb01):
    """启发式天空：蓝青色相(215°±软窗) × 饱和/明度门限 × 上部位置权重。"""
    hue, sat, val = _to_hsv(rgb01)
    hue_deg = hue * 360.0
    d_blue = np.abs((hue_deg - 215.0 + 180.0) % 360.0 - 180.0)
    m_hue = 1.0 - _soft(30.0, 55.0, d_blue)
    m_sat = _soft(0.10, 0.22, sat)
    m_val = _soft(0.30, 0.45, val)
    ny = (np.arange(h, dtype=np.float32) + 0.5) / h
    w_pos = np.clip((0.80 - ny[:, None]) / 0.55, 0.10, 1.0)
    return (m_hue * m_sat * m_val * w_pos).astype(np.float32)


def _subject_heuristic(h, w, rgb01):
    """启发式主体回退（模型不可用时）：边框环带均色显著性 × 中心高斯先验。"""
    border = np.concatenate([
        rgb01[:8].reshape(-1, 3), rgb01[-8:].reshape(-1, 3),
        rgb01[:, :8].reshape(-1, 3), rgb01[:, -8:].reshape(-1, 3)])
    bg = border.mean(axis=0)
    dist = np.sqrt(((rgb01 - bg[None, None, :]) ** 2).sum(-1))
    yy = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    center = np.exp(-(((xx - 0.5) ** 2) * 4.0 + ((yy - 0.5) ** 2) * 4.0))
    sal = dist * (0.35 + 0.65 * center)
    mx = float(sal.max())
    return (sal / mx if mx > 1e-6 else sal).astype(np.float32)


def _mask_subject(h, w, p, rgb01, pil_img, invert_kind=False):
    """AI 主体分割（u²-netp）；模型不可用 → 启发式回退。background = 1 − subject。"""
    sal = None
    if pil_img is not None:
        try:
            from segment_models import get_subject_mask
            sal = get_subject_mask(pil_img)
        except Exception:
            sal = None
    if sal is None:
        sal = _subject_heuristic(h, w, rgb01)
    if invert_kind:
        sal = 1.0 - sal
    return np.clip(sal, 0.0, 1.0).astype(np.float32)


def _mask_components(m):
    """统一组件列表（P4-1 多组件组合）：新格式 components[]；旧格式顶层
    type/params 视为单个 add 组件——两侧向后兼容。"""
    comps = m.get("components")
    if isinstance(comps, list) and comps:
        return [c for c in comps if isinstance(c, dict)]
    return [{"type": m.get("type"), "params": m.get("params") or {}, "mode": "add"}]


def _component_mask(h, w, comp, rgb01, pil_img=None):
    t = comp.get("type")
    p = comp.get("params") or {}
    if t == "linear":
        mask = _mask_linear(h, w, p)
    elif t == "radial":
        mask = _mask_radial(h, w, p)
    elif t == "brush":
        mask = _mask_brush(h, w, p)
    elif t == "luminance":
        mask = _mask_luminance(h, w, p, rgb01)
    elif t == "color":
        mask = _mask_color(h, w, p, rgb01)
    elif t == "sky":
        mask = _mask_sky(h, w, p, rgb01)
    elif t == "subject":
        mask = _mask_subject(h, w, p, rgb01, pil_img)
    elif t == "background":
        mask = _mask_subject(h, w, p, rgb01, pil_img, invert_kind=True)
    else:
        return None
    if t in _SOFT_TYPES:  # 逐像素蒙版末端轻羽化（约 0.4% 边长）
        radius = max(1.0, min(h, w) * 0.004)
        soft = Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8))
        soft = soft.filter(ImageFilter.GaussianBlur(radius))
        mask = np.asarray(soft, np.float32) / 255.0
    return mask


def _mask_array(h, w, m, rgb01, pil_img=None):
    """多组件组合：首个组件为基底，后续按 mode 合成——
    add=并集(max)，sub=减去(mask·(1-c))，inter=相交(mask·c)。（LR 语义）"""
    mask = None
    for i, comp in enumerate(_mask_components(m)):
        cm = _component_mask(h, w, comp, rgb01, pil_img)
        if cm is None:
            continue
        if mask is None or i == 0:
            mask = cm
            continue
        mode = str(comp.get("mode", "add")).lower()
        if mode == "sub":
            mask = mask * (1.0 - cm)
        elif mode == "inter":
            mask = mask * cm
        else:
            mask = np.maximum(mask, cm)
    if mask is None:
        return None
    comps = _mask_components(m)
    if comps and all(c.get("type") in _SOFT_TYPES for c in comps):
        radius = max(1.0, min(h, w) * 0.004)
        soft = Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8))
        soft = soft.filter(ImageFilter.GaussianBlur(radius))
        mask = np.asarray(soft, np.float32) / 255.0
    return mask


def _apply_local_adjustments(img, adj):
    """11 项局部调整——与全局滑杆同一套函数语义（供蒙版混合）。"""
    from PIL import ImageEnhance

    ev = float(adj.get("exposure", 0.0) or 0.0)
    if abs(ev) > 1e-3:
        img = ImageEnhance.Brightness(img).enhance(max(0.01, 1.0 + ev * 0.8))
    ct = float(adj.get("contrast", 1.0) or 1.0)
    if abs(ct - 1.0) > 1e-3:
        img = ImageEnhance.Contrast(img).enhance(max(0.0, ct))
    sat = float(adj.get("saturation", 1.0) or 1.0)
    if abs(sat - 1.0) > 1e-3:
        img = ImageEnhance.Color(img).enhance(max(0.0, sat))
    warmth = float(adj.get("warmth", 0.0) or 0.0)
    if abs(warmth) > 1e-3:
        arr = np.asarray(img, np.float32)
        r, b = arr[..., 0], arr[..., 2]
        if warmth > 0:
            r = np.clip(r * (1.0 + 0.3 * warmth), 0, 255)
            b = np.clip(b * (1.0 - 0.3 * warmth), 0, 255)
        else:
            b = np.clip(b * (1.0 + 0.3 * (-warmth)), 0, 255)
            r = np.clip(r * (1.0 - 0.3 * (-warmth)), 0, 255)
        arr[..., 0], arr[..., 2] = r, b
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    sh = float(adj.get("shadows", 0.0) or 0.0)
    hl = float(adj.get("highlights", 0.0) or 0.0)
    if abs(sh) > 1e-3 or abs(hl) > 1e-3:
        arr = np.asarray(img, np.float32) / 255.0
        lum = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
        sm = np.clip(1.0 - lum * 2.0, 0, 1)
        hm = np.clip(lum * 2.0 - 1.0, 0, 1)
        if abs(sh) > 1e-3:
            arr = np.clip(arr + sh * 0.5 * sm[..., None], 0, 1)
        if abs(hl) > 1e-3:
            arr = np.clip(arr - hl * 0.5 * hm[..., None], 0, 1)
        img = Image.fromarray((arr * 255.0).astype(np.uint8))
    sp = float(adj.get("sharpness", 0.0) or 0.0)
    if sp > 1e-3:
        img = img.filter(ImageFilter.UnsharpMask(
            radius=max(0.5, sp * 2.0), percent=int(sp * 150), threshold=2))
    from photo_s import grade as _g
    for key, fn in (("clarity", _g.apply_clarity), ("texture", _g.apply_texture),
                    ("dehaze", _g.apply_dehaze), ("vibrance", _g.apply_vibrance)):
        v = float(adj.get(key, 0.0) or 0.0)
        if abs(v) > 1e-3:
            img = fn(img, v)
    return img


def apply_masks(img, masks):
    """逐蒙版顺序应用：局部调整图 × 蒙版 与原图线性混合（LR 语义）。"""
    if not masks or not isinstance(masks, list):
        return img
    out = img
    if out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    for m in masks:
        try:
            if not isinstance(m, dict):
                continue
            comps = _mask_components(m)  # 新格式 components / 旧格式顶层 type 均合法
            if not (m.get("type") in MASK_TYPES
                    or any(c.get("type") in MASK_TYPES for c in comps)):
                continue
            adj = {}
            for k, v in (m.get("adjustments") or {}).items():
                if k not in ADJ_DEFAULTS:
                    continue
                v = float(v)
                if abs(v - ADJ_DEFAULTS[k]) > 1e-4:
                    adj[k] = v
            if not adj:
                continue
            amt_raw = m.get("amount", 1.0)
            amount = min(2.0, max(0.0, float(1.0 if amt_raw is None else amt_raw)))
            if amount <= 1e-3:
                continue
            if abs(amount - 1.0) > 1e-3:
                adj = {k: ADJ_DEFAULTS[k] + (v - ADJ_DEFAULTS[k]) * amount
                       for k, v in adj.items()}
            w, h = out.size
            # 任一组件是逐像素型（亮度/颜色/天空/主体/背景）就需要当前像素做蒙版
            rgb01 = None
            if any(c.get("type") in _PIXEL_TYPES for c in comps):
                rgb01 = np.asarray(out.convert("RGB"), np.float32) / 255.0
            mask = _mask_array(h, w, m, rgb01, out)
            if mask is None:
                continue
            if bool(m.get("invert")):
                mask = 1.0 - mask
            base = np.asarray(out.convert("RGB"), np.float32)
            adjusted = np.asarray(
                _apply_local_adjustments(out, adj).convert("RGB"), np.float32)
            blended = base * (1.0 - mask[..., None]) + adjusted * mask[..., None]
            out = Image.fromarray(np.clip(blended, 0.0, 255.0).astype(np.uint8))
        except Exception:
            continue
    return out
