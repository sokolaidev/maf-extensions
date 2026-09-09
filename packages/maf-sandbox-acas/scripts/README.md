# `maf-sandbox-acas` scripts

## `import_disk_image.py`

Imports the `bicep-sandbox` OCI image into an ACA sandbox group as a **disk image**. A sandbox boots from a disk image registered in the group, which is a different namespace from the registry the image was pushed to, so the image has to be imported once before the app can resolve it by reference at runtime (`maf_sandbox_acas.resolve_disk_image_id`). It is idempotent — an image already imported from the same reference is reported and reused, not duplicated — and on success it prints the resolved disk-image id, so you can also use it to populate `BICEP_ACAS_SANDBOX_DISK_IMAGE_ID` if you would rather pin the id than resolve the reference.

A CI deploy is better served by the vendor `aca` CLI (`aca sandboxgroup disk create --identity …`), which needs no Python toolchain and nothing from this repository — [`images/bicep-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/images/bicep-sandbox) has that command line, alongside the build and push it follows. This script is the equivalent for anyone who would rather not install that CLI, and it takes its scope as explicit arguments rather than from the environment.

### Usage

One line, so it pastes cleanly into either shell (a wrapped form needs `\` per line in `bash` but a backtick `` ` `` in PowerShell — mixing them up is the usual cause of a "missing expression" or "failed to spawn" error):

```bash
uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/import_disk_image.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name> --image <name>.azurecr.io/bicep-sandbox:<tag> --identity <managed-identity-resource-id>
```

`--package maf-sandbox-acas` keeps the environment to this distribution's own closure rather than the host workspace's, because nothing here needs the host.

| Argument | What it is |
|---|---|
| `--endpoint` | Sandbox group data-plane endpoint, `https://management.<region>.azuredevcompute.io` |
| `--subscription` | Subscription id **of the sandbox group** |
| `--resource-group` | Resource group **that contains the sandbox group** — not the registry's, and not the managed identity's |
| `--group` | Sandbox group name |
| `--image` | OCI reference to import, `<name>.azurecr.io/<repository>:<tag>` (the registry as its login-server FQDN) |
| `--identity` | Resource id of a managed identity that can pull from the registry and is attached to the group — required for a private registry (below) |
| `--name` | Optional disk-image name; defaults to one derived from the tag |

### Authentication

The script itself authenticates with `DefaultAzureCredential`, so an `az login` session is enough for the one-off run. Reaching the *registry* is separate: the sandbox group pulls the image, so `--identity` must name a managed identity that (a) can pull from that registry and (b) is attached to the sandbox group — a host's infrastructure-as-code for the group usually provisions one and exposes its resource id as an output. The pull role is `Container Registry Repository Reader` on a registry in *RBAC + ABAC* permissions mode, or `AcrPull` on a classic-mode one. Without `--identity`, a private registry answers the pull with a 403 and the create-disk-image operation fails.

## `recover_lifecycle_policy.py`

Finds backend-owned sandboxes whose effective lifecycle metadata does not show auto-delete, then installs the backend lifecycle policy or deletes expired candidates. It uses the service inventory and the backend's labels (`scope`, `thread`, `agent`, `kind`), not the process registry, so it can run after the host process that created a sandbox is gone. Preview is the default.

```bash
uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/recover_lifecycle_policy.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name>
```

Pass `--apply` to change the group. Sandboxes newer than `--fresh-for-minutes` are retained as configuration-in-progress, sandboxes that already report auto-delete are retained, and unrelated sandboxes are ignored. For older backend-owned candidates, the script installs `--auto-suspend-seconds` and `--auto-delete-seconds` and re-reads the sandbox to verify that auto-delete is now visible. Verification checks the SDK's `lifecycle` metadata up to seven times, with five seconds between reads, to allow delayed visibility. If the policy still lacks auto-delete, the report names the exhausted read count. If installation fails or auto-delete remains absent, it deletes only candidates that are expired by `--stopped-for-hours` (default: 24 hours) or by `--max-age-hours` (default: 168 hours, or 7 days); use `--no-max-age` to keep active sandboxes from being deleted solely by creation age. A verification read error is reported and retains the sandbox; a sandbox that disappears during verification is recorded as already absent.
