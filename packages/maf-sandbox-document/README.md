# maf-sandbox-document

Read PDF, DOCX, PPTX and RTF files through a fixed converter in a disposable sandbox. The model chooses a listed file, output format and optional page range; it supplies no converter code. Parsers and archive/XML guards run in the guest.

This package is experimental and not yet published. Its first release requires the core release containing binary file-store reads; release core before this package. The image must also have a completed, currently eligible `document` release before the kind publishes.

## Attach the tool

```python
from maf_sandbox_document import make_document_tools

[document_read] = make_document_tools(
    router,
    file_store,
    "document-reader",
    context,
    sink,
    image=verified_document_image,
    formats=("text", "markdown"),  # Enable images only for a host that can consume them.
)
```

The host supplies a store with `async read(name) -> bytes | None`, and `context.list_files(store)` returns authorized `ListedFile` entries. Text-only AgentFileStore implementations need a binary adapter. Names are matched exactly, never interpreted as host paths, guest paths or URLs. The host must bound ingress bytes before the store returns them; the kind independently checks its 16 MiB input limit.

Use `file_store_provenance` for stores that may change during a call and `requires_file_integrity` when admission requires an established source label. Completion and the `extracted`/`refused` verdict are trusted. Document text, metadata, images, delivery references and converter diagnostics are guest-derived output. Core derives their effective label from the file read, model arguments and conversation.

The router must allow at least container isolation and supply POSIX `EXEC`, `FILES_IN` and `FILES_OUT` with closed egress. Every call requires its own sandbox and disposal, including refusal, failure and cancellation.

## Tool contract

`document_read(file, format="text", pages=None)` accepts `text`, `markdown` or `images`. `pages` is a positive, 1-based inclusive range such as `"2-5"` or `"3"`. PDF ranges select pages; PPTX ranges select slides. Both endpoints clamp to the available last page. DOCX has no reliable pagination without a layout engine, so an explicit range is refused. RTF ignores ranges.

| Input | Text / Markdown | Images | Limits |
|---|---|---|---|
| PDF | PDFium text in reading order; Markdown retains that text | PNG pages at the host's fixed DPI | Supported PDF subset below; no OCR |
| DOCX | Mammoth text or Markdown conversion | Refused | No pagination or faithful layout |
| PPTX | Slide text, table cells and notes; Markdown slide headings | Refused | No office renderer |
| RTF (`application/rtf`, `text/rtf`) | striprtf text, also usable as plain Markdown | Refused | Page range ignored |

Outputs are `document.txt` (`text/plain`), `document.md` (`text/markdown`) or `page-N.png` (`image/png`), plus `document.json` (`application/json`). Image names preserve original page numbers. An extracted verdict requires delivery of every requested output. Guard refusals deliver only the metadata and diagnostic. Invalid arguments, missing files, acquisition failures, timeouts, operational exits and incomplete delivery return `completed=False` with no verdict.

Metadata schema version 1 contains `media_type`, `format`, `supported_formats`, `pages` (a page/slide count, or null), `selected_pages`, `characters`, `truncated` (always false), `range_clamped`, `verdict`, `reason` and `files`. A refusal never represents capped content as a successful extraction.

## Host options and guards

| Option | Default | Allowed |
|---|---|---|
| `exec_timeout_seconds` | 60 | Finite number greater than 0, at most 300 |
| `max_pages` | 200 | 1–10,000 |
| `max_chars` | 1,000,000 | 1–8,000,000 |
| `image_dpi` | 96 | 1–600 |
| `max_image_pixels` | 16,000,000 | 1–32,000,000 per page |
| `max_image_pages` | 20 | 1–100 per call |
| `formats` | text, markdown, images | Nonempty tuple of distinct enabled formats |

OOXML is checked before Mammoth or python-pptx opens it: at most 2,048 ZIP entries, 16 MiB per expanded member, 64 MiB total, and a declared expansion ratio at most 200. Absolute/traversing/duplicate names, symlinks, encryption, unsupported compression and nested archives are refused. Bounded reads verify actual lengths and CRCs; converters receive a rebuilt, stored-only ZIP. XML preflight refuses DTDs and entity declarations, including UTF-16 declarations and XML in nonstandard member names. python-pptx uses a parser with entity resolution, DTD loading and network access disabled. Mammoth's pinned minidom path receives only the preflighted XML; external file access and embedded style maps are disabled.

PDF preflight admits a single classic cross-reference table, bounded objects and streams with no compression, Flate, ASCII85 or ASCIIHex. Each decoded stream is at most 32 MiB, with 64 MiB total. Object/cross-reference streams, incremental or encrypted PDFs, external streams, inline images, other filters (including JPEG/JPEG2000) and decoding parameters are refused. This deliberately restricts compatibility: PDFium has no public per-stream allocation limit, so unsupported structures are not passed to it. A host can prepare an accepted PDF before ingress; this kind never silently falls back to an unbounded parser. Text is capped at 100,000 characters per PDF page or PPTX slide as well as the configured total.

Outputs are capped at 32 MiB per file and 64 MiB total including metadata, independently of backend transfer limits. The execution deadline covers preflight and conversion together. Backend resource limits remain necessary for native parser allocations, CPU and temporary storage; these guards do not establish immunity to parser exploits.

See the [document contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/document.md) and [image guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/document-sandbox/README.md) for verification and release evidence.
