"""置信度估计（纯 numpy，无重依赖）"""
import numpy as np


def estimate_confidence(
    pred: np.ndarray,
    rag_sim=None,
    anomaly_score: float = 0.0,
    model_std: float = 0.0,
) -> float:
    """综合置信度估计

    Args:
        pred: 模型预测 (9,) 归一化空间
        rag_sim: 与训练集 CLIP 相似度 (0-1)，None 表示未启用 RAG
        anomaly_score: 异常分 0-1
        model_std: 预测的标准差（衡量模型自身不确定度）

    Returns:
        0-1 置信度
    """
    uncertainty = min(1.0, model_std / 0.5) if model_std > 0 else 0.5

    if rag_sim is None or rag_sim <= 0:
        outlier = 0.5  # 未知
    else:
        outlier = max(0.0, (0.7 - rag_sim) / 0.7)

    anomaly = max(0.0, min(1.0, anomaly_score))

    confidence = 1.0 - 0.4 * uncertainty - 0.4 * outlier - 0.2 * anomaly
    return float(np.clip(confidence, 0.0, 1.0))


def should_use_advisor(confidence: float, anomaly_score: float) -> bool:
    """是否启用 Advisor 修正"""
    return 0.3 <= confidence < 0.7 and anomaly_score < 0.7


def should_skip(confidence: float, anomaly_score: float) -> bool:
    """是否跳过预测"""
    return anomaly_score > 0.7 or confidence < 0.2
