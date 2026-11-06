# -*- coding: utf-8 -*-
"""P0 升级冒烟：统一命令注册表 + 色调曲线管线。

运行（仓库根）: python smoke_test/test_p0_upgrade.py
"""
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)


def test_registry():
    import service
    import command_registry
    import photo_editor

    photo_editor.register_handlers()

    # 10 条命令全部挂到 IPC（9 editor + 1 system registry_list）
    for cmd in ["get_edits", "apply_edits", "save_edits", "reset_edits",
                "batch_apply_edits", "batch_export_edited", "auto_enhance",
                "auto_tone_editor", "get_image_info", "registry_list"]:
        assert cmd in service._handlers, f"IPC 缺 {cmd}"

    desc = command_registry.describe()
    assert len(desc) == 10, len(desc)
    editors = [d for d in desc if d["category"] == "editor"]
    assert len(editors) == 9
    agent_tools = sorted(d["name"] for d in desc if d["agent_tool"])
    assert agent_tools == ["apply_edits", "auto_tone_editor", "get_edits",
                           "get_image_info", "save_edits"], agent_tools
    assert all(d["params_schema"].get("type") == "object" for d in desc)

    r = command_registry.handle_registry_list({"category": "editor"})
    assert r["ok"] and r["count"] == 9
    print(f"[ok] registry: {len(desc)} commands, agent tools = {agent_tools}")


def test_curves_pipeline():
    from photo_s.grade import apply_curves, _build_curve_lut

    pts = [[0, 0], [64, 40], [128, 180], [255, 255]]
    lut = _build_curve_lut(pts)
    assert len(lut) == 256
    assert lut[0] == 0 and lut[255] == 255

    # RGB 灰阶梯度图: 像素值 = x → 输出必须逐像素等于 LUT
    grad = np.tile(np.arange(256, dtype=np.uint8), (256, 1))
    img = Image.fromarray(np.stack([grad, grad, grad], axis=-1))  # RGB
    out = np.array(apply_curves(img, {"rgb": pts}))
    for x in (0, 1, 63, 64, 100, 127, 128, 200, 254, 255):
        assert int(out[0][x][0]) == lut[x], (x, int(out[0][x][0]), lut[x])

    # 分通道: r 恒等 g 反相 b 恒等
    out2 = np.array(apply_curves(
        img, {"r": [[0, 0], [255, 255]], "g": [[0, 255], [255, 0]]}))
    assert out2[0][128][0] == 128 and out2[0][128][1] == 127 and out2[0][128][2] == 128

    # photo_editor.apply_edits 全链路（curves 键委托 grade.apply_curves）
    import photo_editor
    full = np.array(photo_editor.apply_edits(img.copy(), {"curves": {"rgb": pts}}))
    assert int(full[0][128][0]) == lut[128]
    print(f"[ok] curves pipeline: lut[128]={lut[128]}, "
          f"apply_edits 一致, 分通道映射正确")


if __name__ == "__main__":
    test_registry()
    test_curves_pipeline()
    print("ALL P0 SMOKE PASSED")
