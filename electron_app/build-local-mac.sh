#!/usr/bin/env bash
# build-local-mac.sh — 纯本地打 Mac .dmg，完全不使用 electron-builder。
# 与 Windows 的 build-local.sh 同思路：electron-builder 产出的 .app 本质就是
#   node_modules/electron/dist/Electron.app  改名 + 改 Info.plist + 换图标
#   + Contents/Resources/app.asar(源码 asar pack) + Contents/Resources/python_backend
#   + ad-hoc 代码签名(Apple Silicon 必须) + hdiutil 打 dmg。
# 这些全部本地可做、零下载。
#
# 用法(在 Mac 上跑)：
#   ./build-local-mac.sh cpu     # ONNX 轻量版(推荐)
#   ./build-local-mac.sh gpu     # PyTorch MPS 版(仅 Apple Silicon)
#
# ⚠️ 本脚本在 Windows 端编写、未在 Mac 实测。Mac agent 请按脚本执行，并用文末
#    「启动自检」验证；若某步失败，参照 MAC打包操作说明.md 的排查表修正。
set -e

MODE="${1:-cpu}"
[ "$MODE" = "cpu" ] || [ "$MODE" = "gpu" ] || { echo "用法: build-local-mac.sh cpu|gpu"; exit 1; }
cd "$(dirname "$0")"
EA="$(pwd)"
DIST="node_modules/electron/dist"
ARCH=$(uname -m)                                   # arm64 或 x86_64
VER=$(grep '"version"' package.json | head -1 | sed 's/.*"version"[^"]*"\([^"]*\)".*/\1/')
APP="../package/mac/LensPilot.app"
if [ "$MODE" = "cpu" ]; then DMG="../package/LensPilot-Mac-CPU-${VER}.dmg"; else DMG="../package/LensPilot-Mac-GPU-${VER}.dmg"; fi

[ -d "$DIST/Electron.app" ] || { echo "ERROR: 没找到 $DIST/Electron.app —— 先 npm install 生成本机 electron"; exit 1; }

echo "=== [0/6] clean-before ==="
python3 clean-before-build.py || echo "warn: clean 失败(继续)"

echo "=== [0.5/6] Staging local LLM runtime (llama-server, Metal/CPU) ==="
# 优先 --src/LLAMA_MAC_DIR，其次 brew 装的 llama.cpp
LLAMA_SRC="${LLAMA_MAC_DIR:-}"
if [ -z "$LLAMA_SRC" ] && command -v brew >/dev/null 2>&1 && [ -f "$(brew --prefix llama.cpp 2>/dev/null)/bin/llama-server" ]; then
  LLAMA_SRC="$(brew --prefix llama.cpp)/bin"
fi
if [ -n "$LLAMA_SRC" ] && [ -f "$LLAMA_SRC/llama-server" ]; then
  python3 copy-llm-runtime.py --mac --src "$LLAMA_SRC" || echo "warn: 本地 LLM 运行时暂存失败(继续)"
else
  echo "warn: 未找到 llama-server(mac)，AI 助手本地模型将不可用。"
  echo "      安装: brew install llama.cpp 后重跑，或 LLAMA_MAC_DIR=/path/to/bin python3 copy-llm-runtime.py --mac"
fi

echo "=== [1/6] PyInstaller ($MODE, onedir) ==="
cd python_backend
# 联机拍摄(可选): 本机已装 python-gphoto2 时打进包；未装则跳过——运行时联机拍摄
# 自动降级为"文件夹监听"模式。启用: brew install gphoto2 swig pkg-config && pip3 install gphoto2
GP_FLAG=""
if python3 -c "import gphoto2" 2>/dev/null; then
  GP_FLAG="--collect-all=gphoto2"
  echo "OK: 检测到 gphoto2, 联机拍摄(相机直连)将打入包内"
else
  echo "info: 未安装 gphoto2, 联机拍摄将仅支持文件夹监听模式"
fi
if [ "$MODE" = "cpu" ]; then
  python3 -m PyInstaller --onedir --noconfirm \
    --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy \
    --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash \
    --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --hidden-import=editor_presets \
    --collect-data=cv2 --collect-all=pillow_heif --add-data="$(cd .. && pwd)/model/face_landmarks.onnx:model" \
    --add-data="$(cd .. && pwd)/model/resnet18.yaml:model" \
    --add-data="$(cd .. && pwd)/build-resources/llm:llm" \
    --add-data="$(cd .. && pwd)/python_backend/luts:luts" \
    --collect-all=openai --collect-all=mcp --hidden-import=docx \
    $GP_FLAG \
    --exclude-module torch --exclude-module torchvision \
    --exclude-module scipy --exclude-module pandas --exclude-module matplotlib \
    --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras \
    service.py -n service \
    --distpath ../build-resources/python_backend --workpath ../build-resources/tmp --specpath ../build-resources/tmp
else
  python3 -m PyInstaller --onedir --noconfirm \
    --collect-all=torch --collect-all=torchvision \
    --add-data="$(cd .. && pwd)/model/face_landmarks.onnx:model" \
    --add-data="$(cd .. && pwd)/model/resnet18.yaml:model" \
    --add-data="$(cd .. && pwd)/build-resources/llm:llm" \
    --add-data="$(cd .. && pwd)/python_backend/luts:luts" \
    --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash \
    --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --hidden-import=editor_presets \
    --collect-data=cv2 --collect-all=pillow_heif --collect-all=openai --collect-all=mcp --hidden-import=docx \
    $GP_FLAG \
    --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras --exclude-module tf2onnx \
    service.py -n service \
    --distpath ../build-resources/python_backend --workpath ../build-resources/tmp --specpath ../build-resources/tmp
fi
cd ..

echo "=== [2/6] clean-after + 校验 ==="
if [ "$MODE" = "cpu" ]; then
  python3 clean-after-pyinstaller.py --cpu
  [ -d build-resources/python_backend/service/_internal/torch ] && { echo "ERROR: torch 混入 CPU 包"; exit 1; } || echo "OK: 无 torch"
else
  python3 clean-after-pyinstaller.py
fi
rm -rf build-resources/python_backend/service/_internal/tensorflow 2>/dev/null || true
echo "后端可执行架构:"; file build-resources/python_backend/service/service
[ -f build-resources/python_backend/service/_internal/model/face_landmarks.onnx ] && echo "OK: 眼模型已打进" || { echo "ERROR: 眼模型缺失"; exit 1; }
[ -f build-resources/python_backend/service/_internal/model/resnet18.yaml ] && echo "OK: resnet18.yaml 已打进" || { echo "ERROR: resnet18.yaml 缺失(评分会塌缩到 1.0-1.x)"; exit 1; }
[ -d build-resources/python_backend/service/_internal/pillow_heif ] && echo "OK: pillow_heif 已打进(HEIC 可用)" || echo "warn: pillow_heif 未打进"

echo "=== [3/6] 生成 icon.icns(若没有现成的) ==="
ICNS="assets/icon.icns"
if [ ! -f "$ICNS" ]; then
  SRC=""
  for c in assets/icon-1024.png assets/icon-512.png assets/icon.png assets/icon-256.png; do [ -f "$c" ] && { SRC="$c"; break; }; done
  if [ -n "$SRC" ] && command -v iconutil >/dev/null 2>&1; then
    echo "从 $SRC 生成 icns..."
    rm -rf /tmp/lp.iconset; mkdir -p /tmp/lp.iconset
    for s in 16 32 64 128 256 512; do
      sips -z $s $s "$SRC" --out "/tmp/lp.iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      sips -z $((s*2)) $((s*2)) "$SRC" --out "/tmp/lp.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns /tmp/lp.iconset -o "$ICNS" 2>/dev/null && echo "已生成 $ICNS" || { echo "warn: icns 生成失败，将用 electron 默认图标"; ICNS=""; }
  else
    echo "warn: 无 PNG 源或无 iconutil，将用 electron 默认图标"; ICNS=""
  fi
fi

echo "=== [4/6] 组装 LensPilot.app(纯本地) ==="
rm -rf "$APP"; mkdir -p "$(dirname "$APP")"
cp -R "$DIST/Electron.app" "$APP"
mv "$APP/Contents/MacOS/Electron" "$APP/Contents/MacOS/LensPilot"
PB=/usr/libexec/PlistBuddy
PL="$APP/Contents/Info.plist"
$PB -c "Set :CFBundleExecutable LensPilot" "$PL"
$PB -c "Set :CFBundleName LensPilot" "$PL"
$PB -c "Set :CFBundleDisplayName LensPilot" "$PL" 2>/dev/null || $PB -c "Add :CFBundleDisplayName string LensPilot" "$PL"
$PB -c "Set :CFBundleIdentifier com.dongwu.lenspilot" "$PL"
$PB -c "Set :CFBundleShortVersionString ${VER}" "$PL"
$PB -c "Set :CFBundleVersion ${VER}" "$PL"
if [ -n "$ICNS" ]; then
  cp "$ICNS" "$APP/Contents/Resources/lenspilot.icns"
  $PB -c "Set :CFBundleIconFile lenspilot" "$PL"
fi
# app.asar：从当前源码打包(无 node_modules)
rm -rf /tmp/lp_stage; mkdir -p /tmp/lp_stage
cp THIRD_PARTY_LICENSES.txt LICENSE-Apache-2.0.txt /tmp/lp_stage/ 2>/dev/null || true
cp main.js preload.js python_service.js package.json /tmp/lp_stage/
cp -R src /tmp/lp_stage/
npx asar pack /tmp/lp_stage "$APP/Contents/Resources/app.asar"
# extraResources：PyInstaller 产物 + build-id → Contents/Resources/
cp -R build-resources/python_backend "$APP/Contents/Resources/python_backend"
cp build-resources/build-id.json "$APP/Contents/Resources/" 2>/dev/null || true

echo "=== [5/6] ad-hoc 代码签名(Apple Silicon 必须，否则报「已损坏」无法启动) ==="
# 从内到外签：先 Frameworks 里的 dylib/helper，再签整包。
find "$APP/Contents/Frameworks" -name "*.dylib" -exec codesign --force --sign - {} \; 2>/dev/null || true
find "$APP/Contents/Frameworks" -name "*.app" -exec codesign --force --deep --sign - {} \; 2>/dev/null || true
codesign --force --deep --sign - "$APP" || { echo "ERROR: codesign 失败(见文档排查表)"; exit 1; }
codesign --verify --deep --strict "$APP" 2>&1 && echo "签名校验通过" || echo "warn: 签名校验有告警(ad-hoc 常见，可先启动测试)"

echo "=== [6/6] 制作 DMG(hdiutil，无需 electron-builder) ==="
rm -f "$DMG"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/LensPilot.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "LensPilot ${VER}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo "=== DONE ==="
ls -lh "$DMG"
echo "sha256(填 version.json 的 desktopSha256Mac / desktopSha256MacGPU):"
shasum -a 256 "$DMG"
echo ""
echo "★ 启动自检：open \"$APP\"  —— 应弹出窗口 + 后台起 service 进程(pgrep -f service)；"
echo "  若报「已损坏」→ xattr -cr \"$APP\" 后重试，或见文档「纯本地打包排查」。"
