# `bicep-sandbox` — the image `bicep_validate` runs in

Two layers on Azure Linux: a pinned Bicep CLI, and a [`bicepconfig.json`](bicepconfig.json) at `/maf-sandbox/work`. That is the whole image. It carries no agent code, no Python, and nothing of the host application — the sandbox runs a compiler and nothing else, and *what* to compile arrives at run time as files the tool writes in.

Both samples run this one image. [`samples/01_acas_bicep`](../../samples/01_acas_bicep/) boots it as a disk image in an Azure Container Apps sandbox group; [`samples/02_wslc_bicep`](../../samples/02_wslc_bicep/) runs it as a local container under `wslc`. Sharing it is deliberate: the two samples exist to show that only the backend changes, and they would not be comparable if each validated against a compiler of its own.

## What is in it

| | Why |
|---|---|
| `mcr.microsoft.com/azurelinux/base/core:3.0` | A small Microsoft-maintained base with `tdnf`. Nothing in the tool depends on the distribution — it runs `bicep` and reads SARIF back |
| `icu` | Without it the CLI aborts at startup: `Couldn't find a valid ICU package`. It is not optional for a .NET single-file binary unless you set the invariant-globalization switch |
| `ca-certificates` | Module restore is HTTPS to MCR. Without them every `br/public:` reference fails to restore |
| Bicep CLI, pinned to `v0.46.1` | The pin is the point. Diagnostic wording, built-in rule levels and the API-version cut-off all follow the compiler, so an unpinned image would let a sample's documented output drift underneath it |
| `bicepconfig.json` at `/maf-sandbox/work` | The lint rule set, at the one path Bicep will look for it |

## The config path is the fragile part

`/maf-sandbox/work` is not a convention — it is `maf_sandbox_bicep`'s `_WORK_DIR`, the root its `SandboxSpec` fixes for every validation. Bicep resolves `bicepconfig.json` **only** by walking up from the source file, and the pinned CLI has no `--config-file` flag on either `build` or `lint`. The tool writes each round into a fresh subdirectory of that root — `/maf-sandbox/work/<round>/main.bicep` — so the walk up finds the config in a single step.

Put the file anywhere else and nothing goes red. Measured against this image, on sample 01's `main.bicep`:

| | Compiled under `/maf-sandbox/work` | Compiled elsewhere |
|---|---|---|
| `no-unused-params` | `"level": "error"` | no `level` at all — the rule's built-in default, `warning` |
| `use-recent-api-versions` | reported, with the age in days | **absent** — the config is what switches it on |
| SARIF | parses, diagnostics render | parses, diagnostics render |

Both runs look entirely healthy. The second is simply linting against a weaker rule set than the repository asked for, and the only visible tell is a severity one step lower than it should be — which is why both samples' READMEs tell you to read that severity, and why `TestConfigDiscovery` in `maf-sandbox-bicep` reaches out of the package to read this `Dockerfile` and assert its `COPY` line against the published `_WORK_DIR` constant. The image and the tool cannot drift apart without a test noticing.

## Build

From the repository root, so the build context is this directory:

```bash
wslc build -t bicep-sandbox:local images/bicep-sandbox
```

That is sample 02, and it is the whole story there — `wslc` runs what is already on the machine, so there is nothing to push and nothing to import. Docker and podman take the same arguments (`docker build -t bicep-sandbox:0.46.1 images/bicep-sandbox`).

Everything below is sample 01: getting the same image into an Azure sandbox group, which takes two more steps than people expect.

## Push it to a registry

In the registry, with no local container runtime at all:

```bash
az acr build --registry <name> --image bicep-sandbox:0.46.1 images/bicep-sandbox
```

Or build locally and push:

```bash
az acr login --name <name>
docker build -t <name>.azurecr.io/bicep-sandbox:0.46.1 images/bicep-sandbox
docker push <name>.azurecr.io/bicep-sandbox:0.46.1
```

Tag with the Bicep version rather than `latest`. That tag is what `BICEP_SANDBOX_IMAGE` names and what the disk image is derived from, so a moving tag turns "which compiler produced this diagnostic" into a question nobody can answer afterwards.

## Import it into the sandbox group

A sandbox does not boot from the registry. It boots from a **disk image** registered in the sandbox group, which is a different namespace, so a pushed image has to be imported once before anything can resolve it by reference at run time. This is the step that gets people stuck: the push succeeds, the sample is configured correctly, and the sandbox still cannot be created.

The vendor CLI is the short path, and it needs no Python and nothing from this repository:

```bash
curl -fsSL https://aka.ms/aca-cli-install | sh                       # PowerShell: irm https://aka.ms/aca-cli-install-ps | iex
export AZURE_SUBSCRIPTION_ID=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_SANDBOX_GROUP=<group>
aca sandboxgroup disk create --image <name>.azurecr.io/bicep-sandbox:0.46.1 --name bicep-sandbox-0-46-1
```

Scope comes from the environment rather than from flags — the resource group is the sandbox group's, not the registry's. A private registry additionally needs the pull authenticated, which this project's deploys do with `--identity <managed-identity-resource-id>`. Both the CLI and the service are in preview and Microsoft says the command surface may change, so `aca sandboxgroup disk create --help` is the authority if a flag here does not match.

The portal is the third way, and the one that needs nothing installed: [sandboxes.azure.com](https://sandboxes.azure.com) → your sandbox group → **Disk Images** → **Create** takes the same OCI reference in **Base Image URL**, with **Registry Authentication** set to a username and token or a managed identity for a private registry like this one. It also states plainly what the flag list does not: a disk image is a snapshot, and changing the source tag afterwards does not touch disk images already created.

If you would rather not install the CLI, this repository ships the equivalent as a script with explicit arguments instead of environment variables — see [`packages/maf-sandbox-acas/scripts/README.md`](../../packages/maf-sandbox-acas/scripts/README.md). It is idempotent, and it prints the resolved disk-image id.

Either way the identity doing the pull has to be attached to the sandbox group and hold `Container Registry Repository Reader` on a registry in RBAC + ABAC permissions mode, or `AcrPull` on a classic-mode one. Without it a private registry answers the pull with a 403 and the import fails rather than the run.

Then point the sample at it — `ACAS_SANDBOX_REGISTRY=<name>.azurecr.io` and `BICEP_SANDBOX_IMAGE=bicep-sandbox:0.46.1`. The backend qualifies the bare reference with the registry and resolves it to the imported disk image at acquire time.

## What it may reach at run time

Nothing in this image needs the network to start; the CLI is already inside it. Once a validation runs, egress is Deny-default with exactly four hosts allowed — `mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms` and `live-data.bicep.azure.com` — fixed in `bicep_sandbox_spec` rather than left to configuration, because a deployment that could widen them could undo the containment the whole design rests on. Those four are what module restore needs and no more; ARM is not among them.

Build time is a different question and a different machine: the `Dockerfile` downloads the CLI from `github.com`, which the sandbox never does. Adding anything to this image that needs a fifth host at run time will fail closed, which is the intended direction of that failure.

## Changing the rule set

[`bicepconfig.json`](bicepconfig.json) is the rule set both samples report against, so editing it changes their output. Two rules are deliberately away from their defaults: `no-unused-params` is raised to `error`, because that severity is the samples' visible proof the config was discovered at all, and `use-recent-api-versions` is switched on with `maxAgeInDays: 730`. Rebuild, push and import under a new tag afterwards — a disk image already imported does not change when the tag it came from is overwritten.

## Reproducibility

The Bicep CLI is pinned by release tag and its asset does not move. The base image is not: `3.0` advances as Azure Linux is patched, so two builds a month apart are not byte-identical. Pin the base by digest if you need them to be.
