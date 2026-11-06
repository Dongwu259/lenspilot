@echo off
echo ==========================================
echo Building: Windows CPU Lightweight Version (v0.9.0, trimmed)
echo ==========================================

cd /d %~dp0

echo.
echo [0/5] Cleaning dev residue + generating build-id...
python clean-before-build.py
if errorlevel 1 echo WARNING: clean step failed

echo.
echo [1/5] Installing ONNX Python dependencies...
cd python_backend
rem python -m pip 而非裸 pip:Scripts 目录不保证在 PATH(2026-09-12 无头环境实测)
python -m pip install -r requirements_onnx.txt --quiet
python -c "import ast,io; v=[l for l in io.open('photo_s/__init__.py',encoding='utf-8') if '__version__' in l][0]; assert '2.5.0' in v, 'photo_s 版本不是 2.5.0: '+v; print('photo_s version OK')"

echo.
echo [2/5] Exporting ONNX model (only needed if re-exporting; not bundled)...
if not exist ..\model\best_model.onnx python export_onnx.py
if errorlevel 1 echo WARNING: ONNX export failed (skip if model already exported)

echo.
echo [2.5/5] Staging local LLM runtime (llama-server, CPU)...
python copy-llm-runtime.py --cpu
if errorlevel 1 echo WARNING: local LLM runtime copy failed (AI 助手本地模型将不可用)

echo.
echo [3/5] Building Python backend with PyInstaller (onedir, ONNX, no torch)...
python -m PyInstaller --onedir --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --collect-all=pillow_heif --collect-data=cv2 --add-data="%~dp0model\face_landmarks.onnx;model" --add-data="%~dp0build-resources\llm;llm" --add-data="%~dp0python_backend\luts;luts" --collect-all=openai --collect-all=mcp --hidden-import=docx --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit --hidden-import=photo_s.mask --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets --exclude-module torch --exclude-module torchvision --exclude-module open_clip --exclude-module scipy --exclude-module pandas --exclude-module matplotlib --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras service.py -n service --distpath ..\build-resources\python_backend --workpath ..\build-resources\tmp --specpath ..\build-resources\tmp
cd ..

echo.
echo [3.5/5] Verifying CPU bundle excludes torch/open_clip...
if exist "build-resources\python_backend\service\_internal\torch" (
    echo ERROR: torch 进了 CPU 包！ & exit /b 1
)
if exist "build-resources\python_backend\service\_internal\open_clip" (
    echo ERROR: open_clip 进了 CPU 包！ & exit /b 1
)
echo OK: CPU 包无 torch/open_clip

echo.
echo [4/5] Stripping dev files (CPU mode)...
python clean-after-pyinstaller.py --cpu
if errorlevel 1 echo WARNING: cleanup step failed

echo.
echo [5/5] Building Electron app (dir target -^> win-unpacked)...
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
echo [6/6] Compiling Inno Setup installer...
if exist "C:\Program Files (x86)\Inno Setup 6\iscc.exe" (
  "C:\Program Files (x86)\Inno Setup 6\iscc.exe" installer-win-cpu.iss
) else (
  echo ERROR: Inno Setup not found. Install Inno Setup 6 then run: iscc installer-win-cpu.iss
  exit /b 1
)

echo.
echo ==========================================
echo Done! CPU installer (ONNX, ~173 MB):
echo ==========================================
dir "..\package\LensPilot-Win-CPU-Setup-0.9.0.exe"
