"""PhotoS GUI package (v2.0 layout).

v2.0.0 split the former ~10k-line photo_s/gui.py into focused modules:

    app.py        PhotoSApp (window, panels, dialogs) + run_gui
    theme.py      palettes / fonts / spacing + dark-mode detection
    strings.py    STRINGS (zh/en) UI text tables
    widgets/      FlatButton, grading editors, zoom/pan, Tk-free helpers
    workflows.py  Tk-free seam functions (dedup/cull/hash/hdr/…)

Every name that used to live in photo_s.gui is still importable from
photo_s.gui (re-exported below) — tests, the CLI and plugins keep their
`from photo_s.gui import PhotoSApp, run_gui, STRINGS, COLORS, …` paths.
"""

import tempfile  # noqa: F401 — tests monkeypatch via photo_s.gui.tempfile
import threading  # noqa: F401
import webbrowser  # noqa: F401
from tkinter import filedialog, messagebox  # noqa: F401

from .strings import DEFAULT_LANG, STRINGS  # noqa: F401
from .theme import (  # noqa: F401
    APP_NAME, APP_VERSION, MIN_HEIGHT, MIN_WIDTH, SETTINGS_WIDTH,
    WINDOW_HEIGHT, WINDOW_WIDTH,
    COLORS, PLATFORM_FONTS,
    FONT_BODY, FONT_BUTTON, FONT_BUTTON_LG, FONT_SECTION, FONT_SMALL,
    FONT_TINY, FONT_TITLE,
    _DARK_COLORS, _LIGHT_COLORS, _apply_palette, _system_dark_mode,
)
from .widgets import (  # noqa: F401
    HSL_COLORS, ColorWheel, CurveEditor, HSLPanel,
    FlatButton, _ZoomPanState, _exif_datetime_str, _mask_spec_string,
    _open_image_safe, canvas_unbind_safe, rgb_to_hex,
)
from .app import DND_AVAILABLE, PhotoSApp, run_gui  # noqa: F401
