# maf-sandbox-azure-pricing

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-azure-pricing)](https://pypi.org/project/maf-sandbox-azure-pricing/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-azure-pricing)](https://pypi.org/project/maf-sandbox-azure-pricing/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Importing this package emits `MafSandboxAzurePricingExperimentalWarning`. Its API may change or be removed without notice. This package is not yet released.

Look up Azure public retail prices with one fixed `azmcp pricing get` execution in a fresh sandbox per call. The kind accepts typed filters, builds argv without a shell, validates the JSON envelope and price fields, and disposes the sandbox. It reads and lands no files and receives no Azure credentials.

## Host setup

Call `make_azure_pricing_tools(router, agent_id, context, image=...)` with a configured sandbox router and caller context. It returns the `azure_pricing_get` tool, or an empty list when the router is disabled or absent. The image must provide `/usr/local/bin/azmcp` with the Azure MCP `4.0.0-beta.4` pricing contract. See the [image recipe and live check](https://github.com/sokolaidev/maf-extensions/blob/main/images/azure-pricing-sandbox/README.md).

The spec requires POSIX, at least container isolation, `EXEC`, per-call isolation and disposal. Egress is fixed to GET requests to `prices.azure.com`; the policy derives the `EGRESS_METHODS` requirement. Backends unable to enforce it refuse attachment. Bounded execution is also mandatory: a backend without the optional `BoundedExec` surface is refused before executing the CLI. Docker needs the current packaged egress proxy. Its injected `SSL_CERT_FILE` must be honored; certificate verification is never disabled.

Azure MCP 4.0.0-beta.4 does not accept `--read-only` on `pricing get`. The kind fixes the subcommand and enforces GET-only egress instead. The host cannot supply command names, endpoint overrides or a wider allowlist. Enable network use by attaching this tool only where the host permits that public destination. Host confidentiality policy still governs outgoing query arguments; GET-only access does not prevent data disclosure in a query.

For public-only queries, pass `outbound_max_confidentiality="public"` and run the framework's label-tracking middleware followed by `PolicyEnforcementFunctionMiddleware(block_on_violation=True)`. The policy blocks private context or arguments before sandbox acquisition, including when the host trusts the pricing source's integrity. The cap defaults to `None`, following core's opt-in host classification contract; without enforcing middleware, the declaration alone does not block egress. A direct `tool.func` call bypasses framework middleware and is the host's responsibility.

## Arguments and results

Arguments are `sku`, `service`, `region`, `service_family`, `price_type`, `currency` (default `USD`), `include_savings_plan` (default false), and `filter` (raw OData). At least one filter is required; `currency` and `include_savings_plan` do not count. A `service` query also requires `sku`. `PriceType` accepts `Consumption`, `Reservation` and `DevTestConsumption`. Raw filters are parenthesized before the CLI combines them with the typed filters. Empty, oversized and control-bearing arguments are refused before sandbox acquisition.

The tool's parameter schema describes the price-list fields and their exact, case-sensitive values:

| Parameter | Price-list field | Example |
|---|---|---|
| `sku` | `armSkuName` | `Standard_D4s_v5` |
| `service` | `serviceName` | `Virtual Machines` |
| `region` | `armRegionName` | `westeurope` |
| `service_family` | `serviceFamily` | `Compute` |
| `price_type` | `priceType` | `Consumption` |

Many network and platform meters have an empty `armSkuName`; do not substitute a product name or invent a SKU. The pinned CLI rejects its named `service` argument without `sku`, even when a raw filter is also supplied. The tool refuses that combination before acquiring a sandbox and explains the alternative: omit both `service` and `sku`, and use `filter` on `serviceName`, `productName` or `meterName` instead. For Private Endpoint pricing, use `region="eastus"` with `filter="serviceName eq 'Virtual Network' and productName eq 'Virtual Network Private Link' and meterName eq 'Standard Private Endpoint'"`; those rows have an empty ARM SKU and return as a `Global` fallback.

For raw OData, [Microsoft's API filter reference](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices#api-filters) lists `armRegionName`, `location`, `meterId`, `meterName`, `productId`, `skuId`, `productName`, `skuName`, `serviceName`, `serviceId`, `serviceFamily`, `priceType` and `armSkuName`; `reservationTerm` also works. `contains(meterName, 'Spot')` supports substring queries. There is no operating-system field such as `osType`: Windows VM meters have `Windows` at the end of `productName`. `skuName` is a price-list value, not a portal display name. Unknown fields fail the whole lookup. Recognizable upstream HTTP 400 failures return fixed filter-correction guidance without echoing CLI text or arguments; raw predicates are not parsed or field-validated on the host.

An ARM `region` expands the single CLI query to that region, `Global`, `Zone 1` through `Zone 9`, `US Gov Zone 1` and `US Gov Zone 2`, ANDed with the other filters. After validating every received row, the host keeps exact regional rows and selects billing-zone rows per service/product family. It then keeps `Global` rows only for meters without a regional or selected-zone row, keyed by `(serviceName, productName, meterName)`. Incomplete meter identities cannot suppress a Global row. All terms and price bands for a retained meter survive selection. Global VNet peering already has ARM-region rows and matches directly.

Expanded results label each row `matched_by="region"`, `"zone"` or `"global"`, retaining the CLI's original `region`. Zone rows also carry `zone`, `zone_family` and `zone_basis`; network rows name `zone_reference_region`. Report fields `requested_region`, `selected_rows` and `selection_notes` explain the selection. These annotations are the kind's own and sit in the price report; an unknown `requested_region` is echoed among the descriptions instead.

An explicit `armRegionName` field in `filter` disables expansion and selection; occurrences inside quoted OData strings do not count. The raw predicate and any named `region` retain their existing AND semantics. Supplying a billing-zone name or `Global` as `region`, or omitting `region`, also preserves exact CLI behavior and the original report shape. Empty-region meters, including some reservations, require an explicit predicate such as `armRegionName eq ''`; the `region` argument itself cannot be empty. Unknown ARM regions retain exact regional and Global rows but omit zones with a note. Unknown zone families and ExpressRoute zone rows are also omitted with notes, so an empty result still does not establish that no price exists.

### Billing-zone selection

The source tables were checked on 2026-11-03 against Microsoft's [virtual-network pricing FAQ](https://azure.microsoft.com/en-us/pricing/details/virtual-network/), [Front Door pricing](https://azure.microsoft.com/en-us/pricing/details/frontdoor/), [CDN pricing](https://azure.microsoft.com/en-us/pricing/details/cdn/), [CDN billing zones](https://learn.microsoft.com/en-us/azure/cdn/cdn-billing), [DNS pricing](https://azure.microsoft.com/en-us/pricing/details/dns/) and [region locations](https://learn.microsoft.com/en-us/azure/reliability/regions-list). The host uses a bundled mapping, without extra network calls; newly named regions need a mapping update or an explicit location filter. These billing zones are unrelated to Azure availability zones.

| `zone_family` | Service/product match | Selection |
|---|---|---|
| `network` | Virtual Network Manager and TAP (`Virtual Network` product), VPN Gateway Bandwidth, Azure Cosmos DB Data Transfer, Azure Multicloud Interconnect | Virtual-network FAQ Zone 1–3; US Gov Arizona/Texas/Virginia use `US Gov Zone 1`. Australia Central is Zone 1, while Australia East is Zone 2. |
| `front_door` | `Azure Front Door Service` service, `Azure Front Door` product | Geography: North America 1, Asia Pacific 2, South America 3, Australia 4, India 5, Europe 6, Middle East/Africa 7, Korea 8, US Gov 9. |
| `cdn_classic` | `Content Delivery Network` service, or `Azure Front Door Service` service and product | Geography: North America/Europe/Middle East/Africa 1, Asia Pacific including Korea 2, South America 3, Australia and New Zealand 4, India 5. No inferred government zone. |
| `dns` | `Azure DNS` service | One uniform commercial price set, `Zone 1`; the three mapped government regions use `US Gov Zone 1`. |
| No selection | `ExpressRoute` service | The circuit's peering location determines the zone, not the ARM region. Omit zone rows with a note; query the location explicitly. |

Front Door and CDN selections assume the requested geography represents the serving edge. Actual billing follows the [edge location serving the request](https://learn.microsoft.com/en-us/azure/frontdoor/billing), which the origin region does not determine. The report includes this limitation whenever an edge-zone row is selected.

For known regions missing from the network FAQ, the bundled closest-region estimates below select the reference region's network zone and label `zone_basis="nearest_region"`. They are geographic estimates, not Microsoft-published billing assignments. Direct FAQ assignments use `published_region`; edge families use `geography`; commercial DNS uses `uniform_commercial`. Sovereign clouds are not mapped into commercial zones by proximity.

| Region(s) absent from the network FAQ | Closest listed reference |
|---|---|
| `austriaeast`, `denmarkeast`, `polandcentral`, `swedensouth` | `germanynorth` |
| `belgiumcentral` | `westeurope` |
| `brazilsoutheast`, `chilecentral` | `brazilsouth` |
| `indiasouthcentral` | `centralindia` |
| `indonesiacentral`, `malaysiawest`, `southeastasia3` | `southeastasia` |
| `israelcentral`, `qatarcentral` | `uaecentral` |
| `italynorth` | `switzerlandnorth` |
| `mexicocentral` | `southcentralus` |
| `newzealandnorth` | `australiaeast` |
| `spaincentral` | `francesouth` |
| `swedencentral` | `norwayeast` |
| `taiwannorth`, `taiwannorthwest` | `eastasia` |
| `westus3` | `westus` |

### Bounds and price interpretation

The host selects `exec_timeout_seconds` (default 60), `max_output_bytes` (default 8 MiB for combined stdout/stderr) and `max_rows` (default 100). Responses must be successful, clean UTF-8 JSON with valid pricing values; duplicate keys, non-finite prices, malformed rows, transport errors and stderr fail closed. An empty price array is a completed lookup. Errors do not echo CLI text or query arguments.

The price report includes `prices`, `returned_rows`, `received_rows`, `truncated` and `upstream_limit_reached`; a second item, `descriptions`, carries the same rows' text, each entry joined to its price by `row`. The CLI retrieves at most 5,000 rows before host selection and the presentation cap. `received_rows` counts all validated CLI rows; `selected_rows` counts regional/zone/Global matches before `max_rows`. Intentionally excluded locations do not imply truncation, but reaching the CLI ceiling always does, even if selection leaves few or no rows. Expanded queries reach that ceiling more easily; a Global fallback is based only on received rows and cannot prove a preferred row is absent from a truncated result. Narrow the query; the tool has no continuation token and does not compute totals over incomplete results.

Keep reservation terms with their prices: `retailPrice` covers the whole reservation term even when `unitOfMeasure` says `1 Hour`. Savings plans retain their nested term and prices. These are public retail prices, not negotiated account prices. Azure MCP omits `tierMinimumUnits`, so the result cannot establish usage-tier thresholds.

## Integrity

Each result has two parts, joined by row number. The price report is `SandboxResult.trusted_output`: prices as finite non-negative numbers, `priceType`, savings plans, `isPrimaryMeterRegion`, the kind's selection labels, the counts and flags, and only the text the kind checks itself. That text is `unitOfMeasure` in the set of units the kind holds, `currencyCode` of three capitals, `region` among the price-list locations it knows, `reservationTerm` of `1 Year` or `3 Years`, and an ISO 8601 `effectiveStartDate`. A value that fails its check moves to the descriptions; a row whose unit is not in the set keeps its prices in the report without a unit.

The descriptions are `SandboxResult.output` with an untrusted workload declaration: product, SKU, meter and service names, `serviceFamily`, `location`, `meterId`, and any value that failed its check. The host may pass `egress_integrity=SourceIntegrity.TRUSTED` to vouch for the fixed Microsoft pricing source. Core promotes the descriptions only when the framework also establishes trusted conversation and argument labels. Without either piece of evidence they stay untrusted. The host's result confidentiality is preserved.

That host trust decision covers the descriptions' text; it is not proof that external text is free of instructions. Leave egress integrity unset when the host cannot vouch for it. Fixed standing guidance tells consumers to treat all report fields as data, never instructions or authority to call tools or change policy. This guidance does not replace the host's trust decision or enforce confidentiality.

Besides the price report, only fixed completion, failure and truncation messages and standing pricing guidance use the trusted part of the result. Parsing JSON alone does not establish trust. See the [information-flow contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md).
