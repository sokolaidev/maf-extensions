# Container image release contract

This document defines the contract for published container images and their release evidence, complementing the [candidate image checks](container-images.md). Implementation progress is recorded in the [Status table](#status).

## Outcome

A consumer can select a released image, pull its immutable registry digest, verify its publisher and source revision, and inspect the SBOM and vulnerability assessment for those same bytes. A release record distinguishes packaging tests from deployment qualification and identifies which Python package versions were tested with the image.

## Decisions

| Decision | Choice | State |
|---|---|---|
| Registry | Public packages under `ghcr.io/sokolaidev/maf-extensions/`, linked to this repository | Accepted |
| Vulnerability gate | Refuse every High/Critical finding, including unfixed findings; no exclusions | Accepted |
| Release version | Independent SemVer for each image profile, separate from Python and upstream tool versions | Accepted, decision 1 |
| Initial scope and names | The twelve currently scanned Linux/amd64 profiles, each in its own image repository | Accepted, decision 2 |
| Release control | Maintainer-triggered release; build/test/scan before one protected publication approval | Accepted, decision 3 |
| Signatures | GitHub artifact attestations for provenance and SPDX SBOM, bound to the registry digest | Accepted, decision 4 |
| Consumer execution | Require verified release identity and completion; report current monitoring status separately | Accepted, decision 4 |
| Support window | Daily scans of the newest release per profile and each superseded release for 90 days; fixes ship as new versions | Accepted, decision 5 |
| Evidence freshness | Release scan at most 24 hours old; monitored status becomes stale after 48 hours without a successful assessment | Accepted, decision 6 |

## Decision 1: independent image versions

Each image profile has its own release version, independent of Python packages and upstream tools. A base-package security update can therefore produce a new image release without changing a Python distribution or pretending that the upstream tool changed version. The digest remains the identity consumers pin; a tag is a human-readable release label. The reference format is `ghcr.io/sokolaidev/maf-extensions/<profile>:0.1.0`.

Several profiles do not have one owning Python package. Upstream engine versions are also insufficient: an OpenTofu image can change its Go dependencies, OS packages or provider inventory while keeping the same engine version.

Release rules:

- Begin each profile at `0.1.0`. Security rebuilds and compatible corrections advance the patch; deliberate changes to the launcher contract, packaged tool behavior or supported provider set receive a reviewed minor release while below 1.0.
- Record upstream tool versions, installed package versions and tested consumer versions separately. Preserve existing engine-version labels; an image-release field must not silently change their meaning.
- Never overwrite or reuse a published version tag, including one left by an abandoned attempt. A different build, including a rebuild of the same source, needs a new version. An interrupted publication can resume only from retained bytes and their recorded digest while the attempt remains eligible for retry.
- Do not introduce `latest`, rolling minor aliases or automatic deployment updates in the first deliverable. Consumers select a version and deploy its digest.
- Keep image release metadata separate from the existing generated Python manifests and changelogs. The publisher validates explicitly selected image versions; this contract does not change release-please ownership.

## Decision 2: profile names and boundaries

The first publishing workflow covers all twelve currently scanned profiles on Linux/amd64. Every row uses the prefix `ghcr.io/sokolaidev/maf-extensions/`. The suffix deliberately matches the existing scan profile so that a release record does not need a second naming translation. Profiles can release independently; this scope does not require every publication to release all twelve together.

| Image suffix | Payload |
|---|---|
| `bicep` | Base Bicep sandbox |
| `bicep-prepared` | Bicep with the reviewed prepared AVM cache |
| `sbx-bicep` | The `sbx` template applied to the base Bicep image |
| `diagram` | Graphviz diagram sandbox |
| `drawio-sandbox` | Python/Graphviz Draw.io sandbox |
| `drawio-export` | Draw.io Desktop export runtime |
| `terraform-random` | Terraform with the approved random-provider manifest |
| `opentofu-random` | OpenTofu with the approved random-provider manifest |
| `terraform-prepared` | Terraform with the approved prepared dependency manifest |
| `opentofu-prepared` | OpenTofu with the approved prepared dependency manifest |
| `hyperlight` | The repository's Hyperlight runtime and verification application |
| `egress-proxy` | The shared Docker/WSLC egress proxy implementation |

The initial platform is `linux/amd64`. Custom provider manifests, the larger AVM/platform variants, other architectures and the upstream Hyperlight device plugin require their own release scope and evidence. Publishing the Hyperlight verification application does not qualify an embedding application's payload or prove live KVM/AKS behavior. Each profile records its intended consumers and actual compatibility checks before its first publication.

## Decision 3: release control

A maintainer starts a release for selected profiles, unused versions and a reviewed source commit on `main`. The workflow builds, tests and scans the candidates, then presents the exact candidate identities and reports for one publication approval in the protected `container-release` environment. Publication and signing consume those retained candidates after approval. A verification failure prevents a completed release record. Merges and daily candidate scans do not publish automatically.

The approval is for the irreversible publication step after the evidence exists, following the structure of the repository's Python releases. Selecting the candidate and approving publication are separate from approving this design. Configuring the environment's reviewer and branch restrictions is a prerequisite for enabling the publisher.

The approval request identifies the retained candidate digests, source, profiles, versions and release policy, and displays the reports available at approval. It explicitly authorizes the mandatory fresh assessment of those same bytes before publication, including a refresh after the protected-environment wait. Approval does not freeze the original scan report or authorize publication after a failed refresh. Retain both the displayed and refreshed reports with their timestamps and the approval record. Changed candidate bytes, source, profile/version selection or release policy require a new approval; a passing assessment refresh alone does not.

## Decision 4: signing and verification

Use GitHub artifact attestations for build provenance and the SPDX SBOM, with GitHub Actions OIDC identity and the published registry digest as their subject. Retain the original signature bundles and attach the attestations to the registry image. This follows the existing Hyperlight verification approach and avoids maintaining a separate long-lived signing key.

Consumer instructions verify both attestations with GitHub CLI under the expected repository, workflow, source revision and digest policy, and authenticate the completed catalogue record for the selected profile, version and digest before running the image. The provenance identifies who built which source; the SBOM identifies the inventoried components. The signed evidence index binds the scan reports and build records to the same release. The completion record establishes that the release passed publication verification; attestations alone do not. Report current monitoring status separately from this release-identity result. These checks establish release completion, artifact identity and origin, not a general safety certification.

## Build and publication flow

1. Select one or more profiles and unused release versions. Dispatch the release workflow on protected `main`; its `github.sha` is the selected full source commit. Validate the request against a checked-in profile catalogue. Require a clean checkout of that exact commit and equality between payload, workflow and OIDC source revisions before building or signing. Reject a separately selected payload revision; retry retained bytes only within a run or rerun carrying the same source identity.
2. Build each candidate once. Run the profile's packaging and runtime checks, record the local image ID, generate the Syft inventory, and enforce the existing Grype gate with a valid vulnerability database. Record the database identity and scan time. Add release labels before building; changing the configuration after scanning changes the image identity.
3. Retain the exact OCI manifests and blobs, inventories, reports and their hashes for promotion. Verify transferred bytes before loading or publishing them. Expired or missing candidates must be rebuilt and reassessed; a workflow run identifier alone is not an image artifact.
4. Present the candidate source, profile, version, identities, assessment and same-bytes refresh policy to the maintainer at the publication step. Signing and registry-write permissions belong to this trusted release path; ordinary pull-request scans retain their current read-only permissions. After approval, refresh an assessment older than 24 hours against the retained candidate before publication, retaining the new report and refusing publication unless it passes the approved policy.
5. Serialize publication per profile across all versions, holding the lock through registry writes, verification and the catalogue transition. Acquire locks for multi-profile attempts in a consistent order. Under the lock, revalidate the version reservation and expected digest, including on retries. New or incomplete releases must also advance beyond the profile's current completed release; delivery-only retries of a committed release do not move that pointer. A retry of an already completed version with matching identities and fully delivered evidence is an idempotent no-op; if completion evidence delivery is pending, resume only that delivery from the committed record without changing release pointers or timestamps. Abandon an incomplete attempt overtaken by a newer completed release. Before the registry write, durably register the profile, version, destination, expected manifest digest, source and publication attempt in the release catalogue as incomplete. The monitor reads this catalogue independently of the publishing job's success. Publish the retained candidate, resolve its registry manifest digest, and verify the manifest and configuration/layer relationship against the assessed image. Refuse to promote an existing version that resolves to different bytes. Tag immutability is an enforced release policy, not an assumed GHCR feature.
6. Generate provenance and an SPDX SBOM attestation for the registry digest within the same release workflow run that built the candidate. Separate build and promotion jobs retain the same workflow and source revision, and record their artifact transfer. Verify that the provenance's resolved source dependency and certificate source digest equal the checked-out payload commit; a generic signature from an unrelated promotion run is insufficient.
7. On a fresh runner without signing or registry-write permissions, separately verify the provenance and SPDX SBOM attestations under their expected predicate types and signer/source/digest policy before running image code, then pull by that digest and repeat the required runtime smoke checks. Confirm anonymous image pulls separately from authenticated publishing and attestation retrieval. This is the publisher's pre-completion candidate qualification; it cannot report a completed consumer release or bypass the consumer checks below.
8. After every selected profile verifies and its indexed evidence is durably retained, commit each profile's immutable completed record, current-release pointer and predecessor's superseded time together in one durable catalogue transaction under the profile locks. Record completion-evidence delivery as pending. Read back that committed record before generating or publishing its [release completion v1 attestation](#release-completion-v1); the attestation's fields must exactly match it. A completed record never returns to incomplete or abandoned state. Deliver and independently verify the completion bundle in the required stores, then mark delivery complete and announce the release. A retry must not reset completion or supersession timestamps. Publication is not atomic: an interrupted attempt before the catalogue commit stays incomplete and has no completion attestation. If the maintainer ends retries or retained promotion bytes become irrecoverable before that commit, mark the attempt abandoned, record the reason and permanently retire its version. Any rebuilt replacement uses a new version. After the commit, an interrupted or uncertain attestation upload leaves a completed record with pending evidence delivery; reconcile existing bundles against that record and resume delivery idempotently, never rebuild or abandon the completed release. Monitoring continues throughout, and missing completion evidence is visibly unavailable.

For this initial Linux/amd64 scope, an OCI index must contain exactly one runnable image manifest: the assessed Linux/amd64 manifest. Reject additional runnable children, nested image indexes and unrecognized descriptor types before publication and during consumer verification; an attested index alone does not establish assessment of its children. Non-runnable attestation descriptors may accompany that manifest only when identified and validated as evidence for it. Record both the top-level index digest, when present, and the assessed manifest digest, preserving their relationship in the signed evidence. A local Docker configuration ID is not a registry manifest digest, and an attestation for one must not be accepted as an attestation for the other.

## Evidence and verification

The release record maps profile, image version, platform, source commit, build/publish workflow runs and tested consumer versions to the fully qualified registry digest and assessed platform-manifest digest. It includes upstream engine/provider versions and hashes for the retained evidence. Keep the raw Syft inventory and scan configuration alongside an SPDX representation suitable for attestation; both representations must describe the same assessed image.

Retain the original signed provenance and SBOM bundles, registry manifest, scan report with database metadata, build-input records, runtime results and an evidence checksum index. The completion attestation binds the index hash; the index covers the preceding evidence and excludes the completion bundle itself to avoid a circular hash dependency. For rebuilt OpenTofu, include the upstream revision, explicit module changes and resulting build records. For Hyperlight, preserve the existing build-input hash and wheel provenance records.

Publish durable evidence with the release and registry attestations. The current 30-day Actions artifacts are working storage, not the retention policy for a public release. Retain evidence for as long as the corresponding image remains publicly offered; the monitoring window below does not shorten that retention.

Consumer verification must require the expected repository, exact approved workflow identity, GitHub OIDC issuer, source commit/ref, predicate type and image digest. Verify provenance and SPDX SBOM attestations separately under their respective predicate types; a successful provenance verification does not verify the SBOM. Refuse missing or mismatched attestations and unapproved/self-hosted signing runners. Authenticate the catalogue completion record under the approved release-signing policy and require completed state with the selected profile, version, source, registry and assessed-manifest digests, and evidence-index hash all matching the verified artifact and evidence. Refuse missing, unavailable, unauthenticated, mismatched, incomplete or abandoned records even when provenance and SBOM attestations pass. Select the expected values from the reviewed release policy, not from the unverified candidate. Run image code only after all three checks succeed: provenance attestation, SPDX SBOM attestation and completed-release verification. Reuse the provenance policy and refusal cases in the [existing Hyperlight verifier](../../images/hyperlight-sandbox/README.md#verify-a-published-runtime), adding the separate SBOM and completion checks without weakening its additional payload checks.

The default consumer gate verifies release identity and completion. A stale, unavailable, vulnerable or no-longer-monitored assessment does not invalidate that historical evidence or block execution when the three checks pass. Report the monitoring result alongside the identity result, including when retrieval fails; never present verified identity as a current passing vulnerability assessment. Applications may impose a stricter deployment policy that also requires fresh passing monitoring evidence.

Keep upstream notices and component licence information in the image and SBOM. The repository's MIT licence does not describe every bundled third-party component. A passing vulnerability scan and verified publisher establish specific evidence; they do not certify absence of malware, backdoors or unknown vulnerabilities.

### Release completion v1

The completion record is an in-toto Statement v1 in a GitHub artifact-attestation Sigstore bundle, generated with the custom-predicate mode of `actions/attest`. Its predicate type is `https://github.com/sokolaidev/maf-extensions/blob/main/docs/security/container-image-releases.md#release-completion-v1`; this URI identifies the schema and is not a policy document fetched from the candidate. The single subject names `ghcr.io/sokolaidev/maf-extensions/<profile>` without a tag and carries the published top-level SHA-256 digest. The JSON predicate requires `schemaVersion` equal to `1`, `state` equal to `completed`, and string fields `profile`, `version`, `sourceCommit`, `sourceRef`, `registryDigest`, `assessedManifestDigest` and `evidenceIndexSha256`. Digests use `sha256:<64 lowercase hex characters>`; `sourceCommit` is the full Git commit, and `sourceRef` is `refs/heads/main`. Require the profile and version selected by the consumer, equality with the verified subject and source identity, and a matching SHA-256 of the retained evidence-index bytes. Reject absent fields, wrong types, unsupported schema versions and mismatches.

All three attestation types use the direct release workflow identity `https://github.com/sokolaidev/maf-extensions/.github/workflows/container-image-release.yml@refs/heads/main`, issuer `https://token.actions.githubusercontent.com`, repository `sokolaidev/maf-extensions` and GitHub-hosted runners. The reviewed policy pins both signer and source digests to the selected release commit; neither value comes solely from the candidate's predicate. Build, promotion and completion jobs belong to that workflow at that commit. Creating a differently named or reusable signer requires a reviewed policy change.

Only the durably committed completion record authorizes signing. Publish its completion bundle to GitHub's attestation store and as an OCI attestation for the subject digest, and retain its original bytes with the durable release evidence. Consumers use `gh attestation verify` on the digest-pinned `oci://` image reference with `--repo`, the exact `--cert-identity`, `--cert-oidc-issuer`, `--signer-digest`, `--source-digest`, `--source-ref`, `--deny-self-hosted-runners` and the completion `--predicate-type` above. The default retrieval is GitHub's attestation API; `--bundle-from-oci` or an archived `--bundle` may supply the same signed evidence under the same verification policy. A successful CLI result authenticates the statement, not the custom fields: validate the verified JSON statement's subject and predicate against this schema and the selected release before accepting completion. An unsigned catalogue entry cannot substitute for this attestation. The signed record is immutable completion evidence, so consumers need not query a mutable catalogue to reauthorize it; a pending or failed delivery cannot retroactively undo the committed state. Repeat verification separately with the provenance and SPDX predicate types; also require the provenance's resolved dependency to match the selected source commit.

## Maintenance and public status

### Decision 5: support window

Scan the newest completed release of each profile daily for as long as it remains current. When a newer release supersedes it, continue scanning its exact digest for 90 days from the replacement's publication. Every superseded release receives that window, even if several newer versions ship during it; this is not a two-version cap. The release catalogue records the superseded time and monitoring end date for each digest.

Incomplete and abandoned publication attempts have a separate lifecycle: monitor their digests daily for as long as the images remain public, regardless of later completed releases. They do not enter the completed-release 90-day window. Reconcile pre-write catalogue entries against the registry even when a publisher crashed before recording its push result. Record and monitor any unexpected digest observed at the reserved destination as a failed publication, not as a verified replacement for the intended image. An uncertain push result or registry outage cannot remove an entry from monitoring. Show its publication state alongside the vulnerability assessment; a clean scan cannot turn an incomplete or abandoned candidate into a supported release.

Abandonment does not delete an image automatically. A maintainer may explicitly remove a partial publication where [GitHub's deletion rules](https://docs.github.com/en/packages/learn-github-packages/deleting-and-restoring-a-package) permit it; a public GHCR package [cannot be made private again](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility). Retire its monitoring only after confirming that its digest is no longer publicly retrievable, including through other tags. Keep the catalogue entry, permanently retired version, reason and retained evidence. If removal is unavailable or unconfirmed, continue monitoring; report unavailable evidence whenever public exposure cannot be determined.

Security corrections ship as new image versions. The initial policy does not promise backports to older release lines or a remediation deadline when an upstream fix is unavailable. A monitored vulnerable release stays visibly failing until a replacement or other resolution is recorded; being in the monitoring window is not a clean security assessment.

After the 90-day window, mark the release as no longer monitored instead of continuing to display its last clean result as current. Do not automatically delete its image, reports or original signed evidence. This keeps pinned deployments and historical verification possible while giving consumers an explicit migration window.

Daily candidate rebuilds continue to assess current build inputs. A separate scheduled job assesses the exact monitored digests, including incomplete and abandoned public candidates, against a refreshed vulnerability database and records an observation timestamp. Rebuilding a tag's source is not a rescan of that released image.

A new High/Critical finding makes that digest's current assessment fail even if its original release scan passed. Report the affected digest, supported consumers, available replacement and whether an upstream fix exists. Preserve the historical report and immutable image identity; remediation produces a new release. Do not silently move existing version tags or remove the strict gate to publish an unfixed finding.

### Decision 6: evidence freshness

Publication requires a complete passing scan of the retained candidate performed within the preceding 24 hours, using a successfully refreshed, valid vulnerability database. Check the age immediately before the registry write. If approval or lock acquisition takes longer, refresh the assessment of the same retained bytes under the explicitly approved refresh policy; never rebuild silently or publish with an expired assessment. Retain the refreshed report as the publication assessment, preserving the original report the maintainer saw. A failed refresh blocks publication; the earlier approval cannot override it.

For every monitored digest, a completed assessment remains fresh for at most 48 hours. Missing the daily schedule therefore has a bounded grace period; beyond it the public status is stale, not green. A scanner, registry or evidence-retrieval failure becomes visibly unavailable as soon as observed, even if the preceding clean scan is less than 48 hours old. An observed High/Critical finding is visibly vulnerable immediately. Preserve known findings and their timestamps when later scans fail or become stale.

Every status exposes its image digest, last assessment time, database identity, latest attempt outcome and monitoring end date where applicable. Consumer status reporting reads the authoritative monitor's latest-attempt record over authenticated HTTPS from the endpoint selected by the reviewed release policy, checks the digest and evaluates age at the time of use. A cached badge or previously signed success alone cannot establish the latest attempt. If the authoritative record cannot be retrieved or its identity and latest-attempt relationship cannot be established, report status as unavailable; do not fall back to a cached green result. A stopped scheduler eventually makes its last success stale. This reporting is separate from the release-identity execution gate. These freshness limits describe assessment age, not a promise that new vulnerabilities cannot appear between scans.

The existing README badge remains explicitly about candidate builds. Published-image status identifies the monitored set and distinguishes clean, vulnerable, stale, unavailable and no-longer-monitored evidence from the separate incomplete, completed or abandoned publication state. An aggregate can be green only when every monitored digest belongs to a completed release, has a complete, fresh, passing assessment and has no newer failed assessment attempt. Incomplete and abandoned public candidates prevent a green aggregate even when their vulnerability scans pass.

## Delivery and acceptance

Release readiness requires the build, retention, publication, consumer-verification and exact-digest monitoring paths to satisfy the acceptance cases below. Actual publication remains the maintainer's release step, consistent with [RELEASING.md](../../RELEASING.md).

Acceptance requires a rehearsal that proves a retained candidate is promoted without rebuilding, signature verification succeeds under the intended policy, and changed digest/source/signer/evidence plus an unsigned candidate are refused. Reject an index with an extra unassessed runnable child, a nested index or evidence for the wrong manifest. Cover duplicate-version refusal, concurrent publication of different versions of one profile, partial failure and retry from retained bytes. Verify that an older candidate cannot overtake a newer completed release and that retries preserve the predecessor's original superseded time. Also interrupt publication after a successful registry write but before its result is recorded, then verify independent discovery and monitoring from the pre-write catalogue. Exercise abandonment after loss of retained bytes, permanent version retirement, continued monitoring after another release supersedes the candidate, and refusal to retire monitoring when registry visibility is uncertain. Demonstrate that the High/Critical gate still refuses unfixed findings and that a scheduled published-image check reads the recorded registry digest. Report which deployment/live checks were actually run for each profile.

Keep provenance and SBOM attestations valid while removing or altering the completion record: consumer verification must reject missing/unavailable records, invalid signatures, mismatched identities or evidence hashes, and incomplete or abandoned state before executing image code. Demonstrate that the publisher can qualify the candidate before completion, while the consumer refuses it until the authenticated completed record exists.

Interrupt completion before the catalogue transaction, after its commit but before attestation publication, and after an upload succeeds but before delivery is acknowledged. The first case leaves an incomplete release with no completion signature; the second leaves a completed record with unavailable consumer evidence; the third may expose only a signature matching that already committed record. Reconcile and resume delivery without rebuilding, changing identities, abandoning a completed record, resetting supersession time or moving a newer current-release pointer backwards. A completed record without a verifiable completion bundle must still fail consumer verification.

Reject a build whose payload checkout differs from the workflow/OIDC source commit, and a provenance statement whose resolved source differs from the selected commit. Exercise completion retrieval through GitHub, OCI and an archived bundle under the same policy; reject wrong predicate/schema, missing or wrongly typed fields, wrong workflow/ref/source/signer digest, a mismatched subject name, and an unsigned catalogue record with otherwise valid provenance and SBOMs.

With valid provenance and completion evidence, remove the SBOM attestation or substitute an invalid signature, wrong predicate type or different subject digest: consumer verification must refuse execution. Exercise an approval or lock wait beyond 24 hours: only a fresh passing scan of the approved retained bytes permits publication, failed refreshes block it, and changed bytes or release policy require new approval. Preserve both the approval-time report and the final publication assessment.

With all three release-identity checks passing, exercise stale, unavailable, vulnerable and no-longer-monitored assessments: the default gate still permits execution and reports each monitoring state separately. An older passing report must not hide a newer failed attempt. Wrong-digest status, an untrusted endpoint or inability to establish the latest record must report unavailable, even with a cached passing assessment. Verify the 48-hour age boundary without relying on a new scheduler run. These cases qualify status reporting; they do not add a current-scan requirement to the default execution gate.

## References

- [GitHub Container Registry: linking packages, visibility and digest pulls](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
- [GitHub artifact attestations: container provenance and SBOM subjects](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
- [GitHub CLI verification policy flags](https://cli.github.com/manual/gh_attestation_verify).
- [GitHub custom attestation predicates](https://github.com/actions/attest#custom-attestation).
- [Existing isolated Hyperlight signing rehearsal](../../.github/workflows/hyperlight-provenance.yml).

## Status

The design is accepted; implementation is pending. No images have been published or signed under this contract, and no release verification results are claimed. The existing candidate checks do not implement the release publisher or published-digest monitor.

| Decision | State | Tracking |
|---|---|---|
| Public GHCR destination and repository linkage | Pending registry setup for releases | untracked |
| Strict High/Critical gate for publication | Pending publisher integration; candidate checks already enforce this threshold | untracked |
| 1: Independent per-profile versions and immutable release identities | Pending implementation | untracked |
| 2: Twelve Linux/amd64 profiles and assessed-manifest scope | Pending release catalogue and index validation | untracked |
| 3: Maintainer-controlled publication, per-profile serialization and interrupted-attempt lifecycle | Pending build/retain/publish/verify workflow and protected environment | untracked |
| 4: Digest-bound provenance, SBOM, signed completion records, durable evidence and consumer verification | Pending implementation and signing rehearsal | untracked |
| 5: Daily exact-digest monitoring, superseded-release window and public-candidate monitoring | Pending catalogue and scheduled monitor | untracked |
| 6: Publication scan age, public assessment freshness and failure visibility | Pending publisher and public-status enforcement | untracked |
| Delivery acceptance and per-profile live qualification | Pending rehearsal and recorded results | untracked |
