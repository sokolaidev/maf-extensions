# Terraform AVM validation image

`terraform-avm` packages Terraform and a pinned Azure Verified Modules (AVM) catalogue for offline initialization and validation. It is intended for applications that validate generated Terraform using `maf-sandbox-terraform` with closed egress. It does not run `plan` or `apply`, deploy Azure resources, or need Azure credentials for validation.

## Availability and contents

The release profile is implemented; its first public release is tracked in [#1857](https://github.com/sokolaidev/maf-extensions/issues/1857). No published version or digest is claimed here. The intended repository is `ghcr.io/sokolaidev/maf-extensions/terraform-avm`, initially Linux/amd64. Use a locally built image until publication and independent consumer verification are complete.

The reviewed [catalogue manifest](dependencies.terraform-avm.json) contains 217 registry module entries, including nested dependencies and multiple versions, plus 25 provider entries. These counts describe entries, not 217 distinct root modules. The manifest's nine exclusions record unavailable or unapproved catalogue members. The [selection policy](dependencies.terraform-avm.policy.json) and [engine manifest](image.json) define the approved scope and Terraform version. The smaller `dependencies.terraform-avm-network.json` is a development fixture, not this release's catalogue.

The Terraform engine is built from its pinned upstream release commit with recorded security dependency updates; the [image identity guide](README.md#image-identity) describes its source-build evidence. The catalogue providers are SOKOLAI builds of pinned upstream release commits, using the manifest's patched Go toolchain and dependency overrides. Their package checksums differ from upstream release archives; these binaries are not upstream-distributed provider releases.

Modules, provider archives and source revisions are pinned and verified during preparation. The image retains `/opt/maf-terraform/dependencies.json`, containing the preparation receipt, and `/opt/maf-terraform/engine.json`, containing the verified engine identity. Providers are unpacked in the baked mirror; the launcher requires initialization to link them instead of copying them into each call. Nested registry modules use the receipt's prepared dependency graph. Missing modules or incompatible versions fail offline; the launcher does not silently fetch replacements.

## Use and verify

Build the catalogue using the [AVM build instructions](USAGE.md#build-an-image-with-azure-verified-modules). Select the resulting immutable image identity in your backend configuration and use the [Terraform validation kind](../../packages/maf-sandbox-terraform/README.md) with `Egress.CLOSED`. The application owns the backend image selection; installing the Python package does not download this image. The existing [validation sample](../../samples/20_terraform_validation/README.md) explains the host integration. Source modules keep their normal registry addresses and version constraints, for example:

```hcl
module "network" {
  source        = "Azure/avm-res-network-virtualnetwork/azurerm"
  version       = "0.22.2"
  location      = "westeurope"
  parent_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example"
  address_space = ["10.0.0.0/16"]
}
```

Check requested modules, nested versions and provider constraints against the catalogue before selecting this image. Supplied dependency locks must include the baked `h1:` checksum for each selected SOKOLAI provider; upstream `zh:` checksums alone are insufficient. The launcher verifies those hashes before validation. An upstream-only `.terraform.lock.hcl` is rejected when its checksums do not include the SOKOLAI-built package. Keep image-specific validation locks separate from deployment locks; review and generate the validation lock against this image's offline mirror. Do not remove checksum checks or replace production deployment locks merely to make validation pass. Validation checks configuration and provider schemas; it does not establish that a deployment will succeed or satisfy your application's policies. OpenTofu AVM use and execution through ACAS, WSLC or other backends are not established by this Docker release gate.

After a public release exists, select it from the [release catalogue](https://sokolaidev.github.io/maf-extensions/), verify its signed identity and completion using the [consumer verification procedure](../../docs/security/container-image-releases.md), and pin its digest. Check monitoring status separately from identity verification. A local image has no signed release completion and is not interchangeable with a published digest.

## Provider build identity

Each provider entry retains the official upstream archive metadata for comparison and has a `source_build` record with the source repository, exact commit and archive hash, Go version, dependency overrides, resolved `go.mod`/`go.sum` hashes and resulting ZIP hash. The release recipe rebuilds those packages and refuses a different hash; it never falls back to the official binary. Package timestamps and permissions are normalized for reproducibility.

The installed provider directory contains `build.json`, `go.mod`, `go.sum` and upstream license notices alongside the executable. `dependencies.json` records the actual package SHA-256 and Terraform `h1:` checksum, labels it as a SOKOLAI source build, and retains the original upstream artifact metadata. The provider address and upstream version continue to select the module-compatible API; the image digest and package checksums identify the rebuilt implementation.

Changing source or dependencies requires explicitly updating the build locks, reviewing the generated hashes, rebuilding with lock updates disabled, and passing the full image security and offline qualification gates. The manifest generator preserves existing source-build locks and requires `--provider-builds` for their dry-run verification; a new provider identity requires a separate proposed manifest and reviewed build locks. These provider packages are distributed inside the image, not published as replacements in the Terraform registry.

## Release qualification

The prepared build verifies every selected artifact and initializes all catalogue roots and provider versions without networking. Each provider version must also validate and return its schema through the engine's `providers schema -json` command. Root initialization proves dependency availability; it does not validate every possible module configuration. The release gate then runs six representative network-module cases against the retained image: exact version, compatible constraint, matching provider lock, unavailable module version, wrong provider lock hashes and upstream-only provider lock hashes. Successful cases must load the nested interfaces module, initialize and validate, use linked providers from the baked mirror, and preserve supplied source and lock files. Negative cases must fail initialization for the expected reason without claiming validation succeeded.

The same gate runs before publication approval and after signed-candidate verification and anonymous pull. It binds the receipt to the reviewed catalogue, preparation implementation and launcher, retains results under `runtime.terraformAvm` in `qualification.json`, and checks that no probe container remains. Ordinary PR updates run code and workflow checks without executing this image. No actual public qualification is claimed until #1857 records the release evidence.

## Updates and support

Changes to the engine, catalogue, provider set or operating-system packages require a new reviewed image identity. Preserve Terraform, provider, module and operating-system license notices when redistributing. Consult the [security policy](../../SECURITY.md) for private vulnerability reports; for usage problems, include the image digest, platform, backend/SDK versions and a minimal non-sensitive example in a [repository issue](https://github.com/sokolaidev/maf-extensions/issues).
