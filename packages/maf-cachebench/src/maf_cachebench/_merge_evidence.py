"""Retain merge decisions and verify replacement presence in sent history."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from maf_compaction import record_merge_identity

if TYPE_CHECKING:
    from agent_framework import Message

_MAX_SAMPLES = 16
_MAX_REPLACEMENTS = 64


class MergeEvidence:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.active_lookups = 0
        self.active_checkpoint_lookups = 0
        self.checkpoint_turn = False
        self.checkpoint_lookup_called = False
        self._surveys: dict[str, deque[dict[str, int | str]]] = {}
        self._accepted: dict[str, deque[dict[str, int | str]]] = {}
        self._prepared: dict[str, None] = {}
        self._pending: dict[tuple[str, str], bool] = {}

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
            if (
                event == "answer_prepared"
                and self.active_checkpoint_lookups
                and (batch := data.get("batch"))
            ):
                if batch not in self._prepared and len(self._prepared) == _MAX_REPLACEMENTS:
                    self._prepared.pop(next(iter(self._prepared)))
                self._prepared[str(batch)] = None
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
            {key: val for key, val in data.items() if key not in {"replacement", "batch"}}
        )
        identity = data["replacement"]
        if (
            identity
            and data["tracked_values"]
            and data["kept_tracked_values"] == data["tracked_values"]
        ):
            replacement = (phase, str(identity))
            checkpoint = self.checkpoint_turn and (
                phase == "foreground" or (phase == "apply" and data.get("batch") in self._prepared)
            )
            if replacement not in self._pending and len(self._pending) == _MAX_REPLACEMENTS:
                self._pending.pop(next(iter(self._pending)))
            self._pending[replacement] = checkpoint or self._pending.get(replacement, False)

    def sent(self, messages: Sequence[Message]) -> None:
        """Confirm that a replacement preserving its values was sent to the model."""
        if not self._pending:
            return
        fingerprints = {record_merge_identity(message) for message in messages}
        for phase, fingerprint in tuple(self._pending):
            if fingerprint in fingerprints:
                checkpoint = self._pending.pop((phase, fingerprint))
                self.counts[f"{phase}.value_preserving_merge_sent"] += 1
                if checkpoint:
                    self.counts[f"{phase}.checkpoint_merge_sent"] += 1

    def snapshot(self) -> dict[str, object]:
        """Serialize only counts, bounds and reason codes."""
        return {
            "counts": dict(sorted(self.counts.items())),
            "surveys": {key: list(samples) for key, samples in sorted(self._surveys.items())},
            "accepted": {key: list(samples) for key, samples in sorted(self._accepted.items())},
        }
