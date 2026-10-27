# Documents

The `document` kind reads one caller-listed PDF, DOCX, PPTX or RTF through a fixed guest converter. It requires POSIX execution, file input/output, closed egress, at least container isolation, and a disposable sandbox for every call. The host owns file authorization, input labels, image selection, limits and output delivery.

The [package contract](../../../packages/maf-sandbox-document/README.md) specifies formats, page-range behavior, metadata, bounds and the conservative PDF subset. Text extraction does not perform OCR or promise visual reading order. DOCX/PPTX image rendering and DOCX pagination require an office layout engine and are unsupported.

## Input and integrity

The host's store returns bytes, and the caller context lists authorized `ListedFile` entries. Core's session read preserves those bytes as binary content while applying the same provenance fold and admission check as text. The file name selects one exact listing entry; it never becomes a command or guest path. The guest rechecks the selected document type against its contents.

Completion and the declared verdict are trusted. Extracted text, metadata, images and diagnostics stay in `SandboxResult.output`; core may promote them only when the call's source and conversation labels establish trust. A successful output sink must deliver every requested file before the tool reports `extracted`. Guard refusals produce metadata and a guest-derived diagnostic. Operational failures and timeouts carry no verdict.

## Verification

Deterministic tests execute the fixed guest program against real small documents and crafted archive, XML, stream, page, character and image-limit failures. The [image checker](../../../scripts/document_qualification.py) exercises the same kind through Docker or an explicitly configured ACAS group, recording output hashes, per-call instances and cleanup. [Image instructions](../../../images/document-sandbox/README.md) explain the opt-in requirements. Backend conformance, image publication and production acceptance are separate evidence.

## Status

| Decision | State | Tracking |
|---|---|---|
| Fixed document kind, binary store reads and guarded image profile | Implemented; publication and ACAS qualification pending | [#2206](https://github.com/sokolaidev/maf-extensions/issues/2206) (open) |
