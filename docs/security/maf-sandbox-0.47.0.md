# Security evidence: maf-sandbox 0.47.0

Evidence collected on **2026-10-04 UTC**. This record covers the published core package and identifies which repository checks ran at its source commit. It is not a security certification or an endorsement of every backend, container image or application configuration.

**Assessment:** both PyPI artifacts passed publisher-attestation verification. A bounded dependency audit reported no known vulnerabilities in 11 resolved distributions. The repository's Python CodeQL analysis reported one finding. No built-image vulnerability report, malware-analysis report or independent security audit is included. This record therefore does not support a claim of zero security findings, no backdoors or general production safety.

## Release and artifact identity

| Field | Value |
|---|---|
| Package | [maf-sandbox 0.47.0 on PyPI](https://pypi.org/project/maf-sandbox/0.47.0/) |
| Release | [maf-sandbox-v0.47.0](https://github.com/sokolaidev/maf-extensions/releases/tag/maf-sandbox-v0.47.0), published 2026-10-03 |
| Source commit | [`80bfb7325cac14ad17caf56edd9836f29db332a4`](https://github.com/sokolaidev/maf-extensions/tree/80bfb7325cac14ad17caf56edd9836f29db332a4) |
| Publish run | [37162111482](https://github.com/sokolaidev/maf-extensions/actions/runs/37162111482), completed successfully at that commit |
| PyPI publisher metadata | GitHub repository `sokolaidev/maf-extensions`, workflow `publish-packages.yml`, environment `pypi` |
| Artifact upload date | 2026-10-03 UTC, from [version-specific PyPI metadata](https://pypi.org/pypi/maf-sandbox/0.47.0/json) |

| Artifact | SHA-256 |
|---|---|
| [maf_sandbox-0.47.0-py3-none-any.whl](https://files.pythonhosted.org/packages/de/2a/a27fe521784ad48124417d03d6c570eeb93b3049564562edd84e6ebdc83d/maf_sandbox-0.47.0-py3-none-any.whl) | `b7afc6ee3a3fa635c816d7f956b3e84585ba3d257110f28d55f18e437cfdd111` |
| [maf_sandbox-0.47.0.tar.gz](https://files.pythonhosted.org/packages/1e/13/26a96893d062706ce646e0e93b775f071d6fccfed4b52701725236da15fa/maf_sandbox-0.47.0.tar.gz) | `e614d110d7d38036be7d628dc9d4bb19dc3595da4bfaf3d9f9efe5c8ad3604f3` |

The core release consists of Python distributions. No container image digest is designated as an assessed artifact by this record. Backend packages, optional integrations, downloaded runtimes and images need their own version-specific evidence.

## Checks and findings

| Check | Result and scope | Evidence |
|---|---|---|
| Source analysis | GitHub CodeQL 2.27.1 completed at the release commit. Python: 1 result / 50 rules; JavaScript/TypeScript: 0 / 103; Actions: 0 / 23; Rust: 0 / 28. These are repository-wide analysis counts, not findings attributed specifically to the core wheel. No triage decision or accepted exception is asserted here. | [Hosted run](https://github.com/sokolaidev/maf-extensions/actions/runs/37162046561), [retained analysis metadata](evidence/maf-sandbox-0.47.0/codeql.json) |
| Dependency vulnerability audit | Local pip-audit 2.10.1 queried the PyPI vulnerability service on 2026-10-04: 11 distributions, 0 known vulnerabilities, 0 skipped distributions. Runtime dependencies were resolved for CPython 3.12 on Linux x86-64 / manylinux 2.28, without optional extras or workspace packages. | [Resolved inventory](evidence/maf-sandbox-0.47.0/requirements.txt), [raw audit result](evidence/maf-sandbox-0.47.0/pip-audit.json) |
| Published-artifact identity | Local pypi-attestations 0.0.30 cryptographically verified both artifacts against the expected repository; both commands returned `OK`. The attestation predicate is PyPI Publish v1. | Original [wheel provenance](evidence/maf-sandbox-0.47.0/maf_sandbox-0.47.0-py3-none-any.whl.provenance.json) and [source-archive provenance](evidence/maf-sandbox-0.47.0/maf_sandbox-0.47.0.tar.gz.provenance.json), [local verification record](evidence/maf-sandbox-0.47.0/local-verification.json) |
| Build and compatibility | Hosted build/verify, dependent suites, published-core compatibility gate and clean wheel install/use jobs completed successfully. | [Publish job outcomes](evidence/maf-sandbox-0.47.0/publish-jobs.json), [hosted publish run](https://github.com/sokolaidev/maf-extensions/actions/runs/37162111482) |
| Repository tests | The Tests workflow completed successfully at the release commit. This describes its configured jobs and skips, not exhaustive security coverage. | [Tests run](https://github.com/sokolaidev/maf-extensions/actions/runs/37162047161) |
| Selected live backend tests | Docker backend live tests and the Terraform job completed successfully against checkout code at the release commit. The OpenTofu platform job was skipped. These runs do not establish a clean vulnerability scan of their images. | [Docker run](https://github.com/sokolaidev/maf-extensions/actions/runs/37162047146), [Terraform run](https://github.com/sokolaidev/maf-extensions/actions/runs/37162047162) |
| Published-package live verification | The publish run's `Verify against a live sandbox` job was skipped. Checkout-based live tests above are separate evidence. | [Publish job outcomes](evidence/maf-sandbox-0.47.0/publish-jobs.json) |
| Built-image vulnerability scanning and SBOM | Not supplied for an immutable image digest in this record. The Python dependency inventory is not an image SBOM. | No image-security result claimed |
| Malware/backdoor analysis and independent audit | No such report is included. CodeQL and dependency CVE checks do not establish absence of malicious behavior. | No malware-free or independently audited claim |

The dependency resolution was performed when this record was prepared, not when the release was built. It covers one supported target and one version selection within the package's dependency ranges; it does not cover every installable combination, other platforms, development dependencies or extras. No vulnerability IDs were ignored. The hosted source-analysis configuration was not expanded or independently audited for this record. A successful analysis job reports execution status, not a zero-finding verdict.

Hosted run identities and outcomes are also retained in [workflow metadata](evidence/maf-sandbox-0.47.0/workflow-runs.json). These JSON summaries and the local verification record are unsigned observations. The original PyPI provenance bundles contain signed statements; preserving them does not turn the other observations into attestations.

## Verify the package

With [uv](https://docs.astral.sh/uv/) installed, the following commands download the named artifacts and verify their PyPI attestations against the expected repository. They do not install or execute the target package:

```sh
uvx --from pypi-attestations==0.0.30 pypi-attestations verify pypi --repository https://github.com/sokolaidev/maf-extensions pypi:maf_sandbox-0.47.0-py3-none-any.whl
uvx --from pypi-attestations==0.0.30 pypi-attestations verify pypi --repository https://github.com/sokolaidev/maf-extensions pypi:maf_sandbox-0.47.0.tar.gz
```

The published bytes must match the SHA-256 values above. The [PyPI verification procedure](https://docs.pypi.org/attestations/consuming-attestations/) authenticates artifact bytes and publisher identity. It does not prove a reproducible build, independently validate the source-to-artifact transformation or establish that the package is safe. A PyPI Publish attestation is not a full build-provenance statement.

## Repeat the dependency check

From the repository root, audit the retained, fully version-pinned inventory without installing those packages:

```sh
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps --progress-spinner off --vulnerability-service pypi --requirement docs/security/evidence/maf-sandbox-0.47.0/requirements.txt --format json
```

The vulnerability service changes over time, so a later run may find vulnerabilities absent from this snapshot. The inventory contains version pins rather than artifact hashes and is supplied for auditing, not as an application installation lock. Its input was [requirements.in](evidence/maf-sandbox-0.47.0/requirements.in); uv 0.12.5 produced it with `uv pip compile`, `--python-version 3.12`, `--python-platform x86_64-manylinux_2_28`, `--only-binary :all:`, `--no-header`, `--no-annotate` and the `https://pypi.org/simple` index. Re-resolving that input can choose newer dependencies and is a new measurement.

## Deployment boundaries and remaining work

The release's [isolation contract](https://github.com/sokolaidev/maf-extensions/blob/80bfb7325cac14ad17caf56edd9836f29db332a4/docs/sandbox/policy-isolation.md) requires the host to select an acceptable isolation floor and states that the router checks declarations rather than certifying a deployment. Its [network policy](https://github.com/sokolaidev/maf-extensions/blob/80bfb7325cac14ad17caf56edd9836f29db332a4/docs/sandbox/network.md), [file contract](https://github.com/sokolaidev/maf-extensions/blob/80bfb7325cac14ad17caf56edd9836f29db332a4/docs/sandbox/capabilities.md) and [host-tool authority rules](https://github.com/sokolaidev/maf-extensions/blob/80bfb7325cac14ad17caf56edd9836f29db332a4/docs/sandbox/hosts.md) define additional boundaries. Registered host functions execute with host authority; exposing one requires application authorization. The in-process test fake supplies no containment.

The linked live runs provide bounded evidence for their tested configurations. This record does not qualify ACAS, WSLC, Hyperlight, Docker Sandboxes, Kubernetes or arbitrary image/backend combinations for production use. It does not establish protection against a compromised host or a malicious dependency, nor does it replace an independent review of sandbox escape paths and host authority.

Before expanding the assurance claim, resolve and document the source finding's disposition, assess the exact deployed images and their SBOMs, cover each intended dependency/platform combination, and publish an independent review with its remediation evidence. These are outstanding evidence requirements, not completed checks or scheduled work.

For private reporting, supported versions and how fixes are released, see the [security policy](../../SECURITY.md). For other release records and how evidence is maintained, see [Security evidence](../security.md).
