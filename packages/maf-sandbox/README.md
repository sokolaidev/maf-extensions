# maf-sandbox

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox.MafSandboxExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568), written for use with [Microsoft Agent Framework](https://aka.ms/AgentFramework) but with no dependency on it in its protocol layer.

## Quickstart

```bash
pip install maf-sandbox
```

```python
from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec, CallerContext

# Implement SandboxBackend against your own provider — or install maf-sandbox-acas for a
# ready-made Azure Container Apps Sandboxes backend — then wire it into a router. Configuring
# nothing gets the production posture (the default floor is Isolation.MICROVM); a developer
# machine opts down explicitly:
router = SandboxRouter([my_backend], min_isolation=Isolation.CONTAINER)
sandbox = await router.acquire(SandboxKey(scope="tenant-1", thread_id="t-1", agent_dir="devops"), SandboxSpec(kind="bicep", image="bicep-sandbox:0.46.1", egress_allow=("mcr.microsoft.com",), work_dir="/workspace"))
```

This snippet never calls `ensure_can_serve` (below) and is checked anyway: `acquire` runs the same floor, capability and egress refusals itself before it ever reaches the backend, so the only thing calling `ensure_can_serve` first buys you is the closed-egress-vs-allowlist-spec warning, which `acquire` deliberately stays silent about.

[`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) is that wiring as a runnable program, including the part no snippet shows well: building the `CallerContext` out of callables rather than values, which is what keeps a `SandboxKey` a property of the host's request.

## Threat model

This package draws no isolation boundary itself — it is protocol and policy over whatever a `SandboxBackend` implementation actually provides. `Isolation` is a seven-rung ladder a backend declares itself onto, weakest to strongest: `none` (no boundary at all — the workload runs in the host process, with the host's authority), `runtime` (a software boundary inside the host process, e.g. a restricted interpreter or a WASM runtime's fault isolation), `os_process` (a separate OS process — a kernel-enforced address space, sharing the kernel and the filesystem), `container` (shared-kernel namespaces and cgroups), `hardened_container` (syscall interception in a userspace kernel — gVisor-class), `microvm` (a hypervisor boundary with a minimal or absent guest OS and no ambient identity reachable from inside — the default floor), and `vm` (a dedicated, full VM provisioned for the workload). `SandboxRouter` enforces the checks below on top of that declaration; the package's job is to make an unsafe backend selection fail loudly at construction or attach, not silently at first use. Beyond backend selection this layer holds no credentials, executes nothing and reaches no network, and everything security-relevant about a *specific* sandbox lives in the backend that implements it. It has exactly one boundary of its own, and it is on the way out rather than in: `make_file_system_sink` writes guest-produced bytes under a host directory, so it resolves each destination and refuses one that leaves that directory — see *Getting files back* below, which is also where a host landing somewhere other than a filesystem is told it owns the same question.

## The vocabulary

| | |
|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir)` — the one sandbox a caller may reach |
| `SandboxSpec` | what a sandbox of a given *kind* needs: image, egress allowlist, work dir, `requires` capabilities, and an optional `min_isolation` that may raise the host's floor |
| `Sandbox` | `write_file`, `exec` and `run_code`, the pull surface `stat_file` / `read_file` / `list_dir`, `remove`, and `reclaim` — what a workload gets, gated by what the backend declares, except `reclaim`, which is gated by nothing |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope`, plus the `isolation`, `egress_modes`, `capabilities` and `os_families` it declares |
| `SandboxRouter` | picks the backend, enforces the minimum-isolation floor, the capability match, and the egress rule |
| `SandboxPurger` | duck-typed `purge_scoped_thread(scope, thread_id)` for a host's delete path |

`Isolation`, weakest to strongest: `none < runtime < os_process < container < hardened_container < microvm < vm`. `SandboxRouter`'s default `min_isolation` is `microvm`; an unrecognised rung refuses rather than guesses which side of the floor it falls on.

`SandboxKey`'s scope and thread come from the host's request context through `CallerContext`, whose fields are **callables read at call time** rather than values. That is deliberate: a key a caller can supply is a key a *model* can supply, and that would let one conversation address another's sandbox.

`SandboxSpec.egress_allow` is an allowlist — everything not named is denied, so an empty tuple means no network. Stating it positively means a spec that forgets to mention egress gets the closed configuration rather than the open one.

## Three axes, four checks that are not conveniences

```python
router = SandboxRouter(backends)                                   # default floor: Isolation.MICROVM
router = SandboxRouter(backends, min_isolation=Isolation.VM)       # stricter: dedicated full-VM only
router = SandboxRouter(backends, min_isolation=Isolation.NONE)  # a developer machine, opted down
```

**1. The minimum-isolation floor.** A backend declares its own `isolation`, ranked on the ladder above. The router refuses, at construction, any backend below `min_isolation` — or one whose declared value is not a rung this package recognises, because nothing here can tell whether an unrecognised boundary is stronger or weaker than the floor. A spec may also carry its own `min_isolation`; the effective floor is the *stricter* of the host's and the spec's — a spec may raise the floor for itself and never lower it.

It refuses rather than degrades. Falling back to a stronger backend would hide a misconfiguration; proceeding with the weaker one would break claims the host's security posture makes about every execution surface. Neither is better than an error.

**2. The capability match.** A backend declares `capabilities: frozenset[Capability]` (`EXEC`, `RUN_CODE`, `HOST_TOOLS`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `SNAPSHOT`, `ATTACHED_IDENTITY`) — what it can actually do — and a spec declares `requires`, what its workload cannot run without. `ensure_can_serve(spec)` raises `SandboxCapabilityNotSupported` when the backend is missing something the spec requires. Unlike the floor, silence here is a functionality claim rather than a safety one: an undeclared `capabilities` reads as exactly `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what this package's own `Sandbox` protocol already obligates, so a backend written before `Capability` existed does not have to start lying to keep working.

**3. The egress rule**, unchanged in substance. `egress_allow` was a contract nothing checked, so a backend that reads it and one that ignores it have the same type, the same methods and the same passing tests — each one declares an `Egress` level instead: `allowlist` (deny by default, allow the named hosts), `closed` (all or nothing), or `unrestricted` (cannot confine egress at all). `ensure_can_serve(spec)` refuses the last one. Here silence is *not* read charitably: an undeclared `egress` is treated as `unrestricted` and refused, because a backend written before the property existed cannot have been enforcing an allowlist it never read.

Which direction a backend misses egress by decides the outcome, and it is not symmetrical. A backend that confines **less** than the spec asks silently widens what the workload was designed to reach — refused. One that confines **more** is permitted, with a warning: the sandbox reaches nothing it should not, and the workload fails visibly at whatever it could not fetch.

**4. The guest-shape match.** A backend declares `os_families: frozenset[OsFamily]` — the guest shapes it hands out, `posix` or `windows` — and a spec declares `requires_os_family`, the shape its commands and scripts are written for. `ensure_can_serve(spec)` raises `SandboxOsFamilyNotSupported` on a mismatch, so a POSIX workload meets a Windows guest at attach rather than at its first command. **The axis is path grammar and argv quoting, and nothing else**: a spec asking for `posix` and getting it can still meet an image with no shell, because what is *installed* in a guest is a property of the image, and one backend may be handed many. `docs/sandbox/guest-platform-and-commands.md` settles where that separate question is answered. Silence here is neither of the readings above — an undeclared `os_families` is the *absence of an answer*, read as `frozenset()`, which refuses a spec that asks and leaves every spec that does not exactly as it was. A backend with no guest in the operating-system sense, such as one serving a language runtime, has nothing to declare and declares nothing.

Note that the checks answer to different owners. How strong the boundary must be *here* is the *host's* policy, read from `min_isolation` — and a spec may raise that floor for itself, never lower it. What a sandbox may reach, and what it must be able to do, are properties of the *workload*, stated in its spec. Keeping the axes apart is deliberate: merging isolation into a "required capabilities" list would let a workload ask for a weaker boundary than the deployment mandates.

`ensure_can_serve` is also the whole of a wiring test, in your own repository, against your own backend choice:

```python
router.ensure_can_serve(bicep_sandbox_spec())
```

## Getting files back — the declaration, and where it lands

A workload's only return channel used to be `ExecResult.stdout`, which is right for a diagnostic and wrong for a rendered image. `Capability.FILES_OUT` is the pull surface, and it is narrow in two deliberate ways: this library never *discovers* what a workload produced, and it never decides where the bytes go.

**Declare it.** A `DeclaredOutput` names one artifact as a literal path relative to `work_dir`, in `SandboxSpec.declared_outputs`. Literal rather than a glob: resolving a pattern means enumerating a directory, which is the primitive `Capability.FILES_LIST` exists to gate, so a kind that cannot name its outputs in advance requires *that* capability and a backend serving only `FILES_OUT` refuses it. `media_type` is declared rather than sniffed, because sniffing lets guest-produced content decide how the host handles it. `required=False` is how a workload says an absence is normal — a renderer exiting non-zero produces no file, and the model needs that diagnostic rather than a transfer error stacked on top of it. `name` is the spelling the artifact *lands* under and defaults to `path`; the two come apart as soon as a kind writes into a per-call directory, which warm sandbox reuse forces on any kind whose outputs would otherwise persist into the next round.

`disposition` keeps the two flows apart because they answer to different legs of a host's policy: `LAND` goes to the sink and the question is confidentiality, while `CONSUME` is parsed by the kind that asked for it and the question is integrity. A `CONSUME` output is still counted against every cap — `files_out` bounds the collection the spec declared, not the subset of it that lands.

**Receive it.** `await collect_outputs(sandbox, spec, sink=...)` returns `LandedArtifact`s in declaration order. The order of its phases is part of the contract rather than an implementation detail: everything the declaration alone decides — a sink for anything that lands, a valid name for every output, no two landing names that collide — is settled before the sandbox is touched, then every declared output is stat-ed and capped, then the landing ones are read, and only then is anything delivered. Delivery is a push nothing can take back, so a refusal arriving after the first `deliver` could not leave the host as it found it.

`spec.files_out` is a `TransferLimits` and all three of its fields are load-bearing: a byte ceiling alone does not bound a collection, since ten thousand files one byte under the per-file cap cost exactly what the cap was written to prevent. What comes back when a collection does not fit is specific rather than generic — `SandboxTransferCapExceeded` names both the cap and the file that breached it, `SandboxOutputMissing` names a `required` output that was not there, `SandboxOutputSizeUnknown` is a backend that could not say how large something was, and `SandboxArtifactNameCollision` is two landing names that are one file at the destination: identical, or differing only by case or by Unicode form.

**Land it.** An `OutputSink` wraps a single `async def deliver(artifact) -> LandedArtifact`. This library holds no opinion about where an artifact goes — a directory, a blob container, a file store — which is what keeps that flow visible to the host's own information-flow policy instead of buried in a dependency. `LandedArtifact.display` is the one line the model is allowed to see; `handle` is the host's own reference, and nothing renders it into the transcript.

**`validate_artifact_name` is lexical, so a sink still has to confine its own destination.** It refuses `..`, absolute paths, backslashes and empty segments, so the *name* cannot traverse — which is not the same as safe, because it says nothing about what is already sitting at the path that name resolves to. A symlink in the output directory carries the write straight out of it: the same failure class as [#142](https://github.com/sokolaidev/maf-extensions/issues/142), on the host side of the boundary.

**`make_file_system_sink(root)` is that check, packaged.** It resolves each destination, refuses anything leaving `root` with `SandboxLandingNotConfined`, creates the parents a nested name needs, and writes. Reach for it rather than writing the four lines yourself — two samples here wrote them by hand and only one got it right. Pass `display` when the kind introduces its artifacts in its own words. It stays a check rather than a guarantee, and that is a property of the filesystem rather than of the helper: resolving and writing are two calls, so a host landing genuinely hostile output wants no-follow primitives underneath. What it closes is the standing case — something already in the way when the run started.

A sink landing somewhere that is *not* a filesystem — a blob container, a file store, a UI panel — writes its own `deliver` and owns the equivalent question for that destination.

```python
from pathlib import Path

from maf_sandbox import (
    Capability, DeclaredOutput, SandboxSpec, TransferLimits, collect_outputs,
    make_file_system_sink,
)

spec = SandboxSpec(
    kind="diagram",
    image="diagram-sandbox:1",
    egress_allow=(),
    work_dir="/workspace",
    requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
    declared_outputs=(DeclaredOutput(path="diagram.png", media_type="image/png", required=False),),
    files_out=TransferLimits(max_bytes_per_file=8 * 1024 * 1024, max_total_bytes=16 * 1024 * 1024, max_files=4),
)

landed = await collect_outputs(sandbox, spec, sink=make_file_system_sink(Path("out")))
```

**Reclaim the sandboxes when the conversation ends.** `router.scope(scope, thread_id)` is an async context manager that calls `dispose_scope` however the block ends, and cannot mask an application error on its way out — `dispose_scope` already swallows and logs each backend's failure. Its own reason is why this is packaged rather than left to every host to remember: *a sandbox nobody reclaims is a sandbox somebody pays for.*

```python
async with router.scope(scope, thread_id) as reclaimed:
    ...                                    # attach tools, run the turn
print(f"Disposed {reclaimed.disposed} sandbox(es).")   # the count arrives after the block
```

A workload whose artifact names are not knowable when its tool is built passes the same `DeclaredOutput` type to `collect_outputs(outputs=...)` instead. That is refused unless the spec sets `outputs_named_at_call_time`: without the flag, the tool was attached with no sink required of it and no outbound cap agreed, and collecting there would land artifacts behind both checks.

[`samples/08_docker_codeact_files`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/08_docker_codeact_files) is all of the above as a runnable program, against a real engine.

## Host tools — the contract, and the backends that serve it

`Capability.HOST_TOOLS` is the one capability where trust crosses *outward*: a called function body runs in the host process, with the host's privileges, driven by model-written code, and each host-tool call bypasses whatever middleware the host runs. `maf-sandbox-docker` and `maf-sandbox-acas` declare it; `maf-sandbox-wslc` does not. The safety contract shipped first, before anything could use it, and it is what a host configures either way: `HostToolRegistry` starts empty (nothing is callable until a developer registers it, and registering emits a one-time, suppressible `MafSandboxHostToolsWarning`); `@sandbox_tool(source=..., sink=..., identity=...)` makes the developer answer every information-flow leg with no defaults (`None` is an answer — "not that role"); a `require_declared` gate refuses unstamped functions at registration, which is the only place the declaration is ever read — `register` captures it, `HostToolRegistry.aggregate()` seals the registry as it derives policy from it, and a stamp swapped or removed afterwards reaches nothing; `allowed_identities` (default `frozenset({Identity.APP})`) refuses at registration a tool exercising a broader authority — an `Identity.USER` tool, or an unstamped one read as `APP` — so user authority is opt-in (`frozenset({Identity.APP, Identity.USER})`), a tool declaring `identity=None` is always allowed, and `denied_identities` on the router stays the attach-time backstop; each run is bounded by a host-tool-call cap (`DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN`, refusals included) and by response size caps that reuse `TransferLimits`; arguments are validated host-side at the registry's one door, never in a guest shim; and a host whose posture wants a hard stop rather than awareness passes `denied_capabilities={Capability.HOST_TOOLS}` or `denied_identities={Identity.USER}` to its router.

One sentence to read before registering anything, because a declaration reads like a control and is not one: **`Identity.APP` is not the safe option, only the declared one.** It is the application's full authority, and the only real bounds on it are the emptiness of the registry and the host-tool-call cap — least privilege for host-tool calls comes from what a host registers, never from what it declares. `Identity.USER` is declarable but not servable: registering such a tool raises the whole surface to approval-gated, and calling one is refused with the prerequisites named.

### Reaching the host from inside — `host_tool_calls_over_exec`

The contract says what may be called; it does not say how a host-tool call *reaches* a host whose guest speaks an exit code, stdout, and a stat-and-read pull surface. `host_tool_calls_over_exec` is that channel, built from those primitives and nothing else, and it is a helper a kind composes rather than anything the protocol requires. A kind writes the program and the generated shim (`host_tool_shim`) into a fresh per-run directory (`guest_run_layout`); `host_tool_calls_over_exec` writes the launcher itself, starts it detached and then polls for request files, resolves each one through `HostToolRun.call` — the same one door, with the same gates, cap and ceilings — and writes the answer back. It needs `EXEC`, `FILES_IN` and `FILES_OUT`, and deliberately not `FILES_LIST`. One run is two directories, and that is what keeps a guest-supplied name away from the machinery serving its own call: `WORK_DIRECTORY` is the program's working directory and the only one a kind puts model-named files in, while the shim, the launcher, the output, the exit marker and the calls directory sit in a sibling nothing a model names can reach. The program itself lives in the second one, beside the shim, because `sys.path[0]` follows the *script* rather than the working directory — run from the work directory it would put a guest file named `maf_host_tools.py` ahead of the real module, which is exactly the substitution a list of reserved names is hardest to get right about.

**A run that overruns is signalled, not just reported.** The program is started detached, so the bound expires in the supervisor rather than inside an `exec` a backend could tear down along with its container — which used to mean a timed-out program kept running and the only remedy was disposing the whole sandbox, taking every other call in that conversation with it. The launcher records the program's pid and the supervisor signals it on the way out, over the same `exec` this transport already runs on: no protocol method, no capability beyond the ones the host-tool-call path requires. **The message says the signal was sent, not that the program died** — `kill` reports success for a signal the kernel accepts and discards, and the pid comes from a file the program can rewrite, so a guest that names pid 1 or another process outlives a call that reports `and was sent SIGKILL`. Where the signal could not be sent at all the message says *"could not be signalled, so it may still be running"*, and disposal is what stops it. Making the stop something the host can rely on against hostile code is [#463](https://github.com/sokolaidev/maf-extensions/issues/463) — the pid and the session are still read from files inside the run. **Children the program spawned are killed with it where the guest has `setsid`, unless they left the group.** The launcher starts the program inside a session of its own — `setsid` runs a shell, that shell leads the session, and the program is its child — and the supervisor signals the process group the program starts in, so a program that forked a spinner does not leave it burning CPU for the rest of the conversation. A descendant that calls `setpgid` stays in the session, leaves that group, and survives — `reach` says `"group"` and means it. Where `setsid` is missing the program shares the launcher's session, only its own pid can be signalled — a group signal there would reach the whole container — and the message says so rather than implying otherwise: *"and was sent SIGKILL, which reaches it alone — anything it spawned is still running"*. The signalled processes stay as zombies until the sandbox goes if nothing in the image reaps them, which is normal for a container whose pid 1 is not an init. The guest needs `sh`, `nohup`, `printf`, `mv`, `mkdir`, `rm` and `kill`, and uses `setsid` when it is there.

**The shim is not a control.** It runs where model-written code can read, edit or ignore it, and a program that writes request files itself is served identically. That is the design: every gate is host-side, and a check running in the guest would be decoration.

**The transport tries not to let its own files outlive the call.** It removes the ones it owns — the program, the shim, the launcher, the captured output, the exit marker, the pid, and every request and response the run exchanged with the host — on *every* exit path, success included, over the same `exec` it uses for everything else — **best-effort, not a retention guarantee**: a guest without `rm`, a removal that times out, or a non-zero exit each leave that traffic readable, logged and nothing more. What it cannot remove is `WORK_DIRECTORY`: artifacts live there and a kind collects them after the transport has returned, so removing it would delete the outputs of every successful run. `reclaim_run(sandbox, layout)` is the other half, **a kind's to call in a `finally` once it has collected**, and it takes the whole run directory. A `False` from it is a data-retention failure rather than a tidiness one: nothing comes back for it — the protocol's delete is capability-gated and this transport does not require it — and `acquire` is get-or-create, so a run directory that survives is readable by every later run in the same sandbox for the life of the conversation. **A kind that takes its place in the guest from `SandboxToolSession.guest_call_path()` does not have to remember any of this**: `sandboxed_tool` removes that path, and everything under it, when the call returns — after a result, a refusal and an exception alike — and hands a removal that did not happen to the host's `on_reclaim_failure` as a `ReclaimFailure` — a notification, delivered *after* the framework has already disposed the sandbox by default (see **Upgrading to 0.23** below). A kind that composes its own path keeps `reclaim_run`, and keeps the `finally`. Disposal is no longer the host's to arrange: the framework disposes an unclean sandbox itself and refuses the key until a disposal lands, and a host loosens that only by opting down on the router with `reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP)`, never per kind.

It costs round trips — several backend calls per host-tool call, plus polling, plus one on every return to reclaim, and one more to stop the program on a run that overran. It serves one outstanding call at a time. This module's own docstring counts those costs exactly, beside the code that decides them; whether the trade is worth it is a measurement rather than an assumption.

## Upgrading to 0.26

**A backend says a delete failed by returning, not by raising.** `dispose` is contractually best-effort and never raises, so the refusal 0.23 shipped — a key held closed until its disposal lands — could never fire against a compliant backend: each swallowed its delete error, said nothing, and was read as having disposed. Both disposal methods now carry the answer back:

| Was | Is |
| --- | --- |
| `async def dispose(key) -> None` | `-> DisposalFailure \| None` — a code to branch on, and a detail to log |
| `async def dispose_scope(scope, thread) -> int` | `-> ScopePurge` — `.disposed` is the old count, `.undisposed` the failure |
| `router.dispose_scope(...)` → `int` | → `ScopePurge` |
| `purger.purge_scoped_thread(...)` → `int` | → `ScopePurge` |

**The code is the contract; the detail is not.** `DisposalCode` is a closed set — `unreachable`, `timeout`, `refused`, `unlisted`, `unknown` — and it is what a caller acts on: retry an `unreachable`, raise the bound on a `timeout`, put a `refused` in front of a human, since it is a missing role far more often than anything transient. `detail` is the backend's own sentence, for a log, never to be parsed.

```python
async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
    try:
        gone = await self._client.delete(key)
    except TransportError as exc:                     # never reached the service
        return DisposalFailure("unreachable", f"{key}: {exc}")
    return None if gone else DisposalFailure("refused", f"{key}: the service kept it")
```

**Reach for `unknown` rather than guessing between the others.** A code chosen to look precise is worse than one that admits the backend cannot tell, because a caller branches on it either way. Several failures fold to the most actionable code — `fold_disposal_failures` — keeping every detail.

**A third-party backend must return the new type.** `dispose`'s reason was a `str`; wrap it in a `DisposalFailure` with the code that fits. `dispose_scope` changes shape too: return `ScopePurge(count)` where you returned `count`.

**A caller reading the count reads `.disposed`.** Watch for `if await purger.purge_scoped_thread(...)`: a `ScopePurge` is always truthy where the count it replaced was not. `router.scope(...)`'s record gains `undisposed` beside `disposed`, which is additive.

**`None` means nothing was reported, not that the delete provably happened** — a backend with no way to check returns it too. The conflation is with success on purpose: the alternative refuses every key served by a backend that cannot answer. Say something whenever the delete is *known* not to have landed, and the router will refuse the key and quote you in `SandboxUnclean`.

## Upgrading to 0.25

**The `dispatch` spelling is gone.** 0.24 added the `host_tool_call` names beside the old ones so a dependent could move in its own release; this removes what was kept.

| Was | Is |
| --- | --- |
| `dispatch_over_exec` | `host_tool_calls_over_exec` |
| `DispatchResult` | `HostToolCallResult` |
| `DEFAULT_MAX_DISPATCHES_PER_RUN` | `DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN` |
| `fold_dispatch_transfer_limits` | `fold_host_tool_call_transfer_limits` |
| `HostToolRun.dispatch` | `HostToolRun.call` |
| `registry.dispatch_observer` | `registry.host_tool_calls_observer` |
| `registry.max_dispatches_per_run` | `registry.max_host_tool_calls_per_run` |
| `dispatch_observer=` | `host_tool_calls_observer=` |
| `max_dispatches_per_run=` | `max_host_tool_calls_per_run=` |

Pin `maf-sandbox<0.25` to stay, or rename: an old import is an `ImportError`, an old attribute an `AttributeError`, and an old keyword a `TypeError` — each naming what it wanted, none of them silent.

## Upgrading to 0.23

**`Sandbox` gains `reclaim`, and there is no declare-or-raise escape for it.** Every other protocol method a backend cannot serve may raise `NotImplementedError` as long as it does not declare the matching `Capability` — the `run_code` note below is that escape, spelled out. `reclaim` is the first member with no capability behind it: there is no spec the router could refuse before a caller arrives, and a sandbox answering with `NotImplementedError` leaks a directory per call out of a `finally` that reports rather than raises. **A third-party backend adds one method, and has to genuinely implement it:**

```python
async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
    ...
```

Three rules a caller depends on, all in the docstring. **The caller created it** — under `working_directory`, with an unguessable name — so no filesystem path check is owed before removing it: there is no attacker-chosen component to check. That is what makes `reclaim` payable on a backend that must refuse `remove`: `maf-sandbox-wslc` cannot confine a model-supplied path (no `stat_file`, so no check) and still serves `reclaim` honestly, because this one owes no confinement to begin with. **A directory that is not there is success.** Cleanup runs in a `finally` and must not report a second failure over the first. **Anything else raises**, so the caller can escalate.

`working_directory` says where the directory sits; it is **not** a directory to run the removal from. No backend creates a spec's `work_dir`, so a call that wrote nothing leaves it absent, and a removal that moved there first would fail over a directory that is already gone. The target is absolute, so cwd decides nothing.

Docker, wslc and ACAS implement it as `rm -rf` over their own `exec` — for ACAS deliberately **not** the data-plane `delete_file` its `remove` uses, because whether that service follows a link on delete is unverified. The in-process fake removes the entries from its store and records the call, so a test can assert the cleanup ran.

**A sandbox the framework could not clean is disposed, by default.** When a tool call's guest path could not be removed, or a program the transport stopped may have left something running — a `SignalReach` of `"program"` or `"nothing"` — `sandboxed_tool` now disposes that sandbox from the same `finally`, before the host is told. Better a failed run than leaked data: `acquire` is get-or-create, so a sandbox left warm hands the next call everything the last one could not take back. `on_reclaim_failure` still fires, after the disposal, and `ReclaimFailure.disposal` says what happened — `"disposed"`, `"failed"`, or `"kept"`. A disposal that does not land makes the router refuse the key with `SandboxUnclean` until one does; `SandboxToolSession.acquire` turns that into a refusal the model reads. The opt-down is the host's, on the router, beside `min_isolation` — `SandboxRouter(backends, reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP))` keeps the old behaviour — and a kind cannot lower it. A host whose callback disposed the sandbox itself can drop that code; a host that relied on a warm sandbox surviving a failed cleanup opts down explicitly. `reclaim_timeout` now bounds three legs — removal, disposal, report — so a failing call can cost up to three times it.

## Upgrading to 0.20

**`Sandbox` gains `run_code`, and `Sandbox` is a `runtime_checkable` Protocol — so a sandbox implementation that does not define it stops satisfying the protocol.** `isinstance(x, Sandbox)` returns `False` and a type checker rejects it wherever a `Sandbox` is expected. Every backend published here answers it already, as of the previous release. **A third-party backend adds one method:**

```python
async def run_code(self, code: str, *, timeout: float) -> ExecResult:
    raise NotImplementedError("this backend does not support RUN_CODE")
```

That is the whole migration unless you declare `Capability.RUN_CODE`, in which case implement it: it is the method that capability names, as `exec` is the method `EXEC` names. `timeout` is **wall-clock from the call**, so a backend that serialises calls on one sandbox spends part of it queued rather than leaving the waiting half unbounded, and a deadline that expires before the program starts raises `SandboxQueuedTimeout` rather than a plain `TimeoutError` — the caller's next move differs, retry unchanged versus make the program smaller.

The fake in `maf_sandbox.testing` answers it too: `InProcessSandbox.run_code` records each program in `programs` and matches `outputs` against the code as a substring, exactly as `exec` matches a command line. The two lists are separate on purpose — a test asserting a program was evaluated should not be satisfied by a shell command that happens to contain the same text.

**The compatibility shim for a backend's old single `egress` property is gone.** 0.19 read `egress` through a shim when `egress_modes` was absent; 0.20 does not, so a backend declaring only `egress` is now refused as undeclared — it enforces nothing the router can see. Declare `egress_modes: frozenset[Egress]`, the set of modes it can actually enforce.

**`Capability.NETWORK` is removed** from the capability enum. No backend declared it and no spec required it; how precisely egress is confined lives in `Egress`.

**New, and additive: `assert_egress_conformance` checks that the mode you declare is the mode you enforce.** The two releases above made a backend say what it confines; this is the probe that holds it to the claim, in `maf_sandbox.conformance` beside the four suites already there. Give it a subject acquired with `Egress.ALLOWLIST` and that allowlist, plus one URL on the list and one off it, and it asserts the only outcome every allowlist backend shares — the allowed host answers, the denied host does not. Nothing calls it for you: a backend that declares `allowlist` and never runs it is exactly as it was before.

**New, and additive: a workload can state the guest shape it needs.** `OsFamily` is `posix` or `windows`; a backend declares `os_families: frozenset[OsFamily]` and a spec asks with `requires_os_family`, refused at attach with `SandboxOsFamilyNotSupported` on a mismatch. Nothing existing changes — a spec that asks nothing is refused by nothing, and a backend that declares nothing serves every spec that does not ask. **The axis is path grammar and argv quoting and nothing else**: a spec asking for `posix` and getting it can still meet an image with no shell, because what is *installed* in a guest is a property of the image, and one backend may be handed many. See `docs/sandbox/guest-platform-and-commands.md`.

## Upgrading to 0.19

**A workload now declares the egress mode it runs in, and the router refuses any backend that cannot enforce exactly that mode.** `SandboxSpec` gains `egress: Egress`, defaulting to `Egress.CLOSED`, and a backend declares `egress_modes: frozenset[Egress]` — the set it can actually enforce — in place of the single `egress` property, which is removed. `ensure_can_serve` serves the workload iff `spec.egress` is in that set.

**The tolerance is gone, and this is the change most likely to break a working deployment.** Until 0.19 a backend that confined *more* than the spec asked was permitted with a warning — a `closed` backend served an allowlist spec, and the workload simply failed at whatever it could not fetch. That is now a refusal:

```
SandboxEgressNotEnforced: sandbox backend 'docker' cannot enforce the 'allowlist'
egress the 'bicep' workload runs in (it enforces closed).
```

Neither direction is substituted any more: confining less silently widens what the workload reaches, and confining more hands it a posture it was not built for. If you see this, either give the backend a mode it can enforce — for `maf-sandbox-docker` that means configuring `egress_proxy_image`, without which it declares `{closed}` alone — or ask the kind for the mode you actually have, e.g. `bicep_sandbox_spec(egress=Egress.CLOSED)`.

**`egress_allow` without `Egress.ALLOWLIST` is refused at construction.** A host list is the payload of an allowlist run, so `SandboxSpec(kind=…, egress_allow=("example.invalid",))` now raises `ValueError` unless `egress=Egress.ALLOWLIST` goes with it. Kinds set both together, so this reaches you only if you build a spec by hand.

**`Capability.NETWORK` is removed.** No backend ever declared it and no spec ever required it; how precisely egress is confined lives in `Egress`, which is where it always was.

**Every shipped backend replaced `egress` with `egress_modes` in the same release.** A host that read `backend.egress` directly gets an `AttributeError`; read `backend.egress_modes` instead. Move core and the backends in the same step — an older backend under a 0.19 router is read through a compatibility shim and still resolves, but a 0.19 backend under an older router declares nothing the old router can see and is refused as undeclared.

## Upgrading to 0.18

**`Sandbox.write_file` takes a keyword-only `working_directory`, and refuses more than it used to.** The path is resolved against it and then refused if it escapes, passes through a symlinked parent, lands on a symlink, or names the working directory itself — so a write that used to land can raise `ValueError` or `NotADirectoryError`, and every backend implementation has to declare the parameter. **A version mismatch is invisible to an import check and surfaces at the first call**: an older backend under a 0.18 caller raises `TypeError: … got an unexpected keyword argument 'working_directory'`, and a 0.18 backend under an older caller raises `missing 1 required keyword-only argument`. Move core and the backends in the same step.

**A tool call owns a guest path, and the framework reclaims it. New in 0.18.0, so `maf-sandbox>=0.18.0` is the floor that gets it.** `SandboxToolSession.guest_call_path()` names a place under `work_dir` allocated once per call, and `sandboxed_tool` removes it and everything under it when the call returns — after a result, a refusal and an exception alike — handing a removal that did not happen to the host's `on_reclaim_failure` as a `ReclaimFailure`. A kind that adopts it drops its own `reclaim_run` call and the `finally` around it; a kind that composes its own path keeps both, and keeps today's behaviour.

## Upgrading to 0.17

**A host-tool-call run's *transport* files are deleted now, and a kind has one call to make for the rest.** `host_tool_calls_over_exec` removes its own directory on every exit path; the run directory — the model's shared-in files and its artifacts — is `reclaim_run(sandbox, layout)`, which a kind calls in a `finally` after collecting. **A kind that composes its own run directory and does not call it keeps today's behaviour for that half**, so nothing breaks. If your kind reads anything out of the transport's directory after `host_tool_calls_over_exec` returns, it will no longer be there; nothing shipped here does.


**Every host-tool-call run now issues one more `exec`, and a timed-out one may issue two.** The extra calls are `rm -rf` for the cleanup, which every path pays, and `kill -KILL` for the stop, which only a run holding a usable pid issues — an upload that ran out never started a program, and a run whose pid is missing or unreadable has nothing to aim at. A backend double that counts `exec` calls, or a guest whose commands are allowlisted, will see them — the fakes in this repository's own codeact suite had to be taught to ignore them. **A run's wall clock can exceed its `timeout`, by five separate graces.** Every path pays the reclaim's `_RECLAIM_GRACE` (10s). A run whose last host-tool call returned after the bound pays `_RESPONSE_WRITE_GRACE` (2s) to record the answer, because a tool that has already acted is owed the round trip that says what it did. A run that overran pays three more `_FINAL_READ_GRACE` (2s each): one for the last look at the exit marker and the program's output, one for the pid lookup, and one for the signal itself — each measured fresh, because a slow guest that spends one must not leave the next with nothing. That is `timeout + 18s` with today's constants — **the transport's own overhead, not an upper bound on the call.** A host-tool call is deliberately never cancelled, so one that blocks holds the supervisor for as long as it blocks; bounding that belongs to the tool, which is the only code that knows what it is waiting on. Size an outer deadline from `timeout + 18s` *plus* whatever your slowest registered tool can take. One set any tighter loses the `SandboxProgramTimeout` and its `output`, and cancels whatever host-tool call is in flight — a tool's effect half applied with no record written, which is the trade an outer `asyncio.wait_for` makes on your behalf.

**`GuestRunLayout` gained a `pid` field, and constructing one yourself is a `TypeError` until you pass it.** `guest_run_layout` fills it in, so a kind that uses the factory — which is every kind that follows the documented path — needs no change at all. The field is where the launcher records the program's process id, which is what lets a run that overruns be stopped instead of left going.

**Two more names are reserved in a run's transport directory:** `program_pid` and `program_pid.part`. `guest_run_layout` refuses a `program` named for either, on the same grounds it already refuses `program_exit_code.part` — the launcher writes them, so a program under one of those names is written over. This is about names, not reach: a model-supplied file name cannot collide with them, because a kind writes model-named files only into `work/`. A *program* can still open anything it likes by absolute path — the shim sits where model-written code can read and edit it, and so does everything beside it.

**A timed-out host-tool-call run now signals the guest program**, where before it left it running. A run that reached the program gained a clause saying whether the signal was sent, so a host matching the old text no longer matches those. Only one message for a run that never got that far is unchanged, the launcher upload running out. The launcher's own `exec` running out with no pid gained a clause too, because the launcher backgrounds the interpreter before it writes the pid down: a call that expires between the two leaves a program running and no pid to point at, so that message now says the start could not be established rather than quietly implying none happened. If your host disposes the sandbox on every `SandboxProgramTimeout` to reclaim the CPU, **keep doing that if you need the program actually gone.** The message distinguishes a signal that was sent from one that was not, which is less than it sounds: a sent signal can be discarded, aimed at a pid the program rewrote, or leave children running, so it is not confirmation of termination and disposal is still the only thing that is. The exit marker's meaning is unchanged: the launcher waits on the program, so the code recorded is still the program's own.

## Upgrading to 0.16

**A host-tool run is two guest directories now, and the program's working directory is the new one.** `GuestRunLayout` gained a `work` field; `program`, `shim`, `launcher`, `output`, `exit_code` and `calls` all moved from `<run>/` into `<run>/host_tools/`. A kind that shared files into `layout.directory` and collected artifacts from it must use `layout.work` for both — **this is the failure worth checking for, because nothing raises**: `guest_run_layout` still takes the same arguments, so a kind that never named the moved paths keeps running, the program's `open("input.csv")` fails inside the guest, and an artifact written to the program's own working directory lands where the old collection path does not look. A run that quietly produces nothing is the symptom. Constructing `GuestRunLayout` yourself is the loud half — the new field makes it a `TypeError`.

**A Python module shared into the work directory is no longer importable, and that is the second silent one.** In 0.15 the program sat among the model's files, so `sys.path[0]` was the run directory and a kind could share `helper.py` beside it and have the program `import helper`. The program now runs from `host_tools/`, `sys.path[0]` follows it there, a working directory is never added to `sys.path`, and the launcher drops the inherited path entries that could put the work directory back — so the same import is a `ModuleNotFoundError`. If your kind shares Python rather than data, the program has to opt in:

```python
import maf_host_tools  # first, so the real shim is in sys.modules
import os, sys
sys.path.insert(0, os.getcwd())  # now the work directory is importable
import helper
```

Order matters: once the work directory is on the path, a model-written file can answer any import that follows, which is what the split exists to prevent. Importing the shim first is what keeps that one safe — it is already in `sys.modules` and cannot be shadowed afterwards. Sharing the helper into `host_tools/` instead is not an alternative; that directory is the transport's, and a name that collides with it is refused.

The split is what replaced the reserved-filename list this release was originally going to export. Two directories mean nothing a model can name reaches the transport's own files, so there is no list to keep complete, and the shim can no longer be shadowed by a guest file of the same name — `sys.path[0]` follows the program, which now sits beside the shim rather than among the model's files.

**`guest_run_layout` refuses inputs it accepted in 0.15, and the first two refusals are new constraints rather than newly-enforced old ones.** A `run_directory` containing `:` is rejected: the shim's directory now travels to the guest through `PYTHONPATH`, which separates on `:` and cannot quote one, so such a path would reach the interpreter as two entries — the second of them relative, resolved against the directory the guest writes into. If your run directories embed a timestamp, `/runs/2026-08-17T10:30:00Z` is the shape that stops working. And a `program` name is refused when **the module it would answer to** matches the shim's own module name (`maf_host_tools.so` and friends: the stem is reserved for the shim, because a file under it either shadows the import the program opens with or cannot run as a program), a module the generated shim imports (`json`, `os`, `time`), or one CPython imports at startup (`encodings`, `site`, `sitecustomize`, `usercustomize`, plus — reached through ordinary path lookup on a guest older than 3.11, whose standard library is not frozen, and refused everywhere because the guest's interpreter is not this package's to pin — `abc`, `codecs`, `genericpath`, `io`, `posixpath`, `stat`, `_collections_abc`, `_sitebuiltins`, `_bootlocale`) — the program shares a directory with the shim and that directory is on the path from startup, so such a name is imported instead of the module it stands for, or runs before the program does. One exact filename joins the list 0.15 already refused: `program_exit_code.part`, where the launcher stages the exit code before renaming it into place — that one is an old constraint newly enforced, since a program under it was truncated and renamed away by the launcher's last line in 0.15 too.

**The launcher rewrites the guest's `PYTHONPATH`.** It prepends the shim's directory and keeps an inherited entry only when it is absolute, canonical, *and* outside the run directory. A relative one resolves against the working directory — which the launcher has just changed to the guest's own — so an image that relies on `.` or a relative entry loses it here. An absolute one is dropped when it names the run directory or anything under it: nothing an image meant to name can live there, because the directory did not exist when the image was built, so an entry that does name it is either a coincidence of layout or an attempt to make the guest's own files importable at interpreter startup. `/runs/current-sibling` is kept when the run is `/runs/current`; only the tree itself goes. Every other absolute entry is passed through unchanged, including any that contain glob characters.

**An entry carrying `/./`, `/../` or `//` is dropped whatever it names.** The comparison above is textual, so `/runs/./current/work` is a different string from `/runs/current/work` and the same directory to the interpreter. Such an entry is refused rather than normalised — an entry this cannot compare against the run tree is one it cannot vouch for. If your images export a path spelled that way, spell it canonically or it will not reach the guest.

**The launcher also sets `PYTHONNOUSERSITE=1`, so user site-packages are off inside a run.** `PYTHONPATH` is not the only inherited way into startup: `site` adds `$PYTHONUSERBASE/lib/pythonX.Y/site-packages`, and a `sitecustomize` there runs before the program exactly as one on the path would. Filtering that variable alone would leave the same hole behind `HOME`, which the user base falls back to, so the mechanism goes off rather than being chased through its inputs. **If your image installs dependencies with `pip install --user`, they stop resolving inside a host-tool run** — install them into the system environment instead. The failure is an `ImportError` naming the module, not a silent one.

What none of this closes is a symlink from outside the run tree into it, which needs a `realpath` POSIX `sh` does not have, and `PYTHONSAFEPATH` does not help with any of it — `sitecustomize` runs before any script. If your images export `PYTHONPATH` or `PYTHONUSERBASE` at all, keep them clear of wherever your kind places run directories.

**`host_tool_calls_over_exec` raises `SandboxProgramTimeout` for its own bound.** A `TimeoutError` from it used to mean either the run running out or a backend bounding one of its own calls, and callers could not tell which. The new type — a `TimeoutError` subclass, so existing handlers keep working — is the first. A **bare** `TimeoutError` is the second, and says nothing about whether the program is still running — validation errors and whatever a backend raises for its own reasons come through as themselves, unchanged. It carries the program's partial output on `output`, and what the transport managed to do about the program on `signal` — `sent`, `refused`, `absent`, `unrecorded`, `unknown`. **Branch on `signal`, not on the message text**, which is prose and will keep moving. Only `absent` says nothing was started; none of the others confirms the program was stopped, so a host that needs it gone still disposes the sandbox. Raising this type yourself reports `unknown` unless you say otherwise — nothing claims `absent` but the leg that never reached a launcher.

**There is a new rung, `os_process`, between `runtime` and `container`.** A separate OS process is a real boundary — a kernel-enforced address space — and a weaker one than a container, which is a process *plus* namespaces and cgroups. It exists so that a backend running untrusted code in a subprocess has something honest to declare instead of understating itself as `runtime` or overstating itself as `container`. No backend in this repository provides it; this release is vocabulary.

**`Isolation.PROCESS` is back as a name, and it means the new rung.** If you upgraded through 0.14 you have already made the edit this needs: the old `Isolation.PROCESS` meant *no* boundary and is now `Isolation.NONE`. If you are coming from 0.13 or earlier, read the 0.14 note below first — jumping the version where the old spelling raises is the one path on which this rename is quiet.

**The value is `"os_process"`, not `"process"`, and `Isolation("process")` still raises `ValueError`.** Reusing the attribute is safe because Python resolves it where you wrote it. Reusing the string would not be: a declaration reaches this vocabulary through `Isolation(raw)` at run time, out of configuration nobody re-reads, so the old spelling would have come back ranked two rungs higher having claimed a boundary it never drew. It is refused instead, and it will stay refused.

**Rank numbers shifted; comparisons did not.** Inserting a rung renumbers everything above it — `container` moved from 2 to 3, and so on up. Nothing needs to change if you compare rungs with `meets_floor` or through `ISOLATION_RANK`, which is the only ordering there is. If you persisted a rank *integer* anywhere, it now names a different rung.

## Upgrading to 0.14

**`Isolation.PROCESS` is `Isolation.NONE`.** The rung that provides no boundary was named for where the code runs rather than for what it protects, and read as the opposite of what it meant — "process isolation" implies a boundary, and this rung is the absence of one. One mechanical edit, in host code and in any backend you have written.

**The old spelling is removed outright rather than kept as an alias, and that is the point.** `PROCESS` is reserved for a genuine separate-OS-process rung — a kernel-enforced address space, sharing the kernel and the filesystem — which landed between `runtime` and `container` in 0.16, carrying the value `"os_process"`. An alias would have made that reuse silent: a backend declaring `"process"` *because* it drew no boundary would come back ranked two rungs higher, having claimed one, and a host running `min_isolation=Isolation.RUNTIME` would begin admitting it. So in this release `Isolation.PROCESS` raises `AttributeError`, `Isolation("process")` raises `ValueError`, and a backend still declaring it is refused at construction with `SandboxBackendNotPermitted`. The failure is the notice.

**Check your configuration, not only your code.** `Isolation` is a `StrEnum`, so a floor or a declaration may reach the router as the string `"process"` out of a config file or an environment variable rather than as an attribute. Those fail the same way and at the same moment — but a grep for `Isolation.PROCESS` will not find them.

## Upgrading to 0.11

`0.11.0` retired the word `workspace` from the public vocabulary. It was carrying three unrelated things, and only one of them keeps the stem. Two edits, both mechanical.

**`WorkspaceContext` is `CallerContext`, and `make_workspace_context` is `make_caller_context`.** The type was never a storage concept: two of its three fields are identity, and `list_files` *receives* a store rather than holding one. Its first parameter is now `list_files` where it was `store_walker` — a positional call needs no edit, a keyword one does.

**`work_dir` and `working_directory` are unchanged.** They name the guest's working directory, they are the most common use of the stem by an order of magnitude, and they were never the concept being retired. If you were looking for a rename here, there isn't one.

The dependent packages moved with it: `maf-sandbox-bicep` and `maf-sandbox-codeact` take `file_store` where they took `workspace_store`, and each has its own note.

## Upgrading from 0.4.x

`0.5.0` replaced the `deployed` boolean with a declared isolation floor, and added a capability axis. Four changes need an edit; nothing else moves.

**`SandboxRouter(..., deployed=...)` is gone — pass `min_isolation` instead.** `deployed=True` becomes `min_isolation=Isolation.MICROVM`, which is also the default, so a deployed host can drop the argument entirely. `deployed=False` on a developer machine becomes the rung that host actually accepts, stated explicitly — `min_isolation=Isolation.CONTAINER` for a container backend, `Isolation.NONE` for an in-process fake. There is no longer a value meaning "anything goes": a host that wants the weakest rung names it.

**`DEPLOYED_ISOLATION` is removed.** The policy it expressed is `min_isolation`'s default.

**`Isolation` and `Egress` are `StrEnum`s, and the ladder grew.** Values are unchanged, so `backend.isolation == "vm"` and any stored configuration keep working. The ladder is now `process < runtime < container < hardened_container < microvm < vm`; a declared value outside it is refused at construction rather than silently permitted. (The bottom rung was renamed to `none` in 0.14 — see *Upgrading to 0.14* above. This note describes the ladder as 0.5.0 shipped it.)

**`AcasSandboxBackend` now declares `microvm`, not `vm`.** ACA Sandboxes are hardware-isolated micro-VMs; `vm` now means a dedicated, full VM on remote infrastructure. A host that pinned `min_isolation=Isolation.VM` expecting ACA Sandboxes to satisfy it should use `Isolation.MICROVM` — the default, and the rung the micro-VM standard defines.

Backends need no edit to keep working: one that declares no `capabilities` is read as declaring `DEFAULT_CAPABILITIES` (`exec` + `files_in`), which is what the `Sandbox` protocol already obliges. Declare a wider set to serve workloads that require more.

## Writing a backend

Implement `name`, `isolation`, `egress_modes`, `acquire`, `dispose`, `dispose_scope`. `capabilities` and `os_families` are optional. Eight things worth knowing before you start:

**Declare `egress_modes` honestly.** It is the set of modes you can actually *enforce*, read before any workload's tool is attached, and a backend that omits it enforces nothing the router can see and is refused. List a mode only if you can hold it: a backend that cannot cut the network does not list `closed`, and one that always proxies does not list `unrestricted`. The router serves a workload in exactly the mode it declares or refuses — it never substitutes a stricter one, because a posture the workload was not built for is not a favour.

**`capabilities` is optional, and silence is the opposite of `egress_modes`'s.** Omitting it reads as `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}`, so a backend that has always offered exec and file-write does not need to add the property to keep working — only declare it once you offer more, or less.

**Your `Sandbox` implements `run_code` whether or not you serve it.** It is a member of the protocol, so an implementation without it is not a `Sandbox` at all; raise `NotImplementedError` unless you declare `Capability.RUN_CODE`. The same is true of `remove` and `list_dir` — the methods `FILES_DELETE` and `FILES_LIST` name.

**`reclaim` is the one member that escape does not reach.** Every other protocol method a backend cannot serve may raise `NotImplementedError` as long as it does not declare the matching `Capability` — that is exactly the sentence above. `reclaim` is the first member with no capability behind it, so there is no spec the router could refuse before a caller arrives, and no declaration to withhold: a sandbox answering `reclaim` with `NotImplementedError` leaks a directory per call out of a `finally` that reports rather than raises. A third-party backend has to genuinely implement it. What makes that payable on any backend: `reclaim` owes no confinement, because the directory is one this stack created under `working_directory` with an unguessable name, so there is no attacker-chosen component to check — which is why a backend that must refuse `remove` (`maf-sandbox-wslc`, for want of the filesystem path check) can still serve `reclaim` honestly.

**`os_families` is optional and means nothing about what is installed.** Declare the guest shapes this instance hands out — `posix`, `windows` — when your guest has an operating system at all; a backend serving a language runtime has no answer and gives none, which refuses a spec that asks and leaves every other spec untouched. It says nothing about whether the image has a shell.

**`acquire` is get-or-create.** A workload's fix-round loop calls it every iteration; returning a cold sandbox each time turns a seconds-long loop into a minutes-long one.

**`dispose_scope` returns a `ScopePurge`, and both disposal methods report rather than raise.** They are best-effort and never raise, so a delete that failed has no other way out: `dispose` returns the reason, `dispose_scope` puts it beside the count. Say nothing and the router reads it as disposed, reopens the key it had refused, and hands the next call a sandbox still holding the last one's data.

**`dispose_scope` must not consult only your process's memory.** A multi-replica host serves a conversation delete wherever it lands, so the replica that created the sandbox is usually not the one deleting it. Derive the set from the service — labels, a listing, whatever your provider offers. A backend that skips this leaves billable compute running and the bug is invisible on a single-replica dev box.

Both `dispose` methods are best-effort by contract: purge must never fail a delete.

**If you serve `FILES_OUT`, do not write the confinement check yourself.** A path whose *parent* is a link satisfies every lexical test and still reads outside, and two backends written independently against the prose shipped that same escape ([#142](https://github.com/sokolaidev/maf-extensions/issues/142)). `maf_sandbox.paths.refuse_symlinked_ancestors` is that check, taking your own stat — which must be **unconfined** (it covers the work dir's own ancestors), **no-follow** (a stat that resolves a link describes its target and hides the escape), and **not answered by the guest** wherever your engine offers anything else, since a workload asked to describe its own filesystem can answer falsely and that answer is the one the check trusts. A backend with no other mechanism says so in its own README; the repository's [`tests/test_confinement_stat_source.py`](../../tests/test_confinement_stat_source.py) is what holds it to that.

Then check yourself against `maf_sandbox.conformance`, **on a real instance**: probes that plant a hostile layout through your public surface and attack it. Fill in a `ConformanceSubject` — a sandbox, a working directory, how your guest plants a file and a link, and how it answers whether a path is there, with `PosixGuestSubject` covering a Linux guest that has `ln` and `test` — and `await assert_files_out_conformance(subject)`. It imports no test framework, and a failure names every probe that failed rather than the first.

The same shape holds the other capabilities to one reading: `assert_files_in_conformance` (byte fidelity over UTF-8-representable payloads, UTF-8 `str`, overwrite-replaces, implicit parents, and write-path confinement — **promises the protocol states on `write_file`**), `assert_exec_conformance` (exit-code fidelity, argv quoting, the working directory — **which the suite plants itself, because no backend creates `spec.work_dir` after acquire and the caller-creates rule is the one `host_tool_calls_over_exec` already made deliberate** — and the `TimeoutError` bound, whose probe may discard the sandbox, which two backends do by design), `assert_files_delete_conformance` (a link removed never followed — including one *inside* a recursive removal, which is the escape a service-side tree delete can hide — `recursive` as a word the caller says, the working directory refused), and `assert_egress_conformance` (a declared `Egress.ALLOWLIST` is *enforced*: the guest reaches a host on the allowlist and not one off it). That last one takes the allowed and denied URLs, because a spec's hosts are the deployment's rather than this package's, and it asserts only the outcome an L3-severing and an L7-proxying backend must share. Its subject has to be acquired with `Egress.ALLOWLIST` and that allowlist, and both URLs have to answer 2xx when reached — the allowed probe is its own positive control, and a denied host that fails for its own reasons would pass the deny probe having reached the host, which is the one thing that probe exists to refute. Two levels of opting out, and they are different: a suite run against a subject whose capabilities do not include its gate is **refused with `ValueError`** — a green run that attacked nothing is worse than no run — while a probe needing a capability the subject declares *besides* the gate (FILES_LIST within FILES_OUT, say) is **skipped and the skip reported**, so a backend that quietly stops declaring something cannot go green on fewer probes. The EXEC suite never skips at all: every probe in it needs `EXEC` and nothing besides. A backend with no pull surface answers the exec-verified suites as readily as one with a full one: that is why wslc, which declares only `EXEC` and `FILES_IN`, is held to something at last ([#450](https://github.com/sokolaidev/maf-extensions/issues/450)).

**One suite is owed by every backend and gated on nothing.** `assert_reclaim_conformance` covers `Sandbox.reclaim` — mandatory, behind no `Capability` — so it is refused for no declaration and skips no probe, and a backend that runs only the suites its capabilities gate has still not answered it. It is also the one suite that never reaches for `exec`: its probes plant through `ConformanceSubject.plant_file` and ask `ConformanceSubject.exists` what survived, which is why the seam above has a way to *see* and not only to plant. A guest reached through a language runtime alone, with no shell to answer `EXEC` with, sits it like any other ([#639](https://github.com/sokolaidev/maf-extensions/issues/639)).

Two rules the probes reached for and the protocol has not written yet are filed rather than guessed: whether exec must carry non-UTF-8 bytes losslessly ([#465](https://github.com/sokolaidev/maf-extensions/issues/465)) and whether `acquire` should promise `work_dir` exists ([#466](https://github.com/sokolaidev/maf-extensions/issues/466)). Each surfaced on the suites' first live run — which is the suites working, not failing.

One capability has a second entry point. `measure_files_delete_probes` runs the same probes with no declaration gate and no verdict — for a backend that implements `remove` and does not yet declare `FILES_DELETE`, where a failed probe is a finding about the mechanism rather than a broken promise. The loop it breaks: a capability may only be declared once its mechanism passes the probes, so the probes must be runnable against an undeclared mechanism or the gate can never open. Its results are the citable artefact in the argument over whether an undeleteable backend is an exceptional path (callback, at the kind) or a capability gap (router refuses the spec up front) — `maf-sandbox-acas` ran it against the real service while its declaration was open, and the measured verdict is what let the gate open; the suite now asserts through `assert_files_delete_conformance` instead, which is what the gate is for.

## Provenance

Extracted from a production agent application, where this seam was written for its first execution surface: a tool that compiles agent-authored infrastructure code in a sandbox. The minimum-isolation floor above is not a preference — it is what a security review concluded when it worked through what a shared-kernel boundary does *not* close for code an agent wrote.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
