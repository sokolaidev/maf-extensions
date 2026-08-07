# sandbox-bicep

Sandboxed Bicep validation as a [Microsoft Agent Framework](https://aka.ms/AgentFramework) tool: `bicep_validate` writes the files an agent authored into a sandbox, runs `bicep build` and `bicep lint` there, and returns the compiler's SARIF diagnostics as structured text — T2 (compiler truth) instead of T0 (the model checking its own work).

```
app  ->  sandbox_router  ->  a backend (maf-aca-sandboxes, ...)  ->  this workload
```

This package is a sandbox **kind** in the sense of [`sandbox-router`](../sandbox-router/README.md)'s protocol — and of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568), the upstream feature request this layering is the reference implementation for. It contains **no Azure import and no sandbox lifecycle code**; it asks a `SandboxRouter` for a sandbox and gets back `write_file` and `exec`, so the same tool runs unchanged against ACA Sandboxes, a local Docker container or an in-process fake. Tests enforce both boundaries (`TestNoHostDependency`).

```python
from sandbox_bicep import make_bicep_tools

tools = make_bicep_tools(router, workspace_store, "devops-engineer", context,
                         image="bicep-sandbox:0.46.1")
```

Pass `router=None` — or a router with no backend — and you get `[]` back: an unconfigured host attaches no tool rather than one that fails when called.

What is Bicep-specific — the command templates, the accepted extensions, the SARIF parsing, the one host Bicep is allowed to reach (`mcr.microsoft.com`, for AVM module restore) — lives here and only here. The spec pins the egress allowlist and work directory as properties of the workload, not of configuration: a deployment that could widen Bicep's egress could undo the containment the tool's design rests on.

Its companion artefacts live in the host repository, because a container image and a registry are not Python: `images/bicep-sandbox/` (pinned Bicep on Azure Linux) and `infra/bicep-sandbox/` (the registry and pull identity that serve it). The hard-won behaviours of the pinned CLI — SARIF on stderr for `build` but stdout for `lint`, `build-params` for `.bicepparam`, config discovery only by walking up from the source file — are documented where they bite, in [`_tool.py`](src/sandbox_bicep/_tool.py).

## Provenance

Split out of `maf-aca-sandboxes` (which keeps the ACA backend and nothing else) so the workload's dependency set states its portability: `sandbox-router` + `agent-framework-core`, nothing more. Built for issues [#408](https://github.com/sokolaidev/ats-maf/issues/408) and [#663](https://github.com/sokolaidev/ats-maf/issues/663); the security analysis is in the host repository under `docs/work-in-progress/issue-408-exec-surface-security.md`.
