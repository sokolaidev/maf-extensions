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

## Remaining qualification and preliminary decision

Continue the spike, but do not ship a thin convenience-SDK wrapper. First establish a cancellable byte-preserving transport, an aggregate output cap and exact-instance ownership. Unique physical names and exclusive mutation authority may help ownership, but a get-before-delete check alone does not close the replacement race.

The digest-pinned workload/runtime/supervisor smoke now supplies actual byte and timeout observations. Next qualify storage-base preparation, files, exec, deadlines, process cleanup and call-scope disposal through the shared probes, after selecting an execution strategy that addresses the measured timeout gap. Withhold snapshot/reset, native runtime execution, host tools and attached identity until their contracts are independently established.

Egress translation remains source-only work beyond the single denied direct connection. OpenShell's [policy schema](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/sandbox.proto) distinguishes single-label and recursive host globs and defaults unspecified L7 enforcement to audit. The suite's [host/path semantics](../network.md) require deliberate translation and blocking enforcement. Test allowed success alongside denied hosts, methods, paths and direct routes; include provider-composed policy and later revisions. Policy containment checks do not replace runtime observations.

Local command execution is no longer blocked. The initial SDK probe passed its characterization assertions; the live probe completed and reported the timeout gap above. Ruff checks, focused Pyright and documentation structure checks passed. The full repository gate, wheel smoke tests, caller cancellation/disconnect, bounded capture, stale-deletion races, filesystem confinement, full egress policy tests and shared conformance have not run for this spike. Neither issue is ready to close.
