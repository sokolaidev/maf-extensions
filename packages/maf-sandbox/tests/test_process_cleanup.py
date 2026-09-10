"""Launcher authority, bounded observations, and the limits of lineage attribution."""

import asyncio
import dataclasses
import json
import time

import pytest

from maf_sandbox import (
    Cleanup,
    ExecResult,
    HostToolRegistry,
    HostToolRun,
    Isolation,
    ProcessInfo,
    SandboxObserver,
    SandboxProgramTimeout,
    SandboxRouter,
    SandboxSpec,
    guest_run_layout,
    host_tool_calls_over_exec,
)
from maf_sandbox import _host_tools_over_exec as transport
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


def test_before_launch_observation_spends_the_run_budget():
    class SlowBaseline(Guest):
        async def exec(self, command, *, working_directory, timeout):
            if " -I -S -c " in str(command) and self.scan == 0:
                try:
                    await asyncio.sleep(0.3)
                finally:
                    await asyncio.sleep(0.02)
            return await super().exec(command, working_directory=working_directory, timeout=timeout)

    guest, observer = SlowBaseline(), Recorder()
    with pytest.raises(SandboxProgramTimeout) as expired:
        asyncio.run(
            host_tool_calls_over_exec(
                guest, HostToolRun(HostToolRegistry(observer=observer)), LAYOUT, timeout=0.05
            )
        )
    assert expired.value.signal == "absent"
    assert not guest.signals and guest.reclaims
    assert observer.snapshots[0].unavailable == "TimeoutError"


def test_after_launch_observation_cannot_turn_a_late_exit_into_success():
    class LateExit(Guest):
        async def exec(self, command, *, working_directory, timeout):
            if " -I -S -c " in str(command) and self.scan == 1:
                self.scan += 1
                await asyncio.sleep(0.3)
                self.contents[LAYOUT.exit_code] = b"0"
            result = await super().exec(
                command, working_directory=working_directory, timeout=timeout
            )
            if str(command).startswith("sh "):
                self.contents.pop(LAYOUT.exit_code)
            return result

    guest, observer = LateExit(), Recorder()
    with pytest.raises(SandboxProgramTimeout):
        asyncio.run(
            host_tool_calls_over_exec(
                guest, HostToolRun(HostToolRegistry(observer=observer)), LAYOUT, timeout=0.05
            )
        )
    assert guest.signals and guest.reclaims
    assert observer.snapshots[1].phase == "after_launch"
    assert observer.snapshots[1].unavailable == "TimeoutError"
    assert observer.snapshots[1].incomplete


@pytest.mark.parametrize("slow_step", ["before_cleanup", "signal", "descendants", "after_cleanup"])
def test_process_cleanup_steps_share_one_deadline_and_reserve_a_signal_attempt(slow_step):
    async def scenario():
        observer, operations = Recorder(), []
        child = process(82, ppid=81, pgid=82, start=101)

        class SlowCleanup(Guest):
            async def exec(self, command, *, working_directory, timeout):
                if " --signal " in str(command):
                    step = "descendants"
                    stdout = json.dumps([{"pid": 82, "outcome": "sent"}])
                elif " -I -S -c " in str(command):
                    self.scan += 1
                    step = "before_cleanup" if self.scan == 1 else "after_cleanup"
                    stdout = payload([self.program, child] if self.scan == 1 else [])
                else:
                    step, stdout = "signal", ""
                operations.append((step, timeout))
                if step == slow_step:
                    await asyncio.sleep(0.3)
                return ExecResult(stdout=stdout, exit_code=0)

        guest = SlowCleanup()
        tracked = ProcessTracker(
            guest, HostToolRun(HostToolRegistry(observer=observer)), "python3", LAYOUT.directory
        )
        tracked.pid, tracked.pgid = 81, 80
        tracked.latest = tracked.attribute((guest.program, child))
        tracked.phase = "after_launch"
        launcher = transport._WhatTheLauncherSaid(tracker=tracked, pid=81, pgid=80, executed=True)
        started = time.monotonic()
        await transport._stop_the_program(guest, LAYOUT, until=started + 0.08, launcher=launcher)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, (slow_step, elapsed, operations)
        assert any(step == "signal" for step, _ in operations)
        assert all(timeout <= 0.08 for _, timeout in operations)
        assert [event.phase for event in observer.snapshots] == ["before_cleanup", "after_cleanup"]
        if slow_step != "before_cleanup":
            assert observer.snapshots[-1].unavailable == "TimeoutError"

    asyncio.run(scenario())


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
    result = run(guest)
    assert result.exit_code != 0 and result.producer_owns_stderr
    assert LAYOUT.launcher + ".start" not in guest.contents
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


@pytest.mark.parametrize("failed_scan", [1, 2, 3])
@pytest.mark.parametrize("failure", ["unavailable", "incomplete"])
def test_earlier_snapshot_failures_still_mark_cleanup_unclean(failed_scan, failure):
    class FailedSnapshot(Guest):
        async def exec(self, command, *, working_directory, timeout):
            result = await super().exec(
                command, working_directory=working_directory, timeout=timeout
            )
            if " -I -S -c " in str(command) and self.scan == failed_scan:
                if failure == "unavailable":
                    raise PermissionError("proc unavailable")
                return dataclasses.replace(result, stdout=payload([], incomplete=True))
            return result

    guest, observer = FailedSnapshot(), Recorder()
    notes, token = open_unclean_notes()
    try:
        assert run(guest, observer).exit_code == 0
    finally:
        close_unclean_notes(token)
    assert guest.reclaims and guest.signals
    assert any("verification was unavailable or incomplete" in reason for _, reason in notes)
    assert observer.snapshots[failed_scan - 1].incomplete
    assert not observer.snapshots[-1].incomplete
    assert observer.snapshots[-1].unavailable is None


def test_guest_start_is_released_only_after_receipt_validation():
    class GatedGuest(Guest):
        async def write_file(self, path, content, *, working_directory):
            if path == LAYOUT.launcher + ".start":
                assert self.scan == 1
                assert isinstance(content, str)
                assert len(content) == transport._START_GATE_BYTES
                assert content.strip() in self.contents[LAYOUT.launcher].decode()
                self.released = True
            await super().write_file(path, content, working_directory=working_directory)

    guest = GatedGuest()
    assert run(guest).exit_code == 0
    assert guest.released


def test_start_gate_upload_spends_the_run_budget_and_still_cleans():
    class SlowGate(Guest):
        async def write_file(self, path, content, *, working_directory):
            if path == LAYOUT.launcher + ".start":
                await asyncio.sleep(0.3)
            await super().write_file(path, content, working_directory=working_directory)

    guest = SlowGate()
    with pytest.raises(SandboxProgramTimeout, match="releasing the program"):
        asyncio.run(
            host_tool_calls_over_exec(guest, HostToolRun(HostToolRegistry()), LAYOUT, timeout=0.05)
        )
    assert guest.signals and guest.reclaims


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
