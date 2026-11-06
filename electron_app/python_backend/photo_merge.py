"""photo_merge.py — HDR 合成 / 全景拼接（P6-2）

  merge_hdr   复用 photo_s.hdr.merge_hdr（OpenCV Mertens 曝光融合，可选 MTB
              对齐）——LightCraft "HDR (auto-align, deghost)" 的对应实现。
  merge_panorama  cv2.Stitcher（PANORAMA 模式）拼接 2-15 张。

输出：<dest_dir>/<首文件名>_hdr.jpg / _pano.jpg（冲突自动加序号，绝不覆盖）。
需 opencv；缺失（Mac 构建）返回可读错误——与既有回退口径一致。
"""

import os

import command_registry
from command_registry import command
from service import send_log

_HDR_MAX = 7
_PANO_MIN, _PANO_MAX = 2, 15


def _unique_path(dest_dir, stem, ext):
    out = os.path.join(dest_dir, f"{stem}{ext}")
    n = 1
    while os.path.exists(out):
        out = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        n += 1
    return out


def _validated_paths(params, lo, hi, label):
    paths = params.get("paths") or []
    paths = [p for p in paths if isinstance(p, str) and os.path.isfile(p)]
    if len(paths) < lo:
        return None, f"{label}至少需要 {lo} 张有效照片"
    if len(paths) > hi:
        paths = paths[:hi]
    dest_dir = str(params.get("dest_dir") or "").strip() or os.path.dirname(paths[0])
    return (paths, dest_dir), None


@command("merge_hdr", "editor",
         "[工具] 曝光括弧合并 HDR（Mertens 曝光融合，align=true 时 MTB 对齐）。"
         "输出 <dest_dir>/<首文件名>_hdr.jpg，绝不覆盖已有文件。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"},
                        "description": "2-7 张同场景不同曝光的照片"},
             "dest_dir": {"type": "string", "description": "输出目录，默认首图所在目录"},
             "align": {"type": "boolean", "description": "MTB 对齐（手持括弧建议开）"}},
          "required": ["paths"], "additionalProperties": False},
         agent_tool=True)
def handle_merge_hdr(params):
    ok, err = _validated_paths(params, 2, _HDR_MAX, "HDR")
    if err:
        return {"error": err}
    paths, dest_dir = ok
    warnings = []
    try:
        from photo_s.hdr import merge_hdr
        want_align = bool(params.get("align"))
        try:
            result = merge_hdr(paths, align=want_align)
        except Exception as e:
            # 部分 OpenCV 构建（≥4.x 某些版本）AlignMTB.process 不回填输出，
            # 或 photo 模块绑定残缺 → 对齐失败自动降级未对齐融合（明确告警，
            # 不静默：手持括弧可能出现鬼影）。
            if want_align:
                result = merge_hdr(paths, align=False)
                warnings.append("MTB 对齐在当前 OpenCV 构建不可用，已按未对齐融合"
                                "（手持拍摄可能出现鬼影，建议三脚架括弧）")
            else:
                raise
    except ImportError:
        return {"error": "HDR 合成需要 opencv（当前环境缺失）"}
    except Exception as e:
        return {"error": f"HDR 合并失败: {e}"}
    stem = os.path.splitext(os.path.basename(paths[0]))[0]
    out_path = _unique_path(dest_dir, stem + "_hdr", ".jpg")
    result.save(out_path, quality=95)
    send_log(f"HDR merged: {len(paths)} -> {out_path}", "info")
    out = {"ok": True, "path": out_path, "count": len(paths)}
    if warnings:
        out["warnings"] = warnings
    return out


@command("merge_panorama", "editor",
         "[工具] 全景拼接（cv2.Stitcher PANORAMA，2-15 张有重叠的照片）。"
         "输出 <dest_dir>/<首文件名>_pano.jpg，绝不覆盖已有文件。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"},
                        "description": "2-15 张有重叠的横移照片（按拍摄顺序）"},
             "dest_dir": {"type": "string", "description": "输出目录，默认首图所在目录"}},
          "required": ["paths"], "additionalProperties": False},
         agent_tool=True)
def handle_merge_panorama(params):
    ok, err = _validated_paths(params, _PANO_MIN, _PANO_MAX, "全景")
    if err:
        return {"error": err}
    paths, dest_dir = ok
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return {"error": "全景拼接需要 opencv（当前环境缺失）"}

    images = []
    for p in paths:
        try:
            img = Image.open(p).convert("RGB")
            img.thumbnail((4000, 4000), Image.BILINEAR)  # 限制画幅防内存爆
            images.append(cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR))
        except Exception:
            send_log(f"panorama: skip unreadable {p}", "warn")
            continue
    if len(images) < _PANO_MIN:
        return {"error": "有效照片不足 2 张"}

    stitcher = cv2.Stitcher.create(cv2.Stitcher_PANORAMA)
    status, pano = stitcher.stitch(images)
    if status != cv2.Stitcher_OK:
        reasons = {1: "需要更多重叠", 2: "参数估计失败", 3: "置信度不足"}
        return {"error": f"拼接失败: {reasons.get(int(status), f'code={status}')} "
                         f"（请确认照片有 20%+ 重叠且同一机位横移）"}
    stem = os.path.splitext(os.path.basename(paths[0]))[0]
    out_path = _unique_path(dest_dir, stem + "_pano", ".jpg")
    cv2.imwrite(out_path, pano, [cv2.IMWRITE_JPEG_QUALITY, 95])
    send_log(f"panorama stitched: {len(images)} -> {out_path}", "info")
    h, w = pano.shape[:2]
    return {"ok": True, "path": out_path, "count": len(images), "width": w, "height": h}


def register_handlers():
    command_registry.register_all()
    send_log("photo_merge module loaded (IPC via command_registry)", "info")
