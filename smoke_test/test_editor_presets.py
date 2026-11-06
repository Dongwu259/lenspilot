#!/usr/bin/env python3
"""
test_editor_presets.py — 编辑器预设后端单元测试(0.8.9)

覆盖:内置 LUT 预设解析、用户预设 CRUD、同名覆盖、非法输入、
内置删除保护、edits sanitize(未知键剔除/类型归一)。
运行: python smoke_test/test_editor_presets.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

# 隔离用户数据目录(预设 JSON 写到临时目录)
os.environ["APS_USER_DATA"] = tempfile.mkdtemp(prefix="lenspilot_test_")

# stub service(editor_presets 只需要 send_log/register_handler)
_fake_handlers = {}
svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda *a, **k: None
svc.register_handler = lambda n, h: _fake_handlers.__setitem__(n, h)
sys.modules["service"] = svc

import editor_presets as ep  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  [OK] {name}")


def main():
    # 1. 内置预设: P3 组合模板落地后 = 14 个功能组合 + 5 个 LUT, 共 19 个
    r = ep.handle_list_editor_presets({})
    builtins = [p for p in r["presets"] if p["builtin"]]
    luts = [p for p in builtins if (p.get("category") or "lut") == "lut"]
    assert len(builtins) == 19, f"builtin count = {len(builtins)}"
    assert len(luts) == 5, f"lut builtin count = {len(luts)}"
    assert all(os.path.isfile(p["edits"]["lut_file"]) for p in luts), "lut 文件缺失"
    assert all(p["nameKey"] for p in builtins), "缺 nameKey"
    ok(f"内置预设 {len(builtins)} 个(含 5 LUT), lut 路径全部存在: {[p['id'] for p in luts]}")

    # 2. 保存用户预设(含未知键 bogus + 类型归一)
    r = ep.handle_save_editor_preset({"name": "测试预设", "edits": {
        "exposure": "0.5", "contrast": 1.2, "grayscale": 1, "bogus_key": 999, "lut_file": "x.cube"}})
    assert r.get("ok"), r
    pid = r["preset"]["id"]
    e = r["preset"]["edits"]
    assert "bogus_key" not in e, "未知键未剔除"
    assert e["exposure"] == 0.5 and isinstance(e["exposure"], float), f"类型归一失败: {e}"
    assert e["grayscale"] is True, "bool 归一失败"
    ok(f"保存用户预设 {pid},sanitize 正确: {e}")

    # 3. 同名覆盖(保 id)
    r2 = ep.handle_save_editor_preset({"name": "测试预设", "edits": {"exposure": 1.0}})
    assert r2.get("ok") and r2.get("overwritten") and r2["preset"]["id"] == pid, r2
    assert r2["preset"]["edits"] == {"exposure": 1.0}
    ok("同名覆盖保 id")

    # 4. 列表含用户预设且在内置之后
    r3 = ep.handle_list_editor_presets({})
    ids = [p["id"] for p in r3["presets"]]
    assert pid in ids and ids.index(pid) >= 5, ids
    ok("列表:内置在前,用户在后")

    # 5. 非法输入
    assert "error" in ep.handle_save_editor_preset({"name": "", "edits": {"exposure": 1}})
    assert "error" in ep.handle_save_editor_preset({"name": "x" * 31, "edits": {"exposure": 1}})
    assert "error" in ep.handle_save_editor_preset({"name": "ok", "edits": "not-a-dict"})
    assert "error" in ep.handle_save_editor_preset({"name": "ok", "edits": {"bogus": 1}})
    ok("非法 name/edits/全未知键 全部拒绝")

    # 6. 内置预设删除保护
    assert "error" in ep.handle_delete_editor_preset({"id": "builtin:warm-gold"})
    ok("内置预设不可删")

    # 7. 删除用户预设
    assert ep.handle_delete_editor_preset({"id": pid}).get("ok")
    assert not any(p["id"] == pid for p in ep.handle_list_editor_presets({})["presets"])
    assert "error" in ep.handle_delete_editor_preset({"id": pid})  # 再删 → not found
    ok("用户预设删除 + 幂等 not found")

    # 8. handler 注册
    ep.register_preset_handlers()
    for name in ("list_editor_presets", "save_editor_preset", "delete_editor_preset"):
        assert name in _fake_handlers, name
    ok(f"3 个 IPC handler 已注册: {sorted(_fake_handlers)}")

    print(f"\n全部通过 ({PASS} 项)")


if __name__ == "__main__":
    main()
