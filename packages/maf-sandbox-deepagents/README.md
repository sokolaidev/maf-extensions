# maf-sandbox-deepagents

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxDeepagentsExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_deepagents.MafSandboxDeepagentsExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of LangChain, Inc. or Microsoft — it is a third-party bridge between [Deep Agents](https://docs.langchain.com/oss/python/deepagents/sandboxes) and the `maf-sandbox` suite.

```
deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
```

A `maf-sandbox` router as a Deep Agents sandbox. Deep Agents gives an agent one `execute` tool over a sandbox object the host constructs, and derives its file tools from that; the cloud providers it ships with (LangSmith, Daytona, E2B, Modal, Runloop, Vercel) each wrap a vendor client. `MafSandbox` is that object over a `SandboxRouter` instead — so a LangChain or LangGraph agent runs its shell in a Docker container or an Azure Container Apps sandbox, and the host keeps the router's decisions: a backend below the isolation floor is refused at construction, egress is closed unless the spec names hosts, and the sandbox is keyed from the request context and purged with the conversation.

It is the suite in the other direction. The packaged kinds (`bicep_validate`, `execute_code`) attach to a Microsoft Agent Framework agent as tools; this package attaches a *backend* to a Deep Agents agent and lets the agent write its own commands. What that gives up is said below.

## Quickstart

```python
from deepagents import create_deep_agent
from maf_sandbox import Isolation, SandboxKey, SandboxRouter
from maf_sandbox_deepagents import MafSandbox, deepagents_spec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

router = SandboxRouter([DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER)
spec = deepagents_spec("python:3.12-alpine")

# One sandbox per agent in a conversation: scope, thread and agent directory come from the host's request context.
sandbox = MafSandbox(router, SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="coder"), spec)

agent = create_deep_agent(model=..., backend=sandbox, system_prompt="...")
```

`deepagents_spec` builds the spec Deep Agents needs — `EXEC`, `FILES_IN` and `FILES_OUT` — with the protocol's default base, and derives egress: `egress_allow=("pypi.org",)` runs the sandbox `ALLOWLIST` with that host, and no hosts runs it `CLOSED`. There is no open posture to ask for, because the agent writes the commands, and a spec built another way that asks for `UNRESTRICTED` is refused. `MafSandbox` refuses that, a spec that lacks a required capability, a router with no backend, and a spec that leaves the base to the backend, and asks the router at construction whether the backend can serve it, so a misconfigured host fails before an agent is built.

**Paths are guest paths**, as they are for every sandbox Deep Agents ships: the agent's `read_file`, `write_file` and the rest name them absolutely, and `execute` runs in the base. The adapter's own upload and download take an absolute path anywhere in the guest, or a relative one under the base. Inside `spec.work_dir` they go through the backend's file plane, which refuses a path through a link as `invalid_path`; outside it — Deep Agents keeps its offloaded history under `/conversation_history/` and its large-edit temporaries under `/tmp/` — they go through the same shell the agent already has, in base64 chunks, under the same caps, so the file plane's confinement widens nothing that `execute` had not already opened. Put `spec.work_dir` in the system prompt so the model knows where its own files are; the example does. A backend-allocated base (`work_dir=None`) is refused at construction, because nothing could then tell the model where it is.

The sandbox is acquired on the first operation, a command or a file transfer, and reused warm after that. It lives until the host disposes it: `await sandbox.aclose()`, or the router's `dispose_scope(scope, thread_id)` on the host's own conversation-delete path — the backstop every sandbox in the suite answers to, and the one to wire, because LangGraph fires nothing when a thread is deleted. [`examples/docker_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-deepagents/examples/docker_bicep) runs the whole thing end to end; it moves to `samples/` once this package is published, because the samples install from PyPI.

## What the adapter maps

| Deep Agents | `maf_sandbox` |
|---|---|
| `execute(command, timeout)` | `BoundedExec.exec_bounded(command, working_directory=".", timeout=..., max_output_bytes=...)` — run in the sandbox's storage base, a shell string the backend runs as `sh -c`, under one deadline that a cold acquire spends part of and cannot outlive, and under `max_output_bytes` (1 MiB by default), which the backend enforces before it buffers anything: output past it is dropped whole and the result says so with `truncated=True`; a timeout, an overflow or the caller's cancellation also disposes the sandbox, because nothing establishes that the process stopped when the host stopped waiting; the delete runs after the answer on the process's own loop, so it neither extends the caller's wait nor dies with the caller's loop, and the next operation waits for it before it acquires, so the next command starts cold; `stdout` and `stderr` come back as one stream with `[stderr]` on the second, the way Deep Agents' own backends render it |
| `upload_files([(path, bytes)])` | `Sandbox.write_file` under the base, one call per file, and `mkdir -p` plus `base64 -d` through `exec_bounded` outside it, under `spec.files_in`: a batch over `max_files` is refused whole, a file over `max_bytes_per_file` or past `max_total_bytes` is refused alone; a path through a link under the base is refused as `invalid_path`, and what the shell refuses comes back as `permission_denied`, `is_directory` or `invalid_path`; a shell write that does not finish, on a timeout, an output overflow or the caller's cancellation, disposes the sandbox and fails the whole batch, because what it had put there went with it |
| `download_files([path])` | `Sandbox.stat_file` then `Sandbox.read_file` under the base, and a `test`/`wc -c` probe plus `base64` through `exec_bounded` outside it, under `spec.files_out`: a batch over `max_files` is refused whole, and each file is read under the smaller of `max_bytes_per_file` and what `max_total_bytes` has left, refusing rather than truncating; a missing file is `file_not_found`, a directory `is_directory`, a link under the base `invalid_path`; a shell read that does not finish, on a timeout, an overflow or the caller's cancellation, disposes the sandbox and fails the rest of the batch |
| `id` | An opaque hash of what names the sandbox — the key, the kind, the serving backend and the egress posture — because Deep Agents may render it to the model and a scope is often a tenant |
| `aclose()` | `SandboxRouter.dispose_kind` for the one instance this adapter acquired, so a packaged kind serving the same conversation, or another adapter over the same key with a different backend or egress, keeps its own; before any acquire there is nothing to delete |

Both surfaces are served: the `a*` methods are native, and the synchronous ones, which a sync tool on a LangGraph worker thread calls, run the same coroutine on one loop on a thread of its own, shared by every adapter in the process and started by the first sync call, so a backend that caches a client per loop holds one, not one per adapter or per call.

## What it costs, honestly

**The agent writes the shell.** The packaged kinds run a fixed argv and never a shell; here the model's command string is what runs, which is Deep Agents' model and what its `BaseSandbox` documents. The container boundary and the egress policy are the controls, and nothing above them is. Do not put a credential in the image.

**Cleanup is disposal.** A kind can claim confinement to its own call directory and earn warm reuse through `RECLAIM`; an arbitrary shell cannot, so the router cleans this sandbox by deleting it. It stays warm across the turns of one conversation and goes when the host disposes it.

**Deep Agents' derived file tools need `python3` in the guest.** `ls`, `read_file`, `edit_file`, `glob` and `grep` are shell-and-Python snippets Deep Agents runs through `execute`, and `write_file` runs one too, a preflight that creates the parent directory, before it hands the bytes to `upload_files`. On an image without an interpreter — [`images/bicep-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/images/bicep-sandbox) is one — only `execute`, `delete` (a `test` and an `rm -rf` through `execute`) and this package's own `upload_files` and `download_files` work. The image is the host's to choose, and the sample says so in its prompt.

**No labels and no declared outputs.** MAF's information-flow declarations, the per-call guest path, declared outputs with a landing sink, and host tools called from inside the guest are the kinds' contract with the framework, and Deep Agents has no place to put them. A host that needs them attaches a kind to a MAF agent; this package is for a host that already has a Deep Agents agent.

## Requirements

- Python 3.12 or newer.
- `deepagents` 0.7.x. Its backend protocol has changed between 0.x minors, so one minor is admitted at a time.
- A `maf-sandbox` backend that implements `BoundedExec` (the adapter runs nothing through a sandbox that cannot bound its output), and declares `EXEC`, `FILES_IN` and `FILES_OUT` — [`maf-sandbox-docker`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-docker) and [`maf-sandbox-acas`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox-acas) do; `maf-sandbox-wslc` does not declare `FILES_OUT`, so the router refuses it here.
