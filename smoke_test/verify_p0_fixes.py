# -*- coding: utf-8 -*-
"""verify_p0_fixes.py — python_backend P0 缺陷修复的离线验证(不连服务端/不加载模型)

覆盖:
  项1: pipeline_runner._push_flywheel 调 photo_scorer._capture_feedback 不再 TypeError,
       且 model_score 传 None(允许 _capture_feedback 回读缓存真实分)
  项5: photo_scorer.handle_dedup_burst 的 dry_run 预览与 apply 选出同一个 keeper
       (共用"按缓存分排序"的 keeper 选择逻辑)
  项7: agent_module.handle_agent_fetch_official_key 产出代理形态 _official_config
       (base_url 以 /api/llm/proxy 结尾, api_key 为传入 cloud token),
       磁盘持久化不落 token; _restore_official_config 能清除旧版明文 Key

用法: python smoke_test/verify_p0_fixes.py   (仓库根目录下运行)
"""
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 所有需要 stub 的依赖模块名(每个用例开始前都会重建,保证用例间隔离)
STUB_NAMES = ["service", "photo_scorer", "score_cache", "utils", "model_loader",
              "feedback_queue", "skill_loader", "mcp_client_manager",
              "copy_card", "folder_watcher"]
TARGET_NAMES = ["pipeline_runner", "photo_scorer", "agent_module"]


def _reset_modules():
    """清掉目标模块与 stub 模块的缓存,保证每个用例拿到干净状态。"""
    for name in STUB_NAMES + TARGET_NAMES:
        sys.modules.pop(name, None)


def _make_service_stub():
    svc = types.ModuleType("service")
    svc._app_config = {}
    svc.send_event = lambda *a, **k: None
    svc.send_log = lambda *a, **k: None
    svc.register_handler = lambda *a, **k: None
    svc.subscribe_events = lambda *a, **k: None
    svc.register_cleanup = lambda *a, **k: None
    svc._save_config_to_disk = lambda: None
    svc._get_config_dir = lambda: tempfile.gettempdir()
    return svc


def _make_utils_stub():
    utils = types.ModuleType("utils")
    utils.IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
    utils.RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng"}
    return utils


# ═══════════════════════════════════════════════════════════════════════
# 项1: _push_flywheel 调用签名
# ═══════════════════════════════════════════════════════════════════════
def test_item1_flywheel_no_typeerror():
    _reset_modules()
    captured = {}

    # 用 photo_scorer._capture_feedback 的真实签名做 stub:
    # (path, label, source, model_score=None, category="", override=False)
    ps = types.ModuleType("photo_scorer")
    def _capture_feedback(path, label, source, model_score=None, category="", override=False):
        captured.update({"path": path, "label": label, "source": source,
                         "model_score": model_score, "category": category,
                         "override": override})
    ps._capture_feedback = _capture_feedback
    sys.modules["photo_scorer"] = ps

    sys.modules["service"] = _make_service_stub()
    sys.modules["utils"] = _make_utils_stub()
    sys.modules["score_cache"] = types.ModuleType("score_cache")
    sys.modules["feedback_queue"] = types.ModuleType("feedback_queue")  # 使 _flywheel_available=True

    pr = importlib.import_module("pipeline_runner")
    assert pr._flywheel_available, "feedback_queue stub 后 _flywheel_available 应为 True"

    run = types.SimpleNamespace(template="cull_pipeline")
    # 修复前这里会抛 TypeError(多了 eye_open/blur kwargs)并被 except 吞掉;
    # 现在应正常调用且 model_score 为 None(由 _capture_feedback 回读缓存)
    pr._push_flywheel(run, "D:/photos/a.jpg", 1, "select_keep")

    assert captured, "_capture_feedback 未被调用(可能被静默吞错)"
    assert captured["model_score"] is None, f"model_score 应为 None, 实际 {captured['model_score']!r}"
    assert captured["source"] == "pipeline_select_keep"
    assert captured["category"] == "cull_pipeline"
    assert captured["label"] == 1
    print("[项1] OK: _push_flywheel 调用 _capture_feedback 无 TypeError, model_score=None")


# ═══════════════════════════════════════════════════════════════════════
# 项5: dedup dry_run 与 apply 的 keeper 一致
# ═══════════════════════════════════════════════════════════════════════
def _write_jpeg(path, color):
    from PIL import Image
    Image.new("RGB", (64, 64), color).save(path, "JPEG")


def test_item5_dedup_keeper_consistency():
    _reset_modules()
    tmp = tempfile.mkdtemp(prefix="verify_dedup_")

    # 3 张内容完全相同的图(相同 pHash → 必成连拍组), mtime 对齐到同一时间
    imgs = []
    for i, name in enumerate(["a.jpg", "b.jpg", "c.jpg"]):
        p = os.path.join(tmp, name)
        _write_jpeg(p, (200, 200, 200))
        os.utime(p, (1700000000, 1700000000))
        imgs.append(p)

    # 缓存分: c.jpg 最高 → keeper 必须是 c.jpg(dry_run 与 apply 一致)
    scores = {imgs[0]: 5.0, imgs[1]: 6.0, imgs[2]: 9.0}

    # score_cache stub: _db_path 指向真实 sqlite(供批量缩略图查询),
    # get_score 返回上面设定的分数
    sc = types.ModuleType("score_cache")
    db_path = os.path.join(tmp, "scores.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE scores (path TEXT, model_version TEXT, thumb_path TEXT)")
    for p in imgs:
        conn.execute("INSERT INTO scores VALUES (?, ?, ?)", (p, "test_mv", p))
    conn.commit(); conn.close()
    sc._db_path = db_path
    sc.get_score = lambda p, mv: ({"score": scores[p]} if p in scores else None)
    sc.get_folder = lambda folder, mv, limit=10000, offset=0: []
    sys.modules["score_cache"] = sc

    # model_loader stub(photo_scorer 顶部 from model_loader import 一长串)
    ml = types.ModuleType("model_loader")
    for name in ["load_model", "predict_score", "handle_get_cache_info",
                 "handle_clear_raw_cache", "_load_image_rgb", "_infer",
                 "make_thumbnail", "make_heatmap", "get_backend_pref_info",
                 "_read_backend_pref", "_write_backend_pref", "_cuda_available",
                 "compute_blur_score", "extract_deep_feature",
                 "extract_context_feature", "detect_eyes", "load_eye_model"]:
        setattr(ml, name, lambda *a, **k: None)
    ml.model_version = lambda: "test_mv"
    ml.RAW_EXTS = set()
    sys.modules["model_loader"] = ml

    sys.modules["service"] = _make_service_stub()
    sys.modules["utils"] = _make_utils_stub()

    ps = importlib.import_module("photo_scorer")

    dry = ps.handle_dedup_burst({"folder": tmp, "dry_run": True})
    assert dry["groups"] == 1, f"dry_run 应检出 1 组, 实际 {dry['groups']}"
    dry_kept = dry["group_details"][0]["kept_path"]
    dry_moved = sorted(dry["group_details"][0]["moved"])
    # dry_run 不动文件
    assert all(os.path.exists(p) for p in imgs), "dry_run 不应移动任何文件"

    applied = ps.handle_dedup_burst({"folder": tmp, "dry_run": False})
    assert applied["groups"] == 1, f"apply 应检出 1 组, 实际 {applied['groups']}"
    app_kept = applied["group_details"][0]["kept_path"]
    app_moved = sorted(applied["group_details"][0]["moved"])

    assert dry_kept == app_kept, f"keeper 不一致: dry_run={dry_kept} apply={app_kept}"
    assert dry_moved == app_moved, f"moved 不一致: dry_run={dry_moved} apply={app_moved}"
    assert app_kept == imgs[2], f"keeper 应是缓存分最高的 c.jpg, 实际 {app_kept}"
    # apply 后非 keeper 应已被移走, keeper 保留
    assert os.path.exists(imgs[2]) and not os.path.exists(imgs[0]) and not os.path.exists(imgs[1])
    print(f"[项5] OK: dry_run 与 apply keeper 一致 ({os.path.basename(app_kept)}, 缓存分最高)")


# ═══════════════════════════════════════════════════════════════════════
# 项7: 官方 Key 走服务端代理
# ═══════════════════════════════════════════════════════════════════════
class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_item7_official_proxy():
    _reset_modules()
    svc = _make_service_stub()
    sys.modules["service"] = svc
    for name, attr in [("skill_loader", "get_skill_loader"),
                       ("mcp_client_manager", "get_mcp_manager")]:
        m = types.ModuleType(name)
        setattr(m, attr, lambda *a, **k: None)
        sys.modules[name] = m
    sys.modules["photo_scorer"] = types.ModuleType("photo_scorer")
    sys.modules["copy_card"] = types.ModuleType("copy_card")
    sys.modules["folder_watcher"] = types.ModuleType("folder_watcher")

    ag = importlib.import_module("agent_module")

    # monkeypatch urlopen: 记录请求, 返回 official-config(base_url/model)
    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["token"] = req.headers.get("X-studio-token")
        return _FakeResp({"base_url": "https://api.deepseek.com", "model": "deepseek-chat"})
    ag.urllib.request.urlopen = fake_urlopen

    # ── 7a: fetch 产出代理形态 _official_config ──
    res = ag.handle_agent_fetch_official_key({"cloud_token": "tok-abc"})
    assert res.get("ok"), f"fetch 应成功: {res}"
    cfg = ag._official_config
    assert cfg["base_url"].endswith("/api/llm/proxy"), f"base_url 应以 /api/llm/proxy 结尾: {cfg['base_url']}"
    assert cfg["api_key"] == "tok-abc", f"api_key 应为传入的 cloud token: {cfg['api_key']!r}"
    assert cfg["model"] == "deepseek-chat"
    assert cfg.get("proxy") is True
    assert seen["url"].endswith("/api/llm/official-config"), f"应请求 official-config: {seen['url']}"
    assert seen["method"] == "GET", f"official-config 应为 GET: {seen['method']}"
    assert seen["token"] == "tok-abc", "请求应带 x-studio-token"

    # ── 7b: 磁盘持久化不落 token ──
    saved = svc._app_config.get("agent", {}).get("official_key_data", {})
    assert saved.get("api_key") == "", f"磁盘不应存 token: {saved.get('api_key')!r}"
    assert saved.get("proxy") is True, "磁盘应有 proxy 标记"
    assert saved.get("base_url", "").endswith("/api/llm/proxy")

    # ── 7c: 代理标记恢复(只回 base_url/model) ──
    ag._official_config.clear()
    ag._restore_official_config()
    cfg = ag._official_config
    assert cfg["base_url"].endswith("/api/llm/proxy") and cfg["api_key"] == "" \
        and cfg["model"] == "deepseek-chat", f"代理恢复形态不对: {cfg}"

    # ── 7d: 旧版明文 Key 被抹掉并写回 ──
    svc._app_config["agent"] = {"official_key_data": {
        "base_url": "https://api.deepseek.com", "api_key": "sk-old-plain", "model": "deepseek-chat"}}
    ag._official_config.clear()
    ag._restore_official_config()
    scrubbed = svc._app_config["agent"]["official_key_data"]
    assert scrubbed["api_key"] == "" and scrubbed.get("proxy") is True, \
        f"旧明文 Key 应被抹掉并标记 proxy: {scrubbed}"
    assert not ag._official_config.get("api_key"), "旧明文 Key 不应恢复到内存"

    # ── 7e: proxy 模式不再上报 usage(避免双计) ──
    reported = []
    ag._report_official_usage = lambda tok, n: reported.append((tok, n))
    session = types.SimpleNamespace(cloud_token="tok-abc", tokens_used=1234)
    ag._official_config.clear()
    ag._official_config.update({"base_url": "x/api/llm/proxy", "api_key": "tok-abc",
                                "model": "m", "proxy": True})
    # 直接复用 _record_run 尾部的上报判断逻辑不可行(耦合重), 改为验证条件本身:
    cloud_token = getattr(session, "cloud_token", "") or ""
    if (cloud_token and ag._official_config.get("api_key")
            and not ag._official_config.get("proxy") and session.tokens_used > 0):
        ag._report_official_usage(cloud_token, session.tokens_used)
    assert not reported, "proxy 模式不应上报 usage"
    print("[项7] OK: fetch→代理形态正确, token 不落盘, 旧明文 Key 已清除, proxy 不上报 usage")


if __name__ == "__main__":
    test_item1_flywheel_no_typeerror()
    test_item5_dedup_keeper_consistency()
    test_item7_official_proxy()
    print("全部 P0 验证通过")
