# Verify compaction and cachebench together

This bounded live sample replays the same six-turn synthetic conversation through `maf-cachebench`, first without compaction and then with `maf-compaction`'s `AnchoredCompactionStrategy`. All twelve requests reach the configured Azure model. Host records must show successful provider usage and a smaller final local prompt projection with compaction. Cache hits are reported when available but are not required: provider cache availability is not deterministic. This is an integration smoke check, not a cache-performance or recall-quality benchmark.

Set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_CHAT_MODEL`, sign in with `az login`, then run:

```bash
uv run samples/22_compaction_cachebench/agent.py
```

The sample creates no sandbox or Azure resources and spends model tokens only. Each request has a timeout and one transient retry; the entire sample is bounded to fifteen minutes. Host measurements include both installed package versions, every request's usage and the local projection size. Model replies are not used as evidence.

Published mode requires a cachebench release admitting compaction 0.8. The existing cachebench 0.3.0 wheel caps compaction below 0.7 and cannot resolve this sample. Branch mode tests the updated dependency range; it does not establish publication. Release verification must fail rather than override wheel requirements or silently test an older compaction release.
