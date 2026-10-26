# OpenTofu with Azure Verified Modules

`opentofu-avm` contains OpenTofu and a pinned AVM virtual-network graph for offline initialization and validation through `opentofu_validate`. It runs no plan or apply and needs no Azure credentials.

## Contents and compatibility

The [network policy](dependencies.opentofu-avm-network.policy.json) and [generated manifest](dependencies.opentofu-avm-network.json) select AVM virtual-network 0.22.2, interfaces 0.6.0, AzAPI 2.12.0, modtm 0.4.0 and random 3.7.2 for Linux amd64. The [engine manifest](image.json) pins OpenTofu 1.13.1 and its source-build dependencies. The provider identities use `registry.opentofu.org`; Terraform locks are not interchangeable with this mirror. Upstream provider archives are checksum-verified, unpacked and linked into each call. Their license notices and module licenses remain in the image.

[AVM does not test or guarantee OpenTofu compatibility](https://github.com/Azure/Azure-Verified-Modules/discussions/1512). Compatibility evidence applies to the versions and cases tested here. A successful initialization probe shows dependency loading; it does not prove every module input or Azure deployment works.

The image resolves only baked module versions and providers. Missing modules, incompatible constraints and bad locks produce incomplete validation without downloads. OpenTofu `.tofu` and `.tofu.json` files take precedence over their `.tf` and `.tf.json` counterparts during generation, preparation and runtime module discovery. Override files use the same selected configuration.

## Build and validate

Run from the repository root after `uv sync --locked`. Generation fetches public registry metadata and commit archives; set `GITHUB_TOKEN` to avoid unauthenticated GitHub rate limits.

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.opentofu-avm-network.policy.json --output images/terraform-sandbox/dependencies.opentofu-avm-network.json --check
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile builtin --tag maf-opentofu:avm-base
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-opentofu:avm-base --build-arg MANIFEST=dependencies.opentofu-avm-network.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-opentofu:avm-network images/terraform-sandbox
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-opentofu:avm-base --build-arg MANIFEST=dependencies.opentofu-avm-network.json -f images/terraform-sandbox/prepared.Dockerfile --target prepared --output type=local,dest=dist/dependencies/opentofu-avm images/terraform-sandbox
MAF_AVM_ENGINE=opentofu MAF_OPENTOFU_AVM_IMAGE=maf-opentofu:avm-network MAF_OPENTOFU_AVM_DIR=dist/dependencies/opentofu-avm uv run pytest -q tests/test_terraform_avm_offline.py
```

The build verifies the prepared file inventory and runs offline probes for every provider rank and root package. The test suite exercises a pinned module, a version constraint, a local wrapper, a matching readonly lock, `.tofu` precedence, invalid inputs, unavailable dependencies and a wrong lock. It checks network confinement, source nonmutation and container disposal. Hosts select `engine="opentofu"` and this image when constructing `make_terraform_tools`; deploy by immutable digest.

The release profile uses this small graph. Its runtime qualification binds the image receipt to the manifest and checks validation, nested module loading, provider links, unchanged sources and expected refusals. The security workflow requires provider Go inventories in the SBOM and applies the normal vulnerability gate. Build success alone is not security approval or publication.

## Broader catalogs

The optional [catalog policy](dependencies.opentofu-avm.policy.json) selects Azure `avm-res-`, `avm-ptn-` and `avm-utl-` modules through the OpenTofu catalog API. It is an operator input for a separate image, not the contents of the `opentofu-avm` release profile.

```sh
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.opentofu-avm.policy.json --output dist/opentofu-avm-catalog.json
```

A local generation trial resolved 149 module packages and 24 provider versions, with three exclusions: VM replication 0.1.0 had an unavailable source archive, automation account 0.2.0 exceeded the archive-expansion bound, and Kusto cluster 0.2.0 requested an unapproved `hashicorp/modtm` provider identity. These are generation observations, not offline initialization or validation results for that catalog.

Review the generated module/provider inventory and `excluded` reasons, then build it with the prepared-image recipe. Every included root must pass offline initialization. An engine feature or version incompatibility found during the build needs a documented policy exclusion and regeneration. Module validation requires representative inputs and separate tests; the generator does not establish whole-catalog compatibility. Catalog discovery uses `api.opentofu.org`; version and download resolution use `registry.opentofu.org`, whose module download endpoint returns a JSON location. All module archives remain pinned to immutable commits.

## Qualification and release status

Local Linux amd64 builds for [#2182](https://github.com/sokolaidev/maf-extensions/issues/2182) passed both offline initialization probes for the two baked module packages. Docker reported an image size of 869,957,496 bytes (about 830 MiB). The public-tool Docker suite passed 13 tests; 11 ACAS tests were skipped because ACAS was not configured. Five release-qualification probes passed against the final local image, checking exact and compatible versions, a matching readonly lock, an unavailable version and a bad lock. Tampering with a prepared module caused the image build to refuse the file inventory. The provider inventory consists of the three upstream provider archives pinned in the network manifest; a vulnerability scan has not yet qualified those bytes.

No published version, registry digest, hosted CI result, live ACAS result or deployment compatibility is claimed here. The broader catalog is not runtime-qualified by the network profile. ACAS execution uses `MAF_OPENTOFU_AVM_ACAS_IMAGE` and the existing ACAS environment only when separately configured; skipped tests provide no ACAS evidence.
