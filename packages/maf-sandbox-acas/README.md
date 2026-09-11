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

For workloads requiring `EXEC` or any `FILES_*` capability, `acquire` ensures the bound storage base exists, including on warm reuse. `spec.work_dir=None` lets this backend allocate `/maf-sandbox/work`; an explicit value requires that exact guest-native base. Relative working directories resolve beneath it, with `"."` naming the base; commands and argv remain untouched. Existing directories retain their contents, ownership and modes; an unreadable path, a symlink or a non-directory fails acquire. This guarantees the base's existence on return, not additional guest permissions or the creation of per-call children. Runtime-only workloads require no directory. Missing parents are created through the SDK's data-plane `mkdir`, without a guest command. Ownership follows the service's file plane; its documented concurrent-redirection residual also applies to creation.

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

Acquire checks byte capture for both `EXEC` and `HOST_TOOLS`: working `sh`, `mkdir`, `mkfifo`, `head`, `cat`, `wc`, `dd`, `base64`, `rm` and `rmdir`, plus writable `/tmp`. `HOST_TOOLS` also requires `mv` and `nohup`. These checks accompany the existing observed-removal gate. Missing prerequisites raise `SandboxCapabilityNotSupported`; successful command checks are cached with that sandbox, and failed checks are retryable. A failed capture probe invalidates and attempts to dispose the sandbox. The interpreter remains the workload's choice, and `setsid` stays optional. See the [image command contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/guest-platform-and-commands.md#decision-3--a-static-ceiling-matched-at-attach-and-a-probe-at-acquire).

`AcasSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent, kind)`. Equivalent egress policies reuse a warm sandbox; changed hosts or mode on a usable instance raise `AcasEgressPolicyConflict`. Dispose the kind before changing policy, or use another key. |
| `dispose(key, *, kind=None)` | Deletes the selected kind, or every kind when omitted; retained failures keep their kind for retries; reaches sandboxes known to this process |
| `dispose_scope(scope, thread)` | delete every sandbox for a conversation — **from the service, by label**, not from process memory; report an incomplete purge if a local acquire is active, and refuse new local acquires during the purge |
| `stat_file` / `read_file` / `list_dir` | the pull surface — reads confined to the call's `working_directory`, symlinks and directories refused, a size over the caller's cap refused rather than truncated. Regularity itself cannot be proven here — see below |
| `isolation` | `microvm` — the router's default floor, so a host that configures nothing already permits this backend |
| `declarations.capabilities` | `EXEC, FILES_IN, FILES_OUT, FILES_LIST, FILES_DELETE, HOST_TOOLS` are a ceiling. `acquire` withdraws `FILES_OUT` and `HOST_TOOLS` on a failed guest removal probe and withdraws `FILES_DELETE` unless the file plane confirms the guest removed the probe file |
| `declarations.limits` | the transfer ceilings a spec may not exceed, per direction |
| `declarations.os_families` | `{posix}` — a constant, because every sandbox the service boots is a Linux microVM |

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

**Guest removal compatibility is checked at acquire.** The file plane writes as root, while `exec` runs as the image's `USER`. The backend plants a probe file in a fresh root-owned directory under `/`, asks the guest to remove it with `rm`, and checks the result through the file plane. The file must be gone and its directory must remain. Cleanup runs through the file plane even on cancellation. Preparation, execution and observation share a 30-second timeout, followed by separately bounded cleanup. This checks the image's `rm`; it cannot establish the authority of ordinary workload code.

A completed removal failure refuses `FILES_OUT` and `HOST_TOOLS` and warns an `EXEC`-only workload, conservatively screening guests unable to write beside uploaded files ([#722](https://github.com/sokolaidev/maf-extensions/issues/722)). An inconclusive result serves that functional pair, but `FILES_DELETE` requires an observed removal. Each sandbox keeps its own compatibility result for warm reuse; transient failures are retried. An image-level hint can refuse before a create for 60 seconds from its completed probe; cached refusals do not extend that deadline. After expiry the next cold acquire creates and probes again, so a repaired catalogue image can recover without restarting the host. Every new sandbox that needs the probe must pass its own check. A warm sandbox retains its own verdict: dispose it to acquire from a repaired image, allowing any image hint to expire first.

**Every `remove` runs as the guest.** It executes `rm -f -- <path>` or `rm -rf -- <path>` over guest `exec`, then confirms absence through the file plane. A failed command or an entry still present raises `OSError`; command execution and observation are bounded by `read_timeout_seconds`. A missing path needs no command. The image controls `rm` and could supply a privileged wrapper that passes only the probe, so the probe never authorizes a host-plane delete. Path checks still refuse symlinked parents, but are not held across execution: a swapped parent can redirect removal within the guest's existing reach. `Sandbox.reclaim` raises `NotImplementedError` before any service call because safe ancestry for a host-authority delete cannot be established. ACAS withholds `RECLAIM` and `SNAPSHOT`, so router-managed cleanup disposes the sandbox. Direct callers must dispose it as well.

**`write_file` keeps the same window and is not withheld — know it before choosing this backend for a non-root image.** A parent swapped between the check and the write lands the bytes root-owned wherever the link points. It is stated rather than refused for two reasons: the protocol states the reach rule for removals and says nothing yet about writes ([#951](https://github.com/sokolaidev/maf-extensions/issues/951)), and withholding `FILES_IN` would leave this backend no in-door at all on such an image. **A root image does not remove it**, and the two things it might be wanted for come apart here. Running root removes the *privilege increase* — the bytes land where the guest could have put them itself — and leaves the *confinement failure* exactly as it was: the write still goes outside `working_directory`, which is what a host calling `write_file` with a confined path was promised. A deployment that needs the first can run a root image. A deployment that needs the second cannot get it from this backend on any image, and should treat the sandbox as disposable after a run rather than as a boundary that held.

**`declarations.os_families` is `{posix}`, and it is stated rather than read.** A workload names the guest shape its commands and scripts are written for in `SandboxSpec.requires_os_family`, and the router refuses a backend whose `os_families` does not hold it. Every sandbox this backend hands out is a Linux microVM — from the prebuilt catalogue or from a disk image imported into the group, since the service boots nothing else — so there is no daemon to ask and nothing to probe inside the guest, the way [`maf-sandbox-docker`](https://pypi.org/project/maf-sandbox-docker/) has to. The declaration is what `exec`'s `shlex.join` quoting and this package's `posixpath` path arithmetic already rest on. What it changes is one direction only: an undeclared `os_families` is the empty set, which refuses *every* spec that names a family, so a `posix` workload this backend could always have run was turned away at attach. A `windows` one is still refused here, as it should be — a backend that hands out Windows guests declares them and is matched instead.

**Only regular files are read, and the refusal happens at stat time.** This backend's read *follows* symlinks: a path linking to `/etc/hostname` returns that file's contents, so classifying after the bytes come back would be too late. The type comes from the data-plane payload's `isSymlink` and `isDir` flags, read raw — the SDK's typed `FileInfo` exposes neither, and a payload missing them is refused as `AcasEntryPayloadIncomplete`, never assumed regular ([#136](https://github.com/sokolaidev/maf-extensions/issues/136)).

**What the type check cannot prove.** `isDir` and `isSymlink` establish that an entry is *neither* of those; they do not establish that it is a regular file, and `mode` is permission bits with the type stripped. A FIFO is reported identically to an empty regular file and is classified `FILE` — and reading one never returns, so `read_timeout_seconds` bounds it and a hang becomes a refusal rather than a held-open turn. The missing signal is filed as [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807).

**Every path component is checked, not just the last one.** A guest that points `out` at `/etc` gets a stat of `out/hostname` that says "regular file, 12 bytes" — the parent link is invisible there — so `stat_file`, `read_file` and `list_dir` stat every parent component from the **filesystem root** down, not from the working directory, whose own ancestors the guest can replace just as easily: with `/maf-sandbox -> /` unchecked, `/maf-sandbox/work` stats as a real directory and serves `/`. A link anywhere among those ancestors is refused as an escape; any other non-directory is an ordinary `ENOTDIR`. The check is `maf_sandbox.paths.refuse_symlinked_ancestors`, shared with every other backend serving `FILES_OUT`; what this package supplies is the unconfined, no-follow stat it runs on. Only the parents are refused — a link as the **final** component is still described as `SYMLINK`, which is how a caller learns it is one. One residual stays open and cannot be closed with this API: the stat and the read are separate calls, and the service has no no-follow read, so a guest that swaps a stat-ed file for a symlink in between is followed.

That `dispose_scope` detail is the one worth reading twice. A multi-replica host serves a conversation delete wherever it lands, so the replica that created a sandbox is usually not the one deleting it. A backend that consults only its own registry leaves billable sandboxes running, and the bug is invisible on a single-replica dev box. Sandboxes are labelled at create time so the service can answer the question instead.

Cleanup admission also applies to direct backend callers. Acquire raises `SandboxOutputError` while retained deletion remains unsuccessful or scope purge is active. If an acquire for that scope and thread is already active, scope purge returns an incomplete result with code `unknown`; retry purge after acquisition finishes. An admitted purge refuses new acquires for its scope and thread until all overlapping purges finish, including cancellation cleanup. Other scopes and threads remain available. Hosts must stop new work across replicas before conversation deletion because this barrier belongs to one backend object.

Egress comes from the **spec**, not from configuration: `default_action: Deny` plus one `Allow` rule per host the kind declares. A deployment that could widen a kind's egress could undo the containment its design rests on.

`AcasEgressPolicyConflict` subclasses `SandboxEgressNotEnforced`, so callers can distinguish a held-policy conflict from an unsupported mode while existing catches still work. Warm reuse compares the mode and case-insensitive host set with the policy used to create that sandbox. Acquisition for the same key and kind is serialized across event loops; unrelated keys and kinds can progress concurrently. Host order and equivalent spelling do not force a new sandbox. A mismatch refuses while retaining the original instance for its existing users and disposal; it never replaces a live instance automatically. Coordinate active calls, await `router.dispose_kind(key, spec.kind, timeout=60)` and require `True` before acquiring a changed policy, or choose a different key. Direct backend callers can await `backend.dispose(key, kind=spec.kind)` and require `None` (no disposal failure) before changing policy. A stale held record also requires explicit disposal; a failed resume would not prove the instance is gone. A capture-invalidated instance instead follows the deletion retry path, which must succeed before replacement under any policy. Acquire refuses if invalidation precedes its final guarded check, including during work-directory preparation; retry acquire to recover. Later invalidation can still dispose an accepted instance.

`EGRESS_METHODS` remains unsupported. The live service matches methods case-insensitively, including custom verbs, so it cannot enforce core's literal method contract. Both router matching and direct backend acquisition refuse method-scoped rules with `SandboxCapabilityNotSupported`. A GET-only rule also permits request content; it is not a body-free or read-only channel. The [live measurements](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/acas-egress-methods.md) record the distinction.

## Upgrading to 0.15

**The four optional declarations moved into one `BackendDeclarations`.** `maf-sandbox` 0.26 replaced `capabilities`, `limits`, `egress_modes` and `os_families` as backend attributes with one `declarations` object holding them as fields, and this backend follows it. A host that read them off the backend gets an `AttributeError`:

| Was | Is |
| --- | --- |
| `backend.capabilities` | `backend.declarations.capabilities` |
| `backend.limits` | `backend.declarations.limits` |
| `backend.egress_modes` | `backend.declarations.egress_modes` |

Nothing about what this backend declares changed — the values, and how they are derived from the config, are exactly as they were. `maf-sandbox`'s own README carries the reasoning and what a backend author has to do.

## Extracting this package

It imports nothing from its host application — only `maf-sandbox` and `azure-*` — so moving it to its own repository is a file move plus a dependency line. `src/`, `tests/`, `scripts/` and `pyproject.toml` are already the future repo root.

`TestOnlyDeclaredDependencies` is what keeps that true: it scans this package's sources and fails on any import that is neither the standard library, this package itself, nor a distribution its own `pyproject.toml` declares. Nothing else would notice a stray one, because a workspace has every sibling already on the path — and an undeclared import is exactly what breaks a fresh `pip install` of the published wheel.

What stays behind is the host's adapter — a single module in the host application that maps the host's settings onto an `AcasSandboxConfig` and supplies the request context. Read it first if you want to know what integrating this package involves.

## Provenance

Extracted from a production agent application, where a security review chose a microVM-isolated sandbox over running agent-authored code in the host process. Both halves of that conclusion are visible in this backend's design: the boundary it declares, and the fact that no credential of the host's ever travels inside it.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).

## Exec bytes and text views

`ExecResult.stdout_bytes` and `stderr_bytes` preserve returned program bytes; `stdout_text` and `stderr_text` (also `stdout` and `stderr`) are UTF-8 display views with replacement decoding. Use the byte fields for artifacts and byte counts, and the text views for model or JSON display. See the [output contract, ACAS prerequisites and release migration](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/exec-output.md).
