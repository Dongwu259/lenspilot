#!/usr/bin/env python
"""诊断 HEIC(PIL/pillow_heif)在npm start环境下的行为——为什么PIL.open会挂死"""
import os, sys, time, signal, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'electron_app', 'python_backend'))

P = r"D:\photoDatebase\4-2春游\2026-04-02 075150.heic"

class TimeoutError(Exception): pass

def with_timeout(fn, seconds):
    """Run fn with a timeout on the same thread (signal-based for real hangs)."""
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError))
        signal.alarm(seconds)
        try: return fn()
        finally: signal.alarm(0)
    else:
        # Windows: use thread
        result = [None]; exc = [None]
        def _run():
            try: result[0] = fn()
            except Exception as e: exc[0] = e
        t = threading.Thread(target=_run); t.start(); t.join(seconds)
        if t.is_alive():
            print(f"    !! 函数 {seconds}s 未返回 — 可能挂死(hang) !!")
            return 'HUNG'
        if exc[0]: raise exc[0]
        return result[0]

print(f"测试文件: {os.path.basename(P)} ({os.path.getsize(P)/1e6:.1f}MB)\n")

# 1. pillow_heif 基本信息
print("=== 1. pillow_heif 版本 ===")
try:
    import pillow_heif as ph
    print(f"  version={ph.__version__}")
    print(f"  libheif_version={ph.libheif_version() if hasattr(ph,'libheif_version') else '?'}")
    # Register
    ph.register_heif_opener()
    print("  register_heif_opener: OK")
except Exception as e:
    print(f"  FAIL: {e}")

# 2. PIL.open with timeout (10s)
print("\n=== 2. PIL.Image.open (10s超时) ===")
from PIL import Image as PILImage
try:
    def _open():
        img = PILImage.open(P)
        return img.size, img.mode
    r = with_timeout(_open, 10)
    print(f"  result: {r}")
except TimeoutError:
    print("  !! PIL.open 超时>10s — 挂死了 !!")
except Exception as e:
    print(f"  FAIL: {e}")

# 3. PIL.open + convert with timeout
print("\n=== 3. PIL.open + .convert('RGB') (10s超时) ===")
try:
    def _open_conv():
        img = PILImage.open(P).convert('RGB')
        return img.size
    r = with_timeout(_open_conv, 10)
    print(f"  result: {r}")
except TimeoutError:
    print("  !! convert('RGB') 超时>10s — 挂死了 !!")
except Exception as e:
    print(f"  FAIL: {e}")

# 4. PIL.open + thumbnail 256px with timeout
print("\n=== 4. PIL.open + thumbnail(256) (10s超时) ===")
try:
    def _thumb():
        img = PILImage.open(P)
        img.thumbnail((256,256), PILImage.LANCZOS)
        return img.size
    r = with_timeout(_thumb, 10)
    print(f"  result: {r}")
except TimeoutError:
    print("  !! thumbnail 超时>10s — 挂死了 !!")
except Exception as e:
    print(f"  FAIL: {e}")

# 5. pillow_heif.read_heif direct API
print("\n=== 5. pillow_heif.read_heif 直接API (10s超时) ===")
try:
    import pillow_heif as ph
    def _read():
        heif = ph.read_heif(P)
        return f"mode={heif.mode} size=({heif.size[0]}x{heif.size[1]})"
    r = with_timeout(_read, 10)
    print(f"  result: {r}")
except TimeoutError:
    print("  !! read_heif 超时>10s — 挂死了 !!")
except Exception as e:
    print(f"  FAIL: {e}")

# 6. pillow_heif with convert_hdr_to_8bit
print("\n=== 6. read_heif(convert_hdr_to_8bit=False) (10s超时) ===")
try:
    import pillow_heif as ph
    def _read2():
        heif = ph.read_heif(P, convert_hdr_to_8bit=False)
        return str(heif.size)
    r = with_timeout(_read2, 10)
    print(f"  result: {r}")
except TimeoutError:
    print("  !! 超时>10s — 挂死了 !!")
except Exception as e:
    print(f"  FAIL: {e}")

# 7. Score cache thumbnail test
print("\n=== 7. 评分缓存缩略图是否存在 ===")
import score_cache
import model_loader as ml
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
# Try loading model to get model_version
try:
    ml._load_onnx(str(_REPO_ROOT / "electron_app" / "model" / "best_model.onnx"))
    mv = ml.model_version()
    row = score_cache.get_score(P, mv)
    tp = (row or {}).get("thumb_path","") if row else ""
    exists = os.path.exists(tp) if tp else False
    print(f"  model_version={mv}")
    print(f"  cached_score={row.get('score') if row else 'none'}")
    print(f"  thumb_path={tp}")
    print(f"  thumb_exists={exists}")
    if exists:
        from PIL import Image as PI
        t = PI.open(tp)
        print(f"  thumb_size={t.size} mode={t.mode}")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n=== 诊断完成 ===")
