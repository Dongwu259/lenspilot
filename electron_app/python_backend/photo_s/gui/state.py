"""GUI state — photo_s.gui.state (v2.0).

Two responsibilities, both Tk-free:

* ``~/.photos/gui_state.json`` persistence (window geometry, thumbnail
  size) — used to live inside PhotoSApp methods, extracted so the
  storage format is testable without a display.
* ``ThumbCache`` — byte-bounded LRU for decoded file-list thumbnails.
  The pre-v2.0 cache was a plain dict: a 5,000-photo folder kept every
  decode alive for the session. The API mirrors the dict call sites
  (``cache[key] = img`` / ``cache.get(key)`` / ``in cache`` /
  ``cache.clear()``); values are PIL images or the ``False``
  failed-decode marker.
"""

import json
import threading
from collections import OrderedDict
from pathlib import Path


# ── ~/.photos/gui_state.json ────────────────────────────────────────────────

def state_file() -> Path:
    return Path.home() / ".photos" / "gui_state.json"


def load_state() -> dict:
    """Read gui_state.json; {} on any error (missing, corrupt, …)."""
    try:
        with open(state_file(), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """Write gui_state.json; never crashes on save."""
    try:
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


# ── Bounded thumbnail cache ─────────────────────────────────────────────────

class ThumbCache:
    """Byte-bounded LRU for decoded thumbnails.

    Locked, not just GIL-atomic: RAW decodes insert from worker threads
    while the UI thread reads, and the OrderedDict bookkeeping
    (insert + evict, get + move_to_end) is a multi-step sequence.
    """

    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        self._max = max(int(max_bytes), 1)
        self._data: OrderedDict = OrderedDict()   # key → (img, size)
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _sizeof(img) -> int:
        """Estimated footprint: w×h×3 bytes. The False failure marker
        and empty values cost nothing."""
        if not img:
            return 0
        try:
            return img.width * img.height * 3
        except Exception:
            return 1 << 20  # unknown shape — assume 1 MB

    def __setitem__(self, key, img):
        size = self._sizeof(img)
        with self._lock:
            if key in self._data:
                self._bytes -= self._data[key][1]
            self._data[key] = (img, size)
            self._bytes += size
            while self._bytes > self._max and len(self._data) > 1:
                _k, (_img, sz) = self._data.popitem(last=False)
                self._bytes -= sz

    def __getitem__(self, key):
        with self._lock:
            item = self._data[key]  # KeyError propagates (callers use .get)
            self._data.move_to_end(key)
            return item[0]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        with self._lock:
            return key in self._data

    def clear(self):
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def __len__(self):
        with self._lock:
            return len(self._data)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes
