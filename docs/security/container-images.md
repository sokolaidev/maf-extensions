# Container image security checks

The [Image security workflow](https://github.com/sokolaidev/maf-extensions/actions/workflows/image-security.yml) builds the eligible profiles from the twelve below on Linux/amd64, inventories each built image with Syft, and scans that retained inventory with Grype. It runs daily at 06:35 UTC and by manual dispatch, with a choice of all profiles or one profile. Pull requests and main-branch pushes retain code, workflow, lint, type and CodeQL checks without starting this image-build matrix. Daily builds refresh moving base tags and package-manager inputs; they are new candidates, not rescans of an earlier release's exact bytes.

Failures outside pull requests open or update the workflow's tracking issue through the repository's existing reporter. The scanner jobs have read-only repository permissions; a separate reporter has issue-write permission and never runs on pull requests.

The README badge shows the latest scheduled workflow status on `main`. Passing means every eligible profile in that scheduled run built, its inventory and scan completed, and Grype reported no High or Critical findings in that inventory. Manual scans do not update this badge; it does not establish scan freshness or the status of an already published image.

Image publication is the mandatory image gate: its workflow builds the selected profile, retains its exact OCI bytes, checks runtime behavior, produces SBOMs and refuses High/Critical findings including unfixed ones before maintainer approval. Publication rechecks freshness, and independent signature, anonymous-pull and runtime verification precedes completed release evidence. Removing image builds from PR CI permits an image-build regression to merge; the publication gate prevents publishing it, while daily or manually requested scans detect it earlier. Python package publishing is a separate release path and does not publish these images.

## Release-needed issues

The [Image release tracking workflow](https://github.com/sokolaidev/maf-extensions/actions/workflows/image-release-needed.yml) runs after main-branch image scans, published-image monitoring and image release attempts, or by manual dispatch. It maintains one `Image release needed: <profile>` issue per affected profile. It starts with profiles that have a completed, delivered release; choosing a profile's first release remains a maintainer action. No committed release history means there is no replacement baseline and no automatic release request.

The reporter verifies immutable release evidence, all three release attestations and the indexed assessment before comparing against the latest completed release. It requests a replacement when relevant repository build inputs changed, a subsequent valid candidate scan inventories changed installed components, or monitoring reports High/Critical findings on the current published digest. Input comparison covers image contexts, known image builders, prepared Terraform dependencies, Hyperlight wheel packages and its copied probe, and the proxy context. Publishing/reporting scripts and tests do not by themselves require replacement images. The six consumer README paths explicitly listed in `CONSUMER_GUIDES` in `scripts/select_image_security.py` are also ignored because the current image recipes and build helpers do not consume them. This is an exact-path list, not a Markdown-wide exclusion: unknown guides, nested files and package READMEs retain their normal build-input classification. Recheck the list when a recipe or build helper starts consuming a guide; Dockerfile, builder and dependency changes still require replacement images, and scheduled scans still cover every eligible profile. Parsed package-version-only changes and their matching editable-workspace lock versions are ignored. Missing Git history fails reconciliation instead of guessing that a release is needed.

Each issue carries the current version/digest, changed inputs or component versions, CVEs and available fixes, and evidence links. Component comparison ignores SBOM IDs, locations and timestamps; a different image hash alone is not a reason to release. A candidate with High/Critical findings is blocked on remediation. A passing candidate still needs the full release-preparation and approval gate. Failed or missing candidate assessments do not create replacement requests on their own; workflow failures retain their separate operational trackers, and previously known release reasons remain open.

Reconciliation holds the same catalogue lock as release and monitor history writers throughout planning and issue updates, and manages only marked issues authored by GitHub Actions. It updates the body when actionable state changes or a newer candidate observation advances the saved ordering timestamp; candidate profile jobs must complete strictly later than release completion and the saved observation to replace that state. Build records identify their run and attempt, and both build collection and artifact creation must fall within the producing job. Evidence links and ordering use that job attempt, so rerunning another profile cannot refresh retained evidence. Older artifacts without attempt identity remain unavailable until a fresh scan. Timestamp ties keep the previously accepted state. Before accepting a scan event, the reporter reads complete retained Actions run history and rejects profiles with newer or tied profile-job completions from trusted completed runs, across all rerun attempts, even when no release-needed issue exists. A newer failed profile attempt also prevents an older scan from becoming current, even when its build or identity step produced no artifact; incomplete run or job history stops reconciliation. It preserves prior reasons across unavailable scans, produces no daily reminder comments, and reuses the same issue for later release needs. The body is machine-maintained; add maintainer notes as comments. An issue closes only after a different, completed and delivered digest has authenticated evidence, no remaining source-input differences, addressed component-update requirements and no outstanding known High/Critical findings for that replacement. A candidate scan or manual closure cannot silently dismiss an unresolved request. Evidence retrieval, signature, history-race or issue-state errors leave requests unchanged and report tracking as unavailable. This reporter has issue-write permission but no publication, package-write or signing permission.

During an adjacent core release transition, the Hyperlight profile is deferred when the checkout core cannot satisfy its dependent ranges; the job summary records that no image or security evidence was produced. The other selected profiles still run. A green badge during this transition does not establish Hyperlight image coverage. Invalid or non-adjacent dependency ranges still fail the preflight.

The inventory must contain components and identify the exact built image. Lower-severity findings remain in the reports. High/Critical findings fail even when no fix is available. Build failures, scanner failures and evidence-upload failures also fail the workflow. The policy accepts no vulnerability exclusions. A passing result does not establish the absence of malware, backdoors, uncatalogued software or unknown vulnerabilities.

## Covered profiles

| Profile | Built target |
|---|---|
| `bicep` | Base Bicep sandbox |
| `bicep-prepared` | Bicep with the repository's prepared AVM cache |
| `sbx-bicep` | The `sbx` template applied to the base Bicep image |
| `graphviz` | Graphviz DOT rendering sandbox |
| `drawio-sandbox` | Python/Graphviz Draw.io sandbox |
| `drawio-export` | Draw.io Desktop export runtime |
| `terraform-random` | Terraform with its random-provider profile |
| `opentofu-random` | OpenTofu with its random-provider profile |
| `terraform-prepared` | Terraform prepared from `dependencies.terraform.json` |
| `opentofu-prepared` | OpenTofu prepared from `dependencies.opentofu.json` |
| `hyperlight` | Hyperlight runtime built from the checkout's wheels and lockfile |
| `egress-proxy` | Docker backend's patched egress proxy |

The scan covers the built runtime filesystem. Build-stage-only packages, other architectures, custom image arguments, externally supplied images, upstream device-plugin images and different prepared dependency manifests are outside this inventory. In particular, the larger Terraform AVM and OpenTofu platform/provider/multiversion variants require separate scans. Applying the `sbx` template to another base also creates an unassessed image. Image scanning does not qualify sandbox confinement or backend lifecycle behavior.

The Diagram, Draw.io, Terraform/OpenTofu and Hyperlight profiles use digest-pinned Wolfi bases with signed APK packages. Wolfi is a rolling distribution, so package updates and native-runtime compatibility are assessed by each build and scan. OpenTofu is rebuilt from a pinned upstream commit with explicit Go dependency updates; its retained source-build records distinguish it from the upstream release binary. These images still require deployment-specific qualification, including live AKS/ACAS checks where applicable.

## Reading the evidence

Each matrix job that reaches image identity recording retains an `image-security-<profile>` Actions artifact for 30 days, including when vulnerability findings fail the scan:

- `build.json`: source commit, profile, platform, observation time, run URL and immutable local image ID.
- `image-inspect.json`: Docker's metadata for that exact built image.
- `sbom.syft.json`: the component inventory, source-image metadata and Syft version.
- `grype.json`: findings, Grype version and vulnerability-database metadata.
- `grype.yaml`: the empty configuration used to prevent repository-discovered exclusion rules.

A scanner failure may leave an incomplete artifact. Missing inventory or scan output is missing evidence, not a clean result. Inspect the job's conclusion and steps as well as its files. Actions artifact downloads may require a GitHub login; job summaries identify each target without downloading the artifact. Retain the original reports elsewhere before their expiry if an assessment depends on them.

The local image ID identifies the image configuration and its content-addressed filesystem layers. It is not a registry manifest digest and cannot be substituted into a registry pull command. These checks do not publish images or retain the complete built image. Rebuilding the same source can produce different bytes; use these results only for the recorded local image ID.

## Planned distribution

The agreed registry is GitHub Container Registry, under `ghcr.io/sokolaidev/maf-extensions/`, with packages linked to this repository. The [accepted image release contract](container-image-releases.md) defines names and independent versions for all twelve Linux/amd64 profiles, maintainer-approved publication of the exact assessed bytes, signed provenance and SBOMs, and daily monitoring of released digests. It requires a release scan no more than 24 hours old, stale status after 48 hours, and 90 days of continued monitoring after a release is superseded.

Interrupted publications are registered before the registry write. Incomplete or abandoned candidates remain visibly blocked and monitored for as long as they remain public, even if no release completes. Consumers require an authenticated completed catalogue record for the selected profile, version and digest, even when image attestations pass. Abandonment permanently retires the version; it does not delete the image automatically or start the completed-release 90-day window.

Implementation of that contract is pending. Public publication is not enabled by the scanning workflow: it grants no package-publishing or signing permissions. Published candidates will need their own registry manifest digests, retained reports and signed provenance.
