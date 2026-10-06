# MXC Hyperlight design decisions

The selected design uses MXC Hyperlight to supply a rich Python runtime for CodeAct through the sandbox router. This document records the first-version scope selected on 2026-10-02. The [status table](#status) distinguishes those requirements from implementation and validation. The [research record](../research/mxc-backend.md) contains the release assessment, rationale, candidate transport and feasibility questions.

## Agreed scope

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

## Runtime and files

The CodeAct runtime contract describes the verified Python version, available imports, filesystem access, network policy and state lifetime. A runtime's name alone does not establish support for a library or operation. Dependencies come from a prepared, pinned environment; the network option does not authorize package installation during a tool call.

The application supplies selected input files and receives bounded artifacts through the existing file-store and output-sink contracts. Each session has a private workspace. The adapter enforces file count and byte limits, confined access and safe collection; a host mount alone does not establish those guarantees. Temporary call files and retained session files have distinct lifetimes, including when Python objects retain paths to them.

Acceptance includes both a NumPy/pandas calculation returning text and a workflow that reads a supplied CSV, analyzes it with pandas and returns a matplotlib chart. Both workflows pass on Windows and Linux. No speed, memory or cost advantage is assumed.

## Bounded console capture

The temporary native collector uses **truncate and continue** with a host-configurable default of **1 MiB (1,048,576 bytes) of combined console output per call**. It retains a valid UTF-8 prefix, discards subsequent text and reports omitted bytes with an explicit counter-saturation flag. Reaching the limit without omitting bytes is not truncation. Startup/resume has a separate bounded interval; each call starts with a fresh budget.

Truncation does not fail execution or retire the session. A call that otherwise succeeds publishes its checkpoint, retained output and host-owned truncation metadata together before acknowledgment, and recovery redelivers the same result. Guest errors, timeouts, cancellation and owner loss keep their existing failure behavior and cannot publish success.

The collector checks the budget before buffering and disables automatic mirroring to process stdout. Its separately pinned runtime/session overlays are removable through the [experiment tooling](../../../scripts/experiments/mxc_session_patch/OUTPUT.md#apply-and-remove-the-overlay). The combined text channel does not provide separate streams, arbitrary binary fidelity, correction of the kernel's silent 4096-byte truncation, or a process-wide memory limit. Omission counters describe only bytes received by HostPrint. The existing two-MiB serialized-result limit still applies independently of the configured console budget.

Qualification covers exact and exceeded budgets, continued execution, UTF-8 boundaries, durable publication and lost acknowledgments. Failed execution and malformed control refuse success. Replacing the overlay requires equivalent behavior and renewed qualification on both platforms. The [upstream capture request](https://github.com/hyperlight-dev/hyperlight-unikraft/issues/140) includes a contribution offer; this accepted collector policy does not authorize the separate kernel proposal in the [historical research record](../research/mxc-native-output.md).

## State and ownership

Fresh mode creates call-scoped state and disposes it after result delivery. Persistent mode retains Python variables and dataframes across calls within the same trusted conversation, while separating scope, agent and kind. Access to a persistent session is serialized through execution, artifact handling and checkpoint commitment.

Persistence is controlled by the host, not a model-selected tool argument. Host isolation and cleanup requirements remain authoritative: a host requiring call isolation cannot be served a conversation-scoped session. A change in runtime, state mode or execution policy requires a new instance or an explicitly validated transition.

Persistent cleanup preserves the intended interpreter state while reclaiming temporary call resources. Resetting a baseline is not equivalent to retaining Python state. Background guest threads or processes cannot be allowed to race artifact collection or checkpoint capture merely because calls are serialized.

## Durable completion and recovery

Each successful call in persistent mode produces a durable checkpoint before success is acknowledged. The checkpoint represents consistent Python state, session files and the metadata needed to recover the session and identify the completed call. A partially written checkpoint never replaces the previous committed checkpoint.

If Python finishes but checkpoint commitment fails, the call is not reported as a durable success. The previous checkpoint remains available, and later calls cannot silently continue from uncommitted state. If commitment succeeds but acknowledgment is lost, recovery distinguishes delivering the recorded result from executing the program again. Artifact delivery follows the same call identity so retries do not silently duplicate outputs.

Recovery restores a committed checkpoint rather than silently creating an empty interpreter. Corrupt, incomplete or incompatible checkpoints cause an explicit failure. A failed or interrupted call reports the applicable recovery point; its code is not automatically replayed. Checkpoint recovery does not undo external effects or make network requests exactly-once.

Checkpoint metadata binds trusted ownership, physical generation, runtime and guest artifacts, architecture, policy and format compatibility. The receiving host checks its current policy before execution. Checkpoint storage and recovery credentials remain outside the guest. Checkpoint access and retention reflect the sensitivity of the state they contain.

The existing `SNAPSHOT` capability promises reset to a pre-input baseline. It is not, by itself, a durable session-checkpoint API. Capturing and restoring a modified interpreter, including the supported treatment of Python objects, open files, threads and sockets, requires separate qualification. Preserving files alone does not fulfill interpreter recovery.

## Session lifetime

Persistent sessions have no automatic idle expiry by default. The host can delete a session explicitly or configure an idle timeout. Storage quotas still apply while a session is retained; disabling idle expiry does not authorize unbounded storage. Session idle expiry cannot shorten an already committed result-retention promise.

### Session deletion

Deleting a session retires its identity, refuses new execution and stops active execution before reclaiming the checkpoint. Retirement is durable: the same session identity cannot later create fresh state. Cleanup remains incomplete while execution termination or safe reclamation is unresolved, and outstanding reservations remain charged until their data are accounted for or reclaimed.

Completed results and delivery artifacts remain available for matching retries through their promised expiry, including any applicable clock-forgiveness allowance. Checkpoint reclamation removes only data no longer referenced by retained deliveries or active readers. Retained results and identity records remain charged to the retired session and the shared store; deletion cannot bypass either quota or make a completed call eligible for execution again.

## Result retention

Completed call results and their delivery artifacts remain available for retry for 24 hours from durable commit by default. The host configures the retention window; each committed call records its expiry so later configuration changes cannot shorten an existing promise. Reading or retrying a result does not extend its expiry.

Within the retention window, a matching retry receives the identical committed result and artifacts without executing the program again. After expiry, a retry receives an explicit `result_expired` outcome. The store retains enough durable call identity to distinguish an expired call from a new one; removing a result cannot make its call eligible for execution again.

Storage pressure refuses new work rather than evicting results or artifacts before their promised expiry. Result retention is separate from checkpoint retention: the latest committed checkpoint remains available while the session is recoverable. Superseded checkpoints and chunks can be collected only when no live state, pending publication or retained result/artifact references them. A 24-hour result window does not require retaining 24 hours of complete VM checkpoints.

### Clock forgiveness

Each completed result has a grace budget of five minutes by default for clock uncertainty, configurable by the host. The budget caps the extra retention granted because the host clock is uncertain. While expiry is deferred within that budget, matching retries continue to receive the saved result and artifacts. Once the budget is exhausted, expiry proceeds using the host clock; uncertainty does not indefinitely suspend expiry cleanup.

The configured allowance and consumed budget are durably associated with the result. Retries, restarts and repeated clock anomalies do not replenish it. The base expiry remains fixed, and the latest checkpoint's recoverability remains independent of result expiry. The local implementation uses the accounting procedure below; its crash behavior requires qualification before use.

The retry window is evaluated under this bounded clock-forgiveness policy. A sufficiently incorrect host clock can shorten the actual elapsed retry window; the policy does not guarantee 24 hours of real elapsed retention under arbitrary clock failure. Storage pressure still cannot shorten the policy's retention period, and expired call identity remains protected against re-execution.

### Durable clock-budget accounting

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

## Storage quotas

Durable session storage has both a per-session quota and an aggregate quota across the shared store. Both limits apply: a single session cannot consume the entire store allowance, and individually compliant sessions cannot collectively exceed the store allowance. Exhausting either quota refuses new work without shortening existing result-retention promises.

Enabling persistence requires the host to configure both quota values explicitly. There are no built-in quota defaults and no implicit unlimited mode. Missing or invalid quota configuration refuses persistent-session admission; it does not silently fall back to fresh state.

Both quotas measure logical storage usage: uncompressed retained checkpoint, result and artifact bytes, bounded metadata charges, and outstanding reservations. Compression and deduplication reduce physical storage without increasing admission capacity. Each retained object is charged in full to its owning session; artifact bytes embedded in a result are counted there once. Expired-call identity records remain charged after their payloads are collected.

Physical disk usage has separate safeguards covering restore copies, candidate exports, database pages and journals, compression overhead, diagnostics and cleanup. Temporary storage and concurrency are bounded independently; logical quota availability is not a promise of available filesystem space. Disk exhaustion refuses durable success and preserves the previous committed recovery point. Logical collection does not by itself establish physical disk reclamation.

Before executing a new persistent call, the store atomically reserves capacity against both quotas for the maximum permitted checkpoint and result, including retained delivery artifacts. The previous committed checkpoint, unexpired results and existing reservations remain charged during admission. If either quota cannot accommodate the reservation, the call is refused before guest execution; replaying an existing committed result does not require a new execution reservation.

The reservation remains in force through checkpoint preparation and atomic publication. Publication converts reserved capacity into retained usage and releases the unused remainder; a failed or interrupted call releases capacity only after its stored and temporary data are accounted for or reclaimed. This prevents quota exhaustion caused by competing admissions, but does not guarantee filesystem capacity or eliminate storage failures. Failed publication preserves the previous committed recovery point and cannot acknowledge durable success.

The [storage retention proposal](../research/mxc-backend.md#storage-retention-follow-up-proposal) records the current store gaps, proposed transaction boundary and qualification sequence. Logical-byte accounting, the local SQLite transaction boundary and bounded clock forgiveness are selected. The local accounting and recovery procedures below specify the next experimental increment; the separate [shared-store core](../../../scripts/experiments/mxc_session_patch/HOST_PUBLICATION.md#shared-store-accounting-increment) implements logical reservations and expiry, with fixed-workload Linux/KVM native integration now qualified and Windows/WHP plus physical safeguards still pending.

## Local storage implementation

The first storage implementation uses one local SQLite database per configured shared-store root, containing all admitted sessions. Session identity, call outcomes, checkpoint/result content and references, quota accounting and reservations share that transaction boundary. Admission checks both quotas and records the reservation atomically; publication commits the new checkpoint, saved delivery record and reservation conversion together.

Each session retains exclusive operating-system ownership through a session-specific lock, acquired before database transactions. Guest execution runs outside a database write transaction. Database writes serialize; checkpoint publication latency and contention require qualification before claiming throughput across sessions.

This implementation uses a private local filesystem and supports same-machine recovery. SQLite is the current implementation choice, not a requirement on a future remote storage provider. Network filesystems, machine replacement and distributed fencing remain separate qualification work; selecting SQLite does not reduce the agreed host-configured cross-machine recovery scope.

### Reservation accounting and launch

Each reservation binds the store, session, call, request digest, owner generation and a host-generated scratch token. Its logical charge is the maximum new checkpoint bytes plus maximum serialized result bytes, separately retained artifact bytes and bounded metadata for the new records and manifests. Artifact bytes embedded in the result are not added again. Existing retained state remains charged throughout admission. Versioned metadata accounting includes bounded names, inventories, reference lists and expired/retired identity records; a metadata field with no established bound cannot enter the format. Compression, deduplication and expected later collection are not admission credits.

The shared store also records separate temporary-space allowances for restore copies, candidate export, request/control/output files and cleanup, including their file-count bounds. Publication does not release these allowances while scratch remains. Filesystem capacity checks additionally cover database allocation and transaction headroom; those checks cannot promise space against unrelated writers. The native exporter must enforce candidate bounds while writing, or use a qualified bounded storage mechanism. Rejecting an oversized completed export alone does not bound temporary storage.

Under the session lock, admission starts a write transaction, checks an existing call before considering new work, validates active session ownership, and compares retained usage plus reservations plus the new charge with each quota. It persists the pending call, logical reservation and temporary-space allowance together before making scratch or starting a helper. A matching committed retry needs no new execution reservation, including for a retired session. New work on a retired session refuses. Counter updates and row changes share the transaction, with overflow and negative balances rejected. SQLite's [transaction rules](https://www.sqlite.org/lang_transaction.html) determine contention handling; a busy or failed commit never authorizes execution.

Scratch lives under the store's managed scratch root at the recorded token, using confined access and no links. The host creates it only after reservation commit. A launched helper remains blocked on its private startup header while the supervisor persists its OS process identity, including creation identity and machine boot identity where required to disambiguate PID reuse. Only confirmed persistence permits the header that enables guest restore and execution. Loss of the host before that point leaves a helper without execution authority. The existing [owner gate](../../../scripts/experiments/mxc_session_patch/OWNERSHIP.md) provides the native ordering; the optional journaled supervisor now records process identity before releasing it, with fixed-workload Linux/KVM evidence and Windows/WHP native qualification pending.

Publication requires verified successful helper termination and a bounded, validated candidate. One transaction checks ownership, publishes checkpoint and delivery content, advances the current checkpoint and converts the logical reservation to actual retained usage. It releases unused logical capacity while retaining the separate allowance for remaining scratch. Previous checkpoint references remain charged until safe collection removes them. A commit whose outcome is unknown is resolved by reopening the store and inspecting the durable call record, never by rerunning the program.

### Reservation recovery

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

### Qualification of accounting and recovery

The first increment changes [the experimental store](../../../scripts/experiments/mxc_session_patch/host_store.py) and its focused tests, then [the supervisor](../../../scripts/experiments/mxc_session_patch/host_call.py) and process-death probes. It keeps the production adapter and cross-machine recovery separate. Clock tests use injected time rather than long sleeps; crash tests kill child processes at persisted boundaries. Native qualification runs on GitHub runners with the required hypervisor, separately for Windows/WHP and Linux/KVM. An unavailable platform remains unverified.

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

## Recovery on another machine

Same-machine recovery is the default. The host can explicitly enable recovery on another compatible machine. This option requires checkpoint storage independent of the original machine and exclusive, fenced ownership of the session.

Before restoring, the new owner validates runtime and guest artifacts, architecture and the OS/hypervisor requirements of the checkpoint format. Both operating systems are execution targets; this does not imply Windows-to-Linux checkpoint portability or compatibility across runtime upgrades.

Ownership transfer prevents an old owner from executing or committing after its replacement takes over. A lease timeout alone is insufficient if the old guest can continue making external requests. Recovery creates a new physical generation and does not adopt an unidentified orphan guest. Unavailable storage, lost ownership and incompatible runtime artifacts fail explicitly; they do not downgrade the session to uncheckpointed execution.

## Network policy

Closed networking is the baseline. The host can select an allowlist only where a supported MXC interface and real enforcement are established for that profile. An unsupported allowlisted workload is refused; it is not silently run with unrestricted access or with a different network policy.

Qualification demonstrates that an allowed destination works and a forbidden destination fails, including direct-address, raw-socket, IPv4/IPv6, DNS, redirect and host-local bypass cases. HTTP method/path restrictions require their own evidence before declaration. Proxy environment variables alone do not establish enforcement.

Recovery reapplies the currently authorized policy rather than restoring stale network authority. Allowlisting does not implicitly supply host credentials, enable a credential gateway or authorize runtime package installation. These remain separate host-controlled surfaces.

## Selected MXC extension

The maintainer selected extending MXC's Hyperlight API on 2026-10-02. The production integration remains `kind -> SandboxRouter -> MXC adapter -> MXC Hyperlight`; the direct Unikraft helper remains a feasibility experiment. Existing one-shot execution keeps its fresh-state behavior. Persistent sessions require an explicit, separately admitted experimental surface.

The [native spike evidence](https://github.com/sokolaidev/maf-extensions/blob/2797c74f8d0503d22e9b3a76c1457aa5723a05e1/scripts/experiments/mxc_hyperlight_probe.md#native-state-and-restart-experiment) establishes continuity and same-machine process-restart recovery for the tested Python state on Windows. It does not establish that unchanged MXC exposes those operations. Linux, machine replacement and production durability remain separate gates.

### Proposed responsibility boundary

MXC would own the live native session, guest execution, interruption, quiescent checkpoint capture, compatible restore and disposal. Its session fixes the runtime, mounts and admitted policy; changing these cannot silently replace the guest with empty state. The suite would own trusted conversation identity, serialized tool calls, bounded files and artifacts, checkpoint-store credentials, atomic publication of checkpoint/result records, retry delivery and fenced ownership. Snapshot serialization alone does not implement the suite's durable completion contract.

### Proposed session operations

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

## Implementation gates

The selected product scope precedes implementation. The exact adapter API, native transport, checkpoint representation, durable storage interface and runtime distribution remain engineering decisions. The proposed package name and transport alternatives stay in the [research record](../research/mxc-backend.md#execution-transport-and-ownership).

The first feasibility gate, tracked by [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649), establishes a supported path for both interpreter continuity and durable capture/restore of modified state. MXC's prepared startup snapshot is not evidence of either requirement. If the available mechanism preserves only a restricted subset of state, that limitation returns for a scope decision rather than being hidden behind the word persistence.

Subsequent gates cover bounded execution and output, native memory limits, deadlines and cancellation, owner-death cleanup, safe files, atomic checkpoints, lost acknowledgments, corrupt state, and recovery with competing owners. The first usable version qualifies the agreed file and persistence scope independently on both hosts. Allowlisting remains conditional on verified support; the rest of the scope is not weakened to make an experiment pass.

## Status

| Decision | State | Tracking |
|---|---|---|
| Extend MXC for persistent Hyperlight sessions | Selected; removable experimental patch available, supported output/control transport pending | [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) (open) |
| Interpreter continuity and checkpoint feasibility | Conditional go for experimental integration; production acceptance remains separate | [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649) (closed) by [#1674](https://github.com/sokolaidev/maf-extensions/pull/1674) (merged); [conclusion](../../../scripts/experiments/mxc_session_patch/CONCLUSION.md) |
| Rich Python through MXC Hyperlight and CodeAct | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Code/text execution plus bounded file inputs and artifacts | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Windows WHP and Linux KVM in the first version | Selected; fixed native recovery and local checkpoint/result publication probes passed independently on Windows/WHP and hosted Linux/KVM; production adapter unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open); [probe evidence and limits](../../../scripts/experiments/mxc_session_patch/HOST_PUBLICATION.md) |
| Fresh state by default and optional conversation persistence | Selected; native continuity demonstrated, MXC API unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Restart recovery of persistent Python state | Selected; native process-restart probe passed, durable integration unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Checkpoint after every successful persistent call, before acknowledgment | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Host-configured recovery on another compatible machine | Selected; storage, compatibility and ownership unverified | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Closed networking and conditional enforced allowlisting | Selected; live qualification unrun | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Bounded native output and control transport | Partial experimental Rust-only bounded console capture with explicit truncation; Windows/WHP and Linux/KVM qualified for this scope. Separate streams and faithful binary output remain unimplemented; [evidence](../../../scripts/experiments/mxc_session_patch/OUTPUT.md) | [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) (open) |
| Native owner-death cleanup | Experimental private-pipe controls passed independently on Windows/WHP and Linux/KVM; arbitrary descendants and distributed fencing remain outside this result | [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669) (closed) by [#1674](https://github.com/sokolaidev/maf-extensions/pull/1674) (merged); [evidence and limits](../../../scripts/experiments/mxc_session_patch/OWNERSHIP.md) |
| General bounded file input and artifact collection | Unimplemented beyond the fixed CSV/chart probe | [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670) (open) |
| Compatible-machine recovery and fencing | Unimplemented | [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) (open) |
| Completed-result retry window | Partial: shared-store core persists the selected retry policy and preserves expired identity; store controls and fixed-workload Linux/KVM restart/replay qualified; Windows/WHP native qualification pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Clock uncertainty during result expiry | Partial: shared-store core implements the host-configurable five-minute default, fixed outer deadline and prepaid grants; injected-clock and process-death tests only | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Persistent-session idle expiry | Selected: no automatic expiry by default, optional host-configured idle timeout and explicit deletion; unimplemented | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Transactional logical-charge totals | Experimental implementation: indexed admission totals, explicit format-3 upgrade, rollback/recovery audits; hosted synthetic scaling measurements pending | [#1772](https://github.com/sokolaidev/maf-extensions/issues/1772) (open) |
| Session deletion | Partial: store retirement blocks admission/publication, preserves retries and permits unreserved checkpoint collection; journaled scratch cleanup after verified helper exit implemented; active native termination pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Capacity reservation before persistent execution | Partial: optional supervisor reserves logical and scratch capacity atomically and retains unresolved reservations; bounded OCI export overlay implemented; locked native compilation and fixed-workload Linux/KVM qualified; Windows/WHP and physical safeguards pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Checkpoint collection | Partial: manifest/content validation and transactional reference-based collection preserve retry results and shared chunks; local process controls and fixed-workload Linux/KVM result replay after collection qualified; large-store performance unmeasured | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Interrupted reservation reconciliation | Partial: journal records helper creation identity before startup; explicit reconciliation checks termination and reclaims bounded private scratch; fixed-workload Linux/KVM qualified; unidentified launches remain charged and Windows/WHP native qualification pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Local durable storage transaction boundary | Partial: separate shared-store core with per-session ownership and atomic quota/publication records; local store controls and fixed-workload Linux/KVM restart/replay qualified; Windows/WHP native qualification pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Total session storage and retention | Selected: explicit host-configured per-session and aggregate shared-store quotas required for persistence, no built-in quota defaults; logical accounting and checkpoint collection implemented in shared-store core; fixed-workload Linux/KVM qualified; physical safeguards and Windows/WHP native qualification pending | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Runtime egress qualification | Unrun | [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673) (open) |
| Configuration API and runtime distribution | Open engineering design | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
