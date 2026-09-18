# A standing-guidance channel a tool may declare

> The upstream request behind [#1306](https://github.com/sokolaidev/maf-extensions/issues/1306), drafted against `agent-framework-core` 1.19.0 and not yet filed. What it asks for is one slot a tool declares at attach time, which the framework itself appends to that tool's results labelled trusted. Everything below was measured on 2026-09-18 against 1.19.0 and, where the two are compared, against the 1.18.0 it replaced; nothing argues from a core older than the range this suite admits. The interim this repository ships while the request is open is in [`information-flow.md`](../information-flow.md).

## Why a request rather than a workaround

A sandboxed tool returns two kinds of thing in one result: output derived inside the sandbox, which is untrusted by construction, and a sentence the tool's author committed before the call ran, which says what the untrusted half is worth. The sentence exists because hiding is silent — a hidden failed compile and a hidden clean compile are the same `[var_…]` to the model, and without the sentence the model reports the first as the second.

Until 1.19 a tool expressed that by labelling the sentence's item trusted. 1.19 made a per-item label restrict-only, which was the right change for the reason it was made, and it took this with it. There is no remaining way for a tool that declares `source_integrity="untrusted"` to keep any part of its result visible.

The three roads that do not need upstream are each worse than asking:

- **Stamp the private marker.** `_INTERNAL_RESULT_MARKER` is comparison-by-identity, so it works and cannot be forged. It is also a third party impersonating a framework-owned producer to grant itself the authority 1.19 just withdrew, it widens the item's reach through `allow_principals=True` on the same parse, and the name is demonstrably unstable — it was `_AUTHORITATIVE_CONFIDENTIALITY` in 1.18 and `_AUTHORITATIVE_SECURITY_LABEL` in 1.19, with the old spelling retained only to be discarded.
- **Declare the tool trusted and label every derived item untrusted.** Restriction is still honoured, so this works on 1.18 and 1.19 alike, and it is what this repository ships in the interim. It inverts the direction of failure: a label the framework cannot parse falls through to the invocation fallback, which is now the trusted declaration. `_parse_content_label` raises whenever either axis is missing or unrecognised, and that validation was tightened once inside the supported range already.
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
