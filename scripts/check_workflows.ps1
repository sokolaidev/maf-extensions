# Run the shared workflow checks with a native Python, preserving each argument and its status.
[CmdletBinding()]
param(
    [string] $Python = 'python',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $TestArgs
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$previousPythonUtf8 = $env:PYTHONUTF8
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $env:PYTHONUTF8 = '1'
    & $Python -m pytest tests/test_release_config.py tests/test_verify_live_retry.py tests/test_workflow_shells.py @TestArgs
    $status = $LASTEXITCODE
}
finally {
    Pop-Location
    $env:PYTHONUTF8 = $previousPythonUtf8
}
exit $status
