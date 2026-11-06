"""
pipeline_runner.py — 数字员工自主管线编排（0.8.4 M1，0.8.5 工作流 Schema v2）

第一阶段（PR1 骨架）：scan → score → 出结果 + run_state 原子写 + 崩溃恢复 + IPC。

后续 PR：PR2 三道门（动作/成本/停滞）、PR3 决策点（定阈值 LLM + borderline 规则）、
PR4 出 keeper + 阶段门 + 审核视图、PR5 飞轮埋点、PR6 打包接线。

0.8.5 Schema v2：step 新增可选字段 params/condition/gate（见 step_registry），
主循环支持步骤级参数合并、条件跳过(step_skip)、声明式动作门(approve/reject/edit)。

设计决策（详见 M1_PIPELINE_TECH_SPEC.md）：
- 确定性阶段机骨架 + LLM 决策点（不是自由 ReAct），感知工具直接函数调用
- 复用 _request_confirmation 式的门、send_event、agent_module._ensure_model_loaded
- 独立 _active_run 单例，与 agent 互斥（二选一；Agent 桥接走 _from_agent 跳过）
"""

import os, json, uuid, time, threading, hashlib, math

# ── infrastructure from service ──
from service import send_event, send_log, register_handler, _app_config as _service_config

# ── photo pipelines ──
import photo_scorer
import score_cache
from utils import IMAGE_EXTS, RAW_EXTS

# ── data flywheel (optional, graceful) ──
_flywheel_available = False
try:
    import feedback_queue
    _flywheel_available = True
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

STALL_ROUNDS = 2      # 连续 N 步进展签名为空 → 停滞
GATE_TIMEOUT = None    # None = 一直等人(阶段门/动作门不超时); 成本/置信门也复用此值(由调用方用 wait 输 timeout)
MODEL_DOWNGRADE_ENABLED = True  # 成本超限时允许降档模型一次
ERR_REPEAT = 3        # 同一错误重复 N 次 → 停
STATE_DIR_NAME = ".lenspilot"

# ═══════════════════════════════════════════════════════════════════════
# PipelineRun
# ═══════════════════════════════════════════════════════════════════════

class PipelineRun:
    __slots__ = (
        "run_id", "work_folder", "template", "autonomy", "goal", "params",
        "cancel_event", "pause_event",
        "pending_gates", "pending_gate_info",
        "phase_idx", "step_idx", "status",
        "budget",
        "stall",
        "run_log",
        "artifacts",
        "step_params",
        "start_time",
        "cloud_token",
    )

    def __init__(self, run_id, work_folder, template, autonomy, goal, params):
        self.run_id = run_id
        self.work_folder = work_folder
        self.template = template
        self.autonomy = autonomy
        self.goal = goal
        self.params = params or {}
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()   # set = 已暂停，clear = 跑中
        self.pause_event.clear()
        self.pending_gates = {}                # gate_id → {"event":Event,"decision":str}
        self.pending_gate_info = None          # 挂起中的门 {gate_id,type,summary,payload}，get_state 重渲染用
        self.phase_idx = 0
        self.step_idx = 0
        self.status = "running"
        self.budget = {"token_cap": params.get("budget_token_cap", 150_000),
                       "wall_cap_s": params.get("budget_wall_s", 1800),
                       "step_cap": params.get("budget_steps", 200),
                       "spent_tokens": 0,
                       "spent_s": 0,
                       "steps": 0}
        self.stall = {"last_progress_sig": "", "no_progress_rounds": 0,
                      "last_error": "", "err_repeat": 0, "error_hash": ""}
        self.run_log = []   # list[dict] 决策记录
        self.artifacts = {}
        self.step_params = None   # 当前步骤合并参数(edited_params > step.params > run.params)，主循环每步刷新
        self.start_time = time.time()

# ═══════════════════════════════════════════════════════════════════════
# State persistence
# ═══════════════════════════════════════════════════════════════════════

def _state_dir(folder):
    d = os.path.join(folder, STATE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d

def _state_path(folder):
    return os.path.join(_state_dir(folder), "run_state.json")

def _phases_snapshot(template_id):
    """解析后模板的步骤名快照 [[step名...], ...]，resume 防错位对比用。
    解析失败返回 None（调用方按"无法对比"处理）。"""
    try:
        tmpl = _resolve_template(template_id)
        return [[s["name"] for s in p.get("steps", [])] for p in tmpl.get("phases", [])]
    except Exception:
        return None

def _save_state(run):
    """原子写 run_state.json"""
    p = _state_path(run.work_folder)
    st = {
        "schema": 1,
        "run_id": run.run_id, "template": run.template,
        "autonomy": run.autonomy, "goal": run.goal, "params": run.params,
        "status": run.status,
        "phase_idx": run.phase_idx, "step_idx": run.step_idx,
        "budget": run.budget, "stall": run.stall,
        "artifacts": run.artifacts,
        "phases": _phases_snapshot(run.template),
        "pending_gate": bool(run.pending_gates),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def _load_state(folder, run_id=None):
    """从 run_state.json 恢复。返回 dict 或 None。"""
    p = _state_path(folder)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return None
    if run_id and st.get("run_id") != run_id:
        return None  # 不是同一个 run
    return st

# ═══════════════════════════════════════════════════════════════════════
# Gate system (复用 agent_module._request_confirmation 式的 Event 阻塞)
#    动作门 — 危险操作前等人签
#    成本门 — token/时间/步数硬上限
#    停滞门 — 连续无进展或同错重复
# ═══════════════════════════════════════════════════════════════════════

def _await_gate(run, gate_type, summary, payload=None, timeout=GATE_TIMEOUT):
    """阻塞等人通过门。返回 dict {approved, edited_params?, rejected}。

    门只能由显式 decision(gate_resolve) 或 cancel 解除；pause/resume 不影响。
    cancel 时 handle_pipeline_cancel 会向所有 pending_gates 写入
    {"approved": False, "reason": "canceled"} 并 set event 唤醒本函数。"""
    if run.pause_event.is_set():
        return {"approved": False, "reason": "paused"}
    if run.cancel_event.is_set():
        return {"approved": False, "reason": "canceled"}
    gate_id = uuid.uuid4().hex[:10]
    event = threading.Event()
    run.pending_gates[gate_id] = {"event": event, "decision": None}
    run.pending_gate_info = {"gate_id": gate_id, "type": gate_type,
                             "summary": summary, "payload": payload or {}}
    _emit_pipeline(run, "gate", {
        "gate_id": gate_id, "type": gate_type, "summary": summary,
        "payload": payload or {},
    })
    got = event.wait(timeout=timeout)
    decision = run.pending_gates[gate_id].get("decision")
    # fallback: timeout → treat as rejection (don't auto-proceed on dangerous actions)
    if not got or not decision:
        decision = {"approved": False, "reason": "timeout" if not got else "no_decision"}
    run.pending_gates.pop(gate_id, None)
    run.pending_gate_info = None
    return decision

def _cost_exceeded(run):
    """检查 token/时间/步数 任一超限 → True。本地推理不计 token(spent_tokens 来自 LLM 决策点才累加)。"""
    b = run.budget
    return (b["spent_tokens"] >= b["token_cap"] or
            b["spent_s"] >= b["wall_cap_s"] or
            b["steps"] >= b["step_cap"])

def _degrade_or_pause(run):
    """成本超限处理：先降档模型一次，再超就挂预算门等人。
    返回 True=继续跑 / False=中止(主循环按 cancel_event/status 收尾)。"""
    b = run.budget
    if not b.get("_downgraded") and MODEL_DOWNGRADE_ENABLED:
        # 降档到便宜模型一次，不清空计数器（已花的没法退）
        b["_downgraded"] = True
        _emit_pipeline(run, "log",
                       {"level": "warn", "message": f"成本超限，已自动降档模型; 如再超将暂停"})
        return True  # 不阻塞，继续跑
    # 第二次超限或降档不可用 → 预算门等人：approve 提高预算继续 / reject 终止管线
    run.status = "paused"
    _save_state(run)
    decision = _await_gate(
        run, "budget",
        f"预算耗尽(spent_tokens={b['spent_tokens']}/{b['token_cap']}, time={b['spent_s']}s/{b['wall_cap_s']}s, steps={b['steps']}/{b['step_cap']})",
        payload={"budget": b})
    run.status = "running"
    if decision.get("approved"):
        # 提额策略选"翻倍"：简单可靠——与剩余工作量估算无关，不会提额后仍立即超限
        # （若极端情况下仍超限，预算门会再次弹出，不会死循环：reject/cancel 都会终止）
        b["token_cap"] = b.get("token_cap", 0) * 2
        b["wall_cap_s"] = b.get("wall_cap_s", 0) * 2
        b["step_cap"] = b.get("step_cap", 0) * 2
        _emit_pipeline(run, "log",
                       {"level": "info",
                        "message": f"预算已提高: token_cap={b['token_cap']}, wall_cap_s={b['wall_cap_s']}s, step_cap={b['step_cap']}"})
        _emit_pipeline(run, "resumed", {"reason": "budget_raised"})
        return True
    if decision.get("reason") in ("canceled", "paused"):
        # 取消/暂停打断：不当成拒绝，回主循环由 cancel/pause 常规收尾
        return False
    # 显式拒绝 → 终止管线
    run.status = "canceled"
    _save_state(run)
    _emit_pipeline(run, "done", {"summary": "预算超限，用户终止管线"})
    return False

def _handle_cost_gate(run):
    """步前成本门：超限→降档或预算门。返回 False 表示应中止(取消/拒绝)。"""
    if not _cost_exceeded(run):
        return True
    return _degrade_or_pause(run)

# ═══════════════════════════════════════════════════════════════════════
# Progress signature (stall gate, §1.3 spec)
# ═══════════════════════════════════════════════════════════════════════

def _progress_sig(run):
    """量化"可验证进展"，停滞门比较用。不含 run_log 长度(每步都追加,会使签名恒变)。
    任意 artifact 变化都算进展（list 取长度，防大清单撑爆签名），
    自定义管线的非传统 key(exported_count/cloud_photo_ids 等)变化也不会被误杀。"""
    return repr(sorted(
        (k, len(v) if isinstance(v, list) else v)
        for k, v in run.artifacts.items()))

# ═══════════════════════════════════════════════════════════════════════
# Schema v2: 步骤级参数 / 条件求值
# ═══════════════════════════════════════════════════════════════════════

def _step_param(run, key, default=None):
    """读当前步骤的合并参数(run.step_params)。无 step_params 时回退 run.params。
    合并优先级由主循环保证：门 edited_params > step.params > run.params。"""
    sp = getattr(run, "step_params", None)
    if sp is None:
        return run.params.get(key, default)
    return sp.get(key, default)

_COND_OPS = ("gt", "ge", "lt", "le", "eq", "ne", "exists", "not_exists", "contains")

def _eval_condition(run, cond):
    """结构化求值 step condition（不用 eval/ast）。非法 key/op 抛 ValueError
    （正常应在 validate_template 校验期挡掉，这里是运行期防御）。

    key 取值：
      artifacts.<子键>  — 读 run.artifacts 子键
      params.<子键>     — 读合并后的 step_params
      goal              — run.goal 字符串
    op 白名单：gt/ge/lt/le/eq/ne/exists/not_exists/contains
    """
    if not isinstance(cond, dict):
        raise ValueError("condition 必须是 dict")
    key = cond.get("key")
    op = cond.get("op")
    if not isinstance(key, str) or not key:
        raise ValueError("condition.key 缺失或非法")
    if op not in _COND_OPS:
        raise ValueError(f"非法 condition.op: {op!r}")

    if key == "goal":
        exists = bool(run.goal)
        val = run.goal
    elif key.startswith("artifacts."):
        sub = key[len("artifacts."):]
        if not sub:
            raise ValueError("condition.key 缺少子键: 'artifacts.'")
        exists = sub in run.artifacts
        val = run.artifacts.get(sub)
    elif key.startswith("params."):
        sub = key[len("params."):]
        if not sub:
            raise ValueError("condition.key 缺少子键: 'params.'")
        merged = getattr(run, "step_params", None) or run.params
        exists = sub in merged
        val = merged.get(sub)
    else:
        raise ValueError(f"非法 condition.key: {key!r} (仅支持 artifacts.*/params.*/goal)")

    if op == "exists":
        return exists
    if op == "not_exists":
        return not exists

    value = cond.get("value")
    if op == "eq":
        return val == value
    if op == "ne":
        return val != value
    if op == "contains":
        if val is None:
            return False
        try:
            return value in val
        except TypeError:
            return False

    # gt/ge/lt/le：数值优先，转不了数值就退直接比较
    if val is None:
        return False
    try:
        lhs, rhs = float(val), float(value)
    except (TypeError, ValueError):
        lhs, rhs = val, value
    try:
        if op == "gt":
            return lhs > rhs
        if op == "ge":
            return lhs >= rhs
        if op == "lt":
            return lhs < rhs
        return lhs <= rhs  # le
    except TypeError:
        return False

# ═══════════════════════════════════════════════════════════════════════
# Flywheel hooks — capture pipeline decisions for training
# ═══════════════════════════════════════════════════════════════════════

def _push_flywheel(run, path, label, source_hint="", override=False):
    """将单个 keep/reject 决策入队 (opt-in gated, reuse photo_scorer hook)"""
    if not _flywheel_available:
        return
    try:
        # 拍照: 用 photo_scorer._capture_feedback 里已经有的逻辑(含 opt-in 开关 + eye_open + 512d)
        import photo_scorer as _ps
        if hasattr(_ps, '_capture_feedback'):
            _ps._capture_feedback(path, label, source="pipeline_" + source_hint,
                                 category=run.template,
                                 model_score=None,  # None = 由 _capture_feedback 回读缓存真实分
                                 override=override)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════
# Emit helpers
# ═══════════════════════════════════════════════════════════════════════

def _emit_pipeline(run, event, data=None):
    d = {"run_id": run.run_id, "template": run.template, "status": run.status}
    if data:
        d.update(data)
    # All events use blocking writes (with flush). The earlier noblock=True
    # for progress events caused them to not reach the frontend reliably.
    # Dedup with cached thumbnails is fast enough that pipe flooding is not
    # a concern (same as scoring's 75 events).
    send_event("pipeline_" + event, d)

# ═══════════════════════════════════════════════════════════════════════
# Phase / step definitions (PR1 先跑 scan + score; 其余标记空)
# ═══════════════════════════════════════════════════════════════════════

def _step_log(run, msg, level="info"):
    """每步结束/关键点发射 log 给前端(区别于内部 run_log)"""
    run.run_log.append({"action": "log", "result": msg})
    _emit_pipeline(run, "log", {"level": level, "message": msg})

def _scan_step(run):
    """Phase 1: 扫描编目"""
    _emit_pipeline(run, "step", {"phase": "import", "step": "scan", "detail": "扫描文件中..."})
    result = photo_scorer.handle_scan_images({"folder": run.work_folder})
    count = result.get("count", 0) or result.get("images", 0) or result.get("total", 0) or len(result.get("images_list", []))
    run.artifacts["scanned"] = count
    _emit_pipeline(run, "progress", {"phase": "import", "done": count, "total": count})
    _step_log(run, f"扫描编目完成: 发现 {count} 张")
    return {"ok": True, "count": count}

def _score_step(run):
    """Phase 2: AI 评分（本地推理，copy_on_score=False 不移动文件）"""
    _emit_pipeline(run, "step", {"phase": "score", "step": "score", "detail": "AI 评分中..."})
    # 确保模型已加载(先加载再取 model_version,缓存清理按当前版本限定)
    folder = run.work_folder
    if not photo_scorer._model_loaded:
        _emit_pipeline(run, "log", {"level": "info", "message": "加载 AI 模型..."})
        try:
            from model_loader import refresh_search_paths
            refresh_search_paths()
        except Exception:
            pass
        photo_scorer.handle_load_model({})
    # 清空评分缓存——确保每次都跑全新推理(防止旧 dev 模型 1.0 分残留)
    # 只清当前文件夹 + 当前模型版本的行: 保住人工标记(manual_keep)和其他后端的缓存
    _step_log(run, "清空当前文件夹旧评分缓存,确保使用最新模型评分...")
    try:
        import sqlite3 as _sql
        from score_cache import _db_path as _scdb
        _mv = photo_scorer.model_version()
        _conn = _sql.connect(_scdb, timeout=10)
        _conn.execute("DELETE FROM scores WHERE (path LIKE ? OR path LIKE ?) "
                      "AND model_version = ? AND (manual_keep = 0 OR manual_keep IS NULL)",
                      (folder.replace('\\', '/') + '/%', folder + os.sep + '%', _mv))
        _conn.commit()
        _conn.close()
    except Exception:
        pass
    threshold = float(_step_param(run, "threshold", 6.0) or 6.0)
    # Direct progress callback (bypasses fragile event subscription)
    # 评分循环只看 photo_scorer._score_cancel: 这里把 run 的取消/暂停传导进去
    def _score_progress(current, total):
        if run.cancel_event.is_set():
            photo_scorer.handle_cancel_scoring({})
        while run.pause_event.is_set() and not run.cancel_event.is_set():
            time.sleep(0.2)  # 暂停中: 挂住评分回调(同步阻塞评分循环), 等 resume/cancel
        if run.cancel_event.is_set():  # 暂停期间点了取消
            photo_scorer.handle_cancel_scoring({})
        _emit_pipeline(run, "progress", {
            "phase": "score", "current": current, "total": total,
        })
    result = photo_scorer.handle_score_photos({
        "folder": folder, "threshold": threshold,
        "copy_on_score": False,
        "on_progress": _score_progress,
    })
    scored = result.get("total", 0)
    run.artifacts["scored"] = scored
    run.artifacts["threshold_used"] = threshold
    _emit_pipeline(run, "progress", {"phase": "score", "done": scored, "total": scored})
    _step_log(run, f"AI 评分完成: {scored} 张, 默认阈值 {threshold}")
    return result

# ── 决策点工具：分数直方图 ──

def _score_histogram(folder):
    """对 work_folder 的所有已评分结果做 10 桶直方图。返回 {buckets: [(lo,hi),count]}, raw_scores: [float], total: int}"""
    mv = photo_scorer.model_version()
    rows = score_cache.get_folder(folder, mv)
    scores = [r["score"] for r in rows if r.get("score") not in (None, -1)]
    if not scores:
        return {"buckets": [], "raw_scores": [], "total": 0}
    hist = [0] * 10
    for s in scores:
        idx = min(int(s / 1.0), 9)  # 1-10 范围，but dev 的 repo 模型输出 low; 真实的 1.2.0 模型输出 5+
        idx = max(0, min(9, idx))
        hist[idx] += 1
    buckets = [{"range": f"{i+1}-{i+2}", "lo": i+1, "hi": i+2, "count": hist[i]} for i in range(10)]
    return {"buckets": buckets, "raw_scores": scores[:500], "total": len(scores)}

def _threshold_rule(scores, goal=""):
    """纯规则兜底：根据分数百分位和 goals 返回阈值。"""
    if not scores:
        return 6.0
    s = sorted(scores)
    # 保守：婚礼/活动"精选"留前 30%; "全导"留前 80%
    if any(kw in (goal or "").lower() for kw in ("婚礼", "wedding", "全导", "全留", "全部")):
        pct = 80
    elif any(kw in (goal or "").lower() for kw in ("精选", "精选", "best")):
        pct = 30
    else:
        pct = 40  # 默认精选 40%
    idx = max(0, int(len(s) * (100 - pct) / 100))
    threshold = s[idx] if idx < len(s) else s[-1]
    return round(max(1.0, min(10.0, threshold)), 1)

def _official_cfg_from_agent():
    """读取 agent 的官方 Key 内存配置(含 model)。代理 _load_agent_config 只从磁盘读。"""
    try:
        from agent_module import _official_config
        return {
            "base_url": (_official_config.get("base_url") or "").strip(),
            "api_key": (_official_config.get("api_key") or "").strip(),
            "model": (_official_config.get("model") or "").strip(),
        }
    except Exception:
        return {"base_url": "", "api_key": "", "model": ""}

def _threshold_step(run):
    """决策点 A：从分数直方图建议阈值。LLM 优先，断网/未配退规则。"""
    _emit_pipeline(run, "step", {"phase": "score", "step": "threshold", "detail": "确定阈值..."})
    histo = _score_histogram(run.work_folder)
    scores = histo["raw_scores"]
    # 用户已给阈值 → 跳过
    if _step_param(run, "threshold"):
        t = float(_step_param(run, "threshold"))
        run.artifacts["threshold_used"] = t
        _step_log(run, f"阈值: 用户指定 {t}")
        return {"ok": True, "threshold": t, "source": "user"}

    # 试 LLM (httpx 30s + threading 35s 双层超时保护)
    _step_log(run, "正在调用 AI 分析分数分布确定最佳阈值...")

    # ── 同步诊断：在启动线程前检查 API 配置状态 ──
    try:
        from agent_module import _official_config as _oc, _load_agent_config as _lac
        _cfg = _lac()
        _diag = []
        if _cfg.get("base_url") and _cfg.get("api_key"):
            _diag.append("个人Key已配置")
        else:
            _diag.append("个人Key未配置" + ("(base_url缺失)" if not _cfg.get("base_url") else "(api_key缺失)"))
        if _oc.get("api_key"):
            _diag.append(f"官方Key已配置(model={_oc.get('model','?')})")
        else:
            _diag.append("官方Key未配置")
        _step_log(run, f"LLM诊断: {'; '.join(_diag)}")
    except Exception as e:
        _step_log(run, f"LLM诊断异常: {e}")

    llm_threshold = None
    _llm_box = {"result": None}  # mutable container for thread

    def _do_llm_call():
        try:
            from agent_module import _load_agent_config, _official_config as _oc, _make_client, resolve_model
            cfg = _load_agent_config()
            client, err = _make_client(cfg)
            if not client:
                _llm_box["result"] = ("error", f"无法创建 LLM 客户端: {err}")
                return
            if not scores:
                _llm_box["result"] = ("error", "无评分数据")
                return
            # 模型名: 运行级覆盖 > 用户云端模型 > 本地 Qwen3-VL(resolve_model 兜底)
            model = (_step_param(run, "model") or cfg.get("model", "").strip()
                     or resolve_model(cfg))
            if not model:
                _llm_box["result"] = ("error", "未配置模型名称")
                return
            buckets = histo["buckets"]
            goal = run.goal
            prefs = cfg.get("preferences", "")
            # 根据目标计算建议保留率
            goal_lower = goal.lower() if goal else ""
            if any(kw in goal_lower for kw in ("婚礼", "wedding", "全导", "全留", "全部")):
                target_pct = "60-80"
            elif any(kw in goal_lower for kw in ("严选", "精选", "best", "严格")):
                target_pct = "15-30"
            elif any(kw in goal_lower for kw in ("春游", "旅游", "旅行", "日常")):
                target_pct = "35-55"
            else:
                target_pct = "30-50"
            prompt = f"""你是摄影选片助手。根据分数直方图和目标,确定最佳筛选阈值。
分数 1-10(10为最佳):
{[b['range']+':'+str(b['count'])+'张' for b in buckets]}
共{histo['total']}张。目标: "{goal}"。偏好: "{prefs}"
建议保留率: {target_pct}%。
请根据实际分数分布确定阈值,而非使用固定值。返回JSON: {{"threshold": <float>, "reason": "<一句话>", "expected_keep_pct": <int>}}"""
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=500,
                extra_body={"thinking": {"type": "disabled"}},
                timeout=60)
            usage = resp.usage
            if usage and usage.total_tokens:
                run.budget["spent_tokens"] = run.budget.get("spent_tokens", 0) + usage.total_tokens
            msg = resp.choices[0].message
            content = (getattr(msg, 'content', None) or
                       getattr(msg, 'reasoning_content', None) or "")
            if not content.strip():
                _llm_box["result"] = ("error", "LLM 返回空内容")
                return
            # Strip markdown code fences (```json ... ```)
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
            data = None
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                import re as _re
                m = _re.search(r'\{[^{}]*"threshold"[^{}]*\}', content.replace('\n',' '))
                if m:
                    try: data = json.loads(m.group(0))
                    except Exception: data = None
            if data and isinstance(data, dict):
                t = float(data.get("threshold", 0))
                if 1.0 <= t <= 10.0:
                    _llm_box["result"] = (round(t, 1), data.get("expected_keep_pct", "?"))
                else:
                    _llm_box["result"] = ("error", f"LLM返回阈值超出范围: {t} (原始: {content[:120]})")
            else:
                _llm_box["result"] = ("error", f"LLM返回格式异常,无法解析JSON: {content[:200]}")
        except Exception as e:
            _llm_box["result"] = ("error", str(e))

    t = threading.Thread(target=_do_llm_call, daemon=True)
    t.start(); t.join(timeout=120)
    if t.is_alive():
        _step_log(run, "AI 阈值超时(>120s), 使用本地规则推算")
    else:
        res = _llm_box["result"]
        if res and res[0] == "error":
            _step_log(run, f"LLM 阈值失败({res[1]}), 退规则")
        elif res:
            llm_threshold, keep_pct = res
            # Safety net: if LLM threshold would reject >85%, fall back to rule
            if scores:
                would_keep = sum(1 for s in scores if s >= llm_threshold)
                keep_pct_actual = would_keep * 100 // len(scores) if scores else 0
                if keep_pct_actual < 10:
                    _step_log(run,
                        f"AI 建议阈值 {llm_threshold} 但实际仅留存 {keep_pct_actual}%(过于严格), 自动调整阈值")
                    llm_threshold = None  # force rule fallback below
                else:
                    _step_log(run, f"AI 建议阈值 {llm_threshold} (预期留存 {keep_pct}%, 实际 {keep_pct_actual}%)")
            else:
                _step_log(run, f"AI 建议阈值 {llm_threshold} (预期留存 {keep_pct}%)")

    # 退规则
    if llm_threshold is None:
        llm_threshold = _threshold_rule(scores, run.goal)
        _step_log(run, f"规则阈值: {llm_threshold} (LLM 不可用,使用本地规则推算)")

    run.artifacts["threshold_used"] = llm_threshold
    return {"ok": True, "threshold": llm_threshold, "source": "llm" if llm_threshold else "rule"}

# ── 决策点 B：borderline 裁决（LLM 批量判标量 → 置信门交人审 uncertain）──

def _cull_borderline_step(run):
    """LLM 批量裁决 borderline 照片（仅传 score/blur/eye_open 标量特征，不传图）。
    LLM 不可用时退纯规则。LLM 返回 uncertain 的项送入置信门批量交人一次审。"""
    _emit_pipeline(run, "step", {"phase": "cull", "step": "borderline", "detail": "LLM 裁决 borderline..."})
    folder = run.work_folder
    mv = photo_scorer.model_version()
    rows = score_cache.get_folder(folder, mv)
    threshold = run.artifacts.get("threshold_used", 6.0)

    # ── 收集 borderline 候选：未被手动标过 + 分数在边界区 ──
    candidates = []
    for row in rows:
        manual = row.get("manual_keep") or 0
        if abs(manual) == 1:
            continue
        score = row.get("score") or 0
        if threshold - 0.5 <= score <= threshold + 0.3:
            candidates.append(row)

    if not candidates:
        _step_log(run, "borderline 裁决: 无待裁决照片")
        run.artifacts["borderline_kept"] = 0
        run.artifacts["borderline_rejected"] = 0
        run.artifacts["uncertain_count"] = 0
        return {"ok": True, "kept": 0, "rejected": 0, "uncertain": 0}

    _step_log(run, f"borderline 裁决: {len(candidates)} 张待判断, 正在采集标量特征...")

    # ── 采集标量特征 (blur + eye_open) ──
    # eye_open for HEIC/RAW uses cached 260px JPEG thumbnails (same strategy as _cull_step)
    _FAST_EYE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".webp"}
    _THUMB_EYE_EXTS = {".heic", ".heif"} | set(RAW_EXTS)

    _emit_pipeline(run, "progress", {"phase": "cull", "current": 0, "total": len(candidates)})

    photos = []
    for ci, row in enumerate(candidates):
        path = row["path"]
        ext = os.path.splitext(path)[1].lower()
        blur = None
        try:
            from model_loader import compute_blur_score
            blur = round(float(compute_blur_score(path)), 2)
        except Exception:
            pass
        eye_open = None
        try:
            from eye_detector import eye_open_score
            if ext in _FAST_EYE_EXTS:
                eye_open = eye_open_score(path)
            elif ext in _THUMB_EYE_EXTS:
                thumb = row.get("thumb_path", "")
                if thumb and os.path.exists(thumb):
                    eye_open = eye_open_score(thumb)
            if eye_open is not None:
                eye_open = round(float(eye_open), 2)
        except Exception:
            pass
        photos.append({"path": path, "score": row.get("score") or 0,
                       "blur": blur, "eye_open": eye_open})
        # Progress every 2 photos (blur + eye detection can be slow)
        if (ci + 1) % 2 == 0 or ci == len(candidates) - 1:
            _emit_pipeline(run, "progress", {"phase": "cull", "current": ci + 1, "total": len(candidates)})

    # ── 试 LLM 批量裁决 ──
    llm_decisions = None
    _llm_box = {"result": None}

    def _do_llm_borderline():
        try:
            from agent_module import _load_agent_config, _official_config as _oc, _make_client, resolve_model
            cfg = _load_agent_config()
            client, err = _make_client(cfg)
            if not client:
                _llm_box["result"] = ("error", f"无法创建 LLM 客户端: {err}")
                return
            if not photos:
                return
            # 模型名: 运行级覆盖 > 用户云端模型 > 本地 Qwen3-VL(resolve_model 兜底)
            model = (_step_param(run, "model") or cfg.get("model", "").strip()
                     or resolve_model(cfg))
            if not model:
                _llm_box["result"] = ("error", "未配置模型名称")
                return
            BATCH = 50
            all_decisions = []
            for bs in range(0, len(photos), BATCH):
                batch = photos[bs:bs + BATCH]
                items = []
                for idx, p in enumerate(batch):
                    item = {"id": idx, "score": p["score"]}
                    if p["blur"] is not None:
                        item["blur"] = p["blur"]
                    if p["eye_open"] is not None:
                        item["eye_open"] = p["eye_open"]
                    items.append(item)
                goal = run.goal
                prompt = (
                    f"你是摄影选片助手。以下照片处于阈值边界(阈值={threshold})，"
                    f"请根据标量特征判断每张 keep/reject/uncertain。\n\n"
                    f"照片特征:\n{json.dumps(items, ensure_ascii=False)}\n\n"
                    f'目标: "{goal}"\n'
                    f"判断标准:\n"
                    f"- keep: 照片质量尚可,值得保留\n"
                    f"- reject: 质量不足,建议丢弃\n"
                    f"- uncertain: 信息不足(无眼检测结果/边缘分数),交人审\n\n"
                    f'请只返回一个 JSON 对象,格式: {{"decisions":[{{"id":0,"decision":"keep","reason":"..."}},...]}}\n'
                    f"decision 只能是 keep/reject/uncertain 之一。")
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1, max_tokens=4000,
                    extra_body={"thinking": {"type": "disabled"}},
                    timeout=60)
                usage = resp.usage
                if usage and usage.total_tokens:
                    run.budget["spent_tokens"] = run.budget.get("spent_tokens", 0) + usage.total_tokens
                msg = resp.choices[0].message
                content = (getattr(msg, 'content', None) or
                           getattr(msg, 'reasoning_content', None) or "")
                # Strip markdown code fences
                content = content.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    content = "\n".join(lines).strip()
                data = None
                try:
                    data = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    import re as _re
                    m = _re.search(r'\{[^{}]*"decisions"\s*:\s*\[.*?\][^{}]*\}',
                                   content.replace('\n', ' '), _re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group(0))
                        except Exception:
                            data = None
                if data and isinstance(data.get("decisions"), list):
                    for d in data["decisions"]:
                        d["_bs"] = bs
                    all_decisions.extend(data["decisions"])
            if all_decisions:
                _llm_box["result"] = all_decisions
        except Exception as e:
            _llm_box["result"] = ("error", str(e))

    t = threading.Thread(target=_do_llm_borderline, daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive():
        _step_log(run, "LLM borderline 超时(>120s), 使用本地规则推算")
    else:
        res = _llm_box["result"]
        if res and isinstance(res, list):
            llm_decisions = res
            _step_log(run, f"LLM borderline: {len(llm_decisions)} 条裁决")
        elif res and res[0] == "error":
            _step_log(run, f"LLM borderline 失败({res[1]}), 退规则")

    # ── 应用裁决 ──
    kept, rejected = 0, 0
    uncertain_paths = []

    if llm_decisions:
        for d in llm_decisions:
            bs = d.get("_bs", 0)
            idx = d.get("id", -1)
            pi = bs + idx
            if 0 <= pi < len(photos):
                p = photos[pi]
                dec = d.get("decision", "keep")
                if dec == "reject":
                    photo_scorer.handle_set_manual_keep({"path": p["path"], "keep": -1})
                    rejected += 1
                elif dec == "uncertain":
                    uncertain_paths.append(p)
                else:
                    kept += 1
    else:
        # ── 规则退路 ──
        for p in photos:
            s, eye = p["score"], p["eye_open"]
            if s < threshold - 0.3:
                photo_scorer.handle_set_manual_keep({"path": p["path"], "keep": -1})
                rejected += 1
            elif eye is not None and eye < 0.2:
                photo_scorer.handle_set_manual_keep({"path": p["path"], "keep": -1})
                rejected += 1
            else:
                kept += 1

    # ── 构建 uncertain 详情（带缩略图，供置信门/最终审核视图展示）──
    uncertain_detail = []
    for up in uncertain_paths:
        row = next((r for r in rows if r["path"] == up["path"]), None)
        uncertain_detail.append({
            "path": up["path"],
            "score": up["score"],
            "thumb": row.get("thumb_path", "") if row else "",
            "blur": up.get("blur"),
            "eye_open": up.get("eye_open"),
        })

    run.artifacts["borderline_kept"] = kept
    run.artifacts["borderline_rejected"] = rejected
    run.artifacts["uncertain_count"] = len(uncertain_paths)
    run.artifacts["uncertain_paths"] = uncertain_detail[:100]

    src = "LLM" if llm_decisions else "规则"
    _step_log(run, f"borderline 裁决({src}): 保留 {kept} 拒绝 {rejected} 不确定 {len(uncertain_paths)}")
    return {"ok": True, "kept": kept, "rejected": rejected, "uncertain": len(uncertain_paths)}

def _dedup_preview_step(run):
    """连拍去重预览：dry_run 分组，产出去重组预览供动作门审核。"""
    _emit_pipeline(run, "step", {"phase": "dedup", "step": "preview", "detail": "连拍去重预览(dry_run)..."})
    folder = run.work_folder
    _step_log(run, "连拍去重: 正在计算图片哈希值(使用评分缓存缩略图,通常数秒完成)...")

    # Direct progress + log callbacks (route dedup internal logs to pipeline UI)
    def _dedup_progress(current, total):
        _emit_pipeline(run, "progress", {"phase": "dedup", "current": current, "total": total})
    def _dedup_log(msg, level="info"):
        _step_log(run, msg, level)
    result = photo_scorer.handle_dedup_burst({
        "folder": folder, "dry_run": True,
        "time_window": int(_step_param(run, "dedup_time_window", 3)),
        "similarity": int(_step_param(run, "dedup_similarity", 5)),
        "on_progress": _dedup_progress,
        "on_log": _dedup_log,
    })
    groups = result.get("groups", 0)
    to_move = result.get("duplicates", 0)
    group_details = result.get("group_details", [])

    run.artifacts["dedup_groups"] = groups
    run.artifacts["dedup_to_move"] = to_move
    run.artifacts["dedup_group_details"] = group_details
    run.artifacts["dedup_applied"] = False
    run.artifacts["dedup_confirmed"] = False

    _step_log(run, f"连拍去重预览: {groups} 组连拍, {to_move} 张可移入 _duplicates/")

    if groups > 0 and run.autonomy in ("L2", "L3"):
        # 动作门：预览结果，等人确认后再执行
        payload = {"groups": groups, "to_move": to_move, "artifacts": run.artifacts}
        summary = f"发现 {groups} 组连拍，建议移走 {to_move} 张到 _duplicates/ 文件夹"

        for i, gd in enumerate(group_details[:5]):
            kept_name = gd.get("kept", "?")
            moved_names = ", ".join(gd.get("moved", [])[:3])
            if len(gd.get("moved", [])) > 3:
                moved_names += f" ...等{len(gd['moved'])}张"
            summary += f"\n  组{i+1}: 留「{kept_name}」, 移走 {moved_names}"

        _save_state(run)
        decision = _await_gate(run, "action", summary, payload,
                               timeout=_step_param(run, "gate_timeout") or None)
        run.artifacts["dedup_confirmed"] = decision.get("approved", False)

    return {"ok": True, "groups": groups, "to_move": to_move}


def _dedup_apply_step(run):
    """连拍去重执行：实际移动文件到 _duplicates/（仅在动作门批准后执行）。"""
    if not run.artifacts.get("dedup_confirmed") or run.artifacts.get("dedup_applied"):
        _step_log(run, "连拍去重: 跳过(未确认或已执行)")
        return {"ok": True, "skipped": "not_confirmed_or_already_applied"}

    _emit_pipeline(run, "step", {"phase": "dedup", "step": "apply", "detail": "执行连拍去重..."})
    _step_log(run, "连拍去重: 正在移动重复照片到 _duplicates/ ...")
    folder = run.work_folder

    def _dedup_apply_progress(current, total):
        _emit_pipeline(run, "progress", {"phase": "dedup", "current": current, "total": total})
    def _dedup_apply_log(msg, level="info"):
        _step_log(run, msg, level)
    result = photo_scorer.handle_dedup_burst({
        "folder": folder, "dry_run": False,
        "time_window": int(_step_param(run, "dedup_time_window", 3)),
        "similarity": int(_step_param(run, "dedup_similarity", 5)),
        "on_progress": _dedup_apply_progress,
        "on_log": _dedup_apply_log,
    })
    moved = result.get("duplicates", 0)
    run.artifacts["dedup_applied"] = True
    run.artifacts["dedup_applied_count"] = moved
    _step_log(run, f"连拍去重执行: {moved} 张移入 _duplicates/")
    return {"ok": True, "applied": moved}

def _cull_step(run):
    """废片过滤(PR2 先用规则,P3 加 LLM borderline)。浏览 eye_open + blur 并标记 reject。"""
    _emit_pipeline(run, "step", {"phase": "cull", "step": "cull", "detail": "废片过滤(规则)..."})
    _step_log(run, "废片过滤: 逐张检查眼睛闭合+模糊+评分阈值...")
    folder = run.work_folder
    mv = photo_scorer.model_version()
    rows = score_cache.get_folder(folder, mv)
    eye_threshold = float(_step_param(run, "eye_reject", 0.15) or 0.15)
    # blur_threshold is reserved for PR3 (blur-based culling not yet implemented)

    rejected, borderline = 0, 0
    total = len(rows)
    eye_det = None
    try:
        from eye_detector import eye_open_score
        eye_det = True
    except Exception:
        eye_det = False

    # Format groups for eye detection:
    #   fast: cv2.imread handles natively, use original path
    #   thumb: HEIC/RAW — cv2 can't decode, use cached 260px JPEG thumb from scoring
    _FAST_EYE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".webp"}
    _THUMB_EYE_EXTS = {".heic", ".heif"} | set(RAW_EXTS)

    # Emit initial progress so the bar shows immediately
    _emit_pipeline(run, "progress", {"phase": "cull", "current": 0, "total": total})

    for i, row in enumerate(rows):
        path = row["path"]
        # blur(如果有,从 score_cache 扩展读取; PR2 先跳过 blur 专检,留 PR3)
        ext = os.path.splitext(path)[1].lower()
        if eye_det and ext in _FAST_EYE_EXTS:
            eos = eye_open_score(path)
        elif eye_det and ext in _THUMB_EYE_EXTS:
            thumb = row.get("thumb_path", "")
            eos = eye_open_score(thumb) if thumb and os.path.exists(thumb) else None
        else:
            eos = None
        if eos is not None and eos < eye_threshold:
                photo_scorer.handle_set_manual_keep({"path": path, "keep": -1})
                rejected += 1
                continue
        score = row.get("score") or 0
        threshold = run.artifacts.get("threshold_used", 6.0)
        if score < threshold - 0.5:  # PR2 规则：远低于阈值的直接 reject; borderline 留 PR3
            photo_scorer.handle_set_manual_keep({"path": path, "keep": -1})
            rejected += 1
        elif score < threshold + 0.3 and score >= threshold - 0.5:
            borderline += 1
        # Emit progress every 5 photos
        if (i + 1) % 5 == 0 or i == total - 1:
            _emit_pipeline(run, "progress", {"phase": "cull", "current": i + 1, "total": total})

    run.artifacts["reject"] = rejected
    run.artifacts["borderline"] = borderline
    _emit_pipeline(run, "progress", {"phase": "cull", "done": total, "total": total})
    _step_log(run, f"废片过滤完成: reject {rejected} 张, borderline {borderline} 张")
    return {"ok": True, "rejected": rejected, "borderline": borderline}

def _select_step(run):
    """出 keeper：阈值以上 + 未被 reject 的 = keep 集。产 keep_paths 供审核视图。"""
    _emit_pipeline(run, "step", {"phase": "select", "step": "select", "detail": "出 keeper..."})
    _step_log(run, "出 keeper: 根据阈值+手动标记分类保留/拒绝...")
    folder = run.work_folder
    mv = photo_scorer.model_version()
    threshold = run.artifacts.get("threshold_used", 6.0)

    # Direct SQL query (bypasses get_folder's mtime/size validation which can
    # filter out rows if files were moved by dedup or mtime changed slightly).
    # We need ALL scored rows with their thumb_path for the review grid.
    import sqlite3 as _sql
    rows = []
    try:
        from score_cache import _db_path as _scdb
        _conn = _sql.connect(_scdb, timeout=10)
        _conn.row_factory = _sql.Row
        _raw = _conn.execute(
            "SELECT path, score, thumb_path, manual_keep FROM scores WHERE model_version=? AND (path LIKE ? OR path LIKE ?)",
            (mv, folder.replace('\\', '/') + '/%', folder + os.sep + '%')).fetchall()
        _conn.close()
        for _r in _raw:
            rows.append({"path": _r["path"], "score": _r["score"],
                         "thumb_path": _r["thumb_path"] or "",
                         "manual_keep": _r["manual_keep"]})
    except Exception as e:
        _step_log(run, f"出 keeper: 直接查询失败({e}), 回退 get_folder", "warn")
        rows = score_cache.get_folder(folder, mv)

    # Diagnostic: check thumbnail availability
    _has_thumb = sum(1 for r in rows if r.get("thumb_path"))
    _sample_tp = ""
    _thumb_exists = 0
    if rows:
        _sample_tp = rows[0].get("thumb_path", "")
        if _sample_tp:
            _thumb_exists = 1 if os.path.exists(_sample_tp) else 0
    _step_log(run, f"出 keeper诊断: 查询返回 {len(rows)} 行, "
                f"有thumb_path {_has_thumb} 行, 样本thumb存在={_thumb_exists}, "
                f"sample={_sample_tp[:80] if _sample_tp else 'N/A'}")

    keep_paths, keep_scores, reject_paths = [], [], []
    total = len(rows)
    for i, row in enumerate(rows):
        manual = row.get("manual_keep", 0)
        score = row.get("score") or 0
        if manual == -1:
            reject_paths.append({"path": row["path"], "score": score, "thumb": row.get("thumb_path", "")})
            continue
        if manual == 1 or score >= threshold:
            keep_paths.append({"path": row["path"], "score": score, "thumb": row.get("thumb_path", ""), "manual": manual == 1})
            keep_scores.append(score)
        # Emit progress every 10 photos
        if (i + 1) % 10 == 0 or i == total - 1:
            _emit_pipeline(run, "progress", {"phase": "select", "current": i + 1, "total": total})

    run.artifacts["keep"] = len(keep_paths)
    run.artifacts["keep_paths"] = keep_paths[:500]   # 最多传 500 给前端
    run.artifacts["reject_paths"] = reject_paths[:200]
    run.artifacts["keep_avg_score"] = round(sum(keep_scores) / len(keep_scores), 1) if keep_scores else 0
    # 决策摘要(给审核视图)
    run.artifacts["summary"] = {
        "scanned": run.artifacts.get("scanned", 0),
        "scored": run.artifacts.get("scored", 0),
        "threshold": threshold,
        "dedup_removed": run.artifacts.get("dedup_to_move", 0),
        "rejected": len(reject_paths),
        "borderline": run.artifacts.get("borderline", 0),
        "uncertain": run.artifacts.get("uncertain_count", 0),
        "keep": len(keep_paths),
        "avg_score": run.artifacts["keep_avg_score"],
    }
    _step_log(run, f"出 keeper: {len(keep_paths)} 张(阈值={threshold}, 均分 {run.artifacts.get('keep_avg_score','?')})")

    # ★ 飞轮采集：每个keep/reject决策(limit 200 to avoid overloading queue)
    _captured = 0
    for item in keep_paths[:200]:
        if _flywheel_available and len(keep_paths) > 0:
            _push_flywheel(run, item["path"], 1, "select_keep")
            _captured += 1
    for item in reject_paths[:200]:
        if _flywheel_available and len(reject_paths) > 0:
            _push_flywheel(run, item["path"], 0, "select_reject")
            _captured += 1
    if _captured:
        _step_log(run, f"飞轮采集 {_captured} 条决策(opt-in gated)")

    return {"ok": True, "keep": len(keep_paths)}

# ── 订阅 score_progress 事件 → 转发为 pipeline_progress（进度树）──

_score_unsub = None

def _subscribe_score_progress(run, phase_label="score"):
    """订阅 photo_scorer 的 score_progress 事件，转发给 pipeline UI"""
    def _fwd(event_name, data):
        if event_name != "score_progress":
            return
        if run.status in ("done", "error", "canceled", "rejected"):
            return
        _emit_pipeline(run, "progress", {
            "phase": phase_label,
            "current": data.get("current", 0),
            "total": data.get("total", 0),
            "last_file": data.get("last_file", ""),
            "last_score": data.get("last_score", None),
        })
    try:
        from service import subscribe_events
        return subscribe_events(_fwd)
    except Exception:
        return None

# ── 管线模板定义 ──

# 每种模板的 phases 列表;每个 phase = {id, title, steps}
# 每个 step = {name, fn: (run)->dict}

CULL_PIPELINE = {
    "id": "cull_pipeline",
    "title": "初筛管线",
    "phases": [
        {"id": "import", "title": "导入编目",
         "steps": [{"name": "scan", "fn": _scan_step}]},
        {"id": "score", "title": "AI 评分",
         "steps": [
             {"name": "score", "fn": _score_step},
             {"name": "threshold", "fn": _threshold_step},
         ]},
        {"id": "dedup", "title": "连拍去重",
         "steps": [
             {"name": "dedup_preview", "fn": _dedup_preview_step},
             {"name": "dedup_apply", "fn": _dedup_apply_step},
         ]},
        {"id": "cull", "title": "废片过滤",
         "steps": [
             {"name": "cull", "fn": _cull_step},
             {"name": "cull_borderline", "fn": _cull_borderline_step},
         ]},
        {"id": "select", "title": "出 keeper",
         "steps": [{"name": "select", "fn": _select_step}]},
    ],
}

# 交付链/round-2 步骤 fn 惰性引入（pipeline_tool_steps 顶层只 import os，无循环导入）
from pipeline_tool_steps import (  # noqa: E402
    export_keepers, watermark_keepers, compress_keepers, upload_keepers,
    create_cloud_album,
    grade_keepers, multi_size_keepers, build_customer_preview, delivery_report,
    auto_retouch_keepers,
    export_working_set, wait_retouch, import_retouched, final_select, review_final,
)

# Lightroom 两轮筛选: 粗筛 → LR 修图 → 终选 → 交付
# wait_retouch / review_final 的 gate 是声明式的（step.gate），由主循环在调用 fn 前等人
LR_TWO_ROUND = {
    "id": "lr_two_round",
    "title": "Lightroom 两轮筛选",
    "description": "粗筛 → LR 修图 → 终选 → 上传交付（专业修图工作流）",
    "phases": [
        {"id": "import", "title": "导入编目",
         "steps": [{"name": "scan", "fn": _scan_step}]},
        {"id": "score", "title": "AI 评分",
         "steps": [
             {"name": "score", "fn": _score_step},
             {"name": "threshold", "fn": _threshold_step},
         ]},
        {"id": "rough", "title": "粗筛",
         "steps": [
             {"name": "dedup_preview", "fn": _dedup_preview_step},
             {"name": "dedup_apply", "fn": _dedup_apply_step},
             {"name": "cull", "fn": _cull_step},
             {"name": "cull_borderline", "fn": _cull_borderline_step},
             {"name": "select", "fn": _select_step},
         ]},
        {"id": "handoff", "title": "修图交接",
         "steps": [
             {"name": "export_working_set", "fn": export_working_set},
             {"name": "wait_retouch", "fn": wait_retouch,
              "gate": {"type": "action",
                       "message": "请在 Lightroom 中完成修图并标注星级（★≥3 视为交付），完成后点「继续」",
                       "timeout": 86400}},
         ]},
        {"id": "final", "title": "终选",
         "steps": [
             {"name": "import_retouched", "fn": import_retouched},
             {"name": "final_select", "fn": final_select},
         ]},
        {"id": "final_review", "title": "终选确认",
         "steps": [
             {"name": "review_final", "fn": review_final,
              "gate": {"type": "action",
                       "message": "确认终选 keeper 后交付（导出/水印/上传/建相册）",
                       "timeout": 7200}},
         ]},
        {"id": "deliver", "title": "交付",
         "steps": [
             {"name": "export_keepers", "fn": export_keepers},
             {"name": "auto_retouch_keepers", "fn": auto_retouch_keepers},
             {"name": "grade_keepers", "fn": grade_keepers},
             {"name": "multi_size_keepers", "fn": multi_size_keepers},
             {"name": "watermark_keepers", "fn": watermark_keepers},
             {"name": "compress_keepers", "fn": compress_keepers},
             {"name": "upload_keepers", "fn": upload_keepers},
             {"name": "build_customer_preview", "fn": build_customer_preview},
             {"name": "delivery_report", "fn": delivery_report},
             {"name": "create_cloud_album", "fn": create_cloud_album},
         ]},
    ],
}

TEMPLATES = {"cull_pipeline": CULL_PIPELINE, "lr_two_round": LR_TWO_ROUND}


def _resolve_template(template_id):
    """解析模板 ID → 运行时的 phases 列表（含 fn 引用）。

    优先级：内置 TEMPLATES > 磁盘自定义模板 > 回退到 CULL_PIPELINE。
    """
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]

    # 尝试从磁盘加载自定义模板
    try:
        from pipeline_templates import load_custom_template
        custom = load_custom_template(template_id)
        if custom:
            return custom
    except Exception:
        pass

    return CULL_PIPELINE


def _is_valid_template(template_id):
    """检查模板 ID 是否有效（内置 + 自定义）。"""
    if template_id in TEMPLATES:
        return True
    try:
        from pipeline_templates import load_custom_template
        return load_custom_template(template_id) is not None
    except Exception:
        return False

def _report_pipeline_usage(run):
    """Record pipeline token usage locally + report to server (fire-and-forget).
    Shares the same agent_usage.json window with the AI agent."""
    tokens = run.budget.get("spent_tokens", 0)
    if tokens <= 0:
        return

    # ── 1. Record locally (shared with agent's usage history) ──
    try:
        import json as _j, time as _t, os as _os, threading as _th
        from service import _get_config_dir
        usage_dir = _get_config_dir()
        _os.makedirs(usage_dir, exist_ok=True)
        usage_file = _os.path.join(usage_dir, "agent_usage.json")

        rec = {
            "ts": int(_t.time()),
            "session_id": run.run_id,
            "model": "pipeline:" + (run.template or "cull_pipeline"),
            "task_name": run.goal or "",
            "headless": False,
            "prompt": f"数字员工-{run.template}",
            "iterations": run.budget.get("steps", 0),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": int(tokens),
            "duration_ms": int((_t.time() - run.start_time) * 1000),
        }
        # Use the same lock as agent_module (best-effort import)
        _lock = None
        try:
            from agent_module import _usage_lock
            _lock = _usage_lock
        except Exception:
            _lock = _th.Lock()
        with _lock:
            data = {}
            try:
                if _os.path.exists(usage_file):
                    with open(usage_file, "r", encoding="utf-8") as f:
                        data = _j.load(f)
            except Exception:
                data = {}
            runs = data.get("runs", []) if isinstance(data, dict) else []
            if not isinstance(runs, list):
                runs = []
            runs.append(rec)
            if len(runs) > 500:
                runs = runs[-500:]
            data["runs"] = runs
            try:
                from utils import atomic_write_json
                atomic_write_json(usage_file, data)
            except Exception:
                with open(usage_file, "w", encoding="utf-8") as f:
                    _j.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # best-effort

    # ── 2. Report to server ──
    cloud_token = getattr(run, "cloud_token", "") or ""
    if not cloud_token:
        return
    try:
        from agent_module import _official_config
        # proxy 模式下服务端已按真实 usage 扣量, 不再上报(避免双计)
        if not _official_config.get("api_key") or _official_config.get("proxy"):
            return
    except Exception:
        return
    try:
        import urllib.request as _ur
        body = _j.dumps({"tokens": {"total": int(tokens)}}).encode("utf-8")
        url = "https://studio.dwphoto.top/api/llm/usage-report"
        req = _ur.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "x-studio-token": cloud_token,
        }, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            srv_data = _j.loads(resp.read().decode("utf-8") or "{}")
        remaining = srv_data.get("remaining", -1)
        _step_log(run, f"Token 用量已上报: {tokens} tokens (剩余 {remaining})")
    except Exception:
        pass  # best-effort; reporting failure should not block

# ═══════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════

# 全局单例，与 agent 互斥
_active_run = None
_active_lock = threading.Lock()

def _get_active_run():
    with _active_lock:
        return _active_run

def _clear_active_run(run):
    global _active_run
    with _active_lock:
        if _active_run is run:
            _active_run = None

def _run_pipeline(run):
    """后台线程入口。"""
    global _active_run
    try:
        tmpl = _resolve_template(run.template)
        phases = tmpl["phases"]

        # ── 崩溃恢复：已有 run_state 且未完成 → 从上次断点续 ──
        prev = _load_state(run.work_folder, run.run_id)
        if prev and prev["status"] not in ("done", "error", "canceled", "rejected"):
            run.phase_idx = prev["phase_idx"]
            run.step_idx = prev["step_idx"]
            run.budget = prev.get("budget", run.budget)
            run.stall = prev.get("stall", run.stall)
            run.artifacts = prev.get("artifacts", {})
            run.status = prev.get("status", "paused")
            _emit_pipeline(run, "log",
                           {"level": "info",
                            "message": f"从断点续跑 (phase={run.phase_idx} step={run.step_idx})"})

        run.status = "running"
        _emit_pipeline(run, "started", {"template": run.template, "goal": run.goal})

        for pi in range(run.phase_idx, len(phases)):
            phase = phases[pi]
            run.phase_idx = pi
            _emit_pipeline(run, "phase_start", {"id": phase["id"], "title": phase["title"]})

            steps = phase["steps"]
            for si in range(run.step_idx if pi == run.phase_idx else 0, len(steps)):
                step = steps[si]
                run.step_idx = si

                if run.cancel_event.is_set():
                    run.status = "canceled"
                    _save_state(run)
                    _emit_pipeline(run, "done", {"summary": "已取消"})
                    return

                # 处理暂停
                if run.pause_event.is_set():
                    run.status = "paused"
                    _save_state(run)
                    _emit_pipeline(run, "paused", {})
                    # Event.wait() 语义是"等 set"，不能用来"暂停期间阻塞"；
                    # 小切片轮询(同 _score_progress)，取消可即时生效
                    while run.pause_event.is_set() and not run.cancel_event.is_set():
                        time.sleep(0.2)
                    if run.cancel_event.is_set():  # 暂停期间点了取消
                        run.status = "canceled"
                        _save_state(run)
                        _emit_pipeline(run, "done", {"summary": "已取消"})
                        return
                    run.status = "running"
                    _emit_pipeline(run, "resumed", {})
                    run.start_time = time.time()  # 重新计时（成本门用）

                # ── 成本门（步前检查）──
                run.budget["steps"] = run.budget.get("steps", 0) + 1
                if not _handle_cost_gate(run):
                    # 预算门拒绝/取消 → 终止；否则回到循环顶（会检查 cancel/pause）
                    if run.status in ("canceled", "error"):
                        return
                    continue

                # ── Schema v2: 合并本步参数（edited_params > step.params > run.params）──
                merged = dict(run.params)
                if step.get("params"):
                    merged.update(step["params"])
                run.step_params = merged

                # ── Schema v2: condition 为 false → 跳过（不计停滞门/err_repeat）──
                cond = step.get("condition")
                if cond:
                    try:
                        cond_ok = _eval_condition(run, cond)
                    except ValueError as e:
                        # 校验期应已挡掉；运行期防御：条件非法按不满足处理
                        _step_log(run, f"步骤 {step['name']} 条件非法({e}),跳过", "warn")
                        cond_ok = False
                    if not cond_ok:
                        _emit_pipeline(run, "step_skip", {
                            "phase": phase["id"], "step": step["name"],
                            "reason": "condition_false"})
                        _step_log(run, f"步骤 {step['name']} 跳过(条件不满足)")
                        _save_state(run)
                        continue

                # ── Schema v2: 声明式动作门（approve 执行 / reject 跳过 / edit 改参执行）──
                gate = step.get("gate")
                if gate:
                    _save_state(run)  # 门阻塞期间崩溃也能从本步续跑
                    decision = _await_gate(
                        run, gate.get("type") or "action",
                        gate.get("message") or f"确认执行步骤 {step['name']}",
                        payload={"phase": phase["id"], "step": step["name"],
                                 "message": gate.get("message", "")},
                        timeout=gate.get("timeout") or GATE_TIMEOUT)
                    if not decision.get("approved"):
                        _emit_pipeline(run, "step_skip", {
                            "phase": phase["id"], "step": step["name"],
                            "reason": "gate_rejected"})
                        _step_log(run, f"步骤 {step['name']} 被门拒绝/超时,跳过")
                        _save_state(run)
                        continue
                    edited = decision.get("edited_params")
                    if edited:
                        # edit：把人工改参并入本步参数（优先级最高）
                        run.step_params.update(edited)
                        _step_log(run, f"步骤 {step['name']} 接受人工改参: {list(edited)}")

                # ── 执行一步 ──
                sig_before = _progress_sig(run)
                _emit_pipeline(run, "step_start", {
                    "phase": phase["id"], "step": step["name"], "step_idx": si})
                try:
                    result = step["fn"](run)
                except Exception as e:
                    err_msg = str(e)
                    # ── err_repeat 门：同一错误连续重复 ERR_REPEAT 次才停，
                    #    单次/不同错误先容错续跑(由停滞门兜底) ──
                    if err_msg == run.stall.get("last_error"):
                        run.stall["err_repeat"] = run.stall.get("err_repeat", 0) + 1
                    else:
                        run.stall["last_error"] = err_msg
                        run.stall["err_repeat"] = 1
                    if run.stall["err_repeat"] >= ERR_REPEAT:
                        run.status = "error"
                        run.run_log.append({"action": f"{phase['id']}/{step['name']}",
                                            "error": err_msg})
                        _save_state(run)
                        _emit_pipeline(run, "error",
                                       {"phase": phase["id"], "step": step["name"],
                                        "error": err_msg})
                        return
                    _step_log(run, f"步骤 {step['name']} 失败(连续第 {run.stall['err_repeat']} 次,"
                                   f" 同错重复 {ERR_REPEAT} 次将中止): {err_msg}")
                    _save_state(run)
                    continue

                run.stall["err_repeat"] = 0  # 步骤成功,重置同错计数

                # ── 停滞门 ──
                sig_after = _progress_sig(run)
                if sig_after == sig_before:
                    run.stall["no_progress_rounds"] += 1
                    if run.stall["no_progress_rounds"] >= STALL_ROUNDS:
                        run.status = "error"
                        _save_state(run)
                        _emit_pipeline(run, "error",
                                       {"error": "停滞：连续多步无可验证进展"})
                        return
                else:
                    run.stall["no_progress_rounds"] = 0
                run.stall["last_progress_sig"] = sig_after

                run.budget["spent_s"] = int(time.time() - run.start_time)
                _save_state(run)
                run.run_log.append({"action": f"{phase['id']}/{step['name']}", "ok": True})

            # 阶段结束
            if run.cancel_event.is_set():
                # 取消发生在本阶段最后一步(或步骤内的门)上：steps 循环已走出，
                # 循环顶的取消检查不会再触发，这里兜底收尾为 canceled
                run.status = "canceled"
                _save_state(run)
                _emit_pipeline(run, "done", {"summary": "已取消"})
                return
            run.step_idx = 0
            _emit_pipeline(run, "phase_done", {"id": phase["id"], "title": phase["title"]})
            _save_state(run)

        # ── 阶段门：管线末，按自主度决定是否停等人审 ──
        if run.autonomy in ("L2", "L3"):
            # L2/L3：管线末停，等人审 keeper 产物
            phase_summary = f"初筛完成: 扫描 {run.artifacts.get('scanned',0)} 张, "
            phase_summary += f"评分 {run.artifacts.get('scored',0)} 张, "
            phase_summary += f"去重 {run.artifacts.get('dedup_to_move',0)} 张, "
            phase_summary += f"拒绝 {run.artifacts.get('reject',0)} 张, "
            phase_summary += f"选出 keeper {run.artifacts.get('keep',0)} 张"
            uc = run.artifacts.get('uncertain_count', 0)
            if uc:
                phase_summary += f", {uc} 张需人工审核(已暂保留)"
            # 终审门：与动作门同一机制(_await_gate 独立 Event)。
            # 只能由显式 decision 解除——pause/resume 不再暗含批准；
            # 阻塞期间 run.status 保持 running，pending_gate 由 get_state 返回，
            # 页面重进可重渲染终审 UI；取消可唤醒并收尾为 canceled。
            _save_state(run)  # 门阻塞期间崩溃：resume 后重新走到本门
            decision = _await_gate(run, "phase", phase_summary,
                                   payload={"artifacts": run.artifacts})
            if run.cancel_event.is_set():
                run.status = "canceled"
                _save_state(run)
                _emit_pipeline(run, "done", {"summary": "已取消"})
                return
            if not decision.get("approved"):
                # 用户拒绝 = 这次管线结果不要了：复原(_duplicates 移回、清除标记、
                # 删断点 state)，以独立终态 rejected 结束，不发表示完成的 done。
                # 注意：复原删除 run_state 后不得再 _save_state。
                run.status = "rejected"
                run.artifacts["review_approved"] = False
                run.artifacts["review_rejected"] = True
                _step_log(run, "用户拒绝管线结果，正在复原...")
                moved_back, reset_count, errors = _restore_folder_contents(run.work_folder)
                _report_pipeline_usage(run)  # token 已真实消耗，拒绝不影响配额上报
                msg = f"已拒绝并复原: 移回 {moved_back} 张照片, 清除 {reset_count} 个标记"
                if errors:
                    msg += f", {len(errors)} 个错误"
                _emit_pipeline(run, "rejected", {"summary": msg})
                return
            run.status = "running"
            _emit_pipeline(run, "resumed", {"reason": "final_review_approved"})

        # ── 上报官方 API Token 用量 ──
        _report_pipeline_usage(run)

        # ── 全部阶段完成 ──
        run.status = "done"
        _save_state(run)
        summary = f"初筛完成: 扫描 {run.artifacts.get('scanned', 0)} 张, "
        summary += f"评分 {run.artifacts.get('scored', 0)} 张"
        _emit_pipeline(run, "done", {
            "summary": summary,
            "artifacts": run.artifacts,
        })

    except Exception as e:
        run.status = "error"
        _save_state(run)
        _emit_pipeline(run, "error", {"error": str(e)})
    finally:
        _clear_active_run(run)

# ═══════════════════════════════════════════════════════════════════════
# IPC handlers (registered in service.py)
# ═══════════════════════════════════════════════════════════════════════

def handle_pipeline_start(params, _from_agent=False):
    """POST /api 等效：启动管线。参数见 M1_PIPELINE_TECH_SPEC §4。

    _from_agent：仅供进程内 Python 调用（Agent 桥接）。为 True 时跳过
    "agent 活跃则拒绝"的互斥检查；IPC 分发不会传该参数，恒为 False。"""
    global _active_run

    work_folder = (params.get("work_folder") or "").strip()
    template = (params.get("template") or "cull_pipeline").strip()
    autonomy = (params.get("autonomy") or "L3").strip()
    goal = (params.get("goal") or "").strip()

    if not work_folder or not os.path.isdir(work_folder):
        return {"error": f"工作文件夹无效: {work_folder}"}
    if not _is_valid_template(template):
        return {"error": f"未知模板: {template}；内置: {list(TEMPLATES)}"}

    # 互斥：检查 agent 是否活跃（Agent 桥接调用 _from_agent=True 时跳过）
    try:
        from agent_module import _get_active, _load_agent_config
        if not _from_agent and _get_active() is not None:
            return {"error": "agent_busy", "detail": "Agent 任务正在运行，请等它结束后再启动数字员工"}
    except Exception:
        pass

    # ── 预检：LLM 是否已配置 ──
    # 数字员工依赖大模型做决策(定阈值、borderline裁决)；未配置 API key
    # 时阈值会退回纯规则、弱化智能决策，这不是"数字员工"该有的行为。
    # 强制要求配置后才允许启动——给清晰的错误而非静默降级。
    try:
        cfg = _load_agent_config()
    except Exception:
        cfg = {}
    base_url = (cfg.get("base_url") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    # 同时检查官方 key（_official_config 有内存态 key 时也算已配置）
    has_official = False
    try:
        from agent_module import _official_config
        has_official = bool(_official_config.get("api_key"))
    except Exception:
        pass
    # 本地模型(内置 llama-server + Qwen3-VL)已就绪也算已配置——数字员工可完全离线运行
    has_local = False
    try:
        from local_llm import local_llm
        has_local = bool(cfg.get("use_local", True)) and local_llm.is_model_present() \
            and local_llm._server_exe() is not None
    except Exception:
        pass
    if (not base_url or not api_key) and not has_official and not has_local:
        return {"error": "llm_not_configured",
                "detail": "数字员工需大模型做决策。请先在 AI 助手页启用本地模型(已就绪)，或配置个人云端 API Key"}

    with _active_lock:
        if _active_run is not None:
            return {"error": "pipeline_busy", "detail": "已有数字员工任务在运行"}
        run_id = uuid.uuid4().hex[:12]
        run = PipelineRun(
            run_id, work_folder, template, autonomy, goal,
            params.get("pipeline_params") or params,
        )
        # Store cloud_token for usage reporting (official key quota tracking)
        run.cloud_token = (params.get("cloud_token") or "").strip()
        _active_run = run

    t = threading.Thread(target=_run_pipeline, args=(run,), daemon=True)
    t.start()
    return {"ok": True, "run_id": run_id}

def handle_pipeline_pause(params):
    run = _get_active_run()
    if not run:
        return {"error": "no active pipeline"}
    if run.pause_event.is_set():
        return {"ok": True, "status": "already_paused"}
    run.pause_event.set()
    return {"ok": True}

def handle_pipeline_resume(params):
    run = _get_active_run()
    if not run:
        # 尝试从断点新建 runner（进程重启后的续跑）
        return _resume_from_state(params)
    if not run.pause_event.is_set():
        return {"ok": True, "status": "already_running"}
    run.pause_event.clear()
    return {"ok": True}

def handle_pipeline_gate_resolve(params):
    """人回应了门：批准/拒绝/改参。复用 handle_agent_approve 的 Event 签名模式。"""
    gate_id = params.get("gate_id", "")
    decision = params.get("decision", "reject")  # approve|reject|edit
    edited_params = params.get("params")
    run = _get_active_run()
    if not run:
        return {"error": "no active pipeline"}
    entry = run.pending_gates.get(gate_id)
    if entry:
        entry["decision"] = {"approved": decision in ("approve", "edit"),
                             "edited_params": edited_params if decision == "edit" else None}
        if entry["event"] is not None:
            entry["event"].set()
    else:
        # 兜底：门已不在 pending_gates(如取消竞态/旧流程残留)，记录 decision 即可。
        approved = decision in ("approve", "edit")
        run.pending_gates[gate_id] = {"event": None, "decision": {
            "approved": approved,
            "edited_params": edited_params if decision == "edit" else None}}
        # Track review outcome for override (data flywheel gold signal)
        if gate_id == "final_review":
            run.artifacts["review_approved"] = approved
    # 同时解除暂停：门解决后若用户此前按过暂停，管线应继续推进。
    # (门本身由各自的 Event 解除，pause_event 不再承担门语义)
    run.pause_event.clear()
    return {"ok": True}

def handle_pipeline_cancel(params):
    run = _get_active_run()
    if not run:
        # 清理残留 run_state
        wf = (params.get("work_folder") or "").strip()
        if wf:
            p = _state_path(wf)
            if os.path.isfile(p):
                st = _load_state(wf)
                if st and st["status"] not in ("done", "canceled", "rejected"):
                    st["status"] = "canceled"
                    tmp = p + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, p)
        return {"ok": True, "cleaned": True}
    run.cancel_event.set()
    run.pause_event.clear()  # 如果暂停中,先解除再取消
    # 唤醒所有挂起的门：否则 _await_gate 的 event.wait(None) 永不返回，
    # 管线线程退不出、_active_run 不释放(参考 agent_module.handle_agent_cancel)。
    # 主循环醒后走 cancel_event 检查，把 run 收尾为 canceled 并释放 _active_run。
    for entry in list(run.pending_gates.values()):
        if entry.get("decision") is None:
            entry["decision"] = {"approved": False, "reason": "canceled"}
        if entry.get("event") is not None:
            entry["event"].set()
    return {"ok": True}

def handle_pipeline_get_state(params):
    run = _get_active_run()
    wf = (params.get("work_folder") or "").strip()
    # 活跃 run 属于其他文件夹：不把它的状态串到本文件夹视图（按无活跃 run 处理）
    if run and wf and os.path.normcase(os.path.normpath(run.work_folder)) != \
            os.path.normcase(os.path.normpath(wf)):
        run = None
    if run:
        st = {
            "run_id": run.run_id, "template": run.template,
            "autonomy": run.autonomy, "goal": run.goal,
            "status": run.status,
            "phase_idx": run.phase_idx, "step_idx": run.step_idx,
            "budget": run.budget, "artifacts": run.artifacts,
            "active": True,  # 进程内活跃 run（区别于磁盘残留 state）
        }
        # 门(动作/预算/终审)挂起中：一并返回，页面重进可重渲染门/终审 UI
        if run.pending_gate_info:
            st["pending_gate"] = run.pending_gate_info
        return st
    # 没有活跃 run → 查磁盘残留(process 重启后残留的 run_state)
    if wf:
        st = _load_state(wf)
        if st:
            # 非终态的残留状态: 进程死了但 state 写的是 running/paused/gating → 标 paused
            if st.get("status") not in ("done", "error", "canceled", "rejected", "paused"):
                st["status"] = "paused"  # stale, resumable
            return st
    return {"status": "idle"}

def _resume_from_state(params):
    """进程重启后从 run_state 续跑。"""
    global _active_run
    work_folder = (params.get("work_folder") or "").strip()
    if not work_folder or not os.path.isdir(work_folder):
        return {"error": f"工作文件夹无效: {work_folder}"}

    prev = _load_state(work_folder)
    if not prev:
        return {"error": "no_state", "detail": "该文件夹无未完成的管线"}
    # 终态(done/error/canceled)不可续跑——否则 _run_pipeline 会忽略 prev 从 phase 0 重跑
    if prev.get("status") in ("done", "error", "canceled", "rejected"):
        return {"error": "already_finished", "detail": "该管线已结束，如需重跑请重新开始"}

    # ── 模板快照对比：state 落盘后模板被改过 → 断点位置可能错位，拒绝续跑。
    #    不清除 state（用户可自行选择"重新开始"覆盖）。老 state 无 phases 字段 → 跳过对比。
    saved_phases = prev.get("phases")
    if saved_phases is not None:
        cur_phases = _phases_snapshot(prev["template"])
        if cur_phases is None or cur_phases != saved_phases:
            return {"error": "template_changed",
                    "detail": "模板已变更，请重新开始"}

    # 互斥
    try:
        from agent_module import _get_active
        if _get_active() is not None:
            return {"error": "agent_busy", "detail": "Agent 任务正在运行"}
    except Exception:
        pass
    with _active_lock:
        if _active_run is not None:
            return {"error": "pipeline_busy"}
        run = PipelineRun(
            prev["run_id"], work_folder,
            prev["template"], prev["autonomy"],
            prev["goal"], prev.get("params", {}),
        )
        # 同正常启动：恢复 cloud_token 供 _report_pipeline_usage 服务端上报
        run.cloud_token = (params.get("cloud_token") or "").strip()
        _active_run = run

    t = threading.Thread(target=_run_pipeline, args=(run,), daemon=True)
    t.start()
    return {"ok": True, "run_id": run.run_id, "resumed": True}

def _restore_folder_contents(folder):
    """复原核心：_duplicates/ 移回原目录 + 清除手动标记 + 删除 run_state。
    handle_pipeline_restore 与终审门 reject 共用。返回 (moved_back, reset_count, errors)。"""
    import shutil as _shutil
    moved_back = 0
    errors = []

    # 1. 把 _duplicates/ 里的文件移回原目录
    dup_dir = os.path.join(folder, "_duplicates")
    if os.path.isdir(dup_dir):
        for fname in os.listdir(dup_dir):
            src = os.path.join(dup_dir, fname)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(folder, fname)
            # 处理同名冲突（原文件还在的情况）
            if os.path.exists(dst):
                base, ext = os.path.splitext(fname)
                i = 1
                while os.path.exists(dst):
                    dst = os.path.join(folder, f"{base}_restored{i}{ext}")
                    i += 1
            try:
                _shutil.move(src, dst)
                moved_back += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")
        # 删除空的 _duplicates 文件夹
        try:
            if not os.listdir(dup_dir):
                os.rmdir(dup_dir)
        except Exception:
            pass

    # 2. 清除 score_cache 里的手动标记 (manual_keep -> 0)
    reset_count = 0
    try:
        import sqlite3 as _sql
        from score_cache import _db_path as _scdb
        mv = photo_scorer.model_version()
        conn = _sql.connect(_scdb, timeout=10)
        cur = conn.execute(
            "UPDATE scores SET manual_keep=0 WHERE model_version=? AND (path LIKE ? OR path LIKE ?)",
            (mv, folder.replace('\\', '/') + '/%', folder + os.sep + '%'))
        reset_count = cur.rowcount
        conn.commit()
        conn.close()
    except Exception as e:
        errors.append(f"清除标记失败: {e}")

    # 3. 删除 run_state.json
    state_path = _state_path(folder)
    if os.path.isfile(state_path):
        try:
            os.remove(state_path)
        except Exception as e:
            errors.append(f"删除状态文件失败: {e}")

    # 文件被移回原目录 → 使目录列表缓存失效（#106）
    if moved_back:
        try:
            photo_scorer.invalidate_dir_cache(folder)
        except Exception:
            pass

    return moved_back, reset_count, errors

def handle_pipeline_restore(params):
    """恢复原样：把 _duplicates/ 里的文件移回原目录，清除手动标记，删除 run_state。"""
    folder = (params.get("work_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        return {"error": f"无效文件夹: {folder}"}
    if _get_active_run() is not None:
        return {"error": "pipeline_busy", "detail": "数字员工正在运行,请先取消或等待完成后再恢复"}

    moved_back, reset_count, errors = _restore_folder_contents(folder)
    msg = f"恢复完成: 移回 {moved_back} 张照片, 清除 {reset_count} 个标记"
    if errors:
        msg += f", {len(errors)} 个错误"
    send_log(msg, "success")
    return {"ok": True, "moved_back": moved_back, "reset_count": reset_count,
            "errors": errors[:10], "message": msg}

# ═══════════════════════════════════════════════════════════════════════
# 工作流模板管理 handlers
# ═══════════════════════════════════════════════════════════════════════

def handle_list_templates(params):
    """返回所有可用模板（内置 + 自定义）。"""
    try:
        from pipeline_templates import list_templates as _list
        templates = _list(builtin_templates=TEMPLATES)
        return {"templates": templates}
    except Exception as e:
        return {"error": str(e)}


def handle_get_available_steps(params):
    """返回步骤注册表中所有可用步骤的元数据。"""
    try:
        from step_registry import get_available_steps
        return {"steps": get_available_steps()}
    except Exception as e:
        return {"error": str(e)}


def handle_save_template(params):
    """保存/更新一个自定义管线模板。"""
    template_data = params.get("template", {})
    if not template_data:
        return {"error": "缺少模板数据（params.template）"}
    # 编辑器打开时的原模板 ID：改 ID 保存时由 save_template 删除旧文件
    original_id = (params.get("original_id") or "").strip() or None

    try:
        from pipeline_templates import save_template
        result = save_template(template_data, original_id=original_id)
        return result
    except Exception as e:
        return {"error": str(e)}


def handle_delete_template(params):
    """删除一个自定义管线模板。"""
    tid = (params.get("template_id") or "").strip()
    if not tid:
        return {"error": "缺少 template_id"}

    try:
        from pipeline_templates import delete_template
        if delete_template(tid):
            return {"ok": True}
        return {"error": "模板不存在或为内置模板（不可删除）"}
    except Exception as e:
        return {"error": str(e)}


def handle_clone_template(params):
    """克隆一个模板（内置或自定义）为新的自定义模板。"""
    source_id = (params.get("source_template_id") or "").strip()
    new_id = (params.get("new_id") or "").strip()
    new_title = (params.get("new_title") or "").strip() or None

    if not source_id:
        return {"error": "缺少 source_template_id"}
    if not new_id:
        return {"error": "缺少 new_id"}

    try:
        from pipeline_templates import clone_template
        result = clone_template(source_id, new_id, new_title)
        return result
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════════

def register_handlers():
    cmds = [
        # ── 管线执行 ──
        ("pipeline_start", handle_pipeline_start),
        ("pipeline_pause", handle_pipeline_pause),
        ("pipeline_resume", handle_pipeline_resume),
        ("pipeline_cancel", handle_pipeline_cancel),
        ("pipeline_get_state", handle_pipeline_get_state),
        ("pipeline_gate_resolve", handle_pipeline_gate_resolve),
        ("pipeline_restore", handle_pipeline_restore),
        # ── 工作流模板管理 ──
        ("list_templates", handle_list_templates),
        ("save_template", handle_save_template),
        ("delete_template", handle_delete_template),
        ("get_available_steps", handle_get_available_steps),
        ("clone_template", handle_clone_template),
    ]
    for cmd, fn in cmds:
        register_handler(cmd, fn)
    send_log("pipeline_runner module loaded", "info")
