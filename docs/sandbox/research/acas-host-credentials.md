# Host-selected ACAS credentials across requests and replicas

> Research for [#1169](https://github.com/sokolaidev/maf-extensions/issues/1169), under [#567](https://github.com/sokolaidev/maf-extensions/issues/567), recorded on 2026-09-13. This preserves the source analysis and offline evidence, followed by the implementation disposition. The linked contract owns the implemented API; the baseline observations and original recommendations remain dated evidence.

Source baseline: [`c201baa8`](https://github.com/sokolaidev/maf-extensions/tree/c201baa8ac72c70a9d3226aa4000e8f75c213da7). The inspected environment used Python 3.13.12, `azure-containerapps-sandbox` 0.1.0b4, `azure-core` 1.41.0 and `azure-identity` 1.25.3. No Azure requests or real credentials were used.

## Recommendation

Implementation disposition for #1169: the [ACAS credential contract](../acas-credentials.md) defines the implemented API. `AcasCredentialRequest` selects acquire or cleanup by trusted scope/thread/key; `AcasCredentialBinding` captures a non-secret authority reference, generation and fresh-credential factory. Request exchange is retained. Cleanup on another replica uses an explicit host resolver rather than persisted credential objects or secret-bearing labels. Immediate failure cleanup keeps the acquired grant; later disposal resolves cleanup authority anew. Shutdown reports incomplete cleanup with `AcasClientCloseError`. The source observations below remain evidence for the pinned baseline, not a description of the implementation after this change.

Define authority selection and client ownership together for #1169. Preserve the issue's per-request exchanged-authority requirement unless the maintainer explicitly defers it. A cache keyed by `(loop, scope)` alone cannot meet that requirement when two callers or grants share a scope.

Multiple host replicas are a requirement. SDK clients and their leases remain local to a process and event loop. The host's authority-selection policy and cleanup references must work independently on every replica, including after the creating replica has disappeared. Do not require sticky routing or the original in-memory credential to delete a sandbox.

## Observed baseline and implications

Source links below are pinned to the inspected commit; line numbers describe that baseline.

| Evidence | Current behavior | Implication for #1169 |
|---|---|---|
| [_backend.py:1280](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L1280) | `_group_client()` accepts no caller context and constructs `DefaultAzureCredential`, caching by loop only. | No custom authority selector, caller partition, rotation generation, client capacity or idle eviction. |
| [_backend.py:1255,1302](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L1255) | The cache holds loop objects strongly; `aclose()` loops over every client and credential on the calling loop, then clears the table. | Closed-loop retention, concurrent shutdown and cross-loop ownership need explicit handling. If SDK client construction fails after credential creation, there is no credential cleanup path. |
| [_backend.py:566,574,815,860,1026,1178](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L566) | `_AcasSandbox` retains an SDK sandbox client and uses it for execution, streaming and files. | Evicting its parent pipeline would invalidate a still-usable wrapper. Rebinding must cover all SDK entry points, not only acquisition. |
| [_backend.py:1450](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L1450) | Warm resume catches any ordinary exception and moves toward replacement creation. | Authentication/authorization failures must propagate as authority failures, not be treated as evidence that a resource vanished. This matters when a later caller differs from the creator. |
| [_backend.py:1527](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L1527) | Lifecycle configuration errors are logged and acquisition continues. | A supplied credential denied a required operation must not silently look like successful setup. Coordinate credential error handling here with #1170's stricter attached-authority lifecycle contract. |
| [_backend.py:1788,1920,1982,2128](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L1788) | Disposal/purge uses service labels and a new lookup of the loop's client; retry ledgers retain IDs and ownership keys. | Discovery already works across replicas, but no creator authority reference is retained or reconstructed. Changing only acquire authentication leaves cleanup under the app credential. |
| [_backend.py:742,783](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py#L742) | Exec failure schedules a shielded deletion task using the wrapper's SDK client. | The cleanup task needs an explicit authority binding and lease through deletion/polling. Request-context propagation alone does not survive another replica or restart. |
| [_protocol.py:775,1745](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox/src/maf_sandbox/_protocol.py#L775) | `SandboxKey` identifies scope, conversation, agent and optional call; `CallerContext` provides scope/thread/file callbacks. | Neither is a per-request authority identifier. `call_id` is empty for conversation-scoped work. A scope can cover several users, and one user can present different grants. |
| [test_acas_backend.py:176](https://github.com/sokolaidev/maf-extensions/blob/c201baa8ac72c70a9d3226aa4000e8f75c213da7/packages/maf-sandbox-acas/tests/test_acas_backend.py#L176) | Most offline backend tests replace `_group_client()` with a lambda returning a fake. | Existing lifecycle tests do not cover real credential construction, auth partitioning, eviction or client ownership. Add a dedicated client-manager suite. |

## The proposal's unresolved scope reduction

The [identity-axis proposal, Pillar F](identity-axis-proposal.md#pillar-f--the-control-plane) proposes `Callable[[str], AsyncTokenCredential]`, one client per `(loop, scope)`, bounded capacity and operation leases. Its wrapper-rebinding and lease ideas are useful. Its explicit deferral of per-call exchange is not an accepted replacement for the issue's original per-request requirement.

Inferring a new connection pool per call from the proposal's client-per-call statement would be too strong. The installed SDK permits a supplied transport. Distinct credentials still need isolated authentication-policy state; any shared transport would require a separately verified ownership design. Do not depend on transport sharing in the first implementation.

## Offline evidence

Three probes ran without contacting Azure or using real tokens:

1. **A context-reading credential is unsafe behind a shared auth policy.** The installed `AsyncBearerTokenCredentialPolicy` was called twice under different synthetic caller contexts. It called the credential only once, and the second request reused the first request's authorization header. The sandbox SDK constructs this policy. This demonstrates a hazard in a proposed dynamic-credential shortcut; it is not evidence that today's fixed-app backend leaks one real caller's token to another.
2. **Shutdown does not drain operations.** A fake client was placed in the actual backend cache with an operation pending. Calling the actual `aclose()` closed the client before that operation completed.
3. **Shutdown does not honor owning loops.** A client and credential were cached on one event loop, then the actual backend `aclose()` ran on another. Both fake resources observed closure on the wrong loop. Whether a particular Azure transport raises or leaks in that situation was not live-tested.

Existing focused lifecycle/error/concurrency tests: **29 passed, 414 deselected, 2 warnings**. The selection was `resume_failure_logs_status_and_body or group_client_that_cannot_be_built or cross_loop or overlapping_event_loops or purge_refuses_acquire_on_another` in the ACAS backend suite. These confirm the inspected baseline, not completion of #1169. No full repository gate was run for the analysis.

## Recommended authority model

The following records the design recommendation. The linked implementation contract above owns the API names and resolves the choices that remained open here.

1. **Resolve an explicit host-owned authority binding.** At acquire or a direct backend operation, a configured resolver receives the captured ownership key and operation purpose. It may read trusted host request context, but must not take a principal, credential or cache identifier from guest arguments. Return an immutable, non-secret authority reference, a revision/generation, and a way to create an async credential on the owning loop. Prefer a creation recipe to an already shared loop-bound credential.
2. **Partition authentication state by the actual binding.** Cache entries should include loop, resource/tenant partition, authority reference and generation. A stable scope principal may reuse a binding; two requests may share only when the host explicitly declares them equivalent. A request-specific binding remains distinct even inside the same scope. Never use a raw bearer token as a cache key or log field.
3. **Capture the binding in the acquired wrapper.** Each acquire can return a wrapper bound to that request's authority while preserving existing `SandboxKey` resource-sharing semantics. Do not change the resource key merely to separate token caches. Later operations on that wrapper must not switch principals because an ambient ContextVar changed.
4. **Borrow clients per operation.** The wrapper stores resource ID, captured scope and binding, not a permanent SDK sandbox client. A lease rebuilds the client after eviction. Keep it until polling, async iteration/streaming, response consumption and cancellation cleanup have completed. Nested helper calls must reuse an existing lease or otherwise avoid deadlock when capacity is one.
5. **Make credential ownership explicit.** A credential created for a cache entry is owned and closed by that entry after its last operation. A host-shared credential requires an explicitly different ownership contract; accepting arbitrary singleton returns while unconditionally closing them is unsafe. Rotation selects a new generation and drains the old entry without changing an in-flight operation's authority. No custom-provider failure falls back to DefaultAzureCredential.

The default app-authority path remains compatible when no custom resolver is configured. Do not introduce Azure credential types into the core protocol. A backend-specific resolver and wrapper binding can carry the new behavior; a new mandatory CallerContext field is not established as necessary by this analysis.

## Multiple replicas and cleanup

| Concern | Required behavior |
|---|---|
| Request lands on replica A, next request on B | Both select authority using the same trusted host policy. Each creates its own SDK clients; clients/transports never travel between replicas. |
| A dies; B receives disposal | B obtains an authorized cleanup binding from the captured target scope/reference or a durable host-side mapping. Service labels discover resources; the original replica's registry and credential cache are optional accelerators. |
| Original request assertion expires | The host must supply a reconstructible/refreshable cleanup grant, explicitly configure a cleanup principal, or accept a reported cleanup failure and retry. Exact ephemeral caller authority cannot be recreated merely from a scope string. |
| Cleanup principal differs from request principal | This is an explicit host policy, never an automatic escalation after a 401/403. Its permissions and target restrictions must be documented and tested. If this is unacceptable, configuration must require durable caller-grant recovery and refuse when unavailable. |
| Process restart and label-only discovery | A local map from scope to credential is insufficient. Long scope labels can be irreversible digests; a general sweeper cannot recover the original caller or assertion from them. A trusted host registry or independently configured recovery authority is required. |
| Capacity | Per-loop capacity C across R replicas with L loops each allows up to R × L × C entries. In-flight and provisioning entries count. Local limits do not provide a global minting limit; the host must coordinate one if needed. |
| Eviction and rotation | Evict only idle entries on their owning loop. An eviction affects no remote replica and deletes no sandbox. Every replica must recognize the selected binding generation according to the host's rotation policy. |
| Replica shutdown | Stop new leases, drain active operations within a bound, and close resources on their owning loops before those loops stop. A stopped owning loop is an incomplete-cleanup condition, not successful closure. |
| Concurrent creation/deletion | Client-cache locks coordinate no remote replica. Preserve the existing host obligations: use CALL isolation for a conversation served concurrently across routers, and stop new work across replicas before conversation purge. Credential selection does not add a distributed sandbox lease. |

The decisive invariant is that **a resource's recoverability cannot depend on a credential object stored only in the process that created it**. Captured request context is useful for same-process asynchronous cleanup, but is not a multi-replica recovery design.

## Implementation sequence and acceptance

1. Record the per-request binding and independent cleanup-authority contract. Do not mark scope-only delivery as closing #1169. Identify the host integration used to reconstruct bindings on another replica.
2. Implement an ACAS-local client manager with bounded capacity/waits, single-flight construction, failure/cancellation cleanup, operation leases, generation rotation and owner-loop shutdown.
3. Rebind all wrapper and backend SDK operations, including file payload routes, image lookup, resume/create/polling, refused acquisition cleanup, exec invalidation, disposal and purge. Narrow warm-resume recovery so permission failures cannot create replacements.
4. Test two callers in one scope, credential rotation, full-cache waits/cancellation, single-capacity nested operations, warm wrappers after eviction, construction failure, shutdown while busy, and multiple event loops.
5. Add **two independent backend instances** against one fake service with different local caches. Test create on A then dispose/purge on B; discard A and all its memory; ensure B still cleans up through the explicit resolver. Include expired request context, denied cleanup grants, a retained failed deletion and retry on another instance, duplicate cleanup, and group-wide discovered resources with no local records.
6. Verify Azure permissions and credential acceptance with approved live setup before claiming exchanged caller authority works end to end. A Python AsyncTokenCredential-compatible object alone does not prove the service accepts its audience, delegated token or RBAC grant. Keep `ATTACHED_IDENTITY` withheld; [#1170](https://github.com/sokolaidev/maf-extensions/issues/1170) owns guest-attached authority and its platform limitations. This work does not deliver the host-tools authority path in [#566](https://github.com/sokolaidev/maf-extensions/issues/566) or guest call credentials in [#757](https://github.com/sokolaidev/maf-extensions/issues/757).

## Sources and limits

- [Issue #1169](https://github.com/sokolaidev/maf-extensions/issues/1169) and the pinned repository source cited above.
- Installed SDK `SandboxGroupClient.__init__`, `get_sandbox_client`, `close`, and `_build_async_pipeline`: child sandbox clients share the group's pipeline; the group closes its pipeline separately from credential ownership.
- Installed azure-core `AsyncBearerTokenCredentialPolicy.on_request`: cached-token behavior was exercised directly with synthetic credentials.
- [Azure sandbox Python quickstart](https://learn.microsoft.com/en-us/azure/container-apps/sandboxes-quickstart-python-sdk): the SDK takes a host credential and requires separately granted service access.
- [Azure Identity async credentials](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/README.md): async credential closure is an explicit lifecycle responsibility.
- [Tool-call lifetime and concurrency](../tool-call.md) and the `SandboxBackend.dispose_scope` contract document the existing cross-replica lifecycle obligations.

No live authorization, delegated-token acceptance, distributed deployment, eviction implementation or performance/cost benchmark was performed. The shutdown probes use fake resources to establish when and where the current backend calls close; they do not claim a measured production outage.
