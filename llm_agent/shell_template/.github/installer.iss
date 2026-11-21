; Builds the single-file Windows installer (Setup.exe) from the built app folder.
; Every value comes from environment variables set by the workflow (already validated by prepare.py).
#define AppName GetEnv("APP_NAME")
#define ExeName GetEnv("EXE_NAME")
#define SourceDir GetEnv("SOURCE_DIR")
#define IconFile GetEnv("ICON_FILE")

[Setup]
AppId={#GetEnv("PACKAGE_ID")}
AppName={#AppName}
AppVersion=1.0.0
; Installs for the current user only, so Windows does not ask for administrator rights, and asks no questions.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesInstallIn64BitMode=x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#ExeName}
OutputDir=out
OutputBaseFilename=Setup
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"

[Run]
Filename: "{app}\{#ExeName}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
