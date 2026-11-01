# Word document to Azure cost estimate

An incoming `.docx` lists Azure resources. The host scans its immutable bytes with `maf-sandbox-antivirus`, admits only a byte-bound clean verdict, extracts text with `maf-sandbox-document`, asks a tool-free MAF agent for structured inventory, queries `maf-sandbox-azure-pricing`, and writes a Markdown cost table plus a JSON audit report. Each sandbox call uses Docker and requires disposal.

The PEP 723 block installs published packages, including antivirus and Azure pricing 0.1.0 or later and document 0.2.1 or later. The sandbox APIs remain experimental. The `sample-23` job in Verify (live) exercises the full model path in both published and branch modes; the evidence checker requires three priced fixture entries, one unresolved entry, verified report bytes and complete cleanup.

## Run

From the repository root, supply locally built or verified image references using `ANTIVIRUS_SANDBOX_IMAGE`, `DOCUMENT_SANDBOX_IMAGE`, `AZURE_PRICING_SANDBOX_IMAGE` and `MAF_EGRESS_PROXY_IMAGE`. The build and database requirements are in the [antivirus](../../images/antivirus-sandbox/README.md), [document](../../images/document-sandbox/README.md), [pricing](../../images/azure-pricing-sandbox/README.md) and [Docker proxy](../../packages/maf-sandbox-docker/README.md) guides. Prefer immutable image digests; the JSON report retains the supplied references. Allow at least 4 GiB for the antivirus guest. An outdated antivirus database fails closed: supply an immutable signed mirror snapshot with `--database-snapshot DIRECTORY` when needed.

Set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL` to a deployment supporting structured outputs, then authenticate with `az login` or another `DefaultAzureCredential` source. The extracted document text is sent to that configured model; use documents that host is authorized to process. This mode incurs model inference charges, but deploys no Azure resources and gives no credentials to the sandboxes.

```powershell
uv run --no-project samples/23_word_resource_estimate/agent.py samples/23_word_resource_estimate/resources.docx --output cost-estimate
```

Replace the document argument with an incoming Word file. The default region is `westeurope`, currency `USD`; change them using `--region` and `--currency`. A document entry naming a different region is unresolved, not silently moved. A missing region uses the selected region shown above the table. The output directory must not exist; the sample creates `estimate.md` and `estimate.json` only after sandbox cleanup succeeds, verifies their bytes and removes its own partial report on a write failure. It does not modify or delete the incoming file. Local output files are retained for the user.

The checked-in [resources.docx](resources.docx) contains two Linux B2s VMs, one part-time Linux D2s v5 VM, two P10 LRS disks and an underspecified Blob Storage account. Regenerate it at a new path with `uv run python samples/23_word_resource_estimate/make_document.py new-resources.docx`.

For a model-free integration check, add `--fixture-inventory`. This mode accepts only the exact bytes of the generated example and substitutes its known inventory **after the real scan and document conversion**. Antivirus, document parsing, pricing and cleanup still run in real sandboxes. It needs only the four image variables, and the JSON report records `inventory_source: fixture`; it does not qualify model extraction. It should retain all four entries, estimate the three supported entries when each has one exact retail match, and leave the Blob Storage entry unresolved. Prices are fetched live and are never baked into the fixture.

## Estimate contract

The starter catalog in [estimate.py](estimate.py) supports Linux B2s, Linux D2s v5, Windows B2s and Premium SSD P10 LRS managed disks. Extend that host-owned catalog deliberately when adding resources; a Word document or model cannot add raw OData, a price endpoint, a SKU string or arbitrary filter text. Only catalog values and the host-selected region/currency reach the public pricing service. The model has no tools and cannot skip scanning or call pricing itself. Direct host tool calls do not use framework policy middleware; this sample enforces the narrow query boundary in host code.

The extractor keeps unknown products, missing quantities and unsupported requirements as rows. Each proposed entry must carry a verbatim quote from the converted document. That check establishes the quote's presence, not semantic correctness or complete recall: review the inventory and its evidence before using the estimate. Antivirus scanning does not make document instructions trustworthy, and the inventory remains model-derived.

An estimate requires one exact service/product/SKU/meter/unit match in the selected region and currency, with `Consumption` pricing, a primary meter, an effective date no later than retrieval, and no truncated result. Multiple matches, no matches, failed lookups, region conflicts and missing quantities remain unresolved. The sample never chooses the cheapest row. Reservations, savings plans, Spot and usage-tier pricing are unsupported; Azure MCP's projected report omits tier thresholds, so this sample limits calculation to the catalog's fixed meters. See Microsoft's [retail-price API contract](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

Hourly VM cost is `quantity × hours per resource per month × retailPrice`, using an explicit 730-hour assumption only when hours are absent. Zero hours stays zero. Monthly disk cost is `quantity × retailPrice`, assuming full-month provisioning. Decimal arithmetic rounds each line to two places and sums those displayed lines. VM prices exclude disks; disk prices exclude transactions and bursting. Taxes, negotiated discounts, network charges, operations and unlisted resources are excluded. A resource with multiple charge components is not fully costed by one meter.

The table shows quantity, unit price, usage, monthly cost and assumptions. `estimate.json` also retains document evidence, extracted requirements, the matched meter ID, effective date, retrieval time and selected image references. An unresolved row has `monthly_cost: null`, and any unresolved row makes `inventory_total: null`; `estimated_subtotal` is explicitly partial. Even when every extracted row is priced, the result is a scoped inventory estimate, not a billing quote.

## Validation

```powershell
uv run pytest tests/test_word_resource_estimate.py -q
```

Offline tests cover scan-before-read, byte mismatch, refusal and failure paths, immutable ingress, the real guarded DOCX converter, missing inventory evidence, query caching, wrong or ambiguous pricing meters, truncation, unit-aware arithmetic, partial totals and report ownership. These checks do not establish live antivirus detection, live retail availability or model extraction quality; use the configured live modes for those separate questions.
