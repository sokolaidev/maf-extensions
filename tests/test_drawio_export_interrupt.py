"""Fault readiness requires native PNG bytes, not merely an Electron process."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import drawio_export_interrupt as probe  # noqa: E402


def test_main_process_without_export_is_not_readiness(tmp_path, monkeypatch):
    entry = tmp_path / "123"
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"/opt/drawio/drawio\0")
    clock = iter([0, 0, 2])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    interrupt = Mock()
    monkeypatch.setattr(probe, "interrupt", interrupt)
    with pytest.raises(RuntimeError, match="No native PNG"):
        probe.observe(tmp_path, timeout=1)
    interrupt.assert_not_called()


@pytest.mark.parametrize("data", [b"", b"not png!", probe.PNG_SIGNATURE])
def test_export_startup_requires_png_bytes_before_fault_readiness(tmp_path, monkeypatch, data):
    entry = tmp_path / "123"
    entry.mkdir()
    destination = tmp_path / "output.png"
    (entry / "cmdline").write_bytes(
        b"\0".join(
            [
                b"/opt/drawio/drawio",
                b"--export",
                b"--format",
                b"png",
                b"--output",
                str(destination).encode(),
            ]
        )
    )
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(fcntl=Mock(), F_SETPIPE_SZ=1))
    clock = iter([0, 0, 0, 2])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe, "wait_stopped", Mock())
    monkeypatch.setattr(probe.os, "mkfifo", Mock(), raising=False)
    monkeypatch.setattr(probe.os, "chown", Mock(), raising=False)
    monkeypatch.setattr(probe.os, "open", Mock(return_value=9))
    close = Mock()
    monkeypatch.setattr(probe.os, "close", close)
    monkeypatch.setattr(probe.os, "O_NONBLOCK", 0, raising=False)
    monkeypatch.setattr(probe.os, "read", Mock(return_value=data))
    monkeypatch.setattr(probe.signal, "SIGSTOP", 19, raising=False)
    monkeypatch.setattr(probe.signal, "SIGCONT", 18, raising=False)
    kill = Mock()
    monkeypatch.setattr(probe.os, "kill", kill)
    if data == probe.PNG_SIGNATURE:
        report = probe.observe(tmp_path, timeout=1)
        assert report == {"renderer_pid": 123, "stopped": True, "pngSignature": data.hex()}
        assert kill.call_args.args == (123, 19)
    else:
        with pytest.raises(RuntimeError, match="PNG"):
            probe.observe(tmp_path, timeout=1)
        assert kill.call_args.args == (123, 18)
    close.assert_called_once_with(9)
