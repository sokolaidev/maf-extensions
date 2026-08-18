# 15 — A program calling back into the host (ACA sandbox)

Every other sample sends things *in* to a sandbox and takes results *out*. This one opens the other direction: model-written code, running inside a microVM, calling a function that executes in the host process.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
              ^ maf_sandbox_codeact calls the router        |
              +------ a host function, dispatched ----------+
```

**Read [sample 10's README](../10_inprocess_host_tools/README.md) first.** It is the configuration half — `@sandbox_tool`, the registry gate, the aggregate, the seal, and both ways a router refuses the whole kind — all answered at attach, with no sandbox and no model. This page is the traffic half ([#302](https://github.com/sokolaidev/maf-extensions/issues/302)) and does not re-teach any of it.

## The question this exists to answer

[#133](https://github.com/sokolaidev/maf-extensions/issues/133) says the trade-off is what the feature lives or dies on, and that it should be measured rather than assumed:

> a call-heavy program can cost more round trips than the direct tool calling this pattern exists to replace

So the sample runs the same question twice. Once with the function **dispatched** — the model writes a program, the program calls out once per SKU, and an interpreter does the arithmetic. Once **directly** — the same function handed to the model as an ordinary tool, which the model calls itself and then adds up in its head.

Both routes read the same price table through the same Python body. Any difference between them is a difference in what did the arithmetic, not in what either side was told.

## Why the prices have to be secret

`PRICES` lives in the host process. It is not in the image, not in the file store, and the sandbox has no egress — with no `egress_allow`, the guest initiates nothing at all. A program that wants a price has exactly one road to it.

That is what makes the measurement mean something. If the model could guess a plausible price the sample would measure nothing: a run that skipped every dispatch would still print a number, and the number would look fine. Here a skipped dispatch is a wrong total, and the check says so.

The quantities are awkward for the same reason sample 03 refuses a number a model can recite. The arithmetic has to be work.

## What a dispatch proves on this backend, and not on a local one

ACAS's `exec` is blocking and timeout-bounded. The guest shim publishes a request file and then **blocks**, waiting for a response the host can only write while the program is still running. If the launcher had not detached, the `exec` that started the program would own it, the host would never get to write, and the program would sit there until its deadline.

So a dispatch that is answered *at all* proves the launcher detached and the supervisor took over. The sample does not assert that separately — it is the precondition of act 2 producing any number, and the reason [#365](https://github.com/sokolaidev/maf-extensions/issues/365) treats `Capability.HOST_TOOLS` as a claim about `exec` rather than about a method.

## Reading the round-trip line

```
[measured] round trip: 2 gap(s), min 1.09s, median 1.09s, max 1.10s
```

The interval runs from the host **answering** one call to the **next arriving** — out through the response file, into the guest, back through the next request file. Serving one call is a `stat_file`, a `read_file` and a `write_file`, plus a `stat_file` per poll interval while it waits, so on a remote control plane the figure is dominated by HTTPS round trips rather than by the poll interval itself.

**A gap below the supervisor's poll interval is not a fast round trip.** It is two calls that were outstanding together, which the transport allows — the guest shim claims request ids with `O_CREAT | O_EXCL` precisely so a threaded or forking program can have several in flight. A model that writes a concurrent program will produce a handful of near-zero gaps and a smaller median. Nothing filters those out; a measurement that drops its inconvenient samples is not one.

## What the check does and does not enforce

This is the unusual part, and it is deliberate.

**The dispatch route must reach the exact total.** That is not a claim about a model. The prices came from the host and the sum came from a Python interpreter, so anything other than exact means the program did not run, did not call out, or ignored what came back.

**The direct route is recorded and never required.** It is a model doing arithmetic in its head. Across every run so far it has been wrong, and differently wrong each time — `$214.00`, `$211.55`, `$205.65`, `$203.95`, `$233.20`. But a check that *depended* on it staying wrong would be asserting that a model stays bad at addition, which is not this repository's claim to make and would go red the day it stops being true. The success line names which way it went, so a green never hides it.

Every line the check reads has to carry the `[measured]` tag at the left margin ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)). The sample's `quoted()` prefixes any tagged line inside a model's reply with `> `, so prose that tries to answer for the host is visibly not the host answering.

## Prerequisites

- An Azure subscription with the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes) preview enabled, and a sandbox group.
- `mcr.microsoft.com/devcontainers/python:3.13-bookworm` imported into that group as a disk image — sample 14's image, so a group set up for that one already has it. The guest needs `sh`, `nohup` and `python3`: the launcher is POSIX shell and the shim is Python. A distroless or Windows image cannot serve this whatever it declares. ([#412](https://github.com/sokolaidev/maf-extensions/issues/412) is why the import is needed at all — the backend cannot yet boot the public Python images the platform already publishes.)
- An Azure OpenAI deployment. No key: `az login` is enough.

**This creates a billable sandbox.** One, disposed at the end rather than left to the lifecycle timers — the check fails the run if it was not.

## Environment

| Variable | What it is |
| --- | --- |
| `ACAS_SANDBOX_ENDPOINT` | The control-plane endpoint for your region |
| `ACAS_SANDBOX_SUBSCRIPTION_ID` | The subscription holding the sandbox group |
| `ACAS_SANDBOX_RESOURCE_GROUP` | Its resource group |
| `ACAS_SANDBOX_GROUP` | The sandbox group name |
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI resource |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |

No `ACAS_SANDBOX_REGISTRY`: the image is named fully qualified in `agent.py`.

## Running it

```bash
az login
uv run samples/15_acas_codeact_host_tools/agent.py
```

To run the check the live job runs:

```bash
uv run samples/15_acas_codeact_host_tools/agent.py | tee out.txt
uv run python scripts/check_live_host_tools_dispatch_sample.py out.txt
```

## When it goes wrong

**`SandboxCapabilityNotSupported` at construction.** The backend does not declare `Capability.HOST_TOOLS`. ACAS and Docker do; `maf-sandbox-wslc` does not, and a registry against it is refused where the tool would have been built rather than at the first call.

**The program times out with no answer.** The launcher did not detach, or the guest has no `sh`/`nohup`. Check the image before anything else.

**A dispatched program that hangs leaves the sandbox behind.** [#375](https://github.com/sokolaidev/maf-extensions/issues/375) — a dispatched program that outlives its timeout keeps running and no kind can dispose the sandbox holding it. On this backend that is a billable sandbox nobody can reap, and it is a live defect now that two backends declare the capability. It is the reason the footer counts what it disposed.

**The total is wrong on the dispatch route.** Read the reply: the program is in the transcript. The usual cause is a program that hardcoded a price rather than calling for it, which the SKU-coverage line catches first.
