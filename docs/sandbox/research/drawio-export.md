# Offline Draw.io export

> Decision record for [#1654](https://github.com/sokolaidev/maf-extensions/issues/1654) and container-free execution in [#1656](https://github.com/sokolaidev/maf-extensions/issues/1656). The supported API and resource profile live in the [kind guide](../kinds/drawio.md) and package README.

## Decisions

Use Draw.io Desktop's native exporter for PNG, JPG and SVG. Render the validated, laid-out document rather than reconstructing its appearance in a different graphics library. Keep editable-only creation as the default. Export options, runtime selection and output destinations belong to the host.

Bundle the renderer, stencils, cloud icons and fonts at build time. Runtime egress stays closed, updates are disabled and each export has a fresh profile. Only known bundled image paths and validated embedded images are accepted. The first profile refuses math, links, background resources, external fonts and resource-bearing HTML. Unsupported input is a refusal; a missing runtime or failed renderer is incomplete execution.

Pin Desktop 31.7.0 and verify its distribution hash. The installer adds checks for failed image loading, unknown shapes and excessive export dimensions because upstream can otherwise complete with missing visual content. Retain the modified source scripts and upstream notices. Asset and renderer hashes describe installed content; they do not replace approval of the image distribution.

Embed local images and fonts in SVG. Rich HTML labels use foreignObject and need a compatible browser viewer. The fixed DejaVu font profile deliberately substitutes common Arial/Helvetica, Times and Courier family requests; it does not promise identical typography to an arbitrary editor installation or glyph coverage for every language.

Validate every requested page and output before calling the sink. Deterministic names are diagram-PAGE.png, diagram-PAGE.jpg and diagram-PAGE.svg. Retain diagram.drawio. The sink is not transactional: if delivery fails after an earlier output lands, report incomplete delivery rather than claiming rollback.

## Backends without container engines

The runtime installer and manifest are independent of the Dockerfile. A full-OS POSIX backend can install the same files at /opt/drawio and /opt/maf-drawio and provide Python, Graphviz, Pillow, Xvfb and Electron's libraries. This is a packaging interface, not a qualified new backend. The kind requires EXEC, FILES_IN, FILES_OUT, a POSIX guest and enforced closed egress.

Linux Bubblewrap is the first implemented container-engine-free target: read-only runtime files, private bounded tmpfs, mandatory isolated namespaces, denied networking and supervised descendants. A delegated cgroup v2 subtree limits aggregate memory, swap, processes and CPU before the guest starts. It requires no OCI image or daemon, but does use kernel namespaces; this is the protocol's `CONTAINER` boundary. Acquisition probes prerequisites and refuses unsupported confinement. An ordinary host subprocess is not a fallback. Windows process isolation and macOS Seatbelt need separate execution, policy and Electron qualification.

Keep CodeAct on Hyperlight where selected and route Draw.io through a dedicated router or explicit PER_SPEC selection. The host's isolation floor remains authoritative; no automatic downgrade from MICROVM to container or process isolation. Direct Hyperlight cannot execute this Electron workload, and Node/subprocess availability in MXC Hyperlight does not establish Chromium compatibility.

## Evidence and remaining work

The implementation provides the opt-in export API, image and native-directory provisioning, runtime validation and real Docker/Bubblewrap verification scripts. Runtime qualification is recorded in the delivering pull requests; neither source inspection nor unit fakes prove offline native rendering. ACAS remains separate acceptance work. The existing lightweight Python/Graphviz image retains its editable-only purpose.

On 2026-10-03, the initial local Docker candidate image ID `sha256:e73c3ed0c33749973ecb13384d37284ffa6d236b35f71e9673356d899d1a0c02` passed the verification script with networking disabled. This is historical evidence for that build, not the final review candidate. Two pages produced PNG/JPG rasters of 204 by 224 and 404 by 224 pixels and self-contained SVGs. The fixture includes a bundled Azure icon, connector, bold HTML label and accented/Greek text. A separate browser displayed the exported SVG, including its image and font content. This is a representative visual check, not an exhaustive viewer or glyph-coverage guarantee.

The same candidate passed stored-reference export of page 2 at scale 2, transparent PNG, opaque JPG at quality 75, explicit refusal of remote images, missing assets, unknown shapes and excessive dimensions, and incomplete timeout handling without artifact delivery. Scope disposal succeeded and no candidate containers remained. ACAS, cancellation/owner-death fault injection and container-free rendering were not exercised by this run.

The subsequent review candidate `sha256:1e43c168e2cac42b9808f037148cca520b8f2da99124625d5e4f67541558d5f5` passed the expanded verifier on the same date. It added visible indicator images/shapes and font preflight for every format; eight negative cases covered remote/missing images, unknown main/indicator shapes, absolute/relative indicator-image paths, oversized embedded PNG and excessive dimensions. PNG/JPG/SVG, stored-reference export and incomplete timeout handling passed, with successful disposal and no candidate containers remaining. PNG was visually inspected; the separate-browser SVG evidence above belongs to the initial candidate.

The final review candidate `sha256:5cd1e9ffec77dad26037b99054ac3891bcf6bb183d3c38d0a6ad404fe5d1e122` passed the verifier on the same date with all preceding checks and two additional refusals for UTF-16/UTF-32 embedded SVG declarations. Both pages exported in PNG/JPG/SVG with literal `fontFamily=Missing;` label text preserved; PNG was visually inspected. Stored-reference export, incomplete timeout handling and disposal passed, and no candidate containers remained. This run did not repeat the initial candidate's separate-browser SVG check or qualify another backend.

### Native Linux candidate

The container-free candidate was provisioned directly with debootstrap on 2026-10-03: Debian bookworm amd64, Desktop 31.7.0, Bubblewrap 0.9.0 on Ubuntu 24.04 with Linux `6.18.40.1-microsoft-standard-WSL2`. Runtime manifest SHA-256: `0c87be8da0a42148402afaf3c7652dbbe439c6d3a5f82e2f0a167f70e01fa951`. The native verifier passed the same two-page PNG/JPG/SVG, stored-reference, scale/transparency, resource-refusal and timeout cases. No Docker daemon or OCI image participated in provisioning or execution.

Real Linux tests exercise the core storage-base, file-input, file-output and execution conformance suites; namespace and network denial; private broker descriptors; read-only runtime; detached descendant cleanup; cancellation; owner death; per-call separation; competing owners; stale-instance disposal; and cleanup recovery. Resource probes read back the configured CPU/memory/swap/PID limits and exhaust the private tmpfs, PID and memory limits. CPU throttling latency, every Linux distribution, native Windows/macOS, seccomp hardening and ACAS rendering are not established by this candidate. The opt-in live suite and exporter verifier must be rerun for a deployed runtime and host policy.

The final native review candidate was freshly provisioned with debootstrap on the same date, incorporating the exporter fixes from #1666. Runtime manifest SHA-256: `6b286f10fbf8830c2d32842dc5bf702b7cb712218b3bfcbc83c9592d24ffdaec`; exporter script SHA-256: `b01a1598f3683bf2d421a18213e7574af9a81e29a2d0d2b6ca075b31c0660d51`. The Bubblewrap verifier passed two-page PNG/JPG/SVG with indicator assets and literal font-like label text, stored-reference settings, all ten resource refusals and incomplete timeout handling. No container engine or OCI image participated. The backend suite passed 39 tests on the same Linux host, including abrupt process exit and ordinary setup failure during record writing, after publication, after cgroup creation and after limit setup; subsequent acquisition recovered successfully. Over-limit timeouts were refused before execution, while accepted timeout expiry destroyed the sandbox. These checks establish process-death recovery, not recovery from a host power loss.

## Sources

- [Pinned Desktop distribution](https://github.com/jgraph/drawio-desktop/releases/tag/v31.7.0)
- [Pinned Draw.io exporter](https://github.com/jgraph/drawio/blob/v31.7.0/src/main/webapp/js/export.js)
- [SVG compatibility](https://www.drawio.com/doc/faq/export-to-svg)
- [Bubblewrap](https://github.com/containers/bubblewrap)
- [MXC Hyperlight runtime](https://github.com/microsoft/mxc/blob/v0.9.0/docs/hyperlight/hyperlight-backend.md)
