"""Linux proc-read exit races and conservative helper identity observations."""

from __future__ import annotations

import errno
import importlib
import io
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    identity = importlib.import_module("scripts.experiments.mxc_session_patch.process_identity")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def linux_observer(monkeypatch):
    monkeypatch.setattr(identity, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        identity, "platform", SimpleNamespace(system=lambda: "Linux", node=lambda: "host")
    )
    monkeypatch.setattr(identity, "_linux_boot", lambda: "boot:namespace")
    return identity.Identity("Linux", "host", 123, "42", "boot:namespace")


@pytest.fixture
def proc_read(monkeypatch, linux_observer):
    target = Path(f"/proc/{linux_observer.pid}/stat")
    original = Path.open
    reads = []

    def install(error):
        class Reader(io.StringIO):
            def read(self, *args, **kwargs):
                reads.append(target)
                raise error

        stream = Reader()

        def opened(path, *args, **kwargs):
            if path == target:
                return stream
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opened)
        return stream, reads

    return install


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ESRCH])
@pytest.mark.parametrize("operation", ["capture", "stopped", "terminate"])
def test_process_disappearing_during_proc_read_is_absent(
    linux_observer, proc_read, code, operation
):
    stream, reads = proc_read(OSError(code, "process disappeared"))
    if operation == "capture":
        with pytest.raises(identity.Refused, match="exited before identity persistence"):
            identity.capture(linux_observer.pid)
    elif operation == "stopped":
        assert identity.stopped(linux_observer)
    else:
        identity.terminate(linux_observer)
    assert len(reads) == 1
    assert stream.closed


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.EIO])
@pytest.mark.parametrize("operation", ["capture", "stopped", "terminate"])
def test_other_proc_read_errors_remain_unavailable(linux_observer, proc_read, code, operation):
    error = OSError(code, "unavailable")
    stream, reads = proc_read(error)
    with pytest.raises(identity.Refused, match="evidence is unavailable") as refused:
        getattr(identity, operation)(
            linux_observer.pid if operation == "capture" else linux_observer
        )
    assert refused.value.__cause__ is error
    assert len(reads) == 1
    assert stream.closed


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ESRCH, errno.EACCES])
def test_boot_observation_errors_do_not_prove_exit(linux_observer, monkeypatch, code):
    error = OSError(code, "boot evidence unavailable")

    def unavailable():
        raise error

    monkeypatch.setattr(identity, "_linux_boot", unavailable)
    with pytest.raises(identity.Refused, match="evidence is unavailable") as refused:
        identity.stopped(linux_observer)
    assert refused.value.__cause__ is error


def test_proc_read_esrch_cannot_bypass_namespace_mismatch(linux_observer, proc_read):
    proc_read(ProcessLookupError(errno.ESRCH, "process disappeared"))
    with pytest.raises(identity.Refused, match="boot or process namespace differs"):
        identity.stopped(replace(linux_observer, boot="another-namespace"))


@pytest.mark.parametrize("value", ["", "123 (helper) S", "malformed"])
def test_malformed_proc_evidence_remains_unavailable(linux_observer, monkeypatch, value):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: value)
    with pytest.raises(identity.Refused, match="evidence is unavailable"):
        identity.stopped(linux_observer)


@pytest.mark.parametrize(
    ("created", "state", "stopped"), [("42", "S", False), ("43", "S", True), ("42", "Z", True)]
)
def test_creation_identity_and_zombie_checks_remain(
    linux_observer, monkeypatch, created, state, stopped
):
    value = "123 (helper with ) in name) " + " ".join([state, *(["0"] * 18), created])
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: value)
    assert identity.stopped(linux_observer) is stopped


@pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux proc file descriptors")
def test_linux_exit_between_proc_open_and_read_repeatedly(monkeypatch):
    original = Path.open
    for _ in range(32):
        with subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
        ) as child:
            try:
                captured = identity.capture(child.pid)
                target = Path(f"/proc/{child.pid}/stat")
                streams = []

                def opened(path, *args, **kwargs):
                    stream = original(path, *args, **kwargs)
                    if path == target:
                        streams.append(stream)
                        assert child.stdin is not None
                        try:
                            child.stdin.close()
                            child.wait(timeout=10)
                        except BaseException:
                            stream.close()
                            raise
                    return stream

                with monkeypatch.context() as patch:
                    patch.setattr(Path, "open", opened)
                    assert identity.stopped(captured)
                assert len(streams) == 1
                assert streams[0].closed
                assert identity.stopped(captured)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
