# `diagram-sandbox` — the image `render_diagram` runs in

One layer on Debian: Graphviz, and nothing else. That is the whole image. It carries no agent code, no Python, and nothing of the host application — the sandbox runs a renderer and nothing else, and *what* to render arrives at run time as a DOT file the tool writes in. The PNG is read back out the same way, through `FILES_OUT`.

[`samples/07_docker_diagram`](../../samples/07_docker_diagram/) is the one sample that runs it, and it builds this image with `docker build`. Unlike [`images/bicep-sandbox`](../bicep-sandbox/), there is nothing to push or import: the docker backend runs what is already on the machine.

## What is in it

| | Why |
|---|---|
| `debian:bookworm-slim` | A small, stock Debian with `apt`. Nothing in the tool depends on the distribution — it runs `dot` and reads a PNG back |
| `graphviz` (`--no-install-recommends`) | Provides `dot`, the only program the sandbox runs. `--no-install-recommends` keeps the layer to the renderer and its libraries; the apt lists are dropped afterwards so nothing but the package survives |

There is no working-directory `COPY` and no fixed config: the tool writes its DOT source into `/work` at run time (the `SandboxSpec`'s `work_dir`), which the backend creates as it writes the first file. `render_diagram` names the output format on the `dot` command line, so the image holds no state of its own between the source going in and the image coming out.

## Build

From the repository root, so the build context is this directory:

```bash
docker build -t diagram-sandbox:local images/diagram-sandbox
```

That is the whole story for the sample — `docker` runs what is already on the machine, so there is nothing to push and nothing to import. Podman takes the same arguments.

That the image is built rather than pulled from a registry is deliberate for a sample: there is no widely trusted minimal Graphviz image to reference, and building one here keeps the sample's guest a thing the reader can read in five lines rather than a third-party tag whose contents they have to take on faith.

## What it may reach at run time

Nothing. `render_diagram`'s spec sets `egress_allow=()`, so the docker backend runs the container on `--network none`: Graphviz reads the source it was given and writes an image, and reaches no network at all. Build time is a different question and a different machine — `apt-get` fetches from Debian's mirrors then — but the running sandbox has no egress to fall short of.

## Reproducibility

`debian:bookworm-slim` is a moving tag: it advances as Debian is patched, and `apt-get install graphviz` resolves to whatever version the mirror serves that day (Graphviz 2.43.0, at the time of writing). Two builds a month apart are not byte-identical, and a diagram's exact pixels can shift with a Graphviz release. Pin the base by digest and the package by version if you need them to be — this image is sample-grade, chosen so the sample is legible, not so its output is bit-reproducible. A production deployment replaces it with a hardened image you build and own: minimal base, digest-pinned, scanned, rebuilt on your patch cadence, supplied through the same `image`/`image_id` spec fields — nothing else in the sample's wiring changes.
