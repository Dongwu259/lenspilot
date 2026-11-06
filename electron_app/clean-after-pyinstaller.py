#!/usr/bin/env python
"""
clean-after-pyinstaller.py — Strip dev-only files from PyInstaller output.
Run AFTER PyInstaller build, BEFORE electron-builder packaging.

Removes:
  - C++ headers (torch/include/)       ~65 MB
  - Static libs (torch/lib/*.lib)      ~47 MB
  - CMake configs (torch/share/)       ~1 MB
  - CLI tools (torch/bin/)             ~3 MB
  - Package metadata (*.dist-info/)    ~3 MB
  - PyInstaller temp dir (tmp/)        ~50-200 MB
  - TOTAL SAVED: ~120-320 MB

For CPU builds, also strips CUDA DLLs and backend dirs.
"""
import os
import sys
import shutil
import glob

BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build-resources", "python_backend", "service", "_internal")
CPU_MODE = "--cpu" in sys.argv

def rm(path):
    """Remove file or directory, ignore errors."""
    try:
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
            print(f"  DEL: {path}")
        elif os.path.isdir(path):
            shutil.rmtree(path)
            print(f"  DEL: {path}")
    except Exception as e:
        print(f"  SKIP: {path} ({e})")

def rm_glob(pattern, base=BUILD_DIR):
    """Remove all files matching glob pattern under base."""
    for p in glob.glob(os.path.join(base, pattern), recursive=True):
        rm(p)

# Packages that call importlib.metadata.version() at import time — their
# *.dist-info MUST be kept or the frozen build fails with
# "No package metadata was found for <pkg>" (e.g. mcp). Keep these.
KEEP_METADATA = ("mcp", "openai", "open_clip_torch", "huggingface_hub", "safetensors")

def rm_dist_info(base=BUILD_DIR):
    """Remove top-level *.dist-info except packages that need runtime metadata."""
    for p in glob.glob(os.path.join(base, "*.dist-info")):
        name = os.path.basename(p).lower()
        if any(name.startswith(k + "-") for k in KEEP_METADATA):
            print(f"  KEEP (runtime metadata): {os.path.basename(p)}")
            continue
        rm(p)

def main():
    if not os.path.isdir(BUILD_DIR):
        print(f"ERROR: {BUILD_DIR} not found. Run after PyInstaller build.")
        return

    print(f"Cleaning PyInstaller output: {BUILD_DIR}")

    # ── 1. Always remove: dev-only files ──
    print("\n--- Removing dev files (both builds) ---")

    # C++ headers
    torch_include = os.path.join(BUILD_DIR, "torch", "include")
    rm(torch_include)

    # Static libs
    rm_glob("torch/lib/*.lib")

    # CMake configs
    rm_glob("torch/share/cmake")

    # CLI tools
    torch_bin = os.path.join(BUILD_DIR, "torch", "bin")
    rm(torch_bin)

    # Package metadata (keep mcp/openai — they read their own version at import)
    rm_dist_info()
    rm_glob("torch/**/*.dist-info")

    # PyInstaller temp (from build-resources/tmp/)
    tmp_dir = os.path.join(os.path.dirname(BUILD_DIR), "..", "..", "tmp")
    if os.path.isdir(tmp_dir):
        print(f"  DEL: {tmp_dir}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 2. CPU-specific: remove CUDA DLLs ──
    if CPU_MODE:
        print("\n--- Removing CUDA DLLs (CPU build) ---")
        cuda_dlls = [
            "torch_cuda", "cublas", "cudnn", "cusparse",
            "cufft", "cusolver", "nvrtc", "nvJitLink",
            "curand", "nvperf_host", "cupti", "caffe2_nvrtc",
            "nvToolsExt", "c10_cuda", "nvrtc-builtins",
        ]
        for pattern in cuda_dlls:
            rm_glob(f"torch/lib/{pattern}*.dll")

        # CUDA Python backends
        for backend in ["torch/cuda", "torch/backends/cuda", "torch/backends/cudnn"]:
            rm(os.path.join(BUILD_DIR, backend))

    # ── 3. Report savings ──
    print("\n--- Remaining files in torch/lib/ ---")
    lib_dir = os.path.join(BUILD_DIR, "torch", "lib")
    if os.path.isdir(lib_dir):
        total = sum(os.path.getsize(os.path.join(lib_dir, f))
                   for f in os.listdir(lib_dir) if os.path.isfile(os.path.join(lib_dir, f)))
        print(f"  torch/lib/ size: {total / 1048576:.1f} MB")

if __name__ == "__main__":
    main()
