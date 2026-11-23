# OpenTofu with Azure Verified Modules

The `opentofu-avm` release profile builds an offline Linux amd64 image for `opentofu_validate`. Its [catalogue policy](dependencies.opentofu-avm.policy.json) selects Azure resource, pattern and utility modules from the OpenTofu registry. The [generated manifest](dependencies.opentofu-avm.json) pins the module versions, commit archives, provider builds and exclusion reasons. The [network manifest](dependencies.opentofu-avm-network.json) is a development fixture for the smaller live test suite.

## Contents and compatibility

The catalogue contains 152 registry module packages across 120 module addresses, including nested dependencies and multiple versions. The mirror contains 21 provider versions, including all nine provider packages pinned by the network fixture. Plain Azure roots can use azurerm 4.x, azuread 3.x, local, null, time and tls. The image also retains the virtual-network 0.22.2 graph. The [engine manifest](image.json) pins OpenTofu 1.13.1.

The policy caps AzureRM at 5.10.x. The upstream ZIPs for 5.11.0, 5.12.0, 5.13.0 and 5.14.0 exceed the 64 MiB package limit. A local source build of 5.14.0 produced a 75,282,857-byte ZIP. The locked 5.10.0 package is 53,543,518 bytes and preserves the same module versions and dependency graphs.

The OpenTofu and Terraform catalogues resolve independently. The build shares 42 module archives with the same upstream commit URL and SHA-256 across both catalogues. Each engine verifies the bytes against its own manifest, selects its configuration files and prepares its own dependency graph. OpenTofu gives `.tofu` and `.tofu.json` files precedence. Each registry's provider identities, source builds, package hashes and preparation receipts stay with its engine. Source-built providers retain license notices and link from the unpacked mirror into each call. A supplied readonly lock needs the rebuilt provider's `h1` checksum from the preparation receipt.

Write module sources as `Azure/avm-…/azurerm` or qualify them with `registry.opentofu.org/`. Remove an explicit `registry.terraform.io/` prefix when adapting a Terraform root for this image. Registry module downloads are disabled during validation. Missing versions, excluded modules and incompatible locks return incomplete initialization.

At catalogue generation, the OpenTofu versions API returned HTTP 404 for 36 module addresses shipped in the Terraform image, including `Azure/avm-res-app-job/azurerm`. Removing the host qualifier lets the Log Analytics and managed identity modules resolve. The standalone Container Apps job module still requires a registry that carries it.

Of the 122 matching registry addresses, VM replication 0.1.0 returned HTTP 404 for its archive, and automation account 0.3.0 exceeded the preparation archive-expansion limit. Eight newer releases across five addresses require engine versions above 1.13.1. The policy records those versions individually and allows compatible releases of AVS private cloud, virtual machine, load balancer, private endpoint and Log Analytics workspace. The manifest lists each excluded version and its reason.

[AVM does not guarantee OpenTofu compatibility](https://github.com/Azure/Azure-Verified-Modules/discussions/1512). Each catalogue image build must initialize every included package with a root configuration under `--network=none`. A subdirectory-only dependency is exercised through the root that selects it. Each provider version must also load its schema. Release qualification validates a Log Analytics workspace with a managed identity, the pinned network graph and a plain Azure root. It checks registry and lock refusals, provider links and preservation of the supplied configuration. These probes require no Azure credentials and make no deployment.

## Build and validate

Run from the repository root. The same `avm-archives` build stage serves both release profiles. BuildKit can reuse it on the same builder. The hosted build can also restore its digest-named ZIP files from the cache shared by both profiles. Each preparation checks the digest again before consuming a cached archive.

To transfer those archives between builders, export the stage once. Add `--build-context avm-archives=dist/avm-archives` alongside `--build-arg MODULE_ARCHIVES=avm-archives` in either engine's catalogue build.

```sh
docker build --platform linux/amd64 --build-context scripts=scripts --target avm-archives --output type=local,dest=dist/avm-archives -f images/terraform-sandbox/prepared.Dockerfile images/terraform-sandbox
```

```sh
uv sync --locked
uv run python images/terraform-sandbox/build_image.py --engine opentofu --profile builtin --tag maf-opentofu:avm-base
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg BASE_IMAGE=maf-opentofu:avm-base --build-arg MODULE_ARCHIVES=avm-archives --build-arg MANIFEST=dependencies.opentofu-avm.json -f images/terraform-sandbox/prepared.Dockerfile -t maf-opentofu:avm images/terraform-sandbox
mkdir -p dist/opentofu-avm
docker image inspect --format '{"profile":"opentofu-avm","imageId":"{{.Id}}"}' maf-opentofu:avm > dist/opentofu-avm/candidate.json
uv run python scripts/check_container_release_image.py --candidate dist/opentofu-avm/candidate.json --directory dist/opentofu-avm
```

For a catalogue refresh, export the approved provider packages and verify every pin against the selected registry and commit archives. Set `GITHUB_TOKEN` for authenticated GitHub metadata. A new provider version requires a reviewed source-build lock. The [provider lock instructions](USAGE.md#build-an-image-with-azure-verified-modules) describe how to build proposed locks from upstream release commits.

```sh
docker build --platform linux/amd64 --build-context scripts=scripts --build-arg MANIFEST=dependencies.opentofu-avm.json -f images/terraform-sandbox/prepared.Dockerfile --target provider-packages --output type=local,dest=dist/provider-builds/opentofu-avm images/terraform-sandbox
uv run python scripts/terraform_manifest.py --policy images/terraform-sandbox/dependencies.opentofu-avm.policy.json --output images/terraform-sandbox/dependencies.opentofu-avm.json --provider-builds dist/provider-builds/opentofu-avm --check
```

Select a released image by immutable digest after its retained qualification includes the catalogue probes. The published network image and its prior evidence describe the earlier graph. Catalogue publication is tracked in [#2881](https://github.com/sokolaidev/maf-extensions/issues/2881).

The 2026-11-21 local Linux amd64 catalogue build passed 154 offline probes covering 151 module roots and three provider groups. All 11 release-consumer checks passed against that image. The `~> 0.22` network probe selected virtual network 0.23.0 with interfaces 0.10.0 and linked Azure/azapi and HashiCorp/random. The pinned 0.22.2 case linked three providers. The workspace and identity case linked five. Docker reported 2,754,160,240 bytes for the local image.

## Earlier network-image evidence

The [2026-11-09 local qualification record](../../tests/fixtures/image_dependencies/opentofu-azure-roots.json) binds the expanded image to its source inputs and preparation receipt. All nine offline release probes and 16 public-tool Docker tests passed, including valid and invalid plain `.tofu` Azure roots, unavailable providers, source preservation and cleanup. Fourteen ACAS cases were skipped. The six added providers were built from verified OpenTofu release commits, and the three existing provider hashes reproduced. This local result establishes neither a fresh vulnerability assessment nor hosted qualification, release approval, signing or publication.

Local Linux amd64 builds for [#2182](https://github.com/sokolaidev/maf-extensions/issues/2182) passed both offline initialization probes for the two baked module packages. Docker reported an image size of 869,957,496 bytes (about 830 MiB). The public-tool Docker suite passed 13 tests; 11 ACAS tests were skipped because ACAS was not configured. Five release-qualification probes passed against that local image, checking exact and compatible versions, a matching readonly lock, an unavailable version and a bad lock. Tampering with a prepared module caused the image build to refuse the file inventory. That candidate used upstream provider binaries; subsequent hosted scanning rejected them with High/Critical findings.

The [2026-10-27 remediation record](../../tests/fixtures/image_dependencies/opentofu-avm-remediation.json) covers the source-built replacement providers at the same versions. All three packages reproduced their locked hashes on a second build. Six offline release probes passed, including upstream-only lock refusal. The exact exported candidate passed the strict High/Critical gate with zero Critical/High, two Medium and seven Unknown matches, using Syft 1.54.0, Grype 0.120.0 and its 2026-10-27 database. Local qualification does not substitute for fresh hosted checks or release approval.
