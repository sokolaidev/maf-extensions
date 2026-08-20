# 06 — compute an answer in a Docker container

Sample 03 with exactly one thing changed: the backend. Same agent, same tool, same task — a Python interpreter's answer rather than a model's guess at one — but the sandbox is a plain Docker container instead of a billable Azure microVM, and it runs on any machine with a Docker-compatible engine.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_docker  ->  the container
              ^ maf_sandbox_codeact calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 03's](../03_acas_codeact/agent.py): two import lines, one constructor, and the `min_isolation=` argument. That is the tightest diff in the whole set — [sample 04](../04_wslc_codeact/) also swapped sample 03's Azure model for a local one, and this keeps it, because keeping it is exactly what lets this sample be verified in CI.

## The first sample CI verifies without a cloud sandbox

Sample 03 proves the CodeAct stack against a real Azure microVM, and pays for a billable sandbox each time it runs — so it runs only on demand and after a release. This sample proves the same stack, but its sandbox is a Docker container on the runner: free, and needing no Azure subscription. The model still needs both — it is the same Azure OpenAI deployment sample 03 uses, in a subscription and billed per inference, reached with `DefaultAzureCredential` (a federated credential in CI, `az login` locally), so there is no stored API key. What this sample removes from sample 03 is the **billable sandbox and the stored secret**, not the model's inference charge, which no sample avoids. That is enough to let it join `verify-live.yml`: a real container and a real model, with no billable sandbox and no secret to hold.

A developer without Azure runs it locally by making sample 04's one-line client swap — `OpenAIChatCompletionClient` against a local endpoint in place of the Azure client here. The wiring in this directory is the one CI needs; the swap is small and sample 04 shows it in full.

## The boundary is weaker, and the refusal is the feature

**`DockerSandboxBackend` declares `Isolation.CONTAINER`**, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — this sample has to opt the floor down explicitly to `min_isolation=Isolation.CONTAINER`, and the default would refuse this backend outright. A Docker Desktop or Colima VM does not lift that rung: one shared VM kernel serves every container. A shared kernel is a reasonable place to run a short, disposable program on a machine you already trust, and the wrong thing to put next to a deployment's credentials; the router draws that line for you and will not be argued out of it without saying so in code, at construction time.

**Egress is not a second downgrade here**, unlike the Bicep pair. `codeact_sandbox_spec()`'s `egress_allow` is empty on every backend this kind runs on — the program computes, it does not fetch — so the docker backend's closed-by-default network (every container runs `--network none`) asks for exactly what this workload already wanted. There is no allowlist to fall short of.

## Prerequisites

- **A Docker-compatible engine, reachable through the `docker` client.** Docker Desktop (macOS, Linux, Windows with WSL 2) or Docker Engine (Linux). `docker version` confirms the client can reach a running daemon.
- **`mcr.microsoft.com/devcontainers/python:3.13-bookworm`**. Nothing to build, but pull it once before the first run — `docker pull mcr.microsoft.com/devcontainers/python:3.13-bookworm`. The backend pulls an absent image explicitly before it creates the container, so a first run works without this step; pulling first just separates "the image could not be fetched" from "the sandbox failed". It is a dev-container image — a full toolchain this workload never touches, not just an interpreter — so it is bulkier than the sandbox strictly needs; a minimal Azure Linux Python image becomes the better choice the day that family ships 3.13 too. Either way this reference is for prototyping: production replaces it with a hardened image you build and own — minimal, digest-pinned, scanned, rebuilt on your patch cadence.
- **An Azure OpenAI deployment of a reasoning model** — `gpt-5.4` and its siblings work. This is not a preference: the framework's client asks for encrypted reasoning content, and a deployment that does not support it rejects the very first call with `400 — Encrypted content is not supported with this model` on `param: include`. That error names neither this sample nor the setting behind it, so it is worth choosing correctly rather than debugging later.
- **`az login`**, or any other credential `DefaultAzureCredential` resolves. No API keys are read, and none belong in this tree.

No preview enrolment and no billable sandbox — the container is free. A run that is killed mid-turn leaves the container **running** — it was started with `sleep infinity` and nothing stops it on the way out — so plain `docker ps` shows it, and `docker rm -f <name>` reclaims it.

## Install

Dependencies are declared in `agent.py` itself, in a [PEP 723](https://peps.python.org/pep-0723/) block, so there is nothing to install and nothing to keep in step with this page — [uv](https://docs.astral.sh/uv/) reads them and builds a throwaway environment for the run. From PyPI, never from this workspace:

```bash
uv run agent.py
```

`maf-sandbox` arrives as a dependency of the backend, which otherwise drives the `docker` client and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector. `azure-identity` is separate too, and named explicitly for a reason: sample 03 gets it transitively through `maf-sandbox-acas`, but the docker backend does not depend on it — and `agent-framework-openai` does not install it either — so a sample that authenticates the model with `DefaultAzureCredential` has to ask for it, exactly as `verify-live.yml` does.

## Environment

| Variable | What it is |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment name of the chat model — a reasoning model, per the prerequisites |

There are no sandbox variables at all: the docker backend runs the local engine and reads nothing from the environment. With either model variable unset the program says which and exits non-zero, rather than running. That is deliberate: `make_codeact_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

The first call pays for pulling the image, if it is not already local, plus creating and starting the container — a few seconds, against the minutes a microVM-isolated sandbox needs. `agent.py` prints the model's reply, then what `execute_code` returned, then the disposal line — so what you see looks something like this:

```
354224848179261915075

== Program output as execute_code returned it ==

  stdout:
  354224848179261915075

  [measured] programs whose output came back from the sandbox: 1

  [measured] Disposed 1 sandbox(es).
```

That block is one real run. This model answered with the number alone; another will wrap it in a sentence. What does not vary is the block under it. `354224848179261915075` is a constant a model can recite, so the live check reads the copy inside `== Program output as execute_code returned it ==` — the interpreter's own stdout, recorded by the framework beside the call — and not the one in the reply ([#314](https://github.com/sokolaidev/maf-extensions/issues/314)). `[measured] Disposed 1 sandbox(es).` only prints once `execute_code` has actually created and torn down a container; a `Disposed 0` would mean the model answered without running anything, the T0 behaviour this sample exists to contrast with.

The same number sample 03 gets from a microVM in Azure, computed by the same program in the same way — only the backend underneath differs.

## Troubleshooting

**`Cannot connect to the Docker daemon`** — the client is installed but no daemon is reachable. Start Docker Desktop (or your engine) and confirm with `docker version`, which reports both a Client and a Server section when the daemon is up. This is the most common failure by a wide margin, and the backend names it rather than letting it read as a generic sandbox error.

**`the docker backend needs an event loop that can spawn subprocesses`** — only on Windows, and only if the host installed `WindowsSelectorEventLoopPolicy`. Every call spawns the `docker` client, which asyncio's default Proactor loop on Windows supports; a host that changed that has to change it back.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `DockerSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `min_isolation=Isolation.MICROVM` floor. It raises at construction rather than at first call, on purpose — the reason this backend is a developer-machine one, opted into explicitly.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` requires: run a command and take a file in. `DockerSandboxBackend` declares both, so this only appears against a swapped-in backend that declares less.

**`400 — Encrypted content is not supported with this model`** — the chat deployment is not a reasoning model. See the prerequisite above; nothing about the sandbox is involved, and the run fails before one is created.

**The tool's answer says "printed nothing"** — `execute_code` only returns what the program printed; there is no REPL echo. A model that wrote an expression instead of a `print(...)` call gets exactly this sentence back, and it usually self-corrects on the next call.
