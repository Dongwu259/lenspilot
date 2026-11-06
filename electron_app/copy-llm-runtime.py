#!/usr/bin/env python
"""
copy-llm-runtime.py — 把本地推理引擎 llama-server 暂存到 build-resources/llm/，
供 PyInstaller 的 --add-data 一并打进安装包（打包后位于 service/llm/）。

用法：
  python copy-llm-runtime.py --cpu                # Windows CPU 版（~17MB）
  python copy-llm-runtime.py --cuda               # Windows CUDA 版（含 CUDA runtime DLL，~1GB）
  python copy-llm-runtime.py --mac --src <path>   # macOS 版（Metal/CPU，二进制名 llama-server）

来源目录需含 llama-server（Windows 为 llama-server.exe；CUDA 版还需其运行时
DLL，如 cudart64_12.dll 等，直接复制整个目录即可）。默认来源：
  CPU  : D:/llamaAI/llama.cpp/cpu
  CUDA : D:/llamaAI/llama.cpp/cuda
  MAC  : 无默认（构建机而异）——用 --src 指定，或设环境变量 LLAMA_MAC_DIR。
         获取: brew install llama.cpp 后 --src "$(brew --prefix llama.cpp)/bin"
"""
import argparse
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEST = os.path.join(_HERE, "build-resources", "llm")

_DEFAULT_SRC = {
    "cpu": "D:/llamaAI/llama.cpp/cpu",
    "cuda": "D:/llamaAI/llama.cpp/cuda",
    "mac": "",  # 无默认目录，见 LLAMA_MAC_DIR / --src
}


def _bin_name(kind):
    """Windows 二进制带 .exe，macOS/Linux 无扩展名。"""
    return "llama-server.exe" if kind in ("cpu", "cuda") else "llama-server"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", action="store_true", help="Windows CPU 版运行时")
    ap.add_argument("--cuda", action="store_true", help="Windows CUDA 版运行时")
    ap.add_argument("--mac", action="store_true", help="macOS 版运行时(Metal/CPU)")
    ap.add_argument("--src", default="", help="llama-server 来源目录(覆盖默认)")
    args = ap.parse_args()

    kind = "mac" if args.mac else ("cuda" if args.cuda else "cpu")
    src = args.src.strip()
    if not src:
        src = os.environ.get("LLAMA_MAC_DIR", "").strip() if kind == "mac" else _DEFAULT_SRC.get(kind, "")
    bin_name = _bin_name(kind)
    src_exe = os.path.join(src, bin_name)
    if not os.path.isfile(src_exe):
        print(f"ERROR: 找不到 {src_exe}，请先获取对应版本的 llama.cpp", file=sys.stderr)
        if kind == "mac":
            print("  macOS 版获取: brew install llama.cpp 后\n"
                  "    python copy-llm-runtime.py --mac --src \"$(brew --prefix llama.cpp)/bin\"\n"
                  "  或编译后指定 --src", file=sys.stderr)
        return 1

    os.makedirs(_DEST, exist_ok=True)
    # 清空旧运行时，避免残留 CUDA/CPU/Metal 混用
    for old in os.listdir(_DEST):
        p = os.path.join(_DEST, old)
        try:
            os.remove(p) if os.path.isfile(p) else shutil.rmtree(p)
        except Exception:
            pass
    # 复制整个目录（llama-server + 其依赖 DLL/.dylib/.metal 等）
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(_DEST, item)
        try:
            shutil.copy2(s, d) if os.path.isfile(s) else shutil.copytree(s, d)
        except Exception as e:
            print(f"  skip {item}: {e}")
    size = sum(os.path.getsize(os.path.join(_DEST, f)) for f in os.listdir(_DEST))
    print(f"已暂存 {kind} 版 llama-server 到 {_DEST} ({size/1024/1024:.0f}MB)")
    if sys.platform == "win32":
        print("构建脚本需包含: --add-data=\"{}\\llm;llm\"".format(_DEST.replace("\\llm", "")))
    else:
        print("构建脚本需包含: --add-data=\"%s/llm:llm\"" % _DEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
