"""Pipeline: 串联 anomaly → predict → RAG → confidence → (advisor) → render"""
import os
import time
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image

from .anomaly import AnomalyDetector
from .confidence import estimate_confidence, should_use_advisor, should_skip
from .predictor import AutoTonePredictor
from .rag import RAGEnhancer

# 与 photo_s.engine.RAW_EXTENSIONS 对齐（预测端此前只认 JPEG/PNG，RAW 输入
# 一律死于 PIL "cannot identify image file"——2026-09-11 扫街 ARW 实测发现）
_RAW_EXTS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".crw", ".mrw", ".3fr",
    ".erf", ".kdc", ".mef", ".mos", ".x3f", ".raw", ".rwl",
}


def _load_rgb(image_path: str):
    """按后缀分流解码：RAW 走 rawpy（参数对齐引擎 _load_raw_via_rawpy
    默认值——camera WB、sRGB、gamma(2.222, 4.5)、8bit），其余走 PIL。
    预测所见必须与引擎渲染起点一致，否则 9 字段预测建立在不同的基调上。"""
    ext = os.path.splitext(str(image_path))[1].lower()
    if ext not in _RAW_EXTS:
        return Image.open(image_path).convert("RGB")
    import numpy as np
    import rawpy
    with rawpy.imread(str(image_path)) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=False,
            output_color=rawpy.ColorSpace.sRGB,
            gamma=(2.222, 4.5),
            output_bps=8,
        )
    return Image.fromarray(rgb)


# ── WB 口径换算（LR 绝对 → 引擎相对，6500K=中性）────────────────────────
# 模型输出 wb_temp 是 LR 滑块口径的绝对色温（用户"最终想设成多少 K"，
# 训练标签即 lrxmp 的 LR 值），而引擎 apply_white_balance 的中性点是
# 6500K——相机 WB 已平衡的画面 ≈ 中性。不换算直接应用，恒定均值预测
# （~5250K）会把每张图无差别推蓝 B/R×1.17（2026-09-12 扫街 279 张
# 全批偏蓝事故的根因；探针证实模型对偏色零感知、WB 输出纯数据集均值）。
#
# 换算公式: engine_temp = 6500 × T_pred / T_ashot_lr
#   T_ashot 由 camera_whitebalance 的 R/B 乘子比反演（daylight 预设
#   5200K 锚定 κ 标定相机空间→sRGB 黑体空间），再乘 _LR_KELVIN_FACTOR
#   折算到 LR 滑块口径（标定：训练集 LR AsShot 均值 5250K =
#   scene_biases.json NEUTRAL ÷ 本语料 279 张 κ 反演中位 6937K ≈ 0.757；
#   用中位数抗夜景 CCT 冲顶的删失）。
#   |T_pred − T_ashot_lr| > 2500K 视为预测超出模型能力圈（训练分布外
#   光照，如深夜景），不动 WB——绝不猜。
#   wb_tint 一律丢弃：G 归一化乘子不携带 tint 信息，且当前模型 tint
#   输出 ≈0（±100 量程内 0.17）。
# 未来模型改用 Δtemp/Δtint 标签（相对 AsShot）重训后，本换算无需改动
# 即可承接真实逐图风格 WB——适配层是永久件，当前退化为准 no-op 是
# 模型端信息量问题，不是适配层问题。
_LR_KELVIN_FACTOR = 0.7568
_WB_NEUTRAL_K = 6500.0
_WB_COMPETENCE_K = 2500.0
_WB_EQ_RANGE = (3500.0, 10000.0)
_WB_NEUTRAL_DROP_K = 25.0


def _ashot_kelvin(image_path: str) -> Optional[float]:
    """照片 AsShot 物理色温（K）——camera_whitebalance R/B 乘子比反演，
    daylight 预设（5200K）锚定 κ。非 RAW/元数据缺失/退化 → None。"""
    ext = os.path.splitext(str(image_path))[1].lower()
    if ext not in _RAW_EXTS:
        return None
    try:
        import rawpy
        from photo_s.adjust import _kelvin_rgb

        with rawpy.imread(str(image_path)) as raw:
            m = list(raw.camera_whitebalance or ())
            d = list(raw.daylight_whitebalance or ())
        if len(m) < 3 or min(m[0], m[1], m[2]) <= 0 or m[2] == m[0]:
            return None
        if len(d) < 3 or d[0] <= 0 or d[2] <= 0:
            return None
        c5200 = _kelvin_rgb(5200.0)
        kappa = (d[0] / d[2]) / (c5200[0] / c5200[2])
        target_br = (m[2] / m[0]) * kappa  # 等效 sRGB b/r（黑体模型单调，二分）
        lo, hi = 1500.0, 20000.0
        for _ in range(50):
            mid = (lo + hi) / 2.0
            c = _kelvin_rgb(mid)
            if c[2] / c[0] < target_br:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0
    except Exception:
        return None


def _convert_wb_to_engine(options: Dict[str, Any], image_path: str) -> Dict[str, Any]:
    """模型 WB 字段（LR 绝对口径）→ 引擎相对口径，就地修改。

    基线不可得 / 预测超出能力圈 / 换算后出界或 ≈中性 → 删除 WB 字段
    （＝保持相机白平衡不动）。任何路径都不抛错、不猜。"""
    options.pop("wb_tint", None)
    t_pred = options.get("wb_temp")
    if t_pred is None:
        return options
    t_ashot = _ashot_kelvin(image_path)
    if t_ashot is None:
        options.pop("wb_temp", None)
        return options
    t_base = _LR_KELVIN_FACTOR * t_ashot
    if abs(float(t_pred) - t_base) > _WB_COMPETENCE_K:
        options.pop("wb_temp", None)
        return options
    t_eq = _WB_NEUTRAL_K * float(t_pred) / t_base
    if not (_WB_EQ_RANGE[0] <= t_eq <= _WB_EQ_RANGE[1]):
        options.pop("wb_temp", None)
        return options
    if abs(t_eq - _WB_NEUTRAL_K) < _WB_NEUTRAL_DROP_K:
        options.pop("wb_temp", None)  # 换算后 ≈ 中性：相当于用户没动白平衡
        return options
    options["wb_temp"] = int(round(t_eq))
    return options


def run_auto_tone(
    image_path: str,
    strength: float = 1.0,
    render: bool = True,
    use_rag: bool = True,
    use_advisor: bool = False,
    output_path: Optional[str] = None,
    min_confidence: Optional[float] = None,
    predictor: Optional[AutoTonePredictor] = None,
    anomaly_detector: Optional[AnomalyDetector] = None,
    rag_enhancer: Optional[RAGEnhancer] = None,
) -> Dict[str, Any]:
    """主流程：单图自动调色

    Args:
        image_path: 图像绝对路径
        strength: 调色强度 0-1
        render: 是否渲染输出
        use_rag: 是否启用 RAG
        use_advisor: 是否启用 Advisor（低置信度时）
        output_path: 自定义输出路径
        min_confidence: 渲染阈值（None = 不启用）。置信度低于该值时
            不渲染输出、状态记为 skipped（批量场景用于跳过低置信图）。
        predictor / anomaly_detector / rag_enhancer: 注入依赖（测试用）

    Returns:
        标准 OUTPUT_SCHEMA dict
    """
    t0 = time.time()

    img = _load_rgb(image_path)
    predictor = predictor or AutoTonePredictor()
    predictor.load()
    anomaly_detector = anomaly_detector or AnomalyDetector()
    anomaly_detector.load()

    # 1. 异常检测
    anomaly_score, anomaly_info = anomaly_detector.score(img)

    def _result(options, confidence, warnings, rendered_path, extra_meta=None,
                local=None):
        out = {
            "schema_version": 1,
            "options": options,
            "confidence": confidence,
            "warnings": warnings,
            "rendered_path": rendered_path,
            "metadata": {
                "anomaly_score": round(anomaly_score, 3),
                "model_version": "v7_clean",
                "duration_ms": int((time.time() - t0) * 1000),
                **(extra_meta or {}),
            },
        }
        # 词汇表扩展（加性键）：局部调整仅在预测到非空时携带
        if local:
            out["local"] = local
        return out

    # 高异常图直接跳过（无需预测）
    if anomaly_score > 0.7:
        return _result({}, 0.0,
                       [f"image anomaly too high ({anomaly_score:.2f}), skipped"],
                       None, {"skipped": True})

    # 2. v7_clean 预测
    options = predictor.predict(img)
    pred_norm = np.array([
        (options[f] - predictor.ranges[f][0])
        / (predictor.ranges[f][1] - predictor.ranges[f][0]) * 2 - 1
        for f in predictor.targets
    ], dtype=np.float32)

    # 3. RAG 增强（可选）
    rag_used = False
    max_rag_sim = None
    if use_rag:
        rag = rag_enhancer or RAGEnhancer()
        rag.load()
        if rag.train_clip is not None:
            fused_norm, rag_info = rag.enhance(
                img, pred_norm, predictor.ranges, predictor.targets)
            if rag_info.get("rag_used"):
                pred_norm = fused_norm
                rag_used = True
                max_rag_sim = rag_info.get("max_sim")

                options = {}
                for j, f in enumerate(predictor.targets):
                    lo, hi = predictor.ranges[f]
                    options[f] = float((pred_norm[j] + 1) / 2 * (hi - lo) + lo)

    # 4. 置信度
    pred_std = float(np.std(pred_norm))
    confidence = estimate_confidence(
        pred=pred_norm,
        rag_sim=max_rag_sim,
        anomaly_score=anomaly_score,
        model_std=pred_std,
    )

    warnings = []
    if confidence < 0.4:
        warnings.append(f"low confidence ({confidence:.2f}), manual review suggested")
    if anomaly_score > 0.5:
        warnings.append(f"high anomaly ({anomaly_score:.2f})")

    # 5. 低置信度整体跳过（含渲染）
    if should_skip(confidence, anomaly_score):
        return _result(options, round(confidence, 3),
                       warnings + [f"confidence too low ({confidence:.2f}), skipped"],
                       None, {"skipped": True, "rag_used": rag_used})

    # 6. Advisor（可选）
    if use_advisor and should_use_advisor(confidence, anomaly_score):
        from .advisor import ToneAdvisor
        advisor = ToneAdvisor()
        advice = advisor.advise(img, options)
        delta = advice.get("suggested_delta", {})
        if delta:
            warnings.append(f"advisor: {advice.get('reason', '')}")
            for f, d in delta.items():
                if f in options and f not in ("wb_temp", "wb_tint"):
                    lo, hi = predictor.ranges[f]
                    options[f] = float(options[f] + d * (hi - lo) / 2)

    # 7. 局部调整预测（checkpoint 携带局部头时；全中性 → 空列表）。
    #    hasattr 守卫：注入的旧式/测试桩 predictor 无此方法时保持空局部
    local = (predictor.predict_local(img)
             if hasattr(predictor, "predict_local") else [])

    # 8. 应用 strength（向中性值插值；渲染时不再重复缩放）
    from .render import apply_strength
    options = apply_strength(options, strength)
    if local and strength < 1.0:
        local = [
            {"region": item["region"],
             "params": {k: float(strength * v)
                        for k, v in item["params"].items()}}
            for item in local
        ]
        # strength 缩放后可能全部回到中性 → 空蒙版没有意义
        local = [item for item in local if item["params"]]

    # 8.5 WB 口径换算（LR 绝对 → 引擎相对，见 _convert_wb_to_engine）。
    #    放在 strength 之后：strength 向 LR 口径中性值（5250K）插值，
    #    换算在其后做——当前模型 WB 输出 ≈ 常数时 strength 对 WB 的
    #    影响本来就趋零，语义无损。
    options = _convert_wb_to_engine(options, image_path)

    # 9. 渲染
    rendered_path = None
    if render:
        if min_confidence is not None and confidence < min_confidence:
            warnings.append(
                f"confidence {confidence:.2f} < min_confidence {min_confidence}, render skipped")
        else:
            try:
                from .render import render_options
                if output_path is None:
                    base, ext = os.path.splitext(image_path)
                    output_path = f"{base}_auto{ext or '.jpg'}"
                render_options(img, options, output_path, strength=1.0,
                               local=local)
                rendered_path = output_path
            except Exception as e:
                warnings.append(f"render failed: {e}")

    return _result(options, round(confidence, 3), warnings, rendered_path,
                   {"rag_used": rag_used}, local=local)
