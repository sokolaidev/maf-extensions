# Azure pricing sandbox image

This Linux/amd64 recipe installs the self-contained Azure MCP `4.0.0-beta.4` executable, its license, certificates and runtime libraries. `image.json` pins the npm archive URL and SHA-512; `install.py` verifies it before copying two fixed regular-file members. Node and npm are absent from the runtime. The base images are pinned by digest. This image has not been published or production-qualified.

Build from the repository root:

```bash
docker build --platform linux/amd64 -t maf-azure-pricing:local images/azure-pricing-sandbox
```

The runtime command is `/usr/local/bin/azmcp pricing get`. Version 4.0.0-beta.4 rejects `--read-only` in CLI mode. The [pricing kind](../../packages/maf-sandbox-azure-pricing/README.md) supplies fixed argv and requires GET-only egress to `prices.azure.com`; the image alone does not enforce networking. Do not mount host credentials or disable TLS validation. Docker and WSLC inject the per-sandbox CA through `SSL_CERT_FILE`, which overrides the image's public CA bundle setting.

Run the live checker with a locally built, current egress proxy:

```bash
uv run python scripts/check_live_azure_pricing.py --image maf-azure-pricing:local --proxy-image maf-egress-proxy:local
```

The checker uses public retail queries with no Azure credentials or model calls. It checks returned pricing fields, trusted labels through the real framework middleware, distinct sandbox instances and disposal. A separate digest-pinned Python image probes an unlisted HTTPS host and a disallowed HTTP method through the same proxy policy. Its JSON evidence records exact local image IDs and cleanup; it does not establish publication, ACAS/WSLC qualification or production acceptance.

Update the pinned binary deliberately: recheck CLI flags, envelope, filter validation, pagination ceiling, projected fields, TLS proxy behavior and the runtime dependency scan. The current CLI ceiling is 5,000 rows, and its mapping omits usage-tier thresholds. No runtime installation or endpoint override is permitted by the kind.
