# OpenShell backend spike

> Research: OpenShell v0.1.2 contract characterization for [#1797](https://github.com/sokolaidev/maf-extensions/issues/1797), under [#1796](https://github.com/sokolaidev/maf-extensions/issues/1796). Installed-SDK fixture probes and gateway startup were measured on 2026-10-08; live Docker workload/lifecycle cases were measured on 2026-10-09 and OIDC authorization on 2026-10-10. No suite adapter or runtime is qualified.

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

## Remaining qualification and preliminary decision

Continue the spike, but do not ship a thin convenience-SDK wrapper. The disposable Docker experiment demonstrates byte preservation, aggregate capture refusal, fractional caller deadlines and verified whole-sandbox cleanup under the listed conditions. Live replacement testing now establishes that the name-addressed API fails the general exact-instance disposal requirement without enforceable exclusive ownership. Unique physical names and exclusive mutation authority remain test assumptions, not proof against that race.

Resolve the lifecycle authority boundary before promoting this execution strategy to an adapter. The restricted OIDC profile now has bounded authorization and local allocation/crash evidence, but still needs automatic reconciliation of uncertain creates and a qualified operational authority. Pinned replay now has immediate/restart evidence and an exercised expiry counterexample. It is not a general automatic-reconciliation solution. Specify a server-enforced replay-only receipt contract or explicitly qualify a bounded recovery policy before adopting uncertain creates. Until then, uncertain entries remain pending without name reuse or name-only deletion. Persisted recovery covers a confirmed owner after client SIGKILL and temporary gateway loss with preserved state; automatic uncertain-create reconciliation, distributed authority and permanent control-plane loss remain unresolved. Then qualify storage-base preparation, files, exec, deadlines, process cleanup and call-scope disposal through the shared probes. Withhold snapshot/reset, native runtime execution, host tools and attached identity until their contracts are independently established.

Egress translation remains source-only work beyond the single denied direct connection. OpenShell's [policy schema](https://github.com/NVIDIA/OpenShell/blob/6648bd0c290efbc41ba131ee9831ee45cd431f94/proto/sandbox.proto) distinguishes single-label and recursive host globs and defaults unspecified L7 enforcement to audit. The suite's [host/path semantics](../network.md) require deliberate translation and blocking enforcement. Test allowed success alongside denied hosts, methods, paths and direct routes; include provider-composed policy and later revisions. Policy containment checks do not replace runtime observations.

Docker Desktop startup is no longer blocking the authorization matrix; its 17 observations and cleanup passed on 2026-10-10. The initial SDK probe passed its characterization assertions; completed live experiments cover the timeout gap, owned execution/disposal, controlled replacement, immediate deletion replay, the recorded recovery cases, bounded OIDC authorization matrix local allocation crash/refusal cases, and the bounded replay/restart/injected-retention matrix. The full repository gate, wheel smoke tests, network partitions, permanent control-plane loss, filesystem confinement, full egress policy tests and shared conformance have not run for this spike. Neither issue is ready to close.
