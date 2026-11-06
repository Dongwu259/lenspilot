#!/usr/bin/env python3
"""
autotone_probe.py — auto-tone 模型条件化诊断探针

回答两个问题(2026-09-12 偏蓝事故归因):
  1. 模型对不同场景的预测是否近似恒定(条件化坍缩=输出数据集均值)?
  2. 模型对画面偏色是否有感知(人为偏暖/偏冷后预测是否移动)?

用法: python smoke_test/autotone_probe.py [--photos DIR] [--n 10] [--counterfactual]
单进程短生命周期: 只加载一次塔,预测完即退,不拉 llama-server。
"""
import argparse
import glob
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "electron_app", "python_backend")
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIELDS = ("exposure", "contrast", "saturation", "vibrance", "wb_temp",
          "wb_tint", "clarity", "texture", "dehaze")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", default="D:/photoDatebase/3-19-扫街")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--counterfactual", action="store_true", default=True)
    ap.add_argument("--tmp", default="D:/tmp/autotone_probe")
    args = ap.parse_args()

    import photo_s_plugins
    photo_s_plugins.ensure_auto_tone_plugin()
    from photo_s.plugin import find_provider
    from photo_s.hooks import PluginContext
    from photo_s.engine import ProcessOptions
    from photo_s.adjust import _temperature_gains

    provider = find_provider("auto_tone")
    if provider is None:
        print("ERROR: auto-tone provider 未注册")
        return 1
    print(f"provider: {type(provider).__name__}\n")

    files = sorted(glob.glob(os.path.join(args.photos, "*.arw")))
    if not files:
        print(f"ERROR: 无 ARW: {args.photos}")
        return 1
    n = min(args.n, len(files))
    picks = [files[round(i * (len(files) - 1) / max(n - 1, 1))] for i in range(n)]

    def predict(path):
        ctx = PluginContext(input_path=path, options=ProcessOptions())
        return provider.auto_tone_params(1.0, ctx)

    def show(tag, path):
        r = predict(path)
        opts = r.get("options") or {}
        conf = r.get("confidence")
        rag = r.get("rag_used")
        skip = r.get("skipped")
        if skip or not opts:
            print(f"{tag:<28} SKIPPED {r.get('warnings') or ''}")
            return None
        row = "  ".join(f"{f}={opts.get(f):+.4g}" for f in FIELDS if f in opts)
        t = opts.get("wb_temp")
        extra = ""
        if t:
            g = _temperature_gains(float(t))
            extra = f"  → 引擎增益 B/R={g[2]/g[0]:.3f} (6500K=1.0)"
        print(f"{tag:<28} {row}  conf={conf} rag={rag}{extra}")
        return opts

    results = []
    print("── 不同场景预测 ──")
    for p in picks:
        opts = show(os.path.basename(p), p)
        if opts:
            results.append(opts)

    if results:
        print("\n── 汇总(跨场景离散度) ──")
        for f in FIELDS:
            vals = [r[f] for r in results if f in r]
            if len(vals) >= 2:
                print(f"  {f:<12} 均值={statistics.mean(vals):+.4g}  "
                      f"std={statistics.pstdev(vals):.4g}  "
                      f"min={min(vals):+.4g} max={max(vals):+.4g}")

    if args.counterfactual and picks:
        import numpy as np
        from PIL import Image
        import rawpy
        os.makedirs(args.tmp, exist_ok=True)
        base_path = picks[len(picks) // 2]
        with rawpy.imread(base_path) as raw:
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False,
                                  output_color=rawpy.ColorSpace.sRGB,
                                  gamma=(2.222, 4.5), output_bps=8)
        img = Image.fromarray(rgb)
        variants = {"原样": img}
        a = np.asarray(img).astype(np.float32)
        variants["偏暖(R+18%/B-18%)"] = Image.fromarray(
            np.clip(np.stack([a[..., 0] * 1.18, a[..., 1], a[..., 2] * 0.82], -1),
                    0, 255).astype("uint8"))
        variants["偏冷(R-18%/B+18%)"] = Image.fromarray(
            np.clip(np.stack([a[..., 0] * 0.82, a[..., 1], a[..., 2] * 1.18], -1),
                    0, 255).astype("uint8"))
        print(f"\n── 反事实测试(同一张图 {os.path.basename(base_path)}) ──")
        for name, im in variants.items():
            vp = os.path.join(args.tmp, f"cf_{abs(hash(name)) % 10000}.jpg")
            im.save(vp, quality=92)
            show(name, vp)

    print("\n探针完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
