$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module /opt/maf-shell/modules/PSScriptAnalyzer/1.25.0/PSScriptAnalyzer.psd1
$root = '/tmp/maf-shell-probe'
[System.IO.Directory]::CreateDirectory($root) | Out-Null
$marker = "$root/loaded"
$module = "$root/Probe.psm1"
[System.IO.File]::WriteAllText($module, @'
[System.IO.File]::WriteAllText('/tmp/maf-shell-probe/loaded', 'module body executed')
class ProbeType { [int] $Value }
function Get-Probe { param([string]$Name) $Name }
Export-ModuleMember -Function Get-Probe
'@)
$cases = [ordered]@{
    body = "[System.IO.File]::WriteAllText('$marker', 'body executed')"
    using_module = "using module $module`n[ProbeType]::new()"
    command_lookup = "$module\Get-Probe -Name hello"
    import_statement = "Import-Module '$module'"
    dynamic_parameter = "function Get-Test { dynamicparam { [System.IO.File]::WriteAllText('$marker', 'dynamic parameter executed') } }; Get-Test -Name hello"
    attribute = "[ValidateScript({ [System.IO.File]::WriteAllText('$marker', 'attribute executed') })]param([string]`$Name)"
}
$results = @()
foreach ($case in $cases.GetEnumerator()) {
    foreach ($phase in @('parse', 'analyze')) {
        if ([System.IO.File]::Exists($marker)) { [System.IO.File]::Delete($marker) }
        $failure = $null
        $parseErrors = @()
        try {
            if ($phase -eq 'parse') {
                $tokens = $null
                [System.Management.Automation.Language.Parser]::ParseInput($case.Value, "$root/input.ps1", [ref]$tokens, [ref]$parseErrors) | Out-Null
            } else {
                Invoke-ScriptAnalyzer -ScriptDefinition $case.Value -IncludeSuppressed -Settings @{ IncludeDefaultRules = $true } | Out-Null
            }
        } catch { $failure = $_.Exception.GetType().FullName }
        $results += @{
            case = $case.Key
            phase = $phase
            executed = [System.IO.File]::Exists($marker)
            errors = @($parseErrors | ForEach-Object ErrorId)
            failure = $failure
        }
    }
}
@{ powershell = $PSVersionTable.PSVersion.ToString(); analyzer = (Get-Module PSScriptAnalyzer).Version.ToString(); results = $results } | ConvertTo-Json -Depth 8 -Compress
