"""local_spots.py — 污点修复 / 仿制 / 红眼（P4-2，LightCraft Remove/Heal 路线的轻量版）

spots 协议（photo_editor.DEFAULT_EDITS["spots"]，None = 关）：
  [{"id": 1, "mode": "heal"|"clone", "x": 0.32, "y": 0.5, "r": 0.03,
    "sx": 0.40, "sy": 0.5}]    # 坐标 0-1 相对图幅；r 相对短边；clone 带 sx/sy 源点

redeye 协议（DEFAULT_EDITS["redeye"]，None = 关）：
  [{"id": 1, "x": 0.5, "y": 0.4, "r": 0.05}]

算法：
  heal  全部修复点合成一张软边 mask，一次 cv2.inpaint(TELEA)；无 opencv 回退
        「环形中值填充」（取 1.0r–1.6r 环带中值色软混合，对灰尘/小瑕疵足够）。
  clone 从源点裁同半径软边贴片羽化覆盖目标。
  redeye 圆内 R 通道显著高于 G/B 的像素压为 G/B 均值的暗部（保留瞳孔纹理）。

应用位置：全局影调链之后、蒙版之前（先清瑕疵再调色）。
"""

import numpy as np


def _soft_disk(h, w, cx_px, cy_px, r_px, hardness=0.65):
    """bbox 内的软边圆盘 mask（0-1 float32），圆外为空数组由调用方裁剪。"""
    x0 = max(0, int(cx_px - r_px) - 1)
    x1 = min(w, int(cx_px + r_px) + 2)
    y0 = max(0, int(cy_px - r_px) - 1)
    y1 = min(h, int(cy_px + r_px) + 2)
    if x0 >= x1 or y0 >= y1:
        return None, None
    xs = np.arange(x0, x1, dtype=np.float32) + 0.5
    ys = np.arange(y0, y1, dtype=np.float32) + 0.5
    d = np.sqrt((xs[None, :] - cx_px) ** 2 + (ys[:, None] - cy_px) ** 2) / max(1e-3, r_px)
    m = np.clip((1.0 - d) / max(1e-3, 1.0 - hardness), 0.0, 1.0)
    m = (m * m * (3.0 - 2.0 * m)).astype(np.float32)
    return m, (x0, y0, x1, y1)


def _ring_median_fill(arr, cx_px, cy_px, r_px):
    """无 opencv 回退：1.05r–1.6r 环带的中值色作为填充色。"""
    h, w = arr.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.sqrt((xs - cx_px) ** 2 + (ys - cy_px) ** 2) / max(1e-3, r_px)
    ring = (d > 1.02) & (d < 1.6)
    if not ring.any():
        return np.array([128, 128, 128], np.float32)
    return np.median(arr[ring], axis=0).astype(np.float32)


def apply_spots(img, spots=None, redeye=None):
    spots = [s for s in (spots or []) if isinstance(s, dict)]
    redeye = [e for e in (redeye or []) if isinstance(e, dict)]
    if not spots and not redeye:
        return img

    out = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    if out.mode != "RGB":
        out = out.convert("RGB")
    arr = np.asarray(out).copy()
    h, w = arr.shape[:2]
    scale = float(min(w, h))

    heal = [s for s in spots if s.get("mode", "heal") != "clone"]
    clones = [s for s in spots if s.get("mode") == "clone"]

    # ── heal：合成 mask → 一次 inpaint（或回退）→ 软边混合 ──
    if heal:
        metas = []
        hard_mask = np.zeros((h, w), np.uint8)
        for s in heal:
            try:
                cx, cy = float(s["x"]) * w, float(s["y"]) * h
                r = max(2.0, float(s.get("r", 0.03)) * scale)
            except (KeyError, TypeError, ValueError):
                continue
            m, bbox = _soft_disk(h, w, cx, cy, r)
            if m is None:
                continue
            x0, y0, x1, y1 = bbox
            hard_mask[y0:y1, x0:x1] = np.maximum(
                hard_mask[y0:y1, x0:x1], (m > 0.25).astype(np.uint8) * 255)
            metas.append((m, bbox, cx, cy, r))
        if metas:
            fixed = None
            try:
                import cv2
                bgr = arr[..., ::-1].copy()
                res = cv2.inpaint(bgr, hard_mask, 5, cv2.INPAINT_TELEA)
                fixed = res[..., ::-1].astype(np.float32)
            except ImportError:
                fixed = None
            for m, (x0, y0, x1, y1), cx, cy, r in metas:
                patch = fixed[y0:y1, x0:x1] if fixed is not None else None
                if patch is None:
                    fill = _ring_median_fill(arr.astype(np.float32), cx, cy, r)
                    patch = np.full((y1 - y0, x1 - x0, 3), fill, np.float32)
                region = arr[y0:y1, x0:x1].astype(np.float32)
                arr[y0:y1, x0:x1] = (region * (1 - m[..., None]) + patch * m[..., None]
                                     ).astype(np.uint8)

    # ── clone：源贴片软边覆盖目标 ──
    for s in clones:
        try:
            tx, ty = float(s["x"]) * w, float(s["y"]) * h
            sx, sy = float(s.get("sx", s["x"])) * w, float(s.get("sy", s["y"])) * h
            r = max(2.0, float(s.get("r", 0.03)) * scale)
        except (KeyError, TypeError, ValueError):
            continue
        m, bbox = _soft_disk(h, w, tx, ty, r)
        if m is None:
            continue
        x0, y0, x1, y1 = bbox
        sx0 = int(round(sx - (tx - x0)))
        sy0 = int(round(sy - (ty - y0)))
        sx1, sy1 = sx0 + (x1 - x0), sy0 + (y1 - y0)
        if sx0 < 0 or sy0 < 0 or sx1 > w or sy1 > h:
            continue  # 源贴片出界 → 跳过（不崩预览）
        region = arr[y0:y1, x0:x1].astype(np.float32)
        patch = arr[sy0:sy1, sx0:sx1].astype(np.float32)
        arr[y0:y1, x0:x1] = (region * (1 - m[..., None]) + patch * m[..., None]
                             ).astype(np.uint8)

    # ── redeye：圆内红色 dominance 像素压暗去饱和 ──
    for e in redeye:
        try:
            cx, cy = float(e["x"]) * w, float(e["y"]) * h
            r = max(2.0, float(e.get("r", 0.05)) * scale)
        except (KeyError, TypeError, ValueError):
            continue
        m, bbox = _soft_disk(h, w, cx, cy, r, hardness=0.3)
        if m is None:
            continue
        x0, y0, x1, y1 = bbox
        region = arr[y0:y1, x0:x1].astype(np.float32)
        R, G, B = region[..., 0], region[..., 1], region[..., 2]
        red = (R > G * 1.3 + 8) & (R > B * 1.3 + 8) & (R > 60)
        sel = red & (m > 0.15)
        if sel.any():
            v = (G + B) * 0.5 * 0.6  # G/B 均值压暗作为新瞳孔色，保留周边纹理
            region[..., 0] = np.where(sel, v, R)
            region[..., 1] = np.where(sel, (G + v) * 0.5, G)
            region[..., 2] = np.where(sel, (B + v) * 0.5, B)
            arr[y0:y1, x0:x1] = region.astype(np.uint8)

    from PIL import Image as _I
    return _I.fromarray(arr)
