# -*- coding: utf-8 -*-
"""P6-4 冒烟：DNG 内嵌镜头档案（最小 TIFF 构造 + opcode XML 解析 + 建议映射）。

运行（仓库根）: python smoke_test/test_p6_dng.py
"""
import os
import struct
import sys
import tempfile

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p6_dng_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

from dng_lens import (  # noqa: E402
    handle_read_dng_lens_profile, read_profile, suggest_optics)

_XML = (b'<OpcodeList2>'
        b'<OpNode ID="0" TYPE="WarpRectilinear">'
        b'<OpParams>0.02 0.001 0</OpParams>'
        b'</OpNode>'
        b'<OpNode ID="1" TYPE="FixVignetteRadial">'
        b'<OpParams>0.3 0 0</OpParams>'
        b'</OpNode>'
        b'<OpNode ID="2" TYPE="FixBadPixelsConstant">'
        b'<OpParams>0 0</OpParams>'
        b'</OpNode>'
        b'</OpcodeList2>')


def _mini_dng(xml):
    """最小小端 TIFF：IFD0 仅一条 tag 51009 (OpcodeList2, BYTE)。"""
    header = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = struct.pack("<H", 1)
    ifd += struct.pack("<HHII", 51009, 1, len(xml), 8 + 2 + 12 + 4)  # 外置偏移
    ifd += struct.pack("<I", 0)  # 无下一 IFD
    return header + ifd + xml


def main():
    tmp = tempfile.mkdtemp(prefix="lenspilot_dng_")
    dng = os.path.join(tmp, "IMG.dng")
    with open(dng, "wb") as f:
        f.write(_mini_dng(_XML))

    profile = read_profile(dng)
    assert profile["distortion"] == [0.02, 0.001, 0.0], profile["distortion"]
    assert profile["vignette"] == [0.3, 0.0, 0.0], profile["vignette"]
    assert profile["skipped"] == 1  # FixBadPixelsConstant 不支持 → 计数

    sug = suggest_optics(profile)
    assert sug["distortion"] == 8, sug     # 0.02 × 400
    assert sug["vig"] == -30, sug          # −0.3 × 100（补偿为负增益）
    assert sug["raw"]["vignette"] == [0.3, 0.0, 0.0]

    r = handle_read_dng_lens_profile({"path": dng})
    assert r["ok"] and r["found"] and r["distortion"] == 8 and r["skipped"] == 1
    assert set(r["lists"].get("list2", [])) >= {"WarpRectilinear", "FixVignetteRadial"}

    # 无 opcode 的合法 TIFF → found=False
    dng2 = os.path.join(tmp, "empty.dng")
    with open(dng2, "wb") as f:
        f.write(_mini_dng(b"<OpcodeList2></OpcodeList2>"))
    r2 = handle_read_dng_lens_profile({"path": dng2})
    assert r2["ok"] and not r2["found"]

    # 非 DNG → 可读错误
    jpg = os.path.join(tmp, "x.jpg")
    open(jpg, "wb").write(b"\xff\xd9")
    assert "dng" in handle_read_dng_lens_profile({"path": jpg})["error"]
    assert handle_read_dng_lens_profile({"path": "/nope.dng"})["error"]

    print("[ok] dng: TIFF 走查/opcode 解析/建议映射/skipped 计数/错误路径 全部通过")
    print("P6 DNG SMOKE PASSED")


if __name__ == "__main__":
    main()
