# Repair an architecture diagram on ACAS

The model reads [architecture.md](architecture.md) and creates draw.io XML. The host changes the Orders API edge to reference a nonexistent database vertex, submits that XML to the real `create_drawio` tool, and sends the resulting diagnostic back to the model. At most three repair attempts follow. Success requires the converter's artifact to arrive in the store backing `file_access_read`, a real read returning those exact bytes, and preservation of all three components and both connections.

The PEP 723 block in `agent.py` declares the published packages needed by this sample. `uv` resolves those packages into an isolated environment.

## Network and image

Guest egress is **closed**: `drawio_sandbox_spec` has an empty allowlist, and ACAS maps it to `default_action="Deny"` with no allow rules. The sample checks that contract before constructing the backend. There are no runtime package installs, external modules, or host-tool callbacks. The model endpoint, Azure authentication, sandbox control-plane traffic and file storage are host-side operations; closed guest egress does not make those host operations offline.

Build [the draw.io image](../../images/drawio-sandbox/Dockerfile) with Python 3 and Graphviz already installed. From the repository root:

```bash
docker build -t <registry>.azurecr.io/drawio-sandbox:<revision> images/drawio-sandbox
docker push <registry>.azurecr.io/drawio-sandbox:<revision>
```

Import that image into an existing ACAS sandbox group before running the sample. Follow the [image import procedure](../../images/bicep-sandbox/README.md#import-it-into-the-sandbox-group), substituting the draw.io image reference, or use the [ACAS disk-image import script](../../packages/maf-sandbox-acas/scripts/README.md). A registry image is not itself an ACAS disk image. Use an immutable revision or digest; no image is built, pushed or imported by this sample. Build-time downloads and the service's image import happen before guest execution and do not require widening guest egress.

## Configuration

An existing ACAS group, imported image and Azure OpenAI deployment are required. Authentication uses `DefaultAzureCredential`, including an `az login` session. Running the sample creates billable ACAS sandboxes and makes model calls.

| Variable | Purpose |
| --- | --- |
| `ACAS_SANDBOX_ENDPOINT` | Sandbox group data-plane endpoint |
| `ACAS_SANDBOX_SUBSCRIPTION_ID` | Subscription holding the group |
| `ACAS_SANDBOX_RESOURCE_GROUP` | Resource group holding the group |
| `ACAS_SANDBOX_GROUP` | Existing sandbox group |
| `DRAWIO_SANDBOX_IMAGE` | Image reference used when importing the draw.io disk image |
| `ACAS_SANDBOX_REGISTRY` | Optional registry for a bare `repository:tag`; omit for fully qualified references |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Deployment supported by `OpenAIChatClient` |

From the repository root, after setting these variables:

```bash
uv run --no-project samples/18_acas_drawio_repair/agent.py
```

Missing configuration exits with code 2 before creating resources. The model and converter sequence has a ten-minute deadline. Invalid initial architecture, exhausted repairs, unverified storage read-back, or incomplete cleanup exits nonzero. Each conversion uses the kind's default execution timeout and disposal policy; the final scope purge runs even when per-call cleanup already disposed every sandbox.

## Storage and evidence

`make_file_store_sink` writes the converter's UTF-8 artifact under its host-generated `<call_id>/diagram.drawio` path in a fresh `InMemoryAgentFileStore`. One read-only `FileAccessProvider` uses that same store; model write and delete tools are disabled. The host checks the saved content against the delivered bytes and matches a `file_access_read` function result to both the exact path and content. A model claim that it saved or read the file cannot satisfy the check.

The successful artifact exists until read-back completes. Cleanup then deletes every destination this execution attempted, including a write that partially succeeded before raising, and checks that those files are absent. Existing files are refused before they enter the deletion set. There are no staged architecture inputs in storage; the Markdown is passed directly in the prompt. For a persistent application store, retain the same per-call ownership and cleanup rules. The sample deliberately leaves no downloadable output behind after the test.

The sample accepts plain-text labels, native geometry and a small allowlist of built-in styles. It checks the authored XML and the converter artifact before writing to storage; links, image references, custom fonts, scripts and unknown attributes or styles are refused. This is the sample's fixture policy, not a restriction on the draw.io package's general XML format.

Host `[measured]` JSON records identify configuration, the authored XML hash, the deliberately broken edge, the converter's rejection, each repaired XML hash, successful storage/read-back, storage cleanup, sandbox cleanup and final completion. Every validation record binds the exact submitted XML hash and returned diagnostic to its observed call ID. Each repair record identifies the diagnostic passed to the model, and the checker requires it to match the preceding validation result. Model output is printed separately with measurement-like lines quoted. The model receives the corrupted XML and the actual converter diagnostic with the original architecture on every repair attempt. The host never restores the original edge itself. The exact fixture IDs and labels are part of the sample's task contract, not a general restriction of the draw.io kind.

## Verification

Core's `SandboxObserver.tool_call_ended` emits a `tool_call_ended` JSON record for every `create_drawio` call, including validation rejection. Its `seconds` value covers the tool body and cleanup: acquisition, file transfer, conversion, output collection and sandbox disposal. It excludes the model's authoring, repair and read-back turns. `call` joins a successful call to its stored artifact. `failure` describes a raised Python exception; a returned XML validation diagnostic is recorded with `failure: null`.

Offline tests exercise deterministic corruption, the real packaged converter through core's in-process backend, both successful and failing repair sequences, file-access result attribution, ACAS policy construction, and cleanup. They do not contact ACAS or a model:

```bash
uv run --locked pytest -q tests/test_sample_acas_drawio.py
```

The opt-in live test launches the actual sample with the configured Azure model and ACAS group, verifies host evidence and requires cleanup before success. It creates billable resources; ordinary PR checks skip it. It verifies the closed-egress configuration and successful conversion under that policy, without claiming an independent network-escape probe.

```bash
MAF_ACAS_DRAWIO_LIVE=1 uv run --locked pytest -q tests/test_sample_acas_drawio_live.py
```

In PowerShell, set `$env:MAF_ACAS_DRAWIO_LIVE = "1"` before running the same pytest command. A killed process cannot run `finally`; ACAS's configured auto-suspend and auto-delete timers remain the backstop. Cleanup errors are failures, not proof that resources were removed.

The live test prints every call's timing even when pytest captures passing tests. The `sample-18` job in [Verify (live)](../../.github/workflows/verify-live.yml) runs the sample against published packages by default, or checkout packages with `source: branch`, when `package` is empty or selects `maf-sandbox`, `maf-sandbox-acas` or `maf-sandbox-drawio`. Configure `DRAWIO_SANDBOX_IMAGE` on the `live-verify` environment with the already-imported image reference. Each call's `seconds` appears in the job log, retained for seven days as the `drawio-live-log` artifact. Releases of core, ACAS and draw.io trigger the job through the publish workflow, alongside manual live verification. The workflow creates no registry or disk images and does not change guest egress.
