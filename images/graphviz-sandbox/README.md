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

**Availability:** [Graphviz 0.1.1](https://github.com/sokolaidev/maf-extensions/releases/tag/image-graphviz-v0.1.1) is publicly available for Linux/amd64. Its immutable evidence includes provenance, an SPDX SBOM, signed completion and anonymous-pull qualification. Pin `ghcr.io/sokolaidev/maf-extensions/graphviz@sha256:b44a268e61780d3c9020dbe6cb0cf791903553e4e23061dc0a36fafe6c2f113e`; check the status report for current monitoring before adopting it.

## Verify before pulling and running

Use a reviewed checkout of this repository for the verifier. From its root, create `graphviz-policy.json`. The policy below selects release 0.1.1 and pins its image digests and [source `c36ad8c2`](https://github.com/sokolaidev/maf-extensions/tree/c36ad8c29b3fd44d7bb14bac49dbda475c6780f2). Inspect [publication run 37918950417](https://github.com/sokolaidev/maf-extensions/actions/runs/37918950417) and review that source and workflow before accepting this identity; for another release, select its identity independently rather than copying the downloaded candidate's claims.

```json
{
  "profile": "graphviz",
  "version": "0.1.1",
  "sourceCommit": "c36ad8c29b3fd44d7bb14bac49dbda475c6780f2",
  "sourceRef": "refs/heads/main",
  "registryDigest": "sha256:b44a268e61780d3c9020dbe6cb0cf791903553e4e23061dc0a36fafe6c2f113e",
  "assessedManifestDigest": "sha256:b44a268e61780d3c9020dbe6cb0cf791903553e4e23061dc0a36fafe6c2f113e",
  "imageId": "sha256:e31e0db3787d5e3f61d09f36a4df2f1db5d4498f52abade7e232afe31d378dc5",
  "attemptId": "37918950417"
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

The result is `diagram-output/diagram.png` on the host. On 2026-10-08, this exact Bash example passed for the then-selected 0.1.0 digest through WSL with Docker Desktop 29.8.2 in Linux-container mode and UID/GID 1000:1000; the output was a valid 171 by 251 PNG. That recorded result applies to 0.1.0; the CLI example has not yet been rerun for 0.1.1. It does not qualify the separate agent sample or a production deployment. The output directory must be writable by the selected UID/GID; the input directory is mounted read-only. Mount only the files needed for this render. The image does not enforce networking, filesystem isolation, resource limits or a non-root user by itself: those controls come from the invocation or sandbox backend. Memory, CPU and process limits do not impose a wall-clock deadline; production callers must enforce a timeout and remove a timed-out container.

The manual [consumer qualification](../../samples/07_docker_diagram/README.md#qualify-the-published-image-through-the-sdk) also exercises these CLI controls with `--hardened-cli`. It splits `docker run` into `create` and `start --attach` so it can inspect the short-lived renderer before execution, uses a unique cleanup label, and enforces a 30-second execution deadline. It validates the PNG and requires automatic removal before fallback cleanup; a timed-out render or leftover container fails even when cleanup succeeds. This check requires a non-root user on a Linux host with a local daemon and retains `cli-output/diagram.png` and the `hardenedCli` section of `qualification.json`. It adds no image execution to ordinary PR CI.

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

**Graphviz 0.1.1 status:** the publication workflow passed anonymous pull and offline PNG packaging checks, and independent consumer verification passed its signed release identity. [Hosted SDK qualification on 2026-10-10](https://github.com/sokolaidev/maf-extensions/actions/runs/38001025079) passed PNG delivery, invalid-DOT rejection, closed networking, the SDK timeout and container disposal for the selected 0.1.1 digest. [Hosted SDK and hardened CLI qualification](https://github.com/sokolaidev/maf-extensions/actions/runs/38041599180) subsequently passed both paths for 0.1.1, including CLI rendering as a non-root user with the documented controls and automatic container removal without fallback cleanup. The [measured record](../../samples/07_docker_diagram/README.md#qualify-the-published-image-through-the-sdk) includes PNG hashes, observed controls, timings and retained evidence. The earlier SDK result below applies only to 0.1.0.

The [manual SDK qualification](../../samples/07_docker_diagram/README.md#qualify-the-published-image-through-the-sdk) verifies the signed release identity before exercising sample 07's rendering tool through the published router and Docker backend. It checks PNG delivery, invalid input, closed networking, a deterministic SDK timeout and container disposal, with exact image and package versions in its report. It runs on demand, without a model or Azure credentials, and does not run during ordinary PR CI. Its SDK defaults differ from the hardened Docker CLI example above; consult the observed settings and qualification limits before adopting them.

[Hosted qualification on 2026-10-09](https://github.com/sokolaidev/maf-extensions/actions/runs/37909959442) passed for the published 0.1.0 digest with `maf-sandbox==0.48.0`, `maf-sandbox-docker==0.27.0` and `agent-framework-core==1.20.0`, using Python 3.12.3 and Docker 28.0.4 on Linux/amd64. The [sample evidence](../../samples/07_docker_diagram/README.md#qualify-the-published-image-through-the-sdk) records the checks, output hash, cleanup result and artifact retention. This result does not carry over to a replacement digest.
