# maf-sandbox-drawio

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-drawio/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxDrawioExperimentalWarning`.

Create an editable `diagram.drawio` from model-supplied XML. The `create_drawio(xml: str)` tool validates draw.io cells, applies layout when needed and delivers the file through the host's `OutputSink`.

Requires Python 3.12–3.14.

```bash
pip install maf-sandbox-drawio
```

## Attach the tool

```python
from maf_sandbox_drawio import make_drawio_tools

tools = make_drawio_tools(
    router,
    agent_id="diagram-designer",
    context=context,
    sink=sink,
    image="drawio-sandbox:local",
    preserve_layout=True,
    direction="TB",
)
```

The host supplies the router, `CallerContext` and sink. The model supplies only XML; storage and layout settings belong to the host.

Use `make_file_system_sink(output_directory, existing="replace")` to replace the previous diagram, or a per-call sink to retain separate outputs. The result contains the sink's display reference after delivery.

Build the image from the repository root:

```bash
docker build -t drawio-sandbox:local images/drawio-sandbox
```

The image needs Python and Graphviz. The kind requires a POSIX backend with `EXEC`, `FILES_IN` and `FILES_OUT`. It uses closed network access and the host's isolation floor. Core disposes after each call by default.

For Docker, use `await DockerSandboxBackend.create(config)` so the backend declares POSIX. The plain constructor does not declare an OS family.

## Optional image exports

Build the export runtime with `docker build -t maf-drawio-export:local images/drawio-export`, then opt in through host configuration:

```python
from maf_sandbox_drawio import DrawioExport, make_drawio_tools

tools = make_drawio_tools(
    router,
    agent_id="diagram-designer",
    context=context,
    sink=sink,
    image="maf-drawio-export:local",
    export=DrawioExport(formats=("png", "jpg", "svg")),
    exec_timeout_seconds=120,
)
```

Each selected page produces `diagram-1.png`, `diagram-1.jpg` and `diagram-1.svg` (and so on), alongside the editable `diagram.drawio`. Formats are a nonempty unique tuple of `png`, `jpg` and `svg`. Pages default to all pages; `pages=(2,)` selects the second page. The host can set `scale` in (0, 4], `transparent=True` for PNG/SVG, and `jpeg_quality` from 1 to 100. JPG uses an opaque background. File limits become 25 outputs, 8 MiB per file and 32 MiB total; each render is limited to 4096 pixels per axis and 16 million pixels. The execution deadline covers layout and all exports.

To export an existing file, attach `make_drawio_export_tools(router, agent_id, context, sink, store, export=DrawioExport(...), image=...)`. Its `export_drawio(file)` argument must match exactly one visible store reference. The store supplies uncompressed XML text with complete geometry; the tool reads it through the caller context and does not run layout again. The stored source is not overwritten, although the returned editable copy may have normalized XML formatting.

Pass the host's `file_store_provenance` and optional `requires_file_integrity` to apply the core's recorded-read and admission policy. Resource validation does not promote source integrity.

The offline profile accepts bundled cloud icons (including `img/lib/azure2/...`), validated embedded PNG/JPEG/SVG images and formatting-only HTML labels. Known `https://app.diagrams.net/img/lib/...` references map to bundled assets without a request. External resources, missing assets, unsupported shapes/fonts, math, links and background resources are refused. Fonts are DejaVu Sans, Serif and Sans Mono; common Arial/Helvetica, Times New Roman and Courier New requests map to those families. Other languages may require a future expanded font profile.

SVG embeds images and font data. Rich labels use `foreignObject`, so use a compatible browser viewer; arbitrary SVG consumers may display them differently. Raster and SVG export use the same native document renderer. Every requested output is checked before sink delivery, but the sink is not transactional: a later delivery failure can leave earlier files saved and returns an incomplete result.

Run `uv run python scripts/check_drawio_exports.py --image maf-drawio-export:local --output out/drawio-exports` to exercise real offline rendering. The image is Linux amd64; container-free execution is tracked separately in [#1656](https://github.com/sokolaidev/maf-extensions/issues/1656). A backend must provide the installed runtime and enforce closed networking and process cleanup. Export support does not change the host's isolation floor.

## Model input

Accepts one uncompressed `mxGraphModel` or an `mxfile` containing uncompressed pages:

```xml
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="a" value="Start" vertex="1" parent="1"/>
    <mxCell id="b" value="Finish" vertex="1" parent="1"/>
    <mxCell id="edge" edge="1" source="a" target="b" parent="1"/>
  </root>
</mxGraphModel>
```

The output keeps native editable shapes, connectors, labels, styles, IDs, metadata and layers. It does not render a preview or fetch external resources. XML references remain in the artifact, so validation is not content sanitization.

## Layout policy

| Input geometry | `preserve_layout=True` | `preserve_layout=False` |
|---|---|---|
| Complete | Keep supplied positions and sizes | Apply automatic layout |
| Incomplete | Apply automatic layout | Apply automatic layout |

Complete geometry needs positive width and height for each vertex. Missing `x` or `y` means zero. Overlapping vertices are accepted. Missing connector waypoints do not trigger layout; missing edge geometry receives standard relative geometry.

Automatic layout handles flat flowcharts and component graphs, including cycles and disconnected components. `direction="TB"` places the graph top to bottom; `"LR"` places it left to right.

Graphviz chooses positions and connector paths. Supplied dimensions and rotation are retained; missing dimensions use 160 by 80. Automatic layout replaces routing hints while keeping appearance styles.

Nested groups, relative ports, edge-label vertices, collapsed cells and detached edges require complete geometry with preservation enabled. They are refused when automatic layout is needed.

The tool provides no sequence, BPMN or ER-specific layout, automatic label fitting or guarantee against every overlap. Give large labels explicit dimensions. Preserved geometry keeps its values, not byte-for-byte XML formatting.

## Validation and limits

Every page is checked before and after layout. Invalid IDs, parent cycles, broken endpoints, unsupported geometry and non-finite coordinates are refused. DTDs, entities and compressed pages are refused too.

| Limit | Value |
|---|---|
| Input | 1 MiB, up to 8 pages and 1,000 cells per page |
| Automatic layout | Up to 200 vertices and 600 edges per page |
| Output | One file, at most 2 MiB |
| Execution | One deadline for all pages: 60 seconds by default, at most 300 |
| Diagnostics | At most 2,048 characters |

A failure on any page prevents delivery of the entire artifact. `create_drawio` returns separate content items: trusted completion and verdict, any fixed explanation, and untrusted sink display text or converter diagnostics. The verdict is `created` after delivery and `refused` for rejected XML or unsupported layout requests. Invalid tool arguments, execution failures, missing output and delivery failures remain incomplete with no verdict. The host sets confidentiality and destination policy.

Verify all four layout-policy cases against Docker from a repository checkout:

```bash
uv run python scripts/check_drawio_docker.py --image drawio-sandbox:local --output out/drawio
```

See the [kind guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/drawio.md) for accepted geometry and labels, and the [image definition](https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-sandbox/Dockerfile) for pinned dependencies.
