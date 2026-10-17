"""Correlate ownership claims with observed transport and binding events."""

from __future__ import annotations

import re
from typing import Any

from .evidence import LARGE_SOURCE_SHA256, digest, encode, require


def verify(value: dict[str, Any]) -> None:
    """Require independent request, admission and retirement evidence for every case."""
    events = value["transport"]
    require(isinstance(events, list) and bool(events), "ownership_boot_identity")
    boot = events[0].get("boot") if isinstance(events[0], dict) else None
    require(
        isinstance(boot, str)
        and re.fullmatch(r"[0-9a-f]{32}", boot)
        and all(isinstance(event, dict) and event.get("boot") == boot for event in events),
        "ownership_boot_identity",
    )
    _exchanges(
        [e for e in events if e.get("event") in ("request", "delete_requested")],
        "ownership_request_exchanges",
    )
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
        starts = [e for e in subset if e.get("event") == "binding_started"]
        bound = [_request(subset, start) for start in starts]
        _exchanges(bound, "binding_ambiguous_start")
        terminals = [
            _terminal_request(subset, event)
            for event in subset
            if event.get("event") in ("binding_result", "binding_cancelled")
        ]
        _exchanges(terminals, "binding_ambiguous_terminal")
        exchanges = {request["exchange"] for request in bound}
        require(
            exchanges == {request["exchange"] for request in terminals}, "binding_terminal_count"
        )
        require(
            not any(
                event.get("exchange") in exchanges
                for event in subset
                if event.get("event") == "admission_refused"
            ),
            "refused_binding_execution",
        )
        counts = (
            (1, 2)
            if name == "maf_ordinary_close"
            else (0, 2)
            if name == "maf_idle_close"
            else (1, 1)
        )
        require(
            all(
                sum(start.get("session") == sid for start in starts) == count
                for sid, count in zip((a, b), counts, strict=True)
            ),
            "binding_count",
        )
        deletes: dict[str, dict[str, Any]] = {}
        retirements: dict[str, dict[str, Any]] = {}
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
            peer_active = name == "maf_idle_close" and sid == a
            require(
                all(r.get(k) is True for k in ("completed", "sdk_finished", "sdk_terminated"))
                and all(
                    type(r.get(k)) is int and r[k] == 0
                    for k in ("sdk_streams", "request_ids", "requests")
                )
                and r.get("session_registered") is False
                and r.get("poisoned") is False
                and type(r.get("sessions")) is int
                and r["sessions"] == (1 if sid == a else 0)
                and r.get("active") is peer_active
                and "active_session" in r
                and r["active_session"] == (b if peer_active else None),
                "retirement_state",
            )
            requested = [
                e
                for e in subset
                if e.get("event") == "delete_requested" and e.get("session") == sid
            ]
            require(
                len(requested) == 1
                and requested[0].get("exchange") == r.get("exchange")
                and _ordered(requested[0], r),
                "retirement_delete",
            )
            response = _response(subset, requested[0], 200)
            require(_ordered(response, r), "retirement_response_order")
            deletes[sid], retirements[sid] = requested[0], r
            require(
                all(
                    _ordered(start, requested[0]) for start in starts if start.get("session") == sid
                ),
                "binding_after_delete",
            )
        require(_ordered(retirements[a], retirements[b]), "retirement_order")
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
        survivor_request, survivor_response = _binding(subset, survivor[-1])
        require(_ordered(survivor_response, deletes[b]), "survivor_after_delete")
        if name.startswith("maf_"):
            require(_ordered(retirements[a], survivor_request), "survivor_before_retirement")
        if name == "maf_ordinary_close":
            for sid in (a, b):
                results = [
                    e
                    for e in subset
                    if e.get("event") == "binding_result"
                    and e.get("session") == sid
                    and e is not survivor[-1]
                ]
                require(len(results) == 1, "ordinary_preclose_result")
                require(
                    all(
                        encode(results[0].get(k)) == encode(case["survivor"][k])
                        for k in ("completed", "status", "cleanup", "source_sha256")
                    ),
                    "ordinary_preclose_result",
                )
                _, response = _binding(subset, results[0])
                require(_ordered(response, deletes[a]), "ordinary_preclose_order")
            require(
                not any(e.get("event") == "admission_refused" for e in subset), "ordinary_admission"
            )
            continue
        container = case.get("container")
        require(
            isinstance(container, str) and re.fullmatch(r"[0-9a-f]{64}", container),
            "ownership_container_identity",
        )
        active = b if name == "maf_idle_close" else a
        starts = [
            e for e in subset if e.get("event") == "binding_started" and e.get("session") == active
        ]
        require(bool(starts), "active_binding")
        start = starts[0]
        require(_ordered(start, deletes[a]), "active_before_delete")
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
        terminal = cancelled or [
            e
            for e in subset
            if e.get("event") == "binding_result"
            and e.get("session") == active
            and e.get("request") == start.get("request")
            and _ordered(e, survivor_request)
        ]
        require(
            len(terminal) == 1
            and _ordered(start, terminal[0])
            and _ordered(terminal[0], retirements[active]),
            "active_retirement_order",
        )
        require(_ordered(terminal[0], survivor_request), "survivor_before_active_settled")
        if name == "maf_active_close":
            require(_ordered(deletes[a], terminal[0]), "active_close_order")
        elif name != "maf_idle_close":
            require(_ordered(terminal[0], deletes[a]), "raw_close_order")
        for event in subset:
            if event.get("event") == "request" and event.get("method") == "notifications/cancelled":
                require(_ordered(start, event) and _ordered(event, terminal[0]), "cancel_order")
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
                and admission.get("container") == container
                and type(admission.get("owned_count")) is int
                and admission["owned_count"] == 1
                and admission.get("active_pending") is True
                and _ordered(start, admission)
                and _ordered(admission, terminal[0]),
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
            response = _response(subset, requests[0], 200)
            require(_ordered(response, admission), "admission_response_order")
            if name in ("maf_active_close", "maf_idle_close"):
                before = deletes[a] if name == "maf_active_close" else retirements[a]
                require(_ordered(before, requests[0]), "close_admission_order")
        if name == "equal_ids_repeated_cancel":
            _cancel_probes(subset, admissions, a, b, terminal[0])
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
                and _ordered(e, survivor_request)
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

    require(
        all(
            event.get("session") in seen
            for event in events
            if event.get("event")
            in (
                "binding_started",
                "binding_result",
                "binding_cancelled",
                "admission_refused",
                "delete_requested",
                "retired",
            )
        ),
        "ownership_event_session",
    )


def _cancel_probes(
    events: list[dict[str, Any]],
    admissions: list[dict[str, Any]],
    active: str,
    peer: str,
    terminal: dict[str, Any],
) -> None:
    """Require peer isolation before cancellation and admission probes during repeated cleanup."""
    probes = []
    for admission in sorted(admissions, key=lambda e: e["time_ns"]):
        request = next(
            e
            for e in events
            if e.get("event") == "request" and e.get("exchange") == admission["exchange"]
        )
        probes.append([request, admission])
    cancellations = {
        sid: [
            [request, _response(events, request, 202)]
            for request in sorted(
                (
                    e
                    for e in events
                    if e.get("event") == "request"
                    and e.get("method") == "notifications/cancelled"
                    and e.get("session") == sid
                ),
                key=lambda e: e["time_ns"],
            )
        ]
        for sid in (active, peer)
    }
    first, *repeats = cancellations[active]
    sequence = probes[0] + cancellations[peer][0] + probes[1] + first
    for cancellation, probe in zip(repeats, probes[2:], strict=True):
        sequence += cancellation + probe
    sequence.append(terminal)
    require(
        all(_ordered(left, right) for left, right in zip(sequence, sequence[1:])),
        "cancel_probe_sequence",
    )


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


def _response(events: list[dict[str, Any]], request: dict[str, Any], status: int) -> dict[str, Any]:
    matches = [
        e
        for e in events
        if e.get("event") == "response"
        and e.get("exchange") == request.get("exchange")
        and e.get("session") == request.get("session")
    ]
    require(bool(request.get("exchange")) and len(matches) == 1, "control_response")
    response = matches[0]
    require(
        type(response.get("status")) is int
        and response["status"] == status
        and _ordered(request, response),
        "control_response",
    )

    return response


def _request(events: list[dict[str, Any]], start: dict[str, Any]) -> dict[str, Any]:
    matches = [
        e
        for e in events
        if e.get("event") == "request"
        and e.get("method") == "tools/call"
        and e.get("session") == start.get("session")
        and e.get("request") == start.get("request")
        and _ordered(e, start)
    ]
    require(bool(matches), "binding_transport_request")
    latest = max(e["time_ns"] for e in matches)
    matches = [e for e in matches if e["time_ns"] == latest]
    require(len(matches) == 1, "binding_ambiguous_request")
    request = matches[0]
    require(
        not any(
            e.get("event") == "response"
            and e.get("exchange") == request["exchange"]
            and _ordered(e, start)
            for e in events
        ),
        "binding_completed_exchange",
    )
    return request


def _terminal_request(events: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    require(
        isinstance(result.get("request"), str) and re.fullmatch(r"[0-9a-f]{64}", result["request"]),
        "binding_result_request",
    )
    starts = [
        e
        for e in events
        if e.get("event") == "binding_started"
        and e.get("session") == result["session"]
        and e.get("request") == result["request"]
        and _ordered(e, result)
    ]
    require(bool(starts), "binding_result_request")
    latest = max(e["time_ns"] for e in starts)
    starts = [e for e in starts if e["time_ns"] == latest]
    require(len(starts) == 1, "binding_ambiguous_start")
    request = _request(events, starts[0])
    require(
        not any(
            e.get("event") == "request"
            and e.get("method") == "tools/call"
            and e.get("session") == result["session"]
            and e.get("request") == result["request"]
            and e is not request
            and _ordered(request, e)
            and _ordered(e, result)
            for e in events
        ),
        "binding_reused_request",
    )
    return request


def _binding(
    events: list[dict[str, Any]], result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _terminal_request(events, result)
    response = _response(events, request, 200)
    require(_ordered(result, response), "binding_response_order")
    return request, response
