"""test_local_llm.py — 本地 LLM (Qwen3-VL) 冒烟测试

快速档(默认,无网络):
  1. 常量/URL 指向 ModelScope Qwen3-VL 官方 GGUF;
  2. 断点续传:本地 Range HTTP 服务模拟中断→续传→完整文件;
  3. 旧 MiniCPM 文件清理逻辑。

--live 档(真机端到端,~1.9GB 下载):
  真实下载 GGUF+mmproj → ensure_running 拉起内置 llama-server →
  /v1/models + 一轮带工具定义的 chat completion(验证 Qwen3-VL 模板与
  tool-calls 协议和 agent 解析器兼容)。

用法:
  python smoke_test/test_local_llm.py           # 快速档
  python smoke_test/test_local_llm.py --live    # 真实下载+推理
"""
import io
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "electron_app", "python_backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from local_llm import (GGUF_FILE, GGUF_URL, MMPROJ_FILE, MMPROJ_URL,
                       LOCAL_MODEL_NAME, _LEGACY_MODEL_FILES, local_llm)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✓" if cond else "✗") + " " + name + (f" — {detail}" if detail and not cond else ""))


# ── 1. 常量与 URL ─────────────────────────────────────────────────────────
check("模型名上报为 Qwen3-VL", LOCAL_MODEL_NAME == "Qwen3-VL-2B-Instruct",
      LOCAL_MODEL_NAME)
check("GGUF 指向 ModelScope", GGUF_URL.startswith("https://modelscope.cn/models/Qwen/Qwen3-VL-2B-Instruct-GGUF/"), GGUF_URL)
check("mmproj 指向 ModelScope", MMPROJ_URL.startswith("https://modelscope.cn/models/Qwen/Qwen3-VL-2B-Instruct-GGUF/"), MMPROJ_URL)
check("GGUF 文件名 Q4_K_M", GGUF_FILE == "Qwen3VL-2B-Instruct-Q4_K_M.gguf", GGUF_FILE)
check("mmproj 文件名 F16", MMPROJ_FILE == "mmproj-Qwen3VL-2B-Instruct-F16.gguf", MMPROJ_FILE)
check("旧 MiniCPM 文件已登记清理", _LEGACY_MODEL_FILES == ("MiniCPM-V-4_6-Q4_K_M.gguf", "mmproj-model-f16.gguf"))

# ── 2. 断点续传(本地 Range HTTP 服务) ────────────────────────────────────
class _RangeHandler:
    """最小 Range 支持:GET → 200/206 + 正确的 Content-Range。"""

    def __init__(self, data):
        self.data = data

    def __call__(self, handler):
        rng = handler.headers.get("Range")
        start = 0
        status = 200
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
            status = 206
        body = self.data[start:]
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        if status == 206:
            handler.send_header("Content-Range",
                                f"bytes {start}-{len(self.data)-1}/{len(self.data)}")
        handler.end_headers()
        handler.wfile.write(body)


def _serve(data):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    send = _RangeHandler(data)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            send(self)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/model.bin"


def test_resume():
    data = os.urandom(3 << 20)  # 3MB 伪模型
    srv, url = _serve(data)
    llm = local_llm.__class__()
    d = os.path.join(os.environ.get("TEMP", "."), "lenspilot_llm_resume_test")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, "model.bin")
    part = dest + ".part"
    for p in (dest, part):
        if os.path.isfile(p):
            os.remove(p)
    try:
        # 第一次:模拟中断——手工截取前 1MB 存 .part
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            with open(part, "wb") as f:
                f.write(r.read(1 << 20))
        # 用 download_model_files 的续传路径补完:临时替换模块常量,
        # 只 patch 独立实例的 _llm_dir(绝不碰单例,防污染后续 live 测试)
        import local_llm as m
        old = (m.GGUF_URL, m.GGUF_FILE, m.MMPROJ_URL, m.MMPROJ_FILE)
        m.GGUF_URL, m.GGUF_FILE = url, "model.bin"
        m.MMPROJ_URL, m.MMPROJ_FILE = url, "model2.bin"
        llm = m.local_llm.__class__()
        llm._llm_dir = lambda: d
        try:
            got = llm.download_model_files()
        finally:
            m.GGUF_URL, m.GGUF_FILE, m.MMPROJ_URL, m.MMPROJ_FILE = old
        ok = (open(dest, "rb").read() == data
              and os.path.getsize(os.path.join(d, "model2.bin")) == len(data))
        check("断点续传 1MB+2MB 拼回完整文件", ok)
    finally:
        srv.shutdown()
        for p in (dest, part, os.path.join(d, "model2.bin"),
                  os.path.join(d, "model2.bin.part")):
            if os.path.isfile(p):
                os.remove(p)
        try:
            os.rmdir(d)
        except OSError:
            pass


def test_legacy_cleanup():
    d = os.path.join(os.environ.get("TEMP", "."), "lenspilot_llm_legacy_test")
    os.makedirs(d, exist_ok=True)
    import local_llm as m
    old = (m.GGUF_FILE, m.MMPROJ_FILE)
    m.GGUF_FILE, m.MMPROJ_FILE = "Qwen3VL-2B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-2B-Instruct-F16.gguf"
    # 伪造新模型就绪 + 旧 MiniCPM 残留
    for name in (m.GGUF_FILE, m.MMPROJ_FILE, _LEGACY_MODEL_FILES[0], _LEGACY_MODEL_FILES[1]):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            with open(p, "wb") as f:
                f.write(b"x" * 2_000_000)
    llm = m.local_llm.__class__()
    llm._llm_dir = lambda: d
    llm._cleanup_legacy_models()
    gone = all(not os.path.isfile(os.path.join(d, n)) for n in _LEGACY_MODEL_FILES)
    kept = all(os.path.isfile(os.path.join(d, n)) for n in old)
    check("旧 MiniCPM 文件被清理、新模型保留", gone and kept)
    for name in os.listdir(d):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass


# ── 3. --live 真机端到端 ─────────────────────────────────────────────────
def test_live():
    print("\n── live:真实下载 + llama-server 推理 ──")
    got = local_llm.download_model_files(
        progress_cb=lambda cur, total, name: print(
            f"\r  {name}: {cur/1e6:.0f}/{total/1e6:.0f}MB", end="", flush=True))
    print()
    for k, v in got.items():
        check(f"已下载 {k}", os.path.isfile(v) and os.path.getsize(v) > 1_000_000)
    base, err = local_llm.ensure_running()
    check("llama-server 启动", base is not None, err or "")
    if not base:
        return
    import json
    import urllib.request
    # /v1/models
    with urllib.request.urlopen(base + "/models", timeout=10) as r:
        models = json.loads(r.read().decode("utf-8"))
    mid = models["data"][0]["id"]
    check("/v1/models 可见", bool(mid), str(models)[:200])
    # 带工具定义的一轮对话(验证 Qwen3-VL chat 模板 + tool-calls 输出协议)
    body = json.dumps({
        "model": mid,
        "messages": [{"role": "user", "content": "列出 D 盘根目录的文件"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "列出指定目录内容",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"}}, "required": ["path"]},
            },
        }],
        "max_tokens": 512,
    }).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    import time as _t
    t0 = _t.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read().decode("utf-8"))
    dt = _t.time() - t0
    usage = out.get("usage") or {}
    ct = usage.get("completion_tokens") or 0
    if ct and dt > 0:
        print(f"  [实测] completion {ct} tok / {dt:.1f}s = {ct/dt:.1f} tok/s")
    msg = out["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    ok = bool(calls) and calls[0]["function"]["name"] == "list_dir"
    check("tool_calls 协议输出", ok, json.dumps(msg, ensure_ascii=False)[:300])
    local_llm.stop()


if __name__ == "__main__":
    test_resume()
    test_legacy_cleanup()
    if "--live" in sys.argv:
        test_live()
    print(f"\n通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", *FAIL, sep="\n  ")
        sys.exit(1)
