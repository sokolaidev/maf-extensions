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

The threshold was restored to 900 seconds when B's retirement completed. The observer does not edit `touched` timestamps or call retirement itself; it observes the real sweeper's call and the returned task. The ages above show that elapsed idle age alone did not retire A: its active work kept the registration alive. The two-second threshold makes this an accelerated isolation/retirement check, not a measurement of the default 15-minute policy, a latency SLA, or exhaustive coverage of expiry/admission races.

The observed 404s belong to stale transport GET and DELETE requests, not a workload whose outcome became unknown. Fresh work succeeds after the client establishes a new MCP identity. The checker explicitly aborts A after observing expiry and does not automatically retry that aborted workload. No actual-model retry behavior is claimed.

All eight executed Python source hashes match Git bytes at the candidate. The changed lifecycle checker SHA-256 is `d6e56204e78f70e2a0eb1e4dc312312e12948f7d252ac3c15be392bed73fc2c0`; the observer SHA-256 is `0cf1613ed77a3dea730b9e302091952d701fe869a6655d59452b2c44a1ad63f6`. The remaining six source hashes retain their preceding recorded identities. The focused evidence suite passed 551 tests; fixture type checks reported zero errors and one existing optional-process warning. Negative cases reject non-sweeper retirement, insufficient elapsed age, incomplete SDK draining, lost active state, unexpected client traffic and missing independent container/process/owner observations.

Reproduce with the existing lifecycle command plus `--case idle` and a fresh private root. Retain `idle-report.json`, `idle-expiry.json` and private provider/transport/Gateway logs; no credentials, private paths or raw identities are committed. The startup, registry and combined lifecycle modes and separate live MAF/stdio cases were not rerun. There was no failed live attempt for this candidate. Production-duration idle expiry, broader timing races, other retirement triggers, actual Docker cleanup/daemon failures, prolonged/concurrent churn, repeated discovery failure/backoff and remaining transport/MAF combinations stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). The report's broad remaining-idle-expiry entry includes this unmeasured production-duration boundary. No host-crash, watchdog or unattended-operation claim is made.

## Default 15-minute idle expiry: bounded follow-up (2026-10-09)

The independent `--case idle-default` keeps the service's 900-second idle threshold and all session timestamps unchanged. After two logical Gateway sessions complete valid calls, the observer watches the real sweeper while the checker submits seven new calls through A at two-minute intervals and leaves B without new work. Before admitting a result, evidence must show B's measured idle age reached at least 900 seconds and the observation elapsed for the remaining interval measured when the observer was armed. All seven waiting-period calls and binding executions must belong to A, with no client-directed deletion or extra initialization. Late cancellation notifications for either known session are counted separately and require a correlated accepted response and settlement; their target must not match any workload call observed during the wait. Such transport activity can refresh a session's idle clock without running a workload.

B must fully drain its SDK/transport/request state and adapter registration, leaving A's unchanged callable registration. A must then complete new work with its existing MCP identity. B must receive stale-transport refusal, initialize once, complete new work with a fresh identity and reuse that identity on another call. The case expects twelve tool dispatches, unchanged Gateway/service processes and owner bytes, preservation of the unrelated sentinel, final registry/SDK agreement and service exit zero. It writes `default-idle-report.json` and `default-idle-expiry.json`, retaining `complete_matrix: false`. This case measures the default elapsed interval with A periodically used; the accelerated case separately covers expiry during another session's active compiler. Broader expiry/admission races remain outside this case. Candidate-specific live evidence is recorded below.


The first default-duration attempt on candidate `50bb21f8` observed B's sweeper retirement at an idle age of about 900.75 seconds, with complete SDK/adapter drain. The checker nevertheless refused a success report: it had assumed that every MCP message during the wait was one of A's seven workload calls, while Gateway sent one late cancellation notification for each session that the service accepted with HTTP 202. Neither target matched a recorded workload request; B's control request refreshed its idle timestamp. No B workload ran. The corrected checker distinguishes accepted, correlated control traffic for known sessions from new work, cancellation of an observed workload call and client-directed retirement. Independent cleanup inspection found zero owned containers or fixture processes after the failed attempt. That partial observation does not establish reconnection or final success; the fresh complete run below supplies that evidence.


A retry on `cda1af17` was deliberately stopped early when a complete audit of the first trace identified the second session's control notification as well. That intermediate validator had unnecessarily restricted harmless control traffic to B. The final validator checks the target against every recorded workload call in the wait, while requiring the same known session, exchange, accepted response and ordered settlement. The interrupted retry is not qualification evidence and produced no success report.


## Default 15-minute idle expiry: live result (2026-10-09)

The independent `--case idle-default` passed on clean candidate `d34736448e8c99a5f73ace5b34cf1b686f67f455`. It retained OpenClaw 2026.9.7, published core 0.46.0 / Bicep 0.22.0 / Docker 0.24.4, MCP 1.28.1, MAF core 1.19.0 and Uvicorn 0.54.0. Docker server 29.8.2 used the prepared image and compiler configuration recorded in the accelerated result above; both identities were rechecked. Production service/binding files and installed Gateway code were unchanged. The observer did not write the idle threshold or session timestamps.

`default-idle-report.json` SHA-256 is `0b11942e6a84885d4cb55683faf566fa577a35272123a6f8f5a24c588e2cc699`; `default-idle-expiry.json` SHA-256 is `9bea372077233125ca87743e64f6b2faa0a0d55b051561c7fa99378295bea2de`. The report records twelve tool dispatches and retains `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Baseline | A and B completed valid calls on distinct MCP identities with exactly two SDK tasks and adapter registrations |
| Waiting period | Seven new A calls at two-minute intervals succeeded on A's original identity. B received no new workload. Two late cancellation controls were correlated with accepted HTTP 202 responses and settlement; neither targeted an observed waiting-period workload call |
| Default timer | The unchanged threshold was 900 seconds. The real sweeper retired B at a measured idle age of 900.707369 seconds, 947.7280071 seconds after the observer was armed |
| Retired B | Its SDK task finished, transport terminated and streams/request-ID/request counts reached zero. Its adapter registration was absent, leaving only A with matching SDK/adapter state |
| Preserved A | A completed another valid call on its original identity. Gateway/service processes, owner bytes and the unrelated sentinel remained unchanged |
| Reconnected B | The stale identity received HTTP 404; exactly one fresh initialization preceded new valid work. A second B call reused the fresh identity, and the final registry held exactly two SDK tasks and adapter registrations |
| Replay and shutdown | Exactly twelve tool dispatches occurred without replay. Final service shutdown drained state, closed the listener and exited zero. Independent inspection found zero owned containers and fixture processes |

The observation period exceeds 900 seconds because transport activity can refresh a registration's idle clock even without a new workload. The checker validates the actual last-touch age at retirement as well as monotonic elapsed observation; it does not equate time since the last compiler call with transport inactivity. This is one measured default-duration execution, not an expiry latency SLA or exhaustive coverage of expiry/admission races. The accelerated result separately establishes preservation of an actively running compiler during retirement.

All eight executed Python source hashes match Git bytes at the successful candidate. The lifecycle checker SHA-256 is `3e996ccac1162d80b1734ff66297b8c3152f42547050c946a9d4ec200ccf8ef8`; the observer SHA-256 is `17faedf2b3ca190b9e0c7657d48440eee46ade562152438c9220970245de4021`. The remaining six retain their preceding recorded identities. The focused evidence suite passed 620 tests; the full local gate passed with 17,371 tests and 909 skips, lint/format checks and package/repository type checks. Three temporary mutation checks confirmed rejection when elapsed-observation, measured-idle-age or non-accelerated-policy guards are removed.

Reproduce with the existing lifecycle command plus `--case idle-default` and a fresh private root. Retain both default-idle JSON files and private provider/transport/Gateway logs; credentials, host paths and raw identities are not committed. The earlier accelerated, startup, registry and combined lifecycle modes and separate live MAF/stdio cases were not rerun. The failed and deliberately stopped attempts above remain part of the evidence history; neither produced a success report. Broader expiry/admission races, other retirement triggers, actual Docker cleanup/daemon failures, prolonged/concurrent churn, repeated discovery failure/backoff and remaining transport/MAF combinations stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). No actual-model retry, watchdog, host-crash or unattended-operation claim is made.


## Docker connection loss during cleanup: bounded follow-up (2026-10-09)

The independent `--case docker-disconnect` uses a private Docker CLI configuration and named context, initially pointing at the same local pipe/socket engine as the independent observer. Once two logical Gateway sessions have completed valid calls and A has an observed Bicep compiler, the checker pauses that exact owned container to prevent natural completion. It changes only the private context to a reserved, non-listening loopback endpoint, then aborts A's Gateway turn. The published backend and Docker CLI execute normally; an opt-in observer records real `docker rm -f` outcomes without substituting a result or cleanup implementation.

Acceptance requires correlated accepted MCP cancellation, a nonzero real removal command with connection-refusal evidence for the exact container name, independently retained paused work, poisoned admission and HTTP 503 readiness. Both existing sessions must refuse new work without executing a binding. Shutdown must drain protocol state, exit nonzero and leave the independently observed container intact. The checker then restores the private context and starts the service with retained ownership. The original cleanup path must remove the container before readiness; the same Gateway must complete new work in both sessions on distinct fresh MCP identities without replay of the aborted call. Seven tool dispatches are expected, with an unrelated owner preserved and final cleanup confirmed.

This injects loss of the service's Docker connection, not a Docker daemon crash or a daemon-side removal rejection. It changes a fixture-only context and never switches the user's global context, stops the shared engine or fabricates Docker responses. The daemon remains independently reachable. The case is restricted to local pipe/socket endpoints; TLS/remote-daemon behavior and general outage recovery are not qualified. A successful execution writes `docker-disconnect-report.json` and `docker-disconnect.json` with the candidate, environment and bounded evidence, retaining `complete_matrix: false`. Live execution was blocked at this preparation checkpoint; the subsequent successful execution is recorded below.


The attempted execution on clean candidate `1ee561f4` stopped at prepared-image inspection before starting any fixture service, Gateway, provider or container: the local Docker engine endpoint was absent. Starting Docker Desktop did not restore the engine; startup reported a VMM socket-forwarder failure while renaming a stale socket. A subsequent bounded engine probe timed out. This prerequisite failure is outside the injected scenario and establishes no new cleanup, admission or recovery behavior. No success report was produced, and the real connection-loss/recovery sequence was unrun at this checkpoint. The default-idle evidence above retains its original candidate and successful execution boundary.


The full local gate passed: 17,544 tests, 909 skips, lint/format, package/repository type checks and documentation paths. The repository type pass reported zero errors and 425 warnings. Its full pytest phase preceded four additional classifier negatives; the focused suite passed 691 tests after those additions. The later Linux portability follow-up `1b41b87a` added the `SO_EXCLUSIVEADDRUSE`-absence case and passed 692 focused tests. That follow-up also passed the complete repository type check targeting Linux (zero errors, 425 warnings), Windows fixture typing and replacement hosted CI. Final documentation checks passed 381 tests, Markdown examples passed, and 234 live tracker references matched GitHub. These are offline harness checks; they do not establish the unrun live Docker connection-loss sequence.


## Docker connection loss and retained-resource recovery: live result (2026-10-10)

The independent `--case docker-disconnect` passed on clean candidate `1a537b2d2dc606bbe083e903324a8f73432abbae` after Docker engine availability was restored. The fixture from merged [#1809](https://github.com/sokolaidev/maf-extensions/pull/1809) ran unchanged. This Windows execution retained OpenClaw 2026.9.7, published core/Bicep/Docker 0.46.0/0.22.0/0.24.4, MCP 1.28.1, MAF core 1.19.0 and Uvicorn 0.54.0. Docker client/server were 29.8.2, Node was v26.8.1 and uv was 0.12.5. The immutable prepared image was `sha256:5611ccff6015b5dd25d512c1f2dc6ab3cdfaecf9274a523c48cc07116fb38c89`; the compiler configuration SHA-256 was `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1`.

| Check | Observed result |
|---|---|
| Baseline and fault target | A and B completed valid work on distinct MCP sessions; Docker independently observed A's subsequent compiler and the checker paused its exact owned container before disconnecting the service's private context |
| Cancellation and failed removal | Gateway abort delivered correlated accepted MCP cancellation; four real removal commands in the selected cleanup evidence returned nonzero with connection-refusal diagnostics for the exact target |
| Refused admission | The service settled poisoned with unconfirmed cleanup and returned HTTP 503 readiness; both existing sessions refused new work without binding execution |
| Retained resource | Independent Docker inspection found the same paused compiler after refusal and after poisoned shutdown; the service drained sessions/active work and exited 1 |
| Explicit recovery | Restoring the private Docker context and restarting with unchanged owner bytes removed the retained compiler through the original cleanup path before startup readiness |
| New work | The same Gateway completed new valid work in A and B with two distinct fresh MCP identities; the final registry had two adapter records and two SDK tasks, with no replay of aborted work |
| Final cleanup | Exactly seven tool dispatches, preservation of the unrelated owner during the sequence, final service exit zero and drained state; independent post-run checks found zero owned containers and zero fixture processes |

`docker-disconnect-report.json` SHA-256 is `b1e26fc968700b51420c8d79fe1e7457d8ffbbe90cda4cafb573a8fd06d6b06c`; `docker-disconnect.json` SHA-256 is `593e7c13b18277d8d070992a63026c2a7c337f7b979f4371aa296bec346c0927`. The report records `dirty: false` and retains `complete_matrix: false`. All eight executed source hashes match candidate Git bytes exactly:

| Source | Executed and Git-LF SHA-256 |
|---|---|
| `server.py` | `6fd855751df41e3f841bda44a26a53841b0aa12c760398153a3ed6a903c281d5` |
| `workload_http.py` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` |
| `workload_service.py` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` |
| `openclaw_gateway_check.py` | `4786505bb3d6a7df0c6c99d1be25d9ad1a78c92db46c5be5669f30d098d8dda1` |
| `openclaw_gateway_http_check.py` | `c2c46e21b62739cfb9a65b491491fb858c7245d92236700d8ffd5e839175ac67` |
| `openclaw_gateway_lifecycle_check.py` | `b11d0558c7c6acf6e7e97b2c4a080c6afca474faf0eb71fdad74def8653ed56c` |
| `openclaw_gateway_provider.py` | `f9bf7f8446e0cf620fb371d51cdacfbe2661b3cd0ba31af90436568b453f5403` |
| `openclaw_http_observer.py` | `502a8ca2e37056cf259e39b5d6fa9e474336d605be6e599a5418b1aa7ba44b1e` |

Reproduce with the existing lifecycle command, `--case docker-disconnect` and a fresh private root. Retain both Docker-disconnect JSON files and the private provider, transport and process logs; credentials, raw owner/session identities and host paths are not committed. This successful run supersedes the engine-availability blocker for this case; the earlier prerequisite failure remains a separate unsuccessful attempt with no scenario evidence.

This qualifies one service-side Docker CLI connection refusal and explicit recovery on the recorded Windows candidate. It does not qualify a daemon crash, daemon-side removal rejection, remote/TLS Docker endpoints, an automatic watchdog, actual-model retries or unattended operation. Previous combined lifecycle, registry, idle-expiry and separate live MAF/stdio cases were not rerun. Broader Docker outages, expiry/admission races, other retirement triggers, prolonged/concurrent churn, repeated discovery failures and the remaining transport/MAF matrix stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676).


## Daemon authorization denial and retained-resource recovery: live result (2026-10-11)

The independent [daemon-denial supervisor](../../../tests/fixtures/openclaw_docker_denial_check.py) passed the seven-dispatch Gateway sequence on clean candidate `5a149d372c0273540bf6135492c7b316550ccea7`. The selected fault uses Docker's real [authorization plugin API](https://docs.docker.com/engine/extend/plugins_authorization/): a fixture plugin returns `Allow: false` for DELETE of one exact compiler container, and the daemon rejects the actual Docker CLI removal. The observer forwards the original removal command and records its real exit and diagnostic; it does not replace a cleanup result. This establishes a policy rejection, not a daemon storage failure or crash.

The supervisor starts a dedicated `docker:29.8.2-dind` daemon, resolved to `sha256:1e08cdb63405ca788aea94ef35b792d1e299607c667d33d7bcbcae1fd2611ced`, with its own ephemeral volume and a randomly assigned loopback port protected by mutual TLS. It copies the plugin into that container and discovers it through Docker's [Unix socket plugin interface](https://docs.docker.com/engine/extend/plugin_api/). The [official entrypoint](https://github.com/docker-library/docker/blob/master/29/dind/dockerd-entrypoint.sh) creates the certificates. The daemon uses the [containerd image store](https://docs.docker.com/engine/storage/containerd/) so loading the prepared OCI image preserves its exact identity; the observed storage driver was `overlayfs`. The fixture sets two CPUs, 2 GiB memory and a 512-process limit, disables its bridge and iptables management, and uses no host bind mount or parent Docker socket mount. Docker-in-Docker runs privileged as required by the [official image](https://github.com/docker-library/docs/blob/master/docker/README.md); this disposable setup is not a containment guarantee. The shared engine configuration and global context are unchanged.

The execution retained Windows, OpenClaw 2026.9.7, published core/Bicep/Docker 0.46.0/0.22.0/0.24.4, MCP 1.28.1, MAF core 1.19.0 and Uvicorn 0.54.0. The dedicated Docker daemon was 29.8.2. The prepared image remained `sha256:5611ccff6015b5dd25d512c1f2dc6ab3cdfaecf9274a523c48cc07116fb38c89` and the compiler configuration remained SHA-256 `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1`. Go 1.27.0 on Windows built the Linux/amd64 plugin with `CGO_ENABLED=0` and `-trimpath`. Plugin source SHA-256 was `086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4`; binary SHA-256 was `db8135a51f3041874a7933e064defb62dd99902de682c4bd9aa2a2407ad41202`.

| Check | Observed result |
|---|---|
| Baseline and target | A and B completed valid work on distinct MCP sessions; independent Docker observation found A's next compiler, which the checker paused before arming denial for its exact name and full container ID |
| Real daemon rejection | Correlated accepted MCP cancellation triggered four failed removal commands in the initial cleanup evidence; each matched a plugin decision by the private service-client marker, target and armed nonce, and the CLI reported the plugin's authorization-denied diagnostic |
| Refusal and survival | HTTP 503 readiness, poisoned admission and unconfirmed cleanup; both existing sessions refused new work without executing a binding, and the same paused compiler survived service shutdown with exit 1 |
| Ordered recovery | All removal decisions through shutdown remained denied; disarming the policy and restarting with unchanged owner bytes produced real successful removal and an allowed plugin decision after those denials, before startup readiness |
| New work | The same Gateway completed new valid work in both sessions on two distinct fresh MCP identities; final registry held two adapter records and two SDK tasks, without replay of aborted work |
| Cleanup | Seven dispatches, unrelated owner preserved during the sequence, final service shutdown clean and all nested containers absent; the supervisor removed the exact dedicated daemon and its anonymous volume, then verified absence and unchanged parent engine identity |

The plugin synchronizes its append-only decision journal before returning authorization. A strictly increasing sequence orders decisions without comparing Windows and Linux clocks. Evidence acceptance requires one matching decision per observed service removal command; wrong client/nonce/target, missing or extra decisions, reordered/duplicate sequences and malformed flags refuse qualification. The final `docker-denial-host-report.json` is written only after Gateway qualification and dedicated-daemon cleanup succeed; the inner Gateway report alone cannot establish supervisor cleanup. Independent post-run inspection found no authorization-fixture containers or processes using this run root.

| Private evidence artifact | SHA-256 |
|---|---|
| `docker-denial-host-report.json` | `265742f1b0af3da4d39fbc9a3f7e5716c2eb91aa9bc322e7737e84fb6eb44ac5` |
| `docker-denial-report.json` | `501e4e81ee9e3b53338fec393352cc930238440ff7ee900cb23a12aab96d50e9` |
| `docker-denial.json` | `a71c064ab4d7a79d3c8186c8a9dd20bc2ae313794c5c3ed3553edb149ff10b06` |
| `docker-authz.jsonl` | `103547846c727a5cdbb2d3f57a866b801a01259e4cf61e5ff29e7ac6703b465e` |

The report records `dirty: false` and `complete_matrix: false`. Executed bytes match the recorded hashes. Four Python fixture files used Windows CRLF checkout bytes; all ten Python sources match candidate Git bytes after CRLF-to-LF normalization, while the Go source matches Git byte-for-byte. The two identities are recorded separately rather than claiming raw byte equality:

| Source | Executed SHA-256 | Git-LF SHA-256 |
|---|---|---|
| `server.py` | `6fd855751df41e3f841bda44a26a53841b0aa12c760398153a3ed6a903c281d5` | `6fd855751df41e3f841bda44a26a53841b0aa12c760398153a3ed6a903c281d5` |
| `workload_http.py` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` |
| `workload_service.py` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` |
| `openclaw_docker_denial.py` | `b0ee9bcc9932a1b421161b93288df45bc3eb1d5b4d2d6970b21da71abe6bf62e` | `32b951278591a0436ce595800787a5def7b38c24aba642ef49893f0fb6bad4ce` |
| `openclaw_docker_denial_check.py` | `dcca2904d245f9005b31c458c21b798bfe36a8084a9856451afa62bbb9475016` | `c9b037a18984505dcebee864047eefc7ac4fe73d1ccc44cd44c287655c42a331` |
| `openclaw_gateway_check.py` | `4786505bb3d6a7df0c6c99d1be25d9ad1a78c92db46c5be5669f30d098d8dda1` | `4786505bb3d6a7df0c6c99d1be25d9ad1a78c92db46c5be5669f30d098d8dda1` |
| `openclaw_gateway_http_check.py` | `c2c46e21b62739cfb9a65b491491fb858c7245d92236700d8ffd5e839175ac67` | `c2c46e21b62739cfb9a65b491491fb858c7245d92236700d8ffd5e839175ac67` |
| `openclaw_gateway_lifecycle_check.py` | `ca86012fc9fdc93ef7c3f29eac08d23c54660fb1cca3556341690f3182895dea` | `e8a1a88351e9af60a99c78e3e6298dd2b6e4f1e619ea81950721003eb569f0c9` |
| `openclaw_gateway_provider.py` | `f9bf7f8446e0cf620fb371d51cdacfbe2661b3cd0ba31af90436568b453f5403` | `f9bf7f8446e0cf620fb371d51cdacfbe2661b3cd0ba31af90436568b453f5403` |
| `openclaw_http_observer.py` | `174df39a76a990b4166768210d6102e85949b8633d10670c016d2d02716cd5d5` | `4c8ffcabf67cde31b21c7267299bc96704776041c34cf914219e4d7adcd08d40` |

Reproduce with a local parent Docker context, a prepared image already loaded in that engine, Go on PATH, the pinned OpenClaw installation and a fresh private root. Pass the same private Gateway configuration and prepared Bicep configuration used by the lifecycle checker:

```text
uv run python tests/fixtures/openclaw_docker_denial_check.py --root <fresh-private-root> --config <private-openclaw.json> --bicep-config <prepared.bicepconfig.json> --image <prepared-image-id> --daemon-image <docker-dind-image-id> --openclaw <pinned-openclaw-installation>
```

Retain the host report, Gateway report, detailed evidence, plugin journal and private process logs. Raw identities, host paths, Gateway credentials and TLS private keys are not committed. Preliminary setup probes using Windows bind mounts timed out before creating a daemon; a copy-based standalone authorization probe then verified actual denial, survival and recovery. Those probes did not establish Gateway acceptance. The final clean candidate's full Gateway sequence passed on its first attempt.

The focused evidence suite passed 750 tests, including Go policy tests. Two temporary guard mutations made the corresponding negative tests fail when client correlation or the real authorization-denial requirement was removed. This case adds fixture/evidence coverage only; production packages, service/binding behavior and installed OpenClaw code are unchanged. Earlier connection-loss, idle-expiry, registry and combined lifecycle modes and separate live MAF/stdio combinations were not rerun. General remote/TLS deployment, daemon storage/removal faults, daemon or host crashes, broader outages, expiry/admission races, other retirement triggers, prolonged/concurrent churn, repeated discovery failures and the remaining transport/MAF matrix stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). No watchdog, actual-model retry or unattended-operation claim is made.

The full local gate passed: 17,833 tests, 910 skips, lint/format, package/repository typing, documentation paths and implicit-concatenation checks. Repository typing reported zero errors and 443 warnings. Linux-targeted fixture typing reported zero errors and one existing warning. Markdown examples and all 236 live tracker references passed. Hosted CI is a separate gate and is not established by these local checks.


## Repeated discovery failure: bounded follow-up (2026-10-11)

The independent `--case discovery-backoff` extends the existing eight-slot registry sequence with three real MCP initialization HTTP 503 responses at full capacity. It retains the three delete/reconnect cycles and expects 26 tool dispatches overall. The installed Gateway, prototype service and binding are unchanged. After the first refusal the checker waits 35 seconds before submitting a fresh turn that triggers a second refused initialization. After that failure, a new turn between 30 and 60 seconds must receive the cached-catalog error without provider execution or another initialization. A later turn triggers a third refused initialization; another probe between 60 and 120 seconds must remain suppressed. Both wall-clock evidence and a monotonic observation must place each probe inside its selected window. Quiet intervals reject autonomous discovery, workload calls, binding execution and session retirement.

The checker then deletes one occupied logical session and verifies SDK/adapter draining, leaving seven original registrations. The immediate replacement turn must still fail before provider execution or MCP initialization. After another 65-second quiet interval, a newly submitted turn must trigger one successful initialization and tool listing while still returning the stale-catalog error. A separate new turn must complete on that fresh MCP identity, and all seven surviving sessions must complete work with unchanged identities. None of the seven refused turns may reach the provider or replay later. The same Gateway/service processes, owner bytes and unrelated sentinel must survive; final service exit, owned-resource cleanup and listeners must be clean. The checker writes `backoff-report.json` and `registry-churn.json`, retaining `complete_matrix: false`.

This case measures selected later-backoff windows and cross-runtime recovery after real capacity refusal. It does not measure the originating runtime's five-second first retry, exact deadline transitions, delays above 120 seconds, concurrent discovery, other transport errors, model retry decisions or unattended recovery. A successful live result must be recorded separately against its clean candidate; the harness and offline evidence checks alone do not establish that result.


## Repeated capacity-refusal backoff and recovery: live result (2026-10-11)

The independent `--case discovery-backoff` passed on clean candidate `8e538283a864d75412e853c2e061dd3dcde4507d`. It retained OpenClaw 2026.9.7, published core/Bicep/Docker 0.46.0/0.22.0/0.24.4, MCP 1.28.1, MAF core 1.19.0, Uvicorn 0.54.0, and the immutable prepared image/configuration baseline above. The installed Gateway, prototype service and binding were unchanged. The report records 26 tool dispatches, `dirty: false`, a clean final service shutdown and `complete_matrix: false`.

| Check | Observed result |
|---|---|
| Full registry | Eight distinct registrations and three delete/reconnect cycles completed; all seven unaffected sessions remained callable before the overflow sequence |
| Repeated discovery failures | Exactly three correlated MCP initialization requests received real HTTP 503 responses at the unchanged eight-slot limit; each failed before any workload dispatch |
| Second-failure probe | A new turn ran 35.84–36.31 seconds after the second refusal settled; it returned the catalog error without another initialization or provider execution. The monotonic observation was 36.05 seconds |
| Third-failure probe | A new turn ran 65.86–69.25 seconds after the third refusal settled; it remained suppressed without another initialization or provider execution. The monotonic observation was 69.00 seconds |
| Quiet observation | The four measured intervals lasted at least 35, 35, 30 and 65 seconds; no autonomous discovery, workload call, binding execution or retirement occurred |
| Free slot during cooldown | Deleting one occupied logical session drained its SDK/adapter state and left seven original registrations. A new turn in that deleted session still failed before provider execution or MCP initialization |
| Recovery after cooldown | After another 65-second quiet interval, a newly submitted turn triggered one successful initialization and tool listing while receiving the stale-catalog error. A separate new turn completed on the fresh MCP identity; all seven surviving sessions completed work with their original identities |
| Isolation and cleanup | All seven refused turns stayed absent from provider execution. Gateway/service processes and owner bytes were retained, the unrelated sentinel survived until fixture cleanup, and final service exit was zero. Independent inspection found zero owned containers and zero surviving recorded fixture processes |

These measured windows are consistent with the pinned source's 60- and 120-second later startup backoffs. They establish suppression beyond the preceding delay and eventual cross-runtime recovery, not the exact deadline boundary or a guarantee that a waiting service automatically triggers discovery. Successful work required new turns; none of the refused turns was retried as workload execution.

| Retained evidence | SHA-256 |
|---|---|
| `backoff-report.json` | `17c017ff33ddc6b4100fc1ee0cc2728e1cc5c7a943238d055d49b271fd3a0ba4` |
| `registry-churn.json` | `03a539517099bd7ca4f8bacadcb215d9747784e37b2c656e9c55c82fa373feac` |
| `backoff-windows.json` | `0113ca89b223d285ea336d6020c40e6ab0c12b335f9e8014f72ba582dc79b928` |

All ten source hashes recorded in the report match the candidate's Git bytes. The changed lifecycle checker SHA-256 is `7f2c63a87878c7de023d698c1e819f15e0ede8730a73767a1be6f55d45c2bf11`. The three installed Gateway discovery/allowlist source hashes match the earlier delayed-capacity-recovery table. Reproduce with the existing lifecycle command plus `--case discovery-backoff` and a fresh private root. Retain the three evidence files and private provider/transport/Gateway logs; credentials, host paths and raw identities are not committed.

An earlier clean attempt on `ed4be4f6` stopped at the second quiet-interval assertion after two capacity refusals. Its trace contained no forbidden transport activity in that interval, but the exact clock observations were not retained, so the failed clock delta cannot be reconstructed. It produced no success report; independent inspection found no remaining owned containers or recorded fixture processes. The successful candidate retains every interval observation before validation and adds a 250 ms observation margin while preserving both elapsed-clock assertions. This failed attempt remains distinct from the successful result.

Offline verification passed 859 focused tests and 381 documentation tests. Three temporary guard mutations were rejected when monotonic probe timing, quiet-interval duration or unique-attempt correlation was weakened. The full local gate passed 18,007 tests with 910 skips, lint/format, package/repository typing, documentation paths and implicit-concatenation checks; repository typing reported zero errors and 453 warnings. The timing-observation follow-up was separately rechecked with all 859 focused tests and fixture typing. Markdown examples and 239 live documentation tracker references passed. Hosted CI remains separate evidence on the delivering PR.

The longer lifecycle, idle-expiry, Docker-failure and separate MAF/stdio combinations were not rerun. Originating-runtime first retry, exact deadline races, delays beyond 120 seconds, concurrent discovery, other discovery errors, broader daemon faults, expiry/admission races and the remaining transport/MAF matrix stay open in [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676). No actual-model retry, watchdog, host-crash or unattended-operation claim is made.


## Bicep HTTP ownership and independent MAF closure: live result (2026-10-11)

The standalone [ownership checker](../../../tests/fixtures/openclaw_bicep_ownership_check.py) passed all five cases on clean candidate `d458625fcac0487e950ebfc42032336cfad150e1`. This supplies the real Bicep/MAF composition observations requested by [#1850](https://github.com/sokolaidev/maf-extensions/issues/1850), complementing the existing Gateway evidence. No OpenClaw process or model was used in this execution. The checker serves the unchanged Bicep composition over real loopback HTTP and uses two raw MCP sessions or two real `MCPStreamableHTTPTool` clients as appropriate.

The host ran Windows, Python 3.13.12 and Docker Engine 29.8.2 with Linux guests. Python dependencies retained MAF core 1.19.0, MCP 1.28.1, maf-sandbox 0.46.0, Bicep 0.22.0, Docker backend 0.24.4, Starlette 1.7.0 and Uvicorn 0.54.0. The old local image `sha256:0bda3505a331713614b847025ce35199f3dda5a81ea2a661130525226dbc0265` was unavailable. This run deliberately used the released Bicep AVM 0.1.0 reference `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e`. Its retained signed bundles verified before execution with `releaseIdentityVerified: true`; monitoring was reported clean at verification. The [image release evidence](../../../images/bicep-sandbox/AVM.md#release-010-evidence) records the source and compiler/cache identity. Docker's local image inspection and container `Image` field both returned the same `b3f14b...` identity on this host; the release's separate configuration digest remains `sha256:fb02b2f79c7428f7595f1fa664cc4f240ed54c349e059d8e4a0dccb0e435fab3`. This is qualification of the new image identity, not a rerun of the old local image. The prepared compiler configuration SHA-256 remained `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1`.

| Case | Observed result |
|---|---|
| Equal request IDs and repeated cancellation | A's compiler was independently observed and paused. B submitted the same numeric request ID and received busy without another owned container. Cancellation addressed to B was accepted but left A active. A matching cancellation then reached real disposal; three further matching cancellations while one exact owned Docker removal was held preserved global admission. Releasing the fixture barrier delegated the real removal unchanged. Exact-container absence and a successful new B call followed. All five cancellation messages had correlated HTTP 202 responses. |
| Active response-only loss | The checker closed only the raw HTTP response connection while A's compiler was paused. No MCP cancellation was sent or observed. B remained busy without allocation. After unpausing, the binding returned an incomplete result with confirmed cleanup; the exact container disappeared and B completed new work. The 1,500-variable stress source generated oversized diagnostics, so no completed verdict is claimed for that source. The returned binding result and source digest distinguish normal incomplete settlement from cancellation or a service deadline. |
| Idle MAF close | Closing idle A issued DELETE and retired its SDK/adapter state while B's compiler remained supervised. B's original call returned, and the same B client completed a new valid call. |
| Active MAF close | Closing active A issued DELETE, cancelled only A's binding and held admission through controlled real disposal. Service cleanup and SDK retirement were observed by 22.52 seconds after the call was submitted. B's session survived and completed new work while A's client future was still pending. A's client later raised `ToolExecutionException` with underlying MCP timeout code 408 at 240.01 seconds, consistent with its configured 240-second request timeout. |
| Ordinary independent MAF close | Both clients discovered the unchanged Bicep tool and completed valid calls with one projected content item and matching source/config/image identities. After closing A, B completed another valid call through its existing session. |
| Final ownership and transport state | The run recorded 19 tool-call requests, five cancellation notifications, ten DELETE requests and ten retired sessions. Retirements required completed SDK tasks, terminated transports, zero SDK streams/request IDs/handlers and absent adapter records. The independently created unrelated-owner container survived every scenario boundary and was removed by the checker afterward. Final service state had zero sessions, no active call, no poisoning and an empty owned scope. |

The fixture pauses only a compiler independently identified by `docker top` and uses a one-shot barrier before one exact owned `docker rm -f`. It does not replace removal results, simulate container absence, alter service admission, or shorten service deadlines. Its barrier resolves ownership before arming and delegates absent companion targets and later idempotent removals unchanged. Transport observation reuses the existing observer; no production package, service, binding, MAF or MCP code changed.

Client closure and outstanding-call settlement are distinct in this pinned stack. The active MAF future remained pending after service retirement, while B could already execute. The recorded elapsed values are observation times, not cleanup deadlines or upper bounds for other runs. Hosts must retain ownership of their outstanding client tasks; this evidence does not claim that `close()` promptly completes every awaiting caller. The idle-close run's client was already settled when retirement/cleanup was inspected; its later elapsed observation includes the successful survivor call.

The PEP 723 runner recorded every installed distribution and rejected direct-URL/editable installations. Its complete manifest was:

```text
PyJWT==2.15.1
PyYAML==6.0.3
agent-framework-core==1.19.0
annotated-types==0.8.0
anyio==4.15.1
attrs==26.1.0
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
cryptography==50.0.2
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
maf-sandbox==0.46.0
maf-sandbox-bicep==0.22.0
maf-sandbox-docker==0.24.4
mcp==1.28.1
msgspec==0.21.1
opentelemetry-api==1.45.1
pycparser==3.11
pydantic==2.14.1
pydantic-settings==2.15.0
pydantic_core==2.50.1
python-dotenv==1.2.4
python-multipart==0.0.32
pywin32==312
referencing==0.37.0
rpds-py==2026.9.1
sse-starlette==3.5.0
starlette==1.7.0
typing-inspection==0.4.4
typing_extensions==4.16.0
uvicorn==0.54.0
```

The canonical manifest digest (sorted JSON keys, compact separators) was `743c1ad7ae42687ef852aae3210b9ebe345d01b77027849eef1e16f549a825b7`. A future resolver result must be recorded separately; this manifest is an observed environment, not a claim that every transitive dependency is constrained by the script.

### Ownership reproduction and evidence

Verify the prepared image's release identity using its consumer guide, then run from the repository root on a clean checkout with a fresh private evidence directory outside the repository:

```powershell
uv run --script tests/fixtures/openclaw_bicep_ownership_check.py `
  --prototype samples/experimental/openclaw_bicep/server.py `
  --config images/bicep-sandbox/prepared.bicepconfig.json `
  --root ../ownership-evidence `
  --image ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e
```

Retain `ownership-report.json`, `transport.jsonl` and private process output. The checker removes a stale success report before prerequisites, refuses a reused transport journal or dirty checkout, records exact candidate/source hashes and writes success only after every case and final cleanup pass. The report's SHA-256 was `7383252e5e1f0e199d5cbe406975c2baf06fefbdcc8b8d978cb8a674136da525`; the executed checker file hash was `dc83a4e866750739a9bccce8710fa106b32e0dc41ba95e6afb29b0b337364e66`. Credentials, private paths, raw owner/session/container identities and source-bearing process logs are not committed.

An earlier complete run on `e7dec5ff435739d4316d697a4df6c40cddedd243` passed the same five cases and final cleanup; its report SHA-256 was `23ff72c79fe9c4d4261aade1664883f04957259bef3bf1f7f11fe69c95a8ca5f`. The final run adds explicit separation of service/client settlement and calls through B before awaiting A's client timeout. Earlier unsuccessful candidates remain distinct: `fa1c54b5` and `ce5f926f` stopped at Docker process observation because the selected output omitted the required PID field; `23135107` exposed the fixture's attempt to inspect absent companion/retry removal targets; `14cc6043` completed the cancellation case but rejected the stress source's incomplete result. None wrote a success report. The process selector, one-shot removal barrier and incomplete-result acceptance now have focused regression coverage.

These observations discharge #1850's composition cases from the [closure audit](openclaw-qualification-closure-audit.md). They do not complete [#1851](https://github.com/sokolaidev/maf-extensions/issues/1851)'s finite published transport/100-session retirement subset. [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open until that evidence is delivered and reconciled. Other host platforms, current-version compatibility, new Gateway executions, actual-model behavior, exhaustive crashes and unattended recovery remain outside this execution.

Offline verification passed 904 focused tests and 384 documentation tests. The full local gate passed 18,153 tests with 910 skips, lint/format, package/repository typing, documentation paths and implicit-concatenation checks; repository typing reported zero errors and 453 warnings. The final timing observation and four additional negative regressions were separately checked with the focused suite and fixture/test typing after the full gate collected its tests. Hosted CI remains separate evidence on the delivering PR.


## Published transport bounds and SDK retirement: live result (2026-10-11)

The [published transport checker](../../../tests/fixtures/openclaw_published_transport_check.py) passed all 43 selected cases on clean candidate `80215d91be2df64c139d11dd39544853984ebca2`, using Windows and Python 3.13.12. This supplies [#1851](https://github.com/sokolaidev/maf-extensions/issues/1851)'s finite transport/client evidence. It reuses the existing [HTTP acceptance tests](../../../tests/test_mcp_workload_service.py), unchanged service/adapter code, actual Uvicorn loopback sockets and real MAF clients with synthetic workload bindings. Five parametrized cases directly drive ASGI; the remaining 38 use HTTP sockets. No Docker container, Bicep compiler, OpenClaw Gateway or model ran in this execution.

The runner selects 24 test functions, expanding to 43 cases, and requires all 129 setup/call/teardown reports to pass. No case was skipped or expected-failed. Pytest reported 43 passes in 46.97 seconds; runner elapsed time was 47.78 seconds. One dependency warning reported an unresolved `lifespan` annotation in pydantic-settings; it did not prevent service startup or the passing client cases. These are observed execution times, not latency guarantees.

| Acceptance area | Recorded observation |
|---|---|
| Authentication and framing | POST/GET/DELETE authorization, Host/Origin refusal, malformed/uninitialized requests, duplicate live IDs, typed IDs, nonfinite JSON, Content-Length/chunked body limits, header limits and slow-body refusal passed. Rejected authorization and malformed input did not dispatch a synthetic binding or expose the submitted marker. |
| Capacity and reservations | Eight sessions admitted, ninth refused; initialization and retirement reservations, pending-body settlement, idle/active separation, shutdown startup races, one GET stream and 32-connection limits passed. Event barriers and injected startup/retirement failures are test controls, not additional production behavior. |
| Direct ASGI subset | `test_body_reader_budget_released_after_rejection` exercises the 16-reader budget directly. Four `test_pending_post_releases_reservation_on_body_failure` cases inject malformed input, timeout, overflow and cancellation through ASGI. They prove handler/accounting settlement, not five additional socket-level failure cases. The separate chunked/slow-body and pending-body reservation cases use real HTTP. |
| SDK retirement | Read-only references to 109 distinct session records were retained: eight saturation sessions, 100 sequential churn sessions and one expired idle session. Every SDK task and retirement task finished successfully; every transport terminated with zero SDK streams, request IDs and HTTP handlers, and no active GET handler. The checker records every observation and rejects missing cycles, unsettled state and non-integer zero substitutes. |
| Final transport state | Zero adapter records, zero body readers, no active call, no poisoned service, and FastMCP's default retaining session manager absent. No production registry was added. The 109 observed retirements belong to the churn test, not a claim about the total number of sessions created by all 43 cases. |
| MAF client contract | Two actual `MCPStreamableHTTPTool` clients used trusted static headers, discovered the two synthetic bindings and returned one projected result item with the expected values. B completed a new call through its existing session after A closed; both sessions retired. This complements #1850's real Bicep/MAF composition evidence. |

### Transport environment and reproduction

The selected published baseline is MAF core 1.19.0, MCP 1.28.1, maf-sandbox 0.46.0, Bicep 0.22.0, Docker backend 0.24.4, HTTPX 0.28.1, jsonschema 4.26.0, Starlette 1.7.0, Uvicorn 0.54.0 and pytest 9.0.3. The installed Bicep/Docker packages preserve the requested environment; their presence is not runtime qualification of those backends. Every installed distribution was recorded, direct-URL/editable installs were refused, and suite/framework import locations had to resolve under their installed distributions. The complete resolved manifest was:

```text
agent-framework-core==1.19.0
annotated-types==0.8.0
anyio==4.15.1
attrs==26.1.0
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
colorama==0.4.6
cryptography==50.0.2
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
iniconfig==2.3.1
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
maf-sandbox==0.46.0
maf-sandbox-bicep==0.22.0
maf-sandbox-docker==0.24.4
mcp==1.28.1
msgspec==0.21.1
opentelemetry-api==1.45.1
packaging==26.3
pluggy==1.6.0
pycparser==3.11
pydantic==2.14.1
pydantic-core==2.50.1
pydantic-settings==2.15.0
pygments==2.21.0
pyjwt==2.15.1
pytest==9.0.3
python-dotenv==1.2.4
python-multipart==0.0.32
pywin32==312
pyyaml==6.0.3
referencing==0.37.0
rpds-py==2026.9.1
sse-starlette==3.5.0
starlette==1.7.0
typing-extensions==4.16.0
typing-inspection==0.4.4
uvicorn==0.54.0
```

The canonical manifest SHA-256 (normalized names, sorted JSON keys and compact separators) is `170aa13121a875f4ac1e8043d19d52708b813e16139a568220fb0c52839c8d4e`. Transitive dependencies are observed versions; a later resolution must produce its own manifest and evidence. The runner disables external pytest plugins, conftest loading and environment addopts, and uses an empty temporary pytest configuration to keep workspace dependencies and test selection out of the published execution.

From a clean checkout, use a fresh private evidence directory outside the repository:

```powershell
uv run --script tests/fixtures/openclaw_published_transport_check.py `
  --root ../transport-evidence
```

The service retains its eight-session, 32-connection, 16-reader, 2 MiB body and 16 KiB header limits. The inherited tests deliberately accelerate selected timing: slow-body timeout to 0.02 seconds, incomplete-header deadline to 0.05 seconds, and the pending-body idle case to 0.02 seconds. The churn and active/idle separation tests age retained timestamps by the configured 900-second idle interval. Other cases use fixture events to hold initialization, retirement and pending bodies. These observations do not claim a fresh 900-second expiry run; that is separately recorded in the earlier Gateway evidence.

The runner removes a stale success report before prerequisite checks, refuses a reused attempt directory or dirty checkout, records source hashes and rechecks the source/checkout after execution. `attempt.json` retains the sanitized test-phase observations even when acceptance fails; `transport-report.json` is written only after every required case and retirement/final-state guard passes. Retain private process output separately. Raw credentials, session identities and host paths are absent from the report.

| Evidence identity | SHA-256 |
|---|---|
| `transport-report.json` | `3bb5e3608aba3b21e05a933438feca010888040fc71f6522f3721b0f0b8f09d5` |
| `tests/fixtures/openclaw_published_transport_check.py` | `e0b628d77011deabfd6f0a022b7aba44a2609e498c406b17564e2f4caba74fbf` |
| `tests/test_mcp_workload_service.py` | `6e2af3c9289bd4cce75b145563713787189ae089555de7800d07a8fa67665271` |
| `samples/experimental/openclaw_bicep/server.py` | `6fd855751df41e3f841bda44a26a53841b0aa12c760398153a3ed6a903c281d5` |
| `samples/experimental/openclaw_bicep/workload_http.py` | `b291f4539b3e1d4a4ef3aa570683585da84cfb5b0911eddb20fda7e0cb366b33` |
| `samples/experimental/openclaw_bicep/workload_service.py` | `984c722e9fb732c79d7449e74be1ea3c84e06ae79d5df4eeb5be2391b68376ac` |

The successful candidate was the first published-stack attempt for this checker; earlier workspace validation and type-check corrections are not published execution failures. Negative regressions reject omitted/duplicated tests, missing phases, skipped/expected-failed cases, teardown failures, missing retirement observations, retained SDK/service state, changed dependency versions, editable installations and import shadowing.

These observations fill the closure audit's published transport/resource/MAF attribution gap. [#1865](https://github.com/sokolaidev/maf-extensions/pull/1865) separately delivers #1850's Bicep/MAF ownership evidence. [#1676](https://github.com/sokolaidev/maf-extensions/issues/1676) remains open for delivery and consolidated reconciliation; this transport run alone does not close the host qualification. Historical `complete_matrix: false` reports remain unchanged. No new Gateway, compiler, other-host-platform, current-version, indefinite churn, actual-model or unattended-operation qualification is implied.

Offline verification passed 939 focused tests (eight separate live cases deselected), 387 documentation tests, and typing for the changed fixture/tests with zero errors or warnings. Markdown examples and 254 live documentation tracker references passed. The full local gate passed 18,498 tests with 910 skips, lint/format, package/repository typing, documentation paths and implicit-concatenation checks. Repository typing reported zero errors and 455 warnings. Hosted CI remains separate evidence on the delivering PR.


## Installed worker qualification (2026-10-13)

Issue #1898 packages the existing service as `maf-sandbox-mcp`, with fixed `maf-bicep-mcp` entry point and explicit Bicep/Docker dependencies. The reusable service still imports no concrete kind or backend. The historical sample remains a source specimen; current runtime development and installed tests use the package. This execution qualifies the wheel built from candidate `991a6c11`, not a published release. Subsequent harness/reporting and documentation changes do not change its four runtime module hashes.

The clean environment was created outside the checkout with Python 3.13.12 on Windows and Linux guests on Docker Engine 29.8.2. The candidate wheel was the only local artifact; every dependency resolved from the package index. No editable, `PYTHONPATH` or sibling-source fallback was admitted. Framework, suite, MCP, Uvicorn and worker imports resolved inside the environment's `site-packages`; the private report retains their locations. Each installed worker module matched its bytes inside the built wheel before and after execution. The console executable loaded and ran from that environment.

The deliberate baseline change is MAF core 1.20.0, sandbox 0.48.0, Bicep 0.22.2 and Docker backend 0.27.0, with MCP 1.28.1, Starlette 1.7.0 and Uvicorn 0.54.0. OpenClaw remained the locally installed npm 2026.9.7 release. The selected image remained released Bicep AVM 0.1.0, `ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e`; the configuration hash remained `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1`. The launchers passed its resolved immutable local identity, and observed compiler results attributed that same identity.

### Results and evidence boundary

| Evidence | Observed result |
|---|---|
| Clean-wheel smoke | Installed entry point `--help`, inert fixed binding construction, typing marker and installed module locations passed without allocating a sandbox. |
| Installed transport/MAF suite | All 43 finite cases passed with setup/call/teardown evidence; 109 SDK retirements (eight saturation, 100 churn, one expiry) settled with zero retained sessions/readers/active work and no poisoned service. Synthetic bindings and the original bounded fault/timing controls remain explicit. |
| Real Bicep/MAF ownership | Equal request IDs and repeated cancellation, response loss, idle MAF close, active MAF close and ordinary close all passed. Exact-owned containers were removed, unrelated-owner survival held, and the final sentinel was removed. |
| Active client versus service retirement | Service settlement took 25.91 seconds; the active MAF caller stayed pending until its configured 240-second timeout and raised `ToolExecutionException` with code 408. A surviving client completed validation before that timeout. The host still owns its outstanding client tasks. |
| Instrumented packaged service through Gateway | Two distinct MCP sessions produced valid/invalid/incomplete outcomes with one projected item, denied unauthorized/helper tools, refused competing work without allocation, observed the matching MCP cancellation, removed the exact container, preserved another owner and completed a later call. Observer service exited 0 with no sessions, active work or cleanup uncertainty. |
| Actual installed console through Gateway | A second fresh profile launched the installed `maf-bicep-mcp` executable. Two Gateway sessions each completed valid, invalid and uncached-module/incomplete scenarios with one projected item and confirmed cleanup. An additional MAF call validated prepared virtual-network module 0.7.2. |
| Closed-network behavior | The same image reached a fresh control listener over HTTP/IPv4 TCP with bridge networking and returned the expected nonce. With `network=none`, curl returned connection failure (exit 7), no response body, and Docker inspection confirmed the selected network mode. Both probe containers and the listener were removed. This qualifies one destination/protocol, not all network paths. |

The direct console was stopped only after every call settled and independent Docker inspection showed no owned resources. Its idle Windows process tree was then terminated, its owner lock was reacquired and all launched test processes were confirmed stopped. That console stop is not evidence of graceful Windows signal handling; graceful transport/resource shutdown is the separately observed service pass. No host-crash or power-loss guarantee is added.

The Gateway provider was a deterministic local fixture, not an LLM. The observed Gateway report keeps `complete_matrix: false`; its broader restart/crash/runtime-disposal rows remain credited to their prior candidates, while the finite transport and Bicep ownership rows above were rerun for this wheel. Actual-model behavior, exhaustive interruptions, other host/interpreter versions, remote tenancy, independent watchdogs, unattended operation and package publication remain outside this evidence.

Three preliminary Gateway launcher attempts failed before qualification completed: one waited for the wrong readiness log phrase; one passed a repository digest reference to an assertion expecting the resolved image ID; the first console attempt made the same configured-versus-resolved identity mismatch. Their private attempt records remain failed, and their processes were stopped. The successful runs used fresh isolated state and the matching immutable image identity; no worker behavior was changed to pass those assertions.

### Artifact and report identities

| Artifact | SHA-256 |
|---|---|
| Built worker wheel | `570cb6ba88410822adf449ea610f35b2af80532f6dbf06687e1bb9554dff77a7` |
| Installed transport/ownership/network report | `3288a5fd1c96e6c50b00af41b1e79fe50390189cfde7fafcacaa682d456c8833` |
| Instrumented Gateway report | `14125de0e4bcd28c99df7e5bb868f9a1cd1ebc17cba8aeb88e27caee73eeecfc` |
| Console Gateway report | `f3789a8daca658eba19bcce123c00cc1ed605d1968b7e9722877c64e00d0456b` |
| Canonical resolved manifest | `bf042754f19e5af6646bd7f3d844690eff27e2cae438f03456bf73b3a181db52` |

Runtime modules inside the wheel:

| Module | SHA-256 |
|---|---|
| `maf_sandbox_mcp/__init__.py` | `c5f5876773dd0b24687a7c16b95770af37f5143c5b89bade19407cfe3aeb369e` |
| `maf_sandbox_mcp/server.py` | `f000c5c76c9859ce207d19be008059e875c4c4baf4b307db4a204c0955a9898d` |
| `maf_sandbox_mcp/workload_http.py` | `04d7e3ac3f48559fa7fd1a3ef95dab30e90ecadc69a8df781c15b0652e4d016b` |
| `maf_sandbox_mcp/workload_service.py` | `8972bc76c8ef9f990f4f0a4cbf3f6704b0e5752bb2b42aaa9f7dfcd0a35a45e0` |

Raw reports, launch profiles and logs stay private because they can contain host paths, credentials or compiler source. Hashes identify retained evidence; they do not make those private artifacts publicly downloadable. The complete resolved manifest follows. The unreleased worker's `0.0.0` is repository bootstrap metadata, not a published version.

```text
agent-framework-core==1.20.0
annotated-types==0.8.0
anyio==4.15.1
attrs==26.1.0
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
colorama==0.4.6
cryptography==50.0.2
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
iniconfig==2.3.1
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
maf-sandbox==0.48.0
maf-sandbox-bicep==0.22.2
maf-sandbox-docker==0.27.0
maf-sandbox-mcp==0.0.0
mcp==1.28.1
msgspec==0.22.0
opentelemetry-api==1.45.1
packaging==26.3
pluggy==1.6.0
pycparser==3.11
pydantic==2.14.1
pydantic-core==2.50.1
pydantic-settings==2.15.0
pygments==2.21.0
pyjwt==2.15.1
pytest==9.0.3
python-dotenv==1.2.4
python-multipart==0.0.32
pywin32==312
pyyaml==6.0.3
referencing==0.37.0
regex==2026.9.29
rpds-py==2026.9.1
sse-starlette==3.5.0
starlette==1.7.0
typing-extensions==4.16.0
typing-inspection==0.4.4
uvicorn==0.54.0
```

### Reproduction

Build using `uv build --package maf-sandbox-mcp`, then create a fresh Python environment outside the checkout and install the wheel plus `pytest==9.0.3` and `httpx==0.28.1`. Do not use workspace editables or `PYTHONPATH`. The [installed checker](../../../tests/fixtures/openclaw_installed_worker_check.py) fails on changed baselines, missing runtime modules, differing installed bytes, local dependency fallbacks and incomplete test/cleanup evidence. Use a clean committed checkout and a fresh private evidence directory for each attempt:

```bash
"$WORKER_PYTHON" "$REPO/tests/fixtures/openclaw_installed_worker_check.py" --wheel "$WORKER_WHEEL" --root "$NEW_EVIDENCE" --config "$BICEP_CONFIG" --image "$BICEP_IMAGE_ID"
```

For observed Gateway acceptance, retain the isolated pinned-Gateway setup described earlier, use the fresh environment's Python to launch `openclaw_http_observer.py` with `--installed` and the existing observer arguments, then run `openclaw_gateway_http_check.py --installed` with the existing checker arguments. The prototype argument is retained for compatibility with the historical runner; installed mode imports and hashes the wheel modules, not that source path. Stop the observer through its stop file and verify its zero exit and empty shutdown record.

For console acceptance, launch `maf-bicep-mcp --transport http` from the installed environment as described in the [operator guide](../../../packages/maf-sandbox-mcp/README.md). Start the pinned Gateway and deterministic provider with fresh isolated state and credentials. Resolve the image before launch and pass the identical identity to the checker:

```bash
"$WORKER_PYTHON" "$REPO/tests/fixtures/openclaw_installed_worker_check.py" --wheel "$WORKER_WHEEL" --root "$NEW_EVIDENCE" --config "$BICEP_CONFIG" --image "$BICEP_IMAGE_ID" --gateway-config "$GATEWAY_CONFIG" --provider-evidence "$PROVIDER_EVIDENCE" --owner "$OWNER_DIRECTORY" --openclaw "$OPENCLAW_PACKAGE"
```

The console checker covers outcomes and prepared resolution; the observer is still required for exact session/dispatch/cancellation attribution. After validation, stop only the test processes and independently verify empty owned-container scope and released owner lock. Do not use forced termination of an idle test process as a claim about graceful shutdown or interruption recovery.

### Final packaged candidate

Candidate `d2068a78` completes the repository package registrations, README conventions and experimental import warning. The warning is confined to stderr, preserves `-W error` imports and leaves MCP stdout empty. Its three execution modules retain the hashes above; `__init__.py` now hashes to `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430`. The wheel was rebuilt and installed into a second empty environment outside the checkout; the complete dependency manifest and its hash are unchanged.

The final wheel repeated all 43 transport cases and 109 retirements, all five Bicep/MAF ownership scenarios, the reachable/denied network control, instrumented two-session Gateway acceptance and the actual installed-console Gateway/prepared-module pass. All passed. Final transport state had zero records/readers/active calls and no poison; ownership shutdown was empty, preserved the unrelated owner and removed the sentinel. Active MAF closure again settled the service before its client: 25.41 seconds versus 240.02 seconds, ending with code 408 while the surviving client completed valid work. The observed service exited 0; the separately idle-terminated Windows console released its owner lock. Both Gateway runs confirmed their test processes stopped. The evidence boundaries and unqualified scenarios above remain unchanged.

| Final artifact | SHA-256 |
|---|---|
| Built worker wheel | `ec256f48e82357bd20bdc89095169173daab43bca80544cbeabd871c39ebda8c` |
| Installed transport/ownership/network report | `234b6a28271fc0b1623ef4f1ab874fc26630f7f1a46e66807e2864da09525665` |
| Instrumented Gateway report | `da9c7bfd24f35456ba08c94d020751b3ec3b8e36ddaf7ac2d0ca42f23661c531` |
| Console Gateway report | `3981c1823d3bc846211bcf6b28f02a057efed71e64746fa2127f82ddc84d394f` |

### Prepared-core compatibility reconciliation

The merged #1900 adoption moves the workspace core to MAF 1.21.0. The worker's direct framework requirement therefore permits `>=1.20.0,<1.22`; the core dependency still selects the narrower installable pairing. Its ordinary index installation remains core 0.48.0 and MAF 1.20.0, while CI's prepared-core override can exercise core 0.49.0 and MAF 1.21.0. This does not move either end of the worker's core range or claim published core 0.49 support.

The adjusted wheel is `97585f8326972d1aa275cf7827e2c6b5ff1edcaf023d66c862b5a5d9dbe69c0c`. A third clean external index installation reproduced the complete manifest and all four runtime hashes of the final runtime candidate above, and its installed entry-point smoke passed. The artifact/manifest reconciliation report hashes to `dd7b73dc6dd04556f7d970152312bee34e3c50acf68676454a48fe91b6476653`. The Docker/Gateway observations remain attributed to the unchanged runtime and recorded earlier wheel; they were not independently repeated after this metadata-only adjustment.

Separately, all 15 sandbox wheels installed together with the prepared local core, and the worker's clean-wheel smoke passed with MAF 1.21.0. On that workspace baseline, 1,078 focused service/prototype/qualification/package tests passed, with four opt-in tests skipped. These establish offline and installation compatibility for the prepared pairing; the live Gateway/Bicep qualification remains the MAF 1.20.0 baseline above.

### Review candidate requalification (2026-10-13)

Candidate `c05c84155cff12c360c0fd2d944edb1d02db94bf` changes two runtime paths: stdio shutdown raises on unconfirmed final cleanup, and authenticated non-GET requests to `/ready` return 405. The historical source specimen remains frozen. The earlier artifact hashes still describe their recorded candidates; they do not describe this revised runtime.

Before the fixes, the new shutdown/readiness regressions produced 17 failures and 22 passing controls. The affected workspace suites then passed with 229 tests and 14 skips. A fresh external installation of the revised wheel passed all 39 new regression cases against the recorded MAF 1.20 dependency baseline. Those are synthetic cleanup/readiness checks, separate from the real Docker observations below.

The revised installed wheel repeated all 43 transport cases and 109 SDK retirements, all five real Bicep/MAF ownership scenarios, the reachable/denied network control, instrumented two-session Gateway acceptance and the actual installed-console Gateway/prepared-module pass. All passed on the same Python, Docker, OpenClaw, image/configuration and complete dependency baseline recorded above. Transport and ownership shutdown ended empty and unpoisoned; the unrelated owner survived and the sentinel was removed. Active MAF closure again settled the service before the client: 24.56 seconds versus 240.01 seconds, with client code 408 and a successful survivor call. The instrumented service exited 0. The console was terminated while idle on Windows, exited 1 and released its owner lock; that is still not graceful-console-shutdown evidence. Both Gateway runs confirmed all their test processes stopped. Historical `complete_matrix: false` limits remain in force.

| Revised artifact | SHA-256 |
|---|---|
| Built worker wheel | `b024019f7e0987556c792bca27bb0002dac5aa6e7e6a6f4a19385a7df7f7baa5` |
| `maf_sandbox_mcp/__init__.py` | `6cd4609954365facc1edf656bfffc2366bf5da541f1fc699b88122dd007f2430` |
| `maf_sandbox_mcp/server.py` | `818dd044894a90881cc46f467c6efd16767f532a382f4d4a1706d2cfd94c3ae2` |
| `maf_sandbox_mcp/workload_http.py` | `f020d90da0e670cfc4bdbbdd7f131a3b84cde83cb8d876021b4ae4d84180d599` |
| `maf_sandbox_mcp/workload_service.py` | `8972bc76c8ef9f990f4f0a4cbf3f6704b0e5752bb2b42aaa9f7dfcd0a35a45e0` |
| Installed transport/ownership/network report | `828cd2119b717e12268ade24782056997e53930fe7b6a6eed363960683361add` |
| Instrumented Gateway report | `055f92f3ee6ad4d20ad65184f2fdc09a7d8a545b7423555f989d4fef8385f187` |
| Console Gateway report | `66c033748d11fc79f6be87ab1fc5b18b8e3888584e8b60407b333e452827f454` |


## Published candidate conformance campaign (2026-10-16)

**Historical v1 evidence, superseded by the reviewed v2 contract below.** PR #1983 review identified missing transitive/native byte checks, incomplete source/control correlation, auxiliary-log bounds and interrupted-stage cleanup gaps. The original report and its evaluator result are retained unchanged; the current evaluator refuses that profile.

The repository-local runner for #1972 completed `installed-bicep-windows-v1` on one fresh external installation, with campaign `fce960ecf9f64cb794a4af2a69c03ca4` and candidate SHA-256 `1d2c51882a090bccab8f12ca04a000c65c0ea9dc2386a594d10fa10d836268f8`. Independent evaluation returned `qualified_for_profile` for all eight required groups. This is new evidence for the published worker; none of the historical source or wheel reports above was relabelled or reused.

The retained [expected candidate](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows/candidate.json), [selected plan](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows/plan.json), [complete summary](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows/evidence/summary.json) and [evaluation](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows/evaluation.json) bind the artifact records in the adjacent evidence directory. The summary SHA-256 is `c0f75f4f8673635280babce5ce1c9a0e697cf8935ab4e252363429e778348c35`. The candidate includes every resolved Python distribution, both dependency-lock hashes, installed worker module hashes, npm archive/tree identity and host configuration. Private setup/campaign directories retain the original locks and diagnostics; the public records contain no operator paths, credentials or provider state.

| Candidate dimension | Observed value |
|---|---|
| Runner | Commit `75b9819dc35c01aa49e1e31de44e67685827912c`; runner/adapter content SHA-256 `0da7026e013706117d1c4b5eb870aab20da76a9236f504d72ccdbb42c06b2149` |
| OpenClaw | Installed npm release 2026.9.7; 13,280 runtime files checked against the integrity-pinned archive, before and after the campaign; only the pinned installer's lifecycle-marker removal is allowed |
| Published worker | `maf-sandbox-mcp` 0.1.0; wheel SHA-256 `1228f030135da4e3c5797ec76e19c34275fb7bba20fbc55d3d42d6cc43955529`; installed module origins and bytes verified |
| Dependency selection | MAF 1.21.0, core 0.50.0, Bicep 0.23.0, Docker backend 0.28.2, MCP 1.28.1; complete versions and hashed index resolution in the candidate |
| Host/engine | Windows 11 AMD64, Python 3.13.15, Node 26.8.1, Docker 29.8.2, Linux x86_64 engine with kernel 7.0.14-linuxkit |
| Compiler image | `sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e` |
| Compiler configuration | SHA-256 `d3f160d081dcf48e41f0f0cdf006595b380402ac9730f3ee920738ba616d87e1` |

| Evidence group | Accepted observation |
|---|---|
| Installed identity | Exact complete dependency manifest, worker wheel/modules and npm runtime bytes; isolated imports from the external installed environment, repeated after runtime execution |
| Discovery | Worker-module imports caused no observed guarded side effects; a separate late-exit control refused activity after the report was emitted |
| Policy/input refusal | Missing/oversized configuration, mutable image and malformed authenticated input refused before workload allocation; positive compiler control establishes that work was reachable |
| Transport | All 43 exact expanded cases passed with setup/call/teardown records; 109 SDK retirements settled; socket and direct-ASGI attribution retained |
| Bicep/MAF ownership | All five real compiler scenarios passed: equal IDs/repeated cancellation, response-only loss, idle MAF close, active MAF close and ordinary close; surviving sessions completed valid work |
| Gateway | Two real sessions each projected one valid, invalid and explicitly incomplete result; installed console entry point and prepared AVM module execution passed with the deterministic local provider |
| Network | The selected image reached the nonce-bearing host TCP control on bridge networking and could not reach it with networking disabled; control resources were removed |
| Cleanup | Owned process supervision settled, owner scopes were independently empty, an unrelated sentinel survived and was then removed; no emergency cleanup was needed |

Active MAF closure settled the service in 20.53 seconds, while the pending caller settled at its 240.02-second deadline with code 408. Its surviving peer completed valid work. Response-only loss returned one attributed, explicitly incomplete binding result with confirmed cleanup, without replay or implicit cancellation. These are measured limitations and outcomes, not guarantees of prompt client cancellation or successful compilation after a lost response.

The [development-attempt metadata](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows/development-attempts.json) retains two separate failed campaign summaries: an evaluator that incorrectly required response-loss compilation to complete, and a superseded runner rejected when its fingerprint changed. Their full private evidence directories remain separate. The accepted campaign used a fresh installation and fresh output directory after the corrections; it does not combine successful stages from those attempts.

The historical bundle remains inspectable, but re-evaluation requires its recorded runner revision. It does not satisfy the current contract.

The [operator guide](../openclaw.md#installed-bicep-conformance-runner) describes fresh preparation and execution. Expected inputs must be selected independently and trusted: hashes establish byte correlation, not the truth of a forged observation. The evaluator fixes required groups and case identities and refuses missing, duplicated, transplanted, malformed, narrowed or failed evidence; offline adversarial tests also cover boolean count substitutions, late import effects, changed installed bytes and all-denied network controls. PR CI exercises these offline checks, not the opt-in Docker/Gateway campaign.

This profile is finite, supervised Windows/Docker deterministic evidence. Process supervision confirms stopped processes and released ownership; it does not establish graceful console shutdown, unattended recovery, a model's reasoning, Linux-host support, native MXC/file boundaries, credential delegation, package publication or production acceptance. Worker owner-loss qualification remains #1973; native qualification remains conditional in #1974, credential enforcement remains blocked in #1952, additional tools remain deferred in #1639, and full-backend design remains #1642.


## Reviewed v2 conformance campaign (2026-10-16)

**Historical v2 evidence, superseded by the v3 contract below.** The next review identified missing retained network image identity, unbound Gateway session/turn records, duplicate exchange counting, nonfinite float parsing, premature summary success and preparation/cleanup output-limit gaps. These records remain unchanged; the current evaluator refuses the v2 profile.

The reviewed `installed-bicep-windows-v2` contract requires archive-backed dependency and native assets plus a complete preflight/postflight inventory. Each ownership survivor identifies the fixed simple source; each of six Gateway outcomes retains its image, configuration and source digest; prepared-module qualification requires the fixed AVM source digest shared with the live probe. Ownership checks correlate session/request IDs, accepted cancellation responses, binding cancellation, busy admission, DELETE retirement and survivor ordering. Idle close must preserve the original peer call without an exception.

The file inventory covers the prepared Python environment's `Lib/site-packages` and `Scripts` trees and OpenClaw's complete `node_modules` tree. The host Python installation, Node executable, Docker engine and operating system remain host prerequisites recorded by version; this is not an attestation of those host binaries. The only archive transformations accepted are the pinned OpenClaw lifecycle-marker removal, wheel script relocation with identical bytes, and npm's first-line CRLF normalization for archive-declared executables.

The parent bounds outer, worker, Gateway and observer logs during execution and after process-job cleanup. Before creating resources it persists the exact names and campaign label of the outer sentinel, ownership sentinel and two network controls. Cleanup attempts every recorded owner and control resource independently, retains observation/removal failures, and marks emergency reclamation as a failed campaign. It never sweeps a global container-name prefix.

The fresh v2 candidate is `293320ccbd77ac0cc1522dfd3e8fe839c0cea352236833d39e1a908a605c1eaf`, prepared from runner commit `95ec72deae02e240cc19a176d6b226ee58186224` with runner fingerprint `4fcfefc1012f2e472efb92015925d287228370134180956850e57a8aef29123a`. It contains 43,862 inventoried files: 43,597 verified against 388 locked archives, plus 265 explicitly listed installer outputs. The selected published dependency versions remain worker 0.1.0, core 0.50.0, Bicep 0.23.0, Docker backend 0.28.2, MAF 1.21.0 and MCP 1.28.1.

Independent evaluation returned `qualified_for_profile` for campaign `608ffe97c305475a9e323deb44c9ab2b` across all eight required groups. The [candidate](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2/candidate.json), [plan](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2/plan.json), [evaluation](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2/evaluation.json) and complete bounded observations are retained together. All 43 transport cases and 109 SDK retirements, five real ownership scenarios, six Gateway outcomes, prepared-module execution and paired network controls passed. Fresh postflight verification matched the original runtime inventory; independent cleanup found empty owner scopes, stopped processes, preserved then removed the sentinel, and required no emergency reclamation.

Active MAF close settled the service in 19.84 seconds and the caller in 240.02 seconds, with error code 408; its surviving peer completed valid work. The response-loss result was explicitly incomplete with confirmed cleanup and no replay or implicit cancellation. These observations preserve the client-timeout limitation.

The [separate live interruption control](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2/interruption-control.json) killed a supervised child after resource creation and injected a sentinel-observation failure. All four recorded controls were reclaimed while an unrelated container survived; the observation error and emergency-cleanup flag remained, so this control cannot qualify as a successful campaign. The [development-attempt metadata](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2/development-attempts.json) retains the refused installer-layout checks, a superseded campaign rejected by its runner fingerprint, and a campaign refused after its isolated supervisor added startup bytecode. Full private diagnostics remain separate, and no earlier stage is reused in the accepted campaign.

Historical v2 evaluation requires its recorded runner revision. The following offline command applies only at that revision; the current evaluator refuses v2:

```powershell
$record = 'scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2'
uv run --no-project --python 3.13 python -m scripts.openclaw_conformance evaluate --bundle "$record/evidence" --plan "$record/plan.json" --manifest "$record/candidate.json"
```

That historical campaign qualified its supervised Windows/Docker candidate under v2 with a deterministic provider. Unattended parent/owner loss remains #1973, native qualification remains conditional in #1974, credential enforcement remains blocked in #1952, additional tools remain deferred in #1639, and broader backend design remains #1642.


## Reviewed v3 conformance campaign (2026-10-17)

**Historical v3 evidence, superseded by v4 below.** Review found unbounded discovery/policy subprocess capture, permissive parsing inside the provider tool-result string, and missing request/response correlation for active and surviving bindings. These records remain unchanged and require their recorded runner revision; the current evaluator refuses the v3 profile.

The `installed-bicep-windows-v3` contract retains the inspected candidate image for both network controls. Each of six Gateway outcomes carries a distinct turn and its session, linked to the outgoing request and both original provider observations; the evaluator reconstructs the result from the provider record. Cancellation requests and admission observations must identify distinct exchanges. The shared JSON reader refuses overflow numbers as well as literal nonfinite values, including in otherwise unused fields.

Preparation counts stdout and stderr while the owned process tree runs. Overflow or deadline stops that tree; a normal parent exit also reclaims remaining descendants. Cleanup uses independently bounded command diagnostics, so an exhausted setup log cannot hide a successful Docker observation or prevent removal. The original diagnostics failure remains recorded. A saved summary stays unqualified until complete evaluation succeeds, including the aggregate byte budget for the final success summary.

The [separate live diagnostics control](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3/diagnostics-cleanup-control.json) began with an 8,388,609-byte setup log against the 8,388,608-byte limit. Cleanup reclaimed all four recorded Docker resources, preserved an unrelated container and left the oversized log unchanged. The retained `setup_log_limit` error and emergency-cleanup flag keep that control unqualified. Offline regressions also exercise noisy stdout/stderr, deadlines and descendant reclamation while preserving an unrelated process.

Fresh candidate `0c58788bb3e455a5f668e1a96e8fafe5f914d81e790b83b0808e8607db1ba769` was prepared from frozen runner `e96c6878d80c0c3ffe3c10feeeb0dc2588340efd` with fingerprint `6a30c1954db6578447ca512e99e09a8a92a904e8ca9ac7a72f039a2a85a03a81`. The inventory contains 43,862 files, including 43,597 archive-verified files from 388 locked archives and 265 explicitly listed installer outputs. Published versions remain OpenClaw 2026.9.7, worker 0.1.0, core 0.50.0, Bicep 0.23.0, Docker backend 0.28.2, MAF 1.21.0 and MCP 1.28.1.

Independent evaluation returned `qualified_for_profile` for campaign `dd75630a7a8f4a6b8b04ea06b2953a4b` across all eight groups. The [candidate](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3/candidate.json), [plan](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3/plan.json), [evaluation](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3/evaluation.json) and complete bounded observations are retained together. All 43 transport cases and 109 SDK retirements, five real ownership scenarios, six correlated Gateway turns across two sessions, prepared-module execution and both image-bound network controls passed. Postflight inventory matched the original candidate; final cleanup found empty owner scopes, stopped processes, preserved then removed the sentinel, and required no emergency reclamation. No earlier campaign stage was reused.

Active MAF close settled the service in 20.50 seconds and the caller in 240.02 seconds, with error code 408; the peer completed valid work. This remains deterministic Windows/Docker evidence for one installed candidate. Real-model behavior, Linux, unattended owner loss (#1973), conditional native qualification (#1974), credential enforcement (#1952) and production acceptance remain outside this result. Additional tools remain deferred in #1639 and broader backend design remains #1642.

Re-evaluate the historical v3 report at its recorded runner revision without Docker or installed OpenClaw:

```powershell
$record = 'scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3'
uv run --no-project --python 3.13 python -m scripts.openclaw_conformance evaluate --bundle "$record/evidence" --plan "$record/plan.json" --manifest "$record/candidate.json"
```


## Reviewed v4 conformance campaign (2026-10-17)

The `installed-bicep-windows-v4` contract supervises discovery and policy probes with streaming stdout/stderr limits while preserving their expected exit statuses. Docker inventory observations inside those stages use the same bounded command supervisor. Embedded Gateway tool-result JSON uses the same duplicate-key and finite-number checks as outer evidence before projection discards fields.

Every credited active binding must follow its matching transport request. A surviving result must follow the latest matching binding start and precede the unique successful response on that request's exchange. Request IDs may be reused after completed exchanges; the evaluator rejects stale, missing, ambiguous and mismatched exchanges. The process-tree regression publishes its child PID atomically before the parent can signal completion.

Fresh candidate `2e02925fe204842f5809e9eaf9ac094ca0c8747d5a8e84cdfcecdbc1075a6c33` was prepared from frozen runner `749c60048cee19a48f9d2ca41a65d0c7d08c9b9c` with fingerprint `b4775bdffba41481e53245a46d30e4b485d47f14926bc073b8360d710d3e491d`. The inventory contains 43,868 files: 43,603 archive-verified files from 388 locked archives and 265 explicitly listed installer outputs. Published versions remain OpenClaw 2026.9.7, worker 0.1.0, core 0.50.0, Bicep 0.23.0, Docker backend 0.28.2, MAF 1.21.0 and MCP 1.28.1.

Independent evaluation returned `qualified_for_profile` for campaign `6bdc5a6ece204ffc9cdbf36df99870ac` across all eight groups. The [candidate](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4/candidate.json), [plan](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4/plan.json), [evaluation](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4/evaluation.json) and complete bounded observations are retained together. All 43 transport cases and 109 SDK retirements, five real ownership scenarios, six correlated Gateway turns across two sessions, prepared-module execution and both image-bound network controls passed. Postflight inventory matched the candidate; final cleanup found empty owner scopes, stopped processes, preserved then removed the sentinel, and required no emergency reclamation. No earlier campaign stage was reused.

Active MAF close settled the service in 20.89 seconds and the caller in 240.18 seconds, with recorded error `ToolExecutionException` and code `408`; the peer completed valid work. This remains deterministic Windows/Docker evidence for one installed candidate. Real-model behavior, Linux, unattended owner loss (#1973), conditional native qualification (#1974), credential enforcement (#1952) and production acceptance remain outside this result. Additional tools remain deferred in #1639 and broader backend design remains #1642.

The [separate v4 live diagnostics control](../../../scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4/diagnostics-cleanup-control.json) began with an 8,388,609-byte setup log against an 8,388,608-byte limit. Cleanup reclaimed all four recorded resources, preserved the unrelated container and retained the diagnostics error and emergency-cleanup flag. The control verified final removal of its resources and unrelated container; it remains explicitly unqualified.

The ownership evaluator also requires one binding start and one correlated terminal event per executed transport exchange, forbids binding execution on refused admissions, and checks case-specific ordering of active work, DELETE, cancellation, admission probes and completed retirement, and correlated successful A and B exchanges before ordinary close. The exact per-session binding counts also reject extra executed exchanges. B's survivor exchange starts after A retires in each MAF close scenario. Synthetic controls and mutations of the retained v4 observations reject missing pre-close work, reversed lifecycle events, duplicate starts at equal or distinct timestamps, duplicate or conflicting terminal results, orphaned events and work on refused exchanges; completed exchanges still permit request-ID reuse. The unchanged v4 report passes these stronger offline predicates. This is re-evaluation of the recorded candidate, not a new live campaign or qualification of the branch's upgraded MCP dependency.

Re-evaluate the retained v4 report without Docker or installed OpenClaw:

```powershell
$record = 'scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4'
uv run --no-project --python 3.13 python -m scripts.openclaw_conformance evaluate --bundle "$record/evidence" --plan "$record/plan.json" --manifest "$record/candidate.json"
```

The shared Gateway checker rejects error-marked envelopes, MCP results and details before normalizing a successful workload outcome, and requires exactly one text content item. Synthetic and retained-observation mutations cover each error signal independently. The command supervisor reclaims its owned descendants as soon as the parent exits, then drains its bounded pipes; a standard-library process control verifies successful exit with inherited pipes, captured output and survival of an unrelated process. These checks strengthen offline validation and process supervision without changing the recorded v4 campaign.

Offline qualification enforces the 16 MiB event budget across all artifacts, including before finalization changes the summary to qualified; exact-budget controls still pass. Every retirement must retain the expected sibling count and active-call identity, including B remaining active while idle A closes. The complete pinned `structuredContent:` text projection must strictly parse and equal the details object, so duplicate keys, nonfinite values and contradictory model-facing results cannot be hidden by normalization. Synthetic and retained-v4 controls cover these predicates; the recorded campaign remains unchanged and still passes offline evaluation.

DELETE exchanges share the campaign-wide bounded identity namespace with transport requests. Each retirement requires its own successful DELETE response before the snapshot; orphan retirement events are refused. Busy responses must precede their recorded admission observations, which in turn precede the active binding terminal. Synthetic and retained-v4 mutations cover absent, empty, oversized and reused identities, missing or contradictory DELETE responses, and late busy responses. These are additional offline predicates, not a new live campaign.
