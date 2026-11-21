"""Measure role preservation on the actual candidates of one compaction pass."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Literal

from ._record_diagnostics import RecordReduction


@dataclass(frozen=True)
class RecordRoleCandidate:
    """The source bodies and replacement for one question in a judge batch."""

    sources: tuple[str, ...]
    candidate: str


@dataclass(frozen=True)
class RecordRoleBatch:
    """A shared host task and independent questions keyed by pass-local IDs."""

    task: str
    task_role: str | None
    candidates: Mapping[int, RecordRoleCandidate]


@dataclass(frozen=True)
class RecordRoleVerdict:
    """A host adapter's selected answer and its probability."""

    label: Literal["faithful", "changed", "uncertain"]
    probability: float


def _token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class RecordRoleResponse:
    """Verdicts and batch token counts. Missing or invalid counts become ``None``."""

    verdicts: Mapping[int, RecordRoleVerdict]
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_tokens", _token_count(self.input_tokens))
        object.__setattr__(self, "output_tokens", _token_count(self.output_tokens))


type RecordRoleJudge = Callable[
    [RecordRoleBatch], Awaitable[Mapping[int, RecordRoleVerdict] | RecordRoleResponse]
]
type _Decision = Literal["accept", "refuse", "undecided"]


@dataclass(frozen=True)
class RecordRoleObservation:
    """A candidate's actual strategy outcome and the independently measured role decision."""

    operation: RecordReduction
    strategy_outcome: str
    route: Literal["guard", "structured", "judge"]
    decision: _Decision
    reason: str
    probability: float | None = None


@dataclass(frozen=True)
class RecordRoleRequest:
    """The questions, elapsed time and available usage of one attempted decision request."""

    candidate_ids: tuple[int, ...]
    seconds: float
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class RecordRoleReport:
    """Results from one pass, including actual adapter requests and elapsed judge time."""

    changed: bool
    observations: tuple[RecordRoleObservation, ...]
    request_details: tuple[RecordRoleRequest, ...]

    @property
    def batch_sizes(self) -> tuple[int, ...]:
        """Number of unique questions asked in each decision request."""
        return tuple(len(request.candidate_ids) for request in self.request_details)

    @property
    def judge_seconds(self) -> float:
        """Total elapsed time spent waiting for decision requests."""
        return sum(request.seconds for request in self.request_details)

    @property
    def requests(self) -> int:
        """Number of decision adapter calls issued by this pass."""
        return len(self.request_details)


def _fields(records: tuple[str, ...]) -> dict[str, str] | None:
    fields: dict[str, str] = {}
    for record in records:
        for line in record.splitlines():
            if not line.strip():
                continue
            label, _, value = line.partition(":")
            label = " ".join(label.split()).casefold()
            value = value.strip()
            if (
                not label
                or label in fields
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value)
            ):
                return None
            fields[label] = value
    return fields


def _compare(item: RecordRoleCandidate, role: str) -> _Decision:
    sources = _fields(item.sources)
    if not sources or role not in sources:
        return "undecided"
    candidate = _fields((item.candidate,))
    if candidate is None or candidate.keys() != sources.keys():
        return "undecided"
    if candidate == sources:
        return "accept"
    if candidate[role] != sources[role] and sorted(candidate.values()) == sorted(sources.values()):
        return "refuse"
    return "undecided"


class RecordRolePass:
    """Collect unique unresolved candidates within the host's per-pass request budget."""

    def __init__(
        self,
        task: str,
        task_role: str | None,
        max_requests: int,
        batch_size: int,
        max_text_bytes: int,
    ) -> None:
        if type(max_requests) is not int or max_requests < 0:
            raise ValueError("max_requests must be a non-negative integer")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if type(max_text_bytes) is not int or max_text_bytes < 1:
            raise ValueError("max_text_bytes must be a positive integer")
        self.task = task
        self.task_role = task_role
        self.role = " ".join((task_role or "").split()).casefold()
        self.max_requests = max_requests
        self.batch_size = batch_size
        self.max_text_bytes = max_text_bytes
        self.context_bytes = len(task.encode()) + len((task_role or "").encode())
        self.used_bytes = 0
        self.batches: list[dict[int, RecordRoleCandidate]] = []
        self.seen: dict[RecordRoleCandidate, int] = {}
        self.positions: dict[int, list[int]] = {}
        self.rows: list[RecordRoleObservation] = []

    def observe(
        self, operation: RecordReduction, outcome: str, bodies: list[str], text: str
    ) -> None:
        if outcome != "accepted":
            self.rows.append(RecordRoleObservation(operation, outcome, "guard", "refuse", outcome))
            return
        item = RecordRoleCandidate(tuple(bodies), text)
        decision = _compare(item, self.role) if self.role else "undecided"
        if decision != "undecided":
            reason = "match" if decision == "accept" else "conflict"
            self.rows.append(
                RecordRoleObservation(operation, outcome, "structured", decision, reason)
            )
            return
        position = len(self.rows)
        reason = self._enqueue(item, position) if self.task.strip() else "missing_task"
        self.rows.append(RecordRoleObservation(operation, outcome, "judge", "undecided", reason))

    def _enqueue(self, item: RecordRoleCandidate, position: int) -> str:
        known = self.seen.get(item)
        if known is not None:
            self.positions[known].append(position)
            return "pending"
        if not self.max_requests or (
            len(self.batches) == self.max_requests and len(self.batches[-1]) == self.batch_size
        ):
            return "request_budget"
        size = len(item.candidate.encode()) + sum(len(body.encode()) for body in item.sources)
        if self.context_bytes + size > self.max_text_bytes:
            return "oversize"
        if (
            not self.batches
            or len(self.batches[-1]) == self.batch_size
            or self.used_bytes + size > self.max_text_bytes
        ):
            if len(self.batches) == self.max_requests:
                return "request_budget"
            self.batches.append({})
            self.used_bytes = self.context_bytes
        self.batches[-1][position] = item
        self.seen[item] = position
        self.positions[position] = [position]
        self.used_bytes += size
        return "pending"

    async def finish(self, changed: bool, judge: RecordRoleJudge) -> RecordRoleReport:
        requests: list[RecordRoleRequest] = []
        for items in self.batches:
            start = perf_counter()
            answers: Mapping[int, RecordRoleVerdict] = {}
            input_tokens = output_tokens = None
            reason = "malformed"
            try:
                async with asyncio.timeout(5):
                    response = await judge(RecordRoleBatch(self.task, self.task_role, items.copy()))
                if isinstance(response, RecordRoleResponse):
                    input_tokens, output_tokens = response.input_tokens, response.output_tokens
                    replies = response.verdicts
                else:
                    replies = response
                if isinstance(replies, Mapping) and all(  # pyright: ignore[reportUnnecessaryIsInstance]
                    type(key) is int and key in items for key in replies
                ):
                    answers = dict(replies)
            except TimeoutError:
                reason = "timeout"
            except Exception:
                reason = "unavailable"
            requests.append(
                RecordRoleRequest(tuple(items), perf_counter() - start, input_tokens, output_tokens)
            )
            for key in items:
                verdict = answers.get(key)
                decision: _Decision = "undecided"
                status, probability = reason, None
                if (
                    isinstance(verdict, RecordRoleVerdict)
                    and verdict.label in ("faithful", "changed", "uncertain")
                    and type(verdict.probability) in (int, float)
                    and 0 <= verdict.probability <= 1
                ):
                    probability = verdict.probability
                    status = verdict.label if probability >= 0.8 else "uncertain"
                    if status == "faithful":
                        decision = "accept"
                    elif status == "changed":
                        decision = "refuse"
                for position in self.positions[key]:
                    self.rows[position] = replace(
                        self.rows[position],
                        decision=decision,
                        reason=status,
                        probability=probability,
                    )
        return RecordRoleReport(changed, tuple(self.rows), tuple(requests))
