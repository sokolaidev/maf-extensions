# `bicep-sandbox` — the image `bicep_validate` runs in

The base image has two layers on Azure Linux: a pinned Bicep CLI, and a [`bicepconfig.json`](bicepconfig.json) at `/maf-sandbox/work`. The optional [prepared profile](#prepared-avm-profile) adds a verified module cache for closed-network validation. Neither runtime image carries agent code or Python; files to compile arrive at run time.

Both samples run this one image. [`samples/01_acas_bicep`](../../samples/01_acas_bicep/) boots it as a disk image in an Azure Container Apps sandbox group; [`samples/02_wslc_bicep`](../../samples/02_wslc_bicep/) runs it as a local container under `wslc`. Sharing it is deliberate: the two samples exist to show that only the backend changes, and they would not be comparable if each validated against a compiler of its own.

## What is in it

| | Why |
|---|---|
| `mcr.microsoft.com/azurelinux/base/core:3.0` | A small Microsoft-maintained base with `tdnf`. Nothing in the tool depends on the distribution — it runs `bicep` and reads SARIF back |
| `icu` | Without it the CLI aborts at startup: `Couldn't find a valid ICU package`. It is not optional for a .NET single-file binary unless you set the invariant-globalization switch |
| `ca-certificates` | Module restore is HTTPS to MCR. Without them every `br/public:` reference fails to restore |
| Bicep CLI, pinned to `v0.46.1` | The pin is the point. Diagnostic wording, built-in rule levels and the API-version cut-off all follow the compiler, so an unpinned image would let a sample's documented output drift underneath it |
| `bicepconfig.json` at `/maf-sandbox/work` | Fallback policy for clients that do not stage a configuration |

## Configuration discovery

The Bicep kind uploads its selected configuration into each call directory before staging sources. It uses the [packaged configuration](../../packages/maf-sandbox-bicep/src/maf_sandbox_bicep/bicepconfig.json) by default, or the host's `config` argument. Bicep finds that file by walking up from the source directory. The selected policy takes precedence over the image's fallback config, including for nested sources and reused sandboxes.

Clients that do not stage a configuration depend on the fallback at `/maf-sandbox/work`. The pinned CLI has no `--config-file` flag. Compiling outside that directory uses built-in defaults, which changes these sample diagnostics:

| | Compiled under `/maf-sandbox/work` | Compiled elsewhere |
|---|---|---|
| `no-unused-params` | `"level": "error"` | no `level` at all — the rule's built-in default, `warning` |
| `use-recent-api-versions` | reported, with the age in days | **absent** — the config is what switches it on |
| SARIF | parses, diagnostics render | parses, diagnostics render |

`scripts/check_live_sample.py` checks these diagnostics to verify configuration discovery. For clients relying on the fallback, a disk image must contain the current file; replacing its source tag does not update an imported disk image.

## Build

From the repository root, so the build context is this directory:

```bash
wslc build -t bicep-sandbox:local images/bicep-sandbox
```

That is sample 02, and it is the whole story there — `wslc` runs what is already on the machine, so there is nothing to push and nothing to import. Docker and podman take the same arguments (`docker build -t bicep-sandbox:local images/bicep-sandbox`).

The registry and import steps below apply to either profile.

## Prepared AVM profile

[`prepared.Dockerfile`](prepared.Dockerfile) restores the explicit selection in [`dependencies.bicep-avm.policy.json`](dependencies.bicep-avm.policy.json) at build time. The initial profile contains `br/public:avm/res/network/virtual-network:0.7.2` and `br/public:avm/res/storage/storage-account:0.31.0`. Other resource modules and versions, plus pattern and utility modules, are excluded with reasons in the policy and manifest. This is a small selected profile, not the complete AVM catalog.

From the repository root:

```bash
docker build -t bicep-sandbox:0.46.1-1 images/bicep-sandbox
uv run python scripts/build_bicep_prepared_image.py --base-image bicep-sandbox:0.46.1-1
```

The helper prints a tag of the form `bicep-sandbox:0.46.1-prepared-1-<manifest-sha256-prefix>`. Supply `--tag` to choose another name. Use a trusted base built from this checkout; the builder checks its CLI version. Deploy the resulting immutable image ID or registry digest. Changing the module set changes the default tag and the `org.maf-sandbox.bicep.manifest-sha256` label. Changes to preparation or configuration require a new image revision even if the module manifest is unchanged.

The dependency [manifest](dependencies.bicep-avm.json) locks each version to its OCI manifest SHA-256. Preparation compares the restored manifest with that pin, then checks the compiled template and optional source archive against the manifest's layer digests and sizes. It refuses changed artifacts. A separate build step loads every selected module with `--no-restore` and networking disabled. Python is used only in the preparation stage. The final image contains `/opt/maf-bicep/cache` and `/opt/maf-bicep/dependencies.json`, a receipt recording the CLI version, policy and manifest fingerprints, module pins and cached file hashes. Cache files have their write bits removed; the backend's filesystem boundary still supplies enforcement.

Pass the [prepared configuration](prepared.bicepconfig.json) explicitly as the existing factory's `config` argument, together with `egress=Egress.CLOSED`. For a host running from this checkout:

```python
from pathlib import Path

from maf_sandbox import Egress
from maf_sandbox_bicep import make_bicep_tools

tools = make_bicep_tools(
    router, file_store, "validator", context,
    image=prepared_image,
    egress=Egress.CLOSED,
    config=Path("images/bicep-sandbox/prepared.bicepconfig.json").read_text(encoding="utf-8"),
)
```

An installed host can copy that JSON into its own configuration. Hosts with an existing policy should retain it, add `"cacheRootDirectory": "/opt/maf-bicep/cache"` at the top level and set `analyzers.core.rules.use-recent-module-versions.level` to `"off"`. No image marker overrides host policy. The kind stages the supplied config in every fresh call directory, so nested templates and parameter files find the same cache despite `HOME="$PWD"`. Without that config, the kind uses its packaged policy and the empty per-call cache, even when the image is prepared.

All phases retain `--no-restore`. An unbaked module or version still reports BCP190 and `MODULE RESTORE FAILED`, with an incomplete result and no verdict. The version-currency linter is disabled because it needs the online module index; reviewing and updating the pinned policy owns currency for this profile. The image and its build-time registry artifacts remain part of the host's trusted compiler supply chain; closed runtime egress alone does not establish their provenance.

To change the selected modules, edit the policy, regenerate the manifest, review its pins and exclusions, then rebuild under a new revision:

```bash
uv run python scripts/bicep_dependencies.py lock
```

Locking reaches MCR. Normal image builds use the committed lock and never refresh it automatically. The floating Azure Linux base and its package repositories mean the image is not byte-for-byte reproducible; the receipt pins the module content, not the whole operating system.

For full tool-call verification, set `MAF_BICEP_PREPARED_IMAGE` to the built image ID or tag and run `uv run pytest -q tests/test_bicep_prepared_offline.py`. The tests inspect Docker's `NetworkMode=none` and cover clean network/storage templates, module parameter errors, an unbaked version, omitted cache config, a custom host rule, nested sources, parameter files and repeated calls in one sandbox. The existing Docker live workflow builds this profile and runs those checks after merge and on its daily schedule. These checks do not establish an ACAS import or WSLC runtime result.

## Push it to a registry

In the registry, with no local container runtime at all:

```bash
az acr build --registry <name> --image bicep-sandbox:0.46.1-1 images/bicep-sandbox
```

Or build locally and push:

```bash
az acr login --name <name>
docker build -t <name>.azurecr.io/bicep-sandbox:0.46.1-1 images/bicep-sandbox
docker push <name>.azurecr.io/bicep-sandbox:0.46.1-1
```

Tag `<bicep-version>-<revision>`, and never `latest`. That tag is what `BICEP_SANDBOX_IMAGE` names and what the disk image is derived from, so a moving tag turns "which compiler produced this diagnostic" into a question nobody can answer afterwards.

The revision is the half people leave off, and leaving it off is what [#308](https://github.com/sokolaidev/maf-extensions/issues/308) was. Everything in this image except the CLI can change while the CLI stays put — `bicepconfig.json`, the path it sits at, the base layer — so the Bicep version alone does not identify a build. Start at `-1` and bump it on any change that is not a CLI upgrade; a CLI upgrade resets it:

| Change | Tag |
|---|---|
| Bicep 0.46.1, first build | `bicep-sandbox:0.46.1-1` |
| the config, or the path it is copied to, changes | `bicep-sandbox:0.46.1-2` |
| Bicep upgraded to 0.47.0 | `bicep-sandbox:0.47.0-1` |

**Never overwrite a tag that has been imported.** A disk image retains the old snapshot when its source tag changes. The repository's import script refuses a reference already present in the group with `Nothing imported` and exit 1; it does not compare registry contents. If several snapshots share a reference after imports through the portal or the `aca` CLI, `resolve_disk_image_id` rejects an uncached lookup and names their ids. Pin the intended disk-image id explicitly, or push and import a new revision tag. Successful resolutions are cached for the process lifetime, so restart the host after changing imports or its image configuration.

## Import it into the sandbox group

A sandbox does not boot from the registry. It boots from a **disk image** registered in the sandbox group, which is a different namespace, so a pushed image has to be imported once before anything can resolve it by reference at run time. This is the step that gets people stuck: the push succeeds, the sample is configured correctly, and the sandbox still cannot be created.

The vendor CLI is the short path, and it needs no Python and nothing from this repository:

```bash
curl -fsSL https://aka.ms/aca-cli-install | sh                       # PowerShell: irm https://aka.ms/aca-cli-install-ps | iex
export ACA_SUBSCRIPTION=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_REGION=<region>
aca sandboxgroup disk create --group <group> --image <name>.azurecr.io/bicep-sandbox:0.46.1-1 --name bicep-sandbox-0-46-1-1 \
  --username 00000000-0000-0000-0000-000000000000 --token "$(az acr login --name <registry> --expose-token --query accessToken -o tsv)"
```

Scope comes from the environment rather than from flags — the resource group is the sandbox group's, not the registry's. The region is the one that is easy to miss, because nothing else in this document needs it: leave it out and the CLI stops with `Region required for data plane operations` before it reaches the service at all. Both the CLI and the service are in preview and Microsoft says the command surface may change, so `aca sandboxgroup disk create --help` is the authority if a flag or a variable name here does not match — the above is `aca 1.0.0-preview.1`, which reads `ACA_SUBSCRIPTION` rather than the `AZURE_SUBSCRIPTION_ID` the rest of this project uses.

**Authenticate the pull with a username and token, not with a managed identity.** `--identity <managed-identity-resource-id>` is the flag you would reach for, and against this project's own deployment it does not work: the service answers `RegistryAuthFailed` 401 asking for `registryCredentials` or a `managedIdentityClientId`, and supplying the latter directly in the request body returns the same 401. That is not a missing prerequisite. It was measured with the identity attached to the sandbox group *and* holding `AcrPull` on the registry, which was in classic permissions mode — both halves of the requirement below satisfied — and the same 401 comes back from the vendor CLI and from this repository's `import_disk_image.py` alike. Why the service rejects it is unresolved.

The token above is what works instead. `az acr login --expose-token` warns that it hands back a refresh token rather than an access token; the import accepts it regardless. Its short life is not a problem, because the pull happens once while the disk image is being built and never again when a sandbox boots from it.

The portal is the third way, and the one that needs nothing installed: [sandboxes.azure.com](https://sandboxes.azure.com) → your sandbox group → **Disk Images** → **Create** takes the same OCI reference in **Base Image URL**, with **Registry Authentication** set to a username and token or a managed identity for a private registry like this one. It also states plainly what the flag list does not: a disk image is a snapshot, and changing the source tag afterwards does not touch disk images already created.

If you would rather not install the CLI, this repository ships a script with explicit scope arguments — see [`packages/maf-sandbox-acas/scripts/README.md`](../../packages/maf-sandbox-acas/scripts/README.md). It accepts `--username` with `--token` or `--token-stdin` and prints the new disk-image id after a successful import. A reference already imported is refused with exit 1 and no id on stdout, even if a different `--name` is supplied. Serialize imports for the same group and reference because the listing check and creation are separate service operations.

Whichever route you take, an identity doing the pull has to be attached to the sandbox group and hold `Container Registry Repository Reader` on a registry in RBAC + ABAC permissions mode, or `AcrPull` on a classic-mode one — `az acr show --query roleAssignmentMode` tells you which. Satisfying both is necessary and, on the evidence above, not sufficient, so treat it as the floor rather than the fix: a private registry answers an unauthenticated pull by failing the import rather than the run.

Then point the sample at it — `ACAS_SANDBOX_REGISTRY=<name>.azurecr.io` and `BICEP_SANDBOX_IMAGE=bicep-sandbox:0.46.1-1`. The backend qualifies the bare reference with the registry and resolves it to the imported disk image at acquire time.

## What it may reach at run time

Nothing in this image needs the network to start; the CLI is already inside it. By default, validation uses Deny-default egress with exactly four hosts allowed — `mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms` and `live-data.bicep.azure.com` — fixed in `bicep_sandbox_spec`. Those four are what module restore needs; ARM is not among them. A host can instead choose `Egress.CLOSED`, using local modules or the prepared profile's baked pins and explicit cache configuration.

Build time is a different question and a different machine: the `Dockerfile` downloads the CLI from `github.com`, which the sandbox never does. Adding anything to this image that needs a fifth host at run time will fail closed, which is the intended direction of that failure.

## Changing the rule set

Edit the [package's `bicepconfig.json`](../../packages/maf-sandbox-bicep/src/maf_sandbox_bicep/bicepconfig.json) to change the default policy staged by the Bicep kind. A host can instead pass its own JSON text as `config` at attachment. The packaged policy retains two overrides: `no-unused-params` is `error`, and `use-recent-api-versions` is `warning` with `maxAgeInDays: 730`. [Catalog maintenance](../../docs/maintainers.md#updating-bicep-diagnostic-catalogs) describes the automated proposals for new rules and compiler codes.

The image's [`bicepconfig.json`](bicepconfig.json) remains a fallback for older clients. Changing that fallback requires rebuilding, pushing and importing under a new image revision. A packaged policy change requires updating the Python package and needs no image rebuild.

## Reproducibility

The Bicep CLI is pinned by release tag and its asset does not move. The base image is not: `3.0` advances as Azure Linux is patched, so two builds a month apart are not byte-identical. Pin the base by digest if you need them to be.
