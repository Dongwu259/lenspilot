"""
PhotoS GUI — main application (photo_s/gui/app.py, v2.0 package layout).

PhotoSApp: the Tk application object (window, file panel, settings
panel, workflow dialogs, batch processing loop) plus the run_gui entry
point. The v2.0 split moved design tokens to gui/theme.py, UI strings
to gui/strings.py, widgets to gui/widgets/, Tk-free workflow logic to
gui/workflows.py and state persistence/caches to gui/state.py — this
module holds everything that must own a Tk context.

Features:
  - Chinese / English UI language switching
  - Drag-and-drop file addition (via tkinterdnd2, optional)
  - File list with size/format/dimensions preview
  - Adjustable quality, format, resize options
  - Real-time progress tracking with cancellation
  - Batch summary with space savings and before/after comparison
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from typing import List, Optional

from .. import watermark  # for POSITIONS on the watermark section

from ..engine import (
    ProcessOptions,
    BatchResult,
    batch_process,
    auto_jobs,
    scan_directory,
    format_size,
    _resolve_folder_pattern,
    SUPPORTED_FORMATS,
    INPUT_EXTENSIONS,
    ALL_INPUT_EXTENSIONS,
    RAW_EXTENSIONS,
)
from . import workflows
from .bus import UiBus
from .state import ThumbCache, load_state, save_state, state_file
from .strings import DEFAULT_LANG, STRINGS
from .theme import (
    APP_NAME, APP_VERSION, MIN_HEIGHT, MIN_WIDTH, SETTINGS_WIDTH,
    WINDOW_HEIGHT, WINDOW_WIDTH,
    COLORS, PLATFORM_FONTS,
    FONT_BODY, FONT_BUTTON, FONT_BUTTON_LG, FONT_SECTION, FONT_SMALL,
    FONT_TINY, FONT_TITLE,
    _apply_palette, _system_dark_mode, apply_dpi_awareness,
)
from .widgets import (
    FlatButton,
    _ZoomPanState,
    _exif_datetime_str,
    _mask_spec_string,
    _open_image_safe,
    canvas_unbind_safe,
)


# ── Workspace modules (v2.0) ────────────────────────────────────────────────
# Library: file grid/list · Develop: preview + histogram · Export: settings
# · Tools: workflow launchers. The frames are all built once; switching
# just (un)packs them, so widgets and state survive module changes.
MODULES = ("library", "develop", "export", "tools")


# ── Optional drag-and-drop support ─────────────────────────────────────────

DND_AVAILABLE = False
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_AVAILABLE = True
except ImportError:
    pass


# ── Constants ───────────────────────────────────────────────────────────────


# Color scheme — picked at import time from the system appearance.
# ttk controls on macOS use the native aqua theme and follow the system
# dark/light mode; a hardcoded light palette made dark-mode systems render
# dark native controls on a light background (the "black boxes" report).
# So the layout palette must match the system: light or dark.










# ── Cross-platform font detection ─────────────────────────────────────────────




# ── UI strings (zh / en) ─────────────────────────────────────────────────────




# ── Flat button (renders colors correctly on macOS Aqua) ─────────────────────













# ── Main Application ────────────────────────────────────────────────────────

class PhotoSApp:
    """Main application window."""

    def __init__(self, root):
        self.root = root
        # DPI awareness on Windows: without it Tk renders blurry on
        # scaled displays and mouse coordinates misalign (theme.py owns
        # the escalating per-monitor-v2 chain).
        apply_dpi_awareness()
        # Startup language: persisted user choice > PHOTO_S_LANG env >
        # system detection. DEFAULT_LANG stays as the _t missing-key fallback.
        from .. import i18n
        self.lang = i18n.resolve_language(use_config=False, use_persisted=True)
        self.dark_mode = _system_dark_mode()
        # COLORS is module-global and may be left flipped by a previous
        # app instance (e.g. tests, or embedding PhotoSApp twice in one
        # process) — re-apply the palette so the build always matches
        # THIS instance's dark_mode.
        _apply_palette(self.dark_mode)
        self.root.title(self._t("window_title"))
        _gui_state = self._load_gui_state()
        self.root.geometry(_gui_state.get("geometry")
                           or f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_main_close)

        # Active workspace module (v2.0): restored across restarts
        self._active_module = _gui_state.get("module")
        if self._active_module not in MODULES:
            self._active_module = "library"

        # State
        self.files: List[str] = []
        # Checked subset of self.files (first Treeview column). All
        # workflow actions — process / review / dedup / gallery — operate
        # on the checked files, not on the row selection (which stays an
        # ephemeral multi-select for remove/analyze). New files are
        # checked by default. Survives language/theme rebuilds.
        self._checked: set = set()
        # Ephemeral row selection (highlight) for remove/analyze/compare.
        # Distinct from _checked on purpose: checks define the action
        # scope, selection is a temporary multi-mark like the old
        # Treeview selection.
        self._selected_rows: set = set()
        # Undo stack (survives rebuilds): list of {"label", "run"} for
        # reversible actions — list removal, dedup trash moves, tagging
        # writes. Capped; the toolbar Undo button / ⌘Z pops the latest.
        self._undo_stack: list = []
        self._undo_max = 10
        self._redo_stack: list = []
        self.processing = False
        self.cancel_requested = False
        self.output_dir = tk.StringVar(value="")
        self.prefix = tk.StringVar(value="")
        self.suffix = tk.StringVar(value="_compressed")
        self.quality = tk.IntVar(value=85)
        self.output_format = tk.StringVar(value="JPEG")
        self.scale_percent = tk.StringVar(value="")
        self.max_width = tk.StringVar(value="")
        self.max_height = tk.StringVar(value="")
        self.preserve_exif = tk.BooleanVar(value=True)
        self.optimize = tk.BooleanVar(value=True)
        self.progressive = tk.BooleanVar(value=False)
        self.jpeg_subsampling = tk.StringVar(value="420")
        self.overwrite = tk.BooleanVar(value=False)
        self.target_size_mode = tk.BooleanVar(value=False)
        self.target_size_value = tk.StringVar(value="500")
        self.target_size_unit = tk.StringVar(value="KB")
        self.raw_demosaic = tk.StringVar(value="auto")
        self.raw_color_space = tk.StringVar(value="sRGB")
        self.raw_16bit = tk.BooleanVar(value=False)
        self.raw_half_size = tk.BooleanVar(value=False)
        self.raw_auto_bright = tk.BooleanVar(value=True)
        self.auto_rotate = tk.BooleanVar(value=True)
        self.remove_original = tk.BooleanVar(value=False)
        self.strip_gps = tk.BooleanVar(value=False)
        self.keep_mtime = tk.BooleanVar(value=False)
        self.max_pixels = tk.StringVar(value="")  # longest-side cap (downscale only)
        # Watermark
        self.watermark_text = tk.StringVar(value="")
        self.watermark_image = tk.StringVar(value="")
        self.watermark_position = tk.StringVar(value="BOTTOM_RIGHT")
        self.watermark_opacity = tk.IntVar(value=50)
        # Multi-size
        self.output_sizes = tk.StringVar(value="")
        # Adjust (tone & color)
        self.brightness = tk.DoubleVar(value=1.0)
        self.contrast = tk.DoubleVar(value=1.0)
        self.saturation = tk.DoubleVar(value=1.0)
        self.gamma = tk.DoubleVar(value=1.0)
        self.sharpen = tk.DoubleVar(value=1.0)
        self.export_sharpen = tk.DoubleVar(value=0.0)
        self.highlight_recovery = tk.DoubleVar(value=0.0)
        self.grayscale = tk.BooleanVar(value=False)
        self.sepia = tk.BooleanVar(value=False)
        # Correction (exposure / LOG / denoise / straighten)
        self.ev = tk.DoubleVar(value=0.0)
        self.auto_exposure = tk.StringVar(value="")
        self.log_curve = tk.StringVar(value="")
        self.denoise = tk.StringVar(value="")
        self.lut_file = tk.StringVar(value="")
        self.auto_straighten = tk.BooleanVar(value=False)
        self.max_straighten_angle = tk.StringVar(value="10")
        # White balance / color / evaluation
        self.wb_temp = tk.StringVar(value="")
        self.wb_reference = tk.StringVar(value="")
        # Lightroom-direction grading (v1.6.0) — blank = off
        self.wb_tint = tk.StringVar(value="")
        self.levels = tk.StringVar(value="")
        self.curves = tk.StringVar(value="")
        self.vibrance = tk.StringVar(value="")
        self.color_grading = tk.StringVar(value="")
        self.hsl = tk.StringVar(value="")
        self.clarity = tk.StringVar(value="")
        self.texture = tk.StringVar(value="")
        self.dehaze = tk.StringVar(value="")
        self.vignette = tk.StringVar(value="")
        self.grain = tk.StringVar(value="")
        # Local adjustments + lens correction (v1.7.0) - blank = off
        self.point_color = tk.StringVar(value="")
        self.masks = tk.StringVar(value="")
        self._photo_masks = {}  # per-photo masks (path -> {masks, mask_adjust})
        self.mask_adjust = tk.StringVar(value="")
        self.lens_distort = tk.StringVar(value="")
        self.lens_vignette = tk.StringVar(value="")
        self.lens_ca = tk.StringVar(value="")
        self.lens_profile = tk.StringVar(value="")
        self.auto_levels = tk.BooleanVar(value=False)
        self.srgb = tk.BooleanVar(value=False)
        self.flatten_cmyk = tk.BooleanVar(value=False)
        self.evaluate = tk.BooleanVar(value=False)
        self.blur_score = tk.BooleanVar(value=False)
        self.resume = tk.BooleanVar(value=False)
        self.print_size = tk.StringVar(value="")
        # Metadata
        self.date_shift = tk.StringVar(value="")
        self.sync_date = tk.BooleanVar(value=False)
        self.scrub = tk.BooleanVar(value=False)
        self.gpx_trace = tk.StringVar(value="")
        self.blur_faces = tk.StringVar(value="")       # ""|blur|pixelate
        self.blur_faces_margin = tk.StringVar(value="20")
        # Cutout / background removal (v2.1.0); mode stores the localized
        # label (live-switch remaps combobox values entry-wise)
        self.cutout_mode = tk.StringVar(value="")
        self.cutout_label = tk.StringVar(value="")     # COCO class (object)
        self.cutout_r = tk.StringVar(value="255")      # color-key RGB
        self.cutout_g = tk.StringVar(value="255")
        self.cutout_b = tk.StringVar(value="255")
        self.cutout_tol = tk.StringVar(value="30")
        self.cutout_feather = tk.StringVar(value="0")
        # Composition
        self.crop = tk.StringVar(value="")
        self.crop_ratio = tk.StringVar(value="")
        self.rotate = tk.StringVar(value="")
        self.rotate_bg = tk.StringVar(value="")
        self.flip = tk.StringVar(value="")
        self.pad_ratio = tk.StringVar(value="")
        self.pad_bg = tk.StringVar(value="#000000")
        # Queue + comparison state
        self._queued_files: List[str] = []
        self._last_result = None
        # (path, size, mtime) -> "WxH" cache for the file list; opening every
        # image on each refresh freezes the UI on RAW files
        self._preview_tempdirs: set = set()
        self._dims_cache: dict = {}
        # Byte-bounded LRU (v2.0): plain dict kept every decode alive for
        # the session — a 5k-photo folder could pin GBs of PIL images.
        # False = failed-decode marker (see _drain_thumbnails).
        self._thumb_cache = ThumbCache()
        self._thumb_decoding: set = set()  # cache keys with a RAW decode in flight
        self._thumb_size = 96          # file-list thumbnail edge px
        # v2.4 VirtualGrid: the Library list is a fixed-row-height canvas
        # that materializes only the visible window — a 5k-photo folder
        # never creates 35k widgets. _lib_model is the row data; drawing
        # reads it plus _checked / _selected_rows live.
        self._lib_model: list = []     # [{path,name,size,fmt,dims,cache_key}]
        self._lib_photos: list = []    # PhotoImage refs kept alive per draw
        self._lib_visible = (0, 0)     # index range [first, last) last drawn
        self.filter_var = tk.StringVar(value="")  # file-list filter box
        self.rename_pattern = tk.StringVar(value="")
        self.folder_pattern = tk.StringVar(value="")
        self.jobs = tk.StringVar(value=str(auto_jobs()))  # parallel workers
        # Develop module state (v2.0 workspace) — the embedded preview
        # renders through the real pipeline with a debounce loop, like
        # the preview dialog but in-panel.
        self._dev_selected = ""            # path currently in the viewer
        self._dev_strip_sig = None         # files-tuple the filmstrip built
        self._dev_render_state = {"sig": None, "stable": 0,
                                  "inflight": False, "rendered": None,
                                  "before_photo": None, "after_photo": None,
                                  "before_pil": None, "after_pil": None,
                                  "tempdir": None}
        self._dev_img_cache = ThumbCache(max_bytes=64 * 1024 * 1024)
        self._dev_show_before = tk.BooleanVar(value=False)
        self._dev_bus = UiBus(self.root)   # drained by _dev_tick; never started
        # v2.2 editing efficiency: per-photo develop overlays (copy/paste +
        # undo/redo) and persisted export recipes
        self._settings_clipboard = None      # dev-fields dict (copy/paste)
        self._photo_adjust = {}              # path → dev-fields overlay
        self._dev_history = {}               # path → [dev-fields dict, …]
        self._dev_history_pos = {}           # path → index into that list
        self._export_recipes = getattr(self, "_pending_export_recipes", {})
        try:
            del self._pending_export_recipes
        except AttributeError:
            pass
        # v2.4: AI auto-tone (official plugin) runs in a worker thread; the
        # busy flag re-entry-guards the button until the result lands
        self._dev_ai_busy = False
        # v2.4: before/after compare mode — "off" | "split" | "side";
        # the split divider position is a 0-1 fraction of the viewer
        self._dev_compare_mode = "off"
        self._dev_split_frac = 0.5
        self._dev_compare_photo = None   # PhotoImage ref for the canvas
        # v2.4: preset hover-preview override — when set, the dev preview
        # renders THESE options instead of the live sliders (no var is
        # touched, so leaving the list restores the edit state for free)
        self._dev_preset_preview = None
        self._dev_preset_hovered = ""
        self._dev_mask_mode = False
        self._dev_mask_frame = None
        self._dev_mask_ctl = None
        self._lib_lightbox_active = False
        self._lib_lightbox_frame = None
        self._lib_lightbox_ctl = None
        # v2.4: settings search highlights (label, bg, fg) to restore
        self._settings_search_marked = []
        self._settings_search_hits = []

        self._configure_ttk_styles()

        # Build UI
        self._build_ui()
        self._bind_global_shortcuts()

        # v2.4: first-run quick-start card (skipped once dismissed —
        # every GUI test boots with a fresh HOME, hence the late after()
        # instead of a synchronous show)
        if not getattr(self, "_first_run_done", False):
            self.root.after(900, self._show_first_run_guide)

        # System-appearance follower (v2.0): a manual toggle pins the
        # choice until restart (see _recheck_system_theme).
        self._theme_user_override = False
        self._theme_poll_after = None
        self.root.bind("<FocusIn>", self._on_focus_theme_check, add="+")
        self._schedule_theme_poll()

        # Periodic update for progress polling
        self._progress_lock = threading.Lock()
        self._progress_current = 0
        self._progress_total = 0
        self._progress_path = ""
        self._progress_status = ""
        self._progress_started = None  # ETA baseline
        self._batch_result = None
        self._batch_error = None
        self._after_id = None

    # ── Localization ────────────────────────────────────────────────────────

    def _t(self, key, **kwargs):
        """Look up a UI string in the current language."""
        text = STRINGS[self.lang].get(key) or STRINGS[DEFAULT_LANG].get(key) or key
        return text.format(**kwargs) if kwargs else text

    def _close_dialogs(self):
        """Close open Toplevel dialogs via their WM_DELETE_WINDOW protocol.

        A bare destroy() skips the protocol callback — dialogs use it for
        cleanup (stopping the watch observer, saving a pending rating), so
        a UI rebuild must close them properly first. Dialogs without a
        protocol handler are simply destroyed.
        """
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                continue
            try:
                cmd = child.wm_protocol("WM_DELETE_WINDOW")
                if cmd:
                    child.tk.call(cmd)
                if child.winfo_exists():
                    child.destroy()
            except tk.TclError:
                pass

    # ── Live language / theme switching (v2.0) ───────────────────────────────
    # Switches used to destroy and rebuild the whole UI (hence the old
    # "every tk.Variable must live in __init__" rule). Now the existing
    # widget tree is retranslated / recolored in place: scroll position,
    # expanded sections and row selection survive a switch.

    def _walk_widgets(self, parent):
        """Yield every widget in the subtree, depth-first."""
        for child in parent.winfo_children():
            yield child
            yield from self._walk_widgets(child)

    @staticmethod
    def _translation_remap(old_lang, new_lang):
        """old-language UI text → new-language UI text.

        Built from the STRINGS reverse map. When several keys share one
        old text (e.g. zh "亮度" is both the brightness label and the HSL
        luminance label), the majority target wins — a tie drops the
        entry (staying in the old language is safe, mis-translating is
        not)."""
        old = STRINGS.get(old_lang) or {}
        new = STRINGS.get(new_lang) or {}
        votes = {}   # old text → {new text: count}
        for key, text in old.items():
            tgt = new.get(key)
            if not isinstance(text, str) or tgt is None:
                continue
            votes.setdefault(text, {}).setdefault(tgt, 0)
            votes[text][tgt] += 1
        remap = {}
        for text, tally in votes.items():
            best = max(tally.items(), key=lambda kv: kv[1])
            runners = [t for t, n in tally.items()
                       if n == best[1] and t != best[0]]
            if not runners:                     # clear majority/unique
                remap[text] = best[0]
        return remap

    def _retranslate_widgets(self, old_lang):
        """Remap every static UI string in the widget tree to the new
        language (v2.0 — replaces the destroy+rebuild switch).

        Static texts were built from STRINGS[old], so the reverse map
        identifies them; texts it doesn't recognize (user content,
        numbers, paths, formatted strings) are left alone. Dynamic labels
        (file count, mode rows, progress) are recomputed by their owners
        right after this call. Combobox value lists remap entry-wise and
        keep their selection translated.
        """
        remap = self._translation_remap(old_lang, self.lang)
        if not remap:
            return
        for w in self._walk_widgets(self.root):
            try:
                t = w.cget("text")
            except tk.TclError:
                t = None
            if isinstance(t, str) and t in remap:
                try:
                    w.configure(text=remap[t])
                except tk.TclError:
                    pass
            if isinstance(w, ttk.Combobox):
                try:
                    vals = list(w.cget("values"))
                except tk.TclError:
                    vals = []
                if any(str(v) in remap for v in vals):
                    cur = w.get()
                    w.configure(values=[remap.get(str(v), v) for v in vals])
                    if cur in remap:
                        w.set(remap[cur])

    def _set_language(self, lang):
        """Switch UI language live (v2.0): static texts remap in place,
        dynamic labels recompute — nothing is destroyed, so scroll
        position and section state survive. Dialogs still close via
        their WM protocol (they were built with the old language and may
        hold formatted text the remap can't reach)."""
        if lang == self.lang:
            return
        old = self.lang
        self.lang = lang
        self.root.title(self._t("window_title"))
        self._close_dialogs()
        self._retranslate_widgets(old)
        try:
            self.lang_combo.current(0 if lang == "zh" else 1)
        except tk.TclError:
            pass
        # Dynamic labels re-rendered in the new language by their owners
        self._refresh_file_list()
        self._on_mode_change()
        self._update_count_label()
        self._update_stats()
        if not self.processing:
            self.progress_label.config(
                text=self._t("ready"), fg=COLORS["text_secondary"])

    def _bind_global_shortcuts(self):
        """App-wide accelerator keys (Cmd on macOS / Ctrl elsewhere —
        both bound since Tk accepts either). Modifier chords never
        collide with typing in entries. Review/dedup/gallery/start are
        locked out during processing (the toolbar buttons are too);
        add-file/add-folder stay available for queueing. Root bindings
        survive language/theme rebuilds (the root is never destroyed)."""

        def wrap(fn, allow_during_processing=False):
            def handler(event=None):
                if self.processing and not allow_during_processing:
                    return
                fn()
            return handler

        binds = [
            ("<Command-o>", self._add_files, True),
            ("<Control-o>", self._add_files, True),
            ("<Command-O>", self._add_folder, True),
            ("<Control-O>", self._add_folder, True),
            ("<Command-r>", lambda: self._start_processing(), False),
            ("<Control-r>", lambda: self._start_processing(), False),
            ("<Command-p>", self._preview, False),
            ("<Control-p>", self._preview, False),
            ("<Command-e>", self._show_review, False),
            ("<Control-e>", self._show_review, False),
            ("<Command-d>", self._show_dedup, False),
            ("<Control-d>", self._show_dedup, False),
            ("<Command-g>", self._show_gallery_export, False),
            ("<Control-g>", self._show_gallery_export, False),
            ("<Command-1>", lambda: self._show_module("library"), True),
            ("<Control-1>", lambda: self._show_module("library"), True),
            ("<Command-2>", lambda: self._show_module("develop"), True),
            ("<Control-2>", lambda: self._show_module("develop"), True),
            ("<Command-3>", lambda: self._show_module("export"), True),
            ("<Control-3>", lambda: self._show_module("export"), True),
            ("<Command-4>", lambda: self._show_module("tools"), True),
            ("<Control-4>", lambda: self._show_module("tools"), True),
            ("<Command-z>", self._undo, False),
            ("<Control-z>", self._undo, False),
            ("<Command-Z>", self._redo, False),
            ("<Control-Z>", self._redo, False),
        ]
        for seq, fn, allow in binds:
            self.root.bind(seq, wrap(fn, allow))
        self.root.bind("<Escape>", self._on_global_escape)
        # v2.4: Library keyboard rating (1-5 / P) + Enter-to-viewer +
        # row removal keys, and the ? shortcut table. Canvas widgets
        # don't take keyboard focus, so these live on the root with a
        # focus guard — typing in an entry never triggers them.
        for n in range(1, 6):
            self.root.bind(
                str(n), lambda e, n=n: self._lib_key_rate(n))
        self.root.bind("p", lambda e: self._lib_key_rate(0))
        self.root.bind("<Return>", lambda e: self._lib_key_open())
        self.root.bind("<Left>", self._dev_mask_key_page)
        self.root.bind("<Right>", self._dev_mask_key_page)
        self.root.bind("<Left>", self._lib_lightbox_key, add=True)
        self.root.bind("<Right>", self._lib_lightbox_key, add=True)
        self.root.bind("<Delete>", lambda e: self._lib_key_remove())
        self.root.bind("<BackSpace>", lambda e: self._lib_key_remove())
        self.root.bind("<KeyPress-question>",
                       lambda e: self._lib_key_shortcuts())
        self.root.bind("<Command-question>",
                       lambda e: self._lib_key_shortcuts())

    def _on_global_escape(self, event=None):
        """Esc cancels a running batch from the main window. Events in
        Toplevels never reach the root binding, so dialog-level Escape
        handlers keep working. v2.5: exiting the hosted mask editor (and
        the Library lightbox) wins over batch-cancel."""
        if getattr(self, "_dev_mask_mode", False):
            self._dev_exit_mask_mode()
            return "break"
        if getattr(self, "_lib_lightbox_active", False):
            self._exit_library_lightbox()
            return "break"
        if self.processing:
            self._cancel_processing()

    def _toggle_theme(self):
        """Flip between dark and light palette, live (v2.0): palette
        values remap in place across the widget tree, ttk styles follow
        via _configure_ttk_styles — nothing is destroyed. A manual toggle
        pins the choice: the system-appearance follower stands down until
        restart."""
        self.dark_mode = not self.dark_mode
        self._theme_user_override = True
        self._apply_theme_live()

    def _apply_theme_live(self):
        """Re-apply the palette for self.dark_mode without rebuilding."""
        old_colors = dict(COLORS)          # old palette values, by token
        _apply_palette(self.dark_mode)
        remap = {old: COLORS[k] for k, old in old_colors.items()}
        self._configure_ttk_styles()  # styles persist — must follow the palette
        self._close_dialogs()
        self._recolor_widgets(remap)
        self.root.configure(bg=COLORS["bg"])
        try:
            self.theme_btn.configure(
                text="☀️" if self.dark_mode else "🌙")
        except (AttributeError, tk.TclError):
            pass  # button not built yet (early theme flip in tests)
        self._refresh_file_list()
        self._on_mode_change()
        self._update_count_label()
        self._update_stats()
        if not self.processing:
            self.progress_label.config(
                text=self._t("ready"), fg=COLORS["text_secondary"])

    def _recolor_widgets(self, remap):
        """Walk the tree and remap bg/fg values from the old palette to
        the new one. Widgets colored outside the palette (HSL chips, the
        color wheel) don't match any entry and stay untouched."""
        for w in self._walk_widgets(self.root):
            for opt in ("bg", "fg"):
                try:
                    cur = w.cget(opt)
                except tk.TclError:
                    continue
                if cur in remap:
                    try:
                        w.configure(**{opt: remap[cur]})
                    except tk.TclError:
                        pass
            # FlatButton extras not exposed through bg/fg cget
            hover = getattr(w, "_hover_bg", None)
            if hover in remap:
                try:
                    w.configure(hover_bg=remap[hover])
                except tk.TclError:
                    pass
            border = getattr(w, "_border", None)
            if border in remap:
                try:
                    w.configure(border_color=remap[border])
                except tk.TclError:
                    pass

    # ── System appearance follower (v2.0) ────────────────────────────────────
    # Re-checks the OS appearance on window focus and every 30 s; when it
    # changed AND the user hasn't toggled manually, the theme flips live.
    # A manual toggle pins the choice until restart.

    def _schedule_theme_poll(self):
        try:
            self._theme_poll_after = self.root.after(
                30_000, self._poll_system_theme)
        except tk.TclError:
            pass  # root already gone

    def _poll_system_theme(self):
        self._recheck_system_theme()
        self._schedule_theme_poll()

    def _recheck_system_theme(self):
        if self._theme_user_override or self.processing:
            return
        try:
            want = _system_dark_mode()
        except Exception:
            return
        if want != self.dark_mode:
            self.dark_mode = want
            self._apply_theme_live()

    def _on_focus_theme_check(self, _event=None):
        self._recheck_system_theme()

    # ── Layout memory (~/.photos/gui_state.json) ─────────────────────────────

    @staticmethod
    def _state_file():
        return state_file()

    def _load_gui_state(self) -> dict:
        """Restore window geometry / thumbnail size across restarts."""
        state = load_state()
        try:
            self._thumb_size = int(state.get("thumb_size",
                                             self._thumb_size))
            if self._thumb_size not in (48, 96, 144):
                self._thumb_size = 96
        except Exception:
            pass
        self._first_run_done = bool(state.get("first_run_done", False))
        recipes = state.get("export_recipes")
        if isinstance(recipes, dict):
            # consumed in __init__ after _export_recipes is initialized            # (_load_gui_state runs earlier in startup than that block)
            self._pending_export_recipes = {
                str(k): dict(v) for k, v in recipes.items()
                if isinstance(v, dict)}
        return state

    def _save_gui_state(self):
        """Persist window geometry + thumbnail size; never crashes on save."""
        save_state({
            "geometry": self.root.geometry(),
            "thumb_size": self._thumb_size,
            "module": self._active_module,
            "export_recipes": self._export_recipes,
            "first_run_done": bool(getattr(self, "_first_run_done", False)),
        })

    def _on_main_close(self):
        # Request cancellation of any in-flight batch before the Tk loop
        # dies — worker callbacks into destroyed widgets crash noisily, and
        # preview temp dirs leaked on close.
        try:
            if getattr(self, "processing", False):
                self._cancel_processing()
        except Exception:
            pass
        import shutil
        for td in getattr(self, "_preview_tempdirs", ()) or ():
            shutil.rmtree(td, ignore_errors=True)
        self._save_gui_state()
        self.root.destroy()

    def _on_language_selected(self, _event=None):
        display = self.lang_combo.get()
        lang = "zh" if display == "中文" else "en"
        self._set_language(lang)
        # Persist the user's choice so it survives restarts (auto-detect only
        # applies on the first launch / when nothing is stored).
        from .. import i18n
        i18n.save_language(lang)

    def _configure_ttk_styles(self):
        """Tune ttk widget appearance for a cleaner look.

        Runs at startup AND after every theme switch — ttk styles persist
        across widget rebuilds, so stale style colors would leave widgets
        (entries, sliders, comboboxes, treeview) stuck on the previous
        palette. Also switches to the 'clam' theme: the macOS-native
        'aqua' theme draws most ttk widgets with native controls that
        follow the OS appearance and ignore style colors entirely.
        """
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass  # clam ships with every Tk; never fail on it
        style.configure("Treeview", rowheight=26, font=FONT_BODY,
                        fieldbackground=COLORS["card"],
                        background=COLORS["card"],
                        foreground=COLORS["text"],
                        relief="flat", borderwidth=0)
        style.configure("Treeview.Heading", font=FONT_SMALL, padding=(4, 5),
                        background=COLORS["card"], foreground=COLORS["text"],
                        relief="flat")
        style.map("Treeview", background=[("selected", COLORS["accent"])],
                  foreground=[("selected", "white")])
        style.configure("TCombobox", padding=2, fieldbackground=COLORS["card"],
                        background=COLORS["card"], foreground=COLORS["text"],
                        arrowcolor=COLORS["text_secondary"],
                        bordercolor=COLORS["border"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLORS["card"])],
                  selectbackground=[("readonly", COLORS["accent"])],
                  selectforeground=[("readonly", "white")])
        style.configure("TEntry", fieldbackground=COLORS["card"],
                        foreground=COLORS["text"],
                        insertcolor=COLORS["text"],
                        bordercolor=COLORS["border"],
                        lightcolor=COLORS["border"],
                        darkcolor=COLORS["border"])
        style.configure("TScale", background=COLORS["accent"],
                        troughcolor=COLORS["divider"])
        style.configure("TCheckbutton", background=COLORS["card"],
                        foreground=COLORS["text"], focuscolor=COLORS["card"])
        style.configure("TRadiobutton", background=COLORS["card"],
                        foreground=COLORS["text"], focuscolor=COLORS["card"])
        style.configure("TProgressbar", background=COLORS["accent"],
                        troughcolor=COLORS["progress_bg"],
                        bordercolor=COLORS["progress_bg"],
                        lightcolor=COLORS["accent"],
                        darkcolor=COLORS["accent"])
        # Arrow-less slim scrollbars (modern look): redefine the clam layout
        # to trough + thumb only.
        for orient in ("Vertical", "Horizontal"):
            style.layout(
                orient + ".TScrollbar",
                [(orient + ".Scrollbar.trough",
                  {"children": [(orient + ".Scrollbar.thumb",
                                 {"expand": "1", "sticky": "nswe"})],
                   "sticky": "ns" if orient == "Vertical" else "we"})])
            style.configure(
                orient + ".TScrollbar", background=COLORS["border"],
                troughcolor=COLORS["card"],
                arrowcolor=COLORS["text_secondary"],
                bordercolor=COLORS["card"], lightcolor=COLORS["card"],
                darkcolor=COLORS["card"])

    # ── UI Construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        """Build the entire UI layout."""
        # ── Title bar ───────────────────────────────────────────────────────
        title_frame = tk.Frame(self.root, bg=COLORS["bg"])
        title_frame.pack(fill="x", padx=22, pady=(18, 0))

        title_label = tk.Label(
            title_frame, text=APP_NAME,
            font=FONT_TITLE, fg=COLORS["text"], bg=COLORS["bg"],
        )
        title_label.pack(side="left")

        version_label = tk.Label(
            title_frame, text=f"v{APP_VERSION}",
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg"],
        )
        version_label.pack(side="left", padx=(8, 0), pady=(8, 0))

        subtitle_label = tk.Label(
            title_frame, text=self._t("subtitle"),
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg"],
        )
        subtitle_label.pack(side="left", padx=(16, 0), pady=(8, 0))

        # About button (right side)
        about_btn = FlatButton(
            title_frame, text=self._t("about"), command=self._show_about,
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            hover_bg=COLORS["divider"], font=FONT_SMALL, padx=10, pady=4,
        )
        about_btn.pack(side="right", pady=(6, 0))

        # Plugins button (right side, next to About)
        plugins_btn = FlatButton(
            title_frame, text=self._t("plugins"), command=self._show_plugin_manager,
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            hover_bg=COLORS["divider"], font=FONT_SMALL, padx=10, pady=4,
        )
        plugins_btn.pack(side="right", pady=(6, 0))

        # Settings button (right side)
        settings_btn = FlatButton(
            title_frame, text=self._t("settings"), command=self._show_settings,
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            hover_bg=COLORS["divider"], font=FONT_SMALL, padx=10, pady=4,
        )
        settings_btn.pack(side="right", pady=(6, 0))

        # Language selector (right side)
        self.lang_combo = ttk.Combobox(
            title_frame, values=["中文", "English"], state="readonly",
            font=FONT_SMALL, width=8,
        )
        self.lang_combo.current(0 if self.lang == "zh" else 1)
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        self.lang_combo.pack(side="right", padx=(0, 8), pady=(6, 0))

        # Theme toggle (right side, before language). Stored as an
        # attribute: the icon flips live on theme switches (no rebuild).
        theme_icon = "☀️" if self.dark_mode else "🌙"
        self.theme_btn = FlatButton(
            title_frame, text=theme_icon, command=self._toggle_theme,
            bg=COLORS["bg"], fg=COLORS["text_secondary"],
            hover_bg=COLORS["divider"], font=FONT_SMALL, padx=8, pady=4,
        )
        self.theme_btn.pack(side="right", padx=(0, 4), pady=(6, 0))

        # ── Module bar (v2.0 workspace) ─────────────────────────────────────
        module_bar = tk.Frame(self.root, bg=COLORS["bg"])
        module_bar.pack(fill="x", padx=22, pady=(12, 0))

        self._module_btns = {}
        for name in MODULES:
            btn = FlatButton(
                module_bar, text=self._t("module_" + name),
                command=lambda n=name: self._show_module(n),
                bg=COLORS["bg"], fg=COLORS["text_secondary"],
                hover_bg=COLORS["divider"], border_color=COLORS["bg"],
                font=FONT_BUTTON, padx=18, pady=6,
            )
            btn.pack(side="left", padx=(0, 6))
            self._module_btns[name] = btn

        # ── Content area: one frame per module, switched by packing ────────
        # All four are built once at startup (widgets/tests rely on them
        # existing regardless of visibility); _show_module only (un)packs.
        content = tk.Frame(self.root, bg=COLORS["bg"])
        content.pack(fill="both", expand=True, padx=22, pady=(10, 8))

        self._module_frames = {}
        for name in MODULES:
            self._module_frames[name] = tk.Frame(content, bg=COLORS["bg"])

        # Library: the file list, full width (v2.0 — was the left column)
        self._build_file_panel(self._module_frames["library"])
        # Develop: preview workspace — the edit tools live beside the
        # preview (histogram + ADJUST tab in the right column)
        self._build_develop_panel(self._module_frames["develop"])
        # Export: the queue of finished photos + the export settings
        export_row = tk.Frame(self._module_frames["export"], bg=COLORS["bg"])
        export_row.pack(fill="both", expand=True)
        self._build_export_queue(export_row)
        export_col = tk.Frame(export_row, bg=COLORS["bg"],
                              width=SETTINGS_WIDTH)
        export_col.pack(side="right", fill="y", padx=(10, 0))
        export_col.pack_propagate(False)
        # v2.2: named export recipes (persisted in gui state) — packed
        # before the settings notebook so it stays at the column top
        recipe_card = tk.Frame(export_col, bg=COLORS["card"], bd=0,
                               highlightthickness=0)
        recipe_card.pack(fill="x", pady=(0, 10))
        tk.Label(recipe_card, text=self._t("export_recipes"),
                 font=FONT_SMALL, fg=COLORS["text"], bg=COLORS["card"],
                 anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        recipe_row = tk.Frame(recipe_card, bg=COLORS["card"])
        recipe_row.pack(fill="x", padx=10, pady=(0, 4))
        self._recipe_var = tk.StringVar(value="")
        self._recipe_combo = ttk.Combobox(
            recipe_row, textvariable=self._recipe_var, font=FONT_SMALL,
            values=sorted(self._export_recipes), state="readonly")
        self._recipe_combo.pack(side="left", fill="x", expand=True)
        FlatButton(
            recipe_row, text=self._t("recipe_apply"),
            command=self._export_recipe_apply, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=10,
            pady=3).pack(side="left", padx=(6, 0))
        FlatButton(
            recipe_row, text=self._t("recipe_save"),
            command=self._export_recipe_save, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=10,
            pady=3).pack(side="left", padx=(6, 0))
        FlatButton(
            recipe_row, text=self._t("recipe_delete"),
            command=self._export_recipe_delete, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=10,
            pady=3).pack(side="left", padx=(6, 0))
        self._recipe_status_lbl = tk.Label(recipe_card, text="",
                                           font=FONT_TINY,
                                           fg=COLORS["text_secondary"],
                                           bg=COLORS["card"], anchor="w")
        self._recipe_status_lbl.pack(fill="x", padx=10, pady=(0, 8))
        self._build_settings_panel(export_col, self._dev_settings_host)
        # Tools: workflow launchers
        self._build_tools_panel(self._module_frames["tools"])

        self._show_module(self._active_module)

        # ── Bottom: progress and actions ────────────────────────────────────
        bottom_frame = tk.Frame(self.root, bg=COLORS["bg"])
        bottom_frame.pack(fill="x", padx=22, pady=(0, 18))

        self._build_bottom_panel(bottom_frame)

    # ── Workspace modules (v2.0) ────────────────────────────────────────────

    # Tools module: (title key, description key, PhotoSApp opener method).
    # Each card launches the existing workflow dialog; in-place non-modal
    # panels are the follow-up batch (plan §4.2).
    TOOLS_CARDS = (
        ("review_title", "tools_desc_review", "_show_review"),
        ("dedup_title", "tools_desc_dedup", "_show_dedup"),
        ("compare_view_title", "tools_desc_compare", "_show_compare"),
        ("gallery_title", "tools_desc_gallery", "_show_gallery_export"),
        ("contact_title", "tools_desc_contact", "_show_contact_sheet"),
        ("cull_title", "tools_desc_cull", "_show_cull"),
        ("hash_title", "tools_desc_hash", "_show_hash"),
        ("hdr_title", "tools_desc_hdr", "_show_hdr"),
        ("watch_title", "tools_desc_watch", "_show_watch"),
        ("rename_title", "tools_desc_rename", "_show_rename"),
        ("presets_title", "tools_desc_presets", "_show_presets"),
        ("analyze_title", "tools_desc_analyze", "_show_analysis"),
    )

    # v2.2 copy/paste settings: the ProcessOptions fields that count as
    # "develop" (adjustment) state. Copy grabs exactly these; paste writes
    # them per photo. masks / mask_adjust are deliberately excluded —
    # per-photo masks flow through the dedicated _photo_masks channel the
    # mask dialog owns; a second channel would race the first.
    _DEV_FIELDS = (
        "brightness", "contrast", "saturation", "gamma", "sharpen",
        "export_sharpen", "ev", "auto_exposure", "vibrance", "clarity",
        "texture", "dehaze", "denoise", "wb_temp", "wb_tint",
        "highlight_recovery", "lens_distort",
        "grayscale", "sepia", "auto_levels", "auto_rotate", "auto_straighten",
        "log_curve", "lut_file", "wb_reference", "levels", "curves",
        "color_grading", "hsl", "vignette", "grain", "point_color",
        "lens_vignette", "lens_ca", "lens_profile", "crop", "crop_ratio",
        "rotate_bg", "flip", "pad_ratio", "pad_bg",
    )

    # Export recipes capture the output-side fields (format / size / naming /
    # watermark / color-for-output). Values are canonical ProcessOptions
    # values, so a recipe survives language switches and preset round-trips.
    _EXPORT_RECIPE_FIELDS = (
        "quality", "output_format", "output_dir", "prefix", "suffix",
        "max_width", "max_height", "max_pixels", "scale_percent",
        "target_size_bytes", "optimize", "progressive", "jpeg_subsampling",
        "srgb", "flatten_cmyk", "preserve_exif", "strip_gps",
        "watermark_text", "watermark_image", "watermark_position",
        "watermark_opacity", "output_sizes",
    )

    _DEV_HISTORY_MAX = 50

    def _show_module(self, name):
        """Switch the visible workspace module. Pure pack switching — no
        widget is created or destroyed, so module state (filmstrip,
        scroll positions) survives round-trips."""
        if name not in MODULES:
            name = "library"
        self._active_module = name
        for frame in self._module_frames.values():
            frame.pack_forget()
        self._module_frames[name].pack(fill="both", expand=True)
        for n, btn in self._module_btns.items():
            if n == name:
                btn.configure(bg=COLORS["card"], fg=COLORS["text"],
                              border_color=COLORS["border"])
            else:
                btn.configure(bg=COLORS["bg"], fg=COLORS["text_secondary"],
                              border_color=COLORS["bg"])
        if name == "develop":
            self._dev_refresh_filmstrip()
        elif name == "export":
            self._refresh_export_queue()

    def _build_tools_panel(self, parent):
        """Tools module: a grid of workflow launcher cards. The cards are
        ordinary widgets, so the batch lockout (_toggle_settings) disables
        them like the toolbar buttons."""
        holder = tk.Frame(parent, bg=COLORS["bg"])
        holder.pack(fill="both", expand=True)
        grid = tk.Frame(holder, bg=COLORS["bg"])
        grid.pack(anchor="n", fill="x", pady=(4, 0))
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="tools")
        for i, (title_key, desc_key, opener) in enumerate(self.TOOLS_CARDS):
            card = tk.Frame(grid, bg=COLORS["card"], bd=0,
                            highlightthickness=0)
            card.grid(row=i // 3, column=i % 3, sticky="nsew",
                      padx=(0, 8) if i % 3 < 2 else 0, pady=(0, 8))
            inner = tk.Frame(card, bg=COLORS["card"])
            inner.pack(fill="both", expand=True, padx=12, pady=(10, 4))
            tk.Label(inner, text=self._t(title_key), font=FONT_SECTION,
                     fg=COLORS["text"], bg=COLORS["card"],
                     anchor="w").pack(fill="x")
            tk.Label(inner, text=self._t(desc_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"],
                     anchor="w", wraplength=220,
                     justify="left").pack(fill="x", pady=(4, 8))
            FlatButton(
                inner, text=self._t("tools_open"),
                command=getattr(self, opener),
                bg=COLORS["accent"],
                hover_bg=COLORS["accent_hover"], font=FONT_SMALL,
                padx=12, pady=4,
            ).pack(anchor="w", pady=(0, 8))

    # ── Export module: queue of finished photos (v2.0) ──────────────────────

    def _build_export_queue(self, parent):
        """Export module left card: the photos that will be exported
        (the checked set — check state is managed in Library)."""
        card = tk.Frame(parent, bg=COLORS["card"], bd=0, highlightthickness=0)
        card.pack(side="left", fill="both", expand=True)

        head = tk.Frame(card, bg=COLORS["card"])
        head.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(head, text=self._t("export_queue"), font=FONT_SECTION,
                 fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w")
        tk.Label(head, text=self._t("export_queue_hint"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["card"],
                 wraplength=360, justify="left").pack(
            anchor="w", pady=(2, 6))
        # v2.2: paste Develop settings onto every checked photo at once
        self._paste_checked_btn = FlatButton(
            head, text=self._t("paste_to_checked"),
            command=self._paste_settings_to_checked, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._paste_checked_btn.pack(anchor="w", pady=(0, 8))

        holder = tk.Frame(card, bg=COLORS["card"])
        holder.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        canvas = tk.Canvas(holder, bg=COLORS["card"], highlightthickness=0,
                           bd=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._export_queue_inner = tk.Frame(canvas, bg=COLORS["card"])
        qwin = canvas.create_window((0, 0), window=self._export_queue_inner,
                                    anchor="nw")

        def _sync(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(qwin, width=canvas.winfo_width())
        self._export_queue_inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)

        def _wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>",
                    lambda e: canvas.unbind_all("<MouseWheel>"))
        self._refresh_export_queue()

    def _refresh_export_queue(self):
        """Rebuild the export queue rows (called on file changes and when
        the Export module is shown)."""
        if not hasattr(self, "_export_queue_inner"):
            return
        inner = self._export_queue_inner
        for w in inner.winfo_children():
            w.destroy()
        files = self._checked_files()
        if not files:
            tk.Label(inner, text=self._t("export_queue_empty"),
                     font=FONT_SMALL, fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).pack(anchor="w", pady=8)
            return
        total = 0
        adjusted = 0
        for i, p in enumerate(files):
            row = tk.Frame(inner, bg=COLORS["card"])
            row.pack(fill="x", pady=1)
            try:
                sz = os.path.getsize(p)
                total += sz
                size_txt = format_size(sz)
            except OSError:
                size_txt = "—"
            tk.Label(row, text="{}. {}".format(i + 1, os.path.basename(p)),
                     font=FONT_SMALL, fg=COLORS["text"], bg=COLORS["card"],
                     anchor="w").pack(side="left")
            if p in self._photo_adjust:
                adjusted += 1
                tk.Label(row, text=self._t("export_adjusted_badge"),
                         font=FONT_TINY, fg=COLORS["accent"],
                         bg=COLORS["card"]).pack(side="left", padx=(6, 0))
            tk.Label(row, text=size_txt, font=FONT_TINY,
                     fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).pack(side="right")
        foot = tk.Frame(inner, bg=COLORS["card"])
        foot.pack(fill="x", pady=(6, 0))
        summary = "{} · {}".format(
            self._t("files_count", n=len(files)), format_size(total))
        if adjusted:
            summary += " · {}".format(
                self._t("export_adjusted_count", n=adjusted))
        tk.Label(foot, text=summary,
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(anchor="w")

    # ── Develop module: preview workspace (v2.0) ────────────────────────────

    def _build_develop_panel(self, parent):
        """Develop: filmstrip + large preview rendered through the REAL
        pipeline (debounced, like the preview dialog) + a persistent
        histogram & exposure readout of the processed result."""
        body = tk.Frame(parent, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        # Filmstrip: horizontally scrolling thumbnails of self.files
        strip_canvas = tk.Canvas(parent, bg=COLORS["card"], height=92,
                                 highlightthickness=0)
        strip_canvas.pack(fill="x")
        self._dev_strip_canvas = strip_canvas
        self._dev_strip = tk.Frame(strip_canvas, bg=COLORS["card"])
        win_id = strip_canvas.create_window((0, 0), window=self._dev_strip,
                                            anchor="nw")

        def _sync_strip(_event=None):
            strip_canvas.configure(scrollregion=strip_canvas.bbox("all"))
            strip_canvas.itemconfigure(
                win_id, width=self._dev_strip.winfo_reqwidth())
        self._dev_strip.bind("<Configure>", _sync_strip)

        def _strip_wheel(event):
            strip_canvas.xview_scroll(-1 if event.delta > 0 else 1,
                                      "units")
        strip_canvas.bind(
            "<Enter>", lambda e: strip_canvas.bind_all(
                "<MouseWheel>", _strip_wheel))
        strip_canvas.bind(
            "<Leave>", lambda e: strip_canvas.unbind_all("<MouseWheel>"))

        # Main row: viewer card + analysis sidebar
        main = tk.Frame(body, bg=COLORS["bg"])
        main.pack(fill="both", expand=True, pady=(8, 0))

        side = tk.Frame(main, bg=COLORS["bg"], width=SETTINGS_WIDTH)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        self._dev_side = side

        viewer_card = tk.Frame(main, bg=COLORS["card"], bd=0,
                               highlightthickness=0)
        viewer_card.pack(side="left", fill="both", expand=True)
        self._dev_viewer_card = viewer_card
        # v2.5: 蒙版编辑器宿主（pack 切换见 _dev_mask_apply_layout）
        self._dev_mask_host = tk.Frame(main, bg=COLORS["bg"])

        top = tk.Frame(viewer_card, bg=COLORS["card"])
        top.pack(fill="x", padx=12, pady=(8, 0))
        self._dev_nav_lbl = tk.Label(top, text="", font=FONT_SMALL,
                                     fg=COLORS["text_secondary"],
                                     bg=COLORS["card"])
        self._dev_nav_lbl.pack(side="left")
        self._dev_status_lbl = tk.Label(top, text="", font=FONT_SMALL,
                                        fg=COLORS["text_secondary"],
                                        bg=COLORS["card"])
        self._dev_status_lbl.pack(side="right")

        self._dev_image_lbl = tk.Label(viewer_card, bg=COLORS["card"],
                                       fg=COLORS["text_secondary"],
                                       font=FONT_BODY)
        self._dev_image_lbl.pack(expand=True, fill="both", pady=8)
        # v2.4: compare overlay (split / side-by-side) — placed over the
        # plain label only while a compare mode is active
        self._dev_compare_canvas = tk.Canvas(
            viewer_card, bg=COLORS["card"], highlightthickness=0)
        self._dev_compare_canvas.bind("<Button-1>", self._dev_compare_drag)
        self._dev_compare_canvas.bind("<B1-Motion>",
                                      self._dev_compare_drag)

        bottom_bar = tk.Frame(viewer_card, bg=COLORS["card"])
        bottom_bar.pack(fill="x", padx=12, pady=(0, 10))
        self._dev_toggle_btn = FlatButton(
            bottom_bar, text=self._t("dev_show_before"),
            command=self._dev_toggle_view, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12,
            pady=4)
        self._dev_toggle_btn.pack(side="left")
        # v2.2: copy/paste adjustments + per-photo undo/redo
        self._dev_copy_btn = FlatButton(
            bottom_bar, text=self._t("dev_copy_settings"),
            command=self._dev_copy_settings, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._dev_copy_btn.pack(side="left", padx=(8, 0))
        self._dev_paste_btn = FlatButton(
            bottom_bar, text=self._t("dev_paste_settings"),
            command=self._dev_paste_settings, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._dev_paste_btn.pack(side="left", padx=(8, 0))
        self._dev_undo_btn = FlatButton(
            bottom_bar, text=self._t("dev_undo"), command=self._dev_undo,
            bg=COLORS["bg"], fg=COLORS["text"],
            hover_bg=COLORS["divider"], border_color=COLORS["border"],
            font=FONT_SMALL, padx=12, pady=4)
        self._dev_undo_btn.configure(state=tk.DISABLED)
        self._dev_undo_btn.pack(side="left", padx=(8, 0))
        self._dev_redo_btn = FlatButton(
            bottom_bar, text=self._t("dev_redo"), command=self._dev_redo,
            bg=COLORS["bg"], fg=COLORS["text"],
            hover_bg=COLORS["divider"], border_color=COLORS["border"],
            font=FONT_SMALL, padx=12, pady=4)
        self._dev_redo_btn.configure(state=tk.DISABLED)
        self._dev_redo_btn.pack(side="left", padx=(8, 0))
        # v2.4: AI auto-tone (official plugin) — predicted params land in the
        # per-photo overlay, so they stay editable / undoable / previewed
        self._dev_ai_btn = FlatButton(
            bottom_bar, text=self._t("dev_ai_tone"),
            command=self._dev_ai_tone, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._dev_ai_btn.pack(side="left", padx=(12, 0))
        self._dev_ai_strength = tk.StringVar(value="0.8")
        tk.Label(bottom_bar, text=self._t("dev_ai_strength"), font=FONT_TINY,
                 fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="left", padx=(6, 2))
        self._dev_ai_strength_box = ttk.Combobox(
            bottom_bar, textvariable=self._dev_ai_strength, width=4,
            values=("1.0", "0.8", "0.6", "0.4", "0.2"), state="readonly")
        self._dev_ai_strength_box.pack(side="left")
        # v2.5: mask canvas promoted into Develop (was a popup dialog)
        self._dev_mask_btn = FlatButton(
            bottom_bar, text=self._t("dev_mask_edit"),
            command=self._dev_enter_mask_mode, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._dev_mask_btn.pack(side="left", padx=(12, 0))
        # v2.4: before/after compare — split (draggable divider) and
        # side-by-side, composed from the pipeline-rendered pair
        self._dev_compare_btn = FlatButton(
            bottom_bar, text=self._t("dev_compare_split"),
            command=self._dev_compare_toggle, bg=COLORS["bg"],
            fg=COLORS["text"], hover_bg=COLORS["divider"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=12, pady=4)
        self._dev_compare_btn.pack(side="left", padx=(12, 0))
        tk.Label(bottom_bar, text=self._t("dev_hint"), font=FONT_TINY,
                 fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="right")

        # Sidebar: histogram + readouts (rebuilt per render)
        hist_card = tk.Frame(side, bg=COLORS["card"], bd=0,
                             highlightthickness=0)
        hist_card.pack(fill="x")
        tk.Label(hist_card, text=self._t("dev_hist_title"),
                 font=FONT_SMALL, fg=COLORS["text"], bg=COLORS["card"],
                 anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        self._dev_hist = tk.Canvas(hist_card, height=120,
                                   bg=COLORS["card"], highlightthickness=0)
        self._dev_hist.pack(fill="x", padx=10)
        self._dev_expo_lbl = tk.Label(hist_card, text="", font=FONT_SMALL,
                                      fg=COLORS["text_secondary"],
                                      bg=COLORS["card"], anchor="w",
                                      justify="left")
        self._dev_expo_lbl.pack(fill="x", padx=10, pady=(8, 2))
        self._dev_kelvin_lbl = tk.Label(hist_card, text="", font=FONT_SMALL,
                                        fg=COLORS["text_secondary"],
                                        bg=COLORS["card"], anchor="w")
        self._dev_kelvin_lbl.pack(fill="x", padx=10, pady=(0, 10))

        # v2.4: preset browser — hover previews live through the dev
        # pipeline (a render-options override; sliders are untouched),
        # click applies the preset for real
        preset_card = tk.Frame(side, bg=COLORS["card"], bd=0,
                               highlightthickness=0)
        preset_card.pack(fill="x", pady=(8, 0))
        tk.Label(preset_card, text=self._t("preset_browser"),
                 font=FONT_SMALL, fg=COLORS["text"], bg=COLORS["card"],
                 anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        self._dev_preset_list = tk.Listbox(
            preset_card, height=4, font=FONT_SMALL,
            exportselection=False, bg=COLORS["bg"], fg=COLORS["text"],
            relief="flat", highlightthickness=1,
            highlightbackground=COLORS["border"])
        self._dev_preset_list.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(preset_card, text=self._t("preset_hover_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"], anchor="w").pack(
            fill="x", padx=10, pady=(0, 6))
        self._dev_preset_list.bind("<Motion>", self._dev_preset_hover)
        self._dev_preset_list.bind("<Leave>", self._dev_preset_leave)
        self._dev_preset_list.bind("<<ListboxSelect>>",
                                   self._dev_preset_apply)
        self._dev_refresh_presets()

        # Edit tools host: _build_settings_panel fills this with the
        # ADJUST tab (tone / grading / correction) below the histogram.
        self._dev_settings_host = tk.Frame(side, bg=COLORS["card"], bd=0,
                                           highlightthickness=0)
        self._dev_settings_host.pack(fill="both", expand=True, pady=(8, 0))

        self._dev_display_current()
        # Debounce tick (~500ms stability ≈ 3 × 160ms), like _preview
        self.root.after(160, self._dev_tick)

    def _dev_tick(self):
        """Poll loop: drain worker callbacks, then re-render the selected
        photo once the options signature has been stable. Runs for the
        app's lifetime; cheap no-op unless the Develop module is active."""
        try:
            self._dev_bus.drain_pending()
            st = self._dev_render_state
            if (self._active_module == "develop" and self._dev_selected
                    and not self.processing):
                cur = self._dev_current_sig()
                if cur != st["sig"]:
                    # v2.2: an options change on the SAME photo is an edit —
                    # persist it as the photo's develop overlay (a path
                    # change is a selection, never an edit). The first edit
                    # seeds the undo history with the pre-edit baseline.
                    prev = st["sig"]
                    if (prev is not None and prev[1] == cur[1]
                            and isinstance(prev[0], ProcessOptions)
                            and self._dev_preset_preview is None):
                        if cur[1] not in self._photo_adjust:
                            self._dev_history_push(
                                cur[1], self._dev_fields_of(prev[0]))
                        self._photo_adjust[cur[1]] = \
                            self._dev_fields_of(cur[0])
                    st["sig"] = cur
                    st["stable"] = 0
                else:
                    st["stable"] += 1
                if (st["stable"] >= 3 and not st["inflight"]
                        and cur != st["rendered"]):
                    self._dev_render(cur)
            self.root.after(160, self._dev_tick)
        except tk.TclError:
            pass  # root gone — loop dies with it

    def _dev_current_sig(self):
        # v2.4: a hovered preset owns the preview render — its options
        # replace the live ones without touching a single variable
        if self._dev_preset_preview is not None:
            return (self._dev_preset_preview, self._dev_selected)
        return (self._build_options(), self._dev_selected)

    def _dev_render(self, sig):
        """Launch a real-pipeline render of the selected file (worker
        thread; results marshalled back via the dev bus)."""
        st = self._dev_render_state
        opts, path = sig
        if not st["tempdir"]:
            st["tempdir"] = tempfile.mkdtemp(prefix="photos_devpreview_")
            self._preview_tempdirs.add(st["tempdir"])
        st["inflight"] = True
        # v2.2: a render of the SAME photo as the previous one is a settled
        # edit — snapshot it into the undo history (deduped inside; first
        # selections / photo switches never reach this branch)
        prev = st["rendered"]
        if (prev is not None and prev[1] == path
                and self._dev_preset_preview is None):
            self._dev_history_push(path, self._dev_fields_of(opts))
        render_opts = workflows.preview_options(opts, st["tempdir"])
        self._dev_status_lbl.configure(text=self._t("dev_rendering"),
                                       fg=COLORS["text_secondary"])

        def work():
            try:
                result = workflows.preview_render(path, render_opts)
            except Exception:
                result = None
            self._dev_bus.schedule(
                lambda: self._dev_render_done(result, sig))
        threading.Thread(target=work, daemon=True).start()

    def _dev_render_done(self, result, sig):
        st = self._dev_render_state
        st["inflight"] = False
        if sig != self._dev_current_sig():
            return  # stale — selection or options moved on
        if result is None or not getattr(result, "success", False):
            err = (getattr(result, "error", None) or "unknown")[:120]
            self._dev_status_lbl.configure(
                text=self._t("dev_render_failed", err=err),
                fg=COLORS["danger"])
            return
        st["rendered"] = sig
        try:
            from PIL import Image
            img = Image.open(result.output_path)
            img.load()
            st["after_pil"] = img
            st["after_photo"] = self._dev_fit_photo(img)
        except Exception:
            return
        self._dev_status_lbl.configure(text="")
        try:
            from ..metrics import analyze_image
            analysis = analyze_image(result.output_path)
        except Exception:
            analysis = {}
        self._dev_draw_analysis(analysis)
        self._dev_display_current()

    # ── v2.4: preset browser (hover = live preview, click = apply) ────────

    def _dev_refresh_presets(self):
        """Reload the preset list (names only; bodies load on hover)."""
        from .. import presets as presets_mod
        try:
            names = presets_mod.list_presets()
        except Exception:
            names = []
        lb = getattr(self, "_dev_preset_list", None)
        if lb is None:
            return
        try:
            lb.delete(0, tk.END)
            for n in names:
                lb.insert(tk.END, n)
        except tk.TclError:
            pass

    def _dev_preset_hover(self, event):
        """Hovering a preset swaps the preview's render options — the
        sliders, overlays and undo history are never touched."""
        lb = self._dev_preset_list
        idx = lb.nearest(event.y)
        if idx < 0 or idx >= lb.size():
            return
        name = lb.get(idx)
        if name == self._dev_preset_hovered:
            return
        self._dev_preset_hovered = name
        from .. import presets as presets_mod
        try:
            self._dev_preset_preview = presets_mod.load_preset(name)
        except Exception:
            self._dev_preset_preview = None

    def _dev_preset_leave(self, _event=None):
        self._dev_preset_hovered = ""
        self._dev_preset_preview = None

    def _dev_preset_apply(self, _event=None):
        """Click: apply the selected preset to the UI (the same path the
        preset dialog uses), then clear the hover preview."""
        lb = self._dev_preset_list
        try:
            sel = lb.curselection()
        except tk.TclError:
            sel = ()
        if not sel:
            return
        name = lb.get(sel[0])
        from .. import presets as presets_mod
        try:
            opts = presets_mod.load_preset(name)
        except Exception:
            opts = None
        self._dev_preset_leave()
        try:
            lb.selection_clear(0, tk.END)
        except tk.TclError:
            pass
        if opts is not None:
            self._apply_options_to_ui(opts)

    def _dev_fit_photo(self, img):
        """Fit a PIL image into the viewer label (LANCZOS thumbnail) and
        wrap it as a PhotoImage. Must run on the UI thread."""
        from PIL import Image, ImageTk
        w = max(200, self._dev_image_lbl.winfo_width() - 16)
        h = max(150, self._dev_image_lbl.winfo_height() - 16)
        im = img.convert("RGB")
        im.thumbnail((w, h), Image.LANCZOS)
        return ImageTk.PhotoImage(im, master=self._dev_image_lbl)

    def _dev_toggle_view(self):
        """Flip before/after. The button label always names the OTHER
        view (what clicking will show)."""
        show_before = not self._dev_show_before.get()
        self._dev_show_before.set(show_before)
        self._dev_toggle_btn.configure(
            text=self._t("dev_show_before" if not show_before
                         else "dev_show_after"))
        self._dev_display_current()

    # ── v2.2 editing efficiency: copy/paste + per-photo undo/redo ───────────

    def _dev_fields_of(self, opts) -> dict:
        """Snapshot of the develop (adjustment) fields from a
        ProcessOptions — the copy/paste/undo currency."""
        return {f: getattr(opts, f, None) for f in self._DEV_FIELDS}

    def _apply_dev_fields(self, d: dict) -> None:
        """Map develop fields back onto the GUI vars — a scoped version of
        _apply_options_to_ui that never touches output/export fields.
        Forgiving per field, like its parent."""
        def _fmt(v):
            if isinstance(v, float) and v == int(v):
                return str(int(v))
            return str(v)

        norm = dict(d)
        for k in ("export_sharpen", "highlight_recovery", "wb_tint"):
            if norm.get(k) is None:
                norm[k] = 0.0
        for name in self._DEV_FIELDS:
            var = getattr(self, name, None)
            v = norm.get(name)
            try:
                if v is None:
                    if isinstance(var, tk.StringVar):
                        var.set("")
                    elif isinstance(var, tk.BooleanVar):
                        var.set(False)
                    continue
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(v))
                elif isinstance(var, tk.StringVar):
                    var.set(v if isinstance(v, str) else _fmt(v))
                else:
                    var.set(v)
            except Exception:
                pass

    def _dev_history_push(self, path: str, d: dict) -> None:
        """Append a state to the photo's undo stack (deduped; truncates the
        redo tail; capped at _DEV_HISTORY_MAX)."""
        hist = self._dev_history.setdefault(path, [])
        pos = self._dev_history_pos.get(path, len(hist) - 1)
        if hist and 0 <= pos < len(hist) and hist[pos] == d:
            return
        del hist[pos + 1:]
        hist.append(dict(d))
        if len(hist) > self._DEV_HISTORY_MAX:
            del hist[:len(hist) - self._DEV_HISTORY_MAX]
        self._dev_history_pos[path] = len(hist) - 1
        self._dev_update_undo_buttons()

    def _dev_undo(self):
        hist = self._dev_history.get(self._dev_selected) or []
        pos = self._dev_history_pos.get(self._dev_selected,
                                        len(hist) - 1)
        if pos <= 0:
            return
        pos -= 1
        path = self._dev_selected
        self._dev_history_pos[path] = pos
        d = dict(hist[pos])
        self._photo_adjust[path] = d
        self._apply_dev_fields(d)
        self._dev_update_undo_buttons()

    def _dev_redo(self):
        path = self._dev_selected
        hist = self._dev_history.get(path) or []
        pos = self._dev_history_pos.get(path, len(hist) - 1)
        if pos >= len(hist) - 1:
            return
        pos += 1
        self._dev_history_pos[path] = pos
        d = dict(hist[pos])
        self._photo_adjust[path] = d
        self._apply_dev_fields(d)
        self._dev_update_undo_buttons()

    def _dev_update_undo_buttons(self):
        """Reflect the selected photo's history position in the buttons."""
        path = self._dev_selected
        hist = self._dev_history.get(path) or []
        pos = self._dev_history_pos.get(path, len(hist) - 1)
        try:
            self._dev_undo_btn.configure(
                state=tk.NORMAL if pos > 0 else tk.DISABLED)
            self._dev_redo_btn.configure(
                state=tk.NORMAL if pos < len(hist) - 1 else tk.DISABLED)
        except (AttributeError, tk.TclError):
            pass  # buttons not built yet (early startup)

    def _dev_undo_available(self) -> bool:
        """Cmd+Z routing: per-photo history wins while Develop is active."""
        if self._active_module != "develop" or not self._dev_selected:
            return False
        hist = self._dev_history.get(self._dev_selected) or []
        pos = self._dev_history_pos.get(self._dev_selected,
                                        len(hist) - 1)
        return pos > 0

    def _dev_redo_available(self) -> bool:
        if self._active_module != "develop" or not self._dev_selected:
            return False
        hist = self._dev_history.get(self._dev_selected) or []
        pos = self._dev_history_pos.get(self._dev_selected,
                                        len(hist) - 1)
        return 0 <= pos < len(hist) - 1

    def _dev_copy_settings(self):
        """Copy the selected photo's effective adjustments (its overlay if
        any, else the current global sliders it is shown with)."""
        if not self._dev_selected:
            return
        self._settings_clipboard = dict(
            self._photo_adjust.get(self._dev_selected)
            or self._dev_fields_of(self._build_options()))
        self._dev_status_lbl.configure(
            text=self._t("settings_copied"),
            fg=COLORS["text_secondary"])

    def _dev_paste_settings(self):
        """Paste the clipboard onto the photo in the viewer."""
        if not self._settings_clipboard:
            self._dev_status_lbl.configure(
                text=self._t("paste_need_copy"), fg=COLORS["warning"])
            return
        if not self._dev_selected:
            return
        self._paste_settings_to([self._dev_selected])
        self._dev_status_lbl.configure(
            text=self._t("paste_done", n=1), fg=COLORS["text_secondary"])

    # ── v2.4: AI auto-tone (official plugin) ─────────────────────────────

    # plugin target name → ProcessOptions field. The plugin predicts LR-style
    # values; every target except exposure maps 1:1 onto a develop field
    # (exposure is linear EV stops = ProcessOptions.ev, contrast/saturation
    # are factors with 1.0 neutral, wb_temp is the light-source kelvin).
    _AI_TONE_FIELDS = (
        ("exposure", "ev"), ("contrast", "contrast"),
        ("saturation", "saturation"), ("vibrance", "vibrance"),
        ("wb_temp", "wb_temp"), ("wb_tint", "wb_tint"),
        ("clarity", "clarity"), ("texture", "texture"),
        ("dehaze", "dehaze"),
    )

    def _dev_ai_tone(self):
        """Predict the 9 global params for the selected photo with the
        official auto-tone plugin and write them into its develop overlay.
        Runs in a worker thread (first use downloads weights + towers);
        the result is marshalled back through the dev bus."""
        if self._dev_ai_busy:
            return
        path = self._dev_selected
        if not path:
            self._dev_status_lbl.configure(
                text=self._t("dev_ai_need_photo"), fg=COLORS["warning"])
            return
        try:
            strength = float(self._dev_ai_strength.get())
        except (ValueError, tk.TclError):
            strength = 0.8
        self._dev_ai_busy = True
        self._dev_ai_btn.configure(state=tk.DISABLED)
        self._dev_status_lbl.configure(
            text=self._t("dev_ai_running"), fg=COLORS["text_secondary"])

        def work():
            res, err = None, None
            try:
                from photo_s_plugin_auto_tone import auto_tone
                res = auto_tone(path, strength=strength, render=False)
            except ImportError as e:
                err = ("plugin", str(e))
            except Exception as e:  # model load / inference errors carry
                err = ("failed", str(e) or e.__class__.__name__)  # their own hint
            self._dev_bus.schedule(
                lambda: self._dev_ai_apply(path, res, err))

        threading.Thread(target=work, daemon=True,
                         name="photos-ai-tone").start()

    def _dev_ai_apply(self, path, res, err):
        """Bus-delivered completion: merge the predicted params into the
        photo's overlay + undo history, reflect them in the sliders."""
        self._dev_ai_busy = False
        try:
            self._dev_ai_btn.configure(state=tk.NORMAL)
        except tk.TclError:
            pass  # panel torn down mid-flight
        if err is not None:
            kind, msg = err
            if kind == "plugin":
                self._dev_status_lbl.configure(
                    text=self._t("dev_ai_need_plugin"),
                    fg=COLORS["warning"])
                messagebox.showwarning(
                    self._t("dev_ai_tone"),
                    self._t("dev_ai_install_hint"))
            else:
                self._dev_status_lbl.configure(
                    text=self._t("dev_ai_failed", err=msg[:140]),
                    fg=COLORS["warning"])
            return
        predicted = (res or {}).get("options") or {}
        base = dict(self._photo_adjust.get(path)
                   or self._dev_fields_of(self._build_options()))
        applied = 0
        for src, dst in self._AI_TONE_FIELDS:
            v = predicted.get(src)
            if v is not None:
                base[dst] = v
                applied += 1
        if not applied:
            self._dev_status_lbl.configure(
                text=self._t("dev_ai_no_params"), fg=COLORS["warning"])
            return
        if path not in self._photo_adjust:
            self._dev_history_push(
                path, self._dev_fields_of(self._build_options()))
        self._photo_adjust[path] = base
        self._dev_history_push(path, base)
        if self._dev_selected == path:
            self._apply_dev_fields(base)
        # 词汇表扩展（v2.4）：模型带局部头时把 [{region, params}] 转成
        # 紧凑蒙版字符串并入 per-photo 蒙版（与手动蒙版同一通道，预览/
        # 导出/撤销走既有机制；蒙版可在蒙版编辑器里删除）
        n_masks = self._dev_ai_apply_local(path, (res or {}).get("local"))
        conf = (res or {}).get("confidence")
        text = (self._t("dev_ai_done", conf=conf)
                if isinstance(conf, (int, float))
                else self._t("dev_ai_done", conf=0.0).split(" · ")[0])
        if n_masks:
            text += " · " + self._t("dev_ai_masks_added", n=n_masks)
        self._dev_status_lbl.configure(text=text,
                                       fg=COLORS["text_secondary"])
        self._dev_update_undo_buttons()
        self._refresh_export_queue()

    def _dev_ai_apply_local(self, path, local) -> int:
        """Merge the predicted local adjustments into the photo's per-photo
        masks; returns how many AI masks were added (0 = none predicted)."""
        if not local:
            return 0
        from photo_s.autotone import local_to_specs
        try:
            masks_s, adjust_s = local_to_specs(local)
        except ValueError as e:
            self._dev_status_lbl.configure(
                text=self._t("dev_ai_failed", err=str(e)[:140]),
                fg=COLORS["warning"])
            return 0
        if not masks_s:
            return 0
        existing = ((self._photo_masks or {}).get(path)
                    or {"masks": "", "mask_adjust": ""})
        masks = [m for m in (existing.get("masks") or "").split(";") if m]
        adjusts = [a for a in (existing.get("mask_adjust") or "").split(";")
                   if a]
        # 防 AI 蒙版名与已有蒙版撞名：换前缀重编号
        used = {m.split(":", 1)[0] for m in masks}
        new_masks = masks_s.split(";")
        new_adjusts = adjust_s.split(";")
        renamed_m, renamed_a = [], []
        n = 0
        for m, a in zip(new_masks, new_adjusts):
            prefix = f"ai{n}"
            while prefix in used:
                n += 1
                prefix = f"ai{n}"
            old = m.split(":", 1)[0]
            renamed_m.append(f"{prefix}:{m.split(':', 1)[1]}")
            renamed_a.append(a.replace(f"{old}:", f"{prefix}:", 1))
            used.add(prefix)
            n += 1
        if self._photo_masks is None:
            self._photo_masks = {}
        self._photo_masks[path] = {
            "masks": ";".join(masks + renamed_m),
            "mask_adjust": ";".join(adjusts + renamed_a),
        }
        return len(renamed_m)


    def _paste_settings_to(self, paths) -> None:
        """Write the clipboard onto photos: per-photo overlay + history
        (baseline first for photos never edited this session). If the
        Develop viewer currently shows one of them, refresh its sliders."""
        clip = self._settings_clipboard
        if not clip or not paths:
            return
        for p in paths:
            if p not in self._photo_adjust:
                self._dev_history_push(
                    p, self._dev_fields_of(self._build_options()))
            d = dict(clip)
            self._photo_adjust[p] = d
            self._dev_history_push(p, d)
        if self._dev_selected in paths:
            self._apply_dev_fields(clip)
        self._dev_update_undo_buttons()
        self._refresh_export_queue()

    def _paste_settings_to_checked(self):
        """Export queue action: paste the clipboard onto every checked
        photo."""
        if not self._settings_clipboard:
            self._recipe_status(self._t("paste_need_copy"))
            return
        files = self._checked_files()
        if not files:
            self._recipe_status(self._t("paste_need_checked"))
            return
        self._paste_settings_to(files)
        self._recipe_status(self._t("paste_done", n=len(files)))

    # ── v2.2 export recipes ─────────────────────────────────────────────────

    def _recipe_status(self, text: str) -> None:
        try:
            self._recipe_status_lbl.configure(text=text)
        except (AttributeError, tk.TclError):
            pass

    def _export_recipe_capture(self) -> dict:
        return self._capture_recipe_fields(self._build_options())

    def _capture_recipe_fields(self, opts) -> dict:
        return {f: getattr(opts, f, None)
                for f in self._EXPORT_RECIPE_FIELDS}

    def _export_recipe_save(self):
        name = self._recipe_var.get().strip()
        if not name:
            self._recipe_status(self._t("recipe_need_name"))
            return
        self._export_recipes[name] = self._export_recipe_capture()
        self._save_gui_state()
        self._recipe_combo.configure(values=sorted(self._export_recipes))
        self._recipe_var.set(name)
        self._recipe_status(self._t("recipe_saved", name=name))

    def _export_recipe_apply(self):
        name = self._recipe_var.get().strip()
        recipe = self._export_recipes.get(name)
        if not recipe:
            self._recipe_status(self._t("recipe_need_pick"))
            return
        self._apply_recipe_fields(recipe)
        self._recipe_status(self._t("recipe_applied", name=name))

    def _export_recipe_delete(self):
        name = self._recipe_var.get().strip()
        if name not in self._export_recipes:
            self._recipe_status(self._t("recipe_need_pick"))
            return
        del self._export_recipes[name]
        self._save_gui_state()
        self._recipe_combo.configure(values=sorted(self._export_recipes))
        self._recipe_var.set("")
        self._recipe_status(self._t("recipe_deleted", name=name))

    def _apply_recipe_fields(self, d: dict) -> None:
        """Map an export recipe onto the GUI vars (canonical values back to
        vars — the inverse of _build_options for output-side fields)."""
        def _set(name, value):
            try:
                getattr(self, name).set(value)
            except (AttributeError, tk.TclError):
                pass

        def _num_or_empty(name, v):
            _set(name, "" if v is None else str(int(v)))

        _set("quality", int(d.get("quality") or 85))
        _set("output_format", d.get("output_format") or "JPEG")
        _set("output_dir", d.get("output_dir") or "")
        _set("prefix", d.get("prefix") or "")
        _set("suffix", d.get("suffix") or "")
        _num_or_empty("max_width", d.get("max_width"))
        _num_or_empty("max_height", d.get("max_height"))
        _num_or_empty("max_pixels", d.get("max_pixels"))
        _num_or_empty("scale_percent", d.get("scale_percent"))
        tb = d.get("target_size_bytes")
        _set("target_size_mode", bool(tb))
        if tb:
            if tb >= 1024 ** 2 and tb % 1024 ** 2 == 0:
                _set("target_size_unit", "MB")
                _set("target_size_value", str(tb // 1024 ** 2))
            else:
                _set("target_size_unit", "KB")
                _set("target_size_value", str(max(1, round(tb / 1024))))
        _set("optimize", bool(d.get("optimize")))
        _set("progressive", bool(d.get("progressive")))
        _set("jpeg_subsampling", d.get("jpeg_subsampling") or "420")
        _set("srgb", bool(d.get("srgb")))
        _set("flatten_cmyk", bool(d.get("flatten_cmyk")))
        _set("preserve_exif", bool(d.get("preserve_exif")))
        _set("strip_gps", bool(d.get("strip_gps")))
        _set("watermark_text", d.get("watermark_text") or "")
        _set("watermark_image", d.get("watermark_image") or "")
        _set("watermark_position", d.get("watermark_position")
             or "BOTTOM_RIGHT")
        _set("watermark_opacity", int(d.get("watermark_opacity") or 50))
        sizes = d.get("output_sizes")
        _set("output_sizes", ",".join(str(s) for s in sizes) if sizes else "")


    def _dev_display_current(self):
        # compare modes own the viewer: the overlay canvas composes from
        # the PIL pair; the plain label path stays for off/no-pair cases
        if self._dev_compare_mode != "off":
            self._dev_compare_render()
            return
        try:
            self._dev_compare_canvas.place_forget()
        except (AttributeError, tk.TclError):
            pass  # viewer not built yet (early startup)
        st = self._dev_render_state
        photo = st["before_photo"] if self._dev_show_before.get() \
            else (st["after_photo"] or st["before_photo"])
        if photo is not None:
            self._dev_image_lbl.configure(image=photo, text="")
            self._dev_image_lbl.image = photo
        else:
            self._dev_image_lbl.configure(image="")
            self._dev_image_lbl.configure(
                text=self._t("dev_no_selection"))

    # ── v2.4: before/after compare (split divider + side-by-side) ─────────

    def _dev_compare_toggle(self):
        """Cycle off → split (draggable divider) → side-by-side → off."""
        mode = {"off": "split", "split": "side", "side": "off"}[
            self._dev_compare_mode]
        self._dev_compare_mode = mode
        self._dev_compare_btn.configure(text=self._t(
            {"off": "dev_compare_split", "split": "dev_compare_side",
             "side": "dev_compare_exit"}[mode]))
        self._dev_display_current()

    def _dev_compare_drag(self, event):
        """Move the split divider (click + drag on the compare canvas)."""
        if self._dev_compare_mode != "split":
            return
        w = self._dev_compare_canvas.winfo_width()
        if w > 4:
            frac = min(0.98, max(0.02, event.x / w))
            if abs(frac - self._dev_split_frac) > 0.005:
                self._dev_split_frac = frac
                self._dev_compare_render()

    def _dev_compare_render(self):
        """Compose the before/after pair onto the compare canvas. Split
        mode crops the pair at the divider; side mode scales both to a
        common height and lays them out with a gap. Runs on the UI thread
        (PIL crops at preview size are ~ms)."""
        from PIL import Image, ImageTk
        st = self._dev_render_state
        canvas = self._dev_compare_canvas
        before, after = st.get("before_pil"), st.get("after_pil")
        if (self._dev_compare_mode == "off" or before is None
                or after is None):
            canvas.place_forget()
            return
        try:
            canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
            canvas.delete("all")
            w = max(200, canvas.winfo_width() - 16)
            h = max(150, canvas.winfo_height() - 16)
            b = before.convert("RGB")
            a = after.convert("RGB")
            if self._dev_compare_mode == "side":
                # common height, both halves share the width budget
                half = max(60, (w - 12) // 2)
                bs = min(half / b.width, h / b.height)
                as_ = min(half / a.width, h / a.height)
                s = min(bs, as_)
                bw, aw = max(1, int(b.width * s)), max(1, int(a.width * s))
                bh = ah = max(1, int(b.height * s))
                b2 = b.resize((bw, bh), Image.LANCZOS)
                a2 = a.resize((aw, ah), Image.LANCZOS)
                comp = Image.new("RGB", (bw + 12 + aw, bh), (0, 0, 0))
                comp.paste(b2, (0, 0))
                comp.paste(a2, (bw + 12, 0))
            else:  # split — crop the pair at the divider fraction
                s = min(w / b.width, h / b.height, w / a.width,
                        h / a.height)
                bw = aw = max(1, int(min(b.width, a.width) * s))
                bh = ah = max(1, int(min(b.height, a.height) * s))
                b2 = b.resize((bw, bh), Image.LANCZOS)
                a2 = a.resize((aw, ah), Image.LANCZOS)
                split_x = max(1, min(bw - 1,
                                     int(self._dev_split_frac * bw)))
                comp = a2.copy()
                comp.paste(b2.crop((0, 0, split_x, bh)), (0, 0))
            photo = ImageTk.PhotoImage(comp, master=canvas)
            self._dev_compare_photo = photo
            ox = (max(200, canvas.winfo_width()) - comp.width) // 2
            oy = (max(150, canvas.winfo_height()) - comp.height) // 2
            canvas.create_image(ox, oy, image=photo, anchor="nw")
            if self._dev_compare_mode == "split":
                canvas.create_line(
                    ox + split_x, oy, ox + split_x, oy + bh,
                    fill=COLORS["accent"], width=2)
                canvas.create_oval(
                    ox + split_x - 5, oy + bh // 2 - 10,
                    ox + split_x + 5, oy + bh // 2 + 10,
                    fill=COLORS["accent"], outline="")
            else:
                canvas.create_line(
                    ox + bw + 6, oy, ox + bw + 6, oy + bh,
                    fill=COLORS["divider"], width=1)
        except tk.TclError:
            pass  # viewer torn down mid-compose


    def _dev_select(self, path):
        """Pick a photo for the viewer: reset the debounce, decode the
        original off-thread (RAW-safe), keep any rendered 'after' until a
        new one lands."""
        # v2.2: flush mid-drag edits of the photo we are leaving (the tick
        # may never have seen the final value), then load the new photo's
        # own adjustments so the sliders beside the preview show them.
        old = self._dev_selected
        if old and old != path and old in self._photo_adjust:
            self._photo_adjust[old] = self._dev_fields_of(
                self._build_options())
        self._dev_selected = path
        overlay = self._photo_adjust.get(path)
        if overlay is not None:
            self._apply_dev_fields(overlay)
        st = self._dev_render_state
        st.update(sig=None, stable=0, rendered=None, after_photo=None,
                  before_photo=None, before_pil=None, after_pil=None)
        self._dev_update_undo_buttons()
        try:
            idx = self.files.index(path) + 1
        except ValueError:
            idx = "?"
        self._dev_nav_lbl.configure(
            text="{}/{} · {}".format(idx, len(self.files),
                                     os.path.basename(path)))
        self._dev_highlight_strip()
        self._dev_display_current()

        def work():
            try:
                img = _open_image_safe(path).convert("RGB")
            except Exception:
                img = None
            self._dev_bus.schedule(
                lambda: self._dev_before_ready(path, img))
        threading.Thread(target=work, daemon=True).start()

    def _dev_before_ready(self, path, img):
        if path != self._dev_selected or img is None:
            return
        st = self._dev_render_state
        st["before_pil"] = img
        st["before_photo"] = self._dev_fit_photo(img)
        if st["after_photo"] is None or self._dev_show_before.get():
            self._dev_display_current()

    def _dev_refresh_filmstrip(self):
        """Rebuild the filmstrip when self.files changed (cheap no-op via
        signature compare — called from _refresh_file_list)."""
        if not hasattr(self, "_dev_strip"):
            return
        sig = tuple(self.files)
        if sig == self._dev_strip_sig:
            return
        self._dev_strip_sig = sig
        self._dev_strip_cells = {}
        for w in self._dev_strip.winfo_children():
            w.destroy()
        for path in self.files:
            cell = tk.Frame(self._dev_strip, bg=COLORS["card"],
                            highlightthickness=2,
                            highlightbackground=COLORS["card"])
            cell.pack(side="left", padx=(6, 0), pady=6)
            lbl = tk.Label(cell, text="…", width=9, height=4,
                           bg=COLORS["card"],
                           fg=COLORS["text_secondary"], font=FONT_TINY)
            lbl.pack(padx=1, pady=1)
            lbl.bind("<Button-1>", lambda e, p=path: self._dev_select(p))
            self._dev_strip_cells[path] = (cell, lbl)
            self._dev_load_thumb(lbl, path)
        if self.files:
            if not self._dev_selected or self._dev_selected not in self.files:
                checked = self._checked_files()
                self._dev_select(checked[0] if checked else self.files[0])
            else:
                self._dev_highlight_strip()
        else:
            self._dev_selected = ""
            self._dev_render_state.update(
                sig=None, rendered=None, before_photo=None,
                after_photo=None, before_pil=None, after_pil=None)
            self._dev_nav_lbl.configure(text="")
            self._dev_display_current()

    def _dev_load_thumb(self, lbl, path):
        """Filmstrip thumbnail through the shared decode helper and a
        small dedicated LRU (RAW decodes off the UI thread)."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        key = (path, 64, mtime)
        cached = self._dev_img_cache.get(key)
        if cached:
            self._dev_show_thumb(lbl, cached)
            return

        def work():
            try:
                img = self._decode_thumb_source(path, 64)
            except Exception:
                img = False
            self._dev_img_cache[key] = img
            if img:
                self._dev_bus.schedule(
                    lambda: self._dev_show_thumb(lbl, img))
        threading.Thread(target=work, daemon=True).start()

    def _dev_show_thumb(self, lbl, img):
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(img, master=lbl)
            lbl.configure(image=photo, text="", width=photo.width(),
                          height=photo.height())
            lbl.image = photo
        except tk.TclError:
            pass  # label destroyed with the strip rebuild

    def _dev_highlight_strip(self):
        """Accent-outline the selected filmstrip cell."""
        for path, (cell, _lbl) in getattr(self, "_dev_strip_cells",
                                          {}).items():
            try:
                cell.configure(
                    highlightbackground=COLORS["accent"]
                    if path == self._dev_selected else COLORS["card"])
            except tk.TclError:
                pass

    def _dev_draw_analysis(self, analysis):
        """Render the analysis sidebar: luma bars + R/G/B polylines on a
        Canvas, exposure/Kelvin text below. Data-viz colors are literal
        (not palette tokens) — they represent channel data, like the HSL
        chips."""
        cv = self._dev_hist
        cv.delete("all")
        if not analysis or not analysis.get("ok", True):
            self._dev_expo_lbl.configure(text="")
            self._dev_kelvin_lbl.configure(text="")
            return
        hist = analysis.get("histogram") or {}
        w = max(120, cv.winfo_width())
        h = max(60, cv.winfo_height())
        luma = hist.get("luma") or [0] * 32
        peak = max(max(luma), 1)
        bw = w / 32.0
        for i, count in enumerate(luma):
            bh = count / peak * (h - 6)
            cv.create_rectangle(i * bw, h - bh - 2, (i + 1) * bw - 1, h - 2,
                                fill=COLORS["divider"], width=0)
        for key, color in (("r", "#e0453a"), ("g", "#30a14e"),
                           ("b", "#3478f6")):
            data = hist.get(key)
            if not data:
                continue
            peakc = max(max(data), 1)
            pts = []
            for i, count in enumerate(data):
                pts += [i * bw + bw / 2,
                        h - 3 - count / peakc * (h - 8)]
            cv.create_line(*pts, fill=color, width=1)
        expo = analysis.get("exposure") or {}
        self._dev_expo_lbl.configure(text=self._t(
            "dev_exposure_line", lum=expo.get("luminance", "—"),
            over=expo.get("overexposed_pct", 0),
            under=expo.get("underexposed_pct", 0),
            blur=analysis.get("blur_score", "—")))
        wb = analysis.get("white_balance") or {}
        self._dev_kelvin_lbl.configure(text=self._t(
            "dev_kelvin_line", k=wb.get("kelvin_estimate", "—"),
            tint=wb.get("tint_gm", "—")))

    def _build_file_panel(self, parent):
        """Build the left-side file list panel."""
        # Card container
        card = tk.Frame(parent, bg=COLORS["card"], bd=0, highlightthickness=0)
        card.pack(fill="both", expand=True)

        # Toolbar
        toolbar = tk.Frame(card, bg=COLORS["card"])
        toolbar.pack(fill="x", padx=14, pady=(14, 0))

        self.add_files_btn = FlatButton(
            toolbar, text=self._t("add_images"), command=self._add_files,
            bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
        )
        self.add_files_btn.pack(side="left")

        self.add_folder_btn = FlatButton(
            toolbar, text=self._t("add_folder"), command=self._add_folder,
            bg=COLORS["card"], fg=COLORS["accent"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"],
        )
        self.add_folder_btn.pack(side="left", padx=(8, 0))

        remove_btn = FlatButton(
            toolbar, text=self._t("remove"), command=self._remove_selected,
            bg=COLORS["card"], fg=COLORS["danger"], hover_bg=COLORS["bg"],
            border_color=COLORS["danger"],
        )
        remove_btn.pack(side="left", padx=(8, 0))

        FlatButton(
            toolbar, text=self._t("check_toggle_all"),
            command=self._toggle_all_checks,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        ).pack(side="left", padx=(8, 0))

        # Group separator: file-management (left) vs the rest.
        # 分析 / 清空 live in the "更多工具" menu (decluttered toolbar).
        sep = tk.Frame(toolbar, bg=COLORS["divider"], width=1)
        sep.pack(side="left", fill="y", padx=10)

        # Workflow toolbar (review / dedup / gallery). Gallery is exempted
        # from the processing lockout (read-only export); review/dedup are
        # auto-disabled during a batch (in-place EXIF writes / file moves
        # would race the pipeline).
        wf = tk.Frame(card, bg=COLORS["card"])
        wf.pack(fill="x", padx=14, pady=(8, 0))

        self.review_btn = FlatButton(
            wf, text=self._t("review_btn"), command=self._show_review,
            bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
            font=FONT_SMALL,
        )
        self.review_btn.pack(side="left")

        self.dedup_btn = FlatButton(
            wf, text=self._t("dedup_btn"), command=self._show_dedup,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.dedup_btn.pack(side="left", padx=(8, 0))

        self.rename_btn = FlatButton(
            wf, text=self._t("rename_btn"), command=self._show_rename,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.rename_btn.pack(side="left", padx=(8, 0))

        self.gallery_btn = FlatButton(
            wf, text=self._t("gallery_btn"), command=self._show_gallery_export,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.gallery_btn.pack(side="left", padx=(8, 0))

        self.compare_btn = FlatButton(
            wf, text=self._t("compare_btn"), command=self._show_compare,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.compare_btn.pack(side="left", padx=(8, 0))

        # Group separator: workflow actions (left) vs undo / more (right)
        sep2 = tk.Frame(wf, bg=COLORS["divider"], width=1)
        sep2.pack(side="left", fill="y", padx=10)

        self.undo_btn = FlatButton(
            wf, text=self._t("undo"), command=self._undo,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.undo_btn.pack(side="left", padx=(8, 0))
        self._sync_undo_btn()

        self.redo_btn = FlatButton(
            wf, text=self._t("redo"), command=self._redo,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.redo_btn.pack(side="left", padx=(8, 0))
        self._sync_redo_btn()

        self.more_btn = FlatButton(
            wf, text=self._t("more_btn"), command=self._post_more_menu,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
        )
        self.more_btn.pack(side="left", padx=(8, 0))

        # File-list thumbnail size selector (small/medium/large)
        tk.Label(toolbar, text=self._t("thumb_size_lbl"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="right")
        self.thumb_size_combo = ttk.Combobox(
            toolbar, state="readonly", width=5, font=FONT_SMALL,
            values=[self._t("thumb_small"), self._t("thumb_medium"),
                    self._t("thumb_large")],
        )
        _thumb_idx = {48: 0, 96: 1, 144: 2}.get(self._thumb_size, 1)
        self.thumb_size_combo.current(_thumb_idx)
        self.thumb_size_combo.bind("<<ComboboxSelected>>", self._on_thumb_size)
        self.thumb_size_combo.pack(side="right", padx=(4, 8))

        self.file_count_label = tk.Label(
            toolbar, text=self._t("files_count", n=0), font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["card"],
        )
        self.file_count_label.pack(side="right", padx=(0, 12))

        # File list: scrollable rows with real ttk.Checkbuttons — the
        # same widget as the settings panel. (Treeview cells cannot host
        # widgets, and its per-item image column proved unreliable, so
        # the list is a canvas of row frames instead.)
        # Filter box above the list (display-only view over self.files)
        filter_row = tk.Frame(card, bg=COLORS["card"])
        filter_row.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(filter_row, text=self._t("filter_lbl"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="left")
        self.filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var,
                                      font=FONT_SMALL)
        self.filter_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.filter_entry.bind("<KeyRelease>",
                               lambda e: self._apply_filter())
        FlatButton(filter_row, text="×", command=self._clear_filter,
                   bg=COLORS["card"], fg=COLORS["text_secondary"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=6, pady=1, border_color=COLORS["border"]).pack(
            side="right")

        list_frame = tk.Frame(card, bg=COLORS["card"])
        list_frame.pack(fill="both", expand=True, padx=14, pady=12)
        self._lib_list_frame = list_frame
        # v2.5: 审查灯箱宿主（pack 切换见 _lib_lightbox_apply_layout）
        self._lib_lightbox_host = tk.Frame(card, bg=COLORS["bg"])

        # v2.4 VirtualGrid: rows are drawn directly on the canvas at a
        # fixed row height — only the visible window is materialized, so
        # huge libraries scroll smoothly (the old all-widget rows froze
        # the UI past a few thousand photos).
        self.file_list_canvas = tk.Canvas(
            list_frame, bg=COLORS["card"], highlightthickness=0,
            borderwidth=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=self.file_list_canvas.yview)
        self.file_list_canvas.configure(
            yscrollcommand=self._lib_yscroll)
        scrollbar.pack(side="right", fill="y")
        self._lib_scrollbar = scrollbar
        self.file_list_canvas.pack(side="left", fill="both", expand=True)
        self.file_list_canvas.bind("<Configure>",
                                   lambda e: self._lib_draw())

        self.file_list_canvas.bind("<Button-1>", self._lib_click)
        self.file_list_canvas.bind("<Double-1>", self._lib_double_click)

        # Mousewheel: same pattern as the settings panel (card-scoped
        # bind_all + boundary snapping + momentum debounce)
        _last_boundary = [0.0]

        def _on_mousewheel(event):
            delta = event.delta or (120 if event.num == 4 else
                                    -120 if event.num == 5 else 0)
            amount = -delta if abs(delta) < 10 else -delta / 120
            if time.monotonic() - _last_boundary[0] < 0.15:
                return  # momentum tail right after a boundary hit
            top, bottom = self.file_list_canvas.yview()
            if amount > 0:
                if bottom >= 1.0 - 1e-9:
                    _last_boundary[0] = time.monotonic()
                    self.file_list_canvas.yview_moveto(1.0)
                    return
            elif amount < 0:
                if top <= 1e-9:
                    _last_boundary[0] = time.monotonic()
                    self.file_list_canvas.yview_moveto(0.0)
                    return
            self.file_list_canvas.yview_scroll(amount, "units")

        def _bind_scroll(event):
            self.file_list_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            # X11 wheels send Button-4/5 with num=4/5 instead of delta
            self.file_list_canvas.bind_all("<Button-4>", _on_mousewheel)
            self.file_list_canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_scroll(event):
            self.file_list_canvas.unbind_all("<MouseWheel>")
            self.file_list_canvas.unbind_all("<Button-4>")
            self.file_list_canvas.unbind_all("<Button-5>")

        for w in (list_frame, self.file_list_canvas):
            w.bind("<Enter>", _bind_scroll)
            w.bind("<Leave>", _unbind_scroll)

        # Register drag-and-drop targets (requires tkinterdnd2 AND a TkinterDnD
        # root; with a plain tk.Tk() root — e.g. headless smoke tests — the
        # tkdnd Tcl commands aren't loaded, so degrade gracefully)
        if DND_AVAILABLE:
            try:
                for widget in (card, self.file_list_canvas):
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # File list hint
        hint_text = self._t("hint_dnd" if DND_AVAILABLE else "hint_no_dnd")
        drop_hint = tk.Label(
            card, text=hint_text,
            font=FONT_TINY, fg=COLORS["text_secondary"], bg=COLORS["card"],
        )
        drop_hint.pack(fill="x", padx=14, pady=(0, 10))

    def _build_settings_panel(self, parent, develop_parent):
        """Build the settings panels (v2.0 workspace split).

        ``parent`` hosts the export notebook (output / fx / metadata /
        options). ``develop_parent`` — the Develop module's right column —
        hosts the ADJUST tab contents as a standalone scroll area (it is
        the only develop tab, so no notebook). The same tk.Variables back
        both: a slider moved in Develop drives the preview there and the
        export pipeline here."""
        # Defensive: a rebuild (theme/language) destroys the old card while
        # its Enter/Leave handlers are gone, but any bind_all left behind by
        # the old panel would target a destroyed canvas. Clear it first.
        canvas_unbind_safe(parent)

        # Card container
        card = tk.Frame(parent, bg=COLORS["card"], bd=0, highlightthickness=0)
        card.pack(fill="both", expand=True)

        # v2.4: settings search — find a control by its label across tabs,
        # switch to the owning tab and highlight every hit
        search_row = tk.Frame(card, bg=COLORS["card"])
        search_row.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(search_row, text=self._t("settings_search_lbl"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="left")
        self._settings_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row,
                                textvariable=self._settings_search_var,
                                font=FONT_SMALL)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        search_entry.bind("<KeyRelease>",
                          lambda e: self._settings_search())

        # Category tabs (Lightroom-style): each tab is its own scroll area.
        nb = ttk.Notebook(card)
        nb.pack(fill="both", expand=True)
        self._settings_nb = nb
        self._settings_tabs = []   # (tab_widget_or_None, inner, module)

        def _make_tab_scroll(tab):
            """Scrollable canvas+inner-frame for one category tab."""
            canvas = tk.Canvas(tab, bg=COLORS["card"], highlightthickness=0, bd=0)
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            inner = tk.Frame(canvas, bg=COLORS["card"])
            inner.columnconfigure(0, weight=1)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_frame_configure(_event):
                canvas.configure(scrollregion=canvas.bbox("all"))

            def _on_canvas_configure(event):
                # Keep the inner frame as wide as the visible canvas so
                # settings are never clipped on the right edge
                canvas.itemconfigure(canvas_window, width=event.width)

            inner.bind("<Configure>", _on_frame_configure)
            canvas.bind("<Configure>", _on_canvas_configure)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            # Mousewheel scrolling (bind on the tab so the handler stays alive
            # over child widgets; clamp + re-snap so trackpad momentum can't
            # wobble the view off the edge).
            _last_boundary = [0.0]

            def _on_mousewheel(event):
                delta = event.delta
                amount = -delta if abs(delta) < 10 else -delta / 120
                if time.monotonic() - _last_boundary[0] < 0.15:
                    return
                top, bottom = canvas.yview()
                if amount > 0:
                    if bottom >= 1.0 - 1e-9:
                        _last_boundary[0] = time.monotonic()
                        canvas.yview_moveto(1.0)
                        return
                elif amount < 0:
                    if top <= 1e-9:
                        _last_boundary[0] = time.monotonic()
                        canvas.yview_moveto(0.0)
                        return
                canvas.yview_scroll(amount, "units")

            def _bind_scroll(event):
                canvas.bind_all("<MouseWheel>", _on_mousewheel)

            def _unbind_scroll(event):
                canvas.unbind_all("<MouseWheel>")

            tab.bind("<Enter>", _bind_scroll)
            tab.bind("<Leave>", _unbind_scroll)
            return inner

        def _add_tab(key):
            tab = ttk.Frame(nb)
            nb.add(tab, text=self._t(key))
            self._settings_tabs.append((tab, _make_tab_scroll(tab),
                                        "export"))
            return self._settings_tabs[-1][1]

        OUT = _add_tab("tab_output")      # format/mode/resize/output/sizes/naming/subfolder
        FX = _add_tab("tab_fx")           # watermark
        META = _add_tab("tab_metadata")   # EXIF date / GPX / privacy / face blur
        OPT = _add_tab("tab_options")     # preserve/overwrite/jobs/…
        # The adjust tab lives in the Develop module (edit tools beside
        # the preview); same scroll mechanics, no notebook needed.
        adj_host = tk.Frame(develop_parent, bg=COLORS["card"], bd=0,
                            highlightthickness=0)
        adj_host.pack(fill="both", expand=True)
        ADJ = _make_tab_scroll(adj_host)
        self._settings_tabs.append((None, ADJ, "develop"))

        pad = {"padx": 18, "pady": 4}

        # ── Output Format ────────────────────────────────────────────────────
        fmt_frame = self._add_collapsible_section(OUT, "sec_format")

        self.format_combo = ttk.Combobox(
            fmt_frame, textvariable=self.output_format,
            values=list(SUPPORTED_FORMATS.keys()), state="readonly",
            font=FONT_BODY,
        )
        self.format_combo.pack(fill="x")

        # ── Quality / Target Size ────────────────────────────────────────────
        # Mode toggle: radio buttons
        mode_frame = self._add_collapsible_section(OUT, "sec_mode")

        self.manual_radio = ttk.Radiobutton(
            mode_frame, text=self._t("manual_quality"), variable=self.target_size_mode,
            value=False, command=self._on_mode_change,
        )
        self.manual_radio.pack(anchor="w")

        self.target_radio = ttk.Radiobutton(
            mode_frame, text=self._t("target_size_mode"), variable=self.target_size_mode,
            value=True, command=self._on_mode_change,
        )
        self.target_radio.pack(anchor="w", pady=(4, 0))

        # ── Quality slider (shown in manual mode; ceiling in target mode) ────
        self.quality_section_frame = tk.Frame(mode_frame, bg=COLORS["card"])
        self.quality_section_frame.pack(fill="x", pady=(6, 0))

        self.quality_section_label = tk.Label(
            self.quality_section_frame, text=self._t("quality"),
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["card"],
        )
        self.quality_section_label.pack(anchor="w")

        quality_row = tk.Frame(self.quality_section_frame, bg=COLORS["card"])
        quality_row.pack(fill="x", pady=(4, 0))

        self.quality_label = tk.Label(
            quality_row, text=str(self.quality.get()),
            font=(PLATFORM_FONTS["body"], 14, "bold"),
            fg=COLORS["accent"], bg=COLORS["card"], width=4,
        )
        self.quality_label.pack(side="right")

        self.quality_slider = ttk.Scale(
            quality_row, from_=1, to=100, variable=self.quality,
            orient="horizontal", command=self._on_quality_change,
        )
        self.quality_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # ── Target size input (shown in target mode) ─────────────────────────
        self.target_section_frame = tk.Frame(mode_frame, bg=COLORS["card"])
        self.target_section_frame.pack(fill="x", pady=(6, 0))

        tk.Label(self.target_section_frame, text=self._t("target_size"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(anchor="w")

        target_row = tk.Frame(self.target_section_frame, bg=COLORS["card"])
        target_row.pack(fill="x", pady=(4, 0))

        self.target_entry = ttk.Entry(
            target_row, textvariable=self.target_size_value,
            font=FONT_BODY, width=8,
        )
        self.target_entry.pack(side="left")

        self.target_unit_combo = ttk.Combobox(
            target_row, textvariable=self.target_size_unit,
            values=["KB", "MB"], state="readonly",
            font=FONT_BODY, width=5,
        )
        self.target_unit_combo.pack(side="left", padx=(8, 0))

        tk.Label(target_row, text=self._t("autotune_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"], justify="left",
                 bg=COLORS["card"]).pack(side="left", padx=(8, 0))

        # Hide target section initially (manual mode default)
        self.target_section_frame.pack_forget()

        # ── Resize ──────────────────────────────────────────────────────────
        resize_frame = self._add_collapsible_section(OUT, "sec_resize")
        resize_frame.columnconfigure(1, weight=1)
        resize_frame.columnconfigure(3, weight=1)

        tk.Label(resize_frame, text=self._t("width"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="e", padx=(0, 4))
        w_entry = ttk.Entry(resize_frame, textvariable=self.max_width,
                            font=FONT_BODY, width=7)
        w_entry.grid(row=0, column=1, sticky="w")

        tk.Label(resize_frame, text=self._t("height"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=2, sticky="e", padx=(12, 4))
        h_entry = ttk.Entry(resize_frame, textvariable=self.max_height,
                            font=FONT_BODY, width=7)
        h_entry.grid(row=0, column=3, sticky="w")

        tk.Label(resize_frame, text=self._t("pixels_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Max pixels on the longest side (downscale only)
        tk.Label(resize_frame, text=self._t("max_pixels"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        px_entry = ttk.Entry(resize_frame, textvariable=self.max_pixels,
                             font=FONT_BODY, width=7)
        px_entry.grid(row=2, column=2, columnspan=2, sticky="w",
                      padx=(8, 0), pady=(8, 0))
        tk.Label(resize_frame, text=self._t("max_pixels_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # Scale percentage
        scale_frame = tk.Frame(resize_frame, bg=COLORS["card"])
        scale_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        tk.Label(scale_frame, text=self._t("scale"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(side="left")

        scale_entry = ttk.Entry(scale_frame, textvariable=self.scale_percent,
                                font=FONT_BODY, width=7)
        scale_entry.pack(side="left", padx=(8, 0))

        # ── Output Location ─────────────────────────────────────────────────
        out_frame = self._add_collapsible_section(OUT, "sec_output")
        out_frame.columnconfigure(0, weight=1)

        out_entry = ttk.Entry(out_frame, textvariable=self.output_dir,
                              font=FONT_SMALL)
        out_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        browse_btn = FlatButton(
            out_frame, text=self._t("browse"), command=self._browse_output_dir,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            font=FONT_SMALL, padx=10, pady=4, border_color=COLORS["border"],
        )
        browse_btn.grid(row=0, column=1)

        # Print size (blank = off)
        tk.Label(out_frame, text=self._t("print_size"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(out_frame, textvariable=self.print_size,
                  font=FONT_BODY, width=10).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        tk.Label(out_frame, text=self._t("print_size_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # ── Naming ──────────────────────────────────────────────────────────
        naming_frame = self._add_collapsible_section(OUT, "sec_naming")

        tk.Label(naming_frame, text=self._t("prefix"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w")
        ttk.Entry(naming_frame, textvariable=self.prefix,
                  font=FONT_BODY, width=10).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))

        tk.Label(naming_frame, text=self._t("suffix"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(naming_frame, textvariable=self.suffix,
                  font=FONT_BODY, width=10).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        tk.Label(naming_frame, text=self._t("smart_rename"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"], justify="left").grid(
            row=2, column=0, sticky="w", pady=(12, 0))
        self.rename_entry = ttk.Entry(
            naming_frame, textvariable=self.rename_pattern,
            font=FONT_SMALL, width=20,
        )
        self.rename_entry.grid(
            row=2, column=1, sticky="ew", padx=(8, 0), pady=(12, 0))
        tk.Label(naming_frame, text=self._t("rename_vars"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 0), padx=(0, 8))

        naming_frame.columnconfigure(1, weight=1)

        # ── Folder Organization ─────────────────────────────────────────────
        folder_frame = self._add_collapsible_section(OUT, "sec_subfolder")
        folder_frame.columnconfigure(1, weight=1)

        tk.Label(folder_frame, text=self._t("template"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w")

        # Preset list is localized; index maps to an internal value
        self._folder_preset_keys = [
            "preset_flat", "preset_date", "preset_camera",
            "preset_date_camera", "preset_custom",
        ]
        # Internal value per preset index; None = custom template
        self._folder_preset_values = ["", "date", "camera", "date-camera", None]

        self.folder_combo = ttk.Combobox(
            folder_frame, font=FONT_BODY, state="readonly",
            values=[self._t(k) for k in self._folder_preset_keys],
        )
        self.folder_combo.current(0)
        self.folder_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.folder_combo.bind("<<ComboboxSelected>>", self._on_folder_preset_change)

        tk.Label(folder_frame, text=self._t("custom"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=1, column=0, sticky="w", pady=(8, 0))

        self.folder_custom_entry = ttk.Entry(
            folder_frame, textvariable=self.folder_pattern,
            font=FONT_SMALL,
        )
        self.folder_custom_entry.grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        tk.Label(folder_frame, text=self._t("folder_vars"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0), padx=(0, 8))

        # ── Options ─────────────────────────────────────────────────────────
        opts_frame = self._add_collapsible_section(OPT, "sec_options")

        self._add_checkbox(opts_frame, self._t("preserve_exif"),
                           self.preserve_exif, row=0)
        self._add_checkbox(opts_frame, self._t("optimize"),
                           self.optimize, row=1)
        self._add_checkbox(opts_frame, self._t("progressive"),
                           self.progressive, row=2)
        self._add_checkbox(opts_frame, self._t("overwrite"),
                           self.overwrite, row=3)
        self._add_checkbox(opts_frame, self._t("auto_rotate"),
                           self.auto_rotate, row=4)
        self._add_checkbox(opts_frame, self._t("raw_half_size"),
                           self.raw_half_size, row=5)
        self._add_checkbox(opts_frame, self._t("raw_auto_bright"),
                           self.raw_auto_bright, row=6)
        self._add_checkbox(opts_frame, self._t("delete_original"),
                           self.remove_original, row=7)
        self._add_checkbox(opts_frame, self._t("strip_gps"),
                           self.strip_gps, row=8)
        self._add_checkbox(opts_frame, self._t("keep_mtime"),
                           self.keep_mtime, row=9)

        # Parallel workers
        tk.Label(opts_frame, text=self._t("jobs"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=10, column=0, sticky="w", pady=(10, 0))
        jobs_entry = ttk.Entry(opts_frame, textvariable=self.jobs,
                               font=FONT_BODY, width=5)
        jobs_entry.grid(row=10, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        # JPEG chroma subsampling (444 = full color, larger files)
        tk.Label(opts_frame, text=self._t("jpeg_subsampling"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=11, column=0, sticky="w", pady=(10, 0))
        sub_combo = ttk.Combobox(
            opts_frame, textvariable=self.jpeg_subsampling,
            values=["444", "422", "420"], state="readonly",
            font=FONT_BODY, width=5)
        sub_combo.grid(row=11, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        # RAW demosaic algorithm (amaze = highest quality, slowest)
        tk.Label(opts_frame, text=self._t("raw_demosaic"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=12, column=0, sticky="w", pady=(10, 0))
        dem_combo = ttk.Combobox(
            opts_frame, textvariable=self.raw_demosaic,
            values=["auto", "ahd", "vng", "ppg", "dcb", "dht", "amaze"],
            state="readonly", font=FONT_BODY, width=7)
        dem_combo.grid(row=12, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        # RAW output color space (wider gamuts untagged)
        tk.Label(opts_frame, text=self._t("raw_color_space"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=13, column=0, sticky="w", pady=(10, 0))
        cs_combo = ttk.Combobox(
            opts_frame, textvariable=self.raw_color_space,
            values=["sRGB", "AdobeRGB", "ProPhotoRGB"], state="readonly",
            font=FONT_BODY, width=10)
        cs_combo.grid(row=13, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        self._add_checkbox(opts_frame, self._t("raw_16bit"),
                           self.raw_16bit, row=14)

        # ── Cutout / background removal (v2.1.0) ─────────────────────────────
        cut_frame = self._add_collapsible_section(OPT, "sec_cutout")
        cut_frame.columnconfigure(1, weight=1)
        tk.Label(cut_frame, text=self._t("cutout_mode"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(cut_frame,
                     values=(self._t("cutout_off"), self._t("cutout_subject"),
                             self._t("cutout_person"), self._t("cutout_object"),
                             self._t("cutout_color")),
                     textvariable=self.cutout_mode, state="readonly",
                     width=12, font=FONT_SMALL).grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        # object mode: COCO class entry
        cut_label_frame = tk.Frame(cut_frame, bg=COLORS["card"])
        tk.Label(cut_label_frame, text=self._t("cutout_label"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).pack(side="left", padx=(0, 6))
        ttk.Entry(cut_label_frame, textvariable=self.cutout_label,
                  font=FONT_BODY, width=14).pack(side="left")
        # color mode: R/G/B + tol + feather entries
        cut_color_frame = tk.Frame(cut_frame, bg=COLORS["card"])
        for i, (key, var) in enumerate((
                ("cutout_r", self.cutout_r), ("cutout_g", self.cutout_g),
                ("cutout_b", self.cutout_b))):
            tk.Label(cut_color_frame, text=self._t(key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
                side="left", padx=(0 if i == 0 else 8, 2))
            ttk.Entry(cut_color_frame, textvariable=var, width=3,
                      font=FONT_BODY).pack(side="left")
        for key, var in (("cutout_tol", self.cutout_tol),
                         ("cutout_feather", self.cutout_feather)):
            tk.Label(cut_color_frame, text=self._t(key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
                side="left", padx=(12, 2))
            ttk.Entry(cut_color_frame, textvariable=var, width=4,
                      font=FONT_BODY).pack(side="left")
        cut_label_frame.grid(row=1, column=0, columnspan=2, sticky="w",
                             pady=(4, 0))
        cut_color_frame.grid(row=1, column=0, columnspan=2, sticky="w",
                             pady=(4, 0))
        tk.Label(cut_frame, text=self._t("cutout_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tk.Label(cut_frame, text=self._t("cutout_ai_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        # dynamic visibility: object shows the label frame, color the rgb frame
        self._cutout_frames = {"object": cut_label_frame,
                               "color": cut_color_frame}
        self.cutout_mode.trace_add("write", self._on_cutout_mode_changed)
        self._on_cutout_mode_changed()

        # ── Watermark ────────────────────────────────────────────────────────
        wm_frame = self._add_collapsible_section(FX, "sec_watermark")
        wm_frame.columnconfigure(1, weight=1)

        tk.Label(wm_frame, text=self._t("wm_text"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w")
        wm_text_entry = ttk.Entry(wm_frame, textvariable=self.watermark_text,
                                  font=FONT_BODY)
        wm_text_entry.grid(row=0, column=1, columnspan=2, sticky="ew",
                           padx=(8, 0))

        tk.Label(wm_frame, text=self._t("wm_image"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        wm_image_entry = ttk.Entry(wm_frame, textvariable=self.watermark_image,
                                   font=FONT_SMALL)
        wm_image_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0),
                            pady=(8, 0))
        wm_browse = FlatButton(
            wm_frame, self._t("browse"),
            lambda: self._browse_watermark_image(),
            COLORS["accent"], fg="white")
        wm_browse.grid(row=1, column=2, sticky="e", padx=(6, 0), pady=(8, 0))

        tk.Label(wm_frame, text=self._t("wm_position"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        wm_pos_combo = ttk.Combobox(
            wm_frame, textvariable=self.watermark_position, state="readonly",
            font=FONT_SMALL, width=14,
            values=list(watermark.POSITIONS.keys()))
        wm_pos_combo.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        tk.Label(wm_frame, text=self._t("wm_opacity"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=3, column=0, sticky="w", pady=(8, 0))
        wm_opacity_scale = ttk.Scale(
            wm_frame, from_=0, to=100, variable=self.watermark_opacity,
            command=lambda v: wm_opacity_lbl.config(
                text=f"{int(float(v))}%"))
        wm_opacity_scale.grid(row=3, column=1, sticky="ew", padx=(8, 0),
                              pady=(8, 0))
        wm_opacity_lbl = tk.Label(wm_frame, text="50%", font=FONT_SMALL,
                                  fg=COLORS["text_secondary"],
                                  bg=COLORS["card"], width=4)
        wm_opacity_lbl.grid(row=3, column=2, sticky="e", pady=(8, 0))

        # ── Multi-size output ─────────────────────────────────────────────────
        sizes_frame = self._add_collapsible_section(OUT, "sec_sizes")
        sizes_frame.columnconfigure(0, weight=1)

        sizes_entry = ttk.Entry(sizes_frame, textvariable=self.output_sizes,
                                font=FONT_BODY)
        sizes_entry.grid(row=0, column=0, sticky="ew")
        tk.Label(sizes_frame, text=self._t("sizes_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=1, column=0, sticky="w", pady=(4, 0))

        # ── Adjust (tone & color) ────────────────────────────────────────────
        adj_frame = self._add_collapsible_section(ADJ, "sec_adjust")
        adj_frame.columnconfigure(1, weight=1)

        adj_specs = [
            ("brightness", self.brightness, 0.0, 2.0, 0.05, "{:.2f}"),
            ("contrast", self.contrast, 0.0, 2.0, 0.05, "{:.2f}"),
            ("saturation", self.saturation, 0.0, 2.0, 0.05, "{:.2f}"),
            ("gamma", self.gamma, 0.1, 3.0, 0.05, "{:.2f}"),
            ("sharpen", self.sharpen, 0.0, 3.0, 0.05, "{:.2f}"),
        ]
        for i, (key, var, lo, hi, step, fmt) in enumerate(adj_specs):
            tk.Label(adj_frame, text=self._t(key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
                row=i, column=0, sticky="w")
            val_lbl = tk.Label(adj_frame, text=fmt.format(var.get()),
                               font=FONT_SMALL, fg=COLORS["text_secondary"],
                               bg=COLORS["card"], width=5)
            val_lbl.grid(row=i, column=2, sticky="e")
            ttk.Scale(adj_frame, from_=lo, to=hi, variable=var,
                      command=lambda v, lbl=val_lbl, f=fmt: lbl.config(
                          text=f.format(float(v)))).grid(
                row=i, column=1, sticky="ew", padx=(8, 0))

        self._add_checkbox(adj_frame, self._t("grayscale"),
                           self.grayscale, row=5)
        self._add_checkbox(adj_frame, self._t("sepia"),
                           self.sepia, row=6)

        # Export sharpening (LR-style output-stage USM; 0 = off)
        tk.Label(adj_frame, text=self._t("export_sharpen"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=7, column=0, sticky="w", pady=(8, 0))
        es_val = tk.Label(adj_frame, text="0.00", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["card"],
                          width=5)
        es_val.grid(row=7, column=2, sticky="e", pady=(8, 0))
        ttk.Scale(adj_frame, from_=0.0, to=2.0, variable=self.export_sharpen,
                  command=lambda v, lbl=es_val: lbl.config(
                      text=f"{float(v):.2f}")).grid(
            row=7, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        # Highlight recovery (LR-style; 0 = off)
        tk.Label(adj_frame, text=self._t("highlight_recovery"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=8, column=0, sticky="w", pady=(8, 0))
        hr_val = tk.Label(adj_frame, text="0.00", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["card"],
                          width=5)
        hr_val.grid(row=8, column=2, sticky="e", pady=(8, 0))
        ttk.Scale(adj_frame, from_=0.0, to=1.0, variable=self.highlight_recovery,
                  command=lambda v, lbl=hr_val: lbl.config(
                      text=f"{float(v):.2f}")).grid(
            row=8, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        # ── Composition (crop / rotate / flip / pad) ─────────────────────────
        comp_frame = self._add_collapsible_section(ADJ, "sec_composition")
        comp_frame.columnconfigure(1, weight=1)

        comp_specs = [
            ("crop", self.crop, None),
            ("crop_ratio", self.crop_ratio, None),
            ("rotate", self.rotate, None),
            ("rotate_bg", self.rotate_bg, None),
            ("pad", self.pad_ratio, None),
            ("pad_bg", self.pad_bg, None),
        ]
        for i, (key, var, _) in enumerate(comp_specs):
            tk.Label(comp_frame, text=self._t(key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
                row=i, column=0, sticky="w")
            ttk.Entry(comp_frame, textvariable=var, font=FONT_BODY).grid(
                row=i, column=1, sticky="ew", padx=(8, 0))

        tk.Label(comp_frame, text=self._t("flip"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=6, column=0, sticky="w", pady=(8, 0))
        flip_combo = ttk.Combobox(
            comp_frame, textvariable=self.flip, state="readonly",
            font=FONT_SMALL, width=6, values=["", "h", "v"])
        flip_combo.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        tk.Label(comp_frame, text=self._t("crop_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(comp_frame, text=self._t("pad_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # ── Correction (exposure / LOG / denoise / straighten) ───────────────
        corr_frame = self._add_collapsible_section(ADJ, "sec_correction")
        corr_frame.columnconfigure(1, weight=1)

        # EV exposure slider (-2..+2 stops)
        tk.Label(corr_frame, text=self._t("ev"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w")
        ev_lbl = tk.Label(corr_frame, text="{:+.2f}".format(self.ev.get()),
                          font=FONT_SMALL, fg=COLORS["text_secondary"],
                          bg=COLORS["card"], width=5)
        ev_lbl.grid(row=0, column=2, sticky="e")
        ttk.Scale(corr_frame, from_=-2.0, to=2.0, variable=self.ev,
                  command=lambda v, lbl=ev_lbl: lbl.config(
                      text="{:+.2f}".format(float(v)))).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))
        tk.Label(corr_frame, text=self._t("ev_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Auto-exposure target (blank = off)
        tk.Label(corr_frame, text=self._t("auto_exposure"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=2, column=0, sticky="w")
        ttk.Entry(corr_frame, textvariable=self.auto_exposure,
                  font=FONT_BODY, width=8).grid(
            row=2, column=1, sticky="w", padx=(8, 0))
        tk.Label(corr_frame, text=self._t("auto_exposure_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # LOG recovery curve (blank = off)
        tk.Label(corr_frame, text=self._t("log_curve"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=4, column=0, sticky="w")
        log_combo = ttk.Combobox(
            corr_frame, textvariable=self.log_curve, state="readonly",
            font=FONT_SMALL, width=8,
            values=["", "SLOG3", "CLOG3", "LOGC3", "DLOG", "VLOG", "HLG"])
        log_combo.grid(row=4, column=1, sticky="w", padx=(8, 0))
        tk.Label(corr_frame, text=self._t("log_curve_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Denoise strength (blank = off)
        tk.Label(corr_frame, text=self._t("denoise"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=6, column=0, sticky="w")
        ttk.Entry(corr_frame, textvariable=self.denoise,
                  font=FONT_BODY, width=8).grid(
            row=6, column=1, sticky="w", padx=(8, 0))
        tk.Label(corr_frame, text=self._t("denoise_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # LUT color grade (.cube file or preset name)
        tk.Label(corr_frame, text=self._t("lut"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=8, column=0, sticky="w")
        lut_entry = ttk.Entry(corr_frame, textvariable=self.lut_file,
                              font=FONT_SMALL)
        lut_entry.grid(row=8, column=1, sticky="ew", padx=(8, 0))
        FlatButton(corr_frame, text=self._t("browse"),
                   command=self._browse_lut,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).grid(
            row=8, column=2, sticky="e", padx=(4, 0))
        tk.Label(corr_frame, text=self._t("lut_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Auto-straighten + max angle
        self._add_checkbox(corr_frame, self._t("auto_straighten"),
                           self.auto_straighten, row=8)
        tk.Label(corr_frame, text=self._t("max_straighten_angle"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=9, column=0, sticky="w")
        ttk.Entry(corr_frame, textvariable=self.max_straighten_angle,
                  font=FONT_BODY, width=8).grid(
            row=9, column=1, sticky="w", padx=(8, 0))

        # ── White balance / color / evaluation (row 10+) ─────────────────────
        # WB temperature (blank = off)
        tk.Label(corr_frame, text=self._t("wb_temp"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=10, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(corr_frame, textvariable=self.wb_temp,
                  font=FONT_BODY, width=8).grid(
            row=10, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        tk.Label(corr_frame, text=self._t("wb_temp_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # WB reference image (blank = off)
        tk.Label(corr_frame, text=self._t("wb_reference"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=12, column=0, sticky="w")
        ref_entry = ttk.Entry(corr_frame, textvariable=self.wb_reference,
                              font=FONT_SMALL)
        ref_entry.grid(row=12, column=1, sticky="ew", padx=(8, 0))
        FlatButton(corr_frame, text=self._t("browse_ref"),
                   command=self._browse_wb_reference,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).grid(
            row=12, column=2, sticky="e", padx=(4, 0))
        tk.Label(corr_frame, text=self._t("wb_reference_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Checkboxes: auto levels / color / evaluation
        self._add_checkbox(corr_frame, self._t("auto_levels"),
                           self.auto_levels, row=14)
        self._add_checkbox(corr_frame, self._t("srgb"),
                           self.srgb, row=15)
        self._add_checkbox(corr_frame, self._t("flatten_cmyk"),
                           self.flatten_cmyk, row=16)
        self._add_checkbox(corr_frame, self._t("evaluate"),
                           self.evaluate, row=17)
        self._add_checkbox(corr_frame, self._t("blur_score"),
                           self.blur_score, row=18)
        self._add_checkbox(corr_frame, self._t("resume"),
                           self.resume, row=19)

        # ── LR-direction grading (v1.6.0) — blank = off ──────────────────────
        tk.Label(corr_frame, text=self._t("sec_grading"),
                 font=FONT_SECTION, fg=COLORS["text"],
                 bg=COLORS["card"]).grid(
            row=20, column=0, columnspan=3, sticky="w", pady=(14, 2))

        # entries for the scalar/compact specs; the three interactive
        # editors (curves / color-grading / hsl) get an "edit…" button
        _grade_widgets = [
            ("wb_tint", "wb_tint_hint"),
            ("levels", "levels_hint"),
            ("vibrance", "vibrance_hint"),
            ("clarity", "clarity_hint"),
            ("texture", "texture_hint"),
            ("dehaze", "dehaze_hint"),
            ("vignette", "vignette_hint"),
            ("grain", "grain_hint"),
        ]
        _grade_editors = [
            ("curves", "curves_hint", "edit_curves", "_open_curve_editor",
             "grade_curves_val"),
            ("color_grading", "color_grading_hint", "edit_wheels",
             "_open_color_wheel_dialog", "grade_wheels_val"),
            ("hsl", "hsl_hint", "edit_hsl", "_open_hsl_dialog",
             "grade_hsl_val"),
            ("point_color", "point_color_hint", "edit_point_color",
             "_open_point_color_dialog", "grade_point_color_val"),
            ("masks", "masks_hint", "edit_masks", "_dev_enter_mask_mode",
             "grade_masks_val"),
        ]
        for off, (var_key, hint_key) in enumerate(_grade_widgets):
            r = 21 + off * 2
            tk.Label(corr_frame, text=self._t(var_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
                row=r, column=0, sticky="w")
            ttk.Entry(corr_frame, textvariable=getattr(self, var_key),
                      font=FONT_BODY, width=8).grid(
                row=r, column=1, sticky="w", padx=(8, 0))
            tk.Label(corr_frame, text=self._t(hint_key),
                     font=FONT_TINY, fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).grid(
                row=r + 1, column=0, columnspan=3, sticky="w", pady=(0, 4))
        for off, (var_key, hint_key, btn_key, method, val_attr) in \
                enumerate(_grade_editors):
            r = 21 + (len(_grade_widgets) + off) * 2
            tk.Label(corr_frame, text=self._t(var_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
                row=r, column=0, sticky="w")
            FlatButton(corr_frame, text=self._t(btn_key),
                       command=getattr(self, method),
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=8, pady=2, border_color=COLORS["border"]).grid(
                row=r, column=1, sticky="w", padx=(8, 0))
            val_lbl = tk.Label(corr_frame, text="", font=FONT_TINY,
                               fg=COLORS["text_secondary"],
                               bg=COLORS["card"], anchor="w")
            val_lbl.grid(row=r, column=2, sticky="ew", padx=(4, 0))
            corr_frame.columnconfigure(2, weight=1)
            setattr(self, val_attr, val_lbl)
            tk.Label(corr_frame, text=self._t(hint_key),
                     font=FONT_TINY, fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).grid(
                row=r + 1, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self._refresh_grade_value_labels()

        # ── Lens correction (v1.7.0) - manual params, blank = off ───────────
        lens_frame = self._add_collapsible_section(ADJ, "sec_lens",
                                                   default_open=False)
        lens_frame.columnconfigure(1, weight=1)
        _lens_fields = [
            ("lens_distort", "lens_distort_hint"),
            ("lens_vignette", "lens_vignette_hint"),
            ("lens_ca", "lens_ca_hint"),
        ]
        for off, (var_key, hint_key) in enumerate(_lens_fields):
            r = off * 2
            tk.Label(lens_frame, text=self._t(var_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
                row=r, column=0, sticky="w")
            ttk.Entry(lens_frame, textvariable=getattr(self, var_key),
                      font=FONT_BODY, width=10).grid(
                row=r, column=1, sticky="w", padx=(8, 0))
            tk.Label(lens_frame, text=self._t(hint_key),
                     font=FONT_TINY, fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).grid(
                row=r + 1, column=0, columnspan=3, sticky="w", pady=(0, 4))

        # Lens profile (user-maintained; see photo-s lens-profile)
        tk.Label(lens_frame, text=self._t("lens_profile"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=6, column=0, sticky="w", pady=(8, 0))
        try:
            from ..lensprofile import lens_profile_names
            _lens_profiles = [""] + lens_profile_names()
        except Exception:
            _lens_profiles = [""]
        lp_combo = ttk.Combobox(
            lens_frame, textvariable=self.lens_profile,
            values=_lens_profiles, state="readonly", font=FONT_BODY, width=18)
        lp_combo.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        # ── Metadata section ──────────────────────────────────────────────────
        meta_frame = self._add_collapsible_section(META, "sec_metadata")
        meta_frame.columnconfigure(1, weight=1)

        # EXIF date shift (blank = off)
        tk.Label(meta_frame, text=self._t("date_shift"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=0, column=0, sticky="w")
        ttk.Entry(meta_frame, textvariable=self.date_shift,
                  font=FONT_BODY, width=8).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        tk.Label(meta_frame, text=self._t("date_shift_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # GPX track (blank = off)
        tk.Label(meta_frame, text=self._t("gpx_trace"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(meta_frame, textvariable=self.gpx_trace,
                  font=FONT_SMALL).grid(
            row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        FlatButton(meta_frame, text=self._t("browse_gpx"),
                   command=self._browse_gpx,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).grid(
            row=2, column=2, sticky="e", padx=(4, 0), pady=(8, 0))
        tk.Label(meta_frame, text=self._t("gpx_trace_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self._add_checkbox(meta_frame, self._t("sync_date"),
                           self.sync_date, row=4)
        self._add_checkbox(meta_frame, self._t("scrub"),
                           self.scrub, row=5)

        # Face blur (privacy mask; needs opencv via [enhance] extra)
        tk.Label(meta_frame, text=self._t("blur_faces"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).grid(
            row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(meta_frame,
                     values=(self._t("blur_faces_off"), self._t("blur_faces_blur"),
                             self._t("blur_faces_pixelate")),
                     textvariable=self.blur_faces, state="readonly",
                     width=12, font=FONT_SMALL).grid(
            row=6, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        tk.Label(meta_frame, text=self._t("blur_faces_margin_lbl"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=6, column=2, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Entry(meta_frame, textvariable=self.blur_faces_margin,
                  font=FONT_BODY, width=4).grid(
            row=6, column=3, sticky="w", padx=(4, 0), pady=(8, 0))
        tk.Label(meta_frame, text=self._t("blur_faces_hint"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["card"]).grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(2, 0))

    def _browse_wb_reference(self):
        """Pick a white-balance reference image (gray card)."""
        if self._dlg_cooldown_active():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self._t("wb_reference"),
            filetypes=[("图片 Images", "*.jpg *.jpeg *.png *.webp *.tif *.tiff")])
        self._after_file_dialog()
        if path:
            self.wb_reference.set(path)

    def _browse_lut(self):
        """Pick a .cube LUT file."""
        if self._dlg_cooldown_active():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self._t("lut"),
            filetypes=[("LUT 文件 LUT", "*.cube"), ("全部 All", "*.*")])
        self._after_file_dialog()
        if path:
            self.lut_file.set(path)

    def _refresh_grade_value_labels(self):
        """Show the current curves / color-grading / hsl specs under buttons."""
        for var_key, attr in (("curves", "grade_curves_val"),
                              ("color_grading", "grade_wheels_val"),
                              ("hsl", "grade_hsl_val"),
                              ("point_color", "grade_point_color_val"),
                              ("masks", "grade_masks_val")):
            lbl = getattr(self, attr, None)
            if lbl is None:
                continue
            text = getattr(self, var_key).get()
            if not text:
                text = self._t("grade_none")
            elif len(text) > 34:
                text = text[:33] + "…"
            lbl.config(text=text)

    # ── Shared photo reference (v1.8.0) ─────────────────────────────────

    def _add_photo_reference(self, parent, on_pick=None, max_w=480,
                             max_h=240, render_fn=None):
        """Embed a paged photo preview into an editor dialog.

        Shows the first checked photo (fit into max_w x max_h), with
        prev/next paging across all checked photos. ``on_pick(rgb)``, when
        given, is called with the sampled pixel color on click (used by
        the point-color editor). ``render_fn(base_img, path) -> Image``,
        when given, is called on every page change and via the returned
        ``refresh()`` to show the live effect of the editor's parameters
        (curve/wheel/HSL/point-color dialogs). Returns a dict with
        ``get_index``, ``set_index`` and ``refresh``.
        """
        from PIL import Image as PILImage, ImageTk
        files = self._checked_files()
        st = {"files": files, "idx": 0, "tk": None, "pil": None,
              "scale": 1.0, "orig": None}
        frame = tk.Frame(parent, bg=COLORS["bg"])
        nav = tk.Frame(frame, bg=COLORS["bg"])
        nav.pack(fill="x")
        page = tk.Label(nav, text="", font=FONT_TINY,
                        fg=COLORS["text_secondary"], bg=COLORS["bg"])
        page.pack(side="left")
        img_lbl = tk.Label(frame, bg=COLORS["card"])
        img_lbl.pack(fill="both", expand=True, pady=(4, 0))

        def _show(img, path):
            st["tk"] = ImageTk.PhotoImage(img, master=img_lbl)
            img_lbl.config(image=st["tk"])
            img_lbl.image = st["tk"]
            page.config(text=self._t(
                "mask_page", cur=st["idx"] + 1, total=len(st["files"])))

        def _load(i=None):
            if i is not None:
                st["idx"] = i % max(1, len(st["files"]))
            if not st["files"]:
                page.config(text=self._t("mask_no_check"))
                return
            path = st["files"][st["idx"]]
            base = None
            try:
                # _open_image_safe：PIL 开不了 RAW（.ARW 等），
                # 回退引擎加载器（rawpy/HEIC）——摄影师工作流主力格式
                base = _open_image_safe(path).convert("RGB")
                base.thumbnail((max_w, max_h), PILImage.LANCZOS)
            except Exception:
                base = PILImage.new("RGB", (max_w, max_h), (40, 40, 40))
            st["orig"] = base
            st["pil"] = base
            _show(base, path)
            _refresh()  # 打开/翻页即显示编辑器状态（不再停留在原图）

        def _refresh():
            """Re-render with the current editor state (no-op without
            render_fn). Errors fall back to the plain photo."""
            if render_fn is None or st.get("orig") is None:
                return
            try:
                base = st["orig"].copy()
                out = render_fn(base, st["files"][st["idx"]])
                if out is not None:
                    out.thumbnail((max_w, max_h), PILImage.LANCZOS)
                    st["pil"] = out
                    _show(out, st["files"][st["idx"]])
            except Exception:
                _show(st["orig"], st["files"][st["idx"]])

        def _flip(delta):
            st["idx"] = (st["idx"] + delta) % max(1, len(st["files"]))
            _load()

        def _pick(evt):
            if on_pick is None or st["pil"] is None:
                return
            w, h = st["pil"].width, st["pil"].height
            x = max(0, min(w - 1, int(evt.x / (img_lbl.winfo_width() or 1)
                                      * w)))
            y = max(0, min(h - 1, int(evt.y / (img_lbl.winfo_height() or 1)
                                      * h)))
            on_pick(st["pil"].getpixel((x, y)))

        img_lbl.bind("<Button-1>", _pick)

        btn_row = tk.Frame(frame, bg=COLORS["bg"])
        btn_row.pack(fill="x", pady=(4, 0))
        for text, cmd in ((self._t("mask_prev"), lambda: _flip(-1)),
                          (self._t("mask_next"), lambda: _flip(1))):
            FlatButton(btn_row, text=text, command=cmd,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=6, pady=1, border_color=COLORS["border"]).pack(
                side="left", padx=(0, 6))
        _load()
        return {"get_index": lambda: st["idx"],
                "set_index": lambda i: _load(i),
                "refresh": _refresh, "frame": frame,
                "files": st["files"]}

    def _open_curve_editor(self):
        """Draggable point-curve editor: RGB master + R/G/B tabs."""
        from .widgets import CurveEditor
        from ..grade import _parse_curves
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("dlt_curves"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.geometry("480x560")
        cur = _parse_curves(self.curves.get()) if self.curves.get() else {}
        base = cur.get("rgb")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        editors = {}
        for ch, chname in (("rgb", "RGB"), ("r", "R"), ("g", "G"), ("b", "B")):
            tab = ttk.Frame(nb)
            nb.add(tab, text=chname)
            pts = cur.get(ch) or base or [(0, 0), (255, 255)]
            ed = CurveEditor(tab, channel=ch, points=pts,
                             width=320, height=210)
            ed.pack(fill="both", expand=True, padx=8, pady=8)
            editors[ch] = ed

        def _curve_render(base_img, _path):
            """Render the current curve state onto the preview (live)."""
            from ..grade import _parse_curves, apply_curves
            specs = []
            for ch, ed in editors.items():
                if CurveEditor.is_identity(ed.get_points()):
                    continue
                specs.append(ed.to_spec(ch))
            if not specs:
                return base_img
            return apply_curves(base_img, _parse_curves("|".join(specs)))

        # photo reference strip (v1.8.0): live-renders the curve state
        ref = self._add_photo_reference(win, render_fn=_curve_render)
        ref["frame"].pack(fill="x", padx=10)
        for ed in editors.values():
            ed.on_change = lambda _e, r=ref: r["refresh"]()
        btns = tk.Frame(win, bg=COLORS["bg"])
        btns.pack(fill="x", padx=10, pady=(0, 10))
        FlatButton(btns, text=self._t("ok"),
                   command=lambda: self._curve_editor_ok(win, editors),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right")
        FlatButton(btns, text=self._t("cancel"), command=win.destroy,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right", padx=(0, 8))
        FlatButton(btns, text=self._t("reset"),
                   command=lambda: self._reset_curves(editors),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="left")

    def _reset_curves(self, editors):
        """Restore every curve channel to identity (0,0)→(255,255)."""
        for ed in editors.values():
            ed.set_points([(0, 0), (255, 255)])

    def _curve_editor_ok(self, win, editors):
        from .widgets import CurveEditor
        specs = []
        for ch, ed in editors.items():
            if CurveEditor.is_identity(ed.get_points()):
                continue
            specs.append(ed.to_spec(ch))
        self.curves.set("|".join(specs))
        self._refresh_grade_value_labels()
        if win is not None:
            win.destroy()

    def _open_color_wheel_dialog(self):
        """Three HSV wheels (shadows/midtones/highlights) — LR color grading.

        Each wheel carries a luminance slider (brightness bar): the picked
        hue/sat tint the zone, the luminance shifts its value.
        """
        from .widgets import ColorWheel
        from ..grade import _parse_color_grading
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("dlt_wheels"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        cur = (_parse_color_grading(self.color_grading.get())
               if self.color_grading.get() else {})
        wheels, lums = {}, {}
        for zone, zkey in (("shadows", "zone_shadows"),
                           ("midtones", "zone_midtones"),
                           ("highlights", "zone_highlights")):
            frame = ttk.LabelFrame(win, text=self._t(zkey))
            frame.pack(side="left", padx=6, pady=8)
            wheel = ColorWheel(frame, size=150)
            hue, sat, lum = cur.get(zone, (0.0, 0.0, 0.0))
            wheel.set_value(hue, sat)
            wheel.pack(padx=6, pady=(6, 2))
            # luminance (brightness) bar under the wheel
            lum_row = ttk.Frame(frame)
            lum_row.pack(fill="x", padx=4, pady=(0, 6))
            lum_var = tk.DoubleVar(value=lum * 100.0)
            lums[zone] = lum_var
            ttk.Label(lum_row, text=self._t("grade_lum"),
                      font=FONT_SMALL).pack(side="left")
            ttk.Scale(lum_row, from_=-100.0, to=100.0, variable=lum_var,
                      length=80).pack(side="left", fill="x", expand=True,
                                      padx=4)
            lum_lbl = ttk.Label(lum_row, width=5, font=FONT_SMALL)
            lum_lbl.pack(side="right")
            lum_var.trace_add("write",
                              lambda *a, v=lum_var, l=lum_lbl:
                              l.config(text="{:+.0f}".format(v.get())))
            lum_lbl.config(text="{:+.0f}".format(lum * 100.0))
            wheels[zone] = wheel
        btns = tk.Frame(win, bg=COLORS["bg"])
        btns.pack(fill="x", padx=10, pady=(0, 10))
        FlatButton(btns, text=self._t("ok"),
                   command=lambda: self._wheels_ok(win, wheels, lums),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right")
        FlatButton(btns, text=self._t("cancel"), command=win.destroy,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right", padx=(0, 8))

        def _wheels_render(base_img, _path):
            """Render current wheel state onto the preview (live)."""
            from ..grade import _parse_color_grading, apply_color_grading
            specs = []
            for zone, wheel in wheels.items():
                h, s = wheel.get_value()
                lum = (lums or {}).get(zone, None)
                lum_val = float(lum.get()) / 100.0 \
                    if lum is not None else 0.0
                if s <= 0.02 and abs(lum_val) < 0.005:
                    continue
                base = f"{zone}:{int(round(h))},{s:.2f}"
                specs.append(base if abs(lum_val) < 0.005
                             else f"{base},{lum_val:.2f}")
            if not specs:
                return base_img
            parsed = _parse_color_grading(";".join(specs))
            shadows = parsed.get("shadows", (0.0, 0.0, 0.0))
            midtones = parsed.get("midtones", (0.0, 0.0, 0.0))
            highs = parsed.get("highlights", (0.0, 0.0, 0.0))
            return apply_color_grading(base_img, shadows, midtones, highs)

        # photo reference strip across the bottom (v1.8.0), live-rendered
        ref = self._add_photo_reference(win, max_w=380, max_h=150,
                                        render_fn=_wheels_render)
        ref["frame"].pack(fill="x", padx=10, pady=(0, 4))
        for wheel in wheels.values():
            wheel.on_change = lambda _e, r=ref: r["refresh"]()
        for lum_var in lums.values():
            lum_var.trace_add("write",
                              lambda *a, r=ref: r["refresh"]())

    def _wheels_ok(self, win, wheels, lums=None):
        specs = []
        for zone, wheel in wheels.items():
            h, s = wheel.get_value()
            lum = (lums or {}).get(zone, None)
            lum_val = float(lum.get()) / 100.0 if lum is not None else 0.0
            if s <= 0.02 and abs(lum_val) < 0.005:
                continue  # centre + no luminance → untouched
            base = f"{zone}:{int(round(h))},{s:.2f}"
            specs.append(base if abs(lum_val) < 0.005
                         else f"{base},{lum_val:.2f}")
        self.color_grading.set(";".join(specs))
        self._refresh_grade_value_labels()
        if win is not None:
            win.destroy()

    def _open_hsl_dialog(self):
        """8-color HSL split editor (click a chip, drive h/s/l sliders)."""
        from .widgets import HSLPanel, HSL_COLORS
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("dlt_hsl"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        labels = {name: self._t(f"hsl_{name}") for name, _ in HSL_COLORS}
        labels.update({"hue": self._t("hsl_hue"),
                       "sat": self._t("hsl_sat"),
                       "lum": self._t("hsl_lum")})
        panel = HSLPanel(win, labels=labels)
        panel.load(self.hsl.get())
        panel.pack(padx=12, pady=12, side="left")

        def _hsl_render(base_img, _path):
            """Render current HSL state onto the preview (live)."""
            from ..grade import _parse_hsl, apply_hsl
            s = panel.dump()
            if not s:
                return base_img
            return apply_hsl(base_img, _parse_hsl(s))

        # photo reference strip on the right (v1.8.0), live-rendered
        ref = self._add_photo_reference(win, max_w=320, max_h=200,
                                        render_fn=_hsl_render)
        ref["frame"].pack(side="left", fill="both", expand=True,
                          padx=(0, 12))
        panel.on_change = lambda *a, r=ref: r["refresh"]()
        btns = tk.Frame(win, bg=COLORS["bg"])
        btns.pack(fill="x", side="bottom", padx=12, pady=(0, 12))
        FlatButton(btns, text=self._t("ok"),
                   command=lambda: self._hsl_ok(win, panel),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right")
        FlatButton(btns, text=self._t("cancel"), command=win.destroy,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right", padx=(0, 8))

    def _hsl_ok(self, win, panel):
        self.hsl.set(panel.dump())
        self._refresh_grade_value_labels()
        if win is not None:
            win.destroy()

    # ── Point color editor (v1.7.0) ─────────────────────────────────────

    def _open_point_color_dialog(self):
        """Form editor for the point_color compact spec (list + sliders)."""
        from ..grade import _parse_point_color
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("dlt_point_color"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.geometry("820x560")

        try:
            targets = list(_parse_point_color(self.point_color.get()))
        except ValueError:
            targets = []

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=12)

        lst = tk.Listbox(body, width=24, height=8, font=FONT_SMALL,
                         exportselection=False, bg=COLORS["card"],
                         fg=COLORS["text"], relief="flat",
                         highlightthickness=1,
                         highlightbackground=COLORS["border"])
        lst.grid(row=0, column=0, rowspan=9, sticky="ns", padx=(0, 12))

        editor = tk.Frame(body, bg=COLORS["bg"])
        editor.grid(row=0, column=1, sticky="n")

        pc_r = tk.StringVar(value="200")
        pc_g = tk.StringVar(value="120")
        pc_b = tk.StringVar(value="80")
        pc_hue = tk.DoubleVar(value=0.0)
        pc_sat = tk.DoubleVar(value=0.0)
        pc_lum = tk.DoubleVar(value=0.0)
        pc_range = tk.DoubleVar(value=0.15)
        swatch = tk.Canvas(editor, width=44, height=20,
                           bg=COLORS["card"], highlightthickness=0)

        def _sync_swatch(*_):
            try:
                rgb = (max(0, min(255, int(pc_r.get()))),
                       max(0, min(255, int(pc_g.get()))),
                       max(0, min(255, int(pc_b.get()))))
            except ValueError:
                return
            swatch.config(bg="#%02x%02x%02x" % rgb)

        tk.Label(editor, text=self._t("pc_sample"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=0, column=0, columnspan=2, sticky="w")
        rgb_row = tk.Frame(editor, bg=COLORS["bg"])
        rgb_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for i, var in enumerate((pc_r, pc_g, pc_b)):
            ttk.Entry(rgb_row, textvariable=var, font=FONT_BODY,
                      width=5).grid(row=0, column=i, padx=(0, 4))
            var.trace_add("write", _sync_swatch)
        swatch.grid(row=0, column=3, padx=(8, 0), rowspan=2)

        _sliders = [
            ("pc_hue", pc_hue, -180, 180, 1),
            ("pc_sat", pc_sat, -100, 100, 1),
            ("pc_lum", pc_lum, -100, 100, 1),
            ("pc_range", pc_range, 2, 100, 1),
        ]
        for off, (key, var, lo, hi, res) in enumerate(_sliders):
            tk.Label(editor, text=self._t(key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
                row=2 + off * 2, column=0, sticky="w")
            ttk.Scale(editor, from_=lo, to=hi, variable=var).grid(
                row=2 + off * 2, column=1, sticky="ew", padx=(8, 0))
        editor.columnconfigure(1, weight=1)

        def _refresh_list():
            lst.delete(0, tk.END)
            for r, g, b, h, s, l, rng in targets:
                lst.insert(tk.END, f"{r},{g},{b}  h{h:+.0f} s{s:+.2f} "
                                   f"l{l:+.2f} r{rng:.2f}")

        def _load_selected(_evt=None):
            sel = lst.curselection()
            if not sel:
                return
            r, g, b, h, s, l, rng = targets[sel[0]]
            pc_r.set(str(r)); pc_g.set(str(g)); pc_b.set(str(b))
            pc_hue.set(float(h)); pc_sat.set(float(s)); pc_lum.set(float(l))
            pc_range.set(float(rng * 100))

        def _read_fields():
            def _ch(v):
                try:
                    return max(0, min(255, int(float(v))))
                except (TypeError, ValueError):
                    return 0  # 非数字输入不崩 Tk 回调（_sync_swatch 同款防御）
            return (_ch(pc_r.get()), _ch(pc_g.get()), _ch(pc_b.get()),
                    pc_hue.get(), pc_sat.get() / 100.0,
                    pc_lum.get() / 100.0, pc_range.get() / 100.0)

        def _add():
            targets.append(_read_fields())
            _refresh_list()
            lst.selection_clear(0, tk.END)
            lst.selection_set(tk.END)

        def _update():
            sel = lst.curselection()
            if sel:
                targets[sel[0]] = _read_fields()
                _refresh_list()
                lst.selection_set(sel[0])

        def _delete():
            sel = lst.curselection()
            if sel:
                del targets[sel[0]]
                _refresh_list()

        lst.bind("<<ListboxSelect>>", _load_selected)
        _refresh_list()

        btns = tk.Frame(editor, bg=COLORS["bg"])
        btns.grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))
        for text, cmd in ((self._t("pc_add"), _add),
                          (self._t("pc_update"), _update),
                          (self._t("pc_delete"), _delete)):
            FlatButton(btns, text=text, command=cmd,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=8, pady=2, border_color=COLORS["border"]).pack(
                side="left", padx=(0, 6))

        # photo reference strip: click the photo to sample its color
        def _on_pick(rgb):
            pc_r.set(str(rgb[0]))
            pc_g.set(str(rgb[1]))
            pc_b.set(str(rgb[2]))
            _sync_swatch()

        def _pc_render(base_img, _path):
            """Render current point-color targets onto the preview."""
            from ..grade import apply_point_color
            if not targets:
                return base_img
            return apply_point_color(base_img, list(targets))

        ref = self._add_photo_reference(win, on_pick=_on_pick, max_w=300,
                                        max_h=240,
                                        render_fn=_pc_render)
        ref["frame"].pack(side="left", fill="both", expand=True,
                          padx=(12, 0), pady=(0, 12))
        # live re-render when a target is added/updated/deleted
        _orig_add, _orig_update, _orig_delete = _add, _update, _delete

        def _add_refresh():
            _orig_add()
            ref["refresh"]()

        def _update_refresh():
            _orig_update()
            ref["refresh"]()

        def _delete_refresh():
            _orig_delete()
            ref["refresh"]()

        _add, _update, _delete = _add_refresh, _update_refresh, \
            _delete_refresh

        bottom = tk.Frame(win, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        FlatButton(bottom, text=self._t("ok"),
                   command=lambda: self._point_color_ok(win, targets),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right")
        FlatButton(bottom, text=self._t("cancel"), command=win.destroy,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right", padx=(0, 8))
        _sync_swatch()

    def _point_color_ok(self, win, targets):
        def _n(v):
            v = round(float(v), 3)
            return str(int(v)) if v == int(v) else str(v)
        segs = []
        for r, g, b, h, s, l, rng in targets:
            segs.append(f"{r},{g},{b}:{_n(h)},{_n(s)},{_n(l)},{_n(rng)}")
        self.point_color.set(";".join(segs))
        self._refresh_grade_value_labels()
        if win is not None:
            win.destroy()

    # ── Local mask editor (v1.7.0) ──────────────────────────────────────

    def _open_mask_dialog(self):
        """Form editor for masks + mask_adjust, with red-overlay preview."""
        from ..mask import parse_masks, parse_mask_adjust, render_mask
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("dlt_masks"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        try:
            specs = [(s.name, s.kind, list(s.params), s.feather, s.invert)
                     for s in parse_masks(self.masks.get())]
        except Exception:
            specs = []
        try:
            adjusts = {k: dict(v) for k, v in
                       parse_mask_adjust(self.mask_adjust.get()).items()}
        except Exception:
            adjusts = {}

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=12)

        lst = tk.Listbox(body, width=18, height=6, font=FONT_SMALL,
                         exportselection=False, bg=COLORS["card"],
                         fg=COLORS["text"], relief="flat",
                         highlightthickness=1,
                         highlightbackground=COLORS["border"])
        lst.grid(row=0, column=0, sticky="nw", padx=(0, 12))

        editor = tk.Frame(body, bg=COLORS["bg"])
        editor.grid(row=0, column=1, sticky="nw")
        editor.columnconfigure(1, weight=1)

        m_name = tk.StringVar(value="mask1")
        m_type = tk.StringVar(value="linear")
        m_params = [tk.StringVar(value=v) for v in
                    ("0.5", "0", "0.5", "1")]
        m_feather = tk.DoubleVar(value=0.0)
        m_invert = tk.BooleanVar(value=False)
        # per-mask adjustment sliders (value 0 = untouched)
        adj_vars = {key: tk.DoubleVar(value=0.0) for key in
                    ("exposure", "brightness", "contrast", "saturation",
                     "vibrance", "clarity", "texture", "sharpen",
                     "temp", "tint", "blur")}
        _current = {"name": None}

        tk.Label(editor, text=self._t("mask_name"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=0, column=0, sticky="w")
        ttk.Entry(editor, textvariable=m_name, font=FONT_BODY,
                  width=10).grid(row=0, column=1, sticky="w", padx=(8, 0))
        tk.Label(editor, text=self._t("mask_type"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=1, column=0, sticky="w")
        type_box = ttk.Combobox(editor, textvariable=m_type, width=12,
                                state="readonly", values=(
                                    self._t("mask_linear"),
                                    self._t("mask_radial"),
                                    self._t("mask_color"),
                                    self._t("mask_brush")))
        type_box.grid(row=1, column=1, sticky="w", padx=(8, 0))

        param_lbls = [tk.Label(editor, text="", font=FONT_SMALL,
                               fg=COLORS["text_secondary"],
                               bg=COLORS["bg"]) for _ in range(4)]
        param_widgets = []
        for i in range(4):
            param_lbls[i].grid(row=2 + i, column=0, sticky="w")
            e = ttk.Entry(editor, textvariable=m_params[i], font=FONT_BODY,
                          width=8)
            e.grid(row=2 + i, column=1, sticky="w", padx=(8, 0))
            param_widgets.append(e)

        _PARAM_LABELS = {
            "linear": ("x0", "y0", "x1", "y1"),
            "radial": ("cx", "cy", "rx", "ry"),
            "color": ("r", "g", "b", "tol"),
        }

        # ── brush: draw on a small canvas, dots stored as (x, y, r) ──
        m_brush_points = []  # list of (x_rel, y_rel, r_rel)
        _brush_r = tk.DoubleVar(value=0.06)  # radius as fraction of short side
        _brush_canvas = None

        def _type_key():
            for key, label in (("linear", "mask_linear"),
                               ("radial", "mask_radial"),
                               ("color", "mask_color"),
                               ("brush", "mask_brush")):
                if m_type.get() == self._t(label):
                    return key
            return "linear"

        def _sync_type(*_):
            key = _type_key()
            if key == "brush":
                for w in param_widgets + param_lbls:
                    w.grid_remove()
                _brush_canvas.grid()
                return
            for w in param_widgets + param_lbls:
                w.grid()
            _brush_canvas.grid_remove()
            for i, lbl in enumerate(_PARAM_LABELS[key]):
                param_lbls[i].config(text=lbl)

        def _brush_paint(evt):
            """Collect a dot at the canvas position (relative 0-1 coords)."""
            cw = max(1, _brush_canvas.winfo_width())
            ch = max(1, _brush_canvas.winfo_height())
            x, y = evt.x / cw, evt.y / ch
            x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
            r = max(0.005, min(0.5, _brush_r.get()))
            m_brush_points.append((round(x, 4), round(y, 4), r))
            rad = max(2, r * min(cw, ch))
            _brush_canvas.create_oval(evt.x - rad, evt.y - rad,
                                      evt.x + rad, evt.y + rad,
                                      fill="#ff4444", outline="")

        def _brush_clear():
            m_brush_points.clear()
            _brush_canvas.delete("all")

        _brush_canvas = tk.Canvas(editor, width=180, height=120,
                                  bg=COLORS["card"], highlightthickness=1,
                                  highlightbackground=COLORS["border"])
        _brush_canvas.grid(row=2, column=0, columnspan=2, sticky="w")
        _brush_canvas.bind("<B1-Motion>", _brush_paint)
        _brush_canvas.bind("<Button-1>", _brush_paint)
        tk.Label(editor, text=self._t("mask_brush_size"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=3, column=0, sticky="w")
        ttk.Scale(editor, from_=0.01, to=0.3, variable=_brush_r).grid(
            row=3, column=1, sticky="ew", padx=(8, 0))
        FlatButton(editor, text=self._t("mask_brush_clear"),
                   command=_brush_clear,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=6, pady=1, border_color=COLORS["border"]).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        type_box.bind("<<ComboboxSelected>>", _sync_type)
        _sync_type()

        tk.Label(editor, text=self._t("mask_feather"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=6, column=0, sticky="w")
        ttk.Scale(editor, from_=0, to=100, variable=m_feather).grid(
            row=6, column=1, sticky="ew", padx=(8, 0))
        ttk.Checkbutton(editor, text=self._t("mask_invert"),
                        variable=m_invert).grid(
            row=7, column=0, columnspan=2, sticky="w")

        # adjustment sliders, two per row
        tk.Label(editor, text=self._t("mask_adjust_sec"), font=FONT_SMALL,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(8, 2))
        _ADJ_META = (
            ("exposure", -3, 3), ("brightness", -1, 1), ("contrast", -1, 1),
            ("saturation", -1, 1), ("vibrance", -1, 1), ("clarity", -1, 1),
            ("texture", -1, 1), ("sharpen", -1, 1), ("temp", 0, 12000),
            ("tint", -100, 100), ("blur", 0, 50),
        )
        for i, (key, lo, hi) in enumerate(_ADJ_META):
            r, c = 9 + i // 2, (i % 2) * 2
            tk.Label(editor, text=self._t("adj_" + key), font=FONT_TINY,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
                row=r, column=c, sticky="w")
            ttk.Scale(editor, from_=lo, to=hi,
                      variable=adj_vars[key]).grid(
                row=r, column=c + 1, sticky="ew", padx=(6, 10))

        # ── preview: red overlay of the current form spec on the first
        # checked file (geometric masks also work on a neutral canvas)
        preview = tk.Label(body, text=self._t("mask_no_preview"),
                           font=FONT_SMALL, fg=COLORS["text_secondary"],
                           bg=COLORS["card"], width=44, height=14)
        preview.grid(row=1, column=0, columnspan=2, sticky="w",
                     pady=(10, 0))
        _photo = {"img": None}  # keep a ref so ImageTk isn't GC'd

        def _refresh_preview():
            from PIL import Image as PILImage, ImageTk
            files = self._checked_files()
            base = None
            if files:
                try:
                    base = PILImage.open(files[0]).convert("RGB")
                    base.thumbnail((360, 270), PILImage.LANCZOS)
                except Exception:
                    base = None
            if base is None:
                base = PILImage.new("RGB", (360, 240), (60, 60, 60))
            try:
                key = _type_key()
                if key == "brush":
                    if not m_brush_points:
                        raise ValueError("no dots")
                    from ..mask import MaskSpec
                    spec = MaskSpec("brush", tuple(m_brush_points),
                                    feather=0.0, invert=m_invert.get())
                else:
                    vals = []
                    for var in m_params:
                        v = float(var.get())
                        vals.append(v)
                    params = (int(round(vals[0])), int(round(vals[1])),
                              int(round(vals[2])),
                              max(0.02, vals[3] if len(vals) > 3 else 0.15)) \
                        if key == "color" else tuple(vals[:4])
                    from ..mask import MaskSpec
                    spec = MaskSpec(key, params,
                                    feather=m_feather.get() / 100.0,
                                    invert=m_invert.get())
                m = render_mask(spec, base.width, base.height, img=base)
                overlay = PILImage.new("RGB", base.size, (255, 40, 40))
                out = PILImage.blend(base, overlay, 0.45)
                mask_img = PILImage.fromarray(
                    (m * 255).astype("uint8"), "L").convert("L")
                out = PILImage.composite(out, base, mask_img)
                _photo["img"] = ImageTk.PhotoImage(out, master=preview)
                preview.config(image=_photo["img"], text="", width=360,
                               height=270)
                preview.image = _photo["img"]
            except Exception:
                preview.config(image="", text=self._t("mask_no_preview"))

        FlatButton(editor, text=self._t("mask_refresh"),
                   command=_refresh_preview,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).grid(
            row=15, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def _refresh_list():
            lst.delete(0, tk.END)
            for name, kind, params, feather, invert in specs:
                lst.insert(tk.END, f"{name}  [{kind}]")

        def _read_form():
            key = _type_key()
            if key == "brush":
                if not m_brush_points:
                    raise ValueError("no brush dots")
                return (m_name.get().strip() or f"mask{len(specs) + 1}",
                        "brush", list(m_brush_points), 0.0, m_invert.get(),
                        {})
            vals = [float(v.get()) for v in m_params]
            if key == "color":
                params = (int(round(max(0, min(255, vals[0])))),
                          int(round(max(0, min(255, vals[1])))),
                          int(round(max(0, min(255, vals[2])))),
                          max(0.02, min(1.0, vals[3] if vals[3] else 0.15)))
            elif key == "radial":
                params = (max(0.0, min(1.0, vals[0])),
                          max(0.0, min(1.0, vals[1])),
                          max(0.01, vals[2]), max(0.01, vals[3]))
            else:
                params = tuple(max(0.0, min(1.0, v)) for v in vals[:4])
            adjust = {}
            for k, var in adj_vars.items():
                v = round(var.get(), 3)
                if v != 0.0:
                    adjust[k] = v
            return (m_name.get().strip() or f"mask{len(specs) + 1}", key,
                    list(params), m_feather.get() / 100.0, m_invert.get(),
                    adjust)

        def _load_selected(_evt=None):
            sel = lst.curselection()
            if not sel:
                return
            name, kind, params, feather, invert = specs[sel[0]]
            _current["name"] = name
            m_name.set(name)
            m_type.set(self._t({"linear": "mask_linear",
                                "radial": "mask_radial",
                                "color": "mask_color",
                                "brush": "mask_brush"}[kind]))
            _sync_type()
            if kind == "brush":
                m_brush_points[:] = [tuple(p) for p in params]
                _brush_canvas.delete("all")
                for x, y, r in m_brush_points:
                    cw = max(1, _brush_canvas.winfo_width())
                    ch = max(1, _brush_canvas.winfo_height())
                    rad = max(2, r * min(cw, ch))
                    _brush_canvas.create_oval(x * cw - rad, y * ch - rad,
                                              x * cw + rad, y * ch + rad,
                                              fill="#ff4444", outline="")
                return
            for i in range(4):
                m_params[i].set(str(params[i]) if i < len(params) else "0")
            m_feather.set(feather * 100)
            m_invert.set(invert)
            for k, var in adj_vars.items():
                var.set(adjusts.get(name, {}).get(k, 0.0))

        def _add():
            try:
                name, key, params, feather, invert, adjust = _read_form()
            except ValueError:
                return
            specs.append((name, key, params, feather, invert))
            if adjust:
                adjusts[name] = adjust
            _refresh_list()
            lst.selection_clear(0, tk.END)
            lst.selection_set(tk.END)

        def _update():
            sel = lst.curselection()
            if not sel:
                return
            try:
                old = specs[sel[0]][0]
                name, key, params, feather, invert, adjust = _read_form()
            except ValueError:
                return
            specs[sel[0]] = (name, key, params, feather, invert)
            if old in adjusts:
                del adjusts[old]
            if adjust:
                adjusts[name] = adjust
            _refresh_list()
            lst.selection_set(sel[0])

        def _delete():
            sel = lst.curselection()
            if sel:
                name = specs[sel[0]][0]
                del specs[sel[0]]
                adjusts.pop(name, None)
                _refresh_list()

        lst.bind("<<ListboxSelect>>", _load_selected)
        _refresh_list()

        btns = tk.Frame(body, bg=COLORS["bg"])
        btns.grid(row=0, column=2, sticky="nw", padx=(12, 0))
        for text, cmd in ((self._t("pc_add"), _add),
                          (self._t("pc_update"), _update),
                          (self._t("pc_delete"), _delete)):
            FlatButton(btns, text=text, command=cmd,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=8, pady=2, border_color=COLORS["border"]).pack(
                fill="x", pady=(0, 6))

        bottom = tk.Frame(win, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        FlatButton(bottom, text=self._t("ok"),
                   command=lambda: self._masks_ok(win, specs, adjusts),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right")
        FlatButton(bottom, text=self._t("cancel"), command=win.destroy,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=10, pady=3, border_color=COLORS["border"]).pack(
            side="right", padx=(0, 8))

    def _masks_ok(self, win, specs, adjusts):
        def _n(v):
            if isinstance(v, str):
                # v1.8 字符串型局部调整（curves={...}/hsl={...}/...）——
                # 原样透传；float() 会崩（弹窗 OK 路径的潜伏 bug，旧测试
                # 只开窗不点 OK 从未触发）
                return v
            v = round(float(v), 4)
            return str(int(v)) if v == int(v) else str(v)
        mask_segs = [_mask_spec_string(*s) for s in specs]
        adj_segs = []
        for name, adjust in adjusts.items():
            if name not in {s[0] for s in specs}:
                continue
            adj_segs.append(name + ":" + ",".join(
                f"{k}={_n(v)}" for k, v in adjust.items()))
        self.masks.set(";".join(mask_segs))
        self.mask_adjust.set(";".join(adj_segs))
        self._refresh_grade_value_labels()
        if win is not None:
            win.destroy()

    # ── LR-style canvas mask workflow (v1.8.0) ───────────────────────────

    def _dev_enter_mask_mode(self):
        """v2.5: 蒙版画布从弹窗升格进 Develop（原 _open_mask_workflow）。

        切到 Develop 并把预览/侧栏换成内嵌蒙版编辑器（作用域 = 勾选照片，
        起始 = 当前 Develop 选择）。编辑即所见：退出时写入 _photo_masks，
        预览/导出队列立即反映（与调色覆盖层同一通道）。
        """
        if self._active_module != "develop":
            self._show_module("develop")
        files = self._checked_files()
        if not files:
            messagebox.showwarning(self._t("dlg_no_files_title"),
                                   self._t("mask_no_check"))
            return
        self._dev_mask_mode = True
        # 每次进入重建：_photo_masks/全局 masks 是事实源，撤销栈按会话计
        if self._dev_mask_frame is not None:
            self._dev_mask_frame.destroy()
        self._dev_mask_frame = tk.Frame(self._dev_mask_host,
                                        bg=COLORS["bg"])
        start = self._dev_selected if self._dev_selected in files else None
        self._dev_mask_ctl = self._build_mask_workflow(
            self._dev_mask_frame, files, hosted=True, start_path=start)
        self._dev_mask_frame.pack(fill="both", expand=True)
        self._dev_mask_apply_layout()

    def _dev_exit_mask_mode(self):
        """退出蒙版模式；内嵌编辑视为即改即生效（退出时应用一次）。"""
        ctl = self._dev_mask_ctl
        self._dev_mask_mode = False
        self._dev_mask_ctl = None
        if ctl is not None:
            ctl["apply"]()
        if self._dev_mask_frame is not None:
            self._dev_mask_frame.destroy()
            self._dev_mask_frame = None
        self._dev_mask_apply_layout()
        # 蒙版变化 → 预览与导出队列即时反映（sig 变化由 dev tick 稳定后
        # 触发；这里直接以当前 sig 立即重渲一次，不等防抖）
        if self._dev_selected:
            self._dev_render(self._dev_current_sig())
        self._refresh_export_queue()

    def _dev_mask_apply_layout(self):
        """Develop 主行 pack 切换：调色（预览+侧栏）↔ 蒙版编辑器。"""
        mask_on = getattr(self, "_dev_mask_mode", False)
        if mask_on:
            self._dev_viewer_card.pack_forget()
            self._dev_side.pack_forget()
            self._dev_mask_host.pack(fill="both", expand=True)
        else:
            self._dev_mask_host.pack_forget()
            self._dev_side.pack(side="right", fill="y", padx=(10, 0))
            self._dev_side.pack_propagate(False)
            self._dev_viewer_card.pack(side="left", fill="both",
                                       expand=True)

    def _dev_mask_key_page(self, event):
        """←/→ 在蒙版模式翻页（输入框聚焦/非激活时不拦截）。"""
        if not getattr(self, "_dev_mask_mode", False) \
                or not self._dev_mask_ctl:
            return
        if self._focus_in_text_widget():
            return
        if event.keysym == "Left":
            self._dev_mask_ctl["page_prev"]()
        else:
            self._dev_mask_ctl["page_next"]()
        return "break"

    def _focus_in_text_widget(self) -> bool:
        """键盘焦点在文本输入类控件里（快捷键不得吞掉正常输入）。
        v2.5 从 _lib_keys_active 提取共享（蒙版/灯箱键守卫复用）。"""
        w = None
        try:
            w = self.root.focus_get()
        except tk.TclError:
            pass
        return isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox,
                              tk.Listbox, tk.Spinbox, ttk.Spinbox))

    def _lib_lightbox_key(self, event):
        """←/→ 在灯箱模式翻页（输入框聚焦/非激活时不拦截）。"""
        if not getattr(self, "_lib_lightbox_active", False) \
                or not self._lib_lightbox_ctl:
            return
        if self._focus_in_text_widget():
            return
        self._lib_lightbox_ctl["go"](-1 if event.keysym == "Left" else 1)
        return "break"

    def _build_mask_workflow(self, host, files, *, hosted=False,
                             start_path=None):
        """Canvas mask editor, Lightroom-style — 弹窗/Develop 共用构建器。

        Big image + per-photo masks (each photo keeps its own spec list),
        brush/linear/radial/color/AI tools painted on the canvas with a
        translucent colored overlay, prev/next paging across checked files,
        multiple masks stacked with per-mask visibility and color.

        Per-photo state lives in ``self._photo_masks`` (path -> dict of
        masks/mask_adjust strings) and is injected into batch processing
        via the engine's ``per_file_options`` hook.

        ``hosted=True`` 时由 Develop 内嵌（host = 模块内 frame，退出经
        ``self._dev_exit_mask_mode`` 应用）；否则 host 为独立 Toplevel
        （测试/诊断路径）。返回控制器 ``{apply, undo, page_prev,
        page_next}``。
        """
        from ..mask import (MaskError, MaskSpec, parse_masks,
                           parse_mask_adjust, render_mask)

        # ── per-photo state ──────────────────────────────────────────────
        # photo[path] = {"specs": [(name, kind, params, feather, invert)],
        #                "adjusts": {name: {key: val}},
        #                "visible": {name: bool}}
        photo = {}
        bad_masks_warned = [False]
        for f in files:
            pm = (self._photo_masks or {}).get(f)
            masks_s = (pm or {}).get("masks", self.masks.get())
            adj_s = (pm or {}).get("mask_adjust", self.mask_adjust.get())
            try:
                specs = [(s.name, s.kind, list(s.params), s.feather,
                          s.invert) for s in parse_masks(masks_s)]
            except MaskError:
                # 逐段容错：坏段丢弃、合法段保留（整串清空会让 9 个
                # 合法段 + 1 个笔误段全部消失，OK 后整批蒙版被覆盖）
                specs = []
                for seg in masks_s.split(";"):
                    seg = seg.strip()
                    if not seg:
                        continue
                    try:
                        specs.extend(
                            (s.name, s.kind, list(s.params), s.feather,
                             s.invert) for s in parse_masks(seg))
                    except MaskError:
                        continue
                if not bad_masks_warned[0]:
                    bad_masks_warned[0] = True
                    messagebox.showwarning(
                        self._t("mask_tool"),
                        self._t("mask_bad_segment_warn"))
            try:
                adjusts = {k: dict(v) for k, v in
                           parse_mask_adjust(adj_s).items()}
            except MaskError:
                adjusts = {}
            photo[f] = {"specs": specs, "adjusts": adjusts,
                        "visible": {s[0]: True for s in specs}}
        idx = [0]
        if start_path and start_path in files:
            idx[0] = files.index(start_path)
        current = {"name": None}  # selected mask name
        # undo history: deep snapshots of the CURRENT photo's state, pushed
        # before every mutating action; Ctrl+Z / undo button pops one.
        undo_stack = []
        _MAX_UNDO = 50
        tool = tk.StringVar(value="brush")
        brush_r = tk.DoubleVar(value=0.06)
        feather_v = tk.DoubleVar(value=0.0)  # feather as fraction 0..1
        ai_label = tk.StringVar(value="car")
        color_vals = [tk.StringVar(value=v) for v in ("255", "60", "60")]
        _img_photo = {"tk": None, "pil": None, "scale": 1.0,
                      "ox": 0, "oy": 0}  # canvas->image mapping
        _MASK_COLORS = [(255, 70, 70), (70, 140, 255), (70, 220, 110),
                        (250, 200, 60), (220, 90, 240), (90, 230, 230)]

        top = tk.Frame(host, bg=COLORS["bg"])
        top.pack(fill="x", padx=12, pady=(10, 4))
        page_lbl = tk.Label(top, text="", font=FONT_SMALL,
                            fg=COLORS["text_secondary"], bg=COLORS["bg"])
        page_lbl.pack(side="left")
        tk.Label(top, text="", font=FONT_SMALL, bg=COLORS["bg"],
                 fg=COLORS["text_secondary"]).pack(side="left", padx=8)

        body = tk.Frame(host, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=12)

        # left: mask list
        lst_frame = tk.Frame(body, bg=COLORS["bg"])
        lst_frame.pack(side="left", fill="y", padx=(0, 12))
        tk.Label(lst_frame, text=self._t("mask_list"),
                 font=FONT_SECTION, fg=COLORS["text"],
                 bg=COLORS["bg"]).pack(anchor="w")
        lst = tk.Listbox(lst_frame, width=26, height=14, font=FONT_SMALL,
                         exportselection=False, bg=COLORS["card"],
                         fg=COLORS["text"], relief="flat",
                         highlightthickness=1,
                         highlightbackground=COLORS["border"],
                         selectmode=tk.SINGLE)
        lst.pack(fill="both", expand=True)
        order_bar = tk.Frame(lst_frame, bg=COLORS["bg"])
        order_bar.pack(fill="x", pady=(4, 0))
        for key, label in (("up", "mask_up"), ("down", "mask_down")):
            FlatButton(order_bar, text=self._t(label),
                       command=lambda k=key: _move_layer(k),
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=6, pady=1, border_color=COLORS["border"]).pack(
                side="left", padx=(0, 6))
        vis_vars = {}  # name -> tk.BooleanVar

        def _specs():
            return photo[files[idx[0]]]["specs"]

        def _adjusts():
            return photo[files[idx[0]]]["adjusts"]

        def _visible():
            return photo[files[idx[0]]]["visible"]

        def _snapshot():
            """Deep copy of the current photo's mask state (for undo)."""
            return {"specs": [(s[0], s[1], list(s[2]), s[3], s[4])
                              for s in _specs()],
                    "adjusts": {k: dict(v) for k, v in
                                _adjusts().items()},
                    "visible": dict(_visible())}

        def _push_undo():
            undo_stack.append((files[idx[0]], _snapshot()))
            del undo_stack[:-_MAX_UNDO]

        def _undo():
            if not undo_stack:
                return
            path, snap = undo_stack.pop()
            if path == "__all__":  # apply-to-all 快照：还原全部照片
                for f, s in snap.items():
                    photo[f] = s
                current["name"] = None
                ai_cache.clear()
                _load_adjusts()
                _refresh_list()
                _draw_image()
                return
            if path != files[idx[0]]:
                # undo belongs to another photo: jump there, then restore
                idx[0] = files.index(path)
            photo[path] = snap
            current["name"] = None
            mode["add"] = None  # A/B 模式是临时的，跨照片/撤销不残留
            ai_cache.clear()
            _load_adjusts()
            _refresh_list()
            _draw_image()
            page_lbl.config(text=self._t("mask_page",
                                         cur=idx[0] + 1, total=len(files)))

        def _refresh_list(select=None):
            lst.delete(0, tk.END)
            for name, kind, params, feather, invert in _specs():
                lst.insert(tk.END, f"{name}  [{kind}]"
                                   f"  {'✓' if _visible().get(name) else ''}")
            if select is not None:
                for i, s in enumerate(_specs()):
                    if s[0] == select:
                        lst.selection_clear(0, tk.END)
                        lst.selection_set(i)
                        break

        def _move_layer(direction):
            """Reorder the current mask in the layer stack (list order =
            paint order: later entries paint on top)."""
            sel = lst.curselection()
            if not sel:
                return
            idx = sel[0]
            specs = _specs()
            if idx >= len(specs):
                return
            if direction == "up" and idx > 0:
                _push_undo()
                specs[idx], specs[idx - 1] = specs[idx - 1], specs[idx]
                idx -= 1
            elif direction == "down" and idx < len(specs) - 1:
                _push_undo()
                specs[idx], specs[idx + 1] = specs[idx + 1], specs[idx]
                idx += 1
            else:
                return
            name = specs[idx][0]
            current["name"] = name
            _refresh_list(select=name)
            _draw_image()

        # center: big canvas
        canvas = tk.Canvas(body, width=700, height=460, bg=COLORS["card"],
                           highlightthickness=1,
                           highlightbackground=COLORS["border"],
                           cursor="crosshair")
        canvas.pack(side="left", fill="both", expand=True)
        # AI mask cache: (path, mask_name) -> float32 hxw mask; AI inference
        # is slow, so overlay redraws reuse it instead of re-segmenting.
        ai_cache = {}
        ai_skip_warned = [False]  # AI 叠加层渲染失败只警告一次
        # v2.4 WYSIWYG: per-photo develop-rendered base (tone adjustments,
        # no masks — the canvas blends its own live mask overlays)
        adj_cache = {}

        def _draw_image():
            """Fit the current photo into the canvas, with overlay."""
            if not host.winfo_exists():
                return  # 窗口已关闭（<50ms 内关窗时 after 回调仍会触发）
            canvas.delete("all")
            import numpy as np
            from PIL import Image as PILImage, ImageTk
            path = files[idx[0]]
            base = None
            try:
                # _open_image_safe：RAW 回退引擎加载器（rawpy），
                # 摄影师工作流主力格式在画布上正常显示
                base = adj_cache.get(path) or _open_image_safe(path)
                base = base.convert("RGB")
                base.thumbnail((700, 460), PILImage.LANCZOS)
            except Exception:
                base = PILImage.new("RGB", (700, 460), (40, 40, 40))
            if path not in adj_cache and path in self._photo_adjust:

                def _deliver(result, err, _p=path):
                    if not host.winfo_exists() or files[idx[0]] != _p:
                        return
                    if not result:
                        return  # original stays on failure
                    try:
                        img = _open_image_safe(result.output_path)
                        adj_cache[_p] = img
                        if files[idx[0]] == _p:
                            _draw_image()
                    except Exception:
                        pass

                self._render_adjusted_async(
                    path, _deliver, include_masks=False)
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            if cw < 50 or ch < 50:  # canvas not laid out yet
                cw, ch = 700, 460
            scale = min(cw / base.width, ch / base.height)
            disp = base.resize((max(1, int(base.width * scale)),
                                max(1, int(base.height * scale))),
                               PILImage.LANCZOS)
            ox, oy = (cw - disp.width) // 2, (ch - disp.height) // 2
            _img_photo.update({"pil": base, "scale": scale,
                               "ox": ox, "oy": oy})
            # overlay: blend each visible mask with its color
            specs = _specs()
            # combo 蒙版按名引用其他蒙版——refs + name 缺一不可，
            # 否则 render_mask 报"需要完整蒙版列表"（并误触发 AI 警告）
            refs = {s[0]: MaskSpec(s[1], tuple(s[2]), s[3], s[4], s[0])
                    for s in specs}
            over = np.asarray(disp, dtype=np.float32).copy()
            for i, (name, kind, params, feather, invert) in enumerate(specs):
                if not _visible().get(name, True):
                    continue
                try:
                    if kind in ("subject", "person", "object"):
                        key = (path, name)
                        if key not in ai_cache:
                            ai_cache[key] = render_mask(
                                MaskSpec(kind, tuple(params), feather,
                                         invert, name),
                                base.width, base.height, img=base,
                                refs=refs)
                        m = ai_cache[key]
                    else:
                        m = render_mask(MaskSpec(kind, tuple(params),
                                                 feather, invert, name),
                                        base.width, base.height, img=base,
                                        refs=refs)
                    m = np.asarray(
                        PILImage.fromarray(
                            (m * 255).astype(np.uint8), "L")
                        .resize(disp.size)).astype(np.float32) / 255.0
                    c = _MASK_COLORS[i % len(_MASK_COLORS)]
                    for k in range(3):
                        over[..., k] = over[..., k] * (1 - 0.55 * m) \
                            + c[k] * 0.55 * m
                except MaskError:
                    # AI 蒙版缺 cv2/权重时叠加层静默跳过 → 列表带 ✓ 但画布
                    # 隐形，用户到批量才见失败——一次性点明原因
                    if not ai_skip_warned[0]:
                        ai_skip_warned[0] = True
                        messagebox.showwarning(
                            self._t("mask_tool"),
                            self._t("mask_ai_overlay_warn"))
                    continue
            out = PILImage.fromarray(np.clip(over, 0, 255).astype(np.uint8))
            _img_photo["tk"] = ImageTk.PhotoImage(out, master=canvas)
            canvas.create_image(ox, oy, image=_img_photo["tk"],
                                anchor="nw")

        def _canvas_to_img(evt):
            return ((evt.x - _img_photo["ox"]) / _img_photo["scale"],
                    (evt.y - _img_photo["oy"]) / _img_photo["scale"])

        def _img_to_rel(x, y):
            base = _img_photo["pil"]
            if base is None:
                return 0.0, 0.0
            return (max(0.0, min(1.0, x / base.width)),
                    max(0.0, min(1.0, y / base.height)))

        def _new_spec_name(kind):
            base = kind if kind != "color" else "color"
            existing = {s[0] for s in _specs()}
            i = 1
            while f"{base}{i}" in existing:
                i += 1
            return f"{base}{i}"

        def _set_current_mask(name, spec, adjust=None):
            """Replace or append the named mask in the current photo."""
            _push_undo()
            specs = _specs()
            for i, s in enumerate(specs):
                if s[0] == name:
                    specs[i] = spec
                    break
            else:
                specs.append(spec)
            _visible()[name] = True
            if adjust is not None:
                _adjusts()[name] = adjust
            current["name"] = name
            _refresh_list(select=name)

        def _finish_paint(kind, params, feather=0.0, adjust=None):
            name = current["name"]
            existing = next((s for s in _specs() if s[0] == name), None)
            # 只在与新蒙版同类型时复用名字（A/B 笔画追加）；异类工具
            # 必须新建——否则选中 radial 切 linear 拖一笔会把 radial
            # 静默替换（取色/AI 同理）
            if not existing or existing[1] != kind:
                name = _new_spec_name(kind)
            # brush dots each carry their own radius; feather applies to
            # every kind except color (color has tol, not feather). When
            # appending A/B strokes to an existing mask, keep its feather.
            if kind != "color":
                feather = feather_v.get() if existing is None \
                    else existing[3]
            spec = (name, kind, list(params), feather, False)
            _set_current_mask(name, spec, adjust)

        # ── canvas drag handlers ─────────────────────────────────────────
        drag = {"active": False, "x0": 0, "y0": 0, "x1": 0, "y1": 0,
                "dots": []}
        mode = {"add": None}  # None = paint new; True/False = A add / B
        # subtract strokes onto the current brush mask
        move = {"active": False, "name": None, "dx0": 0, "dy0": 0,
                "orig": None, "moved": False}  # Alt+drag moves an existing mask

        def _mask_at(evt):
            """Return the name of the topmost visible mask under the cursor,
            or None.

            Geometric/brush masks hit-test by distance to their centers/
            axes (a brush stroke is easy to miss through the soft Gaussian
            tail); AI masks hit-test on the rendered mask value (their
            silhouette is the meaningful target).
            """
            base = _img_photo["pil"]
            if base is None:
                return None
            x, y = _canvas_to_img(evt)
            xi = max(0, min(base.width - 1, int(x)))
            yi = max(0, min(base.height - 1, int(y)))
            rx = x / base.width
            ry = y / base.height
            short = float(min(base.width, base.height))
            for name, kind, params, feather, invert in reversed(_specs()):
                if not _visible().get(name, True):
                    continue
                try:
                    if kind in ("subject", "person", "object"):
                        key = (files[idx[0]], name)
                        if key not in ai_cache:
                            ai_cache[key] = render_mask(
                                MaskSpec(kind, tuple(params), feather,
                                         invert),
                                base.width, base.height, img=base)
                        if ai_cache[key][yi, xi] > 0.3:
                            return name
                        continue
                    if kind == "brush":
                        if any((px - rx) ** 2 + (py - ry) ** 2
                               <= (r * 1.5) ** 2
                               for px, py, r in params if r >= 0):
                            return name
                        continue
                    if kind == "linear":
                        x0, y0, x1, y1 = params
                        dx, dy = x1 - x0, y1 - y0
                        l2 = dx * dx + dy * dy
                        if l2 == 0:
                            continue
                        t = ((rx - x0) * dx + (ry - y0) * dy) / l2
                        if 0.0 <= t <= 1.0:
                            px = x0 + t * dx
                            py = y0 + t * dy
                            # 10% of short side, in normalized units
                            tol = 0.1 * short / max(base.width, base.height)
                            if (px - rx) ** 2 + (py - ry) ** 2 <= tol ** 2:
                                return name
                        continue
                    if kind == "radial":
                        cx, cy, rxx, ryy = params
                        d = ((rx - cx) / rxx) ** 2 + ((ry - cy) / ryy) ** 2
                        if d <= 1.0:
                            return name
                        continue
                    # color masks don't move (no spatial center)
                except MaskError:
                    if not ai_skip_warned[0]:
                        ai_skip_warned[0] = True
                        messagebox.showwarning(
                            self._t("mask_tool"),
                            self._t("mask_ai_overlay_warn"))
                    continue
            return None

        def _move_mask(evt):
            """Live-move the mask under Alt+drag: shift params by the
            pointer delta (in relative image coords)."""
            dx_canvas = evt.x - move["dx0"]
            dy_canvas = evt.y - move["dy0"]
            scale = _img_photo["scale"]
            base = _img_photo["pil"]
            if base is None or scale <= 0:
                return
            if abs(dx_canvas) > 1 or abs(dy_canvas) > 1:
                move["moved"] = True  # 无位移的点击不记 undo
            drx = dx_canvas / scale / base.width
            dry = dy_canvas / scale / base.height
            name = move["name"]
            for i, s in enumerate(_specs()):
                if s[0] != name:
                    continue
                kind, params = s[1], move["orig"]
                if kind == "brush":
                    moved = [(max(0.0, min(1.0, x + drx)),
                              max(0.0, min(1.0, y + dry)), r)
                             for x, y, r in params]
                elif kind == "linear":
                    moved = [max(0.0, min(1.0, p + (drx if j % 2 == 0
                                                     else dry)))
                             for j, p in enumerate(params)]
                elif kind == "radial":
                    moved = [max(0.0, min(1.0, params[0] + drx)),
                             max(0.0, min(1.0, params[1] + dry)),
                             params[2], params[3]]
                elif kind == "color":
                    moved = list(params)  # color masks don't move
                else:
                    moved = list(params)
                _specs()[i] = (s[0], kind, moved, s[3], s[4])
                _refresh_list(select=name)
                _draw_image()
                return

        def _on_press(evt):
            # Drag inside an existing mask moves it (LR-style). Exceptions:
            # - A/B mode (user explicitly clicked A or B) with a current
            #   brush mask: pressing paints strokes onto the mask instead
            #   of moving it.
            # - color tool: always picks a color.
            painting = (tool.get() == "brush" and mode["add"] is not None
                        and current["name"] is not None
                        and any(s[0] == current["name"] and s[1] == "brush"
                                for s in _specs()))
            hit = None if painting or tool.get() == "color" \
                else _mask_at(evt)
            if hit:
                for i, s in enumerate(_specs()):
                    if s[0] == hit:
                        # 先存旧蒙版的滑杆编辑再切换，防静默丢失
                        _save_adjusts()
                        move.update({"active": True, "name": hit,
                                     "dx0": evt.x, "dy0": evt.y,
                                     "orig": list(s[2]), "moved": False})
                        current["name"] = hit
                        feather_v.set(s[3] * 100)  # 画布点击同步羽化滑杆
                        _refresh_list(select=hit)
                        _load_adjusts()
                        return
            if tool.get() == "color":
                x, y = _canvas_to_img(evt)
                base = _img_photo["pil"]
                if base is None:
                    return
                px = base.getpixel((max(0, min(base.width - 1, int(x))),
                                    max(0, min(base.height - 1, int(y)))))
                color_vals[0].set(str(px[0]))
                color_vals[1].set(str(px[1]))
                color_vals[2].set(str(px[2]))
                _finish_paint("color",
                              (px[0], px[1], px[2], 0.15))
                _draw_image()
                return
            drag.update({"active": True, "x0": evt.x, "y0": evt.y,
                         "x1": evt.x, "y1": evt.y,
                         "dots": [(evt.x, evt.y)]})

        def _on_drag(evt):
            if move["active"]:
                _move_mask(evt)
                return
            if not drag["active"]:
                return
            drag["x1"], drag["y1"] = evt.x, evt.y
            if tool.get() == "brush":
                drag["dots"].append((evt.x, evt.y))
            canvas.delete("guide")
            if tool.get() == "linear":
                canvas.create_line(drag["x0"], drag["y0"], evt.x, evt.y,
                                   fill="#ffffff", width=1, tags="guide",
                                   dash=(4, 3))
            elif tool.get() == "radial":
                # dashed ellipse: start point is the center, drag extends
                # the radii; bounding box = center ± (dx, dy)
                cx, cy = drag["x0"], drag["y0"]
                rx, ry = abs(evt.x - cx), abs(evt.y - cy)
                canvas.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                                   outline="#ffffff", width=1.5,
                                   tags="guide", dash=(5, 4))
                canvas.create_line(drag["x0"], drag["y0"], evt.x, evt.y,
                                   fill="#ffffff", width=1, tags="guide",
                                   dash=(2, 3))
            elif tool.get() == "brush":
                for px, py in drag["dots"]:
                    r = max(2, brush_r.get() * 30)
                    canvas.create_oval(px - r, py - r, px + r, py + r,
                                       fill="#ff4444", stipple="gray50",
                                       outline="", tags="guide")

        def _on_release(evt):
            if move["active"]:
                # moving is undoable as one step
                move["active"] = False
                if move["moved"]:
                    _push_undo()
                move["moved"] = False
                return
            if not drag["active"]:
                return
            drag["active"] = False
            canvas.delete("guide")
            if tool.get() == "brush":
                dots = []
                for px, py in drag["dots"]:
                    x, y = _canvas_to_img(type("E", (), {"x": px, "y": py})())
                    rx, ry = _img_to_rel(x, y)
                    dots.append((rx, ry, brush_r.get()))
                if not dots:
                    return
                # A/B modes: append to the current brush mask instead of
                # replacing it (subtract dots get a negative radius).
                cur = current["name"]
                cur_spec = None
                for s in _specs():
                    if s[0] == cur and s[1] == "brush":
                        cur_spec = s
                        break
                if cur_spec is not None and mode["add"] is False:
                    neg = [(x, y, -r) for x, y, r in dots]
                    _finish_paint("brush", list(cur_spec[2]) + neg)
                    return
                if cur_spec is not None and mode["add"]:
                    _finish_paint("brush", list(cur_spec[2]) + dots)
                    return
                _finish_paint("brush", dots)
            elif tool.get() == "linear":
                x0, y0 = _img_to_rel(*_canvas_to_img(
                    type("E", (), {"x": drag["x0"], "y": drag["y0"]})()))
                x1, y1 = _img_to_rel(*_canvas_to_img(
                    type("E", (), {"x": drag["x1"], "y": drag["y1"]})()))
                # 点击未拖动 → 零长度渐变：画布上隐形（全 NaN→全 0）、
                # 批量时 parse 必失败——不创建蒙版
                if (x1 - x0) ** 2 + (y1 - y0) ** 2 < 1e-12:
                    return
                _finish_paint("linear", (x0, y0, x1, y1))
            elif tool.get() == "radial":
                cx, cy = _img_to_rel(*_canvas_to_img(
                    type("E", (), {"x": drag["x0"], "y": drag["y0"]})()))
                ex, ey = _img_to_rel(*_canvas_to_img(
                    type("E", (), {"x": drag["x1"], "y": drag["y1"]})()))
                rx = max(0.01, abs(ex - cx))
                ry = max(0.01, abs(ey - cy))
                _finish_paint("radial", (cx, cy, rx, ry))
            _draw_image()

        canvas.bind("<Button-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_drag)
        canvas.bind("<ButtonRelease-1>", _on_release)

        def _ai_mask(kind, label=None):
            """Add an AI mask (subject/person/object) to the current photo."""
            try:
                from ..segmask import segment
                base = _img_photo["pil"]
                if base is None:
                    return
                # CPU 推理耗时数秒：先落 watch 光标 + 状态提示，避免
                # 无反馈冻结（异步化需要 queue 模式，超出本对话框范围）
                host.config(cursor="watch")
                host.update_idletasks()
                try:
                    m = segment(base, kind, label=label)
                finally:
                    host.config(cursor="")
                if m.max() < 0.01:
                    messagebox.showwarning(
                        self._t("mask_ai_empty"), self._t("mask_ai_empty"))
                    return
                # 仅同类型蒙版才复用名字（重跑同一分割）；异类蒙版
                # 被选中时新建，防静默替换
                name = current["name"] if current["name"] and any(
                    s[0] == current["name"] and s[1] == kind
                    for s in _specs()) else None
                if not name:
                    name = _new_spec_name(kind)
                ai_cache.pop((files[idx[0]], name), None)
                _set_current_mask(name, (name, kind, [label] if label
                                         else [],
                                         feather_v.get() / 100.0, False))
                _refresh_list(select=name)
                _draw_image()
            except (ImportError, RuntimeError) as e:
                messagebox.showwarning(self._t("mask_tool"), f"AI: {e}")

        # right: tools + adjustments
        right = tk.Frame(body, bg=COLORS["bg"])
        right.pack(side="right", fill="y", padx=(12, 0))
        tk.Label(right, text=self._t("mask_tool"), font=FONT_SECTION,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w")
        tools = (("brush", "mask_tool_brush"), ("linear", "mask_tool_linear"),
                 ("radial", "mask_tool_radial"), ("color", "mask_tool_color"),
                 ("subject", "mask_tool_subject"),
                 ("person", "mask_tool_person"),
                 ("object", "mask_tool_object"))
        for key, label in tools:
            if key in ("subject", "person", "object"):
                FlatButton(
                    right, text=self._t(label),
                    command=lambda k=key: _ai_mask(
                        k, ai_label.get().strip() if k == "object" else None),
                    bg=COLORS["bg"], fg=COLORS["text"],
                    hover_bg=COLORS["border"], font=FONT_SMALL,
                    padx=8, pady=2, border_color=COLORS["border"]).pack(
                    fill="x", pady=(0, 4))
            else:
                FlatButton(right, text=self._t(label),
                           command=lambda k=key: tool.set(k),
                           bg=COLORS["bg"], fg=COLORS["text"],
                           hover_bg=COLORS["border"], font=FONT_SMALL,
                           padx=8, pady=2,
                           border_color=COLORS["border"]).pack(
                    fill="x", pady=(0, 4))
        tk.Label(right, text=self._t("mask_ai_label"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(anchor="w")
        ttk.Entry(right, textvariable=ai_label, font=FONT_BODY,
                  width=12).pack(fill="x", pady=(0, 6))
        mode_frame = tk.Frame(right, bg=COLORS["bg"])
        mode_frame.pack(fill="x", pady=(0, 6))
        for key, label in (("add", "mask_mode_add"),
                           ("subtract", "mask_mode_subtract")):
            FlatButton(mode_frame, text=self._t(label),
                       command=lambda k=key: mode.__setitem__(
                           "add", k == "add"),
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=8, pady=2, border_color=COLORS["border"]).pack(
                side="left", padx=(0, 6))
        FlatButton(mode_frame, text=self._t("mask_mode_off"),
                   command=lambda: mode.__setitem__("add", None),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            side="left")
        tk.Label(right, text=self._t("mask_brush_size"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(anchor="w")
        ttk.Scale(right, from_=0.01, to=0.3, variable=brush_r).pack(
            fill="x", pady=(0, 2))
        tk.Label(right, text=self._t("mask_feather"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(anchor="w")
        ttk.Scale(right, from_=0, to=100, variable=feather_v,
                  command=lambda _v: _apply_feather()).pack(
            fill="x", pady=(0, 8))
        FlatButton(right, text=self._t("mask_add"), command=lambda:
                   current.__setitem__("name", None),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            fill="x", pady=(0, 4))
        FlatButton(right, text=self._t("mask_del"),
                   command=lambda: _delete_current(),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            fill="x", pady=(0, 4))

        def _delete_current():
            name = current["name"]
            if not name:
                return
            _push_undo()
            _specs()[:] = [s for s in _specs() if s[0] != name]
            _adjusts().pop(name, None)
            _visible().pop(name, None)
            current["name"] = None
            ai_cache.clear()  # 该蒙版的分割缓存随删除失效
            _refresh_list()
            _draw_image()

        def _toggle_visible(name):
            if not name:
                return
            _push_undo()
            _visible()[name] = not _visible().get(name, True)
            _refresh_list(select=name)
            _draw_image()

        lst.bind("<<ListboxSelect>>", lambda e: _select_from_list())
        lst.bind("<space>", lambda e: _toggle_visible(
            current["name"]) if current["name"] else None)

        def _select_from_list():
            sel = lst.curselection()
            if sel and sel[0] < len(_specs()):
                current["name"] = _specs()[sel[0]][0]
                feather_v.set(_specs()[sel[0]][3] * 100)

        feather_undo = {"name": None, "val": None}  # 拖动过程只 push 一次

        def _apply_feather(*_):
            """Live-update the selected mask's feather from the slider."""
            name = current["name"]
            if not name:
                return
            for i, s in enumerate(_specs()):
                if s[0] == name and s[1] != "color":
                    v = feather_v.get() / 100.0
                    if abs(s[3] - v) > 1e-6:
                        # 逐 tick push 会把 50 cap 的旧快照全挤出栈；
                        # 同一次拖动（值单调变化）只记录一个 undo
                        if feather_undo["name"] != name or \
                                abs(feather_undo["val"] - s[3]) > 1e-9:
                            _push_undo()
                            feather_undo.update(name=name, val=v)
                        _specs()[i] = (s[0], s[1], list(s[2]), v, s[4])
                        _refresh_list(select=name)
                        _draw_image()
                        return

        # ── adjustments for the current mask ─────────────────────────────
        adj_vars = {key: tk.DoubleVar(value=0.0) for key in (
            "exposure", "brightness", "contrast", "saturation", "vibrance",
            "clarity", "texture", "sharpen", "temp", "tint", "blur")}
        tk.Label(right, text=self._t("mask_adjust_sec"), font=FONT_SECTION,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w",
                                                          pady=(10, 0))
        _ADJ_META = (
            ("exposure", -3, 3), ("brightness", -1, 1), ("contrast", -1, 1),
            ("saturation", -1, 1), ("vibrance", -1, 1), ("clarity", -1, 1),
            ("texture", -1, 1), ("sharpen", -1, 1), ("temp", 0, 12000),
            ("tint", -100, 100), ("blur", 0, 50),
        )
        for i, (key, lo, hi) in enumerate(_ADJ_META):
            tk.Label(right, text=self._t("adj_" + key), font=FONT_TINY,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
                anchor="w")
            ttk.Scale(right, from_=lo, to=hi, variable=adj_vars[key]).pack(
                fill="x")

        def _save_adjusts():
            name = current["name"]
            if not name:
                return
            # 从既有 dict 合并而非整体替换：滑杆只覆盖标量键，
            # 字符串键（curves/hsl/... 来自 CLI/预设）不被静默丢掉
            adjust = _adjusts().get(name, {})
            for k, var in adj_vars.items():
                v = round(var.get(), 3)
                if v != 0.0:
                    adjust[k] = v
                else:
                    adjust.pop(k, None)
            _adjusts()[name] = adjust

        def _load_adjusts():
            name = current["name"]
            for k, var in adj_vars.items():
                var.set(0.0)
            if not name:
                return
            for k, v in _adjusts().get(name, {}).items():
                if k in adj_vars:
                    adj_vars[k].set(v)

        lst.bind("<<ListboxSelect>>",
                 lambda e: (_save_adjusts(), _select_from_list(),
                            _load_adjusts()))

        # ── paging ───────────────────────────────────────────────────────
        def _page(delta):
            _save_adjusts()
            idx[0] = (idx[0] + delta) % len(files)
            current["name"] = None
            mode["add"] = None  # A/B 模式不跨照片残留
            ai_cache.clear()    # 缓存按 (path, name) 键，翻页后旧图不清理会无界增长
            _load_adjusts()
            _refresh_list()
            _draw_image()
            page_lbl.config(text=self._t("mask_page",
                                         cur=idx[0] + 1, total=len(files)))

        def _page_prev():
            _page(-1)

        def _page_next():
            _page(1)

        host.bind("<Left>", lambda e: _page_prev())
        host.bind("<Right>", lambda e: _page_next())
        host.bind("<Command-z>", lambda e: _undo())
        host.bind("<Control-z>", lambda e: _undo())

        bottom = tk.Frame(host, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=12, pady=(6, 12))
        FlatButton(bottom, text=self._t("mask_prev"), command=_page_prev,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            side="left")
        FlatButton(bottom, text=self._t("mask_next"), command=_page_next,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            side="left", padx=(6, 0))
        FlatButton(bottom, text=self._t("mask_apply_all"),
                   command=lambda: _apply_all(),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            side="left", padx=(24, 0))
        FlatButton(bottom, text=self._t("mask_undo"), command=_undo,
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL,
                   padx=8, pady=2, border_color=COLORS["border"]).pack(
            side="left", padx=(8, 0))

        def _apply_all():
            """Copy current photo's masks to every checked photo (deep)."""
            # 全量快照：Ctrl+Z 还原所有照片，而不是只剩当前一张
            undo_stack.append(("__all__", {
                f: {"specs": [(s[0], s[1], list(s[2]), s[3], s[4])
                              for s in photo[f]["specs"]],
                    "adjusts": {k: dict(v) for k, v in
                                photo[f]["adjusts"].items()},
                    "visible": dict(photo[f]["visible"])}
                for f in files}))
            del undo_stack[:-_MAX_UNDO]
            src = photo[files[idx[0]]]
            for f in files:
                photo[f] = {
                    "specs": [(s[0], s[1], list(s[2]), s[3], s[4])
                              for s in src["specs"]],
                    "adjusts": {k: dict(v) for k, v in
                                src["adjusts"].items()},
                    "visible": dict(src["visible"])}
            self.masks.set(_serialize_masks(photo[f]["specs"],
                                            photo[f]["adjusts"]))

        def _n(v):
            if isinstance(v, str):
                return v  # v1.8 字符串型局部调整（curves={...} 等）透传
            v = round(float(v), 4)
            return str(int(v)) if v == int(v) else str(v)

        def _serialize_masks(specs, adjusts):
            """Per-photo state -> (masks_str, mask_adjust_str)."""
            mask_segs = [_mask_spec_string(*s) for s in specs]
            adj_segs = []
            for name, adjust in adjusts.items():
                if not adjust or name not in {s[0] for s in specs}:
                    continue
                adj_segs.append(name + ":" + ",".join(
                    f"{k}={_n(v)}" for k, v in adjust.items()))
            return ";".join(mask_segs), ";".join(adj_segs)

        def _apply_state():
            """序列化全部照片的蒙版状态到 _photo_masks（弹窗 OK 与
            Develop 退出共用——纯写入，不触碰 UI 生命周期）。"""
            _save_adjusts()
            if self._photo_masks is None:
                self._photo_masks = {}
            for f in files:
                masks_s, adj_s = _serialize_masks(
                    photo[f]["specs"], photo[f]["adjusts"])
                if masks_s == self.masks.get().strip() and \
                        adj_s == self.mask_adjust.get().strip():
                    self._photo_masks.pop(f, None)  # same as global
                else:
                    self._photo_masks[f] = {"masks": masks_s,
                                            "mask_adjust": adj_s}

        if hosted:
            # 内嵌：完成 = 应用 + 退回调色视图（退出路径单一，防重入）
            FlatButton(bottom, text=self._t("mask_done"),
                       command=self._dev_exit_mask_mode,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=10, pady=3, border_color=COLORS["border"]).pack(
                side="right")
        else:
            def _ok_close():
                _apply_state()
                host.destroy()

            FlatButton(bottom, text=self._t("ok"), command=_ok_close,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=10, pady=3, border_color=COLORS["border"]).pack(
                side="right")
            FlatButton(bottom, text=self._t("cancel"),
                       command=host.destroy,
                       bg=COLORS["bg"], fg=COLORS["text"],
                       hover_bg=COLORS["border"], font=FONT_SMALL,
                       padx=10, pady=3, border_color=COLORS["border"]).pack(
                side="right", padx=(0, 8))

        tk.Label(host, text=self._t("mask_overlay_hint"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            fill="x", padx=12, pady=(0, 2))
        tk.Label(host, text=self._t("mask_drag_hint"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            fill="x", padx=12, pady=(0, 8))

        _refresh_list()
        _draw_image()
        page_lbl.config(text=self._t("mask_page", cur=idx[0] + 1,
                                     total=len(files)))
        host.after(50, _draw_image)  # canvas size settled after layout

        return {"apply": _apply_state, "undo": _undo,
                "page_prev": _page_prev, "page_next": _page_next}

    def _browse_gpx(self):
        """Pick a GPX track file."""
        if self._dlg_cooldown_active():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self._t("gpx_trace"),
            filetypes=[("GPX", "*.gpx"), ("All Files", "*.*")])
        self._after_file_dialog()
        if path:
            self.gpx_trace.set(path)

    def _browse_watermark_image(self):
        """Pick a watermark overlay image via file dialog."""
        if self._dlg_cooldown_active():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self._t("wm_image"),
            filetypes=[("图片 Images", "*.png *.jpg *.jpeg *.webp"),
                       ("All files", "*.*")])
        self._after_file_dialog()
        if path:
            self.watermark_image.set(path)

    def _build_bottom_panel(self, parent):
        """Build bottom progress and action bar."""
        # Card container
        card = tk.Frame(parent, bg=COLORS["card"], bd=0, highlightthickness=0)
        card.pack(fill="x")

        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=18, pady=14)

        # Progress label
        self.progress_label = tk.Label(
            inner, text=self._t("ready"),
            font=FONT_BODY, fg=COLORS["text_secondary"], bg=COLORS["card"],
            anchor="w",
        )
        self.progress_label.pack(fill="x")

        # Progress bar
        self.progress_bar = ttk.Progressbar(
            inner, mode="determinate", length=400,
        )
        self.progress_bar.pack(fill="x", pady=(8, 10))

        # Stats row
        stats_frame = tk.Frame(inner, bg=COLORS["card"])
        stats_frame.pack(fill="x")

        self.stats_label = tk.Label(
            stats_frame, text="",
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["card"],
        )
        self.stats_label.pack(side="left")

        # Action buttons
        btn_frame = tk.Frame(inner, bg=COLORS["card"])
        btn_frame.pack(fill="x", pady=(10, 0))

        self.preview_btn = FlatButton(
            btn_frame, text=self._t("preview"), command=self._preview,
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            font=FONT_BUTTON, padx=20, pady=8, border_color=COLORS["border"],
        )
        self.preview_btn.pack(side="left")

        self.start_btn = FlatButton(
            btn_frame, text=self._t("start"), command=self._start_processing,
            bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
            font=FONT_BUTTON_LG, padx=24, pady=8,
        )
        self.start_btn.pack(side="right")

        # Cancel button (hidden by default)
        self.cancel_btn = FlatButton(
            btn_frame, text=self._t("cancel"), command=self._cancel_processing,
            bg=COLORS["danger"], hover_bg=COLORS["danger_hover"],
            font=FONT_BUTTON_LG, padx=24, pady=8,
        )
        # Start hidden

    # ── UI Helper Methods ────────────────────────────────────────────────────

    def _add_collapsible_section(self, parent, title_key, default_open=True):
        """A clickable section header that toggles its content frame open/
        closed (Lightroom-style). Returns the content frame — the caller
        grids/packs its widgets into it.

        Sections are packed into the tab's inner frame, so collapsing one
        (pack_forget) shifts the rest up with no row bookkeeping.
        """
        title = self._t(title_key)
        content = tk.Frame(parent, bg=COLORS["card"])
        state = {"open": bool(default_open)}

        def _toggle():
            state["open"] = not state["open"]
            if state["open"]:
                # after= keeps the section in place: a bare pack() would
                # re-append to the queue end, jumping the section to the
                # bottom of the settings list (it appears to vanish).
                content.pack(fill="x", padx=18, pady=(2, 4), after=header)
                header.configure(text="▾ " + title)
            else:
                content.pack_forget()
                header.configure(text="▸ " + title)

        header = FlatButton(
            parent, text=("▾ " if default_open else "▸ ") + title,
            command=_toggle,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            font=FONT_SECTION, padx=10, pady=3, border_color=COLORS["divider"],
        )
        header.pack(fill="x", pady=(8, 0))
        if default_open:
            content.pack(fill="x", padx=18, pady=(2, 4))
        return content

    def _on_cutout_mode_changed(self, *_):
        """Show the params frame matching the cutout mode (object label vs
        color RGB/tol/feather); grid_remove keeps the grid options for the
        re-show. The mode var holds a localized label, so compare via _t."""
        frames = getattr(self, "_cutout_frames", None)
        if not frames:
            return
        mode = self.cutout_mode.get()
        visible = None
        if mode == self._t("cutout_object"):
            visible = frames["object"]
        elif mode == self._t("cutout_color"):
            visible = frames["color"]
        for frame in frames.values():
            if frame is visible:
                frame.grid()
            else:
                frame.grid_remove()

    def _add_checkbox(self, parent, text, variable, row):
        """Add a native-styled checkbox."""
        cb = ttk.Checkbutton(parent, text=text, variable=variable)
        cb.grid(row=row, sticky="w", pady=2)

    def _on_quality_change(self, value):
        """Update quality label when slider moves."""
        self.quality_label.config(text=str(int(float(value))))

    def _on_mode_change(self):
        """Toggle between manual quality mode and target size mode."""
        if self.target_size_mode.get():
            # Target size mode: quality becomes ceiling
            self.quality_section_label.config(text=self._t("max_quality"))
            self.target_section_frame.pack()
            # Default to 95 as ceiling in target mode
            if self.quality.get() == 85:
                self.quality.set(95)
                self.quality_label.config(text="95")
        else:
            # Manual quality mode
            self.quality_section_label.config(text=self._t("quality"))
            self.target_section_frame.pack_forget()

    # ── About ───────────────────────────────────────────────────────────────

    def _show_about(self):
        """Show the About dialog with version, features and environment info."""
        import platform
        import importlib.util
        import PIL

        win = tk.Toplevel(self.root)
        win.title(self._t("about_title"))
        win.resizable(False, False)
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        inner = tk.Frame(win, bg=COLORS["bg"])
        inner.pack(padx=28, pady=24)

        tk.Label(inner, text=APP_NAME, font=FONT_TITLE,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w")
        tk.Label(inner, text=f"v{APP_VERSION}  ·  {self._t('about_desc')}",
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["bg"]).pack(anchor="w", pady=(2, 14))

        def section(title):
            tk.Label(inner, text=title, font=FONT_SECTION,
                     fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w", pady=(10, 2))

        section(self._t("about_features"))
        tk.Label(inner, text=self._t("about_feature_list"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"], justify="left",
                 bg=COLORS["bg"]).pack(anchor="w")

        section(self._t("about_env"))
        tk.Label(inner, text=f"Python {platform.python_version()}  ·  Pillow {PIL.__version__}",
                 font=FONT_SMALL, fg=COLORS["text_secondary"],
                 bg=COLORS["bg"]).pack(anchor="w")

        section(self._t("about_deps"))
        deps = [
            ("tkinterdnd2", DND_AVAILABLE, "GUI drag & drop"),
            ("rawpy", importlib.util.find_spec("rawpy") is not None, "RAW"),
            ("pillow-heif", importlib.util.find_spec("pillow_heif") is not None, "HEIC"),
            ("pillow-avif-plugin", importlib.util.find_spec("pillow_avif") is not None, "AVIF"),
            ("piexif", importlib.util.find_spec("piexif") is not None, "EXIF"),
            ("watchdog", importlib.util.find_spec("watchdog") is not None, "watch"),
        ]
        for name, installed, purpose in deps:
            row = tk.Frame(inner, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x")
            status = self._t("dep_installed" if installed else "dep_missing")
            color = COLORS["success"] if installed else COLORS["text_secondary"]
            tk.Label(row, text=f"{name} ({purpose})", font=FONT_SMALL,
                     fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
            tk.Label(row, text=status, font=FONT_SMALL,
                     fg=color, bg=COLORS["bg"]).pack(side="left", padx=(8, 0))

        section(self._t("about_shortcuts"))
        tk.Label(inner, text=self._t("shortcuts_text"),
                 font=FONT_SMALL, fg=COLORS["text_secondary"], justify="left",
                 bg=COLORS["bg"]).pack(anchor="w")

        tk.Label(inner, text=self._t("about_license"),
                 font=FONT_TINY, fg=COLORS["text_secondary"],
                 bg=COLORS["bg"]).pack(anchor="w", pady=(16, 12))

        FlatButton(inner, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=24, pady=6).pack()

    # ── Plugin Manager ──────────────────────────────────────────────────────

    def _show_plugin_manager(self):
        """Dialog to manage official plugins: list installed + available,
        install / uninstall / pre-fetch weights. Uses the same logic as the
        `photo-s plugin` CLI (photo_s.plugincmd)."""
        from ..registry import OFFICIAL_PLUGINS, to_dict
        from ..plugincmd import _pip_run, _installed_version
        from ..plugin import clear_cache, discover_plugins

        win = tk.Toplevel(self.root)
        win.title(self._t("plugins_title"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.geometry("560x520")

        status_lbl = tk.Label(win, text="", font=FONT_SMALL,
                              fg=COLORS["text_secondary"], bg=COLORS["bg"],
                              wraplength=520, justify="left")
        status_lbl.pack(fill="x", padx=18, pady=(12, 4))

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        # Worker threads must never touch Tk directly: they put UI
        # callbacks on a queue and a main-thread after-loop drains it.
        bus = UiBus(win)
        schedule = bus.schedule
        bus.start()

        def _section_title(text):
            tk.Label(body, text=text, font=FONT_SECTION,
                     fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w",
                                                              pady=(10, 2))

        def _set_status(text, is_err=False):
            status_lbl.config(text=text,
                              fg=COLORS["danger"] if is_err
                              else COLORS["text_secondary"])

        def _row(text, right_text="", right_color=None):
            """One label row with an optional right-aligned status."""
            row = tk.Frame(body, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x")
            tk.Label(row, text=text, font=FONT_SMALL,
                     fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
            if right_text:
                tk.Label(row, text=right_text, font=FONT_SMALL,
                         fg=right_color or COLORS["text_secondary"],
                         bg=COLORS["bg"]).pack(side="right")

        def _button_row(plugin_name, dist, installed):
            """Action buttons for one official plugin."""
            row = tk.Frame(body, bg=COLORS["bg"])
            row.pack(anchor="e", fill="x", pady=(0, 6))

            def _run(verb):
                # pip runs in a background thread (it blocks for seconds
                # on real installs — freezing the UI otherwise); Tk updates
                # go back through the drain queue.
                _set_status(self._t("plugins_ok", what=verb))

                def worker():
                    try:
                        if verb == "install":
                            proc = _pip_run(["install", "--quiet", dist])
                        else:
                            proc = _pip_run(["uninstall", "-y", dist])
                        ok = proc.returncode == 0
                        detail = (proc.stderr or "").strip()[-200:]
                    except FileNotFoundError:
                        ok, detail = False, "pip not available"

                    def finish():
                        if not win.winfo_exists():
                            return
                        if ok:
                            # drop the entry-point cache so a freshly
                            # installed/removed plugin lists correctly
                            clear_cache()
                            _set_status(self._t(
                                "plugins_ok",
                                what="{} {}".format(plugin_name, verb)))
                        else:
                            _set_status(self._t("plugins_err",
                                                detail=detail),
                                        is_err=True)
                        win.after(600, _refresh)
                    schedule(finish)

                threading.Thread(target=worker, daemon=True).start()

            if installed:
                FlatButton(row, text=self._t("plugins_uninstall"),
                           command=lambda: _run("uninstall"),
                           bg=COLORS["danger"], hover_bg=COLORS["danger_hover"],
                           font=FONT_SMALL, padx=12, pady=4).pack(side="right")
            else:
                FlatButton(row, text=self._t("plugins_install"),
                           command=lambda: _run("install"),
                           bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                           font=FONT_SMALL, padx=12, pady=4).pack(side="right")

        def _refresh():
            for child in body.winfo_children():
                child.destroy()
            _set_status("")
            installed_names = {p.name for p in discover_plugins()}

            _section_title(self._t("plugins_installed"))
            if installed_names:
                for p in discover_plugins():
                    ver = _installed_version("photo-s-plugin-" + p.name)
                    ver_str = " (v{})".format(ver) if ver else ""
                    provides = ", ".join(getattr(p, "provides", ())) or "-"
                    _row("{}  [{}]{}".format(p.name, provides, ver_str))
            else:
                _row(self._t("plugins_none"), right_text="")

            _section_title(self._t("plugins_available"))
            for name in sorted(OFFICIAL_PLUGINS):
                entry = to_dict(OFFICIAL_PLUGINS[name])
                installed = name in installed_names
                _row("{}  —  {}".format(name, entry["description"]),
                     right_text="✅" if installed else "")
                _button_row(name, entry["pypi_distribution"], installed)

        refresh_btn = FlatButton(win, text=self._t("plugins_refresh"),
                                 command=_refresh,
                                 bg=COLORS["bg"], fg=COLORS["text"],
                                 hover_bg=COLORS["border"],
                                 font=FONT_SMALL, padx=12, pady=4,
                                 border_color=COLORS["border"])
        refresh_btn.pack(side="left", padx=(18, 0), pady=(0, 12))
        FlatButton(win, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=20, pady=6).pack(side="right",
                                                           padx=18,
                                                           pady=(0, 12))

        _refresh()

    # ── Settings (MCP + optional deps) ──────────────────────────────────────

    def _show_settings(self):
        """Settings dialog: MCP server status + launch/config, optional
        dependency installs, and a link to the plugin manager."""
        import importlib.util
        from ..plugincmd import _pip_run

        win = tk.Toplevel(self.root)
        win.title(self._t("settings_title"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.geometry("620x580")

        inner = tk.Frame(win, bg=COLORS["bg"])
        inner.pack(fill="both", expand=True, padx=24, pady=18)

        def section_title(text):
            tk.Label(inner, text=text, font=FONT_SECTION,
                     fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w",
                                                              pady=(8, 2))

        def _status_lbl(parent, text, color):
            return tk.Label(parent, text=text, font=FONT_SMALL, fg=color,
                            bg=COLORS["bg"])

        # ── MCP section ─────────────────────────────────────────────────────
        section_title(self._t("set_mcp"))
        tk.Label(inner, text=self._t("set_mcp_desc"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"],
                 wraplength=560, justify="left").pack(anchor="w", pady=(0, 6))

        mcp_row = tk.Frame(inner, bg=COLORS["bg"])
        mcp_row.pack(anchor="w", fill="x")
        mcp_ok = importlib.util.find_spec("mcp") is not None
        mcp_status = _status_lbl(
            mcp_row,
            ("✅ " + self._t("mcp_installed")) if mcp_ok
            else ("❌ " + self._t("mcp_missing")),
            COLORS["success"] if mcp_ok else COLORS["danger"])
        mcp_status.pack(side="left")

        install_btns = []
        if not mcp_ok:
            def _install_mcp():
                self._run_dep_install(win, "mcp>=1.20,<2", mcp_install_btn,
                                      mcp_status,
                                      lambda: "✅ " + self._t("mcp_installed"))
            mcp_install_btn = FlatButton(
                mcp_row, text=self._t("dep_install"),
                command=_install_mcp,
                bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                font=FONT_SMALL, padx=10, pady=3)
            mcp_install_btn.pack(side="left", padx=(10, 0))
            install_btns.append(mcp_install_btn)

        # Launch command + copy
        tk.Label(inner, text=self._t("mcp_launch"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(anchor="w",
                                                                    pady=(10, 2))
        launch_row = tk.Frame(inner, bg=COLORS["bg"])
        launch_row.pack(anchor="w", fill="x")
        launch_entry = ttk.Entry(launch_row, font=FONT_SMALL)
        launch_entry.insert(0, "photo-s mcp")
        launch_entry.configure(state="readonly")
        launch_entry.pack(side="left", fill="x", expand=True)
        copy_launch = FlatButton(
            launch_row, text=self._t("copy"),
            command=lambda: self._copy_text(win, "photo-s mcp",
                                            copy_launch),
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            font=FONT_SMALL, padx=10, pady=3, border_color=COLORS["border"])
        copy_launch.pack(side="left", padx=(8, 0))

        # Claude Desktop config snippet
        tk.Label(inner, text=self._t("mcp_claude_config"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(anchor="w",
                                                                    pady=(10, 2))
        cfg_row = tk.Frame(inner, bg=COLORS["bg"])
        cfg_row.pack(anchor="w", fill="x")
        snippet = self._t("mcp_claude_snippet")
        cfg_text = tk.Text(cfg_row, height=7, font=("Menlo", 10),
                           bg=COLORS["card"], fg=COLORS["text"],
                           relief="flat", borderwidth=0, wrap="none")
        cfg_text.insert("1.0", snippet)
        cfg_text.configure(state="disabled")
        cfg_text.pack(side="left", fill="both", expand=True)
        copy_cfg = FlatButton(
            cfg_row, text=self._t("copy"),
            command=lambda: self._copy_text(win, snippet, copy_cfg),
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            font=FONT_SMALL, padx=10, pady=3, border_color=COLORS["border"])
        copy_cfg.pack(side="left", padx=(8, 0), fill="y")

        # ── Optional dependencies ────────────────────────────────────────────
        section_title(self._t("set_deps"))
        deps = [
            ("rawpy", "rawpy", "RAW"),
            ("piexif", "piexif", "EXIF"),
            ("watchdog", "watchdog", "watch"),
            ("opencv-python-headless", "cv2", "enhance"),
            ("mcp", "mcp", "MCP"),
        ]
        for dist, mod, purpose in deps:
            row = tk.Frame(inner, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x", pady=1)
            tk.Label(row, text=f"{dist} ({purpose})", font=FONT_SMALL,
                     fg=COLORS["text"], bg=COLORS["bg"],
                     width=30, anchor="w").pack(side="left")
            installed = importlib.util.find_spec(mod) is not None
            status = _status_lbl(
                row, ("✅ " + self._t("mcp_installed")) if installed
                else ("· " + self._t("mcp_missing")),
                COLORS["success"] if installed else COLORS["text_secondary"])
            status.pack(side="left")
            if not installed:
                btn = FlatButton(
                    row, text=self._t("dep_install"),
                    command=lambda d=dist, s=status, b=None:
                        self._run_dep_install(
                            win, d, b, s,
                            lambda: "✅ " + self._t("mcp_installed")),
                    bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                    font=FONT_SMALL, padx=8, pady=2)
                btn.pack(side="left", padx=(8, 0))
                install_btns.append(btn)

        # ── Plugin manager link ──────────────────────────────────────────────
        FlatButton(inner, text=self._t("set_plugins_link"),
                   command=lambda: (win.destroy(), self._show_plugin_manager()),
                   bg=COLORS["bg"], fg=COLORS["text"],
                   hover_bg=COLORS["border"], font=FONT_SMALL, padx=12, pady=4,
                   border_color=COLORS["border"]).pack(anchor="w", pady=(14, 0))

        self._settings_install_btns = install_btns

        FlatButton(inner, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=20, pady=6).pack(pady=(16, 0))

    def _copy_text(self, win, text, btn):
        """Copy text to the clipboard and flash the button label."""
        win.clipboard_clear()
        win.clipboard_append(text)
        old = btn.cget("text")
        btn.configure(text=self._t("copied"))

        def _restore():
            # The dialog may have been closed within the 1.2s — the widget
            # is gone then, and Tk's after is interp-global (destroying the
            # widget does NOT cancel it), so guard before touching it.
            if btn.winfo_exists():
                btn.configure(text=old)
        win.after(1200, _restore)

    def _run_dep_install(self, win, dist, btn, status_lbl, ok_text):
        """Install an optional dependency in a background thread.

        pip is subprocess-based (thread-safe); Tk must only be touched from
        the main thread, so all UI updates go through a queue drained by a
        main-thread after-loop. All install buttons are disabled during the
        run (pip holds a global lock — concurrent installs would wedge).
        """
        for b in getattr(self, "_settings_install_btns", []):
            if b is not None:
                b.configure(state="disabled")
        status_lbl.config(text=self._t("dep_installing"))

        bus = UiBus(win)
        bus.start()

        def worker():
            try:
                from ..plugincmd import _pip_run
                proc = _pip_run(["install", "--quiet", dist])
                ok = proc.returncode == 0
                detail = (proc.stderr or "").strip()[-200:]
            except FileNotFoundError:
                ok, detail = False, "pip not available"

            def finish():
                # The dialog may have been closed while pip ran.
                if not win.winfo_exists():
                    return
                for b in getattr(self, "_settings_install_btns", []):
                    if b is not None:
                        b.configure(state="normal")
                if ok:
                    status_lbl.config(text=ok_text(),
                                      fg=COLORS["success"])
                else:
                    status_lbl.config(
                        text="❌ " + (detail or self._t("mcp_missing")),
                        fg=COLORS["danger"])
            bus.schedule(finish)

        threading.Thread(target=worker, daemon=True).start()

    # ── Exposure Analysis ───────────────────────────────────────────────────

    def _show_analysis(self):
        """Dialog showing exposure / sharpness stats + luminance histogram
        for the currently selected file (via photo_s.metrics)."""
        from ..metrics import compute_exposure_stats, compute_blur_score

        selected = list(self._selected_rows)
        if not selected:
            messagebox.showwarning(self._t("analyze_title"),
                                   self._t("analyze_none"))
            return
        path = selected[0]

        stats = compute_exposure_stats(path)
        if not stats.get("ok"):
            messagebox.showerror(self._t("analyze_title"),
                                 self._t("analyze_err"))
            return

        win = tk.Toplevel(self.root)
        win.title("{} — {}".format(self._t("analyze_title"),
                                   os.path.basename(path)))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.resizable(False, False)

        inner = tk.Frame(win, bg=COLORS["bg"])
        inner.pack(padx=24, pady=20)

        def stat_row(label, value, color=None):
            row = tk.Frame(inner, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x", pady=1)
            tk.Label(row, text=label, font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"],
                     width=22, anchor="w").pack(side="left")
            tk.Label(row, text=value, font=FONT_SMALL,
                     fg=color or COLORS["text"], bg=COLORS["bg"]).pack(side="left")

        stat_row(self._t("analyze_luminance"),
                 "{:.3f}".format(stats["luminance"]))
        stat_row(self._t("analyze_over"),
                 "{:.2f}%".format(stats["overexposed_pct"]),
                 COLORS["danger"] if stats["overexposed_pct"] > 0
                 else COLORS["text"])
        stat_row(self._t("analyze_under"),
                 "{:.2f}%".format(stats["underexposed_pct"]),
                 COLORS["warning"] if stats["underexposed_pct"] > 0
                 else COLORS["text"])
        try:
            blur = compute_blur_score(path)
            stat_row(self._t("analyze_blur"), "{:.1f}".format(blur))
        except Exception:
            pass

        # Luminance histogram (from the same grayscale sample)
        tk.Label(inner, text=self._t("analyze_histogram"),
                 font=FONT_SECTION, fg=COLORS["text"],
                 bg=COLORS["bg"]).pack(anchor="w", pady=(14, 6))
        canvas = tk.Canvas(inner, width=360, height=120, bg=COLORS["card"],
                           highlightthickness=0, bd=0)
        canvas.pack()

        from PIL import Image
        img = _open_image_safe(path)
        sample = img.convert("L").copy()
        sample.thumbnail((256, 256))
        hist = sample.histogram()  # 256 bins

        max_bin = max(hist) or 1
        bins = 64  # aggregate into 64 bars
        bar_w = 360 / bins
        for i in range(bins):
            lo = i * 4
            hi = lo + 4
            h = sum(hist[lo:hi]) / max_bin
            h = max(h * 100, 1.0)  # min visible bar
            color = COLORS["accent"] if 30 <= lo <= 225 else COLORS["border"]
            canvas.create_rectangle(i * bar_w, 120 - h,
                                    (i + 1) * bar_w, 120,
                                    fill=color, outline="")

        FlatButton(inner, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=24, pady=6).pack(pady=(16, 0))

    # ── File Management ─────────────────────────────────────────────────────

    def _gallery_build(self, paths, out_dir, title="PhotoS Gallery",
                       thumb_size=360):
        """Sync seam → gui.workflows.gallery_build (Tk-free, testable)."""
        return workflows.gallery_build(paths, out_dir, title=title,
                                       thumb_size=thumb_size)


    def _preview_render(self, path, options):
        """Sync seam → gui.workflows.preview_render (Tk-free, testable)."""
        return workflows.preview_render(path, options)


    def _preview_options(self, tempdir):
        """Sync seam → gui.workflows.preview_options (Tk-free; base options
        from _build_options, preview NEVER deletes the source)."""
        return workflows.preview_options(self._build_options(), tempdir)


    def _contact_sheet_build(self, files, output, cols=4,
                             thumb_size=(240, 240), captions=True,
                             bg=(0, 0, 0)):
        """Sync seam → gui.workflows.contact_sheet_build (Tk-free)."""
        return workflows.contact_sheet_build(files, output, cols=cols,
                                             thumb_size=thumb_size,
                                             captions=captions, bg=bg)


    def _cull_scan(self, paths, thresholds, progress_cb=None):
        """Sync seam → gui.workflows.cull_scan (Tk-free, testable)."""
        return workflows.cull_scan(paths, thresholds,
                                   progress_cb=progress_cb)


    def _hash_generate(self, paths, output, algorithm="sha256",
                       progress_cb=None):
        """Sync seam → gui.workflows.hash_generate (Tk-free, testable)."""
        return workflows.hash_generate(paths, output, algorithm=algorithm,
                                       progress_cb=progress_cb)


    def _hash_verify(self, path):
        """Sync seam → gui.workflows.hash_verify (Tk-free, testable)."""
        return workflows.hash_verify(path)


    def _hdr_merge(self, paths, output, align=False):
        """Sync seam → gui.workflows.hdr_merge (Tk-free, testable)."""
        return workflows.hdr_merge(paths, output, align=align)


    def _apply_options_to_ui(self, opts):
        """Map a ProcessOptions back onto the GUI's tk.Variables (preset
        load). Forgiving: each field is wrapped in try/except so an
        unexpected value degrades instead of aborting. Fields without a GUI
        var (gpx_trace, scrub, date_shift, resume, …) are skipped."""
        def _fmt_num(v) -> str:
            if float(v) == int(v):
                return str(int(v))
            return f"{float(v):.4g}"

        def _set(var, value, fmt=str):
            try:
                if value is None:
                    if isinstance(var, tk.StringVar):
                        var.set("")
                    return
                var.set(fmt(value))
            except Exception:
                pass

        # booleans
        for name in ("preserve_exif", "optimize", "progressive", "overwrite",
                     "raw_half_size", "raw_auto_bright", "raw_16bit",
                     "auto_rotate", "remove_original", "strip_gps",
                     "keep_mtime", "grayscale", "sepia", "auto_levels",
                     "srgb", "flatten_cmyk", "evaluate", "blur_score",
                     "resume", "sync_date", "scrub"):
            _set(getattr(self, name), getattr(opts, name), bool)
        # floats / sliders
        for name in ("brightness", "contrast", "saturation", "gamma",
                     "sharpen", "ev", "wb_tint", "vibrance", "clarity",
                     "texture", "dehaze"):
            _set(getattr(self, name), getattr(opts, name), float)
        # export sharpen / highlight recovery: None → 0 (slider off)
        _set(self.export_sharpen,
             getattr(opts, "export_sharpen", None) or 0.0, float)
        _set(self.highlight_recovery,
             getattr(opts, "highlight_recovery", None) or 0.0, float)
        _set(self.lens_distort, getattr(opts, "lens_distort", 0.0), float)
        # strings (None → "")
        for name in ("output_dir", "prefix", "suffix", "scale_percent",
                     "max_width", "max_height", "max_pixels",
                     "watermark_text", "watermark_image", "auto_exposure",
                     "log_curve", "denoise", "lut_file", "max_straighten_angle",
                     "wb_temp", "wb_reference", "print_size", "crop",
                     "crop_ratio", "rotate_bg", "flip", "pad_ratio",
                     "pad_bg", "rename_pattern", "folder_pattern",
                     "levels", "curves", "color_grading", "hsl",
                     "vignette", "grain", "point_color", "masks",
                     "mask_adjust", "lens_vignette", "lens_ca",
                     "lens_profile"):
            _set(getattr(self, name), getattr(opts, name), str)
        # face blur combobox stores localized labels, options store
        # "blur"/"pixelate"/None
        self.blur_faces.set({
            None: "", "blur": self._t("blur_faces_blur"),
            "pixelate": self._t("blur_faces_pixelate")}.get(
                getattr(opts, "blur_faces", None), ""))
        _set(self.blur_faces_margin, getattr(opts, "blur_faces_margin", None),
             str)
        # cutout spec → vars (inverse of _cutout_spec; mode first so the
        # trace shows the right params frame; invalid/None resets; the
        # invert keyword is not exposed in the GUI and is dropped)
        cutout = getattr(opts, "cutout", None)
        if not cutout:
            self.cutout_mode.set("")
            self.cutout_label.set("")
            self.cutout_r.set("255")
            self.cutout_g.set("255")
            self.cutout_b.set("255")
            self.cutout_tol.set("30")
            self.cutout_feather.set("0")
        else:
            try:
                from ..cutout import parse_cutout
                spec = parse_cutout(cutout)
                kind_label = {
                    "subject": self._t("cutout_subject"),
                    "person": self._t("cutout_person"),
                    "object": self._t("cutout_object"),
                    "color": self._t("cutout_color")}.get(spec.kind, "")
            except Exception:
                kind_label = ""
            self.cutout_mode.set(kind_label)
            if kind_label == self._t("cutout_object"):
                self.cutout_label.set(spec.label or "")
            elif kind_label == self._t("cutout_color"):
                r, g, b = spec.rgb
                self.cutout_r.set(str(r))
                self.cutout_g.set(str(g))
                self.cutout_b.set(str(b))
                self.cutout_tol.set(_fmt_num(spec.tol))
                self.cutout_feather.set(_fmt_num(spec.feather))
        # ints
        _set(self.quality, getattr(opts, "quality", None), int)
        _set(self.watermark_opacity, getattr(opts, "watermark_opacity", None),
             int)
        _set(self.jobs, getattr(opts, "jobs", None), str)
        _set(self.output_format, getattr(opts, "output_format", None), str)
        _set(self.jpeg_subsampling, getattr(opts, "jpeg_subsampling", "420"),
             str)
        _set(self.raw_demosaic, getattr(opts, "raw_demosaic", "auto"), str)
        _set(self.raw_color_space, getattr(opts, "raw_color_space", "sRGB"),
             str)
        # rotate field name differs from the var
        _set(self.rotate, getattr(opts, "rotate_degrees", None), str)
        # watermark position (only set when the preset carries a valid value)
        pos = getattr(opts, "watermark_position", None)
        if pos:
            _set(self.watermark_position, pos, str)
        # target_size_bytes → target mode on + value/unit
        try:
            tsz = getattr(opts, "target_size_bytes", None)
            if tsz:
                self.target_size_mode.set(True)
                if tsz >= 1024 * 1024:
                    self.target_size_value.set(str(round(tsz / 1024 / 1024)))
                    self.target_size_unit.set("MB")
                else:
                    self.target_size_value.set(str(round(tsz / 1024)))
                    self.target_size_unit.set("KB")
        except Exception:
            pass
        # output_sizes list[tuple] → "label:WxH,..." (inverse of _parse_sizes)
        try:
            sizes = getattr(opts, "output_sizes", None)
            if sizes:
                def _dim(v):
                    return "" if v is None else str(v)
                self.output_sizes.set(",".join(
                    "{}:{}x{}".format(label, _dim(w), _dim(h))
                    for label, w, h in sizes))
        except Exception:
            pass
        self._refresh_grade_value_labels()

    def _show_gallery_export(self):
        """Gallery export dialog: title + thumb size + output dir, then
        build the HTML gallery in a background thread (thumbnail
        rendering can take a while)."""
        if not self.files:
            messagebox.showinfo(self._t("gallery_title"),
                                self._t("gallery_need_files"))
            return
        files = self._checked_files()
        if not files:
            messagebox.showinfo(self._t("gallery_title"),
                                self._t("check_none"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("gallery_title"))
        win.geometry("480x360")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(0, weight=1)

        tk.Label(body, text=self._t("gallery_name"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        title_var = tk.StringVar(value="PhotoS Gallery")
        ttk.Entry(body, textvariable=title_var, font=FONT_BODY).grid(
            row=1, column=0, sticky="ew", pady=(0, 10))

        tk.Label(body, text=self._t("gallery_thumb"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=2, column=0, sticky="w", pady=(0, 2))
        thumb_combo = ttk.Combobox(
            body, values=("240", "360", "480", "600"),
            state="readonly", font=FONT_BODY, width=8)
        thumb_combo.set("360")
        thumb_combo.grid(row=3, column=0, sticky="w", pady=(0, 10))

        tk.Label(body, text=self._t("gallery_out"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=4, column=0, sticky="w", pady=(0, 2))
        out_row = tk.Frame(body, bg=COLORS["bg"])
        out_row.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        out_var = tk.StringVar(value=self.output_dir.get())
        ttk.Entry(out_row, textvariable=out_var, font=FONT_BODY).pack(
            side="left", fill="x", expand=True)

        def _browse_out():
            if self._dlg_cooldown_active():
                return
            picked = filedialog.askdirectory(title=self._t("gallery_out"))
            self._after_file_dialog()
            if picked:
                out_var.set(picked)

        FlatButton(
            out_row, text=self._t("browse"), command=_browse_out,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            font=FONT_SMALL, padx=10, pady=3,
            border_color=COLORS["border"]).pack(side="left", padx=(8, 0))

        status_lbl = tk.Label(body, text="", font=FONT_SMALL,
                              fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_lbl.grid(row=6, column=0, sticky="w", pady=(0, 8))

        btns = tk.Frame(body, bg=COLORS["bg"])
        btns.grid(row=7, column=0, sticky="w")
        state = {"output": None}

        # Worker threads must never touch Tk directly: they put UI
        # callbacks on a queue and a main-thread after-loop drains it
        # (win.after from a worker raises "main thread is not in main
        # loop" whenever the mainloop is not running).
        bus = UiBus(win)
        schedule = bus.schedule
        bus.start()

        def set_status(text, color=None):
            if win.winfo_exists():
                status_lbl.configure(
                    text=text, fg=color or COLORS["text_secondary"])

        def run(out_dir, title, thumb):
            # Tk variables are read in the button command (main thread) and
            # handed in as plain values — touching out_var/title_combo from
            # this worker thread is unsafe under non-threaded Tcl.
            if not out_dir:
                schedule(lambda: set_status(
                    self._t("gallery_need_dir"), COLORS["danger"]))
                return
            schedule(lambda: (
                generate_btn.configure(state="disabled"),
                set_status(self._t("gallery_generating"))))
            try:
                res = self._gallery_build(
                    list(files), out_dir,
                    title=title,
                    thumb_size=int(thumb))
            except Exception as e:
                schedule(lambda err=str(e): _failed(err))
            else:
                schedule(lambda: _done(res))

        def _failed(err):
            if not win.winfo_exists():
                return
            generate_btn.configure(state="normal")
            set_status(self._t("gallery_error", err=err), COLORS["danger"])

        def _done(res):
            if not win.winfo_exists():
                return
            state["output"] = res["output"]
            generate_btn.configure(state="normal")
            set_status(self._t("gallery_done", count=res["count"],
                               path=res["output"]), COLORS["accent"])
            open_btn.pack(side="left", padx=(8, 0))

        generate_btn = FlatButton(
            btns, text=self._t("gallery_generate"),
            command=lambda: threading.Thread(
                target=run,
                args=(out_var.get().strip(),
                      title_var.get().strip() or "PhotoS Gallery",
                      thumb_combo.get()),
                daemon=True).start(),
            bg=COLORS["accent"])
        generate_btn.pack(side="left")
        open_btn = FlatButton(
            btns, text=self._t("gallery_open"),
            command=lambda: state["output"]
            and webbrowser.open(Path(state["output"]).as_uri()),
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            border_color=COLORS["border"])

    # ── Duplicate viewer ────────────────────────────────────────────────────

    def _dedup_scan(self, paths, threshold=5, progress_cb=None):
        """Sync seam → gui.workflows.dedup_scan (Tk-free, testable)."""
        return workflows.dedup_scan(paths, threshold=threshold,
                                    progress_cb=progress_cb)


    def _dedup_trash_path(self, path, trash_dir):
        """Sync seam → gui.workflows.dedup_trash_path (Tk-free)."""
        return workflows.dedup_trash_path(path, trash_dir)


    def _dedup_move_to_trash(self, paths, trash_dir, progress_cb=None):
        """Sync seam → gui.workflows.dedup_move_to_trash (Tk-free)."""
        return workflows.dedup_move_to_trash(paths, trash_dir,
                                             progress_cb=progress_cb)


    def _show_dedup(self):
        """Duplicate viewer: scan in a background thread, render groups
        with per-image keep-checkboxes (sharpest pre-checked), move the
        unchecked ones into a ``_duplicates_trash`` subfolder."""
        if not self.files:
            messagebox.showinfo(self._t("dedup_title"),
                                self._t("gallery_need_files"))
            return
        files = self._checked_files()
        if not files:
            messagebox.showinfo(self._t("dedup_title"),
                                self._t("check_none"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("dedup_title"))
        win.geometry("980x660")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        canvas_unbind_safe(win)

        header = tk.Frame(win, bg=COLORS["bg"])
        header.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(header, text=self._t("dedup_title"),
                 font=(PLATFORM_FONTS["title"], 14, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        status_lbl = tk.Label(header, text="", font=FONT_SMALL,
                              fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_lbl.pack(side="left", padx=(16, 0))

        # Scrollable group area
        holder = tk.Frame(win, bg=COLORS["bg"])
        holder.pack(fill="both", expand=True, padx=20, pady=8)
        canvas = tk.Canvas(holder, bg=COLORS["bg"], highlightthickness=0,
                           borderwidth=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=COLORS["bg"])
        canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scroll(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", _sync_scroll)

        def _on_mw(event):
            if inner.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _on_mw))
        canvas.bind("<Leave>",
                    lambda e: canvas.unbind_all("<MouseWheel>"))

        footer = tk.Frame(win, bg=COLORS["bg"])
        footer.pack(fill="x", padx=20, pady=(4, 16))

        state = {"groups": [], "scores": {}, "checks": [], "sharp": set()}

        # Worker→UI marshalling queue (see gallery dialog for rationale)
        bus = UiBus(win)
        schedule = bus.schedule
        bus.start()

        def render():
            for w in inner.winfo_children():
                w.destroy()
            state["checks"] = []
            state["sharp"] = set()
            groups, scores = state["groups"], state["scores"]
            if not groups:
                tk.Label(inner, text=self._t("dedup_none"), font=FONT_BODY,
                         fg=COLORS["text_secondary"],
                         bg=COLORS["bg"]).pack(pady=40)
                execute_btn.configure(state="disabled")
                return
            from PIL import Image, ImageTk
            for gi, group in enumerate(groups):
                card = tk.Frame(inner, bg=COLORS["card"], bd=0,
                                highlightthickness=0)
                card.pack(fill="x", padx=2, pady=(0, 10))
                head = tk.Frame(card, bg=COLORS["card"])
                head.pack(fill="x", padx=12, pady=(10, 4))
                tk.Label(head, text=self._t("dedup_group", i=gi + 1),
                         font=(PLATFORM_FONTS["body"], 12, "bold"),
                         fg=COLORS["text"], bg=COLORS["card"]).pack(
                    side="left")
                tk.Label(head, text="· {}".format(
                    self._t("files_count", n=len(group))), font=FONT_SMALL,
                    fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
                    side="left", padx=(8, 0))
                row = tk.Frame(card, bg=COLORS["card"])
                row.pack(fill="x", padx=12, pady=(0, 12))
                sharpest = max(group,
                               key=lambda p: scores.get(p, 0.0))
                state["sharp"].add(sharpest)
                for p in group:
                    cell = tk.Frame(row, bg=COLORS["card"])
                    cell.pack(side="left", padx=6)
                    try:
                        img = _open_image_safe(p).convert("RGB")
                        img.thumbnail((150, 150), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img, master=cell)
                        lbl = tk.Label(cell, image=photo, bg=COLORS["bg"],
                                       bd=0, highlightthickness=0)
                        lbl.image = photo  # keep the reference alive
                        lbl.pack()
                    except Exception:
                        tk.Label(cell, text="?", width=12, height=6,
                                 bg=COLORS["bg"],
                                 fg=COLORS["text_secondary"]).pack()
                    cap = os.path.basename(p)
                    if len(cap) > 16:
                        cap = cap[:15] + "…"
                    star = " " + self._t("dedup_sharpest") \
                        if p == sharpest else ""
                    tk.Label(cell, text=cap + star, font=FONT_TINY,
                             fg=COLORS["text_secondary"],
                             bg=COLORS["card"]).pack()
                    tk.Label(cell, text="{} {:.1f}".format(
                        self._t("dedup_blur"), scores.get(p, 0.0)),
                        font=FONT_TINY, fg=COLORS["text_secondary"],
                        bg=COLORS["card"]).pack()
                    var = tk.BooleanVar(value=(p == sharpest))
                    ttk.Checkbutton(cell, text=self._t("dedup_keep"),
                                    variable=var).pack(pady=(2, 0))
                    state["checks"].append((p, var))
            execute_btn.configure(state="normal")

        def scan_thread():
            try:
                def cb(cur, total):
                    schedule(lambda: status_lbl.configure(
                        text=self._t("dedup_scanning", n=cur, total=total)))

                groups, scores = self._dedup_scan(list(files),
                                                  progress_cb=cb)
            except Exception as e:
                schedule(lambda err=str(e): _scan_failed(err))
                return
            schedule(lambda: _scanned(groups, scores))

        def _scan_failed(err):
            if not win.winfo_exists():
                return
            status_lbl.configure(text=self._t("op_failed", err=err),
                                 fg=COLORS["danger"])

        def _scanned(groups, scores):
            if not win.winfo_exists():
                return
            state["groups"], state["scores"] = groups, scores
            status_lbl.configure(text="")
            render()

        def execute():
            unchecked = [p for p, var in state["checks"] if not var.get()]
            if not unchecked:
                messagebox.showinfo(self._t("dedup_title"),
                                    self._t("dedup_none_selected"))
                return
            if not messagebox.askyesno(
                    self._t("dedup_title"),
                    self._t("dedup_confirm", n=len(unchecked))):
                return
            # single trash dir next to the first file's folder
            trash_dir = os.path.join(os.path.dirname(files[0]),
                                     "_duplicates_trash")
            execute_btn.configure(state="disabled")

            def move_thread():
                try:
                    def cb(cur, total):
                        schedule(lambda: status_lbl.configure(
                            text="{} {}/{}".format(self._t("dedup_moving"),
                                                   cur, total)))

                    moved, failed, moved_map = self._dedup_move_to_trash(
                        unchecked, trash_dir, progress_cb=cb)
                except Exception as e:
                    schedule(lambda err=str(e): _scan_failed(err))
                    return
                schedule(lambda: _moved(moved, failed, moved_map, unchecked,
                                        trash_dir))

            def _moved(moved, failed, moved_map, unchecked, trash_dir):
                if not win.winfo_exists():
                    return
                # Only files that actually moved leave the UI — a failed
                # move must keep its row (set(unchecked) would hide it).
                moved_set = set(moved_map)
                self.files = [f for f in self.files if f not in moved_set]
                self._checked -= moved_set
                self._refresh_file_list()
                self._update_stats()
                state["groups"] = [
                    [p for p in g if p not in moved_set]
                    for g in state["groups"]]
                state["groups"] = [g for g in state["groups"]
                                   if len(g) >= 2]
                state["scores"] = {p: s for p, s in state["scores"].items()
                                   if p not in moved_set}
                msg = self._t("dedup_moved", n=moved, dir=trash_dir)
                if failed:
                    msg += "（{} 失败）".format(failed)
                status_lbl.configure(text=msg, fg=COLORS["accent"])
                if moved_map:
                    self._push_undo(
                        self._t("undo_dedup", n=len(moved_map)),
                        lambda: self._restore_dedup(dict(moved_map)))
                render()

            threading.Thread(target=move_thread, daemon=True).start()

        execute_btn = FlatButton(
            footer, text=self._t("dedup_execute"), command=execute,
            bg=COLORS["accent"])
        execute_btn.configure(state="disabled")
        execute_btn.pack(side="left")
        FlatButton(
            footer, text=self._t("dedup_rescan"),
            command=lambda: threading.Thread(target=scan_thread,
                                             daemon=True).start(),
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            border_color=COLORS["border"]).pack(side="left", padx=(8, 0))
        FlatButton(
            footer, text=self._t("close"), command=win.destroy,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            border_color=COLORS["border"]).pack(side="right")

        threading.Thread(target=scan_thread, daemon=True).start()

    # ── Review & rate dialog ────────────────────────────────────────────────

    def _review_scan(self, paths, progress_cb=None):
        """Sync seam → gui.workflows.review_scan (Tk-free, testable)."""
        return workflows.review_scan(paths, progress_cb=progress_cb)


    def _review_save(self, path, rating=None, keywords=None, title=None,
                     make=None, model=None, lens=None, iso=None,
                     shutter=None, aperture=None, date=None):
        """Sync: write rating/keywords/title + camera/lens/shooting-field
        diffs into ``path``'s EXIF (PhotoS: UserComment segment for the
        first three; standard EXIF tags for the rest — ``aperture`` maps
        to the engine's ``fnumber`` key, ``date`` to ``datetime``).
        Only changed fields are touched: None leaves a field alone, a
        string ("" included) writes/clears it. Returns (ok, message,
        revert, entry): revert undoes this exact write (None when
        nothing changed); entry is the global undo entry pushed (None
        likewise). Tk-free so tests can call it directly."""
        from ..engine import apply_exif_tags, read_exif_metadata

        m = read_exif_metadata(path)
        tags = {}
        if rating is not None and rating != m.get("rating"):
            tags["rating"] = rating
        kw = (keywords or "").strip()
        if kw != ",".join(m.get("keywords") or []):
            tags["keywords"] = kw
        tl = (title or "").strip()
        if tl != (m.get("title") or ""):
            tags["title"] = tl
        # (argument, meta key holding the current value, engine tag);
        # meta key None → the current value is the normalized
        # date+time pair (EXIF DateTimeOriginal form).
        prev_extra = {}
        for value, meta_key, tag in (
                (make, "make", "make"),
                (model, "camera", "model"),
                (lens, "lens", "lens"),
                (iso, "iso", "iso"),
                (shutter, "shutter", "shutter"),
                (aperture, "fnumber", "fnumber"),
                (date, None, "datetime")):
            if value is None:
                continue
            cur = (_exif_datetime_str(m) if meta_key is None
                   else str(m.get(meta_key) or "").strip())
            prev_extra[tag] = cur
            v = str(value).strip()
            if v != cur:
                tags[tag] = v
        if not tags:
            return True, "", None, None
        prev = {"rating": m.get("rating"),
                "keywords": ",".join(m.get("keywords") or []),
                "title": m.get("title") or ""}
        prev.update(prev_extra)
        try:
            msg = apply_exif_tags(path, tags)
        except Exception as e:
            return False, self._t("review_save_failed", err=str(e)), None, None
        if msg.startswith("⚠️"):
            return False, msg, None, None

        def revert():
            # full restore — None / "" explicitly clear the fields
            # (engine clear semantics, added for undo)
            t = {"rating": prev["rating"],
                 "keywords": prev["keywords"],
                 "title": prev["title"]}
            for tag in prev_extra:
                t[tag] = prev[tag]
            apply_exif_tags(path, t)

        entry = self._push_undo(
            self._t("undo_tag", name=os.path.basename(path)), revert)
        return True, msg, revert, entry

    def _select_move(self, paths, selects_dir, rejects_dir,
                     keep_min=4, reject_max=2, mode="move"):
        """Sync seam → gui.workflows.select_move (Tk-free, testable)."""
        return workflows.select_move(paths, selects_dir, rejects_dir,
                                     keep_min=keep_min,
                                     reject_max=reject_max, mode=mode)


    def _post_more_menu(self):
        """Toolbar 'More Tools' popup: watch / contact sheet / cull / hash /
        presets. A single button keeps the workflow row uncluttered; the whole
        menu locks during processing (it is not in the lockout exemption)."""
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label=self._t("more_watch"),
                         command=self._show_watch)
        menu.add_command(label=self._t("more_contact"),
                         command=self._show_contact_sheet)
        menu.add_command(label=self._t("more_cull"),
                         command=self._show_cull)
        menu.add_command(label=self._t("more_hash"),
                         command=self._show_hash)
        menu.add_command(label=self._t("more_hdr"),
                         command=self._show_hdr)
        menu.add_command(label=self._t("more_presets"),
                         command=self._show_presets)
        menu.add_separator()
        menu.add_command(label=self._t("analyze"),
                         command=self._show_analysis)
        menu.add_command(label=self._t("clear"),
                         command=self._clear_files)
        try:
            menu.tk_popup(self.more_btn.winfo_rootx(),
                          self.more_btn.winfo_rooty()
                          + self.more_btn.winfo_height())
        finally:
            menu.grab_release()

    def _show_review(self):
        """v2.5: 审查灯箱并入 Library（原独立 Toplevel 弹窗）。

        切到 Library 并把文件列表区换成内嵌灯箱：导航 / 0-5 星 / 关键词
        标题 / 拍摄信息编辑 / 精选淘汰双阈值分拣。EXIF 写走 _review_save
        （UserComment 分段，部分更新保留其他标签）；作用域 = 勾选照片；
        Esc / 关闭退出回网格。⌘E 入口不变。
        """
        if not self.files:
            messagebox.showinfo(self._t("review_title"),
                                self._t("review_none"))
            return
        all_paths = self._checked_files()
        if not all_paths:
            messagebox.showinfo(self._t("review_title"),
                                self._t("check_none"))
            return
        if self._active_module != "library":
            self._show_module("library")
        if getattr(self, "_lib_lightbox_active", False):
            self._exit_library_lightbox()  # 保存挂起编辑后重建
        self._lib_lightbox_active = True
        self._lib_lightbox_frame = tk.Frame(self._lib_lightbox_host,
                                            bg=COLORS["bg"])
        start = next(iter(self._selected_rows or ()), None)
        self._lib_lightbox_ctl = self._build_review_ui(
            self._lib_lightbox_frame, all_paths, hosted=True,
            start_path=start if start in all_paths else None)
        self._lib_lightbox_frame.pack(fill="both", expand=True)
        self._lib_lightbox_apply_layout()

    def _exit_library_lightbox(self):
        """退出灯箱回网格：冲掉输入框里未提交的关键词/标题（即改即写
        语义与弹窗 OK 一致），刷新网格行内星标。"""
        ctl = self._lib_lightbox_ctl
        self._lib_lightbox_ctl = None
        self._lib_lightbox_active = False
        if ctl is not None:
            ctl["close"]()
        if self._lib_lightbox_frame is not None:
            self._lib_lightbox_frame.destroy()
            self._lib_lightbox_frame = None
        self._lib_lightbox_apply_layout()
        cache = getattr(self, "_lib_rating_cache", None)
        if cache is not None:
            cache.clear()
        self._lib_draw()

    def _lib_lightbox_apply_layout(self):
        """Library 卡片内 pack 切换：文件网格 ↔ 内嵌灯箱（工具栏不动）。"""
        if getattr(self, "_lib_lightbox_active", False):
            self._lib_list_frame.pack_forget()
            self._lib_lightbox_host.pack(fill="both", expand=True,
                                         padx=14, pady=12)
        else:
            self._lib_lightbox_host.pack_forget()
            self._lib_list_frame.pack(fill="both", expand=True,
                                      padx=14, pady=12)

    def _build_review_ui(self, host, all_paths, *, hosted=False,
                         start_path=None):
        """Lightbox review UI — 弹窗/内嵌共用构建器。

        导航 / 0-5 星评级 / 关键词标题 / 拍摄信息 / 评分关键词筛选 /
        精选淘汰分拣。``hosted=True`` 由 Library 内嵌（退出走
        ``self._exit_library_lightbox``），否则 host 为 Toplevel（诊断
        路径）。返回 ``{close, set_rating, go, undo}`` 控制器。
        """
        import importlib.util

        from ..engine import read_exif_metadata
        has_piexif = importlib.util.find_spec("piexif") is not None

        initial_idx = (all_paths.index(start_path)
                       if start_path and start_path in all_paths else 0)
        state = {"seq": [], "meta": {}, "idx": 0, "rating": None,
                 "photo": None, "reverts": {}}

        header = tk.Frame(host, bg=COLORS["bg"])
        header.pack(fill="x", padx=20, pady=(14, 4))
        tk.Label(header, text=self._t("review_title"),
                 font=(PLATFORM_FONTS["title"], 14, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        pos_lbl = tk.Label(header, text="", font=FONT_BODY,
                           fg=COLORS["text_secondary"], bg=COLORS["bg"])
        pos_lbl.pack(side="left", padx=(16, 0))
        status_lbl = tk.Label(header, text="", font=FONT_SMALL,
                              fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_lbl.pack(side="right")

        if not has_piexif:
            tk.Label(host, text=self._t("review_no_piexif"), font=FONT_SMALL,
                     fg=COLORS["danger"], bg=COLORS["bg"]).pack(
                anchor="w", padx=20)

        # Image area + shooting-info line
        img_lbl = tk.Label(host, bg=COLORS["bg"])
        img_lbl.pack(fill="both", expand=True, padx=20, pady=8)
        info_lbl = tk.Label(host, text="", font=FONT_SMALL,
                            fg=COLORS["text_secondary"], bg=COLORS["bg"])
        info_lbl.pack(fill="x", padx=20, pady=(0, 2))

        # Nav + rating row
        ctrl = tk.Frame(host, bg=COLORS["bg"])
        ctrl.pack(fill="x", padx=20, pady=(0, 6))
        nav = tk.Frame(ctrl, bg=COLORS["bg"])
        nav.pack(side="left")
        prev_btn = FlatButton(
            nav, text=self._t("review_prev"), command=lambda: go(-1),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL)
        prev_btn.pack(side="left")
        next_btn = FlatButton(
            nav, text=self._t("review_next"), command=lambda: go(1),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL)
        next_btn.pack(side="left", padx=(8, 0))

        rating_box = tk.Frame(ctrl, bg=COLORS["bg"])
        rating_box.pack(side="left", padx=(20, 0))
        tk.Label(rating_box, text=self._t("review_rating"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(
            side="left", padx=(0, 6))
        rating_btns = {}
        for n in range(6):
            btn = FlatButton(
                rating_box, text="{}★".format(n),
                command=lambda n=n: set_rating(n),
                bg=COLORS["card"], fg=COLORS["text"],
                hover_bg=COLORS["bg"], border_color=COLORS["border"],
                font=FONT_SMALL, padx=10, pady=3)
            btn.pack(side="left", padx=(4, 0))
            rating_btns[n] = btn

        # Keywords + title row
        fields = tk.Frame(host, bg=COLORS["bg"])
        fields.pack(fill="x", padx=20, pady=(0, 4))
        tk.Label(fields, text=self._t("review_keywords"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        keywords_var = tk.StringVar()
        ttk.Entry(fields, textvariable=keywords_var, font=FONT_BODY).pack(
            side="left", fill="x", expand=True, padx=(8, 16))
        tk.Label(fields, text=self._t("review_title_lbl"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        title_var = tk.StringVar()
        ttk.Entry(fields, textvariable=title_var, font=FONT_BODY).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        save_btn = FlatButton(
            fields, text=self._t("review_save"),
            command=lambda: save_current(),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=10, pady=3)
        save_btn.pack(side="left", padx=(8, 0))
        FlatButton(
            fields, text=self._t("undo"),
            command=lambda: undo_current(),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL, padx=10, pady=3
        ).pack(side="left", padx=(8, 0))

        # Shooting-info editor: make / model / lens / ISO / shutter /
        # aperture / date. Filled from the current image's metadata on
        # every navigation; on save, unchanged fields go out as None so
        # only real edits hit the file (_review_save diffs again anyway).
        exif = tk.Frame(host, bg=COLORS["bg"])
        exif.pack(fill="x", padx=20, pady=(0, 4))
        tk.Label(exif, text=self._t("review_shooting"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=0, column=0, rowspan=2, sticky="nw", pady=2)
        exif_rows = (
            (("make", "review_make", 12), ("model", "review_model", 14),
             ("lens", "review_lens", 18), ("iso", "review_iso", 6)),
            (("shutter", "review_shutter", 9),
             ("aperture", "review_aperture", 7),
             ("date", "review_date", 20)),
        )
        exif_vars = {}
        for r, row_fields in enumerate(exif_rows):
            col = 1
            for name, lbl_key, width in row_fields:
                tk.Label(exif, text=self._t(lbl_key), font=FONT_SMALL,
                         fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
                    row=r, column=col, sticky="w", padx=(12, 4), pady=1)
                var = tk.StringVar()
                exif_vars[name] = var
                ttk.Entry(exif, textvariable=var, font=FONT_SMALL,
                          width=width).grid(
                    row=r, column=col + 1, sticky="w", pady=1)
                col += 2

        # Filter row
        filt = tk.Frame(host, bg=COLORS["bg"])
        filt.pack(fill="x", padx=20, pady=(0, 14))
        tk.Label(filt, text=self._t("review_filter"), font=FONT_BODY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            side="left")
        tk.Label(filt, text=self._t("review_min_rating"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            side="left", padx=(10, 4))
        min_rating_var = tk.StringVar(value="0")
        ttk.Combobox(filt, values=("0", "1", "2", "3", "4", "5"),
                     textvariable=min_rating_var, state="readonly",
                     width=3, font=FONT_SMALL).pack(side="left")
        tk.Label(filt, text=self._t("review_filter_kw"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            side="left", padx=(10, 4))
        filter_var = tk.StringVar()
        ttk.Entry(filt, textvariable=filter_var, font=FONT_SMALL,
                  width=16).pack(side="left")
        FlatButton(
            filt, text=self._t("review_apply_filter"),
            command=lambda: apply_filter(),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=10, pady=3).pack(side="left", padx=(8, 0))
        FlatButton(
            filt, text=self._t("review_clear_filter"),
            command=lambda: clear_filter(),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=10, pady=3).pack(side="left", padx=(8, 0))
        FlatButton(
            filt, text=self._t("close"), command=lambda: on_close(),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=10, pady=3).pack(side="right")

        # Select (keeper workflow) row: after rating, move keepers/rejects to
        # the chosen folders. Acts on the currently filtered set.
        sel = tk.Frame(host, bg=COLORS["bg"])
        sel.pack(fill="x", padx=20, pady=(0, 14))
        tk.Label(sel, text=self._t("review_select_lbl"), font=FONT_BODY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            side="left")
        selects_var = tk.StringVar()
        ttk.Entry(sel, textvariable=selects_var, font=FONT_SMALL,
                  width=16).pack(side="left", padx=(8, 0))
        FlatButton(
            sel, text="📁",
            command=lambda: browse_dir(selects_var),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=6, pady=3).pack(side="left", padx=(2, 10))
        tk.Label(sel, text=self._t("review_rejects_lbl"), font=FONT_BODY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            side="left")
        rejects_var = tk.StringVar()
        ttk.Entry(sel, textvariable=rejects_var, font=FONT_SMALL,
                  width=16).pack(side="left", padx=(8, 0))
        FlatButton(
            sel, text="📁",
            command=lambda: browse_dir(rejects_var),
            bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=6, pady=3).pack(side="left", padx=(2, 10))
        FlatButton(
            sel, text=self._t("review_select_go"),
            command=lambda: do_select(),
            bg=COLORS["accent"], fg="white", hover_bg=COLORS["accent_hover"],
            border_color=COLORS["accent"], font=FONT_SMALL,
            padx=12, pady=3).pack(side="left")

        def browse_dir(var):
            from tkinter import filedialog
            d = filedialog.askdirectory(
                title=self._t("review_select_browse"))
            self._after_file_dialog()
            if d:
                var.set(d)

        def do_select():
            save_current()  # persist the pending rating before sorting
            sd = selects_var.get().strip()
            rd = rejects_var.get().strip()
            if not sd and not rd:
                messagebox.showwarning(
                    self._t("app_title"),
                    self._t("review_select_need_dir"))
                return
            seq = list(state["seq"])
            if not seq:
                return
            results, okc, errc, err = self._select_move(
                seq, sd or None, rd or None, keep_min=4, reject_max=2)
            if err:
                messagebox.showerror(self._t("app_title"), err)
                return
            moved = {r["path"] for r in results
                     if r["ok"] and r["action"] in ("move", "copy")}
            # drop moved files from the queue so the lightbox advances
            state["seq"] = [p for p in state["seq"] if p not in moved]
            if not state["seq"]:
                state["seq"] = list(all_paths)
            state["idx"] = 0
            show()
            if errc:
                messagebox.showwarning(
                    self._t("app_title"),
                    self._t("review_select_done_warn",
                            ok=okc, err=errc))
            else:
                set_status(self._t("review_select_done", n=okc),
                           COLORS["accent"])

        # Worker→UI marshalling queue (see gallery dialog for rationale)
        bus = UiBus(host)
        schedule = bus.schedule
        bus.start()

        def set_status(text, color=None):
            status_lbl.configure(text=text,
                                 fg=color or COLORS["text_secondary"])

        def _fill_exif(m):
            """Push a metadata dict into the shooting-info entries."""
            exif_vars["make"].set(m.get("make") or "")
            exif_vars["model"].set(m.get("camera") or "")
            exif_vars["lens"].set(m.get("lens") or "")
            exif_vars["iso"].set(str(m.get("iso") or ""))
            exif_vars["shutter"].set(m.get("shutter") or "")
            exif_vars["aperture"].set(m.get("fnumber") or "")
            exif_vars["date"].set(_exif_datetime_str(m))

        def save_current():
            """Write rating/keywords/title + shooting-info diffs for the
            current image (unchanged shooting fields go out as None)."""
            if not state["seq"]:
                return True
            p = state["seq"][state["idx"]]
            m0 = state["meta"].get(p, {})

            def _arg(name, cur):
                v = exif_vars[name].get().strip()
                return v if v != cur else None

            ok, msg, revert, entry = self._review_save(
                p, rating=state["rating"],
                keywords=keywords_var.get(),
                title=title_var.get(),
                make=_arg("make", (m0.get("make") or "").strip()),
                model=_arg("model", (m0.get("camera") or "").strip()),
                lens=_arg("lens", (m0.get("lens") or "").strip()),
                iso=_arg("iso", str(m0.get("iso") or "").strip()),
                shutter=_arg("shutter", (m0.get("shutter") or "").strip()),
                aperture=_arg("aperture",
                              (m0.get("fnumber") or "").strip()),
                date=_arg("date", _exif_datetime_str(m0)))
            if not ok:
                set_status(msg, COLORS["danger"])
                return False
            m = state["meta"].get(p, {})
            m["rating"] = state["rating"]
            m["keywords"] = [k for k
                             in keywords_var.get().strip().split(",")
                             if k.strip()]
            m["title"] = title_var.get().strip()
            m["make"] = exif_vars["make"].get().strip()
            m["camera"] = exif_vars["model"].get().strip()
            m["lens"] = exif_vars["lens"].get().strip()
            m["iso"] = exif_vars["iso"].get().strip()
            m["shutter"] = exif_vars["shutter"].get().strip()
            m["fnumber"] = exif_vars["aperture"].get().strip()
            dt = exif_vars["date"].get().strip().replace(" ", ":").split(":")
            m["date"] = "-".join(dt[:3]) if len(dt) >= 3 else ""
            m["time"] = "-".join(dt[3:6]) if len(dt) >= 6 else ""
            if revert is not None:
                # dialog-scoped undo for THIS image (⌘Z in the lightbox)
                state["reverts"].setdefault(p, []).append((entry, revert))
            if msg:
                set_status(self._t("review_saved") + " · " + msg,
                           COLORS["accent"])
            return True

        def undo_current():
            """⌘Z / Undo button in the lightbox: revert the latest save
            on the current image and refresh the display from disk."""
            if not state["seq"]:
                return
            p = state["seq"][state["idx"]]
            stack = state["reverts"].get(p, [])
            if not stack:
                set_status(self._t("undo_none"),
                           COLORS["text_secondary"])
                return
            entry, revert = stack.pop()
            try:
                if entry in self._undo_stack:
                    self._undo_stack.remove(entry)  # keep LIFO coherent
                self._sync_undo_btn()
                revert()
            except Exception as e:
                set_status(self._t("undo_failed", err=str(e)),
                           COLORS["danger"])
                return
            try:
                m = read_exif_metadata(p)
                state["meta"][p] = m
            except Exception:
                m = state["meta"].get(p, {})
            state["rating"] = m.get("rating")
            keywords_var.set(",".join(m.get("keywords") or []))
            title_var.set(m.get("title") or "")
            _fill_exif(m)
            _restyle_rating()
            set_status(self._t("undo_done"), COLORS["accent"])

        def _restyle_rating():
            for n, btn in rating_btns.items():
                active = (state["rating"] is not None
                          and n == state["rating"])
                btn.configure(
                    bg=COLORS["accent"] if active else COLORS["card"],
                    fg="white" if active else COLORS["text"],
                    border_color=COLORS["accent"] if active
                    else COLORS["border"])

        def show():
            if not state["seq"]:
                img_lbl.configure(image="", text=self._t("review_empty"))
                info_lbl.configure(text="")
                pos_lbl.configure(text="0 / 0")
                prev_btn.configure(state="disabled")
                next_btn.configure(state="disabled")
                return
            idx = state["idx"]
            p = state["seq"][idx]
            try:
                # keep the dialog honest: re-read from disk so undo /
                # external CLI writes show up on the next navigation
                m = read_exif_metadata(p)
                state["meta"][p] = m
            except Exception:
                m = state["meta"].get(p, {})
            state["rating"] = m.get("rating")
            keywords_var.set(",".join(m.get("keywords") or []))
            title_var.set(m.get("title") or "")
            _fill_exif(m)
            pos_lbl.configure(text=self._t("review_pos", i=idx + 1,
                                           n=len(state["seq"])))
            parts = []
            if m.get("date"):
                parts.append(m["date"])
            if m.get("camera"):
                parts.append(m["camera"])
            if m.get("iso"):
                parts.append("ISO " + str(m["iso"]))
            if m.get("focal"):
                parts.append(str(m["focal"]))
            # v2.4 WYSIWYG: photos with a develop overlay / masks render
            # through the same pipeline export uses — the lightbox shows
            # what will actually come out, never a bare original
            has_adj = (p in self._photo_adjust
                       or p in (self._photo_masks or {}))
            if has_adj:
                parts.append(self._t("export_adjusted_badge"))
            info_lbl.configure(text="  |  ".join(parts))
            prev_btn.configure(
                state="normal" if idx > 0 else "disabled")
            next_btn.configure(
                state="normal" if idx < len(state["seq"]) - 1
                else "disabled")
            _restyle_rating()
            state["photo"] = None
            try:
                from PIL import Image, ImageTk
                img = _open_image_safe(p).convert("RGB")
                img.thumbnail((900, 540), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=img_lbl)
                img_lbl.configure(image=photo, text="")
                state["photo"] = photo  # keep the reference alive
            except Exception as e:
                img_lbl.configure(image="",
                                  text=os.path.basename(p) + "\n" + str(e))
            if has_adj:
                state["adj"] = (state.get("adj", 0) + 1)

                def _deliver(result, err, _tok=state["adj"]):
                    if not host.winfo_exists() or _tok != state.get("adj"):
                        return  # dialog closed / navigated away
                    if not result:
                        return  # keep the original on render failure
                    try:
                        from PIL import Image, ImageTk
                        img = _open_image_safe(result.output_path)
                        img = img.convert("RGB")
                        img.thumbnail((900, 540), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img, master=img_lbl)
                        img_lbl.configure(image=photo, text="")
                        state["photo"] = photo
                    except Exception:
                        pass  # rendered file unreadable — original stays

                self._render_adjusted_async(p, _deliver)

        def set_rating(n):
            if not state["seq"]:
                return
            state["rating"] = n
            _restyle_rating()
            save_current()

        def go(delta):
            if not state["seq"]:
                return
            save_current()
            new_idx = state["idx"] + delta
            if 0 <= new_idx < len(state["seq"]):
                state["idx"] = new_idx
                show()

        def apply_filter():
            save_current()
            min_r = int(min_rating_var.get() or 0)
            kw = filter_var.get().strip().lower()
            seq = []
            for p in all_paths:
                m = state["meta"].get(p, {})
                if (m.get("rating") or 0) < min_r:
                    continue
                if kw:
                    kws = [k.lower() for k in (m.get("keywords") or [])]
                    if not any(kw in k for k in kws):
                        continue
                seq.append(p)
            state["seq"] = seq
            state["idx"] = 0
            show()

        def clear_filter():
            filter_var.set("")
            min_rating_var.set("0")
            state["seq"] = list(all_paths)
            state["idx"] = 0
            show()

        def on_close():
            save_current()
            if hosted:
                self._exit_library_lightbox()
            else:
                host.destroy()

        if not hosted:
            # 弹窗键绑定；内嵌模式的 ←/→/0-5/Esc/⌘Z 走 root 路由 +
            # _lib_lightbox_active 守卫（_lib_lightbox_key / _lib_key_rate /
            # _on_global_escape / _undo）
            def _focus_in_input():
                w = host.focus_get()
                return isinstance(w, (ttk.Entry, ttk.Combobox, tk.Entry))

            host.bind("<Left>",
                      lambda e: go(-1) if not _focus_in_input() else None)
            host.bind("<Right>",
                      lambda e: go(1) if not _focus_in_input() else None)
            for n in range(6):
                host.bind(str(n), lambda e, n=n: (
                    set_rating(n) if not _focus_in_input() else None))
            host.bind("<Escape>", lambda e: on_close())
            host.bind("<Command-z>", lambda e: undo_current())
            host.bind("<Control-z>", lambda e: undo_current())

        def scan_thread():
            try:
                def cb(cur, total):
                    schedule(lambda: set_status(
                        self._t("review_loading", n=cur, total=total)))

                meta = self._review_scan(all_paths, progress_cb=cb)
            except Exception as e:
                schedule(lambda err=str(e): set_status(
                    self._t("op_failed", err=err), COLORS["danger"]))
                return
            schedule(lambda: _scanned(meta))

        def _scanned(meta):
            if not host.winfo_exists():
                return
            state["meta"] = meta
            state["seq"] = list(all_paths)
            state["idx"] = initial_idx
            set_status("")
            show()

        if not hosted:
            host.protocol("WM_DELETE_WINDOW", on_close)
        threading.Thread(target=scan_thread, daemon=True).start()

        return {"close": save_current, "set_rating": set_rating,
                "go": go, "undo": undo_current}

    def _after_file_dialog(self, btn=None):
        """Work around the macOS Tk native-file-dialog focus bug: after
        the dialog closes, Tk can re-deliver the closing click into the
        window (re-opening the dialog on the next click anywhere) and
        leaves the button stuck in its hover fill (the Leave event was
        eaten by the dialog). Reset the hover look and gate dialog
        re-entry for a short cooldown."""
        if btn is not None:
            btn._on_leave(None)
        self._dlg_guard_until = time.monotonic() + 0.4

    def _dlg_cooldown_active(self) -> bool:
        return time.monotonic() < getattr(self, "_dlg_guard_until", 0.0)

    def _add_files(self):
        """Open file dialog to add image files."""
        if self._dlg_cooldown_active():
            return
        extensions = []
        for ext in sorted(INPUT_EXTENSIONS):
            extensions.append(f"*{ext}")
            extensions.append(f"*{ext.upper()}")

        filetypes = [
            ("All Images", extensions),
            ("All Images + RAW", extensions + ["*.cr2", "*.CR2", "*.nef",
             "*.NEF", "*.arw", "*.ARW", "*.dng", "*.DNG", "*.orf", "*.ORF",
             "*.rw2", "*.RW2", "*.raf", "*.RAF", "*.pef", "*.PEF"]),
            ("JPEG", "*.jpg *.jpeg *.JPG *.JPEG"),
            ("PNG", "*.png *.PNG"),
            ("WebP", "*.webp *.WEBP"),
            ("HEIC", "*.heic *.heif *.HEIC *.HEIF"),
            ("RAW", "*.cr2 *.CR2 *.nef *.NEF *.arw *.ARW *.dng *.DNG *.orf *.ORF *.rw2 *.RW2 *.raf *.RAF"),
            ("All Files", "*.*"),
        ]

        paths = filedialog.askopenfilenames(
            title=self._t("add_images"),
            filetypes=filetypes,
        )
        self._after_file_dialog(self.add_files_btn)

        if paths:
            added = self._append_files(list(paths))
            if added == 0 and self._last_skipped:
                messagebox.showinfo(
                    self._t("dlg_no_images_title"),
                    self._t("dlg_no_supported", m=self._last_skipped))
            elif self._last_skipped:
                messagebox.showinfo(
                    self._t("dlg_added_title"),
                    self._t("dlg_skipped", n=added, m=self._last_skipped))

    def _add_folder(self):
        """Open folder dialog and scan for images (recursively)."""
        if self._dlg_cooldown_active():
            return
        folder = filedialog.askdirectory(title=self._t("add_folder"))
        self._after_file_dialog(self.add_folder_btn)
        if folder:
            images = scan_directory(folder, recursive=True)
            skipped = self._count_unsupported(folder)
            if not images:
                if skipped:
                    messagebox.showinfo(
                        self._t("dlg_no_images_title"),
                        self._t("dlg_no_supported", m=skipped))
                else:
                    messagebox.showinfo(
                        self._t("dlg_no_images_title"),
                        self._t("dlg_no_images"))
                return
            added = self._append_files(images)
            if added == 0:
                messagebox.showinfo(
                    self._t("dlg_no_images_title"),
                    self._t("dlg_no_images"))
                return
            if skipped:
                messagebox.showinfo(
                    self._t("dlg_added_title"),
                    self._t("dlg_skipped", n=added, m=skipped))
            else:
                messagebox.showinfo(
                    self._t("dlg_added_title"),
                    self._t("dlg_added", n=added))

    def _total_size(self) -> int:
        """Total size of listed files, ignoring files that no longer exist."""
        total = 0
        for f in self.files:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        return total

    def _checked_files(self) -> List[str]:
        """Checked files in list order (the set all workflow actions use)."""
        return [p for p in self.files if p in self._checked]

    def _on_thumb_size(self, _event=None):
        """Rebuild the file list at a new thumbnail size (cache keyed on
        size via the row rebuild; cached PIL images are re-fitted)."""
        idx = self.thumb_size_combo.current()
        new = {0: 48, 1: 96, 2: 144}.get(idx, 96)
        if new != self._thumb_size:
            self._thumb_size = new
            self._thumb_cache.clear()  # re-fit to the new size
            self._refresh_file_list()

    def _update_count_label(self):
        """File-count label in the toolbar (shows the checked subset
        when only some files are checked)."""
        if self._checked and len(self._checked) < len(self.files):
            self.file_count_label.config(text=self._t(
                "files_count_checked", n=len(self.files),
                m=len(self._checked)))
        else:
            self.file_count_label.config(
                text=self._t("files_count", n=len(self.files)))

    def _make_check_cb(self, path):
        """Deprecated seam kept for compatibility — checkboxes are canvas
        items in the v2.4 VirtualGrid; state toggles go through
        _toggle_check, which redraws the affected window."""

        def on_toggle():
            self._toggle_check(path)

        return on_toggle

    def _toggle_check(self, path):
        """Toggle one row's checkbox (programmatic / test seam).
        v2.4 VirtualGrid: state lives in _checked; the canvas redraw
        reflects it — no per-row widget to sync."""
        if path in self._checked:
            self._checked.discard(path)
        else:
            self._checked.add(path)
        self._update_count_label()
        self._lib_draw()

    def _push_undo(self, label, run, redo=None):
        """Record a reversible action (label for display, run restores
        the previous state, optional redo re-applies it). Returns the
        entry so callers can remove it from the stack again (e.g.
        in-lightbox undo)."""
        entry = {"label": label, "run": run, "redo": redo}
        self._undo_stack.append(entry)
        if len(self._undo_stack) > self._undo_max:
            self._undo_stack.pop(0)
        # A new action invalidates undone work: replaying stale redo
        # closures against changed state could delete the wrong rows.
        self._redo_stack.clear()
        self._sync_undo_btn()
        self._sync_redo_btn()
        return entry

    def _sync_undo_btn(self):
        btn = getattr(self, "undo_btn", None)
        if btn is not None:
            state = ("normal" if self._undo_stack
                     and not self.processing else "disabled")
            btn.configure(state=state)

    def _undo(self):
        """Pop and run the latest undo entry; push its redo counterpart.
        In the Develop module, per-photo adjustment history wins over the
        global file-action stack (v2.2); the hosted mask editor and the
        Library lightbox (v2.5) win over both while active."""
        if getattr(self, "_dev_mask_mode", False) and self._dev_mask_ctl:
            self._dev_mask_ctl["undo"]()
            return
        if getattr(self, "_lib_lightbox_active", False) \
                and self._lib_lightbox_ctl:
            self._lib_lightbox_ctl["undo"]()
            return
        if self._dev_undo_available():
            self._dev_undo()
            return
        if not self._undo_stack:
            messagebox.showinfo(self._t("undo"), self._t("undo_none"))
            return
        entry = self._undo_stack.pop()
        if entry.get("redo"):
            self._redo_stack.append(
                {"label": entry["label"], "run": entry["redo"]})
            if len(self._redo_stack) > self._undo_max:
                self._redo_stack.pop(0)
        try:
            entry["run"]()
        except Exception as e:
            messagebox.showerror(self._t("undo"),
                                 self._t("undo_failed", err=str(e)))
        self._sync_undo_btn()
        self._sync_redo_btn()

    def _redo(self):
        """Re-apply the most recently undone action (when a redo closure
        was recorded). Develop-module per-photo history takes precedence."""
        if self._dev_redo_available():
            self._dev_redo()
            return
        if not self._redo_stack:
            messagebox.showinfo(self._t("redo"), self._t("redo_none"))
            return
        entry = self._redo_stack.pop()
        self._undo_stack.append(entry)
        try:
            entry["run"]()
        except Exception as e:
            messagebox.showerror(self._t("redo"),
                                 self._t("undo_failed", err=str(e)))
        self._sync_undo_btn()
        self._sync_redo_btn()

    def _redo_remove(self, paths, checked):
        """Re-apply a list removal (redo of _remove_selected / cull)."""
        ps = set(p for _i, p in paths)
        self.files = [f for f in self.files if f not in ps]
        self._checked -= checked
        self._refresh_file_list()
        self._update_stats()

    def _per_file_overlay(self, path: str, opts: ProcessOptions):
        """Per-photo injections for batch processing (LR-style workflow):
        develop overlays from copy/paste (v2.2) plus the mask editor's
        per-photo masks. Returns the effective options for ``path``."""
        from dataclasses import replace
        edits = self._photo_adjust.get(path)
        pm = (self._photo_masks or {}).get(path)
        if not edits and not pm:
            return opts
        kw = dict(edits) if edits else {}
        if pm:
            kw["masks"] = pm.get("masks", opts.masks)
            kw["mask_adjust"] = pm.get("mask_adjust", opts.mask_adjust)
        return replace(opts, **kw)

    def _render_adjusted_async(self, path, deliver, include_masks=True):
        """v2.4 WYSIWYG: render ``path`` through its per-photo overlay
        (+ masks) — the same injection batch export uses — in a worker
        thread. ``deliver(result_or_None, err_text)`` runs on the Tk
        thread via the dev bus. ``include_masks=False`` renders the tone
        adjustments only (mask editors blend their own live overlays on
        top, so baking masks into the base would double-apply and go
        stale while dragging)."""
        base_opts = self._build_options()   # reads tk vars — Tk thread only
        tempdir = tempfile.mkdtemp(prefix="photos_wysiwyg_")
        self._preview_tempdirs.add(tempdir)

        def work():
            result, err = None, None
            try:
                opts = self._per_file_overlay(path, base_opts)
                if not include_masks:
                    from dataclasses import replace as _replace
                    opts = _replace(opts, masks="", mask_adjust="")
                result = workflows.preview_render(
                    path, workflows.preview_options(opts, tempdir))
                if not (result and getattr(result, "success", False)
                        and result.output_path):
                    err = getattr(result, "error", None) or "render failed"
                    result = None
            except Exception as e:
                result, err = None, str(e)
            self._dev_bus.schedule(lambda: deliver(result, err))

        threading.Thread(target=work, daemon=True,
                         name="photos-wysiwyg").start()

    def _sync_redo_btn(self):
        btn = getattr(self, "redo_btn", None)
        if btn is None:
            return
        try:
            if self._redo_stack:
                btn.configure(state="normal")
            else:
                btn.configure(state="disabled")
        except tk.TclError:
            pass

    def _restore_removed(self, pairs, checked):
        """Undo a list removal: re-insert the rows at their original
        positions with their check state."""
        for idx, p in sorted(pairs, reverse=True):
            if os.path.exists(p) and p not in self.files:
                self.files.insert(min(idx, len(self.files)), p)
        if checked:
            self._checked.update(checked)
        self._refresh_file_list()
        self._update_stats()

    def _restore_dedup(self, moved_map):
        """Undo a dedup trash move: rename the files back to their
        original locations and return them to the list (unchecked, as
        they were)."""
        restored = []
        for original, trash_dest in moved_map.items():
            if not os.path.exists(trash_dest):
                continue  # already gone (user cleaned the trash)
            if os.path.exists(original):
                continue  # original spot taken — leave the file in trash
            os.rename(trash_dest, original)
            restored.append(original)
        for p in restored:
            if p not in self.files:
                self.files.append(p)
        if restored:
            self._refresh_file_list()
            self._update_stats()

    def _toggle_all_checks(self):
        """Uncheck all when everything is checked, else check all."""
        if self._checked and len(self._checked) == len(self.files):
            self._checked.clear()
        else:
            self._checked = set(self.files)
        self._refresh_file_list()

    def _select_row(self, path):
        """Click on a row toggles its ephemeral selection highlight
        (remove/analyze/compare scope — distinct from the checkbox)."""
        if path in self._selected_rows:
            self._selected_rows.discard(path)
        else:
            self._selected_rows.add(path)
        self._highlight_row(path)

    def _highlight_row(self, path):
        """Apply/clear the selection highlight (v2.4: the canvas draws the
        selection outline from _selected_rows — one redraw, no widgets)."""
        self._lib_draw()

    def _remove_selected(self):
        """Remove the selected rows from the list (exact by full path,
        so same-name files in different folders never collide)."""
        selected = list(self._selected_rows)
        if not selected:
            return

        remove_paths = set(selected)
        self._selected_rows -= remove_paths
        removed = [(i, f) for i, f in enumerate(self.files)
                   if f in remove_paths]
        was_checked = remove_paths & self._checked
        self.files = [f for f in self.files if f not in remove_paths]
        self._checked -= remove_paths
        self._refresh_file_list()
        self._update_stats()
        self._push_undo(
            self._t("undo_removed", n=len(removed)),
            lambda: self._restore_removed(list(removed), set(was_checked)),
            lambda: self._redo_remove(list(removed), set(was_checked)))

    def _count_unsupported(self, directory) -> int:
        """Count non-hidden files under ``directory`` that PhotoS cannot
        read (anything outside engine.ALL_INPUT_EXTENSIONS)."""
        count = 0
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                if os.path.splitext(f)[1].lower() not in ALL_INPUT_EXTENSIONS:
                    count += 1
        return count

    def _append_files(self, new_paths: List[str]) -> int:
        """Dedupe-append paths to the file list (queued when processing).

        Unsupported files are skipped (counted into ``self._last_skipped``
        for the caller to notify). Returns the number of newly added paths.
        """
        self._last_skipped = 0
        added = 0
        fresh = []
        for p in new_paths:
            if os.path.isdir(p):
                # recursive: a folder may hold the photos in subfolders
                self._last_skipped += self._count_unsupported(p)
                for img in scan_directory(p, recursive=True):
                    if img not in self.files:
                        self.files.append(img)
                        fresh.append(img)
                        added += 1
            elif os.path.isfile(p):
                if os.path.splitext(p)[1].lower() not in ALL_INPUT_EXTENSIONS:
                    self._last_skipped += 1
                    continue
                if p not in self.files:
                    self.files.append(p)
                    fresh.append(p)
                    added += 1

        if fresh:
            # new files start checked (default: process everything added)
            self._checked.update(fresh)
        if added:
            self._refresh_file_list()
            self._update_stats()
            if self.processing:
                # Batch in flight: queue fresh files for an automatic follow-up
                for p in fresh:
                    if p not in self._queued_files:
                        self._queued_files.append(p)
        return added

    def _on_drop(self, event):
        """Handle drag-and-drop of files/folders onto the file list."""
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = event.data.split()

        if paths:
            added = self._append_files(list(paths))
            if added == 0:
                if self._last_skipped:
                    messagebox.showinfo(
                        self._t("dlg_no_images_title"),
                        self._t("dlg_no_supported", m=self._last_skipped))
                else:
                    messagebox.showinfo(
                        self._t("dlg_no_images_title"),
                        self._t("dlg_drop_none"))
            elif self._last_skipped:
                messagebox.showinfo(
                    self._t("dlg_added_title"),
                    self._t("dlg_skipped", n=added, m=self._last_skipped))

    def _clear_files(self):
        """Clear all files from the list."""
        if self.files and messagebox.askyesno(
            self._t("dlg_confirm_clear_title"),
            self._t("dlg_confirm_clear", n=len(self.files)),
        ):
            self.files.clear()
            self._checked.clear()
            self._refresh_file_list()
            self._update_stats()

    # ── v2.4 Library VirtualGrid: fixed-row-height canvas list ─────────────

    def _lib_row_h(self) -> int:
        return self._thumb_size + 8

    def _refresh_file_list(self):
        """Rebuild the list MODEL, then draw only the visible window
        (v2.4 VirtualGrid). Checkbox/selection state lives in
        self._checked / self._selected_rows and is read at draw time, so
        toggles redraw one pass instead of rebuilding rows of widgets."""
        self._lib_model = []
        for path in self._visible_files():
            name = os.path.basename(path)
            try:
                st = os.stat(path)
                size = format_size(st.st_size)
                cache_key = (path, st.st_size, st.st_mtime)
                dims = self._dims_cache.get(cache_key)
                if dims is None:
                    from PIL import Image
                    if Path(path).suffix.lower() in RAW_EXTENSIONS:
                        # header-only read — decoding every RAW for a
                        # dims column would freeze the list
                        import rawpy
                        with rawpy.imread(path) as raw:
                            dims = (f"{raw.sizes.width}"
                                    f"×{raw.sizes.height}")
                    else:
                        with Image.open(path) as img:
                            dims = f"{img.width}×{img.height}"
                    self._dims_cache[cache_key] = dims
            except (OSError, Exception):
                size, dims = "N/A", "—"
                # stat failed (file vanished mid-listing): a stable key
                # keeps the row drawable — it just never gets a thumbnail
                cache_key = (path, 0, 0.0)
            fmt = Path(path).suffix.upper().lstrip(".")
            self._lib_model.append({
                "path": path, "name": name, "size": size, "fmt": fmt,
                "dims": dims, "cache_key": cache_key,
            })
        self._selected_rows &= set(self.files)  # drop gone paths
        self._lib_draw()
        self._update_count_label()
        self._dev_refresh_filmstrip()
        self._refresh_export_queue()

    def _lib_yscroll(self, first, last):
        """yscrollcommand shim: update the scrollbar always; redraw only
        when a DIFFERENT window of rows must materialize. Rows are drawn
        at absolute y coordinates, so the canvas scrolls them natively —
        and an unconditional draw here would ping-pong forever, because
        Tk re-fires yscrollcommand after every scrollregion update (the
        draw sets scrollregion)."""
        sb = getattr(self, "_lib_scrollbar", None)
        if sb is not None:
            try:
                sb.set(first, last)
            except tk.TclError:
                pass
        if not self._lib_model:
            return
        try:
            top = float(first)
        except (TypeError, ValueError):
            return
        n = len(self._lib_model)
        first_idx = max(0, int(top * n) - 2)  # same margin as _lib_draw
        if first_idx != self._lib_visible[0]:
            self._lib_draw()

    def _lib_draw(self):
        """Materialize the visible rows as canvas items (culling: only
        rows intersecting the viewport are drawn; the data model above is
        the full list). Re-entrant-safe — scrollregion updates during a
        draw would recurse through yscrollcommand otherwise."""
        if getattr(self, "_lib_drawing", False):
            return
        canvas = getattr(self, "file_list_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        self._lib_drawing = True
        try:
            self._lib_draw_inner()
        finally:
            self._lib_drawing = False

    def _lib_draw_inner(self):
        from PIL import Image, ImageTk
        canvas = self.file_list_canvas
        canvas.delete("all")
        self._lib_photos = []
        n = len(self._lib_model)
        rh = self._lib_row_h()
        w = max(canvas.winfo_width(), 200)
        total = n * rh
        canvas.configure(scrollregion=(0, 0, w, max(total, 1)))
        if not n:
            return
        top_frac = canvas.yview()[0]
        y0 = top_frac * total
        ch = max(canvas.winfo_height(), rh)
        first = max(0, int(y0 // rh) - 2)
        last = min(n, int((y0 + ch) // rh) + 3)
        self._lib_visible = (first, last)
        thumb = self._thumb_size
        todo = []
        for i in range(first, last):
            row = self._lib_model[i]
            path = row["path"]
            y = i * rh
            bg = COLORS["row_alt"] if i % 2 else COLORS["card"]
            canvas.create_rectangle(0, y, w, y + rh, fill=bg,
                                    width=0, tags=("row:%d" % i,))
            if path in self._selected_rows:
                canvas.create_rectangle(
                    1, y + 1, w - 1, y + rh - 1, outline=COLORS["accent"],
                    width=1, tags=("row:%d" % i,))
            # checkbox (drawn; state read live from _checked)
            cy = y + rh // 2
            checked = path in self._checked
            canvas.create_rectangle(
                12, cy - 7, 26, cy + 7, fill=COLORS["card"],
                outline=(COLORS["accent"] if checked
                         else COLORS["border"]),
                width=1, tags=("cb:%d" % i,))
            if checked:
                canvas.create_text(19, cy, text="✓",
                                   fill=COLORS["accent"], font=FONT_SMALL)
            # thumbnail from the LRU cache (decode queued when missing)
            img = self._thumb_cache.get(row["cache_key"])
            if isinstance(img, Image.Image) and not isinstance(img, bool):
                photo = ImageTk.PhotoImage(img, master=canvas)
                self._lib_photos.append(photo)
                canvas.create_image(36, cy, image=photo, anchor="w")
            else:
                tx = 36 + thumb // 2
                canvas.create_rectangle(
                    36, cy - thumb // 2, 36 + thumb, cy + thumb // 2,
                    fill=COLORS["bg"], outline=COLORS["border"],
                    tags=("thumbph:%d" % i,))
                canvas.create_text(tx, cy, text="▦",
                                   fill=COLORS["text_secondary"])
                if img is None:
                    todo.append((i, row))
            # name (+ adjusted badge dot) — clips at the right columns
            nx = 36 + thumb + 10
            if path in self._photo_adjust:
                canvas.create_oval(nx, cy - 3, nx + 6, cy + 3,
                                   fill=COLORS["accent"], outline="")
                nx += 12
            canvas.create_text(
                nx, cy, text=row["name"], anchor="w", font=FONT_BODY,
                fill=COLORS["text"], width=max(80, w - nx - 340))
            # right-aligned columns: rating stars / dims / fmt / size
            rating = self._lib_rating(path)
            if rating:
                canvas.create_text(
                    w - 320, cy, text="★" * rating, anchor="e",
                    font=FONT_SMALL, fill=COLORS["accent"])
            canvas.create_text(w - 200, cy, text=row["dims"], anchor="e",
                               font=FONT_SMALL,
                               fill=COLORS["text_secondary"])
            canvas.create_text(w - 100, cy, text=row["fmt"], anchor="e",
                               font=FONT_SMALL,
                               fill=COLORS["text_secondary"])
            canvas.create_text(w - 14, cy, text=row["size"], anchor="e",
                               font=FONT_SMALL,
                               fill=COLORS["text_secondary"])
        if todo:
            self._lib_queue_thumbs(todo)

    def _lib_rating(self, path) -> int:
        """Rating for the row badge, cached (EXIF parsed once per path)."""
        cache = getattr(self, "_lib_rating_cache", None)
        if cache is None:
            cache = self._lib_rating_cache = {}
        if path not in cache:
            try:
                from ..engine import read_exif_metadata
                m = read_exif_metadata(path)
                cache[path] = int(m.get("rating") or 0)
            except Exception:
                cache[path] = 0
        return cache[path]

    def _lib_queue_thumbs(self, todo):
        """Queue visible-but-undecoded thumbnails, then pump a few per
        tick (RAW decodes run off the UI thread, like the old rows)."""
        q = getattr(self, "_lib_thumb_queue", None)
        if q is None:
            q = self._lib_thumb_queue = []
        have = {t[1]["cache_key"] for t in q}
        for item in todo:
            if item[1]["cache_key"] not in have:
                q.append(item)
        if q and not getattr(self, "_lib_thumb_after", None):
            self._lib_thumb_after = self.root.after(
                30, lambda: self._lib_pump_thumbs(3))

    def _lib_pump_thumbs(self, batch):
        self._lib_thumb_after = None
        q = getattr(self, "_lib_thumb_queue", None) or []
        done = 0
        while q and done < batch:
            i, row = q.pop(0)
            key = row["cache_key"]
            path = row["path"]
            if self._thumb_cache.get(key) is not None:
                continue  # already decoded meanwhile
            if key in self._thumb_decoding:
                q.append((i, row))  # RAW worker in flight — retry later
                continue
            if Path(path).suffix.lower() in RAW_EXTENSIONS:
                self._thumb_decoding.add(key)

                def _decode(p=path, k=key, s=self._thumb_size):
                    try:
                        self._thumb_cache[k] = self._decode_thumb_source(p, s)
                    except Exception:
                        self._thumb_cache[k] = False
                    finally:
                        self._thumb_decoding.discard(k)
                        try:
                            self.root.after(
                                0, lambda: self._lib_pump_thumbs(batch))
                        except tk.TclError:
                            pass  # window gone

                threading.Thread(target=_decode, daemon=True).start()
                continue
            try:
                self._thumb_cache[key] = self._decode_thumb_source(
                    path, self._thumb_size)
                done += 1
            except Exception:
                self._thumb_cache[key] = False
                done += 1
        if q:
            self._lib_thumb_after = self.root.after(
                30, lambda: self._lib_pump_thumbs(batch))
        else:
            self._lib_draw()  # land the last decoded batch

    def _lib_row_at(self, y) -> int:
        """Model index for a canvas y coordinate, or -1."""
        rh = self._lib_row_h()
        idx = int(y // rh)
        if 0 <= idx < len(self._lib_model):
            return idx
        return -1

    def _lib_click(self, event):
        idx = self._lib_row_at(event.y)
        if idx < 0:
            return
        path = self._lib_model[idx]["path"]
        if event.x <= 34:  # checkbox zone
            self._toggle_check(path)
            return
        self._select_row(path)

    def _lib_double_click(self, event):
        idx = self._lib_row_at(event.y)
        if idx >= 0:
            self._open_compare(self._lib_model[idx]["path"])

    def _lib_rate(self, n):
        """Keyboard rating on the selected rows (1-5 stars; P clears —
        the reject gesture, aligned with the review lightbox keys)."""
        if not self._selected_rows:
            return
        for path in list(self._selected_rows):
            try:
                self._review_save(path, rating=n)
            except Exception:
                pass  # read-only file / EXIF host without write support
        self._lib_rating_cache = {}
        self._lib_draw()

    def _lib_open_in_viewer(self):
        """Enter: send the first selected photo to the Develop viewer."""
        for path in self._selected_rows:
            self._show_module("develop")
            self._dev_select(path)
            return

    # ── v2.4: Library keyboard commands (root-bound, focus-guarded) ──────

    def _lib_keys_active(self) -> bool:
        """Keyboard commands fire only in the Library module and only
        while the focus is not in a text-ish widget (typing "3" into the
        filter box must never rate a photo)."""
        if getattr(self, "_active_module", "") != "library":
            return False
        return not self._focus_in_text_widget()

    def _lib_key_rate(self, n):
        # v2.5: 灯箱激活时 0-5 给灯箱当前照片（不是网格行）
        if getattr(self, "_lib_lightbox_active", False):
            if self._lib_lightbox_ctl is not None \
                    and not self._focus_in_text_widget():
                self._lib_lightbox_ctl["set_rating"](n)
            return
        if self._lib_keys_active():
            self._lib_rate(n)

    def _lib_key_open(self):
        if self._lib_keys_active():
            self._lib_open_in_viewer()

    def _lib_key_remove(self):
        if self._lib_keys_active() and self._selected_rows:
            self._remove_selected()

    def _lib_key_shortcuts(self, event=None):
        """? opens the shortcut table (any module; entries are guarded)."""
        w = None
        try:
            w = self.root.focus_get()
        except tk.TclError:
            pass
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox,
                          tk.Listbox, tk.Spinbox, ttk.Spinbox)):
            return
        self._show_shortcuts()

    def _show_shortcuts(self):
        """The keyboard shortcut table (v2.4)."""
        if self._dlg_cooldown_active():
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("shortcuts_title"))
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        win.geometry("+%d+%d" % (self.root.winfo_rootx() + 80,
                                 self.root.winfo_rooty() + 80))
        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(body, text=self._t("shortcuts_title"),
                 font=FONT_BODY, fg=COLORS["text"],
                 bg=COLORS["bg"]).pack(anchor="w", pady=(0, 8))
        tk.Label(body, text=self._t("shortcuts_text"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"],
                 justify="left", anchor="w").pack(anchor="w")
        FlatButton(
            body, text=self._t("close"),
            command=lambda: _close(), bg=COLORS["card"],
            fg=COLORS["text"], hover_bg=COLORS["bg"],
            border_color=COLORS["border"], font=FONT_SMALL,
            padx=14, pady=4).pack(anchor="e", pady=(12, 0))

        def _close():
            self._after_file_dialog()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        win.bind("<Escape>", lambda e: _close())

    def _settings_search(self):
        """v2.4: find settings by label text across the tabs — switch to
        the owning tab, highlight every hit, scroll the first one into
        view. An empty query restores the normal look."""
        marked = getattr(self, "_settings_search_marked", None) or []
        for lbl, bg, fg in marked:
            try:
                lbl.configure(bg=bg, fg=fg)
            except tk.TclError:
                pass  # panel rebuilt (theme/language) since highlighting
        self._settings_search_marked = []
        self._settings_search_hits = []
        q = (getattr(self, "_settings_search_var", None) or
             tk.StringVar()).get().strip().lower()
        if not q or not getattr(self, "_settings_tabs", None):
            return
        first_hit = None
        for tab, inner, module in self._settings_tabs:
            hits = []
            self._collect_label_hits(inner, q, hits)
            if not hits:
                continue
            if first_hit is None:
                first_hit = (tab, inner, module, hits[0])
            for lbl, bg, fg in hits:
                self._settings_search_marked.append((lbl, bg, fg))
                self._settings_search_hits.append(lbl.cget("text") or "")
        if first_hit is None:
            return
        tab, inner, module, lbl = first_hit
        if module == "develop":
            self._show_module("develop")
        elif tab is not None:
            self._settings_nb.select(tab)
        # scroll the first hit into view (inner's master is the canvas)
        try:
            canvas = inner.master
            total = canvas.bbox("all")[3] or 1
            canvas.yview_moveto(max(0.0, min(0.99, lbl.winfo_y() / total)))
        except (tk.TclError, AttributeError, IndexError):
            pass

    def _collect_label_hits(self, widget, q, out):
        """Recursive label matcher: collect (label, bg, fg) for labels
        whose visible text contains the query."""
        import tkinter as _tk
        for c in widget.winfo_children():
            if isinstance(c, _tk.Label):
                try:
                    t = (c.cget("text") or "").strip()
                except tk.TclError:
                    t = ""
                if t and q in t.lower() and t != "×":
                    out.append((c, c.cget("bg"), c.cget("fg")))
            self._collect_label_hits(c, q, out)

    # ── v2.4: first-run guide card ────────────────────────────────────────

    def _show_first_run_guide(self):
        """One-time quick-start card (persisted flag in gui_state)."""
        if getattr(self, "_first_run_done", False):
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        win = tk.Toplevel(self.root)
        win.title(self._t("guide_title"))
        win.configure(bg=COLORS["card"])
        win.transient(self.root)
        try:
            win.geometry("+%d+%d" % (self.root.winfo_rootx() + 120,
                                     self.root.winfo_rooty() + 120))
        except tk.TclError:
            pass
        body = tk.Frame(win, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=22, pady=18)
        tk.Label(body, text=self._t("guide_title"), font=FONT_SECTION,
                 fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w",
                                                            pady=(0, 10))
        tk.Label(body, text=self._t("guide_text"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"],
                 justify="left", anchor="w").pack(anchor="w")
        FlatButton(
            body, text=self._t("guide_start"), command=lambda: _close(),
            bg=COLORS["accent"], fg="white",
            hover_bg=COLORS["accent_hover"], border_color=COLORS["accent"],
            font=FONT_SMALL, padx=16, pady=5).pack(anchor="e",
                                                   pady=(14, 0))

        def _close():
            self._first_run_done = True
            self._save_gui_state()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        win.bind("<Escape>", lambda e: _close())


    def _visible_files(self):
        """Files matching the filter box (display-only view over self.files)."""
        q = self.filter_var.get().strip().lower()
        if not q:
            return list(self.files)
        return [p for p in self.files
                if q in os.path.basename(p).lower()
                or q in Path(p).suffix.lower().lstrip(".")]

    def _apply_filter(self, _event=None):
        """Rebuild the list for the current filter query."""
        self._refresh_file_list()

    def _clear_filter(self):
        self.filter_var.set("")
        self._refresh_file_list()

    def _decode_thumb_source(self, path: str, size: int):
        """Decode + downscale one thumbnail source (no Tk objects created).

        Runs on the UI thread for plain images and inside a worker for RAW
        files — a RAW postprocess used to run inline in the drain loop and
        froze the interface on every burst.
        """
        from PIL import Image
        if Path(path).suffix.lower() in RAW_EXTENSIONS:
            import rawpy
            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(use_camera_wb=True,
                                      half_size=True, output_bps=8)
                img = Image.fromarray(rgb)
        else:
            with Image.open(path) as im:
                img = im.convert("RGB").copy()
        img.thumbnail((size, size), Image.LANCZOS)
        return img

    def _browse_output_dir(self):
        """Browse for output directory."""
        if self._dlg_cooldown_active():
            return
        folder = filedialog.askdirectory(title=self._t("sec_output"))
        self._after_file_dialog()
        if folder:
            self.output_dir.set(folder)

    # ── Batch rename (live preview) ──────────────────────────────────────────

    def _rename_preview(self, paths, pattern, output_dir=None,
                        overwrite=False):
        """Sync: dry-run the rename and flag in-batch name collisions.

        rename_files(dry_run=True) only consults the filesystem, so two
        inputs mapping to the same target are both reported "ok" against
        the same path. Replay the real run here: earlier outputs occupy
        their names, and a colliding row is pushed through the same
        _unique_target loop the engine runs (quirks included — a target
        whose ``_1`` variant is also taken becomes ``name_1_2``, exactly
        like the real run) and marked "conflict". With overwrite=True no
        suffixing happens at all, so the dry-run rows stand as-is.

        Returns rows of {"input", "output", "status", "error"} with
        status "ok" | "conflict" | "error". Tk-free so tests can call it
        directly.
        """
        from ..rename import rename_files
        rows = rename_files(list(paths), pattern, output_dir=output_dir,
                            overwrite=overwrite, dry_run=True)
        if overwrite:
            return rows
        taken = set()
        for row in rows:
            if row["status"] != "ok" or not row["output"]:
                continue
            target = row["output"]
            # The dry-run output is filesystem-free by construction; the
            # exists() half only fires for case-insensitive-FS collisions
            # the exact-match `taken` set cannot see.
            if target not in taken and not os.path.exists(target):
                taken.add(target)
                continue
            p = Path(target)
            base = p
            counter = 1
            while str(p) in taken or p.exists():
                p = base.with_name("{}_{}{}".format(
                    base.stem, counter, base.suffix))
                counter += 1
            row["output"] = str(p)
            row["status"] = "conflict"
            row["error"] = self._t("rename_conflict_note", name=p.name)
            taken.add(str(p))
        return rows

    def _show_rename(self):
        """Batch rename with live preview: template + options on top, a
        Treeview mapping old -> new names (in-batch conflicts and errors
        flagged), refreshed by a debounced background dry-run on every
        change. Execute confirms, runs the real rename in a worker, then
        refreshes the main file list. Not wired into the undo stack."""
        from ..rename import rename_files

        files = self._checked_files()
        if not files:
            messagebox.showinfo(self._t("rename_title"),
                                self._t("check_none"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("rename_title"))
        win.geometry("880x560")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        canvas_unbind_safe(win)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="x", padx=20, pady=(16, 0))
        body.columnconfigure(2, weight=1)

        pattern_var = tk.StringVar(
            value=self.rename_pattern.get().strip() or "{date}_{seq}")
        mode_var = tk.StringVar(value="inplace")
        dir_var = tk.StringVar(value="")
        overwrite_var = tk.BooleanVar(value=False)

        tk.Label(body, text=self._t("rename_pattern_lbl"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 4))
        ttk.Entry(body, textvariable=pattern_var, font=FONT_BODY).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=(0, 4))
        tk.Label(body, text=self._t("rename_vars"), font=FONT_TINY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Radiobutton(body, text=self._t("rename_mode_inplace"),
                        variable=mode_var, value="inplace",
                        command=lambda: _options_changed()).grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        ttk.Radiobutton(body, text=self._t("rename_mode_copy"),
                        variable=mode_var, value="copy",
                        command=lambda: _options_changed()).grid(
            row=2, column=1, sticky="w", padx=(0, 10), pady=(0, 8))
        dir_entry = ttk.Entry(body, textvariable=dir_var, font=FONT_BODY)
        dir_entry.grid(row=2, column=2, sticky="ew", pady=(0, 8))

        def _browse_dir():
            if self._dlg_cooldown_active():
                return
            folder = filedialog.askdirectory(
                title=self._t("rename_mode_copy"))
            self._after_file_dialog()
            if folder:
                dir_var.set(folder)

        browse_btn = FlatButton(body, text=self._t("browse"),
                                command=_browse_dir,
                                bg=COLORS["card"], fg=COLORS["text"],
                                hover_bg=COLORS["bg"],
                                border_color=COLORS["border"],
                                font=FONT_SMALL)
        browse_btn.grid(row=2, column=3, sticky="w", padx=(8, 0),
                        pady=(0, 8))

        ttk.Checkbutton(body, text=self._t("rename_overwrite"),
                        variable=overwrite_var,
                        command=lambda: _queue_preview()).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 4))

        holder = tk.Frame(win, bg=COLORS["bg"])
        holder.pack(fill="both", expand=True, padx=20, pady=8)
        tree = ttk.Treeview(holder, columns=("old", "new", "status"),
                            show="headings", height=14)
        tree.heading("old", text=self._t("rename_col_old"))
        tree.heading("new", text=self._t("rename_col_new"))
        tree.heading("status", text=self._t("rename_col_status"))
        tree.column("old", width=300, anchor="w")
        tree.column("new", width=300, anchor="w")
        tree.column("status", width=220, anchor="w")
        tree.tag_configure("conflict", foreground=COLORS["warning"])
        tree.tag_configure("error", foreground=COLORS["danger"])
        sb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        footer = tk.Frame(win, bg=COLORS["bg"])
        footer.pack(fill="x", padx=20, pady=(4, 16))

        # Worker→UI marshalling queue (see dedup dialog for rationale)
        bus = UiBus(win)
        schedule = bus.schedule
        bus.start()

        state = {"after_id": None, "token": 0, "rows": []}

        def _status_text(row):
            if row["status"] == "conflict":
                return self._t("rename_status_conflict")
            if row["status"] == "error":
                return row["error"]
            return ""

        def _render_rows(rows):
            tree.delete(*tree.get_children())
            for row in rows:
                new = os.path.basename(row["output"]) \
                    if row["output"] else "—"
                tag = row["status"] \
                    if row["status"] in ("conflict", "error") else ""
                tree.insert("", "end", values=(
                    os.path.basename(row["input"]), new,
                    _status_text(row)), tags=(tag,) if tag else ())

        def _current_options():
            out = dir_var.get().strip() if mode_var.get() == "copy" else ""
            return pattern_var.get(), out or None, overwrite_var.get()

        def _queue_preview(*_):
            if not win.winfo_exists():
                return
            if state["after_id"] is not None:
                try:
                    win.after_cancel(state["after_id"])
                except tk.TclError:
                    pass
            state["after_id"] = win.after(300, _start_preview)

        def _start_preview():
            state["after_id"] = None
            if not win.winfo_exists():
                return
            pattern, out, ow = _current_options()
            if mode_var.get() == "copy" and not out:
                state["rows"] = []
                state["token"] += 1
                _render_rows([])
                status_lbl.configure(text=self._t("rename_need_dir"),
                                     fg=COLORS["warning"])
                execute_btn.configure(state="disabled")
                return
            execute_btn.configure(state="normal")
            state["token"] += 1
            token = state["token"]
            status_lbl.configure(text=self._t("rename_preview_updating"),
                                 fg=COLORS["text_secondary"])

            def run():
                try:
                    rows = self._rename_preview(files, pattern,
                                                output_dir=out,
                                                overwrite=ow)
                except Exception as e:
                    schedule(lambda err=str(e): _preview_failed(token, err))
                    return
                schedule(lambda: _preview_done(token, rows))

            threading.Thread(target=run, daemon=True).start()

        def _preview_failed(token, err):
            if not win.winfo_exists() or token != state["token"]:
                return
            status_lbl.configure(text=self._t("op_failed", err=err),
                                 fg=COLORS["danger"])

        def _preview_done(token, rows):
            if not win.winfo_exists() or token != state["token"]:
                return
            state["rows"] = rows
            _render_rows(rows)
            conflicts = sum(1 for r in rows if r["status"] == "conflict")
            errors = sum(1 for r in rows if r["status"] == "error")
            status_lbl.configure(
                text=self._t("rename_counts", n=len(rows), c=conflicts,
                             e=errors),
                fg=COLORS["warning"] if (conflicts or errors)
                else COLORS["text_secondary"])

        def _options_changed():
            copying = mode_var.get() == "copy"
            dir_entry.configure(state="normal" if copying else "disabled")
            browse_btn.configure(state="normal" if copying else "disabled")
            _queue_preview()

        def _execute():
            pattern, out, ow = _current_options()
            if mode_var.get() == "copy" and not out:
                messagebox.showinfo(self._t("rename_title"),
                                    self._t("rename_need_dir"))
                return
            if not messagebox.askyesno(
                    self._t("rename_title"),
                    self._t("rename_confirm", n=len(files))):
                return
            # conflict count from the last preview — the real run resolves
            # them via _N suffixes, so its own rows only know ok/error
            conflicts = sum(1 for r in state["rows"]
                            if r["status"] == "conflict")
            execute_btn.configure(state="disabled")

            def run():
                try:
                    results = rename_files(list(files), pattern,
                                           output_dir=out, overwrite=ow)
                except Exception as e:
                    schedule(lambda err=str(e): _execute_failed(err))
                    return
                schedule(lambda: _executed(results, conflicts))

            threading.Thread(target=run, daemon=True).start()

        def _execute_failed(err):
            if not win.winfo_exists():
                return
            execute_btn.configure(state="normal")
            status_lbl.configure(text=self._t("op_failed", err=err),
                                 fg=COLORS["danger"])

        def _executed(results, conflicts):
            if not win.winfo_exists():
                return
            execute_btn.configure(state="normal")
            ok = sum(1 for r in results if r["status"] == "ok")
            errors = sum(1 for r in results if r["status"] == "error")
            if mode_var.get() == "inplace":
                # keep the main list pointing at the renamed paths
                renamed = {r["input"]: r["output"] for r in results
                           if r["status"] == "ok" and r["output"]}
                if renamed:
                    self.files = [renamed.get(f, f) for f in self.files]
                    self._checked = {renamed.get(f, f)
                                     for f in self._checked}
                    files[:] = [renamed.get(f, f) for f in files]
            self._refresh_file_list()
            state["rows"] = results
            _render_rows(results)
            msg = self._t("rename_done", ok=ok, c=conflicts, e=errors)
            status_lbl.configure(text=msg, fg=COLORS["accent"])
            messagebox.showinfo(self._t("rename_title"), msg)

        def _on_close():
            if state["after_id"] is not None:
                try:
                    win.after_cancel(state["after_id"])
                except tk.TclError:
                    pass
                state["after_id"] = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

        execute_btn = FlatButton(
            footer, text=self._t("rename_execute"), command=_execute,
            bg=COLORS["accent"], hover_bg=COLORS["accent_hover"])
        execute_btn.pack(side="left")
        status_lbl = tk.Label(footer, text="", font=FONT_SMALL,
                              fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_lbl.pack(side="left", padx=(12, 0))
        FlatButton(
            footer, text=self._t("close"), command=_on_close,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            border_color=COLORS["border"]).pack(side="right")

        pattern_var.trace_add("write", lambda *_: _queue_preview())
        dir_var.trace_add("write", lambda *_: _queue_preview())
        _options_changed()          # syncs dir-entry state + first preview

    # ── Processing ───────────────────────────────────────────────────────────

    def _on_folder_preset_change(self, event=None):
        """When a folder preset is selected from the combobox."""
        idx = self.folder_combo.current()
        if idx < 0 or idx >= len(self._folder_preset_values):
            return
        value = self._folder_preset_values[idx]
        if value is None:  # custom template
            self.folder_pattern.set("")
            self.folder_custom_entry.focus_set()
        else:
            self.folder_pattern.set(value)

    def _cutout_spec(self) -> Optional[str]:
        """Build the cutout spec string from the UI vars (None when off).

        The mode var holds a localized label — map back via _t. A color
        mode with unparseable R/G/B degrades to None (like size parsing).
        """
        mode = self.cutout_mode.get()
        if not mode or mode == self._t("cutout_off"):
            return None
        if mode == self._t("cutout_subject"):
            return "subject"
        if mode == self._t("cutout_person"):
            return "person"
        if mode == self._t("cutout_object"):
            label = self.cutout_label.get().strip()
            return f"object:{label}" if label else None
        if mode == self._t("cutout_color"):
            rgb = []
            for v in (self.cutout_r.get(), self.cutout_g.get(),
                      self.cutout_b.get()):
                try:
                    rgb.append(max(0, min(255, int(float(v)))))
                except ValueError:
                    return None
            try:
                tol = float(self.cutout_tol.get())
            except ValueError:
                tol = 30.0
            try:
                feather = float(self.cutout_feather.get())
            except ValueError:
                feather = 0.0
            return (f"color:{rgb[0]},{rgb[1]},{rgb[2]},"
                    f"tol={tol},feather={feather}")
        return None

    def _build_options(self) -> ProcessOptions:
        """Build ProcessOptions from current UI state."""
        try:
            max_w = int(self.max_width.get()) if self.max_width.get().strip() else None
        except (ValueError, TypeError):
            max_w = None
        try:
            max_h = int(self.max_height.get()) if self.max_height.get().strip() else None
        except (ValueError, TypeError):
            max_h = None
        try:
            scale = int(self.scale_percent.get()) if self.scale_percent.get().strip() else None
        except (ValueError, TypeError):
            scale = None
        try:
            max_pixels = int(self.max_pixels.get()) if self.max_pixels.get().strip() else None
        except (ValueError, TypeError):
            max_pixels = None

        # Parse target size if in target size mode
        target_bytes = None
        if self.target_size_mode.get():
            try:
                val = float(self.target_size_value.get())
                unit = self.target_size_unit.get()
                multiplier = 1024 if unit == "KB" else 1024**2
                target_bytes = int(val * multiplier)
            except ValueError:
                pass  # Invalid input → ignore target size

        # Parse jobs (parallel workers): clamp to >= 1
        jobs_str = self.jobs.get().strip()
        try:
            jobs = max(1, int(jobs_str))
        except ValueError:
            jobs = 1

        def _to_float(value, default):
            try:
                return float(value)
            except (ValueError, TypeError):
                return default

        from ..cli import _parse_sizes  # lazy: cli imports engine only

        # Invalid sizes input degrades to None instead of raising — this
        # runs on the preview drain tick and before processing starts, so
        # a ValueError here would kill the drain / wedge the app.
        try:
            output_sizes = _parse_sizes(self.output_sizes.get().strip()
                                        or None)
        except ValueError:
            output_sizes = None

        return ProcessOptions(
            quality=self.quality.get(),
            output_format=self.output_format.get(),
            output_dir=self.output_dir.get() if self.output_dir.get().strip() else None,
            max_width=max_w,
            max_height=max_h,
            scale_percent=scale,
            preserve_exif=self.preserve_exif.get(),
            optimize=self.optimize.get(),
            progressive=self.progressive.get(),
            jpeg_subsampling=self.jpeg_subsampling.get(),
            overwrite=self.overwrite.get(),
            prefix=self.prefix.get(),
            suffix=self.suffix.get(),
            target_size_bytes=target_bytes,
            raw_half_size=self.raw_half_size.get(),
            raw_auto_bright=self.raw_auto_bright.get(),
            raw_demosaic=self.raw_demosaic.get(),
            raw_color_space=self.raw_color_space.get(),
            raw_16bit=self.raw_16bit.get(),
            auto_rotate=self.auto_rotate.get(),
            remove_original=self.remove_original.get(),
            strip_gps=self.strip_gps.get(),
            keep_mtime=self.keep_mtime.get(),
            max_pixels=max_pixels,
            brightness=_to_float(self.brightness.get(), 1.0),
            contrast=_to_float(self.contrast.get(), 1.0),
            saturation=_to_float(self.saturation.get(), 1.0),
            gamma=_to_float(self.gamma.get(), 1.0),
            sharpen=_to_float(self.sharpen.get(), 1.0),
            export_sharpen=_to_float(self.export_sharpen.get(), 0.0) or None,
            grayscale=self.grayscale.get(),
            sepia=self.sepia.get(),
            ev=_to_float(self.ev.get(), 0.0),
            auto_exposure=_to_float(self.auto_exposure.get(), None)
            if self.auto_exposure.get().strip() else None,
            log_curve=self.log_curve.get() or None,
            denoise=_to_float(self.denoise.get(), 0.0)
            if self.denoise.get().strip() else None,
            lut_file=self.lut_file.get().strip() or None,
            auto_straighten=self.auto_straighten.get(),
            wb_temp=_to_float(self.wb_temp.get(), 0.0)
            if self.wb_temp.get().strip() else None,
            wb_reference=self.wb_reference.get().strip() or None,
            wb_tint=_to_float(self.wb_tint.get(), 0.0)
            if self.wb_tint.get().strip() else 0.0,
            levels=self.levels.get().strip(),
            curves=self.curves.get().strip(),
            vibrance=_to_float(self.vibrance.get(), 0.0)
            if self.vibrance.get().strip() else 0.0,
            color_grading=self.color_grading.get().strip(),
            hsl=self.hsl.get().strip(),
            clarity=_to_float(self.clarity.get(), 0.0)
            if self.clarity.get().strip() else 0.0,
            texture=_to_float(self.texture.get(), 0.0)
            if self.texture.get().strip() else 0.0,
            dehaze=_to_float(self.dehaze.get(), 0.0)
            if self.dehaze.get().strip() else 0.0,
            vignette=self.vignette.get().strip(),
            grain=self.grain.get().strip(),
            # Local adjustments + lens correction (v1.7.0)
            point_color=self.point_color.get().strip(),
            masks=self.masks.get().strip(),
            mask_adjust=self.mask_adjust.get().strip(),
            lens_distort=_to_float(self.lens_distort.get(), 0.0)
            if self.lens_distort.get().strip() else 0.0,
            lens_vignette=self.lens_vignette.get().strip(),
            lens_ca=self.lens_ca.get().strip(),
            lens_profile=self.lens_profile.get() or None,
            auto_levels=self.auto_levels.get(),
            highlight_recovery=(_to_float(self.highlight_recovery.get(), 0.0)
                                or None),
            srgb=self.srgb.get(),
            flatten_cmyk=self.flatten_cmyk.get(),
            evaluate=self.evaluate.get(),
            blur_score=self.blur_score.get(),
            resume=self.resume.get(),
            print_size=self.print_size.get().strip() or None,
            date_shift=self.date_shift.get().strip() or None,
            sync_date=self.sync_date.get(),
            scrub=self.scrub.get(),
            gpx_trace=self.gpx_trace.get().strip() or None,
            blur_faces={"": None,
                        self._t("blur_faces_blur"): "blur",
                        self._t("blur_faces_pixelate"): "pixelate"}.get(
                            self.blur_faces.get()),
            blur_faces_margin=_to_float(self.blur_faces_margin.get(), 20.0),
            cutout=self._cutout_spec(),
            max_straighten_angle=_to_float(self.max_straighten_angle.get(), 10.0),
            crop=self.crop.get().strip() or None,
            crop_ratio=self.crop_ratio.get().strip() or None,
            rotate_degrees=_to_float(self.rotate.get(), 0.0),
            rotate_bg=self.rotate_bg.get().strip() or None,
            flip=self.flip.get() or None,
            pad_ratio=self.pad_ratio.get().strip() or None,
            pad_bg=self.pad_bg.get().strip() or "#000000",
            watermark_text=self.watermark_text.get().strip(),
            watermark_image=self.watermark_image.get().strip(),
            watermark_position=self.watermark_position.get() or "BOTTOM_RIGHT",
            watermark_opacity=int(self.watermark_opacity.get()),
            output_sizes=output_sizes,
            rename_pattern=self.rename_pattern.get(),
            folder_pattern=_resolve_folder_pattern(self.folder_pattern.get()),
            jobs=jobs,
        )

    def _show_watch(self):
        """Folder watcher: auto-process new images dropped into a directory.
        Runs in a daemon thread (watchdog Observer); closing the dialog stops
        it. Uses the current watch fields, not the main-window options."""
        win = tk.Toplevel(self.root)
        win.title(self._t("watch_title"))
        win.geometry("560x460")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        from ..engine import ProcessOptions, SUPPORTED_FORMATS

        watch_dir = tk.StringVar()
        out_dir = tk.StringVar()
        recursive = tk.BooleanVar(value=False)
        fmt = tk.StringVar(value=self.output_format.get() or "JPEG")
        quality = tk.IntVar(value=85)
        rm_orig = tk.BooleanVar(value=False)
        state = {"stop": threading.Event(), "running": False, "count": 0,
                 "thread": None}

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        def _row(row, label_key, var, browse_dir=False):
            tk.Label(body, text=self._t(label_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
                row=row, column=0, sticky="w", pady=(0, 6), padx=(0, 10))
            ttk.Entry(body, textvariable=var, font=FONT_BODY).grid(
                row=row, column=1, sticky="ew", pady=(0, 6))
            if browse_dir:
                def _browse():
                    if self._dlg_cooldown_active():
                        return
                    p = filedialog.askdirectory()
                    self._after_file_dialog()
                    if p:
                        var.set(p)
                FlatButton(body, text=self._t("browse"), command=_browse,
                           bg=COLORS["card"], fg=COLORS["text"],
                           hover_bg=COLORS["bg"],
                           border_color=COLORS["border"],
                           font=FONT_SMALL).grid(
                    row=row, column=2, sticky="w", padx=(8, 0), pady=(0, 6))

        _row(0, "watch_dir", watch_dir, browse_dir=True)
        _row(2, "watch_outdir", out_dir, browse_dir=True)

        ttk.Checkbutton(body, text=self._t("watch_recursive"),
                        variable=recursive).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 6))

        tk.Label(body, text=self._t("watch_format"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=5, column=0, sticky="w", pady=(0, 6), padx=(0, 10))
        ttk.Combobox(body, textvariable=fmt,
                     values=list(SUPPORTED_FORMATS), state="readonly",
                     font=FONT_BODY).grid(row=5, column=1, columnspan=2,
                                          sticky="ew", pady=(0, 6))

        qrow = tk.Frame(body, bg=COLORS["bg"])
        qrow.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(2, 2))
        qval = tk.Label(qrow, text=str(quality.get()), font=FONT_SMALL,
                        fg=COLORS["accent"], bg=COLORS["bg"], width=4)
        qval.pack(side="right")
        qlbl = tk.Label(qrow, text=self._t("watch_quality"), font=FONT_SMALL,
                        fg=COLORS["text_secondary"], bg=COLORS["bg"])
        qlbl.pack(side="left")
        ttk.Scale(qrow, from_=1, to=100, variable=quality,
                  command=lambda v: qval.configure(text=str(int(float(v)))))\
            .pack(side="left", fill="x", expand=True, padx=(8, 8))

        ttk.Checkbutton(body, text=self._t("watch_remove_original"),
                        variable=rm_orig).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(2, 10))

        btns = tk.Frame(body, bg=COLORS["bg"])
        btns.grid(row=8, column=0, columnspan=3, sticky="w")
        start_btn = FlatButton(btns, text=self._t("watch_start"),
                               command=lambda: _start(),
                               bg=COLORS["accent"],
                               hover_bg=COLORS["accent_hover"])
        start_btn.pack(side="left")
        stop_btn = FlatButton(btns, text=self._t("watch_stop"),
                              command=lambda: _stop(),
                              bg=COLORS["card"], fg=COLORS["text"],
                              hover_bg=COLORS["bg"],
                              border_color=COLORS["border"])
        stop_btn.pack(side="left", padx=(8, 0))
        stop_btn.configure(state="disabled")

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))

        bus = UiBus(win)
        schedule = bus.schedule

        def _start():
            # stop-then-start race: the previous watcher thread may still be
            # draining when Start is pressed again — two watchers on the same
            # directory would double-process every new file. Wait for it.
            prev = state.get("thread")
            if prev is not None and prev.is_alive():
                status.configure(
                    text=self._t("watch_stopping_prev")
                    if self._t("watch_stopping_prev") != "watch_stopping_prev"
                    else "等待上一个 watcher 停止… stopping previous watcher…",
                    fg=COLORS["warning"])
                if win.winfo_exists():
                    win.after(200, _start)
                return
            d = watch_dir.get().strip()
            if not d or not os.path.isdir(d):
                status.configure(text=self._t("watch_no_dir"),
                                 fg=COLORS["danger"])
                return
            import importlib.util
            if importlib.util.find_spec("watchdog") is None:
                status.configure(text=self._t("watch_no_watchdog"),
                                 fg=COLORS["danger"])
                return
            state["stop"].clear()
            state["running"] = True
            state["count"] = 0
            start_btn.configure(state="disabled")
            stop_btn.configure(state="normal")
            status.configure(text=self._t("watch_running"),
                             fg=COLORS["success"])
            opts = ProcessOptions(
                quality=int(quality.get()),
                output_format=fmt.get(),
                output_dir=out_dir.get().strip() or None,
                remove_original=rm_orig.get(),
            )
            rec = recursive.get()  # read on the main thread only

            def run():
                from ..watcher import start_watching
                start_watching(d, opts, recursive=rec,
                               on_process=lambda r: schedule(
                                   lambda: _on_result(r)),
                               stop_event=state["stop"])
                state["running"] = False

            state["thread"] = threading.Thread(target=run, daemon=True)
            state["thread"].start()

        def _stop():
            state["stop"].set()
            start_btn.configure(state="normal")
            stop_btn.configure(state="disabled")

        def _on_result(r):
            if not win.winfo_exists():
                return
            if r.success and r.output_path:
                try:
                    self._append_files([r.output_path])
                except Exception:
                    pass
                state["count"] += 1
                status.configure(
                    text=self._t("watch_processed", n=state["count"]),
                    fg=COLORS["success"])


        win.protocol("WM_DELETE_WINDOW", lambda: (_stop(), win.destroy()))
        bus.start()

    def _show_contact_sheet(self):
        """Contact sheet: grid of thumbnails from the checked files."""
        files = self._checked_files()
        if not files:
            if not self.files:
                messagebox.showinfo(self._t("contact_title"),
                                    self._t("contact_need_files"))
            else:
                messagebox.showinfo(self._t("contact_title"),
                                    self._t("check_none"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("contact_title"))
        win.geometry("480x400")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        out_var = tk.StringVar(value=os.path.join(
            os.getcwd(), "contact_sheet.png"))
        cols_var = tk.StringVar(value="4")
        thumb_var = tk.StringVar(value="240x240")
        cap_var = tk.BooleanVar(value=True)
        bg_var = tk.StringVar(value="#000000")

        def _row(row, label_key, var, browse=False):
            tk.Label(body, text=self._t(label_key), font=FONT_SMALL,
                     fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
                row=row, column=0, sticky="w", pady=(0, 6), padx=(0, 10))
            ttk.Entry(body, textvariable=var, font=FONT_BODY).grid(
                row=row, column=1, sticky="ew", pady=(0, 6))
            if browse:
                def _browse():
                    if self._dlg_cooldown_active():
                        return
                    p = filedialog.asksaveasfilename(
                        defaultextension=".png",
                        initialfile=os.path.basename(var.get()),
                        filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"),
                                   ("WebP", "*.webp")])
                    self._after_file_dialog()
                    if p:
                        var.set(p)
                FlatButton(body, text=self._t("browse"), command=_browse,
                           bg=COLORS["card"], fg=COLORS["text"],
                           hover_bg=COLORS["bg"],
                           border_color=COLORS["border"],
                           font=FONT_SMALL).grid(
                    row=row, column=2, sticky="w", padx=(8, 0), pady=(0, 6))

        _row(0, "contact_output", out_var, browse=True)
        _row(2, "contact_cols", cols_var)
        _row(3, "contact_thumb", thumb_var)
        ttk.Checkbutton(body, text=self._t("contact_caption"),
                        variable=cap_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 2))
        _row(5, "contact_bg", bg_var)

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        open_btn = FlatButton(body, text=self._t("contact_open"),
                              command=lambda: _open(),
                              bg=COLORS["card"], fg=COLORS["text"],
                              hover_bg=COLORS["bg"],
                              border_color=COLORS["border"],
                              font=FONT_SMALL)
        open_btn.grid(row=7, column=1, sticky="w", pady=(8, 0))
        open_btn.grid_remove()

        gen_btn = FlatButton(body, text=self._t("contact_generate"),
                             command=lambda: _generate(),
                             bg=COLORS["accent"],
                             hover_bg=COLORS["accent_hover"])
        gen_btn.grid(row=7, column=0, sticky="w", pady=(8, 0))

        bus = UiBus(win)
        schedule = bus.schedule

        def _parse_thumb():
            from ..cli import _parse_dimensions
            try:
                tw, th = _parse_dimensions(thumb_var.get().strip())
                return (tw or 240, th or 240)
            except Exception:
                return (240, 240)

        def _parse_cols():
            try:
                return max(1, int(cols_var.get()))
            except (ValueError, TypeError):
                return 4

        def _parse_bg():
            from ..adjust import hex_to_rgb
            try:
                return hex_to_rgb(bg_var.get().strip())
            except (ValueError, AttributeError):
                status.configure(text=self._t("contact_bad_bg"),
                                 fg=COLORS["warning"])
                return (0, 0, 0)

        def _generate():
            gen_btn.configure(state="disabled")
            status.configure(text=self._t("preview_render"))
            output = out_var.get().strip()
            captions = cap_var.get()
            bg = _parse_bg()
            cols = _parse_cols()      # parsed on the MAIN thread — the old
            thumb = _parse_thumb()    # worker read cols_var/thumb_var itself

            def run():
                try:
                    result = self._contact_sheet_build(
                        files, output, cols=cols,
                        thumb_size=thumb, captions=captions, bg=bg)
                    schedule(lambda: _done(result))
                except Exception as e:
                    schedule(lambda err=str(e): _done(None, err))

            threading.Thread(target=run, daemon=True).start()

        def _done(result, err=None):
            if not win.winfo_exists():
                return
            gen_btn.configure(state="normal")
            if err or not result:
                status.configure(text=self._t("contact_failed",
                                              err=err or "?"),
                                 fg=COLORS["danger"])
                return
            status.configure(text=self._t("contact_done", path=result),
                             fg=COLORS["success"])
            open_btn.grid()

        def _open():
            p = out_var.get().strip()
            if p and os.path.isfile(p):
                webbrowser.open(Path(os.path.abspath(p)).as_uri())


        bus.start()

    def _show_hdr(self):
        """HDR merge: fuse the checked bracketed exposures into one image.

        Uses opencv exposure fusion (Mertens); --align runs AlignMTB for
        handheld brackets. Shows a clear hint when opencv isn't installed.
        """
        files = self._checked_files()
        if len(files) < 2:
            messagebox.showinfo(
                self._t("hdr_title"),
                self._t("hdr_need_files"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("hdr_title"))
        win.geometry("460x240")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        tk.Label(body, text=self._t("hdr_count", n=len(files)),
                 font=FONT_BODY, fg=COLORS["text_secondary"],
                 bg=COLORS["bg"]).grid(row=0, column=0, columnspan=3,
                                       sticky="w", pady=(0, 10))

        out_var = tk.StringVar(value=os.path.join(os.getcwd(), "hdr.jpg"))
        align_var = tk.BooleanVar(value=False)

        tk.Label(body, text=self._t("hdr_output"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=1, column=0, sticky="w", pady=(0, 6), padx=(0, 10))
        ttk.Entry(body, textvariable=out_var, font=FONT_BODY).grid(
            row=1, column=1, sticky="ew", pady=(0, 6))

        def _browse_out():
            if self._dlg_cooldown_active():
                return
            p = filedialog.asksaveasfilename(
                defaultextension=".jpg",
                initialfile=os.path.basename(out_var.get()),
                filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png"),
                           ("TIFF", "*.tif")])
            self._after_file_dialog()
            if p:
                out_var.set(p)

        FlatButton(body, text=self._t("browse"), command=_browse_out,
                   bg=COLORS["card"], fg=COLORS["text"],
                   hover_bg=COLORS["bg"], border_color=COLORS["border"],
                   font=FONT_SMALL).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(0, 6))

        ttk.Checkbutton(body, text=self._t("hdr_align"),
                        variable=align_var).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 2))

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        merge_btn = FlatButton(body, text=self._t("hdr_merge"),
                               command=lambda: _merge(),
                               bg=COLORS["accent"],
                               hover_bg=COLORS["accent_hover"])
        merge_btn.grid(row=4, column=0, sticky="w", pady=(10, 0))

        bus = UiBus(win)
        schedule = bus.schedule

        def _merge():
            merge_btn.configure(state="disabled")
            status.configure(text=self._t("preview_render"))
            output = out_var.get().strip()
            align = align_var.get()

            def run():
                try:
                    self._hdr_merge(files, output, align=align)
                    schedule(lambda: _done(output))
                except Exception as e:
                    schedule(lambda err=str(e): _done(None, err))

            threading.Thread(target=run, daemon=True).start()

        def _done(out, err=None):
            if not win.winfo_exists():
                return
            merge_btn.configure(state="normal")
            if err or not out:
                status.configure(
                    text=self._t("hdr_failed", err=err or "?"),
                    fg=COLORS["danger"])
                return
            status.configure(text=self._t("hdr_done", out=out),
                             fg=COLORS["success"])


        bus.start()

    def _show_cull(self):
        """Cull: classify the file list by exposure/sharpness thresholds,
        then optionally keep only the matches (removing the rest, undoable)."""
        if not self.files:
            messagebox.showinfo(self._t("cull_title"),
                                self._t("cull_no_files"))
            return

        win = tk.Toplevel(self.root)
        win.title(self._t("cull_title"))
        win.geometry("640x540")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        ov = tk.StringVar(); un = tk.StringVar()
        lmin = tk.StringVar(); lmax = tk.StringVar(); shp = tk.StringVar()
        state = {"results": None}

        def _row(row, label_key, var, hint=""):
            tk.Label(body, text=self._t(label_key) + (hint or ""),
                     font=FONT_SMALL, fg=COLORS["text_secondary"],
                     bg=COLORS["bg"]).grid(row=row, column=0, sticky="w",
                                           pady=(0, 6), padx=(0, 10))
            ttk.Entry(body, textvariable=var, font=FONT_BODY, width=10).grid(
                row=row, column=1, sticky="w", pady=(0, 6))

        _row(0, "cull_overexposed", ov, " %")
        _row(1, "cull_underexposed", un, " %")
        _row(2, "cull_lum_min", lmin)
        _row(3, "cull_lum_max", lmax)
        _row(4, "cull_sharp", shp)

        def _thresholds():
            def _num(v):
                v = v.get().strip()
                try:
                    return float(v) if v else None
                except ValueError:
                    return None  # non-numeric input → threshold unset
            return {"overexposed_max": _num(ov),
                    "underexposed_max": _num(un),
                    "luminance_min": _num(lmin),
                    "luminance_max": _num(lmax),
                    "sharpness_min": _num(shp)}

        btns = tk.Frame(body, bg=COLORS["bg"])
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 8))
        scan_btn = FlatButton(btns, text=self._t("cull_scan"),
                              command=lambda: _scan(),
                              bg=COLORS["accent"],
                              hover_bg=COLORS["accent_hover"])
        scan_btn.pack(side="left")
        apply_btn = FlatButton(btns, text=self._t("cull_apply"),
                               command=lambda: _apply(),
                               bg=COLORS["card"], fg=COLORS["text"],
                               hover_bg=COLORS["bg"],
                               border_color=COLORS["border"])
        apply_btn.pack(side="left", padx=(8, 0))
        apply_btn.configure(state="disabled")

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))

        tree = ttk.Treeview(body, columns=("lum", "over", "under", "blur",
                                           "kept"), show="headings",
                            height=12)
        for c, w, t in (("lum", 60, self._t("cull_lum_min").replace(
                             "Luminance min", "Lum")),
                        ("over", 50, "%"), ("under", 50, "%"),
                        ("blur", 50, "blur"), ("kept", 50, "✓")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="center")
        tree.column("lum", width=70)
        vsb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=7, column=0, columnspan=3, sticky="nsew")
        vsb.grid(row=7, column=3, sticky="ns")
        body.rowconfigure(7, weight=1)

        bus = UiBus(win)
        schedule = bus.schedule

        def _scan():
            scan_btn.configure(state="disabled")
            status.configure(text=self._t("preview_render"))
            th = _thresholds()

            def run():
                try:
                    results = self._cull_scan(self.files, th)
                    schedule(lambda: _scanned(results))
                except Exception as e:
                    schedule(lambda err=str(e): status.configure(
                        text=self._t("cull_failed", err=err),
                        fg=COLORS["danger"]))

            threading.Thread(target=run, daemon=True).start()

        def _scanned(results):
            if not win.winfo_exists():
                return
            scan_btn.configure(state="normal")
            state["results"] = results
            kept = sum(1 for r in results if r["kept"])
            status.configure(
                text=self._t("cull_kept", kept=kept, total=len(results)),
                fg=COLORS["accent"])
            for item in tree.get_children():
                tree.delete(item)
            for r in results:
                tree.insert("", "end", values=(
                    f"{r['luminance']:.2f}", f"{r['overexposed_pct']:.1f}",
                    f"{r['underexposed_pct']:.1f}",
                    r.get("blur_score", "-"),
                    "✓" if r["kept"] else "✗"))
            apply_btn.configure(state="normal")

        def _apply():
            if self.processing:
                status.configure(text=self._t("cull_processing"),
                                 fg=COLORS["warning"])
                return
            results = state.get("results")
            if not results:
                return
            kept = [r["path"] for r in results if r["kept"]]
            kept_set = set(kept)
            removed = [(i, f) for i, f in enumerate(self.files)
                       if f not in kept_set]
            if not removed:
                return
            was_checked = set(self._checked - kept_set)
            self.files = kept
            self._checked &= kept_set
            self._refresh_file_list()
            self._update_stats()
            self._push_undo(self._t("undo_cull", n=len(removed)),
                            lambda: self._restore_removed(list(removed),
                                                          set(was_checked)),
                            lambda: self._redo_remove(list(removed),
                                                      set(was_checked)))
            status.configure(text=self._t("cull_kept", kept=len(kept),
                                          total=len(self.files)),
                             fg=COLORS["success"])


        bus.start()

    def _show_hash(self):
        """Checksums: generate a manifest of the checked files, or verify an
        existing one."""
        win = tk.Toplevel(self.root)
        win.title(self._t("hash_title"))
        win.geometry("600x500")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=12, pady=12)

        # ── Generate tab ──
        gen = tk.Frame(nb, bg=COLORS["bg"])
        nb.add(gen, text=self._t("hash_tab_gen"))
        gen.columnconfigure(1, weight=1)
        out_var = tk.StringVar(value=os.path.join(os.getcwd(),
                                                  "manifest.csv"))
        status_g = tk.Label(gen, text="", font=FONT_SMALL,
                            fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_g.grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 8))
        tk.Label(gen, text=self._t("hash_output"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        ttk.Entry(gen, textvariable=out_var, font=FONT_BODY).grid(
            row=1, column=1, sticky="ew", pady=(0, 6))

        def _browse_out():
            if self._dlg_cooldown_active():
                return
            p = filedialog.asksaveasfilename(
                defaultextension=".csv", initialfile="manifest.csv",
                filetypes=[("CSV", "*.csv")])
            self._after_file_dialog()
            if p:
                out_var.set(p)

        FlatButton(gen, text=self._t("browse"), command=_browse_out,
                   bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
                   border_color=COLORS["border"],
                   font=FONT_SMALL).grid(row=1, column=2, sticky="w",
                                         padx=(8, 0), pady=(0, 6))
        open_g = FlatButton(gen, text=self._t("hash_open"),
                            command=lambda: _open_path(out_var.get()),
                            bg=COLORS["card"], fg=COLORS["text"],
                            hover_bg=COLORS["bg"],
                            border_color=COLORS["border"], font=FONT_SMALL)
        open_g.grid(row=2, column=1, sticky="w", pady=(4, 0))
        open_g.grid_remove()
        FlatButton(gen, text=self._t("hash_generate"), command=lambda: _gen(),
                   bg=COLORS["accent"],
                   hover_bg=COLORS["accent_hover"]).grid(
            row=2, column=0, sticky="w", pady=(4, 0))

        # ── Verify tab ──
        ver = tk.Frame(nb, bg=COLORS["bg"])
        nb.add(ver, text=self._t("hash_tab_verify"))
        ver.columnconfigure(1, weight=1)
        manifest_var = tk.StringVar()
        status_v = tk.Label(ver, text="", font=FONT_SMALL,
                            fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status_v.grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 8))

        def _browse_manifest():
            if self._dlg_cooldown_active():
                return
            p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
            self._after_file_dialog()
            if p:
                manifest_var.set(p)

        tk.Label(ver, text=self._t("hash_choose"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        ttk.Entry(ver, textvariable=manifest_var, font=FONT_BODY).grid(
            row=1, column=1, sticky="ew", pady=(0, 6))
        FlatButton(ver, text=self._t("browse"), command=_browse_manifest,
                   bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
                   border_color=COLORS["border"],
                   font=FONT_SMALL).grid(row=1, column=2, sticky="w",
                                         padx=(8, 0), pady=(0, 6))
        FlatButton(ver, text=self._t("hash_verify"), command=lambda: _ver(),
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"]).grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        tree = ttk.Treeview(ver, columns=("kind", "detail"), show="headings",
                            height=10)
        tree.heading("kind", text="")
        tree.heading("detail", text="")
        tree.column("kind", width=90, anchor="w")
        tree.column("detail", width=420, anchor="w")
        tree.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        ver.rowconfigure(3, weight=1)

        bus = UiBus(win)
        schedule = bus.schedule

        def _gen():
            files = self._checked_files()
            if not files:
                status_g.configure(text=self._t("hash_no_files"),
                                   fg=COLORS["warning"])
                return
            status_g.configure(text=self._t("preview_render"))
            output = out_var.get().strip()

            def run():
                try:
                    self._hash_generate(files, output)
                    schedule(lambda: _gen_done())
                except Exception as e:
                    schedule(lambda err=str(e): status_g.configure(
                        text=self._t("hash_failed", err=err),
                        fg=COLORS["danger"]))

            threading.Thread(target=run, daemon=True).start()

        def _gen_done():
            if not win.winfo_exists():
                return
            status_g.configure(
                text=self._t("hash_done", path=out_var.get(),
                             n=len(self._checked_files())),
                fg=COLORS["success"])
            open_g.grid()

        def _ver():
            m = manifest_var.get().strip()
            if not m:
                return
            status_v.configure(text=self._t("preview_render"))

            def run():
                try:
                    report = self._hash_verify(m)
                    schedule(lambda: _ver_done(report))
                except Exception as e:
                    schedule(lambda err=str(e): status_v.configure(
                        text=self._t("hash_failed", err=err),
                        fg=COLORS["danger"]))

            threading.Thread(target=run, daemon=True).start()

        def _ver_done(report):
            if not win.winfo_exists():
                return
            ok_all = (report["missing"] == [] and report["mismatched"] == [])
            status_v.configure(
                text=" · ".join(filter(None, [
                    self._t("hash_total", n=report["total"]),
                    self._t("hash_ok", n=report["ok"]),
                    (self._t("hash_missing", n=len(report["missing"]))
                     if report["missing"] else ""),
                    (self._t("hash_mismatched", n=len(report["mismatched"]))
                     if report["mismatched"] else ""),
                ])) or self._t("hash_all_ok"),
                fg=COLORS["success"] if ok_all else COLORS["danger"])
            for item in tree.get_children():
                tree.delete(item)
            for p in report["missing"]:
                tree.insert("", "end", values=(self._t("hash_missing", n=1),
                                               p))
            for mm in report["mismatched"]:
                tree.insert("", "end", values=(
                    self._t("hash_mismatched", n=1),
                    f"{mm['path']}  (expected {mm['expected'][:12]}… "
                    f"got {mm['actual'][:12]}…)"))

        def _open_path(p):
            if p and os.path.isfile(p):
                webbrowser.open(Path(os.path.abspath(p)).as_uri())


        bus.start()

    def _show_presets(self):
        """Presets: save / load / delete named option sets (stored as JSON in
        ~/.photos/presets). Load applies the preset back onto the settings."""
        win = tk.Toplevel(self.root)
        win.title(self._t("presets_title"))
        win.geometry("520x440")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        from .. import presets

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        tk.Label(body, text=self._t("presets_list"), font=FONT_BODY,
                 fg=COLORS["text"], bg=COLORS["bg"]).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        listbox = tk.Listbox(body, height=8, font=FONT_BODY,
                             selectmode=tk.SINGLE,
                             bg=COLORS["card"], fg=COLORS["text"],
                             highlightthickness=0,
                             relief=tk.FLAT)
        listbox.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        body.rowconfigure(1, weight=1)

        tk.Label(body, text=self._t("presets_name"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        name_var = tk.StringVar()
        ttk.Entry(body, textvariable=name_var, font=FONT_BODY).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=(0, 6))
        tk.Label(body, text=self._t("presets_desc"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        desc_var = tk.StringVar()
        ttk.Entry(body, textvariable=desc_var, font=FONT_BODY).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = tk.Frame(body, bg=COLORS["bg"])
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        FlatButton(btns, text=self._t("presets_save"), command=lambda: _save(),
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"]
                   ).pack(side="left")
        FlatButton(btns, text=self._t("presets_load"), command=lambda: _load(),
                   bg=COLORS["card"], fg=COLORS["text"], hover_bg=COLORS["bg"],
                   border_color=COLORS["border"]).pack(side="left", padx=(8, 0))
        FlatButton(btns, text=self._t("presets_delete"),
                   command=lambda: _delete(),
                   bg=COLORS["card"], fg=COLORS["danger"],
                   hover_bg=COLORS["danger_hover"],
                   border_color=COLORS["border"]).pack(side="left", padx=(8, 0))

        def _refresh():
            listbox.delete(0, tk.END)
            try:
                items = presets.list_presets()
            except Exception:
                items = []
            if not items:
                listbox.insert(tk.END, self._t("presets_empty"))
                listbox.itemconfig(0, fg=COLORS["text_secondary"])
            for it in items:
                listbox.insert(tk.END, it)

        def _selected_name():
            sel = listbox.curselection()
            if not sel:
                return None
            item = listbox.get(sel[0])
            if item == self._t("presets_empty"):
                return None
            return item.split(" — ", 1)[0]

        def _save():
            name = name_var.get().strip()
            if not name:
                status.configure(text=self._t("presets_name_required"),
                                 fg=COLORS["warning"])
                return
            try:
                presets.save_preset(name, self._build_options(),
                                    desc_var.get().strip())
            except Exception as e:
                status.configure(text=self._t("presets_load_failed",
                                              name=str(e)),
                                 fg=COLORS["danger"])
                return
            status.configure(text=self._t("presets_saved"),
                             fg=COLORS["success"])
            _refresh()

        def _load():
            name = _selected_name()
            if not name:
                return
            try:
                opts = presets.load_preset(name)
            except Exception:
                opts = None
            if opts is None:
                status.configure(text=self._t("presets_load_failed",
                                              name=name),
                                 fg=COLORS["danger"])
                return
            self._apply_options_to_ui(opts)
            status.configure(text=self._t("presets_loaded", name=name),
                             fg=COLORS["success"])

        def _delete():
            name = _selected_name()
            if not name:
                return
            if not messagebox.askyesno(
                    self._t("presets_title"),
                    self._t("presets_confirm_delete", name=name)):
                return
            try:
                presets.delete_preset(name)
            except Exception as e:
                status.configure(text=self._t("presets_load_failed",
                                              name=str(e)),
                                 fg=COLORS["danger"])
                return
            status.configure(text=self._t("presets_deleted", name=name),
                             fg=COLORS["success"])
            _refresh()

        _refresh()

    def _preview(self):
        """Visual preview: render the checked file through the REAL engine
        pipeline into a temp dir and show original vs processed side by side,
        auto-refreshing (debounced ~400ms) as settings change. Never deletes
        the source — _preview_options force-sets remove_original=False."""
        files = self._checked_files()
        if not files:
            if not self.files:
                messagebox.showwarning(self._t("dlg_no_files_title"),
                                       self._t("dlg_no_files"))
            else:
                messagebox.showwarning(self._t("dlg_no_files_title"),
                                       self._t("check_none"))
            return

        from PIL import Image, ImageTk

        tempdir = tempfile.mkdtemp(prefix="photos_preview_")
        # registered so _on_main_close can rmtree even when the preview's
        # own drain loop dies with the mainloop (closing the app with a
        # preview open used to strand the temp dir forever)
        self._preview_tempdirs.add(tempdir)
        win = tk.Toplevel(self.root)
        win.title(self._t("preview_title"))
        win.geometry("1000x640")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        state = {"files": files, "idx": 0, "sig": None, "stable": 0,
                 "inflight": False, "render_sig": None, "rendered": None,
                 "tempdir": tempdir}

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=12)

        nav = tk.Frame(body, bg=COLORS["bg"])
        nav.pack(fill="x", pady=(0, 8))
        nav_lbl = tk.Label(nav, text="", font=FONT_SMALL,
                           fg=COLORS["text_secondary"], bg=COLORS["bg"])
        nav_lbl.pack(side="left", padx=(0, 12))
        # v2.4 WYSIWYG: the preview now renders the CURRENT file through its
        # per-photo overlay + masks (same injection as batch export) — the
        # badge makes that visible instead of a silent difference
        adj_lbl = tk.Label(nav, text="", font=FONT_TINY,
                           fg=COLORS["accent"], bg=COLORS["bg"])

        def _sync_adj_badge():
            p = state["files"][state["idx"]]
            if p in self._photo_adjust or p in (self._photo_masks or {}):
                adj_lbl.configure(text=self._t("export_adjusted_badge"))
                adj_lbl.pack(side="left", padx=(0, 12))
            else:
                adj_lbl.pack_forget()
        _sync_adj_badge()
        FlatButton(nav, text="‹", command=lambda: _nav(-1),
                   bg=COLORS["card"], fg=COLORS["text"],
                   hover_bg=COLORS["bg"], border_color=COLORS["border"],
                   font=FONT_BODY, padx=10).pack(side="left")
        FlatButton(nav, text="›", command=lambda: _nav(1),
                   bg=COLORS["card"], fg=COLORS["text"],
                   hover_bg=COLORS["bg"], border_color=COLORS["border"],
                   font=FONT_BODY, padx=10).pack(side="left", padx=(6, 0))

        panels = tk.Frame(body, bg=COLORS["bg"])
        panels.pack(fill="both", expand=True)
        left = tk.Frame(panels, bg=COLORS["card"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        right = tk.Frame(panels, bg=COLORS["card"])
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        tk.Label(left, text=self._t("before"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
            anchor="w", padx=8, pady=(6, 0))
        tk.Label(right, text=self._t("after"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
            anchor="w", padx=8, pady=(6, 0))
        orig_lbl = tk.Label(left, bg=COLORS["card"])
        orig_lbl.pack(expand=True, pady=(0, 8))
        proc_lbl = tk.Label(right, bg=COLORS["card"])
        proc_lbl.pack(expand=True, pady=(0, 8))

        status = tk.Label(body, text="", font=FONT_SMALL,
                          fg=COLORS["text_secondary"], bg=COLORS["bg"])
        status.pack(fill="x", pady=(8, 0))

        def _render_image(lbl, path):
            try:
                img = _open_image_safe(path).convert("RGB")
                img.thumbnail((430, 300), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=lbl)
                lbl.configure(image=photo)
                lbl.image = photo
            except Exception:
                lbl.configure(image="", text=self._t("cannot_load"))

        def _nav(delta):
            n = len(state["files"])
            state["idx"] = (state["idx"] + delta) % n
            state["sig"] = None
            state["stable"] = 0
            # NOTE: inflight is left alone — clearing it would let a second
            # render launch while the old one is still running. The pending
            # render's signature is invalidated instead, so its (now stale)
            # result is discarded when it lands.
            state["render_sig"] = None
            nav_lbl.configure(text=f"{state['idx'] + 1}/{n} · "
                              f"{os.path.basename(state['files'][state['idx']])}")
            _sync_adj_badge()
            _render_image(orig_lbl, state["files"][state["idx"]])
            proc_lbl.configure(image="", text=self._t("preview_render"))
            status.configure(text="")

        def _done(result, sig):
            state["inflight"] = False
            if not win.winfo_exists():
                return
            if sig != state["render_sig"]:
                return  # stale render; a newer one is pending
            # Record what was actually rendered so drain never relaunches the
            # same (options, path) — the debounce counter alone isn't enough:
            # after inflight drops, stable re-accumulates and would relaunch
            # unchanged options forever.
            state["rendered"] = sig
            if result.success and result.output_path:
                _render_image(proc_lbl, result.output_path)
                status.configure(text=self._t(
                    "preview_rendered",
                    in_size=format_size(result.input_size),
                    out_size=format_size(result.output_size),
                    q=result.achieved_quality or "-"))
            else:
                status.configure(text=self._t("preview_error",
                                              err=result.error or "?"))

        bus = UiBus(win)
        schedule = bus.schedule

        def launch(sig, path, opts):
            from ..engine import ProcessResult
            state["inflight"] = True
            # The staleness signature must include the file: ProcessOptions
            # compares by value, so an options-only sig cannot tell a stale
            # render of the previous file from a fresh one of this file.
            rsig = (sig, path)
            state["render_sig"] = rsig
            proc_lbl.configure(image="", text=self._t("preview_render"))
            status.configure(text=self._t("preview_render"))

            def render():
                try:
                    result = self._preview_render(path, opts)
                except Exception as e:
                    result = ProcessResult(
                        input_path=path, output_path="", input_size=0,
                        output_size=0, input_format="", output_format="",
                        input_dims=(0, 0), output_dims=(0, 0),
                        success=False, error=str(e))
                schedule(lambda: _done(result, rsig))

            threading.Thread(target=render, daemon=True).start()

        def drain():
            bus.drain_pending()
            if not win.winfo_exists():
                # Keep draining until an in-flight render finishes, then
                # remove the temp dir — never rmtree while writing.
                if state["inflight"]:
                    self.root.after(100, drain)
                else:
                    shutil.rmtree(state["tempdir"], ignore_errors=True)
                return
            cur = self._build_options()
            if cur != state["sig"]:
                state["sig"] = cur
                state["stable"] = 0
            else:
                state["stable"] += 1
            cur_path = state["files"][state["idx"]]
            # v2.4 WYSIWYG: render through the current file's overlay +
            # masks (the same _per_file_overlay injection batch export
            # uses). Keying staleness on the EFFECTIVE options also means
            # an overlay change (paste / AI tone landing) re-renders.
            eff = workflows.preview_options(
                self._per_file_overlay(cur_path, cur), tempdir)
            if (state["stable"] >= 5 and not state["inflight"]
                    and (eff, cur_path) != state["rendered"]):
                launch(eff, cur_path, eff)
            self.root.after(80, drain)

        nav_lbl.configure(text=f"1/{len(state['files'])} · "
                          f"{os.path.basename(state['files'][0])}")
        _render_image(orig_lbl, state["files"][0])
        self.root.after(80, drain)

    def _start_processing(self, file_list=None, confirm_delete=True):
        """Start batch processing in a background thread.

        Args:
            file_list: Files to process (default: the checked files).
            confirm_delete: Skip the remove-original confirmation on
                            automatic queue follow-up runs.
        """
        if file_list is not None:
            files = list(file_list)
        else:
            # interactive start: process the checked files only
            files = self._checked_files()
            if not files:
                if not self.files:
                    messagebox.showwarning(self._t("dlg_no_files_title"),
                                           self._t("dlg_no_files"))
                else:
                    messagebox.showwarning(self._t("dlg_no_files_title"),
                                           self._t("check_none"))
                return

        if self.processing:
            return

        # Safety check for remove-original
        if confirm_delete and self.remove_original.get():
            if not messagebox.askyesno(
                self._t("dlg_confirm_delete_title"),
                self._t("dlg_confirm_delete", n=len(files)),
            ):
                return

        # Build options BEFORE entering the processing state, so a bad
        # field can never leave the app stuck in processing=True.
        options = self._build_options()

        self.processing = True
        self.cancel_requested = False
        self._batch_result = None
        self._batch_error = None
        self._progress_started = None  # ETA baseline

        # Update UI to processing state
        self.start_btn.pack_forget()
        self.preview_btn.pack_forget()
        self.cancel_btn.pack(side="right")
        self.progress_bar["value"] = 0
        self.progress_label.config(text=self._t("processing"), fg=COLORS["text"])

        # Disable settings during processing (add buttons stay enabled → queue)
        self._toggle_settings(False)

        # Start background thread
        thread = threading.Thread(
            target=self._process_thread,
            args=(files.copy(), options),
            daemon=True,
        )
        thread.start()

        # Start progress polling via after()
        self._poll_progress()

    def _cancel_processing(self):
        """Request cancellation of processing."""
        self.cancel_requested = True
        self.progress_label.config(text=self._t("cancelling"), fg=COLORS["warning"])

    def _process_thread(self, files, options):
        """Background thread for batch processing."""
        def progress_callback(current, total, path, status=""):
            if self.cancel_requested:
                return  # Stop progress updates once cancelled
            with self._progress_lock:
                self._progress_current = current
                self._progress_total = total
                self._progress_path = path
                self._progress_status = status

        def _per_file_masks(path, opts):
            return self._per_file_overlay(path, opts)

        try:
            result = batch_process(
                files, options,
                progress_callback=progress_callback,
                cancel_checker=lambda: self.cancel_requested,
                per_file_options=_per_file_masks,
            )
            with self._progress_lock:
                self._batch_result = result
        except Exception as e:
            with self._progress_lock:
                self._batch_result = BatchResult(
                    results=[], total_input_size=0, total_output_size=0,
                    success_count=0, fail_count=1,
                )
                self._batch_error = str(e)

    def _poll_progress(self):
        """Poll progress from background thread and update UI."""
        if not self.processing:
            return

        with self._progress_lock:
            current = self._progress_current
            total = self._progress_total
            path = self._progress_path
            status = self._progress_status
            result = self._batch_result

        if result is not None:
            # Processing complete
            self._on_processing_done(result)
            return

        if total > 0:
            self.progress_bar["value"] = (current / total) * 100
            if path:
                name = os.path.basename(path) if path else ""
                action = self._t("tuning") if status == "tuning" else ""
                eta = self._eta_text(current, total)
                self.progress_label.config(
                    text=self._t("processing_item", cur=current, total=total, name=name)
                         + ("  " + action if action else "")
                         + (eta if eta else "")
                )

        # Schedule next poll
        self._after_id = self.root.after(100, self._poll_progress)

    def _eta_text(self, current, total):
        """Estimated remaining time for the running batch ("" when unsure)."""
        if not current:
            return ""
        now = time.monotonic()
        if self._progress_started is None:
            self._progress_started = now
        elapsed = now - self._progress_started
        rate = current / elapsed if elapsed > 0 else 0.0
        if rate <= 0:
            return ""
        remain = (total - current) / rate
        if remain <= 0:
            return ""
        m, s = divmod(int(remain), 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"  · {h}:{m:02d}:{s:02d}"
        return f"  · {m}:{s:02d} {self._t('eta_remaining')}"

    def _on_processing_done(self, result: BatchResult):
        """Handle processing completion."""
        self.processing = False
        self._last_result = result  # for double-click before/after comparison
        was_cancelled = self.cancel_requested
        self.cancel_requested = False

        # Restore UI
        self.cancel_btn.pack_forget()
        self.preview_btn.pack(side="left")
        self.start_btn.pack(side="right")
        self.progress_bar["value"] = 0 if was_cancelled else 100

        # Re-enable settings
        self._toggle_settings(True)

        # Drop files that no longer exist (e.g. deleted by remove_original),
        # then refresh so sizes/dimensions shown are current
        self.files = [f for f in self.files if os.path.exists(f)]
        self._refresh_file_list()

        # Update stats
        if was_cancelled:
            self.progress_label.config(
                text=self._t("cancelled_status",
                             ok=result.success_count, fail=result.fail_count),
                fg=COLORS["warning"],
            )
        elif self._batch_error:
            self.progress_label.config(
                text=self._t("failed_status"),
                fg=COLORS["danger"],
            )
            messagebox.showerror(
                self._t("dlg_error_title"),
                self._t("dlg_error", err=self._batch_error),
            )
            self._batch_error = None
            self._update_stats()
            return
        elif result.success_count > 0:
            savings = format_size(result.savings_bytes)
            self.progress_label.config(
                text=self._t("done_status", ok=result.success_count,
                             total=len(result.results), savings=savings,
                             pct=f"{result.savings_percent:.1f}"),
                fg=COLORS["success"],
            )
        else:
            self.progress_label.config(
                text=self._t("failed_status"),
                fg=COLORS["danger"],
            )

        self._update_stats(result)

        # Show summary dialog
        if result.success_count > 0 or result.fail_count > 0:
            self._show_summary(result)

        # Auto-run queued files (added while processing)
        pending = [f for f in self._queued_files if os.path.exists(f)]
        self._queued_files = []
        if pending and not was_cancelled and not self._batch_error:
            # defer so the summary dialog can render first; skip the
            # remove-original confirmation on automatic follow-up runs
            self.root.after(200,
                            lambda: self._start_processing(pending,
                                                           confirm_delete=False))

    def _show_summary(self, result: BatchResult):
        """Show processing summary in a scrollable dialog.

        A plain messagebox truncates long per-file error lists; the
        readonly Text widget keeps every failure visible.
        """
        lines = [self._t("sum_header"), "=" * 40, ""]
        lines.append(f"{self._t('sum_success')}: {result.success_count}")
        lines.append(f"{self._t('sum_failed')}: {result.fail_count}")
        lines.append("")
        lines.append(f"{self._t('sum_original')}: {format_size(result.total_input_size)}")
        lines.append(f"{self._t('sum_compressed')}: {format_size(result.total_output_size)}")
        lines.append(f"{self._t('sum_saved')}: {format_size(result.savings_bytes)} "
                     f"({result.savings_percent:.1f}%)")
        lines.append("")

        # Show individual failures
        for r in result.results:
            if not r.success:
                lines.append(f"{self._t('sum_failed')}: {os.path.basename(r.input_path)}"
                             f"\n  → {r.error}")

        msg = "\n".join(lines)

        win = tk.Toplevel(self.root)
        win.title(self._t("summary_title"))
        win.geometry("600x480")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)

        tk.Label(win, text=self._t("summary_title"),
                 font=(PLATFORM_FONTS["title"], 14, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(pady=(16, 4))

        text = tk.Text(win, wrap="word", font=FONT_BODY,
                       bg=COLORS["card"], fg=COLORS["text"],
                       relief="flat", borderwidth=0,
                       padx=14, pady=10, highlightthickness=0)
        text.insert("1.0", msg)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=20, pady=(4, 8))

        btns = tk.Frame(win, bg=COLORS["bg"])
        btns.pack(pady=(0, 16))
        if result.success_count > 0:
            FlatButton(
                btns, text=self._t("sum_view_compare"),
                command=lambda: (win.destroy(), self._show_comparison(result)),
                bg=COLORS["accent"]).pack(side="left", padx=6)
        FlatButton(
            btns, text=self._t("close"), command=win.destroy,
            bg=COLORS["bg"], fg=COLORS["text"], hover_bg=COLORS["border"],
            border_color=COLORS["border"]).pack(side="left", padx=6)

    def _show_comparison(self, result: BatchResult):
        """Open a before/after comparison window for the first successful result."""
        first = next((r for r in result.results if r.success), None)
        if first:
            self._show_comparison_for(first)

    def _show_comparison_for(self, r):
        """Open a before/after comparison window for one ProcessResult."""
        from PIL import Image, ImageTk

        win = tk.Toplevel(self.root)
        win.title(self._t("compare_title"))
        win.geometry("900x500")
        win.configure(bg=COLORS["bg"])

        tk.Label(win, text=self._t("compare_header"),
                 font=(PLATFORM_FONTS["title"], 14, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(pady=(16, 4))

        # Info row
        if r.input_size > 0:
            saved_pct = (r.input_size - r.output_size) / r.input_size * 100
        else:
            saved_pct = 0.0
        info = (f"{self._t('before')}: {format_size(r.input_size)}  |  "
                f"{self._t('after')}: {format_size(r.output_size)}  |  "
                f"{self._t('saved')}: {saved_pct:.1f}%  |  "
                f"{self._t('quality_lbl')}: {r.achieved_quality}")
        tk.Label(win, text=info, font=FONT_BODY,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(pady=(0, 12))

        # Images row
        img_frame = tk.Frame(win, bg=COLORS["bg"])
        img_frame.pack(fill="both", expand=True, padx=20)

        # Load and display images (fit within ~400×320 px box)
        max_w, max_h = 400, 320
        for label, path in [(self._t("before"), r.input_path),
                            (self._t("after"), r.output_path)]:
            col = tk.Frame(img_frame, bg=COLORS["card"], bd=0, highlightthickness=0)
            col.pack(side="left", fill="both", expand=True, padx=8)

            tk.Label(col, text=label, font=(PLATFORM_FONTS["body"], 11, "bold"),
                     fg=COLORS["text"], bg=COLORS["card"]).pack(pady=(8, 0))

            try:
                img = _open_image_safe(path).convert("RGB")
                w, h = img.size
                ratio = min(max_w / w, max_h / h, 1.0)
                img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))),
                                 Image.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=col)
                img_label = tk.Label(col, image=photo, bg=COLORS["card"])
                img_label.image = photo  # keep reference
                img_label.pack(padx=12, pady=12)
            except Exception:
                tk.Label(col, text=self._t("cannot_load"), font=FONT_SMALL,
                         fg=COLORS["danger"], bg=COLORS["card"]).pack(padx=12, pady=40)

            tk.Label(col, text=f"{r.input_dims[0]}x{r.input_dims[1]}"
                     if label == self._t("before")
                     else f"{r.output_dims[0]}x{r.output_dims[1]}",
                     font=FONT_TINY, fg=COLORS["text_secondary"],
                     bg=COLORS["card"]).pack(pady=(0, 8))

        # Close button
        FlatButton(win, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=24, pady=6).pack(pady=12)

    def _open_compare(self, path):
        """Double-click a file row → before/after comparison for that file."""
        if self._last_result is None:
            messagebox.showinfo(self._t("cmp_no_result"),
                                self._t("cmp_no_result_body"))
            return
        for r in self._last_result.results:
            if r.input_path == path and r.success:
                self._show_comparison_for(r)
                return
        messagebox.showinfo(self._t("cmp_no_result"),
                            self._t("cmp_no_result_body"))

    def _show_compare(self):
        """Multi-image compare viewer: 2-4 checked images side by side on
        canvases. Wheel-zoom and drag-pan apply only to the panel under
        the cursor; the "sync zoom" checkbox makes the wheel zoom every
        panel together. Double-click resets all panels. Each redraw
        renders only the visible region (PIL resize with a source box),
        debounced to ~60ms; originals are decoded once in a worker
        thread."""
        from PIL import Image, ImageTk

        files = self._checked_files()
        if len(files) < 2:
            messagebox.showinfo(self._t("compare_view_title"),
                                self._t("compare_need_two"))
            return
        files = files[:4]

        win = tk.Toplevel(self.root)
        win.title(self._t("compare_view_title"))
        win.geometry("1100x640")
        win.configure(bg=COLORS["bg"])
        win.transient(self.root)
        canvas_unbind_safe(win)

        panels = []
        pending = [None]  # after() id of the debounced redraw

        tk.Label(win, text=self._t("compare_hint"), font=FONT_SMALL,
                 fg=COLORS["text_secondary"], bg=COLORS["bg"]).pack(
            pady=(12, 0))

        row = tk.Frame(win, bg=COLORS["bg"])
        row.pack(fill="both", expand=True, padx=16, pady=12)

        for i, path in enumerate(files):
            col = tk.Frame(row, bg=COLORS["card"], bd=0, highlightthickness=0)
            col.pack(side="left", fill="both", expand=True,
                     padx=(0 if i == 0 else 4, 0))
            tk.Label(col, text=os.path.basename(path), font=FONT_TINY,
                     fg=COLORS["text_secondary"], bg=COLORS["card"]).pack(
                fill="x", padx=6, pady=(4, 0))
            canvas = tk.Canvas(col, bg=COLORS["card"], highlightthickness=0,
                               bd=0)
            canvas.pack(fill="both", expand=True, padx=4, pady=4)
            panels.append({"path": path, "canvas": canvas, "img": None,
                           "iw": 0, "ih": 0, "scale": None, "photo": None,
                           "error": False, "state": _ZoomPanState()})

        def _schedule_redraw():
            # Debounce: wheel/drag/Configure storms merge into one redraw.
            if pending[0] is None:
                pending[0] = win.after(60, _redraw)

        def _redraw():
            pending[0] = None
            if not win.winfo_exists():
                return
            for p in panels:
                canvas = p["canvas"]
                cw, ch = canvas.winfo_width(), canvas.winfo_height()
                if cw < 2 or ch < 2:
                    continue  # not mapped yet; <Configure> will retrigger
                img = p["img"]
                if img is None:
                    canvas.delete("all")
                    canvas.create_text(
                        cw / 2, ch / 2, font=FONT_SMALL,
                        text=self._t("cannot_load") if p["error"]
                        else self._t("compare_loading"),
                        fill=COLORS["danger"] if p["error"]
                        else COLORS["text_secondary"])
                    continue
                iw, ih = p["iw"], p["ih"]
                # Visible source window: the 1/zoom fraction centered on
                # (fx, fy) — always on-image thanks to the state clamp.
                st = p["state"]
                vw, vh = iw / st.zoom, ih / st.zoom
                left = min(max(st.fx * iw - vw / 2, 0.0), iw - vw)
                top = min(max(st.fy * ih - vh / 2, 0.0), ih - vh)
                # Fit the window into the canvas preserving aspect (the
                # canvas background letterboxes the difference).
                scale = min(cw / vw, ch / vh)
                tw = max(1, int(vw * scale))
                th = max(1, int(vh * scale))
                p["scale"] = scale
                view = img.resize((tw, th), Image.LANCZOS,
                                  box=(left, top, left + vw, top + vh))
                photo = ImageTk.PhotoImage(view, master=canvas)
                p["photo"] = photo  # keep a reference or it is GC'd
                canvas.delete("all")
                canvas.create_image(cw / 2, ch / 2, image=photo)

        sync_zoom = tk.BooleanVar(value=False)

        def _zoom_step(factor, p=None):
            # Default: zoom only the panel under the cursor. With the
            # "sync zoom" checkbox on, every panel zooms together.
            if sync_zoom.get() or p is None:
                for q_ in panels:
                    q_["state"].zoom_at(factor)
            else:
                p["state"].zoom_at(factor)
            _schedule_redraw()

        def _make_wheel(p):
            def _on_wheel(event):
                # macOS trackpads/wheels give ±1-ish deltas, Windows ±120;
                # only the direction matters here.
                _zoom_step(1.1 if event.delta > 0 else 1 / 1.1, p)
            return _on_wheel

        drag = {"x": 0, "y": 0, "panel": None}

        def _make_press(p):
            def _on_press(event):
                drag["x"], drag["y"], drag["panel"] = event.x, event.y, p
            return _on_press

        def _on_drag(event):
            p = drag["panel"]
            if p is None or p["img"] is None or not p["scale"]:
                return
            dx, dy = event.x - drag["x"], event.y - drag["y"]
            drag["x"], drag["y"] = event.x, event.y
            # The image follows the cursor, so the view center moves the
            # other way. Pan only the panel under the cursor.
            p["state"].pan(-dx / (p["scale"] * p["iw"]),
                           -dy / (p["scale"] * p["ih"]))
            _schedule_redraw()

        def _on_double(_event):
            # Global reset: re-fit every panel (zoom + center).
            for p in panels:
                p["state"].fit()
            _schedule_redraw()

        for p in panels:
            c = p["canvas"]
            c.bind("<Configure>", lambda _e: _schedule_redraw())
            c.bind("<MouseWheel>", _make_wheel(p))
            if sys.platform.startswith("linux"):
                # X11 reports wheel scrolling as buttons 4/5, not
                # <MouseWheel>.
                c.bind("<Button-4>", lambda _e, p=p: _zoom_step(1.1, p))
                c.bind("<Button-5>", lambda _e, p=p: _zoom_step(1 / 1.1, p))
            c.bind("<ButtonPress-1>", _make_press(p))
            c.bind("<B1-Motion>", _on_drag)
            c.bind("<Double-Button-1>", _on_double)

        bottom = tk.Frame(win, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=16, pady=(0, 12))
        ttk.Checkbutton(bottom, text=self._t("compare_sync_zoom"),
                        variable=sync_zoom).pack(side="left")
        FlatButton(bottom, text=self._t("close"), command=win.destroy,
                   bg=COLORS["accent"], hover_bg=COLORS["accent_hover"],
                   font=FONT_BUTTON, padx=24, pady=6).pack(side="right")

        bus = UiBus(win)
        schedule = bus.schedule

        def _load_done(p, img):
            p["img"] = img
            p["iw"], p["ih"] = img.size
            _schedule_redraw()

        def _load_failed(p):
            p["error"] = True
            _schedule_redraw()

        def _load_all():
            for p in panels:
                try:
                    # convert() forces the decode here in the worker —
                    # Image.open alone is lazy and would decode on the UI
                    # thread at first paint.
                    img = _open_image_safe(p["path"]).convert("RGB")
                except Exception:
                    schedule(lambda p=p: _load_failed(p))
                else:
                    schedule(lambda p=p, img=img: _load_done(p, img))


        threading.Thread(target=_load_all, daemon=True).start()
        bus.start()
        _schedule_redraw()  # paint the loading placeholders right away

    def _update_stats(self, result: BatchResult = None):
        """Update stats label at the bottom."""
        if result:
            self.stats_label.config(
                text=self._t("stats_result",
                             sin=format_size(result.total_input_size),
                             sout=format_size(result.total_output_size),
                             pct=f"{result.savings_percent:.1f}")
            )
        else:
            total = self._total_size()
            if total > 0:
                self.stats_label.config(
                    text=self._t("stats_files", n=len(self.files),
                                 size=format_size(total))
                )
            else:
                if self.files:
                    # Files listed but unreadable (e.g. moved away)
                    self.stats_label.config(
                        text=self._t("stats_files_only", n=len(self.files)))
                else:
                    self.stats_label.config(text="")

    def _toggle_settings(self, enabled: bool):
        """Enable or disable settings controls during processing."""
        state = "normal" if enabled else "disabled"
        # Toggle major controls — but never Toplevel dialogs: they disable
        # their own action buttons to prevent re-entry (a rename dialog
        # re-enabled mid-batch could fire the rename twice).
        for widget in self.root.winfo_children():
            if isinstance(widget, tk.Toplevel):
                continue
            self._set_state_recursive(widget, state)

    def _set_state_recursive(self, widget, state):
        """Recursively set widget state."""
        try:
            # Keep cancel/preview/start and the file-add buttons enabled:
            # files can be added to the queue while a batch is processing.
            # Gallery export and the compare viewer are read-only on the
            # originals, so they stay up too; review/dedup mutate files
            # and are locked out.
            if widget in (self.cancel_btn, self.start_btn, self.preview_btn,
                          self.add_files_btn, self.add_folder_btn,
                          self.gallery_btn, self.compare_btn):
                return
            if isinstance(widget, (FlatButton, ttk.Combobox, ttk.Scale,
                                   ttk.Entry, ttk.Checkbutton, ttk.Radiobutton)):
                widget.configure(state=state)
        except Exception:
            pass
        for child in widget.winfo_children():
            if child in (self.file_list_canvas, self.progress_bar,
                         self.progress_label):
                continue
            self._set_state_recursive(child, state)


# ── Entry Point ─────────────────────────────────────────────────────────────

def run_gui():
    """Launch the PhotoS GUI application."""
    # Resolve the startup language once (persisted choice > env > system);
    # PhotoSApp.__init__ resolves again but _system_language() is memoized.
    from .. import i18n
    _lang = i18n.resolve_language(use_config=False, use_persisted=True)
    # Use TkinterDnD root window when available so drag-and-drop works
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    root.title(STRINGS[_lang]["window_title"])
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
    root.minsize(MIN_WIDTH, MIN_HEIGHT)
    root.configure(bg=COLORS["bg"])

    app = PhotoSApp(root)

    # Center window on screen (clamped so it never opens off-screen)
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = max(0, (sw - WINDOW_WIDTH) // 2)
    y = max(0, (sh - WINDOW_HEIGHT) // 2)
    root.geometry(f"+{x}+{y}")

    root.mainloop()


if __name__ == "__main__":
    run_gui()
