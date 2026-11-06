#!/usr/bin/env python3
"""test_tagger_real.py — 用 smoke_agent_key.txt 的真实多模态 LLM 端到端实测 AI 打标

读 smoke_test/smoke_agent_key.txt（3 行：base_url/api_key/model，# 为注释），
monkeypatch agent_module._load_agent_config 注入配置（multimodal=on），
对 store-assets/_demo_photos 的 5 张人像样片 + 1 张 ARW 跑完整 run_tagging：
  真实 API → JSON 解析 → score_cache 持久化 → 二次运行缓存跳过 → force 重打。

用法: python smoke_test/test_tagger_real.py

注意（2026-07-25 实测）：smoke_agent_key.txt 当前配置的 LongCat-2.0 是
纯文本模型（api.longcat.chat 仅此一个模型，不接受 image_url 内容），
本脚本会按预期全部判 FAIL，但能看到 last_error 诊断正确透出。
要测成功路径，把 key 文件换成视觉模型（GLM-4V / qwen-vl-max / doubao-vision 等
OpenAI 兼容端点，3 行格式不变）再跑即可。
"""
import io
import json
import os
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "electron_app" / "python_backend"
sys.path.insert(0, str(BACKEND_DIR))

failures = []


def check(cond, msg, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {msg}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(msg)


def load_key():
    lines = []
    with open(PROJECT_ROOT / "smoke_test" / "smoke_agent_key.txt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    if len(lines) < 3:
        raise SystemExit("smoke_agent_key.txt 格式错误：需要 3 行 base_url/api_key/model")
    return lines[0], lines[1], lines[2]


base_url, api_key, model = load_key()
print(f"[real-tagger] LLM: base_url={base_url} model={model} key={api_key[:6]}...(已隐藏)")

import agent_module  # noqa: E402

agent_module._load_agent_config = lambda: {
    "base_url": base_url, "api_key": api_key, "model": model,
    "multimodal": "on",
}

import photo_tagger  # noqa: E402
import score_cache  # noqa: E402

DEMO = PROJECT_ROOT / "store-assets" / "_demo_photos"
photos = sorted(str(p) for p in DEMO.glob("*.jpg"))
arw = str(DEMO / "capt_DSC01466.ARW")
if os.path.isfile(arw):
    photos.append(arw)  # 顺带验证 RAW 解码路径
check(len(photos) >= 5, f"样片就位（{len(photos)} 张，含 ARW={os.path.isfile(arw)}）")

# force=True 保证首轮真实调 API（不清库，避免动开发机既有缓存）
progress = []


def progress_cb(p):
    progress.append(dict(p))
    print(f"    进度 {p['current']}/{p['total']} tagged={p['tagged']} "
          f"failed={p['failed']} last={p.get('last_file','')}")


t0 = time.time()
summary = photo_tagger.run_tagging(photos, batch_size=4, force=True, progress_cb=progress_cb)
dt = time.time() - t0

print(f"\n== 打标结果（{dt:.1f}s）==")
print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                 ensure_ascii=False, indent=1))
if "error" in summary:
    check(False, f"预检通过（无 error）", summary.get("detail", summary["error"]))
    raise SystemExit(1)

check(summary["total"] == len(photos), f"total={summary['total']} == {len(photos)}")
check(summary["tagged"] >= len(photos) - 1,
      f"tagged={summary['tagged']}（允许个别失败）")
check(summary["failed"] <= 1, f"failed={summary['failed']}")
check(len(progress) >= 1, "tag_progress 回调有触发")

for p, tags in summary["results"].items():
    print(f"  {os.path.basename(p):26s} → {'、'.join(tags)}")

# 标签质量粗查：人像样片应命中题材/构图类目词
all_tags = [t for tags in summary["results"].values() for t in tags]
check(any(t in ("人像", "特写", "半身") for t in all_tags),
      "样片标签含人像/构图类目词（内容 sanity）")
check(all(len(t) <= 16 for t in all_tags), "标签长度均 ≤16 字符（_clean_tags 截断）")

# 持久化：直接从 score_cache 读回
cached = score_cache.get_tags(photos)
check(len(cached) >= len(photos) - 1,
      f"photo_tags 表持久化读回 {len(cached)}/{len(photos)}")

# 二次运行（force=False）应全部缓存命中跳过
summary2 = photo_tagger.run_tagging(photos, batch_size=4)
check(summary2.get("skipped", 0) == len(photos),
      f"二次运行全缓存跳过（skipped={summary2.get('skipped')}）")

# force=True 应重打（再花一次 API，验证强制路径）
summary3 = photo_tagger.run_tagging(photos[:2], batch_size=2, force=True)
check(summary3.get("tagged", 0) >= 1,
      f"force=True 强制重打（tagged={summary3.get('tagged')}）")

print(f"\n{'='*50}")
if failures:
    print(f"结果: {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("结果: 全部通过 ✔（真实多模态 LLM 端到端）")
