"""gphoto2_sidecar.py - gPhoto2 camera control sidecar (runs under mingw python).

Why: on Windows, python-gphoto2 only works in mingw python (the MSVC-CPython
build hits a libgphoto2 camlib-loading -4). mingw python's gphoto2 is verified
fully working (Sony A7M4: detect/settings/liveview/capture). So the main
backend (CPython+torch) spawns THIS script via mingw python and proxies camera
commands over a JSON-line protocol on stdin/stdout.

Protocol (one JSON object per line):
  Request  (stdin):  {"cmd": "connect"|"get_settings"|"capture"|"liveview_start"|
                      "liveview_stop"|"disconnect"|"ping", ...args}
  Response (stdout): {"type":"response", "ok":bool, ...data}
  Event    (stdout): {"type":"event", "event":"liveview_frame"|"error", ...data}

Uses the verified python-gphoto2 2.6.x API (capture_preview(cam_file, ctx);
Sony config names f-number/focalposition). Runs under mingw python 3.14.
"""
import sys
import os
import json
import base64
import threading
import time

# mingw python needs CAMLIBS/IOLIBS to find libgphoto2 camlib/iolib sub-dirs,
# and PATH (mingw/bin) so the camlib .dll deps resolve. The parent sets these
# in the spawn env, but ensure defaults too.
os.environ.setdefault("CAMLIBS", r"C:\msys64\mingw64\lib\libgphoto2\2.5.34")
os.environ.setdefault("IOLIBS", r"C:\msys64\mingw64\lib\libgphoto2_port\0.12.2")
_mwb = r"C:\msys64\mingw64\bin"
if os.path.isdir(_mwb) and _mwb not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _mwb + os.pathsep + os.environ.get("PATH", "")
try:
    if sys.platform == "win32":
        os.add_dll_directory(_mwb)
except Exception:
    pass

import gphoto2 as gp

_ctx = gp.Context()
_cam = None
_cam_lock = threading.Lock()
_lv_stop = threading.Event()
_lv_thread = None
_shutdown_done = False
# Pacing: a new capture cycle started within this window after the previous one
# can hang the camera session (verified A7M4: re-init's cam.exit blocks forever).
_last_capture_end = 0.0
_MIN_CAPTURE_INTERVAL = 1.0  # seconds of camera quiet time between captures


def send(obj):
    """Write one JSON line to stdout (protocol message)."""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


_TRACE_PATH = os.path.join(os.environ.get("TEMP", os.getcwd()),
                           "lenspilot_sidecar_trace.log")

def _trace(msg):
    """File-based trace (sidecar stderr used to be DEVNULL, so this is the only way
    to see what happens inside the sidecar during a real app run)."""
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as _f:
            _f.write("%.3f %s\n" % (time.time(), str(msg)))
    except Exception:
        pass


def _get_cfg(name):
    try:
        return _cam.get_config(_ctx).get_child_by_name(name).get_value()
    except Exception:
        return None


def _file_get(folder, name):
    """One download attempt into a fresh CameraFile. Returns the CameraFile."""
    cf = gp.CameraFile()
    _cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL, cf, _ctx)
    return cf


def _save_cam_file(cf, local):
    """Save a CameraFile to disk via PYTHON's open(), not cf.save().

    gphoto2's gp_file_save mangles non-ASCII paths on Windows (mingw) and fails
    with [-1] Unspecified error -- this was the actual root cause of the
    "capture always fails in app" saga: the user's tether folder had Chinese
    characters. get_data_and_size() + open() is fully Unicode-safe.
    (Verified: cf.save -> [-1] on 'tether_中文测试\\', open() -> OK.)
    """
    data = cf.get_data_and_size()
    with open(local, "wb") as f:
        f.write(data)


def _file_delete(folder, name):
    """Best-effort delete of a camera-side file (never raises).

    Sony tethered shots live in a volatile RAM buffer (capt_*, folder '/').
    Deleting them after download keeps the buffer/object-store clean -- a
    cluttered store makes later file_get calls fail with [-1]."""
    try:
        _cam.file_delete(folder, name, _ctx)
        return True
    except Exception as e:
        _trace(f"file_delete {folder}/{name} failed: {e}")
        return False


def _rescue_stale_capt(download_dir, limit=30):
    """On connect: find leftover capt_* objects in '/' (RAM buffer from previous
    crashed/failed sessions), download them to download_dir (rescue), then delete
    them camera-side. A stale-cluttered RAM store is the prime suspect for
    intermittent file_get [-1] on NEW captures."""
    try:
        lst = _cam.folder_list_files("/", _ctx)
        names = [lst.get_name(i) for i in range(len(lst))]
    except Exception as e:
        _trace(f"rescue: list '/' failed: {e}")
        return 0
    stale = [n for n in names if n.startswith("capt_")][:limit]
    if not stale:
        return 0
    _trace(f"rescue: {len(stale)} stale capt_* in RAM buffer: {stale[:5]}")
    rescued = 0
    for name in stale:
        if download_dir:
            try:
                os.makedirs(download_dir, exist_ok=True)
                cf = _file_get("/", name)
                _save_cam_file(cf, os.path.join(download_dir, os.path.basename(name)))
                rescued += 1
            except Exception as e:
                _trace(f"rescue: download {name} failed: {e} (deleting anyway)")
        _file_delete("/", name)
    _trace(f"rescue: rescued {rescued}/{len(stale)}, all deleted camera-side")
    return rescued


def do_connect(args):
    global _cam
    try:
        # Autodetect FIRST: distinguishes "no camera visible to libgphoto2"
        # (driver/cable/mode issue) from other init failures. On Windows without
        # WinUSB (Zadig not run), the default PTP class driver claims the camera
        # and libgphoto2 sees 0 devices -> flag needs_driver so the UI can point
        # the user to the driver-install guide instead of a generic error.
        # autodetect is read-only and ~1s; run once per connect.
        try:
            n_cams = gp.gp_camera_autodetect(_ctx)[0]
        except Exception:
            n_cams = -1  # autodetect itself broke; fall through to init for the real error
        if n_cams == 0:
            send({"type": "response", "ok": False, "error_code": "needs_driver",
                  "error": "未检测到相机。最常见原因：未用 Zadig 安装 WinUSB 驱动"
                           "（Windows 默认 PTP 驱动挡住了 libgphoto2）。"
                           "也请确认相机已开机、USB 模式为 PC Remote、使用数据线。"})
            return
        with _cam_lock:
            _cam = gp.Camera()
            _cam.init(_ctx)
            rescued = _rescue_stale_capt(args.get("download_dir", ""))
        send({"type": "response", "ok": True,
              "caps": ["capture", "settings_read", "settings_write", "liveview"],
              "rescued": rescued})
    except Exception as e:
        send({"type": "response", "ok": False, "error": str(e)})


def do_get_settings(args):
    if not _cam:
        send({"type": "response", "ok": False, "error": "not connected"})
        return
    try:
        with _cam_lock:
            send({"type": "response", "ok": True, "settings": {
                "aperture": _get_cfg("f-number"),
                "shutter": _get_cfg("shutterspeed"),
                "iso": _get_cfg("iso"),
                "focal_length": None,  # gphoto2 exposes focalposition (focus pos), not mm
                "exposure_bias": _get_cfg("exposurecompensation"),
            }})
    except Exception as e:
        send({"type": "response", "ok": False, "error": str(e)})


_ADJUSTABLE_SETTINGS = ("f-number", "shutterspeed", "iso", "exposurecompensation")


def do_get_setting_choices(args):
    """Valid choices for each adjustable setting, for UI dropdowns.
    Choice-type widgets (RADIO/MENU) -> list of strings; free-form -> None."""
    if not _cam:
        send({"type": "response", "ok": False, "error": "not connected"})
        return
    choices = {}
    try:
        with _cam_lock:
            cfg = _cam.get_config(_ctx)
            for name in _ADJUSTABLE_SETTINGS:
                try:
                    w = cfg.get_child_by_name(name)
                    if w.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                        choices[name] = [w.get_choice(i) for i in range(w.count_choices())]
                    else:
                        choices[name] = None
                except Exception:
                    choices[name] = None
        _trace(f"get_setting_choices: {[(k, len(v) if v else 0) for k, v in choices.items()]}")
        send({"type": "response", "ok": True, "choices": choices})
    except Exception as e:
        send({"type": "response", "ok": False, "error": str(e)})


def do_set_setting(args):
    """Set one camera config value by widget name. Serialized with the preview
    loop via _cam_lock (A7M4 accepts set_config while preview runs). Returns the
    actual value read back from the camera for confirmation."""
    if not _cam:
        send({"type": "response", "ok": False, "error": "not connected"})
        return
    name = args.get("name", "")
    value = args.get("value")
    if not name or value is None:
        send({"type": "response", "ok": False, "error": "missing name/value"})
        return
    try:
        with _cam_lock:
            cfg = _cam.get_config(_ctx)
            w = cfg.get_child_by_name(name)
            t = w.get_type()
            if t in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU, gp.GP_WIDGET_TEXT):
                w.set_value(str(value))
            elif t == gp.GP_WIDGET_RANGE:
                w.set_value(float(value))
            elif t == gp.GP_WIDGET_TOGGLE:
                w.set_value(int(value))
            else:
                w.set_value(value)
            _cam.set_config(cfg, _ctx)
            # The camera applies config changes asynchronously -- an immediate
            # read-back can return the OLD value (verified A7M4). Poll briefly;
            # report whether the value actually took so the UI can explain
            # "camera ignored this" (e.g. aperture in shutter-priority mode).
            actual = None
            applied = False
            for _ in range(5):
                time.sleep(0.4)
                actual = _cam.get_config(_ctx).get_child_by_name(name).get_value()
                if str(actual) == str(value):
                    applied = True
                    break
        _trace(f"set_setting: {name} -> {actual!r} (wanted {value!r}) applied={applied}")
        resp = {"type": "response", "ok": True, "name": name, "value": actual, "applied": applied}
        if not applied:
            resp["note"] = "相机未应用该设置（可能当前拍摄档位不允许，如快门优先时光圈由相机控制）"
        send(resp)
    except Exception as e:
        _trace(f"set_setting: {name}={value!r} FAIL {e}")
        send({"type": "response", "ok": False, "error": str(e)})


def _drain_events(max_ms=300):
    """Flush pending camera events (FILE_ADDED / PROPERTY_CHANGED / CAPTURE_COMPLETE).

    Undrained events are what actually wedged consecutive captures (the earlier
    "re-init needed" superstition): with a drain before each trigger, 5/5
    back-to-back captures pass with NO exit/init (tether_noreinit_stress.py),
    and each capture is ~2.5x faster. NEVER call between trigger and file_get:
    wait_for_event invalidates the volatile RAM buffer -> [-1]."""
    end = time.time() + max_ms / 1000.0
    n = 0
    while time.time() < end:
        try:
            ev = _cam.wait_for_event(50, _ctx)
        except Exception:
            break
        if ev[0] == gp.GP_EVENT_TIMEOUT:
            break
        n += 1
    return n


def do_capture(args):
    """Capture + download. Sony (and most cameras) can't download a just-captured
    frame while liveview (capture_preview) is active -- file_get fails even though
    the shutter fires. So we pause the preview loop around the capture, then resume
    it (like a phone camera freezing briefly for the shot).

    Session hygiene: NO exit/init re-init between captures -- it intermittently
    HANGS forever (ctypes blocking call), and force-killing the stuck sidecar
    leaves the USB session half-open (camera wedged until power-cycle). A short
    wait_for_event drain before the trigger keeps the session healthy instead
    (verified 5/5 no-reinit captures on A7M4).

    Download robustness: the Sony RAM-buffer object occasionally rejects file_get
    with [-1] (transient USB/session state, or a stale-cluttered object store).
    Retry ladder: immediate -> 0.4s backoff -> drain+retry -> wait_for_event(SD).
    After a successful save, the camera-side object is DELETED to keep the
    volatile RAM buffer clean (leftover capt_* objects correlate with later [-1]).

    Pacing: enforce a minimum quiet interval between capture cycles."""
    global _lv_thread, _last_capture_end
    if not _cam:
        send({"type": "response", "ok": False, "error": "not connected"})
        return
    gap = time.time() - _last_capture_end
    if gap < _MIN_CAPTURE_INTERVAL:
        _trace(f"capture: pacing sleep {_MIN_CAPTURE_INTERVAL - gap:.2f}s")
        time.sleep(_MIN_CAPTURE_INTERVAL - gap)
    download_dir = args.get("download_dir", "")
    was_streaming = _lv_thread is not None and _lv_thread.is_alive()
    _trace(f"capture: enter was_streaming={was_streaming} lvthread_alive={_lv_thread.is_alive() if _lv_thread else None} dir={download_dir!r}")
    if was_streaming:
        _lv_stop.set()
        if _lv_thread is not None:
            _lv_thread.join(timeout=2.0)
        _trace(f"capture: after join lvthread_alive={_lv_thread.is_alive() if _lv_thread else None}")
    try:
        with _cam_lock:
            drained = _drain_events(300)
            _trace(f"capture: drained {drained} pending events")
            try:
                path = _cam.capture(gp.GP_CAPTURE_IMAGE, _ctx)
                _trace(f"capture: trigger ok folder={path.folder!r} name={path.name!r}")
            except Exception as e:
                _trace(f"capture: trigger FAIL {e}; drain + one retry")
                _drain_events(500)
                try:
                    path = _cam.capture(gp.GP_CAPTURE_IMAGE, _ctx)
                    _trace(f"capture: trigger retry ok folder={path.folder!r} name={path.name!r}")
                except Exception as e2:
                    _trace(f"capture: trigger retry FAIL {e2}")
                    raise RuntimeError("trigger: " + str(e2))
            local = None
            if download_dir:
                # IMPORTANT: file_get MUST be immediate. Sony saves the tethered
                # shot to a volatile RAM buffer (capt_<name>, folder '/'); calling
                # wait_for_event first invalidates that buffer -> [-1]. (Verified
                # via gphoto2_capture_diag.py: immediate NORMAL file_get works,
                # RAW and post-wait_for_event both fail.)
                cf = _download_with_retries(path.folder, path.name)
                if cf is None:
                    # Total failure: remove the stale object so it doesn't poison
                    # later captures, and give an actionable message.
                    _file_delete(path.folder, path.name)
                    raise RuntimeError(
                        "download %s/%s: [-1] 多次重试均失败 — "
                        "照片可能仍在相机缓冲中。若反复出现，请关机重开相机（或拔插 USB）"
                        % (path.folder, path.name))
                os.makedirs(download_dir, exist_ok=True)
                local = os.path.join(download_dir, os.path.basename(path.name))
                _save_cam_file(cf, local)
                _trace(f"capture: download OK local={local}")
                _file_delete(path.folder, path.name)  # keep RAM buffer clean
        send({"type": "response", "ok": True, "path": local, "name": path.name})
    except Exception as e:
        send({"type": "response", "ok": False, "error": str(e)})
    finally:
        _last_capture_end = time.time()
        if was_streaming:
            _lv_stop.clear()
            _lv_thread = threading.Thread(target=_lv_loop, daemon=True)
            _lv_thread.start()
            _trace("capture: liveview restarted")


def _download_with_retries(folder, name):
    """file_get with a retry ladder. Returns a CameraFile on success, else None.

    Ladder (each step logs to trace):
      1. immediate file_get (the RAM buffer is volatile -- first try is best)
      2. 0.4s backoff, plain retry (transient USB/session hiccup)
      3. drain pending events (300ms) then retry
      4. wait_for_event FILE_ADDED (~3s): if the shot ALSO landed on the SD card,
         the event path points at the SD copy -- fetch that instead. LAST resort:
         wait_for_event itself invalidates the RAM buffer, so never before step 3.
    """
    try:
        cf = _file_get(folder, name)
        _trace("capture: download OK (immediate)")
        return cf
    except Exception as e:
        _trace(f"capture: download FAIL (immediate) {e}")
    time.sleep(0.4)
    try:
        cf = _file_get(folder, name)
        _trace("capture: download OK (retry after 0.4s)")
        return cf
    except Exception as e:
        _trace(f"capture: download FAIL (retry) {e}")
    try:
        _drain_events(300)
        cf = _file_get(folder, name)
        _trace("capture: download OK (after drain)")
        return cf
    except Exception as e:
        _trace(f"capture: download FAIL (after drain) {e}")
    # Last resort: SD-card copy via FILE_ADDED event
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            ev = _cam.wait_for_event(700, _ctx)
            etype, edata = ev[0], ev[1]
            if etype == gp.GP_EVENT_FILE_ADDED:
                efolder, ename = edata.folder, edata.name
                _trace(f"capture: event FILE_ADDED {efolder}/{ename}")
                try:
                    cf = _file_get(efolder, ename)
                    _trace("capture: download OK (from SD event path)")
                    return cf
                except Exception as e2:
                    _trace(f"capture: download FAIL (event path) {e2}")
                    return None
    except Exception as e:
        _trace(f"capture: wait_for_event fallback error {e}")
    return None


def _lv_loop():
    _trace("lv_loop: start")
    while not _lv_stop.is_set() and _cam:
        try:
            with _cam_lock:
                cf = gp.CameraFile()
                _cam.capture_preview(cf, _ctx)
                data = cf.get_data_and_size()
            if data:
                send({"type": "event", "event": "liveview_frame",
                      "frame": base64.b64encode(data).decode("ascii")})
        except Exception as e:
            _trace(f"lv_loop: error {e}")
            send({"type": "event", "event": "error", "msg": "liveview: " + str(e)})
            break
        # ~15-20fps; capture_preview itself takes time
        _lv_stop.wait(0.05)
    _trace("lv_loop: exit")


def do_liveview_start(args):
    global _lv_thread
    if not _cam:
        send({"type": "response", "ok": False, "error": "not connected"})
        return
    if _lv_thread is not None and _lv_thread.is_alive():
        send({"type": "response", "ok": True, "already": True})  # already streaming
        return
    _lv_stop.clear()
    _lv_thread = threading.Thread(target=_lv_loop, daemon=True)
    _lv_thread.start()
    send({"type": "response", "ok": True})


def do_liveview_stop(args):
    _lv_stop.set()
    send({"type": "response", "ok": True})


def do_disconnect(args):
    global _cam, _lv_thread
    _lv_stop.set()
    with _cam_lock:
        if _cam:
            try:
                _cam.exit(_ctx)
            except Exception:
                pass
        _cam = None
    send({"type": "response", "ok": True})


_HANDLERS = {
    "ping": lambda a: send({"type": "response", "ok": True, "pong": True}),
    "connect": do_connect,
    "get_settings": do_get_settings,
    "get_setting_choices": do_get_setting_choices,
    "set_setting": do_set_setting,
    "capture": do_capture,
    "liveview_start": do_liveview_start,
    "liveview_stop": do_liveview_stop,
    "disconnect": do_disconnect,
}


def main():
    send({"type": "event", "event": "ready"})
    _trace(f"sidecar: start pid={os.getpid()} py={sys.executable} gp={gp.__version__}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        cmd = msg.get("cmd")
        h = _HANDLERS.get(cmd)
        if h:
            try:
                h(msg)
            except Exception as e:
                send({"type": "response", "ok": False, "error": "handler: " + str(e)})
        else:
            send({"type": "response", "ok": False, "error": "unknown cmd: " + str(cmd)})
        if cmd == "disconnect":
            break
    _shutdown_cam()


def _shutdown_cam():
    """Close the camera session cleanly on the way out. A half-open USB session
    (sidecar killed without exit) leaves the camera wedged for the NEXT session
    (init [-10] timeouts until power-cycle). Idempotent: main() calls it and
    atexit calls it again."""
    global _cam, _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    _lv_stop.set()
    try:
        if _cam:
            _cam.exit(_ctx)
    except Exception:
        pass
    _cam = None
    _trace(f"sidecar: exit pid={os.getpid()}")


import atexit
atexit.register(_shutdown_cam)


if __name__ == "__main__":
    main()
