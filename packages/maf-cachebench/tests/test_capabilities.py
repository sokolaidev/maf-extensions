"""Capability fixtures must detect lost wiring, lost data, and incorrect commit outcomes."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

import pytest
from agent_framework import AgentSession, CharacterEstimatorTokenizer, Content, Message
from maf_compaction import CompactionStack, MediaTokenEstimator

import maf_cachebench._capabilities as capabilities
from maf_cachebench import (
    CompactionOptions,
    StrategyOptions,
    build_live_scenario,
    build_strategy,
    run_live,
)
from maf_cachebench._capability_fixtures import (
    FixtureClient,
    FixtureSummarizer,
    media_history,
    result_history,
)
from maf_cachebench._configuration import compaction_options
from maf_cachebench._lifecycle import SessionExperiment
from maf_cachebench._live_cli import _strategy_options, _strategy_settings, build_parser
from maf_cachebench._persistence_scenarios import ScenarioFailure, ScenarioStore
from maf_cachebench._providers import ProviderRuntime
from maf_cachebench._records import _settings_from_dict


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", capabilities.PERSISTENCE_SCENARIOS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("session_mode", ["roundtrip", "restart"])
@pytest.mark.parametrize("history_mode", ["incremental", "replay"])
async def test_persistence_matrix_reports_observed_commits_and_cleanup(
    scenario, stream, session_mode, history_mode
):
    if scenario == "interrupted_stream" and not stream:
        with pytest.raises(ValueError, match="requires streaming"):
            await capabilities.run_capability_scenario(scenario, stream=False)
        return
    record = await capabilities.run_capability_scenario(
        scenario, stream=stream, session_mode=session_mode, history_mode=history_mode
    )
    assert record.passed, record.to_dict()
    decoded = json.loads(json.dumps(record.to_dict()))
    assert decoded["evidence"] == "offline_simulated"
    assert decoded["configuration"]["compaction"]["session_mode"] == session_mode
    assert decoded["configuration"]["compaction"]["history_mode"] == history_mode
    assert decoded["observations"]["request"]["events"][-1] == "lease_released"
    assert decoded["observations"]["request"]["committed"] == (
        scenario in {"fenced", "branch", "migrate", "after_save", "after_save_failure"}
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario", (*capabilities.MEDIA_SCENARIOS, *capabilities.RESULT_SCENARIOS)
)
async def test_host_hook_scenarios_measure_retained_content(scenario):
    record = await capabilities.run_capability_scenario(scenario)
    assert record.passed, record.to_dict()
    assert json.loads(json.dumps(record.to_dict()))["passed"]


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("session_mode", ["roundtrip", "restart"])
@pytest.mark.parametrize("history_mode", ["incremental", "replay"])
async def test_successful_request_does_not_record_callers_handled_exception(
    stream, session_mode, history_mode
):
    try:
        raise ValueError("unrelated caller failure")
    except ValueError:
        record = await capabilities.run_capability_scenario(
            "fenced", stream=stream, session_mode=session_mode, history_mode=history_mode
        )
    assert record.passed, record.to_dict()
    assert record.observations["request"]["error"] is None


@pytest.mark.anyio
async def test_media_estimator_changes_projection_without_changing_description():
    small = await capabilities.run_capability_scenario("media_select_pdf", media_tokens=100)
    large = await capabilities.run_capability_scenario("media_select_pdf", media_tokens=1500)
    assert small.passed and large.passed
    assert (
        large.observations["estimated_tokens_before"]
        - small.observations["estimated_tokens_before"]
        == 2800
    )
    assert (
        large.observations["estimated_tokens_after"] - small.observations["estimated_tokens_after"]
        == 1400
    )
    assert small.observations["summary_input"] == large.observations["summary_input"]


@pytest.mark.anyio
async def test_branch_can_continue_without_mutating_its_source():
    controls = CompactionOptions(
        persistence_scenario="branch", session_mode="restart", history_mode="replay"
    )
    model = FixtureClient()
    experiment = SessionExperiment(lambda: capabilities._binding(model, controls), controls)
    assert experiment.binding.recall is not None
    behavior = experiment.binding.recall.session_state()
    behavior["value_free_streak"] = 2
    experiment.binding.recall.restore_session_state(behavior)
    _, branch = await experiment.run(AgentSession(session_id="source"), "Remember SOURCE-2466.", {})
    store = experiment.persistence_store
    assert store is not None
    source = deepcopy(store.snapshots["source"])
    first_branch = deepcopy(store.snapshots[branch.session_id])
    _, continued = await experiment.run(branch, "Add BRANCH-2466.", {})
    assert store.snapshots["source"] == source
    assert store.snapshots[branch.session_id] == first_branch
    assert continued.session_id != branch.session_id
    final = json.dumps(store.snapshots[continued.session_id])
    assert "SOURCE-2466" in final and "BRANCH-2466" in final
    assert experiment.binding.recall is not None
    assert experiment.binding.recall.session_state()["value_free_streak"] == 2


@pytest.mark.anyio
async def test_stale_scenario_fails_if_store_ignores_fencing(monkeypatch):
    async def unsafe_save(self, session_id, data, *, lease):
        self.snapshots[session_id] = deepcopy(data)
        self.committed = True
        self.events.extend(["save_attempt", "committed"])

    monkeypatch.setattr(ScenarioStore, "save", unsafe_save)
    record = await capabilities.run_capability_scenario("stale")
    assert not record.passed
    assert not record.checks["commit_outcome"]
    assert not record.checks["stored_snapshot_unchanged"]


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["fenced", "branch"])
async def test_scenario_fails_if_restart_loses_recall_state(monkeypatch, scenario):
    restore = CompactionStack.restore

    def lose_state(self, data):
        data = deepcopy(data)
        data["middleware"]["value_free_streak"] = 0
        restore(self, data)

    monkeypatch.setattr(CompactionStack, "restore", lose_state)
    record = await capabilities.run_capability_scenario(scenario)
    assert not record.passed
    assert not record.checks["compaction_state_committed"]
    if scenario == "branch":
        assert not record.checks["branch_restores"]


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["media_describe", "media_select_pdf", "result_rewrite"])
async def test_scenario_fails_when_builder_drops_the_requested_hook(monkeypatch, scenario):
    def omit_hooks(name, options):
        return build_strategy(
            name,
            replace(
                options,
                compaction=replace(
                    options.compaction, media_profile="preserve", result_rewrite_profile="none"
                ),
            ),
        )

    monkeypatch.setattr(capabilities, "build_strategy", omit_hooks)
    record = await capabilities.run_capability_scenario(scenario)
    assert not record.passed


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["fenced", "branch", "after_save_failure"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("lost_role", ["user", "assistant", "all"])
async def test_scenario_rejects_lost_provider_history(monkeypatch, scenario, stream, lost_role):
    save = ScenarioStore.save

    async def lose_history(self, session_id, data, *, lease):
        data = deepcopy(data)
        for key, state in data["state"].items():
            if (
                key != "cachebench_original_history"
                and isinstance(state, dict)
                and "messages" in state
            ):
                state["messages"] = [
                    message
                    for message in state["messages"]
                    if lost_role != "all" and message["role"] != lost_role
                ]
        await save(self, session_id, data, lease=lease)

    monkeypatch.setattr(ScenarioStore, "save", lose_history)
    record = await capabilities.run_capability_scenario(scenario, stream=stream)
    assert not record.passed
    assert not record.checks["history_committed"]
    destination = record.observations["request"]["destination"]
    assert record.observations["snapshots"][destination]["state"]["cachebench_original_history"]


@pytest.mark.parametrize("profile", ["preserve", "describe", "decline", "select_pdf"])
def test_media_normalization_preserves_composed_tokenizer_identity(profile):
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=1000,
        max_output_tokens=0,
        summarizer=cast(Any, FixtureSummarizer()),
        compaction=CompactionOptions(media_profile=profile),
    )
    replaced = replace(options, record_target_tokens=50)
    assert replaced.tokenizer is options.tokenizer
    strategy = cast(Any, build_strategy("tool_and_user_summary_anchored", replaced))
    assert strategy.tokenizer is strategy.tool_results.tokenizer is strategy.user_turns.tokenizer


@pytest.mark.parametrize("custom_estimator", [False, True])
def test_media_normalization_updates_cost_and_restores_original_tokenizer(custom_estimator):
    base = CharacterEstimatorTokenizer()
    original = MediaTokenEstimator(base, image_tokens=73) if custom_estimator else base
    options = StrategyOptions(
        tokenizer=original,
        max_context_window_tokens=1000,
        max_output_tokens=0,
        compaction=CompactionOptions(media_profile="preserve", media_tokens=100),
    )
    changed = replace(options, compaction=replace(options.compaction, media_tokens=200))
    message = Message(
        "user", [Content.from_uri("https://example.invalid/image", media_type="image/png")]
    )
    assert isinstance(changed.tokenizer, MediaTokenEstimator)
    assert isinstance(options.tokenizer, MediaTokenEstimator)
    assert (
        changed.tokenizer.count_message_tokens(message)
        - options.tokenizer.count_message_tokens(message)
        == 100
    )
    disabled = replace(changed, compaction=replace(changed.compaction, media_profile="none"))
    assert disabled.tokenizer is original


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name",
    [
        "anchored",
        "anchored_no_assistant",
        "anchored_min_gain",
        "tool_summary_anchored",
        "tool_and_user_summary_anchored",
    ],
)
async def test_rewrite_policy_reaches_every_result_builder_and_fallback(name):
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=0,
        keep_head_groups=0,
        keep_tail_groups=0,
        keep_tokens=100,
        summarizer=cast(Any, FixtureSummarizer()),
        compaction=CompactionOptions(result_rewrite_profile="preserve"),
    )
    strategy = build_strategy(name, options)
    result_strategy = cast(Any, getattr(strategy, "tool_results", strategy))
    assert result_strategy.rewrite_result_text is not None
    fallback = getattr(result_strategy, "fallback", result_strategy)
    messages = result_history()
    before = deepcopy(messages[1].contents[0])
    await fallback(messages)
    content = messages[1].contents[0]
    assert len(content.result) < len(before.result)
    assert [p.to_dict() for p in content.items or [] if p.type != "text"] == [
        p.to_dict() for p in before.items or [] if p.type != "text"
    ]


@pytest.mark.anyio
async def test_selector_preserves_whole_mixed_turn_and_replay_does_not_repay_summary():
    client = FixtureSummarizer()
    controls = CompactionOptions(media_profile="select_pdf", max_summary_input_tokens=5000)
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=1000,
        max_output_tokens=0,
        user_trigger_fraction=0.01,
        user_min_band_share=0,
        summarizer=cast(Any, client),
        compaction=controls,
        keep_head_user_turns=1,
        keep_tail_user_turns=1,
    )
    strategy = build_strategy("user_summary_anchored", options)
    assert strategy is not None
    messages = media_history()
    messages[1].contents.append(
        Content.from_uri("https://example.invalid/mixed.pdf", media_type="application/pdf")
    )
    original_parts = deepcopy(messages[1].contents)
    await strategy(messages)
    assert messages[1].contents == original_parts
    assert "Explain this." not in "\n".join(m.text for request in client.requests for m in request)
    first_requests = len(client.requests)
    await strategy(messages)
    assert len(client.requests) == first_requests


class _UnsupportedOption(ValueError):
    code = "unsupported_parameter"
    param = "tool_choice"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["invalid_migration", "load", "save", "after_save"])
@pytest.mark.parametrize("stream", [False, True])
async def test_persistence_failure_retains_its_native_cause(monkeypatch, failure, stream):
    scenario = failure if failure in {"invalid_migration", "after_save"} else "fenced"
    controls = CompactionOptions(
        persistence_scenario=scenario, session_mode="restart", stream=stream
    )
    model = FixtureClient()
    experiment = SessionExperiment(lambda: capabilities._binding(model, controls), controls)
    store = experiment.persistence_store
    assert store is not None
    if failure != "invalid_migration":

        async def fail_storage(*args, **kwargs):
            raise ConnectionError("storage connection failed")

        monkeypatch.setattr(store, failure, fail_storage)
    with pytest.raises(ScenarioFailure) as caught:
        await experiment.run(AgentSession(session_id="setup"), "hello", {})
    assert isinstance(
        caught.value.__cause__, ValueError if scenario == "invalid_migration" else ConnectionError
    )
    assert len(model.requests) == (0 if failure in {"invalid_migration", "load"} else 1)
    assert store.requests[0]["committed"] is (failure == "after_save")
    assert not store.lock.locked()


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_persistence_setup_cancellation_remains_cancellation(monkeypatch, stream):
    controls = CompactionOptions(
        persistence_scenario="fenced", session_mode="restart", stream=stream
    )
    model = FixtureClient()
    experiment = SessionExperiment(lambda: capabilities._binding(model, controls), controls)
    store = experiment.persistence_store
    assert store is not None

    async def cancel_load(session_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "load", cancel_load)
    with pytest.raises(asyncio.CancelledError):
        await experiment.run(AgentSession(session_id="cancelled"), "hello", {})
    assert not model.requests
    assert store.requests[0]["error"] == "CancelledError"
    assert not store.requests[0]["committed"]
    assert not store.lock.locked()


class _RateLimitError(RuntimeError):
    status_code = 429


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario", ["save_failure", "after_save_failure", "stale", "invalid_migration"]
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "caller_error", [None, ConnectionError, TimeoutError, _UnsupportedOption, _RateLimitError]
)
async def test_live_runner_records_storage_outcome_without_retrying_model(
    scenario, stream, caller_error
):
    model = FixtureClient()
    controls = CompactionOptions(
        persistence_scenario=scenario, session_mode="restart", stream=stream
    )
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
        compaction=controls,
    )
    waits = []

    async def wait(delay):
        waits.append(delay)

    run = run_live(
        ProviderRuntime(model, "fixture", {}),
        strategy_name="tool_summary_anchored",
        options=options,
        scenario=build_live_scenario(
            salt="capability", tool_turns=1, filler_turns=0, filler_tokens=0
        ),
        probe_repeats=1,
        combined_repeats=1,
        sleep=wait,
    )
    if caller_error is None:
        outcome = await run
    else:
        try:
            raise caller_error("unrelated caller failure")
        except caller_error:
            outcome = await run
    assert outcome.error
    assert len(model.requests) == (0 if scenario == "invalid_migration" else 1)
    assert not waits
    assert not outcome.dropped_options
    assert outcome.rate_limit_retries == 0
    assert outcome.connection_retries == 0
    assert outcome.compaction_diagnostics["storage_evidence"] == "simulated_in_memory"
    assert len(outcome.compaction_diagnostics["persistence_requests"]) == 1
    request = outcome.compaction_diagnostics["persistence_requests"][0]
    assert request["committed"] == (scenario == "after_save_failure")


def test_profiles_roundtrip_in_live_cell_identity_and_older_settings_still_read():
    args = build_parser().parse_args(
        [
            "azure",
            "--session-mode",
            "restart",
            "--persistence-scenario",
            "branch",
            "--media-profile",
            "select_pdf",
            "--media-tokens",
            "800",
            "--result-rewrite-profile",
            "preserve",
        ]
    )
    options = _strategy_options(args, CharacterEstimatorTokenizer(), summarizer=None)
    settings = _strategy_settings(args, options, summarizer=None)
    assert settings.compaction == compaction_options(args)
    assert _settings_from_dict(json.loads(json.dumps(settings.to_dict()))) == settings
    old = settings.to_dict()
    for key in ("persistence_scenario", "media_profile", "media_tokens", "result_rewrite_profile"):
        old["compaction"].pop(key)
    restored = _settings_from_dict(old)
    assert restored.compaction is not None
    assert restored.compaction.persistence_scenario == "none"
    assert restored.compaction.media_profile == "none"
    assert restored != settings


@pytest.mark.anyio
async def test_live_branch_probes_restart_from_the_same_saved_seed():
    model = FixtureClient()
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
        compaction=CompactionOptions(
            persistence_scenario="branch", session_mode="restart", history_mode="replay"
        ),
    )
    outcome = await run_live(
        ProviderRuntime(model, "fixture", {}),
        strategy_name="tool_summary_anchored",
        options=options,
        scenario=build_live_scenario(salt="branch", tool_turns=1, filler_turns=0, filler_tokens=0),
        probe_repeats=2,
        combined_repeats=2,
    )
    assert len(outcome.probes) >= 2
    for index in range(0, len(outcome.probes), 2):
        assert outcome.probes[index].prompt_text == outcome.probes[index + 1].prompt_text
    requests = outcome.compaction_diagnostics["persistence_requests"]
    probe_sources = {request["source"] for request in requests[-len(outcome.probes) :]}
    assert len(probe_sources) == 1
    assert len({request["destination"] for request in requests}) == len(requests)


@pytest.mark.anyio
async def test_live_control_reports_storage_scenario_as_not_applicable():
    model = FixtureClient()
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
        compaction=CompactionOptions(persistence_scenario="save_failure", session_mode="restart"),
    )
    outcome = await run_live(
        ProviderRuntime(model, "fixture", {}),
        strategy_name="none",
        options=options,
        scenario=build_live_scenario(salt="control", tool_turns=1, filler_turns=0, filler_tokens=0),
        probe_repeats=1,
        combined_repeats=1,
    )
    assert len(model.requests) > 1
    assert outcome.compaction_diagnostics["storage_evidence"] == "not_applicable_no_recall_stack"
    assert "persistence_requests" not in outcome.compaction_diagnostics


@pytest.mark.parametrize(
    "controls",
    [
        {"persistence_scenario": "fenced"},
        {"persistence_scenario": "unknown"},
        {"media_tokens": 0},
        {"media_tokens": True},
        {"media_profile": "unknown"},
        {"result_rewrite_profile": "unknown"},
    ],
)
def test_invalid_controls_fail_before_model_work(controls):
    with pytest.raises(ValueError):
        CompactionOptions(**controls)


def test_cli_writes_distinct_records_and_refuses_to_overwrite(tmp_path):
    target = tmp_path / "capabilities.jsonl"
    assert capabilities.main(["--scenario", "all", "--stream", "--output", str(target)]) == 0
    records = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(records) == len(capabilities.PERSISTENCE_SCENARIOS) + len(
        capabilities.MEDIA_SCENARIOS
    ) + len(capabilities.RESULT_SCENARIOS)
    assert all(row["passed"] and row["record_type"] == "cachebench_capability" for row in records)
    previous = target.read_bytes()
    with pytest.raises(FileExistsError):
        capabilities.main(["--scenario", "fenced", "--output", str(target)])
    assert target.read_bytes() == previous


def test_cli_returns_nonzero_for_a_failed_contract(monkeypatch, capsys):
    real = capabilities.run_capability_scenario

    async def fail(*args, **kwargs):
        return replace(await real(*args, **kwargs), checks={"deliberate_failure": False})

    monkeypatch.setattr(capabilities, "run_capability_scenario", fail)
    assert capabilities.main(["--scenario", "fenced"]) == 1
    assert not json.loads(capsys.readouterr().out)["passed"]
