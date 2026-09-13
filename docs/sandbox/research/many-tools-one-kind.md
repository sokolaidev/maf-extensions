# One kind, many tools: what `kind` identifies and what a tool name is

> An exploration. It asks whether one kind may expose more than one tool name — `export_pdf` and `export_docx` out of a single Markdown-export workload — reads the protocol for the answer, and works out where the line between "a second tool" and "a second kind" actually falls. It is kept in the tense it was written, as the record of the argument rather than a description of what shipped. Nothing here has graduated yet; what it decides belongs in [`../kinds/README.md`](../kinds/README.md) and [`../kinds/writing-a-kind.md`](../kinds/writing-a-kind.md).

## The question

Every kind shipped or sampled today attaches exactly one tool: `bicep` attaches `bicep_validate`, `codeact` attaches `execute_code`, and the diagram sample attaches `render_diagram`. The kinds index has a `Tool` column with one entry per row, which reads as though the mapping were one-to-one by construction.

It is not, and the question is what the second tool costs. A Markdown-export workload is the clean case: pandoc turns one Markdown file into a PDF and into a Word document with the same binary, the same working directory and the same closed network, differing only in an output flag and a media type. Does that want `export_pdf` and `export_docx` as two tools of one kind, one tool with a `format` argument, or two kinds?

## The mechanism is already there, and nobody uses it

`sandboxed_tool` takes `spec` and `name` as separate arguments, and its docstring answers the question outright:

> A workload that ships more than one tool calls this once per tool and concatenates; each call answers the attach gate identically, so an unconfigured host still gets `[]`.

The diagram sample already keeps the two apart as distinct module constants — `DIAGRAM_KIND` for the spec and `RENDER_DIAGRAM_TOOL_NAME` for the tool — so the separation is in the code as well as in the signature. What is missing is a user: every call site of `sandboxed_tool` outside the test suite passes one name and returns its list unconcatenated.

The factory documents how to expose multiple names. Sharing a `kind` also raises compatibility questions that the unused concatenation pattern alone does not answer.

## `kind` is identity, and a tool name is not

`SandboxSpec` says it in the field's own documentation:

> `kind` names the workload (`"bicep"` today), and it is **part of the sandbox's identity, not a display label**: a backend must never serve two kinds from one sandbox, because the first spec to arrive would decide the image and the egress policy for both

That sentence is written against two *kinds*. Two tools of one kind can instead address the same instance when their effective key, selected backend and backend reuse partition agree. The router remembers instances per key, kind and backend in `_remember_instance`; `ExclusiveSlots` takes admission holds per key and kind; and `dispose_kind` can sweep the kind across backends. A kind can therefore have several instances, and its name alone does not promise sharing.

At the default effective `IsolationScope.CONVERSATION`, calls use a key without a call id. Lifetime is a separate decision: the router defaults to `Cleanup.DISPOSE`, which removes an acquired instance after its active holders drain, so a later call normally creates another. Warm reuse across successive calls requires an explicit host cleanup floor permitting `RECLAIM` or `RESET`, a compatible spec floor and backend support, and successful cleanup. Effective `IsolationScope.CALL` gives each call a unique key and always disposes its instance.

Admission is separate again. `enter_call` passes `exclusive=exclusive or spec.exclusive_admission` to `ExclusiveSlots.take`; codeact sets `exclusive_admission=True`, so its sibling calls serialize. Ordinary sibling bodies may overlap while the entry is serving, with distinct paths when they use `guest_call_path`, but arrivals wait during draining or cleanup and behind exclusive holders. `_acquire` separately serializes get-or-create and adoption. Sharing permits overlap; it does not guarantee that neither call waits.

## Where the line falls

The split needs more than an instance-versus-call distinction. Routing constraints, admission and cleanup policy affect whether an instance can be shared without being properties baked into it.

| Role | Fields or arguments | Consequence for siblings |
|---|---|---|
| Instance configuration | `image`, `image_id`, `work_dir`, `egress`, `egress_allow` | Deliberate reuse needs agreement on the effective image, storage base and egress policy; enforcement is backend-specific, as below |
| Routing and refusal constraints | `min_isolation`, `requires`, `requires_os_family`, `files_in`, `files_out` | Each spec must pass the selected backend's checks; differing values can still select the same backend. Transfer limits also cap each call |
| Key scope | `isolation_scope` | The stricter host/spec scope decides whether the key names the conversation or one call |
| Cleanup policy | `min_cleanup` | The host/spec floors and backend capabilities decide whether a conversation-scoped instance can survive cleanup |
| Admission policy | `exclusive_admission` | An exclusive call excludes sibling calls sharing its key and kind |
| Confinement description | `confined_to_guest_call_path` | Describes the kind's intended write boundary; it neither proves confinement nor authorizes reuse |
| Output declarations | `declared_outputs`, their media types, `outputs_named_at_call_time`, body-built `DeclaredOutput` | Each tool must satisfy its output capability and sink requirements; each call's collection is bounded by its spec |
| Tool wiring | `output_sink`, `source_integrity`, `standing_guidance`, `approval_mode`, the body and its docstring | Supplied separately for each tool, with independent attach and result validation |

`backend.acquire(key, spec)` receives the **full** `SandboxSpec`, including output declarations, transfer limits and the confinement flag. A field being read per call does not establish that differing sibling specs are compatible. The router checks routing constraints before acquire; `sandboxed_tool` also checks each tool's wiring before exposing it.

The route deserves its own line. `Selection.PER_SPEC` chooses a backend per spec. Two sibling specs whose `requires`, `min_isolation` or other routing constraints differ can still share an instance when both select the same backend and meet its reuse conditions. They can also select *different backends*, and the router then holds separate instance sets under the same key and kind. A shared kind does not refuse that split.

## What the three backends do when siblings disagree

This is where the prose rule and the code diverge. The following compares acquire paths when the same key and kind reach the same backend and an existing instance remains available for reuse. It is a source inspection of the three backends, not a live multi-tool experiment.

| Divergence between sibling specs | Docker | wslc | ACAS |
|---|---|---|---|
| Effective `egress` / `egress_allow` policy | partitions — `_container_name` folds an `_egress_id` into the container name | partitions, by the same naming rule | **refuses** — `_get_or_create` raises `AcasEgressPolicyConflict` when the usable held sandbox's egress differs |
| Effective `work_dir` | **refuses** a different or unrecorded storage base in `_verify_storage_base` | refuses, after the same label check | **refuses** — a held sandbox cannot change its storage base |
| Effective `image` / `image_id` | **no image comparison on reuse** — the image is not folded into the name | no image comparison on reuse | no image comparison on reuse — the held record tracks egress and work dir |

Two backends partition on egress, one refuses, and all three refuse a changed storage base on reuse. None compares the requested image with the held instance's image. When sibling pandoc specs differ only in image, both pass routing and the held guest passes their acquire-time checks, the second can be served inside the first image. That applies during overlapping calls or permitted warm reuse; a completed disposal instead makes the next acquire create from the second spec. The missing check is an image-consistency diagnostic, not a promise that every such acquire succeeds.

That asymmetry is defensible for egress, which is a containment claim the backends were built to enforce, and much less defensible for the image, which is where the whole failure mode of "the first spec decides" actually bites. The protocol asserts the rule in a docstring; the enforcement is partial and backend-specific.

## Two candidate workloads

**markitdown, inbound.** Converting PDF and Word *into* Markdown looks like a two-tool kind and is not one. `markitdown_pdf` and `markitdown_docx` would share the image, the argv, the output shape and the spec, and markitdown sniffs the input type itself — the split buys a longer tool list and nothing else. One `convert_to_markdown(file)` is the honest shape. A reason to use a separate kind would be markitdown's URL and Document-Intelligence converters: they reach the network, giving them a different `egress` posture from closed-network file conversion.

**pandoc, outbound.** Exporting Markdown to PDF and to Word is the candidate for two tools of one kind: one image containing both renderers, one work dir and `Egress.CLOSED`. The `DeclaredOutput` media type, guidance and description can be supplied separately. Different byte limits would also need both specs to pass routing and their shared-instance behavior to be tested. Sharing would still depend on effective scope, backend choice and cleanup policy.

## The split is a description problem, not a protocol one

Even in the pandoc case the second tool is not *required*. The diagram sample sets `outputs_named_at_call_time` and builds its `DeclaredOutput` — path, media type and landed name — inside the body, precisely because the path carries the call's own id. The same lever sets the media type from a `format` argument, so a single `export_markdown(file, format)` covers both formats under one spec with no protocol strain.

What two names buy is therefore not capability but *description*. The body's docstring is the model-facing tool description, passed through verbatim; a PDF export has engine and page-size caveats a docx export does not, and folding both into one description makes each one worse. Two names also give each format its own `standing_guidance`, its own `approval_mode`, and its own host-set confidentiality. Weigh that against a tool list that doubles for every format added — which is the argument for the enum, and it wins as soon as the only real difference is a flag.

As a proposed sharing rule: **agree on the effective image, work dir and egress policy, then expose as many names as the model benefits from.** Different instance requirements are a reason to use different kinds. Agreement is necessary for deliberate sharing, but the tools must also use the same effective key and selected backend; retaining an instance across calls additionally needs compatible cleanup policy. Different routing requirements alone do not require different kinds.

## What the second tool costs

- **Only the unconfigured-host gate is shared.** With the same absent or disabled router, each `sandboxed_tool` call returns `[]`. After that, each tool is validated independently: one sibling can attach while another raises for a missing `output_sink`, an invalid declaration or an unservable spec. A factory promising "both attach or neither" must enforce and test that property itself.
- **Diagnostics may identify only the workload.** Router refusals commonly name `spec.kind`, and the served kind is the observer's fallback when the framework names no tool. Those surfaces do not distinguish siblings, although per-tool attach checks can name the tool.
- **Disposal has no tool-name selector.** A `dispose_kind` sweep without `instance_id` reaches the kind's instances across backends for that key. Routine cleanup and an explicit `instance_id` can target one engine instance, preserving other instances of the same kind. Tools sharing that particular instance still cannot reset it independently.
- **The kinds index assumes one.** Its `Tool` column has one entry per row, and a multi-tool kind needs that shape changed rather than a comma-separated cell.

## What is not settled

- **Where image consistency should be enforced on reuse.** `sandboxed_tool` cannot see its siblings. A router-side record would need at least the effective key, kind and selected backend, and would also need to distinguish that backend's reuse partitions and retire bindings when instances are disposed or replaced. One last spec per key/kind would reject unrelated instances under `Selection.PER_SPEC` and could retain a stale constraint after disposal. Checking at each backend's reuse point is another candidate; this record does not choose between them.
- **Whether differing transfer limits and confinement descriptions are compatible in a multi-tool kind is untested.** The full spec reaches acquire, and `files_out` participates in routing limits and per-call collection. `confined_to_guest_call_path` describes intent; `established_cleanup` currently derives available cleanup operations from backend capabilities regardless of that flag, with host/spec floors deciding sufficiency. Neither observation proves safe sharing between different bodies. A multi-tool compatibility test needs to exercise both specs, their chosen route and their cleanup interaction.
- **Whether the guidance should discourage the split at all.** The inbound candidate favored one tool; the outbound candidate offers a choice between one tool and two names. A pattern whose best example is that narrow may belong in [`../kinds/writing-a-kind.md`](../kinds/writing-a-kind.md) as a caution rather than as a recipe.
