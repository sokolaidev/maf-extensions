# Shell validation image

## Published image and ACAS verification

The first published image is [shell 0.1.0](https://github.com/sokolaidev/maf-extensions/releases/tag/image-shell-v0.1.0), built from `a9bf6d61f4425d2ab0d06d598744dc457e930e92`. Its immutable reference is `ghcr.io/sokolaidev/maf-extensions/shell@sha256:3051b9c39e0f731836d5a30484c2f2cec3df5ae1031df3e73a9de18653250a31`; its configuration identity is `sha256:3e90c0104d992953d72fb7c04d8268c842b3c891e9675179ac0f79ace5fb8456`. [Publication run 40242032430](https://github.com/sokolaidev/maf-extensions/actions/runs/40242032430) completed preparation, anonymous-pull Docker qualification, signing and evidence delivery. The matching kind is [maf-sandbox-shell 0.1.1](https://github.com/sokolaidev/maf-extensions/releases/tag/maf-sandbox-shell-v0.1.1). Authenticate the release assets using the procedure below before importing the digest.

`Verify (live)` includes a separate `shell-acas` job for shell, core and ACAS releases and manual verification. It runs five deterministic public-tool cases against published packages by default: Bash and PowerShell findings, empty files for both, and PowerShell module rules. Each call must preserve its source store and request closed egress. The harness observes each acquired physical sandbox through an independent service client and requires a service 404 after per-call disposal, before the final scope purge. A surviving sandbox, failed lookup, missing import or skipped execution cannot qualify the run. This does not test network denial independently. Automatic verification never imports an image; first-time manual setup can select `import_shell_image=true` with `package=maf-sandbox-shell` after setting the verified digest.

Import the verified digest with the [ACAS importer](../../packages/maf-sandbox-acas/scripts/README.md), then set the `live-verify` environment variable `MAF_SHELL_ACAS_IMAGE` to that fully qualified digest reference. The independent shell job's preflight requires exactly one matching imported snapshot; the harness pins its disk-image ID and retains the mapping in `shell-acas.json`. Configure the existing `ACAS_SANDBOX_*` scope and Azure OIDC identity. No model credentials are needed for this job. Local execution with the same environment and an Azure CLI login is `uv run --no-project scripts/qualify_shell_acas.py --report shell-acas.json`. This uses published wheels; use the workflow's `source: branch` only when testing checkout packages.

ACAS runtime qualification remains unverified. The first [manual import attempt](https://github.com/sokolaidev/maf-extensions/actions/runs/40372916164) reached the original ten-minute prerequisite-job timeout. A [read-only follow-up](https://github.com/sokolaidev/maf-extensions/actions/runs/40374628199) found no matching imported snapshot, so the five ACAS cases did not start. The optional import step now has a thirty-minute limit within a fifty-five-minute shell job; ordinary shell verification retains twenty-five minutes. Shared ACAS preflight retains ten minutes and does not depend on the shell import. Diagnose the outstanding import before creating another snapshot, then rerun with `import_shell_image=false`. This additional work in [#2197](https://github.com/sokolaidev/maf-extensions/pull/2197) does not change the Docker-only acceptance of [#2164](https://github.com/sokolaidev/maf-extensions/issues/2164) recorded in [#2196](https://github.com/sokolaidev/maf-extensions/pull/2196). The historical local records below describe earlier candidates and do not supersede the published release evidence.


The `shell` release profile combines Bash, dash, ShellCheck, PowerShell and PSScriptAnalyzer in a Linux amd64 image. `image.json` pins each analyzer and interpreter by version and archive SHA256; the base image is pinned by digest. The normal image release pipeline builds, scans, signs and records this profile. Adding the profile does not publish an image.

The runtime uses a patched Wolfi base, Bash 5.3-r14 and dash 0.5.13.5-r2. Interpreter APK archives are authenticated through the signed repository index and checked against their pinned SHA256 in an isolated cache before offline installation; dependencies come from the authenticated repository. The launcher requires those exact installed interpreter versions. PowerShell 7.6.8 and PSScriptAnalyzer 1.25.0 remain pinned. The ShellCheck adapter builds in a separate Debian stage; only its executable and required source/license material enter the runtime, whose shared libraries come from Wolfi. Refresh the base digest for OS security updates: its internal package constraints can keep installed versions unchanged during a normal `apk upgrade`.

Use the matching `maf-sandbox-shell` package release with this image: earlier packages reject the changed interpreter identities. Checkout qualification does not prove compatibility with previously published wheels. Repository-wide base and dependency-update standardization remains tracked separately in [#2178](https://github.com/sokolaidev/maf-extensions/issues/2178).

```bash
docker build --platform linux/amd64 -t maf-shell:local images/shell-sandbox
docker run --rm -i --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,exec,size=512m maf-shell:local python3 - < images/shell-sandbox/test_runner.py
```

The image runs as root for the ACAS file plane. The kind requests closed egress and call-scoped disposal. The launcher reads a single JSON bundle, removes it, and supplies each source through anonymous pipes. Submitted `.psm1` files never become filesystem modules. PowerShell command discovery can load host-installed module code, so that code belongs to the host image's trust boundary.

PowerShell uses the default text-analysis rules. Module inputs also receive an in-memory AST pass for the pinned analyzer's two module-only script rules, `PSAvoidGlobalFunctions` and `PSAvoidGlobalAliases`, including their suppressions. The AST carries the manifest name with a canonical `.psm1` suffix; reports retain the original path. Source-byte encoding rules have no original file to inspect. Runtime errors and analyzer warning-stream messages from either pass make analysis incomplete. Recheck the module-only rule set when upgrading PSScriptAnalyzer.

## Host dependencies

Prepare dependencies during a derived image build. Copy approved modules into `/opt/maf-shell/dependencies/modules` and managed assemblies into `/opt/maf-shell/dependencies/assemblies`, including their own pinned transitive dependencies. Do not install dependencies at tool-call time. Run `python3 /opt/maf-shell/prepare_dependencies.py` in the build; it writes the complete file/hash inventory and prints its SHA256. Pass that digest as `dependency_inventory_sha256` to `make_shell_tools`. The launcher checks the tree against that inventory before analysis; symlinks and mismatches fail validation. The fixed PowerShell helper loads every approved managed assembly before parsing; conflicting assembly identities or load failures make analysis incomplete. Reference assemblies by their fixed absolute guest path. Profiles, user module directories and inherited environment variables are excluded.

The `fixtures/` derived image qualifies host module discovery, an assembly reference and a submitted module with the same name. It is test input, not a release profile:

```bash
docker build --build-arg BASE=maf-shell:local -t maf-shell:dependencies images/shell-sandbox/fixtures
docker run --rm -i --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,exec,size=512m maf-shell:dependencies python3 - < images/shell-sandbox/fixtures/test_dependencies.py
```

## ShellCheck audit adapter

ShellCheck's normal diagnostic output omits unused suppression declarations. `ShellAudit.hs` uses the pinned upstream parser to capture every recognized disable clause with its scope and rule ranges. A second analysis ignores only those parsed disable annotations to identify suppressed findings. Strings and heredoc bodies are not scanned as comments. Source reads resolve only against the in-memory manifest, trying the direct path before declared source paths and expanding a leading `SCRIPTDIR` component. Parent components resolve lexically within the manifest root; absolute paths and root escapes cannot select manifest files. `/dev/null` is modeled as an empty source. Sourced files are checked in each caller's dialect in both analysis passes. Ambient `.shellcheckrc` files and external source reads are disabled. Shell-changing directives that contradict the selected dialect and author changes to optional/extended analysis are refused.

The linked adapter is GPL-3.0-or-later. The image includes the upstream source archive, adapter source, parser patch and upstream license under `/usr/share/maf-shell` and `/usr/share/licenses/shellcheck`. Rebuild with this Dockerfile and the pinned archive; the patch fails if the parser shape changes. The Python kind is MIT and communicates with this separate executable using JSON.

## Evidence boundary

The release pipeline runs the offline analyzer fixtures and eleven public SDK cases before approval and again against the anonymously pulled image after signature verification. Both flavors cover clean source, empty files, findings, syntax failures and suppression auditing; PowerShell also covers module-only findings. Each call verifies the selected immutable image, disabled networking, unchanged host store and disposal, including stopped containers. `runtime.json` retains these results under `shell`, with the tested checkout package versions and Docker engine. Post-publication evidence retains the same fields under `runtime.shell` in `qualification.json`. The approval summary presents the recorded cases and reports missing evidence as unavailable.

The ACAS test is opt-in with `MAF_SHELL_ACAS_IMAGE` plus the existing `ACAS_SANDBOX_*` configuration and a previously imported host image. Neither unit tests nor Docker results establish ACAS qualification, publication or deployment success. Image delivery and ACAS qualification are tracked in [#2164](https://github.com/sokolaidev/maf-extensions/issues/2164).

## Select and verify a published image

Use the `shell` repository under `ghcr.io/sokolaidev/maf-extensions/`. Select a completed `image-shell-v<version>` entry in [GitHub Releases](https://github.com/sokolaidev/maf-extensions/releases), and review its source revision and the [current monitoring report](https://sokolaidev.github.io/maf-extensions/). A local build or a registered profile is not a completed release.

From a reviewed checkout, prepare an application-owned `shell-policy.json` with the independently approved `profile` (`shell`), `version`, `sourceCommit`, `sourceRef`, `registryDigest`, `assessedManifestDigest`, `imageId` and `attemptId`. The [release contract](../../docs/security/container-image-releases.md) defines these fields and the distinction between release identity and current monitoring; `attemptId` is informational. Download the selected release's assets into a new directory, then authenticate the retained evidence before execution:

```bash
set -eu
VERSION="$(python3 -c 'import json; print(json.load(open("shell-policy.json"))["version"])')"
EVIDENCE="shell-evidence-$VERSION"
mkdir "$EVIDENCE"
gh release download "image-shell-v$VERSION" \
  --repo sokolaidev/maf-extensions --dir "$EVIDENCE"
python3 scripts/verify_container_release.py \
  --policy shell-policy.json --evidence "$EVIDENCE" --bundles
```

Require successful verification and `releaseIdentityVerified: true`. Pull `ghcr.io/sokolaidev/maf-extensions/shell@<approved-registry-digest>`, then compare the local configuration identity with the approved `imageId`. The registry manifest digest and Docker configuration identity are distinct. Hosts with an image store that cannot provide the configuration identity are not qualified for the release checker; do not substitute a mutable tag.

After pulling and checking that identity, rerun the release checks locally and retain their report in a separate directory:

```bash
uv sync --locked
uv run --locked python scripts/check_container_release_image.py \
  --candidate shell-policy.json --directory shell-local-qualification
```

This runs the reviewed checkout SDK with the selected image; `sdkSource: checkout` does not claim that separately published Python wheels were tested. Configure `make_shell_tools` with the verified registry digest reference and the host-selected `ShellFlavor`. Keep policy, backend limits and dependency-inventory selection in the host configuration; see the [kind guide](../../docs/sandbox/kinds/shell.md).

## Release and ACAS handoff

A maintainer selects `shell`, an unused image version and reviewed `main` through the existing [container image release workflow](../../.github/workflows/container-image-release.yml). Preparation builds and tests once, retains the candidate and scans it; the protected approval controls publication of those bytes. Completion requires the normal strict High/Critical gate, provenance and SBOM attestations, anonymous-pull checks and delivered completion evidence. Do not infer publication from preparation or approval alone, or rebuild an interrupted candidate under the same version.

For ACAS, import the verified published digest into the configured sandbox group using the host's normal image preparation process. Set `MAF_SHELL_ACAS_IMAGE` to that imported fully qualified digest reference and provide `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP` and `ACAS_SANDBOX_GROUP`, plus `ACAS_SANDBOX_REGISTRY` when applicable. Keep actual resource identifiers outside repository files. Run `uv run --locked pytest tests/test_shell_backends.py -k acas -q -ra`. Retain the image import-to-digest mapping, package versions, result log and service-side disposal evidence; skipped tests do not qualify the image. These checks are separate from the protected image publisher and do not deploy user scripts.

## Local qualification, 2026-10-25

### Initial image

The image sources at `759517296717310172e7d768bb4e13550005a5f4`, tested with the qualification harness added for #2164, built on Docker Desktop 29.9.2 in Linux/amd64 mode. All 15 analyzer fixtures and 11 public SDK cases passed, with closed networking, unchanged stores and no remaining containers. The checkout distributions reported `maf-sandbox` 0.51.1, `maf-sandbox-docker` 0.29.0, `maf-sandbox-shell` 0.1.0 and `agent-framework-core` 1.22.0. This was local checkout testing, not hosted CI or published-wheel qualification.

| Evidence | Observed value |
|---|---|
| Docker Desktop image identity used by the runtime checks | `sha256:05783f63736d35ad6d31eb43668f7fb2ad47493b52a9b7defce0cca76f9e04bc` |
| Exported configuration digest, independently hashed and matched to the SBOM and Grype target | `sha256:31c2c564f3ce9febb2a8bbd19dc8f224b2fbc6c0052d8e584a819bf495e9bd6d` |
| Inventory | Syft 1.54.0; 381 components |
| Assessment | Grype 0.120.0; valid database built `2026-10-25T06:27:34Z` |
| Match counts | 3 Critical, 79 High, 86 Medium, 17 Low, 70 Negligible |
| High/Critical fix states | 4 fixed, 52 not-fixed, 26 wont-fix; 28 distinct advisories across 82 matches |
| Syft report SHA256 | `efda3c2a878b54cb9f8f9342cf3693ace84efb00d3e6497aa24408a5289543dc` |
| Grype report SHA256 | `ff600d51e0e7b3e36e3b58fe26128c75d09974fbb2e9c55a56647d4ce3f297fd` |

The strict publication gate **failed**. The Critical matches were `CVE-2026-5450` in `libc6` and `libc-bin`, and `CVE-2025-7458` in `libsqlite3-0`, all without a listed fix for the installed Debian packages. The four fixable matches affected bundled `System.Security.Cryptography.Xml` and `System.Security.Cryptography.Pkcs` 10.0.10, with 10.0.13 listed as fixed. Updating those assemblies alone cannot clear the remaining OS findings. Base/package remediation and a fresh complete assessment are required; no exclusions were applied.

### Updated image

The updated recipe uses Debian 13, Bash 5.2.37, dash 0.5.12-12 and PowerShell 7.6.8. ShellCheck 0.11.0 and PSScriptAnalyzer 1.25.0 remain pinned. The ShellCheck adapter selects its upstream Haskell 98 language and includes the NUMA runtime library required by the newer compiler. The kind's expected analyzer identities change with the recipe: the published `maf-sandbox-shell` 0.1.0 rejects this image. Use the matching updated checkout for qualification and wait for its normal package release before using published wheels; the checkout's unchanged version metadata does not establish published-wheel compatibility.

The final image passed all 15 analyzer fixtures and 11 public SDK cases on the same Docker engine, including network and disposal observations. The exported configuration was independently hashed and matched to both scanner reports.

| Evidence | Observed value |
|---|---|
| Docker Desktop image identity used by the runtime checks | `sha256:785f92a0dae85fc854e105948dfc5be4849b3c0bb70a8af82df8cdaed9647c2a` |
| Exported configuration digest | `sha256:ef688a2119ff84b2fe47bdaa5bbe4f96c63912106df48651a4f39eb47f21246f` |
| Inventory | Syft 1.54.0; 372 components |
| Assessment | Grype 0.120.0; valid database built `2026-10-25T06:27:34Z` |
| Match counts | 0 Critical, 55 High, 50 Medium, 10 Low, 46 Negligible |
| High fix states | 54 wont-fix, 1 not-fixed; 14 distinct advisories across 55 matches |
| Syft report SHA256 | `9026874fbee291a128e42c5026674144f54bd06c6473ee05d07a4ec2750b464f` |
| Grype report SHA256 | `e214957ca814faeb6b58fa45028582d91ed5ce6cec8c0e33ea8a966e4e94f416` |

The update removes the three Critical matches and the bundled .NET findings from this assessment. The remaining High matches affect Debian packages, including libc, util-linux, ncurses, Perl, GCC runtime libraries, zlib and ACL libraries. No fixes are listed for the installed versions. The strict gate still **fails**; no exclusions or policy changes were applied.

The local Docker store exposes an image index identity, distinct from the configuration identity in the exported archive. The export was scanned without claiming a registry publication identity or a protected retained-candidate round trip. No image was published or signed, and ACAS import and execution were not run. #2164 remains open for remediation, publication and ACAS qualification.

### Wolfi runtime

The shell-only remediation pins Wolfi base `sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7`, including Brotli 1.2.1-r1. Reusing the older base from other profiles retained Brotli 1.2.0-r5 and failed on three Critical matches for `CVE-2026-111497`; updating the base digest removed those findings. This change leaves other image profiles and the broader migration in #2178 for separate work.

| Evidence | Observed value |
|---|---|
| Docker Desktop image identity used by the runtime checks | `sha256:bf77f6e6913962af3bb3425d574ca87a35d8cf89b23a1682e93640fe83d24ce2` |
| Exported configuration digest, independently hashed and matched to both scanner targets | `sha256:885b9bcf8a5f1ea0a20ab2a512c7847f63bdedcd51bbb826de51797085214418` |
| Inventory | Syft 1.54.0; 326 components |
| Assessment | Grype 0.120.0; valid database built `2026-10-25T06:27:34Z` |
| Match counts | 0 Critical, 0 High, 2 Medium, 0 Low, 0 Negligible, 0 Unknown |
| Syft report SHA256 | `52c9f373b25de3271fd88635d908816fff95a95022f80664ca62fbbc932178f6` |
| Grype report SHA256 | `10acb339fd16b698d3091a8d9b7b093523dd8ec972a2c8ea73626f7151751521` |

The strict `grype --fail-on high` gate **passes** without exclusions or policy changes. The two remaining Medium matches are `CVE-2025-15367` and `CVE-2026-12345` in `python-3.13` 3.13.16_git20261016-r2, with no fixed version listed. A passing threshold does not mean the image has no vulnerability findings. The protected release workflow must assess its own retained candidate before publication; this local export was not published or signed, and ACAS remains unqualified.

This candidate passed all 15 analyzer fixtures and 11 public SDK cases on Docker Desktop 29.9.2/Linux amd64, with closed networking, unchanged stores and no remaining containers. The checkout distribution versions were unchanged from the earlier local runs above. The derived-image fixtures also passed host module discovery, the managed assembly reference and submitted-module shadowing. These results qualify the tested checkout with the local image; they do not establish published-wheel compatibility, hosted CI, a protected retained-candidate round trip or ACAS qualification. #2164 remains open for publication and ACAS evidence.
