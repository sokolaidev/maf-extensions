"""Check matched merge-loss policies and the host-owned judge boundary."""

import asyncio
import json

import pytest
from agent_framework import CharacterEstimatorTokenizer
from maf_compaction import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

from maf_cachebench import StrategyOptions, build_strategy
from maf_cachebench._capability_fixtures import FixtureClient
from maf_cachebench.merge_loss import (
    _CASES,
    _SOURCES,
    JudgeAnswer,
    Verdict,
    _fixture_judge,
    evaluate,
    main,
)


def test_command_reports_match_the_compaction_count_policy(capsys):
    assert main([]) == 0
    baseline = json.loads(capsys.readouterr().out)
    assert main(["--fixture-judge"]) == 0
    comparison = json.loads(capsys.readouterr().out)
    strategy = build_strategy(
        "tool_and_user_summary_anchored",
        StrategyOptions(
            tokenizer=CharacterEstimatorTokenizer(),
            max_context_window_tokens=3000,
            max_output_tokens=300,
            summarizer=FixtureClient(),
        ),
    )
    assert isinstance(strategy, ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy)
    for case, off, on in zip(_CASES, baseline["cases"], comparison["cases"], strict=True):
        total, kept, required = strategy._value_coverage(list(_SOURCES), case.candidate)
        assert (off["source_values"], off["candidate_kept"]) == (total, kept)
        assert off["policies"]["count"]["accepted"] is (kept >= required)
        assert off["judge"]["requests"] == 0
        assert "judge" not in off["policies"]
        for key in ("count", "context"):
            assert off["policies"][key] == on["policies"][key]

    rows = {row["case"]: row for row in comparison["cases"]}
    unique = rows["unique-target"]
    assert unique["policies"]["count"]["false_accept"]
    assert not unique["policies"]["judge"]["accepted"]
    assert unique["policies"]["judge"]["next_task_correct"]
    repeated = rows["audits-repeated"]
    assert (repeated["candidate_kept"], repeated["context_kept"]) == (3, 5)
    assert repeated["policies"]["count"]["false_refusal"]
    assert repeated["policies"]["context"]["facts_preserved"] == 5
    assert repeated["policies"]["context"]["accepted"]
    omitted = rows["audits-omitted"]
    assert omitted["context_kept"] == 3
    assert omitted["policies"]["context"]["false_refusal"]
    assert omitted["policies"]["judge"]["accepted"]
    assert omitted["policies"]["judge"]["facts_preserved"] == 3
    wrong_role = rows["target-in-wrong-role"]
    assert (wrong_role["candidate_kept"], wrong_role["context_kept"]) == (3, 4)
    assert not wrong_role["policies"]["count"]["accepted"]
    assert wrong_role["policies"]["context"]["false_accept"]
    assert not wrong_role["policies"]["judge"]["accepted"]
    swapped = rows["roles-swapped"]
    assert swapped["judge"]["requests"] == 0
    assert swapped["policies"]["judge"]["next_target"] == "SRV-8000"
    assert swapped["policies"]["judge"]["false_accept"]
    assert rows["below-floor"]["judge"]["status"] == "below_floor"
    assert comparison["totals"]["judge"]["false_accepts"] == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_one_bounded_request_per_eligible_case_and_no_reference_answer():
    requests = []

    async def client(request):
        requests.append(request)
        return await _fixture_judge(request)

    report = await evaluate(
        judge=client, model_id="my-deployment", rubric="Assess task relevance", rubric_version="v7"
    )
    assert report.model_id == "my-deployment"
    assert len(requests) == 4
    request = next(r for r in requests if len(r.retained) == 3)
    assert request.rubric_version == "v7" and request.rubric == "Assess task relevance"
    assert all(item.source_lines for item in request.missing)
    assert {item.value: item.later_messages for item in request.missing} == {
        "LOG-1002": 2,
        "LOG-1003": 1,
    }
    assert not hasattr(request, "expected_target")
    requests.clear()
    limited = await evaluate(judge=client, model_id="fixture", max_request_bytes=1)
    assert not requests
    assert limited.cases[0].judge.status == "oversize"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("empty", "unavailable"),
        ("extra", "unavailable"),
        ("nonfinite", "unavailable"),
        ("invalid_label", "unavailable"),
        ("exception", "unavailable"),
        ("timeout", "timeout"),
        ("uncertain", "uncertain"),
        ("low_confidence", "uncertain"),
    ],
)
async def test_fallback_reproduces_the_count_decision(kind, status):
    async def client(request):
        if kind == "exception":
            raise RuntimeError("provider unavailable")
        if kind == "timeout":
            await asyncio.Event().wait()
        values = {item.value: Verdict("minor", 1.0) for item in request.missing}
        first = request.missing[0].value
        if kind == "empty":
            values.clear()
        elif kind == "extra":
            values["OTHER"] = Verdict("material", 1.0)
        elif kind == "nonfinite":
            values[first] = Verdict("minor", float("nan"))
        elif kind == "invalid_label":
            values[first] = Verdict("allow", 1.0)  # pyright: ignore[reportArgumentType]
        elif kind == "uncertain":
            values[first] = Verdict("uncertain", 0.95)
        elif kind == "low_confidence":
            values[first] = Verdict("minor", 0.5)
        return JudgeAnswer(values)

    report = await evaluate(judge=client, model_id="test", timeout=0.01)
    observed = [row for row in report.cases if row.judge.requests]
    assert len(observed) == 4
    for row in observed:
        assert row.judge.status == status
        assert row.judge.cost_usd is None
        assert row.policies["count"] == row.policies["judge"]


@pytest.mark.anyio
async def test_report_keeps_known_cost_when_the_verdict_is_incomplete():
    async def client(request):
        return JudgeAnswer({}, cost_usd=0.001)

    report = await evaluate(judge=client, model_id="test")
    assert report.cases[0].judge.status == "unavailable"
    assert report.cases[0].judge.cost_usd == 0.001


@pytest.mark.anyio
async def test_caller_cancellation_reaches_the_judge():
    started = asyncio.Event()
    finished = asyncio.Event()

    async def client(request):
        try:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("judge unexpectedly resumed")
        finally:
            finished.set()

    task = asyncio.create_task(evaluate(judge=client, model_id="test"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
