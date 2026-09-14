# maf-sandbox-drawio

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-drawio)](https://pypi.org/project/maf-sandbox-drawio/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-drawio/LICENSE)

> **Experimental.** This package warns on import with `MafSandboxDrawioExperimentalWarning`. Releases before 1.0 may change or remove APIs without notice.

Create an editable `diagram.drawio` file from model-supplied XML. The `create_drawio(xml: str)` tool validates native draw.io cells, applies automatic layout when needed, and delivers the file through the host's `OutputSink`.

This package is experimental and not yet released. It requires Python 3.12 or newer. Install it from this workspace with `uv sync --all-packages` until its first release.

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

The host supplies a `SandboxRouter`, `CallerContext`, and `OutputSink`, just as it does for other sandbox kinds. Use `make_file_system_sink(output_directory, existing="replace")` to keep the most recent diagram, or a sink with `per_call=True` to distinguish outputs from different calls. The tool returns the sink's display reference after successful delivery. The model supplies neither a storage path nor layout settings.

Build the image from the repository root:

```text
docker build -t drawio-sandbox:local images/drawio-sandbox
```

Verify all four layout-policy combinations through a real Docker backend without a model:

```text
uv run python scripts/check_drawio_docker.py --image drawio-sandbox:local --output out/drawio
```

The image provides `python3` and Graphviz `dot`. The kind uploads its fixed converter with each call; no package installation or network access occurs in the guest. It requires `EXEC`, `FILES_IN`, and `FILES_OUT`, and declares closed egress. Use a backend that provides all three capabilities. Confinement is undeclared, so default cleanup disposes the sandbox.

## Model input

Supply an uncompressed `mxfile` or a bare `mxGraphModel`. For example, this input has no layout and produces two connected native shapes:

```xml
<mxGraphModel>
  <root>
    <mxCell id="0" />
    <mxCell id="1" parent="0" />
    <mxCell id="agent" value="Agent" vertex="1" parent="1" style="rounded=1;whiteSpace=wrap;html=1;" />
    <mxCell id="file" value="diagram.drawio" vertex="1" parent="1" style="shape=document;whiteSpace=wrap;html=1;" />
    <mxCell id="creates" edge="1" parent="1" source="agent" target="file" style="endArrow=block;" />
  </root>
</mxGraphModel>
```

Labels, styles, IDs, object metadata, layer membership, and endpoint references survive conversion. A matching ID repeated on an object wrapper's inner cell is retained on the wrapper only; other duplicate XML IDs within a page are rejected. The converter writes an uncompressed UTF-8 file with native vertices and connectors. It does not render a preview or fetch images, links, fonts, or other resources mentioned in the XML. Such references remain in the artifact for its eventual consumer; XML validation is not content sanitization.

## Layout policy

| `preserve_layout` | Page has complete vertex geometry | Behavior |
| --- | --- | --- |
| `True` (default) | Yes | Preserve geometry, including connector waypoints. |
| `True` | No | Apply automatic layout to the page. |
| `False` | Either | Apply automatic layout to the page. |

The decision is per page. A vertex needs an `mxGeometry` with positive width and height; omitted x/y coordinates default to zero. Coordinates at the origin and overlapping shapes are valid supplied layout. Missing connector waypoints do not trigger layout. An attached edge without geometry receives the standard relative edge geometry without moving its vertices. Missing dimensions trigger layout; invalid supplied dimensions or non-finite coordinates are errors in both modes.

Automatic layout supports **flat flowcharts and component graphs**, including multiple layers, disconnected nodes, cycles, self-loops, and parallel edges. All layer vertices participate in one layout, retaining their layer membership. Graphviz determines placement and polyline connector routes, reserving each vertex's rotated bounds when its style sets `rotation`. Supplied dimensions and rotation are retained and missing dimensions default to 160 by 80 units. `direction="TB"` flows top to bottom; `"LR"` flows left to right. Routing styles, including `sourcePort` and `targetPort`, and old waypoints are replaced; embedded `childLayout` hints are removed on automatically laid-out pages. Colors, arrowheads and other appearance attributes remain.

Nested groups, relative ports, edge-label vertices, collapsed cells and detached edges require complete supplied geometry and `preserve_layout=True`. Automatic mode rejects those structures with a diagnostic. It does not infer sequence, BPMN, or ER-specific layout rules, fit arbitrary labels, or promise collision-free text. Large labels may need explicit dimensions. Preservation retains geometry values, not the exact XML byte formatting.

## Validation and limits

Validation checks XML structure, unique page/XML IDs, parent references and cycles, edge endpoints, and finite geometry before and after layout. Numbers use ASCII decimal or scientific notation; underscores and Unicode digits are refused. Geometry elements accept only their supported attributes: position/dimensions, the appropriate `as` role, and `relative` on `mxGeometry`. Waypoint arrays contain unnamed points and cannot declare their own length. Put custom metadata on object wrappers. DTDs, entity declarations, compressed pages, and unsupported cell/geometry elements are refused. A malformed later page prevents the whole output from being written.

Input is capped at 1 MiB, 8 pages and 1000 cells per page, with XML depth and element limits. Automatic layout accepts up to 200 vertices and 600 edges per page. Output is capped at 2 MiB and one file. The host sets `exec_timeout_seconds` (default 60, maximum 300); layout shares one deadline across pages and bounds retained subprocess output. Failure diagnostics are limited to 2048 characters and remain untrusted. Transport details stay in host logs.

The result explicitly declares `SourceIntegrity.UNTRUSTED`: both file content and validation diagnostics derive from model input. The host chooses the output sink and any outward confidentiality policy.

See the [kind design](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/drawio.md), [sandbox host wiring](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md), and [kind authoring guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/writing-a-kind.md).
