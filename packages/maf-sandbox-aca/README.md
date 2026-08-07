# maf-sandbox-aca

> **Experimental.** This package is early-stage (`0.1.0`, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxAcaExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_aca.MafSandboxAcaExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework), built on the [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview.

```
app  ->  maf_sandbox  ->  maf_sandbox_aca  ->  the sandbox
```

An agent that writes code should not be the thing that runs it. This package gives it somewhere else to run: a VM-isolated sandbox with Deny-default egress and no ambient identity, reached as an ordinary tool call so the agent framework's middleware still sees the call and classifies its result — only the *work* leaves the process.

This package is the backend only, with no sandbox kind of its own. [`maf-sandbox-bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-bicep) is the first kind that runs on it, written against [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s protocol rather than against this backend.

## Quickstart

```bash
pip install maf-sandbox-aca
```

```python
from maf_sandbox_aca import AcaSandboxBackend, AcaSandboxConfig
from maf_sandbox import SandboxRouter

backend = AcaSandboxBackend(AcaSandboxConfig(endpoint="https://management.<region>.azuredevcompute.io", subscription_id="<sub-id>", resource_group="<rg>", sandbox_group="<group>", registry="<acr>.azurecr.io"))
router = SandboxRouter([backend], deployed=True)  # VM isolation is what makes `deployed=True` permitted here
```

`azure-containerapps-sandbox` — the data-plane SDK this backend calls — is a hard dependency (it is still a preview, `0.1.0bN`, package; pin it in your own lockfile if you need reproducibility beyond the range this package declares). Authentication is `DefaultAzureCredential`; see [Azure Identity's docs](https://learn.microsoft.com/python/api/overview/azure/identity-readme) for how it resolves credentials in your environment.

## Threat model

**The VM boundary.** `AcaSandboxBackend` declares `Isolation.VM`: execution happens in a hardware-isolated microVM, not a shared-kernel container, which is what lets `maf-sandbox`'s router permit this backend when a host reports it is running deployed (see that package's README). Everything below this line assumes that boundary holds; it is a property of the Azure Container Apps Sandboxes service, not of this package's code.

**What identity is reachable.** No ambient identity is placed inside the sandbox — the control-plane credential this package uses to create and manage sandboxes (`DefaultAzureCredential`) never travels into the guest. Code running inside a sandbox has no path back to the host's Azure identity, the host process's environment, or any other conversation's sandbox: `dispose_scope` deletes by service-side label, not by trusting the caller, and egress is Deny-default with a per-spec allowlist supplied by the *kind*, not by runtime configuration — a deployment that could widen a kind's egress after the fact could undo the containment its design rests on.

## The backend

`AcaSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A warm sandbox is resumed rather than replaced, so a fix-round loop does not pay a cold start per iteration. |
| `dispose(key)` | delete one sandbox |
| `dispose_scope(scope, thread)` | delete every sandbox for a conversation — **from the service, by label**, not from process memory |
| `isolation` | `vm` — which is what lets the router permit it in a deployed environment |

That `dispose_scope` detail is the one worth reading twice. A multi-replica host serves a conversation delete wherever it lands, so the replica that created a sandbox is usually not the one deleting it. A backend that consults only its own registry leaves billable VMs running, and the bug is invisible on a single-replica dev box. Sandboxes are labelled at create time so the service can answer the question instead.

Egress comes from the **spec**, not from configuration: `default_action: Deny` plus one `Allow` rule per host the kind declares. A deployment that could widen a kind's egress could undo the containment its design rests on.

## Extracting this package

It imports nothing from its host application — only `maf-sandbox` and `azure-*` — so moving it to its own repository is a file move plus a dependency line. `src/`, `tests/`, `scripts/` and `pyproject.toml` are already the future repo root.

`TestNoHostDependency` is what keeps that true: it scans this package's sources for any import of the host package and fails with a pointer to the adapter. Nothing else would notice, because the tests run in a process where the host is importable. The host's name appears exactly once, as `_HOST_PACKAGE` in that test — extracting this repository is that one line plus a file move.

What stays behind is the host's adapter — one module in the host application (today `tools/bicep.py`) that maps the host's settings onto an `AcaSandboxConfig` and supplies the request context. Read it first if you want to know what integrating this package involves.

## Provenance

Built for issues [#408](https://github.com/sokolaidev/ats-maf/issues/408) and [#663](https://github.com/sokolaidev/ats-maf/issues/663) of the host application this was extracted from. The security analysis that chose a VM-isolated sandbox over in-process execution — including the verified escalation chain that ruled in-process out, and why `--no-restore` alone was not enough — is in that repository under `docs/work-in-progress/issue-408-exec-surface-security.md`.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
