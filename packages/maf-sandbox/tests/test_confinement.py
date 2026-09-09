"""A confinement promise must fail on filesystem residue or a surviving process."""

import asyncio

import pytest

from maf_sandbox.conformance import (
    ConformanceFailure,
    SandboxFingerprint,
    assert_nothing_left_behind,
)
from maf_sandbox.testing import InProcessSandbox


def test_call_and_cleanup_restore_the_engine_fingerprint():
    sandbox = InProcessSandbox(seed_files={"/image-file": b"original"})
    sandbox.running.add("keepalive:1")

    async def call():
        await sandbox.write_file("input", b"data", working_directory="/work/call")
        await sandbox.exec(["compiler", "input"], working_directory="/work/call", timeout=1)
        await sandbox.reclaim("/work/call", working_directory="/work", timeout=1)

    results = asyncio.run(assert_nothing_left_behind(sandbox, call))
    assert len(results) == 1 and results[0].passed
    assert len(sandbox.commands) == len(sandbox.reclaims) == 1
    assert sandbox.contents == {"/image-file": b"original"}
    assert sandbox.running == {"keepalive:1"}


@pytest.mark.parametrize("residue", ["file", "directory", "symlink", "other", "process", "both"])
def test_cleanup_cannot_hide_residue_outside_the_call_path(residue):
    sandbox = InProcessSandbox()

    async def call():
        await sandbox.write_file("input", b"data", working_directory="/work/call")
        if residue in {"file", "both"}:
            sandbox.contents["/escaped"] = b"residue"
        elif residue == "directory":
            sandbox.directories.add("/escaped")
        elif residue == "symlink":
            sandbox.symlinks.add("/escaped")
        elif residue == "other":
            sandbox.non_regular.add("/escaped")
        if residue in {"process", "both"}:
            sandbox.running.add("detached:42")
        await sandbox.reclaim("/work/call", working_directory="/work", timeout=1)

    with pytest.raises(ConformanceFailure) as caught:
        asyncio.run(assert_nothing_left_behind(sandbox, call))
    assert caught.value.results[0].skipped is None
    if residue != "process":
        assert "/escaped" in str(caught.value)
    if residue in {"process", "both"}:
        assert "detached:42" in str(caught.value)


@pytest.mark.parametrize("change", ["rewrite", "delete", "replace-process", "kill-process"])
def test_changes_to_existing_state_fail(change):
    sandbox = InProcessSandbox(seed_files={"/image-file": b"original"})
    sandbox.running.add("keepalive:1")

    async def call():
        if change == "rewrite":
            sandbox.contents["/image-file"] = b"changed"
        elif change == "delete":
            del sandbox.contents["/image-file"]
        else:
            sandbox.running.remove("keepalive:1")
            if change == "replace-process":
                sandbox.running.add("keepalive:2")

    with pytest.raises(ConformanceFailure, match="image-file|keepalive:1"):
        asyncio.run(assert_nothing_left_behind(sandbox, call))


def test_dirty_baseline_is_refused_before_the_call():
    sandbox = InProcessSandbox()
    sandbox.contents["/already-changed"] = b"old residue"
    called = False

    async def call():
        nonlocal called
        called = True
        sandbox.contents["/already-changed"] = b"new residue"

    with pytest.raises(ConformanceFailure, match="not pristine.*already-changed"):
        asyncio.run(assert_nothing_left_behind(sandbox, call))
    assert not called


class _EngineSubject:
    def __init__(self, *snapshots):
        self.snapshots = iter(snapshots)

    async def fingerprint(self):
        snapshot = next(self.snapshots)
        if isinstance(snapshot, BaseException):
            raise snapshot
        return snapshot


def test_unsupported_engine_is_an_explicit_skip_without_running_the_call():
    async def call():
        pytest.fail("an unsupported engine must not run the workload")

    results = asyncio.run(assert_nothing_left_behind(_EngineSubject(None), call))
    assert not results[0].passed
    assert results[0].skipped == "engine fingerprint is unsupported"
    assert results[0].failure is None


@pytest.mark.parametrize("after", [None, OSError("engine unavailable"), NotImplementedError()])
def test_losing_the_measurement_after_the_call_fails(after):
    async def call():
        pass

    subject = _EngineSubject(SandboxFingerprint(frozenset(), frozenset()), after)
    with pytest.raises(ConformanceFailure):
        asyncio.run(assert_nothing_left_behind(subject, call))


def test_engine_error_is_not_an_unsupported_skip():
    async def call():
        pytest.fail("a failed fingerprint must not run the workload")

    with pytest.raises(ConformanceFailure, match="unreadable mount"):
        asyncio.run(assert_nothing_left_behind(_EngineSubject(OSError("unreadable mount")), call))


def test_engine_diff_remains_authoritative_when_bytes_were_restored():
    async def call():
        pass

    subject = _EngineSubject(
        SandboxFingerprint(frozenset(), frozenset()),
        SandboxFingerprint(frozenset({"/restored"}), frozenset()),
    )
    with pytest.raises(ConformanceFailure, match="/restored"):
        asyncio.run(assert_nothing_left_behind(subject, call))


@pytest.mark.parametrize("error", [RuntimeError("cleanup failed"), asyncio.CancelledError()])
def test_call_failure_cannot_pass_and_cancellation_propagates(error):
    async def call():
        raise error

    expected = (
        asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else ConformanceFailure
    )
    with pytest.raises(expected):
        asyncio.run(assert_nothing_left_behind(InProcessSandbox(), call))
