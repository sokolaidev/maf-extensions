# maf-sandbox-codeact

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-codeact)](https://pypi.org/project/maf-sandbox-codeact/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxCodeactExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_codeact.MafSandboxCodeactExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework).

CodeAct as a Microsoft Agent Framework tool: the agent gets one tool, `execute_code`; the model writes a short Python program; the program runs inside a sandbox and the tool returns what it printed. Computing an answer beats reasoning about what the computation would produce — and the code that does it runs somewhere the host is not.

```
app  ->  maf_sandbox  ->  a backend (maf-sandbox-acas, maf-sandbox-wslc, ...)  ->  this workload
```

This package is a sandbox **kind** in the sense of [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s protocol. It contains no Azure import, no backend import and no sandbox lifecycle code; it asks a `SandboxRouter` for a sandbox and gets back `write_file` and `exec`, so the same tool runs unchanged against ACA Sandboxes, a WSL container or an in-process fake. Tests enforce both boundaries.

## Quickstart

```bash
pip install maf-sandbox-codeact
```

```python
from maf_sandbox_codeact import make_codeact_tools

tools = make_codeact_tools(router, "data-analyst", context,
                           image="mcr.microsoft.com/devcontainers/python:3.13-bookworm")
```

Pass `router=None` — or a router with no backend — and you get `[]` back: an unconfigured host attaches no tool rather than one that fails when called. A backend that cannot `exec`, or cannot take files in, is refused right there with `SandboxCapabilityNotSupported`, before the model is shown a capability it does not have.

`router` and `context` are the host's, and this snippet shows neither being built. [`samples/03_acas_codeact`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/03_acas_codeact) and [`samples/04_wslc_codeact`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/04_wslc_codeact) are the whole wiring as runnable programs — the same agent on a microVM-isolated Azure backend and on a container on your own machine.

## What the model gets

One tool, `execute_code`. The program is written to a directory of its own and run as the argv `["python3", ".../program.py"]`, and the result is its stdout, its stderr when it wrote any, and its exit code when that was not zero. Both of those change shape once `host_tools` is wired — a launcher runs the program and its stderr arrives merged into stdout — which the sections below cover. There is no REPL echo, so a program that computes without printing returns a sentence saying so.

**Every call gets a fresh directory, and that is load-bearing rather than hygiene.** `acquire` is get-or-create, so the same sandbox serves every call in a conversation. Without a per-call directory a file deleted from the file store between rounds would still be there for the next program to read as current, and last round's output file would be collected as this round's — a stale answer presented as a live one, in a kind whose whole job is transforming files.

Four further channels exist and none is on by default: a file store, an output sink, a host-tool registry, and an egress allowlist. Wire none and this is the stdout-only kind it has always been.

### Files in

Pass a `file_store` and the tool grows a `files` parameter:

```python
tools = make_codeact_tools(router, "data-analyst", context,
                           file_store=store, image=...)
```

Each named file is read from the store and written into the program's working directory under its own name, so `data/sales.csv` is what the program opens. **The caller's listing is the authority**: only a name present in `CallerContext.list_files` is ever shared, so a name the model invented — or read out of a file it was given — has nowhere to go. A name outside the listing comes back as a refusal naming the near misses; a name that traverses comes back as a refusal that echoes nothing.

### Files out

Produced files never come back as bytes. They go to a host-supplied `OutputSink`, and the model gets the reference the sink returned. Two ways to name them, and the host picks one:

```python
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools

tools = make_codeact_tools(router, "data-analyst", context,
                           output_sink=sink, outputs=CodeactOutputs.DECLARED, image=...)
```

- **`DECLARED`** adds an `outputs` parameter: the model says what its program will write *before* it runs. Names are validated and capped up front, and one declared but not written is reported back by name rather than dropped. Prefer this.
- **`MANIFEST`** has the program write `outputs.json` listing what it produced — for a program whose output names it can only know once it has read its input. The names are then the guest's rather than the model's, settled after the fact. The manifest is itself a file the collection moved, so it takes one slot of `files_out.max_files` and its bytes count against the ceilings; a cap below 2 leaves no room for an artifact and is refused at attach.

Either way the kind requires `FILES_OUT` and **never** `FILES_LIST`: it collects literal paths and never enumerates a directory, so it runs on every backend that serves the pull surface at all rather than only on the one with the richest file API. `files_out.max_files` is what bounds how many artifacts a single call may produce, and `files_in` bounds what one call may share in — count, per-file bytes and total. Both are enforced by this kind, because no backend's `write_file` or `read_file` knows the workload's caps.

**No media type is ever taken from the guest.** `Artifact.media_type` is `None` on both roads: the kind does not know what a model-written program produced, and a value read out of `outputs.json` would be the guest telling the host how to handle its own bytes — which a sink may act on to choose inline rendering. A host that wants to decide by extension has `Artifact.name` and its own policy.

**Where files land is the host's decision, never this kind's.** That is the point of the sink, and it matters more here than for any other kind: these bytes were authored by model-written code. A host that points the sink at the same store the agent's own file tools write to has given that code an unapproved `file_access_write`, and one that lets it overwrite has given it a way to influence a *different* tool on the next call. Point it somewhere the agent cannot otherwise reach.

### Host tools

Pass a `host_tools` registry and the program gets a way to call out over [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox)'s opt-in transport for compatible `EXEC` backends:

```python
from maf_sandbox import HostToolRegistry
from maf_sandbox_codeact import make_codeact_tools

registry = HostToolRegistry()
registry.register(exchange_rate)
tools = make_codeact_tools(router, "data-analyst", context, host_tools=registry, image=...)
```

A non-empty registry widens `requires` by `Capability.HOST_TOOLS` **and** `Capability.FILES_OUT` together — the transport stats and reads its own request files and the exit marker over the same pull surface, so even a stdout-only program that calls a host function needs it. One requirement the capability set cannot express: the launcher `host_tool_calls_over_exec` writes is POSIX shell and needs a guest with `sh` and `nohup`, so a Windows or distroless image is out whatever it declares. It also carries the registry's `identities`, so a router's `denied_identities` can refuse the widened spec at attach; raises `approval_mode` to `always_require` the moment any tool declares `Identity.USER`, since which call would exercise the caller's own authority is not knowable before the program runs; and makes the host's own `outbound_max_confidentiality` apply the moment a tool declares a sink or leaves the question unanswered — an **unstamped** tool is read as carrying something out, like every other undeclared leg — even though nothing lands, which is the one flow a derivation reading only the spec cannot see. **Reading the registry seals it**, so pass `host_tools` only once everything is registered: a `register` afterwards is refused at the host's own call site. Only where a sandbox is configured, though — an unconfigured host attaches nothing and derives nothing from the registry, so nothing is sealed and a late `register` is allowed. A host developing with sandboxing off meets that refusal in production.

At call time the tool writes the generated guest module beside the program and runs `host_tool_calls_over_exec` under a fresh `HostToolRun` per call. A host-tool-call run is two guest directories: the transport's files — the program, the module, the launcher, the output and exit marker — live in `host_tools/`, and everything a model names in `files=` or `outputs=` lives in `work/`, which is the program's working directory. So none of the transport's names is reserved against a model-supplied one; there is nothing for the two to collide over. Without a registry the run is the flat directory it has always been, and there `program.py` is still refused as an input or output name. The description the model reads names the callable tools and the one call form that always works, and qualifies the "no network access" claim: with no allowlist the sandbox still has none of its own, and the listed tools are the only way past it.

**`maf-sandbox-docker` and `maf-sandbox-acas` declare `Capability.HOST_TOOLS`; `maf-sandbox-wslc` does not.** A stdout-only program with host tools wired needs `{EXEC, FILES_IN, FILES_OUT, HOST_TOOLS}`, which drops wslc twice over — its backend declares only `{EXEC, FILES_IN}`. Against a backend that cannot serve the widened spec, wiring `host_tools` is refused where the tool would have been built: `make_codeact_tools` raises `SandboxCapabilityNotSupported`. Not a dormant wiring that fails at the first call — a construction-time failure, which is the same refusal an unservable spec gets anywhere else, met earlier than most.

What the two declaring backends assert is narrower than the other capabilities, and worth reading before relying on it: that their `exec` **detaches** — a process started by one call outlives it and is observable from the next — because that is the one property `host_tool_calls_over_exec` is built on. Both measure it against a real engine rather than asserting it. What no test covers yet is a full round trip through this kind against a live backend, with the cost of one measured; that is [#302](https://github.com/sokolaidev/maf-extensions/issues/302).

## Threat model

**The source is never a command line.** Model-written code reaches the interpreter as file *content*, on both roads, so there is no command line for it to be part of and nothing about the source to quote or escape. That is the security-relevant decision in this package. Wire no `host_tools` and the command is a fixed two-element argv — a sequence, not a shell string, so no shell runs at all — and that is the path the pinning test covers. Wire one and the run goes through `host_tool_calls_over_exec`, which does use `sh`: it execs a shell line naming the launcher it wrote, and that launcher nests a quoted `sh -c` to redirect the program's output and record its exit code. Every path in either is fixed or generated host-side — the interpreter, the transport's own filenames, and a work directory with a per-call run id — and `maf-sandbox` single-quotes each one; the model contributes none of them.

**Egress is closed by default, and opening it is the host's decision.** The allowlist has two halves. What the *kind* needs to function is fixed in the package and is empty — nothing in `execute_code` resolves a module or installs anything, so there is nothing for it to need — and it is not configurable, because a deployment able to widen what the kind itself requires could undo the containment. What the *deployment* adds is `make_codeact_tools(egress_allow=…)`, empty by default, for endpoints a published kind cannot know: a package index, an internal artifact store. The spec carries the union, which is what the router matches against the backend and what decides whether this tool is declared as carrying something out. A backend that cannot confine egress at all is refused at attach — but one that confines *more* is admitted with a warning, so on a `CLOSED` backend a named host is simply unreachable and the fetch fails loudly. The description the model reads says as much.

Naming a host is a real widening, and worth naming as such: this sandbox runs model-written code, so every allowed host is a way out for anything the program can read — files shared into the run, and whatever a host tool returned. The description the model reads names the allowed hosts and stops claiming the sandbox has no network, because it would no longer be true.

**A host function is callable from inside, on a backend that declares `Capability.HOST_TOOLS` — `maf-sandbox-docker` and `maf-sandbox-acas` both do.** Wiring a `host_tools` registry widens the spec's `requires` by that capability, and against those two the widened spec attaches: the program calls what was registered, each call runs in the host process with the host's authority, and the boundary sees only `execute_code`'s aggregate result. This is the one direction of trust this kind opens outward, and it is off until a host wires a registry. Against a backend that does not declare it — wslc — the widened spec is refused at construction and the program cannot call a host function at all. It cannot open a socket either *unless a host named a destination above* — with no `egress_allow` the sandbox initiates nothing at all. The output sink does not change that either — the kind calls it host-side, after the program has exited, and nothing inside the sandbox can reach it. A host wanting a hard stop on host tools denies `Capability.HOST_TOOLS`; on outputs, `FILES_OUT`.

**The host authorises every way in, and initiates only one of them.** With `file_store` wired, caller-selected files are written in before the program runs — deliberately, and constrained to the caller's listing, so the model cannot widen the set. What that content *is* remains the host's to know: a file in the store may itself carry text from somewhere untrusted, and a program that parses it is running on input the sandbox did not vet. An allowlisted host is the other way, and the *program* initiates that one — whatever it fetches is input nobody chose in advance. Wire no store, no allowlist and no registry, and nothing gets in but the source the model wrote.

**The tool declares no `source_integrity`, in either rendering.** A `"trusted"` claim asks that the result not derive from input the framework has left unestablished, and this one does: what comes back is whatever a model-written `print(...)` chose to emit. Undeclared, MAF's information-flow tracker applies its untrusted default — the fail-safe direction, and the honest one. A declaration would not sit under that default as a floor, either: FIDES reads `source_integrity` as an override and drops the input-label join entirely.

**What the untrusted default costs is the model's sight of the result, not the host's sinks.** FIDES hides an untrusted result by default: the item is replaced by a variable reference the model can pass to another tool without reading, and hidden content does not taint the conversation's integrity, so later tools run ungated. Turn hiding off and the result is visible, the conversation goes untrusted, and `PolicyEnforcementFunctionMiddleware` gates every later tool that has not opted in. A host gets one or the other. Two limits on the hiding half: it lapses once anything else has tainted the conversation, and it is about integrity only — a hidden item still contributes its confidentiality. Measured, with the full conditions, in [`docs/sandbox/information-flow.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md).

**`withhold_guest_output=True` changes what comes back, and not what it claims.** The result is then the exit code, the size of each stream, which of the declared outputs landed — by the name the model itself declared — and one sentence naming the route. No guest-authored *text* survives into it. What does survive is chosen by the program all the same, so the declaration stays absent: withholding removes the guest's text from the result, not the guest from the result's derivation. What it is for is the rendering itself — guest-authored text never reaches the transcript, a host's logs, or anything else downstream of the call, and that holds whether or not a host runs information-flow middleware. Content still reaches the model by the road the option leaves open: the program writes it to a declared output, that lands in the sink, and the host's own already-classified file-reading path serves it back.

**Withheld, the result is two items rather than one string, and only one of them is hidden.** The run's half — the exit code, the sizes, the landed names — carries no label of its own, so it takes whatever label the call would otherwise have had; beside it sits the sentence naming the recovery route, labelled `trusted`, because nothing a run produced reaches it and it is emitted on every return path, refusals included. A FIDES host with hiding on therefore reads the sentence while the numbers sit behind a variable reference — which is the whole point of the split: under one label the sentence went with the numbers it was there to explain, and a model got a hidden result and no way to act on it. The run's half stays unlabelled deliberately: a per-item label replaces the *whole* label, confidentiality included, and confidentiality values are the host's vocabulary rather than this package's to invent. A host wiring no information-flow middleware simply reads both items.

The rendering follows the transport. Off the host-tool-call transport you get the exit code and a size for `stdout` and for `stderr` separately. On it, the launcher merges the program's stderr into its stdout, so there is one `output` size — and `stderr` is the host's field there, so its note about the run is surfaced whole under `note:`. Withholding that note would report a run whose output was dropped for its size as one that printed nothing.

Be precise about what that buys. The prose and the shape are this package's, and the artifact names are the model's own — but what fills them is the program's to choose. An exit status is 8 bits. Each stream's size is a few more, chosen by padding. And **each declared output is one further bit**: the program decides whether to write it, and the result says of every declared name whether it landed, so a call declaring the default eight names carries eight more. So what stops crossing is guest-authored *text*, not every guest-chosen *bit*. That is a narrow, per-call channel rather than the open one a rendered `stdout` is, and it is the honest limit. A host that has to close it should not attach this workload at all.

A "size" here is the UTF-8 length of the text the stream decoded to, not the byte count the program wrote. `ExecResult` states no decoding contract ([#465](https://github.com/sokolaidev/maf-extensions/issues/465)), so a backend replacing an undecodable byte changes the number: the same program writing four bytes, `b"ok\xff\xfe"`, reports 8 on Docker and 5 on ACA Sandboxes. Neither is 4, and nothing here can recover it — `ExecResult` carries neither the bytes nor a length.

**The sink's `display` is not rendered in this mode.** `OutputSink.deliver` is handed an `Artifact` whose `content` is the guest's bytes, and no protocol rule keeps `display` independent of it — a sink that quoted any of that content would put guest-authored text straight back into the result. Withheld, the saved files are named by the spelling the model itself declared, which costs the sink's own detail and needs no promise from the host to stay honest.

The mode is required to be `CodeactOutputs.DECLARED` and both other pairings are refused at construction, each for its own reason. Under `NONE` there is nowhere for content to go, so no call could return anything the model can use. Under `MANIFEST` the *program* names its own files in `outputs.json`, and this kind renders those names back into the result — a guest-chosen string inside a result claiming to hold none, which is the fail-open shape the option exists to close.

The model is told, in the tool description, that printing does not come back. That is not politeness: an agent left to discover it from a result prints its answer anyway, gets nothing usable, and spends further calls working out why.

**Isolation is the host's call, and a store changes what that call is about.** This kind does not raise `SandboxSpec.min_isolation`, so the router's floor governs — `MICROVM` unless the host opted down. A kind that ran code influenced by untrusted external content would pin the floor itself, and this one cannot know whether it is one: with no store and no allowlist, the program's only input is source the model wrote, and opting down to `CONTAINER` weighs model-written code against a shared kernel. **With a store the program also reads whatever those files contain, and with an allowlist whatever an allowed host returns** — so the floor should be chosen against the provenance of everything the program can read, not against this kind's defaults. Only the host knows that.

## Upgrading to 0.7.5

**A wired registry is now carried on the spec, so the router folds the transport's own traffic into the transfer-limit match.** An attach that used to succeed can be refused, with `SandboxTransferLimitsNotPermitted` naming the folded figure — instead of the same run overrunning the backend part-way through.

The lever is the registry's `response_limits`. The fold asks for about `max_host_tool_calls_per_run × response_limits.max_bytes_per_file` of `files_out`, because nothing bounds the sum of the guest's request files. At the 8 MiB default a 32-call registry asks for 344 MB, more than `maf-sandbox-acas` declares. Size the ceiling to what your tools return:

```python
from maf_sandbox import HostToolRegistry, TransferLimits

registry = HostToolRegistry(
    max_host_tool_calls_per_run=32,
    response_limits=TransferLimits(
        max_bytes_per_file=64 * 1024, max_total_bytes=1024 * 1024, max_files=32
    ),
)
```

## Upgrading to 0.7

`0.7.0` requires `maf-sandbox` 0.19, which made the egress mode a thing a workload declares.

**`codeact_sandbox_spec`'s signature is unchanged** — there is no `egress` parameter to pass, because the mode follows from what you already say: name hosts in `egress_allow` and the spec runs in `Egress.ALLOWLIST`, name none and it runs in `Egress.CLOSED`. Nothing to edit.

**What changed is what the router does with it.** A backend that cannot enforce the resulting mode is refused at attach rather than permitted with a warning, so a host that wired an allowlist against a backend confining everything used to get a program that failed at the fetch and now gets:

```
SandboxEgressNotEnforced: sandbox backend 'docker' cannot enforce the 'allowlist'
egress the 'codeact' workload runs in (it enforces closed).
```

Give the backend a mode it can enforce — for `maf-sandbox-docker`, configure `egress_proxy_image` — or drop `egress_allow` and let the program run closed. The refusal is deliberate: a program that silently could not reach what its host meant to allow is the failure this replaces.

## Upgrading to 0.3

`0.3.0` follows `maf-sandbox` 0.11, which retired the word `workspace` from the vocabulary. It requires that release.

**`make_codeact_tools` takes `file_store` where it took `workspace_store`.** This one is **keyword-only**, so unlike the Bicep kind's there is no positional call that survives untouched: every host wiring a store has an edit to make. A host that wires none is unaffected, since the parameter defaults to `None`.

## What this version is not

The `RUN_CODE` road served by an embedded-interpreter backend is absent on purpose — this kind only ever asks for `EXEC`. Host-tool calling is wired (`host_tools`, above), two shipped backends serve it, and what it costs has been measured: [`samples/15_acas_codeact_host_tools`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/15_acas_codeact_host_tools) walks a four-stage lookup on ACAS twice, once as a host-tool call and once through the model's own tool loop, and publishes both against wall clock and tokens ([#302](https://github.com/sokolaidev/maf-extensions/issues/302)). The short version: a host-tool call is a serial round trip of about a second. On the run that sample documents it walked the stages in three tool-calling rounds against the direct route's five, and none of the twelve sales figures were written into a call the model made — what comes back to it is the program's finished table. The design that governs capabilities — declared by backends, required by specs, and what `HOST_TOOLS` carries — is [`docs/sandbox/capabilities.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/capabilities.md), which specifies the file channels above as well; where an artifact lands, and what a host tool may do, is [`docs/sandbox/hosts.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/hosts.md).

The sandbox protocol's delete surface is capability-gated, and this kind does not require `FILES_DELETE`. It therefore uses a fresh directory per call for staleness isolation, while the framework reclaims that directory, and everything under it, when the call returns. A program that walks upwards can still open files outside its own directory during the call. The fresh directory removes staleness from the *namespace*, so a program reading `data.csv` gets this call's or nothing. Everything reachable that way belongs to the same conversation and the same agent, since that is what a sandbox is keyed by.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
