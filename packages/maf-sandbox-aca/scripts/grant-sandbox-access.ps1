<#
.SYNOPSIS
Grant a principal data-plane access to an ACA sandbox group.

.DESCRIPTION
Lets an application create, exec in, and delete sandboxes inside a sandbox group.

This is an OPERATOR script, run once, deliberately not part of any application's deploy
pipeline. Two reasons:

 1. It writes a role assignment into the SANDBOX group's resource group. An application
    deploy that did this would need `Role Based Access Control Administrator` there — a
    standing grant to write RBAC in someone else's resource group, held for the life of the
    pipeline, to do something that happens once.
 2. Who may run code in a sandbox is an access decision, not a build artefact. It should be
    visible in an audit as a deliberate act by a person, not as a side effect of a merge.

Idempotent: creating an assignment that already exists is a no-op.

Requires: az, logged in, with permission to create role assignments on the sandbox group.

.EXAMPLE
./grant-sandbox-access.ps1 -Group acas-swe-dev -ResourceGroup acas-swe-rg `
    -Identity app-identity -IdentityResourceGroup app-swe-rg

.EXAMPLE
./grant-sandbox-access.ps1 -Group acas-swe-dev -ResourceGroup acas-swe-rg `
    -PrincipalId 00000000-0000-0000-0000-000000000000 -Revoke
#>
[CmdletBinding()]
param(
    # Sandbox group (Microsoft.App/sandboxGroups) name.
    [Parameter(Mandatory = $true)][string]$Group,

    # Resource group holding the sandbox group.
    [Parameter(Mandatory = $true)][string]$ResourceGroup,

    # Object id of the principal to grant. One of -PrincipalId or -Identity is required.
    [string]$PrincipalId,

    # User-assigned managed identity to resolve a principal id from.
    [string]$Identity,

    # Resource group of -Identity (default: -ResourceGroup).
    [string]$IdentityResourceGroup,

    # Subscription id (default: current az context).
    [string]$Subscription,

    # Remove the assignment instead of creating it.
    [switch]$Revoke
)

$ErrorActionPreference = 'Stop'

# Container Apps SandboxGroup Data Owner. Read from the live tenant rather than copied:
#   az role definition list --name "Container Apps SandboxGroup Data Owner" --query "[0].name" -o tsv
#
# Its permissions are dataActions ONLY — read/write/delete/action on sandboxGroups — with an
# empty `actions` list, so the holder can create and exec sandboxes but cannot touch the
# group resource itself. "Container Apps SandboxGroup Contributor" is its mirror image
# (control-plane actions, no dataActions): the two are disjoint, not nested, so Contributor
# is NOT a narrower substitute here.
#
# It is, however, WIDE, and that is an accepted residual rather than an oversight: those four
# wildcards cover 59 distinct data-plane operations (`az provider operation show --namespace
# Microsoft.App`) and the application calls about nine. Notably it also grants the group's
# secrets — values included, via `secrets/peek/action` — and `sandboxes/egresspolicy/write`,
# the very allowlist the sandbox is supposed to be subject to. A custom role would narrow it
# to the nine; built-in roles only is a deliberate choice for now. See
# docs/work-in-progress/issue-408-exec-surface-security.md for the full comparison.
$RoleName = 'Container Apps SandboxGroup Data Owner'

if (-not $PrincipalId -and -not $Identity) {
    throw 'One of -PrincipalId or -Identity is required.'
}

if ($Subscription) {
    az account set --subscription $Subscription
}
$SubscriptionId = az account show --query id -o tsv

if (-not $PrincipalId) {
    if (-not $IdentityResourceGroup) { $IdentityResourceGroup = $ResourceGroup }
    Write-Host "resolving principal id of identity '$Identity' in '$IdentityResourceGroup'…"
    $PrincipalId = az identity show `
        --name $Identity --resource-group $IdentityResourceGroup `
        --query principalId -o tsv
}

$Scope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/sandboxGroups/$Group"

# Fail early with a clear message rather than letting the role assignment succeed against a
# scope that does not exist — Azure accepts assignments on absent resources, so a typo here
# would otherwise look like success and surface much later as "sandbox unavailable".
try { az resource show --ids $Scope -o none } catch {
    throw "Sandbox group not found at $Scope. Check -Group / -ResourceGroup / -Subscription."
}

if ($Revoke) {
    Write-Host "revoking '$RoleName' for $PrincipalId on $Group…"
    az role assignment delete `
        --assignee-object-id $PrincipalId `
        --role $RoleName `
        --scope $Scope
    Write-Host 'revoked.'
    return
}

Write-Host "granting '$RoleName' to $PrincipalId on $Group…"
# --assignee-object-id with --assignee-principal-type skips the Graph lookup that --assignee
# does, which a freshly created managed identity often loses a race against.
az role assignment create `
    --assignee-object-id $PrincipalId `
    --assignee-principal-type ServicePrincipal `
    --role $RoleName `
    --scope $Scope `
    -o none

Write-Host 'granted. RBAC can take a few minutes to propagate.'
