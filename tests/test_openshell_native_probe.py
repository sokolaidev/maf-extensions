"""Native-host receipts, bounded file reads and failure cleanup must fail closed."""

from __future__ import annotations

import asyncio
import importlib
import io
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from maf_sandbox import EntryKind, SandboxEntry, SandboxTransferCapExceeded


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_native_probe"), importlib.import_module(
        "openshell_conformance"
    )


@pytest.mark.parametrize(
    "system,kernel,os_type,engine_os",
    [
        ("Windows", "10", "linux", "Ubuntu"),
        ("Linux", "6.8-microsoft-standard-WSL2", "linux", "Ubuntu"),
        ("Linux", "6.8", "linux", "Docker Desktop"),
        ("Linux", "6.8-linuxkit", "linux", "Linux"),
        ("Linux", "6.8", "windows", "Windows"),
    ],
)
def test_non_native_receipts_refused(modules, system, kernel, os_type, engine_os):
    probe, _ = modules
    with pytest.raises(RuntimeError, match="native Linux"):
        probe.native_host(system, kernel, {"OSType": os_type, "OperatingSystem": engine_os})


def test_native_vm_is_not_confused_with_desktop(modules):
    modules[0].native_host(
        "Linux",
        "6.11.0-azure",
        {"OSType": "linux", "OperatingSystem": "Ubuntu 24.04", "KernelVersion": "6.11.0-azure"},
    )


def test_skips_and_failures_are_not_passes(modules):
    from maf_sandbox.conformance import Probe, ProbeResult

    async def noop(*args):
        pass

    p = Probe("example", "test", frozenset(), noop)
    rows = modules[0].summarize(
        (ProbeResult(p), ProbeResult(p, skipped="unsupported"), ProbeResult(p, failure="escaped"))
    )
    assert [r["passed"] for r in rows] == [True, False, False]
    assert rows[2]["failure"] == "escaped"


@pytest.fixture
def candidate(modules):
    async def dispose():
        pass

    return modules[1].Candidate(
        SimpleNamespace(), {"name": "s1797-aaaaaaaaaaaa", "sandbox_id": "original"}, dispose
    )


@pytest.mark.parametrize(
    "path,cwd",
    [
        ("../escape", "/sandbox/work"),
        ("/sandbox/work-other/file", "/sandbox/work"),
        ("file", "../outside"),
        ("a\\b", "/sandbox/work"),
    ],
)
def test_lexical_escape_refused_before_sftp(candidate, path, cwd):
    def forbidden():
        pytest.fail("SFTP opened for an escaping path")

    candidate.sftp = forbidden
    with pytest.raises(ValueError):
        asyncio.run(candidate.read_file(path, working_directory=cwd, max_bytes=10))


@pytest.mark.parametrize("kind", [EntryKind.SYMLINK, EntryKind.DIRECTORY, EntryKind.OTHER])
def test_nonregular_file_never_opened(candidate, kind):
    async def raw_stat(path):
        return SandboxEntry(path, kind if path.endswith("file") else EntryKind.DIRECTORY, None)

    candidate.raw_stat = raw_stat
    candidate.sftp = lambda: pytest.fail("nonregular file opened")
    with pytest.raises(OSError):
        asyncio.run(candidate.read_file("file", working_directory="/sandbox", max_bytes=10))


def test_growth_after_stat_is_bounded_and_refused(candidate):
    requested = []

    async def raw_stat(path):
        return SandboxEntry(
            path, EntryKind.FILE if path.endswith("file") else EntryKind.DIRECTORY, 2
        )

    class Stream(io.BytesIO):
        def read(self, size: int | None = -1):
            requested.append(size)
            return super().read(size)

    @contextmanager
    def sftp():
        yield SimpleNamespace(open=lambda *args: Stream(b"x" * 1000))

    candidate.raw_stat, candidate.sftp = raw_stat, sftp
    with pytest.raises(SandboxTransferCapExceeded):
        asyncio.run(candidate.read_file("file", working_directory="/sandbox", max_bytes=10))
    assert requested == [11]


def test_leaf_metadata_retains_link_kind(modules):
    assert modules[1].entry(
        "link", SimpleNamespace(st_mode=stat.S_IFLNK, st_size=123)
    ) == SandboxEntry("link", EntryKind.SYMLINK, None)


@pytest.mark.parametrize("failure", ["overflow", "terminal", "missing_exit", "deadline", "cancel"])
def test_exec_failure_disposes_and_does_not_return_success(
    candidate, modules, monkeypatch, failure
):
    module = modules[1]
    events = []

    class Call:
        def cancel(self):
            events.append("cancel-stream")

        async def __aiter__(self):
            if failure in ("deadline", "cancel"):
                await asyncio.sleep(10)
            if failure == "overflow":
                yield SimpleNamespace(kind="stdout", data=b"x" * 5)
            if failure == "terminal":
                yield SimpleNamespace(kind="exit", exit_code=0)
                yield SimpleNamespace(kind="stdout", data=b"late")

    class Channel:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("close-channel")

    def imports(name):
        if name == "grpc":
            return SimpleNamespace(aio=SimpleNamespace(insecure_channel=lambda *a, **k: Channel()))
        return SimpleNamespace(
            OpenShellStub=lambda c: SimpleNamespace(ExecSandbox=lambda *a, **k: Call())
        )

    monkeypatch.setattr(module, "import_module", imports)
    monkeypatch.setattr(module, "RawStream", lambda call: call)
    candidate.client = SimpleNamespace(
        scope="workspace", token="synthetic", pb=SimpleNamespace(ExecSandboxRequest=lambda **kw: kw)
    )
    candidate.output_limit = 4

    async def dispose():
        events.append("dispose")
        await asyncio.sleep(0)

    candidate.dispose = dispose

    async def run():
        task = asyncio.create_task(
            candidate.exec(
                ["true"], working_directory="/sandbox", timeout=0.01 if failure == "deadline" else 1
            )
        )
        if failure == "cancel":
            await asyncio.sleep(0)
            task.cancel()
        await task

    expected = {
        "overflow": module.OutputLimitError,
        "terminal": RuntimeError,
        "missing_exit": RuntimeError,
        "deadline": TimeoutError,
        "cancel": asyncio.CancelledError,
    }[failure]
    with pytest.raises(expected):
        asyncio.run(run())
    assert events == ["cancel-stream", "close-channel", "dispose"]


def test_disposal_failure_retains_physical_ownership(modules, monkeypatch, tmp_path):
    probe, _ = modules
    owner = {"sandbox_id": "original", "containers": ["container"], "processes": {"123": "1"}}
    fixture = probe.Fixture(SimpleNamespace(), {"owners": [owner]}, tmp_path / "report.json")
    monkeypatch.setattr(probe, "snapshot", lambda identity: {"containers": [], "processes": {}})

    def failed(*args):
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(probe, "dispose_fixture", failed)
    with pytest.raises(RuntimeError, match="observer unavailable"):
        asyncio.run(fixture.dispose(owner))
    assert fixture.disposed == set()
    assert "original" in fixture.report_path.read_text()


def test_create_identity_is_saved_before_readiness_failure(modules, tmp_path):
    probe, _ = modules
    request = SimpleNamespace(
        spec=SimpleNamespace(
            template=SimpleNamespace(labels={}),
            policy=SimpleNamespace(SerializeToString=lambda: b"policy"),
        ),
        SerializeToString=lambda: b"request",
    )

    def send(payload, *, on_created):
        on_created({"sandbox_id": "original", "containers": [], "processes": {}})
        raise RuntimeError("readiness failed")

    client = SimpleNamespace(
        scope=SimpleNamespace(workspace="workspace"),
        pb=SimpleNamespace(
            CreateSandboxRequest=SimpleNamespace(FromString=lambda payload: request)
        ),
        request=lambda *args: b"request",
        send=send,
    )
    fixture = probe.Fixture(client, {}, tmp_path / "report.json")
    with pytest.raises(RuntimeError, match="readiness failed"):
        asyncio.run(fixture.acquire())
    assert len(fixture.owners) == 1
    assert "original" in fixture.report_path.read_text()
    assert fixture.owners[0]["key"]["call_id"]


@pytest.mark.workflow
def test_workflow_retains_evidence_on_failure():
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/openshell-native.yml").read_text()
    )
    job = workflow["jobs"]["native"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    upload = next(
        s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["if"] == "always()"
    measure = next(s for s in job["steps"] if "openshell_native_probe.py" in s.get("run", ""))
    assert "set -euo pipefail" in measure["run"]
    assert "--with-editable ./packages/maf-sandbox" in measure["run"]
