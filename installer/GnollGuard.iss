; Inno Setup script for Gnoll Guard
; Build with: ISCC.exe installer\GnollGuard.iss
; GitHub Actions builds this automatically alongside the portable .exe.

#define AppName     "Gnoll Guard"
; AppVersion is overridden by CI via /DAppVersion=x.y.z; this is the fallback.
#ifndef AppVersion
  #define AppVersion "1.4.1"
#endif
#define AppURL      "https://gnollguard.com"
#define AppExe      "GnollGuard.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppURL}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/download
; Install to %LocalAppData%\GnollGuard — no UAC prompt, no admin needed
DefaultDirName={localappdata}\GnollGuard
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename=GnollGuard-Setup
SetupIconFile=..\assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
; Never require admin rights
PrivilegesRequired=lowest
WizardStyle=modern
; Disclaimer / terms shown on a page the user must accept before installing
LicenseFile=DISCLAIMER.txt
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; Show "Gnoll Guard x.x.x" in Add/Remove Programs
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}";    Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
