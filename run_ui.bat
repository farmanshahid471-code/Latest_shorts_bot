@echo off
title YouTube Shorts Bot - Control Panel
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :has_venv
echo First run detected - running the automatic setup first...
call setup.bat
if errorlevel 1 goto :fail

:has_venv
REM Stop an old/stale panel before starting a new one.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
echo ==================================================
echo   Starting the Shorts Bot Control Panel
echo ==================================================
echo.
echo   It will open in your browser at:  http://127.0.0.1:5000
echo   Keep this window open while you use it.
echo   The bot keeps running 24/7 even if you close the browser tab.
echo.

REM Open the panel in Chrome if available, else Edge, else the default browser
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & (start chrome http://127.0.0.1:5000 2>nul || start msedge http://127.0.0.1:5000 2>nul || start http://127.0.0.1:5000)"
".venv\Scripts\python.exe" -m yt_shorts_bot.main --mode webui
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" goto :ok
echo   The bot exited with an error code: %RC%
echo   Look at the messages above to see what went wrong.
goto :end

:ok
echo   Bot stopped. Closing this window is fine.

:end
pause
exit /b %RC%

:fail
echo.
echo   Setup did not finish, so the bot could not start.
echo   Fix the problem shown above and try again.
echo.
pause
exit /b 1
