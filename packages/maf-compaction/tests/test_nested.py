"""Tests for finding one strategy inside a composition of them."""

from __future__ import annotations

from typing import Any

from maf_compaction import find_nested_strategy


class _Target:
    def __init__(self, *strategies: Any) -> None:
        self.strategies = list(strategies)


class _Composition:
    def __init__(self, *strategies: Any) -> None:
        self.strategies = list(strategies)


def test_the_match_that_runs_first_is_returned() -> None:
    """A nested composition's parts run before its later siblings, so they are searched first."""
    first, second = _Target(), _Target()

    found = find_nested_strategy(_Composition(_Composition(first), second), _Target)

    assert found is first


def test_the_strategy_itself_is_returned_before_its_parts() -> None:
    outer = _Target(_Target())

    assert find_nested_strategy(outer, _Target) is outer


def test_no_match_and_a_cycle_both_return_none() -> None:
    loop = _Composition()
    loop.strategies.append(loop)

    assert find_nested_strategy(loop, _Target) is None
    assert find_nested_strategy(None, _Target) is None
