# Container image security checks

The [Image security workflow](https://github.com/sokolaidev/maf-extensions/actions/workflows/image-security.yml) builds the twelve profiles below on Linux/amd64, inventories each built image with Syft, and scans that retained inventory with Grype. It runs on relevant pull requests and main-branch changes, daily at 06:35 UTC, and by manual dispatch. Daily builds refresh moving base tags and package-manager inputs; they are new candidates, not rescans of an earlier release's exact bytes.

Failures outside pull requests open or update the workflow's tracking issue through the repository's existing reporter. The scanner jobs have read-only repository permissions; a separate reporter has issue-write permission and never runs on pull requests.

The README badge shows the workflow status on `main`. Passing means every listed profile built, its inventory and scan completed, and Grype reported no High or Critical findings in that inventory. The inventory must contain components and identify the exact built image. Lower-severity findings remain in the reports. High/Critical findings fail even when no fix is available. Build failures, scanner failures and evidence-upload failures also fail the workflow. The policy accepts no vulnerability exclusions. A passing result does not establish the absence of malware, backdoors, uncatalogued software or unknown vulnerabilities.

## Covered profiles

| Profile | Built target |
|---|---|
| `bicep` | Base Bicep sandbox |
| `bicep-prepared` | Bicep with the repository's prepared AVM cache |
| `sbx-bicep` | The `sbx` template applied to the base Bicep image |
| `diagram` | Graphviz diagram sandbox |
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

Each matrix job retains an `image-security-<profile>` Actions artifact for 30 days, including when vulnerability findings fail the scan:

- `build.json`: source commit, profile, platform, observation time, run URL and immutable local image ID.
- `image-inspect.json`: Docker's metadata for that exact built image.
- `sbom.syft.json`: the component inventory, source-image metadata and Syft version.
- `grype.json`: findings, Grype version and vulnerability-database metadata.
- `grype.yaml`: the empty configuration used to prevent repository-discovered exclusion rules.

A scanner failure may leave an incomplete artifact. Missing inventory or scan output is missing evidence, not a clean result. Inspect the job's conclusion and steps as well as its files. Actions artifact downloads may require a GitHub login; job summaries identify each target without downloading the artifact. Retain the original reports elsewhere before their expiry if an assessment depends on them.

The local image ID identifies the image configuration and its content-addressed filesystem layers. It is not a registry manifest digest and cannot be substituted into a registry pull command. These checks do not publish images or retain the complete built image. Rebuilding the same source can produce different bytes; use these results only for the recorded local image ID.

## Planned distribution

The agreed registry is GitHub Container Registry, under `ghcr.io/sokolaidev/maf-extensions/`, with packages linked to this repository. Public publication is not enabled by the scanning workflow: image names, release versioning, supported profiles and the update policy still need a release contract. Published candidates will need their own registry manifest digests, retained reports and signed provenance; this workflow grants no package-publishing or signing permissions.
