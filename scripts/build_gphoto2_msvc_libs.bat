@echo off
set "MSVC=D:\VisualStudio\VC\Tools\MSVC\14.44.35207"
set "SDK=C:\Program Files (x86)\Windows Kits\10"
set "SDKVER=10.0.26100.0"
set "PATH=%MSVC%\bin\Hostx64\x64;C:\msys64\mingw64\bin;%PATH%"
set "INCLUDE=%MSVC%\include;%SDK%\Include\%SDKVER%\ucrt;%SDK%\Include\%SDKVER%\um;%SDK%\Include\%SDKVER%\shared"
set "LIB=%MSVC%\lib\x64;%SDK%\Lib\%SDKVER%\ucrt\x64;%SDK%\Lib\%SDKVER%\um\x64"
echo === where cl ===
where cl
echo === where gendef ===
where gendef
echo === cd mingw bin + gendef ===
cd /d C:\msys64\mingw64\bin
gendef libgphoto2-6.dll
if errorlevel 1 (echo GENDEF1 FAILED) else (echo GENDEF1 OK)
gendef libgphoto2_port-12.dll
if errorlevel 1 (echo GENDEF2 FAILED) else (echo GENDEF2 OK)
echo === lib gphoto2 ===
lib /def:libgphoto2-6.def /machine:x64 /out:gphoto2.lib
echo === lib gphoto2_port ===
lib /def:libgphoto2_port-12.def /machine:x64 /out:gphoto2_port.lib
copy /Y gphoto2.lib C:\msys64\mingw64\lib\
copy /Y gphoto2_port.lib C:\msys64\mingw64\lib\
echo === result ===
dir C:\msys64\mingw64\lib\gphoto2*.lib
