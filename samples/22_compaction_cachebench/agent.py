"""Replay a bounded live benchmark with and without anchored compaction."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "agent-framework-openai",
#     "azure-core[aio]",
#     "azure-identity",
#     "maf-cachebench>=0.3.0",
#     "maf-compaction>=0.8.0",
# ]
# ///

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from importlib.metadata import version

from _scaffold import MEASURED, require_env_vars
from agent_framework import CharacterEstimatorTokenizer
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from maf_cachebench import (
    CellKey,
    ProviderCaller,
    ProviderRuntime,
    TurnCaller,
    TurnRecord,
    build_transcript,
    run_cell,
)
from maf_compaction import AnchoredCompactionStrategy

TURNS = 6


async def benchmark(caller: TurnCaller, model: str, salt: str) -> dict[str, list[TurnRecord]]:
    """Exercise the same transcript under baseline and packaged compaction strategies."""
    tokenizer = CharacterEstimatorTokenizer()
    records: dict[str, list[TurnRecord]] = {}
    for name in ("none", "anchored"):
        transcript = build_transcript(
            name="live-smoke",
            turns=TURNS,
            salt=salt + name.ljust(8),
            tokenizer=tokenizer,
            system_tokens=100,
            user_tokens=30,
            assistant_tokens=30,
            tool_result_tokens=900,
            tool_call_every=1,
        )
        strategy = (
            None
            if name == "none"
            else AnchoredCompactionStrategy(
                max_input_tokens=1800,
                tokenizer=tokenizer,
                keep_head_groups=1,
                keep_tail_groups=1,
                keep_tokens=80,
            )
        )
        records[name] = await run_cell(
            cell=CellKey(
                provider="azure", model=model, transcript="live-smoke", strategy=name, repeat=1
            ),
            transcript=transcript,
            strategy=strategy,
            tokenizer=tokenizer,
            caller=caller,
        )
    return records


async def run() -> int:
    """Send twelve bounded model requests and report provider usage and local projection sizes."""
    env = require_env_vars(("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_CHAT_MODEL"))
    if env is None:
        return 2
    packages = ("maf-cachebench", "maf-compaction")
    print(f"{MEASURED}installed: " + ", ".join(f"{name} {version(name)}" for name in packages))
    async with DefaultAzureCredential() as credential:
        client = OpenAIChatClient(
            model=env["AZURE_OPENAI_CHAT_MODEL"],
            azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
            credential=credential,
        )
        runtime = ProviderRuntime(
            client=client, model=env["AZURE_OPENAI_CHAT_MODEL"], options={"max_tokens": 128}
        )
        caller = ProviderCaller(runtime, request_timeout=60, max_retries=1)
        records = await benchmark(caller, runtime.model, uuid.uuid4().hex)
    for name, turns in records.items():
        for turn in turns:
            print(f"{MEASURED}" + json.dumps({"stage": "turn", "strategy": name, **asdict(turn)}))
    baseline, compacted = records["none"], records["anchored"]
    if any(turn.error or not turn.input_tokens for turns in records.values() for turn in turns):
        raise RuntimeError("Every model request must succeed and report input usage")
    if compacted[-1].sent_tokens_local >= baseline[-1].sent_tokens_local:
        raise RuntimeError("Anchored compaction did not reduce the measured final projection")
    print(f'{MEASURED}{{"stage": "complete", "turns": {TURNS * 2}}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.wait_for(run(), timeout=900)))
