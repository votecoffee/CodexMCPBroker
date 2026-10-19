@echo off
setlocal DisableDelayedExpansion
set "_broker_launcher_shell=powershell.exe"
where pwsh.exe >nul 2>nul
if not errorlevel 1 set "_broker_launcher_shell=pwsh.exe"

"%_broker_launcher_shell%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\InstallAndRun.ps1" %*
exit /b %errorlevel%
