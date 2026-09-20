# Sandbox architecture

An agent calls a sandboxed tool through the normal framework middleware. The tool body runs in the host process and sends the work to a sandbox. The backend provides the execution boundary; the router checks whether its declared contract fits the host and workload.

## Layers

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

| Layer | Responsibility |
|---|---|
| Host application | Agent, request identity, tool policy, credentials, storage and conversation deletion |
| Kind | Workload spec, input selection, guest operations and result contract |
| Core | Protocol types, routing, shared checks and lifecycle coordination |
| Backend | Provider access, sandbox creation, execution, transfer and disposal |
| Guest | Runs the workload with the authority and channels provided to it |

![The model calls a kind through host middleware. The kind's wrapper derives a key from trusted request context, checks the spec through the router and sends operations through one backend to the guest. Guest bytes return to the kind, which builds labelled content items. Declared artifacts go to the host's sink. An explicitly enabled guest-to-host tool channel goes through the host-tool registry and its own gates, bypassing the inward framework middleware. The host owns credentials and storage; the backend provides the execution boundary.](assets/architecture-map.svg)

Kinds use the core protocol and do not import backends. Backends do not import kinds. Protocol modules use only the standard library. Tests enforce these import boundaries and each package's declared dependencies.

## Vocabulary

| Type | Meaning |
|---|---|
| `SandboxKey` | Host-derived `(scope, thread_id, agent_id, call_id)` |
| `SandboxSpec` | Kind, image/runtime, guest base, required capabilities, egress, sharing, cleanup and transfer rules |
| `SandboxBackend` | Acquires and disposes instances; discovers resources for conversation purge |
| `Sandbox` | Acquired instance and its command, runtime, file and cleanup methods |
| `BackendDeclarations` | Optional capability, limit, guest, egress, sharing and attached-authority claims |
| `CallerContext` | Per-call identity accessors and the caller's file listing |
| `SandboxEntry` | Guest entry type, path and known or unknown byte size |
| `DeclaredOutput` | Literal relative output path, with `LAND` or `CONSUME` disposition |
| `TransferLimits` | Per-file bytes, total bytes and file count, separately for input and output |
| `ExecResult` | Returned byte streams, display views, exit status and diagnostic ownership |
| `SandboxObserver` | Synchronous callbacks for selected execution, transfer and lifecycle events |

The protocol defines methods even where a backend must refuse them. Optional behavior is selected through declared capabilities. See [capabilities](capabilities.md) for the complete surface.

## Keys and storage

The full logical sandbox identity is `(key, spec.kind)`. Agents in one conversation have separate `agent_id` values. Different kinds do not share one sandbox. Call-scoped work also carries the framework's call ID.

Every key component comes from trusted host context. Scope and thread accessors are called for each tool call, rather than captured when building the agent. An unbound conversation is refused; there is no shared fallback key.

The caller's file listing determines which input names a kind can read. Enumeration failure is a refusal, not an empty listing. The kind validates selected names before using them in guest operations.

The acquired sandbox owns its base directory. Relative working directories select that base or a child; file operations apply the backend's confinement checks. Guest command strings and argv are not rewritten as paths. See [guest paths and commands](guest-platform-and-commands.md).

## Calls and lifetime

Conversation-scoped acquisition can share an instance for the same key and kind. The router defaults to disposal after active calls finish. Keeping an instance warm across calls requires explicit host opt-in. Call-scoped work uses a separate instance and disposes it at call end.

Backends coordinate concurrent acquisition so callers cannot create duplicate instances and lose track of one.

The wrapper coordinates call admission, output delivery and cleanup. Failed cleanup can refuse reuse until recorded targets are removed. [Tool-call lifetime](tool-call.md) defines those rules.

Conversation purge asks every registered backend, including one that no longer serves new work. Service-backed adapters discover owned resources from service metadata, so purge does not depend on the current replica's memory. A process-owned backend follows its own ownership and shutdown contract.

`SandboxPurger.purge_scoped_thread(scope, thread_id)` connects this to host conversation deletion. Failures are reported without turning a conversation-delete path into a provider exception. The host still needs retry and retention policies. [Operations](operations.md) covers recovery after host death.

## Two directions across the boundary

| Direction | Control point |
|---|---|
| Model calls a sandboxed tool | Framework middleware checks the call; the host-side wrapper runs the kind |
| Guest calls a registered host tool | `HostToolRegistry` applies its own declarations, gates and budgets |
| Kind returns model-visible content | The wrapper applies the [result and label contract](information-flow.md) |
| Kind lands an artifact | The [host sink](hosts.md) checks and stores the delivered bytes |

Guest-to-host tools bypass the middleware that admitted the outer call. Nothing is registered by default. The host must deliberately provide that authority and enforce the registry's gates.

## Framework adapter

`maf_sandbox.maf` is the module that imports `agent_framework`. It is reached explicitly and is not re-exported by `import maf_sandbox`.

| Surface | Role |
|---|---|
| `sandboxed_tool` | Builds a tool, derives caller identity, coordinates cleanup and constructs labelled results |
| `SandboxToolSession` | Holds the binding and per-call access to a sandbox, file store and observer |
| `sandbox_tool_declarations` | Writes the tool's information-flow declarations |
| `file_store_provenance_middleware` | Records integrity information about agent-driven file writes |
| `effective_state_middleware` | Records the configurations that served a tool in session state |

A factory with no configured backend returns an empty tool list. A configured backend that cannot serve the spec raises. During a call, fixed refusal or failure messages go to the model; provider details stay in host logs. [Kind authoring](kinds/writing-a-kind.md) describes the result and error handling.

Dedicated acquisition refusals retain their categories. An otherwise unclassified failure, including `ValueError`, returns unavailable: the type cannot distinguish invalid configuration from a malformed provider response. Listing failures also use a fixed refusal, because exception text must not enter a kind's trusted output.

A kind converting legacy text into content items does so at one return funnel, including accessor refusals. A kind using `SandboxResult` may construct that type on each branch, with refusals in `trusted_output` and `completed=False`. Core checks the result type and renders the fields and committed guidance on every normal return.

The build callback's docstring becomes the tool description. Define it at module level so nesting does not change its indentation.

## Shared helpers

Shared invariants belong where callers cannot skip them. Core holds protocol conveniences and policy. Operational containers, processes and transports belong in backend or sibling packages. A helper needs independent real consumers before becoming shared API.

Some modules require an explicit import because their dependency or use needs care:

| Module | Reason |
|---|---|
| `maf_sandbox.maf` | Depends on framework types |
| `maf_sandbox.paths` | Guest path checks are lexical and are not safe host-filesystem checks; tar helpers fix decoding choices |
| `maf_sandbox.guest_access` | Refuses operations when the backend has not established guest access to transferred files |
| `maf_sandbox.testing`, `maf_sandbox.conformance` | Test fixtures and probes, not production containment |

The shared operational package remains deferred. The existing helpers and backend contracts do not depend on it.

Policy values use enums or named constants. Ordering is explicit data, such as `ISOLATION_RANK`; unknown serialized values are refused.

## Status

| Decision | State | Tracking |
|---|---|---|
| Tool-based integration and separated layers | Implemented | untracked — enforced by package import tests |
| Deployment-owned post-crash cleanup | Implemented | [#1008](https://github.com/sokolaidev/maf-extensions/issues/1008) (closed); [#1014](https://github.com/sokolaidev/maf-extensions/pull/1014) (merged); [operations.md](operations.md) |
| Sandbox identity includes key and kind | Implemented | [#84](https://github.com/sokolaidev/maf-extensions/issues/84) (closed) |
| Stable host-provided agent identity | Implemented | [#1198](https://github.com/sokolaidev/maf-extensions/issues/1198) (closed); [#1205](https://github.com/sokolaidev/maf-extensions/pull/1205) (merged) |
| Isolation, capability and egress admission | Implemented | [policy-isolation.md](policy-isolation.md); [network.md](network.md) |
| Separate output read, listing and landing contracts | Implemented | [#113](https://github.com/sokolaidev/maf-extensions/pull/113) (merged) |
| Guest-to-host tools | Implemented; remaining transport work tracked separately | [hosts.md](hosts.md#status) |
| Guest-family matching and acquire-time command checks | Implemented | [guest-platform-and-commands.md](guest-platform-and-commands.md#status) |
| Capability-gated safe reclamation | Implemented | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) (closed) |
| Shared container tar-header parsing | Implemented | [#731](https://github.com/sokolaidev/maf-extensions/issues/731) (closed) |
| Shared operational package | Deferred | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) (open) |
