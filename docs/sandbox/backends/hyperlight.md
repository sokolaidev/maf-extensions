# Hyperlight

> The packaged Python guest on Windows WHP and Linux KVM, with a killable worker for each sandbox and no file channels.

[`maf-sandbox-hyperlight`](../../../packages/maf-sandbox-hyperlight/) implements `SandboxBackend` directly over the Hyperlight Python SDK. The [package README](../../../packages/maf-sandbox-hyperlight/README.md) owns installation, configuration and usage. Kinds continue to use the core protocol; CodeAct opts into `CodeactRuntime` with the backend's `RUNTIME_INSTRUCTIONS`.

## Declarations and supported family

| Axis | Contract |
| --- | --- |
| Isolation | `MICROVM`, only the packaged Python guest / Wasm backend on x86-64 Windows WHP or Linux KVM |
| Capabilities | `RUN_CODE`, `SNAPSHOT` |
| Egress | `CLOSED`, exact-host `ALLOWLIST`; HTTP 80 and HTTPS 443, no method or identity refinements |
| Guest OS | No OS family declared; this is a language runtime |
| Isolation scope | `CONVERSATION`; the complete key, including `call_id`, still identifies storage in the registry |
| Identity and observation | No attached platform identity; no egress observation claim |
| File capabilities | `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM` withheld; protocol methods refuse |
| Other channels | `EXEC`, `HOST_TOOLS`, `EGRESS_METHODS`, `ATTACHED_IDENTITY` withheld |

The exact 0.7.0 SDK, Wasm backend and Python guest are pinned together. The adapter accepts no custom guest, image or guest working directory, and refuses unsupported platforms. Construction starts no worker. Windows workers retain `WinHvPlatform.dll`; Linux workers verify KVM API access and VM creation. Each uses the pinned host's single-VM mode, `HYPERLIGHT_MAX_SURROGATES=0`. Linux hosts exposing MSHV are refused until that family has its own validation.

The bundled runtime is CPython 3.14 with a reduced standard library. It supplies Python statement execution, separate stdout/stderr, persistent globals and snapshot restore. In the pinned guest, `json`, `math` and `re` are available, while `datetime`, `statistics`, `pickle` and `__future__` are not. The runtime instructions expose this limitation instead of implying desktop Python compatibility.

## Execution and cleanup

A worker owns all PyO3/native objects on its main thread. Acquire starts the process, establishes Windows job or Linux cgroup containment, initializes the guest, runs its warm preparation and records the baseline snapshot. No program is admitted before that completes. The process environment excludes application credentials, and neither input nor output directories are configured in the SDK.

One host process owns Hyperlight within a shared ownership namespace, enforced by a Windows machine-wide named event or a Linux lock file under `/run/lock`. All backend objects in that process share a key/kind registry. Another process refuses acquire and reports disposal as unclean, so a delete routed to the wrong process cannot claim success. The serving host must receive requests and purges; this implementation does not support multiple owners behind one logical backend. Separate Linux mount/PID/cgroup namespaces do not share this guarantee automatically.

A Windows job kills each worker tree on host exit. Linux requires an operator-delegated cgroup v2 root. A trusted supervisor starts in a separate process session and inherits the owner lock before creating a cgroup or worker. It sets `memory.max`, `memory.swap.max=0` and `memory.oom.group=1`; a bootstrap joins containment before executing the worker command. The supervisor monitors host and worker pidfds outside that memory group, kills the worker and its entire group on exit or close, waits for the group to empty and removes it. It also cleans up when the host exits during startup, including while the bootstrap is outside containment. Ownership remains held until cleanup completes, so a new process cannot report a clean purge while the old tree is still being terminated. The operator keeps that supervisor running and owns the lifetime of the delegated subtree. Acquisition refuses unavailable controls instead of weakening containment.

Admission uses locks independent of an asyncio event loop. A sandbox serializes run and reset; the backend serializes acquire and disposal. `run_code` includes queue time in its deadline. Expiry before submission is `SandboxQueuedTimeout` and preserves the running worker. Expiry after submission terminates it and raises `TimeoutError`; cancellation likewise terminates and reaps it before propagating. Cleanup has a separate bounded allowance and uses its own thread so it cannot queue behind blocked pipe readers. Native result buffering is bounded by Windows committed-memory or Linux cgroup-accounted memory limits, with a second byte limit on returned stdout/stderr and a separate 64 KiB retained diagnostic limit. The defaults are 1.5 GiB on Windows and 3 GiB on Linux; Linux disables swap and rounds the ceiling down to a whole page.

Reset restores the original warmed baseline and changes `instance_id` only after success. An ordinary guest exception returns a failed `ExecResult` and permits reuse; a transport/native failure retires the worker. Policy changes require disposal instead of reusing a VM under a different allowlist or execution contract. Exact-instance disposal ignores stale IDs, preserves other kinds and agents, and retains failed targets for retry. `dispose_scope` reaches every matching target in the owner's shared registry. Cancellation waits for the active worker's bounded cleanup attempt, then propagates without starting another target; unreported targets remain registered for retry. CodeAct's exclusive admission covers execution and cleanup, and `Cleanup.RESET` selects warm reset between its calls.

## Egress boundary

Hyperlight's native [network policy](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/hyperlight_sandbox/src/network.rs) checks each request's scheme, host, port and path. The adapter translates each exact core hostname into HTTP and HTTPS root permissions. Wildcards, non-default ports and narrowed method/authority requests are unsupported. The pinned native implementation always blocks CONNECT and TRACE. It exposes HTTP helpers, not raw guest sockets.

HTTP runs on the host network; allowing an internal name or loopback grants access there. Hostname policy does not filter resolved IP addresses. No credentials or proxy settings are forwarded from the application environment, and the adapter attaches no identity. The host remains responsible for which destinations it authorizes.

## Evidence

On 2026-09-13, Windows 11 AMD64 / WHP / host CPython 3.13 and the exact 0.7.0 trio ran the new adapter's guest suite. It covered stream separation, ordinary exceptions and warm state, reset of globals and builtins, absent host environment values, refused guest writes, infinite-loop timeout/cancellation with worker reaping, queue exhaustion without submission, output limits, CLOSED/exact-host HTTP enforcement before and after reset, selective instance disposal, scope purge and CodeAct with both fixed and per-spec routing. Separate Windows subprocess tests exercise committed-memory limits and abrupt owner exit. Portable tests cover failure retention/retry and blocked stdin/stdout/stderr pipes. These are local WHP results; CI's ordinary suite does not validate a hypervisor.

The consolidated [research record](../research/hyperlight-backend.md) preserves the historical investigation. Its proposed file capabilities are not declarations of this adapter. Optional file work must establish its own conformance and cleanup before any file capability is enabled.

The Linux validation used Ubuntu 24.04.4 under WSL2, kernel `6.18.40.1-microsoft-standard-WSL2`, CPython 3.12.3 and the same exact 0.7.0 trio. Real KVM VM creation and all ten guest scenarios passed: nine as an unprivileged host, with the HTTP-policy scenario run separately with permission to bind port 80. Kernel tests exercised cgroup OOM, descendants in separate sessions, abrupt owner exit and lock retention through cleanup. These are WSL2 measurements; the native Linux record is separate below. MSHV and AKS require their own environment records; the ACA measurements are recorded below. CI separately runs the Linux kernel lifecycle checks and the real KVM guest suite, recording the native Linux environment and failing if KVM or guest execution is unavailable.

Native Linux [CI on commit `9030d6ca`](https://github.com/sokolaidev/maf-extensions/actions/runs/34791208333/job/103815669959) used Ubuntu 24.04.5 LTS, kernel `6.17.0-1022-azure`, x86-64, CPython 3.13.15 and the exact 0.7.0 SDK/Wasm/Python guest trio. All ten real KVM guest scenarios passed in 12.19 seconds as an unprivileged host, including CLOSED/exact-host HTTP enforcement and both CodeAct selection modes. The same runner passed 126 package/kernel tests with 12 platform or opt-in skips. The test operator delegated a temporary cgroup subtree, granted KVM group access and allowed the HTTP fixture to bind port 80; no kernel identity or backend admission was mocked.

## Azure Container Apps

Direct execution inside a standard managed ACA Linux application container has no supported deployment path in the published platform contract inspected on 2026-09-14. Consumption, Dedicated and the inspected preview offerings expose no documented mechanism to inject a KVM/MSHV device. The [Hyperlight research record](../research/hyperlight-backend.md#azure-container-apps) pins the current API-source audit, separates that conclusion from unmeasured runtime behavior, and assesses memory, cache, shutdown and owner routing. A Linux adapter alone does not supply the missing platform access.

The live ACA probe summarized in the [Hyperlight research record](../research/hyperlight-backend.md#azure-container-apps) tested standard Consumption and Dedicated D4 application containers in Sweden Central on the same date. For both root and UID 65534, `/dev/kvm` and `/dev/mshv` were absent and the pinned 0.7.0 SDK's first run failed with no hypervisor found. Imports and writable guest caches succeeded. Dedicated exposed the CPU `svm` flag, so hardware capability alone did not grant the required device access. Other regions and profiles remain unmeasured; no Hyperlight guest or adapter conformance passed on ACA.

An ACA application can instead be designed to call a separate Hyperlight worker service on a suitable host. This requires a remote integration with authentication, owner identity, cancellation and purge semantics; it is not support for this local backend inside ACA.

## Status

| Item | State | Tracking |
| --- | --- | --- |
| Initial runtime and reset backend | implemented with Windows WHP validation; umbrella remains open for the independent channels | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open); initial runtime delivered by [#1223](https://github.com/sokolaidev/maf-extensions/pull/1223) (merged) |
| Linux x86-64 KVM and WSL2 | implemented with native Linux KVM CI and separate local WSL2 KVM validation | [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228) (open); Linux implementation delivered by [#1231](https://github.com/sokolaidev/maf-extensions/pull/1231) (merged) |
| AKS hosting | investigation | [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230) (open) |
| Optional writable inputs | open | [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) (open) |
| Optional output collection/listing | open | [#1219](https://github.com/sokolaidev/maf-extensions/issues/1219) (open) |
| Optional file cleanup | open | [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220) (open) |
| Native host tools | open | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
| Direct ACA hosting | investigated: measured device absence and SDK failure on Consumption/D4; no supported device-access mechanism found | [#1229](https://github.com/sokolaidev/maf-extensions/issues/1229) (closed) by [#1242](https://github.com/sokolaidev/maf-extensions/pull/1242) (merged) |
| Separate remote worker for an ACA application | not planned: authenticated single-owner prototype proposal closed without implementation | [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236) (closed) |
