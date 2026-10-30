# Azure pricing

`maf-sandbox-azure-pricing` exposes `azure_pricing_get` for public Azure retail-price queries. It runs the pinned Azure MCP CLI once per call, without credentials or file-store access. The host chooses the image and budgets; the model supplies typed filters.

## Execution and sources

Each call gets a fresh POSIX sandbox with at least container isolation and disposal cleanup. The spec requires `EXEC` and derives `EGRESS_METHODS` from a fixed rule allowing only GET requests to `prices.azure.com`. The kind refuses execution without a host-enforced stdout/stderr byte bound. It neither starts an MCP server nor exposes arbitrary commands.

Azure MCP `4.0.0-beta.4` rejects `--read-only` on its CLI pricing command. The kind fixes `pricing get` and enforces GET-only egress through the backend. There is no endpoint or allowlist argument. Query arguments still leave the guest, so the host governs their confidentiality.

## Results

The host validates the success envelope and every row, projects known pricing fields and applies its row cap. It rejects malformed JSON, ambiguous keys, invalid numbers, missing units and reservation terms, failed execution and stderr. Only fixed completion/failure/truncation messages and standing pricing guidance use the trusted result fields.

Price rows use `SandboxResult.output` with `source_integrity=UNTRUSTED`. The host may establish the network source with `egress_integrity=SourceIntegrity.TRUSTED`. With no file reads, core promotes these rows only when the framework also establishes trusted conversation and argument labels. Without those conditions they remain untrusted. See [information flow](../information-flow.md#how-core-labels-a-call).

The CLI retrieves at most 5,000 rows. `truncated` is true when the host omits rows or the upstream ceiling is reached; no continuation token is available. An empty list is a completed lookup. Reservation prices cover whole terms despite hourly-looking units, savings plans retain nested terms, and Azure MCP omits usage-tier thresholds. Public retail prices do not establish negotiated account pricing.

## Host and verification

The [package guide](../../../packages/maf-sandbox-azure-pricing/README.md) describes arguments, budgets and attachment. The [image guide](../../../images/azure-pricing-sandbox/README.md) describes the pinned archive and live checker. Unit tests cover parsing, refusals, limits, labels and cleanup; the Docker checker exercises the real CLI through the proxy and records image identity, distinct instances and disposal. Other backends require their own live qualification.

## Status

| Decision | State | Tracking |
|---|---|---|
| Disposable public pricing kind and GET-only source | Implemented; publication and other-backend qualification remain separate | [#1678](https://github.com/sokolaidev/maf-extensions/issues/1678) (closed) by [#2331](https://github.com/sokolaidev/maf-extensions/pull/2331) (merged) |
