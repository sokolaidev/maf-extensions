"""Shell release evidence refuses wrong boundaries, results and incomplete cleanup."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agent_framework import Content
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_container_release_image as runtime  # noqa: E402
import shell_qualification as shell  # noqa: E402
from container_release import ROOT  # noqa: E402
from container_release_approval import UNAVAILABLE, evidence_rows  # noqa: E402
from select_image_security import affected  # noqa: E402

IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "fault", [None, "image", "reference", "network", "extra-network", "stopped", "missing"]
)
def test_engine_observation_requires_selected_closed_container(monkeypatch, fault):
    value = {
        "Image": IMAGE,
        "Config": {"Image": IMAGE},
        "State": {"Running": fault != "stopped"},
        "HostConfig": {"NetworkMode": "bridge" if fault == "network" else "none"},
        "NetworkSettings": {"Networks": {"none": {}}},
    }
    if fault == "image":
        value["Image"] = "other"
    if fault == "reference":
        value["Config"]["Image"] = "other"
    if fault == "extra-network":
        value["NetworkSettings"]["Networks"]["bridge"] = {}
    monkeypatch.setattr(shell, "owned", lambda _: [] if fault == "missing" else ["container"])
    monkeypatch.setattr(shell, "docker", lambda *args: json.dumps([value]))
    if fault:
        with pytest.raises(ValueError):
            shell.observation("scope", IMAGE)
    else:
        assert shell.observation("scope", IMAGE)["network"] == "none"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "completion",
        "verdict",
        "syntax-cause",
        "suppression",
        "store",
        "unclean",
        "leftover",
        "purge",
        "no-observation",
        "exception",
        "devices",
    ],
)
def test_public_tools_require_each_result_and_cleanup(monkeypatch, fault):
    live = []
    purges = []
    observer = None
    case_index = 0

    class Router:
        def __init__(self, *args, **kwargs):
            nonlocal observer
            observer = kwargs["observer"]

        async def acquire(self, *args, **kwargs):
            live.append("container")
            return None

        async def dispose_scope(self, *args):
            purges.append(list(live))
            live.clear()
            return SimpleNamespace(undisposed=("container",) if fault == "purge" else ())

    def tools(router, store, *args, **kwargs):
        async def validate(files):
            nonlocal case_index
            _flavor, case, _path, _code, verdict = shell.CASES[case_index]
            case_index += 1
            if fault != "no-observation":
                await router.acquire(None, None)
            if fault == "exception":
                raise ValueError("analysis failed")
            texts = [COMPLETED_TEXT if verdict is not None else NOT_COMPLETED_TEXT]
            if verdict is not None:
                texts.append(f"Result: {verdict}")
            elif fault != "syntax-cause":
                texts.append(
                    "Validation incomplete: syntax, lint or suppression auditing could not complete."
                )
            if fault == "completion":
                texts[0] = NOT_COMPLETED_TEXT
            if fault == "verdict":
                texts.append("Result: clean")
            if case == "suppression" and fault != "suppression":
                texts.append("Inline suppressions are present; see the untrusted audit report.")
            if fault == "store":
                store.files.clear()
            observer.tool_call_ended(SimpleNamespace(unclean=int(fault == "unclean")))
            if fault != "leftover":
                live.clear()
            return [Content.from_text(text) for text in texts]

        return [SimpleNamespace(func=validate)]

    monkeypatch.setattr(
        shell, "docker", lambda *args: '{"Version":"test","Os":"linux","Arch":"amd64"}'
    )
    monkeypatch.setattr(
        shell, "check_image_layers", lambda _: {"layersChecked": 1, "deviceNodesPresent": False}
    )
    monkeypatch.setattr(shell, "owned", lambda _: list(live))
    monkeypatch.setattr(
        shell,
        "check_runtime_devices",
        AsyncMock(side_effect=ValueError("devices") if fault == "devices" else None),
    )
    monkeypatch.setattr(shell, "observation", lambda *args: {"imageId": IMAGE, "network": "none"})
    monkeypatch.setattr(shell, "SandboxRouter", Router)
    monkeypatch.setattr(shell.DockerSandboxBackend, "create", AsyncMock())
    monkeypatch.setattr(shell, "make_shell_tools", tools)
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(shell.check(IMAGE))
    else:
        result = asyncio.run(shell.check(IMAGE))
        assert len(result["checks"]) == len(result["observations"]) == len(shell.CASES)
        assert sum(not row["completed"] for row in result["checks"]) == 2
        assert result["sdkSource"] == "checkout"
        assert result["containersRemaining"] == []
        assert result["acasVerified"] is False
    assert len(purges) == 1
    if fault in {"leftover", "exception"}:
        assert purges == [["container"]]


@pytest.mark.parametrize("fault", [None, "fixtures", "survivor", "sdk"])
def test_release_hook_requires_fixtures_sdk_and_cleanup(monkeypatch, tmp_path, fault):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and fault == "survivor" else ""
        )

    async def output(*args, **kwargs):
        if fault == "fixtures":
            raise ValueError("fixtures failed")
        return b""

    result = {"checks": [{"name": "bash/clean", "passed": True}], "containersRemaining": []}
    probe = (
        AsyncMock(side_effect=ValueError("sdk failed"))
        if fault == "sdk"
        else AsyncMock(return_value=result)
    )
    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    monkeypatch.setattr(shell, "check", probe)
    if fault:
        with pytest.raises(ValueError):
            runtime.check("shell", IMAGE, tmp_path)
    else:
        report = runtime.check("shell", IMAGE, tmp_path)
        assert report["kind"] == "offline-sdk-consumer"
        assert report["shell"]["analyzerFixturesPassed"] is True
        assert report["liveDeploymentVerified"] is False
        rows = evidence_rows({"runtime.json": report}, {"profile": "shell", "imageId": IMAGE})
        assert ("Shell check", result["checks"][0]) in rows
    assert any(command[1:3] == ["rm", "--force"] for command in commands)
    if fault in {"fixtures", "survivor"}:
        probe.assert_not_awaited()
    else:
        probe.assert_awaited_once_with(IMAGE)


def test_missing_shell_evidence_is_unavailable():
    rows = evidence_rows({}, {"profile": "shell", "imageId": IMAGE})
    assert ("Shell check", UNAVAILABLE) in rows
    assert ("Shell analyzer fixtures passed", UNAVAILABLE) in rows
    assert ("Shell layer archive", UNAVAILABLE) in rows
    assert ("Shell ACAS verified", UNAVAILABLE) in rows


@pytest.mark.workflow
def test_candidate_scan_uses_the_same_shell_qualification():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["scan"]["steps"]
        if step.get("name") == "Qualify both public shell tools"
    )
    assert "matrix.profile == 'shell'" in step["if"]
    assert 'check("shell", os.environ["IMAGE_ID"], directory)' in step["run"]
    assert 'write(directory / "runtime.json"' in step["run"]
    assert affected("scripts/shell_qualification.py") == ("shell",)
    assert affected("scripts/shell_runtime_devices.py") == ("shell",)
