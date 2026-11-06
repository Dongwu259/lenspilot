#!/usr/bin/env python
"""
#106/#107 基准与正确性验证脚本
=============================

用 PIL 生成合成 JPEG 到临时目录，对比：
  1. legacy   —— 旧逐张路径（_score_one 循环，基线）
  2. bs=1     —— 新流水线 batch_size=1（隔离“预取/线程池”的收益）
  3. bs=8     —— 新流水线默认批大小（ONNX 固定 batch=1 时主要收益来自预取重叠）
并做正确性校验：逐张 vs 批量分数一致、坏文件隔离、目录缓存命中/失效。

用法（Windows Git Bash）:
  python smoke_test/benchmark_score_batch.py [--count 300] [--backend onnx|pytorch]

说明：
- 全程在临时 APS_USER_DATA 下运行，不碰真实 score_cache / 缩略图缓存。
- 默认用 electron_app/model/best_model.onnx（真实模型真实推理）。
  --backend pytorch 时用 best_model/best_model.pth（仓库根目录，需 torch+CUDA）。
"""
import argparse
import io
import os
import shutil
import sys
import tempfile
import time

# ── 隔离的用户数据目录（必须在导入后端模块之前设置） ──
_TMP_ROOT = tempfile.mkdtemp(prefix="lp_bench_")
os.environ["APS_USER_DATA"] = os.path.join(_TMP_ROOT, "userdata")
os.makedirs(os.environ["APS_USER_DATA"], exist_ok=True)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
BACKEND = os.path.join(PROJ, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def gen_images(dst_dir, count, w=1024, h=768):
    """生成 count 张内容各异的合成 JPEG（渐变+噪声，真实 JPEG 编码）。"""
    import numpy as np
    from PIL import Image
    os.makedirs(dst_dir, exist_ok=True)
    rng = np.random.default_rng(42)
    paths = []
    for i in range(count):
        yy, xx = np.mgrid[0:h, 0:w]
        base = ((xx * (i % 7 + 1) + yy * (i % 5 + 1)) % 255).astype(np.uint8)
        noise = rng.integers(0, 40, size=(h, w), dtype=np.uint8)
        r = np.clip(base + noise, 0, 255).astype(np.uint8)
        g = np.clip(base * 0.6 + noise, 0, 255).astype(np.uint8)
        b = np.clip(255 - base + noise * 0.5, 0, 255).astype(np.uint8)
        arr = np.stack([r, g, b], axis=-1)
        p = os.path.join(dst_dir, f"img_{i:04d}.jpg")
        Image.fromarray(arr).save(p, "JPEG", quality=85)
        paths.append(p)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--backend", default="onnx", choices=["onnx", "pytorch"])
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    args = ap.parse_args()

    import model_loader as ml
    import photo_scorer as ps
    import score_cache  # noqa: F401  (确认指向临时 DB)

    # 静默 IPC 事件/日志（standalone 进程里它们会刷 stdout）
    ps.send_event = lambda *a, **k: None
    ps.send_log = lambda *a, **k: None

    # ── 加载真实模型 ──
    t0 = time.time()
    if args.backend == "onnx":
        onnx_path = os.path.join(PROJ, "electron_app", "model", "best_model.onnx")
        if not os.path.exists(onnx_path):
            print(f"FATAL: 找不到 {onnx_path}")
            return 1
        ml._load_onnx(onnx_path)
    else:
        pth = os.path.join(PROJ, "best_model", "best_model.pth")
        if not os.path.exists(pth):
            print(f"FATAL: 找不到 {pth}")
            return 1
        ml._load_pytorch(pth)
    ps._model_loaded = True
    mv = ml.model_version()
    print(f"模型加载: {ml._backend} / {ml._device} ({time.time()-t0:.1f}s) mv={mv}")
    print(f"score_cache DB: {score_cache._db_path}")
    print(f"批大小(auto): {ps._resolve_batch_size()}  预取线程: {ps._resolve_prefetch_workers()}")

    # ── 生成图片（1 张坏文件验证隔离） ──
    src_dir = os.path.join(_TMP_ROOT, "src")
    print(f"\n生成 {args.count} 张合成 JPEG ({args.width}x{args.height}) → {src_dir}")
    t0 = time.time()
    src_paths = gen_images(src_dir, args.count, w=args.width, h=args.height)
    with open(os.path.join(src_dir, "img_9999_corrupt.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"not a real jpeg")
    src_paths.append(os.path.join(src_dir, "img_9999_corrupt.jpg"))
    print(f"  生成耗时 {time.time()-t0:.1f}s")

    def fresh_copy(tag):
        """把源图复制到独立目录 → 保证 score_cache 全 miss。"""
        d = os.path.join(_TMP_ROOT, tag)
        os.makedirs(d, exist_ok=True)
        for p in src_paths:
            shutil.copy2(p, os.path.join(d, os.path.basename(p)))
        return d

    results = {}

    # ── 模式 1: legacy 逐张 _score_one ──
    d1 = fresh_copy("run_legacy")
    imgs = ps.find_images(d1)
    t0 = time.time()
    legacy_scores = {}
    legacy_failed = 0
    for p in imgs:
        r = ps._score_one(p, mv)
        legacy_scores[r["filename"]] = r["score"]
        if r["score"] < 0:
            legacy_failed += 1
    dt = time.time() - t0
    results["legacy(_score_one 逐张)"] = dt
    print(f"\n[legacy] {len(imgs)} 张 耗时 {dt:.2f}s  ({dt/len(imgs)*1000:.1f} ms/张)  failed={legacy_failed}")

    # ── 模式 2: 流水线 batch_size=1 ──
    d2 = fresh_copy("run_bs1")
    t0 = time.time()
    r2 = ps.handle_score_photos({"folder": d2, "threshold": 6.0,
                                 "copy_on_score": False, "batch_size": 1})
    dt = time.time() - t0
    results["pipeline bs=1"] = dt
    print(f"[bs=1]   {r2['total']} 张 耗时 {dt:.2f}s  ({dt/r2['total']*1000:.1f} ms/张)  failed={r2['failed']}")

    # ── 模式 3: 流水线 batch_size=8 ──
    d3 = fresh_copy("run_bs8")
    t0 = time.time()
    r3 = ps.handle_score_photos({"folder": d3, "threshold": 6.0,
                                 "copy_on_score": False, "batch_size": 8})
    dt = time.time() - t0
    results["pipeline bs=8"] = dt
    print(f"[bs=8]   {r3['total']} 张 耗时 {dt:.2f}s  ({dt/r3['total']*1000:.1f} ms/张)  failed={r3['failed']}")

    # ── 正确性 1: 批量分数 == 逐张分数 ──
    rows = score_cache.get_folder(d3, mv, limit=100000)
    pipe_scores = {os.path.basename(r["path"]): r["score"] for r in rows}
    diffs = []
    for name, s in legacy_scores.items():
        if s >= 0 and name in pipe_scores:
            diffs.append(abs(pipe_scores[name] - s))
    max_diff = max(diffs) if diffs else -1
    print(f"\n[正确性] 逐张 vs 批量 分数最大偏差: {max_diff:.6f}  (对比 {len(diffs)} 张)")
    # CPU/ONNX 完全确定应逐位一致；GPU 上不同 batch 走不同 cudnn kernel，
    # 浮点累加顺序不同会有 1e-3 级抖动（1-10 分制下可忽略）。
    tol = 1e-4 if args.backend == "onnx" else 5e-3
    assert max_diff < tol, f"批量与逐张分数不一致! max_diff={max_diff}"

    # ── 正确性 2: 坏文件隔离（score=-1 计入 failed，不污染整批） ──
    assert r3["failed"] == legacy_failed == 1, f"坏文件计数异常: legacy={legacy_failed} bs8={r3['failed']}"
    print(f"[正确性] 坏文件隔离 OK (failed=1, 其余 {r3['total']-1} 张正常)")

    # ── 正确性 3: 缓存命中跳过推理 ──
    t0 = time.time()
    r4 = ps.handle_score_photos({"folder": d3, "threshold": 6.0, "copy_on_score": False})
    dt = time.time() - t0
    print(f"[缓存]   二次评分(全缓存命中) 耗时 {dt:.2f}s  cached={r4['cached']}")
    assert r4["cached"] == r4["total"] - 1, "缓存命中数异常（坏文件不入缓存，应为 total-1）"

    # ── #106: 目录列表缓存 ──
    print("\n[#106 目录缓存]")
    t0 = time.time(); ps.find_images(d3, force_refresh=True); t_walk = time.time() - t0
    t0 = time.time(); ps.find_images(d3); t_hit = time.time() - t0
    print(f"  首次 walk: {t_walk*1000:.1f}ms   缓存命中: {t_hit*1000:.2f}ms")
    # 根目录新增文件 → mtime 校验自动失效
    new_p = os.path.join(d3, "img_5000_new.jpg")
    shutil.copy2(src_paths[0], new_p)
    time.sleep(0.05)
    n_after_add = len(ps.find_images(d3))
    assert n_after_add == r4["total"] + 1, f"新增文件后列表未刷新: {n_after_add}"
    print(f"  新增文件后自动重扫 OK ({n_after_add} 张)")
    # rename handler → 主动失效
    rn = os.path.join(d3, "img_5000_renamed.jpg")
    ps.handle_rename_file({"oldPath": new_p, "newPath": rn})
    n_after_rn = len(ps.find_images(d3))
    names = [os.path.basename(p) for p in ps.find_images(d3)]
    assert n_after_rn == n_after_add and "img_5000_renamed.jpg" in names and "img_5000_new.jpg" not in names
    print("  rename_file 后缓存失效 OK")
    # delete handler → 主动失效
    ps.handle_delete_file({"path": rn})
    assert len(ps.find_images(d3)) == n_after_add - 1
    print("  delete_file 后缓存失效 OK")
    # force_refresh 绕过缓存
    shutil.copy2(src_paths[0], new_p)
    assert len(ps.find_images(d3, force_refresh=True)) == n_after_add
    print("  force_refresh OK")

    # ── 汇总 ──
    print("\n════ 汇总 ════")
    base = results["legacy(_score_one 逐张)"]
    for k, v in results.items():
        print(f"  {k:24s} {v:7.2f}s   speedup {base/v:.2f}x")

    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
