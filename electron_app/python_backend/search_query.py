"""search_query.py — 照片库字段搜索（LightCraft 式 query 语法，P1-1）

对已评分照片缓存（score_cache: 评分/旗标/尺寸/RAW + AI 标签）做字段查询：

    score:>7 flag:pick keyword:人像 raw:false w:>=3000 date:2026-04 mountain

语法：空格分隔的多个条件，条件之间 AND。
  field:value        精确/子串匹配（按字段语义）
  field:>=v > < <= != 同上支持比较符
  "带 空格 的词"     引号包词（裸词/keyword/name 可用）
  裸词               文件名 或 AI 标签 的子串匹配

支持字段：
  score   AI 美学分（0-10）            rating  星级 0-5（用星标分数段换算，前端传入）
  flag    pick / reject / none         keyword/tag  AI 标签子串
  name    文件名子串                   ext     扩展名（arw/jpg...）
  raw     true / false                 w / h   宽 / 高（px）
  orient  portrait / landscape / square
  date    文件日期 YYYY 或 YYYY-MM 或 YYYY-MM-DD（按本地时区，取文件 mtime——
          拷卡保留 mtime 的场景即拍摄日；EXIF 拍摄时间为后续增强）

数据边界（诚实口径）：搜索目标是「已评分缓存」——未评分照片不在索引里；
iso / camera 等 EXIF 字段需 RAW 解码，暂不支持（查询会报错并列出可用字段）。
"""

import os
import time

import command_registry
from command_registry import command
from service import send_log
import score_cache
from model_loader import model_version

DEFAULT_STAR_BANDS = {1: 4.5, 2: 5.5, 3: 6.5, 4: 7.5, 5: 8.5}  # 与设置页默认一致

_NUMERIC_FIELDS = {"score", "rating", "w", "width", "h", "height"}
_ALL_FIELDS = {"score", "rating", "flag", "keyword", "tag", "name", "ext",
               "raw", "w", "width", "h", "height", "orient", "date"}

_FLAG_MAP = {"pick": 1, "keep": 1, "1": 1, "select": 1,
             "reject": -1, "rejects": -1, "-1": -1, "x": -1,
             "none": 0, "unmarked": 0, "0": 0, "u": 0}


# ── 解析 ────────────────────────────────────────────────────────────────────

def _tokens(q):
    """空白切词，双引号内保留空格。"""
    out, cur, in_q = [], "", False
    for ch in q:
        if ch == '"':
            in_q = not in_q
            continue
        if ch.isspace() and not in_q:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if in_q:
        raise ValueError("引号未闭合")
    if cur:
        out.append(cur)
    return out


def parse(query):
    """query → [(field, op, value), ...]；裸词 → ('name', '~', word)（名称或标签子串）。

    op ∈ {'=', '!=', '>', '>=', '<', '<=', '~'(子串)}；非法字段/值抛 ValueError。
    """
    clauses = []
    for tok in _tokens(query.strip()):
        field, sep, rest = tok.partition(":")
        if not sep:  # 裸词
            clauses.append(("name", "~", field))
            continue
        field = field.lower()
        if field not in _ALL_FIELDS:
            raise ValueError(
                f"未知字段 '{field}'，可用: {' '.join(sorted(_ALL_FIELDS))}")
        for op in (">=", "<=", "!=", ">", "<", "="):
            if rest.startswith(op):
                rest = rest[len(op):]
                break
        else:
            op = "="
        rest = rest.strip()
        if not rest:
            raise ValueError(f"字段 '{field}' 缺少取值")
        if field in _NUMERIC_FIELDS:
            try:
                rest = float(rest)
            except ValueError:
                raise ValueError(f"字段 '{field}' 需要数字，得到 '{rest}'")
        clauses.append((field, op, rest))
    if not clauses:
        raise ValueError("空查询")
    return clauses


# ── 求值 ────────────────────────────────────────────────────────────────────

def _cmp(op, a, b):
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    return False


def _stars_of(score, bands):
    """分数 → 星级 0-5（≥band[n] 记 n 星，取满足的最高档）。"""
    stars = 0
    for n in (1, 2, 3, 4, 5):
        if score >= bands.get(n, DEFAULT_STAR_BANDS[n]):
            stars = n
    return stars


def _date_str(mtime):
    return time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else ""


def _match(clause, row, tags, bands):
    field, op, val = clause
    if field == "name":
        needle = str(val).casefold()
        hay = os.path.basename(row["path"]).casefold()
        if op == "~":
            return needle in hay or any(needle in t.casefold() for t in tags)
        return _cmp(op, hay, needle.casefold())  # 罕见：name:=xxx 精确比较
    if field == "keyword" or field == "tag":
        needle = str(val).casefold()
        hit = any(needle in t.casefold() for t in tags)
        return hit if op in ("=", "~") else not hit
    if field == "flag":
        want = _FLAG_MAP.get(str(val).lower())
        if want is None:
            raise ValueError(f"flag 取值应为 pick/reject/none，得到 '{val}'")
        return _cmp(op, row.get("manual_keep") or 0, want)
    if field == "ext":
        ext = os.path.splitext(row["path"])[1].lower().lstrip(".")
        return _cmp(op, ext, str(val).lower().lstrip("."))
    if field == "raw":
        want = str(val).lower() in ("true", "1", "yes", "y")
        return _cmp(op, bool(row.get("is_raw")), want)
    if field == "orient":
        w, h = row.get("width"), row.get("height")
        if not w or not h:
            return False
        actual = "square" if w == h else ("landscape" if w > h else "portrait")
        return _cmp(op, actual, str(val).lower())
    if field == "date":
        if op == "=":  # 前缀语义: date:2026-04 命中该月每一天
            return _date_str(row.get("mtime")).startswith(str(val))
        return _cmp(op, _date_str(row.get("mtime")), str(val))
    # 数值字段: score / rating / w / h
    num = row.get("score") if field == "score" else (
        _stars_of(row["score"], bands) if field == "rating" else
        row.get("width") if field in ("w", "width") else row.get("height"))
    if num is None:
        return False
    return _cmp(op, num, val)


def search_rows(rows, query, star_bands=None, tags_map=None):
    """对缓存行列表执行查询。返回 (matches, total)；语法错误抛 ValueError。"""
    clauses = parse(query)
    bands = dict(DEFAULT_STAR_BANDS)
    for k, v in (star_bands or {}).items():
        try:
            bands[int(k)] = float(v)
        except (TypeError, ValueError):
            pass
    tags_map = tags_map or {}
    matches = []
    for row in rows:
        tags = tags_map.get(row["path"]) or []
        try:
            if all(_match(c, row, tags, bands) for c in clauses):
                matches.append(row)
        except ValueError:
            raise
    matches.sort(key=lambda r: (r.get("score") is None,
                                -(r.get("score") or 0)))
    return matches, len(rows)


# ── IPC ─────────────────────────────────────────────────────────────────────

@command("search_photos", "selection",
         "[选片] 字段搜索已评分照片库。语法: score:>7 flag:pick keyword:人像 "
         "raw:false w:>=3000 orient:portrait date:2026-04 rating:>=3 name:xxx "
         "ext:arw，多个条件 AND，裸词匹配文件名/标签。只读。",
         params_schema={"type": "object", "properties": {
             "query": {"type": "string", "description": "查询串（见描述）"},
             "folder": {"type": "string", "description": "限定文件夹（与 paths 二选一）"},
             "paths": {"type": "array", "items": {"type": "string"},
                        "description": "限定路径列表（与 folder 二选一）"},
             "star_bands": {"type": "object",
                             "description": "星级分段 {\"5\":8.5,...}，省略用默认"},
             "limit": {"type": "integer", "description": "返回上限，默认 2000"}},
          "required": ["query"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["folder"])
def handle_search_photos(params):
    query = str(params.get("query") or "").strip()
    if not query:
        return {"error": "缺少 query"}
    try:
        parse(query)  # 先校验语法，尽早给可读错误
    except ValueError as e:
        return {"error": str(e)}

    mv = str(params.get("model_version") or model_version())
    paths = params.get("paths")
    if paths:
        rows = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            row = score_cache.get(p, mv, int(st.st_mtime), st.st_size)
            if row:
                rows.append(row)
    else:
        folder = params.get("folder")
        if not folder:
            return {"error": "需要 folder 或 paths"}
        rows = score_cache.get_folder(folder, mv)

    try:
        tags_map = score_cache.get_tags([r["path"] for r in rows])
    except Exception:
        tags_map = {}

    try:
        matches, total = search_rows(rows, query,
                                     params.get("star_bands"), tags_map)
    except ValueError as e:
        return {"error": str(e)}

    limit = max(1, min(10000, int(params.get("limit") or 2000)))
    out = [{"path": r["path"], "score": r.get("score"),
            "manual_keep": r.get("manual_keep") or 0,
            "width": r.get("width"), "height": r.get("height"),
            "is_raw": bool(r.get("is_raw")), "mtime": r.get("mtime"),
            "tags": tags_map.get(r["path"]) or []}
           for r in matches[:limit]]
    return {"ok": True, "count": len(out), "total": total,
            "truncated": len(matches) > limit, "matches": out}


def register_handlers():
    # P0-1 统一命令注册表：@command 装饰器为唯一登记点
    command_registry.register_all()
    send_log("search_query module loaded (IPC via command_registry)", "info")
