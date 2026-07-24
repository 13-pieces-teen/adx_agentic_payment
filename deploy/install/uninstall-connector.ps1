[CmdletBinding()]
param(
    [switch]$PurgeCredentials
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "ADX"))
$installDirectory = [IO.Path]::GetFullPath((Join-Path $installRoot "Connector"))
$stateRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA "adx"))
$stateDirectory = [IO.Path]::GetFullPath((Join-Path $stateRoot "connector"))

$task = Get-ScheduledTask -TaskName "ADX Local Connector" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "ADX Local Connector" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "ADX Local Connector" -Confirm:$false
}

if (
    $installDirectory.StartsWith($installRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
    (Test-Path -LiteralPath $installDirectory)
) {
    Remove-Item -LiteralPath $installDirectory -Recurse -Force
}

if ($PurgeCredentials) {
    if (
        $stateDirectory.StartsWith($stateRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $stateDirectory)
    ) {
        Remove-Item -LiteralPath $stateDirectory -Recurse -Force
        Write-Host "Removed local Connector credentials and event state. This cannot be recovered."
    }
} else {
    Write-Host "Local credentials were retained. Use -PurgeCredentials to remove them."
}

Write-Host "ADX Connector was removed from startup."
Write-Host "Device revocation is a separate action in ADX Arena."
