# Windows gPhoto2 + Sony A7M4 安装全流程(MSYS2 编译 + Zadig)

> 验证日期 2026-07-19。Windows 上 gPhoto2 对 Sony A7M4(ILCE-7M4)**全通**(detect/settings/liveview/capture),和 Mac 一致。
>
> **为什么这么装**:Windows 上 python-gphoto2 **pip 无 wheel、conda-forge 无 win 包**,标准装法都失败。唯一可行路 = **MSYS2 提供工具链,从源码编译 python-gphoto2**(链 libgphoto2)+ **Zadig 换 WinUSB 驱动**。
>
> 编译出来的 gphoto2 是 **mingw python** 的扩展(MSYS2 mingw-w64-x86_64-python,MinGW/GCC 编译),不是官方 CPython(MSVC)。这点很重要,见末尾"torch 共存"。

---

## 0. 前提

- Windows + A7M4 + 数据线。
- 相机:模式拨盘 P/A/S/M;菜单 Network/USB -> **USB 连接 = PC Remote**;**镜头盖打开**(拍摄要对焦,盖着会 `[-1] Unspecified error`)。
- 电池充足或外接电源。

---

## 1. 装 MSYS2

```bash
winget install MSYS2.MSYS2 --accept-source-agreements --accept-package-agreements
```
装到 `C:\msys64\`。安装器是 GUI,winget 可能卡在向导/自动启动的 shell--若卡住,等文件拷贝完(`C:\msys64\usr\bin\pacman.exe` 出现)即可,GUI 可关掉。

验证:
```bash
/c/msys64/usr/bin/bash.exe -lc "pacman --version | head -1"
# 应见 Pacman v6.1.0
```

---

## 2. 装工具链 + libgphoto2 + mingw python

```bash
/c/msys64/usr/bin/bash.exe -lc "pacman -S --needed --noconfirm \
  mingw-w64-x86_64-libgphoto2 \
  mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf \
  mingw-w64-x86_64-python mingw-w64-x86_64-python-pip"
```
含:libgphoto2(C 库+头文件)、gcc(编译器)、pkgconf(pip 找 libgphoto2 用)、mingw python 3.14 + pip。

验证:
```bash
/c/msys64/usr/bin/bash.exe -lc "export PATH=/mingw64/bin:\$PATH; \
  python --version; \
  pkg-config --modversion libgphoto2; \
  ls /mingw64/bin/libgphoto2*.dll"
# 应见: Python 3.14.x / 2.5.34 / libgphoto2-6.dll + libgphoto2_port-12.dll
```

---

## 3. 编译安装 python-gphoto2

mingw python 是 MSYS2 托管的(PEP 668),要 `--break-system-packages`。gphoto2 不是 MSYS2 包(无冲突),安全。

```bash
/c/msys64/usr/bin/bash.exe -lc "export PATH=/mingw64/bin:\$PATH; \
  pip install --break-system-packages gphoto2"
```
这一步从 PyPI 下 gphoto2 源码,**用 mingw gcc 编译**成 `.pyd`(链 libgphoto2)。成功会建出 `gphoto2-2.6.4-cp314-cp314-mingw_x86_64_msvcrt_gnu.whl`。

验证:
```bash
/c/msys64/usr/bin/bash.exe -lc "export PATH=/mingw64/bin:\$PATH; \
  python -c 'import gphoto2 as gp; print(gp.__version__)'"
# 应见 2.6.4
```

> 此时 `gp_camera_autodetect` 返回 0 cameras 是正常的--还没 Zadig,Windows 默认 PTP 驱动挡着 libgphoto2。

---

## 4. Zadig 换 USB 驱动(关键!这步 GUI 手动)

1. 下 Zadig:https://zadig.akeo.ie/
2. A7M4 USB 连上、开机、USB 连接 = PC Remote。
3. Zadig -> **Options -> List All Devices**(勾上)。
4. 下拉找 `Sony ILCE-7M4` / `Sony Alpha-A7 IV`(或 `USB PTP 设备`)。
5. 目标驱动选 **WinUSB** -> **Replace Driver**。
6. 拔插一次 USB。

> 把 Windows 默认 PTP 类驱动换成 WinUSB,libgphoto2 才能经 libusb 访问相机。每台相机每个 USB 口可能要做一次。

---

## 5. 测试(四件:detect/settings/liveview/capture)

测试脚本 `scripts/gphoto2_win_test.py`(autodetect + get_config + capture_preview + capture)。**镜头盖打开**,然后:

```bash
/c/msys64/usr/bin/bash.exe -lc "export PATH=/mingw64/bin:\$PATH; \
  python scripts/gphoto2_win_test.py"
```

A7M4 预期全通:
```
1. autodetect:  Sony Alpha-A7 IV (PC Control)
2. settings:    shutterspeed 1/250, f-number f/2.8, iso 2000, exposurecompensation 0
3. liveview:    ~55KB JPEG -> liveview WORKS (存 liveview_win_test.jpg)
4. capture:     capt_DSCxxxxx.ARW -> capture WORKS
```

CLI 交叉验证(同款命令 Mac 也用):
```bash
/c/msys64/usr/bin/bash.exe -lc "export PATH=/mingw64/bin:\$PATH; \
  gphoto2 --auto-detect; gphoto2 --capture-image; gphoto2 --capture-image-and-download"
```
(若没装 gphoto2 CLI:`pacman -S --needed mingw-w64-x86_64-gphoto2`)

---

## 6. 常见坑

- `import gphoto2` 失败 -> 没用 mingw python / PATH 没带 `/mingw64/bin`(libgphoto2 DLL 找不到)。所有命令前都要 `export PATH=/mingw64/bin:$PATH`。
- `--break-system-packages` 漏了 -> PEP 668 拒绝安装。
- autodetect 找不到相机 -> Zadig 没换对(确认选 WinUSB 不是 libusb-win32)/ 相机不是 PC Remote / 线是充电线。
- `capture` 报 `[-1] Unspecified error` -> **镜头盖关着无法对焦**(最常见),开盖 + power-cycle 相机再测。
- Windows 弹"照片"认领相机 -> 关掉,或 `taskkill /f /im PTPCamera.exe`(Git Bash 里用 `taskkill //F //IM`)。

---

## 7. 协议(LGPL,合规)

- libgphoto2 / python-gphoto2 = **LGPL-2.1+**。LensPilot **动态链接**它们的 DLL(别静态打进 exe),自身代码保持 Apache-2.0;改了 libgphoto2 就把补丁按 LGPL 发布。标准 LGPL 做法,合规。
- gphoto2 CLI = GPL-2.0+,不碰(我们用 libgphoto2)。
- 打包时 libgphoto2-6.dll 等要**单独作为 DLL 提供**(用户可替换),不能静态链入。

---

## 8. 未解决(完整 Windows app 才需要)

1. **torch 共存**:gphoto2 是 mingw python 扩展(MinGW 编译),torch 是 CPython(MSVC)wheel,**两个 python ABI 不兼容**。完整后端(service.py 要 import torch 做评分)不能直接跑在 mingw python。解法二选一:
   - (a) **Sidecar**:mingw python 跑小进程管 gphoto2(liveview/遥控/参数),主后端(CPython+torch)IPC 调它。
   - (b) **给 CPython(MSVC)编 gphoto2**:用 MSVC + mingw 的 libgphoto2 DLL,把 gphoto2 编进官方 CPython(有 torch)。若成功一个 python 全有,架构不变;但 MSVC+mingw DLL+pkg-config 编译不一定顺。
   - USB 控制(liveview/遥控/参数)本身不要 torch,mingw python 单独能跑;评分/教练才要 torch。
2. **PyInstaller 打包**:用 mingw python 构建 + 带 libgphoto2 DLL(LGPL 动态)。
3. **Zadig 自动化**:终端用户不能手 Zadig,打包 `wdi-simple`(libwdi CLI)在安装时/首次连相机时自动装 WinUSB(按 VID/PID,Sony=0x054C)。

---

## 附:mingw python 关键路径

- python: `C:\msys64\mingw64\bin\python.exe`
- gphoto2 CLI(若装): `C:\msys64\mingw64\bin\gphoto2.exe`
- libgphoto2 DLL: `C:\msys64\mingw64\bin\libgphoto2-6.dll`、`libgphoto2_port-12.dll`
- 跑任何 gphoto2 命令前:`export PATH=/mingw64/bin:$PATH`(让 DLL 能被找到)
