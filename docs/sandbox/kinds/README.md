# Kinds

> What a *kind* is: a workload written against the sandbox protocol and nothing else, the pattern the first one set, and how a spec grows from what the kind declares. Sources of record: [`../research/sandbox-architecture.md`](../research/sandbox-architecture.md) and [`../research/files-out.md`](../research/files-out.md).

**These pages own the architecture-facing contract of each kind — the spec it declares and why, its security pattern, its portability story. Each package's own README owns install and usage, and is linked rather than duplicated.** Someone deciding whether a kind fits a deployment reads here; someone wiring it up reads there. Where the two would say the same thing, the package README is the one that gets to say it, because it ships with the code.

Start with [writing a kind](writing-a-kind.md) to build one. It contains a complete JSON-checking tool, host wiring, the result-label contract, and a verification checklist. This index describes the common architectural rules; [information flow](../information-flow.md#how-core-labels-a-call) explains why the label design has this shape.

## A kind is a workload, never a vendor and never a backend

A kind is one thing: a tool factory that asks a `SandboxRouter` for a sandbox and gets back `write_file`, `exec` and the pull surface. It names no backend, imports no provider SDK, and contains no lifecycle code — acquiring, keying, disposing and confining egress are all [`../architecture.md`](../architecture.md)'s, and reclaiming a call's files is [`../tool-call.md`](../tool-call.md)'s, written once. What is left over is what is genuinely workload-specific: the command templates, the accepted inputs, the parsing of what comes back, and the hosts this particular work needs to reach.

That division is a portability claim, and it is test-enforced rather than asserted:

| Test | Where it lives | What it pins |
|---|---|---|
| `TestZeroDependencies` | `packages/maf-sandbox/tests/test_sandbox_router.py` | the protocol modules import nothing outside the standard library. Scoped to `_PROTOCOL_MODULES`, not the whole distribution, because the dist does declare `agent-framework-core` for `maf_sandbox.maf` — a scan that kept claiming "nothing here imports anything" would have had to be deleted rather than narrowed |
| `TestNoDirectAzureImport` | each kind's own suite | no `import azure` anywhere under the package. Strictly redundant with the row below, and kept anyway: its failure message names the property that actually broke — the workload reaching around `maf_sandbox` for a provider — where "undeclared dependency" would not |
| `TestOnlyDeclaredDependencies` | every package | every import is one the package's own `pyproject.toml` declares. This is the defect class that otherwise first reproduces on a clean install, where the workspace is no longer there to satisfy it |

The direction of the boundary matters as much as its existence: **kinds and backends never import each other**, in either direction. A kind that reached for a provider would stop being portable; core reaching for a kind would make the protocol a registry of workloads. Both talk only to the router in the middle.

The payoff is one sentence per kind: the same tool runs unchanged on ACA Sandboxes, a Docker container, a WSL container or an in-process fake ([`../backends/README.md`](../backends/README.md)) — and a backend that cannot serve it is refused at attach, not at first call.

## The pattern the first kind set

`bicep_validate` was written first, against real infrastructure code an agent wrote, and the shape it settled on is what every later kind follows.

- **Fixed command templates, with nothing but a validated path interpolated.** No agent-authored text reaches a command line. Where a kind can use an argv sequence it does, and the backend quotes it; where it genuinely needs a shell line — `|| true`, a redirection — the template is a module-level constant and the one `{path}` in it has already been through validation.
- **The caller's file listing is the injection pin, and now also the label channel.** Only a name present in `CallerContext.list_files` is ever substituted, so a name the model invented, or read out of a poisoned file, has nowhere to go. A failure to enumerate is a *refusal*, never an empty listing: empty would look like "the store has no files" and refuse every name individually with the wrong reason.
- **Sanitized error surfaces.** Provider and transport text can carry endpoint, subscription and tenant ids, and a tool result is persisted into a transcript. That detail goes to the log; the model gets a fixed sentence. What this stack authored itself is safe to surface verbatim, and is.
- **One egress mode, chosen inside the set the kind accepts.** A spec carries a single `Egress` mode — `CLOSED` by default, so a kind that says nothing about the network gets none — and `egress_allow` is the payload of an `ALLOWLIST` run rather than a field with a life of its own; naming hosts in any other mode is refused where it is written. Each kind guards the set of modes it will accept **at construction**, so the posture a deployment may choose is bounded by the kind rather than by the backend it happens to have wired. What the *kind itself* needs to function stays fixed in the package — bicep's four hosts are the kind's, not a deployment's — because a deployment able to widen that could undo the containment the design rests on; where a kind lets a deployment add hosts of its own (codeact does), they are added to the kind's half and never in place of it. The router then serves that exact mode on a backend that enforces it, or refuses at attach — never a more open substitute, which would silently widen what the workload reaches, and never a more isolated one, which would hand it a posture it was not built for. See [`../network.md`](../network.md).
- **T2, not T0 — and a degrade that says so.** The point of running the work is that a compiler, an interpreter or a test runner answers instead of the model checking its own output; a model that reads its own work and agrees with itself has added no information. Every degrade path therefore returns the run to T0 *visibly*: an unconfigured host attaches no tool at all (the agent keeps the ungrounded behaviour it already had, and is never shown a capability it lacks), while a host whose backend cannot honour the spec **raises** — nothing-configured is a choice, can't-confine is a misconfiguration, and quietly shipping the workload without its containment is the one outcome not on offer.

## The spec is where the posture questions are answered

Everything a host needs to decide about a kind is in its `SandboxSpec`, which is why each page below leads with one. `kind` names the workload and is half of a sandbox's identity; `egress` says which of three network postures it runs in and `egress_allow` names the hosts when that posture is `ALLOWLIST`; `requires` says what it cannot run without; `requires_os_family` says what shape of guest its commands are written for, and both shipped kinds leave it `None`, which asks nothing and is refused by nothing; `min_isolation` says whether it raises the host's floor, and most kinds should not ([`../policy-isolation.md`](../policy-isolation.md)); `declared_outputs`, `files_in` and `files_out` say what moves and how much.

**`requires` grows from what the kind declares, in both directions.** A spec that declares any output — of either disposition — is *refused* without `FILES_OUT`, because the capability match is the only thing standing between that spec and a backend with no pull surface, and it only ever runs on what `requires` names. A spec that declares no outputs should not require `FILES_OUT` at all: every capability a kind asks for is a backend it can no longer run on, and asking for one it does not use is portability given away for nothing. The vocabulary and the match are [`../capabilities.md`](../capabilities.md).

## Writing a kind that collects artifacts

The six rules, from [`../research/files-out.md`](../research/files-out.md) § *Writing a kind that collects artifacts*:

1. **Declare your outputs** — literal relative paths, each with a disposition, a media type, and `required` set honestly.
2. **Tell the model where to write.** The output path has to appear in the tool's description; a program that saves its PNG somewhere else produces nothing collectable and no error.
3. **Do not put bytes in the result.** Return the references `deliver` gave you.
4. **Require `FILES_LIST` only if you truly cannot name your outputs.** It is refused on Docker and wslc, so a kind that requires it without needing it has made itself ACAS-only. "The model decides at run time" is *not* that case — set `outputs_named_at_call_time` and pass the names to `collect_outputs(outputs=...)`.
5. **Grow `requires` from what you declare** — the rule above, applied.
6. **Do not combine a sink with an explicit `declarations=`.** It is refused, because the two disagree about what the tool's information flow is.

The same document carries a worked example — a `render_diagram` kind, the smallest workload that exercises every rule above — and its spec is the shortest statement of the whole pattern: `egress_allow=()` because rendering is computation, no `FILES_LIST` because the kind names its own output, `max_files=1` because one call renders one graph, and `required=False` because a renderer failing on malformed input is a diagnostic the model should act on rather than a transport error.

## Writing a kind that declares its information flow

Use the [worked kind-authoring guide](writing-a-kind.md) for a complete factory, body, host configuration, and verification checklist. [Information flow](../information-flow.md) owns the design and the label decision table. These rules summarize what the author must establish:

1. **Choose the declaration from the result's sources.** If model-authored code, untrusted files, or another unestablished source can affect the result, declare `source_integrity=SourceIntegrity.UNTRUSTED`. Leaving it unset delegates to the framework's input-label join or host default; either may answer trusted without knowing about those sources.
2. **Justify every channel before declaring trusted.** Include file-store reads, network responses, and host-tool results. Core refuses an explicit trusted declaration over channels the spec opens but cannot establish as trusted. `nothing_survives_from=(...)` is the author's assertion that a named channel contributes nothing, including presence bits; it is not a proof. A weak file read still demotes the call when the host enables the runtime stamp.
3. **Authorship is not integrity.** A first-party compiler's diagnostic can quote a model-authored identifier. Package-authored formatting does not make that diagnostic trusted.
4. **A source declaration replaces the input-label join.** It does not merely limit or supplement the join. The declaration must account for the sources the framework would otherwise see as well as the ones it cannot see.
5. **Return unlabelled derived items, then committed guidance.** Commit fixed sentences with `standing_guidance=(...)` and return them last, in order, on every normal return, including refusals and error sentences. Their text and presence must be independent of input. Counts, exit statuses, sizes, and conditional advice stay derived. Only `{call_id}` may interpolate. Core requires at least one derived item, validates and rebuilds the suffix, and stamps guidance trusted/public. It refuses every body-written `security_label`, even without a guidance commitment, and never quotes rejected content in its error.
6. **Account for host tools when you serve them.** `HostToolAggregate.result_integrity` folds the registered tools' source integrity, with unstamped tools treated as untrusted. It establishes that channel only; a trusted aggregate does not establish files or network responses. The file-read fold does not track host-tool result dataflow.
7. **Read files through the session.** Resolve a name against `session.list_files(store)` and pass its `ListedFile` entry to `session.read_file`. The visible argument is a name; the bytes behind it reach the body out of band. The session records successful reads per call, including empty files. A direct `store.read` bypasses that record. Unknown integrity wins over established values; missing and refused reads contribute nothing.
8. **Keep sink derivation consistent.** Do not combine `output_sink` with an explicit `declarations=` mapping. Core derives the tool's outward flow from the sink and spec together; a mapping would replace that derivation. The host's outbound confidentiality cap is separate from its result classification.
9. **Keep file integrity, hidden names, and result confidentiality separate.** A listing's integrity describes the bytes; it never licenses echoing the name. When names must be shown, call `positions_holding_hidden_content` before the first await and pass each position's verdict to `echoed_name`. The host supplies the same provenance record to the listing and session. It supplies result confidentiality separately on the attached tool. With both valid declarations, core stamps all derived items with the weaker of source integrity and the call's file fold, copying host confidentiality. Otherwise it leaves those items to framework resolution. Trusted reads never promote an untrusted declaration, and no call changes a shared declaration.

Record the source argument in your kind's design page and package README. A reviewer should be able to see why the declaration holds without reconstructing it from the body. Both shipped kinds declare untrusted and leave the wrapper to label their committed guidance.

## The shipped kinds

| Kind | Tool | `requires` | Egress modes it accepts, and what it runs by default | Package |
|---|---|---|---|---|
| [`bicep`](bicep.md) | `bicep_validate` | `{EXEC, FILES_IN}` — the protocol default, left unaltered | `{UNRESTRICTED, ALLOWLIST, CLOSED}`, defaulting to `ALLOWLIST` with the four AVM hosts fixed in the package | [`maf-sandbox-bicep`](../../../packages/maf-sandbox-bicep/README.md) |
| [`codeact`](codeact.md) | `execute_code` | `{EXEC, FILES_IN}`, grown by `FILES_OUT` and `HOST_TOOLS` as the host wires channels | `{CLOSED, ALLOWLIST}`, derived rather than passed: hosts named runs `ALLOWLIST`, none runs `CLOSED`, and `UNRESTRICTED` is not expressible | [`maf-sandbox-codeact`](../../../packages/maf-sandbox-codeact/README.md) |

Both are stdout-and-diagnostics workloads over `EXEC`; neither raises `min_isolation`, so the host's floor governs both.

## Status

| Decision | State | Tracking |
|---|---|---|
| A kind is protocol-only, and three tests enforce it rather than prose | shipped | — |
| Fixed templates, listing-pinned paths, sanitized surfaces, one chosen egress mode, visible degrades | shipped — the pattern holds in both kinds | — |
| A kind guards the egress modes it accepts at construction, and the spec carries one resolved mode | shipped — bicep takes the mode as an argument, codeact derives it from its host list. The per-kind record is still open even though the change it records is delivered | the per-kind rows on [`bicep.md`](bicep.md) and [`codeact.md`](codeact.md), which carry the open record, the merged PR that delivered it and the closed umbrella; the model itself is [`../network.md`](../network.md) |
| `requires` grows from what the spec declares; a declared output without `FILES_OUT` is refused | shipped — the refusal is at attach, in `sandboxed_tool`, and the `FILES_OUT` rollout it belongs to is still open on its remaining items | [`../capabilities.md`](../capabilities.md) § Status, row "`FILES_OUT` rollout", which carries the open umbrella |
| Per-kind contracts: [`bicep.md`](bicep.md), [`codeact.md`](codeact.md) | see each page | — |
| A guest-OS axis a kind can declare and a backend match | shipped in core, half used — all three real backends declare `os_families` now, and neither kind sets `requires_os_family`, so nothing is refused *in practice*: the refusal fires only on a spec that asks. codeact still execs `python3` and nothing states it | [`../guest-platform-and-commands.md`](../guest-platform-and-commands.md) § Status, first row, which carries the closed issue and the merged PR, with the per-kind reading on [`bicep.md`](bicep.md) and [`codeact.md`](codeact.md) |
| `run_code` is a protocol method a kind could be written against | shipped, unreachable — no shipped backend declares `RUN_CODE`, and codeact hard-requires `EXEC`, so the two cannot meet yet. Whether the kind gets a matcher disjunction or a second spec is undecided | [`codeact.md`](codeact.md) § Status, row "A `RUN_CODE`-only backend serving this kind", which carries the closed method issue and the open matcher one |
| `egress_allow` distinguishes "this kind needs no network" from "nobody asked", so a deployment default has somewhere to live | open — narrowed rather than closed by the mode: a spec now says `CLOSED` outright, but an empty host list still cannot tell the two apart | [`../network.md`](../network.md) § Status, row "A deployment-wide default allowlist a kind inherits", which carries the open issue; the kind-side half is [`codeact.md`](codeact.md)'s two-halved `egress_allow` row |
| What a kind may declare about the result it hands back, and the rules above that follow from it | shipped — core validates and labels committed guidance, and can weaken derived results from the per-call file fold when the host declares confidentiality. The worked guide covers the complete authoring pattern | [`../information-flow.md`](../information-flow.md), which owns the design and issue trail; [`writing-a-kind.md`](writing-a-kind.md), which applies it |
