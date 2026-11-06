"""
pipeline_tool_steps.py — 交付链管线步骤（0.8.5 工作流 Schema v2）

4 个可编排的交付步骤，统一操作 run.artifacts["keep_paths"]（路径列表）：
  export_keepers      导出精选到本地目录
  watermark_keepers   给精选批量加水印（输出 _watermarked/ 副本）
  upload_keepers      上传精选到云端（需已登录）
  create_cloud_album  用已上传照片创建云端相册（可设访问密码）

约定与 pipeline_runner 内置步骤一致：fn(run) -> dict，从 run.step_params 读参。
注意：pipeline_runner 的 helper 全部在函数内延迟导入（模块级 import 会循环）。
未登录/无凭证等可预期失败一律返回 error dict + _step_log，不抛异常崩管线。
产物只存计数/摘要/截断清单，防 run_state 膨胀。
"""
import os

# 分片大小：每片处理后更新 artifacts 进度计数（喂停滞门 _progress_sig）
_CHUNK = 50

# cloud_photo_ids 在 run_state 里的保存上限（建相册只吃这个列表，过小会导致相册缺员）
_MAX_CLOUD_IDS = 500

# 云端服务地址（与 agent_module.CLOUD_SERVER 一致）
_CLOUD_SERVER = "https://studio.dwphoto.top"


def _keep_paths(run):
    """从 artifacts.keep_paths 提取纯路径列表（兼容 dict/str 两种元素）。"""
    raw = run.artifacts.get("keep_paths") or []
    paths = []
    for item in raw:
        p = item.get("path", "") if isinstance(item, dict) else str(item)
        if p:
            paths.append(p)
    return paths


def _cloud_token(run):
    """读取已登录的云端凭证：pipeline 启动参数带入的 run.cloud_token 优先。"""
    token = (getattr(run, "cloud_token", "") or "").strip()
    if not token:
        token = (run.params.get("cloud_token") or "").strip()
    return token


def _deliver_source(run):
    """交付链输入源：AI 自动修图全量成功时优先用修图产物，否则回落 keep_paths。

    部分失败不逐张混用（防"一半修过一半没修"悄悄进交付）——整体回落原图。
    """
    retouched = run.artifacts.get("auto_retouched_paths") or []
    keep_n = len(_keep_paths(run))
    if retouched and len(retouched) == keep_n:
        return list(retouched)
    return _keep_paths(run)


def _export_one(path, out_dir, fmt, quality, max_size):
    """单张导出：PIL 底层调用与 photo_scorer.handle_export_photos /
    agent _t_export_image 一致（转 RGB、等比缩放 LANCZOS、jpeg optimize）。
    返回输出路径。"""
    from PIL import Image
    pil_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    ext_map = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
    img = Image.open(path)
    if fmt in ("jpeg", "webp") and img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    if max_size and max_size > 0:
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    base = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(out_dir, base + ext_map.get(fmt, ".jpg"))
    # 同名冲突加序号（不同子目录的同名文件同批导出时）
    n = 1
    while os.path.exists(out):
        out = os.path.join(out_dir, f"{base}_{n}{ext_map.get(fmt, '.jpg')}")
        n += 1
    save_kwargs = {"format": pil_fmt.get(fmt, "JPEG")}
    if fmt in ("jpeg", "webp"):
        save_kwargs["quality"] = quality
    if fmt in ("jpeg", "png"):
        save_kwargs["optimize"] = True
    img.save(out, **save_kwargs)
    return out


def export_keepers(run):
    """导出 keep 列表到 output_dir（默认 <工作文件夹>/_精选导出）。

    params: format(jpeg/png/webp) / quality(1-100) / max_size(最长边,可空) / output_dir
    产物: exported_count, export_dir
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "导出精选: keep 列表为空,跳过")
        run.artifacts["exported_count"] = 0
        return {"ok": True, "exported": 0}

    fmt = str(_step_param(run, "format", "jpeg") or "jpeg").lower()
    if fmt not in ("jpeg", "png", "webp"):
        fmt = "jpeg"
    quality = int(_step_param(run, "quality", 85) or 85)
    max_size = _step_param(run, "max_size", None)
    max_size = int(max_size) if max_size else None
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "_精选导出")
    os.makedirs(output_dir, exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "export_keepers",
        "detail": f"导出 {len(paths)} 张精选({fmt})..."})

    total = len(paths)
    exported, failed = 0, 0
    for off in range(0, total, _CHUNK):
        if run.cancel_event.is_set():
            _step_log(run, "导出精选: 已取消", "warn")
            break
        for p in paths[off:off + _CHUNK]:
            if run.cancel_event.is_set():
                break
            try:
                _export_one(p, output_dir, fmt, quality, max_size)
                exported += 1
            except Exception as e:
                failed += 1
                _step_log(run, f"导出失败 {os.path.basename(p)}: {e}", "warn")
        run.artifacts["exported_count"] = exported  # 每片更新,喂停滞门
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": min(off + _CHUNK, total), "total": total})

    run.artifacts["exported_count"] = exported
    run.artifacts["export_dir"] = output_dir
    msg = f"导出精选完成: {exported}/{total} 张 → {output_dir}"
    if failed:
        msg += f", 失败 {failed} 张"
    _step_log(run, msg)
    return {"ok": True, "exported": exported, "failed": failed, "output_dir": output_dir}


def watermark_keepers(run):
    """给 keep 列表批量加文字水印（输出到源目录 _watermarked/ 副本）。

    params: text / position(bottom-right 等) / opacity(0-1)
    产物: watermarked_count
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "水印精选: keep 列表为空,跳过")
        run.artifacts["watermarked_count"] = 0
        return {"ok": True, "watermarked": 0}

    text = str(_step_param(run, "text", "© LensPilot") or "")
    position = str(_step_param(run, "position", "bottom-right") or "bottom-right")
    opacity = float(_step_param(run, "opacity", 0.3) or 0.3)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "watermark_keepers",
        "detail": f"给 {len(paths)} 张精选加水印..."})

    import photo_scorer
    total = len(paths)
    done = 0
    for off in range(0, total, _CHUNK):
        if run.cancel_event.is_set():
            _step_log(run, "水印精选: 已取消", "warn")
            break
        chunk = paths[off:off + _CHUNK]
        # 复用 photo_scorer 的水印能力（写 _watermarked/ 副本,不动原图）
        res = photo_scorer.handle_apply_watermark({
            "file_paths": chunk, "text": text,
            "position": position, "opacity": opacity,
        })
        done += res.get("processed", 0)
        run.artifacts["watermarked_count"] = done  # 每片更新,喂停滞门
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": min(off + _CHUNK, total), "total": total})

    run.artifacts["watermarked_count"] = done
    _step_log(run, f"水印精选完成: {done}/{total} 张(输出 _watermarked/)")
    return {"ok": True, "watermarked": done}


def compress_keepers(run):
    """用 PhotoS 引擎把 keep 列表压缩到目标体积（平台交付尺寸），上传前自动改传压缩版。

    params: target_size(目标体积 MB,默认5) / format(jpeg/png/webp) / max_size(最长边,可空)
            / strip_gps(默认 true,交付脱敏) / output_dir(默认 <工作夹>/_压缩交付)
    产物: compress_count, compress_dir, deliver_paths（upload_keepers 优先上传它，失败回退原图）
    输入源: AI 自动修图全量成功时优先用修图产物（_deliver_source），否则 keep 原图。
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _deliver_source(run)
    if not paths:
        _step_log(run, "压缩交付: keep 列表为空,跳过")
        run.artifacts["compress_count"] = 0
        return {"ok": True, "compressed": 0}

    try:
        from photo_s.engine import ProcessOptions, batch_process
    except Exception as e:
        _step_log(run, f"压缩交付: PhotoS 引擎不可用({e})，跳过压缩直接上传原图", "warn")
        run.artifacts["compress_error"] = "engine_unavailable"
        return {"ok": False, "error": "engine_unavailable",
                "detail": f"PhotoS 引擎不可用: {e}"}

    try:
        target_size = float(_step_param(run, "target_size", 5) or 5)
    except (TypeError, ValueError):
        target_size = 5.0
    fmt = str(_step_param(run, "format", "jpeg") or "jpeg").lower()
    # photo_s 引擎的 output_format 大小写敏感（SUPPORTED_FORMATS 键: JPEG/PNG/WebP），这里归一化
    _FMT_CANON = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "webp": "WebP"}
    out_fmt = _FMT_CANON.get(fmt, "JPEG")
    if fmt not in _FMT_CANON:
        fmt = "jpeg"
    max_size = _step_param(run, "max_size", None)
    max_size = int(max_size) if max_size else None
    sg = _step_param(run, "strip_gps", True)
    strip_gps = sg if isinstance(sg, bool) else str(sg).lower() in ("1", "true", "yes", "on")
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "_压缩交付")
    os.makedirs(output_dir, exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "compress_keepers",
        "detail": f"压缩 {len(paths)} 张精选到 {target_size:g}MB({fmt})..."})

    options = ProcessOptions(
        output_dir=output_dir,
        output_format=out_fmt,
        target_size_bytes=int(target_size * 1024 * 1024),
        max_pixels=max_size,
        strip_gps=strip_gps,
        jobs=1,  # 单线程: 与管线自身线程模型一致，避免进度/取消竞态
    )

    total = len(paths)
    def _progress(cur, _tot, _path, _status=""):
        run.artifacts["compress_count"] = cur
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": cur, "total": total})

    try:
        result = batch_process(
            paths, options,
            progress_callback=_progress,
            cancel_checker=lambda: run.cancel_event.is_set(),
        )
    except Exception as e:
        _step_log(run, f"压缩交付: 批量压缩异常({e})", "warn")
        run.artifacts["compress_error"] = "batch_failed"
        return {"ok": False, "error": "batch_failed", "detail": str(e)}

    compressed = []
    for r in result.results:
        if r.success and r.output_path and os.path.exists(r.output_path):
            compressed.append(r.output_path)

    run.artifacts["compress_count"] = len(compressed)
    run.artifacts["compress_dir"] = output_dir
    if len(compressed) == total:
        run.artifacts["deliver_paths"] = compressed  # upload_keepers 优先上传压缩版
    else:
        # 部分失败: 不设 deliver_paths, 本次整体回退上传原图 —— 防静默漏传失败片
        run.artifacts.pop("deliver_paths", None)
        run.artifacts["compress_error"] = f"{total - len(compressed)}_failed"

    msg = f"压缩交付完成: {len(compressed)}/{total} 张 → {output_dir}"
    if result.fail_count:
        msg += f", 失败 {result.fail_count} 张(本次回退上传原图)"
    _step_log(run, msg)
    return {"ok": True, "compressed": len(compressed),
            "failed": result.fail_count, "output_dir": output_dir,
            "saved_percent": round(result.savings_percent, 1)}


def auto_retouch_keepers(run):
    """AI 自动修图：给 keep 列表逐张预测 9 字段 LR 式参数（曝光/对比/白平衡/
    自然饱和/清晰度/纹理/去雾/饱和）并经 photo_s 引擎真实管线应用，
    输出到 <工作夹>/_自动修图/ 副本。

    params: strength(0-1 调色强度,默认1) / engine(auto|suggest,默认auto——
            AI 引擎就绪走模型,否则自动降级 suggest 规则层;显式 suggest 强制规则层)
            / format(默认 jpeg) / quality / output_dir
    产物: auto_retouch_count, auto_retouch_dir, auto_retouched_paths,
          auto_retouch_engine(auto|suggest), auto_retouch_failed
    说明: 权重未就绪时本步骤绝不隐式下载（GB 级塔权重必须经设置页下载），
          一律降级 suggest 保证产物键一致；单张预测失败不中断批次（该张
          输出未修原图副本,计入 failed）。
    """
    import dataclasses
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "自动修图: keep 列表为空,跳过")
        run.artifacts["auto_retouch_count"] = 0
        return {"ok": True, "retouched": 0}

    try:
        strength = max(0.0, min(1.0, float(_step_param(run, "strength", 1.0) or 1.0)))
    except (TypeError, ValueError):
        strength = 1.0
    engine = str(_step_param(run, "engine", "auto") or "auto").strip().lower()
    if engine not in ("auto", "suggest"):
        engine = "auto"
    fmt = str(_step_param(run, "format", "jpeg") or "jpeg").lower()
    _FMT_CANON = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "webp": "WebP"}
    out_fmt = _FMT_CANON.get(fmt, "JPEG")
    quality = int(_step_param(run, "quality", 92) or 92)
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "_自动修图")
    os.makedirs(output_dir, exist_ok=True)

    # ── 引擎选择：auto 模式探测就绪度,未就绪降级 suggest（零网络,不隐式下载）──
    used_engine = engine
    if engine == "auto":
        try:
            import photo_s_plugins
            status = photo_s_plugins.ai_engine_status()
            if not status.get("ready"):
                used_engine = "suggest"
                _step_log(run, f"自动修图: AI 引擎未就绪({status.get('reason')}),"
                               f"降级 suggest 规则层（可在设置页下载 AI 模型）", "warn")
        except Exception as e:
            used_engine = "suggest"
            _step_log(run, f"自动修图: 引擎探测异常({e}),降级 suggest", "warn")

    try:
        from photo_s.engine import ProcessOptions, batch_process
    except Exception as e:
        _step_log(run, f"自动修图: PhotoS 引擎不可用({e})", "warn")
        run.artifacts["auto_retouch_error"] = "engine_unavailable"
        return {"ok": False, "error": "engine_unavailable", "detail": str(e)}

    base = ProcessOptions(
        output_dir=output_dir,
        output_format=out_fmt,
        quality=quality,
        suffix="_修图",  # 引擎默认 _compressed 语义错误;显式后缀兼防 output_dir=源目录时重名
        resume=bool(_step_param(run, "resume", False)),  # 断点续跑:输出已存在的跳过
        jobs=1,  # 与管线线程模型一致,避免进度/取消竞态
        auto_tone=(strength if used_engine == "auto" else None),
    )

    fail_counter = {"n": 0}

    def _per_file(path, opts):
        """逐图参数解析（batch_process per_file 钩子）。失败不中断:原图副本+计数。"""
        try:
            if used_engine == "auto":
                merged, _params = _resolve_auto_tone(opts, path)
                return merged
            from photo_s.suggest import suggest_file
            s = suggest_file(path, scale=strength)
            suggested = s.get("suggested") or {}
            if suggested:
                field_names = {f.name for f in dataclasses.fields(opts)}
                suggested = {k: v for k, v in suggested.items() if k in field_names}
                if suggested:
                    return dataclasses.replace(opts, **suggested)
            return opts
        except Exception as e:
            fail_counter["n"] += 1
            try:
                _step_log(run, f"自动修图: {os.path.basename(path)} 预测失败({e}),输出未修原图", "warn")
            except Exception:
                pass
            return opts

    if used_engine == "auto":
        try:
            import photo_s_plugins
            photo_s_plugins.ensure_auto_tone_plugin()  # 幂等自注册(正常由 service.py 启动时注册,此处兜底)
            from photo_s.autotone import resolve_auto_tone_options
            _resolve_auto_tone = resolve_auto_tone_options
        except Exception as e:
            _step_log(run, f"自动修图: autotone 模块不可用({e}),降级 suggest", "warn")
            used_engine = "suggest"
            base = dataclasses.replace(base, auto_tone=None)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "auto_retouch_keepers",
        "detail": f"AI 自动修图({used_engine}): 处理 {len(paths)} 张精选..."})

    total = len(paths)
    def _progress(cur, _tot, _path, _status=""):
        run.artifacts["auto_retouch_count"] = cur
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": cur, "total": total})

    try:
        result = batch_process(
            paths, base,
            progress_callback=_progress,
            cancel_checker=lambda: run.cancel_event.is_set(),
            per_file_options=_per_file,
        )
    except Exception as e:
        _step_log(run, f"自动修图: 批量处理异常({e})", "warn")
        run.artifacts["auto_retouch_error"] = "batch_failed"
        return {"ok": False, "error": "batch_failed", "detail": str(e)}

    retouched = [r.output_path for r in result.results
                 if r.success and r.output_path and os.path.exists(r.output_path)]
    run.artifacts["auto_retouch_count"] = len(retouched)
    run.artifacts["auto_retouch_dir"] = output_dir
    run.artifacts["auto_retouched_paths"] = retouched
    run.artifacts["auto_retouch_engine"] = used_engine
    failed = max(fail_counter["n"], result.fail_count)
    run.artifacts["auto_retouch_failed"] = failed

    msg = f"自动修图完成({used_engine}): {len(retouched)}/{total} 张 → {output_dir}"
    if failed:
        msg += f", 预测失败 {failed} 张(输出未修原图)"
    _step_log(run, msg)
    return {"ok": True, "retouched": len(retouched), "failed": failed,
            "engine": used_engine, "output_dir": output_dir}


def grade_keepers(run):
    """给 keep 列表批量套 LUT(.cube)风格化，输出到 <工作夹>/_风格化交付/ 副本。

    params: lut_file(.cube 路径,必填) / output_dir(默认 _风格化交付) / format(默认 jpeg) / quality
    产物: graded_count, grade_dir；缺 LUT 时返回错误(不阻塞交付)
    输入源: AI 自动修图全量成功时优先用修图产物（_deliver_source），否则 keep 原图。
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _deliver_source(run)
    if not paths:
        _step_log(run, "风格化交付: keep 列表为空,跳过")
        run.artifacts["graded_count"] = 0
        return {"ok": True, "graded": 0}

    lut_file = str(_step_param(run, "lut_file", "") or "").strip()
    if not lut_file or not os.path.exists(lut_file):
        _step_log(run, "风格化交付: 未提供有效的 LUT 文件(.cube),跳过(设置 lut_file 参数)", "warn")
        run.artifacts["grade_error"] = "missing_lut"
        return {"ok": False, "error": "missing_lut",
                "detail": "需要 lut_file 参数指向 .cube 文件"}

    try:
        from photo_s.engine import ProcessOptions, batch_process
    except Exception as e:
        _step_log(run, f"风格化交付: PhotoS 引擎不可用({e})", "warn")
        run.artifacts["grade_error"] = "engine_unavailable"
        return {"ok": False, "error": "engine_unavailable", "detail": str(e)}

    fmt = str(_step_param(run, "format", "jpeg") or "jpeg").lower()
    quality = int(_step_param(run, "quality", 92) or 92)
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "_风格化交付")
    os.makedirs(output_dir, exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "grade_keepers",
        "detail": f"给 {len(paths)} 张精选套 LUT 风格化..."})

    options = ProcessOptions(
        output_dir=output_dir,
        output_format=("JPEG" if fmt == "jpeg" else "PNG" if fmt == "png" else "WebP"),
        lut_file=lut_file,
        quality=quality,
        jobs=1,
    )

    total = len(paths)
    def _progress(cur, _tot, _path, _status=""):
        run.artifacts["graded_count"] = cur
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": cur, "total": total})

    try:
        result = batch_process(
            paths, options,
            progress_callback=_progress,
            cancel_checker=lambda: run.cancel_event.is_set(),
        )
    except Exception as e:
        _step_log(run, f"风格化交付: 批量处理异常({e})", "warn")
        run.artifacts["grade_error"] = "batch_failed"
        return {"ok": False, "error": "batch_failed", "detail": str(e)}

    graded = [r.output_path for r in result.results
              if r.success and r.output_path and os.path.exists(r.output_path)]
    run.artifacts["graded_count"] = len(graded)
    run.artifacts["grade_dir"] = output_dir

    msg = f"风格化交付完成: {len(graded)}/{total} 张 → {output_dir}"
    if result.fail_count:
        msg += f", 失败 {result.fail_count} 张"
    _step_log(run, msg)
    return {"ok": True, "graded": len(graded),
            "failed": result.fail_count, "output_dir": output_dir}


def multi_size_keepers(run):
    """把 keep 列表输出为多尺寸(如 thumb/screen/full)，输出到 <工作夹>/_多尺寸交付/。

    params: sizes("label:最长边" 逗号分隔,如 "thumb:480,screen:1920"; label 可省略约束
            表示原尺寸副本) / format(默认 jpeg) / output_dir(默认 _多尺寸交付)
    产物: sizes_count, sizes_dir
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "多尺寸交付: keep 列表为空,跳过")
        run.artifacts["sizes_count"] = 0
        return {"ok": True, "sized": 0}

    sizes_spec = str(_step_param(run, "sizes", "thumb:480,screen:1920") or "thumb:480,screen:1920")
    output_sizes = []
    for part in sizes_spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            label, _, cap = part.partition(":")
            label = label.strip() or "size"
            try:
                mw = int(cap.strip()) if cap.strip() else None
            except ValueError:
                mw = None
        else:
            label = part.strip() or "size"
            mw = None
        output_sizes.append((label, mw, None))
    if not output_sizes:
        _step_log(run, "多尺寸交付: sizes 参数无效,跳过", "warn")
        run.artifacts["sizes_count"] = 0
        return {"ok": True, "sized": 0}

    try:
        from photo_s.engine import ProcessOptions, batch_process
    except Exception as e:
        _step_log(run, f"多尺寸交付: PhotoS 引擎不可用({e})", "warn")
        return {"ok": False, "error": "engine_unavailable", "detail": str(e)}

    fmt = str(_step_param(run, "format", "jpeg") or "jpeg").lower()
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "_多尺寸交付")
    os.makedirs(output_dir, exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "multi_size_keepers",
        "detail": f"输出 {len(paths)} 张精选为 {len(output_sizes)+1} 个尺寸..."})

    options = ProcessOptions(
        output_dir=output_dir,
        output_format=("JPEG" if fmt == "jpeg" else "PNG" if fmt == "png" else "WebP"),
        output_sizes=output_sizes,
        suffix="_size",
        jobs=1,
    )

    total = len(paths)
    def _progress(cur, _tot, _path, _status=""):
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": cur, "total": total})

    try:
        result = batch_process(
            paths, options,
            progress_callback=_progress,
            cancel_checker=lambda: run.cancel_event.is_set(),
        )
    except Exception as e:
        _step_log(run, f"多尺寸交付: 批量处理异常({e})", "warn")
        return {"ok": False, "error": "batch_failed", "detail": str(e)}

    ok_count = sum(1 for r in result.results if r.success)
    # 统计实际产出文件(每张 = 主尺寸 + 各 sizes)
    produced = 0
    if ok_count:
        produced = ok_count * (len(output_sizes) + 1)
    run.artifacts["sizes_count"] = produced
    run.artifacts["sizes_dir"] = output_dir
    _step_log(run, f"多尺寸交付完成: {ok_count}/{total} 张 → {output_dir}(约 {produced} 个文件)")
    return {"ok": True, "sized": ok_count, "files": produced,
            "output_dir": output_dir, "sizes": len(output_sizes)}


def build_customer_preview(run):
    """生成客户预览: HTML gallery(逐张缩略+原图链接)或联系表(网格拼图)。

    params: mode(gallery/contact_sheet,默认 gallery) / title(默认"客户预览") /
            output_dir(默认 <工作夹>/客户预览) / cols(联系表列数,默认 4)
    产物: preview_path, preview_mode
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "客户预览: keep 列表为空,跳过")
        run.artifacts["preview_path"] = ""
        return {"ok": True, "previewed": 0}

    mode = str(_step_param(run, "mode", "gallery") or "gallery").lower()
    title = str(_step_param(run, "title", "客户预览") or "客户预览")
    output_dir = (_step_param(run, "output_dir", "") or "").strip()
    if not output_dir:
        output_dir = os.path.join(run.work_folder, "客户预览")
    os.makedirs(output_dir, exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "build_customer_preview",
        "detail": f"生成客户预览({mode})..."})

    try:
        if mode == "contact_sheet":
            from photo_s.contact import build_contact_sheet
            cols = int(_step_param(run, "cols", 4) or 4)
            out_path = os.path.join(output_dir, "contact_sheet.png")
            build_contact_sheet(paths, out_path, cols=cols)
            preview = out_path
        else:
            from photo_s.gallery import build_gallery
            res = build_gallery(paths, output_dir, title=title)
            preview = res.get("output", os.path.join(output_dir, "index.html"))
        run.artifacts["preview_path"] = preview
        run.artifacts["preview_mode"] = mode
        _step_log(run, f"客户预览已生成({mode}): {preview}")
        return {"ok": True, "preview_path": preview, "mode": mode}
    except Exception as e:
        _step_log(run, f"客户预览生成失败: {e}", "warn")
        run.artifacts["preview_error"] = str(e)
        return {"ok": False, "error": "preview_failed", "detail": str(e)}


def delivery_report(run):
    """生成交付清单 CSV(每张交付文件的文件名/路径/体积 + 汇总)。

    params: report_path(默认 <工作夹>/交付报告.csv) / source(deliver_paths 或 keep_paths)
    产物: report_path, report_count
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}

    source = str(_step_param(run, "source", "auto") or "auto").lower()
    if source == "deliver_paths":
        files = run.artifacts.get("deliver_paths") or []
    elif source == "keep_paths":
        files = _keep_paths(run)
    else:  # auto: 优先交付物,否则原 keep
        files = run.artifacts.get("deliver_paths") or _keep_paths(run)
    if not files:
        _step_log(run, "交付报告: 无交付文件,跳过")
        run.artifacts["report_count"] = 0
        return {"ok": True, "reported": 0}

    report_path = (_step_param(run, "report_path", "") or "").strip()
    if not report_path:
        report_path = os.path.join(run.work_folder, "交付报告.csv")
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "delivery_report",
        "detail": f"生成交付清单 {os.path.basename(report_path)}..."})

    import csv
    rows = []
    total_size = 0
    for i, p in enumerate(files, 1):
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        total_size += size
        rows.append([i, os.path.basename(p), p, size,
                     f"{size/1024/1024:.2f} MB"])
    try:
        with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["#", "文件名", "路径", "大小(字节)", "大小(MB)"])
            writer.writerows(rows)
            writer.writerow([])
            writer.writerow(["合计", len(rows), "", total_size,
                             f"{total_size/1024/1024:.2f} MB"])
    except Exception as e:
        _step_log(run, f"交付报告写入失败: {e}", "warn")
        run.artifacts["report_error"] = str(e)
        return {"ok": False, "error": "report_failed", "detail": str(e)}

    run.artifacts["report_path"] = report_path
    run.artifacts["report_count"] = len(rows)
    _step_log(run, f"交付清单已生成: {report_path}({len(rows)} 个文件)")
    return {"ok": True, "report_path": report_path, "reported": len(rows),
            "total_size_bytes": total_size}


def upload_keepers(run):
    """上传 keep 列表到云端（需已登录）。若本 run 有 deliver_paths（压缩交付产物），
    优先上传压缩版，否则上传原 keep 文件。

    params: category(默认 pending)
    产物: uploaded_count, cloud_photo_ids(最多存 _MAX_CLOUD_IDS=500 个), upload_error(失败时)
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    deliver = run.artifacts.get("deliver_paths") or []
    paths = deliver if deliver else _deliver_source(run)
    if not paths:
        _step_log(run, "上传精选: keep 列表为空,跳过")
        run.artifacts["uploaded_count"] = 0
        run.artifacts["cloud_photo_ids"] = []
        return {"ok": True, "uploaded": 0}

    token = _cloud_token(run)
    if not token:
        _step_log(run, "上传精选: 未登录云端,跳过上传(请在软件中登录后重跑)", "warn")
        run.artifacts["upload_error"] = "not_logged_in"
        return {"ok": False, "error": "not_logged_in",
                "detail": "未登录云端，请在软件中登录后再上传"}
    try:
        from cloud_uploader import upload_photo_auto
    except Exception as e:
        _step_log(run, f"上传精选: 上传模块不可用({e})", "warn")
        run.artifacts["upload_error"] = "uploader_unavailable"
        return {"ok": False, "error": "uploader_unavailable", "detail": str(e)}

    category = str(_step_param(run, "category", "pending") or "pending")
    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "upload_keepers",
        "detail": f"上传 {len(paths)} 张精选到云端..."})

    total = len(paths)
    uploaded, failed = 0, 0
    photo_ids = []
    for off in range(0, total, _CHUNK):
        if run.cancel_event.is_set():
            _step_log(run, "上传精选: 已取消", "warn")
            break
        for p in paths[off:off + _CHUNK]:
            if run.cancel_event.is_set():
                break
            try:
                r = upload_photo_auto(_CLOUD_SERVER, token, p, category=category)
                pid = (r or {}).get("id")
                if pid:
                    photo_ids.append(pid)
                uploaded += 1
            except Exception as e:
                failed += 1
                _step_log(run, f"上传失败 {os.path.basename(p)}: {e}", "warn")
        run.artifacts["uploaded_count"] = uploaded            # 每片更新,喂停滞门
        run.artifacts["cloud_photo_ids"] = photo_ids[:_MAX_CLOUD_IDS]  # 上限防 run_state 膨胀
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": min(off + _CHUNK, total), "total": total})

    run.artifacts["uploaded_count"] = uploaded
    run.artifacts["cloud_photo_ids"] = photo_ids[:_MAX_CLOUD_IDS]
    if failed:
        run.artifacts["upload_error"] = f"{failed}_failed"
    msg = f"上传精选完成: {uploaded}/{total} 张"
    if failed:
        msg += f", 失败 {failed} 张"
    _step_log(run, msg)
    return {"ok": True, "uploaded": uploaded, "failed": failed,
            "photo_ids": len(photo_ids)}


def create_cloud_album(run):
    """用已上传的云端照片创建相册（依赖 upload_keepers 的产物）。

    params: title(默认"精选相册") / password(可选访问密码)
    产物: album_code, album_url；失败时 album_error
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}

    photo_ids = run.artifacts.get("cloud_photo_ids") or []
    if not photo_ids:
        _step_log(run, "云端相册: 无已上传照片(需先执行 upload_keepers 步骤)", "warn")
        run.artifacts["album_error"] = "no_uploaded_photos"
        return {"ok": False, "error": "no_uploaded_photos",
                "detail": "请先执行 upload_keepers 步骤上传照片"}

    token = _cloud_token(run)
    if not token:
        _step_log(run, "云端相册: 未登录云端,跳过建相册", "warn")
        run.artifacts["album_error"] = "not_logged_in"
        return {"ok": False, "error": "not_logged_in",
                "detail": "未登录云端，请在软件中登录后再建相册"}
    try:
        from agent_module import _cloud_api  # 与 agent 建相册同一底层 API 调用
    except Exception as e:
        _step_log(run, f"云端相册: agent 模块不可用({e})", "warn")
        run.artifacts["album_error"] = "agent_unavailable"
        return {"ok": False, "error": "agent_unavailable", "detail": str(e)}

    title = str(_step_param(run, "title", "") or "").strip() or "精选相册"
    password = str(_step_param(run, "password", "") or "").strip()
    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "create_cloud_album",
        "detail": f"创建云端相册「{title}」({len(photo_ids)} 张)..."})

    body = {"title": title, "photoIds": photo_ids}
    if password:
        body["password"] = password
    status, data = _cloud_api(token, "POST", "/api/albums", body)
    if status in (200, 201):
        album = data.get("album", data)
        code = album.get("accessCode", "")
        url = f"{_CLOUD_SERVER}/album/{code}" if code else ""
        run.artifacts["album_code"] = code
        run.artifacts["album_url"] = url
        _step_log(run, f"云端相册已建: 「{title}」 {url or code}")
        return {"ok": True, "album_code": code, "album_url": url}

    run.artifacts["album_error"] = f"http_{status}"
    _step_log(run, f"云端相册创建失败(http {status}): {data}", "warn")
    return {"ok": False, "error": f"http_{status}", "detail": data}


def tag_keepers(run):
    """批量打标归档：给 keep 列表写 EXIF rating/keywords/title（就地修改原文件）。

    params: keywords(逗号分隔,可空) / rating(0-5,可空) / title(可空)
    产物: tagged_count
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "批量打标: keep 列表为空,跳过")
        run.artifacts["tagged_count"] = 0
        return {"ok": True, "tagged": 0}

    try:
        from photo_s.engine import apply_exif_tags
    except Exception as e:
        _step_log(run, f"批量打标: PhotoS 引擎不可用({e})", "warn")
        run.artifacts["tag_error"] = "engine_unavailable"
        return {"ok": False, "error": "engine_unavailable", "detail": str(e)}

    tags = {}
    kw = str(_step_param(run, "keywords", "") or "").strip()
    if kw:
        tags["keywords"] = kw
    rating = _step_param(run, "rating", None)
    if rating is not None and rating != "":
        try:
            rv = int(rating)
            if 0 <= rv <= 5:
                tags["rating"] = rv
        except (TypeError, ValueError):
            pass
    title = str(_step_param(run, "title", "") or "").strip()
    if title:
        tags["title"] = title
    if not tags:
        _step_log(run, "批量打标: 未提供 keywords/rating/title,跳过", "warn")
        run.artifacts["tagged_count"] = 0
        return {"ok": True, "tagged": 0, "note": "no_tags"}

    _emit_pipeline(run, "step", {
        "phase": "deliver", "step": "tag_keepers",
        "detail": f"给 {len(paths)} 张打标: {tags}"})

    total = len(paths)
    tagged, failed = 0, 0
    for off in range(0, total, _CHUNK):
        if run.cancel_event.is_set():
            _step_log(run, "批量打标: 已取消", "warn")
            break
        for p in paths[off:off + _CHUNK]:
            if run.cancel_event.is_set():
                break
            try:
                msg = apply_exif_tags(p, tags)
                # photo_s 缺 piexif 时返回提示消息而非抛异常 —— 视为失败
                if msg and "not installed" in msg.lower():
                    failed += 1
                    _step_log(run, f"打标失败 {os.path.basename(p)}: {msg}", "warn")
                else:
                    tagged += 1
            except Exception as e:
                failed += 1
                _step_log(run, f"打标失败 {os.path.basename(p)}: {e}", "warn")
        run.artifacts["tagged_count"] = tagged
        _emit_pipeline(run, "progress", {
            "phase": "deliver", "current": min(off + _CHUNK, total), "total": total})

    run.artifacts["tagged_count"] = tagged
    msg = f"批量打标完成: {tagged}/{total} 张"
    if failed:
        msg += f", 失败 {failed} 张"
    _step_log(run, msg)
    return {"ok": True, "tagged": tagged, "failed": failed}


# ═══════════════════════════════════════════════════════════════════════
# Lightroom 两轮筛选（round-2）步骤 —— 粗筛 → LR 修图 → 终选 → 交付
# ═══════════════════════════════════════════════════════════════════════

def _noop_gate_step(run, label):
    """无操作步骤：声明式 gate 承载在模板 step.gate 上（wait_retouch/review_final 共用）。"""
    from pipeline_runner import _step_log
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    _step_log(run, label)
    return {"ok": True}


def _score_to_rating(score, stars):
    """评分(0-10) → LR 星级(0-5)，按 stars=[t5,t4,t3,t2,t1] 分数段（同 photo-select 前端逻辑）。"""
    if score is None or score < 0:
        return 0
    for star in (5, 4, 3, 2, 1):
        if score >= stars[5 - star]:
            return star
    return 0


def export_working_set(run):
    """把粗筛 keeper 的评分映射为 LR 星级写入 XMP sidecar，供 Lightroom 修图。

    params: star5/star4/star3/star2/star1（评分→星级分数段，默认 8.5/7.5/6.5/5.5/4.5）
    产物: working_set_count, working_set_paths(≤500)
    说明: 含 crs 保护的 sidecar（已有 LR 开发数据的）不会被覆盖。
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    paths = _keep_paths(run)
    if not paths:
        _step_log(run, "导出工作集: 粗筛 keeper 为空,跳过")
        run.artifacts["working_set_count"] = 0
        return {"ok": True, "working_set": 0}

    stars = []
    for s in (5, 4, 3, 2, 1):
        v = _step_param(run, f"star{s}", None)
        try:
            v = float(v) if v else 0
        except (TypeError, ValueError):
            v = 0
        stars.append(max(1.0, min(10.0, v)) if v else {5: 8.5, 4: 7.5, 3: 6.5, 2: 5.5, 1: 4.5}[s])

    import photo_scorer
    import score_cache
    from model_loader import model_version
    mv = model_version()

    _emit_pipeline(run, "step", {
        "phase": "handoff", "step": "export_working_set",
        "detail": f"为 {len(paths)} 张粗筛 keeper 写 LR 星级..."})

    total = len(paths)
    items, done = [], 0
    for off in range(0, total, _CHUNK):
        if run.cancel_event.is_set():
            _step_log(run, "导出工作集: 已取消", "warn")
            break
        for p in paths[off:off + _CHUNK]:
            if run.cancel_event.is_set():
                break
            try:
                row = score_cache.get_score(p, mv)
                rating = _score_to_rating((row or {}).get("score"), stars)
            except Exception:
                rating = 0
            if rating >= 1:
                items.append({"path": p, "rating": rating})
                done += 1
        run.artifacts["working_set_count"] = done  # 每片更新,喂停滞门
        _emit_pipeline(run, "progress", {
            "phase": "handoff", "current": min(off + _CHUNK, total), "total": total})

    written = 0
    if items:
        res = photo_scorer.handle_export_xmp({"items": items})
        written = res.get("written", 0)
        if res.get("errors"):
            _step_log(run, f"导出工作集: {len(res['errors'])} 个 sidecar 跳过/失败(含 crs 保护的)", "warn")

    run.artifacts["working_set_count"] = written
    run.artifacts["working_set_paths"] = [i["path"] for i in items][:_MAX_CLOUD_IDS]
    _step_log(run, f"导出工作集完成: 写入 {written}/{done} 个 LR 星级(供 Lightroom 修图)")
    return {"ok": True, "working_set": written}


def wait_retouch(run):
    """Lightroom 修图交接：挂起管线等人完成修图（声明式 gate 在模板上）。"""
    return _noop_gate_step(run, "等待 Lightroom 修图: 请在 LR 中完成修图并标注星级(★≥3 视为交付)，完成后在界面点「继续」")


def import_retouched(run):
    """导入修图结果：重评变化文件 + 扫描 XMP 识别已修图照片。

    params: re_score(默认 True，变化文件重评；未变文件走缓存，速度不受影响)
    产物: retouched_count, retouched_paths(≤500, {path,thumb}), round2_done=True
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    folder = run.work_folder

    # 1) 重评变化文件（未变 = 缓存命中，不重推理；模型未加载则退 scan-only）
    if _step_param(run, "re_score", True):
        _step_log(run, "导入修图结果: 重评变化文件(未变文件走缓存)...")
        try:
            import photo_scorer
            photo_scorer.handle_score_photos({
                "folder": folder, "output_name": "", "copy_on_score": False,
                "on_progress": lambda cur, tot: _emit_pipeline(
                    run, "progress", {"phase": "final", "current": cur, "total": tot}),
            })
        except Exception as e:
            _step_log(run, f"重评跳过({e})，仅扫描 XMP 识别", "warn")

    # 2) 扫描识别已修图照片
    import photo_retouch
    scan = photo_retouch.scan_retouched_core(folder)
    retouched = scan["retouched"]
    run.artifacts["retouched_count"] = scan["retouched_count"]
    run.artifacts["retouched_paths"] = [
        {"path": r["path"], "thumb": r["thumb_path"]} for r in retouched
    ][:_MAX_CLOUD_IDS]
    run.artifacts["round2_done"] = True
    run.artifacts["round2_stats"] = {
        "modified": scan["modified_count"], "developed": scan["developed_count"],
    }
    msg = (f"导入修图结果: 已修图 {scan['retouched_count']} 张"
           f"(文件变更 {scan['modified_count']} / LR 开发 {scan['developed_count']})")
    _step_log(run, msg)
    return {"ok": True, "retouched": scan["retouched_count"]}


def final_select(run):
    """二次终选：以 LR 星级（LR 修过的照片）与二次阈值汇总终选 keeper，
    覆盖 artifacts.keep_paths 供交付链使用。只读 XMP，不删任何 sidecar。

    params: threshold(二次阈值, 默认沿用粗筛 threshold_used) /
            lr_min_rating(LR 星级达标线, 默认 3, LR 0 星视为主动排除) /
            deliver_same_stem(默认 False, RAW 优先于同 stem 的 jpg 兄弟)
    产物: keep/keep_paths/keep_avg_score/reject_paths/summary/final_summary
    """
    from pipeline_runner import _step_param, _step_log, _emit_pipeline
    if run.cancel_event.is_set():
        return {"ok": False, "canceled": True}
    folder = run.work_folder
    import photo_retouch
    import score_cache
    from model_loader import model_version

    t2 = float(_step_param(run, "threshold", None) or run.artifacts.get("threshold_used", 6.0) or 6.0)
    lr_min = int(_step_param(run, "lr_min_rating", 3) or 3)
    deliver_same_stem = bool(_step_param(run, "deliver_same_stem", False))

    _emit_pipeline(run, "step", {
        "phase": "final", "step": "final_select",
        "detail": f"二次终选(阈值 {t2}, LR 星级 ≥ {lr_min})..."})

    # 候选集 = 粗筛 keeper ∪ 已修图照片（LR 反馈只作用于修过的）
    round1 = set(_keep_paths(run))
    scan = photo_retouch.scan_retouched_core(folder)
    retouched_by_path = {r["path"]: r for r in scan["retouched"]}
    candidates = round1 | set(retouched_by_path.keys())

    mv = model_version()
    keep, reject = [], []
    for p in sorted(candidates):
        row = score_cache.get_score(p, mv) or {}
        manual = row.get("manual_keep", 0) or 0
        score = row.get("score")
        r2 = retouched_by_path.get(p, {})
        if manual == -1:
            reject.append({"path": p, "score": score if score is not None else 0, "thumb": row.get("thumb_path", "")})
            continue
        if manual == 1:
            keep.append({"path": p, "score": score if score is not None else 0, "thumb": row.get("thumb_path", "")})
            continue
        # LR 修过的照片以 LR 星级为准（0 星 = LR 主动排除）
        if r2.get("developed"):
            if (r2.get("xmp_rating") or 0) >= lr_min:
                keep.append({"path": p, "score": score if score is not None else 0, "thumb": row.get("thumb_path", "")})
            else:
                reject.append({"path": p, "score": score if score is not None else 0, "thumb": row.get("thumb_path", "")})
            continue
        # 未修过的按二次阈值
        if score is not None and score >= t2:
            keep.append({"path": p, "score": score, "thumb": row.get("thumb_path", "")})
        elif score is None and p in round1:
            keep.append({"path": p, "score": 0, "thumb": row.get("thumb_path", "")})  # 保留粗筛 keeper 兜底
        else:
            reject.append({"path": p, "score": score if score is not None else 0, "thumb": row.get("thumb_path", "")})

    # stem 去重：LR 导出的 jpg 兄弟不重复交付
    keep_paths = [k["path"] for k in keep]
    keep_paths = photo_retouch.stem_dedup(keep_paths, deliver_same_stem=deliver_same_stem)
    keep_set = set(keep_paths)
    keep = [k for k in keep if k["path"] in keep_set]

    run.artifacts["keep"] = len(keep)
    run.artifacts["keep_paths"] = keep[:_MAX_CLOUD_IDS]
    run.artifacts["keep_avg_score"] = (
        round(sum(k["score"] for k in keep) / len(keep), 1) if keep else 0)
    run.artifacts["reject_paths"] = reject[:_MAX_CLOUD_IDS]
    run.artifacts["summary"] = {
        "round1_keep": len(round1),
        "retouched": scan["retouched_count"],
        "kept": len(keep),
        "rejected": len(reject),
        "lr_min_rating": lr_min,
        "threshold": t2,
    }
    run.artifacts["final_summary"] = dict(run.artifacts["summary"])
    _step_log(run, f"二次终选完成: 保留 {len(keep)} 张 / 排除 {len(reject)} 张 "
                   f"(粗筛 {len(round1)} · LR 修图 {scan['retouched_count']})")
    return {"ok": True, "keep": len(keep)}


def review_final(run):
    """终选确认：交付前挂起人工确认（声明式 gate 在模板上）。"""
    return _noop_gate_step(run, "等待终选确认: 请确认终选 keeper 网格后放行交付链")
