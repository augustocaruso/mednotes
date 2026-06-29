@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP_SCRIPT=%SCRIPT_DIR%bootstrap_windows_python_uv.ps1"

if not exist "%BOOTSTRAP_SCRIPT%" (
  echo Bootstrap script not found: %BOOTSTRAP_SCRIPT% 1>&2
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%" %*
exit /b %ERRORLEVEL%
