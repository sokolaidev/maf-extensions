# Host-selected ACAS credentials

`AcasSandboxConfig.credential_resolver` chooses the credential for the host's ACAS SDK operations. Without a resolver, the backend uses `DefaultAzureCredential`.

This credential stays in the host. It is separate from [sandbox group identity](acas.md#sandbox-group-identity), guest-provisioned tokens and host-tool user credentials.

![Active requests supply a captured authority binding through trusted host context. Later cleanup resolves a binding from durable host policy using the target scope, thread and key. Both paths use a credential factory and an SDK client pool partitioned by event loop, authority and generation. An acquired sandbox wrapper retains its binding for later operations. SDK credentials authenticate calls to ACAS and never become guest credentials; group identity is configured separately.](../assets/acas-credential-flow.svg)

## Select authority

The async resolver receives `AcasCredentialRequest(scope, thread_id, operation, key)`. These values come from host keys and cleanup targets, never guest arguments.

It returns `AcasCredentialBinding(authority, generation, create_credential)`. Authority and generation are nonempty, non-secret references. The factory creates a fresh `AsyncTokenCredential`, directly or through an awaitable, on the loop that uses it.

| Operation | Host responsibility |
|---|---|
| `acquire` | Capture the active request's grant from trusted context. |
| `dispose` | Recover an authorized cleanup grant for the target key without the original request. |
| `dispose_scope` | Recover a grant for the target scope and thread; `key` is `None`. |

An acquired wrapper keeps its binding for exec, files and streaming, even when ambient request context changes. Immediate cleanup of a failed execution or refused cold acquire uses that captured binding. Later explicit cleanup and retained retries resolve authority again.

A failed custom resolver, factory or permission check never falls back to the default credential. Authentication failures and 401/403 responses on warm resume propagate without creating a replacement. During lifecycle configuration, they refuse acquisition and attempt deletion.

The factory must recreate the captured grant after eviction or on another loop. It must not read whichever request happens to be current then. Each returned credential belongs to one pool entry; shared credential singletons are unsupported. Factories clean their partial resources until ownership is returned. Resolver and factory code must not block the event loop.

## Recover cleanup across replicas

All replicas need the same trusted cleanup policy and durable mapping, or an explicit cleanup principal. A different cleanup principal is allowed only through that policy, with permissions limited to intended targets.

Resource labels do not store tokens or authority references. Scope labels may be irreversible hashes. Expired assertions and labels cannot recreate a caller's grant, so operators need their own target registry or authority policy.

Failed recovery reports incomplete cleanup and retains local retries. Another replica can rediscover surviving sandboxes through service labels. The client pool supplies no distributed lock or exactly-once deletion guarantee; the host must stop new work across replicas before purging a conversation.

## Host wiring

Use a context variable for active requests and a separate cleanup resolver backed by durable state or explicit operator authority.

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

Set the binding before running work and reset the context variable afterwards. Its factory creates a fresh credential for the captured grant. Validate the deployment's token audience, delegated-token support and RBAC separately; accepting an SDK credential object does not prove the service accepts that grant.

## Client pool and rotation

Each backend fixes its service target. Clients are partitioned by `(event loop, authority, generation)`. Equal authority/generation values promise interchangeable grants, including across scopes. The factory itself is not part of the cache key.

Use a new generation when grants or factory configuration change. Existing wrappers keep their captured generation, so rotation does not immediately revoke admitted work or cached tokens.

| Setting | Default | Scope |
|---|---|---|
| `max_clients_per_loop` | 32 | Active, constructing and closing entries per owner loop |
| `client_wait_seconds` | 30 | Resolver completion and client acquisition, each bounded separately |
| `client_close_seconds` | 30 | Client closure and shutdown |

An idle entry is closed on its owner loop before replacement. Leases cover polling, response reading and stream cleanup; nested helpers share a lease. Operation-specific exec/read deadlines still apply. Across R replicas with L loops, capacity can reach R × L × the configured limit.

One cancelled construction waiter does not cancel other waiters. When the last leaves, unfinished construction is cancelled and partial resources remain tracked until cleanup. Eager task factories are supported. Waiting tasks release request context when cancelled or timed out.

## Shutdown

Call `await backend.aclose()` before stopping owner event loops. It permanently refuses new leases, drains admitted operations and closes clients and credentials on their owner loops. It does not dispose sandboxes.

`AcasClientCloseError` reports incomplete closure, including timeout or a stopped loop. Retained resources can be retried; restart a stopped owner loop before retrying there. Successfully closed resources are not closed again. Cancelling the close caller does not revoke admitted operations.

`AcasCredentialError` reports resolution, construction and capacity failures without sensitive provider text. Disposal translates it into the normal incomplete-cleanup report.

## Status

| Area | State | Reference |
|---|---|---|
| Authority selection, bounded clients and shutdown | Implemented | [Package README](../../../packages/maf-sandbox-acas/README.md) |
| Cross-replica cleanup policy | Host responsibility; tested with fake replicas | [Operations](../operations.md) |
| Live delegated-token acceptance and distributed performance | Deployment validation required | [ACAS evidence](../research/acas-backend.md) |
