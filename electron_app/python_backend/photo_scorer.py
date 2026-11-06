"""
photo_scorer.py — AI Photo scoring and filtering module
Adapted from:
  - D:\\VSproject\\Cporch\\photo_selector\\photo_selector.py
  - D:\\VSproject\\Cporch\\pytorch\\tools\\photo_filter.py

Uses the ResNet-18 model to score photos and filter excellent ones.
"""

import os
import sys
import shutil
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from service import send_event, send_log, register_handler
from model_loader import (
    load_model, predict_score, handle_get_cache_info, handle_clear_raw_cache,
    _load_image_rgb, _infer, make_thumbnail, make_heatmap, model_version, RAW_EXTS,
    get_backend_pref_info, _read_backend_pref, _write_backend_pref, _cuda_available,
    compute_blur_score, extract_deep_feature, extract_context_feature, detect_eyes, load_eye_model,
    preprocess_image, infer_batch, get_backend_info,
)
import score_cache

# ── Global state ──
_model_loaded = False
_model_lock = threading.Lock()
# 两条评分链各用独立取消标志：一条链开始评分不会清掉另一条链的取消
_score_cancel = threading.Event()        # handle_score_photos（整文件夹评分）
_score_files_cancel = threading.Event()  # handle_score_files（score_files/watch）

from utils import IMAGE_EXTS as IMAGE_EXTENSIONS


def _feedback_opt_in():
    """True only when the user has explicitly enabled feedback sharing."""
    try:
        import service as _svc
        return bool(_svc._app_config.get("feedback_share_enabled", False))
    except Exception:
        return False


def _camera_model(path):
    """Best-effort camera model from EXIF; '' on any failure."""
    try:
        from PIL import Image as _PILImage
        exif = _PILImage.open(path)._getexif() or {}
        return str(exif.get(271, "") or "").strip()[:128]  # 271 = Model
    except Exception:
        return ""


def _capture_feedback(path, label, source, model_score=None, category="", override=False):
    """Enqueue one selection-feedback record (opt-in only; best-effort).

    Captures a 512-d layer4 CONTEXT feature (retains within-scene detail, unlike
    the 128-d dedup feature) plus the interpretable scalars a selection-decision
    head needs: score, blur, eye_open. eye_open is filled by the classical eye
    detector when available (else None). label: 1 keep/winner, 0 reject/loser.
    override=True marks records where a human reversed the pipeline's decision.
    Never raises — must not break the calling handler.
    """
    if not _feedback_opt_in():
        return
    try:
        import feedback_queue
        if not _model_loaded:
            return
        img = _load_image_rgb(path)
        if img is None:
            return
        feat = extract_context_feature(img)
        if feat is None:
            return
        if model_score is None:
            try:
                cached = score_cache.get_score(path, model_version())
                if cached and cached.get("score", -1) >= 0:
                    model_score = float(cached["score"])
            except Exception:
                pass
        try:
            blur = float(compute_blur_score(img))
        except Exception:
            blur = None
        # eye_open ∈ [0,1] from the geometric EAR detector (landmark ONNX).
        # None when no reliable face (profile / landscape) — never guess.
        eye_open = None
        try:
            import eye_detector
            import numpy as _np
            eye_open = eye_detector.eye_open_score(_np.ascontiguousarray(_np.asarray(img)[:, :, ::-1]))  # PIL RGB → contiguous BGR
        except Exception:
            pass
        feedback_queue.enqueue({
            "model_version": model_version(),
            "feature": feat.tolist() if hasattr(feat, "tolist") else list(feat),
            "label": int(label),
            "source": source,
            "category": category or "",
            "camera_model": _camera_model(path),
            "score": model_score,
            "blur": blur,
            "eye_open": eye_open,
            "override": override,
        })
    except Exception:
        pass



# ── Directory listing cache (#106) ──
# find_images() 被评分/扫描/去重/列表等多个命令反复调用，对几千张 RAW 的
# 目录树做全量 os.walk 很慢。进程内缓存：键 = 规范化文件夹路径，TTL 默认
# 45s（APS_DIR_CACHE_TTL_SEC 可覆盖），命中时再对根目录 mtime 做一次廉价
# 校验（顶层文件增删/重命名会改 mtime，立即触发重扫）。子目录内的变更由
# TTL 兜底；本进程内的写操作则通过 invalidate_dir_cache() 精确失效。
_DIR_CACHE_TTL = float(os.environ.get("APS_DIR_CACHE_TTL_SEC", "45"))
_DIR_CACHE_MAX = 32
_dir_list_cache = {}            # key -> (cached_at, root_mtime, [paths])
_dir_list_lock = threading.Lock()


def _dir_cache_key(folder):
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


def invalidate_dir_cache(*paths):
    """Drop cached listings whose scanned tree contains any of the given paths
    (files or dirs). Must be called after any mutation (create/rename/delete/
    move/copy) inside a folder that find_images() may have walked."""
    dirs = set()
    for p in paths:
        if not p:
            continue
        try:
            q = _dir_cache_key(p)
        except Exception:
            continue
        dirs.add(q)
        parent = os.path.dirname(q)
        if parent and parent != q:
            dirs.add(parent)
    if not dirs:
        return
    with _dir_list_lock:
        for key in list(_dir_list_cache):
            for d in dirs:
                # 变更目录 d 落在被扫描树 key 内（d == key 或 d 是 key 的后代）
                if key == d or d.startswith(key + os.sep):
                    _dir_list_cache.pop(key, None)
                    break


def find_images(folder, force_refresh=False):
    """Recursively find all image files in a folder.

    Results are cached in-process (TTL + root-mtime revalidation);
    force_refresh=True bypasses the cache for this call.
    """
    key = _dir_cache_key(folder)
    now = time.time()
    try:
        root_mtime = os.stat(folder).st_mtime
    except OSError:
        root_mtime = None
    if not force_refresh and root_mtime is not None:
        with _dir_list_lock:
            ent = _dir_list_cache.get(key)
        if ent and (now - ent[0]) < _DIR_CACHE_TTL and ent[1] == root_mtime:
            return list(ent[2])
    images = _walk_images(folder)
    with _dir_list_lock:
        if len(_dir_list_cache) >= _DIR_CACHE_MAX:
            oldest = min(_dir_list_cache, key=lambda k: _dir_list_cache[k][0])
            _dir_list_cache.pop(oldest, None)
        # 保存 walk 之前取的 mtime：walk 期间发生的顶层变更会让下次调用
        # 发现 mtime 不一致而重扫（保守方向，不会返回过期列表）。
        _dir_list_cache[key] = (time.time(), root_mtime, images)
    return list(images)


def _walk_images(folder):
    """Recursively find all image files in a folder (uncached walk)."""
    images = []
    # 跳过自己的元数据/输出子目录（os.walk 的 dirs 剪枝只影响子目录，
    # 用户显式选择的根目录本身叫这些名字时其文件仍会被扫描）
    _SKIP_DIRS = {'.lenspilot', '_duplicates', '优秀照片', '_exported', '_watermarked'}
    def _onerror(err):
        send_log(f"跳过无法访问的目录: {err}", "warn")
    for root, dirs, files in os.walk(folder, onerror=_onerror):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                images.append(os.path.join(root, f))
    return sorted(images)


# ══════════════════════════════════════════════════════════════════════
# IPC Handlers
# ══════════════════════════════════════════════════════════════════════

def handle_load_model(params):
    """Load the AI scoring model. Auto-downloads model files if missing.

    backend preference: explicit param > persisted pref > None (legacy auto).
    When set, only that backend's file is loaded/downloaded (no cross-fallback).
    """
    global _model_loaded

    model_dir = params.get("model_dir") or None
    backend = params.get("backend") or _read_backend_pref().get("backend") or None
    send_log(f"开始加载模型... (dir={model_dir}, backend={backend})", "info")

    # First attempt: load whatever is present locally.
    try:
        model, device, loaded_backend, config = _do_load_model(model_dir, backend)
        _report_model_loaded(model, device, loaded_backend, config)
        return _model_result(model, device, loaded_backend, config)
    except FileNotFoundError:
        # No local model — download from server, then reload. This makes the
        # first-run flow robust regardless of how the frontend matches errors.
        send_log("本地未找到模型文件，开始从服务器下载...", "info")
        return _download_and_reload(model_dir, backend=backend)
    except Exception as e:
        # Model files exist but failed to load (e.g. onnx present but .data
        # missing → ONNX Runtime raises, not FileNotFoundError). Try a forced
        # re-download to repair a partial/incomplete download, once.
        send_log(f"模型加载异常，尝试强制重新下载修复: {e}", "warn")
        _model_loader_reset()
        try:
            return _download_and_reload(model_dir, force=True, backend=backend)
        except Exception as e2:
            send_event("model_error", {"error": str(e2)})
            send_log(f"模型修复后仍加载失败: {e2}", "error")
            raise


def _download_and_reload(model_dir, force=False, backend=None):
    """Download (the chosen backend's) model files then reload once."""
    from model_loader import download_model_files, refresh_search_paths
    try:
        refresh_search_paths()
        download_model_files(force=force, backend=backend)
    except Exception as dl_err:
        send_event("model_error", {"error": f"模型下载失败: {dl_err}"})
        send_log(f"模型下载失败: {dl_err}", "error")
        raise
    refresh_search_paths()
    try:
        model, device, loaded_backend, config = _do_load_model(model_dir, backend)
        _report_model_loaded(model, device, loaded_backend, config)
        return _model_result(model, device, loaded_backend, config)
    except Exception as e:
        send_event("model_error", {"error": str(e)})
        send_log(f"模型下载后仍加载失败: {e}", "error")
        raise


def _model_loader_reset():
    """Reset cached model state so a retry re-reads files from disk.

    Also clears _model_file_path/_model_file_size — otherwise model_version()
    would keep returning the old backend's key during the reset→reload window,
    briefly cross-talking the cache.
    """
    try:
        import model_loader as ml
        ml._model = None
        ml._backend = None
        ml._device = None
        ml._transform = None
        ml._config = None
        ml._model_file_path = None
        ml._model_file_size = 0
    except Exception:
        pass



def _do_load_model(model_dir, backend=None):
    from model_loader import load_model
    return load_model(model_dir, backend=backend)


def _report_model_loaded(model, device, backend, config):
    global _model_loaded
    model_name = config.get("model", {}).get("name", "resnet18") if config else "resnet18"
    img_size = config.get("data", {}).get("img_size", 224) if config else 224
    device_name = str(device).upper() if isinstance(device, str) else (
        "GPU" if str(device) == "cuda" else "MPS" if str(device) == "mps" else "CPU")
    _model_loaded = True
    send_event("model_loaded", {
        "model_name": f"{model_name} ({backend})",
        "device": device_name,
        "img_size": img_size,
    })
    send_log(f"模型加载完成: {model_name} ({backend}, {device_name})", "success")


def _model_result(model, device, backend, config):
    model_name = config.get("model", {}).get("name", "resnet18") if config else "resnet18"
    img_size = config.get("data", {}).get("img_size", 224) if config else 224
    device_name = str(device).upper() if isinstance(device, str) else (
        "GPU" if str(device) == "cuda" else "MPS" if str(device) == "mps" else "CPU")
    return {
        "model_name": f"{model_name} ({backend})",
        "device": device_name,
        "img_size": img_size,
    }


def handle_scan_images(params):
    """Scan a folder for images and return count/size info."""
    folder = params.get("folder", "")
    if not folder or not os.path.isdir(folder):
        raise ValueError(f"Invalid folder: {folder}")

    images = find_images(folder, force_refresh=bool(params.get("force_refresh", False)))
    total_size = sum(os.path.getsize(img) for img in images if os.path.isfile(img))

    send_event("scan_complete", {
        "count": len(images),
        "total_size": total_size,
        "folder": folder,
    })

    return {
        "count": len(images),
        "total_size": total_size,
    }


def _score_one(img_path, mv):
    """Score a single image with cache + thumbnail. Returns a result dict.

    Reuses one loaded PIL image for both scoring and thumbnail (no double
    disk read). Cache hit skips model inference entirely.
    """
    filename = os.path.basename(img_path)
    try:
        st = os.stat(img_path)
        size = st.st_size
        mtime = int(st.st_mtime)  # truncate to match INTEGER column
    except OSError:
        size, mtime = 0, 0
    is_raw = os.path.splitext(img_path)[1].lower() in RAW_EXTS

    cached = score_cache.get(img_path, mv, mtime, size)
    if cached:
        thumb_path = cached["thumb_path"]
        if thumb_path and not os.path.exists(thumb_path):
            thumb_path = ""
        # Apply user calibration on read (only for valid scores)
        cached_score = cached["score"]
        if cached_score >= 0:
            try:
                # Imported once at function level to avoid circular dependency
                from preference_engine import apply_user_calibration as _apply_calibration
                cached_score, _ = _apply_calibration(cached_score)
            except Exception as e:
                send_log(f"校准失败（使用缓存原始分）: {e}", "warn")
        return {
            "path": img_path, "filename": filename, "score": cached_score,
            "size": size, "is_raw": is_raw, "width": cached["width"],
            "height": cached["height"], "thumb_path": thumb_path,
            "manual_keep": cached["manual_keep"], "cached": True,
        }

    score = -1
    thumb_path = ""
    width = height = 0
    try:
        img = _load_image_rgb(img_path)
        width, height = img.width, img.height
        thumb_path = make_thumbnail(img, img_path)
        score = _infer(img)
    except Exception as e:
        send_log(f"评分失败 {filename}: {e}", "error")

    if score >= 0:
        score_cache.put(img_path, mv, mtime, size, score, width, height, is_raw, thumb_path)

    # Apply user calibration if available (only for valid scores)
    calibrated_score = score
    if score >= 0:
        try:
            from preference_engine import apply_user_calibration
            calibrated_score, _ = apply_user_calibration(score)
        except Exception as e:
            send_log(f"校准失败（使用原始评分）: {e}", "warn")

    return {
        "path": img_path, "filename": filename, "score": calibrated_score,
        "size": size, "is_raw": is_raw, "width": width, "height": height,
        "thumb_path": thumb_path, "manual_keep": 0, "cached": False,
    }


# ══════════════════════════════════════════════════════════════════════
# Batch inference pipeline (#107)
# 预取（线程池解码+缩略图+预处理，CPU/IO 密集）与批量推理（一次模型调用
# N 张）重叠，结果仍按输入顺序逐张产出，事件/缓存语义与逐张版完全一致。
# ══════════════════════════════════════════════════════════════════════

def _resolve_batch_size(explicit=None):
    """Inference batch size: explicit param > APS_SCORE_BATCH_SIZE env > auto
    (GPU 16 / CPU-ONNX 8)."""
    for v in (explicit, os.environ.get("APS_SCORE_BATCH_SIZE")):
        if v:
            try:
                n = int(v)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass
    try:
        backend, device = get_backend_info()
    except Exception:
        backend, device = "", ""
    dev = str(device).lower()
    if backend == "pytorch" and dev not in ("cpu", "n/a", "none", ""):
        return 16
    return 8


def _resolve_prefetch_workers():
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    return max(2, min(8, n - 2))


def _decode_and_prepare(img_path):
    """Prefetch worker: decode image + thumbnail + model preprocessing.
    CPU/IO-bound — runs in a worker thread. Never raises; per-image failures
    are reported via the dict so one bad file cannot poison the batch."""
    try:
        img = _load_image_rgb(img_path)
        width, height = img.width, img.height
        thumb_path = make_thumbnail(img, img_path)
        arr = preprocess_image(img)
        return {"ok": True, "width": width, "height": height,
                "thumb_path": thumb_path, "arr": arr}
    except Exception as e:
        return {"ok": False, "error": e, "width": 0, "height": 0, "thumb_path": ""}


def _calibrate_score(score):
    """Apply user calibration to a fresh model score (best-effort)."""
    if score < 0:
        return score
    try:
        from preference_engine import apply_user_calibration
        cal, _ = apply_user_calibration(score)
        return cal
    except Exception as e:
        send_log(f"校准失败（使用原始评分）: {e}", "warn")
        return score


def _cached_score_result(img_path, filename, size, is_raw, cached):
    """Build the per-image result dict for a score-cache hit (same shape and
    calibration behavior as the cached branch of _score_one)."""
    thumb_path = cached["thumb_path"]
    if thumb_path and not os.path.exists(thumb_path):
        thumb_path = ""
    cached_score = cached["score"]
    if cached_score >= 0:
        try:
            from preference_engine import apply_user_calibration as _apply_calibration
            cached_score, _ = _apply_calibration(cached_score)
        except Exception as e:
            send_log(f"校准失败（使用缓存原始分）: {e}", "warn")
    return {
        "path": img_path, "filename": filename, "score": cached_score,
        "size": size, "is_raw": is_raw, "width": cached["width"],
        "height": cached["height"], "thumb_path": thumb_path,
        "manual_keep": cached["manual_keep"], "cached": True,
    }


def _iter_scored_results(images, mv, cancel_event, batch_size=None):
    """Yield (index, img_path, result_dict) in input order.

    Score-cache hits skip inference entirely (same as before); misses flow
    through a ThreadPoolExecutor prefetch window (decode + thumbnail +
    preprocess in worker threads) and are scored in batches via
    model_loader.infer_batch. Result dicts are identical in shape and
    semantics to the legacy _score_one().
    """
    total = len(images)
    results = {}   # idx -> result dict
    pending = []   # (idx, path, filename, size, mtime, is_raw) needing inference

    # Pass 1: stat + cache lookup (cheap; no image decode)
    for idx, p in enumerate(images):
        filename = os.path.basename(p)
        try:
            st = os.stat(p)
            size = st.st_size
            mtime = int(st.st_mtime)  # truncate to match INTEGER column
        except OSError:
            size, mtime = 0, 0
        is_raw = os.path.splitext(p)[1].lower() in RAW_EXTS
        cached = score_cache.get(p, mv, mtime, size)
        if cached:
            results[idx] = _cached_score_result(p, filename, size, is_raw, cached)
        else:
            pending.append((idx, p, filename, size, mtime, is_raw))

    next_yield = 0

    if pending:
        bsz = _resolve_batch_size(batch_size)
        workers = _resolve_prefetch_workers()
        window = max(bsz * 2, bsz + 2)
        send_log(f"评分流水线: {len(pending)} 张待推理 (缓存命中 {total - len(pending)}), "
                 f"批大小 {bsz}, 预取线程 {workers}", "info")
        pool = ThreadPoolExecutor(max_workers=workers,
                                  thread_name_prefix="score-prefetch")
        futures = {}
        submit_pos = 0

        def _submit_more():
            nonlocal submit_pos
            while submit_pos < len(pending) and len(futures) < window:
                it = pending[submit_pos]
                futures[it[0]] = pool.submit(_decode_and_prepare, it[1])
                submit_pos += 1

        try:
            _submit_more()
            pos = 0
            while pos < len(pending):
                if cancel_event.is_set():
                    break
                batch = pending[pos:pos + bsz]
                pos += len(batch)
                decoded = []
                for it in batch:
                    fut = futures.pop(it[0], None)
                    if fut is not None:
                        d = fut.result()
                    else:
                        d = {"ok": False, "error": "prefetch missing",
                             "width": 0, "height": 0, "thumb_path": ""}
                    decoded.append((it, d))
                _submit_more()

                ok_items = [(it, d) for it, d in decoded if d.get("ok")]
                if ok_items:
                    try:
                        scores = infer_batch([d["arr"] for _, d in ok_items])
                    except Exception as e:
                        # 整批失败（如 OOM）→ 回退逐张推理，单张失败不拖垮整批
                        send_log(f"批量推理失败，回退逐张推理: {e}", "warn")
                        scores = []
                        for it, d in ok_items:
                            try:
                                scores.append(infer_batch([d["arr"]])[0])
                            except Exception as e2:
                                send_log(f"评分失败 {it[2]}: {e2}", "error")
                                scores.append(-1)
                    for (it, d), score in zip(ok_items, scores):
                        idx, p, filename, size, mtime, is_raw = it
                        if score is None or score < 0:
                            score = -1
                        if score >= 0:
                            score_cache.put(p, mv, mtime, size, score,
                                            d["width"], d["height"], is_raw,
                                            d["thumb_path"])
                        results[idx] = {
                            "path": p, "filename": filename,
                            "score": _calibrate_score(score),
                            "size": size, "is_raw": is_raw,
                            "width": d["width"], "height": d["height"],
                            "thumb_path": d["thumb_path"], "manual_keep": 0,
                            "cached": False,
                        }
                for it, d in decoded:
                    if d.get("ok"):
                        continue
                    idx, p, filename, size, mtime, is_raw = it
                    send_log(f"评分失败 {filename}: {d.get('error')}", "error")
                    results[idx] = {
                        "path": p, "filename": filename, "score": -1,
                        "size": size, "is_raw": is_raw, "width": 0, "height": 0,
                        "thumb_path": "", "manual_keep": 0, "cached": False,
                    }
                # Drain everything available, in input order.
                while next_yield < total and next_yield in results:
                    yield next_yield, images[next_yield], results.pop(next_yield)
                    next_yield += 1
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Final drain: cached-only tails (or no pending at all).
    while next_yield < total and next_yield in results:
        yield next_yield, images[next_yield], results.pop(next_yield)
        next_yield += 1


def handle_score_photos(params):
    """Score all photos in a folder. Streams per-image results + thumbnails.

    copy_on_score (default True): copy score>=threshold photos to the output
    folder during scoring (automation/legacy path). When False (interactive
    page), only score + cache + thumbnail; the renderer re-filters client-side
    and calls apply_selection to materialize the folder.

    force_refresh (default False): bypass the find_images() directory-listing
    cache for the initial scan. batch_size (optional): override the inference
    batch size (auto: 16 GPU / 8 CPU); scoring runs a prefetch+batch pipeline
    but still streams results in folder order with unchanged events.
    """
    global _model_loaded, _score_cancel

    folder = params.get("folder", "")
    threshold = float(params.get("threshold", 6.0))
    output_name = params.get("output_name", "优秀照片").strip()
    copy_on_score = bool(params.get("copy_on_score", True))
    on_progress = params.get("on_progress")  # optional callback(current, total) for pipeline UI

    if not folder or not os.path.isdir(folder):
        raise ValueError(f"Invalid folder: {folder}")

    if not _model_loaded:
        raise RuntimeError("Model not loaded. Call load_model first.")

    _score_cancel.clear()

    force_refresh = bool(params.get("force_refresh", False))
    images = find_images(folder, force_refresh=force_refresh)
    total = len(images)
    if total == 0:
        send_log("未找到图片文件", "warn")
        return {"total": 0, "selected": 0, "failed": 0}

    output_folder = os.path.join(folder, output_name)
    if copy_on_score:
        os.makedirs(output_folder, exist_ok=True)

    mv = model_version()
    send_log(f"开始筛选: 共 {total} 张图片，阈值 ≥ {threshold:.1f}", "info")
    if copy_on_score:
        send_log(f"输出文件夹: {output_folder}", "info")

    batch_size = params.get("batch_size")  # optional override; auto when absent
    selected = 0
    failed = 0
    cached_hits = 0
    scores = []
    copied_any = False

    for i, img_path, r in _iter_scored_results(images, mv, _score_cancel,
                                               batch_size=batch_size):
        if _score_cancel.is_set():
            send_log("评分已取消", "warn")
            break

        score = r["score"]
        if r["cached"]:
            cached_hits += 1
        if score < 0:
            failed += 1
        else:
            scores.append(score)
            if score >= threshold:
                selected += 1
                if copy_on_score:
                    if os.path.abspath(output_folder) == os.path.abspath(os.path.dirname(img_path)):
                        # File already lives in the output folder (e.g. a
                        # previously exported file picked up by the recursive
                        # scan) — copying it back would self-breed each run.
                        pass
                    else:
                        dest_path = os.path.join(output_folder, r["filename"])
                        if os.path.exists(dest_path):
                            name, ext = os.path.splitext(r["filename"])
                            dest_path = os.path.join(output_folder, f"{name}_{i}{ext}")
                        if _score_cancel.is_set():
                            break
                        try:
                            shutil.copy2(img_path, dest_path)
                            copied_any = True
                        except Exception as e:
                            send_log(f"复制失败: {r['filename']} - {e}", "error")

        send_event("score_progress", {
            "current": i + 1,
            "total": total,
            "selected": selected,
            "failed": failed,
            "last_score": score,
            "last_file": r["filename"],
            "image": r,
        })
        # Direct callback for pipeline runner (bypasses event system)
        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception:
                pass

    if copied_any:
        # 评分过程中向输出目录拷入了文件 → 使相关目录列表缓存失效
        invalidate_dir_cache(output_folder)

    avg_score = sum(scores) / len(scores) if scores else 0
    send_log(f"筛选完成: {selected}/{total} 张优秀照片 (缓存命中 {cached_hits})", "success")
    if scores:
        send_log(f"平均分: {avg_score:.2f} | 最高: {max(scores):.2f} | 最低: {min(scores):.2f}", "info")

    send_event("score_done", {
        "total": total,
        "selected": selected,
        "failed": failed,
        "avg_score": round(avg_score, 2),
        "output_folder": output_folder,
        "cached": cached_hits,
        "model_version": mv,
    })

    return {
        "total": total,
        "selected": selected,
        "failed": failed,
        "avg_score": round(avg_score, 2),
        "cached": cached_hits,
    }


def handle_score_files(params):
    """Score an explicit list of files (used by folder-watch incremental flow).

    与 handle_score_photos 共用预取+批量推理流水线，区别是输入是显式文件
    列表而非文件夹扫描。batch_size 参数可选覆盖批大小。
    发独立事件名 watch_score_progress / watch_score_done（带 batch_id），
    不干扰 PhotoSelect 整文件夹的 score_progress / score_done。
    """
    files = params.get("files", [])
    threshold = float(params.get("threshold", 6.0))
    output_folder = params.get("output_folder", "").strip()
    batch_id = params.get("batch_id", "")

    if not files:
        return {"total": 0, "selected": 0, "failed": 0}

    if not _model_loaded:
        raise RuntimeError("Model not loaded. Call load_model first.")

    _score_files_cancel.clear()

    if not output_folder:
        output_folder = os.path.dirname(files[0])
    os.makedirs(output_folder, exist_ok=True)

    total = len(files)
    mv = model_version()
    send_log(f"开始评分 {total} 个文件（阈值 ≥ {threshold:.1f}）", "info")

    batch_size = params.get("batch_size")  # optional override; auto when absent
    selected = 0
    failed = 0
    scores = []
    same_dir_warned = False
    copied_any = False

    for i, img_path, r in _iter_scored_results(files, mv, _score_files_cancel,
                                               batch_size=batch_size):
        if _score_files_cancel.is_set():
            send_log("评分已取消", "warn")
            break

        score = r["score"]
        filename = r["filename"]

        if score < 0:
            failed += 1
        else:
            scores.append(score)

        selected_paths = []
        if score >= threshold:
            selected += 1  # count before copy attempt (consistent with handle_score_photos)
            if os.path.abspath(output_folder) == os.path.abspath(os.path.dirname(img_path)):
                # Output resolves to the file's own folder (tethered/watch
                # scoring passes no explicit output): skip the copy — a folder
                # watcher would re-ingest the copy and self-breed indefinitely.
                if not same_dir_warned:
                    send_log("输出目录与照片所在目录相同，跳过复制（避免监听目录自我复制）", "warn")
                    same_dir_warned = True
            else:
                dest_path = os.path.join(output_folder, filename)
                if os.path.exists(dest_path):
                    name, ext = os.path.splitext(filename)
                    dest_path = os.path.join(output_folder, f"{name}_{i}{ext}")
                if _score_files_cancel.is_set():
                    break
                try:
                    shutil.copy2(img_path, dest_path)
                    selected_paths.append(dest_path)
                    copied_any = True
                except Exception as e:
                    send_log(f"复制失败: {filename} - {e}", "error")

        send_event("watch_score_progress", {
            "current": i + 1, "total": total, "selected": selected,
            "failed": failed, "last_score": score, "last_file": filename,
            "batch_id": batch_id, "selected_paths": selected_paths, "image": r,
        })

    if copied_any:
        invalidate_dir_cache(output_folder)

    avg_score = sum(scores) / len(scores) if scores else 0

    send_log(f"评分完成: {selected}/{total} 张优秀 (失败 {failed})", "success")
    send_event("watch_score_done", {
        "total": total,
        "selected": selected,
        "failed": failed,
        "avg_score": round(avg_score, 2),
        "output_folder": output_folder,
        "batch_id": batch_id,
    })

    return {"total": total, "selected": selected, "failed": failed}


def handle_cancel_scoring(params):
    """Cancel ongoing scoring operation（整文件夹评分与 score_files/watch 两条链都置位）."""
    global _score_cancel, _score_files_cancel
    _score_cancel.set()
    _score_files_cancel.set()
    return {"ok": True}


def handle_get_model_status(params):
    """Return current model loading status."""
    global _model_loaded
    return {"loaded": _model_loaded}


def handle_get_scored_results(params):
    """Return all cached scores for a folder (instant grid on open, no re-score).
    Applies user calibration to each score so the UI shows personalized values."""
    folder = params.get("folder", "")
    if not folder or not os.path.isdir(folder):
        return {"results": [], "model_version": model_version()}
    mv = model_version()
    rows = score_cache.get_folder(folder, mv)
    # Apply user calibration on read
    try:
        from preference_engine import apply_user_calibration
        for r in rows:
            raw = r.get("score", 0)
            if raw > 0:
                cal, _ = apply_user_calibration(raw)
                r["score"] = round(cal, 2)
    except Exception as e:
        send_log(f"批量校准失败: {e}", "warn")
    return {"results": rows, "model_version": mv}


def handle_get_thumbnail(params):
    """Generate/fetch a cached thumbnail for a single path (lightbox / on-demand)."""
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"thumb_path": ""}
    try:
        img = _load_image_rgb(path)
        thumb_path = make_thumbnail(img, path)
        return {"thumb_path": thumb_path}
    except Exception as e:
        send_log(f"缩略图失败 {path}: {e}", "error")
        return {"thumb_path": ""}


def handle_apply_selection(params):
    """Sync the output folder to match the current selection (manual apply).

    Copies keep_paths into the output folder (skip existing), and removes any
    file in the output folder that isn't in keep_paths. Used by the interactive
    photo-select page after live threshold re-filtering / manual overrides.

    output_base (optional): 自定义输出位置的绝对路径；为空时在照片同目录下
    创建 output_name 子文件夹。输出始终落在 <base>/<output_name> 这一受管理
    的子文件夹内，因此"移除未选中"不会误删 base 下的其他文件。
    """
    folder = params.get("folder", "")
    output_name = params.get("output_name", "优秀照片").strip() or "优秀照片"
    output_base = params.get("output_base", "").strip()
    keep_paths = params.get("keep_paths", []) or []
    if not folder:
        raise ValueError("folder required")
    if output_base:
        if not os.path.isdir(output_base):
            raise ValueError(f"输出位置不存在: {output_base}")
        base = output_base
    else:
        base = folder
    output_folder = os.path.join(base, output_name)
    os.makedirs(output_folder, exist_ok=True)

    keep_basenames = set()
    copied = 0
    for src in keep_paths:
        if not src or not os.path.exists(src):
            continue
        base = os.path.basename(src)
        dst = os.path.join(output_folder, base)
        if os.path.exists(dst):
            keep_basenames.add(base)
            continue
        try:
            shutil.copy2(src, dst)
            keep_basenames.add(base)
            copied += 1
        except Exception as e:
            send_log(f"应用选择-复制失败 {base}: {e}", "error")

    # Remove files no longer selected
    removed = 0
    try:
        for f in os.listdir(output_folder):
            fp = os.path.join(output_folder, f)
            if not os.path.isfile(fp):
                continue
            if f not in keep_basenames:
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass

    send_log(f"应用选择: 拷入 {copied}，移除 {removed} → {output_folder}", "success")
    if copied or removed:
        invalidate_dir_cache(output_folder)
    return {"copied": copied, "removed": removed, "output_folder": output_folder}


def handle_export_xmp(params):
    """Write XMP sidecar files so Lightroom/Camera Raw/Bridge can read ratings & labels.

    items: [{path, rating (0-5), label ('Green'|'Red'|'')}]
    clear_paths: [path] — 删除这些照片的 LensPilot sidecar（星标模式下落选/排除
        照片的同步清理）。仅删除本软件写入的 sidecar（内容含 LensPilot 标记），
        用户或其他软件写的 sidecar 不动。
    Sidecar 命名: photo.ARW -> photo.xmp（LR 对 RAW/JPEG 均优先读取 sidecar）。
    已存在的 sidecar 会被覆盖（以 LensPilot 最新标记为准）。
    """
    items = params.get("items", []) or []
    clear_paths = params.get("clear_paths", []) or []
    written = 0
    cleared = 0
    errors = []
    for it in items:
        path = (it.get("path") or "").strip()
        if not path or not os.path.exists(path):
            continue
        try:
            rating = int(it.get("rating", 0))
        except Exception:
            rating = 0
        rating = max(0, min(5, rating))
        label = str(it.get("label", "") or "")
        if label not in ("Green", "Red", "Yellow", "Blue", "Purple"):
            label = ""
        xmp_path = os.path.splitext(path)[0] + ".xmp"
        attrs = [f'xmp:Rating="{rating}"']
        if label:
            attrs.append(f'xmp:Label="{label}"')
        attrs.append('xmp:CreatorTool="LensPilot"')
        # ── crs 保护: 已有 sidecar 含 Lightroom 开发数据(crs:) → 不覆盖,
        #    避免覆盖破坏 LR 的修图结果(round-2 后用户再导出星标的情形) ──
        if os.path.isfile(xmp_path):
            try:
                with open(xmp_path, "r", encoding="utf-8", errors="replace") as _f:
                    _head = _f.read(2048)
                if "crs:" in _head:
                    errors.append({"path": path, "error": "skip_crs_sidecar"})
                    send_log(f"XMP 写入跳过: {os.path.basename(path)} 含 Lightroom 开发数据(crs:),不覆盖", "warn")
                    continue
            except Exception:
                pass
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="LensPilot">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            f'  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" {" ".join(attrs)}/>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
        )
        try:
            with open(xmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            written += 1
        except Exception as e:
            errors.append({"path": path, "error": str(e)})
            send_log(f"XMP 写入失败 {os.path.basename(path)}: {e}", "error")

    # 清理落选/排除照片的 LensPilot sidecar（只删本软件写的，其他的不动）
    for path in clear_paths:
        path = (path or "").strip()
        if not path:
            continue
        xmp_path = os.path.splitext(path)[0] + ".xmp"
        if not os.path.isfile(xmp_path):
            continue
        try:
            with open(xmp_path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(2048)
            if "LensPilot" not in head or "crs:" in head:
                continue  # 非本软件写入（如 LR/Bridge 生成的），或含 LR 开发数据(crs:) → 保留
            os.remove(xmp_path)
            cleared += 1
        except Exception as e:
            errors.append({"path": path, "error": str(e)})
            send_log(f"XMP 清除失败 {os.path.basename(path)}: {e}", "error")

    send_log(f"XMP 导出: 写入 {written} 个 sidecar，清除 {cleared} 个，失败 {len(errors)}",
             "warn" if errors else "success")
    return {"written": written, "cleared": cleared, "errors": errors}


def handle_set_manual_keep(params):
    """Set manual keep/reject override for a photo (persisted in cache)."""
    path = params.get("path", "")
    keep = int(params.get("keep", 0))  # -1 reject, 0 none, 1 keep
    if not path:
        raise ValueError("path required")
    score_cache.set_manual(path, model_version(), keep)
    # Register as implicit feedback for preference learning
    model_score = None
    try:
        mv = model_version()
        cached = score_cache.get_score(path, mv)
        model_score = cached["score"] if cached and cached.get("score", -1) >= 0 else None
        if model_score is not None and keep != 0:
            from preference_engine import register_implicit_feedback
            register_implicit_feedback(path, model_score, keep)
    except Exception:
        pass
    # Data flywheel: opt-in upload queue (keep=1 → label 1, reject=-1 → label 0)
    if keep != 0:
        _capture_feedback(path, 1 if keep > 0 else 0, "manual_keep", model_score=model_score)
    return {"ok": True}


def handle_get_comparison_pair(params):
    """Return two scored photos for A/B comparison (preference learning)."""
    folder = params.get("folder", "")
    if not folder or not os.path.isdir(folder):
        return {"error": "invalid_folder"}
    mv = model_version()
    rows = score_cache.get_folder(folder, mv)
    scored = [r for r in rows if r.get("score", 0) > 0]
    if len(scored) < 2:
        return {"error": "not_enough_scored", "count": len(scored),
                "hint": "请先对文件夹进行 AI 评分"}
    import random
    a, b = random.sample(scored, 2)
    return {
        # score_cache rows have no filename column — derive it from the path.
        "photo_a": {"path": a["path"], "filename": os.path.basename(a["path"]),
                     "score": a.get("score", 0), "thumb": a.get("thumb_path", "")},
        "photo_b": {"path": b["path"], "filename": os.path.basename(b["path"]),
                     "score": b.get("score", 0), "thumb": b.get("thumb_path", "")},
    }


def handle_record_comparison(params):
    """Record a pairwise comparison result for preference learning."""
    path_a = params.get("photo_a", "")
    path_b = params.get("photo_b", "")
    winner = params.get("winner", "a")  # "a", "b", or "draw"
    if not path_a or not path_b:
        return {"error": "Both photo_a and photo_b are required"}
    try:
        from preference_engine import get_profile_manager
        pm = get_profile_manager()
        profile = pm.get_active()
        elo = profile["elo_engine"]
        elo.compare(path_a, path_b, winner)
        # Feed calibration learner: map the A/B outcome to (model_score, user_score)
        # corrections — same ±2.5 mapping as manual keep/reject in
        # preference_engine.record_manual_keep — so enough A/B comparisons turn
        # calibration_ready on (pairs previously came only from keep/reject).
        if winner != "draw":
            try:
                _mv = model_version()
                cal = profile["calibration"]
                ab_pairs = ((path_a, 2.5), (path_b, -2.5)) if winner == "a" else ((path_b, 2.5), (path_a, -2.5))
                for _p, _delta in ab_pairs:
                    cached = score_cache.get_score(_p, _mv)
                    if cached and cached.get("score", -1) >= 0:
                        ms = float(cached["score"])
                        cal.add_correction(ms, max(1.0, min(10.0, ms + _delta)))
            except Exception:
                pass  # calibration signal is best-effort; don't fail the comparison
        # Feed feature-weight learner: extract deep features for the winner
        # and use the combined evidence as a positive sample for that style.
        try:
            from model_loader import extract_deep_feature, _load_image_rgb
            import score_cache as sc
            # Use the winner photo as a positive example of user preference
            winner_path = path_a if winner == "a" else path_b
            if winner != "draw" and _model_loaded:
                winner_img = _load_image_rgb(winner_path)
                if winner_img is not None:
                    features = extract_deep_feature(winner_img)
                    if features is not None and len(features) == 128:
                        fw = profile["feature_weights"]
                        fw.add_sample(features, 9.0)  # winner = high user preference
                        _capture_feedback(winner_path, 1, "comparison")
            # Loser as a lower-preference example
            loser_path = path_b if winner == "a" else path_a
            if winner != "draw" and _model_loaded:
                loser_img = _load_image_rgb(loser_path)
                if loser_img is not None:
                    features = extract_deep_feature(loser_img)
                    if features is not None and len(features) == 128:
                        fw = profile["feature_weights"]
                        fw.add_sample(features, 5.0)  # loser = moderate user preference
                        _capture_feedback(loser_path, 0, "comparison")
        except Exception:
            pass  # feature extraction is best-effort; don't fail the comparison
        pm.save()
        return {"ok": True, "comparisons": elo.comparison_count()}
    except Exception as e:
        return {"error": str(e)}


def handle_get_calibration_status(params):
    """Return calibration status for the frontend preference UI."""
    try:
        from preference_engine import get_profile_manager
        pm = get_profile_manager()
        profile = pm.get_active()
        cal = profile["calibration"]
        elo = profile["elo_engine"]
        fw = profile["feature_weights"]
        import score_cache as sc
        all_fb = sc.get_all()
        keeps = sum(1 for s in all_fb if s.get("manual_keep", 0) == 1)
        rejects = sum(1 for s in all_fb if s.get("manual_keep", 0) == -1)
        return {
            "calibration_ready": cal.is_ready(),
            "calibration_pairs": cal.pair_count(),
            "calibration_params": cal.to_dict().get("params"),
            "elo_comparisons": elo.comparison_count(),
            "elo_rated": len(elo.get_ratings()),
            "feature_samples": fw.sample_count(),
            "feedback_keeps": keeps,
            "feedback_rejects": rejects,
        }
    except Exception as e:
        return {"error": str(e)}


def handle_clear_score_cache(params):
    """Clear all cached scores (settings page)."""
    ok = score_cache.clear()
    return {"ok": ok}


def handle_get_heatmap(params):
    """Generate (or reuse) a heatmap overlay for a single image, on demand.

    Heatmaps are NOT generated during scoring (would multiply inference cost).
    The renderer requests this lazily when the user toggles the heatmap on a
    card / in the lightbox. Returns {heatmap_path} ('' if unavailable).
    """
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"heatmap_path": ""}
    if not _model_loaded:
        raise RuntimeError("Model not loaded. Call load_model first.")
    hp = make_heatmap(path)
    return {"heatmap_path": hp}


# ══════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════

# Import torch here to avoid import error if not installed
try:
    import torch
except ImportError:
    torch = None
    print("WARNING: PyTorch not installed. Photo scoring will not work.", file=sys.stderr)


def handle_list_images_in_folder(params):
    """Return list of image file info objects in a folder."""
    folder = params.get("folder", "")
    if not folder or not os.path.isdir(folder):
        raise ValueError(f"Invalid folder: {folder}")
    paths = find_images(folder, force_refresh=bool(params.get("force_refresh", False)))
    images = []
    for p in paths:
        try:
            st = os.stat(p)
            images.append({
                "path": p,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except OSError:
            images.append({"path": p, "size": 0, "mtime": 0})
    return {"images": images, "count": len(images)}


def handle_rename_file(params):
    """Rename a file."""
    import os as _os
    old = params.get("oldPath", "")
    new = params.get("newPath", "")
    if not old or not new: raise ValueError("oldPath and newPath required")
    if not _os.path.exists(old): raise FileNotFoundError(old)
    # Ensure new path stays in same directory (prevent path traversal via rename)
    if _os.path.dirname(_os.path.abspath(old)) != _os.path.dirname(_os.path.abspath(new)):
        raise ValueError("重命名不能改变文件所在的目录")
    new_dir = _os.path.dirname(new)
    if not _os.path.isdir(new_dir):
        raise FileNotFoundError(f"目标目录不存在: {new_dir}")
    _os.rename(old, new)
    invalidate_dir_cache(old, new)
    return {"ok": True, "newPath": new}


def handle_delete_file(params):
    """Delete a file."""
    import os as _os
    path = params.get("path", "")
    if not path: raise ValueError("path required")
    if not _os.path.exists(path): raise FileNotFoundError(path)
    _os.remove(path)
    invalidate_dir_cache(path)
    return {"ok": True}


def _burst_quality(path, score_weight=0.5, sharp_weight=0.3, eye_weight=0.2):
    """Keep-candidate quality for burst selection. Higher = better.

    Combines the model aesthetic score (from cache, normalized to 0-1),
    sharpness (1 - Laplacian blur), and eye-open (geometric EAR). Any signal
    that is unavailable is dropped and the remaining weights renormalize, so a
    landscape (no face → no eye_open) still ranks by score+sharpness.
    """
    score = None
    try:
        cached = score_cache.get_score(path, model_version())
        if cached and cached.get("score", -1) >= 0:
            score = float(cached["score"])
    except Exception:
        pass
    sharp = None
    try:
        sharp = 1.0 - float(compute_blur_score(path))
    except Exception:
        pass
    eye = None
    try:
        import eye_detector
        eye = eye_detector.eye_open_score(path)
    except Exception:
        pass
    parts = []
    if score is not None:
        parts.append((score_weight, max(0.0, min(1.0, score / 10.0))))
    if sharp is not None:
        parts.append((sharp_weight, max(0.0, min(1.0, sharp))))
    if eye is not None:
        parts.append((eye_weight, max(0.0, min(1.0, eye))))
    if not parts:
        return 0.0
    wsum = sum(w for w, _ in parts) or 1.0
    return sum(w * v for w, v in parts) / wsum


def handle_dedup_burst(params):
    """Burst selection: group by time+phash, KEEP THE BEST of each burst (by
    aesthetic score + sharpness), move the rest to _duplicates/.

    params: folder, time_window(s), similarity(hamming), score_weight,
            sharp_weight, dry_run (True -> preview groups without moving).
    Returns per-group details so the UI can show/override the recommendation.
    """
    import os as _os, shutil as _shutil
    # Use BLOCKING send_log (with flush) for diagnostic visibility.
    from service import send_log as _log_blocking, send_event as _send_blocking

    # Extract callbacks FIRST so _log can use them (Python scoping: any
    # variable assigned in a function is local throughout, so _log must be
    # defined before its first call).
    on_progress = params.get("on_progress")
    on_log = params.get("on_log")
    def _log(msg, level="info"):
        """Log to both service (global) and pipeline UI (if on_log provided)."""
        _log_blocking(msg, level)
        if on_log:
            try:
                on_log(msg, level)
            except Exception:
                pass

    _log("dedup v3.1: 批量预加载缩略图+缓存pHash(主线程)+回退超时保护(10s/张)")
    folder = params.get("folder", "")
    if not folder or not _os.path.isdir(folder):
        raise ValueError(f"Invalid folder: {folder}")
    time_window = int(params.get("time_window", 3))  # seconds
    similarity_threshold = int(params.get("similarity", 5))  # hamming distance
    score_weight = float(params.get("score_weight", 0.5))
    sharp_weight = float(params.get("sharp_weight", 0.3))
    eye_weight = float(params.get("eye_weight", 0.2))
    dry_run = bool(params.get("dry_run", False))

    images = find_images(folder)
    if len(images) < 2: return {"total": len(images), "duplicates": 0, "groups": 0}

    _log(f"开始连拍去重: {len(images)} 张图片, 时间窗口={time_window}秒")

    # Use pure-numpy pHash (NO imagehash, NO scipy). imagehash imports
    # scipy.fftpack at module level, and scipy import hangs/deadlocks in
    # the Electron-spawned Python process (GPU init deadlock). numpy FFT
    # gives a consistent frequency-domain hash for burst detection without
    # any scipy dependency.
    from PIL import Image as PILImage
    import numpy as _np

    def _compute_phash(pil_img, hash_size=8):
        """Pure-numpy pHash using DCT-II matrix (equivalent to scipy.fftpack.dct).
        Returns a tuple of ints (hashable, comparable via Hamming distance)."""
        img_size = hash_size * 4  # 32x32
        img = pil_img.convert("L").resize((img_size, img_size), PILImage.LANCZOS)
        pixels = _np.asarray(img, dtype=_np.float64)
        # DCT-II via matrix multiplication (no scipy needed).
        # M[k,n] = cos(pi * (2n+1) * k / (2N)); 2D DCT = M @ X @ M.T
        n = _np.arange(img_size)
        k = n.reshape(-1, 1)
        M = _np.cos(_np.pi * (2*n + 1) * k / (2*img_size))
        dctcoef = M @ pixels @ M.T
        block = dctcoef[:hash_size, :hash_size]
        avg = block.mean()
        diff = block > avg
        return tuple(int(b) for row in diff for b in row)

    def _hamming(h1, h2):
        """Hamming distance between two hash tuples."""
        return sum(a != b for a, b in zip(h1, h2))

    _log("去重: numpy pHash 引擎就绪(无需scipy)")

    # Cache model_version once (for score_cache thumbnail lookup)
    try:
        _dedup_mv = model_version()
    except Exception:
        _dedup_mv = ""
    _log(f"去重: model_version={_dedup_mv}")

    # Pre-load ALL cached thumbnail paths in ONE bulk query (avoids per-image
    # SQLite queries in the hot loop which could block on DB lock).
    # Uses a direct query (no mtime/size validation) since we just need the
    # thumbnail path - the thumbnail was generated during scoring moments ago.
    _thumb_map = {}
    if _dedup_mv:
        try:
            import sqlite3 as _sql
            from score_cache import _db_path as _scdb
            _conn = _sql.connect(_scdb, timeout=10)
            _rows = _conn.execute(
                "SELECT path, thumb_path FROM scores WHERE model_version=? AND thumb_path != ''",
                (_dedup_mv,)).fetchall()
            _conn.close()
            for _r in _rows:
                _p = _r[0]
                _tp = _r[1]
                if _p and _tp:
                    _thumb_map[_p] = _tp
            _log(f"去重: 预加载 {len(_thumb_map)} 个缓存缩略图路径")
        except Exception as e:
            _log(f"去重: 预加载缩略图失败({e})", "warn")
            # Fallback: per-file lookup
            try:
                rows = score_cache.get_folder(folder, _dedup_mv, limit=10000)
                for row in rows:
                    p = row.get("path")
                    tp = row.get("thumb_path", "")
                    if p and tp:
                        _thumb_map[p] = tp
                _log(f"去重: 回退查询 {len(_thumb_map)} 个缩略图")
            except Exception as e2:
                _log(f"去重: 回退查询也失败({e2})", "warn")

    # dup_dir 提前定义: 下方无缓存缩略图的早退路径(dry_run=False)也要引用它
    dup_dir = _os.path.join(folder, "_duplicates")

    # Safety: if no cached thumbnails at all, skip dedup to avoid HEIC/RAW
    # decode hangs in npm start. The user can re-run after scoring completes.
    if not _thumb_map:
        _log("去重: 无缓存缩略图(评分未完成或model_version不匹配), 跳过去重避免卡死", "warn")
        return {"total": len(images), "duplicates": 0, "groups": 0,
                "dry_run": dry_run, "output_folder": dup_dir if not dry_run else "",
                "group_details": [], "skipped": "no_cached_thumbnails"}

    # Per-image pHash. Uses ONLY cached JPEG thumbnails (dict lookup).
    # NO fallback to _load_image_rgb - HEIC/RAW decode hangs in npm start
    # (libheif GPU-accelerated decode can deadlock). Cache misses are skipped.
    _cache_hits = [0]
    _cache_misses = [0]

    # Diagnostic: check first image path matches _thumb_map keys
    if images:
        _first = images[0]
        _in_map = _first in _thumb_map
        _log(f"去重: 第一张路径: {_first}")
        _log(f"去重: 缓存{'命中' if _in_map else '缺失!!'}")
        if not _in_map and _thumb_map:
            _sample = list(_thumb_map.keys())[:2]
            _log(f"去重: 缓存键样本: {_sample}", "warn")

    def _phash_image(p):
        """Compute pHash from cached thumbnail. Returns hash tuple or None."""
        tp = _thumb_map.get(p)
        if not tp or not _os.path.exists(tp):
            _cache_misses[0] += 1
            return None
        try:
            # Per-image step logging (only first 3 images, for diagnosis)
            _verbose = (_cache_hits[0] + _cache_misses[0]) < 3
            if _verbose:
                _log(f"      step1: PIL.open({os.path.basename(tp)})")
            thumb_img = PILImage.open(tp)
            if _verbose:
                _log(f"      step2: opened size={thumb_img.size} mode={thumb_img.mode}")
            _cache_hits[0] += 1
            thumb_img.thumbnail((256, 256), PILImage.LANCZOS)
            if _verbose:
                _log(f"      step3: thumbnail done {thumb_img.size}")
            h = _compute_phash(thumb_img)
            if _verbose:
                _log(f"      step4: phash done (numpy FFT)")
            return h
        except Exception as e:
            _log(f"      ERROR: {e}", "warn")
            _cache_misses[0] += 1
            return None

    # Group by time proximity
    groups = []
    current_group = [images[0]]
    for i in range(1, len(images)):
        try:
            t1 = _os.path.getmtime(images[i-1])
            t2 = _os.path.getmtime(images[i])
            if abs(t2 - t1) <= time_window:
                current_group.append(images[i])
            else:
                if len(current_group) > 1: groups.append(current_group)
                current_group = [images[i]]
        except Exception:
            current_group.append(images[i])
    if len(current_group) > 1: groups.append(current_group)

    if not dry_run:
        _os.makedirs(dup_dir, exist_ok=True)
    duplicates = 0
    group_details = []

    total_groups = len(groups)
    # Count total images to process (for granular progress)
    total_images = sum(len(g) for g in groups if 2 <= len(g) <= 20)
    images_done = 0
    _log(f"去重: {total_groups} 个时间组, 待处理 {total_images} 张(组内2-20张)")
    # Emit initial progress so the bar shows immediately
    if on_progress:
        try:
            on_progress(0, max(total_images, 1))
        except Exception:
            pass

    for gi, group in enumerate(groups):
        if len(group) < 2: continue
        if len(group) > 20:  # 超大组(复制时产生的假连拍)直接跳过
            _log(f"  组{gi+1}: {len(group)}张 -> 跳过(超大组)")
            images_done += len(group)
            continue
        _log(f"  组{gi+1}/{total_groups}: {len(group)}张, 开始计算pHash...")
        # pHash using cached JPEG thumbnails only (no HEIC/RAW decode).
        # Thread-based timeout (5s/image) as safety net: if PIL or imagehash
        # hangs on a corrupted/unusual thumbnail, skip it instead of stalling.
        import threading as _threading
        hashes = []
        for pi, p in enumerate(group):
            _log(f"    [{images_done+1}/{total_images}] {_os.path.basename(p)}")
            # Run _phash_image in a thread with 5s timeout
            _box = {"result": None}
            def _do_phash(p=p):
                _box["result"] = _phash_image(p)
            _wt = _threading.Thread(target=_do_phash, daemon=True)
            _wt.start(); _wt.join(timeout=5)
            if _wt.is_alive():
                _log(f"      超时(>5s)跳过此图", "warn")
                h = None
            else:
                h = _box["result"]
            if h is not None:
                hashes.append((p, h))
            images_done += 1
            if on_progress and total_images > 0:
                try:
                    on_progress(images_done, total_images)
                except Exception:
                    pass
        if len(hashes) < 2:
            continue
        hashes.sort(key=lambda x: x[0])
        ref_hash = hashes[0][1]
        cluster = [hashes[0][0]] + [p for p, h in hashes[1:] if _hamming(h, ref_hash) < similarity_threshold]
        if len(cluster) < 2:
            continue
        # keeper 选择逻辑: dry_run 预览与 apply 共用——按缓存分排序(只读 SQLite,
        # 不做 _burst_quality 那种全图解码), 保证用户在动作门批准的 keeper 就是
        # 实际执行的 keeper。cluster 已按路径排序, 分数并列时取字母序第一张(稳定)。
        def _cached_score(p):
            try:
                cached = score_cache.get_score(p, _dedup_mv)
                if cached and cached.get("score", -1) >= 0:
                    return float(cached["score"])
            except Exception:
                pass
            return 0.0
        ranked = sorted(cluster, key=lambda p: _cached_score(p), reverse=True)
        keeper = ranked[0]
        to_move = ranked[1:]
        if dry_run:
            # Preview only: report the cluster WITHOUT moving files.
            moved = [_os.path.basename(p) for p in to_move]
            duplicates += len(moved)
            group_details.append({
                "kept": _os.path.basename(keeper),
                "kept_path": keeper,
                "moved": moved,
                # Full paths for the UI (find_images is recursive, so basenames
                # alone cannot be re-anchored to their subfolder).
                "moved_paths": list(to_move),
                "size": len(cluster),
            })
        else:
            # Actual execution: move non-keepers to _duplicates.
            for p in to_move:
                try:
                    dest = _os.path.join(dup_dir, _os.path.basename(p))
                    # In case of name collision, add a suffix
                    if _os.path.exists(dest):
                        base, ext = _os.path.splitext(_os.path.basename(p))
                        dest = _os.path.join(dup_dir, f"{base}_dup{ext}")
                    _shutil.move(p, dest)
                    duplicates += 1
                except Exception as e:
                    _log(f"移动失败 {_os.path.basename(p)}: {e}", "warn")
            group_details.append({
                "kept": _os.path.basename(keeper),
                "kept_path": keeper,
                "moved": [_os.path.basename(p) for p in to_move],
                # Full paths actually moved (UI needs them to sync its view).
                "moved_paths": list(to_move),
                "size": len(cluster),
            })

    if not dry_run and duplicates:
        # 有文件被移入 _duplicates/ → 使目录列表缓存失效
        invalidate_dir_cache(folder, dup_dir)
    verb = "预览" if dry_run else "移除"
    _log(f"连拍去重完成: {len(group_details)} 组连拍, {verb} {duplicates} 张"
         f" (缓存命中 {_cache_hits[0]} 缺失 {_cache_misses[0]})", "success")
    # Final progress: ensure 100%
    if on_progress:
        try:
            on_progress(max(total_images, 1), max(total_images, 1))
        except Exception:
            pass
    return {"total": len(images), "duplicates": duplicates, "groups": len(group_details),
            "dry_run": dry_run, "output_folder": dup_dir, "group_details": group_details}


def handle_apply_watermark(params):
    """Apply watermark (text or image logo) to selected photos (copies, not overwrites)."""
    import os as _os
    file_paths = params.get("file_paths", [])
    text = params.get("text", "© LensPilot")
    image_path = params.get("image_path", "").strip()
    position = params.get("position", "bottom-right")
    opacity = float(params.get("opacity", 0.3))
    font_size = int(params.get("font_size", 36))
    logo_scale = float(params.get("logo_scale", 0.08))  # logo size as fraction of image width

    if not file_paths:
        raise ValueError("No files specified")

    wm_dir = _os.path.join(_os.path.dirname(file_paths[0]), "_watermarked")
    _os.makedirs(wm_dir, exist_ok=True)
    processed = 0

    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("需要安装 Pillow: pip install Pillow")

    # Pre-load logo image if provided
    logo_img = None
    if image_path and _os.path.exists(image_path):
        try:
            logo_img = PILImage.open(image_path).convert('RGBA')
        except Exception as e:
            send_log(f"水印 logo 加载失败: {e}", "warn")

    # Font setup for text watermark
    if not logo_img:
        _FONT_CANDIDATES = (
            "arial.ttf", "Arial.ttf",
            "Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        )
        font = None
        for candidate in _FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
            send_log("水印字体回退为默认位图字体，字号设置可能无效", "warn")

    for fp in file_paths:
        if not _os.path.exists(fp): continue
        try:
            img = PILImage.open(fp).convert('RGBA')
            overlay = PILImage.new('RGBA', img.size, (0, 0, 0, 0))
            margin = 20

            if logo_img:
                # Image/logo watermark
                logo_w = int(img.width * logo_scale)
                logo_h = int(logo_img.height * (logo_w / logo_img.width))
                logo_resized = logo_img.resize((logo_w, logo_h), PILImage.LANCZOS)
                # Apply opacity to logo
                logo_data = logo_resized.getdata()
                new_data = []
                for px in logo_data:
                    new_data.append((px[0], px[1], px[2], int(px[3] * opacity)))
                logo_resized.putdata(list(new_data))  # must convert to list for PIL

                if position == "bottom-right":
                    x, y = img.width - logo_w - margin, img.height - logo_h - margin
                elif position == "bottom-left":
                    x, y = margin, img.height - logo_h - margin
                elif position == "top-right":
                    x, y = img.width - logo_w - margin, margin
                elif position == "top-left":
                    x, y = margin, margin
                else:
                    x, y = (img.width - logo_w) // 2, (img.height - logo_h) // 2

                overlay.paste(logo_resized, (x, y), logo_resized)
            else:
                # Text watermark
                draw = ImageDraw.Draw(overlay)
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

                if position == "bottom-right":
                    x, y = img.width - tw - margin, img.height - th - margin
                elif position == "bottom-left":
                    x, y = margin, img.height - th - margin
                elif position == "top-right":
                    x, y = img.width - tw - margin, margin
                elif position == "top-left":
                    x, y = margin, margin
                else:
                    x, y = (img.width - tw) // 2, (img.height - th) // 2

                fill = (255, 255, 255, int(255 * opacity))
                draw.text((x, y), text, font=font, fill=fill)

            result = PILImage.alpha_composite(img, overlay).convert('RGB')
            dest = _os.path.join(wm_dir, _os.path.basename(fp))
            result.save(dest, quality=95)
            processed += 1
        except Exception as e:
            send_log(f"水印失败: {_os.path.basename(fp)} — {e}", "error")

    send_log(f"水印完成: {processed}/{len(file_paths)} 张", "success")
    if processed:
        invalidate_dir_cache(wm_dir)
    return {"processed": processed, "output_dir": wm_dir}


# ══════════════════════════════════════════════════════════════════════
# Blur / Eye / Dedup handlers
# ══════════════════════════════════════════════════════════════════════

def handle_detect_blur(params):
    """Estimate image blur as a 0-1 score (0=sharp, 1=blurry).

    Pure algorithm, no GPU/model needed. Can batch-process multiple paths.
    """
    paths = params.get("paths") or [params.get("path")]
    if not paths:
        raise ValueError("path or paths required")
    results = {}
    for p in paths:
        if os.path.exists(p):
            results[p] = compute_blur_score(p)
        else:
            results[p] = -1
    if len(results) == 1:
        val = list(results.values())[0]
        return {"path": list(results.keys())[0], "blur_score": val}
    return {"results": results}


def handle_detect_eyes(params):
    """Detect faces and classify eye state (open/closed/half_open).

    Requires facenet-pytorch (MTCNN). Falls back to heuristics if no eye model.
    """
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        raise ValueError("path required")
    return detect_eyes(path)


def handle_extract_feature(params):
    """Extract a 128-d deep feature vector from an image (for dedup & similarity)."""
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        raise ValueError("path required")
    feat = extract_deep_feature(_load_image_rgb(path))
    if feat is None:
        return {"feature": None, "error": "Model not loaded or feature head not available"}
    return {"feature": feat.tolist(), "path": path}


def handle_deep_dedup(params):
    """Two-tier dedup: pHash (fast) + deep feature cosine similarity (precise).

    Accepts a list of image paths, returns duplicate groups.
    """
    paths = params.get("paths", [])
    threshold = float(params.get("similarity_threshold", 0.95))
    if len(paths) < 2:
        return {"groups": [], "total": len(paths)}

    import numpy as _np
    try:
        import imagehash
        from PIL import Image as PILImage
    except ImportError:
        raise RuntimeError("需要安装 imagehash: pip install imagehash")

    # First tier: pHash fast filtering (256px thumb for speed, _load_image_rgb for RAW/HEIC compat)
    candidates = []
    for p in paths:
        try:
            from model_loader import _load_image_rgb
            thumb = _load_image_rgb(p)
            thumb.thumbnail((256, 256), PILImage.LANCZOS)
            ph = imagehash.phash(thumb.convert('L'))
            candidates.append((p, ph))
        except Exception:
            pass

    # Second tier: deep feature similarity (if model loaded)
    model_available = False
    from model_loader import _model as _ml_model
    if _ml_model is not None:
        try:
            model_available = True
        except Exception:
            pass

    groups = []
    visited = set()
    for i, (p1, h1) in enumerate(candidates):
        if i in visited:
            continue
        group = [p1]
        for j, (p2, h2) in enumerate(candidates):
            if j <= i or j in visited:
                continue
            diff = h1 - h2
            if diff > threshold * 64:  # hamming distance too large
                continue
            # Deep feature confirmation
            if model_available:
                try:
                    f1 = extract_deep_feature(_load_image_rgb(p1))
                    f2 = extract_deep_feature(_load_image_rgb(p2))
                    if f1 is not None and f2 is not None:
                        sim = float(_np.dot(f1, f2))
                        if sim < threshold:
                            continue
                except Exception:
                    pass  # fall through: pHash alone says duplicate
            group.append(p2)
            visited.add(j)
        if len(group) > 1:
            groups.append(group)
        visited.add(i)

    return {"groups": groups, "total": len(paths), "deep_features_used": model_available}


def handle_get_thumbnails(params):
    """Generate tiny thumbnails for a list of files (batch). Returns {path: thumb_path}.
    Used by the file browser to avoid loading full-size images via local-image://."""
    paths = params.get("paths", [])
    if not paths:
        return {"thumbs": {}}
    results = {}
    for p in paths:
        try:
            if not os.path.exists(p):
                continue
            img = _load_image_rgb(p)
            if img:
                tp = make_thumbnail(img, p)
                results[p] = tp
        except Exception:
            continue
    return {"thumbs": results}


def register_handlers():
    """Register all photo scorer handlers with the IPC service."""
    register_handler("load_model", handle_load_model)
    register_handler("scan_images", handle_scan_images)
    register_handler("score_photos", handle_score_photos)
    register_handler("score_files", handle_score_files)
    register_handler("cancel_scoring", handle_cancel_scoring)
    register_handler("get_model_status", handle_get_model_status)
    register_handler("list_images_in_folder", handle_list_images_in_folder)
    register_handler("rename_file", handle_rename_file)
    register_handler("delete_file", handle_delete_file)
    register_handler("dedup_burst", handle_dedup_burst)
    register_handler("apply_watermark", handle_apply_watermark)
    register_handler("get_cache_info", handle_get_cache_info)
    register_handler("clear_raw_cache", handle_clear_raw_cache)
    register_handler("download_model", handle_download_model)
    register_handler("get_scored_results", handle_get_scored_results)
    register_handler("get_thumbnail", handle_get_thumbnail)
    register_handler("apply_selection", handle_apply_selection)
    register_handler("export_xmp", handle_export_xmp)
    register_handler("set_manual_keep", handle_set_manual_keep)
    register_handler("clear_score_cache", handle_clear_score_cache)
    register_handler("get_heatmap", handle_get_heatmap)
    register_handler("get_cuda_available", handle_get_cuda_available)
    register_handler("get_backend_pref", handle_get_backend_pref)
    register_handler("set_backend_pref", handle_set_backend_pref)
    register_handler("switch_backend", handle_switch_backend)
    # Preference / personalization
    register_handler("get_comparison_pair", handle_get_comparison_pair)
    register_handler("record_comparison", handle_record_comparison)
    register_handler("get_calibration_status", handle_get_calibration_status)
    register_handler("detect_blur", handle_detect_blur)
    register_handler("detect_eyes", handle_detect_eyes)
    register_handler("extract_feature", handle_extract_feature)
    register_handler("deep_dedup", handle_deep_dedup)
    register_handler("export_photos", handle_export_photos)
    register_handler("get_thumbnails", handle_get_thumbnails)
    send_log("photo_scorer module loaded", "info")


def handle_export_photos(params):
    """Batch export photos with format conversion, resizing, and naming template.

    params: {paths:[str], format:"jpeg"|"png"|"webp", quality:int(1-100),
             max_edge:int|None, name_template:str, output_folder:str}
    """
    from PIL import Image, ImageEnhance
    import re as _re

    paths = params.get("paths", [])
    fmt = params.get("format", "jpeg").lower()
    quality = int(params.get("quality", 85))
    max_edge = params.get("max_edge") or None
    name_tmpl = params.get("name_template", "{original}") or "{original}"
    output_folder = params.get("output_folder", "")

    if not paths:
        return {"error": "No paths provided"}
    if fmt not in ("jpeg", "png", "webp"):
        return {"error": f"Unsupported format: {fmt}"}

    if not output_folder or not os.path.isdir(os.path.dirname(output_folder) if not os.path.isdir(output_folder) else output_folder):
        # Fallback: export to source folder's _exported subfolder
        src_dir = os.path.dirname(paths[0])
        output_folder = os.path.join(src_dir, "_exported")
    os.makedirs(output_folder, exist_ok=True)

    total = len(paths)
    exported = 0
    failed = 0
    errors = []

    # PIL format mapping
    pil_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    ext_map = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
    out_ext = ext_map.get(fmt, ".jpg")
    save_kwargs = {"format": pil_fmt.get(fmt, "JPEG")}
    if fmt == "jpeg":
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    elif fmt == "webp":
        save_kwargs["quality"] = quality
    elif fmt == "png":
        save_kwargs["optimize"] = True

    for i, path in enumerate(paths):
        try:
            img = Image.open(path)
            # Convert to RGB for JPEG/WebP (removes alpha)
            if fmt in ("jpeg", "webp") and img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            # Resize if max_edge specified
            if max_edge and max_edge > 0:
                w, h = img.size
                if max(w, h) > max_edge:
                    ratio = max_edge / max(w, h)
                    img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            # Build output filename from template
            basename = os.path.splitext(os.path.basename(path))[0]
            # Extract date from EXIF or use file mtime
            date_str = ""
            try:
                exif = img.getexif()
                dt_orig = exif.get(36867) or exif.get(306)  # DateTimeOriginal or DateTime
                if dt_orig:
                    date_str = dt_orig.replace(":", "-").replace(" ", "_")[:10]
            except Exception:
                pass
            if not date_str:
                date_str = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))

            # Try to get cached score
            score_str = ""
            try:
                import score_cache as sc
                mv = model_version()
                s = sc.get_score(path, mv)
                if s is not None:
                    score_str = f"{s['score']:.0f}"
            except Exception:
                pass

            name = name_tmpl
            name = name.replace("{original}", basename)
            name = name.replace("{date}", date_str)
            name = name.replace("{rating}", score_str or "N")
            name = name.replace("{index}", f"{i + 1:03d}")
            # Remove unsafe filename chars
            name = _re.sub(r'[<>:"/\\|?*]', '_', name)
            out_path = os.path.join(output_folder, name + out_ext)

            img.save(out_path, **save_kwargs)
            exported += 1
        except Exception as e:
            failed += 1
            errors.append(f"{os.path.basename(path)}: {e}")

        # Progress every 10 photos or at the end
        if (i + 1) % 10 == 0 or i == total - 1:
            send_event("export_progress", {
                "current": i + 1,
                "total": total,
                "exported": exported,
                "failed": failed,
            })

    if exported:
        invalidate_dir_cache(output_folder)
    return {
        "exported": exported,
        "failed": failed,
        "total": total,
        "output_folder": output_folder,
        "errors": errors[:10],  # cap error details
    }


def handle_download_model(params):
    """Download AI model files from server.

    backend ('onnx'|'pytorch'|None): when set, only that backend's files are
    fetched. None downloads all (legacy).
    """
    from model_loader import download_model_files
    force = params.get('force', False)
    backend = params.get('backend') or None
    try:
        ok = download_model_files(force=force, backend=backend)
        return {'ok': ok, 'message': 'Model downloaded' if ok else 'Model already present'}
    except Exception as e:
        return {'error': str(e)}


def handle_get_cuda_available(params):
    """Report whether a CUDA GPU is available (gates the PyTorch option in UI)."""
    return {"cuda_available": _cuda_available()}


def handle_get_backend_pref(params):
    """Snapshot of backend preference + local model presence for the renderer."""
    return get_backend_pref_info()


def handle_set_backend_pref(params):
    """Persist the chosen backend WITHOUT reloading (used after the first-run
    picker, before download). Validates the value."""
    backend = params.get("backend")
    if backend not in ("onnx", "pytorch"):
        raise ValueError("backend must be 'onnx' or 'pytorch'")
    if backend == "pytorch" and not _cuda_available():
        raise RuntimeError("未检测到 GPU 加速器（NVIDIA CUDA 或 Apple MPS），无法选择 PyTorch 后端")
    _write_backend_pref(backend)
    send_log(f"已保存后端偏好: {backend}", "info")
    return {"ok": True, "backend": backend}


def handle_switch_backend(params):
    """Switch to a different backend: reset, persist pref, reload (downloading
    the target backend's files if missing). Emits model_loaded / model_download_*
    events just like handle_load_model. Returns the new model info.
    """
    global _model_loaded
    backend = params.get("backend")
    if backend not in ("onnx", "pytorch"):
        raise ValueError("backend must be 'onnx' or 'pytorch'")
    if backend == "pytorch" and not _cuda_available():
        raise RuntimeError("未检测到 GPU 加速器（NVIDIA CUDA 或 Apple MPS），无法切换到 PyTorch")

    _model_loaded = False  # guard: reject concurrent score_photos during switch
    _model_loader_reset()
    _write_backend_pref(backend)
    send_log(f"切换模型后端 → {backend}", "info")
    # force=False so an already-present target file isn't re-downloaded (秒切);
    # a missing file triggers a targeted download via model_download_* events.
    return _download_and_reload(None, force=False, backend=backend)
