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

So the sample asks one question two ways, and **both of them run Python in the sandbox**:

1. **Dispatched.** The model writes a program; the program calls `unit_price` from inside the guest, over the transport, and computes.
2. **Direct.** `execute_code` with no registry, plus the same function as an ordinary tool. The model calls it for each price, then writes those numbers into the program it runs.

**Holding the interpreter constant is the whole design.** An earlier draft of this sample gave the dispatched route a Python interpreter and the other route nothing but the tool — and then reported that the second one got the arithmetic wrong. It did, reliably. But that measures whether a model can add decimals in one forward pass, which is samples 03 and 06's subject, not dispatch's. Attributing it to the transport was simply wrong, and the finding below only appeared once the confound was removed.

## What it actually costs, and what it actually buys

From a live run:

| | dispatched | direct |
| --- | --- | --- |
| total the program printed | `218.15` | `218.15` |
| wall clock | 12.35s | 4.09s |
| tokens | 1328 | 1665 |
| model messages | 3 | 5 |
| **prices the model wrote into code** | **none** | **all three** |

Both are correct, so correctness is not what a round trip buys. The cost is wall clock — about a second per call on this backend — and it is not paid in tokens; the two land close and which is cheaper depends on how much the guest program prints.

What it buys is the last row. On the direct route every price arrives as a tool result and the only road into the program is for the model to write it into the source:

```python
execute_code({"code": "a=3*41.75
b=7*12.4
c=2*3.05
print(f'{a+b+c:.2f}')"})
```

Those numbers are now in the transcript, the context window, and whatever logs either reaches. On the dispatched route the model writes none of them, and cannot: the program is written *before* any dispatch can answer, so there is no value to embed. The prices travel guest → host → guest without the model as courier.

**The claim is exactly that narrow.** It is not "the prices stay out of the transcript" — a dispatched program may `print` one, and in the run above it did, which is why `prices the model received` is reported on its own line and says so. What the transport decides is whether the *model* has to handle the value. That is the leg a `sink` declaration describes, and it is the reason `Capability.HOST_TOOLS` is a policy question rather than a convenience.

## Why the prices have to be secret

`PRICES` lives in the host process. It is not in the image, not in the file store, and the sandbox has no egress — with no `egress_allow`, the guest initiates nothing at all. A program that wants a price has exactly one road to it.

That is what makes the measurement mean something. If the model could guess a plausible price the sample would measure nothing: a run that skipped every dispatch would still print a number, and the number would look fine. Here a skipped dispatch is a wrong total, and the check says so.

The quantities are awkward, but that no longer carries any weight: both routes compute with an interpreter, so neither is being asked to do mental arithmetic. What matters is only that the *prices* are unguessable, which is what makes a skipped dispatch visible as a wrong total.

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

Both routes run Python, so both are held to the same two things, and both are properties of machinery rather than prose:

- **The program printed the exact total** — read from the framework's record of what `execute_code` returned.
- **Who wrote the prices into a tool call** — none on the dispatched route, all of them on the direct one. Both halves are structural: one is impossible, the other is forced.

**Wall clock and tokens are recorded and never bounded**, and what the model *said* is never read at all. That is deliberate, and it was earned twice. An earlier check required the model's reply to carry the total, and a live run printed `218.15` from the sandbox while the reply said `239.75` — it would have failed a release and blamed the sandbox. An earlier draft also leaned on the direct route staying wrong, which would have gone red the day it stopped being.

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

**A program did not print the total.** This fails the run, for either route. This is the one failure that fails a run. Read the transcript: the program is in it. The usual cause is a program that hardcoded a price rather than calling for it, which the SKU-coverage line catches first.

**The dispatched route reports prices the model wrote into code.** This should be impossible, and the check fails on it. Either the program is being written after a dispatch answered, or that line has stopped measuring tool-call arguments only.
