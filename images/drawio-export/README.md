# Offline Draw.io renderer

An opt-in Linux amd64 runtime for maf-sandbox-drawio image exports. The existing drawio-sandbox image remains sufficient for editable XML output.

```bash
docker build -t maf-drawio-export:local images/drawio-export
uv run python scripts/check_drawio_exports.py --image maf-drawio-export:local --output out/drawio-exports
```

The build verifies the Draw.io Desktop 31.7.0 Debian distribution's SHA-256, installs native dependencies and a fixed DejaVu font set, and prepares /opt/maf-drawio/manifest.json. Network access is needed at build time. Deployment should pin the resulting approved image digest; Debian package updates are not a reproducible-build guarantee.

install.py repacks Desktop's ASAR with guard.js to refuse missing images, unknown shapes and excessive dimensions. export.py resolves local assets, validates embedded images and HTML, runs the native exporter and validates artifacts before publishing its manifest. The host never imports or executes this runtime directly. Preserve the upstream Desktop, Electron, Draw.io and font notices shipped in the image when redistributing it; the modified export behavior is defined by these source files.

Both image and indicatorImage styles require a manifest-listed asset or validated embedded image. Indicator shapes must belong to the native renderer's default-shape registry; stencil-only indicators are refused because Desktop does not construct them. Every variant of each selected font family is checked against the manifest before rendering PNG, JPG or SVG.

XML resources, including embedded SVGs, must use UTF-8, optionally with its byte-order mark. NUL characters, DTD/entity declarations and processing instructions other than the XML declaration are refused before XML parsing.

SVG CSS must not reference resources, including data URIs, image-set() and src(). Image attributes use the separate embedded-image validator; local SVG fragment references and resource-free CSS remain supported.

Embedded SVGs, including bundled assets, must contain no text elements or font declarations in presentation attributes, inline styles or stylesheets. This includes shorthand font properties, @font-face and local() font sources. Convert image text to paths before embedding it. Native diagram labels continue to use the verified DejaVu font set.

Enabled label placeholders are refused before rendering, including object/UserObject wrappers and indirect labels, because substitution occurs after HTML validation. Literal labels with placeholders disabled remain supported. Invalid UTF-8 or JSON in the installed runtime manifest is an incomplete runtime failure, not a content refusal.

Prepared XML is limited to 8 MiB across all pages, including embedded assets, XML escaping and normalized font styles. Expansion is charged before replacement styles are retained or serialized. Each bundled asset is read and verified once per document; repeated references still consume the document budget.

The renderer runs as an unprivileged user with Electron's inner sandbox disabled. A qualified outer sandbox must enforce closed networking, CPU/memory/process limits, file confinement and process-tree disposal. Do not expose host credentials, directories or display sockets. The verification script explicitly selects container isolation; the library does not lower the host's isolation floor.

For a future container-free POSIX backend, install the same runtime paths in a read-only sandbox root and run install.py during provisioning. The installer must receive the pinned, unmodified Desktop archive. Do not run it twice against an already patched archive. Runtime files require Python 3, Pillow, Graphviz, Xvfb, xauth, DejaVu fonts and the Electron shared libraries listed by the Dockerfile. This describes the bundle layout; it does not qualify Bubblewrap, Seatbelt, Windows or Hyperlight. See the [decision record](../../docs/sandbox/research/drawio-export.md).
