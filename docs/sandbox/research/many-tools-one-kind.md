# One kind, many tools: what `kind` identifies and what a tool name is

> An exploration. It asks whether one kind may expose more than one tool name — `export_pdf` and `export_docx` out of a single Markdown-export workload — reads the protocol for the answer, and works out where the line between "a second tool" and "a second kind" actually falls. It is kept in the tense it was written, as the record of the argument rather than a description of what shipped. Nothing here has graduated yet; what it decides belongs in [`../kinds/README.md`](../kinds/README.md) and [`../kinds/writing-a-kind.md`](../kinds/writing-a-kind.md).

## The question

Every kind shipped or sampled today attaches exactly one tool: `bicep` attaches `bicep_validate`, `codeact` attaches `execute_code`, and the diagram sample attaches `render_diagram`. The kinds index has a `Tool` column with one entry per row, which reads as though the mapping were one-to-one by construction.

It is not, and the question is what the second tool costs. A Markdown-export workload is the clean case: pandoc turns one Markdown file into a PDF and into a Word document with the same binary, the same working directory and the same closed network, differing only in an output flag and a media type. Does that want `export_pdf` and `export_docx` as two tools of one kind, one tool with a `format` argument, or two kinds?

## The mechanism is already there, and nobody uses it

`sandboxed_tool` takes `spec` and `name` as separate arguments, and its docstring answers the question outright:

> A workload that ships more than one tool calls this once per tool and concatenates; each call answers the attach gate identically, so an unconfigured host still gets `[]`.

The diagram sample already keeps the two apart as distinct module constants — `DIAGRAM_KIND` for the spec and `RENDER_DIAGRAM_TOOL_NAME` for the tool — so the separation is in the code as well as in the signature. What is missing is a user: every call site of `sandboxed_tool` outside the test suite passes one name and returns its list unconcatenated.

So the mechanism is documented, supported, and unexercised. The interesting part is not whether it works but what sharing a `kind` between two tools actually shares.

## `kind` is identity, and a tool name is not

`SandboxSpec` says it in the field's own documentation:

> `kind` names the workload (`"bicep"` today), and it is **part of the sandbox's identity, not a display label**: a backend must never serve two kinds from one sandbox, because the first spec to arrive would decide the image and the egress policy for both

That sentence is written against two *kinds*, but the mechanism it describes is what two *tools sharing one kind* opt into deliberately. The router remembers instances per key, kind and backend in `_remember_instance`; `ExclusiveSlots` takes an admission slot per key and kind; `dispose` narrows by kind; and the backend's `acquire` is a get-or-create over the same pair. At the default `IsolationScope.CONVERSATION` the instance outlives the call, so two tools sharing a kind share one container for the whole thread — which is the point, and also the hazard.

Admission is worth naming separately, because it is the part that could have made the sharing useless and does not. `enter_call` never asks for an exclusive hold; the exclusive flag is spent only on the get-or-create gate inside `_acquire`. Two sibling tools called in the same assistant message are therefore admitted concurrently against the one sandbox, each writing under its own `guest_call_path`, and neither waits on the other.

## Where the line falls

The split is decided by one question: does the field describe the *instance* or the *call*?

| Baked into the instance — siblings must agree | Resolved per tool or per call — siblings may differ |
|---|---|
| `image`, `image_id` | `declared_outputs`, and their media types |
| `work_dir` | `outputs_named_at_call_time` and the body-built `DeclaredOutput` |
| `egress`, `egress_allow` | `files_in` / `files_out` limits |
| `isolation_scope` | `output_sink` |
| `min_isolation`, `requires`, `requires_os_family` | `source_integrity`, `standing_guidance`, `approval_mode` |
| | the body, and the docstring the model reads as the description |

The right-hand column is resolved by `sandboxed_tool` at attach or by the body at call time, and nothing about the running sandbox depends on it. The left-hand column is either handed to `backend.acquire`, where the first arrival wins, or read by the route.

The route deserves its own line. `BackendSelection.PER_SPEC` is documented as "Per *spec*, not per conversation: two kinds under one key may route apart by design" — so two sibling tools of one kind whose `requires` or `min_isolation` differ can select *different backends*, and the router will then hold two instance sets under one kind. Nothing refuses it. It is simply not what anyone means by "these two tools share a sandbox".

## What the three backends do when siblings disagree

This is where the prose rule and the code diverge, and it is the finding worth keeping. The protocol says the first spec decides image and egress; the backends do not agree on what happens next.

| Divergence between sibling specs | Docker | wslc | ACAS |
|---|---|---|---|
| `egress_allow` | partitions — `_container_name` folds an `_egress_id` into the container name, so each allowlist gets its own container | partitions, by the same naming rule | **refuses** — `_get_or_create` raises `AcasEgressPolicyConflict` when the held sandbox's egress differs |
| `work_dir` | verified against the allocated base | verified | **refuses** — a held sandbox cannot change its storage base |
| `image` | **silently reuses the first** — the image is not folded into the name and is not compared on reuse | silently reuses the first | silently reuses the first — the registry lookup checks egress and work dir, never the image |

Three postures for one rule: two backends partition on egress, one refuses, and *none* of them notices an image change. A pandoc kind whose PDF path wanted a TeX layer and whose docx path did not would attach cleanly, pass every capability check, and then run the second tool inside the first tool's container — on every backend, with no diagnostic anywhere.

That asymmetry is defensible for egress, which is a containment claim the backends were built to enforce, and much less defensible for the image, which is where the whole failure mode of "the first spec decides" actually bites. The protocol asserts the rule in a docstring; the enforcement is partial and backend-specific.

## Two candidate workloads

**markitdown, inbound.** Converting PDF and Word *into* Markdown looks like a two-tool kind and is not one. `markitdown_pdf` and `markitdown_docx` would share the image, the argv, the output shape and the spec, and markitdown sniffs the input type itself — the split buys a longer tool list and nothing else. One `convert_to_markdown(file)` is the honest shape. The one thing that would force a second kind here is not the format at all: markitdown's URL and Document-Intelligence converters reach the network, and that is a different `egress` posture, which is baked into the instance.

**pandoc, outbound.** Exporting Markdown to PDF and to Word is the genuine candidate, because everything in the left-hand column agrees — one image, one work dir, `Egress.CLOSED` because rendering is computation — and everything that differs sits in the right-hand column: the `DeclaredOutput` media type, the byte limits, the guidance and the description.

## The split is a description problem, not a protocol one

Even in the pandoc case the second tool is not *required*. The diagram sample sets `outputs_named_at_call_time` and builds its `DeclaredOutput` — path, media type and landed name — inside the body, precisely because the path carries the call's own id. The same lever sets the media type from a `format` argument, so a single `export_markdown(file, format)` covers both formats under one spec with no protocol strain.

What two names buy is therefore not capability but *description*. The body's docstring is the model-facing tool description, passed through verbatim; a PDF export has engine and page-size caveats a docx export does not, and folding both into one description makes each one worse. Two names also give each format its own `standing_guidance`, its own `approval_mode`, and its own host-set confidentiality. Weigh that against a tool list that doubles for every format added — which is the argument for the enum, and it wins as soon as the only real difference is a flag.

Stated as a rule: **same image and same egress, as many tool names as the model benefits from; different image or different egress, different kinds.**

## What the second tool costs

- **The attach gate is all-or-nothing by construction.** Each `sandboxed_tool` call answers it identically, so an unconfigured host still gets `[]` from both — but a kind shipping several tools should pin that in a test, because "both attach or neither" is now a property rather than a tautology.
- **Refusals name the workload, not the tool.** Every refusal the router raises quotes `spec.kind`, and the served kind is the fallback the observer files a call under when the framework named no tool. Two tools sharing a kind are indistinguishable in a refusal message and in that fallback.
- **Disposal is per kind.** `dispose_kind` and the `kind=` selector take out every sibling at once. That is correct — they share the instance — but it means no tool can be reset independently of the others.
- **The kinds index assumes one.** Its `Tool` column has one entry per row, and a multi-tool kind needs that shape changed rather than a comma-separated cell.

## What is not settled

- **Nothing refuses two sibling specs that disagree on the image.** The rule exists in the `SandboxSpec` docstring and in `_container_name`'s reasoning; no code checks it. `sandboxed_tool` cannot see its siblings, so the check has nowhere obvious to live — the router could remember the spec it last served for a key and kind and refuse a materially different one at acquire, which is where ACAS already refuses on egress, and would make the three backends agree by moving the check above them.
- **Whether `files_out` limits and `confined_to_guest_call_path` may safely differ across siblings is untested.** Both are read per call and neither is handed to `acquire`, so the reasoning says yes; the reasoning said yes about the image too, until the backends were read.
- **Whether the guidance should discourage the split at all.** Both worked examples above resolved to "one tool" or "two kinds", and only the pandoc case landed on "two tools, one kind". A pattern whose best example is that narrow may belong in [`../kinds/writing-a-kind.md`](../kinds/writing-a-kind.md) as a caution rather than as a recipe.
