# ACAS operator scripts

Run these scripts from a repository checkout. They use the `maf-sandbox-acas` environment and explicit Azure scope arguments.

## Import a disk image

`import_disk_image.py` imports an OCI image into a sandbox group's disk-image catalogue. Pushing an image to a registry does not register it with the sandbox group.

For a private Azure Container Registry, pass an exposed token through stdin:

```bash
az acr login --name <registry> --expose-token --query accessToken -o tsv | uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/import_disk_image.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name> --image <registry>.azurecr.io/bicep-sandbox:<tag> --username 00000000-0000-0000-0000-000000000000 --token-stdin
```

Successful import prints only the new disk-image ID to stdout and exits zero. The host can pin that ID or resolve the imported reference with `resolve_disk_image_id`.

| Argument | Meaning |
|---|---|
| `--endpoint` | Sandbox data-plane endpoint. |
| `--subscription`, `--resource-group`, `--group` | Location of the sandbox group, not the registry or identity. |
| `--image` | Fully qualified OCI reference to import. |
| `--username` | Registry username; use the zero UUID above for an exposed ACR token. |
| `--token-stdin` | Read the token without putting it in this script's command-line arguments. |
| `--token` | Alternative token argument; cannot be combined with `--token-stdin`. |
| `--identity` | Alternative managed identity resource ID for the pull; cannot be combined with registry credentials. |
| `--name` | Optional disk-image name; otherwise derived from the tag. |

The script uses `DefaultAzureCredential` for sandbox-group operations. Registry authentication is separate. Omit registry credentials for an anonymously accessible image. Empty, incomplete or conflicting credentials refuse before contacting Azure.

The recorded private-ACR import succeeded with username and token. Managed-identity pull returned `RegistryAuthFailed` despite the tested assignment and role. `--identity` remains available where the preview service supports it, but is not a verified fix for that failure. See the [image import record](https://github.com/sokolaidev/maf-extensions/blob/main/images/bicep-sandbox/README.md#import-it-into-the-sandbox-group).

### Existing references

An already listed reference is refused: stdout stays empty, stderr identifies the existing image and the script exits one. This applies to tags and digests. Changing `--name` does not replace the snapshot.

Give each changed build a new revision tag, import it and update the host setting. To reuse a snapshot, pin its disk-image ID.

`resolve_disk_image_id` refuses an uncached lookup when multiple snapshots share a reference. Successful lookups remain cached for the process lifetime. Restart the host after changing imports or configuration.

Serialize imports for the same group and reference. Listing and creation are separate service operations.

## Recover lifecycle policy

`recover_lifecycle_policy.py` finds backend-owned sandboxes whose service metadata lacks auto-delete. It uses service labels, so it can run after the creating host has exited.

Preview is the default:

```bash
uv run --package maf-sandbox-acas python packages/maf-sandbox-acas/scripts/recover_lifecycle_policy.py --endpoint https://management.<region>.azuredevcompute.io --subscription <sub-id> --resource-group <sandbox-group-rg> --group <sandbox-group-name>
```

Pass `--apply` to change resources. The script retains unrelated sandboxes, recently created candidates and sandboxes already reporting auto-delete.

For older candidates it installs `--auto-suspend-seconds` and `--auto-delete-seconds`, then verifies the service metadata. Verification allows seven reads, five seconds apart. A read error retains the sandbox and reports the error; disappearance is recorded as already absent.

If policy installation fails or verification still lacks auto-delete, only expired candidates are deleted:

| Expiry setting | Default |
|---|---|
| `--stopped-for-hours` | 24 hours stopped |
| `--max-age-hours` | 168 hours since creation, including active sandboxes |
| `--no-max-age` | Disable deletion based only on creation age |

Choose retention values for the deployment. Creation age is a maximum lifetime, not evidence that a sandbox is idle.
