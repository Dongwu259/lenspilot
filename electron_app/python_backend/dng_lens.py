"""dng_lens.py — DNG 内嵌镜头档案读取（P6-4，最小 TIFF 走查 + opcode XML）

DNG 在 IFD0 的 tag 51008/51009/51010（OpcodeList1/2/3，BYTE/UNDEFINED）里
存 XML 操作列表。v1 支持其中最常用的两个 opcode（首次命中优先）：
  WarpRectilinear    畸变多项式  r' = r·(1 + k1·r² + k2·r⁴ + k3·r⁶)
  FixVignetteRadial  暗角多项式  gain = 1 + k1·r² + k2·r⁴ + k3·r⁶
其余 opcode（WarpFisheye、FixBadPixels、GainMap 等）计数后忽略。

输出建议值映射到编辑器 optics 滑杆（近似）：
  distortion = clamp(k1 × 400, ±100)   （滑杆 → 系数 = v/100×0.25）
  vig        = clamp(−k1 × 100, ±100)  （补偿 = 负增益）
"""

import os
import re
import xml.etree.ElementTree as ET

import command_registry
from command_registry import command
from service import send_log

_OPCODE_TAGS = {51008: "list1", 51009: "list2", 51010: "list3"}
_WANTED = ("WarpRectilinear", "FixVignetteRadial")


def _parse_tiff_ifd0(data):
    """最小 TIFF 解析：只取 IFD0 的 {tag: bytes}（支持 BYTE/UNDEFINED/内联/外置）。"""
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        raise ValueError("不是 TIFF/DNG 文件")
    endian = "<" if data[:2] == b"II" else ">"
    import struct
    ifd_off = struct.unpack(endian + "I", data[4:8])[0]
    if ifd_off + 2 > len(data):
        raise ValueError("IFD 越界")
    n = struct.unpack(endian + "H", data[ifd_off:ifd_off + 2])[0]
    tags = {}
    type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}
    for i in range(n):
        off = ifd_off + 2 + i * 12
        if off + 12 > len(data):
            break
        tag, typ, count = struct.unpack(endian + "HHI", data[off:off + 8])
        size = type_sizes.get(typ, 1) * count
        if size <= 4:
            raw = data[off + 8:off + 8 + size]
        else:
            voff = struct.unpack(endian + "I", data[off + 8:off + 12])[0]
            raw = data[voff:voff + size]
        tags[tag] = raw
    return tags


def _parse_opcode_xml(raw):
    """opcode XML → [{type, params:[float]}]。容错：坏节点跳过。"""
    ops = []
    try:
        root = ET.fromstring(raw.decode("utf-8", "replace"))
    except ET.ParseError:
        return ops
    for node in root.iter():
        if not node.tag.lower().endswith("opnode"):
            continue
        op_type = node.attrib.get("TYPE", "")
        params = []
        for child in node.iter():
            if child.tag.lower().endswith("opparams") and child.text:
                params = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?",
                                                       child.text)]
                break
        ops.append({"type": op_type, "params": params})
    return ops


def read_profile(dng_path):
    """DNG → {distortion/vignette 的系数, skipped 计数}。无档案返回空 profile。"""
    with open(dng_path, "rb") as f:
        data = f.read(4 * 1024 * 1024)  # opcode list 位于文件头附近,4MB 足够
    tags = _parse_tiff_ifd0(data)
    profile = {"distortion": None, "vignette": None, "skipped": 0, "lists": {}}
    for tag, name in _OPCODE_TAGS.items():
        raw = tags.get(tag)
        if not raw:
            continue
        ops = _parse_opcode_xml(raw)
        profile["lists"][name] = [o["type"] for o in ops]
        for op in ops:
            if op["type"] == "WarpRectilinear" and profile["distortion"] is None:
                profile["distortion"] = op["params"][:3]
            elif op["type"] == "FixVignetteRadial" and profile["vignette"] is None:
                profile["vignette"] = op["params"][:3]
            elif op["type"] not in _WANTED:
                profile["skipped"] += 1
    return profile


def suggest_optics(profile):
    """档案系数 → optics 滑杆建议值（近似映射，raw 一并返回）。"""
    out = {"distortion": 0, "vig": 0, "raw": {}, "skipped": profile.get("skipped", 0)}
    k = profile.get("distortion")
    if k:
        out["raw"]["distortion"] = k
        try:
            out["distortion"] = int(round(max(-100, min(100, float(k[0]) * 400))))
        except (TypeError, ValueError, IndexError):
            pass
    v = profile.get("vignette")
    if v:
        out["raw"]["vignette"] = v
        try:
            out["vig"] = int(round(max(-100, min(100, -float(v[0]) * 100))))
        except (TypeError, ValueError, IndexError):
            pass
    return out


@command("read_dng_lens_profile", "editor",
         "[编辑器] 读取 DNG 内嵌镜头档案（OpcodeList 的 WarpRectilinear/"
         "FixVignetteRadial），返回 optics 滑杆建议值与原始系数。只读。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": ".dng 照片绝对路径"}},
          "required": ["path"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_read_dng_lens_profile(params):
    path = params.get("path", "")
    if not path or not os.path.isfile(path):
        return {"error": "Invalid path"}
    if not path.lower().endswith(".dng"):
        return {"error": "仅支持 .dng（内嵌 opcode 档案为 DNG 规范特性）"}
    try:
        profile = read_profile(path)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"解析失败: {e}"}
    if not any(profile["lists"].values()):
        return {"ok": True, "found": False,
                "note": "该 DNG 无内嵌镜头档案 (OpcodeList)"}
    return {"ok": True, "found": True, **suggest_optics(profile),
            "lists": profile["lists"]}


def register_handlers():
    command_registry.register_all()
    send_log("dng_lens module loaded (IPC via command_registry)", "info")
