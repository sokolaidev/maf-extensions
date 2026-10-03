# OpenClaw Bicep stdio prototype

This supervised experiment exposes one `bicep_validate` MCP tool through the existing Bicep workload, router and Docker backend. It accepts inline source bytes and returns diagnostics, completion and a verdict for that exact source set. It does not deploy resources, read an agent workspace, return artifacts or accept shell commands. The [research record](../../../docs/sandbox/research/openclaw-integration.md) owns the broader integration design.

The server uses `mcp.server.fastmcp.FastMCP` bundled with the pinned `mcp==1.26.0`, compatible with MAF's SDK 1.x integration. FastMCP generates input/output schemas from typed Pydantic models. A small subclass checks raw inputs before FastMCP coercion, bounds error responses, and supplies the bounded stdio reader through the SDK's underlying server. That private transport access must be requalified when changing the SDK pin. This sample does not install the separate `fastmcp` distribution, whose current 4.x release requires SDK 2.

## Run

Use Python 3.12 or newer, uv, a reachable Linux Docker daemon and the [prepared Bicep image](../../../images/bicep-sandbox/README.md#prepared-avm-profile). The image must be selected by its full local `sha256:` ID or a repository digest; mutable tags are rejected. Build or pull the intended image before starting the service. Keep its filesystem free of credentials: Bicep can read files through compile-time functions even though the submitted filenames are confined.

From the repository root, resolve the image ID with `docker image inspect --format '{{.Id}}' <prepared-image-tag>`, then run:

```bash
uv run --script samples/experimental/openclaw_bicep/server.py --image "$BICEP_IMAGE_ID" --config images/bicep-sandbox/prepared.bicepconfig.json --state-dir "$BICEP_OWNER_STATE_DIR"
```

`BICEP_OWNER_STATE_DIR` names a dedicated, trusted local directory for this deployment. Keep it outside agent-writable workspaces, retain it across restarts, and use the same Docker endpoint when restarting. Do not copy its owner file to another deployment or delete it to work around a startup refusal. Missing or corrupt existing ownership state requires operator reconciliation. The process lock excludes another service using the same state directory on this machine; it is not a distributed lease or a same-user security boundary.

Before admitting work, the service syncs the owner file and, on POSIX, its directory and every ancestor so newly created state directories survive a crash on filesystems honoring `fsync`. Sync failures refuse startup; restarting retries persistence with the same owner. Windows syncs the file but has no portable directory-sync operation here, so its recovery contract covers process restarts, not host crashes or power loss. Host-crash recovery still needs platform-specific live qualification.

The PEP 723 block pins the three suite distributions because the prototype projects the existing framework's fixed leading result fields. `uv run --script` resolves installed distributions rather than workspace editables. For checkout development, use `uv run python samples/experimental/openclaw_bicep/server.py` with the same arguments. Stdout is reserved for MCP; operational diagnostics go to stderr. There is no independently published adapter wheel yet.

## Connect from OpenClaw

Configure a local stdio server under `mcp.servers` using an absolute uv command, an explicit repository working directory, and the arguments above. Set `requestTimeoutMs` above the service's 120-second deadline plus cancellation settlement and cleanup; 240000 ms is a starting prototype setting, not a guaranteed upper bound. Verify discovery with `openclaw mcp doctor <server-name> --probe`, then exercise a real tool call. The [OpenClaw MCP guide](https://docs.openclaw.ai/tools/mcp) documents `command`, `args`, `cwd`, timeouts and tool policy. This service requires uniform authorization for one trusted local operator; it receives no trusted per-agent or per-session identity. MCP authorization does not inherit host-exec command or file approvals.

Use one active OpenClaw session per retained owner directory. OpenClaw 2026.9.7 retains a separate stdio runtime per session; a second session using the same directory cannot acquire the service's ownership lock. Keep that lock intact. The [Gateway qualification record](../../../docs/sandbox/research/openclaw-gateway-qualification.md) contains the tested configuration. The [shared HTTP service design](../../../docs/sandbox/openclaw.md) selects the next lifecycle; [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) and [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) track implementation and Gateway acceptance. This script still serves only stdio.

The server defines exactly one tool operation. FastMCP also advertises empty resource/prompt capabilities, for which OpenClaw generates helper tools. With the configured server name `bicep`, use `tools.allow: ["bicep__bicep_validate"]` to expose only validation. In the qualified release, a normal Gateway agent turn calls it through Tool Search's `tool_call`; `/tools/invoke` returned 404 for this MCP tool in the tested setup. A request looks like:

```json
{
  "files": [
    {"path": "main.bicep", "content": "output greeting string = 'hello'\n"}
  ]
}
```

Every supplied `.bicep` or `.bicepparam` is compiled and linted. There is no entry-point selector and no auxiliary JSON/text file support. Related files must all appear in the same request. The prepared image carries selected pinned AVM modules; it is not an offline copy of the complete registry. An uncached registry module yields `completed=false` and `verdict=null` under `Egress.CLOSED` and `--no-restore`. A missing local source file is a compiler error and yields an invalid verdict for the submitted source set.

## Bounds and lifecycle

| Surface | Prototype setting |
|---|---|
| Input | 1–8 files; 64 KiB UTF-8 per file; 256 KiB total; 200-character names |
| Names | Relative ASCII slash-separated components; no traversal, device names, duplicate case-folded names or file/directory overlap |
| Transport | 2 MiB per incoming newline-delimited frame, enforced before JSON parsing; oversized frames end the connection |
| Output | 16 KiB UTF-8 diagnostics; overflow suppresses completion and verdict rather than presenting a truncated success |
| Isolation | Explicit container floor, call scope, new in-memory file store for each call, no host workspace mount |
| Docker | Closed network, 1 GiB memory, 1 CPU, 128 PIDs, all Linux capabilities dropped |
| Time | 15 seconds per compiler phase; 10 seconds per lifecycle command; 30 seconds for a cold pull; 120 seconds before request cancellation is requested |
| Cleanup | Work settles before the scope sweep; sweep timeout 30 seconds; failures suppress the verdict and refuse future calls |
| Concurrency | One active request; overlapping requests receive a bounded busy error instead of accumulating in a queue |
| Recovery | Exclusive local ownership lock, durable random scope and startup sweep of that scope and fixed thread only |

Cancellation acknowledgment, compiler termination and cleanup are different events. The MCP SDK can acknowledge cancellation while the compiler is still draining. The service retains the active slot and ownership lock until the workload and cleanup settle, including across repeated direct task cancellation. Shutdown drains its final cleanup before releasing ownership. Its deadlines trigger cancellation, not a hard process kill; acquisition and compiler cancellation can require additional settlement time. If a dependency hangs beyond its configured timeout, this prototype retains supervision rather than admitting another call. Do not use it unattended: there is no independent watchdog enforcing a maximum container lifetime while the service is dead.

Source content stays in memory on the host and in the call's container. The owner directory stores no source files. The response includes `source_sha256` over UTF-8 compact JSON of sorted `[path, content]` pairs, `config_sha256` over the exact UTF-8 compiler configuration, and the selected immutable image identity. These identify the submitted snapshot and policy, not the current state of any repository. Diagnostics remain untrusted text. Serialized metadata does not create OpenClaw information-flow enforcement.

The prototype reads only the framework's fixed first completion item and, for completed results, its second verdict item. It rejects unexpected shapes and never searches compiler diagnostics for those fields. This deliberately narrow compatibility projection needs requalification when the pinned packages change; it is not a new public framework-neutral result API.

## Verification

Run `uv run pytest tests/test_openclaw_bicep_prototype.py -q` for input, authority, framing, ownership and cancellation regression tests. FastMCP transport tests verify generated schemas, rejection before coercion, bounded errors and structured error results. Set `MAF_OPENCLAW_BICEP_IMAGE` to the full prepared image ID or digest to opt into real Docker/MCP stdio tests. The live compiler cases use MAF's `MCPStdioTool` to check discovery and structured-result projection; the SDK client exercises cancellation while the compiler is active, scope recovery and preservation of another owner's container. These cases also inspect resource configuration and prepared dependency resolution.

The [Gateway acceptance fixture and checker](../../../docs/sandbox/research/openclaw-gateway-qualification.md#reproduce) separately exercised OpenClaw 2026.9.7 using published Python dependencies and a deterministic local provider. They verified valid/invalid/incomplete outcomes, result identities, one projected content item, unauthenticated refusal, tool allowlist denials, active-compiler client disconnection, exact-container removal and a subsequent successful call in the same session. These are real host-path tests without LLM reasoning. The JSON text mirror uses the SDK-style formatting OpenClaw recognizes for deduplication; the structured result remains authoritative.

The implementation is still an experimental source sample. Only compiler-phase cancellation is live-qualified here; interruption during acquisition/staging, daemon disconnects, worker/Gateway termination at each lifecycle boundary, Docker endpoint changes, an independent crash watchdog, multi-session ownership, and Linux/macOS host coverage remain separate acceptance work.

## Status

| Work | State | Tracking |
|---|---|---|
| Bounded Bicep integration | Supervised prototype; one-session Gateway path qualified with a deterministic provider, complete crash qualification remains | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| OpenClaw session ownership | Shared HTTP lifecycle selected; this prototype remains limited to one session | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (open); [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (open); [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (open) |
