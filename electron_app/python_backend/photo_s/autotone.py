"""photo_s/autotone.py — auto-tone 插件参数 → 引擎管线应用（v2.4 词汇表扩展）

旧接线里引擎槽位拿到的只是插件 numpy 简化渲染后的像素——9 个预测字段
实际只落了 exposure/contrast/saturation 三个（vibrance/WB/clarity/
texture/dehaze 被静默丢弃），局部调整则完全没有通道。本模块把插件的
**参数**（而非像素）接进引擎真实管线：

- 9 个全局字段按引擎调色顺序应用（数值语义与 GUI 覆盖层路径一致）；
- ``local`` 局部调整（``[{region, params}]``）转成 masks/mask_adjust
  紧凑字符串走蒙版管线——与手动蒙版同一套代码，数值一致，可进
  preset/REST/MCP 零胶水传递。

region 词汇表 = v1.8 AI 蒙版：``subject`` / ``person`` / ``object:label``。
params 词汇表 = mask.py :data:`ADJUST_KEYS` 的标量子集
（exposure/contrast/saturation/vibrance/clarity/texture/...）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["apply_auto_tone_params", "resolve_auto_tone_options",
           "local_to_specs", "NEUTRAL"]

# 各字段中性值（= 不改图）。与插件 render.NEUTRAL 对齐——wb 中性 5250K
# 是 lrxmp 训练基线（LR AsShot 平均），非 0。
NEUTRAL: Dict[str, float] = {
    "exposure": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "vibrance": 0.0,
    "wb_temp": 5250.0,
    "wb_tint": 0.0,
    "clarity": 0.0,
    "texture": 0.0,
    "dehaze": 0.0,
}

# 引擎各字段应用顺序（镜像 engine 管线：tone → WB → 曝光 → clarity →
# texture → dehaze → vibrance），保证与全局 options 渲染次序一致。
_ORDER = ("contrast", "saturation", "wb_temp", "wb_tint", "exposure",
          "clarity", "texture", "dehaze", "vibrance")

_EPS = 1e-4


def _is_neutral(field: str, value: float) -> bool:
    n = NEUTRAL.get(field, 0.0)
    return abs(float(value) - n) < _EPS


def local_to_specs(local: List[Dict[str, Any]],
                   prefix: str = "ai") -> Tuple[str, str]:
    """局部调整列表 → (masks, mask_adjust) 紧凑字符串。

    ``[{"region": "subject", "params": {"exposure": -0.3}}]`` →
    ``("ai0:subject", "ai0:exposure=-0.3")``。中性参数被剔除；整个
    params 都中性则该条目跳过（不产生空调整）。非法 region/params 键
    抛 :class:`ValueError`——预测端 bug 不应静默降级。
    """
    from .mask import ADJUST_KEYS

    masks: List[str] = []
    adjusts: List[str] = []
    idx = 0
    for item in local or ():
        region = str(item.get("region") or "").strip()
        if not region:
            raise ValueError(f"local adjustment missing region: {item!r}")
        params = item.get("params") or {}
        kv = []
        for k, v in params.items():
            if k not in ADJUST_KEYS:
                raise ValueError(
                    f"unknown local param {k!r} (allowed: {sorted(ADJUST_KEYS)})")
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"non-numeric param {k}={v!r}") from None
            if not _is_neutral(k, v):
                kv.append(f"{k}={v:.4g}")
        if not kv:
            continue  # 全中性：无蒙版必要
        name = f"{prefix}{idx}"
        masks.append(f"{name}:{region}")
        adjusts.append(f"{name}:{','.join(kv)}")
        idx += 1
    return ";".join(masks), ";".join(adjusts)


def apply_auto_tone_params(img, params: Optional[Dict[str, Any]]):
    """把 :meth:`AutoTonePlugin.auto_tone_params` 的返回应用到图像。

    ``params``: ``{"options": {9 字段}, "local": [{region, params}], ...}``
    （options/local 缺省安全）。返回处理后的 PIL.Image；中性字段跳过，
    单字段失败抛出（与引擎其余调色阶段同策略——不静默吞错）。
    """
    if not params:
        return img
    opts = params.get("options") or {}

    # 全局字段：按引擎顺序逐个应用
    for field in _ORDER:
        if field not in opts or _is_neutral(field, opts[field]):
            continue
        v = float(opts[field])
        if field == "contrast":
            from .adjust import apply_tone_adjustments
            img = apply_tone_adjustments(img, contrast=v)
        elif field == "saturation":
            from .adjust import apply_tone_adjustments
            img = apply_tone_adjustments(img, saturation=v)
        elif field == "wb_temp":
            from .adjust import apply_white_balance
            img = apply_white_balance(img, temp=v)
        elif field == "wb_tint":
            from .adjust import apply_white_balance
            img = apply_white_balance(img, tint=v)
        elif field == "exposure":
            from .adjust import apply_exposure
            img = apply_exposure(img, ev=v)
        elif field == "clarity":
            from .grade import apply_clarity
            img = apply_clarity(img, v)
        elif field == "texture":
            from .grade import apply_texture
            img = apply_texture(img, v)
        elif field == "dehaze":
            from .grade import apply_dehaze
            img = apply_dehaze(img, v)
        elif field == "vibrance":
            from .grade import apply_vibrance
            img = apply_vibrance(img, v)

    # 局部调整：紧凑字符串 → 蒙版管线（与 options.mask_adjust 同一实现）
    local = params.get("local") or []
    if local:
        masks_s, adjust_s = local_to_specs(local)
        if masks_s:
            from .mask import apply_local, parse_mask_adjust, parse_masks, render_mask
            adjusts = parse_mask_adjust(adjust_s)
            specs = {s.name: s for s in parse_masks(masks_s)}
            for name, adjust in adjusts.items():
                spec = specs[name]
                m = render_mask(spec, img.width, img.height, img=img,
                                refs=specs)
                img = apply_local(img, m, adjust)
    return img


# 预测 9 字段 → ProcessOptions 字段名（唯一差异：exposure → ev）
_PARAM_TO_FIELD = {
    "exposure": "ev", "contrast": "contrast", "saturation": "saturation",
    "vibrance": "vibrance", "wb_temp": "wb_temp", "wb_tint": "wb_tint",
    "clarity": "clarity", "texture": "texture", "dehaze": "dehaze",
}


def resolve_auto_tone_options(options, input_path=None):
    """预测一次 auto-tone 参数并合并进 ProcessOptions → (merged, params)。

    与引擎槽位路径（apply_auto_tone_params 的像素协议）数值等价：中性字段
    跳过、用户显式设置的字段不被预测覆盖（含 WB）、局部调整走
    local_to_specs 并入 masks/mask_adjust（ai0/ai1… 命名与手动蒙版天然
    错开）。``auto_tone`` 置 None——引擎不再二次推理，合并结果即"真实
    应用的参数"，XMP sidecar（batch --write-xmp / autopilot）记录的与
    实际渲染的完全一致。

    缺插件抛 RuntimeError（与引擎槽位同一文案 + suggest 替代指引）。
    """
    from .plugin import find_provider

    provider = find_provider("auto_tone")
    if provider is None or not hasattr(provider, "auto_tone_params"):
        raise RuntimeError(
            "--auto-tone needs the auto-tone plugin "
            "(pip install photo-s-plugin-auto-tone); zero-model rule-based "
            "alternative: 'photo-s suggest'")

    ctx = None
    if input_path is not None:
        from .hooks import PluginContext
        ctx = PluginContext(input_path=str(input_path), options=options)
    params = provider.auto_tone_params(float(options.auto_tone or 1.0), ctx)

    changes: Dict[str, Any] = {}
    for key, value in (params.get("options") or {}).items():
        field = _PARAM_TO_FIELD.get(key)
        if field is None or value is None:
            continue
        value = float(value)
        if _is_neutral(key, value):
            continue
        if field == "wb_temp" and options.wb_temp is not None:
            continue  # 用户显式 WB 优先（与槽位路径的叠加语义一致）
        if field == "wb_tint" and options.wb_tint:
            continue
        changes[field] = value

    masks_s, adjust_s = local_to_specs(params.get("local") or [])
    if masks_s:
        changes["masks"] = ";".join(
            [s for s in (options.masks or "", masks_s) if s])
        changes["mask_adjust"] = ";".join(
            [s for s in (options.mask_adjust or "", adjust_s) if s])
    changes["auto_tone"] = None
    return dataclasses.replace(options, **changes), params
