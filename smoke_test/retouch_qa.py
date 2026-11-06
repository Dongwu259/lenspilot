#!/usr/bin/env python3
"""
retouch_qa.py — 修图质量抽检:ARW 引擎默认解码(前) vs 修图输出(后)
指标: 平均亮度/对比(std)/饱和度/高光裁剪/阴影裁剪 + 生成并排对比图
用法: python smoke_test/retouch_qa.py <arw源目录> <修图输出目录> [--pairs 6]
"""
import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "electron_app", "python_backend")
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def decode_raw(path):
    """引擎同参默认解码(camera WB/sRGB/gamma2.222)。"""
    import rawpy
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False,
                              output_color=rawpy.ColorSpace.sRGB,
                              gamma=(2.222, 4.5), output_bps=8)
    return Image.fromarray(rgb)


def metrics(img):
    a = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    luma = a @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    return {
        "luma": round(float(luma.mean()), 4),
        "contrast": round(float(luma.std()), 4),
        "saturation": round(float(sat.mean()), 4),
        "clip_hi": round(float((mx > 0.995).mean()), 5),
        "clip_lo": round(float((mn < 0.005).mean()), 5),
        # R−B 通道均值差(正=偏暖): 全批偏蓝事故的量化指标(2026-09-12)
        "wb_shift": round(float(a[..., 0].mean() - a[..., 2].mean()), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--pairs", type=int, default=6)
    ap.add_argument("--start", type=int, default=0, help="跳过前 N 个输出(取中段样本)")
    ap.add_argument("--side-dir", default="")
    args = ap.parse_args()

    outs = sorted(glob.glob(os.path.join(args.out, "*.jpg")))[args.start:]
    if not outs:
        print("无输出")
        return 1
    side_dir = args.side_dir or os.path.join(args.out, "_对比图")
    os.makedirs(side_dir, exist_ok=True)

    print(f"{'文件':<22} {'亮度 前→后':<16} {'对比 前→后':<16} "
          f"{'饱和 前→后':<16} {'R−B偏移 前→后':<18}")
    n = 0
    for op in outs:
        stem = os.path.splitext(os.path.basename(op))[0]
        for suf in ("_修图", "_compressed", "_size"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
        srcs = glob.glob(os.path.join(args.src, stem + ".*"))
        if not srcs:
            continue
        src = srcs[0]
        if src.lower().endswith((".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng")):
            before = decode_raw(src)
        else:
            before = Image.open(src)
        after = Image.open(op)
        mb, ma = metrics(before), metrics(after)
        print(f"{stem:<22} {mb['luma']:.3f}→{ma['luma']:.3f}      "
              f"{mb['contrast']:.3f}→{ma['contrast']:.3f}      "
              f"{mb['saturation']:.3f}→{ma['saturation']:.3f}      "
              f"{mb['wb_shift']:+.4f}→{ma['wb_shift']:+.4f}")

        # 并排对比图(横向,各压到 900 宽)
        w = 900

        def _th(im):
            r = w / im.width
            return im.convert("RGB").resize((w, int(im.height * r)))
        b, a = _th(before), _th(after)
        h = max(b.height, a.height)
        canvas = Image.new("RGB", (w * 2 + 8, h), (24, 24, 24))
        canvas.paste(b, (0, 0))
        canvas.paste(a, (w + 8, 0))
        canvas.save(os.path.join(side_dir, f"对比_{stem}.jpg"), quality=88)
        n += 1
        if n >= args.pairs:
            break
    print(f"\n{n} 对完成,对比图 → {side_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
