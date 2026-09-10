"""Launcher authority, bounded observations, and the limits of lineage attribution."""

import asyncio
import dataclasses
import json

import pytest

from maf_sandbox import (
    Cleanup,
    ExecResult,
    HostToolRegistry,
    HostToolRun,
    Isolation,
    ProcessInfo,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    guest_run_layout,
    host_tool_calls_over_exec,
)
from maf_sandbox._processes import ProcessTracker, _decode
from maf_sandbox._reclaim import close_unclean_notes, open_unclean_notes
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

LAYOUT = guest_run_layout("/work/call/run")


def process(pid, *, ppid=1, pgid=80, start=100, state="S", **kw):
    return ProcessInfo(pid, ppid, pgid, pgid, start, state, **kw)


def payload(processes, *, incomplete=False):
    rows = []
    for p in processes:
        row = dataclasses.asdict(p)
        row.pop("attribution")
        rows.append(row)
    return json.dumps({"processes": rows, "incomplete": incomplete})


class Recorder(SandboxObserver):
    def __init__(self):
        self.snapshots = []
        self.cleanups = []

    def processes_observed(self, event):
        self.snapshots.append(event)

    def process_cleanup(self, event):
        self.cleanups.append(event)


class Guest(InProcessSandbox):
    def __init__(self, *, receipt="maf-host-tools: process-v1 81 80\n", fail_signal=False):
        super().__init__()
        self.receipt = receipt
        self.fail_signal = fail_signal
        self.signals = []
        self.scan = 0
        self.victim = process(90, pgid=90, start=50)
        self.program = process(
            81,
            uid=1000,
            effective_uid=1000,
            argv=("python", "program.py"),
            command="python program.py",
        )

    async def exec(self, command, *, working_directory, timeout):
        assert isinstance(command, str)
        if " -I -S -c " in command:
            self.scan += 1
            rows = [self.victim]
            if self.scan in (2, 3) or (self.scan == 4 and self.fail_signal):
                rows.append(self.program)
            return ExecResult(stdout=payload(rows), exit_code=0)
        if command.startswith("kill -KILL"):
            self.signals.append(command)
            if self.fail_signal:
                raise PermissionError("refused")
            return ExecResult(stdout="", exit_code=0)
        self.contents[LAYOUT.pid] = b"90"
        self.contents[LAYOUT.session] = b"90"
        self.contents[LAYOUT.exit_code] = b"0"
        self.contents[LAYOUT.output] = b"finished"
        return ExecResult(stdout=self.receipt, exit_code=0)


def run(guest, observer=None):
    registry = HostToolRegistry(observer=observer)
    program = HostToolRun(registry, run_id="run-one")
    return asyncio.run(host_tool_calls_over_exec(guest, program, LAYOUT, timeout=2))


def test_success_cleans_from_the_receipt_and_audits_all_four_phases():
    guest, observer = Guest(), Recorder()
    assert run(guest, observer).stdout == "finished"
    assert guest.signals == ["kill -KILL -80 2>/dev/null"]
    assert [e.phase for e in observer.snapshots] == [
        "before_launch",
        "after_launch",
        "before_cleanup",
        "after_cleanup",
    ]
    assert len({e.snapshot_id for e in observer.snapshots}) == 4
    observed = observer.snapshots[1].processes[1]
    assert observed.uid == 1000 and observed.argv == ("python", "program.py")
    assert observed.attribution == "program"
    assert observer.snapshots[-1].processes[0].attribution == "preexisting"
    assert observer.cleanups[0].outcome == "sent"
    assert guest.reclaims


@pytest.mark.parametrize(
    "receipt",
    [
        "",
        "maf-host-tools: process-v1 0 0\n",
        "maf-host-tools: process-v1 81 1\n",
        "maf-host-tools: process-v1 81 80\n" * 2,
        "maf-host-tools: process-v1 81 80; echo bad\n",
    ],
)
def test_missing_or_invalid_receipt_never_falls_back_to_guest_files(receipt):
    guest = Guest(receipt=receipt)
    assert run(guest).exit_code == 0
    assert not guest.signals
    assert guest.reclaims


def test_process_failure_does_not_prevent_directory_reclamation():
    guest = Guest(fail_signal=True)
    notes, token = open_unclean_notes()
    try:
        assert run(guest).stdout == "finished"
    finally:
        close_unclean_notes(token)
    assert guest.reclaims
    assert notes


def test_observer_failure_does_not_prevent_cleanup():
    class Failing(Recorder):
        def processes_observed(self, event):
            raise RuntimeError("recorder unavailable")

        def process_cleanup(self, event):
            raise RuntimeError("recorder unavailable")

    guest = Guest()
    assert run(guest, Failing()).exit_code == 0
    assert guest.signals and guest.reclaims


@pytest.mark.parametrize("failure", [RuntimeError("read failed"), asyncio.CancelledError()])
def test_error_and_cancellation_keep_observation_and_directory_cleanup(failure):
    class FailingRead(Guest):
        async def stat_file(self, path, *, working_directory):
            raise failure

    guest, observer = FailingRead(), Recorder()
    with pytest.raises(type(failure)):
        run(guest, observer)
    assert guest.signals and guest.reclaims
    assert [e.phase for e in observer.snapshots] == [
        "before_launch",
        "after_launch",
        "before_cleanup",
        "after_cleanup",
    ]


def test_a_failed_final_snapshot_is_reported_without_skipping_reclamation():
    class FailedSnapshot(Guest):
        async def exec(self, command, *, working_directory, timeout):
            if " -I -S -c " in str(command) and self.scan == 3:
                raise PermissionError("proc unavailable")
            return await super().exec(command, working_directory=working_directory, timeout=timeout)

    guest, observer = FailedSnapshot(), Recorder()
    notes, token = open_unclean_notes()
    try:
        assert run(guest, observer).exit_code == 0
    finally:
        close_unclean_notes(token)
    assert guest.reclaims and notes
    assert observer.snapshots[-1].unavailable == "PermissionError"
    assert observer.snapshots[-1].incomplete


def test_an_observed_pid_replacement_is_not_signalled():
    class Replaced(Guest):
        async def exec(self, command, *, working_directory, timeout):
            if " -I -S -c " in str(command) and self.scan == 2:
                self.program = dataclasses.replace(self.program, start_ticks=200)
            return await super().exec(command, working_directory=working_directory, timeout=timeout)

    guest = Replaced()
    assert run(guest).exit_code == 0
    assert not guest.signals
    assert guest.reclaims


def test_cancelling_a_signal_marks_cleanup_incomplete_and_still_reclaims():
    async def scenario():
        sending = asyncio.Event()

        class CancelledSignal(Guest):
            async def exec(self, command, *, working_directory, timeout):
                if str(command).startswith("kill -KILL"):
                    sending.set()
                    await asyncio.Event().wait()
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        guest = CancelledSignal()
        notes, token = open_unclean_notes()
        try:
            task = asyncio.create_task(
                host_tool_calls_over_exec(guest, HostToolRun(HostToolRegistry()), LAYOUT, timeout=2)
            )
            await asyncio.wait_for(sending.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            close_unclean_notes(token)
        assert guest.reclaims
        assert any("interrupted" in reason for _, reason in notes)

    asyncio.run(scenario())


def tracker():
    return ProcessTracker(Guest(), HostToolRun(HostToolRegistry()), "python3", LAYOUT.directory)


def test_lineage_survives_reparenting_but_new_processes_are_not_descendants():
    tracked = tracker()
    tracked.pid, tracked.pgid = 81, 80
    tracked.baseline = {(90, 50)}
    parent, child, unrelated = process(81), process(82, ppid=81, pgid=82), process(91, pgid=91)
    observed = tracked.attribute((child, unrelated, parent))
    assert [p.attribution for p in observed] == ["descendant", "unattributed", "program"]
    observed = tracked.attribute((dataclasses.replace(child, ppid=1), unrelated))
    assert observed[0].attribution == "descendant"
    replacement = process(81, start=200)
    tracked.latest = tracked.attribute((replacement,))
    assert tracked.replaced()
    assert tracked.latest[0].attribution == "unattributed"


def test_zombies_are_observed_without_counting_as_running_survivors():
    tracked = tracker()
    tracked.pid = 81
    tracked.latest = tracked.attribute((process(81, state="Z"),))
    assert not tracked.survivors()
    assert tracked.latest[0].state == "Z"


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"processes": [{}], "incomplete": false}',
        payload([process(81), process(81)]),
        " " * (1024 * 1024 + 1),
    ],
    ids=["missing", "malformed", "duplicate", "oversized"],
)
def test_invalid_or_unbounded_snapshots_are_not_empty_successes(body):
    with pytest.raises((ValueError, TypeError)):
        _decode(body)


@pytest.mark.parametrize("confined", [False, True])
def test_reuse_requires_host_opt_in_even_for_a_confined_kind(confined):
    backend = InProcessSandboxBackend()
    spec = SandboxSpec(kind="test", confined_to_guest_call_path=confined)
    assert (
        SandboxRouter([backend], min_isolation=Isolation.NONE).effective_cleanup(spec)
        is Cleanup.DISPOSE
    )
    assert (
        SandboxRouter(
            [backend], min_isolation=Isolation.NONE, min_cleanup=Cleanup.RECLAIM
        ).effective_cleanup(spec)
        is Cleanup.RECLAIM
    )
