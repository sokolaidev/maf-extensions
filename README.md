# maf-extensions

[![Package and Sandbox Validation](https://img.shields.io/github/actions/workflow/status/sokolaidev/maf-extensions/tests.yml?branch=main&label=validation)](https://github.com/sokolaidev/maf-extensions/actions/workflows/tests.yml) [![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/downloads/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Community extensions for [Microsoft Agent Framework](https://aka.ms/AgentFramework), maintained by [SOKOLAI BV](https://www.sokol.ai). **Not affiliated with or endorsed by Microsoft.** Everything here is experimental (0.x): each package warns on import, and every release before 1.0.0 may include breaking changes.

[![Daily image security](https://img.shields.io/github/actions/workflow/status/sokolaidev/maf-extensions/image-security.yml?branch=main&event=schedule&label=daily%20image%20security)](https://github.com/sokolaidev/maf-extensions/actions/workflows/image-security.yml?query=event%3Aschedule) — [scope and failure policy](docs/security/container-images.md)

See the [security documentation](docs/security/README.md) for release-specific scan results, artifact verification, tested configurations and remaining gaps, and the [security policy](SECURITY.md) for private reporting and supported versions.

## Container images

Looking at this README from a GHCR package page? Use the image's consumer guide for its purpose, pull and verification instructions, runtime interface and support boundaries. A visible registry tag does not establish release completion; check the [release status and evidence](https://sokolaidev.github.io/maf-extensions/) before use.

| Image | Purpose | Consumer documentation |
|---|---|---|
| `ghcr.io/sokolaidev/maf-extensions/graphviz` ([0.1.1](https://github.com/sokolaidev/maf-extensions/releases/tag/image-graphviz-v0.1.1)) | Render Graphviz DOT to PNG on Linux/amd64 | [Graphviz image guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/graphviz-sandbox/README.md) |

## maf-sandbox

Sandboxed tool execution for agents: validate infrastructure, create diagrams, process files or run generated Python. Workload packages (kinds) describe what a tool needs; the router checks host policy and selects a compatible backend to run it. Backends provide the execution boundary, while the host owns identity, credentials, storage and cleanup policy.

The MAF integration exposes kinds as ordinary tools, keeping the outer tool call on the framework's middleware path for approvals, information-flow policy and budgets. Guest-to-host tools use a separately configured registry. Other frameworks can use the [Deep Agents adapter](packages/maf-sandbox-deepagents/) or call the router directly, as the [AutoGen sample](samples/19_autogen_docker_codeact/) does. The suite is the reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568).

### Start here

1. Read the [sandbox overview](docs/sandbox/README.md) for the architecture and application wiring.
2. Choose a [workload](docs/sandbox/kinds/README.md) and a compatible [backend](docs/sandbox/backends/README.md). The router defaults to a `MICROVM` isolation floor; Docker, WSLC and Bubblewrap require an explicit `CONTAINER` floor. Hyperlight runs a packaged Python runtime, without a shell or package installation.
3. Follow the package README for installation and prerequisites, then run a [sample](samples/README.md).

### Packages

All packages require Python 3.12–3.14. Install the backend and workload or framework adapter your application needs; each package README documents its configuration and runtime requirements.

| Package | Released | What it is | Depends on |
|---|---|---|---|
| [`maf-sandbox`](packages/maf-sandbox/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) | Shared sandbox protocol, routing, policy and MAF integration | `agent-framework-core` (protocol modules are import-clean; the glue imports lazily) |
| [`maf-sandbox-acas`](packages/maf-sandbox-acas/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) | MicroVM backend using Azure Container Apps Sandboxes | `maf-sandbox`, `azure-core`, `azure-identity`, `azure-containerapps-sandbox` (preview) |
| [`maf-sandbox-bicep`](packages/maf-sandbox-bicep/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) | Bicep compilation and linting | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-bubblewrap`](packages/maf-sandbox-bubblewrap/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bubblewrap)](https://pypi.org/project/maf-sandbox-bubblewrap/) | Linux namespace and cgroup backend without a container engine | `maf-sandbox` |
| [`maf-sandbox-codeact`](packages/maf-sandbox-codeact/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) | Python execution with optional files and host tools | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-deepagents`](packages/maf-sandbox-deepagents/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) | Sandbox integration for LangChain Deep Agents | `maf-sandbox`, `deepagents` |
| [`maf-sandbox-docker`](packages/maf-sandbox-docker/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) | Container backend for Docker-compatible engines | `maf-sandbox` |
| [`maf-sandbox-docker-sbx`](packages/maf-sandbox-docker-sbx/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker-sbx)](https://pypi.org/project/maf-sandbox-docker-sbx/) | MicroVM backend on Docker Sandboxes (`sbx`), local on Windows, macOS and Linux | `maf-sandbox` |
| [`maf-sandbox-drawio`](packages/maf-sandbox-drawio/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) | Validation and layout of editable draw.io diagrams, with optional PNG/JPG/SVG export | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-hyperlight`](packages/maf-sandbox-hyperlight/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) | Python microVM backend for Windows and Linux | `maf-sandbox`, the matched Hyperlight 0.7.0 Python SDK, Wasm backend and guest |
| [`maf-sandbox-otel`](packages/maf-sandbox-otel/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) | OpenTelemetry logs, traces and metrics for sandbox activity | `maf-sandbox`, `opentelemetry-api` |
| [`maf-sandbox-terraform`](packages/maf-sandbox-terraform/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-terraform)](https://pypi.org/project/maf-sandbox-terraform/) | Offline Terraform and OpenTofu validation and formatting | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-tui`](packages/maf-sandbox-tui/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) | Terminal console to inspect and dispose application sandboxes | `maf-sandbox`, `textual` |
| [`maf-sandbox-wslc`](packages/maf-sandbox-wslc/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) | Local container backend using WSL's container CLI | `maf-sandbox` |
| [`maf-compaction`](packages/maf-compaction/) | not yet released | Prompt-cache-aware compaction strategies, from the [compaction family](docs/compaction/README.md) below | `agent-framework-core` |
| [`maf-cachebench`](packages/maf-cachebench/) | not yet released | The benchmark the compaction strategies were measured with: cost, cache hits and facts kept, on a replayed conversation or a live agent | `maf-compaction`, `agent-framework-core`, `httpx`; provider clients as extras |

```
app  ->  maf_sandbox (router)  ->  a backend (maf_sandbox_acas, testing, ...)  ->  the sandbox
              ^ a kind (maf_sandbox_bicep) calls the router; kinds and backends never import each other
              |
              +--> events --> an observer (maf_sandbox_otel) --> OpenTelemetry
                   an observer serves no sandbox and implements no tool: it is registered on
                   the router and the host-tool registry, and only reads what already happened
```

### Samples and integrations

The [samples README](samples/README.md) lists runnable examples and their requirements. Useful entry points include [Docker CodeAct](samples/06_docker_codeact/), [file inputs and artifacts](samples/08_docker_codeact_files/), [host tools](samples/15_acas_codeact_host_tools/), [Deep Agents](samples/17_deepagents_docker_bicep/), and [Terraform/OpenTofu validation](samples/20_terraform_validation/).

Use [OpenTelemetry](packages/maf-sandbox-otel/) to observe sandbox activity and the [TUI](packages/maf-sandbox-tui/) to inspect application-owned sandboxes. The [shared MCP workload service](docs/sandbox/openclaw.md) is experimental; its documentation records bounded two-session Gateway evidence and the lifecycle and recovery qualification still pending.

## Compaction

`maf-compaction` is a set of compaction strategies shaped around the provider's prompt cache, and `maf-cachebench` the benchmark they were measured with. A prompt cache matches on an exact prefix, and every compaction rewrites history inside that prefix, so compaction breaks the cache by construction. These strategies keep a fixed head and tail of the conversation verbatim, change only the band between them, and change it by rules that produce the same bytes on every later turn. The record strategy has the model write the facts from its tool results into a record and drops the tool groups the record covers; the user-turn strategy summarises the user's turns; the composition runs both with a last-resort chain. They were measured against the framework's own strategies with a benchmark that plants facts in tool results and asks for them back after compaction; the [compaction overview](docs/compaction/README.md) carries the findings and when to use which, and the [package table](#packages) above carries the release state.

## Provenance

Extracted, with their history, from a production agent application where they run today: an advisor that delegates infrastructure work to sub-agents, and needed somewhere safe for those agents' tools to run. Everything here was shaped by that use — the minimum-isolation-floor rule, the label-based purge that survives a multi-replica host, and the compiler-truth validation loop are all answers to problems that showed up in production rather than in design.
