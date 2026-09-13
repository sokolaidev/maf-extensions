# 17 — validate a Bicep file from a Deep Agents agent, in a maf-sandbox container

Sample 05 from the other side of the seam. The agent is [LangChain's Deep Agents](https://docs.langchain.com/oss/python/deepagents/sandboxes), not Microsoft Agent Framework; its sandbox is Deep Agents' own `execute` tool; and what sits behind that tool is a `maf_sandbox` router with the same Docker backend and the same image sample 05 runs.

```
deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
```

[`agent.py`](agent.py) is the `app` box. `main.bicep` is a **byte-identical copy** of [sample 05's](../05_docker_bicep/main.bicep), so the compiler says the same three things about it, and diffing the two programs shows what changes when the framework does: the agent, the model client and the tool, and nothing below the router. [`maf-sandbox-deepagents`](../../packages/maf-sandbox-deepagents/)' README says what the adapter maps and what it gives up.

What the router keeps, and this sample shows:

- **The floor.** `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `microvm` floor, so the router is constructed with `min_isolation=Isolation.CONTAINER` explicitly. Leave that out and construction refuses the backend before any agent exists.
- **Egress.** `deepagents_spec(image)` names no host, so the spec runs `CLOSED` and the container is created with `--network none`. `main.bicep` uses no modules, so the compile completes offline.
- **Keying and disposal.** The sandbox is keyed by scope, thread and agent directory, acquired on the first operation, here the host's upload, and reused warm after it, and purged at the end by `dispose_scope` — the same call every other sample makes, because LangGraph fires nothing when a thread goes.

What it gives up, and this sample says out loud: the agent writes the shell. `bicep_validate` runs a fixed argv and reads SARIF back; here the model types `bicep build main.bicep --no-restore --diagnostics-format sarif` and the sandbox runs it. That is Deep Agents' model, and the container and the closed network are the controls. It also weakens the evidence the sample can print: a tool result still proves *a* command ran in the sandbox, but the model chose which, so a result counting as a compile is a result that carries SARIF `ruleId` entries, and nothing more can be said from outside the model. Sample 05's count is stronger because the kind wrote the command.

SARIF rather than the plain format for a measured reason: with an error in the file — and `no-unused-params` is promoted to one by `bicepconfig.json` — the plain format prints the error alone, and the two warnings beside it go unseen. The SARIF document carries all three, which is why `bicep_validate` reads that and nothing else.

**Two roads to a model, as samples 09 and 13 take them**, in `langchain-openai`'s terms. With `AZURE_OPENAI_ENDPOINT` set, `AzureChatOpenAI` reaches an Azure OpenAI deployment with `DefaultAzureCredential` — no key in the tree, which is what lets this sample have a live job. Unset, `ChatOpenAI` talks to any OpenAI-compatible endpoint, a local server included, and defaults to Ollama's. One `build_model` decides, on one variable.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Same as sample 05.
- **The sandbox image**, built from [`images/bicep-sandbox`](../../images/bicep-sandbox/), from the repository root:

  ```bash
  docker build -t bicep-sandbox:local images/bicep-sandbox
  ```

  It carries the Bicep CLI and `bicepconfig.json` and **no Python**. Deep Agents' `ls`, `read_file`, `edit_file`, `glob` and `grep` tools run `python3` inside the guest, and `write_file` runs a Python preflight there before it uploads, so on this image only `execute` and `delete` work, and the system prompt tells the model so. The host puts `main.bicep` in the sandbox with the adapter's own `upload_files`, which goes through the backend's file plane and needs nothing in the image.

- **A model that can call a tool.** Either an Azure OpenAI deployment reached with `DefaultAzureCredential`, where an `az login` session is the whole of the authentication, or an OpenAI-compatible endpoint: OpenAI itself, a router such as OpenRouter (`OPENAI_BASE_URL=https://openrouter.ai/api/v1`, a model name like `openai/gpt-4o-mini`), or a local server (Ollama, vLLM, LM Studio) — the same road samples 02 and 04 take.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

## Environment

| Variable | What it is |
|---|---|
| `BICEP_SANDBOX_IMAGE` | Local image reference, e.g. `bicep-sandbox:local` |
| `AZURE_OPENAI_ENDPOINT` | Optional. Set it and the Azure road is taken; e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name. Required on the Azure road |
| `OPENAI_CHAT_MODEL` | The model name the endpoint serves. Defaults to Ollama's `minimax-m3:cloud` |
| `OPENAI_BASE_URL` | The endpoint. Unset it is Ollama's `http://localhost:11434/v1`, so reaching OpenAI itself means naming `https://api.openai.com/v1` here |
| `OPENAI_API_KEY` | Key for that endpoint. A local server that ignores it still wants something non-empty, so a placeholder is substituted |

With the image reference unset, or with an endpoint named and no deployment beside it, the program says which variable is missing and exits non-zero rather than running.

## Run

The upload, the first operation, pays for creating the container. The model writes its own summary first; under it the sample prints the compiler's words again, this time straight out of what `execute` returned:

```
== Diagnostics as execute returned them ==

  [stderr] {
  [stderr]   "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json",
  [stderr]   "version": "2.1.0",
  [stderr]   "runs": [
  ...
  [stderr]           "ruleId": "no-unused-params",
  [stderr]           "level": "error",
  ...
  [stderr]           "ruleId": "BCP035",
  ...
  [stderr]           "ruleId": "use-recent-api-versions",
  ...
  [measured] compiles that reached the sandbox: 2

  [measured] Disposed 1 sandbox(es).
```

Only the prose above the heading is the model's; the block under it is the tool's own output, and the `[measured]` lines are the sample vouching for a number. That split earned its keep on the first live run: `gpt-4o-mini` through OpenRouter ran both commands and listed all three diagnostics, and reported every one of them as an error — the SARIF under its prose shows two of them carry no level, which means warning. The block is what to read, and [`scripts/check_live_deepagents_sample.py`](../../scripts/check_live_deepagents_sample.py) is what reads it after a release: the rule ids, the promoted level, and the reply naming what the compiler reported.

The prefixes say which stream a line came from, and the two commands answer on different ones: `bicep build` writes its SARIF to `stderr`, which the adapter prefixes, and `bicep lint` writes the same document to `stdout`, which it does not. Both carry all three diagnostics — sample 05's: `BCP035` for the missing `sku`, `no-unused-params` as an **error**, and `use-recent-api-versions` — the second printing as an error rather than its built-in warning is the visible proof that `bicepconfig.json` at `/maf-sandbox/work` was found.

## Troubleshooting

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. This is the router refusing a shared-kernel boundary at its default posture, at construction rather than at first call, on purpose.

**`SandboxCapabilityNotSupported` at startup** — the backend does not declare `EXEC`, `FILES_IN` and `FILES_OUT`, which `MafSandbox` requires. `maf-sandbox-docker` declares all three; `maf-sandbox-wslc` does not declare `FILES_OUT` and is refused here.

**`Error: sandbox unavailable`** in a tool result — the router or the backend could not serve the command: usually an image that is not on this machine. The provider's message is in the host's log, never in the transcript.

**The model called `read_file`, `write_file` or `ls` and got `python3: not found`** — this image has no interpreter, and those tools need one, `write_file` for the preflight it runs before uploading. The prompt says so; a model that ignores it gets that answer and usually falls back to `execute`.
