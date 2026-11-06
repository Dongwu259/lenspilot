# -*- coding: utf-8 -*-
"""test_grade_gray_hue.py — photo_s grade._to_hsv 灰像素 hue 覆写 bug 的回归锁
（上游报告见 PHOTO_S_UPSTREAM_BUG_GRAY_HUE.md；修复：三段 sel 互斥）
"""
import sys
import numpy as np
from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
from photo_s.grade import apply_hsl, apply_color_grading

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


# 灰区不得被任何单带 HSL 调整命中（buggy 版: [89 89 128]）
out = np.asarray(apply_hsl(Image.new("RGB", (32, 32), (128, 128, 128)), {"blue": [0, 0.3, 0]}))
check("gray untouched by blue ds", (out == 128).all(), out[0, 0])

# 阴影 hue=0° 分级应偏暖 R>B（buggy 版: [60 30 60]，R==B）
out2 = np.asarray(apply_color_grading(Image.new("RGB", (32, 32), (60, 60, 60)), shadows=[0, 0.5]))
check("shadow hue0 warm R>B", int(out2[0, 0, 0]) > int(out2[0, 0, 2]), out2[0, 0])

# 饱和像素行为不变：红像素做 red 带 +sat 应更饱和（取值路径不受修复影响）
out3 = np.asarray(apply_hsl(Image.new("RGB", (8, 8), (200, 40, 40)), {"red": [0, 0.2, 0]}))
check("saturated pixel still shifted", int(out3[0, 0, 0]) - int(out3[0, 0, 1]) >= 160 - 40,
      out3[0, 0])

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
