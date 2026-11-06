"""
auto_tone_manager.py — AI 自动修图(auto-tone)权重管理（0.9.0）

photo_s auto-tone 插件的权重经 modelstore 下载（来源链 GitHub release →
ModelScope 国内镜像，断点续传 + sha256 校验，与上游一致）。本模块提供：

  auto_tone_status    状态查询：插件/torch/核心权重/视觉塔就绪度 + 清单（零网络）
  auto_tone_download  触发后台下载：核心小权重(~4.6MB) + 视觉塔(GB 级,按 checkpoint 解析)

下载进度经 auto_tone_download_progress/done/error 事件推送（监视 modelstore
的 .part 文件增长换算百分比——modelstore.ensure 本身无进度回调）。设计红线：
管线与编辑器绝不隐式触发下载，GB 级塔权重只从设置页这个入口拉。
"""

import os
import threading
import time

_lock = threading.Lock()
_downloading = False


def _emit(name, data):
    from service import send_event
    send_event(name, data)


def _weights_manifest():
    """权重清单（零网络）：[{name, size, required, ready}]。异常返回空表。"""
    try:
        from photo_s.modelstore import cached_path
        from photo_s_plugin_auto_tone import models as _m
        items = []
        for name, meta in _m.WEIGHTS.items():
            specs = _m.weight_specs([name])
            items.append({
                "name": name,
                "size": int(meta.get("size", 0)),
                "required": bool(meta.get("required")),
                "ready": bool(specs and cached_path(specs[0])),
            })
        return items
    except Exception:
        return []


def _tower_manifest():
    """视觉塔条目（零网络）。返回 [{name, size, ready}]；未登记塔返回空表。"""
    try:
        import photo_s_plugins
        from photo_s.modelstore import cached_path
        from photo_s_plugin_auto_tone import models as _m
        repo, chain = photo_s_plugins.resolve_tower_chain()
        if repo is None:
            return []
        meta = _m.TOWERS.get(repo) or {}
        fn = meta.get("filename", "open_clip_pytorch_model.bin")
        ready = bool(_m._hf_cache_hit(repo)) or any(
            cached_path(_m._tower_spec(repo, src)) for src in chain)
        return [{"name": f"{repo}/{fn}", "size": int(meta.get("size", 0)),
                 "required": True, "ready": ready}]
    except Exception:
        return []


def handle_auto_tone_status(params):
    """auto_tone_status：引擎/权重就绪状态（零网络零下载）。"""
    import photo_s_plugins
    st = photo_s_plugins.ai_engine_status()
    manifest = _weights_manifest() + _tower_manifest()
    req = [w for w in manifest if w["required"]]
    return {
        "ok": True,
        "ready": bool(st.get("ready")),
        "engine": st,
        "downloading": _downloading,
        "weights": manifest,
        "required_total_bytes": sum(w["size"] for w in req),
        "required_ready_bytes": sum(w["size"] for w in req if w["ready"]),
    }


def handle_auto_tone_download(params):
    """auto_tone_download：后台线程下载缺失的核心权重 + 视觉塔（幂等）。"""
    global _downloading
    with _lock:
        if _downloading:
            return {"ok": True, "already": True}
        _downloading = True
    threading.Thread(target=_download_all, daemon=True,
                     name="auto-tone-download").start()
    return {"ok": True, "started": True}


def _download_with_progress(spec, total_timeout=7200):
    """ensure() + .part 增长监视线程 → auto_tone_download_progress 事件。"""
    from photo_s.modelstore import cached_path, ensure

    if cached_path(spec):
        _emit("auto_tone_download_progress",
              {"file": spec.name, "pct": 100, "downloaded": spec.size,
               "total": spec.size})
        return

    stop = threading.Event()

    def _watch():
        # .part 在最终路径旁: <path>.<pid>.<tid>.part —— 通配本权重全部 part 取最大者
        from photo_s.modelstore import _path_for
        final = _path_for(spec)
        prefix = os.path.basename(final) + "."
        parent = os.path.dirname(final)
        last_pct = -100
        while not stop.is_set():
            try:
                best = 0
                if os.path.isdir(parent):
                    for fn in os.listdir(parent):
                        if fn.startswith(prefix) and fn.endswith(".part"):
                            best = max(best, os.path.getsize(os.path.join(parent, fn)))
                pct = int(best / max(spec.size, 1) * 100)
                if pct - last_pct >= 2:  # 与选片模型下载同款 2% 步进
                    last_pct = pct
                    _emit("auto_tone_download_progress",
                          {"file": spec.name, "pct": min(pct, 99),
                           "downloaded": best, "total": spec.size})
            except Exception:
                pass
            stop.wait(2.0)

    watcher = threading.Thread(target=_watch, daemon=True, name="at-progress")
    watcher.start()
    try:
        ensure(spec, timeout=60)
        _emit("auto_tone_download_progress",
              {"file": spec.name, "pct": 100, "downloaded": spec.size,
               "total": spec.size})
    finally:
        stop.set()


def _download_all():
    """下载全部缺失权重：核心 required 小权重 → 视觉塔（按 checkpoint 解析）。"""
    global _downloading
    try:
        import photo_s_plugins
        if not photo_s_plugins.ensure_auto_tone_plugin():
            raise RuntimeError("auto-tone 插件不可用（安装不完整）")

        from photo_s.modelstore import cached_path
        from photo_s_plugin_auto_tone import models as _m

        # 1) 核心 required 权重（小,秒级）
        for name, meta in _m.WEIGHTS.items():
            if not meta.get("required"):
                continue
            specs = _m.weight_specs([name])
            if specs and cached_path(specs[0]):
                continue
            if not specs:
                raise RuntimeError(f"未知权重: {name}")
            _download_with_progress(specs[0])

        # 2) 视觉塔（GB 级；来源链 hf → modelscope，断点续传）
        repo, chain = photo_s_plugins.resolve_tower_chain()
        if repo is not None:
            already = bool(_m._hf_cache_hit(repo)) or any(
                cached_path(_m._tower_spec(repo, src)) for src in chain)
            if not already:
                errors = []
                done = False
                for src in chain:
                    try:
                        _download_with_progress(_m._tower_spec(repo, src))
                        done = True
                        break
                    except Exception as e:
                        errors.append(f"{src}: {e}")
                if not done:
                    raise RuntimeError("视觉塔下载失败（" + "; ".join(errors) + "）")

        st = photo_s_plugins.ai_engine_status()
        _emit("auto_tone_download_done", {"ready": bool(st.get("ready")),
                                          "engine": st})
    except Exception as e:
        _emit("auto_tone_download_error", {"error": str(e)})
    finally:
        with _lock:
            _downloading = False


def register_handlers():
    from service import register_handler
    register_handler("auto_tone_status", handle_auto_tone_status)
    register_handler("auto_tone_download", handle_auto_tone_download)
