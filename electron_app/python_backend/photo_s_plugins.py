"""
photo_s_plugins.py — photo_s 插件手动注册（frozen 环境兜底）

PyInstaller 冻结后 importlib.metadata 的 entry_points 不可靠（插件包的
dist-info 未必随包收集），photo_s.plugin.discover_plugins() 在冻结环境
可能发现不到 auto-tone 插件 → 引擎/MCP 拿不到 provider。

本模块在 service.py 启动（IPC 与 --mcp-server 两条路径）时把 vendored
的 photo_s_plugin_auto_tone 手动挂进 photo_s 的插件缓存列表：
  - 导入失败/注册失败一律静默（CPU 版无 torch 也能注册成功——重依赖
    全懒加载，真正推理时才缺）→ 引擎自动走 suggest 降级，绝不崩启动；
  - 幂等：已注册（含 entry_points 正常发现的场景）不重复挂。
"""

import os
import sys

# 国内默认走 ModelScope 源（用户/上游显式设置的环境变量优先）：权重 8 件
# 全量镜像 dwphoto/photo-s-auto-tone-v2，两个视觉塔亦在 ModelScope 有镜像
# ——跳过国内必失败的 HF 尝试（HF 缓存命中仍优先，老用户零重复下载）。
# 必须在任何 photo_s_plugin_auto_tone 导入之前生效：插件 models 模块在
# import 时读取这两个变量定死来源链，本模块是全部消费方的首个 import 点
# （service.py 两条分支 / pipeline_tool_steps / photo_editor / auto_tone_manager
# 均先 import 本模块，插件包自身只在函数体内懒加载）。
os.environ.setdefault("PHOTOS_AUTO_TONE_WEIGHT_SOURCE", "modelscope")
os.environ.setdefault("PHOTOS_AUTO_TONE_TOWER_SOURCE", "modelscope")

_registered = False


def ensure_auto_tone_plugin():
    """把 vendored auto-tone 插件手动注册进 photo_s 插件缓存。幂等。

    Returns:
        True=插件已可用（含已被 entry_points 发现的场景）；False=不可用。
    """
    global _registered
    if _registered:
        return True
    try:
        from photo_s import plugin as _ps_plugin
        from photo_s_plugin_auto_tone import AutoTonePlugin

        plugins = _ps_plugin.discover_plugins()  # 拿到的是缓存的同一 list
        if any(getattr(p, "name", "") == "auto_tone" for p in plugins):
            _registered = True
            return True

        inst = AutoTonePlugin()
        inst.name = "auto_tone"
        plugins.append(inst)
        _registered = True
        return True
    except Exception as e:  # 插件缺失/损坏——photo_s 引擎有 suggest 降级
        try:
            print(f"[photo_s_plugins] auto-tone 插件注册失败(降级 suggest): "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def auto_tone_available():
    """auto-tone 插件是否已注册成功（不保证权重/推理依赖就绪）。"""
    return _registered


def _checkpoint_cached_path():
    """auto-tone 主 checkpoint（auto_tone_v7_clean.pt）的缓存路径；未就绪返回 None。"""
    from photo_s.modelstore import cached_path
    from photo_s_plugin_auto_tone import models as _m

    specs = _m.weight_specs(["auto_tone_v7_clean.pt"])
    return cached_path(specs[0]) if specs else None


def _tower_model_name(ck_file):
    """镜像 photo_s_plugin_auto_tone predictor.load 的塔名推断（零下载）。"""
    import torch
    ck = torch.load(ck_file, map_location="cpu", weights_only=True)
    model_name = ck.get("model_name")
    pretrained = ck.get("pretrained")
    sig_dim = ck.get("sig_dim", 0)
    if model_name is None and sig_dim:
        if sig_dim == 1024:
            model_name = "ViT-L-16-SigLIP-384"
        elif sig_dim == 768:
            model_name = "ViT-L-16-SigLIP-256"
        else:
            model_name = f"ViT-L-16-SigLIP-{sig_dim}"
        pretrained = "webli"
    return model_name, pretrained


def resolve_tower_chain():
    """从 checkpoint 解析视觉塔的 (repo, 来源链)。

    Returns:
        (repo, chain) —— repo 为 HF repo id（或 None=未登记塔，走 open_clip
        自身行为无法预检）；chain 为 ["hf", "modelscope"] 型来源列表（受
        PHOTOS_AUTO_TONE_TOWER_SOURCE 环境变量约束，与上游一致）。
        checkpoint 缺失返回 (None, "checkpoint")；torch 缺失返回 (None, "torch")。
    """
    ck_file = _checkpoint_cached_path()
    if not ck_file:
        return None, "checkpoint"
    try:
        model_name, pretrained = _tower_model_name(ck_file)
    except ImportError:
        return None, "torch"
    except Exception as e:
        return None, f"error:{type(e).__name__}"

    from photo_s_plugin_auto_tone import models as _m
    repo = _m._tower_repo(model_name, pretrained)
    if not repo or repo not in _m.TOWERS:
        return None, "tower:unknown"
    source = os.environ.get("PHOTOS_AUTO_TONE_TOWER_SOURCE", "auto")
    chain = {"hf": ["hf"], "modelscope": ["modelscope"]}.get(
        str(source).strip().lower(), ["hf", "modelscope"])
    return repo, chain


def ai_engine_status():
    """auto-tone AI 引擎就绪探测——零网络零下载，纯本地缓存检查。

    供管线步骤门槛（engine=auto 时权重不齐自动降级 suggest，绝不隐式
    拉 GB 级塔权重）与设置页状态卡共用。

    Returns:
        dict: {plugin, torch, core_weights, tower, ready, reason}
              reason 为首个未就绪环节的可读标识（'plugin'/'torch'/
              'weight:<name>'/'tower'/'tower:unknown'），ready=True 时为 'ok'。
    """
    out = {"plugin": False, "torch": False, "core_weights": False,
           "tower": False, "ready": False, "reason": "ok"}
    if not ensure_auto_tone_plugin():
        out["reason"] = "plugin"
        return out
    out["plugin"] = True
    try:
        import torch  # noqa: F401
    except ImportError:
        out["reason"] = "torch"
        return out
    out["torch"] = True

    try:
        from photo_s.modelstore import cached_path
        from photo_s_plugin_auto_tone import models as _m

        for name, meta in _m.WEIGHTS.items():
            if not meta.get("required"):
                continue
            specs = _m.weight_specs([name])
            if not specs or not cached_path(specs[0]):
                out["reason"] = f"weight:{name}"
                return out
        out["core_weights"] = True

        repo, chain = resolve_tower_chain()
        if repo is None:
            if chain == "tower:unknown":
                # 未登记的塔走 open_clip 自身行为，无法预检——视为就绪
                out["tower"] = True
                out["ready"] = True
                return out
            out["reason"] = str(chain)
            return out
        if _m._hf_cache_hit(repo):
            out["tower"] = True
            out["ready"] = True
            return out
        for src in chain:
            if cached_path(_m._tower_spec(repo, src)):
                out["tower"] = True
                out["ready"] = True
                return out
        out["reason"] = "tower"
        return out
    except Exception as e:
        out["reason"] = f"error:{type(e).__name__}"
        return out
