"""
feedback_queue.py — local opt-in queue for selection-feedback records.

Stores professional photo-selection decisions (deep-feature vector + keep/reject
label + coarse metadata) locally until they are uploaded to the server for the
data flywheel. NO photos, paths, or filenames are stored here — only the derived
feature vector and label. Nothing leaves the machine unless the user has enabled
the opt-in switch AND the uploader runs.

Thread-safety: short-lived connections guarded by a module lock; the backend
runs handlers in worker threads so connections are not shared across threads.
"""

import os
import json
import sqlite3
import threading
import time

from service import send_log
from utils import get_user_data_dir

_db_path = os.path.join(get_user_data_dir(), "feedback_queue.db")
try:
    os.makedirs(get_user_data_dir(), exist_ok=True)
except Exception:
    pass

_lock = threading.Lock()

# Cap the local queue so it can never grow unbounded on a machine that is
# offline / never logged in. Oldest rows are pruned first.
MAX_ROWS = 5000


def _connect():
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_version TEXT,
                    feature_dim INTEGER,
                    feature TEXT,
                    label INTEGER,
                    source TEXT,
                    category TEXT,
                    camera_model TEXT,
                    score REAL,
                    blur REAL,
                    eye_open REAL,
                    created_at TEXT,
                    uploaded INTEGER DEFAULT 0
                )
                """
            )
            # Lightweight migration: add scalar columns to a pre-existing dev db.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_queue)").fetchall()}
            for col in ("score", "blur", "eye_open", "override"):
                if col not in cols:
                    col_type = "INTEGER" if col == "override" else "REAL"
                    conn.execute(f"ALTER TABLE feedback_queue ADD COLUMN {col} {col_type}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fbq_uploaded ON feedback_queue(uploaded, id)")
            conn.commit()
            conn.close()
    except Exception as e:
        send_log(f"feedback_queue init failed: {e}", "error")


_init()


def _prune(conn):
    """Keep at most MAX_ROWS rows: drop uploaded rows first, then oldest."""
    try:
        total = conn.execute("SELECT COUNT(*) FROM feedback_queue").fetchone()[0]
        if total <= MAX_ROWS:
            return
        conn.execute("DELETE FROM feedback_queue WHERE uploaded=1")
        total = conn.execute("SELECT COUNT(*) FROM feedback_queue").fetchone()[0]
        if total > MAX_ROWS:
            excess = total - MAX_ROWS
            conn.execute(
                "DELETE FROM feedback_queue WHERE id IN "
                "(SELECT id FROM feedback_queue ORDER BY id ASC LIMIT ?)",
                (excess,),
            )
    except Exception:
        pass


def enqueue(record):
    """Add one record. `record` keys: model_version, feature (list[float]),
    label (0/1), source, category, camera_model. Returns True on success."""
    try:
        feature = record.get("feature")
        if not isinstance(feature, (list, tuple)) or not feature:
            return False
        label = record.get("label")
        if label not in (0, 1):
            return False

        def _f(x):
            try:
                return None if x is None else float(x)
            except Exception:
                return None

        with _lock:
            conn = _connect()
            conn.execute(
                """INSERT INTO feedback_queue
                   (model_version, feature_dim, feature, label, source, category, camera_model,
                    score, blur, eye_open, override, created_at, uploaded)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    str(record.get("model_version") or "")[:128],
                    len(feature),
                    json.dumps([float(x) for x in feature]),
                    int(label),
                    str(record.get("source") or "manual_keep")[:32],
                    str(record.get("category") or "")[:64],
                    str(record.get("camera_model") or "")[:128],
                    _f(record.get("score")),
                    _f(record.get("blur")),
                    _f(record.get("eye_open")),
                    1 if record.get("override") else 0,
                    record.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            _prune(conn)
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        send_log(f"feedback_queue enqueue failed: {e}", "error")
        return False


def take_pending(n=200):
    """Return up to n un-uploaded rows as dicts (incl. id), oldest first."""
    try:
        with _lock:
            conn = _connect()
            rows = conn.execute(
                "SELECT * FROM feedback_queue WHERE uploaded=0 ORDER BY id ASC LIMIT ?",
                (int(n),),
            ).fetchall()
            conn.close()
        out = []
        for r in rows:
            try:
                feature = json.loads(r["feature"])
            except Exception:
                feature = []
            out.append({
                "id": r["id"],
                "modelVersion": r["model_version"],
                "featureDim": r["feature_dim"],
                "feature": feature,
                "label": r["label"],
                "source": r["source"],
                "category": r["category"],
                "cameraModel": r["camera_model"],
                "score": r["score"],
                "blur": r["blur"],
                "eyeOpen": r["eye_open"],
                "override": r["override"],
                "ts": r["created_at"],
            })
        return out
    except Exception as e:
        send_log(f"feedback_queue take_pending failed: {e}", "error")
        return []


def mark_uploaded(ids):
    """Mark the given row ids as uploaded (they become prune candidates)."""
    if not ids:
        return
    try:
        with _lock:
            conn = _connect()
            conn.executemany(
                "UPDATE feedback_queue SET uploaded=1 WHERE id=?",
                [(int(i),) for i in ids],
            )
            conn.commit()
            conn.close()
    except Exception as e:
        send_log(f"feedback_queue mark_uploaded failed: {e}", "error")


def count_pending():
    try:
        with _lock:
            conn = _connect()
            c = conn.execute("SELECT COUNT(*) FROM feedback_queue WHERE uploaded=0").fetchone()[0]
            conn.close()
        return c
    except Exception:
        return 0


def count_uploaded():
    try:
        with _lock:
            conn = _connect()
            c = conn.execute("SELECT COUNT(*) FROM feedback_queue WHERE uploaded=1").fetchone()[0]
            conn.close()
        return c
    except Exception:
        return 0


def clear():
    try:
        with _lock:
            conn = _connect()
            conn.execute("DELETE FROM feedback_queue")
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        send_log(f"feedback_queue clear failed: {e}", "error")
        return False
