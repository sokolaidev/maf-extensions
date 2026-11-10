# Manual platform remediation rehearsal

The [rehearsal workflow](../../.github/workflows/platform-image-remediation-rehearsal.yml) implements an isolated manual entry point for the [remaining qualification plan](platform-remediation-qualification-2026-11-09.md#remaining-qualification-plan). It qualifies current authenticated platform evidence or an explicitly selected historical rebuild case and can exercise App delivery with a draft receipt PR while normal platform automation stays disabled. [Hosted historical rebuild, App delivery/recovery and the draft receipt PR's normal CI passed on 2026-11-10](platform-remediation-qualification-2026-11-10.md). Positive Bicep and OS update cases, actual dependency-diff delivery and production operation remain acceptance work under [#2517](https://github.com/sokolaidev/maf-extensions/issues/2517).

## Select a case

Dispatch from `main` after this workflow is merged. The [reviewed case manifest](../../scripts/platform_rehearsal_cases.json) defines the required finding type, planner outcome and aggregate result. The request receipt binds the manifest hash, source SHA, workflow run and attempt to the selected case.

| Case | Required authenticated finding | Required result |
|---|---|---|
| `current-rebuild` | Bicep/.NET `source-pin-sufficient`, with no proposed source edits | Ordinary builds and all inherited runtime/inventory checks pass; targeted findings absent; `release-needed` |
| `bicep-update` | Compatible Bicep/.NET patch producing a source update | Each selected finding is observed in a baseline and absent from every applicable candidate; aggregate `ready` |
| `apk-pinned` | Compatible vendor APK fix producing a source pin update | Signed vendor-repository installation, all affected builds/runtime/inventory checks, baseline matches and candidate removal; aggregate `ready` |
| `apk-unpinned` | Compatible vendor APK fix handled by the existing upgrade recipe, with no source edits | Vendor availability and ordinary rebuilt images prove targeted removal; aggregate `release-needed` |
| `historical-rebuild` | Pinned historical Bicep/.NET `source-pin-sufficient` findings, plus separate authenticated current delivery authority | Fresh ordinary builds and runtime/inventory checks for all three Bicep profiles; targeted findings absent; `release-needed`; evidence explicitly labelled historical replay |

Acquisition, planning, source checks, ordinary image builds, vendor checks and final qualification reuse the production implementations. Except for the explicit `historical-rebuild` case, the selected case must exist in current authenticated monitoring. Missing cases, deferred acquisition, stale current inputs and unresolved targeted findings fail the rehearsal; they cannot produce a successful empty qualification. All affected profiles in the acquired plan are checked, including unrelated planned updates in an update case. Rebuild-only cases refuse plans containing source edits. Raw residual findings, including ignored matches, remain in the scan artifacts.

There is no automatic fallback from a missing current case. Select `historical-rebuild` explicitly to exercise the App lifecycle when current published images no longer contain the old Bicep findings. Positive Bicep/APK update cases still require compatible findings and remain separate acceptance work; this rebuild replay proposes no dependency update.

## Historical rebuild fixture

The case manifest pins [monitor 42613419444, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42613419444/attempts/1), producer `3117c76c9c51bcf5ebe209b62de8221ea6a690c0`, and the immutable [monitor evidence release](https://github.com/sokolaidev/maf-extensions/releases/tag/image-monitor-42613419444-1). SHA-256 pins cover the monitor index and all four consumed files for `bicep-docker-sbx-0.2.2`: observation, assessment, SBOM and Grype report. Acquisition authenticates the original trusted-main monitor and immutable tag/producer, then checks every selected byte against the reviewed manifest. Dispatch cannot supply another run, URL, source commit or fixture path.

Those findings include four advisories in `System.Security.Cryptography.Pkcs` and `System.Security.Cryptography.Xml` at `10.0.10`, fixed by the source policy's `10.0.14` pins. The planner runs against current source, requires no source edits and selects `bicep`, `bicep-avm` and `bicep-docker-sbx`. All six baseline/candidate builds, current scanner assessments, runtime/inventory checks and the source gate execute afresh. Both variants build current source: this qualifies rebuild handling, not a newly generated fix or reproduction of the historical image. The recorded historical assessment timestamps remain unchanged.

Historical inputs occupy the ordinary plan/evidence workspace with a `rehearsalFixture` receipt; production validation and freshness entry points reject that receipt. Independently, acquisition retains current authenticated Bicep release evidence under `current/`. Its receipt must cover every rebuilt profile; delivery scopes that authenticated snapshot to the three Bicep profiles and preserves the original acquisition receipt. Source, scoped release/assessment, latest-monitor and age guards still run at aggregation and every App write. Historical findings cannot grant delivery authority or relax production freshness. The qualification and draft receipt carry `evidenceMode: historical-replay`, the fixture identity and `productionRecovery: false`.

[Rehearsal 42716324080, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42716324080/attempts/1) established the missing-current-case boundary at source `f185bff7650a1f3463de17320e567c8cf433c641`: acquisition authenticated monitor 42708859965 and 15 published profiles, but the plan contained only 17 APK `no-compatible-fix` findings, zero updates and no affected profiles. `current-rebuild` refused; builds, source checks and App delivery were skipped. The [retained acquisition artifact](https://github.com/sokolaidev/maf-extensions/actions/runs/42716324080/artifacts/13718817101) expires on 2026-12-09. This refusal does not qualify historical replay or App recovery.

## Run qualification and optional delivery

Leave `exercise_delivery=false` to perform read-only qualification. To exercise the App lifecycle, explicitly select `exercise_delivery=true` on the manual dispatch. The workflow uses the existing remediation App client ID/private key and Docker Sandboxes credentials described in the [platform contract](container-image-releases.md#bicep-and-os-package-remediation). It mints the App token only after all scan jobs, the source gate, aggregation and retention of qualification evidence succeed.

With delivery selected, the App changes only the tracked [test receipt](../../tests/fixtures/image_dependencies/platform-rehearsal-receipt.json) on `bot/platform-remediation-rehearsal-<run-id>`. The receipt binds the qualified result hash and retained artifact URL. It does not deliver the dependency proposal or alter the production remediation branch. The resulting PR is a draft titled `test: retain platform remediation rehearsal evidence`, marked **do not merge**. Keep it available for inspection; close it when its evidence has been reviewed. Human edits, a human-closed PR, an unexpected head or conversion out of draft prevent automated refresh.

For a new run, the harness deliberately stops the first PR-creation request after the signed branch write. It verifies the hosted orphan's App identity, signature, parents, tree and file hashes, records the signed commit, then exercises two read-only refusal probes against unsigned and altered copies of that commit response. These probes do not modify hosted ownership records. Recovery uses the shared publisher with current-evidence checks before writes. It creates one draft PR and repeats delivery, requiring the same PR URL and unchanged head. A later execution against an existing draft records `refresh-passed`; it does not claim another interruption/recovery exercise.

Production source, scoped release/assessment and latest-monitor freshness checks remain active throughout. Unrelated publication can proceed; a change that invalidates the acquired evidence causes refusal. Each run has an isolated branch and concurrency group. No catalogue writer lock is held for the duration of the builds, and this workflow neither publishes images nor changes `PLATFORM_IMAGE_REMEDIATION_ENABLED`.

## Inspect and retry

Artifacts are retained for 30 days and include the workflow attempt in their names:

- `platform-rehearsal-plan-<attempt>`: request, authenticated acquisition receipt, evidence and deterministic plan, or acquisition failure. Historical runs also retain the independent current acquisition under `current/`.
- `platform-rehearsal-<variant>-<profile>-<attempt>`: baseline/candidate build, vendor, runtime, inventory and scan evidence.
- `platform-rehearsal-qualification-<run-id>-<attempt>`: acquired input and all image results, plus `rehearsal-qualified.json` on success. This artifact is uploaded before App token creation.
- `platform-rehearsal-delivery-<run-id>-<attempt>`: delivery progress and any failure record. Successful `delivery-passed` includes the interruption's signed commit, identity, ownership, injected-read refusals, delivered PR/head and unchanged repeat. `refresh-passed` covers only an existing draft's refresh.

Inspect the records, not just the workflow conclusion. `rehearsal-qualified.json` states `productionRecovery: false`; delivery evidence states `ci: not-verified`. After creation, inspect the draft's normal PR workflows at its exact head and retain their URLs/results separately, including any approval hold or failure. A successful draft receipt delivery alone does not satisfy the normal-PR-CI acceptance criterion or qualify delivery of a real package-update diff.

Use **Re-run all jobs** to retry the same run. Downloaded receipts and artifacts are attempt-bound, so rerunning only failed jobs cannot reuse the previous attempt's acquisition. Fresh acquisition must still match the dispatch source; if `main` advanced, start a new manual run. A retry on the same run reuses its isolated branch and verifies signed ownership before recovery. Preserve failed attempt evidence when comparing a recovery. Unclaimed unsigned branches, key changes and human edits require investigation; the workflow never force-updates them.

Record hosted results separately from the dated [Bicep rebuild evidence](platform-remediation-qualification-2026-11-09.md). Automatic enablement, image publication and issue closure remain separate maintainer actions.
