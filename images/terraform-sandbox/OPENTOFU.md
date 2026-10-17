# OpenTofu random image

The `opentofu-random` release profile provides offline initialization and validation for Linux amd64. It contains the pinned OpenTofu engine, Python, the bounded validation launcher, `registry.opentofu.org/hashicorp/random`, and a verified local `random-string` example module. [image.json](image.json) and [dependencies.opentofu.json](dependencies.opentofu.json) define exact engine, provider and module identities. This profile uses the strict preparation pipeline; the archive-only `random` build recipe remains an internal development fixture. There is no separate public `opentofu-prepared` image.

The engine is a SOKOLAI source build of a pinned upstream release commit with reviewed Go dependency overrides. `/opt/maf-terraform/engine.json` records its source revision, overrides, build toolchain and binary checksum. The random provider is also a SOKOLAI build of the pinned OpenTofu release commit, using Go 1.27.2 and `golang.org/x/net` 0.60.0 to remediate vulnerable dependencies in the upstream binary. Its installed directory retains `build.json`, resolved `go.mod`/`go.sum` and license notices; the preparation receipt preserves the upstream archive identity and the reviewed replacement package hash. Keep its `registry.opentofu.org` identity: a Terraform registry lock is not interchangeable. This small profile does not include the separate platform provider catalogue, AVM registry modules, credentials or deployment tooling.

## Select and verify a release

After publication, select `opentofu-random` from the [release catalogue](https://sokolaidev.github.io/maf-extensions/), follow the [signed release verification procedure](../../docs/security/container-image-releases.md), and pin the verified GHCR digest. Review current monitoring separately from signed release identity. A locally built image has no signed release completion. Publication and public consumer evidence remain tracked by [#1844](https://github.com/sokolaidev/maf-extensions/issues/1844).

Configure your Docker backend with that immutable image and use the [Terraform validation kind](../../packages/maf-sandbox-terraform/README.md) with the OpenTofu engine and `Egress.CLOSED`; the [validation sample](../../samples/20_terraform_validation/README.md) describes host integration. Installing the Python package does not download this image. Provider configuration must use the version recorded by its manifest, for example:

```hcl
terraform {
  required_providers {
    random = {
      source  = "registry.opentofu.org/hashicorp/random"
      version = "3.9.1"
    }
  }
}
resource "random_string" "example" {
  length = 16
}
```

Supplied `.terraform.lock.hcl` files are read-only inputs. Include the baked provider's `h1:` checksum from the preparation receipt or generate a native lock against this image's offline mirror in a disposable project. A wrong checksum, an upstream-only archive checksum, or an unavailable version must fail initialization. Source-built provider bytes differ from upstream archives; the baked `h1:` identity is required. Preserve deployment locks separately when they describe a different provider distribution; do not remove checksum checks to make validation pass. The launcher links providers from the baked mirror and stages working files separately from supplied source.

## Obtain the prepared module files

The image retains `/opt/maf-terraform/dependencies.json` and `/opt/maf-terraform/modules/random-string/`. Copy them from the verified image into a new local directory using a stopped container; this does not execute the image:

```sh
# IMAGE is the verified ghcr.io/sokolaidev/maf-extensions/opentofu-random@sha256:... reference.
container=$(docker create "$IMAGE")
mkdir -p opentofu-preparation/modules
docker cp "$container:/opt/maf-terraform/dependencies.json" opentofu-preparation/receipt.json
docker cp "$container:/opt/maf-terraform/modules/." opentofu-preparation/modules/
docker rm "$container"
```

Check each exported module file's SHA-256 against `modules[].files` in `receipt.json`, and require the exact file inventory before using it. Stage the verified directory inside your source tree and reference it with `source = "./modules/random-string"`. The receipt binds the source archive, revision, prepared module graph, provider identities, manifest and preparation policy. These local example files are optional: direct random-provider configurations need no module export. The [developer build instructions](USAGE.md#build-a-prepared-image) also explain exporting a full preparation, including provider ZIPs, from the `prepared` Docker target.

## Build and qualification

From the repository root, the release build dispatcher selects the builtin OpenTofu base and layers `dependencies.opentofu.json` through `prepared.Dockerfile`:

```sh
bash scripts/build_scan_image.sh opentofu-random
```

The image gate runs without networking, with a read-only root filesystem, dropped capabilities and bounded resources. It checks exact and matching-lock initialization and validation with the exported module; unavailable provider versions, missing local modules, wrong lock checksums, upstream-only archive locks and Terraform registry identities must be refused. Successful checks require providers linked from the baked mirror, unchanged source and locks, matching engine source provenance, and module files matching the receipt. The host verifies container removal. Results are retained under `runtime.opentofuRandom` in release qualification evidence and are repeated after anonymous pull of the signed candidate.

Image execution and scanning run in the release gate and explicit image-security runs; ordinary PR updates use code and workflow checks. Every High or Critical vulnerability blocks release, including findings without a fix. Scan results are time-bound evidence, not proof of absence of malware or backdoors. This profile qualifies initialization and validation on Docker, not apply, plan, cloud authentication, production suitability, or operation on other backends.

## Updates and support

Engine, provider, module and base-image changes require a new reviewed image digest and fresh qualification. Preserve engine, provider, module and operating-system license notices when redistributing. Use the [security policy](../../SECURITY.md) for private vulnerability reports. For usage problems, file a [repository issue](https://github.com/sokolaidev/maf-extensions/issues) with the image digest, platform, backend/SDK versions and a minimal non-sensitive configuration.
