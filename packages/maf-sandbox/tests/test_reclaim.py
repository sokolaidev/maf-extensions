"""Tests for the removal that runs in a tool call's ``finally``.

Two properties, and both are about what it does *not* do. It never raises, because an exception
there would replace whatever the call was already reporting. And it refuses a path it was not
given the right to delete, without depending on the caller having derived that path correctly —
this is an irreversible recursive delete, so the guard stands on its own.
"""

from __future__ import annotations

import asyncio

from maf_sandbox import ExecResult
from maf_sandbox._reclaim import reclaim_guest_path
from maf_sandbox.testing import InProcessSandbox

_WORK = "/maf-sandbox/work"


def _reclaim(sandbox, path, *, working_directory=_WORK, timeout=30.0):
    return asyncio.run(
        reclaim_guest_path(sandbox, path, working_directory=working_directory, timeout=timeout)
    )


class TestARemovalThatRuns:
    def test_a_path_under_the_work_dir_is_removed(self):
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/abc123") is None
        assert sandbox.commands == [(f"rm -rf {_WORK}/abc123", _WORK, 30.0)]

    def test_a_name_the_shell_would_read_is_quoted(self):
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/a b; touch pwned") is None
        assert sandbox.commands[0][0] == f"rm -rf '{_WORK}/a b; touch pwned'"


class TestARemovalThatIsRefused:
    """Refused here means no ``exec`` at all — the check is the point, not the message."""

    def test_a_path_outside_the_working_directory(self):
        sandbox = InProcessSandbox()
        reason = _reclaim(sandbox, "/etc")
        assert reason is not None
        assert "outside working directory" in reason
        assert sandbox.commands == []

    def test_a_path_that_climbs_out_of_it(self):
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/../../etc") is not None
        assert sandbox.commands == []

    def test_the_working_directory_itself(self):
        sandbox = InProcessSandbox()
        reason = _reclaim(sandbox, _WORK)
        assert reason is not None
        assert "the working directory itself" in reason
        assert sandbox.commands == []

    def test_the_working_directory_spelled_another_way(self):
        """`==` on the caller's spelling would answer "not the work dir" to `/x/work/.`."""
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/.") is not None
        assert sandbox.commands == []


class TestItNeverRaises:
    def test_a_guest_that_refuses_becomes_a_reason(self):
        class _Refusing(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                await super().exec(command, working_directory=working_directory, timeout=timeout)
                return ExecResult(stdout="", stderr="rm: Read-only file system", exit_code=1)

        reason = _reclaim(_Refusing(), f"{_WORK}/abc123")
        assert reason is not None
        assert "rm exited 1" in reason
        assert "Read-only file system" in reason

    def test_a_transport_failure_becomes_a_reason(self):
        reason = _reclaim(InProcessSandbox(raises=OSError("the guest is gone")), f"{_WORK}/abc123")
        assert reason is not None
        assert "the guest is gone" in reason
