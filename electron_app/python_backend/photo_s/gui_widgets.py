"""Backward-compat shim (v2.0.0): the grading-editor widgets moved to
photo_s.gui.widgets.editors — this module keeps the historical import
path (`from photo_s.gui_widgets import CurveEditor`) working.
"""

from .gui.widgets.editors import (  # noqa: F401
    HSL_COLORS, ColorWheel, CurveEditor, HSLPanel, rgb_to_hex,
)
