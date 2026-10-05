# Offline Draw.io renderer

An opt-in Linux amd64 runtime for maf-sandbox-drawio image exports. The existing drawio-sandbox image remains sufficient for editable XML output.

```bash
docker build -t maf-drawio-export:local images/drawio-export
uv run python scripts/check_drawio_exports.py --image maf-drawio-export:local --output out/drawio-exports
```

The container uses a digest-pinned Wolfi base and signed APK runtime packages. The build verifies the Draw.io Desktop 31.7.0 Debian archive's SHA-256, extracts its application payload, installs native dependencies and a fixed DejaVu font set, and prepares /opt/maf-drawio/manifest.json. Network access is needed at build time. Deployment should pin the resulting approved image digest; Wolfi package updates are not a reproducible-build guarantee.

install.py repacks Desktop's ASAR with guard.js to refuse missing images, unknown shapes and excessive dimensions. export.py resolves local assets, validates embedded images and HTML, runs the native exporter and validates artifacts before publishing its manifest. The host never imports or executes this runtime directly. Preserve the upstream Desktop, Electron, Draw.io and font notices shipped in the image when redistributing it; the modified export behavior is defined by these source files.

Both image and indicatorImage styles require a manifest-listed asset or validated embedded image. Indicator shapes must belong to the native renderer's default-shape registry; stencil-only indicators are refused because Desktop does not construct them. Every variant of each selected font family is checked against the manifest before rendering PNG, JPG or SVG.

Every nonempty stencil lookup made while painting must resolve. This covers auxiliary selectors such as resIcon, prIcon, grIcon and bgIcon using each native shape's prefixes and primitive-background rules. Main-shape probes outside painting retain the native default-shape fallback.

XML resources, including embedded SVGs, must use UTF-8, optionally with its byte-order mark. NUL characters, DTD/entity declarations and processing instructions other than the XML declaration are refused before XML parsing.

SVG attributes, inline styles and stylesheets may reference local resources with `url(#id)`. Each reference must resolve to a unique linearGradient, radialGradient, pattern, clipPath, mask or filter in the same SVG. Every remaining URL, data URI, image-set() and src() reference is refused. Image attributes use the separate embedded-image validator. The exact namespaced RDF declaration of the Dublin Core StillImage type is accepted as non-fetching metadata.

Both provisioning paths run check-assets.py against every bundled azure2 SVG. Draw.io Desktop 31.7.0 has 704 icons: 703 pass, and Private_Endpoint.svg retains its existing font-policy refusal because its path styles declare fonts. The check permits only that named refusal; every other rejection fails provisioning.

Embedded SVGs, including bundled assets, must contain no text elements or font declarations in presentation attributes, inline styles or stylesheets. This includes shorthand font properties, @font-face and local() font sources. CSS font tokens are refused conservatively, including in comments and selectors. Convert image text to paths before embedding it. Native diagram labels continue to use the verified DejaVu font set.

SVG content must be static. SVG animation elements, motion paths, timed discard, CSS animation/keyframe/transition tokens and event handlers are refused in embedded resources and native SVG output. CSS animation tokens are refused conservatively even in comments and selectors. Embedded SVGs must not contain image, feImage or use elements.

Enabled label placeholders are refused before rendering, including object/UserObject wrappers and indirect labels, because substitution occurs after HTML validation. Literal labels with placeholders disabled remain supported. Invalid UTF-8 or JSON in the installed runtime manifest is an incomplete runtime failure, not a content refusal.

Prepared XML is limited to 8 MiB across all pages, including embedded assets, XML escaping and normalized font styles. Expansion is charged before replacement styles are retained or serialized. Each bundled asset is read and verified once per document; repeated references still consume the document budget.

The image runs as root so ACAS's root-owned file-plane directories remain writable. The export coordinator keeps that identity, but starts Xvfb and Electron as uid/gid 10001 with no supplementary groups. It gives the child separate scratch, profile and output directories, validates bounded regular output files without following links, then writes the artifacts into the call directory as root. Editable creation and Graphviz retain the image's root identity. An already unprivileged coordinator, including a native Bubblewrap guest, preserves its existing identity.

The root coordinator needs permission to change ownership, set uid/gid, read the renderer's private output directory and terminate its process group. Set `DockerSandboxConfig(cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"))`; drop-all is mandatory. Nonempty grants require call isolation and disposal, which the export spec already requests, and withhold backend deletion and reclamation. The shared `/maf-sandbox/work` stays root-owned, so editable-only creation, including Graphviz layout, works with no additions. A failed privilege drop stops the export. These grants cover the coordinator; Electron and its renderer run without effective, permitted, inheritable or ambient capabilities. The capability policy applies to every workload on that backend, so use a dedicated backend/router configuration when other workloads require zero capabilities.

Verify the two profiles against the rebuilt image:

```bash
uv run python scripts/check_drawio_docker.py --image maf-drawio-export:local --output out/drawio-editable
uv run python scripts/check_drawio_exports.py --image maf-drawio-export:local --output out/drawio-exports --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add SETUID --cap-add SETGID --cap-add KILL
```

Both checks require clean tool-call reclamation as well as final disposal. The export check observes the renderer's process capability sets.

Electron's inner sandbox remains disabled. A qualified outer sandbox must enforce closed networking, CPU/memory/process limits, file confinement and process-tree disposal. Do not expose host credentials, directories or display sockets. The verification script explicitly selects container isolation; the library does not lower the host's isolation floor. Before exercising the tools, it observes the live Electron and renderer processes through /proc, requires uid/gid 10001 and empty supplementary groups when the coordinator is root, and verifies output publication into a private call directory. Rebuild and import the resulting image before using it on ACAS; changing the host's image reference alone does not update an existing imported image.

## Without a container engine

On Linux amd64, install debootstrap and run `sudo bash images/drawio-export/build-runtime.sh /opt/maf-runtime` with a new destination directory. This provisions Debian bookworm, the verified Desktop package, native libraries, fonts and the same offline exporter directly. It does not build or extract an OCI image. Network access and root are needed for provisioning only. Retain the runtime manifest and approve the directory's distribution separately; Debian packages are not pinned to a snapshot, so this is not a reproducible root filesystem.

Configure the [Bubblewrap backend](../../packages/maf-sandbox-bubblewrap/README.md) with that read-only runtime and a delegated cgroup v2 subtree, then run `scripts/check_bubblewrap_exports.py`. The application runs outside the runtime as an unprivileged Linux user. Host startup probes namespaces, resource controls, the guest interpreter and `/bin/sh`; the export check qualifies the Electron dependencies and bundled resources. Missing prerequisites refuse rather than execute on the host.

The installer must receive the pinned, unmodified Desktop archive; do not run it twice against an already patched archive. Keep the upstream notices with the runtime. Windows, Seatbelt and Hyperlight native rendering remain separate work. See the [decision record](../../docs/sandbox/research/drawio-export.md).
