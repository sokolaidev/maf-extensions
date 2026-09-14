# Terraform and OpenTofu validation images

Build one engine per image, from the repository root. The initial platform is **Linux amd64**. The `builtin` profile has an empty provider mirror and supports built-in resources and local modules. The `random` profile additionally mirrors that engine registry's `hashicorp/random` 3.7.2 package. Downloads happen during image construction; validation has closed egress.

```sh
docker build --platform linux/amd64 --build-arg ENGINE=terraform --build-arg PROFILE=builtin -t maf-terraform:builtin images/terraform-sandbox
docker build --platform linux/amd64 --build-arg ENGINE=opentofu --build-arg PROFILE=builtin -t maf-opentofu:builtin images/terraform-sandbox
docker build --platform linux/amd64 --build-arg ENGINE=terraform --build-arg PROFILE=random -t maf-terraform:random images/terraform-sandbox
docker build --platform linux/amd64 --build-arg ENGINE=opentofu --build-arg PROFILE=random -t maf-opentofu:random images/terraform-sandbox
```

The base is Python 3.13.15 slim pinned by digest in [Dockerfile](Dockerfile). [install.py](install.py) checks every downloaded archive before extracting it, preserves engine license notices, leaves provider licenses inside their mirror archives, and records the engine binary digest in the image. [runner.py](runner.py) verifies that identity and version before executing a request. Deploy the resulting image by immutable digest; a deployment owns its trusted image and provider selection.

| Component | Version | Source | Archive SHA-256 |
|---|---|---|---|
| Terraform | 1.16.2 | [HashiCorp releases](https://releases.hashicorp.com/terraform/1.16.2/) | `0d17011f0c4664539b164b044903d04e296c86c13cb9f28040076c65cfb3985a` |
| OpenTofu | 1.12.6 | [OpenTofu release](https://github.com/opentofu/opentofu/releases/tag/v1.12.6) | `5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8` |
| Terraform random provider | 3.7.2 | [HashiCorp releases](https://releases.hashicorp.com/terraform-provider-random/3.7.2/) | `7b8434212eef0f8c83f5a90c6d76feaf850f6502b61b53c329e85b3b281cba34` |
| OpenTofu random provider | 3.7.2 | [OpenTofu registry release](https://github.com/opentofu/terraform-provider-random/releases/tag/v3.7.2) | `9b0ac4c1d8e36a86b59ced94fa517ae9b015b1d044b3455465cc6f0eab70915d` |

The provider archives differ. Terraform uses `registry.terraform.io`; OpenTofu uses `registry.opentofu.org`. A lock containing another registry identity or incompatible checksums is refused during read-only initialization. The launcher never repairs it or falls back to direct downloads. For other providers, build an explicitly pinned mirror profile and qualify that provider separately; the included profiles make no general provider compatibility claim.

The Python package is MIT-licensed. The Terraform image also contains HashiCorp-licensed engine software, while the OpenTofu engine is MPL-2.0; engine and provider notices are retained in the image and their licensing is independent of the Python package. No engine or provider binary enters the Python wheel.

The source checkout example uses the same factory a host attaches to its agent. It calls no model, requires no cloud credentials, and performs no infrastructure deployment. Run these after `uv sync --locked`:

```sh
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:builtin
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:builtin
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:random --provider
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:random --provider
```

To run the real adapter suite, set `MAF_TERRAFORM_E2E_IMAGE` and `MAF_OPENTOFU_E2E_IMAGE` to the two **random** profile images, then run `uv run pytest -q packages/maf-sandbox-terraform/tests/test_terraform_docker.py`. The suite runs real CLI calls, checks the daemon after each call, and also executes [test_runner.py](test_runner.py) inside each Linux image to exercise bounded pipes, deadlines, environment isolation, and lock behavior. The deterministic package tests require neither Docker nor installed engine binaries.

## Approved dependency preparation

The repository tool [terraform_dependencies.py](../../scripts/terraform_dependencies.py) prepares additional dependency profiles using a host-owned manifest. This is build tooling, not a public Python package API or a guest-facing download service. Run it through its CLI after `uv sync --locked`; the CLI supervises a worker under one 180-second wall-time limit, including DNS and archive processing. The tool executes neither engines nor providers on the host. Validation still has CLOSED egress.

The example manifests pin the existing engine-specific `random` provider archives and the standalone `random_string` module example from HashiCorp's `v3.7.2` commit. The module graph is a single leaf, recorded separately from the provider lock. The selected upstream module files are preserved, including its UTF-8 configuration and ancillary import script; preparation and validation never execute that script. The module archive digest was reviewed and pinned from the immutable commit archive, not authenticated by a release signature. Provider digest pins carry forward the image guide's approved SHA256SUMS values. A provenance string is an operator assertion; the preparer verifies the pinned digest and does not verify signatures or establish signer trust.

```sh
uv run python scripts/terraform_dependencies.py --manifest images/terraform-sandbox/dependencies.terraform.json --output dist/dependencies/terraform
uv run python scripts/terraform_dependencies.py --manifest images/terraform-sandbox/dependencies.opentofu.json --output dist/dependencies/opentofu
docker build --network none --build-arg BASE_IMAGE=maf-terraform:builtin -f images/terraform-sandbox/prepared.Dockerfile -t maf-terraform:prepared dist/dependencies/terraform
docker build --network none --build-arg BASE_IMAGE=maf-opentofu:builtin -f images/terraform-sandbox/prepared.Dockerfile -t maf-opentofu:prepared dist/dependencies/opentofu
uv run python images/terraform-sandbox/example.py --engine terraform --image maf-terraform:prepared --prepared dist/dependencies/terraform
uv run python images/terraform-sandbox/example.py --engine opentofu --image maf-opentofu:prepared --prepared dist/dependencies/opentofu
```

The base must be a trusted builtin-profile image. The recipe refuses a nonempty base mirror, an engine mismatch, changed ZIP bytes, and any missing or additional provider archive. Tags in these local examples are conveniences: deployments must use the resulting immutable image ID/digest. Preparation refuses an existing output directory and publishes only after complete success. Preserve the output in trusted artifact storage; writable directory permissions alone do not make it immutable. Stage module files by the receipt's inventory and check their hashes, as the example does. Do not mix unrelated provider mirrors into the image.

### Manifest and request contract

Only trusted host configuration supplies the manifest. Never construct it from guest URLs or attach the preparer as an agent tool. `schema` is exactly `1`, `engine` is exactly `terraform` or `opentofu`, and unknown fields and duplicate JSON keys are refused. The provider list requires full `hostname/namespace/type` source identity, exact version, target platform, and an artifact. A module entry requires a name, immutable 40- or 64-hex revision, archive subtree and full local graph: directory keys map module-call names to their resolved relative directory targets. `.` is the root. Every configuration directory and edge must match, be reachable and be acyclic. HCL and JSON are supported; remote/dynamic module sources, override files, mixed engine file precedence, hidden content other than dependency locks, state, links and non-text module payloads are refused. Broader graphs require separately prepared local modules; there is no recursive public registry/Git resolver.

Every artifact requires an exact `url`, trusted ZIP `sha256`, and a bounded `provenance` reference. Optional `redirects` lists at most three exact subsequent HTTPS URLs. Alternatively, an exact GitHub release URL can carry its decimal `github_repository_id`; only its first HTTPS response may supply one signed redirect to `release-assets.githubusercontent.com/github-production-release-asset/<repository-id>/<asset-id>`. That capability belongs to this artifact transfer, expires with the transfer, and grants no guest authority over the CDN. Further redirects are refused. Signed queries are accepted only on this server-issued redirect, have a bounded fixed key vocabulary, reject duplicate fields and decoded controls, and never enter receipts or diagnostics.

All ordinary URLs must already be canonical ASCII HTTPS with lowercase DNS host and implicit port 443. Paths are exact and allow only ASCII letters, digits, `_`, `-`, `.`, `~` and `/`; empty/dot segments, percent encoding, duplicate separators, userinfo, explicit ports, backslashes, fragments and caller queries are refused. No prefix rule broadens an ordinary URL. Requests are fixed GETs with `Host`, `User-Agent: maf-dependency-preparation/1`, `Accept: application/octet-stream`, `Accept-Encoding: identity`, and no body. Caller methods, headers and bodies are unsupported fields. Cookies, authentication, environment proxies, CONNECT and service discovery are unused. Every redirect receives the same checks. The transport rejects private, link-local, multicast and transition-address routes, checks all DNS answers, then connects to a checked numeric address with TLS hostname verification and no second DNS lookup.

Limits are 1 MiB manifest, 32 provider archives, 16 module archives, 64 MiB per download, 256 MiB accumulated downloads, 32 KiB parsed response headers, 4,096 ZIP entries, 256 MiB expanded provider data, 8 MiB expanded module data, 256 files per module bundle, and 64 graph nodes. The worker and parent share a fixed 180-second maximum; malformed or incomplete data never publishes an output. Complete SHA-256 verification precedes archive parsing. ZIPs are read as bounded data and never extracted by archive paths on the host. Receipts contain artifact identities, digests, graph/file inventories, bounded decisions and the complete policy hash, omitting transfer URLs and response content. A changed policy changes that identity; runtime warm reuse remains unsupported.

### Verification

Run `uv run pytest -q tests/test_terraform_dependencies.py` for TLS receiver controls, policy refusals, artifact integrity, graph verification and archive bounds. The receiver accepts the negative controls under unrestricted requests before the preparer refuses them. DNS-private refusal tests are separate from the local TLS receiver, whose dial address and trust root are deliberately replaced by the test fixture.

For real adapter checks, set `MAF_TERRAFORM_PREPARED_IMAGE`, `MAF_OPENTOFU_PREPARED_IMAGE` to immutable image IDs and `MAF_TERRAFORM_PREPARED_DIR`, `MAF_OPENTOFU_PREPARED_DIR` to the corresponding output directories, then run `uv run pytest -q tests/test_terraform_dependencies_docker.py`. This verifies both engines, provider/module initialization, correct and mismatched readonly locks, missing dependencies, source nonmutation, daemon-observed network mode and disposal. A separate Docker receiver accepts direct HTTP and raw CONNECT controls over bridge networking before the identical controls are denied inside CLOSED adapter sandboxes. This is Docker evidence; ACAS and WSLC have not been live-qualified for this preparation profile.
