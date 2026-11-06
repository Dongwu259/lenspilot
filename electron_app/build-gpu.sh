#!/usr/bin/env bash
# bash 版 Windows GPU 构建脚本（等价 build-win-gpu.bat，避开 cmd 转义问题）
# 完整流程：clean-before → PyInstaller(PyTorch CUDA,含眼检测) → clean-after+trim → 组装 → iscc
# 注：GPU 与 CPU 共用同一 Electron 前端(win-unpacked)，若已由 CPU 构建生成则只换 python_backend。
set -e
cd "$(dirname "$0")"
EA="$(pwd -W 2>/dev/null || pwd)"
TORCH_LIB="${USERPROFILE}/AppData/Local/Programs/Python/Python313/Lib/site-packages/torch/lib"

echo "=== [0/5] clean-before ==="
python clean-before-build.py

echo "=== [1/5] PyInstaller (PyTorch CUDA, exclude tensorflow) ==="
cd python_backend
python -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"
pip install open-clip-torch --quiet 2>/dev/null || python -m pip install open-clip-torch --quiet
python -m PyInstaller --onedir --noconfirm \
  --collect-all=torch --collect-all=torchvision \
  --collect-all=open_clip --collect-all=photo_s_plugin_auto_tone \
  --copy-metadata=open_clip_torch --copy-metadata=huggingface_hub --copy-metadata=safetensors \
  --add-data="${TORCH_LIB};torch/lib" \
  --add-data="${EA}/model/face_landmarks.onnx;model" \
  --add-data="${EA}/build-resources/llm;llm" --add-data="${EA}/python_backend/luts;luts" \
  --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml \
  --hidden-import=imagehash \
  --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner \
  --collect-data=cv2 --collect-all=pillow_heif --collect-all=openai --collect-all=mcp --hidden-import=docx \
  --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade \
  --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server \
  --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr \
  --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench \
  --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin \
  --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics \
  --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit \
  --hidden-import=photo_s.mask --hidden-import=photo_s.segmask --hidden-import=photo_s.cutout \
  --hidden-import=photo_s.search --hidden-import=photo_s.lrxmp --hidden-import=photo_s.lens \
  --hidden-import=photo_s.lensprofile --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager \
  --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets \
  --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras --exclude-module tf2onnx \
  service.py -n service \
  --distpath ../build-resources/python_backend \
  --workpath ../build-resources/tmp \
  --specpath ../build-resources/tmp
cd ..

echo "=== 验证 torch 已进包 + 眼模型已打进 + open_clip 已进包 + 无 tensorflow ==="
[ -d build-resources/python_backend/service/_internal/torch ] \
  && echo "OK: torch 已进包" || echo "WARNING: torch 未进 GPU 包"
[ -f build-resources/python_backend/service/_internal/model/face_landmarks.onnx ] \
  && echo "OK: face_landmarks.onnx 已打进" || { echo "ERROR: 眼模型缺失"; exit 1; }
[ -d build-resources/python_backend/service/_internal/open_clip ] \
  && echo "OK: open_clip 已进包" || echo "WARNING: open_clip 未进 GPU 包(AI 自动修图将降级 suggest)"
[ -d build-resources/python_backend/service/_internal/photo_s_plugin_auto_tone ] \
  && echo "OK: auto-tone 插件已进包" || echo "WARNING: auto-tone 插件未进包"
if [ -d build-resources/python_backend/service/_internal/tensorflow ]; then
  echo "WARNING: tensorflow 混入(死重~1GB)，删除中..."; rm -rf build-resources/python_backend/service/_internal/tensorflow
fi

echo "=== [2/5] clean-after + trim CUDA dlls ==="
python clean-after-pyinstaller.py
python trim-cuda-dlls.py

echo "=== [3/5] 组装 win-unpacked ==="
if [ -f ../package/win-unpacked/LensPilot.exe ]; then
  echo "win-unpacked 已存在(CPU 已构建同版前端)，仅替换 python_backend..."
  rm -rf ../package/win-unpacked/resources/python_backend/service
  cp -r build-resources/python_backend/service ../package/win-unpacked/resources/python_backend/service
else
  echo "win-unpacked 不存在，跑 electron-builder(electronDist=本地 dist 避开无头卡死)..."
  npx electron-builder --config.electronDist=node_modules/electron/dist \
    || echo "electron-builder 返回非零(多为 icons-bundle 网络失败)，检查 win-unpacked 后继续"
  [ -f ../package/win-unpacked/LensPilot.exe ] || { echo "ERROR: win-unpacked 未生成"; exit 1; }
  rm -rf ../package/win-unpacked/resources/tmp 2>/dev/null || true
fi

echo "=== 清理 asar.unpacked 中误带的构建工具 ==="
rm -rf ../package/win-unpacked/resources/app.asar.unpacked/node_modules 2>/dev/null || true
rm -rf ../package/win-unpacked/resources/app.asar.unpacked/build-resources 2>/dev/null || true
echo "asar.unpacked 清理后: $(du -sh ../package/win-unpacked/resources/app.asar.unpacked 2>/dev/null | cut -f1 || echo 0)"

echo "=== 强制品牌化 LensPilot.exe(rcedit，防 electron-builder icons-bundle 网络失败漏设图标/元数据) ==="
_brand_exe() {
  local exe="$1"; local rce
  rce=$(ls /c/Users/*/AppData/Local/electron-builder/Cache/winCodeSign/*/rcedit-x64.exe 2>/dev/null | head -1)
  [ -z "$rce" ] && { echo "WARNING: 未找到 rcedit，跳过品牌化"; return 0; }
  [ -f "$exe" ] || { echo "WARNING: $exe 不存在，跳过品牌化"; return 0; }
  local ver; ver=$(grep '"version"' package.json | head -1 | sed 's/.*"version"[^"]*"\([^"]*\)".*/\1/')
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
_brand_exe ../package/win-unpacked/LensPilot.exe

echo "=== [4/5] Inno Setup ==="
"C:/Program Files (x86)/Inno Setup 6/iscc.exe" installer-win-gpu.iss

echo "=== [5/5] DONE ==="
ls -la ../package/LensPilot-Win-GPU-Setup-0.9.0.exe 2>&1
