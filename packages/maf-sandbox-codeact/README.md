# maf-sandbox-codeact

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

One tool, `execute_code`. The program is written to a directory of its own and run as the argv `["python3", ".../program.py"]`, and the result is its stdout, its stderr when it wrote any, and its exit code when that was not zero. There is no REPL echo, so a program that computes without printing returns a sentence saying so.

**Every call gets a fresh directory, and that is load-bearing rather than hygiene.** `acquire` is get-or-create, so the same sandbox serves every call in a conversation. Without a per-call directory a file deleted from the workspace between rounds would still be there for the next program to read as current, and last round's output file would be collected as this round's — a stale answer presented as a live one, in a kind whose whole job is transforming files.

Two further channels exist and neither is on by default. Wire neither and this is the stdout-only kind it has always been.

### Files in

Pass a `workspace_store` and the tool grows a `files` parameter:

```python
tools = make_codeact_tools(router, "data-analyst", context,
                           workspace_store=store, image=...)
```

Each named file is read from the store and written into the program's working directory under its own name, so `data/sales.csv` is what the program opens. **The caller's listing is the authority**: only a name present in `WorkspaceContext.list_files` is ever shared, so a name the model invented — or read out of a file it was given — has nowhere to go. A name outside the listing comes back as a refusal naming the near misses; a name that traverses comes back as a refusal that echoes nothing.

### Files out

Produced files never come back as bytes. They go to a host-supplied `OutputSink`, and the model gets the reference the sink returned. Two ways to name them, and the host picks one:

```python
from maf_sandbox_codeact import CodeactOutputs, make_codeact_tools

tools = make_codeact_tools(router, "data-analyst", context,
                           output_sink=sink, outputs=CodeactOutputs.DECLARED, image=...)
```

- **`DECLARED`** adds an `outputs` parameter: the model says what its program will write *before* it runs. Names are validated and capped up front, and one declared but not written is reported back by name rather than dropped. Prefer this.
- **`MANIFEST`** has the program write `outputs.json` listing what it produced — for a program whose output names it can only know once it has read its input. The names are then the guest's rather than the model's, settled after the fact.

Either way the kind requires `FILES_OUT` and **never** `FILES_LIST`: it collects literal paths and never enumerates a directory, so it runs on every backend that serves the pull surface at all rather than only on the one with the richest file API. `files_out.max_files` is what bounds how many artifacts a single call may produce, and `files_in` bounds what one call may share in — count, per-file bytes and total. Both are enforced by this kind, because no backend's `write_file` or `read_file` knows the workload's caps.

**No media type is ever taken from the guest.** `Artifact.media_type` is `None` on both roads: the kind does not know what a model-written program produced, and a value read out of `outputs.json` would be the guest telling the host how to handle its own bytes — which a sink may act on to choose inline rendering. A host that wants to decide by extension has `Artifact.name` and its own policy.

**Where files land is the host's decision, never this kind's.** That is the point of the sink, and it matters more here than for any other kind: these bytes were authored by model-written code. A host that points the sink at the same store the agent's own file tools write to has given that code an unapproved `file_access_write`, and one that lets it overwrite has given it a way to influence a *different* tool on the next call. Point it somewhere the agent cannot otherwise reach.

## Threat model

**The source is never a command line.** Model-written code reaches the interpreter as file *content* and the command is a fixed two-element argv — a sequence, not a shell string — so there is no command line for the source to be part of, nothing to quote, and nothing to escape. That is the security-relevant decision in this package and it is pinned by a test.

**Egress is closed.** `SandboxSpec.egress_allow` is empty, stated as a property of the workload rather than of configuration: the program computes, it does not fetch. A backend that cannot confine egress at all is refused at attach.

**Nothing is dispatchable from inside.** There is no host-tool registry in this version, and that emptiness is the security story rather than a missing feature: the program cannot open a socket and cannot call a host function, so it initiates nothing. The output sink does not change that — the kind calls it host-side, after the program has exited, and nothing inside the sandbox can reach it. A host wanting a hard stop denies `FILES_OUT`.

**A workspace store is ingress, and it is the host's own.** "Nothing can get in" describes what the *program* can initiate, not what the host puts there. With `workspace_store` wired, caller-selected files are written into the sandbox before the program runs — deliberately, and constrained to the caller's listing, so the model cannot widen the set. What that content *is* remains the host's to know: a workspace file may itself carry text from somewhere untrusted, and a program that parses it is running on input the sandbox did not vet. Wire no store and this paragraph does not apply.

**The tool declares no `source_integrity`.** The library's default is `"trusted"`, which is right for a workload whose result is a compiler's own diagnostics and wrong for this one: what comes back is whatever a model-written `print(...)` chose to emit. Undeclared, MAF's information-flow tracker applies its untrusted default and the result taints the conversation — the fail-safe direction, and the honest one.

**Isolation is the host's call, and a store changes what that call is about.** This kind does not raise `SandboxSpec.min_isolation`, so the router's floor governs — `MICROVM` unless the host opted down. A kind that ran code influenced by untrusted external content would pin the floor itself, and this one cannot know whether it is one: with no store, the program's only input is source the model wrote, and opting down to `CONTAINER` weighs model-written code against a shared kernel. **With a store, the program also reads whatever those files contain**, so the floor should be chosen against the provenance of the workspace, not against this kind's defaults. Only the host knows that.

## What this version is not

Host-tool dispatch and the `RUN_CODE` road served by an embedded-interpreter backend are absent on purpose. The design that governs them — capabilities declared by backends and required by specs, and what `HOST_TOOLS` would have to carry before it ships — is [`docs/design/two-axis-sandbox-policy.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/design/two-axis-sandbox-policy.md); the file channels above are specified in [`docs/design/files-out.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/design/files-out.md).

There is also **no way to delete a file from a sandbox**, which is why staleness is answered by a fresh directory per call rather than by cleaning the old one. A long conversation therefore accumulates one directory per call until the sandbox is disposed.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
