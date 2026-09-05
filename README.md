# maf-extensions

[![Tests](https://img.shields.io/github/actions/workflow/status/sokolaidev/maf-extensions/tests.yml?branch=main&label=tests)](https://github.com/sokolaidev/maf-extensions/actions/workflows/tests.yml) [![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/downloads/) [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Community extensions for [Microsoft Agent Framework](https://aka.ms/AgentFramework), maintained by [SOKOLAI BV](https://www.sokol.ai). **Not affiliated with or endorsed by Microsoft.** Everything here is experimental (0.x): each package warns on import, and every release before 1.0.0 may include breaking changes.

## The maf-sandbox family — the first extension suite

Sandboxed code execution for MAF agents, and the first extension suite this repository publishes — the reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568). An agent that writes code should not be the thing that runs it; these packages give the work somewhere else to run, reached as an ordinary tool call so the framework's middleware (approvals, information-flow policy, budgets) still sees it.

[`docs/sandbox/README.md`](docs/sandbox/README.md) is the introduction — what the suite is, how it attaches to a MAF agent, what it buys and what it deliberately is not — written for a reader who does not already know the framework. The table below is the map.

| Package | Released | What it is | Depends on |
|---|---|---|---|
| [`maf-sandbox`](packages/maf-sandbox/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) | The backend-neutral protocol (`Sandbox`, `SandboxBackend`, `SandboxSpec`, `SandboxKey`, `Isolation`, `IsolationScope`, `Capability`), the router with its minimum-isolation-floor, capability-match and isolation-scope policy, the thread-delete purge participant, a public in-process `testing` backend, and the optional MAF glue module | `agent-framework-core` (protocol modules are import-clean; the glue imports lazily) |
| [`maf-sandbox-acas`](packages/maf-sandbox-acas/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) | [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) as a backend: microVM isolation, Deny-default egress, label-based lifecycle that survives multi-replica hosts | `maf-sandbox`, `azure-identity`, `azure-containerapps-sandbox` (preview) |
| [`maf-sandbox-bicep`](packages/maf-sandbox-bicep/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bicep)](https://pypi.org/project/maf-sandbox-bicep/) | The first workload *kind*: `bicep_validate` — compiler-truth validation of agent-authored Bicep, on any backend | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-codeact`](packages/maf-sandbox-codeact/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) | The CodeAct *kind*: `execute_code` — the model writes a short Python program, it runs in a closed sandbox, and what it printed comes back | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-docker`](packages/maf-sandbox-docker/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-docker)](https://pypi.org/project/maf-sandbox-docker/) | Plain Docker containers as a backend: container isolation, Closed or Allowlisted egress, and reading declared outputs back out — for a sandbox on any machine with a Docker-compatible engine, and on CI | `maf-sandbox` |
| [`maf-sandbox-wslc`](packages/maf-sandbox-wslc/) | [![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) | `wslc` (the container CLI that ships with WSL) as a backend: container isolation, Closed egress, for validating on the developer's own machine | `maf-sandbox` |
| [`maf-sandbox-otel`](packages/maf-sandbox-otel/) | not yet released | An *observer* rather than a kind or a backend: it registers on the router and the host-tool registry and turns what a sandbox did — the posture it was served under, host-tool calls, file crossings, per-key disposals — into OpenTelemetry log records, spans and metrics, under the application's providers or a security pipeline's own | `maf-sandbox`, `opentelemetry-api` |

```
app  ->  maf_sandbox (router)  ->  a backend (maf_sandbox_acas, testing, ...)  ->  the sandbox
              ^ a kind (maf_sandbox_bicep) calls the router; kinds and backends never import each other
              |
              +--> events --> an observer (maf_sandbox_otel) --> OpenTelemetry
                   an observer serves no sandbox and implements no tool: it is registered on
                   the router and the host-tool registry, and only reads what already happened
```

## Samples

[`samples/`](samples/) holds small, self-contained programs that show the pieces wired together — the `app` box above, which the package READMEs describe but never show. [`01_acas_bicep`](samples/01_acas_bicep/) is a one-turn agent that validates a deliberately flawed Bicep file: an ACAS backend behind a router, a caller context built the way that keeps one conversation out of another's sandbox, and `bicep_validate` attached to a MAF agent. Samples install from PyPI rather than from this workspace. They are linted and type-checked on every pull request, and most of them are also *run* — against the published wheels, on demand and once after a release, never on a pull request, because three of those runs create a billable Azure sandbox. [`images/bicep-sandbox/`](images/bicep-sandbox/) is the image they validate in — a pinned Bicep CLI and its lint config, with the build, push and disk-image import commands that get it into a sandbox group.

## Development

```bash
uv sync                # one workspace, one lock; agent-framework-core comes from PyPI at the released range
uv run poe gate        # the pre-PR gate: pytest, ruff check, ruff format, both pyright passes
```

`poe` is [Poe the Poet](https://poethepoet.natn.io/), pinned in the dev group: it detects the workspace's `uv.lock` and runs each task through `uv run` itself. The five tasks it composes are the same checks CI runs — `poe types-packages` enumerates every `packages/*/` with its own strict pyright config rather than naming them, so a new package is type-checked on the commit that adds it. CI runs the checks directly, per step, for the annotations; `poe gate` is the one-command local form of the same gate.

Each package is deliberately self-contained — building, testing and publishing need nothing from this root beyond the shared lock. New extensions arrive as sibling directories under `packages/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and what the boundary tests are protecting; [`RELEASING.md`](RELEASING.md) and [`docs/maintainers.md`](docs/maintainers.md) cover releases and the publishing setup. AI agents working here should read [`AGENTS.md`](AGENTS.md) first.

## Provenance

Extracted, with their history, from a production agent application where they run today: an advisor that delegates infrastructure work to sub-agents, and needed somewhere safe for those agents' code to execute. Everything here was shaped by that use — the minimum-isolation-floor rule, the label-based purge that survives a multi-replica host, and the compiler-truth validation loop are all answers to problems that showed up in production rather than in design.
