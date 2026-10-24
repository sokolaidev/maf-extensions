class AnalysisOutput : Microsoft.Windows.PowerShell.ScriptAnalyzer.IOutputWriter {
    [void] WriteError([System.Management.Automation.ErrorRecord] $error) { throw $error.Exception }
    [void] ThrowTerminatingError([System.Management.Automation.ErrorRecord] $error) { throw $error.Exception }
    [void] WriteWarning([string] $message) { throw $message }
    # Verbose and debug messages carry no diagnostics.
    [void] WriteVerbose([string] $message) { }
    [void] WriteDebug([string] $message) { }
}

[AnalysisOutput]::new()
