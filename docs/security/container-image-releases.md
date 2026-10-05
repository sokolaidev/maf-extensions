# Container image release contract

This document defines the accepted contract for the next deliverable after the [candidate image checks](container-images.md). Implementation progress is recorded in the [Status table](#status).

## Outcome

A consumer can select a released image, pull its immutable registry digest, verify its publisher and source revision, and inspect the SBOM and vulnerability assessment for those same bytes. A release record distinguishes packaging tests from deployment qualification and identifies which Python package versions were tested with the image.

## Decisions

| Decision | Choice | State |
|---|---|---|
| Registry | Public packages under `ghcr.io/sokolaidev/maf-extensions/`, linked to this repository | Agreed before this draft |
| Vulnerability gate | Refuse every High/Critical finding, including unfixed findings; no exclusions | Agreed before this draft |
| Release version | Independent SemVer for each image profile, separate from Python and upstream tool versions | Accepted, decision 1 |
| Initial scope and names | The twelve currently scanned Linux/amd64 profiles, each in its own image repository | Accepted, decision 2 |
| Release control | Maintainer-triggered release; build/test/scan before one protected publication approval | Accepted, decision 3 |
| Signatures | GitHub artifact attestations for provenance and SPDX SBOM, bound to the registry digest | Accepted, decision 4 |
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

## Decision 4: signing and verification

Use GitHub artifact attestations for build provenance and the SPDX SBOM, with GitHub Actions OIDC identity and the published registry digest as their subject. Retain the original signature bundles and attach the attestations to the registry image. This follows the existing Hyperlight verification approach and avoids maintaining a separate long-lived signing key.

Consumer instructions verify both attestations with GitHub CLI under the expected repository, workflow, source revision and digest policy, and authenticate the completed catalogue record for the selected profile, version and digest before running the image. The provenance identifies who built which source; the SBOM identifies the inventoried components. The signed evidence index binds the scan reports and build records to the same release. The completion record establishes that the release passed publication verification; attestations alone do not. These checks establish release completion, artifact identity and origin, not a general safety certification.

## Build and publication flow

1. Select one or more profiles, unused release versions and a full source commit reachable from protected `main`. Validate the request against a checked-in profile catalogue. Use a clean checkout and record the workflow revision separately if it differs from the payload source revision.
2. Build each candidate once. Run the profile's packaging and runtime checks, record the local image ID, generate the Syft inventory, and enforce the existing Grype gate with a valid vulnerability database. Record the database identity and scan time. Add release labels before building; changing the configuration after scanning changes the image identity.
3. Retain the exact OCI manifests and blobs, inventories, reports and their hashes for promotion. Verify transferred bytes before loading or publishing them. Expired or missing candidates must be rebuilt and reassessed; a workflow run identifier alone is not an image artifact.
4. Present the candidate source, profile, version, identities and assessment to the maintainer at the publication step. Signing and registry-write permissions belong to this trusted release path; ordinary pull-request scans retain their current read-only permissions. Refresh an assessment older than 24 hours against the retained candidate before publication.
5. Serialize publication per profile across all versions, holding the lock through registry writes, verification and the catalogue transition. Acquire locks for multi-profile attempts in a consistent order. Under the lock, revalidate the version reservation, expected digest and advancement beyond the profile's current completed release, including on retries. A retry of an already completed version with matching identities and evidence is an idempotent no-op; abandon an incomplete attempt overtaken by a newer completed release. Before the registry write, durably register the profile, version, destination, expected manifest digest, source and publication attempt in the release catalogue as incomplete. The monitor reads this catalogue independently of the publishing job's success. Publish the retained candidate, resolve its registry manifest digest, and verify the manifest and configuration/layer relationship against the assessed image. Refuse to promote an existing version that resolves to different bytes. Tag immutability is an enforced release policy, not an assumed GHCR feature.
6. Generate provenance and an SPDX SBOM attestation for the registry digest. The provenance must identify the actual build workflow and payload revision, including the build-to-promotion relationship if these are separate jobs. A generic signature from a later promotion job must not misrepresent that job as the original build.
7. On a fresh runner without signing or registry-write permissions, verify the expected signer, source and digest, pull by that digest, and repeat the required runtime smoke checks. Confirm anonymous image pulls separately from authenticated publishing and attestation retrieval. This is the publisher's pre-completion candidate qualification; it cannot report a completed consumer release or bypass the consumer checks below.
8. Publish the completed release record and complete evidence only after every selected profile verifies. Sign the completion record with the approved release identity, binding its completed state, profile, version, source, registry and assessed-manifest digests, and evidence-index hash. While holding the profile locks, update each current release and its predecessor's superseded time together in the catalogue; a retry must not reset that time. Publication is not atomic: an interrupted attempt may leave a public registry object or tag. Keep its catalogue entry visibly incomplete until completion or abandonment; do not announce it as a successful release or replace its bytes on retry. If the maintainer ends retries or the retained promotion bytes expire or become irrecoverable, mark the attempt abandoned, record the reason, and permanently retire its version from publication. Any rebuilt replacement uses a new version. Incomplete and abandoned public candidates remain subject to the monitoring policy below.

For this initial Linux/amd64 scope, an OCI index must contain exactly one runnable image manifest: the assessed Linux/amd64 manifest. Reject additional runnable children, nested image indexes and unrecognized descriptor types before publication and during consumer verification; an attested index alone does not establish assessment of its children. Non-runnable attestation descriptors may accompany that manifest only when identified and validated as evidence for it. Record both the top-level index digest, when present, and the assessed manifest digest, preserving their relationship in the signed evidence. A local Docker configuration ID is not a registry manifest digest, and an attestation for one must not be accepted as an attestation for the other.

## Evidence and verification

The release record maps profile, image version, platform, source commit, build/publish workflow runs and tested consumer versions to the fully qualified registry digest and assessed platform-manifest digest. It includes upstream engine/provider versions and hashes for the retained evidence. Keep the raw Syft inventory and scan configuration alongside an SPDX representation suitable for attestation; both representations must describe the same assessed image.

Retain the original signed provenance and SBOM bundles, registry manifest, scan report with database metadata, build-input records, runtime results and an evidence checksum index. Bind the checksum index to the signed release evidence so a detached vulnerability report cannot be substituted for a different digest. For rebuilt OpenTofu, include the upstream revision, explicit module changes and resulting build records. For Hyperlight, preserve the existing build-input hash and wheel provenance records.

Publish durable evidence with the release and registry attestations. The current 30-day Actions artifacts are working storage, not the retention policy for a public release. Retain evidence for as long as the corresponding image remains publicly offered; the monitoring window below does not shorten that retention.

Consumer verification must require the expected repository, exact approved workflow identity, GitHub OIDC issuer, source commit/ref, predicate type and image digest. Refuse missing or mismatched attestations and unapproved/self-hosted signing runners. Authenticate the catalogue completion record under the approved release-signing policy and require completed state with the selected profile, version, source, registry and assessed-manifest digests, and evidence-index hash all matching the verified artifact and evidence. Refuse missing, unavailable, unauthenticated, mismatched, incomplete or abandoned records even when provenance and SBOM attestations pass. Select the expected values from the reviewed release policy, not from the unverified candidate. Run image code only after both provenance and completed-release verification succeed. Reuse the policy and refusal cases in the [existing Hyperlight verifier](../../images/hyperlight-sandbox/README.md#verify-a-published-runtime), extracting shared mechanics where useful without weakening its additional payload checks.

Keep upstream notices and component licence information in the image and SBOM. The repository's MIT licence does not describe every bundled third-party component. A passing vulnerability scan and verified publisher establish specific evidence; they do not certify absence of malware, backdoors or unknown vulnerabilities.

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

Publication requires a complete passing scan of the retained candidate performed within the preceding 24 hours, using a successfully refreshed, valid vulnerability database. Check the age immediately before the registry write. If approval takes longer, refresh the assessment of the same retained bytes; never rebuild silently or publish with an expired assessment. A failed refresh blocks publication.

For every monitored digest, a completed assessment remains fresh for at most 48 hours. Missing the daily schedule therefore has a bounded grace period; beyond it the public status is stale, not green. A scanner, registry or evidence-retrieval failure becomes visibly unavailable as soon as observed, even if the preceding clean scan is less than 48 hours old. An observed High/Critical finding is visibly vulnerable immediately. Preserve known findings and their timestamps when later scans fail or become stale.

Every status exposes its image digest, last assessment time, database identity, latest attempt outcome and monitoring end date where applicable. Consumer verification checks age at the time of use; a cached badge or stopped scheduler must not turn an old success into current evidence. These freshness limits describe assessment age, not a promise that new vulnerabilities cannot appear between scans.

The existing README badge remains explicitly about candidate builds. Published-image status identifies the monitored set and distinguishes clean, vulnerable, stale, unavailable and no-longer-monitored evidence from the separate incomplete, completed or abandoned publication state. An aggregate can be green only when every monitored digest belongs to a completed release, has a complete, fresh, passing assessment and has no newer failed assessment attempt. Incomplete and abandoned public candidates prevent a green aggregate even when their vulnerability scans pass.

## Delivery and acceptance

The first PR records accepted decisions and the profile/identity contract. The implementation PR adds the build/retain/publish/verify path, consumer verification and exact-digest monitoring. Actual publication remains the maintainer's release step, consistent with [RELEASING.md](../../RELEASING.md).

Acceptance requires a rehearsal that proves a retained candidate is promoted without rebuilding, signature verification succeeds under the intended policy, and changed digest/source/signer/evidence plus an unsigned candidate are refused. Reject an index with an extra unassessed runnable child, a nested index or evidence for the wrong manifest. Cover duplicate-version refusal, concurrent publication of different versions of one profile, partial failure and retry from retained bytes. Verify that an older candidate cannot overtake a newer completed release and that retries preserve the predecessor's original superseded time. Also interrupt publication after a successful registry write but before its result is recorded, then verify independent discovery and monitoring from the pre-write catalogue. Exercise abandonment after loss of retained bytes, permanent version retirement, continued monitoring after another release supersedes the candidate, and refusal to retire monitoring when registry visibility is uncertain. Demonstrate that the High/Critical gate still refuses unfixed findings and that a scheduled published-image check reads the recorded registry digest. Report which deployment/live checks were actually run for each profile.

Keep provenance and SBOM attestations valid while removing or altering the completion record: consumer verification must reject missing/unavailable records, invalid signatures, mismatched identities or evidence hashes, and incomplete or abandoned state before executing image code. Demonstrate that the publisher can qualify the candidate before completion, while the consumer refuses it until the authenticated completed record exists.

## References

- [GitHub Container Registry: linking packages, visibility and digest pulls](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
- [GitHub artifact attestations: container provenance and SBOM subjects](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
- [GitHub CLI verification policy flags](https://cli.github.com/manual/gh_attestation_verify).
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
