#!/usr/bin/env python3
"""
test_photo_tagger.py — photo_tagger 最小自测（假 LLM client，无网络）

覆盖：
  1. _parse_tag_response：裸 JSON / code fence / 前后废话 / 坏输入
  2. tag_batch：多图编号映射、部分缺失
  3. run_tagging：批处理流程、批次失败降级逐张、逐张失败计 failed、
     取消、缓存跳过（force=False）与 force=True 重打
  4. score_cache tags 持久化：put/get、mtime 失效、clear 清理

用法: python smoke_test/test_photo_tagger.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "electron_app" / "python_backend"
sys.path.insert(0, str(BACKEND_DIR))

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
results = {"pass": 0, "fail": 0}


def ok(msg):
    results["pass"] += 1
    print(f"  {GREEN}PASS{RESET} {msg}")


def fail(msg, detail=""):
    results["fail"] += 1
    print(f"  {RED}FAIL{RESET} {msg}")
    if detail:
        print(f"       {RED}{detail}{RESET}")


def check(cond, msg, detail=""):
    ok(msg) if cond else fail(msg, detail)


# ── 假 OpenAI client ──

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeCompletions:
    """scripted: 每次 create() 弹出下一个响应（str 或 Exception）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # 记录每次调用的 messages

    def create(self, model=None, messages=None, **kw):
        self.calls.append({"model": model, "messages": messages, "kw": kw})
        if not self.script:
            raise RuntimeError("FakeCompletions: no more scripted responses")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)


class FakeClient:
    def __init__(self, script):
        import types
        self.chat = types.SimpleNamespace(completions=FakeCompletions(script))


# ── 临时 score_cache DB ──

import score_cache  # noqa: E402

_tmp_db = os.path.join(tempfile.mkdtemp(prefix="tagger_test_"), "scores.db")
# 换掉模块级 DB 路径并排空连接池（池里可能有指向旧 DB 的连接）
score_cache._db_path = _tmp_db
while True:
    try:
        score_cache._pool.get(block=False).close()
    except Exception:
        break
score_cache._pool_count = 0
score_cache._init()

import photo_tagger  # noqa: E402
from PIL import Image  # noqa: E402

# ── 测试图片 ──

IMG_DIR = Path(tempfile.mkdtemp(prefix="tagger_imgs_"))


def make_img(name, color):
    p = IMG_DIR / name
    Image.new("RGB", (64, 48), color).save(str(p), "JPEG")
    return str(p)


IMG_A = make_img("a.jpg", (200, 30, 30))
IMG_B = make_img("b.jpg", (30, 200, 30))
IMG_C = make_img("c.jpg", (30, 30, 200))
IMG_D = make_img("d.jpg", (200, 200, 30))


def batch_json(n, start=1):
    return json.dumps([{"i": i, "tags": [f"标签{i}", "人像"]} for i in range(start, n + 1)],
                      ensure_ascii=False)


print("== 1. _parse_tag_response ==")
r = photo_tagger._parse_tag_response('[{"i":1,"tags":["婚礼","人像"]},{"i":2,"tags":["风景"]}]')
check(r == {1: ["婚礼", "人像"], 2: ["风景"]}, "裸 JSON 数组解析")

r = photo_tagger._parse_tag_response('```json\n[{"i":1,"tags":["逆光"]}]\n```')
check(r == {1: ["逆光"]}, "code fence 剥离")

r = photo_tagger._parse_tag_response('{"i":1,"tags":["降级成功"]}')
check(r == {1: ["降级成功"]}, "单对象容错（非数组）")

r = photo_tagger._parse_tag_response('好的，结果如下：\n[{"i":"2","tags":["#特写 ","特写","静物"]}]\n希望对你有帮助')
check(r == {2: ["特写", "静物"]}, "前后废话 + 字符串编号 + #前缀/空白/去重")

r = photo_tagger._parse_tag_response('[{"i":1,"tags":[]},{"i":2,"tags":"不是数组"},{"i":3}]')
check(r is None, "空 tags/非法条目 → None（触发降级）")

r = photo_tagger._parse_tag_response('完全没有 JSON')
check(r is None, "无 JSON → None")

print("== 2. tag_batch 编号映射 ==")
client = FakeClient([batch_json(2)])
b64 = photo_tagger._encode_image(IMG_A)
check(isinstance(b64, str) and len(b64) > 100, "_encode_image 产出 base64")
out = photo_tagger.tag_batch(client, "m", [(IMG_A, b64), (IMG_B, b64)])
check(out.get(IMG_A) == ["标签1", "人像"] and out.get(IMG_B) == ["标签2", "人像"],
      "批次两张分别映射到 [1]/[2]")
# user message 里应有 2 个 image_url part
parts = client.chat.completions.calls[0]["messages"][1]["content"]
n_img = sum(1 for p in parts if p.get("type") == "image_url")
check(n_img == 2, "一次调用携带 2 个 image_url part")

print("== 3. run_tagging 流程 ==")
# 3a. 正常批处理：4 张一批
client = FakeClient([batch_json(4)])
prog = []
s = photo_tagger.run_tagging([IMG_A, IMG_B, IMG_C, IMG_D], client=client, model="m",
                             progress_cb=prog.append)
check(s["tagged"] == 4 and s["failed"] == 0 and not s.get("error"), "4 张一批全部成功",
      json.dumps(s, ensure_ascii=False)[:200])
check(prog and prog[-1]["current"] == 4 and prog[-1]["total"] == 4, "进度事件到 4/4")

# 3b. 缓存跳过：再来一轮，force=False 应全部 skipped 且不调 LLM
client2 = FakeClient([])
s = photo_tagger.run_tagging([IMG_A, IMG_B], client=client2, model="m")
check(s["skipped"] == 2 and s["tagged"] == 0 and len(client2.chat.completions.calls) == 0,
      "已有 tags → 跳过且零 LLM 调用")

# 3c. force=True 重打
client3 = FakeClient([batch_json(2)])
s = photo_tagger.run_tagging([IMG_A, IMG_B], force=True, client=client3, model="m")
check(s["tagged"] == 2 and s["skipped"] == 0, "force=True 强制重打")

# 3d. 批次调用异常 → 降级逐张；其中一张逐张也失败 → failed=1
client4 = FakeClient([
    RuntimeError("batch boom"),          # 批次调用炸
    '{"i":1,"tags":["降级成功"]}',          # 逐张 1 成功
    RuntimeError("single boom"),         # 逐张 2 也炸
])
s = photo_tagger.run_tagging([IMG_C, IMG_D], force=True, client=client4, model="m")
check(s["tagged"] == 1 and s["failed"] == 1, "批失败降级逐张：1 成功 1 失败",
      json.dumps(s, ensure_ascii=False)[:200])
check(s["results"].get(IMG_C) == ["降级成功"], "降级结果落进 results")

# 3e. 解析失败（返回垃圾）→ 同样降级逐张
client5 = FakeClient(["无法理解的要求", '{"i":1,"tags":["逐张A"]}', '{"i":1,"tags":["逐张B"]}'])
s = photo_tagger.run_tagging([IMG_A, IMG_B], force=True, client=client5, model="m")
check(s["tagged"] == 2 and s["results"].get(IMG_A) == ["逐张A"], "解析失败降级逐张")

# 3f. 取消：第一批后取消
calls = {"n": 0}


def cancel_after_first():
    calls["n"] += 1
    return calls["n"] > 1  # 第一次检查 False（放行第一批），之后 True


client6 = FakeClient([batch_json(2)])
s = photo_tagger.run_tagging([IMG_A, IMG_B, IMG_C, IMG_D], batch_size=2, force=True,
                             client=client6, model="m", should_cancel=cancel_after_first)
check(s["cancelled"] is True and s["tagged"] == 2, "取消：第一批完成后停止",
      json.dumps(s, ensure_ascii=False)[:200])

print("== 4. score_cache tags 持久化 ==")
tags = score_cache.get_tags([IMG_A, IMG_B, IMG_C, IMG_D])
check(IMG_A in tags and isinstance(tags[IMG_A], list), "get_tags 读回")
check(tags.get(IMG_C) == ["降级成功"], "降级写入的 tags 持久化正确")

# mtime 失效：改文件内容（换图重写）→ 缓存应过期
time.sleep(1.1)
Image.new("RGB", (32, 32), (1, 2, 3)).save(IMG_A, "JPEG")
tags2 = score_cache.get_tags([IMG_A])
check(IMG_A not in tags2, "mtime/size 变化后 tags 过期")

# 不存在的文件
check(score_cache.get_tags([str(IMG_DIR / "nope.jpg")]) == {}, "不存在文件 → 空")

# clear() 同时清 photo_tags（monkeypatch 用户数据目录，避免动真实 thumbs/heatmaps）
score_cache.get_user_data_dir = lambda: tempfile.mkdtemp(prefix="tagger_ud_")
score_cache.clear()
check(score_cache.get_tags([IMG_B, IMG_C]) == {}, "clear() 清空 photo_tags")

print()
total = results["pass"] + results["fail"]
print(f"{GREEN if results['fail'] == 0 else RED}结果: {results['pass']}/{total} 通过{RESET}")
sys.exit(1 if results["fail"] else 0)
