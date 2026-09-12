# maf-sandbox

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox.MafSandboxExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568), written for use with [Microsoft Agent Framework](https://aka.ms/AgentFramework) but with no dependency on it in its protocol layer.

For workloads requiring `EXEC` or any `FILES_*` capability, `acquire` ensures the bound storage base exists, including on warm reuse. Existing directories retain their contents, ownership and modes; an unreadable path, a symlink or a non-directory fails acquire. This guarantees the base's existence on return, not additional guest permissions or the creation of per-call children. Runtime-only workloads require no directory.

`SandboxSpec.work_dir=None` lets the backend allocate its storage base; an explicit value is an image's exact guest-native override. The default remains `/maf-sandbox/work`. Address the base with `working_directory="."` and child directories with relative paths. File paths stay confined to their working directory. The backend resolves directories, while commands and argv remain opaque. Existing absolute addressing is accepted for compatibility.

**Relative call paths:** `SandboxToolSession.guest_call_path()` now returns a child name relative to the allocated base. Pass it as `working_directory`, or use `working_directory="."` when addressing a base-relative file. Do not prepend `spec.work_dir` or embed a guessed absolute base in an argv. `guest_run_layout` accepts that relative call path; its launcher resolves native paths inside the guest before starting Python. CodeAct uses backend allocation; Bicep retains the override needed to find its image's config file.

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

## Testing a confinement claim

A kind setting `SandboxSpec.confined_to_guest_call_path=True` owes `maf_sandbox.conformance.assert_nothing_left_behind` in its own suite. Supply a pristine sandbox's engine fingerprint subject and a callback that runs the kind's call and awaits its cleanup on that sandbox:

```python
from collections.abc import Awaitable, Callable
from maf_sandbox.conformance import FingerprintSubject, assert_nothing_left_behind

async def check_confinement(
    subject: FingerprintSubject, call: Callable[[], Awaitable[object]]
) -> None:
    results = await assert_nothing_left_behind(subject, call)
    assert all(result.passed for result in results)
```

The subject's async `fingerprint()` returns `SandboxFingerprint(changed_paths, running_programs)`, both immutable sets of strings answered from the engine. Paths are changes since creation; process identities must distinguish replacements. An initial path change refuses the probe before the workload runs. Any final path or process difference raises `ConformanceFailure`. Return `None` only when fingerprinting is unsupported: the probe then reports a skipped result without running the callback. Measurement errors fail, including loss of the fingerprint after the call.

An engine subject must refuse writable storage its diff cannot see and include implicit storage such as `/dev/shm` through engine reads. The pair says nothing about kernel state or open sockets. `InProcessSandbox.fingerprint()` supports harness tests through the fake's stores; it does not execute guest programs or detect bytes changed and then restored. It cannot establish a real workload's confinement. See [the cleanup contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/tool-call.md) for the engine requirements and remaining backend work.

## Call cleanup and concurrency

**Upgrade behavior:** call cleanup now defaults to `Cleanup.DISPOSE`. To accept reuse with possible residual state, set `SandboxRouter(min_cleanup=Cleanup.RECLAIM, ...)` explicitly. This also permits unconfined kinds when the backend supports reclamation. `SandboxSpec.confined_to_guest_call_path` describes an attempt to confine changes; it does not certify cleanliness. `FailedReclaimPolicy.KEEP` remains a separate decision about cleanup failure.

Ordinary `sandboxed_tool` call bodies may overlap through one router, including calls whose cleanup resolves to RESET or DISPOSE. RECLAIM removes each call's own directory immediately. Whole-instance cleanup, including failed-reclaim escalation, closes admission and waits for the active siblings to finish; the last call cleans each serving backend and physical instance once, using the strongest required rung. Entrants wait until all records succeed or enter the failure path. An admission wait is bounded per call ahead, by the tool's `admission_timeout` (120 seconds unless it states one) plus twice the tool's own cleanup bound (`reclaim_timeout` where it is set, the router's `reclaim.timeout` otherwise), allowing reclaim or reset followed by disposal, and restarts as each call ahead leaves and as each of its cleanup steps completes, since a call reclaims every instance it held and then cleans each one, all in sequence; a cleanup-completion wait is bounded to 120 seconds. A cancelled or expired waiter leaves the pending cleanup intact.

This gate coordinates one router's callers. It does not isolate concurrent siblings' data or coordinate multiple routers or processes. Hosts sharing a conversation concurrently across routers require `IsolationScope.CALL`.

Direct callers of the router's admission helpers must now **await `release_call(...)`**, because leaving the last hold can execute cleanup queued by another call. `enter_call(..., exclusive=True)` retains explicitly exclusive use, and a spec with `exclusive_admission=True` is held that way on every path: its calls run one at a time in a sandbox, and each pays a disposal under the default cleanup. `sandboxed_tool` handles this lifecycle automatically.

## Threat model

This package draws no isolation boundary itself — it is protocol and policy over whatever a `SandboxBackend` implementation actually provides. `Isolation` is a seven-rung ladder a backend declares itself onto, weakest to strongest: `none` (no boundary at all — the workload runs in the host process, with the host's authority), `runtime` (a software boundary inside the host process, e.g. a restricted interpreter or a WASM runtime's fault isolation), `os_process` (a separate OS process — a kernel-enforced address space, sharing the kernel and the filesystem), `container` (shared-kernel namespaces and cgroups), `hardened_container` (syscall interception in a userspace kernel — gVisor-class), `microvm` (a hypervisor boundary with a minimal or absent guest OS and no ambient identity reachable from inside — the default floor), and `vm` (a dedicated, full VM provisioned for the workload). `SandboxRouter` enforces the checks below on top of that declaration; the package's job is to make an unsafe backend selection fail loudly at construction or attach, not silently at first use. Beyond backend selection this layer holds no credentials, executes nothing and reaches no network, and everything security-relevant about a *specific* sandbox lives in the backend that implements it. It has exactly one boundary of its own, and it is on the way out rather than in: `make_file_system_sink` writes guest-produced bytes under a host directory, so it resolves each destination and refuses one that leaves that directory — see *Getting files back* below, which is also where a host landing somewhere other than a filesystem is told it owns the same question.

## The vocabulary

| | |
|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir, call_id)` — the one sandbox a caller may reach; `call_id` is empty unless the workload runs one sandbox per call |
| `SandboxSpec` | what a sandbox of a given *kind* needs: image, egress allowlist, work dir, `requires` capabilities, and an optional `min_isolation` that may raise the host's floor, and an `isolation_scope` that may raise how little of the conversation one sandbox serves |
| `Sandbox` | `write_file`, `exec` and `run_code`, the pull surface `stat_file` / `read_file` / `list_dir`, `remove`, and `reclaim` — what a workload gets, gated by what the backend declares. `reclaim` remains a required method and may refuse when safety cannot be established; `Capability.RECLAIM` admits router-managed reclamation and gates its conformance suite |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope`, plus the `isolation` it declares and the `BackendDeclarations` it hands the router |
| `BackendDeclarations` | the eight optional declarations in one object — `capabilities`, `limits`, `egress_modes`, `os_families`, `isolation_scopes`, `observes_egress`, `egress_method_tokens`, `attached_identity` — each field's default being its own silence rule |
| `SandboxRouter` | enforces all seven checks — the minimum-isolation floor, the capability match, the guest's shape, the transfer ceilings, the egress rule, the isolation scope and attached authority — against the one backend it picked, or, selecting per spec, against each registered backend until one passes |
| `SandboxPurger` | duck-typed `purge_scoped_thread(scope, thread_id)` for a host's delete path |

`Isolation`, weakest to strongest: `none < runtime < os_process < container < hardened_container < microvm < vm`. `SandboxRouter`'s default `min_isolation` is `microvm`; an unrecognised rung refuses rather than guesses which side of the floor it falls on.

`SandboxKey`'s scope and thread come from the host's request context through `CallerContext`, whose fields are **callables read at call time** rather than values. That is deliberate: a key a caller can supply is a key a *model* can supply, and that would let one conversation address another's sandbox.

`SandboxSpec.egress_allow` is an allowlist — everything not named is denied, so an empty tuple means no network. Stating it positively means a spec that forgets to mention egress gets the closed configuration rather than the open one.

Entries may also be `EgressRule("api.example.com", ("GET",))`. Methods are uppercase HTTP tokens — `HttpMethod` names the common verbs, any other uppercase token is accepted, and a lowercase one is refused because a rule names a verb rather than a spelling. A rule guarantees the verb, never the spelling that reaches. A scoped entry automatically requires `Capability.EGRESS_METHODS`. `SandboxSpec.required_capabilities` combines those derived requirements with the caller's explicit `requires`, so `dataclasses.replace` recomputes them from the current policy. The backend must declare both that capability and the tokens it can enforce through `BackendDeclarations.egress_method_tokens`; unsupported policy refuses at preflight and acquire. All-methods rules without an authority audience normalize to strings, equivalent entries collapse, and conflicting policies for the same host raise `ValueError`. No shipped backend declares method enforcement yet. GET narrows a channel without closing it: URLs, headers and request content can still carry data.

<a id="four-axes-six-checks-that-are-not-conveniences"></a>

## Attached authority

`BackendDeclarations.attached_identity` defaults to `NO_ATTACHED_IDENTITY`. `AttachedIdentity` names an `IdentityScope`, a positive platform-enforced `auto_delete_seconds` bound and the complete set of `AuthorityChannel` values exposed. The only supported channel is `EGRESS_HEADER`; guest token endpoints are unsupported. Scope orders `NONE`, `PER_SANDBOX`, `PER_SCOPE`, `SHARED` from narrowest to widest. Capability and attachment must agree or construction refuses.

A workload must explicitly require `ATTACHED_IDENTITY`, state `max_identity_scope` and positive `max_identity_retention_seconds`, and name concrete destinations with `EgressRule("api.example.com", authority="urn:example:resource")`. The host separately permits sharing through `SandboxRouter(max_identity_scope=...)`, default `NONE`. Core refuses ambient authority, wider sharing than either permits, excessive retention and channels the rules do not bound. These checks run before cold and warm acquire and participate in per-spec routing. An authority-only rule preserves its exact audience without deriving `EGRESS_METHODS`; wildcard authority destinations and conflicting audiences refuse at construction. Effective-state snapshots preserve the audience and attachment bounds, and attached authority activates the outbound confidentiality cap.

No real backend declares support. Backend assignment verification, principal pinning and hard lifetime enforcement remain necessary before one can do so; the fake models declarations only. The [identity contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#identity--whose-authority-sandbox-work-carries) states those obligations and the ACAS limitations. No mandatory `Sandbox` member is added. Host-tool `Identity.USER` and provisioned call credentials remain separate.

## Policy checks

```python
router = SandboxRouter(backends)                                   # default floor: Isolation.MICROVM
router = SandboxRouter(backends, min_isolation=Isolation.VM)       # stricter: dedicated full-VM only
router = SandboxRouter(backends, min_isolation=Isolation.NONE)  # a developer machine, opted down
```

**Which backend, when there is more than one.** By default the router resolves one at construction — `selected="docker"` names it, or the first registered one wins — and every workload gets that one, so a spec it cannot serve is refused with the other registered backends untouched. `SandboxRouter(backends, selection=Selection.PER_SPEC)` routes instead: the first registered backend that passes every check below, decided per spec. It is opt-in because of what it can move, and the claim needs its condition stated. For a router with **no `selected` pin** it can only ever *serve* a spec that is refused today — routing picks the first registered backend exactly as the fixed selection resolves to it — so nothing already running moves, and what changes is that a refusal becomes a running sandbox, which on a remote backend has a price. **Migrating off a pin is the case to check**: `selected=` and `PER_SPEC` are refused together, so a host dropping `selected="second"` has routing start at the *first* registered backend, and a workload the second was serving moves unless `backends` is reordered to match. Registration order is the preference, and the route is a pure function of the spec and what the backends declare — never load, latency or cost — so **one spec always routes to the same backend** and the warm sandbox `acquire` reuses stays reachable. Per spec rather than per conversation: two kinds under one key may route to different backends by design, which is why `dispose` asks every registered backend rather than one.

**1. The minimum-isolation floor.** A backend declares its own `isolation`, ranked on the ladder above. The router refuses, at construction, the backend it resolves to when it sits below `min_isolation` — or when its declared value is not a rung this package recognises, because nothing here can tell whether an unrecognised boundary is stronger or weaker than the floor. Under `Selection.PER_SPEC` there is no one resolved backend, so the same refusal is judged across the whole registration: construction fails when *nothing* registered clears the floor, and an individual backend below it is kept and never routed to. A spec may also carry its own `min_isolation`; the effective floor is the *stricter* of the host's and the spec's — a spec may raise the floor for itself and never lower it.

It refuses rather than degrades — under `Selection.FIXED`, where the backend it resolved to is the only one it will ever use. Promoting to a stronger backend unasked would hide a misconfiguration, and proceeding with the weaker one would break claims the host's security posture makes about every execution surface; neither is better than an error.

Under `Selection.PER_SPEC` a host has asked for that promotion, so routing does pass over a below-floor backend and serve on one that clears the floor. **The floor itself is never crossed** — every candidate is checked against it, so nothing below it can serve — but the passed-over backend would otherwise go unmentioned, which is the *misconfiguration* half of the paragraph above rather than the safety half. The per-spec refusal names it only when no candidate serves at all, since a successful route discards the refusals it passed over — so the router says it once, at construction, with a `logger.warning` naming each registered backend below the floor. It warns rather than refuses because a registration that includes a weaker backend is the arrangement this mode exists to serve, and it does **not** advise unregistering it: `dispose` and `dispose_scope` reach every registered backend, so a host that changed which one serves would strand whatever the old one still holds.

**2. The capability match.** A backend declares `declarations.capabilities` (a `frozenset[Capability]`: `EXEC`, `RUN_CODE`, `HOST_TOOLS`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `SNAPSHOT`, `RECLAIM`, `ATTACHED_IDENTITY`) — what it can actually do — and a spec declares `requires`, what its workload cannot run without. `ensure_can_serve(spec)` raises `SandboxCapabilityNotSupported` when the backend is missing something the spec requires — and where the router selects per spec, that check is also what *chooses*, so it raises only once every registered backend has refused, naming each. Unlike the floor, silence here is a functionality claim rather than a safety one: an unstated `capabilities` reads as exactly `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what this package's own `Sandbox` protocol already obligates, so a backend written before `Capability` existed does not have to start lying to keep working.

**3. The egress rule.** The spec names one mode (`ALLOWLIST`, `CLOSED` or `UNRESTRICTED`), which must belong to `BackendDeclarations.egress_modes`. A backend that does not declare any mode cannot serve a spec. An allowlist carries hostname entries and optional uppercase HTTP methods; method scope additionally requires `EGRESS_METHODS` and support for every token in `egress_method_tokens`. Preflight and acquire run the same checks, so unsupported policy is refused before the backend is called.

Missing in either direction is refused, and the symmetry is the rule rather than an omission. Confining **less** than the spec asks silently widens what the workload was designed to reach; confining **more** hands it a posture it was not built for, and a workload that fails at whatever it could not fetch fails somewhere no reader of the spec would look. A backend serves the mode it declares or turns the workload away.

**4. The guest-shape match.** A backend declares `declarations.os_families` (a `frozenset[OsFamily]`) — the guest shapes it hands out, `posix` or `windows` — and a spec declares `requires_os_family`, the shape its commands and scripts are written for. `ensure_can_serve(spec)` raises `SandboxOsFamilyNotSupported` on a mismatch, so a POSIX workload meets a Windows guest at attach rather than at its first command. **The axis is path grammar and argv quoting, and nothing else**: a spec asking for `posix` and getting it can still meet an image with no shell, because what is *installed* in a guest is a property of the image, and one backend may be handed many. `docs/sandbox/guest-platform-and-commands.md` settles where that separate question is answered. Silence here is neither of the readings above — an unstated `os_families` is the *absence of an answer*, read as `frozenset()`, which refuses a spec that asks and leaves every spec that does not exactly as it was. A backend with no guest in the operating-system sense, such as one serving a language runtime, has nothing to declare and declares nothing.

**5. The transfer-ceiling match.** A spec carries `TransferLimits` per direction — `max_bytes_per_file`, `max_total_bytes`, `max_files` — and a backend may declare its own ceilings as `limits`. A spec asking above them raises `SandboxTransferLimitsNotPermitted` rather than being clamped: a workload served a smaller cap than it declared fails part-way through a collection, and a partial artifact set is worse than none because the model cannot tell what it did not get. Silence follows the safety rule, not the capability one — an undeclared ceiling is the default ceiling, and a bigger ask is refused.

**6. The isolation scope.** How much of a conversation one sandbox serves. A spec declares `isolation_scope` — `conversation`, the default, one sandbox reused across the conversation's calls; or `call`, one created for the call and deleted when it returns — and a backend declares `declarations.isolation_scopes`, the scopes it can serve. `SandboxRouter(min_isolation_scope=...)` is the host's floor on the same axis, and the effective scope is the stricter of the two, exactly as with `min_isolation`. `ensure_can_serve(spec)` raises `SandboxScopeNotEnforced` otherwise, because a backend cannot answer a per-call workload by sharing: every call would succeed with the separation absent. Silence here is the one declaration read as a *claim* rather than as the absence of one — an unstated `isolation_scopes` means `{conversation}`, the get-or-create every backend already did.

`call` costs a cold start per call and buys what cleanup cannot: two calls of one conversation never meet in one filesystem, so a reclaim that failed, a program that would not stop, and anything a call left unnamed all stay where no later call can address them. A backend declares it once it folds `SandboxKey.call_id` into whatever names a sandbox — a container name, a label set, its own registry — and `maf_sandbox.conformance.assert_call_scope_conformance` is what holds it to that. None of it makes a removal an *erasure*: a snapshotted disk image can keep blocks an unlink released, which is a property of the backend's storage and not something the protocol states.

Note that the checks answer to different owners. How strong the boundary must be *here*, and how little of a conversation one sandbox may serve, are the *host's* policy, read from `min_isolation` and `min_isolation_scope` — and a spec may raise either floor for itself, never lower it. What a sandbox may reach, and what it must be able to do, are properties of the *workload*, stated in its spec. Keeping the axes apart is deliberate: merging isolation into a "required capabilities" list would let a workload ask for a weaker boundary than the deployment mandates.

`ensure_can_serve` is also the whole of a wiring test, in your own repository, against your own backend choice:

```python
router.ensure_can_serve(bicep_sandbox_spec())
```

## Getting files back — the declaration, and where it lands

A workload's only return channel used to be `ExecResult.stdout`, which is right for a diagnostic and wrong for a rendered image. `Capability.FILES_OUT` is the pull surface, and it is narrow in two deliberate ways: this library never *discovers* what a workload produced, and it never decides where the bytes go.

**Declare it.** A `DeclaredOutput` names one artifact as a literal path relative to the acquired storage base, in `SandboxSpec.declared_outputs`. Literal rather than a glob: resolving a pattern means enumerating a directory, which is the primitive `Capability.FILES_LIST` exists to gate, so a kind that cannot name its outputs in advance requires *that* capability and a backend serving only `FILES_OUT` refuses it. `media_type` is declared rather than sniffed, because sniffing lets guest-produced content decide how the host handles it. `required=False` is how a workload says an absence is normal — a renderer exiting non-zero produces no file, and the model needs that diagnostic rather than a transfer error stacked on top of it. `name` is the spelling the artifact *lands* under and defaults to `path`; the two come apart as soon as a kind writes into a per-call directory, which warm sandbox reuse forces on any kind whose outputs would otherwise persist into the next round.

`disposition` keeps the two flows apart because they answer to different legs of a host's policy: `LAND` goes to the sink and the question is confidentiality, while `CONSUME` is parsed by the kind that asked for it and the question is integrity. A `CONSUME` output is still counted against every cap — `files_out` bounds the collection the spec declared, not the subset of it that lands.

**Receive it.** `await collect_outputs(sandbox, spec, sink=...)` returns `LandedArtifact`s in declaration order. The order of its phases is part of the contract rather than an implementation detail: everything the declaration alone decides — a sink for anything that lands, a valid name for every output, no two landing names that collide — is settled before the sandbox is touched, then every declared output is stat-ed and capped, then the landing ones are read, and only then is anything delivered. Delivery is a push nothing can take back, so a refusal arriving after the first `deliver` could not leave the host as it found it.

`spec.files_out` is a `TransferLimits` and all three of its fields are load-bearing: a byte ceiling alone does not bound a collection, since ten thousand files one byte under the per-file cap cost exactly what the cap was written to prevent. What comes back when a collection does not fit is specific rather than generic — `SandboxTransferCapExceeded` names both the cap and the file that breached it, `SandboxOutputMissing` names a `required` output that was not there, `SandboxOutputSizeUnknown` is a backend that could not say how large something was, and `SandboxArtifactNameCollision` is two landing names that are one file at the destination: identical, or differing only by case or by Unicode form.

**Land it.** An `OutputSink` wraps a single `async def deliver(artifact) -> LandedArtifact`. This library holds no opinion about where an artifact goes — a directory, a blob container, a file store — which is what keeps that flow visible to the host's own information-flow policy instead of buried in a dependency. `LandedArtifact.display` is the one line the model is allowed to see; `handle` is the host's own reference, and nothing renders it into the transcript.

**`validate_artifact_name` is lexical, so a sink still has to confine its own destination.** It refuses `..`, absolute paths, backslashes and empty segments, so the *name* cannot traverse — which is not the same as safe, because it says nothing about what is already sitting at the path that name resolves to. A symlink in the output directory carries the write straight out of it: the same failure class as [#142](https://github.com/sokolaidev/maf-extensions/issues/142), on the host side of the boundary.

**`make_file_system_sink(root)` is that check, packaged.** It resolves each destination, refuses anything leaving `root` with `SandboxLandingNotConfined`, refuses one that is already there with `SandboxLandingExists`, creates the parents a nested name needs, and writes. That second refusal is the default because the name check in `collect_outputs` is per collection — a name is not fresh just because this call is, and a root more than one conversation lands in is a channel between them. It is an exclusive create rather than a look, so nothing takes the destination in between. **A workload landing one stable name wants `existing="replace"`**, since its own previous call is then the commonest thing in the way; and either way the refusal arrives per artifact, during delivery, so a collection whose third name is occupied leaves the first two landed. Reach for it rather than writing the four lines yourself — two samples here wrote them by hand and only one got it right. Pass `display` when the kind introduces its artifacts in its own words. It stays a check rather than a guarantee, and that is a property of the filesystem rather than of the helper: resolving and writing are two calls, so a host landing genuinely hostile output wants no-follow primitives underneath. What it closes is the standing case — something already in the way when the run started.

A sink landing somewhere that is *not* a filesystem — a blob container, a UI panel — writes its own `deliver` and owns the equivalent question for that destination.

**`make_file_store_sink(store, *, provenance=None)` is the packaged one for an `agent_framework` `AgentFileStore`**, and it lands `<call_id>/<name>` rather than `<name>`: the model reads its own output back with a file-read tool instead of being told by the workload which names landed, and one call's file can never answer for the next call's. The folder is the host-minted call id, which `collect_outputs(call_id=...)` supplies — required rather than optional, because the sink declares `OutputSink.per_call`. A destination that already exists is refused with `SandboxLandingExists` rather than replaced, every landing is recorded into `provenance` *before* the bytes are written, and an artifact whose bytes are not UTF-8 is refused with `SandboxLandingNotText` rather than mangled into a store that holds text. Point it at a store the model can read and **not** write — never the one the agent's `file_access_write` writes to. `sandbox_outputs_read_tools(store)` is the other half — `<prefix>_ls` and `<prefix>_read` over that store and nothing else, read-only by construction rather than by a flag. It exists because `FileAccessProvider` names its tools from fixed constants, so a second one of those is a name collision rather than a second store. [`docs/sandbox/hosts.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md) carries the wiring and the trade: those two tools carry no label of their own, so a host that withholds a workload's guest output and then wires them has moved that output onto a path it classifies rather than kept it away from the model.

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

**Dispose one kind while retaining the others.** `await router.dispose_kind(key, "codeact", timeout=30)` deletes only that kind's sandboxes across every registered backend, including backends that no longer serve new calls. It returns `True` when every backend reports success, or `False` on failure or timeout; logs and `SandboxDisposed` events carry the individual failures. The finite positive timeout covers the per-key disposal lock wait and the whole sweep, and cancellation propagates. Coordinate active calls before disposal, as with `dispose(key)`.

Like `dispose`, this host cleanup creates no refusal on failure. Success clears only the pending targets for that kind; another kind's target or a whole-key target keeps the key refused. For instance cleanup, pass `instance_id=sandbox.instance_id` to `dispose_kind`: it deletes only that engine instance on its serving backend. Failed instance cleanup refuses the key unless the host chose `FailedReclaimPolicy.KEEP`. `dispose_unclean(key, timeout=...)` retries the recorded backend/kind/instance targets; optional `kind` and `instance_id` selectors narrow the retry. The key reopens only once every target lands, and an older attempt cannot erase a newer failure. See [cleanup operations](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/operations.md#host-disposal).

A workload whose artifact names are not knowable when its tool is built passes the same `DeclaredOutput` type to `collect_outputs(outputs=...)` instead. That is refused unless the spec sets `outputs_named_at_call_time`: without the flag, the tool was attached with no sink required of it and no outbound cap agreed, and collecting there would land artifacts behind both checks.

[`samples/08_docker_codeact_files`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/08_docker_codeact_files) is all of the above as a runnable program, against a real engine.

## Host tools — the contract, and the backends that serve it

`Capability.HOST_TOOLS` is the one capability where trust crosses *outward*: a called function body runs in the host process, with the host's privileges, driven by model-written code, and each host-tool call bypasses whatever middleware the host runs. `maf-sandbox-docker` and `maf-sandbox-acas` declare it; `maf-sandbox-wslc` does not. The safety contract shipped first, before anything could use it, and it is what a host configures either way: `HostToolRegistry` starts empty (nothing is callable until a developer registers it, and registering emits a one-time, suppressible `MafSandboxHostToolsWarning`); `@sandbox_tool(source=..., sink=..., identity=...)` makes the developer answer every information-flow leg with no defaults (`None` is an answer — "not that role"); a `require_declared` gate refuses unstamped functions at registration, which is the only place the declaration is ever read — `register` captures it, `HostToolRegistry.aggregate()` seals the registry as it derives policy from it, and a stamp swapped or removed afterwards reaches nothing; `allowed_identities` (default `frozenset({Identity.APP})`) refuses at registration a tool exercising a broader authority — an `Identity.USER` tool, or an unstamped one read as `APP` — so user authority is opt-in (`frozenset({Identity.APP, Identity.USER})`), a tool declaring `identity=None` is always allowed, and `denied_identities` on the router stays the attach-time backstop; each run is bounded by a host-tool-call cap (`DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN`, refusals included) and by response size caps that reuse `TransferLimits`; arguments are validated host-side at the registry's one door, never in a guest shim; and a host whose posture wants a hard stop rather than awareness passes `denied_capabilities={Capability.HOST_TOOLS}` or `denied_identities={Identity.USER}` to its router.

One sentence to read before registering anything, because a declaration reads like a control and is not one: **`Identity.APP` is not the safe option, only the declared one.** It is the application's full authority, and the only real bounds on it are the emptiness of the registry and the host-tool-call cap — least privilege for host-tool calls comes from what a host registers, never from what it declares. `Identity.USER` is served only where a host mints it: give the registry `mint_user_identity`, an async callback returning that run's authority, and it reaches the tool body as `user_identity`. Without one, such a tool registers and its call is refused. Registering one raises the whole surface to approval-gated either way.

### Reaching the host from inside — `host_tool_calls_over_exec`

The contract says what may be called; it does not say how a host-tool call *reaches* a host whose guest speaks an exit code, stdout, and a stat-and-read pull surface. `host_tool_calls_over_exec` is that channel, built from those primitives and nothing else, and it is a helper a kind composes rather than anything the protocol requires. A kind writes the program and the generated shim (`host_tool_shim`) into a fresh per-run directory (`guest_run_layout`); `host_tool_calls_over_exec` writes the launcher itself, starts it detached and then polls for request files, resolves each one through `HostToolRun.call` — the same one door, with the same gates, cap and ceilings — and writes the answer back. It needs `EXEC`, `FILES_IN` and `FILES_OUT`, and deliberately not `FILES_LIST`. One run is two directories, and that is what keeps a guest-supplied name away from the machinery serving its own call: `WORK_DIRECTORY` is the program's working directory and the only one a kind puts model-named files in, while the shim, the launcher, the output, the exit marker and the calls directory sit in a sibling nothing a model names can reach. The program itself lives in the second one, beside the shim, because `sys.path[0]` follows the *script* rather than the working directory — run from the work directory it would put a guest file named `maf_host_tools.py` ahead of the real module, which is exactly the substitution a list of reserved names is hardest to get right about.

**Every supervised run attempts process cleanup, including success.** The launcher reports its program PID and optional dedicated PGID directly to the host before releasing guest execution. A shell helper polls a host-written start gate with `sleep`; it execs the program only after the receipt stream has closed and the host has validated it. Missing or invalid receipts refuse startup. Guest-writable PID/session files never supply signal targets. Process probes require the optional `BoundedExec.exec_bounded` interface, whose backend enforces a combined stdout/stderr byte budget before buffering a complete response. A backend without it reports unavailable observations. The transport collects bounded process snapshots before and after launch and cleanup, retains observed lineage and start ticks, signals the recorded group and observed escaped descendants, and reports survivors. Snapshots contain user IDs and command metadata for audit observers. Any incomplete or unavailable snapshot marks cleanup unclean even if later scans succeed. They are guest-observed diagnostics, not proof that no descendant survived; PID reuse and missed ancestry remain possible. The work directory stays available for artifact collection while the transport independently reclaims its own directory. See [cleanup and process observations](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/tool-call.md#process-cleanup-and-observations).

**The shim is not a control.** It runs where model-written code can read, edit or ignore it, and a program that writes request files itself is served identically. That is the design: every gate is host-side, and a check running in the guest would be decoration.

**The transport tries not to let its own files outlive the call.** It removes the ones it owns — the program, the shim, the launcher, the captured output, the exit marker, the pid, and every request and response the run exchanged with the host — on *every* exit path, success included, over the same `exec` it uses for everything else — **best-effort, not a retention guarantee**: a guest without `rm`, a removal that times out, or a non-zero exit each leave that traffic readable, logged and nothing more. What it cannot remove is `WORK_DIRECTORY`: artifacts live there and a kind collects them after the transport has returned, so removing it would delete the outputs of every successful run. `reclaim_run(sandbox, layout)` is the other half, **a kind's to call in a `finally` once it has collected**, and it takes the whole run directory. A `False` from it is a data-retention failure rather than a tidiness one: nothing comes back for it — the protocol's delete is capability-gated and this transport does not require it — and `acquire` is get-or-create, so a run directory that survives is readable by every later run in the same sandbox for the life of the conversation. **A kind that takes its place in the guest from `SandboxToolSession.guest_call_path()` does not have to remember any of this**: `sandboxed_tool` removes that path, and everything under it, when the call returns — after a result, a refusal and an exception alike — and hands a removal that did not happen to the host's `on_reclaim_failure` as a `ReclaimFailure` — a notification, delivered *after* the framework has already disposed the sandbox by default (see **Upgrading to 0.23** below). A kind that composes its own path keeps `reclaim_run`, and keeps the `finally`. Disposal is no longer the host's to arrange: the framework disposes an unclean sandbox itself and refuses the key until a disposal lands, and a host loosens that only by opting down on the router with `reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP)`, never per kind.

It costs round trips — several backend calls per host-tool call, plus polling, plus one on every return to reclaim, and one more to stop the program on a run that overran. It serves one outstanding call at a time. This module's own docstring counts those costs exactly, beside the code that decides them; whether the trade is worth it is a measurement rather than an assumption.

## Files outside the base, and a synchronous surface

Two helpers a host or a kind composes; the protocol requires neither. Both came out of the Deep Agents adapter's review ([#1108](https://github.com/sokolaidev/maf-extensions/issues/1108)): every consumer of the file plane meets the same questions, so the answers live here once.

**`maf_sandbox.file_transfer` — files over the shell, and one vocabulary for what the plane refuses.** The file plane (`stat_file`, `read_file`, `write_file`) is confined to the working directory it is called against. A host that has to reach outside it — a framework that keeps its own state under `/tmp` or a root-level directory — has `exec`, which is unconfined anyway, so `write_file_over_exec` and `read_file_over_exec` widen nothing the guest had not already opened. They run over `BoundedExec`: a write goes in base64 chunks into a sibling of the target named for the call and is moved into place once the last chunk landed, so a reader, or a second writer over the same path, sees a whole file and never an interleaving of two; a read is probed first, under a budget sized for base64 of the cap, and counted again after decoding, because a file can grow between the probe and the read. The probe does not read absence off `test -e`, which is false for a file behind an unsearchable ancestor as for an absent one: when it is false the probe opens the path and lets the shell's own error tell `permission_denied` from `not_found`. The road costs the utilities it runs — `SHELL_UTILITIES`: `sh`, `mkdir`, `mv`, `rm`, `base64`, `wc` — which an `EXEC` probe checking only `sh` does not establish; that is the image's to carry. What the guest refuses, on either road, is one `FileRefusal` (`not_found`, `is_directory`, `permission_denied`, `invalid_path`), raised as `SandboxFileRefused` by the shell road and read off the plane's exceptions by `file_refusal(exc)` — which answers `PermissionError` before the `OSError` it subclasses, and `SandboxTransferCapExceeded` and a timeout as no refusal at all — and off a stat by `entry_refusal(entry)`. A shell transfer whose command's end is unknown raises `SandboxShellTransferUnfinished`: the command may still be running and a write may have landed in part, so the caller treats the instance as unclean, the way it treats a timed-out `exec`. A caller cancelled mid-transfer sees `asyncio.CancelledError` as usual, and it means the same thing: condemn the instance on the way out, as the Deep Agents adapter does.

```python
from maf_sandbox import (
    FileRefusal,
    SandboxFileRefused,
    SandboxShellTransferUnfinished,
    file_refusal,
    read_file_over_exec,
    write_file_over_exec,
)


async def put_and_get(sandbox, base: str) -> bytes:
    try:
        await write_file_over_exec(sandbox, "/tmp/state.json", b"{}", working_directory=base, timeout=30)
        return await read_file_over_exec(
            sandbox, "/tmp/state.json", working_directory=base, timeout=30, max_bytes=1 << 20
        )
    except SandboxFileRefused as refused:
        assert refused.refusal in FileRefusal
        raise
    except SandboxShellTransferUnfinished:
        raise  # the instance is unclean: dispose it, or queue its cleanup with the router


def code_for(error: BaseException) -> str:
    refusal = file_refusal(error)
    return "failed" if refusal is None else refusal.value
```

**`maf_sandbox.sync_runner` — one loop on a daemon thread.** A framework that calls a sandbox from synchronous tools on worker threads has no loop there, and a caller already holding a running loop cannot nest another. `SyncRunner().run(coroutine)` runs it on one loop of its own and waits on a future, from any thread. One loop for the process rather than one per call, because a backend may cache a client per loop (ACAS does) and never evict one for a loop that closed. A fork carries the loop into the child but not its thread, so the runner resets under `os.register_at_fork`, with a fresh guard, and the child's first call starts its own; without that reset the child waits forever on a future nothing serves.

## A result the model may read half of

A body returns a string or a list of unlabelled `Content` items. To keep a standing sentence readable beside diagnostics, commit it with `sandboxed_tool(..., standing_guidance=(RECOVERY_ROUTE,))` and return it last on every path:

```python
from agent_framework import Content

return [
    Content.from_text(rendered_diagnostics),
    Content.from_text(RECOVERY_ROUTE),
]
```

The wrapper checks the trailing text against the commitment and rebuilds those items as trusted/public guidance. Its text, count, order and placement are fixed; only `{call_id}` may interpolate. Missing guidance, guidance without a derived item before it, and any label supplied by the body are refused. `labelled_result_item` has been removed: replace it with `Content.from_text` and commit the sentence at attach.

**A host can require integrity before a file reaches the workload.** Pass `requires_file_integrity=SourceIntegrity.TRUSTED` to `sandboxed_tool`, beside `file_store_provenance=record`, to refuse files whose integrity falls below that level after the read's provenance fold. `None` leaves admission unrestricted; unestablished integrity is below every level, so a trusted requirement on an unestablished store refuses every file. `FileStoreProvenance(floor=...)` still names the integrity of unrecorded paths, independently of what a tool requires. The refusal is an ordinary `str` from `SandboxToolSession.read_file`, recorded as `StoreFileRead(outcome="refused")`, and needs no result or confidentiality label. See [host wiring](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#file-store-provenance--what-a-kind-reads-and-what-it-is-worth) for the fold, refusal behavior, and limits.

**The file fold can weaken a call's result.** A host enables this by setting the attached tool's `additional_properties["confidentiality"]` to its classification, alongside a valid `source_integrity` declaration. The wrapper stamps every derived item with the weaker of that declaration and the files the call actually read through `SandboxToolSession.read_file`, copying confidentiality verbatim. An untrusted or unestablished read demotes a trusted declaration; a trusted read never promotes an untrusted one. A call that read nothing keeps its declaration, and refused or absent reads do not count. Strings become one stamped item, including returned error sentences. The declaration itself is never changed, so concurrent calls keep separate answers.

Without both valid declarations, derived items remain unlabelled and take the framework's source declaration, input join, or defaults as applicable. `max_allowed_confidentiality` is an outbound cap and does not enable this feature. Guidance keeps its committed label regardless of the file fold. Both shipped kinds declare untrusted, so this never promotes their diagnostics or changes which guidance remains readable. The runtime check also applies to a kind using `nothing_survives_from=(SourceChannel.FILE_STORE,)` to justify a trusted declaration: reading weak content still demotes that call when the host has enabled stamping.

Only text whose value and presence are independent of unestablished sources qualifies as standing guidance. Counts, exit statuses and conditional advice remain derived items. See [information flow](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md#how-core-labels-a-call) for the ownership model and decision table, [writing a kind](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/writing-a-kind.md) for a complete factory and body, and [host configuration](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#classify-derived-tool-results) for the distinction between result confidentiality and outbound caps.

## Recording what the sandbox did

Standard logs carry operational messages, process identifiers and cleanup outcomes. A deployment asked *which conversation reached that host, which host tools ran under whose authority, what crossed the boundary and with what label* answers from records, so `SandboxObserver` is the seam that hands them over: ten frozen events, in this package's own vocabulary, joined by the `SandboxKey` that addresses a sandbox. `SandboxAcquired`, `SandboxDisposed` and `EgressObserved` always carry one; `HostToolCalled`, `ProcessesObserved`, `ProcessCleanup`, `StoreFileRead` and `OutputsCollected` type it `SandboxKey | None`, since each has a case with no sandbox behind it; `ToolCallEnded` carries `keys`, a tuple of every key the call touched — acquired, refused, or only read the store under — so that each *call-scoped* event has a call to join to, which `EgressObserved` is not: its window spans calls and it anchors on the key alone; and `ScopeDisposed` carries none, because a `dispose_scope` is answered with a count rather than with the sandboxes it removed, so it names the conversation and joins on `(scope, thread_id)`.

**A key names a conversation, not a call**, and at the default `IsolationScope.CONVERSATION` two calls in flight on one thread share one. So every event carries a second column, `call`, the id of the tool call it came from: never `None` on `ToolCallEnded`, which is where a call's other events join, always `None` on `EgressObserved`, whose window spans whatever calls ran between two removals, and absent on the other six for what genuinely happened outside a call — an acquire a direct consumer of the router asked for, a scope purge run by a thread deletion, a collection a kind ran outside a tool body, and anything a task the body left running does after the call has ended. It is the same id the call's guest path is named by, and its key's `call_id` at `IsolationScope.CALL`, so a recorder holds one string for a call rather than two.

**And the call record says what fed it.** `ToolCallEnded.fed` folds what the call read out of the agent file store — `weakest_integrity` over the reads that carried text, with how many it folded — so a host asking whether a call was fed anything it never established reads one field rather than joining its `StoreFileRead` records and re-deriving the ordering. The fold is the call's rather than the observer's, so a read made through a second session whose router records nothing counts in it, and `reads` can exceed the `StoreFileRead` records you hold. A read that answered `absent` or `refused` fed nothing and is not in it, and a call that read nothing carries no fold at all: the fold answers `trusted` for an empty listing, which is honest about a result deriving from no file and would read here as a call fed trusted content. It describes inputs, never a result — what a result derives from is the kind's declaration.

```python
from maf_sandbox import (
    HostToolCalled,
    HostToolRegistry,
    Isolation,
    SandboxAcquired,
    SandboxObserver,
    SandboxRouter,
)
from maf_sandbox.testing import InProcessSandboxBackend


class Records(SandboxObserver):
    """Override what you want; every event the base class answers with nothing."""

    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        emit(thread=event.key.thread_id, egress=str(event.spec.egress), refused=event.refusal)

    def host_tool_called(self, event: HostToolCalled) -> None:
        emit(tool=event.tool, sink=event.sink, how=event.outcome, bytes=event.response_bytes)


def emit(**attributes: object) -> None:
    """Wherever this host's records go — a queue, an exporter, a SIEM."""


records = Records()
# Both registration points, since `host_tool_called` above comes from the registry and never
# from the router. The floor is lowered only for the in-process fake, which declares
# `Isolation.NONE`; a real backend leaves the default `microvm` floor where it is.
router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE, observer=records)
registry = HostToolRegistry(observer=records)
```

`SandboxAcquired`, `SandboxDisposed` and `ScopeDisposed` come from the router, and so does `EgressObserved` — reported by a backend that can read its own egress enforcement, through a reporter the router hands it; `HostToolCalled` from `HostToolRegistry(observer=…)`, which is where every other host-tool policy lives; `StoreFileRead` from `SandboxToolSession.read_file` and `ToolCallEnded` from the wrapper `sandboxed_tool` builds, both reading the router's; and `OutputsCollected` from `collect_outputs(..., observer=session.observer, key=key)`, which is a function rather than a policy object and so takes both as arguments.

Three things to know before writing one. **Every way out is recorded** — a refused acquire, an exhausted host-tool cap, a collection refused part-way, a call taken by a cancel — and an acquire's, a collection's or a call's failure is recorded as the exception's *class name*, never its message, which can carry a backend's endpoint. `HostToolCalled.refusal` is the exception: it holds the sanitized sentence the guest was answered with, so treat that one as guest-influenced rather than host-only. **An observer cannot fail a call**: its exceptions are contained and logged, and its return value is never read. **It can, however, slow one down, and it is entered from more than one thread** — it runs wherever the call is served, which for a synchronous tool body is a worker thread, so hand the event to a thread-safe queue or a batching exporter and do no I/O in it. A host that registers nothing builds no event at all.

**What *held* has a second destination, because the two survive different things.** A span survives with the trace and whatever sampled it; somebody reading a conversation back a month later has the transcript and no trace at all. `EffectiveState` is one served acquire as a value rather than an event — the backend that answered, the isolation rung and the resolved scope, the egress mode with its allowlist, both sides of the capability match, every tool the sealed host-tool registry was carrying, and the `call` that joins it to every event that call emitted — and `effective_state_middleware()` writes it into `AgentSession.state`, one entry per tool, overwritten each call:

```python
from agent_framework import Agent

from maf_sandbox.maf import effective_state_middleware

agent = Agent(..., middleware=[effective_state_middleware()])
```

It records the served answer and not the ask, so a refused call writes nothing — it already has an exception, a log line and a `SandboxAcquired` carrying the refusal. And it carries posture, never payload: no model-chosen text, and neither the spec's `labels` nor the `SandboxKey`, since this record is persisted beside a transcript a deployment may classify differently. The call id is the one identifier on it, because a session already knows which conversation it is and cannot say which call.

[`docs/sandbox/observability.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md) carries what each event holds, what a recorder should treat as guest-chosen, and what the seam does not yet see. Egress is no longer on that list for the backends that enforce it themselves: `docker` and `wslc` report their proxy's `ALLOW`/`DENY` decisions, and a backend enforcing in a service it does not run says so through `observes_egress` rather than by being silent.

## Upgrading to 0.27

**These landed in the tree tagged `maf-sandbox-v0.26.0`, which never reached PyPI.** That tag and its GitHub Release are immutable and will stay visible; there is no 0.26.0 to install, and the same tree ships as 0.27.0. The changelog's 0.26.0 section says why.

**A backend's four optional declarations became one object.** `capabilities`, `limits`, `egress_modes` and `os_families` were four attributes the router read off a backend with four `getattr` calls. They are four fields of one `BackendDeclarations`, read with one, and **a backend still carrying any of the four attributes is refused when the router resolves it** — at construction, with the attribute named. That refusal is deliberate: none of the four was ever a member of the `SandboxBackend` protocol, so `isinstance` holds either way and nothing in the type system marks a backend half-moved, while a stray attribute is silently replaced by that field's default. On `egress_modes` the default enforces nothing and refuses every workload; on `limits` it *widens* a ceiling the backend meant to be narrow.

| Was | Is |
| --- | --- |
| `capabilities: frozenset[Capability]` on the backend | `declarations.capabilities` |
| `limits: SandboxLimits` on the backend | `declarations.limits` |
| `egress_modes: frozenset[Egress]` on the backend | `declarations.egress_modes` |
| `os_families: frozenset[OsFamily]` on the backend | `declarations.os_families` |

```python
from maf_sandbox import BackendDeclarations, Capability, Egress, Isolation

class MyBackend:
    name = "mine"
    isolation = Isolation.CONTAINER
    declarations = BackendDeclarations(
        capabilities=frozenset({Capability.EXEC, Capability.FILES_IN}),
        egress_modes=frozenset({Egress.CLOSED}),
    )
```

**Each field's default is its own silence rule, and the four still differ** — `capabilities` reads as `DEFAULT_CAPABILITIES`, `limits` as `DEFAULT_SANDBOX_LIMITS`, and `egress_modes` and `os_families` as the empty set. So a field left unstated means exactly what an absent attribute used to, and a backend that declares neither the object nor any of the four attributes it replaced reads as `DEFAULT_BACKEND_DECLARATIONS`. One that still carries any of the four is refused at construction, per the paragraph above — declaring no object is not a way to stay unmigrated. `isolation` did not move: it is a protocol member, because a backend with no rung cannot be placed against a floor.

`capabilities` and `egress_modes` are now also refused when they are not a *set* — the router subtracts one and tests membership in the other, and a string or a list used to raise a bare `TypeError` out of a host's agent factory. The members are not checked, so a backend declaring plain strings still matches: `Capability` and `Egress` are `StrEnum`.

**`maf_sandbox.testing.InProcessSandboxBackend` lost its `capabilities=`, `limits=`, `egress_modes=` and `os_families=` keyword arguments**, replaced by one `declarations=`. Override with `dataclasses.replace(FAKE_BACKEND_DECLARATIONS, ...)` rather than constructing a bare `BackendDeclarations`: the fake's default states `egress_modes={ALLOWLIST, CLOSED}` so a workload under test attaches, and a bare object resets it to the rule that enforces nothing.

```python
import dataclasses

from maf_sandbox import DEFAULT_CAPABILITIES, Capability
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

# was: InProcessSandboxBackend(capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT})
# is:
InProcessSandboxBackend(
    declarations=dataclasses.replace(
        FAKE_BACKEND_DECLARATIONS, capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT}
    )
)
```

**A backend says a delete failed by returning, not by raising.** `dispose` is contractually best-effort and never raises, so the refusal 0.23 shipped — a key held closed until its disposal lands — could never fire against a compliant backend: each swallowed its delete error, said nothing, and was read as having disposed. Both disposal methods now carry the answer back:

| Was | Is |
| --- | --- |
| `async def dispose(key) -> None` | `dispose(key, *, kind=None, instance_id=None) -> DisposalFailure \| None` — a code to branch on, and a detail to log |
| `async def dispose_scope(scope, thread) -> int` | `-> ScopePurge` — `.disposed` is the old count, `.undisposed` the failure |
| `router.dispose_scope(...)` → `int` | → `ScopePurge` |
| `purger.purge_scoped_thread(...)` → `int` | → `ScopePurge` |

`kind` restricts deletion to that workload, including retained failures on retry; `None` deletes every kind. `instance_id` selects one physical sandbox within the key and optional kind. The example assumes the client accepts both selectors, verifies engine ownership and treats an absent ID as a no-op without selecting a replacement. Backends must also implement `reset(timeout=...)`, raising `NotImplementedError` when they do not declare `SNAPSHOT`.

A backend may retry retained cleanup before acquisition and refuse acquire while cleanup remains pending or a scope purge is active. Direct backend callers must handle these admission failures; the router separately enforces its unclean-key guard. Before purging a conversation, stop new work for it across replicas: a local backend guard cannot prevent another process from creating a sandbox. An incomplete purge must be retried.

**The code is the contract; the detail is not.** `DisposalCode` is a closed set — `unreachable`, `timeout`, `refused`, `unlisted`, `unknown` — and it is what a caller acts on: retry an `unreachable`, raise the bound on a `timeout`, put a `refused` in front of a human, since it is a missing role far more often than anything transient. `detail` is the backend's own sentence, for a log, never to be parsed.

```python
async def dispose(
    self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
) -> DisposalFailure | None:
    try:
        gone = await self._client.delete(key, kind=kind, instance_id=instance_id)
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

**`Sandbox` gained the required `reclaim` method in 0.23.** Current backends implement either safe reclamation or an explicit `NotImplementedError` refusal. Declare `Capability.RECLAIM` only when safe reclamation is established; it admits reclamation for router-managed cleanup, and its conformance suite refuses an undeclared capability before planting. Without that declaration the router selects a stronger established cleanup rung. A third-party backend must provide this method even when it refuses:

```python
async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
    ...
```

The caller supplies a directory it created under `working_directory`, but the guest can replace that path or an ancestor before cleanup. The **reach rule** still applies: removal must not delete anything the guest program could not have deleted itself. The backend owns the mechanism and any checks needed to establish safety, and refuses if it cannot. A framework-chosen name does not license an absent check. For removal that can safely be attempted, an absent directory is success and other failures raise so the caller can escalate.

`working_directory` says where the directory sits; it is **not** a directory to run the removal from. Acquire prepares the base, but a caller's child may never have been created, and the guest can remove a directory before cleanup. Resolve relative targets against the bound storage base and require a child of the working directory. Legacy absolute targets retain their native placement guards. Removal must not depend on changing into the target first.

Docker declares `RECLAIM` and implements it through `rm -rf`, as root when its acquire-time reach check permits it and otherwise as the image's user. Router-managed reclamation requires explicit host opt-in through `Cleanup.RECLAIM` and a workload cleanup floor that permits it; kind confinement metadata is advisory. ACAS and WSLC withhold `RECLAIM` and `SNAPSHOT`, so router-managed cleanup disposes their sandboxes; direct reclamation raises `NotImplementedError` ([ACAS #1088](https://github.com/sokolaidev/maf-extensions/pull/1088), [WSLC #1036](https://github.com/sokolaidev/maf-extensions/pull/1036)). The in-process fake declares `RECLAIM`, removes entries from its store, and records the call.

**A sandbox the framework could not clean is disposed, by default.** Even under explicit `Cleanup.RECLAIM`, an unusable launcher receipt, an unconfirmed signal, an incomplete or unavailable process snapshot, a failed descendant cleanup, observed running survivors, or a failed directory reclamation reaches the unclean policy. `sandboxed_tool` disposes from its `finally` before `on_reclaim_failure` runs; `ReclaimFailure.disposal` reports `"disposed"`, `"failed"` or `"kept"`. A successful lone-PID signal may permit reuse when the other checks succeed, although none of these observations proves complete cleanup. A disposal that does not land makes the router refuse the key with `SandboxUnclean` until one does. The host can retain an instance after cleanup failure with `ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP)`; ordinary reuse also requires an explicit `min_cleanup` opt-in. `reclaim_timeout` bounds removal, disposal and reporting separately, so a failing call can cost up to three times it.

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


**Every supervised run performs bounded process observations and cleanup, including success.** Launch observations share the run's `timeout`; they cannot add another collector budget after it expires. Process cleanup has one `_PROCESS_CLEANUP_GRACE` (5s) shared by observations, the recorded-target signal, escaped-descendant signals and verification. Pre-signal observations get at most one quarter of that budget so a slow collector cannot spend all the time reserved for signals. Exhausted observations are recorded as unavailable, and directory reclamation still gets its separate `_RECLAIM_GRACE` (10s). A run can also spend `_FINAL_READ_GRACE` (2s) on the last exit-marker read, another `_FINAL_READ_GRACE` on a completed result's final output, and `_RESPONSE_WRITE_GRACE` (2s) recording a host tool's answer. These allowances total `timeout + 21s` of transport overhead; they do not bound the whole call. The supervisor never cancels a host-tool call already under way, so an outer deadline must also allow for the slowest registered tool. A tighter outer deadline may lose the timeout's partial output and cancel a tool whose effect is only partly applied.

**`GuestRunLayout` gained a `pid` field, and constructing one yourself is a `TypeError` until you pass it.** `guest_run_layout` fills it in, so a kind that uses the factory — which is every kind that follows the documented path — needs no change at all. The field is where the launcher records the program's process id, which is what lets a run that overruns be stopped instead of left going.

**Two more names are reserved in a run's transport directory:** `program_pid` and `program_pid.part`. `guest_run_layout` refuses a `program` named for either, on the same grounds it already refuses `program_exit_code.part` — the launcher writes them, so a program under one of those names is written over. This is about names, not reach: a model-supplied file name cannot collide with them, because a kind writes model-named files only into `work/`. A *program* can still open anything it likes by absolute path — the shim sits where model-written code can read and edit it, and so does everything beside it.

**A timed-out host-tool-call run now signals the guest program**, where before it left it running. A run that reached the program gained a clause saying whether the signal was sent, so a host matching the old text no longer matches those. Only one message for a run that never got that far is unchanged, the launcher upload running out. The launcher's own `exec` running out with no pid gained a clause too, because the launcher backgrounds the interpreter before it writes the pid down: a call that expires between the two leaves a program running and no pid to point at, so that message now says the start could not be established rather than quietly implying none happened. If your host disposes the sandbox on every `SandboxProgramTimeout` to reclaim the CPU, **keep doing that if you need the program actually gone.** The message distinguishes a signal that was sent from one that was not, which is less than it sounds: a sent signal can be discarded, aimed at a recycled numeric ID, or miss unobserved descendants, so it is not confirmation of termination and disposal is still the only thing that is. The exit marker is a guest-writable report, not proof that the program or its descendants have stopped.

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

**`host_tool_calls_over_exec` raises `SandboxProgramTimeout` for its own bound.** A `TimeoutError` from it used to mean either the run running out or a backend bounding one of its own calls, and callers could not tell which. The new type — a `TimeoutError` subclass, so existing handlers keep working — is the first. A **bare** `TimeoutError` is the second, and says nothing about whether the program is still running — validation errors and whatever a backend raises for its own reasons come through as themselves, unchanged. It carries the program's partial output on `output`, and what the transport managed to do about the program on `signal` — `sent`, `refused`, `absent`, `unrecorded`, `unknown`. **Branch on `signal`, not on the message text**, which is prose and will keep moving. `absent` means no launcher ran or no recorded process remained observable after cleanup; unobserved descendants can still exist. No signal outcome establishes complete cleanup, so a host that needs it gone still disposes the sandbox. Raising this type yourself reports `unknown` unless you say otherwise.

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

A backend that states no `capabilities` field is read as declaring `DEFAULT_CAPABILITIES` (`exec` + `files_in`), which is what the `Sandbox` protocol already obliges. Declare a wider set to serve workloads that require more.

## Writing a backend

Implement `name`, `isolation`, `acquire`, `dispose`, `dispose_scope`, and a `declarations` holding a `BackendDeclarations`. The object is optional and every field in it has a default, but `egress_modes` is the one you must state — silence there is the empty set, and the router refuses every ask. One migration rule the router also enforces: **do not leave any of the four declaration fields as a bare attribute on the backend** — they were attributes before 0.27, the router refuses a backend that still carries one, and a stray attribute is read by nothing and silently replaced by that field's default.

The ordered path through the rest — each `Sandbox` method with what it owes, what to reach for, what never to do, and the probes that prove it; the bundle menu and the three things the stat it runs must be; when `reclaim` must refuse and withhold its capability; the acquire race, the label rules and the disposal contract; the six `assert_*_conformance` suites against a real instance — is [`docs/sandbox/backends/writing-a-backend.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/writing-a-backend.md), with the shipped backends' declarations beside it in [`docs/sandbox/backends/README.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/README.md).
## Provenance

Extracted from a production agent application, where this seam was written for its first execution surface: a tool that compiles agent-authored infrastructure code in a sandbox. The minimum-isolation floor above is not a preference — it is what a security review concluded when it worked through what a shared-kernel boundary does *not* close for code an agent wrote.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).

## Exec bytes and text views

`ExecResult.stdout_bytes` and `stderr_bytes` preserve returned program bytes; `stdout_text` and `stderr_text` (also `stdout` and `stderr`) are UTF-8 display views with replacement decoding. Use the byte fields for artifacts and byte counts, and the text views for model or JSON display. See the [output contract, ACAS prerequisites and release migration](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/exec-output.md).
