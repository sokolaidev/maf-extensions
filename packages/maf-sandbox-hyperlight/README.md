# maf-sandbox-hyperlight

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-hyperlight/LICENSE)

> **Experimental.** This package warns on import with `MafSandboxHyperlightExperimentalWarning`. Releases before 1.0 may change or remove APIs without notice.

Run Python statements in Hyperlight microVMs through the `maf-sandbox` protocol. This backend supports `RUN_CODE` and `SNAPSHOT`, with opt-in `FILES_OUT` for binary artifacts and CodeAct outputs.

## Requirements

Windows x86-64 with Windows Hypervisor Platform, or Linux x86-64 with glibc 2.28 or newer, KVM and a delegated cgroup v2 subtree. Host CPython versions 3.12 through 3.14 are supported by the pinned wheels. The measured configurations are Windows 11 / WHP / host CPython 3.13, Ubuntu 24.04 under WSL2 / KVM / host CPython 3.12, and native Ubuntu 24.04 / KVM / host CPython 3.13, using the exact matched `hyperlight-sandbox`, `hyperlight-sandbox-backend-wasm` and `hyperlight-sandbox-python-guest` 0.7.0 wheels. The guest is CPython 3.14 compiled to WebAssembly. Other operating systems, architectures, hypervisors, custom guests and images are refused. The only explicit guest working directory is `/output`, available with file outputs enabled. Linux hosts exposing `/dev/mshv` are refused until that family is validated.

Each sandbox has a dedicated worker process. The adapter sets `HYPERLIGHT_MAX_SURROGATES=0` inside that process, initializes the platform, warms the packaged guest and takes its initial snapshot during acquire. Windows retains the WHP library handle and uses a job to bound committed memory and terminate the worker tree. Linux verifies KVM VM creation and uses cgroup memory enforcement with an independent lifetime watcher. Linux forwards only the exact environment names `PATH`, `HOME`, `XDG_CACHE_HOME`, `TMPDIR`, `LANG` and `LC_ALL`; Windows matches its system/runtime environment names case-insensitively. The SDK materializes the packaged guest in its ordinary local application cache. File outputs expose only an adapter-owned directory through the guest's `/output` preopen; the default exposes no host directory.

One host process owns this backend within its ownership namespace. Windows uses a machine-wide named event; Linux holds `/run/lock/maf-sandbox-hyperlight.lock` open with an exclusive lock. New lock files are empty, readable by every host user and have no write permissions, regardless of the creating process's umask. The final path is published atomically only after those permissions are ready, without replacing an existing lock. Ordinary Python fork children release their inherited owner descriptor; ownership remains with the original host and its lifetime watchers. Route acquire, execution and purge requests to that process. A second process refuses acquire and returns an unclean disposal result. Ownership lasts until the host exits, including after `aclose()`; Linux watchers retain the lock until old worker trees are gone. Backend objects within the owner share the same key/kind registry. Do not unlink the lock file or use separate mount/PID/cgroup namespaces to route one logical backend across owners. Replicated containers and cross-machine routing require additional deployment work.

### Linux and WSL2 setup

The host user needs read/write access to `/dev/kvm`. WSL2 needs hardware virtualization exposed by Windows, KVM support enabled in its Linux kernel, and nested virtualization enabled on a supported Windows host; see the [WSL configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config#configuration-settings-for-wslconfig). Check the device and follow the [Hyperlight KVM prerequisites](https://hyperlight.org/guides/getting-started/#prerequisites). Acquire attempts actual VM creation, so a device node alone does not satisfy admission.

The operator supplies a writable cgroup v2 root with the memory controller enabled for children, `memory.swap.max`, `memory.oom.group`, `cgroup.kill` and pidfd support. The default root is `/sys/fs/cgroup/maf-sandbox-hyperlight`; set `HyperlightSandboxConfig(linux_cgroup_root="/sys/fs/cgroup/your-delegated-subtree")` when a service manager delegates another path. Keep the application in a leaf beneath that subtree so the root has no processes. The application needs permission to create worker groups and migrate its workers at their common ancestor. The library never mounts cgroups, enables ancestor controllers or elevates privileges.

For local development, on a host whose root cgroup already offers the memory controller, an operator can prepare the subtree and launch a host as the calling user. The calling user must already have KVM device access; replace the interpreter and application arguments with their absolute paths:

```bash
sudo sh -c '
  set -eu
  root=/sys/fs/cgroup/maf-sandbox-hyperlight
  mkdir -p "$root/host"
  printf +memory > "$root/cgroup.subtree_control"
  chown "$SUDO_UID:$SUDO_GID" "$root" "$root/cgroup.procs"
  printf "%s" "$$" > "$root/host/cgroup.procs"
  exec setpriv --reuid "$SUDO_UID" --regid "$SUDO_GID" --init-groups -- "$@"
' sh /absolute/path/to/.venv/bin/python /absolute/path/to/app.py
```

A supervisor starts in a separate process session and inherits the ownership lock before creating the worker cgroup or process. It configures `memory.max`, zero swap allowance and group OOM enforcement; a trusted bootstrap joins that cgroup before executing the worker command. The supervisor holds pidfds for the host and worker and remains outside the worker's memory group. Host exit, worker exit or an explicit close triggers worker termination and `cgroup.kill`, including descendants that change process session. Cleanup also covers startup before the bootstrap joins the group, waits for the kernel's empty-group indication and removes only that worker's cgroup. The supervisor is trusted host infrastructure and must remain running until cleanup completes. A cleanup timeout or unexpected supervisor exit reports an unclean result; ownership remains held while the supervisor retries cleanup. Service managers should own the delegated subtree's lifetime as well. Missing delegation or enforcement controls refuses acquisition; there is no fallback to an unbounded worker.

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

The guest has a reduced standard library: `json`, `math` and `re` are available; `datetime`, `statistics`, `pickle` and `__future__` are absent. Future imports fail. Programs execute statements and must print results; a final expression is not echoed. `RUNTIME_INSTRUCTIONS` describes this profile for the model. There is no shell, package installation, input transfer or host-tool registration.

## Output files

Set `HyperlightSandboxConfig(file_outputs=True)` to declare `FILES_OUT`. Each sandbox gets one private output directory for its lifetime, exposed as `/output`. `SandboxSpec.work_dir` can be `None` or `/output`. Programs write flat files with `open('/output/result.bin', 'wb')`; collect them as relative names such as `result.bin`, with `working_directory="."`. Absolute names, `..` components, Windows path aliases, nested paths, links and special files are refused. The pinned guest cannot create directories or links. The backend checks for symlinks, redirecting reparse points and hardlinks before execution and restore and after execution, and reads files through host operations without running inspection code in the guest. `FILES_LIST`, writable inputs and selective deletion remain unavailable.

The native write budget is 8 MiB per file, 32 MiB total and 64 files. `read_file(max_bytes=...)` applies the smaller of its requested limit and the backend's per-file limit, and raises `SandboxTransferCapExceeded` on overflow instead of returning a prefix. Core collection also enforces the caller's total-byte and file-count limits.

Hyperlight declares `requires_exclusive_admission=True`: a router holds each call through execution, collection, sink delivery and cleanup. Backend admission also serializes the same key/kind across router objects and event loops in the owning process. Different sandbox instances can run concurrently. All admissions held by a task remain authorized, and the router transfers the target lease's authority when cleanup runs in another task or event loop. Direct callers of a file-enabled backend must wrap acquire, execution, reads and reset/disposal in `async with backend.call_admission(key, spec, owner=unique_call_id, timeout=30):`; access outside the active scope refuses. Reads pin the owned output root and validate the opened file before consuming bytes: Windows uses non-following handles with read-only sharing, and Linux opens relative to a verified directory descriptor with `O_NOFOLLOW`. Collect files before the next `run_code` or `reset`, since both clear previous native outputs. Reset retains the directory and clears its files; disposal deletes it only after worker termination. Failed termination retains storage for cleanup retry. Administrative disposal during another call refuses. Abrupt host exit can leave private temporary output directories; deployment retention cleanup must cover them.

For CodeAct, pass the file-enabled backend to the router and select this profile:

```python
from maf_sandbox_codeact import CodeactRuntime
from maf_sandbox_hyperlight import FILE_RUNTIME_INSTRUCTIONS

runtime = CodeactRuntime(
    FILE_RUNTIME_INSTRUCTIONS,
    guest_work_dir="/output",
    use_call_directory=False,
)
```

Pass `runtime`, an `output_sink` and `outputs=CodeactOutputs.DECLARED` or `CodeactOutputs.MANIFEST` to `make_codeact_tools`. Programs use `guest_call_path + '/name'`. The prepared base replaces CodeAct's usual per-call subdirectory because the pinned guest cannot create directories. Whole-sandbox cleanup still applies after every call, including failed delivery and cancellation; `Cleanup.RESET` permits warm reuse.

## Network policy

`CLOSED` is the default. For HTTP access, request `egress=Egress.ALLOWLIST` and supply exact hosts in `egress_allow`. Each host permits HTTP on port 80 and HTTPS on port 443 at all paths through the guest's `http_get(url)` and `http_post(url, body=..., content_type=...)` helpers. Raw sockets are unavailable. The pinned SDK always refuses CONNECT and TRACE. Wildcards, unrestricted egress, method-scoped rules and attached-identity rules are refused rather than weakened. Named hosts remain allowed after reset.

HTTP originates in the worker on the host's network. The host must choose destinations accordingly: allowing loopback or an internal service makes it reachable, and an allowlisted hostname is not an IP-address or DNS-rebinding filter. The worker receives only platform runtime/cache/locale variables, not application credentials or proxy configuration, and the guest does not inherit the worker environment. No platform identity is attached by the adapter.

## Budgets and failure behavior

`HyperlightSandboxConfig` provides these limits:

| Field | Default | Meaning |
| --- | --- | --- |
| `startup_timeout` | 30 seconds | Acquisition queue and cold guest preparation |
| `cleanup_timeout` | 3 seconds | Additional allowance to terminate, reap and close a worker |
| `max_code_bytes` | 1 MiB | UTF-8 source bytes, at most 10 MiB |
| `max_output_bytes` | 1 MiB | Combined UTF-8 stdout/stderr, at most 16 MiB |
| `max_worker_memory_bytes` | Windows: 1.5 GiB; Linux: 3 GiB | Per-worker-tree Windows committed-memory or Linux cgroup-accounted memory ceiling, at most 16 GiB; Linux rounds down to a whole page and disables swap |
| `linux_cgroup_root` | `None` | Linux uses `/sys/fs/cgroup/maf-sandbox-hyperlight` unless an absolute delegated path is supplied; unused on Windows |
| `file_outputs` | `False` | Enable the private `/output` directory and `FILES_OUT` |

The fixed guest heap and stack are 400 MiB and 200 MiB. `run_code(timeout=...)` and `reset(timeout=...)` include their queue time. `SandboxQueuedTimeout` means no operation was submitted and the current guest remains usable. A started operation exceeding its deadline raises `TimeoutError`; cancellation propagates after terminating the worker. Cleanup may add `cleanup_timeout` to the operation budget. Oversized native results raise `HyperlightOutputLimitExceeded`. Protocol errors and worker crashes raise `HyperlightWorkerError`. These failures retire the sandbox; a later acquire prepares a new worker and identity.

Native output is buffered before its byte limit can be checked. The worker's kernel memory ceiling bounds that allocation; the deadline bounds endless output. Linux cgroups account charged memory, not virtual-address reservations, and may terminate the group on OOM. The parent separately bounds retained worker diagnostics to 64 KiB while draining the pipe. Lowering the memory limit too far can make cold preparation fail; the measured KVM startup exceeded 1.5 GiB, so Linux has a larger default.

Disposal is idempotent and returns `DisposalFailure` when cleanup cannot be confirmed. Failed targets stay registered for retry. Cancellation propagates after the active worker's bounded cleanup attempt finishes, without starting another target; unreported targets remain registered for retry. `dispose_scope` sweeps all agents, kinds and call IDs in the owner's matching scope/conversation. `aclose()` raises on incomplete cleanup and only disposes targets created by that backend object.

## Validation and follow-ups

The ordinary tests use deterministic workers and real subprocess pipes. Windows tests exercise job memory limits and abrupt owner death without requiring WHP. Linux kernel tests exercise cgroup OOM, process-tree termination and owner-lock retention without requiring KVM. In a repository checkout, run those Linux tests using `sudo python3 scripts/check_hyperlight_linux.py --python "$PWD/.venv/bin/python"`; the helper creates only a temporary test subtree and drops privileges before running pytest.

Run the real guest suite separately and serially. On Windows:

```powershell
$env:MAF_HYPERLIGHT_LIVE = "1"
uv run pytest -q packages/maf-sandbox-hyperlight/tests/test_hyperlight_live.py
```

On Linux, `sudo python3 scripts/check_hyperlight_linux.py --live --python "$PWD/.venv/bin/python"` provisions a temporary test subtree and runs the real guest suite as the calling user. Alternatively, use an already delegated host as described above, set `MAF_HYPERLIGHT_LIVE=1` and `MAF_HYPERLIGHT_CGROUP_ROOT` to the delegated root, and run the same pytest file. The HTTP-policy test needs permission to bind the available loopback port 80. The local WSL2 record ran the other twelve guest scenarios unprivileged and the HTTP test with that binding permission.

The live suite validates results, errors and reuse, reset, absent host environment values and refused guest writes, queued/program timeouts, cancellation, output limits, HTTP enforcement, selective disposal, scope purge, abrupt worker death and CodeAct under both router selection modes. Two of its scenarios are Linux only: a memory ceiling below what the real guest needs, which the kernel enforces by killing the worker group, and abrupt owner death while a guest program loops inside a native call. CI runs portable tests and dedicated Linux/Windows worker enforcement jobs. The Linux job also runs the real KVM guest suite as an unprivileged host with KVM access and delegated cgroups, recording its environment and exact dependency versions; unavailable KVM or a failed guest scenario fails the job. The local WSL2 guest measurement on 2026-09-18 passed all thirteen scenarios on Ubuntu 24.04.4, kernel `6.18.40.1-microsoft-standard-WSL2`, CPython 3.12.3. [Native Linux CI](https://github.com/sokolaidev/maf-extensions/actions/runs/35330572586/job/105553773894) passed all thirteen guest scenarios on Ubuntu 24.04.5 LTS, kernel `6.17.0-1022-azure`, x86-64 and CPython 3.13.15 with the exact 0.7.0 trio. MSHV, ARM64, ACA and AKS were not measured. MSHV, ARM64 and custom guests remain refused.
