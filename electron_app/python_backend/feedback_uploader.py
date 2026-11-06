"""
feedback_uploader.py — opt-in upload of local selection-feedback to the server.

Mirrors agent_module._report_official_usage: urllib POST with the x-studio-token
header, best-effort, non-blocking. Only runs when the user has enabled feedback
sharing AND a cloud token is present. Uploaded rows are marked so they are never
sent twice; failures leave the queue intact (offline-queue semantics).
"""

import json
import urllib.request
import urllib.error

from service import send_log, register_handler
import feedback_queue

# Overridable via the server_url param (local-server testing); defaults to prod.
DEFAULT_SERVER = "https://studio.dwphoto.top"
_MAX_BATCHES = 5          # drain up to 5 batches per sync call
# 512-d float32 feature ≈ 11.5 KB/record as JSON; keep batch × record well under
# the server's 2 MB body cap (FEEDBACK_MAX_BODY). 80 × ~11.5 KB ≈ 0.9 MB.
_BATCH = 80


def _opt_in():
    try:
        import service as _svc
        return bool(_svc._app_config.get("feedback_share_enabled", False))
    except Exception:
        return False


def _post(server_url, cloud_token, records):
    """POST one batch; returns (ok, accepted, disabled). Swallows nothing."""
    url = server_url.rstrip("/") + "/api/feedback/selection"
    body = json.dumps({"records": records}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "x-studio-token": cloud_token,
        "x-lenspilot-client": "1",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    return True, int(data.get("accepted", 0)), bool(data.get("disabled", False))


def upload_pending(cloud_token, server_url=None, batch=_BATCH):
    """Upload queued feedback in batches. Returns a status dict."""
    if not _opt_in():
        return {"skipped": "opt_out", "uploaded": 0, "pending": feedback_queue.count_pending()}
    if not cloud_token:
        return {"skipped": "no_token", "uploaded": 0, "pending": feedback_queue.count_pending()}

    server = server_url or DEFAULT_SERVER
    total_uploaded = 0
    for _ in range(_MAX_BATCHES):
        rows = feedback_queue.take_pending(batch)
        if not rows:
            break
        ids = [r["id"] for r in rows]
        records = [{
            "modelVersion": r["modelVersion"],
            "featureDim": r["featureDim"],
            "feature": r["feature"],
            "label": r["label"],
            "source": r["source"],
            "category": r["category"],
            "cameraModel": r["cameraModel"],
            "score": r["score"],
            "blur": r["blur"],
            "eyeOpen": r["eyeOpen"],
            "ts": r["ts"],
        } for r in rows]
        try:
            ok, accepted, disabled = _post(server, cloud_token, records)
        except urllib.error.HTTPError as e:
            send_log(f"feedback upload HTTP {e.code}", "warn")
            return {"error": f"http_{e.code}", "uploaded": total_uploaded, "pending": feedback_queue.count_pending()}
        except Exception as e:
            send_log(f"feedback upload failed: {e}", "warn")
            return {"error": "network", "uploaded": total_uploaded, "pending": feedback_queue.count_pending()}
        if disabled:
            # Server ingest flag is off → it drops the batch. Leave rows PENDING
            # (don't mark uploaded / lose them); retry when the admin enables it.
            # Sync is user-triggered (after scoring), so this is no retry-storm.
            return {"skipped": "ingest_disabled", "uploaded": total_uploaded, "pending": feedback_queue.count_pending()}
        # Server stored the batch.
        feedback_queue.mark_uploaded(ids)
        total_uploaded += accepted
        if len(rows) < batch:
            break  # drained

    return {"uploaded": total_uploaded, "pending": feedback_queue.count_pending()}


def handle_feedback_sync(params):
    cloud_token = (params.get("cloud_token") or "").strip()
    server_url = params.get("server_url") or None
    return upload_pending(cloud_token, server_url)


def handle_feedback_get_status(params):
    return {
        "enabled": _opt_in(),
        "pending": feedback_queue.count_pending(),
        "uploaded": feedback_queue.count_uploaded(),
    }


def register_handlers():
    register_handler("feedback_sync", handle_feedback_sync)
    register_handler("feedback_get_status", handle_feedback_get_status)
    send_log("feedback_uploader module loaded", "info")
