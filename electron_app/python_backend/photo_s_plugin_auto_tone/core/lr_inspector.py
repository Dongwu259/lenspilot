# -*- coding: utf-8 -*-
"""P67 Photo Inspector —— 单图诊断（32+ 指标 → LLM 友好文字）。

端口自 AI_LR_trainning/src/p67_inspector.py（蒸馏线教师/学生共用的感知文字化层，
仅去掉了服务器侧 __main__ 演示块）。lr-v1 蒸馏学生训练时消费的 Inspector 文字
即本模块产出 + inspector_text() 末尾的 σ/μ 修正行——口径必须与训练一致，
否则学生看到的诊断分布漂移。

可作:
1. lr-v1 学生 (Qwen3-VL+LoRA) 的 prompt 输入
2. 修图前的 baseline, 修图后的 quality check（training_penalties）
"""
import numpy as np
from PIL import Image
from typing import Union, Dict


def _to_arr(img: Union[Image.Image, np.ndarray]) -> np.ndarray:
    if isinstance(img, Image.Image):
        return np.asarray(img.convert('RGB'), dtype=np.float32)
    if img.dtype != np.float32:
        return img.astype(np.float32)
    return img


def inspect_image(img: Union[Image.Image, np.ndarray], name: str = '') -> Dict:
    """提取图片的 32+ 诊断指标

    Args:
        img: PIL.Image (RGB) 或 numpy 数组 (H, W, 3) float32 0-255
        name: 图名 (debug 用)

    Returns:
        dict with keys:
          - 基础: shape, mean_luma, median_luma
          - 区域 luma: top_luma, mid_luma, bot_luma
          - 通道剪裁: top_R_clip, top_G_clip, top_B_clip, ...
          - 偏色: cast_R, cast_G, cast_B, cast_RB, cast_temp_K, cast_tint
          - 对比度: global_contrast, top_mid_diff, top_bot_diff, dynamic_range
          - 直方图: hist_luma_24d, hist_R_8d, hist_G_8d, hist_B_8d
          - 场景标志: is_backlight, is_night, is_high_sat
          - 描述: description (文字, 给 LLM 看)
    """
    arr = _to_arr(img)
    H, W, _ = arr.shape

    # === 1. 亮度 (Luma) ===
    luma = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    mean_luma = float(luma.mean() / 255.0)
    median_luma = float(np.median(luma) / 255.0)

    # 区域 (顶部 1/3, 中, 底部 1/3)
    top = arr[:H//3]; top_l = luma[:H//3]
    mid = arr[H//3:2*H//3]; mid_l = luma[H//3:2*H//3]
    bot = arr[2*H//3:]; bot_l = luma[2*H//3:]
    top_luma = float(top_l.mean() / 255.0)
    mid_luma = float(mid_l.mean() / 255.0)
    bot_luma = float(bot_l.mean() / 255.0)

    # === 2. 通道剪裁 (Clipping) ===
    def clip_rate(channel, threshold=250):
        return float((channel >= threshold).mean() * 100)

    top_R = clip_rate(top[..., 0]); top_G = clip_rate(top[..., 1]); top_B = clip_rate(top[..., 2])
    mid_R = clip_rate(mid[..., 0]); mid_G = clip_rate(mid[..., 1]); mid_B = clip_rate(mid[..., 2])
    bot_R = clip_rate(bot[..., 0]); bot_G = clip_rate(bot[..., 1]); bot_B = clip_rate(bot[..., 2])
    all_R = clip_rate(arr[..., 0]); all_G = clip_rate(arr[..., 1]); all_B = clip_rate(arr[..., 2])

    # === 3. 偏色 (Color Cast) ===
    mean_R = float(arr[..., 0].mean())
    mean_G = float(arr[..., 1].mean())
    mean_B = float(arr[..., 2].mean())
    cast_R = mean_R - 128
    cast_G = mean_G - 128
    cast_B = mean_B - 128
    cast_RB = mean_R - mean_B
    # 近似色温: tanh(20*(R/B - 1)) → 2000K ~ 10000K
    rb = mean_R / max(mean_B, 1e-3)
    cast_temp_K = float(2000 + (rb - 0.5) * 10000 / 1.5)
    cast_temp_K = max(2000, min(10000, cast_temp_K))
    # 近似 tint: (G - (R+B)/2) / 128
    cast_tint = float((mean_G - (mean_R + mean_B) / 2) / 64)

    top_cast_R = float(top[..., 0].mean()) - 128
    top_cast_G = float(top[..., 1].mean()) - 128
    top_cast_B = float(top[..., 2].mean()) - 128

    # === 4. 对比度 (Contrast) ===
    global_contrast = float(luma.std() / max(luma.mean(), 1e-3) / 255.0)
    top_mid_diff = top_luma - mid_luma
    top_bot_diff = top_luma - bot_luma
    p99, p1 = np.percentile(luma, [99, 1])
    dynamic_range = float((p99 - p1) / 255.0)
    shadow_clip = float((luma < 5).mean() * 100)
    highlight_clip = float((luma > 250).mean() * 100)

    # === 5. Banding 检测 (8-bit 横向 5+ 连续相同值) ===
    R = arr[..., 0].astype(np.int32)
    banded = 0
    for row in R[::8]:  # 采样 1/8 行加速
        run = 1
        for j in range(1, len(row)):
            if row[j] == row[j-1]:
                run += 1
                if run >= 5: banded += 1
            else:
                run = 1
    banded_pct = banded / (R[::8].size) * 100

    # === 6. 直方图 (Histogram) ===
    hist_luma_24d, _ = np.histogram(luma, bins=24, range=(0, 255))
    hist_luma_24d = hist_luma_24d / luma.size  # 归一化
    # 重点: bins 18-23 (>=192) = 高光区比例
    highlight_frac = float(hist_luma_24d[18:].sum())

    hist_R_8d, _ = np.histogram(arr[..., 0], bins=8, range=(0, 255))
    hist_G_8d, _ = np.histogram(arr[..., 1], bins=8, range=(0, 255))
    hist_B_8d, _ = np.histogram(arr[..., 2], bins=8, range=(0, 255))
    hist_R_8d = hist_R_8d / arr[..., 0].size
    hist_G_8d = hist_G_8d / arr[..., 1].size
    hist_B_8d = hist_B_8d / arr[..., 2].size

    # === 7. 场景标志 ===
    is_backlight = top_bot_diff > 0.15
    is_night = mean_luma < 0.18
    is_high_sat = float(np.array(Image.fromarray(arr.astype(np.uint8)).convert('HSV'))[..., 1].mean()) > 0.50

    # === 8. 描述 (给 LLM 看) ===
    parts = []
    if is_night: parts.append(f'夜景 (亮度 {mean_luma:.2f})')
    elif mean_luma < 0.30: parts.append(f'低光 (亮度 {mean_luma:.2f})')
    elif mean_luma > 0.75: parts.append(f'高亮 (亮度 {mean_luma:.2f})')
    else: parts.append(f'亮度 {mean_luma:.2f}')

    if is_backlight: parts.append(f'逆光 (上-下 {top_bot_diff:+.2f})')

    if top_B > 5: parts.append(f'顶部蓝剪裁 {top_B:.1f}%')
    elif top_R > 5: parts.append(f'顶部红剪裁 {top_R:.1f}%')
    if all_B > 2: parts.append(f'全图蓝剪裁 {all_B:.1f}%')

    if cast_RB > 15: parts.append(f'偏红 (R-B {cast_RB:+.1f})')
    elif cast_RB < -15: parts.append(f'偏蓝/黄 (R-B {cast_RB:+.1f})')
    if abs(cast_tint) > 0.15: parts.append(f'偏{"绿" if cast_tint > 0 else "品"} (tint {cast_tint:+.2f})')

    if highlight_clip > 5: parts.append(f'高光占比 {highlight_clip:.1f}%')
    if shadow_clip > 20: parts.append(f'暗部占比 {shadow_clip:.1f}%')

    if dynamic_range < 0.3: parts.append(f'动态范围窄 ({dynamic_range:.2f})')
    if global_contrast < 0.25: parts.append(f'对比低 ({global_contrast:.2f})')

    if banded_pct > 25: parts.append(f'banding 严重 ({banded_pct:.0f}%)')

    description = ', '.join(parts) if parts else '正常'

    return {
        'name': name,
        'shape': (H, W),
        # 曝光
        'mean_luma': mean_luma, 'median_luma': median_luma,
        'top_luma': top_luma, 'mid_luma': mid_luma, 'bot_luma': bot_luma,
        # 剪裁
        'top_R_clip': top_R, 'top_G_clip': top_G, 'top_B_clip': top_B,
        'mid_R_clip': mid_R, 'mid_G_clip': mid_G, 'mid_B_clip': mid_B,
        'bot_R_clip': bot_R, 'bot_G_clip': bot_G, 'bot_B_clip': bot_B,
        'all_R_clip': all_R, 'all_G_clip': all_G, 'all_B_clip': all_B,
        # 偏色
        'cast_R': cast_R, 'cast_G': cast_G, 'cast_B': cast_B,
        'cast_RB': cast_RB, 'cast_temp_K': cast_temp_K, 'cast_tint': cast_tint,
        'top_cast_R': top_cast_R, 'top_cast_G': top_cast_G, 'top_cast_B': top_cast_B,
        # 对比度
        'global_contrast': global_contrast, 'dynamic_range': dynamic_range,
        'top_mid_diff': top_mid_diff, 'top_bot_diff': top_bot_diff,
        'shadow_clip': shadow_clip, 'highlight_clip': highlight_clip,
        # 场景
        'is_backlight': is_backlight, 'is_night': is_night, 'is_high_sat': is_high_sat,
        # 直方图
        'hist_luma_24d': hist_luma_24d.tolist(),
        'highlight_frac': highlight_frac,  # 192+ 比例
        'banded_pct': banded_pct,
        # 描述
        'description': description,
    }


def training_penalties(ins: Dict) -> Dict[str, float]:
    """从 inspect 结果计算训练 loss 用的惩罚项

    Returns:
      {
        'over_bright': max(0, top_luma - 0.85)^2   # 顶部过亮 (用户反馈"再提亮过曝")
        'over_clip':   (top_B_clip/100)^2          # 蓝剪裁
        'over_sat':    max(0, sat - 0.7)^2         # 饱和过冲
        'cast_RB':     abs(cast_RB) / 50           # 偏色 (P63 用户反馈"死板偏黄")
        'low_contrast':max(0, 0.30 - dynamic_range) # 动态范围窄
      }
    """
    over_bright = max(0.0, ins['top_luma'] - 0.85) ** 2
    over_clip = (ins['top_B_clip'] / 100.0) ** 2
    over_sat = max(0.0, ins.get('is_high_sat', 0) * 0.7 - 0.5) ** 2
    cast_pen = abs(ins['cast_RB']) / 50.0
    low_contrast = max(0.0, 0.30 - ins['dynamic_range'])
    return {
        'over_bright': float(over_bright),
        'over_clip': float(over_clip),
        'over_sat': float(over_sat),
        'cast_RB': float(cast_pen),
        'low_contrast': float(low_contrast),
    }


def format_for_llm(ins: Dict) -> str:
    """格式化成 LLM 友好的文字 (Qwen3-VL 可读)"""
    return (
        f"图片 '{ins['name']}' ({ins['shape'][1]}x{ins['shape'][0]}):\n"
        f"  {ins['description']}\n"
        f"  亮度: 平均={ins['mean_luma']:.3f}, 中位={ins['median_luma']:.3f}\n"
        f"  区域: top={ins['top_luma']:.3f}, mid={ins['mid_luma']:.3f}, bot={ins['bot_luma']:.3f}\n"
        f"  剪裁: top R/G/B={ins['top_R_clip']:.1f}/{ins['top_G_clip']:.1f}/{ins['top_B_clip']:.1f}%, "
        f"全图={ins['all_R_clip']:.1f}/{ins['all_G_clip']:.1f}/{ins['all_B_clip']:.1f}%\n"
        f"  偏色: R-B={ins['cast_RB']:+.1f}, 色温≈{ins['cast_temp_K']:.0f}K, tint={ins['cast_tint']:+.2f}\n"
        f"  对比: dynamic={ins['dynamic_range']:.2f}, contrast={ins['global_contrast']:.2f}, "
        f"高光={ins['highlight_clip']:.1f}%, 暗部={ins['shadow_clip']:.1f}%\n"
        f"  场景: backlight={ins['is_backlight']}, night={ins['is_night']}, high_sat={ins['is_high_sat']}\n"
        f"  banding: {ins['banded_pct']:.1f}%"
    )


def inspector_text(img: Image.Image, name: str = '') -> str:
    """lr-v1 学生的完整诊断文字 = format_for_llm + σ/μ 修正行。

    末行与训练/bench 完全一致（global_contrast 原字段量纲有误，σ/μ 行为准）
    ——逐字复制自蒸馏线 bench_eval_student.inspector_text。"""
    ins = inspect_image(img, name=name)
    txt = format_for_llm(ins)
    arr = np.asarray(img.convert('RGB'), dtype=np.float32)
    luma = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    txt += '\n  有效对比(σ/μ)=%.2f  (global_contrast 字段量纲有误, 以本行为准)' % (
        luma.std() / max(luma.mean(), 1e-3))
    return txt
