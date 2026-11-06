#!/usr/bin/env python3
"""
test_auto_retouch.py — photo_s v2.5.0 AI 自动修图接入测试(0.9.0)

覆盖:
  1. photo_s 2.5.0 vendored 版本 + auto-tone 插件手动注册(冻结环境兜底路径)
  2. apply_auto_tone_params 9 字段应用/中性跳过(经 apply_edits 的 auto_tone 键)
  3. resolve_auto_tone_options 参数并入 ProcessOptions(mock provider,
     验证 auto_tone=None 防二次推理 + 用户显式 WB 优先)
  4. auto_retouch_keepers 管线步骤: suggest 引擎端到端(真图) + AI 未就绪自动降级
  5. 编辑器 handle_auto_tone_editor(suggest)→ apply_edits 往返
  6. i18n 新 key 三语齐
运行: python smoke_test/test_auto_retouch.py  (仓库根或 electron_app 下)
"""
import io
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

# stub service(photo_editor / auto_tone_manager 只需 send_event/send_log/register_handler;
# pipeline_runner 另需 _app_config)
svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda *a, **k: None
svc.register_handler = lambda n, h: None
svc._app_config = {}
svc.subscribe_events = lambda fn: (lambda: None)
sys.modules["service"] = svc

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  [OK] {name}")


def _base_img(size=(320, 240)):
    """渐变+色块测试图(纯灰对 clarity/vibrance 等算法不敏感)。"""
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for y in range(size[1]):
        for x in range(size[0]):
            arr[y, x] = (x * 255 // size[0], y * 255 // size[1],
                         (x + y) * 255 // (size[0] + size[1]))
    arr[20:80, 20:100] = (220, 30, 30)  # 高饱和红色区域
    return Image.fromarray(arr, "RGB")


def _diff(a, b):
    return float(np.abs(
        np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)
    ).mean())


# ═══════════════ 1. 版本 + 插件注册 ═══════════════

print("\n[1] photo_s 2.5.0 vendored + 插件手动注册")
import photo_s  # noqa: E402
assert photo_s.__version__ == "2.5.0", photo_s.__version__
ok(f"photo_s __version__ == 2.5.0")

import photo_s_plugins  # noqa: E402
assert photo_s_plugins.ensure_auto_tone_plugin() is True
ok("ensure_auto_tone_plugin() 注册成功(幂等)")

from photo_s.plugin import find_provider  # noqa: E402
prov = find_provider("auto_tone")
assert prov is not None and "auto_tone" in getattr(prov, "provides", ())
ok(f"find_provider('auto_tone') → {type(prov).__name__}")

# 幂等: 再次注册不重复挂
n_before = len(photo_s_plugins.ensure_auto_tone_plugin.__globals__ is not None
               and __import__("photo_s.plugin", fromlist=["discover_plugins"])
               .discover_plugins())
photo_s_plugins.ensure_auto_tone_plugin()
from photo_s.plugin import discover_plugins  # noqa: E402
n_after = len([p for p in discover_plugins() if getattr(p, "name", "") == "auto_tone"])
assert n_after == 1, f"auto_tone provider 数量异常: {n_after}"
ok("重复注册幂等(恰好 1 个 auto_tone provider)")

st = photo_s_plugins.ai_engine_status()
assert set(st) >= {"plugin", "torch", "core_weights", "tower", "ready", "reason"}
assert st["plugin"] is True and st["ready"] in (True, False)
ok(f"ai_engine_status() 探测可用(ready={st['ready']}, reason={st['reason']})")

# ═══════════════ 2. apply_auto_tone_params / apply_edits auto_tone 键 ═══════════════

print("\n[2] apply_edits 的 auto_tone 键(9 字段应用/中性跳过)")
import photo_editor as pe  # noqa: E402
assert pe.DEFAULT_EDITS.get("auto_tone", "missing") is None
ok("DEFAULT_EDITS 含 auto_tone=None(默认关)")

img = _base_img()
tmp = os.path.join(tempfile.gettempdir(), "_at_case2.jpg")
img.save(tmp)

# 2a. 中性参数: 图像不变
edits = dict(pe.DEFAULT_EDITS)
edits["auto_tone"] = {"engine": "auto", "options": {}, "confidence": 0.9, "strength": 1.0}
out = pe.apply_edits(img, edits)
assert _diff(img, out) < 0.01, _diff(img, out)
ok("空 options → 图像不变")

# 2b. 非中性参数: 经 photo_s 真实管线生效
edits["auto_tone"] = {"engine": "auto",
                      "options": {"exposure": 0.5, "contrast": 1.3, "vibrance": 0.4},
                      "confidence": 0.9, "strength": 1.0}
out2 = pe.apply_edits(img, edits)
d2 = _diff(img, out2)
assert d2 > 1.0, f"auto_tone 应用后差异过小: {d2}"
ok(f"exposure/contrast/vibrance 应用生效(平均差 {d2:.1f})")

# 2c. wb_temp 白平衡生效
edits["auto_tone"] = {"engine": "auto", "options": {"wb_temp": 3000},
                      "confidence": 0.9, "strength": 1.0}
out3 = pe.apply_edits(img, edits)
assert _diff(img, out3) > 0.5
ok("wb_temp 生效")

# 2d. 非法结构安全跳过(不崩)
edits["auto_tone"] = {"engine": "auto", "options": "garbage"}
pe.apply_edits(img, edits)  # 不应抛
ok("非法 options 结构安全跳过")

# ═══════════════ 3. resolve_auto_tone_options(mock provider) ═══════════════

print("\n[3] resolve_auto_tone_options 参数协议(mock provider)")
import dataclasses  # noqa: E402
from photo_s.engine import ProcessOptions  # noqa: E402
from photo_s.autotone import resolve_auto_tone_options  # noqa: E402
import photo_s.plugin as _psplugin  # noqa: E402


class _FakeProvider:
    name = "auto_tone"
    provides = ("auto_tone",)

    def __init__(self, params):
        self._params = params

    def auto_tone_params(self, strength, ctx=None):
        return json.loads(json.dumps(self._params))  # deep copy


_orig = _psplugin._PLUGINS
try:
    _psplugin._PLUGINS = [_FakeProvider({
        "options": {"exposure": 0.3, "contrast": 1.15, "wb_temp": 4800.0},
        "local": [], "confidence": 0.88, "warnings": []})]

    opts = ProcessOptions(auto_tone=1.0)
    merged, params = resolve_auto_tone_options(opts, tmp)
    assert merged.auto_tone is None, "merged.auto_tone 应置 None(防引擎二次推理)"
    assert abs(merged.ev - 0.3) < 1e-3, merged.ev
    assert abs(merged.contrast - 1.15) < 1e-3
    assert abs(merged.wb_temp - 4800.0) < 1e-3
    assert params.get("confidence") == 0.88
    ok("预测字段映射进 ProcessOptions(ev/contrast/wb_temp), auto_tone=None")

    # 用户显式 WB 优先: options.wb_temp 已设 → 预测不覆盖
    opts2 = ProcessOptions(auto_tone=1.0, wb_temp=6500)
    merged2, _ = resolve_auto_tone_options(opts2, tmp)
    assert merged2.wb_temp == 6500
    ok("用户显式 WB 不被预测覆盖")

    # strength 传递: auto_tone=0.5 → provider 收到 strength=0.5
    seen = {}
    class _Spy(_FakeProvider):
        def auto_tone_params(self, strength, ctx=None):
            seen["strength"] = strength
            return super().auto_tone_params(strength, ctx)
    _psplugin._PLUGINS = [_Spy({
        "options": {"exposure": 0.3}, "local": [], "confidence": 0.9, "warnings": []})]
    merged3, _ = resolve_auto_tone_options(ProcessOptions(auto_tone=0.5), tmp)
    assert abs(seen["strength"] - 0.5) < 1e-6
    ok("strength 经 ProcessOptions.auto_tone 传递给 provider")
finally:
    _psplugin._PLUGINS = _orig

# ═══════════════ 4. auto_retouch_keepers 管线步骤 ═══════════════

print("\n[4] auto_retouch_keepers(suggest 端到端 + AI 未就绪降级)")
import threading  # noqa: E402
import pipeline_tool_steps as pts  # noqa: E402


class _FakeRun:
    def __init__(self, files, params=None, step_params=None):
        self.cancel_event = threading.Event()
        self.artifacts = {"keep_paths": files}
        self.params = params or {}
        self.step_params = step_params
        self.work_folder = tempfile.mkdtemp(prefix="_at_run_")
        self.run_id = "test_run"
        self.template = "test"
        self.status = "running"
        self.run_log = []

    def set(self):  # 兼容误用
        pass


files = []
work = tempfile.mkdtemp(prefix="_at_src_")
for i in range(3):
    p = os.path.join(work, f"img_{i}.jpg")
    _base_img((300 + i * 40, 220)).save(p)
    files.append(p)
run = _FakeRun(files, step_params={"engine": "suggest", "quality": 90})

r = pts.auto_retouch_keepers(run)
assert r.get("ok") is True, r
assert r.get("engine") == "suggest", r
assert r.get("retouched") == 3, r
assert run.artifacts.get("auto_retouch_engine") == "suggest"
assert run.artifacts.get("auto_retouch_count") == 3
outs = run.artifacts.get("auto_retouched_paths") or []
assert len(outs) == 3 and all(os.path.exists(p) for p in outs)
ok(f"suggest 引擎端到端: 3/3 张 → {os.path.basename(run.work_folder)}/_自动修图/")

# 4b. AI 未就绪自动降级(monkeypatch 探测函数——不再依赖环境真实状态,
#     权重已就绪的机器上同样能验证降级路径)
_orig_status = photo_s_plugins.ai_engine_status
photo_s_plugins.ai_engine_status = lambda: {"plugin": True, "torch": True,
                                            "core_weights": True,
                                            "tower": False, "ready": False,
                                            "reason": "tower"}
try:
    run2 = _FakeRun(files[:1], step_params={"engine": "auto"})
    r2 = pts.auto_retouch_keepers(run2)
finally:
    photo_s_plugins.ai_engine_status = _orig_status
assert r2.get("ok") is True, r2
assert r2.get("engine") == "suggest", f"AI 未就绪应降级 suggest: {r2}"
ok("engine=auto 且 AI 未就绪 → 自动降级 suggest(产物键一致)")

# 4c. keep 列表为空
run3 = _FakeRun([], step_params={"engine": "suggest"})
r3 = pts.auto_retouch_keepers(run3)
assert r3.get("ok") is True and run3.artifacts.get("auto_retouch_count") == 0
ok("keep 列表为空 → 跳过(ok)")

# 4d. 取消
run4 = _FakeRun(files, step_params={"engine": "suggest"})
run4.cancel_event.set()
r4 = pts.auto_retouch_keepers(run4)
assert r4.get("canceled") is True
ok("取消事件生效")

# 4e. _deliver_source: 全量成功优先修图产物;部分失败回落原图
run5 = _FakeRun(files)
run5.artifacts["auto_retouched_paths"] = list(outs)  # 3/3
assert pts._deliver_source(run5) == outs
ok("_deliver_source 全量成功 → 用修图产物")

run6 = _FakeRun(files)
run6.artifacts["auto_retouched_paths"] = outs[:1]  # 1/3 部分失败
assert pts._deliver_source(run6) == files
ok("_deliver_source 部分失败 → 回落 keep 原图")

# ═══════════════ 5. 编辑器 handler 往返 ═══════════════

print("\n[5] handle_auto_tone_editor(suggest) → apply_edits 往返")
r5 = pe.handle_auto_tone_editor({"path": tmp, "engine": "suggest"})
assert isinstance(r5, dict) and "error" not in r5, r5
assert r5["auto_tone"]["engine"] == "suggest"
assert r5["preview"].startswith("data:image/jpeg;base64,")
ok("handle_auto_tone_editor 返回预览+参数快照(suggest)")

edits = dict(pe.DEFAULT_EDITS)
edits["auto_tone"] = r5["auto_tone"]
out5 = pe.apply_edits(img, edits)
# suggest 对渐变+红色块图一般给出非空建议 → 有差异;若建议为空也视为通过(中性图)
d5 = _diff(img, out5)
ok(f"edits.auto_tone → apply_edits 往返成功(平均差 {d5:.2f})")

r6 = pe.handle_auto_tone_editor({"path": tmp, "strength": 0.5})
assert "error" not in r6
assert abs(r6["auto_tone"]["strength"] - 0.5) < 1e-6
ok("strength 参数传递")

bad = pe.handle_auto_tone_editor({"path": "Z:/no/such.jpg"})
assert bad.get("error")
ok("非法路径返回 error dict(不抛)")

# ═══════════════ 6. i18n 新 key 三语齐 ═══════════════

print("\n[6] i18n 新 key 三语齐")
# P2-6b 语言包拆分后：i18n.js 只剩逻辑，键在 i18n-data-{zh,en,ja}.js
_jsdir = os.path.join(BACKEND, "..", "src", "js")
i18n_src = io.open(os.path.join(_jsdir, "i18n.js"), encoding="utf-8").read()
for _lang in ("zh", "en", "ja"):
    _f = os.path.join(_jsdir, f"i18n-data-{_lang}.js")
    if os.path.exists(_f):
        i18n_src += io.open(_f, encoding="utf-8").read()
_KEYS = ["peAiTone", "peAiToneTitle", "peAiToneApplied", "peAiToneRule",
         "peAiToneFail", "atSettings", "atModelStatus", "atStatusChecking",
         "atDownload", "atStatusReady", "atStatusNoEngine", "atStatusNeedWeights",
         "atStatusNeedTower", "atStatusError", "atDlStarting", "atDlDone", "atDlError"]
missing = [k for k in _KEYS
           if len(re.findall(r"[{,\s]" + k + r":", i18n_src)) != 3]
assert not missing, f"missing: {missing}"
ok(f"{len(_KEYS)} 个新 key × 3 语言 全部齐")

# ═══════════════ 汇总 ═══════════════

print(f"\n全部通过 ({PASS} 项)")
try:
    os.remove(tmp)
except OSError:
    pass
