# maf-sandbox-deepagents

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-deepagents)](https://pypi.org/project/maf-sandbox-deepagents/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** The API may change without notice. Importing the package emits `MafSandboxDeepagentsExperimentalWarning`.

Use a `SandboxRouter` as a Deep Agents sandbox. `MafSandbox` connects Deep Agents' command and file tools to a backend such as Docker or ACAS. The router enforces the host's isolation floor and the spec's capabilities and network policy.

This is a third-party integration, not a product of or endorsed by LangChain, Inc. or Microsoft.

```
deepagents  ->  maf_sandbox_deepagents  ->  maf_sandbox (router)  ->  a backend  ->  the sandbox
```

![The model calls Deep Agents command and file tools. MafSandbox maps those operations to the router and backend, which execute the model's shell commands in a guest. Text and downloaded bytes return through Deep Agents' result types. This route does not attach MAF content labels, hide untrusted output, run packaged kinds or provide their declared-output and host-tool channels. Other tools and their information-flow policy belong to the host's Deep Agents integration.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/deepagents-boundary.svg)

## Quickstart

```bash
pip install maf-sandbox-deepagents maf-sandbox-docker
```

```python
from deepagents import create_deep_agent
from maf_sandbox import Isolation, SandboxKey, SandboxRouter
from maf_sandbox_deepagents import MafSandbox, deepagents_spec
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

router = SandboxRouter([DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER)
spec = deepagents_spec("python:3.12-alpine")

# Derive these identifiers from trusted host request context.
sandbox = MafSandbox(router, SandboxKey(scope="tenant-a", thread_id="thread-1", agent_id="coder"), spec)

agent = create_deep_agent(model=..., backend=sandbox, system_prompt=f"Work under {spec.work_dir}.")
```

Use a host-derived key per agent and conversation. The adapter requires conversation scope and an empty `key.call_id`. Its default kind is `deepagents`, separate from packaged kinds using the same conversation.

`deepagents_spec` requires `EXEC`, `FILES_IN` and `FILES_OUT`. An empty `egress_allow` selects `CLOSED`; named hosts select `ALLOWLIST`. `UNRESTRICTED` is refused, including on a manually constructed spec.

The working directory must be an explicit absolute POSIX path. `work_dir=None` is refused because the host must tell the model where its files live. Construction checks the spec and router; the first operation acquires the actual sandbox.

`sandbox.id` identifies the adapter using the key, kind, backend and network policy. It is not the physical instance ID used by the operator console.

## Commands

| Setting | Contract |
|---|---|
| Execution | Model-supplied shell string through `BoundedExec.exec_bounded` |
| Working directory | `"."`, resolved by the backend to the configured base |
| Default deadline | 120 seconds, including admission and cold acquisition |
| Default output cap | 1 MiB combined stdout/stderr, enforced by the backend before buffering |
| Result | Text with guest stderr prefixed by `[stderr]`, or producer-owned stderr by `[note]`; rendering that exceeds the cap is dropped |
| Overflow | Drop the output and return `truncated=True`; never return a partial successful result |

A backend without `BoundedExec` runs no command. Provider details stay in host logs; failures returned to the model use fixed messages. A timeout means the wait ended, not that guest execution is known to have stopped.

The model controls the shell. Backend isolation and network policy apply, but the packaged kinds' fixed-command behavior does not. Keep credentials out of the image.

## Files and paths

Deep Agents' file tools use absolute guest paths. Adapter uploads and downloads also accept relative paths beneath the configured base.

![An upload or download path is checked against the configured guest base. A path inside the base uses the backend's file methods and their confinement checks. A path outside the base uses bounded guest shell commands with base64 transfers, under the guest's existing authority and the same file caps. Both routes return Deep Agents file responses. The base is a transfer-routing boundary, not a restriction on the shell's whole filesystem access.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/deepagents-file-routes.svg)

| Path | Upload | Download |
|---|---|---|
| Inside the base | `write_file` | `stat_file`, then `read_file` |
| Outside the base | `write_file_over_exec` | `read_file_over_exec` |

The outside-base route supports paths such as `/conversation_history/` and `/tmp/`. It uses the same guest authority already available to `execute`. Uploads stage chunks in a sibling file, then move the completed file into place.

Both routes enforce `spec.files_in` or `spec.files_out`. A batch above `max_files` is refused whole. A file above its per-file cap or remaining total budget is refused individually. Downloads refuse oversize results rather than truncate them.

| File condition | Response |
|---|---|
| Missing file | `file_not_found` |
| Directory requested as a file | `is_directory` |
| Link under the base, invalid traversal or non-directory parent | `invalid_path` |
| Unsearchable parent or unreadable file | `permission_denied` |

A native stat/read timeout fails that file alone and leaves the sandbox available. An unfinished write or shell transfer requires disposal. An unfinished upload invalidates the batch; an unfinished shell download stops the remaining downloads.

## Lifetime and cleanup

![Operations enter the router's shared call lifecycle, then acquire or reuse the conversation sandbox. Successful operations release admission and leave it warm. An unfinished command or transfer queues exact-instance disposal. Already admitted sibling calls finish before the queued delete runs; later calls wait for cleanup before starting cold. Explicit close takes exclusive admission and disposes the instance this adapter acquired. Conversation purge is the host's recovery path.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/deepagents-lifetime.svg)

Successful operations keep the sandbox warm across conversation turns. Timeout, backend output overflow, cancellation or a missing command result queues disposal because guest completion is uncertain. Overflow caused only by formatting a completed result drops that result without requiring disposal.

The router coordinates overlapping calls. Adapter-queued deletion waits for the last call already allowed to run, and later calls wait for cleanup. A backend can independently invalidate the whole sandbox on failure; its own timeout and cancellation contract still applies.

After unfinished work, release and cleanup run on the process-owned `SyncRunner` loop. They can outlive the caller's loop and do not extend its response wait. Failed cleanup is not permission to reuse the instance.

`await sandbox.aclose()` takes exclusive admission, then disposes the exact instance this adapter acquired. It returns `False` when closure cannot be confirmed. Before acquisition, there is nothing to delete. Other kinds and replacements remain separate.

Wire `router.dispose_scope(scope, thread_id)` into the host's conversation-delete path. LangGraph thread deletion does not call it automatically. The adapter uses disposal, not per-operation reclamation or reset.

Async methods are native. Synchronous methods run the same coroutines through one shared `SyncRunner` thread and event loop per process. A forked child starts its own runner.

## Requirements and limits

- Python 3.12–3.14 and Deep Agents 0.7.x.
- A backend declaring `EXEC`, `FILES_IN` and `FILES_OUT`, with bounded execution. Docker and ACAS provide this; WSLC lacks output reads.
- Guest commands `sh`, `mkdir`, `mv`, `rm`, `base64` and `wc` for outside-base transfers. Missing commands can fail at transfer time.
- `python3` for Deep Agents' derived `ls`, `read_file`, `write_file`, `edit_file`, `glob` and `grep` tools. Without it, direct execution, shell deletion and adapter uploads/downloads remain available.

This adapter does not supply MAF information-flow labels, automatic hiding, per-call guest directories, declared outputs with a landing sink, or guest-to-host tool calls. Those are the [packaged kinds' contracts](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/kinds/README.md).

The [Docker Bicep sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/17_deepagents_docker_bicep) shows the complete integration with a model.
