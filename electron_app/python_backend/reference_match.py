"""reference_match.py — 参考匹配：从参考图统计卡反推本照片初始参数

流程（标注基建 + 参数化仿色产品功能二合一）：
  参考图(获奖照等) --photo_stats--> 统计卡
  本照片统计卡 vs 参考统计卡 --反向拟合--> 初始编辑参数（只含非默认键）
  人工在编辑器里微调 --journal--> (照片, 最终参数) 训练对

红线：参考图只做本地统计与人眼参照，不训练其像素、不二次分发。

反向拟合按 photo_editor 的真实响应闭式/经验式反推：
  exposure: ImageEnhance.Brightness(1+0.8·ev) 线性乘法 → ev = (ref_key/my_key − 1)/0.8
  contrast: PIL Enhance 因子；线性曝光会等比放大散度，先扣掉亮度比再解残差
  warmth:   通道增益 ±30% → Δcast ≈ w·0.3·(med_r+med_b)，取典型中灰标定常数
  wb_tint / vibrance / hsl / color_grading: 同理经验常数，冒烟测试实证方向与量级
"""

import math

import numpy as np
from PIL import Image

from photo_stats import HUE_BANDS, _rgb_to_hsv, compute_stats


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _fit_exposure(my_key, ref_key, k):
    """线性亮度响应闭式反解：factor = 1 + 0.8·ev → ev = (ref/my − 1)/0.8。"""
    if my_key <= 0.02 or ref_key <= 0.02:
        return 0.0
    return _clamp((ref_key / my_key - 1.0) / 0.8, -1.5, 1.5) * k


def _fit_contrast(my_c, ref_c, key_ratio, k):
    """PIL Enhance 因子反解：先扣掉线性曝光对散度的等比放大，余量才是对比度。"""
    if my_c <= 0.02 or key_ratio <= 0.01:
        return 0.0
    expected = my_c * key_ratio           # 只动曝光时散度应到的位置
    ratio = ref_c / expected
    return _clamp((ratio - 1.0) * k + 1.0, 0.6, 1.6)


def _cast_hue(cast_warm, cast_green):
    """色偏向量 (warm=R−B, green=G−(R+B)/2) → 色相角（度）与幅值。"""
    rgb = np.array([[1.0 + cast_warm, 1.0 + cast_green, 1.0 - cast_warm]])
    rgb = np.clip(rgb, 0.02, 2.0)
    rgb = rgb / rgb.max()
    hue, sat, _ = _rgb_to_hsv(rgb)
    return float(hue[0]), float(sat[0])


def suggest_edits(my_img, ref_stats, strength=1.0, my_stats=None):
    """本照片 PIL Image + 参考统计卡 → (非默认编辑参数 dict, 本照片统计卡)。

    strength: 0..1.5 参考强度（全部差值统一缩放，UI 可做成滑杆）。
    my_stats: 可选的预先计算统计卡，避免调用方已经算过时重复解码/统计。
    """
    k = max(0.0, min(1.5, float(strength or 1.0)))
    my = my_stats if my_stats is not None else compute_stats(my_img)
    edits = {}

    key_ratio = ref_stats["key"] / my["key"] if my["key"] > 1e-6 else 1.0
    e = _fit_exposure(my["key"], ref_stats["key"], k)
    if abs(e) > 0.02:
        edits["exposure"] = round(e, 2)

    c = _fit_contrast(my["contrast"], ref_stats["contrast"], key_ratio, k)
    if abs(c - 1.0) > 0.03:
        edits["contrast"] = round(c, 3)

    dw = (ref_stats["cast_warm"] - my["cast_warm"]) * k
    if abs(dw) > 0.03:  # 阈值在真编辑量级(≈0.085)之上，滤掉线性提亮的剪裁伪影
        edits["warmth"] = round(_clamp(dw * 3.5, -1.0, 1.0), 2)  # Δcast≈0.3·w·(med_r+med_b)≈0.28w

    dg = -((ref_stats["cast_green"] - my["cast_green"]) * k)  # 偏绿 → 负 tint
    if abs(dg) > 0.015:
        edits["wb_tint"] = int(round(_clamp(dg * 400.0, -60.0, 60.0)))

    # ── 饱和：全局均值差走 vibrance（护肤色），分带残差走 HSL ──
    diffs = [ref_stats["sat_profile"][b] - my["sat_profile"][b] for b in HUE_BANDS]
    mean_d = sum(diffs) / len(diffs)
    vib = mean_d * 2.0 * k
    if abs(vib) > 0.03:
        edits["vibrance"] = round(_clamp(vib, -0.5, 0.6), 2)

    hsl = {}
    for band, d in zip(HUE_BANDS, diffs):
        resid = (d - mean_d) * 1.2 * k
        if abs(resid) > 0.06:
            hsl[band] = [0, round(_clamp(resid, -0.6, 0.6), 2), 0]
    if hsl:
        edits["hsl"] = hsl

    # ── 阴影/高光分色：分区色偏差 → [hue°, sat 0..0.5]。
    #    两侧分区像素占比都 ≥3% 才比较——纯提亮会让分区"无中生有"，
    #    那是曝光归因不是分级（zone_frac 由 photo_stats 输出） ──
    zones = {}
    for zone, key_name, frac_name in (("shadows", "shadow_cast", "shadow_frac"),
                                      ("highlights", "highlight_cast", "highlight_frac")):
        if my.get(frac_name, 0.0) < 0.03 or ref_stats.get(frac_name, 0.0) < 0.03:
            continue
        zw = (ref_stats[key_name][0] - my[key_name][0]) * k
        zg = (ref_stats[key_name][1] - my[key_name][1]) * k
        m = math.hypot(zw, zg)
        if m > 0.015:
            hue, _ = _cast_hue(zw, zg)
            # Highlights carry skin: a hue pull there turns faces magenta even
            # at moderate strength, so keep highlight grading to a whisper.
            # 8x/0.5 pinned both zones at 0.5 and looked neon in real photos.
            if zone == "highlights":
                sat = _clamp(m * 1.2, 0.02, 0.05)
            else:
                sat = _clamp(m * 3.0, 0.05, 0.12)
            zones[zone] = [round(hue), round(sat, 2)]
    if zones:
        edits["color_grading"] = zones

    return edits, my


# 自适应预设只接管这四项「基线」参数；风格键（曲线/HSL/清晰度/蒙版等）
# 仍由预设本身决定，避免同一效果被反解和预设叠加两次。
ADAPTIVE_KEYS = ("exposure", "contrast", "warmth", "wb_tint")
ADAPTIVE_EXPOSURE_CAP = 1.0  # 基线归一不负责把夜景拉成白天；风格仍由预设决定


def adaptive_baseline_edits(my_img, source_stats=None, strength=1.0):
    """预设批量应用前的每张照片基线归一 → 只含 exposure/contrast/warmth/wb_tint。

    source_stats: 用户预设保存时记录的「来源照片未编辑统计卡」。有它时把目标
    拉到来源照片的基线（同一起点），再套预设；这是真正的 look transfer。
    没有 source_stats（内置预设/旧用户预设）时只做曝光护栏：中位亮度偏离
    0.30–0.60 才拉回区间，对比度与白平衡保持原样，避免把日落/夜景洗成中性。
    """
    my = compute_stats(my_img)
    if source_stats:
        try:
            edits, _ = suggest_edits(
                my_img, source_stats,
                min(1.5, max(0.0, float(strength or 1.0))), my_stats=my)
            out = {k: edits[k] for k in ADAPTIVE_KEYS if k in edits}
            if "exposure" in out:
                out["exposure"] = round(_clamp(out["exposure"],
                                               -ADAPTIVE_EXPOSURE_CAP,
                                               ADAPTIVE_EXPOSURE_CAP), 2)
            return out
        except Exception:
            pass  # 统计卡损坏 → 退回曝光护栏
    key = _clamp(float(my.get("key") or 0.0), 0.30, 0.60)
    e = _fit_exposure(my["key"], key, min(1.0, max(0.0, float(strength or 1.0))))
    e = _clamp(e, -ADAPTIVE_EXPOSURE_CAP, ADAPTIVE_EXPOSURE_CAP)
    return {"exposure": round(e, 2)} if abs(e) > 0.02 else {}


def verify_match(edited_img, ref_stats):
    """编辑后的图 vs 参考统计卡 → 匹配距离（1.0 ≈ 差一个典型单位）。"""
    from photo_stats import stats_distance
    return stats_distance(compute_stats(edited_img), ref_stats)


def _load_rgb(path):
    """PIL 直开转 RGB；RAW（arw/cr2/nef…）PIL 解不了时回退编辑器内嵌预览加载器。"""
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception:
        from photo_editor import _load_image_rgb  # 延迟导入，避免模块环
        return _load_image_rgb(path)


# ── IPC（P0-1 注册表） ──────────────────────────────────────────────────────

import command_registry  # noqa: E402
from command_registry import command  # noqa: E402


@command("reference_suggest_params", "editor",
         "[编辑器] 参考匹配：给参考图统计卡（或参考图路径），反推本照片初始编辑参数。"
         "产出的参数只含非默认键，由前端装回编辑器供人工微调。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "本照片绝对路径"},
             "reference_path": {"type": "string",
                                "description": "参考图路径（与 reference_stats 二选一，优先用统计卡）"},
             "reference_stats": {"type": "object", "description": "参考图统计卡（photo_stats.compute_stats 产物）"},
             "strength": {"type": "number", "description": "参考强度 0..1.5，默认 1"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_reference_suggest_params(params):
    p = params or {}
    path = p.get("path")
    ref_stats = p.get("reference_stats")
    if not ref_stats:
        ref_path = p.get("reference_path")
        if not ref_path:
            return {"ok": False, "error": "need reference_path or reference_stats"}
        ref_stats = compute_stats(_load_rgb(ref_path))
    img = _load_rgb(path)
    edits, my_stats = suggest_edits(img, ref_stats, p.get("strength", 1.0))
    out = {"ok": True, "edits": edits, "my_stats": my_stats,
           "reference_stats": ref_stats}
    # P1-3b 匹配距离：应用建议参数前后各算一次统计距离（越小越接近参考）。
    # "后"在 ≤256px 缩略图上走完整 apply_edits 管线，与真实渲染口径一致；
    # 任何失败只缺 after 字段，不影响参数返回。
    try:
        from photo_stats import stats_distance
        out["distance"] = {"before": round(stats_distance(my_stats, ref_stats), 3),
                           "after": None}
        if edits:
            from photo_editor import DEFAULT_EDITS, apply_edits  # 延迟导入
            small = img.copy()
            small.thumbnail((256, 256), Image.BILINEAR)
            full = dict(DEFAULT_EDITS)
            full.update(edits)
            out["distance"]["after"] = round(
                verify_match(apply_edits(small, full), ref_stats), 3)
    except Exception:
        pass
    return out


def register_handlers():
    command_registry.register_all()
    try:
        from service import send_log
        send_log("reference_match module loaded (IPC via command_registry)", "info")
    except Exception:
        pass
