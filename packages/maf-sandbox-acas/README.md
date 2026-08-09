# maf-sandbox-acas

> **Renamed.** This package was published as `maf-sandbox-aca` up to `0.2.3`. ACA is Azure Container *Apps*, the broad service, while this backend targets Azure Container Apps **Sandboxes** — so it gained the `s` the rest of the project already used. PyPI names cannot be reused, so this is a new distribution rather than a continuation, and there is no compatibility shim: `maf_sandbox_aca` and the `Aca…` classes do not forward here. See [#68](https://github.com/sokolaidev/maf-extensions/issues/68).

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxAcasExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_acas.MafSandboxAcasExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework), built on the [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview.

```
app  ->  maf_sandbox  ->  maf_sandbox_acas  ->  the sandbox
```

An agent that writes code should not be the thing that runs it. This package gives it somewhere else to run: a VM-isolated sandbox with Deny-default egress and no ambient identity, reached as an ordinary tool call so the agent framework's middleware still sees the call and classifies its result — only the *work* leaves the process.

This package is the backend only, with no sandbox kind of its own. [`maf-sandbox-bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-bicep) is the first kind that runs on it, written against [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s protocol rather than against this backend.

## Quickstart

```bash
pip install maf-sandbox-acas
```

```python
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox import SandboxRouter

backend = AcasSandboxBackend(AcasSandboxConfig(endpoint="https://management.<region>.azuredevcompute.io", subscription_id="<sub-id>", resource_group="<rg>", sandbox_group="<group>", registry="<acr>.azurecr.io"))
router = SandboxRouter([backend], deployed=True)  # VM isolation is what makes `deployed=True` permitted here
```

[`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) runs that pair end to end: the same two lines, plus the workspace context and the workload tool they exist to serve, in a program that validates a Bicep file and disposes the sandbox afterwards.

`azure-containerapps-sandbox` — the data-plane SDK this backend calls — is a hard dependency (it is still a preview, `0.1.0bN`, package; pin it in your own lockfile if you need reproducibility beyond the range this package declares). Authentication is `DefaultAzureCredential`; see [Azure Identity's docs](https://learn.microsoft.com/python/api/overview/azure/identity-readme) for how it resolves credentials in your environment.

## Threat model

**The VM boundary.** `AcasSandboxBackend` declares `Isolation.VM`: execution happens in a hardware-isolated microVM, not a shared-kernel container, which is what lets `maf-sandbox`'s router permit this backend when a host reports it is running deployed (see that package's README). Everything below this line assumes that boundary holds; it is a property of the Azure Container Apps Sandboxes service, not of this package's code.

**What identity is reachable.** No ambient identity is placed inside the sandbox — the control-plane credential this package uses to create and manage sandboxes (`DefaultAzureCredential`) never travels into the guest. Code running inside a sandbox has no path back to the host's Azure identity, the host process's environment, or any other conversation's sandbox: `dispose_scope` deletes by service-side label, not by trusting the caller, and egress is Deny-default with a per-spec allowlist supplied by the *kind*, not by runtime configuration — a deployment that could widen a kind's egress after the fact could undo the containment its design rests on.

## The backend

`AcasSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A warm sandbox is resumed rather than replaced, so a fix-round loop does not pay a cold start per iteration. |
| `dispose(key)` | delete one sandbox |
| `dispose_scope(scope, thread)` | delete every sandbox for a conversation — **from the service, by label**, not from process memory |
| `isolation` | `vm` — which is what lets the router permit it in a deployed environment |

That `dispose_scope` detail is the one worth reading twice. A multi-replica host serves a conversation delete wherever it lands, so the replica that created a sandbox is usually not the one deleting it. A backend that consults only its own registry leaves billable sandboxes running, and the bug is invisible on a single-replica dev box. Sandboxes are labelled at create time so the service can answer the question instead.

Egress comes from the **spec**, not from configuration: `default_action: Deny` plus one `Allow` rule per host the kind declares. A deployment that could widen a kind's egress could undo the containment its design rests on.

## Extracting this package

It imports nothing from its host application — only `maf-sandbox` and `azure-*` — so moving it to its own repository is a file move plus a dependency line. `src/`, `tests/`, `scripts/` and `pyproject.toml` are already the future repo root.

`TestOnlyDeclaredDependencies` is what keeps that true: it scans this package's sources and fails on any import that is neither the standard library, this package itself, nor a distribution its own `pyproject.toml` declares. Nothing else would notice a stray one, because a workspace has every sibling already on the path — and an undeclared import is exactly what breaks a fresh `pip install` of the published wheel.

What stays behind is the host's adapter — a single module in the host application that maps the host's settings onto an `AcasSandboxConfig` and supplies the request context. Read it first if you want to know what integrating this package involves.

## Provenance

Extracted from a production agent application, where a security review chose a VM-isolated sandbox over running agent-authored code in the host process. Both halves of that conclusion are visible in this backend's design: the boundary it declares, and the fact that no credential of the host's ever travels inside it.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
