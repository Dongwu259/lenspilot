#!/usr/bin/env python3
"""
test_batch_export.py — 批量导出后端测试(0.8.9)

覆盖:JPEG/PNG 导出、长边缩放、sRGB ICC、文字水印、唯一命名、
sidecar 编辑生效(grayscale)、缺失文件错误计数。
运行: python smoke_test/test_batch_export.py
"""
import io
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "electron_app", "python_backend"))

# stub service(photo_editor 只需 send_event/send_log/register_handler)
_events = []
svc = types.ModuleType("service")
svc.send_log = lambda *a, **k: None
svc.send_event = lambda n, d: _events.append((n, d))
svc.register_handler = lambda n, h: None
sys.modules["service"] = svc

from PIL import Image  # noqa: E402
import photo_editor as pe  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  [OK] {name}")


def _mk_photo(path, size=(900, 600), color=(200, 120, 60)):
    img = Image.new("RGB", size, color)
    # 渐变,避免纯色压不出差异
    for y in range(size[1]):
        for x in range(0, size[0], 30):
            img.paste((color[0], min(255, color[1] + y // 8), color[2]),
                      (x, y, min(size[0], x + 30), y + 1))
    img.save(path, "JPEG", quality=92)


def main():
    tmp = tempfile.mkdtemp(prefix="pex_test_")
    src = os.path.join(tmp, "src")
    dest = os.path.join(tmp, "out")
    os.makedirs(src)
    p1 = os.path.join(src, "a.jpg")
    p2 = os.path.join(src, "b.jpg")
    _mk_photo(p1)
    _mk_photo(p2, size=(500, 800))

    # 1. 基础 JPEG 导出 ×2
    r = pe.handle_batch_export_edited({
        "paths": [p1, p2], "dest_dir": dest, "format": "JPEG",
        "quality": 90, "max_long_edge": 0, "color_space": "srgb", "watermark": None})
    assert r.get("ok") and r["exported"] == 2 and r["failed"] == 0, r
    outs = sorted(os.listdir(dest))
    assert outs == ["a_edit.jpg", "b_edit.jpg"], outs
    ok(f"基础 JPEG 导出 ×2 → {outs}")

    # 2. 唯一命名:再导一次 → _edit_1
    r = pe.handle_batch_export_edited({
        "paths": [p1], "dest_dir": dest, "format": "JPEG",
        "quality": 90, "max_long_edge": 0, "color_space": "srgb", "watermark": None})
    assert "a_edit_1.jpg" in os.listdir(dest), os.listdir(dest)
    ok("重名自动 _edit_1,不覆盖")

    # 3. 长边缩放(横图 900→400;竖图 800→400)
    dest2 = os.path.join(tmp, "out2")
    r = pe.handle_batch_export_edited({
        "paths": [p1, p2], "dest_dir": dest2, "format": "JPEG",
        "quality": 90, "max_long_edge": 400, "color_space": "srgb", "watermark": None})
    assert r["exported"] == 2, r
    for f in os.listdir(dest2):
        with Image.open(os.path.join(dest2, f)) as im:
            assert max(im.size) <= 400, (f, im.size)
    ok("长边 400 封顶(横竖图均生效)")

    # 4. sRGB ICC 标记
    with Image.open(os.path.join(dest2, "a_edit.jpg")) as im:
        icc = im.info.get("icc_profile")
        assert icc and len(icc) > 100, f"缺 sRGB ICC: {len(icc) if icc else 0}B"
    ok("sRGB ICC 已嵌入")

    # 5. PNG + 文字水印
    dest3 = os.path.join(tmp, "out3")
    r = pe.handle_batch_export_edited({
        "paths": [p1], "dest_dir": dest3, "format": "PNG",
        "quality": 90, "max_long_edge": 0, "color_space": "original",
        "watermark": {"text": "© 测试", "position": "BOTTOM_RIGHT", "opacity": 80}})
    assert r["exported"] == 1 and r["failed"] == 0, r
    assert os.listdir(dest3) == ["a_edit.png"]
    ok("PNG + 文字水印导出成功")

    # 5b. PNG + 图片水印（Logo）
    logo = os.path.join(tmp, "logo.png")
    Image.new("RGBA", (80, 40), (255, 0, 0, 255)).save(logo)
    dest3b = os.path.join(tmp, "out3b")
    r = pe.handle_batch_export_edited({
        "paths": [p1], "dest_dir": dest3b, "format": "PNG",
        "quality": 90, "max_long_edge": 0, "color_space": "original",
        "watermark": {"type": "image", "path": logo, "position": "BOTTOM_RIGHT",
                      "opacity": 100, "scale": 20}})
    assert r["exported"] == 1 and r["failed"] == 0, r
    with Image.open(os.path.join(dest3b, "a_edit.png")) as im:
        px = im.convert("RGB").getpixel((im.width - 30, im.height - 20))
        assert px[0] > 200 and px[1] < 80 and px[2] < 80, f"Logo 未叠加: {px}"
    ok("PNG + 图片水印（右下 Logo）导出成功")

    # 6. sidecar 编辑生效:grayscale → 导出图为灰
    sidecar = p2 + ".edit.json"
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump({"grayscale": True}, f)
    dest4 = os.path.join(tmp, "out4")
    r = pe.handle_batch_export_edited({
        "paths": [p2], "dest_dir": dest4, "format": "JPEG",
        "quality": 90, "max_long_edge": 0, "color_space": "srgb", "watermark": None})
    assert r["exported"] == 1, r
    with Image.open(os.path.join(dest4, "b_edit.jpg")) as im:
        px = im.convert("RGB").getpixel((im.width // 2, im.height // 2))
        assert abs(px[0] - px[1]) <= 2 and abs(px[1] - px[2]) <= 2, f"非灰图: {px}"
    ok("sidecar grayscale 编辑已应用")

    # 7. 缺失文件计入 failed
    r = pe.handle_batch_export_edited({
        "paths": [p1, os.path.join(src, "ghost.jpg")], "dest_dir": dest,
        "format": "JPEG", "quality": 90, "max_long_edge": 0,
        "color_space": "srgb", "watermark": None})
    assert r["exported"] == 1 and r["failed"] == 1 and r["errors"], r
    ok("缺失文件 → failed=1 + errors")

    # 8. 进度事件齐发
    prog = [d for n, d in _events if n == "batch_export_progress"]
    assert len(prog) >= 8 and prog[0]["current"] == 1 and prog[-1]["total"] == 2
    ok(f"进度事件 {len(prog)} 条")

    # 9. 参数校验
    assert "error" in pe.handle_batch_export_edited({"paths": [], "dest_dir": dest})
    assert "error" in pe.handle_batch_export_edited({"paths": [p1], "dest_dir": ""})
    assert "error" in pe.handle_batch_export_edited({
        "paths": [p1], "dest_dir": dest, "format": "GIF"})
    ok("空 paths/空目录/非法格式 全部拒绝")

    print(f"\n全部通过 ({PASS} 项)")


if __name__ == "__main__":
    main()
