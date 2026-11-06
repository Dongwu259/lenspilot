#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数字员工 M1 — 全格式诊断脚本 (HEIC/RAW dedup + 缩略图)
用法: python smoke_test/diagnose_formats.py

测试:
  1. 按格式统计测试图片
  2. HEIC: pillow_heif 各API耗时 (read_heif / PIL.open / thumbnail / pHash)
  3. RAW: rawpy extract_thumb 耗时
  4. 缩略图生成 (make_thumbnail via _load_image_rgb) — 全格式
  5. pHash 计算 — 全格式 (用 256px 缩略图)
  6. 连拍去重模拟 (同文件夹内时间窗口分组)
  7. 逐文件日志 -> 精确定位挂死/慢文件
"""
import os, sys, time, json, threading, signal, io as _io_module

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "electron_app", "python_backend"))

FOLDER = r"D:\photoDatebase\4-2春游"
TOTAL_TIMEOUT = 300  # 整体脚本不超过5分钟


# ── timeout helper (Windows: thread; Unix: signal) ──
class TimeoutError(Exception):
    pass


def with_timeout(fn, seconds, label="?"):
    """Run fn with timeout. Returns (result, 'ok') or (None, 'timeout') or (None, error_str)."""
    if hasattr(signal, 'SIGALRM') and sys.platform != 'win32':
        result = [None]; err = [None]
        def _handler(s, f):
            raise TimeoutError(label)
        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            result[0] = fn()
            return (result[0], 'ok')
        except TimeoutError:
            return (None, f'timeout({seconds}s)')
        except Exception as e:
            return (None, f'{type(e).__name__}: {e}')
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        result = [None]; exc = [None]
        def _run():
            try:
                result[0] = fn()
            except Exception as e:
                exc[0] = e
        t = threading.Thread(target=_run, daemon=True)
        t.start(); t.join(seconds)
        if t.is_alive():
            return (None, f'HUNG(>{seconds}s)')
        if exc[0]:
            return (None, f'{type(exc[0]).__name__}: {exc[0]}')
        return (result[0], 'ok')


# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("LensPilot 全格式诊断: HEIC/RAW dedup + 缩略图")
print("=" * 60)

# ── 1. File census ──
print("\n[1/7] 按格式统计...")
exts = {}
all_files = []
for f in sorted(os.listdir(FOLDER)):
    full = os.path.join(FOLDER, f)
    if not os.path.isfile(full):
        continue  # skip .lenspilot and other directories
    ext = os.path.splitext(f)[1].lower()
    exts[ext] = exts.get(ext, 0) + 1
    all_files.append(full)
for ext, cnt in sorted(exts.items()):
    print(f"  {ext}: {cnt} 张")
print(f"  → 共 {len(all_files)} 张")

# ── 2. HEIC API 对比 ──
print("\n[2/7] HEIC API 性能对比...")

# ensure pillow_heif registered
import pillow_heif
pillow_heif.register_heif_opener()

heic_files = [f for f in all_files if os.path.splitext(f)[1].lower() in ('.heic', '.heif')]
if heic_files:
    sample = heic_files[0]
    fsize_mb = os.path.getsize(sample) / 1e6
    print(f"  样本: {os.path.basename(sample)} ({fsize_mb:.1f}MB)")

    # (a) read_heif info only
    t0 = time.time()
    r, st = with_timeout(lambda: pillow_heif.read_heif(sample), 15, "read_heif")
    dt_read = time.time() - t0
    if st == 'ok':
        print(f"  a) read_heif 信息: {r.size} {r.mode} — {dt_read:.2f}s")
    else:
        print(f"  a) read_heif 信息: {st} — {dt_read:.2f}s")

    # (b) PIL.open (lazy, no decode)
    from PIL import Image as PILImage
    t0 = time.time()
    r, st = with_timeout(lambda: PILImage.open(sample), 10, "PIL.open")
    dt_open = time.time() - t0
    if st == 'ok':
        print(f"  b) PIL.open(lazy): size={r.size} mode={r.mode} — {dt_open:.2f}s")
    else:
        print(f"  b) PIL.open(lazy): {st} — {dt_open:.2f}s")

    # (c) PIL.open + .convert('RGB') — full decode
    t0 = time.time()
    def _open_rgb():
        im = PILImage.open(sample)
        return im.convert('RGB').size
    r, st = with_timeout(_open_rgb, 20, "PIL.open+RGB")
    dt_rgb = time.time() - t0
    if st == 'ok':
        print(f"  c) PIL.open+RGB: {r} — {dt_rgb:.2f}s")
    else:
        print(f"  c) PIL.open+RGB: {st} — {dt_rgb:.2f}s")

    # (d) PIL.open + thumbnail(256) + pHash — actual dedup path
    t0 = time.time()
    def _thumb_phash():
        import imagehash
        im = PILImage.open(sample)
        im.thumbnail((256, 256), PILImage.LANCZOS)
        h = imagehash.phash(im.convert('L'))
        return str(h)
    r, st = with_timeout(_thumb_phash, 20, "thumb+phash")
    dt_phash = time.time() - t0
    if st == 'ok':
        print(f"  d) thumb(256)+pHash: {r} — {dt_phash:.2f}s")
    else:
        print(f"  d) thumb(256)+pHash: {st} — {dt_phash:.2f}s")

    # (e) _load_image_rgb (model_loader path, with caching)
    t0 = time.time()
    def _via_loader():
        from model_loader import _load_image_rgb
        return _load_image_rgb(sample).size
    r, st = with_timeout(_via_loader, 20, "_load_image_rgb")
    dt_loader = time.time() - t0
    if st == 'ok':
        print(f"  e) _load_image_rgb: {r} — {dt_loader:.2f}s")
    else:
        print(f"  e) _load_image_rgb: {st} — {dt_loader:.2f}s")
else:
    print("  无 HEIC 文件，跳过")

# ── 3. RAW rawpy 测试 ──
print("\n[3/7] RAW (rawpy extract_thumb) 测试...")
raw_files = [f for f in all_files if os.path.splitext(f)[1].lower() in (
    '.arw','.srf','.sr2','.arq','.cr2','.cr3','.crw','.nef','.nrw','.raf',
    '.rw2','.orf','.ori','.pef','.ptx','.raw','.rwl','.rwz','.dng','.srw',
    '.x3f','.gpr','.iiq','.cap','.eip','.3fr','.fff','.mef','.mos','.mrw',
    '.mdc','.erf','.dcr','.kdc','.k25','.dcs','.dc2','.bay','.cs1','.sti'
)]

if raw_files:
    try:
        import rawpy
        sample_raw = raw_files[0]
        fsize_mb = os.path.getsize(sample_raw) / 1e6
        print(f"  样本: {os.path.basename(sample_raw)} ({fsize_mb:.1f}MB)")

        # extract_thumb
        t0 = time.time()
        def _extract_thumb():
            with rawpy.imread(sample_raw) as raw:
                thumb = raw.extract_thumb()
                if thumb and thumb.format == rawpy.ThumbFormat.JPEG:
                    from PIL import Image as PI
                    im = PI.open(_io_module.BytesIO(thumb.data))
                    return im.size
                return "no_jpeg_thumb"
        r, st = with_timeout(_extract_thumb, 15, "rawpy")
        dt_raw = time.time() - t0
        if st == 'ok':
            print(f"  a) extract_thumb: {r} — {dt_raw:.2f}s")
        else:
            print(f"  a) extract_thumb: {st} — {dt_raw:.2f}s")

        # full postprocess (half_size)
        t0 = time.time()
        def _postprocess():
            with rawpy.imread(sample_raw) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
            return rgb.shape
        r, st = with_timeout(_postprocess, 30, "rawpy+postprocess")
        dt_post = time.time() - t0
        if st == 'ok':
            print(f"  b) postprocess(half): {r} — {dt_post:.2f}s")
        else:
            print(f"  b) postprocess(half): {st} — {dt_post:.2f}s")

        # _load_image_rgb
        t0 = time.time()
        def _via_loader_raw():
            from model_loader import _load_image_rgb
            return _load_image_rgb(sample_raw).size
        r, st = with_timeout(_via_loader_raw, 20, "_load_image_rgb(RAW)")
        dt_loader_raw = time.time() - t0
        if st == 'ok':
            print(f"  c) _load_image_rgb: {r} — {dt_loader_raw:.2f}s")
        else:
            print(f"  c) _load_image_rgb: {st} — {dt_loader_raw:.2f}s")
    except ImportError:
        print("  rawpy 未安装，跳过 RAW 测试")
else:
    print("  当前目录无 RAW 文件(ARW等)，跳过")

# ── 4. 缩略图生成 — 全部文件 ──
print(f"\n[4/7] 缩略图生成 ({len(all_files)} 张, 逐文件超时30s)...")
from model_loader import _load_image_rgb, make_thumbnail

thumb_ok, thumb_fail, thumb_hung = 0, 0, 0
thumb_fail_details = []
t0 = time.time()

for i, f in enumerate(all_files):
    def _gen(f=f):
        img = _load_image_rgb(f)
        return make_thumbnail(img, f)
    r, st = with_timeout(_gen, 30, os.path.basename(f))
    if st == 'ok':
        thumb_path = r
        if thumb_path and os.path.exists(thumb_path):
            thumb_ok += 1
        else:
            thumb_fail += 1
            thumb_fail_details.append((os.path.basename(f), 'empty_path'))
    elif 'HUNG' in str(st):
        thumb_hung += 1
        thumb_fail_details.append((os.path.basename(f), st))
    else:
        thumb_fail += 1
        thumb_fail_details.append((os.path.basename(f), st))

    if (i + 1) % 10 == 0 or i == len(all_files) - 1:
        elapsed = time.time() - t0
        print(f"  [{i+1}/{len(all_files)}] OK={thumb_ok} FAIL={thumb_fail} HUNG={thumb_hung} ({elapsed:.0f}s)")

dt_thumbs = time.time() - t0
print(f"  缩略图完成: OK={thumb_ok} FAIL={thumb_fail} HUNG={thumb_hung} — {dt_thumbs:.0f}s")
if thumb_fail_details:
    print(f"  失败文件(前10):")
    for fn, why in thumb_fail_details[:10]:
        print(f"    - {fn}: {why}")

# ── 5. pHash — 全部文件 ──
print(f"\n[5/7] pHash 计算 ({len(all_files)} 张, 逐文件超时30s)...")
import imagehash
from PIL import Image as PILImage

phash_ok, phash_fail, phash_hung = 0, 0, 0
phash_fail_details = []
hashes = {}  # path → phash
t0 = time.time()

for i, f in enumerate(all_files):
    def _phash(f=f):
        # 使用轻量路径: 打开→缩略图→pHash (不依赖_load_image_rgb)
        im = PILImage.open(f)
        im.thumbnail((256, 256), PILImage.LANCZOS)
        return imagehash.phash(im.convert('L'))
    r, st = with_timeout(_phash, 30, os.path.basename(f))
    if st == 'ok':
        hashes[f] = r
        phash_ok += 1
    elif 'HUNG' in str(st):
        phash_hung += 1
        phash_fail_details.append((os.path.basename(f), st))
    else:
        phash_fail += 1
        phash_fail_details.append((os.path.basename(f), st))

    if (i + 1) % 10 == 0 or i == len(all_files) - 1:
        elapsed = time.time() - t0
        print(f"  [{i+1}/{len(all_files)}] OK={phash_ok} FAIL={phash_fail} HUNG={phash_hung} ({elapsed:.0f}s)")

dt_phash_all = time.time() - t0
print(f"  pHash完成: OK={phash_ok} FAIL={phash_fail} HUNG={phash_hung} — {dt_phash_all:.0f}s")
if phash_fail_details:
    print(f"  失败文件(前10):")
    for fn, why in phash_fail_details[:10]:
        print(f"    - {fn}: {why}")

# ── 6. 备选路径 pHash — 使用 _load_image_rgb 再 thumbnail ──
print(f"\n[6/7] pHash via _load_image_rgb (备选路径, 逐文件超时30s)...")
# This tests the alternative: use model_loader's image loader, then thumbnail, then pHash
# This is the fix we'd apply if PIL.open hangs on HEIC

if phash_fail > 0 or phash_hung > 0:
    alt_phash_ok, alt_phash_fail, alt_phash_hung = 0, 0, 0
    alt_fail_details = []
    t0 = time.time()
    failed_only = [f for f in all_files if f not in hashes]

    for i, f in enumerate(failed_only):
        def _alt_phash(f=f):
            from model_loader import _load_image_rgb
            img = _load_image_rgb(f)
            img.thumbnail((256, 256), PILImage.LANCZOS)
            return imagehash.phash(img.convert('L'))
        r, st = with_timeout(_alt_phash, 30, os.path.basename(f))
        if st == 'ok':
            hashes[f] = r
            alt_phash_ok += 1
        elif 'HUNG' in str(st):
            alt_phash_hung += 1
            alt_fail_details.append((os.path.basename(f), st))
        else:
            alt_phash_fail += 1
            alt_fail_details.append((os.path.basename(f), st))

        if (i + 1) % 10 == 0 or i == len(failed_only) - 1:
            print(f"  [{i+1}/{len(failed_only)}] OK={alt_phash_ok} FAIL={alt_phash_fail} HUNG={alt_phash_hung}")

    dt_alt = time.time() - t0
    print(f"  备选路径: OK={alt_phash_ok} FAIL={alt_phash_fail} HUNG={alt_phash_hung} — {dt_alt:.0f}s")
    # Merge back
    phash_ok += alt_phash_ok
    if alt_fail_details:
        print(f"  仍失败:")
        for fn, why in alt_fail_details[:10]:
            print(f"    - {fn}: {why}")
else:
    print("  跳过(全部已成功)")

# ── 7. 连拍去重模拟 ──
print(f"\n[7/7] 连拍去重模拟 (有 {len(hashes)} 个有效 hash)...")
if len(hashes) >= 2:
    # Sort by path (≈ time order)
    sorted_paths = sorted(hashes.keys())
    time_window = 3  # seconds
    hamming_threshold = 5

    # Time-based grouping
    groups = []
    current_group = [sorted_paths[0]]
    for i in range(1, len(sorted_paths)):
        try:
            t1 = os.path.getmtime(sorted_paths[i-1])
            t2 = os.path.getmtime(sorted_paths[i])
            if abs(t2 - t1) <= time_window:
                current_group.append(sorted_paths[i])
            else:
                if len(current_group) > 1:
                    groups.append(current_group)
                current_group = [sorted_paths[i]]
        except Exception:
            current_group.append(sorted_paths[i])
    if len(current_group) > 1:
        groups.append(current_group)

    print(f"  时间分组: {len(groups)} 组 (窗口={time_window}s)")

    # Within each group, find similar clusters
    total_dups = 0
    cluster_details = []
    for gi, group in enumerate(groups):
        if len(group) > 20:
            print(f"  组{gi}: {len(group)}张 → 跳过(超大组)")
            continue
        # Cluster by pHash hamming distance
        clustered = set()
        clusters = []
        for p in group:
            if p in clustered:
                continue
            cluster = [p]
            for q in group:
                if q in clustered or q == p:
                    continue
                if (hashes[p] - hashes[q]) < hamming_threshold:
                    cluster.append(q)
                    clustered.add(q)
            clustered.add(p)
            if len(cluster) > 1:
                clusters.append(cluster)
        if clusters:
            for c in clusters:
                total_dups += len(c) - 1
                cluster_details.append({
                    "kept": os.path.basename(c[0]),
                    "moved_count": len(c) - 1,
                    "moved": [os.path.basename(x) for x in c[1:4]],
                })

    print(f"  有效pHash: {phash_ok}/{len(all_files)}")
    print(f"  时间分组: {len(groups)} 组")
    print(f"  相似簇: {len(cluster_details)} 簇")
    print(f"  可去重: {total_dups} 张")
    if cluster_details:
        for d in cluster_details[:5]:
            moved_preview = ', '.join(d['moved'])
            print(f"    keep={d['kept']} ← del {d['moved_count']}张: {moved_preview}")
else:
    print("  pHash不足，跳过去重模拟")

# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("诊断总结")
print("=" * 60)
print(f"  总文件: {len(all_files)}")
print(f"  格式分布: {dict(exts)}")
print(f"  缩略图:  OK={thumb_ok} FAIL={thumb_fail} HUNG={thumb_hung} ({dt_thumbs:.0f}s)")
print(f"  pHash:   OK={phash_ok} FAIL={phash_fail} HUNG={phash_hung} ({dt_phash_all:.0f}s)")
if phash_ok >= len(all_files):
    print("\n[PASS] 全部文件 pHash 正常 - dedup 可以安全启用")
elif phash_fail + phash_hung <= 2:
    print(f"\n[WARN] {phash_fail+phash_hung} 个文件失败 - 建议 per-file 容错 + 超时保护")
else:
    print(f"\n[FAIL] {phash_fail+phash_hung} 个文件失败 - 需要换用备选加载路径或子进程隔离")

if thumb_ok >= len(all_files):
    print("[PASS] 全部文件缩略图正常")
elif thumb_fail + thumb_hung <= 2:
    print(f"[WARN] {thumb_fail+thumb_hung} 个缩略图失败")
else:
    print(f"[FAIL] {thumb_fail+thumb_hung} 个缩略图失败 - 需修复")

print(f"\n总耗时: {time.time() - t0 if 't0' in dir() else '?'}s")
