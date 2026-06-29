#Requires -Version 5.1
<#
.SYNOPSIS
Reset the Medical Notes Workbench Python environment on Windows and rebuild it with uv.

.DESCRIPTION
By default this script resets only the Medical Notes Workbench environment.
Pass -RemoveGlobalPython -YesReallyRemoveGlobalPython to uninstall global
Python Software Foundation installs and the Python Launcher, clean Python PATH
entries, then rebuild everything with uv-managed Python.
Pass -FullReset for the one-command workflow: ensure standalone uv, remove
global Python/launcher, clean WindowsApps aliases from PATH, sync, and check.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $ExtensionRoot,
    [string] $StateDir = (Join-Path $HOME ".gemini\medical-notes-workbench"),
    [string] $PythonVersion = "3.12",
    [switch] $FullReset,
    [switch] $RemoveGlobalPython,
    [switch] $YesReallyRemoveGlobalPython,
    [switch] $RemoveWindowsAppsFromPath,
    [switch] $Dev,
    [switch] $Pdf,
    [switch] $SkipChecks
)

$ErrorActionPreference = "Stop"

function Resolve-WorkbenchExtensionRoot {
    if ($ExtensionRoot) {
        return (Resolve-Path $ExtensionRoot).Path
    }
    if ($PSScriptRoot) {
        $candidate = Resolve-Path (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue
        if ($candidate -and (Test-Path (Join-Path $candidate.Path "pyproject.toml"))) {
            return $candidate.Path
        }
    }
    $default = Join-Path $HOME ".gemini\extensions\medical-notes-workbench"
    if (Test-Path (Join-Path $default "pyproject.toml")) {
        return $default
    }
    throw "Nao encontrei a extensao medical-notes-workbench. Passe -ExtensionRoot com a raiz instalada."
}

$ResolvedExtensionRoot = Resolve-WorkbenchExtensionRoot

if ($FullReset) {
    $RemoveGlobalPython = $true
    $YesReallyRemoveGlobalPython = $true
}

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string] $FilePath,
        [string[]] $Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Get-PythonUninstallEntries {
    $roots = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )

    foreach ($root in $roots) {
        Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
            Where-Object {
                $name = [string] $_.DisplayName
                $publisher = [string] $_.Publisher
                $name -and (
                    $publisher -match "Python Software Foundation" -or
                    $name -match "^Python( \d| Launcher|$)"
                )
            } |
            ForEach-Object {
                [pscustomobject] @{
                    DisplayName = [string] $_.DisplayName
                    Publisher = [string] $_.Publisher
                    QuietUninstallString = [string] $_.QuietUninstallString
                    UninstallString = [string] $_.UninstallString
                    RegistryPath = [string] $_.PSPath
                }
            }
    }
}

function Get-PythonInstallRoots {
    $roots = @(
        "HKCU:\Software\Python\PythonCore\*\InstallPath",
        "HKLM:\Software\Python\PythonCore\*\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\*\InstallPath"
    )

    foreach ($root in $roots) {
        Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.ExecutablePath) {
                    Split-Path -Parent ([string] $_.ExecutablePath)
                }
                elseif ($_.PSChildName) {
                    [string] $_.PSChildName
                }
                elseif ($_.InstallPath) {
                    [string] $_.InstallPath
                }
            }
    }
}

function Get-CommandPaths {
    param([string[]] $Names)

    foreach ($name in $Names) {
        $output = & cmd.exe /d /c "where.exe $name 2>nul"
        if ($LASTEXITCODE -eq 0) {
            $output | Where-Object { $_ }
        }
        elseif ($LASTEXITCODE -eq 1) {
            continue
        }
        else {
            Write-Warning "where.exe retornou codigo $LASTEXITCODE para $name"
        }
    }
}

function ConvertTo-QuietUninstallCommand {
    param([object] $Entry)

    $cmdLine = if ($Entry.QuietUninstallString) {
        $Entry.QuietUninstallString
    }
    else {
        $Entry.UninstallString
    }

    if (-not $cmdLine) {
        return $null
    }

    if ($cmdLine -match "\{[0-9A-Fa-f-]{36}\}") {
        return "msiexec.exe /x $($Matches[0]) /qn /norestart"
    }

    if (-not $Entry.QuietUninstallString -and $cmdLine -notmatch "(?i)(/quiet|/qn|/passive)") {
        $cmdLine = "$cmdLine /quiet"
    }

    return $cmdLine
}

function Invoke-GlobalPythonRemoval {
    param([string[]] $KnownRoots)

    $entries = @(Get-PythonUninstallEntries | Sort-Object RegistryPath -Unique)
    Write-Step "Inventario de Python global"

    $commandPaths = @(Get-CommandPaths @("python", "python3", "py") | Sort-Object -Unique)
    if ($commandPaths.Count -gt 0) {
        Write-Host "Comandos encontrados no PATH:"
        $commandPaths | ForEach-Object { Write-Host "  $_" }
    }
    else {
        Write-Host "Nenhum python/python3/py encontrado no PATH."
    }

    if ($entries.Count -gt 0) {
        Write-Host "Instalacoes registradas para remocao:"
        $entries | ForEach-Object { Write-Host "  $($_.DisplayName) [$($_.Publisher)]" }
    }
    else {
        Write-Host "Nenhuma instalacao PSF/Python Launcher registrada para remocao."
    }

    if (-not $YesReallyRemoveGlobalPython) {
        throw "Remocao global bloqueada. Rode novamente com -RemoveGlobalPython -YesReallyRemoveGlobalPython para confirmar."
    }

    foreach ($entry in $entries) {
        $cmdLine = ConvertTo-QuietUninstallCommand $entry
        if (-not $cmdLine) {
            Write-Warning "Sem comando de uninstall para $($entry.DisplayName). Pulei."
            continue
        }
        if ($PSCmdlet.ShouldProcess($entry.DisplayName, "Uninstall global Python")) {
            Write-Step "Removendo $($entry.DisplayName)"
            $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/s", "/c", $cmdLine) -Wait -PassThru
            if ($process.ExitCode -ne 0) {
                Write-Warning "Uninstall retornou codigo $($process.ExitCode): $($entry.DisplayName)"
            }
        }
    }

    Remove-PythonEnvironmentVariables
    Disable-PythonWindowsAppAliases
    Remove-PythonPathEntries -KnownRoots $KnownRoots
    Remove-ResidualPythonDirectories -KnownRoots $KnownRoots
}

function Disable-PythonWindowsAppAliases {
    $windowsApps = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
    if (-not (Test-Path $windowsApps)) {
        return
    }

    foreach ($name in @("python.exe", "python3.exe")) {
        $aliasPath = Join-Path $windowsApps $name
        if (Test-Path $aliasPath) {
            try {
                if ($PSCmdlet.ShouldProcess($aliasPath, "Remove Windows Python app execution alias")) {
                    Remove-Item -LiteralPath $aliasPath -Force -ErrorAction Stop
                    Write-Host "Alias WindowsApps removido: $aliasPath"
                }
            }
            catch {
                Write-Warning "Nao consegui remover alias $aliasPath. Desative em Settings > Apps > Advanced app settings > App execution aliases."
            }
        }
    }
}

function Remove-PythonEnvironmentVariables {
    foreach ($target in @("User", "Machine")) {
        foreach ($name in @("PYTHONHOME", "PYTHONPATH", "PYLAUNCHER_ALLOW_INSTALL", "PYLAUNCHER_NO_SEARCH_PATH")) {
            try {
                if ([Environment]::GetEnvironmentVariable($name, $target)) {
                    if ($PSCmdlet.ShouldProcess("$target $name", "Remove Python environment variable")) {
                        [Environment]::SetEnvironmentVariable($name, $null, $target)
                    }
                }
            }
            catch {
                Write-Warning "Nao consegui limpar $target ${name}: $($_.Exception.Message)"
            }
        }
    }
}

function Test-PythonPathEntry {
    param(
        [string] $Entry,
        [string[]] $KnownRoots
    )

    if (-not $Entry) {
        return $false
    }

    $expanded = [Environment]::ExpandEnvironmentVariables($Entry).Trim('"').TrimEnd("\")
    if (-not $expanded) {
        return $false
    }

    if ($RemoveWindowsAppsFromPath -and $expanded -like "*\Microsoft\WindowsApps") {
        return $true
    }

    foreach ($root in $KnownRoots) {
        if ($root) {
            $normalizedRoot = [Environment]::ExpandEnvironmentVariables($root).Trim('"').TrimEnd("\")
            if ($normalizedRoot -and $expanded.StartsWith($normalizedRoot, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }

    return ($expanded -match "(?i)\\Programs\\Python\\Python\d+" -or
            $expanded -match "(?i)\\Python\d+(\\Scripts)?$" -or
            $expanded -match "(?i)\\PythonCore\\" -or
            $expanded -match "(?i)\\Python\\Launcher$")
}

function Remove-PythonPathEntries {
    param([string[]] $KnownRoots)

    foreach ($target in @("User", "Machine")) {
        try {
            $pathValue = [Environment]::GetEnvironmentVariable("Path", $target)
            if (-not $pathValue) {
                continue
            }
            $entries = @($pathValue -split ";" | Where-Object { $_ -ne "" })
            $kept = @()
            $removed = @()
            foreach ($entry in $entries) {
                if (Test-PythonPathEntry -Entry $entry -KnownRoots $KnownRoots) {
                    $removed += $entry
                }
                else {
                    $kept += $entry
                }
            }
            if ($removed.Count -gt 0) {
                if ($PSCmdlet.ShouldProcess("$target PATH", "Remove Python PATH entries")) {
                    [Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), $target)
                }
                Write-Host "PATH ${target}: removido"
                $removed | ForEach-Object { Write-Host "  $_" }
            }
        }
        catch {
            Write-Warning "Nao consegui editar PATH ${target}: $($_.Exception.Message)"
        }
    }
}

function Remove-ResidualPythonDirectories {
    param([string[]] $KnownRoots)

    $candidates = @()
    $candidates += $KnownRoots
    if ($env:LOCALAPPDATA) {
        $candidates += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Programs\Python") -Directory -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }
    if ($env:ProgramFiles) {
        $candidates += Get-ChildItem -Path $env:ProgramFiles -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Get-ChildItem -Path ${env:ProgramFiles(x86)} -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }

    $safeCandidates = $candidates |
        Where-Object { $_ -and (Test-Path $_) } |
        Sort-Object -Unique |
        Where-Object {
            $_ -match "(?i)\\Programs\\Python\\Python\d+" -or
            $_ -match "(?i)\\Python\d+$" -or
            $_ -match "(?i)\\Python\\Launcher$"
        }

    foreach ($dir in $safeCandidates) {
        if ($PSCmdlet.ShouldProcess($dir, "Remove residual Python directory")) {
            Write-Step "Removendo diretorio residual: $dir"
            Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Add-UvCandidatePaths {
    $paths = @(
        (Join-Path $HOME ".local\bin"),
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv")
    )

    foreach ($path in $paths) {
        if ($path -and (Test-Path $path) -and (($env:Path -split ";") -notcontains $path)) {
            $env:Path = "$path;$env:Path"
        }
    }

    $wingetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wingetRoot) {
        Get-ChildItem -Path $wingetRoot -Recurse -Filter "uv.exe" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty DirectoryName -Unique |
            ForEach-Object {
                if (($env:Path -split ";") -notcontains $_) {
                    $env:Path = "$_;$env:Path"
                }
            }
    }
}

function Find-UvExecutable {
    Add-UvCandidatePaths
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

function Invoke-UvInstallerUrl {
    param([string] $Url)

    Write-Step "Tentando instalador uv: $Url"
    try {
        $command = "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; irm '$Url' | iex"
        powershell -NoProfile -ExecutionPolicy Bypass -Command $command
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
        Write-Warning "Instalador uv retornou codigo $LASTEXITCODE."
    }
    catch {
        Write-Warning "Instalador uv falhou: $($_.Exception.Message)"
    }
    return $false
}

function Install-UvWithWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        return $false
    }

    Write-Step "Tentando instalar uv via winget"
    try {
        & $winget.Source install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements --silent
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
        Write-Warning "winget retornou codigo $LASTEXITCODE."
    }
    catch {
        Write-Warning "winget falhou: $($_.Exception.Message)"
    }
    return $false
}

function Install-UvFromReleaseZip {
    Write-Step "Tentando instalar uv pelo zip do GitHub Releases"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
            "aarch64"
        }
        elseif ([Environment]::Is64BitOperatingSystem) {
            "x86_64"
        }
        else {
            "i686"
        }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/uv/releases/latest" -UseBasicParsing
        $assetName = "uv-$arch-pc-windows-msvc.zip"
        $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
        if (-not $asset) {
            throw "Asset nao encontrado: $assetName"
        }

        $binDir = Join-Path $env:USERPROFILE ".local\bin"
        $tmpDir = Join-Path $env:TEMP ("uv-release-" + [guid]::NewGuid().ToString("N"))
        $zipPath = Join-Path $env:TEMP $assetName
        New-Item -ItemType Directory -Force $binDir | Out-Null
        New-Item -ItemType Directory -Force $tmpDir | Out-Null
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tmpDir -Force

        foreach ($exeName in @("uv.exe", "uvx.exe")) {
            $exe = Get-ChildItem -Path $tmpDir -Recurse -Filter $exeName -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($exe) {
                $destination = Join-Path $binDir $exeName
                try {
                    Copy-Item -LiteralPath $exe.FullName -Destination $destination -Force
                }
                catch {
                    if (Test-Path $destination) {
                        Write-Warning "$exeName ja existe e esta em uso; mantendo executavel existente."
                    }
                    else {
                        throw
                    }
                }
            }
        }
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue

        if (($env:Path -split ";") -notcontains $binDir) {
            $env:Path = "$binDir;$env:Path"
        }
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (($userPath -split ";") -notcontains $binDir) {
            [Environment]::SetEnvironmentVariable("Path", "$binDir;$userPath", "User")
        }
        return $true
    }
    catch {
        Write-Warning "Download direto do uv falhou: $($_.Exception.Message)"
    }
    return $false
}

function Install-UvStandalone {
    Write-Step "Instalando/atualizando uv"
    $installed = $false
    foreach ($url in @(
        "https://astral.sh/uv/install.ps1",
        "https://releases.astral.sh/github/uv/releases/download/0.11.8/uv-installer.ps1"
    )) {
        if (Invoke-UvInstallerUrl $url) {
            $installed = $true
            break
        }
    }
    if (-not $installed) {
        $installed = Install-UvWithWinget
    }
    if (-not $installed) {
        $installed = Install-UvFromReleaseZip
    }
    if (-not $installed) {
        throw "Falha ao instalar uv por instalador oficial, winget e zip direto."
    }
}

function Resolve-Uv {
    param([switch] $ForceInstall)

    $uv = Find-UvExecutable
    if ($uv -and $uv -notmatch "(?i)\\Python\d+\\Scripts\\") {
        return $uv
    }
    if ($ForceInstall) {
        Write-Step "Garantindo uv standalone antes do reset global"
    }

    Install-UvStandalone

    $uv = Find-UvExecutable
    if ($uv) {
        return $uv
    }

    throw "uv foi instalado, mas nao entrou no PATH desta sessao. Abra um novo PowerShell e rode novamente."
}

if (-not (Test-Path (Join-Path $ResolvedExtensionRoot "pyproject.toml"))) {
    throw "ExtensionRoot invalido: nao encontrei pyproject.toml em $ResolvedExtensionRoot"
}

$knownPythonRoots = @(Get-PythonInstallRoots | Where-Object { $_ } | Sort-Object -Unique)

Write-Step "Preparando diretorio persistente"
New-Item -ItemType Directory -Force $StateDir | Out-Null

$configPath = Join-Path $StateDir "config.toml"
if (-not (Test-Path $configPath)) {
    $configExample = Join-Path $ResolvedExtensionRoot "config.example.toml"
    if (Test-Path $configExample) {
        Copy-Item $configExample $configPath
        Write-Host "config.toml criado em $configPath"
    }
}

$envPath = Join-Path $StateDir ".env"
if (-not (Test-Path $envPath)) {
    $envExample = Join-Path $ResolvedExtensionRoot ".env.example"
    if (Test-Path $envExample) {
        Copy-Item $envExample $envPath
        Write-Host ".env criado em $envPath"
    }
}

$uv = Resolve-Uv -ForceInstall:$FullReset
Write-Step "Usando uv: $uv"
Invoke-Checked $uv @("--version")

if ($RemoveGlobalPython) {
    Invoke-GlobalPythonRemoval -KnownRoots $knownPythonRoots
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Step "Instalando Python gerenciado pelo uv ($PythonVersion)"
Invoke-Checked $uv @("python", "install", $PythonVersion)

$persistentVenv = Join-Path $StateDir ".venv"
$bundleVenv = Join-Path $ResolvedExtensionRoot ".venv"
foreach ($venv in @($persistentVenv, $bundleVenv)) {
    if (Test-Path $venv) {
        if ($PSCmdlet.ShouldProcess($venv, "Remove project virtual environment")) {
            Write-Step "Removendo ambiente antigo: $venv"
            Remove-Item -LiteralPath $venv -Recurse -Force
        }
    }
}

$env:UV_PROJECT_ENVIRONMENT = $persistentVenv
$syncArgs = @("sync", "--project", $ResolvedExtensionRoot, "--python", $PythonVersion)
if ($Dev) {
    $syncArgs += @("--extra", "dev")
}
if ($Pdf) {
    $syncArgs += @("--extra", "pdf")
}

Write-Step "Sincronizando dependencias com uv"
Push-Location $ResolvedExtensionRoot
try {
    # Equivalent command: uv sync --project "$ResolvedExtensionRoot"
    Invoke-Checked $uv $syncArgs

    if (-not $SkipChecks) {
        Write-Step "Rodando checks basicos"
        Invoke-Checked $uv @("run", "python", "-m", "enricher", "--help")
        Invoke-Checked $uv @("run", "python", "scripts\mednotes\wiki\cli.py", "validate", "--config", $configPath)
        Invoke-Checked $uv @("run", "python", "scripts\mednotes\wiki\cli.py", "run-linker", "--help")
    }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Pronto. Ambiente Python do workbench reconstruido com uv." -ForegroundColor Green
Write-Host "ExtensionRoot: $ResolvedExtensionRoot"
Write-Host "StateDir:      $StateDir"
Write-Host "Venv uv:       $persistentVenv"
Write-Host ""
Write-Host "Para comandos manuais nesta sessao:"
Write-Host ('$env:UV_PROJECT_ENVIRONMENT = "{0}"' -f $persistentVenv)
Write-Host 'uv run python scripts\mednotes\wiki\cli.py fix-wiki --dry-run --json'
if ($RemoveGlobalPython) {
    Write-Host ""
    Write-Host "Se 'where python' ainda apontar para Microsoft\\WindowsApps, desative o alias"
    Write-Host "python.exe/python3.exe em Settings > Apps > Advanced app settings > App execution aliases,"
    Write-Host "ou rode novamente com -RemoveWindowsAppsFromPath para remover WindowsApps do PATH."
}
