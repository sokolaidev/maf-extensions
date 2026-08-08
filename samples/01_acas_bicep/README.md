# 01 — validate a Bicep file in an ACA sandbox

A one-turn agent. It puts `main.bicep` into a workspace store, hands the agent the `bicep_validate` tool, asks it to validate the file once, prints the answer, and deletes the sandbox.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_aca  ->  the sandbox
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box — the one the package READMEs describe but never show.

`main.bicep` has a mistake in it on purpose, and that is what makes the run worth watching. The diagnostics you see come from the Bicep compiler running inside a VM-isolated sandbox (**T2**), not from the model reading its own output and agreeing with itself (**T0**). Against a valid file the two are indistinguishable.

## Prerequisites

Read these first; none of them is quick to arrange halfway through.

- **An Azure subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a **sandbox group** in it.
- **A registry serving the pinned Bicep sandbox image** (`bicep-sandbox:<version>`), with the image **imported into the sandbox group as a disk image** — a sandbox boots from a disk image, which is a different namespace from the registry it was pushed to. [`packages/maf-sandbox-aca/scripts/import_disk_image.py`](../../packages/maf-sandbox-aca/scripts/import_disk_image.py) does the import, and its docstring covers the managed identity that needs `AcrPull` on the registry. The image itself is not built in this repository.
- **An Azure OpenAI deployment** for the chat model.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable VM.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that, but a run that is killed mid-turn can still leave one running for up to ten minutes.

## Install

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-aca maf-sandbox-bicep agent-framework-openai
```

`azure-identity` and `agent-framework-core` arrive as dependencies. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `ACA_SANDBOX_ENDPOINT` | Sandbox group data-plane endpoint, `https://management.<region>.azuredevcompute.io` |
| `ACA_SANDBOX_SUBSCRIPTION_ID` | Subscription holding the group |
| `ACA_SANDBOX_RESOURCE_GROUP` | Resource group holding the group |
| `ACA_SANDBOX_GROUP` | Sandbox group name |
| `ACA_SANDBOX_REGISTRY` | Registry login server, `<name>.azurecr.io`. Qualifies the bare image reference below |
| `BICEP_SANDBOX_IMAGE` | `repository:tag` of the Bicep image, e.g. `bicep-sandbox:0.46.1` |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment name of the chat model |

With any of them unset the program says which and exits non-zero, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

```bash
python agent.py
```

The first call is slow — the sandbox is created and booted before the compiler runs. The model writes its own prose around them, but the two diagnostics it is reporting look like this:

```
build(main.bicep): 1 diagnostic(s)
  [error] BCP035 @ main.bicep:31: The specified "resource" declaration is missing
  the following required properties: "sku".
lint(main.bicep): 1 diagnostic(s)
  [warning] no-unused-params @ main.bicep:21: Parameter "environmentName" is
  declared but never used.

Disposed 1 sandbox(es).
```

Only the prose is the model's. The diagnostics are the compiler's, rendered by `bicep_validate` from the SARIF that `bicep build` and `bicep lint` each emit; exact wording follows the Bicep version in your image.

Delete the unused parameter and add a `sku`, and the same run returns `build(main.bicep): no diagnostics` and `lint(main.bicep): no diagnostics`.

## Troubleshooting

**`No sandbox backend: bicep_validate was not attached.`** — the router has no usable backend. Check the `ACA_SANDBOX_*` variables.

**`SandboxBackendNotPermitted` at startup** — `SandboxRouter(..., deployed=True)` refuses anything weaker than a VM boundary. `AcaSandboxBackend` declares `Isolation.VM`, so this only appears if you swapped the backend for a container- or process-isolated one. It raises at construction rather than at first call, on purpose.

**Every `br/public:` module reports `BCP192`** — module restore could not reach its hosts. The four it needs (`mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms`, `live-data.bicep.azure.com`) are fixed in `bicep_sandbox_spec`, so this points at something above the sandbox in your network path, not at configuration.

**A disk image that cannot be resolved** — the image was pushed to the registry but never imported into the sandbox group. See the prerequisite above.
