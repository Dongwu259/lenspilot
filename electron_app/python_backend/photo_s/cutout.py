"""Cutout / background removal (v2.1.0): mask -> alpha -> transparent output.

Cutout is modeled as a compact string so it serializes into
``ProcessOptions`` and reaches CLI / REST / MCP / presets with zero glue:

    cutout = "subject"                          - salient subject (U2Netp)
            | "person"                          - people (PP-HumanSeg)
            | "object:car"                      - any COCO class (YOLOv8n-seg)
            | "color:255,255,255,tol=30,feather=0[,invert]" - color-key

AI kinds reuse the v1.8 segmentation machinery (``segmask`` via
``mask.render_mask``): model cache / thread lock / clear errors all come
from there, and the weights are already pinned (no new downloads).

The ``color`` kind is a *hard* RGB-distance key (Euclidean, 0-255 units,
``feather`` in absolute pixels) - deliberately different from mask.py's
color mask, which is a soft Gaussian in normalized distance. This is the
practical tool for white-background text / logo sheets.

``cutout_mask`` returns the FOREGROUND selection (float32 h x w, 0..1);
``apply_cutout`` writes it as the alpha channel (source alpha is replaced).
Transparent output needs PNG / WebP / TIFF / AVIF / HEIC; the engine raises
a per-file error for JPEG (flattening the cutout to white would be a silent
failure). Conventions match grade.py / mask.py: functions never mutate
inputs and ``img.info`` is copied onto results.
"""

import math

import numpy as np
from PIL import Image, ImageFilter

__all__ = [
    "CutoutError", "CutoutSpec",
    "parse_cutout", "cutout_mask", "apply_cutout",
]

_AI_KINDS = ("subject", "person", "object")
_COLOR = "color"


class CutoutError(ValueError):
    """Invalid or unparseable cutout spec."""


class CutoutSpec:
    """One parsed cutout: kind + params (mirrors mask.MaskSpec's shape)."""

    __slots__ = ("kind", "label", "rgb", "tol", "feather", "invert")

    def __init__(self, kind, label=None, rgb=None,
                 tol=30.0, feather=0.0, invert=False):
        self.kind = kind
        self.label = label
        self.rgb = rgb
        self.tol = tol
        self.feather = feather
        self.invert = invert

    def __repr__(self):
        return (f"CutoutSpec({self.kind!r}, label={self.label!r}, "
                f"rgb={self.rgb!r}, tol={self.tol}, "
                f"feather={self.feather}, invert={self.invert})")

    def to_string(self) -> str:
        """Serialize back to compact spec form (round-trips)."""
        if self.kind in ("subject", "person"):
            s = self.kind
        elif self.kind == "object":
            s = f"object:{self.label}"
        else:  # color
            s = (f"color:{_fmt_num(self.rgb[0])},{_fmt_num(self.rgb[1])},"
                 f"{_fmt_num(self.rgb[2])}")
            if self.tol != 30.0:
                s += f",tol={_fmt_num(self.tol)}"
        if self.feather:
            s += f",feather={_fmt_num(self.feather)}"
        if self.invert:
            s += ",invert"
        return s


def _fmt_num(v) -> str:
    if float(v) == int(v):
        return str(int(v))
    return f"{float(v):.4g}"


def _req_finite(v: float, what: str, spec: str) -> float:
    """Reject NaN/Inf numeric params — they slip past range checks (agent
    f-strings are real inputs; a NaN tol would silently key nothing)."""
    if not math.isfinite(v):
        raise CutoutError(f"{what} must be finite (got {v!r} in {spec!r})")
    return v


def parse_cutout(spec: str) -> CutoutSpec:
    """Parse a cutout spec: ``subject | person | object:label |
    color:R,G,B[,tol=N][,feather=N][,invert]``.

    Raises :class:`CutoutError` (a ValueError — the engine turns it into a
    per-file error, never a silent no-op).
    """
    if not spec or not spec.strip():
        raise CutoutError("empty cutout spec")
    seg = spec.strip()
    head, _, tail = seg.partition(":")
    m = 0
    while m < len(head) and head[m].isalpha():
        m += 1
    kind = head[:m].lower()
    head_junk = head[m:]
    rest = tail.strip()

    if kind in ("subject", "person"):
        if rest or head_junk:
            raise CutoutError(
                f"bad cutout spec {spec!r}: {kind} takes no params "
                f"(got {(head_junk or rest)!r})")
        return CutoutSpec(kind=kind)

    if kind == "object":
        label = rest.lower()
        if not label or any(c in label for c in ":;,="):
            raise CutoutError(
                f"bad cutout spec {spec!r}: object needs one COCO label "
                f"like 'object:car'")
        return CutoutSpec(kind="object", label=label)

    if kind == _COLOR:
        return _parse_color(rest, spec)

    raise CutoutError(
        f"bad cutout spec {spec!r}: unknown kind {kind!r} "
        f"(valid: subject | person | object:label | color:R,G,B[,tol=..])")


def _parse_color(params: str, spec: str) -> CutoutSpec:
    toks = [t.strip() for t in params.split(",") if t.strip()]
    positional = []
    tol = 30.0
    feather = 0.0
    invert = False
    for tok in toks:
        low = tok.lower()
        if low == "invert":
            invert = True
        elif low.startswith("tol="):
            try:
                tol = _req_finite(
                    float(low.split("=", 1)[1]), "tol value", spec)
            except CutoutError:
                raise
            except ValueError:
                raise CutoutError(
                    f"bad tol value {tok!r} in cutout {spec!r}") from None
        elif low.startswith("feather="):
            try:
                feather = _req_finite(
                    float(low.split("=", 1)[1]), "feather value", spec)
            except CutoutError:
                raise
            except ValueError:
                raise CutoutError(
                    f"bad feather value {tok!r} in cutout {spec!r}") from None
        else:
            try:
                positional.append(_req_finite(float(tok), "numeric param", spec))
            except ValueError:
                raise CutoutError(
                    f"bad numeric param {tok!r} in cutout {spec!r}") from None
    if len(positional) != 3:
        raise CutoutError(
            f"bad cutout spec {spec!r}: color needs exactly r,g,b "
            f"(got {len(positional)} numeric params)")
    r, g, b = (int(round(max(0.0, min(255.0, v)))) for v in positional)
    return CutoutSpec(kind=_COLOR, rgb=(r, g, b),
                      tol=max(0.0, min(255.0, tol)),
                      feather=max(0.0, feather), invert=invert)


def cutout_mask(spec: CutoutSpec, img: Image.Image) -> np.ndarray:
    """Foreground-selection soft mask (float32 h x w, 0..1); alpha = mask.

    AI kinds delegate to ``mask.render_mask`` — segmask dispatch, model
    cache, thread lock and MaskError wrapping come for free.
    """
    if spec.kind in _AI_KINDS:
        from .mask import MaskSpec, render_mask
        params = (spec.label,) if spec.kind == "object" else ()
        return render_mask(
            MaskSpec(spec.kind, params, feather=spec.feather,
                     invert=spec.invert, name="cutout"),
            img.width, img.height, img=img)
    return _color_mask(spec, img)


def _color_mask(spec: CutoutSpec, img: Image.Image) -> np.ndarray:
    """Hard RGB-distance key: pixels within ``tol`` (Euclidean 0-255) of the
    target color become background (alpha 0); ``feather`` blurs the edge by
    that many absolute pixels."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    target = np.array(spec.rgb, dtype=np.float32)
    dist = np.sqrt(((arr - target) ** 2).sum(axis=2))
    mask = (dist > spec.tol).astype(np.float32)
    if spec.feather > 0:
        m = Image.fromarray((mask * 255.0).astype(np.uint8), mode="L")
        m = m.filter(ImageFilter.GaussianBlur(radius=spec.feather))
        mask = np.asarray(m, dtype=np.float32) / 255.0
    if spec.invert:
        mask = 1.0 - mask
    return mask


def apply_cutout(img: Image.Image, spec: CutoutSpec) -> Image.Image:
    """Return an RGBA copy of ``img`` with alpha = cutout mask.

    Source alpha (if any) is replaced — the mask IS the alpha by definition.
    ``img.info`` (EXIF/ICC/DPI) is preserved per the pipeline invariant.
    """
    mask = cutout_mask(spec, img)
    alpha = Image.fromarray(np.round(mask * 255.0).astype(np.uint8), mode="L")
    out = img.convert("RGBA")
    out.info = dict(img.info)
    out.putalpha(alpha)
    return out
