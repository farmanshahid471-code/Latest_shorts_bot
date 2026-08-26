@echo off
setlocal EnableDelayedExpansion
title Shorts Repost Bot - Setup (Windows)
cd /d "%~dp0"

set "ROOT=%~dp0"

echo ==================================================
echo   Shorts Repost Bot - Automatic Setup - Windows
echo ==================================================
echo.
echo   This installs everything this bot needs, by itself:
echo     - FFmpeg, downloaded automatically if missing
echo     - Python packages, yt-dlp, YouTube API
echo     - Your settings file, .env
echo.
echo   First time only - takes about 2-10 minutes.
echo.

echo [1/4] Checking Python...

set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
where python >nul 2>&1
if not errorlevel 1 set "PY=python"

if defined PY goto :python_ok
echo.
echo   Python was NOT found on this computer.
echo   Please install it from:  https://www.python.org/downloads/
echo   IMPORTANT: during installation, tick the box that says
echo   "Add Python to PATH".
echo.
echo   Then close this window and double-click setup.bat again.
echo.
pause
exit /b 1

:python_ok
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo       Found: !PYVER!
echo.

echo [2/4] Checking FFmpeg...

set "FFBIN="

where ffmpeg >nul 2>&1
if errorlevel 1 goto :no_system_ffmpeg

set "FFBIN=ffmpeg"
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"drawtext" >nul
if errorlevel 1 goto :system_ffmpeg_weak
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"subtitles" >nul
if errorlevel 1 goto :system_ffmpeg_weak
goto :ffmpeg_ready

:system_ffmpeg_weak
echo       The ffmpeg on PATH is missing drawtext/subtitles filters.
echo       Using a full portable build instead, so captions and text can render.
if exist "%ROOT%ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_local_verify
goto :ffmpeg_portable

:no_system_ffmpeg
if exist "%ROOT%ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_local_verify

echo       FFmpeg not found. Trying the Windows Store installer, winget...
where winget >nul 2>&1
if errorlevel 1 goto :ffmpeg_portable

winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :ffmpeg_portable

where ffmpeg >nul 2>&1
if errorlevel 1 goto :ffmpeg_portable
set "FFBIN=ffmpeg"
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"drawtext" >nul
if errorlevel 1 goto :ffmpeg_portable
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"subtitles" >nul
if errorlevel 1 goto :ffmpeg_portable
echo       FFmpeg installed via winget.
goto :ffmpeg_ready

:ffmpeg_local_verify
set "FFBIN=%ROOT%ffmpeg\bin\ffmpeg.exe"
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"drawtext" >nul
if errorlevel 1 goto :ffmpeg_local_rebuild
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"subtitles" >nul
if errorlevel 1 goto :ffmpeg_local_rebuild
goto :ffmpeg_ready

:ffmpeg_local_rebuild
echo       The bundled FFmpeg is incomplete. Re-downloading a full portable build...
rmdir /S /Q "%ROOT%ffmpeg" >nul 2>&1
goto :ffmpeg_portable

:ffmpeg_portable
echo.
echo       No full FFmpeg found. Downloading a portable copy, about 100 MB...
echo.

set "FFZIP=%ROOT%ffmpeg.zip"
if exist "%FFZIP%" del /Q "%FFZIP%"

rem BtbN mirror: the Gyan.dev direct zip URLs now return 404, so we use
rem GitHub's stable "latest release" asset (filename never changes).
set "FFURL=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"

where curl >nul 2>&1
if not errorlevel 1 goto :use_curl

echo       Downloading with PowerShell...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%FFURL%' -OutFile '%FFZIP%'"
if errorlevel 1 goto :ffmpeg_mirror2
if not exist "%FFZIP%" goto :ffmpeg_mirror2
goto :ffmpeg_extract

:use_curl
echo       Downloading with curl...
curl -L -sS -o "%FFZIP%" "%FFURL%"
if errorlevel 1 goto :ffmpeg_mirror2
if not exist "%FFZIP%" goto :ffmpeg_mirror2
goto :ffmpeg_extract

:ffmpeg_mirror2
echo.
echo       First mirror failed. Trying the GyanD GitHub release (full build)...
del /Q "%FFZIP%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference = 'SilentlyContinue'; $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/GyanD/codexffmpeg/releases/latest' -Headers @{'User-Agent'='yt-shorts-bot'}; $a = $r.assets | Where-Object { $_.name -match 'full_build\.zip$' } | Select-Object -First 1; if (-not $a) { throw 'No zip asset found' }; Invoke-WebRequest -UseBasicParsing -Uri $a.browser_download_url -OutFile '%FFZIP%'"
if errorlevel 1 goto :ffmpeg_manual
if not exist "%FFZIP%" goto :ffmpeg_manual

:ffmpeg_extract
echo       Download finished. Extracting...
cd /d "%ROOT%"

tar -xf "%FFZIP%"
if errorlevel 1 goto :use_expand
goto :after_extract

:use_expand
echo       Using PowerShell to extract...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%FFZIP%' -DestinationPath '%ROOT%' -Force"
if errorlevel 1 goto :ffmpeg_manual

:after_extract
for /d %%d in (ffmpeg-*) do xcopy /E /Y /I "%%d\bin" "ffmpeg\bin" >nul
for /d %%d in (ffmpeg-*) do rmdir /S /Q "%%d" >nul 2>&1
cd /d "%ROOT%"
del /Q "%FFZIP%" >nul 2>&1

if not exist "%ROOT%ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_manual
if not exist "%ROOT%ffmpeg\bin\ffprobe.exe" goto :ffmpeg_manual
set "FFBIN=%ROOT%ffmpeg\bin\ffmpeg.exe"
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"drawtext" >nul
if errorlevel 1 goto :ffmpeg_manual
"!FFBIN!" -hide_banner -filters 2>nul | findstr /C:"subtitles" >nul
if errorlevel 1 goto :ffmpeg_manual

echo       FFmpeg extracted to ffmpeg\bin
echo       Verified: drawtext + subtitles filters present.
echo       The bot finds it automatically, no PATH changes needed.
goto :ffmpeg_ready

:ffmpeg_manual
echo.
echo   Automatic FFmpeg download failed too.
echo   Please do it manually:
echo     1. Go to:  https://github.com/BtbN/FFmpeg-Builds/releases/latest
echo     2. Download  ffmpeg-master-latest-win64-gpl.zip
echo     3. Extract it, then COPY the bin folder from inside it
echo        into a folder named:  ffmpeg
echo        so that the file exists at:
echo        ffmpeg\bin\ffmpeg.exe
echo     4. Run setup.bat again.
echo.
echo   Alternative: close this window and run this in Terminal:
echo       winget install --id Gyan.FFmpeg -e
echo   then open a NEW window and run setup.bat again.
echo.
pause
exit /b 1

:ffmpeg_ready
for /f "delims=" %%v in ('"!FFBIN!" -version 2^>^&1 ^| findstr /b "ffmpeg version"') do echo       Found: %%v
if errorlevel 1 echo       FFmpeg is ready.
echo.

echo [3/4] Setting up Python environment, first time downloads about 200 MB...

if exist ".venv\Scripts\python.exe" goto :venv_ok
%PY% -m venv .venv
if errorlevel 1 goto :venv_failed

:venv_ok
echo       Virtual environment ready.

echo       Installing packages, this can take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
echo       All packages installed.
echo.

echo [4/4] Creating settings file...

if exist ".env" goto :env_exists
copy /Y .env.example .env >nul
echo       Created .env
goto :env_done

:env_exists
echo       .env already exists - leaving it unchanged.

:env_done
echo.

echo ==================================================
echo   DONE! Setup complete.
echo ==================================================
echo.
echo   Next steps:
echo     1. OPTIONAL: edit .env with Notepad - set TARGET_CHANNELS
echo        to the channels you want to grab Shorts from.
echo     2. Double-click  run_ui.bat  to open the control panel.
echo.
pause
exit /b 0

:venv_failed
echo.
echo   Failed to create the Python environment.
echo   Make sure Python is installed and try again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo   Package installation failed. Check your internet connection
echo   and try again.
echo.
pause
exit /b 1
