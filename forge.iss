#define AppName "Forge"
#ifndef AppVersion
#define AppVersion "1.0.0"
#endif
#define Model "qwen3:8b"

[Setup]
AppId={{6F1B2C84-5D0A-4E3B-9C57-2A9E4B7D1F03}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Forge
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=Forge-Setup-{#AppVersion}
SetupIconFile=forge.ico
UninstallDisplayIcon={app}\forge-app.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
DisableProgramGroupPage=yes
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "getollama"; Description: "Download and install Ollama (required to run models, about 1 GB)"; GroupDescription: "Required components:"; Check: not OllamaInstalled
Name: "addpath"; Description: "Add Forge to PATH so you can type 'forge' in any terminal"; GroupDescription: "Command line:"

[Files]
Source: "dist\forge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\forge-app.exe"; IconFilename: "{app}\forge-app.exe"; Comment: "Local coding agent"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\forge-app.exe"; IconFilename: "{app}\forge-app.exe"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""$ErrorActionPreference='Stop'; $f=Join-Path $env:TEMP 'OllamaSetup.exe'; Invoke-WebRequest https://ollama.com/download/OllamaSetup.exe -OutFile $f; Start-Process $f -ArgumentList '/VERYSILENT /NORESTART' -Wait"""; StatusMsg: "Downloading and installing Ollama (this can take a few minutes)..."; Flags: runhidden waituntilterminated; Tasks: getollama
Filename: "{cmd}"; Parameters: "/c ""{code:OllamaExe}"" pull {#Model} & pause"; Description: "Download the {#Model} model now (5 GB)"; Flags: postinstall skipifsilent; Check: OllamaInstalled
Filename: "{app}\forge-app.exe"; Description: "Start Forge"; Flags: postinstall nowait skipifsilent
Filename: "{app}\forge-app.exe"; Flags: nowait; Check: WizardSilent

[Code]
function OllamaExe(Param: String): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe');
  if not FileExists(Result) then Result := 'ollama';
end;

function OllamaInstalled: Boolean;
var
  Code: Integer;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'))
    or (Exec(ExpandConstant('{cmd}'), '/c ollama --version', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // No /T here: when Forge updates itself it starts this installer as its child, and /T would kill the
  // installer along with Forge. The app-mode browser window is closed separately by its title so a
  // stale window cannot keep the profile locked and make the relaunched Forge quit.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM forge-app.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /FI "IMAGENAME eq msedge.exe" /FI "WINDOWTITLE eq Forge"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(800);
  Result := '';
end;

procedure SetPath(Add: Boolean);
var
  Paths, Dir: String;
  P: Integer;
begin
  Dir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then Paths := '';
  P := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Paths) + ';');
  if Add and (P = 0) then
  begin
    if (Paths <> '') and (Paths[Length(Paths)] <> ';') then Paths := Paths + ';';
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths + Dir);
  end
  else if (not Add) and (P > 0) then
  begin
    Delete(Paths, P, Length(Dir) + 1);
    if (Length(Paths) > 0) and (Paths[1] = ';') then Delete(Paths, 1, 1);
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addpath') then SetPath(True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then SetPath(False);
end;


