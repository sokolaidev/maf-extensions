"""The filesystem probe must distinguish policy refusal from missing data and failed execution."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_files_probe")


@pytest.mark.parametrize(
    "output", [b'{"errno":null}', b'{"errno":2}', b'{"errno":20}', b'{"errno":13,"extra":true}']
)
def test_missing_or_successful_access_is_not_policy_denial(probe, output):
    assert not probe.evaluate("outside_policy_read", (0, output, b""))
    assert probe.evaluate("outside_policy_read", (0, b'{"errno":13}', b""))


@pytest.mark.parametrize("status,error", [(1, b""), (0, b"warning")])
def test_failed_execution_cannot_supply_success_evidence(probe, status, error):
    assert not probe.evaluate("guest_can_overwrite", (status, b"True\n", error))


def test_binary_oracle_rejects_truncation_and_text_conversion(probe):
    assert probe.evaluate("binary_readback", (0, probe.PAYLOAD, b""))
    for value in (probe.PAYLOAD[:-1], probe.PAYLOAD.decode(errors="replace").encode()):
        assert not probe.evaluate("binary_readback", (0, value, b""))


def event(kind, value):
    body = SimpleNamespace(exit_code=value) if kind == "exit" else SimpleNamespace(data=value)
    return SimpleNamespace(WhichOneof=lambda _: kind, **{kind: body})


@pytest.mark.parametrize(
    "events,reason",
    [
        ([event("stdout", b"x")], "missing execution exit"),
        ([event("exit", 0), event("stdout", b"late")], "data after terminal exit"),
        ([event("exit", 0), event("stderr", b"late")], "data after terminal exit"),
        ([event("exit", 0), event("exit", 0)], "duplicate exit"),
        ([event("stdout", b"x" * 10000), event("stderr", b"x" * 6385)], "output cap"),
        ([event("unexpected", b"")], "unknown stream event"),
    ],
)
def test_untrustworthy_stream_is_refused_and_cancelled(probe, events, reason):
    cancelled = []

    class Call:
        def __iter__(self):
            return iter(events)

        def cancel(self):
            cancelled.append(True)

    client = SimpleNamespace(
        pb=SimpleNamespace(ExecSandboxRequest=lambda **kwargs: kwargs),
        scope=object(),
        token="fixture",
        stub=SimpleNamespace(ExecSandbox=lambda *args, **kwargs: Call()),
    )
    with pytest.raises(RuntimeError, match=reason):
        probe.execute(client, "fixture", "pass")
    assert cancelled == [True]
