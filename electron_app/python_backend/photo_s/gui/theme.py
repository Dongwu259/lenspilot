"""Design tokens & platform appearance — photo_s.gui.theme (v2.0).

Single source of truth for the GUI look: color palettes (light/dark),
cross-platform fonts, spacing/radius tokens, window metrics, dark-mode
detection and Windows DPI awareness. Widgets read COLORS[key]/FONT_* at
build time and the palette flips in place via _apply_palette (a UI
rebuild picks it up). No tkinter import — importable headless.
"""

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "PhotoS"


APP_VERSION = "2.5.0"


WINDOW_WIDTH = 1120


WINDOW_HEIGHT = 720


MIN_WIDTH = 980


MIN_HEIGHT = 640


SETTINGS_WIDTH = 400


_LIGHT_COLORS = {
    "bg": "#f5f5f7",
    "card": "#ffffff",
    "border": "#d2d2d7",
    "divider": "#e8e8ed",  # hairline separators / scroll troughs
    "text": "#1d1d1f",
    "text_secondary": "#6e6e73",
    "accent": "#007aff",
    "accent_hover": "#0062cc",
    "danger": "#d70015",
    "danger_hover": "#a80010",
    "success": "#248a3d",
    "warning": "#b25000",
    "row_alt": "#f7f7fa",
    "progress_bg": "#e5e5ea",
}


_DARK_COLORS = {
    "bg": "#1e1e1e",
    "card": "#2c2c2e",
    "border": "#48484a",
    "divider": "#3a3a3c",  # hairline separators / scroll troughs
    "text": "#f5f5f7",
    "text_secondary": "#a1a1a6",
    "accent": "#0a84ff",
    "accent_hover": "#409cff",
    "danger": "#ff453a",
    "danger_hover": "#ff6b61",
    "success": "#30d158",
    "warning": "#ff9f0a",
    "row_alt": "#262628",
    "progress_bg": "#3a3a3c",
}


def _linux_dark_mode() -> bool:
    """Linux desktop appearance: True for dark.

    GNOME: `gsettings get org.gnome.desktop.interface color-scheme` →
    'prefer-dark'. KDE: kdeglobals [General] ColorScheme containing
    "dark" (BreezeDark etc.). Every level degrades silently — a missing
    gsettings or an exotic desktop just means light mode.
    """
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface",
             "color-scheme"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if "dark" in out.lower():
            return True
        if out.startswith("'") or out.startswith('"'):
            return False  # an explicit 'default'/'prefer-light' answer
    except Exception:
        pass
    try:
        import configparser
        kde = Path.home() / ".config" / "kdeglobals"
        if kde.is_file():
            cp = configparser.ConfigParser()
            cp.read(kde, encoding="utf-8")
            scheme = (cp.get("General", "ColorScheme", fallback="")
                      or "").lower()
            if "dark" in scheme:
                return True
    except Exception:
        pass
    return False


def _system_dark_mode() -> bool:
    """Detect the OS appearance: True for dark mode.

    macOS: `defaults read -g AppleInterfaceStyle` → "Dark".
    Windows: AppsUseLightTheme registry value == 0.
    Linux: gsettings color-scheme / kdeglobals (v2.0 — used to fall
           back to light unconditionally).
    Override everything with $PHOTOS_DARK=1 / 0.
    """
    env = os.environ.get("PHOTOS_DARK")
    if env is not None:
        return env.lower() in ("1", "true", "yes", "on")
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            return out == "Dark"
        except Exception:
            return False
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return val == 0
        except Exception:
            return False
    return _linux_dark_mode()


def apply_dpi_awareness() -> None:
    """Windows DPI awareness — call once before any Tk widget is built.

    Escalating chain, first success wins (v2.0 — plain system-aware
    before, which rendered blurry on 150%/200% scaled monitors):
      1. per-monitor v2  (Win10 1703+; crisp per-monitor scaling)
      2. per-monitor v1  (Win8.1+)
      3. system aware    (the old behavior)
    Non-Windows: no-op. Modern Tk picks the right `tk scaling` on its
    own once the process is per-monitor aware.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
            ctx = ctypes.c_void_p(-4)
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system
        except Exception:
            pass
    except Exception:
        pass


# Runtime-mutable palette: widgets read COLORS[key] at build time, so an
# in-place update followed by a UI rebuild switches the theme instantly.
COLORS = dict(_DARK_COLORS if _system_dark_mode() else _LIGHT_COLORS)


def _apply_palette(dark: bool) -> None:
    """Switch the active color palette (in-place; existing widgets unaffected
    until the UI is rebuilt)."""
    COLORS.clear()
    COLORS.update(_DARK_COLORS if dark else _LIGHT_COLORS)


def _detect_fonts():
    """Return the best UI fonts for the current platform.

    Uses well-known system fonts that are guaranteed to exist on each platform.
    Tk will silently fall back to its default if a font is missing.
    """
    if sys.platform == "darwin":
        return {"title": "Helvetica Neue", "body": "Helvetica Neue"}
    elif sys.platform == "win32":
        return {"title": "Segoe UI", "body": "Segoe UI"}
    else:  # Linux and others
        return {"title": "Noto Sans", "body": "Noto Sans"}


PLATFORM_FONTS = _detect_fonts()


FONT_TITLE = (PLATFORM_FONTS["title"], 22, "bold")


FONT_SECTION = (PLATFORM_FONTS["body"], 11, "bold")


FONT_BODY = (PLATFORM_FONTS["body"], 11)


FONT_SMALL = (PLATFORM_FONTS["body"], 10)


FONT_TINY = (PLATFORM_FONTS["body"], 9)


FONT_BUTTON = (PLATFORM_FONTS["body"], 11)


FONT_BUTTON_LG = (PLATFORM_FONTS["body"], 12, "bold")


# Layout tokens (v2.0) — spacing scale + corner radii for new UI code.
# Existing dialogs keep their literal paddings; new modules use these so
# the rhythm stays consistent when the v2.1 workspaces land.
SPACING = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 22}
RADIUS = {"pill": 999, "card": 8, "input": 6}
