# Bicep image for Docker Sandboxes

`ghcr.io/sokolaidev/maf-extensions/bicep-docker-sbx` supplies the Bicep compiler and workspace utilities required by `maf-sandbox-docker-sbx`. It is a Linux/amd64 workload template for Docker Sandboxes (`sbx`), not a sandbox daemon, Python SDK or Azure deployment service.

The first completed release is tracked in [#1839](https://github.com/sokolaidev/maf-extensions/issues/1839), under [#1836](https://github.com/sokolaidev/maf-extensions/issues/1836). Select a completed `image-bicep-docker-sbx-v<version>` entry in [GitHub Releases](https://github.com/sokolaidev/maf-extensions/releases); a merged change or passing development build does not establish publication. Image, compiler, sbx and Python package versions are independent.

## Contents and differences

| Component | Selection and purpose |
|---|---|
| Bicep | The release source's [compiler pin](../bicep-sandbox/Dockerfile), acquired as a verified upstream binary or a reviewed [source build](../bicep-sandbox/README.md#choose-the-compiler-route) |
| Operating system | Azure Linux 3.0; exact installed packages are retained in the release SBOM |
| Workspace utilities | bash, util-linux, coreutils and user utilities from the [generic sbx wrapper](../sbx-template/Dockerfile); supports the backend's user/mount namespaces and file transfer |
| Compiler notice | `/usr/share/licenses/bicep/LICENSE` |
| Repository notice | `/licenses/maf-extensions-LICENSE` |
| Working directory | `/maf-sandbox` is absent from the image so the backend can bind its own private workspace there |

Unlike the base [Bicep image](../bicep-sandbox/BICEP.md), this template removes the baked `/maf-sandbox/work/bicepconfig.json` together with that directory. The Bicep kind stages its packaged or explicitly supplied configuration for each call. A direct compiler caller must stage its own policy; without one, Bicep uses its defaults. The image starts as root for backend mount setup; the backend supervises each guest command in its own namespace. Changing the user or recreating `/maf-sandbox` in a derived image can break that setup.

There is no AVM cache, Azure CLI, Python runtime, agent or Azure credential in this profile. Local compilation does not deploy resources. Uncached registry modules fail with `--no-restore`; online restore, AVM modules and other architectures need separate qualification. Base tags can resolve to newer OS packages during a rebuild, so reproduce consumer identity with the signed digest, not by assuming the same source produces identical bytes.

## Verify and load

Follow the [consumer verification procedure](../../docs/security/container-image-releases.md#evidence-and-verification) before execution. Independently approve the `bicep-docker-sbx` profile, version, source commit and digest identities, download all assets for that completed release into a new directory, and run `scripts/verify_container_release.py --policy <policy.json> --evidence <directory> --bundles` from a reviewed checkout. Require `releaseIdentityVerified: true`. The GHCR manifest digest is the pull reference; `imageId` is the separate Docker configuration digest.

For a Linux shell, after verification:

```bash
set -eu
: "${BICEP_SBX_IMAGE:?Set ghcr.io/sokolaidev/maf-extensions/bicep-docker-sbx@sha256:<approved-manifest-digest>}"
: "${BICEP_SBX_IMAGE_ID:?Set the independently approved sha256:<configuration-digest>}"
docker pull --platform linux/amd64 "$BICEP_SBX_IMAGE"
test "$(docker image inspect --format '{{.Id}}' "$BICEP_SBX_IMAGE")" = "$BICEP_SBX_IMAGE_ID"
TEMPLATE="maf-bicep-sbx:$(printf %s "$BICEP_SBX_IMAGE_ID" | cut -d: -f2)"
docker tag "$BICEP_SBX_IMAGE_ID" "$TEMPLATE"
docker save --output bicep-docker-sbx.tar "$TEMPLATE"
sbx template load bicep-docker-sbx.tar
```

Loading is necessary because Docker Engine and sbx have separate image stores. Keep that local template tag bound to the verified bytes and pass it as the Bicep kind's `image` argument. Do not pass a GHCR reference and assume sbx is using the image that Docker Engine verified. The qualification helper uses a fresh temporary tag, verifies the exported archive's configuration digest and platform before loading, and removes that tag after disposing its sandbox.

## Host and SDK setup

Install a supported [Docker Sandboxes host](https://docs.docker.com/ai/sandboxes/install/) with virtualization enabled, Docker Engine for pulling/exporting, and an authenticated sbx CLI. The release runner uses Ubuntu 24.04 with `/dev/kvm`; the actual sbx version and host are retained in the evidence. Windows and macOS hosts require their own runtime qualification. An sbx account login does not replace the release workflow's separate anonymous GHCR pull verification.

On a dedicated host, initialize sbx's deny-all policy, disable SSH agent forwarding and restart its daemon before using the backend. These are host-wide settings: coordinate them with other users of that host. The repository's [setup action](../../.github/actions/setup-sbx/action.yml) shows the release runner setup. `SbxSandboxBackend` also refuses incompatible host configuration, forwarded capabilities and unapproved sandbox state; do not bypass those refusals.

Configure `SbxSandboxBackend(SbxSandboxConfig(...))` with an application-owned workspace root, CPU/memory limits and timeouts. Register it in a `SandboxRouter`, then call `make_bicep_tools(..., image=TEMPLATE, egress=Egress.CLOSED)` using the loaded local template name above. Consult the [backend guide](../../packages/maf-sandbox-docker-sbx/README.md) and [Bicep kind guide](../../docs/sandbox/kinds/bicep.md) for complete host integration and configuration handling. Dispose the application scope when finished; do not delete an attached workspace to simulate successful cleanup.

## Qualification and security evidence

Candidate preparation and fresh-consumer qualification call `scripts/bicep_docker_sbx_qualification.py` through the common release checker. The post-publication stage first verifies the signed candidate and pulls its public GHCR digest anonymously. Both checks use the SDK from the selected source checkout and record its package versions; they do not qualify an independently installed published wheel.

The checks verify template archive identity and platform, load it into sbx, acquire a sandbox through the actual backend, inspect workspace attachment, and prove SDK-to-guest and guest-to-SDK file transfer. Six compiler probes check valid local input, an invalid type, staged strict configuration, default configuration outside the staged directory, a nearer configuration override, and uncached-module refusal. Compiled output, exact diagnostics, compiler/notice hashes and source-build receipts are verified. Success requires sandbox disposal, an empty workspace root and template removal. `runtime.json` and `qualification.json` retain the measured versions, selected image identity, check results and cleanup evidence; approval summaries display these results before publication.

These checks establish one controlled Docker Sandboxes configuration. They do not prove general containment, Azure deployment, Windows/macOS compatibility or production readiness. Ordinary PR updates run mocked gate tests; actual image execution happens in the release gate and manual/scheduled image security workflow.

Publication refuses all High/Critical findings, including unfixed ones. Consult the [public security report](https://sokolaidev.github.io/maf-extensions/) for the current assessment time and findings, separately from signed release identity. A stale or unavailable assessment is not a clean result. Scanner results do not establish the absence of malware or backdoors. Test the exact SDK/backend/image pairing and application policy you plan to deploy.

## Development and support

From the repository root, `BICEP_ROUTE=source bash scripts/build_scan_image.sh bicep-docker-sbx` builds the pinned source route as `maf-image-scan:target`; use `BICEP_ROUTE=upstream` for the verified upstream binary. Both routes retain the sbx wrapper requirements and pass the same release gates. Internal fixtures such as `bicep-sandbox:sbx` are not public releases.

Preserve component notices when redistributing. Report usage defects through [repository issues](https://github.com/sokolaidev/maf-extensions/issues), including the image digest, sbx/backend versions, host platform and a minimal non-sensitive reproduction. Follow [SECURITY.md](../../SECURITY.md) for private vulnerability reports. Community-maintained experimental packages provide no production support SLA.
