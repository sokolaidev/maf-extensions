# Graphviz container image

Render Graphviz DOT files to PNG with `ghcr.io/sokolaidev/maf-extensions/graphviz`. The image contains Graphviz, DejaVu fonts and their runtime libraries on a digest-pinned Wolfi base. It contains no agent application or Python runtime; you supply the DOT source and run `dot`.

| Consumer question | Contract |
|---|---|
| Platform | Published release profile: `linux/amd64`. Native ARM64 and Windows containers are not qualified. Docker Desktop must use Linux containers. |
| Interface | Command-line renderer, with no HTTP service or listening port. Invoke `dot` explicitly. |
| Input and output | DOT file or standard input; `dot -Tpng -o /output/diagram.png` writes a PNG. The release packaging probe exercises PNG; other Graphviz formats need your own validation. |
| Host dependencies | A Linux-container Docker engine for rendering. Python 3.12+ and GitHub CLI with `gh attestation verify` for the repository's release verifier. |
| Agent integration | [Docker diagram sample](../../samples/07_docker_diagram/) and its local `make_diagram_tools` implementation. This image does not install the host SDK or configure a model. |
| Release evidence | [Public status report](https://sokolaidev.github.io/maf-extensions/) and immutable GitHub Releases named `image-graphviz-v<VERSION>`. Image versions are independent of Python package versions. |

## Select a completed release

Start at the [status report](https://sokolaidev.github.io/maf-extensions/). Select a completed `graphviz` release with delivered evidence, inspect its source commit and workflow, and record the exact registry digest. A package page or version tag alone is insufficient: an image can be pushed and signed before public-pull qualification and completion succeed.

**Availability:** [Graphviz 0.1.0](https://github.com/sokolaidev/maf-extensions/releases/tag/image-graphviz-v0.1.0) is publicly available for Linux/amd64. Its immutable evidence includes provenance, an SPDX SBOM, signed completion and anonymous-pull qualification. Pin `ghcr.io/sokolaidev/maf-extensions/graphviz@sha256:c96936644f7dc2e9fa04a21ffc3274fbf52e5c045e978a5e27a2edf80a531e63`; check the status report for current monitoring before adopting it.

## Verify before pulling and running

Use a reviewed checkout of this repository for the verifier. From its root, create `graphviz-policy.json`. The policy below selects release 0.1.0 and pins its image digests and [source `e2c45419`](https://github.com/sokolaidev/maf-extensions/tree/e2c454192438496bbbd25ebf258b1f2a2756ffb7). Inspect [publication run 37820584534](https://github.com/sokolaidev/maf-extensions/actions/runs/37820584534) and review that source and workflow before accepting this identity; for another release, select its identity independently rather than copying the downloaded candidate's claims.

```json
{
  "profile": "graphviz",
  "version": "0.1.0",
  "sourceCommit": "e2c454192438496bbbd25ebf258b1f2a2756ffb7",
  "sourceRef": "refs/heads/main",
  "registryDigest": "sha256:c96936644f7dc2e9fa04a21ffc3274fbf52e5c045e978a5e27a2edf80a531e63",
  "assessedManifestDigest": "sha256:c96936644f7dc2e9fa04a21ffc3274fbf52e5c045e978a5e27a2edf80a531e63",
  "imageId": "sha256:7524f3f2bbd5690ba8dbce77f57d81b1242bbc898554cd3cd37f79bcf08ab9c2",
  "attemptId": "37820584534"
}
```

The registry digest identifies the single runnable manifest; `imageId` identifies its configuration and is not a pull reference. Keep the policy in your application's reviewed configuration.

`attemptId` records the originating workflow run for manual inspection. The verifier requires a positive numeric string but does not compare it with authenticated evidence. `releaseIdentityVerified: true` therefore does not authenticate this run ID; changing only `attemptId` can still pass verification.

With GitHub CLI authenticated for the public repository, download all assets from the selected immutable evidence release into a new directory. The Bash commands below use Python 3.12+ as `python3`:

```bash
set -eu
VERSION="$(python3 -c 'import json; print(json.load(open("graphviz-policy.json"))["version"])')"
EVIDENCE="graphviz-evidence-$VERSION"
mkdir "$EVIDENCE"
gh release download "image-graphviz-v$VERSION" \
  --repo sokolaidev/maf-extensions --dir "$EVIDENCE"
python3 scripts/verify_container_release.py \
  --policy graphviz-policy.json --evidence "$EVIDENCE" --bundles
```

Require successful verification and `releaseIdentityVerified: true`. This verifies provenance, the SPDX inventory, signed release completion and indexed evidence against the selected source and digest. It does not execute the Graphviz image. Missing completion evidence is a refusal, even when provenance and SBOM signatures are valid.

The verifier reports current monitoring separately. Identity success does not mean the latest scan is clean: inspect monitoring status and assessment age, and apply your application's vulnerability policy. Stale, unavailable, failing or retired monitoring is not a clean result. The [release contract](../../docs/security/container-image-releases.md) explains verification and monitoring in detail.

## Render a PNG with Docker

After verification, derive the pull reference from the same policy. This example is for Bash on a Linux Docker host, with a non-root host user and a local daemon. Docker Desktop users need file sharing enabled for the working directory.

```bash
set -eu
IMAGE="$(python3 -c 'import json; p=json.load(open("graphviz-policy.json")); print("ghcr.io/sokolaidev/maf-extensions/graphviz@" + p["registryDigest"])')"
docker pull --platform linux/amd64 "$IMAGE"
mkdir -p diagram-input diagram-output
printf 'digraph { consumer -> renderer -> png }\n' > diagram-input/diagram.dot
docker run --rm --platform linux/amd64 \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --env XDG_CACHE_HOME=/tmp/cache \
  --pids-limit 256 --memory 1g --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount "type=bind,src=$(pwd)/diagram-input,dst=/input,readonly" \
  --mount "type=bind,src=$(pwd)/diagram-output,dst=/output" \
  --entrypoint dot "$IMAGE" \
  -Tpng /input/diagram.dot -o /output/diagram.png
test -s diagram-output/diagram.png
```

The result is `diagram-output/diagram.png` on the host. On 2026-10-08, this exact Bash example passed for the pinned 0.1.0 digest through WSL with Docker Desktop 29.8.2 in Linux-container mode and UID/GID 1000:1000; the output was a valid 171 by 251 PNG. This checks the documented CLI path, not the separate agent sample or a production deployment. The output directory must be writable by the selected UID/GID; the input directory is mounted read-only. Mount only the files needed for this render. The image does not enforce networking, filesystem isolation, resource limits or a non-root user by itself: those controls come from the invocation or sandbox backend. Memory, CPU and process limits do not impose a wall-clock deadline; production callers must enforce a timeout and remove a timed-out container.

For the [Docker diagram sample](../../samples/07_docker_diagram/), set `DIAGRAM_SANDBOX_IMAGE` to the verified digest reference instead of its local-build tag and follow the sample's host/model prerequisites with the `docker` backend. Its tool writes under `/maf-sandbox/work`, requests closed egress and returns the rendered PNG through `FILES_OUT`. The image packaging probe does not establish compatibility with every SDK version or the sample's separate `docker-sbx` path; qualify your chosen combination.

## Build locally

From the repository root:

```bash
docker build --platform linux/amd64 -t graphviz-sandbox:local images/graphviz-sandbox
```

The sample can use this local tag. A local build has no suite release-completion attestation and is not interchangeable with a published digest. It may contain different package versions even when built from the same Dockerfile: the Wolfi base is pinned, but `apk upgrade` and `apk add` use a rolling package repository.

## Updates, support and security

Pin the verified manifest digest in application configuration. To update, select and verify a new completed image release, review its SBOM and vulnerability reports, then test your diagrams and application integration before changing the pin. Graphviz and font updates can change layout and pixels; neither rebuilding from source nor upgrading preserves byte-identical output.

The release gate requires an offline PNG packaging probe, an SPDX SBOM, and no High/Critical findings, including unfixed findings, at publication assessment time. Review the retained reports for lower-severity findings and current monitoring for newly disclosed vulnerabilities. A scanner result or signature does not prove absence of malware or backdoors, and a packaging probe does not establish production suitability for your workload.

See the [security policy](../../SECURITY.md) for support and private vulnerability reporting. Use [repository issues](https://github.com/sokolaidev/maf-extensions/issues) for non-sensitive usage problems; include the image digest, platform, Docker version, relevant SDK versions and a minimal non-sensitive DOT example.

## SDK qualification

The [manual SDK qualification](../../samples/07_docker_diagram/README.md#qualify-the-published-image-through-the-sdk) verifies the signed release identity before exercising sample 07's rendering tool through the published router and Docker backend. It checks PNG delivery, invalid input, closed networking, a deterministic SDK timeout and container disposal, with exact image and package versions in its report. It runs on demand, without a model or Azure credentials, and does not run during ordinary PR CI. Its SDK defaults differ from the hardened Docker CLI example above; consult the observed settings and qualification limits before adopting them.
