# The draw.io kind

`maf-sandbox-drawio` attaches `create_drawio(xml: str)` to a MAF agent. The model supplies native uncompressed draw.io XML. A fixed guest program validates the graph, applies the host's layout policy and writes one editable `diagram.drawio` file. The host's `OutputSink` receives its bytes; the model receives its display reference after delivery.

## Host configuration

The factory `make_drawio_tools(router, agent_id, context, sink, *, image=None, preserve_layout=True, direction="TB", exec_timeout_seconds=60)` follows the ordinary kind attachment pattern. The [package README](../../../packages/maf-sandbox-drawio/README.md) gives the wiring and model input example. Build [the supplied image](../../../images/drawio-sandbox/Dockerfile) with Python 3 and Graphviz, or provide an equivalent image through the backend.

Layout belongs to the kind configuration. The model-facing schema contains only `xml`; the host chooses the layout policy, image, timeout and sink. `preserve_layout=True` retains existing geometry on each complete page. Pages with missing vertex geometry always receive automatic layout. `False` applies automatic layout to every page. `direction="TB"` and `"LR"` select top-to-bottom and left-to-right placement.

## XML and layout contract

The input is an `mxfile` with one to eight uncompressed `diagram` pages, or a bare `mxGraphModel` that the converter wraps in one page. Cell IDs, labels, styles, object metadata, parents and edge endpoint relationships remain native XML. Structural cells `0` and `1` are required on every page. A matching ID repeated on an object wrapper's inner cell is retained on the wrapper only. Validation rejects other duplicate XML IDs within a page, missing or cyclic parents, invalid endpoints and invalid supplied geometry in both layout modes. Geometry attributes are restricted to positions, dimensions and their supported `as`/`relative` fields; waypoint arrays contain unnamed points and cannot declare a length. Numbers use ASCII decimal or scientific notation. DTDs, entity declarations, compressed pages and unsupported cell/geometry elements are errors. Geometry is validated again after layout and every page must succeed before output is written.

A page has layout when every vertex has geometry and positive width and height. Coordinates omitted from otherwise complete geometry default to zero. Origin coordinates and overlaps do not imply missing layout. Relative edge-label vertices need their relative geometry; connector waypoints are optional. An attached edge missing its geometry receives standard relative geometry without repositioning its vertices.

Automatic mode supports flat flowcharts and component graphs, with multiple layers, disconnected nodes, cycles, self-loops and parallel edges. Graphviz computes placement and polyline connector points over all page vertices while their layer membership remains unchanged. Placement reserves each vertex's rotated bounds when its style sets `rotation`. Only generated identifiers and validated dimensions enter DOT: XML IDs, labels, styles and links are never passed to Graphviz. Missing dimensions default to 160 by 80 units; supplied dimensions and rotation remain. Routing attributes, including `sourcePort` and `targetPort`, and waypoints are replaced, and `childLayout` hints are removed from automatically laid-out pages. Appearance attributes such as colors and arrowheads remain.

Nested groups, relative ports, edge-label vertices, collapsed cells and detached edges require complete geometry with preservation enabled. Automatic layout refuses these structures instead of flattening them. Diagram-specific semantics such as sequence-message order are the model's responsibility. Layout does not automatically fit arbitrary labels or guarantee text never overlaps. The converter does not render or fetch referenced images, fonts or links; references remain in the file, so structural validation does not sanitize content for an eventual viewer.

## Execution and delivery

The kind requires `EXEC`, `FILES_IN` and `FILES_OUT`, with closed egress and one output. The converter and input are written under `SandboxToolSession.guest_call_path()` and executed with fixed argv. No model value becomes a command argument. Confinement is undeclared; the default cleanup policy disposes the sandbox. No backend or core protocol changes are required.

Limits are 1 MiB of input, eight pages, 1000 cells per page, and 2 MiB of output. Automatic layout accepts at most 200 vertices and 600 edges per page. Parsing bounds nesting and element count. Graphviz has bounded retained stdout/stderr and one layout deadline shared across pages, inside the sandbox exec timeout. Diagnostics are limited to 2048 characters. The host may set the timeout from greater than zero up to 300 seconds; its default is 60 seconds.

Only successful conversion reaches `collect_outputs`, using the literal call-relative path and landing name `diagram.drawio`, media type `application/xml`. A missing file or failed sink delivery is an error. Repeated names need a host-selected replacement policy or a sink with `per_call=True`. Private storage handles and transport diagnostics stay with the host. `SourceIntegrity.UNTRUSTED` is explicit because artifact content and validation diagnostics derive from model input.

## Status

| Decision | State | Tracking |
| --- | --- | --- |
| Model XML produces one editable draw.io artifact through the sandbox output pipeline | implemented; not yet released | [#1251](https://github.com/sokolaidev/maf-extensions/issues/1251) (open) |
| Preserve supplied layout by default, automatically lay out missing geometry on each page, and expose a host override | implemented for the flat-graph automatic subset described above | [#1251](https://github.com/sokolaidev/maf-extensions/issues/1251) (open) |
| Specialized automatic layouts and previews | outside this implementation | untracked |
