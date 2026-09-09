"""Exercise archive measurement cleanup with real child processes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import measure_docker_listing as measurement  # noqa: E402

_COPY = (
    "import sys, time; sys.stdout.buffer.write(b'x' * 131072); sys.stdout.flush(); time.sleep(60)"
)


@pytest.fixture
def slow_copy(monkeypatch):
    processes: list[subprocess.Popen[bytes]] = []

    def start(command: list[str], *, stdout: int, stderr: int) -> subprocess.Popen[bytes]:
        del command
        process = subprocess.Popen([sys.executable, "-c", _COPY], stdout=stdout, stderr=stderr)
        wait = process.wait
        communicate = process.communicate
        kill = process.kill
        killed = False

        def slow_wait(timeout: float | None = None) -> int:
            if killed and timeout is not None:
                raise subprocess.TimeoutExpired("copy", timeout)
            return wait(timeout=timeout)

        def slow_communicate(input: bytes | None = None, timeout: float | None = None):
            if killed and timeout is not None:
                raise subprocess.TimeoutExpired("copy", timeout)
            return communicate(input=input, timeout=timeout)

        def stop() -> None:
            nonlocal killed
            killed = True
            kill()

        process.wait = slow_wait
        process.communicate = slow_communicate
        process.kill = stop
        processes.append(process)
        return process

    monkeypatch.setattr(
        measurement, "subprocess", SimpleNamespace(Popen=start, PIPE=subprocess.PIPE)
    )
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_archive_cleanup_preserves_byte_ceiling_error(slow_copy):
    with pytest.raises(ValueError, match="archive byte ceiling exceeded"):
        measurement._archive("fixture", "/listing", ceiling=1024)

    (process,) = slow_copy
    assert process.returncode is not None
    assert process.stdout.closed
    assert process.stderr.closed


def test_early_stop_finishes_reaping_before_the_next_copy(monkeypatch, slow_copy):
    def next_copy(*args, **kwargs):
        del args, kwargs
        (process,) = slow_copy
        assert process.returncode is not None
        assert process.stdout.closed
        assert process.stderr.closed
        return {"seconds": 0.25}

    monkeypatch.setattr(measurement, "_archive", next_copy)

    result = measurement._stop_early("fixture")

    assert result["prefix_bytes"] == 65536
    assert result["next_copy_seconds"] == 0.25


@pytest.mark.parametrize("operation", ["_archive", "_stop_early"])
def test_main_attempts_container_removal_after_a_copy_timeout(monkeypatch, operation):
    commands: list[tuple[str, ...]] = []

    def docker(*args: str, content: bytes | None = None) -> bytes:
        del content
        commands.append(args)
        if args[0] == "version":
            return b'{"Client": {"Version": "test"}, "Server": {"Version": "test"}}'
        if args[:2] == ("image", "inspect"):
            return b"sha256:fixture"
        return b""

    def timeout(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired("copy", 10)

    monkeypatch.setattr(measurement, "_docker", docker)
    monkeypatch.setattr(measurement, "_archive", lambda *args, **kwargs: {})
    monkeypatch.setattr(measurement, operation, timeout)
    monkeypatch.setattr(
        sys, "argv", ["measure_docker_listing", "--image", "fixture", "--repeats", "1"]
    )

    with pytest.raises(subprocess.TimeoutExpired):
        measurement.main()

    create = next(args for args in commands if args[0] == "create")
    assert commands[-1] == ("rm", "--force", create[2])
