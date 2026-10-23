$ErrorActionPreference = 'Stop'
$WarningPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
Import-Module /opt/maf-shell/modules/PSScriptAnalyzer/1.25.0/PSScriptAnalyzer.psd1 -ErrorAction Stop
$assemblies = @()
foreach ($path in ([IO.Directory]::EnumerateFiles('/opt/maf-shell/dependencies/assemblies', '*.dll', [IO.SearchOption]::AllDirectories) | Sort-Object)) {
    $assembly = [Reflection.Assembly]::LoadFrom($path)
    if ($assembly.Location -ne $path) { throw 'Host assembly identity conflict' }
    $assemblies += $path
}
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json -AsHashtable
$isModule = [IO.Path]::GetExtension($request.path) -ieq '.psm1'
# The pinned analyzer recognizes modules only with a lowercase extension.
$analysisPath = if ($isModule) { [IO.Path]::ChangeExtension($request.path, '.psm1') } else { $request.path }
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($request.text, $analysisPath, [ref]$tokens, [ref]$parseErrors)
$declarations = @($ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.AttributeAst] -and
    $node.TypeName.FullName -match '(^|\.)SuppressMessage(Attribute)?$'
}, $true) | ForEach-Object {
    @{
        line = $_.Extent.StartLineNumber
        column = $_.Extent.StartColumnNumber
        text = $_.Extent.Text
    }
})
$diagnostics = @($parseErrors | ForEach-Object {
    @{
        file = $request.path
        line = $_.Extent.StartLineNumber
        column = $_.Extent.StartColumnNumber
        rule = 'PSParseError'
        severity = 'error'
        message = $_.Message
    }
})
$suppressed = @()
if ($parseErrors.Count -eq 0 -and $request.text.Length -gt 0) {
    $records = @(Invoke-ScriptAnalyzer -ScriptDefinition $request.text -Settings @{
        IncludeDefaultRules = $true
    } -IncludeSuppressed -ErrorAction Stop -WarningAction Stop)
    if ($isModule) {
        # Module checks need a named AST without enabling rules that read source bytes.
        $analyzer = [Microsoft.Windows.PowerShell.ScriptAnalyzer.ScriptAnalyzer]::Instance
        $analysisRunspace = [runspacefactory]::CreateRunspace()
        try {
            $analysisRunspace.Open()
            $output = & /opt/maf-shell/analysis-output.ps1
            $analyzer.Initialize(
                $analysisRunspace, $output, $null,
                @('PSAvoidGlobalFunctions', 'PSAvoidGlobalAliases'),
                $null, $null, $true, $false, $true, $null
            )
            $records += @($analyzer.AnalyzeSyntaxTree($ast, $tokens, $analysisPath, $false))
        } finally {
            try { $analyzer.CleanUp() } finally { $analysisRunspace.Dispose() }
        }
    }
    foreach ($record in $records) {
        $item = @{
            file = $request.path
            line = $record.Extent.StartLineNumber
            column = $record.Extent.StartColumnNumber
            rule = $record.RuleName
            severity = switch ($record.Severity.ToString()) {
                'Error' { 'error' }
                'Warning' { 'warning' }
                'Information' { 'info' }
                default { throw 'Unsupported diagnostic severity' }
            }
            message = $record.Message
        }
        if ($record.IsSuppressed) { $suppressed += $item } else { $diagnostics += $item }
    }
}
@{
    path = $request.path
    parsed = $parseErrors.Count -eq 0
    policy_conflict = $false
    diagnostics = $diagnostics
    suppressed_diagnostics = $suppressed
    suppressions = $declarations
    powershell = $PSVersionTable.PSVersion.ToString()
    psscriptanalyzer = (Get-Module PSScriptAnalyzer).Version.ToString()
    host_assemblies = $assemblies
} | ConvertTo-Json -Depth 12 -Compress
