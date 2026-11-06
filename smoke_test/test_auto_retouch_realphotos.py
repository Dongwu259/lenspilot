#!/usr/bin/env python3
"""
test_auto_retouch_realphotos.py — 真实照片实测:交付管线 auto_retouch_keepers
跑真实拍摄目录(默认扫街 ARW 全集),输出到原文件夹内新建文件夹
<源目录>/自动修图实测_<时间戳>/_自动修图/。

用法(仓库根或 electron_app 下):
  python smoke_test/test_auto_retouch_realphotos.py "D:/photoDatebase/3-19-扫街" [选项]
选项:
  --limit N      只跑前 N 张(0=全部,默认 0)
  --engine X     auto | suggest(默认 auto)
  --strength F   调色强度 0-1(默认 1.0)
  --quality N    JPEG 质量(默认 92)

产物: <输出夹>/_自动修图/*.jpg + _实测报告.json(计时/引擎/失败清单)
退出码: 0=成功 1=失败
"""
import argparse
import glob
import json
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# service 桩(与 test_auto_retouch.py 同 pattern:仅需 send_event/send_log/_app_config)
svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda *a, **k: None
svc.register_handler = lambda n, h: None
svc._app_config = {}
svc.subscribe_events = lambda fn: (lambda: None)
sys.modules["service"] = svc

import pipeline_tool_steps as pts  # noqa: E402


class _FakeRun:
    """镜像 pipeline Run 的最小接口(同 test_auto_retouch.py _FakeRun)。"""

    def __init__(self, files, work_folder, step_params=None):
        self.cancel_event = threading.Event()
        self.artifacts = {"keep_paths": list(files)}
        self.params = {}
        self.step_params = step_params or {}
        self.work_folder = work_folder
        self.run_id = "realphotos_test"
        self.template = "CULL_PIPELINE"
        self.status = "running"
        self.run_log = []

    def set(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--engine", default="auto", choices=["auto", "suggest"])
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--out", default="", help="复用既有输出夹(配合 --resume 续跑)")
    ap.add_argument("--resume", action="store_true", help="跳过输出已存在的照片")
    args = ap.parse_args()

    src = os.path.abspath(args.folder)
    if not os.path.isdir(src):
        print(f"ERROR: 目录不存在: {src}")
        return 1
    exts = (".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".jpg", ".jpeg", ".png")
    files = sorted(p for p in glob.glob(os.path.join(src, "*"))
                   if p.lower().endswith(exts) and os.path.isfile(p))
    if not files:
        print(f"ERROR: 无可处理照片: {src}")
        return 1
    total_pool = len(files)
    if args.limit > 0:
        files = files[: args.limit]
    n = len(files)

    if args.out:
        out_root = os.path.abspath(args.out)
    else:
        out_root = os.path.join(src, f"自动修图实测_{time.strftime('%Y%m%d_%H%M')}")
    os.makedirs(out_root, exist_ok=True)

    # 引擎就绪度(只读探测,不下载)
    import photo_s_plugins
    st = photo_s_plugins.ai_engine_status()
    print(f"源目录: {src}  (共 {total_pool} 张,本次处理 {n} 张)")
    print(f"引擎状态: {st}")
    print(f"输出夹: {out_root}")
    print(f"参数: engine={args.engine} strength={args.strength} quality={args.quality}")

    run = _FakeRun(files, out_root, step_params={
        "engine": args.engine, "strength": args.strength,
        "quality": args.quality, "format": "jpeg",
        **({"resume": True} if args.resume else {})})

    # 后台看门狗:每 30s 打印进度(输出张数/最新日志/RSS+系统commit),供后台任务跟踪
    stop = threading.Event()

    def _mem_mb():
        try:
            import ctypes
            import ctypes.wintypes as wt

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            k32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wt.HANDLE
            h = k32.GetCurrentProcess()
            psapi.K32GetProcessMemoryInfo.argtypes = [
                wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
            if not psapi.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return -1, -1
            return pmc.WorkingSetSize // (1024 * 1024), pmc.PagefileUsage // (1024 * 1024)
        except Exception:
            return -1, -1

    def watchdog():
        out_dir = os.path.join(out_root, "_自动修图")
        seen = 0
        while not stop.wait(30):
            produced = len(glob.glob(os.path.join(out_dir, "*.jpg"))) if os.path.isdir(out_dir) else 0
            logs = run.run_log[seen:]
            seen = len(run.run_log)
            tail = logs[-1]["result"] if logs else ""
            rss, pf = _mem_mb()
            print(f"[进度] {time.strftime('%H:%M:%S')} 已产出 {produced}/{n} "
                  f"RSS={rss}MB Commit={pf}MB | {tail}", flush=True)

    threading.Thread(target=watchdog, daemon=True).start()

    t0 = time.time()
    try:
        r = pts.auto_retouch_keepers(run)
    finally:
        stop.set()
    dt = time.time() - t0

    print(f"\n=== 步骤返回: {json.dumps(r, ensure_ascii=False)} ===")
    arts = run.artifacts
    summary = {
        "source": src, "requested": n, "pool": total_pool,
        "engine_requested": args.engine,
        "engine_used": arts.get("auto_retouch_engine"),
        "count": arts.get("auto_retouch_count"),
        "failed": arts.get("auto_retouch_failed"),
        "dir": arts.get("auto_retouch_dir"),
        "elapsed_s": round(dt, 1),
        "sec_per_img": round(dt / max(n, 1), 2),
        "step_result": r,
        "logs": [e.get("result") for e in run.run_log if isinstance(e, dict)],
    }
    report_path = os.path.join(out_root, "_实测报告.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    ok = r.get("ok") is True and (arts.get("auto_retouch_count") or 0) > 0
    print(f"引擎(实际): {summary['engine_used']}  产出: {summary['count']}"
          f"  失败: {summary['failed']}  总耗时: {dt:.1f}s"
          f"  ({summary['sec_per_img']}s/张)")
    print(f"报告: {report_path}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
