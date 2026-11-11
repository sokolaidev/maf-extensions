"""The numbered launcher must verify the published wheel before starting the harness."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "openclaw_published_sample", ROOT / "samples/24_openclaw_bicep/agent.py"
)
assert SPEC and SPEC.loader
sample = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT / "samples/24_openclaw_bicep"))
try:
    SPEC.loader.exec_module(sample)
finally:
    sys.path.pop(0)


@pytest.mark.parametrize(
    "status,payload", [(302, b"wheel"), (404, b""), (200, b"wrong"), (200, b"x" * 33)]
)
def test_download_refuses_redirect_error_wrong_hash_and_oversize(
    tmp_path, monkeypatch, status, payload
):
    closed = []
    response = SimpleNamespace(status=status, read=io.BytesIO(payload).read)
    connection = SimpleNamespace(
        request=lambda *args: None,
        getresponse=lambda: response,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(sample.http.client, "HTTPSConnection", lambda *a, **k: connection)
    monkeypatch.setattr(sample, "MAX_WHEEL_BYTES", 32)
    monkeypatch.setattr(sample, "WHEEL_SHA256", hashlib.sha256(b"wheel").hexdigest())
    with pytest.raises(RuntimeError):
        sample.download(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert closed == [True]


def test_download_uses_fixed_tls_host_and_verifies_bytes(tmp_path, monkeypatch):
    observed = []
    connection = SimpleNamespace(
        request=lambda *args: observed.append(args),
        getresponse=lambda: SimpleNamespace(status=200, read=io.BytesIO(b"wheel").read),
        close=lambda: observed.append("closed"),
    )

    def connect(host, *, timeout):
        assert host == "files.pythonhosted.org" and timeout == 30
        return connection

    monkeypatch.setattr(sample.http.client, "HTTPSConnection", connect)
    monkeypatch.setattr(sample, "WHEEL_SHA256", hashlib.sha256(b"wheel").hexdigest())
    wheel = sample.download(tmp_path)
    assert wheel.name == sample.WHEEL and wheel.read_bytes() == b"wheel"
    assert observed == [("GET", sample.WHEEL_PATH), "closed"]


def test_launch_passes_the_verified_wheel_and_restores_process_arguments(tmp_path, monkeypatch):
    before_argv, before_path = sys.argv, sys.path[:]
    wheel, output = tmp_path / sample.WHEEL, tmp_path / "output"

    def run(path, *, run_name):
        assert path == str(sample.HARNESS) and run_name == "__main__"
        args = sys.argv
        assert args[args.index("--wheel") + 1] == str(wheel)
        assert args[args.index("--output") + 1] == str(output)
        assert args[args.index("--mode") + 1] == "model"
        assert args[args.index("--reasoning-effort") + 1] == "none"
        assert args[args.index("--image") + 1] == sample.IMAGE
        assert sys.path[0] == str(sample.HARNESS.parent)
        raise SystemExit("failure")

    monkeypatch.setattr(sample.runpy, "run_path", run)
    with pytest.raises(SystemExit, match="failure"):
        sample.launch(wheel, output, "model")
    assert sys.argv is before_argv and sys.path == before_path


@pytest.mark.parametrize("output", [ROOT / "private-sample-state", ROOT])
def test_checkout_or_existing_output_is_refused_before_download(output, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent.py", "--output", str(output)])
    monkeypatch.setattr(sample, "download", lambda _: pytest.fail("download must not start"))
    with pytest.raises(SystemExit) as exc:
        sample.main()
    assert exc.value.code == 2


def test_main_uses_temporary_wheel_and_propagates_failed_cleanup(tmp_path, monkeypatch):
    events = []
    downloaded = []
    monkeypatch.setattr(sys, "argv", ["agent.py", "--output", str(tmp_path / "state")])
    monkeypatch.setattr(sample.shutil, "which", lambda name: name)

    def download(root):
        wheel = root / sample.WHEEL
        wheel.write_bytes(b"verified")
        downloaded.append(wheel)
        events.append("download")
        return wheel

    def pull(command, **kwargs):
        assert command == ["docker", "pull", sample.IMAGE]
        assert kwargs["check"] is True and kwargs["timeout"] == 300
        events.append("pull")

    def launch(wheel, output, mode):
        assert wheel.read_bytes() == b"verified" and output == tmp_path / "state"
        assert mode == "smoke"
        events.append("launch")
        raise SystemExit("cleanup unconfirmed")

    monkeypatch.setattr(sample, "download", download)
    monkeypatch.setattr(sample.subprocess, "run", pull)
    monkeypatch.setattr(sample, "launch", launch)
    with pytest.raises(SystemExit, match="cleanup unconfirmed"):
        sample.main()
    assert events == ["download", "pull", "launch"]
    assert not downloaded[0].exists()


def test_published_launcher_matches_qualified_worker_and_hosted_image():
    import yaml

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from openclaw_conformance import linux_owner_loss

        assert sample.WHEEL_SHA256 == linux_owner_loss.WHEEL
        assert (
            sample.WHEEL
            == f"maf_sandbox_mcp-{linux_owner_loss.VERSIONS['maf-sandbox-mcp']}-py3-none-any.whl"
        )
    finally:
        sys.path.pop(0)
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-live.yml").read_text())
    assert workflow["jobs"]["openclaw-bicep"]["env"]["BICEP_IMAGE"] == sample.IMAGE
