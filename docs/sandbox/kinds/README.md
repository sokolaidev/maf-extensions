# Kinds

> What a *kind* is: a workload written against the sandbox protocol and nothing else, the pattern the first one set, and how a spec grows from what the kind declares. Sources of record: [`../research/sandbox-architecture.md`](../research/sandbox-architecture.md) and [`../research/files-out.md`](../research/files-out.md).

**These pages own the architecture-facing contract of each kind — the spec it declares and why, its security pattern, its portability story. Each package's own README owns install and usage, and is linked rather than duplicated.** Someone deciding whether a kind fits a deployment reads here; someone wiring it up reads there. Where the two would say the same thing, the package README is the one that gets to say it, because it ships with the code.

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
- **The caller's file listing is the injection pin.** Only a name present in `CallerContext.list_files` is ever substituted, so a name the model invented, or read out of a poisoned file, has nowhere to go. A failure to enumerate is a *refusal*, never an empty listing: empty would look like "the store has no files" and refuse every name individually with the wrong reason.
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

The same document carries a worked example — a `render_diagram` kind, the smallest workload that exercises every rule above — and its spec is the shortest statement of the whole pattern: `egress_allow=()` because rendering is computation, no `FILES_LIST` because the kind names its own output, `max_files=1` because one invocation renders one graph, and `required=False` because a renderer failing on malformed input is a diagnostic the model should act on rather than a transport error.

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
| A kind guards the egress modes it accepts at construction, and the spec carries one resolved mode | shipped — bicep takes the mode as an argument, codeact derives it from its host list | [#525](https://github.com/sokolaidev/maf-extensions/issues/525) (open, the per-kind record) delivered in [#530](https://github.com/sokolaidev/maf-extensions/pull/530) (merged) under [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed) |
| `requires` grows from what the spec declares; a declared output without `FILES_OUT` is refused | shipped | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) (open, the `FILES_OUT` umbrella) |
| Per-kind contracts: [`bicep.md`](bicep.md), [`codeact.md`](codeact.md) | see each page | — |
| A guest-OS axis a kind can declare and a backend match | shipped in core, used by nobody — `requires_os_family` exists and neither kind sets it, no backend declares `os_families`, so the axis refuses nothing today. codeact still execs `python3` and nothing states it | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged) |
| `run_code` is a protocol method a kind could be written against | shipped, unreachable — no shipped backend declares `RUN_CODE`, and codeact hard-requires `EXEC`, so the two cannot meet yet | [#381](https://github.com/sokolaidev/maf-extensions/issues/381) (closed); the matcher question is [#425](https://github.com/sokolaidev/maf-extensions/issues/425) (open) |
| `egress_allow` distinguishes "this kind needs no network" from "nobody asked", so a deployment default has somewhere to live | open — narrowed rather than closed by the mode: a spec now says `CLOSED` outright, but an empty host list still cannot tell the two apart | [#403](https://github.com/sokolaidev/maf-extensions/issues/403) (open) |
