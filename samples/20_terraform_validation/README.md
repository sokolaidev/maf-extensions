# Terraform and OpenTofu validation on Docker or ACAS

One agent turn calls `terraform_validate` or `opentofu_validate` on [main.tf](main.tf). The module uses the random provider and deliberately omits `random_string.length`. The tool initializes from the image's offline provider mirror and returns the provider-schema error. It performs no plan, apply or state operation.

`SAMPLE_BACKEND` selects `docker` (default) or `acas`. `SAMPLE_ENGINE` selects `terraform` (default) or `opentofu`. Both engines use the same module. The host allows container isolation for Docker and requires microVM isolation for ACAS. Guest networking stays closed and every call requires disposal.

## Prerequisites

Install Python 3.12+, [uv](https://docs.astral.sh/uv/) and the Azure CLI, then sign in with `az login`. Configure `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL` for an Azure OpenAI deployment accessible to that identity. All runtime dependencies come from the agent's PEP 723 metadata and published wheels.

Docker needs a Linux amd64 Docker engine and the selected `random` image. From the repository root:

```bash
python images/terraform-sandbox/build_image.py --engine terraform --profile random
python images/terraform-sandbox/build_image.py --engine opentofu --profile random
export TERRAFORM_SANDBOX_IMAGE=maf-terraform:1.16.2-random
export OPENTOFU_SANDBOX_IMAGE=maf-opentofu:1.12.6-random
```

ACAS additionally needs `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP` and `ACAS_SANDBOX_REGISTRY`. Push and import the selected image into that group before running. [Maintainer setup](../../docs/maintainers.md#terraform-and-opentofu-images) gives the build, push and import commands. Image variables contain bare `repository:tag` references; the registry setting qualifies them. Use a fresh tag and import when rebuilding a snapshot.

**Each ACAS validation call creates a billable sandbox.** Docker creates a local container. Both paths also incur model inference costs. The conversation identifier includes the workflow run, attempt, backend and engine so each job's cleanup stays within its own sandboxes.

## Run and check

From the repository root, with the selected image variable and model configuration exported:

```bash
set -euo pipefail
export SAMPLE_BACKEND=docker SAMPLE_ENGINE=terraform
uv run --no-project samples/20_terraform_validation/agent.py 2>&1 | tee terraform.log
python scripts/check_live_terraform_sample.py terraform.log --backend docker --engine terraform --version 1.16.2
```

For OpenTofu, set `SAMPLE_ENGINE=opentofu` and check with `--engine opentofu --version 1.12.6`. For ACAS, set `SAMPLE_BACKEND=acas` and check with `--backend acas`.

The checker reads only the tool report inside the block closed by `[measured] validation results`. It requires the selected engine and pinned version, a failed validation naming the missing `length` argument, and a passing formatting check. Model prose cannot supply this evidence. The sample prints core's per-call timing and disposal records; the checker requires each validation call to have matching successful disposal and a completed final scope purge. `Disposed 0` is expected when per-call disposal already removed everything. Disposal evidence records the backend's report, not an independent service inventory.

[Verify (live)](../../.github/workflows/verify-live.yml) runs four separate jobs, one for each backend/engine combination, against published packages by default. Docker images are built on the runner; ACAS uses imported snapshots. Every job retains its log, including failed runs. The same jobs support `source: branch` for checking workspace package changes.
