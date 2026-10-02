# OpenClaw Gateway qualification of the Bicep prototype

This records supervised execution on 2026-10-03 through the real OpenClaw Gateway and its embedded agent runtime. A deterministic local provider selected tools and captured the results sent back to the provider; it was not an LLM and establishes no evidence about model reasoning or prompt-injection resistance. The service used the published dependency pins in the [prototype](../../../samples/experimental/openclaw_bicep/README.md), launched with `uv run --script`. The broader [integration research](openclaw-integration.md) owns the design direction.

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

The prototype therefore supports one active OpenClaw session per retained owner directory. Do not weaken the lock, delete it or generate untracked owner directories to make another session start. Those changes would break cleanup ownership. [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) compares a separately supervised loopback Streamable HTTP service with one admission/recovery owner, a host-controlled per-session stdio launcher with durable recovery accounting, and a supported host extension lifecycle. No transport migration is selected or implemented by this qualification.

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

## Status

| Work | State | Tracking |
|---|---|---|
| Bicep host qualification | One-session Gateway agent path qualified with a deterministic provider; actual LLM behavior and full crash recovery remain unqualified | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| Multiple Gateway sessions | Design required; shared owner directory rejects a second retained stdio worker | [#1665](https://github.com/sokolaidev/maf-extensions/issues/1665) (open) |
