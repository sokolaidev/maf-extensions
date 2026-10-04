"""The export coordinator retains file access while its renderer drops root privileges."""

import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

RUNTIME = runpy.run_path(str(Path(__file__).parents[1] / "images/drawio-export/export.py"))


@pytest.mark.parametrize("uid", [0, 1000, 10001])
def test_renderer_identity_and_private_environment(tmp_path, monkeypatch, uid):
    options = {}
    killed = []

    class Process:
        pid = 123
        returncode = 0
        stdout = io.BytesIO()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, **kwargs):
            return 0

    def launch(argv, **kwargs):
        options.update(kwargs)
        return Process()

    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(os, "killpg", lambda *args: killed.append(args), raising=False)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(RUNTIME["signal"], "SIGKILL", 9, raising=False)
    RUNTIME["run_renderer"](["xvfb-run", "drawio"], time.monotonic() + 10, tmp_path)
    assert options["cwd"] == tmp_path
    assert options["env"]["HOME"] == options["env"]["TMPDIR"] == str(tmp_path)
    assert options["start_new_session"] is True
    if uid == 0:
        assert options["user"] == options["group"] == 10001
        assert options["extra_groups"] == []
    else:
        assert not {"user", "group", "extra_groups"}.intersection(options)
    assert killed == [(123, 9)]


@pytest.mark.skipif(sys.platform != "linux", reason="requires POSIX identity and file semantics")
@pytest.mark.parametrize("kind", ["regular", "link", "fifo", "empty", "oversized"])
def test_renderer_output_must_be_a_bounded_regular_file(tmp_path, kind):
    if sys.platform != "linux":
        pytest.skip("requires POSIX identity and file semantics")
    path = tmp_path / "output"
    if kind == "link":
        target = tmp_path / "secret"
        target.write_bytes(b"secret")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"svg" if kind == "regular" else b"")
        if kind == "oversized":
            with path.open("wb") as file:
                file.truncate(RUNTIME["MAX_FILE"] + 1)
    if kind == "regular":
        assert RUNTIME["read_export"](path) == b"svg"
    else:
        with pytest.raises((OSError, RuntimeError)):
            RUNTIME["read_export"](path)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux privilege dropping")
def test_real_child_identity_and_root_owned_call_directory():
    if sys.platform != "linux":
        pytest.skip("requires Linux privilege dropping")
    expected = 10001 if os.geteuid() == 0 else os.geteuid()
    with tempfile.TemporaryDirectory(prefix="drawio-identity-test-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o711)
        call = root / "call"
        call.mkdir(mode=0o700)
        profile = root / "profile"
        RUNTIME["renderer_directory"](profile)
        program = """
import json, os, sys
from pathlib import Path
profile, call = map(Path, sys.argv[1:])
try:
    (call / 'unauthorized').write_text('bad')
    can_write_call = True
except PermissionError:
    can_write_call = False
(profile / 'identity.json').write_text(json.dumps({
    'uids': os.getresuid(), 'gids': os.getresgid(), 'groups': os.getgroups(),
    'can_write_call': can_write_call, 'cwd': str(Path.cwd()),
}))
"""
        RUNTIME["run_renderer"](
            ["/usr/bin/python3", "-c", program, str(profile), str(call)],
            time.monotonic() + 10,
            profile,
        )
        identity = json.loads((profile / "identity.json").read_text())
        assert identity["uids"] == [expected] * 3
        assert identity["cwd"] == str(profile)
        if os.geteuid() == 0:
            assert identity["gids"] == [10001] * 3
            assert identity["groups"] == []
            assert identity["can_write_call"] is False
        data = RUNTIME["read_export"](profile / "identity.json")
        (call / "collected.json").write_bytes(data)
        assert (call / "collected.json").stat().st_uid == os.geteuid()
