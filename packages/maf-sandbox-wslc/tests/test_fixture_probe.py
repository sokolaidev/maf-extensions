"""The fixture probe's answer, and its refusal, without an engine.

The live suite skips unless a real ``wslc`` and images are configured, so these are the
only runs CI makes of the guard that keeps an engine failure from passing as a fixture
state.
"""

from __future__ import annotations

import subprocess

import pytest
from _fixture_probe import _the_image_ships

_WORK = "/maf-sandbox/work"


def _answering(monkeypatch, returncode, stdout, stderr="", seen=None):
    def run(args, **kwargs):
        if seen is not None:
            seen.append(list(args))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize(
    "stdout,ships", [("present\n", True), ("absent\n", False)], ids=["present", "absent"]
)
def test_a_clean_run_answers_with_the_word_the_guest_printed(monkeypatch, stdout, ships):
    _answering(monkeypatch, 0, stdout)

    assert _the_image_ships(_WORK, "img") is ships


@pytest.mark.parametrize(
    "returncode,stdout",
    [(1, "absent\n"), (1, "present\n"), (1, ""), (125, "absent\n"), (0, "maybe\n"), (0, "")],
    ids=["failed-absent", "failed-present", "failed-silent", "no-image", "unknown-word", "silent"],
)
def test_anything_else_refuses_to_answer(monkeypatch, returncode, stdout):
    """A failure must not read as either answer.

    One caller requires the path absent and the other requires it present, so a status or
    a word that is not the guest's leaves no direction that is safe to fold it into.
    """
    _answering(monkeypatch, returncode, stdout, stderr="boom")

    with pytest.raises(RuntimeError, match=f"probing img for {_WORK} exited {returncode}"):
        _the_image_ships(_WORK, "img")


def test_the_probe_asks_as_root_on_no_network(monkeypatch):
    seen: list[list[str]] = []
    _answering(monkeypatch, 0, "absent\n", seen=seen)

    _the_image_ships(_WORK, "img")

    argv = seen[0]
    assert argv[:3] == ["wslc", "container", "run"]
    assert "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "0"
    assert argv[-1] == _WORK
