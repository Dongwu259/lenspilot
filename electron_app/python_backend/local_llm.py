"""local_llm.py — 本地 llama-server (llama.cpp) 子进程管理。

方案 A:内置 llama-server 子进程 + OpenAI 兼容端点,运行 Qwen3-VL-2B-Instruct
(GGUF + mmproj) 本地推理,替代官方 API Key / 个人云端 Key。
跨平台:Windows 用 llama-server.exe, macOS/Linux 用 llama-server(Metal/CPU)。

设计要点:
- 懒启动:首次调用 ensure_running() 才拉起子进程,不拖慢 App 启动;
  App 退出/进程崩溃时自动清理。
- 端口:随机空闲端口,仅监听 127.0.0.1(不外网暴露)。
- 模型:GGUF + mmproj 放用户数据目录(llm/),首次使用自动下载(ModelScope
  国内直连);llama-server 随安装包内置(build-resources/llm/)。
- Token:本地推理无成本,不限制生成 token;上下文裁剪阈值取本地模型
  context(256K)的一个大值。
- 思考型模型:Qwen3-VL-2B-Instruct 为非思考型(无 reasoning_content),
  Instruct 版直接输出;若换 Thinking 版再评估思考流处理。
- 下载断点续传:.part 放目标目录(同卷保证 os.replace 原子性),中断后
  续传;旧 MiniCPM 模型文件在新模型就绪后清理。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ── 本地模型常量 ──
LOCAL_MODEL_NAME = "Qwen3-VL-2B-Instruct"  # 上报给 App 的模型名
LOCAL_API_KEY = "local-llm"             # llama-server 不校验 key,占位即可
LOCAL_CTX = 24576                       # 本地推理上下文(小于模型 256K,留余量)
LOCAL_MAX_TOKENS = 1_000_000            # 本地生成不设上限(极大值)
LOCAL_CONTEXT_LIMIT = LOCAL_CTX         # 裁剪阈值对齐服务端 -c(24576):
                                        # 此前写 240K 导致裁剪永不触发,请求超
                                        # 服务端上下文直接 400(2026-09-12 实测)

# GGUF 文件(Qwen3-VL 官方 GGUF 仓库,ModelScope 国内直连;HF 同名仓库可互换)
GGUF_URL = ("https://modelscope.cn/models/Qwen/Qwen3-VL-2B-Instruct-GGUF/"
            "resolve/master/Qwen3VL-2B-Instruct-Q4_K_M.gguf")
MMPROJ_URL = ("https://modelscope.cn/models/Qwen/Qwen3-VL-2B-Instruct-GGUF/"
              "resolve/master/mmproj-Qwen3VL-2B-Instruct-F16.gguf")
GGUF_FILE = "Qwen3VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ_FILE = "mmproj-Qwen3VL-2B-Instruct-F16.gguf"

# 旧模型(0.9.0 前 MiniCPM-V 4.6)文件名——新模型就绪后清理,释放磁盘
_LEGACY_MODEL_FILES = ("MiniCPM-V-4_6-Q4_K_M.gguf", "mmproj-model-f16.gguf")

# llama-server 内置位置(CPU / CUDA / Metal 版)。依次尝试:
#   1) 打包资源 llm/ (PyInstaller --add-data "…;llm" / "…:llm")
#   2) 打包后 service 目录(service.exe / service 同目录)
#   3) 开发目录 build-resources/llm/
def _bundle_root():
    """PyInstaller 打包资源根(_MEIPASS), 否则脚本目录。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _server_bin_name():
    """llama-server 二进制名: Windows 带 .exe, macOS/Linux 无扩展名。"""
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def _exe_candidates():
    _here = os.path.dirname(os.path.abspath(__file__))
    bin_name = _server_bin_name()
    cands = [
        os.path.join(_bundle_root(), "llm", bin_name),
        os.path.join(os.path.dirname(sys.executable), bin_name),
        os.path.join(_here, bin_name),
        os.path.abspath(os.path.join(_here, "..", "build-resources", "llm", bin_name)),
    ]
    return cands


class LocalLLM:
    """管理一个本地 llama-server 子进程。单例使用。"""

    def __init__(self):
        self._proc = None
        self._port = None
        self._lock = threading.Lock()
        self._base_url = None
        self._started_at = 0.0

    # ── 路径 ──
    def _user_data_root(self):
        try:
            from utils import get_user_data_dir
            return get_user_data_dir()
        except Exception:
            if sys.platform == "win32":
                return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LensPilot")
            return os.path.expanduser("~/.lenspilot")

    def _llm_dir(self):
        d = os.path.join(self._user_data_root(), "llm")
        os.makedirs(d, exist_ok=True)
        return d

    def model_path(self):
        return os.path.join(self._llm_dir(), GGUF_FILE)

    def mmproj_path(self):
        return os.path.join(self._llm_dir(), MMPROJ_FILE)

    def _server_exe(self):
        # 开发/测试环境可用 APS_LLAMA_SERVER 覆盖(与 model_loader 的 APS_USER_DATA 一致)
        env = os.environ.get("APS_LLAMA_SERVER", "").strip()
        if env and os.path.isfile(env):
            return env
        for p in _exe_candidates():
            if os.path.isfile(p):
                return p
        return None

    # ── 状态 ──
    def is_model_present(self):
        return os.path.isfile(self.model_path()) and os.path.isfile(self.mmproj_path())

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def endpoint(self):
        """返回 (base_url, api_key, model)；未就绪时 base_url=None。"""
        if not self.is_running():
            return None, None, None
        return f"http://127.0.0.1:{self._port}/v1", LOCAL_API_KEY, LOCAL_MODEL_NAME

    def get_config(self):
        """供前端显示本地模型状态。"""
        exe = self._server_exe()
        if not exe:
            return {"use_local": False, "reason": "llama-server 未内置，请升级到含本地模型的版本"}
        if not self.is_model_present():
            return {"use_local": True, "model_present": False,
                    "reason": "本地模型未下载，首次使用将自动下载 (~1.9GB)"}
        return {"use_local": True, "model_present": True,
                "model": LOCAL_MODEL_NAME, "running": self.is_running(),
                "base_url": f"http://127.0.0.1:{self._port}/v1" if self.is_running() else None}

    # ── 下载模型(首次) ──
    def download_model_files(self, progress_cb=None):
        """下载 GGUF + mmproj 到用户数据目录(断点续传)。

        progress_cb(current_bytes, total_bytes, name)。.part 放目标目录
        (与 dest 同卷,os.replace 原子改名);服务端不支持 Range 时自动
        重下。就绪后清理旧 MiniCPM 文件。
        """
        out = {}
        for url, fname in ((GGUF_URL, GGUF_FILE), (MMPROJ_URL, MMPROJ_FILE)):
            dest = os.path.join(self._llm_dir(), fname)
            if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
                out[fname] = dest
                continue
            tmp = dest + ".part"
            have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
            req = urllib.request.Request(url, headers={"User-Agent": "LensPilot"})
            if have > 0:
                req.add_header("Range", f"bytes={have}-")
            try:
                resp = urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have > 0:  # Range 超界:文件可能已下完,重下保险
                    os.remove(tmp)
                    resp = urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": "LensPilot"}),
                        timeout=120)
                else:
                    raise
            with resp:
                # 206=续传成功(追加);200=服务端不支持 Range(重头写)
                resume_ok = (have > 0
                             and getattr(resp, "status", resp.getcode()) == 206)
                mode = "ab" if resume_ok else "wb"
                base = have if resume_ok else 0
                total = base + int(resp.headers.get("Content-Length") or 0)
                with open(tmp, mode) as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        if progress_cb:
                            progress_cb(f.tell(), total, fname)
            os.replace(tmp, dest)
            out[fname] = dest
        self._cleanup_legacy_models()
        return out

    def _cleanup_legacy_models(self):
        """删除旧版 MiniCPM 模型文件(新模型全部就绪后才调用)。"""
        if not self.is_model_present():
            return
        for name in _LEGACY_MODEL_FILES:
            p = os.path.join(self._llm_dir(), name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass

    # ── 启动 / 停止 ──
    def _free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def ensure_running(self, wait_s=180):
        """拉起 llama-server(若未运行)并等待 /v1/models 就绪。返回 (base_url, err)。"""
        with self._lock:
            if self.is_running():
                return f"http://127.0.0.1:{self._port}/v1", None
            exe = self._server_exe()
            if not exe:
                return None, "本地推理引擎(llama-server)未内置"
            if not self.is_model_present():
                return None, "本地模型未下载（GGUF/mmproj 缺失）"

            self._port = self._free_port()
            cmd = [exe, "-m", self.model_path(), "--mmproj", self.mmproj_path(),
                   "--port", str(self._port), "-c", str(LOCAL_CTX),
                   "--host", "127.0.0.1", "--jinja"]
            # GPU 可用时尽量卸载到显卡(由 CUDA 版 llama-server 自动检测)
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self._proc = None
                return None, f"启动 llama-server 失败: {e}"

            base = f"http://127.0.0.1:{self._port}/v1"
            # 回环健康检查不走系统/环境代理（终端代理常无 NO_PROXY=localhost，
            # 会把 127.0.0.1 路由进代理导致就绪探测永远失败）
            _local_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    self._proc = None
                    return None, "llama-server 提前退出（模型文件可能损坏或引擎不兼容）"
                try:
                    req = urllib.request.Request(f"{base}/models", method="GET")
                    with _local_opener.open(req, timeout=3) as r:
                        if r.status == 200:
                            self._started_at = time.time()
                            return base, None
                except Exception:
                    pass
                time.sleep(2)
            self.stop()
            return None, "等待本地模型就绪超时"

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=10)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None


# 单例
local_llm = LocalLLM()
