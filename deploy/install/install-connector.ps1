[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$BinaryUrl = "",
    [string]$Sha256 = "",
    [string]$AllowRoot = $HOME,
    [switch]$EnableCodexTasks,
    [switch]$ForceReauthorize
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-SecureUrl {
    param(
        [string]$Value,
        [string]$Label,
        [switch]$OriginOnly
    )

    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) {
        throw "$Label must be an absolute URL."
    }
    if ($uri.UserInfo -or $uri.Fragment) {
        throw "$Label must not contain user information or a fragment."
    }
    if ($OriginOnly -and (($uri.AbsolutePath -ne "/") -or $uri.Query)) {
        throw "$Label must be an origin such as https://arena.example."
    }
    $loopback = $uri.IsLoopback
    if (($uri.Scheme -ne "https") -and -not ($uri.Scheme -eq "http" -and $loopback)) {
        throw "$Label must use HTTPS. Plain HTTP is allowed only for localhost."
    }
    return $uri
}

function Quote-TaskArgument {
    param([string]$Value)

    if ($Value.Contains('"')) {
        throw "Scheduled task arguments must not contain quotation marks."
    }
    $escaped = [regex]::Replace($Value, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-DownloadWithoutRedirect {
    param(
        [Uri]$Uri,
        [string]$OutFile
    )

    try {
        # Platform release endpoints are canonical. Refusing every redirect
        # makes an HTTPS-to-HTTP downgrade impossible on all supported
        # Windows PowerShell versions.
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri $Uri `
            -OutFile $OutFile `
            -MaximumRedirection 0 `
            -ErrorAction Stop
    }
    catch {
        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        throw "Secure download failed or the server attempted a redirect: $Uri"
    }
}

$serverUri = Assert-SecureUrl -Value ($Server.Trim()) -Label "Server" -OriginOnly
$Server = $serverUri.AbsoluteUri.TrimEnd("/")

$architecture = switch ($env:PROCESSOR_ARCHITECTURE.ToUpperInvariant()) {
    "AMD64" { "amd64" }
    "ARM64" { "arm64" }
    default { throw "ADX Connector supports 64-bit AMD64 and ARM64 Windows." }
}

if (-not $BinaryUrl) {
    $BinaryUrl = "$Server/downloads/adx-connector-windows-$architecture.exe"
}
$null = Assert-SecureUrl -Value $BinaryUrl -Label "BinaryUrl"
if (([Uri]$BinaryUrl).Query) {
    throw "BinaryUrl query strings are not supported because the checksum URL must be deterministic."
}

$installDirectory = Join-Path $env:LOCALAPPDATA "ADX\Connector"
$target = Join-Path $installDirectory "adx-connector.exe"
$stateDirectory = Join-Path $env:APPDATA "adx\connector"
$statePath = Join-Path $stateDirectory "state.json"
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("adx-connector-" + [Guid]::NewGuid().ToString("N"))
$download = Join-Path $temporaryDirectory "adx-connector.exe"
$checksumFile = Join-Path $temporaryDirectory "adx-connector.exe.sha256"

New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    Write-Host "Downloading ADX Connector..."
    Invoke-DownloadWithoutRedirect -Uri ([Uri]$BinaryUrl) -OutFile $download

    $expectedHash = $Sha256.Trim()
    if (-not $expectedHash) {
        Invoke-DownloadWithoutRedirect -Uri ([Uri]"$BinaryUrl.sha256") -OutFile $checksumFile
        $match = [regex]::Match((Get-Content -Raw $checksumFile), '(?i)\b[0-9a-f]{64}\b')
        if (-not $match.Success) {
            throw "The published checksum file is invalid."
        }
        $expectedHash = $match.Value
    }
    if ($expectedHash -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Sha256 must contain exactly 64 hexadecimal characters."
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash
    if ($actualHash -ine $expectedHash) {
        throw "ADX Connector checksum verification failed."
    }

    $existingTask = Get-ScheduledTask -TaskName "ADX Local Connector" -ErrorAction SilentlyContinue
    if ($existingTask) {
        Stop-ScheduledTask -TaskName "ADX Local Connector" -ErrorAction SilentlyContinue
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $existingTask = Get-ScheduledTask -TaskName "ADX Local Connector" -ErrorAction SilentlyContinue
            if (-not $existingTask -or $existingTask.State -ne "Running") {
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if ($existingTask -and $existingTask.State -eq "Running") {
            throw "The existing ADX Connector task did not stop."
        }
    }

    New-Item -ItemType Directory -Force -Path $installDirectory | Out-Null
    Copy-Item -Force -LiteralPath $download -Destination $target

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & "$env:SystemRoot\System32\icacls.exe" $installDirectory /inheritance:r /grant:r "${identity}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the Connector installation directory ACL."
    }

    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
    & "$env:SystemRoot\System32\icacls.exe" $stateDirectory /inheritance:r /grant:r "${identity}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the Connector credential directory ACL."
    }

    if ($ForceReauthorize -or -not (Test-Path -LiteralPath $statePath)) {
        Write-Host "Your browser will open for one-time authorization."
        & $target pair --server $Server --state $statePath
        if ($LASTEXITCODE -ne 0) {
            throw "Connector authorization did not complete."
        }
    } else {
        Write-Host "Existing device authorization found; keeping it."
    }

    $arguments = @(
        "run",
        "--server", $Server,
        "--state", $statePath,
        "--auto-pair=false",
        "--allow-root", ([IO.Path]::GetFullPath($AllowRoot))
    )
    if ($EnableCodexTasks) {
        $arguments += "--enable-codex-tasks"
    }
    $argumentLine = ($arguments | ForEach-Object { Quote-TaskArgument $_ }) -join " "
    $action = New-ScheduledTaskAction -Execute $target -Argument $argumentLine -WorkingDirectory ([IO.Path]::GetFullPath($AllowRoot))
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask `
        -TaskName "ADX Local Connector" `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Keeps the user-authorized ADX Connector online." `
        -Force | Out-Null
    Start-ScheduledTask -TaskName "ADX Local Connector"

    Write-Host ""
    Write-Host "ADX Connector is installed and starting."
    Write-Host "Run '$target doctor' to inspect local readiness."
    Write-Host "Revoke the device in ADX Arena before uninstalling if this computer is no longer trusted."
}
finally {
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
    if ($resolvedTemporaryDirectory.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
