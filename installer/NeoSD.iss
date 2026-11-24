; Instalador do NEO SD (Inno Setup 6) — compile com o build.bat,
; ou à mão:  ISCC.exe /DAppVersion=1.0.0 installer\NeoSD.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "NEO SD"
#define AppExe  "NeoSD.exe"

[Setup]
AppId={{6E0B7A52-3C1D-4F8E-9B7A-52D1C0A9E4F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=NeoSD
DefaultDirName={autopf}\NeoSD
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; instala só para o usuário atual por padrão (sem pedir administrador); a tela deixa escolher "todos os usuários"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=NeoSD-Setup-{#AppVersion}
SetupIconFile=..\assets\neosd.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"
Name: "consoleicon"; Description: "Criar atalho ""NEO SD (Modo Console)"" na Área de Trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked
Name: "retrobat"; Description: "Baixar e instalar o RetroBat (gratuito, para jogos de consoles antigos). O NeoSD baixa o instalador oficial ao abrir"; GroupDescription: "Jogos retro:"; Flags: unchecked; Check: not RetroBatInstalled
#if FileExists(SourcePath + "..\dist\NeoSD\third_party\AnyFSE\AnyFSE.Installer.exe")
Name: "anyfse"; Description: "Instalar o AnyFSE (usar o NeoSD no lugar do app Xbox no modo tela cheia; pede administrador)"; GroupDescription: "Modo Xbox:"; Flags: unchecked
#endif

[Files]
Source: "..\dist\NeoSD\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; pedido para o NeoSD baixar o RetroBat na primeira vez que abrir (ele apaga o valor depois de ler)
Root: HKCU; Subkey: "Software\NeoSD"; ValueType: dword; ValueName: "InstallRetroBat"; ValueData: 1; Tasks: retrobat; Flags: uninsdeletekey; Check: not IsUpdate

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} (Modo Console)"; Filename: "{app}\{#AppExe}"; Parameters: "--console"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{autodesktop}\{#AppName} (Modo Console)"; Filename: "{app}\{#AppExe}"; Parameters: "--console"; Tasks: consoleicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Instalando o Microsoft Edge WebView2..."; Check: NeedsWebView2; Flags: waituntilterminated skipifdoesntexist
#if FileExists(SourcePath + "..\dist\NeoSD\third_party\AnyFSE\AnyFSE.Installer.exe")
Filename: "{app}\third_party\AnyFSE\AnyFSE.Installer.exe"; Parameters: "/s"; Verb: runas; StatusMsg: "Instalando o AnyFSE..."; Flags: shellexec waituntilterminated skipifdoesntexist; Tasks: anyfse; Check: not IsUpdate
#endif
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppName} agora"; Flags: nowait postinstall skipifsilent
; atualização automática (o NeoSD roda este instalador com /VERYSILENT /update=1): reabre o NeoSD no fim
Filename: "{app}\{#AppExe}"; Parameters: "{param:relaunch|}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
; tira o NeoSD da inicialização do Windows, se tiver sido ligado nas configurações
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v NeoSD /f"; Flags: runhidden; RunOnceId: "DelRun"

[Code]
var
  DownloadPage: TDownloadWizardPage;

{ WebView2 é o "motor" que desenha a interface. Windows 11 já traz; alguns Windows 10 não. }
function NeedsWebView2: Boolean;
var
  V: String;
begin
  Result := not (
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) or
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V));
  if not Result then
    Result := (V = '') or (V = '0.0.0.0');
end;

{ Atualização automática feita pelo próprio NeoSD (parâmetro /update=1): não repete AnyFSE nem RetroBat. }
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

{ RetroBat já instalado (lista de programas do Windows ou pastas comuns): não oferece baixar de novo. }
function RetroBatInstalled: Boolean;
var
  Names: TArrayOfString;
  I: Integer;
  V: String;
begin
  Result := FileExists('C:\RetroBat\emulationstation\emulatorLauncher.exe') or
            FileExists(ExpandConstant('{%USERPROFILE}\Desktop\RetroBat\emulationstation\emulatorLauncher.exe')) or
            FileExists('D:\RetroBat\emulationstation\emulatorLauncher.exe');
  if not Result and RegGetSubkeyNames(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + Names[I], 'DisplayName', V) and
         (Pos('retrobat', Lowercase(V)) > 0) then begin
        Result := True;
        Exit;
      end;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), 'Baixando o Microsoft Edge WebView2...', nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and NeedsWebView2 then begin
    DownloadPage.Clear;
    DownloadPage.Add('https://go.microsoft.com/fwlink/p/?LinkId=2124703', 'MicrosoftEdgeWebview2Setup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        MsgBox('Não consegui baixar o WebView2 (sem internet?). O NEO SD será instalado mesmo assim; ' +
               'se ele não abrir, instale o "Microsoft Edge WebView2 Runtime" pelo site da Microsoft.', mbInformation, MB_OK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then begin
    Data := ExpandConstant('{localappdata}\NeoSD');
    if DirExists(Data) and (MsgBox('Apagar também sua biblioteca (catálogo, capas e configurações) em ' + Data + '?' + #13#10#13#10 +
        'Escolha "Não" para manter tudo caso instale de novo.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(Data, True, True, True);
  end;
end;
