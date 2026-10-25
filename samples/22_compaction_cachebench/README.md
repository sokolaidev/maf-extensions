# Verify compaction and cachebench together

This bounded live sample replays the same six-turn synthetic conversation through `maf-cachebench`, first without compaction and then with `maf-compaction`'s `AnchoredCompactionStrategy`. All twelve requests reach the configured Azure model. Host records must show successful provider usage and a smaller final local prompt projection with compaction. Cache hits are reported when available but are not required: provider cache availability is not deterministic. This is an integration smoke check, not a cache-performance or recall-quality benchmark.

Set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL`, sign in with `az login`, then run:

```bash
uv run samples/22_compaction_cachebench/agent.py
```

The sample creates no sandbox or Azure resources and spends model tokens only. Each request has a timeout and one transient retry; the entire sample is bounded to fifteen minutes. Host measurements include both installed package versions, every request's usage and the local projection size. Model replies are not used as evidence.

The sample uses APIs available since compaction 0.6. Published mode resolves a compatible pair and reports both installed versions; cachebench 0.3.0 currently selects compaction 0.6 because its wheel caps below 0.7. Verifying compaction 0.8 from PyPI requires a cachebench release admitting it. A release-triggered job checks the exact requested version and refuses an older resolution. Branch mode overlays the checkout packages after resolving the published script environment and tests the updated dependency range; it does not establish publication.
