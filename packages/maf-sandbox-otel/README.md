# maf-sandbox-otel

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** The API may change without notice. Importing the package emits `MafSandboxOtelExperimentalWarning`.

Record sandbox activity with OpenTelemetry logs, spans and metrics. The observer reports acquisition, host-tool calls, file transfers, process observations, network decisions and cleanup.

The package depends on `maf-sandbox` and the OpenTelemetry API. The application configures the SDK, exporters and retention. Without an SDK provider, the API uses its no-op implementation.

```bash
pip install maf-sandbox-otel
```

## Wiring

Register the observer on both the router and host-tool registry. Each registration covers a different set of events.

```python
from maf_sandbox import HostToolRegistry, SandboxRouter
from maf_sandbox_otel import OpenTelemetrySandboxObserver

observer = OpenTelemetrySandboxObserver()

router = SandboxRouter([backend], observer=observer)
registry = HostToolRegistry(observer=observer)
```

Kinds using `collect_outputs` pass `observer=` and `key=` to that function. The observer records existing events; it does not change tool policy or the labels on returned content.

![The router, sandboxed tool wrapper, host-tool registry, output collector and observing backends send events to a host observer. The OpenTelemetry observer selects attributes and sends logs, spans and metrics through independently configured providers. The host chooses exporters and retention. Tool and content labels remain part of agent policy; recording them does not enforce that policy.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/observability-channels.svg)

Provider arguments default to the application's global providers. Supply a separate provider to route a signal elsewhere:

```python
observer = OpenTelemetrySandboxObserver(
    logger_provider=security_logs,   # a SIEM pipeline, its own exporter and retention
    tracer_provider=None,            # spans stay with the application's traces
)
```

`logger_provider`, `tracer_provider` and `meter_provider` are independent. A separate audit logger can retain records even when the application samples out traces. Log export still depends on that provider's own configuration and delivery.

## Recorded signals

Each row emits a log and a span. Store reads use an instant span. Process snapshots also emit one `sandbox.process.observed` log per process, without a per-process span or metric label.

| Activity | Log / span name | Metric under `maf_sandbox.` |
|---|---|---|
| Acquire or refuse | `sandbox.acquire` | `sandbox.acquires` |
| Guest calls a host tool | `sandbox.host_tool_call` | `host_tool.calls`, `host_tool.response_bytes` |
| Read a host-store file | `sandbox.files_in` | `store.file_reads` |
| Collect output files | `sandbox.files_out` | `outputs.landed_files`, `outputs.landed_bytes` |
| Dispose a key | `sandbox.dispose` | `sandbox.disposals` |
| Purge a conversation | `sandbox.purge` | `scope.purges`, `scope.purged_sandboxes` |
| Observe network decisions | `sandbox.egress` | `egress.decisions` |
| End a sandboxed tool call | `sandbox.call` | `call.duration` |
| Observe processes | `sandbox.process.snapshot` | `process.snapshots` |
| Attempt process cleanup | `sandbox.process.cleanup` | `process.cleanups` |

Attributes use the `maf_sandbox.*` namespace, with `process.*` fields for process details. The [event guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md) describes each event and its limits.

## Sensitive data

`record_sensitive_data=False` is the default. Configuration, counts, sizes, outcomes and integrity labels are recorded. Model- or guest-chosen text and identifying host strings are omitted.

| Data | Default behavior |
|---|---|
| Sandbox key and conversation | Stable hashes for correlation |
| Scope, thread and agent IDs | Omitted; included with sensitive-data opt-in |
| Framework call IDs | Recorded in clear |
| Artifact names, store filenames and observed network targets | Omitted; included with opt-in |
| Process commands, argv, usernames and paths | Omitted; included with opt-in |
| Process IDs, numeric user IDs, ancestry, state and resource usage | Recorded when available |
| Detailed refusal, disposal and collection-error text | Sensitive fields require opt-in |

Hashes are not secrets. Small identifier spaces can be recovered by hashing candidates. The host must choose a telemetry destination and retention policy suitable for the data it permits.

The exported attributes are a selection from each event. They do not include every `SandboxSpec` field or `ToolCallEnded.fed`. A custom observer can read those fields directly.

## Correlation and trace shape

![Events for one tool call are emitted after their work and become sibling spans under the current application span. The final sandbox.call span records the full call duration; it is not the parent of earlier sandbox spans. The call ID joins them. A sandbox.egress span is detached from the current trace because its proxy window can span several calls; it has no tool-call ID.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/otel-trace-shape.svg)

Use `maf_sandbox.call.id` to join events from one tool call. A conversation-scoped sandbox key can be shared by several concurrent calls.

`maf_sandbox.sandbox.call_id` normally comes from a call-scoped key. On an output-collection record, it carries the collector's artifact call ID instead. Use `maf_sandbox.call.id` to identify the call that performed that collection.

Purge records join through `maf_sandbox.sandbox.conversation`, since they describe a conversation and a backend's count, not individual keys. A tool-call record touching several keys uses aligned lists for their attributes.

## Observation limits

Docker and WSLC report attributable proxy windows after removal confirms the proxy is gone. ACAS and Hyperlight report no egress decisions. Even an observing backend can have missing, unreadable, truncated or undrained windows. No egress record does not mean no traffic.

Process snapshots are bounded observations made inside the guest. Collection errors and truncation are explicit. They do not prove complete cleanup. See [process observations](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md#process-observations).

## Cost and failures

Callbacks run synchronously on the caller's event loop or worker thread. The observer keeps no per-call state and hands records to the OpenTelemetry API. Blocking export can still delay the sandbox call.

Use batch span and log processors when latency matters. Simple processors invoke exporters synchronously. Each signal is attempted independently, so an ordinary span-export failure does not prevent log or metric attempts.

Core contains and logs ordinary observer failures, including observer cancellation. `SystemExit` and `KeyboardInterrupt` remain host control flow and escape. The observer's return value cannot change the tool result.
