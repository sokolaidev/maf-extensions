# maf-sandbox-terraform

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-terraform)](https://pypi.org/project/maf-sandbox-terraform/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-terraform)](https://pypi.org/project/maf-sandbox-terraform/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-terraform/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxTerraformExperimentalWarning`.

Validate Terraform or OpenTofu projects offline. Optional formatting tools return changed file contents for the model to save through host file tools.

Requires Python 3.12–3.14. The Python package does not include the Terraform or OpenTofu CLI.

```bash
pip install maf-sandbox-terraform
```

## Attach the tools

```python
from maf_sandbox_terraform import make_terraform_tools

tools = make_terraform_tools(
    router,
    file_store,
    "infrastructure-agent",
    context,
    engine="terraform",
    image="terraform-sandbox:local",
    formatting=False,
)
```

The host supplies a router, file store and `CallerContext`. It chooses `engine="terraform"` or `engine="opentofu"` and a matching image. With no configured backend the factory returns `[]`.

| Engine | Validation tool | Optional formatting tool |
|---|---|---|
| Terraform | `terraform_validate` | `terraform_format` |
| OpenTofu | `opentofu_validate` | `opentofu_format` |

The model supplies `files: list[str]` and `root_module: str = "."`. It cannot select CLI flags, an image or an engine.

Build images using the [image guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/terraform-sandbox/README.md). The [usage guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/terraform-sandbox/USAGE.md) provides runnable examples and dependency preparation.

## Inputs

Supply a complete manifest of configuration files, local modules and referenced text assets. Every name must appear in the caller's listing. Completeness checks cover only files that the host exposes there.

Terraform accepts `.tf` and `.tf.json`. OpenTofu also accepts `.tofu` and `.tofu.json`, using its native precedence rules. The root must contain recognized configuration. Upload names cannot traverse with `..`; valid relative module references inside files are preserved.

State, plans, variable-value files, CLI credentials, plugin binaries and reserved directories are refused. Inputs are text only, with limits of 64 files, 8 MiB per file and 32 MiB total.

## Validation

The fixed launcher runs these commands without interaction or terminal color:

1. `init -backend=false`, using prepared modules and a filesystem provider mirror.
2. `validate -json`, checking the verdict, diagnostic counts and exit status.
3. `fmt -check -recursive`, reporting formatting separately.

A supplied root lock file is read-only during initialization. Without one, a generated lock exists only inside the disposable guest. No command writes back to the host store.

Initialization failure or malformed, inconsistent, truncated or oversized output means incomplete validation. A hidden report is not evidence of success. Successful validation does not prove that a deployment will succeed.

## Formatting

Set `formatting=True` to attach the separate formatting tool. It runs `fmt -recursive -no-color` without initialization or validation, so a base image without providers is sufficient.

The report maps store-relative paths to complete changed file contents. Unchanged files are omitted. Saving them is a separate host file-write call with the host's approval policy.

All changed files are returned together, or none are. CLI output and returned file bytes share a 128 KiB budget. The complete JSON report, including escaping and metadata, must also fit 128 KiB.

A timeout, formatter failure or overflow returns `Formatting INCOMPLETE` without partial text. Use a smaller complete manifest if possible. A single changed file over the limit cannot be returned. Hidden argument names also withhold file text and locations.

## Execution and labels

The kind requires POSIX, `EXEC`, `FILES_IN`, at least container isolation, closed network access and a separate sandbox per call. Disposal is mandatory. There is no direct-download fallback for missing dependencies.

All phases share `exec_timeout_seconds`: 120 by default, finite and at most 600. Host execution adds five seconds for transport and cleanup. Cancellation waits for bounded execution before disposal.

Providers execute native code and may read other guest paths. Use a dedicated image without credentials, sensitive files or host mounts. This tool exposes no plan, apply, destroy, import or state operations.

Validation and formatting each return an untrusted report and trusted fixed guidance. The host supplies confidentiality and later-tool policy. See the [kind guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/terraform.md) for the complete contract.

Live validation covers Linux amd64 Docker images. ACAS and WSLC execution remain unverified. When the launcher changes, rebuild both base and prepared images; prepared-image receipts bind to its `reader_sha256`.
