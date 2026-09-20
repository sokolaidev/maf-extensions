# maf-sandbox-tui

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-tui/LICENSE)

> **Experimental.** The control protocol, discovery files and UI may change without notice. Importing the package emits `MafSandboxTuiExperimentalWarning`.

MST is a local operator console for sandboxes owned by applications that enable it. It lists physical instances, shows their MAF keys and asks the owning application to delete an exact instance.

![The operator's TUI or CLI discovers a loopback endpoint published by the application. That endpoint calls the application's control adapter, which uses the same monitored router and backend that serve agent work. The host scheduler stops new work and drains active calls before deletion. Inventory comes from admitted acquisitions and observed worker state. MST does not scan arbitrary VMs or kill worker processes directly. The unauthenticated loopback endpoint is reachable by local processes while enabled.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/tui-control-boundary.svg)

## Try it

The demo provides three sample Hyperlight records through a local control server. It does not create real sandboxes.

```powershell
uv run mst --demo
```

Use the arrows to select, `r` to refresh, `d` to review deletion and `q` to quit. For a non-interactive check:

```powershell
uv run mst --demo --json
```

## Commands

Run `mst` without a command to open the TUI. Subcommands print tables or records. `--json` selects JSON; `watch --jsonl` emits one snapshot per line. Connection options work before or after a command.

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

`--older-than` measures time since the last observed lifecycle signal. It does not prove the sandbox has been idle between snapshots.

| Operation | Scope and confirmation |
|---|---|
| `delete` | One physical instance; a replacement has a different ID |
| `purge-thread` | The conversation on every responsive local host; unavailable hosts make the result partial |
| Either deletion command | Prompts in a terminal; scripts and JSON mode require `--yes` |
| `--timeout` | Can shorten the operation; the host's configured limit is the upper bound |

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Endpoint failure or incomplete operation |
| `2` | Invalid input or missing confirmation |
| `3` | Physical instance absent |
| `4` | Operator declined |
| `130` | Watch interrupted |

## Host a Hyperlight backend

Use `MonitoredSandboxBackend` and `MonitoredSandboxRouter` together. Pass the same router to agent work and the control adapter. Only acquisitions admitted through this pair enter the live inventory.

The server opens nothing until `start()` runs. Enable it through host configuration that defaults off.

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

`application_lifecycle`, `settings` and `run_application` are host-supplied. The lifecycle callbacks must stop new work and drain active calls for the conversation. Every start path, on every application replica, must use that coordination. If the host cannot provide a callback's guarantee, omit it; MST refuses that operation.

The wrapper forwards backend admission and requires observed worker exit to confirm physical disposal. A backend without that signal stays visible with an unconfirmed outcome.

The generic monitor reports `ready` when the worker is observed running and `failed` when it has exited or cannot be checked. It does not infer active execution from private backend locks. Backend-specific inventory can report richer states. OpenTelemetry provides the separate history and audit records.

## Exact-instance deletion

![Deletion starts with the selected physical instance ID. The owning host stops new work and drains active calls, then checks that the same ID, key and kind still match. It asks the router to dispose that exact instance and verifies the backend's disposal receipt and observed worker exit. Confirmed removal returns disposed; a missing or replaced instance returns not_found; timeout or uncertain removal returns failed and unconfirmed. A later outcome does not change the timed-out command's result.](https://raw.githubusercontent.com/sokolaidev/maf-extensions/45a84dd48c90bdf6158e1fab0f9ed57004dff933/docs/sandbox/assets/tui-disposal-flow.svg)

A reset or replacement changes `instance_id`. The host checks it again after draining work, so a stale screen cannot delete a replacement. An ID reported for multiple keys or kinds is refused.

All discovery and deletion steps share the operation deadline. Exact deletion cannot begin after that deadline. A timed-out operation is unconfirmed, even if a delayed task later completes.

After uncertain cancellation, affected instances are withheld from public inventory. The monitor still counts them when checking whether a later conversation purge is complete.

## Local control and shutdown

The server binds `127.0.0.1` on an ephemeral port and publishes a per-user discovery file. On POSIX, the discovery directory must belong to the current user and be inaccessible to others.

The prototype has no authentication. Any local process that can reach the listener can use it. Windows discovery needs a user ACL or named-pipe transport before production use. Keep the listener local; remote control requires a separate authenticated transport.

| Method | Version-one route |
|---|---|
| `GET` | `/v1/health`, `/v1/sandboxes`, `/v1/sandboxes/{instance_id}` |
| `DELETE` | `/v1/sandboxes/{instance_id}` |
| `DELETE` | `/v1/scopes/{scope}/threads/{thread_id}` |

`server.close()` withdraws discovery, closes clients and waits briefly for cancelled operations. Host work that delays cancellation can outlive it. The host must wait for that work before tearing down its router and backend. Restarting the same server object is refused until prior operations and teardown settle.

## Updates

Version checks run only when requested and use the fixed HTTPS PyPI endpoint.

| Environment | Update behavior |
|---|---|
| Isolated `uv tool` or pipx install | Delegates to that package manager |
| Project or manually managed environment | Refuses self-update and prints the pinned requirement |
| `--to VERSION` | Supports upgrades and rollbacks; verifies the installed version |
| `--prerelease` | Includes non-yanked prereleases when choosing the newest version |
| `--timeout` | Bounds each probe, lookup, package-manager process and verification |

Pipx self-update requires pipx 1.16 or newer. Older owners report `self_updatable=false` and require a pipx upgrade.
