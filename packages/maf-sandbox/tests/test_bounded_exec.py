"""Untrusted stdout and stderr are bounded before a complete result exists."""

import asyncio
import shlex
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from maf_sandbox import (
    HostToolRegistry,
    HostToolRun,
    ProcessInfo,
    Sandbox,
    SandboxExecOutputLimitExceeded,
    SandboxObserver,
)
from maf_sandbox._processes import ProcessTracker
from maf_sandbox.bounded_exec import read_bounded_process_output
from maf_sandbox.testing import InProcessSandbox


@pytest.mark.parametrize("channel", [1, 2, 0])
def test_output_overflow_interrupts_a_live_producer_and_reaps_it(channel):
    async def scenario():
        script = (
            "import os,time; "
            + (
                f"os.write({channel}, b'x' * 65536); "
                if channel
                else "os.write(1, b'x' * 600); os.write(2, b'y' * 600); "
            )
            + "time.sleep(30)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with pytest.raises(SandboxExecOutputLimitExceeded):
            await read_bounded_process_output(process, max_output_bytes=1024, timeout=5)
        assert process.returncode is not None

    asyncio.run(scenario())


def test_exact_combined_byte_budget_and_exit_code_survive():
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; os.write(1,b'abc'); os.write(2,b'def'); exit(7)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert await read_bounded_process_output(process, max_output_bytes=6, timeout=5) == (
            b"abc",
            b"def",
        )
        assert process.returncode == 7

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_bounded_read_reaps_the_process(cancel):
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task = asyncio.create_task(
            read_bounded_process_output(
                process, max_output_bytes=1024, timeout=5 if cancel else 0.05
            )
        )
        if cancel:
            await asyncio.sleep(0.05)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert process.returncode is not None

    asyncio.run(scenario())


def test_process_observation_never_falls_back_to_unbounded_exec():
    async def forbidden(*args, **kwargs):
        pytest.fail("unbounded execution must not run")

    async def scenario():
        sandbox = SimpleNamespace(instance_id="one", exec=forbidden)
        tracker = ProcessTracker(
            cast(Sandbox, sandbox), HostToolRun(HostToolRegistry()), "python3", "/work"
        )
        await tracker.snapshot("before_launch")
        assert tracker.incomplete and tracker.latest is None

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["snapshot", "descendants"])
def test_probes_request_a_transport_cap_and_audit_overflow(operation):
    class Observer(SandboxObserver):
        events = []

        def processes_observed(self, event):
            self.events.append(event)

        def process_cleanup(self, event):
            self.events.append(event)

    class Guest(InProcessSandbox):
        async def exec(self, *args, **kwargs):
            pytest.fail("probe fell back to unbounded exec")

        async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
            assert max_output_bytes == 1024 * 1024
            assert isinstance(command, str)
            assert ("--signal" in shlex.split(command)[5:]) == (operation == "descendants")
            raise SandboxExecOutputLimitExceeded("overflow")

    async def scenario():
        observer = Observer()
        tracker = ProcessTracker(
            Guest(), HostToolRun(HostToolRegistry(observer=observer)), "python3", "/work"
        )
        if operation == "snapshot":
            await tracker.snapshot("before_launch")
            assert tracker.incomplete and tracker.latest is None
            assert observer.events[-1].unavailable == "SandboxExecOutputLimitExceeded"
        else:
            child = ProcessInfo(82, 1, 82, 82, 100, "S")
            tracker.observed[child.identity] = child
            assert not await tracker.stop_descendants()
            assert observer.events[-1].outcome == "unknown"
            assert observer.events[-1].signal is None

    asyncio.run(scenario())
