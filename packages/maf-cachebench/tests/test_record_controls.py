"""Record reduction controls and background work through the benchmark lifecycle."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from hashlib import sha256
from typing import Any, cast

import maf_compaction
import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Message
from test_compaction_features import Model, Summarizer, binding_factory

from maf_cachebench import (
    CompactionOptions,
    MeteredClient,
    ProviderRuntime,
    StrategyOptions,
    UsageRecorder,
    build_live_scenario,
    build_strategy,
    run_live,
)
from maf_cachebench._configuration import compaction_options
from maf_cachebench._lifecycle import SessionExperiment
from maf_cachebench._live_cli import _strategy_options, _strategy_settings, build_parser
from maf_cachebench._merge_evidence import MergeEvidence
from maf_cachebench._merge_workload import PRESSURE_REQUEST
from maf_cachebench._records import SCHEMA_VERSION, _settings_from_dict

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("share,seconds", [("0.2", "4.5"), ("none", "none")])
def test_record_controls_reach_cli_strategy_and_saved_identity(share, seconds):
    args = build_parser().parse_args(
        [
            "azure",
            "--record-budget-share",
            share,
            "--chain-seconds",
            seconds,
            "--record-merge-split-depth",
            "3",
            "--rewrite-gain-fraction",
            "0.25",
            "--background-record-merges",
            "--tool-delay-seconds",
            "0.25",
        ]
    )
    controls = compaction_options(args)
    options = _strategy_options(
        args, CharacterEstimatorTokenizer(), summarizer=cast(Any, Summarizer())
    )
    strategy = build_strategy("tool_and_user_summary_anchored", options)
    assert isinstance(
        strategy, maf_compaction.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    )
    assert strategy.record_budget_share == (None if share == "none" else 0.2)
    assert strategy.chain_seconds == (None if seconds == "none" else 4.5)
    assert strategy.record_merge_split_depth == 3
    assert strategy.rewrite_gain_fraction == 0.25
    settings = _strategy_settings(args, options, summarizer=None)
    assert _settings_from_dict(settings.to_dict()) == settings
    assert settings.compaction == controls
    assert controls.tool_delay_seconds == 0.25
    for name, value in {
        "record_budget_share": 0.3,
        "chain_seconds": 6.0,
        "record_merge_split_depth": 0,
        "rewrite_gain_fraction": 0.0,
        "background_record_merges": False,
        "record_merge_diagnostics": True,
        "qualify_record_merges": True,
        "tool_delay_seconds": 0.0,
    }.items():
        assert (
            settings.to_dict()
            != replace(settings, compaction=replace(controls, **{name: value})).to_dict()
        )


def test_older_record_controls_remain_absent_on_roundtrip():
    args = build_parser().parse_args(["azure"])
    options = _strategy_options(args, CharacterEstimatorTokenizer(), summarizer=None)
    data = _strategy_settings(args, options, summarizer=None).to_dict()
    for name in (
        "record_budget_share",
        "chain_seconds",
        "record_merge_split_depth",
        "rewrite_gain_fraction",
        "background_record_merges",
        "record_merge_diagnostics",
        "qualify_record_merges",
        "tool_delay_seconds",
    ):
        del data["compaction"][name]
    restored = _settings_from_dict(data)
    assert restored.to_dict() == data
    assert "rewrite_gain_fraction" in restored.unrecorded_compaction_fields
    assert "qualify_record_merges" in restored.unrecorded_compaction_fields
    assert hash(restored) == hash(_settings_from_dict(data))
    assert SCHEMA_VERSION == 26


def test_worker_acceptance_does_not_qualify_and_observed_fingerprints_stay_private():
    evidence = MergeEvidence()
    replacement = Message("assistant", ["qualified synthetic record"])
    identifier = sha256(replacement.text.encode()).hexdigest()
    data = {"tracked_values": 3, "kept_tracked_values": 3, "replacement": identifier}
    evidence.observe("background", "answer_accepted", data)
    evidence.sent([replacement])
    assert "apply.value_preserving_merge_sent" not in evidence.counts
    evidence.observe("apply", "answer_accepted", {**data, "kept_tracked_values": 2})
    evidence.sent([replacement])
    assert "apply.value_preserving_merge_sent" not in evidence.counts
    evidence.observe("apply", "answer_accepted", data)
    evidence.sent([Message("user", [replacement.text])])
    assert "apply.value_preserving_merge_sent" not in evidence.counts
    evidence.sent([replacement])
    evidence.sent([replacement])
    assert evidence.counts["apply.value_preserving_merge_sent"] == 1
    serialized = json.dumps(evidence.snapshot())
    assert identifier not in serialized
    assert replacement.text not in serialized


def test_merge_survey_sample_is_bounded_but_counts_include_every_pass():
    evidence = MergeEvidence()
    for index in range(100):
        evidence.observe("background", "survey", {"pressure": 1, "eligible": 0, "records": index})
    summary = evidence.snapshot()
    counts = cast(dict[str, int], summary["counts"])
    by_status = cast(dict[str, list[dict[str, int | str]]], summary["surveys"])
    assert counts["background.too_few_eligible"] == 100
    surveys = by_status["background.too_few_eligible"]
    assert len(surveys) == 16
    assert surveys[-1]["records"] == 99


async def test_qualification_checkpoint_adds_pressure_once():
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=5000,
        max_output_tokens=1000,
        summarizer=cast(Any, Summarizer()),
        compaction=CompactionOptions(qualify_record_merges=True),
    )
    strategy = build_strategy("tool_and_user_summary_anchored", options)
    assert isinstance(
        strategy, maf_compaction.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    )
    request = Message("user", [PRESSURE_REQUEST])
    messages = [request]
    await strategy.compact_before(messages, options={})
    padded = request.text
    assert padded.startswith(PRESSURE_REQUEST) and len(padded) > len(PRESSURE_REQUEST)
    await strategy.compact_before(messages, options={})
    assert request.text == padded


@pytest.mark.parametrize(
    "name,value",
    [
        ("record_budget_share", 0),
        ("record_budget_share", 1.1),
        ("record_budget_share", True),
        ("record_budget_share", float("nan")),
        ("chain_seconds", 0),
        ("chain_seconds", float("inf")),
        ("record_merge_split_depth", -1),
        ("record_merge_split_depth", 1.5),
        ("record_merge_split_depth", True),
        ("rewrite_gain_fraction", -0.1),
        ("rewrite_gain_fraction", 1),
        ("rewrite_gain_fraction", True),
        ("rewrite_gain_fraction", float("nan")),
        ("tool_delay_seconds", -1),
        ("tool_delay_seconds", float("nan")),
    ],
)
def test_invalid_record_controls_are_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        CompactionOptions(**{name: value})


@pytest.mark.parametrize("background", [False, True])
async def test_live_lookup_delay_is_recorded_without_changing_tool_results(background):
    delays = []

    async def pause(seconds):
        delays.append(seconds)

    scenario = build_live_scenario(salt="delay", filler_turns=1, filler_tokens=20, tool_turns=1)
    scope = next(iter(scenario.tool_lookups))
    model = Model(tool=f"lookup_{scope}")
    outcome = await run_live(
        ProviderRuntime(model, "stub", {}),
        strategy_name="tool_and_user_summary_anchored" if background else "none",
        options=StrategyOptions(
            tokenizer=CharacterEstimatorTokenizer(),
            max_context_window_tokens=100_000,
            max_output_tokens=1000,
            summarizer=cast(Any, Summarizer()),
            compaction=CompactionOptions(
                background_record_merges=background, tool_delay_seconds=0.25
            ),
        ),
        scenario=scenario,
        force_tool_calls=False,
        probe_repeats=1,
        combined_repeats=1,
        sleep=pause,
    )
    assert outcome.error is None
    assert delays == [0.25]
    assert outcome.tool_calls_made == 1
    assert outcome.scopes_called == (scope,)
    tool_results = [
        str(content.result)
        for messages, _ in model.requests
        for message in messages
        for content in message.contents
        if content.type == "function_result"
    ]
    assert any(
        all(code in result for code in scenario.tool_lookups[scope]) for result in tool_results
    )


def _history():
    messages = [Message("user", ["Keep the deployment codes."], message_id="task")]
    for index in range(4):
        body = f"CODE-{index:06d} " + "deployment details " * 200
        messages.extend(
            [
                Message(
                    "assistant",
                    [
                        {
                            "type": "function_call",
                            "call_id": f"rec{index}",
                            "name": maf_compaction.RECALL_TOOL_NAME,
                            "arguments": json.dumps({"values": body}),
                        }
                    ],
                    message_id=f"call{index}",
                ),
                Message(
                    "tool",
                    [
                        {
                            "type": "function_result",
                            "call_id": f"rec{index}",
                            "result": maf_compaction.make_recall_tool()(body),
                        }
                    ],
                    message_id=f"result{index}",
                ),
            ]
        )
    messages.append(Message("user", ["Compare the deployments."], message_id="last"))
    return messages


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["plain", "harness"])
@pytest.mark.parametrize("mode", ["memory", "roundtrip", "restart"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("history", ["incremental", "replay"])
async def test_background_merges_apply_or_cancel_across_session_modes(
    stream, kind, mode, ready, history
):
    entered, release, answered, cancelled = (asyncio.Event() for _ in range(4))

    class Waiting:
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            text = " ".join(sorted(set(re.findall(r"CODE-\d+", messages[-1].text))))
            answered.set()
            return ChatResponse(
                messages=Message("assistant", [text]),
                usage_details={"input_token_count": 100, "output_token_count": 20},
            )

    async def lookup() -> str:
        """Look up one deployment."""
        await asyncio.wait_for(entered.wait(), 5)
        if ready:
            release.set()
            await asyncio.wait_for(answered.wait(), 5)
        return "lookup complete"

    model = Model(tool="lookup")
    summarizer = MeteredClient(Waiting())
    controls = CompactionOptions(
        background_record_merges=True,
        record_budget_share=0.001,
        session_mode=mode,
        stream=stream,
        history_mode=history,
    )
    build = binding_factory(
        model,
        UsageRecorder(),
        kind=kind,
        options=controls,
        strategy_name="tool_and_user_summary_anchored",
        summarizer=summarizer,
        tools=[lookup],
    )
    experiment = SessionExperiment(build, controls, session_build=build)
    session = experiment.binding.agent.create_session()
    session.state["in_memory"] = {"messages": _history()}
    if history == "replay":
        session.state["cachebench_original_history"] = [message.to_dict() for message in _history()]
    response, session = await asyncio.wait_for(experiment.run(session, "Continue.", {}), 15)
    assert response.text == "answer"
    assert entered.is_set()
    assert cancelled.is_set() is not ready
    assert summarizer.cancelled == int(not ready)
    assert len(summarizer.usage) == int(ready)
    assert experiment.totals["records_merged"] == int(ready)
    assert experiment.binding.strategy._background is None
    assert not experiment.registry._merge_work
    sent = json.dumps([message.to_dict() for message in model.requests[-1][0]])
    assert all(f"CODE-{index:06d}" in sent for index in range(4))
    previous_calls = summarizer.calls
    await asyncio.wait_for(experiment.run(session, "What remains?", {}), 15)
    assert summarizer.calls == previous_calls
    assert experiment.totals["records_merged"] == int(ready)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["plain", "harness"])
async def test_background_task_is_cancelled_when_run_is_cancelled(stream, kind):
    entered, tool_entered, cancelled = (asyncio.Event() for _ in range(3))

    class Waiting:
        async def get_response(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def lookup() -> str:
        """Wait for a deployment."""
        await entered.wait()
        tool_entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    controls = CompactionOptions(
        background_record_merges=True, record_budget_share=0.001, stream=stream
    )
    summarizer = MeteredClient(Waiting())
    build = binding_factory(
        Model(tool="lookup"),
        UsageRecorder(),
        options=controls,
        kind=kind,
        strategy_name="tool_and_user_summary_anchored",
        summarizer=summarizer,
        tools=[lookup],
    )
    experiment = SessionExperiment(build, controls, session_build=build)
    session = experiment.binding.agent.create_session()
    session.state["in_memory"] = {"messages": _history()}
    task = asyncio.create_task(experiment.run(session, "Continue.", {}))
    try:
        await asyncio.wait_for(tool_entered.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()
    assert summarizer.cancelled == 1
    assert not experiment.registry._merge_work
