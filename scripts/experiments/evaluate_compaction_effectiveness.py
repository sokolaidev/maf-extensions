"""Screen paired prompt retention and cache usage through cachebench's live runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from agent_framework._compaction import included_token_count
from compare_compaction_prompts import observations, plan
from compare_compaction_retention import prompt_policy
from maf_cachebench import _live, _live_cli
from maf_cachebench._metrics import serialize_message
from maf_cachebench._providers import ConfiguredClient, build_provider, parse_provider_selector
from maf_cachebench._recall import COMBINED_SCOPE, score_answer
from maf_cachebench._tokenizers import build_tokenizer
from maf_compaction import _composed
from maf_compaction._summary_input import summary_request

STRATEGY = "tool_and_user_summary_anchored"
SETTINGS = (
    "--context-window 24000 --fill 0 --tool-share 0 --filler-turns 3 "
    "--filler-tokens 1800 --tool-result-tokens 1000 --tool-turns 4 --markers-per-tool 3 "
    "--tokenizer tiktoken --max-output-tokens 4096 --record-max-tokens 4096 "
    "--answer-max-tokens 4096 --trigger-fraction 0.35 --keep-head-groups 1 "
    "--keep-tail-groups 1 --keep-last-tool-groups 1 --no-temperature "
    "--probe-repeats 1 --combined-repeats 1 --narration neutral"
)


def namespace(run_id: str, seed: int, variant: str) -> str:
    """Use equal-width decimal groups so paired namespace prefixes tokenize equally."""
    digest = hashlib.sha256(f"{run_id}/{seed}/{variant}".encode()).digest()
    return " ".join(f"{int.from_bytes(digest[i : i + 2]):03}"[-3:] for i in range(0, 16, 2))


def scenario_digest(scenario: Any) -> str:
    """Hash workload content without freshly allocated message identifiers."""
    payload = {
        "requests": [
            [serialize_message(message) for message in turn.request]
            for turn in scenario.transcript.turns
        ],
        "facts": [asdict(fact) for fact in scenario.facts],
        "contradictions": [asdict(item) for item in scenario.contradictions],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def usage(response: Any) -> dict[str, Any]:
    """Keep cache writes absent when neither the framework nor provider reports them."""
    normalized = dict(response.usage_details or {})
    raw = getattr(getattr(response, "raw_representation", None), "usage", None)
    raw = raw.model_dump() if raw is not None and hasattr(raw, "model_dump") else raw
    details = (raw or {}).get("input_tokens_details") or {}
    return {
        "input": normalized.get("input_token_count"),
        "cached": normalized.get("cache_read_input_token_count"),
        "output": normalized.get("output_token_count"),
        "cache_write": normalized.get(
            "cache_write_input_token_count", details.get("cache_write_tokens")
        ),
        "provider_usage": raw,
    }


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum each usage component only when every response reported it."""
    result: dict[str, Any] = {"calls": len(rows)}
    for key in ("input", "cached", "output", "cache_write"):
        values = [row["usage"][key] for row in rows]
        result[key] = sum(values) if all(value is not None for value in values) else None
    result["hit_rate"] = (
        result["cached"] / result["input"]
        if result["input"] and result["cached"] is not None
        else None
    )
    result["uncached"] = (
        result["input"] - result["cached"]
        if result["input"] is not None and result["cached"] is not None
        else None
    )
    return result


class Trace:
    """Retain numeric observations and synthetic summaries, excluding provider error text."""

    def __init__(self, path: Path, seed: int, variant: str) -> None:
        self.path, self.seed, self.variant = path, seed, variant
        self.phase = "seed"
        self.rows: list[dict[str, Any]] = []

    def emit(self, kind: str, **values: Any) -> None:
        row = {
            "seed": self.seed,
            "variant": self.variant,
            "phase": self.phase,
            "kind": kind,
            **values,
        }
        self.rows.append(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    @contextmanager
    def capture(self, prompts: dict[str, str]) -> Iterator[None]:
        record = _live.UsageRecorder._record
        summarize = _live.MeteredClient.get_response
        compact = _composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy._compact
        serialize = _live.serialize_history

        def recorded(recorder: Any, context: Any, response: Any) -> None:
            record(recorder, context, response)
            self.emit(
                "agent",
                call=len(recorder.calls),
                usage=usage(response),
                finish_reason=str(response.finish_reason),
            )

        async def summarized(client: Any, *args: Any, **kwargs: Any) -> Any:
            messages = args[0] if args else kwargs["messages"]
            prompt, source = messages[0].text, messages[1].text
            operation = next((key for key, value in prompts.items() if value == prompt), "unknown")
            start = perf_counter()
            try:
                response = await summarize(client, *args, **kwargs)
            except Exception as error:
                self.emit("summary_failure", operation=operation, error_type=type(error).__name__)
                raise
            self.emit(
                "summary",
                operation=operation,
                source=source,
                answer=response.text or "",
                source_chars=len(source),
                answer_chars=len(response.text or ""),
                usage=usage(response),
                elapsed_seconds=perf_counter() - start,
                finish_reason=str(response.finish_reason),
            )
            return response

        async def compacted(strategy: Any, messages: Any) -> bool:
            before = included_token_count(messages)
            calls = sum(row["kind"] == "agent" for row in self.rows)
            try:
                changed = await compact(strategy, messages)
            except Exception as error:
                self.emit(
                    "compaction_failure",
                    error_type=type(error).__name__,
                    tokens=included_token_count(messages),
                    budget=strategy._budget,
                    agent_calls_before=calls,
                )
                raise
            after = included_token_count(messages)
            self.emit(
                "compaction",
                before=before,
                after=after,
                budget=strategy._budget,
                changed=changed,
                agent_calls_before=calls,
            )
            return changed

        def snapshot(*args: Any, **kwargs: Any) -> Any:
            self.phase = "probe"
            return serialize(*args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(_live.UsageRecorder, "_record", recorded))
            stack.enter_context(patch.object(_live.MeteredClient, "get_response", summarized))
            stack.enter_context(
                patch.object(
                    _composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
                    "_compact",
                    compacted,
                )
            )
            stack.enter_context(patch.object(_live, "serialize_history", snapshot))
            yield


def outcome_report(
    outcome: Any, scenario: Any, trace: Trace, *, context_window: int
) -> dict[str, Any]:
    """Score the snapshot and separate conversational usage from measurement probes."""
    scores = _live_cli._sample_scores(outcome, scenario)
    fact_outcomes = (
        scores[0].outcomes if scores else score_answer("", scenario.facts, outcome.snapshot_prompt)
    )
    answered = {(probe.scope, probe.repeat) for probe in outcome.probes if probe.answer.strip()}
    required_scoped = {
        (scope, repeat)
        for scope in scenario.answer_scopes
        if scope != COMBINED_SCOPE
        for repeat in range(1, outcome.probe_repeats + 1)
    }
    required_combined = {
        (scope, repeat)
        for scope in scenario.answer_scopes
        if scope == COMBINED_SCOPE
        for repeat in range(1, outcome.combined_repeats + 1)
    }
    agents = [row for row in trace.rows if row["kind"] == "agent" and row["phase"] == "seed"]
    summaries = [row for row in trace.rows if row["kind"] == "summary" and row["phase"] == "seed"]
    compactions = [
        row for row in trace.rows if row["kind"] == "compaction" and row["phase"] == "seed"
    ]
    probe_calls = sum(len(probe.calls) for probe in outcome.probes)
    if len(agents) != len(outcome.calls) - probe_calls:
        raise ValueError("Trace phase accounting disagrees with cachebench")
    disqualified = outcome.disqualified(context_window)
    return {
        "seed": trace.seed,
        "variant": trace.variant,
        "failed": outcome.error is not None or disqualified,
        "disqualified": disqualified,
        "context_window": context_window,
        "prompt_tokens_peak": outcome.prompt_tokens_peak,
        "steps_completed": outcome.turns_completed,
        "steps_total": outcome.turns_total,
        "facts_left": sum(item.survived for item in fact_outcomes),
        "facts_total": len(scenario.facts),
        "scoped_accuracy": [score.correctness_score for score in scores]
        if required_scoped and required_scoped <= answered
        else [],
        "combined_accuracy": list(_live.score_combined_samples(outcome, scenario))
        if required_combined and required_combined <= answered
        else [],
        "fact_outcomes": [asdict(item) for item in fact_outcomes],
        "agent_seed": totals(agents),
        "summarizer_seed": totals(summaries),
        "workload": totals(agents + summaries),
        "first_agent_cached": agents[0]["usage"]["cached"] if agents else None,
        "agent_seed_calls": [{"call": row["call"], "usage": row["usage"]} for row in agents],
        "compactions": compactions,
        "summary_operations": {
            key: sum(row["operation"] == key for row in summaries)
            for key in ("summary", "fold", "merge", "rewrite_1", "rewrite_2")
        },
        "diagnostics": outcome.compaction_diagnostics,
        "summarizer_failures": outcome.summarizer_failures,
        "context_drift": outcome.context_drift,
        "rate_limit_retries": outcome.rate_limit_retries,
        "connection_retries": outcome.connection_retries,
        "probe_answers": [{"scope": item.scope, "answer": item.answer} for item in outcome.probes],
    }


async def conversation(args: Any, seed: int, variant: str, run_id: str) -> dict[str, Any]:
    """Run one fresh conversation with a stable, independent cache prefix."""
    provider, model = parse_provider_selector(args.provider)
    settings = _live_cli.build_parser().parse_args([args.provider, *SETTINGS.split()])
    scenario = _live.build_live_scenario(
        salt=f"{args.pair_id}-{seed}",
        filler_turns=3,
        filler_tokens=1800,
        tool_turns=4,
        markers_per_tool=3,
        filler_tool_turns=0,
        narration="neutral",
        subset_questions=True,
    )
    cache_id = namespace(run_id, seed, variant)
    prefix = f"Benchmark cache identifier (not a task requirement): {cache_id}.\n"
    trace = Trace(args.trace, seed, variant)
    trace.emit(
        "start",
        cache_id=cache_id,
        scenario_sha256=scenario_digest(scenario),
    )
    start = perf_counter()
    async with build_provider(
        provider, model=model, temperature=None, response_max_tokens=4096
    ) as runtime:
        async with build_provider(
            provider, model=model, temperature=None, response_max_tokens=1024
        ) as summary:
            runtime.options["extra_body"] = {"prompt_cache_key": f"agent-{cache_id}"}
            summary.options["extra_body"] = {"prompt_cache_key": f"summary-{cache_id}"}
            meter = _live.MeteredClient(ConfiguredClient(summary.client, summary.options))
            options = _live_cli._strategy_options(settings, build_tokenizer("tiktoken"), meter)
            _live_cli.mute_unrooted_summary_warnings()
            with (
                prompt_policy(variant, prefix=prefix) as prompts,
                trace.capture(prompts),
                patch.dict(
                    _live._INSTRUCTIONS_BY_NARRATION,
                    {
                        key: prefix + value
                        for key, value in _live._INSTRUCTIONS_BY_NARRATION.items()
                    },
                ),
            ):
                outcome = await _live.run_live(
                    runtime,
                    strategy_name=STRATEGY,
                    options=options,
                    scenario=scenario,
                    tool_result_tokens=1000,
                    narration="neutral",
                    probe_repeats=1,
                    combined_repeats=1,
                    answer_max_tokens=4096,
                    record_max_tokens=4096,
                )
    result = outcome_report(outcome, scenario, trace, context_window=settings.context_window)
    result.update(
        elapsed_seconds=perf_counter() - start,
        cache_id=cache_id,
        scenario_sha256=trace.rows[0]["scenario_sha256"],
    )
    trace.emit("end", failed=result["failed"], facts_left=result["facts_left"])
    return result


async def targeted(args: Any, run_id: str, save: Any, report: dict[str, Any]) -> None:
    """Cover rare operations with identical benign fixtures, independently of live coverage."""
    provider, model = parse_provider_selector(args.provider)
    async with build_provider(
        provider, model=model, temperature=None, response_max_tokens=2048
    ) as runtime:
        for index, row in enumerate(item for item in plan(1) if item["case"] == "benign"):
            cache_id = namespace(run_id, index, row["variant"])
            prefix = f"Benchmark cache identifier (not a task requirement): {cache_id}.\n"
            try:
                async with asyncio.timeout(args.timeout):
                    response = await runtime.client.get_response(
                        summary_request(prefix + row["prompt"], row["source"]),
                        options={**runtime.options, "extra_body": {"prompt_cache_key": cache_id}},
                    )
                row.update(observations(row["source"], response.text or ""))
                row.update(usage=usage(response), finish_reason=str(response.finish_reason))
            except Exception as error:
                row["error_type"] = type(error).__name__
            report["targeted"].append(row)
            save()
            print(f"targeted {row['operation']} {row['variant']}", flush=True)


async def run(args: Any) -> None:
    """Alternate paired conversations and preserve every completed or failed attempt."""
    run_id = uuid4().hex
    report: dict[str, Any] = {
        "run_id": run_id,
        "pair_id": args.pair_id,
        "provider": args.provider,
        "settings": SETTINGS,
        "strategy": STRATEGY,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("maf-cachebench", "maf-compaction", "agent-framework-core")
        },
        "prices": None,
        "limitations": [
            "Three pairs are a screen, not a statistical equivalence result.",
            "Targeted synthetic fixtures do not prove live operation coverage.",
            "Cache-write usage and actual prices are required for a billing verdict.",
            "No uncompacted control or strategy ranking is claimed.",
        ],
        "conversations": [],
        "targeted": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() or args.trace.exists():
        raise ValueError("Use new output paths; completed evidence is not overwritten")

    def save() -> None:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    for seed in range(1, args.pairs + 1):
        for variant in ("baseline", "hardened") if seed % 2 else ("hardened", "baseline"):
            print(f"starting seed {seed} {variant}", flush=True)
            try:
                async with asyncio.timeout(args.timeout):
                    row = await conversation(args, seed, variant, run_id)
            except Exception as error:
                row = {
                    "seed": seed,
                    "variant": variant,
                    "failed": True,
                    "error_type": type(error).__name__,
                }
            report["conversations"].append(row)
            save()
            print(
                json.dumps(
                    {
                        key: row.get(key)
                        for key in (
                            "seed",
                            "variant",
                            "failed",
                            "facts_left",
                            "agent_seed",
                            "workload",
                        )
                    }
                ),
                flush=True,
            )
    await targeted(args, run_id, save, report)
    if any(row["failed"] for row in report["conversations"]) or any(
        "error_type" in row for row in report["targeted"]
    ):
        raise SystemExit("Some attempts failed; retained evidence includes those attempts.")


def main() -> None:
    """Read the bounded experiment settings and run the paired screen."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if not args.provider.startswith("azure-responses:"):
        parser.error(
            "This experiment requires an Azure Responses model with prompt_cache_key support"
        )
    if args.pairs < 1 or args.timeout <= 0:
        parser.error("pairs and timeout must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
