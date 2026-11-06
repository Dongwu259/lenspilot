"""Interactive Tk widgets for Lightroom-direction grading (v1.6.x GUI).

(v2.0: moved from photo_s/gui_widgets.py — that module is now a shim.)

Three widgets drive the engine's compact-string specs (``curves`` /
``color_grading`` / ``hsl``) as a visual front-end:

* ``CurveEditor`` — draggable PCHIP point curve (one channel per instance).
* ``ColorWheel`` — HSV wheel, click/drag picks hue (angle) + saturation
  (radius) for a grading zone.
* ``HSLPanel`` — 8 color domains, click a chip then drive hue/sat/lum.

The serialization/geometry math lives in plain methods (``to_spec`` /
``dump`` / ``_pick_from_pos`` / …) so tests can drive them without a display.
"""

import math
import tkinter as tk
from tkinter import ttk

from ...grade import _monotone_cubic


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(c) for c in rgb)


# ── Draggable point curve ────────────────────────────────────────────────────

class CurveEditor(tk.Canvas):
    """Draggable PCHIP point curve for one channel (rgb/r/g/b).

    Fixed endpoints (0,0) and (255,255); drag near a point to move it,
    double-click empty space to add one, right-click a point to delete
    (endpoints are never deleted). Serializes to ``ch:x,y;x,y``.
    """

    MARGIN = 14
    POINT_R = 4
    HIT = 8

    def __init__(self, master, channel="rgb", points=None, on_change=None,
                 width=280, height=200, bg="#ffffff"):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=1, highlightbackground="#cccccc")
        self.channel = channel
        self.on_change = on_change
        self._drag_idx = None
        self._req_w = width
        self._req_h = height
        # Re-render once the canvas is actually laid out: winfo_width() is 1
        # before mapping, so the initial _render would draw at ~10px and the
        # points would sit where they're not drawn (un-hittable).
        self.bind("<Configure>", lambda e: self._render())
        self.set_points(points if points is not None else [(0, 0), (255, 255)])
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-3>", self._on_right)

    # -- geometry (plain math, testable) -------------------------------------
    def _px(self):
        w = self.winfo_width()
        if w <= 1:  # not laid out yet → requested size
            w = self._req_w
        return max(10, w - 2 * self.MARGIN)

    def _py(self):
        h = self.winfo_height()
        if h <= 1:  # not laid out yet → requested size
            h = self._req_h
        return max(10, h - 2 * self.MARGIN)

    def data_to_canvas(self, x, y):
        return (self.MARGIN + x / 255.0 * self._px(),
                self.MARGIN + (255.0 - y) / 255.0 * self._py())

    def canvas_to_data(self, cx, cy):
        x = (cx - self.MARGIN) / self._px() * 255.0
        y = 255.0 - (cy - self.MARGIN) / self._py() * 255.0
        return (max(0.0, min(255.0, x)), max(0.0, min(255.0, y)))

    # -- points ---------------------------------------------------------------
    def set_points(self, points):
        pts = []
        for x, y in sorted((float(a), float(b)) for a, b in points):
            x = max(0.0, min(255.0, x))
            y = max(0.0, min(255.0, y))
            if pts and abs(pts[-1][0] - x) < 0.5:
                continue  # keep the first point for a shared x
            pts.append((x, y))
        if not pts or pts[0][0] > 0.5:
            pts.insert(0, (0.0, 0.0))
        if not pts or pts[-1][0] < 254.5:
            pts.append((255.0, 255.0))
        self._points = pts
        self._render()
        if self.on_change:
            self.on_change(self)

    def get_points(self):
        return [tuple(p) for p in self._points]

    def to_spec(self, channel=None):
        ch = channel or self.channel
        pts = ";".join(f"{int(round(x))},{int(round(y))}"
                       for x, y in self._points)
        return f"{ch}:{pts}"

    @staticmethod
    def is_identity(points) -> bool:
        """Only the two fixed diagonal endpoints → linear → identity."""
        pts = sorted(points)
        return (len(pts) == 2 and abs(pts[0][0] - 0) < 0.5
                and abs(pts[0][1] - 0) < 0.5
                and abs(pts[1][0] - 255) < 0.5
                and abs(pts[1][1] - 255) < 0.5)

    # -- rendering ------------------------------------------------------------
    def _render(self):
        self.delete("all")
        w, h = self._px(), self._py()
        M = self.MARGIN
        for v in range(0, 256, 32):
            x = M + v / 255.0 * w
            self.create_line(x, M, x, M + h, fill="#eeeeee")
            y = M + (255 - v) / 255.0 * h
            self.create_line(M, y, M + w, y, fill="#eeeeee")
        self.create_line(M, M + h, M + w, M, fill="#e0e0e0")  # diagonal
        # PCHIP curve
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        yq = _monotone_cubic(xs, ys, list(range(256)))
        coords = []
        for i, xv in enumerate(range(256)):
            cx, cy = self.data_to_canvas(xv, yq[i])
            coords += [cx, cy]
        self.create_line(*coords, fill="#1a7f37", width=2, smooth=True)
        for x, y in self._points:
            cx, cy = self.data_to_canvas(x, y)
            self.create_oval(cx - self.POINT_R, cy - self.POINT_R,
                             cx + self.POINT_R, cy + self.POINT_R,
                             fill="#ffffff", outline="#1a7f37", width=2)

    # -- interaction ----------------------------------------------------------
    def _hit_index(self, cx, cy):
        for i, (x, y) in enumerate(self._points):
            px, py = self.data_to_canvas(x, y)
            if abs(px - cx) <= self.HIT and abs(py - cy) <= self.HIT:
                return i
        return None

    def _on_press(self, e):
        idx = self._hit_index(e.x, e.y)
        if idx is not None:
            self._drag_idx = idx
            return
        # Clicking the curve (not on a control point) adds a point there and
        # immediately starts dragging it — the Lightroom interaction. The
        # point snaps onto the curve at the clicked x so adding is a no-op
        # until the user drags.
        x, y = self.canvas_to_data(e.x, e.y)
        x = max(1.0, min(254.0, x))
        xs = [p[0] for p in self._points]
        ys = [p[1] for p in self._points]
        y = float(_monotone_cubic(xs, ys, [x])[0])
        import bisect
        i = bisect.bisect_left(xs, x)
        pts = self._points
        lo = pts[i - 1][0] + 1.0 if i > 0 else 0.0
        hi = pts[i][0] - 1.0 if i < len(pts) else 255.0
        x = max(lo, min(hi, x))
        pts.insert(i, (x, y))
        self._points = pts
        self._drag_idx = i
        self._render()
        if self.on_change:
            self.on_change(self)

    def _on_drag(self, e):
        if self._drag_idx is None:
            return
        x, y = self.canvas_to_data(e.x, e.y)
        pts = self._points
        i = self._drag_idx
        lo = pts[i - 1][0] + 1.0 if i > 0 else 0.0
        hi = pts[i + 1][0] - 1.0 if i < len(pts) - 1 else 255.0
        x = max(lo, min(hi, x))
        pts[i] = (x, y)
        pts.sort(key=lambda p: p[0])
        self._points = pts
        self._drag_idx = min(range(len(pts)),
                             key=lambda j: abs(pts[j][0] - x))
        self._render()
        if self.on_change:
            self.on_change(self)

    def _on_release(self, e):
        self._drag_idx = None

    def _on_right(self, e):
        idx = self._hit_index(e.x, e.y)
        if idx is None or len(self._points) <= 2:
            return
        if idx in (0, len(self._points) - 1):  # endpoints are fixed
            return
        del self._points[idx]
        self._render()
        if self.on_change:
            self.on_change(self)


# ── HSV color wheel ──────────────────────────────────────────────────────────

class ColorWheel(tk.Canvas):
    """HSV color wheel; click/drag picks hue (angle) + saturation (radius).

    Used for a grading zone (shadows/midtones/highlights): the picked hue
    is the zone's target hue, the radius is the tint strength (0 centre
    = no tint, edge = full).
    """

    def __init__(self, master, hue=0.0, sat=0.0, on_change=None,
                 size=180, bg="#ffffff"):
        super().__init__(master, width=size, height=size, bg=bg,
                         highlightthickness=0)
        self.size = size
        self.center = size / 2.0
        self.radius = max(10.0, size / 2.0 - 6)
        self.on_change = on_change
        self._tkimg = None
        self._render_wheel()
        self.set_value(hue, sat)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)

    # -- wheel image ----------------------------------------------------------
    def _render_wheel(self):
        from PIL import Image, ImageTk
        import numpy as np
        n = self.size
        yy, xx = np.mgrid[0:n, 0:n]
        c = (n - 1) / 2.0
        dx = xx - c
        dy = yy - c
        r = np.sqrt(dx * dx + dy * dy) / self.radius
        ang = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0
        sat = np.clip(r, 0.0, 1.0)
        val = np.ones_like(sat)
        h6 = ang / 360.0 * 6.0
        sector = np.floor(h6).astype(np.int32) % 6
        f = h6 - np.floor(h6)
        ch = val * sat
        xv = ch * (1.0 - np.abs(np.mod(sector.astype(np.float64), 2.0)
                                + f - 1.0))
        m = val - ch
        z = np.zeros_like(ch)
        R = np.choose(sector, [ch, xv, z, z, xv, ch]) + m
        G = np.choose(sector, [xv, ch, ch, xv, z, z]) + m
        B = np.choose(sector, [z, z, xv, ch, ch, xv]) + m
        arr = np.stack([R, G, B], axis=-1)
        arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
        arr[r > 1.0] = (245, 245, 245)  # outside the circle → neutral
        img = Image.fromarray(arr, "RGB")
        # master=self: without it the PhotoImage binds to the ambient
        # default root — dead in multi-interp test processes, which
        # surfaced as TclError "pyimageN doesn't exist" under Xvfb.
        self._tkimg = ImageTk.PhotoImage(img, master=self)
        self.create_image(self.center, self.center, image=self._tkimg,
                          tags="wheel")

    # -- value ----------------------------------------------------------------
    def _pick_from_pos(self, cx, cy):
        """Canvas pos → (hue_deg 0-360, sat 0-1). Plain math, testable."""
        dx = cx - self.center
        dy = cy - self.center
        r = math.hypot(dx, dy) / self.radius
        ang = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
        return (ang, max(0.0, min(1.0, r)))

    def set_value(self, hue_deg, sat):
        self._hue = float(hue_deg) % 360.0
        self._sat = max(0.0, min(1.0, float(sat)))
        self._draw_indicator()
        if self.on_change:
            self.on_change(self)

    def get_value(self):
        return (self._hue, self._sat)

    def _draw_indicator(self):
        self.delete("ind")
        ang = math.radians(self._hue)
        r = self.radius * self._sat
        cx = self.center + r * math.cos(ang)
        cy = self.center + r * math.sin(ang)
        self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                         outline="#ffffff", width=2, tags="ind")
        self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                         outline="#222222", width=1, tags="ind")

    def _on_press(self, e):
        hue, sat = self._pick_from_pos(e.x, e.y)
        self.set_value(hue, sat)

    def _on_drag(self, e):
        hue, sat = self._pick_from_pos(e.x, e.y)
        self.set_value(hue, sat)


# ── HSL per-color panel ──────────────────────────────────────────────────────

# 8 domains: (spec name, representative RGB)
HSL_COLORS = [
    ("red", (255, 59, 48)), ("orange", (255, 149, 0)),
    ("yellow", (255, 204, 0)), ("green", (52, 199, 89)),
    ("aqua", (90, 200, 250)), ("blue", (0, 122, 255)),
    ("purple", (175, 82, 222)), ("magenta", (255, 45, 85)),
]

_SLIDER_ROWS = [
    ("hue", -180.0, 180.0, "{:+.0f}°"),
    ("sat", -1.0, 1.0, "{:+.2f}"),
    ("lum", -1.0, 1.0, "{:+.2f}"),
]


class HSLPanel(ttk.Frame):
    """8 color domains: click a chip, then drive hue/sat/lum sliders.

    ``labels`` maps a domain name to a localized label; ``dump()`` emits the
    engine ``color:h,s,l;...`` spec (non-zero entries only).
    """

    def __init__(self, master, adjustments=None, labels=None,
                 on_change=None, **kw):
        super().__init__(master, **kw)
        self.labels = labels or {}
        self.on_change = on_change
        self._adj = {}
        self._selected = None
        self._build()
        self.set_adjustments(adjustments or {})

    def _build(self):
        chip_frame = ttk.Frame(self)
        chip_frame.pack(fill="x")
        self._chips = {}
        for i, (name, rgb) in enumerate(HSL_COLORS):
            c = tk.Canvas(chip_frame, width=26, height=26,
                          highlightthickness=1,
                          highlightbackground="#999999", bg=rgb_to_hex(rgb))
            c.grid(row=0, column=i, padx=2)
            c.bind("<Button-1>", lambda e, n=name: self._select(n))
            self._chips[name] = c
        self._name_lbl = ttk.Label(self, text="")
        self._name_lbl.pack(anchor="w", pady=(6, 0))
        self._vars = {}
        self._value_lbls = {}
        for key, lo, hi, _fmt in _SLIDER_ROWS:
            row = ttk.Frame(self)
            row.pack(fill="x", pady=2)
            label = ttk.Label(row, text=self.labels.get(key, key), width=6)
            label.pack(side="left")
            var = tk.DoubleVar(value=0.0)
            self._vars[key] = var
            s = ttk.Scale(row, from_=lo, to=hi, variable=var,
                          command=lambda v, k=key: self._on_slider(k))
            s.pack(side="left", fill="x", expand=True, padx=6)
            vl = ttk.Label(row, text=_fmt.format(0.0), width=7)
            vl.pack(side="right")
            self._value_lbls[key] = vl

    def _select(self, name):
        self._selected = name
        for n, c in self._chips.items():
            c.configure(highlightbackground="#1a7f37" if n == name
                        else "#999999")
        self._name_lbl.config(text=self.labels.get(name, name))
        cur = self._adj.get(name, (0.0, 0.0, 0.0))
        for key, val in zip(("hue", "sat", "lum"), cur):
            self._vars[key].set(val)
        self._refresh_lbls()

    def _refresh_lbls(self):
        for key, _lo, _hi, fmt in _SLIDER_ROWS:
            self._value_lbls[key].config(text=fmt.format(self._vars[key].get()))

    def _on_slider(self, key):
        if self._selected is None:
            return
        self._refresh_lbls()
        self._adj[self._selected] = tuple(
            self._vars[k].get() for k in ("hue", "sat", "lum"))
        if self.on_change:
            self.on_change(self)

    # -- public API -----------------------------------------------------------
    def set_adjustments(self, adj):
        self._adj = {k: tuple(v) for k, v in adj.items()}
        if self._selected is None:
            if self._adj:
                self._select(next(iter(self._adj)))
            else:
                self._select(HSL_COLORS[0][0])
        else:
            self._refresh_lbls()

    def get_adjustments(self):
        return {k: tuple(v) for k, v in self._adj.items()}

    def load(self, spec_str):
        if spec_str:
            from ...grade import _parse_hsl
            self.set_adjustments(_parse_hsl(spec_str))
        else:
            self.set_adjustments({})

    def dump(self):
        parts = []
        for name, (h, s, l) in self._adj.items():
            if abs(h) > 0.5 or abs(s) > 0.005 or abs(l) > 0.005:
                parts.append(f"{name}:{h:g},{s:g},{l:g}")
        return ";".join(parts)
