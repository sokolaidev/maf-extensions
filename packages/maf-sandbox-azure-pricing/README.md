# maf-sandbox-azure-pricing

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-azure-pricing)](https://pypi.org/project/maf-sandbox-azure-pricing/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-azure-pricing)](https://pypi.org/project/maf-sandbox-azure-pricing/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Importing this package emits `MafSandboxAzurePricingExperimentalWarning`. Its API may change or be removed without notice. This package is not yet released.

Look up Azure public retail prices with one fixed `azmcp pricing get` execution in a fresh sandbox per call. The kind accepts typed filters, builds argv without a shell, validates the JSON envelope and price fields, and disposes the sandbox. It reads and lands no files and receives no Azure credentials.

## Host setup

Call `make_azure_pricing_tools(router, agent_id, context, image=...)` with a configured sandbox router and caller context. It returns the `azure_pricing_get` tool, or an empty list when the router is disabled or absent. The image must provide `/usr/local/bin/azmcp` with the Azure MCP `4.0.0-beta.4` pricing contract. See the [image recipe and live check](https://github.com/sokolaidev/maf-extensions/blob/main/images/azure-pricing-sandbox/README.md).

The spec requires POSIX, at least container isolation, `EXEC`, per-call isolation and disposal. Egress is fixed to GET requests to `prices.azure.com`; the policy derives the `EGRESS_METHODS` requirement. Backends unable to enforce it refuse attachment. Bounded execution is also mandatory: a backend without the optional `BoundedExec` surface is refused before executing the CLI. Docker needs the current packaged egress proxy. Its injected `SSL_CERT_FILE` must be honored; certificate verification is never disabled.

Azure MCP 4.0.0-beta.4 does not accept `--read-only` on `pricing get`. The kind fixes the subcommand and enforces GET-only egress instead. The host cannot supply command names, endpoint overrides or a wider allowlist. Enable network use by attaching this tool only where the host permits that public destination. Host confidentiality policy still governs outgoing query arguments; GET-only access does not prevent data disclosure in a query.

## Arguments and results

Arguments are `sku`, `service`, `region`, `service_family`, `price_type`, `currency` (default `USD`), `include_savings_plan` (default false), and `filter` (raw OData). At least one filter is required; `currency` and `include_savings_plan` do not count. A `service` query also requires `sku`. `PriceType` accepts `Consumption`, `Reservation` and `DevTestConsumption`. Raw filters are parenthesized before the CLI combines them with the typed filters. Empty, oversized and control-bearing arguments are refused before sandbox acquisition.

The host selects `exec_timeout_seconds` (default 60), `max_output_bytes` (default 8 MiB for combined stdout/stderr) and `max_rows` (default 100). Responses must be successful, clean UTF-8 JSON with valid pricing values; duplicate keys, non-finite prices, malformed rows, transport errors and stderr fail closed. An empty price array is a completed lookup. Errors do not echo CLI text or query arguments.

Projected JSON includes `prices`, `returned_rows`, `received_rows`, `truncated` and `upstream_limit_reached`. The CLI retrieves at most 5,000 rows before the host applies its row cap. Reaching that ceiling marks the result potentially truncated, even when all received rows fit. Narrow the query; the tool has no continuation token and does not compute totals over incomplete results.

Keep reservation terms with their prices: `retailPrice` covers the whole reservation term even when `unitOfMeasure` says `1 Hour`. Savings plans retain their nested term and prices. These are public retail prices, not negotiated account prices. Azure MCP omits `tierMinimumUnits`, so the result cannot establish usage-tier thresholds.

## Integrity

Price data is returned through `SandboxResult.output` with an untrusted workload declaration. The host may pass `egress_integrity=SourceIntegrity.TRUSTED` to vouch for the fixed Microsoft pricing source. Core promotes the data only when the framework also establishes trusted conversation and argument labels. Without either piece of evidence it stays untrusted. The host's result confidentiality is preserved.

Only fixed completion, failure and truncation messages and standing pricing guidance use the trusted part of the result. Parsing JSON alone does not establish trust. See the [information-flow contract](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/information-flow.md).
