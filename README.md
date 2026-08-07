# maf-extensions

Community extensions for [Microsoft Agent Framework](https://aka.ms/AgentFramework), maintained by [SOKOLAI BV](https://www.sokol.ai). **Not affiliated with or endorsed by Microsoft.** Everything here is experimental (0.x): each package warns on import, and every release before 1.0.0 may include breaking changes.

## The maf-sandbox family

Sandboxed code execution for MAF agents — the reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568). An agent that writes code should not be the thing that runs it; these packages give the work somewhere else to run, reached as an ordinary tool call so the framework's middleware (approvals, information-flow policy, budgets) still sees it.

| Package | What it is | Depends on |
|---|---|---|
| [`maf-sandbox`](packages/maf-sandbox/) | The backend-neutral protocol (`Sandbox`, `SandboxBackend`, `SandboxSpec`, `SandboxKey`, `Isolation`), the router with its deployed-isolation policy, the thread-delete purge participant, a public in-process `testing` backend, and the optional MAF glue module | `agent-framework-core` (protocol modules are import-clean; the glue imports lazily) |
| [`maf-sandbox-aca`](packages/maf-sandbox-aca/) | [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) as a backend: VM isolation, Deny-default egress, label-based lifecycle that survives multi-replica hosts | `maf-sandbox`, `azure-identity`, `azure-containerapps-sandbox` (preview) |
| [`maf-sandbox-bicep`](packages/maf-sandbox-bicep/) | The first workload *kind*: `bicep_validate` — compiler-truth validation of agent-authored Bicep, on any backend | `maf-sandbox`, `agent-framework-core` |

```
app  ->  maf_sandbox (router)  ->  a backend (maf_sandbox_aca, testing, ...)  ->  the sandbox
              ^ a kind (maf_sandbox_bicep) calls the router; kinds and backends never import each other
```

## Development

```bash
uv sync                # one workspace, one lock; agent-framework-core comes from PyPI at the released range
uv run pytest          # all packages' tests
uv run ruff check .
uv run pyright -p packages/maf-sandbox && uv run pyright -p packages/maf-sandbox-aca && uv run pyright -p packages/maf-sandbox-bicep
```

Each package is deliberately self-contained — building, testing and publishing need nothing from this root beyond the shared lock. New extensions arrive as sibling directories under `packages/`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and what the boundary tests are protecting; [`RELEASING.md`](RELEASING.md) and [`docs/maintainers.md`](docs/maintainers.md) cover releases and the publishing setup.

## Provenance

Extracted, with their history, from a production agent application where they run today: an advisor that delegates infrastructure work to sub-agents, and needed somewhere safe for those agents' code to execute. Everything here was shaped by that use — the deployed-isolation rule, the label-based purge that survives a multi-replica host, and the compiler-truth validation loop are all answers to problems that showed up in production rather than in design.
