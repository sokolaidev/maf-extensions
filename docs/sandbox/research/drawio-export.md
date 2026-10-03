# Offline Draw.io export

> Decision record for [#1654](https://github.com/sokolaidev/maf-extensions/issues/1654) and container-free execution in [#1656](https://github.com/sokolaidev/maf-extensions/issues/1656). The supported API and resource profile live in the [kind guide](../kinds/drawio.md) and package README.

## Decisions

Use Draw.io Desktop's native exporter for PNG, JPG and SVG. Render the validated, laid-out document rather than reconstructing its appearance in a different graphics library. Keep editable-only creation as the default. Export options, runtime selection and output destinations belong to the host.

Bundle the renderer, stencils, cloud icons and fonts at build time. Runtime egress stays closed, updates are disabled and each export has a fresh profile. Only known bundled image paths and validated embedded images are accepted. The first profile refuses math, enabled label placeholders, links, background resources, external fonts and resource-bearing HTML. Unsupported input is a refusal; a missing runtime, corrupt runtime manifest or failed renderer is incomplete execution.

Pin Desktop 31.7.0 and verify its distribution hash. The installer adds checks for failed image loading, unknown shapes and excessive export dimensions because upstream can otherwise complete with missing visual content. Retain the modified source scripts and upstream notices. Asset and renderer hashes describe installed content; they do not replace approval of the image distribution.

Embed local images and fonts in SVG. Rich HTML labels use foreignObject and need a compatible browser viewer. The fixed DejaVu font profile deliberately substitutes common Arial/Helvetica, Times and Courier family requests; it does not promise identical typography to an arbitrary editor installation or glyph coverage for every language.

Validate every requested page and output before calling the sink. Deterministic names are diagram-PAGE.png, diagram-PAGE.jpg and diagram-PAGE.svg. Retain diagram.drawio. The sink is not transactional: if delivery fails after an earlier output lands, report incomplete delivery rather than claiming rollback.

## Backends without container engines

The runtime installer and manifest are independent of the Dockerfile. A full-OS POSIX backend can install the same files at /opt/drawio and /opt/maf-drawio and provide Python, Graphviz, Pillow, Xvfb and Electron's libraries. This is a packaging interface, not a qualified new backend. The kind requires EXEC, FILES_IN, FILES_OUT, a POSIX guest and enforced closed egress.

Linux Bubblewrap is the first proposed container-engine-free target: read-only runtime files, a private work directory, isolated namespaces, denied networking and supervised descendants. It requires no OCI image or daemon, but does use kernel namespaces. Acquisition must probe prerequisites and refuse unsupported confinement. An ordinary host subprocess is not a fallback. Windows process isolation and macOS Seatbelt need separate execution, policy and Electron qualification.

Keep CodeAct on Hyperlight where selected and route Draw.io through a dedicated router or explicit PER_SPEC selection. The host's isolation floor remains authoritative; no automatic downgrade from MICROVM to container or process isolation. Direct Hyperlight cannot execute this Electron workload, and Node/subprocess availability in MXC Hyperlight does not establish Chromium compatibility.

## Evidence and remaining work

The implementation provides the opt-in export API, an image build, runtime validation and a real Docker verification script. Runtime qualification is recorded in the delivering pull request; neither source inspection nor unit fakes prove offline native rendering. ACAS and container-free evidence remain separate acceptance work. The existing lightweight Python/Graphviz image retains its editable-only purpose.

On 2026-10-03, the initial local Docker candidate image ID `sha256:e73c3ed0c33749973ecb13384d37284ffa6d236b35f71e9673356d899d1a0c02` passed the verification script with networking disabled. This is historical evidence for that build, not the final review candidate. Two pages produced PNG/JPG rasters of 204 by 224 and 404 by 224 pixels and self-contained SVGs. The fixture includes a bundled Azure icon, connector, bold HTML label and accented/Greek text. A separate browser displayed the exported SVG, including its image and font content. This is a representative visual check, not an exhaustive viewer or glyph-coverage guarantee.

The same candidate passed stored-reference export of page 2 at scale 2, transparent PNG, opaque JPG at quality 75, explicit refusal of remote images, missing assets, unknown shapes and excessive dimensions, and incomplete timeout handling without artifact delivery. Scope disposal succeeded and no candidate containers remained. ACAS, cancellation/owner-death fault injection and container-free rendering were not exercised by this run.

The subsequent review candidate `sha256:1e43c168e2cac42b9808f037148cca520b8f2da99124625d5e4f67541558d5f5` passed the expanded verifier on the same date. It added visible indicator images/shapes and font preflight for every format; eight negative cases covered remote/missing images, unknown main/indicator shapes, absolute/relative indicator-image paths, oversized embedded PNG and excessive dimensions. PNG/JPG/SVG, stored-reference export and incomplete timeout handling passed, with successful disposal and no candidate containers remaining. PNG was visually inspected; the separate-browser SVG evidence above belongs to the initial candidate.

The XML/font review candidate `sha256:5cd1e9ffec77dad26037b99054ac3891bcf6bb183d3c38d0a6ad404fe5d1e122` passed the verifier on the same date with all preceding checks and two additional refusals for UTF-16/UTF-32 embedded SVG declarations. Both pages exported in PNG/JPG/SVG with literal `fontFamily=Missing;` label text preserved; PNG was visually inspected. Stored-reference export, incomplete timeout handling and disposal passed, and no candidate containers remained. This run did not repeat the initial candidate's separate-browser SVG check or qualify another backend.

The expansion-budget review candidate `sha256:cbc9c21ef845c5751dda4a3215d57cae56c4759f50147d26d127eb1508f28324` passed the verifier on the same date with all preceding checks and a cumulative expansion refusal. An eight-page source containing 256 references to an otherwise accepted bundled SVG was refused specifically by the 8 MiB prepared-document budget, without artifact delivery. Verified assets are cached per document, and each expanded style is charged before retention and final serialization. Both pages exported in PNG/JPG/SVG; PNG was visually inspected. All eleven resource refusals, stored-reference settings, timeout handling and disposal passed, with no candidate containers remaining. Separate-browser SVG and other-backend qualification were not repeated for this candidate.

The current review candidate `sha256:bf7c83dc026e1aad79ef93e854475bde15a994034d217d98966b5eb228e602c1` passed the verifier on the same date with all preceding checks and four additional refusals for enabled object/UserObject placeholders, both substitution and indirect labels. All fifteen resource refusals delivered no artifacts. Two-page PNG/JPG/SVG, stored-reference settings, timeout handling and disposal passed, with no candidate containers remaining; PNG was visually inspected. Portable CLI tests separately verified that invalid UTF-8/JSON runtime manifests return incomplete execution for all three formats before starting the native renderer. This candidate did not repeat separate-browser SVG or other-backend qualification.

## Sources

- [Pinned Desktop distribution](https://github.com/jgraph/drawio-desktop/releases/tag/v31.7.0)
- [Pinned Draw.io exporter](https://github.com/jgraph/drawio/blob/v31.7.0/src/main/webapp/js/export.js)
- [Pinned placeholder label handling](https://github.com/jgraph/drawio/blob/v31.7.0/src/main/webapp/js/grapheditor/Graph.js)
- [SVG compatibility](https://www.drawio.com/doc/faq/export-to-svg)
- [Bubblewrap](https://github.com/containers/bubblewrap)
- [MXC Hyperlight runtime](https://github.com/microsoft/mxc/blob/v0.9.0/docs/hyperlight/hyperlight-backend.md)
