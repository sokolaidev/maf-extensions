# Manual platform remediation rehearsal

The [rehearsal workflow](../../.github/workflows/platform-image-remediation-rehearsal.yml) implements an isolated manual entry point for the [remaining qualification plan](platform-remediation-qualification-2026-11-09.md#remaining-qualification-plan). It qualifies current authenticated platform evidence and can exercise App delivery with a draft receipt PR while normal platform automation stays disabled. This mechanism has not yet been hosted-qualified. Positive Bicep and OS update cases, normal PR CI and production operation remain acceptance work under [#2517](https://github.com/sokolaidev/maf-extensions/issues/2517).

## Select a case

Dispatch from `main` after this workflow is merged. The [reviewed case manifest](../../scripts/platform_rehearsal_cases.json) defines the required finding type, planner outcome and aggregate result. The request receipt binds the manifest hash, source SHA, workflow run and attempt to the selected case.

| Case | Required authenticated finding | Required result |
|---|---|---|
| `current-rebuild` | Bicep/.NET `source-pin-sufficient`, with no proposed source edits | Ordinary builds and all inherited runtime/inventory checks pass; targeted findings absent; `release-needed` |
| `bicep-update` | Compatible Bicep/.NET patch producing a source update | Each selected finding is observed in a baseline and absent from every applicable candidate; aggregate `ready` |
| `apk-pinned` | Compatible vendor APK fix producing a source pin update | Signed vendor-repository installation, all affected builds/runtime/inventory checks, baseline matches and candidate removal; aggregate `ready` |
| `apk-unpinned` | Compatible vendor APK fix handled by the existing upgrade recipe, with no source edits | Vendor availability and ordinary rebuilt images prove targeted removal; aggregate `release-needed` |

Acquisition, planning, source checks, ordinary image builds, vendor checks and final qualification reuse the production implementations. The selected case must exist in current authenticated monitoring. Missing cases, deferred acquisition, stale inputs and unresolved targeted findings fail the rehearsal; they cannot produce a successful empty qualification. All affected profiles in the acquired plan are checked, including unrelated planned updates in an update case. Rebuild-only cases refuse plans containing source edits. Raw residual findings, including ignored matches, remain in the scan artifacts.

This version has no historical-fixture fallback. If current evidence lacks a compatible positive Bicep or APK case, that case remains pending. A historical replay still needs its own reviewed fixture identities and execution mode; offline unit-test fixtures do not qualify hosted package updates. The `current-rebuild` case allows App lifecycle work to proceed independently of that gap without changing production dependency pins.

## Run qualification and optional delivery

Leave `exercise_delivery=false` to perform read-only qualification. To exercise the App lifecycle, explicitly select `exercise_delivery=true` on the manual dispatch. The workflow uses the existing remediation App client ID/private key and Docker Sandboxes credentials described in the [platform contract](container-image-releases.md#bicep-and-os-package-remediation). It mints the App token only after all scan jobs, the source gate, aggregation and retention of qualification evidence succeed.

With delivery selected, the App changes only the tracked [test receipt](../../tests/fixtures/image_dependencies/platform-rehearsal-receipt.json) on `bot/platform-remediation-rehearsal-<run-id>`. The receipt binds the qualified result hash and retained artifact URL. It does not deliver the dependency proposal or alter the production remediation branch. The resulting PR is a draft titled `test: retain platform remediation rehearsal evidence`, marked **do not merge**. Keep it available for inspection; close it when its evidence has been reviewed. Human edits, a human-closed PR, an unexpected head or conversion out of draft prevent automated refresh.

For a new run, the harness deliberately stops the first PR-creation request after the signed branch write. It verifies the hosted orphan's App identity, signature, parents, tree and file hashes, records the signed commit, then exercises two read-only refusal probes against unsigned and altered copies of that commit response. These probes do not modify hosted ownership records. Recovery uses the shared publisher with current-evidence checks before writes. It creates one draft PR and repeats delivery, requiring the same PR URL and unchanged head. A later execution against an existing draft records `refresh-passed`; it does not claim another interruption/recovery exercise.

Production source, scoped release/assessment and latest-monitor freshness checks remain active throughout. Unrelated publication can proceed; a change that invalidates the acquired evidence causes refusal. Each run has an isolated branch and concurrency group. No catalogue writer lock is held for the duration of the builds, and this workflow neither publishes images nor changes `PLATFORM_IMAGE_REMEDIATION_ENABLED`.

## Inspect and retry

Artifacts are retained for 30 days and include the workflow attempt in their names:

- `platform-rehearsal-plan-<attempt>`: request, authenticated acquisition receipt, evidence and deterministic plan, or acquisition failure.
- `platform-rehearsal-<variant>-<profile>-<attempt>`: baseline/candidate build, vendor, runtime, inventory and scan evidence.
- `platform-rehearsal-qualification-<run-id>-<attempt>`: acquired input and all image results, plus `rehearsal-qualified.json` on success. This artifact is uploaded before App token creation.
- `platform-rehearsal-delivery-<run-id>-<attempt>`: delivery progress and any failure record. Successful `delivery-passed` includes the interruption's signed commit, identity, ownership, injected-read refusals, delivered PR/head and unchanged repeat. `refresh-passed` covers only an existing draft's refresh.

Inspect the records, not just the workflow conclusion. `rehearsal-qualified.json` states `productionRecovery: false`; delivery evidence states `ci: not-verified`. After creation, inspect the draft's normal PR workflows at its exact head and retain their URLs/results separately, including any approval hold or failure. A successful draft receipt delivery alone does not satisfy the normal-PR-CI acceptance criterion or qualify delivery of a real package-update diff.

Use **Re-run all jobs** to retry the same run. Downloaded receipts and artifacts are attempt-bound, so rerunning only failed jobs cannot reuse the previous attempt's acquisition. Fresh acquisition must still match the dispatch source; if `main` advanced, start a new manual run. A retry on the same run reuses its isolated branch and verifies signed ownership before recovery. Preserve failed attempt evidence when comparing a recovery. Unclaimed unsigned branches, key changes and human edits require investigation; the workflow never force-updates them.

Record hosted results separately from the dated [Bicep rebuild evidence](platform-remediation-qualification-2026-11-09.md). Automatic enablement, image publication and issue closure remain separate maintainer actions.
