#!/usr/bin/env python3
"""test_xmp_preset.py — Lightroom XMP 预设导入/导出冒烟

覆盖：edits→crs 反向映射、曲线序列、HSL/分级/暗角/颗粒/降噪、
crs:Name（属性 + rdf:li 两种）、导入命令保存为用户预设、导出命令写文件、
无 crs 参数时跳过、LUT/蒙版等无对应项进入 unsupported。
运行：python smoke_test/test_xmp_preset.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_xmp_test_")

svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda *a, **k: None
svc.register_handler = lambda *a, **k: None
sys.modules["service"] = svc

import lr_xmp_import as x  # noqa: E402
import editor_presets as ep  # noqa: E402

PASS = 0


def ok(name, detail=""):
    global PASS
    PASS += 1
    print(f"  [OK] {name} {detail}")


def main():
    edits = {
        "exposure": 0.35, "contrast": 1.12, "highlights": -0.2, "shadows": 0.15,
        "wb_temp": 6200, "wb_tint": 8, "vibrance": 0.2, "saturation": 0.95,
        "clarity": 0.15, "texture": -0.1, "dehaze": 0.12,
        "sharpness": 0.4, "denoise": 4,
        "hsl": {"blue": [0, 0.2, -0.05], "orange": [0, 0.1, 0.02]},
        "color_grading": {"shadows": [220, 0.15, 0], "highlights": [35, 0.1, 0.02]},
        "curves": {"rgb": [[0, 10], [128, 138], [255, 250]]},
        "vignette": [-0.3, 0.6, 0.4], "grain": [0.2, 1.2],
        "lut_file": "C:/x.cube", "masks": [{"id": 1, "type": "sky"}],
    }
    tmp = tempfile.mkdtemp(prefix="lenspilot_xmp_")
    src = os.path.join(tmp, "roundtrip.xmp")
    unsupported = x.write_xmp_preset(src, "Roundtrip Test", edits)
    assert os.path.isfile(src)
    assert "lut_file" in unsupported and "masks" in unsupported
    assert "exposure" not in unsupported
    ok("导出固定参数 XMP（LUT/蒙版进入 unsupported）", unsupported)

    crs = x._read_crs_dict(src)
    assert crs.get("Name") == "Roundtrip Test"
    back, unmatched = x.import_crs(crs)
    assert not unmatched, unmatched
    assert crs.get("ToneCurvePV2012") == "(0, 10), (128, 138), (255, 250)"
    assert back["curves"]["rgb"] == [[0, 10], [128, 138], [255, 250]]
    assert abs(back["exposure"] - 0.35) < 1e-3
    assert abs(back["contrast"] - 1.12) < 1e-3
    assert abs(back["clarity"] - 0.15) < 1e-3
    assert back["denoise"] == 4
    assert abs(back["grain"][0] - 0.2) < 1e-3 and abs(back["grain"][1] - 1.2) < 1e-2
    assert back["vignette"] == [-0.3, 0.6, 0.4]
    assert abs(back["hsl"]["blue"][1] - 0.2) < 1e-3
    assert back["color_grading"]["shadows"][0] == 220
    ok("读回曲线/HSL/分级/暗角/颗粒/降噪", sorted(back.keys()))

    r = x.handle_import_xmp_presets({"paths": [src]})
    assert r["ok"] and len(r["imported"]) == 1 and not r["skipped"], r
    users = ep._load_user()
    assert users and users[-1]["name"] == "Roundtrip Test"
    ok("导入命令保存为用户预设", users[-1]["id"])

    dest = os.path.join(tmp, "exported.xmp")
    out = x.handle_export_xmp_preset({
        "name": "Roundtrip Test", "edits": edits, "dest": dest})
    assert out.get("ok") and os.path.isfile(dest), out
    assert x._read_crs_dict(dest).get("Name") == "Roundtrip Test"
    ok("导出命令写文件并可读回")

    # LR 风格：crs:Name 只在 rdf:Alt/rdf:li 里
    lr_style = os.path.join(tmp, "lr_style.xmp")
    with open(lr_style, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:Exposure2012="0.5" crs:Contrast2012="10">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">LR Import Name</rdf:li></rdf:Alt></crs:Name>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>""")
    r2 = x.handle_import_xmp_presets({"paths": [lr_style]})
    assert r2["imported"] and r2["imported"][0]["name"] == "LR Import Name", r2
    assert r2["imported"][0]["unmatched"] == [], r2["imported"][0]["unmatched"]
    ok("LR 风格 rdf:li 名字解析", r2["imported"][0]["name"])

    empty = os.path.join(tmp, "empty.xmp")
    with open(empty, "w", encoding="utf-8") as f:
        f.write("<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF "
                "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
                "<rdf:Description rdf:about=''/></rdf:RDF></x:xmpmeta>")
    r3 = x.handle_import_xmp_presets({"paths": [empty]})
    assert not r3["imported"] and r3["skipped"], r3
    ok("无 crs 参数的 XMP 跳过")

    print(f"\n全部通过 ({PASS} 项)")


if __name__ == "__main__":
    main()
