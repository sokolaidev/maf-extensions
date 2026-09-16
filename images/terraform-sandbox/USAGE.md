# Building and using the Terraform and OpenTofu images

This page shows how to build, try, check and deploy the validation images. [README.md](README.md) describes what an image contains and the rules its inputs follow.

Run every command from the repository root, after `uv sync --locked`. Every image targets Linux amd64 and holds one engine.

## Build an engine image

Use the Python builder. It reads the pins in [image.json](image.json) and passes the matching build arguments, so do not supply Docker build arguments by hand.

```sh
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile builtin
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile random
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile random
```

The `builtin` profile has no providers. It supports built-in resources and local modules. The `random` profile adds the provider pinned in `dependencies.terraform.json` or `dependencies.opentofu.json`.

The default tag names the engine version and the profile: `maf-terraform:1.16.2-builtin`, `maf-terraform:1.16.2-random`, `maf-opentofu:1.12.6-builtin` and `maf-opentofu:1.12.6-random`. `--tag` sets another tag. It does not change the binary version.

To see which engine and version an image holds, read its labels:

```sh
docker image inspect maf-terraform:1.16.2-builtin --format '{{json .Config.Labels}}'
```

To upgrade an engine, change its version, URL and digest in `image.json`, then rebuild. To add a provider profile, put its manifest next to `image.json` and map the new profile name to that file name. The builder copies the selected manifest into the image.

## Try an image

The example uses the same factory a host attaches to its agent. It calls no model, needs no cloud credentials and deploys nothing.

```sh
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-builtin
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:1.12.6-builtin
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-random --provider
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:1.12.6-random --provider
```

## Build a prepared image

A prepared image gets its providers and modules from [terraform_dependencies.py](../../scripts/terraform_dependencies.py), not from the installer. Only the preparer enforces the request restrictions that [README.md](README.md#approved-dependency-preparation) describes.

1. Prepare the dependencies from a manifest. The output directory must not exist yet.
2. Build [prepared.Dockerfile](prepared.Dockerfile) with `--network none`, on a trusted `builtin` image of the same engine.
3. Try the result, passing the prepared directory to the example.

```sh
uv run python scripts/terraform_dependencies.py --manifest images/terraform-sandbox/dependencies.terraform.json --output dist/dependencies/terraform
uv run python scripts/terraform_dependencies.py --manifest images/terraform-sandbox/dependencies.opentofu.json --output dist/dependencies/opentofu
docker build --platform linux/amd64 --network none --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:1.16.2-prepared dist/dependencies/terraform
docker build --platform linux/amd64 --network none --build-arg BASE_IMAGE=maf-opentofu:1.12.6-builtin -f images/terraform-sandbox/prepared.Dockerfile -t maf-opentofu:1.12.6-prepared dist/dependencies/opentofu
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-prepared --prepared dist/dependencies/terraform
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:1.12.6-prepared --prepared dist/dependencies/opentofu
```

Keep the prepared directory in trusted artifact storage. The tags above are local conveniences; deploy by image ID or digest.

## Build an image with Azure Verified Modules

AVM images are Terraform only. [dependencies.terraform-avm.json](dependencies.terraform-avm.json) pins their modules and providers, and it is generated from [dependencies.terraform-avm.policy.json](dependencies.terraform-avm.policy.json). Nothing moves to a newer release by itself. To take one, change the policy and regenerate.

### 1. Change the policy

The policy names only what a human decides: provider addresses, and registry module sources with version constraints. The generator pins the newest release each constraint admits, and never a prerelease. Every constraint in the policy today is an exact `=` pin, so a refresh edits the version.

If a new module version calls a registry module the policy does not list, generation refuses it. Add that module to the policy.

### 2. Generate the manifest

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.terraform-avm.policy.json --output images/terraform-sandbox/dependencies.terraform-avm.json
```

[terraform_manifest.py](../../scripts/terraform_manifest.py) resolves every pin: exact versions, artifact URLs and digests cross-checked against the release `SHA256SUMS` the registry names, tag-to-commit revisions, module call graphs including nested registry dependencies, and GitHub repository ids. It refuses a registry call the policy does not list, a call that does not pin a version, and a constraint the pinned version fails. It writes nothing until a dry run of the real preparer has proven every pin against downloaded bytes, and then prints which pins moved. Set `GITHUB_TOKEN` to avoid GitHub's unauthenticated rate limit.

Add `--check` to fail instead of writing when the committed manifest no longer matches the policy. That suits a CI guard.

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.terraform-avm.policy.json --output images/terraform-sandbox/dependencies.terraform-avm.json --check
```

The `random` profile manifests are not generated. Each pins one provider beside its engine version and changes only when the engine does.

### 3. Review the change

Generation is deterministic, so the manifest diff is the review. Generated provenance records where each value came from and carries no date; the commit does. The preparer verifies bytes, not signatures, so read the module code at each new commit before you trust it.

To check a module pin by hand:

1. `curl -sI https://registry.terraform.io/v1/modules/<namespace>/<name>/<system>/<version>/download` returns `X-Terraform-Get` with `?ref=<commit>`. Check that the repository tag for that version names the same commit.
2. Download `https://codeload.github.com/<owner>/<repository>/zip/<commit>`, review it, and compare its SHA-256 with the pin.
3. Read every module directory the root loads, and compare its calls with `graph`.

To check a provider pin by hand, compare its digest with the release `SHA256SUMS` and with the registry's download metadata.

### 4. Prepare and build

Build from a trusted `builtin` base without network. Tag with the engine version, the profile and a revision you never reuse.

```sh
uv run python scripts/terraform_dependencies.py --manifest images/terraform-sandbox/dependencies.terraform-avm.json --output dist/dependencies/terraform-avm
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin
docker build --platform linux/amd64 --network none --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:1.16.2-avm-1 dist/dependencies/terraform-avm
```

The build fails unless offline `init` succeeds for every pinned provider and module.

### 5. Import it for ACAS

A sandbox boots from a disk image, not from the registry. Push the tag and import it with the `aca` CLI, as the [Bicep image guide](../bicep-sandbox/README.md#import-it-into-the-sandbox-group) describes. A disk image is a snapshot, so never overwrite an imported tag. Bump the revision instead.

```sh
docker tag maf-terraform:1.16.2-avm-1 <registry>.azurecr.io/maf-terraform:1.16.2-avm-1
docker push <registry>.azurecr.io/maf-terraform:1.16.2-avm-1
export ACA_SUBSCRIPTION=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_REGION=<region>
aca sandboxgroup disk create --group <group> --image <registry>.azurecr.io/maf-terraform:1.16.2-avm-1 --name maf-terraform-1-16-2-avm-1 --username 00000000-0000-0000-0000-000000000000 --token "$(az acr login --name <registry> --expose-token --query accessToken -o tsv)"
```

Configure the ACAS backend with that registry and pass `image="maf-terraform:1.16.2-avm-1"` to `make_terraform_tools`.

## Check an image

`uv run pytest -q packages/maf-sandbox-terraform` runs the deterministic package tests, which need neither Docker nor installed engine binaries.

`uv run pytest -q tests/test_terraform_dependencies.py` covers TLS receiver controls, policy refusals, artifact integrity, graph verification and archive bounds. The receiver accepts the negative controls under unrestricted requests before the preparer refuses them. DNS-private refusal tests are separate from the local TLS receiver, whose dial address and trust root are deliberately replaced by the test fixture. `uv run pytest -q tests/test_terraform_runner_modules.py` checks how the launcher reads module calls and builds records, without an engine.

Set `MAF_IMAGE_BUILD_TESTS=1` and run `uv run pytest -q tests/test_terraform_image_build.py -k live_build` to build and check custom provider profiles for both engines.

### Engine images on Docker

Set `MAF_TERRAFORM_E2E_IMAGE` and `MAF_OPENTOFU_E2E_IMAGE` to the two **random** profile images, then run `uv run pytest -q packages/maf-sandbox-terraform/tests/test_terraform_docker.py`. The suite runs real CLI calls, checks the daemon after each call, and also executes [test_runner.py](test_runner.py) inside each Linux image to exercise bounded pipes, deadlines, environment isolation, and lock behavior.

### Prepared images on Docker

Set `MAF_TERRAFORM_PREPARED_IMAGE`, `MAF_OPENTOFU_PREPARED_IMAGE` to immutable image IDs and `MAF_TERRAFORM_PREPARED_DIR`, `MAF_OPENTOFU_PREPARED_DIR` to the corresponding output directories, then run `uv run pytest -q tests/test_terraform_dependencies_docker.py`. This verifies both engines, provider/module initialization, correct and mismatched readonly locks, missing dependencies, source nonmutation, daemon-observed network mode and disposal. A separate Docker receiver accepts direct HTTP and raw CONNECT controls over bridge networking before the identical controls are denied inside CLOSED adapter sandboxes. This is Docker evidence; ACAS and WSLC have not been live-qualified for this preparation profile.

### The AVM image

The AVM graph has its own suite, [test_terraform_avm_offline.py](../../tests/test_terraform_avm_offline.py). Set `MAF_TERRAFORM_AVM_DIR` to the prepared output. For Docker, set `MAF_TERRAFORM_AVM_IMAGE` to the local image. For ACAS, set `MAF_TERRAFORM_AVM_ACAS_IMAGE` to the imported `repository:tag` and `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP` and `ACAS_SANDBOX_REGISTRY`. Each backend runs the same calls through the Terraform tool: exact and `~>` pins, a local wrapper module, a correct lock, and five failures that must render INCOMPLETE for their named cause. Every sandbox first proves it cannot fetch the registry discovery document. On Docker that is `--network none`; the Docker leg also checks the same probe succeeds on a bridge network. A Docker-only case runs the launcher inside the image against a provider-only root and proves `init` served the provider by links into `/opt/maf-terraform/mirror`, with no copied bytes. ACAS CLOSED denies at a TLS-terminating proxy: DNS and TCP succeed and the request returns 403, so the probe checks for retrieved content rather than a connection. Each ACAS call creates one billable sandbox that the router disposes. `terraform-live.yml` runs the Docker leg after merge and on a schedule; ACAS runs only when an operator supplies those values.
