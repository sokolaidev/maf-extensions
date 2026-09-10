# Run the shared workflow checks with a native Python, preserving each argument and its status.
[CmdletBinding()]
param(
    [string] $Python = 'python',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $TestArgs
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $Python -m pytest tests -m workflow @TestArgs
    $status = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $status
