# A standing-guidance channel a tool may declare

> The upstream request behind [#1306](https://github.com/sokolaidev/maf-extensions/issues/1306), drafted against `agent-framework-core` 1.19.0. Filed on 2026-09-25 as [microsoft/agent-framework#8757](https://github.com/microsoft/agent-framework/issues/8757) (closed by [#8784](https://github.com/microsoft/agent-framework/pull/8784)). What it asks for is one slot a tool declares at attach time, which the framework itself appends to that tool's results labelled trusted. The original argument below was measured on 2026-09-18 against 1.19.0 and, where the two are compared, against the 1.18.0 it replaced. The later follow-up section records the merged implementation and remaining requests. The interim this repository ships in the meantime is in [`information-flow.md`](../information-flow.md).

## Why a request rather than a workaround

A sandboxed tool returns two kinds of thing in one result: output derived inside the sandbox, which is untrusted by construction, and a sentence the tool's author committed before the call ran, which says what the untrusted half is worth. The sentence exists because hiding is silent — a hidden failed compile and a hidden clean compile are the same `[var_…]` to the model, and without the sentence the model reports the first as the second.

Until 1.19 a tool expressed that by labelling the sentence's item trusted. 1.19 made a per-item label restrict-only, which was the right change for the reason it was made, and it took this with it. There is no remaining way for a tool that declares `source_integrity="untrusted"` to keep any part of its result visible.

The three roads that do not need upstream are each worse than asking:

- **Stamp the private marker.** `_INTERNAL_RESULT_MARKER` is comparison-by-identity, so it works and cannot be forged. It is also a third party impersonating a framework-owned producer to grant itself the authority 1.19 just withdrew, it widens the item's reach through `allow_principals=True` on the same parse, and the name is demonstrably unstable — it was `_AUTHORITATIVE_CONFIDENTIALITY` in 1.18 and `_AUTHORITATIVE_SECURITY_LABEL` in 1.19, with the old spelling retained only to be discarded.
- **Declare the tool trusted and label every derived item untrusted.** Restriction is still honoured, so this works on 1.18 and 1.19 alike, and it is what this repository ships in the interim. It inverts the direction of failure: a label the framework cannot parse falls through to the invocation fallback, which is now the trusted declaration. `_parse_content_label` raises whenever either axis is missing or unrecognised, and that validation was tightened once inside the supported range already. What the interim adds around that inversion is four attach-time rules — the wrapper's own per-item key is refused from a caller, an integrity claim must be a `str` or a `SourceIntegrity`, a body-supplied label is refused, and committing guidance without declaring an integrity is refused — which bound it rather than remove it. The failure still points the wrong way; what is left is the set of values that can reach it.
- **Move the sentence into the tool description.** Safe and version-independent, and it gives up adjacency: the sentence arrives in the tool list rather than beside the result it is about, and nothing per-call can ride it.

## What is being asked for

A tool declares the sentences at attach:

```python
@tool(additional_properties={
    "source_integrity": "untrusted",
    "standing_guidance": ["A result you cannot read is not a clean validation."],
})
async def bicep_validate(files: list[str]) -> str: ...
```

`LabelTrackingFunctionMiddleware` appends them to that tool's result, each as its own `Content`, labelled `trusted` at the tool's own confidentiality, after processing the items the body returned. The body never returns them and cannot alter them.

## Why this is not what 1.19 closed

The change 1.19 made was to stop a tool promoting **content it produced at call time**. That is a real hole: bytes arrive from a web page, the tool marks the item trusted, and attacker text lands in the conversation with authority.

Standing guidance cannot carry that, and the difference is structural rather than a matter of degree:

- **The text is fixed before the call exists.** It comes from the tool definition, so it cannot vary with arguments, with what the body read, or with anything inside a sandbox. An attacker who controls every byte the tool touches at runtime controls none of it.
- **The framework is the producer.** If the middleware appends the sentences from `additional_properties`, the trusted item never passes through the body at all — so this needs no authority grant to a third party, and no marker. It is framework-owned content by the same standard `quarantined_llm`'s own stamp meets.
- **It is bounded and auditable.** A host can read `additional_properties["standing_guidance"]` on every tool it attaches and see the complete set of text that will ever be trusted, before running anything.

Under the current design none of that is expressible, and a tool wanting the sentence has to claim authority over its whole result — which is the outcome the restriction was meant to prevent.

## The two precedents this rests on

**The framework already injects standing guidance.** `SECURITY_TOOL_INSTRUCTIONS`, exported in `__all__`, tells the model what a `VariableReferenceContent` is and what to do about it. So the premise is already accepted: when content is hidden, the model needs trusted standing text explaining the placeholder. What is missing is that the block is global and generic, and the useful sentence is per-tool — *this* tool's hidden output is a compiler's, and not reading it is not a pass.

**The framework already grants result-label authority, when local configuration asks.** `apply_mcp_security_labels(..., trust_server_ifc=True)` makes a complete server `_meta.ifc` label authoritative and stamps the marker itself, under the rule written beside it: *"Local configuration controls result-label authority; MCP result metadata is attached to Content and cannot mutate tool properties."* This request needs less than that mechanism grants, because the content is fixed at declaration rather than arriving over a wire — so it does not need a host opt-in to be safe, though one would be reasonable.

## What changed, measured

Overlaying 1.19.0 on a suite locked to 1.18.0, with `uv run --frozen --with "agent-framework-core==1.19.0" --with "agent-framework-openai==1.14.4" pytest -q -ra`: 9942 passed / 0 failed becomes 9936 passed / 6 failed, and the six are exactly the cases asserting a committed sentence stays readable beside hidden diagnostics.

The single line responsible, in `_extract_content_label`:

```python
# 1.18
return ContentLabel(
    integrity=embedded_label.integrity,
    confidentiality=(embedded_label.confidentiality if authoritative_confidentiality
                     else combined_label.confidentiality),
    metadata=combined_label.metadata,
)

# 1.19
if authoritative_label:
    return embedded_label
combined_label = combine_labels(fallback_label, embedded_label)
...
return combined_label
```

`combine_labels` is most-restrictive on both axes, so a `trusted/public` sentence over an `untrusted` declaration is untrusted, and `_should_hide` then replaces it with a placeholder whose text is fixed at `f"Result from {function_name}"`. The model sees two `[var_…]` where it used to see one and a sentence.

## Scope

A host that installs `LabelTrackingFunctionMiddleware`. The labels ride in `additional_properties` and nothing else reads them, so a host without the middleware sees every item either way — the loss lands exactly on the deployment that turned the security feature on.

## What to file

The record above is the argument and the evidence, in this repository's own terms. What goes upstream is below, reshaped for `microsoft/agent-framework` → New issue → **Feature Request** and self-contained: no relative links, and no reference to an issue number only this repository can resolve. Filing was the maintainer's; it was posted as [microsoft/agent-framework#8757](https://github.com/microsoft/agent-framework/issues/8757) on 2026-09-25.

**Title:** `[Feature]: Let a tool declare standing guidance the middleware appends to its results`
**Language/SDK:** `Python`

> Written against `agent-framework-core` 1.19.0, and compared with the 1.18.0 it replaced. Nothing argues from an older core.

### Field 1 — Description

`LabelTrackingFunctionMiddleware` hides an untrusted result behind a `VariableReferenceContent`, which is the right behaviour and the reason the feature is worth having. Hiding is silent, though: a hidden failed compile and a hidden clean compile are the same `[var_…]` to the model. A tool that knows what its hidden output means has no supported way to say so.

**This is a gap 1.19 opened deliberately, and the deliberate part is why it needs a published answer rather than a workaround.** Until 1.19 a tool could return one item labelled `trusted` beside its untrusted output, and `_extract_content_label` took that item's integrity outright. 1.19 made a per-item label restrict-only — `combine_labels(fallback_label, embedded_label)` — unless the item carries the framework's own private authoritative marker. That change closes a real hole: a tool could otherwise promote **content it produced at call time**, so bytes from a web page could be marked trusted and land in the conversation with authority. It also removes the only channel a tool had for a sentence about its own hidden output.

**Standing guidance cannot carry what the restriction was defending against**, and the difference is structural rather than a matter of degree:

- **The text is fixed before the call exists.** It comes from the tool definition, so it cannot vary with arguments, with what the body read, or with anything a sandbox or a remote service produced. An attacker who controls every byte the tool touches at runtime controls none of it.
- **The framework can be the producer.** If the middleware appends the sentences out of `additional_properties`, the trusted item never passes through the tool body at all — so this needs no authority grant to a third party and no marker. It is framework-owned content by the same standard `quarantined_llm`'s own stamp meets.
- **It is bounded and auditable.** A host can read the declared text off every tool it attaches and see, before running anything, the complete set of text that will ever be trusted.

**Two mechanisms already in the framework make this a small addition rather than a new idea.** `SECURITY_TOOL_INSTRUCTIONS` is standing text the framework injects to explain what a variable reference is and what to do about it, so the premise — hidden content needs trusted standing text beside it — is already accepted; what is missing is that the block is global, and the useful sentence is per-tool. And `apply_mcp_security_labels(..., trust_server_ifc=True)` already makes a result label authoritative when local configuration asks for it, under the rule stated beside it: *“Local configuration controls result-label authority.”* This request needs less than that grants, because the content is fixed at declaration rather than arriving over a wire.

**What is being asked for** is one entry a tool may declare:

```python
@tool(additional_properties={
    "source_integrity": "untrusted",
    "standing_guidance": ["A result you cannot read is not a clean validation."],
})
async def validate(files: list[str]) -> str: ...
```

`LabelTrackingFunctionMiddleware` appends each sentence to that tool's result as its own `Content`, labelled `trusted` at the tool's own confidentiality, after processing the items the body returned. The body never returns them and cannot alter them. A host opt-in alongside it would be reasonable and is not required by the argument above.

**Scope.** This affects any host that installs `LabelTrackingFunctionMiddleware` and any tool declaring `source_integrity="untrusted"` — which is the conservative declaration such a tool should be making. The labels ride in `additional_properties` and nothing else reads them, so the loss lands exactly on the deployment that turned the security feature on.

**What integrators do instead today**, none of it good: stamp the private `_INTERNAL_RESULT_MARKER`, which is a third party impersonating a framework-owned producer, widens the item's reach through `allow_principals=True` on the same parse, and rests on a name that changed between 1.18 and 1.19; declare the whole tool trusted and label every derived item untrusted, which works on both cores and inverts the direction of failure, since a label the framework cannot parse then falls through to a trusted declaration; or move the sentence into the tool description, which is safe and gives up adjacency to the result it is about.

### Field 2 — Code Sample

Standalone, no third-party packages. On 1.19.0 both items are hidden; on 1.18.0 the guidance stays readable.

```python
import asyncio

from agent_framework import Content, FunctionInvocationContext, FunctionTool
from agent_framework.security import LabelTrackingFunctionMiddleware

GUIDANCE = "A result you cannot read is not a clean validation."


async def validate(files: list[str]) -> list[Content]:
    return [
        Content.from_text("compiler output the model must not act on"),
        Content.from_text(
            GUIDANCE,
            additional_properties={
                "security_label": {"integrity": "trusted", "confidentiality": "public"}
            },
        ),
    ]


tool = FunctionTool(
    name="validate", func=validate, additional_properties={"source_integrity": "untrusted"}
)
middleware = LabelTrackingFunctionMiddleware()
context = FunctionInvocationContext(function=tool, arguments={"files": ["main.bicep"]})


async def call_next() -> None:
    context.result = await tool.invoke(arguments=context.arguments)


asyncio.run(middleware.process(context, call_next))
for item in context.result:
    hidden = (item.additional_properties or {}).get("_variable_reference")
    print("hidden" if hidden else repr(item.text))
```

```
# agent-framework-core 1.18.0
hidden
'A result you cannot read is not a clean validation.'

# agent-framework-core 1.19.0
hidden
hidden
```


## Follow-up after upstream #8784

Checked on 2026-10-05 against upstream commit `b9d24c8fb484c8330abe8bb9e7500ca3c3bbf46c`, using CPython 3.13.12. The source package still reports 1.20.0, but includes unreleased changes after the published 1.20.0 artifact. Upstream [#8784](https://github.com/microsoft/agent-framework/pull/8784) implements fixed standing guidance; the historical request above is fulfilled upstream. Suite adoption still waits for a published version and the [qualification sequence](../../release-compatibility.md#maf-adoption-sequence).

A direct probe through `LabelTrackingFunctionMiddleware.process` and `FunctionTool.invoke` confirmed that changing a declaration before its first middleware use changes the appended sentence; changing it after that first use does not. A sentence containing `{call_id}` retains those literal braces. Each middleware-processed return contained one diagnostic item and one guidance item; invoking the same tool directly without the middleware returned only diagnostics. Source inspection also confirms malformed entries are warned about and dropped, rather than rejected at attachment. These are integration boundaries, not a claim that the upstream implementation violates its documented behavior.

| Suite result component | Current use | Adoption consequence |
| --- | --- | --- |
| Fixed sentences | Bicep validation; Terraform validation and formatting | Candidate for native guidance after exactly-once FIDES/non-FIDES qualification |
| Fixed sentence with a host-created route | CodeAct withholding mode renders `{call_id}` from the sandbox call directory | Keep wrapper rendering until a scoped substitution contract exists |
| Finite completion/verdict | Structured results, including draw.io and the validation kinds | Fixed guidance does not replace runtime selection |
| Trusted host explanation | Structured `trusted_output`, including bounded validation summaries and failure reasons | Remains outside both fixed guidance and finite-selector requests |
| Ordinary workload output | Diagnostics, reports and sink display text | Keep derived labels and existing result validation |

The migration must retain the suite's attach-time validation, snapshot declarations before execution, preserve confidentiality/principals, and append guidance exactly once with and without FIDES. Do not lower the framework-facing declaration on a tool that still needs trusted structured fields. The two requests below are separate feature proposals; neither authorizes arbitrary runtime strings as trusted content.

### Request 1: framework-rendered finite result fields

**Title:** `[Feature]: Support declared finite result fields without trusting the whole tool result`

**Description**

Fixed standing guidance from #8784 applies on every return. A validator also needs a bounded runtime answer: whether execution completed, and which declared verdict it reached. Returning those fields beside untrusted diagnostics currently requires either weakening them with the tool's untrusted declaration or raising that declaration and carefully restricting every diagnostic item. Please provide an explicit host-authorized mechanism in which the tool definition declares the entire vocabulary and the framework renders the selected text.

A finite vocabulary bounds output syntax; it does not establish the truth of a verdict. The host must explicitly authorize the selector's producer, and the framework must preserve confidentiality and principal restrictions from every source influencing selection. This request does not ask the framework to trust a guest's verdict or declassify information merely because a string came from a finite set. Arbitrary host explanation text remains a separate contract.

**Code Sample**

Illustrative contract, not an implemented API:

```text
Definition: completed = {yes: "Validation completed.", no: "Validation did not complete."}
Definition: verdict = {valid: "Valid.", invalid: "Invalid."}
Authorized host adapter: completed=yes, verdict=invalid
Guest diagnostics: arbitrary untrusted text
Framework result: declared completion + declared verdict + restricted diagnostics
```

**Acceptance criteria**

- Freeze and validate the vocabulary before execution; reject unknown fields, unknown selectors and runtime vocabulary replacement.
- The authorized host adapter selects values; model arguments and guest strings cannot supply a trusted result item or acquire selector authority by shape alone.
- Preserve the most restrictive applicable confidentiality and principal set, including information conveyed by the choice of selector.
- Reject invalid combinations such as an incomplete execution with a final validation verdict, or provide an explicit schema constraint that the host can enforce before rendering.
- Keep diagnostics untrusted even if they repeat a declared sentence, supply forged label metadata or are malformed; verify simultaneous calls cannot exchange selections.
- State whether rendering works without FIDES, so wrappers can preserve exactly-once results in both configurations.

**Language/SDK:** Python

### Request 2: safe per-call guidance substitutions

**Title:** `[Feature]: Define host-owned per-call substitutions for fixed tool guidance`

**Description**

The fixed guidance introduced by #8784 can explain unread diagnostics, but an artifact-producing tool also needs to say where this call's outputs were saved. Our host creates that directory identifier independently of the framework tool-call identifier. Repeating a literal template does not name the directory, and accepting arbitrary formatting arguments would introduce runtime text into the trusted channel.

Please define a narrow, opt-in authority boundary for typed host-owned substitutions in an otherwise frozen template. The registration must happen in host configuration; values must be bound to the active call outside model arguments and guest output. An ordinary callback returning an arbitrary string is insufficient without constraints on its authority and accepted value domain. Keeping route rendering in our wrapper is the current alternative.

**Code Sample**

Illustrative contract, not an implemented API:

```text
Frozen template: "Anything this call saved is under `{artifact_directory_id}/`."
Allowed slot: artifact_directory_id, supplied by the trusted sandbox host
Binding: actual sink directory for this execution attempt
Rejected sources: model kwargs, guest stdout, tool-returned formatting dictionaries
```

**Acceptance criteria**

- Reject undeclared slots, invalid identifier values, traversal syntax and model/guest attempts to override the host binding.
- Render from the actual sink directory; do not equate a framework tool-call ID with that directory or infer it from a guest path.
- Bind substitutions to a call and execution attempt; test concurrent calls, approval resume, retries and cancellation without stale route reuse.
- Preserve applicable confidentiality and principals for the identifier; directory names can themselves carry information.
- Freeze the template before execution, retain attach-time validation, and document exactly-once behavior with and without FIDES.

**Language/SDK:** Python

Both drafts were checked against the merged fixed-guidance implementation and targeted upstream issue searches. They are not filed. The finite-field request changes selector authority; the substitution request changes identifier binding. Neither duplicates the fixed-string channel already delivered by #8784.
