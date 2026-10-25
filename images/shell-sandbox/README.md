# Shell validation image

The `shell` release profile combines Bash, dash, ShellCheck, PowerShell and PSScriptAnalyzer in a Linux amd64 image. `image.json` pins each analyzer and interpreter by version and archive SHA256; the base image is pinned by digest. The normal image release pipeline builds, scans, signs and records this profile. Adding the profile does not publish an image.

The runtime uses Wolfi with Bash 5.3-r14 and dash 0.5.13.5-r2. Interpreter archives are authenticated through the signed APK repository index and checksum-verified in an isolated cache before offline exact-version installation. The launcher checks installed package identities before analysis. PowerShell 7.6.8, PSScriptAnalyzer 1.25.0 and ShellCheck 0.11.0 remain pinned. Explicit security floors move all four glibc packages together beyond the base image's exact revision pins and require the patched certificate bundle. A separate Debian build stage compiles the ShellCheck adapter; only the executable and required source/license material enter the runtime, which supplies its native libraries from Wolfi. Use the matching `maf-sandbox-shell` release or checkout: earlier interpreter identities are rejected.

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

For ACAS, import the verified published digest into the configured sandbox group using the host's normal image preparation process. Set `MAF_SHELL_ACAS_IMAGE` to that imported image identifier and provide `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP` and `ACAS_SANDBOX_GROUP`, plus `ACAS_SANDBOX_REGISTRY` when applicable. Keep actual resource identifiers outside repository files. Run `uv run --locked pytest tests/test_shell_backends.py -k acas -q -ra`. Retain the image import-to-digest mapping, package versions, result log and service-side disposal evidence; skipped tests do not qualify the image. These checks are separate from the protected image publisher and do not deploy user scripts.

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

### Debian 13 image

The Debian 13 recipe used Bash 5.2.37, dash 0.5.12-12 and PowerShell 7.6.8. ShellCheck 0.11.0 and PSScriptAnalyzer 1.25.0 remain pinned. The ShellCheck adapter selects its upstream Haskell 98 language and includes the NUMA runtime library required by the newer compiler. The kind's expected analyzer identities change with the recipe: the published `maf-sandbox-shell` 0.1.0 rejects this image. Use the matching updated checkout for qualification and wait for its normal package release before using published wheels; the checkout's unchanged version metadata does not establish published-wheel compatibility.

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

### Wolfi image

The patched Wolfi candidate built on 2026-10-25 passed all 15 real-analyzer fixtures and all 11 public SDK cases, including closed networking, unchanged stores and no remaining containers. The derived-image module, assembly and submitted-module shadowing fixture also passed. The local Syft 1.54.0 inventory contains 325 components, and Grype 0.120.0 with its valid database built at `2026-10-25T06:27:34Z` reported **0 Critical, 0 High, 2 Medium, 0 Low and 0 Unknown** matches. The remaining Python advisories, `CVE-2025-15367` and `CVE-2026-12345`, have no fixes listed in that report. The strict gate passed without exclusions or severity changes. [PR #2180](https://github.com/sokolaidev/maf-extensions/pull/2180) records the hosted validation. This is candidate evidence from the matching checkout; published-wheel compatibility, publication and ACAS qualification remain outstanding under #2164.

| Evidence | Observed value |
|---|---|
| Local Docker Desktop runtime-test image identity | `sha256:d4fdb68378e92a10e686e860c0d1e5a1228a02eb14dbb52f9f7f91be71cbfe4d` |
| Build configuration digest, matching the inventory target | `sha256:3b8bf9db45a96027c3f0e73ae365200035f0d70c68c71b0c06af0051d08b3708` |
| Syft report SHA256 | `46c8a9596b9c9cbb1a3a54cb112e2ef739df6b3ba175f4b5a76c967f23d1f054` |
| Grype report SHA256 | `0cefb9d88e0f7942b7738d07869281443c7452b7d12c7a631554793da5a31e0c` |
