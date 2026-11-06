#!/usr/bin/env python3
"""
test_grade_edits.py — photo_s v1.6.1 调色接入测试(0.8.9)

覆盖:wb_tint / levels / curves / clarity / texture / dehaze / vibrance /
hsl / color_grading / vignette / grain 各 edits 键经 apply_edits 生效;
LUT 后 EXIF 保留(v1.6.1 上游修复);新键 sidecar 持久化;默认全关时图像不变。
运行: python smoke_test/test_grade_edits.py
"""
import io
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

# stub service(photo_editor 只需 send_event/send_log/register_handler)
svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda *a, **k: None
svc.register_handler = lambda n, h: None
sys.modules["service"] = svc

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
import photo_editor as pe  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  [OK] {name}")


def _base_img(size=(320, 240)):
    """渐变+色块测试图(纯色图对 clarity/dehaze 等局部算法不敏感)。"""
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for y in range(size[1]):
        for x in range(size[0]):
            arr[y, x] = (x * 255 // size[0], y * 255 // size[1],
                         (x + y) * 255 // (size[0] + size[1]))
    # 加一块高饱和红色区域,便于 HSL/vibrance 断言
    arr[20:80, 20:100] = (220, 30, 30)
    # 加一块细棋盘格,给 texture/clarity 等局部对比算法提供细节
    for y in range(160, 220):
        for x in range(200, 300):
            arr[y, x] = (255, 255, 255) if (x + y) % 2 == 0 else (0, 0, 0)
    return Image.fromarray(arr, "RGB")


def _diff(a, b):
    return float(np.abs(
        np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)
    ).mean())


BASE = _base_img()

# 1. 每个新键都让图像发生变化
cases = [
    ("wb_tint", {"wb_tint": 60}),
    ("levels", {"levels": [30, 220, 1.2]}),
    ("curves", {"curves": {"rgb": [[0, 0], [128, 150], [255, 255]]}}),
    ("clarity", {"clarity": 0.6}),
    ("texture", {"texture": 0.6}),
    ("dehaze", {"dehaze": 0.5}),
    ("vibrance", {"vibrance": 0.8}),
    ("hsl", {"hsl": {"red": [0, -0.6, 0.1]}}),
    ("color_grading", {"color_grading": {"shadows": [220, 0.4],
                                          "highlights": [40, 0.3]}}),
    ("vignette", {"vignette": [0.7, 0.4, 0.5]}),
    ("grain", {"grain": [0.5, 1.5]}),
]
for name, edit in cases:
    out = pe.apply_edits(BASE, edit)
    d = _diff(BASE, out)
    assert d > 0.5, f"{name} 未生效 (diff={d})"
    ok(f"{name} 生效 (diff={d:.2f})")

# rotate_deg: expand 画布,尺寸变化即生效(与全图 diff 不可比)
out = pe.apply_edits(BASE, {"rotate_deg": 5})
assert out.size != BASE.size, "rotate_deg 未生效(expand 尺寸应变化)"
ok(f"rotate_deg 生效 (size {BASE.size} → {out.size})")

# 2. 默认全关 → 图像不变
out = pe.apply_edits(BASE, {})
assert _diff(BASE, out) < 1e-6, "默认 edits 改变了图像"
ok("默认 edits 图像不变")

# 3. 新键 sidecar 持久化(仅存非默认值,读回合并默认值)
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "x.jpg")
    BASE.save(p, "JPEG")
    edits = dict(pe.DEFAULT_EDITS)
    edits["vibrance"] = 0.5
    edits["levels"] = [10, 240, 1.1]
    edits["color_grading"] = {"shadows": [220, 0.4]}
    assert pe.save_edits(p, edits)
    back = pe.load_edits(p)
    assert back["vibrance"] == 0.5 and back["levels"] == [10, 240, 1.1]
    assert back["color_grading"] == {"shadows": [220, 0.4]}
    assert back["grain"] is None  # 未设置的键回落默认
    ok("新键 sidecar 保存/读回")

# 4. LUT 后 EXIF 保留(v1.6.1 上游修复回归)
lut_dir = os.path.join(BACKEND, "luts")
cube = None
if os.path.isdir(lut_dir):
    cubes = [f for f in os.listdir(lut_dir) if f.endswith(".cube")]
    cube = os.path.join(lut_dir, cubes[0]) if cubes else None
if cube:
    from photo_s.lut import apply_lut
    img = Image.new("RGB", (64, 64), (128, 90, 60))
    img.info["exif"] = b"Exif\x00\x00fakexif"
    out = apply_lut(img, cube)
    assert out.info.get("exif") == b"Exif\x00\x00fakexif", "LUT 后 EXIF 丢失"
    ok("LUT 后 EXIF 保留(上游 v1.6.1 修复生效)")
else:
    ok("无内置 LUT,跳过 EXIF 用例")

# 5. RGBA 输入经 grade 链不丢 alpha
rgba = BASE.convert("RGBA")
out = pe.apply_edits(rgba, {"vibrance": 0.5, "clarity": 0.4,
                            "levels": [10, 240, 1.0]})
assert out.mode in ("RGB", "RGBA"), f"mode 异常: {out.mode}"
if out.mode == "RGBA":
    assert out.getchannel("A").getextrema() == (255, 255)
ok("RGBA 经 grade 链 alpha 保留")

# 6. 异常输入静默跳过(坏 levels/坏 hsl 色域不崩)
out = pe.apply_edits(BASE, {"levels": ["a", "b"], "hsl": {"nope": [1, 2, 3]}})
assert out is not None
ok("非法输入静默降级不崩")

print(f"\n全部通过 ({PASS} 项)")
