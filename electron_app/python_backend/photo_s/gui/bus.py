"""UiBus — worker→UI event marshalling (photo_s.gui.bus, v2.0).

Formalizes the queue + after-drain convention that used to be copied
into every dialog (13 inline copies):

* worker threads must NEVER touch Tk directly — they queue UI calls
  with ``schedule(fn)`` from any thread;
* a main-thread ``after``-loop drains the queue onto the widget.

Why not ``win.after`` from the worker: it raises
"main thread is not in main loop" whenever the mainloop is not running
(every unit test, and any embedding host without a mainloop).

The loop stops itself when the widget dies (``winfo_exists`` False or
TclError on re-arm) or on ``stop()`` — closing a dialog cannot leak a
drain loop that would fire into destroyed widgets.
"""

import queue
import tkinter as tk


class UiBus:
    """One bus per dialog/window. ``interval`` ms between drains."""

    def __init__(self, widget, interval=80):
        self._widget = widget
        self._interval = interval
        self._q = queue.Queue()
        self._running = False

    def schedule(self, fn):
        """Queue a UI call; safe from any thread."""
        self._q.put(fn)

    def start(self):
        """Arm the drain loop (idempotent)."""
        if not self._running:
            self._running = True
            self._drain()

    def stop(self):
        """Halt the loop after the current drain (widget still alive)."""
        self._running = False

    def drain_pending(self):
        """Run every queued callback once, no re-arm.

        For hybrid poll loops (the preview dialog's debounce loop) that
        embed a drain inside their own after-loop instead of owning a
        plain UiBus drain — the one documented exception to "always use
        UiBus.start()".
        """
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass

    def _drain(self):
        # NOTE: does not set _running — stop() must stay effective even if
        # a drain callback fires afterwards.
        if not self._widget.winfo_exists():
            self._running = False
            return
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        if self._running and self._widget.winfo_exists():
            try:
                self._widget.after(self._interval, self._drain)
            except tk.TclError:
                self._running = False  # widget died mid-schedule
        else:
            self._running = False
