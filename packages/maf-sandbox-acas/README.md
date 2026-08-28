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
| `capabilities` | `EXEC, FILES_IN, FILES_OUT, FILES_LIST, FILES_DELETE, HOST_TOOLS` — declares only what it implements today, and `FILES_OUT` and `HOST_TOOLS` are a ceiling `acquire` withdraws on an image whose guest is not root |
| `limits` | the transfer ceilings a spec may not exceed, per direction |

**Two image namespaces, and `spec.image` says which by whether it carries a tag.** The service prebuilds images and keeps them Ready for every sandbox group — `python-3.13`, `node-22`, `ubuntu` and a dozen more — and a spec reaches them by naming one, with **no registry and no tag**, because the version is part of the name. Anything else is the `repository:tag` the rest of this package is written around: qualified by the configured `registry` and resolved against the disk images this deployment imported with `scripts/import_disk_image.py`.

```python
codeact_sandbox_spec(image="python-3.13")           # the service already has it — import nothing
bicep_sandbox_spec(image="bicep-sandbox:0.46.1")    # yours, imported once, qualified by `registry`
```

The tag is what separates them, and it has to be: `bicep-sandbox:0.46.1` names no registry either, so a rule that looked only for a registry would swallow every deployment configuring an imported image the way `SandboxSpec` documents. A bare name the service does not have is refused before anything is created, with the catalogue in the message — the likely way to arrive at one is a forgotten tag, and the fix is then visible where the error is. `image_id` still skips both lookups, as the field promises.

Microsoft's docs call these *public images*, glossed as "prebuilt images available to all sandbox groups", in the same paragraph that calls Docker Hub a public registry. This package says **prebuilt** to keep those apart; the SDK spells them `list_public_disk_images()` and `begin_create_sandbox(disk=…)`.

`tests/test_acas_e2e.py` is the live suite, skipped unless `ACAS_SANDBOX_ENDPOINT` and `MAF_SANDBOX_ACAS_E2E_IMAGE` name a sandbox group and a guest image. It is what exercises the real data plane — the shared `FILES_OUT` conformance probes, the cap and confinement refusals, the read timeout that turns a fifo from a hang into a refusal, and teardown read back from the service rather than from this process's memory. It runs in `verify-live.yml`, not on a pull request, because every sandbox in it is billable. Most of it shares one sandbox; the prebuilt-image probes need a second, booted from `python-3.13` (override with `MAF_SANDBOX_ACAS_E2E_PREBUILT`), because a name from the catalogue is the thing they exist to prove boots.

**`Capability.FILES_LIST` as well as `FILES_OUT`, and this is the only backend that declares it.** The service enumerates a directory natively, which is the test the protocol's split applies — name the backend that lacks it. A kind whose output names are unpredictable is refused on Docker and wslc and served here.

**`Capability.HOST_TOOLS`, and what it claims is narrower than the others.** It is the only member of the vocabulary with no backend method behind it — the transport is composed by the *kind* out of `exec`, `write_file`, `stat_file` and `read_file`, all of which the capabilities above already cover. What this backend adds by declaring it is one property: **`exec` detaches.** A process started by one call outlives it and is still observable from the next, because the sandbox is a microVM the group keeps between calls, and `host_tool_calls_over_exec` is built on exactly that — its launcher returns at once and the appearance of the exit-code file is the run's only witness. That is measured against the service, not asserted: `TestWhetherThisBackendCouldServeHostTools` in the live suite watches the exit marker be absent when the launcher's `exec` returns and appear afterwards. It is **not** a claim about the image — the shipped launcher wants `sh`, `nohup`, `printf`, `mv`, `mkdir`, `rm` and `kill`, and `setsid` where the image has it, and a kind wants whatever interpreter it names, none of which this backend chooses ([#111](https://github.com/sokolaidev/maf-extensions/issues/111)).

**An image whose guest is not root cannot serve `FILES_OUT` or `HOST_TOOLS`, and `acquire` says so.** The two planes act as two principals: `write_file` lands files as `0:0` whatever the image's `USER` is, while `exec` runs as that `USER` and the SDK offers no selector to raise it. Every directory the file plane creates is therefore root-owned and `0755`, and a guest program on a non-root image can create nothing inside one — not a declared output, and not the pid and exit markers the host-tool launcher writes, which is why that transport cannot even start there ([#722](https://github.com/sokolaidev/maf-extensions/issues/722)). So `acquire` reads the guest's uid once per image, refuses a spec requiring either capability, warns about one requiring only `EXEC` — a command whose whole result is its stdout is unaffected — and serves an image whose uid it could not read, because refusing on an unreadable probe would take a working root image off a deployment. The wall is a directory the *file plane* made rather than the working directory as such — `/tmp` stays writable — and it bounds every shipped kind because each reaches the guest through a `write_file` that creates the call directory. It lands at `acquire` rather than at attach for the reason every image-shaped refusal does: nothing is running when the router matches a capability set.

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
