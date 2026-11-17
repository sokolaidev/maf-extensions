"""Cachebench must expose budget refusals and independently measure retained values."""

import importlib
from pathlib import Path

import pytest


@pytest.mark.anyio
async def test_controlled_coverage_tradeoff(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    comparison = importlib.import_module("compare_merge_coverage")
    strict = await comparison.compare(0.8)
    relaxed = await comparison.compare(0.75)
    assert strict.over_budget == strict.turns == 6
    assert strict.final_missing_values == strict.omitted_values == 0
    assert strict.summary_requests == 7
    assert relaxed.over_budget < strict.over_budget
    assert relaxed.projected_tokens < strict.projected_tokens
    assert relaxed.final_missing_values == relaxed.omitted_values == 20
    assert relaxed.accepted == {0.75: 2}
    assert "no provider requests" in comparison.report([strict, relaxed])
