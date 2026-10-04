@echo off
rem Double-click this file to start Minecraft Name Finder.
rem The first start installs everything it needs into the .venv folder next to this file.
setlocal
cd /d "%~dp0"
title Minecraft Name Finder

if exist ".venv\Scripts\minecraft-finder.exe" goto run

echo.
echo   First start: setting up Minecraft Name Finder. This takes a minute...
echo.

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if defined PY (
    %PY% -m venv .venv || goto failed
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . || goto failed
    goto run
)
where uv >nul 2>&1 || goto nopython
uv venv --quiet --python ">=3.12" .venv || goto failed
uv pip install --quiet --python ".venv\Scripts\python.exe" -e . || goto failed

:run
".venv\Scripts\minecraft-finder.exe" %*
if errorlevel 1 pause
exit /b

:nopython
echo   Python 3.12 or newer is needed, but it was not found.
echo   1. Download it from https://www.python.org/downloads/
echo   2. In the installer, tick "Add python.exe to PATH".
echo   3. Double-click Start.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo   Setup failed - see the messages above.
echo   Delete the .venv folder and try again.
echo.
pause
exit /b 1
