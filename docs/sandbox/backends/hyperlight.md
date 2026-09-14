# Hyperlight

> The packaged Python guest on Windows WHP, with a killable worker for each sandbox and no file channels.

[`maf-sandbox-hyperlight`](../../../packages/maf-sandbox-hyperlight/) implements `SandboxBackend` directly over the Hyperlight Python SDK. The [package README](../../../packages/maf-sandbox-hyperlight/README.md) owns installation, configuration and usage. Kinds continue to use the core protocol; CodeAct opts into `CodeactRuntime` with the backend's `RUNTIME_INSTRUCTIONS`.

## Declarations and supported family

| Axis | Contract |
| --- | --- |
| Isolation | `MICROVM`, only the packaged Python guest / Wasm backend / Windows x86-64 WHP family |
| Capabilities | `RUN_CODE`, `SNAPSHOT` |
| Egress | `CLOSED`, exact-host `ALLOWLIST`; HTTP 80 and HTTPS 443, no method or identity refinements |
| Guest OS | No OS family declared; this is a language runtime |
| Isolation scope | `CONVERSATION`; the complete key, including `call_id`, still identifies storage in the registry |
| Identity and observation | No attached platform identity; no egress observation claim |
| File capabilities | `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM` withheld; protocol methods refuse |
| Other channels | `EXEC`, `HOST_TOOLS`, `EGRESS_METHODS`, `ATTACHED_IDENTITY` withheld |

The exact 0.7.0 SDK, Wasm backend and Python guest are pinned together. The adapter accepts no custom guest, image or guest working directory, and refuses acquire on unvalidated platforms. Construction starts no worker. Each worker retains `WinHvPlatform.dll` and uses the pinned host's single-VM mode, `HYPERLIGHT_MAX_SURROGATES=0`. Default surrogate-manager startup failed in the investigation; this adapter does not depend on it.

The bundled runtime is CPython 3.14 with a reduced standard library. It supplies Python statement execution, separate stdout/stderr, persistent globals and snapshot restore. In the pinned guest, `json`, `math` and `re` are available, while `datetime`, `statistics`, `pickle` and `__future__` are not. The runtime instructions expose this limitation instead of implying desktop Python compatibility.

## Execution and cleanup

A worker owns all PyO3/native objects on its main thread. Acquire starts the process, adds it to a parent-owned Windows job, initializes the guest, runs its warm preparation and records the baseline snapshot. No program is admitted before that completes. The process environment excludes application credentials, and neither input nor output directories are configured in the SDK.

One host process owns Hyperlight on a machine, enforced by a Windows named event held for the host's lifetime. All backend objects in that process share a key/kind registry. Another process refuses acquire and reports disposal as unclean, so a delete routed to the wrong process cannot claim success. The serving host must receive requests and purges; this implementation does not support a multi-process or multi-machine deployment behind one logical backend. A Windows job kills each worker tree on host exit, so registry loss does not leave running guests to rediscover.

Admission uses locks independent of an asyncio event loop. A sandbox serializes run and reset; the backend serializes acquire and disposal. `run_code` includes queue time in its deadline. Expiry before submission is `SandboxQueuedTimeout` and preserves the running worker. Expiry after submission terminates it and raises `TimeoutError`; cancellation likewise terminates and reaps it before propagating. Cleanup has a separate bounded allowance and uses its own thread so it cannot queue behind blocked pipe readers. Native result buffering is bounded by the job's committed-memory limit, with a second byte limit on returned stdout/stderr and a separate 64 KiB retained diagnostic limit.

Reset restores the original warmed baseline and changes `instance_id` only after success. An ordinary guest exception returns a failed `ExecResult` and permits reuse; a transport/native failure retires the worker. Policy changes require disposal instead of reusing a VM under a different allowlist or execution contract. Exact-instance disposal ignores stale IDs, preserves other kinds and agents, and retains failed targets for retry. `dispose_scope` reaches every matching target in the owner's shared registry. Cancellation waits for the active worker's bounded cleanup attempt, then propagates without starting another target; unreported targets remain registered for retry. CodeAct's exclusive admission covers execution and cleanup, and `Cleanup.RESET` selects warm reset between its calls.

## Egress boundary

Hyperlight's native [network policy](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/hyperlight_sandbox/src/network.rs) checks each request's scheme, host, port and path. The adapter translates each exact core hostname into HTTP and HTTPS root permissions. Wildcards, non-default ports and narrowed method/authority requests are unsupported. The pinned native implementation always blocks CONNECT and TRACE. It exposes HTTP helpers, not raw guest sockets.

HTTP runs on the host network; allowing an internal name or loopback grants access there. Hostname policy does not filter resolved IP addresses. No credentials or proxy settings are forwarded from the application environment, and the adapter attaches no identity. The host remains responsible for which destinations it authorizes.

## Evidence

On 2026-09-13, Windows 11 AMD64 / WHP / host CPython 3.13 and the exact 0.7.0 trio ran the new adapter's guest suite. It covered stream separation, ordinary exceptions and warm state, reset of globals and builtins, absent host environment values, refused guest writes, infinite-loop timeout/cancellation with worker reaping, queue exhaustion without submission, output limits, CLOSED/exact-host HTTP enforcement before and after reset, selective instance disposal, scope purge and CodeAct with both fixed and per-spec routing. Separate Windows subprocess tests exercise committed-memory limits and abrupt owner exit. Portable tests cover failure retention/retry and blocked stdin/stdout/stderr pipes. These are local WHP results; CI's ordinary suite does not validate a hypervisor.

The [earlier proposal](../research/hyperlight-backend-proposal.md) and [exploration](../research/hyperlight-backend-exploration.md) preserve the historical investigation. Their proposed file capabilities are not declarations of this adapter. Optional file work must establish its own conformance and cleanup before any file capability is enabled.

The [AKS investigation](../research/hyperlight-aks-integration.md) records the device-plugin/CDI audit and a pinned Python SDK proof. The [AKS Automatic verification](../research/hyperlight-aks-automatic-verification.md) measures non-root wheel loading, guest-cache materialization and aggregate resource restrictions on Azure Linux. Its baseline admission policy refuses the plugin's host paths; device injection, KVM/MSHV guest execution and adapter lifecycle conformance remain unmeasured. Container worker containment gates AKS adapter support; replicated deployments additionally require owner routing and fencing.

## Status

| Item | State | Tracking |
| --- | --- | --- |
| Initial runtime and reset backend | implemented with Windows WHP validation; umbrella remains open for the independent channels | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open); initial runtime delivered by [#1223](https://github.com/sokolaidev/maf-extensions/pull/1223) (merged) |
| AKS hosting | local and AKS packaging measured; Automatic baseline policy blocks plugin admission; device, guest and adapter validation remain open | [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230) (open); manifests [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237) (open), container containment [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) (open), replica ownership [#1239](https://github.com/sokolaidev/maf-extensions/issues/1239) (open) |
| Optional writable inputs | open | [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) (open) |
| Optional output collection/listing | open | [#1219](https://github.com/sokolaidev/maf-extensions/issues/1219) (open) |
| Optional file cleanup | open | [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220) (open) |
| Native host tools | open | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
