"""FlatButton — a Canvas-drawn pill button (photo_s.gui.widgets).

tk.Button on macOS Aqua ignores custom colors entirely, hence the custom
draw. Extracted verbatim from the former photo_s/gui.py (v2.0).
"""

import tkinter as tk
import tkinter.font as tkfont

from ..theme import COLORS, FONT_BUTTON

class FlatButton(tk.Canvas):
    """A flat, rounded-pill button that honors colors on every platform.

    tk.Button on macOS Aqua ignores custom colors entirely, so the button is
    drawn on a Canvas: a rounded rectangle (polygon with smooth corners)
    plus centered text. Hover swaps the fill, a disabled state greys the
    button and blocks clicks. ``configure(text=..., bg=..., fg=...,
    state=...)`` keeps the classic widget API so callers (e.g. the
    copy-flash in the settings dialog) need no changes.
    """

    def __init__(self, master, text, command, bg, fg="white",
                 hover_bg=None, font=None, padx=16, pady=7,
                 border_color=None):
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or bg
        self._border = border_color
        self._font = font or FONT_BUTTON
        self._padx, self._pady = padx, pady
        self._state = "normal"
        self._text = text
        self._fill = bg  # current rendered fill (hover-aware)
        try:
            super().__init__(
                master, bg=master.cget("bg"), highlightthickness=0, bd=0,
                cursor="pointinghand",
            )
        except Exception:
            # Some environments (e.g. headless Xvfb) lack the 'pointinghand'
            # cursor; degrade to the default cursor instead of failing.
            super().__init__(
                master, bg=master.cget("bg"), highlightthickness=0, bd=0,
            )
        self._measure_and_redraw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        # keyboard accessibility: Canvas widgets are not focus targets by
        # default, so every FlatButton was mouse-only
        self.configure(takefocus=True)
        self.bind("<Return>", self._on_key_activate)
        self.bind("<space>", self._on_key_activate)

    # ── internals ─────────────────────────────────────────────────────────

    def _measure_and_redraw(self):
        """Size the canvas to the text, then (re)draw the pill + label."""
        f = tkfont.Font(font=self._font)
        w = f.measure(self._text) + 2 * self._padx + 4
        h = f.metrics("linespace") + 2 * self._pady
        # bypass the FlatButton.configure override (avoid recursion)
        tk.Canvas.configure(self, width=w, height=h)
        self.delete("all")
        radius = h // 2  # full pill
        pts = [radius, 1, w - radius, 1, w - 1, 1, w - 1, radius,
               w - 1, h - radius, w - 1, h - 1, w - radius, h - 1,
               radius, h - 1, 1, h - 1, 1, h - radius, 1, radius, 1, 1]
        fill = COLORS["border"] if self._state == "disabled" else self._fill
        outline = self._border or fill
        self.create_polygon(pts, smooth=True, fill=fill, outline=outline)
        text_color = (COLORS["text_secondary"] if self._state == "disabled"
                      else self._fg)
        self.create_text(w / 2, h / 2, text=self._text, fill=text_color,
                         font=self._font)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw.update(cnf)
        changed = bool(kw)
        old_bg, old_hover = self._bg, self._hover_bg
        for key in ("text", "bg", "fg", "hover_bg", "border_color"):
            if key in kw:
                setattr(self, "_" + key, kw.pop(key))
        # keep the rendered fill in sync with new colors
        if self._fill == old_bg:
            self._fill = self._bg  # idle → follow the new base color
        if self._fill == old_hover:
            self._fill = self._hover_bg  # hovering → new hover color
        if "state" in kw:
            self._state = kw.pop("state")
        if kw:
            tk.Canvas.configure(self, **kw)
        if changed:
            self._measure_and_redraw()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        if key == "bg":
            return self._fill
        if key == "fg":
            return self._fg
        return super().cget(key)

    def _is_enabled(self):
        return self._state != "disabled"

    def _on_key_activate(self, _event):
        if self._is_enabled() and self._command:
            self._command()
        return "break"

    def _on_enter(self, _event):
        if self._is_enabled() and self._fill != self._hover_bg:
            self._fill = self._hover_bg
            self._measure_and_redraw()

    def _on_leave(self, _event):
        if self._fill != self._bg:
            self._fill = self._bg
            self._measure_and_redraw()

    def _on_click(self, _event):
        if self._is_enabled() and self._command:
            self._command()
