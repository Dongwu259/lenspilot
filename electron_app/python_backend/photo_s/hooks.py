"""
PhotoS - Plugin Hook Interface

Third-party plugins extend PhotoS by implementing this interface and
registering via Python entry_points.

Example pyproject.toml:
    [project.entry-points."photo_s.plugins"]
    my-plugin = "my_package:MyPlugin"
"""

from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from .modelstore import WeightSpec


@dataclass
class PluginContext:
    """Context passed to plugin hooks. Plugins can store state here."""
    input_path: str = ""
    output_path: str = ""
    options: Any = None  # ProcessOptions
    metadata: Dict[str, Any] = field(default_factory=dict)


class PhotoSPlugin:
    """Base class for PhotoS plugins.

    Subclass this and override any hooks you need. Register your plugin
    via the 'photo_s.plugins' entry_point group.

    Hook call order:
      1. on_pre_process(img, options, ctx) — before any transformation
      2. on_post_process(result, ctx)      — after save, before cleanup
    """

    # Plugin identifier (set to your entry_point name automatically)
    name: str = ""

    # Operation-provider declaration. When non-empty (e.g. ("denoise",)) the
    # plugin is a *slot provider*: it is EXCLUDED from the generic pre/post
    # hook pass and is invoked only at its declared pipeline slot (see
    # plugin.find_provider / engine denoise slot). Default () keeps the
    # classic filter behavior.
    provides: tuple = ()

    def weight_specs(self) -> List[WeightSpec]:
        """Downloadable model weights for this plugin. Default: none.

        Official plugins with large weights (e.g. ONNX models) expose them
        here; the engine / ``photo-s plugin fetch`` downloads+verifies via
        ``modelstore.ensure`` on first use.
        """
        return []

    def denoise(self, img, strength, ctx: PluginContext):
        """Engine denoise-slot provider. Called when 'denoise' in self.provides.

        Args:
            img: PIL Image (mutable).
            strength: --denoise N value.
            ctx: PluginContext with input_path, options.
        Returns:
            Modified PIL Image.
        Raises:
            Exceptions propagate as per-file errors (unlike generic hooks).
        """
        raise NotImplementedError(
            "{} declares 'denoise' but does not implement denoise()"
            .format(self.name))

    def lut(self, img, lut_path, ctx: PluginContext):
        """Engine lut-slot provider. Called when 'lut' in self.provides.

        Args:
            img: PIL Image (mutable).
            lut_path: --lut .cube path (or preset name the provider resolves).
            ctx: PluginContext with input_path, options.
        Returns:
            Modified PIL Image.
        Raises:
            Exceptions propagate as per-file errors (unlike generic hooks).
        """
        raise NotImplementedError(
            "{} declares 'lut' but does not implement lut()"
            .format(self.name))

    def auto_tone(self, img, strength: float, ctx: PluginContext):
        """Engine auto-tone-slot provider (v2.3). Called when 'auto_tone'
        in self.provides and the user passed ``--auto-tone``.

        Args:
            img: PIL Image.
            strength: 0-1 (``--auto-tone`` value, default 1.0).
            ctx: PluginContext with input_path (the predictor reads the
                original file — pixel-accurate features beat re-saved pixels).
        Returns:
            Adjusted PIL Image (never None — raise for a per-file error).
        """
        raise NotImplementedError(
            "{} declares 'auto_tone' but does not implement auto_tone()"
            .format(self.name))

    def register_mcp_tools(self, mcp) -> None:
        """Surface plugin tools on the MCP server (v2.3 wiring).

        Called once at server startup for every loaded plugin that defines
        this hook — typically ``mcp.add_tool(fn, name=..., description=...)``
        for each plugin capability (see auto-tone's api/mcp_tools.py).
        Exceptions are caught per-plugin and logged, never fatal.
        Default: no extra tools.
        """
        pass

    def register_rest(self, handler_class) -> None:
        """Extend the REST server (v2.3 wiring). Called once at serve
        startup with the ``_PhotoSHandler`` class — typically to add
        dispatch branches / routes (see auto-tone's api/rest.py).
        Exceptions are caught per-plugin and logged, never fatal.
        Default: no extra routes.
        """
        pass

    def on_pre_process(self, img, options, ctx: PluginContext) -> None:
        """Called after image is loaded, before any transformation.

        Args:
            img: PIL Image object (mutable — you can modify it).
            options: ProcessOptions for this image.
            ctx: PluginContext with input_path, metadata dict.
        """
        pass

    def on_post_process(self, result, ctx: PluginContext) -> None:
        """Called after image is saved, before temp file cleanup.

        Args:
            result: ProcessResult with input/output paths, sizes, status.
            ctx: PluginContext with output_path, metadata dict.
        """
        pass
