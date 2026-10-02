# Offline Draw.io renderer

An opt-in Linux amd64 runtime for maf-sandbox-drawio image exports. The existing drawio-sandbox image remains sufficient for editable XML output.

```bash
docker build -t maf-drawio-export:local images/drawio-export
uv run python scripts/check_drawio_exports.py --image maf-drawio-export:local --output out/drawio-exports
```

The build verifies the Draw.io Desktop 31.7.0 Debian distribution's SHA-256, installs native dependencies and a fixed DejaVu font set, and prepares /opt/maf-drawio/manifest.json. Network access is needed at build time. Deployment should pin the resulting approved image digest; Debian package updates are not a reproducible-build guarantee.

install.py repacks Desktop's ASAR with guard.js to refuse missing images, unknown shapes and excessive dimensions. export.py resolves local assets, validates embedded images and HTML, runs the native exporter and validates artifacts before publishing its manifest. The host never imports or executes this runtime directly. Preserve the upstream Desktop, Electron, Draw.io and font notices shipped in the image when redistributing it; the modified export behavior is defined by these source files.

The renderer runs as an unprivileged user with Electron's inner sandbox disabled. A qualified outer sandbox must enforce closed networking, CPU/memory/process limits, file confinement and process-tree disposal. Do not expose host credentials, directories or display sockets. The verification script explicitly selects container isolation; the library does not lower the host's isolation floor.

## Without a container engine

On Linux amd64, install debootstrap and run `sudo bash images/drawio-export/build-runtime.sh /opt/maf-runtime` with a new destination directory. This provisions Debian bookworm, the verified Desktop package, native libraries, fonts and the same offline exporter directly. It does not build or extract an OCI image. Network access and root are needed for provisioning only. Retain the runtime manifest and approve the directory's distribution separately; Debian packages are not pinned to a snapshot, so this is not a reproducible root filesystem.

Configure the [Bubblewrap backend](../../packages/maf-sandbox-bubblewrap/README.md) with that read-only runtime and a delegated cgroup v2 subtree, then run `scripts/check_bubblewrap_exports.py`. The application runs outside the runtime as an unprivileged Linux user. Host startup probes namespaces, resource controls and the guest interpreter; the export check qualifies the Electron dependencies and bundled resources. Missing prerequisites refuse rather than execute on the host.

The installer must receive the pinned, unmodified Desktop archive; do not run it twice against an already patched archive. Keep the upstream notices with the runtime. Windows, Seatbelt and Hyperlight native rendering remain separate work. See the [decision record](../../docs/sandbox/research/drawio-export.md).
