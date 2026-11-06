"""
shooting_coach.py - Tethered-shooting session + Layer1 tools + slow coach (0.8.6 M1).

Orchestrates the "shooting companion":
  - tether_start/stop/status: spin up a CameraRouter + folder_watcher (aggressive
    polling) and subscribe to the fast-tier score events.
  - _on_fast_event: accumulates each captured shot's score and triggers the slow
    coach on score-drop / every-N-shots.
  - coach_advise: the slow tier. Gathers live settings (or EXIF fallback),
    runs Layer1 deterministic rule tools, retrieves Layer2 RAG references,
    pulls a Layer3 genre playbook, then asks a multimodal LLM for advice.
    Falls back to a rule-synthesized summary when no LLM / no multimodal.
  - 9 Layer1 tools are registered into agent_module.TOOL_REGISTRY so the
    conversation agent (and the coach) can call them via function-calling.

See TETHERED_SHOOTING_TECH_SPEC.md §3, IMPLEMENTATION_PLAN_M1.md Step 3-4.
"""
import os
import math
import time
import threading

from service import send_event, send_log, register_handler, subscribe_events
import camera_backend

# ── Session state (M1: single instance) ──
_session = None      # {id, folder, genre, lighting, goal, backend, shots, start_ts, prev_score, unsub}
_router = None       # active CameraRouter
_liveview_stop = threading.Event()
_liveview_thread = None


# ══════════════════════════════════════════════════════════════════════
# Layer 1 - pure domain helpers (also wrapped as agent tools below)
# ══════════════════════════════════════════════════════════════════════

def _parse_aperture(v):
    """Parse an f-number: 'f/2.8', 'F2.8', '2.8' -> 2.8. None on failure."""
    if v is None:
        return None
    s = str(v).strip().lower().replace("f/", "").replace("f", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _parse_shutter(v):
    """Parse shutter to seconds (float): '1/125' -> 0.008, '0.5' -> 0.5. None on failure."""
    if v is None:
        return None
    s = str(v).strip().lower().replace("s", "").strip()
    if "/" in s:
        try:
            a, b = s.split("/")
            return float(a) / float(b)
        except Exception:
            return None
    try:
        return float(s)
    except Exception:
        return None


def _parse_iso(v):
    if v is None:
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def exposure_triangle_check(aperture, shutter, iso):
    """EV + basic warnings. aperture=f-number, shutter=seconds or '1/125', iso=int."""
    N = _parse_aperture(aperture)
    t = _parse_shutter(shutter)
    iso_v = _parse_iso(iso)
    out = {"aperture": N, "shutter_s": t, "iso": iso_v, "ev": None, "warnings": []}
    if N and t and t > 0:
        out["ev"] = round(math.log2((N * N) / t), 2)
    if iso_v and iso_v > 6400:
        out["warnings"].append(f"ISO {iso_v} 偏高，暗光下噪点可能明显")
    if N and N > 11:
        out["warnings"].append(f"光圈 f/{N} 较小，可能衍射软化（见 diffraction_check）")
    return out


def dof_calculator(focal, aperture, distance, sensor="full-frame"):
    """Hyperfocal + DOF near/far. focal=mm, aperture=f-number, distance=meters."""
    try:
        f = float(focal)
        N = _parse_aperture(aperture)
        s_m = float(distance)
    except Exception:
        return {"error": "missing/invalid focal/aperture/distance"}
    if not (f and N and s_m):
        return {"error": "missing focal/aperture/distance"}
    coc = {"full-frame": 0.03, "aps-c": 0.02, "m43": 0.015, "1-inch": 0.006}.get(sensor, 0.03)
    s_mm = s_m * 1000.0
    H = (f * f) / (N * coc) + f           # hyperfocal (mm)
    near = s_mm * (H - f) / (H + (s_mm - f))
    far = s_mm * (H - f) / (H - (s_mm - f)) if H > (s_mm - f) else None
    return {
        "hyperfocal_m": round(H / 1000, 2),
        "near_m": round(near / 1000, 2),
        "far_m": (round(far / 1000, 2) if far else None),
        "dof_m": (round((far - near) / 1000, 2) if far else None),
        "coc_mm": coc,
    }


def flash_sync_check(shutter, has_flash=False, sync_speed=1 / 200):
    """Flash sync-speed violation. shutter=seconds or '1/125'."""
    t = _parse_shutter(shutter)
    if t is None:
        return {"error": "missing shutter"}
    violated = bool(has_flash) and t < sync_speed
    warn = None
    if violated:
        warn = f"快门 ~1/{int(round(1 / t))} 快于闪光同步速度 ~1/{int(round(1 / sync_speed))}，会有黑边"
    return {"shutter_s": t, "sync_speed_s": round(sync_speed, 6),
            "has_flash": bool(has_flash), "violated": violated, "warning": warn}


def motion_blur_risk(focal, shutter, is_stabilized=False):
    """Hand-shake risk via 1/focal rule. focal=mm, shutter=seconds or '1/125'."""
    try:
        f = float(focal)
    except Exception:
        return {"error": "missing focal"}
    t = _parse_shutter(shutter)
    if t is None or t <= 0:
        return {"error": "missing/invalid shutter"}
    safe_t = 1.0 / f
    if is_stabilized:
        safe_t = safe_t / 3.0   # ~3 stops of stabilization
    risk = (t < safe_t) and not is_stabilized
    warn = None
    if risk:
        warn = f"快门 ~1/{int(round(1 / t))} 慢于安全快门 ~1/{int(round(1 / safe_t))}（{f}mm），可能手抖"
    return {"focal": f, "shutter_s": t, "safe_shutter_s": round(safe_t, 6),
            "is_stabilized": bool(is_stabilized), "risk": risk, "warning": warn}


def diffraction_check(aperture, sensor="full-frame"):
    """Diffraction softening. aperture=f-number."""
    N = _parse_aperture(aperture)
    if N is None:
        return {"error": "missing aperture"}
    threshold = 16 if sensor == "full-frame" else 11
    soft = N >= threshold
    return {"aperture": N, "threshold": threshold, "soft": soft,
            "warning": (f"光圈 f/{N} ≥ f/{threshold}，衍射可能软化画质" if soft else None)}


def _is_raw(image_path):
    """True for RAW extensions (ARW/CR2/CR3/NEF/DNG/RAF/ORF/...)."""
    try:
        from utils import RAW_EXTS
        return os.path.splitext(image_path)[1].lower() in RAW_EXTS
    except Exception:
        return False


def _load_shot_image(image_path):
    """Decode a shot file to an RGB PIL Image, or None when undecodable.

    PIL alone is blind to RAW (ARW/CR2/CR3/NEF/DNG/RAF/ORF/...) -- route those
    through model_loader's embedded-preview decode (rawpy + disk cache, shared
    with the scoring pipeline). Never raises: callers must treat None as
    "cannot analyze this file" and say so, never report it as 'normal'.
    """
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        from model_loader import _load_image_rgb
        return _load_image_rgb(image_path)
    except Exception:
        return None


def _raw_preview_exif(image_path):
    """Best-effort EXIF from a RAW file's embedded JPEG preview (rawpy).
    Returns a PIL exif dict (maybe empty) or None when no preview is readable."""
    try:
        import io
        import rawpy
        from PIL import Image
        with rawpy.imread(image_path) as raw:
            thumb = raw.extract_thumb()
        if not thumb or thumb.format != rawpy.ThumbFormat.JPEG:
            return None
        return Image.open(io.BytesIO(thumb.data))._getexif() or {}
    except Exception:
        return None


def histogram_analysis(image_path):
    """Luminance histogram: clipping + mean. Downscaled for speed.
    RAW files go through the embedded-preview decode; an undecodable file
    returns an explicit error so the coach never mistakes it for 'normal'."""
    if not image_path or not os.path.exists(image_path):
        return {"error": "missing or invalid path"}
    try:
        import numpy as np
        img = _load_shot_image(image_path)
        if img is None:
            return {"error": "无法解码该文件（RAW 需 rawpy 支持）", "unreadable": True}
        img = img.convert("L")
        img.thumbnail((256, 256))
        arr = np.asarray(img, dtype=np.float32)
        total = float(arr.size) or 1.0
        clip_shadow = float((arr < 10).sum()) / total
        clip_highlight = float((arr > 245).sum()) / total
        mean = float(arr.mean())
        warn = None
        if clip_highlight > 0.02:
            warn = f"高光溢出 {round(clip_highlight * 100, 1)}%"
        elif clip_shadow > 0.02:
            warn = f"暗部溢出 {round(clip_shadow * 100, 1)}%"
        return {"mean": round(mean, 1), "clip_shadow_pct": round(clip_shadow * 100, 2),
                "clip_highlight_pct": round(clip_highlight * 100, 2), "warning": warn}
    except Exception as e:
        return {"error": str(e)}


def face_eye_check(image_path):
    """Face presence + eye-open (EAR) via eye_detector."""
    if not image_path or not os.path.exists(image_path):
        return {"error": "missing or invalid path"}
    try:
        import eye_detector
        src = image_path
        if _is_raw(image_path):
            # cv2.imread can't read RAW: decode the embedded preview and hand
            # the detector a BGR array instead of the file path.
            img = _load_shot_image(image_path)
            if img is None:
                return {"error": "无法解码该文件（RAW 需 rawpy 支持）", "unreadable": True}
            import numpy as np
            src = np.asarray(img, dtype=np.uint8)[:, :, ::-1]  # RGB -> BGR
        eo = eye_detector.eye_open_score(src)
        if eo is None:
            return {"face_detected": False, "eye_open": None,
                    "note": "无人脸或侧脸，无法判断眼睛"}
        warn = None
        if eo < 0.2:
            warn = "闭眼或眨眼"
        elif eo < 0.5:
            warn = "半睁"
        return {"face_detected": True, "eye_open": round(eo, 2), "warning": warn}
    except Exception as e:
        return {"error": str(e)}


def read_shot_exif(image_path):
    """Read EXIF: aperture/shutter/iso/focal/exposure_bias. The 'agent tool to
    read aperture & shutter' the user asked for. None fields when absent.
    RAW files: PIL can't open the container, so read the embedded preview
    JPEG's EXIF; explicit error when nothing is readable (never silent)."""
    if not image_path or not os.path.exists(image_path):
        return {"error": "missing or invalid path"}
    try:
        if _is_raw(image_path):
            exif = _raw_preview_exif(image_path)
            if exif is None:
                return {"error": "无法读取该 RAW 文件的 EXIF（预览解码失败或未安装 rawpy）",
                        "unreadable": True}
        else:
            from PIL import Image
            exif = Image.open(image_path)._getexif() or {}

        def _rational(v):
            if isinstance(v, tuple) and len(v) == 2 and v[1]:
                return v[0] / v[1]
            # Pillow 返回 IFDRational/Fraction：转成 float，否则 coach_advice
            # 事件在 service 的 json.dumps 处直接崩（且 str(Fraction)="14/5"
            # 还会让下游 _parse_aperture 静默解析失败）。
            try:
                return float(v)
            except Exception:
                return v

        fn = _rational(exif.get(33437)) if 33437 in exif else None   # FNumber
        et = _rational(exif.get(33434)) if 33434 in exif else None   # ExposureTime
        iso = exif.get(34855)                                          # ISOSpeedRatings
        fl = _rational(exif.get(37386)) if 37386 in exif else None   # FocalLength
        eb = _rational(exif.get(37380)) if 37380 in exif else None   # ExposureBiasValue
        shutter_str = None
        if et:
            shutter_str = f"1/{int(round(1 / et))}" if et < 1 else str(et)
        return {
            "aperture": (round(fn, 1) if fn else None),
            "shutter_s": (et if et else None),
            "shutter_str": shutter_str,
            "iso": (int(iso) if iso else None),
            "focal_length": (round(fl, 1) if fl else None),
            "exposure_bias": (round(eb, 2) if eb is not None else None),
        }
    except Exception as e:
        return {"error": str(e)}


def get_camera_settings():
    """Live settings via the active backend; None -> coach uses read_shot_exif.
    M1: DigiCamControlBackend returns None (stub) -> EXIF fallback."""
    if _router is None:
        return {"fallback": "exif", "note": "无活动后端，用 read_shot_exif 读文件 EXIF"}
    try:
        s = _router.get_settings()
    except Exception:
        s = None
    if s and isinstance(s, dict) and s.get("aperture"):
        return s
    return {"fallback": "exif", "note": "后端无实时参数能力，用 read_shot_exif"}


# ══════════════════════════════════════════════════════════════════════
# Slow-tier coach
# ══════════════════════════════════════════════════════════════════════

def _avg_score():
    if not _session:
        return None
    scores = [s["score"] for s in _session["shots"] if s.get("score") is not None]
    return round(sum(scores) / len(scores), 2) if scores else None


def _llm_advise(client, model, path, settings, rules, rules_fired, refs, playbook,
                file_unreadable=False):
    """One multimodal LLM call. Returns (advice_text, ok)."""
    import base64
    import io
    img_b64 = None
    try:
        im = _load_shot_image(path)  # RAW-safe (embedded preview); None on failure
        if im is not None:
            im.thumbnail((768, 768))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
            img_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        img_b64 = None

    sys = ("你是摄影拍摄伙伴教练。基于照片 EXIF、Layer1 规则诊断、参考样片、题材剧本，"
           "给简短可执行的拍摄建议（中文，≤150 字）。不要假装看到照片细节除非附了图像。"
           "优先指出技术问题（曝光/景深/快门/对焦/溢出），再给改进方向。")
    if playbook:
        sys += f"\n\n[题材剧本]\n{playbook[:1500]}"

    ref_summary = [{"caption": r.get("caption"), "exif": r.get("exif"), "score": r.get("score")} for r in refs]
    ctx = (f"EXIF/设置: {settings}\n规则诊断: {rules}\n触发警告: {rules_fired}\n"
           f"参考样片: {ref_summary}")
    if file_unreadable:
        ctx += ("\n注意：该照片文件无法解码（RAW 预览提取失败），未附图像、基于图像的规则未执行。"
                "请明确告诉用户「无法分析该文件」，建议检查 RAW 支持或改用 JPEG 联机，"
                "不要声称技术指标正常。")
    parts = [{"type": "text", "text": f"请给这张照片的拍摄建议。\n{ctx}"}]
    if img_b64:
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})

    msgs = [{"role": "system", "content": sys}, {"role": "user", "content": parts}]
    resp = client.chat.completions.create(model=model, messages=msgs, max_tokens=400, temperature=0.4)
    return ((resp.choices[0].message.content or "").strip(), True)


def _synthesize_advice(settings, rules, rules_fired, refs, file_unreadable=False):
    """Fallback when no LLM/multimodal: synthesize from rules. When the shot
    file itself couldn't be decoded, say so explicitly -- never claim 'normal'
    for a file the coach was blind to."""
    if rules_fired:
        return "建议关注：" + "; ".join(rules_fired[:3])
    if file_unreadable:
        return ("无法完整分析该文件：照片解码失败（RAW 需 rawpy 提取内嵌预览）。"
                "基于图像的直方图/人脸检测未执行，请勿视为「全部正常」。"
                "请确认已安装 RAW 支持（rawpy），或改用 JPEG 联机拍摄。")
    return "技术指标正常，无明确问题。"


def handle_coach_advise(params):
    """Slow-tier coach entry. Gathers settings + Layer1 + Layer2 + Layer3,
    calls multimodal LLM (or synthesizes), emits coach_advice event."""
    path = params.get("shot_path", "")
    trigger = params.get("trigger", "manual")
    if not path or not os.path.exists(path):
        return {"error": "missing shot_path"}

    # 1. settings: live (router) if it has aperture, else EXIF fallback
    live = None
    if _router:
        try:
            live = _router.get_settings()
        except Exception:
            live = None
    exif = read_shot_exif(path)
    settings = live if (live and isinstance(live, dict) and live.get("aperture")) else exif

    N = settings.get("aperture") if isinstance(settings, dict) else None
    # shutter: prefer shutter_s (EXIF float), else parse "shutter" (gphoto2 string like "1/250")
    t = settings.get("shutter_s") if isinstance(settings, dict) else None
    if t is None and isinstance(settings, dict) and settings.get("shutter"):
        t = _parse_shutter(settings.get("shutter"))
    iso = settings.get("iso") if isinstance(settings, dict) else None
    fl = settings.get("focal_length") if isinstance(settings, dict) else None

    # 2. Layer1 rules
    rules = {}
    rules_fired = []
    if N and t and iso:
        r = exposure_triangle_check(N, t, iso); rules["exposure"] = r
        rules_fired += r.get("warnings", [])
    if fl and t:
        r = motion_blur_risk(fl, t, False); rules["motion_blur"] = r
        if r.get("warning"): rules_fired.append(r["warning"])
    if t:
        r = flash_sync_check(t, False); rules["flash_sync"] = r
        if r.get("warning"): rules_fired.append(r["warning"])
    if N:
        r = diffraction_check(N); rules["diffraction"] = r
        if r.get("warning"): rules_fired.append(r["warning"])
    r = histogram_analysis(path); rules["histogram"] = r
    if r.get("warning"): rules_fired.append(r["warning"])
    r = face_eye_check(path); rules["face_eye"] = r
    if r.get("warning"): rules_fired.append(r["warning"])

    # 诚实性标记：文件本身无法解码分析（RAW 无 rawpy / 预览提取失败）时，
    # 后面的文案绝不能回「技术指标正常」。gphoto2 直连有实时参数时，
    # 上面的参数规则已照常跑过，不受影响。
    file_unreadable = (
        (isinstance(settings, dict) and settings.get("unreadable"))
        or (isinstance(rules.get("histogram"), dict) and rules["histogram"].get("unreadable"))
        or (isinstance(rules.get("face_eye"), dict) and rules["face_eye"].get("unreadable"))
    )

    # 3. Layer2 RAG refs
    refs = []
    try:
        import reference_library
        from model_loader import extract_context_feature, _load_image_rgb
        img = _load_image_rgb(path)
        if img is not None:
            feat = extract_context_feature(img)
            if feat is not None:
                genre = _session.get("genre", "") if _session else ""
                lighting = _session.get("lighting", "") if _session else ""
                refs = reference_library.retrieve(feat, genre, lighting, k=5)
    except Exception as e:
        send_log(f"[shooting_coach] ref retrieve failed: {e}", "warn")

    # 4. Layer3 genre playbook (agent_kb)
    playbook = ""
    try:
        import agent_module
        genre = _session.get("genre", "") if _session else ""
        if genre:
            doc = agent_module._kb_doc_by_name(f"{genre}-playbook")
            if doc:
                playbook = doc.get("content", "") or doc.get("body", "") or ""
    except Exception:
        pass

    # 5. multimodal LLM (or fallback)
    advice = ""
    llm_used = False
    try:
        import agent_module
        cfg = agent_module._load_agent_config()
        client, err = agent_module._make_client(cfg)
        model = agent_module.resolve_model(cfg)
        multimodal = bool(cfg.get("multimodal", False))
        if client and model and multimodal:
            advice, llm_used = _llm_advise(client, model, path, settings, rules,
                                           rules_fired, refs, playbook, file_unreadable)
        elif err:
            send_log(f"[shooting_coach] LLM unavailable: {err}", "warn")
    except Exception as e:
        send_log(f"[shooting_coach] LLM advise failed: {e}", "warn")
    if not advice:
        advice = _synthesize_advice(settings, rules, rules_fired, refs, file_unreadable)

    backend_name = None
    if _router and _router.active():
        backend_name = _router.active().name
    result = {
        "advice": advice, "rules_fired": rules_fired, "rules": rules,
        "refs": [{"id": r.get("id"), "caption": r.get("caption"), "score": r.get("score")} for r in refs],
        "settings": settings, "backend": backend_name, "llm_used": llm_used, "trigger": trigger,
    }
    send_event("coach_advice", {
        "session_id": (_session.get("id") if _session else None),
        "shot_path": path, **result,
    })
    return result


# ══════════════════════════════════════════════════════════════════════
# Fast-tier event subscription (trigger slow coach)
# ══════════════════════════════════════════════════════════════════════

def _on_fast_event(name, data):
    """subscribe_events callback: on watch_score_progress, accumulate + trigger."""
    if name != "watch_score_progress" or not _session:
        return
    img_info = data.get("image") or {}
    path = img_info.get("path") or data.get("last_file", "")
    score = data.get("last_score")
    if path:
        _session["shots"].append({"path": path, "score": score})
        _session["last_score"] = score
    n = len(_session["shots"])
    trigger = None
    # 自动建议频率: coach_every=N 每 N 张一次 (0=不按张数); coach_drop 掉分即触发
    every = int(_session.get("coach_every", 10) or 0)
    if every > 0 and n > 0 and n % every == 0:
        trigger = "every_n"
    prev = _session.get("prev_score")
    if _session.get("coach_drop", True) and score is not None and prev is not None and score < prev - 1.5:
        trigger = "score_drop"
    if score is not None:
        _session["prev_score"] = score
    if trigger and path:
        # Run async so we don't block the in-process event subscriber.
        threading.Thread(target=handle_coach_advise,
                         args=({"shot_path": path, "trigger": trigger},),
                         daemon=True).start()


# ══════════════════════════════════════════════════════════════════════
# IPC handlers
# ══════════════════════════════════════════════════════════════════════

def handle_tether_start(params):
    global _router, _session
    folder = params.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        raise ValueError(f"无效的联机文件夹: {folder}")

    # 重复 start（上次异常中断/状态错位）会先完整清理旧会话：否则旧 sidecar
    # 泄漏并与新 sidecar 抢相机 USB，旧订阅未退导致每张 shot 重复计数。
    # 不发 tether_stopped 事件，避免前端误复位新会话 UI。
    if _session or _router:
        send_log("[shooting_coach] tether_start: 检测到残留会话，先完整清理旧会话", "warn")
        _cleanup_tether()

    # 1. camera backend (folder-watch always fallback)
    _router = camera_backend.CameraRouter(folder)
    backend = _router.select()

    # 2. start folder_watcher with aggressive polling
    import folder_watcher
    folder_watcher.handle_start_folder_watch({
        "folder": folder, "mode": "realtime", "filter": params.get("filter", "all"),
        "interval": float(params.get("interval", 0.8)),
        "stability_ms": float(params.get("stability_ms", 800)),
        "output_folder": params.get("output_folder", ""),
        "threshold": float(params.get("threshold", 6.0)),
        "recursive": True, "process_existing": False,
    })

    # 3. subscribe to fast-tier score events
    _session = {
        "id": f"tether_{int(time.time())}", "folder": folder,
        "genre": params.get("genre", ""), "lighting": params.get("lighting", ""),
        "goal": params.get("goal", ""), "backend": backend.name,
        "caps": sorted(backend.capabilities()),
        "shots": [], "start_ts": time.time(), "prev_score": None,
        # 教练自动建议频率: 每 N 张一次 (0=不按张数) + 掉分触发开关
        "coach_every": int(params.get("coach_every", 10) or 0),
        "coach_drop": bool(params.get("coach_drop", True)),
    }
    _session["unsub"] = subscribe_events(_on_fast_event)
    send_event("tether_started", {"session_id": _session["id"], "folder": folder,
                                  "backend": backend.name, "caps": _session["caps"]})
    send_log(f"[shooting_coach] tether started: {folder} (backend={backend.name}, caps={_session['caps']})", "info")
    resp = {"ok": True, "session_id": _session["id"], "backend": backend.name, "caps": _session["caps"]}
    # gphoto2 connect() may fail with a structured error code. Surface it so the
    # UI can show actionable guidance instead of a generic folder-watch fallback.
    err_code = _router.gphoto2_error_code() if backend.name == "folder_watch" else None
    if err_code == "needs_driver":
        resp["needs_driver"] = True
        resp["error_code"] = "needs_driver"
        resp["help_url"] = "https://studio.dwphoto.top/tethered-setup"
        _session["needs_driver"] = True
        _session["error_code"] = "needs_driver"
        send_log("[shooting_coach] 相机未直连 - 需装 WinUSB 驱动，已降级文件夹监听并提示用户", "warn")
    elif err_code == "camera_not_found":
        resp["error_code"] = "camera_not_found"
        resp["help_url"] = "https://studio.dwphoto.top/tethered-setup#mac"
        _session["needs_driver"] = False
        _session["error_code"] = "camera_not_found"
        send_log("[shooting_coach] 相机未直连 - Mac: PTPCamera 可能占用 USB 端口 或 相机未设 PC Remote", "warn")
    else:
        _session["needs_driver"] = False
        _session["error_code"] = None
    return resp


def _cleanup_tether():
    """完整清理当前会话（停取景/停文件夹监听/断相机/退订阅/清全局），
    不发 tether_stopped 事件。handle_tether_stop 与 handle_tether_start
    的残留会话接管共用。"""
    global _router, _session
    # stop liveview first
    _liveview_stop.set()
    # stop folder watcher
    import folder_watcher
    try:
        folder_watcher.handle_stop_folder_watch({})
    except Exception:
        pass
    # disconnect the control backend (gphoto2 camera)
    if _router and _router.active():
        try:
            _router.active().disconnect()
        except Exception:
            pass
    if _session and _session.get("unsub"):
        try:
            _session["unsub"]()
        except Exception:
            pass
    _session = None
    _router = None


def handle_tether_stop(params):
    sid = _session.get("id") if _session else None
    _cleanup_tether()
    send_event("tether_stopped", {"session_id": sid})
    return {"ok": True}


def handle_tether_status(params):
    if not _session:
        return {"running": False}
    return {
        "running": True, "session_id": _session["id"], "folder": _session["folder"],
        "backend": _session["backend"], "caps": _session.get("caps", []),
        "shots": len(_session["shots"]),
        "last_score": _session.get("last_score"), "avg_score": _avg_score(),
        # Persisted at tether_start: keeps the guidance hint visible across
        # status refreshes (gphoto2 stays in needs_driver / camera_not_found
        # until the user resolves the issue and reconnects).
        "needs_driver": _session.get("needs_driver", False),
        "error_code": _session.get("error_code"),
    }


# ══════════════════════════════════════════════════════════════════════
# USB control: capture / liveview / live settings (Stage 2 - gPhoto2)
# ══════════════════════════════════════════════════════════════════════

def _control_backend():
    """The active control backend (gphoto2 > digicamcontrol > folder_watch)."""
    if _router and _router.active():
        return _router.active()
    return None


def handle_tether_capture(params):
    """Remote-capture via the control backend. gphoto2 captures + downloads to
    the tether folder, where folder_watcher picks it up for scoring."""
    b = _control_backend()
    if not b or camera_backend.CAP_CAPTURE not in b.capabilities():
        return {"error": "no capture-capable backend (connect a gPhoto2 camera)"}
    r = b.capture()
    return r or {"error": "capture failed"}


def handle_tether_get_settings(params):
    """Live settings via the control backend; {fallback:'exif'} if unavailable."""
    b = _control_backend()
    if not b or camera_backend.CAP_SETTINGS_READ not in b.capabilities():
        return {"fallback": "exif", "note": "无实时参数后端，用 read_shot_exif"}
    s = b.get_settings()
    return s or {"fallback": "exif"}


def handle_tether_get_setting_choices(params):
    """Valid choices per setting for UI dropdowns (gphoto2 backends)."""
    b = _control_backend()
    if not b or camera_backend.CAP_SETTINGS_WRITE not in b.capabilities():
        return {"error": "no settings-write-capable backend"}
    ch = b.get_setting_choices()
    return {"choices": ch} if ch else {"error": "choices unavailable"}


def handle_tether_set_setting(params):
    """Write one camera setting (aperture/shutter/iso/exposure_bias).
    Returns fresh settings so the UI can confirm the camera actually applied it."""
    b = _control_backend()
    if not b or camera_backend.CAP_SETTINGS_WRITE not in b.capabilities():
        return {"error": "no settings-write-capable backend (connect a gPhoto2 camera)"}
    key = params.get("key", "")
    value = params.get("value")
    if not key or value is None:
        return {"error": "missing key/value"}
    r = b.set_setting(key, value)
    if not r:
        return {"error": f"相机拒绝了 {key}={value}"}
    out = {"ok": True, "settings": b.get_settings()}
    if isinstance(r, dict):
        if r.get("applied") is False:
            out["applied"] = False
            out["note"] = r.get("note") or "相机未应用该设置（可能当前拍摄档位不允许）"
        elif r.get("applied") is not None:
            out["applied"] = True
    return out


def handle_tether_liveview_start(params):
    """Start the liveview polling loop -> liveview_frame events (noblock)."""
    global _liveview_thread
    # 线程守卫：已在跑（线程存活且未置 stop）时直接返回当前状态，避免双击/
    # 状态错位重复起线程消费同一后端导致帧率分裂。
    if _liveview_thread and _liveview_thread.is_alive():
        if not _liveview_stop.is_set():
            return {"ok": True, "already_running": True}
        # stop 已置位但旧线程尚未退出：等它走完再 clear，防止旧线程复活抢帧
        _liveview_thread.join(timeout=2.0)
    b = _control_backend()
    if not b or camera_backend.CAP_LIVEVIEW not in b.capabilities():
        return {"error": "no liveview-capable backend (gPhoto2 + liveview camera required)"}
    b.liveview_start()
    _liveview_stop.clear()
    _liveview_thread = threading.Thread(target=_liveview_loop, daemon=True)
    _liveview_thread.start()
    send_log("[shooting_coach] liveview started", "info")
    return {"ok": True}


def handle_tether_liveview_stop(params):
    _liveview_stop.set()
    b = _control_backend()
    if b:
        b.liveview_stop()
    send_log("[shooting_coach] liveview stopped", "info")
    return {"ok": True}


def _liveview_loop():
    """Poll the control backend for JPEG frames, emit liveview_frame events.
    ~15fps, noblock (no flush) - high-frequency, loss-tolerant."""
    import base64
    while not _liveview_stop.is_set():
        b = _control_backend()
        if not b:
            break
        t0 = time.time()
        try:
            data = b.liveview_frame()
        except Exception as e:
            send_log(f"[shooting_coach] liveview frame error: {e}", "warn")
            break
        if data:
            try:
                b64 = base64.b64encode(data).decode("ascii")
                send_event("liveview_frame", {"frame": b64}, noblock=True)
            except Exception:
                pass
        # target ~15fps (capture_preview itself takes time; this caps the loop)
        dt = time.time() - t0
        if dt < 0.066:
            _liveview_stop.wait(0.066 - dt)


def handle_tether_coach_config(params):
    """Live-update the coach auto-advice frequency for the running session.
    every: 每 N 张自动建议一次 (0=不按张数); drop: 评分掉 >1.5 自动建议."""
    if _session is None:
        return {"error": "no active session"}
    if "every" in params:
        _session["coach_every"] = int(params.get("every") or 0)
    if "drop" in params:
        _session["coach_drop"] = bool(params.get("drop"))
    return {"ok": True, "coach_every": _session.get("coach_every"),
            "coach_drop": _session.get("coach_drop")}


# ══════════════════════════════════════════════════════════════════════
# Layer1 agent-tool wrappers + registration into TOOL_REGISTRY
# ══════════════════════════════════════════════════════════════════════

def _t_read_shot_exif(session, args):
    return read_shot_exif(args.get("path", ""))


def _t_get_camera_settings(session, args):
    return get_camera_settings()


def _t_exposure_triangle(session, args):
    return exposure_triangle_check(args.get("aperture"), args.get("shutter"), args.get("iso"))


def _t_dof(session, args):
    return dof_calculator(args.get("focal"), args.get("aperture"),
                          args.get("distance"), args.get("sensor", "full-frame"))


def _t_flash_sync(session, args):
    return flash_sync_check(args.get("shutter"), bool(args.get("has_flash", False)))


def _t_motion_blur(session, args):
    return motion_blur_risk(args.get("focal"), args.get("shutter"),
                            bool(args.get("is_stabilized", False)))


def _t_diffraction(session, args):
    return diffraction_check(args.get("aperture"), args.get("sensor", "full-frame"))


def _t_histogram(session, args):
    return histogram_analysis(args.get("path", ""))


def _t_face_eye(session, args):
    return face_eye_check(args.get("path", ""))


def register_coach_tools():
    """Inject the 9 Layer1 tools into agent_module.TOOL_REGISTRY so the
    conversation agent can call them via function-calling. Called from
    register_handlers() (after agent_module is loaded)."""
    try:
        import agent_module

        def reg(name, desc, schema, fn, destructive=False):
            agent_module._tool(name, desc, schema, destructive)(fn)

        reg("read_shot_exif",
            "[拍摄] 读取照片 EXIF（光圈/快门/ISO/焦距/曝光补偿）。只读。",
            {"type": "object", "properties": {"path": {"type": "string", "description": "照片绝对路径"}},
             "required": ["path"], "additionalProperties": False}, _t_read_shot_exif)
        reg("get_camera_settings",
            "[拍摄] 读取当前相机实时设置（光圈/快门/ISO/焦距）。无活动后端或无此能力时返回 fallback=exif。只读。",
            {"type": "object", "properties": {}, "additionalProperties": False}, _t_get_camera_settings)
        reg("exposure_triangle_check",
            "[拍摄诊断] 检查曝光三角（光圈/快门/ISO），返回 EV 与警告。只读。",
            {"type": "object", "properties": {
                "aperture": {"type": "string", "description": "如 f/2.8 或 2.8"},
                "shutter": {"type": "string", "description": "如 1/125 或 0.008（秒）"},
                "iso": {"type": "integer"}},
             "required": ["aperture", "shutter", "iso"], "additionalProperties": False}, _t_exposure_triangle)
        reg("dof_calculator",
            "[拍摄诊断] 计算景深/超焦距。focal=mm，aperture=f数，distance=米。只读。",
            {"type": "object", "properties": {
                "focal": {"type": "number", "description": "焦距 mm"},
                "aperture": {"type": "number"},
                "distance": {"type": "number", "description": "对焦距离 米"},
                "sensor": {"type": "string", "enum": ["full-frame", "aps-c", "m43", "1-inch"]}},
             "required": ["focal", "aperture", "distance"], "additionalProperties": False}, _t_dof)
        reg("flash_sync_check",
            "[拍摄诊断] 检查闪光同步速度。shutter=秒或 1/125。只读。",
            {"type": "object", "properties": {
                "shutter": {"type": "string"},
                "has_flash": {"type": "boolean"}},
             "required": ["shutter"], "additionalProperties": False}, _t_flash_sync)
        reg("motion_blur_risk",
            "[拍摄诊断] 评估手抖风险（1/焦距规则）。focal=mm，shutter=秒或 1/125。只读。",
            {"type": "object", "properties": {
                "focal": {"type": "number"},
                "shutter": {"type": "string"},
                "is_stabilized": {"type": "boolean"}},
             "required": ["focal", "shutter"], "additionalProperties": False}, _t_motion_blur)
        reg("diffraction_check",
            "[拍摄诊断] 评估衍射软化。aperture=f数。只读。",
            {"type": "object", "properties": {
                "aperture": {"type": "number"},
                "sensor": {"type": "string"}},
             "required": ["aperture"], "additionalProperties": False}, _t_diffraction)
        reg("histogram_analysis",
            "[拍摄诊断] 分析直方图（高光/暗部溢出、均值）。只读。",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"], "additionalProperties": False}, _t_histogram)
        reg("face_eye_check",
            "[拍摄诊断] 检测人脸与睁眼状态（EAR）。只读。",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"], "additionalProperties": False}, _t_face_eye)
        send_log("[shooting_coach] registered 9 Layer1 coach tools into TOOL_REGISTRY", "info")
    except Exception as e:
        send_log(f"[shooting_coach] register_coach_tools failed: {e}", "error")


def _find_wdi_simple():
    """Locate the bundled wdi-simple.exe (libwdi's non-GUI CLI = Zadig's driver-
    install engine). Searches an env override + candidate tools/ dirs (incl. the
    packaged resources/tools/ via the exe anchor). Returns the absolute path or None."""
    env_path = os.environ.get("LENSPILOT_WDI_SIMPLE")
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    import sys as _sys
    exe_dir = os.path.dirname(os.path.abspath(_sys.executable))  # 打包: resources/python_backend/service/
    candidates = ([env_path] if env_path else []) + [
        os.path.join(backend_dir, "tools", "wdi-simple.exe"),               # python_backend/tools/
        os.path.join(backend_dir, "..", "tools", "wdi-simple.exe"),         # electron_app/tools/
        os.path.join(backend_dir, "..", "..", "tools", "wdi-simple.exe"),   # repo/tools/
        os.path.join(exe_dir, "..", "..", "tools", "wdi-simple.exe"),       # packaged: resources/tools/
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def handle_tether_install_driver(params):
    """Launch the elevated WinUSB driver installer (libwdi/wdi-simple) when the
    gphoto2 backend reports needs_driver. Driver install REQUIRES admin, so we
    spawn the helper via UAC elevation (Start-Process -Verb RunAs).

    打包(frozen)模式下磁盘上没有 .py 可跑 —— helper 内嵌在 service.exe 里，
    以 `service.exe --install-camera-driver` argv 子命令启动; dev 模式跑脚本文件。
    Returns {launched, ready}; ready=False → UI 降级打开手动指南。"""
    import sys as _sys
    import tempfile
    py = _sys.executable  # dev: python.exe; 打包(PyInstaller frozen): service.exe
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    frozen = bool(getattr(_sys, 'frozen', False))
    helper = os.path.join(backend_dir, "install_camera_driver.py")
    if not frozen and not os.path.isfile(helper):
        return {"launched": False, "ready": False,
                "reason": f"安装脚本缺失: {helper}"}
    wdi = _find_wdi_simple()
    if not wdi:
        return {"launched": False, "ready": False,
                "reason": "wdi-simple.exe 未找到（打包应含 resources/tools/wdi-simple.exe）",
                "guide_url": "https://studio.dwphoto.top/tethered-setup"}
    # Write a launcher .cmd that runs the helper with the current interpreter.
    # We run the .cmd elevated (not python directly) so quoting of long paths
    # with spaces survives Start-Process -Verb RunAs. The helper keeps its own
    # console open (input prompt) so the user can read the install result.
    cmd = f'@"{py}" --install-camera-driver\r\n' if frozen else f'@"{py}" "{helper}"\r\n'
    tmp = os.path.join(tempfile.gettempdir(), "lenspilot_install_driver.cmd")
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.write(cmd)
    except Exception as e:
        return {"launched": False, "ready": True, "error": f"写启动器失败: {e}"}
    try:
        import subprocess
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process -Verb RunAs -FilePath '{tmp}'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"launched": False, "ready": True, "error": f"提权启动失败: {e}"}
    send_log("[shooting_coach] 驱动安装器已提权启动 (UAC)", "info")
    return {"launched": True, "ready": True,
            "note": "安装程序已启动（UAC 提权）。完成后请重新点「开始联机」。"}


def register_handlers():
    register_handler("tether_start", handle_tether_start)
    register_handler("tether_stop", handle_tether_stop)
    register_handler("tether_status", handle_tether_status)
    register_handler("coach_advise", handle_coach_advise)
    # Stage 2 USB control
    register_handler("tether_capture", handle_tether_capture)
    register_handler("tether_get_settings", handle_tether_get_settings)
    register_handler("tether_get_setting_choices", handle_tether_get_setting_choices)
    register_handler("tether_set_setting", handle_tether_set_setting)
    register_handler("tether_coach_config", handle_tether_coach_config)
    register_handler("tether_liveview_start", handle_tether_liveview_start)
    register_handler("tether_liveview_stop", handle_tether_liveview_stop)
    register_handler("tether_install_driver", handle_tether_install_driver)
    register_coach_tools()
    send_log("shooting_coach module loaded", "info")
