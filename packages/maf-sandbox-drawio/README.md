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

A failure on any page prevents delivery of the entire artifact. The result is untrusted, whether it contains a delivery reference or a diagnostic. The host sets confidentiality and destination policy.

Verify all four layout-policy cases against Docker from a repository checkout:

```bash
uv run python scripts/check_drawio_docker.py --image drawio-sandbox:local --output out/drawio
```

See the [kind guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/drawio.md) for accepted geometry and labels, and the [image definition](https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-sandbox/Dockerfile) for pinned dependencies.
