# OpenShell backend spike

> Research: initial OpenShell v0.1.2 contract characterization for [#1797](https://github.com/sokolaidev/maf-extensions/issues/1797), under [#1796](https://github.com/sokolaidev/maf-extensions/issues/1796). Installed-SDK fixture probes and gateway startup were measured on 2026-10-08. No suite adapter or workload sandbox is qualified.

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

The [gateway](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/grpc/sandbox.rs) emits exit code 124 on its own execution timeout, while the [exit message](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/openshell.proto) has only `exit_code`. A natural guest exit of 124 and an actual gateway timeout still need comparison through a real sandbox. Closing a transport is not evidence that descendants stopped.

## Gateway startup smoke

The Linux x86-64 gateway archive's SHA-256 matched the v0.1.2 release checksum: `218d887845b3a020ab7535c9985eb9c666d6938f144044957f8b82b42892aadb`. It ran on WSL 2 with kernel `6.18.40.1-microsoft-standard-WSL2`, using Docker Desktop's Linux Engine 29.8.2. This is the upstream experimental Windows/WSL topology, not native Linux qualification.

The smoke used an isolated temporary configuration and SQLite state, an explicit Docker driver and experiment-specific sandbox label, `image_pull_policy = "never"`, and disabled telemetry. The gateway and dedicated health listener bound to loopback with TLS disabled for this local fixture. No provider was attached and no workload was created. The gateway reported its Docker driver connected, and the dedicated `/readyz` listener returned HTTP 200 with version `0.1.2` and a healthy database check. The supervisor/workload route was not exercised. The gateway exited with code 0 after termination, and temporary state was removed.

An initial request to `/readyz` on the main API listener returned 404. Configuring `--health-port` and querying that dedicated listener corrected the smoke harness; the 404 was not a failed sandbox readiness result.

## Remaining qualification and preliminary decision

Continue the spike, but do not ship a thin convenience-SDK wrapper. First establish a cancellable byte-preserving transport, an aggregate output cap and exact-instance ownership. Unique physical names and exclusive mutation authority may help ownership, but a get-before-delete check alone does not close the replacement race.

Then select a digest-pinned workload/runtime/supervisor image set, create one sandbox, and qualify storage-base preparation, files, exec, deadlines, process cleanup and call-scope disposal through the shared probes. Withhold snapshot/reset, native runtime execution, host tools and attached identity until their contracts are independently established.

Egress translation remains source-only work. OpenShell's [policy schema](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/sandbox.proto) distinguishes single-label and recursive host globs and defaults unspecified L7 enforcement to audit. The suite's [host/path semantics](../network.md) require deliberate translation and blocking enforcement. Test allowed success alongside denied hosts, methods, paths and direct routes; include provider-composed policy and later revisions. Policy containment checks do not replace runtime observations.

Local command execution is no longer blocked. The initial SDK probe passed its characterization assertions, Ruff checks and a focused Pyright check. The repository gate, wheel smoke tests, real sandbox execution, cancellation/descendant cleanup, stale-deletion races, filesystem confinement, egress and shared conformance have not run for this spike. Neither issue is ready to close.
