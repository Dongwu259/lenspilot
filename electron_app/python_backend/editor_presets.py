"""
editor_presets.py — 编辑器命名预设(内置 LUT 预设 + 用户预设)

内置预设引用打包的 luts/*.cube(scripts/generate_luts.py 生成),
列出时把 lut_file 解析为绝对路径,前端拿到 edits 直接套用。
用户预设存 get_user_data_dir()/editor_presets.json(不写入打包资源目录)。
"""

import os
import sys
import json
import time

from service import send_log, register_handler
from utils import get_user_data_dir

MAX_USER_PRESETS = 50
MAX_NAME_LEN = 30

# ── 内置 LUT 资源目录(dev: python_backend/luts;frozen: _MEIPASS/luts) ──

def _luts_dir():
    if getattr(sys, "frozen", False):
        for base in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)):
            if base:
                d = os.path.join(base, "luts")
                if os.path.isdir(d):
                    return d
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "luts")


# ── 内置预设(slug → luts/<slug>.cube;nameKey 供前端 i18n) ──

_BUILTIN_SLUGS = [
    ("warm-gold", "pePresetWarmGold", "暖金婚礼"),
    ("teal-orange", "pePresetTealOrange", "青橙电影"),
    ("muted-film", "pePresetMutedFilm", "低饱和胶片"),
    ("airy-fresh", "pePresetAiryFresh", "清新明亮"),
    ("soft-portrait", "pePresetSoftPortrait", "柔肤人像"),
]

# ── 内置功能组合模板(P1-3/P0-2 之后的全词汇量组合;category 供前端分组) ──
# 蒙版只用无几何类型(sky/luminance/color),任意画幅照片直接可用。

_BUILTIN_TEMPLATES = [
    # 风光
    ("tpl-landscape-clarity", "peTplLandscapeClarity", "风光·通透", "landscape", {
        "dehaze": 0.22, "clarity": 0.18, "vibrance": 0.15,
        "curves": {"rgb": [[0, 0], [64, 58], [128, 132], [192, 200], [255, 255]]},
        "masks": [{"id": 1, "type": "sky", "invert": False, "amount": 1.0,
                   "params": {},
                   "adjustments": {"dehaze": 0.3, "saturation": 0.12}}]},
    ),
    ("tpl-blue-sky", "peTplBlueSky", "蓝调天空", "landscape", {
        "vibrance": 0.1,
        "hsl": {"blue": [0, 0.2, -0.05], "aqua": [0, 0.15, 0]},
        "masks": [{"id": 1, "type": "sky", "invert": False, "amount": 1.0,
                   "params": {}, "adjustments": {"saturation": 0.25, "clarity": 0.1}}]},
    ),
    ("tpl-golden-sunset", "peTplGoldenSunset", "落日余晖", "landscape", {
        "wb_temp": 5600, "warmth": 0.15, "vibrance": 0.12,
        "hsl": {"orange": [0, 0.15, 0.03], "red": [0, 0.08, 0]},
        "curves": {"rgb": [[0, 4], [128, 136], [255, 255]]}},
    ),
    # 人像
    ("tpl-portrait-air", "peTplPortraitAir", "人像·通透", "portrait", {
        "texture": -0.08, "vibrance": 0.15, "highlights": -0.1, "shadows": 0.08,
        "curves": {"rgb": [[0, 6], [128, 132], [255, 255]]}},
    ),
    ("tpl-skin-warm", "peTplSkinWarm", "肤色红润", "portrait", {
        "wb_temp": 5300, "vibrance": 0.1,
        "hsl": {"orange": [0, 0.12, 0.04], "red": [0, 0.06, 0.02]}},
    ),
    ("tpl-soft-skin", "peTplSoftSkin", "柔肤磨皮", "portrait", {
        "texture": -0.25, "clarity": -0.12, "vibrance": 0.1,
        "denoise": 6, "sharpness": 0.2},
    ),
    # 胶片
    ("tpl-film-negative", "peTplFilmNegative", "经典负片", "film", {
        "curves": {"rgb": [[0, 14], [64, 74], [128, 138], [192, 198], [255, 251]]},
        "saturation": 0.92, "vibrance": 0.05, "grain": [0.3, 1.0],
        "hsl": {"green": [8, 0.1, 0], "yellow": [6, 0.08, 0]}},
    ),
    ("tpl-cinematic-teal", "peTplCinematicTeal", "青橙调色", "film", {
        "color_grading": {"shadows": [200, 0.22, -0.02], "midtones": [210, 0.08, 0],
                          "highlights": [35, 0.15, 0.02]},
        "clarity": 0.1, "vibrance": 0.12,
        "curves": {"rgb": [[0, 0], [64, 56], [192, 202], [255, 255]]}},
    ),
    ("tpl-vintage-fade", "peTplVintageFade", "复古褪色", "film", {
        "curves": {"rgb": [[0, 22], [64, 80], [192, 200], [255, 248]]},
        "saturation": 0.85, "grain": [0.28, 1.1], "vignette": [0.35, 0.5, 0.5]},
    ),
    # 黑白
    ("tpl-bw-classic", "peTplBwClassic", "经典黑白", "bw", {
        "grayscale": True, "contrast": 1.12,
        "curves": {"rgb": [[0, 6], [128, 124], [255, 252]]}, "grain": [0.22, 1.0]},
    ),
    ("tpl-bw-punch", "peTplBwPunch", "高对比黑白", "bw", {
        "grayscale": True, "contrast": 1.25, "clarity": 0.18, "grain": [0.25, 1.0],
        "curves": {"rgb": [[0, 0], [64, 48], [192, 212], [255, 255]]}},
    ),
    ("tpl-bw-soft", "peTplBwSoft", "银盐柔调", "bw", {
        "grayscale": True, "contrast": 0.94, "texture": -0.12, "grain": [0.2, 1.1],
        "vignette": [0.3, 0.5, 0.5],
        "curves": {"rgb": [[0, 16], [128, 132], [255, 246]]}},
    ),
    # 氛围
    ("tpl-night-clean", "peTplNightClean", "夜景净化", "mood", {
        "denoise": 9, "shadows": 0.12, "highlights": -0.18, "vibrance": 0.1,
        "curves": {"rgb": [[0, 2], [128, 134], [255, 254]]}},
    ),
    ("tpl-misty-air", "peTplMistyAir", "雾感空气", "mood", {
        "dehaze": -0.18, "contrast": 0.96, "saturation": 0.9, "grain": [0.12, 1.0],
        "curves": {"rgb": [[0, 18], [128, 130], [255, 250]]}},
    ),
]


def _builtin_presets():
    """内置预设 = 功能组合模板 + LUT 预设;LUT .cube 缺失时跳过(不崩)。"""
    lut_dir = _luts_dir()
    out = []
    for slug, name_key, fallback, category, edits in _BUILTIN_TEMPLATES:
        out.append({
            "id": f"builtin:{slug}",
            "name": fallback,
            "nameKey": name_key,
            "category": category,
            "builtin": True,
            "edits": dict(edits),
        })
    for slug, name_key, fallback in _BUILTIN_SLUGS:
        path = os.path.join(lut_dir, f"{slug}.cube")
        if not os.path.isfile(path):
            continue
        out.append({
            "id": f"builtin:{slug}",
            "name": fallback,
            "nameKey": name_key,
            "category": "lut",
            "builtin": True,
            "edits": {"lut_file": path},
        })
    return out


# ── 用户预设持久化 ──

def _user_file():
    return os.path.join(get_user_data_dir(), "editor_presets.json")


def _load_user():
    try:
        with open(_user_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [p for p in data
                    if isinstance(p, dict) and p.get("id") and isinstance(p.get("edits"), dict)]
    except FileNotFoundError:
        pass
    except Exception as e:
        send_log(f"editor_presets: load failed: {e}", "warning")
    return []


def _save_user(presets):
    os.makedirs(os.path.dirname(_user_file()), exist_ok=True)
    tmp = _user_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _user_file())


def _sanitize_edits(edits):
    """只保留 photo_editor 认识的键,类型按 DEFAULT_EDITS 归一。"""
    from photo_editor import DEFAULT_EDITS  # 函数内引用,避免模块级耦合
    out = {}
    for k, v in edits.items():
        if k not in DEFAULT_EDITS:
            continue
        default = DEFAULT_EDITS[k]
        try:
            if isinstance(default, bool):
                out[k] = bool(v)
            elif isinstance(default, (int, float)):
                out[k] = type(default)(v)
            elif isinstance(default, str):
                out[k] = str(v) if v else ""
            elif default is None:
                out[k] = v  # crop 等可空键原样透传
        except (TypeError, ValueError):
            continue
    return out


def _stats_for_path(path):
    """照片的未编辑统计卡（自适应预设的基线 / 参考型预设的目标观感），失败返回 None。

    只做本地统计，不保存/不训练像素；参考成片只留统计卡，不复制其像素。
    """
    if not path or not os.path.isfile(str(path)):
        return None
    try:
        from model_loader import _load_image_rgb
        from photo_stats import compute_stats
        img = _load_image_rgb(str(path))
        return compute_stats(img) if img is not None else None
    except Exception as e:
        send_log(f"editor_presets: stats failed: {e}", "warning")
        return None


# ── IPC handlers ──

def handle_list_editor_presets(params):
    return {"presets": _builtin_presets() + _load_user()}


def handle_save_editor_preset(params):
    name = str(params.get("name") or "").strip()
    if not name or len(name) > MAX_NAME_LEN or any(ord(c) < 32 for c in name):
        return {"error": f"Invalid preset name (1-{MAX_NAME_LEN} chars)"}
    edits = params.get("edits")
    if not isinstance(edits, dict):
        edits = {}
    clean = _sanitize_edits(edits)

    # 参考型预设：只保留参考成片的统计卡（目标观感），不保存像素/路径。
    ref_path = str(params.get("reference_path") or "").strip()
    ref_stats = _stats_for_path(ref_path) if ref_path else None
    if ref_path and not ref_stats:
        return {"error": "无法读取参考成片"}
    ref_name = (os.path.basename(ref_path.replace("\\", "/"))
                if ref_stats else "")
    if not clean and not ref_stats:
        return {"error": "No valid edits to save"}
    stats = None if ref_stats else _stats_for_path(params.get("path"))

    def apply_fields(p):
        p["edits"] = clean
        if ref_stats:
            p["reference_stats"] = ref_stats
            p["reference_name"] = ref_name
            p.pop("source_stats", None)
        else:
            p.pop("reference_stats", None)
            p.pop("reference_name", None)
            if stats:
                p["source_stats"] = stats

    presets = _load_user()
    # 同名覆盖(保持原 id),否则新建
    for p in presets:
        if p.get("name") == name:
            apply_fields(p)
            _save_user(presets)
            return {"ok": True, "preset": p, "overwritten": True}
    if len(presets) >= MAX_USER_PRESETS:
        return {"error": f"Preset limit reached ({MAX_USER_PRESETS})"}
    preset = {
        "id": f"u:{int(time.time() * 1000):x}",
        "name": name,
        "builtin": False,
        "edits": clean,
    }
    apply_fields(preset)
    presets.append(preset)
    _save_user(presets)
    return {"ok": True, "preset": preset}


def handle_delete_editor_preset(params):
    pid = str(params.get("id") or "")
    if not pid.startswith("u:"):
        return {"error": "Only user presets can be deleted"}
    presets = _load_user()
    remaining = [p for p in presets if p["id"] != pid]
    if len(remaining) == len(presets):
        return {"error": "Preset not found"}
    _save_user(remaining)
    return {"ok": True}


def register_preset_handlers():
    register_handler("list_editor_presets", handle_list_editor_presets)
    register_handler("save_editor_preset", handle_save_editor_preset)
    register_handler("delete_editor_preset", handle_delete_editor_preset)
    send_log("editor_presets module loaded", "info")
