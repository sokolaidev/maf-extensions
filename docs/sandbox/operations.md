# Operating sandbox cleanup

The deployment owns cleanup after an application process dies. Backends and operator scripts provide resource discovery and deletion; the deployment supplies the target, credentials, retention policy, schedule, and response to failures. The extension starts no daemon and requires no persistent router inventory. The argument for this boundary is recorded in [`research/orphan-cleanup-ownership.md`](research/orphan-cleanup-ownership.md).

## Ownership boundary

| Component | Responsibility |
| --- | --- |
| Host application and router | Request conversation disposal through the registered backends and report resources that remain |
| Backend or provider API | Discover resources from the service or engine, establish their identity and lifecycle state, and delete the selected resources |
| Deployment operator | Select the engine or group, authorize the retention policy, supply credentials, schedule independent executions, and respond to incomplete cleanup |

An operator program talks directly to a backend-specific helper or the provider API. The ACAS example uses the provider SDK; it does not reconstruct a router or attach an agent tool. Operator retention adds no `SandboxBackend` member or capability, and neither the router nor a kind starts a cleanup scheduler. A deployment can replace GitHub Actions with its existing scheduler without changing the extension.

## Purge and expiry

A restarted host can reconstruct its router and call `dispose_scope(scope, thread_id)`. Each registered backend discovers the conversation's resources from service labels. A backend removed from configuration needs its operator to finish cleanup through the original engine or sandbox group.

Retention cleanup supplements conversation purge and [per-call cleanup](tool-call.md); it does not replace either. A failed conversation deletion still needs the host's retry policy, rather than an assumption that waiting one day meets that deletion's requirements. Stopped retention also supplies no maximum running lifetime or deadline for revoking a sandbox's authority; the stronger attached-identity contract remains in [hosts.md](hosts.md).

Retention cleanup requires no conversation IDs. The example below authorizes deletion after a sandbox has been continuously stopped for over one day. Creation age does not establish inactivity, and a host crash alone does not make a running sandbox eligible. ACAS can also install service-side auto-suspend and auto-delete policies, but suspension alone preserves state, and a failed auto-delete configuration supplies no confirmed deletion timer. Docker and WSLC have no equivalent automatic deletion policy supplied by this suite.

## An ACAS cleanup example

[`scripts/cleanup_acas_sandboxes.py`](../../scripts/cleanup_acas_sandboxes.py) is a standalone operator program with its dependencies declared in PEP 723 metadata. It uses the Azure CLI login and four environment variables: `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, and `ACAS_SANDBOX_GROUP`. All four are required; there is no subscription-wide discovery or default group.

The selected group is the ownership boundary: **every sandbox in that group is considered regardless of labels or lifecycle policy, but only `Stopped` sandboxes with a sufficiently old `stateDetails.stoppedAt` qualify**. Running, transitioning, and unknown states are retained. A resume followed by another stop starts a new retention interval. Use a dedicated verification group, or choose a stopped retention period acceptable to every workload sharing the selected group. The program deletes sandbox instances only; disk images, explicit snapshots, volumes, and the group itself are outside its scope.

After configuring those variables and signing in with `az login`, preview sandboxes stopped for over 24 hours:

```bash
uv run --no-project scripts/cleanup_acas_sandboxes.py --stopped-for-hours 24
```

Apply that same policy:

```bash
uv run --no-project scripts/cleanup_acas_sandboxes.py --stopped-for-hours 24 --apply
```

The program inventories all pages before deleting anything. Missing or duplicate IDs and a failed listing prevent every deletion. A stopped sandbox with a missing, invalid, or timezone-free stop time is retained and reported as a failure while other valid candidates can still be deleted. This includes legacy ACAS records without `stateDetails`; the script never substitutes creation time or a locally remembered observation. It compares service stop times with the operator's UTC clock, retaining a sandbox exactly at the cutoff.

Immediately before deleting an immutable ID, the program reads it again and requires the same `Stopped` state and stop timestamp. A changed state or stop time retains that candidate. Deletion waits for the service to report absence. A concurrent removal is successful cleanup; any other failure is reported individually, other candidates are attempted, and another execution can retry what remains.

The pinned SDK's deletion API provides no conditional state check. A resume between the final read and delete can still race with this operator sweep. Deployments requiring an atomic guarantee must coordinate resumption with cleanup outside this script, or use a service-enforced stopped retention policy. The example is suitable for the dedicated live verification group, whose test resources are disposable; it is not a lease or a general activity detector.

The JSON result reports the stop-time cutoff, preview mode, inventory count, eligible IDs, confirmed deletions, resources already absent, candidates retained after rechecking, and failures. Exit status is nonzero for incomplete cleanup. `--summary` appends a Markdown summary for an operator's job report.

## GitHub Actions manages the live verification group

[`cleanup-live.yml`](../../.github/workflows/cleanup-live.yml) runs independently of the verification jobs at minute 37 of every hour, with a fixed 24-hour stopped retention period. It reuses the `live-verify` environment and its existing Azure OIDC federation. It creates no sandbox and uses no model or registry configuration. The group already used by live verification is the only group it selects through its configuration.

Actions → **Cleanup (live)** → **Run workflow** previews by default; turn off `dry_run` to delete. Scheduled executions apply the policy automatically. Concurrent cleanup executions are serialized and never cancel one another. Verification jobs may continue alongside cleanup: running sandboxes and sandboxes stopped for less than one day do not qualify. The workflow never stops a running sandbox to make it eligible. A sandbox that remains running indefinitely needs a functioning platform idle policy or operator attention.

The Actions log carries the JSON result, the job summary carries its counts, and a failed inventory, deletion, or job timeout makes the execution fail. Operators monitor those executions and rerun failures; no retry state is held by the application. GitHub schedules run from the default branch and may be delayed or dropped under load, and public-repository schedules can be disabled after inactivity. A 24-hour stopped retention period therefore does not promise destruction at exactly 24 hours: normally the next hourly sweep removes an eligible sandbox, and a missed or failed execution extends retention. See [GitHub's schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The workflow becomes scheduled when it reaches the repository's default branch. Copying the example to another deployment requires its own group configuration, Azure permissions, and federation. It cannot clean a developer's local Docker or WSLC engine from a GitHub-hosted runner without an explicitly configured connection to that engine.

## Status

| Decision | State | Tracking |
| --- | --- | --- |
| Infrastructure owns post-crash cleanup scheduling; an ACAS operator example manages the live verification group after one day continuously stopped | open — script and workflow implementation; scheduled activation and Actions verification require the default-branch deployment | [#1008](https://github.com/sokolaidev/maf-extensions/issues/1008) (open) |
| Backend-specific Docker and WSLC cleanup, and ACAS recovery for missing lifecycle policies | open — distinct from the group-wide operator example | [#1009](https://github.com/sokolaidev/maf-extensions/issues/1009) (open), [#1010](https://github.com/sokolaidev/maf-extensions/issues/1010) (open), [#1011](https://github.com/sokolaidev/maf-extensions/issues/1011) (open) |
