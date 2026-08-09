# `maf-sandbox-aca` scripts

## `import_disk_image.py`

Imports the `bicep-sandbox` OCI image into an ACA sandbox group as a **disk image**. A sandbox boots from a disk image registered in the group, which is a different namespace from the registry the image was pushed to, so the image has to be imported once before the app can resolve it by reference at runtime (`maf_sandbox_aca.resolve_disk_image_id`). It is idempotent — an image already imported from the same reference is reported and reused, not duplicated — and on success it prints the resolved disk-image id, so you can also use it to populate `BICEP_ACA_SANDBOX_DISK_IMAGE_ID` if you would rather pin the id than resolve the reference.

The deploy workflow no longer runs this — it uses the vendor `aca` CLI (`aca sandboxgroup disk create --identity …`), which needs no Python toolchain and nothing from this repository. This script is the equivalent for anyone who would rather not install that CLI.

### Usage

One line, so it pastes cleanly into either shell (a wrapped form needs `\` per line in `bash` but a backtick `` ` `` in PowerShell — mixing them up is the usual cause of a "missing expression" or "failed to spawn" error):

```bash
uv run --package maf-sandbox-aca python packages/maf-sandbox-aca/scripts/import_disk_image.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name> --image <name>.azurecr.io/bicep-sandbox:<tag> --identity <managed-identity-resource-id>
```

`--package maf-sandbox-aca` keeps the environment to this distribution's own closure rather than the host workspace's, because nothing here needs the host.

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
