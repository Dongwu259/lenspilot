"""
photo_tagger.py — AI 自动标签（多模态 LLM）

用已配置的多模态 LLM 给照片批量生成中文标签：
固定类目（题材/构图/光线，各至多 1-2 个）+ 2-4 个自由关键词。

成本与失败控制：
- 每张图压到 768px JPEG q80（借鉴 shooting_coach._llm_advise），4 张/批
  一次调用（一个 user message 内多个 image_url part + 编号）。
- 要求模型返回严格 JSON：[{"i":1,"tags":["婚礼","人像","逆光"]}, ...]。
  解析鲁棒（剥 code fence、截取首个 [...]）；整批失败/解析不出 → 降级逐张；
  逐张仍失败 → 计 failed 继续，绝不中断整轮。
- 结果写入 score_cache.photo_tags（mtime/size 失效语义与评分缓存一致）；
  已有有效 tags 的照片跳过（force=true 强制重打）。

命令：
  auto_tag_photos {paths, batch_size?, force?} → 汇总 {total, tagged, failed,
      skipped, cancelled, results:{path:[tags]}}；预检失败返回 {"error": ...}
      （multimodal_disabled / llm_unavailable / no_model / already_running）。
  cancel_auto_tag {} → 取消进行中的打标。
  get_photo_tags {paths} → {tags: {path: [tags]}}（只返回未过期的）。
事件：
  tag_progress {current, total, last_file, tagged, failed, skipped}
  tag_done     同汇总（含 results）。
"""

import base64
import io
import json
import os
import re
import threading

from service import send_event, send_log, register_handler
import score_cache
from model_loader import _load_image_rgb

DEFAULT_BATCH_SIZE = 4
MAX_BATCH_SIZE = 8
THUMB_PX = 768
JPEG_QUALITY = 80
MAX_TAGS_PER_PHOTO = 8

SUBJECT_TAGS = ["婚礼", "人像", "风景", "纪实", "静物", "活动", "商业", "其他"]
COMPOSE_TAGS = ["特写", "半身", "全身", "远景", "航拍"]
LIGHT_TAGS = ["顺光", "逆光", "侧光", "夜景", "室内", "黄金时刻"]

_SYSTEM_PROMPT = (
    "你是照片自动打标助手。用户会一次发多张编号照片（[1] [2] …）。"
    "请仔细观察每张照片，为它输出中文标签，规则：\n"
    "1) 题材：从【" + "、".join(SUBJECT_TAGS) + "】中选至多 2 个；\n"
    "2) 构图：从【" + "、".join(COMPOSE_TAGS) + "】中选至多 1 个（拿不准可不选）；\n"
    "3) 光线：从【" + "、".join(LIGHT_TAGS) + "】中选至多 1 个（拿不准可不选）；\n"
    "4) 再加 2-4 个自由关键词（中文、简短、描述画面内容或氛围，如：笑场、逆光剪影、草坪、仪式）。\n"
    "每张照片标签总数不超过 8 个。只返回严格 JSON 数组，不要输出任何其他文字：\n"
    '[{"i":1,"tags":["婚礼","人像","逆光"]},{"i":2,"tags":["风景","远景","黄金时刻","湖面"]}]'
)

_tag_cancel = threading.Event()
_tag_running = False
_tag_lock = threading.Lock()
_last_diag = ""  # 最近一次失败诊断（API 异常或模型原话摘录），随 summary 透出


def _record_diag(msg):
    global _last_diag
    if msg:
        _last_diag = str(msg).replace("\n", " ")[:300]


# ══════════════════════════════════════════════════════════════════════
# Pure helpers (unit-testable, no IPC)
# ══════════════════════════════════════════════════════════════════════

def _encode_image(path):
    """Load (RAW/HEIC-safe) and compress to ≤768px JPEG base64; None on failure."""
    try:
        im = _load_image_rgb(path)
        if im is None:
            return None
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _clean_tags(raw):
    """Normalize one tag list: str-only, strip '#', dedupe, cap length/count."""
    out = []
    if not isinstance(raw, list):
        return out
    for tg in raw:
        if not isinstance(tg, str):
            continue
        tg = tg.strip().lstrip("#").strip()
        if tg and tg not in out:
            out.append(tg[:16])
    return out[:MAX_TAGS_PER_PHOTO]


def _parse_tag_response(text):
    """Parse model output into {index:int -> [tags]}. Tolerates code fences,
    surrounding prose, and a bare single-object answer; returns None when no
    usable JSON is found."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # 候选 JSON：整体 → 首个 [...] 切片 → 首个 {...} 切片；取首个能提取标签的
    candidates = []
    try:
        candidates.append(json.loads(s))
    except Exception:
        pass
    start, end = s.find("["), s.rfind("]")
    if start >= 0 and end > start:
        try:
            candidates.append(json.loads(s[start:end + 1]))
        except Exception:
            pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            candidates.append(json.loads(s[start:end + 1]))
        except Exception:
            pass
    for arr in candidates:
        if isinstance(arr, dict):
            arr = [arr]
        if not isinstance(arr, list):
            continue
        out = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            i = item.get("i")
            if isinstance(i, str) and i.isdigit():
                i = int(i)
            if not isinstance(i, int):
                continue
            tags = _clean_tags(item.get("tags"))
            if tags:
                out[i] = tags
        if out:
            return out
    return None


def _build_batch_messages(batch):
    """batch: [(path, b64)] → system+user messages with numbered image parts."""
    parts = [{"type": "text",
              "text": f"以下是 {len(batch)} 张编号照片，请按规则为每张输出标签 JSON。"}]
    for i, (_p, b64) in enumerate(batch):
        parts.append({"type": "text", "text": f"[{i + 1}]"})
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return [{"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": parts}]


def _call_llm(client, model, messages, max_tokens=1200):
    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=0.2)
    return (resp.choices[0].message.content or "").strip()


def tag_batch(client, model, batch):
    """One LLM call for the whole batch [(path, b64)] → {path: [tags]}.
    Transport/API errors raise (caller degrades); parse failure → {}."""
    text = _call_llm(client, model, _build_batch_messages(batch))
    parsed = _parse_tag_response(text)
    if not parsed:
        # 典型场景：模型不支持视觉输入，用文字回复"看不到图片"——原话透出给用户
        _record_diag("模型未返回标签 JSON，原文：" + (text or "(空响应)")[:160])
    out = {}
    if parsed:
        for i, (p, _b) in enumerate(batch):
            tags = parsed.get(i + 1)
            if tags:
                out[p] = tags
    return out


def tag_single(client, model, path, b64):
    """Per-image fallback after a batch failure. Returns [tags] or None; never raises."""
    try:
        return tag_batch(client, model, [(path, b64)]).get(path)
    except Exception as e:
        _record_diag(f"API 异常: {e}")
        return None


def run_tagging(paths, batch_size=DEFAULT_BATCH_SIZE, force=False,
                client=None, model=None, progress_cb=None, should_cancel=None):
    """Tag a list of image paths. Returns a summary dict; preflight problems
    return {"error": ...} instead. client/model injectable for tests — when
    client is None the agent config + official-key fallback build a real one.
    """
    global _last_diag
    _last_diag = ""
    if client is None:
        from agent_module import _load_agent_config, _make_client, resolve_model, vision_available
        cfg = _load_agent_config()
        if not vision_available(cfg):
            return {"error": "multimodal_disabled",
                    "detail": "多模态功能未开启。请在 AI 助手设置中启用「多模态（视觉/音频）」，或启用本地模型后再使用 AI 打标。"}
        client, err = _make_client(cfg)
        if not client:
            return {"error": "llm_unavailable", "detail": err or "LLM 未配置"}
        model = model or resolve_model(cfg)
        if not model:
            return {"error": "no_model", "detail": "未配置模型名称"}

    try:
        batch_size = int(batch_size or DEFAULT_BATCH_SIZE)
    except Exception:
        batch_size = DEFAULT_BATCH_SIZE
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))

    summary = {"total": 0, "tagged": 0, "failed": 0, "skipped": 0,
               "cancelled": False, "results": {}}

    # Existing files only; skip already-tagged unless force
    pending = []
    seen = set()
    for p in paths or []:
        if not p or not isinstance(p, str) or p in seen:
            continue
        seen.add(p)
        if not os.path.isfile(p):
            summary["failed"] += 1
            continue
        pending.append(p)
    if not force and pending:
        cached = score_cache.get_tags(pending)
        if cached:
            hit = set(cached)
            summary["skipped"] = len(hit)
            summary["results"].update(cached)
            pending = [p for p in pending if p not in hit]

    summary["total"] = len(pending) + summary["skipped"]
    done = 0  # processed in this run (tagged + failed), for progress display

    def _emit(last_file=""):
        if progress_cb:
            progress_cb({
                "current": done,
                "total": len(pending),
                "last_file": last_file,
                "tagged": summary["tagged"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
            })

    for off in range(0, len(pending), batch_size):
        if should_cancel and should_cancel():
            summary["cancelled"] = True
            break
        chunk = pending[off:off + batch_size]
        batch = []  # [(path, b64)] — images that decoded OK
        for p in chunk:
            b64 = _encode_image(p)
            if b64:
                batch.append((p, b64))
            else:
                summary["failed"] += 1
                done += 1
                _emit(os.path.basename(p))
        if not batch:
            continue

        try:
            tagged = tag_batch(client, model, batch)
        except Exception as e:
            send_log(f"[tagger] 批量打标异常，降级逐张: {e}", "warn")
            _record_diag(f"API 异常: {e}")
            tagged = {}
        if not tagged:
            # Batch call failed or returned nothing usable → per-image fallback
            for p, b in batch:
                if should_cancel and should_cancel():
                    summary["cancelled"] = True
                    break
                one = tag_single(client, model, p, b)
                if one:
                    tagged[p] = one

        for p, _b in batch:
            tags = tagged.get(p)
            if tags:
                try:
                    st = os.stat(p)
                    score_cache.put_tags(p, int(st.st_mtime), st.st_size, tags)
                except Exception:
                    pass
                summary["tagged"] += 1
                summary["results"][p] = tags
            else:
                summary["failed"] += 1
            done += 1
            _emit(os.path.basename(p))

    if summary["failed"] and _last_diag:
        summary["last_error"] = _last_diag
    return summary


# ══════════════════════════════════════════════════════════════════════
# IPC handlers
# ══════════════════════════════════════════════════════════════════════

def handle_auto_tag_photos(params):
    global _tag_running
    paths = params.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return {"error": "missing_paths", "detail": "auto_tag_photos 需要非空 paths 数组"}
    with _tag_lock:
        if _tag_running:
            return {"error": "already_running", "detail": "已有打标任务进行中"}
        _tag_running = True
    _tag_cancel.clear()
    try:
        summary = run_tagging(
            paths,
            batch_size=params.get("batch_size"),
            force=bool(params.get("force")),
            progress_cb=lambda d: send_event("tag_progress", d, noblock=True),
            should_cancel=_tag_cancel.is_set,
        )
        if summary.get("error"):
            return summary
        # tag_done 带全量 results，前端据此补显；响应体同内容（调用方兜底）。
        send_event("tag_done", summary)
        return summary
    finally:
        with _tag_lock:
            _tag_running = False


def handle_cancel_auto_tag(params):
    _tag_cancel.set()
    return {"ok": True}


def handle_get_photo_tags(params):
    paths = params.get("paths") or []
    if not isinstance(paths, list):
        return {"tags": {}}
    return {"tags": score_cache.get_tags(paths)}


def register_handlers():
    register_handler("auto_tag_photos", handle_auto_tag_photos)
    register_handler("cancel_auto_tag", handle_cancel_auto_tag)
    register_handler("get_photo_tags", handle_get_photo_tags)
