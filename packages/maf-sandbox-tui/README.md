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

Version checks are explicit and read the fixed HTTPS PyPI project endpoint; MST never checks in
the background. An update is delegated only when the running executable belongs to an isolated
`uv tool` or pipx environment. `mst update --to VERSION` also permits an explicit rollback and
verifies the installed version after the manager finishes. In a project or manually managed
virtual environment, MST refuses to rewrite its own dependencies and prints the corresponding
`uv lock`/`uv sync` command instead. `--prerelease` includes non-yanked prereleases when choosing
the newest version.

`delete` resolves the current record and still sends the physical `instance_id`, so a concurrent replacement is protected. `purge-thread` deliberately has a larger blast radius: it asks every responsive local host to purge the conversation and reports a partial result if any discovered host is unavailable. Destructive commands prompt on an interactive terminal and require `--yes` in scripts or JSON mode. `--timeout` may shorten an operation, but the application host's configured disposal timeout remains the upper bound.

Successful commands exit zero. Endpoint or incomplete-operation failures use `1`, invalid or missing confirmation uses `2`, an absent physical instance uses `3`, and an operator declining confirmation uses `4`. An interrupted watch uses `130`.

## Host a real Hyperlight backend

The MAF application remains the sandbox authority. Merely importing or constructing `SandboxControlServer` opens nothing. A host configuration that defaults off must opt in before the application starts the server on its event loop, passing the same backend and router that serve CodeAct calls.

```python
from maf_sandbox import Cleanup, SandboxRouter
from maf_sandbox_hyperlight import HyperlightSandboxBackend
from maf_sandbox_tui import HyperlightControl, SandboxControlServer

backend = HyperlightSandboxBackend()
router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
control = HyperlightControl(backend, router, source_id="research-agent")

if settings.enable_local_sandbox_control:
    # Entering the context is the action that opens the loopback listener.
    async with SandboxControlServer(control, source_id="research-agent"):
        await run_application(router)
else:
    await run_application(router)
```

When enabled, the server binds an ephemeral port on the literal loopback address `127.0.0.1` and publishes the address in a per-user discovery file. `mst` discovers responsive local endpoints automatically. There are deliberately no keys in this local prototype. Loopback is machine-local, not user-private: any local process that can reach the listener can use it while the host has it enabled. Do not proxy, forward or expose the listener outside the host; remote control requires a separately designed authenticated transport.

## Control protocol

Version one exposes `GET /v1/health`, `GET /v1/sandboxes`, `GET /v1/sandboxes/{instance_id}`, `DELETE /v1/sandboxes/{instance_id}`, and `DELETE /v1/scopes/{scope}/threads/{thread_id}`. Exact delete calls `SandboxRouter.dispose_kind` and verifies that the physical instance disappeared. Conversation purge calls `SandboxRouter.dispose_scope` under a shared timeout and aggregates outcomes across responsive hosts. A reset or replacement rotates the identifier, so a stale screen cannot remove the newer sandbox at the same logical MAF key.

Live inventory comes from the backend registry rather than OpenTelemetry. `maf-sandbox-otel` remains the complementary history and audit surface.
