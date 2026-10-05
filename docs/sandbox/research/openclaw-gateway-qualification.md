# OpenClaw Gateway qualification of the Bicep prototype

> Supervised qualification: one-session stdio on 2026-10-03 and bounded two-session HTTP on 2026-10-05, both with a deterministic provider. The shared HTTP service and Gateway-abort cancellation passed the checks below; the complete lifecycle/recovery matrix and unattended operation remain unqualified. The sample README carries the current operator contract.

This records supervised execution on 2026-10-03 in Europe/Amsterdam (UTC+02:00) through the real OpenClaw Gateway and its embedded agent runtime. The combined-candidate acceptance report was written at `2026-10-03T00:40:22+02:00`, equivalent to `2026-10-02T22:40:22Z`. A deterministic local provider selected tools and captured the results sent back to the provider; it was not an LLM and establishes no evidence about model reasoning or prompt-injection resistance. The service used the published dependency pins in the [prototype](../../../samples/experimental/openclaw_bicep/README.md), launched with `uv run --script`. The broader [integration research](openclaw-integration.md) owns the design direction.

## Tested configuration

| Component | Observed version or setting |
|---|---|
| OpenClaw | npm `openclaw@2026.9.7`, CLI revision `c074824` |
| npm package integrity | `sha512-/8N2LnfTFQPvnZizi8qKSFfnLQaPvSG3Cb4xo1YV7b4JhYiUc43ZNRpXJ01bWghLK0Ezk3HVeo/DGHcIRQwRWA==` |
| Host | Windows, Node 26.7.0; Linux Docker guests |
| Bicep image | Local immutable ID `sha256:0bda3505a331713614b847025ce35199f3dda5a81ea2a661130525226dbc0265`; Bicep 0.46.1 (`545b338e2c`) |
| Compiler configuration | `images/bicep-sandbox/prepared.bicepconfig.json`, SHA-256 `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Gateway | Separate state, config, home and workspace; loopback port 19761; random bearer token; no channels or plugins; heartbeat disabled |
| Provider | Loopback port 19762, `openai-completions`, deterministic fixture with no external provider calls |
| Tool policy | `tools.allow = ["bicep__bicep_validate"]` |
| MCP | Configured server name `bicep`; stdio; connection timeout 60000 ms; request timeout 240000 ms |

The npm installation used `--ignore-scripts`, followed by OpenClaw's inspected package-local postinstall script; OpenClaw refuses to run before that lifecycle step completes. On the first Windows setup, state operations failed with `OpenClaw state maintenance does not own this database` and an offline-maintenance error. Startup succeeded after using a dedicated process `USERPROFILE` and precreating its `AppData/Local/OpenClaw/locks/openclaw-state-owners` directory and the SQLite parent directory. This is an observed setup workaround, not a proven root-cause diagnosis or a repair instruction for an existing installation. No OpenClaw package source was patched.

## Results

| Check | Observed result |
|---|---|
| CLI discovery | `mcp probe bicep --json` found one server tool, `bicep__bicep_validate`; the host additionally exposed generic resource/prompt helpers from FastMCP's advertised capabilities |
| Gateway routing | `/tools/invoke` returned 404 for the configured MCP tool in this setup; `/v1/chat/completions` reached it through the normal agent runtime |
| Model-facing discovery | OpenClaw presented `tool_search`, `tool_describe` and `tool_call`; the fixture selected the validator through `tool_call` with its exact name |
| Valid source | Completed, valid, status `ok`, confirmed cleanup |
| Type-invalid source | Completed, invalid, status `ok`, confirmed cleanup; compiler diagnostic `BCP033` |
| Uncached registry dependency | Incomplete, null verdict, status `incomplete`, confirmed cleanup; compiler diagnostic `BCP190` |
| Result identity | Source-set digest, exact compiler-config digest and immutable image identity matched the submitted inputs and host configuration |
| Authentication | Both HTTP endpoints returned 401 without the Gateway token |
| Tool policy | Forced `exec` and `bicep__resources_list` requests through `tool_call` were rejected as unknown tool IDs under the explicit allowlist |
| Client disconnect | The checker observed an active Bicep process, closed the HTTP connection, then observed removal of that exact container; another owner's container survived |
| Post-disconnect admission | A subsequent call in the same session returned a completed valid result with confirmed cleanup |
| Resource configuration | Inspection confirmed network mode `none`, 1 GiB memory, one CPU, 128 PIDs and all capabilities dropped |

The reusable checker measured 2.872 seconds from client disconnection to observed exact-container removal; an earlier manual run measured 4.959 seconds. These are polling observations, not a latency guarantee or proof of immediate guest termination. Gateway diagnostics recorded `HTTP client disconnected` and stopped model fallback because the caller signal was aborted. No packet capture verified the intermediate MCP cancellation notification. The service deliberately drains work before disposing resources; a removed container proves its guest processes no longer exist, not the time when the compiler first stopped. The complete acquisition/staging/disposal interruption matrix remains untested.

The first Gateway results contained the diagnostic payload twice in `result.content`: OpenClaw projected `structuredContent` and retained the prototype's compact JSON text mirror. OpenClaw 2026.9.7 removes that mirror only when it matches its two-space JSON representation. The prototype now emits that representation, including literal Unicode. The actual Gateway was rerun after reloading its MCP runtime: all three outcomes produced one projected content item, with the same structured result. A transport regression also covers Unicode. OpenClaw's `details.structuredContent` still carries structured metadata; this is not a claim that the overall model-facing envelope contains the data only once.

The fixture's final response is always `QUALIFICATION_TOOL_RESULT_RECEIVED`; that text alone proves nothing about validation. The checker examines the preceding tool result, expected completion/verdict/status, content count, digests and cleanup. Compiler results arrived inside OpenClaw's external-untrusted-content wrapper. That observation does not establish information-flow enforcement or resistance to malicious diagnostic instructions.

## Session ownership finding

A second Gateway session with the same stdio definition attempted to launch another service against the retained owner directory. The first session still owned its cached MCP runtime, so the second service could not acquire the lock; OpenClaw reported connection closure, no callable tool under the allowlist and HTTP 500. Reusing the original session worked. This is an integration limitation even for a single trusted operator: operator identity does not imply one server process.

This tested stdio configuration therefore supports one active OpenClaw session per retained owner directory. Do not weaken the lock, delete it or generate untracked owner directories to make another session start. Those changes would break cleanup ownership. [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) compares a separately supervised loopback Streamable HTTP service with one admission/recovery owner, a host-controlled per-session stdio launcher with durable recovery accounting, and a supported host extension lifecycle. This historical stdio run selected no transport migration; the subsequent shared HTTP design and implementation are tracked below.

## Reproduce

Use a fresh, trusted task directory, the prepared image, the current prototype checkout and the pinned OpenClaw installation. Keep the npm cache and temporary files on a volume with sufficient space. Install locally with `npm install --prefix "$TASK_ROOT" --ignore-scripts --no-audit --no-fund --save-exact openclaw@2026.9.7`, then run `node "$TASK_ROOT/node_modules/openclaw/scripts/postinstall-bundled-plugins.mjs"`. Confirm `node "$TASK_ROOT/node_modules/openclaw/openclaw.mjs" --version`. These commands do not install a Gateway service.

Set `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH` and `OPENCLAW_HOME` to separate paths beneath the task directory. On Windows, set `USERPROFILE` for the Gateway process to its temporary home and precreate the lock directory described above. Preserve the original `USERPROFILE` explicitly in the MCP server's `env` so uv and Docker use the intended runtime configuration. Do not change the machine-wide environment. Set `OPENCLAW_SKIP_CHANNELS=1` and task-local `TEMP`/`TMP`. The MCP launch needs an explicit uv cache location if its parent uses a different home.

Create this dedicated Gateway config, replacing every capitalized placeholder with a trusted local value. All executable and file paths should be absolute. Generate a fresh random bearer token and keep the config outside the agent workspace. The sample command launches published Python dependencies, not workspace editables.

```json
{
  "gateway": {
    "mode": "local",
    "bind": "loopback",
    "port": 19761,
    "auth": {"mode": "token", "token": "RANDOM_LOCAL_TOKEN"},
    "controlUi": {"enabled": false},
    "http": {"endpoints": {"chatCompletions": {"enabled": true}}}
  },
  "agents": {"defaults": {
    "workspace": "ISOLATED_WORKSPACE",
    "skipBootstrap": true,
    "heartbeat": {"every": "0m"},
    "model": {"primary": "qualification/fixture"}
  }},
  "tools": {"allow": ["bicep__bicep_validate"]},
  "plugins": {"enabled": false},
  "discovery": {"mdns": {"mode": "off"}},
  "models": {"mode": "replace", "providers": {"qualification": {
    "baseUrl": "http://127.0.0.1:19762/v1",
    "apiKey": "qualification-local",
    "api": "openai-completions",
    "models": [{
      "id": "fixture", "name": "Deterministic qualification fixture",
      "reasoning": false, "input": ["text"],
      "contextWindow": 128000, "maxTokens": 2048,
      "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    }]
  }}},
  "mcp": {"servers": {"bicep": {
    "command": "ABSOLUTE_UV_EXECUTABLE",
    "args": ["run", "--script", "ABSOLUTE_SERVER_SCRIPT",
      "--image", "IMMUTABLE_IMAGE_ID",
      "--config", "ABSOLUTE_BICEP_CONFIG",
      "--state-dir", "RETAINED_OWNER_DIRECTORY"],
    "cwd": "ABSOLUTE_REPOSITORY_ROOT",
    "transport": "stdio", "enabled": true,
    "connectionTimeoutMs": 60000, "requestTimeoutMs": 240000,
    "env": {"USERPROFILE": "ORIGINAL_WINDOWS_PROFILE", "UV_CACHE_DIR": "TASK_UV_CACHE"}
  }}}
}
```

Run `config validate` and `mcp probe bicep --json` through the local `openclaw.mjs` command before starting the Gateway. The probe initializes the retained owner directory and exits; it must not compete with an already-running service. Start the [fixture provider](../../../tests/fixtures/openclaw_gateway_provider.py) in a separate terminal:

```bash
uv run python tests/fixtures/openclaw_gateway_provider.py --evidence "$TASK_ROOT/evidence.jsonl"
```

Start `node "$TASK_ROOT/node_modules/openclaw/openclaw.mjs" gateway run` with the isolated environment and wait for its ready message. Then run the [acceptance checker](../../../tests/fixtures/openclaw_gateway_check.py) from the repository:

```bash
uv run python tests/fixtures/openclaw_gateway_check.py \
  --config "$OPENCLAW_CONFIG_PATH" \
  --owner "$BICEP_OWNER_STATE_DIR" \
  --bicep-config images/bicep-sandbox/prepared.bicepconfig.json \
  --image "$BICEP_IMAGE_ID" \
  --evidence "$TASK_ROOT/evidence.jsonl" \
  --report "$TASK_ROOT/acceptance.json" \
  --session qualification
```

Use that same `--session` when repeating the check against a live Gateway. The checker refuses stale success evidence by removing its old report before work, then writes a new report only after every assertion passes. It creates and removes a separately owned temporary Docker container for the disconnect-preservation check. A failure can leave the service's owned resources for its normal recovery path; inspect the dedicated scope and logs rather than running an unscoped cleanup. Stop the Gateway and fixture after checking, and verify that the service's owner scope has no remaining containers.

The evidence file retains tool results and advertised tool names, not the host system prompt, runtime machine identity or bearer token. Keep raw Gateway logs local; they can contain host paths and source snippets. The fixture is a manual test utility, accepts only loopback connections and is not a deployable provider. The local Python unit gate does not run these Gateway acceptance checks automatically.

Official references: [MCP configuration](https://docs.openclaw.ai/tools/mcp), [Gateway chat API](https://docs.openclaw.ai/gateway/openai-http-api), [HTTP tool API](https://docs.openclaw.ai/gateway/tools-invoke-http-api), and [local provider configuration](https://docs.openclaw.ai/gateway/local-models). These are live documentation; the results above apply to the exact installed release.

## HTTP qualification preparation (2026-10-04)

The shared service is merged in [#1680](https://github.com/sokolaidev/maf-extensions/pull/1680). The [HTTP checker](../../../tests/fixtures/openclaw_gateway_http_check.py) prepares the next bounded part of [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676): two distinct Gateway sessions with valid/invalid/incomplete results, tool-policy denials, one shared admission slot, and a Gateway turn abort whose MCP cancellation is observed at the service. At this preparation checkpoint, the checker had not passed against the real Docker-backed Gateway setup. On that date a bounded read-only Docker health probe timed out. An operator-authorized Desktop restart remained blocked by local VMM socket connection errors; live execution and prior test-resource cleanup were unconfirmed. The pinned OpenClaw CLI accepted the isolated HTTP configuration, which proves configuration syntax only. The earlier successful SDK/MAF Docker evidence applies to its recorded candidate; the 2026-10-05 execution below supplies the first successful two-session HTTP Gateway report.

Each provider request carries a fresh qualification turn ID, and each result must match exactly one record for that turn and scenario. Both Gateway sessions must map to distinct observed MCP sessions. The [test-only ASGI observer](../../../tests/fixtures/openclaw_http_observer.py) records allowlisted methods, typed request-ID digests, session-ID digests, response status and service counts. It wraps the unchanged Bicep startup composition and HTTP server, forwards messages without rewriting them, and retains no authorization headers, source arguments or response bodies. It adds instrumentation overhead and is not a deployable service feature. Provider result evidence still contains compiler diagnostics; keep it outside the repository.

Use the pinned installation and isolated Gateway/provider setup above, with fresh state for this HTTP run. Start the observer with published dependencies in place of the ordinary service command; select a separate random service credential stored outside the agent workspace:

```bash
uv run --script tests/fixtures/openclaw_http_observer.py \
  --prototype samples/experimental/openclaw_bicep/server.py \
  --image "$BICEP_IMAGE_ID" \
  --config images/bicep-sandbox/prepared.bicepconfig.json \
  --state-dir "$BICEP_OWNER_STATE_DIR" \
  --token-file "$BICEP_TOKEN_FILE" \
  --port 19763 --evidence "$TASK_ROOT/transport.jsonl"
```

This observer pins MAF core 1.19.0 as well as the prototype's published core, Bicep, Docker, MCP and Uvicorn versions. Replace only the dedicated Gateway's `mcp.servers.bicep` entry with a `streamable-http` definition pointing to `http://127.0.0.1:19763/mcp`, using its trusted `headers.Authorization` setting for the service bearer credential and the existing timeouts. Keep Gateway authentication and service authentication separate. Wait for authenticated service readiness, validate the Gateway configuration, start the provider and Gateway, then run:

```bash
uv run python tests/fixtures/openclaw_gateway_http_check.py \
  --config "$OPENCLAW_CONFIG_PATH" --owner "$BICEP_OWNER_STATE_DIR" \
  --bicep-config images/bicep-sandbox/prepared.bicepconfig.json \
  --image "$BICEP_IMAGE_ID" --openclaw "$TASK_ROOT/node_modules/openclaw" \
  --evidence "$TASK_ROOT/evidence.jsonl" \
  --transport-evidence "$TASK_ROOT/transport.jsonl" \
  --report "$TASK_ROOT/http-acceptance.json"
```

The checker creates two fresh Gateway session names per run. Use a fresh dedicated Gateway process when repeating a failed run so cached client runtimes do not accumulate against the eight-session limit. During contention it requires a compiler process still active before aborting and rejects a busy response that fabricated a workload result. Before accepting exact-container removal, it matches the later cancellation notification to the active request and session and requires HTTP 202 from that same HTTP exchange. Natural compiler completion or the provider's fixed final message cannot alone satisfy that check. It creates one separately owned sentinel container and removes that exact resource in its finalizer; failed active work remains under the service's retained recovery owner. Stop the dedicated Gateway, provider and service after checking, and verify the final owner scope is empty and the observer recorded clean shutdown. Do not restart a shared Docker daemon as an implicit fixture cleanup step.

A successful report records candidate/source hashes and dependency versions and deliberately sets `complete_matrix: false`. It covers only the cases above. Gateway runtime disposal, restart/reload without implicit replay, selected service crash and cleanup-failure recovery, and the full transport/MAF matrix on that candidate still need their own execution and evidence. Do not close #1676 based on this report. The offline [evidence tests](../../../tests/test_openclaw_gateway_qualification.py) exercise correlation, identity refusal, redaction, typed cancellation matching and removal of stale success reports; they do not establish live Gateway or Docker acceptance.

## Two-session HTTP execution (2026-10-05)

The merged checker from [#1696](https://github.com/sokolaidev/maf-extensions/pull/1696) passed against the real OpenClaw Gateway on clean candidate `176b98c438ade6534be501722f2648524d970828`. The fixture provider was deterministic and local. This establishes the bounded host/tool path below, not model reasoning, prompt-injection resistance or the complete [acceptance matrix](../openclaw.md#acceptance-before-enabling-multiple-gateway-sessions). The report retains `complete_matrix: false`, and [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open.

Docker was healthy for this run: Docker Desktop 4.94.0, Engine 29.8.2, Linux amd64 guests, on the Windows host with Node 26.7.0. The earlier image was absent, so the base and prepared profile were rebuilt from this checkout. The preparation verified the committed module digests and passed its network-disabled compile checks. No old image identity or earlier live result was reused as evidence for the new image.

| Component | Qualified value |
|---|---|
| OpenClaw | `2026.9.7` |
| Published core / Bicep / Docker packages | `0.46.0` / `0.22.0` / `0.24.4` |
| MCP / MAF core / Uvicorn | `1.28.1` / `1.19.0` / `0.54.0` |
| Immutable image ID | `sha256:5611ccff6015b5dd25d512c1f2dc6ab3cdfaecf9274a523c48cc07116fb38c89` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Prepared module manifest SHA-256 | `3bb31d522dc9f9626091534bbb6fe2620da993c9c5b22d327ffbea02b3f63ece` |
| Successful local report SHA-256 | `9627ca6cadd6657b0fa0a96f4488aa6cca03647435ae76dfc75ebf91c363d9a8` |

The MCP 1.28.1 baseline is deliberate: it is the pin on the tested candidate. The earlier MCP 1.26.0 evidence above remains historical. The observer checked the actual installed published versions and service source hashes before the checker accepted any results.

| Check | Observed result |
|---|---|
| Session separation | Two fresh Gateway session names mapped to two distinct MCP sessions on one service |
| Outcomes in both sessions | Valid and invalid completed with status `ok`; uncached dependency was incomplete with null verdict; all three outcomes confirmed cleanup |
| Result identity and projection | Expected source/config/image identities and exactly one projected content item in both sessions |
| Gateway authentication and tool policy | Missing Gateway authentication refused; forced `exec` and resource-helper requests refused in both sessions |
| Shared admission | A was still compiling when B received a busy tool error; B allocated no additional container and fabricated no workload result |
| Gateway turn abort | A's HTTP socket was closed while compilation was observed; a later `notifications/cancelled` matched A's typed request digest and MCP session digest, with HTTP 202 from that same observed exchange |
| Cleanup and isolation | A's exact container disappeared; a separately owned sentinel survived; the checker removed its sentinel in its finalizer |
| Continued use | B subsequently completed a valid call with confirmed cleanup |
| Final service state | After interrupting the dedicated processes, all three listeners were closed; the observer recorded `sessions=0`, `active=false`, `poisoned=false`; the retained owner had no containers |

The successful checker report was produced before stopping the processes. The final service state was inspected separately afterward. Closed listeners do not establish graceful Gateway runtime disposal: that lifecycle case still needs its own controlled check. The four specifically recorded resources from the earlier Docker-blocked run were also confirmed absent; this was a targeted inspection, without an unscoped cleanup.

On the same candidate and rebuilt image, the focused suites ran with `MAF_OPENCLAW_BICEP_IMAGE` set: **158 passed, no skips**. This includes all four opt-in Docker cases: HTTP MAF outcomes/cancellation/owned recovery, the published-dependency two-client HTTP smoke, stdio compiler outcomes/cleanup, and active-compiler cancellation/owned recovery. The published smoke reported the same six package versions as the Gateway service above. The offline transport cases in those files cover additional isolation and malformed-input behavior; they do not substitute for every real Gateway lifecycle case.

The checker recorded these source identities. Its report, observer evidence, provider results and process logs remain outside the repository; raw logs can contain host paths and diagnostic source content. The report digest above identifies the retained successful report without publishing those logs.

| Source | SHA-256 |
|---|---|
| `server.py` | `95879a31bd6d4dccefa7fa620e86d24547c22568e30327760a3841419f00a591` |
| `workload_http.py` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` |
| `workload_service.py` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` |
| `openclaw_gateway_http_check.py` | `35fc7ab63443d0695ef9799f63abe8d951c7a474f4778706b96a497916f320d8` |
| `openclaw_gateway_provider.py` | `f9bf7f8446e0cf620fb371d51cdacfbe2661b3cd0ba31af90436568b453f5403` |
| `openclaw_http_observer.py` | `9fabe5196de734dc225f494327d3b0eeb8fa13df4f053797d522f5a858a1daca` |

Reproduce with the HTTP procedure above and a freshly built prepared image, recording its immutable identity. For the accompanying transport checks, set `MAF_OPENCLAW_BICEP_IMAGE` to that identity and run `uv run pytest -q tests/test_mcp_workload_service.py tests/test_openclaw_bicep_prototype.py tests/test_openclaw_gateway_qualification.py`. Keep published-service and workspace-test dependency evidence distinct.

The next bounded deliverable is Gateway restart/configuration reload and service restart with the retained owner: verify reconnection and refusal to replay unknown outcomes. Active/idle Gateway runtime disposal, selected service crashes and cleanup-failure recovery, and the remaining real-host transport/MAF combinations are still required for #1676. No independent watchdog, unattended recovery guarantee, actual-model qualification or Linux/macOS host result is established here.

## Status

| Work | State | Tracking |
|---|---|---|
| Bicep host qualification | One-session stdio and bounded two-session HTTP Gateway paths qualified with a deterministic provider; actual LLM behavior and full crash recovery remain unqualified | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| Multiple Gateway sessions | Two-session HTTP outcomes, shared admission and accepted Gateway-abort cancellation qualified on the recorded candidate; remaining lifecycle/recovery matrix open | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (closed) by [#1677](https://github.com/sokolaidev/maf-extensions/pull/1677) (merged); [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (closed) by [#1680](https://github.com/sokolaidev/maf-extensions/pull/1680) (merged); [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (open) |
