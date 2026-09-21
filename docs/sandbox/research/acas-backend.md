# ACA Sandboxes research

> Consolidated research record for ACAS host credentials, exec byte capture and method-scoped egress, measured 2026-09-10 through 2026-09-14. The implemented operational contracts live in [`../backends/acas.md`](../backends/acas.md), [`../backends/acas-credentials.md`](../backends/acas-credentials.md), [`../exec-output.md`](../exec-output.md) and [`../network.md`](../network.md). This record keeps the source evidence, measurements and remaining limits without repeating those guides.

## Conclusions at a glance

- ACAS control-plane credentials are host-owned and selected per request. Acquired wrappers capture a non-secret authority binding; later disposal resolves cleanup authority independently so another replica can clean up after the creator disappears. Credential objects and bearer tokens never enter the guest.
- ACAS exec output loses arbitrary bytes before the SDK decodes it. The implemented solution uses bounded FIFO capture and chunked retrieval through guest execution, preserving exact stdout/stderr while retaining deadlines, cancellation, overflow refusal and cleanup semantics.
- ACAS can enforce method-scoped HTTP policy on the tested HTTPS path, including custom methods, but its service matches method spelling case-insensitively and important surfaces remain unmeasured. The backend therefore withholds `EGRESS_METHODS` and refuses method-scoped rules rather than claiming literal enforcement.
- ACAS working-directory preparation preserves existing directories and creates missing directories with guest authority, refusing if that creation fails. The service stat exposes no ownership, so the host-authority file plane — which mints root-owned directories — cannot be bounded by an ownership check and is not used for preparation.
- ACAS remains the reference `MICROVM` backend and the only shipped backend that declares directory listing. It is a remote, billable service: live evidence is separate from offline tests and must be run with disposable groups and explicit cleanup.

## Host-selected credentials

### Implemented authority model

`AcasSandboxConfig.credential_resolver` receives an `AcasCredentialRequest` containing trusted `scope`, `thread_id`, `operation` and optional `key`. It returns an `AcasCredentialBinding(authority, generation, create_credential)`, where the authority and generation are non-secret host references and the factory creates a fresh async Azure credential on the owning event loop.

| Operation | Resolver purpose |
|---|---|
| `acquire` | Select the current request's grant from trusted host context; two callers in one scope may receive different bindings |
| `dispose` | Recover an authorized cleanup grant for a target key without requiring the original request context |
| `dispose_scope` | Recover scope/thread cleanup authority on whichever replica receives the conversation deletion |

An acquired wrapper captures its binding. Exec, streaming, file operations, polling and cancellation cleanup continue under that binding even if ambient request context changes. Immediate failure cleanup keeps the acquire binding; later disposal and retained retries resolve cleanup authority anew. A resolver, credential factory or permission failure never falls back to `DefaultAzureCredential`, and authentication failures do not trigger replacement creation.

The backend caches clients locally by event loop, authority and generation. A lease lasts through the full SDK operation, including response consumption and streaming cleanup. Wrappers retain resource IDs and captured bindings rather than permanent transports, so an evicted client can be rebuilt without changing the authority of a warm wrapper. Shared credential singletons are unsupported; the cache owns and closes credentials it creates. Rotation selects a new generation while in-flight work keeps its old binding.

The current defaults are `max_clients_per_loop=32`, `client_wait_seconds=30` and `client_close_seconds=30`. Capacity and shutdown are per event loop and therefore multiply across replicas; they are not a distributed credential-minting limit. Eager construction registers ownership before resource work, cancellation of one waiter does not cancel another's lease, and nested helpers share an existing lease to avoid deadlock at capacity one.

`await backend.aclose()` permanently refuses new work, drains admitted operations and closes SDK resources on their owning loops before those loops stop. It does not delete sandboxes. Incomplete closure raises `AcasClientCloseError`, retaining resources for a later retry; hosts must keep owner loops alive through closure. This replaced earlier behavior that logged and suppressed close failures.

### Replica-independent cleanup

Service labels and durable host policy, not a creator's process-local credential cache, are the recovery mechanism. If replica A creates a sandbox and disappears, replica B must resolve cleanup authority from trusted host state, an explicit cleanup principal or a reconstructible grant. Scope labels alone cannot recreate an expired caller assertion, and a cleanup principal different from the request principal is an explicit host policy rather than an automatic 401/403 fallback.

Disposal does not persist bearer tokens or authority references in resource labels. The local pool coordinates client ownership only; it does not provide distributed sandbox locks or exactly-once deletion. Hosts must stop new work across replicas before conversation purge. Failed cleanup remains observable and retryable, including after process restart.

ACAS group-attached managed identity is a separate platform configuration. The host owns group assignments and permissions; acquisition does not inspect ARM assignments or require management-read access. Group identity is not the same as the host's control-plane credential, does not automatically satisfy core's attached-identity capability contract and is not revoked by deleting one sandbox. Guest token access was measured for a specific API, image and group configuration only; deployment acceptance remains separate.

### Baseline evidence

The source baseline used Python 3.13.12, `azure-containerapps-sandbox` 0.1.0b4, `azure-core` 1.41.0 and `azure-identity` 1.25.3 without Azure requests or real credentials. The inspected pre-implementation backend cached group clients by event loop, retained SDK clients in sandbox wrappers, closed resources from the calling loop and selected cleanup credentials from local state. Those observations motivated the implemented binding, lease, generation and shutdown contract.

Offline probes established three hazards:

1. Azure Core's bearer-token policy reused a cached authorization header across two synthetic caller contexts when a shared dynamic credential was used. A credential shortcut that reads ambient context behind a shared policy is unsafe.
2. The old `aclose()` closed a client while an operation was still pending.
3. Closing a cache populated on one event loop from another loop closed fake client and credential resources on the wrong loop.

The focused baseline suite passed 29 tests with 414 deselected and two warnings. It did not exercise live authorization, delegated-token acceptance, distributed deployment or production performance.

## Exact ACAS exec output

### Where loss occurs

The service's `executeShellCommand` response already contains replacement characters for invalid UTF-8 before the SDK constructs its typed result. A program writing `b"ok\\xff\\xfe"` to stdout and `b"err\\xff\\xfe"` to stderr returned JSON text containing U+FFFD; the adapter cannot reverse that loss. The SDK and CLI exposed no raw-byte selector, and the interactive shell path is not evidence for separate noninteractive byte streams.

### Historical measurements

The probe used the public Python 3.13 image, `azure-containerapps-sandbox` 0.1.0b4 and the pre-implementation backend. It measured temporary-file/base64 envelopes, direct file redirection and FIFO readers.

| Strategy or case | Result |
|---|---|
| 277-byte corpus containing every byte, U+FFFD, CRLF and incomplete UTF-8 | Base64 envelope preserved both streams and exit 7 |
| Empty streams, quoted argv and three concurrent commands | Exact |
| 65,536 bytes per stream through the envelope | Exact |
| 1 MiB per stream through the envelope | Service truncated/terminated at 1 MiB, returned exit 137 and an incomplete response |
| Direct file redirection and binary retrieval at 1 MiB per stream | Exact streams and exit 7 |
| Two concurrent FIFO readers at 1 MiB per stream | All four streams exact and both exit codes 7 |
| Delayed background writer | FIFO capture returned `beforeafter`; plain file redirection returned only `before` |

The envelope's service diagnostic was `[adc] output exceeded 1048576 bytes and was truncated; process terminated`. Base64 consumes response budget and can change the reported status, so incomplete frames must be refused, never treated as successful output. Plain redirection also reads too early: a descendant can write after the parent shell exits. FIFO readers wait for writer EOF and preserve that background output.

### Implemented capture contract

The implementation uses bounded FIFO capture and retrieves 48 KiB base64 chunks through guest exec, avoiding privileged file reads and the single-response envelope limit. It preserves malformed-byte corpora in argv and shell forms, quoted arguments, delayed background output and concurrent 1 MiB-per-stream commands with exit 7. Successful captures leave no scratch directories.

`exec_output_limit_bytes` defaults to 1 MiB per stream. Overflow, malformed framing, reader failure, interrupted retrieval or scratch-removal failure raises without returning partial output. A timeout or cancellation invalidates and attempts to delete the sandbox; a one-second command timeout returned after 7.67 seconds including deletion in the live validation. The deadline covers launch, draining, retrieval and cleanup, with a separate bounded deletion allowance. The narrow exception is a completed HTTP 429 `Retry-After` wait with no retry started: the throttled request answered, so the sandbox remains reusable. Other ambiguous retryable statuses and cancellation still dispose.

The capture wrapper requires `sh`, `mkdir`, `mkfifo`, `cat`, `wc`, `dd`, `base64`, `rm`, `rmdir`, writable scratch space and independent directories for concurrent captures. It does not relax strict host-tool/control-message decoding. A non-root image must also permit the guest to create and read its scratch files; missing utilities or permissions are acquisition/exec failures, not reasons to use the host file plane.

The live implementation validation passed concurrent byte capture, background output, overflow refusal, timeout and cancellation, confirmed empty scopes after cleanup and passed direct Docker EXEC/FILES_IN conformance with the widened byte corpus. Portable tests cover root and UID 65534 containers, original umask, symlink replacement, failed readers and unwritable scratch. WSLC raw adapter behavior is covered offline; the capture implementation was not live-exercised on WSLC.

## Method-scoped egress

### Setup and measurements

The live service measurement used main `7de3c5e1`, `maf-sandbox-acas` 0.21.0, `azure-containerapps-sandbox` 0.1.0b4, API `2026-02-01-preview`, host Python 3.12 and guest Python 3.13. A temporary HTTP origin behind HTTPS ingress accepted arbitrary methods, returned 200 and recorded the received method and synthetic request content. Guest sandboxes used full traffic inspection, deny-default policy and one allow rule for the scoped host.

| Policy | Guest method | Result |
|---|---|---|
| All methods | GET, POST, `get`, `Get`, PROPFIND, `propfind`, `PropFind`, X-CUSTOM | 200 |
| GET only | GET, `get`, `Get` | 200 |
| GET only | POST, PROPFIND, `propfind`, `PropFind`, X-CUSTOM | 403 with service denial reason |
| `get` only | GET, `get`, `Get` | 200 |
| PROPFIND only | PROPFIND, `propfind`, `PropFind` | 200 |
| PROPFIND only | GET, POST, `get`, `Get`, X-CUSTOM | 403 with service denial reason |

The service admits custom methods but matches case-insensitively. The outgoing trace retained `get` and `Get`; their origin receipts contained GET, so the complete rewrite path cannot be attributed to one proxy. The custom-method result is unambiguous: a PROPFIND-only rule admitted all three spellings. A GET request carrying `issue377-synthetic-body` reached the origin under GET-only policy, proving method scope does not make a channel body-free or read-only.

The ordinary GET/POST egress probe would pass this service: allowed GET reaches, scoped POST is denied and the all-method control reaches. It does not establish literal case semantics, redirects, rule precedence, wildcard overlap, every token, or the non-TLS path. Those gaps keep `EGRESS_METHODS` withheld. Core now uses uppercase method tokens and does not express case distinctions; that removes the old case-mismatch objection but does not close the unmeasured surfaces.

### Reuse and policy changes

A live reuse measurement acquired a host-wide allowlist, changed the same key/kind to another host, then changed it to CLOSED. Every acquire returned the same physical instance, and the original endpoint remained reachable. The service received no replacement policy. This surviving-instance mismatch is unsafe for a changed egress declaration, so the implementation records mode and normalized host set with the held instance and raises `AcasEgressPolicyConflict` before resume. Equivalent policies reuse regardless of host order or case. A caller must dispose the kind successfully or use another key before changing policy; automatic replacement could disrupt another caller. Capture invalidation is a separate deletion path and requires successful deletion before replacement.

### Contract boundary

The backend declares `{Egress.ALLOWLIST, Egress.CLOSED}` and never `UNRESTRICTED`; ACAS cannot express an unrestricted mode because the service policy is deny-by-default. A method-scoped rule therefore refuses at router preflight and direct backend acquisition with `SandboxCapabilityNotSupported`. The service measurement is evidence for a future capability, not its implementation acceptance.

When method scope is eventually reconsidered, acceptance must cover an endpoint that accepts both GET and POST, a control policy where POST reaches, a scoped policy where GET reaches and POST is denied, custom methods, case behavior, redirects, precedence, wildcard overlap and the non-TLS path. A backend must declare enforceable tokens and compare policy changes during warm reuse; a raw SDK method field is not enough.

## Working-directory preparation authority

Measured 2026-09-21 against a disposable dev sandbox group (Sweden Central), `azure-containerapps-sandbox 0.1.0b4`, on prebuilt `python-3.13`, imported `bicep-sandbox:0.46.1` (root guests) and imported `python-nonroot:3.13` (guest uid 10001).

### The service stat carries no ownership

A raw `files/stat` payload carries `name`, `path`, `size`, `mode`, `isDir`, `isSymlink` and `modifiedTime` — no owner, uid or gid. `mode` is the low nine permission bits with the sticky and setuid bits stripped: `/tmp`, really `1777`, reports `511` (`0o777`). So core's `path_ancestors_are_host_owned`, which needs `(uid, mode)` per ancestor, cannot be answered from a stat; `mode` alone cannot tell a root-owned `0755` directory from a guest-owned one, nor see the sticky bit that makes `/tmp` safe to create under.

### Why creation runs as the guest

The data plane creates every directory root-owned (#722), and preparation's ancestry check and its creation are separate service calls, so a parent replaced between them could redirect a host-authority `mkdir` to a protected location the guest could not reach. With no ownership to bound it by, the reach rule cannot license a host-authority creation here. Preparation therefore issues `mkdir -p` as the guest over exec for missing directories and refuses if that creation fails (#1339). Existing directories are preserved without checking whether the guest could create or write to them. The kernel applies the guest's permissions to the syscall, so a redirected creation can only land where the guest could already have made one. This does not restore the host-authority write fallback #1266 removed.

### Live evidence

On `python-nonroot:3.13` the guest (uid 10001) could not `mkdir` under `/`; acquiring with a base under the root-owned `/maf-sandbox` tree was refused, while a `/tmp` base was prepared and usable. Root guests (`python-3.13`, `bicep-sandbox:0.46.1`) prepared, reused and repaired a nested `/maf-sandbox/...` base. The full live suite passed 58/58 and left zero sandboxes in the group.

## Remaining limits and operational boundaries

- Live credential acceptance for a particular delegated token, audience, RBAC grant or cleanup principal remains deployment-specific. The offline credential suite does not establish distributed performance or Azure authorization.
- Client capacity is local to event loops and replicas; it is not a global token-minting limit. Hosts needing global coordination must provide it.
- ACAS native reads and listings retain a measured concurrent-swap residual. A future atomic service operation is needed to bind path resolution to a trusted directory boundary.
- ACAS does not declare `SNAPSHOT` or `RECLAIM`; cleanup uses disposal. Snapshot timing and lifecycle measurements did not establish a consistent benefit sufficient to change that decision, and snapshot quota/storage pricing remains unverified.
- The service's file metadata lacks a regular-file type signal beyond “not directory and not symlink”; FIFO reads are bounded by timeout rather than proven safe.
- Method-scoped egress remains unsupported while redirects, rule precedence, wildcard overlap and non-TLS behavior are unmeasured.
- All live measurements require disposable ACAS groups, explicit label-based cleanup and post-run inventory. No research result should be read as a universal claim about every ACAS region, image, API version or service rollout.
