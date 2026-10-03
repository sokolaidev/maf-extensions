# Shared MCP workload prototype: Bicep

This supervised experiment exposes one `bicep_validate` MCP tool over default stdio or opt-in authenticated loopback HTTP through the existing Bicep workload, router and Docker backend. It accepts inline source bytes and returns diagnostics, completion and a verdict for that exact source set. It does not deploy resources, read an agent workspace, return artifacts or accept shell commands. The [research record](../../../docs/sandbox/research/openclaw-integration.md) owns the broader integration design.

The server uses `mcp.server.fastmcp.FastMCP` bundled with the pinned `mcp==1.26.0`, compatible with MAF's SDK 1.x integration. FastMCP generates input/output schemas from typed Pydantic models. The stdio subclass checks raw inputs before FastMCP coercion and bounds framing/errors. HTTP uses the reusable registration and supervision in [workload_service.py](workload_service.py) and the bounded session owner in [workload_http.py](workload_http.py). Both preserve the same Bicep schema and structured-result projection. The HTTP adapter drives FastMCP's underlying server with SDK transports instead of constructing the SDK's retaining session manager. Its registry is the actual transport registry, capped at eight initializing/live/closing records. This private FastMCP access and the Uvicorn protocol subclass must be requalified when changing their pins. This sample does not install the separate `fastmcp` distribution, whose current 4.x release requires SDK 2.

## Run

Use Python 3.12 or newer, uv, a reachable Linux Docker daemon and the [prepared Bicep image](../../../images/bicep-sandbox/README.md#prepared-avm-profile). The image must be selected by its full local `sha256:` ID or a repository digest; mutable tags are rejected. Build or pull the intended image before starting the service. Keep its filesystem free of credentials: Bicep can read files through compile-time functions even though the submitted filenames are confined.

From the repository root, resolve the image ID with `docker image inspect --format '{{.Id}}' <prepared-image-tag>`, then run:

```bash
uv run --script samples/experimental/openclaw_bicep/server.py --image "$BICEP_IMAGE_ID" --config images/bicep-sandbox/prepared.bicepconfig.json --state-dir "$BICEP_OWNER_STATE_DIR"
```

`BICEP_OWNER_STATE_DIR` names a dedicated, trusted local directory for this deployment. Keep it outside agent-writable workspaces, retain it across restarts, and use the same Docker endpoint when restarting. Do not copy its owner file to another deployment or delete it to work around a startup refusal. Missing or corrupt existing ownership state requires operator reconciliation. The process lock excludes another service using the same state directory on this machine; it is not a distributed lease or a same-user security boundary.

Before admitting work, the service syncs the owner file and, on POSIX, its directory and every ancestor so newly created state directories survive a crash on filesystems honoring `fsync`. Sync failures refuse startup; restarting retries persistence with the same owner. Windows syncs the file but has no portable directory-sync operation here, so its recovery contract covers process restarts, not host crashes or power loss. Host-crash recovery still needs platform-specific live qualification.

The PEP 723 block pins the three suite distributions because the prototype projects the existing framework's fixed leading result fields. `uv run --script` resolves installed distributions rather than workspace editables. For checkout development, use `uv run python samples/experimental/openclaw_bicep/server.py` with the same arguments. Stdout is reserved for MCP; operational diagnostics go to stderr. There is no independently published adapter wheel yet.

## Shared HTTP mode

Generate an operator-owned random credential outside agent workspaces. The file contains 64 lowercase hex characters (256 random bits), optionally followed by a newline. For example, run `python -c "import secrets; print(secrets.token_hex(32))"` and save the output in a file whose OS permissions restrict access to the operator. Do not place the credential itself on the service command line or in model-visible configuration.

```bash
uv run --script samples/experimental/openclaw_bicep/server.py --transport http --port 8765 --token-file "$BICEP_TOKEN_FILE" --image "$BICEP_IMAGE_ID" --config images/bicep-sandbox/prepared.bicepconfig.json --state-dir "$BICEP_OWNER_STATE_DIR"
```

The service binds `127.0.0.1` only. Clients connect to `http://127.0.0.1:8765/mcp` with `Authorization: Bearer <file contents>`; MAF supplies it through `MCPStreamableHTTPTool(static_headers=...)`. Every method requires authentication, the exact Host/address/port, and no Origin header. `GET /ready` with the same authorization returns 200 after resource recovery, or 503 during shutdown or cleanup uncertainty. Keep one process/worker with no reload or reverse proxy. Stdio and HTTP cannot simultaneously own the same directory.

The listener admits at most 32 connections and bounds headers to 16 KiB, incomplete headers/body assembly to 10 seconds, POST bodies to 2 MiB, body readers to 16 and GET streams to one per session. Sessions expire after 15 minutes without a client request, excluding active work. There is one global active tool call across all registered bindings, with bounded busy errors and no queue. Bare HTTP disconnects leave work supervised; explicit MCP cancellation and session DELETE retire only that session's work. The 120-second Bicep request deadline requests cancellation and permits additional settlement/cleanup time.

Stop with the process's normal interrupt/termination handling and wait for shutdown before starting a replacement. Shutdown closes admission, retires transport sessions and drains cleanup before releasing the owner lock. If work hangs, ownership remains held. For credential or policy rotation, drain and stop the service, replace the trusted files, restart with the same owner directory and Docker endpoint, and reconnect clients. A restarted service rejects old MCP session IDs; clients must not automatically replay requests with unknown outcomes. There is no independent watchdog or host-crash guarantee.

## Adding a workload binding

Trusted startup code supplies `Resource` records with startup, cleanup, close and capability callbacks, and `Binding` records with an explicit MCP tool/schema, execution callback, resource names, required capabilities, byte limits and request deadline. `WorkloadService` handles shared admission, host-minted call context, cancellation and cleanup; `WorkloadHTTP` handles transport sessions and ingress. Neither module imports a concrete kind or backend. A new binding calls existing kind factories through the router and defines its own bounded input/result projection. Registration is explicit and fixed for the application lifetime; duplicate names and missing capabilities fail before readiness.

The Bicep composition in `http_application` supplies one Docker resource and retains the existing owner file format and cleanup scope. Two synthetic bindings test cross-tool admission and different schemas/results. Additional real kinds, artifact storage and persistent conversation state require their own host policies and qualification; a callable alone does not supply them. These modules remain experimental source, with no separate adapter distribution.

## Connect from OpenClaw

Configure a local stdio server under `mcp.servers` using an absolute uv command, an explicit repository working directory, and the arguments above. Set `requestTimeoutMs` above the service's 120-second deadline plus cancellation settlement and cleanup; 240000 ms is a starting prototype setting, not a guaranteed upper bound. Verify discovery with `openclaw mcp doctor <server-name> --probe`, then exercise a real tool call. The [OpenClaw MCP guide](https://docs.openclaw.ai/tools/mcp) documents `command`, `args`, `cwd`, timeouts and tool policy. This service requires uniform authorization for one trusted local operator; it receives no trusted per-agent or per-session identity. MCP authorization does not inherit host-exec command or file approvals.

In stdio mode, use one active OpenClaw session per retained owner directory. OpenClaw 2026.9.7 retains a separate stdio runtime per session; a second session using the same directory cannot acquire the service's ownership lock. Keep that lock intact. The [Gateway qualification record](../../../docs/sandbox/research/openclaw-gateway-qualification.md) contains the tested configuration. The [shared service design](../../../docs/sandbox/openclaw.md) defines the opt-in HTTP lifecycle. Start the HTTP process independently of OpenClaw, then configure clients with its fixed Streamable HTTP endpoint and authorization header. Real two-session OpenClaw Gateway qualification remains [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676); the SDK/MAF checks below do not substitute for it.

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
| Stdio framing | 2 MiB per incoming newline-delimited frame, enforced before JSON parsing; oversized frames end the connection |
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

The implementation is still an experimental source sample. Both transports have local compiler-phase cancellation evidence; interruption during acquisition/staging, daemon disconnects, worker/Gateway termination at each lifecycle boundary, Docker endpoint changes, an independent crash watchdog, real multi-session Gateway behavior, and Linux/macOS host coverage remain separate acceptance work.

The reusable service tests run with `uv run pytest tests/test_mcp_workload_service.py -q`. They use real loopback HTTP and two MAF clients, distinct test binding schemas, shared admission, equal-ID cancellation isolation, retirement under cleanup, 100-session churn, idle expiry, socket disconnects, GET-stream shutdown, actual TCP connection/header/body limits, startup rollback and cleanup-failure refusal. These checks do not use Docker unless `MAF_OPENCLAW_BICEP_IMAGE` is set.

With the prepared image selected by immutable ID, the opt-in HTTP Docker test checks valid/invalid Bicep, uncached and prepared modules, unchanged schemas/digests, cancellation while a compiler process is observed, exact-container removal, recovery of retained ownership and preservation of an unrelated owner's container. A separate `uv run --script` smoke uses the sample's published pins and two MAF clients; the verified versions are core 0.46.0, Bicep 0.22.0, Docker 0.24.4, MCP 1.26.0, MAF core 1.19.0 and Uvicorn 0.54.0. This evidence was obtained locally on Windows with a Linux Docker daemon; it is not hosted or two-session Gateway acceptance.

## Status

| Work | State | Tracking |
|---|---|---|
| Bounded Bicep integration | Supervised prototype; one-session Gateway path qualified with a deterministic provider, complete crash qualification remains | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| OpenClaw session ownership | HTTP service implemented with SDK/MAF acceptance; real multi-session Gateway qualification remains open | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (closed) by [#1677](https://github.com/sokolaidev/maf-extensions/pull/1677) (merged); [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (closed) by [#1680](https://github.com/sokolaidev/maf-extensions/pull/1680) (merged); [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (open) |
