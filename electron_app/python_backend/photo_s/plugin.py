"""
PhotoS - Plugin Discovery & Loading

Discovers third-party plugins via Python entry_points and manages their lifecycle.
"""

import sys
from typing import List, Optional

from .hooks import PhotoSPlugin, PluginContext


_PLUGINS: Optional[List[PhotoSPlugin]] = None


def discover_plugins() -> List[PhotoSPlugin]:
    """Discover and load all registered PhotoS plugins.

    Plugins are discovered via the 'photo_s.plugins' entry_point group.
    Results are cached after first call.

    Returns:
        List of instantiated PhotoSPlugin objects.
    """
    global _PLUGINS
    if _PLUGINS is not None:
        return _PLUGINS

    # Build into a local list and publish only once complete — assigning
    # _PLUGINS up front would let concurrent first callers (parallel batch
    # workers) observe the empty half-built list.
    plugins: List[PhotoSPlugin] = []

    if sys.version_info >= (3, 10):
        from importlib.metadata import entry_points
        eps = entry_points(group="photo_s.plugins")
    else:
        # Python 3.9 compat
        try:
            from importlib.metadata import entry_points
            eps = entry_points().get("photo_s.plugins", [])
        except Exception:
            eps = []

    for ep in eps:
        try:
            plugin_cls = ep.load()
            plugin = plugin_cls()
            plugin.name = ep.name
            plugins.append(plugin)
        except Exception as e:
            # Skip broken plugins, but say so — a plugin that fails to load
            # used to vanish without a trace ("why is my LUT gone?")
            print(f"⚠️  plugin {ep.name!r} failed to load: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

    _PLUGINS = plugins
    return _PLUGINS


def find_provider(operation: str) -> Optional[PhotoSPlugin]:
    """First loaded plugin whose ``provides`` contains ``operation``.

    Slot providers (e.g. SCUNet for "denoise") are looked up here by the
    engine at their pipeline slot. First-wins among loaded plugins; None when
    no plugin provides the operation.
    """
    for plugin in discover_plugins():
        provides = getattr(plugin, "provides", ())
        if provides and operation in provides:
            return plugin
    return None


def clear_cache() -> None:
    """Drop the cached plugin list (test seam / dynamic environments)."""
    global _PLUGINS
    _PLUGINS = None


def run_pre_process(img, options, ctx: PluginContext) -> None:
    """Run on_pre_process hook on all loaded filter plugins.

    Slot providers (``provides`` non-empty) are skipped here — they are
    invoked only at their declared pipeline slot via :func:`find_provider`.
    """
    for plugin in discover_plugins():
        if getattr(plugin, "provides", ()):
            continue  # providers run only at their pipeline slot
        try:
            plugin.on_pre_process(img, options, ctx)
        except Exception as e:
            # plugins must not break the pipeline — but
            # a failing hook must not vanish silently either
            print(f"⚠️  plugin {plugin.name!r} on_pre_process failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)


def run_post_process(result, ctx: PluginContext) -> None:
    """Run on_post_process hook on all loaded filter plugins (see above)."""
    for plugin in discover_plugins():
        if getattr(plugin, "provides", ()):
            continue  # providers run only at their pipeline slot
        try:
            plugin.on_post_process(result, ctx)
        except Exception as e:
            # plugins must not break the pipeline — but
            # a failing hook must not vanish silently either
            print(f"⚠️  plugin {plugin.name!r} on_post_process failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
