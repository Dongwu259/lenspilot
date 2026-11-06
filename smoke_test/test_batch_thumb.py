# -*- coding: utf-8 -*-
"""test_batch_thumb.py — 第 2 项优化：批量缩略图降采样渲染

render_edit_thumbnail（解码层限长边 + crop 等比缩放 + 小图跑像素管线）与
全尺寸 apply_edits 后缩小的结果在视觉上等价（均值差 < 3/255），
且批量应用走该路径后缩略图照常生成。
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "electron_app" / "python_backend"))
from editor_render import render_edit_thumbnail
from photo_editor import apply_edits, load_edits
import photo_editor as pe

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


tmp = tempfile.mkdtemp(prefix="batch_thumb_")

# 大图 + 含 crop/调色的编辑组合
W, H = 2400, 1600
yy, xx = np.mgrid[0:H, 0:W]
arr = np.stack([60 + (xx * 150 // W), 50 + (yy * 120 // H),
                np.clip(xx // 8 + yy // 10, 0, 255)], axis=-1).astype(np.uint8)
arr[0:400, 0:600] = (200, 120, 90)
img = Image.fromarray(arr)

edits = {
    "crop": [600, 400, 1200, 800],   # 全尺寸像素空间的裁剪
    "exposure": 0.4, "contrast": 1.2, "warmth": 0.2,
    "vibrance": 0.15, "clarity": 0.2,
}

small, scale = render_edit_thumbnail(img, edits, max_side=1024)
check("capped max side", max(small.size) <= 1024, (small.size, scale))
check("scale reported", abs(scale - 1024 / 2400) < 1e-6, scale)
check("crop aspect kept", abs(small.size[0] / small.size[1] - 1200 / 800) < 0.02, small.size)

# 等价性：全尺寸渲染 → 等比缩小 vs 降采样渲染
full = apply_edits(img, edits)
full_small = full.resize((max(1, round(full.width * scale)), max(1, round(full.height * scale))),
                         Image.LANCZOS)
a = np.asarray(full_small.convert("RGB"), dtype=np.int16)
b = np.asarray(small.convert("RGB"), dtype=np.int16)
h2 = min(a.shape[0], b.shape[0])
w2 = min(a.shape[1], b.shape[1])
diff = float(np.abs(a[:h2, :w2] - b[:h2, :w2]).mean())
check("visual equivalence", diff < 3.0, f"mean|diff|={diff:.2f}/255")

# 小图路径（无需缩放）：与 apply_edits 完全一致
tiny = Image.new("RGB", (300, 200), (110, 120, 130))
t_edits = {"exposure": 0.2}
t_out, t_scale = render_edit_thumbnail(tiny, t_edits, max_side=1024)
check("small image passthrough", t_scale == 1.0 and
      np.array_equal(np.asarray(t_out), np.asarray(apply_edits(tiny, t_edits))))

# 批量应用端到端：sidecar 落盘 + 缩略图管线吃降采样渲染输出无碍
p1 = os.path.join(tmp, "a.jpg")
img.save(p1, quality=92)
r = pe.handle_batch_apply_edits({"paths": [p1], "edits": {"exposure": 0.3}})
check("batch ok", r.get("ok") and r.get("processed") == 1, r)
sidecar = load_edits(p1)
check("batch sidecar written", abs(sidecar.get("exposure", 0) - 0.3) < 1e-6)
# make_thumbnail 消费降采样渲染产物（260px 级缩略, 1024 输入绰绰有余）
from model_loader import make_thumbnail
tp = make_thumbnail(small, p1)
check("thumb from downscaled render", bool(tp) and os.path.exists(tp), tp)
if tp:
    with Image.open(tp) as th:
        check("thumb is small", max(th.size) <= 260, th.size)

print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
