#!/usr/bin/env bash
# build-local.sh — 纯本地打包，完全不使用 electron-builder（因此不联网、不下载、
# 不会遇到 electron 下载卡死 / icons-bundle 图标失败 / app.asar 被覆盖 / package.json
# 被回退 这些 electron-builder 的坑）。
#
# 原理：electron-builder 产出的 win-unpacked 本质就是
#   node_modules/electron/dist/*  +  electron.exe→LensPilot.exe(改名+rcedit品牌化)
#   + resources/app.asar(从源码 asar pack) + resources/python_backend(PyInstaller产物)
#   + 裁剪 locales。
# 这些步骤全部可本地完成，已实测 vanilla electron.exe 不强制 asar 完整性校验、能正常加载。
#
# 用法：
#   bash build-local.sh cpu     # ONNX 轻量版
#   bash build-local.sh gpu     # PyTorch CUDA 版
set -e

MODE="${1:-}"
[ "$MODE" = "cpu" ] || [ "$MODE" = "gpu" ] || { echo "用法: build-local.sh cpu|gpu"; exit 1; }

cd "$(dirname "$0")"
EA="$(pwd -W 2>/dev/null || pwd)"
DIST="node_modules/electron/dist"
OUT="../package/win-unpacked"
TORCH_LIB="${USERPROFILE}/AppData/Local/Programs/Python/Python313/Lib/site-packages/torch/lib"

# rcedit 品牌化（winCodeSign 缓存里自带，无需下载）
_brand_exe() {
  local exe="$1" rce ver
  rce=$(ls /c/Users/*/AppData/Local/electron-builder/Cache/winCodeSign/*/rcedit-x64.exe 2>/dev/null | head -1)
  [ -z "$rce" ] && { echo "WARNING: 未找到 rcedit，跳过品牌化（图标/名称会是 Electron 默认）"; return 0; }
  ver=$(grep '"version"' package.json | head -1 | sed 's/.*"version"[^"]*"\([^"]*\)".*/\1/')
  "$rce" "$exe" \
    --set-icon "${EA}/assets/icon.ico" \
    --set-version-string "ProductName" "LensPilot" \
    --set-version-string "FileDescription" "LensPilot" \
    --set-version-string "CompanyName" "Dongwu259" \
    --set-version-string "LegalCopyright" "Copyright (C) 2026 钱东吴" \
    --set-version-string "OriginalFilename" "LensPilot.exe" \
    --set-version-string "InternalName" "LensPilot" \
    --set-file-version "${ver}.0" --set-product-version "${ver}.0" \
    && echo "rcedit 品牌化完成 (${ver})" || echo "WARNING: rcedit 失败"
}

echo "=== [0/5] clean-before ==="
python clean-before-build.py

echo "=== [1/5] PyInstaller ($MODE) ==="
cd python_backend
if [ "$MODE" = "cpu" ]; then
  python -m PyInstaller --onedir --noconfirm \
    --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy \
    --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash \
    --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner \
    --collect-data=cv2 --collect-all=pillow_heif --add-data="${EA}/model/face_landmarks.onnx;model" \
    --collect-all=openai --collect-all=mcp --hidden-import=docx \
    --exclude-module torch --exclude-module torchvision \
    --exclude-module scipy --exclude-module pandas --exclude-module matplotlib \
    --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras \
    service.py -n service \
    --distpath ../build-resources/python_backend --workpath ../build-resources/tmp --specpath ../build-resources/tmp
else
  python -m PyInstaller --onedir --noconfirm \
    --collect-all=torch --collect-all=torchvision \
    --add-data="${TORCH_LIB};torch/lib" \
    --add-data="${EA}/model/face_landmarks.onnx;model" \
    --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml \
    --hidden-import=imagehash \
    --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner \
    --collect-data=cv2 --collect-all=pillow_heif --collect-all=openai --collect-all=mcp --hidden-import=docx \
    --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras --exclude-module tf2onnx \
    service.py -n service \
    --distpath ../build-resources/python_backend --workpath ../build-resources/tmp --specpath ../build-resources/tmp
fi
cd ..

echo "=== [2/5] clean-after + 校验 ==="
if [ "$MODE" = "cpu" ]; then
  python clean-after-pyinstaller.py --cpu
  [ -d build-resources/python_backend/service/_internal/torch ] && { echo "ERROR: torch 混入 CPU 包"; exit 1; } || echo "OK: 无 torch"
else
  python clean-after-pyinstaller.py
  python trim-cuda-dlls.py
fi
# tensorflow 兜底清理（正常已被 --exclude-module 挡住）
rm -rf build-resources/python_backend/service/_internal/tensorflow 2>/dev/null || true
[ -f build-resources/python_backend/service/_internal/model/face_landmarks.onnx ] \
  && echo "OK: 眼模型已打进" || { echo "ERROR: 眼模型缺失"; exit 1; }
[ -d build-resources/python_backend/service/_internal/pillow_heif ] \
  && echo "OK: pillow_heif 已打进" || echo "WARNING: pillow_heif 未打进（HEIC 将不可用）"

echo "=== [3/5] 组装 win-unpacked（纯本地，无 electron-builder）==="
rm -rf "$OUT"; mkdir -p "$OUT"
cp -r "$DIST"/. "$OUT"/
mv "$OUT/electron.exe" "$OUT/LensPilot.exe"
[ -f "$OUT/LICENSE" ] && mv "$OUT/LICENSE" "$OUT/LICENSE.electron.txt" || true
# app.asar：从当前源码打包（main.js/preload/python_service/package.json/src，无 node_modules）
rm -rf /tmp/lp_stage; mkdir -p /tmp/lp_stage
cp main.js preload.js python_service.js package.json /tmp/lp_stage/
cp THIRD_PARTY_LICENSES.txt LICENSE-Apache-2.0.txt /tmp/lp_stage/ 2>/dev/null || true
cp -r src /tmp/lp_stage/
npx asar pack /tmp/lp_stage "$OUT/resources/app.asar"
# extraResources：PyInstaller 产物 + build-id
cp -r build-resources/python_backend "$OUT/resources/python_backend"
cp build-resources/build-id.json "$OUT/resources/" 2>/dev/null || true
# 裁剪 locales（留 zh-CN / en-US / en）
( cd "$OUT/locales" && for f in *.pak; do case "$f" in zh-CN.pak|en-US.pak|en.pak) ;; *) rm -f "$f";; esac; done )
# 品牌化 exe
_brand_exe "$OUT/LensPilot.exe"
echo "win-unpacked 组装完成：$(du -sh "$OUT" 2>/dev/null | awk '{print $1}')"

echo "=== [4/5] Inno Setup ==="
if [ "$MODE" = "cpu" ]; then ISS=installer-win-cpu.iss; else ISS=installer-win-gpu.iss; fi
"C:/Program Files (x86)/Inno Setup 6/iscc.exe" "$ISS"

echo "=== [5/5] DONE ==="
if [ "$MODE" = "cpu" ]; then ls -la ../package/LensPilot-Win-CPU-Setup-*.exe 2>&1 | tail -1
else ls -la ../package/LensPilot-Win-GPU-Setup-*.exe 2>&1 | tail -1; fi
