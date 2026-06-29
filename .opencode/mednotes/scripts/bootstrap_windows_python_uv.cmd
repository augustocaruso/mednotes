@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%bootstrap_windows_python_uv.ps1"

if not exist "%BOOTSTRAP%" (
  echo Bootstrap script not found: %BOOTSTRAP% 1>&2
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
