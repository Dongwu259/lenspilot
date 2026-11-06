"""
photo_editor.py — 编辑器命令层 + 引擎门面（P3-9 拆分后）

原 1300+ 行单文件按职责拆为三层：
  editor_render.py — 渲染引擎（DEFAULT_EDITS/apply 管线/sidecar IO/预览缓存）
  editor_info.py   — 色彩空间 + EXIF 只读信息
  photo_editor.py  — IPC 命令（@command 经统一注册表）+ 向后兼容 re-export

架构不变：edits 存 sidecar JSON（{path}.edit.json），按需应用于预览/导出，
绝不改原图。所有既有 `from photo_editor import X` 引用面保持不变。
"""

import os
import json
import io
import base64
import threading
from PIL import Image, ImageEnhance
import numpy as np

from service import send_event, send_log
from model_loader import _load_image_rgb, make_thumbnail
import command_registry
from command_registry import command

# ── 引擎 re-export（向后兼容：tests / reference_match / editor_presets 等既有引用面）──
from editor_render import (  # noqa: F401
    DEFAULT_EDITS, apply_edits, _apply_geometry, _apply_pixel,
    load_edits, save_edits, has_edits, _edit_path, _thumb_path,
    _apply_edits_preview, render_edit_thumbnail, render_preset_thumbnails,
    OperationCancelled,
    _PV_CACHE, _PV_CACHE_MAX, _pv_lock, _pv_stats,
    _pv_file_fingerprint, _pv_geom_key,
)
from editor_info import _detect_color_space, _extract_exif  # noqa: F401

# Batch cancellation: IPC handlers run in a thread pool, so a cancel command can
# arrive while batch_apply_edits / batch_export_edited is looping. Flags are
# keyed by a frontend-generated task_id and consumed at file boundaries.
_BATCH_CANCEL = set()
_BATCH_CANCEL_LOCK = threading.Lock()

# Full-resolution edited image cache for >6000px chunked detail loading. A
# viewport tile is cut from this image, so a 9000px original is rendered once
# and subsequent pans only pay for a crop/JPEG encode. LRU 1 to bound memory.
_TILE_CACHE = {}
_TILE_CACHE_MAX = 1
_TILE_MAX_SIDE = 12000
_TILE_LOCK = threading.Lock()


def _batch_is_cancelled(task_id):
    if not task_id:
        return False
    with _BATCH_CANCEL_LOCK:
        return task_id in _BATCH_CANCEL


def _batch_clear_cancel(task_id):
    if not task_id:
        return
    with _BATCH_CANCEL_LOCK:
        _BATCH_CANCEL.discard(task_id)


@command("cancel_batch_task", "editor",
         "[编辑器] 取消正在运行的批量应用/批量导出任务（下一个文件边界生效）。",
         params_schema={"type": "object", "properties": {
             "task_id": {"type": "string", "description": "批量任务 id"}},
          "required": ["task_id"], "additionalProperties": False})
def handle_cancel_batch_task(params):
    task_id = str(params.get("task_id") or "")
    if not task_id:
        return {"error": "missing task_id"}
    with _BATCH_CANCEL_LOCK:
        _BATCH_CANCEL.add(task_id)
    return {"ok": True, "task_id": task_id}


# ── IPC Handlers（P0-1：经统一命令注册表登记，见 command_registry.py）──

@command("get_edits", "editor",
         "[编辑器] 读取照片当前的非破坏性编辑参数（sidecar 合并默认值）。只读。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_get_edits(params):
    """Return edit parameters for a photo."""
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}
    edits = load_edits(path)
    return {"edits": edits, "has_edits": os.path.exists(_edit_path(path))}

@command("apply_edits", "editor",
         "[编辑器] 应用编辑参数渲染照片：preview=true 返回 base64 预览图；"
         "preview=false 导出 <名字>_edited.jpg（绝不改原图）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"},
             "edits": {"type": "object", "description": "编辑参数 dict（省略则用已保存的）"},
             "preview": {"type": "boolean", "description": "true=预览(默认)；false=导出"},
             "preview_max_side": {"type": "integer", "description": "预览长边像素；默认 800，编辑器高倍放大/1:1 可传 1600-6000"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_apply_edits(params):
    """Apply edits and return a preview thumbnail (base64 JPEG, small)."""
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}

    edits = params.get("edits", {})
    if params.get("preview", True):
        return _apply_edits_preview(path, edits, params.get("preview_max_side", 800))

    try:
        img = _load_image_rgb(path)
        if img is None:
            return {"error": "Failed to load image"}

        # Merge with saved edits if not explicitly provided
        full_edits = load_edits(path)
        full_edits.update(edits)

        result = apply_edits(img, full_edits)

        # Save full-size edited copy
        dirname = os.path.dirname(path)
        basename = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(dirname, f"{basename}_edited.jpg")
        # Preserve EXIF metadata from original
        exif_data = None
        try:
            exif_data = img.info.get('exif')
        except Exception:
            pass
        result.save(out_path, "JPEG", quality=92, optimize=True, exif=exif_data or b'')
        # Invalidate original thumbnail cache so UI picks up changes
        try:
            thumb = make_thumbnail(result, path)
        except Exception:
            thumb = ""
        return {"path": out_path, "thumb": thumb, "edits": full_edits}
    except Exception as e:
        send_log(f"apply_edits failed: {e}", "error")
        return {"error": str(e)}


@command("apply_preset_thumbnails", "editor",
         "[编辑器] 预设卡片墙：一次解码渲染多个预设的小图预览（前端按强度缩放后传入）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"},
             "base_edits": {"type": "object", "description": "当前编辑参数（缩略图的基准）"},
             "presets": {"type": "array", "items": {"type": "object"},
                         "description": "[{id, edits}] 预设列表，edits 已按强度缩放"},
             "max_side": {"type": "integer", "description": "卡片长边像素，默认 180（96-320）"}},
          "required": ["path", "presets"], "additionalProperties": False},
         agent_tool=False, workfolder_paths=["path"])
def handle_apply_preset_thumbnails(params):
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}
    return render_preset_thumbnails(path, params.get("base_edits") or {},
                                    params.get("presets") or [],
                                    params.get("max_side", 180))


@command("apply_edits_tile", "editor",
         "[编辑器] 超大原图分块细节：按归一化区域返回当前视口的原生细节图。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"},
             "edits": {"type": "object"},
             "region": {"type": "array", "items": {"type": "number"},
                        "description": "[x,y,w,h] 归一化 0-1（相对编辑后完整图）"},
             "out_width": {"type": "integer", "description": "输出像素宽"},
             "out_height": {"type": "integer", "description": "输出像素高"}},
          "required": ["path", "region"], "additionalProperties": False})
def handle_apply_edits_tile(params):
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}
    region = params.get("region") or []
    try:
        x, y, w, h = [float(v) for v in region]
    except (TypeError, ValueError):
        return {"error": "Invalid region"}
    if len(region) != 4 or w <= 0 or h <= 0:
        return {"error": "Invalid region"}

    full_edits = load_edits(path)
    full_edits.update(params.get("edits") or {})
    mtime, size = _pv_file_fingerprint(path)
    key = (path, mtime, size, json.dumps(full_edits, sort_keys=True, default=str))
    with _TILE_LOCK:
        entry = _TILE_CACHE.pop(key, None)
        if entry is not None:
            _TILE_CACHE[key] = entry  # LRU touch
    if entry is None:
        try:
            img = _load_image_rgb(path)
            if img is None:
                return {"error": "Failed to load image"}
            render_edits = full_edits
            # Cap pathological originals to bound memory; crop coordinates are
            # in post-geometry space, so scale them with the decode ratio.
            w0, h0 = img.size
            scale = min(1.0, _TILE_MAX_SIDE / max(w0, h0)) if max(w0, h0) else 1.0
            if scale < 1.0:
                img = img.resize((max(1, round(w0 * scale)), max(1, round(h0 * scale))),
                                 Image.LANCZOS)
                render_edits = dict(full_edits)
                crop = render_edits.get("crop")
                if crop and len(crop) == 4:
                    render_edits["crop"] = [int(c * scale) for c in crop]
            rendered = apply_edits(img, render_edits)
            with _TILE_LOCK:
                _TILE_CACHE[key] = rendered
                while len(_TILE_CACHE) > _TILE_CACHE_MAX:
                    _TILE_CACHE.pop(next(iter(_TILE_CACHE)))
            entry = rendered
        except Exception as e:
            send_log(f"apply_edits_tile failed: {e}", "error")
            return {"error": str(e)}

    try:
        W, H = entry.size
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.001, min(1.0 - x, w))
        h = max(0.001, min(1.0 - y, h))
        box = (int(x * W), int(y * H),
               max(int(x * W) + 1, int((x + w) * W)),
               max(int(y * H) + 1, int((y + h) * H)))
        tile = entry.crop(box)
        try:
            out_w = max(16, min(4096, int(params.get("out_width") or 0)))
            out_h = max(16, min(4096, int(params.get("out_height") or 0)))
        except (TypeError, ValueError):
            out_w = out_h = 0
        if out_w and out_h:
            tile = tile.resize((out_w, out_h), Image.LANCZOS)
        buf = io.BytesIO()
        tile.save(buf, format="JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "preview": "data:image/jpeg;base64," + b64,
            "full_width": W, "full_height": H,
            "region": [x, y, w, h],
            "tile_width": tile.size[0], "tile_height": tile.size[1],
        }
    except Exception as e:
        send_log(f"apply_edits_tile crop failed: {e}", "error")
        return {"error": str(e)}


def _journal(op, path, edits=None, count=None):
    """P2-2 追加式编辑日志（best-effort，绝不影响编辑主流程）。"""
    try:
        from edit_journal import append as journal_append
        keys = None
        if edits is not None:
            keys = sorted(k for k, v in (edits or {}).items()
                          if v not in (None, "", 0, 1, False))
        journal_append(op, path, keys=keys, count=count)
    except Exception:
        pass


def _suggested_stats_reusable(path, sug):
    """建议快照的 stats 可否直接复用（P2-4）：有 stats 且照片文件自建议后未被替换。"""
    if not sug or not sug.get("stats"):
        return False
    try:
        return os.path.getmtime(path) <= float(sug.get("ts") or 0) + 2.0
    except OSError:
        return True  # 取不到 mtime 时按可复用处理，避免无谓重算


@command("save_edits", "editor",
         "[编辑器] 把编辑参数持久化到照片 sidecar（不渲染像素；可 reset_edits 撤销）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"},
             "edits": {"type": "object", "description": "编辑参数 dict"}},
          "required": ["path", "edits"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_save_edits(params):
    """Persist edit parameters to disk."""
    path = params.get("path", "")
    edits = params.get("edits", {})
    if not path:
        return {"error": "Invalid path"}
    ok = save_edits(path, edits)
    if ok:
        _journal("save", path, edits=edits)
        try:  # 记忆层落定（数据飞轮闭环, best-effort 绝不影响保存）
            from edit_memory import (enabled as mem_enabled, load as mem_load,
                                     latest_suggested, settle as mem_settle)
            if mem_enabled():
                st = None
                # 单次 load 复用：快照检索与落定配对共用一份 entries（P2-6b）
                entries = mem_load()
                # P2-4: 建议时算过同一张原图的统计 → 直接复用快照, 不再二次解码
                if not _suggested_stats_reusable(path, latest_suggested(path, entries=entries)):
                    from photo_stats import compute_stats
                    img0 = _load_image_rgb(path)
                    if img0 is not None:
                        st = compute_stats(img0)
                mem_settle(path, edits, stats=st, entries=entries)
        except Exception:
            pass
    # Invalidate thumbnail so it gets regenerated
    try:
        tp = _thumb_path(path)
        if os.path.exists(tp):
            os.remove(tp)
    except Exception:
        pass
    return {"ok": ok}


@command("reset_edits", "editor",
         "[编辑器] 清除照片全部编辑参数，恢复原图状态（删除 sidecar，不可逆）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         destructive=True)
def handle_reset_edits(params):
    """Remove all edits for a photo (reset to original)."""
    path = params.get("path", "")
    if not path:
        return {"error": "Invalid path"}
    try:
        ep = _edit_path(path)
        if os.path.exists(ep):
            os.remove(ep)
        _journal("reset", path)
        # Clean up edited thumbnails
        tp = _thumb_path(path)
        if os.path.exists(tp):
            os.remove(tp)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@command("batch_apply_edits", "editor",
         "[编辑器] 把同一组编辑参数批量应用到多张照片（写各图 sidecar + 刷新缩略图）。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"}, "description": "照片路径列表"},
             "edits": {"type": "object", "description": "编辑参数 dict"},
             "task_id": {"type": "string", "description": "可取消任务 id"}},
          "required": ["paths", "edits"], "additionalProperties": False},
         destructive=True)
def handle_batch_apply_edits(params):
    """把同一组编辑参数批量应用到多张照片（保存 sidecar + 刷新缩略图）。

    params: paths(列表,必填) / edits(编辑参数 dict)
    返回: processed / failed, failed_paths
    """
    paths = params.get("paths") or []
    edits = params.get("edits") or {}
    task_id = str(params.get("task_id") or "")
    if not paths:
        return {"error": "缺少 paths"}
    # P2-5 批量轻量落定：entries 预读一次复用；每张不带 stats（审计链完整，
    # 检索自动跳过无 stats 条目），各自配对其此前最近一条 AI 建议（如有）
    mem_settle, mem_entries = None, None
    try:
        from edit_memory import (enabled as _mem_enabled, load as _mem_load,
                                 settle as _mem_settle)
        if _mem_enabled():
            mem_entries = _mem_load()
            mem_settle = _mem_settle
    except Exception:
        mem_settle, mem_entries = None, None
    processed, failed, failed_paths = 0, 0, []
    cancelled = False
    for p in paths:
        if _batch_is_cancelled(task_id):
            cancelled = True
            break
        try:
            if not os.path.exists(p):
                raise FileNotFoundError(p)
            img = _load_image_rgb(p)
            if img is None:
                raise ValueError(f"无法加载: {p}")
            if _batch_is_cancelled(task_id):
                cancelled = True
                break
            full_edits = load_edits(p)
            full_edits.update(edits)
            # 缩略图级渲染（第 2 项优化）：此处只为刷新缩略图，
            # 不必对全尺寸原图跑完整像素管线（LUT/HSL/降噪等在小图上等价）
            out, _thumb_scale = render_edit_thumbnail(
                img, full_edits, cancel_check=lambda: _batch_is_cancelled(task_id))
            # 持久化编辑参数（save_edits 只存非默认值）
            save_edits(p, full_edits)
            if mem_settle and mem_entries is not None:
                try:
                    mem_settle(p, full_edits, stats=None, entries=mem_entries)
                except Exception:
                    pass
            # 刷新缩略图缓存
            try:
                make_thumbnail(out, p)
            except Exception:
                pass
            processed += 1
        except OperationCancelled:
            cancelled = True
            break
        except Exception as e:
            failed += 1
            failed_paths.append({"path": p, "error": str(e)})
    _batch_clear_cancel(task_id)
    send_log(f"批量应用编辑{cancelled and '（已取消）' or ''}: {processed} 成功 / {failed} 失败",
             "info" if not failed and not cancelled else "warn")
    if processed:
        _journal("batch", paths[0], edits=edits, count=processed)
    return {"ok": True, "cancelled": cancelled, "processed": processed,
            "failed": failed, "failed_paths": failed_paths[:50]}


# Preset batch: a look owns tone/style keys, but never the target's geometry,
# spot repairs, lens corrections, or camera calibration. Masks are kept from
# the target when the preset does not define its own (presets with sky masks
# are content-aware looks and do transfer).
_PRESET_KEEP_TARGET = (
    "rotation", "flip_h", "flip_v", "crop", "rotate_deg",
    "spots", "redeye", "optics", "calibration", "srgb", "auto_straighten",
)
_PRESET_KEEP_TARGET_IF_ABSENT = ("masks", "log_curve")


def _merge_preset_edits(target, preset, adaptive=None):
    merged = dict(DEFAULT_EDITS)
    merged.update(preset or {})
    for k in _PRESET_KEEP_TARGET:
        merged[k] = target.get(k, DEFAULT_EDITS.get(k))
    for k in _PRESET_KEEP_TARGET_IF_ABSENT:
        if not (preset or {}).get(k) and target.get(k) is not None:
            merged[k] = target.get(k)
    merged["wb_reference"] = ""  # source-photo gray card must not leak
    if adaptive:
        def clamp(v, lo, hi):
            return max(lo, min(hi, v))
        # Baseline normalization combines with the look; style keys stay as-is.
        merged["exposure"] = round(clamp(
            float(merged.get("exposure") or 0) + float(adaptive.get("exposure") or 0),
            -3.0, 3.0), 3)
        merged["contrast"] = round(clamp(
            float(merged.get("contrast") or 1.0) * float(adaptive.get("contrast") or 1.0),
            0.2, 2.0), 3)
        merged["warmth"] = round(clamp(
            float(merged.get("warmth") or 0) + float(adaptive.get("warmth") or 0),
            -1.0, 1.0), 3)
        merged["wb_tint"] = int(round(clamp(
            float(merged.get("wb_tint") or 0) + float(adaptive.get("wb_tint") or 0),
            -100.0, 100.0)))
    return merged


@command("batch_apply_preset", "editor",
         "[编辑器] 把预设批量套用到多张照片（逐张自适应曝光/白平衡，保留各自几何/污点/镜头校正）。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"}, "description": "照片路径列表"},
             "preset_edits": {"type": "object", "description": "已按强度缩放的预设参数"},
             "source_stats": {"type": "object", "description": "用户预设的来源照片统计卡（可省略）"},
             "reference_stats": {"type": "object", "description": "参考型预设的目标统计卡（优先于 preset_edits）"},
             "adaptive": {"type": "boolean", "description": "是否先做每张基线归一"},
             "adaptive_strength": {"type": "number", "description": "基线归一/参考匹配强度 0-1.5，默认 1"},
             "task_id": {"type": "string", "description": "可取消任务 id"}},
          "required": ["paths"], "additionalProperties": False},
         destructive=True)
def handle_batch_apply_preset(params):
    """把预设套用到多张照片：逐张统计 → 参考匹配或自适应基线 → 套用 → 保存/刷新缩略图。"""
    paths = params.get("paths") or []
    preset_edits = params.get("preset_edits")
    reference_stats = params.get("reference_stats")
    reference_mode = isinstance(reference_stats, dict) and bool(reference_stats)
    if not paths:
        return {"error": "缺少 paths"}
    if not reference_mode and (not isinstance(preset_edits, dict) or not preset_edits):
        return {"error": "预设为空"}
    if not reference_mode:
        reference_stats = None
    source_stats = params.get("source_stats")
    adaptive = bool(params.get("adaptive"))
    try:
        strength = max(0.0, min(1.5, float(params.get("adaptive_strength") or 1.0)))
    except (TypeError, ValueError):
        strength = 1.0
    task_id = str(params.get("task_id") or "")

    adaptive_fn = None
    if adaptive:
        try:
            from reference_match import adaptive_baseline_edits as adaptive_fn
        except Exception as e:
            send_log(f"batch_apply_preset: adaptive unavailable: {e}", "warn")

    mem_settle, mem_entries = None, None
    try:
        from edit_memory import (enabled as _mem_enabled, load as _mem_load,
                                 settle as _mem_settle)
        if _mem_enabled():
            mem_entries = _mem_load()
            mem_settle = _mem_settle
    except Exception:
        mem_settle, mem_entries = None, None

    processed, failed, failed_paths = 0, 0, []
    cancelled = False
    for p in paths:
        if _batch_is_cancelled(task_id):
            cancelled = True
            break
        try:
            if not os.path.exists(p):
                raise FileNotFoundError(p)
            img = _load_image_rgb(p)
            if img is None:
                raise ValueError(f"无法加载: {p}")
            if _batch_is_cancelled(task_id):
                cancelled = True
                break
            target = load_edits(p)
            adaptive_edits = {}
            if reference_mode:
                # Reference presets are inherently per-photo: fit the target
                # stats card on this photo instead of using fixed values.
                try:
                    from reference_match import suggest_edits
                    look_edits, _ = suggest_edits(img, reference_stats, strength)
                except Exception as e:
                    raise ValueError(f"参考匹配失败: {e}")
            else:
                look_edits = preset_edits
                if adaptive and adaptive_fn:
                    try:
                        adaptive_edits = adaptive_fn(img, source_stats, strength)
                    except Exception as e:
                        send_log(f"preset adaptive failed ({os.path.basename(p)}): {e}", "warn")
            merged = _merge_preset_edits(target, look_edits, adaptive_edits)
            out, _thumb_scale = render_edit_thumbnail(
                img, merged, cancel_check=lambda: _batch_is_cancelled(task_id))
            save_edits(p, merged)
            if mem_settle and mem_entries is not None:
                try:
                    mem_settle(p, merged, stats=None, entries=mem_entries)
                except Exception:
                    pass
            try:
                make_thumbnail(out, p)
            except Exception:
                pass
            processed += 1
        except OperationCancelled:
            cancelled = True
            break
        except Exception as e:
            failed += 1
            failed_paths.append({"path": p, "error": str(e)})
    _batch_clear_cancel(task_id)
    send_log(f"批量套用预设{cancelled and '（已取消）' or ''}: {processed} 成功 / {failed} 失败",
             "info" if not failed and not cancelled else "warn")
    if processed:
        _journal("batch", paths[0],
                 edits=preset_edits if not reference_mode else {"reference": True},
                 count=processed)
    return {"ok": True, "cancelled": cancelled, "processed": processed,
            "failed": failed, "failed_paths": failed_paths[:50]}


@command("auto_enhance", "editor",
         "[编辑器] 一键自动增强（直方图拉伸 + 轻锐化 + 微饱和），返回预览与建议参数。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False})
def handle_auto_enhance(params):
    """Auto-enhance: basic histogram stretching + slight sharpen."""
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}

    try:
        img = _load_image_rgb(path)
        if img is None:
            return {"error": "Failed to load image"}

        # Histogram equalization per channel
        arr = np.array(img, dtype=np.float32)
        for c in range(3):
            ch = arr[:, :, c]
            lo, hi = np.percentile(ch, 2), np.percentile(ch, 98)
            if hi - lo > 10:
                ch = np.clip((ch - lo) / (hi - lo) * 255, 0, 255)
            arr[:, :, c] = ch
        img = Image.fromarray(arr.astype(np.uint8))

        # Moderate sharpening
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.2)

        # Slight saturation boost
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(1.05)

        # Generate preview
        img.thumbnail((800, 800), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # Return suggested edits
        suggested = dict(DEFAULT_EDITS)
        suggested["sharpness"] = 0.3
        suggested["saturation"] = 1.05
        return {"preview": "data:image/jpeg;base64," + b64, "edits": suggested}
    except Exception as e:
        return {"error": str(e)}


def _dedupe_variants(pairs, limit=3):
    """[(label, options|None), ...] → 建议变体列表（参数 JSON 去重，保序取前 limit）。

    P1-1：AI 建议卡的"换个方案"。空/None 丢弃；首项恒为当前已应用方案；
    只剩一个时返回 None（无可切换，UI 不出按钮）。
    """
    seen, out = set(), []
    for label, cand in pairs:
        if not cand:
            continue
        sig = json.dumps(cand, sort_keys=True, ensure_ascii=False)
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"label": label, "options": cand})
    return out[:max(2, int(limit))] if len(out) > 1 else None



@command("auto_tone_editor", "editor",
         "[编辑器/AI] AI 自动修图：lr-v1 蒸馏大脑（用户 LR 风格）优先，降级 auto→suggest；"
         "返回预览与 9 字段参数快照（不改像素，配合 save_edits 持久化）；"
         "个性化记忆参与时附 auto_tone.memory 元信息与建议变体 top-3。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"},
             "engine": {"type": "string", "description": "distill/auto/suggest，默认 auto"},
             "strength": {"type": "number", "description": "0-1 强度，默认 1.0"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_auto_tone_editor(params):
    """AI 自动修图：对源文件预测一次 LR 式调色参数，返回预览 + 参数快照。

    engine=distill：lr-v1 蒸馏大脑（用户 LR 修图风格，Qwen3-VL-4B+LoRA +
    P67 Inspector 诊断文字），带决策叙事（scene/reasoning/summary）回传；
    engine=auto：photo_s auto-tone 插件预测 9 字段（权重未就绪自动降级
    suggest，绝不隐式下载 GB 级权重）；engine=suggest：零模型规则层。
    降级链 distill → auto → suggest，任何一层失败自动下落。
    本 handler 不改像素——参数经返回值进 edits 持久化（save_edits），预览/
    导出统一走 apply_edits 的 auto_tone 键应用（全分辨率、可重放）。
    """
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}
    try:
        strength = max(0.0, min(1.0, float(params.get("strength", 1.0) or 1.0)))
    except (TypeError, ValueError):
        strength = 1.0
    engine = str(params.get("engine", "auto") or "auto").strip().lower()
    if engine not in ("auto", "suggest", "distill"):
        engine = "auto"

    used_engine = engine
    options, confidence, warnings = {}, 0.0, []
    narrative = None
    model_version = None
    if engine == "distill":
        # lr-v1 蒸馏大脑（用户 LR 修图风格，Qwen3-VL-4B+LoRA）：资产未就绪 /
        # 推理失败 → 降级 auto（再由 auto 分支降级 suggest），绝不隐式下载。
        try:
            from photo_s_plugin_auto_tone.core.lr_distill import get_distill_advisor
            adv = get_distill_advisor()
            if not adv.ready():
                used_engine = "auto"
                warnings.append(f"lr-v1 not ready ({adv.error}), fallback to auto")
            else:
                # 走 _load_image_rgb（RAW → 内嵌预览，与编辑器所见一致）；
                # advise 内部再降到 ≤2048 做诊断与推理
                img_in = _load_image_rgb(path)
                if img_in is None:
                    used_engine = "auto"
                    warnings.append("lr-v1: failed to load image, fallback to auto")
                else:
                    res = adv.advise(img_in, name=os.path.basename(path),
                                     strength=strength)
                    if res.get("options"):
                        options = res["options"]
                        # 学生无置信度头；bench 口径 JSON 100% 合规 → 固定高置信
                        confidence = 0.9
                        model_version = res.get("model_version", "lr-v1")
                        narrative = res.get("narrative")
                    else:
                        used_engine = "auto"
                        warnings.append(
                            f"lr-v1 predict failed ({res.get('error')}), fallback to auto")
        except Exception as e:
            used_engine = "auto"
            warnings.append(f"lr-v1 error: {e}, fallback to auto")

    if used_engine == "auto":
        try:
            import photo_s_plugins
            status = photo_s_plugins.ai_engine_status()
            if not status.get("ready"):
                used_engine = "suggest"
                warnings.append(f"AI engine not ready ({status.get('reason')}), fallback to suggest")
            else:
                from photo_s.engine import ProcessOptions
                from photo_s.hooks import PluginContext
                from photo_s.plugin import find_provider
                provider = find_provider("auto_tone")
                ctx = PluginContext(input_path=path, options=ProcessOptions())
                pred = provider.auto_tone_params(strength, ctx)
                options = pred.get("options") or {}
                confidence = float(pred.get("confidence") or 0.0)
                warnings.extend(pred.get("warnings") or [])
        except Exception as e:
            used_engine = "suggest"
            warnings.append(f"AI predict failed: {e}")

    if used_engine == "suggest":
        try:
            from photo_s.suggest import suggest_file
            s = suggest_file(path, scale=strength)
            if not s.get("ok"):
                return {"error": s.get("error") or "suggest failed"}
            # suggest 产出 ProcessOptions 字段名(ev/highlight_recovery/levels...)，
            # 映射到 9 字段参数协议（apply_auto_tone_params 的词汇表）；无等价项丢弃
            _FIELD_MAP = {"ev": "exposure"}
            options = {_FIELD_MAP.get(k, k): v for k, v in (s.get("suggested") or {}).items()
                       if _FIELD_MAP.get(k, k) in ("exposure", "contrast", "saturation",
                                                   "vibrance", "wb_temp", "wb_tint",
                                                   "clarity", "texture", "dehaze")}
            for r in (s.get("reasons") or [])[:5]:
                advice = r.get("advice") or ""
                if advice:
                    warnings.append(advice)
        except Exception as e:
            return {"error": f"predict failed: {e}"}

    # 预览：缩略图上应用参数（快）；全分辨率应用统一走 apply_edits
    try:
        img = _load_image_rgb(path)
        if img is None:
            return {"error": "Failed to load image"}
        # 记忆层（JitRL 式, best-effort）：建议快照 + 同批折扣检索重排
        # （E2 定案: 冻结先验 + 固定 β=0.5; 记忆不足自动回退先验）
        # P2-6 总开关关闭 → 整块旁路（不快照/不重排/不下发 variants）
        mem_info = None
        variants = None
        try:
            from edit_memory import (blend_with_prior, enabled as mem_enabled,
                                     memory_median, suggest as mem_suggest)
            from photo_stats import compute_stats
            if mem_enabled():
                st = compute_stats(img)
                mem_suggest(path, st, options)
                if options:
                    prior_options = options
                    blend = blend_with_prior(options, st,
                                             batch=os.path.dirname(path), beta=0.5)
                    if blend.get("used_memory") and blend.get("params"):
                        options = blend["params"]
                    mem_info = {"used_memory": bool(blend.get("used_memory")),
                                "confidence": blend.get("confidence")}
                    # P1-1 建议变体：当前方案 / 原始 AI 先验 / 纯个人风格中位数
                    med = memory_median(st, batch=os.path.dirname(path))[0]
                    variants = _dedupe_variants(
                        [("memory", options), ("prior", prior_options), ("personal", med)])
        except Exception as e:
            mem_info = {"error": str(e)[:120]}
        small = img.copy()
        small.thumbnail((800, 800), Image.LANCZOS)
        if options:
            from photo_s.autotone import apply_auto_tone_params
            small = apply_auto_tone_params(small, {"options": options})
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        return {"error": str(e)}

    auto_tone = {"engine": used_engine, "options": options,
                 "confidence": round(confidence, 3), "strength": strength,
                 "warnings": warnings}
    if mem_info:
        auto_tone["memory"] = mem_info
    if variants:
        auto_tone["variants"] = variants
    if model_version:
        auto_tone["model_version"] = model_version
    if narrative:
        auto_tone["narrative"] = narrative
    return {"preview": "data:image/jpeg;base64," + b64,
            "auto_tone": auto_tone}


# ── Color space detection ──

@command("get_image_info", "editor",
         "[编辑器] 读取照片的色彩空间 / 尺寸 / EXIF 摘要。只读。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_get_image_info(params):
    """Return color space and EXIF info for a photo."""
    path = params.get('path', '')
    if not path or not os.path.exists(path):
        return {'error': 'Invalid path'}
    try:
        img = Image.open(path)
        info = {
            'colorSpace': _detect_color_space(img),
            'width': img.width,
            'height': img.height,
        }
        # EXIF (only for JPEG/TIFF, not RAW)
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.jpg', '.jpeg', '.tif', '.tiff', '.heic', '.heif'):
            info['exif'] = _extract_exif(img)
        else:
            info['exif'] = {}
        return info
    except Exception:
        pass
    # RAW 等 PIL 打不开的格式: 用 _load_image_rgb(内嵌预览)取尺寸,
    # 与 apply_edits 的处理对象一致,保证前端裁剪坐标换算正确
    try:
        img = _load_image_rgb(path)
        if img is None:
            return {'error': 'Failed to load image'}
        return {
            'colorSpace': _detect_color_space(img),
            'width': img.width,
            'height': img.height,
            'exif': {},
        }
    except Exception as e:
        return {'error': str(e)}


# ── Batch export (LR 式批量导出) ──

_EXPORT_FORMATS = {
    "JPEG": (".jpg", "JPEG"),
    "PNG": (".png", "PNG"),
    "WEBP": (".webp", "WEBP"),
    "TIFF": (".tif", "TIFF"),
}


def _export_unique_path(dest_dir, stem, ext):
    """目标路径冲突自动加序号,绝不覆盖已有文件。"""
    out = os.path.join(dest_dir, f"{stem}_edit{ext}")
    n = 1
    while os.path.exists(out):
        out = os.path.join(dest_dir, f"{stem}_edit_{n}{ext}")
        n += 1
    return out


@command("batch_export_edited", "editor",
         "[编辑器] 批量导出：应用各图 sidecar 编辑 + 格式/尺寸/色彩空间/水印，"
         "输出到指定文件夹（同名自动加序号，绝不覆盖已有文件）。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"}},
             "dest_dir": {"type": "string"},
             "format": {"type": "string", "description": "JPEG/PNG/WEBP/TIFF，默认 JPEG"},
             "quality": {"type": "integer", "description": "1-100，默认 90"},
             "max_long_edge": {"type": "integer", "description": "长边封顶，0=原图"},
             "color_space": {"type": "string", "description": "srgb/original"},
             "watermark": {"type": "object",
                           "description": "{type:'text'|'image', text, path, position, opacity, scale} 或 null"},
             "task_id": {"type": "string", "description": "可取消任务 id"}},
          "required": ["paths", "dest_dir"], "additionalProperties": False})
def handle_batch_export_edited(params):
    """批量导出:应用 sidecar 编辑 + 格式/尺寸/色彩空间/水印,输出到指定文件夹。

    params: paths, dest_dir, format(JPEG/PNG/WEBP/TIFF), quality,
            max_long_edge(0=原图), color_space(srgb/original),
            watermark{type:'text'|'image', text/path, position, opacity, scale} 或 None
    """
    paths = params.get("paths") or []
    dest_dir = (params.get("dest_dir") or "").strip()
    task_id = str(params.get("task_id") or "")
    fmt = str(params.get("format") or "JPEG").upper()
    if not paths:
        return {"error": "No photos selected"}
    if not dest_dir:
        return {"error": "No destination folder"}
    if fmt not in _EXPORT_FORMATS:
        return {"error": f"Unsupported format: {fmt}"}
    try:
        quality = max(1, min(100, int(params.get("quality") or 90)))
    except (TypeError, ValueError):
        quality = 90
    try:
        max_edge = max(0, int(params.get("max_long_edge") or 0))
    except (TypeError, ValueError):
        max_edge = 0
    color_space = params.get("color_space") or "srgb"
    wm = params.get("watermark") or None

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        return {"error": f"Cannot create destination: {e}"}

    ext, pil_fmt = _EXPORT_FORMATS[fmt]
    total = len(paths)
    exported = 0
    cancelled = False
    errors = []

    for i, path in enumerate(paths):
        if _batch_is_cancelled(task_id):
            cancelled = True
            break
        name = os.path.basename(path)
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError(name)
            img = _load_image_rgb(path)
            if img is None:
                raise ValueError("load failed")
            if _batch_is_cancelled(task_id):
                raise OperationCancelled()
            src_info = dict(img.info)  # LUT 等步骤可能丢 info,留底
            out = apply_edits(img, load_edits(path),
                              cancel_check=lambda: _batch_is_cancelled(task_id))

            # 尺寸:长边封顶(只缩不放)
            if max_edge and max(out.size) > max_edge:
                w, h = out.size
                scale = max_edge / max(w, h)
                out = out.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                 Image.LANCZOS)

            # 色彩空间:sRGB 打标 / 保留原始 ICC
            if color_space == "srgb":
                try:
                    from photo_s.adjust import apply_color_management
                    out = apply_color_management(out, srgb=True)
                except Exception as e:
                    send_log(f"batch_export: color mgmt skipped: {e}", "warn")

            # 水印：文字 / 图片 Logo（图片优先由 type 决定）
            if wm:
                try:
                    wm_type = str(wm.get("type") or
                                  ("image" if wm.get("path") else "text")).lower()
                    position = str(wm.get("position") or "BOTTOM_RIGHT")
                    opacity = max(1, min(100, int(wm.get("opacity") or 60)))
                    margin = max(8, round(min(out.size) * 0.02))
                    if wm_type == "image" and wm.get("path"):
                        from photo_s.watermark import apply_image_watermark
                        logo = str(wm["path"])
                        if not os.path.isfile(logo):
                            send_log(f"batch_export: watermark image missing: {logo}", "warn")
                        else:
                            scale = max(3, min(60, int(wm.get("scale") or 15)))
                            out = apply_image_watermark(
                                out, logo, position=position,
                                scale=scale, opacity=opacity, margin=margin)
                    elif wm.get("text"):
                        from photo_s.watermark import apply_text_watermark
                        font_size = max(18, round(min(out.size) * 0.028))
                        out = apply_text_watermark(
                            out, str(wm["text"]),
                            position=position,
                            font_size=font_size, opacity=opacity,
                            margin=round(font_size * 0.8))
                except Exception as e:
                    send_log(f"batch_export: watermark skipped: {e}", "warn")

            if _batch_is_cancelled(task_id):
                raise OperationCancelled()
            out_path = _export_unique_path(
                dest_dir, os.path.splitext(name)[0], ext)
            save_kw = {}
            if pil_fmt in ("JPEG", "WEBP"):
                save_kw["quality"] = quality
            if pil_fmt == "JPEG":
                save_kw["optimize"] = True
            exif_bytes = out.info.get("exif") or src_info.get("exif")
            if exif_bytes and pil_fmt in ("JPEG", "TIFF", "WEBP"):
                save_kw["exif"] = exif_bytes
            icc = out.info.get("icc_profile") or \
                (src_info.get("icc_profile") if color_space != "srgb" else None)
            if icc:
                save_kw["icc_profile"] = icc
            if out.mode not in ("RGB", "L") and pil_fmt == "JPEG":
                out = out.convert("RGB")
            out.save(out_path, pil_fmt, **save_kw)
            exported += 1
        except OperationCancelled:
            cancelled = True
            break
        except Exception as e:
            errors.append(f"{name}: {e}")
            send_log(f"batch_export failed for {name}: {e}", "error")
        send_event("batch_export_progress",
                   {"current": i + 1, "total": total, "name": name})

    _batch_clear_cancel(task_id)
    return {
        "ok": True, "cancelled": cancelled, "exported": exported, "failed": len(errors),
        "errors": errors[:20], "dest_dir": dest_dir,
    }


def register_handlers():
    # P0-1 统一命令注册表：本域命令已在各 @command 装饰器登记，
    # 这里一次性挂接（含 registry.list 等其它已迁移域，重复注册无害）
    command_registry.register_all()
    send_log("photo_editor module loaded (IPC via command_registry)", "info")
    try:
        from editor_presets import register_preset_handlers
        register_preset_handlers()
    except Exception as e:
        send_log(f"Warning: editor_presets not loaded: {e}", "error")
