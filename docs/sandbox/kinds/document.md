# Documents

The `document` kind reads one caller-listed PDF, DOCX, PPTX or RTF through a fixed guest converter. It requires POSIX execution, file input/output, closed egress, at least container isolation, and a disposable sandbox for every call. The host owns file authorization, input labels, image selection, limits and output delivery.

The [package contract](../../../packages/maf-sandbox-document/README.md) specifies formats, page-range behavior, metadata, bounds and the conservative PDF subset. Text extraction does not perform OCR or promise visual reading order. DOCX/PPTX image rendering and DOCX pagination require an office layout engine and are unsupported.

## Input and integrity

The host's store returns bytes, and the caller context lists authorized `ListedFile` entries. Core's session read preserves those bytes as binary content while applying the same provenance fold and admission check as text. The file name selects one exact listing entry; it never becomes a command or guest path. The guest rechecks the selected document type against its contents.

Completion and the declared verdict are trusted. Extracted text, metadata, images and diagnostics stay in `SandboxResult.output`; core may promote them only when the call's source and conversation labels establish trust. A successful output sink must deliver every requested file before the tool reports `extracted`. Guard refusals produce metadata and a guest-derived diagnostic. Operational failures and timeouts carry no verdict.

## Markdown export

The host-facing `export_pdf` API accepts Markdown, passive SVG assets and `ExportOptions`, returning bounded PDF bytes after disposal. It requires an explicit image from the separate [document-export profile](../../../images/document-export/README.md); the reader image remains independent. Both use the `document` kind with call isolation and closed egress. Raw HTML is escaped, external resources are refused, and invalid Mermaid falls back to its source with a visible note. The package contract defines bounds and exceptions. Hosts own routing identity and any storage or streaming of the returned untrusted bytes.

The [export qualification](../../../scripts/document_export_qualification.py) checks the public API against an exact Docker image with A4/Letter fixtures, embedded fonts, extracted text, Mermaid/SVG rendering, request evidence and disposal. Its Linux image and signed release evidence remain distinct from native browser checks.

Hosts can import `validate_svg` and `is_valid_asset_name` from `maf_sandbox_document` to preflight individual assets before export. They share the export's passive-SVG and naming rules; `validate_svg` returns `None` or raises `ValueError` with the same refusal messages, and `is_valid_asset_name` returns a boolean including the 256-character limit. Aggregate export limits still apply. See the package contract for an example that omits rejected assets; select `ExportOptions(unsupplied_images="placeholder")` to render their image references as placeholders, or rewrite the references before using the default refusal mode.

`ExportOptions.unsupplied_images` defaults to `"refuse"`. With `"placeholder"`, image tokens that do not name supplied SVG assets become escaped `[Image: alt text]` placeholders without loading their source. The parser's tokens determine replacements, so code spans, reference images and table cells follow the same grammar as rendering. Supplied assets retain all validation. `export_pdf_result` returns an `ExportResult` containing PDF bytes and the guest-reported `images_replaced` count after disposal; `export_pdf` keeps its bytes return type. Placeholder mode requires a renderer that reports the count.

## Verification

Deterministic tests execute the fixed guest program against real small documents and crafted archive, XML, stream, page, character and image-limit failures. The [image checker](../../../scripts/document_qualification.py) exercises the same kind through Docker or an explicitly configured ACAS group, recording output hashes, per-call instances and cleanup. [Image instructions](../../../images/document-sandbox/README.md) explain the opt-in requirements. Backend conformance, image publication and production acceptance are separate evidence.

## Status

| Decision | State | Tracking |
|---|---|---|
| Fixed document kind, binary store reads and guarded image profile | Implemented; ACAS image compatibility tracked below | [#2206](https://github.com/sokolaidev/maf-extensions/issues/2206) (closed) by [#2215](https://github.com/sokolaidev/maf-extensions/pull/2215) (merged) |
| Document image ACAS layer compatibility | Device-free recipe and release layer guard implemented; local layer inspection and twelve Docker cases passed; replacement image publication and live ACAS import/execution pending | [#2377](https://github.com/sokolaidev/maf-extensions/issues/2377) (closed) by [#2378](https://github.com/sokolaidev/maf-extensions/pull/2378) (merged) |
| Markdown to PDF export on a separate image profile | API, renderer and qualification harness implemented; native browser checks and [Linux Docker qualification at `3c5d86ae`](https://github.com/sokolaidev/maf-extensions/actions/runs/42325489637) passed; image publication and production acceptance pending | [#2527](https://github.com/sokolaidev/maf-extensions/issues/2527) (closed) by [#2528](https://github.com/sokolaidev/maf-extensions/pull/2528) (merged) |
| Optional unsupplied-image placeholders and replacement counts | Implemented; native browser and local Linux Docker qualification passed; updated image publication and published-image qualification pending | [#2604](https://github.com/sokolaidev/maf-extensions/issues/2604) (closed) by [#2611](https://github.com/sokolaidev/maf-extensions/pull/2611) (merged) |
