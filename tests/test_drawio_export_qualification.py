"""Export release qualification rejects unsafe boundaries and incomplete evidence."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import drawio_export_qualification as export  # noqa: E402
from check_container_release_image import check  # noqa: E402

IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "fault",
    [None, "add", "drop", "privileged", "nnp", "memory", "cpus", "pids", "mount", "pid", "device"],
)
def test_export_requires_observed_outer_boundary(monkeypatch, fault):
    host = {
        "CapAdd": list(export.CAPABILITIES),
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges"],
        "Memory": 1024**3,
        "NanoCpus": 2_000_000_000,
        "PidsLimit": 256,
    }
    value = {"HostConfig": host, "Mounts": []}
    if fault in {"add", "drop"}:
        host["CapAdd" if fault == "add" else "CapDrop"] = []
    elif fault == "privileged":
        host["Privileged"] = True
    elif fault == "nnp":
        host["SecurityOpt"] = []
    elif fault in {"memory", "cpus", "pids"}:
        host[{"memory": "Memory", "cpus": "NanoCpus", "pids": "PidsLimit"}[fault]] = 0
    elif fault == "mount":
        value["Mounts"] = [{"Type": "bind", "Source": "/host", "Destination": "/data"}]
    elif fault == "pid":
        host["PidMode"] = "host"
    elif fault == "device":
        host["Devices"] = [{"PathOnHost": "/dev/kvm"}]
    monkeypatch.setattr(export.editable, "observation", lambda *_: {"containerId": "cid"})
    monkeypatch.setattr(export.editable, "docker", lambda *_: json.dumps([value]))
    if fault:
        with pytest.raises(ValueError):
            export.observation("scope", IMAGE)
    else:
        result = export.observation("scope", IMAGE)
        assert result["pidsLimit"] == 256
        assert result["capAdd"] == sorted(export.CAPABILITIES)


def test_export_dispatch_retains_native_report(monkeypatch, tmp_path):
    native = {"containersRemaining": [], "exports": {"renderer_identity": {"renderer_uid": 10001}}}
    probe = AsyncMock(return_value=native)
    monkeypatch.setattr(export, "check", probe)
    result = check("drawio-export", IMAGE, tmp_path)
    probe.assert_awaited_once_with(IMAGE)
    assert result["drawio"] == native
    assert result["kind"] == "offline-sdk-consumer" and result["passed"]


def test_export_dispatch_propagates_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(export, "check", AsyncMock(side_effect=ValueError("renderer mismatch")))
    with pytest.raises(ValueError, match="renderer mismatch"):
        check("drawio-export", IMAGE, tmp_path)


def test_export_mutable_identity_refused_before_editable_probe(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(export.editable, "check", probe)
    with pytest.raises(ValueError):
        asyncio.run(export.check("drawio-export:latest"))
    probe.assert_not_called()


def test_export_suite_rejects_optimized_python():
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "import asyncio,sys; from pathlib import Path; sys.path.insert(0,'scripts'); import check_drawio_exports as c; asyncio.run(c.check('unused',Path('unused')))",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode != 0
    assert "requires Python assertions" in result.stderr


@pytest.mark.parametrize(
    "fault", [None, "leftover", "purge", "artifact", "identity", "observation", "probe"]
)
def test_export_qualification_requires_complete_evidence(monkeypatch, fault):
    from types import SimpleNamespace

    from maf_sandbox import SandboxKey, SandboxSpec

    editable = {"packages": {}, "docker": {}, "versions": {}, "containersRemaining": []}
    monkeypatch.setattr(export.editable, "check", AsyncMock(return_value=editable))
    monkeypatch.setattr(export, "observation", lambda *_: {"containerId": "id"})
    monkeypatch.setattr(
        export.editable, "owned", lambda *_: ["leftover"] if fault == "leftover" else []
    )
    monkeypatch.setattr(export.DockerSandboxBackend, "_bind_daemon", AsyncMock())
    monkeypatch.setattr(
        export.DockerSandboxBackend,
        "_families_the_daemon_serves",
        AsyncMock(return_value=frozenset()),
    )
    sandbox = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(exit_code=int(fault == "probe"), stdout="{}"))
    )
    monkeypatch.setattr(export.DockerSandboxBackend, "acquire", AsyncMock(return_value=sandbox))
    purge = AsyncMock(return_value=SimpleNamespace(undisposed=("id",) if fault == "purge" else ()))
    monkeypatch.setattr(export.DockerSandboxBackend, "dispose_scope", purge)

    async def native(image, destination, backend, *, emit):
        assert image == IMAGE and emit is False
        for call in ("identity", "export", "export"):
            await backend.acquire(
                SandboxKey("scope", "thread", "agent", call_id=call),
                SandboxSpec(kind="probe", image=image),
            )
        names = ["diagram.drawio", "stored/diagram-2.png", "stored/diagram-2.jpg"]
        names += [f"diagram-{page}.{fmt}" for page in (1, 2) for fmt in ("png", "jpg", "svg")]
        if fault == "artifact":
            names.pop()
        for name in names:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"validated by native suite")
        return {
            "renderer_identity": {"caller_uid": 1 if fault == "identity" else 0},
            "clean_calls": 2 if fault == "observation" else 1,
        }

    monkeypatch.setattr(export.check_drawio_exports, "check", native)
    if fault:
        with pytest.raises(ValueError):
            asyncio.run(export.check(IMAGE))
    else:
        result = asyncio.run(export.check(IMAGE))
        assert len(result["artifacts"]) == 9
        assert result["containersRemaining"] == []
        assert result["editable"] == editable
    purge.assert_awaited_once_with("scope", "thread")


@pytest.mark.parametrize("present", [False, True])
def test_approval_reports_export_evidence_or_unavailable(present):
    from container_release_approval import UNAVAILABLE, evidence_rows

    candidate = {"profile": "drawio-export", "imageId": IMAGE}
    details = {
        "exports": {"clean_calls": 32},
        "editable": {"checks": ["layout"]},
        "artifacts": {"diagram-1.png": {"sha256": "a" * 64}},
        "coordinatorCapabilities": list(export.CAPABILITIES),
    }
    reports = {"runtime.json": {**candidate, "drawio": details}} if present else {}
    rows = dict(evidence_rows(reports, candidate))
    for label, field in (
        ("Native export and renderer checks", "exports"),
        ("Editable checks without capability grants", "editable"),
        ("Export artifacts (hashes and sizes)", "artifacts"),
        ("Required coordinator capabilities", "coordinatorCapabilities"),
    ):
        assert rows[label] == (details[field] if present else UNAVAILABLE)


@pytest.mark.parametrize("missing", [None, "electron", "draw.io", "dependency"])
def test_scan_must_include_every_bundled_application_package(missing):
    packages = [
        {"name": name, "version": "1.0.0"} for name in ("electron", "draw.io", "dependency")
    ]
    runtime = {"drawio": {"versions": {"packageInventory": packages}}}
    sbom = {"artifacts": [p for p in packages if p["name"] != missing]}
    if missing:
        with pytest.raises(ValueError, match="SBOM omits"):
            export.verify_inventory(sbom, runtime)
    else:
        export.verify_inventory(sbom, runtime)


def test_native_inventory_cannot_be_empty():
    with pytest.raises(ValueError, match="Missing native"):
        export.verify_inventory(
            {"artifacts": []}, {"drawio": {"versions": {"packageInventory": []}}}
        )


def test_assessment_refuses_os_only_sbom_before_scanning(monkeypatch, tmp_path):
    import prepare_container_release as prepare

    runtime = {
        "profile": "drawio-export",
        "drawio": {
            "versions": {
                "packageInventory": [
                    {"name": "electron", "version": "44.7.0"},
                    {"name": "draw.io", "version": "32.4.1"},
                ]
            }
        },
    }
    (tmp_path / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    (tmp_path / "sbom.syft.json").write_text('{"artifacts": []}', encoding="utf-8")
    commands = []
    monkeypatch.setattr(prepare, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(prepare, "verify_inventory", lambda *_: None)
    with pytest.raises(ValueError, match="SBOM omits"):
        prepare.assess(tmp_path, IMAGE)
    assert len(commands) == 1 and commands[0][0] == "syft"
