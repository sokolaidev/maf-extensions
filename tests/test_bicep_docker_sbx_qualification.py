"""SBX release checks refuse image drift, failed transfers and incomplete cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bicep_docker_sbx_qualification as sbx  # noqa: E402
import check_container_release_image as runtime  # noqa: E402
from test_bicep_qualification import report  # noqa: E402

pytestmark = pytest.mark.workflow
IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize("change", [None, "identity", "tag", "platform"])
def test_archive_binds_template_to_selected_image(tmp_path, change):
    raw = json.dumps(
        {"os": "linux", "architecture": "arm64" if change == "platform" else "amd64"}
    ).encode()
    image = "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest = [
        {"RepoTags": ["wrong" if change == "tag" else "candidate:local"], "Config": "config.json"}
    ]
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in (("manifest.json", json.dumps(manifest).encode()), ("config.json", raw)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    if change:
        with pytest.raises(ValueError):
            sbx.verify_archive(path, IMAGE if change == "identity" else image, "candidate:local")
    else:
        sbx.verify_archive(path, image, "candidate:local")


@pytest.mark.parametrize("value", [None, {}, {"sandboxes": None}, [None], [{"id": "unknown"}]])
def test_unknown_listing_never_proves_cleanup(monkeypatch, value):
    monkeypatch.setattr(sbx, "command", lambda *a: json.dumps(value))
    with pytest.raises(ValueError, match="sandbox list"):
        sbx.sandboxes("sbx")


@pytest.mark.parametrize("wrapped", [False, True])
def test_supported_listing_shapes(monkeypatch, wrapped):
    rows = [{"name": "probe", "workspaces": ["workspace"]}]
    monkeypatch.setattr(
        sbx, "command", lambda *a: json.dumps({"sandboxes": rows} if wrapped else rows)
    )
    assert sbx.sandboxes("sbx") == rows


@pytest.mark.parametrize(
    "failure", [None, "transfer", "compiler", "cleanup", "survivor", "files", "load", "archive"]
)
def test_backend_qualification_and_failure_cleanup(monkeypatch, tmp_path, failure):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sbx, "mkdtemp", lambda **kw: str(workspace))
    commands = []
    state = {"disposed": False, "acquired": False}
    data = report()
    if failure == "compiler":
        data["checks"][1]["exitCode"] = 0
    payload = json.dumps(data).encode()
    written = {}

    def command(*args, **kw):
        commands.append(args)
        if args[1:3] == ("template", "rm"):
            assert args[3] == "--force" and args[4].startswith("maf-bicep-qualification:")
        if args[1:3] == ("template", "load") and failure == "load":
            raise ValueError("load failed")
        return "sbx v0.48.0" if args[1:] == ("version",) else ""

    def archive(*args):
        if failure == "archive":
            raise ValueError("archive failed")
        assert args[1] == IMAGE

    def rows(*args):
        return (
            []
            if state["disposed"] and failure != "survivor"
            else [{"name": "probe", "workspaces": [str(workspace / "mount")]}]
        )

    class Sandbox:
        name = "probe"
        mount = SimpleNamespace(host=workspace / "mount")

        async def write_file(self, path, content, **kwargs):
            written[path] = content

        async def read_file(self, path, **kwargs):
            if path.endswith("output.txt"):
                return (
                    b"wrong"
                    if failure == "transfer"
                    else written["release-probe/input.txt"].encode()
                )
            return json.dumps(data["checks"][0]["template"]).encode()

        async def exec(self, command, **kwargs):
            assert "release-probe/input.txt" in written
            assert kwargs["working_directory"] == "." and kwargs["timeout"] <= 180
            if command == sbx.bicep.command():
                raw = payload
            elif "cp release-probe" in command[-1]:
                raw = written["release-probe/input.txt"].encode()
            else:
                raw = (
                    "bash 5\nunshare 2.40\n"
                    + hashlib.sha256((sbx.ROOT / "LICENSE").read_bytes()).hexdigest()
                    + "  LICENSE\n"
                ).encode()
            return SimpleNamespace(exit_code=0, stdout=raw.decode(), stdout_bytes=raw, stderr="")

    class Backend:
        def __init__(self, config):
            assert config.workspace_root == workspace

        async def acquire(self, key, spec):
            state["acquired"] = True
            assert spec.image.startswith("maf-bicep-qualification:")
            assert spec.egress.value == "closed"
            return Sandbox()

        async def dispose_scope(self, scope, thread):
            state["disposed"] = True
            if failure == "files":
                (workspace / "survivor").write_text("kept")
            return SimpleNamespace(undisposed=["failed"] if failure == "cleanup" else [])

    monkeypatch.setattr(sbx, "command", command)
    monkeypatch.setattr(sbx, "verify_archive", archive)
    monkeypatch.setattr(sbx, "sandboxes", rows)
    monkeypatch.setattr(sbx, "SbxSandboxBackend", Backend)
    if failure:
        with pytest.raises(ValueError):
            asyncio.run(sbx.check(IMAGE))
    else:
        result = asyncio.run(sbx.check(IMAGE))
        assert result["backend"] == "maf-sandbox-docker-sbx"
        assert result["configuration"] == "staged-through-sdk"
        assert result["checks"][2]["name"] == "staged-strict-config"
        assert result["template"]["imageId"] == IMAGE
        assert result["workspaceRoundTrip"] and result["compiledOutputTransfer"]
        assert result["sandboxesRemaining"] == [] and result["workspaceRemoved"]
        assert written["bicepconfig.json"] == (sbx.bicep.CONTEXT / "bicepconfig.json").read_bytes()
    assert state["disposed"]
    if failure in {"cleanup", "survivor", "files"}:
        assert workspace.exists()
        assert not any(args[1:3] == ("template", "rm") for args in commands)
    else:
        assert not workspace.exists()
        assert any(args[1:3] == ("template", "rm") for args in commands) is (
            failure not in {"load", "archive"}
        )
        assert any(args[1:3] == ("image", "rm") for args in commands)
    if failure in {"load", "archive"}:
        assert not state["acquired"]


def test_release_dispatch_uses_sbx_evidence(monkeypatch, tmp_path):
    async def check(image_id):
        assert image_id == IMAGE
        return {"sandboxesRemaining": [], "build": {"route": "upstream"}}

    monkeypatch.setattr(sbx, "check", check)
    result = runtime.check("bicep-docker-sbx", IMAGE, tmp_path)
    assert result["kind"] == "docker-sbx-consumer"
    assert result["bicep"]["build"]["route"] == "upstream"
    assert result["liveDeploymentVerified"] is False


def test_release_and_scheduled_hosts_prepare_sbx_without_pr_builds():
    root = Path(__file__).resolve().parents[1]
    for filename, jobs, expression in (
        ("container-image-release.yml", ["prepare", "qualify"], "inputs.profile"),
        ("image-security.yml", ["scan"], "matrix.profile"),
    ):
        workflow = yaml.safe_load((root / ".github/workflows" / filename).read_text())
        assert "pull_request" not in workflow[True]
        for name in jobs:
            job = workflow["jobs"][name]
            assert "ubuntu-24.04" in job["runs-on"]
            [setup] = [
                step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-sbx"
            ]
            assert setup["if"].startswith(expression + " == 'bicep-docker-sbx'")
            assert setup["with"]["docker-pat"] == "${{ secrets.DOCKER_PAT }}"
