# 04 — compute an answer in a WSL container

Sample 03 with exactly one thing changed: the backend. Same agent, same tool, same task — a Python interpreter's answer rather than a model's guess at one — but the sandbox is a container on your own machine, created in about half a second, and there is no Azure subscription anywhere in the picture.

```
app  ->  maf_sandbox (router)  ->  maf_sandbox_wslc  ->  the container
              ^ maf_sandbox_codeact calls the router
```

[`agent.py`](agent.py) is the `app` box, and it is worth diffing against [sample 03's](../03_acas_codeact/agent.py): two import lines, one constructor, and the `min_isolation=` argument. Everything below the router is untouched — the same claim samples 01 and 02 make for `bicep_validate`, shown here for `execute_code` instead.

## The boundary is weaker, and the refusal is the feature

**`WslcSandboxBackend` declares `Isolation.CONTAINER`**, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — this sample has to opt the floor down explicitly to `min_isolation=Isolation.CONTAINER`, and the default would refuse this backend outright. A shared kernel is a reasonable place to run a short, disposable program on a machine you already trust, and the wrong thing to put next to a deployment's credentials; the router draws that line for you and will not be argued out of it without saying so in code, at construction time.

**Egress is not a second downgrade here**, unlike the Bicep pair. `codeact_sandbox_spec()`'s `egress_allow` is empty on every backend this kind runs on — the program computes, it does not fetch — so `wslc`'s closed-by-default network (every container runs `--network none`) asks for exactly what this workload already wanted. There is no allowlist to fall short of, and no `MODULE RESTORE FAILED` banner waiting to fire, because this kind never asks a backend to restore anything.

## Prerequisites

- **Windows with WSL 2.9.3 or later.** `wslc` is WSL's container CLI and ships with it; `wsl --version` reports the version, `wslc --version` confirms the CLI is on `PATH`. Nothing else needs installing — no Docker, no daemon, no login.
- **`mcr.microsoft.com/devcontainers/python:3.13-bookworm`**, the same reference [sample 03](../03_acas_codeact/) uses. Nothing to build: `wslc container run` pulls it from MCR the first time it is needed, the way `docker run` would. It is a dev-container image — a full toolchain this workload never touches, not just an interpreter — so it is bulkier than the sandbox strictly needs; a minimal Azure Linux Python image becomes the better choice the day that family ships 3.13 too.
- **An OpenAI-compatible endpoint** — api.openai.com, or a local server that speaks the same protocol. The sample deliberately uses the chat-completions API, the one surface local servers implement well; their newer-API support is often partial in ways that surface as an empty final answer. The model needs to be able to call a tool; beyond that this sample asks nothing unusual of it.

No Azure subscription, no preview enrolment, no billable sandbox. A run that is killed mid-turn leaves the container **running** — it was started with `sleep infinity` and nothing stops it on the way out — so plain `wslc container list` shows it, it holds WSL VM memory until it goes, and `wslc container remove -f <name>` reclaims it.

## Install

From PyPI, not from this workspace:

```bash
python -m venv .venv && source .venv/bin/activate
pip install maf-sandbox-wslc maf-sandbox-codeact agent-framework-openai
```

`maf-sandbox` arrives as a dependency, and nothing Azure does — the backend drives `wslc.exe` and imports only the standard library. `agent-framework-openai` is separate because the framework's core ships no model connector.

## Environment

| Variable | What it is |
|---|---|
| `OPENAI_API_KEY` | Key for the endpoint below. A local server that ignores it still wants something non-empty here |
| `OPENAI_CHAT_MODEL` | Model name, e.g. `gpt-4o` — or whatever your local server calls the model it serves |
| `OPENAI_BASE_URL` | *Optional.* An OpenAI-compatible base URL, e.g. `http://localhost:11434/v1`. Unset, the client talks to OpenAI |

With either of the first two unset the program says which and exits non-zero, rather than running. That is deliberate: `make_codeact_tools` returns an empty list when the router has no backend, so a half-configured run does not crash — it produces an agent with no tools, which answers from the model alone. That failure looks exactly like success.

## Run

```bash
python agent.py
```

The first call pays for pulling the image, if it is not already local, plus creating and starting the container — a few seconds, against the minutes a microVM-isolated sandbox needs. The model writes its own prose around it, but the answer it is reporting looks like this:

```
stdout:
354224848179261915075

Disposed 1 sandbox(es).
```

The same number sample 03 gets from a microVM in Azure, in the same `stdout:\n<value>` shape `_format_result` in `maf-sandbox-codeact` renders unchanged for either backend — no stderr, no exit code, because the program printed one line and exited cleanly.

## Troubleshooting

**The image cannot be pulled** — `wslc container run` needs to reach `mcr.microsoft.com` for the first pull; after that it is cached locally. Check your network path if it fails every time rather than only the first.

**`wslc` is not found** — WSL is older than 2.9.3, or is not installed. `wsl --version` reports it; `wsl --update` moves it forward. There is no separate package to install: the CLI is part of WSL.

**`SandboxBackendNotPermitted` at startup** — the router was constructed without `min_isolation=Isolation.CONTAINER`. `WslcSandboxBackend` declares `Isolation.CONTAINER`, below the router's default `min_isolation=Isolation.MICROVM` floor, so this is the router refusing a shared-kernel boundary at its default posture. It raises at construction rather than at first call, on purpose. This is not a configuration problem to work around — it is the reason this backend is a developer-machine one, opted into explicitly.

**`SandboxCapabilityNotSupported` at startup** — the backend cannot do what `execute_code` requires: run a command and take a file in. `WslcSandboxBackend` declares both, so this only appears against a swapped-in backend that declares less.

**The tool's answer says "printed nothing"** — `execute_code` only returns what the program printed; there is no REPL echo. A model that wrote an expression instead of a `print(...)` call gets exactly this sentence back, and it usually self-corrects on the next call.
