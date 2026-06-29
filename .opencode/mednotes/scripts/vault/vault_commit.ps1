<# Commit post-mutation do vault. Wrapper PowerShell para o core cross-platform. #>

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

& $Uv.Source run --project $ProjectRoot python $Core commit @args
exit $LASTEXITCODE
