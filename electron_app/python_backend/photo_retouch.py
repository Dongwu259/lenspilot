"""
photo_retouch.py — Lightroom 桥接:读取 XMP sidecar 识别修图结果 (0.8.9)

两轮筛选工作流的后端核心:
  第1轮 粗筛: 软件 AI 评分 + 写 XMP 星标(export_xmp) → Lightroom 读星标修图
  第2轮 终选: 本模块读回 LR 写回/增强的 .xmp → 识别"已修过"的照片
            (developed=LR 开发数据 crs: 块 / rating=LR 星级 / modified=文件变更)
            → 供交互页二次筛选与数字员工 final_select 使用。

设计要点:
  - 只读,不写任何文件;绝不删除/覆盖含 LR crs: 开发数据的 sidecar(见 photo_scorer.export_xmp 保护)。
  - XMP 解析用 stdlib xml.etree.ElementTree + 正则兜底(容忍 LR 半写/损坏文件),无新依赖。
  - 所有对 photo_scorer / score_cache 的引用均惰性导入,避免循环依赖(service 注册本模块)。
"""

import os
import re
import xml.etree.ElementTree as ET

# ── 命名空间 ──
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP_NS = "http://ns.adobe.com/xap/1.0/"
_CRS_NS = "http://ns.adobe.com/camera-raw-settings/1.0/"

# ── 正则兜底(LR 半写 / 非标准 XML)──
_RE_RATING = re.compile(r'xmp:Rating="?([0-5])"?')
_RE_LABEL = re.compile(r'xmp:Label="([^"]*)"')
_RE_DEVELOPED = re.compile(r'xmlns:crs="|crs:')


# ═══════════════════════════════════════════════════════════════════════
# XMP sidecar 读取
# ═══════════════════════════════════════════════════════════════════════

def read_xmp_sidecar(photo_path):
    """读取 {photo}.xmp sidecar。

    返回 {has_sidecar, rating(0-5), label, developed, creator}:
      rating    — xmp:Rating(0-5),我方写的与 LR 写的都是它
      label     — xmp:Label(Green/Red/...)
      developed — True 当 sidecar 含任何 crs: 属性(即 LR 拥有开发数据)
      creator   — xmp:CreatorTool(LensPilot / Adobe Lightroom ...)
    文件不存在返回 has_sidecar=False 的全默认。
    """
    out = {"has_sidecar": False, "rating": 0, "label": "", "developed": False, "creator": ""}
    xp = os.path.splitext(photo_path)[0] + ".xmp"
    if not os.path.isfile(xp):
        return out
    out["has_sidecar"] = True
    try:
        with open(xp, "rb") as f:
            data = f.read()
    except Exception:
        return out

    try:
        root = ET.fromstring(data)  # 尊重声明编码,容忍 <?xpacket ...?>
        for d in root.iter():
            t = d.tag
            if not isinstance(t, str):
                continue  # 注释/处理指令无 tag
            # 容忍命名空间别名(rdf:)与无命名空间的裸 Description
            if t != "{%s}Description" % _RDF_NS and not t.endswith("}Description") and t != "Description":
                continue
            r = d.get("{%s}Rating" % _XMP_NS)
            if r is not None:
                try:
                    out["rating"] = max(0, min(5, int(float(r))))
                except Exception:
                    pass
            lab = d.get("{%s}Label" % _XMP_NS)
            if lab:
                out["label"] = str(lab).strip()
            cr = d.get("{%s}CreatorTool" % _XMP_NS)
            if cr:
                out["creator"] = str(cr).strip()
            if any(k.startswith("{%s}" % _CRS_NS) for k in d.attrib):
                out["developed"] = True
            # 结构化 <xmp:Rating><rdf:Alt><rdf:li>4</rdf:li>... 形式(少见)
            if not out["rating"]:
                for ch in d.iter():
                    if ch.tag != "{%s}Rating" % _XMP_NS:
                        continue
                    txt = (ch.text or "").strip()
                    if not txt:
                        for li in ch.iter():
                            if isinstance(li.tag, str) and li.tag.endswith("}li"):
                                txt = (li.text or "").strip()
                                if txt:
                                    break
                    if txt:
                        try:
                            out["rating"] = max(0, min(5, int(float(txt))))
                        except Exception:
                            pass
    except ET.ParseError:
        # 畸形/半写文件:正则兜底
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return out
        m = _RE_RATING.search(text)
        if m:
            try:
                out["rating"] = max(0, min(5, int(m.group(1))))
            except Exception:
                pass
        m = _RE_LABEL.search(text)
        if m:
            out["label"] = m.group(1)
        if _RE_DEVELOPED.search(text):
            out["developed"] = True
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════════════════
# stem 去重:LR 导出可能在 RAW 旁生成同名 .jpg
# ═══════════════════════════════════════════════════════════════════════

def stem_dedup(paths, scores_by_path=None, deliver_same_stem=False):
    """按大小写不敏感 basename-stem 分组,同组内:
      - RAW(携带 sidecar + 开发数据)优先于 JPEG/PNG/TIFF/DNG 兄弟 → 只留 RAW;
      - 否则分数更高者留(有 scores 时);再否则保留第一个(字典序稳定)。
    返回去重后的 path 列表(保序)。deliver_same_stem=True 时不做去重。
    """
    if deliver_same_stem or not paths:
        return list(paths)
    from utils import RAW_EXTS as _RAW
    scores_by_path = scores_by_path or {}

    def _is_raw(p):
        return os.path.splitext(p)[1].lower() in _RAW

    groups = {}  # stem(lower) → [paths]
    for p in paths:
        base = os.path.splitext(os.path.basename(p))[0].lower()
        groups.setdefault(base, []).append(p)
    out = []
    for stem in groups:
        grp = groups[stem]
        raws = [p for p in grp if _is_raw(p)]
        if raws and len(grp) > 1:
            pick = sorted(raws)[0]  # RAW 唯一权威
        elif len(grp) > 1:
            pick = max(grp, key=lambda p: scores_by_path.get(p, 0) or 0)
        else:
            pick = grp[0]
        out.append(pick)
    return out


# ═══════════════════════════════════════════════════════════════════════
# scan_retouched 核心:识别"已修过"的照片
# ═══════════════════════════════════════════════════════════════════════

def scan_retouched_core(folder):
    """扫描文件夹,识别已修图照片(供 round-2 筛选/终选)。

    对每张图读 .xmp sidecar + 对比 scores 缓存(mtime/size):
      modified  = 无 scores 行(新文件) 或 stat mtime/size ≠ 缓存行(文件被改/重导出)
      retouched = developed or modified or (has_sidecar and rating>=1)
    返回 {
      "retouched": [ {path, filename, score, thumb_path, manual_keep,
                      xmp_rating, xmp_label, developed, modified} ],
      "retouched_count": int, "modified_count": int, "developed_count": int,
    }
    """
    import photo_scorer
    import score_cache
    from model_loader import model_version

    images = photo_scorer.find_images(folder, force_refresh=True)
    mv = model_version()
    out, modified_n, developed_n = [], 0, 0
    for p in images:
        try:
            row = score_cache.get_score(p, mv)
        except Exception:
            row = None
        modified = False
        score, thumb, manual = (row.get("score"), row.get("thumb_path"), row.get("manual_keep")) if row else (None, "", 0)
        if row:
            try:
                st = os.stat(p)
                if int(st.st_mtime) != (row.get("mtime") or 0) or st.st_size != (row.get("size") or 0):
                    modified = True
            except OSError:
                modified = True
        else:
            modified = True  # 未评过的新文件也视为"变化",round-2 重评会覆盖

        sx = read_xmp_sidecar(p)
        retouched = bool(sx["developed"] or modified or (sx["has_sidecar"] and sx["rating"] >= 1))
        if sx["developed"]:
            developed_n += 1
        if modified:
            modified_n += 1
        if not retouched:
            continue
        out.append({
            "path": p,
            "filename": os.path.basename(p),
            "score": score if score is not None else -1,
            "thumb_path": thumb or "",
            "manual_keep": manual or 0,
            "xmp_rating": sx["rating"],
            "xmp_label": sx["label"],
            "developed": sx["developed"],
            "modified": modified,
        })
    return {
        "retouched": out,
        "retouched_count": len(out),
        "modified_count": modified_n,
        "developed_count": developed_n,
    }


def handle_scan_retouched(params):
    """IPC: scan_retouched {folder} → 返回已修图照片列表(供前端 round-2 筛选)。"""
    from service import send_event, send_log
    folder = (params.get("folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        return {"error": f"无效文件夹: {folder}"}
    try:
        res = scan_retouched_core(folder)
    except Exception as e:
        send_log(f"scan_retouched 失败: {e}", "error")
        return {"error": str(e)}
    # 大列表分块发射进度(前端轮询结果,不依赖该事件)
    results = res["retouched"]
    if len(results) > 300:
        step = max(1, len(results) // 10)
        for i in range(0, len(results), step):
            try:
                send_event("scan_retouched_progress", {
                    "current": min(i + step, len(results)), "total": len(results),
                })
            except Exception:
                break
    return {"results": results, "counts": {
        "retouched": res["retouched_count"],
        "modified": res["modified_count"],
        "developed": res["developed_count"],
    }}


def register_handlers():
    from service import register_handler, send_log
    register_handler("scan_retouched", handle_scan_retouched)
    send_log("photo_retouch module loaded", "info")
