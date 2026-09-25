# maf-extensions

[![Tests](https://img.shields.io/github/actions/workflow/status/sokolaidev/maf-extensions/tests.yml?branch=main&label=tests)](https://github.com/sokolaidev/maf-extensions/actions/workflows/tests.yml) [![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/downloads/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Community extensions for [Microsoft Agent Framework](https://aka.ms/AgentFramework), maintained by [SOKOLAI BV](https://www.sokol.ai). **Not affiliated with or endorsed by Microsoft.** Everything here is experimental (0.x): each package warns on import, and every release before 1.0.0 may include breaking changes.

## maf-sandbox

Sandboxed tool execution for MAF agents: validate infrastructure, create diagrams, process files or run generated code. These packages separate what a tool does from where its work runs. Tool calls still pass through the framework's middleware for approvals, information-flow policy and budgets. The suite is the reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568).

See the [sandbox documentation](docs/sandbox/README.md) for concepts, setup and supported workloads.

| Package | Released | What it is | Depends on |
|---|---|---|---|
| [`maf-sandbox`](packages/maf-sandbox/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) | Shared sandbox protocol, routing, policy and MAF integration | `agent-framework-core` (protocol modules are import-clean; the glue imports lazily) |
| [`maf-sandbox-acas`](packages/maf-sandbox-acas/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) | MicroVM backend using Azure Container Apps Sandboxes | `maf-sandbox`, `azure-identity`, `azure-containerapps-sandbox` (preview) |
| [`maf-sandbox-bicep`](packages/maf-sandbox-bicep/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) | Bicep compilation and linting | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-codeact`](packages/maf-sandbox-codeact/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) | Python execution with optional files and host tools | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-deepagents`](packages/maf-sandbox-deepagents/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) | Sandbox integration for LangChain Deep Agents | `maf-sandbox`, `deepagents` |
| [`maf-sandbox-docker`](packages/maf-sandbox-docker/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) | Container backend for Docker-compatible engines | `maf-sandbox` |
| [`maf-sandbox-docker-sbx`](packages/maf-sandbox-docker-sbx/) | not yet released | MicroVM backend on Docker Sandboxes (`sbx`), local on Windows, macOS and Linux | `maf-sandbox` |
| [`maf-sandbox-drawio`](packages/maf-sandbox-drawio/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) | Validation and layout of editable draw.io diagrams | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-hyperlight`](packages/maf-sandbox-hyperlight/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) | Python microVM backend for Windows and Linux | `maf-sandbox`, the matched Hyperlight 0.7.0 Python SDK, Wasm backend and guest |
| [`maf-sandbox-otel`](packages/maf-sandbox-otel/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) | OpenTelemetry logs, traces and metrics for sandbox activity | `maf-sandbox`, `opentelemetry-api` |
| [`maf-sandbox-terraform`](packages/maf-sandbox-terraform/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-terraform)](https://pypi.org/project/maf-sandbox-terraform/) | Offline Terraform and OpenTofu validation and formatting | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-tui`](packages/maf-sandbox-tui/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) | Terminal console to inspect and dispose application sandboxes | `maf-sandbox`, `textual` |
| [`maf-sandbox-wslc`](packages/maf-sandbox-wslc/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) | Local container backend using WSL's container CLI | `maf-sandbox` |

```
app  ->  maf_sandbox (router)  ->  a backend (maf_sandbox_acas, testing, ...)  ->  the sandbox
              ^ a kind (maf_sandbox_bicep) calls the router; kinds and backends never import each other
              |
              +--> events --> an observer (maf_sandbox_otel) --> OpenTelemetry
                   an observer serves no sandbox and implements no tool: it is registered on
                   the router and the host-tool registry, and only reads what already happened
```

### Samples

See the [samples README](samples/README.md) for runnable examples and their requirements.

## Development

```bash
uv sync                # one workspace, one lock; agent-framework-core comes from PyPI at the released range
uv run poe gate        # the pre-PR gate: pytest, ruff check, ruff format, both pyright passes
```

`poe` is [Poe the Poet](https://poethepoet.natn.io/), pinned in the dev group: it detects the workspace's `uv.lock` and runs each task through `uv run` itself. The five tasks it composes are the same checks CI runs — `poe types-packages` enumerates every `packages/*/` with its own strict pyright config rather than naming them, so a new package is type-checked on the commit that adds it. CI runs the checks directly, per step, for the annotations; `poe gate` is the one-command local form of the same gate.

Each package is deliberately self-contained — building, testing and publishing need nothing from this root beyond the shared lock. New extensions arrive as sibling directories under `packages/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and what the boundary tests are protecting; [`RELEASING.md`](RELEASING.md) and [`docs/maintainers.md`](docs/maintainers.md) cover releases and the publishing setup. AI agents working here should read [`AGENTS.md`](AGENTS.md) first.

## Provenance

Extracted, with their history, from a production agent application where they run today: an advisor that delegates infrastructure work to sub-agents, and needed somewhere safe for those agents' tools to run. Everything here was shaped by that use — the minimum-isolation-floor rule, the label-based purge that survives a multi-replica host, and the compiler-truth validation loop are all answers to problems that showed up in production rather than in design.
