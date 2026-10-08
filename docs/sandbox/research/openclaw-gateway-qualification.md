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

| Work | State | Tracking |
|---|---|---|
| Bicep host qualification | One-session stdio and bounded two-session HTTP Gateway paths qualified with a deterministic provider; actual LLM behavior and full crash recovery remain unqualified | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| Multiple Gateway sessions | Bounded HTTP outcomes, reload/restart, withheld results, selected crash/cleanup refusal, selective deletion and eight-slot/three-cycle churn qualified on recorded candidates. Immediate discovery refusal follows capacity overflow; after cooldown, a new turn refreshes discovery and subsequent new work succeeds without restart or replay, preserving seven other sessions. Startup-unavailability discovery recovery, actual Docker cleanup failures, other retirement triggers, idle expiry, prolonged/concurrent churn and the remaining matrix stay open | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (closed) by [#1677](https://github.com/sokolaidev/maf-extensions/pull/1677) (merged); [#1675](https://github.com/sokolaidev/maf-extensions/issues/1675) (closed) by [#1680](https://github.com/sokolaidev/maf-extensions/pull/1680) (merged); [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) (open) |


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
