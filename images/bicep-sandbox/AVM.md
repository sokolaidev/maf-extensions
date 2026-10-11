# Bicep AVM container image

Validate Bicep templates that use the selected Azure Verified Modules with `ghcr.io/sokolaidev/maf-extensions/bicep-avm`. The image contains the Bicep compiler, a pinned module cache and its verification receipt. It has no HTTP service, agent application, Python runtime or Azure credentials, and does not deploy resources.

**Availability:** [Bicep AVM 0.1.0](https://github.com/sokolaidev/maf-extensions/releases/tag/image-bicep-avm-v0.1.0) is publicly available for Linux/amd64 with immutable signed evidence and anonymous-pull qualification. Pin `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e` after verifying the release below. Check the [public status report](https://sokolaidev.github.io/maf-extensions/) for current monitoring. Image versions are independent of the Bicep compiler and Python package versions.

## Included modules

| Component | Selected version |
|---|---|
| Bicep compiler | 0.46.1 |
| `br/public:avm/res/network/virtual-network` | 0.7.2 |
| `br/public:avm/res/storage/storage-account` | 0.31.0 |

This is a selected module set, not the full AVM catalogue. Other modules and versions require a reviewed policy/manifest update and another image release. The [policy](dependencies.bicep-avm.policy.json) defines exclusions; the [manifest](dependencies.bicep-avm.json) pins OCI module digests. The image retains `/opt/maf-bicep/dependencies.json`, including policy/manifest fingerprints and cached file hashes. [Preparation](README.md#prepared-avm-profile) checks downloaded module bytes and initializes the cache during the build. Compilation uses `--no-restore`; an uncached version is refused rather than fetched.

The initial platform is Linux/amd64. Runtime checks use a Linux-container Docker engine with closed networking, a read-only root, temporary scratch space and bounded CPU, memory, processes and execution time. The SDK must run on the host and use a separately configured backend. A Docker result does not establish ACAS import, WSLC support or production deployment.

## Verify a release before execution

Use a reviewed checkout of this repository and a GitHub CLI supporting `gh attestation verify`. Review the completed release's source and workflow, then create an application-owned `bicep-avm-policy.json` with `profile` set to `bicep-avm` and the approved `version`, `sourceCommit`, `sourceRef`, `registryDigest`, `assessedManifestDigest`, `imageId` and `attemptId`. The registry/assessed digest identifies the runnable manifest; `imageId` is its configuration identity. Choose expected values independently of the downloaded candidate's claims. The [release contract](../../docs/security/container-image-releases.md) describes the identity policy; the originating `attemptId` is informational and is not authenticated by the consumer verifier.

For release 0.1.0, review [source `e2602e5d`](https://github.com/sokolaidev/maf-extensions/tree/e2602e5dfc35d456d66b0c346b41abb5c3434aac) and [publication run 38160334716](https://github.com/sokolaidev/maf-extensions/actions/runs/38160334716) before adopting this policy:

```json
{
  "profile": "bicep-avm",
  "version": "0.1.0",
  "sourceCommit": "e2602e5dfc35d456d66b0c346b41abb5c3434aac",
  "sourceRef": "refs/heads/main",
  "registryDigest": "sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e",
  "assessedManifestDigest": "sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e",
  "imageId": "sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3",
  "attemptId": "38160334716"
}
```

From the reviewed checkout, download all assets of the selected immutable release into a new evidence directory and verify its retained bundles:

```bash
set -eu
VERSION="$(python3 -c 'import json; print(json.load(open("bicep-avm-policy.json"))["version"])')"
EVIDENCE="bicep-avm-evidence-$VERSION"
mkdir "$EVIDENCE"
gh release download "image-bicep-avm-v$VERSION" \
  --repo sokolaidev/maf-extensions --dir "$EVIDENCE"
python3 scripts/verify_container_release.py \
  --policy bicep-avm-policy.json --evidence "$EVIDENCE" --bundles
```

Require a successful exit and `releaseIdentityVerified: true` before pulling and executing the image. Verification checks provenance, the SPDX inventory, signed completion and indexed evidence. Inspect monitoring status separately and apply your application's vulnerability policy: stale, unavailable, failing or retired monitoring is not a clean result. Publication requires no High/Critical findings, including unfixed findings; retain lower-severity findings rather than describing this as a zero-vulnerability guarantee.

## Configure the consumer

After verification, pin `ghcr.io/sokolaidev/maf-extensions/bicep-avm@<approved-registry-digest>` in the host configuration. Never substitute a mutable tag or Docker configuration ID for that pull reference.

The Bicep tool stages its own configuration into each call. Copy [prepared.bicepconfig.json](prepared.bicepconfig.json) into the host's reviewed configuration, or add `"cacheRootDirectory": "/opt/maf-bicep/cache"` and disable the online `use-recent-module-versions` rule in the host's existing policy. Pass this JSON as `make_bicep_tools(..., config=...)` along with the verified `image` and `egress=Egress.CLOSED`; the [integration example](README.md#prepared-avm-profile) shows the existing factory wiring. The image's fallback configuration does not override the host policy. Omitting the cache configuration gives an incomplete module-restore result even for a cached module.

For example, the selected cache supports this source without an Azure account or runtime registry access:

```bicep
module network 'br/public:avm/res/network/virtual-network:0.7.2' = {
  name: 'network'
  params: {
    name: 'example-network'
    addressPrefixes: ['10.0.0.0/16']
  }
}
```

Valid input produces compiler validation results, not an Azure deployment. Invalid parameters produce diagnostics; uncached modules produce `BCP190` and an incomplete result. Keep the backend's timeout, file and resource boundaries in place when validating untrusted source.

## Release 0.1.0 evidence

On 2026-10-11, [publication run 38160334716](https://github.com/sokolaidev/maf-extensions/actions/runs/38160334716) completed protected approval, registry publication, provenance/SPDX attestations, anonymous pull, qualification and signed completion for the identity above. Its [immutable evidence](https://github.com/sokolaidev/maf-extensions/releases/tag/image-bicep-avm-v0.1.0) includes `qualification.json`: on a fresh runner, both selected modules compiled with networking disabled; invalid parameters returned `BCP036`, while an uncached module version and missing cache configuration returned `BCP190`. All five checks passed, outputs appeared only for valid cases, the receipt matched the reviewed module pins and no probe containers remained. Full release verification of the retained signature bundles independently returned `releaseIdentityVerified: true`.

The publication inventory contains 192 components from Syft 1.54.0. Grype 0.120.0 reported zero matches and zero ignored matches at 17:39:20 UTC, using the valid database built at 06:30:42 UTC; the gate included unfixed High/Critical findings. [First monitoring run 38161720673](https://github.com/sokolaidev/maf-extensions/actions/runs/38161720673) assessed the same digest at 17:59:11 UTC with no findings. [Pages deployment 38162017451](https://github.com/sokolaidev/maf-extensions/actions/runs/38162017451) published catalogue sequence 26; the publicly served catalogue matched the authoritative snapshot byte-for-byte when checked. These are dated scan results, not a guarantee of absent vulnerabilities. Current monitoring remains separate from release identity.

This completes the selected two-module Linux/amd64 image scope. The retained runtime checks exercise the compiler directly; local SDK development checks described below are separate evidence. ACAS import, WSLC execution, other module versions and application-specific production deployment remain unverified.

## Qualification and updates

The release pipeline runs the same offline gate before approval and again after signed-candidate verification and anonymous pull on a fresh runner. It checks both selected modules, invalid parameters, an uncached module version and missing cache configuration. Success requires the expected exit codes and diagnostics, compiled output only for valid inputs, a receipt matching the reviewed policy/manifest and no remaining probe container after cleanup. The immutable release's `qualification.json` retains these results under `runtime.bicepAvm`; ordinary PR updates do not execute images.

For local development, build the base and cache using the [existing build instructions](README.md#prepared-avm-profile), then run the [SDK checks](../../tests/test_bicep_prepared_offline.py) with `MAF_BICEP_PREPARED_IMAGE` set to the immutable local image identity. That environment variable names the internal preparation fixture; the public release profile is `bicep-avm`. A local build has no signed release completion and is not interchangeable with a published digest.

Changes to the compiler, module set, base packages or policy require a new reviewed image identity. Verify that release and requalify your templates before changing the application pin. Preserve the compiler's [MIT license](BICEP-LICENSE), cached module/source notices and operating-system license files when redistributing the image. Use the [security policy](../../SECURITY.md) for private vulnerability reporting and [repository issues](https://github.com/sokolaidev/maf-extensions/issues) for usage problems, including the digest, platform, backend/SDK versions and a minimal non-sensitive example.
