# 14 — CodeAct with files in and files out (ACA sandbox)

[Sample 08](../08_docker_codeact_files/) with the backend swapped and nothing else rethought. Same task, same `sales.csv`, same two channels, same sink. What differs is listed below.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
              ^ maf_sandbox_codeact calls the router
```

**Read [sample 08's README](../08_docker_codeact_files/README.md) first.** It explains both file channels, what the sink is for and where it must not point. This page covers only what is different here, which is the backend, the isolation floor, the shape of the pull, and the bill.

## Why a second backend for this

Everything that reads a file *back out* of a sandbox has been Docker: sample 07 through a hand-written kind, sample 08 through the packaged one. Docker is the local backend, where landing an artifact is a `docker cp` between two processes on the same machine.

The set makes a careful *"same workload, one line lower"* claim for running a command — samples 01, 02, 05 and 09 are one Bicep workload across four backends — and has had no counterpart for the pull surface. So nothing demonstrated that a workload landing artifacts is portable in the way [`docs/design/files-out.md`](../../docs/design/files-out.md) says it is. This sample is that counterpart ([#300](https://github.com/sokolaidev/maf-extensions/issues/300)).

The application code is the evidence. `make_recording_sink`'s body is copied from sample 08 line for line, and that is the point rather than laziness: a sink is host-side code that never learns which backend produced the bytes it was handed. If it had to know, the surface would not be portable and this sample would be making the opposite case.

## What is new against sample 08

- **`AcasSandboxBackend` in place of `DockerSandboxBackend`**, reading four `ACAS_SANDBOX_*` variables where the Docker one read none — it drives a local daemon, this one a data-plane endpoint.
- **No `min_isolation`.** Sample 08 opts *down* to `Isolation.CONTAINER`, and its own comment says the floor should be chosen against the provenance of the file store, because a store turns the program's input into something other than source the model wrote. Here that costs nothing to answer: the router's default floor is `Isolation.MICROVM` and this backend meets it exactly, so the same workload runs a rung higher without the application asking for it.
- **`await backend.aclose()`** on the way out. There is an HTTP client to close; the Docker backend has none.
- **It creates a billable sandbox.** Sample 08 costs a container on your own machine.

Everything else is the same file. Diff the two and what changes is the three code rows above — the backend swap taking its four `ACAS_SANDBOX_*` variables and their `require_env_vars` check with it — plus the `THREAD_ID` that keys this sample's sandbox and the dependency block, where `maf-sandbox-docker` becomes `maf-sandbox-acas` and a comment explains why the two Azure entries are there. Those two entries themselves are the same in both samples: each imports `DefaultAzureCredential` for its model client, and neither gets it from its backend.

## What the pull actually does here

`FILES_OUT` on this backend is a **stat, then a read**, each an HTTPS call to the sandbox group's data plane. That ordering is confinement rather than an optimisation, and it is worth knowing three things about it:

**The read follows symlinks** — in the parent directories as much as in the final component — so every one of them is classified before a byte moves. A symlink is refused whether or not its target would have resolved somewhere legitimate.

**A cap is a refusal, not a truncation.** This backend declares 32 MiB per file and 128 MiB per transfer, and it checks the size again against what arrived, because the SDK buffers the whole response rather than exposing an incremental hook. A file the service reports no size for is refused rather than read. Those are the backend's ceilings, and they are not what a run of this sample hits first — the workload's own are tighter, and the section at the end says which.

**One residual stays open, and is documented rather than hidden.** A guest that swaps the stat-ed file for a symlink between the two calls wins: the service follows it, and this API has no no-follow read. An atomic no-follow read or a frozen guest filesystem would close it; nothing available here does. On Docker the equivalent question has a different answer, which is the sort of thing only running the same workload on both surfaces makes visible.

## What to watch

**The summary lands as `summary.md`, not `<run-id>/summary.md`** — the same distinction sample 08 draws, and the one place it is visible from outside is on disk in `out/`. Here the run directory lives in a microVM in Azure and the delivered name still comes back bare.

**Warm reuse is real on this backend, not hypothetical.** `acquire` is get-or-create, and this backend's sandboxes persist between calls under lifecycle timers rather than dying with the process. Every call still gets a fresh directory inside the sandbox, which is what stops a file deleted from the file store between rounds from being read as current by the next program.

**The last line of output is the host's, not the model's**, and both of the last two are tagged `[measured]`. `out/` may still hold a summary an earlier run left there, so listing the directory would report a delivery that did not happen — the sample prints what the sink actually took *this turn*, as JSON, because a comma is legal in an artifact name. The tag is what stops a model writing that line for it; the reply is filtered before printing, so a line of it starting with the tag comes out quoted ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)).

## Prerequisites

Read these first; none of them is quick to arrange halfway through. They are sample 03's, unchanged — same service, same image.

- **An Azure subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a **sandbox group** in it.
- **The CodeAct image imported into that sandbox group as a disk image**: `mcr.microsoft.com/devcontainers/python:3.13-bookworm`. Nothing to build or push — it is already a public image, so importing it is the only step, and [sample 03's README](../03_acas_codeact/README.md#prerequisites) carries the command line and the portal route. If you have run sample 03, this is already done.
- **An Azure OpenAI deployment of a reasoning model.** Not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the first call with `400 — Encrypted content is not supported with this model`.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable sandbox.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that — but they run in sequence rather than together, so a run killed mid-turn leaves a sandbox running for `auto_suspend_seconds` (60 by default), suspended for `auto_delete_seconds` (600) after that, and gone about eleven minutes from the last call.

## Environment

| Variable | What it is |
|---|---|
| `ACAS_SANDBOX_ENDPOINT` | Sandbox group data-plane endpoint, `https://management.<region>.azuredevcompute.io` |
| `ACAS_SANDBOX_SUBSCRIPTION_ID` | Subscription holding the group |
| `ACAS_SANDBOX_RESOURCE_GROUP` | Resource group holding the group |
| `ACAS_SANDBOX_GROUP` | Sandbox group name |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment name of the chat model — a reasoning model, per the prerequisites |

No `ACAS_SANDBOX_REGISTRY`: `agent.py` names the image by its full MCR reference, and a fully-qualified reference is passed through untouched rather than qualified against a registry.

With any of these unset the program says which and exits non-zero, rather than running. That is deliberate, and the failure it avoids is specific: `make_codeact_tools` returns an **empty list** when the router holds no backend at all, so a run configured with nothing does not crash — it builds an agent with no tools, which answers from the model alone, and that looks exactly like success.

Only that case is quiet. A backend that *is* registered and cannot serve the workload is refused loudly instead, and every mismatch raises: an isolation floor at `SandboxRouter(...)`, and the rest out of `make_codeact_tools` itself — `SandboxCapabilityNotSupported` for a missing capability, and its siblings for a transfer ceiling below what the workload asks or an egress promise the backend will not make.

## Running it

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing on this page to keep in step with it — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

The first call is slow: the sandbox is created and booted before the interpreter runs. Expected shape — the grand total over `sales.csv` is **1124**:

```
The grand total is 1124. The per-region summary was saved as summary.md.

  [measured] Disposed 1 sandbox(es).
  [measured] Delivered this turn into out/: ["summary.md"]
```

`out/summary.md` then holds the per-region table: north 390, south 200, east 84, west 450. The wording of the reply is the model's and varies run to run; the two tagged lines are the sample's own report and do not.

Sample 08 prints the same two lines over the same numbers. That is what makes a red on one side while the other is green a statement about the backend rather than about the workload — and it is why one script, [`scripts/check_live_codeact_files_sample.py`](../../scripts/check_live_codeact_files_sample.py), checks both.

## When it goes wrong

Sample 08's [refusals](../08_docker_codeact_files/README.md#when-it-goes-wrong) — a file outside the listing, a name that traverses, a declared output never written — are the kind's and read the same here. These are this backend's:

**A disk image that cannot be resolved** — the image was named but never imported into the sandbox group. The error names the reference it looked for, and that reference has to match the one the import step used exactly.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` was configured to need. With both channels wired that is `EXEC`, `FILES_IN` and `FILES_OUT`; this backend declares all three plus `FILES_LIST`, so this only appears against a swapped-in backend that declares less. Note which way it fails: the whole kind is refused at attach, so `execute_code` is absent rather than quietly thinner.

**`SandboxTransferCapExceeded` on the way out** — the program wrote something larger than **8 MiB**, and the refusal names *this workload's* ceiling rather than the backend's. Two limits stack here and the tighter one binds: the sample passes no `files_out=`, so the CodeAct kind's default applies — 8 MiB per file, 32 MiB in total, 8 files — and collection is checked against the spec's, not the backend's. The backend's own 32 MiB/128 MiB is a ceiling above that and never binds in this sample. Either way it is a refusal and not a truncation.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. Nothing about the sandbox is involved, and the run fails before one is created.
