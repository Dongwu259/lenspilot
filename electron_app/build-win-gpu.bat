@echo off
echo ==========================================
echo Building: Windows CUDA GPU Version (v0.9.0, trimmed)
echo ==========================================

cd /d %~dp0

echo.
echo [0/5] Cleaning dev residue + generating build-id...
python clean-before-build.py
if errorlevel 1 echo WARNING: clean step failed

echo.
echo [1/5] Installing CUDA Python dependencies...
cd python_backend
rem python -m pip 而非裸 pip:pip.exe 所在的 Scripts 目录不保证在 PATH,
rem python -m 永远解析到与构建同用的解释器(2026-09-12 无头环境实测)
python -m pip install -r requirements_cuda.txt --quiet
python -m pip install open-clip-torch --quiet
if errorlevel 1 echo ERROR: open-clip-torch install failed & exit /b 1
python -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"

echo.
echo [1.5/5] Staging local LLM runtime (llama-server, CUDA)...
python copy-llm-runtime.py --cuda
if errorlevel 1 echo WARNING: local LLM runtime copy failed (AI 助手本地模型将不可用)

echo.
echo [2/5] Building Python backend with PyInstaller (onedir, PyTorch CUDA)...
python -m PyInstaller --onedir --noconfirm --collect-all=torch --collect-all=torchvision --collect-all=open_clip --collect-all=photo_s_plugin_auto_tone --copy-metadata=open_clip_torch --copy-metadata=huggingface_hub --copy-metadata=safetensors --add-data="%USERPROFILE%\AppData\Local\Programs\Python\Python313\Lib\site-packages\torch\lib;torch\lib" --add-data="%~dp0model\face_landmarks.onnx;model" --add-data="%~dp0build-resources\llm;llm" --add-data="%~dp0python_backend\luts;luts" --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --collect-data=cv2 --collect-all=pillow_heif --collect-all=openai --collect-all=mcp --hidden-import=docx --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit --hidden-import=photo_s.mask --hidden-import=photo_s.segmask --hidden-import=photo_s.cutout --hidden-import=photo_s.search --hidden-import=photo_s.lrxmp --hidden-import=photo_s.lens --hidden-import=photo_s.lensprofile --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras --exclude-module tf2onnx service.py -n service --distpath ..\build-resources\python_backend --workpath ..\build-resources\tmp --specpath ..\build-resources\tmp
cd ..

echo.
echo [3/5] Stripping dev files + trimming CUDA DLLs (safe, tested for ResNet inference)...
python clean-after-pyinstaller.py
python trim-cuda-dlls.py
if errorlevel 1 echo ERROR: DLL trim failed & exit /b 1

echo.
echo [4/5] Building Electron app (dir target -^> win-unpacked)...
rem Skip electron-builder if win-unpacked exists with current app.asar
if exist "..\package\win-unpacked\LensPilot.exe" (
    echo win-unpacked exists, updating python_backend only...
    if exist "..\package\win-unpacked\resources\python_backend\service" (
        rmdir /s /q "..\package\win-unpacked\resources\python_backend\service"
    )
    xcopy /e /i /y "build-resources\python_backend\service" "..\package\win-unpacked\resources\python_backend\service"
    if not exist "..\package\win-unpacked\resources\python_backend\service" (
        mkdir "..\package\win-unpacked\resources\python_backend\service"
        xcopy /e /i /y "build-resources\python_backend\service" "..\package\win-unpacked\resources\python_backend\service"
    )
) else (
    echo Building with electron-builder...
    call npm run build
    if errorlevel 1 (
        echo electron-builder failed, trying manual asar pack...
        call scripts\build-manual.bat
        if errorlevel 1 exit /b 1
    )
)

rem Clean any stale tmp/ directory copied by electron-builder extraResources
if exist "..\package\win-unpacked\resources\tmp" (
    echo Removing stale tmp/ from resources...
    rmdir /s /q "..\package\win-unpacked\resources\tmp"
)

echo.
echo === Cleaning asar.unpacked build tools mistakenly included ===
rem electron-builder v26 auto-unpacks devDependencies (electron ~359MB, etc.)
rem despite "files" exclusions. These are NOT needed at runtime.
if exist "..\package\win-unpacked\resources\app.asar.unpacked\node_modules" (
    echo Removing app.asar.unpacked\node_modules...
    rmdir /s /q "..\package\win-unpacked\resources\app.asar.unpacked\node_modules"
)
if exist "..\package\win-unpacked\resources\app.asar.unpacked\build-resources" (
    echo Removing app.asar.unpacked\build-resources...
    rmdir /s /q "..\package\win-unpacked\resources\app.asar.unpacked\build-resources"
)

echo.
echo === Branding LensPilot.exe with rcedit ===
rem electron-builder may skip icon/metadata if icons-bundle download fails
setlocal
for /f "tokens=2 delims=: " %%v in ('node -e "console.log(require('./package.json').version)"') do set VER=%%v
for /f "delims=" %%r in ('dir /b /s "%LOCALAPPDATA%\electron-builder\Cache\winCodeSign\*\rcedit-x64.exe" 2^>nul') do set RCEDIT=%%r
if defined RCEDIT (
    "%RCEDIT%" "..\package\win-unpacked\LensPilot.exe" ^
        --set-icon "%~dp0assets\icon.ico" ^
        --set-version-string "ProductName" "LensPilot" ^
        --set-version-string "FileDescription" "LensPilot" ^
        --set-version-string "CompanyName" "Dongwu259" ^
        --set-version-string "LegalCopyright" "Copyright (C) 2026 钱东吴" ^
        --set-version-string "OriginalFilename" "LensPilot.exe" ^
        --set-version-string "InternalName" "LensPilot" ^
        --set-file-version "%VER%.0" --set-product-version "%VER%.0"
    echo rcedit branding complete (%VER%)
) else (
    echo WARNING: rcedit not found, skipping branding
)
endlocal

echo.
echo [5/5] Compiling Inno Setup installer...
if exist "C:\Program Files (x86)\Inno Setup 6\iscc.exe" (
  "C:\Program Files (x86)\Inno Setup 6\iscc.exe" installer-win-gpu.iss
) else (
  echo ERROR: Inno Setup not found. Install Inno Setup 6 then run: iscc installer-win-gpu.iss
  exit /b 1
)

echo.
echo ==========================================
echo Done! GPU installer (trimmed, ~752 MB):
echo ==========================================
dir "..\package\LensPilot-Win-GPU-Setup-0.9.0.exe"
echo.
echo Savings breakdown:
echo   - Removed 14 CUDA DLLs (cusparse, cufft, cusolver, cudnn_adv, nvrtc,
echo     cublasLt, curand, nvperf_host, nvToolsExt, caffe2_nvrtc,
echo     cudnn_engines_precompiled)
echo   - Kept cudnn_engines_runtime_compiled (27MB) as lightweight alternative
echo   - Kept cupti64 (4MB) which is a dependency of shm.dll
echo   - Total lib: 4.0GB -^> 1.43GB
echo   - Installer: 1.99GB -^> ~780MB (61%% reduction)
