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

The image supplies the CLI. The package supplies a complete `bicepconfig.json` and stages it before compilation in each fresh call directory below `/maf-sandbox/work`. Bicep finds that configuration by walking up from the source. A config upload failure leaves validation incomplete. Each call also owns its generated files, `HOME`, `TMPDIR` and module cache. A manifest accepts at most 63 source files, reserving one transfer slot for the config.

The packaged config lists every linter rule in its recorded upstream release, including disabled rules. It preserves upstream defaults except for `no-unused-params=error` and `use-recent-api-versions=warning` with `maxAgeInDays=730`. Compiler IDs live separately in `compiler_codes.json`; `catalog_source.json` records the release, commit and upstream settings. These files ship in the wheel and source archive. Updating the catalog does not upgrade the host-selected compiler.

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

This version requires `maf-sandbox>=0.42.0,<0.43`. `bicep_validate` returns `SandboxResult`, which the attached tool renders as a `list[Content]`: completion, an optional `valid` or `invalid` verdict, any trusted refusals and diagnostic summary, any untrusted diagnostics or file-listing hints, then fixed guidance. The item count varies. Incomplete calls have no verdict, and either output sequence may be empty.

Completion, verdict, refusals and the selected summary carry trusted integrity. Raw diagnostics retain untrusted integrity even with trusted inputs. The host supplies result confidentiality; fixed guidance remains trusted/public. When middleware hides diagnostics, the model reads the verdict and summary or reports the files as unvalidated if there is no verdict. Refused names, staging failures, timeouts, unreadable SARIF and failed module restores leave the call incomplete.

The summary is a separate JSON text item with `type="bicep_diagnostics"`. Each record selects a catalog rule ID, a severity and a reference such as `files[0]` to an input argument. It contains no messages, raw paths, line numbers or counts. Records are deduplicated across build and lint, sorted and capped at 128. Boolean fields `unrecognized_diagnostics`, `unattributed_locations` and `truncated` identify gaps; unmatched locations use `file="unattributed"`. Unknown IDs remain in the raw report. An empty recognized subset does not establish clean validation. The summary retains the call's confidentiality and does not restore a conversation that is already untrusted.

A restore failure reports `MODULE RESTORE FAILED`. Hidden, empty or unreadable diagnostics do not establish a successful validation. Forwarding hidden diagnostics to a file writer remains subject to that tool's policy.

Diagnostic formatting removes call-directory paths and applies permitted display names. It does not remove arbitrary compiler prose or every external URL.

Direct `invoke(..., skip_parsing=True)` callers must read each item's `.text` without assuming fixed indexes. Preserve the items and labels; do not turn the list into a Python string representation. Framework function-result content exposes `.items` and joins their text in `.result`.

Core disposes after each call by default. Explicit host opt-in can permit reclaim on a supporting backend. The kind's confinement declaration describes its file placement; it does not certify that a reused sandbox is clean.

See the [Bicep guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/bicep.md) for the full contract and [information flow](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md) for labels.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
