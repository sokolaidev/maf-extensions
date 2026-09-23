# Observability

The host can observe sandbox activity through `SandboxObserver`. Events describe the configuration served, work attempted and cleanup outcomes. They use core types and require no telemetry SDK.

The observer records activity. It does not authorize tools, change content labels or prove that guest output is trustworthy. Those rules belong to [information flow](information-flow.md).

![Source tools declare result labels; returned content items carry their own labels to the model. Host policy checks later calls to destination tools against those tools' accepted labels. Separately, the sandbox's router, tool wrapper, host-tool registry, output collector and observing backends emit events. A host observer receives them. The OpenTelemetry observer selects and redacts attributes before sending logs, spans and metrics to host-configured providers and exporters. Observation records selected facts and does not enforce information-flow policy.](assets/observability-channels.svg)

## Register an observer

Subclass `SandboxObserver` and override the callbacks you need. Other callbacks are no-ops. Register it on both `SandboxRouter` and `HostToolRegistry`; pass it to `collect_outputs(observer=..., key=...)` for output records.

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
    """Send these fields to the host's chosen recorder."""


records = Records()
# The fake backend requires Isolation.NONE. Keep the deployment's real isolation floor.
router = SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE, observer=records)
registry = HostToolRegistry(observer=records)
```

Callbacks are synchronous and can run on an event loop or worker thread. An `async def` callback is refused at registration. Keep callbacks short and make shared state thread-safe. Use a thread-safe queue or `loop.call_soon_threadsafe` to hand work to an exporter.

Ordinary exceptions, `CancelledError` and `GeneratorExit` are logged and contained. `SystemExit` and `KeyboardInterrupt` escape, including inside exception groups. An observer's return value does not change the tool result.

When no observer is registered, no observer event is built. The effective-state collector can still record configuration independently.

## Events

The ten frozen event types describe one operation or observation each.

| Event | Records |
|---|---|
| `SandboxAcquired` | Served or refused acquisition, full spec, backend declarations, resolved isolation scope and timing |
| `SandboxDisposed` | One backend's key disposal and physical outcome |
| `ScopeDisposed` | One backend's conversation purge, count and physical outcome |
| `EgressObserved` | A bounded proxy window of network decisions, with unreadable and truncated indicators |
| `HostToolCalled` | A guest-to-host tool call, tool declaration, sizes, timing and refusal |
| `StoreFileRead` | Host-store read outcome and the integrity label of accepted text |
| `OutputsCollected` | Declared and landed files, byte counts, transfer limits and collection errors |
| `ToolCallEnded` | Call duration, failure class, touched keys, cleanup state and input-integrity summary |
| `ProcessesObserved` | A bounded process snapshot at a run boundary |
| `ProcessCleanup` | A cleanup action, target identity, reach and outcome |

Disposal outcomes distinguish `gone`, `may_remain` and `unknown`. A count or successful API response alone does not prove physical removal. Purge records give backend counts, not an inventory of removed keys or reopened refusals.

Core events are **not redacted**. They can contain the full spec and identifying or guest-chosen strings. A custom recorder must choose what to export. The [OpenTelemetry observer](../../packages/maf-sandbox-otel/README.md) omits sensitive strings by default.

## Join records to work

Use `event.call` to join activity from the same tool call. It is always present on `ToolCallEnded`. Events outside a call can have no call ID. `EgressObserved.call` is always `None`, since a proxy window can cover several calls.

| Identity | Events |
|---|---|
| Required sandbox key | Acquire, key disposal and egress |
| Optional sandbox key | Host-tool calls, process observations, process cleanup, store reads and output collection |
| `(scope, thread_id)` | Conversation purge |
| Tuple of touched keys | Tool-call end, including refused acquisitions and store-only work |

A conversation key can be shared by concurrent calls. Its `call_id` is empty, so it cannot identify the call that performed an operation. `OutputsCollected.call_id` names the artifact collection; `event.call` identifies the actual tool call.

`ToolCallEnded.fed` summarizes accepted host-store reads: their count and weakest integrity label. Absent and refused reads contribute nothing. No accepted reads gives `None`, not `trusted`. The summary describes inputs, not result integrity, and can include reads from sessions without their own observer.

## OpenTelemetry

`maf-sandbox-otel` converts each event into a log and span, plus metrics where useful. Store reads use instant spans. Process snapshots add per-process logs. The application supplies SDK providers, exporters and retention.

![Within an application span, acquisition, file, host-tool and call-end records are sibling spans. They are emitted after their operations, and the call-end span uses the full reported call duration. The call ID joins them. Egress records use a fresh trace context with no tool-call ID because the observation window belongs to the sandbox, not one call.](assets/otel-trace-shape.svg)

The recorder selects fields from the core event. It does not export every spec field, every host-tool declaration field or `ToolCallEnded.fed`. Logs, spans and metrics use independent providers. Logs can survive trace sampling, but delivery still depends on the configured log pipeline.

See the [package guide](../../packages/maf-sandbox-otel/README.md) for wiring, signal names, redaction and correlation attributes.

## Served configuration in the session

`effective_state_middleware()` writes JSON-serializable `EffectiveState` records to `AgentSession.state["maf_sandbox.served"]`.

```python
from agent_framework import Agent

from maf_sandbox.maf import effective_state_middleware

agent = Agent(..., middleware=[effective_state_middleware()])
```

The state has one entry per tool. A served call replaces that tool's entry with its distinct served configurations. Refused calls and calls that acquire nothing keep the last served entry.

| Included | Omitted |
|---|---|
| Backend, isolation, scope and network policy | Raw `SandboxKey` and `SandboxSpec.labels` |
| Required and declared capabilities | Model code, prompts and result payloads |
| Image, work directory and execution contract | File contents and artifact bytes |
| Output declarations and transfer limits | CodeAct runtime instructions; only its profile digest is retained |
| Sealed host-tool names and tool call ID | Model-chosen tool arguments |

`execution_contract` is an opaque identifier or JSON `null`. Session state records what served the tool; it is not a complete telemetry history.

## Network observations

Docker and WSLC read their proxy logs before removal and publish the window only after removal confirms the proxy is gone. Failed or cancelled removal publishes nothing. A sequential retry reads the surviving proxy again and publishes on confirmed removal.

This is not an exactly-once log. Overlapping removals can duplicate a window. Host exit between removal and publication can lose one. A live or undrained proxy has no final record.

| Decision | Meaning |
|---|---|
| `ALLOW` | Proxy admitted an HTTP request; does not prove application-level success |
| `DENY` | Host, method, path, TLS requirement or resolved address refused by policy |
| `DENY-NONGLOBAL` | Legacy proxy's refusal of a non-global address |
| `UNREACHABLE` | Proxy could not connect or validate the upstream TLS peer |

Windows are bounded in lines and bytes. `truncated` means the window may be incomplete. Unreadable logs are reported as such. A successful CONNECT preflight is omitted when the proxy inspects the HTTP request inside it; rejected CONNECTs are reported. Target strings are guest-chosen and require the OpenTelemetry sensitive-data opt-in, including allowed targets.

`observes_egress=False` means the backend reports no decisions. `True` means it can report attributable windows; it does not promise complete traffic coverage. ACAS and Hyperlight emit no egress decisions. Absence of an event does not mean absence of traffic.

Docker and WSLC store the full key in ownership labels for recovery after a host restart. The encoded labels have a 4096-byte budget. An oversized key still acquires, but logs a warning and omits attribution labels. Legacy hashed labels alone cannot recover a key; a caller-supplied key needs a proven owner match.

## Process observations

Host-tool runs take snapshots at four boundaries. Register the observer on `HostToolRegistry` to receive them.

![A host-tool run takes process snapshots before launch, after launch, before cleanup and after cleanup. Cleanup actions emit separate records between the last two snapshots. Every snapshot has a three-second backend bound, at most 256 processes and at most one MiB of transport output. Guest-observed records can be incomplete or unavailable. Earlier incomplete observations prevent safe reuse even if the final scan succeeds. An empty final snapshot cannot prove that no process was hidden or missed.](assets/process-observation-flow.svg)

Snapshots retain available process identity, ancestry, user IDs, command, arguments, paths, state and resource usage. They do not collect environment variables. Attribution distinguishes the program, descendants, group members, preexisting processes and new unattributed processes. The last group is diagnostic evidence, not a kill list. Zombies do not count as running survivors.

Each collection is limited to 256 processes, 1 MiB and three seconds, with separate field bounds. Probes use `BoundedExec.exec_bounded`; a backend without it runs no probe. Unavailable, incomplete or over-budget observations enter the cleanup-failure policy. An earlier incomplete snapshot prevents reuse even if the final scan succeeds.

Cancellation records an unavailable, incomplete snapshot before propagating. Cancellation before launch can leave only the first boundary recorded.

| Cleanup outcome | Meaning |
|---|---|
| `sent` | Signal request succeeded; does not prove the process stopped |
| `absent` | Target observed absent |
| `refused` | Action refused or failed |
| `replaced` | Saved process identity no longer matched; signal skipped |
| `unrecorded` | No launcher identity retained |
| `unknown` | Outcome could not be established |

Cleanup reach is `group`, `program` or `nothing`. These fields do not prove full cleanup. Snapshots run inside the guest and can miss hidden processes or activity between boundaries. Separate checks by the sandbox engine provide stronger evidence.

Ordinary logs carry IDs, counts and outcomes. OpenTelemetry adds summary spans and counters, plus per-process logs. Commands, usernames and paths require `record_sensitive_data=True`. Process IDs and commands are never metric labels.

## Status

| Decision | State | Tracking |
|---|---|---|
| Core observer, event types and failure handling | Shipped | [#904](https://github.com/sokolaidev/maf-extensions/issues/904) (closed) by [#988](https://github.com/sokolaidev/maf-extensions/pull/988) (merged); [#906](https://github.com/sokolaidev/maf-extensions/pull/906) (merged) |
| Full served spec and session effective state | Shipped | [#380](https://github.com/sokolaidev/maf-extensions/issues/380) (closed) by [#953](https://github.com/sokolaidev/maf-extensions/pull/953) (merged) |
| OpenTelemetry logs, spans and metrics | Shipped; exports selected fields | [#907](https://github.com/sokolaidev/maf-extensions/pull/907) (merged); [#975](https://github.com/sokolaidev/maf-extensions/issues/975) (closed) by [#988](https://github.com/sokolaidev/maf-extensions/pull/988) (merged) |
| Call IDs on events | Shipped | [#922](https://github.com/sokolaidev/maf-extensions/issues/922) (closed) by [#952](https://github.com/sokolaidev/maf-extensions/pull/952) (merged) |
| Conversation purge records | Shipped | [#917](https://github.com/sokolaidev/maf-extensions/issues/917) (closed) by [#947](https://github.com/sokolaidev/maf-extensions/pull/947) (merged) |
| Proxy decisions and publication after confirmed removal | Shipped on Docker and WSLC | [#948](https://github.com/sokolaidev/maf-extensions/issues/948) (closed) by [#963](https://github.com/sokolaidev/maf-extensions/pull/963) (merged); [#970](https://github.com/sokolaidev/maf-extensions/issues/970) (closed) by [#1070](https://github.com/sokolaidev/maf-extensions/pull/1070) (merged) |
| Keys on host-tool and output-collection records | Shipped | [#949](https://github.com/sokolaidev/maf-extensions/issues/949) (closed) by [#959](https://github.com/sokolaidev/maf-extensions/pull/959) (merged) |
| Input-integrity summary on the call-end event | Shipped; not exported by OpenTelemetry | [#987](https://github.com/sokolaidev/maf-extensions/issues/987) (closed) by [#993](https://github.com/sokolaidev/maf-extensions/pull/993) (merged) |
| Process snapshots and cleanup action records | Shipped | [#463](https://github.com/sokolaidev/maf-extensions/issues/463) (closed) by [#1091](https://github.com/sokolaidev/maf-extensions/pull/1091) (merged) |
