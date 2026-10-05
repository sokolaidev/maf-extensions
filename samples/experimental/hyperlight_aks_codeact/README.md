# CodeAct on Hyperlight in AKS

A complete application sample for the [Hyperlight AKS deployment](../../../images/hyperlight-sandbox/README.md). A trusted host launches one supervised pod; the application inside it attaches CodeAct to Hyperlight and computes the 100th Fibonacci number. Default smoke mode calls the real tool directly without a model. Model mode asks an OpenAI-compatible model to write and execute the program, then checks the actual tool result and the model's answer.

This experimental sample runs from the locked workspace. The separate [AKS probe](../hyperlight-aks/probe.py) remains the lifecycle and failure-injection harness. This sample demonstrates application wiring, not production sizing or a public execution service.

## Prerequisites

- An operator-prepared Standard AKS cluster following the deployment guide: a verified Linux x86-64 KVM node pool, the pinned device plugin with a free allocation, and a Restricted application namespace.
- Namespace-scoped controller credentials, an explicit kubeconfig/context, and kubectl on the trusted host. Image pull authorization and admission must already permit the final application digest. The launcher does not provision infrastructure or change RBAC/admission.
- Python 3.13, uv, a Linux/amd64 Docker builder, and an approved digest-pinned Hyperlight runtime image built from the same checkout as the controller.

Keep controller credentials outside the application pod. The application needs no Kubernetes API access; it has no service-account token, host mounts or added capabilities. The controller retains the standard aggregate limits: one CPU, 4 GiB memory and 2 GiB ephemeral storage. These are starting budgets, not measured requirements for your application.

## Build the application image

From the repository root, sync the workspace and prepare the base runtime using the deployment guide's build and provenance procedure. Then supply its approved registry digest as `HYPERLIGHT_RUNTIME`. Set `SAMPLE_TAG` to a new image tag in your approved registry.

```bash
uv sync --locked --python 3.13
SAMPLE_DIR=samples/experimental/hyperlight_aks_codeact
MODEL_CONSTRAINTS="$(mktemp)"
uv export --locked --no-emit-workspace --no-hashes --output-file "$MODEL_CONSTRAINTS"
uv pip compile --python-version 3.13 --python-platform x86_64-unknown-linux-gnu \
  --constraint "$MODEL_CONSTRAINTS" --generate-hashes --no-header \
  samples/experimental/hyperlight_aks_codeact/requirements.in \
  --output-file "$SAMPLE_DIR/requirements.txt"
docker build --platform linux/amd64 \
  --build-arg HYPERLIGHT_RUNTIME="$HYPERLIGHT_RUNTIME" \
  --tag "$SAMPLE_TAG" samples/experimental/hyperlight_aks_codeact
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint python \
  "$SAMPLE_TAG" /opt/aks-codeact/agent.py --help
```

The image inherits the runtime's locked dependencies and adds the application, shared sample scaffold, and model client. The generated `requirements.txt` is ignored by Git; compilation constrains every model dependency to the workspace lock and records artifact hashes. Keep that generated file with the application build record. The image checks the model import and dependency consistency at build time and installs no packages at startup. The application starts from its immutable image directory so its sibling scaffold is importable; the PID 1 supervisor still uses Python isolated mode.

Publish and attest the derived image through your approved application-image workflow, then obtain its immutable digest and approve that digest in namespace admission. Set `SAMPLE_IMAGE` to that full registry reference including `@sha256:...`. The base runtime's attestation and payload verifier do **not** authenticate the newly added sample files or approve the derived image. Keep both the runtime build record and the sample source revision with the final application-image provenance. This sample does not publish images or change admission.

## Run smoke mode

Set `CONTROLLER_KUBECONFIG`, `AKS_CONTEXT` and `APP_NAMESPACE` to your existing controller configuration. Choose and record a fresh `SAMPLE_THREAD` for this session; retain it if cleanup needs recovery. Commands below use Bash syntax; PowerShell users can pass the same arguments with their environment-variable syntax.

```bash
uv run --locked python samples/experimental/hyperlight_aks_codeact/launch.py run \
  --kubeconfig "$CONTROLLER_KUBECONFIG" --context "$AKS_CONTEXT" \
  --namespace "$APP_NAMESPACE" --scope samples --thread "$SAMPLE_THREAD" \
  --image "$SAMPLE_IMAGE"
```

The application reads its supervisor-issued binding, uses its scope/thread/agent identity, and explicitly selects aggregate pod containment with `max_worker_memory_bytes=None`. It refuses direct execution outside the supervised pod. CodeAct uses closed guest egress and `Cleanup.RESET`; a final scope purge and backend closure run even when a call fails or is cancelled.

Successful output contains a definitive CodeAct result with `Result: ok` and stdout exactly `354224848179261915075`, followed by confirmed scope disposal and a measured JSON record with `complete: true`, application duration and cgroup peak memory. The launcher returns a confirmed pod result with exit code zero, pod UID, total elapsed time and observed platform controls. Missing or wrong tool output fails even if the model says the right answer. Diagnostics are bounded and may include application/model text.

## Run a model-driven turn

Use an operator-created Secret in the application namespace with exactly these required keys:

| Key | Value |
| --- | --- |
| `OPENAI_BASE_URL` | HTTPS OpenAI-compatible API base URL, including its API path; for Azure OpenAI v1, the endpoint followed by `/openai/v1/` |
| `OPENAI_MODEL` | Model or deployment name supporting tool calls |
| `OPENAI_API_KEY` | API key authorized for that endpoint |

Manage this Secret through your normal secret tooling; never put its values in the image, source, command arguments or a committed manifest. The launcher references only these three keys, replaces any existing entries for those names, never imports arbitrary Secret entries, and does not read Secret values. Namespace policy must authorize the controller to create a workload referencing this Secret. Model access occurs in the trusted application, outside the Hyperlight guest; cluster network policy must permit the chosen HTTPS endpoint and DNS. Closed guest egress does not restrict the host application's model request.

Add `--model-secret YOUR_SECRET_NAME` to the smoke command to enable model mode. This makes billable inference requests. The application checks both the CodeAct tool result and the final answer. It uses the same ownership, containment and cleanup path as smoke mode.

## Recover interrupted cleanup

Exit code 3 means cleanup is pending. Preserve the same namespace, scope, thread and agent; do not remove the ownership ledger or bypass it by starting a replacement session. Reconnect and run:

```bash
uv run --locked python samples/experimental/hyperlight_aks_codeact/launch.py recover \
  --kubeconfig "$CONTROLLER_KUBECONFIG" --context "$AKS_CONTEXT" \
  --namespace "$APP_NAMESPACE" --scope samples --thread "$SAMPLE_THREAD"
```

Recovery requests retirement and waits for confirmed termination. A recovered exit code describes the retired application's outcome, not a fresh successful turn. Permanent node loss still requires the deployment guide's operator fencing procedure; API disappearance alone is not proof that execution stopped. If you supplied `--agent` originally, repeat it during recovery.

## Verification and limits

Portable tests exercise binding refusal, identity/configuration, exact result checks, cleanup on failure/cancellation, Secret-reference injection without weakening the standard pod, and pending-cleanup recovery. They do not execute KVM or contact a model or AKS. The image `--help` check proves imports only. Live acceptance requires both commands above against the approved application image on the intended cluster.

The Fibonacci task is deliberately small. Application duration includes tool/model execution and application cleanup; controller elapsed time additionally includes scheduling, startup and pod retirement. Cgroup peak memory covers the whole container, including the worker and snapshot. If that optional counter is absent, unreadable or malformed, `memory_peak_bytes` is `null`; this does not change the verified execution/cleanup outcome and provides no sizing evidence. Neither metric alone establishes cold image-pull time, concurrent capacity or a production resource minimum. Use representative tasks and repeated sessions before sizing.
