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
        "events": [{"outcome": "delivery_uncertain"}, {"stage": "worker_prepared"}],
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


@pytest.mark.parametrize("size", [16200, 16300, 20000, 64000, 256000, 400000])
def test_large_request_eof_is_an_observation_only_after_initialization(size: int) -> None:
    name = f"native-request-{size}"
    report = {
        "case": name,
        "initialized": True,
        "reaped": True,
        "error": "EOFError",
        "events": [],
    }
    assert probe.validate_reports([report]) == []
    report["events"] = [{"stage": "policy_enter"}]
    assert probe.validate_reports([report]) == [name]
    report["events"] = []
    report["initialized"] = False
    assert probe.validate_reports([report]) == [name]
    report["initialized"] = True
    report["reaped"] = False
    assert probe.validate_reports([report]) == [name]


@pytest.mark.parametrize("size", [8000, 12000, 16000, 16100, 16199])
def test_eof_below_observed_failure_boundary_is_a_regression(size: int) -> None:
    name = f"native-request-{size}"
    report = {"case": name, "initialized": True, "reaped": True, "error": "EOFError", "events": []}
    assert probe.validate_reports([report]) == [name]


def test_cli_saves_failed_evidence_and_exits_nonzero_for_request_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def reports(selected: list[str], docker: bool) -> list[dict[str, object]]:
        return [
            {
                "case": "native-request-16100",
                "initialized": True,
                "reaped": True,
                "error": "EOFError",
                "events": [],
            }
        ]

    output = tmp_path / "probe.json"
    monkeypatch.setattr(probe, "run_probes", reports)
    monkeypatch.setattr(probe, "version", lambda name: "0.7.0")
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: "baseline")
    monkeypatch.setattr(probe.sys, "argv", [str(_SCRIPT), "--live", "--output", str(output)])
    with pytest.raises(SystemExit, match="unexpected probe results: native-request-16100"):
        probe.main()
    assert json.loads(output.read_text(encoding="utf-8"))["unexpected_results"] == [
        "native-request-16100"
    ]


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


@pytest.mark.parametrize(
    "failure", [None, "reuse", "late_registration", "retirement", "policy_cleanup", "both_cleanup"]
)
def test_reuse_policies_are_closed_and_drained_after_worker_retirement(monkeypatch, failure):
    policies = []
    workers = []

    class Worker:
        def __init__(self, config):
            self.process = self
            self._drainer = self
            self._stderr = b""
            self.retired = False
            workers.append(self)

        def poll(self):
            return 0 if self.retired else None

        def is_alive(self):
            return not self.retired

        def close(self):
            self.retired = True
            if failure in {"retirement", "both_cleanup"}:
                raise OSError("worker retirement failed")

        async def exchange(self, message, policy, timeout):
            service = policy.service
            if message["op"] == "init":
                return {"op": "ready"}
            if message["op"] == "run":
                await service(
                    {
                        "op": "callback",
                        "run": message["run"],
                        "seq": 1,
                        "payload": '{"name":"echo","arguments":{"value":1}}',
                    }
                )
                if failure == "reuse" and message["run"].endswith("-next"):
                    raise RuntimeError("reuse failed after callback preparation")
            if message["op"] == failure:
                raise RuntimeError("post-reuse operation failed")
            return {"op": "result", "exit_code": 0}

    class Policy(probe.Policy):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            policies.append(self)

        async def cleanup(self):
            assert workers[0].retired
            await super().cleanup()
            if failure in {"policy_cleanup", "both_cleanup"} and self is policies[0]:
                raise ValueError("policy cleanup failed")

    monkeypatch.setattr(probe, "ProbeWorker", Worker)
    monkeypatch.setattr(probe, "Policy", Policy)

    async def publish(result):
        pytest.fail("closed probe policy must not publish")

    async def exercise():
        if failure in {"retirement", "policy_cleanup", "both_cleanup"}:
            expected = {
                "retirement": OSError,
                "policy_cleanup": ValueError,
                "both_cleanup": ExceptionGroup,
            }[failure]
            with pytest.raises(expected) as error:
                await probe.probe_case("reuse-check", "pass", reuse=True)
            if failure == "both_cleanup":
                assert [type(e) for e in error.value.exceptions] == [OSError, ValueError]
            assert len(policies) == 2 and workers[0].retired
            for policy in policies:
                assert policy.closed and not policy.pending
                with pytest.raises(RuntimeError, match="closed"):
                    await policy.run.call("echo", {"value": 1}, publish=publish)
            return
        report = await probe.probe_case("reuse-check", "pass", reuse=True)
        assert report["reaped"] and len(policies) == 2
        assert all(not policy.pending for policy in policies)
        for policy in policies:
            with pytest.raises(RuntimeError, match="closed"):
                await policy.run.call("echo", {"value": 1}, publish=publish)
        for key in ("events", "reuse_events"):
            assert [
                event["outcome"] for event in report[key] if event["stage"] == "core_observation"
            ] == ["delivery_uncertain"]
        assert (report.get("error") == "RuntimeError") is (failure is not None)

    asyncio.run(exercise())
