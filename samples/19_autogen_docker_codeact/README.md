# 19 — compute an answer from an AutoGen agent, in a maf-sandbox container

Sample 06 from the other side of the seam. The agent is [AutoGen](https://microsoft.github.io/autogen/)'s `AssistantAgent`, its tool is `PythonCodeExecutionTool`, and what sits behind that tool is a `CodeExecutor` this sample writes — the four-method contract `autogen_core.code_executor` names — acquiring from a `maf_sandbox` router with the same Docker backend, the same image and the same task sample 06 runs.

```
autogen  ->  the sample's CodeExecutor  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
```

[`agent.py`](agent.py) is the `app` box. There is no Microsoft Agent Framework here and no `execute_code`; the task, the one right answer and the checker are sample 03's and sample 06's, unchanged, and diffing the two programs shows what changes when the framework does: the agent, the model client and the tool. The fourth piece is the executor itself, and it is sample code — AutoGen ships the contract, not a sandbox.

AutoGen is in maintenance mode — its README says it will not receive new features, and names Microsoft Agent Framework as its successor — which is why this is a sample and not a package: the contract will not move, so the executor is cheap to keep here, and a package would cost a range pull request in every core cycle for a framework that has stopped moving.

## What the router keeps

- **The floor.** `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `microvm` floor, so the router is constructed with `min_isolation=Isolation.CONTAINER` explicitly. Leave that out and construction refuses the backend before any agent exists.
- **Egress.** The spec names no host, so the container is created with `--network none`. The program computes, it does not fetch.
- **Keying and disposal.** The sandbox is keyed by scope, thread and agent id, acquired on the first tool call and reused warm after it, and purged at the end by `dispose_scope` — the same call every other sample makes.

## What the executor gives up, said out loud

- **No call admission.** `acquire` without `enter_call` is sound for one turn with nothing else on the key, and both model clients set `parallel_tool_calls=False` for exactly this reason — `AssistantAgent` executes every tool call a model response returns concurrently, and two concurrent calls over one unadmitted key is the sharing the sample is not built to referee. A host running concurrent calls over one key — more agents on one conversation, for instance — needs the admission lifecycle `maf-sandbox-deepagents` implements.
- **Lifecycle release is the executor's, but nobody calls it here.** `stop()` and `restart()` condemn the acquired sandbox through the router's unclean path — the contract says `stop` releases resources and `restart` runs when the agent is reset — but AutoGen 0.7.5 never drives either in this wiring: `AssistantAgent.on_reset()` only clears its model context, and `PythonCodeExecutionTool` forwards no lifecycle calls to its executor. The guarantee the sample relies on is the explicit `dispose_scope()` in `run()`, not the framework. A host that calls the lifecycle methods itself gets the release; one that relies on the agent's reset does not.
- **Cancelling abandons the wait, not the process.** AutoGen's `CancellationToken` cancels the await — the executor links it before awaiting `exec_bounded`, and a cancelled call raises out of the tool. The guest program keeps running until `dispose_scope` removes the container.
- **No declarative config.** The executor is not a `Component`, so `dump_component()` raises `NotImplementedError`. So does `PythonCodeExecutionTool.dump_component()`, whose `_to_config` calls the executor's, and so does an agent holding the tool. A router is not serialisable config.
- **The program travels in argv.** `python3 -c <code>` is bounded by the guest's argument size limit. A large program needs `write_file_over_exec` and a file, which this executor does not build.

## Two roads to a model, as samples 09 and 13 take them

In `autogen-ext`'s terms. With `AZURE_OPENAI_ENDPOINT` set, `AzureOpenAIChatCompletionClient` reaches an Azure OpenAI deployment with `DefaultAzureCredential` — no key in the tree, which is what lets this sample have a live job. Unset, `OpenAIChatCompletionClient` talks to any OpenAI-compatible endpoint, a local server included, and defaults to Ollama's. One `build_model` decides, on one variable.

Both roads pass a `model_info`, because AutoGen only knows OpenAI's model names: a deployment named `gpt-5.4` — or a local `minimax-m3:cloud` — is not one, and the client refuses to guess. The declared capabilities say what the deployment must actually have here, `function_calling` above all: the agent has to call the tool.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Same as sample 06.
- **`mcr.microsoft.com/devcontainers/python:3.13-bookworm`**. Nothing to build; the backend pulls an absent image before it creates the container. Same reasoning as sample 06's for why a full dev-container image is a prototyping convenience and production replaces it.
- **A model that can call a tool.** Either an Azure OpenAI deployment of a reasoning model — sample 06's prerequisite applies unchanged, encrypted reasoning content included — or an OpenAI-compatible endpoint: OpenAI itself, a router such as OpenRouter, or a local server (Ollama, vLLM, LM Studio).

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Optional. Set it and the Azure road is taken; e.g. `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | The chat deployment name. Required on the Azure road; the same string is passed as both the deployment and the model name, the way sample 06's client reads its one variable |
| `OPENAI_CHAT_MODEL` | The model name the endpoint serves. Defaults to Ollama's `minimax-m3:cloud` |
| `OPENAI_BASE_URL` | The endpoint. Unset it is Ollama's `http://localhost:11434/v1`, so reaching OpenAI itself means naming `https://api.openai.com/v1` here |
| `OPENAI_API_KEY` | Key for that endpoint. A local server that ignores it still wants something non-empty, so a placeholder is substituted |

With an endpoint named and no deployment beside it, the program says which variable is missing and exits non-zero rather than running.

## Run

The first tool call pays for creating the container; the router reuses it warm for any further call in the turn. The agent's reply prints first, then what `CodeExecutor` returned, then the disposal line:

```
354224848179261915075

== Program output as CodeExecutor returned it ==

  stdout:
  354224848179261915075

  [measured] programs whose output came back from the sandbox: 1

  [measured] Disposed 1 sandbox(es).
```

What does not vary is the block under the reply. `354224848179261915075` is a constant a model can recite, so the live check reads the copy inside `== Program output as CodeExecutor returned it ==` — the interpreter's own stdout, recorded by AutoGen beside the call — and not the one in the reply ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)). It is the same checker samples 03 and 06 run; its heading accepts both tool names. `Disposed N` reports only the final scope purge. A healthy run reports `Disposed 1`: the executor performs no per-call cleanup on the happy path. A run whose only call lost its execution — a timeout, an overflow, or any other failure the result did not come back from — does clean up per call, and then reports `Disposed 0`.

## Troubleshooting

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. This is the router refusing a shared-kernel boundary at its default posture, at construction rather than at first call, on purpose.

**`ValueError: The agent name must be a valid Python identifier`** — AutoGen refuses a name like `data-analyst`, which Microsoft Agent Framework accepts. The agent here is named `data_analyst` for that reason; a host renaming it needs an identifier, not a label.

**`ValueError: model_info is required when model name is not a valid OpenAI model`** — the client was built without a `model_info`. Both roads in `build_model` pass one; a copy that dropped it fails at construction, before anything is paid for.

**The tool's answer says the program failed, timed out, or exceeded the byte budget** — the executor's bound, not the model's: a two-minute execution ceiling and a 1 MiB output budget, the defaults the Deep Agents adapter carries. A run that loses its execution — a timeout, an overflow, or any other failure the result did not come back from — pays for its sandbox: it is condemned through the router's unclean path, so the key is refused until the instance's delete lands, and the next tool call creates a fresh one. The model reads an `Error:` string in every case, never an exception out of the tool.

**`NotImplementedError` from `dump_component`** — the executor, the tool and an agent holding them are not serialisable config here. See the give-ups above.