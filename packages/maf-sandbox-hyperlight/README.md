# maf-sandbox-hyperlight

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-hyperlight/LICENSE)

> **Experimental.** This package warns on import with `MafSandboxHyperlightExperimentalWarning`. Releases before 1.0 may change or remove APIs without notice.

Run Python statements in Hyperlight microVMs through the `maf-sandbox` protocol. This backend supports `RUN_CODE` and `SNAPSHOT`, including CodeAct without file channels. It is experimental and has not yet been released.

## Requirements

Windows x86-64 with Windows Hypervisor Platform enabled and a host CPython version from 3.12 through 3.14. The validated configuration is Windows 11 / WHP / host CPython 3.13, using the exact matched `hyperlight-sandbox`, `hyperlight-sandbox-backend-wasm` and `hyperlight-sandbox-python-guest` 0.7.0 wheels. The guest is CPython 3.14 compiled to WebAssembly. Other operating systems, hypervisors, custom guests, images and guest working directories are refused.

Each sandbox has a dedicated worker process. The adapter sets `HYPERLIGHT_MAX_SURROGATES=0` inside that process, retains the WHP library handle, warms the packaged guest and takes its initial snapshot during acquire. A Windows job limits worker memory and kills its process tree when the owning host exits. The SDK materializes the packaged guest in its ordinary local application cache; no host directory is exposed to guest code.

One host process may own this backend on a machine at a time. Route acquire, execution and purge requests to that process. A second process refuses acquire and returns an unclean disposal result; it cannot silently report that another process's workers were deleted. Ownership lasts until the host process exits, including after `aclose()`. Use separate machines for independent backend hosts. Backend objects within the owner share the same key/kind registry.

## Direct execution

Install this package when its first release is available; the dependency pins select the compatible SDK and guest automatically. Repository development uses `uv sync`.

```python
import asyncio

from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend


async def main() -> None:
    backend = HyperlightSandboxBackend()
    key = SandboxKey(scope="user-1", thread_id="thread-1", agent_id="analyst")
    spec = SandboxSpec(
        kind="python",
        work_dir=None,
        requires=frozenset({Capability.RUN_CODE}),
    )
    try:
        sandbox = await backend.acquire(key, spec)
        result = await sandbox.run_code("answer = 6 * 7\nprint(answer)", timeout=5)
        print(result.stdout)  # 42
        await sandbox.reset(timeout=5)
    finally:
        await backend.aclose()


asyncio.run(main())
```

`acquire` reuses a live sandbox for the complete `SandboxKey` and kind. A different network policy, execution contract or resource configuration requires disposal first. `reset` restores the initial runtime state and rotates `instance_id`; disposal with an older ID cannot remove its replacement. Ordinary Python exceptions return a nonzero `ExecResult` and leave the runtime usable. Explicit reset is what removes accumulated Python state.

## CodeAct

Install `maf-sandbox-codeact` alongside this backend and explicitly select `CodeactRuntime(RUNTIME_INSTRUCTIONS)`. Its default exec variant requires capabilities this backend does not provide.

```python
from maf_sandbox import CallerContext, Cleanup, SandboxRouter
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import RUNTIME_INSTRUCTIONS, HyperlightSandboxBackend


def tools_for(context: CallerContext):
    backend = HyperlightSandboxBackend()
    router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
    tools = make_codeact_tools(
        router,
        "analyst",
        context,
        runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS),
    )
    return tools, backend
```

The host supplies `CallerContext` from trusted request state and calls `backend.aclose()` at shutdown. `Cleanup.RESET` permits warm reuse while removing state after each tool call; the router's stronger default disposal policy also works. CodeAct uses exclusive admission so the next call waits for the previous call's cleanup.

The guest has a reduced standard library: `json`, `math` and `re` are available; `datetime`, `statistics`, `pickle` and `__future__` are absent. Future imports fail. Programs execute statements and must print results; a final expression is not echoed. `RUNTIME_INSTRUCTIONS` describes this profile for the model. There is no shell, package installation, writable host filesystem, file-transfer channel or host-tool registration.

## Network policy

`CLOSED` is the default. For HTTP access, request `egress=Egress.ALLOWLIST` and supply exact hosts in `egress_allow`. Each host permits HTTP on port 80 and HTTPS on port 443 at all paths through the guest's `http_get(url)` and `http_post(url, body=..., content_type=...)` helpers. Raw sockets are unavailable. The pinned SDK always refuses CONNECT and TRACE. Wildcards, unrestricted egress, method-scoped rules and attached-identity rules are refused rather than weakened. Named hosts remain allowed after reset.

HTTP originates in the worker on the host's network. The host must choose destinations accordingly: allowing loopback or an internal service makes it reachable, and an allowlisted hostname is not an IP-address or DNS-rebinding filter. The worker receives only Windows runtime/cache location variables, not application credentials or proxy configuration, and the guest does not inherit the worker environment. No platform identity is attached by the adapter.

## Budgets and failure behavior

`HyperlightSandboxConfig` provides these limits:

| Field | Default | Meaning |
| --- | --- | --- |
| `startup_timeout` | 30 seconds | Acquisition queue and cold guest preparation |
| `cleanup_timeout` | 3 seconds | Additional allowance to terminate, reap and close a worker |
| `max_code_bytes` | 1 MiB | UTF-8 source bytes, at most 10 MiB |
| `max_output_bytes` | 1 MiB | Combined UTF-8 stdout/stderr, at most 16 MiB |
| `max_worker_memory_bytes` | 1.5 GiB | Windows job committed-memory limit for one worker tree, at most 16 GiB |

The fixed guest heap and stack are 400 MiB and 200 MiB. `run_code(timeout=...)` and `reset(timeout=...)` include their queue time. `SandboxQueuedTimeout` means no operation was submitted and the current guest remains usable. A started operation exceeding its deadline raises `TimeoutError`; cancellation propagates after terminating the worker. Cleanup may add `cleanup_timeout` to the operation budget. Oversized native results raise `HyperlightOutputLimitExceeded`. Protocol errors and worker crashes raise `HyperlightWorkerError`. These failures retire the sandbox; a later acquire prepares a new worker and identity.

Native output is buffered before its byte limit can be checked. The job's memory ceiling bounds that allocation; the deadline bounds endless output. The parent separately bounds retained worker diagnostics to 64 KiB while draining the pipe. Lowering the memory limit too far can make cold preparation fail.

Disposal is idempotent and returns `DisposalFailure` when cleanup cannot be confirmed. Failed targets stay registered for retry. `dispose_scope` sweeps all agents, kinds and call IDs in the owner's matching scope/conversation. `aclose()` raises on incomplete cleanup and only disposes targets created by that backend object.

## Validation and follow-ups

The ordinary tests use deterministic workers and real subprocess pipes. Windows-only tests exercise job memory limits and abrupt owner death without requiring WHP. Run the real guest suite separately, serially, on a WHP host:

```powershell
$env:MAF_HYPERLIGHT_LIVE = "1"
uv run pytest -q packages/maf-sandbox-hyperlight/tests/test_hyperlight_live.py
```

The live suite validates results, errors and reuse, reset, absent host environment and writable file channels, queued/program timeouts, cancellation, output limits, HTTP enforcement, selective disposal, scope purge and CodeAct under both router selection modes. It binds a temporary loopback HTTP server on port 80; that port must be available. CI runs the ordinary suite on Linux and the worker/job tests on Windows; hosted CI does not establish WHP execution. KVM, MSHV, Windows ARM64 and custom guests are unvalidated and refused.

The [backend contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/hyperlight.md) records the architecture and evidence. [#382](https://github.com/sokolaidev/maf-extensions/issues/382) remains the umbrella. Optional files are independent work under [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218), [#1219](https://github.com/sokolaidev/maf-extensions/issues/1219) and [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220); native host tools remain under [#369](https://github.com/sokolaidev/maf-extensions/issues/369).
