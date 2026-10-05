# Finite result fields and host-owned artifact routes

> Two unresolved upstream proposals: framework-rendered finite result fields and host-owned substitutions in fixed guidance. These requests extend the fixed-string channel argued for in [the original guidance record](upstream-standing-guidance-channel.md); they do not rewrite that completed proposal. The [adoption boundary](../../release-compatibility.md#native-guidance-adoption-boundary) records the measured upstream behavior and current migration constraints.

Prepared on 2026-10-05 against upstream source commit `b9d24c8fb484c8330abe8bb9e7500ca3c3bbf46c`. The requests below are separate feature proposals; neither authorizes arbitrary runtime strings as trusted content.

## Request 1: framework-rendered finite result fields


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

## Request 2: safe per-call guidance substitutions

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
