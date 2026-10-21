[IO.File]::WriteAllText('/tmp/maf-shell-host-module-loaded', 'host')
function Get-HostValue { param([string]$Name) $Name }
