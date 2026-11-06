# -*- coding: utf-8 -*-
"""P0-1 Agent 端到端：真实导入 agent_module，验证注册表工具生成 + 工作夹沙箱。

运行（仓库根）: python smoke_test/test_p0_agent_tools.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

GEN = {"get_edits", "apply_edits", "save_edits", "auto_tone_editor", "get_image_info"}


def main():
    import agent_module
    import command_registry

    # 模拟真实引导顺序: agent_module 先于 photo_editor 加载,工具生成
    # 惰性发生在首次 _tool_specs()（启动顺序无关）
    agent_module.register_handlers()
    import photo_editor
    photo_editor.register_handlers()

    agent_module._tool_specs()
    got = GEN & set(agent_module.TOOL_REGISTRY)
    assert got == GEN, f"缺工具: {GEN - got}"

    specs = {s["function"]["name"]: s["function"] for s in agent_module._tool_specs()}
    for name in GEN:
        assert name in specs, f"LLM specs 缺 {name}"
        assert specs[name]["parameters"].get("type") == "object"
    print(f"[ok] agent tools: {len(GEN)} 个注册表工具已进入 LLM specs "
          f"(总工具数 {len(specs)})")

    # 工作夹沙箱: 注册表工具的 path 越界必须被拒
    fn = agent_module.TOOL_REGISTRY["get_edits"]["fn"]
    session = type("S", (), {"work_folder": ROOT})()
    try:
        fn(session, {"path": "C:/Windows/win.ini"})
        raise AssertionError("沙箱未拦截工作夹外路径")
    except agent_module.SandboxViolation:
        pass
    # 工作夹内路径正常走通(PROJECT.md 真实存在 → 返回默认编辑参数,端到端执行)
    r = fn(session, {"path": os.path.join(ROOT, "PROJECT.md")})
    assert isinstance(r, dict) and "edits" in r and r.get("has_edits") is False
    print("[ok] sandbox: 工作夹外拒绝 / 工作夹内放行并端到端返回")
    print("AGENT TOOLS SMOKE PASSED")


if __name__ == "__main__":
    main()
