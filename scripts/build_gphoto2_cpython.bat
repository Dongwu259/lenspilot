@echo off
rem 在 Windows 上为 CPython 编译 python-gphoto2 的参考脚本。
rem 注意：MSVC/SDK/MSYS2/Python 路径均为某台构建机的示例值，使用前请按
rem scripts/gphoto2_windows_setup.md 的说明改成本机实际路径。
set "REPO_ROOT=%~dp0.."
set "MSVC=D:\VisualStudio\VC\Tools\MSVC\14.44.35207"
set "SDK=C:\Program Files (x86)\Windows Kits\10"
set "SDKVER=10.0.26100.0"
set "MWD=C:\msys64\mingw64"
set "PATH=%MSVC%\bin\Hostx64\x64;%MWD%\bin;%PATH%"
set "INCLUDE=%MSVC%\include;%SDK%\Include\%SDKVER%\ucrt;%SDK%\Include\%SDKVER%\um;%SDK%\Include\%SDKVER%\shared;C:\gphoto2_headers"
set "LIB=%MSVC%\lib\x64;%SDK%\Lib\%SDKVER%\ucrt\x64;%SDK%\Lib\%SDKVER%\um\x64;%MWD%\lib"
set "PKG_CONFIG_PATH=%MWD%\lib\pkgconfig"
set "CPY=C:\Users\16341\AppData\Local\Programs\Python\Python313"
echo === pkg-config cflags/libs ===
pkg-config --cflags --libs libgphoto2
echo === pip install gphoto2 (from patched sdist, CPython 3.13 + MSVC) ===
"%CPY%\python.exe" -m pip install "%REPO_ROOT%\gp_build\gphoto2-2.6.4"
echo PIP_EXIT=%ERRORLEVEL%
echo === verify import gphoto2 + torch in CPython 3.13 ===
"%CPY%\python.exe" -c "import gphoto2; print('gphoto2', gphoto2.__version__)"
"%CPY%\python.exe" -c "import torch; print('torch', torch.__version__)"
