# 01 — validate a Bicep file in an ACA sandbox

A one-turn agent. It puts `main.bicep` into a file store, hands the agent the `bicep_validate` tool, asks it to validate the file once, prints the answer, and deletes the sandbox.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_acas  ->  the sandbox
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box — the one the package READMEs describe but never show.

`main.bicep` has a mistake in it on purpose, and that is what makes the run worth watching. The diagnostics you see come from the Bicep compiler running inside a microVM-isolated sandbox (**T2**), not from the model reading its own output and agreeing with itself (**T0**). Against a valid file the two are indistinguishable.

## Prerequisites

Read these first; none of them is quick to arrange halfway through.

- **An Azure subscription enrolled in the [Container Apps Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) preview**, and a **sandbox group** in it.
- **A registry serving the pinned Bicep sandbox image** (`bicep-sandbox:<version>`), with the image **imported into the sandbox group as a disk image** — a sandbox boots from a disk image, which is a different namespace from the registry it was pushed to, so pushing is not enough. [`images/bicep-sandbox`](../../images/bicep-sandbox/) is the image, and its README has the build, push and import command lines, including the identity the sandbox group pulls with. That image is sample-grade, deliberately: production replaces it with a hardened build you own — minimal, digest-pinned, scanned, rebuilt on your patch cadence — pushed and imported the same way.
- **An Azure OpenAI deployment of a reasoning model** — `gpt-5.4` and its siblings work. This is not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the very first call with `400 — Encrypted content is not supported with this model` on `param: include`. That error names neither this sample nor the setting behind it, so it is worth choosing correctly rather than debugging later.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

**This creates a billable sandbox.** The sample deletes it on the way out, and the backend's auto-suspend and auto-delete timers are a backstop underneath that, but a run that is killed mid-turn can still leave one active for up to ten minutes.

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
| `ACAS_SANDBOX_REGISTRY` | Registry login server, `<name>.azurecr.io`. Qualifies the bare image reference below |
| `BICEP_SANDBOX_IMAGE` | `repository:tag` of the Bicep image, e.g. `bicep-sandbox:0.46.1` |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment name of the chat model — a reasoning model, per the prerequisites |

With any of them unset the program says which and exits non-zero, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The first call is slow — the sandbox is created and booted before the compiler runs. The model writes its own prose around them, but the diagnostics it is reporting look like this:

```
  [error]   no-unused-params @ main.bicep:21: Parameter "environmentName" is
  declared but never used.
  [warning] BCP035 @ main.bicep:31: The specified "resource" declaration is missing
  the following required properties: "sku".
  [warning] use-recent-api-versions @ main.bicep:31: Use more recent API version for
  'Microsoft.Storage/storageAccounts'. '2023-01-01' is N days old ...

Disposed 1 sandbox(es).
```

Only the prose is the model's. The diagnostics are the compiler's, rendered by `bicep_validate` from the SARIF that `bicep build` and `bicep lint` each emit; exact wording follows the Bicep version in your image.

Two of those are worth reading closely, because neither says what a newcomer expects.

**`no-unused-params` is an `error`, and that is the signal to look for.** Its built-in level is `warning`; it is an error here because the image ships a `bicepconfig.json`, and Bicep finds that file *only* by walking up from the source it is compiling. So seeing this rule raised is the visible proof the config was discovered. If it ever prints `[warning]`, the config was not found and the linter fell back to weaker built-in defaults — a failure that otherwise looks entirely healthy, since SARIF still parses and diagnostics still render.

**`BCP035` is a `warning`, not an error.** A missing required property reads like it should stop the build, and in current Bicep it does not — `bicep build` prints `Warning BCP035` and still emits a template. Diagnostics that *are* errors, such as `BCP057` for an undefined name, come back as `[error]`.

`use-recent-api-versions` fires because the pinned `2023-01-01` API version has aged past the linter's 730-day threshold, and the day count in that message climbs on its own. Expect this sample's output to gain diagnostics over time with no change to the code — that is the linter working, not drift to be fixed.

Delete the unused parameter and add a `sku`, and what remains is the API-version warning.

## Troubleshooting

**`No sandbox backend: bicep_validate was not attached.`** — the router has no usable backend. Check the `ACAS_SANDBOX_*` variables.

**`SandboxBackendNotPermitted` at startup** — `SandboxRouter`'s default minimum-isolation floor is `Isolation.MICROVM`, and it refuses any backend below that. `AcasSandboxBackend` declares `Isolation.MICROVM`, exactly the floor, so this only appears if you swapped the backend for a container- or process-isolated one — and it means the swapped-in backend needs `min_isolation` lowered explicitly, not left implicit. It raises at construction rather than at first call, on purpose.

**`SandboxEgressNotEnforced` at startup** — the other half of the same story, raised one call later by `make_bicep_tools`: the backend cannot confine the sandbox to the two artifact hosts this workload names, so everything else — ARM included — would be reachable from code an agent wrote. `AcasSandboxBackend` declares `Egress.ALLOWLIST` and builds a Deny-default policy, so this too only appears against a swapped backend. A backend that can only run fully *closed* is accepted instead, with a warning: module restore then fails and `bicep_validate` says so rather than reporting a clean file.

**Every `br/public:` module reports `BCP192`** — module restore could not reach its hosts. The four it needs (`mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `aka.ms`, `live-data.bicep.azure.com`) are fixed in `bicep_sandbox_spec`, so this points at something above the sandbox in your network path, not at configuration.

**A disk image that cannot be resolved** — the image was pushed to the registry but never imported into the sandbox group. See the prerequisite above.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. See the prerequisite above; nothing about the sandbox is involved, and the run fails before one is created.

**`no-unused-params` reports as `[warning]`** — the image's `bicepconfig.json` was not found, so every linter rule is at its built-in default and the rule set is weaker than intended. Bicep resolves that file only by walking up from the source, so this means it is missing from the work-dir root in the image you are running.
