"""Asset read retries preserve evidence bytes and expose failures without publishing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from container_release_assets import Evidence  # noqa: E402
from container_release_history import GitHub, sha256  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def delays(monkeypatch):
    values = []
    monkeypatch.setattr("container_release_history.time.sleep", values.append)
    return values


@pytest.mark.parametrize(
    "diagnostic",
    [
        b"gh: Request Timeout (HTTP 408)",
        b"gh: Too Many Requests (HTTP 429)",
        b"gh: Internal Server Error (HTTP 500)",
        b"gh: Bad Gateway (HTTP 502)",
        b"gh: Service Unavailable (HTTP 503)",
        b"unexpected EOF",
        b"read: connection reset by peer",
        b"net/http: TLS handshake timeout",
        b"error connecting to api.github.com",
        None,
    ],
)
def test_transient_download_restarts_from_empty_file(
    monkeypatch, tmp_path, delays, capsys, diagnostic
):
    calls = []

    def run(command, **kwargs):
        output = kwargs["stdout"]
        assert output.tell() == 0
        assert Path(output.name).stat().st_size == 0
        assert command[-4:] == [
            "-H",
            "Accept: application/octet-stream",
            "-H",
            "Cache-Control: no-cache",
        ]
        calls.append(command)
        if len(calls) == 1:
            output.write(b"partial output longer than the successful download")
            output.flush()
            if diagnostic is None:
                raise subprocess.TimeoutExpired(command, 600, stderr=b"timed out")
            raise subprocess.CalledProcessError(1, command, stderr=diagnostic)
        output.write(b"complete")

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    destination = tmp_path / "asset"
    GitHub().download_file(123, destination)
    assert destination.read_bytes() == b"complete"
    assert len(calls) == 2
    assert delays == [1]
    assert "Release asset 123 download attempt 1/3" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", [False, True])
def test_exhausted_download_cleans_partial_file_and_retains_diagnostic(
    monkeypatch, tmp_path, delays, timeout
):
    failures = []

    def run(command, **kwargs):
        kwargs["stdout"].write(b"partial")
        error = (
            subprocess.TimeoutExpired(command, 600, stderr=b"timed out")
            if timeout
            else subprocess.CalledProcessError(1, command, stderr=b"gh: Bad Gateway (HTTP 502)")
        )
        failures.append(error)
        raise error

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    destination = tmp_path / "asset"
    with pytest.raises((subprocess.CalledProcessError, subprocess.TimeoutExpired)) as caught:
        GitHub().download_file(123, destination)
    assert caught.value is failures[-1]
    assert len(failures) == 3
    assert delays == [1, 2]
    assert not destination.exists()
    assert "Release asset 123 download attempt 3/3" in caught.value.__notes__[0]
    assert ("timed out" if timeout else "HTTP 502") in caught.value.__notes__[0]


@pytest.mark.parametrize(
    "diagnostic, returncode",
    [
        (b"gh: Bad credentials (HTTP 401)", 1),
        (b"gh: Forbidden (HTTP 403)", 1),
        (b"gh: Not Found (HTTP 404)", 1),
        (b"gh: Unprocessable Entity (HTTP 422)", 1),
        (b"unknown failure", 1),
        (b"authentication required", 4),
        (b"gh: Forbidden (HTTP 403) connection reset", 1),
        (b"invalid \xff" + b"x" * 5000, 1),
    ],
)
def test_permanent_failure_is_not_retried(monkeypatch, tmp_path, delays, diagnostic, returncode):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write(b"partial")
        raise subprocess.CalledProcessError(returncode, command, stderr=diagnostic)

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    destination = tmp_path / "asset"
    with pytest.raises(subprocess.CalledProcessError) as caught:
        GitHub().download_file(123, destination)
    assert len(calls) == 1
    assert delays == []
    assert not destination.exists()
    assert caught.value.__notes__ == [
        "Release asset 123 download attempt 1/3: "
        + diagnostic.decode("utf-8", errors="replace")[:4096]
    ]


def test_existing_destination_is_preserved(monkeypatch, tmp_path, delays):
    destination = tmp_path / "asset"
    destination.write_bytes(b"existing evidence")
    monkeypatch.setattr(
        "container_release_history.subprocess.run", lambda *a, **kw: pytest.fail("download")
    )
    with pytest.raises(FileExistsError):
        GitHub().download_file(123, destination)
    assert destination.read_bytes() == b"existing evidence"
    assert delays == []


def test_launch_failure_cleans_destination_without_retry(monkeypatch, tmp_path, delays):
    def run(*args, **kwargs):
        raise FileNotFoundError("gh unavailable")

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    destination = tmp_path / "asset"
    with pytest.raises(FileNotFoundError, match="gh unavailable"):
        GitHub().download_file(123, destination)
    assert not destination.exists()
    assert delays == []


@pytest.mark.parametrize("body", [b"wrong", b"cut"])
def test_successful_transport_still_requires_evidence_hash(monkeypatch, tmp_path, delays, body):
    def run(command, **kwargs):
        kwargs["stdout"].write(body)

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    expected = sha256(b"valid")
    asset = {"id": 123, "state": "uploaded", "size": 5, "digest": expected}
    with pytest.raises(ValueError, match="Durable evidence differs"):
        Evidence().download(asset, tmp_path / "asset", expected)
    assert delays == []
