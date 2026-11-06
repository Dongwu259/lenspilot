"""
cloud_uploader.py — Upload photos to the studio cloud server.

Two upload paths:
  * Small files (<= CHUNK_THRESHOLD): single multipart POST /api/upload.
  * Large files (> CHUNK_THRESHOLD): resumable chunked upload via
    /api/upload/chunk/{init,status,chunk,complete}. Chunk sessions persist
    to ~/.lenspilot/pending-uploads.json so a cancelled upload can be
    resumed on the next attempt (only the missing chunks are re-sent).
"""

import os
import json
import time
import threading
import urllib.request
import urllib.error

from service import send_event, send_log, register_handler

# ── State ──
_upload_cancel = threading.Event()
_uploading = False

# ── Chunked upload config (must agree with studio-server lib/config.js defaults) ──
CHUNK_SIZE = 8 * 1024 * 1024          # 8 MB per chunk
CHUNK_THRESHOLD = 10 * 1024 * 1024    # files > 10 MB use chunked upload
CHUNK_RETRIES = 3                     # per-chunk retry attempts
CHUNK_RETRY_BACKOFF = 1.5             # seconds, doubled each retry

try:
    from utils import get_user_data_dir
    PENDING_DIR = get_user_data_dir()
except Exception:
    PENDING_DIR = os.path.join(os.path.expanduser("~"), ".lenspilot")
PENDING_FILE = os.path.join(PENDING_DIR, "pending-uploads.json")
_pending_lock = threading.Lock()


# ── Pending-upload persistence (for cross-attempt resume) ──

def _load_pending():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending(data):
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        tmp = PENDING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, PENDING_FILE)
    except Exception as e:
        send_log(f"无法保存续传记录: {e}", "warn")


def _pending_key(file_path):
    try:
        st = os.stat(file_path)
        return f"{file_path}|{int(st.st_mtime)}|{st.st_size}"
    except Exception:
        return file_path


def _take_pending(file_path):
    """Pop and return a stored uploadId for this file, if any."""
    key = _pending_key(file_path)
    with _pending_lock:
        data = _load_pending()
        return data.pop(key, None), data


def _set_pending(file_path, upload_id):
    key = _pending_key(file_path)
    with _pending_lock:
        data = _load_pending()
        data[key] = upload_id
        _save_pending(data)


def _clear_pending(file_path):
    key = _pending_key(file_path)
    with _pending_lock:
        data = _load_pending()
        if key in data:
            del data[key]
            _save_pending(data)


# ── HTTP helpers ──

def _request_json(method, url, token, body=None, headers=None, timeout=60):
    """Issue a JSON request; return parsed JSON. Raises RuntimeError on failure."""
    data = None
    h = {"x-studio-token": token}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} — {err}")
    except Exception as e:
        raise RuntimeError(str(e))


def _request_raw(method, url, token, body_bytes, extra_headers=None, timeout=120):
    """Issue a raw-body request; return parsed JSON."""
    h = {
        "x-studio-token": token,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(body_bytes)),
    }
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, data=body_bytes, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} — {err}")
    except Exception as e:
        raise RuntimeError(str(e))


# ── Single-shot upload (small files) ──

def _create_multipart_body(fields, file_data, filename, boundary):
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode())
    lines.append(f"--{boundary}".encode())
    lines.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode())
    lines.append(b"Content-Type: application/octet-stream")
    lines.append(b"")
    lines.append(file_data)
    lines.append(f"--{boundary}--".encode())
    return b"\r\n".join(lines)


def upload_photo(server_url, token, file_path, score=0, category="pending",
                 tags="", camera_model="", idempotency_key=""):
    """Upload a single (small) photo via the classic multipart endpoint."""
    global _upload_cancel
    if _upload_cancel.is_set():
        raise RuntimeError("上传已取消")
    import uuid
    boundary = f"----StudioUpload{uuid.uuid4().hex[:16]}"
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_data = f.read()

    fields = {
        "score": str(score),
        "category": category,
        "tags": tags,
        "camera_model": camera_model,
    }
    if idempotency_key:
        fields["idempotency_key"] = idempotency_key

    body = _create_multipart_body(fields, file_data, filename, boundary)
    req = urllib.request.Request(
        f"{server_url}/api/upload",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-studio-token": token,
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Upload failed: HTTP {e.code} — {error_body}")
    except Exception as e:
        raise RuntimeError(f"Upload failed: {e}")


# ── Resumable chunked upload (large files) ──

def _chunk_status(server_url, token, upload_id):
    """Return set of already-received part numbers, or None if session gone."""
    try:
        r = _request_json("GET", f"{server_url}/api/upload/chunk/status?uploadId={upload_id}",
                          token, timeout=30)
        return set(r.get("parts", [])), r.get("chunks", 0)
    except RuntimeError as e:
        if "404" in str(e):
            return None, 0
        raise


def upload_photo_chunked(server_url, token, file_path, score=0, category="pending",
                         tags="", camera_model="", idempotency_key="", batch=None):
    """Upload a large photo in resumable chunks. Resumes from a prior interrupted
    attempt if a valid uploadId is persisted for this file.

    batch: optional {"current", "total", "success", "failed"} batch-level progress
    context, so chunk events report real batch progress instead of a fake 1/1."""
    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    batch = batch or {}

    # Try to resume an existing session
    upload_id, _ = _take_pending(file_path)
    have_parts = set()
    chunks = 0
    if upload_id:
        parts, chunks = _chunk_status(server_url, token, upload_id)
        if parts is None:
            send_log(f"续传会话已失效，重新开始: {filename}", "info")
            upload_id = None
        else:
            have_parts = parts
            send_log(f"续传 {filename}: 已有 {len(have_parts)}/{chunks} 块", "info")

    # Init a new session if needed
    if not upload_id:
        r = _request_json("POST", f"{server_url}/api/upload/chunk/init", token, body={
            "filename": filename,
            "size": file_size,
            "score": score,
            "category": category,
            "tags": tags,
            "camera_model": camera_model,
            "idempotency_key": idempotency_key,
        }, timeout=60)
        # Idempotency short-circuit: already uploaded previously
        if r.get("duplicated"):
            return r
        upload_id = r["uploadId"]
        chunks = r["chunks"]
        _set_pending(file_path, upload_id)

    # Upload missing chunks
    with open(file_path, "rb") as f:
        for part_number in range(1, chunks + 1):
            if _upload_cancel.is_set():
                _set_pending(file_path, upload_id)  # keep for next attempt
                raise RuntimeError("上传已取消")

            if part_number in have_parts:
                continue

            f.seek((part_number - 1) * CHUNK_SIZE)
            chunk = f.read(CHUNK_SIZE)

            attempt = 0
            while True:
                if _upload_cancel.is_set():
                    _set_pending(file_path, upload_id)
                    raise RuntimeError("上传已取消")
                try:
                    _request_raw("POST", f"{server_url}/api/upload/chunk", token, chunk, extra_headers={
                        "X-Upload-Id": upload_id,
                        "X-Part-Number": str(part_number),
                    }, timeout=180)
                    break
                except RuntimeError as e:
                    attempt += 1
                    if attempt > CHUNK_RETRIES:
                        _set_pending(file_path, upload_id)
                        raise RuntimeError(f"分块 {part_number}/{chunks} 上传失败: {e}")
                    send_log(f"分块 {part_number}/{chunks} 失败，重试 {attempt}/{CHUNK_RETRIES}: {e}", "warn")
                    time.sleep(CHUNK_RETRY_BACKOFF * attempt)

            # Report real batch-level current/total so the frontend progress bar
            # doesn't oscillate between per-file 100% and batch-level percentages.
            send_event("upload_progress", {
                "current": batch.get("current", 1),
                "total": batch.get("total", 1),
                "success": batch.get("success", 0),
                "failed": batch.get("failed", 0),
                "chunk_part": part_number, "chunk_total": chunks,
                "current_file": filename, "current_score": score,
            })

    # Finalize
    result = _request_json("POST", f"{server_url}/api/upload/chunk/complete", token, body={
        "uploadId": upload_id,
    }, timeout=300)
    _clear_pending(file_path)
    return result


def upload_photo_auto(server_url, token, file_path, batch=None, **kwargs):
    """Pick the upload path based on file size."""
    if os.path.getsize(file_path) > CHUNK_THRESHOLD:
        return upload_photo_chunked(server_url, token, file_path, batch=batch, **kwargs)
    return upload_photo(server_url, token, file_path, **kwargs)


# ── IPC Handlers ──

def handle_upload_photos(params):
    """Upload multiple photos to the cloud server."""
    global _uploading, _upload_cancel

    server_url = params.get("server_url", "https://studio.dwphoto.top")
    token = params.get("token", "")
    file_paths = params.get("file_paths", [])
    scores = params.get("scores", {})  # dict of file_path → score
    idempotency_key = params.get("idempotency_key", "")
    # Per-file keys from the renderer (path → key). A whole batch sharing one
    # key gets deduped down to its first file by the server.
    idempotency_keys = params.get("idempotency_keys") or {}

    if not token:
        raise ValueError("Missing authentication token")
    if not file_paths:
        raise ValueError("No files to upload")

    _upload_cancel.clear()
    _uploading = True

    total = len(file_paths)
    success = 0
    failed = 0
    uploaded_paths = []

    send_log(f"开始上传 {total} 张照片到 {server_url}", "info")

    for i, file_path in enumerate(file_paths):
        if _upload_cancel.is_set():
            send_log("上传已取消", "warn")
            break

        filename = os.path.basename(file_path)
        score = scores.get(file_path, 0)
        # Per-file key so the server doesn't dedupe a multi-photo batch down
        # to its first file; falls back to the batch key for a single upload.
        file_key = idempotency_keys.get(file_path)
        if not file_key:
            file_key = idempotency_key if total == 1 else (f"{idempotency_key}_{i}" if idempotency_key else "")

        try:
            result = upload_photo_auto(
                server_url, token, file_path,
                score=score,
                category="pending",
                idempotency_key=file_key,
                # Batch context so chunk events carry real batch-level progress
                batch={"current": i + 1, "total": total,
                       "success": success, "failed": failed},
            )
            success += 1
            uploaded_paths.append(file_path)
            send_event("upload_progress", {
                "current": i + 1,
                "total": total,
                "success": success,
                "failed": failed,
                "current_file": filename,
                "current_score": score,
            })
        except Exception as e:
            failed += 1
            send_log(f"上传失败: {filename} — {e}", "error")
            send_event("upload_progress", {
                "current": i + 1,
                "total": total,
                "success": success,
                "failed": failed,
                "current_file": filename,
                "error": str(e),
            })

    _uploading = False
    cancelled = _upload_cancel.is_set()
    send_event("upload_done", {
        "total": total,
        "success": success,
        "failed": failed,
        "cancelled": cancelled,
        "uploaded_paths": uploaded_paths,
    })
    send_log(f"上传{'已取消' if cancelled else '完成'}: {success}/{total} 成功, {failed} 失败",
             "warn" if (failed or cancelled) else "info")

    return {"total": total, "success": success, "failed": failed,
            "cancelled": cancelled, "uploaded_paths": uploaded_paths}


def handle_cancel_upload(params):
    """Cancel ongoing upload."""
    global _upload_cancel
    _upload_cancel.set()
    return {"ok": True}


def handle_get_upload_status(params):
    """Return current upload status."""
    global _uploading
    return {"uploading": _uploading}


def handle_test_connection(params):
    """Test connection to the cloud server."""
    server_url = params.get("server_url", "https://studio.dwphoto.top")
    token = params.get("token", "")

    # /api/auth/me works for every valid token (/api/stats is admin-only)
    req = urllib.request.Request(
        f"{server_url}/api/auth/me",
        headers={"x-studio-token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            me = json.loads(resp.read())
            return {"connected": True, "user": {"email": me.get("email"), "subscription": me.get("subscription")}}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Connection failed: HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"Connection failed: {e}")


# ── Registration ──

def register_handlers():
    register_handler("upload_photos", handle_upload_photos)
    register_handler("cancel_upload", handle_cancel_upload)
    register_handler("get_upload_status", handle_get_upload_status)
    register_handler("cloud_test_connection", handle_test_connection)
