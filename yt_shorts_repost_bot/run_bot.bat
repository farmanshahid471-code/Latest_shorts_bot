@echo off
title Shorts Repost Bot
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :has_venv
echo First run detected - running the automatic setup first...
call setup.bat
if errorlevel 1 goto :fail

:has_venv
set "PYTHONPATH=%CD%\.."
".venv\Scripts\python.exe" -m yt_shorts_repost_bot.main %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" goto :ok
echo   The bot exited with an error code: %RC%
echo   Look at the messages above to see what went wrong.
goto :end

:ok
echo   Done.

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
