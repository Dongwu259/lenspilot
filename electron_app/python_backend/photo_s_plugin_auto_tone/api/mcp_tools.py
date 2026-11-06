"""MCP 工具实现

注册到 photo-s 的 MCP server（mcp.add_tool）：
    from photo_s_plugin_auto_tone.api.mcp_tools import auto_tone_tool, ...
"""
import json
import os
import time
from typing import List, Optional

# 风格 key -> 中文名（避免循环 import；与 core.style.STYLE_CN 保持同步）
STYLE_CN_MAP = {
    'melancholy_blue': '忧郁蓝调', 'vintage_film': '复古胶片', 'fresh_natural': '清新自然',
    'cinematic': '电影感', 'high_contrast_bw': '高对比黑白', 'golden_hour': '暖色黄昏',
    'cool_dawn': '冷色清晨', 'docu_bw': '黑白纪实', 'low_key': '低调暗调', 'high_key': '高调明亮',
    'urban_night': '都市夜景', 'portrait_warm': '暖色人像', 'landscape_vivid': '鲜艳风景',
    'film_noir': '黑色电影', 'pastel': '粉彩梦幻', 'minimalist': '极简主义',
}


def register_mcp_tools(mcp):
    """注册 MCP 工具到 photo-s 的 FastMCP 实例"""
    mcp.add_tool(auto_tone_tool, name="auto_tone",
                 description="AI 自动调色：预测 9 字段 Lightroom 参数并渲染")
    mcp.add_tool(aesthetic_score_tool, name="aesthetic_score",
                 description="美学评分 1-10（需 qwen extra）")
    mcp.add_tool(verify_aesthetic_tool, name="verify_aesthetic",
                 description="美学验证（v2.4）：SigLIP 回归头快速评分，"
                             "缺席时回落 Qwen VLM——audit 闸门/候选排序用")
    mcp.add_tool(tone_advisor_tool, name="tone_advisor",
                 description="修图建议（需 qwen extra）")
    mcp.add_tool(batch_auto_tone_tool, name="batch_auto_tone",
                 description="批量自动调色（可选风格化 style_desc）")
    mcp.add_tool(auto_tone_with_style_tool, name="auto_tone_with_style",
                 description="风格化 AI 自动调色：任意自然语言风格描述（如'忧郁蓝调'），"
                             "叠加 SigLIP 预测 + 风格偏置")
    mcp.add_tool(analyze_visual_style_tool, name="analyze_visual_style",
                 description="SigLIP 视觉风格分析：返回图像 top-K 艺术风格及置信度")


def auto_tone_tool(
    image_path: str,
    strength: float = 1.0,
    render: bool = True,
    use_rag: bool = True,
) -> str:
    """AI 自动调色工具。

    Args:
        image_path: 图像绝对路径
        strength: 调色强度 0-1
        render: 是否渲染输出
        use_rag: 是否启用 RAG

    Returns:
        JSON 字符串（含 schema_version, options, confidence, warnings）
    """
    from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

    result = run_auto_tone(image_path, strength=strength, render=render,
                           use_rag=use_rag)
    return json.dumps(result, ensure_ascii=False)


def aesthetic_score_tool(image_path: str) -> str:
    """美学评分工具。

    Args:
        image_path: 图像绝对路径

    Returns:
        JSON 字符串（含 score 1-10, bucket, confidence）
    """
    from photo_s_plugin_auto_tone.core.aesthetic import AestheticScorer

    result = AestheticScorer().score(image_path)
    return json.dumps({"schema_version": 1, **result}, ensure_ascii=False)


def verify_aesthetic_tool(image_path: str, prefer: str = "auto") -> str:
    """美学验证工具（v2.4 —— 全自动闭环的 stop 条件）。

    Args:
        image_path: 图像绝对路径
        prefer: auto（SigLIP 头优先，缺席回落 Qwen）| head | qwen

    Returns:
        JSON 字符串（score 1-10, bucket, source, confidence, loaded；
        无可用 verifier 时 score=null + raw 指引）
    """
    from photo_s_plugin_auto_tone.core.verifier import verify_aesthetic

    result = verify_aesthetic(image_path, prefer=prefer)
    return json.dumps({"schema_version": 1, "image_path": image_path,
                       **result}, ensure_ascii=False)


def tone_advisor_tool(image_path: str, current_options: Optional[str] = None) -> str:
    """修图建议工具。

    Args:
        image_path: 图像绝对路径
        current_options: JSON 字符串格式的当前 9 字段参数（None 时用 auto_tone 预测）

    Returns:
        JSON 字符串（含 current_options, suggested_delta, reason）
    """
    from photo_s_plugin_auto_tone.core.advisor import ToneAdvisor
    from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

    if current_options:
        options = json.loads(current_options)
    else:
        result = run_auto_tone(image_path, render=False, use_rag=True)
        options = result.get("options", {})

    advice = ToneAdvisor().advise(image_path, options)
    return json.dumps({"schema_version": 1, **advice}, ensure_ascii=False)


def auto_tone_with_style_tool(
    image_path: str,
    style_desc: Optional[str] = None,
    strength: float = 1.0,
    use_qwen: bool = True,
    render: bool = True,
) -> str:
    """风格化 AI 自动调色工具。

    支持任意自然语言风格描述（如"忧郁蓝调"、"电影感"、"暖色夕阳中的城市"）。
    style_desc 为 None 时自动用 SigLIP 视觉分析选择风格。

    Args:
        image_path: 图像绝对路径
        style_desc: 风格描述（None=自动分析）
        strength: 风格强度 0-1
        use_qwen: True=Qwen 解析（更灵活，需 qwen extra），False=预设（快速）
        render: 是否渲染输出

    Returns:
        JSON 字符串（schema_version=2: options, bias, bias_source,
        style_desc, visual_styles, rendered_path, warnings, metadata）
    """
    from photo_s_plugin_auto_tone.core.style import auto_tone_with_style

    result = auto_tone_with_style(
        image_path, style_desc=style_desc, strength=strength,
        use_qwen=use_qwen, render=render,
    )
    return json.dumps(result, ensure_ascii=False)


def analyze_visual_style_tool(image_path: str, top_k: int = 3) -> str:
    """视觉风格分析工具：用 SigLIP 分析图像最可能的艺术风格。

    Args:
        image_path: 图像绝对路径
        top_k: 返回前 K 个风格

    Returns:
        JSON 字符串（schema_version=1: image_path, top_styles）
    """
    from photo_s_plugin_auto_tone.core.style import analyze_visual_style

    top_styles = analyze_visual_style(image_path, top_k=top_k)
    return json.dumps({
        "schema_version": 1,
        "image_path": image_path,
        "top_styles": [
            {"style_key": k, "style_cn": STYLE_CN_MAP.get(k, k),
             "confidence": round(c, 4)}
            for k, c in top_styles
        ],
    }, ensure_ascii=False)


def batch_auto_tone_tool(
    image_paths: List[str],
    strength: float = 1.0,
    skip_low_confidence: bool = True,
    output_dir: Optional[str] = None,
    style_desc: Optional[str] = None,
) -> str:
    """批量自动调色工具（可选风格化）。

    Args:
        image_paths: 图像路径列表
        strength: 调色强度
        skip_low_confidence: 跳过低置信度图（不渲染输出；仅普通模式生效）
        output_dir: 输出目录（None 写到原图同目录）
        style_desc: 风格描述（None=普通 auto_tone，否则叠加风格偏置）

    Returns:
        JSON 字符串（含 total, processed, skipped, failed, results）
    """
    from photo_s_plugin_auto_tone.core.pipeline import run_auto_tone

    results = []
    t0 = time.time()
    for path in image_paths:
        try:
            out_path = None
            if output_dir:
                stem, ext = os.path.splitext(os.path.basename(path))
                suffix = '_styled' if style_desc else '_auto'
                out_path = os.path.join(
                    output_dir, f"{stem}{suffix}{ext or '.jpg'}")

            if style_desc:
                # 风格化批量（无 confidence 字段；风格化不做低置信跳过）
                from photo_s_plugin_auto_tone.core.style import (
                    auto_tone_with_style)
                r = auto_tone_with_style(
                    path, style_desc=style_desc, strength=strength,
                    use_qwen=True, render=True, output_path=out_path,
                )
                r_warnings = r.get("warnings", [])
                skipped = False
                conf = 1.0  # 风格化路径无 confidence，保持字段稳定
            else:
                # 普通自动调色；min_confidence：低置信图直接不渲染
                #（避免先写文件再标记 skipped）
                r = run_auto_tone(
                    path, strength=strength, render=True,
                    use_rag=True, output_path=out_path,
                    min_confidence=0.3 if skip_low_confidence else None,
                )
                r_warnings = r["warnings"]
                skipped = bool(r["metadata"].get("skipped"))
                conf = r["confidence"]

            status = "skipped" if skipped else "ok"
            if not skipped and any("render failed" in w for w in r_warnings):
                status = "failed"

            row = {
                "image_path": path,
                "status": status,
                "confidence": conf,
                "rendered_path": r.get("rendered_path"),
                "warnings": r_warnings,
                "options": r.get("options"),
            }
            if r.get("local"):
                row["local"] = r["local"]
            if style_desc:
                row["style_desc"] = r.get("style_desc")
                row["bias_source"] = r.get("bias_source")
            results.append(row)
        except Exception as e:
            results.append({
                "image_path": path,
                "status": "failed",
                "reason": str(e),
            })

    elapsed = time.time() - t0
    summary = {
        "schema_version": 1,
        "total": len(image_paths),
        "processed": sum(1 for r in results if r["status"] == "ok"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "elapsed_sec": round(elapsed, 2),
        "results": results,
        "metadata": {
            "throughput_imgs_per_sec": round(len(image_paths) / max(elapsed, 0.1), 2),
            "style_desc": style_desc,
        },
    }
    return json.dumps(summary, ensure_ascii=False)
