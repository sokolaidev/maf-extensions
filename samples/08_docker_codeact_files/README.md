# 08 — CodeAct with files in and files out (Docker)

The first sample where the agent is **given** a file and **hands one back**. Samples 03, 04 and 06 run `execute_code` with nothing to read and take only stdout; this one wires both of the kind's file channels and asks a question that needs each.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ maf_sandbox_codeact calls the router
```

Sample 07 also lands an artifact, and gets there by defining a kind inline against the protocol — it is the worked example of *writing* a kind. This sample changes no workload code at all: the same packaged `execute_code` a host already has, with three constructor arguments it did not pass before.

## What is new against sample 06

Two channels, three arguments to `make_codeact_tools` — the second channel takes two of them, and they are not separable:

- **`file_store=store`** adds `files`. Each named file is read from the store and written into the program's working directory under its own name. **The caller's listing is the authority**: only a path returned by `CallerContext.list_files` is ever shared, so a name the model invented — or read out of a file it was given — has nowhere to go.
- **`output_sink=...` *and* `outputs=CodeactOutputs.DECLARED`** together add `outputs`. The model says what its program will write *before* it runs; names are validated and capped up front, and one declared but never written is reported back by name rather than dropped. Produced files never return as bytes — they go to the sink, and the model gets the sentence the sink returned.

The mode is not optional once a sink is passed. `outputs` defaults to `CodeactOutputs.NONE`, and a sink under that default is refused where you wrote it rather than ignored at run time:

```
ValueError: execute_code: an output sink was supplied with outputs='none', so nothing would ever be landed in it. Pass an outputs mode, or drop the sink.
```

Everything else is sample 06 unchanged: the same image, the same backend, the same `Isolation.CONTAINER` floor, the same model reached with `DefaultAzureCredential`.

## What to watch

**The program opens `sales.csv` by that name.** Every call gets a fresh directory inside the sandbox and the shared file is written into it, so a bare relative name is what the program uses — no run id, no absolute path. That freshness is load-bearing rather than hygiene: `acquire` is get-or-create, so one sandbox serves every call in a conversation, and without a per-call directory a file deleted from the file store between rounds would still be there for the next program to read as current. A nested store path works the same way — seed the store with `data/sales.csv` and the program opens `data/sales.csv`.

**The summary lands as `summary.md`, not `<run-id>/summary.md`.** Inside the sandbox the file lives under the run directory; the delivered name is a separate field. This is the one place that distinction is visible from outside — on disk, in `out/`.

**The last line of output is the host's, not the model's.** The model is told a sentence by the sink; the sample prints, as JSON, what the sink actually took *this turn* — JSON because a comma is legal in an artifact name, so a comma-joined list would read one delivery back as two. That distinction is the whole value of the line: `out/` may still hold a summary an earlier run left there, so listing the directory would report a delivery that did not happen. A turn that computes the right total and writes nothing is exactly the failure this sample exists to make visible, and it is not visible from the transcript alone.

## Where the sink points, and why it is the interesting decision

`make_recording_sink` writes under this directory's `out/`. The agent's file store is a separate `InMemoryAgentFileStore`, and **the two are deliberately not the same place**.

That matters more here than for any other kind, because these bytes were authored by model-written code. A host that points the sink at the store the agent's own file tools write to has handed that code an unapproved `file_access_write`; one that lets it overwrite has given it a way to influence a *different* tool on the next call. Point the sink somewhere the agent cannot otherwise reach — which is what this sample does, and the reason `out/` is a plain directory on the host rather than another store.

`Artifact.media_type` is always `None` on both output roads. Not an omission: the kind does not know what a model-written program produced, and a type read out of the guest would be the sandbox telling the host how to handle its own content — which a sink may act on to choose inline rendering. Decide by extension and your own policy if you need to.

## The other road, and why this sample takes this one

`CodeactOutputs.MANIFEST` has the program write `outputs.json` listing what it produced, for a program whose output names it can only know once it has read its input. It is the right road there and the wrong one here: the names become the guest's rather than the model's, settled after the fact, so nothing can report a file that was promised and never written. This sample takes `DECLARED` because that diagnostic is half of what the channel is for.

## Prerequisites

- A Docker-compatible engine (Docker Desktop, colima, podman with the Docker socket).
- An Azure OpenAI deployment. No key: authentication is `DefaultAzureCredential`, so an `az login` session or a federated CI credential is enough.

Nothing is built. The image is `mcr.microsoft.com/devcontainers/python:3.13-bookworm`, pulled the way any `docker run` pulls it — a convenience for a sample, and bulkier than this workload needs. A production deployment supplies a hardened image of its own through the same `image` field; nothing else in the wiring changes.

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name |

## Running it

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

Expected shape — the grand total over `sales.csv` is **1124**:

```
The grand total is 1124. The per-region summary was saved as summary.md.

  [measured] Disposed 1 sandbox(es).
  [measured] Delivered this turn into out/: ["summary.md"]
```

`out/summary.md` then holds the per-region table: north 390, south 200, east 84, west 450.

The wording of the first line is the model's and varies run to run. The two tagged ones are the sample's own report of what the router disposed and what the sink took, and the live check reads *those two* off the tag — a model writes into the same stream, so a reply saying "Disposed 1 sandbox(es)." would otherwise answer for the router. It also looks for the grand total anywhere in the transcript, which in a healthy run means in the model's reply; that one is a claim about the answer reaching the model, and the evidence a program ran is the landed file, whose four region totals are that grand total decomposed. The reply is filtered before printing, so a line of it starting with that tag comes out quoted, `> [measured] …` ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)).

[Sample 14](../14_acas_codeact_files/) is this sample on a real Azure sandbox — same task, same data, same two lines, a different backend underneath — and one script checks both.

A nested declared name works too — `reports/summary.md` lands at `out/reports/summary.md`, because the sink makes each destination's own parent. Nesting cannot climb out: names are validated relative before they arrive, and the sink resolves each destination and refuses one that lands outside `out/` — which lexical validation alone would not catch, since a symlink already sitting in `out/` carries a write wherever it points.

The tool result behind that reply — which the sample does not print, because the model's answer is what a host would show — looks like this, and is worth knowing the shape of:

```
stdout:
1124

Saved:
- summary.md (96 bytes), in out/
```

The bullet is the sink's own sentence. That is the only thing about the landing the model is told: no host path, and nothing guest-derived.

## When it goes wrong

Both refusals below are ordinary tool results, not exceptions — the turn continues and the model can correct itself, which is the point of reporting them by name.

**A file that is not in the listing.** The listing is the authority, so the refusal shows what is actually visible rather than leaving the model to guess again:

```
Error: 'not-there.csv' is not in this tool's file listing, so it was not shared. Files visible here: sales.csv.
```

A name that *traverses* never reaches the listing check — the name validator refuses it first, naming the rule it broke:

```
Error: '../secrets.env' cannot be shared — artifact name '../secrets.env' contains a '..' traversal segment
```

Note what that refusal does **not** carry: the listing. Telling a caller which names exist, in answer to a name that tried to leave the store, is an invitation to keep trying spellings until one lands.

**A declared output that was never written.** The program ran, exited cleanly, and a name it promised is not there — reported by name, with whatever *was* written still saved:

```
Not written by the program, so not saved: missing.md. Write each file into the working directory before the program exits.
```

This is the case `MANIFEST` cannot report, and the reason this sample uses `DECLARED`.

**Nothing printed.** `execute_code` returns stdout; there is no REPL echo, so a program ending in a bare expression comes back with a sentence saying so.
