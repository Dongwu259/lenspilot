"""
camera_backend.py - Camera control abstraction for tethered shooting (0.8.6 M1).

Tier 0  FolderWatchBackend   : any external tethering app (digiCamControl / EOS
        Utility / Lightroom / gPhoto2 CLI / ...) writes captures into a watched
        folder. No direct camera control; the coach falls back to post-capture
        EXIF for settings. ALWAYS available - the universal baseline.
Tier 1  DigiCamControlBackend: Windows digiCamControl HTTP API for *live* settings.
        Capture still happens via folder-watch (digiCamControl writes the files);
        this backend only adds live settings-read when reachable.
        M1 scope: reachability probe + settings stub (HTTP API endpoints/ports
        need real-world verification -> degrade to None -> coach uses EXIF).
        liveview/trigger deferred to M1.5.
Tier 2  GPhoto2Backend        : libgphoto2 USB control (~2000+ models).
        Windows runs the mingw-python sidecar (gphoto2_sidecar.py); Mac/Linux
        import gphoto2 directly in-process. Verified on Sony A7M4.

CameraRouter picks a backend by capability; FolderWatchBackend is always the
fallback. The coach layer reads capabilities() and degrades gracefully:
  no SETTINGS_READ -> use read_shot_exif(path) post-capture
  no LIVEVIEW      -> hide liveview UI
  nothing          -> pure folder watch

See TETHERED_SHOOTING_TECH_SPEC.md §1, IMPLEMENTATION_PLAN_M1.md Step 1.
"""
import os
import sys
import threading
import time
import urllib.request

from service import send_log

# ── Capability flags ──
CAP_FILE_WATCH = "file_watch"          # observes a folder for new captures
CAP_CAPTURE = "capture"                # can trigger capture itself
CAP_SETTINGS_READ = "settings_read"    # can read live aperture/shutter/iso/focal
CAP_SETTINGS_WRITE = "settings_write"
CAP_LIVEVIEW = "liveview"


class CameraBackend:
    """Abstract camera-control backend.

    Subclasses declare capabilities(); methods they don't support return
    None / False / empty so callers can degrade uniformly. The coach never
    assumes a capability is present - it checks capabilities() first.
    """
    name = "base"

    def list_devices(self):
        """Discoverable devices as [{id, label}]. [] when none / unsupported."""
        return []

    def connect(self, device_id=None):
        """device_id: folder path for FolderWatch, camera id otherwise.
        Returns True on success."""
        return False

    def disconnect(self):
        pass

    def capture(self):
        """Trigger a capture. Returns captured-file info or None if unsupported."""
        return None

    def get_settings(self):
        """Return {aperture, shutter, iso, focal_length, exposure_bias} or None.
        None => backend can't read live settings; coach falls back to EXIF."""
        return None

    def set_setting(self, key, value):
        return False

    def liveview(self):
        """Return an iterator of frames, or None if unsupported."""
        return None

    def liveview_frame(self):
        """Return one JPEG frame (bytes), or None. Poll repeatedly for a stream.
        Default None - backends without liveview leave this."""
        return None

    def liveview_start(self):
        """Start liveview streaming (for streaming backends like the gphoto2
        sidecar). Default no-op - direct backends just poll liveview_frame()."""
        pass

    def liveview_stop(self):
        """Stop liveview streaming. Default no-op."""
        pass

    def capabilities(self):
        """Return the set of CAP_* this backend currently offers."""
        return set()

    def is_connected(self):
        return False


class FolderWatchBackend(CameraBackend):
    """Tier 0: no direct camera control.

    Relies on an external tethering app writing captures into a watched folder;
    LensPilot's folder_watcher detects and scores them. Always available - this
    is the universal baseline that makes LensPilot brand-agnostic out of the box.

    No live settings: the coach uses read_shot_exif() on each captured file.
    """
    name = "folder_watch"

    def __init__(self):
        self._folder = None

    def connect(self, device_id=None):
        folder = (device_id or "").strip()
        if folder and os.path.isdir(folder):
            self._folder = folder
            return True
        # Tolerate a not-yet-existing folder: still "connected" so the watcher
        # can be started (it creates on demand); is_connected reflects intent.
        self._folder = folder or None
        return bool(self._folder)

    def disconnect(self):
        self._folder = None

    def capabilities(self):
        return {CAP_FILE_WATCH}

    def is_connected(self):
        return self._folder is not None


class DigiCamControlBackend(CameraBackend):
    """Tier 1: digiCamControl (Windows) HTTP API for live settings.

    Capture still happens via folder-watch (digiCamControl writes files to the
    watched folder); this backend only enriches with live settings-read when the
    digiCamControl CameraControlServer plugin is reachable.

    M1 scope: reachability probe only. The settings-read HTTP endpoints/variable
    names need verification against a real digiCamControl install + target
    cameras, so get_settings() returns None in M1 (coach uses EXIF). M1.5 fills
    in the verified fetch. Default port 59782 is the documented default but MUST
    be confirmed.
    """
    name = "digicamcontrol"
    DEFAULT_PORT = 59782
    _PROBE_TTL = 5.0  # cache probe result for 5s

    def __init__(self, base_url=None, port=None):
        p = port or self.DEFAULT_PORT
        self._base = (base_url or f"http://127.0.0.1:{p}").rstrip("/")
        self._folder = None
        self._probe_cache = None   # None=unknown, True/False
        self._probe_ts = 0.0
        self._probe_lock = threading.Lock()
        self._stub_warned = False

    def _probe(self, force=False):
        """Best-effort reachability check of the digiCamControl HTTP server.
        Cached briefly to avoid hammering on every shot."""
        now = time.time()
        with self._probe_lock:
            if self._probe_cache is not None and not force and (now - self._probe_ts) < self._PROBE_TTL:
                return self._probe_cache
            ok = False
            try:
                req = urllib.request.Request(self._base + "/session", method="GET")
                with urllib.request.urlopen(req, timeout=1.5) as r:
                    ok = (r.status == 200)
            except Exception:
                ok = False
            self._probe_cache = ok
            self._probe_ts = now
            return ok

    def connect(self, device_id=None):
        self._folder = (device_id or "").strip() or None
        # "Connected" = the HTTP server is up. Folder is optional context.
        return self._probe()

    def disconnect(self):
        self._folder = None

    def get_settings(self):
        """M1 stub: returns None.

        digiCamControl's CameraControlServer exposes camera variables over HTTP,
        but the exact endpoints/variable names are unverified. Rather than guess
        (risk of garbage strings as 'aperture'), M1 degrades to None and the
        coach falls back to read_shot_exif(). M1.5 implements the verified fetch
        against a real install and adds CAP_SETTINGS_READ here.
        """
        if not self._probe():
            return None
        if not self._stub_warned:
            send_log("[camera_backend] digiCamControl detected; settings-read API "
                     "unverified (M1.5) -> using EXIF fallback", "info")
            self._stub_warned = True
        return None

    def capabilities(self):
        # M1: only file_watch. CAP_SETTINGS_READ is added in M1.5 once
        # get_settings() returns verified values.
        caps = {CAP_FILE_WATCH}
        if self._probe():
            s = self.get_settings()
            if s:
                caps.add(CAP_SETTINGS_READ)
        return caps

    def is_connected(self):
        return self._probe()


class GPhoto2Backend(CameraBackend):
    """Tier 2: gPhoto2/libgphoto2 USB control. Broadest brand coverage (~2000+
    models). Verified on Sony A7M4 (ILCE-7M4): detect/settings/liveview/capture.

    Two modes:
      - Mac/Linux: DIRECT import of gphoto2 in this process (python-gphoto2 works
        in the main CPython there).
      - Windows: SIDECAR. python-gphoto2 only works in mingw python (the MSVC
        build hits a libgphoto2 camlib-loading -4). So we spawn a mingw-python
        subprocess running gphoto2_sidecar.py and proxy commands over JSON-line
        stdin/stdout. The main backend (CPython+torch) stays the caller.

    `import gphoto2` (direct mode) is optional. On Windows the sidecar python
    (mingw) must exist; else the backend is inactive. Never raises.

    API notes (python-gphoto2 2.6.x, verified A7M4):
      - gp_camera_autodetect(ctx) -> [count, CameraList]; get_name(i)/get_value(i).
      - capture_preview(cam_file, ctx) - CameraFile FIRST.
      - Sony config: 'f-number' (not aperture), 'focalposition' (not focallength).
    """
    name = "gphoto2"

    def __init__(self, download_dir=None):
        self._download_dir = download_dir
        # direct mode (Mac/Linux)
        self._cam = None
        self._ctx = None
        self._gp = None
        # sidecar mode (Windows)
        self._use_sidecar = (sys.platform == "win32")
        self._proc = None
        self._reader = None
        self._resp_q = None
        self._frame_q = None
        # Serializes sidecar commands: service.py dispatches IPC handlers on a
        # 16-thread pool, so tether_capture and the 3s tether_get_settings poll
        # run CONCURRENTLY. Without a lock, two _send_cmd calls interleave stdin
        # writes and steal each other's responses from the shared _resp_q.
        self._send_lock = threading.Lock()
        self._connected = False
        self._caps = set()
        # Sidecar events (type=="event") are NOT command responses: the reader
        # diverts them here instead of _resp_q. A stray event consumed as a
        # response shifts every later response by one and never self-heals.
        self._last_event = None    # last non-frame event dict (ready/error/...)
        self._last_error = None    # msg of the last "error" event (liveview failures)
        self._ready_ev = threading.Event()  # set when the sidecar announces "ready"
        # Structured failure code from the last connect() attempt, surfaced to
        # the UI so it can guide the user (e.g. "needs_driver" -> open the
        # Zadig/WinUSB install guide instead of a generic "connect failed").
        self._last_error_code = None
        # Direct-mode (Mac/Linux) session hygiene, mirroring gphoto2_sidecar.py:
        # libgphoto2 calls on one camera are NOT thread-safe, and service.py
        # dispatches IPC on a thread pool (settings poll + liveview poll +
        # capture run concurrently) -> serialize every camera call with this.
        self._cam_lock = threading.Lock()
        # Pacing: a new capture cycle started too soon after the previous one
        # can hang the camera session (verified A7M4 in the sidecar).
        self._last_capture_end = 0.0
        self._MIN_CAPTURE_INTERVAL = 1.0

    # ── direct-mode helper (Mac/Linux) ──
    def _ensure_gp(self):
        if self._gp is not None:
            return self._gp
        try:
            import gphoto2 as gp
            self._gp = gp
        except ImportError:
            self._gp = None
        except Exception:
            self._gp = None
        return self._gp

    # ── direct-mode helpers (Mac/Linux); the sidecar carries its own copies ──
    def _drain_events(self, max_ms=300):
        """Flush pending camera events (FILE_ADDED / PROPERTY_CHANGED / ...).

        Undrained events wedge consecutive captures. NEVER call between trigger
        and file_get: wait_for_event invalidates the volatile RAM buffer -> [-1].
        Caller must hold _cam_lock."""
        gp = self._gp
        end = time.time() + max_ms / 1000.0
        n = 0
        while time.time() < end:
            try:
                ev = self._cam.wait_for_event(50, self._ctx)
            except Exception:
                break
            if ev[0] == gp.GP_EVENT_TIMEOUT:
                break
            n += 1
        return n

    @staticmethod
    def _save_cam_file(cf, local):
        """Save a CameraFile via PYTHON's open(), not cf.save(): gp_file_save
        mangles non-ASCII paths ([-1] on Windows; the open() path is Unicode-safe
        everywhere). Caller must hold _cam_lock."""
        data = cf.get_data_and_size()
        with open(local, "wb") as f:
            f.write(data)

    def _file_delete(self, folder, name):
        """Best-effort camera-side delete (never raises). Keeps the volatile
        RAM buffer clean -- a cluttered store makes later file_get fail [-1].
        Caller must hold _cam_lock."""
        try:
            self._cam.file_delete(folder, name, self._ctx)
            return True
        except Exception:
            return False

    def _rescue_stale_capt(self, download_dir, limit=30):
        """On connect: rescue leftover capt_* objects in '/' (RAM buffer from
        previous crashed sessions) into download_dir, then delete them
        camera-side. Caller must hold _cam_lock."""
        try:
            lst = self._cam.folder_list_files("/", self._ctx)
            names = [lst.get_name(i) for i in range(len(lst))]
        except Exception:
            return 0
        stale = [n for n in names if n.startswith("capt_")][:limit]
        rescued = 0
        for name in stale:
            if download_dir:
                try:
                    os.makedirs(download_dir, exist_ok=True)
                    cf = self._gp.CameraFile()
                    self._cam.file_get("/", name, self._gp.GP_FILE_TYPE_NORMAL, cf, self._ctx)
                    self._save_cam_file(cf, os.path.join(download_dir, os.path.basename(name)))
                    rescued += 1
                except Exception:
                    pass  # delete anyway
            self._file_delete("/", name)
        return rescued

    def _download_with_retries(self, folder, name):
        """file_get with a retry ladder. Returns a CameraFile on success, None.
        Caller must hold _cam_lock.

        Ladder: immediate (RAM buffer volatile, first try best) -> 0.4s backoff
        -> drain(300ms)+retry -> wait_for_event FILE_ADDED (~3s) for an SD-card
        copy (LAST resort: wait_for_event invalidates the RAM buffer)."""
        gp = self._gp
        for attempt in ("immediate", "backoff", "drain"):
            if attempt == "backoff":
                time.sleep(0.4)
            elif attempt == "drain":
                self._drain_events(300)
            try:
                cf = gp.CameraFile()
                self._cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL, cf, self._ctx)
                return cf
            except Exception:
                continue
        # Last resort: SD-card copy via FILE_ADDED event
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                ev = self._cam.wait_for_event(700, self._ctx)
                if ev[0] == gp.GP_EVENT_FILE_ADDED:
                    try:
                        cf = gp.CameraFile()
                        self._cam.file_get(ev[1].folder, ev[1].name,
                                           gp.GP_FILE_TYPE_NORMAL, cf, self._ctx)
                        return cf
                    except Exception:
                        return None
        except Exception:
            pass
        return None

    # ── sidecar-mode helpers (Windows) ──
    def _msys2_root(self):
        """Locate the msys64 install root without hardcoded paths.

        Order: MSYS2_ROOT env → derive from GPHOTO2_SIDECAR_PYTHON → common
        install dirs → bash on PATH. A candidate is only accepted when it
        actually contains mingw64\\bin\\python.exe (this excludes Git Bash's
        mingw64, which has no python/libgphoto2). Last resort keeps the old
        C:\\msys64 default so behavior is unchanged when nothing is found.
        """
        def _valid(root):
            return bool(root) and os.path.isfile(
                os.path.join(root, "mingw64", "bin", "python.exe"))

        root = os.environ.get("MSYS2_ROOT")
        if _valid(root):
            return root
        env_py = os.environ.get("GPHOTO2_SIDECAR_PYTHON")
        if env_py:
            # <root>\mingw64\bin\python.exe → strip 3 components
            parts = os.path.normpath(env_py).split(os.sep)
            if len(parts) >= 4:
                cand = os.sep.join(parts[:-3])
                if _valid(cand):
                    return cand
        for cand in (r"C:\msys64", r"D:\msys64", r"C:\tools\msys64"):
            if _valid(cand):
                return cand
        import shutil as _shutil
        bash = _shutil.which("bash")
        if bash:
            parts = os.path.normpath(bash).split(os.sep)
            for up in (2, 3):  # <root>\usr\bin\bash.exe / <root>\bin\bash.exe
                if len(parts) > up:
                    cand = os.sep.join(parts[:-up]) or os.sep
                    if _valid(cand):
                        return cand
        return r"C:\msys64"

    @staticmethod
    def _newest_subdir(parent):
        """First (newest, by name sort) versioned subdir, e.g. libgphoto2\\2.5.34.

        Version-agnostic: msys upgrades bump the version dir (2.5.34 → 2.5.35),
        and a hardcoded pin silently breaks the sidecar (#P2)."""
        try:
            subs = sorted(
                (d for d in os.listdir(parent)
                 if os.path.isdir(os.path.join(parent, d))),
                reverse=True,
            )
        except OSError:
            return None
        return os.path.join(parent, subs[0]) if subs else None

    def _sidecar_python(self):
        env_py = os.environ.get("GPHOTO2_SIDECAR_PYTHON")
        if env_py:
            return env_py
        return os.path.join(self._msys2_root(), "mingw64", "bin", "python.exe")

    def _sidecar_env(self):
        e = dict(os.environ)
        mingw = os.path.join(self._msys2_root(), "mingw64")
        camlibs = (os.environ.get("GPHOTO2_CAMLIBS")
                   or self._newest_subdir(os.path.join(mingw, "lib", "libgphoto2")))
        iolibs = (os.environ.get("GPHOTO2_IOLIBS")
                  or self._newest_subdir(os.path.join(mingw, "lib", "libgphoto2_port")))
        if camlibs:
            e["CAMLIBS"] = camlibs
        if iolibs:
            e["IOLIBS"] = iolibs
        e["PATH"] = os.path.join(mingw, "bin") + os.pathsep + e.get("PATH", "")
        return e

    def _sidecar_script(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gphoto2_sidecar.py")

    def _reader_loop(self, proc, resp_q, frame_q):
        """Demultiplex sidecar stdout: responses -> resp_q, frames -> frame_q,
        everything else recorded/dropped. proc/queues are passed in (not read
        from self) so a stale reader from a previous spawn can never write into
        the NEW sidecar's queues after a respawn."""
        import json as _json
        import queue as _queue
        for line in proc.stdout:
            try:
                msg = _json.loads(line)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "event":
                ev = msg.get("event")
                if ev == "liveview_frame":
                    if frame_q is not None:
                        # latest-only (maxsize=1): if a frame is still waiting, drop it
                        # and replace with the newer one so the consumer never lags.
                        try:
                            frame_q.put_nowait(msg.get("frame"))
                        except _queue.Full:
                            try:
                                frame_q.get_nowait()
                                frame_q.put_nowait(msg.get("frame"))
                            except Exception:
                                pass
                else:
                    # ready / error / future events: record + log, NEVER into
                    # resp_q -- an event consumed as a command response shifts
                    # every later response by one (permanent misalignment).
                    self._last_event = msg
                    if ev == "ready":
                        self._ready_ev.set()
                    elif ev == "error":
                        self._last_error = msg.get("msg") or msg.get("error") or "unknown sidecar error"
                        send_log(f"[gphoto2] sidecar 事件错误: {self._last_error}", "warn")
                continue
            if mtype == "response":
                if resp_q is not None:
                    resp_q.put(msg)
            else:
                # Unknown protocol message: drop it rather than poison resp_q.
                send_log(f"[gphoto2] sidecar 未知消息已丢弃: {str(msg)[:200]}", "warn")

    def _send_cmd(self, cmd, timeout=30):
        import json as _json
        import queue as _queue
        # One command in flight at a time: the sidecar answers in order, so
        # serializing write+wait also guarantees response routing.
        with self._send_lock:
            if not self._proc or not self._proc.stdin:
                return {"ok": False, "error": "sidecar not running"}
            if self._proc.poll() is not None:
                return {"ok": False, "error": f"sidecar exited (code {self._proc.returncode})"}
            try:
                self._proc.stdin.write(_json.dumps(cmd) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                return {"ok": False, "error": "write failed: " + str(e)}
            # Wait for THIS command's response, skipping anything that isn't a
            # protocol response. Defense in depth: even if a stray message ever
            # lands in _resp_q (older reader, respawn race), one pollution must
            # not misalign every later response -- we skip it and keep waiting.
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"ok": False, "error": "timeout"}
                try:
                    resp = self._resp_q.get(timeout=remaining)
                except _queue.Empty:
                    return {"ok": False, "error": "timeout"}
                if isinstance(resp, dict) and resp.get("type") == "response":
                    return resp
                send_log(f"[gphoto2] 跳过非响应消息: {str(resp)[:200]}", "warn")

    def _stop_sidecar(self):
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(2)
            except Exception:
                pass
            self._proc = None

    # ── public API (dispatches by mode) ──
    def list_devices(self):
        if self._use_sidecar:
            return []  # sidecar: detection happens via connect()
        gp = self._ensure_gp()
        if not gp:
            return []
        try:
            ctx = gp.Context()
            result = gp.gp_camera_autodetect(ctx)
            count, cam_list = result[0], result[1]
            return [{"id": cam_list.get_value(i), "label": cam_list.get_name(i)}
                    for i in range(count)]
        except Exception as e:
            send_log(f"[gphoto2] list_devices failed: {e}", "warn")
            return []

    def connect(self, device_id=None):
        if self._use_sidecar:
            return self._sidecar_connect()
        gp = self._ensure_gp()
        if not gp:
            return False
        try:
            self._ctx = gp.Context()
            # Autodetect FIRST: distinguishes "no camera" (PTPCamera claiming the
            # USB port on Mac, or camera off/wrong mode) from other init failures.
            # On Mac, libgphoto2's USB path is native; unlike Windows there's no
            # Zadig/WinUSB driver issue. The fix is typically:
            #   killall PTPCamera && check USB mode = PC Remote && use a data cable.
            try:
                n_cams = gp.gp_camera_autodetect(self._ctx)[0]
            except Exception:
                n_cams = -1
            if n_cams == 0:
                self._last_error_code = "camera_not_found"
                send_log("[gphoto2] 未检测到相机 (Mac: PTPCamera 可能占用了 USB 端口，"
                         "终端运行 killall PTPCamera；确认相机已开机、USB 设为 PC Remote，"
                         "使用数据线)", "warn")
                return False
            with self._cam_lock:
                self._cam = gp.Camera()
                self._cam.init(self._ctx)
                # Rescue stale RAM-buffer shots from a previous crashed session
                # (and clear them: a cluttered store breaks later downloads).
                rescued = self._rescue_stale_capt(self._download_dir or "")
            self._connected = True
            self._last_error_code = None
            self._caps = {CAP_CAPTURE, CAP_SETTINGS_READ, CAP_SETTINGS_WRITE, CAP_LIVEVIEW}
            if rescued:
                send_log(f"[gphoto2] 从相机缓冲恢复了 {rescued} 张未下载的照片", "info")
            return True
        except Exception as e:
            send_log(f"[gphoto2] connect failed: {e}", "warn")
            self._cam = None
            return False

    def _sidecar_connect(self):
        import subprocess
        import threading
        import queue as _queue
        py = self._sidecar_python()
        script = self._sidecar_script()
        if not os.path.isfile(py):
            send_log(f"[gphoto2] sidecar python not found: {py}", "warn")
            return False
        if not os.path.isfile(script):
            send_log(f"[gphoto2] sidecar script not found: {script}", "warn")
            return False
        # Two attempts: a camera recovering from a wedged session answers init
        # only after long USB timeouts (~25s), so a single short attempt can
        # false-negative right after an unclean shutdown.
        for attempt in (1, 2):
            try:
                self._proc = subprocess.Popen(
                    [py, script],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                    env=self._sidecar_env(), text=True, encoding="utf-8", bufsize=1)
            except Exception as e:
                send_log(f"[gphoto2] sidecar spawn failed: {e}", "warn")
                return False
            self._resp_q = _queue.Queue()
            self._frame_q = _queue.Queue(maxsize=1)  # latest-frame-only (no lag buildup)
            self._ready_ev.clear()
            self._reader = threading.Thread(target=self._reader_loop,
                                            args=(self._proc, self._resp_q, self._frame_q),
                                            daemon=True)
            self._reader.start()
            # The sidecar announces itself with a "ready" EVENT (not a response);
            # the reader records it via _ready_ev.
            self._ready_ev.wait(timeout=10)
            # download_dir lets the sidecar rescue stale RAM-buffer shots on connect
            resp = self._send_cmd({"cmd": "connect",
                                   "download_dir": self._download_dir or ""}, timeout=45)
            if resp.get("ok"):
                self._connected = True
                self._caps = set(resp.get("caps", []))
                self._last_error_code = None
                if resp.get("rescued"):
                    send_log(f"[gphoto2] 从相机缓冲恢复了 {resp['rescued']} 张未下载的照片", "info")
                return True
            err = resp.get("error")
            self._last_error_code = resp.get("error_code")  # e.g. "needs_driver"
            # needs_driver won't resolve on retry (Zadig not yet run) -> skip
            # the 2nd attempt's ~3s wait and surface the actionable signal.
            if self._last_error_code == "needs_driver":
                send_log("[gphoto2] 未检测到相机 - 需用 Zadig 安装 WinUSB 驱动 "
                         "(见驱动安装指南)。已降级为文件夹监听模式。", "warn")
                return False
            send_log(f"[gphoto2] sidecar connect failed (attempt {attempt}/2): {err}", "warn")
            self._stop_sidecar()
            if attempt < 2:
                time.sleep(3)
        send_log("[gphoto2] 相机连接失败 — 若相机之前异常断开，请关机重开（或拔插 USB）后再试", "warn")
        return False

    def disconnect(self):
        if self._use_sidecar:
            if self._proc:
                try:
                    self._send_cmd({"cmd": "disconnect"}, timeout=5)
                except Exception:
                    pass
                self._stop_sidecar()
            self._connected = False
            self._caps = set()
            return
        with self._cam_lock:
            if self._cam and self._ctx:
                try:
                    self._cam.exit(self._ctx)
                except Exception:
                    pass
            self._cam = None
            self._ctx = None
        self._connected = False
        self._caps = set()

    def get_settings(self):
        if not self._connected:
            return None
        if self._use_sidecar:
            resp = self._send_cmd({"cmd": "get_settings"}, timeout=10)
            return resp.get("settings") if resp.get("ok") else None
        if not self._cam:
            return None
        try:
            with self._cam_lock:
                config = self._cam.get_config(self._ctx)

                def _get(name):
                    try:
                        return config.get_child_by_name(name).get_value()
                    except Exception:
                        return None
                return {
                    "aperture": _get("f-number"),
                    "shutter": _get("shutterspeed"),
                    "iso": _get("iso"),
                    "focal_length": None,  # gphoto2 exposes focalposition (focus pos), not mm
                    "exposure_bias": _get("exposurecompensation"),
                }
        except Exception as e:
            send_log(f"[gphoto2] get_settings failed: {e}", "warn")
            return None

    def set_setting(self, key, value):
        m = {"shutter": "shutterspeed", "aperture": "f-number", "iso": "iso",
             "exposure_bias": "exposurecompensation"}
        cname = m.get(key, key)
        if self._use_sidecar:
            if not self._connected:
                return False
            resp = self._send_cmd({"cmd": "set_setting", "name": cname, "value": value}, timeout=10)
            if not resp.get("ok"):
                send_log(f"[gphoto2] set_setting {key}={value} failed: {resp.get('error')}", "warn")
                return False
            if resp.get("applied") is False:
                send_log(f"[gphoto2] set_setting {key}={value}: 相机未应用（档位限制?）", "warn")
            return resp  # {name, value, applied} so callers can surface 档位拒绝
        if not self._cam or not self._gp:
            return False
        gp = self._gp
        try:
            with self._cam_lock:
                cfg = self._cam.get_config(self._ctx)
                w = cfg.get_child_by_name(cname)
                t = w.get_type()
                if t in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU, gp.GP_WIDGET_TEXT):
                    w.set_value(str(value))
                elif t == gp.GP_WIDGET_RANGE:
                    w.set_value(float(value))
                elif t == gp.GP_WIDGET_TOGGLE:
                    w.set_value(int(value))
                else:
                    w.set_value(value)
                self._cam.set_config(cfg, self._ctx)
                # The camera applies config changes asynchronously -- an immediate
                # read-back can return the OLD value (verified A7M4). Poll briefly
                # and report whether the value took, so the UI can explain
                # "camera ignored this" (e.g. aperture in shutter-priority mode).
                actual = None
                applied = False
                for _ in range(5):
                    time.sleep(0.4)
                    actual = self._cam.get_config(self._ctx).get_child_by_name(cname).get_value()
                    if str(actual) == str(value):
                        applied = True
                        break
            resp = {"name": cname, "value": actual, "applied": applied}
            if not applied:
                resp["note"] = "相机未应用该设置（可能当前拍摄档位不允许，如快门优先时光圈由相机控制）"
            return resp
        except Exception as e:
            send_log(f"[gphoto2] set_setting {key}={value} failed: {e}", "warn")
            return False

    def get_setting_choices(self):
        """Valid choices per adjustable setting, for UI dropdowns.
        Returns {f-number: [...]|None, shutterspeed: [...]|None, iso: ...,
        exposurecompensation: ...} or None when the backend can't provide them."""
        names = ("f-number", "shutterspeed", "iso", "exposurecompensation")
        if self._use_sidecar:
            if not self._connected:
                return None
            resp = self._send_cmd({"cmd": "get_setting_choices"}, timeout=10)
            return resp.get("choices") if resp.get("ok") else None
        gp = self._gp
        if not self._cam or not gp:
            return None
        try:
            with self._cam_lock:
                cfg = self._cam.get_config(self._ctx)
                out = {}
                for name in names:
                    try:
                        w = cfg.get_child_by_name(name)
                        if w.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                            out[name] = [w.get_choice(i) for i in range(w.count_choices())]
                        else:
                            out[name] = None
                    except Exception:
                        out[name] = None
            return out
        except Exception as e:
            send_log(f"[gphoto2] get_setting_choices failed: {e}", "warn")
            return None

    def _stderr_dbg(self, msg):
        """Surface a debug line via stderr -- python_service prints stderr to the
        console log, unlike send_log (which goes to the renderer as an event)."""
        try:
            sys.stderr.write(str(msg) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def capture(self):
        if not self._connected:
            self._stderr_dbg("[gphoto2] capture: NOT CONNECTED (sidecar down?)")
            return None
        if self._use_sidecar:
            # Capture involves stop-liveview + re-init + download retries in the
            # sidecar; 60s covers worst-case USB timeouts on a recovering camera.
            resp = self._send_cmd({"cmd": "capture", "download_dir": self._download_dir or ""}, timeout=60)
            if resp.get("error") == "timeout":
                # The sidecar is stuck inside a blocking camera call (seen on
                # A7M4 when capture cycles ran back-to-back; pacing in the
                # sidecar now prevents it). Only a kill+respawn can recover.
                self._stderr_dbg("[gphoto2] capture: sidecar unresponsive, restarting it")
                send_log("[gphoto2] 相机无响应，正在重启相机连接…", "warn")
                self._stop_sidecar()
                self._connected = False
                self._caps = set()
                if self._sidecar_connect():
                    resp = self._send_cmd({"cmd": "capture", "download_dir": self._download_dir or ""}, timeout=60)
                else:
                    return {"error": "相机连接中断且重连失败 — 请关机重开相机（或拔插 USB）后重新开始联机"}
            if resp.get("ok") and resp.get("path"):
                return {"path": resp["path"], "name": resp.get("name", "")}
            err = resp.get("error") or ("ok but no path (download_dir empty?)" if resp.get("ok") else "sidecar capture error")
            self._stderr_dbg(f"[gphoto2] capture failed: {err} | resp={resp}")
            send_log(f"[gphoto2] capture failed: {err}", "error")
            return {"error": err}
        if not self._cam:
            return None
        gp = self._gp
        # Pacing: enforce a minimum quiet interval between capture cycles --
        # back-to-back cycles can hang the camera session (verified A7M4).
        gap = time.time() - self._last_capture_end
        if gap < self._MIN_CAPTURE_INTERVAL:
            time.sleep(self._MIN_CAPTURE_INTERVAL - gap)
        try:
            # Hold the lock for the WHOLE capture cycle: a liveview frame
            # (capture_preview) slipping in between trigger and file_get breaks
            # the volatile RAM-buffer download. Preview just pauses briefly,
            # like a phone camera freezing for the shot.
            with self._cam_lock:
                self._drain_events(300)
                try:
                    path = self._cam.capture(gp.GP_CAPTURE_IMAGE, self._ctx)
                except Exception:
                    self._drain_events(500)
                    path = self._cam.capture(gp.GP_CAPTURE_IMAGE, self._ctx)
                if not self._download_dir:
                    return {"name": path.name, "folder": path.folder}
                # file_get MUST be immediate: the tethered shot lives in a
                # volatile RAM buffer; waiting first invalidates it -> [-1].
                cf = self._download_with_retries(path.folder, path.name)
                if cf is None:
                    # Remove the stale object so it doesn't poison later captures.
                    self._file_delete(path.folder, path.name)
                    return {"error": "download %s/%s: [-1] 多次重试均失败 — "
                                     "照片可能仍在相机缓冲中。若反复出现，请关机重开相机（或拔插 USB）"
                                     % (path.folder, path.name)}
                os.makedirs(self._download_dir, exist_ok=True)
                local = os.path.join(self._download_dir, os.path.basename(path.name))
                self._save_cam_file(cf, local)  # Unicode-safe (no gp_file_save)
                self._file_delete(path.folder, path.name)  # keep RAM buffer clean
                return {"path": local, "name": path.name}
        except Exception as e:
            send_log(f"[gphoto2] capture failed: {e}", "error")
            return {"error": str(e)}
        finally:
            self._last_capture_end = time.time()

    def liveview_start(self):
        if not self._connected:
            return
        if self._use_sidecar:
            self._send_cmd({"cmd": "liveview_start"}, timeout=10)
        # direct mode: no-op (liveview_frame polls capture_preview)

    def liveview_stop(self):
        if self._use_sidecar and self._connected:
            self._send_cmd({"cmd": "liveview_stop"}, timeout=5)

    def liveview_frame(self):
        if not self._connected:
            return None
        if self._use_sidecar:
            import queue as _queue
            import base64 as _b64
            if not self._frame_q:
                return None
            try:
                b64 = self._frame_q.get(timeout=1.0)
                return _b64.b64decode(b64)
            except _queue.Empty:
                return None
        if not self._cam:
            return None
        gp = self._gp
        try:
            with self._cam_lock:
                cam_file = gp.CameraFile()
                self._cam.capture_preview(cam_file, self._ctx)
                return cam_file.get_data_and_size()
        except Exception as e:
            send_log(f"[gphoto2] liveview_frame failed: {e}", "warn")
            return None

    def capabilities(self):
        return set(self._caps) if self._connected else set()

    def is_connected(self):
        return self._connected

    def last_error_code(self):
        """Structured code from the last connect() failure (e.g. 'needs_driver'),
        or None on success / no attempt. Lets the UI give actionable guidance
        (point to the Zadig/WinUSB guide) instead of a generic 'connect failed'."""
        return self._last_error_code


# ── Router ──

# Priority: deeper-control backends first; folder-watch is always last (fallback).
_BACKEND_PRIORITY = ["gphoto2", "digicamcontrol", "folder_watch"]


class CameraRouter:
    """Selects the best available backend for a folder + needed capabilities.

    FolderWatchBackend is always instantiated as the ultimate fallback, so a
    connected camera is NEVER unusable - worst case it falls back to pure
    folder-watch + post-capture EXIF.

    Priority: gphoto2 (full control) > digicamcontrol (Windows live settings)
    > folder_watch (universal baseline).
    """
    def __init__(self, folder, digicamcontrol_url=None, download_dir=None):
        self._folder = folder
        # Deeper-control first; folder-watch last as the guaranteed fallback.
        # gphoto2 needs a download_dir so capture() drops files into the watched
        # folder for scoring. Defaults to the tether folder.
        self._backends = [
            GPhoto2Backend(download_dir=download_dir or folder),
            DigiCamControlBackend(base_url=digicamcontrol_url),
            FolderWatchBackend(),
        ]
        self._active = None

    def select(self, needed_caps=None):
        """Pick the first backend (by priority) that connects and offers all
        needed_caps. Returns the chosen backend; always falls back to
        FolderWatchBackend so the result is never None."""
        needed = set(needed_caps or [])
        for b in self._backends:
            try:
                if b.connect(self._folder) and needed.issubset(b.capabilities()):
                    self._active = b
                    send_log(f"[camera_backend] selected backend: {b.name} "
                             f"(caps={sorted(b.capabilities())})", "info")
                    return b
            except Exception as e:
                send_log(f"[camera_backend] {b.name} connect failed: {e}", "warn")

        # Fallback: folder-watch (connects with just the folder; always works)
        fw = self._backends[-1]  # FolderWatchBackend
        try:
            fw.connect(self._folder)
        except Exception:
            pass
        self._active = fw
        send_log(f"[camera_backend] fallback to {fw.name}", "info")
        return fw

    def active(self):
        """The backend chosen by the last select(); may be None before select()."""
        return self._active

    def get_settings(self):
        """Convenience: active backend's live settings, or None (coach uses EXIF)."""
        b = self._active
        if not b:
            return None
        try:
            return b.get_settings()
        except Exception:
            return None

    def needs_driver(self):
        """True if the gphoto2 backend failed specifically because no camera was
        visible to libgphoto2 (Windows: WinUSB not installed via Zadig). When
        True, select() has fallen back to folder-watch; the UI should point the
        user to the driver-install guide rather than report a generic failure."""
        for b in self._backends:
            if getattr(b, "name", "") == "gphoto2" and \
               getattr(b, "_last_error_code", None) == "needs_driver":
                return True
        return False

    def gphoto2_error_code(self):
        """Structured error code from the gphoto2 backend's last connect() failure,
        or None on success / no attempt. 'needs_driver' = Windows (Zadig/WinUSB),
        'camera_not_found' = Mac (PTPCamera/PC Remote/data cable)."""
        for b in self._backends:
            if getattr(b, "name", "") == "gphoto2":
                return getattr(b, "_last_error_code", None)
        return None
