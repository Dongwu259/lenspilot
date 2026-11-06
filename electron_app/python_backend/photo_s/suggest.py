"""photo_s/suggest.py — 规则型参数推荐（v2.3.0，零模型零依赖）

把 ``metrics.analyze_image`` 的统计量映射为保守的 ``ProcessOptions`` 调整建议，
每条建议附**理由 + 依据指标**（可解释）。这是 agent 闭环 ``analyze → suggest →
process → audit`` 里缺的中间一环：无网络/无插件时的参数推荐保底层。

与 auto-tone 插件的分工（见 docs/AGENT_API.md）：
    suggest    = 确定性规则层：快、离线、可解释，修"客观偏差"（曝光/白平衡/对比）
    auto-tone  = 个人风格 AI 层（插件，4.6MB 权重）：预测"风格参数"，suggest 不做风格

设计约束：
- **保守**：只在指标明确偏离时建议；中性图返回 ``suggested={}``（``neutral=True``），
  agent 拿到空建议即知"客观上没什么可修的"。
- **ProcessOptions 直接可用**：``suggested`` 的键就是引擎字段名（ev / wb_temp /
  wb_tint / contrast / vibrance / clarity / highlight_recovery / levels），
  REST ``/process``、MCP ``process``、``--preset`` 均可直接吃。
- ``scale``（0-1）整体缩放数值建议的幅度——agent 想更温和就 0.5。
- 每张图独立、无状态、纯 stdlib + analyze 结果（不重复解码）。
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["suggest_params", "suggest_file"]

# 中性参考值（与 metrics.analyze_image 的输出语义对齐）
_NEUTRAL_KELVIN = 6500.0
_KELVIN_DEADBAND = 700.0        # 估计色温偏离中性超过此值才建议矫正
_TINT_DEADBAND = 4.0            # G-M 偏离（0-255 尺度）
_LUMA_TARGET = 0.5
_EV_MIN = 0.15                  # 小于 0.15 档不值得动
_CONTRAST_FLOOR = 0.10          # luma std/255 低于此 → 轻加对比
_SAT_FLOOR = 0.15               # 平均饱和度低于此 → vibrance（护肤色，不用全局饱和）
_BLUR_FLOOR = 0.05              # audit 的 blur_min 同款阈值


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round(v: float, nd: int = 2) -> float:
    return round(float(v), nd)


def suggest_params(analysis: dict, scale: float = 1.0) -> dict:
    """``analyze_image`` 的输出 → ``{suggested, reasons, neutral}``。

    ``suggested``：ProcessOptions 字段 → 建议值（只含偏离项；中性图为空 dict）。
    ``reasons``：每条 ``{field, metric, value, advice}``——人能读，agent 能转述。
    """
    scale = _clamp(float(scale or 1.0), 0.0, 1.0)
    suggested: Dict[str, Any] = {}
    reasons: List[dict] = []

    def advise(field: str, value, metric: str, mval, advice: str):
        suggested[field] = value
        reasons.append({"field": field, "metric": metric,
                        "value": mval, "advice": advice})

    stats = analysis.get("stats") or {}
    exposure = analysis.get("exposure") or {}
    wb = analysis.get("white_balance") or {}
    lum = float(exposure.get("luminance", 0.5) or 0.5)
    over = float(exposure.get("overexposed_pct", 0.0) or 0.0)
    under = float(exposure.get("underexposed_pct", 0.0) or 0.0)

    # ── 曝光：EV 把平均亮度拉回 ~0.5，按 2^EV 换算档数 ──────────────────────
    if lum > 0.05:
        import math
        need_stops = math.log2(_LUMA_TARGET / lum) * scale
        if abs(need_stops) >= _EV_MIN:
            ev = _clamp(need_stops, -1.0, 1.0)
            advise("ev", _round(ev, 2), "exposure.luminance", lum,
                   "mean luminance {} → target ~0.5, {:+.2f} stops".format(
                       _round(lum, 3), ev))

    # ── 高光死白：过曝占比高时优先拉回（比整体压暗更保主体）──────────────────
    if over > 2.0:
        hr = _clamp(over / 20.0 * scale, 0.1, 0.6)
        advise("highlight_recovery", _round(hr, 2), "exposure.overexposed_pct",
               over,
               "{:.1f}% pixels clipped → recover highlights ({})".format(
                   over, _round(hr, 2)))

    # ── 白平衡：估计色温直接作为矫正温度（引擎语义：把该色温光源拉回 6500K）──
    kelvin = int(wb.get("kelvin_estimate", _NEUTRAL_KELVIN) or _NEUTRAL_KELVIN)
    if abs(kelvin - _NEUTRAL_KELVIN) > _KELVIN_DEADBAND:
        # scale 温和化：向 6500 靠拢的部分按 scale 折算
        k = _NEUTRAL_KELVIN + (kelvin - _NEUTRAL_KELVIN) * scale
        k = int(round(k / 50.0) * 50)
        advise("wb_temp", k, "white_balance.kelvin_estimate", kelvin,
               "WB leans {} (est. {}K) → correct with wb_temp={}K".format(
                   "cool" if kelvin > _NEUTRAL_KELVIN else "warm",
                   kelvin, k))

    tint = float(wb.get("tint_gm", 0.0) or 0.0)
    if abs(tint) > _TINT_DEADBAND:
        # 图偏绿(tint>0) → 补洋红(wb_tint 正方向)；偏洋红 → 补绿
        t = _clamp(tint * 1.2 * scale, -15.0, 15.0)
        advise("wb_tint", _round(t, 1), "white_balance.tint_gm", tint,
               "{} lean ({:+.1f}) → wb_tint {:+.1f} to cancel".format(
                   "green" if tint > 0 else "magenta", tint, t))

    # ── 对比度：luma 标准差偏低 → 轻加（乘法因子，保守上限 1.18）────────────
    contrast = float(stats.get("contrast", 0.5) or 0.0)
    if contrast < _CONTRAST_FLOOR:
        deficit = (_CONTRAST_FLOOR - contrast)
        factor = 1.0 + _clamp(deficit * 1.2 * scale, 0.0, 0.18)
        if factor > 1.03:
            advise("contrast", _round(factor, 2), "stats.contrast", contrast,
                   "low contrast ({}) → contrast ×{}".format(
                       _round(contrast, 3), _round(factor, 2)))

    # ── 直方图两端未用满且无裁切 → levels 拉伸黑白场 ─────────────────────────
    hist_luma = (analysis.get("histogram") or {}).get("luma") or []
    if len(hist_luma) == 32 and sum(hist_luma) > 0:
        total = float(sum(hist_luma))
        first = next((i for i, c in enumerate(hist_luma)
                      if c / total > 0.005), 0)
        last = next((31 - i for i, c in enumerate(reversed(hist_luma))
                     if c / total > 0.005), 31)
        clip_black = hist_luma[0] / total > 0.06
        clip_white = hist_luma[31] / total > 0.06
        if first >= 3 and last <= 28 and not clip_black and not clip_white:
            black = int(first * 255 / 32)
            white = int((last + 1) * 255 / 32)
            if white - black < 80:
                pass  # 极窄区间（合成图/极暗场景）拉伸会爆对比度，不碰
            else:
                if scale < 1.0:  # 温和模式：向 0/255 收一半
                    black = int(black * scale)
                    white = int(255 - (255 - white) * scale)
                advise("levels", "{},{},1.0".format(black, white),
                       "histogram.luma range",
                       "bins {}-{}".format(first, last),
                       "range unused without clipping → levels {}/{}/1.0".format(
                           black, white))

    # ── 饱和度：vibrance 优先（护肤色/已饱和区；不用全局 saturation）─────────
    sat = float(stats.get("saturation_mean", 0.5) or 0.0)
    if sat < _SAT_FLOOR:
        v = _clamp((0.12 + (_SAT_FLOOR - sat) * 0.8) * scale, 0.08, 0.25)
        advise("vibrance", _round(v, 2), "stats.saturation_mean", sat,
               "muted colors (sat {}) → vibrance {} (skin-safe)".format(
                   _round(sat, 3), _round(v, 2)))

    # ── 细节：低对比 + 低清晰度信号同时出现才给极轻的 clarity ────────────────
    blur = float(analysis.get("blur_score", 1.0) or 0.0)
    if blur < _BLUR_FLOOR and contrast < _CONTRAST_FLOOR:
        c = _clamp(0.05 * scale, 0.0, 0.05)
        if c > 0:
            advise("clarity", _round(c, 2), "blur_score", blur,
                   "low detail ({}) + low contrast → clarity {} (mild)".format(
                       _round(blur, 3), _round(c, 2)))

    # 欠曝提示（只进 reasons，不与 ev 重复建议）
    if under > 10.0 and "ev" not in suggested:
        reasons.append({"field": "note", "metric": "exposure.underexposed_pct",
                        "value": under,
                        "advice": "{:.1f}% deep shadows — consider ev/auto "
                                  "exposure if subject is dark".format(under)})

    return {"suggested": suggested, "reasons": reasons,
            "neutral": not suggested}


def suggest_file(path: str, sample_size: int = 256,
                 scale: float = 1.0) -> dict:
    """分析 + 推荐一步到位（``photo-s suggest`` / MCP / REST 的后端）。"""
    from .metrics import analyze_image
    analysis = analyze_image(path, sample_size=sample_size)
    if not analysis.get("ok"):
        return {"ok": False, "path": path,
                "error": analysis.get("error", "unreadable image"),
                "suggested": {}, "reasons": [], "neutral": False}
    out = suggest_params(analysis, scale=scale)
    out["ok"] = True
    out["path"] = path
    return out
