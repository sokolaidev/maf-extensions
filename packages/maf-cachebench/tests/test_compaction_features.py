"""Exercise current compaction through the benchmark's real middleware and lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

import maf_compaction
import pytest
from agent_framework import (
    AgentSession,
    BaseChatClient,
    CharacterEstimatorTokenizer,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)
from maf_compaction import (
    RECALL_TOOL_NAME,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    find_nested_strategy,
    make_recall_tool,
)

from maf_cachebench import (
    CompactionOptions,
    ProviderRuntime,
    StrategyOptions,
    UsageRecorder,
    build_live_agent,
    build_live_scenario,
    build_strategy,
    restore_state,
    run_live,
)
from maf_cachebench._configuration import compaction_options
from maf_cachebench._lifecycle import LiveBinding, SessionExperiment
from maf_cachebench._live import snapshot_decisions
from maf_cachebench._live_cli import (
    _strategy_options,
    _strategy_settings,
    build_parser,
    run_live_comparison,
)
from maf_cachebench._records import _settings_from_dict

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Model(FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], BaseChatClient[Any]):
    def __init__(self, *, refusal=False, truncated=False, failure=False, record=False):
        super().__init__()
        self.requests = []
        self.refusal = refusal
        self.truncated = truncated
        self.failure = failure
        self.record = record
        self.closed = 0

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.requests.append((deepcopy(list(messages)), dict(options)))
        index = len(self.requests)
        content = Content.from_text("answer")
        finish = "stop"
        if index == 1 and self.refusal:
            content = Content.from_text(
                "refused", additional_properties={"core": {"model_output_kind": "refusal"}}
            )
        elif index == 1 and self.truncated:
            content = Content.from_function_call(
                call_id="cut", name=RECALL_TOOL_NAME, arguments='{"values": "unfinished'
            )
            finish = "length"
        elif index == 1 and self.record:
            content = Content.from_function_call(
                call_id="record", name=RECALL_TOOL_NAME, arguments='{"values":"CODE-123456"}'
            )
            finish = "tool_calls"
        response = ChatResponse(
            messages=Message("assistant", [content]),
            finish_reason=finish,
            usage_details={
                "input_token_count": index * 100,
                "output_token_count": index * 10,
                "cache_read_input_token_count": index * 50,
            },
        )
        if stream:

            async def updates():
                try:
                    yield ChatResponseUpdate(role="assistant", contents=[content])
                    if self.failure:
                        raise asyncio.CancelledError()
                finally:
                    self.closed += 1

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer():
            if self.failure:
                raise RuntimeError("model unavailable")
            return response

        return answer()


def binding_factory(
    model,
    recorder,
    *,
    kind="plain",
    force=False,
    options=None,
    built=None,
    strategy_name="tool_summary_anchored",
    summarizer=None,
    budget=100_000,
):
    tokenizer = CharacterEstimatorTokenizer()
    controls = options or CompactionOptions()

    def build():
        strategy = build_strategy(
            strategy_name,
            StrategyOptions(
                tokenizer=tokenizer,
                max_context_window_tokens=budget,
                max_output_tokens=1000,
                compaction=controls,
                summarizer=summarizer,
                keep_head_user_turns=0,
                user_min_band_share=0,
            ),
        )
        gate = RecallGate()
        recording = find_nested_strategy(
            strategy, ToolResultAnchoredSummarizationCompactionStrategy
        )
        assert recording is not None
        recall = ToolResultRecallMiddleware(
            max_input_tokens=budget - 1000,
            tokenizer=tokenizer,
            arm=gate.arm,
            disarm=gate.disarm,
            reforce=recording.take_reforce,
            preserve_prompt_cache=controls.preserve_prompt_cache,
            record_instruction=controls.record_instruction,
        )
        if force:
            state = recall.session_state()
            state["force_next"] = True
            recall.restore_session_state(state)
        runtime = ProviderRuntime(model, "stub", {})
        agent = build_live_agent(
            runtime,
            kind=kind,
            strategy=strategy,
            tokenizer=tokenizer,
            tools=[make_recall_tool(gate)],
            recorder=recorder,
            extra_middleware=[recall],
            max_context_window_tokens=budget,
            max_output_tokens=1000,
        )
        binding = LiveBinding(agent, strategy, recall, gate)
        if built is not None:
            built.append(binding)
        return binding

    return build


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["plain", "harness"])
@pytest.mark.parametrize("failure", ["refusal", "truncated"])
async def test_every_recall_attempt_is_metered_and_gate_is_disarmed(stream, kind, failure):
    model = Model(**{failure: True})
    recorder = UsageRecorder()
    binding = binding_factory(model, recorder, kind=kind, force=True)()
    response = binding.agent.run("question", session=AgentSession(), stream=stream)
    if stream:
        assert isinstance(response, ResponseStream)
        try:
            response = await response.get_final_response()
        finally:
            # The final response has already closed the provider stream.
            assert model.closed == 2
    else:
        response = await response
    assert response.text == "answer"
    assert len(model.requests) == len(recorder.calls) == 2
    assert sum(call.input_tokens for call in recorder.calls) == 300
    assert sum(call.output_tokens for call in recorder.calls) == 30
    assert sum(call.cached_tokens for call in recorder.calls) == 150
    assert binding.gate is not None
    assert binding.gate.session_state() == {"armed": False}


def test_probe_restore_rewinds_backoff_gate_and_record_identity():
    binding = binding_factory(Model(), UsageRecorder())()
    assert binding.recall is not None and binding.gate is not None
    snapshot = snapshot_decisions(binding.strategy, binding.recall)
    changed = binding.recall.session_state()
    changed.update(
        pin_disabled=True,
        pin_disabled_reason="value_free",
        value_free_streak=2,
        value_records=["later-probe"],
        last_record="later-record",
    )
    binding.recall.restore_session_state(changed)
    binding.gate.arm()
    restore_state(AgentSession(), {}, binding.recall, binding.strategy, snapshot)
    assert binding.recall.session_state() == snapshot.recall
    assert binding.gate.session_state() == snapshot.gate


@pytest.mark.parametrize("mode", ["memory", "roundtrip", "restart"])
@pytest.mark.parametrize("history", ["incremental", "replay"])
@pytest.mark.parametrize("stream", [False, True])
async def test_lifecycle_restores_history_and_counts_both_requests(mode, history, stream):
    if mode != "memory" and not hasattr(maf_compaction, "SessionPersistence"):
        pytest.skip("Installed compaction predates SessionPersistence")
    model = Model()
    recorder = UsageRecorder()
    built = []
    options = CompactionOptions(session_mode=mode, history_mode=history, stream=stream)
    experiment = SessionExperiment(binding_factory(model, recorder, built=built), options)
    session = AgentSession()
    for text in ("Remember CODE-123456.", "Continue.", "Continue again."):
        response, session = await experiment.run(session, text, {})
        assert response.text == "answer"
    assert len(recorder.calls) == len(model.requests) == 3
    assert any("CODE-123456" in message.text for message in model.requests[-1][0])
    assert sum("CODE-123456" in message.text for message in model.requests[-1][0]) == 1
    assert sum(message.text == "answer" for message in model.requests[-1][0]) == 2
    if history == "replay":
        ids = [
            message["message_id"]
            for message in session.to_dict()["state"]["cachebench_original_history"]
        ]
        assert len(ids) == len(set(ids))
    if mode == "restart":
        assert len(built) == 4
    if mode != "memory":
        assert session.state[experiment.registry.state_key]["version"] == 5


@pytest.mark.parametrize("stream", [False, True])
async def test_completed_recall_record_survives_restart_and_original_history_replay(stream):
    if not hasattr(maf_compaction, "SessionPersistence"):
        pytest.skip("Installed compaction predates SessionPersistence")
    model = Model(record=True)
    recorder = UsageRecorder()
    experiment = SessionExperiment(
        binding_factory(model, recorder, force=True),
        CompactionOptions(session_mode="restart", history_mode="replay", stream=stream),
    )
    _, session = await experiment.run(AgentSession(), "Remember CODE-123456.", {})
    assert experiment.binding.recall is not None
    first = experiment.binding.recall.session_state()
    assert first["last_record"]
    assert not first["force_next"]
    _, session = await experiment.run(session, "Continue.", {})
    assert experiment.binding.recall is not None
    assert experiment.binding.recall.session_state()["last_record"] == first["last_record"]
    assert experiment.totals["records_forced"] == 1
    assert len(model.requests) == len(recorder.calls) == 3
    assert model.requests[-1][1].get("tool_choice") in (None, "auto")
    assert any("CODE-123456" in message.text for message in model.requests[-1][0])


@pytest.mark.parametrize("stream", [False, True])
async def test_replayed_history_is_in_the_committed_snapshot(monkeypatch, stream):
    if not hasattr(maf_compaction, "SessionPersistence"):
        pytest.skip("Installed compaction predates SessionPersistence")
    persistence_type = maf_compaction.SessionPersistence
    snapshots = []

    def persistence(*args, **kwargs):
        save = kwargs["save"]

        async def capture(session_id, data):
            snapshots.append(deepcopy(data))
            await save(session_id, data)

        return persistence_type(*args, **{**kwargs, "save": capture})

    monkeypatch.setattr(maf_compaction, "SessionPersistence", persistence)
    experiment = SessionExperiment(
        binding_factory(Model(), UsageRecorder()),
        CompactionOptions(session_mode="restart", history_mode="replay", stream=stream),
    )
    session = AgentSession()
    for text in ("Remember CODE-123456.", "Continue."):
        _, session = await experiment.run(session, text, {})
    histories = [snapshot["state"]["cachebench_original_history"] for snapshot in snapshots]
    assert [len(history) for history in histories] == [2, 4]
    assert histories[-1] == session.to_dict()["state"]["cachebench_original_history"]


@pytest.mark.parametrize("stream", [False, True])
async def test_failed_persistent_request_does_not_commit_history(stream):
    if not hasattr(maf_compaction, "SessionPersistence"):
        pytest.skip("Installed compaction predates SessionPersistence")
    model = Model(failure=True)
    experiment = SessionExperiment(
        binding_factory(model, UsageRecorder()),
        CompactionOptions(session_mode="restart", stream=stream),
    )
    session = AgentSession()
    with pytest.raises(asyncio.CancelledError if stream else RuntimeError):
        await experiment.run(session, "must not be saved", {})
    assert "must not be saved" not in str(session.to_dict())
    assert not experiment.lock.locked()
    if stream:
        assert model.closed == 1


@pytest.mark.parametrize("mode", ["memory", "roundtrip", "restart"])
@pytest.mark.parametrize("strategy", ["none", "tool_summary_anchored"])
async def test_run_live_exposes_modes_and_reproduces_costs(mode, strategy):
    if (
        mode != "memory"
        and strategy != "none"
        and not hasattr(maf_compaction, "SessionPersistence")
    ):
        pytest.skip("Installed compaction predates SessionPersistence")
    model = Model()
    options = StrategyOptions(
        tokenizer=CharacterEstimatorTokenizer(),
        max_context_window_tokens=100_000,
        max_output_tokens=1000,
        compaction=CompactionOptions(session_mode=mode, history_mode="replay", stream=True),
    )
    scenario = build_live_scenario(salt="modes", filler_turns=1, filler_tokens=20, tool_turns=1)
    outcome = await run_live(
        ProviderRuntime(model, "stub", {}),
        strategy_name=strategy,
        options=options,
        scenario=scenario,
        force_tool_calls=False,
        probe_repeats=2,
        combined_repeats=2,
    )
    assert outcome.error is None
    assert len(outcome.calls) == len(model.requests)
    assert outcome.input_tokens == sum((i + 1) * 100 for i in range(len(model.requests)))


def test_cli_controls_reach_settings_and_distinguish_cells():
    args = build_parser().parse_args(
        [
            "azure",
            "--preserve-prompt-cache",
            "--record-instruction",
            "Retain identifiers.",
            "--no-supports-tool-choice",
            "--forced-call-timeout-seconds",
            "4",
            "--summary-input-policy",
            "chunk",
            "--max-summary-input-tokens",
            "500",
            "--summary-options-json",
            '{"max_tokens": 100}',
            "--retry-without-summary-options",
            "--session-mode",
            "restart",
            "--history-mode",
            "replay",
            "--stream",
        ]
    )
    controls = compaction_options(args)
    options = _strategy_options(args, CharacterEstimatorTokenizer(), summarizer=None)
    settings = _strategy_settings(args, options, summarizer=None)
    assert settings.compaction == options.compaction == controls
    assert _settings_from_dict(settings.to_dict()) == settings
    assert settings != replace(settings, compaction=replace(controls, preserve_prompt_cache=False))
    assert settings.package_versions
    older = settings.to_dict()
    del older["compaction"]
    del older["package_versions"]
    assert _settings_from_dict(older).compaction is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"forced_call_timeout_seconds": 0},
        {"summary_options_json": "[]"},
        {"summary_options_json": '{"max_tokens": NaN}'},
        {"estimate_margin": float("nan")},
        {"session_mode": "unknown"},
        {"summary_backoff_seconds": -1},
    ],
)
def test_bad_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CompactionOptions(**kwargs)


@pytest.mark.parametrize("dry_run", [False, True])
async def test_unsupported_persistence_is_rejected_before_any_comparison_row(monkeypatch, dry_run):
    monkeypatch.delattr(maf_compaction, "SessionPersistence", raising=False)
    args = build_parser().parse_args(
        ["azure", "--strategies", "none,tool_summary_anchored", "--session-mode", "restart"]
        + (["--dry-run"] if dry_run else [])
    )
    with pytest.raises(SystemExit, match="SessionPersistence"):
        await run_live_comparison(args)


class Summarizer:
    def __init__(self):
        self.requests = []

    async def get_response(self, messages, *, options, **kwargs):
        self.requests.append((deepcopy(messages), dict(options)))
        return ChatResponse(messages=Message("assistant", ["Remember the user's requirements."]))


@pytest.mark.parametrize("stream", [False, True])
async def test_composed_summaries_replay_after_restart_without_repaying_same_summary(stream):
    if not hasattr(maf_compaction, "SessionPersistence"):
        pytest.skip("Installed compaction predates SessionPersistence")
    summarizer = Summarizer()
    model = Model()
    controls = CompactionOptions(
        session_mode="restart",
        history_mode="replay",
        stream=stream,
        summary_options_json='{"max_tokens": 111}',
    )
    experiment = SessionExperiment(
        binding_factory(
            model,
            UsageRecorder(),
            options=controls,
            strategy_name="tool_and_user_summary_anchored",
            summarizer=summarizer,
            budget=5000,
        ),
        controls,
    )
    session = AgentSession()
    for index in range(7):
        _, session = await experiment.run(session, f"Requirement {index}: " + "x " * 1800, {})
    assert summarizer.requests
    assert all(options == {"max_tokens": 111} for _, options in summarizer.requests)
    _, session = await experiment.run(session, "Continue.", {})
    count = len(summarizer.requests)
    _, session = await experiment.run(session, "Continue again.", {})
    assert len(summarizer.requests) == count
    assert experiment.totals["user_summaries_replayed"] > 0
    assert experiment.totals["user_compactions"] > 0
    assert "Remember the user's requirements." in " ".join(
        message.text for message in model.requests[-1][0]
    )


def test_summary_and_overflow_controls_reach_both_strategy_halves():
    controls = CompactionOptions(
        max_summary_input_tokens=1234,
        summary_timeout_seconds=3,
        summary_input_policy="chunk",
        chunk_cache_size=0,
        summary_options_json='{"max_tokens": 111}',
        retry_without_summary_options=True,
        max_consecutive_summary_failures=2,
        summary_backoff_seconds=5,
        overflow_policy="raise",
        estimate_margin=0.1,
        user_summary_prompt="Summarize requirements.",
        user_fold_prompt="Fold requirements.",
        record_merge_prompt="Merge records.",
    )
    strategy = build_strategy(
        "tool_and_user_summary_anchored",
        StrategyOptions(
            tokenizer=CharacterEstimatorTokenizer(),
            max_context_window_tokens=100_000,
            max_output_tokens=1000,
            summarizer=cast(Any, Summarizer()),
            compaction=controls,
        ),
    )
    assert isinstance(strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy)
    assert strategy.overflow_policy == "raise"
    assert strategy.estimate_margin == 0.1
    assert strategy.user_turns.max_summary_input_tokens == 1234
    assert strategy.user_turns.summary_input_policy == "chunk"
    assert strategy.user_turns.chunk_cache_size == 0
    assert strategy.user_turns.retry_without_summary_options
    assert strategy.user_turns.summary_options == {"max_tokens": 111}
    assert strategy.user_turns.summary_timeout_seconds == 3
    assert strategy.user_turns.max_consecutive_summary_failures == 2
    assert strategy.user_turns.summary_backoff_seconds == 5


def test_missing_persistence_api_is_refused_before_contacting_model(monkeypatch):
    monkeypatch.delattr(maf_compaction, "SessionPersistence", raising=False)
    model = Model()
    with pytest.raises(ValueError, match="SessionPersistence"):
        SessionExperiment(
            binding_factory(model, UsageRecorder()), CompactionOptions(session_mode="restart")
        )
    assert model.requests == []
