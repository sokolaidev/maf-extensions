# maf-sandbox-mcp

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-mcp)](https://pypi.org/project/maf-sandbox-mcp/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-mcp)](https://pypi.org/project/maf-sandbox-mcp/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** The API may change without notice. Importing the package emits `MafSandboxMcpExperimentalWarning`.

Experimental supervised MCP worker for closed-network Bicep validation. The installable distribution owns the reusable service (`workload_service`, `workload_http`) and the fixed Bicep/Docker composition (`server`). The reusable service modules import no concrete kind or backend. The composition uses SDK-bundled FastMCP (`mcp==1.30.0`), the existing MAF Bicep callable and Docker; it does not run a model or install the separate `fastmcp` distribution.

## Install and start

This package is not yet published. Build a wheel from the repository with `uv build --package maf-sandbox-mcp`, then install that wheel into a fresh Python 3.12–3.14 virtual environment. For example, from a directory outside the checkout:

```bash
uv venv worker-env --python 3.13
uv pip install --python worker-env/bin/python /absolute/path/maf_sandbox_mcp-0.0.0-py3-none-any.whl
worker-env/bin/maf-bicep-mcp --help
```

On Windows, use `worker-env/Scripts/python.exe` and `worker-env/Scripts/maf-bicep-mcp.exe`. The wheel includes every runtime module; neither the checkout, `PYTHONPATH` nor an editable install is required. Publication and its version assignment remain maintainer actions.

Provide a reachable Linux Docker daemon, an already prepared immutable Bicep image and a trusted prepared `bicepconfig.json`. Select the image by full `sha256:` ID or repository digest; mutable tags are rejected. The [prepared-image guide](https://github.com/sokolaidev/maf-extensions/blob/main/images/bicep-sandbox/README.md#prepared-avm-profile) describes the selected cache, which is not the whole AVM catalogue. Keep credentials out of the image because Bicep compile-time functions can read guest files.

Generate a random 256-bit credential using `secrets.token_hex(32)` and store its 64 lowercase hex characters in an operator-owned file, optionally followed by a newline. Restrict its OS permissions. Keep the credential, compiler configuration and owner directory outside agent-writable workspaces.

```bash
maf-bicep-mcp --transport http --port 8765 --token-file "$BICEP_TOKEN_FILE" --image "$BICEP_IMAGE_ID" --config "$BICEP_CONFIG" --state-dir "$BICEP_OWNER_STATE_DIR"
```

Stdio is also available with `--transport stdio` (the default). Stdout is reserved for MCP messages; diagnostics go to stderr. Stdio and HTTP cannot simultaneously own the same directory. The entry point accepts no plugin name, import path, shell command or arbitrary compiler flag.

## Ownership and operation

Use one service process, one worker, no reload and no reverse proxy. The service binds `127.0.0.1` only. Clients use `http://127.0.0.1:8765/mcp` with a Bearer authorization header; MAF supports `MCPStreamableHTTPTool(static_headers=...)`. Every method requires authentication, the exact Host/address/port and no Origin. Authenticated `GET /ready` returns 200 after owned-resource recovery and 503 while draining or when cleanup is uncertain.

Retain the dedicated owner directory across restarts and keep the same Docker endpoint. Do not copy its owner file to another deployment, delete it to bypass a refusal or manufacture replacement identity. Missing or corrupt retained ownership requires operator reconciliation. The local process lock excludes another process using this directory; it is neither a distributed lease nor a same-user security boundary. Windows qualification covers process restart, not power loss or host crashes.

The service permits one active tool call globally, with a bounded busy error and no queue. It caps initializing/live/closing MCP sessions at eight, expires idle sessions after 15 minutes, and excludes active work from idle expiry. The listener bounds connections (32), headers (16 KiB), incomplete headers/body assembly (10 seconds), POST bodies (2 MiB), body readers (16) and GET streams (one per session).

A bare disconnect leaves work supervised. Explicit MCP cancellation and session DELETE retire only that session's work. Admission and deployment ownership remain held until execution and cleanup settle, even after a deadline or repeated cancellation. The Bicep request deadline is 120 seconds; settlement can take longer. A MAF client task may remain pending until its own timeout even after service retirement; the host must supervise those tasks separately.

Stop through normal interrupt/termination handling and wait for shutdown before replacing the process. Shutdown closes admission, drains sessions and cleanup, then releases ownership. A hung operation retains ownership; there is no independent watchdog. To rotate credentials or policy, drain and stop, replace trusted files, restart with the same owner directory and Docker endpoint, then reconnect clients. Old session IDs are invalid after restart. Retry an unknown outcome only after deliberate reconciliation; never automatically replay it.

## Tool contract and policy

`bicep_validate(files=[{"path": "main.bicep", "content": "..."}])` validates every supplied source. It accepts one to eight inline `.bicep`/`.bicepparam` files, at most 64 KiB UTF-8 per file and 256 KiB total. Names are bounded portable relative paths; traversal, devices, case-insensitive overlap, NUL and malformed Unicode are refused before resource allocation. No host workspace, auxiliary JSON asset, deployment command or output artifact is exposed.

The response separates `completed` from `verdict` (`valid`, `invalid` or null), with `status`, diagnostics capped at 16 KiB, `source_sha256`, `config_sha256`, immutable `image` and `cleanup`. Invalid code is a completed compiler verdict. Unprepared modules, malformed reports and interruption have no verdict. Cleanup failure poisons the service and prevents uncertain reuse. Diagnostics are untrusted compiler text. Digests attribute inputs; they do not enforce information flow in OpenClaw. See the [full service contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/openclaw.md).

Trusted composition explicitly admits container isolation and call scope, `Egress.CLOSED`, one GiB memory, one CPU, 128 PIDs and all capabilities dropped. Compilation uses the prepared cache with `--no-restore`. Inline sources enter an in-memory store with no host file bridge; this does not certify filesystem confinement for a future mounted-workspace adapter.

## Compatibility and qualification

The recorded installed-worker qualification baseline is MAF core 1.20.0, sandbox 0.48.0, Bicep 0.22.2, Docker backend 0.27.0, MCP 1.28.1, Starlette 1.7.0 and Uvicorn 0.54.0. That record does not qualify MCP 1.30.0; rerun affected installed-worker and Docker/OpenClaw acceptance before relying on the new SDK in those environments. Capture the full resolved manifest for every qualification; declared ranges do not pin all transitives. The earlier source-only evidence used MAF 1.19.0 and different suite versions and is not evidence for this wheel. Python metadata permits 3.12–3.14; a recorded runtime execution establishes only its named interpreter/platform.

Private FastMCP session access and Uvicorn protocol hooks are deliberate pinned compatibility boundaries. MCP and Uvicorn version changes fail closed; changing them or the MAF projection, dependencies or image requires affected acceptance to be rerun. The [qualification record](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/research/openclaw-gateway-qualification.md) distinguishes installed artifact, deterministic Gateway fixture, real compiler and synthetic transport evidence. No actual-model, remote multi-tenant, all-platform, complete-crash or unattended-operation claim is made.

New workload compositions can explicitly register bounded `Binding` and `Resource` records with the reusable service. They must supply their own schema, policy, lifecycle and qualification; installing a kind does not discover or enable it automatically.
