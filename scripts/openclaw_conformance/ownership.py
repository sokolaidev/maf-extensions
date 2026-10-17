"""Correlate ownership claims with observed transport and binding events."""

from __future__ import annotations

import re
from typing import Any

from .evidence import LARGE_SOURCE_SHA256, digest, encode, require


def verify(value: dict[str, Any]) -> None:
    """Require independent request, admission and retirement evidence for every case."""
    events = value["transport"]
    seen: set[str] = set()
    for name, case in value["cases"].items():
        a, b = case.get("session_a"), case.get("session_b")
        require(
            all(
                isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s) and s not in seen
                for s in (a, b)
            )
            and a != b,
            "ownership_session_identity",
        )
        seen.update((a, b))
        subset = [e for e in events if e.get("session") in (a, b)]
        _exchanges(
            [e for e in subset if e.get("event") == "request"], "ownership_request_exchanges"
        )
        for sid, count in ((a, 4), (b, 1)):
            requests = [
                e
                for e in subset
                if e.get("event") == "request"
                and e.get("method") == "notifications/cancelled"
                and e.get("session") == sid
            ]
            require(
                len(requests) == (count if name == "equal_ids_repeated_cancel" else 0),
                "cancel_count",
            )
            for request in requests:
                require(request.get("target") == digest(42), "cancel_target")
                _response(subset, request, 202)
            retired = [e for e in subset if e.get("event") == "retired" and e.get("session") == sid]
            require(len(retired) == 1, "retirement_count")
            r = retired[0]
            require(
                all(r.get(k) is True for k in ("completed", "sdk_finished", "sdk_terminated"))
                and all(
                    type(r.get(k)) is int and r[k] == 0
                    for k in ("sdk_streams", "request_ids", "requests")
                )
                and r.get("session_registered") is False
                and r.get("poisoned") is False,
                "retirement_state",
            )
            require(
                sum(
                    e.get("event") == "delete_requested"
                    and e.get("exchange") == r.get("exchange")
                    and e.get("session") == sid
                    and _ordered(e, r)
                    for e in subset
                )
                == 1,
                "retirement_delete",
            )
        survivor = [
            e
            for e in subset
            if e.get("session") == b
            and e.get("event") == "binding_result"
            and e.get("source_sha256") == case["survivor"]["source_sha256"]
        ]
        require(bool(survivor), "survivor_result")
        require(
            all(
                encode(survivor[-1].get(k)) == encode(case["survivor"][k])
                for k in ("completed", "status", "cleanup", "source_sha256")
            ),
            "survivor_result",
        )
        _binding(subset, survivor[-1])
        if name.startswith("maf_"):
            retired_a = next(
                e for e in subset if e.get("event") == "retired" and e.get("session") == a
            )
            require(_ordered(retired_a, survivor[-1]), "survivor_before_retirement")
        if name == "maf_ordinary_close":
            require(
                not any(e.get("event") == "admission_refused" for e in subset), "ordinary_admission"
            )
            continue
        active = b if name == "maf_idle_close" else a
        starts = [
            e for e in subset if e.get("event") == "binding_started" and e.get("session") == active
        ]
        require(bool(starts), "active_binding")
        start = starts[0]
        require(
            isinstance(start.get("request"), str)
            and re.fullmatch(r"[0-9a-f]{64}", start["request"]),
            "active_request_identity",
        )
        if name in ("equal_ids_repeated_cancel", "response_loss"):
            require(start.get("request") == digest(42), "active_request")
        cancelled = [e for e in subset if e.get("event") == "binding_cancelled"]
        if name in ("equal_ids_repeated_cancel", "maf_active_close"):
            require(
                len(cancelled) == 1
                and cancelled[0].get("session") == active
                and cancelled[0].get("request") == start.get("request")
                and _ordered(start, cancelled[0]),
                "binding_cancellation",
            )
        else:
            require(not cancelled, "unexpected_binding_cancellation")
        admissions = [e for e in subset if e.get("event") == "admission_refused"]
        require(
            len(admissions) == (5 if name == "equal_ids_repeated_cancel" else 1), "admission_count"
        )
        _exchanges(admissions, "admission_exchanges")
        for admission in admissions:
            require(
                admission.get("session") == b
                and admission.get("request") == digest(42)
                and admission.get("active_session") == active
                and admission.get("active_request") == start.get("request")
                and admission.get("container") == case.get("container")
                and type(admission.get("owned_count")) is int
                and admission["owned_count"] == 1
                and admission.get("active_pending") is True
                and _ordered(start, admission),
                "admission_identity",
            )
            requests = [
                e
                for e in subset
                if e.get("event") == "request"
                and e.get("method") == "tools/call"
                and e.get("exchange") == admission.get("exchange")
                and e.get("session") == b
                and e.get("request") == digest(42)
            ]
            require(len(requests) == 1 and _ordered(requests[0], admission), "admission_request")
            _response(subset, requests[0], 200)
        if name == "maf_idle_close":
            require(
                case.get("call_raised") is False
                and case.get("call_error_type") is None
                and case.get("call_error_code") is None,
                "idle_close_interrupted",
            )
            results = [
                e
                for e in subset
                if e.get("event") == "binding_result"
                and e.get("session") == active
                and e.get("request") == start.get("request")
            ]
            require(
                len(results) == 1
                and results[0].get("source_sha256") == LARGE_SOURCE_SHA256
                and results[0].get("cleanup") == "confirmed"
                and type(results[0].get("completed")) is bool
                and results[0].get("status") == ("ok" if results[0]["completed"] else "incomplete"),
                "idle_peer_result",
            )
            _binding(subset, results[0])


def _exchanges(events: list[dict[str, Any]], reason: str) -> None:
    ids = [e.get("exchange") for e in events]
    require(all(isinstance(v, str) and 0 < len(v) <= 128 for v in ids), reason)
    require(len(set(ids)) == len(ids), reason)


def _ordered(first: dict[str, Any], last: dict[str, Any]) -> bool:
    return (
        type(first.get("time_ns")) is int
        and type(last.get("time_ns")) is int
        and 0 < first["time_ns"] <= last["time_ns"]
    )


def _response(events: list[dict[str, Any]], request: dict[str, Any], status: int) -> None:
    require(
        bool(request.get("exchange"))
        and sum(
            e.get("event") == "response"
            and e.get("exchange") == request["exchange"]
            and e.get("session") == request["session"]
            and type(e.get("status")) is int
            and e["status"] == status
            and _ordered(request, e)
            for e in events
        )
        == 1,
        "control_response",
    )


def _binding(events: list[dict[str, Any]], result: dict[str, Any]) -> None:
    require(
        isinstance(result.get("request"), str)
        and re.fullmatch(r"[0-9a-f]{64}", result["request"])
        and any(
            e.get("event") == "binding_started"
            and e.get("session") == result["session"]
            and e.get("request") == result["request"]
            and _ordered(e, result)
            for e in events
        ),
        "binding_result_request",
    )
