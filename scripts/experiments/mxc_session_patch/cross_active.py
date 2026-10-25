"""Fixed active-guest probe using the native helper's private ownership pipe."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import process_identity as identity
from .host_store import Refused

MARKER = b"MXC_ACTIVE_READY:99"
CODE = b"assert remote_counter == 41\nremote_counter = 99\nprint('MXC_ACTIVE_READY:99', flush=True)\nwhile True: pass\n"
RESULT = {
    "boundary": "during_guest_execution",
    "native_counter": 99,
    "helper_stopped_after_owner_loss": True,
    "worker_killed": True,
}
STOP_SECONDS = 5


def command(helper: Path, startup: Path, work: Path, request: Path) -> list[str]:
    """Select the existing owned mode for this fixed live-execution probe."""
    return [
        str(helper.resolve()),
        "call-owned",
        str(startup.resolve()),
        str(work / "candidate"),
        str(request),
        str(work / "native.json"),
    ]


def execute(
    helper, startup, work, code, output_limit, *, before_spawn, before_start, checkpoint_limits
):
    """Run only the fixed probe; this path can never return a publishable result."""
    if code != CODE or output_limit != 1024:
        raise Refused("unexpected active guest probe")
    work = work.resolve()
    request = work / "code.py"
    request.write_bytes(code)
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update({key: str(work) for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")})
    before_spawn()
    with (
        (work / "native.stdout").open("wb") as stdout,
        (work / "native.stderr").open("wb") as stderr,
        subprocess.Popen(
            command(helper, startup, work, request),
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=work,
            env=env,
        ) as child,
    ):
        try:
            before_start(child)
            temporary = work / "active-helper.tmp"
            temporary.write_text(identity.capture(child.pid).encode(), encoding="utf-8")
            temporary.replace(work / "active-helper.json")
            assert child.stdin is not None
            child.stdin.write(b"MXCOWN1\n")
            child.stdin.flush()
            child.wait(timeout=90)
            raise Refused("active helper exited before owner loss")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)


def observe(root: Path):
    """Read only the trusted launch record and bounded fixed-probe readiness output."""
    paths = list((root / "scratch").glob("*/active-helper.json"))
    if not paths:
        return None
    if len(paths) != 1:
        raise Refused("ambiguous active helper launch")
    path = paths[0]
    with path.open("rb") as stream:
        value = stream.read(1025)
    helper = identity.Identity.decode(value.decode("utf-8"))
    work = path.parent
    with (work / "native.stdout").open("rb") as stream:
        output = stream.read(1025)
    if len(output) > 1024:
        raise Refused("active guest readiness exceeds its bound")
    if (work / "candidate").exists() or (work / "native.json").exists():
        raise Refused("active guest probe already completed")
    return helper, work, MARKER in output.splitlines()
