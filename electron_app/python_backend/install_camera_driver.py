"""Elevated WinUSB driver installer for LensPilot tethered shooting (Windows).

Launched UAC-elevated by shooting_coach.handle_tether_install_driver when the
gphoto2 backend reports needs_driver -- i.e. libgphoto2 sees 0 cameras because
Windows' default PTP class driver has claimed the camera. This script:

  1. Locates the bundled wdi-simple.exe (libwdi's non-GUI CLI = Zadig's engine).
  2. Detects the connected camera's USB VID/PID (Windows-native Get-CimInstance).
  3. Invokes wdi-simple with args from tools/wdi_simple.args to install WinUSB.

Then the user re-clicks "开始联机" and gphoto2 can see the camera.

WHY elevated: driver installation requires admin (UAC). Unavoidable on Windows.
The INF/catalog libwdi generates is signed by its embedded self-signed test
cert, so Windows shows ONE "Would you like to install this device software?"
trust prompt -- this is the floor for non-WHQL driver install; there is no
fully-silent path without WHQL (expensive, out of scope for 0.8.6).

✅ PROCUREMENT SOLVED (2026-07-20): wdi-simple.exe (libwdi 1.5.1, akeo.ie 官方
构建, 含内嵌驱动文件) 已就位 electron_app/tools/wdi-simple.exe, 打包时经
electron-builder extraResources → resources/tools/ 随包分发。打包(frozen)模式下
本脚本没有磁盘 .py, 由 service.exe --install-camera-driver argv 子命令调起 main()。
获取/重建方式仍见 scripts/get_wdi_simple.md。
"""
import os
import re
import subprocess
import sys

# Known camera USB vendor IDs (4 hex, upper). Used to auto-pick the camera out
# of all connected USB devices. Extend as more brands are verified.
CAMERA_VIDS = {
    "054C": "Sony",
    "04A9": "Canon",
    "04B0": "Nikon",
    "04CB": "Fuji",
    "04DA": "Panasonic",
    "07B4": "Olympus",
    "0A17": "Pentax/Ricoh",
    "04B9": "Leica",
}

GUIDE_URL = "https://studio.dwphoto.top/tethered-setup"

# Default wdi-simple arg template. {VID}/{PID} are substituted by this script.
# ✅ Flags verified from libwdi 1.5.1 source (examples/wdi-simple.c):
#    --vid/--pid (0x hex), --type 0=WinUSB 1=libusb-win32 2=libusbK 3=usbser 4=custom.
#    There is NO -W flag (that was a wrong guess; -w is --wcid, not WinUSB).
#    wdi-simple defaults to WDI_WINUSB already, but --type 0 makes it explicit.
DEFAULT_ARGS_TEMPLATE = "--vid {VID} --pid {PID} --type 0"


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def find_wdi_simple():
    """Locate the bundled wdi-simple.exe. Env override + candidate tools/ dirs
    (incl. packaged resources/tools/ via the exe anchor)."""
    env = os.environ.get("LENSPILOT_WDI_SIMPLE")
    here = _here()
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))  # 打包: resources/python_backend/service/
    candidates = ([env] if env else []) + [
        os.path.join(here, "tools", "wdi-simple.exe"),                  # python_backend/tools/
        os.path.join(here, "..", "tools", "wdi-simple.exe"),            # electron_app/tools/
        os.path.join(here, "..", "..", "tools", "wdi-simple.exe"),      # repo/tools/
        os.path.join(exe_dir, "..", "..", "tools", "wdi-simple.exe"),   # packaged: resources/tools/
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def find_args_file():
    here = _here()
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for c in [os.path.join(here, "tools", "wdi_simple.args"),
              os.path.join(here, "..", "tools", "wdi_simple.args"),
              os.path.join(here, "..", "..", "tools", "wdi_simple.args"),
              os.path.join(exe_dir, "..", "..", "tools", "wdi_simple.args")]:
        if os.path.isfile(c):
            return c
    return None


def load_args_template():
    """First non-comment, non-blank line of tools/wdi_simple.args, else default."""
    af = find_args_file()
    if not af:
        return DEFAULT_ARGS_TEMPLATE, None
    try:
        with open(af, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    return s, af
    except Exception:
        pass
    return DEFAULT_ARGS_TEMPLATE, af


def detect_cameras():
    """Return [(vid, pid, brand)] for connected USB devices matching known
    camera vendor IDs. Uses Windows-native CIM (no dependency on wdi-simple)."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity -Filter \"PNPDeviceID LIKE 'USB\\\\VID_%'\" | "
             "Select-Object -ExpandProperty PNPDeviceID"],
            text=True, stderr=subprocess.STDOUT, timeout=20)
    except Exception as e:
        print(f"[!] 检测 USB 设备失败: {e}")
        return []
    cams = []
    seen = set()
    for line in out.splitlines():
        m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", line)
        if not m:
            continue
        vid, pid = m.group(1).upper(), m.group(2).upper()
        brand = CAMERA_VIDS.get(vid)
        if brand and (vid, pid) not in seen:
            seen.add((vid, pid))
            cams.append((vid, pid, brand))
    return cams


def open_guide():
    try:
        subprocess.Popen(["cmd", "/c", "start", "", GUIDE_URL])
    except Exception:
        pass


def main():
    print("=" * 64)
    print("LensPilot 相机 USB 驱动安装器  (libwdi / wdi-simple)")
    print("=" * 64)
    print("此脚本以管理员权限运行，用 libwdi 的 wdi-simple 把相机的 Windows 默认")
    print("PTP 驱动替换为 WinUSB，使 libgphoto2 能访问相机。")
    print()

    wdi = find_wdi_simple()
    if not wdi:
        print("[!] 未找到 wdi-simple.exe —— 自动安装组件尚未就绪。")
        print("    获取方式见: scripts/get_wdi_simple.md")
        print("    放到 electron_app/tools/wdi-simple.exe 后，回到 LensPilot 重新点")
        print("    「一键安装驱动」。")
        print()
        print("    现在打开手动安装指南 (Zadig) ...")
        open_guide()
        return 0

    print(f"[+] wdi-simple: {wdi}")
    args_tmpl, af = load_args_template()
    src = f"(from {af})" if af else "(默认; 建议 procured 后跑 wdi-simple --help 核实)"
    print(f"[+] 参数模板: {args_tmpl}  {src}")

    cams = detect_cameras()
    if not cams:
        print()
        print("[!] 未检测到已知品牌的相机 USB 设备。")
        print("    请确认: 相机已用【数据线】连接、开机、USB 模式设为 PC Remote。")
        print("    若相机品牌不在内置 VID 表，可手动在 tools/wdi_simple.args 指定")
        print("    --vid/--pid 后重试。")
        return 1

    print(f"[+] 检测到 {len(cams)} 个相机设备: " +
          ", ".join(f"{b} VID_{v}/PID_{p}" for v, p, b in cams))

    any_fail = False
    for vid, pid, brand in cams:
        args = args_tmpl.replace("{VID}", f"0x{vid}").replace("{PID}", f"0x{pid}")
        print()
        print(f"[*] 为 {brand} (VID_{vid}/PID_{pid}) 安装 WinUSB ...")
        print(f"    > wdi-simple {args}")
        try:
            r = subprocess.run([wdi] + args.split(),
                               capture_output=True, text=True, timeout=120)
            if r.stdout:
                print(r.stdout)
            if r.stderr:
                print(r.stderr)
            print(f"    退出码: {r.returncode}")
            if r.returncode != 0:
                any_fail = True
        except Exception as e:
            print(f"    [!] 执行失败: {e}")
            any_fail = True

    print()
    if any_fail:
        print("[部分失败] 若 wdi-simple 报参数错误，请运行 `wdi-simple --help` 核实，")
        print("          然后修改 tools/wdi_simple.args 第一行的参数模板。")
    else:
        print("[完成] 若上述显示成功，请关闭此窗口，回到 LensPilot 重新点「开始联机」。")
    return 0 if not any_fail else 1


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except Exception as e:
        print(f"[!] 脚本异常: {e}")
        code = 1
    try:
        input("\n按回车关闭此窗口...")
    except Exception:
        pass
    sys.exit(code)
