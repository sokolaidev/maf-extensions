"""Retain merge decisions and verify replacement presence in sent history."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_framework import Message

_MAX_SAMPLES = 16
_MAX_REPLACEMENTS = 64


class MergeEvidence:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.active_lookups = 0
        self._surveys: dict[str, deque[dict[str, int | str]]] = {}
        self._accepted: dict[str, deque[dict[str, int | str]]] = {}
        self._pending: dict[tuple[str, str], None] = {}

    def observe(self, phase: str, event: str, data: Mapping[str, int | str]) -> None:
        """Count all events and retain a bounded sample of each pressure outcome."""
        reason = data.get("reason")
        key = f"{phase}.{event}" if reason is None else f"{phase}.{event}.{reason}"
        self.counts[key] += 1
        if (
            phase == "background"
            and self.active_lookups
            and event in {"request_prepared", "answer_prepared"}
        ):
            self.counts[f"{key}_during_lookup"] += 1
        if event == "survey":
            if not data["pressure"]:
                status = "no_pressure"
            elif int(data["eligible"]) < 2:
                status = "too_few_eligible"
            else:
                status = "search"
            group = f"{phase}.{status}"
            self.counts[group] += 1
            if group not in self._surveys:
                self._surveys[group] = deque(maxlen=_MAX_SAMPLES)
            self._surveys[group].append(dict(data))
        if event != "answer_accepted" or phase == "background":
            return
        if phase not in self._accepted:
            self._accepted[phase] = deque(maxlen=_MAX_SAMPLES)
        self._accepted[phase].append(
            {key: val for key, val in data.items() if key != "replacement"}
        )
        if data["tracked_values"] and data["kept_tracked_values"] == data["tracked_values"]:
            replacement = (phase, str(data["replacement"]))
            if len(self._pending) == _MAX_REPLACEMENTS:
                self._pending.pop(next(iter(self._pending)))
            self._pending[replacement] = None

    def sent(self, messages: Sequence[Message]) -> None:
        """Confirm that a replacement preserving its values was sent to the model."""
        if not self._pending:
            return
        fingerprints = {
            sha256(message.text.encode()).hexdigest()
            for message in messages
            if message.role == "assistant" and message.text
        }
        for phase, fingerprint in tuple(self._pending):
            if fingerprint in fingerprints:
                del self._pending[phase, fingerprint]
                self.counts[f"{phase}.value_preserving_merge_sent"] += 1

    def snapshot(self) -> dict[str, object]:
        """Serialize only counts, bounds and reason codes."""
        return {
            "counts": dict(sorted(self.counts.items())),
            "surveys": {key: list(samples) for key, samples in sorted(self._surveys.items())},
            "accepted": {key: list(samples) for key, samples in sorted(self._accepted.items())},
        }
