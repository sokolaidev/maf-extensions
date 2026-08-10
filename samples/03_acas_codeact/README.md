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

  Both the CLI and the service are in preview and Microsoft says the command surface may change, so `aca sandboxgroup disk create --help` is the authority if a flag here does not match. `aca sandboxgroup disk list-public` lists the sandbox group's public presets, and one of them, `python`, may already carry a usable interpreter without an import step at all — check what Python version it actually has before relying on it, since a public preset moves independently of this sample and nothing here pins it.
- **An Azure OpenAI deployment of a reasoning model** — `gpt-5.4` and its siblings work. This is not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the very first call with `400 — Encrypted content is not supported with this model` on `param: include`. That error names neither this sample nor the setting behind it, so it is worth choosing correctly rather than debugging later.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable sandbox.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that, but a run that is killed mid-turn can still leave one active for up to ten minutes.

`mcr.microsoft.com/devcontainers/python:3.13-bookworm` is a dev-container image — it carries a full toolchain this workload never touches, not just a Python interpreter, so it is bulkier than the sandbox strictly needs. It is used here because it is the only standard MCR image family at Python 3.13; a minimal Azure Linux Python image becomes the better choice the day that family ships 3.13 too.

## Install

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-acas maf-sandbox-codeact agent-framework-openai
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

```bash
python agent.py
```

The first call is slow — the sandbox is created and booted before the interpreter runs. The model writes its own prose around it, but the answer it is reporting looks like this:

```
stdout:
354224848179261915075

Disposed 1 sandbox(es).
```

Only the surrounding prose is the model's. `stdout:\n354224848179261915075` is `execute_code`'s own result format, unmodified — no stderr and no exit code because the program printed exactly one line and exited cleanly. `_format_result` in `maf-sandbox-codeact` is what renders it, and the two-line shape is the same whatever the program did.

## Troubleshooting

**`No sandbox backend: execute_code was not attached.`** — the router has no usable backend. Check the `ACAS_SANDBOX_*` variables.

**`SandboxBackendNotPermitted` at startup** — `SandboxRouter`'s default minimum-isolation floor is `Isolation.MICROVM`, and it refuses any backend below that. `AcasSandboxBackend` declares `Isolation.MICROVM`, exactly the floor, so this only appears if you swapped the backend for a container- or process-isolated one — and it means the swapped-in backend needs `min_isolation` lowered explicitly, not left implicit. It raises at construction rather than at first call, on purpose.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` requires: run a command and take a file in. `AcasSandboxBackend` declares both, so this only appears against a swapped-in backend that declares less.

**A disk image that cannot be resolved** — the image was named but never imported into the sandbox group. See the prerequisite above; the error names the reference it looked for, and that reference has to match the one the import step used exactly.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. See the prerequisite above; nothing about the sandbox is involved, and the run fails before one is created.

**The tool's answer says "printed nothing"** — `execute_code` only returns what the program printed; there is no REPL echo. A model that wrote an expression instead of a `print(...)` call gets exactly this sentence back, and it usually self-corrects on the next call.
