# What a sandbox reports about itself

> The seam a host records through: one observer it registers, one frozen event per thing that happened, and the key that joins them. What the seam does not see is at the end, because a record with an unstated blind spot is worse than none.

## The problem

Every security-relevant thing this suite does, it already does. What it does not do is write any of it down in a form a deployment can query.

Ask which conversations reached `example.com` this week, how much they sent, which host tools ran and under whose authority, which files crossed and with what label — and there is no single place that answers. The facts exist as side effects in four places that share no key: this package's `logging` records, almost all of them at `warning`; the egress proxy's own container stdout, which the backend reads once at acquire and never again; the sandbox service's control plane, for a backend that enforces egress there; and a per-run ledger nothing emits. A **served** call that did nothing wrong leaves at most one `info` line, and no record at all.

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

**There are two registration points, because there are two host-policy objects.** `SandboxRouter(observer=…)` owns the sandbox lifecycle and is what `sandboxed_tool` reads for a call's own events. `HostToolRegistry(observer=…)` owns what a guest may call back into — the same split as every other host-tool policy, which lives on the registry because it is a statement about what the *host* will execute. A host may wire either alone. `collect_outputs` is neither: it is a function a kind calls per collection, so it takes `observer=` and `key=` as arguments, and a kind passes `session.observer`.

## What is recorded

| Event | Emitted at | What it answers |
|---|---|---|
| `SandboxAcquired` | `SandboxRouter.acquire`, served or refused | Which key ran under which spec, on which backend, at which isolation rung and scope — or the class name of the refusal that stopped it |
| `SandboxDisposed` | Every disposal, once per backend asked | What became of the delete — `gone`, `may_remain` or `unknown` — and the `DisposalCode` and detail behind it. Three values rather than a flag because `dispose` returns `None` both for a verified delete and for one a backend cannot check, and an interrupted disposal never answered at all |
| `HostToolCalled` | `HostToolRun.call` | Which tool a guest program called, under which declaration, how it ended, and how many bytes came back |
| `StoreFileRead` | `SandboxToolSession.read_file` | Which file a call read out of the host's store, the integrity label the read folded, and whether text actually crossed — `read`, `absent` or `refused`, since an empty file and a missing one are otherwise the same record |
| `OutputsCollected` | `collect_outputs` | What a spec declared, what a sink took, under which `TransferLimits`, and — for a `per_call` sink — the folder they landed in |
| `ToolCallEnded` | `sandboxed_tool`'s wrapper | One sandboxed tool call: every key it touched, served or refused, what it cost, what the **body** raised, and what it left unclean |

Every event is a frozen dataclass in this package's own vocabulary — a `SandboxKey`, a `SandboxSpec`, a `SourceIntegrity`. Nothing here imports a telemetry library; core's protocol modules are standard library only, and a package that turns these into spans, log records and counters sits above this seam rather than inside it.

`SandboxAcquired` carries the whole `SandboxSpec` rather than a projection of it, deliberately: which fields a record needs is the recorder's question, and a field added to the spec later reaches an existing recorder with no change here. It is also the "what held" half that [#380](https://github.com/sokolaidev/maf-extensions/issues/380) asks for.

**The key is what joins them, and the six do not all carry one the same way.** Three shapes, because three things are true:

- `SandboxAcquired` and `SandboxDisposed` are *addressed* by a key, so `key: SandboxKey` and it is always there.
- `HostToolCalled`, `StoreFileRead` and `OutputsCollected` type it `SandboxKey | None`, and each is `None` for its own reason: a `HostToolRun` the transport built without one, a store read whose key could not be *derived* — no conversation bound to the request context, or a call-scoped workload asked outside a call — and a collection a kind did not pass one for. Note the middle case is about the context, not about acquisition: a session that never acquired still keys its reads from the scope and thread it was built with.
- `ToolCallEnded` carries `keys: tuple[SandboxKey, ...]` — every key the call **touched**, in order, since one call may reach two sandboxes, and an empty tuple rather than `None` for a call that touched none. Touched rather than acquired, because both of the other cases are ordinary: a refused acquire is named so its own `SandboxAcquired` has a call to join to, and so is a key the call only read the store under — `execute_code` reads its listed files before it acquires and returns early when one is refused. A recorder wanting only what was served reads the acquire records, where the refusal is stated.

So a host joining records treats the middle three as joinable when the key is present, and joins the last through its tuple rather than looking for a `key` field it does not have. `collect_outputs` has no key of its own, so a kind that wants its collections joined passes one; the `call_id` it already stamps on each artifact is recorded beside it, which is what reaches the folder a `per_call` sink landed them in. `HostToolRun(key=…)` is the same for a transport: without it a host-tool record says which run called and nothing about whose conversation.

**Every way out of an instrumented site is recorded, not only the successful one.** An acquire that was refused, a host-tool call that a cap exhausted, a collection that a cap refused part-way, a tool call taken by a cancel — each of those is the record an operator goes looking for, and none is emitted beside a `return`. Where a site can wrap its whole body the record comes from a `finally`; where it cannot, the cancellation carries its own catch, because `CancelledError` is not an `Exception` and an `except Exception` that looks exhaustive would drop exactly the disposal a timeout took. **How a refusal is recorded differs by event, and a recorder has to know which it is holding.** `SandboxAcquired.refusal`, `OutputsCollected.refusal` and `ToolCallEnded.failure` are the exception's **class name** and never its message, because a message is what carries a backend's endpoint or an SDK's response body and these records are handed over whole. `HostToolCalled.refusal` is the other kind: it is the sanitized sentence the *guest* was answered with, which is safe for a transcript and so safe for a record — but it is not purely host vocabulary, since the two refusals that fire before a name resolves quote a bounded copy of what the guest asked for. Treat that one field as guest-influenced when deciding what may leave the process.

## An observer cannot fail a call, and cannot slow one down safely

An observer runs wherever the call it records is served — on the event-loop task for a tool body that awaits, and for a body that does not, on the worker thread the framework runs it on. Its failure is contained — logged as a warning, and the call runs on — with the same narrow catch the host-tools bracket already uses: an `Exception`, a `CancelledError`, a `GeneratorExit` from an observer's own generator are contained, while `SystemExit` and `KeyboardInterrupt` are the host's control flow and escape. An observer's return value is never read, so none of them can change what a call answers.

What is *not* contained is time. Blocking in an observer blocks the call, so an observer's job is to hand the event on — to a queue, or to an exporter batching on a thread of its own — and never to do the I/O itself. **The handoff has to be thread-safe.** Two calls in flight reach one observer at once, and a synchronous tool body's `ToolCallEnded` arrives from a worker thread rather than from the loop, so it wants a `queue.Queue` or `loop.call_soon_threadsafe`; an `asyncio.Queue` is neither safe to fill from another thread nor woken by it.

**A host that registers nothing pays nothing.** No observer means no event is built: each site checks first and takes the uninstrumented path, which is what the tests pin rather than what the code merely implies.

## What reaches a wire is the recorder's decision

Nothing here redacts, and that is a position rather than an omission. An observer runs in the host's own process, sees no more than the host's log already could, and is the only party that knows what its exporter's retention and audience are.

What a recorder should know is which of these values a *guest* chose. Hostnames come from the spec and backend names from configuration, so both are the host's. Artifact names are model-chosen — the exfiltration audit measures a 255-byte-per-file channel through them ([`research/exfiltration-audit.md`](research/exfiltration-audit.md)) — and a `HostToolCalled.refusal` is a sanitized sentence safe for a transcript, but the two refusals that fire before a name resolves quote a bounded copy of what the guest asked for. The transcript rule applies to an exporter too: nothing a guest chose should leave the host unless the host asked for it.

## What this seam does not see

**The egress proxy's own decisions.** The docker and wslc proxy prints `ALLOW`, `DENY`, `DENY-NONGLOBAL` and `UNREACHABLE` inside its own container; the backend reads that stream once, at acquire, to wait for the `listening` marker, and the proxy is recreated per acquire so the lines die with the container. They carry no sandbox key either. A backend that enforces egress in the service — `acas` — exposes the deny reason only inside the guest. So `SandboxAcquired` records the mode and the allowlist a sandbox was **served** under, which is what a spec asked for and not what a guest then reached. Routing those lines to a record is [#904](https://github.com/sokolaidev/maf-extensions/issues/904)'s remaining half, and [`network.md`](network.md) carries why a host two conversations share is worth seeing at all.

**Which call a record came from, when two run at once.** The key joins a record to a sandbox and a conversation, and at the default `conversation` scope it carries no `call_id` — so two calls in flight on one thread emit interleaved records under one key, and nothing separates them. A recorder inside a traced host has the framework's own span context to correlate by, and one reading records alone does not. That wants a call correlation id beside the key, which is [#922](https://github.com/sokolaidev/maf-extensions/issues/922).

**A scope purge.** `dispose_scope` — what a thread deletion runs, and what `router.scope(…)` runs when its block ends — asks every backend and emits nothing, so of the two ways a sandbox goes away only the per-key one is recorded. `SandboxDisposed` cannot carry it: a backend answers a purge with a count rather than the keys it removed, so there is no key to put in the event and no honest way to invent one. It wants a seventh event keyed on `(scope, thread_id)`, which is [#917](https://github.com/sokolaidev/maf-extensions/issues/917).

**An exporter.** Turning these events into OpenTelemetry spans, log records and counters — under the app's providers or a security pipeline's own — is a package above this seam, so that a host wanting the events and not the dependency pays for neither.

## Status

| Decision | State | Tracking |
|---|---|---|
| A `SandboxObserver` a host registers, and frozen events in core's own vocabulary | shipped — six events, registered on `SandboxRouter` and `HostToolRegistry`, passed to `collect_outputs`; refusals, cancellations and served calls alike | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| An observer's failure never reaches the call, and no observer builds no event | shipped — `Exception`, `CancelledError` and `GeneratorExit` contained and logged; `SystemExit` and `KeyboardInterrupt` escape | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| The served configuration is recorded per acquire, as the whole spec | shipped — `SandboxAcquired.spec`, beside the resolved isolation scope, which the spec alone does not answer | [#380](https://github.com/sokolaidev/maf-extensions/issues/380) (open) |
| An OpenTelemetry recorder, under the app's providers or a security pipeline's own | open — no package implements this observer yet; core cannot, since its protocol modules are standard library only | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| A record says which **call** it came from, not only which sandbox and conversation | open — the key carries no `call_id` at the default scope, so concurrent calls in one conversation are indistinguishable in the records alone. A traced host correlates by the framework's span context meanwhile | [#922](https://github.com/sokolaidev/maf-extensions/issues/922) (open) |
| A scope purge is recorded, so thread deletion is not the one disposal nobody can see | open — `dispose_scope` emits nothing, and `SandboxDisposed` cannot carry it: a backend answers a purge with a count, not the keys it removed. It wants a seventh event keyed on `(scope, thread_id)` | [#917](https://github.com/sokolaidev/maf-extensions/issues/917) (open) |
| The proxy's `ALLOW`/`DENY` lines reach a record, keyed to the sandbox that caused them | open — the lines die with the proxy container, and for a service-enforced backend they never leave the guest | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| A host-tool record carries the key of the sandbox its run belongs to | partial — `HostToolRun(key=…)` accepts one and every record carries it; the shipped `execute_code` transport does not pass one yet | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
| A collection's record is joined to the conversation that produced it | partial — `collect_outputs(observer=…, key=…)` takes both, and `SandboxToolSession.observer` is where a kind reads the first; no shipped kind passes either yet, so the two kinds are their own change | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (open) |
