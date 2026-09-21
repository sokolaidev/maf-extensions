# draw.io

`create_drawio(xml: str)` turns native draw.io XML into one editable `diagram.drawio` file. A guest program checks the graph and applies the host's layout settings. The host's `OutputSink` receives the file; the model receives completion, verdict and output items.

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
| Result integrity | Trusted completion and verdict; untrusted sink display and converter diagnostics; no standing guidance |

Build the [supplied image](../../../images/drawio-sandbox/Dockerfile) or provide an equivalent one. Docker hosts use `await DockerSandboxBackend.create(config)` to discover the guest family before attachment.

## Result

The kind uses the [result contract](../information-flow.md#the-result-contract). `verdict` is `created` after artifact delivery and `refused` when the converter rejects the source or an unsupported layout request. `completed` is false, with no verdict, for invalid tool arguments, an unavailable sandbox, execution or Graphviz failures, timeouts, missing output and failed delivery. The renderer reserves exit code 2 for source rejection and exit code 3 for operational failure; other nonzero exits also report incomplete conversion.

The sink's display reference and the converter's diagnostic are `output`, labelled untrusted because either can contain guest-derived text. Fixed explanations from the kind are `trusted_output`. The wrapper renders completion and verdict as separate trusted items, so a host can hide workload output while leaving the result readable.

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

![The result contract gives draw.io trusted completion, verdict and fixed explanations, with separately labelled untrusted sink display or converter diagnostics. FIDES can hide untrusted items while leaving the verdict readable. Every item carries the call's confidentiality. Later model-called tools enforce destination policy. The artifact reaches the configured OutputSink during create_drawio.](../assets/drawio-information-flow.svg)

The XML argument can contain expanded hidden content. Diagnostics can quote it, and the sink can compose its display from artifact bytes. The kind declares `SourceIntegrity.UNTRUSTED` for workload output. The result contract raises the attached tool's declaration to trusted and writes the untrusted label only on output items.

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
| Four-field result contract | Implemented for draw.io, sample 18 and its checks; remaining adoption tracked separately | [#1374](https://github.com/sokolaidev/maf-extensions/pull/1374) (merged); [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (open) |
