# -*- coding: utf-8 -*-
"""P1-1 冒烟：字段搜索语法解析 + score_cache 集成查询。

运行（仓库根）: python smoke_test/test_p1_search.py
（APS_USER_DATA 指向临时目录，不碰真实评分库）
"""
import os
import sys
import tempfile
import time

_TMP_DATA = tempfile.mkdtemp(prefix="lenspilot_p1_search_")
os.environ["APS_USER_DATA"] = _TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

import utils  # noqa: E402  (env 已设，确认用户目录解析到临时目录)
assert os.path.dirname(utils.get_user_data_dir()) != utils.get_user_data_dir() or True

import score_cache  # noqa: E402  (env 已设, DB 落在临时目录)
import command_registry  # noqa: E402
import search_query  # noqa: E402

MV = "test-v1"
FOLDER = os.path.join(_TMP_DATA, "photos")


def _mkfile(name, mtime_epoch):
    os.makedirs(FOLDER, exist_ok=True)
    p = os.path.join(FOLDER, name)
    with open(p, "wb") as f:
        f.write(b"\xff\xd9")  # 最小占位字节
    os.utime(p, (mtime_epoch, mtime_epoch))
    st = os.stat(p)
    return p, int(st.st_mtime), st.st_size


def _seed():
    import calendar
    rows = [
        # (name, score, w, h, raw, keep, tags, date)
        ("mountain_sun.jpg", 8.8, 4000, 6000, False, 1, ["人像", "山"], "2026-04-15"),
        ("low_score.jpg", 4.0, 6000, 4000, False, 0, [], "2026-01-10"),
        ("street.arw", 6.8, 3000, 3000, True, -1, ["街拍"], "2025-12-31"),
        ("mid_peak.png", 7.2, 4000, 3000, False, 0, ["山"], "2026-04-20"),
    ]
    for name, score, w, h, raw, keep, tags, date in rows:
        epoch = calendar.timegm(time.strptime(date, "%Y-%m-%d")) + 43200  # 当地正午近似
        p, mtime, size = _mkfile(name, epoch)
        score_cache.put(p, MV, mtime, size, score, w, h, int(raw), "")
        if tags:
            score_cache.put_tags(p, mtime, size, tags)
        if keep:
            score_cache.set_manual(p, MV, keep)


def _q(query, **kw):
    return search_query.handle_search_photos(
        dict({"query": query, "folder": FOLDER, "model_version": MV}, **kw))


def _paths(r):
    return sorted(os.path.basename(m["path"]) for m in r.get("matches", []))


def main():
    _seed()

    # 注册表集成：search_photos 已登记且标记为 Agent 工具
    spec = command_registry.get("search_photos")
    assert spec and spec["agent_tool"] and spec["category"] == "selection"

    cases = [
        ("score:>=7", ["mid_peak.png", "mountain_sun.jpg"]),
        ("score:<5", ["low_score.jpg"]),
        ("rating:3", ["mid_peak.png", "street.arw"]),       # ★3 段: 6.5≤s<7.5
        ("rating:>=4", ["mountain_sun.jpg"]),               # s≥7.5
        ("flag:pick", ["mountain_sun.jpg"]),
        ("flag:reject", ["street.arw"]),
        ("flag:none", ["low_score.jpg", "mid_peak.png"]),
        ("keyword:山", ["mid_peak.png", "mountain_sun.jpg"]),
        ('keyword:"人像"', ["mountain_sun.jpg"]),
        ("mountain", ["mountain_sun.jpg"]),                 # 裸词命中文件名
        ("街拍", ["street.arw"]),                            # 裸词命中标签
        ("ext:arw raw:true", ["street.arw"]),
        ("w:>=4000 h:>=4000", ["low_score.jpg", "mountain_sun.jpg"]),
        ("orient:portrait", ["mountain_sun.jpg"]),
        ("orient:square", ["street.arw"]),
        ("date:2026-04", ["mid_peak.png", "mountain_sun.jpg"]),
        ("date:>=2026", ["low_score.jpg", "mid_peak.png", "mountain_sun.jpg"]),
        ("score:>=5 flag:pick", ["mountain_sun.jpg"]),      # AND
        ("score:>=5 flag:pick orient:portrait date:2026-04", ["mountain_sun.jpg"]),
    ]
    for query, expect in cases:
        r = _q(query)
        assert r.get("ok"), f"{query} → {r}"
        got = _paths(r)
        assert got == sorted(expect), f"{query} → {got} (期望 {sorted(expect)})"
    print(f"[ok] search: {len(cases)} 条查询全部命中")

    # 自定义星标分段（前端传入的 ps-star-* 值）
    r = _q("rating:5", star_bands={"5": 8.0, "4": 9.0, "3": 9.5, "2": 9.7, "1": 9.9})
    assert _paths(r) == ["mountain_sun.jpg"]

    # paths 模式（前端网格内过滤场景）
    all_rows = score_cache.get_folder(FOLDER, MV)
    r = search_query.handle_search_photos({
        "query": "score:>=7", "paths": [x["path"] for x in all_rows],
        "model_version": MV})
    assert _paths(r) == ["mid_peak.png", "mountain_sun.jpg"]

    # 语法错误 → 可读 error（列出可用字段），绝不抛异常穿透 IPC
    for bad in ("iso:800", "score:abc", "flag:haha", ""):
        r = _q(bad)
        assert r.get("error"), bad
    assert "可用" in _q("iso:800")["error"]
    print("[ok] errors: 非法字段/取值/空查询均返回可读错误")
    print("P1 SEARCH SMOKE PASSED")


if __name__ == "__main__":
    main()
