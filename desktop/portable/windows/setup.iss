#include "version.iss"

[Setup]
AppId={{630E71AE-1043-4DEE-B63A-3F337F1DBA4D}
AppName=Jaro
AppVersion={#AppVersion}
AppPublisher=Jard
AppPublisherURL=https://www.jard.ai/
AppSupportURL=https://github.com/AdamGera/Jaro/issues
AppUpdatesURL=https://github.com/AdamGera/Jaro/releases
PrivilegesRequired=lowest
SetupArchitecture=x64
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.22000
DefaultDirName={localappdata}\Programs\Jaro
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallFilesDir={app}\uninstall
UninstallDisplayIcon={app}\Jaro.ico
CloseApplications=no
RestartApplications=no
SetupIconFile=Jaro.ico
SetupMutex=Jaro.PerUserSetup.630E71AE
#ifdef SignedEngineDir
SignedUninstaller=yes
SignedUninstallerDir="{#SignedEngineDir}"
#endif
Compression=lzma2/normal
SolidCompression=yes
OutputDir=output
OutputBaseFilename=Jaro-Setup-{#AppVersion}-windows-x64
VersionInfoVersion={#AppVersion}

[Files]
Source: "launchers\Jaro.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "launchers\jardctl.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "Jaro.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "payload\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Jaro"; Filename: "{app}\bin\Jaro.exe"; IconFilename: "{app}\Jaro.ico"

[UninstallDelete]
Type: files; Name: "{app}\current-version"
Type: files; Name: "{app}\current-version.partial"
Type: files; Name: "{app}\rejected-update.sha256"

[Code]
const
  InvalidHandle = -1;
  ReadWriteAccess = $C0000000;
  OpenAlways = 4;
  NormalAttributes = $80;
  ReplaceAndFlush = $9;
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  CloseJaro = 'Open Jaro from Start and close its window. Wait for any jardctl jobs to finish, then retry.';

var
  InstallLock: THandle;

function WinCreateFile(Name: String; Access, ShareMode: Cardinal; Security: THandle;
  Creation, Attributes: Cardinal; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function WinCloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function MoveFileEx(Existing, NewName: String; Flags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';

function LockInstallation(): Boolean;
var
  Data: String;
  Handle: THandle;
begin
  Result := True;
  if InstallLock <> 0 then
    Exit;
  Result := False;
  Data := ExpandConstant('{localappdata}\Jaro');
  if not ForceDirectories(Data) then
    Exit;
  Handle := WinCreateFile(Data + '\.installer-lock', ReadWriteAccess, 0, 0,
    OpenAlways, NormalAttributes, 0);
  if Handle = THandle(InvalidHandle) then
    Exit;
  InstallLock := Handle;
  Result := True;
end;

procedure UnlockInstallation();
begin
  if InstallLock <> 0 then begin
    WinCloseHandle(InstallLock);
    InstallLock := 0;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Expected: String;
  Active: AnsiString;
begin
  Result := '';
  if not LockInstallation() then begin
    Result := CloseJaro;
    Exit;
  end;
  Expected := ExpandConstant('{param:JAROUPDATE|}');
  if (Expected <> '') and
      (not LoadStringFromFile(ExpandConstant('{app}\current-version'), Active) or (Trim(String(Active)) <> Expected)) then begin
    UnlockInstallation();
    Result := 'The installed Jaro version changed. Reopen Jaro and check for updates again.';
  end;
end;

procedure CurStepChanged(Step: TSetupStep);
var
  Active, Partial, Payload: String;
  ExitCode: Integer;
begin
  if Step <> ssPostInstall then
    Exit;
  Payload := ExpandConstant('{app}\versions\{#AppVersion}');
  if not Exec(Payload + '\python\python.exe', '-B -X utf8 "' + Payload +
      '\desktop\portable\open_workspace.py" --help', Payload, SW_HIDE, ewWaitUntilTerminated, ExitCode) or
      (ExitCode <> 0) then
    RaiseException('Setup could not start the new Jaro runtime. The active version was preserved.');
  Active := ExpandConstant('{app}\current-version');
  Partial := Active + '.partial';
  if not SaveStringToFile(Partial, '{#AppVersion}' + #10, False) then
    RaiseException('Setup could not stage the Jaro version.');
  if not MoveFileEx(Partial, Active, ReplaceAndFlush) then begin
    DeleteFile(Partial);
    RaiseException('Setup could not activate the new Jaro version.');
  end;
  if not Exec(ExpandConstant('{app}\bin\Jaro.exe'), '--repair-shortcuts', ExpandConstant('{app}\bin'),
      SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    Log('Setup could not migrate Jaro shortcuts that use an old version path.');
end;

procedure DeinitializeSetup();
begin
  UnlockInstallation();
end;

function InitializeUninstall(): Boolean;
begin
  Result := LockInstallation();
  if not Result then
    SuppressibleMsgBox(CloseJaro, mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(Step: TUninstallStep);
var
  StartupCommand: String;
begin
  if (Step = usPostUninstall) and RegQueryStringValue(HKCU, RunKey, 'Jaro', StartupCommand) then
    if CompareText(StartupCommand, '"' + ExpandConstant('{app}\bin\Jaro.exe') + '" --background') = 0 then
      RegDeleteValue(HKCU, RunKey, 'Jaro');
end;

procedure DeinitializeUninstall();
begin
  UnlockInstallation();
end;
