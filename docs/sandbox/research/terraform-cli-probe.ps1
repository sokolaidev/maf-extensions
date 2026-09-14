param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$probeDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $probeDirectory -Force | Out-Null
$downloads = @(
    @{ Name = 'terraform'; Version = '1.16.2'; Base = 'https://releases.hashicorp.com/terraform/1.16.2'; Archive = 'terraform_1.16.2_linux_amd64.zip'; Sha256 = '0d17011f0c4664539b164b044903d04e296c86c13cb9f28040076c65cfb3985a' },
    @{ Name = 'tofu'; Version = '1.12.6'; Base = 'https://github.com/opentofu/opentofu/releases/download/v1.12.6'; Archive = 'tofu_1.12.6_linux_amd64.zip'; Sha256 = '5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8' },
    @{ Name = 'random'; Version = '3.7.2'; Base = 'https://releases.hashicorp.com/terraform-provider-random/3.7.2'; Archive = 'terraform-provider-random_3.7.2_linux_amd64.zip'; Sha256 = '7b8434212eef0f8c83f5a90c6d76feaf850f6502b61b53c329e85b3b281cba34' }
)
$manifest = foreach ($download in $downloads) {
    $archivePath = Join-Path $probeDirectory $download.Archive
    if (-not (Test-Path -LiteralPath $archivePath)) {
        Invoke-WebRequest "$($download.Base)/$($download.Archive)" -OutFile $archivePath
    }
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $download.Sha256) { throw "Checksum mismatch for $($download.Archive)" }
    if ($download.Name -eq 'random') {
        foreach ($registry in @('registry.terraform.io', 'registry.opentofu.org')) {
            $mirrorDirectory = Join-Path $probeDirectory "mirror/$registry/hashicorp/random"
            New-Item -ItemType Directory -Path $mirrorDirectory -Force | Out-Null
            Copy-Item -LiteralPath $archivePath -Destination $mirrorDirectory -Force
        }
    } else {
        Expand-Archive -LiteralPath $archivePath -DestinationPath (Join-Path $probeDirectory "bin/$($download.Name)") -Force
    }
    [ordered]@{ name = $download.Name; version = $download.Version; url = "$($download.Base)/$($download.Archive)"; sha256 = $actualHash }
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $probeDirectory 'downloads.json') -Encoding utf8
$guestDirectory = Join-Path $probeDirectory 'guest'
New-Item -ItemType Directory -Path $guestDirectory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'terraform-cli-probe.py') -Destination (Join-Path $guestDirectory 'probe.py') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'terraform-cli-probe.Dockerfile') -Destination (Join-Path $probeDirectory 'Dockerfile') -Force
@'
*
!bin/
!bin/**
!mirror/
!mirror/**
!guest/
!guest/probe.py
'@ | Set-Content -LiteralPath (Join-Path $probeDirectory '.dockerignore') -Encoding utf8

$probeName = 'maf-terraform-probe-' + [guid]::NewGuid().ToString('N')
$probeImage = "${probeName}:local"
docker build --network none --pull=false -t $probeImage $probeDirectory
if ($LASTEXITCODE -ne 0) { throw 'Could not build the local research image' }
docker image inspect $probeImage --format '{{.Id}}' | Set-Content -LiteralPath (Join-Path $probeDirectory 'image-id.txt')
try {
    docker run --rm --pull never --name $probeName --label research=terraform --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --memory 768m --cpus 2 --pids-limit 128 --tmpfs '/tmp:rw,exec,nosuid,nodev,size=384m,mode=1777' $probeImage > (Join-Path $probeDirectory 'raw-results.json')
    if ($LASTEXITCODE -ne 0) { throw 'The CLI probe failed; inspect the captured output' }
} finally {
    # The generated name belongs only to this probe; a failed run must not leave its container.
    $remaining = docker ps -aq --filter "name=^/${probeName}$"
    if ($remaining) { docker rm -f $probeName | Out-Null }
}
Write-Output "Research results: $(Join-Path $probeDirectory 'raw-results.json')"
