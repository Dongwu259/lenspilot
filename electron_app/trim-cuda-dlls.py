#!/usr/bin/env python
"""
trim-cuda-dlls.py — Remove CUDA DLLs not needed for ResNet photo inference.
Run AFTER PyInstaller build for GPU version ONLY.

Tested with: PyTorch 2.11.0+cu128, CUDA 12.8, cuDNN 9.19.0
Each DLL listed below was individually tested by removing it and running
a ResNet-18 GPU inference test (forward pass, batch inference, image input).

ResNet-18 photo scoring needs:
  Core:         torch_cuda, torch_cpu, torch_python, torch, torch_global_deps
  C10:          c10, c10_cuda
  CUDA:         cudart64_12
  cuBLAS:       cublas64_12 (fp32 BLAS for matmul)
  cuDNN:        cudnn64_9, cudnn_ops64_9, cudnn_cnn64_9, cudnn_heuristic64_9,
                cudnn_graph64_9, cudnn_engines_runtime_compiled64_9
  JIT:          nvJitLink_120_0, nvrtc-builtins64_128
  Runtime:      libiomp5md, libiompstubs5md, uv, zlibwapi, shm
  Profiling:    cupti64_2025.1.1 (dependency of shm.dll!)

Removes (tested safe, not needed for ResNet-18 fp32 inference):
  cublasLt64_12.dll                          644 MB (fp16/BF16 mixed-precision BLAS)
  cudnn_engines_precompiled64_9.dll          459 MB (precompiled kernels — runtime engine suffices)
  cusparse64_12.dll                          362 MB (sparse matrices)
  cufft64_11.dll                             264 MB (FFT)
  cudnn_adv64_9.dll                          257 MB (RNN/attention ops)
  cusolver64_11.dll                          216 MB (dense linear solvers)
  cusolverMg64_11.dll                        150 MB (multi-GPU solver)
  nvrtc64_120_0.dll                           83 MB (runtime compilation)
  nvrtc64_120_0.alt.dll                       83 MB (runtime compilation alt)
  curand64_10.dll                             69 MB (random numbers)
  nvperf_host.dll                             21 MB (profiling host)
  cufftw64_11.dll                            0.2 MB (FFT wrapper)
  nvToolsExt64_1.dll                         0.05 MB (tools extension)
  caffe2_nvrtc.dll                           0.02 MB (Caffe2 NVRTC)
  ─────────────────────────────────────────────────
  TOTAL SAVED: ~2,608 MB → lib reduced from 4.0 GB to ~1.4 GB
"""
import os
import sys

BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build-resources", "python_backend", "service", "_internal")
LIB_DIR = os.path.join(BUILD_DIR, "torch", "lib")

# DLLs tested-safe to remove for ResNet-18 fp32 inference
# Each was individually verified with a GPU scoring test
REMOVE_PATTERNS = [
    # Mixed-precision BLAS — fp32 ResNet uses regular cublas64_12.dll only
    "cublasLt64_12.dll",
    # Precompiled cuDNN engines — runtime-compiled engine (kept) handles kernel compilation
    "cudnn_engines_precompiled64_9.dll",
    # Sparse matrices — ResNet uses dense tensors only
    "cusparse64_12.dll",
    # FFT — no Fourier transforms in photo scoring
    "cufft64_11.dll",
    "cufftw64_11.dll",
    # Advanced cuDNN ops (RNN, attention, etc.) — ResNet uses basic CNN ops only
    "cudnn_adv64_9.dll",
    # Linear system solvers — training only, not used for inference
    "cusolver64_11.dll",
    "cusolverMg64_11.dll",
    # Runtime compilation — models are pre-built, no JIT compile needed
    "nvrtc64_120_0.dll",
    "nvrtc64_120_0.alt.dll",
    "caffe2_nvrtc.dll",
    # Random number generation — inference with model.eval() is deterministic
    "curand64_10.dll",
    # Profiling — not used in production inference
    "nvperf_host.dll",
    # Tools extension — only needed by Nsight/Visual Profiler
    "nvToolsExt64_1.dll",
]

# NOTE: These DLLs MUST be kept (tested — removing them causes failures):
# - cupti64_2025.1.1.dll         — dependency of shm.dll (torch fails to import without it!)
# - cudnn_engines_runtime_compiled64_9.dll — needed when precompiled engines are removed
# - All torch_*, c10_*, cublas64_*, cudnn64_*, cudnn_ops*, cudnn_cnn*, cudnn_heuristic*, cudnn_graph*
# - nvJitLink_120_0.dll, nvrtc-builtins64_128.dll
# - libiomp*, uv.dll, zlibwapi.dll, shm.dll


def main():
    if not os.path.isdir(LIB_DIR):
        print(f"ERROR: {LIB_DIR} not found.")
        print("Run after PyInstaller builds the GPU python_backend.")
        return 1

    # Calculate total size before
    before = 0
    for f in os.listdir(LIB_DIR):
        path = os.path.join(LIB_DIR, f)
        if os.path.isfile(path):
            before += os.path.getsize(path)
    print(f"torch/lib/ before: {before / 1048576:.1f} MB")

    # Collect all files in LIB_DIR
    all_files = set()
    for f in os.listdir(LIB_DIR):
        path = os.path.join(LIB_DIR, f)
        if os.path.isfile(path):
            all_files.add(f)

    removed = 0
    removed_count = 0
    for dll in REMOVE_PATTERNS:
        path = os.path.join(LIB_DIR, dll)
        if os.path.isfile(path):
            size = os.path.getsize(path)
            os.remove(path)
            removed += size
            removed_count += 1
            print(f"  DEL: {dll} ({size / 1048576:.1f} MB)")
        else:
            print(f"  N/A: {dll} (not found)")

    # Calculate total size after
    after = 0
    for f in os.listdir(LIB_DIR):
        path = os.path.join(LIB_DIR, f)
        if os.path.isfile(path):
            after += os.path.getsize(path)

    print(f"\ntorch/lib/ after:  {after / 1048576:.1f} MB")
    print(f"Saved: {removed / 1048576:.1f} MB ({removed_count} DLLs removed)")
    print(f"Kept: {(len(all_files) - removed_count)} DLLs ({after / 1048576:.1f} MB)")
    print(f"\nEstimated GPU installer size: ~{after / 1048576 * 0.55:.0f} MB (with LZMA2 compression)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
