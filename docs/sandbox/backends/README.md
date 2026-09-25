# Sandbox backends

A backend creates sandboxes, runs work, transfers files and cleans up. Kinds call the shared `Sandbox` protocol. They do not import a backend.

The host chooses a backend and sets policy. The router checks that the backend can meet each kind's requirements. Unsupported requests are refused.

## Where a backend fits

![The model calls a sandbox tool through host policy. The tool wrapper and kind use the router, which selects a backend. The backend runs the guest and returns execution bytes and files. The kind and wrapper turn those results into labelled content items for the model. Destination tools receive later model calls through host policy; their accepted integrity and confidentiality are separate from the backend's isolation and capabilities.](../assets/backend-boundary.svg)

Backend isolation protects the host from guest execution. It does not make guest output trusted. The kind and tool wrapper decide which returned content items they can vouch for. Source tools and destination tools keep their own label declarations.

See [information flow](../information-flow.md) for the source-tool, content-item and destination-tool rules, and [the four result fields](../information-flow.md#the-result-contract) for the returned content contract.

## Choose a backend

| Backend | Isolation | Execution | File support | Cleanup |
|---|---|---|---|---|
| [ACAS](acas.md) | `MICROVM` | Commands; host-tool transport | Upload, read, list, delete; image checks apply | Dispose |
| [Docker Sandboxes](docker-sbx.md) | `MICROVM` | Commands | Upload, read, list and delete in a host-owned workspace; removals run in the guest | Dispose or reclaim |
| [Docker](docker.md) | `CONTAINER` | Commands; host-tool transport | Upload, read, delete in the container root filesystem | Dispose by default; optional reclaim |
| [WSLC](wslc.md) | `CONTAINER` | Commands | Upload | Dispose |
| [Hyperlight](hyperlight.md) | `MICROVM` | Packaged Python runtime | Optional flat output reads and listing | Reset; dispose on failure |
| [In-process](in-process.md) | `NONE` | Scripted test results | In-memory test store | Test implementations |

The router's default minimum is `MICROVM`. Docker and WSLC require an explicit host floor of `CONTAINER`. The fake requires `NONE` and belongs only in tests.

| Backend | Network policy | Guest OS declaration | Sharing |
|---|---|---|---|
| ACAS | `CLOSED`, host `ALLOWLIST` | POSIX | Conversation or call |
| Docker Sandboxes | `CLOSED` | POSIX | Conversation |
| Docker | `CLOSED`; `ALLOWLIST` with a configured proxy | POSIX when the async factory confirms a Linux daemon | Conversation or call |
| WSLC | `CLOSED`; `ALLOWLIST` with a configured proxy | POSIX | Conversation or call |
| Hyperlight | `CLOSED`, exact-host HTTP/HTTPS `ALLOWLIST` | None; language runtime | Conversation, one owning host process |
| In-process | Declarations for policy tests; no network enforcement | None by default | One shared fake; separate key/kind stores are opt-in |

Docker and WSLC report attributable proxy decisions when a proxy image is configured. ACAS and Hyperlight make no egress observation claim. An absent event is not proof that nothing was attempted.

## File boundaries

| Backend | Important limit |
|---|---|
| ACAS | Workload writes and deletes run as the guest. Native reads, stat and listing retain races between path checks and file access. |
| Docker Sandboxes | File methods reach only the storage base's parent. On Windows the plane rests on the guest being unable to create links in its workspace. |
| Docker | Pauses the guest during path checks and archive transfers. The file view covers the root filesystem, not guest mounts such as tmpfs. |
| WSLC | Uploads use root authority. A guest can replace a checked parent before extraction and redirect the write. |
| Hyperlight | Optional output collection accepts flat names under `/output`; no input upload or listing. |
| In-process | Exercises protocol behavior, not operating-system confinement. |

ACAS permits 32 MiB per file, 128 MiB total and 128 files in each direction. Docker permits 64 MiB per file, 256 MiB total and 256 files.

WSLC, Hyperlight and the fake use `DEFAULT_SANDBOX_LIMITS`: 8 MiB per file, 32 MiB total and 64 files in each direction. These ceilings do not grant an otherwise absent capability.

<a id="the-six-declarations"></a>

## Backend declarations

`SandboxBackend.isolation` is required. The remaining declarations are fields of one `BackendDeclarations` object, available before acquisition.

| Field | Default | Meaning |
|---|---|---|
| `capabilities` | `EXEC`, `FILES_IN` | Supported operations |
| `limits` | `DEFAULT_SANDBOX_LIMITS` | Transfer ceilings |
| `egress_modes` | Empty | Network modes enforced; empty refuses every spec |
| `os_families` | Empty | Guest OS families; empty refuses a spec that requires one |
| `isolation_scopes` | `CONVERSATION` | Supported sharing boundaries |
| `observes_egress` | `False` | Whether attributable network decisions can be reported |
| `egress_method_tokens` | Empty | Methods enforced with `EGRESS_METHODS`; `None` means any token |
| `attached_identity` | `NO_ATTACHED_IDENTITY` | Identity promised within the core policy contract |
| `requires_exclusive_admission` | `False` | Whether one call must retain ownership through delivery and cleanup |

No real backend advertises the core `ATTACHED_IDENTITY` contract. ACAS separately supports [group-configured identity](acas.md#sandbox-group-identity). The host owns that configuration; the router does not discover or constrain it through these declarations.

A backend declaring attached identity must name its authority channels and enforced retention bound. Its declaration must agree with `ATTACHED_IDENTITY` and fit host and workload policy. See [identity](../hosts.md#identity--whose-authority-sandbox-work-carries).

## Lifecycle rules

Each real sandbox belongs to `(SandboxKey, kind)`. Warm acquisition reuses that sandbox when its policy and storage base still match. Calls sharing a key but using different kinds have separate sandboxes.

Container and service backends write ownership labels at creation and discover resources through their provider when disposing them. Long label values are hashed, not truncated. Hyperlight instead uses its owning process's shared registry; requests and purges must reach that owner.

`dispose(key)` covers all kinds for that key. A kind filter narrows it. `dispose_scope` covers the selected scope and conversation. Deletion failures are reported and retained for retry. Exact-instance cleanup must not delete a replacement.

Backend cleanup and operator retention are separate. The backend supplies discovery and deletion helpers; the operator owns credentials, coordination and scheduling. See [operations](../operations.md).

For implementation, use [writing a backend](writing-a-backend.md). For ACAS authentication, use [host-selected credentials](acas-credentials.md). Package READMEs own installation and configuration examples.

## Status

| Area | State | Reference |
|---|---|---|
| Backend selection and declarations | Implemented | [Policy and isolation](../policy-isolation.md), [capabilities](../capabilities.md) |
| Service and container backends | Implemented with the limits above | [ACAS](acas.md), [Docker](docker.md), [WSLC](wslc.md) |
| Local microVM backend | Implemented, `CLOSED` only; live CI added | [Docker Sandboxes](docker-sbx.md) |
| Packaged Python runtime | Implemented for the supported host family | [Hyperlight](hyperlight.md) |
| Test backend | Implemented; no security boundary | [In-process](in-process.md) |
| Credential ownership and retention | Defined per backend and deployment | [ACAS credentials](acas-credentials.md), [operations](../operations.md) |
