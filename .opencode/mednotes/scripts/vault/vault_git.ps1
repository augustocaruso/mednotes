<# Wrapper PowerShell generico para a politica Git cross-platform do vault. #>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
if (-not (Test-Path (Join-Path $ProjectRoot "pyproject.toml"))) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path
}
$Core = Join-Path $ScriptDir "vault_git.py"

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    Write-Error "uv e obrigatorio para executar scripts Python do Workbench. Rode /mednotes:setup ou scripts\bootstrap_windows_python_uv.ps1."
    exit 127
}

& $Uv.Source run --project $ProjectRoot python $Core @args
exit $LASTEXITCODE
