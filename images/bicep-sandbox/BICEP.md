# Bicep container image

Use `ghcr.io/sokolaidev/maf-extensions/bicep` to compile and validate local Bicep source. The base image contains the Bicep CLI and a fallback validation configuration. It does not include an AVM module cache, Python, an agent, an HTTP service or Azure credentials, and compilation does not deploy resources.

**Publication:** the first public release is tracked in [#1837](https://github.com/sokolaidev/maf-extensions/issues/1837). Build and local qualification results do not establish publication. Select a completed `image-bicep-v<version>` entry in [GitHub Releases](https://github.com/sokolaidev/maf-extensions/releases) and inspect the [public image status report](https://sokolaidev.github.io/maf-extensions/) before adopting it. Image release versions are independent of compiler and Python package versions.

## Contents and scope

| Component | Selection |
|---|---|
| Platform | Linux/amd64 |
| Bicep compiler | Version and download SHA-256 pinned in the release source's [Dockerfile](Dockerfile) |
| Operating system | Azure Linux 3.0 with ICU, CA certificates and curl; exact installed inventory retained in the release SBOM |
| Fallback configuration | `/maf-sandbox/work/bicepconfig.json`, from [bicepconfig.json](bicepconfig.json) |
| Compiler license | `/usr/share/licenses/bicep/LICENSE`, from [BICEP-LICENSE](BICEP-LICENSE) |

A released digest keeps its original compiler and OS packages even after these source files change. The OS base tag is refreshed during a build; rebuilding the same source may produce different bytes. Select the signed release digest, not a locally reconstructed image, for a reproducible consumer identity.

Local source works with networking disabled. Registry modules absent from the image cannot be compiled with `--no-restore`; the compiler returns `BCP190`. Use the separate [Bicep AVM profile](AVM.md) for its documented preloaded module set. Other module sets and online restore require a separate application decision and are outside the base image's offline qualification.

## Verify before execution

Use a reviewed repository checkout and a GitHub CLI supporting `gh attestation verify`. Review the chosen completed release's source commit and publication run. Create an application-owned `bicep-policy.json` containing `profile: "bicep"`, the approved `version`, `sourceCommit`, `sourceRef`, `registryDigest`, `assessedManifestDigest`, `imageId` and `attemptId`. Choose expected identities independently of the downloaded candidate's claims. The [release contract](../../docs/security/container-image-releases.md) describes each field; `attemptId` is informational and is not authenticated by the consumer verifier.

From the reviewed checkout, download all assets into a new directory and verify the retained bundles:

```bash
set -eu
VERSION="$(python3 -c 'import json; print(json.load(open("bicep-policy.json"))["version"])')"
EVIDENCE="bicep-evidence-$VERSION"
mkdir "$EVIDENCE"
gh release download "image-bicep-v$VERSION" \
  --repo sokolaidev/maf-extensions --dir "$EVIDENCE"
python3 scripts/verify_container_release.py \
  --policy bicep-policy.json --evidence "$EVIDENCE" --bundles
```

Require successful verification and `releaseIdentityVerified: true` before execution. Pin `ghcr.io/sokolaidev/maf-extensions/bicep@<approved-registry-digest>`; the registry digest identifies the runnable manifest, whereas `imageId` is the Docker configuration identity and cannot replace it in the pull reference.

Release identity and monitoring are separate results. Inspect the public report's assessment time and findings and apply your application's vulnerability policy. Publication requires no High/Critical findings, including unfixed findings; lower-severity findings remain visible. A stale, unavailable, failing or retired monitoring result is not clean, and a passing scan does not establish absence of malware or backdoors or suitability for every production application.

## Compile local input

After verification, set `BICEP_IMAGE` to the approved digest reference. This Linux-shell example streams a local `main.bicep` through standard input, copies the image's fallback policy into scratch space, disables networking and removes the container on exit:

```bash
set -eu
: "${BICEP_IMAGE:?Set the verified GHCR digest reference}"
docker run --rm -i --platform linux/amd64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
  --memory 1g --cpus 2 --tmpfs /tmp:rw,exec,size=512m \
  --env DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet-bundle \
  --workdir /tmp --entrypoint sh "$BICEP_IMAGE" -ec '
    export HOME=/tmp
    cp /maf-sandbox/work/bicepconfig.json /tmp/bicepconfig.json
    cat > /tmp/main.bicep
    bicep build /tmp/main.bicep --no-restore --outfile /tmp/main.json
    cat /tmp/main.json
  ' < main.bicep
```

For example, `param name string = 'example'` followed by `output result string = name` compiles to an ARM template. An incompatible value such as `output result string = 42` fails with compiler diagnostics. Use a backend that stages all local module files, preserving their relative paths, when your source uses local modules; this example stages only `main.bicep`. For untrusted input, use a supervised backend with a wall-clock timeout and bounded file/output handling; Docker resource limits alone do not provide that timeout.

Bicep discovers `bicepconfig.json` by walking upward from the source file. Under `/maf-sandbox/work`, the baked fallback makes `no-unused-params` an error and enables `use-recent-api-versions` warnings. A file under `/tmp` does not inherit that fallback unless it is copied there, as above. A nearer configuration overrides the fallback. [Source/build documentation](README.md) explains the configuration and available backend integrations.

For the host SDK, configure `make_bicep_tools(..., image=<verified-digest-reference>, egress=Egress.CLOSED)` with a separately configured backend/router. The kind stages its own packaged or explicitly supplied `config` for every call; the image fallback does not override it. Keep the host's SDK versions, validation policy and backend limits in your application configuration. The [Bicep kind guide](../../docs/sandbox/kinds/bicep.md) describes that integration.

## Retained qualification and updates

The release pipeline runs six direct compiler probes before approval and again after signed candidate verification and anonymous pull on a fresh runner: valid local input, an invalid type, fallback-policy enforcement, default-policy behavior outside the fallback directory, a staged-policy override and uncached-module rejection. The checks compare the installed compiler, configuration and license hashes with the selected source, inspect compiled templates and exact diagnostics, and require container removal. Results retain the compiler version, Docker CLI backend, disabled network and cleanup observation under `bicep` in `runtime.json` and `runtime.bicep` in `qualification.json`. Ordinary PR updates test the gate logic without executing images.

These checks qualify the compiler directly under Docker. They do not establish SDK/backend integration, ACAS import, WSLC execution, Azure deployment or production readiness. The public release evidence and #1837 record the actual hosted results; this guide does not substitute for them.

Compiler, base-package or policy changes require a new reviewed image identity. Verify the replacement and requalify your templates before changing the application pin. Preserve compiler and OS notices when redistributing. Report vulnerabilities privately using [SECURITY.md](../../SECURITY.md); for usage problems, open a [repository issue](https://github.com/sokolaidev/maf-extensions/issues) with the image digest, platform, backend/SDK versions and a minimal non-sensitive example.
