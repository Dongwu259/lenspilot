"""editor_render.py — 编辑器渲染引擎（P3-9 自 photo_editor.py 拆出）

编辑参数默认值 + 非破坏性渲染管线（几何段/像素段）+ sidecar 读写 +
P1-2 预览分级缓存（解码层/几何层 LRU）。纯引擎：不含任何 IPC 命令
（命令层见 photo_editor.py，经 command_registry 登记并 re-export 本模块）。
"""

import os
import json
import io
import base64
import threading
from PIL import Image, ImageEnhance, ImageFilter
import numpy as np

from model_loader import _load_image_rgb
from service import send_log


# ── Sidecar path helpers ──

def _edit_path(img_path):
    """Sidecar JSON file for edit parameters."""
    return img_path + ".edit.json"


def _thumb_path(img_path, suffix="_edited"):
    """Generate an edited thumbnail path distinct from original."""
    import hashlib
    dirname = os.path.dirname(img_path)
    key = hashlib.md5(img_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join(dirname, f".thumb_{key}{suffix}.jpg")


# ── Edit params default ──

DEFAULT_EDITS = {
    "exposure": 0.0,     # -3.0 .. +3.0
    "contrast": 1.0,     # 0.0 .. 2.0
    "saturation": 1.0,   # 0.0 .. 2.0
    "warmth": 0.0,       # -1.0 (cool) .. +1.0 (warm)
    "shadows": 0.0,      # -1.0 .. +1.0
    "highlights": 0.0,   # -1.0 .. +1.0
    "sharpness": 0.0,    # 0.0 .. 2.0
    "rotation": 0,       # 0 / 90 / 180 / 270
    "flip_h": False,
    "flip_v": False,
    "crop": None,        # [x, y, w, h] or None
    "rotate_deg": 0.0,   # 任意角旋转(水平校正) -45..+45 度,正 = 顺时针;0 = 关(expand 画布+黑边,配合裁剪)
    # ── PhotoS 增强（惰性委托，缺失/失败静默跳过，不影响原有编辑） ──
    "wb_temp": 0,        # 白平衡色温 Kelvin；0 = 关
    "wb_reference": "",  # 灰卡参考图路径；空 = 关（与 wb_temp 同时给时参考图优先）
    "lut_file": "",      # .cube LUT 路径；空 = 关
    "log_curve": "",     # LOG 还原曲线: SLOG3/CLOG3/LOGC3/DLOG/VLOG/HLG；空 = 关
    "denoise": 0,        # 降噪强度 0-20；0 = 关（≥15 时优先 SCUNet 强降噪）
    "auto_levels": False,      # 自动色阶（2% 剪裁直方图拉伸）
    "auto_exposure": 0,        # 自动曝光目标亮度 0-1；0 = 关（0.45 ≈ 中性灰）
    "grayscale": False,        # 黑白转换
    "sepia": False,            # 复古 sepia 色调
    "srgb": False,             # 打 sRGB ICC 色彩管理标记
    "auto_straighten": False,  # 自动扶正地平线（opencv，置信度门控）
    # ── PhotoS v1.6 调色（grade.py，LR 向；惰性委托，缺失/失败静默跳过） ──
    "wb_tint": 0,        # 白平衡 G/M 轴 -100(绿)..+100(品红)；0 = 关
    "levels": None,      # 手动色阶 [black, white, gamma]；None = 关
    "curves": None,      # 点曲线 {"rgb": [[x,y],...], "r"/"g"/"b": [...]}，0-255；None = 关
    "clarity": 0.0,      # 清晰度（大半径局部对比）-1..1；0 = 关
    "texture": 0.0,      # 纹理（小半径细节）-1..1；0 = 关
    "dehaze": 0.0,       # 去雾 -1..1（负 = 加雾）；0 = 关
    "vibrance": 0.0,     # 自然饱和度 -1..1；0 = 关
    "hsl": None,         # HSL 分色 {"red": [dh,ds,dl], ... 8 色域}；None = 关
    "color_grading": None,  # 三向分级 {"shadows"/"midtones"/"highlights": [hue,sat,lum?]}；None = 关
    "vignette": None,    # 暗角 [amount, midpoint, feather]；None = 关
    "grain": None,       # 胶片颗粒 [amount, size]；None = 关
    "masks": None,       # 局部调整蒙版（P1-3，见 local_masks.py）；None = 关
    # [{id, type: linear|radial|brush|luminance|color|sky, invert, amount,
    #   params: {...0-1 相对坐标}, adjustments: {非默认局部调整项},
    #   components: [{type, params, mode: add|sub|inter}]（多组件组合，P4）}]
    "spots": None,       # 污点修复/仿制（P4，见 local_spots.py）；None = 关
    # [{id, mode: heal|clone, x, y, r[, sx, sy]}] 坐标 0-1，r 相对短边
    "redeye": None,      # 红眼修正（P4）；[{id, x, y, r}]
    "optics": None,      # 镜头校正（P4，见 optics_fix.py）；None = 关
    # {"distortion": -100..100, "ca": -100..100, "vig": -100..100}
    "calibration": None,  # 相机校准（P6，见 calibration.py）；None = 关
    # {"shadows_tint","r_hue","r_sat","g_hue","g_sat","b_hue","b_sat": -100..100}
    # ── PhotoS v2.5 AI 自动修图（auto-tone 预测参数快照；None = 关） ──
    # 结构: {"engine": "auto"|"suggest", "options": {9 字段}, "confidence": 0-1,
    #        "strength": 0-1}——存预测快照而非开关,导出/重放零重推理；
    # 应用经 photo_s.autotone.apply_auto_tone_params(纯 adjust/grade,CPU 也可)。
    "auto_tone": None,
}


# ── Core editing functions ──

class OperationCancelled(Exception):
    """Raised by the render pipeline when a batch task is cancelled mid-file."""
    pass


def _check_cancel(cancel_check):
    if cancel_check and cancel_check():
        raise OperationCancelled()


def apply_edits(pil_img, edits, cancel_check=None):
    """Apply a dict of edits to a PIL Image. Returns modified PIL Image.

    cancel_check is an optional zero-arg callable; it is polled between
    pipeline stages so a batch cancel can interrupt a single large file
    (decode / geometry / each pixel stage) instead of waiting for the next file.
    """
    _check_cancel(cancel_check)
    geom = _apply_geometry(pil_img, edits, cancel_check)
    _check_cancel(cancel_check)
    return _apply_pixel(geom, edits, cancel_check)


def _apply_geometry(pil_img, edits, cancel_check=None):
    """几何段（P1-2 分级渲染第 1 级）：旋转/翻转/任意角/裁剪。返回新图。"""
    _check_cancel(cancel_check)
    img = pil_img.copy()

    # ── Rotation / flip ──
    rot = edits.get("rotation", 0)
    if rot == 90:
        img = img.transpose(Image.ROTATE_270)
    elif rot == 180:
        img = img.transpose(Image.ROTATE_180)
    elif rot == 270:
        img = img.transpose(Image.ROTATE_90)
    if edits.get("flip_h"):
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if edits.get("flip_v"):
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    _check_cancel(cancel_check)

    # ── 任意角旋转(水平校正,expand 画布黑边;先于裁剪,裁剪坐标即旋转后画布空间) ──
    rdeg = edits.get("rotate_deg", 0) or 0
    if abs(rdeg) > 0.01:
        try:
            from photo_s.adjust import apply_rotate
            img = apply_rotate(img, float(rdeg))
        except Exception:
            pass
    _check_cancel(cancel_check)

    # ── Crop ──
    crop = edits.get("crop")
    if crop and len(crop) == 4:
        try:
            x, y, w, h = int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3])
            if w > 0 and h > 0:
                img = img.crop((x, y, x + w, y + h))
        except (ValueError, TypeError):
            pass
    _check_cancel(cancel_check)

    return img


def _apply_pixel(pil_img, edits, cancel_check=None):
    """像素段（P1-2 分级渲染第 2 级）：光学 → auto_tone → 基础调整 → PhotoS 链。

    几何之后的所有步骤；所有操作均返回新图、不原地修改入参。
    cancel_check 在每个大阶段之间轮询，支持单文件中的低层取消。
    """
    _check_cancel(cancel_check)
    img = pil_img

    # ── 镜头校正（P4-3）：光学缺陷先修，再谈影调 ──
    try:
        optics = edits.get("optics")
        if optics and isinstance(optics, dict):
            from optics_fix import apply_optics
            img = apply_optics(img, optics)
    except Exception:
        pass

    # ── AI 自动修图（auto-tone 预测参数快照；在手动调整之前,手动项可叠加其上） ──
    auto_tone = edits.get("auto_tone")
    if isinstance(auto_tone, dict) and auto_tone.get("options"):
        try:
            from photo_s.autotone import apply_auto_tone_params
            img = apply_auto_tone_params(img, auto_tone)
        except Exception:
            pass  # 与其余 PhotoS 委托同策略:应用失败不崩预览/导出

    # ── Exposure (gamma-inspired curve via power) ──
    ev = edits.get("exposure", 0.0)
    if abs(ev) > 0.001:
        # exposure: +1.0 = 2x brighter (gamma 0.5), -1.0 = 2x darker (gamma 2.0)
        factor = 1.0 + ev * 0.8
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(max(0.01, factor))

    # ── Contrast ──
    contrast = edits.get("contrast", 1.0)
    if abs(contrast - 1.0) > 0.001:
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(max(0.0, contrast))

    # ── Saturation ──
    sat = edits.get("saturation", 1.0)
    if abs(sat - 1.0) > 0.001:
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(max(0.0, sat))

    # ── Warmth (color temperature via numpy channel adjustment) ──
    warmth = edits.get("warmth", 0.0)
    if abs(warmth) > 0.001:
        arr = np.array(img, dtype=np.float32)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        if warmth > 0:
            r = np.clip(r * (1.0 + warmth * 0.3), 0, 255)
            b = np.clip(b * (1.0 - warmth * 0.3), 0, 255)
        else:
            b = np.clip(b * (1.0 + abs(warmth) * 0.3), 0, 255)
            r = np.clip(r * (1.0 - abs(warmth) * 0.3), 0, 255)
        arr[:, :, 0], arr[:, :, 2] = r, b
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    # ── Shadows / highlights (numpy tone mapping) ──
    shadows = edits.get("shadows", 0.0)
    highlights = edits.get("highlights", 0.0)
    if abs(shadows) > 0.001 or abs(highlights) > 0.001:
        arr = np.array(img, dtype=np.float32) / 255.0
        # Mask: dark pixels (shadows) and bright pixels (highlights)
        lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        shadow_mask = np.clip(1.0 - lum * 2.0, 0, 1)
        highlight_mask = np.clip(lum * 2.0 - 1.0, 0, 1)
        if abs(shadows) > 0.001:
            arr = np.clip(arr + shadows * 0.5 * shadow_mask[:, :, None], 0, 1)
        if abs(highlights) > 0.001:
            arr = np.clip(arr - highlights * 0.5 * highlight_mask[:, :, None], 0, 1)
        img = Image.fromarray((arr * 255).astype(np.uint8))

    # ── Sharpness ──
    sharp = edits.get("sharpness", 0.0)
    if sharp > 0.001:
        # Use unsharp mask for realistic sharpening
        radius = max(0.5, sharp * 2.0)
        amount = int(sharp * 150)
        img = img.filter(ImageFilter.UnsharpMask(radius=radius, percent=amount, threshold=2))
    _check_cancel(cancel_check)

    # ── PhotoS 增强（懒加载委托；引擎缺失/异常静默返回原图，绝不崩预览/导出） ──
    # 顺序参照 photo_s 管线: LOG还原 → LUT → 白平衡 → 降噪 → 自动色阶 → 自动扶正
    try:
        log_curve = str(edits.get("log_curve", "") or "").strip()
        if log_curve:
            from photo_s.logcurve import apply_log_recovery
            img = apply_log_recovery(img, log_curve)
    except Exception:
        pass

    try:
        lut_file = str(edits.get("lut_file", "") or "").strip()
        if lut_file and os.path.exists(lut_file):
            from photo_s.lut import apply_lut
            img = apply_lut(img, lut_file)
    except Exception:
        pass

    try:
        wb_ref = str(edits.get("wb_reference", "") or "").strip()
        wb_temp = edits.get("wb_temp", 0) or 0
        wb_tint = edits.get("wb_tint", 0) or 0
        if wb_ref or wb_temp or wb_tint:
            from photo_s.adjust import apply_white_balance
            img = apply_white_balance(
                img,
                reference=wb_ref or None,
                temp=float(wb_temp) if wb_temp else None,
                tint=float(wb_tint))
    except Exception:
        pass

    try:
        # 黑白 / 复古 sepia（tone 转换，先于降噪，让降噪作用于最终色调）
        if edits.get("grayscale") or edits.get("sepia"):
            from photo_s.adjust import apply_tone_adjustments
            img = apply_tone_adjustments(
                img, grayscale=bool(edits.get("grayscale")),
                sepia=bool(edits.get("sepia")))
    except Exception:
        pass

    # ── 相机校准（P6-3）：原色先定，再做影调 ──
    try:
        calibration = edits.get("calibration")
        if calibration and isinstance(calibration, dict):
            from calibration import apply_calibration
            img = apply_calibration(img, calibration)
    except Exception:
        pass
    _check_cancel(cancel_check)

    # ── PhotoS v1.6 调色（grade.py）：色阶→曲线→清晰度/纹理→去雾→自然饱和→HSL→三向分级 ──
    # 顺序对齐上游引擎：WB/曝光之后、降噪之前；vignette/grain 放在降噪/auto_levels 之后
    try:
        levels = edits.get("levels")
        if levels and len(levels) >= 2:
            from photo_s.grade import apply_levels
            img = apply_levels(
                img, black=float(levels[0]), white=float(levels[1]),
                gamma=float(levels[2]) if len(levels) > 2 else 1.0)
    except Exception:
        pass

    try:
        curves = edits.get("curves")
        if curves and isinstance(curves, dict):
            from photo_s.grade import apply_curves
            img = apply_curves(img, curves)
    except Exception:
        pass

    try:
        clarity = edits.get("clarity", 0.0) or 0.0
        if abs(clarity) > 0.001:
            from photo_s.grade import apply_clarity
            img = apply_clarity(img, float(clarity))
    except Exception:
        pass

    try:
        texture = edits.get("texture", 0.0) or 0.0
        if abs(texture) > 0.001:
            from photo_s.grade import apply_texture
            img = apply_texture(img, float(texture))
    except Exception:
        pass

    try:
        dehaze = edits.get("dehaze", 0.0) or 0.0
        if abs(dehaze) > 0.001:
            from photo_s.grade import apply_dehaze
            img = apply_dehaze(img, float(dehaze))
    except Exception:
        pass

    try:
        vibrance = edits.get("vibrance", 0.0) or 0.0
        if abs(vibrance) > 0.001:
            from photo_s.grade import apply_vibrance
            img = apply_vibrance(img, float(vibrance))
    except Exception:
        pass

    try:
        hsl = edits.get("hsl")
        if hsl and isinstance(hsl, dict):
            from photo_s.grade import apply_hsl
            img = apply_hsl(img, hsl)
    except Exception:
        pass

    try:
        cg = edits.get("color_grading")
        if cg and isinstance(cg, dict):
            from photo_s.grade import apply_color_grading
            img = apply_color_grading(
                img, shadows=cg.get("shadows"),
                midtones=cg.get("midtones"),
                highlights=cg.get("highlights"))
    except Exception:
        pass
    _check_cancel(cancel_check)

    try:
        denoise = edits.get("denoise", 0) or 0
        if denoise > 0:
            # 强降噪(≥15)优先 SCUNet(onnxruntime)，失败/离线回退 NLM
            if denoise >= 15:
                try:
                    from scunet_denoise import apply_scunet
                    img = apply_scunet(img, strength=float(denoise))
                except Exception:
                    from photo_s.denoise import apply_denoise
                    img = apply_denoise(img, strength=float(denoise))
            else:
                from photo_s.denoise import apply_denoise
                img = apply_denoise(img, strength=float(denoise))
    except Exception:
        pass

    try:
        if edits.get("auto_levels"):
            from photo_s.adjust import apply_auto_levels
            img = apply_auto_levels(img)
    except Exception:
        pass

    # ── 污点修复/仿制/红眼（P4-2）：先清瑕疵再调色 ──
    try:
        spots = edits.get("spots")
        redeye = edits.get("redeye")
        if (spots and isinstance(spots, list)) or (redeye and isinstance(redeye, list)):
            from local_spots import apply_spots
            img = apply_spots(img, spots, redeye)
    except Exception:
        pass
    _check_cancel(cancel_check)

    # ── 局部调整蒙版（P1-3）：全局链之后、风格化(暗角/颗粒)之前 ──
    try:
        masks = edits.get("masks")
        if masks and isinstance(masks, list):
            from local_masks import apply_masks
            img = apply_masks(img, masks)
    except Exception:
        pass
    _check_cancel(cancel_check)

    # ── 收尾风格化（vignette/grain，对齐上游：降噪/auto_levels 之后、几何之前） ──
    try:
        vignette = edits.get("vignette")
        if vignette and len(vignette) >= 1 and abs(float(vignette[0])) > 0.001:
            from photo_s.grade import apply_vignette
            img = apply_vignette(
                img, amount=float(vignette[0]),
                midpoint=float(vignette[1]) if len(vignette) > 1 else 0.5,
                feather=float(vignette[2]) if len(vignette) > 2 else 0.5)
    except Exception:
        pass

    try:
        grain = edits.get("grain")
        if grain and len(grain) >= 1 and abs(float(grain[0])) > 0.001:
            from photo_s.grade import apply_grain
            img = apply_grain(
                img, amount=float(grain[0]),
                size=float(grain[1]) if len(grain) > 1 else 1.0)
    except Exception:
        pass
    _check_cancel(cancel_check)

    try:
        # 自动曝光: 目标亮度归一化（0 = 关）
        auto_exp = edits.get("auto_exposure", 0) or 0
        if auto_exp:
            from photo_s.adjust import apply_exposure
            img = apply_exposure(img, auto_exposure=float(auto_exp))
    except Exception:
        pass

    try:
        # sRGB 色彩管理标记（交付前统一色域）
        if edits.get("srgb"):
            from photo_s.adjust import apply_color_management
            img = apply_color_management(img, srgb=True)
    except Exception:
        pass

    try:
        if edits.get("auto_straighten"):
            from photo_s.straighten import apply_auto_straighten
            _out, _straightened = apply_auto_straighten(img)  # 返回 (image, bool)
            if _straightened:
                img = _out
    except Exception:
        pass
    _check_cancel(cancel_check)

    return img

def load_edits(img_path):
    """Load persisted edit parameters from sidecar JSON."""
    try:
        ep = _edit_path(img_path)
        if os.path.exists(ep):
            with open(ep, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults to fill any missing keys
            merged = dict(DEFAULT_EDITS)
            merged.update(saved)
            return merged
    except Exception:
        pass
    return dict(DEFAULT_EDITS)


def save_edits(img_path, edits):
    """Persist edit parameters to sidecar JSON."""
    try:
        ep = _edit_path(img_path)
        # Only save non-default values to keep file small
        to_save = {k: v for k, v in edits.items()
                   if k in DEFAULT_EDITS and v != DEFAULT_EDITS.get(k)}
        if to_save:
            tmp = ep + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ep)
        elif os.path.exists(ep):
            os.remove(ep)  # all defaults → delete sidecar
        return True
    except Exception as e:
        send_log(f"save_edits failed: {e}", "warn")
        return False


def has_edits(img_path):
    """Check if a photo has non-default edits."""
    return os.path.exists(_edit_path(img_path))

# ── P1-2 预览分级缓存：解码层 + 几何层 ──
# 滑杆迟滞的大头是 RAW 内嵌预览解码与几何重排；按参数组缓存这两级后，
# 色调滑杆拖动只重算 _apply_pixel。LRU 上限 4 张；文件指纹(mtime+size)
# 变化即整条失效，绝不返回过期像素。

_PV_CACHE = {}            # path -> {mtime, size, scale, base, geom_key, geom_img}
_PV_CACHE_MAX = 4
# Detail renders are expensive (decode at up to 6k + full pixel pipeline), so
# cache the finished JPEG by path/mtime/max_side/edits. Interactive 800px
# previews keep their own decode/geometry caches above.
_DETAIL_CACHE = {}        # (path, mtime, size, max_side, edits_json) -> (data_url, edits)
_DETAIL_CACHE_MAX = 6
_pv_lock = threading.Lock()
_pv_stats = {"decode_hit": 0, "decode_miss": 0, "geom_hit": 0, "geom_miss": 0}


def _pv_file_fingerprint(path):
    try:
        st = os.stat(path)
        return int(st.st_mtime), st.st_size
    except OSError:
        return None, None


def _pv_geom_key(full_edits):
    crop = full_edits.get("crop")
    return (full_edits.get("rotation", 0), bool(full_edits.get("flip_h")),
            bool(full_edits.get("flip_v")),
            tuple(int(c) for c in crop) if crop else None,
            round(float(full_edits.get("rotate_deg", 0) or 0), 2))

def _apply_edits_preview(path, edits, max_side=800):
    """预览路径（P1-2 分级缓存）：解码层 → 几何层 → 像素段。

    色调滑杆连动时解码/几何两级命中缓存，只重算像素段；
    crop 坐标按预览比例缩放，保证预览与全尺寸结果一致。

    max_side > 1280 时走“细节预览”路径：跳过缓存、按请求分辨率重新解码，
    供编辑器 1:1 / 高倍放大检查细节；常规 800px 预览仍走缓存以保交互帧率。
    """
    try:
        max_side = max(200, min(12000, int(max_side or 800)))
    except (TypeError, ValueError):
        max_side = 800
    detail = max_side > 1280

    full_edits = load_edits(path)
    full_edits.update(edits)
    mtime, size = _pv_file_fingerprint(path)
    detail_key = None
    if detail:
        detail_key = (path, mtime, size, max_side,
                      json.dumps(full_edits, sort_keys=True, default=str))
        with _pv_lock:
            cached = _DETAIL_CACHE.pop(detail_key, None)
            if cached is not None:
                _DETAIL_CACHE[detail_key] = cached  # LRU touch
                return {"preview": cached[0], "edits": dict(cached[1])}

    if detail:
        # Detail path: decode at requested resolution. Caches are sized for the
        # interactive 1280px preview and must not be reused here.
        img = _load_image_rgb(path)
        if img is None:
            return {"error": "Failed to load image"}
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        base = img
        if scale < 1.0:
            base = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        geom = None
    else:
        with _pv_lock:
            e = _PV_CACHE.get(path) if mtime is not None else None
            if e and e["mtime"] == mtime and e["size"] == size:
                _PV_CACHE[path] = _PV_CACHE.pop(path)  # LRU touch
                scale, base = e["scale"], e["base"]
                _pv_stats["decode_hit"] += 1
            else:
                scale = base = None

        if scale is None:
            img = _load_image_rgb(path)
            if img is None:
                return {"error": "Failed to load image"}
            _max_preview = 1280
            w, h = img.size
            scale = min(1.0, _max_preview / max(w, h))
            base = img
            if scale < 1.0:
                base = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            with _pv_lock:
                _PV_CACHE[path] = {"mtime": mtime, "size": size, "scale": scale,
                                   "base": base, "geom_key": None, "geom_img": None}
                while len(_PV_CACHE) > _PV_CACHE_MAX:
                    _PV_CACHE.pop(next(iter(_PV_CACHE)))
                _pv_stats["decode_miss"] += 1

    # crop 坐标按预览比例缩放（本地副本；sidecar 仍存全尺寸坐标）
    if scale < 1.0:
        crop = full_edits.get("crop")
        if crop and len(crop) == 4:
            full_edits["crop"] = [int(c * scale) for c in crop]

    # 几何层：几何参数组未变 → 复用上次结果（细节路径不复用 1280 缓存）
    gkey = _pv_geom_key(full_edits)
    if not detail:
        with _pv_lock:
            e = _PV_CACHE.get(path)
            geom = e["geom_img"] if (e and e["geom_key"] == gkey
                                     and e["geom_img"] is not None) else None
        if geom is not None:
            _pv_stats["geom_hit"] += 1
    if geom is None:
        geom = _apply_geometry(base, full_edits)
        if not detail:
            with _pv_lock:
                e = _PV_CACHE.get(path)
                if e:
                    e["geom_key"] = gkey
                    e["geom_img"] = geom
                _pv_stats["geom_miss"] += 1

    # 像素段：copy 隔离缓存对象（像素操作均返回新图，copy 是防御性兜底）
    result = _apply_pixel(geom.copy(), full_edits)
    result.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=(86 if detail else 75))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    preview = "data:image/jpeg;base64," + b64
    if detail and detail_key is not None:
        with _pv_lock:
            _DETAIL_CACHE[detail_key] = (preview, dict(full_edits))
            while len(_DETAIL_CACHE) > _DETAIL_CACHE_MAX:
                _DETAIL_CACHE.pop(next(iter(_DETAIL_CACHE)))
    return {"preview": preview, "edits": full_edits}


# ── Preset card wall: one decode + geometry pass, then a tiny pixel pass per preset ──
# Cards are ~180px; the work image is downscaled to 2x before the pixel stage
# (masks/curves use 0-1 relative coordinates) so a 19-preset wall stays fast.
_PRESET_THUMB_CACHE = {}
_PRESET_THUMB_CACHE_MAX = 4
_PRESET_GEOMETRY_KEYS = ("rotation", "flip_h", "flip_v", "crop", "rotate_deg")


def render_preset_thumbnails(path, base_edits, presets, max_side=180):
    """Render one small preview per preset from a single decode.

    base_edits is the photo's current edit state; preset edits are expected to
    be already amount-scaled by the caller. Geometry is baked once into a shared
    work image, so preset thumbnails never re-crop or rotate the target photo.
    """
    try:
        max_side = max(96, min(320, int(max_side or 180)))
    except (TypeError, ValueError):
        max_side = 180
    presets = [p for p in (presets or [])
               if isinstance(p, dict) and p.get("id")]
    if not presets:
        return {"thumbs": []}

    mtime, size = _pv_file_fingerprint(path)
    key = (path, mtime, size, max_side,
           json.dumps(base_edits or {}, sort_keys=True, default=str),
           json.dumps([(p.get("id"), p.get("edits") or {},
                        p.get("reference_stats"), p.get("strength"))
                       for p in presets], sort_keys=True, default=str))
    with _pv_lock:
        cached = _PRESET_THUMB_CACHE.pop(key, None)
        if cached is not None:
            _PRESET_THUMB_CACHE[key] = cached  # LRU touch
            return {"thumbs": cached}

    # Decode layer: reuse the 1280px interactive base when possible.
    scale = None
    with _pv_lock:
        e = _PV_CACHE.get(path) if mtime is not None else None
        if e and e["mtime"] == mtime and e["size"] == size:
            _PV_CACHE[path] = _PV_CACHE.pop(path)
            scale, base_img = e["scale"], e["base"]
    if scale is None:
        img = _load_image_rgb(path)
        if img is None:
            return {"error": "Failed to load image"}
        w, h = img.size
        scale = min(1.0, 1280 / max(w, h)) if max(w, h) else 1.0
        base_img = img
        if scale < 1.0:
            base_img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                  Image.LANCZOS)
        with _pv_lock:
            _PV_CACHE[path] = {"mtime": mtime, "size": size, "scale": scale,
                               "base": base_img, "geom_key": None, "geom_img": None}
            while len(_PV_CACHE) > _PV_CACHE_MAX:
                _PV_CACHE.pop(next(iter(_PV_CACHE)))

    full = load_edits(path)
    full.update(base_edits or {})
    if scale < 1.0:
        crop = full.get("crop")
        if crop and len(crop) == 4:
            full["crop"] = [int(c * scale) for c in crop]

    # Geometry is shared by every card; presets never move geometry.
    geom = _apply_geometry(base_img, full)
    work = geom.copy()
    work.thumbnail((max_side * 2, max_side * 2), Image.LANCZOS)

    thumbs = []
    for p in presets:
        merged = dict(full)
        preset_edits = p.get("edits")
        if not isinstance(preset_edits, dict):
            preset_edits = {}
        # Reference presets carry a target stats card instead of fixed values;
        # fit them to this photo on the same geometry-only work image the
        # single-photo reference match uses.
        ref_stats = p.get("reference_stats")
        if isinstance(ref_stats, dict) and ref_stats:
            try:
                from reference_match import suggest_edits
                try:
                    strength = float(p.get("strength") or 1.0)
                except (TypeError, ValueError):
                    strength = 1.0
                preset_edits, _ = suggest_edits(work, ref_stats, strength)
            except Exception as e:
                send_log(f"preset reference thumb failed ({p.get('id')}): {e}", "warn")
                preset_edits = {}
        for k, v in preset_edits.items():
            if k in _PRESET_GEOMETRY_KEYS:
                continue  # geometry already baked into `work`
            merged[k] = v
        try:
            out = _apply_pixel(work.copy(), merged)
            out.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=74)
            thumbs.append({"id": p.get("id"),
                           "preview": "data:image/jpeg;base64," +
                                      base64.b64encode(buf.getvalue()).decode("ascii")})
        except Exception as e:
            send_log(f"preset thumbnail failed ({p.get('id')}): {e}", "warn")
            thumbs.append({"id": p.get("id"), "preview": ""})
    with _pv_lock:
        _PRESET_THUMB_CACHE[key] = thumbs
        while len(_PRESET_THUMB_CACHE) > _PRESET_THUMB_CACHE_MAX:
            _PRESET_THUMB_CACHE.pop(next(iter(_PRESET_THUMB_CACHE)))
    return {"thumbs": thumbs}


def render_edit_thumbnail(pil_img, edits, max_side=1024, cancel_check=None):
    """缩略图级渲染（批量保存刷新缩略图专用，免全尺寸像素管线）：
    解码后先限长边再跑 apply_edits，crop 坐标等比缩放（与 _apply_edits_preview
    同口径）；masks/spots 等局部坐标均为 0-1 相对坐标，天然尺度无关。
    返回 (缩略图级 PIL Image, 实际缩放比)。"""
    w, h = pil_img.size
    scale = min(1.0, max_side / max(w, h)) if max(w, h) else 1.0
    if scale < 1.0:
        img = pil_img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.LANCZOS)
        edits = dict(edits)
        crop = edits.get("crop")
        if crop and len(crop) == 4:
            edits["crop"] = [max(0, int(c * scale)) for c in crop]
    else:
        img = pil_img
    return apply_edits(img, edits, cancel_check=cancel_check), scale
