"""Tests for the removal that runs in a tool call's ``finally``.

Two properties, and both are about what it does *not* do. It never raises, because an exception
there would replace whatever the call was already reporting. And it refuses a path it was not
given the right to delete, without depending on the caller having derived that path correctly —
this is an irreversible recursive delete, so the guard stands on its own.

Every assertion about a removal that happened reads :attr:`InProcessSandbox.reclaims`. The
mechanism is a dispatched protocol member rather than a command line, so ``commands`` would
answer the same for a removal that ran and one that was never attempted.
"""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox._reclaim import reclaim_guest_path
from maf_sandbox.testing import InProcessSandbox

_WORK = "/maf-sandbox/work"


def _reclaim(sandbox, path, *, working_directory=_WORK, timeout=30.0):
    return asyncio.run(
        reclaim_guest_path(sandbox, path, working_directory=working_directory, timeout=timeout)
    )


class TestARemovalThatRuns:
    def test_a_path_under_the_work_dir_is_removed(self):
        sandbox = InProcessSandbox(seed_files={f"{_WORK}/abc123/program.py": "written by a call"})
        assert _reclaim(sandbox, f"{_WORK}/abc123") is None
        assert sandbox.reclaims == [(f"{_WORK}/abc123", _WORK, 30.0)]
        assert f"{_WORK}/abc123/program.py" not in sandbox.contents

    def test_the_working_directory_says_where_the_call_directory_sits(self):
        """It is passed through as the spec's own, not rewritten into somewhere to run from.

        No backend creates a spec's work dir, so a removal that moved there first would fail
        for a call that took a path and wrote nothing. That duty is the backend's now — the
        reclaim contract states it, and `an-absent-working-directory-still-succeeds` asks it.
        """
        sandbox = InProcessSandbox()
        _reclaim(sandbox, f"{_WORK}/abc123")
        assert sandbox.reclaims[0][1] == _WORK

    def test_a_name_a_shell_would_read_reaches_the_backend_unaltered(self):
        """Nothing is quoted here, because nothing here builds a command.

        A name escaped on the way past would have the backend remove a directory that is not
        the call's; making it safe wherever a shell is involved is the backend's own.
        """
        awkward = f"{_WORK}/a b; touch pwned"
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, awkward) is None
        assert sandbox.reclaims == [(awkward, _WORK, 30.0)]


class TestARemovalThatIsRefused:
    """Refused here means no removal call at all — the check is the point, not the message."""

    def test_a_path_outside_the_working_directory(self):
        sandbox = InProcessSandbox()
        reason = _reclaim(sandbox, "/etc")
        assert reason is not None
        assert "outside working directory" in reason
        assert sandbox.reclaims == []

    def test_a_path_that_climbs_out_of_it(self):
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/../../etc") is not None
        assert sandbox.reclaims == []

    def test_the_working_directory_itself(self):
        sandbox = InProcessSandbox()
        reason = _reclaim(sandbox, _WORK)
        assert reason is not None
        assert "the working directory itself" in reason
        assert sandbox.reclaims == []

    def test_a_path_one_component_from_the_root(self):
        """Reclaiming `/abc123` under a work dir of `/` — cleanup turning into an outage."""
        sandbox = InProcessSandbox()
        reason = _reclaim(sandbox, "/abc123", working_directory="/")
        assert reason is not None
        assert "too close to the root" in reason
        assert sandbox.reclaims == []

    def test_the_working_directory_spelled_another_way(self):
        """`==` on the caller's spelling would answer "not the work dir" to `/x/work/.`."""
        sandbox = InProcessSandbox()
        assert _reclaim(sandbox, f"{_WORK}/.") is not None
        assert sandbox.reclaims == []


class TestItNeverRaises:
    def test_a_backend_that_refuses_becomes_a_reason(self):
        """A sandbox alive on every other surface, whose removal is the one thing refused."""

        class _Refusing(InProcessSandbox):
            async def reclaim(self, directory, *, working_directory, timeout):
                self.reclaims.append((directory, working_directory, timeout))
                raise PermissionError("Read-only file system")

        sandbox = _Refusing()
        reason = _reclaim(sandbox, f"{_WORK}/abc123")
        assert reason is not None
        assert "Read-only file system" in reason
        assert sandbox.reclaims == [(f"{_WORK}/abc123", _WORK, 30.0)]

    def test_a_transport_failure_becomes_a_reason(self):
        reason = _reclaim(InProcessSandbox(raises=OSError("the guest is gone")), f"{_WORK}/abc123")
        assert reason is not None
        assert "the guest is gone" in reason

    def test_a_cancelled_removal_is_not_answered_with_a_reason(self):
        """Cancellation at this await is the caller's deadline, not a backend failure.

        Answering with a reason would contain it, and the call would return past a bound the
        host thought it had. The caller records the loss and lets it through.
        """
        cancelled = InProcessSandbox(raises=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            _reclaim(cancelled, f"{_WORK}/abc123")
