"""Independent fallback cleanup refuses scope ambiguity and retains failures."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
cleanup = importlib.import_module("openclaw_conformance.owner_loss_cleanup")
e = importlib.import_module("openclaw_conformance.evidence")


def test_invalid_owner_never_reaches_docker(tmp_path, monkeypatch):
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("caller-selected-scope")
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: pytest.fail("untrusted cleanup"))
    with pytest.raises(e.Refused, match="invalid_cleanup_owner"):
        cleanup.cleanup(tmp_path, "worker-active")


@pytest.mark.parametrize("field,bad", [("sentinel_name", "shared"), ("sentinel_scope", "shared")])
def test_invalid_sentinel_never_reaches_docker(tmp_path, monkeypatch, field, bad):
    value = {
        "sentinel_name": "oc-owner-loss-" + "a" * 32,
        "sentinel_scope": "owner-loss-sentinel-" + "b" * 32,
    }
    value[field] = bad
    e.write(tmp_path / "resources.json", value)
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: pytest.fail("untrusted cleanup"))
    with pytest.raises(e.Refused, match="invalid_sentinel_control"):
        cleanup.cleanup(tmp_path, "worker-active")


@pytest.mark.parametrize("changed", ["label", "id"])
def test_changed_resource_is_not_removed(tmp_path, monkeypatch, changed):
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)
    scope = "openclaw-bicep-" + "a" * 32
    identifier = "b" * 64
    called = []

    def docker(args, **kwargs):
        called.append(args)
        if args[1] == "ps":
            return SimpleNamespace(returncode=0, stdout=identifier)
        assert args[1] == "inspect"
        info = {
            "Id": "c" * 64 if changed == "id" else identifier,
            "Config": {"Labels": {"maf-sandbox.scope": "foreign" if changed == "label" else scope}},
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps([info]))

    monkeypatch.setattr(cleanup.subprocess, "run", docker)
    with pytest.raises(e.Refused, match="cleanup_scope_changed"):
        cleanup.cleanup(tmp_path, "worker-active")
    assert not any(args[1] == "rm" for args in called)


@pytest.mark.parametrize("failure", ["unavailable", "remaining"])
def test_cleanup_uncertainty_is_not_success(tmp_path, monkeypatch, failure):
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)

    def docker(args, **kwargs):
        if failure == "unavailable":
            return SimpleNamespace(returncode=1, stdout="")
        if args[1] == "ps":
            return SimpleNamespace(returncode=0, stdout="b" * 64)
        if args[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "b" * 64,
                            "Config": {
                                "Labels": {"maf-sandbox.scope": "openclaw-bicep-" + "a" * 32}
                            },
                        }
                    ]
                ),
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(cleanup.subprocess, "run", docker)
    with pytest.raises(e.Refused, match="independent_observer_unavailable|cleanup_survivor"):
        cleanup.cleanup(tmp_path, "worker-active")


def test_unconfirmed_process_tree_stops_campaign_even_if_docker_is_empty(tmp_path, monkeypatch):
    runner = importlib.import_module("openclaw_conformance.owner_loss")
    called = []

    class Processes:
        stopped = False

        def __init__(self, root):
            self.children = []

        def start(self, case, command, environment):
            called.append(case)
            self.children.append((SimpleNamespace(poll=lambda: 1, returncode=1), None))

        def close(self):
            return ["tree_not_empty"]

    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(Processes=Processes))
    monkeypatch.setattr(runner, "check_prepared", lambda *a: None)
    monkeypatch.setattr(runner, "plan", lambda *a: {"campaign_id": "a" * 32})
    monkeypatch.setattr(runner, "environment", lambda *a: {})
    monkeypatch.setattr(cleanup, "cleanup", lambda *a: {"confirmed": True, "remaining": []})
    output = tmp_path / "campaign"
    result = runner.run(tmp_path / "prepared", {}, {}, output)
    assert result["outcome"] == "failed"
    assert called == ["control"]
    artifact = e.read(output / "evidence/control.json")
    assert artifact["observations"]["independent_cleanup"]["confirmed"] is False
    assert artifact["observations"]["independent_cleanup"]["error"] == "process_tree_unconfirmed"


@pytest.mark.parametrize("when", ["exit", "shutdown", "cleanup"])
@pytest.mark.parametrize("name", ["control.log", "control/worker.log", "control/transport.jsonl"])
@pytest.mark.parametrize("oversized", [False, True])
def test_final_output_budget_includes_exit_shutdown_and_cleanup(
    tmp_path, monkeypatch, when, name, oversized
):
    runner = importlib.import_module("openclaw_conformance.owner_loss")
    output = tmp_path / "campaign"
    events = []
    monkeypatch.setitem(e.LIMITS, "log", 16)
    monkeypatch.setitem(e.LIMITS, "events", 2048)

    def emit():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        limit = e.LIMITS["events" if path.suffix == ".jsonl" else "log"]
        path.write_bytes(b"x" * (limit + int(oversized)))

    class Processes:
        stopped = True

        def __init__(self, root):
            self.children = []

        def start(self, case, command, environment):
            if when == "exit":
                emit()
            self.children.append((SimpleNamespace(poll=lambda: 0, returncode=0), None))

        def close(self):
            if when == "shutdown":
                emit()
            events.append("closed")
            return []

    def independent(*args):
        assert events == ["closed"]
        events.append("independent")
        if when == "cleanup":
            emit()
        return {"confirmed": True, "remaining": []}

    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(Processes=Processes))
    monkeypatch.setattr(runner, "CASES", ["control"])
    monkeypatch.setattr(runner, "check_prepared", lambda *a: None)
    monkeypatch.setattr(runner, "plan", lambda *a: {"campaign_id": "a" * 32})
    monkeypatch.setattr(runner, "environment", lambda *a: {})
    monkeypatch.setattr(runner, "verify", lambda *a: None)
    monkeypatch.setattr(runner, "evaluate", lambda *a, **k: {"outcome": "qualified_for_profile"})
    monkeypatch.setattr(cleanup, "cleanup", independent)
    result = runner.run(tmp_path / "prepared", {}, {}, output)
    assert events == ["closed", "independent"]
    assert result["outcome"] == ("failed" if oversized else "qualified_for_profile")
    artifact = e.read(output / "evidence/control.json")
    assert artifact["observations"]["parent_failure"] == ("stage_log_limit" if oversized else None)
    assert artifact["observations"]["independent_cleanup"]["confirmed"] is True


@pytest.mark.parametrize("file", ["observer", "transport", "provider", "client"])
@pytest.mark.parametrize(
    "bad",
    [b'{"event":"unfinished"}', b"\n", b'{"event":"x","event":"y"}\n', b'{"n":NaN}\n', b"[]\n"],
)
def test_final_owner_loss_trace_rejects_incomplete_or_ambiguous_records(tmp_path, file, bad):
    cases = importlib.import_module("openclaw_conformance.owner_loss_cases")
    for name in ("observer", "transport", "provider", "client"):
        (tmp_path / (name + ".jsonl")).write_bytes(b'{"event":"complete"}\n')
    with (tmp_path / (file + ".jsonl")).open("ab") as stream:
        stream.write(bad)
    with pytest.raises(RuntimeError):
        cases.observations(tmp_path, "client-active")


def test_final_owner_loss_trace_accepts_complete_records_and_absent_control_client(tmp_path):
    cases = importlib.import_module("openclaw_conformance.owner_loss_cases")
    for name in ("observer", "transport", "provider"):
        (tmp_path / (name + ".jsonl")).write_bytes(b'{"event":"complete"}\n')
    observed = cases.observations(tmp_path, "control")
    assert observed == {
        "events": [{"event": "complete"}],
        "transport": [{"event": "complete"}],
        "provider": [{"event": "complete"}],
        "client": [],
    }
    with pytest.raises(FileNotFoundError):
        cases.observations(tmp_path, "client-active")


@pytest.mark.parametrize("selection", ["default", "host", "context", "config"])
def test_independent_cleanup_queries_the_case_engine_not_parent_configuration(
    tmp_path, monkeypatch, selection
):
    install = importlib.import_module("openclaw_conformance.install")
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    selected = {
        "host": ("DOCKER_HOST", "unix:///case-engine.sock"),
        "context": ("DOCKER_CONTEXT", "case-context"),
        "config": ("DOCKER_CONFIG", str(tmp_path / "explicit-config")),
    }
    if selection in selected:
        monkeypatch.setenv(*selected[selection])
    monkeypatch.setenv("HOME", str(tmp_path / "parent-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "parent-profile"))
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", str(tmp_path / "parent-certs"))
    monkeypatch.setenv("DOCKER_CUSTOM_HEADERS", "X-Parent=unrelated")
    service_environment = install.environment(tmp_path)
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)
    identifier = "b" * 64
    removed = []
    queried = []

    def docker(args, **kwargs):
        queried.append(kwargs.get("env"))
        if kwargs.get("env") != service_environment:
            return SimpleNamespace(returncode=0, stdout="")
        if args[1] == "ps":
            return SimpleNamespace(returncode=0, stdout="" if removed else identifier)
        if args[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": identifier,
                            "Config": {
                                "Labels": {"maf-sandbox.scope": "openclaw-bicep-" + "a" * 32}
                            },
                        }
                    ]
                ),
            )
        assert args[1:] == ["rm", "-f", identifier]
        removed.append(identifier)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(cleanup.subprocess, "run", docker)
    result = cleanup.cleanup(tmp_path, "worker-active")
    assert result["confirmed"] is True
    assert result["removed"] == removed == [identifier]
    assert len(queried) == 4
    assert all(env == service_environment for env in queried)


@pytest.mark.parametrize(
    "phase,prior_failure",
    [
        ("start", False),
        ("poll", False),
        ("close", False),
        ("cleanup", False),
        ("logs", False),
        ("logs", True),
    ],
)
def test_operator_interrupt_retains_failure_and_never_starts_another_case(
    tmp_path, monkeypatch, phase, prior_failure
):
    runner = importlib.import_module("openclaw_conformance.owner_loss")
    started = []
    cleaned = []

    def interrupt(at):
        if phase == at:
            raise KeyboardInterrupt

    def poll():
        interrupt("poll")
        return 1 if prior_failure else 0

    class Processes:
        stopped = True

        def __init__(self, root):
            self.children = []

        def start(self, case, command, environment):
            started.append(case)
            interrupt("start")
            self.children.append(
                (SimpleNamespace(poll=poll, returncode=1 if prior_failure else 0), None)
            )

        def close(self):
            interrupt("close")
            return []

    def independent(root, case):
        cleaned.append(case)
        interrupt("cleanup")
        return {"confirmed": True, "remaining": []}

    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(Processes=Processes))
    monkeypatch.setattr(runner, "check_prepared", lambda *a: None)
    monkeypatch.setattr(runner, "plan", lambda *a: {"campaign_id": "a" * 32})
    monkeypatch.setattr(runner, "environment", lambda *a: {})
    monkeypatch.setattr(runner, "check_logs", lambda *a: interrupt("logs"))
    monkeypatch.setattr(runner, "verify", lambda *a: None)
    monkeypatch.setattr(cleanup, "cleanup", independent)
    output = tmp_path / "campaign"
    try:
        result = runner.run(tmp_path / "prepared", {}, {}, output)
    except KeyboardInterrupt:
        pytest.fail("Interruption escaped before failure evidence was retained")
    assert result["outcome"] == "failed"
    assert started == cleaned == ["control"]
    assert e.read(output / "failures.json") == [{"case": "control", "reason": "KeyboardInterrupt"}]
    artifact = e.read(output / "evidence/control.json")["observations"]
    assert artifact["parent_failure"] == "KeyboardInterrupt"
    assert artifact["independent_cleanup"]["confirmed"] is (phase not in {"close", "cleanup"})
    summary = e.read(output / "evidence/summary.json")
    assert summary["outcome"] == "failed"
    assert len(summary["attempts"]) == 1
    assert summary["attempts"][0]["outcome"] == "fail"
