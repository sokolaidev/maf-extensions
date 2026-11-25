"""Bounded, source-bound answers for an opt-in record-merge tolerance."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from math import ceil
from typing import Any


@dataclass(frozen=True)
class LossyMerge:
    sources: str
    count: int
    text: str
    total: int
    kept: int
    #: The reasoning-prefix guard the batch was judged eligible under; None for an answer
    #: stored before it was recorded. Only False changes matching: it lifts the guard again.
    guarded: bool | None = None

    @property
    def size(self) -> int:
        return len(json.dumps(asdict(self)).encode("utf-8"))


class MergeCoverage:
    """Keep a tolerance and FIFO answer cache under one prompt and configuration."""

    def __init__(self, start: float, floor: float, step: float, cache_bytes: int) -> None:
        for name, value in (("share", start), ("floor", floor)):
            if isinstance(value, bool) or not 0 <= value <= 1:
                raise ValueError(f"merge_coverage_{name} must be in [0.0, 1.0].")
        if floor > start:
            raise ValueError("merge_coverage_floor must not exceed merge_coverage_share.")
        if isinstance(step, bool) or not 0 < step <= 1 or (floor < start and start - step == start):
            raise ValueError(
                "merge_coverage_step must be in (0.0, 1.0] and lower the starting bar."
            )
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("merge_answer_cache_bytes must be a non-negative integer.")
        self.start = start
        self.floor = floor
        self.step = step
        self.cache_bytes = cache_bytes
        self.current = start
        self.prompt = ""
        self.answers: list[LossyMerge] = []

    @property
    def adaptive(self) -> bool:
        return self.floor < self.start

    def bind(self, prompt: str) -> None:
        if prompt != self.prompt:
            self.prompt = prompt
            self.current = self.start
            self.answers = []

    def lower(self, steps: int = 1) -> bool:
        bar = max(self.floor, float(Fraction(str(self.current)) - steps * Fraction(str(self.step))))
        if bar >= self.current:
            return False
        self.current = bar
        return True

    def lower_to_next_answer(self) -> bool:
        """Skip empty steps without rounding a candidate's value ratio upward."""
        current, step = Fraction(str(self.current)), Fraction(str(self.step))
        steps = max(1, ceil((current - Fraction(str(self.floor))) / step))
        for answer in self.answers:
            ratio = Fraction(answer.kept, answer.total) if answer.total else Fraction(1)
            steps = min(steps, max(1, ceil((current - ratio) / step)))
        return self.lower(steps)

    def required(self, total: int) -> int:
        """Keep legacy fixed-policy rounding; decimal steps must not add a phantom value."""
        if not self.adaptive:
            return ceil(total * self.current)
        return ceil(Decimal(total) * Decimal(str(self.current)))

    def remember(self, answer: LossyMerge) -> None:
        if not self.adaptive or answer.size + 2 > self.cache_bytes:
            return
        self.answers = [item for item in self.answers if item.sources != answer.sources]
        self.answers.append(answer)
        size = sum(item.size + 2 for item in self.answers)
        while size > self.cache_bytes:
            size -= self.answers.pop(0).size + 2

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy": [self.start, self.floor, self.step, self.prompt],
            "current": self.current,
            "answers": [asdict(answer) for answer in self.answers],
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.current = self.start
        self.answers = []
        if state.get("policy") != [self.start, self.floor, self.step, self.prompt]:
            return
        current = state.get("current", self.start)
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            if self.floor <= current <= self.start:
                self.current = current
        for entry in state.get("answers", []):
            try:
                answer = LossyMerge(**entry)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(entry["sources"], str)
                and isinstance(entry["text"], str)
                and type(answer.count) is int
                and answer.count >= 2
                and type(answer.total) is int
                and type(answer.kept) is int
                and 0 <= answer.kept <= answer.total
                and (answer.guarded is None or type(answer.guarded) is bool)
            ):
                self.remember(answer)
