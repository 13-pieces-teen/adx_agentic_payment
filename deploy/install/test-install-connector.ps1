[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptPath = Join-Path $PSScriptRoot "install-connector.ps1"
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    $messages = ($parseErrors | ForEach-Object Message) -join "; "
    throw "install-connector.ps1 has parser errors: $messages"
}

$webRequests = $ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq "Invoke-WebRequest"
}, $true)
if ($webRequests.Count -ne 1) {
    throw "Expected exactly one guarded Invoke-WebRequest call, found $($webRequests.Count)."
}

$commandText = $webRequests[0].Extent.Text
if ($commandText -notmatch '(?s)-MaximumRedirection\s+0(?:\s|`|$)') {
    throw "Invoke-WebRequest must refuse redirects with -MaximumRedirection 0."
}
if ($commandText -notmatch '(?s)-ErrorAction\s+Stop(?:\s|`|$)') {
    throw "Invoke-WebRequest must fail closed with -ErrorAction Stop."
}

$source = [IO.File]::ReadAllText($scriptPath)
if ($source -notmatch '(?s)pair\s+--server\s+\$Server\s+--state\s+\$statePath') {
    throw "Pairing must use the installer-owned state path."
}
if ($source -notmatch '"--state",\s+\$statePath') {
    throw "The Scheduled Task must use the installer-owned state path."
}

Write-Host "install-connector.ps1 security checks passed."
