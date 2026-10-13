# OpenClaw Bicep end-to-end sample

Run two turns through a real OpenClaw Gateway, an installed `maf-bicep-mcp` worker, and the Bicep compiler in Docker. The first submits [`main.bicep`](main.bicep), whose output has the wrong type. The second changes `int` to `string` and validates the repair. No Azure resources are deployed.

**You do not need an existing OpenClaw installation.** The launcher installs OpenClaw 2026.9.7 inside the output directory, creates an isolated configuration and session, and starts both services. Smoke mode uses a deterministic local model substitute and needs no model account. Model mode sends the same task to an operator-selected streaming Chat Completions endpoint.

This is experimental because `maf-sandbox-mcp` is not yet published. Supply its locally built wheel; the launcher installs it into a fresh environment outside the checkout, verifies its installed module hashes against the wheel, and obtains its dependencies from the package index. The worker console discards inherited Python import overrides so a parent `PYTHONPATH` cannot substitute source for the installed worker. The historical [source prototype](../openclaw_bicep/) and the [worker package](../../../packages/maf-sandbox-mcp/README.md) remain separate. Promotion to a numbered, published-package sample and a normal live verification job follows publication.

## Prerequisites

- `uv`, `node` and `npm` on `PATH`. The pinned OpenClaw package requires Node `>=24.16.0 <25` or `>=26.1.0`. The launcher asks uv for Python 3.13.
- A reachable Docker daemon running Linux/amd64 containers, and a pulled or locally built prepared Bicep image. Follow the [AVM consumer guide](../../../images/bicep-sandbox/AVM.md) to verify the public release; its registry digest is distinct from a local Docker image ID.
- Network access for the initial npm/Python installation and image pull. Bicep tool calls use the worker's closed-network guest configuration. Smoke mode makes no external model request.
- A new, private output directory outside this checkout. It retains credentials, source, model responses, logs and OpenClaw state; do not upload it or place it in a shared directory. POSIX permissions restrict newly created state; on Windows choose a directory protected by your user's ACLs.

## Run without a model account

From the repository root, in PowerShell:

```powershell
uv build --package maf-sandbox-mcp
$image = 'ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e'
docker pull $image
$output = Join-Path $env:TEMP ('openclaw-bicep-' + [guid]::NewGuid().ToString('N'))
uv run --no-project samples/experimental/openclaw_bicep_e2e/agent.py --wheel dist/maf_sandbox_mcp-0.0.0-py3-none-any.whl --image $image --output $output
Get-Content (Join-Path $output 'report.json')
```

Or in a POSIX shell:

```bash
uv build --package maf-sandbox-mcp
image='ghcr.io/sokolaidev/maf-extensions/bicep-avm@sha256:b3f14b266052def3f7b5a7b6ca4fb38f06acdff0e99e7df2437dd4f0c6b0b82e'
docker pull "$image"
private_parent=$(mktemp -d)
uv run --no-project samples/experimental/openclaw_bicep_e2e/agent.py --wheel dist/maf_sandbox_mcp-0.0.0-py3-none-any.whl --image "$image" --output "$private_parent/result"
cat "$private_parent/result/report.json"
```

Use the wheel filename produced by your build if its version differs. Setup can take several minutes. `setup.log`, `worker.log` and `gateway.log` are in the output directory. Every attempt needs a fresh output directory; the launcher refuses reuse. To reuse only an already provisioned OpenClaw package, add `--openclaw <previous-output>/openclaw/node_modules/openclaw`. Its version must still match the pin; configuration, sessions, owner state and the worker environment are always new.

The worker environment pins core 0.48.0, Bicep 0.22.2, Docker backend 0.27.0 and MAF 1.20.0, matching the installed qualification baseline. Other dependency versions are resolved at installation and recorded in the report; this is not a complete transitive lock. A worker or OpenClaw upgrade needs fresh qualification. npm dependency lifecycle scripts are disabled; the launcher explicitly runs the pinned OpenClaw package's bundled-plugin setup script.

## What passes

Both turns use one Gateway session through a stable HTTP `user` value. The only admitted workload tool is `bicep__bicep_validate`. OpenClaw can expose it through its compact `tool_call` surface; the sample records its actual arguments and the Gateway-projected tool result when the provider receives the follow-up request. Assistant prose cannot supply acceptance evidence.

The launcher requires:

1. An actual tool result for the first turn, with the original source, `completed: true`, `status: ok`, and `verdict: invalid`.
2. An actual tool result for the repair turn, ending with `verdict: valid` and the single declaration `output greeting string = 'hello'` (whitespace may differ). A real model may try several repairs, but every observed call must complete and confirm cleanup.
3. Matching image identity, configuration hash and submitted-source hash on every observed result, plus `cleanup: confirmed` and zero Docker containers in this worker's exact owner scope after each turn.
4. Stopped service processes, an independently reacquired worker owner lock, and no owned containers at final cleanup. Any cleanup error or emergency container purge makes the result fail.

Successful output includes two `[measured]` lines with the invalid/valid verdicts and a final `passed: true`. The exit code is nonzero on failure. `report.json` retains measured verdicts, identities, dependency versions and cleanup status. `tool-evidence.jsonl` retains the actual calls/results; `answer-1.json` and `answer-2.json` retain assistant responses separately. Missing observations fail rather than falling back to prose.

On normal exit or Ctrl+C, the launcher stops the processes it started and checks its owner scope. Windows uses forced process-tree termination after completed calls; POSIX uses process-group termination. This is not evidence of graceful Windows signals, independent crash recovery, or cleanup after the launcher itself is killed. Keep the private owner directory for diagnosis after interruption; do not delete ownership state as a recovery shortcut. The worker's [ownership contract](../../../packages/maf-sandbox-mcp/README.md) governs recovery.

## Optional real model

Use an endpoint that supports streaming OpenAI-compatible Chat Completions with function tools. Create a local env-file outside the repository with these variables, using your provider's endpoint, model and credential:

```dotenv
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_MODEL=your-tool-capable-model
OPENAI_API_KEY=your-local-credential
```

Then add `--mode model` and load the file through uv:

```powershell
$modelOutput = Join-Path $env:TEMP ('openclaw-bicep-model-' + [guid]::NewGuid().ToString('N'))
uv run --no-project --env-file C:/private/model.env samples/experimental/openclaw_bicep_e2e/agent.py --mode model --wheel dist/maf_sandbox_mcp-0.0.0-py3-none-any.whl --image $image --output $modelOutput
```

For a local compatible server, a loopback `http://localhost:<port>/v1` endpoint is allowed and the key may be omitted. Remote endpoints require HTTPS and a key. Provider-specific APIs, OAuth and existing OpenClaw login profiles are not supported by this sample. It never reads or rewrites your normal OpenClaw configuration. Model mode forwards prompts, tool schemas and results to the configured endpoint and may incur inference charges.

The local authenticated relay records results before forwarding to the real provider. It limits model requests to 32, each request/response to 4 MiB, and socket waits to 240 seconds. The Gateway turn has a 300-second socket timeout. These are finite budgets and socket timeouts, not a production proxy or a total wall-clock deadline. The same evidence checker gates smoke and model modes. A passing smoke report explicitly says `actual_model: false`; it does not establish model reasoning.

## Qualification and limits

On 2026-10-13, the default installation path passed on Windows with Python 3.13.12, Node 26.8.1, OpenClaw 2026.9.7 and Docker 29.8.2 running Linux guests. A fresh worker environment produced one invalid and one valid compiler result, confirmed per-call cleanup, zero owned containers, stopped service processes and a released owner lock. The final source was also rerun with the provisioned pinned OpenClaw package, the documented registry-digest reference, and a deliberately failing shadow module on the parent `PYTHONPATH`. The unsanitized import control failed on that module; the installed console passed both turns and cleanup. The sample's offline tests cover evidence refusal, history attribution/deduplication, deterministic provider HTTP behavior, authenticated relay forwarding and cleanup failure reporting.

No real model was configured for this qualification. The relay's model-forwarding contract was tested with a local synthetic endpoint; that does not qualify any external model/provider. POSIX launcher execution, arbitrary OpenClaw versions, production operation, file/artifact delivery and broader sandbox conformance remain unverified. This sample implements [#1932](https://github.com/sokolaidev/maf-extensions/issues/1932), a narrow follow-up under [#1641](https://github.com/sokolaidev/maf-extensions/issues/1641).
