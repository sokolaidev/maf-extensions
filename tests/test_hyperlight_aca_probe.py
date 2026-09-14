"""Keep incomplete ACA probe collections distinct from measured Hyperlight failures."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROBE = Path(__file__).resolve().parents[1] / "docs/sandbox/research/hyperlight-aca-probe.py"


@pytest.fixture
def probe(monkeypatch):
    spec = importlib.util.spec_from_file_location("hyperlight_aca_probe", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    records = []
    holds = []
    monkeypatch.setattr(
        module, "emit", lambda stage, **values: records.append({"stage": stage, **values})
    )
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            geteuid=lambda: 0,
            chmod=lambda *_: None,
            chown=lambda *_: None,
            defpath=os.defpath,
            killpg=lambda *_: None,
        ),
    )
    monkeypatch.setattr(module, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(sys, "argv", [str(_PROBE), "--hold-seconds", "1800"])
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(platform="linux", executable=sys.executable),
    )
    monkeypatch.setattr(
        module,
        "platform",
        SimpleNamespace(
            machine=lambda: "x86_64",
            python_version=lambda: "3.13.15",
            release=lambda: "test-kernel",
            freedesktop_os_release=lambda: {"PRETTY_NAME": "test Linux"},
        ),
    )
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=holds.append))

    def path(value):
        if str(value).startswith(("/proc/", "/sys/")):
            return SimpleNamespace(read_text=lambda: "")
        return Path(value)

    monkeypatch.setattr(module, "Path", path)
    return module, records, holds


def install_children(monkeypatch, module, fault):
    calls = []

    def popen(command, **options):
        calls.append((command[-1], options["user"]))
        broken = len(calls) == 1
        if broken and fault == "launch":
            raise OSError("child launch refused")
        marker = {"stage": "child_complete", "uid": options["user"], "child_stage": command[-1]}
        if broken and fault == "wrong_identity":
            marker["uid"] = 1234
        if broken and fault == "wrong_stage":
            marker["child_stage"] = "unknown"
        observation = {
            "stage": "guest_run",
            "status": "error",
            "detail": "No Hypervisor was found for Sandbox",
        }
        output = json.dumps(observation) + "\n" + json.dumps(marker) + "\n"
        if broken and fault == "missing":
            output = ""
        if broken and fault == "malformed":
            output = "not a completion record\n"
        if broken and fault == "truncated":
            output = "x" * (module.DIAGNOSTIC_LIMIT + 1) + "\n" + output

        class Child:
            pid = 123456
            returncode = 7 if broken and fault == "exit" else 0
            stdout = io.BytesIO(output.encode())
            stderr = io.BytesIO()

            def __init__(self):
                self.waits = 0

            def wait(self, timeout):
                self.waits += 1
                if broken and fault == "timeout" and self.waits == 1:
                    raise subprocess.TimeoutExpired(command, timeout)
                return self.returncode

        return Child()

    monkeypatch.setattr(
        module,
        "subprocess",
        SimpleNamespace(
            Popen=popen,
            TimeoutExpired=subprocess.TimeoutExpired,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    if fault == "cleanup":

        def killpg(*_):
            if len(calls) == 1:
                raise PermissionError("process group cleanup refused")

        monkeypatch.setattr(module.os, "killpg", killpg)
    return calls


@pytest.mark.parametrize(
    "fault",
    [
        "launch",
        "cleanup",
        "exit",
        "timeout",
        "missing",
        "malformed",
        "wrong_identity",
        "wrong_stage",
        "truncated",
    ],
)
def test_incomplete_children_refuse_success_and_log_hold(probe, monkeypatch, fault):
    module, records, holds = probe
    calls = install_children(monkeypatch, module, fault)
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code == 1
    assert calls == [("devices", 0), ("guest", 0), ("devices", 65534), ("guest", 65534)]
    assert records[-1]["stage"] == "probe_incomplete"
    assert not any(record["stage"] == "probe_complete" for record in records)
    assert holds == []


def test_measured_native_failure_still_completes_collection(probe, monkeypatch):
    module, records, holds = probe
    install_children(monkeypatch, module, None)
    module.main()
    results = [record for record in records if record["stage"] == "child_result"]
    assert len(results) == 4
    assert all("No Hypervisor was found for Sandbox" in result["stdout"] for result in results)
    assert records[-1]["stage"] == "probe_complete"
    assert holds == [1800]


@pytest.mark.parametrize("error", [SystemExit(2), KeyboardInterrupt()])
def test_guest_process_control_exceptions_propagate(probe, monkeypatch, error):
    module, records, _ = probe
    monkeypatch.setattr(module, "version", lambda _: "0.7.0")

    def import_module(_):
        raise error

    monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=import_module))
    with pytest.raises(type(error)):
        module.guest()
    assert not any(record.get("status") == "error" for record in records)


def test_guest_operational_errors_remain_observations(probe, monkeypatch):
    module, records, _ = probe
    monkeypatch.setattr(module, "version", lambda _: "0.7.0")

    def import_module(_):
        raise RuntimeError("SDK could not initialize")

    monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=import_module))
    module.guest()
    assert records[-1]["stage"] == "sdk_import"
    assert records[-1]["error_type"] == "RuntimeError"
