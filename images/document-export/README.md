# Document export runtime

This separate `document-export` profile converts Markdown to PDF through `maf_sandbox_document.export_pdf`. It contains headless Chromium, CommonMark with GFM tables and strikethrough, a SHA-512-pinned Mermaid bundle, DejaVu Sans/Mono and Noto emoji fonts. The existing `document` reader image does not contain this renderer.

Build and qualify Linux/amd64 from the repository root:

```sh
uv run --locked python scripts/build_wolfi_python.py --platform linux/amd64 -t maf-document-export:local images/document-export
uv run python scripts/document_export_qualification.py --image maf-document-export:local --output out/document-export
```

The qualification uses the public host API and checks A4 and Letter page dimensions and pagination, extracted headings/table/code/diagram/SVG text, embedded text and monospace fonts, Mermaid fallback, page-limit and remote-image refusals, closed container networking, fresh instances and disposal. It retains fixture PDFs, measured page dimensions and renderer request evidence. `MAF_DOCUMENT_EXPORT_IMAGE` enables the same checks in pytest. Local native browser checks alone do not qualify the Linux image or sandbox boundary; ACAS and production qualification are separate.

The built-in layout has a title/date block, print typography, wrapping code, fixed-width wrapping tables, repeated table headers, rows kept together where they fit on a page, headings kept with following content, and `Page X of Y` footers. Oversized individual table rows cannot fit intact on one page and Chromium may split them. The layout is fixed in v1; standalone SVG/draw.io export and themes are not supported.

Raw HTML is escaped. Markdown images must name a supplied passive SVG; they become inline data images. SVG scripts, event attributes, embedded HTML, CSS and external references are refused. Mermaid configuration directives are disabled; invalid or unsupported diagrams show their original source and a visible note. The page uses a nonce-restricted script policy, denies connections and external resources, blocks service workers and intercepts requests. A forbidden resource attempt refuses the export. Chromium runs inside the disposable sandbox; the backend must enforce closed egress and at least container isolation. This is not a host browser or an arbitrary JavaScript execution API.

Expanded Markdown markup is capped at 32,000,000 UTF-8 bytes as token fragments are rendered, before repeated link URLs or titles can accumulate beyond the budget. Repeated SVG references count separately; an image that would exceed that budget is refused before its base64 copy is created. `MAF_DOCUMENT_EXPORT_BROWSER` enables native Chromium request-interception and CSP-denial tests using the supplied executable path; those checks do not qualify a container image.

The image uses the suite's digest-pinned Wolfi base and shared Python APK preparation. Python dependencies require hashes; `mermaid.json` pins the upstream archive's integrity. OS packages are resolved from Wolfi's signed repository at build time, as in the other profiles, so the retained image digest and SBOM identify the actual browser/fonts. Mermaid's package metadata and license are retained for inventory. Maintainers update the Mermaid version and integrity together and rerun qualification. The existing release pipeline supplies candidate identity, scans, SBOM, signatures, provenance and the release-completion record; adding this profile does not publish it. Publish and qualify its exact digest before releasing the document package that requires it.
