# -*- coding: utf-8 -*-
"""P2-1 冒烟：Lightroom XMP crs: 参数解析与映射。

运行（仓库根）: python smoke_test/test_p2_xmp.py
"""
import os
import sys
import tempfile

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p2_xmp_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

from lr_xmp_import import (  # noqa: E402
    find_sidecar, handle_import_lr_develop, import_crs, _read_crs_dict)

_ATTR_XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:ProcessVersion="11.0"
    crs:Exposure2012="+0.35"
    crs:Contrast2012="+15"
    crs:Highlights2012="-20"
    crs:Shadows2012="+30"
    crs:Temperature="5600"
    crs:Tint="+10"
    crs:Vibrance="+25"
    crs:ToneCurvePV2012="(0, 0), (64, 40), (255, 255)"
    crs:HueAdjustmentRed="-12"
    crs:SaturationAdjustmentOrange="30"
    crs:ColorGradeShadowHue="210"
    crs:ColorGradeShadowSat="20"
    crs:ColorGradeShadowLum="-5"
    crs:PostCropVignetteAmount="-15"
    crs:Whites2012="+10"
    crs:ParametricDarks="+5"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

_ELEM_XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:Contrast2012="+15">
   <crs:Exposure2012>+0.60</crs:Exposure2012>
   <crs:Vibrance>+25</crs:Vibrance>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def main():
    tmp = tempfile.mkdtemp(prefix="lenspilot_xmp_")
    photo = os.path.join(tmp, "DSC00001.ARW")
    open(photo, "wb").write(b"\x00")
    xmp = photo + ".xmp"
    open(xmp, "w", encoding="utf-8").write(_ATTR_XMP)

    assert find_sidecar(photo) == xmp
    crs = _read_crs_dict(xmp)
    assert crs["Exposure2012"] == "+0.35" and crs["ToneCurvePV2012"].startswith("(0, 0)")

    edits, unmatched = import_crs(crs)
    assert abs(edits["exposure"] - 0.35) < 1e-6
    assert abs(edits["contrast"] - 1.15) < 1e-6
    assert abs(edits["highlights"] - (-0.2)) < 1e-6
    assert abs(edits["shadows"] - 0.3) < 1e-6
    assert edits["wb_temp"] == 5600 and edits["wb_tint"] == 10
    assert abs(edits["vibrance"] - 0.25) < 1e-6
    assert edits["curves"]["rgb"] == [[0, 0], [64, 40], [255, 255]]
    assert edits["hsl"]["red"][0] == -3.6            # LR -12 → -3.6°
    assert abs(edits["hsl"]["orange"][1] - 0.3) < 1e-6
    assert edits["color_grading"]["shadows"] == [210, 0.2, -0.05]
    assert edits["vignette"][0] == -0.15
    assert "Whites2012" in unmatched and "ParametricDarks" in unmatched

    # IPC handler（agent 工具入口）
    r = handle_import_lr_develop({"path": photo})
    assert r["ok"] and abs(r["edits"]["exposure"] - 0.35) < 1e-6
    assert r["sidecar"] == xmp

    # 子元素写法兼容（<crs:Exposure2012>+0.60</crs:Exposure2012>）
    photo2 = os.path.join(tmp, "DSC00002.ARW")
    open(photo2, "wb").write(b"\x00")
    open(photo2 + ".xmp", "w", encoding="utf-8").write(_ELEM_XMP)
    crs2 = _read_crs_dict(photo2 + ".xmp")
    edits2, _ = import_crs(crs2)
    assert abs(edits2["exposure"] - 0.60) < 1e-6
    assert abs(edits2["vibrance"] - 0.25) < 1e-6

    # 无 sidecar
    photo3 = os.path.join(tmp, "DSC00003.ARW")
    open(photo3, "wb").write(b"\x00")
    r3 = handle_import_lr_develop({"path": photo3})
    assert r3.get("error") and "sidecar" in r3["error"]

    print("[ok] xmp: 属性/子元素两种写法 + 映射表 + unmatched + IPC 全部通过")
    print("P2 XMP IMPORT SMOKE PASSED")


if __name__ == "__main__":
    main()
