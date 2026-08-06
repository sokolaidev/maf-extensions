#!/usr/bin/env bash
# Grant a principal data-plane access to an ACA sandbox group, so an application can
# create, exec in, and delete sandboxes inside it.
#
# This is an OPERATOR script, run once, deliberately not part of any application's deploy
# pipeline. Two reasons:
#
#   1. It writes a role assignment into the SANDBOX group's resource group. An application
#      deploy that did this would need `Role Based Access Control Administrator` there —
#      a standing grant to write RBAC in someone else's resource group, held for the life
#      of the pipeline, to do something that happens once.
#   2. Who may run code in a sandbox is an access decision, not a build artefact. It should
#      be visible in an audit as a deliberate act by a person, not as a side effect of a
#      merge to main.
#
# Idempotent: `az role assignment create` is a no-op when the assignment already exists.
#
# Requires: az, logged in, with permission to create role assignments on the sandbox group.

set -euo pipefail

# Container Apps SandboxGroup Data Owner. Read from the live tenant rather than copied:
#   az role definition list --name "Container Apps SandboxGroup Data Owner" --query "[0].name" -o tsv
#
# Its permissions are dataActions ONLY — read/write/delete/action on sandboxGroups — with an
# empty `actions` list. That is the whole point: the holder can create and exec sandboxes but
# cannot touch the group resource itself. The similarly named "Container Apps SandboxGroup
# Contributor" is its mirror image (control-plane actions, no dataActions), so the two roles
# are disjoint rather than nested, and Contributor is NOT a narrower substitute here.
#
# It is, however, WIDE, and that is an accepted residual rather than an oversight: those four
# wildcards cover 59 distinct data-plane operations (`az provider operation show --namespace
# Microsoft.App`) and the application calls about nine. Notably it also grants the group's
# secrets — values included, via `secrets/peek/action` — and `sandboxes/egresspolicy/write`,
# the very allowlist the sandbox is supposed to be subject to. A custom role would narrow it
# to the nine; built-in roles only is a deliberate choice for now. See
# docs/work-in-progress/issue-408-exec-surface-security.md for the full comparison.
ROLE_NAME="Container Apps SandboxGroup Data Owner"

usage() {
  cat <<EOF
Usage: $(basename "$0") --group <name> --resource-group <name> (--principal-id <id> | --identity <name>) [options]

Required:
  -n, --group <name>              Sandbox group (Microsoft.App/sandboxGroups) name
  -g, --resource-group <name>     Resource group holding the sandbox group

  and one of:
  -p, --principal-id <object-id>  Object id of the principal to grant
  -i, --identity <name>           User-assigned managed identity to resolve a principal id
                                  from (e.g. the application's identity)

Options:
  -s, --subscription <id>         Subscription id (default: current az context)
      --identity-resource-group <name>
                                  Resource group of --identity (default: --resource-group)
      --revoke                    Remove the assignment instead of creating it
  -h, --help                      Show this help

Examples:
  # Grant the app's managed identity access to the sandbox group
  $(basename "$0") \\
      --group acas-ats-maf-swe-dev --resource-group acas-ats-maf-swe-rg \\
      --identity ats-identity --identity-resource-group ats-maf-swe-rg

  # Revoke it again
  $(basename "$0") \\
      --group acas-ats-maf-swe-dev --resource-group acas-ats-maf-swe-rg \\
      --principal-id 00000000-0000-0000-0000-000000000000 --revoke
EOF
}

GROUP=""
RESOURCE_GROUP=""
PRINCIPAL_ID=""
IDENTITY=""
IDENTITY_RG=""
SUBSCRIPTION=""
REVOKE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--group) GROUP="$2"; shift 2 ;;
    -g|--resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    -p|--principal-id) PRINCIPAL_ID="$2"; shift 2 ;;
    -i|--identity) IDENTITY="$2"; shift 2 ;;
    --identity-resource-group) IDENTITY_RG="$2"; shift 2 ;;
    -s|--subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --revoke) REVOKE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$GROUP" || -z "$RESOURCE_GROUP" ]]; then
  echo "error: --group and --resource-group are required" >&2
  usage >&2
  exit 2
fi
if [[ -z "$PRINCIPAL_ID" && -z "$IDENTITY" ]]; then
  echo "error: one of --principal-id or --identity is required" >&2
  usage >&2
  exit 2
fi

if [[ -n "$SUBSCRIPTION" ]]; then
  az account set --subscription "$SUBSCRIPTION"
fi
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"

if [[ -z "$PRINCIPAL_ID" ]]; then
  IDENTITY_RG="${IDENTITY_RG:-$RESOURCE_GROUP}"
  echo "resolving principal id of identity '$IDENTITY' in '$IDENTITY_RG'…" >&2
  PRINCIPAL_ID="$(az identity show \
    --name "$IDENTITY" --resource-group "$IDENTITY_RG" \
    --query principalId -o tsv)"
fi

SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.App/sandboxGroups/${GROUP}"

# Fail early with a clear message rather than letting the role assignment succeed against a
# scope that does not exist — Azure accepts assignments on absent resources, so a typo here
# would otherwise look like success and surface much later as "sandbox unavailable".
if ! az resource show --ids "$SCOPE" -o none 2>/dev/null; then
  echo "error: sandbox group not found at $SCOPE" >&2
  echo "       check --group / --resource-group / --subscription" >&2
  exit 1
fi

if [[ "$REVOKE" -eq 1 ]]; then
  echo "revoking '$ROLE_NAME' for $PRINCIPAL_ID on $GROUP…" >&2
  az role assignment delete \
    --assignee-object-id "$PRINCIPAL_ID" \
    --role "$ROLE_NAME" \
    --scope "$SCOPE"
  echo "revoked." >&2
  exit 0
fi

echo "granting '$ROLE_NAME' to $PRINCIPAL_ID on $GROUP…" >&2
# --assignee-object-id with --assignee-principal-type skips the Graph lookup that
# --assignee does, which a freshly created managed identity often loses a race against.
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "$ROLE_NAME" \
  --scope "$SCOPE" \
  -o none

echo "granted. RBAC can take a few minutes to propagate." >&2
