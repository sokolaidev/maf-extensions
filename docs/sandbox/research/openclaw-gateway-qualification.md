Warning: truncated output (original token count: 74919)
Total output lines: 2238

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

The checker recorded these source identities from the exact bytes read during execution. These are local file hashes, not Git blob hashes; checkout line-ending conversion can change them without changing the Python source. The active-service crash table below distinguishes the executed bytes from the LF-normalized Git files explicitly. Its report, observer evidence, provider results and process logs remain outside the repository; raw logs can contain host paths and diagnostic source content. The report digest above identifies the retained successful report without publishing those logs.

| Source | Executed file bytes SHA-256 |
|---|---|
| `server.py` | `95879a31bd6d4dccefa7fa620e86d24547c22568e30327760a3841419f00a591` |
| `workload_http.py` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` |
| `workload_service.py` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` |
| `openclaw_gateway_http_check.py` | `35fc7ab63443d0695ef9799f63abe8d951c7a474f4778706b96a497916f320d8` |
| `openclaw_gateway_provider.py` | `f9bf7f8446e0cf620fb371d51cdacfbe2661b3cd0ba31af90436568b453f5403` |
| `openclaw_http_observer.py` | `9fabe5196de734dc225f494327d3b0eeb8fa13df4f053797d522f5a858a1daca` |

Reproduce with the HTTP procedure above and a freshly built prepared image, recording its immutable identity. For the accompanying transport checks, set `MAF_OPENCLAW_BICEP_IMAGE` to that identity and run `uv run pytest -q tests/test_mcp_workload_service.py tests/test_openclaw_bicep_prototype.py tests/test_openclaw_gateway_qualification.py`. Keep published-service and workspace-test dependency evidence distinct.

The following lifecycle execution completes the next bounded deliverable: Gateway restart/configuration reload and service restart with the retained owner, including reconnection and a controlled unknown outcome. Active/idle Gateway runtime disposal, selected service crashes and cleanup-failure recovery, and the remaining real-host transport/MAF combinations are still required for #1676. No independent watchdog, unattended recovery guarantee, actual-model qualification or Linux/macOS host result is established here.

## Reload, restart and withheld-result execution (2026-10-05)

The [lifecycle checker](../../../tests/fixtures/openclaw_gateway_lifecycle_check.py) first passed against the real Gateway and Docker on clean candidate `6b2842a24b9777499d64141046af3ce0e3aca6b0`. The checker at that stage repeated all cases successfully on clean candidate `94b3a63a21c06889118f378069a0ac117a48b81b`, supplying a mutable local image tag and resolving it to the immutable prepared image before any service launch. It first repeated the two-session HTTP baseline above, then exercised two additional logical Gateway sessions through each lifecycle transition. The host, OpenClaw, published Python dependencies, compiler configuration and immutable prepared image were the same qualified values listed above. The checker retained `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Image identity | Docker inspection resolved the supplied local tag to the immutable image ID listed above; the service, sentinels and result checks all used that ID |
| Unknown outcome | The observer withheld one completed JSON tool result after confirmed cleanup; Uvicorn returned HTTP 500, and Gateway projected a transport error without a structured workload result |
| No implicit replay | Exactly one dispatch produced the withheld result; subsequent explicit turns and all lifecycle transitions brought the total to exactly 18 expected dispatches, with no extra dispatch through final shutdown |
| MCP configuration reload | Increasing the configured request timeout triggered a hot reload in the same Gateway process; both lifecycle sessions received accepted HTTP DELETE responses, then used distinct fresh MCP sessions for successful valid calls |
| Gateway process restart | The harness killed its idle Gateway process, waited for exit, and launched a replacement with the same isolated configuration and state; both logical sessions used fresh MCP sessions and completed valid calls |
| Graceful service restart | A local stop-file request invoked normal Uvicorn shutdown; the observer recorded zero sessions, no active call and no poisoned admission before the process exited successfully |
| Retained ownership and stale IDs | The replacement service retained identical owner-file bytes and identical service sources/dependencies; an MCP session initialized before shutdown received HTTP 404 afterward |
| Reconnection after service restart | Both Gateway sessions established distinct fresh MCP sessions and completed valid calls with confirmed cleanup |
| Isolation and final shutdown | The unrelated owner's sentinel survived every transition; the harness removed that exact sentinel, drained the service, reaped its child processes and verified empty owned scope and closed fixture listeners |

The fault is an explicit qualification control on the observer, enabled only by `--drop-result-file`. The next `tools/call` consumes that local file, buffers its response, records only completion/cleanup and pseudonymous correlation fields plus a body hash, and withholds all result bytes. It raises after the SDK handler returns so the fault does not become an SDK-generated workload response. Subsequent responses remain transparent. This qualifies the observed HTTP 500 unknown-outcome path; it does not establish arbitrary network partitions, lost streamed responses, active-process crash behavior or a universal exactly-once guarantee. The deterministic provider ends the turn after the tool error; actual-model decisions to retry remain unqualified.

The Gateway restart is deliberately an idle process kill and relaunch. It proves reconnection after process replacement, but does not prove graceful Gateway shutdown or selective active/idle runtime retirement. The accepted DELETE observations belong to configuration reload. The service restart is graceful and uses the retained owner directory; it does not substitute for killing the service while a compiler container is active.

That candidate's successful local report SHA-256 is `da5de023447241cb1b8e3eabe80b2b75f0f58d9cfd779086745228075be3d0c3`. The image/count hardening report on `2759de3d1dbb665ccef08a78f97ef84402376da3` remains historical at `e7d6c2bd5ac9aed1e9531891c02619421eeaf7883c28d3d4d527e41013226c5d`, and the initial report at `0f70995823891d3440edd3fdd8231677f4b1c866fa4bea8c27172a9991438c07`. Its service and provider source identities match the preceding table. The revised checkers, observer and unchanged Docker helper are identified below. Raw provider output, process logs, credentials and owner/session identities remain outside the repository. A separate post-run process inspection found no remaining process associated with the isolated qualification directory.

| Source | Executed file bytes SHA-256 |
|---|---|
| `openclaw_gateway_lifecycle_check.py` | `cf993e0a563a2f61109bb41120b4a6fe56b9f00fb2411e0c0ceefd3cb4af1737` |
| `openclaw_http_observer.py` | `65315a5301cb1e384d8440970276f9c5ecf10111d27e8755d769f52c57fef527` |
| `openclaw_gateway_http_check.py` | `7cc202deee62a2ebe0fa6dc6acd8fa00d7d7fcd1f22fec6ba69403416f19a9d5` |
| `openclaw_gateway_check.py` | `4786505bb3d6a7df0c6c99d1be25d9ad1a78c92db46c5be5669f30d098d8dda1` |

To reproduce, use the dedicated HTTP Gateway configuration from the earlier procedure as a template, with all three configured fixture ports free. Do not start those processes separately: this checker supervises its own provider, Gateway and published-dependency service. `--root` must name a new private directory outside the repository. The checker and standalone observer resolve image references through Docker inspection and refuse a malformed immutable ID. Both entry points inspect even an immutable ID to verify local availability. The checker replaces credentials and workspace/state paths, disables CLI respawning so the owned Gateway process can be killed precisely, and refuses a pre-existing root or occupied fixture port. A failed run produces no success report in its fresh root.

```bash
uv run python tests/fixtures/openclaw_gateway_lifecycle_check.py \
  --root "$TASK_ROOT/lifecycle-fresh" \
  --config "$OPENCLAW_CONFIG_PATH" \
  --bicep-config images/bicep-sandbox/prepared.bicepconfig.json \
  --image "$BICEP_IMAGE_ID" \
  --openclaw "$TASK_ROOT/node_modules/openclaw"
```

The output is `lifecycle-report.json` under that new root, with candidate identity, dirty state, source hashes, baseline results and lifecycle results. Keep it together with private transport/provider evidence and process logs. The offline evidence suite at that candidate had 71 passing cases, including refusal of duplicated calls/results, another exchange/session/service process, stale or incomplete completion, cleanup failure and fabricated projections; it also verifies that withholding is one-shot and leaves the next response intact. Additional tests cover Docker image resolution and service-launch arguments, reject malformed image identities, and require dispatch counts independently of observed evidence. That baseline required nine dispatches and none for denied tools; that lifecycle checker required eighteen again after final shutdown. Removing tag resolution from the launch path made one test fail, bypassing image-ID validation made five fail, and removing the fixed-count comparison made five fail. Structural checks allow schema names in error text while rejecting workload-result fields; malformed withheld bodies are refused with a body hash and no body content. Docker inspection failures retain a bounded diagnostic. Restoring the substring check made two cases fail, removing the image diagnostic made one fail, and removing malformed-body handling made seven fail. Those tests validate the checker rather than replacing live evidence.

The next case at that stage was selected service-crash recovery while a compiler container is active, followed by cleanup-failure refusal/recovery and selective active/idle Gateway runtime retirement. Registry saturation/churn and the remaining real-host transport/MAF combinations still need their matrix-specific evidence. [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open; no independent watchdog, actual-model acceptance, host-crash durability or Linux/macOS qualification is established by this Windows execution.

## Active-service crash and retained-owner recovery (2026-10-06)

The extended [lifecycle checker](../../../tests/fixtures/openclaw_gateway_lifecycle_check.py) passed on clean candidate `5a1695aab53b3a01849d32d63338b06a600919f4` with the same pinned OpenClaw 2026.9.7, published Python dependencies, prepared image and compiler configuration recorded above. It repeated the baseline and all preceding lifecycle cases before exercising one additional service crash. The successful report SHA-256 is `addce2750af5b394e15efb9b0734ccfccc342630143bda564f6cfc1d25e35c35`; it retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Active work at process death | Docker reported a Bicep compiler in the retained owner's sole container; the observer then recorded the matching active session and exited its Python process through `os._exit(86)`, without running shutdown cleanup |
| Independent survivor check | After the service process exited with status 86, Docker still reported that exact container and its Bicep process; the checker then paused the orphan so natural completion could not masquerade as recovery |
| Unknown outcome and no replay | Gateway returned a transport error without a workload result; one dispatch belongs to the crashed turn, and the complete sequence had exactly 21 expected tool dispatches through final shutdown |
| Startup reconciliation | The orphan remained paused and in the same owner scope until replacement startup; the replacement retained identical owner-file bytes, sources and dependency versions |
| Cleanup before readiness | At the actual ASGI startup-complete boundary, the observer independently queried Docker and found an empty owner scope; the supervisor then verified exact-orphan absence and successful authenticated readiness |
| Reconnection | Both existing logical Gateway sessions established distinct fresh MCP sessions and completed new valid calls with confirmed cleanup |
| Isolation and shutdown | The unrelated owner's sentinel survived the crash and recovery; final graceful shutdown left zero sessions, no active call, no poisoned admission, no owned containers and no fixture listeners |

The measured interval from observing the surviving compiler to observing its absence after replacement startup was 5.221 seconds in this execution. It includes the deliberately paused interval and client error settlement, and is not a recovery deadline or performance guarantee. An independent post-execution process and Docker inspection found no remaining fixture process or owned container.

This is a selected forced-process-exit case. The observer's opt-in `--crash-file` control refuses to exit without unfinished active work; it is enabled by the lifecycle checker, not by the prototype. The orphan is paused only after service death and independent compiler-survival observation. This prevents a naturally completed compiler from satisfying the reconciliation check, but does not qualify every running-container failure. No production service or binding code changed. Host crashes, daemon loss, cleanup-failure refusal/recovery, an independent watchdog, selective active/idle Gateway runtime retirement, actual-model retries and the remaining real-host transport/MAF combinations remain unqualified. [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open.

Use the reproduction command above with a fresh `--root`; the extended checker includes this case and requires 21 dispatches. Keep `lifecycle-report.json`, `recovery.json`, transport/provider evidence and process logs together privately. The supervisor removes the exact deliberately orphaned container if qualification fails; a failed check writes no success report. Raw logs, owner/session/container identities and credentials remain outside the repository.

The offline evidence suite has 109 passing cases. New negative cases reject absent or repeated crash observations, wrong process/session correlation, completed or gracefully settled work, fabricated workload results, surviving resources, wrong recovery identity and readiness preceding cleanup. Startup observation is checked before its ASGI message is forwarded. These tests validate the checker and fault controls; the live execution establishes the bounded host behavior above.

| Changed source | Executed CRLF bytes SHA-256 | Git LF bytes SHA-256 |
|---|---|---|
| `openclaw_gateway_lifecycle_check.py` | `169ea6305f308ac5c44cce2eed52611809611bdd69796dbad13831bc4b981877` | `0a3704df986bc45032c4d46cc57c87b125f9de60c141612a1216b66649323c02` |
| `openclaw_http_observer.py` | `d20c3f6a20a38b2b0bb5af6ef24c3945860144192ea4f43f3f23f9c33707eb55` | `775460b4f73679ce86e64f587c21277eb0b28aad58cccb831ec1491a37c3c966` |

The executed service, provider, baseline checker and Docker helper retained the measured byte identities listed in the preceding evidence tables. For the two changed fixtures, the Git files at the executed candidate and the final PR revision are byte-identical to each other after LF normalization. Converting those LF files to CRLF exactly reproduces the measured execution hashes above. The retained report is unchanged; its `source_hashes` values describe executed bytes. A fresh LF checkout records the Git LF values instead. Compare like byte representations, or deliberately reproduce CRLF files when checking the historical execution digests; a clean Git status alone does not prove byte identity across checkout line endings.

## Status

The [2026-10-12 consolidated reconciliation](openclaw-qualification-closure-audit.md#consolidated-closure-reconciliation-2026-10-12) covers every original #1676 acceptance row using the separately recorded Gateway, Bicep/MAF and published transport evidence. Both finite follow-ups are merged. Qualification is complete for that supervised, selected baseline; the first-tool design disposition is recorded in the integration audit and #1898 delivers the separately recorded [installed worker](#installed-worker-qualification-2026-10-13). Historical reports and their `complete_matrix: false` values are unchanged.

| Work | State | Tracking |
|---|---|---|
| GitHub-hosted Linux Gateway-to-Docker sample | Qualified on the recorded Ubuntu candidate with deterministic provider and independent cleanup; broader Linux and real-model claims remain unqualified | [#2214](https://github.com/sokolaidev/maf-extensions/issues/2214) (closed) by [#2217](https://github.com/sokolaidev/maf-extensions/pull/2217) (merged) |
| GitHub-hosted real-model Bicep sample | Complete for the recorded Ubuntu/Azure OpenAI candidate with reasoning disabled: actual invalid/valid compiler results and independent cleanup passed | [#2288](https://github.com/sokolaidev/maf-extensions/issues/2288) (closed) by [#2294](https://github.com/sokolaidev/maf-extensions/pull/2294) (merged) |
| GitHub-hosted Linux actor-loss profile | Complete for the recorded Ubuntu v2 candidate: thirteen actor-loss cases and independent cleanup passed; broader failure modes remain unqualified | [#2386](https://github.com/sokolaidev/maf-extensions/issues/2386) (closed) by [#2387](https://github.com/sokolaidev/maf-extensions/pull/2387) (merged) |
| Current published Docker/Bicep candidate | Complete for the recorded worker 0.3.1 candidate: fresh baseline, all fifteen recovery cases and real-model Bicep sample passed | [#2542](https://github.com/sokolaidev/maf-extensions/issues/2542) (closed) by [#2545](https://github.com/sokolaidev/maf-extensions/pull/2545) (merged) |
| Numbered published Bicep sample | Complete: verified published worker quick start and routine deterministic hosted smoke; both compiler verdicts and independent cleanup passed | [#2657](https://github.com/sokolaidev/maf-extensions/issues/2657) (closed) by [#2659](https://github.com/sokolaidev/maf-extensions/pull/2659) (merged) |
| Linux Docker access and removal failure recovery | Complete for the reviewed v4 candidate: all fifteen cases passed with pre-probe startup observations and exact removal-command joins; broader failures remain unqualified | [#2413](https://github.com/sokolaidev/maf-extensions/issues/2413) (closed) by [#2414](https://github.com/sokolaidev/maf-extensions/pull/2414) (merged) |
| Linux Gateway-to-Docker sample | Recorded deterministic Linux-on-Docker-Desktop run passed; real-model behavior remains unqualified | [#2185](https://github.com/sokolaidev/maf-extensions/issues/2185) (closed) by [#2191](https://github.com/sokolaidev/maf-extensions/pull/2191) (merged) |
| Installed worker | Published 0.1.0; historical and fresh fixed-profile qualifications recorded separately below | [#1898](https://github.com/sokolaidev/maf-extensions/issues/1898) (closed) by [#1920](https://github.com/sokolaidev/maf-extensions/pull/1920) (merged) |
| Bicep host qualification | First-tool design and finite supervised qualification complete; installed worker delivered separately in #1898; actual LLM behavior and full crash recovery remain unqualified | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (closed) by [#1901](https://github.com/sokolaidev/maf-extensions/pull/1901) (merged) |
| Multiple Gateway sessions | Finite acceptance reconciled across recorded Gateway evidence, five Bicep/MAF cases and 43 published transport cases with 109 SDK retirements; qualification complete. Active MAF callers can remain pending until their request timeout after service retirement; broader stress, crash and platform scenarios remain unqualified | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (closed) by [#1677](https://github.com/sokolaidev/maf-extensions/pull/1677) (merged); [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (closed) by [#1680](https://github.com/sokolaidev/maf-extensions/pull/1680) (merged); [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (closed) by [#1882](https://github.com/sokolaidev/maf-extensions/pull/1882) (merged); [#1850](https://github.com/sokolaidev/maf-extensions/issues/1850) (closed) by [#1865](https://github.com/sokolaidev/maf-extensions/pull/1865) (merged); [#1851](https://github.com/sokolaidev/maf-extensions/issues/1851) (closed) by [#1870](https://github.com/sokolaidev/maf-extensions/pull/1870) (merged) |


## Startup cleanup refusal and explicit Gateway recovery (2026-10-06)

The extended lifecycle checker passed on clean candidate `ca1d6271e67272fdbf72790a9646a93b761f05e9`, using the same pinned OpenClaw 2026.9.7, published Python dependencies, immutable prepared image and compiler configuration above. The report SHA-256 is `4edfcad41e1b60d114c24ad8fc2643713ba06f8c74874f9a352cceb7cdc4725a`; the private `cleanup-refusal.json` evidence SHA-256 is `1de180a75e3854a3fe28c214937ba813732b0979f4dc7a317c2b1a19b75319df`. The report retains `complete_matrix: false` and repeats the baseline, reload/restart, withheld-result and active-service crash cases.

| Check | Observed result |
|---|---|
| Exact retained orphan | The compiler survived forced service exit and was then paused; its owner-file bytes and sole-container identity remained unchanged |
| Injected cleanup refusal | The observer returned false at the Bicep resource cleanup contract, once during replacement startup and once during startup rollback, only while Docker independently reported that exact orphan in the retained scope |
| Readiness and admission | The service reported failed ASGI startup with poisoned admission, no active call and zero sessions; pinned Uvicorn exited with status 3, and the supervisor observed a refused TCP connection rather than a ready listener |
| Gateway refusal | Fresh turns in both existing logical sessions reported the Bicep binding disconnected, with no workload result and zero additional service dispatches |
| Service recovery | Removing the fault and restarting with identical owner bytes, source identities and dependencies removed the paused orphan before ASGI startup completion and authenticated readiness |
| Gateway recovery | An explicit idle Gateway process restart rebuilt discovery; both logical sessions obtained distinct fresh MCP sessions and completed newly submitted valid calls |
| No replay and isolation | Exactly 21 service dispatches remained through final shutdown; the unrelated owner's sentinel survived, and independent post-execution inspection found zero owned containers and fixture processes |

The observer's opt-in `--refuse-cleanup-file` names the exact retained orphan. While present it reports unconfirmed cleanup without calling the underlying resource cleanup; a mismatched owner/container refuses the fault. Removing the file restores delegation to the real resource cleanup. This injects the service's cleanup-result boundary, not a Docker API, permission or daemon failure. No production service or binding code changed. The fault is persistent across both startup cleanup attempts and never edits the retained owner directory.

Service readiness alone did not restore the Gateway's tool catalog in a preceding clean attempt (`9cbcad99`). Both refused turns reported the disconnected binding; after service recovery, the next turn failed before tool execution because the explicit allowlist matched no registered callable tool. That attempt wrote no success report. The successful procedure therefore includes an explicit idle Gateway restart after service readiness, retaining the logical session names and submitting new calls. It does not qualify automatic discovery recovery, arbitrary retries or transparent replay. The prior active-crash evidence without intervening refused turns remains a separate, narrower observation.

Use the same reproduction command above with a fresh root. The checker creates and removes its fault file, retains `cleanup-refusal.json` alongside `recovery.json`, `lifecycle-report.json` and private transport/provider evidence, and rejects an unexpected successful startup, another orphan/process, missing refusal attempts, wrong exit status, an unrelated Gateway error or fabricated workload projection. The offline evidence suite has 150 passing cases. Removing the no-readiness/no-dispatch check made three negative cases fail. The complete local gate passed with 14,782 tests and 906 skips; Markdown examples and 226 live documentation trackers also passed. These checks validate the harness separately from the real Gateway/Docker execution.

The changed fixtures were executed with LF bytes, identical to the committed Git bytes in this candidate. Other source identities are unchanged from the preceding execution.

| Changed source | Executed and Git-LF SHA-256 |
|---|---|
| `openclaw_gateway_lifecycle_check.py` | `a2e3dcaee1d86da0d7a75b96eea5754cc35bc1499190a97a771ac195921529ad` |
| `openclaw_http_observer.py` | `01e87632aed5aa1e9dac1f71cb5870505f279f3e4b3639e2dbb66d16d6ba17fe` |

[#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open. Cleanup failure after a completed call, automatic Gateway discovery recovery after an unavailable service, selective active/idle runtime retirement, registry saturation/churn and the remaining transport/MAF combinations still need evidence. Host/daemon crashes, actual-model retry decisions, an independent watchdog and unattended operation remain unqualified.


## Completed-call cleanup confirmation: next bounded case (2026-10-07)

The next qualification slice targets the shared resource check after the real Bicep binding has returned a completed valid result with confirmed inner cleanup. The observer records that result's identities, then an opt-in `--refuse-completed-cleanup-file` makes the resource check return false. Docker scope inspection remains independent: this selected fault does not manufacture a retained container or simulate a Docker removal/daemon failure. The service must suppress success, report `cleanup_failed` with `cleanup: failed`, refuse readiness and refuse new work in both existing Gateway sessions, including after removal of the fault control. The expected recovery is an explicit service restart with the same owner followed by an idle Gateway process restart and two newly submitted valid calls. It does not claim automatic discovery recovery.

The checker requires one binding execution for the failed call, no binding executions for either poisoned-session probe and exactly 26 MCP tool dispatches across the full baseline and lifecycle sequence. The latter includes two error-only poisoned-session probes. It checks the recorded poisoned shutdown separately from clean shutdowns, preserves the unrelated owner, and retains `complete_matrix: false`. Candidate-specific live results will be recorded separately after execution; this paragraph specifies the case and is not live evidence.

A regression using the real HTTP composition confirmed that a resource-level refusal could set `status: cleanup_failed` while retaining `cleanup: confirmed` when the validator itself was not poisoned. The binding failure projection now treats `cleanup_failed` as failed cleanup independently of the validator's state. The regression covers the inner sweep, outer sweep and resource-only refusal, checking suppressed completion/verdict, error semantics, readiness and subsequent admission.


## Completed-call cleanup refusal and explicit recovery (2026-10-07)

The extended lifecycle checker passed on clean candidate `87f352ed885a89c0bfa02e88ada13931007bf18c`, repeating the two-session baseline and every preceding lifecycle case with the same pinned OpenClaw 2026.9.7, published Python dependencies, compiler configuration and prepared image recorded above. The successful report SHA-256 is `195e8cdddb3506cf73dc179f7d1b4c73b1a70be470abfe605507679300ff0ecb`; private `completed-cleanup.json` evidence SHA-256 is `7966223e99712bd51ddf459e51c1fc7a8608e1514724b19aadb34397821ff503`. The report retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Completed work before refusal | The unchanged binding execution returned a completed valid result with confirmed inner cleanup and matching source/configuration/image identities; the observer recorded it before refusing the shared resource check |
| Fault boundary | The observer returned false without calling the shared resource cleanup; an independent Docker query found the owned scope empty after the binding sweep. This is a cleanup confirmation failure, not an observed Docker removal failure |
| Returned result | Gateway projected one content item with error status and a structured `cleanup_failed` result: `completed: false`, null verdict and `cleanup: failed`, retaining the submitted source/configuration/image identities |
| Sticky admission | After the supervisor removed the fault file, both existing MCP sessions received the service-unavailable tool error without a structured workload result; neither executed the binding, and authenticated readiness returned HTTP 503 |
| Poisoned shutdown | Normal service shutdown drained both sessions and the active slot; the observer retained the poisoned state and exited with status 1 rather than reporting clean service health |
| Explicit recovery | Restart with identical owner-file bytes, sources and dependencies found an empty owned scope at ASGI startup completion and became ready; an explicit idle Gateway restart gave both logical sessions fresh MCP sessions and valid newly submitted results |
| No replay and isolation | Exactly 26 MCP tool dispatches through final shutdown, including the two refused probes; the unrelated sentinel survived. Independent post-execution inspection found zero owned containers and fixture processes |

The opt-in observer flag is `--refuse-completed-cleanup-file`. The fault requires a real completed valid binding result and the same call still holding service admission. It records only allowlisted result identities and pseudonymous transport correlation, returns the binding result unchanged, and refuses at the subsequent resource check. With the file absent, it delegates to the actual resource cleanup. The resulting service poison persists after file removal. The Bicep failure projection fix is in the executed `server.py`; the shared service and HTTP implementation are unchanged.

Reproduce using the lifecycle command above with a fresh private root. Retain `completed-cleanup.json` together with the existing report, startup-refusal/recovery evidence and provider/transport logs. The checker now requires 26 dispatches, one completed binding result for the fault, no binding execution for either poisoned probe, one poisoned shutdown between two clean shutdowns and a final empty owned scope. The 21-dispatch records above remain historical evidence for their named candidates. Private logs, credentials and owner/session/container identities are not committed.

The three changed Python sources were executed with LF bytes identical to the Git files at this candidate. Other source hashes remain those recorded for the preceding execution.

| Changed source | Executed and Git-LF SHA-256 |
|---|---|
| `server.py` | `6fd855751df41e3f841bda44a26a53841b0aa12c760398153a3ed6a903c281d5` |
| `openclaw_gateway_lifecycle_check.py` | `a7b97bc3fde11b6a18111920d1759d7f70453e660c285f11b21a4d3eade913b3` |
| `openclaw_http_observer.py` | `dd8f2009a921388cc2422d11b0a80b16dc10430c4e9aa0fbbf366598bd292461` |

This selected post-completion confirmation fault does not establish retained-container recovery after a Docker removal failure. Actual Docker cleanup/daemon failures, automatic Gateway discovery recovery, selective active/idle runtime retirement, registry saturation/churn and the remaining transport/MAF combinations still require evidence. Host crashes, actual-model retry decisions, an independent watchdog and unattended operation remain unqualified. [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) stays open.


Local verification passed the full gate (15,469 tests passed, 908 skipped), the focused qualification/HTTP/prototype suite (353 passed, four opt-in live cases skipped), and all four of those opt-in Docker cases in a separate execution. The live selection reported 22 passes because it also includes 18 offline setup/cleanup variants. The live cases cover HTTP MAF cancellation/owned recovery, the published-dependency HTTP smoke, stdio compiler outcomes and active-compiler cancellation/owned recovery. Workspace checks and the published-dependency service remain separate evidence classes. Final documentation checks passed 224 structure tests, Markdown examples, all 20 sample dependency-floor checks and 226 live tracker references. These are local results; hosted CI is recorded on the delivering PR.


## Selective Gateway session retirement: bounded design (2026-10-07)

The next case uses the pinned Gateway's public `sessions.delete` RPC, invoked by its own CLI against the isolated loopback configuration. The checker selects exact generated HTTP session keys from `sessions.list` and supplies `expectedSessionId`; it never deletes a default session or operates outside its private Gateway. In OpenClaw 2026.9.7, session deletion drains active work before `cleanupSessionBeforeMutation` requests MCP runtime retirement. This source path motivates the test; it is not runtime evidence.

After the existing 26-dispatch sequence, A begins a long compilation. Deleting idle B must produce an accepted, correlated MCP DELETE and remove only B's server registration while the exact compiler and A's active slot survive, with no cancellation notification. A fresh B session must receive busy without another container. Deleting active A must then produce matching accepted MCP cancellation and a targeted DELETE, remove the exact compiler and A's registration, and preserve B's replacement session for successful later work. A newly submitted call in A must establish a fresh MCP session. The selected sequence requires exactly 30 tool dispatches and no service or Gateway restart during these deletions. Other disposal triggers, registry saturation/churn, automatic discovery recovery and the remaining matrix stay open.


## Selective idle and active session deletion: live result (2026-10-07)

The complete lifecycle sequence passed on clean candidate `59bc3e5e50b4ae509a3e62649231520d3eee7500`, using the same pinned OpenClaw 2026.9.7, published Python dependencies, prepared compiler image and configuration recorded above. The report SHA-256 is `ccb2ca8e61ef23635a28194604bc9715d2eb25a559984f10ec7c571c38c3722f`. It records exactly 30 tool dispatches and retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Idle B deletion | The public `sessions.delete` RPC acknowledged the selected logical session. Its MCP DELETE returned HTTP 200, the registration finished retiring, and only A remained registered. A's exact compiler and active slot survived; no cancellation notification was sent |
| B reconnects while A runs | B established a fresh MCP session and received busy without creating another container or disturbing A's compiler |
| Active A deletion | Gateway delivered cancellation for A's exact MCP request/session and received HTTP 202, then sent a targeted DELETE. A's registration finished retiring, its exact compiler container disappeared, and B's replacement registration survived |
| Aborted HTTP turn | The pinned Gateway returned HTTP 500 with `api_error` / `internal error`; the provider received no projected workload result for that turn. The error alone is not cancellation evidence: the checker separately requires correlated accepted cancellation and exact-container removal |
| Subsequent work | B completed a valid call using the same replacement MCP session; a new call in A completed using a fresh MCP session |
| Ownership and shutdown | Both deletions used the same Gateway/service processes and unchanged owner-file bytes. The unrelated sentinel survived, exactly 30 dispatches remained through shutdown, and independent inspection found zero owned containers and fixture processes |

MCP DELETE acceptance and registry removal are separate events. The service retains the registration until active work, SDK processing and HTTP handlers drain. The observer attaches a completion callback to the existing retirement task and records registry/active-slot state after it finishes; it does not change retirement behavior or await cleanup in the HTTP response path. The checker bounds its wait for that event to 30 seconds and verifies the selected session, exchange, process boot, timestamps, completed retirement task and surviving registration. No production service or binding code changed.

Two preceding attempts produced no success report. Candidate `bbe0d8f3` incorrectly expected registration removal at HTTP response completion; the real response still carried the retiring record while asynchronous cleanup continued. Candidate `009d1551` passed both selective retirement checks but incorrectly expected HTTP 200 for the aborted chat turn. Inspection of the pinned Gateway's non-streaming error handler confirmed the generic HTTP 500 projection. Both attempts cleaned up completely. The successful candidate separates acceptance from retirement and pins that abort response while retaining the independent cancellation, resource and no-result checks.

Reproduce with the lifecycle command above and a fresh private root. Retain `retirement-idle.json` (SHA-256 `52feaf828fee67ccc80122ec965c9a8ea5056206426753eafe203abd4ee25b91`) and `retirement-active.json` (SHA-256 `5f7915869ac9d64105be30d0c3a66fd24844e2569af080c5a24af285e5d55bfb`) alongside the lifecycle report and existing private provider/transport evidence. Earlier 21- and 26-dispatch reports remain historical evidence for their named candidates. Credentials, host paths and raw session/container identities remain private.

The changed fixtures' executed LF bytes match Git at the successful candidate; other source identities remain unchanged from the completed-call cleanup execution above.

| Changed source | Executed and Git-LF SHA-256 |
|---|---|
| `openclaw_gateway_lifecycle_check.py` | `73694a733849230310b9239c1e820e5086957b4e349a1e4cbb2669e037509195` |
| `openclaw_http_observer.py` | `e0758505a8d6863fa77904d1a34d5d3b9ed6112b01345f421609733057b2604c` |

This qualifies the selected public session-deletion trigger only. Other Gateway retirement triggers, registry saturation/churn, actual Docker removal/daemon failures, automatic discovery recovery and the remaining real-host transport/MAF combinations stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). The provider remains deterministic; actual-model retries, host crashes, an independent watchdog and unattended operation remain unqualified. Separate live MAF/stdio cases were not rerun for this fixtures-and-evidence-only change.

The corrected evidence suite passed 304 tests; final documentation structure/path checks passed 381 tests. Markdown examples and 228 live documentation tracker references passed. These offline checks validate the checker and documentation separately from the real Gateway/Docker execution. Full local gate and hosted CI results are recorded on the delivering PR.

A subsequent observer-only correction decodes session headers with replacement, matching the wrapped HTTP adapter. Offline regressions compare the actual adapter and observed responses for DELETE, GET and POST with a non-ASCII session header: authenticated requests retain HTTP 404 and unauthenticated requests retain HTTP 401, with no registry allocation or retirement event. The focused suite now passes 310 tests. The full live lifecycle was not rerun for this malformed-header correction; the successful live evidence and hashes above remain tied to candidate `59bc3e5e50b4ae509a3e62649231520d3eee7500`.


## Registry saturation and bounded session churn: next case (2026-10-08)

The next bounded case fills the unchanged eight-slot HTTP registry through eight distinct real Gateway sessions. The independently runnable `--case registry` starts a fresh service and Gateway; the default combined lifecycle mode retains all preceding checks. Three idle delete/reconnect cycles reuse one slot while preserving the other seven sessions; those seven must subsequently execute valid calls with unchanged MCP identities. A ninth logical session then fails initialization with HTTP 503 at the MCP service, without a tool dispatch, binding execution or registry change. After freeing a slot, a separate probe records whether Gateway retries discovery. The independent case requires 18 tool dispatches and writes `registry-report.json`; combined lifecycle mode requires 46. Neither mode restarts the Gateway/service during the registry case.

The observer samples adapter registrations, pending request IDs/handlers and the pinned SDK's live server tasks and request-stream counts on authenticated readiness probes. Retirement evidence separately requires the selected SDK task to finish, its transport to terminate and its request streams and adapter request bookkeeping to reach zero. This uses MCP 1.28.1 internals for observation only and does not change admission or retirement behavior. Exact session sets, owner bytes, process identity, empty owned container scope and the unrelated sentinel are checked between steps. Candidate-specific live evidence is recorded below. Idle expiry, other retirement triggers, prolonged/concurrent churn, actual Docker removal failures and remaining transport/MAF combinations stay outside this selected case.

Two combined executions of candidate `5b1ee79f` stopped before reaching the registry case: one compiler returned an incomplete result before active deletion, and another execution observed an unexpected cancellation notification during idle deletion. Neither wrote a success report. The independent registry mode preserves the strict combined checks and qualifies only its own capacity/churn sequence; it does not turn either failed combined attempt into accepted lifecycle evidence.

The first independent registry execution (`6e6214d4`) filled all eight slots and proved MCP initialization refusal without work allocation. After an idle deletion drained SDK and adapter state to seven slots, the subsequent Gateway turn failed before provider execution or another MCP initialization, reporting that its explicit allowlist matched no callable tools. This establishes a post-refusal discovery limitation, not failed slot reclamation. The retained checker measures bounded churn before overflow and separately pins the free-slot catalog refusal; automatic recovery remains unqualified.


## Registry capacity, bounded churn and discovery refusal: live result (2026-10-08)

The independent `--case registry` execution passed on clean candidate `d595d660a65af51e0875e544aaf7f6d2bb7dd302`, retaining the pinned OpenClaw 2026.9.7, published Python dependencies, prepared compiler image and configuration recorded above. Its `registry-report.json` SHA-256 is `1c7d092e660709000901ed2a5fc46a183d7b65cea5631d94312f9a19ec4098cf`; `registry-churn.json` SHA-256 is `484c5c3c6534b8200472b9ced0a482cc53f5a47c90afc64dea2704e19c5f8180`. The report records 18 tool dispatches, `case: registry` and `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Capacity and idle state | Eight distinct Gateway sessions filled all eight service registrations. Readiness snapshots matched the exact session set and live SDK tasks, with no unfinished tool work or stale adapter request IDs |
| Three cycles before overflow | Each selected public `sessions.delete` retired one idle registration, finished its SDK task, terminated its transport and drained SDK streams and adapter bookkeeping. Each newly submitted call established a fresh MCP identity and returned a valid result; counts followed 8 to 7 to 8 |
| Unaffected sessions before overflow | All seven other sessions subsequently completed valid calls using their unchanged MCP identities |
| Ninth logical session | MCP initialization returned HTTP 503 with eight registrations retained and no binding execution or tool dispatch. Gateway projected HTTP 500 with `api_error` |
| Slot reclamation after refusal | Deleting one occupied logical session again drained SDK/adapter state and freed a slot; the service remained ready with seven registrations |
| Gateway discovery after refusal | A new call for the deleted logical session failed with HTTP 500 before provider execution or another MCP initialization. The scoped Gateway log reported no callable tools matching that session's explicit allowlist. The report records `reconnected: false`, `reason: no_callable_tools`, one free slot and zero MCP initialization attempts |
| Isolation and final shutdown | Gateway/service processes and owner-file bytes stayed unchanged throughout the case. The unrelated sentinel survived; shutdown was clean with exactly 18 dispatches. Independent inspection found zero owned containers and fixture processes |

Slot reclamation is therefore verified, while automatic Gateway discovery recovery after saturation is not. The successful calls in the churn and unaffected-session checks occurred before the capacity refusal; this execution does not establish that surviving sessions remain callable afterward. The refusal probe records the observed pinned-host behavior and is not a recovery implementation. Investigating discovery invalidation and retry after transient initialization failure is the next bounded priority in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676).

Reproduce using the lifecycle command above with `--case registry` and a fresh private root; retain both registry JSON records and the private provider, transport and Gateway logs. The two preceding combined attempts and the first independent attempt described above produced no success report; independent inspection found no owned containers or fixture processes after each. The final independent result does not requalify the combined 46-dispatch lifecycle sequence. Separate live MAF/stdio cases were not rerun. Idle expiry, other retirement triggers, prolonged/concurrent churn, actual Docker removal/daemon failures and the remaining transport/MAF matrix stay open. The provider remains deterministic; actual-model retries, host crashes, a watchdog and unattended operation remain unqualified.

All eight executed Python source hashes match Git bytes at the successful candidate. The changed sources are listed below; the other five retain the recorded baseline identities.

| Changed source | Executed and Git-LF SHA-256 |
|---|---|
| `openclaw_gateway_http_check.py` | `c2c46e21b62739cfb9a65b491491fb858c7245d92236700d8ffd5e839175ac67` |
| `openclaw_gateway_lifecycle_check.py` | `11f039c44df9d5fb8fe0c4a45844bf398854f313db5f4a17284fba4ab7defa4c` |
| `openclaw_http_observer.py` | `80246424d3450df7696245282a387ce0f767020511d475212f9ea5417e122349` |

The full local gate passed 16,915 tests with 909 skipped, plus lint, formatting, package/root type checks, documentation paths and implicit-concatenation checks. Its test collection preceded the final catalog-refusal refinement; the final focused evidence suite then passed 412 tests. These offline results are separate from the real Gateway/Docker run and hosted CI on the delivering PR. Production service and binding code are unchanged.

The final shutdown validator additionally requires the last service process to exit with status zero in both registry and combined modes; drained observer records alone cannot establish process success. Offline regressions reject failed, killed or unfinished process exits even with otherwise clean shutdown evidence. This assertion was added after the recorded live candidate and has not been rerun live; that historical report establishes the recorded drain/resource observations but did not independently assert the final service exit status.


## Discovery after capacity refusal: bounded follow-up (2026-10-08)

Source inspection of the pinned OpenClaw 2026.9.7 package identifies process-wide startup backoff keyed by workspace, agent directory, requester scope, server name and configuration fingerprint; the key does not include the logical session. A first failure sets a 30-second deadline for other runtimes sharing that key. The runtime owning that first failure may retry after five seconds. Each runtime also caches a failed catalog: once its deadline passes, `getCatalog` starts a background refresh and returns the stale catalog to the triggering caller. An explicit allowlist with no callable tool then fails before provider execution. This source reading motivates the next case; it is not evidence of runtime recovery.

The registry checker retains the immediate refusal probe, then waits 35 seconds with no new turns and requires no discovery or workload replay. A new turn must trigger a background MCP initialization and tool listing while still receiving the stale-catalog error before provider execution. After correlated successful discovery, a separately submitted turn must complete with the fresh MCP identity; all seven unaffected sessions must remain callable with their original identities. The Gateway and service must remain the same processes, ownership bytes and the unrelated sentinel must survive, and final service exit must be zero. This extends the independent case to 26 dispatches and the combined case to 54. It changes no production or installed Gateway code; candidate-specific live results are recorded below.


## Delayed discovery recovery without restart: live result (2026-10-08)

The independent `--case registry` sequence passed on clean candidate `b7cfa908b28e224e2393666b71477c10f44d6e09`, using the unchanged pinned OpenClaw 2026.9.7, published Python dependencies, prepared compiler image and configuration. The checker and evidence tests changed; the installed Gateway, prototype service and binding did not. `registry-report.json` SHA-256 is `949fe59a43cb49f4a544cb6b951b2cb8ec02b1831bc2c59d2401a56f9ddedabb`; `registry-churn.json` SHA-256 is `1b68454089e9ad7b4a83dc9b149f7ac1e523c588593d50bc2a0ab6e6f7d99e7c`. The report records 26 dispatches and retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Earlier bounded sequence | Eight occupied slots, three delete/reconnect cycles and seven unaffected-session calls passed; the ninth logical session received MCP initialization HTTP 503 without workload execution |
| Immediate free-slot probe | Deleting one occupied session freed a slot. A new turn in that logical session failed with the allowlist/catalog error before provider execution or another MCP initialization |
| Dormant cooldown | The checker waited 35 seconds without submitting a turn. No discovery attempt, tool call, binding execution or retirement occurred; seven service registrations remained |
| Refresh-triggering turn | A new turn still returned HTTP 500 with the catalog error and never reached the provider. It triggered exactly one successful MCP initialization and one successful tool listing on a fresh protocol session, restoring the registry to eight |
| Newly submitted work | A separate new turn completed with that fresh MCP identity. All seven other logical sessions then completed valid calls using their unchanged MCP identities |
| Replay and ownership | No refused turn reached the provider later, and exactly 26 tool dispatches remained through shutdown. Gateway/service processes and owner bytes were unchanged throughout; the unrelated sentinel survived |
| Final shutdown | The service exited with status zero and drained its SDK/adapter state. Listener shutdown passed; independent inspection found zero owned containers and fixture processes |

This narrows the earlier discovery conclusion: the immediate refusal is reproducible, but it does not establish a permanent catalog failure. The earlier candidate's free-slot snapshots occurred about 5.4 and 6.3 seconds after the capacity response, inside the cross-runtime 30-second backoff identified in the pinned source. In this execution, recovery required a new turn after the wait to trigger background discovery and a subsequent new turn to execute work. Waiting alone did not refresh the catalog. These were deliberately distinct submitted turns, not an automatic retry of a workload with an unknown outcome. The 35-second wait is a qualification margin around the source's first-failure cooldown, not a measured minimum, user-facing recovery SLA or generic retry policy.

The source explanation has two parts. `createServerMcpRuntime` builds the shared startup key without a logical session identity, so a capacity refusal can suppress initialization in a different runtime sharing that configuration. `connectWithMcpStartupBackoff` doubles the initial 15-second delay to 30 seconds; it grants the originating runtime a five-second first retry and increases later failure delays up to ten minutes. Separately, `getCatalog` returns the cached empty catalog while launching its due background refresh. The explicit allowlist guard then rejects a turn with no callable tools before provider execution. The live evidence pins this selected first-failure, different-runtime path; it does not measure the originating-runtime retry or later exponential delays.

All eight executed Python sources match Git bytes at the successful candidate. The lifecycle checker SHA-256 is `63278786aa39bce4d555e08335b1174ca66ec91778f61f6c3357829d39363826`; the other seven retain their preceding recorded identities. The report also records the exact installed Gateway files used for source analysis:

| Packaged source relative to OpenClaw `dist/` | SHA-256 |
|---|---|
| `agents/agent-bundle-mcp-runtime.js` | `7fd725036af0dbc99aa4d9791eb70b277770902b1d021098650adeb4275d5e92` |
| `agent-bundle-mcp-manager-api-CCU0OWCp.mjs` | `4ec2bab7d23d032a1eaac5d136c8b2b26ed1c5285793f7bad9b330601f8beac6` |
| `builtin-openclaw-dCw2mRD9.mjs` | `7c408f516754ffb156c23d27727a325fa56785c8d514276d79bd843053712a38` |

Reproduce with the existing lifecycle command plus `--case registry` and a fresh private root. The current independent case requires 26 dispatches; the default combined case now expects 54, but that combined sequence was not rerun. Historical reports retain their original counts and candidate boundaries, including the earlier final-exit assertion limitation; this live candidate executes the strengthened zero-exit check. Keep both registry JSON files and private provider/transport/Gateway logs. Credentials, host paths and raw identities remain outside the repository.

The focused evidence suite passed 464 tests, including rejection of wrong boots/exchanges/session identities, duplicate or missing discovery records, failed responses, invalid ordering, active/poisoned/nonempty state mismatches and any workload replay during refresh. Other startup-unavailability/restart recovery cases, repeated failure backoff, concurrent discovery, idle expiry, other retirement triggers, actual Docker failures and remaining transport/MAF combinations are not qualified by this result. Separate live MAF/stdio checks were not rerun. No model retry policy, watchdog, host-crash or unattended-operation claim is made, and [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open.


## Initially unavailable service: bounded follow-up (2026-10-08)

The independent `--case unavailable` starts the Gateway and deterministic provider while the configured MCP service has no process or listener. Two logical sessions must fail with the explicit allowlist/catalog error before reaching the provider; the owner file and transport evidence must still be absent. The checker then starts the service once, verifies the pinned environment and empty owner/registry state, and waits 35 seconds without submitting work. No discovery or replay is permitted during that interval.

Each failed logical session then receives a new discovery-triggering turn. The selected pinned behavior must return the stale-catalog error without provider execution while completing one correlated MCP initialization and tool listing. A separately submitted turn must succeed on that fresh identity, and both sessions must later succeed with their identities unchanged. The case requires exactly four tool dispatches, unchanged Gateway/service processes after service startup, unchanged owner bytes, preservation of the unrelated sentinel, drained state and final service exit zero. It writes `startup-report.json` and `startup-discovery.json`, retains `complete_matrix: false`, and does not rerun the longer registry or combined lifecycle sequences. Candidate-specific live evidence is recorded below.


## Initial service absence and two-session recovery: live result (2026-10-08)

The independent `--case unavailable` execution passed on clean candidate `db54ccf4722d7231eda9f8203748e96dac5ba71f`, retaining OpenClaw 2026.9.7, the published Python dependencies, prepared compiler image and configuration recorded above. The installed Gateway, prototype service and binding were unchanged. `startup-report.json` SHA-256 is `0031db2d1164be7531665c26ce47789c3e9dc7548ea24652f83e6159ae350b4a`; `startup-discovery.json` SHA-256 is `cbc9e9b9abdc1153c570bb195a528ed5d7650b0803502a9c074f8725d6aa0429`. The report records four tool dispatches, `case: unavailable` and `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Initial session A and B turns | Both returned the pinned Gateway HTTP 500 catalog/allowlist error before provider execution. The service process had not been started, its port was closed before and after each turn, and both the owner file and service transport evidence were absent |
| Service becomes available | Exactly one service startup was recorded. The published environment matched, startup found an empty owner scope, and authenticated readiness observed zero adapter registrations and SDK tasks |
| Quiet cooldown | No new turns were submitted for 35 seconds. No service request, binding execution or retirement occurred, and both registry/SDK counts remained zero |
| A and B refresh turns | Each newly submitted turn returned HTTP 500 from the stale catalog without reaching the provider, while completing exactly one successful MCP initialization and tool listing. Correlated evidence established distinct fresh identities and registry/SDK counts of one then two |
| New work and connection reuse | A separately submitted turn in each logical session completed a valid workload result on its recovered identity. Both then completed another valid call using the same identities; exactly four tool dispatches occurred |
| Replay and ownership | None of the initial or refresh-triggering failed turns reached the provider later. Gateway stayed running; the service started once and stayed running after that start. Owner-file bytes and the unrelated sentinel were preserved |
| Final shutdown | The service exited with status zero, drained session/active state and closed its listener. Independent inspection found zero remaining owned containers and fixture processes |

This qualifies an initially absent loopback service that is subsequently started once. It does not simulate failed startup cleanup, a poisoned live service, dropped connections during active work or a daemon outage. The two initial Gateway failures have independent process/listener/file observations rather than MCP HTTP response evidence, because no MCP server exists at that stage. Subsequent initialization and listing are validated against actual service transport observations. A generic Gateway HTTP 500 alone would not satisfy either stage.

The first-failure backoff and cached-catalog explanation from the capacity case is consistent with this execution. A ready service did not cause spontaneous discovery during the quiet interval; new turns triggered it. The triggering turns still failed before provider execution, and only separately submitted work completed. This is not a policy for automatically retrying unknown workload outcomes or a recovery latency guarantee. The 35-second interval is a bounded qualification margin; earlier retry timing and repeated exponential backoff were not measured.

All eight executed Python source hashes match Git bytes at the successful candidate. The changed lifecycle checker SHA-256 is `76c82fc5ebdbf4b8b134fb63eec55628539eccd791b62b6b9264927403c823a3`; the other seven retain their preceding recorded identities. The three installed Gateway discovery/allowlist source hashes exactly match the table in the delayed capacity-recovery result above and are also retained in the report.

Reproduce with the existing lifecycle command plus `--case unavailable` and a fresh private root. Retain `startup-report.json`, `startup-discovery.json` and private provider/transport/Gateway logs; credentials, host paths and raw identities are not committed. The existing registry and combined lifecycle modes retain their 26- and 54-dispatch contracts and were not rerun for this addition. Most changes to their orchestration are the guard around the independent case and shared snapshot/source-identity helpers; `git diff -w` separates them from indentation. Separate live MAF/stdio cases were not rerun.

The focused evidence suite passed 490 tests. Regressions reject failure projections without independent service absence, unexpected registration counts during discovery, and nonzero/unfinished final exits in the new mode; the existing discovery correlation, no-work and lifecycle evidence checks remain covered. Actual Docker cleanup/removal failures, other retirement triggers, idle expiry, prolonged/concurrent churn, repeated discovery failure/backoff and remaining real-host transport/MAF combinations remain open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). No actual-model retry, independent watchdog, host-crash or unattended-operation claim is made.

## Idle-session expiry: bounded follow-up (2026-10-08)

The independent `--case idle` qualifies timer-driven retirement through the service's existing sweeper, followed by Gateway reconnection for new work. It first completes valid calls in two logical sessions, starts a long compilation in A, and waits until both registrations exceed a two-second fixture threshold before arming that threshold. The observer records the unchanged 900-second default separately, observes the sweeper as the actual retirement caller, and restores the default after retirement. No session timestamps, production source, Gateway source or retirement implementation are changed. This is an accelerated expiry/isolation case, not a measurement of the production 15-minute idle interval.

B must finish draining its SDK task, transport streams and adapter registration while A retains its exact compiler, active request and MCP identity. Only after that observation does the checker explicitly abort A's turn and require correlated accepted MCP cancellation plus exact-container removal. New work in A must reuse its identity; new work in B must reject the expired identity with HTTP 404, initialize a fresh one, succeed and subsequently reuse it. The case expects six tool dispatches, no replay of the aborted turn, unchanged processes/ownership, preservation of the unrelated sentinel and final service exit zero. It writes `idle-report.json` and `idle-expiry.json` and retains `complete_matrix: false`. Candidate-specific live evidence is recorded below.


## Accelerated idle expiry and active-session isolation: live result (2026-10-09)

The independent `--case idle` passed on clean candidate `76e5d8b60cc50859affa4ded3b763f62bfa61c96` using unchanged OpenClaw 2026.9.7, published core 0.46.0 / Bicep 0.22.0 / Docker 0.24.4, MCP 1.28.1, MAF core 1.19.0 and Uvicorn 0.54.0. Docker server 29.8.2 used the same prepared image `sha256:5611ccff6015b5dd25d512c1f2dc6ab3cdfaecf9274a523c48cc07116fb38c89` and compiler configuration SHA-256 `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1`. Production service/binding files and installed Gateway code were unchanged. The fixture temporarily changed only the running service object's idle threshold; the actual sweeper and retirement implementation executed normally.

`idle-report.json` SHA-256 is `5d6e99c4c5b5ec1b483dadca901dd577c8982bc6b232cd1da0c3301a648beeee`; `idle-expiry.json` SHA-256 is `e189db9f7b40c27340fc245fcea2ab2d9e1951a94311444c633df0ca7180128a`. The report records six tool dispatches and retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Baseline | Two logical Gateway sessions completed valid calls with distinct MCP identities and exactly two SDK/adapter registrations |
| Active work | A's long compilation held the shared admission slot and had one outstanding typed request; the exact owned container contained a Bicep compiler |
| Expiry eligibility | The observer recorded a 900-second default and a two-second fixture threshold. At the sweeper's retirement call, B's last-touch age was about 22.66 seconds and A's about 9.83 seconds; both exceeded the fixture threshold |
| Timer origin | The observer compared the caller's code object with the installed adapter's `_expire` method. Exactly one timer retirement was recorded, with no client request, DELETE, binding dispatch or explicit retirement during the observation window |
| Expired B | Its SDK task finished successfully, transport terminated, and stream/request-ID/request counts reached zero. The adapter registration was absent; A was the sole remaining registration |
| Preserved A | A retained its active request, running SDK task, MCP identity and exact compiler container. Owner bytes, Gateway/service processes and the unrelated sentinel survived; the service remained unpoisoned |
| Explicit abort | Only after expiry and compiler-survival observation did the checker abort A's Gateway turn. Correlated accepted MCP cancellation and exact-container removal were required before continuing |
| New work | A completed a valid call using its original identity. B's stale transport received GET/DELETE HTTP 404, then one fresh initialization preceded a valid call on a new identity. A second B call reused that identity; the final registry held exactly two SDK tasks and adapter records |
| Replay and shutdown | No result for the aborted turn reached the provider, and exactly six tool dispatches remained through shutdown. The service drained and exited with status zero; independent inspection found zero owned containers and fixture processes |

The threshold was restored to 900 seconds when B's retirement completed. The observer does not edit `touched` timestamps or call retirement itself; it observes the real sweeper's call and the returned task. The ages above show that elapsed idle age alone did not retire A: its active work kept the registration alive. The two-second threshold makes this an accelerated isolation/retirement check, not a measurement of t…29919 tokens truncated…ns, the Windows owner-loss matrix on Linux, arbitrary escaping descendants, unattended recovery, delegated egress, MXC or production operation. The earlier Docker Desktop and Windows records retain their own candidate boundaries.

## GitHub-hosted real-model profile (2026-10-28)

[#2288](https://github.com/sokolaidev/maf-extensions/issues/2288) follows the deterministic hosted sample with an explicitly dispatched Azure OpenAI run. The repository's existing `live-verify` environment supplies the endpoint, deployment and OIDC identity; no new Azure resources or long-lived model key are needed. The profile uses the public [Azure OpenAI v1 Chat Completions API](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat?view=rest-microsoft-foundry-v1) with an Entra bearer token obtained after federated login.

The published dependency audit found core 0.52.0, Docker 0.30.0 and MAF 1.23.0 available, but published Bicep 0.24.0 and MCP 0.2.0 still require the preceding core/MAF line. The current source worker already requires core 0.52/MAF 1.23. This profile therefore selects the published MCP 0.2.0 wheel with SHA-256 `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545`, alongside the sample's existing published core 0.51.1, Bicep 0.24.0, Docker 0.29.0 and MAF 1.22.0 pins. It does not qualify the worker built from current main or change release order. The deterministic source-wheel route remains separate.

Acceptance retains the original invalid source and the minimal valid repair, source/configuration/image attribution and independently verified cleanup. The model may make multiple repair calls, but every observed call must complete and confirm cleanup. The relay bounds requests to 32 and completions to 4,096 tokens, requires completed streaming responses, and records only counts and hashed provider/model identities in public evidence. The hosted collector binds acceptance to the selected mode and rejects smoke substitution, incomplete forwarding and missing cleanup. Only bounded Markdown evidence is uploaded; runtime JSON and private logs stay outside the checkout in disposable runner storage.

The accepted hosted result below identifies its exact source, runtime, worker wheel, compiler image, dependency inventory and provider/model hashes. It qualifies the explicitly selected Chat Completions profile with reasoning disabled. Linux owner-loss qualification, reasoning-enabled tools, other models/providers, delegated egress, native MXC and production operation remain separate. The unsuccessful attempts are retained before the accepted result.

The first hosted attempt, [run 40782344281](https://github.com/sokolaidev/maf-extensions/actions/runs/40782344281), tested `9f67b26d17f079f100e0e3a995e127ae40c3f200` on Ubuntu image `20261026.354.1`. OIDC login, wheel checksum verification and compiler-image download passed. The sample failed; its console reported stopped processes and no cleanup errors. The collector then discarded partial observations after a missing-file `FileNotFoundError`, so the public record does not identify the sample's failing phase or establish independent owner cleanup. The retained Markdown SHA-256 is `4307c5503f38db51478083e037ff67a8a4f5ef97b6d8808c21cde21e0847a652`. This attempt cannot be attributed to setup or provider behavior from its retained evidence. The next candidate retains a fixed stage label, exception class, numeric exit code and selected fixed diagnostic codes, while keeping raw logs private and preserving partial evidence when owner or tool evidence is absent.

[Run 40783428530](https://github.com/sokolaidev/maf-extensions/actions/runs/40783428530) tested `ed213fc08a6cf9d79eb9f41668ff53153d2fa8b6` on the same runner image. Installation and Gateway startup passed, but the invalid-source turn observed eight provider requests, eight errors and zero forwarded responses; no compiler result was accepted. Final process-group cleanup, owner-lock reacquisition and an independent zero-container inventory passed with no cleanup errors. Missing tool evidence kept collection unqualified. Its Markdown SHA-256 is `f71e61b2a74a25de5616de393753a90a35bdd9e7c2da6d7b5cf61e100899a2f0`. This identifies the failing phase without identifying the provider rejection; the following candidate records HTTP status and fixed allowlisted API error identifiers, excluding arbitrary provider messages.

[Run 40784605910](https://github.com/sokolaidev/maf-extensions/actions/runs/40784605910) tested `2facd0dd759fead8c68c88682cf4de50a69a7b2e` and identified HTTP 400 responses naming `reasoning_effort` for all eight requests. Zero responses were forwarded and no compiler result was accepted; process cleanup, owner-lock release and the independent empty owner scope passed again. The retained Markdown SHA-256 is `37a3597966d0670ab2d2af354ac08c519b479f32ca089d3152027183261a7cbe`. The next candidate omits the Gateway's reasoning-effort control from forwarded requests and uses the configured provider's default; it retains the same completion-token cap and acceptance requirements.

[Run 40785484775](https://github.com/sokolaidev/maf-extensions/actions/runs/40785484775), candidate `b5c62171457a01774265ea0dccb67a1f7870bde9`, repeated the same eight HTTP 400 rejections with the field omitted. Cleanup passed and no model response or compiler result was accepted. Its Markdown SHA-256 is `c9d892da903db0679120db1e10471d0d1b5cb0d84a8d9b45c1b837742c127b36`. The inference that omitting the field would restore compatibility was wrong. The configured model's [published API contract](https://developers.openai.com/api/docs/models/gpt-6-luna) permits Chat Completions function tools only with `reasoning_effort: none`; the default is `medium`. The following profile explicitly selects `none` and records it as an acceptance condition. This remains real-model tool selection and repair, but cannot qualify reasoning-enabled tool use. Responses API support would require a separate provider/Gateway design and qualification.

[Run 40786514492](https://github.com/sokolaidev/maf-extensions/actions/runs/40786514492), candidate `434d6946c2120326b1df32d381ca92b3db731c1f`, completed four real-model forwards with `reasoning_effort: none`, no provider errors and one hashed returned-model identity. The invalid-source turn still produced no accepted tool evidence, so the profile remained unqualified. Final process cleanup, owner-lock release and the independent zero-container inventory passed. Its Markdown SHA-256 is `e1aa661731e2116dce497117978c2e65d9cc2509dcbd929e92847d3ee112ad59`. A subsequent regression test reproduced the reader ignoring the canonical `mcp:bicep:bicep__bicep_validate` tool ID while recognizing its short alias. The next candidate recognizes both forms under the same result/source/image/configuration/cleanup checks and retains fixed-category call counts without publishing call identities. This is a demonstrated reader defect, but this attempt's retained evidence does not establish which identifier the model used.

[Run 40787367427](https://github.com/sokolaidev/maf-extensions/actions/runs/40787367427), candidate `0f14be1767da35960b2037fe43c350c15d4a9dc0`, observed one `tool_search`, one `tool_describe` and one canonical Bicep `tool_call`, with four completed model forwards and no provider errors. The reader captured the Bicep result and rejected it with `changed-invalid-source`; no turn was accepted. The submitted-source digest, image/configuration attribution, completion and per-call cleanup checks precede that refusal and passed; final cleanup and independent owner inventory passed too. Its Markdown SHA-256 is `00e15f8cb1f89dacc95c9ab3436a625548d368ea5df073e3f78e8a5ca2fa93af`. The retained evidence does not distinguish a whitespace change from another source alteration. The next candidate supplies the exact first-source JSON string and explicitly forbids extra bytes; byte-for-byte acceptance remains unchanged.

### Accepted real-model result

[Run 40787931344, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/40787931344) passed on 2026-10-28 UTC at `1c026062dc85a13cb710399018cd78d22124e690`. Seven model requests completed with seven forwarded responses, no provider errors and no upstream failure records. The model made two discovery searches, one description request and two canonical Bicep calls. One call per turn produced the required invalid then valid compiler verdicts; each completed with confirmed cleanup and matching source/configuration/image attribution. Final process-group absence, owner-lock reacquisition and the independent zero-container inventory passed with no cleanup errors. Both `passed` and `qualified` are true.

The explicit profile uses `reasoning_effort: none`, a 32-request budget and a 4,096-token completion cap. It establishes actual model-driven discovery, tool use and repair under that setting. It does not establish reasoning-enabled tools, general model reliability, every repair retry pattern, a newer source-built worker, Linux owner-loss, other providers/hosts or production acceptance. Later documentation changes do not broaden this executable candidate.

| Candidate or evidence | Recorded identity |
| --- | --- |
| Source | `1c026062dc85a13cb710399018cd78d22124e690` |
| Sample launcher SHA-256 | `19997811b786efb888d906d19b55fae89b172fe5c944e886b234967ee5f9b759` |
| OpenClaw | `2026.9.7` |
| Runner image | `20261026.354.1` |
| Platform | `Linux-7.0.0-1017-azure-x86_64-with-glibc2.39` |
| Node | `v26.8.1` |
| uv | `uv 0.12.5 (x86_64-unknown-linux-gnu)` |
| Worker Python | `Python 3.13.15` |
| Docker Engine | `28.0.4` |
| Docker API | `1.48` |
| containerd | `v2.4.2` |
| runc | `1.5.2` |
| Compiler registry digest | `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Published worker wheel SHA-256 | `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545` |
| Endpoint SHA-256 | `308241e7111832947f4614c87b2bb7f8a135c63fbac6cf5fce09ffa744c635de` |
| Requested model SHA-256 | `9a7e60cd1c7bb9d5493832103af02aca05272564b29e49852a9a0b5d8d96ec2c` |
| Returned model SHA-256 | `b8be814ffed8fd2f3fc7c660d5f2038c33c02a1d0ed3a6a31e542bb97c23813a` |
| Private report SHA-256 | `ae1c74ac2718613c895cf21b414a3f6d752fd6661762ef5998cc4abb4f1c392f` |
| Private tool evidence SHA-256 | `938fb6a5be2e67b44a393708e74fd305ae41368a899a1beb2040a18b423a2545` |
| Retained Markdown SHA-256 | `9c511cd03ff095b0e7fee75d30e7a30813907edfa3cb41a8e32b518f3d736e32` |

| Turn | Calls | Verdict | Cleanup | Submitted-source SHA-256 |
| --- | --- | --- | --- | --- |
| 1 | 1 | `invalid` | `confirmed` | `91285d8d03f8c76ee86fddeab9f039132ebc75ddcc0f5dbd3a0f5e4bd7b4b0cf` |
| 2 | 1 | `valid` | `confirmed` | `25a645af6181b25dc4727c19663a910c8c45d8ca84bf4db34548c16c11c301c7` |

The installed worker modules matched the verified wheel:

| Module | SHA-256 |
| --- | --- |
| __init__.py | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| server.py | `005eeeccd40ab0dc65a31281a72e3b353eae87cf62d42afb590b447faa3ba2bb` |
| workload_http.py | `4bc49175b2c4bb0159a8c90481703f820380b02058f86ff4f49041f7afa7bb0f` |
| workload_service.py | `3c44b867007dfc8fc026c0e094d106ed64174311c4a82fd4e61bca31a96ab34b` |

The complete installed worker dependency inventory was:

| Distribution | Version |
| --- | --- |
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.22.0` |
| annotated-types | `0.8.0` |
| anyio | `4.15.1` |
| attrs | `26.1.0` |
| certifi | `2026.10.23` |
| cffi | `2.1.1` |
| click | `8.5.0` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore | `1.0.9` |
| httpx | `0.28.1` |
| httpx-sse | `0.4.3` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.51.1` |
| maf-sandbox-bicep | `0.24.0` |
| maf-sandbox-docker | `0.29.0` |
| maf-sandbox-mcp | `0.2.0` |
| mcp | `1.30.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.1` |
| pycparser | `3.12` |
| pydantic | `2.14.1` |
| pydantic-settings | `2.16.2` |
| pydantic_core | `2.50.1` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.32` |
| referencing | `0.37.0` |
| regex | `2026.9.29` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.6.0` |
| starlette | `1.7.0` |
| typing-inspection | `0.4.4` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

The evidence artifact contains only allowlisted Markdown observations. Credentials, endpoint/deployment names, sessions, submitted source and raw provider responses were not included in it. The private report and tool evidence were hashed before the disposable runner was removed.

## GitHub-hosted Linux actor-loss profile (2026-11-01)

After #2294 merged and #2288 closed, [#2386](https://github.com/sokolaidev/maf-extensions/issues/2386) selects a separate Linux actor-loss profile. The Windows campaign remains fifteen cases on its original candidate. The new `owner-loss-linux-v1` profile selects thirteen: a control plus Gateway, client and worker loss at pre-admission, active compilation, held response and held cleanup. Docker connection unavailability and daemon removal denial are excluded from this first Linux profile and remain unqualified there.

The [runner](../../../scripts/openclaw_conformance/linux_owner_loss.py) requires a fresh deterministic installed sample on the same runner before faults. It pins the published `maf-sandbox-mcp` 0.2.0 wheel and core/Bicep/Docker/MAF/MCP at 0.51.1/0.24.0/0.29.0/1.22.0/1.30.0, preserves OpenClaw 2026.9.7 and the prepared compiler digest, and records the complete installed dependency inventory and runtime file fingerprint. A fresh resolved transitive dependency inventory is a new candidate; matching the principal pins does not transplant prior qualification.

The Linux injector verifies process group/session membership and a stable procfs birth identity around pidfd acquisition. It sends SIGKILL through that descriptor and observes exit through the same descriptor. Existing correlated request, barrier, result, settlement, sibling, replacement and stale-authority predicates remain mandatory. Worker restart retains its exclusive owner and rotates the token explicitly; process death alone is not readiness, recovery or cancellation. Both product recovery and process cleanup must complete before acceptance. Harness orphan removal cannot turn failed recovery into a pass.

The opt-in workflow runs on Ubuntu 24.04 without cloud credentials or actual-model calls. Reproduce with `gh workflow run docker-live.yml --ref <candidate> -f openclaw_bicep=true -f openclaw_mode=owner-loss`. Only the bounded Markdown report is uploaded. Runtime state, raw events, sessions and JSON remain in temporary directories outside the checkout. Actual-model retry behavior, Docker/host failure, escaped process trees, automatic watchdog recovery, newer workers, native MXC and production remain unqualified.

### Historical Linux v1 actor-loss result

This v1 result predates the stricter `owner-loss-linux-v2` evidence contract. It does not qualify v2: that contract additionally binds case artifacts to a fresh campaign and candidate with recorded hashes, correlates killed identities with startup records, and retains bounded diagnostics for incomplete campaigns. The fresh hosted v2 result is recorded below.

[Run 41353738697, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41353738697) passed on source `10ff274ad0fd1bc1c1f4c886c36f55aa206a5c13`. Its fresh deterministic installed baseline passed before fault injection, followed by all thirteen selected cases. The independent collector re-evaluated each raw case against the fixed profile, verified the same baseline and installed runtime, and emitted `qualified: true`. The retained Markdown SHA-256 is `d9750776c7ee2725e4ad47559d681d345dd903903bd572d358dedae4e9266086`.

The accepted predicates establish exact actor exit through Linux pidfds, surviving-service settlement after Gateway/client loss, explicit same-owner worker replacement after worker loss, cleanup before replacement readiness, old-token and old-session/cancellation/deletion refusal, no automatic replay, usable sibling/fresh calls, unrelated sentinel preservation and final process/container absence. Every case reported no harness or parent failure, confirmed independent cleanup, and no orphan removal by either cleanup sweep. This is bounded operator-driven recovery with deterministic provider decisions, not unattended or real-model fault recovery.

Runner: Ubuntu 24.04 image `20261026.354.1`, Linux `7.0.0-1017-azure` / `x86_64`, Python `3.13.15`, Node `v26.8.1`, uv 0.12.5, Docker `28.0.4` / API `1.48`, containerd v2.4.2 and runc 1.5.2. The thirteen cases took 479.246 seconds in total; setup and evidence collection are separate.

| Identity | Retained value |
|---|---|
| Qualification source fingerprint | `4e66bdafa1be6b6ee7f433f31e7d34d132e919c5d0930f5d9ec67a6a19eb2e6a` |
| Published worker wheel SHA-256 | `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545` |
| Compiler registry digest | `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Baseline report SHA-256 | `7d81ba1e12884cdaaec54665aabcada9765ece62525d49c5c5fb78e2c5fa1196` |
| Baseline tool evidence SHA-256 | `9be64e55d23cf67c5a99f56ea6bcf142c4307e2a2187f1947e24e35ff0ccdf97` |
| npm lock SHA-256 | `aaaf2df960db9281dd51f41782db739403dad083fa6e45d82df697b575dfdd9f` |
| Installed runtime (43057 files) fingerprint | `472ac08a26b0ff07790ca637794e91a81b0ca24915ad83287e12a24eee7c1cea` |
| Installed worker `__init__.py` SHA-256 | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker `server.py` SHA-256 | `005eeeccd40ab0dc65a31281a72e3b353eae87cf62d42afb590b447faa3ba2bb` |
| Installed worker `workload_http.py` SHA-256 | `4bc49175b2c4bb0159a8c90481703f820380b02058f86ff4f49041f7afa7bb0f` |
| Installed worker `workload_service.py` SHA-256 | `3c44b867007dfc8fc026c0e094d106ed64174311c4a82fd4e61bca31a96ab34b` |

| Case | Result | Seconds | Private case evidence SHA-256 |
|---|---|---:|---|
| `control` | Passed; independent cleanup confirmed | 22.566 | `a52c6c539bbaebb2ade092d5d39376c675a8c9f1e15e5ecf7fa17649c2e37332` |
| `gateway-pre_admission` | Passed; independent cleanup confirmed | 35.134 | `52cfcd3293a8d4ee45e90e52d1a060e2271ea55d9851e4b00f658dcb9b345b35` |
| `gateway-active` | Passed; independent cleanup confirmed | 38.142 | `84ace6c0073be7b3c56f81eb39163b71a9f944f44ce92089575a62cca1f26b7d` |
| `gateway-response` | Passed; independent cleanup confirmed | 35.048 | `91fefb5c0f51dda871e88079777d2cadd3f41e30852f748ca29609bc631bfa90` |
| `gateway-cleanup` | Passed; independent cleanup confirmed | 35.047 | `dd67d29a46b11a9bad7ca233799b05155071b5a2d87ca171e4bb87d1a0e3cc3a` |
| `client-pre_admission` | Passed; independent cleanup confirmed | 36.121 | `2b39803b4ccaf8cb35d40708fcdf83f070b748edb1a0bdbb3a66ed1061c334ad` |
| `client-active` | Passed; independent cleanup confirmed | 34.793 | `648730c42046df0f0a520e3093c77356cc0358d0bfc3feff34fea0a74614603c` |
| `client-response` | Passed; independent cleanup confirmed | 36.005 | `e70efbf09f8d33a2d0121f8f682695743579acb8207c6e83f21eab7805a64060` |
| `client-cleanup` | Passed; independent cleanup confirmed | 36.074 | `d6a26bc76f2b610d4b2a836d79220dd0d4912063e0487d54806faa07e5b4bd55` |
| `worker-pre_admission` | Passed; independent cleanup confirmed | 38.989 | `212a417b8f99a21a8f77d47b21d6f5aed446b0f7d19b2db277a36ef0a767ca7f` |
| `worker-active` | Passed; independent cleanup confirmed | 39.923 | `00fc89e0728514f351774dbae4cabbfc77004fdb51c2fabfd94487efa6dc5cbc` |
| `worker-response` | Passed; independent cleanup confirmed | 45.774 | `77d7bf687b23552c5a9add19fb3c9d15eac1ab68ad6e68d01fac41c5d5c79d63` |
| `worker-cleanup` | Passed; independent cleanup confirmed | 45.631 | `b8a56e5b4de4bb5c853b31cd8a62f7ee81d3b24d01f977772b09d6909eecbbe2` |

Complete installed Python inventory for this candidate:

| Distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.22.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| certifi | `2026.10.23` |
| cffi | `2.1.1` |
| click | `8.5.0` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore | `1.0.9` |
| httpx | `0.28.1` |
| httpx-sse | `0.4.3` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.51.1` |
| maf-sandbox-bicep | `0.24.0` |
| maf-sandbox-docker | `0.29.0` |
| maf-sandbox-mcp | `0.2.0` |
| mcp | `1.30.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.2` |
| pydantic-settings | `2.16.2` |
| pydantic_core | `2.50.2` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.33` |
| referencing | `0.37.0` |
| regex | `2026.9.29` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.6.0` |
| starlette | `1.7.0` |
| typing-inspection | `0.4.4` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

The principal pins match the selected published-worker profile; the transitive inventory is recorded independently rather than inherited from the earlier real-model run. The Windows fifteen-case result remains separate. Linux Docker-unavailable/removal-denied recovery and all broader exclusions above remain unqualified. Documentation and offline regression-test changes after the accepted source do not constitute another hosted run.

## Accepted Linux v2 actor-loss result (2026-11-02)

[Run 41527289097, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41527289097) passed the fresh installed baseline and all thirteen cases on source `8bf3e21324004a7ccfdfcfea646b0817bb47df8a`. The independent collector emitted `qualified: true` for `owner-loss-linux-v2`, with campaign `e563f70ea1c74732a0035c976fa030d1`. Retained Markdown SHA-256: `56aad15f8f06ba9e41595be0a8a9dd955ca564a74ac1a06f1adcb40a97a303e3`.

The v2 collector verifies each case's campaign ID, complete candidate digest and recorded artifact hash. It also matches killed PID and birth identity to the selected actor's retained startup binding, with separate worker/client process-start records. The source fingerprint and dirty-source check include the hosted baseline reporter. Postflight inventories are read-only, and case-bound receipts preserve failed or interrupted observations across collection retries. Incomplete campaigns remain unqualified while publishing bounded diagnostics for available cases, including parent cleanup interruption. The historical v1 result above does not satisfy this stronger contract.

Linux teardown disarms the case alarm and defers interruption until owned process groups are terminated. A failed graceful close is followed by forced termination and group-absence checks; container sweeping is refused while owned processes remain unconfirmed. The [Linux repository test job](https://github.com/sokolaidev/maf-extensions/actions/runs/41527146911/job/125557786110) ran the native alarm/TERM-resistance regression (0.385 seconds), owned pidfd termination (0.002 seconds), and unrelated-actor refusal (0.002 seconds) on this accepted source. Its retained JUnit records show all three passed without skips.

Runner: Ubuntu 24.04 image `20261026.354.1`, Linux `7.0.0-1017-azure` / `x86_64`, Python `3.13.15`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and Docker `28.0.4` / API `1.48`. Case execution total: 480.469 seconds, excluding setup and collection.

| Identity | Retained value |
|---|---|
| Qualification source fingerprint | `a437761c6eaa84432dbb81394db991df082dd0a983a4ff756d447a443f1e11b0` |
| Published worker wheel SHA-256 | `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Baseline report SHA-256 | `7d81ba1e12884cdaaec54665aabcada9765ece62525d49c5c5fb78e2c5fa1196` |
| Baseline tool evidence SHA-256 | `a66c2676a02bbb294a0f01f8ed4c741e4902abe8a85dd9bd55d11f49f0923a3b` |
| npm lock SHA-256 | `5db997dec92617c66bc22230ab3aed80f0efea55385950847392cae65f35f0d8` |
| Installed runtime (43059 files) fingerprint | `740b4dad83d0b6668e5b3bd143b3429ca41625977bf97ec0db06da448b5a04cc` |
| Installed worker __init__.py SHA-256 | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker server.py SHA-256 | `005eeeccd40ab0dc65a31281a72e3b353eae87cf62d42afb590b447faa3ba2bb` |
| Installed worker workload_http.py SHA-256 | `4bc49175b2c4bb0159a8c90481703f820380b02058f86ff4f49041f7afa7bb0f` |
| Installed worker workload_service.py SHA-256 | `3c44b867007dfc8fc026c0e094d106ed64174311c4a82fd4e61bca31a96ab34b` |

| Case | Result | Seconds | Private case evidence SHA-256 |
|---|---|---:|---|
| `control` | Passed; independent cleanup confirmed | 22.560 | `f83b1ad35d0bd99233da3e7ef7278ddcccf330b88fea81eb1a3b6a1b5bcfa7bc` |
| `gateway-pre_admission` | Passed; independent cleanup confirmed | 35.479 | `628101973389823e023e8810662bb59d0390598751eabc80b30b354dc7a11f55` |
| `gateway-active` | Passed; independent cleanup confirmed | 39.020 | `ef75e034539ed31eb0f863a44cd8d0f1e5f33641a7ff7b9649ecc21d2fbb3b61` |
| `gateway-response` | Passed; independent cleanup confirmed | 36.274 | `0bc1d82fc42f46c8c95dfb2a80767ac7edf8582dd738ffc5f5b76d27f02f6362` |
| `gateway-cleanup` | Passed; independent cleanup confirmed | 35.668 | `292d34289572b56d2233dd59c63a092e8d71b2b5a6dcdbdd76e69a01dae9fe39` |
| `client-pre_admission` | Passed; independent cleanup confirmed | 29.931 | `4203419412b429c6b0f0f63e81078ec85869a309820e6ff787f3c77689c042cb` |
| `client-active` | Passed; independent cleanup confirmed | 35.336 | `2533bd362988806fbf9246fa5cabdf2728a4a4aec56e13479fe415839de695d2` |
| `client-response` | Passed; independent cleanup confirmed | 36.464 | `e3969751269cfbeff24f6ff078c094d78a34c4593af286f10d4262d04c82cc52` |
| `client-cleanup` | Passed; independent cleanup confirmed | 36.219 | `a64f4013e4e30834e3b7086798a7e2a4ca977d8c84cde86fa821f6259479cd7d` |
| `worker-pre_admission` | Passed; independent cleanup confirmed | 39.973 | `55ab6f3d9b37a82999d914997e4684e387a2579c59072a2912042a424d02f9ee` |
| `worker-active` | Passed; independent cleanup confirmed | 41.055 | `1bc2a90d0504ac9e2b129aa8d89f3a3ea557f2df761b96b05206c7c31411000e` |
| `worker-response` | Passed; independent cleanup confirmed | 46.229 | `48ff2202e12f307c994a2965072cfebf002861198a283e37e9fac1cec2450b73` |
| `worker-cleanup` | Passed; independent cleanup confirmed | 46.260 | `d37b805cbe3ec48a925a753df81a157829113f12be98f88d2b7e09241eb03ccb` |

Complete installed Python inventory for this candidate:

| Distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.22.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| certifi | `2026.10.23` |
| cffi | `2.1.1` |
| click | `8.5.0` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore | `1.0.9` |
| httpx | `0.28.1` |
| httpx-sse | `0.4.3` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.51.1` |
| maf-sandbox-bicep | `0.24.0` |
| maf-sandbox-docker | `0.29.0` |
| maf-sandbox-mcp | `0.2.0` |
| mcp | `1.30.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.2` |
| pydantic-settings | `2.16.2` |
| pydantic_core | `2.50.2` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.33` |
| referencing | `0.37.0` |
| regex | `2026.9.29` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.6.0` |
| starlette | `1.7.0` |
| typing-inspection | `0.4.4` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

Every case passed without harness or parent failure, with independently confirmed resource absence. This result qualifies only the recorded deterministic-provider candidate and bounded operator-driven recovery. Docker unavailability/removal denial, daemon or host loss, escaped process trees, actual-model retry behavior, unattended watchdogs, newer candidates and production remain unqualified. The documentation update that records this result does not constitute another hosted run.

## Linux v3 Docker failure extension (2026-11-03)

[#2413](https://github.com/sokolaidev/maf-extensions/issues/2413) extends the fixed profile to fifteen cases, retaining the thirteen actor-loss cases and adding `worker-unavailable` and `worker-denied`. The installed worker, OpenClaw and principal dependency pins are unchanged. A new campaign must run a fresh installed baseline and all fifteen cases; the historical v2 result does not qualify this extension.

Both new cases kill the pidfd-bound worker while its exact compiler container is independently observed running and paused. The replacement must exit without readiness while the orphan remains. `worker-unavailable` uses a private CLI context pointed at a reserved, non-listening loopback socket, with failed and restored Docker probes using the replacement's environment. It does not stop the parent daemon or qualify an actual daemon outage. `worker-denied` uses the existing Go authorization plugin in a disposable, mutually authenticated daemon and denies DELETE only for the selected compiler. The collector joins the real failed commands to denial decisions and the later successful cleanup to allow decisions, requiring cleanup before replacement readiness.

The dedicated daemon uses `docker:29.8.2-dind@sha256:6316d612722fd7fb2bd7edf9fcbb4439d08915cfb2e0e0b28bf16675fc9f3314` (Linux amd64), whose configuration identity is `sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1`. The authorization source SHA-256 remains `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4`; the workflow pins Go 1.27.3 and records the built binary hash. The daemon uses the classic overlay2 image store to preserve the hosted parent's prepared compiler configuration identity. The earlier Windows fixture's containerd store remains separate. The disposable daemon is privileged, bounded to two CPUs, 2 GiB and 512 processes, exposes only a random loopback TLS port, and mounts no parent Docker socket. This fixture is not a security-isolation claim.

The v3 evidence retains blocked/restored engine identity, unchanged orphan inventory, sentinel survival, no startup readiness during the blocked interval, subsequent cleanup, stale-authority refusal and usable fresh work without replay. Dedicated-daemon and volume registrations are hashed into each case; postflight rechecks those bytes and inventories the exact parent scope without deleting survivors. Failed or interrupted receipts remain rejected on collection retries. Emergency cleanup is not product recovery. Host and daemon loss, escaped process trees, actual-model retry decisions, unattended watchdogs, newer dependency candidates and production remain outside this profile.

The accepted hosted v3 result is recorded below. Runtime JSON, logs, credentials and private case records stay in temporary directories outside the checkout; the workflow retains only bounded Markdown evidence.

The first v3 [run 41544175541, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41544175541) at `cd745f7e2fa2e5872040149c339042384b70cb9b` passed the fresh baseline, all thirteen actor-loss cases and removal denial, with independent cleanup confirmed in every case. Connection-loss qualification refused with `private_context_reachable` before starting the blocked replacement, so the campaign remained unqualified. Its Markdown SHA-256 is `c1bc0dfedcc809856650b261ea9a5515e8379353e7a9cd6da427cd34d70a74a4`. The probe relied on the return code of formatted `docker info`; [Docker 28.0.4's implementation](https://github.com/docker/cli/blob/v28.0.4/cli/command/system/info.go#L93-L115) returns the formatter's result even when server information cannot be fetched. The retained report does not establish the underlying connection result. The corrected probe uses a server-side container inventory; the restored probe still requires the original nonempty engine identity. A new complete campaign is required, and the failed attempt is not reinterpreted as acceptance.

The intermediate [run 41546361600, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41546361600) at `7ceee45e573387b808a350ea022f808899ee920f` passed the fresh baseline and all fifteen cases, with independent cleanup confirmed. Its Markdown SHA-256 is `443d79020eaed308148c4138159afd3ccfdafc767def88ba104a3445f34b4fec`. That run predates the final emergency-cleanup bookkeeping correction; the complete final-source campaign is recorded below.

## Accepted Linux v3 Docker failure result (2026-11-03)

This is the historical v3 result, superseded by the v4 acceptance requirements below. Review identified two verifier gaps: a failed observer probe could hide a product startup attempt, and removal-command copies could agree without identifying the selected orphan. The retained v3 report does not establish those stronger predicates.

[Run 41547803565, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41547803565) passed the fresh installed baseline and all fifteen cases at source `e3832c4708ba9ef88bba84585d3cc38eb56fe858`. Independent collection returned `qualified: true` for campaign `f323e5fd83234bc0a76540361d4df541` under `owner-loss-linux-v3`. Retained Markdown SHA-256: `6ffae578d2d650978ab8492c2e009db2f35e8ed7e8a4ae3cad57fde74e3fcfc0`.

Both blocked replacement cases passed: startup refused with the exact orphan retained, and restoration allowed product cleanup before readiness. Old token/session/cancellation/deletion authority was rejected while fresh work remained usable without replay. Removal-denial decisions and restored allow decisions matched the real commands. The unrelated sentinel survived, the dedicated daemon did not restart, and independent inventories confirmed absence of registered daemon/container/volume resources. No emergency cleanup established recovery.

Runner: Ubuntu 24.04 image `20261026.354.1`, Linux `7.0.0-1017-azure` / `x86_64`, Python `3.13.15`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and parent Docker `28.0.4` / API `1.48`. The denial daemon was Docker `29.8.2` using `overlay2` and mutual TLS, with `go version go1.27.3 linux/amd64`. Case execution total was 371.773 seconds, excluding setup and collection.

| Identity | Retained value |
|---|---|
| Qualification source fingerprint | `4b4530283180f2d92095b31464f18855ad7b90e3dec86cc6bbdede2530c8acbf` |
| Published worker wheel SHA-256 | `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Baseline report SHA-256 | `7d81ba1e12884cdaaec54665aabcada9765ece62525d49c5c5fb78e2c5fa1196` |
| Baseline tool evidence SHA-256 | `079dc7cf73b7b9f617b330ec6776dfad07a9ac60b6e748021a043073a0a0de15` |
| npm lock SHA-256 | `5db997dec92617c66bc22230ab3aed80f0efea55385950847392cae65f35f0d8` |
| Installed runtime (43059 files) fingerprint | `4a4ff7d736bab73319f9d5c1a04abc5c8a9cd180a920257c3fe32094bd3b1f07` |
| Denial daemon image ID | `sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1` |
| Authorization source SHA-256 | `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4` |
| Authorization binary SHA-256 | `923dbc7fe663de0a65a3ee53dff51cb5b38c8a3f7d6f5c1ced5172bb5a725e05` |
| Daemon registration SHA-256 | `68b31f99366a7a66559d6943ab14e01edd9d298c43ef1b502648ff98568e68cf` |
| Daemon control SHA-256 | `3765bfc069c2d595bcdc60da31bdbb91260b87e065e92a3ab93da51a0a7d29bc` |
| Installed worker __init__.py SHA-256 | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker server.py SHA-256 | `005eeeccd40ab0dc65a31281a72e3b353eae87cf62d42afb590b447faa3ba2bb` |
| Installed worker workload_http.py SHA-256 | `4bc49175b2c4bb0159a8c90481703f820380b02058f86ff4f49041f7afa7bb0f` |
| Installed worker workload_service.py SHA-256 | `3c44b867007dfc8fc026c0e094d106ed64174311c4a82fd4e61bca31a96ab34b` |

| Case | Result | Seconds | Private case evidence SHA-256 |
|---|---|---:|---|
| `control` | Passed; independent cleanup confirmed | 15.361 | `f3c76fc8129200b12760079930aca3f7359b030626103d4ae392cb4c98276f11` |
| `gateway-pre_admission` | Passed; independent cleanup confirmed | 23.104 | `9c7c3eda8a8ba51191d892452e328808a6b23096cd2d61230efea4f72a79a4d7` |
| `gateway-active` | Passed; independent cleanup confirmed | 24.759 | `868a4fd1eef5451b5bdb729b1f0dd7de42b1e0b635436f48e441308b5ae8a6ac` |
| `gateway-response` | Passed; independent cleanup confirmed | 23.010 | `864403726d1b61a0461f1cb70beed7853e8fe8ddee919e52e039130942e98134` |
| `gateway-cleanup` | Passed; independent cleanup confirmed | 23.095 | `8065d5509c3bee3608281e4ab91342f0c5f11551cb39a409dbd81f9de53bfe82` |
| `client-pre_admission` | Passed; independent cleanup confirmed | 20.262 | `651e7cb79442e3508310c359aafec29b67b8dd3955ba7fb444bad318f90b4599` |
| `client-active` | Passed; independent cleanup confirmed | 23.466 | `bde2cc5f592380c450c5960a4bbe4842c5e68ffa57205e1890b4b3e776b4f05d` |
| `client-response` | Passed; independent cleanup confirmed | 24.593 | `edb1a787103e9408a79a34eb7ddaf354bda02d01921da4b3b089e4b4caf18aa4` |
| `client-cleanup` | Passed; independent cleanup confirmed | 24.216 | `d5870f7611eef1ff6fc27e776e9ce85771a6b678b890cf272cbf70e838cc103b` |
| `worker-pre_admission` | Passed; independent cleanup confirmed | 25.977 | `ce51db82b1227bd206bcd182c77b33c612c49b99376b6537e8a7c69ba09ebdc7` |
| `worker-active` | Passed; independent cleanup confirmed | 26.664 | `dc659af677be33eb8ee4202f0f36f598cc247f1c5a7b04e1d490d9a95ef3c568` |
| `worker-response` | Passed; independent cleanup confirmed | 29.327 | `b465002b5475f912b47f30edd969de53b96bbc9687b423dde2ecdd528e3176f8` |
| `worker-cleanup` | Passed; independent cleanup confirmed | 29.364 | `48004e28fe3081f42fedb3b59b74c6ab421e589b23fa739cd857f63383c523e9` |
| `worker-unavailable` | Passed; independent cleanup confirmed | 27.681 | `efafff701e551e42c637d1eceb40a38439c5c526406cf979561b2d21bdfcc7df` |
| `worker-denied` | Passed; independent cleanup confirmed | 30.891 | `b1388c2a468d2ff5f3d72233087383bc62d2886f72c263e907fbd323d2861900` |

Complete installed Python inventory for this candidate:

| Distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.22.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| certifi | `2026.10.23` |
| cffi | `2.1.1` |
| click | `8.5.0` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore | `1.0.9` |
| httpx | `0.28.1` |
| httpx-sse | `0.4.3` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.51.1` |
| maf-sandbox-bicep | `0.24.0` |
| maf-sandbox-docker | `0.29.0` |
| maf-sandbox-mcp | `0.2.0` |
| mcp | `1.30.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.2` |
| pydantic-settings | `2.16.2` |
| pydantic_core | `2.50.2` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.33` |
| referencing | `0.37.0` |
| regex | `2026.9.29` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.6.0` |
| starlette | `1.7.0` |
| typing-inspection | `0.4.4` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

This accepts the recorded deterministic-provider candidate and bounded operator-driven recovery only. Connection loss affects the replacement's private Docker context, and removal denial is an exact-target daemon authorization decision. Actual daemon or host loss, escaped process trees, model retry decisions, unattended watchdogs, newer dependencies and production remain unqualified. Only documentation follows the accepted source; recording this result is not another live run.

## Linux v4 review corrections (2026-11-03)

The observer now records the product's `lifespan.startup.complete` before querying Docker. Both failed replacement-startup cases reject that marker while blocked, including when the observer itself fails before recording readiness. Positive startup controls require the same marker before each successful startup, bound to its boot. Shared owner-loss and lifecycle refusal checks also reject attempted startup completion.

Daemon authorization joins now validate each command's selected target and integer exit outcome. Owner-loss observations additionally require the independently inspected full container ID; lifecycle observations retain their separately selected name-only target contract. Rejection tests alter both copied and raw command records together, covering failed and restored removals. Permitted target names and exact IDs remain accepted.

Five of the six initial regression cases reproduced the reported false acceptance; the existing restored-container filter already rejected the sixth. The new regressions and sibling checks cover observer-induced failure, absent or misattributed positive startup markers, missing inspected IDs, target aliases and malformed outcomes. This strengthens qualification evidence without changing the installed product or published dependency pins. The fresh baseline and complete fifteen-case hosted v4 campaign passed as recorded below. Earlier v3 results are not reinterpreted as v4 acceptance.

The next review identified two additional correlations required before v4 acceptance: in both Docker failure cases, restored orphan removal must precede that boot's product `startup_complete`, not the observer's later inventory/ready event, and both retained daemon-registration clients must match the denial observation before any postflight inventory. Regressions reproduce cleanup between those two startup events and swaps of either registration file or both files together with updated hashes. All four reproductions failed before the fixes. These predicates are included in the fresh complete campaign required for acceptance.

The intermediate v4 [run 41630941621, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41630941621) at `2babb6f1eafd6e37d1a450909eff2fd66ed00497` passed its fresh baseline and all fifteen cases, with Markdown SHA-256 `392dadcaab4417687917558d3e809034c16a2d4fa2c8bf80254526d460375baf`. That source predates the product-startup cleanup deadline and registration-client binding fixes. It does not establish those predicates; the complete reviewed-source result is recorded below.

## Accepted Linux v4 Docker failure result (2026-11-03)

[Run 41632978862, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/41632978862) passed the fresh installed baseline and all fifteen cases at source `8ed98a0d00bd4b59c8ec4ff56f73ba0ab70e553d`. Independent collection returned `qualified: true` for campaign `f9f87c2515c9467685ee5d899d7dd6a7` under `owner-loss-linux-v4`. Retained Markdown SHA-256: `ccc5a4a3d675f3615de3d85133515813612bf34db2eb7847306374c311bff9d9`.

The v4 observer recorded startup completion before each successful startup inventory and observed no completion attempt during either blocked interval. Failed and restored removal records matched the independently inspected orphan ID, selected target and command outcome. In both failure cases, successful orphan removal preceded product startup completion. Postflight bound both registration clients to the denial observation before inventory. These are fresh observations from the corrected source; the historical v3 results do not supply them.

Both blocked replacement cases passed: startup refused with the exact orphan retained, and restoration allowed orphan removal before product startup completion. Old token/session/cancellation/deletion authority was rejected while fresh work remained usable without replay. Removal-denial decisions and restored allow decisions matched the real commands. The unrelated sentinel survived, the dedicated daemon did not restart, and independent inventories confirmed absence of registered daemon/container/volume resources. No emergency cleanup established recovery.

Runner: Ubuntu 24.04 image `20261026.354.1`, Linux `7.0.0-1017-azure` / `x86_64`, Python `3.13.15`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and parent Docker `28.0.4` / API `1.48`. The denial daemon was Docker `29.8.2` using `overlay2` and mutual TLS, with `go version go1.27.3 linux/amd64`. Case execution total was 571.559 seconds, excluding setup and collection.

| Identity | Retained value |
|---|---|
| Qualification source fingerprint | `3a5ab87eff7c20570a0ff7658ae9b7605419ad6f443754d116d8586ed9a2500e` |
| Published worker wheel SHA-256 | `ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Baseline report SHA-256 | `7d81ba1e12884cdaaec54665aabcada9765ece62525d49c5c5fb78e2c5fa1196` |
| Baseline tool evidence SHA-256 | `06ba1e8176c95304c7c251471ef2780970f317df6e0fbc59f07d61b40fe2789b` |
| npm lock SHA-256 | `5db997dec92617c66bc22230ab3aed80f0efea55385950847392cae65f35f0d8` |
| Installed runtime (43059 files) fingerprint | `06e668932827510a257440925bb63d9b9609a69d1f15389c16623e38bd377510` |
| Denial daemon image ID | `sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1` |
| Authorization source SHA-256 | `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4` |
| Authorization binary SHA-256 | `923dbc7fe663de0a65a3ee53dff51cb5b38c8a3f7d6f5c1ced5172bb5a725e05` |
| Daemon registration SHA-256 | `6e0d178070d292b1c616d775897fea5c94ae4ef60544b6841c65c9f0e36bd535` |
| Daemon control SHA-256 | `2570e8f89afc0cb3be2fcef5c4f8cf366322adc34fc9412b4b5d3dfd39df8ab2` |
| Installed worker __init__.py SHA-256 | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker server.py SHA-256 | `005eeeccd40ab0dc65a31281a72e3b353eae87cf62d42afb590b447faa3ba2bb` |
| Installed worker workload_http.py SHA-256 | `4bc49175b2c4bb0159a8c90481703f820380b02058f86ff4f49041f7afa7bb0f` |
| Installed worker workload_service.py SHA-256 | `3c44b867007dfc8fc026c0e094d106ed64174311c4a82fd4e61bca31a96ab34b` |

| Case | Result | Seconds | Private case evidence SHA-256 |
|---|---|---:|---|
| `control` | Passed; independent cleanup confirmed | 22.310 | `0f77bad78097a2657f95f23b79e1a0cbe3d7f4f9b8911954d605373fd83c64b4` |
| `gateway-pre_admission` | Passed; independent cleanup confirmed | 35.383 | `26040f4f4190a4dc77602be5af97717c2e21e855b83fba12f95e27168a9fd2ca` |
| `gateway-active` | Passed; independent cleanup confirmed | 38.741 | `ef6b6d6e6af3d2ea36b3eddf1d30b89225a2e2aa3cccc9e904a761d452d8e989` |
| `gateway-response` | Passed; independent cleanup confirmed | 34.866 | `fcc51e0dd67f2cd9c23b67065251dcf912b5e0ebbe9a25bb7384fee388d55b00` |
| `gateway-cleanup` | Passed; independent cleanup confirmed | 34.862 | `6c32bd6dd7f264c7a7b1aaec0be971238379ae20535b4e41afa98e1831058a09` |
| `client-pre_admission` | Passed; independent cleanup confirmed | 35.243 | `4d91d46256e336cb1b89fa27657de3998eb1d22041fa43d420555dda88a341c8` |
| `client-active` | Passed; independent cleanup confirmed | 34.500 | `9b5803bcfd54442fba024631f72d8d3942d0722ebb3d822aeb5ae9ee5b01a24e` |
| `client-response` | Passed; independent cleanup confirmed | 35.494 | `02ab08a7e0d163fa99a4927f4f22e1256ec7cd8eb0e398b976d601d48050f61f` |
| `client-cleanup` | Passed; independent cleanup confirmed | 36.595 | `afbf53a7296267ae37e733a7fa5cc7ce70772aee561f096ae6e77a20251c967d` |
| `worker-pre_admission` | Passed; independent cleanup confirmed | 40.034 | `e5a1d9001c5db153763b360c91d90c2eb65d2e2062a76844a4da511c16727000` |
| `worker-active` | Passed; independent cleanup confirmed | 40.986 | `cc15dcc7bd56e7c1bfdc52ea04d571762ef054634a4726211fea95c8bb9c049a` |
| `worker-response` | Passed; independent cleanup confirmed | 46.316 | `5897d3ac2d6e7a4062a9f615951620e1fa0afe5b1eea4459c44093dbda43013b` |
| `worker-cleanup` | Passed; independent cleanup confirmed | 46.909 | `c254d73bfab8344cd35894b522204f4c64096708ecf4188b5914c52a4f0bcff3` |
| `worker-unavailable` | Passed; independent cleanup confirmed | 42.496 | `05905158a55c2185007438b66fa4e5de6e9621aff452f2e224c467785531ff8e` |
| `worker-denied` | Passed; independent cleanup confirmed | 46.823 | `dc9bb8e7021b319aa5bc5a509a197b2c8568b072c7d2d7db2b693903cdd69384` |

Complete installed Python inventory for this candidate:

| Distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.22.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| certifi | `2026.10.23` |
| cffi | `2.1.1` |
| click | `8.5.0` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore | `1.0.9` |
| httpx | `0.28.1` |
| httpx-sse | `0.4.3` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.51.1` |
| maf-sandbox-bicep | `0.24.0` |
| maf-sandbox-docker | `0.29.0` |
| maf-sandbox-mcp | `0.2.0` |
| mcp | `1.30.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.2` |
| pydantic-settings | `2.16.2` |
| pydantic_core | `2.50.2` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.33` |
| referencing | `0.37.0` |
| regex | `2026.9.29` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.6.0` |
| starlette | `1.7.0` |
| typing-inspection | `0.4.4` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

This accepts the recorded deterministic-provider candidate and bounded operator-driven recovery only. Connection loss affects the replacement's private Docker context, and removal denial is an exact-target daemon authorization decision. Actual daemon or host loss, escaped process trees, model retry decisions, unattended watchdogs, newer dependencies and production remain unqualified. Only documentation follows the accepted source; recording this result is not another live run.

## Published candidate refresh (2026-11-07)

[#2542](https://github.com/sokolaidev/maf-extensions/issues/2542) selects a new published package candidate for the existing Docker/Bicep integration. PyPI metadata and a clean Windows/Python 3.13.12 installation confirm compatible requirements; `uv pip check` passed for all 38 installed distributions. This is installation evidence only. The fresh GitHub-hosted deterministic baseline, fifteen-case recovery campaign and real-model sample passed as recorded below.

| Component | Accepted Linux v4 candidate | Selected Linux v5 candidate | Relevant change |
|---|---|---|---|
| Published worker | 0.2.0 | 0.3.1 | MCP 2 transport adaptation and MAF 1.24 compatibility; existing bounded HTTP/service lifecycle |
| Core | 0.51.1 | 0.53.1 | MAF provenance/provider-lifetime adaptation, binary store support and refusal of inputs changed during reading |
| Bicep | 0.24.0 | 0.25.1 | MAF 1.24 compatibility and the core 0.53 dependency line |
| Docker backend | 0.29.0 | 0.30.6 | Core 0.53 compatibility and managed proxy dependency updates; this closed-network Bicep profile does not exercise credentialed proxy behavior |
| MAF Core | 1.22.0 | 1.24.0 | Installed provider/provenance contracts consumed by the published worker and kind |
| MCP SDK | 1.30.0 | 2.2.0 | Worker transport uses the MCP 2.2 API and refuses other SDK versions; published 2.3.0 is outside its exact requirement |

Package deltas are recorded in the [worker](../../../packages/maf-sandbox-mcp/CHANGELOG.md), [core](../../../packages/maf-sandbox/CHANGELOG.md), [Bicep](../../../packages/maf-sandbox-bicep/CHANGELOG.md) and [Docker](../../../packages/maf-sandbox-docker/CHANGELOG.md) release histories. Source/workspace compatibility does not establish behavior of the installed artifacts. No package requirement or generated release metadata is changed by this qualification refresh.

The [published worker wheel](https://files.pythonhosted.org/packages/b9/2f/7958be1bb8a8bcfdca8b14817f6ea8bc74a0e4bd432a24eb591c1c7a0a53/maf_sandbox_mcp-0.3.1-py3-none-any.whl) has verified SHA-256 `36d1af36ef6ed6fc174eeedc34a2c5f9a5e6a670134e0f6682d9c35955b079b1`. Both hosted profiles select that wheel and the same six principal package versions. The sample pins MCP explicitly; the recovery planner rejects both the historical 1.30 and newer unsupported 2.3 SDK. Other transitive packages resolve at installation and their complete inventories must be recorded and compared between hosted runs; the principal pins are not a full transitive lock.

OpenClaw 2026.9.7, the compiler image, the fifteen recovery cases and their evidence predicates stay fixed. Linux v5 distinguishes this published candidate from the historical v4 acceptance, which remains bound to its recorded source and packages. Real-model acceptance additionally requires actual upstream tool-result observations with reasoning disabled, both compiler verdicts and independent cleanup. Actual daemon/host loss, escaped process trees, model retries after injected failure, unattended watchdogs, newer candidates and production remain unqualified. No new workload tool is introduced, and no experiment JSON is retained.

### Initial hosted refresh results and harness compatibility

[Recovery run 42388068184, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42388068184) at `c32410c056753290104fef3248f121c5b7a06d5a` passed its fresh installed deterministic baseline, then refused the first control case with `parent_failure: ModuleNotFoundError`, unconfirmed cleanup and an incomplete case inventory. It remains unqualified. Retained Markdown SHA-256: `fc881a3b3389f78f715ca1176d2d43693fdf66a11a11ad820d6db33e59d0568b`. The bounded report cannot establish recovery or complete case cleanup.

A clean published-environment reproduction identifies the import: recovery reused `compiler_running` through the legacy MAF ownership fixture, which imports `httpx`; the MCP 2 candidate supplies `httpx2`. The unchanged standard-library process predicate now lives in the shared Gateway helper and both callers use it. This avoids adding a legacy HTTP client to the selected candidate. The regression fails before the move and passes afterward; the actual published environment also reaches the resource-setup boundary without importing the legacy fixture. A fresh complete recovery campaign is required.

The separate [real-model run 42388117416, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42388117416) passed at the same source `c32410c056753290104fef3248f121c5b7a06d5a`: six forwarded model responses, two observed Bicep results (one invalid and one valid), reasoning disabled, no provider errors, confirmed per-call cleanup, stopped processes, released owner lock and independently observed zero owned containers. Retained Markdown SHA-256: `39e97b0b9d59b9682afa1fbaa22da4299705ab92caad6eb66979f2f842f9e1b2`. This is model-path evidence for the selected published packages; it does not establish recovery. The subsequent import correction affects only recovery/legacy qualification fixtures, not the sample or published wheel.

## Accepted published candidate campaigns (2026-11-07)

[Recovery run 42388961604, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42388961604) passed a fresh installed deterministic baseline and all fifteen Linux v5 cases at `c8f541460f95a004000a75f3e3ccdda67e4327bf`. Independent collection returned `qualified: true` for campaign `5fe843b1bc91449e9d6786f034f91f22`, with cleanup confirmed for every case and no emergency cleanup required. Retained Markdown SHA-256: `d9d1b1d7ff292dacdfaebac4e419eebbf9d777ddf1607bc94b236e8fe31ecdcb`.

[Real-model run 42388117416, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/42388117416) passed at `c32410c056753290104fef3248f121c5b7a06d5a` with the same published wheel, worker module hashes, principal dependencies, Gateway version and compiler image/configuration. Retained Markdown SHA-256: `39e97b0b9d59b9682afa1fbaa22da4299705ab92caad6eb66979f2f842f9e1b2`. The later source change only removes the recovery harness's incidental legacy-client dependency; the model sample bytes are unchanged.

The model completed 6 requests and 6 forwarded responses, with 2 observed Bicep results, no provider errors and reasoning disabled. Both compiler verdicts and per-call cleanup were observed. Final checks confirmed stopped processes, owner-lock release and independently observed zero owned containers.

Recovery host: Ubuntu image `20261101.360.1`, Linux `7.0.0-1017-azure` / `x86_64`, Python `3.13.15`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)`, Docker `28.0.4` / API `1.48`. Model host: Ubuntu image `20261101.360.1`, `Linux-7.0.0-1017-azure-x86_64-with-glibc2.39`, `Python 3.13.15`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)`, Docker `28.0.4`. Recovery case execution total: 323.414 seconds, excluding setup and collection.

| Identity | Recorded value |
|---|---|
| Recovery source fingerprint | `550c78dd8132c26691153dead6c4433015c5a3ea0e0684c4668db6c13b346bf1` |
| Published worker SHA-256 | `36d1af36ef6ed6fc174eeedc34a2c5f9a5e6a670134e0f6682d9c35955b079b1` |
| Compiler image | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler configuration SHA-256 | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |
| Baseline report SHA-256 | `f61ed3de8bc04b75cbf3dc65623f461cbd8ef40ca46c4b7eb15c732e20fc6a5e` |
| Baseline tool evidence SHA-256 | `9aae2d1e282657460631b530051121c53614085b1c42d2e2923b5c7646126330` |
| npm lock SHA-256 | `6d37a9e295c8716bb5e5e31c0d43b436c8c3ef3c71bdb61e2c70dc4c9f2eebd7` |
| Recovery runtime fingerprint | `51ccf1bf16897962f1364a1ecc42fb713085a564c39858c219b53c67b2ab6175` |
| Model sample SHA-256 | `bc8392eeb0158cca6dc08896f5be358cf3e75a657b8d8c07f04e13df58f39bd6` |
| Model report SHA-256 | `877f4c5c92025dc4df2b1e676917910d33ad6511e17ae21e7452cb02e62ecb96` |
| Model tool evidence SHA-256 | `2201ae964819f5c1165d01c6e7cb9a2b509c4e68da35140949046e7c4d94d1bc` |
| Model endpoint SHA-256 | `308241e7111832947f4614c87b2bb7f8a135c63fbac6cf5fce09ffa744c635de` |
| Requested model SHA-256 | `9a7e60cd1c7bb9d5493832103af02aca05272564b29e49852a9a0b5d8d96ec2c` |
| Returned model SHA-256 | `b8be814ffed8fd2f3fc7c660d5f2038c33c02a1d0ed3a6a31e542bb97c23813a` |
| Installed worker __init__.py SHA-256 | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker server.py SHA-256 | `c69866ad7b3d35d7dc4fd1373493ccd404a1153101af45f72d7a6dcc1a31a89b` |
| Installed worker workload_http.py SHA-256 | `6d0047e30349d80ef076f34eef7172bf0f0e8ad07a00e6fae4f7aab8a6d57f07` |
| Installed worker workload_service.py SHA-256 | `f9cd38791fac1ed5254aac7bbb170a4a395d289712dd8468ac8a70c538b8ca91` |
| Denial fixture daemon_image | `sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1` |
| Denial fixture daemon_version | `29.8.2` |
| Denial fixture daemon_storage_driver | `overlay2` |
| Denial fixture go_version | `go version go1.27.3 linux/amd64` |
| Denial fixture plugin_source_sha256 | `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4` |
| Denial fixture plugin_binary_sha256 | `923dbc7fe663de0a65a3ee53dff51cb5b38c8a3f7d6f5c1ced5172bb5a725e05` |
| Denial fixture registration_sha256 | `9488961deed368a4e8dacbe3a717ecc01d65edb887f266dd4cde4345486981dd` |
| Denial fixture control_sha256 | `c5516acb6a02eacddb8f8a304223b58752c0c043b251056d5c58ff5178e7bb0c` |

| Case | Result | Seconds | Private evidence SHA-256 |
|---|---|---:|---|
| `control` | Passed; independent cleanup confirmed | 13.540 | `05b511ae2af9e4077f646a6ee119943a9751841282ce4fb8f39746058ce386b8` |
| `gateway-pre_admission` | Passed; independent cleanup confirmed | 20.267 | `a9c0e91e578803fb6c4206ea858cc84792383cae8e1b07eeef6ad12529f6b638` |
| `gateway-active` | Passed; independent cleanup confirmed | 21.383 | `6eeba45787d66dae03e20165966f6b6ae8c6217032a0451d4c20f54e13fe8554` |
| `gateway-response` | Passed; independent cleanup confirmed | 19.950 | `06d0c88b8767a193af186758fb218de2d01ee5b717d4dca62367cc7908be384d` |
| `gateway-cleanup` | Passed; independent cleanup confirmed | 20.544 | `8dddc9587e3a770f698b6719cd3d44531672bf11d4ff727e604bc8fa0b27e242` |
| `client-pre_admission` | Passed; independent cleanup confirmed | 17.351 | `ff9b7602ad143ea69cd36a7b1010516b1371063875e6c8048d93eaaacc3a2520` |
| `client-active` | Passed; independent cleanup confirmed | 19.982 | `1914f83ff789ae16e2bb519c8d1fd119584b93f91c4504150d0bff1229e8c799` |
| `client-response` | Passed; independent cleanup confirmed | 20.948 | `a7a577543dc549f9cbc55a907be37fe929724a62b16128b6ea09c77705424b30` |
| `client-cleanup` | Passed; independent cleanup confirmed | 20.956 | `0c5b00a58f8e2328d5f70f4f34844732005cde34a93e2e914db299301a29d523` |
| `worker-pre_admission` | Passed; independent cleanup confirmed | 22.394 | `b219fdb802aab7bd860e1aa3b74c5922d1e7936ad09808400a4f7ad2a19a2783` |
| `worker-active` | Passed; independent cleanup confirmed | 23.066 | `02e75a055528942fd8c4d6d8d3243734fed419cc5d53dfb316925e7c996ee62c` |
| `worker-response` | Passed; independent cleanup confirmed | 26.200 | `ed9b3d185b28e3312306a622ee096368386706872c594422f7a76167a4687971` |
| `worker-cleanup` | Passed; independent cleanup confirmed | 25.869 | `5f5253bab88ef8e19e0b5fe7f66be66a4901e1c7da04a18e570cb3ef687c5fec` |
| `worker-unavailable` | Passed; independent cleanup confirmed | 24.033 | `60ddde5a40f87cd9a2847d45feed5c48a26cf24a25c3b7d88eb07e7a12dbb3f6` |
| `worker-denied` | Passed; independent cleanup confirmed | 26.932 | `8c5abad4a61a0c9937cb96433fabc461adc238ee9a95d5bf2fc296552e7e70e6` |

Complete installed Python inventories are compared below. Matching version strings do not imply byte-identical environments: the recovery runtime and worker identities above retain their separate evidence.

| Distribution | Recovery | Model |
|---|---|---|
| PyJWT | `2.15.1` | `2.15.1` |
| PyYAML | `6.0.3` | `6.0.3` |
| agent-framework-core | `1.24.0` | `1.24.0` |
| annotated-types | `0.8.0` | `0.8.0` |
| anyio | `4.16.0` | `4.16.0` |
| attrs | `26.1.0` | `26.1.0` |
| cffi | `2.1.1` | `2.1.1` |
| click | `8.5.1` | `8.5.1` |
| cryptography | `50.0.2` | `50.0.2` |
| h11 | `0.16.0` | `0.16.0` |
| httpcore2 | `2.16.0` | `2.16.0` |
| httpx2 | `2.16.0` | `2.16.0` |
| idna | `3.20` | `3.20` |
| jsonschema | `4.26.0` | `4.26.0` |
| jsonschema-specifications | `2025.9.1` | `2025.9.1` |
| maf-sandbox | `0.53.1` | `0.53.1` |
| maf-sandbox-bicep | `0.25.1` | `0.25.1` |
| maf-sandbox-docker | `0.30.6` | `0.30.6` |
| maf-sandbox-mcp | `0.3.1` | `0.3.1` |
| mcp | `2.2.0` | `2.2.0` |
| mcp-types | `2.2.0` | `2.2.0` |
| msgspec | `0.22.0` | `0.22.0` |
| opentelemetry-api | `1.45.2` | `1.45.2` |
| pycparser | `3.12` | `3.12` |
| pydantic | `2.14.3` | `2.14.3` |
| pydantic_core | `2.50.4` | `2.50.4` |
| python-dotenv | `1.2.4` | `1.2.4` |
| python-multipart | `0.0.33` | `0.0.33` |
| referencing | `0.37.0` | `0.37.0` |
| regex | `2026.11.7` | `2026.11.7` |
| rpds-py | `2026.9.1` | `2026.9.1` |
| sse-starlette | `3.7.0` | `3.7.0` |
| starlette | `1.7.0` | `1.7.0` |
| truststore | `0.10.5` | `0.10.5` |
| typing-inspection | `0.4.5` | `0.4.5` |
| typing_extensions | `4.16.0` | `4.16.0` |
| uvicorn | `0.54.0` | `0.54.0` |

This accepts the selected published candidate for the recorded Linux deterministic recovery profile and separately observed real-model Bicep path. It does not qualify model decisions after injected failures, actual daemon/host loss, escaped process trees, unattended watchdogs, newer package sets or production. Historical campaigns remain unchanged; the failed initial recovery run is not counted toward acceptance. Only a workflow pin assertion and documentation follow the final recovery source.

## Numbered published sample smoke (2026-11-11)

[#2657](https://github.com/sokolaidev/maf-extensions/issues/2657) adds [sample 24](../../../samples/24_openclaw_bicep/), a numbered launcher that downloads and verifies the published worker, pulls the pinned compiler image, and calls the existing Gateway harness in the same process. The worker installs into a private environment from its wheel and published dependencies; no worker is built from the checkout. The launcher uses only standard-library dependencies and the canonical sample scaffold. Keep the full checkout for the shared qualification harness.

[GitHub-hosted run 43108932108, attempt 1](https://github.com/sokolaidev/maf-extensions/actions/runs/43108932108) passed at `f3ba1f3d2333ffd80736f53574164c7f7feff3fd` through the numbered entry point. The collector returned `qualified: true` for `published-smoke-v1`: one invalid result and one valid result, one call each, confirmed per-call cleanup, stopped service processes, a released owner lock and independently observed zero owned containers. The provider was deterministic (`actual_model: false`); no model account or inference was used. Retained Markdown SHA-256: `6e2bcac0e955d020d8c74d6a263ffe662994ef309d9ebe3311754b2538289db8`.

The host was Ubuntu image `20261101.360.1`, `Linux-7.0.0-1017-azure-x86_64-with-glibc2.39`, Docker `28.0.4` / API `1.48`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and worker `Python 3.13.15`. The fixed principal versions and worker module hashes match the 2026-11-07 candidate. Transitive resolution changed `regex` from `2026.11.7` to `2026.11.9`; the full installed inventory below records this smoke candidate rather than assuming a transitive lock.

| Identity | SHA-256 or immutable reference |
|---|---|
| Launcher | `423ad7e07f012ccd38359104e1c6cf4acfeb01e3023d4b052f4c4c8c1d5b91ca` |
| Shared harness | `bc8392eeb0158cca6dc08896f5be358cf3e75a657b8d8c07f04e13df58f39bd6` |
| Private report | `dfabd1e039bfa0180d3be844bf432ccb1195cc5a525a7af39144b3286b99a498` |
| Private tool evidence | `812e41eaa778b2ad0d545da6c6f22dee386586e596f8ee77e1e55e458603f892` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Compiler registry reference | `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e` |
| Published worker wheel | `36d1af36ef6ed6fc174eeedc34a2c5f9a5e6a670134e0f6682d9c35955b079b1` |
| Installed worker __init__.py | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| Installed worker server.py | `c69866ad7b3d35d7dc4fd1373493ccd404a1153101af45f72d7a6dcc1a31a89b` |
| Installed worker workload_http.py | `6d0047e30349d80ef076f34eef7172bf0f0e8ad07a00e6fae4f7aab8a6d57f07` |
| Installed worker workload_service.py | `f9cd38791fac1ed5254aac7bbb170a4a395d289712dd8468ac8a70c538b8ca91` |
| invalid submitted source | `91285d8d03f8c76ee86fddeab9f039132ebc75ddcc0f5dbd3a0f5e4bd7b4b0cf` |
| valid submitted source | `25a645af6181b25dc4727c19663a910c8c45d8ca84bf4db34548c16c11c301c7` |
| Compiler configuration | `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |

| Installed distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.3` |
| agent-framework-core | `1.24.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| cffi | `2.1.1` |
| click | `8.5.1` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore2 | `2.16.0` |
| httpx2 | `2.16.0` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.53.1` |
| maf-sandbox-bicep | `0.25.1` |
| maf-sandbox-docker | `0.30.6` |
| maf-sandbox-mcp | `0.3.1` |
| mcp | `2.2.0` |
| mcp-types | `2.2.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.3` |
| pydantic_core | `2.50.4` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.33` |
| referencing | `0.37.0` |
| regex | `2026.11.9` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.7.0` |
| starlette | `1.7.0` |
| truststore | `0.10.5` |
| typing-inspection | `0.4.5` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

The deterministic published profile now runs on main pushes, the daily Docker-live schedule and ordinary manual dispatches; explicit model and owner-loss selections retain their separate opt-in jobs. The retained run proves the new entry point on this hosted candidate. Optional model mode is documented and its routing is tested offline, but was not rerun through sample 24. The fifteen-case recovery campaign was not rerun against this transitive resolution. Production operation, other hosts/models, unattended recovery and actual daemon/host loss remain unqualified. Only bounded Markdown is retained; private state and JSON reports stay outside the checkout.

## Newer Gateway Docker assessment (2026-11-27)

[#3005](https://github.com/sokolaidev/maf-extensions/issues/3005) continues the Docker workstream while #2104 waits upstream. At the start of this assessment, the numbered sample remained on OpenClaw 2026.9.7. This assessment selects npm `openclaw@2026.11.10` and holds the published worker, principal Python package pins and compiler image constant; transitive Python resolution is recorded rather than assumed identical. It does not add workload tools, credential delegation or a full sandbox backend.

### Candidate and compatibility findings

The [2026.11.10 release](https://github.com/openclaw/openclaw/releases/tag/v2026.11.10) declares Node `>=24.16.0 <25 || >=26.1.0`; the existing Node 26.8.1 runner satisfies that range. The exact npm tarball is `https://registry.npmjs.org/openclaw/-/openclaw-2026.11.10.tgz`, with integrity `sha512-/FKDjxY1ZdUSXHx1a9b4EUpPRZywM3uv4181g22XImJfp/5+h+fFUZ7B7HthNrdYzq//05zH0Yf7mefzIlWFaQ==`. Installation keeps the sample's existing `--ignore-scripts` followed by the explicit package-local postinstall. Installed package metadata and the npm lock entry are checked separately from the registry selection.

The assessment identified four fixture/configuration differences:

1. The new Gateway refuses the sample's `agents.defaults.heartbeat` key. Its [heartbeat migration documentation](https://github.com/openclaw/openclaw/blob/v2026.11.10/docs/gateway/heartbeat.md) moves heartbeat to automation jobs. This sample starts with fresh private state, so the candidate removes that obsolete setting; it does not migrate an operator's existing jobs or configuration.
2. The Gateway appends a user-role runtime-context envelope to the model request. Choosing the last user message therefore loses the sample turn marker, and checking only the last message's role misses the preceding tool result. The [runtime-context producer](https://github.com/openclaw/openclaw/blob/v2026.11.10/src/agents/embedded-agent-runner/run/runtime-context-prompt.ts) and [envelope definition](https://github.com/openclaw/openclaw/blob/v2026.11.10/packages/llm-core/src/types.ts) establish the selected version's shape. The provider now excludes that exact closed envelope from its fixture accounting view. It preserves the original messages when forwarding to a real model, refuses malformed envelopes or envelopes containing a sample turn marker, and still refuses ordinary unmarked user requests. This is accounting for the sample's owned prompts, not authentication of message provenance.

3. The model-facing dispatcher is now `dispatch_action`, documented in the tagged [Tool Search contract](https://github.com/openclaw/openclaw/blob/v2026.11.10/docs/tools/tool-search.md). A diagnostic run observed it in the offered tools and returned assistant history; the old observer recognized only `tool_call` and therefore recorded no Bicep result. The fixture now selects the advertised dispatcher and observes both names, while keeping the exact tool identity, source/config/image digests, verdict and cleanup requirements unchanged.

4. `dispatch_action` no longer repeats the target's `details` in model-facing text. Its content still contains the structured JSON projected by [the MCP result adapter](https://github.com/openclaw/openclaw/blob/v2026.11.10/src/agents/mcp-content.ts). The checker reads that exact `structuredContent:` block when `details` is absent; malformed JSON, prose and non-object payloads are refused. If `details` is present it remains authoritative and cannot be bypassed with a different content block. All existing source, compiler, completion, verdict and cleanup predicates still apply.

| Attempt | Observed outcome |
|---|---|
| [Main baseline](https://github.com/sokolaidev/maf-extensions/actions/runs/45913393206), `4b5c2efc9c5c6eea3ae6594bfc6d21269378d20f` | OpenClaw 2026.9.7 passed the existing deterministic invalid/valid Docker sample and independent cleanup collector. |
| [Version-only candidate](https://github.com/sokolaidev/maf-extensions/actions/runs/45919645274), `4ab5007bc883e74b0953b6947e437adcf28bfb65` | Startup failed; no tool result. A subsequent bounded diagnostic run identified the rejected heartbeat key. Cleanup stopped the owned processes and independently found zero owned containers. |
| [Fresh-state config correction](https://github.com/sokolaidev/maf-extensions/actions/runs/45920375668), `7b8b0141e9b63d725d2a6694e8298d85b5cecf12` | Gateway started, but the first turn failed. Subsequent bounded provider diagnostics identified seven missing-turn errors and zero calls/results; message-shape inspection identified the extra user-role context. Cleanup remained confirmed. |

[Dispatcher diagnostic run 45922929686](https://github.com/sokolaidev/maf-extensions/actions/runs/45922929686) observed the offered `dispatch_action`, `tool_describe` and `tool_search` names and a returned `dispatch_action` call. [Run 45923298765](https://github.com/sokolaidev/maf-extensions/actions/runs/45923298765) then recorded a Bicep result but refused it with `KeyError`; [bounded projection inspection](https://github.com/sokolaidev/maf-extensions/actions/runs/45923599421) confirmed a `{tool, result: {content}}` envelope without `details`. These failed candidates are not counted as acceptance.

The diagnostic reports expose fixed error categories, message roles and marker presence, not prompts, responses, tokens or private logs. Initial proof-job setup failures (missing PyYAML, an unbounded Ruff install and formatting) are not candidate acceptance runs. The final proof uses the repository's Ruff range and the required test dependencies. A shallow-checkout error also prevented the first comparison/gate jobs from restoring the baseline; the corrected jobs fetch that commit explicitly.


### Hosted identities and retained observations

The [2026.9.7 comparison job](https://github.com/sokolaidev/maf-extensions/actions/runs/45923801974/job/140260401977) and [2026.11.10 candidate job](https://github.com/sokolaidev/maf-extensions/actions/runs/45923801974/job/140260401608) both passed `published-smoke-v1` at proof source `25e662917a7ea2d4f34fe7bb54527a343c00042a`. Each produced an invalid result then a valid result, one call per turn, expected source/config/image identities and confirmed per-call cleanup. Both stopped the owned processes, released the owner lock and independently observed zero owned containers, with zero cleanup errors. The provider was deterministic; no external model was used. Each focused run passed 86 tests with two Windows-only skips, lint and formatting.

The comparison restores the unchanged 2026.9.7 harness before execution. Both jobs use the same final provider, evidence checker and tests. The 2026.11.10 candidate alone changes the Gateway version and removes the retired heartbeat setting. Only the shared compatibility changes and this record enter the delivery branch.

Both jobs used Ubuntu runner image `20261122.23.1`, `Linux-7.0.0-1020-azure-x86_64-with-glibc2.39`, Docker `28.0.4` / API `1.48`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and worker `Python 3.13.15`. The installed npm candidate lock SHA-256 was `6dde493346e165673622de3aba63a65058e9f5ab6889aa8f7f3afa6b137c1191`; its package version, resolved URL and integrity matched the registry selection.

| Identity | 2026.9.7 comparison | 2026.11.10 candidate |
|---|---|---|
| Shared harness SHA-256 | `bc8392eeb0158cca6dc08896f5be358cf3e75a657b8d8c07f04e13df58f39bd6` | `270b1e6edae08bd322e17ae14d43f3851095e56eb82266fa643048073d8ece26` |
| Private report SHA-256 | `9f898fef5797410a1d5765ad9247a1a058504c7d65882619a6ea6a51bd63a683` | `caef47c623c3a6acf480b9c165e2184806dd1728756276ad6888feb75bffdf27` |
| Private tool evidence SHA-256 | `74d2c97eba493875af5200bb318f13db1eb5888dae64f30310dae51aeba52ec0` | `e59944452dc84434a47e21f6b0162858859f77c654d3b8c7a40bfb4fdaf90138` |
| Compiler image ID | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` | `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3` |
| Published worker wheel SHA-256 | `36d1af36ef6ed6fc174eeedc34a2c5f9a5e6a670134e0f6682d9c35955b079b1` | Same |
| Launcher SHA-256 | `423ad7e07f012ccd38359104e1c6cf4acfeb01e3023d4b052f4c4c8c1d5b91ca` | Same |

The compiler registry reference remains `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e`. Installed worker module hashes match the numbered published-sample receipt above. Both jobs resolved the following complete Python inventory; matching versions are not a claim that the whole environments are byte-identical.

| Installed distribution | Version |
|---|---|
| PyJWT | `2.15.1` |
| PyYAML | `6.0.4` |
| agent-framework-core | `1.24.0` |
| annotated-types | `0.8.0` |
| anyio | `4.16.0` |
| attrs | `26.1.0` |
| cffi | `2.1.1` |
| click | `8.5.1` |
| cryptography | `50.0.2` |
| h11 | `0.16.0` |
| httpcore2 | `2.16.2` |
| httpx2 | `2.16.2` |
| idna | `3.20` |
| jsonschema | `4.26.0` |
| jsonschema-specifications | `2025.9.1` |
| maf-sandbox | `0.53.1` |
| maf-sandbox-bicep | `0.25.1` |
| maf-sandbox-docker | `0.30.6` |
| maf-sandbox-mcp | `0.3.1` |
| mcp | `2.2.0` |
| mcp-types | `2.2.0` |
| msgspec | `0.22.0` |
| opentelemetry-api | `1.45.2` |
| pycparser | `3.12` |
| pydantic | `2.14.4` |
| pydantic_core | `2.50.5` |
| python-dotenv | `1.2.4` |
| python-multipart | `0.0.34` |
| referencing | `0.37.0` |
| regex | `2026.11.25` |
| rpds-py | `2026.9.1` |
| sse-starlette | `3.7.1` |
| starlette | `1.7.0` |
| truststore | `0.10.5` |
| typing-inspection | `0.4.5` |
| typing_extensions | `4.16.0` |
| uvicorn | `0.54.0` |

Only bounded Markdown artifacts are retained by the proof workflow. Private state, reports and logs remain in the runner's temporary directory. The proof workflow and diagnostic edits are excluded from the delivery branch.

### Reproduction and promotion boundary

Use the numbered [sample 24](../../../samples/24_openclaw_bicep/README.md) on a native Linux Docker host with the fixture fix in this change. In a disposable checkout only, set `OPENCLAW = "2026.11.10"` in the shared harness and remove `"heartbeat": {"every": "0m"}` from its fresh Gateway configuration. Invoke the normal deterministic numbered sample with an output directory outside the checkout, then run `scripts/report_openclaw_hosted.py --published` with the same runner environment. The proof commit and run above retain the exact temporary candidate configuration; that workflow and the version/config changes are not part of the default sample.

At this assessment's completion, [follow-up #3009](https://github.com/sokolaidev/maf-extensions/issues/3009) owned the separate 2026.11.10 real-model run and bounded owner-loss campaign, with the default held at 2026.9.7. The [subsequent qualification below](#newer-gateway-model-and-recovery-qualification-2026-11-27) records their outcomes and promotion. Historical model/recovery acceptance does not transfer across this Gateway change or new transitive resolutions. No native MXC behavior, unattended operation, daemon/host loss, prompt-injection resistance or production support is established here.

## Newer Gateway model and recovery qualification (2026-11-27)

[#3009](https://github.com/sokolaidev/maf-extensions/issues/3009) evaluates OpenClaw 2026.11.10 with the published worker 0.3.1, the six principal Python pins and immutable compiler from the assessment above. Fresh Gateway configuration omits the retired heartbeat setting. The model and recovery profiles remain separate; neither proves a model retry after an injected fault.

### Fresh baseline and real-model execution

[Run 46016688274](https://github.com/sokolaidev/maf-extensions/actions/runs/46016688274), attempt 1, passed both the numbered deterministic sample and the real-model profile at source `0865a6e4c2b557056ef9f72a59db96ab2d1022db`. Each produced the expected invalid then valid Bicep result, one call per turn, matching source/configuration/compiler identities, confirmed per-call cleanup, stopped service processes, a released owner lock and independently observed zero owned containers.

The model profile used the existing live-verify Azure OpenAI configuration with reasoning disabled. It forwarded all six requests, observed two Bicep results through canonical identifiers, recorded two `dispatch_action` calls and two `tool_describe` calls, and reported no provider errors. Endpoint and model identities are hashed; credentials, prompts and responses are not published.

Both runs used Ubuntu image `20261122.23.1`, `Linux-7.0.0-1020-azure-x86_64-with-glibc2.39`, Docker `28.0.4` / API `1.48`, Node `v26.8.1`, `uv 0.12.5 (x86_64-unknown-linux-gnu)` and worker `Python 3.13.15`. The complete 37-distribution inventory and four installed worker hashes exactly match the newer Gateway assessment above; matching versions are not a claim of byte-identical full environments.

| Identity | SHA-256 |
|---|---|
| Shared harness | `270b1e6edae08bd322e17ae14d43f3851095e56eb82266fa643048073d8ece26` |
| Deterministic report | `caef47c623c3a6acf480b9c165e2184806dd1728756276ad6888feb75bffdf27` |
| Deterministic tool evidence | `b901cb847aa3c9167ff610e0dccad819086bebbe68595812d60089d1bcf51259` |
| Model report | `747c79bec49a07c2e530205ef72d43bf335a5bfdc10df6dec5e8438adf5a3b4d` |
| Model tool evidence | `33c4dd7378cece8eb8d471e02f6fc15a53964a60ce14ab0ef91a88e6dd2811cf` |
| Model endpoint | `308241e7111832947f4614c87b2bb7f8a135c63fbac6cf5fce09ffa744c635de` |
| Requested model | `9a7e60cd1c7bb9d5493832103af02aca05272564b29e49852a9a0b5d8d96ec2c` |
| Returned model | `b8be814ffed8fd2f3fc7c660d5f2038c33c02a1d0ed3a6a31e542bb97c23813a` |

### Recovery preparation and rejected attempts

The first recovery attempt in run 46016688274 passed its fresh deterministic baseline but stopped in the control case with `parent_failure: FileNotFoundError`, unconfirmed independent cleanup and an incomplete case inventory. The bounded report does not establish recovery or complete case cleanup. Its installed npm lock SHA-256 was `6dde493346e165673622de3aba63a65058e9f5ab6889aa8f7f3afa6b137c1191`, and its published worker, transitive inventory and compiler identities matched the model run.

The recovery campaign has its own deterministic provider and projection checker. Those fixtures now recognize the closed runtime-context envelope, select the advertised dispatcher and accept the exact structured-text projection when details are absent. Present details remain authoritative, and source, configuration, image, completion, verdict and cleanup checks remain mandatory. The earlier model sample bytes and published wheel are unchanged by these fixture corrections.

Proof setup attempts 46017169529 and 46017360910 did not execute a recovery campaign: focused pytest first lacked the fixture import path, then a job-wide import-path setting disrupted type resolution. The corrected workflow scopes that setting to pytest. These setup failures are not candidate acceptance evidence.

### Accepted recovery campaign and promotion

[Run 46017844621](https://github.com/sokolaidev/maf-extensions/actions/runs/46017844621), attempt 1, passed a fresh installed baseline and all fifteen Linux v5 cases at source `95bd9fb24f9fa24bb7d9c33d0379c1645c5fd7dc`. Independent collection returned `qualified: true` for campaign `de5f1433d834493197fca4a98c75a518`. Every case confirmed cleanup without emergency removal, preserved the unrelated-owner sentinel until its explicit teardown, and passed the unchanged fault-specific evaluator. Case execution total was 452.686 seconds, excluding setup and collection.

The recovery candidate matches the model run's complete Python distribution inventory, installed worker module hashes, published wheel, Gateway version and compiler/configuration identities. Its fresh runtime inventory contains 45908 files and remained unchanged through the campaign. The qualification-source difference from the model run is confined to recovery fixtures and their version assertion; the shared model harness and provider bytes are unchanged.

| Recovery identity | SHA-256 |
|---|---|
| Runner source fingerprint | `d2328971d140dfd302c31827fe0efc17b498c58db98610492ba2d60575e07181` |
| Installed runtime fingerprint | `d84bf418f4b950f804939b44710da0ff391a4b9520fb179e851c80026d3f67f0` |
| npm lock | `6dde493346e165673622de3aba63a65058e9f5ab6889aa8f7f3afa6b137c1191` |
| Baseline report | `caef47c623c3a6acf480b9c165e2184806dd1728756276ad6888feb75bffdf27` |
| Baseline tool evidence | `067356df57b5f64aa7cf281653401ca61b2fd3b4424de642a8612f12d22c2f64` |
| Retained Markdown artifact ZIP | `ce2262b50dd58c889bd531bfa063faf15836539748e7c468276130f762db91b4` |

| Case | Seconds | Independent result |
|---|---:|---|
| `control` | 19.050 | Passed; cleanup confirmed |
| `gateway-pre_admission` | 28.628 | Passed; cleanup confirmed |
| `gateway-active` | 30.298 | Passed; cleanup confirmed |
| `gateway-response` | 27.997 | Passed; cleanup confirmed |
| `gateway-cleanup` | 27.705 | Passed; cleanup confirmed |
| `client-pre_admission` | 23.855 | Passed; cleanup confirmed |
| `client-active` | 28.089 | Passed; cleanup confirmed |
| `client-response` | 29.037 | Passed; cleanup confirmed |
| `client-cleanup` | 28.823 | Passed; cleanup confirmed |
| `worker-pre_admission` | 32.018 | Passed; cleanup confirmed |
| `worker-active` | 33.410 | Passed; cleanup confirmed |
| `worker-response` | 37.458 | Passed; cleanup confirmed |
| `worker-cleanup` | 35.733 | Passed; cleanup confirmed |
| `worker-unavailable` | 33.924 | Passed; cleanup confirmed |
| `worker-denied` | 36.659 | Passed; cleanup confirmed |

The removal-denied case used the existing private TLS daemon image `sha256:98175203ef6f7a6147c3edbff4f2606d2707b413a61b45fed021004ddbf0ebd1`, Docker `29.8.2` with `overlay2`, and `go version go1.27.3 linux/amd64`. The daemon remained unchanged. Its authorization-plugin source SHA-256 is `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4` and compiled binary SHA-256 is `923dbc7fe663de0a65a3ee53dff51cb5b38c8a3f7d6f5c1ced5172bb5a725e05`. The unavailable case still covers loss of the replacement worker's Docker connection; neither case establishes actual host or daemon loss.

These selected hosted results support promoting the shared sample and Linux recovery assertion to 2026.11.10. The numbered sample inherits that pin. Historical Windows conformance, credential delegation, native MXC, additional workload tools, model retries after faults, unattended recovery and production operation gain no new qualification. Only bounded Markdown artifacts are retained; private JSON, logs, tokens and sessions remain outside the checkout. Temporary proof workflows are excluded from the delivery.

## Real-model worker restart — 2026-11-28

[#3024](https://github.com/sokolaidev/maf-extensions/issues/3024) combines the real-model path with one controlled pre-admission worker outage. The harness stops the installed worker after observing the model's first Bicep selection but before releasing that response to Gateway. It restarts only after receiving Gateway's explicit failed tool result, then forwards that unchanged result to the model. The original prompt, Gateway process and user session remain unchanged. A same-turn retry must use the identical source; the second turn still repairs and validates the greeting declaration.

The attempt budget is one failed Bicep selection followed by exactly one invalid execution and one valid repair. Separate Docker create/destroy observations must account for two distinct containers created after restart readiness, with no extra execution and no retained owned resources. Missing failure/retry evidence, source changes, duplicate identities, exceeded attempts or incomplete cleanup refuse acceptance. The provider's ordinary 32-request/4,096-token limits and hosted 18-minute sample deadline remain in force.

The worker is restarted by the qualification harness. This profile does not cover active-compiler termination, ambiguous-result replay, automatic supervision, host or daemon loss, other providers/models or production operation. Existing deterministic actor-loss evidence remains separate. Worker 0.3.1, the principal dependency pins and compiler image remain unchanged; no additional workload tool or MXC work is included.

Live qualification is pending.
