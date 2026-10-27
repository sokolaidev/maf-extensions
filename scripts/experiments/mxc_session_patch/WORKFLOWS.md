# MXC qualification workflows

The MXC experiments are source-only qualification tooling. Moving a dispatcher does not qualify a new runtime, convert retained checkpoints or establish production readiness. Existing candidate-specific reports and run links remain the evidence.

| Entry point | Purpose | Trigger |
| --- | --- | --- |
| [mxc-native.yml](../../../.github/workflows/mxc-native.yml) | Current native session/recovery, byte streams and bounded files on Linux/KVM and Windows/WHP | Manual |
| [openclaw-mxc.yml](../../../.github/workflows/openclaw-mxc.yml) | Official OpenClaw MXC host preparation and smoke comparison | Manual |
| [mxc-accounting.yml](../../../.github/workflows/mxc-accounting.yml) | Admission scaling with 0, 1,000 and 10,000 retained identities | Manual |
| [mxc-physical.yml](../../../.github/workflows/mxc-physical.yml) | Storage, accounting, retention and process controls on Linux and Windows | Relevant pull requests or manual |
| [mxc-remote.yml](../../../.github/workflows/mxc-remote.yml) | Ownership and 1 GiB checkpoint transfer on Linux and Windows | Relevant pull requests or manual |
| [mxc-volume.yml](../../../.github/workflows/mxc-volume.yml) | Real disposable filesystem exhaustion and recovery | Relevant pull requests or manual |
| [mxc-cross-runner.yml](../../../.github/workflows/mxc-cross-runner.yml) | Independent Linux host recovery with mutual TLS and fencing | Manual |

Dispatch `mxc-native.yml` with `mxc_v1=qualify`, `mxc_streams=true`, `mxc_files=true` and optionally `mxc_prelaunch=true` for the current native profiles. The stream and file jobs retain their separate kernel builds and consume artifacts from their own workflow run. Use `mxc_v1=resolve-lock` only to resolve dependencies; it does not qualify native execution. Input names are retained for callers, while the session job's display name no longer names a fixed MXC version. Dispatch `openclaw-mxc.yml` with `openclaw_mxc=true` and `openclaw_mxc_phase=smoke` or `prerequisites`.

## Retired entry points

The pre-1.0 `mxc-recovery` and `mxc-durability` jobs and inputs are removed from `tests.yml`. The current session runner executes the same `storage_probe` and `durability_probe` modules and additionally checks bounded output, remote publication/replay and incompatible-checkpoint refusal with recovery by the retained helper. The old helper, probes and reports remain available for historical reproduction and compatibility tests. Removing the old dispatchers does not promise that a current profile can restore a legacy checkpoint.

The historical [Unikraft comparison workflow](../unikraft_output/workflow.yml) is archived outside `.github/workflows/`. Its [reproduction instructions and measurements](../unikraft_output/README.md) remain available. The outcome is still to defer overlay removal; [#1957](https://github.com/sokolaidev/maf-extensions/issues/1957) requires an equivalent integrated candidate.

The ordinary package gate no longer schedules empty MXC qualification jobs. Its required aggregate still waits for every remaining validation shard. Offline MXC tests continue to run in the repository suite.

## Actions history retention

Prune only completed pull-request runs older than 30 days from the accounting, physical, remote and volume workflows, and only after verifying that the associated pull request is closed or merged. Keep runs referenced by research, reports, issue or review evidence; keep the latest successful evidence for each active platform/profile and all unresolved diagnostic failures. An empty or ambiguous pull-request association is not proof of eligibility. Keep manual cross-runner and upstream comparison runs. Do not bulk-delete shared `tests.yml` runs or rewrite Git history.

Before each cleanup, produce an explicit run-ID inventory and protect evidence references before deleting anything. Deleting a run also deletes its remaining artifacts. Most experiment report artifacts already expire after 14 days; run-history deletion chiefly reduces Actions clutter once those artifacts expire. This policy is a maintenance procedure, not a scheduled deletion job.

The 2026-10-27 audit queried the four eligible workflows for pull-request runs created before 2026-09-27 UTC. All four returned zero candidates, so no runs or artifacts were deleted. Historical native and diagnostic evidence remains retained under its existing GitHub retention settings.
