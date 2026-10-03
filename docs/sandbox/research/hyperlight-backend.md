# Hyperlight research

> Consolidated research record, 2026-08-16 through 2026-09-23. It combines the Hyperlight backend design, source exploration, filesystem prerequisite and cleanup audit, Azure Container Apps feasibility audit and live ACA probe, and the AKS upstream audit and measurements. The runtime backend is implemented for its validated family; flat output collection is now opt-in; writable inputs and native host tools remain separate follow-up work. The decided contract lives in the [Hyperlight backend guide](../backends/hyperlight.md).

> The [2026-10-02 host-tool channel proposal](#host-tool-channel-design-2026-10-02) adds the source audit, agreed architectural choices and remaining prototype questions for #369. Its decided target contract lives in [Host responsibilities](../hosts.md#backend-owned-channels-and-automatic-selection); the core/exec interface is now implemented in [program channels](../program-channels.md); native integration remains pending.

## Decision and scope

`maf-sandbox-hyperlight` is a runtime-shaped `SandboxBackend` over `hyperlight-sandbox` directly. It is not an integration of `agent-framework-hyperlight`: that package's provider and `execute_code` tool are the layers this suite replaces, while the Hyperlight SDK and Wasm guest are the backend beneath the suite's protocol.

The backend serves Python source through `RUN_CODE`, not shell commands through `EXEC`. Its guest is a hardware micro-VM running CPython compiled to Wasm. There is no shell, argv, process table, `subprocess`, `os.fork` or host filesystem in the guest. A spec requiring `EXEC`, including a say-nothing spec whose default capabilities include it, is refused at attach rather than degraded. CodeAct uses the explicit `CodeactRuntime` path for this backend.

The initial supported family is the pinned Python guest/Wasm backend on x86-64 Windows WHP and Linux KVM. The backend declares `MICROVM`, `RUN_CODE` and `SNAPSHOT`; file capabilities and native host tools are withheld until their own contracts and conformance are complete. Egress is `CLOSED` or exact-host `ALLOWLIST` with HTTP/HTTPS root permissions; method and identity refinements are not part of the current backend contract. Linux MSHV, custom guests, Hyperlight-JS and other inner backends need independent family entries and evidence.

The central design rule is that declarations derive from the configured guest. A backend instance resolves its family member at construction and keeps the resulting declarations static. A custom guest or unsupported inner backend starts with no declarations and is refused or admitted only after its own conformance run. Hosts register separate configured instances and let the router select between them; the adapter does not choose a weaker declaration per request.

## The stack and dependency boundary

| Layer | Role | Measured size or version |
| --- | --- | --- |
| `agent_framework_hyperlight` | MAF context provider and `execute_code` tool; not used by this backend | approximately 1,700 Python lines |
| `hyperlight_sandbox` | Stable Python facade: create, register tools, allow domains, run, snapshot, restore and collect outputs | 234 Python lines in the source read |
| `hyperlight_sandbox_backend_wasm` | PyO3/Rust module with Wasmtime, WASI, wasi-http and KVM/MSHV/WHP drivers | approximately 9 MiB native binary in the source read |
| `hyperlight_sandbox_python_guest` | CPython compiled to wasm32-wasi with `call_tool`, HTTP helpers and output support | approximately 19 MiB Wasm, 44 MiB AOT in the source read |

The published stack has demonstrated version skew: an AOT guest compiled with one Wasmtime patch level was paired with a backend embedding another, and the native binding does not JIT raw Wasm. The matched trio must therefore be pinned exactly and every bump requires a new conformance run. The older source exploration used a matched 0.4.0 trio; the later filesystem and ACA measurements used the matched 0.7.0 trio. A broad dependency range is not safe.

The native `WasmSandbox` is thread-confined. It cannot be touched or garbage-collected from another thread without an uncatchable Rust panic. A worker actor must own the sandbox and snapshot for its whole lifetime, including disposal. The worker also owns path-confinement checks for staging and output collection; symlink, junction, reparse-point and TOCTOU defenses belong to the suite's path utilities rather than being imported from the upstream wrapper.

## Contract and measured runtime behavior

### Execution

The protocol-facing method is conceptually:

```python
async def run_code(self, code: str, *, timeout: float) -> ExecResult
```

The result carries stdout, stderr and an exit code. The guest returns stdout only; the last expression is not echoed, so instructions must tell the model to use `print(...)`. Artifacts, where a future file channel is enabled, belong under `/output`. The pinned runtime has a reduced standard library: `json`, `math` and `re` are available in the measured guest, while modules such as `datetime`, `statistics`, `pickle` and `__future__` were not. The guest also lacks `threading`, `subprocess`, `os.fork`, `socket`, `ssl` and `urllib`; HTTP is supplied only through injected helpers when egress is allowed.

A sandbox is created, warmed by one initial run and snapshotted. Each call restores the baseline, runs fresh source and collects output. Ordinary Python exceptions return a nonzero execution result and leave the sandbox reusable. Native failures, oversized FFI payloads, memory exhaustion and runtime panics poison the sandbox; restoring the baseline can heal those failures. A hang is different: no interrupt, fuel, deadline or cancellation exists in the SDK, so a stuck thread cannot reach `restore`. The timeout boundary must therefore be an OS-process kill followed by worker recreation, and the cold start must not consume the guest-program timeout.

One sandbox serves one call at a time. Queue time must be distinguishable from a program exceeding its budget because the caller's next action differs. Cancellation and timeout must terminate and reap the whole worker process rather than abandon a thread holding a guest. The backend's current lifecycle guide defines the Windows job and Linux cgroup containment required to make that claim honest.

### Measured performance and limits

The source exploration measured a lazy constructor at approximately 50–70 ms, a first run at approximately 4.2 seconds, snapshot creation at approximately 0.65 seconds, steady restore-plus-run at approximately 8.7 ms and a plain run at approximately 0.4 ms. A 1 MiB source string added approximately 16 ms. The older Windows guest reached a hard abort at approximately 40–50 MiB of user allocation despite a 400 MiB guest heap default; memory headroom is therefore a workload constraint, not merely a transfer-limit detail.

The native shared buffer is 16,376 bytes. FFI framing consumed approximately 192 bytes across the measured failure sizes, so a host-tool response ceiling must be enforced well below 16 KiB before a value crosses the boundary. Exceeding the buffer is an in-guest uncatchable abort that poisons the sandbox; it is not a clean refusal.

The guest has additional sharp edges at the pinned versions: `time.sleep` can panic because no Tokio reactor is present, and `os.listdir` fails even where direct `open()` works. Network helpers require scheme-qualified targets in the tested SDK. These facts belong in runtime instructions and tests, not in a claim of general CPython compatibility.

### Egress

Hyperlight's native wasi-http boundary enforces an exact per-entry allowlist. The default is deny-all; `allow_domain` permits a selected host and still rejects an unlisted host. The adapter translates a core hostname into HTTP and HTTPS root permissions because the native API requires scheme-qualified targets. The same boundary enforces the seven standard methods named in `allow_domain(target, methods)`, measured live on 2026-09-25; the adapter declares `EGRESS_METHODS` for them. The contract does not expose arbitrary ports, wildcards, raw sockets, CONNECT, TRACE or custom methods.

HTTP runs on the host network. Allowing an internal hostname or loopback therefore grants access to the host-side network, and hostname policy does not filter resolved IP addresses. Application credentials and proxy settings are not forwarded, and the backend attaches no identity. The host owns destination authorization. Custom method tokens make the next run fail, so the adapter refuses them before they reach the runtime.

### Host tools

The guest's `call_tool(name, **kwargs)` is a synchronous FFI callback. Registration happens before the first run and cannot be undone; callbacks live for the sandbox lifetime. The callback runs on the same OS thread that called `run`, so an adapter needs a trampoline per sealed registry name and a bridge from that thread to the host event loop. The guest blocks until the host returns, and a hung callback hangs the guest.

The FFI marshals nested dictionaries, lists, strings, numbers, booleans and null. Positional arguments are rejected. Unsupported return types fall back to string conversion, which is lossy. Host exceptions become catchable guest `RuntimeError` values while the sandbox remains healthy; unregistered names use the same wrapper. Duplicate registration last-wins in the measured SDK, so the adapter must enforce the suite's own sealed-name policy before registration.

The native channel supplies only name lookup. It has no call cap, response ceiling, argument policy, integrity label, identity policy or per-call approval. Those gates belong to `HostToolRun` and the core protocol. Native host tools are therefore a separate follow-up under [#369](https://github.com/sokolaidev/maf-extensions/issues/369), with the measured 16,376-byte boundary and approximately 192-byte framing as acceptance inputs.

## Host-tool channel design, 2026-10-02

This proposal records the decisions for [#369](https://github.com/sokolaidev/maf-extensions/issues/369), following a source and issue-state audit on 2026-10-01 and a design discussion on 2026-10-02. It proposes a backend-owned channel with automatic selection, not merely a new native callback on the existing runtime method. The agreed target behavior is in [Host responsibilities](../hosts.md#backend-owned-channels-and-automatic-selection). Exact API signatures and the first execution profile remain subject to a bounded prototype on the pinned Hyperlight stack.

### Evidence and remaining gap

The audit used main at [`9240d7283bd8215bb3b253c3e49d518ba1ce4cbc`](https://github.com/sokolaidev/maf-extensions/commit/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc). It read source and GitHub state; it did not execute tests or acquire a Hyperlight guest. At that observation, #369 remained open and [#425](https://github.com/sokolaidev/maf-extensions/issues/425) was closed: [#1199](https://github.com/sokolaidev/maf-extensions/pull/1199) had delivered CodeAct's explicit runtime mode. Native host tools therefore needed integration with an existing runtime path, independently of the delivered initial Hyperlight runtime and its platform work.

| Audited surface | Finding | Design consequence |
| --- | --- | --- |
| Core [`_router.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox/src/maf_sandbox/_router.py), `fold_host_tool_call_transfer_limits` | Every spec carrying host tools receives exec/file transport overhead. | Admission must use the selected channel's resources and ceilings while preserving separate workload file limits. |
| Core [`_protocol.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox/src/maf_sandbox/_protocol.py), `BackendDeclarations` and `Capability.HOST_TOOLS` | The declarations object already exists; `HOST_TOOLS` has no corresponding channel operation. | Extend the contract; do not repeat the completed declarations consolidation. |
| CodeAct [`_tool.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox-codeact/src/maf_sandbox_codeact/_tool.py), `_codeact_spec` and `host_tool_calls_over_exec` | Runtime mode explicitly refuses nonempty registries. Exec mode constructs the shim and layout, selects the interpreter and drives the transport. | Move transport composition into backends and connect the existing runtime consumer through the new contract. |
| Hyperlight [`_backend.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_backend.py) and [`_worker.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_worker.py) | Acquisition rejects host tools. Initialization warms the guest and takes a snapshot without registering tool callbacks. | Registration compatibility and baseline preparation must be designed before native host tools can be declared. |
| Hyperlight [`_process.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_process.py), `Worker.request` | The parent sends one request and reads one final response while the native worker executes. | Host-tool callbacks require a bounded exchange in both directions while the program is active. |
| Core [`_host_tools.py`](https://github.com/sokolaidev/maf-extensions/blob/9240d7283bd8215bb3b253c3e49d518ba1ce4cbc/packages/maf-sandbox/src/maf_sandbox/_host_tools.py), `HostToolRun.call` | Policy, serialization and byte accounting are centralized, but success is recorded before the transport sends the result. | Preserve this policy entry point and add transport confirmation to delivery accounting; checking native size only after return is too late. |

The audited Hyperlight package pins the SDK, Wasm backend and Python guest to 0.7.0. The 16,376-byte buffer and approximately 192-byte framing above are historical measurements from the matched 0.4.0 exploration. They are inputs to remeasure, not constants to copy into the new declaration. No current native callback capacity or cancellation result is established by this proposal.

### Decisions and alternatives

| Decision | Chosen direction and reason | Alternative not selected |
| --- | --- | --- |
| Transport ownership | Shape C: the backend owns transport resources, execution and cleanup; core retains policy through `HostToolRun.call`. This represents both file transport and native callbacks. | Transport flags or a transport axis driven by the kind leave CodeAct composing execution-specific mechanics. |
| Execution selection | The router automatically selects a compatible backend and channel for the workload's requirements. | Requiring the host to select CodeAct's exec/runtime variant for every attachment. This changes the earlier explicit-selection recommendation; that mode's shipped existence remains a foundation. |
| Preference | The host orders eligible candidates; exec-first is the default without a preference. | A universal runtime-first choice or a fixed preference the host cannot override. |
| Acquisition fallback | Try the next compatible option only for a classified availability failure during initial acquisition, before workload execution. | Stop on every availability failure, or replay after execution starts or its start becomes uncertain. Policy/configuration failures stop the call. |
| Guest API | Preserve `maf_host_tools` across transports, including arguments, results and refusals. | Backend-specific guest APIs are deferred to a separate follow-up issue, not rejected permanently. No such issue has been filed by this record. |
| Limits | Strict admission, including framing and refusal envelopes; incompatible channels are skipped without clamping. | Negotiating smaller effective limits after selecting a channel. |
| Runtime compatibility | Named execution profiles, starting with a narrowly defined portable profile whose guarantees need validation. | Treating all Python runtimes as interchangeable or requiring one universal environment for every workload. |
| Selection lifetime | Pin backend and channel for the sandbox's lifetime. | Per-call reselection that silently discards or moves guest state. |
| Callback timeout | Return a recoverable refusal only after the host call stops cleanly and the channel can resume; otherwise terminate the run and retire the sandbox. | Always terminating on timeout, or resuming while a callback remains unsafe. |
| Delivery accounting | Reserve budget before transmission and commit delivery only on transport confirmation; record host execution separately. | Counting a serialized response as delivered before the transport accepts it. |
| Migration | Require the new contract immediately for backends declaring `HOST_TOOLS`, with a breaking release and migration guidance. | A legacy exec compatibility adapter. |
| File requirements | Keep file capabilities and budgets independent of execution profiles. | Multiplying profiles for no-files, output-only and writable-input combinations. |
| Delivery plan | Two implementation PRs, informed by a native prototype before finalizing the first. | One PR containing the entire core, exec and native implementation. |

### Proposed contract structure

The following responsibilities describe the proposed interface, not public type names or a settled signature. Protocol-facing data and structural interfaces must remain standard-library-only. A kind must not import a backend or select native SDK functions itself.

| Responsibility | Information or operation the contract must carry |
| --- | --- |
| Workload requirements | Named execution profile, program intent, host-tool surface, independent file requirements and the existing security policy. |
| Backend declaration | Supported profiles and channel contracts, transport-owned resources, request/response/refusal ceilings and framing rules. Declaring `HOST_TOOLS` obligates a working channel for the advertised contracts. |
| Selection | Host preference over permitted candidates, complete compatibility checks, an immutable selected backend/channel for the acquired sandbox, and explicit availability failure classification. |
| Program execution | Source submission, selected profile, the live run's policy entry point and deadline, with backend-owned interpreter, shim, layout and supervision where needed. |
| Results | Guest stdout/stderr, execution outcome, trusted transport diagnostics kept distinct from guest text, and host execution/delivery observations. Preserve queue-versus-program timeout distinctions. |
| Lifecycle | Registration and snapshot compatibility, per-run binding and invalidation, bounded cancellation, output collection before cleanup, and retirement when safety cannot be established. |

Selection must check a complete candidate contract rather than replacing `EXEC` with `RUN_CODE` in one flat capability set. The backend's private transport prerequisites do not become universal workload requirements. Host-denied operations remain denied; preference and fallback cannot widen the configured routing boundary. How these requirements are represented alongside the current `SandboxSpec`, `Selection.FIXED` and `Selection.PER_SPEC` APIs remains an interface question for PR 1.

CodeAct instructions must describe the requested profile's actual guarantees. The profile needs a defined Python language/version policy, guaranteed imports and facilities, source/output behavior, and the shared host-tool API. File placement and access still require their own capabilities. An arbitrary exec image cannot claim portable compatibility merely because `python3` exists, and a `RUN_CODE` declaration alone does not establish Python support. Profile naming, versioning and the first supported environment need prototype evidence before they become API.

### Native callback and delivery lifecycle

The parent retains the registry, host callables and caller authority. The worker registers transport trampolines compatible with the sealed tool-name set before warming and snapshotting where the native SDK requires that ordering. Registration establishes a route, not authority to execute a host tool outside a live run. The registration shape itself must be verified on the pinned SDK.

For each program, the parent creates a fresh `HostToolRun` in the caller's context and binds the worker channel to that run. A callback message carries only bounded, validated protocol data. The parent resolves it through `HostToolRun.call` on the host event loop, preserving declaration and argument checks, identity/approval policy, caps, sanitized refusals and observation. No host callable or credential is serialized into the guest or worker registration.

The synchronous native callback must wait with a deadline while the parent services the request. The implementation needs correlation and run-generation checks so stale, duplicated or delayed worker messages cannot spend a later run's authority or count delivery twice. Completion, timeout and cancellation must invalidate the binding before reuse. This is a proposed mechanism to enforce the agreed per-run boundary; the wire schema and correlation types are not settled.

Both host-tool and remaining program deadlines constrain the callback. Cancellation must be observed by the host call, not merely by the waiting worker. Cancelling an await on a thread does not prove its callable stopped. If bounded cleanup cannot establish that the host call ended and the channel is healthy, end the run and retire the sandbox; record any unresolved host operation rather than implying worker termination undid its effects.

Response handling needs two phases inside the core policy path: validate/serialize and reserve the bounded response, then commit delivery when the transport confirms acceptance or publication. Exec publication and native acceptance need explicit, testable confirmation points. A parent-to-worker pipe write alone does not establish that native marshalling accepted the response. Strict JSON, escaping, framing and refusal envelopes must fit every boundary before transmission. The prototype must determine whether the pinned SDK exposes sufficient confirmation or requires a revised bridge.

Failure after host execution must not be reported as an unexecuted tool. An uncertain handoff must not be counted as confirmed delivery or automatically retried. The accounting design must settle reservation release, uncertainty and observer ordering without enabling a failed or duplicated confirmation to free budget incorrectly. These details change `HostToolRun.call` internally while preserving it as the sole policy entry point; a second dispatcher bypassing it is not an implementation option.

### Prototype and acceptance

Before finalizing the first PR, run a bounded prototype against the exact supported Hyperlight dependency trio. Record versions, platform, source revision, encoded sizes and observed failure outcomes. Keep synthetic callbacks separate from host credentials and external side effects. Historical measurements, mock callbacks and ordinary `RUN_CODE` tests do not establish native host-tool conformance.

| Check | Required evidence |
| --- | --- |
| Shared guest API | The same program uses `maf_host_tools` on exec and native channels; arguments, JSON results and refusals agree. |
| Profile guarantees | Verify the initial portable Python facilities and imports; unsupported images/profiles refuse before workload execution. |
| Native envelopes | Measure request, response and refusal boundaries, including Unicode/escaping and actual framing; oversized values never reach an unsafe native buffer. |
| Event-loop bridge | Service callbacks while the native run blocks, preserve caller/observation context, and reject stale or invalid worker messages. |
| Deadlines and cancellation | Distinguish queued work, program expiry and callback timeout; prove recoverable refusal only after clean host-call termination, and retirement otherwise. |
| Registration and reuse | Verify pre-warm registration and snapshots, reject incompatible tool-name sets, and prove no authority survives from one run into the next. |
| Delivery | Demonstrate the confirmation point; inject failure before/after host execution and during handoff, with truthful ledgers and observations and no automatic retry. |

The implementation suites must also cover host preference, both routing modes, initial availability fallback, policy/configuration refusals, strict budgets, independent file limits, sandbox-lifetime pinning, monitored wrappers, output collection and cleanup ordering. Both channel shapes must exercise the same validation, identity/approval and cap policy. Existing exec behavior needs regression coverage; Hyperlight may declare `HOST_TOOLS` only after native conformance passes on the pinned stack for each claimed family.

### Two-PR implementation and follow-up

1. **Core and exec migration:** settle the contract with the prototype results; add profiles, automatic selection, host preference and classified initial-acquisition fallback; make admission transport-specific; implement confirmed delivery accounting; migrate CodeAct and every repository-owned backend already declaring host tools. Missing channel implementations refuse attachment. Include breaking-change migration guidance, wrapper/conformance changes and dependency bounds needed by each adapting package. The native prototype informs this PR but does not itself enable Hyperlight's capability.
2. **Hyperlight integration:** implement the bounded worker callback protocol, registration/snapshot compatibility, per-run authority, shared guest API, cleanup and native delivery confirmation. Connect runtime CodeAct and retain real-guest conformance evidence before declaring `HOST_TOOLS`.

These are two implementation PRs, not promises of two release events. Release order must follow the repository's core/dependent publishing rules. Neither PR has a number yet. #369 remains open until both implementations and required verification are complete; a core-only change must not close it or mark native support delivered.

Backend-specific guest APIs belong in a separate future issue. That follow-up should examine an explicit host-selected opt-in, truthful instructions and profile compatibility without weakening the shared API default. It is deferred and unfiled here, not an additional acceptance requirement for #369. Guest-initiated HTTP callbacks and migration of live sandbox state are outside this design.

Recording this proposal adds no implementation, live-test result, profile guarantee or production acceptance evidence. The signature, profile guarantees, envelope ceilings, delivery confirmation and cancellation behavior listed above remain the prototype's open questions.

## Native host-tool prototype, 2026-10-02

The bounded [prototype](../../../scripts/probe_hyperlight_host_tools.py) exercised the proposed callback bridge on Windows 11 x86-64 with WHP, host Python 3.13.12 and the exactly matched 0.7.0 SDK, Wasm backend and Python guest. It started from repository commit `09b5aaab1cbfecfd79f8e15cfb4a1fb15c1a28e2`, which contains the design above. This is synthetic research evidence; no production channel, profile declaration or `HOST_TOOLS` capability was added.

The final matrix covered 30 native cases and one Docker comparison, with no unexpected results under the harness's explicit positive and negative expectations. Its probe source SHA-256, normalized to UTF-8 with LF newlines, was `b47f4ec432fe107e7eb4d23457f8f6c57584f514592a602b4ad49420ff8a99df`. The measurements below retain the relevant outcomes; the raw local report also contains worker diagnostics and is not committed.

A same-day follow-up tightened EOF classification to the first observed failing request value size, 16,200 ASCII bytes, independently of the 8 KiB application cap. The corrected validator accepted all 31 original recorded outcomes. A fresh four-case native run, with probe source SHA-256 `665a2269bc7c4b8987e13a29c503c6c2768d05d175b5d7a0389ac9ff715dc5cc`, again delivered 12,044-, 16,044- and 16,144-byte request payloads to the parent and observed EOF for the 16,200-character value; every worker was reaped. Offline regressions require unexpected EOF below that observed failure point to produce failed evidence and a nonzero CLI exit. This does not establish a universal native ceiling.

The worker reused the adapter's contained process boundary, registered one internal `maf_dispatch` callback before warming, installed an in-memory `maf_host_tools` module and snapshotted that baseline. Each program restored the baseline and received a fresh run generation. The parent retained the registry and resolved callbacks through the real `HostToolRun.call` on its event loop. No guest files were needed for the native facade. One generic dispatcher was sufficient; native registration did not need to carry individual host callables or change between runs. Late registration after initialization raised `RuntimeError`.

### Observed behavior

| Probe | Observed result |
| --- | --- |
| Shared API on native and Docker exec | The same program round-tripped nested JSON, Unicode, booleans and null, then caught a missing-tool refusal as `HostToolError`. Both printed identical stdout and exited zero. Docker used `python:3.13-slim` and the shipped file transport. |
| Warm reuse and authority | A second program used a fresh `HostToolRun`; policy observation retained its new context. A callback outside a live run failed before policy dispatch. A synthetic stale generation was rejected before dispatch. Offline coverage also rejected duplicate callback sequences. |
| Policy refusals | Call cap, response cap and invalid arguments produced catchable refusals. A successful call after response refusal demonstrated continued guest execution. An oversized request that still fit the native boundary was refused before core dispatch. |
| Cooperative callback timeout | The synthetic async callback stopped before the refusal was returned; the program caught it and successfully called another tool. |
| Uncooperative callback, program timeout and cancellation | The worker was retired and reaped. The synthetic cancellation-resistant host task was separately released and drained after retirement; killing the worker did not itself stop that host task. |
| Delivery counterexample | Core emitted `HostToolCalled(outcome="delivered")`, then the worker emitted its prepared-response marker, then a forced exception before native marshalling caused guest failure. Neither marker proves native acceptance. |
| Python facilities | Guest Python reported 3.14.0. `json`, `math`, `re`, `sys`, `types` and `os` imported; `asyncio`, `datetime`, `statistics`, `pickle`, `__future__`, `threading` and `socket` did not. Importability alone does not certify every operation in a module. |

Every measured native worker was reaped and its diagnostic drainer stopped; the comparison container was disposed. The host tools were synthetic async functions without credentials or external effects. This does not establish cancellation safety for blocking synchronous host functions, host identity/approval conformance, cross-process owner death, queued-work expiry, incompatible registry reuse, Linux KVM, AKS or MSHV.

### The two native directions have different limits

The earlier 0.4.0 measurements cannot be reused as a 0.7.0 response ceiling. At the pinned upstream commit [`6ae78065617d5603c1dd5fdbb63d62d8201ac68c`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/wasm_sandbox/src/lib.rs), `WasmComponentSandbox::with_tools` configures the guest input buffer with `config.heap_size.min(70_000_000)`. That is a source-level allocation request, not a measured usable payload maximum. This probe used a 400 MiB heap and 200 MiB stack and deliberately stopped far below that allocation.

| Direction | Encoded observations | Outcome |
| --- | --- | --- |
| Host response to guest | ASCII strings of 8,000, 16,300, 20,000, 64,000, 256,000 and 400,000 bytes | All reached the guest at the expected length. These cases deliberately replaced the core-approved value after dispatch and bypassed its response cap; they measured transport only. |
| Host refusal to guest | A 64,000-character refusal inside a 64,015-byte JSON envelope | Guest caught `HostToolError` and measured the full refusal. This also bypassed the application response cap. |
| Guest request to host | JSON payloads of 8,044, 12,044, 16,044 and 16,144 UTF-8 bytes | All reached the parent callback. The first round-tripped; the larger replies were refused by the conservative core response cap. |
| Guest request to host | JSON payload construction of 16,244 bytes and larger, through a 400,000-character value | The worker pipe closed before any callback reached the parent. Cleanup succeeded. This brackets an observed failure point; it does not measure the complete native frame or identify an exact universal ceiling. |
| Escaping | An 812-byte response JSON string containing Unicode, quotes, backslashes and newlines required an estimated 1,318 bytes when encoded again as a native JSON string | Round-trip preserved the value. JSON string framing is content-dependent, not a fixed byte allowance. The report separately counts the complete parent-to-worker IPC envelope. |

Ordinary policy cases used an 8 KiB application cap and a 512 KiB actual IPC envelope cap. The prototype's `framing_bytes=32` is a synthetic core-accounting input, not a declaration of native overhead. The estimated native JSON length models serialization of the returned string; it excludes lower-level native framing. Full strict limits still need every real representation, including refusal envelopes, bounded before publication. A parent-side request check cannot prevent a failure that happens before the native callback reaches the parent. A guest helper check can improve diagnostics but cannot constrain a guest that calls the native dispatcher directly.

The current upstream [`ToolRegistry::dispatch`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/hyperlight_sandbox/src/tools.rs) calls `schema.validate(name, &args)?` when the tool has a schema, before executing its handler, unlike the older name-only observation above. For this prototype it validates the dispatcher's string argument, not the host tool's nested arguments. Core remains the policy boundary.

The Python bridge's [`build_tool_registry`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/sdk/python/pyo3_common/src/lib.rs) creates a handler that obtains the callback result with `cb.call(py, (), Some(&kwargs))?`, resolves a possible coroutine, then converts it with `py_to_json(result.bind(py))`. The subsequent [`Tools<HostBindings>::dispatch` implementation for `HostState`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/wasm_sandbox/src/lib.rs) serializes the dispatched value with `serde_json::to_string(&v)`. Callback completion therefore precedes this serialization; the inspected public Python API exposes no confirmation hook after it.

### Consequences for the implementation

The in-memory shared facade, single registered dispatcher and caller-context event-loop bridge are viable for the measured guest. A named portable profile must nevertheless specify a deliberately small language/import contract; it cannot promise ordinary CPython's standard library or infer compatibility from the host interpreter's version.

The prototype does not validate the delivery contract yet. Core needs separate execution, reservation, confirmed delivery and uncertain-handoff states. Native integration needs a trustworthy acceptance hook after the relevant serialization, or another bridge with an equivalent confirmation point. A pipe write, callback return preparation, guest-controlled acknowledgement or successful program completion cannot establish per-response native acceptance. This remains a prerequisite for the Hyperlight implementation; no uncertain callback should be replayed.

Request framing also remains a native integration prerequisite. The measured parent refusal is useful only after the callback arrives; direct oversized native calls must have a bounded containment outcome and must not be presented as recoverable policy refusals. Core/exec migration can use these findings to define the contract, but must not attribute a production-native capability to this prototype. [#369](https://github.com/sokolaidev/maf-extensions/issues/369) remains open.

### Reproducing the bounded prototype

On a WHP-capable Windows host with the locked workspace environment and Docker available, run the following with a fresh output filename. The command executes only synthetic callbacks and a local comparison container. Omit `--docker` for native-only measurements; `--case` selects individual probes. Linux requires the adapter's delegated cgroup setup and is not qualified by this Windows record.

```powershell
uv sync --locked
uv run python scripts/probe_hyperlight_host_tools.py --live --docker --output native-channel-probe.json
uv run pytest -q tests/test_probe_hyperlight_host_tools.py
```

The JSON report retains platform, exact dependency versions, base commit, the probe's SHA-256 over UTF-8 source with LF newlines, complete response IPC envelope byte counts, callback events and cleanup outcomes. It refuses to overwrite existing evidence and exits nonzero for unexpected results. Expected large-request pipe closure is an explicitly recognized negative observation, not native conformance. Raw worker diagnostics can contain local paths; review them before sharing the report. The offline tests cover strict IPC parsing, escaped-byte limits, stale/duplicate authority checks and false-positive evidence classification; they are not substitutes for the live run.

## Filesystem findings

Runtime execution does not require file channels. The initial backend withholds `FILES_IN`, `FILES_OUT`, `FILES_LIST`, deletion and reclaim, and refuses file-enabled specs. The 0.7.0 filesystem probe explains why.

| Probe | 0.7.0 result on Windows WHP | Contract consequence |
| --- | --- | --- |
| Read staged `/input/source.txt` | Expected bytes were readable | Read-only input passthrough works |
| Edit `/input/source.txt` | Guest received `PermissionError` | `/input` cannot implement writable input |
| Stage `/output/staged.txt` before the next execution | The next execution deleted it | Host staging cannot persist through preparation |
| Create `/output/created.txt` and collect immediately | Host read the expected bytes | Immediate output collection is possible |
| Restore a snapshot after staging | Runtime state reset and output was empty; staged input remained | Restore alone is not complete call cleanup |
| Ordinary Python exception followed by another run | First run failed normally; second run succeeded | Ordinary guest errors need no worker replacement |

The released Wasm `run_impl` calls `CapFs.prepare_for_run`, which clears output files before entering the guest; generic restore also prepares the run. This is intentional upstream behavior, not an adapter path bug. Directly removing the clear would also bypass cached quota accounting. A future writable-input mode needs an explicit upstream policy that preserves files across executions, reconciles actual host-side files against count and byte quotas, keeps restore as an explicit clear/reset operation, and tests staging, mutation, deletion, persistence and unsafe entries. A future output mode must collect before any restore because restore removes output files written after the snapshot.

The three file follow-ups are distinct: writable inputs and persistence ([#1218](https://github.com/sokolaidev/maf-extensions/issues/1218)), output collection/listing ([#1219](https://github.com/sokolaidev/maf-extensions/issues/1219)) and file cleanup ([#1220](https://github.com/sokolaidev/maf-extensions/issues/1220)). Copying inputs through a hidden prelude or using a private patched native wheel was rejected because each adds an unverified filesystem lifecycle or abandons an installable dependency set.

### File cleanup follow-up, 2026-09-22

The remaining input cleanup acceptance criteria in [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220) depend on writable staging in [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218). Output-only reset and disposal were delivered by [#1344](https://github.com/sokolaidev/maf-extensions/pull/1344), and [#1397](https://github.com/sokolaidev/maf-extensions/pull/1397) added flat listing. This audit did not enable another file channel or close either remaining issue.

PyPI still reported 0.7.0 as the latest release of the [Python SDK](https://pypi.org/project/hyperlight-sandbox/0.7.0/), [Wasm backend](https://pypi.org/project/hyperlight-sandbox-backend-wasm/0.7.0/) and [Python guest](https://pypi.org/project/hyperlight-sandbox-python-guest/0.7.0/). In that release, [`WasmSandbox::run_impl`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/v0.7.0/src/wasm_sandbox/src/lib.rs) calls `prepare_for_run` before guest execution. At upstream commit `e38f49d149111f66ee2265c6d6c216fab62d018c`, [`CapFs::prepare_for_run`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/e38f49d149111f66ee2265c6d6c216fab62d018c/src/hyperlight_sandbox/src/cap_fs.rs) still called `clear_output_files`, and the [Python native constructor](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/e38f49d149111f66ee2265c6d6c216fab62d018c/src/sdk/python/wasm_backend/src/lib.rs) exposed no preservation policy. Host-staged writable files therefore remained subject to deletion before the guest could use them.

The next step is an upstream opt-in preservation policy with quota reconciliation, exposed through the native binding and Python facade in a compatible published dependency set. Adapter adoption must distinguish execution preparation from explicit reset: reset clears staging, writable files and handles, then restores the acquired storage base. Admission must cover staging through collection, delivery and cleanup; failed reset must retire the worker, and failed disposal must retain cleanup targets for retry. `RECLAIM` and `FILES_DELETE` remain withheld until their independent reach and link-removal guarantees are established. Whole-worker disposal remains the fallback.

The audit ran against repository commit `d60ac4229ece2f42e567cada97970bf8ed873ab4` on Windows x86-64 with WHP, CPython 3.13.12 and the exact 0.7.0 trio:

| Verification | Result | Evidence boundary |
| --- | --- | --- |
| Backend, host file fixtures and offline CodeAct tests | 129 passed, 5 skipped, 13 deselected | Admission, reset, failed cleanup and retry, confinement, output conformance and failed/cancelled sink delivery; skipped probes are not passing evidence |
| Real flat-output and CodeAct delivery tests | 10 passed, 19 deselected | Binary collection before cleanup, reset and disposal, consecutive calls without stale output delivery, declared and manifest outputs, both router selection modes, with and without monitoring |

```powershell
uv run pytest -q packages/maf-sandbox-hyperlight/tests/test_hyperlight_backend.py packages/maf-sandbox-hyperlight/tests/test_hyperlight_files.py tests/test_hyperlight_codeact.py -k 'not live'
$env:MAF_HYPERLIGHT_LIVE = '1'
uv run pytest -q packages/maf-sandbox-hyperlight/tests/test_hyperlight_live.py tests/test_hyperlight_codeact.py -k 'real_flat_outputs_are_binary_and_reset_before_reuse or live_codeact_delivers_flat_binary_outputs_and_cleans'
```

These were local output-only measurements, not a CI run or proof of writable-input support. The audit did not repeat the original native input-staging probe or the Linux KVM suite. Input-enabled reset, disposal, deletion/reclaim reach and consecutive input/output CodeAct calls remain unverified until the preservation prerequisite is available.

## Conformance and environment evidence

The September 2026 output implementation uses one adapter-owned directory per sandbox and the pinned guest's flat `/output` preopen. Backend-required admission spans execution, host collection, delivery and cleanup across routers and event loops. CodeAct explicitly uses the prepared base without `makedirs`; nested outputs remain unavailable. No guest inspection code is used. Host checks reject traversal, symlinks, hardlinks, special entries and redirecting Windows reparse points, and snapshot reset clears files before reuse. This implements opt-in `FILES_OUT`; listing and writable inputs remain withheld.

Local output validation on 2026-09-20 (UTC+02:00 calendar date) passed binary round-trip, size refusal and reset cleanup against the exact 0.7.0 trio on Windows WHP and WSL2 KVM. CodeAct's real WHP and WSL2 KVM output tests passed under both router selection modes, including per-file, aggregate-byte and file-count refusal. The exec-free Linux fixture passed the core flat-output and storage-base suites; listing and writable-input/deletion reach probes were explicitly skipped because those capabilities are withheld. Windows tested a real directory junction, and Linux tested symlinks and a FIFO. The flat positive control establishes no nested-file reach. These results add output-specific evidence to the earlier runtime measurements below.

The pull-surface conformance probes do not exercise `EXEC`, so they can be reused against a runtime backend if their fixture plants files and symlinks directly on the host side of the output directory. The standard subject shells `ln` through `exec` and therefore cannot be used unchanged by Hyperlight. Six runtime probes are required: timeout enforcement, nonzero exit versus refusal, stdout/stderr separation, oversized source refusal without truncation, no shell reachability and queue-time refusal distinct from program timeout.

The source investigation measured the matched 0.4.0 stack on Windows WHP and found the micro-VM bar: the Hyper-V platform library loaded, no host filesystem was exposed, the metadata endpoint was unreachable even when allowlisted, egress defaulted to deny with a strict per-entry allowlist, and only declared guest-host channels were observable. The later 0.7.0 filesystem probe confirmed the file-specific behavior above. Linux KVM and MSHV remain separate family validations.

The shipped backend guide records the later adapter evidence: Windows WHP, WSL2/KVM and native Linux KVM suites cover stream separation, ordinary exceptions, warm state, reset, environment isolation, guest writes, timeout/cancellation worker reaping, output limits, exact-host HTTP policy, selective disposal, scope purge, CodeAct routing and lifecycle controls. Those are environment-specific measurements, not a universal claim for MSHV, AKS or arbitrary guests.

## AKS upstream basis and evidence, 2026-09-22

The selected direction was to reuse upstream's Kubernetes deployment and add the suite's session lifecycle above it. The [AKS deployment design](../backends/hyperlight.md#aks-deployment-design) records that decision and its implementation status. The parent [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230) retains the detailed Automatic and Standard probe reports; this section records what supports the design and what remains unproven.

### What upstream supplied

The source audit pinned `hyperlight-dev/hyperlight-on-kubernetes` at `fc71b4501d23977fcc54f7be144d884fc8210667`, still its default-branch head when checked. Its [architecture](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/docs/architecture.md) uses a node device-plugin DaemonSet, CDI and an extended resource. Its [Azure guide](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/docs/azure-deployment.md) provisions infrastructure, builds/pushes the plugin and deploys a sample application. Existing clusters need the reusable deployment pieces, not an unmodified provisioning script that also creates pools outside the intended KVM scope.

The [plugin manifest](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/manifests/device-plugin.yaml) runs as root with `privileged: false`, writes the kubelet device-plugin and CDI directories, and reads the host device directory. Its source registers `hyperlight.dev/hypervisor` and emits a CDI mapping with configurable UID/GID. It exposes an existing device; it does not create an Azure VM or require changing the host device's ownership or mode. Node labels are an operator/setup responsibility; the audited plugin does not perform the auto-labeling described in the architecture prose. The default 2,000 advertised allocations share the same device and establish no safe VM count or memory capacity.

The [KVM application manifest](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/k8s/deployment-kvm.yaml) requests one hypervisor allocation and runs non-root with container CPU/memory limits. Its 128 MiB limit belongs to the small [Rust sample](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/host/src/main.rs), not the Python guest. The inspected repository supplied no delegated-cgroup helper or per-worker resource/owner supervisor.

The deployment addition therefore pins upstream source and image digests, renders all placeholders, validates device permissions and uses a small overlay for node selection, resource budgets and security settings. Reusable plugin defects belong upstream; there is no independent plugin implementation in the design. The initial device count is one allocation per enabled node, increased only with measured admission and node headroom. The application explicitly disables service-account token mounting; plugin capabilities, seccomp and host-path access need their own validation under the cluster's admission policy.

### Measured Standard AKS evidence

The rerun used Kubernetes 1.35.7, Ubuntu 24.04.4, kernel 6.8.0-1067-azure, containerd 2.3.3-2, x86-64 Standard_D2ads_v5 nodes and Python 3.13.12 with the exact 0.7.0 SDK/backend/guest trio. Both nodes exposed KVM and cgroup v2; execution used one. The non-root application opened KVM, created a VM and executed Python. An otherwise matching pod without the device failed. The host KVM device's ownership and mode were unchanged.

| Probe | Result | Boundary |
|---|---|---|
| Existing live guest/Linux adapter suite | 31 passed, no skips, 108.35 seconds | Local containment revision `1aaa4612`, trusted cgroup-delegation helper; not the proposed container integration |
| Additional operator/timing/replacement checks | Five successful executions, including one repeated operator check | 36 executions overall, not 36 distinct tests |
| Pod creation to main-container start | 6–8 seconds across three raw-SDK pods | Cached base image, running node; includes a 6–7-second dependency init container; one-second timestamps |
| Full adapter acquire in an already-running delegated container | 3.61–3.88 seconds across five fresh workers | Includes process startup and baseline snapshot; excludes pod creation and delegation handshake |
| Raw SDK fresh VM plus first code | Median 109.62 ms at 25/35 MiB heap/stack; 1,179.33 ms at 400/200 MiB | 30 samples each in an initialized process; different from full adapter acquire |
| Raw SDK restore plus code | Median 2.07 ms at 25/35 MiB; 5.45 ms at 400/200 MiB | 30 samples each; separate first-restore cost |
| Default adapter worker memory | RSS 1,337.13 MiB; worker cgroup peak 1,743.84 MiB | 400/200 MiB guest configuration, 3 GiB worker limit; RSS and cgroup accounting differ |
| Full containment suite container peak | 3,154.52 MiB under a 4 GiB limit | Includes deliberate worker OOM tests; not a single idle VM's requirement |
| 404-node/669-edge CAF diagram at 50/100 MiB | RSS 171.28 MiB; container peak 221.01 MiB | DOT generation through the raw-SDK bridge; no Graphviz process in the guest |
| Same diagram with retained snapshot and five restores | Final RSS 330.19 MiB; container peak 391.32 MiB | Snapshot/reuse materially changes sizing; not adapter capacity proof |
| Delete pod while an infinite guest ran | API deletion 12.52 seconds with 10-second grace; independent observation confirmed the exact container cgroup disappeared | Healthy node/control plane; not a hard deadline or partition result |

These results support KVM feasibility and workload-specific sizing. They do not establish a production memory minimum. The first container integration must measure its own application overhead, startup, snapshot, output and failure peaks, with declared headroom and one resident VM. Copying either upstream's small Rust limit or the raw SDK diagram's steady RSS would omit costs demonstrated by the other measurements.

The Standard cluster admitted the experiment but its Audit/Warn policies flagged trusted helper authority, image registries and resource thresholds. Earlier Automatic probes required a temporary infrastructure exception. Neither result established default baseline compliance. Experiment resources and host-side artifacts were removed, original workloads remained healthy, and the configured admission policies were preserved. The custom helper established a separate per-worker containment proof; packaging it is not a prerequisite of the selected upstream-based container design.

### Additions and validation still required

[#1237](https://github.com/sokolaidev/maf-extensions/issues/1237) owns the thin upstream deployment overlay, reproducible images and device lifecycle. Its remaining checks include plugin/kubelet restarts, stale CDI, unusable-device health, eligible node images, admission requirements and teardown. MSHV remains outside the first deployment.

[#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) owns explicit container containment and one authenticated session per pod. The application stays with its local adapter; the host's pod controller retains ownership and cleanup state outside the failing pod. This needs a distinct lifecycle implementation and protocol error mapping. Removing the current cgroup checks would not implement it. The earlier remote-worker proposal [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236) was closed as not planned and is not a delivered dependency or reopened by this design.

The validation must cover actual Python `RUN_CODE`/`SNAPSHOT` and CodeAct reset; pre-submission queue expiry versus active cancellation; infinite guest and native hang; worker/owner OOM and abrupt death; verified whole-pod retirement; no surviving worker or stale state on replacement; cross-user rejection; exact-instance disposal and retryable failed purge; output delivery/cleanup; and `CLOSED`/`ALLOWLIST` behavior. Pod deletion, node drain/reboot, controller restart, API outage and network partition need explicit evidence. If shutdown cannot be confirmed, the safe result is pending cleanup with replacement refused, not successful disposal. Generic distributed owner routing/purge remains conditional work in [#1239](https://github.com/sokolaidev/maf-extensions/issues/1239).

The initial design update delivered no container-containment implementation or new live probe. The following implementation and measurements supersede that status; they do not replace the historical delegated-cgroup evidence above.

### Explicit scoped-pod implementation and probe, 2026-09-23

The implementation adds `HyperlightPodConfig`, a non-root namespace PID 1 supervisor, `HyperlightPodController`, an image builder and a thin overlay of the pinned upstream plugin. One `(scope, thread_id, agent_id, kind)` owns each pod. A tool call does not allocate a new pod unless the host deliberately chooses that lifetime. The application and adapter remain colocated; there is no remote execution API, node cgroup helper or silent weakening of local containment. The [deployment instructions](../../../images/hyperlight-sandbox/README.md) describe the operational contract.

The Standard AKS probe used the same Kubernetes, Ubuntu, kernel, containerd, node family, Python and pinned 0.7.0 trio listed above. The upstream image was `ghcr.io/hyperlight-dev/hyperlight-device-plugin:fc71b45@sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98`, with one advertised allocation on one verified node. Application pods passed Restricted admission with no host mounts, capabilities, privilege escalation or service-account token. The actual container had finite CPU/memory/PID controls and swap disabled. The cluster still warned about public registries and root infrastructure; this is not blanket Microsoft-baseline compliance. No policy or RBAC exception was added for this run.

A prebuilt application image was built locally. Live pods used the documented registry-free path: a pinned Python base, immutable digest-verified wheel bundle and hash-locked dependency init container. Consequently, startup numbers include dependency installation and do not measure a prebuilt application's cold image pull.

| Live check | Result |
|---|---|
| Guest execution and snapshot reset | Passed; globals cleared, instance changed, ordinary exceptions allowed reuse |
| Ownership and admission | Cross-scope/thread/agent and changed policy refused; stale-instance disposal preserved the current worker; queued expiry preserved it |
| CodeAct | Fixed and per-spec routes passed with reset between calls |
| Files and limits | Binary delivery, listing, bounded reads and reset cleanup passed; oversized source refused before execution; oversized native output retired the pod |
| Network | CLOSED refused HTTP; ALLOWLIST reached only the named loopback host and survived reset |
| Active failure | Infinite guest timeout, cancellation, stopped native worker, worker SIGKILL and owner SIGKILL all retired the pod with exit 70 |
| Aggregate OOM | Host allocation pressure with a resident VM reached the 4 GiB limit; runtime reported `OOMKilled`, exit 137; cleanup confirmed termination |
| Controller death | Killing the controller during active execution retired the workload without first requesting pod deletion; replacement admission was refused while its reservation existed |
| Recovery | Live cleanup remained pending; a fresh controller confirmed termination and released the reservation in 11.07 seconds after controller death |
| Same-scope replacement | After confirmed cleanup, the recovered scope received a different pod UID and passed the positive guest/reset checks |
| Device-plugin restart | Replacement plugin became ready and a fresh positive workload passed |

These are 12 application modes plus the controller-death/recovery experiment, not 13 adapter conformance tests. Every selected mode used a real KVM guest; expected failure modes passed by producing the required retirement and exact-UID runtime termination. No skipped test is counted as a live pass. Init and attach startup defects found during the work were corrected and rerun. Failed initialization cleanup has a distinct runtime-proof path.

| Measurement | Observed value | Scope |
|---|---|---|
| Pod creation to main-container start | 12–13 seconds across the 12 selected modes | Cached Python image; includes 10–12 seconds of dependency bootstrap; one-second Kubernetes timestamps |
| Full adapter acquisition | 2.71 seconds in the final positive run; 2.65–3.52 seconds across measured modes | Running container; worker, default 400/200 MiB heap/stack, baseline snapshot and controller acknowledgement |
| First trivial code after acquisition | 52.8 ms in the final positive run | Includes remote lifecycle acknowledgement; not raw SDK latency |
| Snapshot reset | 207.1 ms in that run | Includes controller transport; not a warmed in-process SDK benchmark |
| Worker RSS | 1,130.13 MiB | One resident VM after execution and reset |
| Application RSS | 34.93 MiB | Owning Python process, excluding the worker and PID 1 |
| Main-container memory | Current 1,163.82 MiB; peak 1,833.93 MiB | Cgroup accounting, not RSS; 4 GiB container limit |

This measured small program leaves roughly 2.2 GiB below the chosen ceiling, not a production sizing guarantee. Larger application state, output buffers, dependency imports and workloads still require their own peak measurements. Controller API calls and verified deletion added substantial time to complete probe runs; the 32–37-second end-to-end durations are not VM startup times.

Teardown removed the application pods and reservations, test namespaces, plugin DaemonSet and exact test-created CDI/socket artifacts. Guarded node cleanup verified the host KVM ownership and mode were unchanged. Original node labels were restored; all 34 baseline pods retained their UIDs and restart counts. Admission policy and cluster RBAC were unchanged.

Remaining acceptance includes kubelet restart, node drain/reboot or reimage, API outage and network partition, unusable-device health, registry/provenance policy, production networking, and application-specific sizing. Runtime termination is the live cleanup proof; no independent host process/cgroup observer or production node fencing service was added. Synthetic terminal states and missing pods without a saved receipt retain ownership in unit tests, but those tests do not substitute for a live partition exercise. Individual worker-only OOM and every existing local file/egress conformance variant were not rerun under this mode. [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230), [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237) and [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) remain open for that work; generic distributed routing remains [#1239](https://github.com/sokolaidev/maf-extensions/issues/1239).

### Failure-recovery follow-up, 2026-09-23

The merged implementation from [#1406](https://github.com/sokolaidev/maf-extensions/pull/1406) was exercised on one temporary, tainted Standard AKS user node, separate from the existing system pool. The measured platform was `Standard_D4ads_v5`, Kubernetes 1.35.7, Ubuntu 24.04.5, node image `AKSUbuntu-2404gen2containerd-202609.09.0`, kernel `6.8.0-1067-azure`, containerd `2.3.3-2` and runc `1.4.3-2`. Application code used Hyperlight 0.5.0 workspace wheels, the locked dependencies and unchanged 0.7.0 SDK/backend/guest trio. A later bundle included the controller recovery fix described below. Guest code and the runtime containment implementation were unchanged.

Application pods retained Restricted admission and the default one-core/4 GiB limits and 400/200 MiB guest heap/stack. The default device count stayed one. A temporary privileged diagnostic pod on the disposable node observed exact-pod processes and cgroups and injected the selected faults. It is test instrumentation, not a deployed containment dependency or production fencing service. Both API interruption tests used real network connections; neither stopped the managed control plane.

| Live check | Observed outcome |
|---|---|
| Kubelet stop/restart | Cleanup remained pending and duplicate ownership was refused while kubelet was unavailable. The first bounded recovery attempt expired; retry after node recovery confirmed exit 70 and allowed a fresh same-scope pod. The plugin re-registered. |
| Controller-to-API connection loss | A local transparent TCP relay dropped the controller's real TLS connections and rejected reconnects. The workload stopped, its ledger remained, and another owner was refused. Independent observation found no survivors within 10.10 seconds, an observation bound rather than a shutdown deadline. Restored connectivity allowed recovery and fresh execution. |
| Node-to-API interruption | A temporary, automatically removed node-local rule blocked the API endpoint for 70 seconds. Deletion stayed unconfirmed and a duplicate owner was refused. Restoring traffic allowed termination proof, cleanup and fresh execution. |
| Drain | Eviction of the active probe pod and drain completed in 19.28 seconds. The node was uncordoned, cleanup completed and a fresh same-scope pod succeeded. The drain selected only the owned application pod. |
| Reboot | The test node returned Ready with a different boot ID after 48.09 seconds. Cleanup initially stayed pending; a normal retry confirmed exit 70, released ownership and permitted a fresh pod. This measured a graceful node reboot, not reimage or permanent node loss. |
| Default OOM | Memory pressure reached the declared container budget. The measured `memory.oom.group=1` killed the process group; runtime exit was 137 and replacement succeeded. This is aggregate OOM evidence. |
| Injected worker-only OOM | For one disposable pod only, instrumentation set `memory.oom.group=0` and protected the owner/supervisor from OOM selection. The kernel identified the exact native worker as the OOM victim. PID 1 detected the OOM and retired the pod with exit 70; fresh replacement succeeded. These fault-only overrides are not recommended deployment settings. |
| No device request | An otherwise matching pod recorded `/dev/kvm` absent, failed before guest initialization and retired with exit 70. Its ownership reservation was released. |
| Missing/stale CDI | Removing the owned CDI file or replacing its device name prevented container creation while the plugin still advertised one allocation. Startup timed out, never-started cleanup released ownership, and restoring the exact spec allowed a fresh successful pod in each case. This did not demonstrate automatic CDI repair. |

For every retirement row above, the independent node observer found no processes or cgroup belonging to the original pod before the same ownership scope received a different pod UID. The observer established this during healthy-node recovery; it does not supply automatic fencing when a node remains unreachable. No source was replayed automatically.

The first controller-connection-loss probe exposed an error-mapping defect: a failed ledger read escaped as a raw subprocess error even though ownership remained reserved. The initial fix mapped failed ownership lookup and termination-receipt persistence to `HyperlightPodCleanupPending`. Eight unit regression cases failed before that fix and passed afterward, retaining the ledger/finalizer until a successful retry. The real connection-loss probe then passed with the corrected controller. That initial full local gate passed with 11,321 tests and 598 platform/live skips.

The response-validation follow-up in [#1411](https://github.com/sokolaidev/maf-extensions/pull/1411) covered non-object API responses during ownership lookup, termination polling, receipt persistence, rejected-allocation cleanup and final cleanup, while preserving explicit pending-cleanup decisions. Twelve additional unit cases failed before that fix and passed afterward, including `[]` and `null` through the real API decoder at all five boundaries. The combined regression coverage totaled 20 cases; all 133 pod lifecycle tests and the final local gate of 11,333 tests passed, with 598 platform/live skips. AKS probes were not rerun for this follow-up; neither its unit tests nor the skipped tests are live AKS passes.

The fresh positive control measured acquisition at 2.477 seconds, first code at 51.7 ms and reset at 211.0 ms. Worker RSS was 1,129.91 MiB, application RSS 34.93 MiB, and aggregate container peak 1,833.89 MiB under 4 GiB. The simple probe left 55.2% of that limit unused; this does not establish a production memory minimum or justify increasing device count. Bootstrap still installed a digest-verified bundle, so these results do not measure a prebuilt image's cold pull.

Deployment acceptance remains open. A GitHub provenance verification for the pinned upstream plugin digest returned HTTP 404; no attestation was verified. The [pinned upstream Dockerfile](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/Dockerfile) uses floating builder/runtime tags, and the [plugin](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/main.go) checks device-path existence rather than successful VM creation. An approved image registry, provenance policy, production networking, application-specific peak measurements, node reimage/permanent loss, and the remaining file/egress variants still require acceptance. The probe added no policy or RBAC exception. This evidence does not establish AKS Automatic admission or blanket Microsoft-baseline compliance.

Teardown removed every application pod and ownership reservation, both probe namespaces, the upstream plugin, diagnostic pod and temporary node pool. The test VM scale set was absent afterward. The cluster returned to its original two nodes and 34 pods, with unchanged original pod UIDs/restart counts, node labels and cluster identity/security/network/admission settings. Host KVM ownership and mode were unchanged before deleting the test node.

### Platform matrix, 2026-09-25

[#1425](https://github.com/sokolaidev/maf-extensions/issues/1425) asked which node platforms the pod integration supports and how an unsupported node fails. Two temporary one-node `Standard_D4ads_v5` user pools were added to the Standard cluster with the eligibility labels set on the pool, at Kubernetes 1.35.7 and the default security type:

| Pool | Node image | Kernel | containerd | runc | Host `/dev/kvm` |
|---|---|---|---|---|---|
| Ubuntu 24.04.5 | `AKSUbuntu-2404gen2containerd-202609.15.0` | `6.8.0-1067-azure` | 2.3.3-2 | 1.4.3-2 | `0660` root:993 |
| Azure Linux 3.0 | `AKSAzureLinux-V3gen2-202609.15.0` | `6.6.150.1-1.azl3` | 2.2.4 | 1.3.6 | `0666` root:32 |

The pinned plugin advertised one allocation on each node. The pods ran a bundle built from the change under test, with the unchanged 0.7.0 SDK/backend/guest trio. Running every probe mode as two concurrent scopes put each mode on both nodes. `positive`, both CodeAct modes, `files` and `allowlist` exited 0 on both. `timeout`, `cancel`, `owner-death`, `output-limit`, `worker-death` and `native-hang` retired the pod with exit 70, and `oom` ended with the aggregate OOM exit 137, on both. Acquisition took 2.46 to 2.67 seconds, reset about 0.2 seconds, and the container memory peak was about 1.92 GB under the 4 GiB limit on both. PID 1 observed swap 0, `cpu.max` of one core and `pids.max` near 19,150; the cluster sets no per-pod PID limit, so that value is the node default.

Three negatives exercised the new refusals without changing a node:

| Case | Observed outcome |
|---|---|
| Both allocations held by other scopes | `supervise` with a 40-second startup budget raised `TimeoutError` after 56.8 seconds, naming `Unschedulable` and `2 Insufficient hyperlight.dev/hypervisor`. Its pod and reservation were removed. |
| Plugin overlay rendering CDI owner 1000:1000 | On Ubuntu, PID 1 refused before starting the application: `supervise` raised `HyperlightPodPlatformError` with `KVM initialization failed ... (Permission denied)` after 38 seconds and confirmed cleanup. On Azure Linux the pod ran, because the host device is world-writable and the CDI owner does not gate it. Restoring 65534:65534 and restarting the plugin pods restored success on both. |
| No `/dev/kvm` (local Docker Desktop, same image) | PID 1 exited 78 with `(No such file or directory)`. |

A node without nested virtualization was not measured. The subscription offers only Bsv2 among B-series sizes in the region and has no Bsv2 quota; Bsv2 is [documented](https://learn.microsoft.com/azure/virtual-machines/sizes/general-purpose/bsv2-series#feature-support) without nested virtualization, so the plugin would advertise nothing there and scheduling would refuse the pod. The `nodes` report marked both test nodes verified. Trusted Launch and confidential VM pools, other VM families and Kubernetes 1.36 were not measured.

A second pair of pools on 2026-09-26, the same sizes and node images, measured what the first run left out. Created with `hyperlight.dev/enabled=true` only, both nodes ran the plugin and advertised one allocation. The `nodes` report marked them verified and not schedulable. `chroot /host runc --version` in a `sysadmin` debug pod read runc 1.4.3-2 on Ubuntu and 1.3.6 on Azure Linux; the runc column above comes from these nodes, not the first pair. `az aks nodepool update --labels` then added `hyperlight.dev/hypervisor=kvm` to the existing nodes in place: the same node objects and plugin pods remained. `positive` exited 0 on both nodes. Both pools were deleted afterwards.

### Upgrade and rollback, 2026-09-26 to 27

[#1426](https://github.com/sokolaidev/maf-extensions/issues/1426) exercised the upgrade and rollback procedure on temporary one-node `Standard_D4ads_v5` pools from the Ubuntu row above. The runtime moved between 0.5.0 and 0.6.0, each image run with its own release's controller and pulled by digest. The plugin moved between upstream `51d7dab` and the pinned `fc71b45`. Each run's record is on its issue: [runtime](https://github.com/sokolaidev/maf-extensions/issues/1512#issuecomment-5854856249), [plugin](https://github.com/sokolaidev/maf-extensions/issues/1513#issuecomment-5850250572) and [failures and interrupted maintenance](https://github.com/sokolaidev/maf-extensions/issues/1514#issuecomment-5850391223).

Ownership held in every case. A scope still held or awaiting cleanup refused a second owner, eviction by `kubectl drain --force` retired owners and the controller confirmed their cleanup, and `recover` completed a scope whose controller was killed. A reservation left by either runtime release was recovered by the other. Three things the procedure had assumed did not hold: `drain` needs `--force` for the controller's bare pods, a cordoned node refuses the controller's pods so guest execution can only be checked after uncordoning, and a ready plugin pod can advertise no allocation for several seconds. Three failures lose their reason on the way to the host: a mismatched controller and image ([#1510](https://github.com/sokolaidev/maf-extensions/issues/1510)), a bundle pod whose image does not pull ([#1521](https://github.com/sokolaidev/maf-extensions/issues/1521)), and a second owner on a reserved scope ([#1522](https://github.com/sokolaidev/maf-extensions/issues/1522)). The [runbook](../../../images/hyperlight-sandbox/README.md#upgrade-and-rollback) now follows the measured order.

A follow-up on 2026-09-28 swapped the plugin to `fc71b45` and back while a scope's cleanup was pending, its controller killed and its pod held by the finalizer ([record](https://github.com/sokolaidev/maf-extensions/issues/1514#issuecomment-5877072624)). The drain waited on that finalizer until its own timeout, the plugin was replaced regardless, and the reservation held across both swaps: a second owner was refused with `HyperlightPodReserved` each time. `recover` then returned `controller stream closed` and the scope accepted a new owner. After one replacement, allocation read 1, dropped to 0 and returned within about 20 seconds.

## Azure Container Apps

### Source feasibility audit

Standard managed ACA Linux application containers have no documented mechanism to provide `/dev/kvm` or `/dev/mshv` to an application container. The inspected stable and preview Container Apps schemas expose no application device mapping, CDI selection, Kubernetes extended-resource request, `hostPath`, runtime-class or security-context facility for this use. ACA also documents that customers do not receive the underlying Kubernetes APIs, and privileged host-level access is not part of the application contract.

Consumption, Dedicated, the inspected Flexible preview and confidential-compute profiles change capacity or tenancy but do not document a Hyperlight device facility. GPU fields are evidence for GPU provisioning only, not a general device-resource map. ACA managed sessions/sandboxes are separate service interfaces, not a way to inject a hypervisor into an ordinary application container.

This is a deployment conclusion, not a claim that every ACA node lacks virtualization hardware or that an undocumented experiment could never run. Revisit only when Microsoft documents the device, eligible profiles and regions, permissions and runtime constraints. A Linux adapter cannot create the missing ACA device access.

### Live Consumption and Dedicated probe

The live probe used standard managed ACA applications in Sweden Central on 2026-09-14, with one 1-vCPU/2-GiB container, no ingress, one replica and a 30-second termination grace period. It tested both the default Consumption profile and Dedicated D4, as root and UID 65534, using the exact 0.7.0 SDK trio, Wasm/Python guest, 400 MiB heap, 200 MiB stack and `HYPERLIGHT_MAX_SURROGATES=0`.

| Check | Consumption | Dedicated D4 |
| --- | --- | --- |
| CPU flags | `vmx=false`, `svm=false`, `hypervisor=true` | `vmx=false`, `svm=true`, `hypervisor=true` |
| `/dev/kvm` and `/dev/mshv` | `ENOENT` for both identities | `ENOENT` for both identities |
| SDK imports, cache materialization and `Sandbox` construction | Succeeded | Succeeded |
| First `Sandbox.run()` | `No Hypervisor was found for Sandbox` | Same |
| Snapshot/restore and guest execution | Not reached | Not reached |
| Security fields | No effective capabilities, `NoNewPrivs=1`, `Seccomp=2` for non-root | Same |

Both applications deployed and ran the diagnostic process. Exit zero means the process collected observations, not that Hyperlight executed. Dedicated exposing `svm` while still lacking the device demonstrates that CPU flags alone do not grant the required facility. Other regions, profiles, MSHV and future infrastructure were not tested.

The result reinforces the decision: no Hyperlight guest or adapter conformance passed on ACA. The source audit and live probe together support the same conclusion without turning a negative test into a universal platform claim.

## Separate worker alternative

An ACA application can call an authenticated Hyperlight worker service on a suitable dedicated host. This is not local backend execution inside ACA and requires its own transport, authorization, owner routing, cancellation, cleanup and state-loss semantics.

The smallest viable prototype keeps kinds on the existing core protocol and exposes one remote `RUN_CODE`/`SNAPSHOT` owner. It must derive authority from a service-scoped identity and reject cross-scope acquire, execution and purge; preserve the complete sandbox key, owner generation and instance ID; carry deadlines without resetting the budget at each hop; terminate native work on cancellation; avoid replaying ambiguous execution; fence replaced owners; and report lost state explicitly. File channels, native host tools, multiple workers, automatic failover and VM-state migration remain outside it.

A Windows worker can use the validated WHP family. A Linux worker depends on real KVM validation, not a VM SKU name. The separate worker proposal was tracked by [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236) and is not an implementation of direct ACA hosting.

## Remaining limits

- The backend is validated only for the pinned Python guest/Wasm family on the measured Windows WHP and Linux KVM environments. MSHV, AKS, other architectures, custom guests and Hyperlight-JS need their own declarations and evidence.
- `RUN_CODE` timeout and cancellation require a killable process boundary; restore cannot reach a stuck thread. Memory and returned-output limits must be enforced before native aborts become uncatchable.
- Filesystem channels remain withheld until upstream persistence, quota reconciliation, collection order, safe cleanup and conformance are complete.
- Native host tools remain withheld until the transport is integrated with core caps, deadlines, JSON response limits, identity and approval policy.
- Resolved-IP filtering, path rules and attached identity are not current Hyperlight claims.
- Direct ACA hosting remains unsupported by the inspected platform contract and measured profiles. The separate-worker route is a new remote integration, not a backend configuration switch.
- The pinned Hyperlight distributions and guest artifacts have licenses and notices independent of the Python package; dependency upgrades must retain the exact matched-version and conformance discipline.

## Upstream requests drafted, 2026-09-25

Three requests this path waits on are drafted in [`upstream-hyperlight-requests.md`](upstream-hyperlight-requests.md) and filed the same day as [hyperlight-dev/hyperlight-sandbox#227](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/227), [hyperlight-dev/hyperlight-sandbox#228](https://github.com/hyperlight-dev/hyperlight-sandbox/issues/228) and [hyperlight-dev/hyperlight-on-kubernetes#15](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/issues/15), all open: the preservation policy [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) needs from `hyperlight-sandbox`, eager validation of `allow_domain` method tokens in the same SDK's lazy path, which the [#377](https://github.com/sokolaidev/maf-extensions/issues/377) measurement found missing, and digest-pinned base images with build provenance for the device-plugin image [#1424](https://github.com/sokolaidev/maf-extensions/issues/1424) has to admit.

## HTTPS method conformance through the adapter, 2026-09-27

The [#1509](https://github.com/sokolaidev/maf-extensions/issues/1509) probe used the real `HyperlightSandboxBackend`, its supervised worker and raw guest wasi-http requests. Each run created a loopback recording server behind a temporary Cloudflare Quick Tunnel, with a random path and synthetic request data only. TLS terminated at the relay's publicly trusted HTTPS endpoint on port 443. The unchanged SDK validated its certificate using its bundled public roots; no custom root or certificate-validation bypass was used. The relay forwarded requests to the recording server, so recorded framing describes that final hop rather than the original TLS wire.

| Measured host | Host Python | Result |
| --- | --- | --- |
| Windows 11 x86-64, build 26220, WHP | 3.13.12 | HTTPS matrix and body probes passed |
| Ubuntu 24.04 under WSL2, x86-64, kernel 6.18.40.1, KVM | 3.13.15 | HTTPS matrix and body probes passed |
| GitHub-hosted Ubuntu 24.04.5, x86-64, kernel 6.17.0-1022-azure, KVM | 3.13.15 | HTTPS matrix and body probes passed |

All runs used workspace core 0.44.0 and Hyperlight adapter 0.6.0, the exactly pinned `hyperlight-sandbox`, `hyperlight-sandbox-backend-wasm` and `hyperlight-sandbox-python-guest` 0.7.0 trio, and SHA-256-verified cloudflared 2026.9.3. The local Linux helper ran the test as an unprivileged user in a temporary delegated cgroup without changing device permissions. The [hosted KVM job](https://github.com/sokolaidev/maf-extensions/actions/runs/36276244905/job/108499472847) on `94407dd4` passed the HTTPS case, all 16 existing live backend tests, 14 CodeAct delivery tests and the CodeAct sample. Its runner used the existing KVM/device and port preparation steps. These measurements do not add AKS or MSHV qualification.

All seven declared methods first succeeded under an unrestricted-method host rule. Each singleton rule then admitted its named method and refused the other six: seven positive and 42 negative cases, checked against both raw guest results and recording-server logs. The shared GET/POST conformance probe also passed. TRACE, CONNECT and two custom methods were refused from the guest under the unrestricted-method rule. Separate offline cases pinned router and direct-acquisition refusal for unsupported policy tokens before worker construction. TLS and connection errors raise harness failures rather than count as method denials. A Windows mutation run changed only the worker's `allow_domain` call to drop `methods`: the same HTTPS test failed because HEAD received 200 under the GET-only rule. The worker source was then restored byte-for-byte.

Under GET-only policy, a GET carrying an explicit `Content-Length` delivered all 10,259 synthetic body bytes and its query to the recorder. A POST control delivered the same bytes. A GET with automatic framing completed with 200 but delivered no body bytes in this relay setup. That last observation does not identify where the bytes were omitted, and the relay may change framing. The recording parser separately passed real HTTP tests with content-length and chunked GET bodies, including chunk extensions and trailers. The explicit-length HTTPS result establishes that GET-only does not prevent outbound request bodies or provide confidentiality.

Workers were disposed and reaped, pipe-draining threads stopped, and the recording server and tunnel were closed after each run. The Linux helper also removed its temporary cgroup. Startup depends on the public relay and DNS: one development run timed out before readiness, so this network-dependent probe has a separate explicit opt-in and does not run in the ordinary offline gate.

### Reproducing the HTTPS probe

Download the [cloudflared 2026.9.3 release](https://github.com/cloudflare/cloudflared/releases/tag/2026.9.3) binary for the measured host. The fixture verifies its SHA-256 before starting it: Windows amd64 `f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2`, Linux amd64 `77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2`. The probe publishes only its temporary recording fixture; it needs no Cloudflare account or repository credentials.

On a WHP-capable Windows host, set `MAF_HYPERLIGHT_LIVE=1`, `MAF_HYPERLIGHT_HTTPS_LIVE=1` and `MAF_HYPERLIGHT_CLOUDFLARED` to the downloaded executable, then run:

```powershell
uv run pytest -q -s packages/maf-sandbox-hyperlight/tests/test_hyperlight_https_live.py
```

On a prepared Linux KVM host, use the existing cgroup helper with those environment settings:

```bash
sudo env MAF_HYPERLIGHT_HTTPS_LIVE=1 MAF_HYPERLIGHT_CLOUDFLARED="$CLOUDFLARED" python3 scripts/check_hyperlight_linux.py --live --python "$PWD/.venv/bin/python" -- -q -s packages/maf-sandbox-hyperlight/tests/test_hyperlight_https_live.py
```

The Tests workflow exposes the same Linux run through its `hyperlight_https` dispatch input, disabled by default. Its download is pinned and verified, and the test prints `HTTPS_METHOD_EVIDENCE` with platform, dependency versions, matrix counts, body observations and completed cleanup. Both opt-ins are required for the HTTPS test; the existing offline and loopback HTTP suites retain their original behavior.

## Core/exec migration and native publication check, 2026-10-02

The first implementation, [#1664](https://github.com/sokolaidev/maf-extensions/pull/1664), defines `ProgramRequirements`, `ProgramChannel`, the live `HostToolPolicy` and explicit initial-acquisition `SandboxBackendUnavailable`. CodeAct delegates automatic execution to the retained backend channel; Docker, ACAS and Docker Sandboxes declare the exec host-tool channel, and WSLC declares its program-only variant. Program budgets are independent of shared files. `HostToolRun.call` requires trusted publication before confirmed delivery accounting. The [migration guide](../program-channels.md) describes the implemented interface and its limits.

A local Docker `python:3.13-slim` conformance run verified the portable profile, two fresh policy runs on one guest, shared API values/refusals, confirmed response-file publication and revoked completed authority. This is Docker channel evidence, not qualification of ACAS, WSLC or Docker Sandboxes infrastructure.

The pinned 0.7.0 Hyperlight stack was rerun locally on Windows/WHP for `shared-api`, `callback-timeout` and `handoff-failure`. All three matched the harness expectations and workers were reaped. Prepared native replies now remain reserved until teardown, which records `delivery_uncertain` and zero confirmed bytes because the SDK provides no post-serialization acceptance hook. This replaces the harness's earlier accounting assumption, not the historical boundary observations above. No production native channel or full native conformance is claimed. Native acceptance and oversized-request containment remain prerequisites for the second implementation PR and for closing #369.
