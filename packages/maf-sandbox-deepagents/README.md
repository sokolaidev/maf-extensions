# maf-sandbox-deepagents

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxDeepagentsExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_deepagents.MafSandboxDeepagentsExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of LangChain, Inc. or Microsoft — it is a third-party bridge between [Deep Agents](https://docs.langchain.com/oss/python/deepagents/sandboxes) and the `maf-sandbox` suite.

```
deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
```

A `maf-sandbox` router as a Deep Agents sandbox. Deep Agents gives an agent one `execute` tool over a sandbox object the host constructs, and derives its file tools from that; the cloud providers it ships with (LangSmith, Daytona, E2B, Modal, Runloop, Vercel) each wrap a vendor client. `MafSandbox` is that object over a `SandboxRouter` instead — so a LangChain or LangGraph agent runs its shell in a Docker container, an Azure Container Apps sandbox or a `wslc` container, and the host keeps the router's decisions: a backend below the isolation floor is refused at construction, egress is closed unless the spec names hosts, and the sandbox is keyed from the request context and purged with the conversation.

It is the suite in the other direction. The packaged kinds (`bicep_validate`, `execute_code`) attach to a Microsoft Agent Framework agent as tools; this package attaches a *backend* to a Deep Agents agent and lets the agent write its own commands. What that gives up is said below.

## Quickstart

```python
from deepagents import create_deep_agent
from maf_sandbox import Isolation, SandboxKey, SandboxRouter
from maf_sandbox_deepagents import MafSandbox, deepagents_spec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

router = SandboxRouter([DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER)
spec = deepagents_spec("python:3.12-alpine")

# One sandbox per conversation. Scope and thread come from the host's request context.
sandbox = MafSandbox(router, SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="coder"), spec)

agent = create_deep_agent(model=..., backend=sandbox, system_prompt="...")
```

`deepagents_spec` builds the spec Deep Agents needs — `EXEC`, `FILES_IN` and `FILES_OUT` — with the protocol's default base, or `work_dir=None` to let the backend allocate one, and derives egress: `egress_allow=("pypi.org",)` runs the sandbox `ALLOWLIST` with that host, and no hosts runs it `CLOSED`. There is no open posture to ask for, because the agent writes the commands. `MafSandbox` refuses a spec that lacks a required capability, and asks the router at construction whether the backend can serve it, so a misconfigured host fails before an agent is built.

The sandbox is acquired on the first command and reused warm after that. It lives until the host disposes it: `await sandbox.aclose()`, or the router's `dispose_scope(scope, thread_id)` on the host's own conversation-delete path — the backstop every sandbox in the suite answers to, and the one to wire, because LangGraph fires nothing when a thread is deleted. [`examples/docker_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-deepagents/examples/docker_bicep) runs the whole thing end to end; it moves to `samples/` once this package is published, because the samples install from PyPI.

## What the adapter maps

| Deep Agents | `maf_sandbox` |
|---|---|
| `execute(command, timeout)` | `Sandbox.exec(command, working_directory=".", timeout=...)` — run in the sandbox's storage base, a shell string the backend runs as `sh -c`; `stdout` and `stderr` come back as one stream with `[stderr]` on the second, the way Deep Agents' own backends render it |
| `upload_files([(path, bytes)])` | `Sandbox.write_file`, one call per file, relative to the storage base; a path outside it or through a link is refused as `invalid_path` |
| `download_files([path])` | `Sandbox.stat_file` then `Sandbox.read_file`, capped by `spec.files_out.max_bytes_per_file` and refusing rather than truncating; a missing file is `file_not_found`, a directory `is_directory`, a link `invalid_path` |
| `id` | An opaque hash of the key and the kind, because Deep Agents may render it to the model and a scope is often a tenant |
| `aclose()` | `SandboxRouter.dispose_kind` for this conversation's sandbox of this kind alone, so a packaged kind serving the same conversation keeps its own |

Both surfaces are served: the `a*` methods are native, and the synchronous ones run the same coroutine on a loop of their own, which is what a sync tool on a LangGraph worker thread calls.

## What it costs, honestly

**The agent writes the shell.** The packaged kinds run a fixed argv and never a shell; here the model's command string is what runs, which is Deep Agents' model and what its `BaseSandbox` documents. The container boundary and the egress policy are the controls, and nothing above them is. Do not put a credential in the image.

**Cleanup is disposal.** A kind can claim confinement to its own call directory and earn warm reuse through `RECLAIM`; an arbitrary shell cannot, so the router cleans this sandbox by deleting it. It stays warm across the turns of one conversation and goes when the host disposes it.

**Deep Agents' derived file tools need `python3` in the guest.** `ls`, `read_file`, `write_file`, `edit_file`, `glob` and `grep` are shell-and-Python snippets Deep Agents runs through `execute`. On an image without an interpreter — [`images/bicep-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/images/bicep-sandbox) is one — only `execute` and this package's own `upload_files` and `download_files` work. The image is the host's to choose, and the sample says so in its prompt.

**No labels and no declared outputs.** MAF's information-flow declarations, the per-call guest path, declared outputs with a landing sink, and host tools called from inside the guest are the kinds' contract with the framework, and Deep Agents has no place to put them. A host that needs them attaches a kind to a MAF agent; this package is for a host that already has a Deep Agents agent.

## Requirements

- Python 3.12 or newer.
- `deepagents` 0.7.x. Its backend protocol has changed between 0.x minors, so one minor is admitted at a time.
- A `maf-sandbox` backend that declares `EXEC`, `FILES_IN` and `FILES_OUT` — [`maf-sandbox-docker`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-docker) and [`maf-sandbox-acas`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-acas) do; `maf-sandbox-wslc` does not declare `FILES_OUT`, so the router refuses it here.
