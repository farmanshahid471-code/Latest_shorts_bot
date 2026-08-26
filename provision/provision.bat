@echo off
rem Semi-automated Google Cloud + YouTube OAuth provisioner (Windows wrapper).
rem Double-clicking it opens an interactive menu.
rem From a terminal:  provision.bat doctor | init <name> | guide <name> | verify <name> | scaffold <name> | connect <name> | status
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=py -3"
    goto :run
)
where python >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=python"
    goto :run
)
echo.
echo [ERROR] Python was not found on this PC.
echo         Install Python 3.9+ from https://www.python.org/downloads/
echo         IMPORTANT: tick "Add python.exe to PATH" during install,
echo         then reopen this file.
echo.
pause
exit /b 1

:run
%PYCMD% "%~dp0provision.py" %*
if errorlevel 1 (
    echo.
    echo (the command above failed - window stays open so you can read it)
    pause
)
endlocal
