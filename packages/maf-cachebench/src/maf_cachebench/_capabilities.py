"""Run reproducible offline capability scenarios through Cachebench's shared builders."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from copy import deepcopy
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from agent_framework import AgentSession, CharacterEstimatorTokenizer, HistoryProvider, Message
from agent_framework._compaction import EXCLUDED_KEY
from maf_compaction import (
    MediaTokenEstimator,
    RecallGate,
    ToolResultRecallMiddleware,
    is_preserved,
    make_recall_tool,
)

from ._capability_fixtures import FixtureClient, FixtureSummarizer, media_history, result_history
from ._configuration import CompactionOptions
from ._lifecycle import LiveBinding, SessionExperiment
from ._live import UsageRecorder, build_live_agent
from ._providers import ProviderRuntime
from ._strategies import StrategyOptions, build_strategy

PERSISTENCE_SCENARIOS = (
    "fenced",
    "stale",
    "branch",
    "migrate",
    "invalid_migration",
    "after_save",
    "save_failure",
    "after_save_failure",
    "request_failure",
    "interrupted_stream",
)
MEDIA_SCENARIOS = ("media_preserve", "media_describe", "media_decline", "media_select_pdf")
RESULT_SCENARIOS = ("result_preserve", "result_rewrite", "result_invalid")
SCENARIOS = (*PERSISTENCE_SCENARIOS, *MEDIA_SCENARIOS, *RESULT_SCENARIOS)


@dataclass(frozen=True)
class CapabilityRecord:
    """A capability observation, separate from provider cost/cache measurement records."""

    scenario: str
    configuration: dict[str, Any]
    observations: dict[str, Any]
    checks: dict[str, bool]
    package_versions: dict[str, str]
    error: str | None = None
    schema: int = 1
    record_type: str = "cachebench_capability"
    evidence: str = "offline_simulated"

    @property
    def passed(self) -> bool:
        """Whether every explicit check passed without an unexpected runner failure."""
        return self.error is None and bool(self.checks) and all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize resolved configuration, observations and the computed outcome."""
        return {**asdict(self), "passed": self.passed}


def _binding(model: FixtureClient, controls: CompactionOptions) -> LiveBinding:
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
        compaction=controls,
    )
    strategy = build_strategy("tool_summary_anchored", options)
    gate = RecallGate()
    recall = ToolResultRecallMiddleware(
        max_input_tokens=99_000,
        tokenizer=options.tokenizer,
        arm=gate.arm,
        disarm=gate.disarm,
    )
    agent = build_live_agent(
        ProviderRuntime(model, "offline-fixture", {}),
        kind="plain",
        strategy=strategy,
        tokenizer=options.tokenizer,
        tools=[make_recall_tool(gate)],
        recorder=UsageRecorder(),
        extra_middleware=[recall],
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
    )
    return LiveBinding(agent, strategy, recall, gate)


async def _persistence(
    scenario: str,
    *,
    stream: bool,
    session_mode: str,
    history_mode: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    controls = CompactionOptions(
        persistence_scenario=(
            "fenced" if scenario in {"request_failure", "interrupted_stream"} else scenario
        ),
        session_mode=session_mode,
        history_mode=history_mode,
        stream=stream,
    )
    model = FixtureClient(fail=scenario == "request_failure")
    experiment = SessionExperiment(lambda: _binding(model, controls), controls)
    assert experiment.binding.recall is not None
    behavior = experiment.binding.recall.session_state()
    behavior["value_free_streak"] = 2
    experiment.binding.recall.restore_session_state(behavior)
    store = experiment.persistence_store
    assert store is not None
    model.on_event = lambda event: store.events.append(event)
    session = AgentSession(session_id="fixture-source")
    session.state["fixture_value"] = "CODE-2466"
    failure: str | None = None
    returned: AgentSession | None = None
    try:
        _, returned = await experiment.run(
            session,
            "Remember CODE-2466.",
            {},
            interrupt_stream=scenario == "interrupted_stream",
        )
    except (ValueError, RuntimeError) as exc:
        failure = type(exc).__name__
    request = store.requests[-1]
    expected_failure = {
        "stale": "ScenarioFailure",
        "invalid_migration": "ScenarioFailure",
        "save_failure": "ScenarioFailure",
        "after_save_failure": "ScenarioFailure",
        "request_failure": "RuntimeError",
        "interrupted_stream": "ScenarioInterruption",
    }.get(scenario)
    expected_commit = expected_failure is None or scenario == "after_save_failure"
    events = store.events
    expected_events = ["lease_acquired", "load"]
    if scenario in {"migrate", "invalid_migration"}:
        expected_events.append("migrate")
    if scenario != "invalid_migration":
        expected_events.append("model_request")
        if stream:
            expected_events.append("stream_closed")
        elif scenario != "request_failure":
            expected_events.append("response_finished")
        if scenario not in {"request_failure", "interrupted_stream"}:
            expected_events.append("save_attempt")
            if scenario == "stale":
                expected_events.append("stale_rejected")
            if expected_commit:
                expected_events.append("committed")
            if scenario in {"after_save", "after_save_failure"}:
                expected_events.append("after_save")
    expected_events.append("lease_released")
    checks = {
        "expected_error": failure == expected_failure,
        "recorded_error": request["error"] == expected_failure,
        "commit_outcome": request["committed"] == expected_commit,
        "ordering": events == expected_events,
        "no_model_retry": len(model.requests) == (0 if scenario == "invalid_migration" else 1),
        "cleanup": store.active_token is None and not store.lock.locked(),
        "stream_cleanup": model.closed == (1 if stream and scenario != "invalid_migration" else 0),
        "notification": request["notified"] == (scenario == "after_save"),
    }
    stored = store.snapshots[store.destination if expected_commit else store.source]
    history_source = next(
        provider.source_id
        for provider in experiment.binding.agent.context_providers
        if isinstance(provider, HistoryProvider)
    )
    saved_history = [
        Message.from_dict(message)
        for message in stored["state"].get(history_source, {}).get("messages", [])
    ]
    expected_history = [
        ("user", "Remember CODE-2466."),
        ("assistant", "Fixture answer CODE-2466."),
    ]
    if expected_commit:
        stack = stored["state"].get(experiment.registry.state_key, {})
        checks["history_committed"] = [
            (message.role, message.text) for message in saved_history
        ] == expected_history
        if history_mode == "replay":
            replay = [
                Message.from_dict(message)
                for message in stored["state"].get("cachebench_original_history", [])
            ]
            checks["replay_archive_committed"] = [
                (message.role, message.text) for message in replay
            ] == expected_history
        checks["compaction_state_committed"] = (
            stack.get("version") == 5 and stack.get("middleware", {}).get("value_free_streak") == 2
        )
        checks["host_state_retained"] = stored["state"].get("fixture_value") == "CODE-2466"
        if returned is not None:
            checks["returned_saved_id"] = returned.session_id == store.destination
    else:
        checks["history_not_committed"] = not saved_history
        checks["stored_snapshot_unchanged"] = stored == store.source_snapshot
    if scenario == "branch":
        source = store.snapshots[store.source]
        checks["source_unchanged"] = source == store.source_snapshot
        checks["separate_ids"] = (
            source["session_id"] == store.source
            and stored["session_id"] == store.destination
            and store.source != store.destination
        )
        detached = AgentSession.from_dict(deepcopy(stored))
        with experiment.registry.activate(detached):
            checks["branch_restores"] = (
                experiment.binding.recall.session_state()["value_free_streak"] == 2
            )
    observations = {
        "request": request,
        "model_requests": len(model.requests),
        "streams_closed": model.closed,
        "snapshots": deepcopy(store.snapshots),
        "expected_error": expected_failure,
        "history_source": history_source,
    }
    return (
        {
            "compaction": asdict(controls),
            "strategy": "tool_summary_anchored",
            "max_context_window_tokens": 100_000,
            "max_output_tokens": 1000,
        },
        observations,
        checks,
    )


async def _media(
    scenario: str, media_tokens: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    profile = scenario.removeprefix("media_")
    controls = CompactionOptions(
        media_profile=profile, media_tokens=media_tokens, max_summary_input_tokens=5000
    )
    summarizer = FixtureSummarizer()
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=1000,
        max_output_tokens=0,
        compaction=controls,
        summarizer=cast(Any, summarizer),
        keep_head_user_turns=1,
        keep_tail_user_turns=1,
        user_min_band_share=0,
        user_trigger_fraction=0.01,
    )
    strategy = build_strategy("user_summary_anchored", options)
    assert strategy is not None
    messages = media_history()
    originals = deepcopy(messages)
    estimator = cast(MediaTokenEstimator, options.tokenizer)
    before_tokens = sum(estimator.count_message_tokens(m) for m in messages)
    changed = await strategy(messages)
    by_id = {m.message_id: m for m in messages}
    excluded = [m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY)]
    preserved = [m.message_id for m in messages if is_preserved(m)]
    summary_input = "\n".join(m.text for request in summarizer.requests for m in request)
    expected_exclusions: dict[str, set[str]] = {
        "preserve": {"later"},
        "describe": {"image", "pdf", "later"},
        "decline": set(),
        "select_pdf": {"pdf", "later"},
    }
    expected_excluded = expected_exclusions[profile]
    checks = {
        "excluded_turns": set(excluded) == expected_excluded,
        "compaction_triggered": bool(changed) == bool(expected_excluded),
        "anchors_retained": not {"head", "tail"}.intersection(excluded),
        "source_parts_unchanged": all(
            by_id[m.message_id].contents == m.contents for m in originals
        ),
        "description_input": ("Fixture application/pdf: CODE-2466." in summary_input)
        == (profile in {"describe", "select_pdf"}),
        "image_description_input": ("Fixture image/png: CODE-2466." in summary_input)
        == (profile == "describe"),
        "estimated_media_cost": estimator.estimate_media is not None
        and estimator.estimate_media(originals[1].contents[1]) == media_tokens,
    }
    if profile in {"preserve", "select_pdf"}:
        checks["rejected_media_preserved"] = "image" in preserved
    after_tokens = sum(
        estimator.count_message_tokens(m)
        for m in messages
        if not m.additional_properties.get(EXCLUDED_KEY)
    )
    observations = {
        "estimated_tokens_before": before_tokens,
        "estimated_tokens_after": after_tokens,
        "excluded_ids": excluded,
        "preserved_ids": preserved,
        "summary_input": summary_input,
        "summary_requests": len(summarizer.requests),
    }
    return (
        {
            "compaction": asdict(controls),
            "strategy": "user_summary_anchored",
            "max_context_window_tokens": 1000,
            "max_output_tokens": 0,
            "keep_head_user_turns": 1,
            "keep_tail_user_turns": 1,
            "user_min_band_share": 0,
            "user_trigger_fraction": 0.01,
        },
        observations,
        checks,
    )


async def _result(scenario: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    profile = {
        "result_preserve": "none",
        "result_rewrite": "preserve",
        "result_invalid": "invalid",
    }[scenario]
    controls = CompactionOptions(result_rewrite_profile=profile)
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=0,
        keep_head_groups=0,
        keep_tail_groups=0,
        keep_tokens=100,
        compaction=controls,
    )
    strategy = build_strategy("anchored", options)
    assert strategy is not None
    messages = result_history()
    content = messages[1].contents[0]
    before = deepcopy(content)
    failure: str | None = None
    try:
        await strategy(messages)
    except ValueError as exc:
        failure = str(exc)
    before_text = next(item for item in before.items or [] if item.type == "text")
    after_text = next(item for item in content.items or [] if item.type == "text")
    checks = {
        "invalid_rewrite_rejected": (failure is not None) == (profile == "invalid"),
        "shortened_only_when_authorized": (len(after_text.text or "") < len(before_text.text or ""))
        == (profile == "preserve"),
        "item_metadata_retained": after_text.additional_properties
        == before_text.additional_properties,
        "result_metadata_retained": content.additional_properties == before.additional_properties,
        "media_retained_in_order": [p.to_dict() for p in content.items or [] if p.type != "text"]
        == [p.to_dict() for p in before.items or [] if p.type != "text"],
    }
    if profile == "preserve":
        checks["wire_text_matches"] = content.result == after_text.text
    else:
        checks["unchanged_result"] = content.to_dict() == before.to_dict()
    observations = {
        "error": failure,
        "text_characters_before": len(before_text.text or ""),
        "text_characters_after": len(after_text.text or ""),
        "result": content.to_dict(),
    }
    return (
        {
            "compaction": asdict(controls),
            "strategy": "anchored",
            "keep_tokens": 100,
            "max_context_window_tokens": 100_000,
            "max_output_tokens": 0,
            "keep_head_groups": 0,
            "keep_tail_groups": 0,
        },
        observations,
        checks,
    )


async def run_capability_scenario(
    scenario: str,
    *,
    stream: bool = False,
    session_mode: str = "restart",
    history_mode: str = "replay",
    media_tokens: int = 1024,
) -> CapabilityRecord:
    """Measure one named fixture; no network, provider credentials or storage service is used."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown capability scenario: {scenario}")
    if scenario == "interrupted_stream" and not stream:
        raise ValueError("interrupted_stream requires streaming")
    if session_mode not in {"roundtrip", "restart"}:
        raise ValueError("session_mode must be roundtrip or restart")
    configuration: dict[str, Any] = {
        "fixture_version": 1,
        "stream": stream,
        "session_mode": session_mode,
        "history_mode": history_mode,
        "media_tokens": media_tokens,
    }
    observations: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    error = None
    try:
        if scenario in PERSISTENCE_SCENARIOS:
            resolved, observations, checks = await _persistence(
                scenario,
                stream=stream,
                session_mode=session_mode,
                history_mode=history_mode,
            )
        elif scenario in MEDIA_SCENARIOS:
            resolved, observations, checks = await _media(scenario, media_tokens)
        else:
            resolved, observations, checks = await _result(scenario)
        configuration.update(resolved)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return CapabilityRecord(
        scenario,
        configuration,
        observations,
        checks,
        {
            name: version(name)
            for name in ("maf-cachebench", "maf-compaction", "agent-framework-core")
        }
        | {"python": platform.python_version()},
        error=error,
    )


def main(argv: list[str] | None = None) -> int:
    """Write JSONL capability evidence and fail if any measured contract fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--session-mode", choices=("roundtrip", "restart"), default="restart")
    parser.add_argument("--history-mode", choices=("incremental", "replay"), default="replay")
    parser.add_argument("--media-tokens", type=int, default=1024)
    parser.add_argument("--output", type=Path, help="Write JSONL to a new file (never overwrite).")
    args = parser.parse_args(argv)
    if args.scenario == "interrupted_stream" and not args.stream:
        parser.error("interrupted_stream requires --stream")
    if args.media_tokens <= 0:
        parser.error("--media-tokens must be positive")

    async def run() -> list[CapabilityRecord]:
        scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
        return [
            await run_capability_scenario(
                name,
                stream=args.stream or name == "interrupted_stream",
                session_mode=args.session_mode,
                history_mode=args.history_mode,
                media_tokens=args.media_tokens,
            )
            for name in scenarios
        ]

    records = asyncio.run(run())
    encoded = "".join(json.dumps(record.to_dict(), sort_keys=True) + "\n" for record in records)
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as target:
            target.write(encoded)
    else:
        print(encoded, end="")
    return 0 if all(record.passed for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
