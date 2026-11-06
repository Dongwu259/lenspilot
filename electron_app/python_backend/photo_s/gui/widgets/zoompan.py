"""_ZoomPanState — Tk-free zoom/pan math for the compare viewer.

Pure math, no tkinter import, fully unit-testable. Extracted verbatim
from the former photo_s/gui.py (v2.0).
"""

class _ZoomPanState:
    """Tk-free zoom/pan state for the multi-image compare viewer.

    zoom = 1.0 fits the whole image; the visible window is the 1/zoom
    fraction of the source centered on (fx, fy) — image-fraction
    coordinates in [0, 1]. Each compare panel owns one instance: wheel
    zoom and drag pan target a single instance by default, or every
    instance when the viewer's sync-zoom checkbox is on. Pure math:
    no tkinter import, fully unit-testable.
    """

    MIN_ZOOM = 1.0
    MAX_ZOOM = 16.0

    def __init__(self):
        self.zoom = self.MIN_ZOOM
        self.fx = 0.5
        self.fy = 0.5

    def fit(self):
        """Reset to the fit-the-whole-image view."""
        self.zoom = self.MIN_ZOOM
        self.fx = self.fy = 0.5

    def zoom_at(self, factor):
        """Scale zoom by ``factor``, clamped to [1, 16]. Landing back on
        1.0 re-centers the view (a fit view has nothing to pan)."""
        self.zoom = min(self.MAX_ZOOM,
                        max(self.MIN_ZOOM, self.zoom * factor))
        if self.zoom <= self.MIN_ZOOM:
            self.fx = self.fy = 0.5
        else:
            self._clamp_center()

    def pan(self, dfx, dfy):
        """Move the center by (dfx, dfy) in image-fraction units."""
        self.fx += dfx
        self.fy += dfy
        self._clamp_center()

    def _clamp_center(self):
        # The visible half-extent is 1/(2*zoom); keep the center at least
        # that far from every edge so the window never leaves the image.
        # At zoom == 1 the range degenerates to 0.5 — pan is a no-op.
        m = 0.5 / self.zoom
        self.fx = min(1.0 - m, max(m, self.fx))
        self.fy = min(1.0 - m, max(m, self.fy))
