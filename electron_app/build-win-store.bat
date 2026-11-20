@echo off
rem ============================================================
rem Build LensPilot MSIX for Microsoft Store (CPU edition, x64).
rem
rem Prereqs:
rem   - Windows 10/11 SDK (makeappx/signtool)
rem   - Node.js + electron_app\node_modules installed
rem   - Python 3.13 with python_backend deps (only for a fresh backend build)
rem
rem Identity: edit packaging\msix\appx-identity.json
rem   - Store submission: fill Partner Center "App identity" values.
rem     Upload the produced .msix unsigned - Partner Center signs it.
rem   - Local test: set MSIX_TEST_SIGN=1, publisher must equal the test
rem     cert subject (scripts\make-msix-test-cert.ps1).
rem ============================================================
setlocal EnableDelayedExpansion
cd /d %~dp0

set "VERDOT="
for /f "delims=" %%v in ('node -p "require('./package.json').version"') do set VERDOT=%%v
if not defined VERDOT ( echo ERROR: cannot read version from package.json & exit /b 1 )
set "VERMSIX=!VERDOT!.0"
echo === LensPilot MSIX build v!VERMSIX! (CPU) ===

rem ── [1/6] CPU python backend (reuse if already built) ──────────────
if exist "build-resources\python_backend\service\service.exe" (
    echo [1/6] python backend exists, skipping PyInstaller
) else (
    echo [1/6] building CPU python backend...
    python clean-before-build.py
    if errorlevel 1 echo WARNING: clean step failed
    set "EDIR=%CD%"
    cd python_backend
    python -m pip install -r requirements_onnx.txt --quiet
    python -m PyInstaller --onedir --hidden-import=onnxruntime --hidden-import=rawpy --hidden-import=numpy --hidden-import=PIL --hidden-import=yaml --hidden-import=imagehash --hidden-import=feedback_queue --hidden-import=feedback_uploader --hidden-import=eye_detector --hidden-import=pipeline_runner --collect-all=pillow_heif --collect-data=cv2 --add-data="!EDIR!\model\face_landmarks.onnx;model" --add-data="!EDIR!\build-resources\llm;llm" --add-data="!EDIR!\python_backend\luts;luts" --collect-all=openai --collect-all=mcp --hidden-import=docx --hidden-import=photo_s.engine --hidden-import=photo_s.adjust --hidden-import=photo_s.grade --hidden-import=photo_s.logcurve --hidden-import=photo_s.lut --hidden-import=photo_s.mcp_server --hidden-import=photo_s.contract --hidden-import=photo_s.faceblur --hidden-import=photo_s.hdr --hidden-import=photo_s.select --hidden-import=photo_s.i18n --hidden-import=photo_s.bench --hidden-import=photo_s.watcher --hidden-import=photo_s.registry --hidden-import=photo_s.plugin --hidden-import=photo_s.hooks --hidden-import=photo_s.presets --hidden-import=photo_s.metrics --hidden-import=photo_s.autotone --hidden-import=photo_s.suggest --hidden-import=photo_s.audit --hidden-import=photo_s.mask --hidden-import=photo_s_plugins --hidden-import=auto_tone_manager --hidden-import=scunet_denoise --hidden-import=piexif --hidden-import=editor_presets --exclude-module torch --exclude-module torchvision --exclude-module open_clip --exclude-module scipy --exclude-module pandas --exclude-module matplotlib --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras service.py -n service --distpath ..\build-resources\python_backend --workpath ..\build-resources\tmp --specpath ..\build-resources\tmp
    if errorlevel 1 exit /b 1
    cd ..
    if exist "build-resources\python_backend\service\_internal\torch" ( echo ERROR: torch in CPU pack & exit /b 1 )
    python clean-after-pyinstaller.py --cpu
    if errorlevel 1 echo WARNING: cleanup step failed
)

rem Stage llama CPU runtime if missing, then rebuild bundled copy
if not exist "build-resources\llm\llama-server.exe" (
    python copy-llm-runtime.py --cpu
    if errorlevel 1 echo WARNING: llama runtime copy failed
)

rem ── [2/6] win-unpacked (reuse if exists) ───────────────────────────
if exist "..\package\win-unpacked\LensPilot.exe" (
    echo [2/6] win-unpacked exists, syncing python_backend only
) else (
    echo [2/6] building electron app ^(electron-builder dir target^)...
    call npm run build
    if errorlevel 1 exit /b 1
    for /f "delims=" %%r in ('dir /b /s "%LOCALAPPDATA%\electron-builder\Cache\winCodeSign\*\rcedit-x64.exe" 2^>nul') do set RCEDIT=%%r
    if defined RCEDIT (
        "!RCEDIT!" "..\package\win-unpacked\LensPilot.exe" --set-icon "%~dp0assets\icon.ico" --set-version-string "ProductName" "LensPilot" --set-version-string "FileDescription" "LensPilot" --set-version-string "CompanyName" "Dongwu259" --set-file-version "!VERMSIX!" --set-product-version "!VERMSIX!"
        echo rcedit branding complete
    ) else (
        echo WARNING: rcedit not found, skipping branding
    )
)
if exist "..\package\win-unpacked\resources\python_backend\service" rmdir /s /q "..\package\win-unpacked\resources\python_backend\service"
xcopy /e /i /y "build-resources\python_backend\service" "..\package\win-unpacked\resources\python_backend\service" >nul
if not exist "..\package\win-unpacked\resources\python_backend\service\service.exe" ( echo ERROR: service.exe missing & exit /b 1 )

rem ── [3/6] repack app.asar from current sources ─────────────────────
rem (keeps main.js/preload/src - e.g. the MSIX update guard - in sync
rem  even when win-unpacked is reused from a previous build)
echo [3/6] repacking app.asar...
set "ASTAGE=%TEMP%\lenspilot-asar-src"
if exist "%ASTAGE%" rmdir /s /q "%ASTAGE%"
mkdir "%ASTAGE%"
rem 逐个复制（一条 copy 带多个源文件时行为不可靠，曾导致 asar 缺入口 → 默认欢迎页）
copy /y main.js "%ASTAGE%\" >nul
copy /y preload.js "%ASTAGE%\" >nul
copy /y python_service.js "%ASTAGE%\" >nul
copy /y package.json "%ASTAGE%\" >nul
for %%f in (main.js preload.js python_service.js package.json) do (
    if not exist "%ASTAGE%\%%f" ( echo ERROR: staging missing %%f & exit /b 1 )
)
robocopy src "%ASTAGE%\src" /E /XF "*.bak" "*.bak*" "*-bak*" /NFL /NDL /NJH /NJS >nul
if errorlevel 8 ( echo ERROR: robocopy src failed & exit /b 1 )
call node_modules\.bin\asar.cmd pack "%ASTAGE%" "..\package\win-unpacked\resources\app.asar"
if errorlevel 1 ( echo ERROR: asar pack failed & exit /b 1 )
rem 打包后校验：入口文件必须真实进包（防 staging 残缺 → 默认欢迎页）
call node_modules\.bin\asar.cmd list "..\package\win-unpacked\resources\app.asar" | findstr /c:"\main.js" >nul
if errorlevel 1 ( echo ERROR: app.asar missing main.js & exit /b 1 )
call node_modules\.bin\asar.cmd list "..\package\win-unpacked\resources\app.asar" | findstr /c:"\package.json" >nul
if errorlevel 1 ( echo ERROR: app.asar missing package.json & exit /b 1 )
echo asar verified ^(main.js + package.json present^)
if exist "%ASTAGE%" rmdir /s /q "%ASTAGE%"
if exist "..\package\win-unpacked\resources\app.asar.unpacked" rmdir /s /q "..\package\win-unpacked\resources\app.asar.unpacked"

rem ── [4/6] MSIX assets (tiles/splash/logos) ─────────────────────────
echo [4/6] generating MSIX assets...
python packaging\msix\gen-msix-assets.py
if errorlevel 1 exit /b 1

rem ── [5/6] stage layout + manifest ──────────────────────────────────
echo [5/6] staging MSIX layout...
set "LAYOUT=%CD%\..\package\_msix-layout"
if exist "%LAYOUT%" rmdir /s /q "%LAYOUT%"
mkdir "%LAYOUT%"
robocopy "..\package\win-unpacked" "%LAYOUT%" /E /NFL /NDL /NJH /NJS >nul
if errorlevel 8 ( echo ERROR: robocopy layout failed & exit /b 1 )
if exist "%LAYOUT%\resources\tmp" rmdir /s /q "%LAYOUT%\resources\tmp"
python packaging\msix\fill-manifest.py "%LAYOUT%\AppxManifest.xml" "!VERMSIX!"
if errorlevel 1 exit /b 1
xcopy /e /i /y "packaging\msix\Assets" "%LAYOUT%\Assets" >nul
if not exist "%LAYOUT%\Assets\StoreLogo.scale-200.png" ( echo ERROR: MSIX assets missing & exit /b 1 )

rem ── [6/6] pack (.msix) ─────────────────────────────────────────────
set "SDKROOT=C:\Program Files (x86)\Windows Kits\10\bin"
set "SDKBIN="
rem 注意：SDKROOT 含 "(x86)"，在 for 的括号块里必须用延迟展开（!SDKROOT!），
rem 否则 %SDKROOT% 在解析期展开会把括号当成块边界，报"此时不应有 \Windows"。
for /f "delims=" %%v in ('dir /b /ad /o:n "!SDKROOT!" 2^>nul') do (
    if exist "!SDKROOT!\%%v\x64\makeappx.exe" set SDKBIN=!SDKROOT!\%%v\x64
)
if not defined SDKBIN ( echo ERROR: makeappx not found. Install Windows 10/11 SDK. & exit /b 1 )
echo using SDK: !SDKBIN!

set "OUTDIR=..\package"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
set "MSIX=%OUTDIR%\LensPilot_!VERMSIX!_x64.msix"
echo [6/6] packing %MSIX% ...
"!SDKBIN!\makeappx.exe" pack /d "%LAYOUT%" /p "%MSIX%" /o
if errorlevel 1 exit /b 1
if exist "%LAYOUT%" rmdir /s /q "%LAYOUT%"

rem ── signing ────────────────────────────────────────────────────────
if defined MSIX_TEST_SIGN (
    echo signing with local test cert ^(MSIX_TEST_SIGN^)...
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make-msix-test-cert.ps1
    if errorlevel 1 exit /b 1
    "!SDKBIN!\signtool.exe" sign /fd SHA256 /f "%TEMP%\lp-msix-test.pfx" /p lp-test "%MSIX%"
    if errorlevel 1 exit /b 1
    echo.
    echo TEST SIGN DONE. To install locally:
    echo   1. certutil -addstore TrustedPeople "%TEMP%\lp-msix-test.cer"   ^(admin^)
    echo   2. powershell Add-AppxPackage -Path "%MSIX%"
) else (
    echo UNSIGNED package ready for Partner Center upload:
    echo   %MSIX%
)

endlocal
