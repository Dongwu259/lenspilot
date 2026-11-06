; LensPilot — Windows GPU Installer (v0.7.0)
; Compile with Inno Setup: iscc installer-win-gpu.iss
; Prerequisite: electron-builder produced ../package/win-unpacked/ (target: dir)

#define AppName "LensPilot"
#define AppVersion "0.9.0"
#define AppPublisher "Dongwu259"
#define AppURL "https://studio.dwphoto.top"
; electron-builder dir target outputs to ../package/win-unpacked/
#define SourceDir "{#SourcePath}\..\package\win-unpacked"
#define OutputDir "{#SourcePath}\..\package"
#define SetupName "LensPilot-Win-GPU-Setup-0.9.0"

[Setup]
AppId={{D7A3C8E1-F5B4-4A2D-9E1C-8F7D6B3A5E4F}
AppName={#AppName} (GPU)
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\LensPilot (GPU)
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

[Languages]
Name: "chinese"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "额外图标:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}\THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE-GPL-3.0.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\LensPilot (GPU)"; Filename: "{app}\LensPilot.exe"
Name: "{autodesktop}\LensPilot (GPU)"; Filename: "{app}\LensPilot.exe"; Tasks: desktopicon
Name: "{autoprograms}\卸载 LensPilot (GPU)"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\LensPilot.exe"; Description: "启动 LensPilot"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean user data left in AppData (models, cache, config, scores.db, etc.)
; Electron uses %APPDATA% (Roaming) by default, not %LOCALAPPDATA%
Type: filesandordirs; Name: "{%USERPROFILE}\AppData\Roaming\LensPilot"
Type: filesandordirs; Name: "{localappdata}\LensPilot"
; Clean portable-mode data directory if it exists next to the app
Type: filesandordirs; Name: "{app}\data"
