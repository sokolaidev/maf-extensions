# maf-extensions

Community extensions for [Microsoft Agent Framework](https://aka.ms/AgentFramework), maintained by [SOKOLAI BV](https://www.sokol.ai). **Not affiliated with or endorsed by Microsoft.** Everything here is experimental (0.x): each package warns on import, and every release before 1.0.0 may include breaking changes.

## The maf-sandbox family

Sandboxed code execution for MAF agents — the reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568). An agent that writes code should not be the thing that runs it; these packages give the work somewhere else to run, reached as an ordinary tool call so the framework's middleware (approvals, information-flow policy, budgets) still sees it.

| Package | What it is | Depends on |
|---|---|---|
| [`maf-sandbox`](packages/maf-sandbox/) | The backend-neutral protocol (`Sandbox`, `SandboxBackend`, `SandboxSpec`, `SandboxKey`, `Isolation`, `Capability`), the router with its minimum-isolation-floor and capability-match policy, the thread-delete purge participant, a public in-process `testing` backend, and the optional MAF glue module | `agent-framework-core` (protocol modules are import-clean; the glue imports lazily) |
| [`maf-sandbox-acas`](packages/maf-sandbox-acas/) | [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) as a backend: VM isolation, Deny-default egress, label-based lifecycle that survives multi-replica hosts | `maf-sandbox`, `azure-identity`, `azure-containerapps-sandbox` (preview) |
| [`maf-sandbox-bicep`](packages/maf-sandbox-bicep/) | The first workload *kind*: `bicep_validate` — compiler-truth validation of agent-authored Bicep, on any backend | `maf-sandbox`, `agent-framework-core` |
| [`maf-sandbox-wslc`](packages/maf-sandbox-wslc/) | `wslc` (the container CLI that ships with WSL) as a backend: container isolation, Closed egress, for validating on the developer's own machine | `maf-sandbox` |

```
app  ->  maf_sandbox (router)  ->  a backend (maf_sandbox_acas, testing, ...)  ->  the sandbox
              ^ a kind (maf_sandbox_bicep) calls the router; kinds and backends never import each other
```

## Samples

[`samples/`](samples/) holds small, self-contained programs that show the pieces wired together — the `app` box above, which the package READMEs describe but never show. [`01_acas_bicep`](samples/01_acas_bicep/) is a one-turn agent that validates a deliberately flawed Bicep file: an ACAS backend behind a router, a workspace context built the way that keeps one conversation out of another's sandbox, and `bicep_validate` attached to a MAF agent. Samples install from PyPI rather than from this workspace, and are linted but not run in CI. [`images/bicep-sandbox/`](images/bicep-sandbox/) is the image they validate in — a pinned Bicep CLI and its lint config, with the build, push and disk-image import commands that get it into a sandbox group.

## Development

```bash
uv sync                # one workspace, one lock; agent-framework-core comes from PyPI at the released range
uv run pytest          # all packages' tests
uv run ruff check .
uv run pyright -p packages/maf-sandbox && uv run pyright -p packages/maf-sandbox-acas && uv run pyright -p packages/maf-sandbox-bicep && uv run pyright -p packages/maf-sandbox-wslc
```

Each package is deliberately self-contained — building, testing and publishing need nothing from this root beyond the shared lock. New extensions arrive as sibling directories under `packages/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and what the boundary tests are protecting; [`RELEASING.md`](RELEASING.md) and [`docs/maintainers.md`](docs/maintainers.md) cover releases and the publishing setup. AI agents working here should read [`AGENTS.md`](AGENTS.md) first.

## Provenance

Extracted, with their history, from a production agent application where they run today: an advisor that delegates infrastructure work to sub-agents, and needed somewhere safe for those agents' code to execute. Everything here was shaped by that use — the minimum-isolation-floor rule, the label-based purge that survives a multi-replica host, and the compiler-truth validation loop are all answers to problems that showed up in production rather than in design.
