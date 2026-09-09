# `maf-sandbox-acas` scripts

## `import_disk_image.py`

Imports an OCI image into an ACA sandbox group as a **disk image**. A sandbox boots from a disk image registered in the group, which is a different namespace from the registry the image was pushed to, so the image has to be imported before the app can resolve it by reference at runtime (`maf_sandbox_acas.resolve_disk_image_id`). A successful import prints only the new disk-image id to stdout and exits 0, so you can also use it to populate `BICEP_ACAS_SANDBOX_DISK_IMAGE_ID` if you would rather pin the id than resolve the reference.

The vendor `aca` CLI (`aca sandboxgroup disk create --username … --token …`) is another option that needs no Python toolchain — [`images/bicep-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/images/bicep-sandbox) has that command line, alongside the build and push it follows. This script takes its scope as explicit arguments rather than from the environment.

### Usage

One line, so it pastes cleanly into either shell (a wrapped form needs `\` per line in `bash` but a backtick `` ` `` in PowerShell — mixing them up is the usual cause of a "missing expression" or "failed to spawn" error):

```bash
az acr login --name <registry> --expose-token --query accessToken -o tsv | uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/import_disk_image.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name> --image <name>.azurecr.io/bicep-sandbox:<tag> --username 00000000-0000-0000-0000-000000000000 --token-stdin
```

`--package maf-sandbox-acas` keeps the environment to this distribution's own closure rather than the host workspace's, because nothing here needs the host.

| Argument | What it is |
|---|---|
| `--endpoint` | Sandbox group data-plane endpoint, `https://management.<region>.azuredevcompute.io` |
| `--subscription` | Subscription id **of the sandbox group** |
| `--resource-group` | Resource group **that contains the sandbox group** — not the registry's, and not the managed identity's |
| `--group` | Sandbox group name |
| `--image` | OCI reference to import, `<name>.azurecr.io/<repository>:<tag>` (the registry as its login-server FQDN) |
| `--username` | Registry username; for an exposed ACR token, use `00000000-0000-0000-0000-000000000000` |
| `--token` | Registry token, paired with `--username`; mutually exclusive with `--token-stdin` |
| `--token-stdin` | Read the registry token from stdin, paired with `--username`; keeps the token out of this script's command-line arguments |
| `--identity` | Alternative managed identity resource id for the pull; cannot be combined with registry credentials, and has a measured service failure (below) |
| `--name` | Optional disk-image name; defaults to one derived from the tag |

### Authentication

The script authenticates to the sandbox data plane with `DefaultAzureCredential`, using an `az login` session with permission to manage the group. Registry authentication is separate: `--username` and a token become the SDK's `RegistryCredentials`. Omit both for an anonymously accessible image. Incomplete credentials, empty credentials, or credentials combined with `--identity` are rejected before contacting Azure.

For a private ACR registry, use the exposed token shown above. [The measured import](../../../images/bicep-sandbox/README.md#import-it-into-the-sandbox-group) succeeded with a username and token, while `--identity` returned `RegistryAuthFailed` 401 even with the identity attached to the sandbox group and holding `AcrPull` on a classic-permissions-mode registry. The same rejection occurred with the vendor CLI. `--identity` remains available for deployments where the preview service supports it; it is not a prerequisite for token authentication or a verified fix for that service failure. This is the recorded measurement, not a fresh live-service verification.

### Existing references and new builds

The script refuses any reference already listed in the group: it prints `Nothing imported` and the existing id to stderr, leaves stdout empty, and exits 1. This applies to tags and digest references alike. It neither checks the registry for changed contents nor deletes or replaces an existing snapshot; changing `--name` does not bypass the refusal.

Push each changed build under a new revision tag (for example, `bicep-sandbox:0.46.1-1` → `bicep-sandbox:0.46.1-2`), import that reference, and update the host's image setting. If you intend to reuse an existing snapshot, pin its disk-image id explicitly. When several snapshots already share a reference, `resolve_disk_image_id` refuses an uncached lookup and names the candidate ids instead of selecting by listing order. Successful lookups remain cached for the process lifetime, so restart the host after changing imports or its image configuration. Serialize imports for a group and reference: the listing check and creation are separate service operations.

## `recover_lifecycle_policy.py`

Finds backend-owned sandboxes whose effective lifecycle metadata does not show auto-delete, then installs the backend lifecycle policy or deletes expired candidates. It uses the service inventory and the backend's labels (`scope`, `thread`, `agent`, `kind`), not the process registry, so it can run after the host process that created a sandbox is gone. Preview is the default.

```bash
uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/recover_lifecycle_policy.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name>
```

Pass `--apply` to change the group. Sandboxes newer than `--fresh-for-minutes` are retained as configuration-in-progress, sandboxes that already report auto-delete are retained, and unrelated sandboxes are ignored. For older backend-owned candidates, the script installs `--auto-suspend-seconds` and `--auto-delete-seconds` and re-reads the sandbox to verify that auto-delete is now visible. Verification checks the SDK's `lifecycle` metadata up to seven times, with five seconds between reads, to allow delayed visibility. If the policy still lacks auto-delete, the report names the exhausted read count. If installation fails or auto-delete remains absent, it deletes only candidates that are expired by `--stopped-for-hours` (default: 24 hours) or by `--max-age-hours` (default: 168 hours, or 7 days); use `--no-max-age` to keep active sandboxes from being deleted solely by creation age. A verification read error is reported and retains the sandbox; a sandbox that disappears during verification is recorded as already absent.
