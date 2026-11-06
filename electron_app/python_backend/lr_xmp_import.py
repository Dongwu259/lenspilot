"""lr_xmp_import.py — Lightroom XMP 读写（照片 sidecar 只读；预设可导入/导出）

读取：LR 写在照片 sidecar 里的 crs: 参数 → LensPilot 编辑参数映射。
写入：只写用户明确选择的「预设 .xmp」文件（不是照片 sidecar，绝不覆盖 LR 的
照片 sidecar），供 Lightroom「导入配置文件与预设」使用。

映射表（近似处已注明）：
  Exposure2012 → exposure (EV 直读)
  Contrast2012 → contrast 1+v/100（LR 2012 对比度是色调曲线，近似）
  Highlights2012/Shadows2012 → highlights/shadows v/100
  Temperature → wb_temp (K)；Tint → wb_tint
  Vibrance → vibrance v/100；Saturation → saturation 1+v/100
  Clarity2012/Texture/Dehaze → 同名 v/100；Sharpness(0-150) → sharpness v/75
  ToneCurvePV2012 + Red/Green/Blue → curves（0-255 点位直读）
  HueAdjustment{8色} → hsl[c][0] = v*0.3 度（LR ±100 ≈ ±30°，近似）
  Saturation/LuminanceAdjustment{8色} → hsl[c][1]/[2] = v/100
  ColorGrade{Shadow,Mitone,Highlight}{Hue,Sat,Lum} → color_grading 三向
  PostCropVignette* → vignette；Grain/GrainSize → grain；LuminanceSmoothing → denoise
  ConvertToGrayscale → grayscale
明确不映射（返回 unmatched，多为 LensPilot 无对应项）：Whites/Blacks 2012、
参数曲线、锐化细节子项、色差/几何校正、色彩校准(Calibration)、点颜色、IAWD 等。
导出时 LUT/LOG/蒙版/污点/镜头校正/校准/auto_tone/几何等会列入 unsupported。
"""

import os
import uuid
import xml.etree.ElementTree as ET

import command_registry
from command_registry import command
from service import send_log

_CRS_URI = "http://ns.adobe.com/camera-raw-settings/1.0/"
_RDF_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_CRS_NS = "{" + _CRS_URI + "}"
_RDF_NS = "{" + _RDF_URI + "}"
_X_NS = "adobe:ns:meta/"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_XMP_NS = "http://ns.adobe.com/xap/1.0/"

_HSL_COLORS = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]

# LR(HSL) 色相 ±100 ≈ ±30° 的近似换算
_HUE_FACTOR = 0.3


def find_sidecar(img_path):
    """LR sidecar 命名兼容：<name>.<ext>.xmp 与 <name>.xmp 两种都找。"""
    for cand in (img_path + ".xmp", os.path.splitext(img_path)[0] + ".xmp"):
        if os.path.isfile(cand):
            return cand
    return None


def _read_crs_dict(xmp_path):
    """收集 crs: 参数（同时兼容子元素与 rdf:Description 属性两种写法）。"""
    root = ET.parse(xmp_path).getroot()
    out = {}

    def put(key, val):
        k = key[len(_CRS_NS):]
        if k not in out:
            out[k] = (val or "").strip()

    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith(_CRS_NS):
            val = el.text
            if not (val or "").strip():
                lis = el.findall(f".//{_RDF_NS}li")
                texts = [(li.text or "").strip() for li in lis
                         if (li.text or "").strip()]
                if texts:
                    if el.tag.startswith(_CRS_NS + "ToneCurve"):
                        # <rdf:Seq> 曲线点：拼成 "(0, 0), (128, 140)" 供 _tone_curve
                        val = ", ".join(f"({t})" for t in texts)
                    else:
                        val = texts[0]
            put(el.tag, val)
        for k, v in el.attrib.items():
            if k.startswith(_CRS_NS):
                put(k, v)
    return out


def _num(crs, key, default=None):
    v = crs.get(key)
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace("+", ""))
    except ValueError:
        return default


def _tone_curve(val):
    """LR 曲线串 "(0, 0), (128, 140), (255, 255)" → [[x,y],...]（0-255）。"""
    pts = []
    for pair in str(val).replace("), (", ")|(").split("|"):
        pair = pair.strip().strip("()")
        a, _, b = pair.partition(",")
        try:
            pts.append([int(round(float(a))), int(round(float(b)))])
        except ValueError:
            return None
    return pts if len(pts) >= 2 else None


def import_crs(crs):
    """crs dict → (edits, matched_keys, unmatched_keys)。只含非默认项。"""
    edits, unmatched = {}, []

    def put(edit_key, value, crs_key):
        edits[edit_key] = value

    def f(key, default=None):
        return _num(crs, key, default)

    ev = f("Exposure2012")
    if ev is not None and abs(ev) > 1e-4:
        put("exposure", round(max(-3.0, min(3.0, ev)), 3), "Exposure2012")
    ct = f("Contrast2012")
    if ct is not None and abs(ct) > 1e-4:
        put("contrast", round(1.0 + max(-100, min(100, ct)) / 100.0, 3), "Contrast2012")
    hl, sh = f("Highlights2012"), f("Shadows2012")
    if hl is not None and abs(hl) > 1e-4:
        put("highlights", round(max(-1, min(1, hl / 100.0)), 3), "Highlights2012")
    if sh is not None and abs(sh) > 1e-4:
        put("shadows", round(max(-1, min(1, sh / 100.0)), 3), "Shadows2012")
    temp = f("Temperature")
    if temp is not None and abs(temp) > 1:
        put("wb_temp", int(round(temp)), "Temperature")
    tint = f("Tint")
    if tint is not None and abs(tint) > 1e-4:
        put("wb_tint", int(round(max(-100, min(100, tint)))), "Tint")
    vib = f("Vibrance")
    if vib is not None and abs(vib) > 1e-4:
        put("vibrance", round(max(-1, min(1, vib / 100.0)), 3), "Vibrance")
    sat = f("Saturation")
    if sat is not None and abs(sat) > 1e-4:
        put("saturation", round(1.0 + max(-100, min(100, sat)) / 100.0, 3), "Saturation")
    for crs_key, edit_key in (("Clarity2012", "clarity"), ("Texture", "texture"),
                              ("Dehaze", "dehaze")):
        v = f(crs_key)
        if v is not None and abs(v) > 1e-4:
            put(edit_key, round(max(-1, min(1, v / 100.0)), 3), crs_key)
    sp = f("Sharpness")
    if sp is not None and sp > 1e-4:
        put("sharpness", round(max(0, min(2.0, sp / 75.0)), 3), "Sharpness")

    # 点曲线
    curves = {}
    for crs_key, ch in (("ToneCurvePV2012", "rgb"), ("ToneCurveRed2012", "r"),
                        ("ToneCurveGreen2012", "g"), ("ToneCurveBlue2012", "b")):
        pts = _tone_curve(crs[crs_key]) if crs.get(crs_key) else None
        if pts and not (ch == "rgb" and pts == [[0, 0], [255, 255]]):
            curves[ch] = pts
    if curves:
        put("curves", curves, "ToneCurve*")

    # HSL 8 色域
    hsl = {}
    for c in _HSL_COLORS:
        dh = f(f"HueAdjustment{c}")
        ds = f(f"SaturationAdjustment{c}")
        dl = f(f"LuminanceAdjustment{c}")
        if dh is None and ds is None and dl is None:
            continue
        triple = [round((dh or 0.0) * _HUE_FACTOR, 1),
                  round(max(-1, min(1, (ds or 0.0) / 100.0)), 3),
                  round(max(-1, min(1, (dl or 0.0) / 100.0)), 3)]
        if any(abs(x) > 1e-4 for x in triple):
            hsl[c.lower()] = triple
    if hsl:
        put("hsl", hsl, "HSL*")

    # 三向颜色分级（新 ColorGrade* 优先，旧 SplitToning 回退）
    grading = {}
    for zone, prefix in (("shadows", "Shadow"), ("midtones", "Midtone"),
                         ("highlights", "Highlight")):
        hue = f(f"ColorGrade{prefix}Hue")
        s = f(f"ColorGrade{prefix}Sat")
        l = f(f"ColorGrade{prefix}Lum")
        if (hue is None or hue == 0) and not s and not l:
            continue
        if hue is None and s is None and l is None:
            continue
        triple = [int(round(hue if hue is not None else 0)),
                  round(max(0, min(1, (s or 0.0) / 100.0)), 3),
                  round(max(-1, min(1, (l or 0.0) / 100.0)), 3)]
        if triple[1] > 1e-4 or abs(triple[2]) > 1e-4:
            grading[zone] = triple
    if not grading:  # 旧版分离色调：高光/阴影 hue+sat
        for zone, prefix in (("shadows", "Shadow"), ("highlights", "Highlight")):
            hue = f(f"SplitToning{prefix}Hue")
            s = f(f"SplitToning{prefix}Saturation")
            if hue is not None and s and s > 0:
                grading[zone] = [int(round(hue)), round(min(1, s / 100.0), 3), 0.0]
    if grading:
        put("color_grading", grading, "ColorGrade*/SplitToning*")

    # 暗角 / 颗粒 / 降噪 / 黑白
    vg = f("PostCropVignetteAmount")
    if vg is not None and abs(vg) > 1e-4:
        mid = f("PostCropVignetteMidpoint", 50.0)
        feather = f("PostCropVignetteFeather", 50.0)
        put("vignette", [round(max(-1, min(1, vg / 100.0)), 3),
                         round(max(0, min(1, (mid or 50.0) / 100.0)), 3),
                         round(max(0, min(1, (feather or 50.0) / 100.0)), 3)],
            "PostCropVignette*")
    gr = f("Grain")
    if gr is not None and gr > 1e-4:
        gs = f("GrainSize", 50.0)
        put("grain", [round(max(0, min(1, gr / 100.0)), 3),
                      round(max(0.5, min(3.0, (gs or 50.0) / 50.0)), 2)], "Grain*")
    smo = f("LuminanceSmoothing")
    if smo is not None and smo > 1e-4:
        put("denoise", int(round(max(0, min(20, smo / 5.0)))), "LuminanceSmoothing")
    gray = crs.get("ConvertToGrayscale", "").lower() in ("true", "1")
    if gray:
        put("grayscale", True, "ConvertToGrayscale")

    # unmatched = 未能消费的键（已知无对应项如 Whites2012 / 参数曲线也如实列出）
    used = {"Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
            "Temperature", "Tint", "Vibrance", "Saturation", "Clarity2012",
            "Texture", "Dehaze", "Sharpness", "ConvertToGrayscale",
            "LuminanceSmoothing",
            # 预设元数据（不是可映射的调整项）
            "Name", "PresetType", "ProcessVersion", "Version", "WhiteBalance",
            "HasSettings", "UUID", "CreatorTool", "Cluster", "Copyright",
            "ContactInfo", "SupportsAmount", "SupportsColor",
            "SupportsMonochrome", "SupportsHighDynamicRange",
            "SupportsNormalDynamicRange", "SupportsSceneReferred",
            "SupportsOutputReferred"}
    for k in crs:
        if k.startswith(("ToneCurve", "HueAdjustment", "SaturationAdjustment",
                         "LuminanceAdjustment", "PostCropVignette", "Grain")):
            used.add(k)
        elif k.startswith("SplitToning") and not k.endswith("Balance"):
            used.add(k)
        elif k.startswith(("ColorGradeShadow", "ColorGradeMidtone", "ColorGradeHighlight")):
            used.add(k)
    unmatched = [k for k in crs if k not in used]
    return edits, unmatched


# ── 预设导出（LensPilot edits → LR 预设 XMP） ──────────────────────────────

def _fmt(v):
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int):
        return str(v)
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _levels_curve(levels):
    """levels [black, white, gamma] → 5 点曲线（近似 apply_levels 的 LUT）。"""
    try:
        black, white, gamma = float(levels[0]), float(levels[1]), float(levels[2])
    except (TypeError, ValueError, IndexError):
        return None
    span = max(1.0, white - black)
    inv = 1.0 / max(0.05, gamma)
    pts = []
    for v in (0, 64, 128, 192, 255):
        t = 0.0 if v <= black else (1.0 if v >= white else (v - black) / span)
        pts.append([v, int(round(255 * (t ** inv)))])
    return pts


def edits_to_crs(edits):
    """LensPilot edits → (crs attrs dict, curve seq dict, unsupported keys)。"""
    e = edits or {}
    attrs, curves, unsupported = {}, {}, []

    def num(k, default=0.0):
        try:
            v = e.get(k)
            return default if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return default

    ev = num("exposure")
    if abs(ev) > 1e-4:
        attrs["Exposure2012"] = _fmt(max(-5.0, min(5.0, ev)))
    ct = num("contrast", 1.0)
    if abs(ct - 1.0) > 1e-4:
        attrs["Contrast2012"] = _fmt(max(-100.0, min(100.0, (ct - 1.0) * 100.0)))
    for k, crs_key in (("highlights", "Highlights2012"), ("shadows", "Shadows2012")):
        v = num(k)
        if abs(v) > 1e-4:
            attrs[crs_key] = _fmt(max(-100.0, min(100.0, v * 100.0)))

    temp, warmth, tint = num("wb_temp"), num("warmth"), num("wb_tint")
    if abs(temp) > 0.5 or abs(warmth) > 1e-4:
        kelvin = temp if abs(temp) > 0.5 else 5500.0
        kelvin += warmth * 2000.0
        attrs["Temperature"] = str(int(round(max(2000.0, min(12000.0, kelvin)))))
    if abs(tint) > 1e-4:
        attrs["Tint"] = _fmt(max(-100.0, min(100.0, tint)))
        if "Temperature" not in attrs:
            attrs["Temperature"] = "5500"

    vib = num("vibrance")
    if abs(vib) > 1e-4:
        attrs["Vibrance"] = _fmt(max(-100.0, min(100.0, vib * 100.0)))
    sat = num("saturation", 1.0)
    if abs(sat - 1.0) > 1e-4:
        attrs["Saturation"] = _fmt(max(-100.0, min(100.0, (sat - 1.0) * 100.0)))
    for k, crs_key in (("clarity", "Clarity2012"), ("texture", "Texture"),
                       ("dehaze", "Dehaze")):
        v = num(k)
        if abs(v) > 1e-4:
            attrs[crs_key] = _fmt(max(-100.0, min(100.0, v * 100.0)))
    sharp = num("sharpness")
    if abs(sharp) > 1e-4:
        attrs["Sharpness"] = _fmt(max(0.0, min(150.0, sharp * 75.0)))
    denoise = num("denoise")
    if abs(denoise) > 1e-4:
        attrs["LuminanceSmoothing"] = _fmt(max(0.0, min(100.0, denoise * 5.0)))

    hsl = e.get("hsl") or {}
    if isinstance(hsl, dict):
        for c in _HSL_COLORS:
            v = hsl.get(c.lower())
            if not isinstance(v, (list, tuple)) or len(v) < 3:
                continue
            try:
                dh, ds, dl = float(v[0] or 0), float(v[1] or 0), float(v[2] or 0)
            except (TypeError, ValueError):
                continue
            if abs(dh) > 1e-4:
                attrs[f"HueAdjustment{c}"] = _fmt(
                    max(-100.0, min(100.0, dh / _HUE_FACTOR)))
            if abs(ds) > 1e-4:
                attrs[f"SaturationAdjustment{c}"] = _fmt(
                    max(-100.0, min(100.0, ds * 100.0)))
            if abs(dl) > 1e-4:
                attrs[f"LuminanceAdjustment{c}"] = _fmt(
                    max(-100.0, min(100.0, dl * 100.0)))

    cg = e.get("color_grading") or {}
    if isinstance(cg, dict):
        for zone, prefix in (("shadows", "Shadow"), ("midtones", "Midtone"),
                             ("highlights", "Highlight")):
            v = cg.get(zone)
            if not isinstance(v, (list, tuple)) or len(v) < 2:
                continue
            try:
                hue = int(round(float(v[0] or 0))) % 360
                sat = max(0.0, min(1.0, float(v[1] or 0)))
                lum = max(-1.0, min(1.0, float(v[2]) if len(v) > 2 and v[2] is not None else 0.0))
            except (TypeError, ValueError):
                continue
            if sat > 1e-4 or abs(lum) > 1e-4:
                attrs[f"ColorGrade{prefix}Hue"] = str(hue)
                attrs[f"ColorGrade{prefix}Sat"] = _fmt(sat * 100.0)
                attrs[f"ColorGrade{prefix}Lum"] = _fmt(lum * 100.0)

    cv = e.get("curves") or {}
    if isinstance(cv, dict):
        for ch, key in (("rgb", "ToneCurvePV2012"), ("r", "ToneCurveRed2012"),
                        ("g", "ToneCurveGreen2012"), ("b", "ToneCurveBlue2012")):
            pts = cv.get(ch)
            if isinstance(pts, (list, tuple)) and len(pts) >= 2:
                try:
                    curves[key] = [[int(round(float(p[0]))), int(round(float(p[1])))]
                                   for p in pts]
                except (TypeError, ValueError, IndexError):
                    pass
    levels = e.get("levels")
    if levels:
        if "ToneCurvePV2012" in curves:
            unsupported.append("levels")
        else:
            curve = _levels_curve(levels)
            if curve:
                curves["ToneCurvePV2012"] = curve

    vg = e.get("vignette")
    if isinstance(vg, (list, tuple)) and vg:
        try:
            amount = float(vg[0] or 0)
            mid = float(vg[1]) if len(vg) > 1 and vg[1] is not None else 0.5
            feather = float(vg[2]) if len(vg) > 2 and vg[2] is not None else 0.5
        except (TypeError, ValueError):
            amount = mid = feather = 0
        if abs(amount) > 1e-4:
            attrs["PostCropVignetteAmount"] = _fmt(
                max(-100.0, min(100.0, amount * 100.0)))
            attrs["PostCropVignetteMidpoint"] = _fmt(
                max(0.0, min(100.0, mid * 100.0)))
            attrs["PostCropVignetteFeather"] = _fmt(
                max(0.0, min(100.0, feather * 100.0)))
    gr = e.get("grain")
    if isinstance(gr, (list, tuple)) and gr:
        try:
            amount = float(gr[0] or 0)
            size = float(gr[1]) if len(gr) > 1 and gr[1] is not None else 1.0
        except (TypeError, ValueError):
            amount, size = 0.0, 1.0
        if amount > 1e-4:
            attrs["Grain"] = _fmt(max(0.0, min(100.0, amount * 100.0)))
            attrs["GrainSize"] = _fmt(max(0.0, min(100.0, size * 50.0)))
    if e.get("grayscale"):
        attrs["ConvertToGrayscale"] = "True"

    for k in ("lut_file", "log_curve", "wb_reference", "auto_tone", "masks",
              "spots", "redeye", "optics", "calibration", "auto_levels",
              "auto_exposure", "auto_straighten", "sepia", "srgb", "crop",
              "rotation", "flip_h", "flip_v", "rotate_deg"):
        v = e.get(k)
        if v not in (None, "", 0, 0.0, False, [], {}):
            unsupported.append(k)
    return attrs, curves, sorted(set(unsupported))


def write_xmp_preset(dest, name, edits):
    """写 Lightroom 可导入的 .xmp 预设文件；返回 unsupported 键列表。"""
    attrs, curves, unsupported = edits_to_crs(edits)
    ET.register_namespace("x", _X_NS)
    ET.register_namespace("rdf", _RDF_URI)
    ET.register_namespace("crs", _CRS_URI)
    ET.register_namespace("xmp", _XMP_NS)
    meta = ET.Element(f"{{{_X_NS}}}xmpmeta")
    meta.set(f"{{{_X_NS}}}xmptk", "LensPilot")
    rdf = ET.SubElement(meta, f"{{{_RDF_URI}}}RDF")
    desc = ET.SubElement(rdf, f"{{{_RDF_URI}}}Description",
                         {f"{{{_RDF_URI}}}about": ""})
    desc.set(f"{{{_CRS_URI}}}PresetType", "Normal")
    desc.set(f"{{{_CRS_URI}}}ProcessVersion", "15.0")
    desc.set(f"{{{_CRS_URI}}}Version", "15.0")
    desc.set(f"{{{_CRS_URI}}}WhiteBalance",
             "Custom" if ("Temperature" in attrs or "Tint" in attrs) else "As Shot")
    desc.set(f"{{{_CRS_URI}}}HasSettings", "True")
    desc.set(f"{{{_CRS_URI}}}UUID", str(uuid.uuid4()))
    desc.set(f"{{{_XMP_NS}}}CreatorTool", "LensPilot")
    for k, v in attrs.items():
        desc.set(f"{{{_CRS_URI}}}{k}", v)
    name_el = ET.SubElement(desc, f"{{{_CRS_URI}}}Name")
    alt = ET.SubElement(name_el, f"{{{_RDF_URI}}}Alt")
    li = ET.SubElement(alt, f"{{{_RDF_URI}}}li", {f"{{{_XML_NS}}}lang": "x-default"})
    li.text = name
    for key, pts in curves.items():
        seq_el = ET.SubElement(desc, f"{{{_CRS_URI}}}{key}")
        seq = ET.SubElement(seq_el, f"{{{_RDF_URI}}}Seq")
        for x, y in pts:
            row = ET.SubElement(seq, f"{{{_RDF_URI}}}li")
            row.text = f"{int(x)}, {int(y)}"
    ET.indent(meta, space="  ")
    ET.ElementTree(meta).write(dest, encoding="utf-8", xml_declaration=True)
    return unsupported


@command("import_xmp_presets", "editor",
         "[编辑器] 导入 Lightroom .xmp 预设：读取 crs: 参数，映射后保存为用户预设。"
         "支持一次导入多个文件；只读 XMP，不写照片 sidecar。",
         params_schema={"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"},
                       "description": "XMP 预设文件路径列表"}},
          "required": ["paths"], "additionalProperties": False})
def handle_import_xmp_presets(params):
    paths = params.get("paths") or []
    if not paths:
        return {"error": "缺少 paths"}
    from editor_presets import handle_save_editor_preset
    imported, skipped = [], []
    for p in paths:
        try:
            if not os.path.isfile(p):
                raise FileNotFoundError(p)
            crs = _read_crs_dict(p)
            edits, unmatched = import_crs(crs)
            if not edits:
                skipped.append({"path": p, "reason": "XMP 中没有可映射的 crs: 参数"})
                continue
            raw_name = (crs.get("Name") or "").strip()
            if not raw_name:
                raw_name = os.path.splitext(os.path.basename(p))[0]
            name = raw_name[:30].strip() or "XMP 预设"
            r = handle_save_editor_preset({"name": name, "edits": edits})
            if r.get("error"):
                skipped.append({"path": p, "reason": r["error"]})
            else:
                imported.append({
                    "path": p, "name": name, "id": r["preset"]["id"],
                    "overwritten": bool(r.get("overwritten")),
                    "unmatched": sorted(set(unmatched))[:40],
                })
        except Exception as e:
            skipped.append({"path": p, "reason": str(e)})
    return {"ok": True, "imported": imported, "skipped": skipped}


@command("export_xmp_preset", "editor",
         "[编辑器] 把固定参数预设导出为 Lightroom 可导入的 .xmp 文件。"
         "只写用户选择的预设文件，不写照片 sidecar；LUT/蒙版/污点等无对应项会列入 unsupported。",
         params_schema={"type": "object", "properties": {
             "name": {"type": "string", "description": "预设名称"},
             "edits": {"type": "object", "description": "固定编辑参数"},
             "dest": {"type": "string", "description": "目标 .xmp 路径"}},
          "required": ["edits", "dest"], "additionalProperties": False})
def handle_export_xmp_preset(params):
    name = str(params.get("name") or "").strip() or "LensPilot Preset"
    edits = params.get("edits")
    dest = str(params.get("dest") or "").strip()
    if not isinstance(edits, dict):
        return {"error": "Invalid edits"}
    if not dest:
        return {"error": "缺少导出路径"}
    if not dest.lower().endswith(".xmp"):
        dest += ".xmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        unsupported = write_xmp_preset(dest, name, edits)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "path": dest, "unsupported": sorted(set(unsupported))}


@command("import_lr_develop", "editor",
         "[编辑器] 读取照片同目录 Lightroom XMP sidecar 的 crs: 修改参数，映射为 "
         "LensPilot 编辑参数（近似映射处见模块文档；只读，绝不写 sidecar）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_import_lr_develop(params):
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        return {"error": "Invalid path"}
    sidecar = find_sidecar(path)
    if not sidecar:
        return {"error": "未找到 XMP sidecar（<名称>.<后缀>.xmp / <名称>.xmp）"}
    try:
        crs = _read_crs_dict(sidecar)
    except Exception as e:
        return {"error": f"XMP 解析失败: {e}"}
    if not crs:
        return {"ok": True, "edits": {}, "matched": [], "unmatched": [],
                "sidecar": sidecar, "note": "sidecar 无 crs: 参数"}
    edits, unmatched = import_crs(crs)
    return {"ok": True, "edits": edits, "matched": sorted(edits.keys()),
            "unmatched": sorted(set(unmatched))[:60], "sidecar": sidecar}


def register_handlers():
    command_registry.register_all()
    send_log("lr_xmp_import module loaded (IPC via command_registry)", "info")
