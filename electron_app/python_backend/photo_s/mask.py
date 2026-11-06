"""Local-adjustment masks (linear / radial / color / AI / brush / combo).

Masks are modeled as compact strings so they serialize into
``ProcessOptions`` and reach CLI / REST / MCP / presets with zero glue:

    masks       = "sky:linear:0.5,0,0.5,1,feather=0.3; face:color:255,200,180,tol=0.15"
    mask_adjust = "sky:exposure=-0.7,sat=0.2; face:brightness=0.1,clarity=0.3"

Each ``masks`` entry is ``[name:]type:params`` (unnamed entries get the
sequential name "1", "2", ...). Coordinates are relative 0-1, so one spec
applies to every image in a batch regardless of resolution. ``mask_adjust``
entries reference masks by name and carry ``key=value`` adjustments applied
under the mask (see :func:`apply_local`).

v1.8 types (opt-in, AI needs ``opencv-python-headless`` + one-time weight
download via ``modelstore``; missing deps raise a clear :class:`MaskError`):

    subject                - salient subject (U2Netp)
    person                 - people (YOLOv8n-seg class person)
    object:car             - any COCO class (YOLOv8n-seg)
    brush:0.5,0.5,0.05|0.6,0.5,0.05   - stroke dots (x, y, radius), '|'-separated
    combo:sky&face         - intersection of two named masks (replaces both)
    combo:sky-face         - difference (sky minus face)

``mask_adjust`` values are scalar floats, except the string-parameter keys
(``curves`` / ``hsl`` / ``color_grading`` / ``vignette`` / ``grain``) which
take the same compact strings as the global grade options - so any grading
can be localized under a mask.

Pure numpy + PIL for geometric masks; AI types lazily import cv2. Conventions
match ``grade.py``: functions never mutate inputs and ``img.info`` is copied
onto results.
"""

import math

import numpy as np
from PIL import Image, ImageFilter

__all__ = [
    "MaskError", "MaskSpec",
    "parse_masks", "parse_mask_adjust",
    "render_mask", "render_all", "combine",
    "apply_local", "ADJUST_KEYS",
]


class MaskError(ValueError):
    """Invalid or unparseable mask spec."""


# AI segmentation + brush + combo are parsed and rendered in v1.8. AI types
# need opencv + one-time weight download; missing deps raise at render time,
# never at parse time (so specs stay serializable on any machine).
_AI_TYPES = ("subject", "person", "object")
_BRUSH = "brush"
_COMBO = "combo"
_V18_TYPES = _AI_TYPES + (_BRUSH, _COMBO)
# name characters forbidden everywhere (incl. brush '|' separators)
_BAD_NAME_CHARS = ":;,= |"


class MaskSpec:
    """One parsed mask: kind + params + feather + invert + name."""

    __slots__ = ("kind", "params", "feather", "invert", "name")

    def __init__(self, kind, params, feather=0.0, invert=False, name=""):
        self.kind = kind
        self.params = params
        self.feather = feather
        self.invert = invert
        self.name = name

    def __repr__(self):
        return (f"MaskSpec({self.kind!r}, {self.params!r}, "
                f"feather={self.feather}, invert={self.invert}, "
                f"name={self.name!r})")

    def to_string(self) -> str:
        """Serialize back to compact spec form (round-trips)."""
        if self.kind in ("subject", "person"):
            base = ""
        elif self.kind == "object":
            base = self.params[0]
        elif self.kind == "brush":
            base = "|".join(
                (f"-{_fmt_num(x)},{_fmt_num(y)},{_fmt_num(-r)}"
                 if r < 0 else
                 f"{_fmt_num(x)},{_fmt_num(y)},{_fmt_num(r)}")
                for x, y, r in self.params)
        elif self.kind == "combo":
            base = f"{self.params[0]}{self.params[1]}{self.params[2]}"
        elif self.kind == "color":
            base = ",".join(_fmt_num(v) for v in self.params[:3])
            base += f",tol={_fmt_num(self.params[3])}"
        else:
            base = ",".join(_fmt_num(v) for v in self.params)
        s = f"{self.kind}:{base}" if base else self.kind
        if self.feather:
            s += f",feather={_fmt_num(self.feather)}"
        if self.invert:
            s += ",invert"
        if self.name and not self.name.isdigit():
            s = f"{self.name}:{s}"
        return s


def _fmt_num(v) -> str:
    if float(v) == int(v):
        return str(int(v))
    return f"{float(v):.4g}"


def _req_finite(v: float, what: str, seg: str) -> float:
    """Reject NaN/Inf numeric params — they slip past range checks and
    silently render as black masks (agent f-strings are real inputs)."""
    if not math.isfinite(v):
        raise MaskError(f"{what} must be finite (got {v!r} in {seg!r})")
    return v


def _extract_tail_keywords(params: str, seg: str):
    """Strip trailing ``,feather=..``/``,invert`` keywords off params.

    ``to_string``/GUI 对每种 kind 都把 feather/invert 追加在段尾
    （brush 落在最后一个 ``|`` 点后），几何类型解析时逐 token 处理，
    v1.8 类型（subject/person/object/brush/combo）需要先剥走。
    返回 (params_without_keywords, feather, invert)。
    """
    feather = 0.0
    invert = False
    pipes = params.split("|")
    toks = pipes[-1].split(",")
    while toks:
        t = toks[-1].strip().lower()
        if t == "invert":
            invert = True
            toks.pop()
        elif t.startswith("feather="):
            try:
                feather = _req_finite(
                    float(t.split("=", 1)[1]), "feather value", seg)
            except MaskError:
                raise
            except ValueError:
                raise MaskError(
                    f"bad feather value {t!r} in mask {seg!r}") from None
            toks.pop()
        else:
            break
    pipes[-1] = ",".join(toks)
    return "|".join(pipes), feather, invert


def _parse_mask_segment(seg: str, index: int) -> MaskSpec:
    """Parse one ``[name:]type:params`` segment into a MaskSpec."""
    seg = seg.strip()
    if not seg:
        raise MaskError("empty mask segment")
    parts = seg.split(":")
    _KNOWN_TYPES = ("linear", "radial", "color") + _V18_TYPES
    if len(parts) == 3:
        name, mtype, params = parts[0].strip(), parts[1].strip().lower(), parts[2]
    elif len(parts) == 2:
        # type token 可能带尾部关键字（"m:subject,feather=0.3"）
        head, _, kws = parts[1].partition(",")
        head = head.strip().lower()
        if head in _KNOWN_TYPES:
            # "main:subject" - named param-less mask (v1.8), not type:params
            name, mtype, params = parts[0].strip(), head, kws
        else:
            name, mtype, params = str(index), parts[0].strip().lower(), parts[1]
    else:
        raise MaskError(
            f"mask segment {seg!r} must be '[name:]type:params' "
            f"(colons separate name/type/params)")
    if mtype not in ("linear", "radial", "color") + _V18_TYPES:
        raise MaskError(
            f"unknown mask type {mtype!r} (expected linear, radial, color, "
            f"subject, person, object, brush or combo)")
    if not name or any(c in name for c in _BAD_NAME_CHARS):
        raise MaskError(f"invalid mask name {name!r} in segment {seg!r}")

    # v1.8 types have their own params syntax.
    if mtype == "subject":
        params, feather, invert = _extract_tail_keywords(params, seg)
        if params.strip():
            raise MaskError(f"subject mask takes no params (got {seg!r})")
        return MaskSpec("subject", (), feather, invert, name)
    if mtype == "person":
        params, feather, invert = _extract_tail_keywords(params, seg)
        if params.strip():
            raise MaskError(f"person mask takes no params (got {seg!r})")
        return MaskSpec("person", (), feather, invert, name)
    if mtype == "object":
        params, feather, invert = _extract_tail_keywords(params, seg)
        label = params.strip().lower()
        # 空格允许（traffic light / hot dog 等 15 个 COCO 类含空格），
        # label 是段末位，空格不会破坏 ;/: 结构
        if not label or any(c in label for c in ":;,="):
            raise MaskError(
                f"object mask needs one COCO label like 'object:car' "
                f"(got {seg!r})")
        return MaskSpec("object", (label,), feather, invert, name)
    if mtype == "brush":
        return _parse_brush(seg, name, params)
    if mtype == "combo":
        return _parse_combo(seg, name, params)

    # Geometric types: positional floats plus feather=/tol= keywords and the
    # "invert" flag, comma-separated.
    positional: list = []
    feather = 0.0
    tol = None
    invert = False
    for tok in params.split(","):
        tok = tok.strip()
        if not tok:
            continue
        low = tok.lower()
        if low == "invert":
            invert = True
            continue
        if low.startswith("feather="):
            feather = _req_finite(
                float(low.split("=", 1)[1]), "feather value", seg)
            continue
        if low.startswith("tol="):
            tol = _req_finite(float(low.split("=", 1)[1]), "tol value", seg)
            continue
        try:
            positional.append(
                _req_finite(float(tok), "numeric param", seg))
        except ValueError:
            raise MaskError(f"bad numeric param {tok!r} in mask {seg!r}") from None

    feather = max(0.0, min(1.0, feather))
    feather_kw = feather > 0.0 or "feather=" in params.lower()
    if mtype == "linear":
        if len(positional) < 4:
            raise MaskError(f"linear mask needs x0,y0,x1,y1 (got {seg!r})")
        if len(positional) > 5:
            raise MaskError(f"too many params in mask {seg!r}")
        if len(positional) == 5 and not feather_kw:
            # positional 5th = feather；关键字显式给出时优先
            feather = max(0.0, min(1.0, positional[4]))
        vals = tuple(max(0.0, min(1.0, v)) for v in positional[:4])
        if vals[0] == vals[2] and vals[1] == vals[3]:
            raise MaskError(f"linear mask axis has zero length (got {seg!r})")
        return MaskSpec("linear", vals, feather, invert, name)
    if mtype == "radial":
        if len(positional) < 4:
            raise MaskError(f"radial mask needs cx,cy,rx,ry (got {seg!r})")
        if len(positional) > 5:
            raise MaskError(f"too many params in mask {seg!r}")
        if len(positional) == 5 and not feather_kw:
            feather = max(0.0, min(1.0, positional[4]))
        cx, cy = max(0.0, min(1.0, positional[0])), max(0.0, min(1.0, positional[1]))
        rx, ry = positional[2], positional[3]
        if rx <= 0 or ry <= 0:
            raise MaskError(f"radial mask radii must be > 0 (got {seg!r})")
        return MaskSpec("radial", (cx, cy, rx, ry), feather, invert, name)
    # color
    if len(positional) < 3:
        raise MaskError(f"color mask needs r,g,b (got {seg!r})")
    if len(positional) > 4:
        raise MaskError(f"too many params in mask {seg!r}")
    r, g, b = (int(round(max(0.0, min(255.0, v)))) for v in positional[:3])
    if tol is None:
        tol = positional[3] if len(positional) == 4 else 0.15
    tol = max(0.02, min(1.0, tol))
    return MaskSpec("color", (r, g, b, tol), feather, invert, name)


def _parse_brush(seg: str, name: str, params: str) -> MaskSpec:
    """Parse ``brush:x,y,r|x,y,r|...`` - stroke dots with relative radius.

    Each dot is ``cx,cy,r``; radius is relative to the shorter image side
    (0.05 = 5% of the short edge). Dots connect into a stroke (capsule
    union) at render time. '|' separates dots so ';' stays the mask-list
    separator.

    A negative dot (``-x,y,r``) subtracts from the mask (v1.8 "subtract
    from mask" mode): the rendered mask is ``pos_union - neg_union``.
    Internally the negative radius is stored as ``-r`` so the spec tuple
    stays (x, y, r) - no structural change.
    """
    params, feather, invert = _extract_tail_keywords(params, seg)
    dots = []
    for dot in params.split("|"):
        dot = dot.strip()
        if not dot:
            continue
        subtract = dot.startswith("-")
        if subtract:
            dot = dot[1:]
        parts = dot.split(",")
        if len(parts) != 3:
            raise MaskError(
                f"brush dot must be 'x,y,r' (got {dot!r} in {seg!r})")
        try:
            x, y, r = (_req_finite(float(p), "brush dot value", seg)
                       for p in parts)
        except ValueError:
            raise MaskError(
                f"brush dot values must be numeric (got {dot!r} in {seg!r})"
            ) from None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise MaskError(f"brush dot coordinates out of range (got {seg!r})")
        if r <= 0.0 or r > 0.5:
            raise MaskError(
                f"brush radius must be in (0, 0.5] (got {dot!r} in {seg!r})")
        dots.append((x, y, -r if subtract else r))
    if not dots:
        raise MaskError(f"brush mask needs at least one dot (got {seg!r})")
    return MaskSpec("brush", tuple(dots), feather, invert, name)


def _parse_combo(seg: str, name: str, params: str) -> MaskSpec:
    """Parse ``combo:A&B`` (intersection) or ``combo:A-B`` (difference).

    References existing named masks; both operands are replaced by the
    combo result at render time (union of the operands would defeat the
    combination). Names must not contain ``&`` or ``-``.
    """
    params, feather, invert = _extract_tail_keywords(params, seg)
    a_op_b = params.strip()
    for op in ("&", "-"):
        if op in a_op_b:
            a, b = a_op_b.split(op, 1)
            a, b = a.strip(), b.strip()
            if not a or not b:
                raise MaskError(f"combo needs two mask names (got {seg!r})")
            if any(c in a + b for c in ":;,= &"):
                raise MaskError(f"invalid mask name in combo (got {seg!r})")
            if a == b:
                raise MaskError(f"combo operands must differ (got {seg!r})")
            if name in (a, b):
                raise MaskError(
                    f"combo {name!r} cannot reference itself (got {seg!r})")
            if "-" in a or "-" in b:
                raise MaskError(
                    f"combo operand names must not contain '-' "
                    f"(ambiguous with the A-B operator, got {seg!r})")
            return MaskSpec("combo", (a, op, b), feather, invert, name)
    raise MaskError(
        f"combo needs 'A&B' or 'A-B' (got {seg!r})")


def parse_masks(s: str) -> list:
    """Parse the ``masks`` option string into a list of MaskSpec."""
    if not s or not s.strip():
        return []
    specs = []
    names = set()
    for i, seg in enumerate(s.split(";")):
        seg = seg.strip()
        if not seg:
            continue
        spec = _parse_mask_segment(seg, i + 1)
        if spec.name in names:
            raise MaskError(f"duplicate mask name {spec.name!r}")
        names.add(spec.name)
        specs.append(spec)
    return specs


# Supported per-mask adjustments. Scalar keys take float values (additive
# deltas unless noted; temp is an absolute Kelvin value, tint the G(-)/M(+)
# axis, blur a Gaussian radius in pixels, sharpen a multiplier offset from
# 1.0). String keys take the same compact strings as the global grade
# options (curves / hsl / color_grading / vignette / grain) - so any
# grading can be localized under a mask.
ADJUST_KEYS = (
    "exposure", "brightness", "contrast", "saturation", "vibrance",
    "clarity", "texture", "sharpen", "temp", "tint", "blur",
)
ADJUST_STRING_KEYS = ("curves", "hsl", "color_grading", "vignette", "grain")
_ALL_ADJUST_KEYS = ADJUST_KEYS + ADJUST_STRING_KEYS


def _split_outside_braces(s: str, sep: str) -> list:
    """Split on ``sep`` while ignoring it inside ``{...}`` groups.

    String-parameter values (curves/hsl/...) are wrapped in ``{}`` so their
    own ``;``/``,`` never collide with the mask_adjust separators.
    """
    out, depth, buf = [], 0, []
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def parse_mask_adjust(s: str) -> dict:
    """Parse ``mask_adjust`` -> ``{name: {key: value}}``.

    ``"sky:exposure=-0.7,sat=0.2"`` -> ``{"sky": {"exposure": -0.7,
    "sat": 0.2}}``. String-parameter keys (curves/hsl/color_grading/
    vignette/grain) keep their compact strings as-is, wrapped in ``{}``
    (e.g. ``sky:curves={r:0,0;128,140;255,255}``) so their own separators
    do not collide. Unknown keys raise :class:`MaskError` (a typo in a
    mask adjustment must not be silently ignored).
    """
    out: dict = {}
    if not s or not s.strip():
        return out
    for seg in _split_outside_braces(s, ";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            raise MaskError(
                f"mask_adjust segment {seg!r} must be 'name:key=value,...'")
        name, rest = seg.split(":", 1)
        name = name.strip()
        adjust = {}
        for item in _split_outside_braces(rest, ","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise MaskError(
                    f"mask_adjust item {item!r} must be 'key=value'")
            key, val = item.split("=", 1)
            key = key.strip().lower()
            if key not in _ALL_ADJUST_KEYS:
                raise MaskError(
                    f"unknown mask adjustment {key!r} "
                    f"(expected {','.join(_ALL_ADJUST_KEYS)})")
            if key in ADJUST_STRING_KEYS:
                val = val.strip()
                if val.startswith("{") and val.endswith("}"):
                    val = val[1:-1]
                if not val:
                    raise MaskError(
                        f"mask_adjust value for {key!r} must not be empty")
                adjust[key] = val
                continue
            try:
                adjust[key] = _req_finite(
                    float(val), f"mask_adjust value for {key!r}", seg)
            except MaskError:
                raise  # _req_finite 的 finite 信息比通用 numeric 更精确
            except ValueError:
                raise MaskError(
                    f"mask_adjust value for {key!r} must be numeric "
                    f"(got {val!r})") from None
        if not name or not adjust:
            raise MaskError(f"bad mask_adjust segment {seg!r}")
        if name in out:
            # 同蒙版多段合并（GUI 每蒙版一段；agent 手写
            # "a:exposure=..;a:brightness=.." 是自然写法——
            # 覆盖会静默丢前段，拒绝则误伤合法输入）
            out[name].update(adjust)
        else:
            out[name] = adjust
    return out


# ── Rendering ────────────────────────────────────────────────────────────────

def _coords(w: int, h: int):
    """Normalized coordinate grids (x in 0..1 columns, y in 0..1 rows)."""
    ys, xs = np.mgrid[0:h, 0:w]
    x = xs.astype(np.float32) / max(1, w - 1)
    y = ys.astype(np.float32) / max(1, h - 1)
    return x, y


def render_mask(spec: MaskSpec, w: int, h: int,
                img: Image.Image = None,
                refs: dict = None,
                _stack: tuple = None) -> np.ndarray:
    """Render one mask as a float32 ``h x w`` array in 0..1.

    ``img`` is required for color masks (they measure the image's own
    pixels) and AI masks (they segment it); geometric masks ignore it.
    ``refs`` (name -> MaskSpec) is required for combo masks, which combine
    two referenced masks; combo recursion is depth-limited (64) and
    cycle-checked — a cycle raises :class:`MaskError` with the chain path
    instead of ``RecursionError``. ``_stack`` is the internal combo-name
    path for cycle/depth detection (not part of the public API).
    """
    x, y = _coords(w, h)
    if spec.kind in _AI_TYPES:
        if img is None:
            raise MaskError(
                f"{spec.kind} mask needs the image to segment")
        mask = _ai_mask(img, spec)
    elif spec.kind == "brush":
        mask = _brush_mask(spec, w, h)
    elif spec.kind == "combo":
        if not refs:
            raise MaskError(
                f"combo mask {spec.name!r} needs the full mask list "
                f"(render_all/engine passes refs)")
        stack = _stack or ()
        if len(stack) >= 64:
            raise MaskError(
                f"combo chain too deep: "
                f"{' -> '.join(stack + (spec.name,))}")
        if spec.name in stack:
            raise MaskError(
                f"combo cycle detected: "
                f"{' -> '.join(stack + (spec.name,))}")
        stack = stack + (spec.name,)
        a_name, op, b_name = spec.params
        for n in (a_name, b_name):
            if n not in refs:
                raise MaskError(
                    f"combo references unknown mask {n!r} (has {sorted(refs)})")
        a = render_mask(refs[a_name], w, h, img=img, refs=refs, _stack=stack)
        b = render_mask(refs[b_name], w, h, img=img, refs=refs, _stack=stack)
        mask = np.minimum(a, b) if op == "&" else np.clip(a - b, 0.0, 1.0)
    elif spec.kind == "linear":
        x0, y0, x1, y1 = spec.params
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy
        # Projection of each pixel onto the gradient axis, 0 at the start
        # point and 1 at the end point: a linear ramp perpendicular to the
        # axis (the Lightroom linear-gradient shape).
        t = ((x - x0) * dx + (y - y0) * dy) / length2
        mask = np.clip(t, 0.0, 1.0)
    elif spec.kind == "radial":
        cx, cy, rx, ry = spec.params
        d = np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2)
        feather = spec.feather if spec.feather > 0 else 0.05
        inner = max(0.0, 1.0 - feather)
        mask = np.clip((1.0 - d) / (1.0 - inner), 0.0, 1.0)
    elif spec.kind == "color":
        if img is None:
            raise MaskError(
                "color mask needs the image to measure against")
        mask = _color_mask(img, spec)
    else:
        # GUI 直接构造 MaskSpec 时可能拼错 kind —— 明确报错而非
        # 落入 color 分支给出误导信息
        raise MaskError(f"unknown mask kind {spec.kind!r}")
    # radial 的软边已由其椭圆公式（inner = 1 - feather）承担——再加
    # 高斯会双倍羽化；linear/brush/AI 的 feather 由这里的高斯承担
    if spec.feather > 0 and spec.kind not in ("color", "combo", "radial"):
        mask = _feather_mask(mask, spec.feather, w, h)
    if spec.invert:
        mask = 1.0 - mask
    return mask.astype(np.float32)


def _ai_mask(img: Image.Image, spec: MaskSpec) -> np.ndarray:
    """AI segmentation (subject/person/object) - lazily imports segmask.

    Raises a clear :class:`MaskError` when opencv or the model weights are
    missing (one-time download via modelstore on first use).
    """
    from .segmask import segment
    try:
        if spec.kind == "subject":
            return segment(img, "subject")
        if spec.kind == "person":
            return segment(img, "person")
        return segment(img, "object", label=spec.params[0])
    except (ImportError, RuntimeError) as e:
        raise MaskError(
            f"AI mask {spec.name!r} ({spec.kind}): {e}") from e
    except Exception as e:  # cv2.error / 形状异常：per-file 契约要清晰错误
        raise MaskError(
            f"AI mask {spec.name!r} ({spec.kind}) failed: {e}") from e


def _brush_mask(spec: MaskSpec, w: int, h: int) -> np.ndarray:
    """Stroke mask: union of capsule shapes between dot centers.

    Dots are relative ``(x, y, r)``; radius is relative to the shorter
    image side. Each consecutive pair of dots forms a capsule (line swept
    by the brush) so fast strokes paint continuous paths.

    Negative dots (radius stored as ``-r``) subtract from the result:
    ``mask = pos_union - neg_union`` (v1.8 subtract-from-mask mode).
    """
    import numpy as np
    short = float(min(w, h))
    ys, xs = np.mgrid[0:h, 0:w]
    xs = xs.astype(np.float32)
    ys = ys.astype(np.float32)
    pos = np.zeros((h, w), dtype=np.float32)
    neg = np.zeros((h, w), dtype=np.float32)
    dots = spec.params

    def _stroke(target, cx, cy, r):
        px, py = cx * max(1, w - 1), cy * max(1, h - 1)
        rad = max(0.5, r * short)
        d2 = (xs - px) ** 2 + (ys - py) ** 2
        np.maximum(target, np.exp(-d2 / (2.0 * (rad * 0.5) ** 2)),
                   out=target)

    for i, (cx, cy, r) in enumerate(dots):
        target = neg if r < 0 else pos
        _stroke(target, cx, cy, abs(r))
        if i + 1 < len(dots):
            nx, ny, nr = dots[i + 1]
            if (nr < 0) != (r < 0):
                continue  # sign change: no capsule across the boundary
            target = neg if nr < 0 else pos
            np.maximum(
                target, _capsule(xs, ys, cx * max(1, w - 1),
                                 cy * max(1, h - 1),
                                 nx * max(1, w - 1), ny * max(1, h - 1),
                                 max(0.5, abs(nr) * short)),
                out=target)
    return np.clip(pos - neg, 0.0, 1.0)


def _capsule(xs, ys, x0, y0, x1, y1, rad):
    """Soft capsule between (x0, y0) and (x1, y1), radius ``rad`` px."""
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    if length2 == 0:
        d2 = (xs - x0) ** 2 + (ys - y0) ** 2
    else:
        t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / length2, 0.0, 1.0)
        d2 = (xs - (x0 + t * dx)) ** 2 + (ys - (y0 + t * dy)) ** 2
    return np.exp(-d2 / (2.0 * (rad * 0.5) ** 2))


def _color_mask(img: Image.Image, spec: MaskSpec) -> np.ndarray:
    """Soft mask by perceptual distance from a sampled color.

    Distance combines angular hue distance with saturation and value
    deltas (each normalized to 0..1), so a saturated sample ignores gray
    pixels via the saturation term instead of via noisy hue readings.
    """
    import colorsys
    r, g, b, tol = spec.params
    th, ts, tv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    target_h = th * 360.0
    from .grade import _to_hsv
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    hue, sat, val = _to_hsv(arr)
    hue_deg = hue * 360.0
    d = (hue_deg - target_h) % 360.0
    dh = np.where(d > 180.0, d - 360.0, d) / 180.0
    ds = np.abs(sat - ts)
    dv = np.abs(val - tv)
    dist = np.sqrt(dh * dh + ds * ds + dv * dv)
    mask = np.exp(-(dist ** 2) / (2.0 * tol * tol))
    # Gray pixels (sat near 0) have meaningless hue - zero them out unless
    # the target itself is near-gray.
    if ts > 0.1:
        mask = mask * np.clip(sat / 0.1, 0.0, 1.0)
    return mask


def _feather_mask(mask: np.ndarray, feather: float, w: int, h: int):
    """Gaussian-feather a mask; sigma scales with the shorter image side."""
    sigma = max(0.5, feather * min(w, h) / 8.0)
    m = Image.fromarray((mask * 255.0).astype(np.uint8), mode="L")
    m = m.filter(ImageFilter.GaussianBlur(radius=sigma))
    return np.asarray(m, dtype=np.float32) / 255.0


def render_all(specs, w: int, h: int, img: Image.Image = None) -> dict:
    """Render every spec -> ``{name: float32 h x w array}``.

    ``img`` is required when any spec is a color or AI mask; combo masks
    resolve their references from the full spec list.
    """
    refs = {s.name: s for s in specs}
    return {spec.name: render_mask(spec, w, h, img=img, refs=refs)
            for spec in specs}


def combine(masks) -> np.ndarray:
    """Combine rendered masks with union (max) semantics."""
    out = None
    for m in masks:
        out = m if out is None else np.maximum(out, m)
    if out is None:
        raise MaskError("combine() needs at least one mask")
    return out


# ── Local adjustments ────────────────────────────────────────────────────────

def _parse_adjust_string(key: str, value: str, parser):
    """Parse a string-parameter adjustment (same compact strings as the
    global grade options); failures become :class:`MaskError` with the key
    name so per-file errors stay actionable."""
    try:
        return parser(value)
    except (ValueError, TypeError) as e:
        raise MaskError(f"bad {key} string: {e}") from None


def apply_local(img: Image.Image, mask: np.ndarray,
                adjust: dict) -> Image.Image:
    """Apply scalar adjustments under a mask: ``out = mix(orig, adjusted)``.

    ``adjust`` keys come from :data:`ADJUST_KEYS`; each engine function is
    reused as-is so local and global grading stay numerically identical.
    The mask is a float32 0..1 array matching the image size; the result
    is the original outside the mask and the fully-adjusted image inside.
    """
    if not adjust:
        return img
    m = np.asarray(mask, dtype=np.float32)
    if m.shape != (img.height, img.width):
        raise MaskError(
            f"mask shape {m.shape} does not match image "
            f"({img.width}x{img.height})")
    if not m.any():
        return img  # empty mask - nothing to adjust

    out = img
    if adjust.get("exposure"):
        from .adjust import apply_exposure
        out = apply_exposure(out, ev=adjust["exposure"])
    if any(adjust.get(k) for k in ("brightness", "contrast", "saturation",
                                   "sharpen")):
        from .adjust import apply_tone_adjustments
        out = apply_tone_adjustments(
            out,
            brightness=1.0 + adjust.get("brightness", 0.0),
            contrast=1.0 + adjust.get("contrast", 0.0),
            saturation=1.0 + adjust.get("saturation", 0.0),
            sharpen=1.0 + adjust.get("sharpen", 0.0))
    if adjust.get("temp"):
        from .adjust import apply_white_balance
        out = apply_white_balance(out, temp=adjust["temp"])
    if adjust.get("tint"):
        from .adjust import apply_white_balance
        out = apply_white_balance(out, tint=adjust["tint"])
    if adjust.get("vibrance"):
        from .grade import apply_vibrance
        out = apply_vibrance(out, adjust["vibrance"])
    if adjust.get("clarity"):
        from .grade import apply_clarity
        out = apply_clarity(out, adjust["clarity"])
    if adjust.get("texture"):
        from .grade import apply_texture
        out = apply_texture(out, adjust["texture"])
    # String-parameter adjustments (v1.8): same compact strings as the
    # global grade options, localized under the mask.
    if adjust.get("curves"):
        from .grade import apply_curves, _parse_curves
        out = apply_curves(
            out, _parse_adjust_string("curves", adjust["curves"],
                                      _parse_curves))
    if adjust.get("hsl"):
        from .grade import apply_hsl, _parse_hsl
        out = apply_hsl(
            out, _parse_adjust_string("hsl", adjust["hsl"], _parse_hsl))
    if adjust.get("color_grading"):
        from .grade import apply_color_grading, _parse_color_grading
        z = _parse_adjust_string("color_grading", adjust["color_grading"],
                                 _parse_color_grading)
        out = apply_color_grading(
            out, shadows=z.get("shadows"), midtones=z.get("midtones"),
            highlights=z.get("highlights"))
    if adjust.get("vignette"):
        from .grade import apply_vignette, _parse_vignette
        out = apply_vignette(
            out, *_parse_adjust_string("vignette", adjust["vignette"],
                                       _parse_vignette))
    if adjust.get("grain"):
        from .grade import apply_grain, _parse_grain
        out = apply_grain(
            out, *_parse_adjust_string("grain", adjust["grain"],
                                       _parse_grain))
    if adjust.get("blur", 0) > 0:
        radius = max(0.1, min(50.0, adjust["blur"]))
        if out.mode not in ("RGB", "RGBA", "L"):
            out = out.convert("RGBA")
        out = out.filter(ImageFilter.GaussianBlur(radius=radius))

    if out is img:  # nothing actually adjusted
        return img
    # Blend: fully-adjusted inside the mask, original outside.
    a = np.asarray(img.convert("RGBA"), dtype=np.float32)
    b = np.asarray(out.convert("RGBA"), dtype=np.float32)
    mixed = a * (1.0 - m[..., np.newaxis]) + b * m[..., np.newaxis]
    result = Image.fromarray(np.round(mixed).astype(np.uint8), mode="RGBA")
    if img.mode in ("RGB", "RGBA", "L"):
        result = result.convert(img.mode)
    else:
        result = result.convert("RGB")
    result.info = img.info.copy()
    return result
