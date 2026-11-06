# -*- coding: utf-8 -*-
"""P6-1 冒烟：命名版本快照（create/list/apply/delete + 同名覆盖 + 注册表）。

运行（仓库根）: python smoke_test/test_p6_versions.py
"""
import os
import sys
import tempfile

os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_p6_vc_")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

import command_registry  # noqa: E402
import edit_versions as ev  # noqa: E402


def main():
    photo = os.path.join(tempfile.mkdtemp(prefix="lenspilot_vc_"), "a.jpg")
    open(photo, "wb").write(b"\xff\xd9")

    r1 = ev.handle_vcopy_create({"path": photo, "name": "黑白版",
                                 "edits": {"grayscale": True, "contrast": 1.2}})
    assert r1["ok"] and not r1.get("overwritten")
    r2 = ev.handle_vcopy_create({"path": photo, "name": "高饱版",
                                 "edits": {"saturation": 1.4}})
    assert r2["ok"]
    # 同名覆盖保 id
    r3 = ev.handle_vcopy_create({"path": photo, "name": "黑白版",
                                 "edits": {"grayscale": True, "contrast": 1.4}})
    assert r3["ok"] and r3["overwritten"] and r3["copy"]["id"] == r1["copy"]["id"]

    lst = ev.handle_vcopy_list({"path": photo})
    assert lst["count"] == 2
    names = {c["name"] for c in lst["copies"]}
    assert names == {"黑白版", "高饱版"}
    assert all("grayscale" in c["keys"] or "saturation" in c["keys"] for c in lst["copies"])

    applied = ev.handle_vcopy_apply({"path": photo, "vcid": r2["copy"]["id"]})
    assert applied["ok"] and applied["edits"]["saturation"] == 1.4
    assert ev.handle_vcopy_apply({"path": photo, "vcid": "nope"})["error"]

    d = ev.handle_vcopy_delete({"path": photo, "vcid": r1["copy"]["id"]})
    assert d["ok"] and ev.handle_vcopy_list({"path": photo})["count"] == 1
    assert ev.handle_vcopy_delete({"path": photo, "vcid": r1["copy"]["id"]})["error"]

    # 注册表：4 条命令登记，list/apply/create 为 Agent 工具，delete 标记破坏性
    for name in ("vcopy_list", "vcopy_create", "vcopy_apply", "vcopy_delete"):
        spec = command_registry.get(name)
        assert spec, name
    assert command_registry.get("vcopy_delete")["destructive"]
    assert command_registry.get("vcopy_list")["agent_tool"]
    print("[ok] versions: 创建/覆盖/列表/应用/删除/registry 全部通过")
    print("P6 VERSIONS SMOKE PASSED")


if __name__ == "__main__":
    main()
