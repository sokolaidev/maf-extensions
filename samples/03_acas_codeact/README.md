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
- **The sandbox image imported into the sandbox group as a disk image.** Unlike the Bicep samples, there is nothing to build or push here: `mcr.microsoft.com/devcontainers/python:3.13-bookworm` is already a public image, so importing it is the only step. A sandbox boots from a disk image, which is a different namespace from the registry an image lives in, so pointing the sample at the reference is not enough on its own.

  ```bash
  curl -fsSL https://aka.ms/aca-cli-install | sh                       # PowerShell: irm https://aka.ms/aca-cli-install-ps | iex
  export AZURE_SUBSCRIPTION_ID=<sub-id> ACA_RESOURCE_GROUP=<sandbox-group-rg> ACA_SANDBOX_GROUP=<group>
  aca sandboxgroup disk create --image mcr.microsoft.com/devcontainers/python:3.13-bookworm --name python-3-13
  ```

  The portal does the same thing without installing anything, which suits a one-off import: [sandboxes.azure.com](https://sandboxes.azure.com) → your sandbox group → **Disk Images** → **Create**, paste the reference into **Base Image URL**, and leave **Registry Authentication** on *No authentication (public registry)* — MCR needs none. Either way the disk image is a snapshot: the portal states that changes to the source container image do not affect disk images already created, so moving the tag later means importing again.

  Both the CLI and the service are in preview and Microsoft says the command surface may change, so `aca sandboxgroup disk create --help` is the authority if a flag here does not match. `aca sandboxgroup disk list-public` lists the sandbox group's public presets, and one of them, `python`, may already carry a usable interpreter without an import step at all — check what Python version it actually has before relying on it, since a public preset moves independently of this sample and nothing here pins it.
- **An Azure OpenAI deployment of a reasoning model** — `gpt-5.4` and its siblings work. This is not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the very first call with `400 — Encrypted content is not supported with this model` on `param: include`. That error names neither this sample nor the setting behind it, so it is worth choosing correctly rather than debugging later.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable sandbox.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that — but they run in sequence rather than together, so a run killed mid-turn leaves a sandbox running for `auto_suspend_seconds` (60 by default), suspended for `auto_delete_seconds` (600) after that, and gone about eleven minutes from the last call. Reclaiming it yourself is the plan; the timers are what happens when the process dies before it can.

`mcr.microsoft.com/devcontainers/python:3.13-bookworm` is a dev-container image — it carries a full toolchain this workload never touches, not just a Python interpreter, so it is bulkier than the sandbox strictly needs. It is used here because it is the only standard MCR image family at Python 3.13; a minimal Azure Linux Python image becomes the better choice the day that family ships 3.13 too. Either way this reference is for prototyping the sample: production replaces it with a hardened image you build and own — minimal, digest-pinned, scanned, rebuilt on your patch cadence — imported into the sandbox group the same way.

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

There is no `ACAS_SANDBOX_REGISTRY` here, unlike sample 01: `agent.py` names the image by its full MCR reference, and a fully-qualified reference is passed through untouched rather than qualified against a registry.

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

**`No sandbox backend: execute_code was not attached.`** — the router has no usable backend. Check the `ACAS_SANDBOX_*` variables.

**`SandboxBackendNotPermitted` at startup** — `SandboxRouter`'s default minimum-isolation floor is `Isolation.MICROVM`, and it refuses any backend below that. `AcasSandboxBackend` declares `Isolation.MICROVM`, exactly the floor, so this only appears if you swapped the backend for a container- or process-isolated one — and it means the swapped-in backend needs `min_isolation` lowered explicitly, not left implicit. It raises at construction rather than at first call, on purpose.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` requires: run a command and take a file in. `AcasSandboxBackend` declares both, so this only appears against a swapped-in backend that declares less.

**A disk image that cannot be resolved** — the image was named but never imported into the sandbox group. See the prerequisite above; the error names the reference it looked for, and that reference has to match the one the import step used exactly.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. See the prerequisite above; nothing about the sandbox is involved, and the run fails before one is created.

**The tool's answer says "printed nothing"** — `execute_code` only returns what the program printed; there is no REPL echo. A model that wrote an expression instead of a `print(...)` call gets exactly this sentence back, and it usually self-corrects on the next call.
