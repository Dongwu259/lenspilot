# -*- coding: utf-8 -*-
"""P2-2 冒烟：追加式编辑日志（append-only / 轮转 / 半行容错）。

运行（仓库根）: python smoke_test/test_p2_journal.py
"""
import json
import os
import sys
import tempfile

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p2_journal_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import edit_journal as ej  # noqa: E402
import photo_editor  # noqa: E402


def main():
    tmp = tempfile.mkdtemp(prefix="lenspilot_journal_photos_")
    p1 = os.path.join(tmp, "a.jpg")
    p2 = os.path.join(tmp, "b.jpg")
    for p in (p1, p2):
        open(p, "wb").write(b"\xff\xd9")

    # 经 photo_editor 钩子落日志
    photo_editor.handle_save_edits({"path": p1, "edits": {"exposure": 0.3, "contrast": 1.2}})
    photo_editor.handle_save_edits({"path": p1, "edits": {"curves": {"rgb": [[0, 0], [255, 255]]}}})
    photo_editor.handle_save_edits({"path": p2, "edits": {}})
    photo_editor.handle_reset_edits({"path": p1})

    entries = ej.query()
    assert [e["op"] for e in entries] == ["save", "save", "save", "reset"]
    assert entries[0]["path"] == p1 and "contrast" in entries[0]["keys"]
    assert entries[1]["keys"] == ["curves"]  # 恒等曲线不记为非默认键
    assert ej.query(path=p1) and all(e["path"] == p1 for e in ej.query(path=p1))
    assert len(ej.query(limit=2)) == 2

    # 半行（崩溃残留）容错：手写一行坏 JSON 不影响后续读取
    with open(ej._PATH, "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "op": "save", "path": "x"\n')  # 未闭合(含换行)
    ej.append("save", p2, keys=["clarity"])
    entries = ej.query()
    assert entries[-1]["op"] == "save" and entries[-1]["keys"] == ["clarity"]

    # 轮转：超限触发 .1 文件，query 仍按时间正序读全
    with open(ej._PATH, "a", encoding="utf-8") as f:
        f.write("x" * (ej._MAX_BYTES))  # 直接撑爆触发下一次 append 的轮转
    ej.append("save", p1, keys=["exposure"])
    assert os.path.isfile(ej._PATH + ".1")
    entries = ej.query(limit=500)
    assert entries[0]["ts"] <= entries[-1]["ts"]

    # registry：edit_journal_list 已登记且为 Agent 工具
    import command_registry
    spec = command_registry.get("edit_journal_list")
    assert spec and spec["agent_tool"]

    r = photo_editor  # noqa: F841  (确认钩子与 handler 在同一进程可用)
    from edit_journal import handle_edit_journal_list
    r2 = handle_edit_journal_list({"path": p2, "limit": 5})
    assert r2["ok"] and all(e["path"] == p2 for e in r2["entries"])
    print("[ok] journal: append-only/过滤/limit/半行容错/轮转/registry 全部通过")
    print("P2 JOURNAL SMOKE PASSED")


if __name__ == "__main__":
    main()
