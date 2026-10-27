# Document image

The `document` profile supplies Python 3.13, pypdfium2, Mammoth, python-pptx and striprtf for Linux/amd64. It has no office suite, OCR engine or SDK. The kind stages its fixed Python programs per call. See the [kind contract](../../packages/maf-sandbox-document/README.md) for supported PDF structures, output formats and guards.

The Dockerfile uses a digest-pinned Wolfi base. Refresh that digest for OS security updates: the base pins installed APK versions, so a normal `apk upgrade` can retain vulnerable versions. Direct and transitive Python dependencies are pinned with SHA-256 hashes in [requirements.txt](requirements.txt); pip admits wheels only and is removed after installation. Dependabot watches this directory. After changing [requirements.in](requirements.in), regenerate the lock with `uv pip compile images/document-sandbox/requirements.in --generate-hashes --no-header --no-annotate --python-version 3.13 --python-platform x86_64-manylinux_2_28 --output-file images/document-sandbox/requirements.txt`. Review dependency versions and rerun the parser security fixtures.

## Build and qualify

```bash
docker build --platform linux/amd64 -t maf-document:local images/document-sandbox
uv run python scripts/document_qualification.py --image maf-document:local --output out/document
```

The checker resolves Docker's immutable image ID, verifies closed networking and distinct containers per call, exercises all four formats, checks PNG dimensions and refusal results, and records output hashes and disposal. It uses the checkout SDK. It does not establish image publication, ACAS compatibility or production qualification.

ACAS conformance uses an already configured sandbox group and imported image. Set the existing `ACAS_SANDBOX_ENDPOINT`, `ACAS_SANDBOX_SUBSCRIPTION_ID`, `ACAS_SANDBOX_RESOURCE_GROUP`, `ACAS_SANDBOX_GROUP` and, when needed, `ACAS_SANDBOX_REGISTRY` variables, then run:

```bash
uv run python scripts/document_qualification.py --backend acas --image DOCUMENT_IMAGE --output out/document-acas
```

This creates and disposes twelve billable call sandboxes; it provisions no group or registry. The opt-in pytest equivalents use `MAF_DOCUMENT_DOCKER_IMAGE` or `MAF_DOCUMENT_ACAS_IMAGE` with `tests/test_document_live.py`.

## Publication and trust

Select a completed `image-document-v<version>` release in `ghcr.io/sokolaidev/maf-extensions/document`. The existing image release workflow builds this profile, qualifies it through the SDK, scans its SBOM, and retains provenance and completion evidence. Follow the [consumer verification procedure](../../docs/security/container-image-releases.md#evidence-and-verification), then pin the verified registry manifest digest. A local build or merged PR is not a published release.

Python wheels retain their own distribution license files in the image. PDFium and pypdfium2 include their upstream notices; Mammoth, python-pptx, striprtf, Pillow, lxml and the other transitive packages retain their respective licenses. Consult the release SPDX inventory. The repository's MIT license does not replace upstream terms.
