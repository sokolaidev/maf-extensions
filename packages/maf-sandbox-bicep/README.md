# maf-sandbox-bicep

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxBicepExperimentalWarning`.

Compile and lint Bicep templates and parameter files from an agent's file store. The `bicep_validate` tool returns compiler diagnostics and fixed guidance about how to use them.

This is an independent package for [Microsoft Agent Framework](https://aka.ms/AgentFramework). It uses the `maf-sandbox` protocol and has no backend dependency.

## Quickstart

```bash
pip install maf-sandbox-bicep
```

```python
from maf_sandbox_bicep import make_bicep_tools

tools = make_bicep_tools(
    router,
    file_store,
    "devops-engineer",
    context,
    image="bicep-sandbox:0.46.1",
)
```

The host supplies `router`, `file_store` and `CallerContext`. The context lists the files this caller may share. With no router or no configured backend, the factory returns `[]`. An incompatible backend is refused before attachment.

Build the [Bicep image](https://github.com/sokolaidev/maf-extensions/blob/main/images/bicep-sandbox/README.md) and follow the [ACAS sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep), [WSLC sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/02_wslc_bicep) or [Docker sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/05_docker_bicep) for complete wiring.

## Validation

The tool reads only requested names from the caller's listing. It stages all selected files before compiling, so local modules and parameter-file references resolve together.

Templates use `bicep build`; parameter files use `bicep build-params`. The tool also runs lint as applicable. Commands are fixed templates with validated paths. Model text is passed as file content.

The image supplies the CLI and `bicepconfig.json` at `/maf-sandbox/work`. The compiler finds that configuration by walking up from the source. Each call uses a fresh child directory for sources, generated files, `HOME`, `TMPDIR` and the module cache.

`exec_timeout_seconds` defaults to 120 per compiler command. Cancellation waits for the bounded active command before cleanup. A cancelled host wait alone does not prove that the guest stopped.

## Network access

The default `Egress.ALLOWLIST` permits public module restore through these fixed destinations:

| Destination | Purpose |
|---|---|
| `mcr.microsoft.com` | Module manifests |
| `*.data.mcr.microsoft.com` | Module layers |
| `aka.ms` | Module-index redirect |
| `live-data.bicep.azure.com` | Module-index data |

The factory does not widen that list. A Docker or WSLC backend needs its filtering proxy configured to serve it.

Pass `egress=Egress.CLOSED` for offline validation. This adds `--no-restore`; local modules work, while unavailable external modules leave validation incomplete. A host may explicitly select `UNRESTRICTED` only with a backend that supports it.

The allowlist grants no Azure Resource Manager access and supplies no credentials.

## Results and cleanup

The result contains two text items: the untrusted diagnostic report and trusted fixed guidance. The host supplies result confidentiality. With automatic hiding active in a trusted conversation, the report can be hidden while guidance remains readable.

A restore failure reports `MODULE RESTORE FAILED`. Hidden, empty or unreadable diagnostics do not establish a successful validation. Forwarding hidden diagnostics to a file writer remains subject to that tool's policy.

Diagnostic formatting removes call-directory paths and applies permitted display names. It does not remove arbitrary compiler prose or every external URL.

Direct `invoke(..., skip_parsing=True)` callers receive a list of `Content` items. Preserve those items and labels; do not turn the list into a Python string representation.

Core disposes after each call by default. Explicit host opt-in can permit reclaim on a supporting backend. The kind's confinement declaration describes its file placement; it does not certify that a reused sandbox is clean.

See the [Bicep guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/bicep.md) for the full contract and [information flow](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md) for labels.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
