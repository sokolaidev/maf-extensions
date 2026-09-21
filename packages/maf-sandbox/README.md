# maf-sandbox

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox)](https://pypi.org/project/maf-sandbox/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxExperimentalWarning`.

Run agent tools through a shared sandbox protocol. The host chooses the isolation and cleanup policy. A backend creates the sandbox; a workload package, called a *kind*, uses it.

The protocol uses only the Python standard library. Microsoft Agent Framework integration lives in `maf_sandbox.maf`; the distribution includes the framework dependency. This is an independent package, not a Microsoft product.

## Install and wire a backend

```bash
pip install maf-sandbox
```

Install a [backend](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/README.md) for the environment where code will run. The router defaults to microVM isolation and disposal after each sandboxed tool call.

```python
from maf_sandbox import Capability, SandboxKey, SandboxRouter, SandboxSpec

router = SandboxRouter([backend])
key = SandboxKey(scope="tenant-1", thread_id="thread-1", agent_id="analyst")
spec = SandboxSpec(
    kind="python",
    image="python-3.13",
    work_dir=None,
    requires=frozenset({Capability.EXEC}),
)

router.ensure_can_serve(spec)
try:
    sandbox = await router.acquire(key, spec)
    result = await sandbox.exec(["python3", "-c", "print(6 * 7)"], working_directory=".", timeout=30)
    print(result.stdout_text)
finally:
    await router.dispose(key)
```

`backend` is supplied by the host. The image name above selects ACAS's prebuilt Python image; use an image supported by your backend. Docker and WSLC require explicit `min_isolation=Isolation.CONTAINER` on the router.

Direct `acquire` does not wrap a tool call or schedule its cleanup. The `finally` above owns disposal. Packaged kinds use `sandboxed_tool`, which handles call admission, labels and cleanup.

For complete application wiring, start with the [ACAS Bicep sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) or [Docker CodeAct sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/06_docker_codeact).

## Main types

| Type | Purpose |
|---|---|
| `SandboxKey` | Names the host scope, conversation, agent and optional tool call. |
| `SandboxSpec` | States a kind's image, commands, file limits, network policy and other requirements. |
| `SandboxBackend` | Acquires sandboxes and disposes them by key or conversation. |
| `BackendDeclarations` | States the backend's capabilities and limits. |
| `Sandbox` | Runs commands or code and provides the declared file operations. |
| `SandboxRouter` | Matches requirements to a backend and enforces host policy. |
| `CallerContext` | Reads the caller's scope, conversation and allowed file listing at call time. |

The host supplies key values. They must not become model arguments. `CallerContext` uses callables so one tool can serve different requests without capturing the wrong conversation.

Kinds use the protocol and never import backends. See the [architecture guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/architecture.md) for package responsibilities.

<a id="four-axes-six-checks-that-are-not-conveniences"></a>

## Policy checks

`ensure_can_serve(spec)` checks configuration before a tool is attached. `acquire` repeats the same policy checks before reaching the backend.

| Check | Rule |
|---|---|
| Isolation | Meet the stronger of the host and kind minimums. |
| Capabilities | Supply every required operation, including requirements derived from other settings. |
| Network | Enforce the exact requested mode and allowed destinations. |
| Guest platform | Match `requires_os_family` when the kind names one. |
| Transfers | Accept the requested per-file bytes, total bytes and file count. |
| Lifetime | Meet the host and kind isolation-scope requirements. |
| Attached authority | Respect explicit opt-in, sharing, destination and retention bounds. |

The isolation order is `none < runtime < os_process < container < hardened_container < microvm < vm`. These are backend declarations. Core does not create or independently verify the underlying boundary.

Network access defaults to `Egress.CLOSED`. An allowlist requires `Egress.ALLOWLIST` and `egress_allow`. Method rules such as `EgressRule("api.example.com", ("GET",))` additionally require `EGRESS_METHODS`. No shipped backend declares that capability. GET requests can still carry data.

The default transfer limits in each direction are 8 MiB per file, 32 MiB total and 64 files. They bound accepted transfers; they do not promise an SDK memory ceiling.

By default, `Selection.FIXED` uses `selected=` or the first registered backend. `Selection.PER_SPEC` uses the first backend whose declarations meet the spec. Registration order sets preference. A runtime failure does not trigger fallback to another backend.

See [policy](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/policy.md) and [capabilities](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/capabilities.md) for the complete rules.

## Attached authority

`BackendDeclarations.attached_identity` describes authority supplied through the core contract. A workload must request `ATTACHED_IDENTITY`, bound its sharing and lifetime, and name authorized destinations. The host separately sets `max_identity_scope`; its default permits none.

No real backend advertises this core contract. ACAS sandbox-group identity is separate, deployment-owned configuration. Core does not discover or bound those Azure assignments. See [host identity](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#identity--whose-authority-sandbox-work-carries).

## Files in and out

`work_dir=None` lets the backend choose the guest storage base. An explicit path requests that exact base. The spec's default is `/maf-sandbox/work`. Acquisition prepares the base for workloads using commands or files; it does not grant the guest extra permissions.

Use `working_directory="."` for the base and relative child paths beneath it. Commands and their arguments are passed through unchanged. `SandboxToolSession.guest_call_path()` supplies a relative call directory; do not prepend a guessed guest path.

File operations check paths against their working directory. The strength of that check depends on the backend. In particular, [ACAS](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/acas.md) and [WSLC](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/wslc.md) document concurrent path changes that can escape a checked directory.

For outputs, declare literal filenames and provide a host-owned destination:

```python
from pathlib import Path

from maf_sandbox import Capability, DeclaredOutput, SandboxSpec, collect_outputs, make_file_system_sink

spec = SandboxSpec(
    kind="diagram",
    requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
    declared_outputs=(DeclaredOutput("diagram.png", media_type="image/png"),),
)
sink = make_file_system_sink(Path("out"))

# After the workload has written its declared output:
landed = await collect_outputs(sandbox, spec, sink=sink)
```

Collection validates names, checks every declared file and reads all files destined for the sink before delivering any. Transfer failure therefore prevents delivery. A sink failure can still leave earlier artifacts delivered; delivery has no transaction or rollback.

`OutputDisposition.CONSUME` reserves an output for the kind to parse instead of landing it. It still counts against transfer limits. Call-time declarations require `outputs_named_at_call_time=True` on the spec.

| Sink | Behavior |
|---|---|
| `make_file_system_sink(root)` | Writes beneath the root and refuses existing files by default. `existing="replace"` permits replacement. |
| `make_file_store_sink(store, provenance=...)` | Writes UTF-8 text beneath a call-ID folder, refuses replacement and records untrusted origin before writing. |
| Custom `OutputSink` | Owns destination checks, storage policy and the model-facing display reference. |

The filesystem sink checks resolved paths but does not hold them against concurrent host-side replacement. Protect its destination from other writers. A store used for sandbox outputs should be readable by the agent and separate from its writable input store.

For binary content use `ExecResult.stdout_bytes` and `stderr_bytes`. Text views, including `stdout` and `stderr`, use UTF-8 replacement decoding. Keep byte fields out of JSON serialization unless you encode them explicitly. See [execution output](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/exec-output.md).

## Labels and model-visible results

Source tools declare what they produce. Returned content items carry integrity and confidentiality labels. The host's information-flow middleware decides what the model can read and which later tools may receive it.

![Source tools declare result labels. Returned content items have their own effective integrity and confidentiality. The framework shows content or a hidden reference to the model. Before a later destination tool runs, policy checks the conversation and argument labels against that tool's accepted integrity and confidentiality.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/c6479aaa19d14ffcf25f76ea7ff11ce152a16fb2/docs/sandbox/assets/information-flow.svg)

With automatic hiding enabled, untrusted content is hidden while the conversation remains trusted. Hidden content still affects confidentiality. Passing its reference to another tool remains subject to that destination's policy.

`sandboxed_tool` accepts a string or unlabelled `Content` items. A kind can commit fixed `standing_guidance` and return it last on every normal path. Core verifies that text and stamps it trusted/public, while the framework preserves stricter call confidentiality. Counts, exit statuses and advice chosen from guest output do not qualify as fixed guidance.

For these mixed results, the framework-facing source declaration is trusted. The kind's output claim remains in `maf_sandbox_derived_integrity`, and core labels each derived item separately. Every shipped kind claims untrusted output.

`FileStoreProvenance` records the integrity of stored text. Session reads can weaken a call's derived result; trusted reads never promote an untrusted kind. `requires_file_integrity` can refuse weak or unknown inputs before execution. The host must wire the shared provenance record into both writes and reads.

See [information flow](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md) for label rules and the decided result contract. Its status table identifies the contract work that is not yet implemented.

## Calling host tools from guest code

`HostToolRegistry` exposes only functions the host registers. Registered functions run in the host process with its privileges. These nested calls bypass ordinary agent tool middleware, so the registry must enforce their policy.

Use `@sandbox_tool(source=..., sink=..., identity=...)` to declare each function's role. Set `require_declared=True` to refuse undeclared functions. The registry reads declarations at registration and seals when their combined policy is read.

Application authority is allowed by default. User authority requires explicit `allowed_identities`, a host `mint_user_identity` callback and approval of the enclosing tool. Identity labels describe authority; they do not reduce the function's permissions.

Call-count and response-size limits apply per run. The router can forbid the entire channel with `denied_capabilities={Capability.HOST_TOOLS}`. Docker and ACAS support the exec-based transport; WSLC and Hyperlight do not.

See [host-tool controls](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md#calling-host-tools) and [CodeAct](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-codeact/README.md) for wiring.

## Call cleanup and concurrency

`sandboxed_tool` disposes sandboxes after each call by default. Hosts can explicitly choose `Cleanup.RESET` for snapshot restore, or `Cleanup.RECLAIM` for removal of call files on a supporting backend. Reclaim can leave other files and processes behind. A confinement declaration alone does not permit reuse.

Ordinary calls can overlap through one router. Whole-sandbox cleanup stops new admission and waits for active siblings. Kinds requesting exclusive admission run one call at a time, including cleanup.

This coordination belongs to one router. It does not separate siblings' data or coordinate unrelated processes. Use `IsolationScope.CALL` when calls must never share a sandbox; it always requires disposal.

Failed call cleanup normally refuses the key until removal succeeds. `dispose_unclean` retries recorded targets. `FailedReclaimPolicy.KEEP` is an explicit host choice to accept reuse after failure.

At conversation deletion, stop new work across replicas and call `router.dispose_scope(scope, thread_id)`. Check the returned `ScopePurge.undisposed`; a count alone does not establish completion. `router.scope(...)` provides this cleanup as an async context manager.

`dispose_kind(key, kind, timeout=...)` narrows cleanup to one kind. Adding `instance_id=` targets one physical instance and protects replacements. Direct admission users must await `release_call(...)`, which may perform queued cleanup.

See [call cleanup](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/tool-call.md) and [host disposal](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/operations.md#host-disposal) for timeouts, retries and ownership.

## Observability and extension points

Register a `SandboxObserver` on the router and host-tool registry. Kinds also pass it to output collection. Events describe acquisitions, calls, transfers, processes, network decisions and cleanup. Use `maf-sandbox-otel` for OpenTelemetry export.

Callbacks run synchronously and may arrive on different threads. Keep them short and send records to a thread-safe queue. Ordinary observer failures are logged without changing the tool result. Event delivery is not a durable audit guarantee.

`effective_state_middleware()` can persist the served configuration in `AgentSession.state`. It records configuration rather than payloads. See [observability](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md).

For new implementations, use [writing a kind](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/writing-a-kind.md) or [writing a backend](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/writing-a-backend.md). Backend declarations belong in `BackendDeclarations`, not standalone capability attributes.

Run the shared conformance suites against real backends. A kind claiming call-directory confinement must run `assert_nothing_left_behind` with an engine-backed fingerprint subject. A skipped probe or in-process fake does not prove confinement.

`BoundedExec`, shell file-transfer helpers and `SyncRunner` are optional building blocks. Their contracts and failure handling are covered by the [guest guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/guest-platform-and-commands.md) and [backend authoring guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/writing-a-backend.md).

Maintained by [SOKOLAI BV](https://www.sokol.ai).
