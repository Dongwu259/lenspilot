"""photo_s/search.py — 语义图像搜索与自动打标（v2.5）

``photo-s index DIR`` 建嵌入索引 → ``photo-s find "日落 海边"``（文本）或
``--image ref.jpg``（以图搜图）余弦排序；``index --tags a,b`` 自动打标——
标签文本嵌入 × 图像嵌入，过阈值即写 EXIF keywords（可选 XMP dc:subject，
LR 关键词面板可见）。

特征抽取在 ``embed`` provider 槽位后面（同 lut/denoise/auto_tone/verify 的
发现机制）：

- **内置 ``hist84``**：lrxmp 84 维直方图特征，零依赖纯图像——无插件时的
  底座，只支持以图搜图；
- **auto-tone 插件 ``siglip:ViT-L-16-SigLIP-384``**：SigLIP 塔（与风格化/
  verifier 共用同一塔注册表 + ModelScope 国内链，零新增下载），文本 + 图像
  双编码——自然语言搜索（SigLIP webli 语料以英文为主，
  中文查询不保证排序质量，建议英文关键词）。

索引 = npz（默认 ``<root>/.photo-s-index.npz``）：extractor 名 + 维度 +
相对路径 + mtime/size（增量重排）+ L2 归一特征 + 已打标签。查询严格使用
建索引时的抽取器（索引记录名字）：抽取器变了就报错要求重建，不静默混用
两种空间。
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .engine import ALL_INPUT_EXTENSIONS

__all__ = ["INDEX_FILENAME", "HIST_NAME", "get_extractor", "load_index",
           "build_index", "find_similar", "auto_tag"]

INDEX_FILENAME = ".photo-s-index.npz"
HIST_NAME = "hist84"


def _probe_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


def _normalize(rows: np.ndarray) -> np.ndarray:
    rows = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return rows / norms


class _HistExtractor:
    """内置 84 维直方图特征（lrxmp.content_features 截掉截距列）。

    纯图像——embed_texts 返回 None（文本搜索需 embed 插件，find 给安装
    指引，不静默降级）。
    """

    name = HIST_NAME
    dim = 84

    def embed_images(self, paths: Sequence[str]) -> np.ndarray:
        from PIL import Image
        from .lrxmp import content_features
        rows = []
        for p in paths:
            with Image.open(p) as im:
                rows.append(content_features(im)[:-1])
        return _normalize(np.asarray(rows, dtype=np.float32))

    def embed_texts(self, texts: Sequence[str]) -> Optional[np.ndarray]:
        return None


class _PluginExtractor:
    """embed provider（auto-tone 插件 SigLIP）的适配层。

    provider 约定：``embed_images(paths) -> (N, D)``、可选
    ``embed_texts(texts) -> (M, D)``（均为未归一数值即可，这里统一 L2）；
    ``embed_name`` / ``embed_dim`` 可省（省略时探测）。
    """

    def __init__(self, provider: Any):
        self.provider = provider
        self.name = str(getattr(provider, "embed_name", "embed"))

    @property
    def dim(self) -> int:
        d = getattr(self.provider, "embed_dim", None)
        if d:
            return int(d)
        # 未声明维度：借一次 8×8 探针（塔加载一次，provider 内共享缓存）
        import tempfile as _tf
        fd, tmp = _tf.mkstemp(suffix=".png")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(_probe_bytes())
            d = int(self.embed_images([tmp]).shape[-1])
            self.provider.embed_dim = d
            return d
        finally:
            os.unlink(tmp)

    def embed_images(self, paths: Sequence[str]) -> np.ndarray:
        return _normalize(self.provider.embed_images(list(paths)))

    def embed_texts(self, texts: Sequence[str]) -> Optional[np.ndarray]:
        fn = getattr(self.provider, "embed_texts", None)
        if fn is None:
            return None
        return _normalize(fn(list(texts)))


def get_extractor(name: Optional[str] = None):
    """解析特征抽取器：``name=None`` 自动（embed 插件优先，回落 hist84）。

    指定名字时严格匹配——索引里记录的抽取器必须在场，否则报错要求重建，
    绝不把两种嵌入空间混在一个索引里。
    """
    if name == HIST_NAME:
        return _HistExtractor()
    from .plugin import find_provider
    provider = find_provider("embed")
    if provider is not None and hasattr(provider, "embed_images"):
        pname = str(getattr(provider, "embed_name", "embed"))
        if name is None or pname == name:
            return _PluginExtractor(provider)
        raise RuntimeError(
            f"index was built with extractor {name!r} but the installed "
            f"embed provider is {pname!r} — rebuild the index "
            f"('photo-s index ... --rebuild')")
    if name is None:
        return _HistExtractor()
    raise RuntimeError(
        f"index extractor {name!r} needs the auto-tone plugin's embed "
        f"provider (pip install photo-s-plugin-auto-tone); or rebuild the "
        f"index with the built-in extractor ({HIST_NAME})")


# ---------------------------------------------------------------- 索引 I/O

def load_index(index_path: str) -> Optional[Dict[str, Any]]:
    """读索引 → dict（extractor/dim/root/paths/mtimes/sizes/feats/tags）；
    缺失返回 None，损坏抛 RuntimeError（要求 --rebuild）。"""
    if not os.path.exists(index_path):
        return None
    try:
        with np.load(index_path, allow_pickle=False) as z:
            idx = {
                "extractor": str(z["extractor"]),
                "dim": int(z["dim"]),
                "root": str(z["root"]),
                "paths": [str(p) for p in z["paths"].tolist()],
                "mtimes": z["mtimes"].tolist(),
                "sizes": z["sizes"].tolist(),
                "feats": z["feats"],
                "tags": ([str(t) for t in z["tags"].tolist()]
                         if "tags" in z.files else []),
            }
    except Exception as e:
        raise RuntimeError(
            f"index unreadable: {index_path} ({type(e).__name__}: {e}) — "
            f"rebuild with 'photo-s index ... --rebuild'") from e
    if not idx["tags"]:
        idx["tags"] = [""] * len(idx["paths"])
    return idx


def _save_index(index_path: str, idx: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(index_path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".index-", suffix=".npz")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f, extractor=idx["extractor"], dim=idx["dim"],
                root=idx["root"], paths=np.array(idx["paths"]),
                mtimes=np.asarray(idx["mtimes"], dtype=np.int64),
                sizes=np.asarray(idx["sizes"], dtype=np.int64),
                feats=np.asarray(idx["feats"], dtype=np.float32),
                tags=np.array(idx.get("tags") or [""] * len(idx["paths"])))
        os.replace(tmp, index_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _walk_images(paths: Sequence[str], recursive: bool) -> List[str]:
    out: List[str] = []
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.isfile(ap):
            if os.path.splitext(ap)[1].lower() in ALL_INPUT_EXTENSIONS:
                out.append(ap)
        elif os.path.isdir(ap):
            if recursive:
                for root, _dirs, files in os.walk(ap):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in ALL_INPUT_EXTENSIONS:
                            out.append(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(ap)):
                    fp = os.path.join(ap, f)
                    if os.path.isfile(fp) and \
                            os.path.splitext(f)[1].lower() in ALL_INPUT_EXTENSIONS:
                        out.append(fp)
    return sorted(set(out))


def default_index_path(paths: Sequence[str]) -> str:
    """默认索引位置 = 公共根目录下的 ``.photo-s-index.npz``。"""
    aps = [os.path.abspath(p) for p in paths] or [os.getcwd()]
    root = os.path.commonpath(aps)
    return os.path.join(root, INDEX_FILENAME)


def build_index(paths: Sequence[str], *, recursive: bool = False,
                index_path: Optional[str] = None,
                rebuild: bool = False,
                extractor_name: Optional[str] = None,
                batch_size: int = 32) -> Dict[str, Any]:
    """建/增量更新索引。mtime+size 未变且抽取器一致的行保留；消失的剔除。

    返回 {index, root, extractor, dim, total, indexed, kept, removed}。
    """
    files = _walk_images(paths, recursive)
    if not files:
        raise RuntimeError(
            "no supported image files found under the given paths")
    index_path = index_path or default_index_path(paths)
    root = os.path.dirname(index_path)
    ext = get_extractor(extractor_name)

    old = None if rebuild else load_index(index_path)
    rels = [os.path.relpath(f, root) for f in files]
    mtimes = [int(os.path.getmtime(f)) for f in files]
    sizes = [int(os.path.getsize(f)) for f in files]

    old_rel_pos: Dict[str, int] = {}
    old_tags: List[str] = []
    if old and old["extractor"] == ext.name and old["dim"] == ext.dim:
        old_rel_pos = {r: i for i, r in enumerate(old["paths"])}
        old_tags = old.get("tags") or [""] * len(old["paths"])

    keep: List[int] = []
    todo: List[int] = []
    for i, rel in enumerate(rels):
        j = old_rel_pos.get(rel)
        if j is not None and old["mtimes"][j] == mtimes[i] \
                and old["sizes"][j] == sizes[i]:
            keep.append(i)
        else:
            todo.append(i)
    removed = 0
    if old:
        kept_rels = {rels[i] for i in keep}
        removed = sum(1 for r in old["paths"] if r not in kept_rels)

    feats = np.zeros((len(files), ext.dim), dtype=np.float32)
    tags: List[str] = [""] * len(files)
    for i in keep:
        j = old_rel_pos[rels[i]]
        feats[i] = old["feats"][j]
        tags[i] = old_tags[j] if j < len(old_tags) else ""
    for start in range(0, len(todo), max(1, int(batch_size))):
        chunk = todo[start:start + max(1, int(batch_size))]
        got = ext.embed_images([files[i] for i in chunk])
        if got.shape[0] != len(chunk) or got.shape[-1] != ext.dim:
            raise RuntimeError(
                f"extractor returned {got.shape} for {len(chunk)} images "
                f"(expected (*, {ext.dim}))")
        for off, i in enumerate(chunk):
            feats[i] = got[off]

    idx = {"extractor": ext.name, "dim": int(feats.shape[-1]), "root": root,
           "paths": rels, "mtimes": mtimes, "sizes": sizes, "feats": feats,
           "tags": tags}
    _save_index(index_path, idx)
    return {"index": index_path, "root": root, "extractor": ext.name,
            "dim": idx["dim"], "total": len(files), "indexed": len(todo),
            "kept": len(keep), "removed": removed}


# ---------------------------------------------------------------- 查询 / 打标

def find_similar(index_path: str, *, text: Optional[str] = None,
                 image: Optional[str] = None, k: int = 10,
                 min_score: Optional[float] = None) -> Dict[str, Any]:
    """文本 / 以图搜图。返回 {index, extractor, query, hits: [{path, score}]}。

    文本查询无文本编码器（内置 hist84）→ RuntimeError 安装指引；
    查询图恰为索引内图像时自身以 score≈1.0 返回，取舍由调用方决定。
    """
    idx = load_index(index_path)
    if idx is None:
        raise RuntimeError(
            f"index not found: {index_path} — run 'photo-s index <dir>' first")
    if text and image:
        raise RuntimeError("pass either text or image, not both")
    ext = get_extractor(idx["extractor"])
    if text:
        q = ext.embed_texts([text])
        if q is None:
            raise RuntimeError(
                f"text search needs a text encoder — index extractor "
                f"{idx['extractor']!r} is image-only; "
                f"pip install photo-s-plugin-auto-tone, or search by --image")
        q = q[0]
    elif image:
        q = ext.embed_images([os.path.abspath(image)])[0]
    else:
        raise RuntimeError("a text query or --image is required")

    feats = np.asarray(idx["feats"], dtype=np.float32)
    scores = feats @ q.astype(np.float32)
    order = np.argsort(-scores)[:max(1, int(k))]
    hits: List[Dict[str, Any]] = []
    for i in order:
        score = float(scores[i])
        if min_score is not None and score < float(min_score):
            continue
        hits.append({"path": os.path.join(idx["root"], idx["paths"][i]),
                     "score": round(score, 4)})
    return {"index": index_path, "extractor": idx["extractor"],
            "query": text or image, "hits": hits}


def auto_tag(index_path: str, tags: Sequence[str], *,
             min_score: float = 0.2, max_tags: int = 5,
             write_xmp: bool = False) -> Dict[str, Any]:
    """对既有索引自动打标：标签文本嵌入 × 图像嵌入 → 阈值内 top-N →
    EXIF keywords（apply_exif_tags 已有通道）+ 可选 XMP dc:subject。

    返回 {index, assigned: {path: [tags]}, tagged, untouched}；标签写回
    索引（增量重建不重算）。
    """
    idx = load_index(index_path)
    if idx is None:
        raise RuntimeError(
            f"index not found: {index_path} — run 'photo-s index <dir>' first")
    tags = [str(t).strip() for t in tags if str(t).strip()]
    if not tags:
        raise RuntimeError("tags list is empty")
    ext = get_extractor(idx["extractor"])
    tag_feats = ext.embed_texts(tags)
    if tag_feats is None:
        raise RuntimeError(
            f"auto-tagging needs a text encoder — index extractor "
            f"{idx['extractor']!r} is image-only; "
            f"pip install photo-s-plugin-auto-tone")

    from .engine import apply_exif_tags
    sims = np.asarray(idx["feats"], dtype=np.float32) @ tag_feats.T
    assigned: Dict[str, List[str]] = {}
    new_tags: List[str] = list(idx.get("tags") or [])
    while len(new_tags) < len(idx["paths"]):
        new_tags.append("")
    for i, rel in enumerate(idx["paths"]):
        order = np.argsort(-sims[i])[:max(1, int(max_tags))]
        chosen = [tags[j] for j in order if sims[i][j] >= float(min_score)]
        path = os.path.join(idx["root"], rel)
        if chosen:
            # EXIF UserComment 协议按空白切分——多词标签下划线连接；
            # XMP dc:subject 保留原文（LR 关键词面板所见即索引所记）
            exif_kw = ",".join(t.replace(" ", "_") for t in chosen)
            apply_exif_tags(path, {"keywords": exif_kw})
            if write_xmp:
                from .lrxmp import write_xmp_sidecar
                write_xmp_sidecar(path, None, keywords=chosen)
            assigned[path] = chosen
            new_tags[i] = ",".join(chosen)
        else:
            new_tags[i] = ""
    idx["tags"] = new_tags
    _save_index(index_path, idx)
    return {"index": index_path, "assigned": assigned,
            "tagged": len(assigned),
            "untouched": len(idx["paths"]) - len(assigned)}
