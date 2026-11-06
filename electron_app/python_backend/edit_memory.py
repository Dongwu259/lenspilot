"""edit_memory.py — JitRL 式个性化情景记忆（training-free，桌面本地）

论文机制（arXiv 2601.18510）的修图域落地，候选级重排路径：
  记忆条目 = (照片统计 s, 参数决策 a, 结果 G)     ← edit_journal 思想的参数化版
  检索：kNN（photo_stats 特征，无量纲距离）→ 相似经验
  优势：Ã(cand) = 相似度加权的负参数距离（KL 约束改进的检索估计）
  重排：score = β·Ã(cand)（先验 logprob 钩子预留，v1 不混刻度）
  回退：经验不足 / 相似度低 → fallback=True，调用方退回基础模型

存储与 edit_journal 同模式：append-only JSONL、8MB 轮转、坏行跳过、
写入 best-effort 绝不影响编辑主流程。β 从小起步；KL 形式天然限漂移。
"""

import json
import os
import threading
import time

import numpy as np

from photo_stats import HUE_BANDS, feature_vector, stats_distance
from service import send_log
from utils import get_user_data_dir

_LOCK = threading.Lock()
_PATH = os.path.join(get_user_data_dir(), "edit_memory.jsonl")
_MAX_BYTES = 8 * 1024 * 1024

_MIN_ENTRIES = 5     # 少于这个数不检索（fallback）
_MIN_SIM = 0.35      # 平均相似度低于此值 → fallback
_MAX_FIELD_DIST = 3.0  # 单字段距离截断（典型单位）
ACCEPT_TOL = 0.35    # 落定判"采纳"：平均参数距离 < 35% 典型步长（settle 与 stats 共用口径）

# 参与检索/重排的可调字段：(取值路径, 典型单位)。hsl 取第 1 分量 (ds)。
ACTION_FIELDS = [
    ("exposure", 0.3), ("contrast", 0.15), ("saturation", 0.15),
    ("warmth", 0.2), ("shadows", 0.3), ("highlights", 0.3), ("vibrance", 0.3),
    ("wb_temp", 300), ("wb_tint", 12),
    ("clarity", 0.3), ("texture", 0.3), ("dehaze", 0.3),
] + [("hsl.%s.1" % b, 0.2) for b in HUE_BANDS]

# 同批距离折扣（E3: λ=0.5 全指标最优——批内经验更可信）
BATCH_DISCOUNT = 0.5


def _rotate_if_needed():
    try:
        if os.path.exists(_PATH) and os.path.getsize(_PATH) > _MAX_BYTES:
            os.replace(_PATH, _PATH + ".1")
    except OSError:
        pass


def record(kind, stats, params, path=None, suggested=None, weight=1.0):
    """追加一条经验。kind: edit(手动/微调落盘) | accept(建议原样采纳) | reject(拒绝)。
    best-effort：任何失败只告警，绝不影响编辑主流程。"""
    try:
        rec = {
            "ts": int(time.time()),
            "kind": str(kind)[:16],
            "stats": stats,
            "params": params or {},
            "weight": max(0.0, min(5.0, float(weight or 1.0))),
        }
        if path:
            rec["path"] = str(path)
            rec["batch"] = os.path.dirname(str(path))  # 批 = 照片所在目录（同批≈同场拍摄）
        if suggested is not None:
            rec["suggested"] = suggested
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            _rotate_if_needed()
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:
        try:
            send_log(f"edit_memory record failed: {e}", "warn")
        except Exception:
            pass
        return False


def load():
    """全部经验（含 .1 轮转文件，时间正序）。坏行跳过。"""
    out = []
    try:
        for fp in (_PATH + ".1", _PATH):
            if not os.path.isfile(fp):
                continue
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("stats") is not None or rec.get("params") is not None:
                        out.append(rec)  # 无 stats 的条目保留审计（检索时自动跳过）
    except Exception as e:
        try:
            send_log(f"edit_memory load failed: {e}", "warn")
        except Exception:
            pass
    return out


def enabled():
    """总开关（app_config.edit_memory_enabled，缺省开）。关 = 不建议快照、不检索重排、不落定。

    P2-6 隐私出口：关闭后 photo_editor 两处接线完全旁路（写侧也停，
    已有记忆保留在本地文件里，可随时清除）。
    """
    try:
        import service
        cfg = getattr(service, "_app_config", None) or {}
        return bool(cfg.get("edit_memory_enabled", True))
    except Exception:
        return True


def latest_suggested(path, entries=None):
    """该照片最近一条 suggested 条目（无则 None）。save 端复用其 stats 快照，
    免去同图二次解码+统计（P2-4）；entries 可由调用方预读传入（P2-6b 单次 load）。"""
    try:
        entries = load() if entries is None else entries
        for e in reversed(entries):
            if e.get("kind") == "suggested" and e.get("path") == str(path):
                return e
    except Exception:
        pass
    return None


def clear(batch=None):
    """清除记忆：batch=照片目录 → 只清该批（batch 字段 normcase 匹配）；
    None → 全清（含 .1 轮转文件）。返回删除条数，失败返回 -1。"""
    try:
        entries = load()
        with _LOCK:
            if batch is None:
                keep, removed = [], len(entries)
            else:
                key = os.path.normcase(str(batch))
                keep = [e for e in entries
                        if os.path.normcase(str(e.get("batch") or "")) != key]
                removed = len(entries) - len(keep)
            tmp = _PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for e in keep:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, _PATH)
            if os.path.exists(_PATH + ".1"):
                os.remove(_PATH + ".1")
        return removed
    except Exception as e:
        try:
            send_log(f"edit_memory clear failed: {e}", "warn")
        except Exception:
            pass
        return -1


def _get_param(params, field):
    """按 ACTION_FIELDS 的点路径取值；wb_temp 0(=关) 视作中性 5500。"""
    v = params
    for part in field.split("."):
        if isinstance(v, (list, tuple)):
            v = v[int(part)] if int(part) < len(v) else 0
        else:
            v = (v or {}).get(part, 0)
    if field == "wb_temp" and (not v or v <= 0):
        return 5500.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _neighbors(stats, entries, k=12, batch=None):
    """按 photo_stats 特征距离取最近 k 条可用经验 → [(entry, sim)]。
    同批（目录相同）距离打 BATCH_DISCOUNT 折——E3: 批内经验更可信。"""
    cand = []
    target = np.array(feature_vector(stats))
    for e in entries:
        if e.get("kind") not in ("edit", "accept") or not e.get("params"):
            continue
        try:
            vec = np.array(feature_vector(e["stats"]))
        except (KeyError, TypeError, ValueError):
            continue
        dist = float(np.sqrt(((vec - target) ** 2).sum()))
        if batch and e.get("batch") and e["batch"] == batch:
            dist *= (1.0 - BATCH_DISCOUNT)
        cand.append((dist, e))
    cand.sort(key=lambda t: t[0])
    out = []
    for dist, e in cand[:max(1, int(k or 12))]:
        out.append((e, 1.0 / (1.0 + dist)))
    return out


def _field_distance(a, b, unit):
    return min(abs(a - b) / unit, _MAX_FIELD_DIST)


def memory_median(stats, k=12, entries=None, batch=None):
    """检索 + 逐字段加权中位数 → (params 或 None, confidence 0..1)。

    confidence 综合经验数量与平均相似度；低于阈值返回 None（调用方回退）。
    """
    entries = load() if entries is None else entries
    nbrs = _neighbors(stats, entries, k, batch)
    if len(nbrs) < _MIN_ENTRIES:
        return None, 0.0
    sims = [s for _, s in nbrs]
    conf = float(np.mean(sims))
    if conf < _MIN_SIM:
        return None, conf
    out = {}
    for field, unit in ACTION_FIELDS:
        vals = []
        for e, s in nbrs:
            w = s * float(e.get("weight", 1.0))
            vals.append((_get_param(e["params"], field), w))
        vals.sort(key=lambda t: t[0])
        total = sum(w for _, w in vals)
        if total <= 0:
            continue
        acc, med = 0.0, vals[0][0]
        for v, w in vals:
            acc += w
            if acc >= total / 2.0:
                med = v
                break
        out[field] = med
    return _to_edits(out), conf


def advantage(candidate, nbrs):
    """Ã(cand)：相似度加权的负参数距离（0 = 与经验完全一致，越负越偏）。"""
    if not nbrs:
        return 0.0
    total = 0.0
    wsum = 0.0
    for e, s in nbrs:
        w = s * float(e.get("weight", 1.0))
        d = 0.0
        for field, unit in ACTION_FIELDS:
            d += _field_distance(_get_param(candidate, field),
                                 _get_param(e["params"], field), unit)
        total += -d * w
        wsum += w
    return total / max(wsum, 1e-9)


def suggest(path, stats, params):
    """AI 建议快照：数据飞轮第一步（保存时 settle 闭环）。"""
    return record("suggested", stats, params, path=path)


def settle(path, params, stats=None, entries=None):
    """保存落定：找该照片最近一条 suggested，追加 edit 经验（含 accepted 判定）。
    返回 (ok, accepted)。stats 缺省沿用建议时的快照（原图不变）；entries 可由
    批量调用方预读一次传入（P2-5，免每张全文件重读）。"""
    try:
        entries = load() if entries is None else entries
    except Exception:
        entries = []
    sug = None
    for e in reversed(entries):
        if e.get("kind") == "suggested" and e.get("path") == str(path):
            sug = e
            break
    accepted = False
    if sug:
        tot = sum(_field_distance(_get_param(params or {}, f),
                                  _get_param(sug.get("params") or {}, f), u)
                  for f, u in ACTION_FIELDS) / len(ACTION_FIELDS)
        accepted = tot < ACCEPT_TOL
    ok = record("edit", stats or (sug or {}).get("stats"), params,
                path=path, suggested=(sug or {}).get("params"))
    return ok, accepted


def stats_summary():
    """北极星指标（只读）：建议数 / 落定配对数 / 采纳数 / 采纳率 + kind 分布。

    采纳口径与 settle 完全一致：edit 条目携带 suggested 时，重算平均参数
    距离 < ACCEPT_TOL 即计采纳（同一次建议多次保存各计一次落定配对）。
    """
    entries = load()
    kinds = {}
    suggested = paired = accepted = 0
    for e in entries:
        k = e.get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
        if k == "suggested":
            suggested += 1
        elif k == "edit" and e.get("suggested") is not None:
            paired += 1
            tot = sum(_field_distance(_get_param(e.get("params") or {}, f),
                                      _get_param(e.get("suggested") or {}, f), u)
                      for f, u in ACTION_FIELDS) / len(ACTION_FIELDS)
            if tot < ACCEPT_TOL:
                accepted += 1
    return {"count": len(entries), "kinds": kinds, "suggestions": suggested,
            "settled": paired, "accepted": accepted,
            "adoption_rate": round(accepted / paired, 4) if paired else None}


def blend_with_prior(prior, stats, batch=None, beta=0.5, k=8, entries=None):
    """E2 产品形态：候选 = [先验, 记忆中位数]，score = 先验单位距离 + β·Ã 距离。
    记忆不足/相似度低 → 原样返回先验（fallback=True）。β 平台 0.25–4，默认 0.5。"""
    prior = dict(prior or {})
    entries = load() if entries is None else entries
    nbrs = _neighbors(stats, entries, k, batch)
    conf = float(np.mean([s for _, s in nbrs])) if len(nbrs) >= _MIN_ENTRIES else 0.0
    if len(nbrs) < _MIN_ENTRIES or conf < _MIN_SIM:
        return {"params": prior, "used_memory": False, "fallback": True,
                "confidence": round(conf, 4)}
    med, _ = memory_median(stats, k, entries=[e for e, _ in nbrs], batch=batch)
    if not med:
        return {"params": prior, "used_memory": False, "fallback": True,
                "confidence": round(conf, 4)}
    cands = [prior, {**prior, **med}]  # 记忆字段覆盖先验，未覆盖键（engine 专有）保留

    def dist_to(a, b):
        return sum(_field_distance(_get_param(a, f), _get_param(b, f), u)
                   for f, u in ACTION_FIELDS) / len(ACTION_FIELDS)

    def adist(c):
        tot, wsum = 0.0, 0.0
        for e, s in nbrs:
            w = s * float(e.get("weight", 1.0))
            d = sum(_field_distance(_get_param(c, f), _get_param(e["params"], f), u)
                    for f, u in ACTION_FIELDS)
            tot += d * w
            wsum += w
        return tot / max(wsum, 1e-9)

    pick = min(cands, key=lambda c: dist_to(c, prior) + beta * adist(c))
    return {"params": pick, "used_memory": pick is cands[1], "fallback": False,
            "confidence": round(conf, 4)}


def rerank(candidates, stats, beta=1.0, k=12, entries=None, batch=None):
    """JitRL 候选级重排：score = β·Ã（candidate["_prior_logprob"] 预留先验钩子）。

    返回 {"ranked": [{params, score, advantage}], "fallback": bool, "confidence": f}。
    fallback=True 时 ranked 原样返回（score=0），调用方应退回基础模型输出。
    """
    candidates = [c for c in (candidates or []) if isinstance(c, dict)]
    if not candidates:
        return {"ranked": [], "fallback": True, "confidence": 0.0}
    entries = load() if entries is None else entries
    nbrs = _neighbors(stats, entries, k, batch)
    conf = float(np.mean([s for _, s in nbrs])) if len(nbrs) >= _MIN_ENTRIES else 0.0
    if len(nbrs) < _MIN_ENTRIES or conf < _MIN_SIM:
        return {"ranked": [{"params": c, "score": 0.0, "advantage": 0.0} for c in candidates],
                "fallback": True, "confidence": conf}
    scored = []
    for c in candidates:
        adv = advantage(c, nbrs)
        prior = float(c.get("_prior_logprob", 0.0) or 0.0)
        scored.append({"params": c, "advantage": round(adv, 4),
                       "score": round(beta * adv + prior, 4)})
    scored.sort(key=lambda t: t["score"], reverse=True)
    return {"ranked": scored, "fallback": False, "confidence": round(conf, 4)}


def _to_edits(median_fields):
    """加权中位数结果 → 只含非默认键的编辑参数 dict。"""
    edits = {}
    defaults = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "warmth": 0.0,
                "shadows": 0.0, "highlights": 0.0, "vibrance": 0.0,
                "wb_temp": 0, "wb_tint": 0, "clarity": 0.0, "texture": 0.0,
                "dehaze": 0.0}
    for field, _unit in ACTION_FIELDS:
        if not field.startswith("hsl."):
            v = median_fields.get(field)
            if v is None:
                continue
            default = defaults.get(field)
            if field == "wb_temp":
                v = int(round(v)) if abs(v - 5500.0) >= 150 else 0
            elif isinstance(default, int):
                v = int(round(v))
            else:
                v = round(v, 3)
            if v != default:
                edits[field] = v
    hsl = {}
    for band in HUE_BANDS:
        v = median_fields.get("hsl.%s.1" % band)
        if v is not None and abs(v) > 0.04:
            hsl[band] = [0, round(v, 2), 0]
    if hsl:
        edits["hsl"] = hsl
    return edits


# ── IPC（P0-1 注册表） ──────────────────────────────────────────────────────

import command_registry  # noqa: E402
from command_registry import command  # noqa: E402


@command("memory_record", "editor",
         "[记忆层] 追加一条个性化经验 (照片统计, 参数, 结果)。kind: edit|accept|reject。",
         params_schema={"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["edit", "accept", "reject"]},
             "stats": {"type": "object", "description": "photo_stats.compute_stats 产物"},
             "params": {"type": "object", "description": "最终编辑参数"},
             "path": {"type": "string", "description": "照片路径（可省略）"},
             "suggested": {"type": "object", "description": "当时的 AI 建议参数（可省略）"},
             "weight": {"type": "number", "description": "经验权重 0..5，默认 1"}},
          "required": ["kind", "stats", "params"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_memory_record(params):
    p = params or {}
    ok = record(p.get("kind"), p.get("stats"), p.get("params"),
                path=p.get("path"), suggested=p.get("suggested"),
                weight=p.get("weight", 1.0))
    return {"ok": bool(ok)}


@command("memory_median", "editor",
         "[记忆层] 按照片统计检索相似经验，给出个性化参数中位数（JitRL 检索路径）。"
         "confidence 低时 params 为 null，调用方应回退基础模型。",
         params_schema={"type": "object", "properties": {
             "stats": {"type": "object"}, "k": {"type": "integer"}},
          "required": ["stats"], "additionalProperties": False},
         agent_tool=True)
def handle_memory_median(params):
    p = params or {}
    edits, conf = memory_median(p.get("stats"), p.get("k", 12))
    return {"ok": True, "params": edits, "confidence": round(conf, 4),
            "fallback": edits is None}


@command("memory_rerank", "editor",
         "[记忆层] 候选参数组按检索优势重排（score = β·Ã）。fallback=True 表示经验不足。",
         params_schema={"type": "object", "properties": {
             "stats": {"type": "object"},
             "candidates": {"type": "array", "items": {"type": "object"},
                            "description": "候选参数组列表，可带 _prior_logprob"},
             "beta": {"type": "number", "description": "优势权重，默认 1"},
             "k": {"type": "integer", "description": "检索近邻数，默认 12"}},
          "required": ["stats", "candidates"], "additionalProperties": False},
         agent_tool=True)
def handle_memory_rerank(params):
    p = params or {}
    return {"ok": True, **rerank(p.get("candidates"), p.get("stats"),
                                 beta=p.get("beta", 1.0), k=p.get("k", 12))}


@command("memory_status", "editor",
         "[记忆层] 情景记忆状态：条目数、按 kind 分布、存储路径。只读。"
         "传 path 时附带该照片最近 30 条记忆摘要（历史面板合并展示用）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "按照片路径过滤摘要（可省略）"}},
          "additionalProperties": False},
         agent_tool=True)
def handle_memory_status(params):
    p = params or {}
    entries = load()
    kinds = {}
    for e in entries:
        kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    out = {"ok": True, "count": len(entries), "kinds": kinds, "path": _PATH,
           "enabled": enabled()}
    if p.get("path"):
        # P3-10 双日志 UI 合并：单照片记忆摘要（时间倒序，最新 30 条）
        out["entries"] = [{"ts": e.get("ts"), "kind": e.get("kind")}
                          for e in reversed(entries)
                          if e.get("path") == str(p["path"])][:30]
    return out


@command("memory_clear", "editor",
         "[记忆层] 清除个性化记忆：不传 batch 全清；传 batch（照片目录）只清该批。不可逆。",
         params_schema={"type": "object", "properties": {
             "batch": {"type": "string", "description": "照片目录（省略 = 全部清除）"}},
          "additionalProperties": False},
         destructive=True, agent_tool=True)
def handle_memory_clear(params):
    p = params or {}
    removed = clear(p.get("batch"))
    return {"ok": removed >= 0, "removed": max(removed, 0), "enabled": enabled()}


@command("memory_stats", "editor",
         "[记忆层] 北极星指标：AI 建议数 / 保存落定配对数 / 采纳数 / 采纳率 / 记忆条目分布。"
         "采纳口径 = 最终参数与建议的平均距离 < 35% 典型步长。只读。",
         params_schema={"type": "object", "properties": {}, "additionalProperties": False},
         agent_tool=True)
def handle_memory_stats(params):
    return {"ok": True, "enabled": enabled(), **stats_summary()}


@command("memory_suggest", "editor",
         "[记忆层] AI 建议快照（数据飞轮第一步）：记录 (照片统计, 建议参数)，保存时由 save_edits 自动 settle 闭环。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片绝对路径"},
             "stats": {"type": "object", "description": "photo_stats.compute_stats 产物"},
             "params": {"type": "object", "description": "建议参数"}},
          "required": ["path", "stats", "params"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_memory_suggest(params):
    p = params or {}
    ok = suggest(p.get("path"), p.get("stats"), p.get("params"))
    return {"ok": bool(ok)}


@command("memory_settle", "editor",
         "[记忆层] 保存落定：把最终参数与最近一条建议配对成经验（accepted=与建议距离<0.35 典型单位）。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string"},
             "params": {"type": "object", "description": "最终编辑参数"},
             "stats": {"type": "object", "description": "可省略，缺省沿用建议时快照"}},
          "required": ["path", "params"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_memory_settle(params):
    p = params or {}
    ok, accepted = settle(p.get("path"), p.get("params"), stats=p.get("stats"))
    return {"ok": bool(ok), "accepted": bool(accepted)}


@command("memory_blend", "editor",
         "[记忆层] E2 产品形态：先验参数与记忆中位数按 score=先验距离+β·Ã 选优（β 平台 0.25–4）。记忆不足原样返回先验。",
         params_schema={"type": "object", "properties": {
             "path": {"type": "string", "description": "照片路径（推导 batch）"},
             "stats": {"type": "object"},
             "prior": {"type": "object", "description": "先验参数"},
             "beta": {"type": "number", "description": "默认 0.5"},
             "k": {"type": "integer", "description": "默认 8"}},
          "required": ["path", "stats", "prior"], "additionalProperties": False},
         agent_tool=True, workfolder_paths=["path"])
def handle_memory_blend(params):
    p = params or {}
    out = blend_with_prior(p.get("prior"), p.get("stats"),
                           batch=os.path.dirname(str(p.get("path") or "")),
                           beta=p.get("beta", 0.5), k=p.get("k", 8))
    return {"ok": True, **out}


def register_handlers():
    command_registry.register_all()
    send_log("edit_memory module loaded (IPC via command_registry)", "info")
