#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
E2E dedup test: creates temp folder with known duplicates, runs full dedup,
verifies files are correctly moved. No impact on real data.
用法: python smoke_test/verify_dedup_fix.py
"""
import os, sys, time, shutil, tempfile, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "electron_app", "python_backend"))

# Ensure pillow_heif registered
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

import photo_scorer

print("=" * 60)
print("去重 E2E 验证: 模拟连拍去重")
print("=" * 60)

# ── 1. Load model ──
print("\n[1] 加载模型...")
if not photo_scorer._model_loaded:
    r = photo_scorer.handle_load_model({})
    print(f"  模型: {r.get('model_name','?')} on {r.get('device','?')}")

# ── 2. Create test folder with known duplicates ──
print("\n[2] 创建测试文件夹(含复制的连拍组)...")
tmpdir = tempfile.mkdtemp(prefix="dedup_test_")
print(f"  测试目录: {tmpdir}")

# Find a real image to use as source
real_folder = r"D:\photoDatebase\4-2春游"
src_heic = None
src_arw = None
for f in sorted(os.listdir(real_folder)):
    full = os.path.join(real_folder, f)
    if not os.path.isfile(full):
        continue
    ext = os.path.splitext(f)[1].lower()
    if ext == '.heic' and not src_heic:
        src_heic = full
    if ext == '.arw' and not src_arw:
        src_arw = full
    if src_heic and src_arw:
        break

if not src_heic or not src_arw:
    print("  FAIL: 找不到源文件"); sys.exit(1)

# Create 3 "burst groups" by copying files with similar timestamps
# Group 1: 3 HEIC copies (simulate 3-shot burst)
heic_names = []
for i in range(3):
    dst = os.path.join(tmpdir, f"burst1_{i:02d}.heic")
    shutil.copy2(src_heic, dst)
    heic_names.append(dst)
    # Set mtime close together
    t = time.time() + i * 2  # 2s apart
    os.utime(dst, (t, t))

# Group 2: 2 ARW copies (simulate 2-shot burst)
arw_names = []
for i in range(2):
    dst = os.path.join(tmpdir, f"burst2_{i:02d}.arw")
    shutil.copy2(src_arw, dst)
    arw_names.append(dst)
    t = time.time() + 10 + i * 1  # 1s apart, start 10s after group 1
    os.utime(dst, (t, t))

# Single photo (no burst)
single = os.path.join(tmpdir, "single_standalone.heic")
shutil.copy2(src_heic, single)
os.utime(single, (time.time() + 30, time.time() + 30))

print(f"  创建: {len(heic_names)} HEIC (group1) + {len(arw_names)} ARW (group2) + 1 standalone = {len(os.listdir(tmpdir))} 张")
total_files = len(os.listdir(tmpdir))

# ── 3. Score files ──
print("\n[3] 评分...")
import score_cache
score_cache.clear()
r = photo_scorer.handle_score_photos({"folder": tmpdir, "threshold": 6.0, "copy_on_score": False})
print(f"  评分: {r['total']} 张")

# ── 4. Dry-run dedup ──
print("\n[4] 去重 dry_run...")
r = photo_scorer.handle_dedup_burst({
    "folder": tmpdir, "dry_run": True,
    "time_window": 5, "similarity": 5,
})
groups = r.get("groups", 0)
duplicates = r.get("duplicates", 0)
group_details = r.get("group_details", [])
print(f"  连拍组: {groups}, 可去重: {duplicates}")
for g in group_details:
    print(f"    keep={g.get('kept','?')}  del {len(g.get('moved',[]))}张: {g.get('moved',[])}")

# Verify: should find 2 groups (3 HEIC + 2 ARW)
assert groups >= 2, f"FAIL: expected >=2 groups, got {groups}"
assert duplicates == 3, f"FAIL: expected 3 duplicates (2 from group1 + 1 from group2), got {duplicates}"
print("  [PASS] dry_run correctly identified duplicates")

# ── 5. Execute dedup ──
print("\n[5] 去重执行(实际移动)...")
r = photo_scorer.handle_dedup_burst({
    "folder": tmpdir, "dry_run": False,
    "time_window": 5, "similarity": 5,
})
moved = r.get("duplicates", 0)
print(f"  已移动: {moved} 张")

# Verify: remaining files + moved files = total
dup_dir = os.path.join(tmpdir, "_duplicates")
remaining = len([f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))])
in_dup = len(os.listdir(dup_dir)) if os.path.isdir(dup_dir) else 0
print(f"  剩余: {remaining} 张, _duplicates/ 中: {in_dup} 张")
assert moved == 3, f"FAIL: expected 3 moved, got {moved}"
assert remaining == 3, f"FAIL: expected 3 remaining (1 HEIC keeper + 1 ARW keeper + 1 standalone), got {remaining}"
assert in_dup == 3, f"FAIL: expected 3 in _duplicates, got {in_dup}"
print("  [PASS] 文件正确移动")

# ── 6. Verify dedup is idempotent (running again finds nothing) ──
print("\n[6] 幂等性验证(再次去重)...")
r3 = photo_scorer.handle_dedup_burst({
    "folder": tmpdir, "dry_run": False,
    "time_window": 5, "similarity": 5,
})
assert r3.get("duplicates", 0) == 0, f"FAIL: re-run should find 0 new duplicates, got {r3['duplicates']}"
print("  [PASS] 去重幂等(二次运行无新增)")

# ── 7. Cleanup ──
print("\n[7] 清理...")
shutil.rmtree(tmpdir, ignore_errors=True)
print(f"  已删除: {tmpdir}")

print("\n" + "=" * 60)
print("[PASS] 去重 E2E 验证全部通过!")
print("=" * 60)
