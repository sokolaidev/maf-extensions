# Information flow

Information-flow labels control which tool results the model can read and where data can go. A sandbox limits what a program can do. Labels control how the agent uses its answer.

This design uses MAF's information-flow module, FIDES, from `agent-framework-core>=1.19.0,<1.20`. A **kind** is a sandbox workload exposed as a tool, such as a compiler or code runner.

## Tools, content and the model

A **source tool** returns data to the agent. A **destination tool** sends data somewhere, such as a file or a service. One tool can do both.

![Source tools declare the integrity and confidentiality of their results. Each returned content item has its own effective label. The framework shows trusted text to the model and can hide untrusted text behind a variable reference. Visible items affect the conversation's integrity; hidden items still affect its confidentiality. The model's next tool call passes through a policy check using conversation and argument labels. Destination tools declare whether they accept untrusted input and the highest confidentiality they accept.](assets/information-flow.svg)

Tool declarations and content labels serve different purposes:

| Where | Property | Meaning |
|---|---|---|
| Source tool | `source_integrity` | Integrity claimed for its results |
| Source tool | `confidentiality` | Host classification of its results |
| Content item | `security_label` | Integrity and confidentiality of this item; can restrict the call's label |
| Destination tool | `accepts_untrusted` | Opt-in to calls with untrusted conversation content or arguments |
| Destination tool | `max_allowed_confidentiality` | Highest confidentiality the destination accepts |

The host configures destination policy. Accepting untrusted input does not bypass confidentiality checks. See [host configuration](hosts.md) for file outputs, network access and host tools.

## What the framework tracks

Every label has two parts:

- **Integrity:** `trusted` or `untrusted`. This describes the source of the content, not whether the content is correct.
- **Confidentiality:** who may receive the content. Values include `public`, `private` and `user_identity`. The host supplies the classification; this suite does not rank those values itself.

Labels combine by keeping the more restrictive value in each part. One untrusted source makes the combined integrity untrusted.

The framework also tracks a conversation label. Once the model sees untrusted content, the conversation stays untrusted. Later tool calls are subject to the host's policy for that label.

The model's own output has no source label. Tool policy uses the conversation and argument labels.

### Where a label comes from

1. The framework uses the tool's `source_integrity` declaration when one is present.
2. Without that declaration, it uses expanded references' stored labels, or the host's `default_integrity` when there are none. Argument labels can only make this more restrictive.
3. A valid `security_label` on a returned item can further restrict the call's label. It cannot make an untrusted call's result trusted.

A tool's integrity declaration replaces the input-based calculation. Confidentiality works differently: the framework combines the tool's classification with argument confidentiality. Declaring `public` cannot make a private argument public.

The default integrity is `untrusted`, but the host can change it. Omitting a declaration therefore delegates the decision to the host.

## The result contract

A kind returns a `SandboxResult` with four fields. Each answers a different question, so the model can understand the outcome while workload text stays hidden.

| Field | Question it answers | Returned content | Integrity for sandbox workloads |
|---|---|---|---|
| `completed` | Did the workload reach a definitive answer? | One fixed sentence | Trusted |
| `verdict` | What was the answer? | One value from the tool's declared set, or no item for `None` | Trusted |
| `trusted_output` | What text can the kind vouch for? | One item per string; may be empty | Trusted |
| `output` | What did the workload produce? | One item per string; may be empty | Untrusted |

![A kind returns SandboxResult to sandboxed_tool, which builds separate Content items. Completed gives a fixed completion sentence. Verdict gives a declared answer. Trusted output gives text the kind vouches for. These items inherit trusted integrity and the call's confidentiality, so the model can read them. Workload output receives an untrusted label and becomes a hidden variable reference. The diagram assumes an untrusted workload declaration, automatic hiding enabled and a still-trusted conversation.](assets/result-contract.svg)

**Four fields does not mean exactly four items.** The verdict is optional, and each output string becomes its own `Content` item. The wrapper returns them in the order shown above. Committed standing guidance, if present, comes last.

Labels apply to whole content items. Putting all four fields in one JSON string would give them one label and lose the separation.

### Completion and verdict

`completed=True` means the workload reached an answer. It does not mean the answer was a pass. For example, a validator can complete with the verdict `invalid`.

`completed=False` means there is no definitive answer. It cannot be combined with a verdict. An exception still goes through framework error handling, outside this result shape.

The tool lists its allowed verdicts when it is attached to the agent. Values must be nonblank strings, integers or Booleans. They must have distinct text forms: `1` and `"1"` cannot both be declared.

The wrapper checks a returned verdict by type and text. Declaring `0` does not allow `False`, even though Python compares them as equal.

### Trusted text and workload output

`trusted_output` is a claim made by the kind author. The wrapper does not prove that the text is safe to trust. Put text whose sources are uncertain in `output`.

Sandbox diagnostics, guest text and provider reports belong in `output`. Their integrity is `untrusted`. A custom tool with a justified `trusted` source declaration can retain trusted output, subject to the [file-read checks](#how-core-labels-a-call).

### How the wrapper labels the fields

Enable the contract with `result_contract=True` and declare the allowed `verdicts=(...)`. The body returns a `SandboxResult`, with strings rather than labelled `Content` objects. An integrity declaration is required.

The wrapper exposes `source_integrity="trusted"` to the framework. It preserves the kind's workload claim separately as `maf_sandbox_derived_integrity`.

- `completed`, `verdict` and `trusted_output` have no wrapper-written label. They inherit trusted integrity and the call's confidentiality.
- `output` items receive a complete label based on the workload claim and file-read checks. The wrapper uses the host's confidentiality, or `public` if none is valid.
- The framework keeps any stricter confidentiality from the call. A `public` stamp never lowers a private result.

This arrangement lets the wrapper restrict workload output while keeping the other fields readable. A tool that keeps an untrusted framework declaration cannot make selected items trusted.

Returning a `SandboxResult` without opting in is refused. So is returning another shape after opting in. [Status](#status) lists implementation coverage.

## When the model sees untrusted output

FIDES hides an untrusted item only when all these conditions hold:

- `auto_hide_untrusted` is enabled.
- The conversation is still trusted.
- The tool is not `inspect_variable`.

![An untrusted result is hidden only when auto-hide is enabled, the conversation is still trusted and the tool is not inspect_variable. The model then sees a variable reference and the conversation stays trusted. Otherwise the model reads the text, the conversation becomes or stays untrusted, and later untrusted output is visible too. Both hidden and visible items still contribute confidentiality.](assets/untrusted-output-visibility.svg)

The model receives a reference such as `[var_…]` instead of the text. It can pass that reference to another tool, subject to the host's policy. Expanding the reference restores its stored label.

Hidden content does not make the conversation's integrity untrusted. Its confidentiality still counts and can block a destination.

If hiding is disabled, or the conversation is already untrusted, the model sees the text. Hiding is therefore conditional; a kind cannot promise that its output always stays hidden.

## The rule

**A kind may claim trusted content only when every source that affects it is established as trusted, or contributes nothing to it.** Fixed choices follow the rule in the next section.

Check both the program that emits the result and the data it reads. Sources include sandbox programs, files, network responses and host-tool results.

Being first-party or deterministic does not make a compiler's diagnostics trusted. Formatting guest text in package code does not make that text trusted either. Who typed the input is not the test.

Use an explicit `untrusted` declaration when a result can contain content from an untrusted or unknown source. Do not rely on argument labels or a host default to cover sources they cannot see.

At attachment, core rejects a trusted workload declaration if the spec opens a source channel that is not established as trusted. A kind can declare that nothing from a channel affects its result. The author must justify that claim.

### Selection is not authorship

A source may select a trusted verdict from values the kind author fixed in advance. It cannot supply new text through that choice.

All four conditions apply:

1. List the complete set when the tool is attached. A Boolean or a small verdict set qualifies. Arbitrary sizes, durations, paths and counts do not.
2. Return the kind's own constant for the selected case, not the guest string that matched it.
3. Vary only the choice. Do not add source text to it.
4. Consider repeated calls. Even one bit per call can reveal substantial information over a conversation. The wrapper does not limit that total.

Use the result contract for these values. A trusted verdict still carries the call's confidentiality.

## One result, two labels

**Standing guidance** is fixed advice the kind declares with `standing_guidance=(...)`. Its text and presence must be independent of untrusted or unknown sources. It must apply on every normal return, including returned failures.

Standing guidance requires an integrity declaration, just as the result contract does.

Advice shown only on failure is not standing guidance. Report the failure in `completed` or `verdict`. Put variable diagnostics in `output`.

With the result contract, the wrapper appends guidance itself. With a text-item result, the body returns at least one workload item followed by the exact guidance in order. The wrapper checks that suffix and rebuilds it from the declaration.

The wrapper labels guidance `trusted/public`. The framework still applies the call's stricter confidentiality. Only `{call_id}` may vary; core supplies that identifier, and it requires an async body with an active call.

Bodies cannot supply their own `security_label`. A duplicate guidance sentence outside the required suffix remains workload output.

### Why confidentiality changes the design

A valid `security_label` contains both integrity and confidentiality. An integrity-only label is discarded, and the item falls back to the call's label.

For contract tools and tools with standing guidance, the wrapper must label workload items. Otherwise they would inherit the raised trusted declaration. When the host supplies no valid confidentiality, the wrapper uses `public` as a floor.

Other tools receive wrapper-written result labels only when both `source_integrity` and the host's result `confidentiality` are valid. Otherwise the framework resolves the label. An outbound confidentiality cap is not a result classification.

## How core labels a call

The wrapper can lower workload integrity after a file read. It never raises it because a file was trusted.

1. The host lists files as `ListedFile(name, integrity)`.
2. The kind reads a selected entry through `SandboxToolSession.read_file`.
3. The session checks its source record before and after the read. A changed record makes integrity unknown.
4. Each successful read contributes to the call's `FedFromStore` record. Unknown or untrusted integrity weakens the call's workload items.

Empty files count as successful reads. Missing or refused reads do not. Without a session source record, only the listing's evidence is available.

![The host lists files with their integrity. The session checks the listing against its source record before and after a read; a changed record makes integrity unknown. Accepted reads accumulate in this call's FedFromStore record. When the wrapper writes labels, any unknown or untrusted read makes every workload item untrusted. Other reads preserve the kind's claim. The contract's first three fields and standing guidance are unaffected, and the host's result confidentiality is preserved.](assets/file-read-labels.svg)

When the wrapper writes labels, and the host classifies results as `private`:

| Kind's workload claim | Files read in this call | Workload-item label |
|---|---|---|
| `trusted` | None, or all trusted | `trusted/private` |
| `trusted` | Any untrusted or unknown | `untrusted/private` |
| `untrusted` | Any, or none | `untrusted/private` |

One weak read affects every workload item in the call. It does not lower the contract's first three fields or standing guidance. Each call has its own read record; the attached tool declaration is unchanged.

These checks cover `SandboxToolSession.read_file`. They do not cover direct store reads, network responses or host-tool results. The kind must account for those sources separately.

`nothing_survives_from=(SourceChannel.FILE_STORE,)` states that file content does not affect the result. It does not bypass the read checks or prove that claim.

Only returned values receive wrapper labels. Keep raised exception text free of guest content and hidden arguments.

See [host file provenance](hosts.md#file-store-provenance--what-a-kind-reads-and-what-it-is-worth) for source records and their timing limits. See [result classification](hosts.md#classify-derived-tool-results) for host setup.

## What labels say about each source

| Source | What the kind must account for |
|---|---|
| Plain argument | A string has no label just because it is an argument |
| Expanded hidden reference | Its stored label follows the expanded content |
| File-store content | A file name does not label the file's contents; use the host's source record |
| Network response | An allowed hostname does not establish response integrity |
| Host-tool result | Read `HostToolAggregate.result_integrity`; it covers the registered sources only |
| Sandbox program | The program emitting the bytes is itself a source |

`AgentFileStore` stores plain text without labels. A file-write tool can receive an expanded hidden reference and write those bytes into the store. The host must track file origins separately, using the same record for listing and reading.

### The call arguments have already been rewritten

The framework expands hidden references before the body runs. A file name argument may therefore contain hidden text. Its file-integrity label does not say whether that name may be shown.

![A hidden reference expands into a tool argument with its stored label before the body runs. Host policy checks whether the call may proceed. If allowed, a file-write tool stores the expanded bytes as plain text without labels. Host middleware separately records the write as untrusted when the call exits. Later reads combine the file text with that integrity record; result confidentiality remains a separate host setting. Argument provenance middleware identifies changed positions so hidden names can be reported by position. Writes still in flight may not yet be recorded.](assets/hidden-reference-storage.svg)

With `argument_provenance_middleware`, a kind can check which argument positions changed. Without it, `positions_holding_hidden_content` compares against stored payloads and can report extra matches. Ask before host code can change the hidden-content store, then use `echoed_name` to render a safe name or position.

The middleware uses a private framework record, guarded by upgrade tests. If tracking is active but that record is missing, the helper warns and treats every queried position as possibly rewritten.

The host's policy normally blocks expanded untrusted arguments. Host approval settings, logging-only settings or an integrity opt-in can allow them through. Confidentiality checks still apply. Report positions rather than echoing possibly hidden values.

## What the shipped kinds declare

All four kinds claim `untrusted` for workload output:

| Kind | Sources that require this claim |
|---|---|
| [`bicep`](kinds/bicep.md) | Compiler diagnostics, stored templates and restore responses |
| [`codeact`](kinds/codeact.md) | Guest programs, stored files, network responses and host-tool results |
| [`terraform`](kinds/terraform.md) | Provider reports and stored configuration |
| `drawio` | Expanded XML arguments and Graphviz layout output |

For tools with standing guidance, read the workload claim from `maf_sandbox_derived_integrity`. Their framework-facing `source_integrity` is raised to keep guidance readable.

## Status

| Decision | State | Tracking |
|---|---|---|
| Wrapper owns labels, checks file reads and preserves standing guidance | Implemented | [Kind-authoring guide](kinds/writing-a-kind.md) and [host configuration](hosts.md) |
| Four-field `SandboxResult` | Implemented in `maf-sandbox`; opt-in with `result_contract=True` | [Kind-authoring guide](kinds/writing-a-kind.md) |
| Use the result contract in all four kinds, samples and live checks | Partially implemented: Terraform/OpenTofu tools and their live tests use the contract; adoption by the other kinds, samples and their live checks remains open | Terraform/OpenTofu by [#1367](https://github.com/sokolaidev/maf-extensions/pull/1367) (merged); remaining adoption in [#1357](https://github.com/sokolaidev/maf-extensions/issues/1357) (open) |
