# Host-selected ACAS credentials

ACAS control-plane credentials authenticate the host's SDK operations. They remain outside the guest and are independent of host-tool user credentials, guest-provisioned tokens and platform-attached managed identity. `AcasSandboxConfig.credential_resolver` selects this authority; omitting it retains `DefaultAzureCredential`.

ACAS supports managed identity configured on the sandbox group. The host owns that configuration; the adapter does not inspect its assignment on acquisition or require management-read permission. Guest token acquisition was measured for M1's tested API, image and group configuration, as [sandbox group identity](backends/acas.md#sandbox-group-identity) records. The host credential selected below is independent of that configured guest authority.

## Request and cleanup authority

The async resolver receives an `AcasCredentialRequest` with `scope`, `thread_id`, `operation`, and an optional `key`. The backend supplies these values from the host's `SandboxKey` or disposal target; guest arguments never select an authority. The resolver returns `AcasCredentialBinding(authority, generation, create_credential)`. `authority` and `generation` are nonempty, non-secret host references. The factory returns a fresh Azure `AsyncTokenCredential`, directly or through an awaitable, on the loop that will use it.

| Operation | Resolver input | Required host policy |
|---|---|---|
| `acquire` | Captured key, scope and thread | Resolve the current request's grant from trusted host context. Two callers in one scope can return different bindings. |
| `dispose` | Target key, scope and thread | Resolve an authorized cleanup grant without requiring the original request context. This also covers retained per-key deletion retries before acquire. |
| `dispose_scope` | Target scope and thread, `key=None` | Resolve authority for discovering and deleting that conversation's sandboxes across replicas. Retained scope-wide retries use this operation too. |

An acquired wrapper captures its binding. Subsequent exec, streaming and file operations use that binding even if the host's ambient request context changes. Immediate deletion after failed execution and cleanup of a refused cold acquire use the captured acquire authority; a later explicit disposal or retained retry resolves cleanup authority anew. A failed custom resolver, credential factory or permission check never selects the default credential as a fallback. Authentication failures and HTTP 401/403 on warm resume propagate without replacement creation. Those failures during lifecycle configuration refuse acquisition and attempt deletion, retaining failed deletion for recovery.

Capture the grant when resolving the binding: `create_credential` must reconstruct that captured authority after eviction or on another event loop, rather than read whichever request context is current when it eventually runs. Each returned credential belongs to its cache entry. Returning a shared credential singleton is unsupported; the backend closes each owned client and credential. An async factory owns and cleans any resources it allocates until it successfully returns its credential. Resolver and factory code must not block the event-loop thread.

## Replica-independent recovery

Every replica needs the same trusted authority-selection policy and access to the host's durable cleanup mapping or explicitly configured cleanup principal. A cleanup principal may differ from the request principal only by that explicit policy, with permissions restricted to the intended targets. A host requiring the original caller's authority must make its grant recoverable; expired assertions and scope labels alone cannot recreate it. When recovery is unavailable, disposal reports failure and retains local retry ownership. Another replica can rediscover surviving resources through service labels.

The backend does not persist bearer tokens or authority references in resource labels. Disposal resolves from scope/thread/key, not a creator's process-local credential object. A host needing per-creator recovery must maintain the corresponding durable mapping itself. Long scope labels can be irreversible digests; a group-wide operator sweep needs a trusted target registry or independently configured operator authority. Deleting a sandbox on another replica does not require sticky request routing or a surviving creator cache.

The credential pool supplies local client ownership, not distributed sandbox locking or exactly-once deletion. The host must still stop new work across replicas before conversation purge and follow the [tool-call concurrency contract](tool-call.md). Repeated discovery/deletion of an already absent sandbox remains safe. No guarantee is made that an expired or revoked caller grant can delete its former resources.

## Host wiring

This example receives the request binding through a host-owned context variable and delegates cleanup to a host service that must work independently on every replica. The context variable is only for active requests; `recover_cleanup` must use durable host state or an explicit cleanup identity.

```python
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from maf_sandbox_acas import (
    AcasCredentialBinding, AcasCredentialRequest,
    AcasSandboxBackend, AcasSandboxConfig,
)

def build_backend(
    endpoint: str,
    request_binding: ContextVar[AcasCredentialBinding],
    recover_cleanup: Callable[[str, str], Awaitable[AcasCredentialBinding]],
) -> AcasSandboxBackend:
    async def resolve(request: AcasCredentialRequest) -> AcasCredentialBinding:
        if request.operation == "acquire":
            return request_binding.get()
        return await recover_cleanup(request.scope, request.thread_id)

    return AcasSandboxBackend(AcasSandboxConfig(
        endpoint=endpoint,
        credential_resolver=resolve,
        max_clients_per_loop=32,
        client_wait_seconds=30,
        client_close_seconds=30,
    ))
```

The host obtains a binding from its authentication layer before running the workload and resets its context variable afterwards. The factory on that binding creates a new credential for the captured grant; it never returns the host's shared SDK credential. The backend's subscription, resource group and sandbox group settings still identify the service target. Supplying an `AsyncTokenCredential` does not establish that ACAS accepts a particular delegated token, audience or RBAC grant; validate that deployment separately.

## Capacity, rotation and shutdown

**Shutdown migration:** `aclose()` now raises `AcasClientCloseError` when SDK cleanup is incomplete; earlier versions logged and suppressed close failures. Hosts must handle this exception in their shutdown policy, keep owner loops running until closure completes, and retry retained resources where possible. Closing is terminal: construct a new backend if more work must be admitted afterwards. This contract change is released as a breaking change.

Each backend instance fixes its service target and partitions its SDK pipelines by `(event loop, authority, generation)`. Equal authority/generation values assert that the grants are interchangeable, including across scopes. The factory is not part of cache identity. Select a new generation when the grant or factory configuration changes. New bindings get separate authentication-policy state; existing wrappers keep their captured generation, so rotation does not promise immediate revocation of already admitted work or cached tokens. Old idle entries remain eligible for eviction.

`max_clients_per_loop` defaults to 32 and counts active, constructing and closing entries. The least recently used idle entry is closed on its owning loop before replacement. An operation lease lasts through SDK polling, response consumption and streaming cleanup; nested wrapper helpers share that lease. Wrappers retain resource IDs and authority bindings rather than SDK transports and rebuild after eviction. Cancellation of one construction waiter does not cancel another's lease; when the last waiter leaves unfinished construction, cancellation is requested and owned partial resources remain tracked through cleanup. New callers wait for that construction to finish, then share a successful result or start fresh after cleanup.

`client_wait_seconds` defaults to 30 and independently bounds resolver completion and client acquisition, including capacity waits and construction. It does not replace operation-specific exec/read deadlines. `client_close_seconds` defaults to 30 and bounds shutdown/closure. Capacity is per loop, not global: R replicas with L active owner loops each can hold up to R × L × capacity entries. A host needing a global credential-minting limit must enforce it separately.

Eager task factories are supported: construction and retirement suspend before resource work so ownership is registered before they can finish. Capacity and shutdown waiters share one notification bridge per loop and change signal; timeout or cancellation releases a waiter's request context without cancelling that shared signal or waiting for an unrelated active lease to return.

Call `await backend.aclose()` before stopping its owner event loops. It permanently refuses new leases, drains admitted operations, and dispatches resource closure to every still-running owner loop. It does not dispose sandboxes. `AcasClientCloseError` reports timeout, a stopped owner loop or failed resource closure; retained resources permit a later close attempt. Resume a stopped owner loop before retrying closure there. A cancelled close caller does not revoke already admitted work. Successfully closed resources are not closed again. `AcasCredentialError` reports resolver/construction/capacity failures without including potentially sensitive provider error text; disposal translates these into its existing incomplete-cleanup report.

The [research record](research/acas-host-credentials.md) contains the baseline findings and the implementation disposition. Tests exercise fake service replicas and the installed SDK authentication policy; live delegated-token acceptance and distributed deployment performance remain unverified.

## Status

| Item | Status | Tracked by |
|---|---|---|
| Host-selected authority across sandbox operations, bounded client ownership and replica-independent cleanup | implemented; release pending | [#1169](https://github.com/sokolaidev/maf-extensions/issues/1169) (closed) by [#1225](https://github.com/sokolaidev/maf-extensions/pull/1225) (merged) |
| Eager task progress and completed capacity-waiter reclamation | implemented; release pending | [#1233](https://github.com/sokolaidev/maf-extensions/issues/1233) (closed), [#1234](https://github.com/sokolaidev/maf-extensions/issues/1234) (closed) by [#1235](https://github.com/sokolaidev/maf-extensions/pull/1235) (merged) |
