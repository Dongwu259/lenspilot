# -*- coding: utf-8 -*-
"""P1-2 冒烟：预览分级缓存（解码层/几何层命中 + 像素等价性 + 失效）。

运行（仓库根）: python smoke_test/test_p1_preview_cache.py
"""
import base64
import io
import os
import sys
import tempfile

import numpy as np
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="lenspilot_p1_pv_")
os.environ["APS_USER_DATA"] = os.path.join(_TMP, "ud")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "electron_app", "python_backend")
sys.path.insert(0, BACKEND)

import photo_editor  # noqa: E402

W, H = 3200, 2100  # >1280 → 预览必然缩放（走 scale<1 分支）


def _mk_photo():
    arr = np.zeros((H, W, 3), dtype=np.uint8)
    arr[..., 0] = np.linspace(0, 255, W, dtype=np.uint8)[None, :]
    arr[..., 1] = np.linspace(0, 255, H, dtype=np.uint8)[:, None]
    arr[..., 2] = 128
    p = os.path.join(_TMP, "photo.jpg")
    Image.fromarray(arr).save(p, "JPEG", quality=92)
    return p


def _call(path, edits):
    return photo_editor.handle_apply_edits(
        {"path": path, "edits": edits, "preview": True})


def _stats():
    return dict(photo_editor._pv_stats)


def main():
    path = _mk_photo()
    photo_editor._pv_stats.update(decode_hit=0, decode_miss=0, geom_hit=0, geom_miss=0)

    # 1) 首次：解码 miss + 几何 miss
    r1 = _call(path, {"exposure": 0.1})
    assert r1.get("preview", "").startswith("data:image/jpeg;base64,")
    assert _stats() == {"decode_hit": 0, "decode_miss": 1, "geom_hit": 0, "geom_miss": 1}

    # 2) 换色调参数：解码/几何全命中，只重算像素段；输出随参数变化
    r2 = _call(path, {"exposure": 0.5})
    assert r2["preview"] != r1["preview"]
    assert _stats()["decode_hit"] == 1 and _stats()["geom_hit"] == 1

    # 3) 改几何（旋转）：几何层 miss、解码层仍命中
    _call(path, {"rotation": 90})
    assert _stats()["decode_hit"] == 2 and _stats()["geom_miss"] == 2

    # 4) 旋转保持、再调色调：几何命中
    _call(path, {"rotation": 90, "contrast": 1.3})
    assert _stats()["decode_hit"] == 3 and _stats()["geom_hit"] == 2

    # 5) 像素等价性：分级缓存输出 ≡ 一次性 apply_edits 输出（同参同图逐字节一致）
    scale = min(1.0, 1280 / max(W, H))
    img = Image.open(path).convert("RGB")
    if scale < 1.0:
        img = img.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
    expect = photo_editor.apply_edits(img, {"exposure": 0.1})
    expect.thumbnail((800, 800), Image.LANCZOS)
    buf = io.BytesIO()
    expect.save(buf, format="JPEG", quality=75)
    b64_expect = base64.b64encode(buf.getvalue()).decode("ascii")
    assert r1["preview"].split(",")[1] == b64_expect, "分级缓存预览与一次性渲染不一致"
    print("[ok] parity: 分级缓存预览与一次性 apply_edits 逐字节一致")

    # 6) 文件指纹变化（mtime）→ 解码层失效重建
    st = os.stat(path)
    os.utime(path, (st.st_mtime + 120, st.st_mtime + 120))
    _call(path, {"exposure": 0.1})
    assert _stats()["decode_miss"] == 2

    # 7) LRU 上限：塞入 5 张后最旧的被逐出（缓存dict ≤ 4）
    for i in range(5):
        p_i = os.path.join(_TMP, f"p{i}.jpg")
        Image.fromarray(np.zeros((800, 600, 3), dtype=np.uint8)).save(p_i, "JPEG")
        _call(p_i, {})
    assert len(photo_editor._PV_CACHE) <= photo_editor._PV_CACHE_MAX
    assert path not in photo_editor._PV_CACHE  # 最初那张已被逐出
    print("[ok] cache: 命中统计/像素等价/指纹失效/LRU 上限全部通过")
    print("P1 PREVIEW CACHE SMOKE PASSED")


if __name__ == "__main__":
    main()
