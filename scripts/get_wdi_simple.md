# 获取 wdi-simple.exe(libwdi 的非 GUI 驱动安装 CLI)

> 用途:LensPilot 联机拍摄的「一键安装驱动」用它把相机的 Windows 默认 PTP 驱动
> 换成 WinUSB,使 libgphoto2 能访问相机。wdi-simple = Zadig 的驱动安装引擎被作者
> (Pete Batard)抽成的独立库 **libwdi**(LGPL-3.0)自带的 CLI 示例--不用 fork Zadig。

## 现状(2026-07-20 已解决 ✅)

**wdi-simple.exe 已就位 `electron_app/tools/wdi-simple.exe`**(libwdi 1.5.1.788,
akeo.ie 官方构建,3.86MB 含内嵌驱动文件),并已接入打包链:
electron-builder extraResources → `resources/tools/`;打包(frozen)模式下经
`service.exe --install-camera-driver` argv 子命令启动(磁盘上无 .py 也能跑)。
以下为 2026-07-19 的原始调查存档。

**libwdi 的 GitHub release 不提供 `wdi-simple.exe`**--只发 `zadig-2.9.exe`(GUI)。
原因:`zadig-2.9.exe` 是作者用 **WDK + 内嵌驱动文件 + 自签证书** 编好的成品,开箱即用;
而 `wdi-simple` 只是 `examples/wdi-simple.c` 一个示例,**源码 release 不含驱动文件**,
要靠 `embedder` 工具在构建时从 WDK 读取 `winusbcoinstaller2.dll`/`WdfCoInstaller` 等
生成 `embedded.h`(`embedder.h` 硬性要求 `WDK_DIR`/`LIBUSB0_DIR`/`LIBUSBK_DIR`/`USER_DIR`
至少一个有定义且路径有效)。

**本机构建环境无法构建能用的 wdi-simple**,三个硬阻塞:
1. autotools 全缺(`autoconf`/`automake`/`libtool`/`make` 都 MISSING)+ 无出站网络
   装不了 -> 走不了 `./bootstrap.sh && ./configure && make`。
2. 没装 WDK -> embedder 拿不到驱动文件,`embedded.h` 内嵌数据为空。
3. 没有内嵌驱动的 wdi-simple 能编出来但 `wdi_install_driver` 装不了 WinUSB(缺
   coinstaller/inf 数据)。

## ✅ 已核实 CLI flag(来自 libwdi 1.5.1 `examples/wdi-simple.c`)

```
-v, --vid <id>      VID(0x 前缀十六进制)
-p, --pid <id>      PID
-t, --type <n>      驱动类型: 0=WinUSB, 1=libusb-win32, 2=libusbK, 3=usbser, 4=custom
-n, --name <名称>    设备名(可选)
-w, --wcid           WCID 驱动(≠ WinUSB!)
-x, --extract        仅解压不安装
-s, --silent         静默
-c, --cert <名称>    安装内嵌受信任证书
-h, --help           用法
```
⚠️ **没有 `-W`**。装 WinUSB 用 `--type 0`(wdi-simple 默认已是 WDI_WINUSB,可省,写上更明确)。
`tools/wdi_simple.args` 默认模板已是 `--vid {VID} --pid {PID} --type 0`。

## 真正拿到能用的 wdi-simple.exe 的路(都需要 WDK,本机暂不具备)

### 方式 A:从 libwdi 的 CI 构建产物下载(需网络)
libwdi 的 GitHub Actions 有 `mingw.yml` 工作流(README 徽章可见),每次推送都会用
MinGW + WDK 构建。到 https://github.com/pbatard/libwdi/actions 找最新的 mingw 工作流
run,下载其 artifacts(可能含 `wdi-simple.exe`)。这是最省事的"别人编好的"途径。

### 方式 B:自己从源码编译(需 WDK + autotools + 网络)
1. 装 WDK(https://go.microsoft.com/fwlink/p/?LinkID=253170 ,约几个 GB)。
2. MSYS2 装 autotools:`pacman -S autoconf automake libtool make`(需网络)。
3. 下载 libwdi 源码(解压到本仓库同级任意工作目录),然后:
   ```bash
   cd libwdi-1.5.1
   export PATH=/mingw64/bin:/usr/bin:$PATH
   ./bootstrap.sh
   ./configure --with-wdkdir="C:/Program Files (x86)/Windows Kits/10"
   make
   # 产物:examples/wdi-simple.exe(静态链 libwdi)
   ```
4. 放到 `electron_app/tools/wdi-simple.exe`。

## 替代方案(本机当前推荐)

既然本机编不了,0.8.6 有两条务实路:

### 替代 1:暂缓静默安装,用手动 Zadig(已上线,推荐 0.8.6)
- 检测层 + 指南页(`studio.dwphoto.top/tethered-setup`)+ 「一键安装驱动」按钮(二进制
  缺失时降级为打开指南)都已就位。Windows 用户按指南用 `zadig-2.9.exe` 手动装一次。
- 「一键安装驱动」按钮代码已接好,**等以后 wdi-simple.exe 就位(放进 `tools/`)自动生效**,
  无需改代码。

### 替代 2:打包 `zadig-2.9.exe`,按钮直接启动它
- 把 `zadig-2.9.exe` 作为 extraResources 打进安装包;「一键安装驱动」按钮改成启动它
  (而非 wdi-simple)。用户仍需在 Zadig GUI 里手选设备+WinUSB+Replace,但**省了下载步骤**。
- 注意:**Zadig 是 GPL-3.0**(libwdi 是 LGPL-3.0)。作为独立程序随安装包分发 OK(GPL 允许
  未修改程序的重分发),但不能链入/宣称是我们的代码。打包时在 license 里声明。

## 硬约束(无论哪条路,现代 Windows 底线)

- 装驱动必须 **UAC 提权**(一次)。
- libwdi 生成的 INF/目录是它自签的测试证书,Windows 会弹**一次**"是否安装此设备软件"
  信任提示。除非 WHQL 签名(费钱,0.8.6 不做),否则**无完全静默**。未来可用我们的
  Authenticode 证书签目录降到"始终信任"一次。

## 相关文件

- `electron_app/python_backend/install_camera_driver.py` - 提权安装器(检测 VID/PID + 调 wdi-simple,args 从 `tools/wdi_simple.args` 读)
- `electron_app/python_backend/shooting_coach.py::handle_tether_install_driver` - IPC 入口(提权启动安装器)
- `electron_app/tools/wdi_simple.args` - wdi-simple 参数模板(`--vid {VID} --pid {PID} --type 0`,已核实)
- `studio-server/public/tethered-setup.html` - 用户侧手动安装指南(Zadig)
- `libwdi-1.5.1/` - libwdi 源码(自行下载,待具备 WDK+autotools 时编译)
