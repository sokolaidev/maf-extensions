# Egress proxy image

`egress-proxy` is backend infrastructure for the Docker and WSLC sandbox backends. It supplies the repository's patched iron-proxy, its entrypoint and CA certificates on Linux/amd64. It is configured by a backend for one sandbox generation; it is not a general-purpose forward proxy or a workload image.

## Publication and verification

The initial public release is tracked in [#1846](https://github.com/sokolaidev/maf-extensions/issues/1846). The target repository is `ghcr.io/sokolaidev/maf-extensions/egress-proxy`. Use only a completed `image-egress-proxy-v<version>` release with retained evidence. A merged change or passing local check does not establish publication.

Follow the [consumer verification procedure](../../docs/security/container-image-releases.md#evidence-and-verification): independently select the expected source and digests, download the release evidence, and verify provenance, SPDX and completion bundles before execution. Require `releaseIdentityVerified: true`, then pin `ghcr.io/sokolaidev/maf-extensions/egress-proxy@sha256:<verified-manifest-digest>`. The manifest digest and Docker image/configuration identity are recorded separately; do not substitute one for the other.

The [public security report](https://sokolaidev.github.io/maf-extensions/) reports monitoring separately from signed release identity. Publication refuses all known High/Critical findings, including unfixed findings; build, inventory and scanner errors also fail. Assessments expire after 48 hours, and superseded completed releases remain monitored for 90 days. A clean scan is dated evidence, not a malware-free or production-safety guarantee.

## Use with a backend

Configure the proxy on the backend, and select a separate workload image for each sandbox. Docker requires Engine 28 or later for its internal, unaddressed bridge. The backend creates a proxy for each acquired sandbox, supplies the policy, binds its tunnel only to the internal network, connects its outbound leg, and installs the generated public CA in the guest. Keep the default hardening enabled.

```python
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

backend = await DockerSandboxBackend.create(
    DockerSandboxConfig(
        egress_proxy_image="ghcr.io/sokolaidev/maf-extensions/egress-proxy@sha256:<verified-manifest-digest>",
    )
)
```

Select `Egress.ALLOWLIST` and explicit host rules on the workload's `SandboxSpec`; string hosts permit every supported HTTP method, while `EgressRule` can restrict methods and paths. Unlisted destinations are denied. Use [the Docker backend guide](../../docs/sandbox/backends/docker.md) for lifecycle and policy configuration. `WslcSandboxConfig.egress_proxy_image` accepts the same image contract, but the initial image release's recorded runtime qualification covers Docker only; byte-identical packaged sources do not prove WSLC runtime compatibility.

Attached credentials require an application-owned `CredentialGateway` and authority-bearing `EgressRule` entries. The provider must authorize the user, agent, call, audience and runtime generation before issuing a grant. The proxy replaces guest authorization with the matching short-lived bearer grant only for its exact HTTPS origin, strips authorization on an unbound allowed host, and refuses expired grants. The upstream receives the credential and must be trusted not to disclose it. The image does not supply a credential provider or make a tenant-authorization decision. See [attached identity](../../docs/sandbox/hosts.md#credentials-for-guest-http-requests) for the contract.

The backend supplies `MAF_SANDBOX_CONFIG_B64` and `MAF_SANDBOX_TUNNEL_SUBNETS`; running the image alone without those values deliberately fails. It generates ephemeral CA and boot material under `/run/maf-proxy`. Do not publish port 3128 to other workloads, mount the Docker socket or credential directories into guests, reuse a proxy across unrelated sandboxes, or expose its CA private key. Destruction of the sandbox must remove its proxy and network too.

## Included components and policy changes

The source context is [the Docker backend's proxy](../../packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy/Dockerfile), mirrored byte-for-byte in WSLC. The release wrapper adds image-specific documentation, the suite license and the policy sources without replacing the compiled executable. The build checks the upstream Git commit, applies `iron.patch`, adds `credential_gate.go`, verifies Go modules, runs the credential gate's Go tests, and builds a static executable. Exact resolved Go modules and Alpine components belong to the release SBOM; package repositories can change between builds, so rebuilt bytes need a new candidate and image version.

The policy patch changes wildcard hosts to exclude their apex, makes an explicit `*` method a literal rather than an unrestricted method list, keeps CONNECT setup separate from inner request policy, rejects WebSocket upgrades, restricts upstream schemes and control-plane destinations, and adds the sandbox-bound credential gate. Default plaintext HTTP is disabled; the backend's explicit private-HTTP option has a narrower private-address policy. The release check below exercises only the stated HTTPS policy; it does not qualify every patch branch or optional private-HTTP configuration.

`/licenses/iron-proxy-LICENSE` retains the upstream notice, and `/licenses/Go-LICENSE` retains the Go runtime license. `/usr/share/maf-egress-proxy/build-info.txt` records the actual compiler and module versions embedded in the executable. `/licenses/maf-extensions-LICENSE` covers the repository additions; `/usr/share/maf-egress-proxy/iron.patch` and `credential_gate.go` retain the modifications. Components keep their own licenses; consult the SPDX inventory for transitive license metadata. The upstream proxy is pinned by the source Dockerfile and is not automatically replaced with the latest upstream release.

## Release qualification

Candidate preparation and post-publication qualification invoke `scripts/egress_proxy_qualification.py` through the common release checker. The latter stage first verifies candidate attestations and pulls the selected public digest anonymously. Both stages use the checkout SDK and record its package versions, the engine version, exact proxy and fixture image identities, executable/notice/source hashes, configuration boundaries and cleanup. They do not claim qualification against an independently installed published SDK wheel.

The disposable fixture runs a digest-pinned Python HTTPS origin on Docker's outbound bridge, with a generated test CA and synthetic credentials. The harness adds that CA and fixed host mappings only to the disposable proxy. Requests from the sandbox must demonstrate credential replacement on an allowed origin, authorization stripping on an unbound allowed host, method/path/host refusal, blocked direct access to the origin, absence of credentials from guest grant/key paths and engine metadata/logs, and grant expiry. Host refusal also needs a DENY audit decision, so DNS failure cannot stand in for policy enforcement. Success requires removal of all scoped containers and networks. Raw credentials, private keys and complete engine metadata are not retained in the release report.

These observations qualify one bounded Docker configuration with controlled endpoints. They do not establish general network containment, cross-tenant isolation in your application, arbitrary upstream protocol compatibility, WSLC operation, or production deployment readiness. Test the SDK/backend/image pairing and application policy you will actually deploy. Image execution is a release gate, not part of each iterative PR check.

For a development build from the repository root:

```sh
bash scripts/build_scan_image.sh egress-proxy
```

This produces the local `maf-image-scan:target` tag. It is neither signed nor published. The protected release workflow repeats runtime checks against retained immutable bytes, requires a passing inventory and vulnerability assessment, and waits for approval before publication.

## Support

Report defects through [repository issues](https://github.com/sokolaidev/maf-extensions/issues), including the image version and digest, SDK and engine versions, a redacted policy and minimal reproduction. Never include grants, private keys or complete unredacted container inspection. Follow [SECURITY.md](../../SECURITY.md) for private vulnerability reports. The packages remain experimental; community maintenance provides no production-support SLA.
