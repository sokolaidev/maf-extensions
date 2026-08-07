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

## Provenance

Extracted (with full history) from the host application that grew them, where the deployed production evidence lives: issues [#408](https://github.com/sokolaidev/ats-maf/issues/408), [#663](https://github.com/sokolaidev/ats-maf/issues/663) and the epic [#694](https://github.com/sokolaidev/ats-maf/issues/694).
