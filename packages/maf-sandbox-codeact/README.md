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

One tool, `execute_code(code)`. The program is written to `/work/program.py` and run as the argv `["python3", "/work/program.py"]`, and the result is its stdout, its stderr when it wrote any, and its exit code when that was not zero. Nothing else comes back: there is no REPL echo, and no file is read out of the sandbox, so a program that computes without printing returns a sentence saying so.

## Threat model

**The source is never a command line.** Model-written code reaches the interpreter as file *content* and the command is a fixed two-element argv — a sequence, not a shell string — so there is no command line for the source to be part of, nothing to quote, and nothing to escape. That is the security-relevant decision in this package and it is pinned by a test.

**Egress is closed.** `SandboxSpec.egress_allow` is empty, stated as a property of the workload rather than of configuration: the program computes, it does not fetch. A backend that cannot confine egress at all is refused at attach.

**Nothing is dispatchable from inside.** There is no host-tool registry in this version, and that emptiness is the security story rather than a missing feature: with no network and no host functions reachable, nothing external can enter the sandbox and nothing leaves it but what the program printed. Adding that surface is what changes the calculus, so it is deliberately not here yet — see below.

**The tool declares no `source_integrity`.** The library's default is `"trusted"`, which is right for a workload whose result is a compiler's own diagnostics and wrong for this one: what comes back is whatever a model-written `print(...)` chose to emit. Undeclared, MAF's information-flow tracker applies its untrusted default and the result taints the conversation — the fail-safe direction, and the honest one.

**Isolation is the host's call.** This kind does not raise `SandboxSpec.min_isolation`, so the router's floor governs — `MICROVM` unless the host opted down. A kind that ran code influenced by untrusted external content would pin the floor itself; this one has no such input.

## What this version is not

`FILES_OUT` (reading artefacts back out of the sandbox), a workspace-files parameter, host-tool dispatch, and the `RUN_CODE` road served by an embedded-interpreter backend are all absent on purpose. The design that governs them — capabilities declared by backends and required by specs, and what `HOST_TOOLS` would have to carry before it ships — is [`docs/design/two-axis-sandbox-policy.md`](https://github.com/sokolaidev/maf-extensions/blob/main/docs/design/two-axis-sandbox-policy.md).

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
