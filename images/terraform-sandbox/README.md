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
