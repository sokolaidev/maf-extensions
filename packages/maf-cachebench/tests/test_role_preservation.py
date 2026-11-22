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


_FIELDS = "Incoming target: NODE-1784\nOutgoing target: NODE-9642"
_SWAPPED = "Incoming target: NODE-9642\nOutgoing target: NODE-1784"


@pytest.mark.parametrize(
    ("source", "candidate", "role", "expected"),
    [
        (
            _FIELDS,
            " OUTGOING target: NODE-9642\nIncoming   target : NODE-1784 ",
            " INCOMING target ",
            "match",
        ),
        (_FIELDS, _SWAPPED, "Incoming target", "conflict"),
        (_FIELDS, _SWAPPED, None, "needs_interpretation"),
        (_FIELDS, _SWAPPED, "Unknown", "needs_interpretation"),
        (
            _FIELDS,
            _FIELDS + "\nincoming target: NODE-1784",
            "Incoming target",
            "needs_interpretation",
        ),
        (
            _FIELDS,
            _FIELDS + "\nIncoming target: NODE-9642",
            "Incoming target",
            "needs_interpretation",
        ),
        (
            _FIELDS + "\nIncoming target: NODE-9642",
            _SWAPPED,
            "Incoming target",
            "needs_interpretation",
        ),
        (_FIELDS, "Outgoing target: NODE-9642", "Incoming target", "needs_interpretation"),
        (_FIELDS, "Incoming target: NODE-1784", "Incoming target", "needs_interpretation"),
        (
            _FIELDS,
            _FIELDS + "\nUse the outgoing target.",
            "Incoming target",
            "needs_interpretation",
        ),
        (
            _FIELDS + "\nUse the outgoing target.",
            _FIELDS,
            "Incoming target",
            "needs_interpretation",
        ),
        (_FIELDS, _SWAPPED + "\nCorrection: reversed", "Incoming target", "needs_interpretation"),
        (_FIELDS, _FIELDS + " instead of NODE-1784", "Incoming target", "needs_interpretation"),
        (
            _FIELDS,
            _FIELDS.replace("NODE-1784", "node-1784"),
            "Incoming target",
            "needs_interpretation",
        ),
        ("A: 1000\nB: 2000\nC: 2000", "A: 2000\nB: 1000\nC: 1000", "A", "needs_interpretation"),
    ],
)
def test_complete_structured_comparison(source, candidate, role, expected):
    request = roles.CandidateRequest(
        "Which deployment target should be used next?", (source,), candidate
    )
    assert roles.compare_roles(request, task_role=role) == expected


@pytest.mark.anyio
async def test_routed_judge_sees_only_unresolved_candidates_and_preserves_the_task_answer():
    cases = list(roles._cases())
    prose_swaps = {"invoice", "cutover", "settlement"}
    routed = [
        c
        for c in cases
        if c.variant == "paraphrase" or (c.variant == "swap" and c.name in prose_swaps)
    ]
    verdicts = {
        c.request: roles.CandidateVerdict("changed" if c.variant == "swap" else "faithful", 0.9)
        for c in routed
    }
    calls = []

    async def judge(request):
        if request not in verdicts:
            pytest.fail("judge called for a conclusive structured decision")
        calls.append(request)
        return verdicts[request]

    report = await roles.evaluate_roles(judge=judge, model_id="fixture", route_structured=True)
    assert calls == [c.request for c in routed]
    for case, result in zip(cases, report.cases, strict=True):
        row = result.policies["routed_judge"]
        assert row.answer == case.source_answer
        assert row.accepted == (case.variant != "swap")
        if case in routed:
            assert result.structured_comparison == "needs_interpretation"
            assert row.requests == 1 and row.verdict == verdicts[case.request]
        else:
            assert result.structured_comparison == (
                "conflict" if case.variant == "swap" else "match"
            )
            assert row.requests == 0 and row.latency_ms == 0 and row.verdict is None
    assert report.summaries["held_out"]["routed_judge"].requests == 6
    assert report.summaries["held_out"]["routed_judge"].passed


@pytest.mark.anyio
async def test_routed_fallback_applies_only_after_an_unresolved_comparison():
    async def judge(request):
        return roles.CandidateVerdict("uncertain", 0.95)

    with pytest.raises(ValueError, match="requires a candidate judge"):
        await roles.evaluate_roles(route_structured=True)
    report = await roles.evaluate_roles(judge=judge, model_id="fixture", route_structured=True)
    held_out = report.summaries["held_out"]["routed_judge"]
    assert held_out.requests == held_out.fallbacks == 6
    assert held_out.false_accepts == 2
    assert not held_out.passed


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
