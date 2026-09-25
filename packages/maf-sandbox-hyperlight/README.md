# maf-sandbox-hyperlight

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-hyperlight)](https://pypi.org/project/maf-sandbox-hyperlight/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-hyperlight/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxHyperlightExperimentalWarning`.

Run Python statements in Hyperlight microVMs. Each sandbox has a dedicated worker process and a warmed Python state that can be restored with `reset`.

```bash
pip install maf-sandbox-hyperlight
```

## Requirements

| Host | Required setup |
|---|---|
| Windows x86-64 | Windows Hypervisor Platform and host CPython 3.12–3.14. |
| Linux x86-64 | glibc 2.28 or newer, KVM, host CPython 3.12–3.14 and a delegated cgroup v2 subtree. |
| WSL2 | The Linux requirements, plus KVM and nested virtualization exposed by the Windows host. |

The package pins the SDK, Wasm backend and Python guest together at 0.7.0. The guest is CPython 3.14 compiled to WebAssembly. ARM64, Linux MSHV, custom guests and custom images are refused.

`json`, `math` and `re` are available. `datetime`, `statistics`, `pickle` and `__future__` are absent. Programs run statements and must print results; final expressions are not echoed. There is no shell or package installation.

The backend declares `RUN_CODE`, `SNAPSHOT` and `EGRESS_METHODS`, plus `FILES_OUT` and `FILES_LIST` when output files are enabled. It supports conversation scope, one call at a time per sandbox. It provides no command execution, input transfer or host-tool registration.

<a id="linux-and-wsl2-setup"></a>

### Linux setup

The host user needs read/write access to `/dev/kvm`. Acquisition attempts VM creation; the device's presence alone is insufficient.

The operator must delegate a writable cgroup v2 subtree with the memory controller enabled for children. It must support `memory.swap.max`, `memory.oom.group`, `cgroup.kill` and pidfds. The default is `/sys/fs/cgroup/maf-sandbox-hyperlight`; set `linux_cgroup_root` to another delegated path when needed.

Keep the application in a leaf beneath that subtree. Its root must contain no processes. The application needs permission to create worker groups and move workers at their common ancestor. The library does not mount cgroups, enable ancestor controllers or elevate privileges.

For local development, an operator can prepare a subtree and launch the application as the calling user. This assumes the root cgroup already provides the memory controller and the user already has KVM access:

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

## Direct execution

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
        print(result.stdout_text)
        await sandbox.reset(timeout=5)
    finally:
        await backend.aclose()


asyncio.run(main())
```

Acquisition reuses the live sandbox for the same complete key and kind. Ordinary Python exceptions return a failed `ExecResult` and leave it usable. Reset removes accumulated Python state and changes `instance_id`.

A different network policy or execution contract requires disposal first. An old instance ID cannot delete its replacement.

## CodeAct

Install `maf-sandbox-codeact` and explicitly select the runtime profile:

```python
from maf_sandbox import Cleanup, SandboxRouter
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import HyperlightSandboxBackend, RUNTIME_INSTRUCTIONS

backend = HyperlightSandboxBackend()
router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
tools = make_codeact_tools(
    router,
    "analyst",
    context,
    runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS),
)
```

The host supplies `CallerContext` and closes the backend at shutdown. `Cleanup.RESET` permits warm reuse while restoring the baseline after each call. The router's default disposal policy also works. CodeAct's default exec variant is incompatible with this backend.

## Output files

Set `HyperlightSandboxConfig(file_outputs=True)`. Each sandbox receives one private directory exposed as `/output`. `work_dir` may be `None` or `/output`.

This configuration adds `FILES_OUT` and `FILES_LIST`. `list_dir(".", working_directory=".")` returns sorted direct child names with trusted kinds and regular-file sizes. Empty storage returns an empty tuple. Links and Windows reparse entries are reported as links; hardlinks and special entries are reported as `OTHER`, without readable sizes. Child directories can be reported but cannot be enumerated.

Listing is limited to 64 entries and 64 KiB of UTF-8 filenames, counting every entry kind. Overflow, an inspection failure or an entry replaced during inspection refuses the entire listing. Enumeration stays bound to the acquired root and excludes execution, reset and storage deletion until inspection finishes.

Programs write files such as `/output/result.bin`. Collection uses flat relative names such as `result.bin`, with `working_directory="."`. Nested paths, links, special files, traversal and Windows path aliases are refused.

The native write limits are 8 MiB per file, 32 MiB total and 64 files. `read_file(max_bytes=...)` also enforces the requested cap and refuses overflow without returning a prefix. Core applies its own collection limits.

Collect and deliver files before the next execution or reset; both clear previous outputs. Reset keeps the directory. Disposal removes it after confirmed worker termination. Failed termination retains it for retry, and abrupt host exit can leave storage for deployment cleanup.

The router holds exclusive admission through execution, collection, delivery and cleanup. Direct file-enabled callers must hold `backend.call_admission(key, spec, owner=unique_call_id, timeout=30)` around acquire, execution, listing, reads and cleanup. File access outside that scope refuses.

For CodeAct outputs, use this profile with an output sink and `CodeactOutputs.DECLARED` or `MANIFEST`:

```python
from maf_sandbox_codeact import CodeactRuntime
from maf_sandbox_hyperlight import FILE_RUNTIME_INSTRUCTIONS

runtime = CodeactRuntime(
    FILE_RUNTIME_INSTRUCTIONS,
    guest_work_dir="/output",
    use_call_directory=False,
)
```

Programs use `guest_call_path + '/name'`. The guest cannot create directories, so this profile uses the prepared base instead of a call subdirectory.

## Network access

`CLOSED` is the default. For HTTP access, select `Egress.ALLOWLIST` and exact hosts in `egress_allow`. Guest helpers `http_get` and `http_post` can then use HTTP port 80 and HTTPS port 443 at any path on those hosts.

An `EgressRule(host, methods=...)` limits a host to GET, HEAD, POST, PUT, PATCH, DELETE or OPTIONS. The runtime checks the method before connecting, including for raw wasi-http requests. It refuses CONNECT, TRACE and custom methods, so a rule naming one is refused.

Raw sockets, wildcard hosts, unrestricted access, path rules and attached-authority rules are unavailable. Reset preserves the allowlist.

Requests originate on the host network. Allowing an internal or loopback hostname makes it reachable; hostname policy does not filter resolved IP addresses. The worker receives only selected platform variables, and the guest receives no application credentials.

## Limits and failures

| `HyperlightSandboxConfig` field | Default |
|---|---|
| `startup_timeout` | 30 seconds for acquisition queue and cold preparation |
| `cleanup_timeout` | 3 additional seconds to stop and reap a worker |
| `max_code_bytes` | 1 MiB; maximum 10 MiB |
| `max_output_bytes` | 1 MiB combined stdout/stderr; maximum 16 MiB |
| `max_worker_memory_bytes` | 1.5 GiB on Windows, 3 GiB on Linux; maximum 16 GiB |
| `linux_cgroup_root` | `None`, selecting the default delegated path |
| `pod` | `None`; explicitly selects supervised aggregate container containment when set |
| `file_outputs` | `False` |

Guest heap and stack are fixed at 400 MiB and 200 MiB. Native output is buffered before the byte check; the worker's kernel memory limit and execution deadline bound that work. Retained worker diagnostics are separately limited to 64 KiB.

Execution and reset deadlines include queue time. `SandboxQueuedTimeout` means nothing was submitted, so the worker remains usable. An active timeout, cancellation, native-output overflow or worker failure retires the sandbox. Cleanup can add `cleanup_timeout` to the response time.

## Ownership and shutdown

For the upstream Kubernetes deployment, run one `(scope, thread_id, agent_id, kind)` per pod, with the application and adapter together. Read `HyperlightPodConfig.from_environment()` inside the supervisor's application process and pass it as `pod`, together with `max_worker_memory_bytes=None`. This mode uses the whole container's budget and retires the whole pod on active failure. It does not promise that the application survives worker OOM. The default local containment remains unchanged. See the [AKS deployment instructions](https://github.com/sokolaidev/maf-extensions/blob/main/images/hyperlight-sandbox/README.md) for the required controller, image and permissions.

One host process owns the backend within its shared ownership namespace. Backend objects in that process share the key/kind registry. Route acquisition, execution and purge to that owner.

Windows uses a machine-wide event and a job for worker containment. Linux uses `/run/lock/maf-sandbox-hyperlight.lock`, cgroups and a trusted supervisor. The supervisor remains outside the worker's memory group and holds ownership until the worker tree is gone.

A second owner refuses acquisition and reports unclean disposal. Ownership lasts until the host exits, including after `aclose()`. Do not unlink the lock or assume separate container namespaces coordinate one logical backend.

Failed disposal stays registered for retry. `dispose_scope` covers the owner's matching conversation. `aclose()` disposes that backend object's targets and raises on incomplete cleanup.

## Verification

Real guest execution is measured on Windows WHP and Linux KVM, including suitable WSL2 hosts. Offline tests and kernel containment tests cover separate parts of the contract. ARM64, MSHV and remote-worker deployments are not supported.

Run the real guest suite separately and serially. On Windows:

```powershell
$env:MAF_HYPERLIGHT_LIVE = "1"
uv run pytest -q packages/maf-sandbox-hyperlight/tests/test_hyperlight_live.py
```

On Linux, `sudo python3 scripts/check_hyperlight_linux.py --live --python "$PWD/.venv/bin/python"` prepares a temporary test subtree and runs as the calling user. Omit `--live` for kernel containment tests without KVM. The HTTP test needs permission to bind loopback port 80.

See the [backend guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/hyperlight.md) for worker lifecycle and deployment limits, and the [measurement record](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/hyperlight-backend.md) for tested environments.
