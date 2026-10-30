# Draw.io editable-document image

The `drawio` image supplies Python 3.13, Graphviz and DejaVu fonts for creating editable `.drawio` documents with `maf-sandbox-drawio`. It runs on Linux/amd64. The Python SDK stages its bounded converter into the guest at call time; the image does not contain the SDK, Draw.io Desktop or Electron. Native PNG/SVG/JPEG export belongs to the separate `drawio-export` image.

## Publication and verification

The first public release is tracked in [#1841](https://github.com/sokolaidev/maf-extensions/issues/1841). Use only a completed `image-drawio-v<version>` release with retained evidence; a successful build or merged PR does not establish publication. The target repository is `ghcr.io/sokolaidev/maf-extensions/drawio`.

Follow the [consumer verification procedure](../../docs/security/container-image-releases.md#evidence-and-verification) to select an expected release identity, download its retained evidence, and verify its provenance, SPDX and completion bundles before execution. Pin `ghcr.io/sokolaidev/maf-extensions/drawio@sha256:<verified-manifest-digest>` rather than relying on a mutable tag. The registry manifest digest and Docker configuration/image ID are different identifiers; the release records both.

The [public security report](https://sokolaidev.github.io/maf-extensions/) reports current monitoring separately from signed release identity. The release gate refuses all known High/Critical findings, including unfixed findings. Assessments expire after 48 hours; a clean assessment is dated evidence, not proof of no malware or unknown vulnerabilities. Superseded completed versions remain monitored for 90 days.

Create an application-owned `drawio-policy.json` containing `profile: "drawio"`, the approved `version`, `sourceCommit`, `sourceRef`, `registryDigest`, `assessedManifestDigest`, `imageId` and `attemptId`. Select expected identities independently of the downloaded candidate claims; `attemptId` is informational and is not authenticated by the consumer verifier. From a reviewed checkout:

```sh
set -eu
VERSION="$(python3 -c 'import json; print(json.load(open("drawio-policy.json"))["version"])')"
EVIDENCE="drawio-evidence-$VERSION"
mkdir "$EVIDENCE"
gh release download "image-drawio-v$VERSION" --repo sokolaidev/maf-extensions --dir "$EVIDENCE"
python3 scripts/verify_container_release.py --policy drawio-policy.json --evidence "$EVIDENCE" --bundles
```

Require `releaseIdentityVerified: true` before execution. Current monitoring remains a separate result for the application's policy.

## Use with the SDK

Supply the verified image reference to the Draw.io tool on a Docker backend. No model or cloud credentials are needed for editable conversion. This example assumes your application has constructed `router`, `context` and an output `sink` using the [Draw.io integration guide](../../docs/sandbox/kinds/drawio.md).

```python
from maf_sandbox_drawio import make_drawio_tools

[create_drawio] = make_drawio_tools(
    router,
    "diagram-author",
    context,
    sink,
    image="ghcr.io/sokolaidev/maf-extensions/drawio@sha256:<verified-manifest-digest>",
    preserve_layout=True,
)
```

Call the tool with editable, uncompressed `mxfile` XML. A successful call delivers `diagram.drawio` through the host output sink. With `preserve_layout=True`, complete supplied vertex geometry is preserved; missing geometry triggers Graphviz layout. With `preserve_layout=False`, Graphviz lays out the graph even when positions were supplied. Invalid XML is refused without delivering an artifact. The kind requests closed egress and file-in/file-out capabilities; the selected backend must provide them. Opening the resulting document in a desktop application is a separate consumer action.

For a local build and the existing four-case authoring check:

```sh
uv run --locked python scripts/build_wolfi_python.py --pull --platform linux/amd64 -t drawio:local images/drawio-sandbox
uv run --locked python scripts/check_drawio_docker.py --image drawio:local --output drawio-check
```

That local tag is for development. It is not a signed published release.

## What release qualification establishes

Candidate preparation and post-publication qualification run the checkout's `maf-sandbox`, Docker backend and Draw.io kind against the exact Docker image ID. The post-publication stage first verifies candidate attestations and pulls the approved manifest anonymously. `runtime.json` and `qualification.json` retain the SDK package versions and explicitly identify their source as the checkout, not an independent installation of published SDK wheels.

The checks create and retrieve editable output for all four combinations of layout preservation and supplied/missing geometry, verify node labels and edge relationships, reject invalid XML without output, observe each actual container's image identity and `network=none`, and refuse incomplete per-call or scope cleanup. Reports include output hashes, runtime versions, license hashes and Docker version. The existing High/Critical scan, SPDX inventory and signed completion gates remain required.

These checks qualify this bounded Docker consumer path. They do not establish ACAS, WSLC, Docker Sandboxes, interactive Draw.io UI, native image export, arbitrary document compatibility or application-specific production containment. Package APIs remain experimental. Use the versions recorded in the selected release's evidence when evaluating that pairing; later SDK versions require their own compatibility checks.

## Contents and notices

The pinned Wolfi base installs current packages from its configured repository at build time. Exact resolved versions and dependencies are recorded in each release's SBOM; a rebuild can therefore produce new bytes and must receive a new image version. No dependencies are installed when converting a document.

Python retains its license at `/usr/lib/python3.13/LICENSE.txt`, Graphviz at `/usr/share/doc/graphviz/COPYING`, and the copied [DejaVu 2.37 notice](https://github.com/dejavu-fonts/dejavu-fonts/blob/version_2_37/LICENSE) at `/usr/share/licenses/drawio/DEJAVU-LICENSE`. The fonts include the upstream Bitstream and Arev terms recorded in that notice. These components retain their own licenses; the repository's MIT license does not replace them. Consult the SPDX inventory for the remaining transitive components and their license metadata.

## Support

Report reproducible defects through [repository issues](https://github.com/sokolaidev/maf-extensions/issues), including the release version, manifest digest, SDK versions, backend and a minimal non-sensitive XML example. Follow [SECURITY.md](../../SECURITY.md) for private vulnerability reporting. Community maintenance does not provide a production-support SLA.
