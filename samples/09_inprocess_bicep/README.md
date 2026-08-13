# 09 — validate a Bicep file against a backend that is not real

Sample 02 with the backend swapped for the in-process fake from `maf_sandbox.testing`. Same agent, same tool, same kind of deliberately broken file — but the sandbox is an `InProcessSandbox` in this process, scripted to answer the two fixed bicep commands. No container, no VM, no image, no Bicep binary, no Azure subscription.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox.testing  ->  this process
              ^ maf_sandbox_bicep calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 02's](../02_wslc_bicep/agent.py): the backend constructor is the change. Everything below the router is the same workload — `make_bicep_tools`, the fixed `bicep build` and `bicep lint` command templates, the SARIF parser — running against a backend that has no guest filesystem and no compiler. That is the floor of the set, below sample 02's local container and below every real backend, and it is the learning ground for the protocol seam itself: the router acquires, the workload writes the file and runs the commands, the SARIF comes back and is rendered, all in one process a reader can step through with no infrastructure at all.

The fake is an honest stand-in for the compiler here because the command templates carry no model text. `bicep build {path} --diagnostics-format sarif 2>&1 || true` and `bicep lint {path} --diagnostics-format sarif || true` are fixed strings, so the fake matches a marker on each and returns the SARIF a real `exec` would have produced: `bicep build` gets an empty document, `bicep lint` gets one with a single `no-hardcoded-location` warning — the finding a real compiler flags against the hardcoded `location: 'eastus'` in [`main.bicep`](main.bicep). Nothing is mocked about the workload; only the executor is.

## Prerequisites

- **A model endpoint.** That is the whole of it. Locally, an [Ollama](https://ollama.com/) server on its default port — `ollama serve` and `ollama pull minimax-m3:cloud` (or whatever model you prefer) is the entire setup, and even the model name is optional because the sample defaults it. In CI, an Azure OpenAI deployment reached with a federated credential, no key stored anywhere. The model needs to be able to call a tool; beyond that this sample asks nothing unusual of it.

No Bicep CLI, no sandbox image, no container runtime, no Azure subscription, no billable anything. The "sandbox" is a Python object that lives for one turn.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

`maf-sandbox` brings the router and the in-process fake; `maf-sandbox-bicep` brings the workload; `agent-framework-openai` brings the model client. `azure-identity` and `azure-core[aio]` are declared for the CI path — a local-only run installs them too, because the block is the single source, and never imports them.

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | *Optional.* Set it to reach Azure OpenAI in CI (with a federated credential — no key). Unset, the client talks to a local Ollama server. The two paths are mutually exclusive and this one variable is the branch |
| `AZURE_OPENAI_CHAT_MODEL` | Required **only** when `AZURE_OPENAI_ENDPOINT` is set. The Azure deployment name |
| `OPENAI_CHAT_MODEL` | *Optional, local only.* Model name. Defaults to `minimax-m3:cloud`, so a running Ollama server with that model pulled is the whole of configuration |
| `OPENAI_BASE_URL` | *Optional, local only.* An OpenAI-compatible base URL. Defaults to `http://localhost:11434/v1`, Ollama's endpoint |

Local mode needs none of them — an unset `AZURE_OPENAI_ENDPOINT` and a running `ollama serve` is enough. Azure mode needs the first two and exits non-zero if `AZURE_OPENAI_CHAT_MODEL` is missing, rather than running. That is deliberate: `make_bicep_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The model writes its own prose around the diagnostics, but what it is reporting comes from the fake, and looks like this:

```
  [warning] no-hardcoded-location @ main.bicep:6: Resource location should not be
  a hard-coded string; use a parameter, a variable, or an expression like
  resourceGroup().location.

Disposed 1 sandbox(es).
```

One diagnostic, the one `bicep lint` was scripted to return; `bicep build` came back clean. The rule id is an opaque token the model is instructed to echo verbatim, so its presence is evidence the scripted SARIF reached the workload, was parsed, was rendered, and came back through the agent — the whole protocol seam, in one process, with nothing rented. The `Disposed 1 sandbox(es).` line is the proof a sandbox was acquired at all, and not the model answering from reading the file.

## Troubleshooting

**`Connection refused` at `localhost:11434`** — Ollama is not running. `ollama serve` starts it; the sample's default base URL points there. Point `OPENAI_BASE_URL` at a different server if you run another.

**`model not found`** — Ollama does not have the model. `ollama pull minimax-m3:cloud` fetches the default, or set `OPENAI_CHAT_MODEL` to one you already have.

**`Azure OpenAI client requires either an API key or an Azure AD token provider`** — the CI path was taken (`AZURE_OPENAI_ENDPOINT` is set) but the credential resolved to nothing. An `az login` (or a federated CI credential) is what satisfies `DefaultAzureCredential`; check that the federated identity is configured for this branch's subject, the way the other Azure samples do.

**No diagnostic in the output, just the model's opinion** — the tool was not attached, which means `make_bicep_tools` returned `[]`. The router has a backend here, so the only cause is the fake not matching the command markers, and that is a change to the fixed command templates in `maf-sandbox-bicep` rather than anything in this directory.