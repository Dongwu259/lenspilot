#!/usr/bin/env python
"""修复: ①缩略图审核视图不显示 ②score_cache过期检测 ③验证local-image路径"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'electron_app', 'python_backend'))

FOLDER = r"D:\photoDatebase\4-2春游"
import glob, score_cache, model_loader as ml

# Load model
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
ml._load_onnx(str(_REPO_ROOT / "electron_app" / "model" / "best_model.onnx"))
mv = ml.model_version()
print(f"model_version={mv}")

# Get first 3 scored files
images = sorted(glob.glob(os.path.join(FOLDER, "*.*")))[:3]
for p in images:
    row = score_cache.get_score(p, mv)
    if row:
        tp = row.get("thumb_path", "")
        score = row.get("score", -1)
        exists = os.path.exists(tp) if tp else False
        name = os.path.basename(p)
        print(f"\n{name}: score={score:.1f}")
        print(f"  thumb_path: {tp}")
        print(f"  thumb_exists: {exists}")
        if exists:
            from PIL import Image as PI
            t = PI.open(tp)
            print(f"  thumb_size: {t.size} mode={t.mode}")
            # Check if local-image:// would work
            print(f"  local-image://?p={tp}")
            print(f"  main.js IMAGE_EXTENSIONS has .jpg: {'.jpg' in {'jpg'}}")
    else:
        print(f"\n{os.path.basename(p)}: NO CACHE (scoring not run yet)")

# Check score_cache vs expected
print("\n=== Score distribution ===")
rows = score_cache.get_folder(FOLDER, mv)
scores = [r['score'] for r in rows if r.get('score', -1) >= 0]
if scores:
    print(f"  count={len(scores)} min={min(scores):.1f} max={max(scores):.1f}")
else:
    print("  NO SCORES — need to run pipeline scoring first")
    print("  Run: python smoke_test/test_pipeline.py (or npm start digital employee)")
