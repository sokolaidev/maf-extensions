"""A stable boundary session cannot substitute for fresh physical or report evidence."""

import importlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SESSION = "48c7e51d-4460-f896-3321-deec9fb17225"


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_restart_probe")


def status(network, session, process=SESSION, sequence=1):
    return NS(
        network_instance_id=network,
        observed=NS(session_id=session, process_instance_id=process, sequence=sequence),
    )


@pytest.mark.parametrize(
    "fault", [None, "network", "session", "process", "empty", "cached", "changed"]
)
def test_descriptor_bound_authority_still_requires_fresh_reports(probe, fault):
    before = status("old-network", "old-session")
    accepted = status("new-network", "new-session")
    current = status("new-network", "new-session", sequence=2)
    if fault == "network":
        current.network_instance_id = "old-network"
    elif fault == "session":
        current.observed.session_id = "old-session"
    elif fault in ("process", "empty"):
        current.observed.process_instance_id = "wrong" if fault == "process" else ""
    elif fault == "cached":
        current.observed.sequence = 1
    elif fault == "changed":
        current.observed.session_id = "third-session"
    assert probe.fresh_authority(before, current, accepted, boundary_session=SESSION) is (
        fault is None
    )
    assert not probe.fresh_authority(before, current, accepted)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "boundary",
        "container",
        "generation",
        "session",
        "role",
        "stopped",
        "symlink",
        "name",
        "extra",
        "oversize",
        "http",
    ],
)
def test_descriptor_read_is_bounded_and_binds_the_owned_runtime(probe, monkeypatch, fault):
    runtime = {
        "containers": [
            {
                "Id": role,
                "State": "running",
                "Labels": {"openshell.ai/isolation-role": role, "openshell.ai/sandbox-id": "owned"},
            }
            for role in ("sandbox", "supervisor")
        ]
    }
    descriptor = {
        "boundary_id": "owned",
        "generation": "generation",
        "session_id": SESSION,
        "resource_claims": {"docker.container_id": "sandbox"},
        "tls": {"trust_anchor_pem": "public"},
    }
    if fault == "boundary":
        descriptor["boundary_id"] = "other"
    if fault == "container":
        descriptor["resource_claims"]["docker.container_id"] = "other"
    if fault == "generation":
        descriptor["generation"] = ""
    if fault == "session":
        descriptor["session_id"] = "not-a-uuid"
    if fault == "role":
        runtime["containers"].pop()
    if fault == "stopped":
        runtime["containers"][0]["State"] = "exited"
    payload = json.dumps(descriptor).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        entry = tarfile.TarInfo("wrong.json" if fault == "name" else "runtime-descriptor.json")
        entry.size = len(payload)
        if fault == "symlink":
            entry.type = tarfile.SYMTYPE
        tar.addfile(entry, io.BytesIO(payload))
        if fault == "extra":
            tar.addfile(tarfile.TarInfo("extra"))
    raw = b"x" * 1048577 if fault == "oversize" else archive.getvalue()
    requests, closed = [], []
    monkeypatch.setattr(
        probe,
        "DockerConnection",
        lambda *args, **kwargs: NS(
            request=lambda *args: requests.append(args),
            getresponse=lambda: NS(
                status=404 if fault == "http" else 200, read=lambda size: raw[:size]
            ),
            close=lambda: closed.append(True),
        ),
    )
    if fault:
        with pytest.raises((RuntimeError, ValueError)):
            probe.boundary_descriptor("owned", runtime)
    else:
        result = probe.boundary_descriptor("owned", runtime)
        assert result["session_id"] == SESSION
        assert result["resource_claims"]["docker.container_id"] == "sandbox"
        assert "tls" not in result
        assert len(result["sha256"]) == 64
    if requests:
        assert requests[0][0] == "GET"
        assert "/containers/supervisor/archive?" in requests[0][1]
        assert closed == [True]


@pytest.mark.parametrize("missing", ["original_supervisor_absent", "original_processes_retired"])
def test_recovery_refuses_boundary_identity_without_physical_retirement(
    probe, monkeypatch, missing
):
    transition = probe.RestartAuthority(
        NS(
            client=NS(expected_version="0.1.7"),
            owners=[{"name": "owned", "sandbox_id": "physical"}],
            save=lambda: None,
        ),
        NS(),
        "10.0.0.2",
        lambda *args: None,
    )
    before = status("old", "old")
    before.SerializeToString = lambda: b"before"
    row = {
        "runtime_continuity": False,
        "before": {},
        "after": {},
        "after_spec_hex": "spec",
        "original_supervisor_absent": True,
        "original_processes_retired": True,
    }
    row[missing] = False
    monkeypatch.setattr(probe, "snapshot", lambda *args: {})
    monkeypatch.setattr(probe, "boundary_descriptor", lambda *args: {"session_id": SESSION})
    monkeypatch.setattr(transition, "restart_runtime", lambda *args: (row, before))
    monkeypatch.setattr(transition, "stable", lambda *args: None)
    with pytest.raises(RuntimeError, match="verified physical replacement"):
        transition.recover(None, None, True)


def test_attestation_rejects_a_changed_descriptor_before_traffic(probe, monkeypatch):
    transition = probe.RestartAuthority(
        NS(client=NS(), owners=[{"name": "owned", "sandbox_id": "physical"}]),
        NS(),
        "10.0.0.2",
        lambda *args: None,
    )
    current = status("new-network", "new-session")
    current.SerializeToString = lambda: b"status"
    monkeypatch.setattr(transition, "stable", lambda *args: None)
    monkeypatch.setattr(transition, "wait_provider", lambda *args: current)
    monkeypatch.setattr(
        probe, "boundary_descriptor", lambda *args: {"session_id": SESSION, "sha256": "changed"}
    )
    with pytest.raises(RuntimeError, match="boundary descriptor changed"):
        transition.attest(
            None,
            True,
            status("old-network", "old-session"),
            {},
            "spec",
            boundary={"session_id": SESSION, "sha256": "original"},
        )
