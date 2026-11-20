"""Matched full-coverage controls and candidate-judge measurement contracts."""

import asyncio
import json
from dataclasses import asdict

import pytest

from maf_cachebench import role_preservation as roles


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_cli_keeps_the_two_offline_controls_and_scores_each_split(capsys):
    assert roles.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["cases"]) == 18
    for split, sources in (("development", 2), ("held_out", 4)):
        totals = report["summaries"][split]
        missing = totals["missing_value"]
        pairs = totals["structured_pairs"]
        assert set(totals) == {"missing_value", "structured_pairs"}
        assert missing["candidates"] == pairs["candidates"] == 3 * sources
        assert (missing["false_accepts"], missing["false_refusals"]) == (sources, 0)
        assert (pairs["false_accepts"], pairs["false_refusals"]) == (0, sources)
        assert missing["requests"] == pairs["requests"] == 0
        assert missing["median_request_ms"] is None
        for case in (c for c in report["cases"] if c["split"] == split):
            expected = case["expected_answer"]
            changed = case["variant"] == "swap"
            assert case["policies"]["missing_value"]["status"] == "no_missing_values"
            assert (case["policies"]["missing_value"]["answer"] != expected) == changed
            assert case["policies"]["structured_pairs"]["answer"] == expected


def test_fixture_roles_are_matched_and_reference_fields_are_hidden():
    cases = list(roles._cases())
    by_source = {}
    for case in cases:
        req = case.request
        assert set(asdict(req)) == {"task", "sources", "candidate", "rubric"}
        assert len(req.candidate) < sum(map(len, req.sources))
        by_source.setdefault(case.name, []).append(case)
    for group in by_source.values():
        assert {c.variant for c in group} == {"faithful", "paraphrase", "swap"}
        assert len({c.request.sources for c in group}) == 1
        assert len({c.request.candidate for c in group}) == 3
        assert len({c.source_answer for c in group}) == 1
    cutover = by_source["cutover"]
    assert (
        cutover[1].request.candidate
        == "NODE-9642 is leaving service. The cutover goes to NODE-1784."
    )
    assert (
        cutover[2].request.candidate
        == "NODE-1784 is leaving service. The cutover goes to NODE-9642."
    )
    assert cutover[2].candidate_answer == "NODE-9642"
    assert by_source["settlement"][2].request.candidate == (
        "They claimed 7600 dollars and got authorization for 3400."
    )


@pytest.mark.anyio
async def test_candidate_judge_receives_every_candidate_and_scores_held_out_separately():
    cases = list(roles._cases())
    verdicts = {
        c.request: roles.CandidateVerdict("changed" if c.variant == "swap" else "faithful", 0.8)
        for c in cases
    }
    calls = []

    async def judge(request):
        calls.append(request)
        return verdicts[request]

    report = await roles.evaluate_roles(judge=judge, model_id="fixture")
    assert calls == [c.request for c in cases]
    assert report.model_id == "fixture"
    for split in ("development", "held_out"):
        summary = report.summaries[split]["candidate_judge"]
        assert summary.passed
        assert summary.requests == summary.candidates
        assert summary.requests_per_candidate == 1 and summary.median_request_ms is not None
        assert summary.abstentions == summary.fallbacks == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("low", "uncertain"),
        ("uncertain", "uncertain"),
        ("invalid", "unavailable"),
        ("error", "unavailable"),
        ("timeout", "timeout"),
        ("oversize", "oversize"),
    ],
)
async def test_fallback_records_coverage_and_cannot_pass_the_gate(monkeypatch, mode, status):
    calls = []

    async def judge(request):
        calls.append(request)
        if mode == "error":
            raise ValueError("unavailable")
        if mode == "timeout":
            await asyncio.Event().wait()
        if mode == "invalid":
            return roles.CandidateVerdict("changed", float("nan"))
        return roles.CandidateVerdict("uncertain" if mode == "uncertain" else "changed", 0.79)

    if mode == "oversize":
        monkeypatch.setattr(roles, "_MAX_BYTES", 1)
    if mode == "timeout":
        monkeypatch.setattr(roles, "_TIMEOUT", 0.005)
    report = await roles.evaluate_roles(judge=judge, model_id="fixture")
    assert len(calls) == (0 if mode == "oversize" else 18)
    for case in report.cases:
        row = case.policies["candidate_judge"]
        assert row.status == status and row.accepted
        assert row.answer == case.policies["missing_value"].answer
    summary = report.summaries["held_out"]["candidate_judge"]
    assert summary.fallbacks == summary.candidates and not summary.passed
    assert summary.abstentions == (summary.candidates if status == "uncertain" else 0)


@pytest.mark.anyio
async def test_cancellation_propagates_before_starting_another_case():
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def judge(request):
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            stopped.set()

    task = asyncio.create_task(roles.evaluate_roles(judge=judge, model_id="fixture"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
