; Ploptimizer - Inno Setup 6 installer script.
;
; Compiled in CI (see .github/workflows/ci.yml) right after the PyInstaller
; one-dir build, producing a single
;   dist/Ploptimizer-Setup-<version>-windows-x64.exe
; Local build (from the repo root, after `uv run pyinstaller ...`):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\ploptimizer.iss
;
; The installer is UNSIGNED: SmartScreen may still show an "unknown publisher"
; reputation warning on first run. Only an Authenticode signature removes it.

; ---------------------------------------------------------------------------
; Version resolution: PLOPTIMIZER_VERSION (set by CI from the release tag, or
; from pyproject.toml -- see the "Resolve build version" step in
; .github/workflows/ci.yml) wins; otherwise a clearly-fake 0.0.0 for
; unmanaged local builds. A leading "v" is stripped in CI, so the installer
; version always matches the release tag.
;
; Deliberately expression-only: ISPP has no statement blocks (#function with
; begin/while/:= does not exist -- only #sub/#endsub wrapping expressions),
; so the pyproject.toml fallback lives in CI rather than here.
; ---------------------------------------------------------------------------

#define EnvVersion GetEnv("PLOPTIMIZER_VERSION")
#if defined(EnvVersion) && EnvVersion != ""
  #if Copy(EnvVersion, 1, 1) == "v"
    #define AppVersion Copy(EnvVersion, 2, 64)
  #else
    #define AppVersion EnvVersion
  #endif
#else
  #define AppVersion "0.0.0"
#endif

[Setup]
; NEVER regenerate this GUID: it is what makes upgrade-in-place work and
; keeps a single Add/Remove Programs entry across versions.
AppId={{29009301-DA66-4809-AB2E-9387250C3CEF}
AppName=Ploptimizer
AppVersion={#AppVersion}
AppVerName=Ploptimizer {#AppVersion}
AppPublisher=twilsonco
AppPublisherURL=https://github.com/twilsonco/Ploptimizer
AppSupportURL=https://github.com/twilsonco/Ploptimizer/issues
AppUpdatesURL=https://github.com/twilsonco/Ploptimizer/releases

VersionInfoVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany=twilsonco
VersionInfoProductName=Ploptimizer
VersionInfoDescription=Ploptimizer Setup
VersionInfoCopyright=Copyright (C) 2026 twilsonco

; The app is entirely per-user: config lives at %LOCALAPPDATA%\PLT-Optimizer,
; and the "Run at Startup" toggle writes a shortcut into the *user* Startup
; folder. A per-user install is therefore the consistent default; the dialog
; override lets power users opt into a machine-wide install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Ploptimizer
DefaultGroupName=Ploptimizer
DisableProgramGroupPage=yes
DisableWelcomePage=no
UninstallDisplayName=Ploptimizer
UninstallDisplayIcon={app}\Ploptimizer.exe
ArchitecturesInstallIn64BitMode=x64compatible
; Deployment target is Windows 7 SP1 (Inno 6.x already defaults to this;
; pinned explicitly so a future Inno upgrade cannot silently raise the floor).
MinVersion=6.1sp1
OutputDir=..\dist
OutputBaseFilename=Ploptimizer-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; A running tray app locks its own EXE, so let Setup detect and close it
; during upgrade-in-place (default behaviour, made explicit here).
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Whole PyInstaller one-dir tree (EXE + _internal/). SourceDir is relative to
; this script, i.e. <repo>/dist/Ploptimizer/.
Source: "..\dist\Ploptimizer\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Ploptimizer"; Filename: "{app}\Ploptimizer.exe"
Name: "{group}\Uninstall Ploptimizer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Ploptimizer"; Filename: "{app}\Ploptimizer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Ploptimizer.exe"; Description: "{cm:LaunchProgram,Ploptimizer}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller leaves strays here and there across upgrades; never touch the
; user's config at %LOCALAPPDATA%\PLT-Optimizer\ or the log directories
; (both are outside {app} and are intentionally preserved by the uninstaller).
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
; The tray app's "Run at Startup" toggle (plt_optimizer/utils/startup.py)
; creates this shortcut in the *current user's* Startup folder. The app owns
; it at runtime, but on uninstall the target EXE is gone, so remove the
; dangling shortcut rather than leaving it to error at every login. Silently
; ignored when the user never enabled the toggle.
Type: files; Name: "{userstartup}\PLT-Optimizer.lnk"
