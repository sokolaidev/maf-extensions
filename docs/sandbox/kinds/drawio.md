# draw.io

`create_drawio(xml: str)` turns native draw.io XML into one editable `diagram.drawio` file. A guest program checks the graph and applies the host's layout settings. The host's `OutputSink` receives the file; the model receives a delivery reference.

See the [package README](../../../packages/maf-sandbox-drawio/README.md) for wiring and an input example.

## Contract

| Setting | Value |
|---|---|
| Kind and tool | `drawio`; `create_drawio` |
| Required capabilities | `EXEC`, `FILES_IN`, `FILES_OUT` |
| Guest | POSIX, with Python 3 and Graphviz `dot` |
| Network | `CLOSED` |
| Output | One `diagram.drawio` file, `application/xml` |
| Cleanup | Disposal by default; no call-directory confinement claim |
| Result integrity | `untrusted`; no standing guidance |

Build the [supplied image](../../../images/drawio-sandbox/Dockerfile) or provide an equivalent one. Docker hosts use `await DockerSandboxBackend.create(config)` to discover the guest family before attachment.

## Result

The kind uses the [result contract](../information-flow.md#the-result-contract). `verdict` is `created` where the converter produced a diagram and `refused` where it ran and rejected the source. `completed` is false where the converter never ran — a source that is not text, an oversized input, an unavailable sandbox — and such a call carries no verdict at all.

The delivered artifact's display reference is `trusted_output`: the sink minted it for a name this kind fixed, so it carries nothing the supplied source chose. The converter's own diagnostic is `output`, because it quotes whatever the source made it say. What this kind says about refusing before the converter ran is `trusted_output` too.

## Host configuration

The model supplies only `xml`. The host selects the sink, image, timeout and layout:

| Option | Behavior |
|---|---|
| `preserve_layout=True` | Keep complete geometry; lay out pages with missing vertex geometry |
| `preserve_layout=False` | Lay out every page |
| `direction="TB"` | Top-to-bottom layout |
| `direction="LR"` | Left-to-right layout |
| `exec_timeout_seconds` | Default 60; must be finite, greater than zero and at most 300 |

## XML and layout

Accept an `mxfile` with one to eight uncompressed pages, or a bare `mxGraphModel` wrapped into one page. Each page needs structural cells `0` and `1`.

Native IDs, labels, styles, object metadata, parents and edge relationships are retained. A matching ID on an object wrapper's inner cell stays on the wrapper only. Other duplicate IDs, missing or cyclic parents, invalid endpoints and invalid geometry are refused.

DTDs, entity declarations, compressed pages and unsupported elements are refused. Geometry allows supported position, dimension, `as` and `relative` fields. Waypoint arrays contain unnamed points without a declared length. Numbers use ASCII decimal or scientific notation.

A complete layout gives each ordinary vertex a positive width and height. Edge-label vertices need relative geometry instead. Detached edges need explicit endpoint points.

Omitted coordinates default to zero. Overlaps and origin coordinates do not trigger layout. Missing edge geometry receives standard relative geometry.

| Structure | Layout support |
|---|---|
| Flat graphs, multiple layers, disconnected nodes, cycles, self-loops and parallel edges | Automatic layout or preservation |
| Nested groups, relative ports, edge-label vertices, collapsed cells and detached edges | Complete geometry with preservation enabled |

![After parsing the XML, the converter checks each page's cells, endpoints and geometry. It keeps complete geometry when preservation is enabled. Otherwise the page must support automatic layout within the layout limits; unsupported cases are refused. Graphviz lays out accepted pages. Both routes fill missing edge geometry and check the result. Only after every page succeeds and the output fits the size limit does the tool write diagram.drawio and deliver it through OutputSink. Any invalid page, failed layout or exceeded limit stops delivery; no partial artifact is returned.](../assets/drawio-layout-flow.svg)

Graphviz receives generated IDs and validated dimensions, never XML labels, links or styles. Missing dimensions default to 160 by 80 units. Supplied dimensions and rotation are retained.

Automatic layout replaces connector routing and waypoints, including `sourcePort` and `targetPort`, and removes `childLayout` hints. Appearance and layer membership remain. Geometry is checked again before output is written.

The model owns diagram meaning, such as sequence-message order. Layout does not guarantee that text fits or never overlaps. Image, font and link references stay in the file; the converter neither fetches them nor sanitizes them for a viewer.

## Result labels and tool flow

![The draw.io tool is a source tool declaring untrusted integrity. Its delivery reference or diagnostic is one untrusted content result with host-controlled confidentiality. It has no trusted guidance item. FIDES shows the text or a hidden reference to the model. The model's next call to a reader, writer or other tool is checked against that destination's integrity and confidentiality policy. Artifact delivery occurs separately during create_drawio.](../assets/drawio-information-flow.svg)

The XML argument can contain expanded hidden content. Diagnostics can quote it, and Graphviz is a separate program producing layout output. The result therefore claims `SourceIntegrity.UNTRUSTED` explicitly.

The artifact goes to the configured sink during the call. The diagram shows the text result and later model-called tools. The host supplies result confidentiality and any outward confidentiality limit; [information flow](../information-flow.md) explains the distinction.

## Execution and delivery

Input and converter files are written beneath `session.guest_call_path()`. Execution uses fixed arguments. Model values do not become command arguments.

| Limit | Maximum |
|---|---|
| Input / output | 1 MiB / 2 MiB |
| Pages / cells per page | 8 / 1000 |
| Automatic layout per page | 200 vertices, 600 edges |
| Returned diagnostics | 2048 characters |

Parsing also bounds nesting and element count. Graphviz has bounded retained output and one layout deadline shared across pages, within the execution timeout.

Every page must succeed before collection. Missing output or failed delivery is an error. Repeated file names need a host-selected replacement policy or `per_call=True`. Private storage handles and transport details stay with the host.

## Status

| Contract | State | Details |
|---|---|---|
| Editable output, XML checks and configured layout | Implemented | [Package README](../../../packages/maf-sandbox-drawio/README.md) |
| Specialized automatic layouts and previews | Outside the supported contract | untracked |
| Four-field result contract | Open; this kind returns text | [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (open) |
