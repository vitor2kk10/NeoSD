@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title NEO SD - gerando o instalador
set /p VER=<VERSION
if not exist build mkdir build
set "LOG=%CD%\build\build.log"
echo NEO SD %VER% - build iniciado em %DATE% %TIME%> "%LOG%"
echo.
echo  ===============================================
echo   NEO SD %VER% - gerando .exe e instalador
echo  ===============================================
echo.

rem ---------- 1. Python ----------
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython
echo [1/5] Python encontrado: %PY%
%PY% -c "import sys; print('Python', sys.version)" >> "%LOG%" 2>&1

rem ---------- 2. ambiente isolado so para o build ----------
if not exist ".venv-build\Scripts\python.exe" (
  echo [2/5] Criando ambiente de build...
  %PY% -m venv .venv-build >> "%LOG%" 2>&1 || goto :fail
) else (
  echo [2/5] Ambiente de build ja existe
)
set "VPY=.venv-build\Scripts\python.exe"
rem o cache do pip em %LOCALAPPDATA%\pip pode estar travado (antivirus ou criado como administrador): nao usa
set "PIP_NO_CACHE_DIR=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
"%VPY%" -m pip install --no-cache-dir --upgrade pip >> "%LOG%" 2>&1
echo       Instalando dependencias (pode demorar alguns minutos na primeira vez)...
"%VPY%" -m pip install --no-cache-dir -r requirements.txt "pyinstaller>=6.6" >> "%LOG%" 2>&1 || goto :pipfail

rem ---------- 3. .exe ----------
echo [3/5] Gerando NeoSD.exe com PyInstaller (uns 2 a 5 minutos)...
if exist "dist\NeoSD" rmdir /s /q "dist\NeoSD"
rem credenciais de desenvolvedor (ScreenScraper) de chaves.local.json, fora do git
"%VPY%" tools\build_info.py >> "%LOG%" 2>&1
"%VPY%" tools\embutir_chaves.py > build\chaves.txt 2>&1
type build\chaves.txt
type build\chaves.txt >> "%LOG%"
"%VPY%" -m PyInstaller NeoSD.spec --noconfirm --clean >> "%LOG%" 2>&1 || goto :fail
if not exist "dist\NeoSD\NeoSD.exe" goto :fail

rem ---------- extras: ferramentas, avisos de licenca e AnyFSE (opcional) ----------
xcopy /e /i /y /q tools "dist\NeoSD\tools" >nul
copy /y THIRD_PARTY_NOTICES.txt "dist\NeoSD\" >nul
rem sons do modo console (pasta SFX); NEOSD_NO_SFX=1 gera sem eles (ex.: para distribuir sem sons de terceiros)
if /i "%NEOSD_NO_SFX%"=="1" (
  echo       Sons: pulados ^(NEOSD_NO_SFX=1^)
) else if exist "SFX" (
  xcopy /e /i /y /q SFX "dist\NeoSD\SFX" >nul
  echo       Sons do modo console incluidos ^(pasta SFX^)
)
if /i "%NEOSD_NO_ANYFSE%"=="1" (
  echo       AnyFSE: pulado ^(NEOSD_NO_ANYFSE=1^)
) else (
  echo       Baixando o instalador oficial do AnyFSE para ir junto...
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\baixar-anyfse.ps1 -Dest "dist\NeoSD\third_party\AnyFSE" >> "%LOG%" 2>&1
  if errorlevel 1 echo       Seguindo sem o AnyFSE: o NeoSD funciona igual, so nao oferece instalar o AnyFSE.
)

rem ---------- 4. instalador (Inno Setup) ----------
rem O Inno Setup e o programa que transforma installer\NeoSD.iss (a "receita") em dist\NeoSD-Setup-x.exe
if not exist "installer\NeoSD.iss" (
  echo FALTA installer\NeoSD.iss>> "%LOG%"
  echo.
  echo  *** Falta o arquivo installer\NeoSD.iss ^(a receita do instalador^). Nao apague esse arquivo. ***
  echo  Baixe o projeto de novo ou peca o arquivo ao Claude.
  goto :fail
)
call :findiscc
if not defined ISCC (
  echo [4/5] Inno Setup nao encontrado. Baixando do site oficial ^(so na primeira vez^)...
  echo --- instalando o Inno Setup 6.7.4 ^(download oficial^) --->> "%LOG%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe' -OutFile 'build\innosetup-setup.exe'" >> "%LOG%" 2>&1
  if exist "build\innosetup-setup.exe" (
    echo       Instalando o Inno Setup so para o seu usuario ^(sem administrador^)...
    start "" /wait "build\innosetup-setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CURRENTUSER
    if errorlevel 1 (echo o instalador do Inno Setup falhou>> "%LOG%") else (echo Inno Setup instalado>> "%LOG%")
    del "build\innosetup-setup.exe" >nul 2>&1
  ) else (
    echo nao consegui baixar o Inno Setup>> "%LOG%"
  )
  call :findiscc
)
if not defined ISCC (
  echo       Tentando pelo winget...
  echo --- winget install JRSoftware.InnoSetup --->> "%LOG%"
  winget install -e --id JRSoftware.InnoSetup --scope user --accept-package-agreements --accept-source-agreements --silent >> "%LOG%" 2>&1
  call :findiscc
)
if defined ISCC echo --- ISCC: "%ISCC%" --->> "%LOG%"
if defined ISCC (
  echo [4/5] Gerando o instalador...
  "%ISCC%" /Qp /DAppVersion=%VER% installer\NeoSD.iss >> "%LOG%" 2>&1 || goto :fail
) else (
  echo SEM INNO SETUP: instalador nao gerado>> "%LOG%"
  echo.
  echo  *** AVISO: nao consegui instalar o Inno Setup, entao o instalador NAO foi gerado. ***
  echo  Instale o Inno Setup 6 pelo site https://jrsoftware.org/isdl.php e rode o build.bat de novo.
  echo  O NeoSD.exe e a versao portatil continuam sendo gerados normalmente.
  echo.
)

rem ---------- 5. versao portatil (zip) ----------
echo [5/5] Gerando a versao portatil (.zip)...
echo Este arquivo faz o NEO SD guardar os dados nesta pasta (modo portatil).> "dist\NeoSD\portable.txt"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\NeoSD' -DestinationPath 'dist\NeoSD-Portable-%VER%.zip' -Force" || goto :fail
del "dist\NeoSD\portable.txt" >nul 2>&1

echo BUILD OK>> "%LOG%"
echo.
echo  Pronto! Arquivos em: %CD%\dist
if exist "dist\NeoSD-Setup-%VER%.exe" echo    - NeoSD-Setup-%VER%.exe     (instalador: leve este para a outra maquina)
echo    - NeoSD-Portable-%VER%.zip  (sem instalar: descompacte e abra NeoSD.exe)
echo    - NeoSD\NeoSD.exe         (para testar aqui mesmo)
echo.
start "" "%CD%\dist"
pause
exit /b 0

:findiscc
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 7\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe") do if exist %%P set "ISCC=%%~P"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
exit /b 0

:pipfail
echo BUILD FALHOU: dependencias>> "%LOG%"
powershell -NoProfile -Command "Get-Content -Tail 25 '%LOG%'"
echo.
echo  *** Falha ao instalar as dependencias. ***
echo  Se o erro for "Permission denied": feche o build, pause o antivirus por alguns minutos
echo  (ou adicione esta pasta como excecao), apague a pasta .venv-build e rode o build.bat de novo.
echo  Evite rodar como administrador.
pause
exit /b 1

:nopython
echo  Python 3.10 ou mais novo nao foi encontrado.
echo  Instale pelo site python.org (marque "Add python.exe to PATH") ou rode:
echo     winget install -e --id Python.Python.3.12
echo  e execute o build.bat de novo.
pause
exit /b 1

:fail
echo BUILD FALHOU>> "%LOG%"
echo.
powershell -NoProfile -Command "Get-Content -Tail 25 '%LOG%'"
echo.
echo  *** O build falhou. O registro completo esta em build\build.log ***
pause
exit /b 1
