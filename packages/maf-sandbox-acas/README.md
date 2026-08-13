# maf-sandbox-acas

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-acas)](https://pypi.org/project/maf-sandbox-acas/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Renamed.** This package was published as `maf-sandbox-aca` up to `0.2.3`. ACA is Azure Container *Apps*, the broad service, while this backend targets Azure Container Apps **Sandboxes** — so it gained the `s` the rest of the project already used. PyPI names cannot be reused, so this is a new distribution rather than a continuation, and there is no compatibility shim: `maf_sandbox_aca` and the `Aca…` classes do not forward here. See [#68](https://github.com/sokolaidev/maf-extensions/issues/68).

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxAcasExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_acas.MafSandboxAcasExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework), built on the [Azure Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview.

```
app  ->  maf_sandbox  ->  maf_sandbox_acas  ->  the sandbox
```

An agent that writes code should not be the thing that runs it. This package gives it somewhere else to run: a microVM-isolated sandbox with Deny-default egress and no ambient identity, reached as an ordinary tool call so the agent framework's middleware still sees the call and classifies its result — only the *work* leaves the process.

This package is the backend only, with no sandbox kind of its own. [`maf-sandbox-bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-bicep) is the first kind that runs on it, written against [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s protocol rather than against this backend.

## Quickstart

```bash
pip install maf-sandbox-acas
```

```python
from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox import SandboxRouter

backend = AcasSandboxBackend(AcasSandboxConfig(endpoint="https://management.<region>.azuredevcompute.io", subscription_id="<sub-id>", resource_group="<rg>", sandbox_group="<group>", registry="<acr>.azurecr.io"))
router = SandboxRouter([backend])  # microVM isolation meets the router's default floor
```

[`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) runs that pair end to end: the same two lines, plus the caller context and the workload tool they exist to serve, in a program that validates a Bicep file and disposes the sandbox afterwards.

`azure-containerapps-sandbox` — the data-plane SDK this backend calls — is a hard dependency (it is still a preview, `0.1.0bN`, package; pin it in your own lockfile if you need reproducibility beyond the range this package declares). Authentication is `DefaultAzureCredential`; see [Azure Identity's docs](https://learn.microsoft.com/python/api/overview/azure/identity-readme) for how it resolves credentials in your environment.

## Threat model

**The micro-VM boundary.** `AcasSandboxBackend` declares `Isolation.MICROVM`: execution happens in a hardware-isolated microVM, not a shared-kernel container, and that rung is `maf-sandbox`'s router's default floor — a host that configures nothing already permits this backend (see that package's README). Everything below this line assumes that boundary holds; it is a property of the Azure Container Apps Sandboxes service, not of this package's code.

**What identity is reachable.** No ambient identity is placed inside the sandbox — the control-plane credential this package uses to create and manage sandboxes (`DefaultAzureCredential`) never travels into the guest. Code running inside a sandbox has no path back to the host's Azure identity, the host process's environment, or any other conversation's sandbox: `dispose_scope` deletes by service-side label, not by trusting the caller, and egress is Deny-default with a per-spec allowlist supplied by the *kind*, not by runtime configuration — a deployment that could widen a kind's egress after the fact could undo the containment its design rests on.

## The backend

`AcasSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A warm sandbox is resumed rather than replaced, so a fix-round loop does not pay a cold start per iteration. |
| `dispose(key)` | delete one sandbox |
| `dispose_scope(scope, thread)` | delete every sandbox for a conversation — **from the service, by label**, not from process memory |
| `stat_file` / `read_file` / `list_dir` | the pull surface — reads confined to the call's `working_directory`, symlinks and directories refused, a size over the caller's cap refused rather than truncated. Regularity itself cannot be proven here — see below |
| `isolation` | `microvm` — the router's default floor, so a host that configures nothing already permits this backend |
| `capabilities` | `EXEC, FILES_IN, FILES_OUT, FILES_LIST` — declares only what it implements today |
| `limits` | the transfer ceilings a spec may not exceed, per direction |

**`Capability.FILES_LIST` as well as `FILES_OUT`, and this is the only backend that declares it.** The service enumerates a directory natively, which is the test the protocol's split applies — name the backend that lacks it. A kind whose output names are unpredictable is refused on Docker and wslc and served here.

**Only regular files are read, and the refusal happens at stat time.** This backend's read *follows* symlinks: a path linking to `/etc/hostname` returns that file's contents, so classifying after the bytes come back would be too late. The type comes from the data-plane payload's `isSymlink` and `isDir` flags, read raw — the SDK's typed `FileInfo` exposes neither, and a payload missing them is refused as `AcasEntryPayloadIncomplete`, never assumed regular ([#136](https://github.com/sokolaidev/maf-extensions/issues/136)).

**What the type check cannot prove.** `isDir` and `isSymlink` establish that an entry is *neither* of those; they do not establish that it is a regular file, and `mode` is permission bits with the type stripped. A FIFO is reported identically to an empty regular file and is classified `FILE` — and reading one never returns, so `read_timeout_seconds` bounds it and a hang becomes a refusal rather than a held-open turn. The missing signal is filed as [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807).

**Every path component is checked, not just the last one.** A guest that points `out` at `/etc` gets a stat of `out/hostname` that says "regular file, 12 bytes" — the parent link is invisible there — so `stat_file`, `read_file` and `list_dir` stat every parent component from the **filesystem root** down, not from the working directory, whose own ancestors the guest can replace just as easily: with `/maf-sandbox -> /` unchecked, `/maf-sandbox/work` stats as a real directory and serves `/`. A link anywhere in that chain is refused as an escape; any other non-directory is an ordinary `ENOTDIR`. The walk is `maf_sandbox.paths.refuse_symlinked_parents`, shared with every other backend serving `FILES_OUT`; what this package supplies is the unconfined, no-follow stat it runs on. Only the parents are refused — a link as the **final** component is still described as `SYMLINK`, which is how a caller learns it is one. One residual stays open and cannot be closed with this API: the stat and the read are separate calls, and the service has no no-follow read, so a guest that swaps a stat-ed file for a symlink in between is followed.

That `dispose_scope` detail is the one worth reading twice. A multi-replica host serves a conversation delete wherever it lands, so the replica that created a sandbox is usually not the one deleting it. A backend that consults only its own registry leaves billable sandboxes running, and the bug is invisible on a single-replica dev box. Sandboxes are labelled at create time so the service can answer the question instead.

Egress comes from the **spec**, not from configuration: `default_action: Deny` plus one `Allow` rule per host the kind declares. A deployment that could widen a kind's egress could undo the containment its design rests on.

## Extracting this package

It imports nothing from its host application — only `maf-sandbox` and `azure-*` — so moving it to its own repository is a file move plus a dependency line. `src/`, `tests/`, `scripts/` and `pyproject.toml` are already the future repo root.

`TestOnlyDeclaredDependencies` is what keeps that true: it scans this package's sources and fails on any import that is neither the standard library, this package itself, nor a distribution its own `pyproject.toml` declares. Nothing else would notice a stray one, because a workspace has every sibling already on the path — and an undeclared import is exactly what breaks a fresh `pip install` of the published wheel.

What stays behind is the host's adapter — a single module in the host application that maps the host's settings onto an `AcasSandboxConfig` and supplies the request context. Read it first if you want to know what integrating this package involves.

## Provenance

Extracted from a production agent application, where a security review chose a microVM-isolated sandbox over running agent-authored code in the host process. Both halves of that conclusion are visible in this backend's design: the boundary it declares, and the fact that no credential of the host's ever travels inside it.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
