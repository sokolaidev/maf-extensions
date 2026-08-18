# 15 — A program calling back into the host (ACA sandbox)

Every other sample sends things *in* to a sandbox and takes results *out*. This one opens the other direction: model-written code, running inside a microVM, calling functions that execute in the host process.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
              ^ maf_sandbox_codeact calls the router        |
              +------ a host function, dispatched ----------+
```

**Read [sample 10's README](../10_inprocess_host_tools/README.md) first.** It is the configuration half — `@sandbox_tool`, the registry gate, the aggregate, the seal, and both ways a router refuses the whole kind — all answered at attach, with no sandbox and no model. This page is the traffic half ([#302](https://github.com/sokolaidev/maf-extensions/issues/302)).

## The workload, and why it looks like a database

[#133](https://github.com/sokolaidev/maf-extensions/issues/133) is specific about what needs measuring:

> Each dispatch is at minimum one round trip — on a remote backend, an HTTP call — so a **call-heavy** program can cost more round trips than the direct tool calling this pattern exists to replace.

A call-heavy workload is one where the calls *depend on each other*, so three tables and a question that cannot be answered without walking them in order:

```
state name  ->  state id  ->  store ids  ->  sales rows  ->  product names
```

Two states, five stores, three products: **four stages, twelve lookups at best**. Acts 2 and 3 answer it twice, and **both run Python in the sandbox** — the only thing that differs is where the lookups happen. Holding the interpreter constant is what keeps this a measurement of dispatch rather than of CodeAct, which samples 03 and 06 already cover.

The data lives in the host process, is not in the image or the file store, and the sandbox has no egress. A program that wants any of it has one road.

## What the stages cost

From a live run:

| | dispatched | direct |
| --- | --- | --- |
| lookups | 21 | 12 |
| **model round trips** | **3** | **5** |
| tool calls per model round trip | `[1, 1, 1]` | `[2, 2, 5, 3, 1]` |
| wall clock | 48.07s | 14.60s |
| tokens | 6,270 | 6,217 |
| **sales figures the model wrote into code** | **0** | **12** |
| state totals the program printed | 2 of 2 | 2 of 2 |

Read `[2, 2, 5, 3, 1]` — the first four entries *are* the walk, and the last is the program. Two state ids, two store lists, five sales rows, three product names, then one `execute_code`. On the dispatched side every entry is `execute_code`; the lookups do not appear there at all, because they never reach the model. **Direct tool calling batches within a stage and never across one**, because it cannot ask for a store's sales until it has been told the store ids. So it pays a model round trip per stage.

Dispatch resolves the whole walk inside one program and pays a *transport* round trip per call instead — serially, always, with no batching available at any layer ([#439](https://github.com/sokolaidev/maf-extensions/issues/439)).

Both answers are correct, so correctness is not what a round trip buys. What it buys is the last row.

## Where the data ends up, which is the real trade

On the direct route every figure arrives as a tool result and the only road into the program is for the model to write it into the source:

```python
execute_code({"code": "wa = 1240.50 + 310.25 + 88.10 + 655.75 + ..."})
```

Those values are now in the transcript, the context window, and whatever logs either reaches — and they stay there, turn after turn. That is a **ceiling** long before it is a bill: context is capped, and an agent that compacts to stay under it is paying a summarisation call and losing fidelity every time.

Dispatched, the model writes none of them and *cannot*: the program exists before any dispatch can answer, so there is no value to embed.

**The claim is exactly that narrow.** It is not "the data stays out of the transcript" — a dispatched program is free to `print` a figure. What the transport decides is whether the *model* has to be the courier, which is the leg a `sink` declaration describes.

## The dispatch cap is real, and this workload exceeds the default

`HostToolRegistry` allows 16 dispatches a run. This walk needs up to 21 written naively, and live runs used 18 to 29 — so the default does not fit it. A program that exhausts the budget does not raise: it comes back with a partial answer and `Need more host-tool budget to complete the table` in its output, which is a wrong summary rather than a failure.

So the sample budgets for it out loud. A call-heavy host has to, and the arithmetic is not the whole story: the *model* writes the program, and a program that looks up a product name per sales row asks twelve times where a caching one asks three.

## What a dispatch proves here, and not on a local backend

ACAS's `exec` is blocking and timeout-bounded. The guest shim publishes a request file and then **blocks**, waiting for a response the host can only write while the program is still running. If the launcher had not detached, the `exec` would own the program, the host would never get to write, and it would sit there until its deadline.

So a dispatch that is answered at all proves the launcher detached and the supervisor took over — that is the precondition of act 2 producing any number, and the reason [#365](https://github.com/sokolaidev/maf-extensions/issues/365) treats `Capability.HOST_TOOLS` as a claim about `exec` rather than about a method.

**What the sample cannot show is the other half of that.** #302 wants the timeout to be a supervisor bound rather than an exec bound, and #133 that *"a wedged guest needs its own ceiling"*. There is no such ceiling yet: [#375](https://github.com/sokolaidev/maf-extensions/issues/375) is the sandbox nobody disposes, and [#437](https://github.com/sokolaidev/maf-extensions/issues/437) is the kill that reaches only the interpreter's own pid.

## What the runs leave in the guest

Act 5 enumerates the work root with `list_dir`, which needs `Capability.FILES_LIST` — ACAS declares it and Docker does not, so this act is one more reason the sample belongs on this backend.

A fresh directory per run is real, and on a warm sandbox it is not hypothetical: every run of both acts is still there when act 5 looks. What is *not* there is any cleanup. The transport says so itself — *"Nothing in the protocol deletes"* — and [#438](https://github.com/sokolaidev/maf-extensions/issues/438) is the general case: no kind is obliged to clean up, and the protocol gives it nothing to clean up with.

Sixty-three transport files survived one run of this sample. That is **three per served call** — the id the caller claimed with an exclusive create, the request, and the answer — so the sample reports the answered subset alongside the total, because a bare file count reads as three times the traffic there was.

Disposing the sandbox is the only thing that removes them, which is what the footer does.

## What the check enforces

Seven live runs decided this. The lookup count moved between 18 and 29, wall clock between 35s and 87s, dispatched round trips between two and four. What did not move is what is asserted:

- **Both programs printed both state totals** — read from the framework's record of what `execute_code` returned, so an interpreter produced them.
- **Direct needed more model round trips than dispatch**, and its shape shows at least four batches — one per stage.
- **Who carried the figures**: none dispatched, all of them direct. Both halves are structural — one is impossible, the other is forced.
- **The runs left transport files behind,** and at least one of them is an answered call. Zero of either would mean the enumeration looked somewhere the transport does not write.

Wall clock, tokens and lookup counts are **recorded and never bounded**, and what a model *said* is never read. Every line the check reads carries the `[measured]` tag at the left margin ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)); `quoted()` prefixes any tagged line inside a model's reply with `> `, so prose that tries to answer for the host is visibly not the host answering.

## Prerequisites

- An Azure subscription with the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes) preview enabled, and a sandbox group.
- `mcr.microsoft.com/devcontainers/python:3.13-bookworm` available to that group — sample 14's image, so a group set up for that one already has it. The launcher is POSIX shell and the shim is Python, so the guest needs **`sh`, `nohup`, `mkdir`, `mv`, `printf` and `python3`** — the launcher creates the working directory, redirects output and renames the exit marker into place. A distroless or Windows image cannot serve this whatever it declares.
- An Azure OpenAI deployment. No key: `az login` is enough.

**This creates two billable sandboxes**, one per route, both disposed at the end rather than left to the lifecycle timers — the check fails the run if they were not.

Two rather than one because nothing deletes a run's transport files. Sharing a sandbox would leave the dispatched route's responses — every id, store list and sales row — readable on the guest filesystem for the direct route's program, which is a second road to the same data that this sample never measures and the comparison assumes does not exist. Cleaning up between them is not available: there is no way to delete a guest file, which is the same gap ([#438](https://github.com/sokolaidev/maf-extensions/issues/438)) act 5 reports.

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

**`Need more host-tool budget to complete the table`.** The dispatch cap. `DISPATCH_CAP` in `agent.py` is set above the worst observed run; a bigger dataset needs a bigger budget, and the arithmetic in that comment is the place to start.

**`SandboxCapabilityNotSupported` at construction.** The backend does not declare `Capability.HOST_TOOLS`. ACAS and Docker do; `maf-sandbox-wslc` does not, and a registry against it is refused where the tool would have been built rather than at the first call.

**A program times out with no answer.** The launcher did not detach, or the guest has no `sh`/`nohup`. Check the image before anything else — and note #375: nothing disposes the sandbox that program is still running in.

**The dispatched route wrote a figure into code.** The check fails on this and it should be impossible: the program is written before any dispatch can answer. Either the model was handed a value somewhere it should not have been, or that line has stopped measuring tool-call arguments only.

**`no such directory: '<run>/host_tools'`.** A run that never dispatched — act 3's, since without a registry the kind uses the flat run directory it always has. Act 5 catches that by its own type; anything else should still be heard.
