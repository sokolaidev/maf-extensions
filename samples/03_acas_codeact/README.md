# 03 — compute an answer in an ACA sandbox

A one-turn agent. It hands the agent one tool, `execute_code`, asks it to compute the 100th Fibonacci number with a Python program, prints the answer, and deletes the sandbox.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
              ^ maf_sandbox_codeact calls the router
```

[`agent.py`](agent.py) is the `app` box — the one the package READMEs describe but never show.

The task has exactly one right answer, and that is what makes the run worth watching. `354224848179261915075` is not something to eyeball, and this sample runs whether or not the model can produce it from memory: the number in the reply is what a Python interpreter running inside a microVM-isolated sandbox (**T2**) computed, not what the model predicted (**T0**). A question with a range of acceptable answers would prove much less.

## Prerequisites

Read these first; none of them is quick to arrange halfway through.

- **An Azure subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a **sandbox group** in it.
- **An Azure OpenAI deployment of a reasoning model** — `gpt-5.4` and its siblings work. This is not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the very first call with `400 — Encrypted content is not supported with this model` on `param: include`. That error names neither this sample nor the setting behind it, so it is worth choosing correctly rather than debugging later.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable sandbox.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that — but they run in sequence rather than together, so a run killed mid-turn leaves a sandbox running for `auto_suspend_seconds` (60 by default), suspended for `auto_delete_seconds` (600) after that, and gone about eleven minutes from the last call. Reclaiming it yourself is the plan; the timers are what happens when the process dies before it can.

`python-3.13` is one of the prebuilt images the service keeps Ready for every sandbox group, so the sample names it and imports nothing. For prototyping that is the whole image story; production replaces it with a hardened image you build, own and import into the sandbox group yourself — minimal, digest-pinned, scanned, rebuilt on your patch cadence — the import path [the `maf-sandbox-acas` README](../../packages/maf-sandbox-acas/README.md) and [its import script](../../packages/maf-sandbox-acas/scripts/import_disk_image.py) document.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

`azure-identity` and `agent-framework-core` arrive as dependencies. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `ACAS_SANDBOX_ENDPOINT` | Sandbox group data-plane endpoint, `https://management.<region>.azuredevcompute.io` |
| `ACAS_SANDBOX_SUBSCRIPTION_ID` | Subscription holding the group |
| `ACAS_SANDBOX_RESOURCE_GROUP` | Resource group holding the group |
| `ACAS_SANDBOX_GROUP` | Sandbox group name |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment name of the chat model — a reasoning model, per the prerequisites |

There is no `ACAS_SANDBOX_REGISTRY` here, unlike sample 01: `agent.py` names a prebuilt image by its bare service-provided name (`python-3.13`), which carries no tag — a bare name resolves against the sandbox group's catalogue, and only a `repository:tag` reference would be qualified against a registry.

With any of these unset the program says which and exits non-zero, rather than running. That is deliberate: `make_codeact_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The first call is slow — the sandbox is created and booted before the interpreter runs. `agent.py` prints the model's reply, then what `execute_code` returned, then the disposal line — so what you see looks something like this:

```
354224848179261915075

== Program output as execute_code returned it ==

  stdout:
  354224848179261915075

  [measured] programs whose output came back from the sandbox: 1

  [measured] Disposed 1 sandbox(es).
```

That block is one real run. This model answered with the number alone; another will wrap it in a sentence. What does not vary is the number and the disposal line.

The wording around the number is the model's and varies run to run; the model is instructed to report the tool's answer verbatim, not to paraphrase, round, or recompute it. What tells you the number came from a real run rather than the model reciting a well-known sequence is the block below it. `354224848179261915075` is a constant, and a model that never ran anything can write it — so the live check reads the copy inside `== Program output as execute_code returned it ==`, which is the interpreter's own stdout, recorded by the framework beside the call ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)). The `[measured]` lines are the sample vouching for a number, and the model's reply is filtered before printing so a line of it starting with that tag comes out quoted (`> [measured] …`) — a reply can write the heading and cannot close the block.

`[measured] Disposed 1 sandbox(es).` is the other half: it only prints once `execute_code` has actually created and torn down a sandbox, and a `Disposed 0` would mean the model answered without running anything.

## Troubleshooting

**`No sandbox backend: execute_code was not attached.`** — the router holds no backend at all, which is the only case `make_codeact_tools` answers with an empty list. Unset `ACAS_SANDBOX_*` variables do not reach here: the program checks those first and exits with a message naming them. A backend that is registered and cannot serve the workload does not reach here either — every mismatch raises: an isolation floor at construction, and the rest at attach — a missing capability, a transfer ceiling below what the workload asks, or an egress promise the backend will not make.

**`SandboxBackendNotPermitted` at startup** — `SandboxRouter`'s default minimum-isolation floor is `Isolation.MICROVM`, and it refuses any backend below that. `AcasSandboxBackend` declares `Isolation.MICROVM`, exactly the floor, so this only appears if you swapped the backend for a container- or process-isolated one — and it means the swapped-in backend needs `min_isolation` lowered explicitly, not left implicit. It raises at construction rather than at first call, on purpose.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` requires: run a command and take a file in. `AcasSandboxBackend` declares both, so this only appears against a swapped-in backend that declares less.

**A disk image that cannot be resolved** — the image was named but the group cannot find it, for one of two reasons. A `repository:tag` reference was never imported into the sandbox group — the reference in the error has to match the one the import step used exactly. Or a bare name the service's catalogue does not hold: `python-3.13` is what the service provides today, but the catalogue is per group and the service owns its contents, so a group whose catalogue does not carry the name fails at boot with the catalogue in the message (`It provides: …`) — `aca sandboxgroup disk list-public` is the live version of the same list.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. See the prerequisite above; nothing about the sandbox is involved, and the run fails before one is created.

**The tool's answer says "printed nothing"** — `execute_code` only returns what the program printed; there is no REPL echo. A model that wrote an expression instead of a `print(...)` call gets exactly this sentence back, and it usually self-corrects on the next call.
