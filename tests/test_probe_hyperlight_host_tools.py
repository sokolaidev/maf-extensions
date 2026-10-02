"""Offline checks for the native-channel research harness and its evidence decisions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_hyperlight_host_tools.py"
_spec = importlib.util.spec_from_file_location("probe_host_tools", _SCRIPT)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


@pytest.mark.parametrize("raw", [b"{}", b"[]\n", b'{"n":NaN}\n', b'{"n":Infinity}\n'])
def test_invalid_wire_data_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        probe.decode(raw)


def test_ipc_limit_counts_escaping_and_delimiter(monkeypatch: pytest.MonkeyPatch) -> None:
    message = {"value": 'é\n"\\'}
    encoded = probe.encode(message)
    monkeypatch.setattr(probe, "WIRE_LIMIT", len(encoded))
    assert probe.decode(encoded) == message
    monkeypatch.setattr(probe, "WIRE_LIMIT", len(encoded) - 1)
    with pytest.raises(ValueError, match="limit"):
        probe.encode(message)
    with pytest.raises(ValueError, match="oversized"):
        probe.decode(encoded)


def test_stale_and_duplicate_requests_never_spend_policy_authority() -> None:
    async def exercise() -> None:
        policy = probe.Policy("current")
        request = {
            "op": "callback",
            "run": "old",
            "seq": 1,
            "payload": '{"name":"echo","arguments":{"value":1}}',
        }
        with pytest.raises(ValueError, match="stale"):
            await policy.service(request)
        assert not policy.events
        request["run"] = "current"
        response = await policy.service(request)
        assert json.loads(response["response"]) == {"value": 1}
        events = list(policy.events)
        with pytest.raises(ValueError, match="duplicate"):
            await policy.service(request)
        assert policy.events == events
        assert probe.CONTEXT.get() == "unbound"
        await policy.cleanup()

    asyncio.run(exercise())


def test_request_limit_precedes_policy_and_stale_prepared_is_rejected() -> None:
    async def exercise() -> None:
        policy = probe.Policy("current")
        policy.request_limit = 1
        response = await policy.service(
            {"op": "callback", "run": "current", "seq": 1, "payload": "{}"}
        )
        assert "refusal" in json.loads(response["response"])
        assert not any(e["stage"] == "policy_enter" for e in policy.events)
        with pytest.raises(ValueError, match="stale"):
            await policy.service({"op": "prepared", "run": "old", "seq": 1})
        await policy.cleanup()

    asyncio.run(exercise())


def test_delivery_counterexample_requires_both_observation_and_later_failure() -> None:
    report = {
        "case": "handoff-failure",
        "reaped": True,
        "result": {"exit_code": 1, "stderr": "synthetic failure before native marshalling"},
        "events": [{"outcome": "delivered"}, {"stage": "worker_prepared"}],
    }
    assert probe.validate_reports([report]) == []
    report["events"] = [{"stage": "worker_prepared"}]
    assert probe.validate_reports([report]) == ["handoff-failure"]


@pytest.mark.parametrize(
    "name", ["program-timeout", "stubborn-callback", "stale-generation", "native-request-16300"]
)
def test_initialization_failure_is_not_a_negative_case_pass(name: str) -> None:
    report = {
        "case": name,
        "reaped": True,
        "error": "TimeoutError",
        "error_detail": "cleanup budget",
        "events": [],
    }
    assert probe.validate_reports([report]) == [name]


def test_large_request_eof_is_an_observation_only_after_initialization() -> None:
    report = {
        "case": "native-request-16300",
        "initialized": True,
        "reaped": True,
        "error": "EOFError",
        "events": [],
    }
    assert probe.validate_reports([report]) == []
    report["events"] = [{"stage": "policy_enter"}]
    assert probe.validate_reports([report]) == ["native-request-16300"]
    report["events"] = []
    report["case"] = "native-request-8000"
    assert probe.validate_reports([report]) == ["native-request-8000"]


def test_timeout_refusal_requires_confirmed_host_stop() -> None:
    report = {
        "case": "callback-timeout",
        "reaped": True,
        "result": {"exit_code": 0, "stdout": "timeout\nalive\n"},
        "events": [],
    }
    assert probe.validate_reports([report]) == ["callback-timeout"]
    report["events"] = [{"stage": "timeout_refusal", "stopped": True}]
    assert probe.validate_reports([report]) == []
    report["reaped"] = False
    assert probe.validate_reports([report]) == ["callback-timeout"]
