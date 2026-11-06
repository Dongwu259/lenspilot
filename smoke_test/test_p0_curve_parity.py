# -*- coding: utf-8 -*-
"""P0-2 前后端曲线一致性：node(photo-edit.js _curveLut) vs python(_build_curve_lut)。

运行（仓库根）: node smoke_test/test_curve_parity.mjs | python smoke_test/test_p0_curve_parity.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

from photo_s.grade import _build_curve_lut  # noqa: E402

payload = json.load(sys.stdin)
worst = 0
for pts, js_lut in zip(payload["cases"], payload["luts"]):
    py_lut = _build_curve_lut(pts)
    assert len(js_lut) == 256 and len(py_lut) == 256
    diffs = [abs(a - b) for a, b in zip(js_lut, py_lut)]
    d = max(diffs)
    worst = max(worst, d)
    assert d <= 1, f"曲线偏差 >1: {pts} max_diff={d}"
    # 单调性健全性: 恒等曲线必须完全一致
    if pts == [[0, 0], [255, 255]]:
        assert js_lut == py_lut == list(range(256))
print(f"[ok] curve parity: {len(payload['cases'])} 条曲线前后端最大偏差 {worst}/255")
