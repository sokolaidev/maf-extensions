"""Compare record-merge loss policies on matched, synthetic continuations."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from math import ceil, isfinite
from time import perf_counter
from typing import Literal

from agent_framework import CharacterEstimatorTokenizer
from maf_compaction import DEFAULT_COVERAGE_SHARE
from maf_compaction._toolsummary import _distinctive_tokens  # pyright: ignore[reportPrivateUsage]

_TASK = "Which deployment target should the next step use?"
_TARGET = "Deployment target: "
_RUBRIC = "Mark a missing value material if its role or value is needed to answer the task."
_TOKENIZER = CharacterEstimatorTokenizer()


@dataclass(frozen=True)
class _Case:
    name: str
    candidate: str
    retained: tuple[str, ...] = ()


_ACTIVE = _TARGET + "SRV-9000"
_RETIRED = "Retired target: SRV-8000"
_AUDITS = ("Audit: LOG-1001", "Audit: LOG-1002", "Audit: LOG-1003")
_FACTS = (_ACTIVE, _RETIRED, *_AUDITS)
_SOURCES = (
    "\n".join((_ACTIVE, _RETIRED, "Both entries were verified during the deployment handover.")),
    "\n".join((*_AUDITS, "Each audit entry describes a completed review of prior work.")),
)
_PARTIAL = "\n".join((_ACTIVE, _RETIRED, _AUDITS[0]))
_NO_TARGET = "\n".join((_RETIRED, *_AUDITS))
_CASES = (
    _Case("unique-target", _NO_TARGET),
    _Case("audits-repeated", _PARTIAL, (*_AUDITS[1:], _AUDITS[1])),
    _Case("audits-omitted", _PARTIAL),
    _Case(
        "target-in-wrong-role", "\n".join((_RETIRED, *_AUDITS[:2])), ("Prior candidate: SRV-9000",)
    ),
    _Case("roles-swapped", "\n".join((_TARGET + "SRV-8000", "Retired target: SRV-9000", *_AUDITS))),
    _Case("below-floor", _ACTIVE),
)


@dataclass(frozen=True)
class MissingValue:
    """One omitted token, its source lines and its frequency in subsequent retained messages."""

    value: str
    source_lines: tuple[str, ...]
    later_messages: int


@dataclass(frozen=True)
class JudgeRequest:
    """The complete synthetic task and record text shared with a host's judge."""

    task: str
    rubric: str
    rubric_version: str
    candidate: str
    retained: tuple[str, ...]
    missing: tuple[MissingValue, ...]


@dataclass(frozen=True)
class Verdict:
    """A materiality label and its probability for one requested value."""

    label: Literal["material", "minor", "uncertain"]
    probability: float


@dataclass(frozen=True)
class JudgeAnswer:
    """Per-value judgments and the cost in USD when supplied by the adapter."""

    values: Mapping[str, Verdict]
    cost_usd: float | None = None


Judge = Callable[[JudgeRequest], Awaitable[JudgeAnswer]]


@dataclass(frozen=True)
class _Observation:
    accepted: bool
    false_accept: bool
    false_refusal: bool
    next_target: str | None
    next_task_correct: bool
    facts_preserved: int
    prompt_tokens: int
    over_budget: bool


def _next_target(lines: set[str]) -> str | None:
    targets = {line.removeprefix(_TARGET) for line in lines if line.startswith(_TARGET)}
    return targets.pop() if len(targets) == 1 else None


def _score(case: _Case, accepted: bool, budget: int) -> _Observation:
    candidate = "\n".join((case.candidate, *case.retained))
    safe = _next_target(set(candidate.splitlines())) == "SRV-9000"
    records = (case.candidate,) if accepted else _SOURCES
    prompt = "\n".join((*records, *case.retained, _TASK))
    lines = set(prompt.splitlines())
    target = _next_target(lines)
    tokens = _TOKENIZER.count_tokens(prompt)
    return _Observation(
        accepted,
        accepted and not safe,
        not accepted and safe,
        target,
        target == "SRV-9000",
        len(lines.intersection(_FACTS)),
        tokens,
        tokens > budget,
    )


@dataclass(frozen=True)
class _Judgment:
    status: str
    requests: int = 0
    latency_ms: float = 0.0
    cost_usd: float | None = 0.0
    values: Mapping[str, Verdict] | None = None


@dataclass(frozen=True)
class CaseResult:
    """Matched policy observations and the optional judge measurement for one candidate."""

    case: str
    source_values: int
    candidate_kept: int
    context_kept: int
    later_mentions: dict[str, int]
    judge: _Judgment
    policies: dict[str, _Observation]


@dataclass(frozen=True)
class MergeLossReport:
    """The benchmark configuration, policy totals and individual observations."""

    fixture: str
    source_facts: int
    share: float
    floor: float
    budget: int
    model_id: str | None
    rubric: str
    rubric_version: str
    confidence: float
    max_request_bytes: int
    timeout_seconds: float
    totals: dict[str, dict[str, int]]
    cases: list[CaseResult]


async def _judge(
    client: Judge, request: JudgeRequest, *, max_bytes: int, timeout: float, confidence: float
) -> _Judgment:
    if len(json.dumps(asdict(request), ensure_ascii=False).encode("utf-8")) > max_bytes:
        return _Judgment("oversize")
    start = perf_counter()
    cost = None
    try:
        async with asyncio.timeout(timeout):
            answer = await client(request)
        if answer.cost_usd is not None:
            if (
                isinstance(answer.cost_usd, bool)
                or not isfinite(answer.cost_usd)
                or answer.cost_usd < 0
            ):
                raise ValueError("invalid cost")
            cost = answer.cost_usd
        if set(answer.values) != {item.value for item in request.missing}:
            raise ValueError("missing or unexpected verdict")
        for verdict in answer.values.values():
            if (
                verdict.label not in ("minor", "material", "uncertain")
                or isinstance(verdict.probability, bool)
                or not 0 <= verdict.probability <= 1
            ):
                raise ValueError("invalid verdict")
        verdicts = dict(answer.values)
        if any(v.label == "uncertain" or v.probability < confidence for v in verdicts.values()):
            status = "uncertain"
        elif any(v.label == "material" for v in verdicts.values()):
            status = "material"
        else:
            status = "minor"
        return _Judgment(status, 1, (perf_counter() - start) * 1000, cost, verdicts)
    except TimeoutError:
        status = "timeout"
    except Exception:
        status = "unavailable"
    return _Judgment(status, 1, (perf_counter() - start) * 1000, cost)


async def evaluate(
    *,
    judge: Judge | None = None,
    model_id: str | None = None,
    rubric: str = _RUBRIC,
    rubric_version: str = "deployment-v1",
    share: float = DEFAULT_COVERAGE_SHARE,
    floor: float = 0.6,
    confidence: float = 0.8,
    timeout: float = 5.0,
    max_request_bytes: int = 4096,
    budget: int = 45,
) -> MergeLossReport:
    """Score each fixed candidate under count, surviving-context and optional judge policies.

    The host owns the async judge. Only the bounded JudgeRequest leaves the benchmark.
    The score reads role-labelled fixture lines and estimates prompt tokens from their text.
    """
    if not 0 <= floor <= share <= 1 or not 0 <= confidence <= 1:
        raise ValueError("require 0 <= floor <= share <= 1 and 0 <= confidence <= 1")
    if not isfinite(timeout) or timeout <= 0 or max_request_bytes < 1 or budget < 1:
        raise ValueError("timeout, max_request_bytes and budget must be positive")
    if judge is not None and not (
        model_id and model_id.strip() and rubric.strip() and rubric_version.strip()
    ):
        raise ValueError("a judge requires model_id, rubric and rubric_version")
    lines_by_value: dict[str, list[str]] = {}
    for body in _SOURCES:
        for line in body.splitlines():
            for value in _distinctive_tokens(line):
                lines_by_value.setdefault(value, []).append(line)
    source_values = set(lines_by_value)
    required = ceil(len(source_values) * share)
    minimum = ceil(len(source_values) * floor)
    policies = ("count", "context") if judge is None else ("count", "context", "judge")
    results: list[CaseResult] = []
    totals = {p: {"false_accepts": 0, "false_refusals": 0, "over_budget": 0} for p in policies}
    for case in _CASES:
        lost = source_values - _distinctive_tokens(case.candidate)
        kept = len(source_values) - len(lost)
        later = [_distinctive_tokens(message) for message in case.retained]
        missing = tuple(
            MissingValue(
                value,
                tuple(lines_by_value[value]),
                sum(value in values for values in later),
            )
            for value in sorted(lost)
        )
        surviving = kept + sum(item.later_messages > 0 for item in missing)
        count_passes = kept >= required
        decisions = {"count": count_passes, "context": kept >= minimum and surviving >= required}
        judgment = _Judgment("off")
        if judge is not None:
            judgment = _Judgment("below_floor" if kept < minimum else "no_missing_values")
            if missing and kept >= minimum:
                request = JudgeRequest(
                    _TASK, rubric, rubric_version, case.candidate, case.retained, missing
                )
                judgment = await _judge(
                    judge,
                    request,
                    max_bytes=max_request_bytes,
                    timeout=timeout,
                    confidence=confidence,
                )
            decisions["judge"] = {"minor": True, "material": False}.get(
                judgment.status, count_passes
            )
        outcomes = {
            policy: _score(case, accepted, budget) for policy, accepted in decisions.items()
        }
        for policy, outcome in outcomes.items():
            totals[policy]["false_accepts"] += outcome.false_accept
            totals[policy]["false_refusals"] += outcome.false_refusal
            totals[policy]["over_budget"] += outcome.over_budget
        results.append(
            CaseResult(
                case=case.name,
                source_values=len(source_values),
                candidate_kept=kept,
                context_kept=surviving,
                later_mentions={m.value: m.later_messages for m in missing},
                judge=judgment,
                policies=outcomes,
            )
        )
    return MergeLossReport(
        fixture="deployment-v1",
        source_facts=len(_FACTS),
        share=share,
        floor=floor,
        budget=budget,
        model_id=model_id,
        rubric=rubric,
        rubric_version=rubric_version,
        confidence=confidence,
        max_request_bytes=max_request_bytes,
        timeout_seconds=timeout,
        totals=totals,
        cases=results,
    )


async def _fixture_judge(request: JudgeRequest) -> JudgeAnswer:
    retained = {line for message in request.retained for line in message.splitlines()}
    verdicts: dict[str, Verdict] = {}
    for item in request.missing:
        needed = any(
            line.startswith(_TARGET) and line not in retained for line in item.source_lines
        )
        verdicts[item.value] = Verdict("material" if needed else "minor", 1.0)
    return JudgeAnswer(verdicts, cost_usd=0.0)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the matched offline comparison as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-judge", action="store_true", help="Include a scripted offline judge."
    )
    args = parser.parse_args(argv)
    report = asyncio.run(
        evaluate(
            judge=_fixture_judge if args.fixture_judge else None,
            model_id="scripted-fixture" if args.fixture_judge else None,
        )
    )
    print(json.dumps(asdict(report), indent=2, allow_nan=False))
    return 0
