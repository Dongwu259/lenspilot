; LensPilot — Windows CPU Lightweight Installer (v0.7.0)
; Compile with Inno Setup: iscc installer-win-cpu.iss
; Prerequisite: electron-builder produced ../package/win-unpacked/ (target: dir)

#define AppName "LensPilot"
#define AppVersion "0.9.0"
#define AppPublisher "Dongwu259"
#define AppURL "https://studio.dwphoto.top"
#define SourceDir "{#SourcePath}\..\package\win-unpacked"
#define OutputDir "{#SourcePath}\..\package"
#define SetupName "LensPilot-Win-CPU-Setup-0.9.0"

[Setup]
AppId={{E8B3D9F2-A6C5-4B3E-A0D2-9F8E7C4B6A5G}
AppName={#AppName} (CPU)
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\LensPilot (CPU)
DefaultGroupName=LensPilot
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename={#SetupName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile={#SourcePath}\license.txt
SetupIconFile={#SourcePath}\assets\icon.ico
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\LensPilot.exe

; 签名（将 certificate.pfx 放在 electron_app/ 目录）
; 编译时自动检测：有证书则签名，无则跳过
#ifdef SIGNTOOL
#define SignedName "mysigntool"
#else
#define SignedName "dummy"
#endif

[Languages]
Name: "chinese"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "额外图标:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}\THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE-GPL-3.0.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\LensPilot (CPU)"; Filename: "{app}\LensPilot.exe"
Name: "{autodesktop}\LensPilot (CPU)"; Filename: "{app}\LensPilot.exe"; Tasks: desktopicon
Name: "{autoprograms}\卸载 LensPilot (CPU)"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\LensPilot.exe"; Description: "启动 LensPilot"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean user data left in AppData (models, cache, config, scores.db, etc.)
; Electron uses %APPDATA% (Roaming) by default, not %LOCALAPPDATA%
Type: filesandordirs; Name: "{%USERPROFILE}\AppData\Roaming\LensPilot"
Type: filesandordirs; Name: "{localappdata}\LensPilot"
; Clean portable-mode data directory if it exists next to the app
Type: filesandordirs; Name: "{app}\data"
