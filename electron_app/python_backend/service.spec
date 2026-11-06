# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('app_config.json', '.'), ('luts', 'luts')]
binaries = []
hiddenimports = ['copy_card', 'photo_scorer', 'cloud_uploader', 'ftp_server', 'folder_watcher', 'agent_module', 'agent_watcher', 'mcp_client_manager', 'skill_loader', 'drive_detector', 'drive_detector_linux', 'drive_detector_mac', 'drive_detector_win', 'utils', 'model_loader', 'camera_backend', 'reference_library', 'shooting_coach', 'photo_tagger', 'editor_presets', 'torch', 'torchvision']
tmp_ret = collect_all('mcp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('torch')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('torchvision')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# gphoto2 is OPTIONAL (Mac: brew libgphoto2 + pip gphoto2; Windows: not installed).
# Guarded so Windows builds don't fail; Mac builds include it for GPhoto2Backend.
try:
    tmp_ret = collect_all('gphoto2')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception:
    pass


a = Analysis(
    ['service.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='service',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='service',
)
