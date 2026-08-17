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

A non-empty registry widens `requires` by `Capability.HOST_TOOLS` **and** `Capability.FILES_OUT` together — the transport stats and reads its own request files and the exit marker over the same pull surface, so even a stdout-only program that calls a host function needs it. One requirement the capability set cannot express: the launcher `dispatch_over_exec` writes is POSIX shell and needs a guest with `sh` and `nohup`, so a Windows or distroless image is out whatever it declares. It also carries the registry's `identities`, so a router's `denied_identities` can refuse the widened spec at attach; raises `approval_mode` to `always_require` the moment any tool declares `Identity.USER`, since which call would exercise the caller's own authority is not knowable before the program runs; and makes the host's own `outbound_max_confidentiality` apply the moment a tool declares a sink or leaves the question unanswered — an **unstamped** tool is read as carrying something out, like every other undeclared leg — even though nothing lands, which is the one flow a derivation reading only the spec cannot see. **Reading the registry seals it**, so pass `host_tools` only once everything is registered: a `register` afterwards is refused at the host's own call site. Only where a sandbox is configured, though — an unconfigured host attaches nothing and derives nothing from the registry, so nothing is sealed and a late `register` is allowed. A host developing with sandboxing off meets that refusal in production.

At call time the tool writes the generated guest module beside the program and runs `dispatch_over_exec` under a fresh `HostToolRun` per call. A dispatching run is two guest directories: the transport's files — the program, the module, the launcher, the output and exit marker — live in `host_tools/`, and everything a model names in `files=` or `outputs=` lives in `work/`, which is the program's working directory. So none of the transport's names is reserved against a model-supplied one; there is nothing for the two to collide over. Without a registry the run is the flat directory it has always been, and there `program.py` is still refused as an input or output name. The description the model reads names the dispatchable tools and the one call form that always works, and qualifies the "no network access" claim: with no allowlist the sandbox still has none of its own, and the listed tools are the only way past it.

**No shipped backend declares `Capability.HOST_TOOLS` yet.** A stdout-only program with host tools wired needs `{EXEC, FILES_IN, FILES_OUT, HOST_TOOLS}` — which already drops `maf-sandbox-wslc`, whose backend declares only `{EXEC, FILES_IN}` — and none of this repository's shipped backends declare `HOST_TOOLS` at all, so wiring `host_tools` today is refused where the tool would have been built: `make_codeact_tools` raises `SandboxCapabilityNotSupported`. Not a dormant wiring that starts working when a backend arrives — a construction-time failure, which is the same refusal an unservable spec gets anywhere else, met earlier than most. Wire it when a backend can serve it.

## Threat model

**The source is never a command line.** Model-written code reaches the interpreter as file *content*, on both roads, so there is no command line for it to be part of and nothing about the source to quote or escape. That is the security-relevant decision in this package. Wire no `host_tools` and the command is a fixed two-element argv — a sequence, not a shell string, so no shell runs at all — and that is the path the pinning test covers. Wire one and the run goes through `dispatch_over_exec`, which does use `sh`: it execs a shell line naming the launcher it wrote, and that launcher nests a quoted `sh -c` to redirect the program's output and record its exit code. Every path in either is fixed or generated host-side — the interpreter, the transport's own filenames, and a work directory with a per-call run id — and `maf-sandbox` single-quotes each one; the model contributes none of them.

**Egress is closed by default, and opening it is the host's decision.** The allowlist has two halves. What the *kind* needs to function is fixed in the package and is empty — nothing in `execute_code` resolves a module or installs anything, so there is nothing for it to need — and it is not configurable, because a deployment able to widen what the kind itself requires could undo the containment. What the *deployment* adds is `make_codeact_tools(egress_allow=…)`, empty by default, for endpoints a published kind cannot know: a package index, an internal artifact store. The spec carries the union, which is what the router matches against the backend and what decides whether this tool is declared as carrying something out. A backend that cannot confine egress at all is refused at attach — but one that confines *more* is admitted with a warning, so on a `CLOSED` backend a named host is simply unreachable and the fetch fails loudly. The description the model reads says as much.

Naming a host is a real widening, and worth naming as such: this sandbox runs model-written code, so every allowed host is a way out for anything the program can read — files shared into the run, and whatever a host tool returned. The description the model reads names the allowed hosts and stops claiming the sandbox has no network, because it would no longer be true.

**No host function is dispatchable from inside, on any shipped backend.** A `host_tools` registry can be wired, and doing so widens the spec's `requires` by `Capability.HOST_TOOLS` — but no shipped backend declares it, so the widened spec is refused at attach rather than reaching a sandbox. The program cannot call a host function; that is a property of today's backends, not a missing feature of this kind. It cannot open a socket either *unless a host named a destination above* — with no `egress_allow` the sandbox initiates nothing at all. The output sink does not change that either — the kind calls it host-side, after the program has exited, and nothing inside the sandbox can reach it. A host wanting a hard stop on host tools denies `Capability.HOST_TOOLS`; on outputs, `FILES_OUT`.

**The host authorises every way in, and initiates only one of them.** With `file_store` wired, caller-selected files are written in before the program runs — deliberately, and constrained to the caller's listing, so the model cannot widen the set. What that content *is* remains the host's to know: a file in the store may itself carry text from somewhere untrusted, and a program that parses it is running on input the sandbox did not vet. An allowlisted host is the other way, and the *program* initiates that one — whatever it fetches is input nobody chose in advance. Wire no store, no allowlist and no registry, and nothing gets in but the source the model wrote.

**The tool declares no `source_integrity`.** The library's default is `"trusted"`, which is right for a workload whose result is a compiler's own diagnostics and wrong for this one: what comes back is whatever a model-written `print(...)` chose to emit. Undeclared, MAF's information-flow tracker applies its untrusted default and the result taints the conversation — the fail-safe direction, and the honest one.

**Isolation is the host's call, and a store changes what that call is about.** This kind does not raise `SandboxSpec.min_isolation`, so the router's floor governs — `MICROVM` unless the host opted down. A kind that ran code influenced by untrusted external content would pin the floor itself, and this one cannot know whether it is one: with no store and no allowlist, the program's only input is source the model wrote, and opting down to `CONTAINER` weighs model-written code against a shared kernel. **With a store the program also reads whatever those files contain, and with an allowlist whatever an allowed host returns** — so the floor should be chosen against the provenance of everything the program can read, not against this kind's defaults. Only the host knows that.

## Upgrading to 0.3

`0.3.0` follows `maf-sandbox` 0.11, which retired the word `workspace` from the vocabulary. It requires that release.

**`make_codeact_tools` takes `file_store` where it took `workspace_store`.** This one is **keyword-only**, so unlike the Bicep kind's there is no positional call that survives untouched: every host wiring a store has an edit to make. A host that wires none is unaffected, since the parameter defaults to `None`.

## What this version is not

The `RUN_CODE` road served by an embedded-interpreter backend is absent on purpose — this kind only ever asks for `EXEC`. Host-tool dispatch is wired (`host_tools`, above), but nothing can serve it: no shipped backend declares `Capability.HOST_TOOLS`, so passing a registry is refused at the factory rather than reaching a sandbox. The design that governs capabilities — declared by backends, required by specs, and what `HOST_TOOLS` carries — is [`docs/design/two-axis-sandbox-policy.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/design/two-axis-sandbox-policy.md); the file channels above are specified in [`docs/design/files-out.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/design/files-out.md).

There is also **no way to delete a file from a sandbox**, which is why staleness is answered by a fresh directory per call rather than by cleaning the old one. A long conversation therefore accumulates one directory per call until the sandbox is disposed — and a program that walks upwards can still open them. The fresh directory removes staleness from the *namespace*, so a program reading `data.csv` gets this call's or nothing; it does not put earlier rounds out of reach. Everything reachable that way belongs to the same conversation and the same agent, since that is what a sandbox is keyed by.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
