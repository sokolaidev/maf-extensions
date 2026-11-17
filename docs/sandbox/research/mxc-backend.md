# MXC integration research, design and qualification

> This is the single MXC research record for the suite: accepted design decisions, release assessments, candidate-specific evidence and reproduction instructions. The experimental host runtime is packaged as [`maf-sandbox-mxc`](../../../packages/maf-sandbox-mxc/README.md); native build recipes and qualification probes remain under `scripts/experiments`. The package does not yet implement `SandboxBackend`.

Read the [accepted design](#accepted-design) and [status table](#status) for the selected contract and its implementation boundaries. The release assessments and experiment sections preserve the findings at their recorded source revisions; historical proposals, pending gates and conclusions do not override later qualified evidence. A successful probe establishes only the behavior, platform and profile it measured. Closed networking remains the baseline, and production adapter conformance remains unqualified.

Generated reports stay outside Git. Hosted evidence is linked by workflow run and source identity; retained artifacts may expire. Source pins and patch manifests remain versioned inputs. Reproduction commands below run from the repository root unless they explicitly select another directory; bare asset filenames belong to the linked tooling directory for that profile.

Navigation:

- [Accepted design](#accepted-design)
- [Implementation status](#status)
- [Release assessments and implementation history](#release-assessments-and-implementation-history)
- [Native output research](#native-output-research)
- [Original Hyperlight feasibility probe](#original-hyperlight-feasibility-probe)
- [Original feasibility conclusion](#original-feasibility-conclusion)
- [MXC 0.9 session overlay reproduction](#mxc-09-session-overlay-reproduction)
- [Checkpoint publication and recovery experiments](#checkpoint-publication-and-recovery-experiments)
- [Owner-liveness experiment](#owner-liveness-experiment)
- [Console and bounded-capture experiments](#console-and-bounded-capture-experiments)
- [MXC 1.0 and 1.0.1 profile reproduction](#mxc-10-and-101-profile-reproduction)
- [MXC 1.1 profile reproduction](#mxc-11-profile-reproduction)
- [Byte-stream profile reproduction](#byte-stream-profile-reproduction)
- [Bounded-file profile reproduction](#bounded-file-profile-reproduction)
- [Upstream Unikraft comparison](#upstream-unikraft-comparison)
- [Qualification workflows and evidence retention](#qualification-workflows-and-evidence-retention)

## Accepted design

The selected design uses MXC Hyperlight to supply a rich Python runtime for CodeAct through the sandbox router. These decisions record the first-version scope selected on 2026-10-02. The [status table](#status) distinguishes those requirements from implementation and validation. The [release assessments and implementation history](#release-assessments-and-implementation-history) contain the rationale, candidate transport and feasibility questions.

### Agreed scope

The host runtime now lives in the unreleased [`maf-sandbox-mxc`](../../../packages/maf-sandbox-mxc/README.md) package: durable sessions, helper supervision, bounded file/stream transports and mutual-TLS coordination. Build recipes and qualification probes remain repository tooling. The package does not yet implement `SandboxBackend`; its packaging and offline tests do not extend the native qualification recorded for each helper/profile.

| Decision | Selected behavior |
|---|---|
| First MXC backend | Hyperlight with a rich Python environment |
| Workload | CodeAct, including code/text execution, file inputs and output artifacts |
| First-version hosts | Windows x86-64 with WHP and Linux x86-64 with KVM, independently qualified |
| Default state lifetime | Fresh Python state per call |
| Optional state lifetime | Host-enabled persistent Python state per conversation |
| Restart recovery | Persistent sessions recover from a durable checkpoint |
| Checkpoint timing | After every successful persistent tool call, before acknowledging success |
| Recovery placement | Same machine by default; another compatible machine when requested through host configuration |
| Guest networking | Closed by default; optional host allowlist where support and enforcement are verified |

The first usable version includes both text results and file-based analysis. A text-only compatibility probe is an intermediate engineering step, not the delivery scope. Additional MXC containment backends and non-Python runtimes remain outside this first version. The existing direct Hyperlight backend remains separately available.

### Runtime and files

The CodeAct runtime contract describes the verified Python version, available imports, filesystem access, network policy and state lifetime. A runtime's name alone does not establish support for a library or operation. Dependencies come from a prepared, pinned environment; the network option does not authorize package installation during a tool call.

The application supplies selected input files and receives bounded artifacts through the existing file-store and output-sink contracts. Each session has a private workspace. The adapter enforces file count and byte limits, confined access and safe collection; a host mount alone does not establish those guarantees. Temporary call files and retained session files have distinct lifetimes, including when Python objects retain paths to them.

Input files remain available within the lifecycle requested by the host; enabling persistent Python does not automatically make every uploaded input persistent. The lifecycle belongs to trusted host configuration and must respect the sandbox isolation and cleanup requirements. The first version supports host-selected call and session lifecycles: call files are reclaimed before durable success; session files remain available across successful calls and restart until the session ends. Explicit host-release and time-based input expiry are deferred. Omitting the lifecycle selects call lifetime; retaining an input across calls requires an explicit host selection of session lifetime. An upload targeting an existing session filename is refused unless the host explicitly requests replacement. Uploaded inputs are writable by guest code by default within their requested lifecycle. Host upload replacement authority remains separate from guest writes. Retained delivery artifacts follow the separately selected result-retention promise.

Per-call transfers default to at most 64 input files and 16 MiB of input bytes, and independently at most 64 requested artifact files and 16 MiB of artifact bytes. The host may configure these bounds. These transfer allowances are separate from retained workspace capacity, checkpoint capacity and durable delivery retention. The live workspace, including retained inputs and guest-created files, defaults to 64 MiB and 256 files, configurable by the host. Admission counts existing retained files together with proposed uploads; an explicit replacement substitutes its new size rather than adding a second retained copy. Temporary copies remain subject to separate scratch accounting. A guest operation that would exceed the workspace allowance returns a catchable filesystem capacity error before changing the affected file; it does not automatically fail the entire call. Code may recover by reclaiming space or reducing its write. Subsequent completion still requires all requested artifacts and a valid checkpoint. This guarantee applies to each write or resize operation, not an entire buffered file-writing sequence that may have already completed earlier operations.

Every requested artifact must be present, safe to collect and within the configured bounds before a call can succeed. A missing, unsafe or oversized artifact fails the entire call: no successful result is published and the checkpoint does not advance. The previous committed checkpoint and retained delivery results remain available under their existing retention rules. Guest writes from the failed call cannot be adopted as committed session state.

Acceptance includes both a NumPy/pandas calculation returning text and a workflow that reads a supplied CSV, analyzes it with pandas and returns a matplotlib chart. Both workflows pass on Windows and Linux. No speed, memory or cost advantage is assumed.

### Bounded console capture

The temporary native collector uses **truncate and continue** with a host-configurable default of **1 MiB (1,048,576 bytes) of combined console output per call**. It retains a valid UTF-8 prefix, discards subsequent text and reports omitted bytes with an explicit counter-saturation flag. Reaching the limit without omitting bytes is not truncation. Startup/resume has a separate bounded interval; each call starts with a fresh budget.

Truncation does not fail execution or retire the session. A call that otherwise succeeds publishes its checkpoint, retained output and host-owned truncation metadata together before acknowledgment, and recovery redelivers the same result. Guest errors, timeouts, cancellation and owner loss keep their existing failure behavior and cannot publish success.

The collector checks the budget before buffering and disables automatic mirroring to process stdout. Its separately pinned runtime/session overlays are removable through the [experiment tooling](#apply-and-remove-the-overlay). The combined text channel does not provide separate streams, arbitrary binary fidelity, correction of the kernel's silent 4096-byte truncation, or a process-wide memory limit. Omission counters describe only bytes received by HostPrint. The existing two-MiB serialized-result limit still applies independently of the configured console budget.

Qualification covers exact and exceeded budgets, continued execution, UTF-8 boundaries, durable publication and lost acknowledgments. Failed execution and malformed control refuse success. Replacing the overlay requires equivalent behavior and renewed qualification on both platforms. The [upstream capture request](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140) includes a contribution offer; the separate kernel profile has its own later decision below.

### Separate byte-stream profile

On 2026-10-08 the maintainer selected **1 MiB per stream, truncate and continue** for the new MXC 1.0 kernel profile. stdout and stderr each retain an arbitrary-byte prefix and report omitted bytes with a saturation flag. Guest writes still succeed in full when only retention is truncated; the host resets both budgets between calls. Malformed native frames retire the session instead of producing a successful checkpoint.

The host publishes a bounded control record separately from the two byte payloads and never parses guest output as completion. The experimental format-4 store explicitly reserves three MiB for the base64 result envelope before execution; the previous two-MiB default is unchanged. Checkpoint and result publication remains one transaction, and retries return the saved envelope without executing again. A changed kernel uses a new snapshot identity; existing profiles are not silently upgraded.

Implementation and both-host qualification are tracked in [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786) and the [native-output research record](#native-output-research). This selected policy is not a production-support claim.

### State and ownership

Fresh mode creates call-scoped state and disposes it after result delivery. Persistent mode retains Python variables and dataframes across calls within the same trusted conversation, while separating scope, agent and kind. Access to a persistent session is serialized through execution, artifact handling and checkpoint commitment.

Persistence is controlled by the host, not a model-selected tool argument. Host isolation and cleanup requirements remain authoritative: a host requiring call isolation cannot be served a conversation-scoped session. A change in runtime, state mode or execution policy requires a new instance or an explicitly validated transition.

Persistent cleanup preserves the intended interpreter state while reclaiming temporary call resources. Resetting a baseline is not equivalent to retaining Python state. Background guest threads or processes cannot be allowed to race artifact collection or checkpoint capture merely because calls are serialized.

### Durable completion and recovery

Each successful call in persistent mode produces a durable checkpoint before success is acknowledged. The checkpoint represents consistent Python state, session files and the metadata needed to recover the session and identify the completed call. A partially written checkpoint never replaces the previous committed checkpoint.

If Python finishes but checkpoint commitment fails, the call is not reported as a durable success. The previous checkpoint remains available, and later calls cannot silently continue from uncommitted state. If commitment succeeds but acknowledgment is lost, recovery distinguishes delivering the recorded result from executing the program again. Artifact delivery follows the same call identity so retries do not silently duplicate outputs.

Recovery restores a committed checkpoint rather than silently creating an empty interpreter. Corrupt, incomplete or incompatible checkpoints cause an explicit failure. A failed or interrupted call reports the applicable recovery point; its code is not automatically replayed. Checkpoint recovery does not undo external effects or make network requests exactly-once.

Checkpoint metadata binds trusted ownership, physical generation, runtime and guest artifacts, architecture, policy and format compatibility. The receiving host checks its current policy before execution. Checkpoint storage and recovery credentials remain outside the guest. Checkpoint access and retention reflect the sensitivity of the state they contain.

The existing `SNAPSHOT` capability promises reset to a pre-input baseline. It is not, by itself, a durable session-checkpoint API. Capturing and restoring a modified interpreter, including the supported treatment of Python objects, open files, threads and sockets, requires separate qualification. Preserving files alone does not fulfill interpreter recovery.

### Session lifetime

Persistent sessions have no automatic idle expiry by default. The host can delete a session explicitly or configure an idle timeout. Storage quotas still apply while a session is retained; disabling idle expiry does not authorize unbounded storage. Session idle expiry cannot shorten an already committed result-retention promise. The experimental policy starts an interval at creation or completed call cleanup; running calls and unresolved cleanup are protected. Reopening, checkpoint reads and matching result retries do not reset that interval. Host clock uncertainty uses a separate persisted forgiveness budget, with the same five-minute default and bounded grants as result retention.

#### Session deletion

Deleting a session retires its identity, refuses new execution and stops active execution before reclaiming the checkpoint. Retirement is durable: the same session identity cannot later create fresh state. Cleanup remains incomplete while execution termination or safe reclamation is unresolved, and outstanding reservations remain charged until their data are accounted for or reclaimed.

Completed results and delivery artifacts remain available for matching retries through their promised expiry, including any applicable clock-forgiveness allowance. Checkpoint reclamation removes only data no longer referenced by retained deliveries or active readers. Retained results and identity records remain charged to the retired session and the shared store; deletion cannot bypass either quota or make a completed call eligible for execution again.

### Result retention

Completed call results and their delivery artifacts remain available for retry for 24 hours from durable commit by default. The host configures the retention window; each committed call records its expiry so later configuration changes cannot shorten an existing promise. Reading or retrying a result does not extend its expiry.

Within the retention window, a matching retry receives the identical committed result and artifacts without executing the program again. After expiry, a retry receives an explicit `result_expired` outcome. The store retains enough durable call identity to distinguish an expired call from a new one; removing a result cannot make its call eligible for execution again.

Storage pressure refuses new work rather than evicting results or artifacts before their promised expiry. Result retention is separate from checkpoint retention: the latest committed checkpoint remains available while the session is recoverable. Superseded checkpoints and chunks can be collected only when no live state, pending publication or retained result/artifact references them. A 24-hour result window does not require retaining 24 hours of complete VM checkpoints.

#### Clock forgiveness

Each completed result has a grace budget of five minutes by default for clock uncertainty, configurable by the host. The budget caps the extra retention granted because the host clock is uncertain. While expiry is deferred within that budget, matching retries continue to receive the saved result and artifacts. Once the budget is exhausted, expiry proceeds using the host clock; uncertainty does not indefinitely suspend expiry cleanup.

The configured allowance and consumed budget are durably associated with the result. Retries, restarts and repeated clock anomalies do not replenish it. The base expiry remains fixed, and the latest checkpoint's recoverability remains independent of result expiry. The local implementation uses the accounting procedure below; its crash behavior requires qualification before use.

The retry window is evaluated under this bounded clock-forgiveness policy. A sufficiently incorrect host clock can shorten the actual elapsed retry window; the policy does not guarantee 24 hours of real elapsed retention under arbitrary clock failure. Storage pressure still cannot shorten the policy's retention period, and expired call identity remains protected against re-execution.

#### Durable clock-budget accounting

A delivery record persists its base UTC expiry, original grace allowance, remaining grace and terminal delivery state. The outer grace deadline is the base expiry plus the original allowance; it is never calculated from the time of a retry. Both the allowance and base expiry are fixed at publication, so later configuration changes cannot replenish the budget or change an existing record's deadlines. The first implementation stores integer time values and takes UTC and monotonic observations through an injectable clock.

During ownership, the clock observer compares UTC progress with elapsed monotonic time. A backwards UTC step or a discrepancy greater than one second, after allowing for the sampling interval, marks affected retained results uncertain. The discrepancy is measured against the owner's initial paired observation so successive small differences cannot silently reset the comparison. Existing results are conservatively uncertain after ownership recovery because process-local observations cannot establish clock continuity through downtime. Uncertainty stays attached to an affected result until expiry; a later plausible clock sample does not reset its budget. The one-second detection tolerance is an implementation parameter distinct from the host's five-minute grace allowance.

Expiry and retry use the same decision under session ownership:

| Condition | Decision |
|---|---|
| Delivery already marked expired | Return `result_expired`, even if UTC later moves backwards |
| UTC is before the base expiry | Retain and permit matching replay; no forgiveness grant is needed |
| UTC has reached the base expiry, without observed uncertainty | Mark delivery expired |
| UTC has reached the outer grace deadline | Mark delivery expired; unused forgiveness cannot start a later window |
| UTC is between the two deadlines, with uncertainty and remaining grace or a valid grant | Permit matching replay under a prepaid grace grant |
| No grace remains and no valid grant exists | Use host UTC and mark delivery expired when it has reached the base expiry |

A grant covers at most one second, shortened to the remaining budget and the time left before the outer deadline. Before granting it, a transaction deducts its full duration from the persisted budget and records the owner generation. Its local monotonic deadline is calculated before that transaction, so a slow commit cannot extend the grant. The grant becomes usable only after confirmed commit, is shared by retries within that interval, and is never refunded. A crash or ownership change forfeits its unused part; the next owner can spend only the persisted remainder. Thus each crash can lose at most one outstanding one-second grant per result, while repeated crashes cannot recreate grace. No grant can override the UTC outer deadline or an expired delivery state. Python's [monotonic clock contract](https://docs.python.org/3/library/time.html#time.monotonic) supplies elapsed-time measurement, not a portable timestamp to restore after reboot.

Failure to persist a grant or expiry returns an explicit storage failure; it cannot authorize execution of the call or deletion of its delivery bytes. Marking delivery expired and removing its retention references occur atomically, with physical collection handled separately. Expiry is evaluated on access and collection: retained bytes awaiting a cleanup pass do not authorize late replay. A clock stuck before the base expiry can still delay expiry under the selected host-clock fallback; the grace budget limits additional forgiveness, not real elapsed retention under arbitrary clock failure.

### Storage quotas

Durable session storage has both a per-session quota and an aggregate quota across the shared store. Both limits apply: a single session cannot consume the entire store allowance, and individually compliant sessions cannot collectively exceed the store allowance. Exhausting either quota refuses new work without shortening existing result-retention promises.

Enabling persistence requires the host to configure both quota values explicitly. There are no built-in quota defaults and no implicit unlimited mode. Missing or invalid quota configuration refuses persistent-session admission; it does not silently fall back to fresh state.

Both quotas measure logical storage usage: uncompressed retained checkpoint, result and artifact bytes, bounded metadata charges, and outstanding reservations. Compression and deduplication reduce physical storage without increasing admission capacity. Each retained object is charged in full to its owning session; artifact bytes embedded in a result are counted there once. Expired-call identity records remain charged after their payloads are collected.

Physical disk usage has separate safeguards covering restore copies, candidate exports, database pages and journals, compression overhead, diagnostics and cleanup. Temporary storage and concurrency are bounded independently; logical quota availability is not a promise of available filesystem space. Disk exhaustion refuses durable success and preserves the previous committed recovery point. Logical collection does not by itself establish physical disk reclamation. New experimental roots may select a fixed physical policy: a SQLite main-file ceiling, transaction headroom and minimum free-space floor. Every owner reapplies the ceiling; admission and publication check remaining database growth and outstanding scratch allowances under the write transaction. The checks observe volume capacity without reserving it against unrelated writers. Existing-root migration is separate; [the experimental contract](#optional-physical-storage-safeguards) defines the scope.

Before executing a new persistent call, the store atomically reserves capacity against both quotas for the maximum permitted checkpoint and result, including retained delivery artifacts. The previous committed checkpoint, unexpired results and existing reservations remain charged during admission. If either quota cannot accommodate the reservation, the call is refused before guest execution; replaying an existing committed result does not require a new execution reservation.

The reservation remains in force through checkpoint preparation and atomic publication. Publication converts reserved capacity into retained usage and releases the unused remainder; a failed or interrupted call releases capacity only after its stored and temporary data are accounted for or reclaimed. This prevents quota exhaustion caused by competing admissions, but does not guarantee filesystem capacity or eliminate storage failures. Failed publication preserves the previous committed recovery point and cannot acknowledge durable success.

The [storage retention proposal](#storage-retention-follow-up-proposal) records the current store gaps, proposed transaction boundary and qualification sequence. Logical-byte accounting, the local SQLite transaction boundary and bounded clock forgiveness are selected. The separate [shared-store core](#shared-store-accounting-increment) implements these procedures. The [acceptance record](#storage-and-retention-acceptance) separates host-store controls from fixed-workload native qualification on Linux/KVM and Windows/WHP, including the later physical-storage and pre-launch recovery results. Production adapter integration remains separate.

### Local storage implementation

The first storage implementation uses one local SQLite database per configured shared-store root, containing all admitted sessions. Session identity, call outcomes, checkpoint/result content and references, quota accounting and reservations share that transaction boundary. Admission checks both quotas and records the reservation atomically; publication commits the new checkpoint, saved delivery record and reservation conversion together.

Each session retains exclusive operating-system ownership through a session-specific lock, acquired before database transactions. Guest execution runs outside a database write transaction. Database writes serialize; checkpoint publication latency and contention require qualification before claiming throughput across sessions.

This implementation uses a private local filesystem and supports same-machine recovery. SQLite is the current implementation choice, not a requirement on a future remote storage provider. Network filesystems, machine replacement and distributed fencing remain separate qualification work; selecting SQLite does not reduce the agreed host-configured cross-machine recovery scope.

#### Reservation accounting and launch

Each reservation binds the store, session, call, request digest, owner generation and a host-generated scratch token. Its logical charge is the maximum new checkpoint bytes plus maximum serialized result bytes, separately retained artifact bytes and bounded metadata for the new records and manifests. Artifact bytes embedded in the result are not added again. Existing retained state remains charged throughout admission. Versioned metadata accounting includes bounded names, inventories, reference lists and expired/retired identity records; a metadata field with no established bound cannot enter the format. Compression, deduplication and expected later collection are not admission credits.

The shared store also records separate temporary-space allowances for restore copies, candidate export, request/control/output files and cleanup, including their file-count bounds. Publication does not release these allowances while scratch remains. Filesystem capacity checks additionally cover database allocation and transaction headroom; those checks cannot promise space against unrelated writers. The native exporter must enforce candidate bounds while writing, or use a qualified bounded storage mechanism. Rejecting an oversized completed export alone does not bound temporary storage.

Under the session lock, admission starts a write transaction, checks an existing call before considering new work, validates active session ownership, and compares retained usage plus reservations plus the new charge with each quota. It persists the pending call, logical reservation and temporary-space allowance together before making scratch or starting a helper. A matching committed retry needs no new execution reservation, including for a retired session. New work on a retired session refuses. Counter updates and row changes share the transaction, with overflow and negative balances rejected. SQLite's [transaction rules](https://www.sqlite.org/lang_transaction.html) determine contention handling; a busy or failed commit never authorizes execution.

Scratch lives under the store's managed scratch root at the recorded token, using confined access and no links. The host creates it only after reservation commit. A launched helper remains blocked on its private startup header while the supervisor persists its OS process identity, including creation identity and machine boot identity where required to disambiguate PID reuse. Only confirmed persistence permits the header that enables guest restore and execution. Loss of the host before that point leaves a helper without execution authority. The existing [owner gate](#owner-liveness-experiment) provides the native ordering; the optional journaled supervisor now records process identity before releasing it, with fixed-workload Linux/KVM evidence and Windows/WHP native qualification pending.

Publication requires verified successful helper termination and a bounded, validated candidate. One transaction checks ownership, publishes checkpoint and delivery content, advances the current checkpoint and converts the logical reservation to actual retained usage. It releases unused logical capacity while retaining the separate allowance for remaining scratch. Previous checkpoint references remain charged until safe collection removes them. A commit whose outcome is unknown is resolved by reopening the store and inspecting the durable call record, never by rerunning the program.

#### Reservation recovery

A replacement owner acquires the session lock and advances only that session's generation before authorizing new work. It reconciles durable call records with outstanding reservations and recorded scratch. Acquiring the host lock alone does not prove that the native helper has exited: recovery checks the recorded process identity and establishes termination before reclaiming its files or admitting replacement execution. A reused PID never authorizes killing the unrelated process. An uninspectable process, unknown writer or corrupt inventory leaves cleanup incomplete and its allowance charged; reservation age is not a reclamation signal.

| Durable state after interruption | Recovery action |
|---|---|
| No committed admission | No authorized execution exists; unexpected managed scratch is quarantined for reconciliation, not treated as free capacity |
| Reserved, with no execution-enabled helper | Reclaim known private scratch and release unused allowances after cleanup; retain interrupted call identity |
| Reserved, with execution-enabled helper | Establish native termination, mark the call interrupted, preserve the previous checkpoint and reconcile scratch |
| Checkpoint/result publication rolled back | Preserve the previous checkpoint and pending reservation until interruption and cleanup are recorded |
| Call committed, acknowledgment missing | Preserve saved delivery and new checkpoint; redeliver according to expiry policy; reconcile only residual scratch |
| Cleanup partly complete | Recheck the same recorded token and finish idempotently; never decrement an allowance twice |
| Unknown or corrupt call/reservation/reference state | Refuse affected recovery and reclamation; do not infer a fresh call from missing payload |

Cleanup first records its intent while retaining the allowance, then removes only known private files after excluding writers. A final transaction releases the allowance once reclamation is established under the filesystem's durability contract. A crash after deletion but before release leaves conservative over-accounting that recovery can repair; reversing that order could admit work against files still consuming space. Missing files alone do not prove safe cleanup while their writer can still recreate them. Unknown scratch blocks further scratch admission until its ownership and physical charge are reconciled. Collectors acquire one session lock at a time before write transactions, and never wait for another session's lock while holding a database transaction.

Interrupted call IDs remain non-executable and require explicit recovery. Retired sessions stay retired through every recovery branch; completing cleanup does not reactivate them. New-format admission validates schema/accounting version before mutation. Existing experimental databases lack the required expiry and reservation records and refuse without an explicit migration; opening them cannot fabricate historic timestamps or erase their identities.

#### Qualification of accounting and recovery

The first increment changes [the experimental store](../../../scripts/experiments/mxc_session_patch/host_store.py) and its focused tests, then [the supervisor](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/host_call.py) and process-death probes. It keeps the production adapter and cross-machine recovery separate. Clock tests use injected time rather than long sleeps; crash tests kill child processes at persisted boundaries. Native qualification runs on GitHub runners with the required hypervisor, separately for Windows/WHP and Linux/KVM. An unavailable platform remains unverified.

| Area | Required controls |
|---|---|
| Fixed expiry | Exact base and outer deadlines; first retry long after expiry; zero grace; later configuration changes; expired state cannot become available again |
| Clock forgiveness | Forward/backward jumps, cumulative drift, recovery with no clock continuity, concurrent retries sharing one grant, and a clock stuck before base expiry |
| Grace durability | Death before and after grant commit; one-second maximum forfeiture per outstanding grant; repeated restarts exhaust rather than reset the budget; failed/ambiguous commit never grants time |
| Shared quotas | Two sessions race for the last capacity; exact boundaries; metadata growth; both counters update atomically; full-store replay starts no helper |
| Launch recovery | Death before scratch creation, after helper creation, after identity persistence and after startup header; PID reuse and unavailable termination evidence |
| Publication recovery | Death during chunk storage, before commit, after commit and before acknowledgment; committed retry never re-executes |
| Cleanup and retirement | Death before/after unlink and allowance release; cleanup is idempotent; retained results survive session deletion; corrupt references refuse collection |
| Physical bounds | Native export refuses before exceeding its allowance; residual scratch remains charged; disk-full/journal failures preserve the previous recovery point |

### Recovery on another machine

Same-machine recovery is the default and uses local SQLite without a separate coordinator. Multi-host deployments may explicitly configure a separate recovery coordinator. The default installation neither starts nor requires that service. Recovery on another compatible machine requires host opt-in, checkpoint storage independent of the original machine and exclusive, fenced ownership of the session. A requested remote-recovery configuration without the required storage and ownership authority refuses rather than falling back to local recovery.

The optional coordinator owns SQLite on its own persistent local disk, independently of execution machines. Execution hosts access the coordinator through its API and never open or mount its database file. The first implementation does not replicate this database or automatically move its authority to another coordinator. Losing an execution host and losing coordinator storage are separate failure cases.

Execution hosts authenticate to the coordinator with mutual TLS. The coordinator validates each client certificate against its configured CA and maps its exact certificate fingerprint to an explicit host incarnation, session set and operation set. The client validates the coordinator certificate and hostname. Certificate identity does not itself authorize shutdown, establish guest termination or attest a running native guest. Certificate provisioning and rotation are host-managed; the experimental service reloads its allowlist on restart. Removing a certificate blocks subsequent access but does not prove that its existing guest has stopped.

The first multi-host coordinator has no automatic failover. While it is unavailable, new calls and recovery cannot proceed and report unavailable authority; the host may retry when it returns. Hosts do not elect a substitute coordinator or use local state as replacement authority. An active call cannot acknowledge durable success without confirmed publication. An ambiguous publication outcome is resolved against its original call identity when authority becomes available, without re-executing committed code. Existing execution deadlines and cancellation still apply during an outage.

Execution machines may be shared or dedicated. Automatic shutdown is disabled by default and requires separate trusted host configuration; enabling remote recovery alone does not enable shutdown. Configuration identifies the permitted action and its resource scope. Session-level recovery authority does not authorize shutting down a shared machine. A whole-machine action requires explicit authority for that machine and coordination of every affected session. A narrower action is usable only when its implementation establishes that the old guest has stopped and cannot resume with stale authority.

When shutdown is not enabled, recovery can proceed after independently verified termination of the previous execution resource. Otherwise it remains pending until termination is established; an unreachable machine or expired lease is not proof. Sending a shutdown request is not confirmation of termination. Before replacement execution, the recovery authority binds the termination evidence to the exact prior resource incarnation and takeover attempt, revokes stale publication authority and prevents the previous owner from reacquiring execution authority through a restart.

Before restoring, the new owner validates runtime and guest artifacts, architecture and the OS/hypervisor requirements of the checkpoint format. Both operating systems are execution targets; this does not imply Windows-to-Linux checkpoint portability or compatibility across runtime upgrades.

Ownership transfer prevents an old owner from executing or committing after its replacement takes over. A lease timeout alone is insufficient if the old guest can continue making external requests. Recovery creates a new physical generation and does not adopt an unidentified orphan guest. Unavailable storage, lost ownership and incompatible runtime artifacts fail explicitly; they do not downgrade the session to uncheckpointed execution.

### Network policy

Closed networking is the baseline. The host can select an allowlist only where a supported MXC interface and real enforcement are established for that profile. An unsupported allowlisted workload is refused; it is not silently run with unrestricted access or with a different network policy.

Qualification demonstrates that an allowed destination works and a forbidden destination fails, including direct-address, raw-socket, IPv4/IPv6, DNS, redirect and host-local bypass cases. HTTP method/path restrictions require their own evidence before declaration. Proxy environment variables alone do not establish enforcement.

Recovery reapplies the currently authorized policy rather than restoring stale network authority. Allowlisting does not implicitly supply host credentials, enable a credential gateway or authorize runtime package installation. These remain separate host-controlled surfaces.

### Selected MXC extension

The maintainer selected extending MXC's Hyperlight API on 2026-10-02. The production integration remains `kind -> SandboxRouter -> MXC adapter -> MXC Hyperlight`; the direct Unikraft helper remains a feasibility experiment. Existing one-shot execution keeps its fresh-state behavior. Persistent sessions require an explicit, separately admitted experimental surface.

The [native spike evidence](https://github.com/sokolaidev/maf-extensions/blob/2797c74f8d0503d22e9b3a76c1457aa5723a05e1/scripts/experiments/mxc_hyperlight_probe.md#native-state-and-restart-experiment) establishes continuity and same-machine process-restart recovery for the tested Python state on Windows. It does not establish that unchanged MXC exposes those operations. Linux, machine replacement and production durability remain separate gates.

#### Proposed responsibility boundary

MXC would own the live native session, guest execution, interruption, quiescent checkpoint capture, compatible restore and disposal. Its session fixes the runtime, mounts and admitted policy; changing these cannot silently replace the guest with empty state. The suite would own trusted conversation identity, serialized tool calls, bounded files and artifacts, checkpoint-store credentials, atomic publication of checkpoint/result records, retry delivery and fenced ownership. Snapshot serialization alone does not implement the suite's durable completion contract.

#### Proposed session operations

These are proposed semantics for upstream discussion, not shipped method names or an approved MXC schema.

| Operation | Required behavior |
|---|---|
| Create | Bind an explicit runtime/artifact profile and admitted policy to a new session; no implicit network or setup download |
| Execute | Keep interpreter state; report execution completion separately from durable tool-call success |
| Prepare checkpoint | Freeze the completed state and export a new immutable candidate with format, runtime/artifact and compatibility metadata; do not overwrite a committed checkpoint |
| Confirm commit | A trusted host confirms durable publication for the current candidate and generation; only then may the next execution start |
| Restore | Validate a host-selected committed candidate against current admission and compatibility, then create a new physical session; failure cannot fall back to empty state |
| Close | Stop execution and release native resources; do not remove caller-owned committed checkpoints |

A successful execution moves the session from ready to uncommitted. It stays there through checkpoint export and durable publication; another execution is refused until the host confirms the current commit. The confirmation is a host assertion, not independent proof by MXC that remote storage is durable. A failed execution, timeout, cancellation or failed capture requires an explicit recovery/close path. A publication retry may reuse the same immutable candidate while the guest stays parked, but cannot rerun the Python program or mutate the candidate. Lost acknowledgment after publication is resolved from the host's durable call/result record.

The first upstream increment should expose and test native session primitives with closed networking and no mounts, using the existing one-shot path as a fresh-state control. This is an implementation gate, not a reduction of the selected file/artifact scope. Follow with the experimental engine/SDK contract, an out-of-process control channel, bounded file consistency, durable host orchestration and both-platform qualification. Guest output must remain separate from control data; the observed merged stdout/stderr requires its own transport fix or underlying runtime change before promising stream fidelity.

Any JSON additions belong in MXC's mutable development contract and require corresponding SDK changes and runtime experimental authorization. Do not retrofit fields into the shipped stable schema or assume the current generic lifecycle operations already support Hyperlight sessions. Remote storage providers and distributed leases remain host integration concerns rather than mandatory MXC dependencies.

### Implementation gates

The selected product scope precedes implementation. The exact adapter API, native transport, checkpoint representation, durable storage interface and runtime distribution remain engineering decisions. The proposed package name and transport alternatives stay in the [research record](#execution-transport-and-ownership).

The first feasibility gate, tracked by [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649), establishes a supported path for both interpreter continuity and durable capture/restore of modified state. MXC's prepared startup snapshot is not evidence of either requirement. If the available mechanism preserves only a restricted subset of state, that limitation returns for a scope decision rather than being hidden behind the word persistence.

Subsequent gates cover bounded execution and output, native memory limits, deadlines and cancellation, owner-death cleanup, safe files, atomic checkpoints, lost acknowledgments, corrupt state, and recovery with competing owners. The first usable version qualifies the agreed file and persistence scope independently on both hosts. Allowlisting remains conditional on verified support; the rest of the scope is not weakened to make an experiment pass.

## Status

| Decision | State | Tracking |
|---|---|---|
| Extend MXC for persistent Hyperlight sessions | Removable experimental session and byte-stream patches qualified on both hosts; production adapter remains pending | [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) (closed) by [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786) (merged) |
| Interpreter continuity and checkpoint feasibility | Conditional go for experimental integration; production acceptance remains separate | [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649) (closed) by [#1674](https://github.com/sokolaidev/maf-extensions/pull/1674) (merged); [conclusion](#original-feasibility-conclusion) |
| Rich Python through MXC Hyperlight and CodeAct | Experimental local adapter candidate; native CodeAct qualification pending | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Code/text execution plus bounded file inputs and artifacts | Bounded directory transport and program-channel candidate; native qualification pending | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Windows WHP and Linux KVM in the first version | Selected; fixed native recovery and local checkpoint/result publication probes passed independently on Windows/WHP and hosted Linux/KVM; production adapter unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open); [probe evidence and limits](#checkpoint-publication-and-recovery-experiments) |
| Fresh state by default and optional conversation persistence | Selected; native continuity demonstrated, MXC API unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Restart recovery of persistent Python state | Selected; native process-restart probe passed, durable integration unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Checkpoint after every successful persistent call, before acknowledgment | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Host-configured recovery on another compatible machine | Selected; storage, compatibility and ownership unverified | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Closed networking and conditional enforced allowlisting | Native socket denial passed for the retained file profile on Linux/KVM and Windows/WHP; allowlisting remains unsupported and is refused | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open); [closed-network candidate](#closed-network-qualification-candidate) |
| Adopt upstream bounded capture | Qualification complete: [measured defer decision](#adoption-boundary-assessment-2026-10-16) on both hosts; integrated replacement remains open | [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) (closed) by [#1959](https://github.com/sokolaidev/maf-extensions/pull/1959) (merged); [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957) (open) |
| Bounded native output and control transport | Experimental separate stdout/stderr qualified on Linux/KVM and Windows/WHP: byte fidelity, one-MiB per-stream truncation, strict control, failures and durable replay; [evidence](#qualified-byte-stream-candidate) | [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) (closed) by [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786) (merged) |
| Native owner-death cleanup | Experimental private-pipe controls passed independently on Windows/WHP and Linux/KVM; arbitrary descendants and distributed fencing remain outside this result | [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669) (closed) by [#1674](https://github.com/sokolaidev/maf-extensions/pull/1674) (merged); [evidence and limits](#owner-liveness-experiment) |
| General bounded file input and artifact collection | Experimental virtual workspace qualified on Linux/KVM and Windows/WHP: 24 file/control cases, artifact/checkpoint consistency and eight durable crash boundaries; production adapter remains unimplemented; [evidence](#qualified-file-candidate) | [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670) (closed) by [#1799](https://github.com/sokolaidev/maf-extensions/pull/1799) (merged) |
| Compatible-machine recovery and fencing | Shared and dedicated hosts selected; separate coordinator optional for explicit multi-host deployment; automatic shutdown requires separate opt-in and explicit resource scope; durable ownership journal and bounded mutual-TLS publication API candidates with process controls; bounded nested checkpoint streaming qualified with a 1 GiB synthetic payload on Linux and Windows; opt-in remote scratch accounting and trusted reconciliation candidate with process controls; native host binding, bounded export and durable helper cleanup qualified with same-machine native restore on Linux/KVM and Windows/WHP; guest-scoped mutual-TLS controller and durable generation retirement candidate for the pinned closed-network profile; live supervisor locks block takeover; [hosted native Linux cross-runner recovery passed](#hosted-native-cross-runner-evidence-2026-10-24) for the fixed workload, but the repeat hit a CPU-vendor mismatch; [CPU-admitted native repeat passed on matching AMD hosts](#cpu-admitted-native-repeat-2026-10-25); [native worker-process crash before publication qualified on AMD/Linux](#qualified-worker-process-crash-before-publication-2026-10-25), including takeover refusal across pending-fence restart and recovery of committed state; [active-guest worker loss qualified on AMD/Linux](#qualified-active-guest-worker-loss-2026-10-28), including native helper retirement before controller termination, pending-fence restart and committed-state recovery; other owner-loss cases, infrastructure shutdown and network-authority revocation remain pending | [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) (open) |
| Completed-result retry window | Experimental acceptance complete: host-configured expiry is persisted per call, storage pressure preserves promised delivery and terminal expiry preserves charged identity; format-4/7 host controls and earlier native replay qualification passed | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Clock uncertainty during result expiry | Experimental acceptance complete under the selected host-clock policy: five-minute default budget, fixed outer deadline and prepaid grants; injected-clock and process-death controls passed on Linux/Windows, including format-7 owner recovery | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Persistent-session idle expiry | Experimental implementation: disabled by default, persisted deadlines and bounded forgiveness, active-call protection, retirement preserving result retries; local process controls and controlled-clock native lifecycle checks passed on Linux/KVM and Windows/WHP; [evidence](#qualified-idle-expiry-candidate); [contract](#optional-persistent-session-idle-expiry) | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged); [#1806](https://github.com/sokolaidev/maf-extensions/pull/1806) (merged) |
| Transactional logical-charge totals | Experimental implementation: indexed admission totals, explicit format-3 upgrade, rollback/recovery audits; synthetic GitHub Linux/Windows scaling measurements recorded | [#1772](https://github.com/sokolaidev/maf-extensions/issues/1772) (closed) by [#1775](https://github.com/sokolaidev/maf-extensions/pull/1775) (merged) |
| Format-4 native durability qualification | Qualified fixed workload: eight publication/cleanup crash boundaries, accounting reconciliation, both quota refusals and replay after collection passed independently on GitHub Linux/KVM and Windows/WHP; reports retained | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| MXC 1.0 migration | Spike complete: new session controls qualified independently on Linux/KVM and Windows/WHP; old checkpoints refused unchanged and recovered by the old helper; production adapter remains pending | [#1780](https://github.com/sokolaidev/maf-extensions/issues/1780) (closed) by [#1784](https://github.com/sokolaidev/maf-extensions/pull/1784) (merged) |
| Session deletion | Experimental implementation qualified: durable retirement blocks admission/publication, preserves retries and permits unreserved checkpoint collection; owner-observed termination and seven deletion-crash boundaries passed on Linux/KVM and Windows/WHP; host controls also verify format-7 retirement and retained delivery; [native evidence](#qualified-active-deletion-candidate) | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged); [#1802](https://github.com/sokolaidev/maf-extensions/pull/1802) (merged) |
| Capacity reservation before persistent execution | Experimental acceptance complete: atomic per-session/shared-store logical reservations and bounded scratch allowances; unresolved work remains charged; native bounded-export and format-4/6 quota/failure controls passed on both hosts; operating-system quotas remain separate | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Checkpoint collection | Experimental acceptance complete: current-checkpoint and cross-session chunk preservation, corruption refusal, atomic charge release and exact retained delivery; format-4/7 collection-crash controls passed on both hosts; the removal count is bounded, but whole-store validation cost remains unmeasured at large payload scale | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Interrupted reservation reconciliation | Experimental controls qualified: recorded helpers require confirmed termination; format 7 also permits new-owner cleanup of provably unlaunched scratch; ambiguous attempts remain charged; host process controls and separately pinned native recovery passed on both hosts | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Local durable storage transaction boundary | Experimental acceptance complete for a private local SQLite store: per-session ownership, atomic quota/checkpoint/result records and process-restart recovery; host storage controls and earlier fixed-workload native restart/replay passed independently on both hosts | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Physical storage safeguards | Qualified experimental safeguards: new format-6 roots enforce a per-connection SQLite page ceiling and check host-configured volume headroom; host-storage controls passed independently on GitHub Linux and Windows (34 tests each), and native format-6 allocation failure, recovery and retained retries passed on Linux/KVM and Windows/WHP; [evidence](#qualified-native-physical-storage-candidate); synthetic host-store recovery after actual exhaustion of disposable ext4/NTFS volumes also passed with verified image cleanup ([evidence](#disposable-filesystem-exhaustion-follow-up)); native publication after actual ext4/NTFS exhaustion, prior-state recovery, retained replay and independent image cleanup also passed on both hosts ([evidence](#qualified-native-full-filesystem-publication)); OS quotas, existing-root migration and power loss remain unqualified | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged); [#1807](https://github.com/sokolaidev/maf-extensions/pull/1807) (merged) |
| Checkpoint restore/export exhaustion | Qualified experimental boundaries on Linux/KVM and Windows/WHP: real ext4/NTFS export failure preserved prior native state and retry results and allowed identified scratch reclamation; partial restore failure preserved checkpoint/retry data while retaining unidentified scratch and refusing new calls; fixture teardown passed independently and does not resolve that reservation; [evidence](#qualified-checkpoint-copy-exhaustion-boundaries) | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged); [#1828](https://github.com/sokolaidev/maf-extensions/pull/1828) (merged) |
| Pre-launch scratch recovery | Qualified experimental format 7: opt-in new roots record intent before helper creation; a new owner can reclaim provably unlaunched scratch while ambiguous attempts remain charged; process-crash controls passed on Linux/Windows, and native file-profile restore/export disk-full recovery and exact retries passed on Linux/KVM and Windows/WHP; [evidence](#qualified-pre-launch-recovery) | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged); [#1831](https://github.com/sokolaidev/maf-extensions/pull/1831) (merged) |
| Total session storage and retention | Experimental scope complete: explicit logical per-session/shared-store quotas, persisted retention with bounded forgiveness, safe checkpoint collection and permanently charged identities; consolidated Linux/Windows host controls passed; [acceptance record](#storage-and-retention-acceptance) | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (closed) by [#1852](https://github.com/sokolaidev/maf-extensions/pull/1852) (merged) |
| Production storage integration and further qualification | Unimplemented: adapter integration and separate artifacts; operating-system quotas, targeted journal faults, power loss, migration/resizing and payload-heavy collection performance remain unqualified and are outside #1672 experimental acceptance | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Runtime egress qualification | Closed-network candidate checks Python, native Python and libc IPv4/IPv6 TCP, UDP and raw-socket denial before and after checkpoint restore; hosted results pending; allowlisting remains unsupported | [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673) (open) |
| Configuration API and runtime distribution | Open engineering design | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |

## Release assessments and implementation history

### Local adapter integration candidate

The next integration is `MxcSandboxBackend` in `maf-sandbox-mxc`, using a runtime program channel with explicit helper/startup identities and the existing SQLite journal. The initial adapter uses the retained 1.0.1 custom file profile; MXC 1.1 console qualification remains separate. A new helper command collects a bounded call-directory inventory after execution, including dynamically named files and empty directories, and reclaims call files before checkpoint capture. Existing named-artifact requests retain their previous identity and wire format.

Fresh state remains the default. Persistent interpreter recovery requires explicit configuration and host-selected reclamation rather than disposal. The local adapter holds one process owner per root and one call admission lease through staging, execution, delivery and cleanup. Recovered sessions bind the full scope/thread/agent/call identity, workload contract, closed network policy and runtime assets. Missing journals or changed profiles refuse acquisition; initialization is an explicit operation. Completed request retries remain distinct from new CodeAct tool calls, which receive new call directories.

The candidate's native acceptance tests cover the shared protocol's file contract without assuming a shell or a filesystem capable of links, NumPy/pandas text output, CSV-to-matplotlib PNG delivery, and restart/retry without incrementing the guest counter twice. Link-free conformance requires an actual guest link-refusal control and guest-visible byte reads; local modeled probe tests are not native evidence. Until the hosted adapter tests pass, the candidate is not qualified. The configured root is local; coordinator integration, compatible-machine adapter recovery and whole-machine isolation remain separate.

### Closed-network qualification candidate

The [network probe](../../../scripts/experiments/mxc_session_patch/network_probe.py) exercises 18 socket-creation cases through `socket`, `_socket` and native libc: IPv4 and IPv6, each with stream, datagram and raw socket types. Every case must return an explicit permission denial; missing APIs, unsupported operations and connection failures do not count. An admitted socket fails the probe before any outbound operation. A second helper process restores the captured interpreter and repeats the complete matrix with a persisted sequence control. The native workflow runs this independently for the MXC 1.1 console profile and retained 1.0.1 bounded-file profile on Windows/WHP and Linux/KVM. Results remain workflow artifacts, and the candidate does not establish enforced allowlisting, guest credentials or network-authority revocation. Hosted qualification is pending.

> A research record and initial proposal, recorded on 2026-10-02: what MXC v0.9.0 adds to the suite, what the suite can offer MXC users, and how an experimental adapter could fit the existing protocol. The maintainer selected rich Python through MXC Hyperlight, with code/text execution, file inputs and output artifacts, on both Windows and Linux for the first usable version. Optional persistent Python state must be recoverable after host restart, with a durable checkpoint after every successful tool call. Host configuration may also permit recovery on another compatible machine. Allowlisted networking is desired where enforceable; closed networking remains the baseline. Transport and runtime distribution remain implementation decisions to validate. No MXC adapter is implemented or qualified by this record.
> The [accepted design](#accepted-design) and [status table](#status) govern the selected contract. This section retains the original assessment and design reasoning.
> The follow-up logical-byte quota and local SQLite recommendations were subsequently accepted; their selected semantics now live under [Storage quotas](#storage-quotas) and [Local storage implementation](#local-storage-implementation). The maintainer subsequently selected [bounded clock forgiveness](#clock-forgiveness) for result expiry and [session deletion](#session-deletion) that preserves promised delivery while retiring the session identity. The owning design now also specifies [durable clock-budget accounting](#durable-clock-budget-accounting) and [reservation recovery](#reservation-recovery). The follow-up proposal below retains its pre-decision wording.

MXC would extend the suite's execution environments. The proposed integration places it beneath `SandboxRouter`, with a separately qualified profile for each concrete MXC backend. Workload kinds, admission, result labels and application ownership remain in the suite. A common configuration schema does not make all MXC backends interchangeable.

### Evidence and scope

Generated reports are kept outside Git. Hosted evidence links below identify workflow runs; their artifacts are subject to retention and may have expired. Prose findings and source identities remain in this document.

The original assessment was made on 2026-09-29 and 2026-09-30. This record checks the design against MXC tag `v0.9.0`, resolved on 2026-10-02 to commit `86fb3d2abaf9c431556692037bff881830b543a5`. It compares the suite at commit `240e62f91be648a0214ea8cdc08f67f96045c620`; unrelated working-tree edits are not evidence of delivered behavior. Links to current suite guides explain the contracts, while those commit identifiers identify the snapshots assessed here.

The evidence consists of release notes, tagged backend guides, selected MXC source and contract tests, and the suite's protocol, CodeAct runtime contract and backend implementations. No MXC binary was installed or executed. No performance, cost, cross-platform compatibility, isolation or production-readiness result was measured.

The [v0.9.0 release](https://github.com/microsoft/mxc/releases/tag/v0.9.0) calls `0.9.0-alpha` the stable policy schema and includes WSLC and IsolationSession lifecycle APIs without experimental opt-in. Hyperlight, NanVix and Windows Sandbox use the development `0.10.0-alpha` contract and remain experimental. The release is marked as a prerelease. The [tagged README](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/README.md) warns that some generated policies are overly permissive and that MXC profiles should not currently be treated as security boundaries. Stable schema support is not a security qualification, and a wrapper cannot create missing enforcement.

The earlier [two-axis policy record](two-axis-sandbox-policy.md) mentions MXC as a backend family. It does not assess this release or design an adapter. This record owns that narrower investigation. The existing [Hyperlight record](hyperlight-backend.md) and [Hyperlight guide](../backends/hyperlight.md) describe the suite's separate direct integration.

### MXC 1.1.0 upgrade candidate

[Issue #2761](https://github.com/sokolaidev/maf-extensions/issues/2761) tracks the separately pinned [1.1 candidate](#mxc-11-profile-reproduction). MXC [v1.1.0](https://github.com/microsoft/mxc/releases/tag/v1.1.0), commit `c66201839d63cae2239db5e92f1f1d6076cf5181`, moves Hyperlight-Unikraft to 0.21.0 (`34c0d647a9f8679c0ce53b76e174894b2c5bfa7a`) and Hyperlight host/common to 0.19.0 (`a03088470d822b538d2525ef9a5ae9e29daea1e8`). The source assessment was made on 2026-11-15. Existing 1.0.1 helper/profile assets and downstream byte-stream/file kernels remain pinned independently; this candidate does not silently upgrade persistent sessions.

The [runtime upgrade](https://github.com/microsoft/mxc/pull/1687) fixes Python output without a trailing newline and requires matching runtime provisioning. The [tagged runtime](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/34c0d647a9f8679c0ce53b76e174894b2c5bfa7a/src/lib.rs) provides `output_limit`, `echo_output(false)`, captured text and omitted-byte reporting. The candidate removes the old capture implementation and uses those APIs, retaining only overflow saturation/reporting compatibility. Upstream captures stderr separately as well as in the combined stream: each capture is bounded, but their aggregate retained allocation can reach twice the configured limit. Capture limits do not stop guest execution or bound the echo unless echo is explicitly disabled.

The [MXC runner](https://github.com/microsoft/mxc/blob/c66201839d63cae2239db5e92f1f1d6076cf5181/src/mxc-sdk/src/backends/hyperlight/common/mod.rs) still rewinds before successive calls, so session continuity remains a removable SDK patch. Bounded snapshot storage remains a separate patch and now charges segmented memory and transport blobs as well as snapshot metadata. SQLite durability, freshness, fencing, replay and mutual-TLS checkpoint transfer remain host responsibilities. The upstream [persistent-session request](https://github.com/microsoft/mxc/issues/1374) was still open at assessment time.

MXC's Hyperlight interface still rejects enabled networking. WSLC's new proxy is not Hyperlight allowlist support. The [native-TLS downloader](https://github.com/microsoft/mxc/pull/1536) changes Linux build dependencies; the candidate requires OpenSSL development libraries and a newly resolved, reviewed lockfile. New NVX and Windows365 backends remain deferred under the current scope.

Qualification must use GitHub Linux/KVM and Windows/WHP runners and the retained lock. It covers rich Python state, output floods, bounded exports, restart recovery and mutual-TLS checkpoint publication/restore/replay. The new helper must reject an incompatible 1.0.1 checkpoint without modifying it, and the old helper must still restore it. Lock resolution alone is not native qualification. Cross-runner portability, physical power loss and compatibility of the custom byte-stream/file kernels remain separate acceptance boundaries; no old report qualifies this candidate.

[PR #2764](https://github.com/sokolaidev/maf-extensions/pull/2764) retains the candidate and this qualification summary. [Run 43770746800](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800), source `a57e40029915e4aae92defdad75b597b04268cc5`, qualified [Linux/KVM](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) and [Windows/WHP](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) with the same retained dependency lock. Both passed rich state, output and network contracts, aggregate export bounds, restart/crash controls, mutual-TLS native transfer/replay, unchanged old-checkpoint refusal, old-helper recovery and patch removal. Exact export controls covered 894,126,477 bytes on Linux and 894,384,576 bytes on Windows, each across eight files; budgets short by one byte or one file refused. These are correctness controls, not performance benchmarks.

Recommendation: use the 1.1 candidate for new closed-network rich-Python session experiments under the tested profile. Preserve existing sessions' original helpers and runtime assets. Keep the custom byte-stream/file profiles on their independently qualified 1.0.1 pins until they are ported and qualified; the successful session candidate does not qualify those kernels, cross-runner recovery, physical power loss or production deployment. The OSV check on 2026-11-15 at 22:34 UTC found no known advisories for the candidate's 289 registry crates, with kernel, rootfs, OS libraries and path patches excluded from that claim.

### MXC 1.0 migration spike

[Spike #1780](https://github.com/sokolaidev/maf-extensions/issues/1780) assesses release `v1.0.0`, commit `7bf210247986cb73b1b314df60c2f1109c479c0b`, against the independently qualified 0.9 baseline from [#1778](https://github.com/sokolaidev/maf-extensions/pull/1778). The [release](https://github.com/microsoft/mxc/releases/tag/v1.0.0) stabilizes versioned Rust, .NET and Node SDK entrypoints, but its [Hyperlight backend](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/docs/backends/hyperlight/hyperlight-backend.md) remains experimental under `1.1.0-alpha`. Generic lifecycle APIs do not supply persistent Hyperlight interpreter semantics. The runner still rewinds its baseline; the Unikraft 0.17.0 HostPrint collector still accumulates an unbounded string, and snapshot export has no host-selected byte/file budget. All three experimental overlays therefore remain necessary in this candidate.

The candidate lives separately in [mxc_v1_patch](#mxc-10-and-101-profile-reproduction). It retains the old tools and evidence, uses the SDK's documented-as-internal compatibility exports only inside the generated probe wrapper, and ports session state transitions, bounded capture and bounded export without changing the SQLite store. Source, dependency and rootfs identities are pinned. The manager verifies all affected files across the complete prerequisite chain before applying or removing a layer. Future upstream replacement still belongs behind the wrapper and requires equivalent behavior tests.

The 2026-10-08 [native qualification run](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210) tested source `da064543023d2b95ae06735d2746f520caa4eb61`. Linux/KVM and Windows/WHP independently passed locked builds, helper/collector/budget unit tests, fresh-state isolation, NumPy/pandas/lambda/open-file continuity after a killed helper, malformed snapshot refusal, bounded console controls, bounded native export and all eight format-4 crash boundaries. Quota refusals happened before launch; retained results replayed after checkpoint collection. The [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1), [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1) and their linked [native state evidence](#hosted-qualification) retain exact identities and measurements. The workflow separately identifies dependency resolution as `dependencies-resolved`, never a native pass.

Old-to-new restore was explicitly refused on both platforms: the old snapshot key is `k5e9192dfed5c8dbb-c1`, while the new runtime requires `k485adb2bab362516-c3`. The rejected restore left every old checkpoint file unchanged, and the old-pin helper built in the same job subsequently recovered the Python state. Guest snapshot conversion remains unimplemented. The recommendation is to adopt these pins for new experimental sessions and subsequent adapter work, while retaining the old profile for existing sessions. All three removable overlays remain required; the public SDK stabilization does not replace them.

Guest snapshot compatibility and SQLite format compatibility are separate. A changed helper/kernel/rootfs profile must not silently reopen an old persistent session with fresh state. Keep the exact old helper binary and its profile artifacts available for existing sessions until an explicit migration or retirement policy is selected; a rebuild from the same source can have a different helper hash and is not automatically the same store profile. These checks do not qualify cross-platform snapshot transfer, remote takeover, physical power loss, physical disk quotas or production adapter conformance. Closed networking remains the baseline; enforced allowlisting stays under #1673.

### MXC 1.0.1 release analysis

The 2026-10-24 assessment compares [MXC 1.0.1](https://github.com/microsoft/mxc/releases/tag/v1.0.1), released on 2026-10-17 at `fda29ac63bc306b7f97f3b86b4b63d44f7f40cf2`, with the active 1.0.0 experiment. The SDK package version advances, while the stable request schema stays `1.0.0`; Hyperlight remains experimental. This refresh changes the source-only experiment in [mxc_v1_patch](#mxc-10-and-101-profile-reproduction), inherited by the stream/file and cross-runner harnesses. It does not add a shipped `maf-sandbox-mxc` package or change the suite's separate direct Hyperlight backend.

| Upstream change | Effect on this solution |
|---|---|
| Hyperlight removes legacy network-policy wiring and validates supported policies before execution | Aligns with the experiment's closed-network baseline; it does not deliver enforced allowlisting |
| WSLC fixes image-store/session-name failures and permits up to eight concurrent lifecycle executions, refusing a second execution on a busy container | Useful for a future MXC WSLC adapter; the suite's current WSLC backend uses its own integration and does not acquire these fixes from this pin |
| ProcessContainer fixes PSEC 1.0 host-loopback routing and WFP denial reporting | Improves the Windows SDK surface; it does not add LPAC support to the public V1 request contract |
| Bubblewrap fixes stub-resolver DNS and loopback-startup races; Seatbelt denies metadata reads for denied paths | Improves future backend options; not exercised by this Hyperlight qualification |
| PTY and telemetry fixes | Relevant to the broader SDK; no new persistence, ownership or checkpoint-publication contract |

The [tagged Hyperlight source](https://github.com/microsoft/mxc/blob/fda29ac63bc306b7f97f3b86b4b63d44f7f40cf2/src/mxc-sdk/src/backends/hyperlight/common/mod.rs) still rewinds the warm baseline for fresh execution. Session continuity therefore retains the session overlay. [SDK dependencies](https://github.com/microsoft/mxc/blob/fda29ac63bc306b7f97f3b86b4b63d44f7f40cf2/src/mxc-sdk/Cargo.toml) still pin Unikraft 0.17.0, so bounded capture/export and the separate byte-stream/file layers also remain required. Only the session patch's surrounding import context changes; its state machine and the other patch contents are unchanged. The wrapper still uses internal SDK compatibility exports rather than claiming that the stable V1 contract supplies these features.

The runtime pin `8f636e00cdf29e6c33ba0f578482c595d7827cf6`, Hyperlight host/common pin `388ea8ebd88639a062f7a87f477db2930605a307`, verified agent rootfs and custom kernel pins remain unchanged. In particular, Hyperlight's CPU-vendor restore guard remains: upgrading MXC does not repair the cross-runner compatibility-admission gap in #1671, exposed by [run 40080442094](https://github.com/sokolaidev/maf-extensions/actions/runs/40080442094). Independently passing Linux/KVM and Windows/WHP controls would not establish cross-platform or cross-vendor snapshot portability.

New sessions use the updated SDK only after qualification. Existing durable sessions retain their exact helper and profile assets: a changed SDK source or rebuilt helper must not silently reopen a stored profile, even when kernel/rootfs identities remain the same. The retained 0.9-to-1.0 migration control checks the old incompatible kernel contract; it is not evidence of in-place 1.0.0-to-1.0.1 session migration. Historical reports retain their original sources and are not relabeled as 1.0.1 results.

Local source checks confirmed that the session/output/storage, byte-stream and bounded-file layers apply and reverse without tracked content changes or leftover files. [Lock-resolution run 40082278659](https://github.com/sokolaidev/maf-extensions/actions/runs/40082278659) produced byte-identical Linux and Windows locks, changing only `mxc-sdk` from 1.0.0 to 1.0.1; all transitive dependency versions remain retained. The new lock SHA-256 is `fd2ab5121a94260ba44501b782f2cf6504dc247c6a6c549dfc9d200974c054be`. The locked native qualification is [run 40082577780](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780), source `7545863209d86738bf4f37ae2ed788675e552734`. Resolved dependencies alone are not a native pass. The six selected native jobs passed: session/migration, byte streams and bounded files on both Linux/KVM and Windows/WHP. The [1.0.1 evidence](#mxc-101-refresh) records rich-state recovery, bounded capture/export, mutual-TLS publication/restore/replay, 23 stream scenarios, 24 file/control cases, crash/quota controls, format-7 pre-launch recovery, deletion, idle expiry and independent volume cleanup. All native overlays were removed successfully after qualification. The complete hosted workflow also passed its ordinary repository/package, worker-safety, type and packaging checks.

The adoption recommendation is MXC 1.0.1 for new experimental sessions with the existing closed-network profile and all overlays retained. Local focused controls passed 101 tests, and lint, formatting, security, types, documentation references, Markdown examples and 335 tracker references passed. The local full suite recorded 23,580 passes, 936 skips and one timeout while starting a replacement worker in the unchanged direct-Hyperlight process test; the entire module passed serially afterward (18 tests). This is not a clean full local-suite claim, and no direct-Hyperlight runtime change is included.

### Capabilities MXC adds

The platform inventory comes from the tagged README and [schema guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/schema.md). These are upstream-described capabilities, not capabilities established for a suite adapter.

| MXC surface | Gap in the assessed suite | Potential use and constraint |
|---|---|---|
| Windows ProcessContainer | No Windows-native execution backend | Run Windows executables with native OS restrictions; capability availability depends on Windows servicing and the selected implementation |
| macOS Seatbelt | No native macOS containment backend | Run local macOS tools without a Linux VM; Linux images and path assumptions do not carry across |
| Linux Bubblewrap and LXC | No adapters for these mechanisms | Add Linux choices beyond Docker; Bubblewrap uses a host tool environment rather than a container image |
| Windows IsolationSession | No isolated-user session backend | Separate Windows session identity and state-aware execution; requires a supported preview OS and has restrictive-policy gaps |
| Windows Sandbox and NanVix | No adapters for these environments | Additional VM execution choices; both are experimental |
| Hyperlight with Unikraft | Existing Hyperlight exposes a narrower Python runtime | Richer Python libraries and additional language runtimes inside a microVM; this is a different guest and execution contract |

MXC also exposes ingress and host-loopback policy, desktop and clipboard restrictions, selected host-filesystem permissions, Learning Mode and denial capture, TypeScript/Rust/.NET SDK surfaces, and versioned JSON contracts. These vary by backend. The suite's egress policy is not an ingress policy, its bounded file plane is not an arbitrary host-mount policy, and its execution telemetry is not permission-learning support. See the [release](https://github.com/microsoft/mxc/releases/tag/v0.9.0), [schema](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/schema.md) and [Rust SDK](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/core/mxc-sdk/README.md).

The [Hyperlight runtime guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/hyperlight/hyperlight-backend.md) describes an `agent` image with Python and scientific/data-processing libraries, plus `python`, `python-shell`, `node`, `bash` and `dotnet-jit` choices. This would expand the suite's microVM workloads. Comparable libraries can already be installed in suitable Docker or ACAS images; their mere availability is not new suite functionality. No startup or memory advantage over those alternatives is established here.

### What the suite offers MXC users

MXC already provides backend selection, execution and lifecycle APIs. Another generic execution wrapper would duplicate that surface. The useful addition is an application contract above it:

| Suite surface | Benefit above MXC | Boundary |
|---|---|---|
| Workload kinds | CodeAct, Bicep, Terraform/OpenTofu and draw.io tool behavior | Each kind still needs a compatible runtime, tools and capabilities |
| Router admission | Check isolation, operations, guest family, file budgets, network modes, sharing and authority before serving work | Checks declarations; it does not certify the provider |
| Trusted request ownership | Bind resources to scope, conversation, agent, kind and call where required | Model input cannot choose another caller's sandbox |
| Files and artifacts | Confined input placement, bounded output collection and explicit output sinks | Mounting a directory alone does not meet these contracts |
| MAF result contract | Separate completion, finite verdicts and justified trusted text from untrusted workload output | FIDES behavior belongs to the MAF integration; other framework adapters do not inherit it automatically |
| Host tools and credential gateways | Expose authorized host services or retain upstream tokens outside the guest | Must be implemented and qualified for the selected profile; existing Docker/WSLC support does not transfer automatically |
| Cleanup and observability | Coordinate call completion, disposal, conversation purge and correlated events | Host-death cleanup and orphan recovery still need provider-specific mechanisms |
| Conformance probes | Exercise file confinement, execution, egress, ownership and disposal contracts | Passing a subset is not a complete isolation audit |

The owning contracts are [policy and isolation](../policy-isolation.md), [information flow](../information-flow.md), [host boundaries](../hosts.md), [operations](../operations.md) and [backend authoring](../backends/writing-a-backend.md).

### Network differences that affect adoption

MXC's [WSLC guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/wsl/wsl-container-getting-started.md) describes `runtimeConfig.networkProxy` as cooperative routing. A client can bypass those environment settings with direct connections. The suite's WSLC adapter instead constructs an internal workload network and a filtering proxy, with optional method/path restrictions and a credential gateway. Replacing the current adapter with MXC WSLC would lose that enforcement unless equivalent topology were supplied. For a future MXC WSLC profile, do not declare `ALLOWLIST` from proxy configuration alone.

That limitation does not describe every MXC backend. The [Bubblewrap guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/bwrap-support/bubblewrap-backend.md) describes a private network namespace and enforced proxy-only networking. The [Seatbelt guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/seatbelt/seatbelt-backend.md) describes kernel-enforced proxy access when host-loopback remains denied; allowing host-loopback broadens access beyond the proxy port. Admission must account for the complete configuration, not just the proxy URL.

IsolationSession's [one-shot contract](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/isolation-session/oneshot.md) requires an explicit unrestricted directional posture and rejects restrictive network policies. It is therefore unsuitable for an initial `CLOSED` workload, despite its stable lifecycle API. MXC also documents a single WSLC daemon worker that serializes execution across sandboxes; concurrent throughput needs verification before adoption. See the [state-aware WSLC guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/wsl/wslc-state-aware.md).

For Hyperlight, distinguish runtime internals from the public contract. The tagged guide says the `network` section is rejected and execution runs without networking. `HyperlightScriptRunner` contains legacy allow/block-list translation, but `validate_runner` applies `NetworkPolicySupport::LEGACY`; the public development schema uses directional fields. The [migration regression script](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/tests/scripts/run_hyperlight_network_migration_test.ps1) expects old `network.allowedHosts` requests to fail exact parsing. Internal support is not evidence that an adapter can request allowlisting through the public API. The initial design would qualify the omitted-network request as `CLOSED`, including direct sockets and loopback probes, before declaring it. See the [runner](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) and [policy validator](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/core/wxc_common/src/validator.rs).

### Proposed architecture

The proposed package name is `maf-sandbox-mxc`. No package or public API is created by this record.

```text
agent framework -> kind -> SandboxRouter -> concrete MXC profile -> MXC runtime
                               |
                               +-> existing independently configured backends
```

Each configured backend object would select one concrete containment mechanism, one runtime where applicable, and one verified binary/schema combination. Its isolation and `BackendDeclarations` would be fixed before tool attachment. Do not use MXC's abstract `process` selection to switch enforcement mechanisms after admission, and do not union capabilities across all MXC backends. Probe availability and refuse an unsupported host; do not silently select a weaker backend.

The suite's default `MICROVM` floor would remain unchanged. Native process and shared-kernel profiles require explicit host policy allowing their assessed isolation. A product's use of the word container, or WSLC's use of a shared VM, does not establish the suite's per-workload microVM contract. Windows Sandbox's local full VM also needs a deliberate mapping: the current `Isolation.VM` contract describes dedicated remote infrastructure, so its name alone is insufficient.

Keep the core protocol dependency-free. MXC schema translation, runtime checks and transport belong in the adapter package; kinds continue to depend only on the protocol. Do not add general ingress, desktop policy or unrestricted host mounts to `SandboxSpec` merely to mirror MXC. A future workload requiring those surfaces should motivate a separate contract design.

### Selected first target

The maintainer selected rich Python through MXC Hyperlight on 2026-10-02. It adds a useful runtime while targeting the existing isolation floor. Native Windows, Bubblewrap, Seatbelt and MXC WSLC adapters are outside this first target. The selection establishes workload priority; it does not approve unverified capability declarations or replace the suite's existing Hyperlight adapter.

The first usable version includes both code/text execution and bounded file inputs and output artifacts. It targets Windows x86-64 with WHP and Linux x86-64 with KVM; each host must pass its own live qualification before the first release. These scope choices do not establish AKS readiness or support for other architectures.

The proposed user-facing integration is CodeAct with an explicit Python runtime contract. A first compatibility demonstration would compute a small NumPy result and perform a pandas aggregation from data embedded in the program, returning bounded text. The first usable version's file acceptance example would stage a CSV through the file store, analyze it with pandas, render a chart with matplotlib and return the chart through the existing output sink. Both examples must pass on both target hosts. Those are acceptance workloads to run, not claims that the integration already works.

Runtime instructions would describe the verified Python version, imports, filesystem access, network restrictions and the selected state lifetime. Require dependencies to be present in the pinned image; do not install packages over the guest network during a tool call. Fresh mode would start each call from the documented baseline; optional persistent mode must retain Python variables and dataframes between calls in the same conversation. Host-mounted data needs its own lifecycle regardless of the guest snapshot.

### Optional persistent state

The maintainer requested optional persistence. The proposed default remains fresh state per call, with a host-selected conversation mode for workloads that need notebook-like continuity. Persistence is a first-version requirement to qualify on both hosts, not an existing MXC guarantee. Merely retaining a workspace or a warm native process does not meet the requirement to retain Python objects.

| Mode | Ownership and state | Proposed cleanup |
|---|---|---|
| Fresh, default | Call-scoped instance; no Python variables carried into another call | Deliver outputs, then dispose |
| Persistent, opt-in | Conversation-scoped instance, also separated by trusted scope, agent and kind; serialized access preserves Python state | Reclaim temporary call files without resetting Python; dispose on explicit session end, conversation purge or unrecoverable execution failure |

Mode selection would belong to trusted host configuration, participate in the execution-contract identity and remain fixed for a live instance. A model cannot select another session or enable persistence through tool arguments. Changing mode, runtime or policy requires a new instance. A host that requires call isolation must refuse persistent mode rather than silently widen sharing.

The pinned MXC `HyperlightScriptRunner.run_once` restores its baseline before subsequent executions. A long-lived helper around that unchanged runner would still erase Python state. Persistent mode therefore needs a supported execution path that can retain the guest interpreter, or an upstream change. The experiment must establish that path before the adapter advertises persistence. Do not substitute replayed code, file retention or serialized Python objects and call it equivalent interpreter continuity.

Conversation scope alone does not preserve state under the current suite's cleanup contract: default disposal destroys the instance, and reset erases the state. Persistent mode needs explicit host reuse opt-in and a qualified `RECLAIM` path, using CodeAct's per-call directories without requesting reset. The file design must distinguish temporary call inputs/outputs from any deliberate session workspace; paths retained in Python objects must have a documented lifetime. Background guest processes and threads can mutate files between calls, so exclusive admission by itself is not evidence that output collection is race-free. If safe reclamation or collection cannot coexist with the intended state, revise the lifecycle design before implementation rather than weaken a capability claim.

Timeout, cancellation and native failure may require destroying a persistent guest. Report execution failure and the recoverable checkpoint explicitly; do not silently continue with an empty replacement while implying variables survived. The maintainer revised the earlier live-session-only choice: optional persistent state must now be recoverable after host restart. Artifacts already delivered to application storage follow that storage's retention and durability policy; they do not depend on keeping the guest alive. Persistent mode also needs bounded session count, memory and idle retention, plus tests for conversation separation and purge.

#### Recovery after restart

Recovery must restore the agreed Python state and session files from a consistent committed checkpoint. The selected requirement is not met by retaining artifact files alone. The first feasibility gate is whether MXC exposes a supported capture/restore path for a mutated guest, with usable snapshot serialization and compatibility rules. A prepared startup snapshot proves neither capture of user state nor durable recovery. The suite's `SNAPSHOT` capability means reset to a pre-input baseline; it must not be presented as a durable checkpoint contract.

The maintainer selected a durable checkpoint after every successful tool call in persistent mode. The adapter must commit the checkpoint before acknowledging the call as successful. The design must coordinate guest quiescence, Python state, session files, generation metadata and artifact delivery; a partially written checkpoint cannot replace the last complete one. A checkpoint failure after successful Python execution is an operation failure, not an acknowledged durable success. Preserve the last committed checkpoint and prevent later calls from silently building on uncommitted state. Fresh mode has no durable-state requirement.

An interrupted call must be reported as interrupted or outcome-unknown where appropriate. A checkpoint may commit before the caller receives its acknowledgment, so durable call identifiers and result/artifact records must distinguish retrying delivery from executing the program again. Do not replay code automatically to reconstruct state: allowlisted requests may already have produced external effects, and local recovery cannot undo them. Checkpointing after every success does not make external effects exactly-once.

Checkpoint metadata would bind trusted ownership, runtime/image/schema versions, architecture, policy and a generation identifier. Recovery needs exclusive ownership and fencing so an old owner cannot execute or commit after its replacement takes over. Reauthorize the restored session against current host policy before execution, exclude host credentials from checkpoints, and protect checkpoint access and retention like the data it contains. Refuse corrupt or incompatible checkpoints explicitly; do not claim Windows-to-Linux portability or upgrade compatibility without evidence. The persistence mechanism, supported Python objects and treatment of open files, threads and sockets remain to be qualified. If only a restricted serialization approach is feasible, present that limitation for a scope decision instead of silently substituting it for interpreter recovery.

The maintainer selected recovery on another compatible machine when the host requests it through configuration. The proposed default is same-machine recovery; enabling remote recovery is a trusted host decision, not a model tool argument. The receiving host must independently admit the workload and satisfy a tested compatibility profile for the checkpoint, including the native runtime build, guest artifacts, architecture and relevant OS/hypervisor constraints. Matching the Python version alone is insufficient. Supporting Windows and Linux as execution hosts does not promise checkpoint portability between them.

Remote recovery requires checkpoint data and metadata to remain available independently of the failed machine, together with atomic ownership transfer and fencing. Validate that configured storage and coordination can provide those guarantees before admitting persistent work with remote recovery enabled. A lease timeout alone is insufficient if the old guest can keep executing or making external requests; the design must retire or fence that execution authority before allowing a new owner to run. Unavailable storage, lost ownership or incompatible artifacts cause explicit refusal. Do not silently fall back to an uncheckpointed session or a fresh interpreter.

#### Host configuration

The following are proposed policy semantics, not a published configuration API. Transport-specific fields and storage interfaces remain to be designed after the capture/restore experiment.

| Setting | Default | Host-selected alternative |
|---|---|---|
| Python state | Fresh per call | Persistent per conversation, isolated by trusted ownership |
| Persistent checkpoint timing | After every successful call, committed before success is acknowledged | No weaker timing selected for the first version |
| Recovery placement | Same machine | Another compatible machine, with independent durable storage and fenced ownership |
| Guest networking | Closed | Explicit allowlist where supported and verified; otherwise refuse that workload |

Storage credentials and recovery authority stay in the trusted host. Enabling persistence or remote recovery does not attach either authority to guest Python. Configuration validation must keep the existing isolation, cleanup and authority floors in force.

#### Conditional allowlisted networking

The maintainer requested allowlisted access if possible. Closed networking stays the default; a host may opt into an allowlist only through a supported public MXC contract and a demonstrated enforcement mechanism on the selected platform. Until then, an allowlisted workload is refused rather than executed with unrestricted or silently closed networking. The offline profile remains independently useful.

Qualification must demonstrate both successful access to a permitted destination and denial of a forbidden one, plus bypass checks for raw sockets, direct addresses, IPv4/IPv6, DNS changes, redirects and host-local destinations. Declare method/path restrictions only if separately supported and tested. Proxy environment variables alone are insufficient. Keep credential injection and package installation outside this networking decision: permission to contact a service grants neither host credentials nor permission to mutate the prepared runtime. Persisted session recovery must reapply the currently authorized policy rather than restore stale networking authority.

### First compatibility experiment

For a Hyperlight prototype, begin with Python statements and bounded textual results: no input mount, output mount, attached identity, host tools or guest network access. The `agent` image is the candidate runtime. The host supplies `CodeactRuntime` instructions derived from verified imports and semantics; it does not infer compatibility from the image name. CodeAct remains Python-only even though MXC can execute other languages.

| Declaration | Candidate initial value | Evidence needed before advertising it |
|---|---|---|
| Isolation | `MICROVM` | Hardware boundary, bounded native process ownership, closed networking and explicit guest-to-host channels |
| Capabilities | Only `RUN_CODE` | Python statements, result streams, exit status, error behavior, timeouts and cancellation |
| Egress | Only `CLOSED` | Exact public request accepted and guest connections refused; no inferred support from legacy internals |
| Guest OS family | Empty | Runtime interface does not promise arbitrary POSIX `EXEC` |
| Isolation scopes | `CALL` | Physical execution belongs to the trusted call key; concurrent callers cannot share mutable guest state |
| Cleanup | Dispose | Native execution tree is terminated and reaped; no warm reuse promise |
| Admission | Exclusive through delivery and cleanup | One active call per sandbox object; overlapping owners cannot reset or delete its state |
| Egress observation | False | No attributable network-decision stream established |
| Attached identity | None | Native process environment and all guest channels exclude application credentials |

Defaults in `BackendDeclarations` include file capabilities, so the implementation would explicitly set the capability set rather than inherit defaults. Configure the proposed call-scoped experiment's router with `min_isolation_scope=IsolationScope.CALL`: ordinary CodeAct specs request conversation scope, and a call-only backend does not silently substitute a different scope. Set CodeAct's input/output channels off and use a runtime without a guest storage base. Requests requiring files, shell execution, snapshots, reclaim or host tools would refuse during admission. This text-only experiment is an intermediate gate; the selected first usable version must also qualify the file plane.

The next useful increment is bounded file input and artifact output, for example a staged CSV and a returned chart. Design those together with an adapter-owned private workspace. Never mount the application's repository or home directory as a shortcut. Qualify no-follow host access, symlinks and Windows reparse points, hard links, path replacement, special files, byte/count limits and deletion ordering. Output collection happens only after every guest writer is stopped or excluded. Host-backed mount contents are not reset by restoring the guest snapshot.

### Execution transport and ownership

Prefer a process boundary between Python and MXC native code. A native fault or blocked runtime should be terminable without taking down the agent host. An initial experiment can use the platform executor with a private request file and separate stdout/stderr pipes. Use argv directly and never interpolate model source into a host shell command. The child gets a minimal host-owned environment; it does not inherit application credentials or incidental proxy configuration.

The CLI is a candidate transport, not yet the selected production interface. The Hyperlight runner's `ScriptResponse.standard_out` stays empty while output is emitted through process streams. Capturing those streams is necessary but not sufficient: verify raw-byte preservation, separation, bounded buffering and whether diagnostics mix with guest output. Treat every such byte as untrusted. Parse status only from a trusted control channel or documented process status; guest text that looks like a timeout or error must not control exception classification.

If the CLI cannot preserve status and output independently, use a small out-of-process helper around MXC's typed Rust SDK/C ABI with a dedicated framed control channel. Bound frames and native-side buffering as well as Python reads. Do not parse guest stdout as control messages or silently merge streams to satisfy the protocol. A Node SDK helper remains an option, but adds its runtime/deployment requirement; direct in-process Python FFI is deferred because it shares the agent host's failure domain. The transport experiment must choose one approach before implementation proceeds.

Each acquired wrapper would own a generation identifier associated with `(SandboxKey, kind)`. A one-shot Hyperlight executor need not have an MXC state-aware sandbox ID, so do not invent a mapping to `provision/start/stop` APIs that this backend does not expose. Each new physical guest generation must get a new instance identity; stale cleanup cannot affect a replacement. If one call needs several program executions, document snapshot restoration between them and verify that this matches the kind's runtime assumptions.

`run_code` would use a monotonic deadline starting at method entry, including queueing, process launch and execution. Expiry before submission is `SandboxQueuedTimeout`; expiry after execution begins is `TimeoutError`. The native runtime's timeout receives the remaining budget, while the parent enforces an independent deadline. Cancellation and native failure retire the execution tree before releasing ownership. Cleanup has a separate bounded allowance and reports failures rather than pretending disposal succeeded.

On Windows, qualify a job that limits committed memory and kills the native process tree on owner death. On Linux, qualify cgroup ownership and a supervisor or another equivalent demonstrated owner-death mechanism. Guest scratch allocation is not a host memory limit. Runtime install, image download and snapshot preparation occur as explicit operator setup, outside tool calls; pin the executor and guest artifacts, select an explicit image home, and avoid workload-controlled runtime search paths. Check package licenses and supported distribution artifacts before deciding whether the adapter bundles binaries or requires operator installation.

The initial process-owned experiment would document that calls and purge reach the owning host process. Any residual request files, workspaces or helper resources need an independently verifiable cleanup policy after a crash. The selected optional persistent mode adds durable checkpoint ownership, policy fingerprints, exact-instance disposal and explicit recovery outcomes. A replacement host restores a validated checkpoint into a new physical generation after fencing the old owner; it must not adopt an orphan guest or confuse logical conversation identity with physical instance identity. MXC lifecycle APIs do not by themselves establish interpreter continuity, durable recovery or safe reuse.

### Validation and implementation sequence

1. Target rich Python through Hyperlight on both Windows WHP and Linux KVM, with code/text execution, input files, artifacts and optional persistent Python state recoverable after restart. Commit a checkpoint after each successful persistent call before acknowledging success. Permit recovery on another compatible machine only when host configuration requests it and the required storage, compatibility and ownership guarantees hold. Keep the fresh, text-only, closed-network experiment as the initial evidence gate; investigate allowlisting as a conditional addition.
2. Build a small experiment outside shipped package code against the pinned release. Verify runtime availability, imports, raw streams, structured failure status, deadline enforcement, memory/output bounds and descendant cleanup. Record exact binary and guest versions alongside outcomes.
3. For the selected profile, write the proposed declarations and refusals from those results. If a required property cannot be established, narrow the profile or keep it unimplemented; do not lower the workload's requirement.
4. Implement the adapter and focused regression tests, then exercise the real backend through the applicable shared conformance probes and backend-specific adversarial cases.
5. Add a CodeAct sample using closed networking and no file channels. Compare cold setup, first execution, repeated execution, memory and concurrency only after correctness is established; separate image preparation from execution timing.
6. Qualify bounded file input, artifacts and optional persistent Python state with restart recovery on both hosts, including host-configured recovery on a compatible replacement machine, before accepting the first usable version. Add allowlisted networking only where its public contract and enforcement are verified. Additional concrete MXC profiles remain deferred. Promote decided content into an owning backend guide and tracker when implementation is selected.

| Acceptance area | Required cases |
|---|---|
| Admission | Unsupported runtime/schema/host refuses; lower isolation cannot pass the default floor; undeclared operations and egress modes refuse |
| Python results | Known imports, printed output, stderr, arbitrary bytes, nonzero exit, exceptions, no expression echo, snapshot state semantics |
| Result integrity | Guest-crafted error/status text cannot become a trusted verdict or control message; native diagnostics remain untrusted |
| Deadlines | Queue expiry without execution, busy loop, sleeping code, setup delay, timeout/native-error distinction and cancellation |
| Resource bounds | Output flood including native buffering, host memory exhaustion, child processes and cleanup after native faults |
| Closed networking | Direct IPv4/IPv6, DNS, loopback, host services and inherited proxy routes; required probes cannot count as passing when skipped |
| Conditional allowlisting | Permitted requests succeed, forbidden requests fail, alternate connection paths cannot bypass policy, unsupported restrictions refuse and recovery reapplies current policy |
| Ownership | Concurrent calls, scope purge, stale generation disposal, owner death and absence of host credentials |
| Optional persistent mode | Python variables and dataframes survive successful calls; fresh mode stays fresh; conversations remain separate; cleanup preserves intended state; failures identify the recovery point; retention limits and purge hold |
| Restart recovery | Every acknowledged successful persistent call has a committed checkpoint; failed commits preserve the previous one and block uncommitted continuation; restore matching Python state and session files; corruption and incompatibility refuse; old owners are fenced; retries after lost acknowledgments do not replay committed work or duplicate artifact delivery |
| Host-configured remote recovery | Disabled by default; permitted only by trusted host configuration; restore on a tested compatible machine; reject mismatches and unavailable durable storage; concurrent owners, stale commits and old-guest execution cannot survive ownership transfer |
| First-version file plane | Exact bytes, parent swaps, links/reparse points, hard links, special files, oversized output, collection races and confined deletion on both hosts |

A proposed upstream contribution would be a Python/MAF reference integration and focused conformance cases, especially around runtime status, streams and network-policy semantics. This record does not publish an upstream issue or PR.

The maintainer subsequently selected extending MXC's Hyperlight API; the [owning design](#selected-mxc-extension) records the responsibility boundary and proposed session operations. The lower-level helper remains a feasibility probe, not the production integration route.

### Design decisions still open

| Decision | Working recommendation | What changes the choice |
|---|---|---|
| Transport | Out-of-process native executor experiment; typed helper if needed | Byte/status separation, bounded buffering and reproducible deployment |
| Runtime distribution | Explicit operator-provisioned pinned artifacts during the experiment | Verified release contents, licenses, installation and upgrade experience |

Adapter work and live validation are tracked by [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648), beginning with feasibility spike [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649). This record does not establish implementation or live validation. Rich Python through Hyperlight is the selected priority; the remaining recommendations are not approvals or measured guarantees.

### Storage retention follow-up proposal

This section records a follow-up proposal on 2026-10-04 for [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672). The accepted lifetime, retry and reservation policies live in the [owning design](#session-lifetime). The implementation below remains proposed; it does not change the earlier feasibility assessment or establish runtime support.

#### Findings in the existing experiment

At suite commit `b95508b3d579fbeb7e9504aec9d274623fedb08a`, [Store](../../../scripts/experiments/mxc_session_patch/host_store.py) owns one local database and retains every committed call, file inventory and compressed chunk. Its checkpoint and result limits bound individual publications, not cumulative storage. Independent counters in those databases would not provide atomic admission against a shared-store quota.

`Store.begin` treats an absent call ID as new execution. Collection therefore cannot delete call identity when it removes a result. `Store.restore` also requires the latest checkpoint's call to remain committed: result expiry must be represented separately from execution outcome. A checkpoint can remain current after its result expires, and an old result can remain deliverable after its checkpoint is superseded.

[The supervisor](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/host_call.py) materializes a restore directory and exports a candidate outside the database. Those copies, bounded diagnostics and interrupted work require ownership and cleanup accounting too. A sum of compressed database payload lengths would omit them. SQLite deletion also normally leaves reusable pages in the database; shrinking the file with `VACUUM` can require free space up to twice the original database size. Logical collection and physical reclamation need separate acceptance criteria. See [SQLite's VACUUM documentation](https://sqlite.org/lang_vacuum.html).

#### Accounting decision pending

Recommend measuring both quotas in uncompressed retained checkpoint, result and artifact bytes, plus explicitly bounded metadata charges and outstanding reservations. Charge each retained object in full to its owning session; compression and content deduplication would save physical space without increasing admission capacity. Artifacts embedded in the serialized result would be counted there once; separately retained artifacts would have their own charge. Superseded checkpoints would remain charged until collection removes their references. Compact expired-call records would remain charged, so indefinitely retaining identity can eventually refuse new work even when old payloads have been collected.

This model would require separate physical-space safeguards for restored files, candidate export, compression overhead, SQLite pages and journals, and cleanup or compaction. Scratch byte/count limits and maximum concurrent reservations would bound expected temporary demand; a free-space check alone would not reserve disk against unrelated writers. Publication must still handle disk exhaustion without acknowledging success. The alternative is a strict physical allocation quota covering all these files, which requires filesystem-level enforcement or a separately qualified allocator. The logical model is a recommendation awaiting a maintainer decision, not an approved interpretation of the quotas.

#### Proposed local transaction boundary

For the first storage experiment, prefer one local SQLite database for all admitted sessions in a configured store root. Session ownership would remain an operating-system lock per session, acquired before database transactions. Admission, reservation conversion and collection would transact in the same database; guest execution would run outside its write transaction. This avoids a separate quota ledger and session database disagreeing after a crash. Database writes would serialize, which needs measurement before promising throughput. Network filesystems, remote takeover and distributed fencing would remain the separate [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) qualification.

The proposed records would separate these responsibilities:

| Record | Purpose |
|---|---|
| Store configuration | Schema/accounting version and explicit aggregate quota |
| Session | Trusted identity, compatibility profile, owner generation, per-session quota and current checkpoint |
| Call identity | Session and call ID, request digest and durable execution outcome; retained after payload expiry |
| Delivery record | Result hash, retained artifact references, committed timestamp and fixed expiry |
| Checkpoint | Independently retained file inventory and chunk references |
| Reservation | Session, call, owner generation, maximum charges and cleanup state |
| Content/reference records | Verified bytes and explicit checkpoint/artifact reachability |

A new format should reject old databases explicitly until a migration is provided. Existing calls have no committed timestamps: opening a store must not fabricate an expiry or silently remove an earlier retry promise. Quota reductions below retained usage would block new admissions while preserving existing promises. Delivery would check identity and availability before attempting a new reservation, so a full store could still answer retries.

#### Publication, restart and collection

| Boundary | Proposed durable behavior |
|---|---|
| Before execution | In one transaction, validate both available balances and persist call intent plus the complete reservation; refusal starts no guest |
| During execution/capture | Keep reservation and prior committed state; enforce the candidate/result bounds rather than relying only on the eventual commit check |
| Publication | Atomically publish checkpoint, result/artifact references and fixed expiry, advance the current checkpoint, and convert reservation into actual retained charges |
| Publication failure | Keep the previous recovery point; retain the failed call identity and cleanup charge until private data is reclaimed |
| Commit followed by lost acknowledgment | Return the saved delivery record on retry; create neither a new guest nor a new execution reservation |
| Owner death | Acquire exclusive ownership and a new generation, establish old execution termination, reconcile publication and private scratch, then release only proven-unused capacity |
| Expired delivery | Return `result_expired`; remove eligible payload references without changing the committed execution outcome or current checkpoint |
| Collection interruption | Recover a committed reference/accounting transaction or roll it back; retry physical cleanup idempotently with its charge retained |

A reservation age or missing PID alone would not authorize reclamation. Cleanup would use host-owned directory identities and confined no-follow access, and refuse unknown or corrupt inventory. A committed call could still have residual scratch after acknowledgment; that cleanup would retain its own charge without making the call eligible for execution again.

Collection would retain roots for the current checkpoint, every unexpired delivery artifact, active reservations and any active restore or delivery reader. It would validate the reference graph before deleting an unreachable inventory or chunk, and update references and charged usage atomically. Incremental batches would bound transaction work; unfinished batches would retain their remaining charge. A corrupt live root would refuse destructive collection rather than being treated as absent. Freshly verified checkpoint bytes must still replace differing stored bytes under the same hash inside publication, preserving the experiment's existing corruption-repair and rollback behavior.

Persisted expiry requires an explicit clock contract across restarts. The implementation plan must cover clock rollback and forward jumps before enabling destructive expiry; elapsed process time alone cannot establish time spent offline. Session deletion/idle expiry must also preserve promised deliveries and prevent an old session identity from being recreated as fresh while retries remain valid. These are remaining design questions, not silently selected policies.

#### Implementation and qualification sequence

1. Settle quota accounting, then the local transaction boundary and expiry clock contract one decision at a time. Specify bounded metadata charges, scratch admission and session retirement before coding the storage format.
2. Extend the experimental store and focused tests with the new schema, atomic dual-quota admission and retry states. Keep production adapter and cross-machine claims out of this increment.
3. Add publication conversion, restart reconciliation and reference-based collection with process-death injection. Wire bounded private scratch and cleanup into the supervisor.
4. Exercise the integrated experiment on GitHub runners for Windows/WHP and Linux/KVM where the required hypervisors are available, recording the exact candidate and any unavailable platform. Retain reports and hashes rather than large checkpoint trees.

| Test area | Required evidence |
|---|---|
| Admission | Exact quota boundary; either limit exceeded; competing processes from different sessions; maximum reservation before guest launch; replay succeeds at full quota |
| Delivery | Fixed expiry across retry/restart/config changes; matching expired ID refuses execution; changed request digest refuses; retained artifacts remain byte-identical |
| Checkpoint independence | Latest checkpoint restores after its result expires; older result replays after newer checkpoint publication |
| Reservation recovery | Process death before/during/after publication and cleanup; stale generation refuses; unknown scratch does not free capacity; failed cleanup remains charged |
| Collection | Shared chunks and active readers remain pinned; corrupt root refuses deletion; interrupted batches recover; replacement of corrupt historical chunks remains transactional |
| Storage bounds | Incompressible data, metadata/tombstone growth, scratch duplication, journal overhead, filesystem exhaustion and physical reclamation measured separately from logical usage |
| Compatibility | Old format refuses without mutation; no invented historic expiry; unsupported remote store refuses rather than starting fresh |

### Bounded file-plane design follow-up (#1670)

On 2026-10-08, following the merge of [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786), implementation planning started for [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670). The baseline is suite commit `894d4b07d503cc1a99f163ef42c1cdada67281e8`, MXC `7bf210247986cb73b1b314df60c2f1109c479c0b` and hyperlight-unikraft `8f636e00cdf29e6c33ba0f578482c595d7827cf6`. The following design records the initial source inspection and acceptance criteria; the later [qualified candidate](#qualified-file-candidate) provides experimental native evidence. No production capability is established.

#### Mechanisms and constraints

The qualified session overlay restores without mounts. Guest-private files, including the existing open-file state probe, travel in VM memory. The pinned runtime separately provides [host-directory mounts](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/8f636e00cdf29e6c33ba0f578482c595d7827cf6/src/hostfs.rs) backed by `cap_std::fs::Dir`, with read-only flags and chunked operations. That confines host path access but does not itself reserve aggregate file bytes or count, include mounted contents in the VM checkpoint, or retain immutable artifact deliveries. The write callback accepts an offset and bytes; the truncate callback calls `set_len` without an aggregate storage allowance. Read chunking is not a write quota.

The hostfs protocol reopens paths on each read/write chunk. Its own source identifies handle-based operations as future work. Guest vnode open and close are no-ops in the pinned kernel's `lib/hostfs/hostfs_vnops.c`. Consequently, a mounted-workspace design must qualify retained open descriptors, rename/unlink/recreate and restore behavior explicitly; the existing guest-private open-file probe does not establish those properties for a mount.

The runtime's [AppSandbox boundary contract](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/8f636e00cdf29e6c33ba0f578482c595d7827cf6/src/lib.rs) says the vCPU is halted and guest threads are blocked between entries. This gives a potential consistency boundary for host-managed workspace capture. Entering the VM again to run an artifact-reading script can schedule other guest work, so serialization of host calls alone does not prove a stable artifact view. Guest-produced filenames and bytes remain untrusted; guest text cannot authorize publication.

#### Proposed implementation boundary

Keep the new experiment separate from the qualified streams profile and place any native extension behind its existing backend wrapper. Evaluate a bounded host-managed workspace with limits enforced before every growth operation, an immutable exported workspace inventory and a checkpoint that binds that inventory. A memory-backed file service could avoid exposing host directories and make write bounds explicit, but it would require a removable runtime extension and native filesystem-semantic tests. An ordinary writable mount without that accounting and recovery work is insufficient.

Reserve input staging, the live workspace, the exported candidate and retained delivery artifacts before guest execution. Bind the complete request identity to code, input names and bytes, selected output names and relevant policy. A retry with changed inputs or artifact selection must refuse, while a matching committed retry must deliver identical bytes without starting the helper. Publish the result, artifacts and corresponding recoverable file state atomically using the existing format-4 ownership and cleanup rules; preserve the previous checkpoint on any capture or publication failure.

The maintainer selected availability within a requested lifecycle. The owning design now records host-selected input lifetime, independently of whether Python state persists. The maintainer selected call and session lifecycles for the first version, aligning with the existing suite model. Explicit host release and time-based input expiry are deferred. A session-scoped input survives restart, while a call-scoped input is reclaimed before durable success. The maintainer selected call lifetime when the host omits a lifecycle; session retention requires an explicit host choice. The maintainer also selected refusal for an upload targeting an existing session filename unless the host explicitly requests replacement. The replacement choice must be included in request identity so a retry cannot change overwrite authority. The maintainer selected writable uploaded inputs by default within their requested lifecycle; this does not grant host uploads implicit replacement authority. The native candidate below qualifies these selected requirements within its recorded experimental boundaries. Guest code can copy input bytes into Python objects or other guest state, so deleting the original file does not erase those copies; the sandbox lifetime remains the boundary for that state. Result artifacts retain their separate delivery promise.

#### Host request contract

The initial [request module](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/files/request.py) validates immutable input byte snapshots, relative portable names, count and byte allowances, and host-selected call/session lifetime and replacement permission. It rejects duplicate, case-colliding and file/parent-colliding names. The canonical retry identity includes code and input hashes, names, lifecycles, replacement authority, selected artifacts and transfer limits. Offline tests exercise exact and exceeded bounds and changed-request refusal after reopening the existing SQLite store.

The request module defines admission and identity only; native transfer, workspace enforcement and checkpoint publication live in the separate implementation below. Native qualification remains pending. The maintainer selected host-configurable defaults of 64 files and 16 MiB total in each transfer direction. The request contract uses a 16 MiB per-file default as well, so one file may consume the entire directional byte allowance. The maintainer selected host-configurable live workspace defaults of 64 MiB and 256 files, covering retained inputs and guest-created files. Workspace limits also enter retry identity. The maintainer selected all-or-nothing artifact delivery: any missing, unsafe or oversized requested artifact fails the entire call, prevents successful result publication and preserves the prior committed checkpoint. Retained earlier deliveries keep their existing expiry promises. Qualification must prove that the failed call's Python and file changes cannot become the next committed state.

The host-side [inventory planner](../../../tests/mxc_inventory.py) applies upload replacement authority to an immutable file inventory and refuses aggregate workspace overages or path collisions before returning a new inventory. It does not expose a guest filesystem or establish native write enforcement. The maintainer selected a catchable filesystem capacity error without changing the affected file. The reference inventory model checks complete write/append/resize growth before allocation; an over-budget operation leaves the original inventory intact and later operations can proceed. Its completion planner checks all requested artifacts against one immutable inventory before selecting session-lifetime files for retention. These offline operations alone do not establish native enforcement or transactional checkpoint integration.

The pinned kernel's `hostfs_write` divides a guest write into chunks and calls `fs_write_bytes` once per chunk; the host callback immediately writes each received chunk. A quota check added only to that callback could accept early chunks and reject a later one after modifying the file. The selected refusal semantics therefore require a whole-operation admission/staging protocol or equivalent enforcement before the first mutation. Native qualification must include a write crossing multiple transfer chunks, failure near the final chunk, and continued execution after the error. Earlier completed writes and a separate successful truncate remain separate operations; a failed later write does not roll back the entire guest program unless the call itself fails.

#### Qualification gates

| Area | Required evidence on Linux/KVM and Windows/WHP |
|---|---|
| Inputs | Empty and arbitrary bytes, nested names, exact and exceeded count/byte limits, changed-request retry refusal, no guest launch after admission refusal |
| Confinement | Absolute paths, traversal, alternate separators, duplicate or colliding names, parent swaps, symlinks, hard links, special files and host reparse points where applicable |
| Workspace | Growth through write, append, truncate and sparse offsets; file/directory count; rename/unlink/recreate; retained descriptors; restart with consistent names and contents |
| Artifact capture | Host-selected bounded names; missing/oversized outputs; mutation during capture; background writers; exact byte equality with the committed file state |
| Delivery | Lost acknowledgment, matching retry after later calls and checkpoint collection, expiry and forgiveness, quota pressure without premature artifact eviction |
| Failure | Helper death, cancellation, timeout, capture/publication failure and all existing publication/cleanup crash boundaries with files included |
| Workloads | Text analysis and CSV-to-matplotlib output with exact input verification and a validated returned image |

These gates define the experimental file qualification below. The production adapter must still establish its own integration and must not advertise FILES_IN or FILES_OUT solely because the experiment passes.

#### Native file candidate

The removable `mxc_files_patch` layer now implements a virtual in-memory hostfs service. Guest names identify entries in that service and never resolve to host filesystem paths. The runtime overlay registers the service; the combined kernel overlay retains the separate-stream profile and adds whole-write admission, chunk staging and commit. No file changes before the complete admitted write arrives. Count, resize, append and sparse-growth limits apply at the service boundary. One staged write is active at a time; competing mutations receive `EBUSY` until it commits or aborts. Symlinks and hard links refuse; the kernel additionally refuses special-file creation instead of accepting it as a regular file. The existing stream profile and kernel remain separate.

The helper places call inputs under `/workspace/calls/<host-token>/` and session inputs under `/workspace/session/`, exposes those locations as `guest_call_path` and `guest_session_path`, and selects the call directory as the current directory. Artifact names are relative to the call directory. A new host token per call prevents an old call path from naming a later call's upload. Upload replacement checks the logical filename in the session namespace, requires exact spelling and explicit host authority, and refuses Unicode/case aliases and file/parent collisions against retained entries. Call-lifetime files are removed before checkpoint publication; renaming a file preserves its lifetime, while a guest-created copy is independent state. These locations and lifetimes are experimental conventions, not new production APIs.

After Python completes, the helper keeps the vCPU stopped while validating every requested artifact, copying artifact bytes, reclaiming call files, and exporting retained workspace state and VM memory. The checkpoint includes two fixed host files, `workspace.json` and `workspace.bin`, whose inventory and packed bytes participate in the existing checkpoint digest and SQLite transaction. Guest filenames occur only inside the inventory. The VM exporter receives the remaining checkpoint budget after reserving both workspace sidecars. A failed artifact check cannot produce a candidate or advance the store's committed checkpoint.

The file transport returns the bounded stream envelope plus named, hashed, base64 artifacts. It reserves a conservative encoded-result bound before execution, independent of the workspace and checkpoint bounds. The shared store's explicit maximum increases to 92 MiB to admit the largest selected file profile; its existing two-MiB default remains unchanged. Scratch admission covers two checkpoint copies, packed inputs, artifact payloads, bounded process diagnostics and control records. This is logical workspace and staging accounting, not a process-wide RSS bound or a quota for every guest-private filesystem outside `/workspace`.

The candidate's GitHub workflow builds one kernel and uses it for separate Linux/KVM and Windows/WHP qualification. Native probes cover lifecycle and explicit replacement, multi-chunk write and writev refusal without mutation, subsequent successful writes, append/sparse/resize/count bounds, links and special files, artifact failure, retained descriptors, text, CSV-to-PNG, and background mutation with artifact/checkpoint byte comparison. The durability matrix adds retained session files and artifact replay to all eight publication/cleanup crash boundaries, and checks that a failed artifact selection preserves the prior Python and file state. Host-side checks cover malformed completion and quota refusal before launch. The later candidate below passed these probes independently on both hosts; a kernel build alone does not qualify them.

The service synthesizes permission bits and acknowledges `chmod` without persisting permission changes. The pinned hostfs ABI remains path-based: it does not provide full POSIX inode identity for an open descriptor after unlink and recreation or every directory-rename case. This experiment must report its measured descriptor behavior separately from guest-private files retained in VM memory. It does not establish a production backend, arbitrary filesystem semantics, a runtime memory limit or live-network support.

For removal, restore the runtime checkout's original embedded kernel first, then remove the files layer with `python -m scripts.experiments.mxc_files_patch.patch remove --source <mxc> --runtime <runtime> --host <hyperlight>`. The underlying streams layer and the three MXC 1.0 layers remain separate; the qualification runner removes those in reverse order as well. The file kernel source overlay has its own `kernel_patch` apply/check/remove commands and pinned before/after fingerprints. Returning to another kernel or provider is a profile change, not permission to restore an incompatible file checkpoint.

#### Native qualification history

The first native candidate, `42445050a2d035a5a74141deb814b0cb2843e0f5`, built on both hosts in [run 37841579045](https://github.com/sokolaidev/maf-extensions/actions/runs/37841579045), but its first probe stopped on Python resource warnings from unclosed probe files. That run did not qualify the file profile. The probes now close their files explicitly.

Candidate `c941258cd725728180827ff67e401cb2be46087d` passed all 20 file cases and eight publication/cleanup crash boundaries independently on Linux/KVM and Windows/WHP in [run 37843173092](https://github.com/sokolaidev/maf-extensions/actions/runs/37843173092), attempt 1. Both hosts used kernel SHA-256 `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`, preserved the earlier Python-state controls, and removed all overlays. Both verified that artifact failure preserved the prior Python and file state, that saved artifacts replayed without a helper after checkpoint collection, and that a background writer's captured artifact matched the retained workspace bytes. Later upload-alias checks, default-limit transfers and bounded failure diagnostics require the later candidate's own qualification.

#### Qualified file candidate

[GitHub run 37845642100](https://github.com/sokolaidev/maf-extensions/actions/runs/37845642100), attempt 1, qualified candidate `16aa209a5fe37a783263da08c43edbf135de4503` independently on Linux/KVM and Windows/WHP. The retained [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/37845642100/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/37845642100/attempts/1) identify the actual helpers, locked dependencies, rootfs, source-built kernel and observed builder identity. Both hosts used kernel SHA-256 `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2` and successfully removed all overlays. Later documentation and evidence commits do not relabel the tested source commit.

Each host passed 24 file/control cases: a byte-exact transfer of 64 inputs and 64 selected artifacts totaling 16 MiB in each direction; two host-side pre-admission count/byte refusals; writable binary and nested inputs; call cleanup and retained session descriptors across restart; explicit replacement and case-alias refusal; whole-write/writev quota rejection with unchanged bytes and offsets; subsequent exact-bound writes; append, sparse growth, resize and count limits; links/special-file refusals; all-or-nothing missing, oversized and directory artifact rejection; preserved prior files; rename/list operations; text analysis; CSV-to-matplotlib output; background-writer artifact/workspace equality; and guest failure, timeout and cancellation without publication. The standalone Rust workspace tests also passed on both hosts, covering staged-write atomicity, malformed/incomplete frames, bounds and exclusion of competing mutations.

Both native durability runs passed all eight publication/cleanup crash boundaries, independently reconciled SQLite charges, refused new work under session/store quota pressure, and replayed identical saved artifacts without a helper after later calls and checkpoint collection. A separate failed-artifact call changed both Python state and a session file; a subsequent recovery call verified the previous committed versions. The input/control, full-transfer and chart results matched across platforms. The retained [example chart](../../../scripts/experiments/mxc_files_patch/example-chart.png) renders the supplied values 3 and 4 and has SHA-256 `efca6043bacc813811ccc5caaff4341e0b7d9263f947e69d2691bc19d5979ffb` on both hosts.

[#1799](https://github.com/sokolaidev/maf-extensions/pull/1799) delivers the experimental scope of #1670. This evidence covers same-machine process restart and the explicitly bounded virtual service. It does not establish full POSIX filesystem behavior, machine power-loss durability, compatible-machine migration/fencing, production file-store/output-sink integration, total process RSS limits or live network access. The [experiment README](#bounded-file-profile-reproduction) describes running and removing the profile; those remaining areas stay with the parent and separate follow-up issues.


### Active-session deletion candidate

After the bounded-file qualification in #1799, the next #1672 increment adds owner-observed deletion to the experimental console, byte-stream and file supervisors. The host sets an in-process event; the owner persists retirement before terminating execution. Retirement and publication are ordered by the existing SQLite transaction: a result committed first remains available for its promised retry window, while retirement committed first refuses publication. The notification itself is not durable until observed, so host restart before observation requires resubmission.

Restart recovery uses `NativeJournal.delete()` under the existing session lock. Windows termination checks creation time and waits on the same process handle; Linux opens a pidfd before rechecking creation identity and signals through that descriptor. An uncertain identity or failed stop leaves reservations charged. Cleanup retains its existing intent/removal/release journal. Checkpoint collection stays separate and bounded; it preserves completed results. This candidate adds neither physical disk guarantees nor idle expiry, distributed fencing, arbitrary-descendant cleanup or a production adapter.

Local real-process controls cover active deletion through all three supervisors, a mismatched process creation identity, uncertain launch refusal, retirement before admission, lost acknowledgment after publication, another session's live helper and interrupted cleanup. The GitHub bounded-file qualification adds native readiness followed by deletion, plus supervisor process death after retirement, helper termination, cleanup intent, scratch removal and cleanup release. Native Linux/KVM and Windows/WHP qualification was pending at the initial implementation checkpoint.


### Qualified active-deletion candidate

[Run 37857634416](https://github.com/sokolaidev/maf-extensions/actions/runs/37857634416), attempt 1, qualified implementation commit `0704d31b4bfd9ef9a67e736556515551dc071d73` independently on Linux/KVM and Windows/WHP. The retained [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/37857634416/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/37857634416/attempts/1) include eight deletion scenarios per host: active deletion and supervisor death before/after retirement commit, after helper termination, after cleanup intent, after scratch removal, and before/after cleanup release. Each scenario verified native readiness, confirmed helper termination, released scratch/reservations, refused late publication, and replayed the prior result without a helper after checkpoint collection. The eight records match across hosts, including retained-result hashes; this does not establish cross-host checkpoint portability.

Both jobs also repeated all 24 file/control cases, eight publication/cleanup crash boundaries, quota refusals, artifact-failure recovery and source-overlay removal. They used kernel SHA-256 `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`. The Linux helper was `3ab0f2e60b98e66a9f38a55ba21075a54bd3d6d9baa6496f34e944994113cc8b`; the Windows helper was `26e90bd1a1b9b11b34122f4582bad5c309e8d93e01ef8febc3b2dd2af0ba4136`.

Earlier [run 37855581216](https://github.com/sokolaidev/maf-extensions/actions/runs/37855581216), source `84f5789eeb8236d25f06830921976c8b4602b0dd`, remained unqualified. Windows passed the file and durability matrices, then refused deletion recovery when a concurrently exiting helper returned access denied from termination before its process handle was signaled. The final candidate waits on the same creation-checked handle and still refuses cleanup unless exit is confirmed. Linux's earlier job was superseded by the corrected run. The separate offline durability fixture was also updated for the lifecycle callback; the current PR's repository suite passed.

This result qualifies the experimental same-machine deletion path. It does not close #1672: idle expiry and physical-storage safeguards remain, alongside the separate production adapter and compatible-machine fencing work. The host must still resubmit a deletion request lost before its retirement transaction commits.

### Optional idle-expiry candidate

[#1806](https://github.com/sokolaidev/maf-extensions/pull/1806) adds opt-in host-side idle retirement under #1672. Its interval begins at session creation or completed call cleanup; running calls and unresolved cleanup are protected, while reopening, checkpoint reads and retained-result retries do not refresh activity. `SharedStore` evaluates idle policy in the admission transaction and checks before restore, and `NativeJournal.expire_idle()` provides the owner-thread operation for a host scheduler. No background scheduler or production adapter is added.

A session policy is fixed at creation. Enabling a new session upgrades the root to format 5, so older supervisors cannot ignore its lifecycle policy. Existing format-4 sessions remain disabled and usable by the new implementation; they cannot be assigned invented historical idle timestamps. Per-session deadlines and finite forgiveness survive restart. Clock jumps, prepaid grants and lost grants follow the selected result-retention model with an independent budget. Outstanding native work must be reconciled before a new idle interval can begin.

Local tests exercise all three supervisors, actual helper termination/cleanup after owner recovery, transaction failure and real process death around retirement/forgiveness commits. GitHub qualification adds a native bounded-file call with controlled host time passing its timeout, followed by restart, expiry and byte-identical artifact retries after checkpoint collection. Native validation was pending at the initial candidate checkpoint; the following record qualifies its exact implementation source. The earlier reports remain evidence for their own source commits. Physical-storage safeguards, distributed fencing and production integration remain outside this increment.

### Qualified idle-expiry candidate

Both native jobs in [run 37919647589, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/37919647589/attempts/1) passed for implementation commit `246ce970a7b0f6ea13f81dc78badd0b5bf0a1cd8`. The retained [Linux/KVM report](https://github.com/sokolaidev/maf-extensions/actions/runs/37919647589/attempts/1) and [Windows/WHP report](https://github.com/sokolaidev/maf-extensions/actions/runs/37919647589/attempts/1) each record an active native helper protected while controlled host time crosses the timeout, an idle interval starting after cleanup, retries that leave the deadline unchanged, restart preserving that deadline, retirement refusing new execution, two collected checkpoints, and retained artifact results replayed byte-for-byte without a helper. Scratch is empty and logical accounting reconciles afterward.

Both saved-result hashes match across platforms: `8ae02cd5e1a95e5b96d5eb82be2f7c15bfa93242033e32bb694ac9c7291bed04`. The common kernel digest is `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`. The Linux helper digest is `05122e27063774d532882a52e226657c65eb9f15becfb4acbf582eb870339814`; Windows is `134a28ef3fd86a77cae992b040b7320bbf8707b487c69696735e416dac80ee07`. Each job also passed the existing 24 file/control cases, eight publication/cleanup crash boundaries, eight active-deletion scenarios, quota refusal, artifact-failure recovery and overlay removal.

The dispatch's first overall attempt was cancelled because its independent repository-test shard reached the 15-minute timeout during Graphviz package installation, before tests began; the summary gate consequently failed. This does not change the two successful native job results. That shard was rerun separately. The candidate's full local gate passed with 17,400 tests, 909 skips and zero type-check errors, and its separate PR CI passed. Native evidence remains pinned to attempt 1 and the implementation commit above; subsequent documentation/report commits do not relabel it.

This closes the experimental idle-expiry gap, not #1672. Physical-storage safeguards remain. No background scheduler, production adapter, distributed fencing, arbitrary-descendant cleanup or guarantees under an arbitrarily incorrect host clock follow from these controlled-clock results.

The admission follow-up evaluates idle policy after capacity validation and in the same transaction as the new call identity and reservations. Delays beyond the final grant or fixed outer deadline refuse without clearing the deadline or admitting a call; refusal commits retirement, while a failed reservation rolls back its newly spent grant. Local regression controls cover both logical and native-scratch admission. The retained native reports above predate this host-side transaction correction and remain evidence only for their recorded source.


### Physical-storage admission candidate

Following #1806, the next #1672 increment separates the main SQLite file ceiling from observed volume headroom. The proposed opt-in policy applies only to new roots, uses explicit host-provided database/transaction/free-space byte limits, and persists format 6 so older supervisors refuse it. Enabling it on a root with older live owners would leave their connections uncapped; online migration is excluded from this increment. The chosen candidate retains DELETE journaling and applies `max_page_count` on every connection. SQLite's `journal_size_limit` applies after commit/checkpoint, so it is not evidence of an active-journal hard bound.

Admission conservatively budgets remaining main-file growth and all outstanding scratch allowances, plus the configured transaction and free-space margins. Existing retries and cleanup bypass admission headroom; actual storage errors remain explicit. The real allocation-failure control uses a small database ceiling and incompressible checkpoint bytes to obtain `SQLITE_FULL` without filling the developer's volume. It verifies rollback, the preceding checkpoint, retained results and conservative reservations across reopen. Separate controls inject `ENOSPC` and capacity observations; these must not be described as real journal or filesystem exhaustion. Hosted Linux/Windows controls qualify Python/SQLite behavior separately from native Hyperlight execution. The selected contract is recorded in [the backend design](#accepted-design) and [the experiment](#optional-physical-storage-safeguards); OS-enforced quotas and migration remain follow-up work; native format-6 qualification is recorded below.


### Native physical-storage qualification candidate

The format-6 follow-up adds a bounded-file native probe to the existing Linux/KVM and Windows/WHP runner matrix. A separate calibration store measures a real seed and successful recovery checkpoint together; a new format-6 root then fixes its SQLite ceiling at that combined allocation plus 8 MiB, with explicit transaction and free-space margins. No existing root is upgraded or resized. Independent supervisors seed the capped root, submit a guest call carrying 64 MiB of incompressible session data, and recover after a required real `SQLITE_FULL`. The failed call must have exported its mutated session files and stopped its recorded helper before publication fails. Its reservation stays charged until recovery reclaims scratch.

The recovery supervisor refuses a retry of the interrupted identity, executes a new guest call that checks the previous Python value and session file, verifies the failed call's upload is absent, collects the superseded checkpoint, and retrieves the original artifact result byte-for-byte without a helper. Each boundary checks SQLite integrity, the reapplied allocation ceiling and logical accounting. Offline tests exercise the supervisor orchestration with a simulated guest; the following record qualifies the native candidate on both hosts. This remains experimental evidence under #1672; whole-volume exhaustion, active-journal I/O faults, power loss, OS quotas, migration and production integration are outside this probe.

The first candidate, `6c430de9e88c59714448470e4d44b3a0b3930623`, remained unqualified in [run 38004415898](https://github.com/sokolaidev/maf-extensions/actions/runs/38004415898). Linux reached the required allocation failure, preserved the seed checkpoint and retained its reservation, but the subsequent successful-call publication also exceeded a ceiling sized from only the seed. Calibration now includes normal second-checkpoint growth before fixing the new root policy, and a larger 64 MiB input supplies the overflow pressure. The physical probe runs before the longer file/durability matrices so fixture failures surface sooner. The earlier Windows job was superseded before completion. No policy resizing or storage implementation change is involved.


### Qualified native physical-storage candidate

[Run 38006811225, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38006811225/attempts/1) passed for implementation source `3bc434135055176d0e3d136e239cd27310ac7afa`. The retained [Linux/KVM report](https://github.com/sokolaidev/maf-extensions/actions/runs/38006811225/attempts/1) and [Windows/WHP report](https://github.com/sokolaidev/maf-extensions/actions/runs/38006811225/attempts/1) each require a real `SQLITE_FULL` after native export, confirmed helper exit, rollback preserving the prior checkpoint and reservation, a fresh supervisor refusing the interrupted identity, a successful new call starting from the previous Python state and session file, absence of the failed upload, and byte-identical retained artifact replay after checkpoint collection. Scratch is empty and logical accounting and SQLite integrity reconcile afterward.

| Host | Fixed database ceiling (bytes) | After failed publication (bytes) | After recovery and collection (bytes) |
| --- | ---: | ---: | ---: |
| Linux/KVM | 388,874,240 | 269,156,352 | 380,416,000 |
| Windows/WHP | 388,931,584 | 269,086,720 | 380,321,792 |

These ceilings are fixture configuration derived from separate calibration roots, not application defaults. Normal second-checkpoint growth measured 111,345,664 bytes on Linux and 111,464,448 bytes on Windows, explaining why the first seed-only allowance was insufficient. Each host used its database ceiling again as the configured transaction allowance, a 67,108,864-byte minimum free-space margin, and a 67,108,864-byte incompressible input. Every supervisor reapplied the same ceiling; no root was resized. Collection permits page reuse rather than promising file shrinkage.

Both retained seed results have SHA-256 `bf95a4b161547f5221a8580170cacaafddd6472c3c1a41689bc8ca7bba74a54d`. The common kernel digest is `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`; the Linux helper digest is `30a7c9dc9b36f8134080a6e4012d634358d180a976f61c50e427ff7ae8e82c15`, and Windows is `8d73c0a0d005c434bea78cab959d5e5d2c3db0543450dbdbb88d9b1fb83da6ae`. Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. Each job also repeated all 24 file/control cases, eight publication/cleanup crash boundaries, eight active-deletion scenarios, quota refusals, artifact-failure recovery, controlled-clock idle retirement and source-overlay removal.

[#1815](https://github.com/sokolaidev/maf-extensions/pull/1815) retains this experimental same-machine evidence. Matching result digests do not establish cross-host checkpoint portability. The complete local suite passed 17,688 tests with 909 skips at the implementation candidate, alongside lint, formatting, type and documentation checks. Whole-volume exhaustion, active-journal I/O faults, power loss, OS quotas, offline migration/resizing, distributed fencing and production integration remain outside this qualification; #1672 remains open.

### Disposable-filesystem exhaustion follow-up

Following the [native format-6 qualification](#qualified-native-physical-storage-candidate), the next candidate tests actual capacity exhaustion on disposable 128 MiB ext4 and NTFS filesystems. A separate owner process attempts publication after a real headroom check and a competing filler consumes the observed capacity. Recovery must retain the previous checkpoint and result, preserve the interrupted identity and reservation, and permit new publication after space is restored. The [probe contract](#disposable-filesystem-exhaustion-candidate) separates this synthetic host-store evidence from native execution and power-loss guarantees. [Run 38060111346, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) passed on both platforms at PR head `88c72af219a5bd95505a17285538abacb1622f0b`, tested merge `7aadccaef6e8451bd8a94bd42a2a0074f3302acf`. Each filesystem reached zero free bytes and the database failed at 118,784 bytes, below its 32 MiB ceiling. Separate-process recovery and independent image detachment/removal passed; [reports](#disposable-filesystem-exhaustion-candidate) preserve the source identity and observations. This is synthetic host-store evidence; that run did not exercise native guest publication on a full volume, active-journal faults or power loss. Native publication is qualified separately below. #1672 remains open.


### Qualified native full-filesystem publication

[Run 38128658746, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1) passed on Linux/KVM and Windows/WHP for implementation source `f2cec9b7efb7e8cd5c4d1d78788f009197200ac8`. The candidate combines #1815's native state/file recovery checks with #1819's owned disposable filesystems. An 8 GiB ext4/NTFS fixture holds SQLite and journal-managed native scratch; a filler exhausts actual capacity after native export, helper termination and publication admission. The [probe contract](#native-disposable-filesystem-exhaustion-candidate) fixes an independent 1 GiB database ceiling, 128 MiB transaction allowance and 64 MiB free-space floor. These are fixture values, not production sizing.

The complete [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1) each require operating-system `ENOSPC` (28), real SQLite `SQLITE_FULL` (13), rollback retaining the previous checkpoint and failed reservation, and confirmed helper termination. A fresh supervisor refuses the interrupted identity, reclaims scratch and executes a new call from the preceding Python state and session files; the failed upload is absent. The original completed artifact result replays byte-for-byte after checkpoint collection. Integrity, accounting and scratch cleanup reconcile.

| Observation | Linux/KVM, ext4 | Windows/WHP, NTFS |
| --- | --- | --- |
| Filesystem capacity, bytes | 8,350,298,112 | 8,587,833,344 |
| Free bytes at failed publication | 0 | 0 |
| Filler bytes written | 6,205,317,120 | 6,425,870,336 |
| Database bytes at failed publication | 269,119,488 | 269,037,568 |
| SQLite version | 3.45.1 | 3.49.1 |
| Independent image detachment/removal | Passed | Passed |

The [Linux cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1) and [Windows cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1) reports verify image detachment and removal. Both saved-result digests are `bf95a4b161547f5221a8580170cacaafddd6472c3c1a41689bc8ca7bba74a54d`. The shared kernel digest is `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`; helper digests are `98bedb02eec8816ec0d20ec172ec638a8876e9dd537c9defee8d04ea94d5bd32` on Linux and `75b9864c798f082638460b761d61cc74dd42da6229401b6beffe120774df69ad` on Windows. Each job also passed the existing native state, SQLite-ceiling, file, durability, deletion and idle-expiry probes and removed the source overlays. The shared 128 MiB synthetic fixture passed separately in [run 38129033627](https://github.com/sokolaidev/maf-extensions/actions/runs/38129033627).

[#1824](https://github.com/sokolaidev/maf-extensions/pull/1824) retains this experimental same-machine qualification. Local validation passed 17,794 tests with 910 skips, plus lint, formatting, types and documentation checks. Mid-execution/export/restore exhaustion, targeted active-journal I/O faults, power loss, OS quotas, migration, distributed fencing and production integration remain unqualified. Matching result digests do not establish cross-host checkpoint portability. #1672 remains open.


### Checkpoint-copy exhaustion follow-up

The next candidate extends #1824's publication boundary to checkpoint restore and native export, using the same owned ext4/NTFS fixtures. The [probe contract](#checkpoint-copy-filesystem-exhaustion-candidate) separates export recovery after recorded-helper termination from conservative refusal after restore fails before helper registration. Export must preserve the previous Python state, session files and saved result while reclaiming failed scratch. Restore must preserve and validate the committed checkpoint and retry result, while retaining unidentified scratch and refusing new work after restart. This records the existing pre-launch cleanup gap; it does not silently authorize reclamation. Hosted qualification is recorded below; #1672 remains open.


### Qualified checkpoint-copy exhaustion boundaries

The checkpoint-copy candidate passed independently on Linux/KVM and Windows/WHP in [run 38139649785, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1) at implementation source `0fa619ea6c560b11d15b44e21fde0f67e5b268f6`. The complete [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1) retain three distinct supervisors per case, real filesystem exhaustion, integrity/accounting audits and matching seed/retry results.

| Observation | Linux/KVM, ext4 | Windows/WHP, NTFS |
| --- | --- | --- |
| Free bytes after filling, both cases | 0 | 0 |
| Free bytes released before native export | 33,554,432 | 33,554,432 |
| Native export OS error | 28 (`ENOSPC`) | 112 (`ERROR_DISK_FULL`) |
| Candidate bytes remaining after failed export | 0 | 0 |
| Restore bytes copied before filling | 11,101 | 568 |
| Restore copy errno | 28 (`ENOSPC`) | 28 (`ENOSPC`) |
| Python / SQLite | 3.12.3 / 3.45.1 | 3.12.10 / 3.49.1 |

Export reached native execution and refused completion without an index or successful control report. After confirmed helper termination, a fresh supervisor reclaimed its scratch, executed from the preceding Python state and session files, verified the failed upload was absent, collected the old checkpoint and replayed its saved result exactly. Restore failed before helper registration. Its fresh supervisor verified the committed checkpoint bytes and session files and replayed the saved result, while refusing both unidentified scratch reclamation and new calls. The partial scratch and reservation remained charged. This qualifies conservative refusal after restore failure, not automatic session recovery or safe reclamation of an unidentified launch.

The [Linux cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1) and [Windows cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1) independently confirm image detachment and removal. Removing the restore fixture does not resolve its journal reservation. Both cases retained result SHA-256 `bf95a4b161547f5221a8580170cacaafddd6472c3c1a41689bc8ca7bba74a54d`; the common kernel digest is `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`. The Linux helper digest is `0179d286a11314f8e006a8fedf9b2a0d4efc93ca4c3d49113fab2a4a577ff3fc`, and Windows is `c9c491c9a27c68819f7494751e970563ec213507f6f3367d0ba95eb01b984d72`. These matching results do not establish cross-host checkpoint portability.

[#1828](https://github.com/sokolaidev/maf-extensions/pull/1828) retains the probe and evidence. Each host also repeated the existing file, publication/cleanup, deletion, idle-expiry and physical-storage matrices and removed the source overlays. Local validation passed 17,799 tests with 910 skips, plus lint, formatting, types and documentation checks. The pre-launch cleanup gap, targeted active-journal I/O faults, power loss, OS quotas, migration, distributed fencing and production integration remain outside this qualification; #1672 remains open.


### Pre-launch recovery candidate

The restore-exhaustion controls in #1828 exposed a conservative gap: a call that failed before helper registration preserved its checkpoint and result but could not reclaim scratch or resume. The next #1672 increment records launch intent before process creation so recovery can distinguish a provably unlaunched call from a possibly spawned, unidentified helper. The [format-7 contract](#optional-pre-launch-recovery) opts new roots into this distinction and retains the old refusal for formats 4–6. A new owner may reclaim bounded scratch for an interrupted call that never committed launch intent; a committed intent without a recorded helper identity still blocks cleanup. Saved result retries and the prohibition on rerunning an interrupted call identity remain unchanged.

Real-process controls test the intent and cleanup transaction boundaries and verify all three experimental transports commit intent before spawning. Separate native qualification must demonstrate actual partial-restore and export disk exhaustion on ext4/NTFS, preserved prior Python state/session files, retained result replay, safe scratch reclamation and independent fixture teardown. The qualification below records the process controls separately from the native Hyperlight file-profile results. Neither establishes power-loss durability.


### Qualified pre-launch recovery

[Process-control run 38145865449, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38145865449/attempts/1) passed 57 tests on each host, including all 23 pre-launch controls. Its PR head was `53e3566b5a931f079c37be5fbb2542bb8183bc9e`, and both hosts tested merge `cacdc9878ee902fba5a5b9812de19db7c1700e33`. The [normalized process results](https://github.com/sokolaidev/maf-extensions/actions/runs/38145865449/attempts/1) retain runtime versions, test names, counts and original JUnit hashes. Five launch/identity crash boundaries and five cleanup boundaries distinguish provably unlaunched calls from ambiguous attempts, preserve charges until cleanup completes, and prevent execution before the ownership header. Each console, byte-stream and file supervisor committed intent before process creation; failure of that transaction prevented a spawn. These controls use ordinary helper processes.

The native file-profile qualification passed separately on Linux/KVM and Windows/WHP in [run 38145859671, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) at implementation source `53e3566b5a931f079c37be5fbb2542bb8183bc9e`. The complete [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) record format 7 and three distinct supervisors per export/restore case. The owned 8 GiB ext4/NTFS fixtures retain the 1 GiB database ceiling, 128 MiB transaction allowance and 64 MiB free-space floor; these remain fixture sizing, not application defaults.

| Observation | Linux/KVM, ext4 | Windows/WHP, NTFS |
| --- | --- | --- |
| Free bytes after filling, both cases | 0 | 0 |
| Free bytes released before native export | 33,554,432 | 33,554,432 |
| Native export OS error | 28 (`ENOSPC`) | 112 (`ERROR_DISK_FULL`) |
| Restore bytes copied before filling | 11,101 | 568 |
| Restore copy errno | 28 (`ENOSPC`) | 28 (`ENOSPC`) |
| Prior native state and session files recovered, both cases | Passed | Passed |
| Scratch reclaimed and new call completed, both cases | Passed | Passed |
| Python / SQLite | 3.12.3 / 3.45.1 | 3.12.10 / 3.49.1 |

Both failed calls preserved the preceding checkpoint/result and their reservation. After space was restored, a new owner refused the interrupted call identity, reclaimed bounded scratch, executed from the preceding Python state and session files, verified the failed upload was absent, collected the old checkpoint and replayed the original result exactly. Export recovery still required confirmed helper termination. Restore recovery used the durable proof that no launch intent had been committed; it no longer left the session blocked as the format-6 case in #1828 does.

The [Linux cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) and [Windows cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) independently confirm image detachment/removal after journal reconciliation. Both cases retained result SHA-256 `bf95a4b161547f5221a8580170cacaafddd6472c3c1a41689bc8ca7bba74a54d`. The common kernel digest is `ad89328122b2b7fb92a503d25b97ee328bd960dad2f307f082d839c489977ba2`; the Linux helper digest is `0e8fe926268fc72ea1ab38b49a3db8d4da06215ba355154010ee784ef9e2b3cf`, and Windows is `bb578abdfbd18d4ae6ea705e5d69f0e846619c68acfb5a05c30b6568e2e0c655`. Matching results do not establish cross-host checkpoint portability. The existing file, durability, deletion, idle-expiry and SQLite-ceiling matrices also passed, and the source overlays were removed.

[#1831](https://github.com/sokolaidev/maf-extensions/pull/1831) retains this experimental same-machine recovery increment. Local validation passed 17,823 tests with 910 skips, plus lint, formatting, types and documentation checks. A committed launch intent without recorded identity still refuses cleanup. Older roots retain their prior semantics; migration, targeted active-journal faults, power loss, OS quotas, distributed fencing and production integration remain unqualified. #1672 remains open.


### Storage and retention acceptance

This record maps the original experimental scope of [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) to the selected [logical storage and retention contract](#storage-quotas). Acceptance covers host-controlled per-session/shared-store accounting, retained delivery promises and safe collection. It does not redefine logical quotas as operating-system disk quotas or promote the experiment to production support.

| Requirement | Implementation and acceptance controls |
| --- | --- |
| Explicit total storage limits | `Limits` requires per-session and shared-store quotas. `begin` reserves maximum publication capacity atomically; the accounting ledger charges payloads, metadata, permanent identities and outstanding reservations. Controls cover each quota, concurrent admissions and replay while full. |
| Host-controlled retention without premature eviction | Each result persists its expiry and bounded forgiveness allowance. Controls cover policy changes, repeated owner recovery, prepaid-grant crashes, terminal expiry and saved results surviving storage pressure. |
| Safe collection with ownership checks | `collect_checkpoints` requires the current owner and no unresolved reservation for that session. Controls retain current checkpoints and cross-session shared chunks, bound deletion batches, refuse stale generations and reconcile logical charges. |
| Corruption refusal | Collection audits accounting, manifests, content, references and ownership before deleting. Controls corrupt six independent checkpoint representations and another session's manifest, requiring refusal without modifying retained data. |
| Interrupted collection | Real supervisor processes exit before collection, before commit and after commit. Recovery verifies the latest checkpoint, exact retained result, accounting rollback/commit and idempotent collection. |
| Historical calls remain bounded | Repeated publication, expiry and collection eventually refuse new admission under either quota because permanent call identities remain charged. Restart preserves the refusal and the latest checkpoint; expired identities cannot execute again. |

The [shared-store controls](../../../packages/maf-sandbox-mxc/tests/test_mxc_shared_store.py) run the quota, retention, collection, corruption and collection-crash cases against both format 4 and opt-in format 7. The [storage and retention workflow](../../../.github/workflows/mxc-physical.yml) also runs accounting, private-root permissions, native-journal process controls, idle expiry, physical safeguards and pre-launch recovery on GitHub Linux and Windows runners. These tests use SQLite and ordinary host processes with synthetic checkpoint bytes; native Hyperlight qualification remains the separately pinned evidence earlier in this record. [Run 38161348414, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38161348414/attempts/1) passed at source `4ba33250956ef99f2065f6d4da4acd9a6f1d14cd`: each host collected 321 tests, passed 320 and skipped one test specific to the other platform. Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. All 26 format-4 and all 26 format-7 acceptance cases passed on each host, including the three collection-crash boundaries, six corruption cases and both permanent-identity quota controls. The [acceptance report](https://github.com/sokolaidev/maf-extensions/actions/runs/38161348414/attempts/1) preserves source/runtime identities, per-module counts, skipped-test reasons, the 52 case names and original JUnit hashes. The first attempt at `e39976cb544dd594bce5194873be8fb49fdd070e` failed before tests because its metadata command called the SQLite version string; the corrected command and the complete matrix passed in the cited run.

Scope beyond this acceptance remains explicit: operating-system quotas, targeted active-journal I/O faults, power loss, existing-root migration/resizing and payload-heavy collection performance are not established. Admission totals are indexed, but collection still validates the whole retained checkpoint store under its write transaction; a bounded deletion count is not a bounded latency guarantee. Call identities consume quota permanently, so the store eventually refuses new work rather than promising an indefinitely reusable identity namespace. Separately stored artifacts and production adapter integration remain with [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648); compatible-machine recovery/fencing and egress retain [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) and [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673).


[PR #1852](https://github.com/sokolaidev/maf-extensions/pull/1852) completes #1672's original experimental acceptance scope. The local full suite passed 18,079 tests with 910 skips, alongside lint, formatting, both type-check passes and documentation checks. The later evidence commit changes only documentation and retained JSON. The existing native qualification remains tied to its original commits; the new host-control run does not replace or broaden those measurements. Further production storage work remains under #1648, without making every unqualified failure mode an implicit prerequisite for this experimental issue.

### Compatible-machine recovery investigation

Issue [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) begins from the completed local storage acceptance in #1852. This investigation proposes the remote ownership boundary; it does not claim an implementation, a selected infrastructure provider or native cross-machine qualification.

The current `SharedStore` acquires a local session lock and records a generation in a local SQLite transaction. `NativeJournal` records local process creation identity and scratch ownership; its termination checks apply to processes on that machine. The native helper's private ownership pipe stops execution when its supervisor exits, but a supervisor isolated from the replacement host may remain alive and keep that pipe open. Copying the database or expiring a lease therefore cannot establish exclusive guest execution. A shared network filesystem is not an extension of the qualified local-store contract.

A proposed recovery authority would live independently of execution machines and retain durable ownership generations, call identities, committed checkpoint/result references and existing retention/accounting promises. Publication would compare the current generation atomically with committing the checkpoint/result reference. Immutable payloads would be fully uploaded and verified before publication; an upload alone would not acknowledge success. Storage unavailability would refuse admission and publication, and an ambiguous publication response would resolve through the same call identity rather than executing again. The authority's storage provider and availability model remain open design decisions; no Redis, database or object-store implementation is selected here.

Takeover needs a durable intermediate state that revokes the old generation's authority to admit work and publish, without authorizing a replacement to execute yet. Replacement execution would require trusted evidence that the prior execution resource has stopped and cannot resume under its old authority, including any required network authority revocation. That evidence must bind the exact resource incarnation and takeover attempt; a stale stop receipt or reused host name cannot authorize recovery. A timeout, failed heartbeat or replacement-side inability to reach the old host is insufficient. Any restart of the old machine must obtain current authority before launching a helper. Failure to establish this boundary leaves takeover pending and refuses replacement execution.

The host-policy decision selected both shared and dedicated execution machines, with automatic shutdown available only through explicit opt-in configuration. The decided contract is recorded under [recovery on another machine](#recovery-on-another-machine). Whole-machine shutdown requires authority covering that machine and coordination of its affected sessions; a session-level recovery setting cannot grant it implicitly. Without confirmed termination, takeover remains pending. Independent durable storage, compatibility identity and the infrastructure used for native qualification remain open engineering decisions. Same-machine recovery remains the default.

The deployment decision keeps the default same-machine SQLite path free of a separate coordinator. A multi-host deployment may explicitly configure one. This permits an independent recovery authority without imposing an additional service on local users; it does not yet select the coordinator's storage engine. Missing remote authority would be an explicit configuration failure, not an implicit local fallback. The decided deployment boundary is recorded in the main recovery design.

The availability decision defers automatic coordinator failover. New calls and recovery wait for the configured authority to become available; no execution host may promote its local copy into an alternative authority. Outstanding publication is resolved by durable call identity before reporting success or deciding a retry outcome. This choice requires explicit outage and restart tests, but does not require a replicated coordinator in the first implementation. The accepted behavior is recorded in the main recovery design.

The storage decision selects SQLite on the optional coordinator's own persistent local disk. Execution hosts would use its API, never a shared database file. The coordinator is independent of execution machines; its storage loss is not covered by execution-host recovery. This preserves the selected single-authority availability model without introducing a replicated database. The decided storage boundary is recorded in the main recovery design.

| Acceptance area | Required evidence |
| --- | --- |
| Host opt-in | Default local recovery requires no separate coordinator; a configured multi-host coordinator is opt-in; remote recovery refuses without trusted configuration; automatic shutdown defaults off and requires separate authority for its exact resource scope on shared and dedicated hosts; storage failure never starts a fresh session. |
| Coordinator outage | New calls and takeover refuse unavailable authority; active calls retain their deadlines; an ambiguous publication resolves by call identity after restart without local fallback or automatic coordinator promotion. |
| Independent storage | A replacement restores committed Python state, session files and retained results after losing access to the original execution machine and its local disk. |
| Immutable compatibility | Exact supported runtime, helper, guest, architecture, checkpoint-format and policy identities are checked before execution; matching hashes alone do not establish portability. |
| Competing owners | Concurrent takeover attempts select one new generation; stale admission, publication and replay of old shutdown evidence refuse. |
| Old execution | A partitioned supervisor remains alive in a negative control; the replacement cannot execute until the configured shutdown boundary is confirmed. |
| Crash and lost acknowledgment | Interrupt takeover and publication before and after their durable transitions; retry observes the same decision/result without repeating committed code. |
| Corruption and storage faults | Incomplete payloads, mismatched hashes and unavailable or ambiguous storage refuse without replacing the last committed checkpoint or losing retention charges. |
| Native compatibility | Test separate compatible Linux/KVM machines and, independently, Windows/WHP machines; retain source/runtime identities and cleanup evidence. Windows/Linux checkpoint portability remains unclaimed. |

Ordinary process tests can qualify the ownership protocol and its refusal paths. They cannot establish infrastructure shutdown, network enforcement, native checkpoint portability or survival of a storage-service failure. The eventual evidence must distinguish those boundaries, and #1671 remains open until its selected remote-recovery requirements are exercised.

#### Coordinator ownership candidate

The first candidate in [remote_authority.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_authority.py) implements the durable ownership journal on coordinator-local SQLite. It is explicitly enabled, preserves compatibility identity and monotonically increasing generations across restart, and permanently refuses reuse of old execution-resource incarnations. A termination request revokes affected owners before confirmation. A guest request affects its session; an explicitly authorized whole-machine request revokes every active session recorded on that machine incarnation and blocks additional admissions there. Termination confirmation leaves sessions stopped; a separate acquisition grants a new owner. Request retries and repeated confirmations cannot reactivate stale generations.

The coordinator's trusted termination verifier receives the persisted request, including its exact resource incarnation and affected sessions. The candidate does not implement a shutdown provider or accept a worker-supplied claim as proof. Automatic requests require separate opt-in configuration; whole-machine requests additionally require that host in the configured authority scope. Resource incarnation and compatibility values are trusted inputs pending authenticated service integration. Metadata digests detect accidental alteration, not malicious edits by a trusted writer.

A short `Authority.guard` transaction checks ownership and serializes trusted publication rows against revocation. The caller must keep payload transfer and guest execution outside this transaction and must leave transaction completion to the guard. The process controls use a small synthetic publication table to test this ordering; they do not integrate the existing checkpoint/result store or establish native publication. The journal caps each retained metadata ledger and applies a 32 MiB SQLite database ceiling. These are experimental metadata bounds, not a transferred implementation of #1672's checkpoint, result, scratch or retention accounting.

[Process controls](../../../tests/test_mxc_remote_authority.py) exercise competing real processes, stale owners, pending termination across restart, shared-host scope, unverified evidence, metadata changes, read-only storage and `SQLITE_FULL`. Separate supervisors exit before and after revocation, confirmation and synthetic publication commits. A dedicated [GitHub workflow](../../../.github/workflows/mxc-remote.yml) runs the controls independently on Linux and Windows. Local qualification passed 41 ownership controls; the earlier combined run also passed all 102 existing shared-store controls. [Run 38175084631, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38175084631/attempts/1) passed all 41 controls with no skips on each platform at candidate `53237669d0bc508ca0b0b1502b565704879d2035`, tested merge `936a75bd702903027fcb68e7f4e2e88111bc1c0e`. Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. The [report](https://github.com/sokolaidev/maf-extensions/actions/runs/38175084631/attempts/1) records runtime identities, all control names and original JUnit hashes. A clean local full-suite run at the fixed candidate passed 18,121 tests with 910 skips; an earlier run edited during execution mixed schema versions and is not qualifying evidence. [PR #1862](https://github.com/sokolaidev/maf-extensions/pull/1862) retains this candidate. Evidence-only commit `d2bbe72814ba4c855fafcd06ba17ab1b06e10286` did not alter those executable controls; the retained report qualifies only the original candidate above.

The startup follow-up in `4cb769e5d95bdf371c1ff2b1c50ca81ec6690a95` changes initialization to detect an empty schema under the initializer lock and transaction. It adds four controls: process death before schema creation, before and after the first commit, and preservation of a nonempty foreign schema. Both pre-commit restart cases failed against the original implementation. [Run 38178914909, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38178914909/attempts/1) passed all 45 controls without skips on Linux and Windows at tested merge `589294a23929e4cc0f9cbefbcc9626084a04d876`, which includes that follow-up commit. Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. This later run provides the follow-up evidence; it does not change the original 41-control report or broaden the ordinary-process qualification into native multi-host recovery. The related local-store initialization contract is tracked separately in [#1866](https://github.com/sokolaidev/maf-extensions/issues/1866).

A further startup-hardening candidate validates every required coordinator table against its creation definition, preserving additional trusted publication tables while refusing missing or altered ownership tables. Initialization OS and SQLite errors, including nonblocking lock contention, become domain-level `Refused` errors. Seven additional controls failed on the preceding implementation: four partial/altered schema cases, a real competing initializer process, a corrupt SQLite header and unavailable root storage. All 52 coordinator controls then passed locally. The 45-control hosted run above predates this hardening and does not qualify these seven cases.

This candidate leaves #1671 open. An authenticated API, checkpoint/result transfer and retention integration, a deployment-specific termination verifier, network-authority revocation and native recovery between distinct compatible machines remain necessary. No infrastructure shutdown, native cross-machine recovery, coordinator-storage-loss recovery or production support is established by these process tests.

#### Local-store initialization recovery

The follow-up for [#1866](https://github.com/sokolaidev/maf-extensions/issues/1866) extends interrupted-first-initialization recovery to the experimental `Store` and `SharedStore`. It classifies an empty SQLite schema under the initialization lock and write transaction, commits the legacy schema with its first session metadata, and refuses damaged or foreign nonempty schemas without repair. The [startup contract](#interrupted-first-initialization) retains missing-metadata/profile/format refusal and excludes external replacement or truncation of a committed database. Complete existing legacy stores remain readable; partial schemas from the earlier non-atomic initializer remain refused.

[Run 38320389173, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38320389173/attempts/1) qualified implementation source `0c78ba883e98a0d18cd297bffba13e74aaf5c3ed` independently on Linux and Windows. Each host passed all 81 [startup controls](../../../tests/test_mxc_local_initialization.py) without skips, including abrupt process death around connection/schema/metadata/commit boundaries, the separate shared-owner transaction, competing initializers, generation continuity, saved-result retry, checkpoint restore and unchanged damaged-store refusal. The complete storage workflow passed 420 controls and skipped one platform-specific permission control on each host: Windows child ACLs on Linux, POSIX ownership on Windows. The initial Linux run exposed a test fixture with broad directory permissions; the successful source creates its private root before testing migration refusal.

| Evidence | Linux | Windows |
|---|---|---|
| Python / SQLite | 3.12.3 / 3.45.1 | 3.12.10 / 3.49.1 |
| Startup controls | 81 passed, no skips | 81 passed, no skips |
| Full storage controls | 420 passed, 1 platform skip | 420 passed, 1 platform skip |
| JUnit SHA-256 | `23418933e627a1e54dd13bf71e4a89ff4c690cccd14aa09d32d9797446ead729` | `f1b385b06857bdea08137c4e1d9a83dd6e163fa6d88d2d9e51928bb639fb183f` |

These controls use ordinary host processes and synthetic checkpoint bytes. They do not qualify native Hyperlight startup, power loss, arbitrary store erasure, cross-machine recovery or production support. #1671's remaining integration and native qualification scope is unchanged.

#### Linux helper exit during identity observation

The follow-up for [#1869](https://github.com/sokolaidev/maf-extensions/issues/1869) treats `ProcessLookupError` (`ESRCH`) from reading `/proc/<pid>/stat` as process disappearance, alongside the existing missing-file case. [Linux proc documentation](https://docs.kernel.org/filesystems/proc.html#process-specific-subdirectories) specifies that an open descriptor neither retains the process nor observes a replacement that reuses its PID; operations after exit commonly fail with `ESRCH`. The exception boundary remains confined to the process stat read. Boot/namespace observation failures, permission and other I/O errors, malformed evidence, machine mismatch and namespace mismatch still refuse reconciliation; process creation identity remains mandatory for a live PID.

The [regression controls](../../../packages/maf-sandbox-mxc/tests/test_mxc_process_identity.py) inject read errors after a successful file open and exercise capture, stopped observation and termination. Four controls failed against the preceding implementation, including namespace mismatch after a disappearing process. A Linux-only control repeats a real child exit between opening its proc stat file and reading that descriptor 32 times. These controls qualify ordinary host-process identity handling, not native Hyperlight termination, arbitrary descendants or cross-machine recovery. The local Windows full suite passed 19,041 tests with 922 skips; lint, formatting, package and repository type checks, Markdown examples, documentation paths and live tracker checks also passed.

[Run 38330119277, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38330119277/attempts/1) qualified implementation source `6c9785a31fabbaac180c72ba81860e03f3330d89` on GitHub-hosted Linux and Windows. Linux passed all 26 identity controls, including the 32 real exit-between-open-and-read iterations. Windows passed the 25 deterministic controls and skipped the Linux-only descriptor control. The full storage matrix passed 526 controls with one platform skip on Linux and 525 with two platform skips on Windows. The additional skips are the Windows private-root ACL control on Linux and the POSIX ownership control on Windows. This evidence applies to the cited executable source; the subsequent evidence documentation does not broaden its qualification.

| Evidence | Linux | Windows |
|---|---|---|
| Python / SQLite | 3.12.3 / 3.45.1 | 3.12.10 / 3.49.1 |
| JUnit SHA-256 | `771f4d2da00b7b6a3cfaf9d3f110e633ddac7f0e0a5829106d15cb2f43d1ec35` | `8f792390fa869928041a4aea0bd16341fdfc4ee803722bcbf39ede1584b15954` |

#### Mutual-TLS coordinator publication candidate

The selected authentication mechanism is mutual TLS. The [API candidate](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_api.py) verifies the client chain and exact certificate fingerprint before dispatch. Trusted configuration maps each fingerprint to a host and boot incarnation, permitted sessions and permitted operations. Request bodies cannot override host identity, boot identity or the session's configured runtime/policy profile. Clients verify the coordinator chain and hostname. Certificate issuance and rotation remain host-managed; a configuration restart removes access for withdrawn fingerprints without pretending to terminate their guests. Worker-selected guest incarnation identifiers remain trusted lifecycle inputs, not independent attestation.

The service exposes acquisition, call admission, publication, saved retry and checkpoint retrieval. It serializes requests and owns the existing shared SQLite publication store on coordinator-local disk. It does not mount that file on an execution host. Publication reuses `SharedStore` checkpoint/result atomicity, quota reservations, retention and the persisted forgiveness budget. A final authority guard checks the exact owner generation after checkpoint preparation and stays held across the publication commit. The ownership and publication databases remain separate: the guard serializes revocation against that commit, rather than claiming a distributed transaction. No payload transfer occurs under the final authority guard. Failure after the publication commit can leave an ambiguous response, which the client resolves with the original call identity; it never automatically resends an execution request.

The initial version-1 transport is one length-prefixed JSON record per TLS connection, with an 8 MiB frame ceiling, a 64 KiB call-request ceiling and a 4 MiB combined configured checkpoint/result ceiling. Checkpoint inventories contain at most the configured file count, with flat validated names; nested/native checkpoint transfer is supplied by the later version-2 candidate below. One private transfer directory bounds temporary payloads independently of retained data. The exclusive coordinator process lock permits bounded cleanup of that directory after restart; links, nonregular entries and over-budget contents refuse cleanup. Interrupted call reservations remain charged and require explicit recovery; transfer cleanup does not authorize execution or release those reservations. The service has no worker operation for requesting shutdown or confirming termination. Trusted coordinator administration still uses the ownership journal's configured termination verifier.

The [new controls](../../../tests/test_mxc_remote_api.py) use real TLS sockets and separate coordinator processes. They cover certificate and hostname rejection, session/operation scope, borrowed-owner refusal, revocation during checkpoint preparation, held authority through commit, retained result/checkpoint delivery, abrupt process death before/after commit, certificate removal, outage refusal and bounded transfer recovery. The replacement-owner control supplies synthetic trusted termination evidence; it does not establish infrastructure shutdown or denial of a native guest's network authority. These controls leave [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) open for native-size transfer qualification, remote scratch/reservation reconciliation, a deployment termination verifier, network-authority revocation and native recovery between distinct compatible machines. Production support, coordinator-storage-loss recovery and power-loss durability are not established.

[Run 38416642358, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38416642358/attempts/1) passed all 91 ownership/API controls on each platform, with no skips, at implementation source `5a6d6216a5984d176effb319caad5b757568635e`. This includes all 39 API controls, real TLS connections, coordinator process restarts and the final private-staging regression. Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. The [report](https://github.com/sokolaidev/maf-extensions/actions/runs/38416642358/attempts/1) records every control name, both runtime identities and original JUnit hashes. The preceding Linux candidate exposed a restore destination outside the required private staging boundary; the qualified implementation restores beneath that boundary and validates its bounded cleanup inventory.

At implementation source `5a6d6216a5984d176effb319caad5b757568635e`, the local Windows suite passed 19,241 tests with 923 skips; lint, formatting, all package and repository type checks, documentation references, implicit concatenation, Markdown examples and live tracker checks passed. Removing the final commit guard caused all three fencing controls to fail. The unchanged shared-store integration also passed the existing [storage matrix, run 38415640027](https://github.com/sokolaidev/maf-extensions/actions/runs/38415640027/attempts/1) on Linux and Windows at preceding source `235b039f30d85def0268c846bf5d0f2b795d22d0`; that run does not qualify the later API restore correction. The later workflow-trigger correction at `27a5e806c147e57d1033a4f4d3310fd0418c6035` added four workflow coverage controls; its local Windows full suite passed 19,245 tests with 923 skips, and lint, formatting, all package and repository type checks, documentation references and implicit concatenation passed. The earlier review-validation attempt had a subprocess-output failure under inherited pytest parallelism and an ownership-contention failure; both passed in isolation, followed by the clean full run with parallelism confined to the top-level command. That correction and subsequent evidence prose leave both executable candidates unchanged and do not extend qualification to native recovery between machines.

#### Native-size checkpoint transfer qualification

The version-2 [coordinator transport](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_api.py) streams checkpoint files with a bounded, hashed inventory. The retained Linux native [format-7 report](https://github.com/sokolaidev/maf-extensions/actions/runs/38145859671/attempts/1) contains an 862,572,544-byte blob under `blobs/sha256/`; a flat 4 MiB request cannot represent that checkpoint. The selected extension supports the shared store's 2 GiB checkpoint ceiling and nested portable paths while keeping results at a separate 4 MiB ceiling. Detailed limits and client methods are in the [publication contract](#optional-coordinator-api-experiment).

Transfer uses one authenticated connection with a ready response before upload, bounded binary blocks and an explicit completion record. Partial transfers are disposable staging, not resumable sessions. Every upload must match an admitted request and current reservation, and the existing final ownership guard still covers the SQLite publication commit. After an ambiguous acknowledgment, the host queries the original call identity. Download verifies every file and the final ownership response before exposing its destination. Cleanup neither releases a call reservation nor proves guest termination.

The [process controls](../../../tests/test_mxc_remote_api.py) exercise nested and empty files, oversized and aliased manifests, corrupt bytes, interrupted uploads/downloads, storage write failure, revocation during upload/download, coordinator death at publication boundaries, restart cleanup, retry and destination preservation. The [remote workflow](../../../.github/workflows/mxc-remote.yml) selects a 1 GiB synthetic blob for both Linux and Windows and records its payload size in JUnit alongside the existing source/runtime evidence. The default local fixture is 8 MiB. These are transport and publication controls using synthetic bytes; neither fixture boots a transferred Hyperlight guest. [Run 38803765472, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38803765472/attempts/1) passed all 128 ownership/API controls on each platform, with no failures or skips, including a 1,073,741,826-byte checkpoint (1 GiB blob plus the index). The [report](https://github.com/sokolaidev/maf-extensions/actions/runs/38803765472/attempts/1) records the control names, payload size, JUnit hashes and runtime identities. Both runners tested merge source `ca6ec868abe2a54d21cc9577ccafd3bab789f507`, whose parents are baseline `d6a512c2b3d77500a3d16921779dc96fced3dee7` and implementation `3f46866756c910ebafeac10ff7418e909c7bcb90` in [PR #1954](https://github.com/sokolaidev/maf-extensions/pull/1954). Linux used Python 3.12.3 / SQLite 3.45.1; Windows used Python 3.12.10 / SQLite 3.49.1. The subsequent retained-evidence update changes no executable behavior.

Issue [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) remains open for remote execution scratch/reservation reconciliation, a deployment-specific termination verifier, network-authority revocation and native recovery between distinct compatible machines. Cross-OS checkpoint portability, coordinator-storage-loss recovery, power-loss durability, production serving and a 2 GiB end-to-end payload are not established by the 1 GiB transport control.

Local verification at implementation `3f468667` passed 132 focused API/ownership/workflow controls, lint, formatting, all package and repository type checks, documentation references, implicit concatenation, Markdown examples and 281 live tracker checks. The full Windows suite recorded 19,535 passes, 924 skips and three startup-timeout failures in the unchanged Hyperlight process module. All 18 tests in that module passed in isolation on the implementation branch and on clean baseline `d6a512c2`; this is not a claim of a clean full local suite.


### Remote scratch reconciliation candidate (2026-10-16)

The next #1671 boundary is execution-host temporary capacity. The coordinator now offers an opt-in format-2 authority journal with immutable, call-bound scratch receipts, durable byte accounting across configured hosts and a separate trusted controller reconciliation method. Publication preserves scratch charges until cleanup proof; a new owner cannot reuse the old allowance merely because fencing succeeded. Admission and cleanup use conservative ordering across the authority and publication databases, leaving capacity charged after ambiguous outcomes rather than claiming a distributed transaction. Released receipts remain bounded idempotency tombstones.

The [experiment contract](#optional-remote-execution-scratch-accounting) specifies the required verifier evidence and both crash sequences. The [controls](../../../tests/test_mxc_remote_api.py) use real TLS, SQLite and coordinator process death, with synthetic trusted cleanup evidence. This implements the accounting/reconciliation candidate; native host integration must still enforce scratch bounds, prove exact helper termination and actual file removal, and prevent delayed recreation. Deployment-specific fencing, network-authority revocation and native recovery between distinct compatible machines remain open under [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671). Automatic shutdown remains separately opt-in, and same-machine operation still needs no coordinator.


#### Native execution-host binding candidate

The [native adapter](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_host.py) reuses the format-7 local launch journal and bounded native exporter. Each remote receipt becomes a permanent local call identity whose request digest binds the complete receipt. Restore and candidate files remain inside journal-managed scratch, and the trusted controller verifies exact helper termination and file removal before the coordinator releases its charge. Cleanup retains the terminal local identity so a delayed receipt cannot recreate scratch or execute again. Unknown launches, missing journals and unverified cleanup continue to consume capacity.

This deliberately keeps the coordinator's checkpoint/result store authoritative and avoids another process journal or cleanup protocol. The [contract](#native-host-binding-for-remote-scratch) separates bounded trusted writers from an OS disk quota, and same-machine local verification from a deployment's trusted remote controller transport. Process controls cover host death, active helper termination, partial cleanup, receipt changes and replay refusal. The MXC 1.0 Linux/Windows runner passed native Python-state publication and restoration through mutual TLS in [run 38970794986, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38970794986/attempts/1). The [report](https://github.com/sokolaidev/maf-extensions/actions/runs/38970794986/attempts/1) binds implementation `266daedf00e00983222b037dec52bdf6d41b0755`, helper/rootfs identities, native checkpoint sizes above 862 MB and both JUnit hashes. Each platform passed the native control without skips; the separate remote matrix passed 199 ordinary controls on each platform, including 1 GiB transfer and host process recovery. The native control restores counter state after coordinator restart, replays committed results without loading the helper, verifies cleanup between calls and refuses a delayed local receipt. These are same-machine results on each platform. Deployment-wide termination verification, network-authority revocation and native recovery between distinct compatible machines remain open under [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671).

Local Windows verification passed all package/repository type checks, lint, formatting, Markdown examples and 291 tracker checks. The full-suite rerun recorded 20,035 passes, 926 skips and one failure in the unchanged certificate-removal test: the client received a transport refusal instead of the authorization text the assertion expected. That control passed in isolation; unchanged main passed 21 repetitions without reproducing the mismatch. This is not a clean full local-suite claim. Normal hosted PR checks passed at the implementation revision.


#### Deployment controller fencing candidate

The next #1671 increment connects the coordinator's pending fence to a separately authenticated host-controller endpoint. [The implementation contract](#deployment-controller-for-generation-fencing) binds one provisioned local journal to one complete coordinator owner, retires that generation durably before cleanup, and requires exact owner/request evidence before confirming termination. Shared hosts retain guest-scoped process cleanup; machine shutdown is unsupported. Automatic termination remains off unless explicitly enabled at both ends.

This closes the trusted-controller transport and generation-retirement gap for the pinned closed-network experiment. It does not infer success from an unreachable host, steal a live supervisor's lock, release remote scratch accounting, or treat a missing journal as proof. Deployment configuration must keep every call for an owner on its single retained journal root. Ordinary process/TLS tests cover interruption and refusal; native recovery between distinct compatible machines, external network-authority revocation, infrastructure shutdown and production integration remain open under [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671).


#### Cross-runner native qualification candidate

The #1671 experiment uses three concurrent Linux GitHub runners joined through a temporary Tailscale overlay, with GitHub OIDC and the existing mutual-TLS worker/controller boundaries. One runner retains the coordinator journals while A publishes and B restores a common pinned native fixture. The source-only driver covers explicit fencing, saved-result replay without a helper, fresh state restoration, coordinator process restart, stale/competing-owner refusal and missing-journal refusal. Setup, evidence retention and remaining limits are in [the host-publication record](#native-recovery-between-github-runners). The workflow is manually dispatched and requires a scoped tailnet identity; adding it is not evidence of a successful hosted native recovery run.


#### Hosted native cross-runner evidence (2026-10-24)

[Run 40048039643, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40048039643/attempts/1) passed on three distinct Ubuntu 24.04 x86-64 runners, with KVM on the execution hosts, at source `7fb50bd851fedc588d3e4e12c0066242bbd300c3`. The downloaded A, B and coordinator reports each record `status: passed`, `distinct_boots: 3`, Python 3.12.3, a closed guest-network policy and matching helper/startup identities. Tailscale authenticated the hosts through GitHub OIDC; the worker and controller requests used mutual TLS. The runtime remains the pinned MXC 1.0 candidate with the removable session, output and storage overlays.

| Retained measurement | Value |
|---|---|
| Helper SHA-256 | `0be69b19564bf17821f899c60ec2d7f68114586277c1c91337822cd78f91bb9f` |
| Startup index SHA-256 | `768133b432b6168cec2b31a33faa14526482e401c80d80bf943e85bc423bb9c8` |
| A checkpoint | 862,203,088 bytes |
| B replacement checkpoint | 862,223,568 bytes |
| A report SHA-256 | `e90768ad55751f21d76e8b262f990b3e47a05c4bd5625492252e69210ec31cba` |
| B report SHA-256 | `acb4269098b38839a9960fbfd5c5ebba5c643d3691ec5f479816b03f65e71cc9` |
| Coordinator report SHA-256 | `c686b9496e039056cc290522d1024ef0906fb02cfd9b9561cf472587a42e379f` |

The fixed scenario restored `remote_counter = 41` on B and advanced it to 42. It passed coordinator-process restart, retained-result replay with an absent helper, competing-owner refusal, missing-journal refusal, stale-owner refusal, retired-receipt refusal and scratch reconciliation. Checkpoint/result transfer used the coordinator API; GitHub artifacts distributed only the initial common fixture and discovery records. Both execution generations were explicitly fenced and reconciled.

[Run 40043993705](https://github.com/sokolaidev/maf-extensions/actions/runs/40043993705) previously reached B's fresh native call but failed with `native execution/capture failed: 1`; helper output was not retained. [PR #2125](https://github.com/sokolaidev/maf-extensions/pull/2125) added bounded diagnostic retention and changed no runtime behavior. The successful run does not explain that earlier failure or establish a failure rate.

##### Repeat blocked by CPU-vendor compatibility

[Run 40080442094, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40080442094/attempts/1) failed at source `0969fdda89aeb1f51c97eb19ae5d96c23b24b58d`, with no changes to the native harness since the successful run. All three reports recorded failed status, distinct boots and matching fixture profiles. Authentication, discovery and the competing-owner refusal completed; A's initial seed call failed before publication or B's recovery. Its retained 177-byte `native.stderr` identifies the cause: `snapshot CPU vendor mismatch: file was created on AuthenticAMD but the current CPU is GenuineIntel (snapshot produced by hyperlight 0.17.0)`. Standard output was empty.

| Failed repeat identity | SHA-256 |
|---|---|
| Helper | `e38366787798f63b17a7131c5c2b5da581e3d75259a2b1e4124ad952c4966189` |
| Startup index | `984ad61bd3c566b23ebcda4b321fdf05e94bc1e58a8d989c2e098ba8c50c627e` |
| A report | `f6329e59085f6a3c16a14e67135b143868755935e5b961e296bf165d1a1e65d9` |
| B report | `bf7aaa33caade74d7fe32bd01c9b463a7e30a4d2e0f7ebf94d21cacebf63b0b5` |
| Coordinator report | `050ad82ce964e5eebc7dbc84c90d970c5686b31f6d87ce69495afc8a6bfac058` |
| A native stderr | `eed89853dee944314e114c0c926f001aa2e5c2c82bea89e6fd26745d47bd23e1` |

This refusal is an upstream compatibility boundary, not evidence of a transfer or fencing defect. Hyperlight 0.17.0's [snapshot loader](https://github.com/hyperlight-dev/hyperlight/blob/v0.17.0/src/hyperlight_host/src/sandbox/snapshot/file/config.rs) checks architecture, snapshot ABI, hypervisor and CPU vendor; its [snapshot documentation](https://github.com/hyperlight-dev/hyperlight/blob/v0.17.0/docs/snapshot-versioning.md) explicitly separates Intel and AMD snapshots. The discovery profile used by that run compared helper/startup hashes, policy, operating system and architecture, which did not establish CPU compatibility. A shared `ubuntu-24.04` label likewise does not establish that the fixture producer and execution runners share a CPU vendor; GitHub's [standard runner specification](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories) specifies x64 without pinning Intel or AMD. The coordinator performs no native restoration and does not need the execution hosts' CPU vendor.

The [CPU-admission candidate](#cpu-compatibility-admission) now binds the fixture producer vendor to its helper/startup hashes and workflow attempt, requires A and B to match it before session acquisition, and exempts the coordinator CPU from native compatibility. Missing or mixed CPU data refuses qualification, and the execution hosts recheck local identity before serving. Its regression controls use synthetic CPU data and a substitute helper; the matching-runner native repeat is qualified below. Preserve the upstream restore guard; neither rewriting snapshot metadata nor substituting fresh state is a recovery mechanism. An incompatible runner allocation must remain unqualified, not be counted as a passed recovery test. This repeat explains its own failure; without retained native diagnostics it cannot establish the cause of the earlier B failure.

The successful run qualifies the fixed workload on its recorded Linux hosts and common fixture. It does not establish abrupt execution-machine loss, coordinator-storage loss, physical reboot or power-loss durability, cross-OS checkpoint portability, arbitrary descendant termination, network-enabled guests or production integration. Coordinator storage was independent of A/B for the workflow lifetime only. [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) remains open for native failure cases before broadening the recovery claim. The CPU-admitted repeat below satisfies the matching-runner prerequisite. The next failure-case target is loss of A with takeover blocked until trusted termination evidence is available; an unreachable owner alone must never authorize B.

##### CPU-admitted native repeat (2026-10-25)

[Run 40145617038, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40145617038/attempts/1) passed preflight, native fixture construction and all three participants at merged source `951f9a88569e3b8577052f5c79fb73160dae51e0`, including the CPU-admission controls from [#2148](https://github.com/sokolaidev/maf-extensions/pull/2148). The fixture producer and execution hosts A and B each recorded `AuthenticAMD`; the coordinator recorded no local CPU vendor, as required for its exemption. All three final reports record `status: passed`, `phase: native`, `distinct_boots: 3`, Python 3.12.3 and the same closed-policy Linux/x86-64 fixture profile, source SHA, workflow run and attempt. The native fixture uses the pinned MXC 1.0.1 experiment and its existing removable overlays.

| Retained measurement | Value |
|---|---|
| Helper SHA-256 | `f7702e80d3cb5632d4077cbd0e139848c0793155f40467685e0e3457cd9f757c` |
| Startup index SHA-256 | `d86301be9a3f9227de3c4e10b9c02921a1449651cfa9df752037d0b2668a0efb` |
| A checkpoint | 862,420,176 bytes |
| B replacement checkpoint | 862,440,656 bytes |
| A report SHA-256 | `92075ca9fc85df587ed6ffee86ee5f5e55406b7fdbc0055b20eb82825746981b` |
| B report SHA-256 | `2210a05408ca1f1bb5b291957e428d221cbfbcb09ae2ff1d6057a971fc904695` |
| Coordinator report SHA-256 | `d2fb22e260535a38a115148ef12455d161e2a15f1cc59f3332d953450c89d92f` |

The coordinator report confirms competing-owner refusal, coordinator-process restart, missing-proof refusal, stale-owner refusal, retired-receipt refusal, saved-result delivery with an absent helper, native state restoration and scratch reconciliation. The fixed native program restored `remote_counter = 41` on B and advanced it to 42. Checkpoints exceeded the small-frame API limit and crossed the mutual-TLS streaming API; artifacts transported only the startup fixture and public discovery. Both execution generations were explicitly fenced and reconciled. The downloaded final role artifacts contained the three reports and an empty coordinator service log; no surviving helper streams were retained after successful cleanup.

This qualifies the fixed workload with producer-to-execution CPU admission enforced on this AMD runner allocation. It does not qualify Intel execution, arbitrary CPU models/features, cross-vendor or cross-OS restoration, abrupt execution-process or machine loss, coordinator-storage loss, physical reboot, power-loss durability, network-enabled guests or production integration. The synthetic refusal controls remain the evidence for incompatible admission; this successful allocation did not exercise a real mismatched pair. Native owner-loss and other failure cases remain open under #1671, with automatic shutdown disabled and trusted termination evidence required before takeover.


##### Worker-process crash candidate

The next #1671 case is [worker-process death after native capture and before publication](#worker-process-crash-before-publication), selected before whole-machine loss. The opt-in `worker_crash` workflow mode starts A's fixed 41-to-99 call in a separate process, waits for successful native capture, and kills the process before the coordinator can receive the candidate. The coordinator retains the committed value 41 and the interrupted call's scratch charge. B remains blocked before trusted fencing and across a restart with a pending fence; after controller verification and cleanup, it must replay the saved result, refuse the interrupted identity and restore 41-to-42.

This candidate leaves the existing successful baseline selectable and changes no published package. Ordinary controls exercise real process death and TLS/SQLite orchestration with a substitute helper. Native failure-case qualification requires an explicit run from the merged trusted workflow ref with `worker_crash=true`; the preceding successful repeat did not exercise this case. A's machine and controller remain available, and the native helper has exited before the crash. Loss during guest execution, an unreachable machine, coordinator-storage loss and infrastructure termination verification remain separate requirements. #1671 stays open.

##### Qualified worker-process crash before publication (2026-10-25)

[Run 40208706071, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40208706071/attempts/1) passed preflight, native fixture construction and all three participant jobs at merged source `33b9eb7dc4571359f322007fc21de4986a205a12`, with `worker_crash=true` from [#2167](https://github.com/sokolaidev/maf-extensions/pull/2167). The three reports agree on source, workflow attempt, closed-policy Linux/x86-64 fixture, Python 3.12.3 and `distinct_boots: 3`; each records `status: passed` and `phase: native`. The fixture producer and execution hosts A/B recorded `AuthenticAMD`; the coordinator's local CPU vendor is null under its existing exemption. The run uses the pinned MXC 1.0.1 experiment and removable overlays.

| Retained measurement | Value |
|---|---|
| Helper SHA-256 | `57af6fb00392e9325fcf80a9e8e0304cb17bee357c46a1df9f55275bdf59ed55` |
| Startup index SHA-256 | `2e692d7e1fc4a8329363e22bc11cf22eee1350cb99f91d598388065c1af6e54c` |
| Committed seed checkpoint | 862,313,680 bytes |
| Replacement checkpoint | 862,334,160 bytes |
| A report SHA-256 | `97ad6349994dbf4d6fb905745dc0b824500abb879c8816796a2dc36be359f83b` |
| B report SHA-256 | `42d0a60439c8c30b028e23232369503c88f9a65540942c79a358f344be7baea6` |
| Coordinator report SHA-256 | `01fe0335ef3ff4bef1406f1df0cda047cc04c925d0b92d9084db80bcd2461a0d` |

The coordinator report records `worker_killed: true`, `boundary: before_remote_publication` and `native_counter: 99`, together with all 13 scenario controls. A's worker completed native capture and was forcibly killed before sending the candidate publication. The coordinator retained the seed checkpoint with value 41 and the interrupted call's scratch receipt. B was refused while A remained owner, after the fence request without confirmation, and after coordinator restart with that pending fence. Only trusted controller verification and scratch reconciliation permitted B's newer generation. B replayed the saved seed result without a helper, refused retry of the interrupted call identity, restored 41 and advanced it to 42. Stale-owner and retired-receipt controls passed, and both execution generations were fenced and reconciled before completion.

The checkpoint sizes exceed the small-frame API bound and crossed the mutual-TLS streaming API. The downloaded report archives matched GitHub's advertised SHA-256 digests and contained only the three reports and an empty coordinator service log; no helper streams survived successful cleanup. The report hashes above bind the retained measurements to those original artifacts.

This qualifies worker-process loss after native capture and before publication for this fixed AMD/Linux workload. A's machine and trusted driver/controller remained available, and the native helper had finished before the crash. It does not qualify loss during guest execution, an unreachable or terminated machine, coordinator-storage loss, power loss, arbitrary CPU features, cross-vendor or cross-OS restoration, network-enabled guests or production integration. Whole-machine loss still needs independent infrastructure termination evidence; automatic shutdown remains disabled. #1671 stays open for those separate failure cases.

##### Active-guest worker-loss candidate

The next #1671 case extends the qualified post-capture boundary to [worker loss while the guest is still executing](#worker-process-loss-during-guest-execution). The opt-in `active_guest_crash` mode restores committed state 41, mutates it to 99, emits a fixed readiness marker and loops. The marker only synchronizes the fixed probe. The trusted driver requires a live native helper identity, kills the call worker and observes that helper stop through owner-pipe loss before any controller termination, with no candidate checkpoint or completion report. Takeover remains blocked until the existing trusted fence and scratch reconciliation complete; B must replay the seed result, refuse the interrupted call and restore 41 to 42.

The fixed probe uses the existing native `call-owned` mode because ordinary bounded output becomes visible only after execution finishes. It cannot return a publishable result and does not replace the normal bounded call path. Ordinary regression controls use real worker/helper processes with substituted native execution. This candidate adds a 14-control active-guest scenario, while preserving the baseline and qualified post-capture mode; the two crash inputs are mutually exclusive. Native qualification requires the merged trusted workflow with `active_guest_crash=true`. A's machine and controller remain available, automatic shutdown stays off, and #1671 remains open for this native run and the separate machine/storage failure boundaries.

##### Qualified active-guest worker loss (2026-10-28)

[Run 40779366624, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40779366624/attempts/1) passed preflight, native fixture construction and all three participant jobs at merged source `cf6a3f8b2ea3a07c0f5e6bbdb9aba6b2e6d72ecd`, with `active_guest_crash=true` and `worker_crash=false`. The three participant reports from that run agreed on source, workflow attempt, closed-policy Linux/x86-64 fixture, Python 3.12.3 and `distinct_boots: 3`; each records `status: passed` and `phase: native`. The fixture producer and execution hosts A/B recorded `AuthenticAMD`; the coordinator's local CPU vendor is null under its existing exemption. The run uses the pinned MXC 1.0.1 experiment and removable overlays.

| Retained measurement | Value |
|---|---|
| Helper SHA-256 | `c7c5d0c88a248b6c681ca03895899da7101d6fea07f848458c6019f016b874c9` |
| Startup index SHA-256 | `943812aec8c4bdd2873abd986c8b3e9e4be3b210e87077e054afad0b57ffd772` |
| Committed seed checkpoint | 862,170,320 bytes |
| Replacement checkpoint | 862,190,800 bytes |
| A report SHA-256 | `8583376f7cd9146abead53492c35e63a2f369bcc6d50787261345eccb7acea60` |
| B report SHA-256 | `2ccb059e58b67dd98f8e7aa2ac44f6d88f5c2ad4aad927d5748b6c8d3fbd699c` |
| Coordinator report SHA-256 | `ec86094dbf385811713117e286507562d8fd9097db25a12ee28c89c09b37ecd8` |

The coordinator report records `boundary: during_guest_execution`, `native_counter: 99`, `worker_killed: true`, `helper_stopped_after_owner_loss: true` and all 14 scenario controls. A's fixed guest restored committed value 41, changed its live value to 99, emitted readiness and kept executing. The trusted driver killed its call worker only while the recorded native helper was still alive, then observed that helper stop within the five-second bound before any controller termination. Neither a candidate checkpoint nor a native completion report was present after owner loss. The coordinator retained the seed checkpoint and interrupted call's scratch receipt; B remained blocked before trusted fencing and across coordinator restart with the fence pending. Controller verification and scratch reconciliation then permitted B's newer generation, saved-result replay without a helper, refusal of the interrupted call identity, and native restoration of 41 to 42. Both execution generations were fenced and reconciled before completion.

Both checkpoint payloads exceed the small-frame API limit and crossed the mutual-TLS streaming API. All three downloaded report archives matched GitHub's advertised SHA-256 digests; their contents were the three reports and an empty coordinator service log. Successful cleanup left no helper streams to retain. The evidence file preserves parsed reports, original report hashes, archive digests and artifact identifiers; report hashes refer to the original downloaded bytes, not their nested JSON representation.

This qualifies the fixed active-guest worker-loss case on this compatible AMD/Linux allocation. A's machine and trusted driver/controller remained available. It does not qualify an unreachable or terminated machine, coordinator-storage loss, physical reboot, power-loss durability, arbitrary CPU models/features, cross-vendor or cross-OS restoration, network-enabled guests, network-authority revocation or production integration. Whole-machine loss still requires independent infrastructure termination evidence. Automatic shutdown remains disabled, and #1671 stays open for those separate failure boundaries.

### Coordinator and storage failure follow-up

Cloud VMs and VM-level isolation qualification are deferred. Continue #1671 with the existing GitHub-runner and process/TLS experiments; worker-process loss remains the native qualification boundary. The coordinator/storage audit found the following coverage and gap:

| Failure boundary | Existing evidence or next control |
| --- | --- |
| Coordinator dies around ownership or fence commit | Ordinary subprocess controls in [test_mxc_remote_authority.py](../../../tests/test_mxc_remote_authority.py) preserve the committed transition and block replacement while proof is pending. |
| Publication acknowledgment is lost | Ordinary TLS/process controls in [test_mxc_remote_api.py](../../../tests/test_mxc_remote_api.py) resolve the original call after restart without reexecuting it. |
| SQLite is full, transfer fails, or cleanup verification is unavailable | Ordinary authority/API controls preserve the prior checkpoint, outstanding reservations and scratch charges. |
| Ownership or publication journal is missing, empty or corrupt at startup | The previous coordinator could recreate missing databases or defer opening publication storage. [Explicit provisioning and existing-only startup](#coordinator-journal-loss) now refuse these cases before serving requests; regression controls retain a pending fence, committed result and scratch charge through temporary loss. |
| Permanent coordinator storage loss or rollback to a valid old database | Unqualified. Refusal does not reconstruct ownership, call identities or checkpoint state; restoring backups needs a separate consistency and fencing design. |

The new startup contract requires `initialize=True` once for an empty root, then existing-only startup with all configured sessions present. The cross-runner driver initializes only its first coordinator process. These are ordinary storage-refusal controls; no new native recovery run or whole-machine-loss evidence is implied. #1671 remains open, automatic shutdown remains opt-in, and production integration is outside this experiment.

#### Valid but stale coordinator journals

A local reproduction at source `769b1a087d549fac339bb86b70c601f9204d78b0` confirms that existing-only startup does not establish journal freshness. The probe commits a seed call under owner A and saves both closed SQLite journals, then commits a second call, confirms A's fence with a synthetic trusted verifier and acquires owner B. It restarts against copies of the latest journals, either older journal paired with the latest other journal, and the older pair. Each copy contains a complete SQLite database; no bytes are corrupted and initialization remains disabled.

| Restored journals | Startup accepted | Revoked owner A accepted | Previously completed call admitted as new work |
| --- | --- | --- | --- |
| Both latest | Yes | No | No; saved result returned |
| Older ownership, latest publication | Yes | Yes | No; saved result returned |
| Latest ownership, older publication | Yes | No | Yes |
| Both older | Yes | Yes | Yes |

The probe checks admission through the coordinator API and does not execute a guest, demonstrate duplicate external side effects, or establish native recovery. Its unchanged-journal control preserves the expected fencing and retry behavior. The unsafe cases demonstrate loss of authority and completed-call history after restoring valid old data.

Full rollback protection needs a trusted freshness reference outside the journals' backup/restore boundary. A checksum, revision or cross-reference stored only in those journals can be restored with them; even a mutually consistent old pair cannot prove it is current.

Accepted decision: backup protection requires a host-provided durable record of the latest accepted revision outside that boundary. The protected experiment covers both stale and mismatched journal backups, including rollback of both journals. Recovery refuses when the record is unavailable or disagrees; restoring journals must not restore or recreate their freshness record. This is experimental protection, not a production qualification.

Accepted provider decision: the first implementation uses a separate SQLite database on the same machine, explicitly excluded from coordinator backup restores. The host supplies its location and maintains that exclusion; a different directory alone does not establish the guarantee. Protection covers rollback of the coordinator journals while the freshness record remains current. It does not cover a whole-machine rollback that also restores the freshness record. Independently hosted storage, cloud VMs and whole-machine qualification remain deferred.

Accepted adoption decision: the first increment provisions fresh protected coordinator stores only and leaves existing stores untouched. Existing journals have no independently retained freshness history, so creating their first freshness record from their present contents cannot prove those contents are current. Migration requires a separately designed procedure and remains deferred.

The protected API takes `Coordinator(..., freshness_root=host_retained_root, initialize=True)` for first provisioning, and the same `freshness_root` without `initialize` on restart. The coordinator root and freshness root must be separate, non-nested private directories. Provisioning requires both roots to be empty apart from their service locks. Each journal carries the freshness store identity, its role and a revision; the separate database retains the corresponding latest revisions. Startup requires both records, and every journal transaction checks its own revision. A protected journal refuses an open without the record configuration. Omitting the option continues the unprotected experiment for existing stores; it never migrates or certifies them. Partially provisioned protected roots refuse ordinary startup and reinitialization; no recovery or adoption procedure for those roots is supplied here.

Each journal mutation advances its revision and the retained head in one attached-database SQLite transaction. The experiment requires file-backed databases, `DELETE` journals and `FULL` or stronger synchronous settings, following SQLite's [multi-database atomicity conditions](https://www.sqlite.org/lang_attach.html) and [super-journal commit protocol](https://www.sqlite.org/atomiccommit.html#multi_file_commit). The record is attached only immediately before commit, so a read-only ownership guard can remain open while publication commits on its own connection. The record's service lock excludes another coordinator using that record. This preserves the existing conservative ordering between ownership and publication operations; it does not combine those two journals into one application transaction.

The cross-runner fixture explicitly enables this mode with sibling `service` and `freshness` directories. The host must retain the entire freshness directory, including SQLite recovery files, outside coordinator restores. Directory separation does not enforce the host's backup policy. Restores must occur with the service stopped; live file replacement, arbitrary edits to journal contents, rollback of the freshness storage itself, moving hot recovery files, interrupted-commit filesystem fault injection and power-loss durability remain unqualified.

The added `tests/test_mxc_freshness.py` controls exercise old ownership, old publication, an old pair and a current pair; absent, empty, corrupt or stale freshness records; missing configuration and wrong store identity; exclusive record ownership; fresh-only provisioning; scratch reconciliation; injected transaction failure; and process exits before the record update, after that update but before commit, and after commit. They test coordinator admission and result/checkpoint recovery without executing a native guest. The existing MXC remote workflow runs these controls on Linux and Windows and retains its runtime versions and JUnit results; a configured workflow is not evidence of a completed hosted run.

## Native output research

> Research for [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668): preserve bounded stdout/stderr bytes and keep native completion independent of guest output. The original proposal and investigation below describe the 0.9 experiment. The separate MXC 1.0 byte-stream profile is experimentally qualified on Linux/KVM and Windows/WHP; the final section records the candidate and validation boundaries.
> The later accepted bounded-console policy is recorded in the [MXC design](#bounded-console-capture).

### Source findings

The experiment pins MXC v0.9.0 at `86fb3d2abaf9c431556692037bff881830b543a5` and `hyperlight-unikraft` 0.14.1. The published crate records source commit `2b4790d1626e2645dd1f00469311424be8cb2f94`; that source pins its Unikraft submodule to `195cf5b65e52d9ca3b75b70949ee736a5eb2cad1`. These findings describe those revisions, not newer upstream releases.

Three boundaries lose information or lack a limit:

- Unikraft's [`init_posix_tty`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/lib/posix-tty/tty.c) initializes stderr with `uk_sys_dup2(1, 2)` and supplies the same output file for `/dev/stdout` and `/dev/stderr`. The host receives no original stream identity.
- [`hl_call_host_print`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/hcall.c) clamps the message length to 4096. [`hyperlight_console_out`](https://github.com/unikraft/unikraft/blob/195cf5b65e52d9ca3b75b70949ee736a5eb2cad1/plat/hyperlight/console.c) then returns the original length on callback success. This explains the previously observed silent truncation; retrying the same operation from Python cannot recover a suffix reported as written.
- Rust's [`GuestConfig::register`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/src/lib.rs) accepts `HostPrint` as a `String`, prints it to host stdout, and appends it to an unbounded `Arc<Mutex<String>>`. A parent pipe limit does not bound this accumulator. A string-only callback cannot provide the required arbitrary-byte transport.

### Original proposed boundary

Keep the public suite integration beneath MXC. Add separately pinned, reversible patches for the underlying Rust runtime and Unikraft kernel, with a host-controlled experimental feature and an explicitly different runtime profile. Do not edit the Cargo registry cache or replace historical runtime evidence.

Give the guest distinct stdout and stderr file objects so writes, duplicated descriptors and `/dev/stdout` or `/dev/stderr` preserve the destination selected by the program. Intentional program redirection still takes effect. Send bounded raw-byte chunks through a dedicated native callback with explicit stream identity. Keep kernel diagnostics separate. Validate the chunk length and stream value on the host; reject malformed input without using guest bytes as host completion records.

Bound captured bytes in the Rust callback before appending. Do not print captured guest data through a blocking host stdout path during execution. The proposed initial experiment limit is one MiB per stream. On overflow, latch a native failure, stop execution, retire the session and refuse checkpoint publication. Exact-limit output succeeds only if execution also completes normally. An overflow outcome must survive a guest that catches a write error. Timeouts, cancellation and owner loss remain independent termination paths.

Export stdout and stderr as bytes into host-owned bounded result files. Publish a small native control record separately, only after execution and capture succeed. Parse that record with a strict schema and size limit. The host must not infer success from markers, JSON, filenames or other content printed by the guest. No total ordering between concurrent stdout and stderr is promised; byte order within each stream is preserved.

### Compatibility and removal

Changing the embedded kernel changes the runtime's snapshot key, which [`build.rs`](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/2b4790d1626e2645dd1f00469311424be8cb2f94/build.rs) derives from kernel bytes and the snapshot contract. Build a fresh startup snapshot for this profile. Existing session checkpoints remain bound to their original runtime; do not silently restore them with the new kernel or replace them with fresh state.

Build the patched kernel on a GitHub Linux runner from pinned source and retain its hash and build identity. Use the same kernel artifact for separate Windows/WHP and Linux/KVM tests. A kernel build or one platform's pass is not evidence for the other platform.

Keep temporary runtime access behind the existing `probe/backend.rs` wrapper. Replacement requires an equivalent upstream API and rerunning the same behavioral tests before removing the patches. Source removal does not migrate checkpoints or revoke existing executables.

### Original qualification plan

Exercise zero-length output; 4095, 4096 and 4097-byte writes; the exact stream limit and one byte beyond; embedded NUL and all byte values; Unicode spanning chunk boundaries; mixed `write` and `writev`; repeated and concurrent writes; descriptor duplication/redirection; and guest text resembling native completion. Verify independent stdout/stderr byte hashes and no silent truncation.

For overflow, malformed native control, guest failure, timeout, cancellation and owner death, verify no successful publication, bounded retained output, native termination and refusal to reuse the failed session. Repeat recovery and lost-acknowledgment tests with the new runtime identity. Retain candidate-specific evidence and leave #1668 open until both platform qualifications pass.

### Why upstream has this shape

History checked on 2026-10-03 distinguishes a console design from the stronger execution contract we need. This is a source and public-discussion audit, not a statement of maintainer intent where none was published.

**Merged stdout/stderr is inherited terminal behavior.** [Unikraft PR #1226](https://github.com/unikraft/unikraft/pull/1226) introduces ordinary file objects for the system console and describes the serial option as preserving the earlier console behavior. Its [stdio initialization commit](https://github.com/unikraft/unikraft/commit/62e8e3983b569c1f71cb4d13345b0fc9fca19ab6) duplicates stdout into stderr. Both writing to one terminal is reasonable console behavior; separate capture requires choosing distinct destinations before that merge. It is not a Hyperlight-specific prohibition on separate streams. [The console-interface work](https://github.com/unikraft/unikraft/pull/1464) also explicitly removed the largely unused distinction between kernel/debug printing. Changing a shared console globally would therefore affect more than Python output.

**The small callback has a defensible boot and performance role.** The [HostPrint introduction](https://github.com/unikraft/unikraft/commit/65a15925fd551aadc9c6fd89bf5b179e442d3032) replaces one VM exit per byte with one per buffer. It uses a fixed stack buffer and preserves a debug-port fallback before host calls are available. Later [host-call buffer work](https://github.com/unikraft/unikraft/commit/b632b9e3de5596511f30c5ef176edd3e98405dbd) explicitly keeps boot-time queries on small stack buffers while allocating generic transport buffers after allocator initialization. This supports retaining an allocation-free early console; it does not justify acknowledging bytes that were never sent. Classifying the silent truncation as a bug is our engineering assessment, not an upstream maintainer verdict found in discussion.

**Capture was added as convenience, without an output-quota policy.** The [capture commit](https://github.com/hyperlight-dev/hyperlight-unikraft/commit/6be6da5465a195d091e4f16234b7d777f006821e) adds the string accumulator and `drain_output()` alongside live printing. The commit contains no rationale for leaving total accumulation unbounded. A console streaming sink and a bounded execution-result collector have different needs; a reusable upstream API could expose a configurable sink and limits without imposing this suite's one-MiB proposal on all users.

**MXC documents process-level console capture.** Its [pinned Hyperlight backend](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) says output goes through HostPrint to the executor process's stdout, leaving the structured stdout/stderr fields empty. It drains the duplicate runtime buffer after execution. The observed limitation follows that integration model. [MXC PR #349](https://github.com/microsoft/mxc/pull/349) fixes terminal chunk presentation, not guest stream identity or native truncation.

**Structured results already have a separate upstream direction.** Current `hyperlight-unikraft` main at `3df47f64f99229e3cebef07b22ba948c69e1398c` declares version 0.17.0. Its [function-call API](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/3df47f64f99229e3cebef07b22ba948c69e1398c/docs/calls.md) returns structured values separately from printed output, with documented 64-KiB guest-result and host-function message limits. This newer API is absent from our pinned 0.14.1 experiment. Even the pinned runtime already signals execution completion through a native `CallDone` event rather than stdout parsing. Neither mechanism provides arbitrary stdout/stderr capture, and guest return values remain untrusted data rather than proof of durable host publication.

The current main's [Rust callback](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/3df47f64f99229e3cebef07b22ba948c69e1398c/src/lib.rs) still appends HostPrint text without a total limit, and its pinned [kernel callback](https://github.com/unikraft/unikraft/blob/bd441efa1f585ef7089cee8900741a3a8ac48374/plat/hyperlight/hcall.c) still has the 4096-byte clamp. Upgrading alone does not resolve #1668. The kernel console also carries an explicit TODO to move kernel diagnostics out of captured guest output. Public issues and relevant PR histories yielded no explicit rejection of bounded separate streams; absence of such a discussion does not establish whether maintainers would accept a particular API.

### Revised recommendation before implementation

Do not treat the three-layer patch as the only possible solution. Preserve the early console and its existing default behavior. Separate the concerns:

1. Prove and fix silent console truncation with a small regression and chunked or correctly reported writes; keep early-boot constraints intact.
2. Propose an opt-in bounded byte-output sink with explicit overflow semantics. Guest output must not block a host callback indefinitely, and a guest catching an I/O error must not clear a latched overflow.
3. Compare a dedicated guest output device against driver-level descriptor redirection into separate pipes or files. Redirection may avoid changing the kernel, but must qualify native-extension writes, descendants, concurrency, bounded storage/draining, and persistent descriptor state; replacing only Python's `sys.stdout` is insufficient. The open upstream [x86_64 Bash pipe failure](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/124) is a reason to test that alternative, not assume it works.
4. Evaluate the newer structured result API independently for result transport. It can reduce custom transport work but cannot replace stdout/stderr fidelity or the host's checkpoint publication protocol.

A kernel patch remains a candidate after those comparisons, with new snapshots and both-platform qualification required. No upstream issue/comment/PR was sent during this investigation, and the pending extension decision has not been treated as approved.

### MXC 1.0 implementation and qualification (2026-10-08)

Merged [#1784](https://github.com/sokolaidev/maf-extensions/pull/1784) supplies the qualified MXC 1.0 session, bounded-console and storage baseline. The new experiment extends that baseline with a separately removable kernel layer in [`mxc_streams_patch`](../../../scripts/experiments/mxc_streams_patch). It pins MXC `7bf210247986cb73b1b314df60c2f1109c479c0b`, runtime `8f636e00cdf29e6c33ba0f578482c595d7827cf6`, and Unikraft `978c8a7d3ac33aa06cef870aead0182c89974002`. The patch metadata also pins the elfloader and libelf submodules and checks every affected source file before applying or removing the layer.

The opt-in kernel configuration gives stdout and stderr separate file objects, including their `/dev` entries. Each write carries a stream ID and raw bytes through a dedicated `MafWriteBytes` callback in chunks of at most 4096 bytes. This avoids the serial console's string conversion, newline rewriting and fixed-message truncation. Descriptor duplication and program redirection still select the destination file object. Kernel diagnostics retain the existing console. The profile also routes the pinned runtime's older devfs implementation through the same byte file objects and registers its missing stderr device. Both paths are covered by the native results below.

The GitHub Linux build retains the kernel hash, source identities, builder image identity and installed toolchain package list. The upstream builder uses a tagged base image and unversioned package installation, so these records identify the observed build rather than promise reproducible bytes across future builds. Both native host qualifications must consume the same resulting kernel artifact. A successful build is reported as `built-not-qualified`.

The host maintainer accepted one MiB per stream on 2026-10-08: retain a byte prefix, report omitted bytes with saturating counters, and let execution continue. Each stream has its own budget, reset by the host between calls. A successful guest write still reports its full length when retention truncates. Malformed transport remains a latched failure that retires the session and refuses checkpoint publication. This supersedes the earlier fail-and-retire overflow proposal for this new profile.

The new profile explicitly reserves three MiB for a result envelope, covering two full one-MiB streams encoded as base64 plus bounded metadata. The format-4 result allowance now permits this explicit configuration; its two-MiB default and the older store remain unchanged. Admission reserves this allowance before native execution, and scratch accounting includes both byte files and separate bounded diagnostics. Existing console capture, result envelopes and historical evidence remain unchanged. The runtime callback, native control validation and probes are implemented and experimentally qualified. [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786) delivers #1668; the production adapter and general bounded file/artifact plane remain separate work.

The first kernel build passed on a GitHub Linux runner in [run 37769914334](https://github.com/sokolaidev/maf-extensions/actions/runs/37769914334), attempt 1, from candidate `a92b193d6103a03047229d43d5e1a391132ea250`. The [build report](https://github.com/sokolaidev/maf-extensions/actions/runs/37769914334/attempts/1) records an 873,448-byte x86-64 ELF with SHA-256 `b25f0b8143f41a98fbb70a1cc0ee2fcab035b91b6f36b8e99f55b66b8f2d746d` and successful overlay removal. That first artifact was compilation evidence only and predates the dedicated host callback and older-devfs correction. It must not replace the kernel in an existing runtime profile.

#### Qualified byte-stream candidate

[GitHub run 37776716075](https://github.com/sokolaidev/maf-extensions/actions/runs/37776716075), attempt 1, qualified candidate `57f5c3aae00bab7970bdb5dd81fea7df82229561` independently on Linux/KVM and Windows/WHP. The [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/37776716075/attempts/1) and [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/37776716075/attempts/1) identify the helper, dependency lock, source-built kernel, builder identity and Python-state controls. Both hosts consumed kernel SHA-256 `9e638ba3d231adc8288ddda4c344019afd9d9fbd1d6987d28b26eed707a6fe19` and report successful overlay removal. The kernel profile differs from the earlier build-only artifact.

All 23 stream/failure cases matched across hosts: zero output; 4095/4096/4097-byte and one-MiB boundaries; all byte values; Unicode crossing a chunk boundary; writev and native C writes; descriptor duplication/redirection and reopened devices; concurrent stdout/stderr; text resembling control; continued execution after truncation and fresh-process recovery; guest error, timeout, owner loss and cancellation. A 16-MiB flood per stream retained exactly one MiB and counted the remaining 15 MiB. Reopened devices obeyed the same bounds. Rust unit tests independently covered malformed stream frames, sticky failure and counter saturation; Python tests covered strict control validation and pre-launch admission refusal.

The format-4 crash matrix exercised all eight publication/cleanup boundaries with both full byte prefixes and omission counts. Committed retries returned the identical envelope without a helper, including after checkpoint collection. Uncommitted failure preserved the previous checkpoint; quota refusal occurred before native launch; independently reconciled SQLite totals matched persisted accounting. This is same-machine process-crash evidence, not cross-machine fencing, machine power-loss evidence or a production adapter qualification.

After this native run, the only executable adjustment tightened host scratch admission by one KiB to reserve readiness/control headroom explicitly. Its focused boundary test passed; the native kernel, Rust overlays, helper and transport remained unchanged. The reports deliberately retain the actual native-tested commit rather than relabeling it as the final documentation commit. The [experiment README](#byte-stream-profile-reproduction) describes running and removing the profile.

### Upstream bounded capture qualification (2026-10-15)

[Upstream PR #146](https://github.com/hyperlight-dev/hyperlight-unikraft/pull/146) closed [upstream #140](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140) at commit `cf458a07ed847f06d78284998a5beb13dc9fb426`. [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) tracks downstream qualification. This comparison preserves the older experiments and their evidence; the upstream fix does not retroactively change their pinned runtime.

#### Source comparison

| Concern | Upstream at the pinned commit | Consequence for the MXC profile |
|---|---|---|
| Retained output | `output_limit(bytes)` bounds each capture between drains; overflow retains a UTF-8 prefix and continues execution | The original host-accumulation requirement is implemented, but the default remains unlimited |
| Mirroring | `echo_output(false)` is a separate setting | Both settings must be applied on fresh boot and restore |
| Stream identity | `take_output()` contains stdout and stderr together; `take_stderr()` duplicates stderr separately | A stderr flood can exhaust the combined budget before stdout is retained; these are not independent stdout/stderr budgets |
| Drain ordering | `take_output()` clears the stderr text and omission counter | Stderr must be collected first |
| Binary transport | `CapturedOutput.text` is a Rust `String`; callbacks accept strings | It cannot represent the existing arbitrary-byte stream contract |
| Omission counters | `Capture::push` uses ordinary `u64` addition without a saturation flag | Preserve the downstream saturation contract; this is a source finding, not a measured long-running overflow |
| Persistence | The upstream output API does not implement the MXC format-4 result/checkpoint transaction or bounded-export overlay | Session, storage and durable replay support cannot be retired on the strength of this fix |

The upstream snapshot contract is `c4` and its embedded x86-64 kernel has SHA-256 `2d5dc13d1dd13e31b37c98f0ab5413d90b5eed9104e6215e1e06ede559c9f275`. The retained MXC 1.0 runtime at `8f636e00cdf29e6c33ba0f578482c595d7827cf6` used contract `c3`. These source identities differ before attempting any old-checkpoint restore; a migration must keep old profiles available and explicitly qualify refusal/recovery. This comparison does not attribute all intervening kernel and contract changes to PR #146.

The [qualification harness](#upstream-unikraft-comparison) runs the exact upstream collector, output-limit, restore and echo tests alongside independent native contract comparisons. It leaves the existing MXC overlay sources and pins unchanged.

#### Native measurements and decision

The first [comparison run 38651934008](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008), source `a4d79f147fbf257f6032472fee18d4627948f1b1`, passed all nine upstream controls on both hosts but failed two of six independent comparisons. The unqualified [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008/attempts/1) reports remain retained. Binary output produced a host-call decoding error; the combined Unicode/reopened-device probe failed when opening stderr.

The diagnostic [run 38652752789](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789), source `9c61b9e8520dfa839596e07dcf29e293108125f4`, separated the reopened-device cases and captured the failure details. Both [Linux/KVM](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789/attempts/1) and [Windows/WHP](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789/attempts/1) completed five upstream collector tests, three upstream native limit/restore tests, one upstream echo test and seven independent comparison/diagnostic tests. The diagnostic tests assert the observed gaps; their successful completion means the behavior was measured, not that MXC adoption passed.

Both hosts used CPIO SHA-256 `8a1439a2085d218500f4ef5cfe42aecd63a233ac01f09db2ad95b9b3eaff52e9`, dependency-lock SHA-256 `f9611836e9490ac971e7e463fd771076005b03b1833d837917f371692c2dbde8`, and the same probe bytes. Their observations match exactly. The reports retain the observed base/rootfs image identities, kernel and executable hashes, Rust version, source commit, run and attempt. This is a standalone upstream Python rootfs, not the MXC rich-agent helper or a production adapter.

| Measured behavior | Linux/KVM and Windows/WHP result |
|---|---|
| Prefix limits, 4095/4096/4097-byte boundaries, four-MiB output, continuation and reset | Passed |
| UTF-8 prefix, reopened stdout, restore and echo controls | Passed |
| Combined drain ordering | Clears both stderr text and its omitted count, as documented upstream |
| Independent stdout budget | Not equivalent: 32 stderr bytes exhaust a 16-byte combined prefix; eight later stdout bytes are omitted, even though stderr is separately retained |
| All 256 byte values | Rejected with `ReadHostFunctionCall` / FlatBuffer function-call decoding error |
| Reopened stderr in this image | `OSError: [Errno 22] Invalid argument: '/dev/stderr'`, native `CallFailed { status: 1 }` |

**Decision: defer overlay removal.** The original configurable text-capture bound is implemented and its controls pass on both hosts. Upstream PR #146 does not replace the qualified independent byte-stream transport. A future adapter may reuse bounded text capture for diagnostics, but must explicitly disable echo, account for both buffers, respect drain ordering and preserve omission-counter semantics. Keep the current session, storage, stream and kernel profiles available.

No migrated MXC helper was qualified here. Durable result/checkpoint publication and replay, old-checkpoint refusal/recovery, and the complete timeout/cancellation/owner-loss matrix remain unverified on this upstream revision. [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) stays open for that adoption work beneath parent #1648. The earlier reports continue to describe only their original candidates.

### Adoption boundary assessment (2026-10-16)

[PR #1959](https://github.com/sokolaidev/maf-extensions/pull/1959) completes the remaining assessment for [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948) with a measured **defer overlay removal** decision. This supersedes the earlier open-work statement for that issue; integrated replacement remains tracked by [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957). The existing profiles, pins and overlays remain unchanged.

[Run 38816373753](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753) completed on Linux/KVM and Windows/WHP at harness commit `ada309adc98d3a8f953f303e89a94ad6c0f94e81`. The [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) assessments retain the native executable hashes, retained helper's source and patch graph, kernel/rootfs identities, checkpoint fingerprints and same-run upstream report hashes. Their observations and host publication results match. The [experiment README](#adoption-boundary-assessment) also retains the unsuccessful Windows fixture attempts; those remain unqualified.

| Boundary | Measured result on both hosts | Adoption consequence |
|---|---|---|
| Real retained checkpoint | The unchanged MXC 1.0 console-profile helper creates a rich Python-state checkpoint with key `k485adb2bab362516-c3`; upstream `k2d5dc13d1dd13e31-c4` refuses it, every checkpoint file remains unchanged, and the original helper recovers the saved state afterward | Preserve the old runtime; this is refusal/recovery, not conversion or migration of the stream/file profiles |
| Guest failure and completion-like text | A guest exception returns native `CallFailed { status: 1 }`; printed JSON remains data, but subsequent execution and snapshot capture are permitted | The session overlay still owns retirement after failure |
| Byte-prefix metadata | At a 1,048,576-byte limit, a UTF-8 boundary retains 1,048,575 bytes and reports three omitted bytes | The strict independent byte-stream result reader refuses this metadata |
| Host publication and replay | Forged completion text and the measured incompatible metadata are refused; previous checkpoint bytes survive, saved results replay identically after reopening the store, and interrupted call identities cannot execute again | Existing host checks preserve their boundary; this is not native publication qualification on the new runtime |

The host publication controls use **synthetic checkpoint bytes**, with metadata taken from the native measurement. Reopening the store happens in the same host process; it does not establish the native crash or lost-acknowledgment matrix. The real checkpoint experiment separately exercises the retained console profile and upstream refusal. Neither test installs a migrated MXC helper or demonstrates checkpoint conversion.

No session, bounded-console, storage, byte-stream or kernel overlay can be retired from this evidence. [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957) requires an equivalent integrated candidate, the complete native format-4 publication/crash/replay and timeout/cancellation/owner-loss matrices, and refusal/recovery or explicit conversion for every retained profile before removal. Production acceptance remains outside this qualification.

## Original Hyperlight feasibility probe

Tooling: [scripts/experiments](../../../scripts/experiments).

Initial Windows evidence for [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649), recorded on 2026-10-02. This is an experiment, not a backend implementation or a completed persistence qualification.

### Reproduction

Download the `mxc-release-binaries.zip` asset from [MXC v0.9.0](https://github.com/microsoft/mxc/releases/tag/v0.9.0), verify its SHA-256 below, and extract the platform executor and adjacent libraries. Run [the probe](../../../scripts/experiments/mxc_hyperlight_probe.py) with an absolute executor location and a dedicated state directory. `--setup` explicitly downloads and warms the agent image; subsequent `--run` executions reuse that prepared image. Omitting both options performs only help, availability and schema checks.

```text
python scripts/experiments/mxc_hyperlight_probe.py --executor <executor> --state-dir <isolated-directory> --setup --run
```

The probe supplies a small host environment, uses its own image home, and submits fixed programs without mounts or a network configuration. It records stdout, stderr, requests and process exit status separately. Exit zero from the probe means the recording completed; it is not a conformance verdict. The controls deliberately include rejected configuration and a guest exception. The timeouts bound these fixed experiments; this harness does not implement production process-tree supervision or output quotas. Keep the state directory outside Git: runtime files are large and diagnostics can include host locations.

### Artifact identity

| Artifact | Identity |
|---|---|
| MXC source | `86fb3d2abaf9c431556692037bff881830b543a5` (`v0.9.0`) |
| Release archive SHA-256 | `f233b59ade8639d5121615b6ab63d941ddc312b1f008c291a728cad37d84fe74` |
| Windows x64 executor SHA-256 | `819ecd9b08a8dcb2376d489c0352087468bf56800ab059f57d38901d7743049e` |
| Authenticode | Valid Microsoft Corporation signature at verification time |
| Guest rootfs | `ghcr.io/hyperlight-dev/hyperlight-unikraft/agent:initrd-v0.14.1` |
| Downloaded rootfs layer | `sha256:b7e71eeaaea60deb87456f62bb793ea1503329c130c9b7b8dc9693b998851c4b` |
| Extracted initrd SHA-256 | `8a9b9e383510dea8bb58b3aca72ac4a8d141f163f6a2fdc68af54197c5922e7e` |
| Guest Python | CPython 3.12.14 |

### Observed Windows controls

| Control | Result | Interpretation |
|---|---|---|
| `--help`, `--probe` | Exit 0; `hyperlightAvailable: true` | Executor starts and detects local Hyperlight availability |
| Closed request, experimental schema | Dry-run exit 0 | Request accepted; does not prove network confinement |
| Legacy `network.allowedHosts` | Dry-run exit 1; unknown field, expected `egress` or `ingress` | Legacy allowlist translation cannot be reached through this public request shape |
| Agent setup | Exit 0; downloaded rootfs and created warm snapshot | Runtime installation and initial guest boot succeeded |
| NumPy/pandas | Exit 0; `np.arange(4).sum()` = `6`; dataframe sum = `{'x': 3}` | Rich Python executes through the released Windows binary |
| stdout/stderr markers | Both `guest-stdout` and `guest-stderr` in process stdout; process stderr empty | Independent host pipes do not preserve guest stream identity |
| Controlled `ValueError` | Exit 1; traceback and driver diagnostic in stdout; stderr empty | Guest failure status is distinguishable in this case; guest text and diagnostics are mixed |
| Fresh seed/read in separate executor processes | Seed prints `73`; reader prints `absent` | Fresh-execution control passes; this does not test a reused guest |

### Persistence investigation

The pinned [MXC runner](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) restores `rewind` before every execution after the first. Its guest and rewind fields are private. The CLI exposes no session checkpoint operation; the inspected SDK/FFI entry points do not provide a Hyperlight modified-state capture/restore contract. A persistent helper around the unchanged MXC runner would therefore still lose state.

The lower-level [hyperlight-unikraft 0.14.1 API](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/v0.14.1/src/lib.rs) exposes `AppSandbox.run`, `snapshot_to`, `restore_from` and `SandboxBuilder.from_snapshot_dir`. Its documentation describes capturing the application heap, parked threads and scheduler state at the current boundary. The [Python driver](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/v0.14.1/drivers/hl_py.h) executes source in the existing `__main__` dictionary. These are source-level reasons to try a persistent native helper; they are not measured proof of durable Python recovery.

The native experiment below now exercises this path. A later experiment must coordinate session files and host-controlled atomic commitment; the library's disk snapshot alone does not establish that application contract.

A helper that calls Unikraft directly would bypass MXC's current runner. Treat it as an experimental feasibility probe and identify the MXC API change or alternative integration needed before proposing it as the production adapter. The extension is proposed in [MXC #1374](https://github.com/microsoft/mxc/issues/1374); no upstream PR has been published.

### Native state and restart experiment

The [native helper](../../../scripts/experiments/mxc_native_state/src/main.rs) uses exactly `hyperlight-unikraft` 0.14.1, with Hyperlight host/common 0.17.0 recorded in [Cargo.lock](../../../scripts/experiments/mxc_native_state/Cargo.lock). The [supervisor](../../../scripts/experiments/mxc_native_state_probe.py) verifies the recorded rootfs hash, supplies a minimal environment, and requires a new evidence directory. No host mounts or guest network policy are configured. This exercises Unikraft directly, bypassing MXC's runner.

```text
cargo build --locked --manifest-path scripts/experiments/mxc_native_state/Cargo.toml
python scripts/experiments/mxc_native_state_probe.py --helper <built-helper> --initrd <verified-agent-initrd> --state-dir <new-evidence-directory>
```

On Windows, this experiment used Rust 1.98.0 targeting `x86_64-pc-windows-gnu` and portable MinGW GCC 16.2.0 (POSIX/SEH/MSVCRT). Rust and compiler tools were isolated from the user's normal environment. The GCC archive was `x86_64-16.2.0-release-posix-seh-msvcrt-rt_v14-rev1.7z`, SHA-256 `a3cfb25037981ea1cd2f4e5452fa45c700b20fffc311e5d78de90dedd142b73a`. Set that compiler's `bin` directory on the build process's PATH; ordinary Windows MSVC and Linux builds have not been qualified here. The compiler version is pinned in [rust-toolchain.toml](../../../scripts/experiments/mxc_native_state/rust-toolchain.toml).

| Native control | Measured Windows result |
|---|---|
| Multiple executions on one guest | Variable, dataframe update and NumPy array retained |
| Additional interpreter state | Lambda and open guest file retained, including its seek position |
| Capture boundary | State captured before changing the variable/dataframe to `999`; restored values match the checkpoint, not the later mutations |
| Abrupt process termination | Supervisor killed only its owned helper after the snapshot and atomic readiness report completed |
| Separate-process restore | New helper restored the on-disk snapshot and passed the same Python assertions |
| Saved checkpoint stability | All recorded checkpoint-file hashes remained unchanged after restoration |
| Truncated index | Exit 1, explicit JSON parsing error, no success report |
| Missing snapshot blobs | Exit 1, explicit missing-blob error, no success report |
| Altered compatibility key | Exit 1, explicit kernel/host-contract mismatch, no success report |

The initial recovery execution passed; a second complete execution also passed and included all three refusal controls. The helpers were distinct processes. The helper executable SHA-256 was `77db7ece296c0ce7a1bea4834afb243773e78bdd9c280a3943e72541fd78f751`; the snapshot compatibility key was `k5e9192dfed5c8dbb-c1`. The second snapshot's main memory blob occupied 915,845,120 bytes. This is one observed footprint for this workload, not a capacity estimate or performance benchmark.

`cargo build --locked`, `cargo fmt --check`, `cargo clippy --locked -- -D warnings`, supervisor lint/format/type checks, real helper `--help`, and a safe invalid-argument control were used for validation. Generated reports, snapshots and diagnostic files remain outside Git; this prose preserves the historical result and executable identity.

#### Integration consequence

Rich Python continuity and modified-state restoration are feasible on the tested Windows host through the underlying library. They are not supported persistent-session behavior of the unchanged MXC runner. The maintainer selected extending MXC's Hyperlight session API on 2026-10-02. The production adapter will use that MXC surface; the lower-level helper remains an experiment. The owning design in [PR #1650](https://github.com/sokolaidev/maf-extensions/pull/1650) records the proposed responsibility boundary and checkpoint barrier. No production session API has been implemented.

### Removable MXC session patch

The [temporary patch bundle](#mxc-09-session-overlay-reproduction) now exercises persistence through an opt-in module in MXC's own Hyperlight common crate. It preserves the existing runner, isolates the temporary API behind one wrapper, and includes pinned apply/remove tooling. Windows/WHP recovery, commit barriers, stale-token refusal, failure retirement and the original fresh-state control passed through this patched MXC path. The bundle records exact evidence, reproduction and replacement steps; it remains an experiment, not a production adapter or upstream-supported API.

### Remaining evidence

Lost acknowledgments, atomic checkpoint commitment after every successful tool call, host file/artifact consistency, compatible second-machine recovery, fencing, resource limits, general owner-death cleanup and network enforcement remain unrun. Linux/KVM, host reboot and power-loss durability have not been tested. The open-file case covers an in-guest file, not a host mount or arbitrary sockets/threads. Refusal controls cover malformed metadata, missing blobs and a mismatched compatibility key; payload corruption and adversarial snapshot inputs need separate qualification. Guest stdout/stderr separation still needs a supported native output channel or upstream change before promising the existing result contract.
### Filed upstream feature request

Proposed title: **Hyperlight: opt-in persistent sessions with checkpoint export and restore**

The following proposal was filed as [MXC #1374](https://github.com/microsoft/mxc/issues/1374), with an offer to contribute a PR after API agreement. This retained proposal describes the evidence available when the issue was filed; the temporary patch above is subsequent work. The pinned [contributor guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/CONTRIBUTING.md#before-you-start-file-an-issue) asks for an issue before implementation and a written design for larger changes. Searches of open and closed MXC issues for Hyperlight snapshot/persistence did not identify an equivalent proposal on 2026-10-02. The runner at current main `298bb3909a5130cc6caed575494290fbcacf0db5` still restores its rewind baseline before subsequent executions.

#### Description of the new feature / enhancement

Add an opt-in experimental Hyperlight session API that preserves interpreter state across executions and can export/restore a checkpoint of modified state. Keep existing one-shot requests fresh by default.

Agent code-execution hosts need notebook-style Python continuity and recovery after a host process restarts. MXC's current Hyperlight runner reuses a guest but restores its baseline between executions, so retaining a runner does not retain Python variables. Its prepared startup snapshot is a different operation from capturing modified session state.

We have a reproducible Windows/WHP feasibility probe using the agent rootfs from MXC v0.9.0 and pinned hyperlight-unikraft 0.14.1. A variable, pandas dataframe, NumPy array, lambda and open in-guest file survived multiple executions and disk checkpoint restoration in a new process after the original helper was killed. Changes after capture did not appear in recovered state. Malformed metadata, missing blobs and a changed compatibility key were refused. [Probe, dependency pins and evidence](https://github.com/sokolaidev/maf-extensions/blob/2797c74f8d0503d22e9b3a76c1457aa5723a05e1/scripts/experiments/mxc_hyperlight_probe.md#native-state-and-restart-experiment).

These results use the underlying library directly; they do not establish a supported MXC session API, Linux qualification, host reboot/power-loss durability, remote recovery or arbitrary thread/socket recovery. We intend to contribute an implementation after agreeing the API boundary and test plan.

#### Proposed technical implementation details

Start with an explicit Hyperlight native session abstraction. Bind the runtime/artifact identity and admitted policy at creation; reject incompatible changes instead of replacing the session with empty state. Preserve the existing one-shot runner's behavior and timeout handling.

Expose the following semantics, with method names and SDK placement to agree:

- Create or restore a session with an explicit runtime and currently admitted policy.
- Execute without baseline rewind, reporting execution completion separately from durable application success.
- Park the session and export an immutable checkpoint candidate with its runtime/artifact and compatibility metadata.
- Keep execution blocked while the candidate is uncommitted; let the trusted host confirm publication for that candidate/generation before resuming. The confirmation is the host's assertion about its store, not MXC independently certifying storage durability.
- Close or explicitly recover after execution, timeout, cancellation or capture failure; never silently reset a persistent session or replay its program.

MXC would own native session lifetime, interruption, checkpoint capture and compatible restore. Applications would own their durable store, conversation identity, atomic checkpoint/result publication, idempotent result delivery and distributed fencing. No cloud storage dependency is needed in MXC. Host mounts and external side effects require separate consistency rules; a VM snapshot alone cannot roll them back.

The first increment can qualify these native primitives without mounts and with networking closed. Then expose them through MXC's experimental engine/SDK contract and supervised out-of-process transport. Any JSON additions should use permanent locations in the mutable development schema, keep runtime experimental authorization, and include matching TypeScript SDK/contract tests. Existing stable schemas and one-shot requests must remain compatible. Do not assume existing generic provision/exec lifecycle operations already provide Hyperlight session semantics.

Acceptance should include a fresh-state control, same-session variables/dataframes, restore after helper death, immutable capture boundaries, rejection of malformed/incomplete/incompatible checkpoints, failed capture/commit barriers, policy-change refusal, cancellation and disposal on Windows/WHP and Linux/KVM. An initial backend-level patch should exercise these paths before changing the wider SDK surface.

Output fidelity needs a related transport work item: with the released executor, guest stdout and stderr markers both arrive on process stdout. Control messages must remain independent of guest text, and stream separation needs its own demonstrated implementation. Conditional allowlisting remains separately qualified; this proposal does not enable networking.

## Original feasibility conclusion

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

Recommendation: **go for experimental integration using the removable MXC patch; no-go for advertising the selected production backend contract yet**. The feasibility spike establishes modified-interpreter capture and recovery for fixed workloads. It does not narrow the selected file, persistence, networking or compatible-machine recovery requirements.

MXC v0.9.0 at `86fb3d2abaf9c431556692037bff881830b543a5` provides fresh execution through its public runner. Persistent-session operations in this experiment require the feature-gated downstream patch; they are not a supported released MXC API. [Upstream #1374](https://github.com/microsoft/mxc/issues/1374) contains the proposal and contribution offer. The wrapper keeps replacement with an upstream implementation localized; no upstream PR has been published.

| Area | Evidence and decision |
|---|---|
| Rich Python and continuity | Fixed NumPy/pandas state, lambda and guest file/seek position passed on Windows/WHP and Linux/KVM |
| Capture and process restart | Separate native process restores committed interpreter state; post-capture mutations remain absent |
| Durable completion | Local checkpoint/result transaction and retries passed; corruption regressions cover reused chunks; this is not power-loss or host-reboot qualification |
| Files and artifacts | Fixed CSV-to-chart and identical saved-artifact redelivery passed; general file confinement remains unimplemented |
| Execution transport | Guest streams merge and a long write was truncated; no-go for the suite's faithful bounded-output contract until corrected |
| Compatible-machine recovery | Unrun; local OS ownership is not distributed fencing, and cross-hypervisor checkpoint compatibility is not implied |
| Networking | No network authority is configured in the fixed probes; closed and allowlisted enforcement require independent bypass tests |

See [native controls](#mxc-09-session-overlay-reproduction), [publication evidence](#checkpoint-publication-and-recovery-experiments) and [owner-liveness continuation](#owner-liveness-experiment) for exact source identities, runtime results and limits. Historical reports remain tied to their measured revisions.

Independent implementation follows under [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648): bounded native transport [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668), owner-death cleanup [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669), safe files [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670), compatible-machine recovery/fencing [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671), storage retention [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672), and egress qualification [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673). Router/CodeAct integration remains under the parent issue, gated on those contracts rather than the existence of a runtime name.

## MXC 0.9 session overlay reproduction

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

Experimental work for [maf-extensions #1649](https://github.com/sokolaidev/maf-extensions/issues/1649) and the proposed upstream API in [microsoft/mxc #1374](https://github.com/microsoft/mxc/issues/1374). This bundle enables a native persistence probe; it is not a shipped backend, supported MXC API or durable conversation store.

### Removal boundary

[session-preview.patch](../../../scripts/experiments/mxc_session_patch/session-preview.patch) changes exactly three files at MXC v0.9.0 commit `86fb3d2abaf9c431556692037bff881830b543a5`: it adds the disabled-by-default `maf-session-preview` Cargo feature, a gated module declaration, and one new module. It does not change the existing one-shot runner, public JSON schemas, SDKs or dependencies. [patch.json](../../../scripts/experiments/mxc_session_patch/patch.json) binds the base revision and patch checksum.

Only [probe/backend.rs](../../../scripts/experiments/mxc_session_patch/probe/backend.rs) imports the temporary session API. Its session, checkpoint and error types belong to the wrapper, so the test program does not depend on upstream type names. The feature and dependency selection live in [Cargo.toml.template](../../../scripts/experiments/mxc_session_patch/Cargo.toml.template). No shipped maf-sandbox package imports this experiment.

### Session behavior

Restore takes an explicitly selected, trusted snapshot with no mounts or networking configured. Execution preserves Python state. Each successful execution blocks subsequent execution until capture produces a candidate and the host confirms publication of that same candidate on that same live session. Stale and foreign tokens cannot release the barrier. Guest errors, timeouts and capture failures retire the guest; recovery requires an explicit restore. Closing a session leaves caller-owned checkpoint files intact.

Confirmation is the host's assertion about its store. The probe confirms immediately after snapshot export and does not implement durable publication. A production host must atomically publish the checkpoint and result, then confirm, then acknowledge the successful tool call. It must own result redelivery, fencing, identity and compatibility checks. The snapshot directory is caller-owned and is not made immutable by this patch; the host must protect published checkpoints. Failed exports may leave incomplete directories for host cleanup.

### Apply and reproduce

Use a dedicated, clean MXC checkout at the exact base above. The tool checks revision, checksum and forward/reverse applicability; it refuses drift rather than attempting a three-way merge. The build directory must be new and outside this repository. It receives generated local paths and a copy of the retained [Cargo.lock](../../../scripts/experiments/mxc_session_patch/Cargo.lock), which pins `hyperlight-unikraft` 0.14.1 and Hyperlight host/common 0.17.0.

```text
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py apply --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py configure --source <mxc-checkout> --build-dir <new-build-directory>
cargo build --locked --manifest-path <new-build-directory>/Cargo.toml
```

Prepare the verified MXC agent image and startup snapshot using [the original experiment](#reproduction). Supply that image home explicitly so the original runner's fresh-state control uses the same prepared runtime. The patched session restores the `snapshot` directory adjacent to the supplied initrd. The supervisor checks the recorded initrd hash; this is not independent verification of the startup snapshot's contents.

```text
python scripts/experiments/mxc_native_state_probe.py --helper <built-mxc-session-state-probe> --initrd <agent-initrd> --image-home <prepared-image-home> --state-dir <new-evidence-directory>
cargo test --locked --manifest-path <mxc-checkout>/src/Cargo.toml -p hyperlight_common --features maf-session-preview -- --test-threads=1
```

The Windows measurement used Rust 1.98.0 with `x86_64-pc-windows-gnu` and the portable GCC toolchain identified in [the native experiment](#native-state-and-restart-experiment). Select that toolchain explicitly to reproduce it. The later [host publication continuation](#linux-evidence) qualifies the fixed native and publication probes on Linux/KVM; MSVC remains unqualified. Checkpoint and diagnostic files stay outside Git; diagnostic output may contain host paths. The fixed-program supervisor is not production process-tree supervision or a bounded output transport.

### Measured evidence

The source patch applies and reverses on a pristine checkout at the pinned base, leaving its Git status clean; a different base is refused. The patched common crate's unit suite passed **22 tests**, including the two added state/token tests. Locked builds, Rust formatting and probe and patched-crate Clippy with warnings denied passed, as did Python lint/format/type checks, the generated-manifest check, real helper help and invalid-argument controls. Two complete Windows probe executions passed; this summary describes the final executable.

| Live control | Result |
|---|---|
| Original MXC one-shot runner | Second execution cannot see the first execution's variable |
| Persistent Python | Integer, pandas update, NumPy array, lambda and open guest file/seek position survive |
| Commit barrier | Execution refused both before capture and before confirmation |
| Candidate identity | Earlier checkpoint token cannot confirm the next candidate |
| Helper death and restore | Supervisor kills its owned helper; a different process restores the saved state |
| Capture boundary | Post-capture mutations do not appear on restore; checkpoint file hashes remain unchanged |
| Existing capture destination | Capture fails, preserves the saved checkpoint and retires the session |
| Guest exception and timeout | Both retire the session and refuse further execution |
| Explicit close | Further execution refused |
| Truncated metadata, missing blobs, wrong compatibility key | Each refused without a success report |

These Windows controls establish native behavior on one tested host. They do not qualify power-loss durability, actual host reboot, another machine, Linux, mounted files, network enforcement, arbitrary threads/sockets, adversarial snapshot inputs, cancellation, resource quotas or stdout/stderr fidelity. Capturing after successful executions is exercised; atomic host-store commitment and acknowledgment recovery were outside this initial native measurement and are exercised by the continuation below. The pre-PR repository gate passed: 12,958 tests passed and 749 skipped, with lint, format, type and documentation checks also passing. Markdown-block and authenticated tracker checks passed separately. Hosted CI is a separate result.

### Host publication continuation

The [local publication experiment](#checkpoint-publication-and-recovery-experiments) adds checkpoint/result transactions, interrupted-call refusal, saved-result redelivery, and a fixed CSV-to-chart workflow. Its independent Windows/WHP and hosted Linux/KVM evidence extends these measurements. General transport, file and recovery limits remain documented there.

### Output transport continuation

The [descriptor experiment](#console-and-bounded-capture-experiments) compares separate guest pipes and files with the existing console. It retains a minimal truncation reproducer and distinguishes cooperative byte fidelity from host-enforced output bounds. The [bounded capture overlay](#bounded-native-capture-overlay) adds a separately removable Rust-only collector with the accepted truncate-and-continue policy. The separate [bounded storage overlay](#bounded-checkpoint-export-overlay) adds checkpoint write limits above it. Remove storage first when installed, then output, then the original session patch.

### Remove or replace

Stop sessions using the patched binary before switching providers. Removing source edits does not revoke an already-built binary or migrate saved state.

```text
python scripts/experiments/mxc_session_patch/patch.py remove --source <mxc-checkout>
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
```

Removal reverses only this patch and refuses edited or mismatched hunks; it never forces a reset or deletes checkpoint files. Keep the pinned runtime available for outstanding sessions until their recovery or migration is resolved.

When upstream provides an equivalent supported API:

1. Replace the implementation of `probe/backend.rs` and the manifest's dependency/feature selection with the released API. Preserve the wrapper's commit barrier even if upstream locates publication acknowledgment elsewhere.
2. Run the same native controls against the new provider on Windows/WHP and Linux/KVM. Qualify checkpoint compatibility explicitly; migrate through a tested path or keep old sessions on the pinned runtime until they finish. Never replace an incompatible session with fresh state silently.
3. Reverse the source patch, rebuild without `maf-session-preview`, and remove `session-preview.patch`, `patch.json` and the patch application tool. Retain the behavior probes and historical evidence. A production adapter should depend on the replacement wrapper, never on `session_preview` directly.

The upstream issue contains our contribution offer. This downstream patch has not been submitted as an upstream PR.

## Checkpoint publication and recovery experiments

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

This continuation of [spike #1649](https://github.com/sokolaidev/maf-extensions/issues/1649) exercises the host side of the [removable native patch](#mxc-09-session-overlay-reproduction). The runtime now lives in the unreleased [maf-sandbox-mxc package](../../../packages/maf-sandbox-mxc/README.md); qualification probes remain repository tooling. Packaging does not complete the adapter or replace the selected first-version requirements in [the owning design](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/mxc-backend.md#accepted-design).

The [storage and retention acceptance record](#storage-and-retention-acceptance) maps the current shared-store scope and remaining limits. The sections below retain the earlier experiments and their version-specific evidence.

### Publication and recovery

[host_store.py](../../../scripts/experiments/mxc_session_patch/host_store.py) stores compressed, hashed snapshot chunks and a bounded result in one local SQLite database. SQLite uses rollback journaling and `synchronous=FULL`; committing the transaction publishes both the current checkpoint reference and the saved result. Snapshot bytes are inside that transaction, so there is no separately renamed directory whose durability the database assumes. Each referenced chunk is stored from the freshly captured bytes; a differing stored payload under the same hash is replaced inside that transaction. Result JSON includes the fixed experiment's chart artifact bytes.

A local operating-system file lock covers the entire owner lifetime. Acquisition increments a generation; stale-generation operations refuse. This is single-machine ownership on a private local filesystem, not a distributed lease or permission to recover on another machine. The profile binds the host-supplied session identity, helper hash, startup-index hash, platform, machine and closed/no-mount policy. An incompatible profile refuses; startup-index identity is not independent verification of every runtime blob or production policy admission.

Before native execution, the store records a pending call with its source hash. The native helper restores the latest verified committed state, executes once, exports a checkpoint and closes. It cannot execute another program from the uncommitted candidate. The host publishes checkpoint plus result, then acknowledges. This experiment deliberately restores a guest for each call; it does not implement warm resident-session confirmation or performance optimizations.

| Interruption boundary | Recovery behavior |
|---|---|
| Before the checkpoint/result transaction commits | Previous committed state remains current; interrupted call identity cannot replay automatically |
| After commit, before acknowledgment | Same call identity and source receive the saved result without starting the helper |
| A previously committed call retried after later calls | Original saved result is returned; the latest checkpoint remains current |
| Same call identity with different source | Refused |
| Missing metadata, changed profile, corrupt result or snapshot | Refused; no fallback to fresh state |

The operating system releases the lock when the host process dies. The native helper has no database access. The [owner-pipe continuation](#owner-liveness-experiment) binds `call-owned` execution to host liveness; the publication fault stops described here occur after the helper has closed. Network access and host mounts are absent, so this test does not address externally visible guest side effects or distributed fencing.

### Interrupted first initialization

[Store](../../../scripts/experiments/mxc_session_patch/host_store.py) and [SharedStore](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/shared_store.py) inspect SQLite's schema while holding their initialization lock and a write transaction. A missing database, a zero-byte database, or a valid rollback-format SQLite database with no schema objects may initialize. Schema creation and the first metadata insert commit together. Process death before that commit leaves an empty schema that a new process can initialize; after commit, startup validates the stored profile, configuration and schema before acquiring a new generation. Shared-store idle-policy activation and owner acquisition remain a second atomic transaction; interruption there preserves the first transaction and resumes with the same profile.

Local stores and the coordinator refuse retained WAL or unknown SQLite journal formats before opening SQLite, preserving the database and its sidecars rather than converting them. The preflight reads SQLite's persisted [read/write format versions](https://www.sqlite.org/fileformat.html#file_format_version_numbers) under the initialization lock. Normal SQLite rollback-journal recovery remains necessary after a supported store crashes; it can change physical files before application schema inspection and is distinct from application-level repair.

A nonempty foreign or partial schema is not an interrupted empty store. Every non-internal schema object must match the format-specific allowlist, including accounting triggers, required indexes and optional idle/physical tables. Extra objects, missing required definitions, changed definitions, missing committed metadata, incompatible profiles and unsupported formats refuse before startup writes, without recreating the store. The call-expiry and uncertainty lookup indexes retain their older-store lazy installation; if already present, their definitions must match. This also refuses partial schemas left by the legacy non-atomic initializer; they need explicit investigation rather than guessed repair. Existing complete legacy schemas remain readable, and formats 4 through 7 retain their version-specific checks and explicit format-3 accounting upgrade. An empty shared database cannot bypass the original store's migration requirement.

The trusted private-root contract excludes external replacement or truncation of a committed database: an erased file is indistinguishable from an uninitialized empty file without independent durable evidence. This change does not establish protection against that loss, power loss, native guest recovery or remote takeover. [Issue #1866](https://github.com/sokolaidev/maf-extensions/issues/1866) covers ordinary-process startup recovery only.

[Startup controls](../../../tests/test_mxc_local_initialization.py) terminate actual processes after connection, before schema creation, between table creations, around session metadata insertion, and before/after initialization and owner commits. They then verify checkpoint publication, exact result retry, generation continuity and preserved configuration. Additional controls cover empty files/databases, legacy schema compatibility, competing initializers, unchanged damaged/foreign databases and migration refusal. The [storage acceptance workflow](../../../.github/workflows/mxc-physical.yml) runs these controls on GitHub Linux and Windows; its JUnit and runtime artifacts identify the tested source and Python/SQLite versions separately from earlier native evidence.

### Native files and output findings

The fixed guest program writes a supplied CSV to `/tmp/input.csv`, reads it with pandas, writes `/tmp/chart.png` with matplotlib, and exports the PNG as guest-authored data. The host checks chunk order, byte limits, PNG signature and the final hash before including the chart in the transaction. Retried calls return the same saved artifact bytes and result hash. This exercises guest file persistence and artifact publication; it is not qualification of the general FILES_IN/FILES_OUT API, arbitrary paths, host mounts or concurrent writers.

A first chart transfer exposed a reproducible output limit: a single long base64 write arrived as only 4,096 bytes, without a complete artifact. The experiment now sends chunks smaller than that boundary and refuses incomplete transfers. Guest stdout and stderr still share the native output path. [host_call.py](../../../scripts/experiments/mxc_session_patch/host_call.py) therefore names the captured field `combined_output`; it does not promise the suite's separate-stream result contract. Guest chart records are untrusted data and never supply execution or checkpoint status. Those statuses come from process exit and a separate native-written control report.

The Python supervisor bounds each process pipe and enforces a wall deadline. This does not establish native buffer limits, byte-exact arbitrary guest output, process-tree cleanup or a supported production control transport. These remain blockers for advertising the adapter's execution contract.

### Reproduce

Apply the pinned source patch and generate a new build directory as described in [the patch guide](#apply-and-reproduce). The helper now also supports `call <restore> <checkpoint> <code-file> <report>`. Build from this continuation's manifest template so it includes that mode.

```text
cargo build --locked --manifest-path <generated-build>/Cargo.toml
python scripts/experiments/mxc_session_patch/host_probe.py --helper <built-helper> --startup <prepared-agent-snapshot> --state-dir <new-evidence-directory>
uv run pytest -q tests/test_mxc_host_store.py
```

The live probe creates a new private store, kills only its own host-call processes at named publication boundaries, and restarts with identical call identities. The fixed programs assert Python counter values so replay would fail. It verifies that result redelivery creates no native checkpoint candidate and that the saved chart remains byte-identical. Keep database, snapshots, diagnostics and generated programs outside Git: they contain session state and may contain host paths.

### Hosted Linux runner

The legacy `mxc_recovery` dispatcher is retired. Its native recovery, publication and owner-loss reports remain historical evidence for the pinned pre-1.0 candidate. The [current workflow guide](#qualification-workflows-and-evidence-retention) identifies the maintained native and process controls; [the original workflow](https://github.com/sokolaidev/maf-extensions/blob/1c2a2b2b/.github/workflows/tests.yml) retains the historical dispatch definition.

### Windows evidence

All real host-process crash cases passed: before commit, after commit and before acknowledgment. The latest committed Python counter survived each boundary, committed retries did not execute the helper, and the original PNG result remained identical when retried after later calls. The initial 4,096-byte output truncation was refused rather than committed as an artifact.

Fifteen focused store/protocol tests passed, including abrupt process exits inside a SQLite transaction, stale ownership, changed profiles, result/chunk corruption, missing session metadata, interrupted-call refusal, checkpoint limits, hard links, and dropped/duplicate chart chunks. Native locked build, formatting and strict Clippy passed. These are process-crash and fixed-workload results, not power-loss or production backend acceptance.

### Linux evidence

The [hosted result](https://github.com/sokolaidev/maf-extensions/actions/runs/37071950296) records a successful [GitHub Actions run](https://github.com/sokolaidev/maf-extensions/actions/runs/37071950296) on Ubuntu 24.04 x86-64/KVM, kernel `6.17.0-1022-azure`, host Python 3.12.3 and Rust 1.98.0. Both native recovery and host publication passed at source commit `eca6246c85eff10e2814042ca8864649898673a7`. The run produced reports with exact helper, patch, rootfs, checkpoint, result and source hashes. Workflow artifacts have a 14-day retention period; only this prose summary remains in Git.

Native controls established fresh-state isolation, persistent Python objects and open guest-file position, commit barriers, recovery in a different helper process, unchanged saved checkpoint bytes, session retirement after errors/timeouts, and refusal of truncated, missing or incompatible checkpoint metadata. Publication controls established previous-state recovery after a pre-commit host crash, interrupted-call refusal, saved-result redelivery after commit and before acknowledgment, and byte-identical chart redelivery after later calls. These independently qualify the fixed workload on Linux/KVM; they do not establish Windows-to-Linux checkpoint portability.

The first hosted run seeded and captured successfully but restore reported no available hypervisor. Replacing a one-time KVM ACL with persistent runner ownership through udev allowed the unchanged probes to pass. An earlier local Linux attempt encountered executable-format and filesystem I/O failures; it does not qualify Linux.

The [merged-main result](https://github.com/sokolaidev/maf-extensions/actions/runs/37084553838) records a successful [hosted rerun](https://github.com/sokolaidev/maf-extensions/actions/runs/37084553838) at `37187e9a92549c160e982584a2afe8c5f756786e`, including the chunk-replacement correction. It predates the owner-pipe continuation. The earlier Windows and Linux records remain tied to their original revisions. Four store regressions cover invalid compressed bytes and valid compressed data with the wrong hash in historical chunks, successful restoration after replacement, and rollback of chunk changes before publication. These corruption controls complement the merged-main runtime measurement.

### Limits and next gate

SQLite FULL synchronization expresses a storage requirement; process-crash tests do not prove actual power-loss or device-cache behavior. Use only a private local filesystem for this experiment. Network filesystems, remote recovery, distributed fencing, encryption, retention/garbage collection, arbitrary snapshot input and production filesystem race resistance remain outside its contract. Checkpoint/result size limits exist; total historical storage is not yet bounded.

A production `maf-sandbox-mxc` adapter remains gated on bounded faithful output, independently framed native control, owner-death cleanup, safe file input/collection, and adapter-level conformance on both Windows/WHP and Linux/KVM. The fixed native and host-publication probes now pass independently on both platforms. Closed networking remains the baseline. Conditional allowlisting needs its own enforcement evidence. The host store and behavior tests do not import the temporary MXC API; its eventual upstream replacement stays confined to the existing Rust wrapper and manifest selection.

### Shared-store accounting increment

[shared_store.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/shared_store.py) is a separate, importable experimental store core for [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672). The existing native supervisor still uses the original `Store`; it does not opt into this new format. The new `SharedStore` owns one named session in a shared local database, requires explicit session/store quotas through `Limits`, and publishes checkpoint bytes, serialized result, call identity and logical reservation conversion in one transaction. File inventories and per-session identities are charged even when chunk bytes are deduplicated. Checkpoint/result limits can be lowered from the existing experiment's maxima.

The shared-store root is created with owner-only access and verified before opening retained data. Linux requires the current effective owner and no group/other mode permissions; those mode bits also bound effective POSIX ACL access. Windows checks the current owner and a supported DACL granting only that owner (including Owner Rights), SYSTEM and Administrators. Existing permissive or unverifiable access is refused without changing permissions. Database, SQLite sidecar and lock files must be single-link regular files; Windows file ACLs are checked independently of the parent. Windows creation relies on Python 3.12.4 or newer for `mkdir(0o700)`; earlier versions must supply an already private root or are refused by verification. These checks retain the private-tree/no-external-writers assumption and do not qualify filesystem race resistance.

`begin` either reserves maximum publication capacity or returns the saved result; a full quota does not prevent replay. `commit` preserves the previous checkpoint and the reservation on failure. Recovery advances only the owned session's generation, marks its pending calls interrupted and keeps their reservations charged. The optional journaled supervisor releases interrupted reservations only after establishing recorded-helper termination and private scratch removal; an unidentified launch remains charged. `retire` durably blocks new execution and publication while keeping completed delivery available. It records retirement intent and does not itself terminate native execution; outstanding reservations continue to block reclamation.

Result expiry has the selected 24-hour default and five-minute grace default, persisted per call and independent of checkpoint recovery. An injectable clock supports deterministic clock-change controls. Grace is deducted in transactions before use, in grants of at most one second, with a fixed outer deadline. A failed transaction restores the in-memory grants that preceded it, while discarding grants it failed to commit. Reopening the owner forfeits any unfinished grant without replenishing its budget. Expiry releases only the serialized result's logical charge; the call identity and checkpoint remain. The `expire` operation examines a bounded batch of due records and does not load their result payloads. Artifacts embedded in a result share that result's lifetime; separately stored artifacts remain unimplemented.

`collect_checkpoints` removes a bounded number of superseded checkpoints, or any checkpoint belonging to the retired session once that session has no outstanding reservations. It preserves call identity and result bytes, and deletes a chunk only when no checkpoint in any session references it. Checkpoint inventories, reference removal, logical-charge release and chunk deletion share one transaction. A process death before commit rolls the entire collection batch back; a repeated successful collection does not release capacity twice.

The checkpoint format includes a digest covering the ordered file inventory and chunk references. Restore verifies that digest and the charged size as well as each file's bytes. Before destructive collection, the store validates all retained checkpoint inventories and content across sessions, including current-checkpoint references and foreign keys. Corrupt or unowned content refuses collection rather than being treated as garbage. This integrity pass streams file bytes but scans the whole retained checkpoint store under a write transaction: only the number of removals is bounded per call, not validation work or latency. Large-store performance remains unmeasured. Admission reads indexed, transactionally maintained logical totals; accounting audits still scan retained history. The accounting benchmark below measures those paths separately.

The accounting format charges a fixed 4,096 bytes for store metadata, 20,480 bytes per session, 2,048 bytes per call, 768 bytes per checkpoint file and 80 bytes per chunk reference, in addition to uncompressed payload bytes. These are versioned logical charges rather than measurements of SQLite pages. Session profiles are limited to 16,384 encoded bytes and checkpoint paths to 512 encoded bytes. Publication reserves inventory space conservatively before content is known. Quota values must agree with the existing store configuration; changing persisted quotas requires a future explicit configuration operation. Retention/grace changes affect only subsequent publications.

Focused tests cover concurrent processes competing for shared capacity, session separation, failed publication, real process death before and after checkpoint/result commit and grace commit, exact expiry boundaries, changed retention configuration, clock rollback, corrupted content and preserved checkpoint recovery after result expiry. Collection/retirement controls add shared chunks across sessions, preserved result delivery after checkpoint removal, capacity reuse, idempotent batches, retirement with an outstanding reservation, and real process death around collection and retirement commits. These are local store tests, not new Windows/WHP or Linux/KVM native qualification. The previous native reports apply only to the original supervisor/store path.

Windows/WHP export-bound qualification, physical disk safeguards, unidentified-launch reconciliation, native termination during retirement and idle expiry remain pending. The original native store and shared-store formats 1 and 2 refuse without migration; the implementation neither invents missing timestamps nor assumes a manifest for old records. SQLite result deletion permits page reuse and does not establish physical disk reclamation. This increment does not close #1672 or enable production persistence.


#### Transactional charge totals

[accounting.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/accounting.py) maintains one total per session and one for the shared store. SQLite triggers change those totals with session metadata, permanent call identities, retained checkpoint/result charges and logical reservations. Admission reads the two indexed totals inside the same `BEGIN IMMEDIATE` transaction that admits the call. Publication removes the reservation before adding retained charges within that transaction, avoiding transient integer overflow. Failed transactions roll back both representations. Retirement keeps charges; expiry, checkpoint collection and verified scratch reconciliation release only their own charges. Identity records remain charged.

Totals require nonnegative SQLite integers; overflow to SQLite REAL and noninteger source charges are rejected. Reopen and `audit_usage()` compare totals against an independent scan of retained charge records and verify the trigger definitions. Collection also performs this accounting audit before its existing payload integrity pass. A missing or inconsistent total is refused, never rebuilt implicitly. These checks assume the existing private store and trusted writers: an external edit to a plausible total during an open owner is detected by an audit or reopen, not by a history scan on each admission.

Format 4 adds these totals. A format-3 root requires explicit `SharedStore(..., upgrade_accounting=True)` to validate checkpoint integrity, reconstruct totals from retained records and install triggers with the version change in one initialization transaction. A crash before commit leaves format 3; after commit it leaves complete format 4. Interrupted calls and their reservations survive. Formats 1 and 2 remain unsupported. Normal opens never infer a missing ledger is permission to upgrade or repair it.

Opening a root, upgrading, explicit accounting audits and checkpoint collection still scan retained history. Scratch admission separately audits managed scratch and sums outstanding launch allowances; this change does not optimize that path. The result-expiry and uncertainty indexes remain unchanged. No bound on full-store audit latency or physical-disk usage follows from indexed admission.

The manually dispatched [MXC accounting scaling workflow](../../../.github/workflows/mxc-accounting.yml) runs [accounting_benchmark.py](../../../scripts/experiments/mxc_session_patch/accounting_benchmark.py) on GitHub Linux and Windows runners. It seeds 0, 1,000 and 10,000 interrupted identities and measures 50 admissions at each size. Its scan control uses the prior aggregate lookups with the same current trigger/write costs. Each mode reports admission and write-lock median/p95, admissions per measured second, accounting audit time and zero-payload collection time separately. Setup, reconciliation and teardown are excluded from admission timing. Identities accumulate during measurement. This synthetic workload does not measure native guest execution, payload-heavy collection, concurrent-writer throughput or physical power loss. [Run 37540209616, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/37540209616) passed on both platforms. The [Linux result](https://github.com/sokolaidev/maf-extensions/actions/runs/37540209616/attempts/1) and [Windows result](https://github.com/sokolaidev/maf-extensions/actions/runs/37540209616/attempts/1) retain the complete measurements, Python/SQLite versions and source identity: PR head `1984359b5836bed9c511de9ed8b151d39ac0fdfe`, tested merge `f7ac3453306b89852a27010b26436c29cd94c03a`.

The pairs below are **stored totals / scan control**. Throughput covers measured admission time only, not the excluded reconciliation step.

| Runner | Initial retained identities | Median admission (ms) | Median write lock (ms) | Admissions / measured second |
| --- | ---: | ---: | ---: | ---: |
| Linux | 0 | 1.300 / 1.202 | 1.285 / 1.181 | 572.0 / 468.2 |
| Linux | 1,000 | 1.224 / 1.265 | 1.204 / 1.245 | 221.9 / 622.1 |
| Linux | 10,000 | 1.123 / 2.367 | 1.104 / 2.345 | 832.4 / 284.5 |
| Windows | 0 | 38.191 / 38.304 | 38.050 / 38.193 | 25.4 / 25.2 |
| Windows | 1,000 | 41.849 / 43.756 | 41.711 / 43.616 | 23.2 / 22.5 |
| Windows | 10,000 | 40.476 / 41.037 | 40.330 / 40.895 | 24.2 / 20.8 |

At 10,000 initial identities, the stored-total accounting audit took 8.153 ms on Linux and 12.304 ms on Windows; zero-payload collection took 9.547 ms and 13.955 ms respectively. Those history scans remain. Linux median admission improved in this run; the Windows median difference was small. One runner sample does not establish a general speedup or isolate filesystem-flush cost from scheduling noise. The regression test independently verifies absence of aggregate admission queries and stable SQLite VM work between 0 and 5,000 retained identities.

#### Journaled supervisor increment

[shared_call.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/shared_call.py) provides an optional importable supervisor over the shared store. It uses the existing bounded helper protocol through a pre-header callback in [host_call.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/host_call.py); the original CLI still uses the original store. The host must supply its pinned compatibility profile when opening `SharedStore`, plus explicit `ScratchLimits` for per-call bytes, filesystem entries and aggregate temporary bytes. Admission records the logical reservation and scratch allowance in one transaction. A mismatched aggregate scratch budget refuses new admission. Saved retries return without scratch creation or helper launch.

[native_journal.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/native_journal.py) creates a private directory at the recorded random token and commits helper identity before the supervisor sends the startup header. Windows identity uses the process creation FILETIME and checks termination through a process handle. Linux identity uses boot ID, observer PID namespace and process start ticks. A missing process stat file or `ESRCH` while reading an already-open stat file establishes process disappearance; unavailable boot/namespace evidence and other read errors still refuse reconciliation. A reused PID with a different creation identity establishes that the recorded process is gone; permission failures, a different machine/boot/namespace or malformed identity refuse reconciliation. The serialized identity has an integrity digest. These checks cover the single VM-owning helper, not arbitrary descendants.

Publication retains the scratch allowance. `NativeJournal.reclaim` refuses while the recorded helper is alive, commits cleanup intent, checks and removes the bounded private tree, then releases scratch and any interrupted logical reservation in a final transaction. Completed results remain replayable, while interrupted call identities remain interrupted. Retirement permits this cleanup after helper exit but does not actively terminate the helper. An outstanding launch blocks new calls and checkpoint collection for that session, including after successful publication. An explicit retry of reconciliation handles process death between deletion and charge release; it does not execute guest code.

Unknown directories, links, hard links, excess bytes or excess entries refuse cleanup and remain charged. A crash before identity persistence leaves a conservative unresolved launch, even if the directory is absent: startup was never authorized, but this increment has no durable evidence sufficient to reclaim an unidentified process's files. It does not automatically retry or discard that reservation. The filesystem procedure assumes a private local tree without external writers; descriptor-relative race resistance and durable directory deletion across power loss remain unqualified. Temporary allowances are accounting reservations, not operating-system disk quotas, and whole-volume safeguards remain pending even with the bounded serializer below.

Local tests use real lightweight child processes to exercise header ordering, Windows creation identity, blocked cleanup while a helper lives, permission failure, identity corruption, shared scratch admission, preserved delivery after cleanup, and abrupt host death around identity persistence and every cleanup transaction boundary. They do not exercise Hyperlight. The original Linux/KVM probe below qualifies Linux process identity and the new supervisor for its fixed workload; the later format-4 qualification extends these controls to Windows/WHP. Format 3 introduced launch and scratch records; format 4 adds transactional totals with the explicit upgrade described above. Earlier native evidence below remains evidence for its recorded source and format.


#### Bounded checkpoint export overlay

[storage_patch.py](../../../scripts/experiments/mxc_session_patch/storage_patch.py) manages a third, separately removable overlay above the existing session and output overlays. [storage-patch.json](../../../scripts/experiments/mxc_session_patch/storage-patch.json) pins the Hyperlight host source, runtime, MXC base and every changed file. The overlay adds a bounded snapshot-save entry point at the OCI writer, forwards it through the runtime and session wrapper, and enables the helper's `call-stored` mode. Existing helper modes retain their original export behavior. Applying or removing the overlay refuses changed prerequisites or mixed state; remove storage before output, then remove the original session patch.

The serializer charges each complete file payload against a cumulative byte budget and each staging-file creation against a file-count budget before creating that file. It requires a new candidate directory. Exhaustion refuses export and retires the uncommitted session; it never produces a successful control report or publishes a partial checkpoint. Charges are not refunded after a failed write. This conservative write budget includes OCI metadata and staging payloads, and prevents partial export from exceeding its allowance. It does not cap the in-memory snapshot allocation, filesystem allocation units, SQLite pages or unrelated writers.

The optional shared supervisor passes its configured checkpoint byte/file bounds to `call-stored`, refusing helpers without that mode. Before reserving new work it checks that scratch covers two maximum checkpoint copies, bounded stdout/stderr diagnostics, retained console output, request/control bytes and conservatively bounded path entries. Completed retries bypass that new-work capacity check. Failed exports retain reservations until journal reconciliation verifies termination and removes their bounded partial files.

[storage_probe.py](../../../scripts/experiments/mxc_session_patch/storage_probe.py) is wired into the existing opt-in `mxc_recovery` GitHub workflow. It checks one-byte and one-file native refusals, observes the partial candidate within those bounds, reconciles scratch, restores the preceding committed Python value, and verifies restart/result replay and replay after checkpoint collection. The runner also builds the locked dependency graph, runs standalone budget tests, and checks removal of the storage and output overlays. It then checks the original session patch's state without removing it. The [Linux/KVM result](https://github.com/sokolaidev/maf-extensions/actions/runs/37370958352/attempts/2) records successful locked native compilation, budget tests, live probes and storage/output overlay removal in [GitHub Actions attempt 2](https://github.com/sokolaidev/maf-extensions/actions/runs/37370958352/attempts/2), at source commit `b1ca4bb0a2dfaf04308ab55f584770e78c5f0b25`. The one-byte bound refused before writing any file; the one-file bound refused after one 915,800,064-byte file. Both preserved the preceding checkpoint and allowed verified scratch cleanup. A new supervisor process replayed the saved result without launching a helper, and replay remained identical after checkpoint collection. This is fixed-workload process-restart evidence on Ubuntu 24.04 x86-64/KVM with Python 3.12.3 and Rust 1.98.0. This historical result does not qualify Windows/WHP; see the later format-4 qualification below. Physical host reboot, power-loss cleanup and whole-volume disk headroom remain unqualified.


#### Format-4 native qualification

[qualification_runner.py](../../../scripts/experiments/mxc_session_patch/qualification_runner.py) retains the legacy pinned format-4 runner for reproduction and baseline compatibility checks. Its dedicated `mxc_durability` dispatcher is retired. Dispatch `mxc-native.yml` with `mxc_v1=qualify` for the current candidate on Linux/KVM and Windows/WHP: that runner executes the same `storage_probe` and `durability_probe` modules, alongside native state, bounded output and retained-checkpoint compatibility checks. Missing hypervisor support or failed probes refuse qualification; dependency resolution alone never qualifies native execution.

[durability_probe.py](../../../scripts/experiments/mxc_session_patch/durability_probe.py) starts a separate supervisor for each native call and forcibly exits at eight publication/cleanup boundaries. Reopening verifies retained results without an available helper, refuses interrupted calls, reconciles reservations and scratch after verified helper termination, and compares transactional totals against independent source-row aggregates. The next native call must continue from the most recently committed Python value. Separate stores exercise per-session and shared-store quota refusal before helper launch while preserving completed retries. Obsolete checkpoint collection must remove the seed checkpoint while preserving its saved result.

The workflow retains reports and diagnostics for 14 days, excluding SQLite stores, guest snapshots and scratch payloads. Reports bind the helper hash, pinned sources, operating system and workflow commit/run/attempt. Overlay removal is checked after successful native controls. Local tests simulate guest execution while using real supervisor and helper processes; they validate the harness, not Hyperlight. The [Linux/KVM result](https://github.com/sokolaidev/maf-extensions/actions/runs/37682974926/attempts/1) passed in [run 37682974926, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/37682974926) at source `e0881bb1898fc006a0fbed9d45c531490887f291`, with Python 3.12.3 and SQLite 3.45.1. All eight crash boundaries, both quota controls, replay after collecting eight checkpoints, bounded-export controls and removal of all three overlays passed. The [Windows/WHP result](https://github.com/sokolaidev/maf-extensions/actions/runs/37682974926/attempts/1) independently passed the same controls in that run at the same source, with Python 3.12.10 and SQLite 3.49.1. Physical reboot, power loss, Windows/Linux checkpoint portability, concurrent native writers, physical disk headroom, payload-heavy collection performance and production adapter conformance remain outside this qualification.

### Optional persistent-session idle expiry

A new session can opt into `SharedStore(..., idle_policy=Policy(timeout_seconds, grace_seconds=300))` from [idle.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/idle.py). Omission disables idle expiry. The policy becomes part of the session profile: reopening requires the same policy, and an existing session without a policy cannot acquire a guessed activity timestamp. Enabling a new session upgrades the shared root to format 5 in its owner transaction; format-4 binaries refuse that root. Existing sessions without idle expiry remain usable by this implementation, and roots with no idle-enabled session stay at format 4. The one bounded idle record fits within the existing session metadata allowance.

The first deadline starts at session creation. Admission suspends the timer; successful non-native publication starts a new interval, while native calls start it only after journaled scratch cleanup releases their outstanding records. Running calls and unresolved reservations prevent automatic idle retirement. After owner death, explicit recovery must establish helper termination and finish cleanup before the idle interval starts. This is an inactivity policy, not an execution timeout or an unidentified-helper collector.

Opening a session, reading its checkpoint and retrying a completed result do not refresh its deadline. `begin` evaluates idle expiry after capacity validation in the same write transaction that reserves a new call; an elapsed grant cannot authorize delayed admission. A retirement decision commits before returning refusal, while a failed reservation rolls back any new grant. `restore` checks idle retirement before reading checkpoint bytes. A host scheduler can call `NativeJournal.expire_idle()` on the owning thread, or reopen an unowned session with its original profile and policy and call it there. The experiment creates no background thread and holds only its existing session lock. Checkpoint collection remains a separate bounded operation; idle retirement leaves promised result/artifact retention and permanent identities intact.

The deadline and remaining forgiveness persist in SQLite. A reopened owner or an observed clock discontinuity uses the same bounded policy as result retention: grants of at most one second, prepaid in the transaction, a fixed outer deadline, and no refund of a grant lost at restart. Only completion of newly admitted work replenishes the next idle interval's budget. UTC before the base deadline retains the session without spending forgiveness; an incorrect host clock can delay or accelerate real elapsed expiry. Transaction failure cannot authorize retirement or a new grant. Policy changes, missing metadata and incompatible formats refuse explicitly.

Local controls cover exact deadlines, all three supervisors, real helper cleanup after owner recovery, independent sessions, retained retries, clock jumps, grant exhaustion across restarts, and process death before/after retirement and grant commits. The bounded-file GitHub runner additionally exercises a real native call while controlled host time crosses the idle timeout, then checks cleanup, restart, retirement and retained artifact retries. This increment passed on GitHub Linux/KVM and Windows/WHP at `246ce970a7b0f6ea13f81dc78badd0b5bf0a1cd8`; the [candidate record](#qualified-idle-expiry-candidate) links both complete reports. Controlled clocks establish policy behavior, not real elapsed-time guarantees under arbitrary host-clock failure. Earlier qualification reports retain their original source commits.


### Optional physical-storage safeguards

A new shared root can opt into `SharedStore(..., physical_policy=physical.Policy(database_bytes, transaction_bytes, minimum_free_bytes))`, importing `physical` from this experiment. All three byte values are explicit host configuration; the database ceiling and transaction allowance must be positive, and the minimum free-space floor may be zero. The database ceiling must be a multiple of SQLite's page size. The fixed policy is persisted in format 6 and must match on every open, including another session in the same root. Older supervisors refuse format 6. Existing format-4/5 roots cannot enable this policy online: an older live owner would otherwise retain an uncapped connection. Offline migration and policy resizing are separate work. Disabled roots retain their existing format and behavior.

Every connection reapplies and verifies SQLite's `max_page_count`, retains `journal_mode=DELETE` and `synchronous=FULL`, and uses memory for SQLite temporary tables. The page limit caps the main database file; it does not cap an active rollback journal. `journal_size_limit` is deliberately not treated as such a cap: SQLite applies it after commit/checkpoint. See the [SQLite pragma contract](https://www.sqlite.org/pragma.html#pragma_max_page_count) and [journal-size semantics](https://www.sqlite.org/pragma.html#pragma_journal_size_limit).

Before creating a root/session, admitting a new call, preparing native scratch or capturing a publication, a write transaction checks free space on the root volume. Required headroom is remaining main-database growth to its ceiling, plus the host's transaction allowance, minimum free-space floor, every outstanding scratch allowance in full, and any proposed new scratch allowance. Existing scratch bytes are counted again conservatively rather than credited before verified cleanup. The transaction allowance covers the host's journal, filesystem-allocation and diagnostic margin; it is a configured allowance, not a measured or enforced journal maximum. This check observes capacity without reserving filesystem blocks. Unrelated writers, concurrent roots and external destinations can still consume space after observation. Checkpoint/restore copies outside journal-managed scratch remain the caller's responsibility; there is no operating-system disk quota or memory bound here.

Low-space admission refuses before persisting the new call or launch. Publication failure preserves the preceding committed checkpoint, retained result and outstanding reservation; it cannot acknowledge durable success. Reopening an existing session, replay and cleanup do not require admission headroom, although their own SQLite writes can still fail explicitly when the filesystem cannot support them. SQLite errors propagate after rollback, including `SQLITE_FULL`; the [transaction error contract](https://www.sqlite.org/lang_transaction.html#response_to_errors_within_a_transaction) permits SQLite to roll back either a statement or its transaction, so the host transaction wrapper also rolls back. No automatic result expiry or deletion is used to make room. Collection permits page reuse and does not promise that the database file shrinks.

The local controls exercise exact admission thresholds, competing session scratch allowances, policy/reopen compatibility, missing capacity observations, real `SQLITE_FULL` during incompressible checkpoint capture, injected `ENOSPC`, owner-process restart, retained replay and low-space collection. The [physical-storage workflow](../../../.github/workflows/mxc-physical.yml) runs these controls on Linux and Windows and retains JUnit results plus Python/SQLite identities in its logs. These are host-storage controls, not native Hyperlight execution or actual whole-volume exhaustion. Power loss, active-journal I/O fault injection, filesystem quotas and format migration remain unverified. This increment does not close #1672.


[Run 37981601154, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/37981601154/attempts/1) passed at implementation source `217bc83e4ff931ae3a1b8820b89f5efffc74d9da`. The [Linux job](https://github.com/sokolaidev/maf-extensions/actions/runs/37981601154/job/113993232879) used Python 3.12.3 and SQLite 3.45.1; the [Windows job](https://github.com/sokolaidev/maf-extensions/actions/runs/37981601154/job/113993233290) used Python 3.12.10 and SQLite 3.49.1. Each retained JUnit report contains 34 tests, zero failures/errors and zero skips. These reports qualify the host-storage controls above; they do not extend the historical native format-4/5 reports to format 6.


The bounded-file native runner subsequently qualified format 6 on Linux/KVM and Windows/WHP at `3bc434135055176d0e3d136e239cd27310ac7afa` in [run 38006811225, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38006811225/attempts/1). Its fixed-workload probe first measures normal two-checkpoint growth in a separate calibration store, then creates a new root with an immutable ceiling. A 64 MiB incompressible session upload triggers real `SQLITE_FULL` after guest execution/export and confirmed helper exit. A fresh supervisor reclaims retained scratch, recovers the prior Python value and session file, publishes successfully, and replays the original artifact result byte-for-byte after checkpoint collection. Both reports check the reapplied page ceiling, SQLite integrity and logical accounting. The [candidate record](#qualified-native-physical-storage-candidate) retains the exact reports, limits and hashes. These measurements establish same-machine experimental recovery at the main-database ceiling, not whole-volume or active-journal exhaustion guarantees.

### Disposable-filesystem exhaustion candidate

The [disposable-filesystem workflow](../../../.github/workflows/mxc-volume.yml) creates a 128 MiB ext4 loop volume on Linux and a 128 MiB NTFS VHD on Windows. The [host-store probe](../../../scripts/experiments/mxc_session_patch/volume_probe.py) validates the volume marker, capacity and separation from the checkout and reports before writing. After a real format-6 publication headroom check, a test-only capture override fills that filesystem with actual bytes until the operating system reports `ENOSPC`, then calls the unchanged checkpoint capture implementation. Qualification requires real `SQLITE_FULL` while the database remains below its independent 32 MiB ceiling. The harness frees only its filler after the failed owner exits; a separate process must restore the previous checkpoint, replay identical result bytes, retain the interrupted reservation, refuse re-execution, pass integrity/accounting audits and publish successfully in another session.

This is host-store qualification with synthetic checkpoint bytes, not a native guest run, power-loss test or filesystem quota guarantee. Checkpoint candidates and diagnostics intentionally reside outside the exhausted volume. Cleanup detaches the exact created image and independently checks attachment state before removing it; reports are retained without database, checkpoint or filler payloads. The fixed-workload host-store controls passed on both platforms; the retained evidence below records their exact candidate. Active-journal faults, restore-copy exhaustion, existing-root migration and cross-machine recovery remain separate work. Issue #1672 remains open.


[Run 38060111346, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) passed for PR head `88c72af219a5bd95505a17285538abacb1622f0b`, tested merge `7aadccaef6e8451bd8a94bd42a2a0074f3302acf`. The [Linux report](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) used Python 3.12.3 and SQLite 3.45.1; the [Windows report](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) used Python 3.12.10 and SQLite 3.49.1. Both report zero free bytes, operating-system `ENOSPC` (28), SQLite `SQLITE_FULL` (13), and a 118,784-byte database below the 33,554,432-byte ceiling. Separate-process recovery preserved the checkpoint, byte-identical result and interrupted reservation, refused the interrupted retry, passed integrity/accounting audits, and published successfully in another session. The [Linux cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) and [Windows cleanup](https://github.com/sokolaidev/maf-extensions/actions/runs/38060111346/attempts/1) reports independently confirm image detachment and removal. Earlier Windows fixture attempts refused before seeding because of directory ownership or unsupported SDDL account aliases; the qualified fixture explicitly assigns the new directory's owner and uses supported owner-rights access entries, with the store's existing validation as a preflight. No storage implementation or access validator was changed.


### Native disposable-filesystem exhaustion candidate

The [native volume probe](../../../scripts/experiments/mxc_files_patch/volume_probe.py) places a new format-6 store and its journal-managed restore/export scratch on an owned 8 GiB ext4 or NTFS image. Diagnostics stay outside the image. The fixed policy permits a 1 GiB main database, a 128 MiB transaction allowance and a 64 MiB free-space floor; this is a fixture configuration, not production sizing. After a native call changes Python state and session files, exports its checkpoint, terminates its helper and passes the real publication headroom check, a competing filler consumes the remaining filesystem space. Publication must return real `SQLITE_FULL` below the database ceiling without acknowledging success.

Separate supervisors must preserve the preceding checkpoint/result and failed reservation, then refuse the interrupted identity, reclaim scratch and execute a new call from the preceding Python state and session files after the filler is removed. The failed upload must be absent, and the original completed artifact result must replay byte-for-byte after checkpoint collection. The shared volume action verifies exact image ownership before detaching and independently checks removal. Offline tests simulate guest execution and exhaustion. Native qualification passed on Linux/KVM and Windows/WHP at `f2cec9b7efb7e8cd5c4d1d78788f009197200ac8` in [run 38128658746, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38128658746/attempts/1); the [research record](#qualified-native-full-filesystem-publication) retains both complete reports and independent volume cleanup results. This candidate does not exercise exhaustion during guest execution, export or restore copying, targeted active-journal I/O faults, power loss, OS quotas, migration or production integration. #1672 remains open.


### Checkpoint-copy filesystem exhaustion candidate

The [checkpoint-copy probe](../../../scripts/experiments/mxc_files_patch/checkpoint_volume_probe.py) reuses the owned 8 GiB ext4/NTFS fixture and unchanged native helper. It covers two earlier boundaries than the publication probe. Export first restores the previous checkpoint, registers the helper, exhausts the volume, then releases 32 MiB before authorizing native execution. Guest output and an incomplete candidate must prove execution reached export; the helper must report the platform's disk-full error and stop without publishing completion. A fresh supervisor must reclaim its identified scratch, execute from the preceding Python state and session files, collect the old checkpoint and replay its result exactly.

Restore exhausts the volume after checkpoint bytes have started reaching journal-managed scratch, then requires a real `ENOSPC` from the copy. A fresh supervisor must verify the committed checkpoint's content and session files, replay its result, refuse the interrupted identity, and preserve the unidentified launch and its reservation. `NativeJournal.reclaim` and new calls must remain refused: this probe deliberately preserves the existing conservative pre-launch rule. It does not add evidence that an unidentified helper can be reclaimed safely, or claim that the session automatically resumes. The restored bytes are validated without executing a new guest call in that blocked session.

Export runs before restore because the final restore case intentionally leaves charged scratch. Removing the disposable image is fixture teardown, not successful journal reconciliation. Diagnostics and the recovery verification copy remain outside the bounded image; database and failed restore/export scratch remain inside it. Offline controls simulate the guest and I/O failure. Hosted qualification passed on Linux/KVM and Windows/WHP at `0fa619ea6c560b11d15b44e21fde0f67e5b268f6` in [run 38139649785, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38139649785/attempts/1); the [research record](#qualified-checkpoint-copy-exhaustion-boundaries) retains both reports and independent cleanup evidence. Power loss, targeted journal faults, OS quotas, migration, distributed fencing and production integration remain outside this candidate. #1672 remains open.


### Optional pre-launch recovery

New roots may opt into format 7 with `SharedStore(..., physical_policy=policy, prelaunch_recovery=True)`. The physical policy is required, and older roots refuse this option without migration. Reopening format 7 requires the same explicit option and physical policy. Older supervisors refuse its version. Formats 4, 5 and 6 keep their existing conservative launch semantics.

A format-7 `prepared` launch proves the supervisor has not crossed the process-creation boundary. Each console, byte-stream and file transport calls `NativeJournal.start` immediately before spawning its helper, after staging host request files. That method commits `starting` before returning. The existing `arm` step then records the blocked helper's identity before the ownership header can authorize execution. Direct journal callers must preserve this ordering too: `arm` alone cannot establish that process creation was preceded by durable intent. A failed intent transaction cannot spawn; an interrupted or failed spawn after the committed intent remains ambiguous even when no process was actually created.

After restart, a new owner may explicitly call `NativeJournal.reclaim` for an interrupted `prepared` record with no identity. The session lock, earlier call generation, scratch inventory checks and existing cleanup journal govern this path. Cleanup intent keeps the reservation charged; confirmed scratch removal precedes allowance release. An interrupted cleanup resumes with the same proof. `starting` records without identity remain charged and refuse reconciliation. Recorded identities still require confirmed helper termination. The interrupted call identity can never execute again; a new call may be admitted after cleanup, and saved results retain their existing retry contract. Retirement may use the same cleanup path but never permits new execution.

The [process controls](../../../tests/test_mxc_prelaunch.py) cover launch-intent and cleanup crashes, non-execution before the ownership header, all three transport callbacks, idle/retirement interaction, altered schema and old-format refusal. They use ordinary helper processes, not Hyperlight guests. The native checkpoint-copy probe selects this contract with `--prelaunch-recovery`; dispatch `mxc-native.yml` with both `mxc_files=true` and `mxc_prelaunch=true` to exercise export and partial-restore exhaustion on fresh owned ext4/NTFS fixtures. The default dispatch retains the format-6 qualification path. Format-7 restore must reclaim scratch and execute from the previous Python state and session files after space is restored. Hosted process controls and native file-profile qualification passed on Linux/KVM and Windows/WHP at implementation source `53e3566b5a931f079c37be5fbb2542bb8183bc9e`; the [research record](#qualified-pre-launch-recovery) retains the separate process and native results, source identities and independent cleanup evidence. This remains experimental same-machine process recovery; power loss, unidentified spawned-process reconciliation, migration, distributed fencing and production integration remain outside its contract. #1672 remains open.

### Optional coordinator API experiment

[remote_api.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_api.py) supplies an importable, explicitly enabled coordinator and client over mutual TLS. Configure `Coordinator(root, grants, sessions, enabled=True)` on the service process, where `grants` maps SHA-256 fingerprints of DER client certificates to `Grant(host, boot, sessions, operations)`, and `sessions` maps session names to `Session(profile, limits)`. Provision a new, empty root once with `initialize=True`; ordinary startup and every restart omit that flag and require the existing ownership journal and every configured publication session. Limits reuse the shared-store logical quota, result retention and forgiveness-budget configuration. All sessions in one publication root must use the same aggregate store quota. Start `Server((bind_host, port), server_context(certificate, key, ca), coordinator)` and call `serve_forever()` on that process; close the server and coordinator when it exits. The root must be coordinator-local private storage. Certificate/key/CA arguments are paths to host-provisioned PEM files; no credential material belongs in requests.

Execution hosts construct `Client((host, port), client_context(certificate, key, ca), expected_hostname)`. `Client.call(operation, session, **arguments)` returns a response dictionary or refuses; transport failures never trigger automatic retries. Each request uses a fresh TLS connection with chain and hostname verification, so a removed certificate mapping takes effect after service restart. The service validates client certificate chains and matches exact certificate fingerprints; certificate subject names and worker-supplied fields do not grant authority.

| Operation | Arguments | Response |
|---|---|---|
| `acquire` | `guest`: fresh guest incarnation identifier | `owner`: opaque serialized ownership record; repeat the same resource identity to resolve a lost acquire acknowledgment |
| `begin` | `owner`, `call`, base64 `request` | `result`: `null` for newly admitted work, otherwise the saved base64 result; an interrupted/pending identity refuses re-execution |
| `publish` | `owner`, `call`, base64 `request`, `files` mapping flat names to base64 bytes, base64 `result` | `committed: true` after checkpoint/result commit; resolve an ambiguous outcome with `retry` |
| `retry` | `owner`, `call`, base64 `request` | Saved base64 `result`; unknown identities refuse without admission |
| `restore` | `owner` | `call` and base64 `files` for the current checkpoint, independently of result expiry |

Version 1 (`Client.call`) retains the 8 MiB JSON frame, 64 KiB decoded call request and flat-file representation. Its `publish` and `restore` refuse sessions whose configured checkpoint plus result allowance exceeds 4 MiB; it never buffers a native-size checkpoint. Admission and retry remain version 1 for every session. Results remain bounded to 4 MiB by the coordinator configuration, independently of checkpoint size.

Version 2 uses `Client.publish(session, owner, call, request, checkpoint, result)` and `Client.restore(session, owner, destination, checkpoint_bytes=..., files=...)`. Publish requires an already admitted call and a stable source tree; request/result arguments are bytes. Restore returns the saved call identifier (or `None` when no checkpoint exists), refuses an existing destination, and exposes a directory only after all hashes and the final coordinator response verify. Its optional download limits can narrow the format maxima. These operations use the same `publish`/`restore` certificate permissions; they do not admit work or grant ownership.

A version-2 publish sends a bounded JSON inventory with each relative path, exact size and SHA-256 digest; the coordinator authorizes scope, ownership, request identity and reservation before replying ready. Raw file bytes follow in inventory order, using at most 1 MiB blocks, followed by a framed completion record after the sender rechecks its source hashes. The receiver verifies every file before the existing fenced checkpoint/result commit. Restore sends the inventory, raw bytes and a final ownership-checked completion response. JSON records remain capped at 8 MiB. Streaming I/O shares one absolute 300-second deadline per endpoint, rather than resetting a timeout for every block. Hashing and SQLite work remain synchronous; this is not a CPU or transaction-latency guarantee. TLS handshakes and initial request framing retain the 10-second limit.

The format permits at most 2 GiB of checkpoint bytes, 128 files, 16 path components and 512 UTF-8 path bytes, narrowed by session limits. Paths use the shared-store portable name grammar; duplicate paths, file/directory overlap, case aliases (including parent directories), device names and links refuse. Empty files are retained; empty directories are not part of the checkpoint inventory. The source must remain stable through publication. One serialized connection owns coordinator staging at a time, so temporary checkpoint payloads are bounded to 2 GiB independently of retained data. Startup and failure cleanup inventory the complete staging tree before deleting anything, with bounded bytes, files, depth and total entries; unexpected links, nonregular entries or excess inventory refuse cleanup. The client stages downloads privately beside the destination. There is no resumable upload state or automatic transfer retry. Interrupted call reservations remain charged after cleanup and service restart; resolve an ambiguous publication with the original call identity. The optional remote scratch mode below adds execution-host allowance reconciliation; coordinator transfer staging remains a separate bound.


`SharedStore.commit(..., commit_guard=...)` prepares the checkpoint in its publication transaction, then enters the supplied authority context immediately around the actual commit. The coordinator passes `Authority.guard(owner)`, so a revocation during preparation rolls back publication and competing revocation cannot pass the final generation check until commit finishes. The guard does not write ownership state. Ownership and publication use separate local SQLite databases with no cross-database atomicity claim; a failure after the publication commit is resolved from the saved call record. Existing local callers use the default no-op guard.

The worker API cannot request or confirm termination. A trusted coordinator controller uses `Authority.request_fence` and `confirm_fence` with its deployment-specific verifier before replacement acquisition. The API tests use synthetic termination evidence and ordinary processes, not Hyperlight guests. Native guest integration, deployment termination/network enforcement, multi-host qualification and production serving remain outside this candidate. The optional remote scratch mode below qualifies accounting and reconciliation with synthetic trusted evidence.

Version-2 transport qualification passed all 128 ownership/API controls on both Linux and Windows in [run 38803765472, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38803765472/attempts/1), including a 1 GiB blob round trip with coordinator restart and verified saved-result retry. The [report](https://github.com/sokolaidev/maf-extensions/actions/runs/38803765472/attempts/1) binds both original JUnit hashes and payload sizes to tested merge source `ca6ec868abe2a54d21cc9577ccafd3bab789f507` (implementation `3f46866756c910ebafeac10ff7418e909c7bcb90`). This evidence uses synthetic bytes; it does not establish native checkpoint boot, cross-OS portability or the 2 GiB format maximum end to end.


#### Coordinator journal loss

Normal startup opens both journals before accepting requests and refuses missing, empty or corrupt journals and absent configured publication sessions. Ownership operations always reopen the existing database; they cannot initialize a replacement after storage disappears while the service is running. SQLite existing-only opens use `mode=rw` so a missing file cannot be recreated between checking its path and connecting. Failed startup releases all acquired handles. Adding sessions to an already provisioned root requires a separate migration; startup does not silently create them.

Initialization requires an empty root under the service lock and is a provisioning action, never a fallback for failed startup. The cross-runner driver passes `--initialize` only to its first coordinator service process, and omits it on restart. Do not derive this flag from journal existence in a deployment supervisor. If initialization stops partway through, a missing journal causes normal startup to refuse; preserve the evidence and reprovision only before any owner has been issued. Losing the complete root cannot be distinguished from a new deployment by local files alone, so the operator must preserve this provisioning/startup distinction outside that root.

The ordinary regression controls remove, empty or corrupt each closed journal while a committed result, pending fence and scratch receipt exist. They require startup refusal without recreating or rewriting the damaged journal. Restoring the exact latest test bytes preserves the pending fence, scratch charge, saved result and checkpoint. This is storage-failure refusal coverage, not a backup restoration protocol: stale but structurally valid databases, mismatched journal backups, live mounted-storage replacement and permanent storage loss require separate authority and recovery designs. No timeout, missing file or failed integrity check authorizes takeover, scratch refund or tool reexecution.

### Optional remote execution scratch accounting

Configure a **new coordinator root** with `scratch_policy=remote_scratch.Policy(bytes=..., entries=..., total_bytes=...)`. The policy fixes each call's maximum temporary bytes and filesystem entries and a shared byte allowance across all configured execution hosts. This is logical admission accounting, not a disk reservation or enforcement of native export bounds. The native host adapter must enforce those bounds before this mode can qualify real execution. The default remains the preceding transfer-only experiment. Authority journal format 2 pins the mode and policy; changing either on an existing root refuses. Format-1 roots require a separate migration design. Controllers opening `Authority` directly must pass the same `scratch_policy`.

A new `begin` returns `result: null` plus `scratch`, the serialized `remote_scratch.Receipt`. The receipt binds a coordinator-generated token, full opaque owner (session, generation, host, boot and guest incarnation, runtime/policy digest and owner token), call identifier, SHA-256 request digest and configured byte/entry allowances. It contains no worker-selected host path. Persist the receipt before creating host scratch, and only execute after receiving a successful new admission response. A receipt lookup or ambiguous `begin` outcome never authorizes another execution. Retries of completed calls retain the original result-only response and consume no new allowance.

The authority journal charges scratch first, then the publication store reserves maximum checkpoint/result capacity. They are separate SQLite databases: a partial admission can leave scratch charged even when no call row exists. Coordinator death, a lost acknowledgment, failed admission, ownership revocation and replacement acquisition do not refund it. Successful publication converts the logical checkpoint/result reservation but leaves scratch charged. A session with outstanding scratch cannot admit another call, and the aggregate allowance also limits other sessions. Restore and saved-result retry remain available under valid ownership. Released receipts remain as bounded tombstones for idempotent cleanup and call-identity refusal; the authority metadata capacity (4096 records by default) bounds the ledger, including released receipts. Exhaustion refuses new admission; garbage collection and capacity migration are not implemented.

The trusted controller calls `Coordinator.pending_scratch()` and `Coordinator.reconcile_scratch(receipt, verifier)` on the serialized service owner thread. These are local controller methods, not worker API operations. The verifier must establish termination of the exact call's helper, removal of all scratch covered by that receipt, and prevention of delayed launch or recreation under that token. Reachability loss, lease expiry, guest output, a worker's self-reported cleanup, or machine shutdown alone does not prove scratch removal. Shared-host cleanup must target only the recorded call; this mode grants no machine-shutdown authority. Replacement ownership still requires the separate fencing protocol and its configured scope.

Reconciliation durably marks the receipt `cleaning` before calling the verifier outside SQLite transactions. Both publication formats check the scratch state before payload preparation and again under the final publication guard, so cleanup intent refuses subsequent publication. Anything except the exact boolean `True`, including verifier errors, leaves charges and cleanup intent in place. After proof, cleanup removes the call's remaining publication reservation and marks an unfinished call interrupted, preserving any committed result/checkpoint. Only after that commit does it release the scratch allowance. A crash or storage failure between the commits keeps the scratch charge until reconciliation repeats; no cross-database atomicity is claimed. A repeated completed cleanup is harmless, while a changed receipt refuses before verification. Reconciliation never automatically retries a tool function.

The process controls cover immutable receipt bindings, cross-session capacity, saved-result replay after cleanup, revoked-owner refusal, a replacement owner blocked by old scratch, verifier outages, storage failures, cleanup intent racing both publication formats, coordinator death at two admission boundaries and six cleanup boundaries, and real mutual-TLS admission/publication with worker cleanup refused. Verifiers in these controls supply synthetic trusted evidence. They do not establish native process termination, actual remote file removal, bounded native scratch creation, network-authority revocation, cross-machine checkpoint boot, coordinator-storage-loss recovery, power-loss durability or production support.


### Native host binding for remote scratch

[remote_host.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_host.py) connects the optional coordinator mode to the existing native supervisor. A trusted host configures its host/boot identity, session profile, scratch policy and local physical storage policy. Its local format-7 `SharedStore` pins those settings and retains launch records; it does not duplicate checkpoint/result publication. Keep that journal available on the same execution machine for reconciliation. Missing records, a lost journal or a different machine/boot are not cleanup evidence.

`Host.call` sends `begin` over the existing client and returns saved results without loading the helper. For new work it validates the complete receipt, request digest and pinned helper/startup hashes, then durably admits the receipt token as a local call identity with the complete receipt encoded in its request binding. It creates scratch only afterward. Download stages inside that private tree, the helper uses `call-stored` with explicit checkpoint byte/file limits, and its process creation identity is persisted before the ownership header. Native request, output, diagnostic and checkpoint bounds must fit the receipt's byte/entry allowances, including both checkpoint copies and download staging directories. These are bounded trusted writers, not an OS filesystem quota against arbitrary host processes.

Publication streams the candidate through the coordinator's existing fenced commit. Success and transport ambiguity both retain the local launch record and remote scratch charge. Resolve an ambiguous result using the original coordinator call identity; `Host.execute` never resumes an existing local token. Local scratch uses its own journal-generated directory name, never a path supplied by the receipt. A host root's logical and physical policies bound its retained journal; no new journal migration or tombstone collection is introduced.

The trusted controller may use `Host.reconcile` as its verifier on the execution machine. It acquires the existing session OS lock, validates the full receipt binding, terminates only the recorded helper through its OS creation identity, and invokes the existing bounded cleanup journal. A provably unlaunched record can be reclaimed by the next local owner; an unidentified `starting` record still refuses. Unknown entries, links, excess inventory, changed bindings and unavailable process evidence leave charges intact. Successful cleanup keeps the permanent interrupted local call identity, preventing delayed launch or scratch recreation under that token. A repeated verification checks the retained terminal identity and audits the scratch root. Local cleanup finishing before coordinator release is safe to retry; committed remote results and checkpoints remain intact.

This is a trusted local controller adapter, not a worker cleanup RPC or a remote proof transport. A deployment must establish how its coordinator invokes and trusts the correct machine's controller. It does not grant machine shutdown, revoke network authority, or prove that every guest belonging to an ownership generation has stopped. Those remain separate fencing requirements before a replacement owner is admitted. An admission lost before local journaling remains conservatively charged and needs deployment-specific reconciliation; an empty local directory cannot release it.

The ordinary process controls exercise exact bindings, active-helper termination, host process death, cleanup failures, preserved result replay and delayed receipt refusal. The opt-in MXC 1.0 runner also executes `test_native_host_real_helper_tls_restore_and_cleanup`: two native calls persist and restore Python state through a restarted mutual-TLS coordinator, with explicit local cleanup between calls. Its `remote-native.xml` and stage log are retained with the runner's source/helper identity. The [evidence](https://github.com/sokolaidev/maf-extensions/actions/runs/38970794986/attempts/1) records a successful native control with zero skips on both Linux/KVM and Windows/WHP in [run 38970794986, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/38970794986/attempts/1), at implementation `266daedf00e00983222b037dec52bdf6d41b0755`. Linux published 862,272,720-byte and 862,293,200-byte checkpoints; Windows published 862,231,810-byte and 862,252,290-byte checkpoints. Each platform also passed 199 ordinary ownership/API controls with a 1,073,741,826-byte synthetic checkpoint in [run 38970777428](https://github.com/sokolaidev/maf-extensions/actions/runs/38970777428/attempts/1); only the separately dispatched native control was skipped there. This is same-machine evidence, not recovery between distinct machines or cross-OS portability.


### Deployment controller for generation fencing

[remote_control.py](../../../packages/maf-sandbox-mxc/src/maf_sandbox_mxc/remote_control.py) supplies a separate, opt-in mutual-TLS listener for a trusted host controller. It reuses the bounded framing and TLS transport but exposes only `fence`; the worker publication listener does not gain this operation. Controller certificate fingerprints map to explicitly configured owner tokens. Use a dedicated controller certificate and a server certificate/hostname identifying the intended controller; do not grant execution-worker certificates controller authority. The coordinator configures the controller address and TLS trust locally, never from guest output or a worker request.

Provision a fresh `Host(..., owner=owner)` journal once for each complete ownership identity, before that generation executes any call. Its immutable local profile binds the session, coordinator generation/token, machine/boot/guest incarnation, runtime, limits and closed-network policy. Every call for that owner must use that one private root. A replacement generation uses a different root; an existing unbound journal cannot be silently adopted. Persist the owner-to-root configuration across controller restarts. Never recreate or reprovision an erased root as proof of termination: deployment configuration and retained private storage are trust assumptions, and a lost journal blocks this adapter.

Configure `Controller({owner.token: host}, {controller_fingerprint: frozenset({owner.token})}, enabled=True)` behind the existing `Server` using `server_context`. On the coordinator's serialized service thread, call `Coordinator.fence(owner, request_id, Verifier(client, owner))`, where `client` uses `client_context` with the dedicated controller credentials and verified controller hostname. These are trusted orchestration interfaces; neither provisioning nor fencing is a worker RPC.

The coordinator first persists revocation, refusing old publication and replacement acquisition. Outside its SQLite transaction, the verifier sends the complete owner and fence request. The controller checks certificate scope and the immutable journal binding, acquires the session OS lock, and durably retires the whole generation before invoking the existing `NativeJournal.delete` protocol. It terminates only recorded helpers by OS creation identity, reclaims their bounded private scratch, and audits the scratch root. The retired session blocks every later call, including an issued receipt that never reached local admission. Only a response matching the complete owner and fence allows the coordinator to confirm termination and admit a replacement.

A live supervisor holding the session lock causes refusal; this adapter does not kill the supervisor or steal its lock. Takeover waits until the supervisor exits and the controller can prove cleanup. Unidentified launches, unknown files, unavailable controllers, missing or incompatible journals, TLS failures and storage errors leave the fence pending. Controller death after retirement or cleanup is retryable with the same request; coordinator death or a lost response likewise requires repeating that identity. Scratch charges remain separate: fencing does not refund them. Use existing receipt reconciliation after proof, and retain charges for receipts never recorded locally.

Only guest-scoped requests for a pinned closed-network profile are supported. Automatic termination requires explicit opt-in in both the coordinator's `fencing_policy` and the controller's `automatic_shutdown`; both default to false. Whole-machine shutdown is refused even on dedicated hosts. This adapter cannot revoke an external network authority or prove arbitrary descendants have stopped, and must not serve an allowlisted/network-enabled generation. The existing helper/profile qualification remains the basis for the closed native workload, not a claim of general process containment.

Process controls cover a separate TLS controller, actual recorded-helper termination, an unrelated surviving process, delayed-receipt refusal, certificate and request scope, busy/missing/ambiguous journals, six controller-death boundaries, coordinator restart and independent automatic-termination opt-ins. The tests substitute ordinary processes for native execution; they do not add native distinct-machine recovery, power-loss, coordinator-store-loss or production qualification. The Linux/Windows remote-control workflow runs these alongside the existing transfer controls.


### Native recovery between GitHub runners

The manually dispatched [cross-runner workflow](../../../.github/workflows/mxc-cross-runner.yml) builds one pinned Linux helper/startup fixture, then starts three concurrent Ubuntu jobs: execution hosts A and B, and a separate coordinator/storage host. [cross_runner.py](../../../scripts/experiments/mxc_session_patch/cross_runner.py) is a qualification driver, not a deployable service API. Its fixed-program and orchestration operations are available only to the current run's coordinator certificate; the experimental worker API and deployment controller retain their existing authorization. No package gains a Tailscale dependency.

The participant jobs join Tailscale as ephemeral `tag:mxc-ci` nodes using GitHub OIDC. Each generates its own two-hour TLS identity and retains its private key locally. Public discovery artifacts bind the role, overlay address, certificate, hashed Linux boot identity, helper/startup profile, source commit, workflow run and attempt. Every participant rejects mixed attempts, duplicate machines and incompatible profiles. Artifacts carry only that public discovery and the common build fixture; runtime checkpoints and saved results cross the existing mutual-TLS streaming API. The private journals, TLS keys and recovered call state are never uploaded as artifacts.

Before dispatch, configure the following in a dedicated test tailnet or a policy that isolates the CI tag. See Tailscale's [GitHub Action](https://tailscale.com/docs/integrations/github/github-action) and [workload identity federation](https://tailscale.com/docs/features/workload-identity-federation) instructions for identity setup.

| Setting | Required value |
| --- | --- |
| Tailscale tag | `tag:mxc-ci`, owned by the administrator configuring the experiment |
| Network grant | `tag:mxc-ci` to `tag:mxc-ci` on TCP port 9443 only; no grants to other tailnet resources |
| Federated identity issuer | `https://token.actions.githubusercontent.com` |
| Federated identity subject | `repo:sokolaidev@259664926/maf-extensions@1326989641:environment:mxc-cross-runner` |
| Additional claim | `workflow_ref` equals `sokolaidev/maf-extensions/.github/workflows/mxc-cross-runner.yml@refs/heads/main`; use only a specifically reviewed branch when qualifying before merge |
| Identity scope/tag | `auth_keys`, scoped to `tag:mxc-ci` |
| Repository secret | `MXC_TAILSCALE_CLIENT_ID`: the federated identity client ID |
| Repository secret | `MXC_TAILSCALE_AUDIENCE`: the identity's configured audience |
| GitHub environment | `mxc-cross-runner`, restricted to the same trusted branch as the identity |
| Runner concurrency | Three Ubuntu participant jobs must run at once; discovery fails after ten minutes if they cannot overlap |

This repository uses GitHub's immutable OIDC subject prefix (owner/repository IDs included), verified through `gh api repos/sokolaidev/maf-extensions/actions/oidc/customization/sub`. Recheck that value when configuring the identity; a name-only subject will not match. GitHub's [OIDC reference](https://docs.github.com/en/actions/reference/security/oidc) describes the environment and workflow claims.

Merge the narrow network grant into the tailnet policy; an existing allow-all rule still grants access and must not cover these nodes. No exit node, subnet route, Tailscale SSH or public listener is needed. Each listener binds only its overlay address. The pinned Tailscale action enables subnet-route acceptance during connection; a required follow-up step disables DNS and subnet-route acceptance and verifies both preferences before discovery or MXC execution. The tailnet isolation policy must also hold during that connection window. The workflow grants `id-token: write` only to participant jobs and requests no reusable auth key or OAuth secret. Missing repository secrets fail before the native build starts. The action logs its ephemeral node out during job cleanup; a canceled or timed-out workflow is not successful recovery evidence.

For the first run, merge the new workflow into the default branch so GitHub registers its manual trigger, then dispatch `mxc-cross-runner.yml` once configuration is in place. Later changes can be dispatched from an explicitly trusted reviewed ref. Re-run all jobs together: failed-job-only reruns cannot reuse another attempt's fixture or discovery identities. Only this explicit dispatch joins the tailnet; ordinary PR tests use loopback TLS and a substitute helper. The workflow retains public discovery and the build fixture for one day, and role reports/build diagnostics for fourteen days. Role artifacts also retain any surviving `native.stdout` and `native.stderr` files from the fixed test programs; each helper stream is capped at 1 MiB. These explicit paths exclude credentials, journals and checkpoint payloads. Successful reconciliation removes scratch, so a successful run may have no helper streams to upload. Participant jobs have a thirty-minute limit. A connection can use Tailscale's relay when direct connectivity is unavailable, so a native-size transfer may hit the existing five-minute transfer deadline; this fails qualification rather than falling back to artifact transfer.

The scenario publishes `remote_counter = 41` on A, restarts the coordinator process against its retained journal, fences A through the trusted controller, reconciles its scratch, acquires a replacement generation on B, retrieves the saved result with a deliberately absent helper, and executes a fresh native call that restores and advances the counter to 42. It requires checkpoint payloads larger than the small-frame API limit, verifies competing-owner, consumed-receipt and stale-owner refusal, and checks that an unprovisioned journal leaves termination unconfirmed and takeover blocked. Both execution generations are explicitly fenced and reconciled on success. Automatic shutdown stays disabled; these are explicit guest-scoped requests.

A successful local control establishes orchestration and protocol behavior with a substitute helper, not native migration. A successful hosted run additionally establishes recovery between the particular Linux machines and pinned native fixture recorded by that run. Neither establishes abrupt execution-machine loss, coordinator-storage loss, power-loss durability, cross-OS portability, arbitrary descendant termination, network-enabled guests or production support. The coordinator's runner-local storage is independent of A/B only for this workflow's lifetime. The [hosted evidence record](#hosted-native-cross-runner-evidence-2026-10-24) retains the successful Linux run, exact report identities, an earlier unexplained native failure and a repeat refused by Hyperlight for AMD-to-Intel snapshot restoration. The admission candidate now requires both execution hosts to match the fixture producer CPU vendor before native work. The [CPU-admitted repeat](#cpu-admitted-native-repeat-2026-10-25) passed on a matching AMD fixture/A/B allocation. Issue #1671 remains open for native owner-loss and other failure-case qualification; the successful fixed workload does not establish the broader deployment claims above.


#### CPU compatibility admission

The Linux cross-runner fixture now includes `fixture.json`, recorded only after the producer completes its fresh native startup controls. It binds the helper and startup-index hashes, closed policy, Linux/x86-64 profile and producer CPU vendor to the source SHA, workflow run and attempt. Each execution host reads every processor's `vendor_id` from `/proc/cpuinfo`; missing, unknown, duplicate or mixed values refuse admission. The current candidate accepts only `GenuineIntel` and `AuthenticAMD`. A missing producer record, including an older fixture, also refuses admission.

Discovery requires A and B to match that producer vendor, even when A and B match each other. The coordinator carries the common fixture profile but does not probe or match its own CPU: it never restores a guest. Execution hosts recheck their local CPU and fixture before serving requests, before any session acquisition or checkpoint transfer. Refusal leaves a failed stage report; successful bootstrap and discovery leave only pending status, and only a completed native scenario can report passed. Existing report paths and retention bounds remain unchanged.

These are admission controls, not a new snapshot portability claim. The existing upstream loader still validates the snapshot, and no metadata is rewritten, guest state regenerated or guard bypassed. A shared standard-runner label does not guarantee the required vendor allocation: an incompatible allocation fails the workflow and stays unqualified. The [2026-10-25 native repeat](#cpu-admitted-native-repeat-2026-10-25) passed on a compatible AMD fixture/A/B allocation. Ordinary PR controls use synthetic vendor data and a substitute helper; CPU-model/features, physical host loss and broader recovery behavior remain outside their evidence.


#### Worker-process crash before publication

The `worker_crash` workflow input adds a fixed failure case to the existing CPU-admitted three-runner scenario. It defaults to false. After the initial counter value 41 is committed and its scratch reconciled, A starts a separate call-worker process through `cross_crash.py`. That child uses the real admission, checkpoint download and native helper. Its publication transport validates the completed native output 99, exposes a local readiness marker, and pauses before sending any candidate checkpoint or result to the coordinator. A's trusted test driver then forcibly terminates and reaps that child. Owner configuration crosses stdin, and the readiness marker remains local; neither is added to artifact paths.

The coordinator must still name the seed checkpoint and retain the crashed call's scratch receipt. B must be refused both while A remains the recorded owner and after an explicit fence request without confirmation. A coordinator-process restart must preserve the pending fence, unchanged checkpoint and charged receipt. Only a subsequent mutual-TLS controller verification may retire A's generation, reconcile scratch and permit B to acquire a newer generation. B must replay the original result without a helper, refuse the interrupted call identity, and restore 41 before advancing to 42. Successful reports add the worker-crash boundary and five corresponding controls; timeout, premature child exit, stale/invalid readiness or unexpected native output fail the scenario.

Run all participants together from the trusted workflow ref:

```bash
gh workflow run mxc-cross-runner.yml --repo sokolaidev/maf-extensions --ref main -f worker_crash=true
```

This is a call-worker process crash after native capture, with A's machine and trusted driver/controller still available. The native helper has finished by this boundary. It does not simulate an unreachable machine, loss during live guest execution, power loss or destruction of the host journal. Local regression controls use a substitute helper with real subprocess termination, SQLite and mutual TLS. The [2026-10-25 native run](#qualified-worker-process-crash-before-publication-2026-10-25) passed all 13 scenario controls on compatible AMD/Linux execution hosts at merged source `33b9eb7dc4571359f322007fc21de4986a205a12`, including unpublished-state discard, takeover refusal across pending-fence restart, saved-result replay and scratch reconciliation. Whole-machine loss still requires an independent infrastructure termination verifier. Automatic shutdown remains disabled, and neither a worker exit nor a pending fence authorizes takeover by itself.

#### Worker-process loss during guest execution

The opt-in `active_guest_crash` workflow input selects a second fixed failure boundary and cannot be combined with `worker_crash`. Both default to false, preserving the ordinary recovery baseline. A first commits 41 and reconciles its scratch. The separate call worker then uses the existing remote admission, restore and native journal hooks with the native helper's `call-owned` mode. Its fixed program asserts the restored value 41, sets it to 99, prints `MXC_ACTIVE_READY:99`, and loops without returning. This probe uses live stdout because the normal bounded-output mode exposes its payload only after execution; it accepts only the fixed source and never returns a publishable result. It does not change the normal bounded call path.

The trusted driver reads a host-written helper creation identity and at most 1,025 bytes of stdout, refusing output beyond the 1,024-byte readiness allowance. The guest marker synchronizes this fixed test only; it is neither completion nor termination evidence. The driver requires the recorded helper to remain alive at that marker and refuses an existing candidate or native completion report. It kills and reaps the call worker, then requires that same helper identity to stop within five seconds, without first invoking controller termination. The private owner-pipe watcher supplies native retirement. A missing marker, premature helper exit, surviving helper, stale scratch or completion artifact fails qualification. Failure cleanup may terminate the exact recorded helper, but cannot turn the failed observation into a pass.

After helper retirement, the existing coordinator controls still require the committed checkpoint and interrupted scratch charge to survive pending-fence restart. Worker death and the local helper observation do not confirm the distributed fence. Only trusted controller verification and reconciliation permit B's newer generation, saved-result replay, interrupted-call refusal and restoration of 41 to 42. Successful reports contain `boundary: during_guest_execution`, `helper_stopped_after_owner_loss: true` and 14 scenario controls.

Run all participants together from the trusted workflow ref:

```bash
gh workflow run mxc-cross-runner.yml --repo sokolaidev/maf-extensions --ref main -f active_guest_crash=true
```

Ordinary controls retain real worker/helper processes, private pipe EOF, process identities, SQLite and mutual TLS while substituting native guest execution. The [2026-10-28 native run](#qualified-active-guest-worker-loss-2026-10-28) passed all 14 controls on compatible AMD/Linux hosts, including helper retirement after worker loss before controller termination, takeover refusal across pending-fence restart and recovery of committed state. A's machine and trusted controller remained available. Whole-machine loss, coordinator-storage loss, network-authority revocation and production integration remain outside this evidence; automatic shutdown remains disabled.

## Owner-liveness experiment

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

The `call-owned` helper mode requires the eight-byte `MXCOWN1\n` header on stdin before loading a snapshot. The host keeps the private pipe's writer open for the entire native call. EOF, a pipe error or the one-byte cancellation command `C` terminates the native process with exit code 74; an invalid header or command exits with 75. Normal completion retains the existing separate native-written capture report. The guest has no access to this host-side pipe and its output cannot supply ownership commands.

The watcher terminates the process without waiting for guest execution to unwind. Process exit closes the helper's VM handles. A killed owner therefore cannot leave this native process executing indefinitely merely because the Python program has not returned. The compatibility `call` mode remains available for historical experiments; the host publication supervisor uses `call-owned` exclusively. This adds no dependency on the temporary MXC session API outside the existing Rust wrapper.

Ownership loss cannot acknowledge a tool call: the helper cannot publish the database transaction, and its host publishes only after a successful helper exit and capture report. A candidate left by interruption during capture is uncommitted scratch data. This mechanism does not provide distributed fencing, supervision of arbitrary descendants, prompt cleanup under host scheduler starvation, output fidelity, native output-buffer bounds or generic file confinement. Multiple trusted writers holding the pipe open would extend ownership, so the host must not delegate or inherit that writer into other processes.

[owner_probe.py](../../../scripts/experiments/mxc_session_patch/owner_probe.py) uses a separate owner process holding the only writer. After the fixed guest emits its readiness marker and enters a loop, the probe kills the owner or sends a cancellation/invalid command, requires native exit within five seconds, and verifies no candidate or completion report exists. The guest marker synchronizes this fixed test only; it is not production control status. Missing ownership and an invalid header must refuse before attempting to load even an absent snapshot. The normal publication probe independently exercises successful owned calls and saved-result retries.

```text
cargo test --locked --manifest-path <generated-build>/Cargo.toml
python scripts/experiments/mxc_session_patch/owner_probe.py --helper <built-helper> --startup <prepared-agent-snapshot> --state-dir <new-evidence-directory>
```

The retired `mxc_recovery` workflow input ran these controls on the GitHub Linux/KVM runner after native recovery and host publication. Windows/WHP is independently qualified with the same fixed probe. Runtime reports are retained with the exact executable hash; only reports and diagnostics are uploaded, never checkpoints. The [workflow guide](#qualification-workflows-and-evidence-retention) identifies current ownership and recovery checks. Track completion in [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669).

The Windows owner report records all five controls passing. Owner death, cancellation and an invalid command stopped the live helper within 0.18 seconds in this measurement; five seconds is the probe deadline, not a real-time production guarantee.

The Windows publication report also passes the full publication/crash/redelivery probe using `call-owned` and the same helper hash. This covers normal completion and lost acknowledgments alongside the independent live interruption controls.

The [Linux/KVM report](https://github.com/sokolaidev/maf-extensions/actions/runs/37085668203) records all five ownership controls, native state/restart controls, and publication/crash/redelivery controls passing in [run 37085668203](https://github.com/sokolaidev/maf-extensions/actions/runs/37085668203) at `4bead84e554b4553d0c12c0beca69a36a4fb3f5c`. The three live stops were below 0.016 seconds in this measurement. Later commits update only documentation/evidence; these timings are observations, not portable latency guarantees. The unchanged source patch remains replaceable independently of the owner pipe.

## Console and bounded-capture experiments

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

[descriptor_probe.py](../../../scripts/experiments/mxc_session_patch/descriptor_probe.py) tests whether guest descriptor redirection can satisfy [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) without changing the embedded kernel. It uses the existing pinned MXC session patch; `execute-owned` executes once under the private owner pipe without exporting a checkpoint. `call-owned` still tests checkpoint export. The descriptor experiment changes no shipped package or upstream runtime API.

### Result

Independent Windows/WHP and Linux/KVM measurements establish **no-go for guest descriptor redirection alone as a host-enforced output boundary**. [Windows evidence](https://github.com/sokolaidev/maf-extensions/actions/runs/37136213341) and [Linux evidence](https://github.com/sokolaidev/maf-extensions/actions/runs/37136213341) record individual outcomes and executable identities. [GitHub run 37136213341](https://github.com/sokolaidev/maf-extensions/actions/runs/37136213341) passed the complete workflow at source `5b2436f57c672889f50277d30a2083a69735b0c3`, including the existing native recovery, atomic publication and owner-liveness controls. Both retained output measurements use that source. A subsequent review fix moves descriptor and console writes outside assertions so Python optimization cannot remove them; these retained native measurements predate that fix. An ordinary PR check does not execute the opt-in runtime probe.

| Control | Windows/WHP and Linux/KVM observation |
|---|---|
| Console writes of 4095 / 4096 / 4097 bytes | 4095 / 4096 / 4096 bytes arrive, while all three writes and native executions report success |
| Separate pipes with binary and Unicode, concurrent writers, duplicated descriptors, writev and native C write | Exact expected hashes; 20,314 bytes in each stream |
| Exact one-MiB collector limit | Complete retained output |
| One byte beyond the collector limit | Guest collector retains one MiB and flags overflow; native execution still reports success |
| Reopen `/dev/stdout` after redirection | Bytes bypass both pipe collectors and reach the original native console |
| Guest file-size limit | `RLIMIT_FSIZE` refuses resource 1 with EINVAL |
| Native deadline / explicit owner cancellation while writing to pipes | Exit 1 with TimedOut / exit 74; neither produces native success |
| Open redirected file / buffered pipe across process restart | Both preserve the expected before/after binary bytes and descriptor state |

The pipe measurements are observations from fixed cooperative guest programs. The collector deliberately reports its overflow flag rather than raising; this demonstrates that the existing native path does not enforce that flag. A cooperative wrapper could raise, but guest code can bypass or replace that wrapper. Their hashes and counters do not create host authority, enforce a native memory quota, or prevent hostile guest code from replacing a reader or bypassing it. The supervisor independently bounds its retained process output and wall time. Its fallback kill is an inconclusive runtime measurement, never a passing deadline result. The evidence validator requires native completion, expected byte hashes and all counterexamples; tests reject missing controls, corrupted hashes and supervisor fallback.

The checkpoint test preserves open file and buffered pipe descriptors, not actively draining reader threads or arbitrary descendants. No general host file-export mechanism or safe artifact collection is established. The filesystem alternative would also require a quota outside guest control. The experiment fails while querying the file-size limit, before setting it or writing the test file; the pinned kernel's `uk_sys_prlimit64` resource switch also excludes `RLIMIT_FSIZE` for setting and querying. Catching that refusal cannot provide a quota. #1668 remains open.

Local verification passed 13,196 tests with 751 skipped, repository lint/format and package/root type checks, documentation checks, two Rust tests, Clippy with warnings denied, and real helper help/invalid-argument controls. The eight evidence-validator regressions are included in that historical count. Subsequent subprocess regressions check complete descriptor payloads with and without Python optimization and the three console boundary writes under `-O`; Windows substitutes POSIX write calls with `os.write` adapters, while Linux executes the native calls. These checks do not establish arbitrary descendant capture, reader-thread snapshot safety, native output quotas or a production backend.

### Reproduce

Build the helper using the [existing patch instructions](#apply-and-reproduce) and prepare a matching startup snapshot. Use a new evidence directory outside the repository:

```text
python scripts/experiments/mxc_session_patch/descriptor_probe.py --helper <native-helper> --startup <startup-snapshot> --state-dir <new-evidence-directory>
```

The retired `mxc_recovery` dispatcher ran the same probe on GitHub Linux/KVM and retained JSON and bounded diagnostic streams, not snapshot directories. Its reports remain historical evidence; see the [current workflow guide](#qualification-workflows-and-evidence-retention) for maintained qualification entry points. The probe's final assessment distinguishes successful reproduction of a limitation from successful qualification of the requested contract.

### Minimal console reproducer and proposed fix

Execute this fixed guest code with `execute-owned` and a matching startup snapshot, retaining native stdout separately from the native control file:

```python
import os

written = os.write(1, b"X" * 4097)
if written != 4097:
    raise RuntimeError(f"console write returned {written}, expected 4097")
```

The unmodified pinned console emits 4096 `X` bytes and the helper writes `{"executed": true}`. The 4095/4096 positive controls exclude a generic failure to execute or collect output. This reproducer is also run automatically by the descriptor probe.

The proposed upstream correction is to preserve the allocation-free early console but stop acknowledging unsent bytes: loop over bounded chunks or propagate a truthful short write/error through the console layers. A chunking implementation must handle UTF-8 boundaries because the current HostPrint parameter is a string; merely slicing every 4096 bytes risks splitting a code point. Arbitrary binary output and distinct streams require a byte-oriented interface or separately qualified transport. This is a proposed fix, not an implemented or tested kernel patch.

The [source/history research](#native-output-research) explains why the existing console is shared, identifies the newer structured result channel, and compares the remaining implementation choices. The subsequent bounded-capture request is reported in [hyperlight-unikraft #140](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140). No upstream PR has been submitted.

### Bounded native capture overlay

The [accepted design](#bounded-console-capture) sets a **host-configurable 1 MiB combined console budget per call, with truncation and continued execution**. `output-runtime.patch` bounds the Rust HostPrint collector before appending and disables automatic stdout mirroring when bounded capture is selected. The callback retains a valid UTF-8 prefix; if the next character would cross the limit, it omits that character and all subsequent output. Empty output at a zero limit and exact-limit output without omission are not truncated. Omitted bytes are counted with an explicit saturation flag. These counters describe messages received by HostPrint; they cannot detect bytes the kernel already discarded.

`output-session.patch` layers over the unchanged session-preview patch and adds the opt-in `maf-output-preview` feature. Startup/resume has a bounded interval of its own. Execution starts a fresh budget, and the host receives capture metadata separately from guest text. Truncation does not change the session commit barrier, guest-error behavior, deadline or owner cancellation. The `call-bounded` helper writes a bounded text file and a small native control record to host-owned paths. The host validates their sizes and metadata before saving a base64-encoded console payload and truncation metadata in the existing checkpoint/result transaction. The experimental store's independent two-MiB serialized-result limit still applies; setting a larger output budget does not raise that storage limit.

The overlay preserves the default legacy console path for the earlier probes and leaves kernel bytes unchanged. It does not provide arbitrary binary fidelity, separate stdout/stderr, a fix for silent 4096-byte console truncation, process-wide memory containment or safe general file export. #1668 remains open. A host runtime profile includes the helper identity and output policy; this work does not authorize silently changing an existing session's profile.

#### Apply and remove the overlay

Start with the existing session patch applied to its pinned MXC checkout, and a separate clean `hyperlight-unikraft` checkout at `2b4790d1626e2645dd1f00469311424be8cb2f94`. The overlay tool checks both revisions, patch checksums and exact affected-file contents before modifying either checkout. Keep the registry cache unchanged. The generated manifest overrides only `hyperlight-unikraft` with the explicitly selected source; all other dependency versions remain locked.

```text
python scripts/experiments/mxc_session_patch/output_patch.py apply --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/output_patch.py configure --source <mxc-checkout> --runtime <runtime-checkout> --build-dir <new-build-directory>
cargo build --locked --manifest-path <new-build-directory>/Cargo.toml
python scripts/experiments/mxc_session_patch/bounded_probe.py --helper <native-helper> --startup <startup-snapshot> --state-dir <new-evidence-directory>
```

The generated helper enables bounded-output support; its `call-bounded` command defaults to 1,048,576 retained bytes and accepts an optional final byte-limit argument. The durable host harness selects this mode with `--bounded-output`, with an optional `--output-limit`. The original commands preserve the historical probes' behavior.

Stop sessions using the experimental executable before replacing it. Remove this overlay before removing the prerequisite session patch:

```text
python scripts/experiments/mxc_session_patch/output_patch.py remove --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/output_patch.py check --source <mxc-checkout> --runtime <runtime-checkout>
python scripts/experiments/mxc_session_patch/patch.py check --source <mxc-checkout>
```

An upstream replacement must pass the same retained-output, overflow, recovery and metadata validation checks before removing the overlay and selecting the new API behind `probe/backend.rs`. Source removal alone neither revokes built executables nor migrates existing stores.

#### Qualification

`bounded_probe.py` exercises the default limit, exact-limit output, overflow followed by successful state recovery, a custom small limit, zero retention, UTF-8 boundaries, reopening `/dev/stdout`, a lost acknowledgment after publishing a truncated result, guest failure and an output-producing timeout. It rejects supervisor fallback as an inconclusive result. Native collector unit tests additionally exercise sustained over-limit input, metadata-counter saturation, the legacy default, bounded/legacy small-message growth, unlocked mirroring, and capture recovery after a mirror panic. Linux measurements run through the opt-in GitHub KVM job; separate Windows/WHP evidence is required. The results summarized below record successful qualification of this bounded text-capture scope.

The current Windows/WHP report and [Linux/KVM report](https://github.com/sokolaidev/maf-extensions/actions/runs/37159683759) passed at source `9e10213d8ca566848ec8acc9b6ede1bc185514e8`. Retained output hashes and counters match between platforms and match the earlier candidate. [GitHub run 37159683759](https://github.com/sokolaidev/maf-extensions/actions/runs/37159683759) passed the complete workflow, including native recovery, publication, owner-liveness, descriptor controls, bounded-output qualification and overlay removal. Artifact `11286634373` has SHA-256 `5cebe0dd8b6354f51641624b645634921aa1381d0fb95c91e874e871135627c1`. The first Windows attempt on this revision failed during the overflow case when the native process could not allocate 1 GiB; it is not counted as a qualification pass. The retry passed after the full local suite ended, without changing the code or host settings. The earlier candidate at `c279bbe0` also passed both platforms, including [GitHub run 37152285593](https://github.com/sokolaidev/maf-extensions/actions/runs/37152285593).

At `9e10213d`, focused validation passed 52 Python tests, five native collector tests, two helper tests and Clippy with warnings denied. Repository lint/format, package/root type checks, Markdown and authenticated tracker checks passed. GitHub offline suites passed 13,896 tests with 593 skipped, alongside packaging and published-core compatibility checks. The local full suite reported 13,694 passed, 794 skipped and one failure in the unchanged WSLC `-W error` import test; that test and its complete four-test packaging file passed on isolated reruns without changes. The original local full run is not a green gate. This qualifies the tested restored-session profile; independent cold-boot output stress, arbitrary binary fidelity, separate streams and process-wide memory limits remain outside the evidence.

The runtime measurements summarized above predate the collector growth and mirroring review fixes. The revised collector grows bounded capacity geometrically up to the configured limit and preserves the legacy string growth policy. Legacy mirroring runs without the capture lock so a stdout panic cannot poison capture. Review validation is recorded in [#1693](https://github.com/sokolaidev/maf-extensions/pull/1693); the earlier reports do not claim a native run of the revised collector.

## MXC 1.0 and 1.0.1 profile reproduction

Tooling: [scripts/experiments/mxc_v1_patch](../../../scripts/experiments/mxc_v1_patch).

This is the separately pinned candidate for [spike #1780](https://github.com/sokolaidev/maf-extensions/issues/1780). The [research record](#mxc-10-migration-spike) owns the adoption recommendation. The [0.9 experiment](#checkpoint-publication-and-recovery-experiments) and its runtime assets remain available.

`manifest.json` pins MXC 1.0.1, Unikraft 0.17.0, Hyperlight host/common 0.17.0 and each affected file before/after the session, output and storage layers. `rootfs.json` pins the matching Linux/amd64 agent image and extracted initrd. `Cargo.lock` is the candidate dependency graph. There is no package release or production adapter in this experiment.

### Apply and reverse

Use fresh checkouts at the manifest's exact commits. From the repository root, invoke `python -m scripts.experiments.mxc_v1_patch.patch` with the action, `--layer session|output|storage`, and explicit `--source`, `--runtime` and `--host` checkout paths. Apply in that order and remove in reverse order. Every operation validates the entire known file state and patch checksums before changing any checkout; modified prerequisites and mixed layers are refused. Unrelated files are outside this integrity check.

After all layers are applied, use action `configure` with `--build-dir` naming a new directory. The build reuses the existing `probe/main.rs`, `probe/backend.rs` and `probe/owner.rs`, replacing only backend imports and the recorded MXC source identity. The single Rust wrapper uses the consolidated SDK's internal compatibility exports; the stable V1 API does not promise this surface. Build with `cargo +1.98.0 build --locked --manifest-path <build-dir>/Cargo.toml`.

### Hosted qualification

Dispatch `mxc-native.yml` on the candidate branch with `mxc_v1=resolve-lock` to prepare a dependency lock candidate. This mode reports `dependencies-resolved`, never `qualified`, and performs no native qualification. Review and commit the resulting `build/Cargo.lock` before dispatching `mxc_v1=qualify`. Qualification must use the retained lock with `--locked`.

The native mode independently checks KVM or WHP, builds the new helper, runs collector/budget unit tests, prepares the verified rootfs, and runs the existing rich-state, bounded-output, bounded-export and format-4 durability controls. It then builds the old pinned helper and produces an old checkpoint. The new helper must refuse that incompatible checkpoint without changing it; the old helper must still restore it. All layers must be removable afterward. A failed stage retains an `unqualified` result and diagnostics.

Artifacts contain reports, logs and the resolved lockfile for 14 days, excluding guest snapshots and SQLite stores. The historical MXC 1.0.0 results summarized below come from [run 37752153210](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210), source `da064543023d2b95ae06735d2746f520caa4eb61`; later documentation/report commits do not change the tested harness.

| Platform | Migration controls | Rich Python state | Outcome |
|---|---|---|---|
| Linux/KVM | [Report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1) | [Report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1) | Qualified |
| Windows/WHP | [Report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1) | [Report](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210/attempts/1) | Qualified |

Both platforms refused the old `k5e9192dfed5c8dbb-c1` snapshot under the new `k485adb2bab362516-c3` runtime without changing the checkpoint; the old-pin helper then restored its state. Existing persistent sessions require their exact old helper/profile assets. The experiment does not convert guest snapshots or silently substitute fresh state. Process crashes are not physical reboot or power-loss tests, and independently passing on two platforms does not establish checkpoint portability between them.

### MXC 1.0.1 refresh

The active SDK pin is release `v1.0.1`, commit `fda29ac63bc306b7f97f3b86b4b63d44f7f40cf2`. The runtime, Hyperlight host/common, rootfs and downstream byte-stream/file kernel pins stay unchanged. The [release analysis](#mxc-101-release-analysis) describes the source changes and remaining limits. All temporary layers remain necessary; their apply/remove checks cover the updated SDK. The historical results above qualify their recorded 1.0.0 sources only. [Run 40082577780](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780), source `7545863209d86738bf4f37ae2ed788675e552734`, qualified the updated SDK with the retained lock on both platforms. Later report/documentation commits do not change the tested harness.

| Platform | Session and migration | Rich state | Byte streams | Files and format-7 recovery | Volume cleanup |
|---|---|---|---|---|---|
| Linux/KVM | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Passed](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Detached and removed](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) |
| Windows/WHP | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Passed](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) | [Detached and removed](https://github.com/sokolaidev/maf-extensions/actions/runs/40082577780/attempts/1) |

Each platform passed 23 byte-stream scenarios, 24 file/control cases, durable replay/crash controls, format-7 pre-launch checkpoint-copy recovery, physical-storage checks, deletion and idle lifecycle controls. Migration also passed mutual-TLS native publication/restore/replay and verified local cleanup; the incompatible 0.9 checkpoint stayed unchanged and recovered with its old helper. These are independent same-machine controls on each platform, not a fresh cross-runner qualification or an in-place 1.0.0-to-1.0.1 session migration.

## MXC 1.1 profile reproduction

Tooling: [scripts/experiments/mxc_v11_patch](../../../scripts/experiments/mxc_v11_patch).

Generated MXC and Unikraft output reports are not committed. Keep local reports outside the checkout; hosted runs upload them as Actions artifacts subject to workflow retention. The documentation preserves prose findings and run/source identities, but historical artifacts may have expired. JSON source pins and patch manifests remain versioned inputs.

This separately pinned candidate implements [#2761](https://github.com/sokolaidev/maf-extensions/issues/2761). The existing [1.0.1 experiment](#mxc-10-and-101-profile-reproduction) and its runtime assets remain available for old-session recovery. This experiment does not release a package or migrate existing checkpoints.

`manifest.json` pins MXC 1.1.0, Hyperlight-Unikraft 0.21.0 and Hyperlight host/common 0.19.0. `rootfs.json` pins the Linux/amd64 agent image's index, manifest, layer and extracted initrd. Each patch records affected-file hashes before and after application. The [release analysis](#mxc-110-upgrade-candidate) records the adoption boundary.

### Reversible layers

Use fresh checkouts at the manifest's exact commits. Run `python -m scripts.experiments.mxc_v11_patch.patch apply --layer session|output|storage --source <mxc> --runtime <unikraft> --host <hyperlight>`, in that order. Use `remove` in reverse order. `configure` takes the same paths and a fresh `--build-dir` and produces the helper build with the retained dependency lock.

- Session continuity remains an opt-in SDK patch; MXC's ordinary Hyperlight runner still rewinds between calls.
- Output uses upstream `output_limit`, `echo_output(false)` and `take_output`. The old capture implementation is removed from this candidate. A small runtime patch preserves saturation reporting for omitted-byte counts; the adapter maps upstream field names to the existing helper protocol. Upstream separately captures stderr, so the configured limit applies to each capture, not their aggregate allocation.
- Bounded storage charges all snapshot memory blobs, the new transport blob, configuration, manifest, layout and index before writing. A refused or partial checkpoint never becomes a successful publication.

The host journal, freshness protection, replay, ownership and mutual-TLS transfer controls are reused. Hyperlight networking remains closed. The byte-stream/file custom kernels and their existing qualification stay on 1.0.1; this candidate does not claim those kernels are compatible with 0.21.

### GitHub runner qualification

Dispatch `mxc-native.yml` with `mxc_v11=resolve-lock` first. This only resolves dependencies and reports `dependencies-resolved`. Review and retain the produced `build/Cargo.lock`, then dispatch `mxc_v11=qualify`; native builds use `--locked`.

[Resolution run 43769791355](https://github.com/sokolaidev/maf-extensions/actions/runs/43769791355) independently produced the same lock on Linux and Windows: SHA-256 `ff7ed2c0e6a093f99ea7cce17f7cf1b01ba89ec91ac3d1584a2b603f23d2ceb2`. An OSV check on 2026-11-15 at 22:34 UTC found no known advisories for its 289 registry crates. Refreshing the inherited graph replaced affected `libz-sys`, `rustix` and `tokio` versions with 1.1.33, 1.1.6 and 1.54.1 respectively. This check excludes path patches, system libraries, kernel and rootfs; it is not a whole-image security assessment.

Qualification runs independently on Linux/KVM and Windows/WHP: helper build, overflow/budget tests, rich Python state, bounded output and exports, durable replay/crash controls, and mutual-TLS checkpoint publication/restore/replay. It builds the retained 1.0.1 helper to produce an old checkpoint, requires the new helper to refuse it unchanged, and requires the old helper to restore it. Finally, every patch is removed and checked. Reports retain source, helper, dependency and image identities; failure remains `unqualified`.

Cross-runner checkpoint portability, physical reboot/power loss, byte-stream/file kernel migration and production deployment are separate acceptance boundaries. Existing sessions require their exact original helper/profile assets. A rejected old checkpoint must never be replaced silently with fresh state.

### Qualification summary

[Run 43770746800](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800), source `a57e40029915e4aae92defdad75b597b04268cc5`, qualified both platforms with the retained lock. Subsequent documentation and report-removal commits do not change the tested native harness.

| Platform | Session, output, storage and recovery | Rich Python state | Exact export |
|---|---|---|---|
| Linux/KVM | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) | [Passed](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) | 894,126,477 bytes / 8 files |
| Windows/WHP | [Qualified](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) | [Passed](https://github.com/sokolaidev/maf-extensions/actions/runs/43770746800/attempts/1) | 894,384,576 bytes / 8 files |

Both platforms passed stdout/stderr flushing without newlines, split UTF-8 writes, closed IPv4/IPv6 TCP/UDP socket denial, bounded-output floods and durable truncation metadata. Exact export budgets passed; one-byte and one-file deficits refused without exceeding their budgets. Restart/crash and lost-ack controls passed, as did mutual-TLS native checkpoint publication, restore, replay and verified local cleanup. Each new helper refused an incompatible 1.0.1 checkpoint without changing it, and its old-pin helper restored the same checkpoint afterward. All layers were removed successfully.

The candidate is suitable for new closed-network rich-Python session experiments under this qualified profile. Existing sessions and custom byte-stream/file profiles retain their original assets. Independent qualification on each platform does not establish checkpoint portability between them.

## Byte-stream profile reproduction

Tooling: [scripts/experiments/mxc_streams_patch](../../../scripts/experiments/mxc_streams_patch).

This profile extends the [MXC 1.0 experiment](#mxc-10-and-101-profile-reproduction) with distinct stdout/stderr byte channels. It is experimental qualification tooling, not a shipped sandbox backend. The [design](#separate-byte-stream-profile) records the accepted policy and the [research](#native-output-research) records candidate-specific evidence.

Each stream retains at most 1,048,576 bytes per call. Further bytes are acknowledged to the guest, discarded by the host collector, and counted with an explicit saturation flag. Prefixes may end inside a UTF-8 sequence; consumers choose decoding policy after receiving bytes. An exact-limit write is not truncated. Malformed stream IDs or chunks larger than 4096 bytes latch a transport fault and prevent successful checkpoint publication. No ordering across the two streams is promised.

The native helper writes two bounded byte files and a separate control record of at most 1024 bytes. The parent rejects duplicate keys, unexpected fields, invalid types, counters, limits and payload lengths. Guest output is never parsed as native completion. Failed execution may retain diagnostic byte files but produces no successful control record. The format-4 supervisor reserves three MiB for the encoded result and accounts for payload, diagnostic, readiness and control files separately from checkpoint capacity. The existing two-MiB result default remains unchanged.

### Run on GitHub

Dispatch `mxc-native.yml` at the candidate branch with `mxc_streams=true`. The Linux kernel job builds from `kernel.json` source pins, applies `kernel.patch`, records the observed builder/toolchain identity, and reverses the patch. The two native jobs download that same run's kernel artifact, verify its hash and source metadata, then build the helper with the retained MXC 1.0 dependency lock. Linux requires KVM; Windows requires WHP. Setting only `mxc_stream_kernel=true` builds the kernel without qualifying execution.

A native job reports `qualified` only after byte probes, the Python state/restart controls, the format-4 crash matrix and overlay removal all pass. The crash matrix carries two full retained byte streams, checks omission counters and result replay, and reconciles SQLite charges. Failure leaves an `unqualified` report and logs. Compilation or a successful job whose opt-in steps were skipped does not qualify this profile.

### Remove the overlays

`kernel_patch` manages an explicit runtime checkout with initialized, pinned submodules. `patch` manages explicit MXC, runtime and Hyperlight-host checkouts with the three MXC 1.0 experiment layers already applied. `configure` first creates a fresh helper build from that baseline, then applies the stream layer and installs the backend wrapper. Both tools check source commits, affected files and patch hashes before mutation.

```bash
python -m scripts.experiments.mxc_streams_patch.kernel_patch remove --source RUNTIME_SOURCE
python -m scripts.experiments.mxc_streams_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
```

Reverse the underlying MXC 1.0 layers afterward, in storage/output/session order. The qualification runner also restores the original embedded kernel file before reversing the Rust layers. A manually configured build must restore that saved kernel too. Source removal does not change existing executables or migrate checkpoints; keep the original runtime available for its saved sessions. Replace this profile with upstream support only after rerunning the same behavioral qualification.

## Bounded-file profile reproduction

Tooling: [scripts/experiments/mxc_files_patch](../../../scripts/experiments/mxc_files_patch).

This profile adds bounded input files, a virtual workspace and named artifact delivery to the [separate-stream experiment](#byte-stream-profile-reproduction). It is repository qualification tooling, not a production backend. The [design](#runtime-and-files) records the selected policy; the [research](#native-file-candidate) records implementation boundaries and candidate-specific evidence.

### File contract

`Request` accepts immutable input bytes, portable relative names, host-selected call/session lifetimes and explicit replacement authority. Each direction defaults to 64 files and 16 MiB, including a 16 MiB per-file ceiling. The live workspace defaults to 256 files and 64 MiB. All selected bounds enter retry identity. The host may configure limits within the experiment's fixed ceilings.

The helper exposes `guest_call_path` and `guest_session_path` and starts code in the call directory. Input files are writable. Session inputs use the session directory; other inputs and requested artifacts use the call directory. An upload replacing a retained logical filename needs exact spelling and explicit authority, including when the new upload is call-scoped. Files keep their lifetime when renamed. A guest-created copy is independent state.

Guest paths name entries in a virtual service, never host paths. The service refuses links, special files and growth beyond its logical limits. Whole writes are staged before mutation; capacity refusal is a catchable filesystem error. Earlier successful operations, including an explicit truncate, are not rolled back by a later write error. Directory metadata is separately bounded. The service permits one staged write at a time; other mutations return `EBUSY` until it commits or aborts. Permission bits are synthesized; `chmod` does not persist permission changes. The upstream path-based ABI does not promise full POSIX inode behavior for retained descriptors after unlink/recreation or every directory rename.

The helper validates all artifacts while the guest is stopped. It copies their bytes, reclaims call files, and captures retained workspace files alongside VM state. The format-4 store publishes that checkpoint and the result envelope atomically before acknowledgment. Matching retries return identical artifact bytes without a helper; failure preserves the previous committed state. Workspace limits do not bound all guest-private filesystems or total process memory.

### Session deletion

The experimental shared-call supervisors accept a host-owned `threading.Event` through `delete=`. The owner thread observes it before admission, during native execution, before publication and before acknowledgment. Observation durably retires the session before interrupting execution. A request committed after the call result preserves that result for retry; deletion never changes the selected retry window. The event is in-process notification, not a durable cross-process request queue: a request lost before retirement must be resubmitted by the host.

`NativeJournal.delete()` resumes deletion while holding the session ownership lock, including after restart. It stops a recorded helper through a creation-checked Windows process handle or Linux pidfd, waits for confirmed termination, and then reclaims its private scratch through the existing cleanup journal. A missing/corrupt identity, unavailable process evidence, unknown scratch entry or failed termination keeps unresolved capacity charged. This method does not terminate arbitrary descendants or fence another machine. Checkpoint collection remains a separate bounded operation; completed results and call identities survive it. These are experimental host controls, not a production backend API. The [qualified deletion record](#qualified-active-deletion-candidate) retains the Linux/KVM and Windows/WHP reports for active deletion and seven supervisor-crash boundaries.

### Optional idle expiry

New shared-store sessions may set `idle_policy=Policy(timeout_seconds, grace_seconds=300)`; omitting it keeps idle expiry disabled. Running calls and unresolved cleanup remain protected. The interval starts at creation or completed cleanup, and reopening or replaying a result does not reset it. The host schedules `NativeJournal.expire_idle()` under session ownership; admission and restore also check expiry. Idle retirement preserves retained result/artifact bytes and call identity. The [store contract](#optional-persistent-session-idle-expiry) defines persistent deadlines, finite clock forgiveness, format-5 compatibility and recovery boundaries. The [qualified idle record](#qualified-idle-expiry-candidate) retains candidate-specific Linux/KVM and Windows/WHP evidence using controlled host time.

### Run on GitHub

Dispatch `mxc-native.yml` at the candidate branch with `mxc_files=true`. The Linux job builds the pinned combined kernel and retains its source, builder and toolchain identity. Linux/KVM and Windows/WHP jobs consume the same kernel artifact, build the helper with the retained dependency lock, and run native file and recovery probes. No local Rust build is required. The [qualified candidate record](#qualified-file-candidate) links the retained reports and [example chart](../../../scripts/experiments/mxc_files_patch/example-chart.png).

A native job reports `qualified` only after file probes, Python-state controls, the eight-boundary crash matrix, failed-artifact recovery, active deletion/restart controls, idle-expiry controls and overlay removal pass. The artifact includes bounded failure diagnostics and the collected CSV chart. Compilation or a successful job with opt-in steps skipped is not native qualification.

### Remove the profile

The file `patch` tool requires the three MXC 1.0 layers and the stream layer. Its `configure` action creates a fresh helper from the MXC 1.0 baseline and installs both stream and file layers. Every mutation checks pinned commits, patch hashes and affected source fingerprints.

Restore the runtime checkout's saved original embedded kernel before removing its Rust layers. For the separate kernel-source checkout and native-source checkouts:

```bash
python -m scripts.experiments.mxc_files_patch.kernel_patch remove --source KERNEL_RUNTIME_SOURCE
python -m scripts.experiments.mxc_files_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
python -m scripts.experiments.mxc_streams_patch.patch remove --source MXC_SOURCE --runtime RUNTIME_SOURCE --host HOST_SOURCE
```

Remove the underlying MXC 1.0 layers in storage/output/session order. The qualification runner performs this sequence and checks removal. Removing source patches does not change existing executables or migrate saved sessions. Replacing the profile with upstream support requires equivalent behavioral qualification and an explicit checkpoint-compatibility decision.

## Upstream Unikraft comparison

Tooling: [scripts/experiments/unikraft_output](../../../scripts/experiments/unikraft_output).

This experiment tracks [#1948](https://github.com/sokolaidev/maf-extensions/issues/1948). It compares upstream commit `cf458a07ed847f06d78284998a5beb13dc9fb426` with the existing MXC native-output contract. It does not modify the qualified MXC profiles or install an upstream runtime into a persistent store.

The workflow builds one Python rootfs from the pinned upstream driver and passes its exact bytes to both native jobs. Its build report records the CPIO hash and observed Docker image identities. The Dockerfile still installs unversioned Debian packages; a later build is not promised to produce identical bytes. The embedded kernel and Cargo dependency lock come from the pinned upstream commit.

Each native job checks source and rootfs identity, runs five upstream collector tests, three upstream native output-limit tests, the upstream echo test, and seven independent comparison tests. Missing or ignored tests refuse qualification. The report records kernel, rootfs, dependency lock, probe and native comparison executable hashes. Logs and reports are uploaded even on failure. Guest networking remains disabled by the upstream builder default.

The comparison tests intentionally establish whether upstream matches the existing contract. A successful experiment may establish a mismatch: stderr consumes the combined output budget, draining combined output clears stderr metadata, arbitrary binary output is rejected, and reopening stderr fails. The diagnostic probes assert these observed gaps rather than treating them as supported behavior. Such a result is recorded as `measured` with `defer-overlay-removal`, never as a qualified MXC integration. The guest-control-text probe checks that text is returned as data; it does not exercise our durable publication protocol.

The historical **Upstream Unikraft output qualification** entry point is archived as [workflow.yml](../../../scripts/experiments/unikraft_output/workflow.yml), outside the active Actions directory. Its reports and comparison tooling remain available. For a deliberate reproduction, restore that file to `.github/workflows/unikraft-output.yml` through a reviewed pull request, then dispatch it once the definition is available on the default branch; the archive is not an active workflow. Integrated replacement qualification remains tracked by [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957). The workflow supplies a fresh pinned checkout, Rust 1.98.0, the matching rootfs and a build report from that same run and attempt. Its native step runs `python scripts/experiments/unikraft_output/qualify.py --source upstream --output evidence`; this runner intentionally requires the producing workflow identity. Existing test files or modified tracked upstream source are refused. The temporary integration test is removed after execution.

The [research record](#native-output-research) owns the adoption decision. Migrating retained profiles, MXC durable publication/replay and the complete timeout/cancellation/owner-loss matrix require an integrated migration candidate; the standalone upstream tests do not establish them.

### Retained measurements

[Run 38652752789](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789) tested source `9c61b9e8520dfa839596e07dcf29e293108125f4`. The [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38652752789/attempts/1) reports match on source, kernel, rootfs, dependency lock, probe and observations. Each completed nine upstream controls and seven comparison/diagnostic controls. The result is **defer overlay removal**: binary data is rejected, reopened stderr returns EINVAL in the tested image, and combined capture is not an independent stdout budget.

The initial [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008/attempts/1) reports preserve the two failed comparisons from [run 38651934008](https://github.com/sokolaidev/maf-extensions/actions/runs/38651934008). Subsequent diagnostic tests characterize those failures; they do not turn unsupported behavior into a qualification pass. Later report/documentation commits and the switch to manual workflow dispatch do not change the tested native harness.

### Adoption boundary assessment

The workflow also runs `python -m scripts.experiments.unikraft_output.adoption --source upstream --output evidence --root retained-mxc`. It requires the upstream measurements from the same workflow run, attempt and source commit. It builds the unchanged, pinned MXC 1.0 session/output/storage helper and produces a rich-state checkpoint with the original native probe. The upstream runtime must refuse that actual checkpoint, every checkpoint file must remain unchanged, and the retained helper must recover the saved Python state afterward. Reports retain both source graphs, kernel/rootfs/helper identities, native observations and checkpoint hashes; uploaded evidence excludes checkpoint payloads and stores.

Two additional native controls assess the session and result boundaries. A guest exception after printing completion-like JSON is still a native error, but the upstream API permits continued execution and snapshot capture afterward. The session overlay therefore still owns retirement after failure. A one-MiB capture ending inside a UTF-8 character retains fewer than one MiB while reporting omitted bytes; that cannot satisfy the byte-stream prefix metadata contract.

The host publication control supplies this measured metadata and forged completion text to the existing strict result reader. It verifies refusal before publication, preserved checkpoint bytes, identical saved-result replay after reopening the store, and refusal to execute interrupted call identities again. Its checkpoints are explicitly synthetic host fixtures: these controls do not qualify native publication on the new runtime. The migrated-helper crash, timeout, cancellation and owner-loss matrices remain prerequisites for any future overlay removal.

The initial [Linux assessment](https://github.com/sokolaidev/maf-extensions/actions/runs/38811214907/attempts/1) completed in [run 38811214907](https://github.com/sokolaidev/maf-extensions/actions/runs/38811214907), source `7bd8c13403522ba98412a9d064d2f1468a675c3f`. The [Windows assessment](https://github.com/sokolaidev/maf-extensions/actions/runs/38811214907/attempts/1) passed the native checkpoint and capture probes but refused the host publication fixture because its directory owner differed from the runner user. Its overall status remains `unqualified`. The corresponding [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38811214907/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38811214907/attempts/1) upstream reports were uploaded as workflow artifacts.

The second [Windows assessment](https://github.com/sokolaidev/maf-extensions/actions/runs/38813712529/attempts/1) and its [upstream report](https://github.com/sokolaidev/maf-extensions/actions/runs/38813712529/attempts/1), from [run 38813712529](https://github.com/sokolaidev/maf-extensions/actions/runs/38813712529) at `5dad40064dccc995fd88a94e419906ab73b03eb1`, retain a separate DACL refusal after the native probes passed. The fast host preflight in [run 38815295994](https://github.com/sokolaidev/maf-extensions/actions/runs/38815295994) subsequently stopped before Windows native compilation because the initializer file's owner differed. These failures do not count as successful assessments.

The fixture now provisions the fresh store directory and its empty database, initializer and session-lock files with the runner user's ownership and a supported owner-rights DACL before opening the store. The existing access validator remains authoritative, and an existing fixture root is refused before any permissions change. Integrated adoption remains tracked by [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957).

The final [Linux/KVM assessment](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) and [Windows/WHP assessment](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) both completed in [run 38816373753](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753), source `ada309adc98d3a8f953f303e89a94ad6c0f94e81`. Their native observations and host publication results match. The corresponding [Linux](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) and [Windows](https://github.com/sokolaidev/maf-extensions/actions/runs/38816373753/attempts/1) upstream reports were uploaded as workflow artifacts and bound by SHA-256 in each assessment. Both report `measured` and `defer-overlay-removal`. Later report and documentation commits do not change the tested executable harness.

## Qualification workflows and evidence retention

Tooling: [scripts/experiments/mxc_session_patch](../../../scripts/experiments/mxc_session_patch).

Generated MXC and Unikraft output reports are not committed. Keep local reports outside the checkout; hosted runs upload them as Actions artifacts subject to workflow retention. The documentation preserves prose findings and run/source identities, but historical artifacts may have expired. JSON source pins and patch manifests remain versioned inputs.

The MXC experiments are source-only qualification tooling that imports the [host runtime package](../../../packages/maf-sandbox-mxc/README.md). Native workflow commands install that package explicitly; local qualification requires a workspace sync or an installed wheel. Moving a dispatcher does not qualify a new runtime, convert retained checkpoints or establish production readiness. Existing candidate-specific reports and run links remain the evidence.

| Entry point | Purpose | Trigger |
| --- | --- | --- |
| [mxc-native.yml](../../../.github/workflows/mxc-native.yml) | Current native session/recovery, byte streams and bounded files on Linux/KVM and Windows/WHP | Manual |
| [openclaw-mxc.yml](../../../.github/workflows/openclaw-mxc.yml) | Official OpenClaw MXC host preparation and smoke comparison | Manual |
| [mxc-accounting.yml](../../../.github/workflows/mxc-accounting.yml) | Admission scaling with 0, 1,000 and 10,000 retained identities | Manual |
| [mxc-physical.yml](../../../.github/workflows/mxc-physical.yml) | Storage, accounting, retention and process controls on Linux and Windows | Relevant pull requests or manual |
| [mxc-remote.yml](../../../.github/workflows/mxc-remote.yml) | Ownership and 1 GiB checkpoint transfer on Linux and Windows | Relevant pull requests or manual |
| [mxc-volume.yml](../../../.github/workflows/mxc-volume.yml) | Real disposable filesystem exhaustion and recovery | Relevant pull requests or manual |
| [mxc-cross-runner.yml](../../../.github/workflows/mxc-cross-runner.yml) | Independent Linux host recovery with mutual TLS and fencing | Manual |

Dispatch `mxc-native.yml` with `mxc_v1=qualify`, `mxc_streams=true`, `mxc_files=true` and optionally `mxc_prelaunch=true` for the current native profiles. The stream and file jobs retain their separate kernel builds and consume artifacts from their own workflow run. Use `mxc_v1=resolve-lock` only to resolve dependencies; it does not qualify native execution. Input names are retained for callers, while the session job's display name no longer names a fixed MXC version. Dispatch `openclaw-mxc.yml` with `openclaw_mxc=true` and `openclaw_mxc_phase=smoke` or `prerequisites`.

### Retired entry points

The pre-1.0 `mxc-recovery` and `mxc-durability` jobs and inputs are removed from `tests.yml`. The current session runner executes the same `storage_probe` and `durability_probe` modules and additionally checks bounded output, remote publication/replay and incompatible-checkpoint refusal with recovery by the retained helper. The old helper and probes remain available for historical reproduction and compatibility tests; report retention follows the policy above. Removing the old dispatchers does not promise that a current profile can restore a legacy checkpoint.

The historical [Unikraft comparison workflow](../../../scripts/experiments/unikraft_output/workflow.yml) is archived outside `.github/workflows/`. Its [reproduction instructions and measurements](#upstream-unikraft-comparison) remain available. The outcome is still to defer overlay removal; [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957) requires an equivalent integrated candidate.

The ordinary package gate no longer schedules empty MXC qualification jobs. Its required aggregate still waits for every remaining validation shard. Offline MXC tests continue to run in the repository suite.

### Actions history retention

Prune only completed pull-request runs older than 30 days from the accounting, physical, remote and volume workflows, and only after verifying that the associated pull request is closed or merged. Keep runs referenced by research, reports, issue or review evidence; keep the latest successful evidence for each active platform/profile and all unresolved diagnostic failures. An empty or ambiguous pull-request association is not proof of eligibility. Keep manual cross-runner and upstream comparison runs. Do not bulk-delete shared `tests.yml` runs or rewrite Git history.

Before each cleanup, produce an explicit run-ID inventory and protect evidence references before deleting anything. Deleting a run also deletes its remaining artifacts. Most experiment report artifacts already expire after 14 days; run-history deletion chiefly reduces Actions clutter once those artifacts expire. This policy is a maintenance procedure, not a scheduled deletion job.

The 2026-10-27 audit queried the four eligible workflows for pull-request runs created before 2026-09-27 UTC. All four returned zero candidates, so no runs or artifacts were deleted. Historical native and diagnostic evidence remains retained under its existing GitHub retention settings.
