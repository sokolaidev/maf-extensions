# Operating sandbox cleanup

The host owns cleanup while serving calls and deleting conversations. The deployment owns recovery after a host process dies. It supplies the target engine or group, credentials, retention policy, schedule and response to failures.

![Per-call cleanup belongs to the tool wrapper and router. Conversation deletion asks every registered backend to find and remove the conversation's resources. After host death, a deployment-owned operator discovers resources directly from the original engine or service and applies its retention policy. ACAS uses continuous stopped time, Docker uses an operator-selected creation age, and WSLC uses stopped time plus separate orphan-infrastructure rules. Failed or incomplete cleanup needs another attempt.](assets/cleanup-ownership.svg)

## Host disposal

| Router operation | Scope and result |
|---|---|
| `dispose(key)` | Every kind for a key, across registered backends; failures go to logs and observer events |
| `dispose_kind(key, kind, timeout=...)` | One kind across registered backends; returns `True` on a successful sweep and `False` on failure or timeout |
| `dispose_kind(..., instance_id=...)` | The exact physical instance on its serving backend; an absent ID cannot select a replacement |
| `dispose_unclean(key, kind=None, instance_id=None, timeout=...)` | Retry recorded cleanup targets, optionally narrowed by kind or instance |
| `dispose_scope(scope, thread_id)` | Discover and purge a conversation through every registered backend |

These methods do not drain active calls. The host must stop new work and coordinate active calls before deletion.

Kind disposal requires a finite positive timeout. The deadline includes waiting for the per-key, per-event-loop disposal lock and the backend sweep. Cancellation propagates.

A failed ordinary host sweep does not create an unclean-key refusal. Failed exact-instance disposal retains the target and refuses the key unless the host chose `FailedReclaimPolicy.KEEP`. A successful retry clears only the targets it covered; another target or a newer failure keeps the key refused.

Without recorded targets or selectors, `dispose_unclean` sweeps all registered backends. See [call cleanup](tool-call.md#cleanup-as-a-consequence) for refusal and recovery rules.

## Purge and retention

A restarted host can reconstruct a router and call `dispose_scope`. Service-backed adapters discover resources by ownership labels. A backend removed from configuration still needs cleanup through its original engine or group.

Retention supplements conversation purge and per-call cleanup. It does not meet a failed deletion request merely by promising a later sweep. It also does not establish a maximum running lifetime or revoke attached authority on a deadline.

| Backend | Operator policy |
|---|---|
| ACAS | Service lifecycle policies, a stopped-retention sweep, or recovery of labelled instances missing effective auto-delete |
| Docker | `reap(max_age)` uses an operator-selected maximum creation age |
| WSLC | `reap(stopped_for, scope=...)` uses continuous stopped time; orphan infrastructure has separate age rules |

ACAS suspension retains state. A failed auto-delete configuration leaves no confirmed deletion timer. Docker and WSLC have no automatic deletion timer supplied by this suite.

For WSLC maintenance, stop acquisitions, restarts and other resource changes in the selected scopes. Name-based network deletion has no atomic identity or retention check. Prevent overlapping sweeps and monitor failures. See [operator retention](backends/wslc.md#operator-retention).

## ACAS stopped-retention example

[`cleanup_acas_sandboxes.py`](../../scripts/cleanup_acas_sandboxes.py) uses Azure CLI login and requires all four environment variables:

- `ACAS_SANDBOX_ENDPOINT`
- `ACAS_SANDBOX_SUBSCRIPTION_ID`
- `ACAS_SANDBOX_RESOURCE_GROUP`
- `ACAS_SANDBOX_GROUP`

The selected group is the ownership boundary. Every sandbox in it is considered, regardless of labels. Only a `Stopped` sandbox with a sufficiently old `stateDetails.stoppedAt` is eligible. Running, transitioning and unknown states stay untouched.

After `az login`, preview instances stopped for over 24 hours:

```bash
uv run --no-project scripts/cleanup_acas_sandboxes.py --stopped-for-hours 24
```

Apply the same policy:

```bash
uv run --no-project scripts/cleanup_acas_sandboxes.py --stopped-for-hours 24 --apply
```

| Check | Result |
|---|---|
| Inventory all pages | Listing failure or missing/duplicate IDs prevents every deletion |
| Validate stop time | Missing, invalid or timezone-free timestamps are retained and reported as failures |
| Compare with UTC cutoff | Exactly at the cutoff is retained; creation time is never a substitute |
| Re-read the immutable ID | State and stop time must still match before deletion |
| Wait for absence | Confirmed removal or already absent is success; other failures are reported individually |

A resume followed by another stop starts a new retention interval. The script deletes instances only, not images, snapshots, volumes or the group.

The SDK has no conditional state-and-delete operation. A resume can race with the final deletion. Coordinate resumption externally when an atomic guarantee is required. The repository example targets a dedicated group of disposable verification resources.

The JSON result includes counts, eligible IDs, confirmed removals, retained candidates and failures. Incomplete cleanup exits nonzero. `--summary` appends a Markdown job report.

## Scheduled verification cleanup

[`cleanup-live.yml`](../../.github/workflows/cleanup-live.yml) runs at minute 37 each hour with a 24-hour stopped-retention policy. It uses the `live-verify` environment, Azure OIDC and the configured verification group.

Manual runs preview by default; disable `dry_run` to delete. Scheduled runs apply the policy. Cleanup runs are serialized. The workflow never stops a running sandbox to make it eligible.

Operators monitor the JSON log, job summary and failure status, then retry incomplete work. Missed, delayed or failed runs extend retention; the policy does not promise deletion at exactly 24 hours.

Each deployment supplies its own target, permissions and scheduler. A GitHub-hosted runner cannot clean a local Docker or WSLC engine without a configured connection.

## Status

| Decision | State | Tracking |
|---|---|---|
| Kind disposal and targeted cleanup retry | Implemented | [#1006](https://github.com/sokolaidev/maf-extensions/issues/1006) (closed); [#1028](https://github.com/sokolaidev/maf-extensions/pull/1028) (merged) |
| Deployment-owned recovery and scheduled ACAS stopped retention | Implemented; deployment monitors executions | [#1008](https://github.com/sokolaidev/maf-extensions/issues/1008) (closed); [#1014](https://github.com/sokolaidev/maf-extensions/pull/1014) (merged) |
| Docker maximum-age cleanup | Implemented | [#1009](https://github.com/sokolaidev/maf-extensions/issues/1009) (closed); [#1012](https://github.com/sokolaidev/maf-extensions/pull/1012) (merged) |
| WSLC stopped retention and orphan cleanup | Implemented | [#1010](https://github.com/sokolaidev/maf-extensions/issues/1010) (closed); [#1015](https://github.com/sokolaidev/maf-extensions/pull/1015) (merged) |
| ACAS recovery for missing lifecycle policies | Implemented | [#1011](https://github.com/sokolaidev/maf-extensions/issues/1011) (closed); [#1022](https://github.com/sokolaidev/maf-extensions/pull/1022) (merged) |
