# maf-sandbox-tui

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-tui)](https://pypi.org/project/maf-sandbox-tui/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-tui/LICENSE)

> **Experimental.** This package warns on import with `MafSandboxTuiExperimentalWarning`. Its prototype protocol, discovery files and UI may change without notice.

MST is a keyboard-first operator console for sandboxes owned by opted-in MAF applications. It lists physical instances, shows their trusted MAF key and lifecycle signal, and asks the owning router to dispose one exact generation. It does not scan for arbitrary Hyperlight VMs and never kills a worker process directly.

## Try the complete local flow

The demo starts an authenticated loopback control endpoint, connects the TUI to it, and supplies three representative Hyperlight records. Press `r` to refresh, select a row with the arrow keys, press `d` to review an exact-instance disposal, and `q` to quit.

```powershell
uv run mst --demo
```

For a non-interactive protocol check:

```powershell
uv run mst --demo --json
```

## Host a real Hyperlight backend

The MAF application remains the sandbox authority. It opts in by hosting `SandboxControlServer` on its event loop and passing the same backend and router that serve CodeAct calls.

```python
from maf_sandbox import Cleanup, SandboxRouter
from maf_sandbox_hyperlight import HyperlightSandboxBackend
from maf_sandbox_tui import HyperlightControl, SandboxControlServer

backend = HyperlightSandboxBackend()
router = SandboxRouter([backend], min_cleanup=Cleanup.RESET)
control = HyperlightControl(backend, router, source_id="research-agent")

async with SandboxControlServer(control, source_id="research-agent"):
    await run_application(router)
```

The server binds an ephemeral loopback port and publishes a bearer token in a per-user discovery file. `mst` discovers responsive local endpoints automatically. The prototype applies mode `0600` to the file, but Windows does not implement POSIX modes as an access-control boundary; run it only where the user profile and `%LOCALAPPDATA%` are protected from other users. A production Windows transport needs an explicit user ACL or an authenticated named pipe.

## Control protocol

Version one exposes `GET /v1/health`, `GET /v1/sandboxes`, `GET /v1/sandboxes/{instance_id}`, and `DELETE /v1/sandboxes/{instance_id}`. All routes require the discovery file's bearer token. Delete selects the physical `instance_id`, calls `SandboxRouter.dispose_kind`, and verifies that the instance disappeared. A reset or replacement rotates the identifier, so a stale screen cannot remove the newer sandbox at the same logical MAF key.

Live inventory comes from the backend registry rather than OpenTelemetry. `maf-sandbox-otel` remains the complementary history and audit surface.
