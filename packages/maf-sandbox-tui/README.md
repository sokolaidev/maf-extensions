# maf-sandbox-tui

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-tui/LICENSE)

> **Experimental.** This package warns on import with `MafSandboxTuiExperimentalWarning`. Its prototype protocol, discovery files and UI may change without notice.

MST is a keyboard-first operator console for sandboxes owned by opted-in MAF applications. It lists physical instances, shows their trusted MAF key and lifecycle signal, and asks the owning router to dispose one exact generation. It does not scan for arbitrary Hyperlight VMs and never kills a worker process directly.

## Try the complete local flow

The demo starts a loopback control endpoint, connects the TUI to it, and supplies three representative Hyperlight records. Press `r` to refresh, select a row with the arrow keys, press `d` to review an exact-instance disposal, and `q` to quit.

```powershell
uv run mst --demo
```

For a non-interactive protocol check:

```powershell
uv run mst --demo --json
```

## Use MST from scripts

Running `mst` without a command opens the TUI. Subcommands print plain tables or records; `--json` selects stable JSON and `watch --jsonl` writes one compact snapshot per line. Connection options may appear before or after the command.

```console
mst version [--json]
mst update --check [--prerelease] [--json]
mst update [--prerelease] [--json]
mst update --to VERSION [--json]
mst hosts [--json]
mst list [--host SOURCE] [--backend NAME] [--scope SCOPE] [--thread ID] [--kind KIND] [--state STATE] [--older-than 5m] [--json]
mst show INSTANCE_ID [--json]
mst watch [--interval 2] [--count 0] [--jsonl]
mst delete INSTANCE_ID [--timeout 10] [--yes] [--json]
mst purge-thread --scope SCOPE --thread ID [--timeout 10] [--yes] [--json]
```

`--older-than` compares the age of the last lifecycle signal observed by MST. It is not an execution-idle guarantee: an execution that starts and finishes between inventory snapshots may not change that timestamp.

Version checks are explicit and read the fixed HTTPS PyPI project endpoint; MST never checks in the background. An update is delegated only when the running executable belongs to an isolated `uv tool` or pipx environment. `mst update --to VERSION` also permits an explicit rollback and verifies the installed version after the manager finishes. In a project or manually managed virtual environment, MST refuses to rewrite its own dependencies and names the pinned requirement for that environment's package manager and lock file. `--prerelease` includes non-yanked prereleases when choosing the newest version, and `--timeout` bounds each manager probe, version lookup, package-manager process and post-update verification.

Exact-version self-update through pipx requires pipx 1.16 or newer, whose `install --upgrade` accepts both upgrades and rollbacks. For an older pipx owner, MST reports `self_updatable=false` and asks to upgrade pipx instead of using `--force` to replace executables.

`delete` resolves the current record and still sends the physical `instance_id`, so a concurrent replacement is protected. `purge-thread` deliberately has a larger blast radius: it asks every responsive local host to purge the conversation and reports a partial result if any discovered host is unavailable. Destructive commands prompt on an interactive terminal and require `--yes` in scripts or JSON mode. `--timeout` may shorten an operation, but the application host's configured disposal timeout remains the upper bound.

Composite exact delete and conversation purge bound cancellation settlement when an endpoint catches cancellation and delays settling. Exact delete does not start owner disposal after its shared deadline expires, even if ownership probes or event-loop scheduling consume the remaining time. A timed-out command reports an unconfirmed result and retains late tasks for completion callbacks; a later physical outcome is never retroactively reported as success for that command.

Successful commands exit zero. Endpoint or incomplete-operation failures use `1`, invalid or missing confirmation uses `2`, an absent physical instance uses `3`, and an operator declining confirmation uses `4`. An interrupted watch uses `130`.

## Host a real Hyperlight backend

The MAF application remains the sandbox authority. Merely importing or constructing `SandboxControlServer` opens nothing. A host configuration that defaults off must opt in before the application starts the server on its event loop, passing the same backend and router that serve CodeAct calls.

```python
from maf_sandbox import Cleanup, SandboxKey
from maf_sandbox_hyperlight import HyperlightSandboxBackend
from maf_sandbox_tui import HyperlightControl, MonitoredSandboxBackend, MonitoredSandboxRouter, SandboxControlServer

backend = HyperlightSandboxBackend()
monitored = MonitoredSandboxBackend(backend)
router = MonitoredSandboxRouter([monitored], min_cleanup=Cleanup.RESET)
def quiesce_instance(key: SandboxKey):
    return application_lifecycle.quiesce(key.scope, key.thread_id)

async def purge_conversation(scope: str, thread_id: str):
    async with application_lifecycle.quiesce(scope, thread_id):
        return await router.dispose_scope(scope, thread_id)

control = HyperlightControl(
    monitored,
    router,
    source_id="research-agent",
    quiesce_instance=quiesce_instance,
    quiesced_purge=purge_conversation,
)
server = SandboxControlServer(control, source_id="research-agent")

try:
    if settings.enable_local_sandbox_control:
        await server.start()
    await run_application(router)
finally:
    if settings.enable_local_sandbox_control:
        # Host-provided lifecycle coordination waits for local control work to settle.
        await server.close()
        await application_lifecycle.wait_for_control_settlement()
    await backend.aclose()
```

When enabled, the server binds an ephemeral port on the literal loopback address `127.0.0.1` and publishes the address in a per-user discovery file. `mst` discovers responsive local endpoints automatically. On POSIX hosts, MST atomically creates the discovery directory and refuses one that is not owned by the current user or is accessible to another user. There are deliberately no keys in this local prototype. Loopback is machine-local, not user-private: any local process that can reach the listener can use it while the host has it enabled. Windows discovery still needs an explicit user ACL or a named-pipe transport before production use. Do not proxy, forward or expose the listener outside the host; remote control requires a separately designed authenticated transport.

`application_lifecycle.quiesce` represents the host's conversation scheduler; it is not supplied by MST. Every path that starts work for that conversation, on every application replica, must participate in the same boundary. `quiesce_instance` must fence new work and drain active calls for the key before exact disposal, and MST rechecks the generation inside that boundary. Omit either callback if the host cannot provide its guarantee; MST will refuse that disposal operation without calling the router.

## Control protocol

Version one exposes `GET /v1/health`, `GET /v1/sandboxes`, `GET /v1/sandboxes/{instance_id}`, `DELETE /v1/sandboxes/{instance_id}`, and `DELETE /v1/scopes/{scope}/threads/{thread_id}`. Exact delete calls `SandboxRouter.dispose_kind` only inside host-provided quiescence and verifies that the physical instance disappeared. Conversation purge invokes the host-provided quiesced purge under a shared timeout and aggregates outcomes across responsive hosts. A reset or replacement rotates the identifier, so a stale screen cannot remove the newer sandbox at the same logical MAF key.

If one host reports the same physical ID for multiple keys or kinds, its show and exact-delete routes refuse that ID. A cancelled backend disposal with uncertain physical outcome withholds affected generations from the public inventory; the monitor still counts them when deciding whether a later conversation purge is complete.

Server shutdown withdraws discovery, closes clients and waits briefly for canceled control operations. If a host operation delays cancellation, server shutdown can finish while that operation remains registered until it settles; restarting the same server object is refused until prior operations and teardown settle. The host must coordinate its own router/backend teardown with any still-running work rather than treat server closure as disposal confirmation.

Live inventory comes from acquisitions admitted by `MonitoredSandboxRouter` through `MonitoredSandboxBackend`; applications must use both instead of registering the wrapped backend directly. The wrapper forwards optional backend admission unchanged, preserving Hyperlight ownership through output delivery and cleanup without serializing ordinary backends. The wrapper requires an observed worker process exit before confirming physical disposal; a backend without that signal remains visible and is reported as unconfirmed. It depends only on `maf-sandbox` and leaves backend packages unchanged. It reports a tracked acquisition as `ready` while its worker is observed running and as `failed` when the worker has exited or its liveness cannot be checked; it does not infer active execution from backend-private locks. A backend-specific inventory may provide richer lifecycle states. `maf-sandbox-otel` remains the complementary history and audit surface.
