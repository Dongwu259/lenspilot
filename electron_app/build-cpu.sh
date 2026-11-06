#!/usr/bin/env bash
# bash 版 Windows CPU 构建脚本（等价 build-win-cpu.bat，避开 cmd 转义问题）
# 完整流程：clean-before → PyInstaller(ONNX,含眼检测) → clean-after → electron-builder → iscc
set -e
cd "$(dirname "$0")"
EA="$(pwd -W 2>/dev/null || pwd)"

echo "=== [0/5] clean-before ==="
python clean-before-build.py

echo "=== [1/5] PyInstaller (ONNX, exclude torch/tensorflow) ==="
cd python_backend
python -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"
python -m PyInstaller --onedir \
  --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy \
  --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash \
  --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner \
  --collect-data=cv2 --collect-all=pillow_heif --add-data="${EA}/model/face_landmarks.onnx;model" \
  --add-data="${EA}/build-resources/llm;llm" --add-data="${EA}/python_backend/luts;luts" \
  --collect-all=openai --collect-all=mcp --hidden-import=docx \
  --exclude-module torch --exclude-module torchvision --exclude-module open_clip \
  --exclude-module scipy --exclude-module pandas --exclude-module matplotlib \
  --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras \
  service.py -n service \
  --distpath ../build-resources/python_backend \
  --workpath ../build-resources/tmp \
  --specpath ../build-resources/tmp
cd ..

echo "=== 验证 torch/open_clip 未进包 ==="
if [ -d build-resources/python_backend/service/_internal/torch ]; then
  echo "ERROR: torch 进了 CPU 包！"; exit 1
fi
if [ -d build-resources/python_backend/service/_internal/open_clip ]; then
  echo "ERROR: open_clip 进了 CPU 包！"; exit 1
fi
echo "OK: CPU 包无 torch/open_clip"
echo "=== 验证眼模型已打进 ==="
[ -f build-resources/python_backend/service/_internal/model/face_landmarks.onnx ] \
  && echo "OK: face_landmarks.onnx 已打进" || { echo "ERROR: 眼模型缺失"; exit 1; }

echo "=== [2/5] clean-after (strip dev files + CUDA dlls) ==="
python clean-after-pyinstaller.py --cpu

echo "=== [3/5] electron-builder (electronDist=本地 dist 以避开无头下载/解压卡死) ==="
# app-builder 在 win-unpacked 产出后会去 GitHub 下 icons-bundle.tar.gz；国内常超时，
# 但那步对 dir 目标 + Inno 打包(图标用 assets/icon.ico)无影响。容忍其非零退出，
# 只要 win-unpacked/LensPilot.exe 已生成就继续。
npx electron-builder --config.electronDist=node_modules/electron/dist \
  || echo "electron-builder 返回非零(多为 icons-bundle 网络失败)，检查 win-unpacked 后继续"
[ -f ../package/win-unpacked/LensPilot.exe ] || { echo "ERROR: win-unpacked 未生成，electron-builder 真失败"; exit 1; }

echo "=== 清理 extraResources 误带的 stale tmp ==="
rm -rf ../package/win-unpacked/resources/tmp 2>/dev/null || true

echo "=== 清理 asar.unpacked 中误带的 node_modules/electron 等构建工具 ==="
# electron-builder v26 无视 files 排除规则，会把 devDependencies
# (electron 359MB、electron-winstaller 31MB 等) 自动解包进 asar.unpacked。
# 这些在运行时完全不需要——Electron 用自己的 runtime，Python 后端在 resources/python_backend/。
rm -rf ../package/win-unpacked/resources/app.asar.unpacked/node_modules 2>/dev/null || true
rm -rf ../package/win-unpacked/resources/app.asar.unpacked/build-resources 2>/dev/null || true
echo "asar.unpacked 清理后大小: $(du -sh ../package/win-unpacked/resources/app.asar.unpacked 2>/dev/null | cut -f1 || echo '不存在')"

echo "=== 强制品牌化 LensPilot.exe(rcedit) ==="
# electron-builder 靠 app-builder 下 icons-bundle 来设图标/改 exe 元数据；国内网络失败时
# 会静默跳过，导致 LensPilot.exe 保留默认 Electron 图标 + "Electron" 名称/公司。这里用
# winCodeSign 缓存里的 rcedit 强制补上(幂等，重复跑无害)。
_brand_exe() {
  local exe="$1"
  local rce
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
"C:/Program Files (x86)/Inno Setup 6/iscc.exe" installer-win-cpu.iss

echo "=== [5/5] DONE ==="
ls -la ../package/LensPilot-Win-CPU-Setup-0.9.0.exe 2>&1
