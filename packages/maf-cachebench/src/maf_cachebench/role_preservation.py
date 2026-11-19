"""Measure whether a full-coverage record merge preserves the answer to a role-specific task."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from statistics import median
from time import perf_counter
from typing import Literal

from maf_compaction._toolsummary import _distinctive_tokens  # pyright: ignore[reportPrivateUsage]

FIXTURE = "role-preservation-v1"
RUBRIC = (
    "Compare the candidate with the source records. Does the candidate preserve which value "
    "answers the task? Equivalent wording and order are allowed. A value reassigned to another "
    "role changes the answer. Return uncertain when the relationship cannot be determined."
)
CONFIDENCE = 0.8
_TIMEOUT = 5.0
_MAX_BYTES = 4096


@dataclass(frozen=True)
class CandidateRequest:
    """The synthetic text supplied to a host-owned judge, without the reference answer."""

    task: str
    sources: tuple[str, ...]
    candidate: str
    rubric: str = RUBRIC


@dataclass(frozen=True)
class CandidateVerdict:
    """The selected choice and its probability as returned by the judge."""

    label: Literal["faithful", "changed", "uncertain"]
    probability: float


CandidateJudge = Callable[[CandidateRequest], Awaitable[CandidateVerdict]]


@dataclass(frozen=True)
class _Source:
    name: str
    split: Literal["development", "held_out"]
    task: str
    labels: tuple[str, str]
    values: tuple[str, str]
    paraphrase: str
    other_first: bool
    swap_prose: bool


_TARGET = "Which deployment target should be used next?"
_AMOUNT = "What amount in USD has been approved for payment?"
_SOURCES = (
    _Source(
        "release",
        "development",
        _TARGET,
        ("Deployment target", "Retired target"),
        ("SRV-5001", "SRV-6209"),
        "Deploy to {answer}. We retired {other}.",
        False,
        False,
    ),
    _Source(
        "invoice",
        "development",
        _AMOUNT,
        ("Approved USD", "Requested USD"),
        ("4500", "8700"),
        "The request was for {other} USD. Pay {answer} USD.",
        True,
        True,
    ),
    _Source(
        "cutover",
        "held_out",
        _TARGET,
        ("Incoming target", "Outgoing target"),
        ("NODE-1784", "NODE-9642"),
        "{other} is leaving service. The cutover goes to {answer}.",
        True,
        True,
    ),
    _Source(
        "rollback",
        "held_out",
        _TARGET,
        ("Rollback destination", "Abandoned destination"),
        ("ZONE-9384", "ZONE-5267"),
        "Roll back to {answer} after withdrawing from {other}.",
        False,
        False,
    ),
    _Source(
        "settlement",
        "held_out",
        _AMOUNT,
        ("Authorized settlement USD", "Initial claim USD"),
        ("7600", "3400"),
        "They claimed {other} dollars and got authorization for {answer}.",
        False,
        True,
    ),
    _Source(
        "reimbursement",
        "held_out",
        _AMOUNT,
        ("Payable USD", "Submitted USD"),
        ("2850", "4825"),
        "Of the {other} dollars submitted, release {answer} dollars.",
        True,
        False,
    ),
)


@dataclass(frozen=True)
class _Case:
    name: str
    split: str
    variant: str
    request: CandidateRequest
    source_answer: str
    candidate_answer: str


def _cases() -> Iterator[_Case]:
    for source in _SOURCES:
        answer, other = source.values
        first, second = source.labels
        records = (f"{first}: {answer}", f"{second}: {other}")
        if source.other_first:
            records = records[::-1]
        faithful = "\n".join(reversed(records))
        sources = tuple(
            record + "\nThis entry is from the signed handover record." for record in records
        )
        swap = (
            source.paraphrase.format(answer=other, other=answer)
            if source.swap_prose
            else "\n".join((f"{first}: {other}", f"{second}: {answer}"))
        )
        variants = (
            ("faithful", faithful, answer),
            ("paraphrase", source.paraphrase.format(answer=answer, other=other), answer),
            ("swap", swap, other),
        )
        source_values = _distinctive_tokens("\n".join(sources))
        for variant, candidate, candidate_answer in variants:
            if _distinctive_tokens(candidate) != source_values or source_values != set(
                source.values
            ):
                raise ValueError(f"{source.name}/{variant} must preserve both distinctive values")
            yield _Case(
                source.name,
                source.split,
                variant,
                CandidateRequest(source.task, sources, candidate),
                answer,
                candidate_answer,
            )


def _pairs(text: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        if separator:
            pairs.add((label.strip().casefold(), value.strip()))
    return pairs


@dataclass(frozen=True)
class Observation:
    """One policy decision, including the answer remaining after accepting or refusing the merge."""

    status: str
    accepted: bool
    answer: str
    false_accept: bool
    false_refusal: bool
    requests: int
    latency_ms: float
    verdict: CandidateVerdict | None


async def _ask(
    judge: CandidateJudge, request: CandidateRequest
) -> tuple[str, CandidateVerdict | None, int, float]:
    if len(json.dumps(asdict(request), ensure_ascii=False).encode("utf-8")) > _MAX_BYTES:
        return "oversize", None, 0, 0.0
    start = perf_counter()
    verdict = None
    try:
        async with asyncio.timeout(_TIMEOUT):
            reply = await judge(request)
        if (
            reply.label not in ("faithful", "changed", "uncertain")
            or isinstance(reply.probability, bool)
            or not 0 <= reply.probability <= 1
        ):
            raise ValueError("invalid candidate judgment")
        verdict = reply
        status = reply.label if reply.probability >= CONFIDENCE else "uncertain"
    except TimeoutError:
        status = "timeout"
    except Exception:
        status = "unavailable"
    return status, verdict, 1, (perf_counter() - start) * 1000


@dataclass(frozen=True)
class CaseResult:
    """Observations for all configured policies on an identical source and candidate."""

    source: str
    split: str
    variant: str
    expected_answer: str
    candidate_answer: str
    policies: dict[str, Observation]


@dataclass(frozen=True)
class PolicySummary:
    """Decision quality and measurement coverage for one split."""

    candidates: int
    false_accepts: int
    false_refusals: int
    abstentions: int
    fallbacks: int
    requests: int
    requests_per_candidate: float
    median_request_ms: float | None
    passed: bool


def _summarize(rows: list[Observation]) -> PolicySummary:
    false_accepts = sum(row.false_accept for row in rows)
    false_refusals = sum(row.false_refusal for row in rows)
    fallbacks = sum(
        row.status in {"uncertain", "timeout", "unavailable", "oversize"} for row in rows
    )
    latencies = [row.latency_ms for row in rows if row.requests]
    return PolicySummary(
        len(rows),
        false_accepts,
        false_refusals,
        sum(row.status == "uncertain" for row in rows),
        fallbacks,
        len(latencies),
        len(latencies) / len(rows),
        median(latencies) if latencies else None,
        false_accepts == false_refusals == fallbacks == 0,
    )


@dataclass(frozen=True)
class RoleReport:
    """Configuration, split-level results and per-candidate decisions."""

    fixture: str
    rubric: str
    confidence: float
    timeout_seconds: float
    max_request_bytes: int
    model_id: str | None
    summaries: dict[str, dict[str, PolicySummary]]
    cases: list[CaseResult]


async def evaluate_roles(
    *, judge: CandidateJudge | None = None, model_id: str | None = None
) -> RoleReport:
    """Compare the missing-value gate, literal role pairs and an optional full-candidate judge."""
    if judge is not None and not (model_id and model_id.strip()):
        raise ValueError("a candidate judge requires a model identity")
    cases: list[CaseResult] = []
    for case in _cases():
        request = case.request
        pairs_match = _pairs("\n".join(request.sources)) == _pairs(request.candidate)
        outcomes: dict[str, tuple[str, CandidateVerdict | None, int, float]] = {
            "missing_value": ("no_missing_values", None, 0, 0.0),
            "structured_pairs": ("faithful" if pairs_match else "changed", None, 0, 0.0),
        }
        if judge is not None:
            outcomes["candidate_judge"] = await _ask(judge, request)
        policies: dict[str, Observation] = {}
        for policy, (status, verdict, requests, elapsed) in outcomes.items():
            # Every fixture clears the count bar, including on an unavailable judgment.
            accepted = status != "changed"
            safe = case.candidate_answer == case.source_answer
            policies[policy] = Observation(
                status,
                accepted,
                case.candidate_answer if accepted else case.source_answer,
                accepted and not safe,
                not accepted and safe,
                requests,
                elapsed,
                verdict,
            )
        cases.append(
            CaseResult(
                case.name,
                case.split,
                case.variant,
                case.source_answer,
                case.candidate_answer,
                policies,
            )
        )
    summaries = {
        split: {
            policy: _summarize([case.policies[policy] for case in cases if case.split == split])
            for policy in cases[0].policies
        }
        for split in ("development", "held_out")
    }
    return RoleReport(FIXTURE, RUBRIC, CONFIDENCE, _TIMEOUT, _MAX_BYTES, model_id, summaries, cases)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the offline comparison for both tasks as JSON."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    print(json.dumps(asdict(asyncio.run(evaluate_roles())), indent=2, allow_nan=False))
    return 0
