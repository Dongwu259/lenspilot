"""photo_s/audit.py — 出片质量闸门（agent 终止条件，v1.9.0 阶段 1）

组合现有感知指标给出明确的 pass/fail + 原因——**agent 全自动修图的 stop
条件**：没有终止判据，"全自动"要么无限调要么错误时机停。

阈值均为保守出片标准（可 CLI 覆盖），纯 stdlib 复用 metrics。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["audit_image", "DEFAULT_THRESHOLDS"]

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "overexposed_max": 2.0,    # 过曝像素占比 %（高光死白上限）
    "underexposed_max": 2.0,   # 欠曝像素占比 %
    "blur_min": 0.05,          # Laplacian 方差模糊分下限
    "luminance_min": 0.05,     # 平均亮度下限（0-1）
    "luminance_max": 0.95,     # 平均亮度上限
    "contrast_min": 0.05,      # 对比度下限
    "kelvin_min": 2500.0,      # 色温估计范围
    "kelvin_max": 10000.0,
}


def audit_image(path: str, *, sample_size: int = 256,
                aesthetic: Optional[float] = None,
                verifier: Optional[Any] = None,
                **thresholds: float) -> Dict[str, Any]:
    """单图出片审计 → ``{ok, passed, checks, reason}``。

    ``checks``：每项 ``{name, ok, value, threshold, direction}``；
    ``reason``：失败项摘要（agent 可直接读）；不可读图返回 ``ok=False``。
    阈值经 ``DEFAULT_THRESHOLDS`` 合并，可逐项覆盖。

    v2.4 美学闸门：``aesthetic``（1-10 阈值）非 None 时追加一项
    ``aesthetic`` 检查——分数来自 ``verifier``（``find_provider("verify")``
    注入的 auto-tone 插件；返回 ``{score, ...}`` 的可调用）。**stop 条件
    语义**：请求了美学闸门但插件缺席时抛
    :class:`RuntimeError`（装插件/训头），静默放行会让 agent 在错误
    的"通过"上停机；verifier 给不出分数（未训头且无 qwen extra）记
    该项 fail（value=None，原因在 reason）。
    """
    from .metrics import analyze_image
    a = analyze_image(path, sample_size=sample_size)
    if not a.get("ok"):
        return {"ok": False, "path": path, "error": a.get("error",
                                                          "unreadable image"),
                "passed": False, "checks": [], "reason": "unreadable image"}
    if aesthetic is not None:
        if verifier is None:
            raise RuntimeError(
                "aesthetic gate requested but no verifier plugin: "
                "pip install 'photo-s-plugin-auto-tone[model]' "
                "(head via tools/train_verifier.py, or the [qwen] extra); "
                "or drop --aesthetic to audit technical quality only")
    overrides = {}
    for k, v in thresholds.items():
        if v is None:
            continue
        try:
            overrides[k] = float(v)
        except (TypeError, ValueError):
            continue  # a bad override value must not crash the audit batch
    th = {**DEFAULT_THRESHOLDS, **overrides}
    ex = a["exposure"]
    st = a["stats"]
    wb = a["white_balance"]
    checks: List[Dict[str, Any]] = []

    def check(name, ok, value, threshold, direction):
        try:
            thr = round(float(threshold), 3)
        except (TypeError, ValueError):
            thr = str(threshold)  # range 阈值（如 "0.05-0.95"）
        checks.append({"name": name, "ok": bool(ok),
                       "value": round(float(value), 3),
                       "threshold": thr, "direction": direction})

    check("overexposed", ex["overexposed_pct"] <= th["overexposed_max"],
          ex["overexposed_pct"], th["overexposed_max"], "<=")
    check("underexposed", ex["underexposed_pct"] <= th["underexposed_max"],
          ex["underexposed_pct"], th["underexposed_max"], "<=")
    check("blur", a["blur_score"] >= th["blur_min"], a["blur_score"],
          th["blur_min"], ">=")
    lum = ex["luminance"]
    check("luminance",
          th["luminance_min"] <= lum <= th["luminance_max"], lum,
          f"{th['luminance_min']}-{th['luminance_max']}", "range")
    check("contrast", st["contrast"] >= th["contrast_min"], st["contrast"],
          th["contrast_min"], ">=")
    kelvin = wb["kelvin_estimate"]
    check("white_balance", th["kelvin_min"] <= kelvin <= th["kelvin_max"],
          kelvin, f"{th['kelvin_min']:.0f}-{th['kelvin_max']:.0f}", "range")

    if aesthetic is not None:
        try:
            v = verifier(path) if callable(verifier) else verifier.verify(path)
            score = v.get("score") if isinstance(v, dict) else None
        except Exception as e:  # verifier 内部失败 → 该项 fail，不炸整批
            score, v = None, {"raw": str(e)}
        if score is None:
            checks.append({
                "name": "aesthetic", "ok": False, "value": None,
                "threshold": round(float(aesthetic), 3), "direction": ">=",
                "error": (v.get("raw") or "verifier returned no score"),
            })
        else:
            check("aesthetic", float(score) >= float(aesthetic), score,
                  aesthetic, ">=")

    failed = [c for c in checks if not c["ok"]]

    def _fmt(c):
        s = f"{c['name']}={c['value']}{c['direction']}{c['threshold']}"
        err = c.get("error")
        return f"{s} ({str(err)[:120]})" if err else s

    reason = "ok" if not failed else "; ".join(_fmt(c) for c in failed)
    return {"ok": True, "path": path, "passed": not failed,
            "checks": checks, "reason": reason}
