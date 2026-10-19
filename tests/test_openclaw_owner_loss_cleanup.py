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
