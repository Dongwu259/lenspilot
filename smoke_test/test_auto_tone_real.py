"""test_auto_tone_real.py — AI 自动修图真权重端到端验证（live）

镜像两条生产代码路径：
  下载：auto_tone_manager._download_all 的调用链（modelstore.ensure,
        来源链由 photo_s_plugins 的 MS 优先默认值决定）
  推理：photo_editor.handle_auto_tone_editor engine=auto 的调用链
        （find_provider("auto_tone") → auto_tone_params → apply_auto_tone_params）

用法:
  python smoke_test/test_auto_tone_real.py --live [--keep]
  （--keep 保留输出图便于人工检查；默认输出到临时目录）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "electron_app", "python_backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✓" if cond else "✗") + " " + name +
          (f" — {detail}" if detail and not cond else ""), flush=True)


def _make_test_image(out_path):
    """合成测试图：天空渐变 + 肤色块 + 灰阶条（CLIP 可理解的基本视觉结构）。"""
    from PIL import Image, ImageDraw
    w, h = 640, 400
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            # 上半:偏蓝渐变天空;下半:暖棕地面
            if y < h // 2:
                t = y / (h / 2)
                px[x, y] = (int(90 + 60 * t), int(140 - 30 * t), int(210 - 40 * t))
            else:
                px[x, y] = (150 + (x % 40), 110, 80)
    d = ImageDraw.Draw(img)
    # 肤色块 + 灰阶条 + 白块(测白平衡/曝光方向的锚点)
    d.rectangle([40, 40, 140, 120], fill=(222, 182, 152))
    for i in range(8):
        v = 30 + i * 28
        d.rectangle([360 + i * 30, 300, 385 + i * 30, 360], fill=(v, v, v))
    d.rectangle([500, 40, 600, 120], fill=(245, 245, 245))
    img.save(out_path, quality=92)
    return out_path


def _download_all_sync():
    """与 auto_tone_manager._download_all 相同的下载链（同步 CLI 版）。"""
    import photo_s_plugins
    if not photo_s_plugins.ensure_auto_tone_plugin():
        raise RuntimeError("auto-tone 插件不可用")
    from photo_s.modelstore import cached_path, ensure
    from photo_s_plugin_auto_tone import models as _m

    # 1) 核心 required 权重
    for name, meta in _m.WEIGHTS.items():
        if not meta.get("required"):
            continue
        specs = _m.weight_specs([name])
        if specs and cached_path(specs[0]):
            check(f"核心权重已就绪 {name}", True)
            continue
        if not specs:
            raise RuntimeError(f"未知权重: {name}")
        print(f"  下载核心权重 {name} ...", flush=True)
        ensure(specs[0], timeout=300)
        check(f"核心权重下载+sha256 {name}", bool(cached_path(specs[0])))

    # 2) 视觉塔（GB 级；photo_s_plugins 默认 MS 优先）
    repo, chain = photo_s_plugins.resolve_tower_chain()
    print(f"  塔解析: repo={repo} chain={chain}", flush=True)
    if repo is None:
        raise RuntimeError(f"塔解析失败: {chain}")
    already = bool(_m._hf_cache_hit(repo)) or any(
        cached_path(_m._tower_spec(repo, src)) for src in chain)
    if already:
        check("视觉塔已就绪", True)
        return
    errors = []
    done = False
    for src in chain:
        try:
            spec = _m._tower_spec(repo, src)
            print(f"  下载视觉塔 {spec.name}（{spec.size/1e9:.2f}GB, src={src}）...",
                  flush=True)
            ensure(spec, timeout=7200)
            done = True
            break
        except Exception as e:
            errors.append(f"{src}: {e}")
    check("视觉塔下载+sha256", done, "; ".join(errors))


def _real_inference(keep):
    """与 photo_editor.handle_auto_tone_editor engine=auto 相同的推理链。"""
    import tempfile
    import photo_s_plugins
    assert photo_s_plugins.ensure_auto_tone_plugin()
    from photo_s.engine import ProcessOptions
    from photo_s.hooks import PluginContext
    from photo_s.plugin import find_provider
    from photo_s.autotone import apply_auto_tone_params

    provider = find_provider("auto_tone")
    check("find_provider(auto_tone)", provider is not None)

    tmpdir = tempfile.mkdtemp(prefix="lenspilot_at_live_")
    img_path = _make_test_image(os.path.join(tmpdir, "src.jpg"))

    import time
    t0 = time.time()
    ctx = PluginContext(input_path=img_path, options=ProcessOptions())
    pred = provider.auto_tone_params(1.0, ctx)
    dt = time.time() - t0
    opts = pred.get("options") or {}
    conf = pred.get("confidence")
    nondefault = {k: v for k, v in opts.items() if v not in (None, 0, 0.0, "")}
    print(f"  推理耗时 {dt:.1f}s | confidence={conf} | 预测字段 {len(opts)} 项",
          flush=True)
    print(f"  非零参数: {nondefault}", flush=True)
    check("auto_tone_params 返回 options", isinstance(opts, dict) and len(opts) >= 5)
    check("9 字段协议词汇", all(k in {
        "exposure", "contrast", "saturation", "vibrance", "wb_temp",
        "wb_tint", "clarity", "texture", "dehaze"} for k in opts),
        str(sorted(opts)))

    # 应用到像素（编辑器 apply_edits 同款路径）→ 导出对比图
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    out = apply_auto_tone_params(img, pred)
    out_path = os.path.join(tmpdir, "after.jpg")
    out.save(out_path, quality=92)
    check("apply_auto_tone_params 像素应用",
          os.path.isfile(out_path) and os.path.getsize(out_path) > 10_000)
    print(f"  输出: {out_path if keep else '(临时目录,--keep 可保留)'}", flush=True)
    return tmpdir


if __name__ == "__main__":
    if "--live" not in sys.argv:
        print("本脚本仅 live 模式（真权重下载+推理）。加 --live 运行。")
        sys.exit(0)
    keep = "--keep" in sys.argv
    _download_all_sync()
    import photo_s_plugins
    st = photo_s_plugins.ai_engine_status()
    check("ai_engine_status ready", bool(st.get("ready")), str(st))
    _real_inference(keep)
    print(f"\n通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", *FAIL, sep="\n  ")
        sys.exit(1)
