#!/bin/bash
# Run this on macOS:  ./build-mac.sh cpu   (CPU/ONNX, recommended)  or  ./build-mac.sh gpu   (PyTorch MPS)
set -e

PLATFORM=$(uname -m)
MODE="${1:-cpu}"
echo "=========================================="
echo "Building: macOS $PLATFORM ($MODE, onedir, no model bundled)"
echo "=========================================="

cd "$(dirname "$0")"

echo "[0] Cleaning dev residue + generating build-id..."
python3 clean-before-build.py || echo "WARNING: clean step failed (continuing)"

echo "[0.5] Staging local LLM runtime (llama-server, Metal/CPU)..."
LLAMA_SRC="${LLAMA_MAC_DIR:-}"
if [ -z "$LLAMA_SRC" ] && command -v brew >/dev/null 2>&1 && [ -f "$(brew --prefix llama.cpp 2>/dev/null)/bin/llama-server" ]; then
  LLAMA_SRC="$(brew --prefix llama.cpp)/bin"
fi
if [ -n "$LLAMA_SRC" ] && [ -f "$LLAMA_SRC/llama-server" ]; then
  python3 copy-llm-runtime.py --mac --src "$LLAMA_SRC" || echo "WARNING: local LLM runtime staging failed (continuing)"
else
  echo "WARNING: llama-server (mac) not found — AI Assistant local model unavailable."
  echo "         Install: brew install llama.cpp and rerun, or LLAMA_MAC_DIR=/path/to/bin python3 copy-llm-runtime.py --mac"
fi

# 联机拍摄(可选): 本机已装 python-gphoto2 时打进包；未装则跳过——运行时联机拍摄
# 自动降级为"文件夹监听"模式(外部联机软件输出目录)。启用方法见 MAC打包操作说明.md:
#   brew install gphoto2 swig pkg-config && pip3 install gphoto2
GP_FLAG=""
if python3 -c "import gphoto2" 2>/dev/null; then
    GP_FLAG="--collect-all=gphoto2"
    echo "OK: 检测到 gphoto2, 联机拍摄(相机直连)将打入包内"
else
    echo "info: 未安装 gphoto2, 联机拍摄将仅支持文件夹监听模式"
fi

if [ "$MODE" = "cpu" ]; then
    echo "[1/3] Installing ONNX dependencies..."
    cd python_backend
    pip3 install -r requirements_onnx.txt --quiet
    python3 -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"

    echo "[2/3] Building Python backend (onedir, ONNX, exclude torch)..."
    python3 -m PyInstaller --onedir \
        --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy \
        --hidden-import=PIL --hidden-import=yaml \
        --hidden-import=imagehash --collect-all=openai --collect-all=mcp --hidden-import=docx \
        --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --collect-data=cv2 --collect-all=pillow_heif --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit --hidden-import=photo_s.mask --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets \
        $GP_FLAG \
        --add-data="$(cd .. && pwd)/model/face_landmarks.onnx:model" \
        --add-data="$(cd .. && pwd)/model/resnet18.yaml:model" \
        --add-data="$(cd .. && pwd)/build-resources/llm:llm" \
        --add-data="$(cd .. && pwd)/python_backend/luts:luts" \
        --exclude-module torch --exclude-module torchvision --exclude-module open_clip \
        --exclude-module scipy --exclude-module pandas --exclude-module matplotlib \
        service.py -n service \
        --distpath ../build-resources/python_backend \
        --workpath ../build-resources/tmp --specpath ../build-resources/tmp
    if [ -d ../build-resources/python_backend/service/_internal/torch ]; then echo "ERROR: torch 进了 CPU 包！"; exit 1; fi
    echo "OK: CPU 包无 torch/open_clip"
    cd ..
else
    echo "[1/3] Installing PyTorch (MPS GPU) dependencies..."
    cd python_backend
    pip3 install -r requirements_cuda.txt --quiet
    pip3 install open-clip-torch --quiet
    python3 -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"

    echo "[2/3] Building Python backend (onedir, PyTorch)..."
    python3 -m PyInstaller --onedir \
        --hidden-import=torch --hidden-import=torchvision --hidden-import=rawpy \
        --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml \
        --hidden-import=imagehash --collect-all=openai --collect-all=mcp --hidden-import=docx \
        --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --collect-data=cv2 --collect-all=pillow_heif --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit --hidden-import=photo_s.mask --hidden-import=photo_s.segmask --hidden-import=photo_s.cutout --hidden-import=photo_s.search --hidden-import=photo_s.lrxmp --hidden-import=photo_s.lens --hidden-import=photo_s.lensprofile --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets \
        --collect-all=open_clip --collect-all=photo_s_plugin_auto_tone \
        --copy-metadata=open_clip_torch --copy-metadata=huggingface_hub --copy-metadata=safetensors \
        $GP_FLAG \
        --add-data="$(cd .. && pwd)/model/face_landmarks.onnx:model" \
        --add-data="$(cd .. && pwd)/model/resnet18.yaml:model" \
        --add-data="$(cd .. && pwd)/build-resources/llm:llm" \
        --add-data="$(cd .. && pwd)/python_backend/luts:luts" \
        --exclude-module scipy --exclude-module pandas --exclude-module matplotlib \
        service.py -n service \
        --distpath ../build-resources/python_backend \
        --workpath ../build-resources/tmp --specpath ../build-resources/tmp
    cd ..
fi

# No model files are bundled — first launch downloads from the server.
# (If you want offline-ready bundles, copy onnx/pth into build-resources/model/ here.)

echo "[3/3] Building Electron DMG..."
npm run build

echo "Done! Output in ../package/"
ls -lh ../package/*.dmg
