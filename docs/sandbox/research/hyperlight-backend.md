# Hyperlight research

> Consolidated research record, 2026-08-16 through 2026-09-22. It combines the Hyperlight backend design, source exploration, filesystem prerequisite and cleanup audit, Azure Container Apps feasibility audit and live ACA probe, and the AKS upstream audit and measurements. The runtime backend is implemented for its validated family; flat output collection is now opt-in; writable inputs and native host tools remain separate follow-up work. The decided contract lives in the [Hyperlight backend guide](../backends/hyperlight.md).

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

Hyperlight's native wasi-http boundary enforces an exact per-entry allowlist. The default is deny-all; `allow_domain` permits a selected host and still rejects an unlisted host. The adapter translates a core hostname into HTTP and HTTPS root permissions because the native API requires scheme-qualified targets. The current contract does not expose method-scoped rules, arbitrary ports, wildcards, raw sockets, CONNECT or TRACE.

HTTP runs on the host network. Allowing an internal hostname or loopback therefore grants access to the host-side network, and hostname policy does not filter resolved IP addresses. Application credentials and proxy settings are not forwarded, and the backend attaches no identity. The host owns destination authorization. Method-scoped egress remains a possible additive capability, not a current Hyperlight declaration.

### Host tools

The guest's `call_tool(name, **kwargs)` is a synchronous FFI callback. Registration happens before the first run and cannot be undone; callbacks live for the sandbox lifetime. The callback runs on the same OS thread that called `run`, so an adapter needs a trampoline per sealed registry name and a bridge from that thread to the host event loop. The guest blocks until the host returns, and a hung callback hangs the guest.

The FFI marshals nested dictionaries, lists, strings, numbers, booleans and null. Positional arguments are rejected. Unsupported return types fall back to string conversion, which is lossy. Host exceptions become catchable guest `RuntimeError` values while the sandbox remains healthy; unregistered names use the same wrapper. Duplicate registration last-wins in the measured SDK, so the adapter must enforce the suite's own sealed-name policy before registration.

The native channel supplies only name lookup. It has no call cap, response ceiling, argument policy, integrity label, identity policy or per-call approval. Those gates belong to `HostToolRun` and the core protocol. Native host tools are therefore a separate follow-up under [#369](https://github.com/sokolaidev/maf-extensions/issues/369), with the measured 16,376-byte boundary and approximately 192-byte framing as acceptance inputs.

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

No container-containment implementation, deployable application overlay, new live probe or production support declaration was delivered by this design update. The existing local Linux and Windows containment contracts remain unchanged.

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
- Method-scoped egress, resolved-IP filtering and attached identity are not current Hyperlight claims.
- Direct ACA hosting remains unsupported by the inspected platform contract and measured profiles. The separate-worker route is a new remote integration, not a backend configuration switch.
- The pinned Hyperlight distributions and guest artifacts have licenses and notices independent of the Python package; dependency upgrades must retain the exact matched-version and conformance discipline.
