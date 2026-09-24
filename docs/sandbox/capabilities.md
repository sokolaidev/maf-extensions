# Capabilities and file transfer

A kind states what its workload needs in `SandboxSpec`. A backend states what it can provide in `BackendDeclarations`. The router checks the match before exposing a tool and again before acquisition.

Capabilities describe operations. [Isolation](policy-isolation.md), [network policy](network.md), [guest family](guest-platform-and-commands.md), limits and identity have their own checks.

## Capability vocabulary

| Capability | Meaning | Real backend support |
|---|---|---|
| `EXEC` | Run a shell command or argv | ACAS, Docker, WSLC |
| `RUN_CODE` | Run code through a language runtime | Hyperlight |
| `HOST_TOOLS` | Support calls from a guest program into registered host functions | ACAS, Docker |
| `FILES_IN` | Write files into the sandbox | ACAS, Docker, WSLC |
| `FILES_OUT` | Inspect and read declared output files | ACAS, Docker; Hyperlight with flat outputs enabled |
| `FILES_LIST` | Discover directory entries | ACAS; Hyperlight with output files enabled, flat base only |
| `FILES_DELETE` | Remove workload-selected paths | ACAS, Docker |
| `SNAPSHOT` | Reset to a baseline taken before input | Hyperlight |
| `RECLAIM` | Safely remove the framework's call directory | Docker |
| `ATTACHED_IDENTITY` | Enforce the core attached-authority contract | None |
| `EGRESS_METHODS` | Enforce HTTP method restrictions | Docker and WSLC with iron-proxy configured; Hyperlight for GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS |

This table shows supported configurations. Image checks and backend options can narrow them. Each [backend guide](backends/README.md) states its limits. The in-process fake declares test behavior; it provides no real containment.

`DEFAULT_CAPABILITIES` contains `EXEC` and `FILES_IN`. An omitted capability declaration uses that default. It does not imply support for other operations.

`spec.requires` contains explicit workload requirements. `spec.required_capabilities` also includes requirements derived from policy, such as `EGRESS_METHODS` for a method-limited rule. `RECLAIM` is a cleanup declaration and cannot be a workload requirement.

<a id="backend-selection"></a>

## Selecting a backend

`Selection.FIXED` is the default. The router uses `selected`, or the first registered backend when no name is supplied. It checks each workload against that backend.

`Selection.PER_SPEC` tries backend declarations in registration order. It chooses the first candidate that satisfies the workload and host policy. It cannot be combined with `selected`.

![Fixed selection chooses the named or first backend, then checks the workload against it. Per-spec selection checks registered backends in order and chooses the first that meets the isolation floor, capabilities, limits, guest family, network, sharing and identity rules. No match produces a refusal. Both paths acquire the chosen backend; an image probe can still refuse there.](assets/router-selection.svg)

Selection uses declarations, not health, cost or latency. An acquisition failure does not trigger a second selection. If no candidate matches, the refusal includes the candidates' reasons and keeps the first candidate's exception type.

Host denials apply in both modes. A workload cannot restore a denied capability or identity. All registered backends remain available for disposal, including candidates that cannot serve new work.

With no backend configured, `sandboxed_tool` attaches no tools. Direct acquisition raises `NoSandboxBackend`. See [router policy](policy-isolation.md) for the complete checks and declaration defaults.

## File operations

Paths belong to the guest. A relative `working_directory` resolves under the sandbox's bound storage base; `"."` selects that base. File paths resolve under the selected working directory. Commands and argv remain unchanged.

Relative names use `/`, independent of the host OS. They cannot escape their working directory. Explicit absolute paths follow the guest-native contract. Do not use host filesystem functions to interpret guest paths.

| Operation | Contract |
|---|---|
| `write_file` | Accept `str` or `bytes`; encode text as UTF-8 without newline rewriting; create needed parents |
| `stat_file` | Describe the final entry without following it; refuse traversal through a linked ancestor |
| `read_file` | Read a regular file only, within the requested byte bound |
| `list_dir` | Enumerate a directory only with `FILES_LIST`; report links without traversing them |
| `remove` | Remove a workload-selected path only with `FILES_DELETE` |
| `reclaim` | Remove a framework-owned call directory only where safe cleanup is supported |

`EntryKind` distinguishes `FILE`, `DIRECTORY`, `SYMLINK` and `OTHER`. Junctions and reparse points count as links. Devices, sockets and FIFOs are not regular files. An unknown or negative size cannot authorize a read.

`stat_file` may describe a final link. Listing through that link is refused. A delete may unlink the final link, but must not follow it or a linked parent.

## Declaring outputs

`DeclaredOutput` names one literal relative path. It does not accept globs. Its default role is `LAND`, its default `required` value is `True`, and its default public name is the path. `media_type` is optional and supplied by the kind; core does not infer it from bytes.

| Role | Purpose | Who reads the bytes? |
|---|---|---|
| `CONSUME` | Feed an output back into the kind, such as compiler diagnostics | The kind, with its own bounded read |
| `LAND` | Deliver an artifact to host-owned storage | `collect_outputs`, then the host sink |

Both roles count against file limits and undergo path, type and size checks. `collect_outputs` does not read `CONSUME` bytes. Consumed content affects the kind's source-integrity reasoning; landed content uses an outbound destination.

![A declared output path is checked for confinement, regular-file type, known size and transfer limits. A consume output then goes to a bounded read owned by the kind. A land output is read by collect_outputs and included in the fully checked batch before any host sink receives bytes. Missing required files and unsafe or oversized files are refused.](assets/file-transfer-boundary.svg)

Declare `outputs_named_at_call_time=True` when the tool supplies outputs through `collect_outputs(outputs=...)`. Without it, that override is refused. Both fixed and call-time outputs need `FILES_OUT`; landing also needs an `OutputSink`.

Known filenames do not require `FILES_LIST`. A model-provided literal name or a known manifest can identify outputs directly. Listing is required only when the kind must discover unknown names.

## Transfer limits

`SandboxLimits` has separate inbound and outbound `TransferLimits`. Each direction defaults to 8 MiB per file, 32 MiB total and 64 files. The workload cannot request any limit above the backend's ceiling.

Output collection checks declared sizes before reading. It checks actual returned bytes again before delivery. Missing required files, unsafe paths, non-files and exceeded limits raise `SandboxOutputError` subclasses. Files are refused, not truncated.

The complete set is checked and read before the first sink callback. A callback can still fail after earlier artifacts were delivered; there is no batch rollback. [Host output handling](hosts.md#where-artifacts-land) owns that contract.

These limits bound accepted transfers. They do not promise a peak-memory bound when an SDK buffers a response before returning it. A backend that needs a hard memory bound must enforce it while reading.

## Confinement and deletion

Confinement covers the selected directory, its ancestors and the final operation. A lexical path check alone cannot stop the guest from replacing a parent after it was checked.

Backends establish safe reach through their own mechanisms. Docker uses engine metadata and guest freezing for relevant file operations. ACAS and WSLC document remaining check-then-use windows. Their [backend guides](backends/README.md) define the exact guarantees.

Guest-supplied answers are weaker than engine metadata. For example, ACAS file metadata cannot establish every regular-file property; a non-directory, non-link entry may still be a FIFO. Bounded reads limit the wait but do not make that metadata more precise.

`remove` treats a missing path as success. Directories require `recursive=True`, including empty ones. Recursive removal unlinks interior links without following them. It refuses the working directory itself, outside paths and linked parents.

An invalid path raises `ValueError`; an operation failure raises `OSError`; an unsupported operation raises `NotImplementedError`. Output collection translates path and access failures into its output-error family while retaining their causes.

`reclaim` has a different caller and purpose. The framework supplies an unguessable call path, but that name does not prove safe ancestry after the guest runs. Core refuses the working directory itself and paths fewer than two components from root. The backend must establish safe removal reach or refuse.

## Cleanup and runtimes

The router defaults to `Cleanup.DISPOSE`. Reuse requires an explicit host setting. `RECLAIM` permits call-directory removal; `SNAPSHOT` permits reset; disposal is always available. The router selects an available operation at or above the host and workload floors.

`confined_to_guest_call_path` describes the kind's behavior. It neither proves complete cleanup nor enables reuse. [Tool-call lifetime](tool-call.md#cleanup-as-a-consequence) covers cleanup, concurrency and failure handling.

`RUN_CODE` has no working-directory argument. The backend owns runtime state and imports. Its wall timeout includes waiting for execution; `SandboxQueuedTimeout` distinguishes a job that never started.

CodeAct selects an explicit `CodeactRuntime`; its default path uses `EXEC`. A runtime path requests `RUN_CODE` and only the file capabilities it uses. The [CodeAct guide](kinds/codeact.md) defines that integration.

## Verify a backend

`maf_sandbox.conformance` provides reusable probes without importing pytest. Run them against the backend's real execution and file mechanisms. An undeclared capability is skipped, which is not a passing proof of support.

The in-process fake tests protocol handling and refusal paths. It cannot establish a real backend's containment, command behavior or process cleanup.

## Status

| Decision | State | Tracking |
|---|---|---|
| Capability and transfer vocabulary | Implemented | [#113](https://github.com/sokolaidev/maf-extensions/pull/113) (merged) |
| Backend selection per spec | Implemented; host opt-in | [#328](https://github.com/sokolaidev/maf-extensions/issues/328) (closed); [#872](https://github.com/sokolaidev/maf-extensions/pull/872) (merged) |
| File output support | ACAS and Docker implemented; WSLC output transport remains blocked | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) (open); [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open) |
| Call-time output names | Implemented | [#156](https://github.com/sokolaidev/maf-extensions/pull/156) (merged) |
| File confinement and backend-owned reclamation | Implemented with backend-specific limits | [#488](https://github.com/sokolaidev/maf-extensions/pull/488) (merged); [#477](https://github.com/sokolaidev/maf-extensions/issues/477) (closed) |
| Backend-owned storage base | Implemented | [#480](https://github.com/sokolaidev/maf-extensions/issues/480) (closed); [#1090](https://github.com/sokolaidev/maf-extensions/pull/1090) (merged) |
| Cleanup selection | Implemented; disposal by default | [Tool-call lifetime](tool-call.md#status) |
| Runtime code execution and Hyperlight | Runtime path implemented; broader backend work tracked separately | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open) |
| Method-limited egress and attached authority | Core admission implemented; no real backend advertises either capability | [Network](network.md#status); [hosts](hosts.md#status) |
| Atomic batch delivery | Unimplemented; delivery remains per artifact | untracked; [host contract](hosts.md#where-artifacts-land) |
