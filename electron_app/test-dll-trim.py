#!/usr/bin/env python
"""
test-dll-trim.py — Systematically test which CUDA DLLs can be removed safely.
Moves candidate DLLs to a backup dir, runs a ResNet-18 GPU inference test
in a subprocess (so DLLs aren't locked), then restores.

Run this script DIRECTLY (not imported) to avoid DLL locking.
"""
import subprocess
import sys
import os
import shutil
import time

TORCH_LIB = os.path.expandvars(
    r"%USERPROFILE%\AppData\Local\Programs\Python\Python313\Lib\site-packages\torch\lib"
)
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dll_backup_tmp")

# Small scoring test that exercises the actual CNN inference path
SCORING_TEST = r"""
import torch
import torchvision.models as models
import sys

print("Loading ResNet-18...")
device = torch.device('cuda')
model = models.resnet18(weights=None).to(device)
model.eval()
print(f"Model on: {device}")

# Test multiple forward passes (exercises cuDNN kernel selection)
for i in range(3):
    x = torch.randn(1, 3, 224, 224, device=device)
    with torch.no_grad():
        y = model(x)
    print(f"Pass {i+1}: output shape={y.shape}")

# Test batch inference (our actual use case)
x_batch = torch.randn(4, 3, 224, 224, device=device)
with torch.no_grad():
    y_batch = model(x_batch)
print(f"Batch: output shape={y_batch.shape}")

# Test with actual image-like input
from PIL import Image
import torchvision.transforms as T
import numpy as np

img = Image.fromarray(np.random.randint(0, 255, (300, 400, 3), dtype=np.uint8))
transform = T.Compose([
    T.Resize((224, 224)),
    T.ToTensor(),
    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])
tensor = transform(img).unsqueeze(0).to(device)
with torch.no_grad():
    result = model(tensor)
print(f"Image test: output shape={result.shape}, top class={result.argmax().item()}")

print("ALL_TESTS_PASSED")
"""


def test_scoring():
    """Run the scoring test in a subprocess. Returns True if all tests pass."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", SCORING_TEST],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "CUDA_MODULE_LOADING": "LAZY"}
        )
        passed = "ALL_TESTS_PASSED" in result.stdout
        if not passed:
            print(f"    STDOUT (last 500 chars): {result.stdout[-500:]}")
            print(f"    STDERR (last 500 chars): {result.stderr[-500:]}")
        return passed
    except subprocess.TimeoutExpired:
        print("    TIMEOUT after 60s")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def move_to_backup(dll_names):
    """Move DLLs from torch/lib to backup dir."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for name in dll_names:
        src = os.path.join(TORCH_LIB, name)
        dst = os.path.join(BACKUP_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)
            # print(f"  Moved: {name}")


def restore_from_backup(dll_names):
    """Move DLLs from backup back to torch/lib."""
    for name in dll_names:
        src = os.path.join(BACKUP_DIR, name)
        dst = os.path.join(TORCH_LIB, name)
        if os.path.exists(src):
            shutil.move(src, dst)
            # print(f"  Restored: {name}")


def get_size_mb(dll_names):
    """Get total size in MB of DLL files."""
    total = 0
    for name in dll_names:
        path = os.path.join(TORCH_LIB, name)
        if os.path.exists(path):
            total += os.path.getsize(path)
        else:
            # Check backup
            bpath = os.path.join(BACKUP_DIR, name)
            if os.path.exists(bpath):
                total += os.path.getsize(bpath)
    return total / (1024 * 1024)


def main():
    if not os.path.isdir(TORCH_LIB):
        print(f"ERROR: torch/lib not found at {TORCH_LIB}")
        return 1

    # Ensure clean backup dir
    if os.path.isdir(BACKUP_DIR):
        # Restore any leftover DLLs
        for f in os.listdir(BACKUP_DIR):
            src = os.path.join(BACKUP_DIR, f)
            dst = os.path.join(TORCH_LIB, f)
            if not os.path.exists(dst):
                shutil.move(src, dst)
        shutil.rmtree(BACKUP_DIR)

    # Define test groups
    groups = {
        # Only test the profiler sub-group to find which is needed
        "1e-nvperf_host only": [
            "nvperf_host.dll",         # 21 MB — profiling
        ],
        "1e-cupti only": [
            "cupti64_2025.1.1.dll",    # 4 MB — profiling tracer
        ],
        "1e-nvToolsExt only": [
            "nvToolsExt64_1.dll",      # 0.05 MB — tools extension
        ],
    }

    all_tested = []
    cumulative_savings = 0

    print("=" * 70)
    print("CUDA DLL Trim Test Harness")
    print(f"PyTorch lib: {TORCH_LIB}")
    print("=" * 70)

    # ── Baseline test ──
    print("\n[0] Testing BASELINE (all DLLs present)...")
    if test_scoring():
        print("    BASELINE: [PASS]")
    else:
        print("    BASELINE: [FAIL] -- cannot continue, GPU may not be available")
        return 1

    # ── Test each group cumulatively ──
    for group_name, dlls in groups.items():
        size_mb = get_size_mb(dlls)
        print(f"\n[{len(all_tested)+1}] Removing {group_name} ({size_mb:.0f} MB, {len(dlls)} files)...")

        # Remove DLLs
        move_to_backup(dlls)
        time.sleep(0.5)  # Let filesystem settle

        # Test
        if test_scoring():
            print(f"    {group_name}: [SAFE] (saved {size_mb:.0f} MB)")
            cumulative_savings += size_mb
            all_tested.append((group_name, True, size_mb))
        else:
            print(f"    {group_name}: [NEEDED] -- restoring...")
            restore_from_backup(dlls)
            all_tested.append((group_name, False, size_mb))
            # Only break on expected failure groups
            if "EXPECT FAIL" in group_name:
                print("    (Expected failure -- confirming precompiled engines are required)")
            else:
                print(f"    WARNING: Unexpected failure! These DLLs must be kept.")

    # ── Summary ──
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    total_safe = 0
    total_unsafe = 0
    for name, safe, size in all_tested:
        status = "[SAFE]" if safe else "[NEED]"
        print(f"  {status}  {name}: {size:.0f} MB")
        if safe:
            total_safe += size
        else:
            total_unsafe += size

    print(f"\n  Safe to remove: {total_safe:.0f} MB")
    print(f"  Must keep:      {total_unsafe:.0f} MB")
    print(f"  Net savings:    {total_safe:.0f} MB")

    lib_total = sum(
        os.path.getsize(os.path.join(TORCH_LIB, f))
        for f in os.listdir(TORCH_LIB) if os.path.isfile(os.path.join(TORCH_LIB, f))
    ) / (1024 * 1024)
    print(f"\n  Original lib:   {lib_total:.0f} MB")
    print(f"  After trim:     {lib_total - total_safe:.0f} MB")

    # Cleanup: restore all from backup
    remaining = os.listdir(BACKUP_DIR) if os.path.isdir(BACKUP_DIR) else []
    if remaining:
        print(f"\n  Restoring {len(remaining)} remaining DLLs...")
        restore_from_backup(remaining)

    if os.path.isdir(BACKUP_DIR):
        shutil.rmtree(BACKUP_DIR)

    print("\nDone. All DLLs restored to original location.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
