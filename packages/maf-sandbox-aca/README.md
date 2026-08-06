# maf-aca-sandboxes

[Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) as a sandbox backend for [Microsoft Agent Framework](https://aka.ms/AgentFramework) agents — plus the sandbox kinds that run on it.

```
app  ->  sandbox_router  ->  maf_aca_sandboxes  ->  a sandbox kind
```

An agent that writes code should not be the thing that runs it. This package gives it somewhere else to run: a VM-isolated sandbox with Deny-default egress and no ambient identity, reached as an ordinary tool call so the agent framework's middleware still sees the call and classifies its result — only the *work* leaves the process.

**Bicep validation is the first kind.** A GitHub Copilot agent and an Azure CLI surface are the obvious next ones, and each arrives as a sibling subpackage rather than as changes to the backend.

## The backend

`AcaSandboxBackend` implements `sandbox_router.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A warm sandbox is resumed rather than replaced, so a fix-round loop does not pay a cold start per iteration. |
| `dispose(key)` | delete one sandbox |
| `dispose_scope(scope, thread)` | delete every sandbox for a conversation — **from the service, by label**, not from process memory |
| `isolation` | `vm` — which is what lets the router permit it in a deployed environment |

That `dispose_scope` detail is the one worth reading twice. A multi-replica host serves a conversation delete wherever it lands, so the replica that created a sandbox is usually not the one deleting it. A backend that consults only its own registry leaves billable VMs running, and the bug is invisible on a single-replica dev box. Sandboxes are labelled at create time so the service can answer the question instead.

Egress comes from the **spec**, not from configuration: `default_action: Deny` plus one `Allow` rule per host the kind declares. A deployment that could widen a kind's egress could undo the containment its design rests on.

## The Bicep kind

```python
from maf_aca_sandboxes.bicep import make_bicep_tools

tools = make_bicep_tools(router, workspace_store, "devops-engineer", context,
                         image="myacr.azurecr.io/bicep-sandbox:0.46.1")
```

`bicep_validate` writes the agent's `.bicep` files into a sandbox, runs `bicep build` and `bicep lint`, and returns the compiler's SARIF diagnostics as structured text. Pass `router=None` — or a router with no backend — and you get `[]` back: an unconfigured host attaches no tool rather than one that fails when called.

It contains **no Azure import and no sandbox lifecycle code**, so the same tool runs unchanged on any backend. A test enforces that.

Its companion artefacts live in the host repository, because a container image and a registry are not Python: `images/bicep-sandbox/` (pinned Bicep on Azure Linux) and `infra/bicep-sandbox/` (the registry and pull identity that serve it).

### Things learned the hard way

- **`bicep build` writes SARIF to stderr; `bicep lint` writes it to stdout.** The build template carries `2>&1` for exactly that reason, and a test pins it — dropping it makes every build report "could not parse SARIF output" against a perfectly healthy sandbox.
- **Unparseable output is an error, never zero diagnostics.** A broken sandbox must not read as a clean build.
- **`exec` takes a command string, not argv.** So the rule is a fixed template with one interpolation, and that interpolation validated against the workspace listing first. Being in the listing is *not* evidence a name is safe — a file can be created with a hostile name.
- **`DiskImage.image` is a `DiskImageSpec`; the OCI reference is `.base`.** Comparing the object to a string never matches, which made resolve-by-reference fail for every correctly imported image until it was caught by introspecting the SDK.

## Install

```bash
pip install maf-aca-sandboxes        # the backend and its kinds
pip install maf-aca-sandboxes[aca]   # + the ACA Sandboxes data-plane SDK, needed to actually run
```

The `aca` extra is separate because `azure-containerapps-sandbox` is still `0.1.0bN`: a host that has not configured a sandbox group should not carry a preview SDK. Every entry point degrades with a clear message when it is absent.

## Extracting this package

It imports nothing from its host application — only `sandbox-router`, `agent-framework-core` and `azure-*` — so moving it to its own repository is a file move plus a dependency line. `src/`, `tests/`, `scripts/` and `pyproject.toml` are already the future repo root; `images/bicep-sandbox/` and `infra/bicep-sandbox/` come with it, and the two workflows under `.github/` have to be re-created rather than moved since GitHub only reads workflows from there.

Two things this `pyproject.toml` inherits from the workspace root today and would have to declare for itself: the ruff/pyright configuration, and `[tool.pytest.ini_options]`.

What stays behind is the host's adapter — one module in the host application (today `tools/bicep.py`) that maps the host's settings onto an `AcaConfig` and supplies the request context. Read it first if you want to know what integrating this package involves.

`TestNoHostDependency` is what keeps all of that true: it scans this package's sources for any import of the host package and fails with a pointer to the adapter. Nothing else would notice, because the tests run in a process where the host is importable. The host's name appears exactly once, as `_HOST_PACKAGE` in that test — extracting this repository is that one line plus a file move.

## Provenance

Built for issues [#408](https://github.com/sokolaidev/ats-maf/issues/408) and [#663](https://github.com/sokolaidev/ats-maf/issues/663) of the application this currently ships inside. The security analysis that chose a VM-isolated sandbox over in-process execution — including the verified escalation chain that ruled in-process out, and why `--no-restore` alone was not enough — is in that repository under `docs/work-in-progress/issue-408-exec-surface-security.md`.
