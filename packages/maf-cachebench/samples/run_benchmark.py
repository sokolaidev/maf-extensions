"""Compare one compaction strategy against the uncompacted baseline on a live provider.

This is the programmatic equivalent of:

    cachebench --providers azure --sizes mid --strategies none,context_window

Use the CLI for real sweeps. This sample exists to show how the pieces fit together when
you want to drive the benchmark from your own code, for example to plug in a custom
CompactionStrategy of your own.

Requires the environment variables for whichever provider you select. For the default
Azure provider that is AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and
AZURE_OPENAI_CHAT_COMPLETION_MODEL.
"""

import asyncio
import os

from agent_framework import CharacterEstimatorTokenizer

from maf_cachebench import (
    CellKey,
    ProviderCaller,
    build_preset,
    build_provider,
    build_strategy,
    prompt_cache_key_options,
    render_summary_table,
    resolve_context_window,
    run_cell,
    summarize_cell,
)
from maf_cachebench._run_identity import new_run_id
from maf_cachebench._strategies import StrategyOptions

PROVIDER = os.environ.get("CACHEBENCH_PROVIDER", "azure")
SIZE = "mid"


async def main() -> None:
    """Replay the same transcript with and without compaction, then print the comparison."""
    tokenizer = CharacterEstimatorTokenizer()
    runtime = build_provider(PROVIDER, temperature=0.0, response_max_tokens=16)
    run_id = new_run_id()

    summaries = []
    for strategy_name in ("none", "context_window"):
        cell = CellKey(
            provider=PROVIDER,
            model=runtime.model,
            transcript=SIZE,
            strategy=strategy_name,
            repeat=1,
        )
        # A distinct salt per cell keeps the two runs in separate cache namespaces, so the
        # baseline cannot serve cache hits to the compacted run.
        salt = f"{run_id}-{cell.label}"
        transcript = build_preset(SIZE, salt=salt, tokenizer=tokenizer)
        caller = ProviderCaller(runtime, extra_options=prompt_cache_key_options(PROVIDER, salt))
        options = StrategyOptions(
            tokenizer=tokenizer,
            max_context_window_tokens=resolve_context_window(transcript.approx_final_prompt_tokens),
            max_output_tokens=512,
        )
        print(f"running {cell.label} ({len(transcript.turns)} turns)...")
        records = await run_cell(
            cell=cell,
            transcript=transcript,
            strategy=build_strategy(strategy_name, options),
            tokenizer=tokenizer,
            caller=caller,
        )
        summaries.append(
            summarize_cell(
                records,
                cell=cell,
                reports_cache_tokens=bool(records)
                and all(record.cached_tokens is not None for record in records),
            )
        )

    print()
    print(render_summary_table(summaries, cache_read_ratio=0.25))


if __name__ == "__main__":
    asyncio.run(main())
