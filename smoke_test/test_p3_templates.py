# -*- coding: utf-8 -*-
"""P3 冒烟：内置功能组合模板（后端合规性 + sanitize + 列表集成）。

运行（仓库根）: python smoke_test/test_p3_templates.py
"""
import os
import sys
import tempfile

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p3_tpl_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import editor_presets  # noqa: E402
from photo_editor import DEFAULT_EDITS  # noqa: E402

GEOMETRY_FREE_MASKS = {"sky", "luminance", "color"}


def main():
    templates = editor_presets._builtin_presets()
    tpls = [p for p in templates if p["id"].startswith("builtin:tpl-")]
    luts = [p for p in templates if p["id"].startswith("builtin:") and not p["id"].startswith("builtin:tpl-")]
    assert len(tpls) == 14, len(tpls)
    assert all(p["category"] for p in tpls)

    cats = {p["category"] for p in tpls}
    assert cats == {"landscape", "portrait", "film", "bw", "mood"}, cats
    assert all(p["nameKey"].startswith("peTpl") for p in tpls)

    for p in tpls:
        e = p["edits"]
        bad = [k for k in e if k not in DEFAULT_EDITS]
        assert not bad, f"{p['id']}: 未知键 {bad}"
        # 曲线：x 严格递增、含 0/255 端点
        for ch, pts in (e.get("curves") or {}).items():
            xs = [q[0] for q in pts]
            assert xs == sorted(xs) and len(set(xs)) == len(xs), f"{p['id']} curves x"
            assert xs[0] == 0 and xs[-1] == 255, f"{p['id']} curves 端点"
        # HSL / 三向分级：三元组结构
        for dom, tr in (e.get("hsl") or {}).items():
            assert len(tr) == 3 and -50 <= tr[0] <= 50, f"{p['id']} hsl {dom}"
        for zone, tr in (e.get("color_grading") or {}).items():
            assert len(tr) == 3 and 0 <= tr[0] <= 360, f"{p['id']} grading {zone}"
        # 模板蒙版必须无几何（任意画幅可用）
        for m in e.get("masks") or []:
            assert m["type"] in GEOMETRY_FREE_MASKS, f"{p['id']} mask {m['type']}"
            assert m.get("adjustments"), f"{p['id']} mask 无调整"
        # 数值键在合理区间（粗检）
        if "denoise" in e:
            assert 0 <= e["denoise"] <= 20
        for k in ("dehaze", "clarity", "vibrance", "texture"):
            if k in e:
                assert -1 <= e[k] <= 1, f"{p['id']} {k}={e[k]}"
        if "contrast" in e:
            assert 0 <= e["contrast"] <= 2

    # sanitize 透传复合键（curves/hsl/masks 为 None 默认 → 原样保留）
    sample = tpls[0]
    clean = editor_presets._sanitize_edits(sample["edits"])
    assert clean.get("masks") and clean.get("curves"), "sanitize 丢复合键"

    # 列表 IPC：模板 + LUT(若 cube 存在) + 无用户预设
    r = editor_presets.handle_list_editor_presets({})
    ids = [p["id"] for p in r["presets"]]
    assert len(ids) == len(templates) and len(set(ids)) == len(ids)

    # 端到端：每个模板都能被真实管线渲染（含蒙版/曲线/颗粒组合）
    import numpy as np
    from PIL import Image
    import photo_editor
    img = Image.fromarray(np.zeros((240, 320, 3), np.uint8))
    arr = np.zeros((240, 320, 3), np.uint8)
    arr[:120] = [60, 100, 200]      # 蓝天
    arr[120:] = [90, 120, 60]       # 绿地
    img = Image.fromarray(arr)
    for p in tpls:
        out = photo_editor.apply_edits(img.copy(), dict(p["edits"]))
        assert out.size == img.size, p["id"]
    print(f"[ok] templates: {len(tpls)} 个功能组合模板合规且全部可渲染, 类别 {sorted(cats)}, "
          f"LUT 预设 {len(luts)} 个")
    print("P3 TEMPLATES SMOKE PASSED")


if __name__ == "__main__":
    main()
