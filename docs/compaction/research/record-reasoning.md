# Completed recall records with reasoning

[#2631](https://github.com/sokolaidev/maf-extensions/issues/2631) proposes removing reasoning together with the recall call and result it belongs to. The implementation requires a later genuine user request, excludes synthetic recall instructions, tool results, generated summaries and excluded users as boundaries, and retains the current carried result. A mixed group or a rich result remains protected.

Removing an old record can also change the prefix of reasoning that remains later in the conversation. Anthropic's [preserved-thinking contract](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking#compact-on-the-client) makes this a separate constraint from keeping the current tool loop whole. Record replacement therefore stops before any later retained reasoning. Lossless reshapes validate a batch of eligible completed records and replace them together, with no history rescan per record; an unrelated or active reasoning group protects every record before it. This is a conservative record-selection rule, not provider-wide qualification of the other compaction strategies.

## Qualification matrix

Unit tests cover whole-group exclusion, exact value retention, completed and active turns, synthetic boundaries, later-reasoning protection, mixed content, carried results and persisted decision replay. Replay restores a previously accepted edit on exact matching sources even when later reasoning has arrived: that reasoning was produced on the compacted prefix, so restoring the original prefix would change its input. These tests do not establish service acceptance. The live results below qualify two configured routes; Gemini remains unqualified, so the change remains in draft and #2631 stays open.

| Route | Required probe | Acceptance evidence |
|---|---|---|
| OpenAI Responses, stateless | Request real recall calls with reasoning and encrypted reasoning replay where supported; complete that tool loop, add a new user request, replace the old recall group, and send the projected messages with `store=false`. Repeat after session serialization and restore. | Both baseline and compacted requests succeed; no orphaned reasoning, calls or results; record values remain available. Active-loop input remains unchanged. |
| Anthropic thinking | Obtain actual signed thinking and recall blocks, finish the assistant tool loop, then replace completed records. Include a later retained thinking block as a negative selection case. On models supporting binding controls, explicitly request prefix mismatch errors. | Baseline and compacted requests succeed without dropping invalid thinking to hide a prefix mismatch. Current-loop and later-prefix guard cases remain byte-for-byte unchanged on the wire. |
| Gemini | Specify the API and framework adapter, capture a real signed function call, complete the turn, replace the old record, and replay after restore. Cover the adapter's actual thought/signature representation. | Call/signature leave together, with no surviving signature attached to the wrong part; continuation and restored continuation succeed. |

Use synthetic record values only. Retain model and SDK versions, request shapes, finish reasons and usage; keep credentials and opaque provider tokens out of the public evidence. A missing reasoning/signature block is an unqualified probe, not a pass. The AG-UI history adapter drops reasoning across reconstructed runs, as the issue's comment explains, so these probes must exercise an adapter that actually replays it.

## Live results, 2026-11-11

The [qualification probe](../../../scripts/experiments/qualify_record_reasoning.py) authenticated with Azure CLI credentials to existing Azure AI Foundry deployments. The [sanitized evidence](record-reasoning-live-2026-11-11.json) retains both reports and request shapes. Product code was commit `9fe573c51b1edc8b0fabb9e9a377930f61306179`; the then-uncommitted probe's SHA-256 was `5a9fa69f308680994a051e047126985b68c684eb9144f0e5aaa46ed601436843`. The committed probe adds only explicit parentheses required by repository lint; its Python AST was verified identical to the executed source. Python was 3.13.12, `agent-framework-core` 1.24.0 and `azure-identity` 1.26.0. No provider keys were used.

| Route | Model and adapter | Settings | Result |
|---|---|---|---|
| Foundry OpenAI Responses `/openai/v1/responses` | `gpt-5.4-mini`, version `2026-03-17`; `agent-framework-openai` 1.18.0, `openai` 3.29.0 | `store=false`, encrypted reasoning replay, medium effort; tool-loop completion uses effort `none` | 15/15 requests returned HTTP 200; all probe checks passed |
| Foundry Anthropic `/anthropic/v1/messages` | `claude-sonnet-5-5`, version `2`; `agent-framework-anthropic` 1.0.0b261022, `anthropic` 1.13.0 | Adaptive thinking, high effort; tool-loop completion uses low effort; `thinking-binding-controls-2026-08-01`, prefix mismatch behavior `error` | 15/15 positive requests returned HTTP 200 with explicitly reported empty `input_transformations`; deliberate system-prefix mutation returned the expected HTTP 400 signature error |
| Gemini | No route configured | Not run | Pending |

Both routes produced two actual recall calls with protected reasoning. Each loop completed without later reasoning before a genuine user request made the records eligible. The probe exercised lossless reshape, a merge of exactly two written records after the production reshape, and rewrite of one original completed record. Synthetic identifiers remained in the resulting record bodies and in the live continuation answers. Every operation reduced the serialized request history. Active-loop and later-retained-reasoning guards selected no records and preserved the serialized history exactly; both guarded histories were accepted by the service.

Each operation also passed after JSON serialization and restoration of messages and strategy state. Replaying decisions on the original history reproduced the compacted provider input exactly, including when later reasoning generated on that compacted input was appended before restoration. Actual outbound request bodies were checked against the framework adapter's projection. Claude's deliberate prefix mutation establishes that its successful requests were tested with binding enforcement active.

The record merge/rewrite summarizer was a deterministic fixture retaining distinct values. These probes qualify provider acceptance and record projection/replay for the listed routes and settings, not live summarizer quality, tokenizer accuracy, streaming, all models or the AG-UI within-run case. The integrated compaction suite passed 1,499 tests; the full repository gate was not rerun for this qualification.

## Reproduce

Use an existing Azure CLI login authorized for inference. Set `QUALIFICATION_MODEL_NAME` and `QUALIFICATION_MODEL_VERSION` to the public model metadata. For OpenAI, set `AZURE_OPENAI_ENDPOINT` to the resource endpoint and `AZURE_OPENAI_MODEL` to the deployment name; for Claude, use `ANTHROPIC_FOUNDRY_ENDPOINT` and `ANTHROPIC_FOUNDRY_MODEL`. Endpoints omit the API suffix, which the probe adds. The Azure scopes are `https://cognitiveservices.azure.com/.default` and `https://ai.azure.com/.default`, respectively.

Run from the repository root, choosing an output directory outside tracked files:

```bash
uv run python scripts/experiments/qualify_record_reasoning.py --provider openai --output .local-evidence/record-reasoning
uv run --with agent-framework-anthropic==1.0.0b261022 --with agent-framework-core==1.24.0 python scripts/experiments/qualify_record_reasoning.py --provider anthropic --output .local-evidence/record-reasoning
```

Each run writes a public metrics report and a separate `*.private.json` containing raw provider bodies and opaque reasoning. Keep the private files local. The committed evidence adds only ordered message/content types, serialized history sizes and tool counts derived from those bodies; it contains no resource endpoints, deployment aliases, credentials or opaque provider tokens. The adapters' private serialization helpers are version-specific; the report records the installed versions so a later adapter update requires fresh qualification.
