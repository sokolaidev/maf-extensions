# Building and using the Terraform and OpenTofu images

Steps and commands only. [README.md](README.md) explains what each step does and why.

Run every command from the repository root, after `uv sync --locked`.

## TL;DR

Base engine image, with no baked modules and no providers:

```sh
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin
```

Use `--engine opentofu` for OpenTofu, or `--profile random` to add the `random` provider.

Terraform image with the Azure Verified Modules catalog baked in:

```sh
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin && docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin --build-arg MANIFEST=dependencies.terraform-avm.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:1.16.2-avm-1 images/terraform-sandbox
```

The build downloads about 540 MB and produces an image of about 3.4 GB.

## Build an engine image

Use the builder, not `docker build`.

```sh
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile builtin
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile random
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile random
```

The default tags are `maf-terraform:1.16.2-builtin`, `maf-terraform:1.16.2-random`, `maf-opentofu:1.12.6-builtin` and `maf-opentofu:1.12.6-random`. `--tag` sets another tag.

Read an image's engine labels:

```sh
docker image inspect maf-terraform:1.16.2-builtin --format '{{json .Config.Labels}}'
```

To upgrade an engine, change its version, URL and digest in [image.json](image.json), then rebuild.

To add a provider profile, put its manifest next to `image.json`, map the profile name to that file name in `image.json`, then build with `--profile <name>`.

## Try an image

```sh
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-builtin
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:1.12.6-builtin
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-random --provider
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:1.12.6-random --provider
```

## Build a prepared image

1. Build the `builtin` image of the same engine from this checkout.
2. Build [prepared.Dockerfile](prepared.Dockerfile) from `images/terraform-sandbox`, naming the manifest. Preparation runs inside the build.
3. Export the prepared directory with `--target prepared`, and try the image with it.

```sh
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin --build-arg MANIFEST=dependencies.terraform.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:1.16.2-prepared images/terraform-sandbox
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin --build-arg MANIFEST=dependencies.terraform.json -f images/terraform-sandbox/prepared.Dockerfile --target prepared --output type=local,dest=dist/dependencies/terraform images/terraform-sandbox
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:1.16.2-prepared --prepared dist/dependencies/terraform
```

For OpenTofu, use `maf-opentofu:1.12.6-builtin`, `dependencies.opentofu.json` and `--engine opentofu`.

Keep the exported directory in trusted storage. To rebuild from it without preparing again, add `--build-context prepared=<directory>` and drop `MANIFEST`. Deploy by image ID or digest, not by tag.

## Build an image with Azure Verified Modules

Terraform only. [dependencies.terraform-avm.policy.json](dependencies.terraform-avm.policy.json) is the whole catalog. [dependencies.terraform-avm-network.policy.json](dependencies.terraform-avm-network.policy.json) is the small graph CI builds; the steps are the same with its file names.

### 1. Change the policy

- To approve a provider, add it to `providers` with a version bound.
- To bake a module outside the catalog, add it to `registry_modules` with a version constraint.
- To leave a catalog module out, add it to `catalog.exclude` with a reason.

### 2. Generate the manifest

Set `GITHUB_TOKEN` first to avoid GitHub's rate limit. The catalog takes about 3 minutes.

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.terraform-avm.policy.json --output images/terraform-sandbox/dependencies.terraform-avm.json
```

Check that the committed manifest still matches the policy:

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.terraform-avm.policy.json --output images/terraform-sandbox/dependencies.terraform-avm.json --check
```

### 3. Review the change

Review the manifest diff, and read the module code at each new commit. Read the `excluded` list too: each entry is a root that did not bake, with the reason.

To check a module pin by hand:

1. `curl -sI https://registry.terraform.io/v1/modules/<namespace>/<name>/<system>/<version>/download` returns `X-Terraform-Get` with `?ref=<commit>`. Check that the repository tag for that version names the same commit.
2. Download `https://codeload.github.com/<owner>/<repository>/zip/<commit>`, review it, and compare its SHA-256 with the pin.
3. Read every module directory the root loads, and compare its calls with `graph`.

To check a provider pin by hand, compare its digest with the release `SHA256SUMS` and with the registry's download metadata.

### 4. Build

Tag with the engine version, the profile and a revision you never reuse.

```sh
uv run python images/terraform-sandbox/build_image.py --engine terraform --profile builtin
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin --build-arg MANIFEST=dependencies.terraform-avm.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:1.16.2-avm-1 images/terraform-sandbox
```

Export the prepared directory to keep it, or to run the checks:

```sh
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-terraform:1.16.2-builtin --build-arg MANIFEST=dependencies.terraform-avm.json -f images/terraform-sandbox/prepared.Dockerfile --target prepared --output type=local,dest=dist/dependencies/terraform-avm images/terraform-sandbox
```

If an offline probe fails, the build prints that root's `init` output. Add the module to `catalog.exclude` with a reason, then regenerate and rebuild.

### 5. Import it for ACAS

Push the tag, then create a disk image from it, as the [Bicep image guide](../bicep-sandbox/README.md#import-it-into-the-sandbox-group) describes. Never overwrite an imported tag; bump the revision instead.

```sh
docker tag maf-terraform:1.16.2-avm-1 <registry>.azurecr.io/maf-terraform:1.16.2-avm-1
docker push <registry>.azurecr.io/maf-terraform:1.16.2-avm-1
export ACA_SUBSCRIPTION=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_REGION=<region>
aca sandboxgroup disk create --group <group> --image <registry>.azurecr.io/maf-terraform:1.16.2-avm-1 --name maf-terraform-1-16-2-avm-1 --username 00000000-0000-0000-0000-000000000000 --token "$(az acr login --name <registry> --expose-token --query accessToken -o tsv)"
```

Configure the ACAS backend with that registry and pass `image="maf-terraform:1.16.2-avm-1"` to `make_terraform_tools`.

## Check an image

[README.md](README.md#verification) says what each suite proves.

- Package tests: `uv run pytest -q packages/maf-sandbox-terraform`
- Preparation: `uv run pytest -q tests/test_terraform_dependencies.py`
- Launcher module records: `uv run pytest -q tests/test_terraform_runner_modules.py`
- Custom provider profiles, built live: set `MAF_IMAGE_BUILD_TESTS=1`, then `uv run pytest -q tests/test_terraform_image_build.py -k live_build`
- Engine images on Docker: set `MAF_TERRAFORM_E2E_IMAGE` and `MAF_OPENTOFU_E2E_IMAGE` to the two `random` images, then `uv run pytest -q packages/maf-sandbox-terraform/tests/test_terraform_docker.py`
- Prepared images on Docker: set `MAF_TERRAFORM_PREPARED_IMAGE` and `MAF_OPENTOFU_PREPARED_IMAGE` to image IDs, and `MAF_TERRAFORM_PREPARED_DIR` and `MAF_OPENTOFU_PREPARED_DIR` to their exported prepared directories, then `uv run pytest -q tests/test_terraform_dependencies_docker.py`
- AVM image, catalog or network: set `MAF_TERRAFORM_AVM_DIR` to the exported prepared directory, then `uv run pytest -q tests/test_terraform_avm_offline.py`
  - For Docker, also set `MAF_TERRAFORM_AVM_IMAGE` to the local image.
  - For ACAS, also set `MAF_TERRAFORM_AVM_ACAS_IMAGE` to the imported `repository:tag`, and `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP` and `ACAS_SANDBOX_REGISTRY`. Each ACAS call creates one billable sandbox.
