# MXC Hyperlight design decisions

MXC Hyperlight supplies a rich Python runtime for CodeAct through the sandbox router. This document records the first-version scope selected on 2026-10-02. The [status table](#status) distinguishes those requirements from implementation and validation. The [research record](../research/mxc-backend.md) contains the release assessment, rationale, candidate transport and feasibility questions.

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

## Implementation gates

The selected product scope precedes implementation. The exact adapter API, native transport, checkpoint representation, durable storage interface and runtime distribution remain engineering decisions. The proposed package name and transport alternatives stay in the [research record](../research/mxc-backend.md#execution-transport-and-ownership).

The first feasibility gate, tracked by [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649), establishes a supported path for both interpreter continuity and durable capture/restore of modified state. MXC's prepared startup snapshot is not evidence of either requirement. If the available mechanism preserves only a restricted subset of state, that limitation returns for a scope decision rather than being hidden behind the word persistence.

Subsequent gates cover bounded execution and output, native memory limits, deadlines and cancellation, owner-death cleanup, safe files, atomic checkpoints, lost acknowledgments, corrupt state, and recovery with competing owners. The first usable version qualifies the agreed file and persistence scope independently on both hosts. Allowlisting remains conditional on verified support; the rest of the scope is not weakened to make an experiment pass.

## Status

| Decision | State | Tracking |
|---|---|---|
| Rich Python through MXC Hyperlight and CodeAct | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Code/text execution plus bounded file inputs and artifacts | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Windows WHP and Linux KVM in the first version | Selected; live qualification unrun | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Fresh state by default and optional conversation persistence | Selected; continuity mechanism unverified | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Restart recovery of persistent Python state | Selected; capture/restore mechanism unverified | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Checkpoint after every successful persistent call, before acknowledgment | Selected; unimplemented | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Host-configured recovery on another compatible machine | Selected; storage, compatibility and ownership unverified | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Closed networking and conditional enforced allowlisting | Selected; live qualification unrun | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
| Transport, configuration API, checkpoint storage and runtime distribution | Open engineering design | [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648) (open) |
