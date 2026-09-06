# maf-sandbox-otel

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-otel)](https://pypi.org/project/maf-sandbox-otel/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxOtelExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_otel.MafSandboxOtelExperimentalWarning)` once you've read the notice.

**OpenTelemetry records of what a sandbox did** — which conversation was served what posture, which host tools a guest called and under whose authority, what crossed the boundary and with what integrity label, and how each sandbox disposed **by key** was disposed of. A conversation's sandboxes can also go away in a *scope purge*, which core emits no event for and which is therefore not audited here — see [the limits](#three-limits-worth-knowing-before-you-rely-on-it) below.

`maf-sandbox` reports these as events on an observer seam and records nothing itself. This package is one observer: it turns each event into a **log record** and a **span**, and the countable ones into a **metric**. A store read has no duration of its own, so its span is a single instant. Every signal goes through the providers you give the constructor, so records you route to a security pipeline do not also land on the application's trace. It depends on `maf-sandbox` and the OpenTelemetry **API**, and on nothing else — no backend, no agent framework, no SDK.

```bash
pip install maf-sandbox-otel
```

## Wiring

There are two registration points because there are two host-policy objects, and a host that wires one records only that half.

```python
from maf_sandbox import HostToolRegistry, SandboxRouter
from maf_sandbox_otel import OpenTelemetrySandboxObserver

observer = OpenTelemetrySandboxObserver()

router = SandboxRouter([backend], observer=observer)
registry = HostToolRegistry(observer=observer)
```

`collect_outputs` is neither — it is a function a kind calls per collection, so a kind that reports its file landings passes the observer and the key as arguments.

Each provider argument defaults to the global one, so with nothing else configured these records land beside the application's own traces:

```python
observer = OpenTelemetrySandboxObserver(
    logger_provider=security_logs,   # a SIEM pipeline, its own exporter and retention
    tracer_provider=None,            # spans stay with the application's traces
)
```

Splitting them is usually what a security record wants. A SIEM does not want the application's trace sampling applied to it, and an application's trace store does not want a year of egress records. The three providers are independent, so a deployment can move the logs and leave the spans where they were.

## What is recorded

| Event | Span | Metric |
|---|---|---|
| A sandbox was served, or refused | `sandbox.acquire` | `maf_sandbox.sandbox.acquires` |
| A guest called back into the host | `sandbox.host_tool_call` | `maf_sandbox.host_tool.calls`, `.response_bytes` |
| A call read a file out of the host's store | `sandbox.files_in` *(one instant, no duration)* | `maf_sandbox.store.file_reads` |
| A collection landed artifacts in a sink | `sandbox.files_out` | `maf_sandbox.outputs.landed_files`, `.landed_bytes` |
| One backend answered one disposal | `sandbox.dispose` | `maf_sandbox.sandbox.disposals` |
| A sandboxed tool call ended | `sandbox.call` | `maf_sandbox.call.duration` |

Every event also emits a log record, and that is the one a security pipeline should keep: it does not depend on anything else being instrumented, and it survives a trace sampler that discarded the span. Attributes are under `maf_sandbox.*`, so they select cleanly out of a pipeline carrying everyone else's.

## What crosses, and what does not

**Shape and policy always; content only when asked.** A sandbox's posture — the egress mode and its allowlist, the isolation rung, the capabilities, the sealed host-tool surface and the authority it carries, the integrity label, the counts, the sizes, the outcome — is what a security question is asked in, and a guest chooses none of it. Names and sentences are the other half: an artifact name is written by the model, a host-tool refusal quotes a bounded copy of what the guest asked for, and a store file name is the host's own vocabulary about its own data. Those cross only under `record_sensitive_data=True`, which mirrors the agent framework's switch of the same name and is off by default.

A `SandboxKey` is the column every other record joins on, so it cannot simply be dropped. It is **hashed** by default — stable across processes, so grouping still works, and not reversible by reading. It is not a secret: an id drawn from a small space can be recovered by hashing the candidates, and a deployment that needs the key withheld from a pipeline should not send it rather than trust this.

The **call id** is the one part of a key recorded in the clear. The framework generates it per call, it is drawn from nobody's vocabulary, and it is what names the folder a `per_call` sink lands that call's artifacts in — so hashing it would cost the correlation a landing record exists for and protect nothing.

## Three limits worth knowing before you rely on it

**The egress posture recorded is the one that was *served*, not the traffic that was *reached*.** `sandbox.acquire` carries the mode and the allowlist the sandbox ran under. It does not carry which hosts the guest actually opened a tunnel to: the docker and wslc proxies print their `ALLOW`/`DENY` lines inside their own container, which the backend reads once at acquire and never again, and ACAS enforces egress in the service where the guest is the only party that sees the refusal. Reaching those lines is a change in each backend rather than here, and until one lands, "which conversations were *allowed* to reach host X" is answerable and "which ones *did*" is not.

**A scope purge is not recorded, so one of the two ways a sandbox goes away is invisible.** `SandboxDisposed` is emitted per key. `dispose_scope` — what a thread deletion runs, and what `router.scope(...)` runs when its block ends — asks every backend and emits nothing, so a conversation's sandboxes can be removed with no record here. The gap is core's rather than this package's, and it is not an oversight that a recorder could paper over: a backend answers a purge with a *count* rather than the keys it removed, so there is no key to put in an event. Until core grows an event keyed on `(scope, thread_id)` ([#917](https://github.com/sokolaidev/maf-extensions/issues/917)), "was every sandbox for this conversation cleaned up" is not answerable from these records.

**The events of one call are siblings, not children of `sandbox.call`.** Every event arrives after the work it describes, and the call's own event arrives last, so there is no moment at which this package could open a parent for the others to nest under. Each is parented to whatever span is current where it arrives instead — the agent framework's `execute_tool` span — and `sandbox.call` carries the total the caller waited for. Buffering them into a real tree would need per-call state that a cancellation could leak. That holds for a tool body that awaits nothing too, even though its record arrives on a worker thread: the framework dispatches with `asyncio.to_thread` from inside the span, and that copies the context the current span lives in, so the span crosses with the body. A test reproduces that dispatch rather than describing it.

## Cost

An observer is called synchronously inside the call it records — on the event loop's task, or on the worker thread a synchronous tool body runs on — so this package does no I/O: it hands each record to the OpenTelemetry API and returns. It keeps no per-call state, so two calls reaching one observer at once share nothing to race over; the OpenTelemetry tracer, meter and logger it holds are thread-safe by that API's own contract. With no SDK installed at all, the API's no-op implementations answer and the cost is a few attribute dictionaries per call.

**Whether export blocks the call is your SDK configuration, not this package.** A `BatchSpanProcessor` and a `BatchLogRecordProcessor` hand off to their own thread, which is what keeps a slow collector away from a sandbox call. The `Simple*` processors call the exporter **synchronously**, inside `span.end()` and `logger.emit()` — so configured that way, a network exporter blocks the call for as long as the export takes. Use the batch processors where call latency matters; the simple ones are for tests, which is what this package's own suite uses them for.

A failure here never reaches the call — `maf-sandbox` contains whatever an observer does and logs it. With two deliberate exceptions: `SystemExit` and `KeyboardInterrupt` are the host's own control flow rather than a recorder failing, so core lets them through, including when one arrives as a leaf of an exception group. Nothing in this package raises them, but an exporter or a provider you configure can, and if it does the call goes down with it.
