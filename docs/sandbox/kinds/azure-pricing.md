# Azure pricing

`maf-sandbox-azure-pricing` exposes `azure_pricing_get` for public Azure retail-price queries. It runs the pinned Azure MCP CLI once per call, without credentials or file-store access. The host chooses the image and budgets; the model supplies typed filters.

## Execution and sources

Each call gets a fresh POSIX sandbox with at least container isolation and disposal cleanup. The spec requires `EXEC` and derives `EGRESS_METHODS` from a fixed rule allowing only GET requests to `prices.azure.com`. The kind refuses execution without a host-enforced stdout/stderr byte bound. It neither starts an MCP server nor exposes arbitrary commands.

Azure MCP `4.0.0-beta.4` rejects `--read-only` on its CLI pricing command. The kind fixes `pricing get` and enforces GET-only egress through the backend. There is no endpoint or allowlist argument. Query arguments still leave the guest, so the host governs their confidentiality.

The host can pass `outbound_max_confidentiality="public"` and wire label tracking followed by enforcing policy middleware to refuse private queries before acquisition. The cap is opt-in, in the host's classification vocabulary; a declaration without middleware does not enforce it. Trusting the network source does not bypass the cap.

## Results

The host validates the success envelope and every row, projects known pricing fields, selects applicable locations and applies its row cap. It rejects malformed JSON, ambiguous keys, invalid numbers, missing units and reservation terms, failed execution and stderr. Besides fixed completion/failure/truncation messages and standing pricing guidance, the trusted result fields carry the checked part of each row, described below.

An ARM-region query retrieves that region, Global and billing-zone candidates in one CLI execution. The host selects zones by service/product family, then uses Global as a fallback per service/product/meter. For West Europe, network rows use Zone 1, Front Door Standard/Premium uses Zone 6, and CDN/classic Front Door uses Zone 1. Rows carry `matched_by` and zone provenance in the price report. The [package guide](../../../packages/maf-sandbox-azure-pricing/README.md#billing-zone-selection) records the source tables, nearest-region estimates and exclusions. Edge zones assume the requested geography represents the serving edge; ExpressRoute requires an explicit peering-location filter. Unknown regions and families omit zone rows with a note. An explicit `armRegionName` predicate disables expansion; exact Global/zone queries and queries without `region` retain their original behavior.

The report, every retained field of each numbered row, uses `SandboxResult.output` with `source_integrity=UNTRUSTED`. The host may establish the network source with `egress_integrity=SourceIntegrity.TRUSTED`. With no file reads, core promotes these rows only when the framework also establishes trusted conversation and argument labels. Without those conditions they remain untrusted. See [information flow](../information-flow.md#how-core-labels-a-call).

`SandboxResult.trusted_output` repeats a subset of that report under the same row numbers, `checked_prices`, with the same counts, flags and selection notes: each row's `retailPrice` and `unitPrice` (finite non-negative numbers), `priceType`, savings plans, `isPrimaryMeterRegion`, the kind's selection labels, and five text fields only where they pass a check the kind holds, `unitOfMeasure` in the set of units the kind measured, `currencyCode` of three capitals, `region` among the price-list locations the kind knows, `reservationTerm` of `1 Year` or `3 Years`, and an ISO 8601 `effectiveStartDate`. Every other field, and a value that fails its check, appears in the report only, so a unit outside the set leaves its checked row without a unit. A `requested_region` the kind knows appears in both; an unknown one is echoed in neither, so neither part repeats the caller's text.

Trusting that source covers its text fields as well as numeric prices. Fixed standing guidance treats every field as data, never instructions; it is contextual guidance, not a sanitizer or a substitute for the host's trust decision.

The CLI retrieves at most 5,000 rows. `truncated` is true when the presentation cap omits selected rows or the upstream ceiling is reached, even if host location selection leaves few rows; no continuation token is available. Global fallbacks cannot establish the absence of preferred rows in a truncated result. An empty list is a completed lookup. Reservation prices cover whole terms despite hourly-looking units, savings plans retain nested terms, and Azure MCP omits usage-tier thresholds. Public retail prices do not establish negotiated account pricing.

## Host and verification

The [package guide](../../../packages/maf-sandbox-azure-pricing/README.md) describes arguments, budgets and attachment. The [image guide](../../../images/azure-pricing-sandbox/README.md) describes the pinned archive and live checker. Unit tests cover parsing, refusals, limits, labels and cleanup; the Docker checker exercises the real CLI through the proxy and records image identity, distinct instances and disposal. Other backends require their own live qualification.

## Status

| Decision | State | Tracking |
|---|---|---|
| Disposable public pricing kind and GET-only source | Implemented; publication and other-backend qualification remain separate | [#1678](https://github.com/sokolaidev/maf-extensions/issues/1678) (closed) by [#2331](https://github.com/sokolaidev/maf-extensions/pull/2331) (merged) |
| Regional Global fallback and per-family billing zones | Implemented; nearest-region and serving-edge assumptions remain explicit | [#2418](https://github.com/sokolaidev/maf-extensions/issues/2418) (closed) by [#2420](https://github.com/sokolaidev/maf-extensions/pull/2420) (merged) |
