# -*- coding: utf-8 -*-
"""lr-v1 蒸馏学生接入冒烟测试。

单元部分（默认，纯 CPU、零模型资产、秒级）：
    1. SYSTEM_CONTRACT 逐字校验（sha256 钉服务器训练数据实测值）
    2. 档位 → 引擎口径换算（exposure k/3、代表值、WB 偏移 6500 锚、
       中性档省略、strength 插值）
    3. parse_json_loose（围栏 / 前后杂文 / 坏 JSON）
    4. lr_inspector 诊断文字（σ/μ 修正行，与训练输入同构）
    5. ready() 资产探测（临时目录模拟就绪/缺失）

live 部分（--live，需 distill_assets/Qwen3-VL-4B-Instruct + lr-v1 adapter；
首次加载分钟级，CPU 推理 4B 单图约分钟级）：
    端到端单图 → 9 档位合法 + options 形状 + 叙事结构 + 渲染应用不炸。

用法：
    python smoke_test/test_lr_distill.py                    # 单元
    python smoke_test/test_lr_distill.py --live             # 端到端（默认图）
    python smoke_test/test_lr_distill.py --live --img X.jpg
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "electron_app" / "python_backend"))

from photo_s_plugin_auto_tone.core import lr_distill  # noqa: E402

_CONTRACT_SHA = "b83c35908345d86bc3494a224f1615ad5a115b9d1850e3dfeacf983a8cf5f056"


def _bands(**over):
    b = {"exposure": "E0", "contrast": "C0", "saturation": "S0", "vibrance": "V0",
         "wb_temp": "WB_N", "wb_tint": "TN", "clarity": "CL0", "texture": "TX0",
         "dehaze": "DH0"}
    b.update(over)
    return b


def test_contract():
    c = lr_distill.SYSTEM_CONTRACT
    assert len(c) == 601, f"contract len {len(c)} != 601"
    sha = hashlib.sha256(c.encode("utf-8")).hexdigest()
    assert sha == _CONTRACT_SHA, f"contract sha {sha} != {_CONTRACT_SHA}（转录损坏）"
    print("[ok] SYSTEM_CONTRACT 601 字符，sha256 与训练数据一致")


def test_bands_to_options():
    f = lr_distill.bands_to_options
    o = f(_bands())
    assert o == {"contrast": 1.06, "saturation": 1.00, "vibrance": 0.0,
                 "clarity": 0.0, "texture": 0.0, "dehaze": 0.0}, o
    assert "exposure" not in o and "wb_temp" not in o and "wb_tint" not in o

    o = f(_bands(exposure="E+3", vibrance="V+2", saturation="S-1",
                 contrast="C+1", clarity="CL+2", texture="TX-1", dehaze="DH+1",
                 wb_temp="WB_W1", wb_tint="TM2"))
    assert abs(o["exposure"] - 1.0) < 1e-9
    assert abs(o["vibrance"] - 0.15) < 1e-9
    assert abs(o["saturation"] - 0.95) < 1e-9
    assert abs(o["contrast"] - 1.13) < 1e-9
    assert abs(o["clarity"] - 0.30) < 1e-9
    assert abs(o["texture"] + 0.12) < 1e-9
    assert abs(o["dehaze"] - 0.08) < 1e-9
    assert o["wb_temp"] == 6800.0, o["wb_temp"]     # 6500 + 300（schema §4）
    assert o["wb_tint"] == 12.0

    o = f(_bands(exposure="E-2", wb_temp="WB_C3", wb_tint="TG1"))
    assert abs(o["exposure"] + 2 / 3) < 1e-9
    assert o["wb_temp"] == 5300.0                    # 6500 - 1200
    assert o["wb_tint"] == -5.0
    print("[ok] bands_to_options：代表值/WB 偏移(6500 锚)/中性档省略")

    for bad in [{"exposure": "E+3.5"}, {"exposure": "X1"}, {"contrast": "C+2"},
                {"vibrance": "V+4"}, {"dehaze": "DH-1"}, {"wb_temp": "WB_X1"},
                {"wb_tint": "TK1"}, {"texture": "tx0"}]:
        try:
            f(_bands(**bad))
            raise AssertionError(f"should reject {bad}")
        except ValueError:
            pass
    print("[ok] 非法档位全部拒绝")


def test_strength():
    o = lr_distill.bands_to_options(
        _bands(exposure="E+3", contrast="C+1", wb_temp="WB_W1", wb_tint="TM1"))
    s = lr_distill._apply_strength(o, 0.5)
    assert abs(s["exposure"] - 0.5) < 1e-9
    assert abs(s["contrast"] - 1.065) < 1e-9
    assert "wb_temp" not in s                          # WB 不插值：直接放弃
    assert abs(s["wb_tint"] - 2.5) < 1e-9
    assert lr_distill._apply_strength(o, 1.0) == o    # 满强度原样
    print("[ok] strength 插值（WB 保守放弃）")


def test_parse():
    p = lr_distill.parse_json_loose
    good = '{"scene": "x", "params": {}}'
    assert p(f"```json\n{good}\n```") == {"scene": "x", "params": {}}
    assert p(f"前置杂文 {good} 后置杂文") == {"scene": "x", "params": {}}
    assert p("没有任何 json") is None
    assert p("```json\n{broken\n```") is None
    print("[ok] parse_json_loose：围栏/杂文/坏 JSON")


def test_inspector():
    from photo_s_plugin_auto_tone.core import lr_inspector
    img_path = ROOT / "smoke_test" / "_lut_preview.jpg"
    if not img_path.is_file():
        print("[skip] inspector（无测试图）")
        return
    from PIL import Image
    txt = lr_inspector.inspector_text(Image.open(img_path), name="t.jpg")
    assert "有效对比(σ/μ)=" in txt and "亮度: 平均=" in txt and "场景:" in txt
    print(f"[ok] inspector 文字 {len(txt)} 字符（含 σ/μ 修正行）")


def test_defensive_parse():
    p = lr_distill.parse_student_output
    # 标准学生输出（契约形态）不受影响
    ok = json.dumps({"scene": "s", "reasoning": ["r"], "summary": "m",
                     "params": _bands()}, ensure_ascii=False)
    bands, notes, obj = p(ok)
    assert bands == _bands() and not notes and obj["scene"] == "s"

    # 别名（中/英）+ 纯字符串档位 + 顶层平铺（漏 params 层）+ 尾逗号 + 前置杂文
    weird = ('前置说明 {"曝光": {"band": "E+1"}, "饱和度": "S-1", "Vibrance": "V+2", '
             '"色温": "WB_N", "wb_tint": "TN", "clarity": "CL0", "texture": "TX-1", '
             '"dehaze": "DH0", "contrast": "C0",}')
    bands, notes, _ = p(weird)
    assert bands["exposure"] == "E+1" and bands["saturation"] == "S-1"
    assert bands["vibrance"] == "V+2" and bands["texture"] == "TX-1" and not notes

    # value 反推（band 缺失）：数值/字符串数值/全角符号 → 最近档
    bands, notes, _ = p(json.dumps({"params": {
        "exposure": {"value": 1.0}, "saturation": {"value": "0.95"},
        "vibrance": {"value": 0.14}, "wb_temp": {"value": 300},
        "contrast": {"value": 1.10}, "clarity": {"value": "＋0.30"},
        "texture": {"value": 0}, "dehaze": {"value": 0.08}, "wb_tint": {"value": 0}}}))
    assert bands["exposure"] == "E+3" and bands["saturation"] == "S-1"
    assert bands["vibrance"] == "V+2" and bands["wb_temp"] == "WB_W1"
    assert bands["contrast"] == "C+1" and bands["clarity"] == "CL+2"
    assert bands["dehaze"] == "DH+1" and bands["wb_tint"] == "TN"  # 中性死区

    # exposure 越界钉回 schema §2 范围
    b = dict(_bands()); b["exposure"] = {"band": "E+9"}
    bands, notes, _ = p(json.dumps({"params": b}))
    assert bands["exposure"] == "E+8" and any("越界" in n for n in notes)

    # 部分抢救：1 个烂字段 → 中性化 + note；3 个烂字段 → 整体放弃
    b = dict(_bands()); b["saturation"] = {"band": "SX-1"}
    bands, notes, _ = p(json.dumps({"params": b}))
    assert bands["saturation"] == "S0" and any("saturation" in n for n in notes)
    b = dict(_bands())
    for f in ("saturation", "vibrance", "texture"):
        b[f] = {"band": "?"}
    bands, notes, _ = p(json.dumps({"params": b}))
    assert bands is None and any("放弃" in n for n in notes)

    # 括号配平：尾部杂文里出现 } 不截错
    bands, _, _ = p(json.dumps({"params": _bands()}) + " 后面 } 杂文")
    assert bands == _bands()
    print("[ok] 防御式解析：别名/平铺/尾逗号/value反推/中性死区/越界钉回/部分抢救/括号配平")


def test_ready():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "base"
        lora = Path(td) / "lora"
        adv = lr_distill.LRDistillAdvisor(lora_path=str(lora), base_path=str(base))
        assert not adv.ready() and "LoRA" in adv.error
        lora.mkdir()
        (lora / "adapter_config.json").write_text("{}")
        (lora / "adapter_model.safetensors").write_bytes(b"x" * 8)
        assert not adv.ready() and "base snapshot" in adv.error
        base.mkdir()
        (base / "config.json").write_text("{}")
        (base / "model-00001-of-00002.safetensors").write_bytes(b"x" * 8)
        assert adv.ready()
    print("[ok] ready() 资产探测：LoRA 缺失 / 基座缺失 / 齐备")


def test_live(img_path):
    adv = lr_distill.get_distill_advisor()
    print(f"[live] base={adv.base_path}\n[live] lora={adv.lora_path}")
    assert adv.ready(), f"assets not ready: {adv.error}"
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    res = adv.advise(img, name=os.path.basename(img_path))
    assert "error" not in res, res.get("error")
    print(f"[live] {res['duration_ms']}ms | summary: {res['narrative']['summary']}")
    print(f"[live] scene: {res['narrative']['scene']}")
    for r in res["narrative"]["reasoning"]:
        print(f"[live]   - {r}")
    print(f"[live] bands: {res['narrative']['bands']}")
    print(f"[live] options: {json.dumps(res['options'], ensure_ascii=False)}")
    assert set(res["narrative"]["bands"]) == set(lr_distill.FIELDS)
    for f, b in res["narrative"]["bands"].items():
        assert lr_distill.band_valid(f, b), f"invalid band {f}={b}"
    for k, v in res["options"].items():
        assert isinstance(v, float) and (v == v), (k, v)
    # 渲染应用不炸（编辑器预览同款路径）
    sys.path.insert(0, str(ROOT / "electron_app" / "python_backend"))
    from photo_s.autotone import apply_auto_tone_params
    small = img.copy()
    small.thumbnail((800, 800))
    out = apply_auto_tone_params(small, {"options": res["options"]})
    assert out.size == small.size
    print("[live] apply_auto_tone_params 渲染通过")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--img", default=str(ROOT / "smoke_test" / "_lut_preview.jpg"))
    args = ap.parse_args()

    test_contract()
    test_bands_to_options()
    test_strength()
    test_parse()
    test_defensive_parse()
    test_inspector()
    test_ready()
    print("\n==== 单元测试全部通过 ====")
    if args.live:
        print("\n==== live 端到端 ====")
        test_live(args.img)
