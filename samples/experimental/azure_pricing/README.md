# Azure pricing on Bubblewrap and Docker Sandboxes

This workspace sample runs the existing `azure_pricing_get` tool against public retail prices, with a fresh sandbox for each call. It validates actual consumption and reservation reports for `Standard_D4s_v5` in `westeurope`, USD, before optionally letting an Azure OpenAI agent use the same tool. Model credentials stay on the host. The pricing guest needs no Azure credentials.

The backend additions required by this sample have not been published, so run it from this checkout with `uv sync --locked`. Once both backend releases are available, move this example to the next numbered slot with published PEP 723 floors, branch/published `verify-live.yml` jobs and the three release-dispatch filters described in [CONTRIBUTING.md](../../../CONTRIBUTING.md#adding-a-sample). Do not claim published-package qualification from workspace imports.

## Prepare the runtime

The [pricing recipe](../../../images/azure-pricing-sandbox/) pins Azure MCP CLI 4.0.0-beta.4 and verifies its archive checksum. The [native template](../../../images/azure-pricing-sandbox/native.Dockerfile) adds Python for enforcement probes, Bash, coreutils and util-linux for Docker Sandboxes, and the mount points Bubblewrap requires. Dependencies and certificate trust are provisioned before calls.

On a Linux provisioning machine with Docker, run as root into a new dedicated directory:

```bash
sudo bash samples/experimental/azure_pricing/provision.sh /opt/maf-pricing-runtime
```

This exports an immutable runtime directory, a Docker Sandboxes template archive, image identities and a SHA-256 for the exported runtime. It builds the repository's pinned and patched iron-proxy, exports its native binary, license and build metadata, and records its SHA-256. Docker is used for provisioning; Bubblewrap execution uses the exported directory and native binary without a container engine. Keep the runtime, proxy binary and their ancestors under trusted administrator control and read-only while calls run. Provisioning output includes the exact installed package/image identities; an APK repository update can change the runtime digest while preserving the pinned Azure MCP CLI.

## Bubblewrap

Use a Linux host with Bubblewrap 0.11.1 or compatible support for disabled nested user namespaces, bounded tmpfs mounts, and delegated cgroup v2 `cpu`, `memory`, `pids`, swap limits and `cgroup.kill`. The [backend prerequisites](../../../packages/maf-sandbox-bubblewrap/README.md#prerequisites) also apply. The runtime must include `/usr/bin/python3`, `/bin/sh` and `/usr/local/bin/azmcp`. A runtime id is an exact label matching `SandboxSpec.image`; it is not an OCI image passed to Bubblewrap.

Run as an unprivileged user in a dedicated systemd service. Substitute an operator-selected service user and the absolute paths to this checkout and its Linux virtual environment. The wrapper moves its host process into a leaf before enabling controllers; its state directory is private and dedicated to this sample.

```bash
source_sha=$(git rev-parse HEAD)
sudo systemd-run --wait --pipe --collect --unit=maf-pricing-check \
  -p User=my-service-user -p Delegate='cpu memory pids' \
  /bin/bash "$PWD/samples/experimental/azure_pricing/run-bubblewrap.sh" \
  /opt/maf-pricing-runtime "$PWD/.venv/bin/python" "$source_sha" --no-model --qualify
```

The router explicitly selects `Isolation.CONTAINER`. The guest retains its private network namespace. Its only egress channel is a mounted Unix socket reaching a per-sandbox native proxy; only the proxy's public certificate enters the guest. Signing keys remain on the host. The proxy checks upstream TLS and enforces the host/method/path rules after TLS termination. Private and link-local upstream destinations are refused. Both proxy and guest belong to the sandbox's cgroup and die with their owner. The default backend configuration remains closed-only; allowlisting requires both `egress_proxy` and its verified `egress_proxy_sha256`.

## Docker Sandboxes

Use a supported host with working virtualization, a logged-in `sbx`, and the [backend prerequisites](../../../packages/maf-sandbox-docker-sbx/README.md#requirements). Host policy changes are explicit operator setup. On a dedicated host, the operator prepares it with:

```bash
sbx login
sbx policy init deny-all
sbx settings set ssh.agentForwardingEnabled false
sbx daemon restart
sbx template load /opt/maf-pricing-runtime/template.tar
```

No MCP servers may be registered. Stored secrets, organization governance and global network grants must not widen the pricing allowlist; the backend checks them and refuses incompatible posture. Do not bypass those checks. Docker Sandboxes supplies proxy certificate trust to the template; certificate verification stays enabled. Template loading and host-wide policy setup are not sample side effects.

```bash
uv run python samples/experimental/azure_pricing/agent.py \
  --backend docker-sbx --image maf-pricing-native:qualification \
  --workspace /var/lib/my-service/pricing-workspaces \
  --source "$(git rev-parse HEAD)" --no-model --qualify
```

Choose a new, private workspace root used only by this run. The router retains its default `MICROVM` floor. Every tool call gets a distinct microVM and workspace, followed by disposal. The local template tag must continue to name the archive just loaded; retain the archive hash and image id alongside the log. The [Docker Sandboxes live workflow](../../../.github/workflows/sbx-live.yml) builds and loads the template on a disposable Ubuntu runner and retains those identities.

## Agent mode and evidence

For agent mode, configure `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL`, authenticate the host with `DefaultAzureCredential`, and omit `--no-model`. The Linux service needs its own host identity and environment; credentials are never mounted into the runtime. Agent mode incurs model inference costs. `--no-model` still performs real public price lookups and uses no model credentials.

Keep logs and validate them separately:

```bash
uv run python scripts/check_live_backend_pricing.py pricing.log --backend bubblewrap --no-model --require-probes
```

Use `set -euo pipefail` when piping the sample through `tee`. For model evidence omit `--no-model` from both commands. Host records include imported-source hashes, installed versions, runtime identity, per-call timing and sandbox identities. The checker requires actual price fields, distinct calls, independent host inventory checks and completed cleanup; model prose cannot satisfy it. Empty results mean no matching rows; truncated results are not exhaustive. Both are represented by the tool, but fail this deliberately narrow qualification query. Reservation prices are whole-term amounts even when the returned unit says `1 Hour`; never multiply them by monthly hours. Retail prices are not negotiated account quotes.

`--qualify` adds disallowed host/method and direct-network probes, output overflow, timeout and cancellation, checking disposal after every case. The root test `test_live_backend_pricing` skips unless `MAF_PRICING_E2E=1`; provide the CLI argument list as JSON in `MAF_PRICING_LIVE_ARGS` and an explicit log destination in `MAF_PRICING_EVIDENCE_LOG`. Offline tests need neither Azure credentials nor real model calls.

Local Bubblewrap tool-only pricing and enforcement probes have passed on Ubuntu 24.04 under WSL 2 with Bubblewrap 0.11.1. Docker Sandboxes live qualification and model execution remain pending. Neither local execution nor workspace tests establish published-package, hosted, macOS or production qualification. Track delivery in [#2473](https://github.com/sokolaidev/maf-extensions/issues/2473), [#2474](https://github.com/sokolaidev/maf-extensions/issues/2474), and prerequisites [#2540](https://github.com/sokolaidev/maf-extensions/issues/2540) and [#2541](https://github.com/sokolaidev/maf-extensions/issues/2541).
