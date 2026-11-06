"""模型权重注册与路径解析

许可注意：本模块管理的权重文件采用 CC-BY-NC 4.0（非商用），
代码本身为 MIT。详见插件目录 LICENSE-WEIGHTS.txt。

权重不打进 wheel，首次使用时从 photo_s 仓库的 GitHub Release 下载到
photo_s.modelstore 缓存目录（~/.cache/photo-s/models，可用
$PHOTOS_CACHE_DIR 覆盖），并做 sha256 校验。

所有 URL / sha256 均可用环境变量覆盖（离线 / 镜像 / 测试场景）：
    PHOTOS_AUTO_TONE_URL_BASE       覆盖整个 URL 前缀
    PHOTOS_AUTO_TONE_<NAME>_URL     覆盖单个文件 URL（file:// 亦可）
    PHOTOS_AUTO_TONE_<NAME>_SHA256  覆盖单个文件 sha256
    PHOTOS_AUTO_TONE_QWEN_BASE      Qwen3-VL 基座（本地路径或 HF model id）

视觉塔（SigLIP / CLIP，open_clip 运行时从 HuggingFace 拉取的大文件）
由 :func:`resolve_tower_pretrained` 经 modelstore 下载校验，国内可回落
ModelScope 镜像（见 TOWERS 注释）。
"""

import json
import os
import shutil
import threading
from typing import Dict, List, Optional

GITHUB_REPO = "Dongwu259/photo_s"
RELEASE_TAG = "auto-tone-v0.1.0"
URL_BASE = f"https://github.com/{GITHUB_REPO}/releases/download/{RELEASE_TAG}"
# v2.1 风格化权重托管在独立 release tag（老 tag 不动，已有缓存不受影响）
STYLE_RELEASE_TAG = "auto-tone-v2.1.0"
STYLE_URL_BASE = f"https://github.com/{GITHUB_REPO}/releases/download/{STYLE_RELEASE_TAG}"

QWEN_BASE_MODEL = os.environ.get("PHOTOS_AUTO_TONE_QWEN_BASE",
                                 "Qwen/Qwen3-VL-2B-Instruct")

# ── 插件权重 ModelScope 镜像（dwphoto/photo-s-auto-tone-v2）────────────────
#
# 与塔同样的来源链思路：GitHub release 为主源，国内可切 ModelScope。
# PHOTOS_AUTO_TONE_WEIGHT_SOURCE：
#   auto（默认）= GitHub 优先，下载失败回落 ModelScope 镜像
#   github      = 仅 GitHub release
#   modelscope  = 有镜像的文件走 ModelScope，其余仍走 GitHub
#
# 镜像文件与 GitHub 字节不同时（重存导致）必须分开设 sha——校验按
# 各自来源的钉死值来，镜像被替换即报错。
WEIGHT_SOURCE = os.environ.get("PHOTOS_AUTO_TONE_WEIGHT_SOURCE",
                               "auto").strip().lower()
MODELSCOPE_REPO = os.environ.get("PHOTOS_AUTO_TONE_MODELSCOPE_REPO",
                                 "dwphoto/photo-s-auto-tone-v2")
MODELSCOPE_URL_BASE = (f"https://modelscope.cn/models/{MODELSCOPE_REPO}"
                       f"/resolve/master")

# name → ModelScope 实测 sha256。二进制文件与 GitHub release 字节一致
# （sha 相同）；两个 JSON config 例外——ModelScope 上传时把 CRLF 规范化
# 成 LF（语义等价，差 1 字节），按镜像实测值钉。auto_tone_siglip 重存
# 版差异是 torch 重存编码，非模型差异。
MODELSCOPE_WEIGHTS: Dict[str, str] = {
    # ── 与 GitHub 字节一致 ─────────────────────────────────────────
    "hand_features.npz":
        "de29659dd0a5668f2689fe5d83ead27e1449d9d1d0ae6b1db1d421aa3a616c37",
    "auto_tone_v7_clean.pt":
        "6b41c1f9e43415a19277bf0e2f70ccce6c0da27079c915502990459bef95a15a",
    "clip_train_rag.npz":
        "0c830971c1ea4457758fee2353e63ec9338c428d114cf0eb68a94a89a6d6e580",
    # ── 重存版（weights_only=True 兼容；权重张量与 GitHub 逐位一致）──
    "auto_tone_siglip_h192_d03.pt":
        "31e8c001ded31c413b347a9854c1c3da76cadd0a7088f82d09e26d63ae0d4d32",
    # ── ModelScope CRLF→LF 规范化（语义等价 JSON）─────────────────
    "lora_aesthetic_config.json":
        "6696fc6348b33767fb94802494891689af2672c94c27f8b971bbf6b2da98daa1",
    "lora_advisor_config.json":
        "a2460d9713499a2ecff391fbee68aa728b965cab3caa3a24ccae48b648b6fb52",
    # LoRA safetensors（二进制字节一致；2026-09-02 上传自训练机原件，
    # 回读 sha 复核）——至此全部 8 个权重均有镜像，100% 国内源可用
    "lora_aesthetic.safetensors":
        "571d808074916ddd5e244114bb2fb0084df900ab346815ea81b083a901e9add2",
    "lora_advisor.safetensors":
        "26381130c226b49e73ab589fa53fe6e5791dd03f0ca173813b083e69f7df4b31",
}

# ── 视觉塔（open_clip 的大权重，~1.7-2.6GB）───────────────────────────────
#
# open_clip 默认经 huggingface_hub 从 HF 拉塔（国内常 SSL 失败）。
# resolve_tower_pretrained() 在交给 open_clip 前把 pretrained tag 解析成
# 本地文件：先查 HF hub 缓存（老用户零重复下载），否则按来源链下载到
# modelstore towers/ 目录——复用 ensure() 的断点续传/重试/sha256 校验。
#
# 来源链 PHOTOS_AUTO_TONE_TOWER_SOURCE：
#   auto（默认）= 先 HuggingFace，失败回落 ModelScope 镜像
#   hf          = 仅 HuggingFace
#   modelscope  = 仅 ModelScope（国内推荐，跳过必失败的 HF 尝试）
# 单塔 URL / sha256 可用 PHOTOS_AUTO_TONE_TOWER_URL / _SHA256 整体覆盖。
#
# sha256 为上游文件内容哈希（HF LFS blob 名，本机缓存实测复核）；
# ModelScope 镜像按同一 sha 校验——镜像与上游不一致会在校验处报错，
# 不会静默用上被替换的权重。
TOWERS: Dict[str, dict] = {
    # SigLIP ViT-L/16 384 webli —— auto_tone_with_style / analyze_visual_style
    # sha256 = ModelScope 镜像实测（2.61GB 全量下载校验，2026-09-02）
    "timm/ViT-L-16-SigLIP-384": {
        "filename": "open_clip_pytorch_model.bin",
        "sha256": "0e5943977fd1c6048c056921cc34da37aa0374a8f56ad3b1e111be6ea90aea8d",
        "size": 2_610_158_302,
        "modelscope": "timm/ViT-L-16-SigLIP-384",
    },
    # CLIP ViT-L/14 openai —— v7_clean predictor / RAG / anomaly
    # ⚠️ filename 必须是 open_clip_model.safetensors：sha/size 钉的是该
    # safetensors 的 LFS blob（内容寻址）；HF 的 .bin 是另一份序列化
    # （1710639510 字节，sha 不同，torch 2.6+ weights_only 亦拒收其旧
    # 版 pickle）。safetensors 走 open_clip 的 safetensors 加载器，
    # 无 pickle/weights_only 兼容问题。2026-09-06 全量下载复核。
    "timm/vit_large_patch14_clip_224.openai": {
        "filename": "open_clip_model.safetensors",
        "sha256": "9ce2e8a8ebfff3793d7d375ad6d3c35cb9aebf3de7ace0fc7308accab7cd207e",
        "size": 1_710_517_724,
        "modelscope": "timm/vit_large_patch14_clip_224.openai",
    },
}

_TOWER_CANDIDATE_FILES = ("open_clip_model.safetensors",
                          "open_clip_pytorch_model.bin")


# open_clip 不可用时的已知 (model_name, tag) → repo 兜底——解析/预下载
# 不应要求推理依赖齐全（plugin fetch 场景、CI 测试环境）
_TOWER_TAG_FALLBACK = {
    ("ViT-L-16-SigLIP-384", "webli"): "timm/ViT-L-16-SigLIP-384",
    ("ViT-L-14", "openai"): "timm/vit_large_patch14_clip_224.openai",
}


def _tower_repo(model_name: str, pretrained) -> Optional[str]:
    """(model_name, pretrained tag) → open_clip 配置里的 HF repo id。

    无 hf_hub（直链 URL 型 pretrained）返回 None，调用方原样透传给
    open_clip 维持旧行为。open_clip 不可导入时退回内置兜底表（已知
    两塔），仍未知才透传。
    """
    try:
        from open_clip.pretrained import get_pretrained_cfg
    except ImportError:
        return _TOWER_TAG_FALLBACK.get((model_name, pretrained))
    try:
        cfg = get_pretrained_cfg(model_name, pretrained)
    except Exception:
        return _TOWER_TAG_FALLBACK.get((model_name, pretrained))
    repo = None
    if isinstance(cfg, dict):
        repo = (cfg.get("hf_hub") or "").rstrip("/") or None
    elif isinstance(cfg, (tuple, list)) and cfg and cfg[0]:
        loc = str(cfg[0])
        repo = None if loc.startswith(("http://", "https://")) else loc
    return repo or _TOWER_TAG_FALLBACK.get((model_name, pretrained))


def _hf_cache_hit(repo: str) -> Optional[str]:
    """HF hub 本地缓存已命中 → 直接用（完整性由 HF blob sha 布局保证）。"""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    for fn in _TOWER_CANDIDATE_FILES:
        try:
            p = try_to_load_from_cache(repo_id=repo, filename=fn)
        except Exception:
            p = None
        if p and os.path.isfile(p):
            return p
    return None


def _tower_spec(repo: str, source: str):
    """构建塔的 WeightSpec（modelstore 缓存名 towers/<repo>/<filename>）。"""
    from photo_s.modelstore import WeightSpec

    meta = TOWERS[repo]
    fn = meta["filename"]
    if source == "modelscope":
        url = (f"https://modelscope.cn/models/{meta['modelscope']}"
               f"/resolve/master/{fn}")
    else:
        url = f"https://huggingface.co/{repo}/resolve/main/{fn}"
    url = os.environ.get("PHOTOS_AUTO_TONE_TOWER_URL", url)
    sha = os.environ.get("PHOTOS_AUTO_TONE_TOWER_SHA256", meta["sha256"])
    return WeightSpec(
        name=f"towers/{repo.replace('/', '__')}/{fn}",
        url=url, sha256=sha, size=meta["size"])


def resolve_tower_pretrained(model_name: str, pretrained):
    """把 open_clip 的 pretrained tag 解析成本地塔文件路径（可下载）。

    返回值直接作 create_model_and_transforms 的 pretrained 参数：
    本地文件路径、或原样透传的 URL/未知 tag。已有 HF 缓存优先复用。
    """
    if (not isinstance(pretrained, str) or not pretrained
            or os.path.exists(pretrained)
            or pretrained.startswith(("http://", "https://", "file://"))):
        return pretrained

    repo = _tower_repo(model_name, pretrained)
    if not repo or repo not in TOWERS:
        # 未登记的塔：维持 open_clip 自身下载行为
        return pretrained

    cached = _hf_cache_hit(repo)
    if cached:
        return cached

    source = os.environ.get("PHOTOS_AUTO_TONE_TOWER_SOURCE",
                            "auto").strip().lower()
    chain = {"hf": ["hf"], "modelscope": ["modelscope"]}.get(
        source, ["hf", "modelscope"])

    from photo_s.modelstore import ensure

    errors = []
    for src in chain:
        try:
            return ensure(_tower_spec(repo, src))
        except RuntimeError as e:
            errors.append(f"{src}: {e}")
    raise RuntimeError(
        "auto-tone 视觉塔下载失败 ({})。可设 PHOTOS_AUTO_TONE_TOWER_SOURCE="
        "modelscope 走国内镜像，或 PHOTOS_AUTO_TONE_TOWER_URL 指向自备文件"
        "（sha256 需匹配）。详情: {}".format(repo, "; ".join(errors)))

# name → (sha256, size, required)
WEIGHTS: Dict[str, dict] = {
    # ── 核心（auto_tone 主流程必需）────────────────────────────
    "auto_tone_v7_clean.pt": {
        "sha256": "6b41c1f9e43415a19277bf0e2f70ccce6c0da27079c915502990459bef95a15a",
        "size": 452_309,
        "required": True,
    },
    "clip_train_rag.npz": {
        "sha256": "0c830971c1ea4457758fee2353e63ec9338c428d114cf0eb68a94a89a6d6e580",
        "size": 3_713_369,
        "required": True,
    },
    "hand_features.npz": {
        # v0.1.0 起此值多打了一个字符（db21d4→db1d4），导致线上资产校验
        # 必败——2026-08-28 以 GitHub release 资产实测 sha 修正
        "sha256": "de29659dd0a5668f2689fe5d83ead27e1449d9d1d0ae6b1db1d421aa3a616c37",
        "size": 593_590,
        "required": True,
    },
    # ── v2.1 风格化（auto_tone_with_style / analyze_visual_style）──
    # SigLIP+MLP h192 d=0.3（PSNR 32.21，比 v7_clean 高 2.93 dB）。
    # 重存为纯 tensor/Python 类型，兼容 torch.load(weights_only=True)。
    "auto_tone_siglip_h192_d03.pt": {
        "sha256": "d64d2ea67cc725ffb61663c50239e1d9be95c6a559abaed37aedfcb0d2b68c92",
        "size": 871_277,
        "required": False,
        "url_base": STYLE_URL_BASE,
    },
    # ── 可选（Qwen3-VL LoRA，仅 aesthetic / advisor 需要）──────
    "lora_aesthetic.safetensors": {
        "sha256": "571d808074916ddd5e244114bb2fb0084df900ab346815ea81b083a901e9add2",
        "size": 139_518_856,
        "required": False,
    },
    "lora_aesthetic_config.json": {
        "sha256": "a8cd78bdac08d501f54806dc82acc0c8daf35a501a3bacb4aadc082825db4071",
        "size": 1_156,
        "required": False,
    },
    "lora_advisor.safetensors": {
        "sha256": "26381130c226b49e73ab589fa53fe6e5791dd03f0ca173813b083e69f7df4b31",
        "size": 278_979_768,
        "required": False,
    },
    "lora_advisor_config.json": {
        "sha256": "a9e945d0c0a03fe7d964c666f681cbf6853a2cdbda1b29496bb9c4acf6979655",
        "size": 1_157,
        "required": False,
    },
}


def _env_suffix(name: str) -> str:
    return name.upper().replace(".", "_").replace("-", "_")


def weight_specs(names: Optional[List[str]] = None) -> "List":
    """构建 WeightSpec 列表（GitHub 源 + 环境变量覆盖；ModelScope 镜像
    见 :func:`ensure_weight` 的来源链）。"""
    from photo_s.modelstore import WeightSpec

    # PHOTOS_AUTO_TONE_URL_BASE 覆盖所有条目的前缀（离线/镜像/测试），
    # 优先级高于条目各自的 url_base；未覆盖时 v2.1 条目用 STYLE_URL_BASE
    env_base = os.environ.get("PHOTOS_AUTO_TONE_URL_BASE")
    base = env_base or URL_BASE
    specs = []
    for name, meta in WEIGHTS.items():
        if names is not None and name not in names:
            continue
        entry_base = env_base or meta.get("url_base", base)
        url = os.environ.get(
            f"PHOTOS_AUTO_TONE_{_env_suffix(name)}_URL",
            f"{entry_base}/{name}")
        sha = os.environ.get(f"PHOTOS_AUTO_TONE_{_env_suffix(name)}_SHA256",
                             meta["sha256"])
        specs.append(WeightSpec(name=name, url=url, sha256=sha, size=meta["size"]))
    return specs


def _modelscope_spec(name: str):
    """name 的 ModelScope 镜像 WeightSpec（无镜像返回 None）。"""
    from photo_s.modelstore import WeightSpec

    ms_sha = MODELSCOPE_WEIGHTS.get(name)
    if not ms_sha:
        return None
    url = os.environ.get(f"PHOTOS_AUTO_TONE_{_env_suffix(name)}_MS_URL",
                         f"{MODELSCOPE_URL_BASE}/{name}")
    return WeightSpec(name=name, url=url, sha256=ms_sha,
                      size=WEIGHTS[name]["size"])


def ensure_weight(name: str) -> str:
    """单权重解析：按来源链（github ↔ modelscope 镜像）下载校验。

    每次使用都重新校验（cached_path 命中才返回——被篡改/过期的缓存
    不能绕过 sha256 闸门）。全部来源失败时报含尝试明细的错误。
    """
    from photo_s.modelstore import cached_path, ensure

    specs = weight_specs([name])
    if not specs:
        raise KeyError(f"unknown weight: {name}")
    gh_spec = specs[0]
    ms_spec = _modelscope_spec(name)

    if WEIGHT_SOURCE == "github":
        chain = [gh_spec]
    elif WEIGHT_SOURCE == "modelscope":
        chain = ([ms_spec, gh_spec] if ms_spec else [gh_spec])
    else:  # auto
        chain = ([gh_spec, ms_spec] if ms_spec else [gh_spec])

    for spec in chain:  # 已缓存且校验通过 → 零网络
        if cached_path(spec):
            return cached_path(spec)
    errors = []
    for spec in chain:
        try:
            return ensure(spec)
        except RuntimeError as e:
            errors.append(f"{spec.url}: {e}")
    raise RuntimeError(
        f"权重 {name} 下载失败（来源链 {[s.url for s in chain]}）："
        + "; ".join(errors))


def ensure_core() -> Dict[str, str]:
    """下载（如需）核心权重，返回 {name: 本地路径}。"""
    paths = {}
    for name, meta in WEIGHTS.items():
        if meta["required"]:
            paths[name] = ensure_weight(name)
    return paths


def core_path(name: str) -> str:
    """单个核心权重的本地路径（不存在则下载；来源链含 ModelScope）。"""
    return ensure_weight(name)


_LORA_PREFIX = {"aesthetic": "lora_aesthetic", "advisor": "lora_advisor"}
_lora_lock = threading.Lock()
_lora_dirs: Dict[str, str] = {}


def ensure_lora_dir(kind: str) -> str:
    """组装 PEFT 需要的 LoRA 目录（adapter_config.json + adapter_model.safetensors）。

    modelstore 缓存是平铺文件，PEFT 需要目录布局，这里在缓存目录下建
    lora_<kind>/ 并把两个文件链接/复制进去。
    """
    if kind not in _LORA_PREFIX:
        raise ValueError(f"kind must be one of {sorted(_LORA_PREFIX)}")
    with _lora_lock:
        if kind in _lora_dirs:
            return _lora_dirs[kind]

        from photo_s.modelstore import cache_dir

        prefix = _LORA_PREFIX[kind]
        safetensors = ensure_weight(f"{prefix}.safetensors")
        config = ensure_weight(f"{prefix}_config.json")

        lora_dir = os.path.join(cache_dir(), f"lora_{kind}")
        os.makedirs(lora_dir, exist_ok=True)
        dst_w = os.path.join(lora_dir, "adapter_model.safetensors")
        dst_c = os.path.join(lora_dir, "adapter_config.json")
        _link_or_copy(safetensors, dst_w)
        _link_or_copy(config, dst_c)
        _lora_dirs[kind] = lora_dir
        return lora_dir


def _link_or_copy(src: str, dst: str) -> None:
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def pick_device() -> str:
    """cuda → mps → cpu 自动选择（保持多平台可用）。

    torch 未安装时返回 "cpu" 而非抛错——设备选择不应早于"真正要跑
    推理"失败（如权重预取、verifier 可用性探测等路径）。
    """
    try:
        import torch
    except ImportError:
        return "cpu"

    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ── SigLIP tokenizer（transformers 布局的本地目录）────────────────────────
# style 路径要给 16 风格文案做文本编码：AutoTokenizer 默认从 HF 在线拉，
# 国内失败后视觉分析被静默禁用（top_styles 为空）。TOWER_SOURCE=modelscope
# 时改为直接用 ModelScope 镜像（timm 仓自带全套 tokenizer 文件）组装
# 本地目录；auto 模式下作 HF 失败后的回落。sha 为镜像实测（2026-09-02）。
SIGLIP_TOKENIZER_FILES: Dict[str, str] = {
    "tokenizer.json":
        "83051c8005acc696637fe0c62c711ecee4b59083b4cf07ff9ad5f637eb2a3d2a",
    "tokenizer_config.json":
        "8571e0cf70f7ae095c5c544ab94b7967e94c79262202d61604da10cbe426cecb",
    "special_tokens_map.json":
        "3a60d3bb0808e7e629845031c2d720d33c2aceee1a6c535255de15d45b9f1ac7",
}
SIGLIP_TOKENIZER_MS_REPO = "timm/ViT-L-16-SigLIP-384"


def ensure_siglip_tokenizer_dir() -> Optional[str]:
    """tokenizer 本地目录（modelstore tokenizers/ViT-L-16-SigLIP-384/）。

    TOWER_SOURCE=github 时返回 None（保持 HF 在线行为）；modelscope/auto
    时下载缺失文件（sha 钉死）。目录不完整且下载失败 → None（调用方回落
    原行为，不炸主流程）。
    """
    if os.environ.get("PHOTOS_AUTO_TONE_TOWER_SOURCE", "auto").strip().lower() \
            == "github":
        return None
    from photo_s.modelstore import cache_dir

    directory = os.path.join(cache_dir(), "tokenizers", "ViT-L-16-SigLIP-384")
    base = (f"https://modelscope.cn/models/{SIGLIP_TOKENIZER_MS_REPO}"
            f"/resolve/master")
    for fn, sha in SIGLIP_TOKENIZER_FILES.items():
        dst = os.path.join(directory, fn)
        if os.path.isfile(dst):
            continue
        import hashlib
        import urllib.request

        tmp = dst + f".{os.getpid()}.part"
        try:
            os.makedirs(directory, exist_ok=True)
            req = urllib.request.Request(
                f"{base}/{fn}",
                headers={"User-Agent": "photo-s-auto-tone-tokenizer"})
            with urllib.request.urlopen(req, timeout=30) as r, \
                    open(tmp, "wb") as f:
                digest = hashlib.sha256()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest().lower() != sha:
                raise RuntimeError(f"sha256 mismatch for tokenizer {fn}")
            os.replace(tmp, dst)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return None  # 目录不完整：宁可回落 HF/禁用，不半套启动
    return directory


_shared_clip_lock = threading.Lock()
_shared_clip: Dict[str, tuple] = {}


def get_shared_clip(model_name: str, pretrained, device: str):
    """进程内共享的 CLIP 模型（predictor / rag / anomaly 复用同一份权重）。

    返回 (model, preprocess)。塔权重经 resolve_tower_pretrained 解析
    （HF 缓存命中 / modelstore 下载 + ModelScope 国内回落）。
    """
    with _shared_clip_lock:
        key = f"{model_name}|{pretrained}|{device}"
        if key not in _shared_clip:
            import open_clip

            model, _, preprocess = open_clip.create_model_and_transforms(
                model_name,
                pretrained=resolve_tower_pretrained(model_name, pretrained),
                device=device)
            model.eval()
            for p in model.parameters():
                p.requires_grad_(False)
            _shared_clip[key] = (model, preprocess)
        return _shared_clip[key]
