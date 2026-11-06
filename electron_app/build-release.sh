#!/usr/bin/env bash
# =============================================================================
# build-release.sh — LensPilot 一键发布打包脚本 v0.8.8
# =============================================================================
# 用法:
#   bash build-release.sh cpu     # ONNX 轻量版 (~173 MB)
#   bash build-release.sh gpu     # PyTorch CUDA 版 (~752 MB)
#
# 流程: clean → PyInstaller → trim → asar → brand → InnoSetup
# =============================================================================
set -e

MODE="${1:-}"
if [ "$MODE" != "cpu" ] && [ "$MODE" != "gpu" ]; then
  echo "用法: bash build-release.sh cpu|gpu"
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd -W 2>/dev/null || pwd)"
EA="$ROOT/electron_app"
DIST="$ROOT/package"
UNPACKED="$DIST/win-unpacked"
PY_BACKEND="$EA/python_backend"
BUILD_RES="$EA/build-resources"
ASSETS="$EA/assets"
VER="0.8.8"

echo "============================================"
echo " LensPilot v${VER} — ${MODE^^} 打包"
echo "============================================"

# ── 0. 验证环境 ──
echo "[0/6] 验证环境..."
command -v python >/dev/null 2>&1 || { echo "ERROR: python 未安装"; exit 1; }
ISCC="C:/Program Files (x86)/Inno Setup 6/iscc.exe"
[ -f "$ISCC" ] || { echo "ERROR: Inno Setup 6 未安装于 $ISCC"; exit 1; }
[ -f "$ASSETS/icon.ico" ] || { echo "ERROR: $ASSETS/icon.ico 不存在"; exit 1; }

# ── 1. 清理 + 生成 build-id ──
echo "[1/6] 清理旧产物 + 生成 build-id..."
cd "$EA"
python clean-before-build.py 2>&1 | tail -1
rm -rf "$BUILD_RES/python_backend/service" "$BUILD_RES/tmp"/* 2>/dev/null || true

# ── 2. PyInstaller 构建 Python 后端 ──
echo "[2/6] PyInstaller 构建 Python 后端 (${MODE})..."
cd "$PY_BACKEND"
FACE_MODEL="$EA/model/face_landmarks.onnx"

COMMON_FLAGS="--onedir --noconfirm \
  --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml \
  --hidden-import=imagehash --hidden-import=feedback_queue --hidden-import=feedback_uploader \
  --hidden-import=eye_detector --hidden-import=pipeline_runner \
  --add-data=\"${FACE_MODEL};model\" \
  --collect-all=pillow_heif --collect-data=cv2 \
  --collect-all=openai --collect-all=mcp --hidden-import=docx \
  --exclude-module=tensorflow --exclude-module=tensorboard --exclude-module=keras \
  --exclude-module=tf2onnx \
  --distpath ../build-resources/python_backend \
  --workpath ../build-resources/tmp \
  --specpath ../build-resources/tmp"

if [ "$MODE" = "cpu" ]; then
  eval python -m PyInstaller $COMMON_FLAGS \
    --hidden-import=onnxruntime \
    --exclude-module=torch --exclude-module=torchvision \
    --exclude-module=scipy --exclude-module=pandas --exclude-module=matplotlib \
    service.py -n service -y 2>&1 | tail -3
else
  eval python -m PyInstaller $COMMON_FLAGS \
    --hidden-import=torch --hidden-import=torchvision \
    --collect-all=torch --collect-all=torchvision \
    service.py -n service -y 2>&1 | tail -3
fi
cd "$EA"

# ── 3. 裁剪 (clean-after + GPU CUDA trim) ──
echo "[3/6] 裁剪开发文件..."
if [ "$MODE" = "cpu" ]; then
  python clean-after-pyinstaller.py --cpu 2>&1 | tail -1
else
  python clean-after-pyinstaller.py 2>&1 | tail -1
  python trim-cuda-dlls.py 2>&1 | tail -3
fi
echo "  后端体积: $(du -sh $BUILD_RES/python_backend/service/ | cut -f1)"

# ── 4. 打包 app.asar ──
echo "[4/6] 打包 app.asar..."
rm -rf "$EA/_asar_tmp" && mkdir "$EA/_asar_tmp"

# 仅打包运行时需要的文件
cp "$EA/main.js" "$EA/preload.js" "$EA/python_service.js" "$EA/package.json" "$EA/_asar_tmp/"
cp -r "$EA/src" "$EA/_asar_tmp/"
cp -r "$EA/assets" "$EA/_asar_tmp/"
# 排除开发/调试文件
rm -rf "$EA/_asar_tmp/src/js/_check" 2>/dev/null || true
find "$EA/_asar_tmp" -name "*.map" -delete 2>/dev/null || true
find "$EA/_asar_tmp" -name ".DS_Store" -delete 2>/dev/null || true

npx asar pack "$EA/_asar_tmp" "$UNPACKED/resources/app.asar" 2>&1
rm -rf "$EA/_asar_tmp"
echo "  asar: $(ls -lh $UNPACKED/resources/app.asar | awk '{print $5}')"

# ── 5. 更新 win-unpacked ──
echo "[5/6] 更新 win-unpacked..."
rm -rf "$UNPACKED/resources/python_backend/service"
cp -r "$BUILD_RES/python_backend/service" "$UNPACKED/resources/python_backend/"
cp "$BUILD_RES/build-id.json" "$UNPACKED/resources/" 2>/dev/null || true
# 清理残留
rm -rf "$UNPACKED/resources/tmp" "$UNPACKED/resources/app.asar.unpacked" 2>/dev/null || true

# ── 品牌化 exe ──
echo "  品牌化 LensPilot.exe..."
RCE=$(find /c/Users -name "rcedit-x64.exe" -path "*/electron-builder/*" 2>/dev/null | sort -r | head -1)
if [ -n "$RCE" ]; then
  "$RCE" "$UNPACKED/LensPilot.exe" \
    --set-icon "$ASSETS/icon.ico" \
    --set-version-string "ProductName" "LensPilot" \
    --set-version-string "FileDescription" "LensPilot - AI 智能摄影工作流" \
    --set-version-string "CompanyName" "Dongwu259" \
    --set-version-string "LegalCopyright" "Copyright (C) 2026 钱东吴" \
    --set-version-string "OriginalFilename" "LensPilot.exe" \
    --set-version-string "InternalName" "LensPilot" \
    --set-file-version "${VER}.0" --set-product-version "${VER}.0" 2>&1
  echo "  品牌化完成"
else
  echo "  WARNING: 未找到 rcedit，跳过品牌化"
fi

echo "  win-unpacked 总体积: $(du -sh $UNPACKED | cut -f1)"

# ── 6. Inno Setup 编译安装包 ──
echo "[6/6] 编译安装包..."
ISS_FILE="$EA/installer-win-${MODE}.iss"
"$ISCC" "$ISS_FILE" 2>&1 | tail -3

# ── 7. 代码签名（可选） ──
SIGNTOOL=$(find "C:/Program Files (x86)/Windows Kits/10/bin" -name "signtool.exe" -path "*/x64/*" 2>/dev/null | sort -r | head -1)
CERT_FILE="$EA/certificate.pfx"
CERT_PASS="${SIGN_PASSWORD:-}"

if [ -f "$CERT_FILE" ] && [ -n "$SIGNTOOL" ] && [ -n "$CERT_PASS" ]; then
  echo "[7/7] 代码签名..."
  # 签名 win-unpacked 主要 exe
  "$SIGNTOOL" sign /fd SHA256 /f "$CERT_FILE" /p "$CERT_PASS" \
    /tr http://timestamp.digicert.com /td SHA256 \
    "$UNPACKED/LensPilot.exe" 2>&1 | tail -1
  # 签名安装包
  INSTALLER=$(ls "$DIST"/LensPilot-Win-${MODE^^}-Setup-${VER}.exe 2>/dev/null)
  "$SIGNTOOL" sign /fd SHA256 /f "$CERT_FILE" /p "$CERT_PASS" \
    /tr http://timestamp.digicert.com /td SHA256 \
    "$INSTALLER" 2>&1 | tail -1
  echo "  签名完成"
else
  echo "[7/7] ⚠ 跳过签名（将 certificate.pfx 放到 electron_app/ 并设 SIGN_PASSWORD 环境变量以启用）"
fi

# ── 完成 ──
INSTALLER=$(ls "$DIST"/LensPilot-Win-${MODE^^}-Setup-${VER}.exe 2>/dev/null)
echo ""
echo "============================================"
echo " ✅ 打包完成"
echo "  文件: $INSTALLER"
echo "  大小: $(ls -lh "$INSTALLER" | awk '{print $5}')"
echo "============================================"
