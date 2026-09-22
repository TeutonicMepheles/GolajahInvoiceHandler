@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. See the message above and docs\windows-deployment.md.
  pause
  exit /b 1
)
echo.
echo Installation completed. Open the desktop shortcut to use the application.
pause
