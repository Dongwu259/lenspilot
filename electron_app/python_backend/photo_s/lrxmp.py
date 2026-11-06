"""photo_s/lrxmp.py — Lightroom 数据桥接（个人修图数据管线，v1.9.0 阶段 1）

CLI 入口：``photo-s lr-scan [paths...] [--export-dir DIR] [--json]``——自动发现
catalog/XMP、聚合覆盖报告、可选导出训练数据 JSONL（每张已编辑照片的
``options`` = PhotoS 参数即训练标签）。

纯 stdlib 零依赖。四层：

1. :func:`parse_xmp_sidecar` — XMP sidecar 解析（LR 目录勾选「自动写入 XMP」
   后每张 RAW 一个 .xmp，字段名即 ``crs:`` 属性名）
2. :func:`parse_develop_blob` — LR catalog 快照解析（``Adobe_imageDevelopSettings.text``
   列，明文 ``s = { key = value }`` 格式，LR 18 实测）
3. :func:`crs_to_options` — crs 字段 → ``ProcessOptions`` kwargs（紧凑字符串，
   含 HSL/曲线/分级/几何蒙版/局部调整/点颜色）
4. :func:`scan_catalog` — SQLite 全目录扫描（settings + 历史步骤 + 文件路径关联，
   关联链已验证：settings.image → Adobe_images → AgLibraryFile → folder → root）

映射覆盖率速查（LR 字段 → PhotoS 字段）：

**直接映射**::

    Temperature          → wb_temp（Kelvin；仅当 white_balance 非 "As Shot"）
    Tint                 → wb_tint（G/M 轴，+ = 品红，LR 同号）
    Exposure2012         → exposure（EV）
    Contrast2012         → contrast = 1 + v/100（倍率）
    Saturation           → saturation = 1 + v/100（倍率）
    Vibrance/Texture/Clarity2012/Dehaze → 同名 /100（[-1, 1]）
    Hue/Saturation/LuminanceAdjustment*8 → hsl 紧凑串（hue*1.8 度，sat/lum /100）
    ToneCurvePV2012[/R/G/B] → curves 紧凑串（点对直传）
    ColorGrade*（3 路 + 每区 L）→ color_grading（hue>180 折到 -180..180，sat/lum /100）
    VignetteAmount/Midpoint → vignette（/100）
    CropLeft/Top/Right/Bottom → crop（相对 0-1 → 像素，需 image_size）
    Mask/CircularGradient、Mask/LinearGradient → masks radial/linear + mask_adjust
      （相对坐标 0-1 直传、Feather/100、MaskInverted → invert；LR 渐变角度按
      顺时针惯例近似；Local* → 11 项标量键）

**近似映射**（语义/数值有损）::

    Highlights2012/Shadows2012/Whites2012/Blacks2012 → curves 折线近似
    Sharpness/SharpenRadius → sharpen（倍率近似）
    LuminanceSmoothing/ColorNoiseReduction → denoise 强度（语义近似）
    GrainAmount/Size/Frequency → grain（近似）
    LensManualDistortionAmount → lens_distort（近似）
    LocalTemperature → temp = 5250 + v（LR 局部色温是偏移量，近似绝对温度）

**待标定**（结构已解析，数值映射需受控实验）::

    LocalPointColors（19 浮点元组：首 3 项 = 取样色 0-1；偏移/范围字段未知）

**v1.8 承接**（语法已预留，解析即归类报告）::

    Mask/Paint（笔刷）、Mask/Aggregate（AI 主题/人物/对象等）

**未覆盖**::

    PerspectiveUpright/Transform*（透视）、RetouchAreas（修复笔）、
    LensProfile*（镜头配置）、ColorGradeGlobal*（全局分级，PhotoS 无对应）

v2.5 起支持反向：:func:`options_to_xmp` 把 ``ProcessOptions`` 写成 LR 可读的
``.xmp`` sidecar（上面「直接映射」的逐字段逆变换 + radial/linear 蒙版 →
MaskGroupBasedCorrections + 评分/关键词 → xmp:Rating / dc:subject）——LR 直接
打开原图即可续修，是「agent 用的 Lightroom」双向互通的写出侧。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "LrError", "parse_xmp_sidecar", "parse_develop_blob", "scan_catalog",
    "crs_to_options", "options_to_xmp", "write_xmp_sidecar",
    "coverage", "scan_and_report", "discover_inputs",
    "write_export", "HSL_COLORS", "LOCAL_MAP", "content_features",
]

_XMP_CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"
_XMP_CRD = "{http://ns.adobe.com/camera-raw-defaults/1.0/}"

# Lightroom 8 色域顺序 = PhotoS hsl 顺序
HSL_COLORS: Tuple[str, ...] = (
    "red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta",
)

# LR 局部调整键（Local*）→ PhotoS mask_adjust 标量键
LOCAL_MAP: Dict[str, str] = {
    "LocalExposure2012": "exposure",    # EV 直传
    "LocalBrightness": "brightness",    # 1 + v/100
    "LocalContrast2012": "contrast",    # 1 + v/100
    "LocalSaturation": "saturation",    # 1 + v/100
    "LocalVibrance": "vibrance",        # v/100
    "LocalClarity": "clarity",          # v/100
    "LocalTexture": "texture",          # v/100
    "LocalSharpness": "sharpen",        # 1 + v/100
    "LocalTemperature": "temp",         # 5250 + v（偏移近似绝对温度）
    "LocalTint": "tint",                # 直传
}


class LrError(ValueError):
    """LR 数据解析/映射错误。"""


# ---------------------------------------------------------------- XMP sidecar

def parse_xmp_sidecar(source: Any) -> Dict[str, Any]:
    """解析 XMP sidecar（文件路径或 XML 字符串）→ ``{crs字段: 值}``。

    值均为字符串（XMP 属性原始文本）；``crd:`` 默认段字段以 ``crd_`` 前缀并入。
    v2.5 起 XMP sidecar 与 catalog blob 同能：曲线属性（``"0, 0 128, 140"``）
    转为 float 列表、``MaskGroupBasedCorrections``/``CorrectionMasks`` 嵌套
    元素转为与 blob 同形的 list-of-dicts（此前只有 catalog 明文快照能读出
    曲线/蒙版）。
    """
    if isinstance(source, (str, os.PathLike)) and os.path.exists(source):
        with open(source, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = str(source)
    if "<!ENTITY" in text:
        # stdlib ElementTree expands internal entities — a crafted sidecar
        # (billion laughs) would balloon memory. Real XMP files from
        # Lightroom never carry entity definitions.
        raise LrError("XMP 包含实体定义, 已拒绝 (entity definitions rejected)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise LrError(f"XMP 解析失败: {e}") from e
    out: Dict[str, Any] = {}
    for elem in root.iter():
        for key, value in elem.attrib.items():
            if key.startswith(_XMP_CRS):
                out[key[len(_XMP_CRS):]] = value
            elif key.startswith(_XMP_CRD):
                out["crd_" + key[len(_XMP_CRD):]] = value
    _shim_curve_lists(out)
    corrections = _xmp_mask_groups(root)
    if corrections:
        out["MaskGroupBasedCorrections"] = corrections
    return out


def _shim_curve_lists(out: Dict[str, Any]) -> None:
    """ToneCurve* XMP 字符串（``"0, 0 128, 140 255, 255"``）→ float 列表。

    _curves_string 只消费 list（catalog blob 形状）；XMP 属性原生是字符串，
    不转换则曲线静默丢失（v2.5 前的既有缺口）。点对 = "x, y"（逗号后可带
    空格），相邻点对以空白分隔——不能按空白裸切，那会把点对切碎。
    """
    for key in _CURVE_KEY_BY_CHANNEL.values():
        v = out.get(key)
        if not isinstance(v, str) or not v.strip():
            continue
        pairs = re.findall(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", v)
        if len(pairs) >= 2:
            nums: List[float] = []
            for xs, ys in pairs:
                nums.extend((float(xs), float(ys)))
            out[key] = nums


def _coerce_xmp_scalar(v: str) -> Any:
    """XMP 标量文本 → int/float/bool/str（对齐 catalog blob 的值类型；
    MaskInverted 之类必须真 bool——字符串 "False" 是 truthy）。"""
    if v == "True":
        return True
    if v == "False":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _rdf_li_items(elem: Any) -> List[Any]:
    """rdf:Bag/Alt/Seq 容器 → li 元素列表（无容器时把子元素当 li，宽容解析）。"""
    out: List[Any] = []
    for child in elem:
        if child.tag.rsplit("}", 1)[-1] in ("Bag", "Alt", "Seq"):
            out.extend(child)
        else:
            out.append(child)
    return out


def _xmp_mask_groups(root: Any) -> List[Dict[str, Any]]:
    """MaskGroupBasedCorrections / CorrectionMasks 元素 → blob 同形 corrections。

    只处理直接挂在 rdf:Description 下的组元素——嵌在组内 li 里的
    CorrectionMasks 是蒙版几何载体，不是独立修正组（iter 会重复访问）。
    """
    parent_of = {c: p for p in root.iter() for c in p}
    out: List[Dict[str, Any]] = []
    for elem in root.iter():
        if not elem.tag.startswith(_XMP_CRS):
            continue
        name = elem.tag[len(_XMP_CRS):]
        if name not in ("MaskGroupBasedCorrections", "CorrectionMasks"):
            continue
        parent = parent_of.get(elem)
        if parent is not None and parent.tag.startswith(_XMP_CRS):
            continue
        for li in _rdf_li_items(elem):
            corr: Dict[str, Any] = {}
            for k, v in li.attrib.items():
                if k.startswith(_XMP_CRS):
                    corr[k[len(_XMP_CRS):]] = _coerce_xmp_scalar(v)
            for child in li:
                if not child.tag.startswith(_XMP_CRS):
                    continue
                cname = child.tag[len(_XMP_CRS):]
                if cname == "CorrectionMasks":
                    corr[cname] = [
                        {k[len(_XMP_CRS):]: _coerce_xmp_scalar(v)
                         for k, v in m.attrib.items() if k.startswith(_XMP_CRS)}
                        for m in _rdf_li_items(child)]
                elif cname == "LocalPointColors":
                    corr[cname] = [(it.text or "").strip()
                                   for it in _rdf_li_items(child)]
                elif child.text is not None and child.text.strip():
                    corr[cname] = _coerce_xmp_scalar(child.text.strip())
            if corr:
                out.append(corr)
    return out


# -------------------------------------------------------- catalog 快照（明文）

_TOKEN_RE = re.compile(r"""
    \s*(?:(\{|\}|,|=)                        # 标点
        |"((?:[^"\\]|\\.)*)"                 # 引号字符串
        |([A-Za-z_][A-Za-z0-9_.]*)           # 标识符/键名
        |([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)  # 数字
    )""", re.VERBOSE)


def _tokenize(text: str) -> List[Any]:
    toks: List[Any] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        punc, qstr, ident, num = m.groups()
        if punc is not None:
            toks.append(punc)
        elif qstr is not None:
            toks.append(qstr)
        elif ident is not None:
            toks.append(ident)
        else:
            toks.append(float(num) if ("." in num or "e" in num.lower())
                        else int(num))
    return toks


def _parse_value(toks: List[Any], i: int) -> Tuple[Any, int]:
    t = toks[i]
    if t == "{":
        return _parse_block(toks, i)
    if t == "true":
        return True, i + 1
    if t == "false":
        return False, i + 1
    if isinstance(t, (int, float)):
        return t, i + 1
    return str(t), i + 1


def _parse_block(toks: List[Any], i: int) -> Tuple[Any, int]:
    """``{ ... }`` → (dict|list, 结束下标)。块内出现 ``key =`` 即 dict，否则 list。"""
    out: Any = None
    i += 1
    while i < len(toks):
        t = toks[i]
        if t == "}":
            return (out if out is not None else {}), i + 1
        if t == ",":
            i += 1
            continue
        if (isinstance(t, str) and i + 1 < len(toks) and toks[i + 1] == "="):
            if out is None:
                out = {}
            key = t
            i += 2
            val, i = _parse_value(toks, i)
            out[key] = val
        else:
            if out is None:
                out = []
            val, i = _parse_value(toks, i)
            out.append(val)
    return (out if out is not None else {}), i


def parse_develop_blob(text: str) -> Dict[str, Any]:
    """解析 LR catalog 的 ``s = { key = value, ... }`` 明文快照 → dict。

    值类型：数字（int/float）/ 字符串 / bool / 嵌套 dict / 数组；
    容错：未知字符跳过，遇块结束即返回（LR 18 实测格式）。
    """
    toks = _tokenize(text)
    try:
        start = toks.index("{")
    except ValueError:
        raise LrError("快照中找不到 '{'（非 s = { ... } 格式）") from None
    val, _ = _parse_block(toks, start)
    if not isinstance(val, dict):
        raise LrError("快照顶层应为 dict")
    return val


# ---------------------------------------------------------------- 映射

def _f(settings: Dict[str, Any], key: str) -> float:
    v = settings.get(key)
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _hsl_string(settings: Dict[str, Any]) -> str:
    """LR HSL 24 项 → PhotoS hsl 紧凑串（只含非零色域）。"""
    segs = []
    for color in HSL_COLORS:
        cap = color.capitalize()
        h = _f(settings, f"HueAdjustment{cap}")
        s = _f(settings, f"SaturationAdjustment{cap}")
        l = _f(settings, f"LuminanceAdjustment{cap}")
        if h or s or l:
            segs.append(f"{color}:{h * 1.8:.3f},{s / 100.0:.3f},{l / 100.0:.3f}")
    return ";".join(segs)


_CURVE_CHANNELS = (
    ("ToneCurvePV2012", "rgb"),
    ("ToneCurvePV2012Red", "r"),
    ("ToneCurvePV2012Green", "g"),
    ("ToneCurvePV2012Blue", "b"),
)


def _curves_string(settings: Dict[str, Any]) -> str:
    """ToneCurvePV2012 平铺点对 → PhotoS curves 紧凑串（'' = 无曲线编辑）。"""
    segs = []
    for key, ch in _CURVE_CHANNELS:
        pts = settings.get(key)
        if not isinstance(pts, list) or len(pts) < 4:
            continue
        pairs = [f"{pts[i]:g},{pts[i + 1]:g}" for i in range(0, len(pts) - 1, 2)]
        if len(pairs) == 2 and pairs[0] == "0,0" and pairs[1] == "255,255":
            continue  # 恒等曲线
        segs.append(f"{ch}:{';'.join(pairs)}")
    return "|".join(segs)


def _color_grading_string(settings: Dict[str, Any]) -> str:
    """ColorGrade 3 路 + 每区 L → PhotoS color_grading 紧凑串。"""
    zones = {
        "ColorGradeShadow": "shadows",
        "ColorGradeMidtone": "midtones",
        "ColorGradeHighlight": "highlights",
    }
    segs = []
    for key, zone in zones.items():
        hue = _f(settings, key + "Hue")
        sat = _f(settings, key + "Sat")
        lum = _f(settings, key + "Lum")
        if not (hue or sat or lum):
            continue
        h = hue - 360.0 if hue > 180.0 else hue
        segs.append(f"{zone}:{h:.1f},{sat / 100.0:.3f},{lum / 100.0:.3f}")
    return ";".join(segs)


def _point_color_tuples(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """LocalPointColors 元组解析 → [{rgb: (r,g,b) 0-255, raw: 19 浮点}]。

    首 3 项为取样色（0-1 归一化）；其余字段（偏移/范围/曲线）待受控实验标定。
    """
    out = []
    for corr in _iter_corrections(settings):
        for raw in corr.get("LocalPointColors") or []:
            if not isinstance(raw, str):
                continue
            try:
                nums = [float(x) for x in raw.split(",")]
            except ValueError:
                continue
            if len(nums) >= 3:
                rgb = tuple(max(0, min(255, int(round(v * 255.0))))
                            for v in nums[:3])
                out.append({"rgb": rgb, "raw": nums})
    return out


def _iter_corrections(settings: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """MaskGroupBasedCorrections（LR 17+；回退 legacy CorrectionMasks）。"""
    src = settings.get("MaskGroupBasedCorrections")
    if not isinstance(src, list):
        src = settings.get("CorrectionMasks")
    for corr in src or []:
        if isinstance(corr, dict):
            yield corr


def _safe_mask_name(name: str) -> str:
    """PhotoS 蒙版名不允许 ``:;,= `` 字符——LR 的「蒙版 1」等名替换为下划线。"""
    return re.sub(r"[:;,= ]", "_", name).strip() or "mask"


def _box(cm: Dict[str, Any]) -> Tuple[float, float, float, float]:
    l = _f(cm, "Left")
    t = _f(cm, "Top")
    r = _f(cm, "Right")
    b = _f(cm, "Bottom")
    if r <= l:
        r = l + 1e-6
    if b <= t:
        b = t + 1e-6
    return l, t, r, b


def _radial_mask(cm: Dict[str, Any], name: str) -> str:
    l, t, r, b = _box(cm)
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    rx, ry = (r - l) / 2.0, (b - t) / 2.0
    feather = min(1.0, _f(cm, "Feather") / 100.0)
    seg = (f"{name}:radial:{cx:.4f},{cy:.4f},{rx:.4f},{ry:.4f},"
           f"feather={feather:.3f}")
    if cm.get("MaskInverted"):
        seg += ",invert"
    return seg


def _linear_mask(cm: Dict[str, Any], name: str) -> str:
    l, t, r, b = _box(cm)
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    half = 0.5 * math.hypot(r - l, b - t)
    ang = math.radians(_f(cm, "Angle"))
    # LR Angle：0° = 自上而下，正角顺时针（屏幕坐标 y 向下）
    dx, dy = math.sin(ang) * half, math.cos(ang) * half
    feather = min(1.0, _f(cm, "Feather") / 100.0)
    seg = (f"{name}:linear:{cx - dx:.4f},{cy - dy:.4f},"
           f"{cx + dx:.4f},{cy + dy:.4f},feather={feather:.3f}")
    if cm.get("MaskInverted"):
        seg += ",invert"
    return seg


def _local_adjust(corr: Dict[str, Any], name: str) -> str:
    parts = []
    for lr_key, ps_key in LOCAL_MAP.items():
        v = _f(corr, lr_key)
        if v == 0.0:
            continue
        if ps_key in ("brightness", "contrast", "saturation", "sharpen"):
            parts.append(f"{ps_key}={(1.0 + v / 100.0):.4f}")
        elif ps_key in ("vibrance", "clarity", "texture"):
            parts.append(f"{ps_key}={(v / 100.0):.4f}")
        elif ps_key == "temp":
            parts.append(f"temp={5250.0 + v:.1f}")
        else:
            parts.append(f"{ps_key}={v:.4f}")
    return f"{name}:{','.join(parts)}" if parts else ""


def _masks_and_adjust(settings: Dict[str, Any]
                      ) -> Tuple[str, str, List[str]]:
    """(masks_str, mask_adjust_str, v1_8_names)——几何蒙版进 PhotoS，
    笔刷/AI 蒙版（Mask/Paint、Mask/Aggregate）归入 v1.8 待承接。"""
    masks_segs: List[str] = []
    adjust_segs: List[str] = []
    v18: List[str] = []
    for corr in _iter_corrections(settings):
        name = _safe_mask_name(str(corr.get("CorrectionName") or "mask"))
        geom: Optional[str] = None
        for cm in corr.get("CorrectionMasks") or []:
            if not isinstance(cm, dict):
                continue
            what = cm.get("What", "")
            if what == "Mask/CircularGradient":
                geom = _radial_mask(cm, name)
                break
            if what == "Mask/LinearGradient":
                geom = _linear_mask(cm, name)
                break
        if geom is not None:
            masks_segs.append(geom)
            adj = _local_adjust(corr, name)
            if adj:
                adjust_segs.append(adj)
        else:
            v18.append(name)
    return ";".join(masks_segs), ";".join(adjust_segs), v18


def _crop_relative(settings: Dict[str, Any]
                   ) -> Optional[Tuple[float, float, float, float]]:
    """LR 相对裁剪（0-1）→ (left, top, right, bottom)；未裁剪返回 None。"""
    try:
        l = _f(settings, "CropLeft")
        t = _f(settings, "CropTop")
        r = _f(settings, "CropRight") if "CropRight" in settings else 1.0
        b = _f(settings, "CropBottom") if "CropBottom" in settings else 1.0
    except (TypeError, ValueError):
        return None
    if l <= 0.0001 and t <= 0.0001 and r >= 0.9999 and b >= 0.9999:
        return None
    return (l, t, r, b)


def _crop_to_pixels(rel: Tuple[float, float, float, float],
                    w: int, h: int) -> str:
    l, t, r, b = rel
    x = int(round(l * w))
    y = int(round(t * h))
    cw = max(1, int(round((r - l) * w)))
    ch = max(1, int(round((b - t) * h)))
    return f"{cw}x{ch}+{x}+{y}"


def crs_to_options(settings: Dict[str, Any], *,
                   image_size: Optional[Tuple[int, int]] = None,
                   white_balance: Optional[str] = None) -> Dict[str, Any]:
    """crs 字段 → ``ProcessOptions`` kwargs（PhotoS 可直接消费的值）。

    ``image_size=(w, h)`` 时裁剪换算为像素；``white_balance`` 非 "As Shot" 时
    才映射 Temperature/Tint（避免把机内白平衡误当编辑）。数值换算见模块 docstring。
    """
    opts: Dict[str, Any] = {}
    try:
        if white_balance != "As Shot":
            if "Temperature" in settings:
                opts["wb_temp"] = int(round(_f(settings, "Temperature")))
            if "Tint" in settings:
                opts["wb_tint"] = _f(settings, "Tint")
        if "Exposure2012" in settings:
            opts["exposure"] = _f(settings, "Exposure2012")
        if "Contrast2012" in settings:
            opts["contrast"] = 1.0 + _f(settings, "Contrast2012") / 100.0
        if "Saturation" in settings:
            opts["saturation"] = 1.0 + _f(settings, "Saturation") / 100.0
        for key, ps_key in (("Vibrance", "vibrance"), ("Texture", "texture"),
                            ("Clarity2012", "clarity"), ("Dehaze", "dehaze")):
            if key in settings:
                opts[ps_key] = _f(settings, key) / 100.0
        hsl = _hsl_string(settings)
        if hsl:
            opts["hsl"] = hsl
        curves = _curves_string(settings)
        if curves:
            opts["curves"] = curves
        cg = _color_grading_string(settings)
        if cg:
            opts["color_grading"] = cg
        if "VignetteAmount" in settings and _f(settings, "VignetteAmount") != 0.0:
            mid = _f(settings, "VignetteMidpoint") / 100.0
            opts["vignette"] = f"{_f(settings, 'VignetteAmount') / 100.0:.3f},{mid:.3f}"
        if image_size:
            rel = _crop_relative(settings)
            if rel:
                opts["crop"] = _crop_to_pixels(rel, image_size[0], image_size[1])
        masks, adjust, _v18 = _masks_and_adjust(settings)
        if masks:
            opts["masks"] = masks
        if adjust:
            opts["mask_adjust"] = adjust
    except (TypeError, ValueError) as e:
        raise LrError(f"crs 字段映射失败: {e}") from e
    return opts


# ---------------------------------------------------------------- XMP 写出 (v2.5)

# 结构常量：fixture lr_crs_edited.xmp（LR 18 实测）同源——PV2023 系，
# LR 4+ 均可读 ProcessVersion=15.4
_XMP_WRITER_VERSION = "18.3.2"
_XMP_PROCESS_VERSION = "15.4"

_XMP_TOP_NS = "adobe:ns:meta/"
_XMP_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP_XAP_NS = "http://ns.adobe.com/xap/1.0/"
_XMP_DC_NS = "http://purl.org/dc/elements/1.1/"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_CURVE_KEY_BY_CHANNEL = {"rgb": "ToneCurvePV2012", "r": "ToneCurvePV2012Red",
                         "g": "ToneCurvePV2012Green", "b": "ToneCurvePV2012Blue"}
_CG_ZONE_KEYS = {"shadows": "ColorGradeShadow", "midtones": "ColorGradeMidtone",
                 "highlights": "ColorGradeHighlight"}
_PS_LOCAL_TO_LR: Dict[str, str] = {v: k for k, v in LOCAL_MAP.items()}

# 单向写出（LR 受益、读侧不回读——crs_to_options 未实现这些字段）：
# highlight_recovery → Highlights2012 / denoise → LuminanceSmoothing /
# sharpen → Sharpness / grain → Grain*（语义近似，docstring 已声明）


def _lr_num(v: float) -> str:
    """LR 标度数值格式化：整值给整串，否则 5 位小数去尾零。"""
    v = float(v)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.5f}".rstrip("0").rstrip(".")


def _lr_signed(v: float) -> str:
    """带符号标量（Exposure2012/Tint 的 LR 风格 ``+0.35``）。"""
    v = float(v)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):+d}"
    return f"{v:+.5f}".rstrip("0").rstrip(".")


def _corrections_from_masks(masks_str: str, adjust_str: str,
                            warnings: List[str]) -> List[Dict[str, Any]]:
    """masks/mask_adjust 紧凑串 → LR 修正组（radial/linear 可逆，其余告警）。"""
    from .mask import parse_mask_adjust, parse_masks

    specs = parse_masks(masks_str) if masks_str else []
    adjusts = parse_mask_adjust(adjust_str) if adjust_str else {}
    out: List[Dict[str, Any]] = []
    for spec in specs:
        lr_attrs: Dict[str, str] = {}
        for key, val in (adjusts.get(spec.name) or {}).items():
            lr_key = _PS_LOCAL_TO_LR.get(key)
            if lr_key is None:
                warnings.append(
                    f"mask '{spec.name}': 局部参数 {key} 无 LR 对应字段，已跳过")
                continue
            val = float(val)
            if key in ("brightness", "contrast", "saturation", "sharpen"):
                lr_v = (val - 1.0) * 100.0
            elif key in ("vibrance", "clarity", "texture"):
                lr_v = val * 100.0
            elif key == "temp":
                lr_v = val - 5250.0
            else:
                lr_v = val
            if abs(lr_v) > 1e-9:
                lr_attrs[lr_key] = _lr_num(lr_v)
        if spec.kind == "radial":
            cx, cy, rx, ry = (float(v) for v in spec.params[:4])
            geo = _mask_geo("Mask/CircularGradient",
                            cx - rx, cy - ry, cx + rx, cy + ry,
                            spec.feather, spec.invert)
        elif spec.kind == "linear":
            x0, y0, x1, y1 = (float(v) for v in spec.params[:4])
            # _linear_mask 的逆：box = 端点包络；dx,dy = 半向量 →
            # Angle = atan2(dx, dy)（LR 0° = 自上而下，正角顺时针）
            angle = math.degrees(math.atan2((x1 - x0) / 2.0, (y1 - y0) / 2.0))
            geo = _mask_geo("Mask/LinearGradient",
                            min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                            spec.feather, spec.invert, angle)
        else:
            warnings.append(
                f"mask '{spec.name}'（{spec.kind}）无 LR 几何等价，已跳过"
                f"（其局部调整一并跳过）")
            continue
        out.append({"name": spec.name, "attrs": lr_attrs, "mask": geo})
    kept = {c["name"] for c in out}
    for name, adj in adjusts.items():
        if adj and name not in kept and not any(
                w.startswith(f"mask '{name}'") for w in warnings):
            warnings.append(f"mask '{name}' 未导出，其局部调整一并跳过")
    return out


def _mask_geo(what: str, left: float, top: float, right: float, bottom: float,
              feather: float, invert: bool,
              angle: Optional[float] = None) -> Dict[str, str]:
    def u(v: float) -> str:
        return f"{max(0.0, min(1.0, float(v))):.6f}"

    geo = {
        "What": what,
        "Top": u(top), "Left": u(left), "Bottom": u(bottom), "Right": u(right),
        "CenterWeight": "0", "CornerRadius": "0",
        "XOffset": "0.000000", "YOffset": "0.000000",
        "Feather": _lr_num(max(0.0, min(1.0, float(feather or 0.0))) * 100.0),
        "MaskInput": "100",
        "MaskInverted": "True" if invert else "False",
        "MaskVersion": "1",
    }
    if angle is not None:
        geo["Angle"] = f"{float(angle):.4f}"
    return geo


def _render_xmp(crs_attrs: Dict[str, str],
                corrections: List[Dict[str, Any]],
                rating: Optional[int], label: Optional[str],
                keywords: Optional[Sequence[str]],
                title: Optional[str]) -> str:
    for prefix, uri in (("x", _XMP_TOP_NS), ("rdf", _XMP_RDF_NS),
                        ("crs", _XMP_CRS[1:-1]), ("xmp", _XMP_XAP_NS),
                        ("dc", _XMP_DC_NS)):
        ET.register_namespace(prefix, uri)
    root = ET.Element(f"{{{_XMP_TOP_NS}}}xmpmeta")
    rdf = ET.SubElement(root, f"{{{_XMP_RDF_NS}}}RDF")
    desc = ET.SubElement(rdf, f"{{{_XMP_RDF_NS}}}Description",
                         {f"{{{_XMP_RDF_NS}}}about": ""})
    if rating is not None:
        desc.set(f"{{{_XMP_XAP_NS}}}Rating", str(int(rating)))
    if label:
        desc.set(f"{{{_XMP_XAP_NS}}}Label", label)
    for k, v in crs_attrs.items():
        desc.set(f"{_XMP_CRS}{k}", v)
    if corrections:
        groups = ET.SubElement(desc, f"{_XMP_CRS}MaskGroupBasedCorrections")
        bag = ET.SubElement(groups, f"{{{_XMP_RDF_NS}}}Bag")
        for corr in corrections:
            li = ET.SubElement(bag, f"{{{_XMP_RDF_NS}}}li")
            li.set(f"{_XMP_CRS}CorrectionName", corr["name"])
            li.set(f"{_XMP_CRS}CorrectionActive", "True")
            for k, v in corr["attrs"].items():
                li.set(f"{_XMP_CRS}{k}", v)
            masks_el = ET.SubElement(li, f"{_XMP_CRS}CorrectionMasks")
            mbag = ET.SubElement(masks_el, f"{{{_XMP_RDF_NS}}}Bag")
            mli = ET.SubElement(mbag, f"{{{_XMP_RDF_NS}}}li")
            for k, v in corr["mask"].items():
                mli.set(f"{_XMP_CRS}{k}", v)
    if keywords:
        subj = ET.SubElement(desc, f"{{{_XMP_DC_NS}}}subject")
        kbag = ET.SubElement(subj, f"{{{_XMP_RDF_NS}}}Bag")
        for kw in keywords:
            kli = ET.SubElement(kbag, f"{{{_XMP_RDF_NS}}}li")
            kli.text = str(kw)
    if title:
        t = ET.SubElement(desc, f"{{{_XMP_DC_NS}}}title")
        alt = ET.SubElement(t, f"{{{_XMP_RDF_NS}}}Alt")
        tli = ET.SubElement(alt, f"{{{_XMP_RDF_NS}}}li", {_XML_LANG: "x-default"})
        tli.text = str(title)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode") + "\n")


def options_to_xmp(options: Any, *, image_size: Optional[Tuple[int, int]] = None,
                   rating: Optional[int] = None, label: Optional[str] = None,
                   keywords: Optional[Sequence[str]] = None,
                   title: Optional[str] = None) -> Tuple[str, List[str]]:
    """``ProcessOptions`` → LR 可读 XMP sidecar 文本；返回 ``(xmp, warnings)``。

    crs_to_options「直接映射」的逐字段逆变换（数值互逆，往返可验证）；此外
    单向写出 LR 受益字段：``highlight_recovery→Highlights2012``、
    ``denoise→LuminanceSmoothing``、``sharpen→Sharpness``、``grain→Grain*``
    （读侧未实现这些字段，往返不核对）。中性值一律不写（LR 对缺省属性取
    默认，sidecar 干净）。

    不可逆内容进 warnings 不抛错：AI/笔刷/combo/颜色蒙版、字符串型局部
    调整、point_color、levels、crop_ratio、lens_vignette/lens_ca、rotate/flip
    （vignette 的 feather 无 LR 手动暗角对应，写出即丢）。
    ``options`` 可为 ProcessOptions、dict（按字段名过滤）或 None（仅元数据）。
    """
    warnings: List[str] = []
    if options is None or isinstance(options, dict):
        from .engine import ProcessOptions
        if isinstance(options, dict):
            known = ProcessOptions.__dataclass_fields__
            options = ProcessOptions(**{k: v for k, v in options.items()
                                        if k in known})
        else:
            options = ProcessOptions()
    crs: Dict[str, str] = {
        "Version": _XMP_WRITER_VERSION,
        "ProcessVersion": _XMP_PROCESS_VERSION,
        "HasSettings": "True",
    }

    ev = getattr(options, "ev", None)
    if ev:
        crs["Exposure2012"] = _lr_signed(ev)
    if options.contrast != 1.0:
        crs["Contrast2012"] = _lr_num((options.contrast - 1.0) * 100.0)
    if options.saturation != 1.0:
        crs["Saturation"] = _lr_num((options.saturation - 1.0) * 100.0)
    for field, key in (("vibrance", "Vibrance"), ("texture", "Texture"),
                       ("clarity", "Clarity2012"), ("dehaze", "Dehaze")):
        v = float(getattr(options, field, 0.0) or 0.0)
        if v:
            crs[key] = _lr_num(v * 100.0)
    if options.wb_temp is not None or options.wb_tint:
        crs["WhiteBalance"] = "Custom"
        if options.wb_temp is not None:
            crs["Temperature"] = str(int(round(options.wb_temp)))
        if options.wb_tint:
            crs["Tint"] = _lr_signed(options.wb_tint)

    # 单向写出（LR 受益）
    hr = getattr(options, "highlight_recovery", None)
    if hr:
        crs["Highlights2012"] = _lr_num(-float(hr) * 100.0)
    dn = getattr(options, "denoise", None)
    if dn:
        crs["LuminanceSmoothing"] = _lr_num(float(dn) * 100.0)
    if options.sharpen != 1.0:
        crs["Sharpness"] = _lr_num(max(0.0, min(150.0,
                                                (options.sharpen - 1.0) * 100.0)))
    if options.grain:
        try:
            parts = [p.strip() for p in options.grain.split(",")]
            amount = float(parts[0]) if parts and parts[0] else 0.1
            size = float(parts[1]) if len(parts) > 1 and parts[1] else 1.0
        except ValueError:
            raise LrError(f"grain 无法解析: {options.grain!r}") from None
        if amount:
            crs["GrainAmount"] = _lr_num(amount * 100.0)
            crs["GrainSize"] = _lr_num(size * 25.0)
            crs["GrainFrequency"] = "50"

    if options.hsl:
        from .grade import _parse_hsl  # noqa: SLF001 — 同仓紧凑串语法的唯一权威
        for color, (h, s, l) in _parse_hsl(options.hsl).items():
            cap = color.capitalize()
            if h:
                crs[f"HueAdjustment{cap}"] = _lr_num(
                    max(-30.0, min(30.0, h / 1.8)))
            if s:
                crs[f"SaturationAdjustment{cap}"] = _lr_num(s * 100.0)
            if l:
                crs[f"LuminanceAdjustment{cap}"] = _lr_num(l * 100.0)

    if options.curves:
        from .grade import _parse_curves  # noqa: SLF001 — 同上
        for ch, points in _parse_curves(options.curves).items():
            if len(points) == 2 and points[0] == (0.0, 0.0) \
                    and points[1] == (255.0, 255.0):
                continue  # 恒等曲线
            crs[_CURVE_KEY_BY_CHANNEL[ch]] = " ".join(
                f"{x:g}, {y:g}" for x, y in points)

    if options.color_grading:
        from .grade import _parse_color_grading  # noqa: SLF001 — 同上
        for zone, (h, s, l) in _parse_color_grading(options.color_grading).items():
            key = _CG_ZONE_KEYS[zone]
            if h:
                crs[key + "Hue"] = _lr_num(h + 360.0 if h < 0 else h)
            if s:
                crs[key + "Sat"] = _lr_num(s * 100.0)
            if l:
                crs[key + "Lum"] = _lr_num(l * 100.0)

    if options.vignette:
        # 缺省字段沿用 grade._parse_vignette 默认（midpoint/feather 0.5）
        try:
            parts = [p.strip() for p in options.vignette.split(",")]
            amount = float(parts[0]) if parts and parts[0] else 0.5
            midpoint = float(parts[1]) if len(parts) > 1 and parts[1] else 0.5
        except ValueError:
            raise LrError(f"vignette 无法解析: {options.vignette!r}") from None
        if amount:
            crs["VignetteAmount"] = _lr_num(amount * 100.0)
            crs["VignetteMidpoint"] = _lr_num(midpoint * 100.0)

    if options.crop:
        m = re.match(r"(\d+)x(\d+)(?:\+(\d+)\+(\d+))?", options.crop)
        if not m:
            raise LrError(f"crop 无法解析: {options.crop!r}")
        cw, ch = int(m.group(1)), int(m.group(2))
        if not image_size:
            warnings.append("crop 需要 image_size（write_xmp_sidecar 自动提供），"
                            "已跳过")
        else:
            w, h = image_size
            if m.group(3) is not None:
                x, y = int(m.group(3)), int(m.group(4))
            else:  # 与 apply_crop 的居中语义一致
                x, y = (w - cw) // 2, (h - ch) // 2
            if not (x == 0 and y == 0 and cw >= w and ch >= h):
                crs["CropLeft"] = f"{x / w:.6f}"
                crs["CropTop"] = f"{y / h:.6f}"
                crs["CropRight"] = f"{(x + cw) / w:.6f}"
                crs["CropBottom"] = f"{(y + ch) / h:.6f}"
                crs["HasCrop"] = "True"
                crs["CropAngle"] = "0"
                crs["CropConstrainToWarp"] = "0"
                crs["CropConstrainToUnitSquare"] = "1"

    corrections = _corrections_from_masks(options.masks, options.mask_adjust,
                                          warnings)

    # 不可逆字段逐条告警（非中性才值得打扰用户）
    if options.levels and options.levels.strip() not in ("", "0,255", "0,255,1"):
        warnings.append("levels 无 LR PV2013 对应字段，已跳过")
    if options.point_color:
        warnings.append("point_color 尚无 LR 写出映射，已跳过")
    if options.lens_distort:
        crs["LensManualDistortionAmount"] = _lr_num(options.lens_distort * 100.0)
    if options.lens_vignette or options.lens_ca:
        warnings.append("lens_vignette/lens_ca 无 LR 手动字段，已跳过")
    if options.crop_ratio:
        warnings.append("crop_ratio 请在 LR 裁剪工具中按比例重设，已跳过")
    if getattr(options, "rotate_degrees", 0.0) or options.flip:
        warnings.append("rotate/flip 无无损 XMP 对应（crs:Orientation 未接线），"
                        "已跳过")

    xmp = _render_xmp(crs, corrections, rating, label, keywords, title)
    return xmp, warnings


def write_xmp_sidecar(image_path: str, options: Any = None, *,
                      rating: Optional[int] = None, label: Optional[str] = None,
                      keywords: Optional[Sequence[str]] = None,
                      title: Optional[str] = None,
                      out_dir: Optional[str] = None) -> Tuple[str, List[str]]:
    """把 options 写成 ``<原图>.xmp``（LR 打开原图即见调整）；返回 (路径, warnings)。

    裁剪换算所需的 image_size 自动从原图读取。``out_dir`` 指定时 sidecar
    写入该目录（文件名不变）。
    """
    from PIL import Image

    p = os.fspath(image_path)
    with Image.open(p) as im:
        size = im.size
    xmp, warnings = options_to_xmp(options, image_size=size, rating=rating,
                                   label=label, keywords=keywords, title=title)
    stem = os.path.splitext(os.path.basename(p))[0]
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        sidecar = os.path.join(os.fspath(out_dir), stem + ".xmp")
    else:
        sidecar = os.path.splitext(p)[0] + ".xmp"
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write(xmp)
    return sidecar, warnings


# ---------------------------------------------------------------- 覆盖率

# 结构字段（ID/版本/名称/常写默认），不计入"编辑"
_STRUCTURAL_KEYS = frozenset((
    "Version", "CompatibleVersion", "ProcessVersion", "HasSettings",
    "HasCrop", "HasToneCurve", "ToneCurveName2012", "CameraProfile",
    "CameraProfileDigest", "UUID", "CorrectionID", "CorrectionSyncID",
    "MaskID", "MaskSyncID", "MaskName", "CorrectionName", "CorrectionActive",
    "CorrectionAmount", "MaskActive", "MaskBlendMode", "MaskValue", "MaskInverted",
    "CenterWeight", "Feather", "Flipped", "Roundness", "Angle", "Midpoint",
    "SupportsAmount", "SupportsMonochrome", "SupportsOutputReferred",
    "Left", "Top", "Right", "Bottom", "What", "Masks", "MaskGroupBasedCorrections",
    "CorrectionMasks", "LocalPointColors", "PointColors", "CurveRefineSaturation",
    "Brightness", "Contrast",  # LR 2012 时代常写默认
    # 支持性字段（非编辑）
    "WhiteBalance", "CropConstrainAspectRatio", "CropConstrainToUnitSquare",
    "LensProfileEnable", "LensProfileName", "LensProfileDigest", "LensProfileSetup",
    "LensProfileFilename", "LensProfileDistortionScale", "LensProfileVignettingScale",
    "AutoLateralCA", "Look", "RedEyeInfo", "EnableDistractionRemoval", "FilterList",
    "HDRMaxValue", "LensBlur", "PerspectiveScale", "Shadows",
    "UprightCenterNormX", "UprightCenterNormY", "UprightFocalLength35mm",
    "UprightVersion", "UprightTransformCount",
    "DefringePurpleHueLo", "DefringePurpleHueHi", "DefringeGreenHueLo",
    "DefringeGreenHueHi",
    # XMP 侧格式支持字段（蒙版几何/相机配置/元数据）
    "AlreadyApplied", "RawFileName", "Copyright", "Name", "Amount",
    "ConvertToGrayscale", "LookTable", "OverrideLookVignette",
    "LensProfileIsEmbedded", "MaskVersion", "ModelVersion", "ReferencePoint",
    "InputDigest", "InputDigestVersion", "LocalInputDigest",
    "LocalInputDigestVersion", "MaskDigest", "WholeImageArea", "Origin",
    "FullMaskSize", "MaskSubType", "MaskSubCategoryID",
    "ZeroX", "ZeroY", "FullX", "FullY", "Radius", "Flow",
    "LocalCurveRefineSaturation",
))
# 常写默认值（不等于"编辑"）
_DEFAULT_VALUES: Dict[str, float] = {
    "Temperature": 5250.0, "Tint": 0.0, "Sharpness": 40.0,
    "ColorNoiseReduction": 25.0, "SharpenDetail": 25.0, "SharpenEdgeMasking": 0.0,
    "ParametricShadowSplit": 25.0, "ParametricMidtoneSplit": 50.0,
    "ParametricHighlightSplit": 75.0, "ColorGradeBlending": 50.0,
    "VignetteMidpoint": 50.0, "LuminanceSmoothing": 0.0,
    "SharpenRadius": 1.0, "GrainSize": 25.0,
    "ColorNoiseReductionDetail": 50.0, "ColorNoiseReductionSmoothness": 50.0,
}

# 直接映射（→ ProcessOptions 字段）
_MAPPED_KEYS = frozenset((
    "Temperature", "Tint", "Exposure2012", "Contrast2012", "Saturation",
    "Vibrance", "Texture", "Clarity2012", "Dehaze", "VignetteAmount",
    "CropLeft", "CropTop", "CropRight", "CropBottom",
    "ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue",
    "ColorGradeShadowHue", "ColorGradeShadowSat", "ColorGradeShadowLum",
    "ColorGradeMidtoneHue", "ColorGradeMidtoneSat", "ColorGradeMidtoneLum",
    "ColorGradeHighlightHue", "ColorGradeHighlightSat", "ColorGradeHighlightLum",
))
# 近似映射（有损）
_APPROX_KEYS = frozenset((
    "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Sharpness", "SharpenRadius", "GrainAmount", "GrainSize", "GrainFrequency",
    "LensManualDistortionAmount", "PostCropVignetteAmount",
))
# 半映射/待标定
_PARTIAL_KEYS = frozenset((
    "LocalPointColors", "LensProfileEnable", "LensProfileName",
    "AutoLateralCA",
    # XMP 顶层局部调整（真实编辑；与蒙版几何的配对结构待标定）
    "LocalExposure2012", "LocalBrightness", "LocalContrast2012",
    "LocalSaturation", "LocalVibrance", "LocalClarity", "LocalTexture",
    "LocalSharpness", "LocalTemperature", "LocalTint", "LocalShadows2012",
    "LocalBlacks2012", "LocalHighlights2012", "LocalWhites2012",
))
# 无对应（缺口）
_UNMAPPED_KEYS = frozenset((
    "PerspectiveUpright", "UprightTransformCount", "UprightTransform_1",
    "UprightTransform_2", "UprightTransform_3", "UprightTransform_4",
    "RetouchAreas", "RetouchInfo", "RetouchAreas1", "RetouchAreas2",
    "ColorGradeGlobalHue", "ColorGradeGlobalSat", "ColorGradeGlobalLum",
    "DefringePurpleAmount", "DefringeGreenAmount",
))
# v1.8 承接（AI 蒙版/笔刷）
_V18_MASKS = ("Mask/Paint", "Mask/Aggregate", "Mask/Subject", "Mask/Person",
              "Mask/Depth", "Mask/Background", "Mask/Object", "Mask/Brush")


def coverage(settings: Dict[str, Any], *,
             white_balance: Optional[str] = None) -> Dict[str, Any]:
    """非默认参数分类 → {mapped, approximate, partial, unmapped, v1_8, edited,
    mappable_ratio}。供训练数据可行性报告使用。"""
    out = {k: [] for k in
           ("mapped", "approximate", "partial", "unmapped", "v1_8")}
    if not isinstance(settings, dict):
        return {**out, "edited": False, "mappable_ratio": 0.0}

    # 蒙版归类：几何 → mapped；笔刷/AI → v1_8；局部调整也算编辑
    for corr in _iter_corrections(settings):
        kinds = [cm.get("What", "") for cm in corr.get("CorrectionMasks") or []
                 if isinstance(cm, dict)]
        name = _safe_mask_name(str(corr.get("CorrectionName") or "mask"))
        has_geom = any(k in ("Mask/CircularGradient", "Mask/LinearGradient")
                       for k in kinds)
        if has_geom:
            out["mapped"].append(f"mask:{name}")
            if any(_f(corr, lr_key) != 0.0 for lr_key in LOCAL_MAP):
                out["mapped"].append(f"local:{name}")
        elif any(k in _V18_MASKS for k in kinds):
            out["v1_8"].append(f"{name} ({';'.join(kinds) or 'composite'})")

    for key, value in settings.items():
        if key in _STRUCTURAL_KEYS or key.startswith("crd_"):
            continue
        if key in _DEFAULT_VALUES and _f(settings, key) == _DEFAULT_VALUES[key]:
            continue
        # 恒等曲线/零值 HSL 不算编辑
        if key.startswith("ToneCurvePV2012") and isinstance(value, list) and \
                len(value) == 4 and value[0] == 0 and value[1] == 0 and \
                value[2] == 255 and value[3] == 255:
            continue
        if isinstance(value, (int, float)):
            if float(value) == 0.0:
                continue
        elif isinstance(value, str):
            try:
                if float(value) == 0.0:
                    continue
            except ValueError:
                pass  # 非数字字符串（如 "As Shot"）
        if key.startswith(("HueAdjustment", "SaturationAdjustment",
                           "LuminanceAdjustment")):
            out["mapped"].append(key)
        elif key in _MAPPED_KEYS:
            if key in ("Temperature", "Tint") and white_balance == "As Shot":
                continue  # 机内白平衡，非编辑
            out["mapped"].append(key)
        elif key in _APPROX_KEYS:
            out["approximate"].append(key)
        elif key in _PARTIAL_KEYS:
            out["partial"].append(key)
        elif key in _UNMAPPED_KEYS:
            out["unmapped"].append(key)
        # 其余未分类但非默认：归 unmapped（保守）
        else:
            out["unmapped"].append(key)

    edited = bool(out["mapped"] or out["approximate"] or out["partial"]
                  or out["unmapped"] or out["v1_8"])
    total = sum(len(out[k]) for k in ("mapped", "approximate"))
    mappable_ratio = (total / (total + len(out["partial"])
                               + len(out["unmapped"]))) if total else 0.0
    out["edited"] = edited
    out["mappable_ratio"] = round(mappable_ratio, 3)
    return out


# ---------------------------------------------------------------- catalog 扫描

_SCAN_SQL = """
    SELECT s.id_local, s.image, s.text, s.hasMasks, s.hasAIMasks,
           s.hasPointColor, s.whiteBalance, im.fileWidth, im.fileHeight,
           im.rating, f.baseName, f.extension, fo.pathFromRoot, rf.absolutePath
    FROM Adobe_imageDevelopSettings s
    JOIN Adobe_images im ON s.image = im.id_local
    LEFT JOIN AgLibraryFile f ON im.rootFile = f.id_local
    LEFT JOIN AgLibraryFolder fo ON f.folder = fo.id_local
    LEFT JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
"""

_HISTORY_SQL = """
    SELECT image, name, valueString, relValueString
    FROM Adobe_libraryImageDevelopHistoryStep
"""


def scan_catalog(db_path: str, *, with_history: bool = True,
                 parse: bool = True) -> List[Dict[str, Any]]:
    """扫描一个 LR catalog（只读）→ 逐图记录。

    记录：``{path, image_id, image_size, white_balance, has_masks,
    has_ai_masks, has_point_color, settings, history}``——``settings`` 为解析后
    的 crs dict（``parse=False`` 时为原始文本）；``history`` 为 ``{name, value}``
    工具使用轨迹（仅 ``with_history=True``）。关联链已验证（LR 18）：
    settings.image → Adobe_images → AgLibraryFile → folder → root。
    """
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise LrError(f"无法打开 catalog {db_path}: {e}") from e
    try:
        rows = conn.execute(_SCAN_SQL).fetchall()
        history: Dict[int, List[Dict[str, Any]]] = {}
        if with_history:
            for image, name, value, rel in conn.execute(_HISTORY_SQL):
                history.setdefault(image, []).append(
                    {"name": name, "value": value or rel or ""})
    finally:
        conn.close()

    records: List[Dict[str, Any]] = []
    for row in rows:
        (sid, image, text, has_masks, has_ai, has_pc, white_balance,
         fw, fh, rating, base, ext, path_from_root, root_path) = row
        path = ""
        if base and root_path:
            path = os.path.join(root_path, path_from_root or "",
                                f"{base}.{ext or ''}")
        rec: Dict[str, Any] = {
            "path": path,
            "image_id": image,  # Adobe_images.id_local —— 与历史步骤 image 同空间
            "image_size": (fw, fh) if fw and fh else None,
            "white_balance": white_balance,
            "rating": int(rating or 0),  # 0-5 星（0=未评）——美学标注源
            "has_masks": bool(has_masks),
            "has_ai_masks": bool(has_ai),
            "has_point_color": bool(has_pc),
            "settings": None,
            "history": history.get(image),
        }
        if parse and text:
            try:
                rec["settings"] = parse_develop_blob(text)
            except LrError:
                rec["settings"] = None
        elif parse:
            rec["settings"] = {}
        records.append(rec)
    return records


# ---------------------------------------------------------------- 发现 + 报告

def discover_inputs(paths: Optional[Sequence[str]] = None,
                    max_depth: int = 5) -> Tuple[List[str], List[str]]:
    """展开输入为 (.lrcat, .xmp) 列表。

    文件直接采纳；目录递归发现（跳过隐藏目录）。无参数时默认扫
    ``~/Pictures`` 与 ``~/Desktop``（LR 常见位置），其他机器零配置可跑。
    """
    catalogs: List[str] = []
    xmp: List[str] = []
    if paths:
        roots = [os.path.abspath(os.path.expanduser(p)) for p in paths]
    else:
        # No explicit paths: fall back to the common Lightroom locations.
        # Say so loudly — silently walking ~/Desktop surprised people who
        # only meant to test the command.
        print("⚠️  未指定路径, 默认扫描 ~/Pictures 与 ~/Desktop "
              "(no paths given; scanning ~/Pictures and ~/Desktop by default)",
              file=sys.stderr)
        roots = [os.path.join(os.path.expanduser("~"), d)
                 for d in ("Pictures", "Desktop")]
    for root in roots:
        if os.path.isfile(root):
            if root.endswith(".lrcat"):
                catalogs.append(root)
            elif root.lower().endswith(".xmp"):
                xmp.append(root)
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".lrcat"):
                    catalogs.append(os.path.join(dirpath, fn))
                elif fn.lower().endswith(".xmp"):
                    xmp.append(os.path.join(dirpath, fn))
    return sorted(set(catalogs)), sorted(set(xmp))


def _export_record(r: Dict[str, Any], source: str) -> Dict[str, Any]:
    """一张照片的训练数据记录：path + options（PhotoS 参数即标签）。"""
    s = r.get("settings") or {}
    if s:
        c = coverage(s, white_balance=r.get("white_balance"))
        o = crs_to_options(
            s,
            image_size=tuple(r["image_size"]) if r.get("image_size") else None,
            white_balance=r.get("white_balance"))
    else:
        c, o = {"edited": False, "mapped": [], "approximate": [],
                "partial": [], "unmapped": [], "v1_8": []}, {}
    return {
        "source": source,
        "catalog": r.get("catalog"),
        "path": r["path"],
        "rating": r.get("rating", 0),
        "edited": bool(c["edited"]),
        "options": o,
        "image_size": list(r["image_size"]) if r.get("image_size") else None,
        "white_balance": r.get("white_balance"),
        "history": r.get("history"),
        "coverage": {k: len(v) for k, v in c.items()
                     if isinstance(v, (list, tuple))},
    }


def aggregate_report(catalog_records: Sequence[Dict[str, Any]],
                     xmp_records: Sequence[Dict[str, Any]],
                     catalog_paths: Sequence[str]) -> Dict[str, Any]:
    """聚合覆盖报告（JSON 安全）——lr-scan 的数据核心。"""
    from collections import Counter
    param_usage: Counter = Counter()
    tool_usage: Counter = Counter()
    mask_kinds: Counter = Counter()
    approx: Counter = Counter()
    unmapped: Counter = Counter()
    total = edited = v18 = pc = failed = 0
    cat_totals: Dict[str, Dict[str, int]] = {}
    for cat in catalog_paths:
        cat_totals[cat] = {"photos": 0, "edited": 0, "v1_8": 0,
                           "point_color": 0, "failed": 0}
    # 统计（catalog 记录）
    for r in catalog_records:
        total += 1
        ct = cat_totals.get(r.get("catalog", ""))
        if ct:
            ct["photos"] += 1
        s = r.get("settings")
        if not s:
            failed += 1
            if ct:
                ct["failed"] += 1
            continue
        c = coverage(s, white_balance=r.get("white_balance"))
        if c["edited"]:
            edited += 1
            if ct:
                ct["edited"] += 1
        for k in c["mapped"]:
            if k.startswith("mask:") or k.startswith("local:"):
                continue
            param_usage[k] += 1
        if c["mapped"]:
            o = crs_to_options(
                s, image_size=tuple(r["image_size"])
                if r.get("image_size") else None,
                white_balance=r.get("white_balance"))
            for m in o.get("masks", "").split(";"):
                if m:
                    mask_kinds[m.split(":")[1]] += 1
        for k in c["approximate"]:
            approx[k] += 1
        for k in c["unmapped"]:
            unmapped[k] += 1
        if c["v1_8"]:
            v18 += 1
            if ct:
                ct["v1_8"] += 1
        if _point_color_tuples(s):
            pc += 1
            if ct:
                ct["point_color"] += 1
        if r.get("history"):
            for h in r["history"]:
                if h["name"] and not h["name"].startswith("导入"):
                    tool_usage[h["name"]] += 1
    # 统计（XMP 记录）
    xmp_edited = 0
    for r in xmp_records:
        s = r.get("settings") or {}
        c = coverage(s, white_balance=r.get("white_balance"))
        if c["edited"]:
            xmp_edited += 1
            edited += 1
        for k in c["mapped"]:
            if not (k.startswith("mask:") or k.startswith("local:")):
                param_usage[k] += 1
        for k in c["approximate"]:
            approx[k] += 1
        for k in c["unmapped"]:
            unmapped[k] += 1
        if _point_color_tuples(s):
            pc += 1
    return {
        "inputs": {"catalogs": list(catalog_paths), "xmp_files": len(xmp_records)},
        "catalogs": [{"name": os.path.basename(os.path.dirname(
            os.path.dirname(cat))) or cat, **cat_totals[cat]}
            for cat in catalog_paths],
        "param_usage": dict(param_usage.most_common()),
        "tool_usage": dict(tool_usage.most_common()),
        "mask_kinds": dict(mask_kinds.most_common()),
        "approximate": dict(approx.most_common()),
        "unmapped": dict(unmapped.most_common()),
        "summary": {
            "photos": total,
            "xmp_photos": len(xmp_records),
            "edited": edited,
            "xmp_edited": xmp_edited,
            "v1_8_photos": v18,
            "point_color_photos": pc,
            "failed": failed,
            "mapped_fields": sum(param_usage.values()),
        },
    }


def scan_and_report(paths: Optional[Sequence[str]] = None
                    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """发现输入 → 扫描/解析 → (报告 dict, 训练数据记录 list)。

    报告与记录均为 JSON 安全（list/tuple 已转）。失败项记入统计不中断。
    """
    catalogs, xmp_files = discover_inputs(paths)
    catalog_records: List[Dict[str, Any]] = []
    for cat in catalogs:
        try:
            recs = scan_catalog(cat)
        except LrError:
            recs = [{"path": cat, "settings": None, "history": None,
                     "image_size": None, "white_balance": None,
                     "has_masks": False, "has_ai_masks": False,
                     "has_point_color": False}]
        for rec in recs:
            rec["catalog"] = cat
            catalog_records.append(rec)
    xmp_records: List[Dict[str, Any]] = []
    for p in xmp_files:
        try:
            s = parse_xmp_sidecar(p)
        except LrError:
            s = {}
        xmp_records.append({
            "path": p[:-4] if p.lower().endswith(".xmp") else p,
            "image_size": None, "white_balance": s.get("WhiteBalance"),
            "settings": s, "history": None,
            "has_masks": False, "has_ai_masks": False, "has_point_color": False,
        })
    report = aggregate_report(catalog_records, xmp_records, catalogs)
    records = ([_export_record(r, "catalog") for r in catalog_records]
               + [_export_record(r, "xmp") for r in xmp_records])
    return report, records


def merge_packages(pkg_dirs: Sequence[str], out_dir: str) -> Dict[str, Any]:
    """合并多机数据包 → 合并后的 lr_records.jsonl + before/ 图集。

    每个 pkg 目录 = 一台电脑的 ``lr-scan --export-dir --render-dir`` 产出。
    按 basename 去重（LR 文件名天然唯一）；before 图幂等复制；
    记录加 ``source_pkg`` 溯源。返回合并报告（JSON 安全）。
    """
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    out_before = os.path.join(out_dir, "before")
    records: List[Dict[str, Any]] = []
    seen: set = set()
    dupes: List[str] = []
    conflicts: List[str] = []
    images_copied = 0
    for pkg in pkg_dirs:
        pkg = os.path.abspath(pkg)
        name = os.path.basename(pkg)
        jsonl = os.path.join(pkg, "lr_records.jsonl")
        if not os.path.exists(jsonl):
            conflicts.append(f"{name}: 缺 lr_records.jsonl")
            continue
        src_before = os.path.join(pkg, "before")
        for line in open(jsonl, encoding="utf-8"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                conflicts.append(f"{name}: 坏行")
                continue
            stem = os.path.splitext(os.path.basename(rec.get("path") or ""))[0]
            if not stem:
                continue
            if stem in seen:
                # Cameras reuse filenames (DSC0001.NEF everywhere): dropping
                # the record threw away a DIFFERENT photo's training data.
                # Disambiguate with the package name instead.
                stem = f"{stem}__{name}"
                if stem in seen:
                    dupes.append(stem)
                    continue
                rec["path"] = rec.get("path") or ""
                dupes.append(os.path.basename(rec["path"]))
            seen.add(stem)
            img = rec.get("image")
            if img:
                img_name = os.path.basename(img)
                src_img = os.path.join(src_before, img_name)
                if os.path.exists(src_img):
                    dst = os.path.join(out_before, f"{stem}{os.path.splitext(img_name)[1]}")
                    if not os.path.exists(dst):
                        os.makedirs(out_before, exist_ok=True)
                        shutil.copy2(src_img, dst)
                        images_copied += 1
                    rec["image"] = dst  # 绝对路径，训练侧零配置
            rec["source_pkg"] = name
            records.append(rec)
    out_jsonl = os.path.join(out_dir, "lr_records.jsonl")
    tmp_jsonl = out_jsonl + f".tmp.{os.getpid()}"
    with open(tmp_jsonl, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp_jsonl, out_jsonl)  # atomic: a crashed merge no longer
    # leaves a truncated half-written training set
    return {
        "packages": len(pkg_dirs),
        "records": len(records),
        "edited": sum(1 for r in records if r.get("edited")),
        "images_copied": images_copied,
        "duplicates": dupes,
        "conflicts": conflicts,
    }


def write_export(records: Sequence[Dict[str, Any]], out_dir: str,
                 images: Optional[Dict[str, str]] = None,
                 sanitize: bool = False) -> str:
    """写训练数据 JSONL（每行一张照片：path + options + image）→ 返回文件路径。

    ``images``：path → before 渲染图路径（--render-dir 产出），写入 ``image`` 键。
    ``sanitize``：path/image 只留 basename（分享训练包时隐藏本地目录结构——
    目录名可能泄露拍摄地/客户）；原始映射写 ``lr_paths.json``（仅本地，勿外发）。
    """
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "lr_records.jsonl")
    mapping: Dict[str, str] = {}
    with open(out, "w", encoding="utf-8") as f:
        for rec in records:
            line = dict(rec)
            orig = line.get("path")
            if sanitize and orig:
                mapping[orig] = os.path.basename(orig)
                line["path"] = os.path.basename(orig)
            if sanitize and line.get("catalog"):
                # 目录结构同样脱敏：只留会话名（如 3-29）
                line["catalog"] = os.path.basename(
                    os.path.dirname(os.path.dirname(line["catalog"]))) \
                    or line["catalog"]
            if images and orig in images:
                line["image"] = (os.path.basename(images[orig])
                                 if sanitize else images[orig])
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    if sanitize and mapping:
        with open(os.path.join(out_dir, "lr_paths.json"), "w",
                  encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
    return out


# ---------------------------------------------------------------- 训练数据

_RAW_EXT = (".arw", ".cr2", ".cr3", ".nef", ".nrw", ".dng", ".raf", ".rw2",
            ".orf", ".pef", ".srw", ".x3f")

# 自动基调回归的目标参数（9 项全局；裁剪/扶正是意图不学）
TARGETS: Tuple[str, ...] = ("exposure", "contrast", "saturation", "vibrance",
                            "wb_temp", "wb_tint", "clarity", "texture", "dehaze")
_NEUTRAL = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0,
            "vibrance": 0.0, "wb_temp": 5250.0, "wb_tint": 0.0,
            "clarity": 0.0, "texture": 0.0, "dehaze": 0.0}
_WB_SCALE = 500.0  # wb_temp 归一化：(-5250)/500


def _target_vector(options: Dict[str, Any]) -> List[float]:
    v = []
    for t in TARGETS:
        x = float(options.get(t, _NEUTRAL[t]))
        if t == "wb_temp":
            x = (x - 5250.0) / _WB_SCALE
        v.append(x)
    return v


def _target_options(vec: Sequence[float]) -> Dict[str, Any]:
    """预测向量 → ProcessOptions 可消费的 options dict。

    输出键直接对齐 ``ProcessOptions`` 字段（exposure 的字段名是 ``ev``，
    训练命名空间仍用 LR 的 "exposure"）——REST/MCP/CLI 拿到后零映射可套用。
    """
    out = {}
    for t, x in zip(TARGETS, vec):
        x = float(x)
        if t == "wb_temp":
            x = x * _WB_SCALE + 5250.0
            out[t] = int(round(x))
        else:
            out[t] = round(x, 3)
    if "exposure" in out:
        out["ev"] = out.pop("exposure")
    return out


def _content_features(img) -> List[float]:
    """内容特征：32-bin luma + 3×16-bin 通道直方图 + 4 统计 = 84 维。

    零依赖（numpy），作为 CLIP embedding 的平价替代；CLIP 升级路径见文档。
    """
    import numpy as np
    from PIL import Image as _PILImage
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    if max(h, w) > 256:
        scale = 256.0 / max(h, w)
        arr = np.asarray(img.resize((max(1, int(w * scale)),
                                     max(1, int(h * scale))),
                                    _PILImage.LANCZOS), dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    feats: List[float] = []
    hl, _ = np.histogram(luma, bins=32, range=(0, 255))
    feats.extend(float(c) / float(luma.size) for c in hl)
    for ch in (r, g, b):
        hc, _ = np.histogram(ch, bins=16, range=(0, 255))
        feats.extend(float(c) / float(ch.size) for c in hc)
    feats.extend([
        float(luma.mean()) / 255.0,
        float(luma.std()) / 255.0,
        float((np.maximum.reduce([r, g, b]) -
               np.minimum.reduce([r, g, b])).mean()) / 255.0,
        float(luma.std() / (luma.mean() + 1e-6)),
    ])
    # intercept column: the ridge solver assumed one was here (the comment
    # said "X 已含截距列" but no constant was appended) — without it the
    # fit is forced through the origin and biases every target
    feats.append(1.0)
    return feats


# 公开别名（v2.5 语义搜索的内置特征）：85 列 = 84 维 + 末位截距（岭回归
# 兼容）。语义检索请截掉最后一列——常量列污染 cosine。私有名保留：插件
# predictor 依赖它。
content_features = _content_features


def render_before_images(records: Sequence[Dict[str, Any]], out_dir: str,
                         max_side: int = 1536,
                         progress_callback: Optional[Callable] = None
                         ) -> Dict[str, Any]:
    """rawpy 默认渲染已编辑照片的 before 图 → ``<out_dir>/<原名>.jpg``。

    幂等（已存在跳过）；RAW 走 rawpy，非 RAW（如原片 JPEG）直接复制解码。
    返回 ``{rendered, skipped, failed, images: {path: jpg}}``。
    """
    import rawpy
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    images: Dict[str, str] = {}
    rendered = skipped = failed = 0
    edited = [r for r in records if r.get("edited")]
    total = len(edited)
    for i, rec in enumerate(edited):
        p = rec.get("path")
        if progress_callback:
            progress_callback(i, total, p or "", "rendering")
        if not p or not os.path.exists(p):
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        out = os.path.join(out_dir, stem + ".jpg")
        if os.path.exists(out):
            images[p] = out
            skipped += 1
            continue
        try:
            if p.lower().endswith(_RAW_EXT):
                with rawpy.imread(p) as raw:
                    img = Image.fromarray(raw.postprocess())
            else:
                img = Image.open(p)
                if img.mode != "RGB":
                    img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            # 隐私：显式剥 EXIF（Pillow 版本差异——旧版默认从 info 拷贝
            # exif，可能把原片 GPS 带进训练图集；不依赖默认行为）
            img.info.pop("exif", None)
            img.save(out, "JPEG", quality=90)
            images[p] = out
            rendered += 1
        except Exception:
            failed += 1
    return {"rendered": rendered, "skipped": skipped, "failed": failed,
            "images": images}


def load_training_data(records: Sequence[Dict[str, Any]],
                       image_dir: Optional[str] = None
                       ) -> Tuple[List[List[float]], List[List[float]],
                                  List[Dict[str, Any]], Dict[str, int]]:
    """records → (X, Y, metas, stats)。

    X = 84 维内容特征（image 键指向 before 图），Y = 9 项目标参数向量
    （含归一化）。缺图/无编辑的记录跳过。stats = {"edited", "missing",
    "loaded"} 供训练错误诊断——sanitize 包的 image 是 basename，
    ``image_dir`` 给定且路径相对时按它解析（绝对路径原样用）。"""
    from PIL import Image
    X: List[List[float]] = []
    Y: List[List[float]] = []
    metas: List[Dict[str, Any]] = []
    stats = {"edited": 0, "missing": 0, "loaded": 0}
    for rec in records:
        if not rec.get("edited"):
            continue
        stats["edited"] += 1
        img_path = rec.get("image")
        if img_path and image_dir and not os.path.isabs(img_path):
            # sanitize 包内 image 是 basename：按 image_dir 解析；
            # 绝对路径（lr-merge 后）原样使用
            joined = os.path.join(image_dir, img_path)
            if os.path.exists(joined):
                img_path = joined
        if not img_path and image_dir:
            img_path = os.path.join(image_dir, os.path.splitext(
                os.path.basename(rec["path"]))[0] + ".jpg")
        if not img_path or not os.path.exists(img_path):
            stats["missing"] += 1
            continue
        try:
            img = Image.open(img_path)
        except Exception:
            stats["missing"] += 1
            continue
        stats["loaded"] += 1
        X.append(_content_features(img))
        Y.append(_target_vector(rec.get("options") or {}))
        metas.append({"path": rec.get("path"), "image": img_path,
                      "options": rec.get("options")})
    return X, Y, metas, stats


def train_auto_tone(records: Sequence[Dict[str, Any]],
                    out_path: str,
                    image_dir: Optional[str] = None,
                    ridge_lambda: float = 1.0) -> Dict[str, Any]:
    """岭回归自动基调模型（闭式解，纯 numpy）→ model.npz。

    Y = 9 项全局参数（TARGETS）；X = 84 维内容特征。小数据、零依赖、
    任何机器可训——是 CLIP+MLP 路线的可落地基线（升级路径见模块 docstring）。
    """
    import numpy as np
    X, Y, _m, stats = load_training_data(records, image_dir)
    if len(X) < 30:
        raise LrError(
            f"训练样本不足（{len(X)} < 30：共 {len(records)} 条记录、"
            f"{stats['edited']} 条已编辑、{stats['missing']} 条缺图跳过）"
            f"——sanitize 数据包需 --images 指 before 图目录，或先 lr-merge")
    Xa = np.asarray(X, dtype=np.float64)
    Ya = np.asarray(Y, dtype=np.float64)
    n, d = Xa.shape
    # 闭式解 (XᵀX + λI)⁻¹XᵀY；截距列由 _content_features 追加（λI 的
    # 截距行/列保持单位值，正则化不作用于常数项的均值估计——小数据下
    # 简化处理，可接受）
    A = Xa.T @ Xa + ridge_lambda * np.eye(d)
    W = np.linalg.solve(A, Xa.T @ Ya)
    pred = Xa @ W
    ss_res = float(((Ya - pred) ** 2).sum())
    ss_tot = float(((Ya - Ya.mean(axis=0)) ** 2).sum())
    r2 = 1.0 - ss_res / max(ss_tot, 1e-12)
    np.savez(out_path, W=W, targets=np.array(TARGETS),
             n_samples=n, ridge_lambda=ridge_lambda)
    return {"samples": n, "dims": d, "r2": round(r2, 3), "out": out_path}


def _predict_clip_mlp(img, model) -> np.ndarray:
    """CLIP+MLP 前向（tools/train_tone_torch.py 产物）。

    格式自适应：新版保存 W (emb, hidden) / W2 (hidden, 9)；旧版为转置存储。
    npz 内的 clip_model/clip_pretrained 缺省 ViT-L-14/openai。torch/open_clip
    惰性导入，缺失抛清晰 LrError（区别于岭回归分支的零依赖）。
    """
    import numpy as np
    try:
        import torch
        import open_clip
    except ImportError:
        raise LrError(
            "CLIP+MLP 模型需要 torch + open-clip-torch（pip install torch "
            "open-clip-torch），或改用 photo-s lr-train 岭回归模型（零依赖）")
    emb_dim = int(model["emb_dim"]) if "emb_dim" in model.files else None
    for key in ("W", "b", "W2", "b2"):
        if key not in model.files:
            raise LrError(
                f"CLIP+MLP 模型缺少 {key} 键（损坏或不完整 npz）")
    W, b, W2, b2 = model["W"], model["b"], model["W2"], model["b2"]
    clip_model = (str(model["clip_model"])
                  if "clip_model" in model.files else "ViT-L-14")
    pretrained = (str(model["clip_pretrained"])
                  if "clip_pretrained" in model.files else "openai")
    try:
        clip, _, preprocess = open_clip.create_model_and_transforms(
            clip_model, pretrained=pretrained)
    except Exception as e:
        # open_clip 预训练权重首次使用联网下载，失败是裸 urllib 异常
        raise LrError(
            f"CLIP 预训练权重加载失败（{clip_model}/{pretrained}，"
            f"首次使用需联网下载）: {e}") from None
    clip.eval()
    with torch.no_grad():
        f = clip.encode_image(preprocess(img.convert("RGB")).unsqueeze(0))
        f = np.asarray(f.squeeze(0).numpy(), dtype=np.float64)
    # 权重方向自适应：emb_dim 键优先，否则按特征维推断（新旧格式兼容）
    if emb_dim is not None:
        if W.shape[0] == emb_dim:
            pass
        elif W.shape[1] == emb_dim:
            W, W2 = W.T, W2.T
        else:
            raise LrError(f"模型权重维度不匹配 emb_dim={emb_dim}, W={W.shape}")
    elif W.shape[1] == f.shape[0]:
        W, W2 = W.T, W2.T
    elif W.shape[0] != f.shape[0]:
        raise LrError(f"模型权重维度不匹配特征 {f.shape[0]}, W={W.shape}")
    h = np.maximum(f @ W + b, 0.0)
    return h @ W2 + b2


def predict_auto_tone(image_path: str, model_path: str) -> Dict[str, Any]:
    """模型推理：图片 → 9 项全局参数 options。

    自动识别两种模型格式：
    - 岭回归（photo-s lr-train 输出）：84 维内容特征 @ W，纯 numpy 零依赖
    - CLIP+MLP（tools/train_tone_torch.py 输出，含 W2/b2）：768 维 CLIP
      embedding + 双层 MLP，需 torch + open-clip-torch（缺依赖报清晰 LrError）
    """
    import numpy as np
    from PIL import Image
    if not os.path.exists(model_path):
        raise LrError(
            f"模型文件不存在: {model_path}（先 photo-s lr-train，"
            f"或 --model 指定 npz）")
    if not os.path.exists(image_path):
        raise LrError(f"图片不存在: {image_path}")
    try:
        model = np.load(model_path)
    except Exception as e:
        raise LrError(f"模型加载失败 {model_path}: {e}") from None
    if "targets" not in model.files:
        raise LrError(
            f"模型缺少 targets 键（{model_path} 不是 lr-train 产物）")
    targets = model["targets"]
    if tuple(targets) != TARGETS:
        raise LrError(f"模型目标不匹配 {tuple(targets)} != {TARGETS}")
    img = Image.open(image_path)
    if "W2" in model.files and "b2" in model.files:
        vec = _predict_clip_mlp(img, model)
    else:
        if "W" not in model.files:
            raise LrError(f"模型缺少 W 键（{model_path}）")
        x = np.asarray(_content_features(img), dtype=np.float64)
        W = model["W"]
        if W.ndim != 2 or W.shape[0] != len(x):
            raise LrError(
                f"模型权重维度不匹配特征 {len(x)} 维, W={W.shape}")
        vec = x @ W
    opts = _target_options(vec)
    return {"path": image_path, "options": opts}


def cluster_recipes(records: Sequence[Dict[str, Any]], k: int = 6,
                    seed: int = 0) -> Dict[str, Any]:
    """编辑配方聚类（numpy KMeans，参数向量空间）→ 个人风格配方库。

    每簇中心 = PhotoS options（可直接 preset）；输出簇大小/占比/代表照片。
    """
    import numpy as np
    vecs = []
    metas = []
    for rec in records:
        if not rec.get("edited"):
            continue
        o = rec.get("options") or {}
        if not o:
            continue
        vecs.append(_target_vector(o))
        metas.append({"path": rec.get("path")})
    if len(vecs) < k * 2:
        raise LrError(f"可聚类样本不足（{len(vecs)} < {k * 2}）")
    X = np.asarray(vecs, dtype=np.float64)
    # 按列归一化（标准差）
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0
    Xs = X / scale
    rng = np.random.default_rng(seed)
    n = len(Xs)
    # KMeans++ 初始化
    centers = [Xs[int(rng.integers(n))]]
    for _ in range(1, k):
        dist = np.min([((Xs - c) ** 2).sum(axis=1) for c in centers], axis=0)
        probs = dist / dist.sum()
        centers.append(Xs[int(rng.choice(n, p=probs))])
    centers = np.asarray(centers)
    for _ in range(50):
        labels = np.argmin([((Xs - c) ** 2).sum(axis=1) for c in centers],
                           axis=0)
        new_centers = np.array([Xs[labels == j].mean(axis=0) if (labels == j)
                                .any() else centers[j] for j in range(k)])
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    clusters = []
    for j in range(k):
        idx = np.where(labels == j)[0]
        center = centers[j] * scale
        clusters.append({
            "size": int(len(idx)),
            "ratio": round(float(len(idx)) / n, 3),
            "options": _target_options(center),
            "examples": [metas[i]["path"] for i in idx[:3]],
        })
    return {"k": k, "samples": n,
            "clusters": sorted(clusters, key=lambda c: -c["size"])}


def similar_photos(query_path: str, records: Sequence[Dict[str, Any]],
                   image_dir: Optional[str] = None, k: int = 5,
                   ) -> List[Dict[str, Any]]:
    """相似修图检索：内容特征（84 维直方图特征）L1 距离 kNN。

    新图 → 最像的既往修图 → 其 options 即修图起点。CLIP embedding 为
    升级路径（换特征即得），本实现零依赖。
    """
    from PIL import Image
    q = _content_features(Image.open(query_path))
    q_stem = os.path.splitext(os.path.basename(query_path))[0]
    scored = []
    for rec in records:
        if not rec.get("edited"):
            continue
        if os.path.splitext(os.path.basename(rec.get("path") or ""))[0] \
                == q_stem:
            continue  # 排除查询图自身
        img_path = rec.get("image")
        if not img_path and image_dir:
            img_path = os.path.join(image_dir, os.path.splitext(
                os.path.basename(rec["path"]))[0] + ".jpg")
        if not img_path or not os.path.exists(img_path):
            continue
        try:
            f = _content_features(Image.open(img_path))
        except Exception:
            continue
        dist = sum(abs(a - b) for a, b in zip(q, f))
        scored.append({"path": rec.get("path"), "distance": round(dist, 4),
                       "options": rec.get("options")})
    scored.sort(key=lambda s: s["distance"])
    return scored[:k]


def prep_eval_set(records: Sequence[Dict[str, Any]], out_path: str,
                  image_dir: Optional[str] = None, sample: int = 200,
                  seed: int = 1) -> Dict[str, Any]:
    """教师评测集：采样已编辑照片 → before/after 渲染对 + 打分 prompt。

    after 由 PhotoS 自己渲染（options → process_image），因此评测集是
    自包含的：before.jpg + after.jpg + options + 教师打分模板。
    """
    import random
    from PIL import Image
    from .engine import ProcessOptions, process_image
    edited = [r for r in records if r.get("edited") and r.get("options")]
    if len(edited) > sample:
        rng = random.Random(seed)
        edited = rng.sample(edited, sample)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    done = 0
    for rec in edited:
        src = rec.get("path")
        if not src or not os.path.exists(src):
            continue
        stem = os.path.splitext(os.path.basename(src))[0]
        before = os.path.join(out_dir, f"eval_before_{stem}.jpg")
        after = os.path.join(out_dir, f"eval_after_{stem}.jpg")
        try:
            if not os.path.exists(before):
                # before = 已渲染图（rawpy 默认显影），PIL 开不了 RAW
                bsrc = rec.get("image") or src
                if not os.path.exists(bsrc):
                    continue
                img = Image.open(bsrc)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.thumbnail((1024, 1024))
                img.save(before, "JPEG", quality=88)
            if not os.path.exists(after):
                rec_opts = dict(rec.get("options") or {})
                # 记录是 LR 命名空间（exposure）；ProcessOptions 字段是 ev——
                # 不映射会被 dataclass 过滤静默丢弃，after 渲染缺曝光编辑
                if "exposure" in rec_opts and "ev" not in rec_opts:
                    rec_opts["ev"] = rec_opts.pop("exposure")
                fields = {k: v for k, v in rec_opts.items()
                          if k in ProcessOptions.__dataclass_fields__}
                fields["output_dir"] = out_dir  # 中间产物不污染源目录
                res = process_image(src, ProcessOptions(**fields))
                if res.success and res.output_path:
                    img = Image.open(res.output_path)
                    img.thumbnail((1024, 1024))
                    img.save(after, "JPEG", quality=88)
            entries.append({"path": src, "before": before, "after": after,
                            "options": rec.get("options")})
            done += 1
        except Exception:
            continue
    prompt = (
        "# 教师美学评估任务\n\n"
        "对每对图片（before=原图，after=PhotoS 修图结果）评分：\n\n"
        "1. **提升度 1-10**：after 相对 before 的美学提升（构图/曝光/色彩/氛围）\n"
        "2. **问题清单**：after 中的新缺陷（过曝、色偏、不自然、丢失细节）\n"
        "3. **一句话点评**：这张图是否达到出片标准\n\n"
        "输出 JSON：{\"path\": ..., \"score\": N, \"issues\": [...], \"verdict\": ...}\n"
    )
    with open(os.path.join(out_dir, "eval_prompt.md"), "w",
              encoding="utf-8") as f:
        f.write(prompt)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"count": len(entries), "prompt_file": "eval_prompt.md",
                   "entries": entries}, f, ensure_ascii=False, indent=2)
    return {"count": len(entries), "out": out_path}
