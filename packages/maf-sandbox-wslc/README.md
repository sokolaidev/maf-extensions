# maf-sandbox-wslc

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxWslcExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_wslc.MafSandboxWslcExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework).

```
app  ->  maf_sandbox  ->  maf_sandbox_wslc  ->  the container
```

The developer-machine sandbox backend: a container created by `wslc`, the container CLI that ships with WSL, in about half a second — no subscription, no daemon, no login, and no dependency but [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox) itself. A workload written against the protocol runs here unchanged, which is what makes it a workload rather than an integration.

For workloads requiring `EXEC` or any `FILES_*` capability, `acquire` ensures the bound storage base exists, including on warm reuse. `spec.work_dir=None` lets this backend allocate `/maf-sandbox/work`; an explicit value requires that exact guest-native base. Relative working directories resolve beneath it, with `"."` naming the base; commands and argv remain untouched. Existing directories retain their contents, ownership and modes; an unreadable path, a symlink or a non-directory fails acquire. This guarantees the base's existence on return, not additional guest permissions or the creation of per-call children. Runtime-only workloads require no directory. Missing directories are sent through the WSLC tar file plane: ancestors are root-owned and the base uses the resolved image uid/gid. An unresolved identity refuses creation of a missing base. No guest `mkdir` is needed; the file plane's documented concurrent-redirection residual also applies to creation.

## Quickstart

```bash
pip install maf-sandbox-wslc
```

```python
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

router = SandboxRouter([WslcSandboxBackend(WslcSandboxConfig())], min_isolation=Isolation.CONTAINER)
```

[`samples/02_wslc_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/02_wslc_bicep) runs those two lines end to end: a one-turn agent that validates a Bicep file against the compiler and takes the container down afterwards. Its sibling `samples/01_acas_bicep` is the same program on a microVM-isolated Azure backend, and the diff between them is two imports and one constructor.

## Requirements

**Windows with WSL 2.9.3 or later.** `wslc` is part of WSL; `wsl --version` reports the version and `wsl --update` moves it forward. There is nothing else to install. The command-line contract this backend depends on — argv passed to `exec` natively, `cp` from a tar on stdin, label filters on `list`, `WSLC_E_*` codes on stderr — was verified against **wslc 2.9.4.0**. Every call spawns `wslc.exe`, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so.

## What this backend declares

Acquire checks the image prerequisites for the requested capabilities: `sh` for `EXEC`, and the external `test` command plus resolved write ownership for `FILES_IN`. A failed check raises `SandboxCapabilityNotSupported` before the sandbox is handed out. Successful command checks are cached per engine instance; failed checks are retryable. These checks do not strengthen guest-answered path checks. See the [image command contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/guest-platform-and-commands.md#decision-3--a-static-ceiling-matched-at-attach-and-a-probe-at-acquire).

**`Isolation.CONTAINER`.** A container shares the host kernel and sits next to whatever the host process holds, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — construct the router with `min_isolation=Isolation.CONTAINER` and it admits this backend; leave the floor at its default and construction raises `SandboxBackendNotPermitted`. That refusal is the feature: this is a backend for the machine you are already sitting at, and opting the floor down is the one thing that lets you use it — there is no flag left to forget. Use a microVM-isolated backend where a deployment's credentials are in the picture.

**`Egress.CLOSED` by default, `Egress.ALLOWLIST` on request.** With no proxy configured every container is created `--network none`: the CLI cannot allow one host and deny the rest, so a spec's allowlist is honoured by denying everything — confining *more* than a workload asked for, which the router permits with a warning precisely because the failure is loud, and a workload built for this reports the shortfall rather than passing an incomplete result off as a clean one.

Set `egress_proxy_image` and the declaration becomes `ALLOWLIST`: each sandbox gets its own internal network and a dual-homed filtering proxy, and the spec's allowlist is enforced by topology — the container has no route out except the proxy, which opens a CONNECT tunnel only to the hosts the spec names. TLS is not decrypted, and the sandbox never resolves an external name itself. The proxy is shipped as source, not as an image you must trust: build it from the packaged recipe, whose only pinned dependency is its Azure Linux base.

```python
from pathlib import Path
from maf_sandbox_wslc import proxy_build_context, WslcSandboxConfig

print(f"wslc build -t maf-egress-proxy:local {proxy_build_context()}")  # run this once
config = WslcSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
```

Egress decisions are read before proxy removal and reported only once that removal succeeds or confirms absence. Failed or cancelled removals publish no egress event; a retry reads the surviving proxy again. Without a successful retry, that window remains unreported. See the [egress observation contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md) for attribution and delivery limits.

## The backend

`WslcSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent, call, kind)`. A running container is reused, a stopped one started, a missing one created — router-managed calls dispose it at cleanup, so the next call creates fresh. At `IsolationScope.CONVERSATION` the key's `call_id` is empty and one sandbox serves the conversation's calls; at `IsolationScope.CALL` it names the tool call, so no acquire repeats it and get-or-create finds nothing warm. |
| `write_file(path, content, *, working_directory)` | a confined tar on stdin to `cp - <container>:/`, with guest-owned file and missing-directory entries |
| `dispose(key, *, kind=None)` | Deletes the selected kind, or every kind when omitted; retained failures keep their kind for retries; includes proxies and networks |
| `dispose_scope(scope, thread)` | delete every container for a conversation — **by label, read back from wslc**, not from process memory |
| `reap(stopped_for, *, scope=None)` | operator retention for stopped workloads and orphan infrastructure; returns `WslcReapResult` with workload, proxy and network removal counts and failures |
| `isolation` | `container` — below the router's default `microvm` floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER` |
| `declarations.egress_modes` | `{closed}`, or `{closed, allowlist}` when `egress_proxy_image` is set — an internal network behind a filtering proxy, torn down with the sandbox |
| `declarations.capabilities` | `{EXEC, FILES_IN}` — a command line and files written in; nothing more |
| `declarations.isolation_scopes` | `{conversation, call}` — the key's `call_id` folds into the container name, the registry entry and the label a disposal selects on, so a spec asking for one sandbox per tool call is served rather than refused |
| `reclaim(...)` | refused: no branch of this engine's path check reports an owner, so nothing licenses a root delete; dispose the sandbox |
| `guest_principal` | diagnostic `root`, `unprivileged`, or `unknown`, from a bounded `id -u` probe at acquire |
| `declarations.os_families` | `{posix}` — a constant, because `wslc` runs Linux containers and has no other guest to hand out |

**Part of the filesystem path check on a write is answered inside the guest, and what that buys a hostile workload is bounded — that is the residual to know about before choosing this backend.** `write_file` refuses a path whose parents are links, which takes classifying every component from the filesystem root down. Measured on `wslc 2.9.4.0`, `container cp` refuses a missing path with `ERROR_PATH_NOT_FOUND` and a directory with a diagnostic of its own, and exits 0 for everything else. **Those two are the answers that let the check continue** — absent ends it, a directory descends — and both come from the engine, outside the container. What is left is which non-directory kind an exit-0 path is, and that is settled by `test` run in the container being confined, through core's own `maf_sandbox.paths.stat_by_asking_the_guest_as_root`, which spells the probe and its ordering once so that no backend in this position invents a fourth version. The probe runs as `--user 0`: the file plane writes as root, so a probe as the image's user would be blind above a directory only root can search, and a `cp` still lands bytes there. Root is asked for reach and never for trust, and the helper checks that reach rather than assuming it, since a uid is not a capability set.

**A workload running as root can replace `test` in its own image and be believed, so what it can buy with that is the thing to state.** A claim of a directory, or that nothing is there, contradicts a `cp` that accepted the source and is dropped: the component becomes `other`, which refuses a path through it exactly as a link does. So a lie picks which refusal a caller sees and never a path outside `working_directory`. At the leaf the guest's word is taken: a link is refused, and anything else is written over. A guest hiding a link at its own leaf gets the bytes on the link rather than on its target, because this file plane replaces a leaf link instead of following it. `maf-sandbox-docker` answers the whole question out of its engine, and this one will too if `container cp` ever grows a container-to-stdout form ([#125](https://github.com/sokolaidev/maf-extensions/issues/125), upstream [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310)) — a tar header names an entry type without asking anyone.

**A stat temporarily copies guest bytes onto the host.** The CLI has no container-to-stdout form: `container cp CONTAINER:path LOCAL_PATH` copies an accepted source into a private temporary directory for that stat. The backend removes that directory after the copy subprocess exits, including on errors, timeout and cancellation, before asking the guest to classify the entry. It never uses a file named `-` in the application working directory. **Disk use and copy I/O remain unbounded by `SandboxSpec.files_in` or the stdout limit**: overwriting a large existing file, or refusing a regular-file ancestor, can copy its full content into the host temporary filesystem first. Concurrent stats have separate directories; a host-process crash or a filesystem cleanup error can leave temporary data behind. Use a host temporary filesystem with an enforced quota when guest-chosen disk consumption is unacceptable. [#1133](https://github.com/sokolaidev/maf-extensions/issues/1133) records this containment boundary; avoiding the copy still needs a metadata or stdout surface from the engine.

**`declarations.os_families` is `{posix}`, and it is stated rather than read.** A workload names the guest shape its commands and scripts are written for in `SandboxSpec.requires_os_family`, and the router refuses a backend whose `os_families` does not hold it. `wslc` runs Linux containers in WSL 2's utility VM and has no other guest to hand out, so there is no engine to ask the way [`maf-sandbox-docker`](https://pypi.org/project/maf-sandbox-docker/) asks its daemon. The declaration is what this package's argv and its POSIX guest path handling already rest on. What it changes is one direction only: an undeclared `os_families` is the empty set, which refuses *every* spec that names a family, so a `posix` workload this backend could always have run was turned away at attach. A `windows` one is still refused here, as it should be — a backend that hands out Windows guests declares them and is matched instead.

**Cleanup is disposal for every workload, including one claiming `confined_to_guest_call_path`.** This backend withholds both `RECLAIM` and `SNAPSHOT`. The file plane writes as root, but the engine cannot establish whether the guest could replace a call directory's ancestors, so `reclaim` refuses alongside `remove`. The principal probe is a guest announcement used only for diagnostics; missing `id`, failed probes and malformed replies report `unknown`, and no reply licenses a privileged delete. It is read per acquire, with no memo keyed by a mutable image or container name. The shared core principal protocol and capability refusal remain separate work; this property does not claim engine-authenticated ownership.

Container names are derived from the key rather than remembered, so `acquire` and `dispose` agree on one without a registry to keep in sync. Labels are the durable record `dispose(key, kind=...)` and `dispose_scope` select on, and their values are digested when they are long or carry a separator — the same mapping on both sides, because transforming one and not the other makes a purge quietly select nothing.

`stop` is never used to tear a sandbox down. A container whose init process ignores `SIGTERM` takes ten seconds to stop and under a quarter of a second to remove, and there is nothing in a sandbox worth waiting for. The one place it is used is the *egress proxy*, and only where a host registered an observer: its record has to be closed before it is read, or a request answered between the read and the removal reaches nobody. That pays the same ten-second worst case, on the proxy alone, on an acquire that is already collecting records.

## Write ownership

Files and newly created directories at or below `working_directory` belong to the image's user. Each acquire reads `Config.User` through `wslc container inspect`: an empty user means `0:0`, and a numeric `uid:gid` is used directly. A named user or omitted group uses bounded `id -u` and `id -g` replies from the guest, so those images need a working `id`; an unresolved identity refuses `write_file`. Existing directories keep their ownership and modes, including setgid and sticky bits. Ancestors above `working_directory` receive no guest-owned tar entry. Ownership stamping lets the guest modify inputs and create outputs; the copy still acts with host authority, so it does not close the path-check or concurrent-redirection residual.

To verify non-root writes locally, set `MAF_SANDBOX_WSLC_E2E_IMAGE` to a runnable root image and `MAF_SANDBOX_WSLC_E2E_NONROOT_IMAGE` to a non-root image without `/maf-sandbox/work`. Build the guest-owned fixture with `wslc image build -t maf-sandbox-wslc-guest-owned:ci packages/maf-sandbox-wslc/tests/fixtures/guest-owned`, set `MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE=maf-sandbox-wslc-guest-owned:ci`, then run `uv run pytest packages/maf-sandbox-wslc/tests/test_wslc_e2e.py -k 'GuestThatIsNotRoot or guest_owned_work_dir' -q`. The tests check input modification, output and directory creation, preservation of an existing directory's metadata, and the shared reach probe with a writable non-root working directory. Each reads its image's own layout before acquiring and refuses a fixture that does not match, because acquire prepares the base and leaves both images looking alike afterwards. Containers are disposed after each test.

## Operator retention

`reap` discovers resources through WSLC, so a separate operator process can clean up after the application exits without its registry or conversation keys. It starts no timer. Save the following as an operator program, install this package in that program's environment, and run it under the Windows account that owns the WSLC engine:

```python
import asyncio
import json
from dataclasses import asdict
from datetime import timedelta

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

backend = WslcSandboxBackend(WslcSandboxConfig())
result = asyncio.run(backend.reap(timedelta(hours=24), scope="my-app"))
print(json.dumps(asdict(result)))
raise SystemExit(bool(result.failures))
```

**Pause and drain acquisitions, restarts, and other resource mutations in the selected scopes before running the sweep.** WSLC 2.9.3 addresses networks by name and offers no conditional identity or timestamp check on removal. The sweep rechecks identities, labels, workload presence and stop time, but these reads cannot close the interval before deletion. Workload removal uses its immutable ID without `--force`, so the engine refuses a workload that is running at deletion; a restart followed by another stop between inspection and deletion still requires operator coordination. Do not overlap sweeps. `scope` uses the same label encoding as creation; omit it only when maintenance covers all scopes on that engine.

Retention compares inspected UTC timestamps with the operator's Windows clock. Keep the Windows and WSL clocks synchronized: clock skew can advance or delay expiry by the offset. The helper does not measure or compensate for that offset.

The positive `stopped_for` duration applies these rules, with resources exactly at the cutoff retained:

| Resource present | Retention rule |
| --- | --- |
| Workload | `State.Status == "exited"`, `State.Running == false`, and `State.FinishedAt` older than the cutoff. A restart and stop resets this interval. A never-started container in `created` state uses `Created`. Running, transitioning and unknown workload states preserve the whole group. |
| Proxy with no workload | Its `Created` timestamp must precede the cutoff. This is an explicit maximum creation age for orphan infrastructure, including running proxies; it is not a workload inactivity claim. |
  | Network with no workload or proxy | Its `maf-sandbox.network-created-at` label, written when the backend requests network creation, must precede the cutoff. Legacy networks without that label are retained and reported for manual cleanup. |

An expired workload is removed first. Only successful removal permits deleting its proxy and network, even if they were rebuilt more recently. An expired orphan proxy similarly permits removing its network. Workload absence is checked again before each infrastructure removal. Proxies are force-removed by inspected ID; networks are never forcibly disconnected from attached containers. An eligible proxy confirmed absent by both ID and name permits network cleanup without incrementing the proxy-removal count. A replacement proxy is retained, and a workload that disappears stops cleanup of its group. Only backend-shaped names with all four identity labels qualify, and grouped resources must share those labels. Missing or malformed age metadata retains the affected group with a failure; an incomplete inventory prevents all deletion. Failure counts include infrastructure, while `disposed` counts workloads only. Repeated sweeps tolerate resources already gone and reevaluate the resources left behind: a recently rebuilt proxy or network may need to age before a later retry qualifies it independently.

Failure codes preserve the source: `unlisted` covers failed queries and invalid inventory, ownership or age metadata; `unreachable` covers command invocation exceptions; `timeout` means the command's outcome is unknown; and `refused` means deletion was rejected and inspection confirmed the resource still exists.

The egress drain recovers the key from the proxy's engine labels and reads its log by the inspected ID before removal, including from a fresh operator process. New proxies carry a lossless `maf-sandbox.key.v1` label when its encoded value fits within 4,096 bytes; existing ownership labels and cleanup filters are unchanged. Larger keys still acquire normally: the backend warns and writes an empty attribution label, so scope purges and reaping cannot recover their keys. Key-addressed disposal and acquire can still drain them using the caller's key, which stands in only where there is no attribution to read — an absent or empty label, never a present one that was refused — and only where the ownership labels name that key, call included. A swept leftover is reported under the key that ran behind that proxy, and one whose attribution cannot be recovered at all is left unreported rather than filed under the conversation that swept it. Legacy proxies with plain ownership values remain attributable, while hashed values cannot be recovered without the caller's key. Missing or malformed attribution yields no event, and absence alone does not establish a lost window. Failed or cancelled removals publish no event, and a successful sequential retry reports its window once. Overlapping cleanup can still report the same window more than once; see the [egress observation contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md). Retention does not stop running workloads and does not infer inactivity from host death. An operator needing a maximum running lifetime must supply that policy separately.

Use Windows Task Scheduler, or a Windows runner explicitly connected to the same engine, to execute the program during a maintenance window. Set its working directory, Python environment and `wslc` executable path explicitly, prevent overlapping runs, and monitor nonzero exits. Running as a different account or on a GitHub-hosted runner does not reach the developer's WSLC resources. A missed run extends retention. The scheduler and maintenance coordination belong to the deployment; see [cleanup ownership](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/operations.md).

The inspection contract comes from Microsoft's [WSLC 2.9.3 container implementation](https://github.com/microsoft/WSL/blob/2.9.3/src/windows/wslcsession/WSLCContainer.cpp) and [network implementation](https://github.com/microsoft/WSL/blob/2.9.3/src/windows/wslcsession/WSLCSession.cpp). Container inspection exposes timezone-qualified `Created` and `State.FinishedAt`; the reaper uses these instead of numeric listing fields. It accepts both 2.9.3 JSON arrays and the JSON-lines listings introduced by newer CLIs, including the [2.9.10 container output](https://github.com/microsoft/WSL/blob/2.9.10/src/windows/wslc/tasks/ContainerTasks.cpp). Offline tests cover these shapes and retention races.

Live verification on 2026-09-08 used WSLC 2.9.4.0 and Python 3.13.12. The array listing's `CreatedAt` matched Unix seconds from inspected `Created`; `StateChangedAt` was also in Unix seconds but could fall in the second after inspected `State.FinishedAt`. [WSLC 2.9.4](https://github.com/microsoft/WSL/blob/2.9.4/src/windows/wslcsession/WSLCContainer.cpp) records state-change events separately from the inspected process exit. Inspection retained fractional seconds and a UTC suffix. Four separate-process probes passed: closed and allowlisted stopped workloads, an orphan proxy with its network, and a network alone. Running, freshly stopped and out-of-scope workloads and their infrastructure survived, and repeat sweeps removed nothing. Two further probes removed the proxy through WSLC just before its revalidation or removal, confirming that network cleanup continues. The WSL lifecycle clock was about five seconds ahead of Windows; the probes allow a bounded offset and wait until the inspected timestamp passes the operator's cutoff. WSLC 2.9.3 and 2.9.10 remain source-checked and covered offline, not measured live.

To repeat the live cleanup probes, set `MAF_SANDBOX_WSLC_E2E_IMAGE` to a runnable image and `MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE` to a built proxy image, then run `uv run pytest packages/maf-sandbox-wslc/tests/test_wslc_e2e.py -k reap -q`. The separate-process probes abruptly exit the creator before another process sweeps the isolated test scope with a 30-second retention period, and include an out-of-scope control. The race probes remove a proxy during a sweep. All probes remove their resources afterward.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).

## Upgrading to 0.13

**The four optional declarations moved into one `BackendDeclarations`.** `maf-sandbox` 0.26 replaced `capabilities`, `limits`, `egress_modes` and `os_families` as backend attributes with one `declarations` object holding them as fields, and this backend follows it. A host that read them off the backend gets an `AttributeError`:

| Was | Is |
| --- | --- |
| `backend.capabilities` | `backend.declarations.capabilities` |
| `backend.egress_modes` | `backend.declarations.egress_modes` |

`limits` is not in that table because this backend never declared one — the router read its silence as `DEFAULT_SANDBOX_LIMITS`, and there was no `backend.limits` to read. `backend.declarations.limits` now answers with that same constant, so the ceiling is unchanged and the value is newly *reachable* rather than renamed.

Nothing about what this backend declares changed — the values, and how they are derived from the config, are exactly as they were. `maf-sandbox`'s own README carries the reasoning and what a backend author has to do.

## Exec bytes and text views

`ExecResult.stdout_bytes` and `stderr_bytes` preserve returned program bytes; `stdout_text` and `stderr_text` (also `stdout` and `stderr`) are UTF-8 display views with replacement decoding. Use the byte fields for artifacts and byte counts, and the text views for model or JSON display. See the [output contract, ACAS prerequisites and release migration](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/exec-output.md).
