"""Validate live-test fixture resolution and sandbox ownership without contacting Azure."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from maf_sandbox import ExecResult, Isolation
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig


class _StoppableSandbox:
    """A sandbox the service stops when it goes idle, refusing calls until it is resumed.

    What the data plane really answers is HTTP 409 ``GlobalSandboxNotRunning``; the exception
    type is not what is being pinned here, only that a stopped sandbox refuses and that
    nothing short of another ``acquire`` puts it back.
    """

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.running = True

    def the_idle_timer_fires(self) -> None:
        self.running = False

    async def exec(self, command, *, working_directory: str, timeout: float) -> ExecResult:
        if not self.running:
            raise RuntimeError("Sandbox is not running")
        return ExecResult(stdout="ran")


class _ResumingBackend:
    """Enough of the backend for ``_Live``: ``acquire`` resumes, and counts how often."""

    def __init__(self, sandbox: _StoppableSandbox, *, replacement: _StoppableSandbox | None = None):
        self.sandbox = sandbox
        self.replacement = replacement
        self.acquires = 0

    async def acquire(self, key, spec) -> _StoppableSandbox:
        self.acquires += 1
        if self.replacement is not None:
            return self.replacement
        self.sandbox.running = True
        return self.sandbox


def _live_on(suite, loop, backend, sandbox, monkeypatch):
    """The live suite's own fixture object, over a fake backend and an image name."""
    monkeypatch.setattr(suite, "_IMAGE", "example:1")
    return suite._Live(loop, backend, suite._key("resume-test"), suite._spec(), sandbox)


def _live_suite():
    path = Path(__file__).with_name("test_acas_e2e.py")
    spec = importlib.util.spec_from_file_location("acas_live_fixture_test", path)
    assert spec and spec.loader
    suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suite)
    return suite


def test_guest_family_probe_retains_the_adopted_sandbox(monkeypatch):
    suite = _live_suite()
    monkeypatch.setattr(suite, "_IMAGE", "example:1")
    declarations = AcasSandboxBackend(
        AcasSandboxConfig(endpoint="https://sandbox.example.test")
    ).declarations
    backend = InProcessSandboxBackend(
        isolation=Isolation.MICROVM, declarations=declarations, sandbox_per_key=True
    )

    async def exec_posix(self, *args, **kwargs):
        assert self.instance_id not in backend.disposed_instances
        return ExecResult(stdout="posix")

    monkeypatch.setattr(InProcessSandbox, "exec", exec_posix)
    loop = asyncio.new_event_loop()
    try:
        key = suite._key("fixture-test")
        original = loop.run_until_complete(backend.acquire(key, suite._spec()))
        live = suite._Live(loop, backend, key, suite._spec(), original)
        suite.TestTheDeclaredGuestFamilyAgainstTheRealService().test_a_workload_requiring_posix_is_served_and_runs(
            live
        )
        assert original.instance_id in backend.disposed_instances
        assert live.sandbox.instance_id != original.instance_id
        following = loop.run_until_complete(backend.acquire(key, suite._spec()))
        assert live.sandbox.instance_id == following.instance_id
        result = live.run(
            live.sandbox.exec(["sh", "-c", "printf posix"], working_directory="/", timeout=1)
        )
        assert result.stdout == "posix"
    finally:
        loop.close()


def test_live_suite_resolves_its_fixtures_without_running_them():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--setup-plan", "-q", "tests/test_acas_e2e.py"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ACAS_SANDBOX_ENDPOINT": "https://sandbox.example.test"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_stopped_sandbox_is_resumed_before_the_call_that_needs_it(monkeypatch):
    """`_Live.run` returns through ``acquire`` first, so an idle gap does not fail a probe.

    The live suite cannot run on a pull request, so the behaviour keeping its shared fixture
    usable across the whole module is pinned here instead (#1097). The gap is real there: the
    fixtures for the other images each create and probe a sandbox of their own, minutes during
    which this one is idle and the service's auto-suspend timer stops it.
    """
    suite = _live_suite()
    sandbox = _StoppableSandbox("sandbox-1")
    backend = _ResumingBackend(sandbox)
    loop = asyncio.new_event_loop()
    try:
        live = _live_on(suite, loop, backend, sandbox, monkeypatch)
        sandbox.the_idle_timer_fires()

        ran = live.run(sandbox.exec("printf ran", working_directory="/", timeout=1))

        assert ran.stdout == "ran"
        assert backend.acquires == 1, "the call went to the sandbox without resuming it"
    finally:
        loop.close()


def test_a_sandbox_that_was_replaced_rather_than_resumed_is_reported(monkeypatch):
    """A failed resume creates a replacement, and the coroutine already built cannot use it.

    Caught rather than run against: the call would land on a guest holding none of the state
    the probes around it planted, and the failure would name whatever they assert next.
    """
    suite = _live_suite()
    sandbox = _StoppableSandbox("sandbox-1")
    backend = _ResumingBackend(sandbox, replacement=_StoppableSandbox("sandbox-2"))
    loop = asyncio.new_event_loop()
    try:
        live = _live_on(suite, loop, backend, sandbox, monkeypatch)

        with pytest.raises(AssertionError, match="replaced rather than resumed"):
            live.run(sandbox.exec("printf ran", working_directory="/", timeout=1))
    finally:
        loop.close()
