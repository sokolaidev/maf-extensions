# Security evidence

Use the release records below to assess the evidence for the package version you plan to deploy. Each record identifies its source commit, published artifacts, checks and remaining gaps. A successful workflow means its configured checks passed; it does not establish the absence of malware, backdoors or undiscovered vulnerabilities.

| Release record | Scope | Evidence date |
|---|---|---|
| [maf-sandbox 0.47.0](security/maf-sandbox-0.47.0.md) | Core wheel and source archive; repository scans and selected live tests at its source commit | 2026-10-04 |

The packages remain experimental. A record covers only the versions, platforms and artifacts it names. It is a historical snapshot, not a continuously refreshed security rating, and does not extend support for an older release.

For ongoing built-image checks, see [container image security](security/container-images.md). Its badge covers twelve named Linux/amd64 profiles and links to retained inventories and vulnerability reports; those results do not alter the historical release records above.

## Reporting and updates

Report suspected vulnerabilities privately through the [security reporting policy](../SECURITY.md). It defines the acknowledgement target, coordinated disclosure process and supported versions. Only the newest release of each package is supported; fixes ship in new releases rather than being backported to older versions. Published disclosures belong in the repository's [security advisories](https://github.com/sokolaidev/maf-extensions/security/advisories).

## Applying the evidence to an application

Choose a backend and configuration that meet the application's [isolation policy](sandbox/policy-isolation.md), [network policy](sandbox/network.md), [file-access requirements](sandbox/capabilities.md) and [host authority requirements](sandbox/hosts.md). The router checks backend declarations; it does not independently certify the deployment. Registered host tools run with host authority and require their own authorization checks.

For container deployments, identify the exact image digest and its scan date. A passing source scan or Dockerfile check does not establish the vulnerability status of the built image. A new image, dependency resolution, backend configuration or package version needs its own evidence.

## Maintaining a release record

Preserve the original evidence date, commit, hashes and outcomes. Add dated corrections or follow-up results explicitly rather than replacing an older result with a scan of current main. Link hosted checks to specific runs and distinguish local measurements from CI results. Record scanner versions, target platforms, dependency inventories, skipped checks and any exclusions or accepted findings with their rationale and scope.

Before making an image-security claim, attach the immutable image reference, dependency inventory (SBOM), scan report, scanner/database date and verification instructions. Before claiming an independent audit, link the report and identify the audited commit, scope and remediation results. Missing evidence stays visible as a gap.
