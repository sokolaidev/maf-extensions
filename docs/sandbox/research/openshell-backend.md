# OpenShell backend spike

> Research: OpenShell v0.1.2 contract characterization for [#1797](https://github.com/sokolaidev/maf-extensions/issues/1797), under [#1796](https://github.com/sokolaidev/maf-extensions/issues/1796). Installed-SDK fixture probes and gateway startup were measured on 2026-10-08; live Docker workload/lifecycle cases were measured on 2026-10-09 and OIDC authorization on 2026-10-10; guest file access, filesystem policy, native SFTP and bounded HTTP/HTTPS egress matrices, anonymous provider composition, serial policy revisions, synthetic credential binding, multi-provider static updates, elapsed credential expiry and bounded gateway-managed OAuth refresh were characterized on 2026-10-11. The spike concludes with a defer decision; the current adoption boundary is recorded in the [backend index](../backends/README.md#openshell-evaluation). No suite adapter or runtime is qualified.

## Spike conclusion - 2026-10-11

**Defer general OpenShell backend adoption.** The bounded spike has enough source, offline and live evidence to make that decision. It does not approve an adapter package or any suite capability. OpenShell v0.1.2 can execute owned workloads through the raw transport under the measured conditions, but a stale first name-addressed deletion can remove a replacement, and bounded create replay cannot establish the original admission after retained history expires. Official upstream recovery and conditional deletion remain dependencies under [#1817](https://github.com/sokolaidev/maf-extensions/issues/1817) and [#1818](https://github.com/sokolaidev/maf-extensions/issues/1818).

### Exit-criteria reconciliation

| Original work area | Evidence and disposition |
|---|---|
| Immutable upstream release and suite contract mapping | Completed: v0.1.2 source, SDK/protobuf and suite execution, lifecycle, files and egress boundaries inspected. |
| Disposable transport/lifecycle harness | Completed: opt-in experiments and offline guards are outside shipped packages; raw transport avoids the measured convenience-SDK byte/deadline behavior. |
| Offline execution, bounds, cancellation and stale disposal probes | Completed as characterization: positive ownership/cleanup cases and unsafe stale-name deletion are recorded; the proposed receipt/deletion model is a specification, not an upstream implementation. |
| Linux Docker and shared runtime/file conformance | Partially exercised on Docker Desktop/WSL; a supported native Linux target and the shared storage-base, EXEC, FILES_IN, FILES_OUT, FILES_DELETE, reach and call-scope suites remain unqualified. Carry forward in [#1833](https://github.com/sokolaidev/maf-extensions/issues/1833). |
| CLOSED, allowlists and provider-added policy | Bounded HTTP/HTTPS, direct-route, composition and serial policy observations completed. Complete effective-policy translation and in-flight cutover remain unqualified; carry forward in [#1834](https://github.com/sokolaidev/maf-extensions/issues/1834). |
| Credential ownership, expiry and refresh | Additional bounded static/OAuth evidence completed. Continuous-process behavior, automatic failure recovery, reconnect/restart and a qualified revocation boundary remain unqualified; carry forward in [#1835](https://github.com/sokolaidev/maf-extensions/issues/1835). |
| Evidence, limitations and decision | Completed by this closeout: exact pins, reproduction, cleanup observations and known refusals appear below; defer adoption and keep the epic open. |

This reconciles the spike's acceptance of a bounded defer outcome; deferred checks are not counted as passed conformance. No first supported runtime is selected. Native Linux Docker remains the candidate for future qualification; the measured Windows/WSL topology does not establish native Linux, MicroVM or Kubernetes support. Every prospective suite capability, including attached identity, remains withheld because no OpenShell adapter ships.

The repository retains the research record, fixtures and offline tests. Detailed raw execution transcripts and machine-local receipts were retained during the spike and are not bundled into this PR; the public record contains the bounded observations and reproducible commands, not a claim that every transcript is published. No new live qualification is implied by rebasing or merging this closeout. Use only official upstream releases/source, keep the creation-recovery FR as a draft, and reopen adoption through the epic after the prerequisite and qualification outcomes warrant it.

## Native Linux qualification harness — 2026-10-24

The follow-up for [#1833](https://github.com/sokolaidev/maf-extensions/issues/1833) in [PR #2115](https://github.com/sokolaidev/maf-extensions/pull/2115) adds an [Ubuntu runner workflow](../../../.github/workflows/openshell-native.yml), [pinned gateway configuration](../../../scripts/experiments/openshell-native.toml), [workload image recipe](../../../scripts/experiments/openshell-native.Dockerfile) and [measurement driver](../../../scripts/experiments/openshell_native_probe.py). The driver refuses Docker Desktop/WSL, uses the official v0.1.2 gateway archive checksum and immutable runtime/supervisor references recorded below, and records the suite revision, SDK versions, engine/kernel, workload image, policy and physical ownership receipts.

The [candidate transport](../../../scripts/experiments/openshell_conformance.py) connects raw bounded exec and native SFTP to the shared storage-base, EXEC, FILES_IN, FILES_OUT, FILES_DELETE, reach and call-scope probes. Its subject requests all five relevant capability probe groups to expose findings; this is a measurement configuration, not a backend declaration. It declares no shipped capability. Storage-base probes cover the image default and an explicitly prepared nested base. Call-scope measurements use fresh keys differing only in call ID within one workspace and independently dispose physical instances; they do not establish shared-workspace deletion safety.

File metadata checks use the suite's existing lexical/ancestor rules, with native SFTP as the file plane and guest exec as the independent planting/control plane. The SFTP root is restricted to the pinned image's `/sandbox` base; arbitrary roots are unsupported. A deterministic barrier replaces a checked parent with a link before native file open, testing whether serial metadata checks suffice under replacement. Static probe success must not be promoted to atomic no-follow support. Recursive deletion is measurement-only and shares the native path-replacement limitation.

Additional execution cases measure exact binary streams, natural exit 124, aggregate output at and above the cap, a fractional deadline, repeated cancellation, parent/child process observations, and injected cleanup failure followed by separate recovery. File-bound cases exercise exact and exceeded caps, growth after metadata inspection and nonregular-file refusal. A failed execution disposes the whole fixture instance before returning where disposal succeeds. Cleanup records container identities and known PID/start times independently of gateway acknowledgements; incomplete cleanup fails the run and retains ownership records.

The workflow retains JSON findings, gateway/probe logs, immutable image metadata and final engine inventory for 14 days, including failed runs. A successful characterization job means the experiment completed and cleanup was verified; inspect every shared probe's `passed`, `failure` and `skipped` fields. Findings and skips are not conformance passes. Automatic uncertain-create recovery, general exact-instance disposal, attached identity, snapshot/reset, host tools and native runtime execution remain withheld. The runner is a disposable sole-controller experiment, not an adoption of a restricted production authority profile.

Before sandbox creation, the report records a pending workspace/name selector, request ID, request digest and call key. Only the original response's ownership callback clears that pending entry. Lost responses retain the selector without replaying creation; confirmed owners can still be disposed, but unresolved admission prevents verified physical cleanup and workspace deletion. Workspace creation also records its name before dispatch. These diagnostic records are not a durable restart-recovery ledger or a substitute for #1817.

### Hosted observations

[Run 40035060161](https://github.com/sokolaidev/maf-extensions/actions/runs/40035060161) completed on the GitHub-hosted `ubuntu-24.04` runner: Ubuntu 24.04.5 LTS, kernel `6.17.0-1022-azure`, Docker 28.0.4, x86_64, Python 3.13.17, OpenShell SDK 0.1.2, gRPC 1.84.0 and Paramiko 4.0.0. The checkout was PR merge revision `b087894bb42966de634b8bdd6868262d73fcdcad`, with workload image `sha256:945c2ccb2aa011b64890a85992b5a835294dbc3344ed7ceace2ef5d5a9c56513`. The gateway and runtime/supervisor are unchanged official artifacts. The fixture disables the Docker AppArmor profile and requires Landlock; this result does not qualify a different security profile.

| Measurement | Observed result | Qualification boundary |
|---|---|---|
| Shared file probes | FILES_OUT 12/12, FILES_IN 12/12, FILES_DELETE 10/10 passed | Serial static-path controls pass; concurrent confinement fails below. |
| Shared execution, reach and storage | EXEC 7/7, reach 2/2, default/explicit storage bases 2/2, call scope 5/5 passed | 50 shared results passed with no skips; fixture key separation does not establish general lifecycle authority. |
| Native file bounds | 5/5 passed: exact 1,024-byte transfer, oversized read/write refusal, growth to 1,025 bytes after metadata inspection, FIFO refusal | Growth is detected during bounded capture; this does not make the pathname stable or authorize nonregular reads. |
| Additional execution cases | 7/7 passed: bytes, natural exit 124, exact/over aggregate cap, fractional deadline, repeated cancellation, cleanup failure/recovery | Parent and child PIDs/start times were observed before deadline/cancellation; physical absence was independently checked after disposal. |
| Checked-parent replacement | Read returned `outside-secret`; write overwrote the outside sentinel | Both targets stayed inside `/sandbox` but escaped the requested `/sandbox/race/work` working directory. Metadata prechecks do not provide atomic no-follow confinement. |
| Final cleanup | All 18 owned instances disposed; containers, volumes, recorded processes and workspace verified absent | Sole-controller fixture cleanup only; no stale-name safety or uncertain-create recovery claim. |

The run's `report.json` has SHA-256 `6eb750668b22bfdf966a6be33cad237214d11fad4b4676e8a1b3237f20acb108`, retained with gateway/probe logs and image metadata in artifact `openshell-native-40035060161-1`. The earlier [run 40033815667](https://github.com/sokolaidev/maf-extensions/actions/runs/40033815667) independently completed the same shared/execution/replacement cases before the five file-bound cases were added.

The native baseline leaves **EXEC blocked for adoption** by #1817/#1818. **FILES_IN, FILES_OUT, FILES_LIST and FILES_DELETE remain unsupported under concurrent guest mutation**: the measured read/write replacement escape rules out promoting static probe success, and enumeration/removal have no atomic no-follow qualification. Snapshot/reset, host tools and native runtime execution remain unsupported; attached identity remains unqualified. Keep the epic's defer decision. A future official lifecycle implementation still requires downstream qualification, and a confined file plane requires a separate atomic operation boundary or an explicitly justified restriction; these measurements authorize neither a backend release nor a restricted production profile.

## Native owned HTTPS trust boundary — 2026-10-25

The [HTTPS probe](../../../scripts/experiments/openshell_https_probe.py) prepares four owned TLS endpoints for #1834: a valid leaf, a leaf for another hostname, an expired leaf and a leaf signed by an untrusted CA. A short-lived fixture CA is added to the workload image's system bundle through `update-ca-certificates`; the gateway, supervisor and sandbox runtime remain official pinned v0.1.2 artifacts. Private origin keys stay in the disposable origin and temporary certificate directory and are excluded from the workload build context and evidence artifact.

**Installing a destination CA in the workload does not qualify trusted owned HTTPS in this Docker topology.** In [run 40147249931](https://github.com/sokolaidev/maf-extensions/actions/runs/40147249931), the independent client accepted the owned valid leaf with explicit CA trust, but the sandbox's same-origin GET disconnected and the TLS origin recorded exactly `TLSV1_ALERT_UNKNOWN_CA`, without an HTTP request. The workload image's build installed the CA successfully. The sandbox instance and workspace were independently verified absent after the failed measurement. The [pinned Docker driver](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-driver-docker/src/lib.rs) runs the network supervisor in its separate official image, while the [pinned verifier](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-supervisor-network/src/l7/tls.rs) reads the system trust store of the process doing verification. The workload CA does not reach that store. Cross-driver destination-CA delivery is tracked by [NVIDIA/OpenShell#3295](https://github.com/NVIDIA/OpenShell/issues/3295), open at this checkpoint; its issue text's older combined-container assumptions are not evidence for this measured topology.

Independent controls require valid GET/POST/path responses, hostname-mismatch code 62, expiry code 10 and unknown-issuer code 20. The valid endpoint must fail without the explicit CA, and the mismatched endpoint must succeed under its actual certificate hostname. All these controls passed in the run above. They validate the fixture, not OpenShell's hostname/expiry behavior: without a trusted sandbox positive control, those dependent cases remain unrun. Policy configuration, independent-client success and workload certificate installation alone cannot substitute for that positive control.

The native characterization now also reads back the installed public CA fingerprint and its presence in the workload's system/effective bundles. An exact same-port unknown-CA alert paired with the first guest disconnect records a finding with `passed=false`, `qualification_passed=false`, a destination-trust blocker and six explicit unrun cases; it then measures empty-policy refusal in a separate instance. Arbitrary disconnects, timeouts, wrong-port alerts and any origin HTTP side effect fail the experiment. A green characterization job can reproduce the blocker; it does not mean the intended eight-case HTTPS qualification passed. If official supervisor trust becomes available, all eight cases must pass before the report can mark qualification successful.

[Run 40147761410, attempt 2](https://github.com/sokolaidev/maf-extensions/actions/runs/40147761410/attempts/2) completed the characterization at suite revision `d952747d2fb17fb2ef4678b7879078d90bdf2f5a`, using workload image `sha256:985725867e4b20ee4aa1900bcf99878962472b14ec403a19bcc474f73db50631`. The installed CA fingerprint matched `dd838814fc227be0864b375cf2361ea1ca324f39b67d3b11d3aa8f360a808796`; the system bundle contained it, while the effective bundle did not. The valid-origin GET reproduced the exact unknown-CA finding, the separate empty-policy request returned `EACCES` with no origin event, and all six dependent cases remained unrun with `qualification_passed=false`. Both HTTPS instances and the workspace were independently verified absent. Artifact `openshell-native-40147761410-2` retains `https.json` with SHA-256 `8294b4d6caa28b6eec75b532f7d7adbb6f4c0b98eba51ef8dfcca545f2eba0fe`. The same job also completed the 18-instance runtime/file baseline and all 17 HTTP cases with cleanup.

Attempt 1 of that run completed the HTTPS observations and verified physical absence, but workspace deletion failed because the gateway still reported a sandbox record. The unchanged attempt 2 verified both physical and workspace absence; this does not qualify the absence of a cleanup race. Earlier run 40146438471 stopped on an SFTP-close EOF in the runtime baseline; its rerun passed that baseline but exposed a fixture-only Docker BuildKit image-reference error, corrected before the HTTPS measurements above. The full local suite passed 23,849 tests with 947 skips before the final finding/report guards; the final focused OpenShell/workflow suite passed 383 tests. Lint, formatting, security baseline, both type-check passes, Markdown checks, documentation references and 343 live tracker references passed.

The workflow retains CA/leaf fingerprints, immutable workload image identity, independent controls, guest/origin observations and instance/workspace cleanup evidence for 14 days. No verification bypass, custom supervisor image, upstream submission or capability declaration is introduced. Trusted owned HTTPS remains blocked for this official pinned topology; credentials, certificate revocation, IPv6, DNS rebinding, policy transitions, concurrent/restart behavior and production trust management remain unqualified. #1834 stays open.

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

## Lifecycle authorization boundary — source assessment, 2026-10-09

The pinned [workspace documentation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/workspaces.mdx), [authentication documentation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/gateways/authentication.mdx), [workspace authorization implementation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/auth/workspace_authz.rs) and public RPC descriptors establish the following source-level boundary. Current documentation search was used for discovery; these claims are checked against the pinned release rather than inferred from an unversioned RFC.

| Control | Pinned contract | Effect on exclusive lifecycle ownership |
|---|---|---|
| Workspace membership | Ordinary OIDC callers require membership in the selected workspace. | A dedicated adapter workspace can exclude unrelated ordinary principals. Runtime denial was subsequently observed in the 2026-10-10 fixture below. |
| Workspace User role | Both `CreateSandbox` and `DeleteSandbox` require the workspace `user` role and `sandbox:write` scope when scope enforcement is enabled. | Another writer in the same workspace has lifecycle authority; the creator does not receive an exclusive ownership fence. |
| Scope enforcement | `scopes_claim` enables checks; `openshell:all` satisfies every scope requirement. | Read-only credentials can be restricted, but granting `sandbox:write` admits both creation and deletion. |
| Membership administration | Workspace Admins can add users; Platform Admins can manage every workspace and bypass membership checks. | Administrators remain trusted lifecycle authorities; ordinary membership isolation does not constrain them. |
| Local authentication without OIDC roles | An empty configured admin role treats authenticated users as Platform Admins. | Merely enabling local authentication or reusing the preceding unauthenticated fixture does not establish exclusive adapter authority. |
| Physical-name reuse | The preceding live replacement probes recreated deleted names successfully. | The API supplies no never-reuse guarantee. An adapter would need its own persistent allocation authority in addition to access restrictions. |

The candidate restricted profile is therefore a dedicated OIDC workspace with the adapter as its only writer, explicit scopes, trusted platform/workspace administrators and host operators, and one persistent allocation/recovery authority that never reuses physical names. It is a proposal, not a qualified backend configuration. It does not defend against competing clients using the adapter's own identity, administrative replacement or loss of the allocation history. A generic shared-workspace backend remains blocked on conditional deletion or another independently qualified physical-instance authority.

The [authorization probe](../../../scripts/experiments/openshell_authority_probe.py) is prepared to run a loopback-only OIDC issuer with ephemeral Ed25519 credentials, a gateway with unauthenticated user access disabled, explicit roles/scopes, and a fresh workspace. Its 17 expected observations cover adapter creation, anonymous and outsider refusal, a read-only member, membership escalation refusal even with all scopes, a peer before/after membership, same-workspace deletion and name reuse, administrative deletion without membership, revocation, and same-principal name reuse. All resources are fixture-owned. The issuer is a controlled JWT validation fixture, not an external identity-provider integration or production TLS test.

The fixture issuer's signatures were independently verified with OpenSSL in WSL, its claims were checked, and its discovery/public-JWKS endpoints were exercised without exposing credentials. Ruff and focused Pyright passed, as did the existing 260 execution/recovery/documentation tests. **At this preparation checkpoint the live authorization matrix had not run:** Docker Desktop failed during local startup before any new sandbox or workspace could be created. The subsequent execution below supplies the runtime evidence; this source assessment alone did not establish enforcement. The earlier lifecycle results retain their original evidence boundary.

Once the disposable control fixture is available, place the authorization probe beside `openshell_owned_probe.py` and `openshell_owned_exec.py`, provide the existing fixture configuration and temporary gateway signing keys, and run it without another gateway already bound to the fixture ports:

```sh
python /opt/openshell_authority_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --config /opt/gateway.toml
```

The script owns a fresh gateway database and a temporary OIDC issuer for that execution, checks each observed status against its expected status, verifies physical-resource and workspace cleanup, and removes its temporary issuer credentials. Its numeric-loopback HTTP allowance and plaintext gRPC are explicit development-fixture settings. The control container still requires Docker socket and host PID observation access.

## Lifecycle authorization — live Docker fixture, 2026-10-10

The authorization probe ran against the same pinned OpenShell v0.1.2 gateway and immutable control/guest images identified above, with Python 3.13.16, grpcio 1.84.0, Docker Engine 29.8.2 and Linux kernel `7.0.14-linuxkit`. The temporary gateway required OIDC, used explicit role/scope claims and an isolated database, and accepted credentials only from the controlled loopback issuer. Adapter and reader subjects were workspace Users; the platform administrator was not a workspace member. The ordinary adapter token carried `sandbox:read sandbox:write workspace:read`; the reader carried only `sandbox:read`.

| Observed operation | Status | Evidence |
|---|---|---|
| Adapter creates A | `OK` | A reached ready; physical containers and process start times captured independently. |
| Anonymous create / delete A | `UNAUTHENTICATED` / `UNAUTHENTICATED` | Original containers and known processes survived the refused delete. |
| Nonmember creates / deletes A | `PERMISSION_DENIED` / `NOT_FOUND` | Original containers and known processes survived the refused delete. |
| Read-only member creates / deletes A / reads A | `PERMISSION_DENIED` / `PERMISSION_DENIED` / `OK` | Original containers and known processes survived the refused delete. |
| Adapter adds a peer, using `openshell:all` | `PERMISSION_DENIED` | Scope breadth did not bypass the workspace role requirement. |
| Peer deletes A before membership | `NOT_FOUND` | Original containers and known processes survived the refused delete. |
| Platform admin adds peer as User | `OK` | Same peer identity gained workspace membership. |
| Peer deletes adapter-created A | `OK` | Independent observation verified A's physical resources absent. |
| Peer creates B under A's former name | `OK` | B had a different physical sandbox ID and reached ready. |
| Platform admin deletes B without membership | `OK` | Independent observation verified B's physical resources absent. |
| Platform admin removes peer / revoked peer creates | `OK` / `PERMISSION_DENIED` | Membership revocation prevented this fresh create request. |
| Adapter creates C under the same name | `OK` | C reached ready with a third distinct physical ID. |

All 17 RPC status observations matched the final probe's expectations, and the four refused deletions retained the captured original containers and processes. The initial execution had 15 matching observations: it expected `PERMISSION_DENIED` for the two nonmember deletions. Inspection of pinned [`handle_delete_sandbox_inner` and its authorization tests](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/grpc/sandbox.rs) confirmed deliberate conversion of workspace permission refusal to `NOT_FOUND` to conceal object existence. The probe now requires that exact status and independent survival after each refused deletion; it does not accept arbitrary errors. Both raw executions were retained locally, including the initial expectation failures.

The probe independently verified absence of each captured container, matching sandbox-labelled containers/volumes and known process identities after disposal. It also verified workspace deletion through `GetWorkspace` returning `NOT_FOUND`. Final Docker label queries found no sandbox containers or volumes, and the exact temporary control container was removed, destroying its gateway signing state. The temporary issuer, keys and isolated database had already been removed by the probe. Docker remains available; immutable local images and receipts were retained.

**Decision:** the configured workspace boundary excludes the tested unrelated ordinary principals, and scopes exclude the tested read-only member from mutation. It does not establish creator-exclusive ownership. Another workspace writer can delete and reuse the adapter's name; a platform administrator can delete without membership; the adapter's own identity can reuse the name. The dedicated sole-writer profile is still conditional on trusted administrators/host operators and a persistent allocation/recovery authority. The earlier stale first-delete counterexample remains decisive for general shared-workspace exact-instance disposal.

Focused execution/recovery/documentation checks, Ruff lint/format, focused Pyright and documentation paths passed. This fixture does not qualify production TLS, an external identity provider, token revocation/caching, Workspace Admin behavior, competing clients using the same adapter identity, or durable name allocation. It also does not turn the earlier disposable execution harness into a shipped backend.

## Persistent allocation and uncertain creation - 2026-10-10

The [recovery helper](../../../scripts/experiments/openshell_recovery.py) now includes an experimental local allocation authority, exercised by the [allocation crash probe](../../../scripts/experiments/openshell_allocation_probe.py). Initialization is explicit and restricted to a new dedicated workspace/history. Reopening requires the existing lock and ledger; missing history is not silently replaced. The ledger binds to a caller-supplied gateway/workspace identity and retains every reservation, including cleaned and uncertain entries. A monotonic workspace-local counter yields 19-character physical names and refuses exhaustion. The first live attempt exposed the gateway's 19-character name limit; its longer name was rejected before workload creation, and the corrected probe was rerun in fresh workspaces.

The authority uses a nonblocking operating-system file lock for its entire lifetime and rejects simultaneous operations on the same object. All participating local clients must use that same trusted, unreplaced lock file and allocation history. Reservations persist exact create-request bytes and separate stable create/delete request IDs before dispatch. The journal changes the reservation to `uncertain` and syncs it before sending. Only a successful create response with captured physical evidence transitions it to `owned`. File replacement is atomic, with file sync and directory sync on POSIX. Confirmed recovery retains the existing pending/cleaned lifecycle and independent absence checks; cleaned records remain historical receipts rather than fresh observations.

Reserved and uncertain entries do not query, replay, adopt or delete by name. Their names remain consumed, and recovery reports retained ownership with cleanup unverified. This deliberately leaves an accepted create without durable confirmation unresolved. A successful lookup cannot prove which instance a lost response created, and replay after admission-history expiry has not been qualified.

The live probe used the same pinned v0.1.2 Docker/OIDC fixture and sole ordinary workspace writer as the preceding authorization matrix. It started separate client and competing-owner processes, killed each client with SIGKILL at a signalled barrier, and reopened the ledger from the coordinator process. The gateway continued running throughout these cases.

| Crash barrier | Observed persisted state and remote workload | Recovery and name allocation |
|---|---|---|
| Reservation saved, before dispatch | `reserved`; no sandbox existed. A competing process was refused the ledger lock. | Recovery made no lookup/delete calls. The next reservation used a different name. |
| Create accepted and ready, result withheld from the allocation authority | `uncertain`; the captured containers and known processes survived client exit `-9`. A competing process was refused the ledger lock. | Recovery made no lookup/delete calls and kept cleanup unverified. The next reservation used a different name. The uncertain workload remained running until separate fixture cleanup. |
| Confirmed physical owner saved | `owned`; the captured containers and known processes survived client exit `-9`. A competing process was refused the ledger lock. | Recovery used the saved owner and delete request ID, independently verified physical absence, and persisted `cleaned`. The tombstone remained; the next reservation used a different name. |

To repeat, place the allocation and recovery modules beside the existing authorization/owned-execution modules in the prepared control fixture. Use fresh ledger/evidence directories for the coordinator; worker restarts within that execution reopen the same history. For example, inside the control container:

```sh
python /opt/openshell_allocation_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --config /opt/gateway.toml --ledger /tmp/allocation-ledger --evidence /tmp/allocation-evidence
```

The uncertain case is controlled loss between the transport callback receiving a successful response and the allocation authority persisting it, followed by actual SIGKILL. It is not a network partition or proof of gateway behavior when an RPC deadline races acceptance. The test coordinator's separate oracle captured physical identity before signalling that barrier. Only the fixture used that oracle to clean up the uncertain workload; the allocator never received it, and the ledger still said `uncertain` afterward. This is test-resource disposal, not successful automatic recovery of an uncertain create.

All three crash cases and three competing-process refusals passed on the final code. The 17-case authorization matrix also passed after extracting the common ephemeral gateway setup. Physical resources and the workspace were independently verified absent; final Docker label queries were empty. The exact disposable control container and its temporary credentials were removed. Non-secret allocation history, fixture oracles and raw receipts were retained locally. Eighteen additional offline cases cover durable reservations, failed persistence/confirmation, uncertain-state refusal, binding/history validation, lock exclusion, reentrant operations, closed/inherited handles and no reuse after cleanup/reopen. Together with existing execution/recovery/documentation checks, 278 tests passed, along with Ruff, focused Pyright and documentation path checks.

**Boundary:** this is one local filesystem authority shared by cooperating clients, not a distributed or server-enforced fence. The configured binding is not an attested gateway generation. Another ledger path, copied or rolled-back history, direct use of the same credentials, administrators and host operators can bypass these assumptions. Windows exercised offline state/lock tests; the live crash measurements used Linux file locking inside Docker Desktop. Host reboot, power loss, shared/network filesystems, permanent gateway-state loss and counter-history restoration remain unqualified. Most importantly, uncertain creation now has a safe refusal state but no automatic reconciliation; no production backend is qualified.

## Create replay, restart and retention - 2026-10-10

Pinned [`mutation_replay`](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/grpc/mutation_replay.rs) stores admission under the identity provider, issuer and subject, then RPC method, workspace selector and request ID. It compares the canonical payload and recorded workspace identity before restoring success. Successful admissions expire 24 hours after completion; an expired success is removed and the same key can be admitted again. Unresolved admissions have no successful-completion expiry. The pinned [ordinary create recipe](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/grpc/mutation_replay/ordinary.rs) restores the sandbox by its saved physical ID, not by its current name. Restoration requires that original object to remain present. Public API inspection found no read-only admission lookup or replay-only create flag; invoking create to retrieve a receipt is still a potentially mutating operation.

The [replay probe](../../../scripts/experiments/openshell_replay_probe.py) uses the same pinned Docker/OIDC fixture, exact serialized request bytes and principal, and captures the `openshell-replayed` response metadata. It leaves the allocation ledger uncertain throughout characterization; the allocator itself still refuses automatic replay. Gateway restarts preserve the same isolated database, issuer and signing state.

| Live observation | Result |
|---|---|
| Create accepted but result withheld from the allocation authority; immediate identical replay | `OK`, original physical ID, replay marker true. |
| Same principal/request ID with changed command | `FAILED_PRECONDITION`, specifically a payload mismatch. |
| Same request bytes under another authorized workspace subject | `ALREADY_EXISTS`; the other subject did not retrieve the adapter's receipt. |
| Identical replay after stopping and restarting the gateway | `OK`, original physical ID, replay marker true. |
| Original deleted and a same-name replacement present, receipt still retained | `FAILED_PRECONDITION`, specifically replay unavailable; replacement ID/resources remained present. |
| Original deleted, successful receipt completion time aged beyond retention, identical request | `OK`, a new physical ID, replay marker false. The create was admitted again. |
| Replay following that new admission | `OK`, the new physical ID, replay marker true. The marker no longer refers to the first instance. |
| Admission changed to unresolved and its row timestamps aged beyond retention | `FAILED_PRECONDITION`, specifically outcome uncertain; the original physical ID remained present. |

All eight final observations passed. Retention was fault-injected by stopping the disposable gateway, matching exactly one successful create receipt by physical ID and setting its completion timestamp to 25 hours earlier before restarting. This is an exercised expiry branch, not a real 24-hour elapsed-time test. The unresolved case removes success/completion from one fixture admission and ages its row timestamps; it characterizes that persisted state, not an actual gateway crash timed between mutation and confirmation. The injection refuses zero/multiple matching rows, other object types and a gateway that has not stopped. No production persistence was accessed or modified.

To repeat, place the replay probe beside the allocation/recovery and authorization/owned-execution modules in the prepared control fixture, then run:

```sh
python /opt/openshell_replay_probe.py --execute --image sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f --config /opt/gateway.toml
```

**Decision:** immediate same-principal replay and restart with retained state can recover the tested original receipt. They do not establish safe automatic reconciliation across arbitrary downtime or missing admission history. After expiry, the call can create a different sandbox before the client sees that it was not a replay; if that response is lost, another retry can label the new result replayed. Therefore neither request-ID equality nor the replay marker alone proves the original creation. Keep uncertain allocation entries pending and their names consumed. A safe general recovery path needs a server-enforced replay-only/receipt query bound to the original admission, or an explicitly qualified bounded recovery policy that rules out expiry, identity changes and admission-state loss. Conditional exact-instance deletion remains a separate prerequisite for shared-workspace use.

Six offline tests verify the fixture injection boundary. The 17 authorization cases passed again with the restartable gateway fixture; 284 focused execution/recovery/replay/documentation tests, Ruff lint/format, focused Pyright, documentation paths and commit hooks passed. All captured physical instances and the workspace were verified absent after cleanup; final Docker sandbox-label queries were empty. The exact control container and ephemeral signing/database state were removed. Raw receipts remain local. Full repository/package gates, wheel smoke, hosted CI, real retention elapsed time, permanent database loss and production identity/TLS remain unverified.

## Recovery prerequisite contracts - 2026-10-10

These are proposed acceptance contracts, not OpenShell v0.1.2 features or implemented adapter behavior. The measured spike now separates two independent prerequisites: [non-mutating original-create receipts](https://github.com/sokolaidev/maf-extensions/issues/1817) and [deletion guarded by physical identity](https://github.com/sokolaidev/maf-extensions/issues/1818). Both are child issues of the epic alongside this spike. Neither should be inferred from the other: knowing which instance was created does not make name-addressed deletion conditional.

### Original-create receipt

The host persists a gateway state epoch, immutable workspace identity, caller identity, create request ID and canonical payload digest before dispatch. A proposed server contract binds those values at admission and rejects an obsolete epoch before creating anything. The epoch must represent server state continuity, not an arbitrary client string. Within an epoch, the original admission identity cannot be reassigned after receipt expiry. A new epoch invalidates old requests instead of admitting them again; garbage collection must return unavailable history rather than silently recycle the original key.

A receipt lookup is non-mutating with respect to admission and workloads. It never executes create, starts a sandbox, adopts the current same-name object or manufactures success after expiry. Confirmed receipts identify the original admission and physical sandbox even if that sandbox has since been removed. Retention of that identity, or an explicit unavailable-history result, replaces the present dependence on restoring a still-live object.

| Proposed outcome | Required evidence | Host action |
|---|---|---|
| Confirmed original | Immutable original admission and physical IDs, matching epoch/workspace/caller/payload binding | Persist confirmation before recovery; obtain physical lifecycle evidence separately. |
| Unresolved | Original admission exists without a confirmed outcome | Keep ownership pending and the name consumed; do not execute create again. |
| Absent at observation | No matching record at the lookup's observation point | Keep pending: an in-flight request can still commit after this observation. |
| History unavailable or expired | Original history cannot be established, including lost or rolled-back state | Refuse automatic adoption/replay; preserve the ownership record for reconciliation. |
| Binding mismatch or authorization refusal | Epoch, workspace, caller, payload or permissions do not match | Refuse mutation and retain pending ownership; do not retry under a different identity. |

An absent lookup is deliberately not terminal non-creation. If a future design needs that result, an atomic server-side admission fence must first prevent delayed requests from creating effects and account for already executing work. Timeouts, a second absent lookup and a local reservation cannot supply that fence. Receipt confirmation likewise does not establish resource absence or complete cleanup. Authentication checks remain current even when retrieving a historical outcome.

Acceptance tests must independently show no workload/admission mutation during lookup for confirmed, deleted-original, replacement-present, unresolved, missing and expired histories. Use explicit barriers for delayed create acceptance after an absent lookup. Cover preserved-state restart, state loss/restore/rollback, workspace replacement, identity changes and payload mismatch. A successful current-name lookup or replay marker cannot satisfy those tests.

### Conditional physical-instance deletion

A proposed delete request supplies gateway epoch, immutable workspace identity, selector, expected physical ID and stable deletion request ID under the caller's existing authority. The server compares the expected ID and commits to the physical target under the same lifecycle serialization that excludes replacement. Once accepted, queued work, driver calls, restart recovery and delayed cleanup retain that physical target; no later name lookup may redirect them. An adapter-side precheck remains insufficient.

Mismatch or unknown epoch/identity refuses before deletion side effects. An already-gone or historical-success result must still identify the expected original and leave any replacement intact. The expected physical ID and epoch/workspace binding form part of replay payload identity; admission expiry cannot make an A-targeted request delete B. An acknowledgement means only the stated control-plane outcome. The host keeps incomplete cleanup pending until independent resource/process evidence establishes disposal within the qualified runtime boundary.

The first acceptance harness should reuse the existing stale-precheck reproducer with barriers before admission, while waiting for lifecycle serialization, and before the physical deletion. In each case replace A with B and require every A-targeted request to leave B intact. Extend through cancellation before/after admission, lost responses, expired replay, deferred driver cleanup, gateway restart and permanent state loss. Replaying an already accepted deletion is one case, not proof of the stale-first-request contract.

### Adoption decision and work order

Prefer the explicit receipt and deletion contracts for general backend support. A bounded retry policy is a separate, currently unqualified operational alternative: it must establish history continuity, stable identities, server/client timing assumptions, request delay limits and a persisted conservative recovery cutoff across restart. A local timer or the 24-hour constant alone does not rule out admission-state loss, so no bounded replay fallback is enabled by this proposal. The restricted sole-writer profile still trusts administrators/host operators and an intact shared allocation history.

First produce the receipt API/state-model candidate under #1817 and the conditional-delete API/barrier harness under #1818. Then qualify any actual server implementation against the pinned probes before connecting those results to the allocator. Backend promotion still requires confined storage, full egress and shared conformance; these contracts do not complete #1797. This increment changes research and issue organization only, runs no new Docker cases, and publishes no upstream contribution.

## Executable contract model and deletion barriers - 2026-10-10

The [contract model](../../../scripts/experiments/openshell_contract_model.py) and [scenario tests](../../../tests/test_openshell_contract_model.py) supply the first offline deliverable for #1817 and #1818. They are a Python reference proposal with symbolic admission, physical-instance and cleanup-job IDs; they do not call OpenShell, implement a wire protocol, persist state, or change the allocator. Each model transition is atomic by definition. Real lock placement, storage durability, authenticated identity and externally established epoch continuity remain implementation obligations, not results of these tests.

The receipt surface separates `admit_create`, `complete_create` and non-mutating `lookup`. Immutable request values bind the proposed epoch/workspace/authenticated identity, request ID, name and canonical-payload digest. The digest is an input to this model; it does not implement upstream canonicalization. Confirmed outcomes keep their original admission/physical IDs after resource removal or replacement. Receipt expiry burns the key rather than admitting it again; unresolved admission cannot expire. An absent lookup does not prevent later admission/completion. If another instance occupies the name before a pending create completes, the original remains unresolved instead of adopting that instance.

The deletion surface separates atomic `admit_delete`, registry-only `acknowledge_delete` and physical `cleanup_delete`. Admission compares the expected ID and saves it in a cleanup job. Acknowledgement can remove only the original registry entry; it does not remove model resources or prove disposal. Driver cleanup consumes the saved physical target, including after receipt expiry or a retained-state restart represented by a deep copy. Explicitly notified state loss changes the modeled epoch and fences old work while leaving physical resources outstanding. The model does not detect database loss or rollback by itself, and copying state is not a restart/durability experiment.

| Invariant / scenario | Offline evidence |
|---|---|
| Lookup changes neither admission nor workload state | Entire model state is compared before/after lookup across absent, unresolved, confirmed, expired, replacement, lost-state, revoked and mismatched bindings. A deliberately admitting lookup fails the same oracle. |
| Missing or stale history does not silently choose a new original | Late completion after absence remains possible; expired keys refuse readmission; changed caller/binding cannot retrieve the original; a pending create refuses an intervening same-name instance. |
| Accepted deletion removes A while preserving B | Four deterministic schedules place replacement before admission/while logically queued, after atomic admission, after acknowledgement before driver cleanup, and after cleanup. The oracle checks B's registry/resources and every driver effect, and requires A's removal when deletion was accepted. |
| The harness rejects false success | A deliberately name-resolving driver deletes B and is detected; a no-op driver preserves B but fails the required disposal of A. These are expected negative-control failures inside passing tests. |
| Acknowledgement and replay do not imply cleanup | Acknowledged A remains physically present until cleanup; lost-reply/retained-state-copy recovery keeps the same job target; expired delete receipts do not redirect the retained job. |
| State/binding loss refuses mutation | Epoch/workspace mismatch, changed target under a reused delete request ID, and explicitly notified history loss refuse old work without claiming original resources absent. |

All 35 model scenarios passed. Run `uv run pytest -q tests/test_openshell_contract_model.py` for the executable proposal. The schedules enumerate chosen event orderings rather than run concurrent threads; there is no cancellation timing, real driver, multi-replica, power-loss, rollback-detection or production authentication qualification. No new Docker case ran for this increment. The full focused execution/recovery/replay/model/documentation set is 319 passing tests; Ruff lint/format, focused Pyright and documentation paths passed. Full repository/package gates and hosted CI remain unrun.

### Mapping the deletion proposal to pinned upstream code

The pinned [`ComputeRuntime` implementation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/compute/mod.rs) already resolves a name once into `SandboxDeleteTarget`, acquires the sandbox-ID lifecycle gate and local global lock, and hands that target to an owned worker. The worker rereads by physical ID, performs the durable deleting transition and passes physical ID/name to the driver. If the original ID vanished while queued, it acknowledges that original rather than re-resolving the replacement's name. This is source inspection, not new driver qualification.

The measured stale-first-request gap is earlier: public `DeleteSandbox` supplies no caller-expected ID, so name resolution can select B after the caller's successful lookup of A. A candidate should extend the public request/admission fingerprint and authorized target resolution with the expected ID, preserve the existing captured-ID path, and revalidate immutable workspace/name/epoch identity before committing. The internal `delete_sandbox_by_id` helper is not a ready public API: exposing it also requires workspace/authorization and state-epoch checks. Multi-replica serialization and restart/driver behavior need their own qualification; the model's atomic transition is not evidence for those implementations.

The receipt candidate separately requires server-side non-recycled admission identity and a read-only historical result, replacing neither current authorization checks nor independent cleanup observation. The work order below now tracks official upstream delivery and downstream qualification before integrating either proposal. The current Python allocator continues to retain uncertain creation without automatic replay or name-based adoption.

## Guest file access and filesystem policy - 2026-10-11

The [filesystem probe](../../../scripts/experiments/openshell_files_probe.py) ran 14 characterization cases against official OpenShell v0.1.2 with the previously pinned runtime, supervisor and control images. Python was 3.13.16; Docker Desktop Linux Engine was 29.8.2 with kernel `7.0.14-linuxkit`. No fork code or server patch was used. The new sentinel workload image was `sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd`, derived from the retained workload image `sha256:9c108fb76d00a3ba1465ef4ff33e8630b2a65941efbc3e6d809e948bbbfda24f` with the [fixture recipe](../../../scripts/experiments/openshell-files.Dockerfile). This is an identified local build, not a reproducible-build claim.

The fixture adds a world-writable directory and sentinel under `/opt/spike1797`, and a world-writable sentinel under `/etc`. A separate ordinary container running the same image as UID 1000 could read/write the `/opt` sentinel and overwrite the `/etc` sentinel without OpenShell. Those positive controls distinguish the observed policy denials from ordinary Unix ownership restrictions. The OpenShell workload retained the existing required-Landlock policy: `/sandbox` and `/tmp` writable, `/etc` readable but not writable, and `/opt` unlisted. It ran with a fresh workspace and short-lived fixture administrator token; this does not extend the earlier authorization matrix.

| Case | Observed result | Boundary |
|---|---|---|
| Binary input and output | All 256 byte values repeated 16 times round-tripped exactly: 4,096 bytes, SHA-256 `c8f5d0341d54d951a71b136e6e2afcb14d11ed8489a7ae126a8fee0df6ecf193` | Input was base64 in a small exec argument; output was raw gRPC bytes. This is not a native upload/download or large-file qualification. |
| Guest ownership and working directory | The guest overwrote the input, and exec honored `/sandbox/nested` | The probe itself created the parent as the guest; no adapter storage-base preparation contract was exercised. |
| Read-only policy | The `/etc` sentinel was readable; overwrite returned `EACCES`; its contents remained unchanged | The unrestricted same-UID control could overwrite it. |
| Unlisted path | Direct read, direct new-file write and a symlinked read into `/opt/spike1797` returned `EACCES` | These selected accesses were denied; this is not exhaustive filesystem containment proof. |
| Allowed path outside `/sandbox` | Direct `/tmp` access, `../tmp` from `/sandbox`, a linked parent into `/tmp`, and a linked final file into `/tmp` succeeded | Expected under the configured policy. Landlock is not a per-call file-API path resolver and does not by itself satisfy the suite's link/escape refusal contract. |
| Disposal | Independent Docker observation found captured container IDs and sandbox-labelled containers/volumes absent, and known workload PID/start-time identities gone; workspace lookup returned not found | A unique isolated fixture under one lifecycle authority; no stale-name or uncertain-create recovery claim. |

All 14 characterization assertions passed, including the expected accesses outside the candidate storage base. A passing result therefore does not qualify `FILES_IN`, `FILES_OUT`, storage-base confinement, or a new backend. The raw exec collector caps accumulated stdout plus stderr at 16 KiB for these small commands and uses a 10-second transport deadline; the transport can allocate a message before that cap is checked. It is not the suite's bounded file-read implementation, and cancellation is not asserted to terminate the guest command. Whole-sandbox disposal remains the separately observed cleanup boundary.

The pinned Python sandbox client exposes no file-transfer method. The separate official [SFTP implementation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-sandbox/src/sftp.rs) was inspected: it uses a directory descriptor and `openat2` resolution flags. Native SFTP transport, its root selection and link semantics have not been exercised here. Next qualify that official transfer surface, including metadata, link refusal, byte limits and any path-replacement window; then test the remaining egress allow/deny matrix. Do not promote exec-mediated file operations to a backend on the strength of this characterization.

### Reproduction and validation

Prepare the existing immutable control image and a copy of the gateway configuration described in the live fixture section, including its gateway-JWT path placeholders and explicit local authentication setting. The control image's original embedded config predates those additions: using it unchanged made the first attempt refuse request-ID admission before creation. The corrected probe generates its own temporary gateway signing keys, OIDC keys and database, then removes them on exit. It requires the control container's Docker socket, host network and host PID namespace for the same independent observation as earlier probes. No credentials should be copied from the user's environment.

Tag the verified retained workload image as `maf-openshell-1797-files-base`, then build `scripts/experiments/openshell-files.Dockerfile` with `--pull=false`. The build output must resolve that alias to the pinned workload digest above. An initial `FROM sha256:<local-id>` attempt was rejected by Docker as an image name; the verified local alias fixes build resolution without changing the base. Inside the prepared disposable control container, run the following with the copied probe directory and corrected configuration paths:

```sh
python /probe/openshell_files_probe.py --execute --config /opt/files-gateway.toml --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd
```

Use the observed immutable image ID if rebuilding; never substitute an unverified tag at probe time. The final run verified temporary key/database directories absent and no fixture-namespace containers or volumes remaining. The temporary control container was removed after observation; local fixture images remain for reproducibility. Unrelated Docker containers were not changed.

Eleven [oracle tests](../../../tests/test_openshell_files_probe.py) reject missing-path errors masquerading as policy refusal, unsuccessful commands, corrupted/truncated binary data, and malformed or oversized exec streams. The full focused execution/recovery/replay/model/files/documentation selection passed 330 tests. Ruff lint/format, focused Pyright and documentation paths passed. Full repository/package gates, hosted CI, native SFTP, concurrent filesystem mutation, large/bounded file transfers and the full egress matrix remain unrun.

## Native SFTP characterization - 2026-10-11

The [SFTP probe](../../../scripts/experiments/openshell_sftp_probe.py) extends the same disposable official-v0.1.2 filesystem fixture with native transfer observations. It obtains a `CreateSshSession` for the confirmed sandbox ID, bridges the official `ForwardTcp` SSH target over gRPC, and opens the supervisor's `sftp` subsystem with Paramiko 4.0.0. No fork, custom server, Docker-copy transfer or workload SSH daemon is used. The control container uses the same pinned image plus the test-client install; its writable layer is not a new immutable control-image claim. The client bridge has a 60-second stream deadline, 10-second SSH setup budget and 5-second SFTP channel timeout. The original 14 guest-exec cases and all 18 native SFTP characterization cases passed in the completed run.

| Native observation | Measured result | Integration consequence |
|---|---|---|
| Binary round-trip | 131,072 bytes containing all byte values survived native SFTP write/read; SHA-256 `59f410ae5e17962412e2aed4f815918f634932f2abf084f00bb638c4db017850` matched independent guest exec | Establishes these bytes on this fixture, not arbitrary-size transfer or suite capability conformance. |
| Metadata and guest authority | `lstat` reported a regular file of the exact size owned by UID 1000; the guest overwrote it and SFTP read the new bytes | The selected file view matches the workload storage and leaves the input writable by the guest. |
| Root and absolute names | SFTP normalized `.` to `/`, corresponding to guest `/sandbox`; both `/native.bin` and `/sandbox/native.bin` read the same file | Absolute names are not a general guest-root view; an adapter must specify the mapping. |
| Outside-root access | `../tmp/spike1797-value` returned permission denied; `/tmp/spike1797-value` and the existing absolute link into guest `/tmp` returned missing, although exec had proved the real `/tmp` file exists | Selected traversal/absolute-link paths did not leave the SFTP root. Missing here does not establish guest-wide absence. |
| Link metadata versus opening | `lstat` and directory listing reported guest-created links as links, but opening an in-root final link or a path through an in-root linked parent followed it | Native SFTP does not itself provide the suite's required refusal of such paths. Separate preflight metadata checks would still leave a concurrent replacement window. |
| Bounded read primitive | Reading 1,025 bytes from the 128 KiB file returned exactly the prefix needed to detect a 1,024-byte application limit | This was a requested-length observation, not an implemented `SandboxTransferCapExceeded` contract, full transport-memory bound or concurrent-growth qualification. |
| Link removal | Removing the final symlink preserved its target | A single final-link unlink case; recursive removal and guest-authority races remain unqualified. |
| Symlink creation interoperability | Paramiko `symlink(existing_target, new_link)` failed; with two unused names it instead created a link at the first argument pointing at the second | The selected client/server combination disagrees on symlink argument ordering. Do not silently treat the convenience call as compatible. |

The initial native run stopped at symlink creation; its sandbox and workspace were cleaned up. The completed run isolated that observation with two unused names and planted the read/list test links through guest exec, so creation interoperability cannot masquerade as read-confinement evidence. The [pinned SFTP handler](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-sandbox/src/sftp.rs) uses `linkpath` for the destination and `targetpath` for its target, whereas the observed Paramiko call sends its source then destination. This records a concrete interoperability result without asserting that every SFTP client has the same behavior.

Both runs independently verified the captured physical containers, known workload PID/start-time identities and sandbox-labelled volumes absent after deletion. Workspaces, ephemeral gateway/OIDC keys and database directories were removed, and the exact temporary control container was subsequently removed. Local images remain. This is still a unique-name, single-authority fixture; it does not solve the open conditional-delete or uncertain-create dependencies.

To repeat, prepare the same disposable control/image/configuration as the filesystem section, install `paramiko==4.0.0` in that control container, copy the experiment directory, then run:

```sh
python /probe/openshell_sftp_probe.py --execute --config /opt/files-gateway.toml --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd
```

The measured test-client dependencies were Paramiko 4.0.0, cryptography 50.0.2, bcrypt 5.0.0, PyNaCl 1.6.2, invoke 3.0.3, cffi 2.1.1 and pycparser 3.11; only Paramiko is pinned by the probe metadata, so transitive resolution may change on repetition. The fixture is loopback-only with temporary gateway authentication. It checks the supplied SSH fingerprint when present; this run does not qualify production SSH trust, credential expiry, revocation or multi-tenant file access.

Four [bridge failure tests](../../../tests/test_openshell_sftp_probe.py) verify original-instance binding and cleanup after handshake, host-key or authentication failure. The focused execution/recovery/replay/model/files/SFTP/documentation selection passed 334 tests; Ruff lint/format, focused Pyright and documentation checks passed. Full repository/package gates and hosted CI remain unrun. `FILES_IN`/`FILES_OUT` remain withheld: root selection across configurations, atomic link refusal under concurrent mutation, storage-base preparation, non-regular entries, recursive operations and complete transfer limits need further work. The next independent experiment is the official egress allow/deny matrix; it does not depend on accepting these file capabilities.

## Plaintext HTTP egress matrix - 2026-10-11

The [egress probe](../../../scripts/experiments/openshell_egress_probe.py) ran against official OpenShell v0.1.2 with the same pinned runtime, supervisor, control and sentinel-workload images. Three fresh sandboxes selected explicit L7 enforcement, omitted/default enforcement, and an empty network policy. The completed matrix passed 19 characterization cases. No fork, provider, attached credentials, middleware or mutable server patch was used.

A separate labelled origin container on a dedicated internal Docker network served plaintext HTTP on ports 8000 and 8001. It recorded method, Host, raw path/query and listener port. Every guest request carried a unique correlation marker. The independent observer required the exact expected origin record for success and no corresponding record for denial, except the permitted first hop of the redirect case. It also required a structured `policy_denied` 403 or explicit `EACCES`; DNS failure, timeout, connection refusal and arbitrary application 403s do not count. Unrestricted control requests confirmed the origin itself accepted GET, POST, the denied path and the alternate port.

Hostname routing used IP-encoded `nip.io` names resolving to the disposable origin's private IPv4 address. Resolution was checked before the run, and each allowed policy endpoint restricted `allowed_ips` to that exact address. HTTP traffic remained on the private Docker network, but DNS depended on public `nip.io` resolution; this is not an offline or controlled-DNS/rebinding qualification. The supervisor retained its official host-network topology, and workload containers retained network mode `none`. An attempted fixture hosts-file copy was refused by Docker's mounted-file protection before this DNS approach; no resolver or supervisor binary was modified.

| Case group | Observed result | Qualification boundary |
|---|---|---|
| Exact host, GET and paths | GET `/allowed`, a query-bearing request and `/allowed/child` reached the origin with status 200 | Policy used separate exact `/allowed` and descendant `/allowed/**` rules. |
| Denied method and paths | POST `/allowed`, GET `/blocked` and GET `/allowedness` returned structured L7 policy 403s with no matching origin request | These are method/path enforcement observations for plaintext HTTP, not arbitrary HTTP parsing coverage. |
| Denied host and port | An unlisted hostname and the allowed hostname on port 8001 returned `EACCES`; no matching origin request | The alternate port had an independent successful origin control. |
| Redirect | The allowed `/allowed/redirect` endpoint was reached; its redirect to the denied hostname returned `EACCES` without a second origin hit | One same-origin-address/different-host redirect, not all redirect chains or authority handling. |
| Host globs | `*.wild.<fixture>` admitted one label, refused a deeper name and refused its apex; `**.recursive.<fixture>` admitted a deeper name and refused its apex | Copying the suite's `*.` literally would incorrectly narrow deep subdomains. `**.` is a candidate translation for these cases, not a complete host-normalization proof. |
| Raw IPv4 TCP | A direct socket to the otherwise reachable origin returned `EACCES` under both the enforced and empty policies | A single private IPv4 destination/port; no claim about all protocols, IPv6 or metadata routes. |
| Empty network policy | The HTTP request and direct socket were refused with `EACCES` and no matching origin HTTP request | Extends the earlier direct-connection evidence; not full suite `CLOSED` conformance. |
| Omitted enforcement | POST `/allowed` and GET `/blocked` both reached the origin with status 200 | Confirms the audit default. An adapter must explicitly select enforcement for restrictive L7 rules. |

The first live attempt expected a host denial to be HTTP 403; it instead observed native `EACCES` and zero origin hits. Its physical sandbox and workspace were cleaned up, and the harness was corrected to distinguish L4/socket refusal from L7 policy responses. A 16-case run then passed; the final 19-case run added path-prefix and wildcard-apex checks and required exact origin attribution plus structured policy evidence. Reported passing assertions include audit-mode requests intentionally allowed by that configuration; they do not establish an adopted backend.

The source [endpoint schema](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/sandbox.proto) and the official [forward-proxy L7 test](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/e2e/rust/tests/forward_proxy_l7_bypass.rs) informed the fixture. Rules select the Python binary family, `protocol="rest"`, and the private origin IP explicitly. The audit control omits `enforcement`; the restrictive fixture sets `NETWORK_ENFORCEMENT_MODE_ENFORCE`. These binary/IP allowances are fixture configuration, not a general suite policy translator.

### Reproduction, cleanup and remaining scope

Use the same disposable signed-gateway configuration and immutable images as the filesystem fixture. On a dedicated internal Docker network, start a copy of the control image with the experiment directory and run `python /probe/openshell_egress_probe.py --serve-origin`. No host ports need publishing. Obtain that exact container's private IPv4 address, then run the following inside the separate host-network/PID-observer control container:

```sh
python /probe/openshell_egress_probe.py --execute --config /opt/files-gateway.toml --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP"
```

`ORIGIN_IP` must identify the owned recording fixture. The gateway signing/issuer/database setup is now shared through `signed_gateway`; its lifetime and cleanup remain explicit. The origin journal is separately retained as evidence. Every live attempt independently verified its captured physical container IDs, known workload PID/start-time identities and sandbox-labelled volumes absent, and verified workspace removal. Temporary keys/database directories were absent afterward. The exact control and origin containers and their dedicated network were removed; unrelated Docker resources were untouched, and local images remain.

Fifteen [egress evidence tests](../../../tests/test_openshell_egress_probe.py) reject generic connection failures, unrelated 403s, incorrect/missing/duplicate origin attribution and side effects on a denied request. The focused spike/documentation selection passed 349 tests. Ruff lint/format, focused Pyright and documentation checks passed; full repository/package gates and hosted CI remain unrun.

Next qualify HTTPS inspection with verified upstream certificates, then provider-composed policy and policy updates. IPv6, DNS changes/rebinding, alternate protocols and ports, encoded/canonicalized paths, case normalization, header/authority confusion, multi-hop redirects, unrestricted-mode fidelity and shared conformance remain untested. Egress declarations and method/path capabilities remain withheld. The lifecycle dependencies #1817/#1818 and native-file contract gaps remain independent of these HTTP observations.

## Native HTTP translation and cutover - 2026-10-24

The native workflow now also runs the [HTTP probe](../../../scripts/experiments/openshell_egress_probe.py) through the retained-ownership fixture, with a private recording origin on an internal Docker network. [PR #2138](https://github.com/sokolaidev/maf-extensions/pull/2138) delivers this fixed IPv4 HTTP-port experiment. It is not an implementation or declaration of a supported suite egress mode.

The fixture compiler accepts explicit uncredentialed `EgressRule` values with GET/POST methods and canonical ASCII paths. Suite `*.host` becomes OpenShell `**.host` so deeper subdomains remain included while the apex is excluded. Suite `/prefix/*` becomes the exact `/prefix` plus `/prefix/**`. It selects blocking `ENFORCE` explicitly and refuses attached providers, authority grants, duplicate host rules, unrestricted method/path requests and unqualified path encodings instead of silently broadening them. The selected HTTP port and executable are fixture restrictions, so this compiler cannot serve as a general suite allowlist translator.

An independent origin barrier admits a request but withholds its response. The controller clears the base policy, waits for the exact active version/hash to report `LOADED`, checks fresh HTTP/direct TCP refusals, writes a unique local signal through a separate guest execution, and then releases the origin response. After either a response or an exception, the guest waits for that exact signal before trying a second request through the same HTTP client. The signal uses the execution transport rather than the network policy under test and fails closed if absent. A matching origin journal and response echo establish completed traffic; connection closure is recorded separately from explicit policy refusal. No outcome from this bounded case establishes instantaneous revocation or production isolation. Policy restoration and a successful allowed request provide a final positive control.

[Hosted run 40109508741](https://github.com/sokolaidev/maf-extensions/actions/runs/40109508741) passed all 17 HTTP cases on native Linux with official OpenShell v0.1.2 at `6648bd0c290efbc41ba131ee9831ee45cd431f94` and the unchanged pinned images. The run tested PR head `d3c162769e98c3f5521c7cc67c69e86ae0ff96a1` through GitHub's merge revision `5837dedbb6b68eeec114e4102ae885f78b91da53`. The `egress.json` SHA-256 is `86e99ce6af129bf9b6340db57b44dcceba2ec95f64ffe7ce2ca5fcc4557ddb6c` in the workflow's 14-day artifact. Its `followup_signal_after_loaded` records revision 3 and policy hash `11eeb63cfbced79aeb1d82d4aaea33d8646c0ac02fc92e755c9e07f7627dda10`.

Earlier runs [40084927200](https://github.com/sokolaidev/maf-extensions/actions/runs/40084927200) and [40085405533](https://github.com/sokolaidev/maf-extensions/actions/runs/40085405533) predate the separate follow-up signal. Their 17 serial cases remain valid, but an early disconnect could start the follow-up before exact-loaded confirmation, so neither historical follow-up establishes post-confirmation ordering. The matrix below uses the controller-gated run instead.

| Measurement | Native observation |
| --- | --- |
| Exact and recursive host/path translation | Parent, child and deeper paths succeeded; prefix collision, denied path/host and wildcard apex were refused. One-label and deeper wildcard subdomains succeeded. |
| Method, port and redirect boundaries | POST, the alternate port and direct IPv4 TCP were refused. Only the allowed first redirect hop reached the origin. Uppercase host access succeeded. |
| Empty policy after exact loaded revision 3 | Fresh HTTP and direct TCP returned permission denial with no correlated origin request. |
| Admitted request across cutover | The origin recorded the held request, but the guest received `RemoteDisconnected` rather than its response. After the controller signal, the follow-up attempted connection generation 2 and returned `PermissionError` (`errno=13`), with no second origin hit; `same_connection=false`. The follow-up is ordered after exact-loaded confirmation; the earlier disconnection's timing is not established. This did not exercise a surviving persistent connection. |
| Restored policy at exact loaded revision 4 | The final allowed GET succeeded with matching independent origin evidence. |
| Cleanup | The single captured sandbox was disposed, physical and workspace absence were verified, and no pending create remained. |

The already-admitted request reached the origin before policy replacement, so response disconnection cannot establish rollback of origin-side effects. The fixture records `instant_revocation_qualified=false`. The controller-gated revision passes 92 focused tests; removing its signal gate fails all four real-subprocess cases, including early disconnect. The broader four-worker run completed with 23,630 passed, 936 skipped and four failures outside the changed files; all four passed on unchanged HEAD and with the fix restored, and the combined rerun passed 96 tests. This is not a green full local gate. Earlier mutation checks reject shallow wildcard translation, unrelated response echoes, a missing origin barrier, proxy-environment assumptions and miscounted refused reconnects.

Trusted owned HTTPS, certificate/hostname validation, provider-composed authority, IPv6, DNS rebinding, alternate protocols, concurrent controllers and restart remain unqualified. The independent credential-lifecycle work stays in #1835; general lifecycle authority stays blocked by #1817/#1818. #1834 remains open.

## HTTPS policy and origin trust - 2026-10-11

The opt-in [HTTPS probe](../../../scripts/experiments/openshell_https_probe.py) reuses the HTTP probe's owned-workspace lifecycle and independent Docker cleanup checks. All **9 HTTPS cases passed** with the same v0.1.2 gateway, SDK, unchanged official supervisor/runtime digests and sentinel workload image recorded above. The original **19 HTTP cases also passed** after adding the profile. These remain provider-free characterization fixtures rather than suite capability qualification.

The HTTPS matrix deliberately has two evidence boundaries. A public `httpbingo.org` echo endpoint supplies successful HTTPS responses containing the exact method and unique request URL; unrestricted certificate-verifying controls establish that it accepts the tested GET, POST and otherwise denied path. Public denials require structured OpenShell `policy_denied` responses or explicit `EACCES`, but there is no independent access to that public origin's request journal. A separate private origin supplies the untrusted-certificate case and an independent HTTP/TLS event journal. No certificate verification was disabled and no official image was rebuilt or patched.

| Case | Observation | Boundary |
|---|---|---|
| Allowed HTTPS GET | `/anything` returned 200 with the exact correlated URL and GET method, both before and after the certificate-refusal case | Exercises default Python trust of the injected inspection CA and successful upstream HTTPS for this public service. |
| Explicit enforced method/path denial | POST `/anything` and GET `/anything/blocked` returned structured L7 policy 403s | Public origin controls accept both; absence of public-origin side effects was not independently observed. |
| Denied host and empty network policy | `example.com` under the allowlist and the public echo host under empty policy both returned `EACCES` | Selected HTTPS requests only; not complete CLOSED or alternate-route conformance. |
| Untrusted private origin | Guest saw `RemoteDisconnected`; the independent origin recorded exactly one `SSLV3_ALERT_CERTIFICATE_UNKNOWN` and no HTTP request during that attempt | The same origin failed ordinary Python verification with self-signed certificate code 18 and succeeded with that certificate explicitly trusted by the independent control. This establishes rejection of this untrusted origin certificate, not a complete TLS validation matrix. |
| Omitted L7 enforcement | POST and the otherwise denied path reached the public echo service successfully | Audit-default behavior applies to these HTTPS requests too; translation must select explicit `ENFORCE`. |

The private origin used a fresh one-day self-signed RSA certificate with the exact `allowed.<private-ip>.nip.io` DNS SAN. Its private key stayed inside the owned origin container. Only the public certificate was copied to the independent control, which used it as an explicit trust anchor for its positive control; it was never installed in the supervisor or guest. Each measured request ran serially. The origin journal had to preserve its earlier prefix and contribute exactly the certificate-specific alert for the failing TLS attempt; a disconnect alone, timeout, DNS failure, arbitrary alert, duplicate alert or HTTP request does not satisfy the oracle. Initial attempts intentionally failed the assertion when no independent TLS alert was recorded or when its expected alert name differed from the measured one; those attempts also verified cleanup. The final matrix used the observed certificate-specific alert, with regression tests against weaker evidence.

Pinned [TLS root loading](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-supervisor-network/src/l7/tls.rs) combines bundled roots with the supervisor's system bundle. [Network setup](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-supervisor-network/src/run.rs) can overlay an operator proxy CA bundle, but the inspected [Docker driver argument builder](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-driver-docker/src/lib.rs) does not forward that bundle option. Consequently this fixture uses public trusted HTTPS for success and a private untrusted origin for rejection. This is a pinned-source observation, not a claim that every deployment mode lacks private CA support.

### Reproduction and validation

Use the HTTP section's labelled internal origin network and host-network control container. Copy the experiment directory to `/probe` in both containers and the signed gateway fixture configuration to the control. Generate the origin certificate inside the disposable origin with `openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 -keyout /tmp/https-origin-key.pem -out /tmp/https-origin-cert.pem -subj /CN=spike-1797-https -addext "subjectAltName=DNS:allowed.$ORIGIN_IP.nip.io"`; copy only its public certificate to `/tmp/https-origin-cert.pem` in the control. Start the origin with `python /probe/openshell_https_probe.py --serve-origin --cert /tmp/https-origin-cert.pem --key /tmp/https-origin-key.pem`, then execute:

```sh
python /probe/openshell_https_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

The fixture requires public DNS, outbound HTTPS to the public echo service, and reachability of the owned private origin from the host-network control/supervisor. The public control supplies a User-Agent because the echo service refused the control without one. It is not an offline or deterministic external-service test. The three fresh policy sandboxes each used a distinct physical identity. Every created sandbox, supervisor, labelled volume and recorded workload PID/start-time was independently absent after deletion; workspace removal was also verified. The origin/control containers and dedicated network were removed by exact identity afterward, and temporary signing/gateway directories were absent. Image digests remained unchanged.

Validation: **362 focused tests passed**, including 13 HTTPS evidence-oracle tests, the existing spike tests and 232 documentation structure checks. Focused Ruff, formatting, Pyright, document paths and this document's issue trackers passed. The full repository gate, wheel smoke tests and hosted CI were not run. HTTPS redirects, hostname mismatch, certificate expiry/revocation, TLS skip mode, private-root provisioning, provider-composed policy, policy revisions, IPv6, DNS rebinding and shared egress conformance remain unqualified. Lifecycle adoption prerequisites remain open under #1817 and #1818.

## Anonymous provider composition and policy revisions - 2026-10-11

The opt-in [composition probe](../../../scripts/experiments/openshell_composition_probe.py) passed **19 live HTTP observations** against the recording private origin, using the same v0.1.2 gateway, unchanged official runtime/supervisor images and sentinel workload digest as the preceding matrix. One workspace-owned anonymous provider profile allowed only GET `/provider` on one private-origin hostname with explicit `ENFORCE`; the base policy used a different hostname on the same origin and port. No credentials, model endpoints, external accounts or fork builds were involved. The separate **19-case original HTTP matrix passed again** after extending the shared fixture lifecycle.

| Transition | Measured result | Consequence for the suite |
|---|---|---|
| Empty base policy before attachment | Both hostnames returned `EACCES`; no corresponding origin requests | Provides the closed baseline for these selected routes. |
| Attach anonymous provider | After the exact attachment receipt reported `READY`, GET `/provider` succeeded; POST and the other path returned structured policy 403s; the base hostname stayed closed | Provider-derived rules add network authority even though the base network policy is empty. Explicit L7 enforcement still applied. |
| Add and replace base policy | Revision 2 allowed GET `/base`; revision 3 allowed GET `/next` and denied `/base`. Provider GET remained usable after both updates | Base updates and the provider layer compose independently in this distinct-host fixture. |
| Stale optimistic update | Reusing the resource version observed after revision 2 returned `ABORTED`; `/next` stayed allowed and `/base` stayed denied | One serial stale-writer refusal is measured; this is not a concurrent-writer or partition test. |
| Clear the base network policy | Loaded revision 4 closed the base hostname but provider GET still succeeded | Clearing base rules alone cannot implement `Egress.CLOSED` while a provider retains endpoint authority. |
| Detach, reattach and detach again | Exact detach receipts reached `REVOKED` and subsequent provider requests returned `EACCES`; reattachment restored access with a new receipt and attachment epoch; querying the original receipt returned `SUPERSEDED` | Wait for installed revocation and correlate the exact generation. An earlier ready receipt cannot certify a later attachment. |

Every request used a fresh guest process/connection and unique query marker. Success required exactly one origin record matching method, hostname, raw path/query and listener port plus the matching response echo. Denials required explicit `EACCES` or structured `policy_denied` and zero correlated origin requests. The origin independently accepted control GET/POST requests for both hostnames. These checks measure behavior after acknowledged installation; they do not establish atomic cutover or revocation of existing connections and in-flight requests.

Provider mutation acceptance was not treated as readiness. Before probing an attachment or detachment, the harness queried its immutable receipt and required the exact receipt, expected `READY`/`REVOKED` state, nonempty supervisor session/process identity, matching attachment epoch, provider-environment revision, config revision and policy hash, and all three installation flags. Each base update required its exact version and policy hash to be `LOADED` and active. The first and second attachment had the same config fingerprint and policy hash in this run but different epochs and receipts: fingerprints identify content, so they cannot substitute for attachment identity. The pinned [provider composition documentation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/providers/profiles.mdx) and [readiness/update protocol](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/openshell.proto) describe the separate base/provider layers and desired-versus-installed states.

A prospective adapter must validate the effective provider-composed authority against the requested network policy. For CLOSED, refuse incompatible providers or remove their endpoint authority and independently qualify the resulting closed behavior; an empty base policy is insufficient. Allowlisted operation must include provider endpoints in its translation and admission checks. These observations are consistent with documented composition, so this slice does not request an upstream behavior change or replace the lifecycle dependencies #1817/#1818.

### Reproduction, cleanup and scope

Use the preceding HTTP origin/control recipe, including immutable image references, the owned internal network, public DNS resolving the private-origin fixture names, `/probe` experiment files and signed gateway configuration. Run:

```sh
python /probe/openshell_composition_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

The experiment creates only unique workspace-scoped profile/provider names and one sandbox. Cleanup first confirms the original sandbox identity and verifies absence of its containers, volumes and recorded workload PID/start-times; then it confirms the original provider ID, deletes the provider/profile and observes `NOT_FOUND` for each, deletes the workspace and verifies absence. This remains the exclusive-writer disposable fixture described above, not proof that name-based delete is race-safe. The independent origin journal was retained before removing the exact origin/control containers and network; final labelled resource queries and temporary gateway/signing-directory checks were empty.

Validation: **384 focused tests passed**, including 22 new guards against saved-only, partial, stale, wrong-receipt and historical-policy acknowledgements. Focused Ruff, formatting, Pyright, documentation paths and this document's issue trackers passed. Full repository gate, wheel smoke tests and hosted CI were not run. Remaining composition coverage includes overlapping endpoints, multiple providers, credential injection and refresh, provider profile edits, global overrides, HTTPS composition, in-flight connection revocation, concurrent transitions, disconnect/reconnect and restart. No suite capability is promoted by this bounded result.

## Overlapping rules and synthetic credential binding - 2026-10-11

The opt-in [credential probe](../../../scripts/experiments/openshell_credentials_probe.py) verified **16 live observations** with one static synthetic provider on the unchanged official v0.1.2 images. Its profile declared GET-only access to `/provider/**`, a bearer credential environment key and `/usr/local/bin/python*`. A broad base rule independently allowed GET/POST for the same hostname and other control destinations, and also authorized a copied interpreter at `/sandbox/probe-python`. All HTTP destinations were the owned private origin; no real account credentials or external API endpoints were used. Passing this characterization records both working binding checks and limits of the composed policy; it is not credential-confinement conformance.

| Case | Observation | Contract implication |
|---|---|---|
| Guest credential material and allowed rewrite | The environment held an `openshell:resolve:` placeholder whose digest differed from the synthetic value. Supplying that placeholder in a Bearer header produced exactly the expected synthetic Authorization digest at the bound origin | Static substitution kept the synthetic value out of the tested environment variable. This is not a proof that guest code can never obtain credentials through other surfaces. |
| No supplied header | The bound request without an Authorization header reached the origin with no header | Static `auth_style: bearer` metadata did not automatically add authentication; this fixture uses explicit placeholder rewriting. |
| GET-only provider overlapping broad GET/POST base | POST to the provider path reached the origin with the substituted credential. After an exact loaded base revision removed POST, the same method received a structured policy 403 with no correlated origin request | A narrow provider allow rule is not a ceiling over broader traffic authority granted by the base policy. Validate the composed rules, not only the provider profile. |
| Path, hostname and port binding | Otherwise permitted requests outside `/provider/**`, to the other allowed hostname, or to allowed port 8001 returned 403 `credential_endpoint_mismatch` when carrying the placeholder. Matching requests without it reached the origin | Traffic permission did not broaden this static credential's host/port/path binding. |
| Different binary authorized by base policy | The interpreter copied to `/sandbox/probe-python`, outside the provider binary pattern but allowed by the base rule, successfully used the placeholder at the bound endpoint | Provider binary lists alone do not establish per-binary credential isolation. |
| Detach and retained placeholder | After the exact receipt reported `REVOKED`, newly launched processes lacked the environment key. A saved old placeholder returned 500 `credential_unavailable` with no correlated origin request; the base-authorized request without credentials still succeeded | Credential revocation and traffic revocation are separate. This checks new requests after acknowledgement, not in-flight connections. |

The initial matrix rejected its hypothesis that the narrower provider rule would block POST: it observed successful POST delivery with the exact synthetic credential digest, then verified cleanup. The completed matrix records that widening explicitly and adds the installed GET-only base-policy control. It does not relabel that first observation as a successful denial.

The origin's opt-in authorization observer records only SHA-256 of the received Authorization header, or null when absent; it never journals or echoes the header value. Each successful request required exactly one origin event with matching method, host, port, unique query marker and expected digest, plus the corresponding response echo. Refusals required the specific policy, binding or unavailable-credential error/status and no correlated origin event. The random synthetic value was generated in the control process and supplied only to its workspace-owned provider record; guest code obtained a placeholder from the installed environment. No real token was supplied, and neither the synthetic value nor a placeholder is included in the public research record.

The pinned [provider contract](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/providers/profiles.mdx) explicitly distinguishes network permission from endpoint-bound credential resolution and documents the lack of binary-scoped static injection. The [relay](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-supervisor-network/src/l7/relay.rs) distinguishes binding mismatch (403) from unavailable placeholders (500). These observations do not establish an upstream contract violation. A future adapter must consider every rule granting traffic to a credentialed destination and must not promise provider-only method restrictions or per-binary credential isolation from a profile's allow/binary list. Endpoint binding is useful evidence, but attached identity remains unqualified.

### Reproduction and remaining scope

Use the existing labelled HTTP origin/control recipe with immutable official image references, the owned private network, signed gateway configuration and current experiment files. Start the origin with `python /probe/openshell_egress_probe.py --serve-origin --record-authorization`, then run:

```sh
python /probe/openshell_credentials_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

The fixture is restricted to serial requests with fresh processes/connections, one static synthetic credential, HTTP headers, one overlapping base/provider arrangement and explicit enforcement. It does not qualify multiple providers, credential rotation/refresh, HTTPS credential composition, query/body/WebSocket rewriting, host/path normalization attacks, response-mediated secret exposure, existing-process environment revocation, concurrent updates, reconnect/restart, or in-flight request termination. No new upstream FR is filed for these documented semantics; adoption dependencies #1817/#1818 remain open.

Validation: **397 focused tests passed**, including 13 new credential evidence guards, plus scoped Ruff, formatting, Pyright, document paths and OpenShell tracker checks. The prior 19-case anonymous composition matrix was rerun after the provider-construction refactor, and the 19-case HTTP matrix after adding the optional header observer; both passed. Original sandbox container/volume/PID identities, provider/profile and workspace absence were verified after each experiment. Origin journals were retained, then the exact origin/control containers and network were removed; temporary gateway/signing directories and final labelled resources were absent. The full repository gate, wheel smoke and hosted CI were not run.

## Multiple static providers and retained credential revisions - 2026-10-11

The opt-in [rotation probe](../../../scripts/experiments/openshell_rotation_probe.py) verified **23 live observations** with two attached static synthetic providers on the same owned origin, distinct environment keys, and disjoint `/a/**` and `/b/**` endpoint bindings. A third workspace-owned provider deliberately reused A's environment key to test attachment rejection. The base policy independently allowed GET traffic, so credential refusals could be distinguished from lack of network permission. All official v0.1.2 image pins and the sentinel workload digest were unchanged. This qualifies a bounded manual static-value update, not automatic refresh, distributed rotation or upstream key retirement.

| Case | Measured result | Implication |
|---|---|---|
| Two providers | New guest processes received distinct placeholders; each resolved to its own expected Authorization digest at its own path | Both static bindings worked together in this fixture. |
| Cross-provider endpoint use | A's placeholder at B's path and B's placeholder at A's path returned 403 `credential_endpoint_mismatch`, with no corresponding origin request | Base traffic permission did not substitute one credential's endpoint authority for the other's. |
| Duplicate environment key | Attaching the third provider returned `FAILED_PRECONDITION`; the attached provider ID set remained exactly A/B and the sandbox resource version did not change. Both original bindings still worked | The tested attachment conflict was rejected without partial attachment or observed disruption. Updates/refresh-induced key collisions were not exercised. |
| Manual static update of A | After the exact update receipt reported installed `READY`, a fresh process resolved A's new placeholder to the replacement value; a saved old A placeholder still resolved to the original value. Fresh and saved B references continued to resolve B's original value | Ordinary static update readiness does not retarget existing references or certify retirement of the old upstream key. |
| Selective detach of A | After installed `REVOKED`, old and new A references were refused at both A and B paths with 403 `credential_endpoint_mismatch`; fresh processes had no A key. B's fresh and saved references continued to work | The measured refusal category differs from the single-provider case, but both old and new A references had no origin delivery. Other-provider service remained available. |
| Final detach of B | Its saved reference returned 500 `credential_unavailable`; new processes had neither key. A base-authorized request without credentials still succeeded | Revoking credential use remains separate from revoking network traffic. |

Each update wait was bound to the exact sole sandbox target, mutation ID, provider name/physical ID and provider resource version returned by `UpdateProvider`, followed by the existing exact installed-receipt oracle. Successful requests required one correlated method/host/path/port event and the precise expected Authorization digest; cross-binding and revoked-reference checks required the specific HTTP status/error and no corresponding origin event. The origin stored only header digests, never header values. Environment observations checked placeholder shape, distinctness and difference from each synthetic value; they did not print values or placeholder strings.

Retained references were saved as guest files and reused in later fresh processes. This establishes reference-resolution behavior, not a long-running process lifecycle or in-flight connection result. A first attempt expected the single-provider 500 after detaching A and refused to claim success when it observed a 403 instead; cleanup passed. The completed matrix preserved that distinction and added old/new A probes against B's path as well as A's. The pinned [runtime limitations](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/providers/profiles.mdx) document retained ordinary-static revisions. The [resolver](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-core/src/secrets.rs) classifies disallowed endpoint keys separately from unavailable placeholders; adapters should preserve the measured error category rather than infer revocation from a generic network failure.

The integration consequence is narrower than a general rotation guarantee: use readiness to establish that new launches can receive the new static reference, and use a separately qualified revocation/restart policy before declaring an old upstream credential retired. Do not apply these observations to gateway-managed refresh, which has a different documented reference lifecycle. No upstream behavior-change request follows from this documented ordinary-static behavior; lifecycle dependencies #1817/#1818 and attached-identity qualification remain open.

### Reproduction and validation

Use the existing owned HTTP origin/control recipe and start the origin with `--serve-origin --record-authorization`. The probe creates three temporary profiles/providers, attaches only two, and uses generated synthetic values with no real accounts. Execute:

```sh
python /probe/openshell_rotation_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

Validation: **409 focused tests passed**, including 12 update-target guards against missing/ambiguous targets, mismatched mutation/provider/sandbox identity and inconsistent revisions. The existing 16-case credential matrix also passed after parameterizing provider keys and endpoint paths. Scoped Ruff, formatting, Pyright, documentation paths and OpenShell tracker checks passed. Sandbox containers, volumes and recorded workload PID/start-times were independently absent after cleanup; every created provider/profile and workspace was checked absent. Origin journals were retained before removing the exact origin/control containers and network; temporary gateway/signing directories and final labelled resources were absent. Full repository gate, wheel smoke and hosted CI were not run.

Remaining coverage includes credential expiry, automatic refresh and stable handles, update/refresh collision rejection, provider profile changes, HTTPS composition, reconnect/restart, old-generation retention limits, concurrent mutation and in-flight revocation. This slice does not promote attached identity or claim that all secrets are inaccessible to guest code.

## Static credential expiry and restoration - 2026-10-11

The opt-in [expiry probe](../../../scripts/experiments/openshell_expiry_probe.py) exercises actual elapsed Unix-time expiry for two successive synthetic static credential generations. The first expiry is set 45 seconds from preparation start and the replacement expiry 30 seconds later. Expiry is not simulated by editing the gateway clock or database. The runtime, supervisor, sentinel workload and private recording origin use the same pins as the preceding credential matrices. Provider updates and detachment require exact installed receipts; every HTTP observation correlates a unique request marker with the independent origin journal and checks only an Authorization digest.

The final run passed **16 live observations**:

| Observation | Result |
|---|---|
| Original and replacement references before expiry | Both delivered their own expected credential digest. |
| Original reference after its expiry | 500 `credential_unavailable`, no origin hit; the unexpired replacement remained usable. |
| Saved replacement reference after its expiry | 500 `credential_unavailable`, no origin hit. |
| Fresh process launched after the second expiry | Still inherited a placeholder. Using it returned 500 `credential_unavailable`, with no origin hit. |
| Ordinary base-authorized traffic after expiry | Reached the origin without an Authorization header. |
| New value installed with explicit expiry clearing | A fresh reference delivered the new value; neither expired saved reference became usable again. |
| Acknowledged detachment | Fresh process environment no longer contained the credential key. |

The first attempt expected the new process environment to omit the expired key and stopped on its retained placeholder; owned cleanup still passed. The final probe characterizes that observation and adds a direct fail-closed request using the fresh process's expired placeholder. Presence of an environment key is not evidence that its credential is usable. This bounds the [upstream environment-building and retained-generation description](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/providers/profiles.mdx): the measured existing sandbox can launch with an expired placeholder, while resolution blocks forwarding. It does not establish how quickly every launch environment converges or how a new sandbox behaves when attached credentials have already expired.

Reproduce inside the owned control fixture with the origin's `--record-authorization` observer enabled:

```sh
python /probe/openshell_expiry_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

Both attempts independently verified sandbox container, volume and process absence, provider/profile deletion and workspace removal. The control/origin containers and dedicated network were removed after retaining the digest-only journal; temporary gateway key/database directories were absent. **409 focused regression tests passed**, along with scoped Ruff, formatting, Pyright, documentation paths and issue trackers. The new live matrix reuses the existing origin-attribution and exact-receipt guards. Full repository gate, wheels and hosted CI were not run.

This is serial HTTP characterization with fresh request processes and saved placeholders, not a production identity qualification or proof of in-flight revocation. Automatic refresh, refresh failures, stable handle epochs, restart/reconnect, concurrency and upstream key retirement remain unqualified. The next fixture can use the official gateway's loopback HTTP token-service allowance and synthetic OAuth client credentials, as exercised in the [pinned upstream refresh-handle test](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/e2e/rust/tests/provider_refresh_handles.rs). No fork or new upstream behavior-change request is needed for this slice; #1817/#1818 remain adoption dependencies and the creation-recovery FR remains a draft.

## Gateway-managed OAuth refresh and recovery - 2026-10-11

The opt-in [refresh probe](../../../scripts/experiments/openshell_refresh_probe.py) uses a loopback OAuth client-credentials issuer, generated client material and generated access tokens. The gateway, official runtime/supervisor images, sentinel workload and private HTTP recording origin remain pinned as in the preceding matrices. Only credential digests, generation numbers and bounded status fields enter the evidence. The issuer requires the expected client material and grant type; it never accepts real account credentials. The [official implementation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/crates/openshell-server/src/provider_refresh.rs) permits loopback HTTP token endpoints for local tests. This does not qualify issuer TLS or a real OAuth service.

The final run passed **26 live observations**:

| Observation | Result |
|---|---|
| Ordinary `UpdateProvider` attempts to replace a refresh-managed value | `FAILED_PRECONDITION`; provider metadata remained unchanged and the current token remained usable. |
| Worker-driven refresh without another rotate request | A second issuer generation appeared, and the saved guest reference eventually delivered that token. The fresh process placeholder was unchanged. |
| Explicit `RotateProviderCredential` | The same saved reference eventually delivered the replacement token; the fresh process placeholder was unchanged. |
| Explicit refresh reconfiguration | The old reference eventually returned 500 `credential_unavailable` with no correlated origin hit. Fresh processes received a different reference that delivered the current token. |
| Synthetic HTTP 503 token-endpoint failure | RPC `UNAVAILABLE`, status `error`, failure code `oauth_token_endpoint_retryable`, recovery action `RETRY`, and a next-refresh timestamp 60 seconds after the recorded error. The current token remained usable; a manual rotate recovered the same reference. |
| Synthetic `invalid_client` failure | RPC `FAILED_PRECONDITION`, status `configuration_required`, failure code `oauth_invalid_client`, recovery action `FIX_CONFIGURATION`, and a next-refresh timestamp 3,600 seconds after the recorded error. |
| Short-lived current token after configuration failure | Worked before its actual expiry, then its saved reference returned 500 `credential_unavailable` with no origin hit. Base-authorized traffic without credentials remained usable. |
| Reconfiguration and rotation after expiry | Installed a different usable reference while the previous epoch's saved reference stayed revoked. Acknowledged detachment removed the key from the next process environment. |

Refresh status is bound to exactly one physical provider ID, provider name, credential key and strategy. Issuer generation/expiry evidence is compared with gateway refresh status; data-plane evidence requires the exact Authorization digest at the independently recorded origin. The worker observation waits through the official one-minute worker interval, without invoking another rotate operation. The failure retry intervals above are **reported schedule metadata**, not an observed 60-second recovery retry or a real one-hour wait. Recovery after the transient failure was explicitly manual.

Refresh/configure replies are not an installed data-plane barrier. The probe retains all intermediate request observations while polling for convergence for at most 30 seconds. Old-token requests reached the origin after refresh status had advanced; old-epoch requests also reached it after reconfiguration returned, before eventual refusal. Therefore this evidence cannot justify immediate old-key retirement or instantaneous revocation. Attach/detach still use exact installed receipts. Reuse of saved references occurs in fresh processes/connections; it does not qualify a continuously running guest, in-flight requests or reconnect/restart behavior.

The first attempt stopped because it expected no retry timestamp for `invalid_client`. The pinned implementation and the detailed [refresh documentation](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/docs/how-it-works/providers/profiles.mdx) instead specify hourly configuration retries. A second attempt stopped after treating refusal of an already-expired reference as evidence that recovery had propagated. The final matrix verifies the retry schedule explicitly and requires positive delivery of the new token before asserting a changed launch reference; an expired-reference refusal alone cannot prove recovery. All three attempts independently verified sandbox containers, volumes and processes absent, plus provider/profile and workspace removal. The synthetic issuer stopped; temporary gateway key/database directories were absent, and the origin/control containers and dedicated network were removed after retaining the digest-only origin journal.

Reproduce with the owned recording origin and control fixture:

```sh
python /probe/openshell_refresh_probe.py --execute --image sha256:203544c2e3a55c0d66ee26752d572ba9c7b8f83f926f8bf08e234aeb8ca2afbd --origin-ip "$ORIGIN_IP" --config /opt/files-gateway.toml
```

**420 focused tests passed**, including 11 guards for refresh-status ownership, synthetic issuer errors, token generation and secret-free evidence. Scoped Ruff, formatting, Pyright, documentation paths and OpenShell issue trackers passed. Full repository gate, wheels and hosted CI were not run. Remaining identity coverage includes long-running processes, HTTPS credential composition, refresh-token grants and interactive reauthorization, automatic failure retry, co-minted credentials, concurrent mutations, disconnect/reconnect, restart and in-flight revocation. Attached identity remains unqualified. This documented behavior needs no new upstream FR from this slice; lifecycle dependencies #1817/#1818 remain open and the creation-recovery FR remains a draft.

## Official upstream dependencies and draft request - 2026-10-11

Use official NVIDIA/OpenShell source and releases for this integration. Do not use contributor forks, fork patches, or fork builds as dependency candidates. This supersedes the proposed next step of preparing a competing server implementation. The existing local reference model remains a test specification, not a supported implementation.

The suite dependency issues remain children of #1796: [#1818](https://github.com/sokolaidev/maf-extensions/issues/1818) tracks official conditional-delete delivery under [NVIDIA/OpenShell#3210](https://github.com/NVIDIA/OpenShell/issues/3210); [#1817](https://github.com/sokolaidev/maf-extensions/issues/1817) tracks the original-create recovery gap related to accepted [NVIDIA/OpenShell#3051](https://github.com/NVIDIA/OpenShell/issues/3051). Both upstream issues were open on 2026-10-11. The creation-recovery request below is a local draft, with no separate upstream issue yet. Reuse these downstream trackers instead of filing duplicate problems.

Official main at [`eeba0e7954c0fb4d8e9e2e29d1bfa68e290eb8b3`](https://github.com/NVIDIA/OpenShell/tree/eeba0e7954c0fb4d8e9e2e29d1bfa68e290eb8b3) still has a name-addressed public `DeleteSandboxRequest` without a caller-expected sandbox ID. Its [public protocol](https://github.com/NVIDIA/OpenShell/blob/eeba0e7954c0fb4d8e9e2e29d1bfa68e290eb8b3/proto/openshell.proto) documents 24-hour successful create replay. This is a current source comparison, not a new runtime qualification. The earlier retention experiment remains specific to v0.1.2 and injected persistence expiry.

Dependency completion requires an official deliverer and release or artifact revision, verified support for the required semantics, then passing downstream runtime probes. For conditional deletion, a client must refuse an unsupported server before dispatching a potentially unconditional delete; adding a protobuf field alone cannot establish that an older server enforces it. Retain the original/replacement barrier matrix and independently observe physical cleanup. For creation recovery, retain uncertain ownership until the original admission is authoritatively identified; no automatic ordinary-create replay is enabled by this draft. Upstream issue closure or source inspection alone does not close either downstream issue.

### Draft upstream feature request: read-only recovery of original sandbox creation

Draft status: not submitted. The following body is framed in OpenShell terms. Before submission, obtain the operator's own first-hand use statement required by [upstream contribution guidance](https://github.com/NVIDIA/OpenShell/blob/main/CONTRIBUTING.md); evaluation evidence below does not claim production use. The API example is notional and is not implemented. The draft does not request a fork or a parallel implementation of the mutation-reliability project.

**Proposed title:** `feat(api): inspect original sandbox creation without replaying the mutation`

**User Story**

An operator recovering an OpenShell client after a lost create response needs to determine which sandbox, if any, belongs to the original create attempt. The client may return after the successful replay window has expired. Recovery should inspect that attempt without starting another workload or adopting whichever sandbox currently occupies the requested name.

**Problem Statement**

`CreateSandbox(request_id=...)` supports mutation replay, but replaying a mutation and querying historical admission are different operations. Successful admission records have a bounded retention period; a later request can be a new admission. A current-name lookup also cannot identify the original attempt after deletion and replacement. A recovered client therefore cannot use either operation as an unconditional original-create receipt lookup.

**Impact / Why This Matters**

The conservative workaround is to keep the uncertain attempt pending, reserve its unique name indefinitely, and require operator-assisted reconciliation. This avoids accidental creation or adoption but prevents automatic recovery and verified cleanup after extended downtime. Even a never-reusing client allocator cannot infer what an interrupted server request committed from the current name alone.

**Proposed User-Facing Workflow**

1. Before dispatching create, persist a recovery selector that the server can bind to exactly one admission: authenticated principal, immutable workspace identity, request identity, and effective payload binding. If ordinary request IDs can be recycled, use a distinct non-recycled admission identity or an equivalent server-enforced generation mechanism available before dispatch; the design must work even when the first response is lost.
2. Submit `CreateSandbox` and retain the selector if the response is lost or the client crashes.
3. Query a read-only recovery API with that selector. For example, `LookupSandboxCreation(recovery_selector)` returns `CONFIRMED(original_admission_id, original_sandbox_id)`, `UNRESOLVED`, `ABSENT_AT_OBSERVATION`, or `HISTORY_UNAVAILABLE`; authorization and binding errors remain explicit. These names illustrate the contract rather than prescribe the wire schema.
4. Adopt only a confirmed original identity after validating the binding. Other outcomes keep the attempt pending. A confirmation identifies historical creation; it does not promise that the resource still exists, is Ready, or has been physically cleaned up.

An absent observation is not terminal non-creation: an in-flight request may still commit. Releasing the reservation would require a separately specified atomic fence against late admission and effects. This request does not add that cancellation/fencing API.

**Acceptance Criteria**

- Lookup never admits or executes a mutation, provisions a workload, or invokes mutation hooks with provisioning side effects, including for missing, expired and unresolved receipts.
- The recovery selector cannot silently identify a later admission after successful-replay expiry or request-ID reuse. Any additional identity/generation mechanism is bound by the server, not merely asserted by the client.
- A confirmed result returns the immutable original admission and sandbox identity. Deleting the original or creating a same-name replacement never substitutes the replacement; expired historical evidence produces an explicit unavailable result.
- Current authorization is enforced for the original scope. Workspace replacement, changed caller identity and payload mismatch cannot retrieve or adopt another attempt's outcome; cross-tenant existence is not leaked.
- Restart with retained state preserves the binding. Document the behavior and support boundary for receipt garbage collection and control-state loss/restore. If continuity cannot be established, recovery fails closed rather than confirming a later admission. A caller-supplied epoch alone is insufficient.
- Tests cover lost responses, delayed admission after an absent observation, original deletion/name reuse, retained-state restart, successful-replay expiry, unresolved admission, authorization changes and binding mismatch. Tests check for absence of new admission/workload effects as well as the returned status.
- SDK documentation distinguishes historical confirmation, current resource state, mutation replay and physical cleanup. Existing bounded mutation replay can remain available for callers that deliberately choose it.

**Alternatives Considered**

- Retry `CreateSandbox` with the same request ID: useful within the qualified retained-history contract, but after successful receipt expiry it can admit a different create; it cannot serve as a non-mutating historical lookup.
- Use `GetSandbox(name)` or client labels: these describe a current resource and do not prove which original admission created it. A labels-based recovery design would need its own enforced immutable binding and authority contract.
- Keep a client journal or dedicated orchestration service: useful for ownership and reservations, but it cannot atomically record the gateway's admission outcome when the response is lost. Middleware/interceptors could support a design only if they can durably bind the actual committed admission and expose authorized read-only history; ordinary observation callbacks alone do not establish that guarantee.
- Retain all receipts forever: unnecessary for the lookup contract. Bounded retention with explicit unavailable history is acceptable, provided lookup never executes creation or aliases a later admission.
- Introduce a generic long-running-operations service: potentially broader than this specific recovery requirement; an extension to the existing admission store may suffice.

**Evidence and Relationship to Existing Work**

Related: [NVIDIA/OpenShell#3051](https://github.com/NVIDIA/OpenShell/issues/3051), which owns general mutation reliability and has delivered admission/replay work. This proposal requests read-only original-create inspection and explicit history boundaries; it does not replace that work. Conditional deletion remains a separate capability under [NVIDIA/OpenShell#3210](https://github.com/NVIDIA/OpenShell/issues/3210).

Evaluation of OpenShell v0.1.2 at `6648bd0c290efbc41ba131ee9831ee45cd431f94` exercised immediate replay and retained-state gateway restart. In a disposable Docker-backed fixture, injecting expiry into a successful admission record allowed the same create request to produce a new sandbox; a subsequent replay then referred to that new result. This was an injected-expiry experiment, not a real 24-hour wait, production deployment, or a claim that ordinary bounded replay violates its documented contract. The current official protocol at `eeba0e7954c0fb4d8e9e2e29d1bfa68e290eb8b3` still documents the bounded successful replay window; current-head runtime behavior has not been rerun. The requested inspection surface must be qualified separately from any offline model.

## Status

| Work | Status | Tracking |
|---|---|---|
| OpenShell backend adoption | Open; general adoption blocked on lifecycle prerequisites and remaining conformance | [#1796](https://github.com/sokolaidev/maf-extensions/issues/1796) (open) |
| Pinned SDK/runtime spike | Completed with a defer decision; qualification continues in the follow-ups below | [#1797](https://github.com/sokolaidev/maf-extensions/issues/1797) (closed) by [#1848](https://github.com/sokolaidev/maf-extensions/pull/1848) (merged) |
| Non-mutating original-create receipts | Blocked on official upstream recovery contract; local FR draft and offline model prepared | [#1817](https://github.com/sokolaidev/maf-extensions/issues/1817) (open) |
| Conditional physical-instance deletion | Blocked on official upstream conditional deletion; offline barrier harness prepared | [#1818](https://github.com/sokolaidev/maf-extensions/issues/1818) (open) |
| Native Linux runtime and file conformance | Native baseline measured; lifecycle and concurrent file confinement blocked | [#1833](https://github.com/sokolaidev/maf-extensions/issues/1833) (open); baseline by [#2115](https://github.com/sokolaidev/maf-extensions/pull/2115) (merged) |
| Effective egress translation and revocation | Open; native HTTP cutover measured; owned HTTPS blocked by supervisor destination trust; broader contracts unqualified | [#1834](https://github.com/sokolaidev/maf-extensions/issues/1834) (open) |
| Provider credential lifecycle | Open; deferred from the spike | [#1835](https://github.com/sokolaidev/maf-extensions/issues/1835) (open) |

## Adoption follow-up

The spike ends with a defer outcome. General adoption stays blocked on official read-only creation recovery (#1817) and conditional physical-instance deletion (#1818). A separately qualified restricted authority profile would require an explicit new adoption decision; unique names and a client journal alone do not close those gaps.

Native Linux runtime/file qualification remains in #1833 after the hosted baseline and confirmed working-directory replacement escape above; effective egress translation and revocation remain in #1834, and provider credential lifecycle in #1835. Network partitions, permanent control-plane loss and general lifecycle recovery remain unqualified. The bounded observations above are not production support, policy containment proof is not runtime qualification, and refresh/configure replies are not immediate data-plane barriers.

Epic #1796 stays open. Do not create or release an adapter as part of this research closeout. No snapshot/reset, native runtime execution, host tools or attached identity capability is declared. Package implementation, supported deployment documentation, package/CI gates and release consideration belong to a later approved adoption scope.
