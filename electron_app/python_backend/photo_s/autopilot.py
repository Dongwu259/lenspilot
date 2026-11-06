"""photo_s/autopilot.py — 无人值守修图管线（v2.5 watch 联动）

监视目录 → 新图稳定后自动走「suggest / auto-tone → process → audit」→
按闸门结果路由到 ``passed/`` / ``review/``，每图一行 JSONL 轨迹。零件全部
复用既有闭环砖块（suggest / auto_tone 槽位 / audit / watcher 防抖），本模块
只做编排与路由——「agent 的常驻修图后台」：

- mode=suggest：零模型规则层（暗图/偏色自动修），缺什么都不装；
- mode=auto_tone：个人风格 AI 层，缺插件启动即报错（fail-loud 同槽位约定）；
- mode=both：先修偏再上风格（AGENT_API §7.1 的叠加语义）；
- audit 阈值与 ``--aesthetic`` 美学闸门直通 audit_image（缺 verifier 同样
  启动即报错，不静默放行）；
- write_xmp：每图在原图旁写 LR sidecar——auto-tone 的真实预测参数进 XMP，
  用户在 LR 里修正后 lr-scan 回采即是下一版模型的残差训练信号。

CLI ``photo-s autopilot``；MCP ``autopilot_start/status/stop``；REST
``/v1/autopilot``。
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .audit import audit_image
from .engine import ALL_INPUT_EXTENSIONS, ProcessOptions, process_image

__all__ = ["AutopilotConfig", "process_one", "run_autopilot",
           "validate_config", "MODES"]

MODES = ("suggest", "auto_tone", "both")
DEFAULT_OUT_NAME = "photo-s-out"


@dataclasses.dataclass
class AutopilotConfig:
    watch_dir: str
    out_dir: Optional[str] = None          # 默认 <watch>/photo-s-out/
    mode: str = "suggest"                  # suggest | auto_tone | both
    auto_tone_strength: float = 1.0
    scale: float = 1.0                     # suggest 幅度
    thresholds: Dict[str, float] = dataclasses.field(default_factory=dict)
    aesthetic: Optional[float] = None      # 1-10 美学闸门（需 verify 插件）
    write_xmp: bool = False
    recursive: bool = False
    scan_existing: bool = False            # 启动时处理目录中已有图片
    log_path: Optional[str] = None         # 默认 <out>/autopilot.jsonl
    quality: Optional[int] = None
    output_format: Optional[str] = None
    resize: Optional[str] = None           # "WxH"

    @property
    def out_root(self) -> str:
        return self.out_dir or os.path.join(self.watch_dir, DEFAULT_OUT_NAME)

    def base_options(self) -> ProcessOptions:
        opts = ProcessOptions()
        if self.quality is not None:
            opts.quality = self.quality
        if self.output_format:
            opts.output_format = self.output_format
        if self.resize:
            m = re.match(r"^(\d+)[xX](\d+)$", self.resize.strip())
            if not m:
                raise RuntimeError(
                    f"resize must be WxH, got {self.resize!r}")
            opts.max_width = int(m.group(1))
            opts.max_height = int(m.group(2))
        return opts


def validate_config(cfg: AutopilotConfig) -> Optional[Any]:
    """启动即失败（fail-loud）：目录存在、mode 合法、模式/闸门所需插件在场。

    返回 aesthetic 闸门要用的 verifier（未启用美学闸门时为 None）。
    """
    if not os.path.isdir(cfg.watch_dir):
        raise RuntimeError(f"watch dir not found: {cfg.watch_dir}")
    if cfg.mode not in MODES:
        raise RuntimeError(f"mode must be one of {MODES}, got {cfg.mode!r}")
    cfg.auto_tone_strength = max(0.0, min(1.0, float(cfg.auto_tone_strength)))
    cfg.scale = max(0.0, min(1.0, float(cfg.scale)))
    if cfg.mode in ("auto_tone", "both"):
        from .plugin import find_provider
        provider = find_provider("auto_tone")
        if provider is None or not hasattr(provider, "auto_tone_params"):
            raise RuntimeError(
                "autopilot mode=auto_tone needs the auto-tone plugin "
                "(pip install photo-s-plugin-auto-tone); zero-model "
                "alternative: mode=suggest")
    verifier = None
    if cfg.aesthetic is not None:
        from .plugin import find_provider
        verifier = find_provider("verify")
        if verifier is None:
            raise RuntimeError(
                "autopilot --aesthetic needs the auto-tone plugin's "
                "verifier (pip install photo-s-plugin-auto-tone); drop "
                "--aesthetic to use technical gates only")
    try:
        os.makedirs(os.path.join(cfg.out_root, ".staging"), exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create output dirs: {e}") from e
    return verifier


def _iter_images(watch_dir: str, recursive: bool,
                 skip_under: str) -> List[str]:
    """目录内图片清单（跳过输出根目录，防止自触发）。"""
    out = []
    skip = os.path.abspath(skip_under)
    if recursive:
        for root, dirs, files in os.walk(watch_dir):
            if os.path.abspath(root) == skip or \
                    os.path.abspath(root).startswith(skip + os.sep):
                dirs[:] = []
                continue
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in ALL_INPUT_EXTENSIONS:
                    out.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(watch_dir)):
            p = os.path.join(watch_dir, f)
            if os.path.isfile(p) and \
                    os.path.splitext(f)[1].lower() in ALL_INPUT_EXTENSIONS:
                out.append(p)
    return out


def _route(src: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(src))
    dest = os.path.join(dest_dir, base + ext)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base}_{i}{ext}")
        i += 1
    os.replace(src, dest)
    return dest


def _append_log(cfg: AutopilotConfig, rec: Dict[str, Any]) -> None:
    path = cfg.log_path or os.path.join(cfg.out_root, "autopilot.jsonl")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def process_one(path: str, cfg: AutopilotConfig, *,
                verifier: Optional[Any] = None) -> Dict[str, Any]:
    """单图完整闭环：参数 → 处理 → 闸门 → 路由 → JSONL。永不抛错（记录 error）。"""
    rec: Dict[str, Any] = {"ts": time.time(), "input": path, "output": None,
                           "params": None, "audit": None, "routed": None,
                           "error": None}
    try:
        opts = cfg.base_options()
        if cfg.mode in ("suggest", "both"):
            from .suggest import suggest_file
            sug = suggest_file(path, scale=cfg.scale)
            if not sug.get("ok"):
                raise RuntimeError(f"suggest failed: {sug.get('error')}")
            rec["suggest"] = sug.get("suggested") or {}
            for k, v in rec["suggest"].items():
                if hasattr(opts, k):
                    setattr(opts, k, v)
        if cfg.mode in ("auto_tone", "both"):
            from .autotone import resolve_auto_tone_options
            opts.auto_tone = cfg.auto_tone_strength
            opts, params = resolve_auto_tone_options(opts, path)
            rec["auto_tone_params"] = params.get("options") or {}
            rec["confidence"] = params.get("confidence")

        opts.output_dir = os.path.join(cfg.out_root, ".staging")
        opts.suffix = ""
        opts.overwrite = True
        result = process_image(path, opts)
        if not result.success:
            raise RuntimeError(result.error or "processing failed")
        rec["output"] = result.output_path
        rec["params"] = {k: v for k, v in dataclasses.asdict(opts).items()
                         if v not in (None, "", 0.0, 1.0, False)}

        audit = audit_image(result.output_path, aesthetic=cfg.aesthetic,
                            verifier=verifier, **cfg.thresholds)
        rec["audit"] = {"passed": bool(audit.get("passed")),
                        "reason": audit.get("reason",
                                            audit.get("error", ""))}
        dest = _route(result.output_path,
                      os.path.join(cfg.out_root,
                                   "passed" if rec["audit"]["passed"]
                                   else "review"))
        rec["routed"] = dest

        if cfg.write_xmp:
            from .lrxmp import write_xmp_sidecar
            sidecar, _warns = write_xmp_sidecar(path, opts)
            rec["xmp"] = sidecar
    except Exception as e:  # noqa: BLE001 — 单图失败是记录项，不是管线失败
        rec["error"] = f"{type(e).__name__}: {e}"
    _append_log(cfg, rec)
    return rec


def run_autopilot(cfg: AutopilotConfig, *,
                  on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                  stop_event: Optional[threading.Event] = None) -> None:
    """阻塞主循环：scan_existing 预灌 + watchdog 供料 + 单工作线程逐图闭环。

    需 watchdog（同 watch 命令）；``stop_event`` 置位或 Ctrl+C 退出。
    """
    import importlib.util
    if importlib.util.find_spec("watchdog") is None:
        raise RuntimeError("autopilot needs watchdog (pip install watchdog)")

    verifier = validate_config(cfg)
    from .watcher import start_watching

    q: "queue.Queue[str]" = queue.Queue()
    skip_root = os.path.abspath(cfg.out_root)

    def enqueue(path: str) -> None:
        ap = os.path.abspath(path)
        if ap == skip_root or ap.startswith(skip_root + os.sep):
            return  # 自己的路由产物不回炉
        q.put(path)

    if cfg.scan_existing:
        for p in _iter_images(cfg.watch_dir, cfg.recursive, cfg.out_root):
            q.put(p)

    def worker() -> None:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                path = q.get(timeout=0.5)
            except queue.Empty:
                continue
            rec = process_one(path, cfg, verifier=verifier)
            if on_event:
                try:
                    on_event(rec)
                except Exception:  # noqa: BLE001 — 回调挂了不能拖死工作线程
                    pass

    threading.Thread(target=worker, daemon=True,
                     name="autopilot-worker").start()
    start_watching(cfg.watch_dir, ProcessOptions(suffix=""),
                   recursive=cfg.recursive, stop_event=stop_event,
                   on_file=enqueue)
