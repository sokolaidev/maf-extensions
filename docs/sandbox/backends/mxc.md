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
| Bounded native output and control transport | Unimplemented; merged streams and truncation remain blockers | [#1668](https://github.com/sokolaidev/maf-extensions/issues/1668) (open) |
| Native owner-death cleanup | Experimental private-pipe implementation; qualification tracked separately | [#1669](https://github.com/sokolaidev/maf-extensions/issues/1669) (open) |
| General bounded file input and artifact collection | Unimplemented beyond the fixed CSV/chart probe | [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670) (open) |
| Compatible-machine recovery and fencing | Unimplemented | [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) (open) |
| Total session storage and retention | Unimplemented | [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672) (open) |
| Runtime egress qualification | Unrun | [#1673](https://github.com/sokolaidev/maf-extensions/issues/1673) (open) |
| Configuration API and runtime distribution | Open engineering design | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
