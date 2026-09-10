"""Validate live-test fixture resolution and sandbox ownership without contacting Azure."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from maf_sandbox import ExecResult, Isolation
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig


def test_guest_family_probe_retains_the_adopted_sandbox(monkeypatch):
    path = Path(__file__).with_name("test_acas_e2e.py")
    spec = importlib.util.spec_from_file_location("acas_live_fixture_test", path)
    assert spec and spec.loader
    suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suite)
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
        live = suite._Live(loop, backend, key, original)
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
