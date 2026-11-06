"""
score_cache.py — SQLite-backed persistent score cache

Caches per-image scores keyed by (path, model_version) with mtime+size
invalidation, plus thumbnail path and manual keep/reject override. Lets the
user re-open a folder and see all results instantly without re-scoring.

Also persists AI auto-tags in a photo_tags table (path PK, mtime/size
invalidation identical to the score rows) written by photo_tagger.

Thread-safety: each call opens its own short-lived connection with
check_same_thread=False. The Python backend runs handlers in worker threads,
so connections must not be shared across threads.
"""

import json
import os
import shutil
import sqlite3
import time
import threading
try:
    from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty

from service import send_log
from utils import get_user_data_dir

_db_path = os.path.join(get_user_data_dir(), "scores.db")
try:
    os.makedirs(get_user_data_dir(), exist_ok=True)
except Exception:
    pass

# Connection pool: up to 4 persistent connections, shared across threads.
# Each thread checks out a connection, uses it, then returns it.
_pool = Queue(maxsize=4)
_pool_lock = threading.Lock()
_pool_count = 0
_pool_max = 4


def _connect():
    """Get a connection from the pool, or create one if under limit."""
    try:
        return _pool.get(block=False)
    except Empty:
        pass
    global _pool_count
    with _pool_lock:
        if _pool_count < _pool_max:
            _pool_count += 1
            conn = sqlite3.connect(_db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
    # Pool full — fall back to one-off connection (will block until one is returned)
    return _pool.get(block=True, timeout=30)


def _release(conn):
    """Return a connection to the pool. If the pool is already full, close the
    extra connection instead of retrying — the previous in-place recursion
    spun until RecursionError and leaked the connection."""
    try:
        _pool.put(conn, block=False)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _init():
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")


def _init():
    try:
        conn = _connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                path TEXT NOT NULL,
                model_version TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                score REAL,
                width INTEGER,
                height INTEGER,
                is_raw INTEGER DEFAULT 0,
                thumb_path TEXT,
                manual_keep INTEGER DEFAULT 0,
                scored_at INTEGER,
                PRIMARY KEY (path, model_version)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_path ON scores(path)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS photo_tags (
                path TEXT PRIMARY KEY,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                tags_json TEXT NOT NULL,
                tagged_at INTEGER
            )
        """)
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"score_cache init failed: {e}", "error")


_init()


def _row_to_dict(row):
    if not row:
        return None
    return {
        "path": row["path"],
        "model_version": row["model_version"],
        "mtime": row["mtime"],
        "size": row["size"],
        "score": row["score"],
        "width": row["width"],
        "height": row["height"],
        "is_raw": bool(row["is_raw"]),
        "thumb_path": row["thumb_path"] or "",
        "manual_keep": row["manual_keep"],
        "scored_at": row["scored_at"],
    }


def get(path, model_version, mtime, size):
    """Return cached row if path+model match and mtime+size unchanged, else None."""
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM scores WHERE path=? AND model_version=?",
            (path, model_version),
        ).fetchone()
        _release(conn)
        if row and row["mtime"] == mtime and row["size"] == size:
            return _row_to_dict(row)
        return None
    except Exception:
        return None


def get_score(path, model_version):
    """Get stored score for a path without mtime/size validation (best-effort)."""
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM scores WHERE path=? AND model_version=?",
            (path, model_version),
        ).fetchone()
        _release(conn)
        if row:
            return _row_to_dict(row)
        return None
    except Exception:
        return None


def put_tags(path, mtime, size, tags):
    """Persist AI auto-tags for one file. tags: list[str]. Upsert by path;
    mtime/size recorded so reads can invalidate like the score rows."""
    try:
        payload = json.dumps([str(t) for t in tags][:12], ensure_ascii=False)
        conn = _connect()
        conn.execute(
            """INSERT INTO photo_tags (path, mtime, size, tags_json, tagged_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 mtime=excluded.mtime, size=excluded.size,
                 tags_json=excluded.tags_json, tagged_at=excluded.tagged_at""",
            (path, int(mtime), int(size), payload, int(time.time())),
        )
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"score_cache put_tags failed: {e}", "error")


def get_tags(paths):
    """Return {path: [tags]} for cached tags whose file still exists with
    unchanged mtime/size (same staleness semantics as the score cache)."""
    out = {}
    if not paths:
        return out
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        rows = []
        # Chunked IN queries (SQLite parameter limit is 999 by default)
        for i in range(0, len(paths), 400):
            chunk = paths[i:i + 400]
            q = ("SELECT path, mtime, size, tags_json FROM photo_tags "
                 "WHERE path IN (" + ",".join("?" * len(chunk)) + ")")
            rows.extend(conn.execute(q, chunk).fetchall())
        _release(conn)
        for row in rows:
            p = row["path"]
            try:
                st = os.stat(p)
            except OSError:
                continue  # file gone → stale
            if int(st.st_mtime) != row["mtime"] or st.st_size != row["size"]:
                continue  # file changed → stale (will be re-tagged)
            try:
                tags = json.loads(row["tags_json"])
            except Exception:
                continue
            if isinstance(tags, list) and tags:
                out[p] = [t for t in tags if isinstance(t, str)][:12]
        return out
    except Exception as e:
        send_log(f"score_cache get_tags failed: {e}", "error")
        return {}


def put(path, model_version, mtime, size, score, width, height, is_raw, thumb_path):
    try:
        conn = _connect()
        conn.execute(
            """INSERT INTO scores (path, model_version, mtime, size, score, width, height,
                                   is_raw, thumb_path, manual_keep, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(path, model_version) DO UPDATE SET
                 mtime=excluded.mtime, size=excluded.size, score=excluded.score,
                 width=excluded.width, height=excluded.height, is_raw=excluded.is_raw,
                 thumb_path=excluded.thumb_path, scored_at=excluded.scored_at""",
            (path, model_version, mtime, size, score, width, height,
             1 if is_raw else 0, thumb_path, int(time.time())),
        )
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"score_cache put failed: {e}", "error")


def get_folder(folder, model_version, limit=10000, offset=0):
    """Return cached rows whose path is under `folder`, matching model_version
    and whose file still exists with unchanged mtime/size.

    Pushes folder filtering into SQL via LIKE with normalized separators,
    avoiding the full-table scan into memory. LIMIT/OFFSET support pagination.
    """
    results = []
    try:
        norm_folder = os.path.normpath(folder)
        # Build LIKE patterns that handle both / and \ path separators.
        # LIKE-escape with ESCAPE '\': literal backslashes, '%' and '_' in the
        # path must ALL be escaped. (Bug fix: the old code escaped only %/_,
        # so pattern1's trailing sep+'%' collapsed to a literal '%' — never
        # matching any Windows path — and pattern2's '/'-conversion destroyed
        # the '\_' escapes, so any folder containing '_' matched nothing and
        # get_folder silently returned [].)
        esc = (norm_folder.replace('\\', '\\\\')
                          .replace('%', '\\%')
                          .replace('_', '\\_'))
        sep = '\\\\' if os.sep == '\\' else os.sep  # escaped separator for LIKE
        pattern1 = esc + sep + '%'    # exact folder prefix
        base2 = norm_folder.replace('\\', '/')
        esc2 = base2.replace('%', '\\%').replace('_', '\\_')
        pattern2 = esc2 + '/%'        # forward-slash variant

        conn = _connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM scores
               WHERE model_version=?
                 AND (path LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'
                      OR REPLACE(path, '\\', '/') LIKE ? ESCAPE '\\')
               ORDER BY path
               LIMIT ? OFFSET ?""",
            (model_version, pattern1, pattern2, pattern2, limit, offset),
        ).fetchall()
        _release(conn)

        # Also match the folder itself (files at the root of the folder)
        # and do existence staleness check in Python (cannot do in SQL)
        for row in rows:
            p = row["path"]
            np = os.path.normpath(p)
            if np != norm_folder and not np.startswith(norm_folder + os.sep):
                # Double-check: SQL LIKE may have false positives with similar prefixes
                continue
            if not os.path.exists(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if int(st.st_mtime) != row["mtime"] or st.st_size != row["size"]:
                continue  # file changed → stale, skip (will be re-scored)
            results.append(_row_to_dict(row))
        return results
    except Exception as e:
        send_log(f"score_cache get_folder failed: {e}", "error")
        return []


def set_manual(path, model_version, keep):
    """Set manual override: -1 reject, 0 none, 1 keep. Inserts a stub row if absent."""
    try:
        conn = _connect()
        conn.execute(
            """INSERT INTO scores (path, model_version, mtime, size, manual_keep, scored_at)
               VALUES (?, ?, 0, 0, ?, ?)
               ON CONFLICT(path, model_version) DO UPDATE SET manual_keep=excluded.manual_keep""",
            (path, model_version, keep, int(time.time())),
        )
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"score_cache set_manual failed: {e}", "error")


def get_all():
    """Return all cached entries with manual_keep != 0 (feedback signals)."""
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT path, model_version, score, manual_keep, width, height, mtime, size "
            "FROM scores WHERE manual_keep != 0"
        ).fetchall()
        _release(conn)
        return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def _empty_dir(d):
    """Remove everything inside directory d (but not d itself). Best-effort:
    files held open by a concurrent scoring run are skipped, not fatal."""
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except Exception:
            pass


def clear():
    try:
        conn = _connect()
        conn.execute("DELETE FROM scores")
        conn.execute("DELETE FROM photo_tags")
        conn.commit()
        _release(conn)
    except Exception as e:
        send_log(f"score_cache clear failed: {e}", "error")
        return False
    # Also drop the derived thumbnail/heatmap files so they don't keep using
    # disk after the rows are gone (clear_user_data removes the whole dirs;
    # here we keep the dirs and only empty their contents).
    try:
        data_dir = get_user_data_dir()
        _empty_dir(os.path.join(data_dir, "thumbs"))
        _empty_dir(os.path.join(data_dir, "heatmaps"))
    except Exception as e:
        send_log(f"score_cache clear: thumb/heatmap cleanup failed: {e}", "warn")
    return True
