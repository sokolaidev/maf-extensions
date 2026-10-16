"""Regression controls for bounded output and exact best-effort reclamation."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
runner = importlib.import_module("openclaw_conformance.__main__")
resources = importlib.import_module("openclaw_conformance.resources")


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize(
    "relative",
    ["gateway/gateway.log", "gateway/worker.log", "gateway/provider.jsonl", "gateway.log"],
)
def test_auxiliary_output_is_bounded_even_when_child_exits_between_polls(
    tmp_path, monkeypatch, finished, relative
):
    class Processes:
        stopped = False

        def __init__(self, root):
            polls = iter([0] if finished else [None, 0])
            self.children = [(SimpleNamespace(poll=lambda: next(polls, 0), returncode=0), None)]

        def start(self, *args):
            (tmp_path / "gateway.log").write_bytes(b"")
            (tmp_path / "gateway").mkdir(exist_ok=True)
            (tmp_path / "gateway/observations.json").write_text("{}")
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 33)

        def close(self):
            self.stopped = True
            return []

    fake_agent = ModuleType("agent")
    setattr(fake_agent, "Processes", Processes)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(runner.LIMITS, "log", 32)
    monkeypatch.setitem(runner.LIMITS, "events", 32)
    with pytest.raises(runner.Refused, match="stage_log_limit"):
        runner.stage("gateway", tmp_path, tmp_path, tmp_path / "candidate.json")


@pytest.mark.parametrize("failure", ["sentinel", "observe", "remove", "owner"])
def test_cleanup_keeps_attempting_all_scopes_and_control_resources(tmp_path, monkeypatch, failure):
    expected = resources.inventory("a" * 32)
    owners = ["1" * 32, "2" * 32, "3" * 32, "4" * 32]
    for relative, owner in zip(
        (
            "ownership/owner/owner",
            "gateway/owner/owner",
            "policy/refused-owner/owner",
            "policy/input-owner/owner",
        ),
        owners,
        strict=True,
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text("bad" if failure == "owner" and owner == owners[0] else owner)
    calls = []
    live = {"owned-" + o for o in owners} | set(expected["names"].values())
    unrelated = "unrelated-container"
    live.add(unrelated)

    def command(args, *rest):
        calls.append(args)
        if args[1] == "inspect":
            if failure == "sentinel":
                raise OSError("unavailable")
            return "true"
        if args[1:3] == ["rm", "-f"]:
            if failure == "remove" and args[-1] == "owned-" + owners[0]:
                raise OSError("refused")
            live.discard(args[-1])
            return ""
        label = next((a for a in args if a.startswith("label=maf-sandbox.scope=")), None)
        if label:
            owner = label.rsplit("-", 1)[-1]
            if failure == "observe" and owner == owners[0]:
                raise OSError("unavailable")
            target = "owned-" + owner
        else:
            target = next(a for a in args if a.startswith("name=^/"))[7:-1]
        return target if target in live else ""

    monkeypatch.setattr(runner, "command", command)
    result = runner.cleanup(tmp_path, tmp_path, expected, False)
    assert result["errors"]
    assert result["processes_stopped"] is False
    assert result["emergency_cleanup"] is True
    assert unrelated in live
    assert all(name not in live for name in expected["names"].values())
    assert all("owned-" + owner not in live for owner in owners[1:])
    assert any(
        args[1:3] == ["rm", "-f"] and args[-1] == expected["names"]["sentinel"] for args in calls
    )


def test_control_inventory_is_persisted_before_docker_creation(tmp_path, monkeypatch):
    m = importlib.import_module("test_openclaw_conformance").manifest()
    path = tmp_path / "candidate.json"
    runner.write(path, m)
    output = tmp_path / "campaign"
    monkeypatch.setattr(runner, "check_prepared", lambda *args: None)
    observed = []

    def command(args, *rest):
        if args[1] == "run":
            saved = runner.read(output / "resources.json")
            assert args[args.index("--name") + 1] == saved["names"]["sentinel"]
            observed.append(saved)
        raise OSError("Docker command interrupted")

    monkeypatch.setattr(runner, "command", command)
    assert runner.run(tmp_path / "prepared", path, output)["outcome"] == "failed"
    assert len(observed) == 1
    summary = runner.read(output / "evidence/summary.json")
    assert [r["group"] for r in summary["attempts"]] == ["cleanup"]
