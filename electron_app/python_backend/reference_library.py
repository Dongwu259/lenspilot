"""
reference_library.py - Layer 2 multimodal RAG reference store (0.8.6 M1).

The shooting coach's "moat": a private library of expert reference shots
(image feature + EXIF + genre + lighting + caption) that grows as the user
works. The coach retrieves the K most-similar references for the current shot
and feeds them (image + EXIF + caption) into the LLM context, so a general LLM
can give professional advice without "memorizing" photography.

Mirrors score_cache.py's SQLite pattern (connection pool + WAL + defensive
try/except + send_log on error). The DB lands in APS_USER_DATA
(get_user_data_dir) so it needs no packaging changes.

M1: brute-force cosine retrieval (library is small at first). Scales fine to
~10k rows; switch to ANN (sqlite-vec / faiss) later if needed.

shooting_coach imports and calls retrieve()/add_reference() in-process.
0.8.8: register_handlers() adds IPC commands for manual import
(reference_import / reference_stats / reference_clear) - see bottom of file.

See TETHERED_SHOOTING_TECH_SPEC.md §3.2, IMPLEMENTATION_PLAN_M1.md Step 2.
"""
import os
import json
import sqlite3
import threading
import time
from queue import Queue, Empty

from service import send_log, register_handler
from utils import get_user_data_dir

try:
    import numpy as np
except ImportError:
    np = None  # feature ops become unavailable; retrieve() returns []

# ── DB path & connection pool (mirror score_cache.py) ──
_db_path = os.path.join(get_user_data_dir(), "reference_library.db")
_pool = Queue(maxsize=4)
_pool_lock = threading.Lock()
_pool_count = 0
_pool_max = 4

_FEATURE_DIM = 512  # extract_context_feature output (layer4 global avg pool)


def _connect():
    """Check out a pooled sqlite connection, or create a new one (capped)."""
    global _pool_count
    try:
        return _pool.get_nowait()
    except Empty:
        pass
    with _pool_lock:
        if _pool_count < _pool_max:
            _pool_count += 1
        else:
            try:
                return _pool.get(timeout=30)
            except Empty:
                pass  # last resort: create one over the cap
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def _release(conn):
    try:
        _pool.put_nowait(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _init():
    """Create the table if absent. Called at import (same convention as score_cache)."""
    try:
        os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    except Exception:
        pass
    try:
        conn = _connect()
        # NOTE: table name is 'reference_items' (not 'references' - that's a SQL keyword).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reference_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature BLOB NOT NULL,
                exif TEXT,
                genre TEXT,
                lighting TEXT,
                caption TEXT,
                thumb_path TEXT,
                source TEXT,
                ts INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_genre_lighting "
                     "ON reference_items(genre, lighting)")
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"[reference_library] init failed: {e}", "error")


_init()


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    blob = d.pop("feature", None)
    if blob is not None and np is not None:
        try:
            d["feature"] = np.frombuffer(blob, dtype=np.float32)
        except Exception:
            d["feature"] = None
    else:
        d["feature"] = None
    return d


def add_reference(path=None, feature=None, exif=None, genre="", lighting="",
                  caption="", thumb_path="", source="seed"):
    """Add one reference. Pass either `path` (feature extracted via
    model_loader.extract_context_feature) or a precomputed `feature` (512d).

    Returns the new row id, or None on failure / when the feature can't be
    extracted (e.g. model not loaded). Never raises - must not break the caller.
    """
    if feature is None and path:
        try:
            from model_loader import extract_context_feature, _load_image_rgb
            img = _load_image_rgb(path)
            if img is None:
                return None
            feature = extract_context_feature(img)
        except Exception as e:
            send_log(f"[reference_library] feature extract failed: {e}", "warn")
            return None
    if feature is None:
        return None
    # Serialize feature to BLOB
    if np is not None and not isinstance(feature, (bytes, bytearray)):
        try:
            feature = np.asarray(feature, dtype=np.float32).tobytes()
        except Exception as e:
            send_log(f"[reference_library] feature serialize failed: {e}", "warn")
            return None
    exif_json = json.dumps(exif, ensure_ascii=False) if isinstance(exif, dict) else (exif or "")
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO reference_items(feature, exif, genre, lighting, caption, "
            "thumb_path, source, ts) VALUES(?,?,?,?,?,?,?,?)",
            (feature, exif_json, genre or "", lighting or "", caption or "",
             thumb_path or "", source or "seed", int(time.time())))
        conn.commit()
        rid = cur.lastrowid
        _release(conn)
        return rid
    except Exception as e:
        send_log(f"[reference_library] add_reference failed: {e}", "error")
        return None


def retrieve(query_feature, genre="", lighting="", k=5):
    """Return up to k most-similar references (cosine) to query_feature,
    filtered by genre (and lighting if given). Falls back one level at a time
    when the filtered set is empty: genre+lighting -> genre only -> whole
    library (0.8.8), so a sparsely-populated genre shelf still yields refs.
    Empty list if numpy unavailable or the library is empty. Each result:
    {id, feature, exif(parsed), genre, lighting, caption, thumb_path, source,
    ts, score}.

    Brute-force over the filtered set - fine for M1 scale.
    """
    if np is None or query_feature is None:
        return []
    try:
        q = np.asarray(query_feature, dtype=np.float32)
        qn = float(np.linalg.norm(q))
        if qn == 0:
            return []
        q = q / qn
        conn = _connect()
        rows = []
        if genre and lighting:
            rows = conn.execute(
                "SELECT * FROM reference_items WHERE genre=? AND lighting=?",
                (genre, lighting)).fetchall()
        if not rows and genre:
            # Fallback: genre+lighting (or genre shelf) empty -> genre only
            rows = conn.execute(
                "SELECT * FROM reference_items WHERE genre=?", (genre,)).fetchall()
        if not rows:
            # Fallback: genre shelf empty -> search the whole library
            rows = conn.execute("SELECT * FROM reference_items").fetchall()
        _release(conn)

        scored = []
        for r in rows:
            d = _row_to_dict(r)
            f = d.get("feature")
            if f is None:
                continue
            fn = float(np.linalg.norm(f))
            if fn == 0:
                continue
            cos = float(np.dot(q, f / fn))
            scored.append((cos, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for cos, d in scored[:k]:
            d["score"] = cos
            ex = d.get("exif")
            if ex:
                try:
                    d["exif"] = json.loads(ex)
                except Exception:
                    pass
            out.append(d)
        return out
    except Exception as e:
        send_log(f"[reference_library] retrieve failed: {e}", "error")
        return []


def count():
    """Total reference count (for UI/debug). 0 on error."""
    try:
        conn = _connect()
        n = conn.execute("SELECT COUNT(*) FROM reference_items").fetchone()[0]
        _release(conn)
        return int(n)
    except Exception:
        return 0


def clear():
    """Delete all references (debug/reset)."""
    try:
        conn = _connect()
        conn.execute("DELETE FROM reference_items")
        conn.commit()
        _release(conn)
        return True
    except Exception as e:
        send_log(f"[reference_library] clear failed: {e}", "error")
        return False


def seed_reference_library(seed_dir=None):
    """M1 stub: import curated seed reference shots (Strobist / textbook / pro
    blog images). Seed-source licensing must be cleared before populating (see
    TETHERED_SHOOTING_TECH_SPEC.md §10). M1 leaves the library empty - the coach
    degrades to Layer1 rules + Layer3 playbook when retrieve() returns [].
    """
    # TODO(M2): implement seed import once licensing is resolved.
    send_log("[reference_library] seed import not implemented (M1) - library starts empty", "info")
    return 0


# ══════════════════════════════════════════════════════════════════════
# IPC handlers - manual reference import by genre shelf (0.8.8)
# ══════════════════════════════════════════════════════════════════════

def _model_ready():
    """True when the scoring model is loaded (feature extraction possible)."""
    try:
        import model_loader
        return getattr(model_loader, "_model", None) is not None
    except Exception:
        return False


def handle_reference_import(params):
    """reference_import {paths: [], genre: '', lighting: ''} - import picked
    files into one genre shelf. caption = filename, source = 'manual'.
    Per-file failure does not abort the batch."""
    paths = params.get("paths") or []
    if not isinstance(paths, list):
        paths = [paths]
    genre = params.get("genre") or ""
    lighting = params.get("lighting") or ""
    total = len(paths)
    if not total:
        return {"error": "未选择文件", "imported": 0, "failed": 0, "total": 0}
    if not _model_ready():
        return {"error": "AI 模型未加载，请先在「智能选片」页加载模型后再导入",
                "imported": 0, "failed": total, "total": total}
    imported = 0
    failed = 0
    for p in paths:
        try:
            rid = add_reference(path=p, genre=genre, lighting=lighting,
                                caption=os.path.basename(p), source="manual")
            if rid:
                imported += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            send_log(f"[reference_library] import failed for {p}: {e}", "warn")
    send_log(f"[reference_library] 手动导入完成: {imported}/{total} 成功, "
             f"{failed} 失败 (genre='{genre}', lighting='{lighting}')", "info")
    return {"imported": imported, "failed": failed, "total": total}


def handle_reference_stats(_params):
    """reference_stats {} - total + per-genre counts ('' key = 通用 shelf)."""
    by_genre = {}
    total = 0
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT genre, COUNT(*) AS n FROM reference_items GROUP BY genre").fetchall()
        _release(conn)
        for r in rows:
            n = int(r["n"])
            by_genre[r["genre"] or ""] = n
            total += n
    except Exception as e:
        send_log(f"[reference_library] stats failed: {e}", "error")
    return {"total": total, "by_genre": by_genre}


def handle_reference_clear(params):
    """reference_clear {genre} - delete ONE genre shelf only. '' deletes the
    通用 shelf (genre=''), NOT the whole library."""
    genre = params.get("genre")
    if genre is None:
        genre = ""
    try:
        conn = _connect()
        cur = conn.execute("DELETE FROM reference_items WHERE genre=?", (genre,))
        deleted = cur.rowcount
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM reference_items").fetchone()[0]
        _release(conn)
        label = genre or "通用"
        send_log(f"[reference_library] 清空题材「{label}」: 删除 {deleted} 条, 剩余 {remaining} 条", "info")
        return {"deleted": int(deleted), "remaining": int(remaining)}
    except Exception as e:
        send_log(f"[reference_library] clear failed: {e}", "error")
        return {"error": str(e), "deleted": 0, "remaining": count()}


def register_handlers():
    register_handler("reference_import", handle_reference_import)
    register_handler("reference_stats", handle_reference_stats)
    register_handler("reference_clear", handle_reference_clear)
    send_log("reference_library module loaded", "info")
