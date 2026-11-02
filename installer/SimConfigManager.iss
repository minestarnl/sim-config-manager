; Inno Setup script — builds SimConfigManager-Setup-<version>.exe (CI passes /DMyAppVersion=x.y.z)
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8F2E6C4B-3A1D-4E7B-9C5A-2D1F0B7E6A93}
AppName=Sim Config Manager
AppVersion={#MyAppVersion}
AppVerName=Sim Config Manager {#MyAppVersion}
AppPublisher=Sim Config Manager
DefaultDirName={autopf}\SimConfigManager
DefaultGroupName=Sim Config Manager
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=SimConfigManager-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\SimConfigManager.exe
UninstallDisplayName=Sim Config Manager

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\SimConfigManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\SimConfigManager-MCP.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Sim Config Manager"; Filename: "{app}\SimConfigManager.exe"
Name: "{autodesktop}\Sim Config Manager"; Filename: "{app}\SimConfigManager.exe"; Tasks: desktopicon

[Run]
; normal install: offer to start the app; silent install (in-app update): always start it again, in the tray
Filename: "{app}\SimConfigManager.exe"; Description: "Start Sim Config Manager"; Flags: nowait postinstall skipifsilent
Filename: "{app}\SimConfigManager.exe"; Parameters: "--minimized"; Flags: nowait; Check: WizardSilent

[UninstallRun]
; remove the start-at-logon task, the firewall rule and the Claude Desktop entry (data in ProgramData is kept)
Filename: "{app}\SimConfigManager.exe"; Parameters: "--uninstall-cleanup"; Flags: waituntilterminated runhidden; RunOnceId: "SCMCleanup"
