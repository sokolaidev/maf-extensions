# `wslc` — WSL containers

> The developer-machine backend on Windows: a container in about half a second, no daemon and no login, serving `exec` and files *in* only. Install and configuration: [`packages/maf-sandbox-wslc/README.md`](../../../packages/maf-sandbox-wslc/README.md).

## What it declares

The declared capabilities are a ceiling for a conforming image. Acquire checks `sh` for `EXEC` and the external `test` command for `FILES_IN`, including its true and false exit statuses as the root principal the write-path check uses. A shell builtin cannot satisfy that external-command check. Unresolved write ownership also refuses `FILES_IN` at acquire. Successful command checks are cached per engine instance ID; failed checks are retried, and a refused container stays tracked for host disposal. The checks do not strengthen guest-answered path checks or enable reclamation. See [the ceiling and probe contract](../guest-platform-and-commands.md#decision-3--a-static-ceiling-matched-at-attach-and-a-probe-at-acquire).

The four below `isolation` are fields of this backend's `declarations`.

| Declaration | Value |
|---|---|
| `isolation` | `Isolation.CONTAINER` |
| `capabilities` | `EXEC`, `FILES_IN` — the narrowest set any shipped backend declares, and it has not grown: no `FILES_OUT`, no `FILES_LIST`, no `FILES_DELETE`, no `RUN_CODE`, no `RECLAIM`, no `SNAPSHOT` |
| `egress_modes` | `{Egress.CLOSED}`; `{Egress.CLOSED, Egress.ALLOWLIST}` when an egress proxy image is configured. Never `UNRESTRICTED` |
| `limits` | **not declared** |
| `os_families` | `{OsFamily.POSIX}` — a constant rather than a read: `wslc` runs Linux containers in WSL 2's utility VM and has no other guest to hand out, so there is no engine to ask |
| `isolation_scopes` | `{IsolationScope.CONVERSATION, IsolationScope.CALL}` — see [one sandbox per call](#one-sandbox-per-call) |

`container` is below the router's default floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER`; with nothing passed, construction raises. That refusal is the point of the declaration, not a limitation to work around — there is no flag left to forget.

## What it needs

**Windows with WSL 2.9.3 or later**, and nothing else to install: `wslc` ships as part of WSL. Every call spawns `wslc.exe`, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so. This is the one shipped backend that runs on a single operating system, which is exactly why [`docker`](docker.md) rather than this one carries the live gate.

## Lifecycle

Creates land in **about half a second**, which is what makes this the backend to iterate against. Names are derived from a digest of scope, thread, agent dir, kind and egress identity, so acquire and dispose agree without a registry; get-or-create is serialised per `(loop, key, kind)`, because a create names no container until it returns and two racing acquires would each build a network, a proxy and a sandbox. Labels are written at create and both `dispose(key, kind=...)` and `dispose_scope` select on them from the CLI's own listing, with values hashed rather than truncated for the reason every backend here hashes them — a shared prefix would let one conversation's purge delete another's containers.

**A spec's mode is enforced or refused, never approximated.** With no proxy image the set is `{CLOSED}` alone, so a workload running `ALLOWLIST` is refused at attach rather than handed the closed run it did not ask for; with one, both modes are enforceable and a spec naming no hosts still resolves to the closed shape. What the modes mean is [`../network.md`](../network.md).

**Egress scaffolding is re-ensured on every acquire, not only on create.** A proxy a host reboot stopped, or one a crashed setup left half-connected, is rebuilt here — the alternative is handing back a sandbox that declares an allowlist and enforces nothing, which is the exact failure the honesty rule exists to prevent. Rebuilding it is also what makes its *record* perishable, so this backend implements `ObservesEgress` and drains the proxy's `ALLOW` and `DENY` lines before every removal that would take them, in `_ensure_proxy` and in `_purge` alike — so each `EgressObserved` covers one acquire's window rather than a sandbox's whole life. A window with nothing to report emits nothing, which makes an absent record inconclusive rather than reassuring: a live proxy's current window is unreported until a removal, and an unattributable one is never reported. A purge recovers attribution from the proxy's engine labels, including proxies created by another replica or left behind by failed setup. A nonempty `maf-sandbox.key.v1` label is a URL-safe base64 encoding of a JSON array containing the exact scope, thread ID, agent directory and call ID (empty for conversation scope); the existing ownership selectors and derived names stay unchanged. Decoded values must agree with those selectors. The encoded attribution budget is 4,096 bytes. Larger keys still acquire normally: the backend warns and writes an empty label, which disables recovery rather than falling back to partial legacy selectors. Scope purges and reaping cannot attribute those proxies. Legacy proxies are attributable only when all three selectors contain unchanged plain values; a hashed selector cannot be reversed. Key-addressed disposal and acquire can still drain legacy or oversized-key proxies using the caller's key. Scope purges and reaping inspect the proxy and read its log by that same engine ID. Missing or malformed attribution emits no egress event, and absence alone does not prove a window was lost. `observes_egress` reports attributable windows, not a complete audit history; failed or cancelled removals publish no event, and a successful sequential retry reports its window once. Overlapping cleanup can still report the same window more than once (see [egress observation](../observability.md)). The topology is the internal-network-plus-CONNECT-proxy shape [`docker`](docker.md) copies verbatim, and so is the drain; the axis is [`../network.md`](../network.md).

## Write ownership

`write_file` stamps the image user's uid and gid onto files and explicit entries for missing directories at or below `working_directory`. Existing directories retain their ownership and modes; missing ancestors above that boundary are left to the transport. Acquire reads numeric `Config.User` pairs from container inspection, treating an empty user as root. Named identities and omitted groups use bounded guest `id` replies; unresolved identity refuses the write. These facts are read afresh on each acquire.

A non-root guest can modify its inputs and create outputs beside them. The transport still copies with host authority: stamping ownership is not an atomic guest-authority write and does not close concurrent path redirection or the residual left where the stat asks the guest. The live suite runs the shared reach probe on an image that gives its non-root user the working directory; that probe checks the resulting ownership, not atomic resolution.

## Why it serves neither `FILES_OUT` nor `FILES_LIST`

`wslc container cp` has three forms — local→container, container→local, and stdin→container — and **no container→stdout form**, so there is no reverse of the tar it writes on the way in and no tar header to read a type and size from before the content. Container→local writes a raw file to a host path with no header at all. Worse, a **symlink source exits 0, writes nothing to stderr, and produces a 0-byte file** — neither preserved, nor followed, nor refused, and indistinguishable from a legitimately empty artifact. Confinement requires refusing a link whether or not its target would have resolved somewhere legitimate, and this mechanism cannot tell the difference at all. Serving the capability anyway would mean an `exec`-based `stat` before every read, which requires the image to contain a shell — precisely the dependency the `FILES_LIST` split exists to avoid. So `stat_file`, `read_file` and `list_dir` raise `NotImplementedError` naming the backend and the reason: the router refuses such a spec before a workload runs, and the raise is the honest floor under a caller that skipped the check, where a bare `AttributeError` would name neither the backend nor the file and read as unrelated to a `write_file` that had just succeeded.

Deferred rather than rejected, in [#125](https://github.com/sokolaidev/maf-extensions/issues/125), and filed upstream as [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (the symlink bug) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (the missing stdout form) — **either of which reopens the question**.

## No `FILES_DELETE`, no `RUN_CODE`, and no `limits`

`remove` raises too, and not for want of `rm` — nor for want of the check. Confining a removal means classifying the path's ancestors, and this backend **does** run that check: `write_file` builds it from the private `_stat_guest` through `confine_resolve_guest_write_path`, and `confine_resolve_guest_delete_path` is the same ancestor check with the final component left alone. **Who answers it used to be the blocker, and is not any more.** `container cp` settles a directory and a missing path out of the engine, and exits 0 for everything else, so those two answers — the ones that let the check continue — come from outside the container. What the guest is asked is which non-directory kind a streamed component is, and a claim of a directory contradicts the engine and is dropped, so no answer it gives carries a path through a link ([#495](https://github.com/sokolaidev/maf-extensions/issues/495)). A delete leaves its final component alone by design, so `confine_resolve_guest_delete_path` over this stat refuses an escape exactly as a write does. **What is left is not the check.** Nothing here implements a removal or answers the shared delete probes, and no branch of this stat reports an owner, so the reach rule has nothing to read and a removal could only run at the guest's own authority — which is a capability decision rather than a missing mechanism, and the absent pull surface ([#125](https://github.com/sokolaidev/maf-extensions/issues/125)) is not it either.

**The reach rule also withholds raised reclamation.** The file plane writes as root, but the engine cannot establish who owns the pre-existing ancestors of the call directory. An ancestor the guest could replace cannot license a recursive delete as root. The guest principal is diagnostic only and supplies no missing engine fact: `remove` and `reclaim` both refuse, with the reported principal named in the message. This answers [#839](https://github.com/sokolaidev/maf-extensions/issues/839) as a decision to withhold the mechanism until an engine-authenticated stat exists.

`run_code` raises for a different reason, and one this backend shares with [`acas`](acas.md) and [`docker`](docker.md): it is a `Sandbox` method, so it is implemented, but *which* runtime an image carries is a property of the image and this backend does not parse the reference it is handed. Declaring `RUN_CODE` would be a claim about someone else's artefact. A workload wanting an interpreter by name execs it and owns that assumption.

**`limits` is not declared at all**, and that silence is read the way a safety claim's silence is read — as the conservative default, `DEFAULT_SANDBOX_LIMITS`, rather than as "no ceiling". The router refuses a spec asking above it. Since this backend serves no out-door, the direction that matters is `files_in`. See [`../capabilities.md`](../capabilities.md).

## Cleanup by disposal

This backend withholds `Capability.RECLAIM` and `Capability.SNAPSHOT`, so the router resolves every call to `Cleanup.DISPOSE`, even when a workload claims `confined_to_guest_call_path`. `reclaim` raises `NotImplementedError` before running a guest command. Disposal uses the CLI's own label listing and narrows to the call's kind; its registry is only a fallback when listing fails. A later call pays a fresh create.

At acquire, a bounded `id -u` command runs as the image's user from `/`. Its diagnostic `guest_principal` is `root` for uid 0, `unprivileged` for a non-zero uid, and `unknown` for a failed or malformed answer. The log names it beside `cleanup=dispose`. Nothing is cached by a mutable image or container name. This is an announcement by the guest, not an engine observation, and it licenses no raised operation. The shared core principal protocol and capability refusal are separate work.

## Operator retention

`WslcSandboxBackend.reap(stopped_for, *, scope=None)` discovers resources from WSLC without router memory. It retains running workloads, expires stopped workloads using the inspected `State.FinishedAt`, and treats a never-started container's `Created` as its retention origin. A successful workload removal permits removal of its proxy and network. Without a workload, an orphan proxy uses its own creation age; a network alone uses the backend's `maf-sandbox.network-created-at` creation-request label. Legacy network-only leftovers lacking that timestamp are reported for manual cleanup. These infrastructure rules are maximum-age policies, not workload inactivity signals.

The operator pauses and drains acquisitions and restarts in the selected scopes and schedules independent, non-overlapping executions. Workloads are removed by immutable ID without force. Networks are revalidated and removed without disconnecting endpoints, but WSLC 2.9.3 only addresses them by name, so maintenance coordination is required to prevent replacement or restart races between inspection and removal. No scheduler, inventory store or new protocol member is added to the extension. The [package README](../../../packages/maf-sandbox-wslc/README.md#operator-retention) carries the executable example, failure behavior and supported metadata; [operations](../operations.md) owns the deployment boundary.

## One sandbox per call

A spec asking for `IsolationScope.CALL` is served here rather than refused. What entitles this
backend to declare it is that `SandboxKey.call_id` reaches all three things that decide which
container an acquire resolves to and which one a disposal removes: the **container name**, which
folds the call id as a `call:`-tagged part; the **registry entry**, filed under
`(scope, thread, agent, call, kind)`; and the **label** a disposal selects on,
`maf-sandbox.call`. Two acquires differing only in `call_id` are therefore two containers, and
ending one call leaves the sibling call of the same assistant message running — which is the
property `maf_sandbox.conformance.assert_call_scope_conformance` measures, and which the live
suite here answers against a real engine.

**A conversation-scoped key is byte-for-byte what it was.** The call id is appended to the name
only when it is non-empty, and the label is written only then, so a container created by a
release before this one is still found by name and still reached by the label selector. The tag
is what keeps the two optional name parts apart: untagged, a sandbox with an allowlist and no
call would share a name with a call whose id spelled that allowlist.

**The conversation's purge is still the backstop.** `dispose_scope` selects on scope and thread
alone, never on the call, so a per-call container whose own delete did not land is reached when
the conversation ends. That delete is reported and the key is not marked unclean: a call-scoped
key has no next acquire to refuse.

**What it costs is a cold start per call**, which is the trade the scope exists to offer rather
than a regression — the default is still `conversation`, and a host raises the floor with
`SandboxRouter(min_isolation_scope=...)` or a spec raises it for itself.

## Status

| Decision | State | Tracking |
|---|---|---|
| A workload can ask for a sandbox per tool call, and this backend serves one | shipped — `call_id` reaches the container name, the registry entry and the disposal's label filter, and a conversation-scoped key keeps the name and labels it already had. `assert_call_scope_conformance` is wired into the live suite, which no pull request runs | [#436](https://github.com/sokolaidev/maf-extensions/issues/436) (closed) by [#1139](https://github.com/sokolaidev/maf-extensions/pull/1139) (merged) |
| The backend, `EXEC` and `FILES_IN`, both egress modes, label purge | shipped | — |
| Operator retention for stopped workloads and orphan infrastructure | implemented — separate-process workload and partial-infrastructure cleanup verified on WSLC 2.9.4.0 | [#1010](https://github.com/sokolaidev/maf-extensions/issues/1010) (closed) by [#1015](https://github.com/sokolaidev/maf-extensions/pull/1015) (merged) |
| `egress_modes = {CLOSED}`, or `{CLOSED, ALLOWLIST}` with a proxy image; a mode outside the set is refused rather than degraded | shipped | [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| `observes_egress = True` with a proxy image: the proxy's own `ALLOW`/`DENY` lines are drained before every removal that would take them, and become `EgressObserved` keyed to the sandbox | shipped | [#948](https://github.com/sokolaidev/maf-extensions/issues/948) (closed) by [#963](https://github.com/sokolaidev/maf-extensions/pull/963) (merged) |
| `run_code` implemented as a refusal, `RUN_CODE` undeclared | shipped — the capability set is still `{EXEC, FILES_IN}` | [#531](https://github.com/sokolaidev/maf-extensions/pull/531) (merged) |
| wslc serves `FILES_OUT` | deferred — `cp` has no container-to-stdout form, and a symlink source writes a 0-byte file at exit 0 | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open; upstream [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310), both open |
| wslc serves `FILES_LIST` | deferred — would need an in-image shell, the dependency the split exists to avoid | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open |
| wslc declares `FILES_DELETE` | deferred — and no longer on the check. The engine settles every ancestor a removal would descend through, and the guest's remaining answer cannot carry a path through a link, so what is left is that nothing implements a removal or answers the shared delete probes, and that the reach rule has nothing to read here, so a removal would stay at the guest's authority | the confinement half was [#495](https://github.com/sokolaidev/maf-extensions/issues/495) (closed) by [#1135](https://github.com/sokolaidev/maf-extensions/pull/1135) (merged); the reach half is [#839](https://github.com/sokolaidev/maf-extensions/issues/839) (closed) by [#1036](https://github.com/sokolaidev/maf-extensions/pull/1036) (merged), and [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open) was never it. Which of them it is was [#743](https://github.com/sokolaidev/maf-extensions/issues/743) (closed) by [#848](https://github.com/sokolaidev/maf-extensions/pull/848) (merged) |
| No `limits` declaration; silence resolves to `DEFAULT_SANDBOX_LIMITS` | shipped — deliberate, and the conservative direction | — |
| Shared egress probes — this topology answers the same egress contract the other backends answer | shipped — `test_wslc_e2e.py` calls `assert_egress_conformance` for the allowed-host and denied-host outcomes, and retains its stricter `000` check and network teardown assertions. The suite runs on a developer's Windows host with a WSL that ships `wslc`; no CI runner is configured for it. Docker exercises the shared proxy topology after merge, daily, and on demand | [#402](https://github.com/sokolaidev/maf-extensions/issues/402) (closed) — shared probes by [#547](https://github.com/sokolaidev/maf-extensions/pull/547) (merged), backend wiring by [#548](https://github.com/sokolaidev/maf-extensions/pull/548) (merged); closure recorded by [#1101](https://github.com/sokolaidev/maf-extensions/pull/1101) (merged) |
| A guest-platform axis a kind can declare and match | shipped — this backend declares `{POSIX}`, a constant rather than a read, so a POSIX workload is served here and one asking for `WINDOWS` is refused at attach | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged); the declaration itself is [#588](https://github.com/sokolaidev/maf-extensions/issues/588) (closed) by [#946](https://github.com/sokolaidev/maf-extensions/pull/946) (merged) |
| Which principal a file-plane call acts as | implemented — the file plane copies with host authority and stamps the image user's ownership; `exec` runs as that user. Cleanup disposes the container because no engine fact licenses raising a recursive removal | [#695](https://github.com/sokolaidev/maf-extensions/issues/695) (closed) by [#706](https://github.com/sokolaidev/maf-extensions/pull/706) (merged); cleanup is tracked by [#982](https://github.com/sokolaidev/maf-extensions/issues/982) (closed) by [#1036](https://github.com/sokolaidev/maf-extensions/pull/1036) (merged) |
| Withhold `RECLAIM`; report the guest principal and clean every call by disposal | implemented — root and non-root disposal verified live; the shared core principal API remains separate | [#982](https://github.com/sokolaidev/maf-extensions/issues/982) (closed) by [#1036](https://github.com/sokolaidev/maf-extensions/pull/1036) (merged); supersedes the reclaim mechanism from [#477](https://github.com/sokolaidev/maf-extensions/issues/477) and [#711](https://github.com/sokolaidev/maf-extensions/issues/711) |
| Guest-owned inputs and missing directories | implemented — non-root guests can modify inputs and create outputs; existing directory metadata is preserved, and the guest-owned image exercises the reach probe | [#965](https://github.com/sokolaidev/maf-extensions/issues/965) (closed) by [#1053](https://github.com/sokolaidev/maf-extensions/pull/1053) (merged) |
