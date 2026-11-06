"""
PhotoS - REST API Server (stdlib only)

Serves a small JSON HTTP API so external tools / AI agents can drive
PhotoS over the network. Defaults to binding 127.0.0.1 (local only).

Endpoints:
  GET  /health         → {"status": "ok", "version": ...}
  GET  /info           → supported formats, writable formats, installed plugins
  POST /process        → {"paths": [...], "options": {...}} → batch result JSON
                         {"async": true} → 202 {"task_id", ...} background task
                         {"dry_run": true} → paths/options without processing
  GET  /tasks          → list of running/finished task summaries
  GET  /tasks/<id>     → task progress {status, current, total, current_path, result?}
  POST /tasks/<id>/cancel → request cancellation (in-flight images finish)
  POST /dedup          → {"paths": [...], "threshold": 5}    → duplicate groups
  POST /rename         → {"paths": [...], "pattern": ..., "output_dir"?,
                           "overwrite"?, "dry_run"?}         → rename results
  POST /contact-sheet  → {"paths": [...], "output": ..., "cols"?, "thumb_width"?,
                           "thumb_height"?, "captions"?, "bg"?} → output path
  POST /check          → {"paths": [...]}                    → integrity report

Security: POST /process reads and writes arbitrary paths on this machine.
Only expose it on a trusted local network; use --token for Bearer auth.
"""

import json
import os
import secrets
import threading

class _ClientGone(Exception):
    """SSE client disconnected mid-stream (BrokenPipe/Reset)."""


_INVALID_BODY = object()  # _read_json sentinel: body was not valid JSON


import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

from . import __version__
from .engine import (
    ProcessOptions,
    batch_process,
    scan_directory,
    _canonical_format,
    SUPPORTED_FORMATS,
    PIL_WRITABLE,
    ALL_INPUT_EXTENSIONS,
)

VERSION = __version__

# Max accepted request body for JSON endpoints (guards _read_json against
# unbounded Content-Length memory exhaustion).
MAX_BODY_BYTES = 1_000_000

# Hosts accepted when no token is configured. DNS-rebinding attacks make a
# browser resolve attacker.com → 127.0.0.1 and send Host: attacker.com, so
# only loopback / the actual bound address may claim the request.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _scalar_groups():
    """Derive int/float/str/bool field groups from ProcessOptions annotations.

    Any new scalar field on ProcessOptions is automatically exposed via the
    JSON API — no hand-maintained whitelist to keep in sync (the old hardcoded
    tuples drifted and silently dropped fields like output_sizes / pad).
    Non-scalar fields (list, e.g. output_sizes) are handled explicitly below.
    """
    from dataclasses import fields as _fields
    from typing import get_args, get_origin
    ints, floats, strs, bools = set(), set(), set(), set()
    for f in _fields(ProcessOptions):
        origin = get_origin(f.type)
        if origin is None:
            base = getattr(f.type, "__name__", None) or str(f.type)
        else:
            # Optional[X] → origin is Union; the underlying scalar is args[0]
            args = get_args(f.type)
            base = (getattr(args[0], "__name__", None) or str(args[0])) if args else ""
        if base == "int":
            ints.add(f.name)
        elif base == "float":
            floats.add(f.name)
        elif base == "str":
            strs.add(f.name)
        elif base == "bool":
            bools.add(f.name)
    return (sorted(ints), sorted(floats), sorted(strs), sorted(bools))


_INT_FIELDS, _FLOAT_FIELDS, _STR_FIELDS, _BOOL_FIELDS = _scalar_groups()


def _options_from_dict(data: dict, base: Optional[ProcessOptions] = None) -> ProcessOptions:
    """Build ProcessOptions from a JSON options dict (unknown keys ignored).

    Starts from ``base`` (server defaults) if given, then applies overrides.
    Raises ValueError for invalid output formats / filenames that could
    escape the output directory (prefix/suffix traversal).
    """
    from .engine import _has_path_traversal
    opts = replace(base) if base is not None else ProcessOptions()
    for key in _INT_FIELDS:
        if key in data and data[key] is not None:
            try:
                setattr(opts, key, int(data[key]))
            except (TypeError, ValueError):
                pass
    for key in _FLOAT_FIELDS:
        if key in data and data[key] is not None:
            try:
                setattr(opts, key, float(data[key]))
            except (TypeError, ValueError):
                pass
    for key in _STR_FIELDS:
        if key in data and data[key] is not None:
            setattr(opts, key, str(data[key]))
    for key in _BOOL_FIELDS:
        if key in data and data[key] is not None:
            setattr(opts, key, bool(data[key]))
    # sane numeric ranges — a quality of 0 / 100000 from a malformed request
    # used to flow straight into every save call
    opts.quality = max(1, min(100, int(opts.quality)))
    if opts.scale_percent is not None:
        opts.scale_percent = max(1, min(100, int(opts.scale_percent)))
    opts.watermark_opacity = max(0, min(100, int(opts.watermark_opacity)))
    opts.jobs = max(1, min(64, int(opts.jobs)))
    # a prefix/suffix like "/../../pwned" is joined into output filenames —
    # reject it here (400) instead of failing per-file deep in the engine
    for field in ("prefix", "suffix"):
        value = getattr(opts, field) or ""
        if _has_path_traversal(value):
            raise ValueError(
                f"options.{field} must not contain path separators or "
                "traversal segments")
    # case-insensitive format ("png" / "WEBP" → canonical "PNG" / "WebP")
    opts.output_format = _canonical_format(opts.output_format)
    if opts.output_format not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported output format {opts.output_format!r}; "
            f"supported: {sorted(SUPPORTED_FORMATS)}")
    if data.get("target_size"):
        from .config import _parse_size
        size = _parse_size(data["target_size"])
        if size is not None:
            opts.target_size_bytes = size
    # config-style alias: "pad" → pad_ratio (same key the CLI/config use)
    if data.get("pad"):
        opts.pad_ratio = str(data["pad"])
    # multi-size: accept list of [label, w, h] tuples or dicts
    # e.g. [["thumb", 480, None], {"label": "screen", "width": 1920}]
    sizes = data.get("output_sizes")
    if sizes is not None:
        parsed_sizes = []
        for entry in sizes:
            if isinstance(entry, dict):
                label = str(entry.get("label", "") or "")
                mw = entry.get("width") or entry.get("max_width")
                mh = entry.get("height") or entry.get("max_height")
            elif isinstance(entry, (list, tuple)):
                parts = list(entry)
                label = str(parts[0]) if parts else ""
                mw = parts[1] if len(parts) > 1 else None
                mh = parts[2] if len(parts) > 2 else None
            else:
                continue  # skip non-dict/non-sequence entries (e.g. 42)
            try:
                mw = int(mw) if mw else None
            except (TypeError, ValueError):
                mw = None
            try:
                mh = int(mh) if mh else None
            except (TypeError, ValueError):
                mh = None
            if label:
                parsed_sizes.append((label, mw, mh))
        if parsed_sizes:
            opts.output_sizes = parsed_sizes
    return opts


def _resolve_paths(paths: List[str], recursive: bool = False) -> List[str]:
    """Expand a list of file/dir paths into supported image files.

    Directories are scanned shallowly by default; pass ``recursive=True``
    (the request's "recursive" field) to include subdirectories.
    """
    result = set()
    for p in paths:
        if os.path.isdir(p):
            result.update(scan_directory(p, recursive=recursive))
        elif os.path.isfile(p) and Path(p).suffix.lower() in ALL_INPUT_EXTENSIONS:
            result.add(os.path.abspath(p))
    return sorted(result)


# ── Async task store (POST /process with "async": true) ─────────────────
# Agents start a batch and poll GET /tasks/<id> for progress/result instead
# of holding a single HTTP request open for a long batch. Cancel is honored
# via the engine's cancel_checker (in-flight images finish; pending ones
# are drained as "Cancelled").

_TASKS_LOCK = threading.Lock()
_TASKS = {}          # task_id → {"state": dict, "cancel": threading.Event}
_MAX_TASKS = 100
_TERMINAL = ("done", "error", "cancelled")


def _prune_tasks() -> None:
    """Drop finished tasks beyond the cap (oldest finished first)."""
    if len(_TASKS) <= _MAX_TASKS:
        return
    finished = sorted(
        (t for t in _TASKS.values() if t["state"]["status"] in _TERMINAL),
        key=lambda t: t["state"].get("finished_at", 0),
    )
    for t in finished[: len(_TASKS) - _MAX_TASKS]:
        _TASKS.pop(t["state"]["task_id"], None)


# Concurrent background batches. Unbounded task threads meant one request
# flood could spawn unlimited workers (each holding CPU + memory).
_MAX_RUNNING_TASKS = max(2, (os.cpu_count() or 2) * 2)


def start_task(paths: List[str], options: ProcessOptions,
               dry_run: bool = False,
               audit: bool = False,
               aesthetic=None) -> str:
    """Start a /process batch in the background; return the task_id.

    The agent polls GET /tasks/<id> for progress and result, and may
    cancel via POST /tasks/<id>/cancel. Raises RuntimeError when too many
    tasks are already running (callers translate that to 503).

    ``audit=True`` (v2.3) runs the quality gate over the batch OUTPUTS when
    processing finishes and attaches per-file ``{passed, reason}`` plus an
    overall pass rate to the task result — the stop condition lives inside
    the task instead of a separate /audit round-trip.
    """
    task_id = secrets.token_urlsafe(12)
    cancel = threading.Event()
    state = {
        "task_id": task_id,
        "status": "running",
        "current": 0,
        "total": len(paths),
        "current_path": "",
        "result": None,
    }

    def progress(current, total, path, *extra):
        state["current"] = current
        state["total"] = total
        state["current_path"] = path or ""

    def _audit_outputs(result_dict: dict) -> dict:
        from .audit import audit_image
        verifier = None
        if aesthetic is not None:
            from .plugin import find_provider
            verifier = find_provider("verify")
        audited = 0
        passed = 0
        for row in result_dict.get("results", []):
            out = row.get("output") or ""
            if row.get("status") != "ok" or not out \
                    or not os.path.exists(out):
                continue
            # aesthetic gate with no plugin raises RuntimeError — the run()
            # wrapper turns it into an error task (agent sees the misconfig)
            a = audit_image(out, aesthetic=aesthetic, verifier=verifier)
            row["audit"] = {
                "passed": bool(a.get("passed")),
                "reason": a.get("reason", ""),
            }
            audited += 1
            passed += 1 if a.get("passed") else 0
        result_dict["audit_summary"] = {
            "audited": audited, "passed": passed,
            "failed": audited - passed,
            "pass_rate": round(passed / audited, 3) if audited else None,
        }
        return result_dict

    def run():
        try:
            if dry_run:
                state["result"] = {"dry_run": True, "count": len(paths)}
            else:
                result = batch_process(
                    paths, options,
                    progress_callback=progress,
                    cancel_checker=lambda: cancel.is_set(),
                )
                result_dict = result.to_dict()
                if audit and not cancel.is_set():
                    result_dict = _audit_outputs(result_dict)
                state["result"] = result_dict
            state["status"] = "cancelled" if cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001 — report to the polling agent
            state["result"] = {"error": type(e).__name__}
            state["status"] = "error"
        finally:
            state["finished_at"] = time.time()
            # Self-prune: terminal tasks beyond the cap are dropped here too,
            # so /tasks listing never grows without bound even without polls.
            with _TASKS_LOCK:
                _prune_tasks()

    with _TASKS_LOCK:
        running = sum(1 for t in _TASKS.values()
                      if t["state"]["status"] == "running")
        if running >= _MAX_RUNNING_TASKS:
            raise RuntimeError(
                f"too many running tasks ({running}); retry when a batch "
                "finishes or cancel one via POST /tasks/<id>/cancel")
        _prune_tasks()
        _TASKS[task_id] = {"state": state, "cancel": cancel}
    threading.Thread(target=run, daemon=True).start()
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    """Return a snapshot of a task's state (without the cancel event).

    The copy is taken under the store lock — handing out the live dict let
    the worker thread mutate it mid-serialization
    ("dictionary changed size during iteration").
    """
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task["state"]) if task else None


class _PhotoSHandler(BaseHTTPRequestHandler):
    """HTTP handler. options/token are class attrs set by create_server."""

    options = ProcessOptions()
    token: Optional[str] = None

    # Drop connections that stop sending mid-request (slowloris-style thread
    # starvation against the thread-per-connection model).
    timeout = 60

    def log_message(self, fmt, *args):  # silence default request logging
        pass

    # ── helpers ──────────────────────────────────────────────────────────
    def _send_json(self, status: int, payload: dict):
        from .contract import versioned
        body = json.dumps(versioned(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_host(self) -> str:
        """The Host header's hostname (port stripped), lowercased."""
        host = (self.headers.get("Host", "") or "").strip().lower()
        # strip port: IPv6 [::1]:port vs bare host:port
        if host.startswith("["):
            host = host.split("]")[0] + "]"
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return host

    def _host_allowed(self) -> bool:
        """Whether the request's Host header matches loopback / the bind address.

        DNS-rebinding: an attacker's page fetches http://attacker.com which
        the browser resolves to 127.0.0.1, but the Host header stays
        attacker.com — so requiring Host ∈ {loopback, bound-address} blocks it.
        """
        host = self._request_host()
        if host in _LOOPBACK_HOSTS:
            return True
        bound = getattr(self.server, "server_address", None)
        if bound:
            bound_host = bound[0]
            if bound_host in ("", "0.0.0.0", "::"):
                # wildcard bind: any Host would pass — require a token instead
                return False
            if host == bound_host.lower():
                return True
        return False

    def _authed(self) -> bool:
        if self.token:
            # constant-time compare: a plain == leaks the token length/prefix
            # through response timing
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {self.token}"
            return (len(supplied) == len(expected)
                    and secrets.compare_digest(supplied, expected))
        # No token configured: still block browser cross-origin requests
        # (localhost CSRF drive-by) AND DNS-rebinding (Host must be loopback /
        # the actual bound address, not a rebinding hostname). A malicious
        # page can send a CORS "simple request" (text/plain JSON, no
        # preflight) to 127.0.0.1 and it would otherwise be fully processed.
        # Browsers always reveal their origin on fetch/XHR; CLI/curl/agent
        # clients send no Origin header and are unaffected.
        if not self._host_allowed():
            return False
        origin = self.headers.get("Origin")
        if origin:
            bound = getattr(self.server, "server_address", None)
            bound_host = bound[0] if bound else ""
            bound_port = bound[1] if bound else None
            # scheme must match the actual wire protocol — a TLS deployment
            # would otherwise reject every legitimate browser origin
            scheme = "https" if os.environ.get("PHOTO_S_TLS") else "http"
            expected = f"{scheme}://{bound_host}"
            if bound_port:
                expected += f":{bound_port}"
            return origin == expected
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # Drain (discard) the body so the connection closes cleanly — a
            # socket closed with unread received data sends RST and the client
            # loses the 413 response. We stream-and-drop, never buffering.
            _remaining = length
            try:
                while _remaining > 0:
                    chunk = self.rfile.read(min(_remaining, 65536))
                    if not chunk:
                        break
                    _remaining -= len(chunk)
            except OSError:
                pass
            self.close_connection = True
            self._send_json(413, {"error": "payload too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            # A malformed body must NOT surface later as a misleading
            # "no supported image files found" ({} was indistinguishable
            # from an empty payload).
            return _INVALID_BODY
        if not isinstance(body, dict):
            return _INVALID_BODY
        return body

    def _handle_process_stream(self, data: dict):
        """SSE endpoint: run a batch, stream one ``data:`` frame per file.

        Each progress event is ``data: {"current","total","path"}``; the final
        frame is ``data: {"status":"done","result":{...}}`` (or an error
        frame). The connection closes when the batch finishes, so clients
        read until EOF instead of polling ``/tasks/<id>``.
        """
        from .engine import batch_process
        paths = _resolve_paths(data.get("paths", []),
                               bool(data.get("recursive", False)))
        if not paths:
            self._send_json(400, {"error": "no supported image files found"})
            return
        opts = _options_from_dict(data.get("options", {}), self.options)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        gone = threading.Event()
        frame_lock = threading.Lock()
        stop_heartbeat = threading.Event()

        def _frame(payload: dict):
            try:
                with frame_lock:
                    self.wfile.write(
                        f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                gone.set()  # cancel the batch, not just this frame
                raise _ClientGone

        def _heartbeat():
            # Comment frames keep proxies from idling out and surface dead
            # clients during long single-file stages (RAW decode) that emit
            # no progress events.
            while not stop_heartbeat.wait(15.0):
                try:
                    with frame_lock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    gone.set()
                    return

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

        def progress(current, total, path, *extra):
            _frame({"current": current, "total": total, "path": path or ""})

        try:
            result = batch_process(
                paths, opts, progress_callback=progress,
                # a disconnected client cancels the batch: pending files are
                # skipped instead of grinding through the whole list
                cancel_checker=lambda: gone.is_set())
            _frame({"status": "done", "result": result.to_dict()})
        except _ClientGone:
            return  # client disconnected mid-stream — batch already cancelled
        except Exception as e:  # noqa: BLE001 — report to the streaming agent
            try:
                _frame({"status": "error", "error": type(e).__name__})
            except _ClientGone:
                pass
        finally:
            stop_heartbeat.set()

    # ── routes ───────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._authed():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            self._dispatch_get()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # noqa: BLE001 — never kill the connection
            # no str(e): internal paths / state must not leak to clients
            print(f"server error in GET {self.path}: "
                  f"{type(e).__name__}: {e}", file=os.sys.stderr)
            try:
                self._send_json(500, {"ok": False,
                                      "error": f"internal error ({type(e).__name__})"})
            except OSError:
                pass

    def _dispatch_get(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "version": VERSION})
        elif self.path == "/info":
            from .plugin import discover_plugins
            self._send_json(200, {
                "version": VERSION,
                "formats": sorted(SUPPORTED_FORMATS),
                "writable": sorted(PIL_WRITABLE),
                "input_extensions": sorted(ALL_INPUT_EXTENSIONS),
                "plugins": [p.name for p in discover_plugins()],
            })
        elif self.path == "/plugins":
            from .registry import OFFICIAL_PLUGINS, to_dict
            from .plugin import discover_plugins
            from .plugincmd import _installed_version
            installed_objs = {}
            for p in discover_plugins():
                installed_objs[p.name] = p
            installed = []
            for name, p in installed_objs.items():
                installed.append({
                    "name": name,
                    "provides": list(getattr(p, "provides", ())),
                    "version": _installed_version("photo-s-plugin-" + name),
                })
            available = []
            for name, official in OFFICIAL_PLUGINS.items():
                entry = to_dict(official)
                entry["installed"] = name in installed_objs
                available.append(entry)
            self._send_json(200, {"installed": installed,
                                  "available": available})
        elif self.path == "/tasks":
            with _TASKS_LOCK:
                tasks = [{
                    "task_id": t["state"]["task_id"],
                    "status": t["state"]["status"],
                    "current": t["state"]["current"],
                    "total": t["state"]["total"],
                } for t in _TASKS.values()]
            self._send_json(200, {"tasks": tasks})
        elif self.path.startswith("/tasks/"):
            state = get_task(self.path[len("/tasks/"):])
            if state is None:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, state)
        elif self.path.startswith("/v1/autopilot/"):
            # v2.5 无人值守闭环：与 MCP autopilot_* 同一注册表（函数级导入
            # 避免 server <-> mcp_server 环）
            from .mcp_server import autopilot_status_tool
            aid = self.path[len("/v1/autopilot/"):].strip("/")
            if not aid:
                self._send_json(404, {"error": "autopilot id required"})
                return
            payload = autopilot_status_tool(aid)
            if not payload.get("ok"):
                self._send_json(404, payload)
                return
            self._send_json(200, payload)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            self._send_json(401, {"error": "unauthorized"})
            return
        data = self._read_json()
        if data is None:  # payload too large — 413 already sent
            return
        if data is _INVALID_BODY:
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        try:
            self._dispatch_post(data)
        except Exception as e:  # noqa: BLE001 — a bad request must not kill
            # the connection with an empty reply; report it as a JSON 500.
            # no str(e) in the response — internal paths must not leak
            print(f"server error in POST {self.path}: "
                  f"{type(e).__name__}: {e}", file=os.sys.stderr)
            try:
                self._send_json(500, {"ok": False,
                                      "error": f"internal error ({type(e).__name__})"})
            except OSError:
                pass

    def _dispatch_post(self, data: dict):
        if self.path == "/process/stream":
            self._handle_process_stream(data)
        elif self.path == "/analyze":
            # Perceptual feedback loop: agents analyze -> adjust -> process.
            from .metrics import analyze_image
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            if not paths:
                self._send_json(400, {"error": "no supported image files found"})
                return
            try:
                sample_size = int(data.get("sample_size", 256))
            except (TypeError, ValueError):
                sample_size = 256
            sample_size = max(16, min(2048, sample_size))
            grid = int(data.get("grid", 0) or 0)
            if grid not in (4, 8):
                grid = 0
            results = [analyze_image(p, sample_size=sample_size, grid=grid)
                       for p in paths]
            self._send_json(200, {"ok": all(r.get("ok") for r in results),
                                  "count": len(results),
                                  "results": results})
        elif self.path == "/diff":
            # Numeric before/after comparison (PSNR/SSIM/MAD).
            from .metrics import compare_images
            path_a = data.get("path_a", "")
            path_b = data.get("path_b", "")
            if not path_a or not path_b or not os.path.exists(path_a) \
                    or not os.path.exists(path_b):
                self._send_json(400, {"error": "path_a and path_b required"})
                return
            try:
                sample_size = int(data.get("sample_size", 256))
            except (TypeError, ValueError):
                sample_size = 256
            self._send_json(200, compare_images(
                path_a, path_b, sample_size=max(16, min(1024, sample_size))))
        elif self.path == "/audit":
            # Quality gate: pass/fail + reasons (agent stop condition).
            from .audit import audit_image
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            if not paths:
                self._send_json(400, {"error": "no supported image files found"})
                return
            thresholds = {k: data[k] for k in
                          ("overexposed_max", "underexposed_max", "blur_min")
                          if k in data and isinstance(data[k], (int, float))}
            aesthetic = data.get("aesthetic")
            verifier = None
            if isinstance(aesthetic, (int, float)):
                from .plugin import find_provider
                verifier = find_provider("verify")
            results = [audit_image(p, aesthetic=aesthetic, verifier=verifier,
                                   **thresholds) for p in paths]
            self._send_json(200, {
                "ok": True,
                "count": len(results),
                "passed": sum(1 for r in results if r.get("passed")),
                "results": results,
            })
        elif self.path == "/v1/suggest":
            # Rule-based suggestions: analyze stats → conservative params.
            # The bridge between /analyze and /process in the grading loop.
            from .suggest import suggest_file
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            if not paths:
                self._send_json(400, {"error": "no supported image files found"})
                return
            try:
                scale = float(data.get("scale", 1.0))
            except (TypeError, ValueError):
                scale = 1.0
            scale = max(0.0, min(1.0, scale))
            results = [suggest_file(p, scale=scale) for p in paths]
            self._send_json(200, {
                "ok": all(r.get("ok") for r in results),
                "count": len(results),
                "neutral": sum(1 for r in results if r.get("neutral")),
                "results": results,
            })
        elif self.path == "/preview":
            # Visual snapshot: downscaled JPEG + histogram PNG (base64).
            from .metrics import snapshot_image
            path = data.get("path", "")
            if not path or not os.path.exists(path):
                self._send_json(400, {"error": "path required"})
                return
            try:
                max_dim = int(data.get("max_dim", 1024))
            except (TypeError, ValueError):
                max_dim = 1024
            include_histogram = bool(data.get("include_histogram", True))
            self._send_json(200, snapshot_image(
                path, max_dim=max(64, min(4096, max_dim)),
                include_histogram=include_histogram))
        elif self.path == "/process":
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            if not paths:
                self._send_json(400, {"error": "no supported image files found"})
                return
            try:
                opts = _options_from_dict(data.get("options", {}), self.options)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
            if data.get("async"):
                # Long-running batch → background task; agent polls /tasks/<id>.
                aesthetic = data.get("aesthetic")
                try:
                    task_id = start_task(paths, opts,
                                         dry_run=bool(data.get("dry_run")),
                                         audit=bool(data.get("audit")),
                                         aesthetic=(None if aesthetic is None
                                                    else float(aesthetic)))
                except RuntimeError as e:
                    self._send_json(503, {"ok": False, "error": str(e)})
                    return
                self._send_json(202, {
                    "task_id": task_id,
                    "status": "running",
                    "total": len(paths),
                    "poll": f"/tasks/{task_id}",
                })
                return
            if data.get("dry_run"):
                self._send_json(200, {
                    "dry_run": True,
                    "count": len(paths),
                    "paths": paths,
                    "options": data.get("options", {}),
                })
                return
            result = batch_process(paths, opts)
            self._send_json(200, result.to_dict())
        elif self.path == "/dedup":
            from .dedup import find_duplicates
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            try:
                threshold = int(data.get("threshold", 5))
            except (TypeError, ValueError):
                threshold = 5
            dup = find_duplicates(paths, threshold=threshold)
            self._send_json(200, {
                "count": len(dup),
                "groups": [{"hash": h, "paths": ps} for h, ps in dup.items()],
            })
        elif self.path == "/rename":
            from .rename import rename_files
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            pattern = data.get("pattern") or ""
            if not paths or not pattern:
                self._send_json(400, {"error": "paths and pattern are required"})
                return
            results = rename_files(
                paths, pattern,
                output_dir=data.get("output_dir"),
                overwrite=bool(data.get("overwrite", False)),
                dry_run=bool(data.get("dry_run", False)),
            )
            ok = sum(1 for r in results if r["status"] == "ok")
            self._send_json(200, {"total": len(results), "ok": ok,
                                  "results": results})
        elif self.path == "/contact-sheet":
            from .contact import build_contact_sheet
            from .adjust import hex_to_rgb
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            output = data.get("output") or ""
            if not paths or not output:
                self._send_json(400, {"error": "paths and output are required"})
                return
            try:
                cols = int(data.get("cols", 4))
            except (TypeError, ValueError):
                cols = 4
            cols = max(1, cols)  # 0/negative would crash the grid math
            try:
                tw = int(data.get("thumb_width", 240))
                th = int(data.get("thumb_height", 240))
            except (TypeError, ValueError):
                tw = th = 240
            tw, th = max(1, tw), max(1, th)
            try:
                bg = hex_to_rgb(data.get("bg", "#000000"))
            except ValueError:
                bg = (0, 0, 0)
            out = build_contact_sheet(
                paths, output, cols=cols, thumb_size=(tw, th),
                captions=bool(data.get("captions", False)), bg=bg)
            self._send_json(200, {"output": out, "count": len(paths)})
        elif self.path == "/check":
            from .check import verify_images
            paths = _resolve_paths(data.get("paths", []),
                                   bool(data.get("recursive", False)))
            if not paths:
                self._send_json(400, {"error": "no supported image files found"})
                return
            results = verify_images(paths)
            corrupt = [r for r in results if not r["ok"]]
            self._send_json(200, {
                "checked": len(results),
                "ok": len(results) - len(corrupt),
                "corrupt": [{"path": r["path"], "error": r["error"]}
                            for r in corrupt],
            })
        elif self.path.startswith("/tasks/") and self.path.endswith("/cancel"):
            task_id = self.path[len("/tasks/"):-len("/cancel")]
            with _TASKS_LOCK:
                task = _TASKS.get(task_id)
            if task is None:
                self._send_json(404, {"error": "task not found"})
                return
            task["cancel"].set()
            self._send_json(200, {"task_id": task_id, "cancelled": True})
        elif self.path == "/v1/autopilot":
            # v2.5：启动无人值守管线（watch → suggest/auto-tone → audit →
            # passed/review 分流）；202 + id，GET /v1/autopilot/{id} 轮询
            from .mcp_server import autopilot_start_tool
            if not isinstance(data.get("dir"), str) \
                    or not data.get("dir"):
                self._send_json(400, {"ok": False,
                                      "error": "'dir' is required"})
                return
            def _num(key):
                v = data.get(key)
                return float(v) if v is not None else None
            payload = autopilot_start_tool(
                dir=data["dir"],
                out_dir=data.get("out_dir"),
                mode=str(data.get("mode", "suggest")),
                auto_tone=_num("auto_tone"),
                scale=float(data.get("scale", 1.0) or 1.0),
                aesthetic=_num("aesthetic"),
                overexposed_max=_num("overexposed_max"),
                underexposed_max=_num("underexposed_max"),
                blur_min=_num("blur_min"),
                write_xmp=bool(data.get("write_xmp", False)),
                recursive=bool(data.get("recursive", False)),
                scan_existing=bool(data.get("scan_existing", False)),
                quality=data.get("quality"),
                output_format=data.get("output_format"),
                resize=data.get("resize"),
                timeout=_num("timeout"),
            )
            self._send_json(202 if payload.get("started") else 400, payload)
        elif self.path.startswith("/v1/autopilot/") \
                and self.path.endswith("/cancel"):
            from .mcp_server import autopilot_stop_tool
            aid = self.path[len("/v1/autopilot/"):-len("/cancel")]
            payload = autopilot_stop_tool(aid)
            self._send_json(200 if payload.get("ok") else 404, payload)
        elif self.path == "/v1/index":
            # v2.5 语义搜索：建索引（+可选自动打标），返回索引路径供
            # /v1/find 使用（与 MCP index 工具同一实现）
            from .mcp_server import index_tool
            paths = data.get("paths") or []
            if not isinstance(paths, list) or not paths:
                self._send_json(400, {"ok": False,
                                      "error": "'paths' (list) is required"})
                return
            tags = data.get("tags")
            payload = index_tool(
                paths=paths,
                recursive=bool(data.get("recursive", False)),
                index=data.get("index"),
                rebuild=bool(data.get("rebuild", False)),
                tags=tags if isinstance(tags, list) else None,
                min_score=float(data.get("min_score", 0.2) or 0.2),
                max_tags=int(data.get("max_tags", 5) or 5),
                write_xmp=bool(data.get("write_xmp", False)),
            )
            self._send_json(200 if payload.get("ok") else 400, payload)
        elif self.path == "/v1/find":
            from .mcp_server import find_tool
            payload = find_tool(
                query=data.get("query"),
                image=data.get("image"),
                index=data.get("index"),
                k=int(data.get("k", 10) or 10),
                min_score=data.get("min_score"),
            )
            self._send_json(200 if payload.get("ok") else 400, payload)
        elif self.path == "/plugins":
            # Remote plugin management: {"action": "install|uninstall|fetch",
            # "name": "scunet", "dry_run": bool?}
            from .registry import get_official
            from .plugincmd import _pip_run
            action = data.get("action")
            name = data.get("name")
            if action not in ("install", "uninstall", "fetch"):
                self._send_json(400, {"error": "action must be one of "
                                                "install|uninstall|fetch"})
                return
            if action in ("install", "uninstall"):
                # pip install into this interpreter + auto-import on the next
                # discover() = remote code execution. It now requires BOTH an
                # explicit opt-in env flag AND a token on the server. dry-run
                # (the argv preview) stays open.
                if not os.environ.get("PHOTO_S_ALLOW_REMOTE_PLUGINS"):
                    self._send_json(403, {
                        "ok": False,
                        "error": ("remote plugin install/uninstall is disabled; "
                                  "start the server with a token and set "
                                  "PHOTO_S_ALLOW_REMOTE_PLUGINS=1 to enable")})
                    return
                if not self.token:
                    self._send_json(403, {
                        "ok": False,
                        "error": "remote plugin install requires --token"})
                    return
            official = get_official(name) if name else None
            if action in ("install", "uninstall") and official is None:
                self._send_json(400, {"error": "unknown plugin: {}".format(name)})
                return
            if action == "fetch":
                from .plugin import discover_plugins
                plugin = next((p for p in discover_plugins()
                               if p.name == name), None)
                if plugin is None:
                    self._send_json(400, {"error": "plugin not installed: {}"
                                          .format(name)})
                    return
                try:
                    from .modelstore import ensure
                    weights = [ensure(s) for s in plugin.weight_specs()]
                except RuntimeError as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                    return
                self._send_json(200, {"ok": True, "name": name,
                                      "weights": [{"path": w} for w in weights]})
                return

            dist = official.pypi_distribution
            if data.get("dry_run"):
                argv = ["install", "--quiet", dist] if action == "install" \
                    else ["uninstall", "-y", dist]
                self._send_json(200, {"ok": True, "name": name,
                                      "dry_run": True, "pip_argv": argv})
                return
            try:
                proc = _pip_run(["install", "--quiet", dist] if action == "install"
                                else ["uninstall", "-y", dist])
            except FileNotFoundError:
                self._send_json(500, {"ok": False,
                                      "error": "pip not available"})
                return
            if proc.returncode != 0:
                self._send_json(500, {"ok": False,
                                      "error": "pip {} failed: {}".format(
                                          action, (proc.stderr or "").strip()[-300:])})
                return
            self._send_json(200, {"ok": True, "name": name,
                                  "action": action, "distribution": dist})
        else:
            self._send_json(404, {"error": "not found"})


def generate_token() -> str:
    """Generate a random Bearer token for --token auto."""
    return secrets.token_urlsafe(24)


def write_ready_file(path: str, port: int, token: Optional[str]) -> None:
    """Atomically write the server handshake file for host agents.

    JSON payload: {"port", "token", "pid"}. The agent polls for this file
    instead of parsing stdout (robust on Windows where stdout encoding and
    buffering are unreliable). The temp file is created 0600 from the start
    — chmod-after-write left a short world-readable window with the bearer
    token inside.
    """
    payload = {"port": port, "token": token, "pid": os.getpid()}
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)  # atomic on Windows and POSIX


def create_server(host: str = "127.0.0.1", port: int = 0,
                  options: Optional[ProcessOptions] = None,
                  token: Optional[str] = None) -> ThreadingHTTPServer:
    """Create (but do not start) a PhotoS API server. Used by run_server and tests."""
    _PhotoSHandler.options = options or ProcessOptions()
    _PhotoSHandler.token = token
    _register_plugin_rest()
    return ThreadingHTTPServer((host, port), _PhotoSHandler)


def _register_plugin_rest() -> int:
    """Let installed plugins extend the REST surface (v2.3 wiring).

    A plugin defining ``register_rest(handler_class)`` (hooks.PhotoSPlugin
    protocol) is called with the handler class — e.g. auto-tone adds
    /v1/auto_tone routes. Broken plugins are logged and skipped, never
    fatal. The handler class is process-global, so registration runs ONCE:
    re-creating servers (tests, reconnects) must not stack wrapper layers
    on do_POST.
    """
    if getattr(_PhotoSHandler, "_plugin_routes_registered", False):
        return 0
    registered = 0
    try:
        from .plugin import discover_plugins
        from .hooks import PhotoSPlugin
        plugins = discover_plugins()
    except Exception as e:
        print("plugin discovery failed: {}".format(e), file=os.sys.stderr)
        return 0
    for plugin in plugins:
        fn = getattr(plugin, "register_rest", None)
        # only real overrides count (base-class default is a no-op)
        if not callable(fn) or \
                getattr(type(plugin), "register_rest", None) is \
                PhotoSPlugin.register_rest:
            continue
        try:
            fn(_PhotoSHandler)
            registered += 1
        except Exception as e:
            print("plugin '{}' REST registration failed: {}".format(
                getattr(plugin, "name", "?"), e), file=os.sys.stderr)
    if registered:
        _PhotoSHandler._plugin_routes_registered = True
        print("plugin REST routes registered by {} plugin(s)".format(
            registered), file=os.sys.stderr)
    return registered


def run_server(host: str = "127.0.0.1", port: int = 8787,
               options: Optional[ProcessOptions] = None,
               token: Optional[str] = None,
               ready_file: Optional[str] = None) -> None:
    """Start the PhotoS API server, blocking until Ctrl+C.

    Args:
        ready_file: If set, write {"port", "token", "pid"} to this path once
                    the server is listening — the automation handshake for
                    host agents (paired with --token auto).

    Binding a non-loopback address without a token used to expose an
    unauthenticated read/write API to the whole LAN — a token is now
    auto-generated (and printed) in that case.

    TLS: set ``PHOTO_S_TLS=1`` plus ``PHOTO_S_CERT`` (PEM cert) and
    ``PHOTO_S_KEY`` (PEM key; defaults to the cert file if omitted) to serve
    over HTTPS. Requesting TLS without a cert is an error — the server never
    claims https unless the socket is actually wrapped.
    """
    _loopback = host in ("127.0.0.1", "localhost", "::1", "")
    if not _loopback and not token:
        token = generate_token()
        print("⚠️  绑定非回环地址且未提供 token：已自动生成 Bearer token "
              "(non-loopback bind without --token: one was generated)")
        print(f"    Authorization: Bearer {token}")
    tls_enabled = bool(os.environ.get("PHOTO_S_TLS"))
    tls_cert = os.environ.get("PHOTO_S_CERT") or None
    tls_key = os.environ.get("PHOTO_S_KEY") or tls_cert
    if tls_enabled and not tls_cert:
        raise RuntimeError(
            "PHOTO_S_TLS set but no certificate: provide PHOTO_S_CERT "
            "(and PHOTO_S_KEY if separate) as PEM paths")

    server = create_server(host, port, options=options, token=token)
    scheme = "http"
    if tls_enabled:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    actual_port = server.server_address[1]
    print(f"🚀 PhotoS API 服务已启动 Server started: {scheme}://{host}:{actual_port}")
    print(f"   端点 Endpoints: GET /health, GET /info, "
          f"POST /process /dedup /rename /contact-sheet /check")
    if token:
        print(f"   🔐 已启用 Bearer token 认证 Bearer auth enabled")
        if ready_file:
            print(f"   📄 握手文件 Ready file: {ready_file}")
    else:
        print("   ⚠️  无认证 No auth — 仅限本机信任环境 (trusted local use only)")
    print("   按 Ctrl+C 停止 Press Ctrl+C to stop")
    print()

    if ready_file:
        try:
            write_ready_file(ready_file, actual_port, token)
        except OSError as e:
            print(f"⚠️  握手文件写入失败 Ready file error: {e}", file=os.sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if ready_file:
            try:
                os.unlink(ready_file)
            except OSError:
                pass
