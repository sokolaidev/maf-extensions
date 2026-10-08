# OpenShell backend spike

> Research: OpenShell v0.1.2 contract characterization for [#1797](https://github.com/sokolaidev/maf-extensions/issues/1797), under [#1796](https://github.com/sokolaidev/maf-extensions/issues/1796). Installed-SDK fixture probes and gateway startup were measured on 2026-10-08; a live Docker workload was measured on 2026-10-09. No suite adapter or runtime is qualified.

## Target and evidence boundary

OpenShell is a candidate backend beneath the suite's protocol/router/kind boundary. This record evaluates the public SDK against the [backend contract](../backends/writing-a-backend.md); the separate [OpenClaw research](openclaw-integration.md) concerns hosting suite workloads through OpenClaw.

The upstream reference is [v0.1.2 at `6648bd0c290efbc41ba131ee9831ee45cd431f94`](https://github.com/NVIDIA/OpenShell/tree/6648bd0c290efbc41ba131ee9831ee45cd431f94). Initial source mapping used suite revision `1023fe287ee4bb92e2b4b4908e6a28c9f2e77396`; this experiment was prepared on `0d3647499199b089f7c4e2d9d547ed4569096b82`.

The [SDK probe](../../../scripts/experiments/openshell_sdk_probe.py) installs `openshell==0.1.2` through its PEP 723 metadata and calls the real SDK against a loopback gRPC fixture. The fixture returns controlled protobuf events; it does not execute guest commands or reproduce a real deletion race. Generated protobuf imports serve the experiment and are not an adopted adapter dependency contract.

```sh
uv run --script scripts/experiments/openshell_sdk_probe.py
```

The command emits versions, source hash and measurements as JSON. It creates no OpenShell sandbox, imports no local credentials, binds an ephemeral loopback port, and shuts down the fixture in a `finally` block. A changed result fails assertions for the fixed SDK behaviors; a successful probe characterizes compatibility gaps and does not mean the SDK satisfies the suite contract.

## Installed SDK observations

Measured with Python 3.13.12 on Windows, grpcio 1.84.0, protobuf 7.36.2, cloudpickle 3.1.2, httpx 0.28.1 and googleapis-common-protos 1.75.5. The installed `sandbox.py` SHA-256 was `6b883b224375ae48dda8cba7c8ff5c48e69de3e94248335e208514e968cd7f95`, equal to the file downloaded from the pinned upstream commit. This verifies that source file, not the entire wheel or its dependencies.

| Observation | Measured result | Integration consequence |
|---|---|---|
| Non-UTF-8 stdout/stderr | Raw chunks preserved `00fffe80` and `fe00ff`; the convenience result re-encoded as `00efbfbdefbfbdefbfbd` and `efbfbd00efbfbd` | Consume bytes before display decoding. |
| Streaming retention | Consuming 8,388,608 bytes without caller accumulation retained 8,538,985 traced bytes after the last raw chunk; peak was 25,174,342 bytes | Streaming does not establish bounded capture. Memory figures are observations from this process, not stable thresholds or guest-memory measurements. |
| Fractional execution deadline | `timeout_seconds=0.25` raised `TypeError` before any RPC | The adapter must preserve the suite's floating-point budget through another supported transport surface or explicit deadline ownership. |
| Exit code 124 | Returned as a normal result | An adapter cannot infer timeout solely from this code. |
| Deletion precondition | The generated request has `name`, `workspace_scope`, `allow_missing` and `request_id`; no expected-instance field | Waiting against an expected ID does not condition the initial delete request. |
| Replay identity | The SDK emitted empty `request_id` values for the exercised exec and delete methods | Retry admission and uncertain responses need explicit treatment. |

The [pinned SDK](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/python/openshell/sandbox.py) accumulates output in `exec_stream` before building its final replacement-decoded result. Its `wait_deleted` returns when the current sandbox ID differs from the expected ID. That protects waiting from following a replacement; it does not prevent a stale name-addressed delete from initially targeting one.

The [gateway](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/grpc/sandbox.rs) emits exit code 124 on its own execution timeout, while the [exit message](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/openshell.proto) has only `exit_code`. At this stage, a natural guest exit of 124 and an actual gateway timeout still needed comparison through a real sandbox; the later experiment below supplies that comparison. Closing a transport is not evidence that descendants stopped.

## Gateway startup smoke

The Linux x86-64 gateway archive's SHA-256 matched the v0.1.2 release checksum: `218d887845b3a020ab7535c9985eb9c666d6938f144044957f8b82b42892aadb`. It ran on WSL 2 with kernel `6.18.40.1-microsoft-standard-WSL2`, using Docker Desktop's Linux Engine 29.8.2. This is the upstream experimental Windows/WSL topology, not native Linux qualification.

The smoke used an isolated temporary configuration and SQLite state, an explicit Docker driver and experiment-specific sandbox label, `image_pull_policy = "never"`, and disabled telemetry. The gateway and dedicated health listener bound to loopback with TLS disabled for this local fixture. No provider was attached and no workload was created. The gateway reported its Docker driver connected, and the dedicated `/readyz` listener returned HTTP 200 with version `0.1.2` and a healthy database check. The supervisor/workload route was not exercised. The gateway exited with code 0 after termination, and temporary state was removed.

An initial request to `/readyz` on the main API listener returned 404. Configuring `--health-port` and querying that dedicated listener corrected the smoke harness; the 404 was not a failed sandbox readiness result.

## Live Docker workload — 2026-10-09

The [live probe](../../../scripts/experiments/openshell_live_probe.py) created a disposable workload through SDK and gateway v0.1.2 on Docker Desktop Linux Engine 29.8.2, kernel `7.0.14-linuxkit`. The gateway and SDK ran inside a temporary Linux control container using Python 3.13.16. This exercised the actual supervisor and guest runtime; it remains a Docker Desktop observation, not qualification of native Linux, MicroVMs or Kubernetes.

| Component | Measured immutable image |
|---|---|
| Runtime | `ghcr.io/nvidia/openshell/sandbox@sha256:b0f0f6217b11b22954b10a033a9fd798cf0a4e35cf4af63c1ba0d9c03e74338a` |
| Supervisor | `ghcr.io/nvidia/openshell/supervisor@sha256:90a6f1a7257a8d3c76f89cc7cc1a1050567cf065a59ce76888f23dfd4f4c757d` |
| Workload base | `python@sha256:8fb4cfa1a2616d7b8e0c2175cc6ad68f5729c34ea8488c0b360d2934b7be9024` |
| Locally built workload | `sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f` |
| Locally built control container | `sha256:8d8714182c026753e1f3b369c7d15e35ba30e297a6e4253aa8b60e47f6e64776` |

Runtime and supervisor references select Linux amd64 manifests. Local image IDs identify the measured builds, not published images or reproducible-build claims. The workload added a `sandbox` user with UID 1000 and an owned `/sandbox` working directory to the pinned Python base. The control image used the same base, installed `openshell==0.1.2`, and copied the checksum-verified gateway binary described above.

The isolated gateway used the Docker driver, `image_pull_policy = "never"`, explicit runtime and supervisor references, telemetry disabled and ephemeral database/signing state. Its API and health listeners bound to loopback on ports 19797 and 19798. TLS was disabled and `gateway.auth.allow_unauthenticated_users = true` was explicit for the disposable fixture; launch-scoped gateway JWT authentication used a temporary Ed25519 key pair. This setup does not qualify production authentication. The control container mounted the Docker socket, and host networking let the supervisor reach the gateway's loopback callback. The workload itself had Docker network mode `none`.

The probe supplied no providers or network rules, required Landlock, selected user/group `sandbox`, and allowed reads under `/usr`, `/bin`, `/lib`, `/lib64`, `/etc`, `/proc` and writes under `/sandbox`, `/tmp`, `/home/sandbox`, `/dev/null`, with `include_workdir` enabled. These are the requested policy and observed Docker topology; this experiment did not establish every effective filesystem or proxy rule.

To repeat the guest measurements, prepare that disposable gateway and an immutable workload image containing Python and the `sandbox` user, then run the probe in a process that shares the gateway's loopback namespace:

```sh
uv run --script scripts/experiments/openshell_live_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --observe-seconds 15
```

Use the new local image ID if rebuilding. The observation event supplies the sandbox ID; select its workload using Docker labels `openshell.ai/sandbox-id` and `openshell.ai/isolation-role=sandbox`, inspect its network mode and process table during the observation window, and check containers and volumes bearing that exact sandbox ID after deletion. The script's `api_deleted` value alone is only API evidence. The probe accumulates its small fixed outputs and is not an implementation of bounded capture.

| Measurement | Observed result |
|---|---|
| Invalid UTF-8 | Raw stdout `00fffe80`, stderr `fe00ff`, exit 0. |
| Natural exit 124 | Exit 124 after approximately 0.025 seconds, empty streams. |
| Ten-second sleep with one-second timeout | Exit 124 after approximately 1.005 seconds, empty streams. |
| Parent spawning a delayed child, one-second timeout | Exit 124 after approximately 1.005 seconds; a later command read the child's marker as present. |
| Independent process observation | Both timed-out parent commands and the child remained in Docker's workload process table after their execution responses had completed. A separate earlier attempt also observed the child after its parent naturally exited. |
| Direct TCP connection to `1.1.1.1:443` | `connect_ex` returned 13 (`EACCES`); no application data was sent. |
| Disposal | Delete completed and the expected-ID wait returned; independently, no containers or volumes bearing that sandbox ID remained. A temporary observer in the Docker host PID namespace found none of the five previously observed workload PIDs. |

The timeout response therefore did not establish termination of either the command or its descendant. This is an adapter contract gap observed in this configuration, not evidence of a sandbox escape. Natural exit 124 and gateway timeout also remained indistinguishable by exit code and stream content alone. A future adapter needs independently attributable deadlines and verified process cleanup or whole-sandbox disposal before returning ownership to its caller.

Deletion removed the observed processes and resources; it does not prove stale-name race safety, cleanup under control-plane loss or bounded cleanup time in failure cases. The temporary gateway was subsequently removed, destroying its signing keys and database; no experiment-labelled containers or volumes remained. Local images were retained. One denied direct connection with network mode `none` does not qualify the suite's complete `CLOSED` contract or any allowlist behavior.

## Owned execution experiment — 2026-10-09

The [execution core](../../../scripts/experiments/openshell_owned_exec.py) and [live driver](../../../scripts/experiments/openshell_owned_probe.py) now exercise an owned asynchronous gRPC stream with a caller deadline and separately budgeted disposal. They remain experiment code outside shipped packages. The channel is owned directly; the driver does not read the convenience client's private stub or channel. Generated bindings still come from the pinned SDK's private `_proto` namespace, which is an explicit experimental dependency rather than an accepted production API.

Each case creates a unique physical sandbox and disposes it even after ordinary success. Execution uses a nonzero request ID, omits the gateway execution timeout, and attributes the deadline through `asyncio.timeout`. A natural exit 124 therefore remains an ordinary result. Aggregate stdout plus stderr capture is capped before appending each chunk, and the gRPC channel separately limits message size. These establish retained output and message limits, not a measured bound on total process memory or upstream buffering.

After stream cancellation, a separate task owns the finite cleanup budget and is shielded from repeated caller cancellation. The receipt records the primary outcome independently from `cleanup_verified`; failed or unverified cleanup keeps `ownership_retained` true and retains the sandbox name and ID. Cancellation is deliberately recorded in this diagnostic receipt rather than propagated as the suite's caller-facing exception. Receipts are not a durable recovery service, and this experiment does not establish cleanup after the client process dies.

The live Docker observer reads the engine API independently of gateway lifecycle replies, captures immutable container IDs and known process start times, and requires absence of those container IDs, matching sandbox-labelled containers/volumes and the known processes before reporting verified cleanup. The control fixture additionally joins the Docker host PID namespace for these observations. It retains the same image identities, effective fixture configuration and authentication limitations as the preceding smoke; the final gateway configuration is copied into the control container after creation because the image's original configuration does not contain the temporary JWT settings.

| Live case | Primary outcome | Execution / through cleanup | Independent cleanup result |
|---|---|---|---|
| Exact binary streams | Exit 0; stdout `00fffe80`, stderr `fe00ff` | 0.033 / 0.194 seconds | Verified. |
| Natural exit 124 | Ordinary exit 124 | 0.029 / 0.175 seconds | Verified. |
| Parent and child, 0.25-second caller deadline | Deadline, no guest exit inferred | 0.251 / 0.475 seconds | Parent and child were observed before disposal; both gone before return. |
| Parent and child, explicit task cancellation | Cancelled, no guest exit inferred | 0.252 / 0.463 seconds | Verified despite additional cancellation requests during cleanup. |
| Parent and child, execution-channel closure | Transport error, not caller deadline | 0.251 / 0.482 seconds | Verified through the separate cleanup channel. This is controlled channel closure, not a network-partition experiment. |
| 2,048 stdout bytes plus 2,048 stderr bytes | Exit 0 at the 4,096-byte aggregate cap | 0.038 / 0.196 seconds | Verified. |
| Same output plus one byte | Output-limit failure; offending chunk not appended | 0.029 / 0.219 seconds | Verified. |
| 256-byte message limit, guest writes 8,192 bytes | gRPC `RESOURCE_EXHAUSTED` for a 4,102-byte message | 0.031 / 0.244 seconds | Verified; transport rejection is not partial guest success. |
| Injected cleanup stall after caller deadline | Deadline plus cleanup timeout; ownership retained | 0.251 / 0.352 seconds to incomplete receipt | Both containers still existed; a separate recovery operation then verified disposal. |

Times are single observations, not latency guarantees. The execution clock begins immediately before dispatch, after sandbox provisioning/readiness. The second clock includes disposal but is not the suite's complete tool-call latency. Normal cleanup had a 20-second budget; the deliberately stalled case used 0.1 seconds. A separate final host-side check found none of the 28 known PIDs from the retained receipts, and Docker label queries found no experiment containers or volumes. The temporary gateway and its signing/database state were removed after the experiments; local images and raw receipts were retained.

To repeat, prepare the preceding disposable gateway with Docker socket access and the Docker host PID namespace, place both experiment modules together, and run the driver from the gateway's loopback namespace. For example, inside the prepared control container:

```sh
python /opt/openshell_owned_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --case deadline_child
```

Other case names are `bytes`, `natural_124`, `cancel_child`, `disconnect_child`, `cap_exact`, `cap_over`, `message_limit` and `cleanup_timeout`. Use the new immutable local image ID if rebuilding. Capture stdout from process start: it first emits the created sandbox identity, then the final receipt. `cleanup_timeout` deliberately reports incomplete cleanup before a separately recorded recovery; a zero process exit only reports final resource recovery and is not a conformance verdict. On other incomplete receipts, preserve the ownership record and recover before further use. The pre-dispatch setup-failure fallback uses the SDK delete/wait path and does not carry the same independent verification claim.

Fourteen offline regression cases cover byte fidelity, natural exit 124, exact/over-limit capture, fractional deadlines, repeated cancellation while cleanup is pending, incomplete/failed/cancelled cleanup, missing or out-of-order terminal events, dispatch failure and transport failures. The live cases additionally exercised actual protobuf streaming and gRPC message rejection. Ruff, focused Pyright, documentation structure and path checks passed. The original SDK/runtime limitation remains; the experiment works around it by retiring the entire sandbox, which sacrifices reuse and guest state after each execution.

## Stale deletion and persisted recovery — 2026-10-09

The [lifecycle probe](../../../scripts/experiments/openshell_lifecycle_probe.py) now reproduces replacement at an explicit barrier between lookup and deletion. It also kills a client process after confirmed ownership has been saved by the [recovery journal](../../../scripts/experiments/openshell_recovery.py). The same pinned fixture images, Docker host PID observer and loopback authentication limits apply. The workload policy builder is shared with the preceding execution experiment; its fractional-deadline/cleanup case passed again after that extraction.

| Live case | Observation | Consequence |
|---|---|---|
| Replacement visible before recovery lookup | A was disposed and B created under the same name. Recovery observed B's different ID, did not delete it, independently verified A's resources absent, and left B running. | The guard handles a replacement already visible at lookup. |
| Replacement after a successful identity precheck | Lookup matched A; the controlled replacer disposed A and created B; the stale first delete used a fresh request ID. The delete response identified B and independent observation confirmed B was disposed. | A get-before-delete guard cannot satisfy exact-instance disposal against a concurrent name owner. This is an adapter contract counterexample, not a sandbox escape. |
| Replay of A's already accepted deletion | A was deleted with a retained request ID, then B created under the same name. Replaying the identical request returned A's ID and left B running. | Admission replay protects this immediate retry; it does not make a previously unadmitted stale request conditional on A. Replay after retention expiry was not tested. |
| Client process killed with SIGKILL | The client persisted A's physical ID, container IDs, known PID start times and a deletion request ID before signalling readiness. After client exit `-9`, the workload parent and child remained alive in two sandbox containers. A new connection recovered from the journal and independently verified disposal. | A client dying does not establish remote termination. The saved confirmed-owner record enabled recovery in this case. |
| Gateway stopped during recovery | After another client kill, the actual gateway process was terminated. Recovery returned `UNAVAILABLE`, retained ownership and saved phase `pending`. Restarting the gateway with its same database and signing state let a fresh recovery process verify disposal. | Temporary gateway loss is distinct from successful cleanup. This did not simulate permanent database loss, host reboot or key loss. |

The journal writes a temporary file, syncs it, atomically replaces the record and syncs its directory on POSIX. Recovery persists `pending` before making a deletion request and retains the same request ID. Malformed records refuse mutation; observer or RPC failures leave recovery pending. A completed record is explicitly a historical receipt and does not claim a fresh observation. The journal assumes a trusted path and one writer; concurrent recovery workers, initial creation with a lost response, power-loss durability and automatic discovery of abandoned records are not qualified.

Deletion is still name-addressed. The recovery helper's ID lookup is a refusal check under exclusive mutation authority, not an atomic fence. A mismatched deletion response can expose damage after it occurred; it cannot undo deletion of B. Therefore the currently measured API is a **no-go for general exact-instance disposal when another actor can replace the physical name**. A production proposal must establish enforceable exclusive ownership without name reuse, obtain a conditional-delete API, or use another independently qualified physical-instance lifecycle authority. Request IDs alone do not solve the first-request race.

To repeat inside the prepared control container, place all four experiment modules together (`openshell_lifecycle_probe`, `openshell_recovery`, `openshell_owned_probe`, `openshell_owned_exec`) and provide a fresh journal path:

```sh
python /opt/openshell_lifecycle_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --case stale_race --ledger /tmp/lifecycle/stale-race.json
```

Other cases are `stale_precheck`, `delete_replay` and `client_crash`. `crash_hold` intentionally leaves the owned sandbox running and records its identity; follow it with `--case recover` using the same journal. The gateway-interruption experiment stops only the known fixture gateway process between those two steps, attempts recovery while it is unavailable, then restarts it with the same state and repeats recovery. Preserve the journal and stdout receipts before removing the control container. The script characterizes results; read its structured fields rather than treating exit 0 as a conformance verdict.

Fourteen new offline regression cases cover replacement refusal, stable request IDs after a lost response, unavailable gateway, mismatched deletion responses, malformed ownership, independently observed absence, interrupted atomic replacement and historical completed receipts. Combined with the preceding execution and documentation tests, 260 tests passed; focused Ruff, Pyright, documentation path and diff checks also passed. All test-owned replacements and recovered workloads were independently observed absent, and the temporary gateway was removed after journals and receipts were saved locally. No package or hosted CI gate ran.

## Remaining qualification and preliminary decision

Continue the spike, but do not ship a thin convenience-SDK wrapper. The disposable Docker experiment demonstrates byte preservation, aggregate capture refusal, fractional caller deadlines and verified whole-sandbox cleanup under the listed conditions. Live replacement testing now establishes that the name-addressed API fails the general exact-instance disposal requirement without enforceable exclusive ownership. Unique physical names and exclusive mutation authority remain test assumptions, not proof against that race.

Resolve the lifecycle authority boundary before promoting this execution strategy to an adapter. Persisted recovery now covers a confirmed owner after client SIGKILL and temporary gateway loss with preserved state; creation uncertainty, concurrent recovery and permanent control-plane loss remain unresolved. Then qualify storage-base preparation, files, exec, deadlines, process cleanup and call-scope disposal through the shared probes. Withhold snapshot/reset, native runtime execution, host tools and attached identity until their contracts are independently established.

Egress translation remains source-only work beyond the single denied direct connection. OpenShell's [policy schema](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/sandbox.proto) distinguishes single-label and recursive host globs and defaults unspecified L7 enforcement to audit. The suite's [host/path semantics](../network.md) require deliberate translation and blocking enforcement. Test allowed success alongside denied hosts, methods, paths and direct routes; include provider-composed policy and later revisions. Policy containment checks do not replace runtime observations.

Local command execution is no longer blocked. The initial SDK probe passed its characterization assertions; the live experiments now cover the timeout gap, owned execution/disposal, controlled replacement, immediate deletion replay and the recorded recovery cases. The full repository gate, wheel smoke tests, network partitions, permanent control-plane loss, filesystem confinement, full egress policy tests and shared conformance have not run for this spike. Neither issue is ready to close.
