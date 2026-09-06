# What a sandbox reports about itself

> The seam a host records through: one observer it registers, one frozen event per thing that happened, and the key that joins them — plus the snapshot of what held, for the reader who has the conversation and not the trace. What the seam does not see is at the end, because a record with an unstated blind spot is worse than none.

## The problem

Every security-relevant thing this suite does, it already does. What it does not do is write any of it down in a form a deployment can query.

Ask which conversations reached `example.com` this week, how much they sent, which host tools ran and under whose authority, which files crossed and with what label — and there is no single place that answers. The facts exist as side effects in four places that share no key: this package's `logging` records, almost all of them at `warning`; the egress proxy's own container stdout, which dies with a container rebuilt on every acquire; the sandbox service's control plane, for a backend that enforces egress there; and a per-run ledger nothing emits. A **served** call that did nothing wrong leaves at most one `info` line, and no record at all.

MAF's own OpenTelemetry sees the whole thing as one `execute_tool` span plus a duration. That is the aggregate the boundary is designed to show the middleware — [`hosts.md`](hosts.md) says a host-tool call bypasses the middleware chain, so it bypasses the span too.

## The seam

A host subclasses `SandboxObserver` and registers it. Every method does nothing by default, so a host overrides only what it wants:

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
    def sandbox_acquired(self, event: SandboxAcquired) -> None:
        emit("sandbox.acquire", thread=event.key.thread_id, egress=str(event.spec.egress))

    def host_tool_called(self, event: HostToolCalled) -> None:
        emit("sandbox.host_tool_call", tool=event.tool, sink=event.sink, how=event.outcome)


def emit(name: str, **attributes: object) -> None:
    """Wherever this host's records go — a queue, an exporter, a SIEM."""


records = Records()
# One recorder, both registration points: the router would record no host-tool call, and the
# registry no acquire. `min_isolation` is lowered only because the in-process fake declares
# `Isolation.NONE`, which the default `microvm` floor refuses at construction — a deployment
# wiring a real backend leaves the floor alone.
router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE, observer=records)
registry = HostToolRegistry(observer=records)
```

**It is a class to inherit from rather than a `Protocol`.** This seam gains events as the suite learns to see more, and a structural implementer would stop satisfying a protocol the moment one arrived. Inheriting means a new event is a new no-op a host already has. Both registration points refuse anything that is not a `SandboxObserver`, and refuse an `async def` override — nothing awaits an observer, so a coroutine one would lose every event it saw.

**There are two registration points, because there are two host-policy objects.** `SandboxRouter(observer=…)` owns the sandbox lifecycle and is what `sandboxed_tool` reads for a call's own events. `HostToolRegistry(observer=…)` owns what a guest may call back into — the same split as every other host-tool policy, which lives on the registry because it is a statement about what the *host* will execute. A host may wire either alone. `collect_outputs` is neither: it is a function a kind calls per collection, so it takes `observer=` and `key=` as arguments, and a kind passes `session.observer` beside the key it already took for its acquire. That stays a call-site pattern rather than a `SandboxToolSession.collect_outputs` filling both in, because a kind collecting under caps of its own passes a spec the session does not hold — codeact charges its manifest against the budget before the artifacts are read — so a wrapper would take back as arguments most of what it saved, and leave two ways to do one thing.

## What is recorded

| Event | Emitted at | What it answers |
|---|---|---|
| `SandboxAcquired` | `SandboxRouter.acquire`, served or refused | Which key ran under which spec, on which backend, at which isolation rung and scope — or the class name of the refusal that stopped it |
| `SandboxDisposed` | Every disposal of a **key**, once per backend asked | What became of the delete — `gone`, `may_remain` or `unknown` — and the `DisposalCode` and detail behind it. Three values rather than a flag because `dispose` returns `None` both for a verified delete and for one a backend cannot check, and an interrupted disposal never answered at all |
| `ScopeDisposed` | Every purge of a **conversation**, once per backend asked | How many sandboxes that backend removed for `(scope, thread_id)`, under the same three-value `outcome` — so the routine cleanup a thread deletion runs is recorded beside the per-key delete rather than only inferable from its absence |
| `EgressObserved` | A backend implementing `ObservesEgress`, before each removal that would take its enforcer's record with it — **except one it cannot attribute**, since a scope purge is addressed by a conversation and this event needs a key | Every `CONNECT` the guest **attempted** and how the enforcer answered it: one `EgressDecision` each — `ALLOW`, `DENY`, `DENY-NONGLOBAL` or `UNREACHABLE`, with the host and port it asked for. Only `ALLOW` opened a tunnel, so a reader counting what was *reached* filters on the verb. The read is bounded in lines and in bytes, so `truncated` says the window **may** be short of it rather than that a decision was certainly dropped, and `unreadable` names a window the backend could not account for |
| `HostToolCalled` | `HostToolRun.call` | Which tool a guest program called, under which declaration, how it ended, and how many bytes came back |
| `StoreFileRead` | `SandboxToolSession.read_file` | Which file a call read out of the host's store, the integrity label the read folded, and whether text actually crossed — `read`, `absent` or `refused`, since an empty file and a missing one are otherwise the same record |
| `OutputsCollected` | `collect_outputs` | What a spec declared, what a sink took, under which `TransferLimits`, and — for a `per_call` sink — the folder they landed in |
| `ToolCallEnded` | `sandboxed_tool`'s wrapper | One sandboxed tool call: its own id, every key it touched, served or refused, what it cost, what the **body** raised, and what it left unclean |

Every event is a frozen dataclass in this package's own vocabulary — a `SandboxKey`, a `SandboxSpec`, a `SourceIntegrity`. Nothing here imports a telemetry library; core's protocol modules are standard library only, and a package that turns these into spans, log records and counters sits above this seam rather than inside it.

`SandboxAcquired` carries the whole `SandboxSpec` rather than a projection of it, deliberately: which fields a record needs is the recorder's question, and a field added to the spec later reaches an existing recorder with no change here. It is also the "what held" half, and the section below is where that answer goes when a trace is not where somebody will look for it.

**The key is what joins them, and the eight do not all carry one the same way.** Four shapes, because four things are true:

- `SandboxAcquired`, `SandboxDisposed` and `EgressObserved` are *addressed* by a key, so `key: SandboxKey` and it is always there.
- `HostToolCalled`, `StoreFileRead` and `OutputsCollected` type it `SandboxKey | None`, and each is `None` for its own reason: a `HostToolRun` the transport built without one, a store read whose key could not be *derived* — no conversation bound to the request context, or a call-scoped workload asked outside a call — and a collection a kind did not pass one for. Note the middle case is about the context, not about acquisition: a session that never acquired still keys its reads from the scope and thread it was built with.
- `ScopeDisposed` carries no key at all, and this is the one place that is not a gap: a backend answers a purge with a *count* rather than with the sandboxes it removed, so it holds `scope` and `thread_id` and joins to the rest on the conversation. Reading it as a per-key record — one delete, or one per sandbox — is the mistake to avoid; it is one record per **backend asked**, and `disposed` is that backend's own number.
- `ToolCallEnded` carries `keys: tuple[SandboxKey, ...]` — every key the call **touched**, in order, since one call may reach two sandboxes, and an empty tuple rather than `None` for a call that touched none. Touched rather than acquired, because both of the other cases are ordinary: a refused acquire is named so its own `SandboxAcquired` has a call to join to, and so is a key the call only read the store under — `execute_code` reads its listed files before it acquires and returns early when one is refused. A recorder wanting only what was served reads the acquire records, where the refusal is stated.

So a host joining records treats the middle three as joinable when the key is present, joins `ToolCallEnded` through its tuple rather than looking for a `key` field it does not have, and joins `ScopeDisposed` on the conversation. `collect_outputs` has no key of its own, so a kind that wants its collections joined passes one; the `call_id` it already stamps on each artifact is recorded beside it, which is what reaches the folder a `per_call` sink landed them in. `HostToolRun(key=…)` is the same for a transport: without it a host-tool record says which run called and nothing about whose conversation.

**`call` is the second join column, and it is the one the key cannot be.** A key names a sandbox and a conversation; at the default `conversation` scope it names no call, so two calls in flight on one thread are one key and their records interleave. Every event carries `call` — the id of the tool call it came from — and `ToolCallEnded` is the one event where it is never `None`, because that record is where a call's other events join. `EgressObserved` is the other end of that: its `call` is **always** `None`, because a drain covers a window between removals and the decisions in it span whatever calls happened meanwhile — naming the call that collected them would file one call's traffic under another's. On the remaining six it is absent for what genuinely happened outside a call: an acquire a direct consumer of the router asked for, a disposal from a framework reclaim, a scope purge — which a thread deletion and a closing `scope` block both run from outside any call — a collection a kind ran outside a tool body — and anything a task the body left running does *after* the call, since a child task starts from a copy of the context and the call's record is the only part of it the two share.

It is the same id the call's own guest path is named by, and — at `IsolationScope.CALL` — its key's `call_id` too, so a recorder holds one string for a call rather than two, and can find the folder that call's files are under. Note that `OutputsCollected` carries both `call` and `call_id`, and they answer different questions: `call_id` is what a kind asked the sink to stamp on each artifact, read from the argument, while `call` is which call collected, read from the seam. A kind passing its own call's id spells them the same; a kind passing none, or a meaning of its own, still gets a record that joins.

`HostToolCalled` gets its `call` where the `HostToolRun` was built rather than per call, because a guest's callback is served on a task of the transport's own whose context is a copy rather than the body's. A transport therefore builds its run **inside** the tool call it supervises — the shipped `execute_code` one does — and a run built elsewhere records no call.

**Every way out of an instrumented site is recorded, not only the successful one.** An acquire that was refused, a host-tool call that a cap exhausted, a collection that a cap refused part-way, a tool call taken by a cancel — each of those is the record an operator goes looking for, and none is emitted beside a `return`. Where a site can wrap its whole body the record comes from a `finally`; where it cannot, the cancellation carries its own catch, because `CancelledError` is not an `Exception` and an `except Exception` that looks exhaustive would drop exactly the disposal a timeout took. **How a refusal is recorded differs by event, and a recorder has to know which it is holding.** `SandboxAcquired.refusal`, `OutputsCollected.refusal` and `ToolCallEnded.failure` are the exception's **class name** and never its message, because a message is what carries a backend's endpoint or an SDK's response body and these records are handed over whole. `HostToolCalled.refusal` is the other kind: it is the sanitized sentence the *guest* was answered with, which is safe for a transcript and so safe for a record — but it is not purely host vocabulary, since the two refusals that fire before a name resolves quote a bounded copy of what the guest asked for. Treat that one field as guest-influenced when deciding what may leave the process.

## What held, written where the conversation lives

The records above answer *what happened*. The adjacent question — **what was live for this call** — has a second destination, because the two survive different things: a span survives with the trace and whatever sampled it, and somebody reading a conversation back a month later has the transcript and no trace at all.

`EffectiveState` is one served acquire as a value rather than an event: the backend that answered, the isolation rung it declared, the scope the host and the spec resolved to, the egress mode and its allowlist, the capabilities the workload required beside the ones the backend declared, the image, the guest working directory, the declared outputs, the transfer caps per direction — and **every tool the sealed host-tool registry was carrying**. That last one is the half no event answered before: a spec's `host_tools` carries the registry's *folds* — its result integrity, its identities, its ceilings — because those are what the router matches on, so which tools were actually callable existed only as the code that registered them.

`effective_state_middleware()` is what writes it. Add it to the agent's middleware and every served call leaves its snapshot in `session.state["maf_sandbox.served"]`, keyed by the tool the model called:

```python
from agent_framework import Agent

from maf_sandbox.maf import effective_state_middleware

agent = Agent(..., middleware=[effective_state_middleware()])
```

**The served answer, not the ask.** A refused call writes nothing: it already has an exception, a log line and a `SandboxAcquired` carrying the refusal's class name, and there is no posture to describe. A call that acquired nothing writes nothing either, which leaves the tool's previous entry standing — *the last posture this tool was served under* stays true across a call that got no sandbox.

**One entry per tool, overwritten each call.** Session state is persisted and lives as long as the conversation, so a per-call history here would grow without bound — and per-call is what the events above already answer. What survives here is the current answer, which is the question this destination is good at.

**One identifier, and it is the one every record already carries.** The snapshot names the `call` it was served to, so it joins to that call's acquire, its host-tool calls and its collections — and to the folder a `per_call` sink landed its artifacts in. The framework generates it and it names nobody on its own, which is what separates it from the two things below.

**Posture, never payload — and the spec's `labels` are not in it.** Every field is host configuration, a backend's own declaration, or that id: no artifact name, no file name, no code, no result text. Labels are the one that is a decision rather than an obvious omission. They are host deployment vocabulary — a tenant, a cost centre, a subscription — and this record is persisted beside a transcript a deployment may classify differently, so the seam above is where a recorder that wants them reads them, with the host choosing the destination. The `SandboxKey` is out for the same reason and one more: the session **is** that conversation, so a scope and a thread id in its own state answer nothing they do not already — which is not true of the call id, and is why that one is in.

**And OpenTelemetry sees the same acquire from the other side.** `maf-sandbox-otel` turns each `SandboxAcquired` into a span, a log record and a counter, which is what makes the posture queryable *across* conversations rather than only inside one — the kind, the backend, the image, the isolation rung and scope, the egress mode with its allowlist and count, and both sides of the capability match. Both destinations are built from the one event, so they cannot disagree about what held; what differs is only what each survives. The sealed registry's names are on this record and not yet on that span.

**Nothing is built when nobody is listening.** The acquire path checks for an observer and for an open collection before it composes either, so a host that wired neither pays for neither — the same promise the seam above makes, and pinned the same way.

## An observer cannot fail a call, and cannot slow one down safely

An observer runs wherever the call it records is served — on the event-loop task for a tool body that awaits, and for a body that does not, on the worker thread the framework runs it on. Its failure is contained — logged as a warning, and the call runs on — with the same narrow catch the host-tools bracket already uses: an `Exception`, a `CancelledError`, a `GeneratorExit` from an observer's own generator are contained, while `SystemExit` and `KeyboardInterrupt` are the host's control flow and escape. An observer's return value is never read, so none of them can change what a call answers.

What is *not* contained is time. Blocking in an observer blocks the call, so an observer's job is to hand the event on — to a queue, or to an exporter batching on a thread of its own — and never to do the I/O itself. **The handoff has to be thread-safe.** Two calls in flight reach one observer at once, and a synchronous tool body's `ToolCallEnded` arrives from a worker thread rather than from the loop, so it wants a `queue.Queue` or `loop.call_soon_threadsafe`; an `asyncio.Queue` is neither safe to fill from another thread nor woken by it.

**A host that registers nothing pays nothing.** No observer means no event is built: each site checks first and takes the uninstrumented path, which is what the tests pin rather than what the code merely implies.

## What reaches a wire is the recorder's decision

Nothing here redacts, and that is a position rather than an omission. An observer runs in the host's own process, sees no more than the host's log already could, and is the only party that knows what its exporter's retention and audience are.

What a recorder should know is which of these values a *guest* chose. Hostnames come from the spec and backend names from configuration, so both are the host's. Artifact names are model-chosen — the exfiltration audit measures a 255-byte-per-file channel through them ([`research/exfiltration-audit.md`](research/exfiltration-audit.md)) — and a `HostToolCalled.refusal` is a sanitized sentence safe for a transcript, but the two refusals that fire before a name resolves quote a bounded copy of what the guest asked for. The transcript rule applies to an exporter too: nothing a guest chose should leave the host unless the host asked for it.

## What this seam does not see

**Egress, on a backend that does not enforce it itself.** `SandboxAcquired` records the mode and the allowlist a sandbox was **served** under — what its spec asked for — and `EgressObserved` records what its enforcer then decided. The second only exists where the backend runs the enforcer: `docker` and `wslc` own the proxy, so they read its `ALLOW`, `DENY`, `DENY-NONGLOBAL` and `UNREACHABLE` lines back before the container holding them goes. `acas` enforces in the service, and the one signal the documentation names — the `x-deny-reason` header — is visible only inside the guest, so it reports nothing.

That difference is why silence is not a reading. A key with no `EgressObserved` record was either watched and quiet or never watched at all, and the two are the opposite conclusions; `BackendDeclarations.observes_egress` rides on the acquire record so a reader can tell which. `False` is the certain half — nothing was watched. `True` is narrower than it looks: a backend reports the windows it can *attribute*, and one keyed on a sandbox can only attribute what it acquired, so a container another replica created or one predating this process reports nothing either. [`network.md`](network.md) carries why a host two conversations share is worth seeing at all.

**How many refused keys a purge reopened.** A purge every backend answered cleanly clears the conversation's entries from the unclean ledger, so keys that `acquire` was refusing become servable again. *Whether* that happened is on the records already — it is the same condition they carry, every one of the purge's `ScopeDisposed` events reading `gone` — but the number of keys is not, because the clear happens once for the whole purge while the event is per backend, and a router with no backend registered clears the ledger while emitting nothing at all. The resource facts a purge is audited for — what went, what may still be there, and on which backend — are recorded in full.

**An exporter.** Turning these events into OpenTelemetry spans, log records and counters — under the app's providers or a security pipeline's own — is a package above this seam, so that a host wanting the events and not the dependency pays for neither. That package is `maf-sandbox-otel`, and core still cannot host it: its protocol modules are standard library only.

## Status

| Decision | State | Tracking |
|---|---|---|
| A `SandboxObserver` a host registers, and frozen events in core's own vocabulary | shipped — eight events, registered on `SandboxRouter` and `HostToolRegistry`, passed to `collect_outputs`, and reported by a backend that watches its own egress; refusals, cancellations and served calls alike | [#906](https://github.com/sokolaidev/maf-extensions/pull/906) (merged), under [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| An observer's failure never reaches the call, and no observer builds no event | shipped — `Exception`, `CancelledError` and `GeneratorExit` contained and logged; `SystemExit` and `KeyboardInterrupt` escape | [#906](https://github.com/sokolaidev/maf-extensions/pull/906) (merged), under [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| The served configuration is recorded per acquire, as the whole spec — the sealed host-tool registry's names with it | shipped — `SandboxAcquired.spec`, beside the resolved isolation scope, which the spec alone does not answer; `HostToolAggregate.names` is what makes *which tools were callable* answerable from the spec that served rather than only from the code that registered them | [#380](https://github.com/sokolaidev/maf-extensions/issues/380) (closed) by [#953](https://github.com/sokolaidev/maf-extensions/pull/953) (merged) |
| What held reaches the conversation, not only the trace | shipped — `EffectiveState`, one JSON-serializable snapshot per distinct posture a call was served, written into `AgentSession.state` by `effective_state_middleware()`: one entry per tool, overwritten each call, and posture only — no model-chosen text, no `SandboxSpec.labels`, no `SandboxKey` | [#380](https://github.com/sokolaidev/maf-extensions/issues/380) (closed) by [#953](https://github.com/sokolaidev/maf-extensions/pull/953) (merged) |
| An OpenTelemetry recorder, under the app's providers or a security pipeline's own | partial — `maf-sandbox-otel` implements it: it registers on the router and the host-tool registry and turns each event into a log record, a span — a zero-duration point span for the one event with no duration of its own, a `StoreFileRead` — and a metric where it counts, under the app's providers or a pipeline's own. Core still cannot host it, since its protocol modules are standard library only. What is not covered is what nothing emits — the kind wiring, in the row below. Two things core does emit and this does not render yet, each waiting on a floor that admits the release adding it: the sealed registry's names, in the first row above, and the `call` id, in the row below | [#907](https://github.com/sokolaidev/maf-extensions/pull/907) (merged), under [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| A record says which **call** it came from, not only which sandbox and conversation | shipped — every event carries `call`, never `None` on `ToolCallEnded` and absent only on what happened outside a call. It is the id the guest path and a call-scoped key are already named by, so a recorder holds one string for a call rather than two. `maf-sandbox-otel` does not export it yet: reading a field this release adds needs a floor on this release, so that is its own change | [#922](https://github.com/sokolaidev/maf-extensions/issues/922) (closed) by [#952](https://github.com/sokolaidev/maf-extensions/pull/952) (merged) |
| A scope purge is recorded, so thread deletion is not the one disposal nobody can see | shipped — `ScopeDisposed`, one per backend asked, keyed on `(scope, thread_id)` because a backend answers a purge with a count and not with the keys it removed; a purge a cancel took is recorded too. What is not on it is how many refused keys the purge reopened | [#917](https://github.com/sokolaidev/maf-extensions/issues/917) (closed) by [#947](https://github.com/sokolaidev/maf-extensions/pull/947) (merged) |
| The proxy's `ALLOW`/`DENY` lines reach a record, keyed to the sandbox that caused them | shipped — `EgressObserved`, drained by `docker` and `wslc` before every removal that would take the lines with it, which is once per acquire rather than once per sandbox. Bounded in lines and in bytes, and `truncated` says the window may be short of that rather than that a decision was certainly dropped. A service-enforced backend still reports nothing, and `observes_egress` on the acquire record is what stops that reading as a quiet sandbox | [#948](https://github.com/sokolaidev/maf-extensions/issues/948) (closed) by [#963](https://github.com/sokolaidev/maf-extensions/pull/963) (merged) |
| A host-tool record carries the key of the sandbox its run belongs to | shipped — `HostToolRun(key=…)` accepts one and every record carries it, and `execute_code` passes the key it took for its own acquire, so a host-tool call joins to the conversation that made it rather than to its `run_id` alone | [#949](https://github.com/sokolaidev/maf-extensions/issues/949) (closed) by [#959](https://github.com/sokolaidev/maf-extensions/pull/959) (merged) |
| A collection's record is joined to the conversation that produced it | shipped — `collect_outputs(observer=…, key=…)` takes both and codeact passes both, `session.observer` beside that same key, so every collection the one kind that lands anything runs emits an `OutputsCollected` — refused part-way included | [#949](https://github.com/sokolaidev/maf-extensions/issues/949) (closed) by [#959](https://github.com/sokolaidev/maf-extensions/pull/959) (merged) |
