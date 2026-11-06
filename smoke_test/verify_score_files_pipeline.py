import os, sys, tempfile, threading, time, io
_tmp = tempfile.mkdtemp(prefix="lp_vfy_")
os.environ["APS_USER_DATA"] = os.path.join(_tmp, "userdata")
os.makedirs(os.environ["APS_USER_DATA"], exist_ok=True)
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import model_loader as ml, photo_scorer as ps
import numpy as np
from PIL import Image

events = []
ps.send_event = lambda name, data: events.append((name, data))
ps.send_log = lambda *a, **k: None

ml._load_onnx(str(_REPO_ROOT / "electron_app" / "model" / "best_model.onnx"))
ps._model_loaded = True

d = os.path.join(_tmp, "imgs"); os.makedirs(d)
rng = np.random.default_rng(1)
files = []
for i in range(20):
    p = os.path.join(d, f"f{i:02d}.jpg")
    Image.fromarray(rng.integers(0, 255, (512, 512, 3), dtype=np.uint8)).save(p)
    files.append(p)

# ── 1. handle_score_files: 事件顺序 + payload 形状 + 输出目录拷贝 ──
out_dir = os.path.join(_tmp, "out")
r = ps.handle_score_files({"files": files, "threshold": 0.0, "output_folder": out_dir,
                           "batch_id": "b1", "batch_size": 8})
prog = [e for e in events if e[0] == "watch_score_progress"]
done = [e for e in events if e[0] == "watch_score_done"]
assert r["total"] == 20 and r["failed"] == 0, r
assert len(prog) == 20 and len(done) == 1
assert [e[1]["current"] for e in prog] == list(range(1, 21)), "事件顺序乱序!"
assert all(e[1]["batch_id"] == "b1" for e in prog)
img = prog[0][1]["image"]
assert set(img) == {"path", "filename", "score", "size", "is_raw", "width",
                    "height", "thumb_path", "manual_keep", "cached"}, set(img)
assert len(os.listdir(out_dir)) == 20, "threshold=0 应全部拷贝"
print(f"1. score_files 顺序/payload/拷贝 OK (selected={r['selected']})")

# ── 2. 缓存命中: 二次跑全 cached=True，不再推理 ──
events.clear()
r2 = ps.handle_score_files({"files": files, "threshold": 6.0, "output_folder": out_dir})
prog2 = [e for e in events if e[0] == "watch_score_progress"]
assert all(e[1]["image"]["cached"] for e in prog2), "二次应全部缓存命中"
print("2. 缓存命中跳过推理 OK")

# ── 3. 取消语义: 评 3 张后取消，事件应立即停止 ──
events.clear()
files3 = []
d3 = os.path.join(_tmp, "imgs3"); os.makedirs(d3)
for i in range(20):
    p = os.path.join(d3, f"c{i:02d}.jpg")
    Image.fromarray(rng.integers(0, 255, (512, 512, 3), dtype=np.uint8)).save(p)
    files3.append(p)

def _cancel_after_3(name, data):
    events.append((name, data))
    if name == "watch_score_progress" and data["current"] >= 3:
        ps.handle_cancel_scoring({})
ps.send_event = _cancel_after_3
r3 = ps.handle_score_files({"files": files3, "threshold": 6.0, "output_folder": ""})
prog3 = [e for e in events if e[0] == "watch_score_progress"]
assert len(prog3) <= 4, f"取消后仍发出 {len(prog3)} 个进度事件"
print(f"3. 取消语义 OK (发出 {len(prog3)}/20 个进度事件后停止)")

# ── 4. 显式 batch_size=1 与 batch_size=8 分数一致 ──
ps.send_event = lambda name, data: events.append((name, data))
events.clear()
d4 = os.path.join(_tmp, "imgs4"); os.makedirs(d4)
f4 = []
for i in range(9):
    p = os.path.join(d4, f"x{i}.jpg")
    Image.fromarray(rng.integers(0, 255, (256, 256, 3), dtype=np.uint8)).save(p)
    f4.append(p)
r4a = ps.handle_score_files({"files": f4, "threshold": 6.0, "output_folder": "", "batch_size": 1})
s1 = {os.path.basename(p): __import__("score_cache").get_score(p, ml.model_version())["score"] for p in f4}
import score_cache
# 清掉再按 bs=8 重评（改 mtime 使缓存失效太重，直接换目录拷贝）
import shutil as _sh
d5 = os.path.join(_tmp, "imgs5"); os.makedirs(d5)
f5 = [_sh.copy2(p, os.path.join(d5, os.path.basename(p))) for p in f4]
r4b = ps.handle_score_files({"files": f5, "threshold": 6.0, "output_folder": "", "batch_size": 8})
s8 = {os.path.basename(p): score_cache.get_score(p, ml.model_version())["score"] for p in f5}
diff = max(abs(s1[k] - s8[k]) for k in s1)
assert diff < 1e-4, f"bs=1 vs bs=8 分数不一致: {diff}"
print(f"4. bs=1 vs bs=8 分数一致 OK (max diff {diff:.2e})")

print("\n全部通过")
