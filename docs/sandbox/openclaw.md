# OpenClaw workload service

The OpenClaw integration exposes fixed workload operations through MCP. OpenClaw owns agent turns and tool authorization; the service owns input snapshots, sandbox policy, admission and cleanup. The [Bicep stdio prototype](../../samples/experimental/openclaw_bicep/README.md) is the implemented baseline. The shared HTTP lifecycle below is the selected design; its implementation and host qualification are tracked separately in the final table.

## Process and transport

One independently supervised Python process serves stateful Streamable HTTP at a fixed operator-selected `http://127.0.0.1:<port>/mcp` endpoint. It uses SDK-bundled `mcp.server.fastmcp.FastMCP` with `mcp==1.26.0`, JSON responses to POST requests, and separate MCP sessions for each connected client. It has no event store or replay of completed results. SDK-managed GET streams remain supported for client compatibility. Stateless HTTP is excluded: a cancellation notification must reach the same MCP session that owns its request.

The operator starts the service separately from OpenClaw. OpenClaw's `mcp.servers.bicep` definition uses `transport: "streamable-http"`, the fixed URL and an operator-provisioned authorization header; it does not launch the Python worker. MAF uses `MCPStreamableHTTPTool` against the same endpoint. The supported client baseline for qualification is OpenClaw 2026.9.7 with its TypeScript SDK 1.30.0 and MAF core 1.19.0 with Python MCP 1.26.0. Other versions require their own evidence. Stdio remains the script's default; HTTP is explicit opt-in. The two modes are mutually exclusive for the same owner directory.

The ownership lock, Docker backend, validator and global admission slot belong to the outer ASGI application lifetime. They are constructed once, before transport sessions, and are released after all work settles. FastMCP's per-session server lifetime cannot own these shared resources. There is exactly one ASGI worker, no automatic code reload and no independently initialized worker pool. Startup persists the retained owner state and sweeps only that deployment's scope before accepting connections. A second service process using the same state directory refuses startup.

## Authority and admission

All clients represent one trusted local operator with uniform policy. Authentication uses a random bearer credential with at least 256 bits of entropy, held in operator-controlled storage outside agent workspaces and supplied through trusted client configuration. The service reads its credential and immutable image/configuration policy at startup. It authenticates every MCP HTTP method before session allocation, parsing or dispatch; missing or invalid credentials receive 401. Rotation drains and restarts the service and reconnects clients. Logs exclude credentials, source contents and raw session IDs.

The listener binds only IPv4 loopback. An exact Host allowlist includes the selected address and port; all requests carrying an Origin header are rejected with 403 for this non-browser deployment. Wildcard origins, redirects, reverse proxies, external listeners and remote multi-tenancy are outside this design. Loopback authentication does not establish isolation from a compromised process running as the same OS user. These rules implement a local deployment contract, not an OAuth authorization server.

MCP session IDs and JSON-RPC request IDs route messages; they do not authorize filesystem access, select an owner or identify an OpenClaw agent. The adapter associates each active call with the authenticated transport session and request ID, independent of tool arguments. Duplicate live request IDs within one session are rejected before SDK dispatch. Equal IDs in different sessions remain independent. A leaked session ID cannot replace the bearer credential, and clients sharing that credential are not mutually distrustful tenants.

One global validator admits one tool call at a time. Contention returns a bounded MCP tool error with no verdict; it creates no queue or sandbox. The active slot remains held through cancellation and disposal. Each admitted call keeps its own in-memory file store and the prototype's immutable source/result contract. Image selection, backend, closed guest networking, configuration and resource limits remain operator-owned. Session closure never sweeps the shared Docker scope; only the global owner does, after active work settles.

## Bounded transport sessions

The session adapter owns a registry capped at eight records, including initializing and closing sessions. Allocation is atomic and occurs only for a valid authenticated initialize request without a session ID. Unknown IDs receive 404; malformed initialization and ordinary requests without an initialized session allocate nothing. A full registry returns 503 without creating another SDK transport. The service removes a retired record only after its transport and any associated call have settled. Idle sessions expire after 15 minutes without an authenticated client request; an open GET stream alone does not refresh this timer. Active work is never evicted to make room.

The pinned SDK retains explicitly terminated transport records, so HTTP mode needs an encapsulated session-manager integration that bounds both the adapter registry and SDK state. A cap on a second dictionary alone is insufficient. Any required private SDK access is isolated, documented against 1.26.0 and covered by churn/retirement tests. This is an implementation requirement, not behavior supplied by FastMCP defaults.

Each POST is limited to 2 MiB before JSON parsing, counting streamed/chunked bytes as well as Content-Length; exceeding it returns 413. Request-body assembly has a 10-second deadline. The HTTP server permits at most 32 accepted connections and 16 KiB of request headers per request; the adapter allows at most 16 simultaneous POST body readers and one GET stream per session. Cancellation and DELETE must remain usable while compilation is busy. Admission checks occur before expensive schema processing, and every rejected request releases its transport budget. Slow readers, excessive headers and connection churn are explicit acceptance cases; this local service does not claim resistance to denial of service by its OS owner.

## Cancellation, shutdown and recovery

An explicit MCP cancellation applies only to its transport session's matching request. It requests cancellation of that call and retains the existing repeated-cancellation drain. A protocol acknowledgment does not release admission or prove compiler termination. A bare HTTP disconnect does not cancel work: the caller's result may be lost, while the service completes or reaches its existing 120-second cancellation deadline and then drains cleanup. Gateway turn cancellation must be qualified through the client's actual MCP notification path. A service-side deadline is a fallback, not a hard maximum container lifetime.

DELETE marks that MCP session closing, rejects new work from it and requests cancellation of its associated call before transport retirement. The response acknowledges session retirement, not completed sandbox cleanup. Closing an idle session leaves another session's active call untouched. Losing a GET stream has no process-lifetime effect. An idle-session expiry uses the same retirement path. Control requests and cleanup run without holding a registry lock across waits, so a closing session cannot prevent another client's cancellation or admission checks.

Graceful service shutdown stops admission, retires transport sessions, drains the active call, verifies the final owned-scope sweep, then releases the process lock. Cleanup uncertainty poisons global admission and requires recovery; a clean-looking MCP connection cannot override it. If draining hangs, the service retains ownership and reports that it is not ready. A supervisor must not start a replacement while the lock is held. Forced process death can leave a container, which the next owner reconciles after acquiring the retained lock. No independent watchdog or hard recovery deadline is claimed.

Gateway restart/reload affects its client sessions, not the service process. Service restart invalidates all old MCP session IDs, which receive 404; clients establish new sessions. A client does not silently replay a validation request whose outcome became unknown. A deliberate retry is a new validation of a new submitted snapshot, even if its bytes match. The service stores no cross-call source or result history. Policy changes use a drained service restart. Retained ownership cannot be moved to another Docker endpoint without operator reconciliation. Windows retains the prototype's process-restart recovery limit; host-crash durability remains separately unqualified.

## Acceptance before enabling multiple Gateway sessions

| Case | Required observation |
|---|---|
| Two real Gateway sessions | Both discover only the authorized validator and independently produce valid, invalid and incomplete results with matching input/config/image identities and one projected content item. Use published Python dependencies and the pinned OpenClaw installation. |
| Shared admission | Hold A inside compilation; B receives busy with no extra container. After A's confirmed cleanup, B succeeds. No fairness or parallel compilation is promised. |
| Cancellation isolation | Send equal JSON-RPC IDs from two transport sessions. A cannot cancel B's matching ID. Cancel A repeatedly during active compilation and disposal; busy remains until A settles, then B succeeds. |
| Session closure | Closing idle A preserves active B. Closing active A retires only A, keeps the global slot until cleanup, and leaves B connected. Exercise both MAF close and OpenClaw disposal. |
| Disconnect semantics | Drop only the HTTP response stream and observe continued bounded supervision. Separately abort a Gateway turn, capture the MCP cancellation notification and verify exact-container cleanup; do not infer notification delivery from a disconnected socket. |
| Restart and reload | Reload one client's configuration, restart Gateway, and gracefully restart the service. Verify invalidated IDs/reconnection, no implicit request replay, preserved unrelated owners and successful later calls. |
| Authentication and framing | Reject missing/wrong credentials on POST, GET and DELETE; bad Host/Origin; malformed bodies; oversized Content-Length and chunked bodies; duplicate live IDs and ordinary uninitialized requests. No rejection allocates a sandbox or leaks submitted bytes. |
| Resource bounds | Fill all eight records, including initialization/retirement races; ninth initialization fails without allocation. Cycle at least 100 sessions, verify SDK and adapter records return to zero after retirement, exercise idle expiry, slow bodies and GET/connection limits. |
| Failure and recovery | Inject cleanup failure and repeated cancellation; all clients observe refusal until recovery. Kill the service with an active container, then restart with the same owner and verify startup cleanup. This selected crash case does not complete the whole interruption matrix. |
| MAF compatibility | Two real `MCPStreamableHTTPTool` instances discover and call with static authorization headers, preserve structured-result projection and close independently. Repeat with the exact published dependencies selected for the implementation. |

Acceptance uses a deterministic provider and host-observed results, transport messages and container identities. It makes no claim about LLM reasoning, prompt-injection resistance, information-flow enforcement in OpenClaw or unattended operation. The [research record](research/openclaw-integration.md#multi-session-lifecycle-decision) contains the option comparison and the smaller transport probe that informed this design.

## Status

| Decision | State | Tracking |
|---|---|---|
| First Bicep operation | Supervised stdio prototype merged; one-session Gateway evidence | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open); [#1651](https://github.com/sokolaidev/maf-extensions/pull/1651) (merged) |
| Shared service lifecycle | Selected design; HTTP mode is not implemented | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (open) |
| Shared HTTP implementation | Pending bounded transport, ownership and MAF tests | [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (open) |
| Two-session Gateway qualification | Pending implementation and the acceptance matrix above | [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (open) |
| Independent crash recovery and other host platforms | Deferred; no hard maximum lifetime or host-crash guarantee | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
