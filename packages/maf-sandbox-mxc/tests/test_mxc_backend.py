"""Offline adapter admission, ownership and retained-session controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace

import pytest
from maf_sandbox import Egress, ProgramRequirements, SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_storage_base_conformance

from maf_sandbox_mxc._backend import MxcSandboxBackend
from maf_sandbox_mxc._config import MxcSandboxConfig, startup_identity
from maf_sandbox_mxc._files import Files
from maf_sandbox_mxc._state import Catalog, contract
from maf_sandbox_mxc.files.request import FileLimits, Request
from maf_sandbox_mxc.host_store import Refused
from maf_sandbox_mxc.shared_store import Limits, ScratchLimits

KEY = SandboxKey("scope", "thread", "agent")
SPEC = SandboxSpec(
    kind="python",
    work_dir=None,
    requires=frozenset(),
    program=ProgramRequirements(max_program_bytes=65536),
)


def test_contract_identity_survives_hash_randomization_and_label_order():
    program = (
        "from maf_sandbox import Capability, SandboxSpec\n"
        "from maf_sandbox_mxc._state import contract\n"
        "caps=frozenset({Capability.FILES_IN, Capability.FILES_OUT, Capability.FILES_LIST})\n"
        "print(contract(SandboxSpec(kind='test', requires=caps)))"
    )
    fingerprints = {
        subprocess.check_output(
            [sys.executable, "-c", program],
            env=os.environ | {"PYTHONHASHSEED": seed},
            timeout=30,
        )
        for seed in ("1", "2", "3", "4")
    }
    assert len(fingerprints) == 1
    first = replace(SPEC, labels={"a": "one", "b": "two"})
    reordered = replace(SPEC, labels={"b": "two", "a": "one"})
    changed = replace(first, execution_contract="different")
    assert contract(first) == contract(reordered) != contract(changed)


def test_reclaim_excludes_programs_until_worker_cleanup_finishes(config, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def reclaim(self, session):
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(Catalog, "reclaim", reclaim)

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="cleanup", timeout=20):
                sandbox = await backend.acquire(KEY, SPEC)
                cleanup = asyncio.create_task(
                    sandbox.reclaim("old", working_directory=".", timeout=10)
                )
                try:
                    async with asyncio.timeout(5):
                        while not started.is_set():
                            await asyncio.sleep(0.01)
                    with pytest.raises(Refused, match="already has"):
                        await sandbox.run_call(
                            "next", "print(1)", guest_call_path="next", timeout=5
                        )
                finally:
                    release.set()
                    await cleanup
                sandbox.authorize()
        finally:
            release.set()
            await backend.aclose()

    asyncio.run(check())


def test_collected_files_do_not_become_another_calls_implicit_uploads():
    files = Files(FileLimits())
    files.write("input", b"host", "one")
    assert [item.data for item in files.inputs("one")] == [b"host"]
    files.publish(
        json.dumps(
            {
                "call_directory": "one",
                "directories": [],
                "artifacts": [{"name": "output", "base64": "Z3Vlc3Q="}],
            }
        ).encode()
    )
    assert files.read("output", "one", 5) == b"guest"
    assert files.inputs("one") == ()
    files.write("output", b"explicit", "one")
    assert [item.data for item in files.inputs("one")] == [b"explicit"]


@pytest.fixture
def config(tmp_path):
    helper = tmp_path / "helper"
    helper.write_bytes(b"test helper identity")
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "snapshot").write_bytes(b"test snapshot")
    return MxcSandboxConfig(
        helper,
        startup,
        tmp_path / "store",
        "unit-test-file-profile",
        hashlib.sha256(helper.read_bytes()).hexdigest(),
        startup_identity(startup),
        Limits(100_000_000, 200_000_000, checkpoint_bytes=1024, result_bytes=40_000_000),
        ScratchLimits(100_000_000, 100_000, 200_000_000),
    )


def test_closed_admission_refuses_unsupported_policy_before_store_creation(config):
    backend = MxcSandboxBackend(config)
    for spec in (
        replace(SPEC, egress=Egress.UNRESTRICTED),
        replace(SPEC, egress=Egress.ALLOWLIST, egress_allow=("example.com",)),
        replace(SPEC, program=ProgramRequirements()),
        replace(SPEC, image="unqualified"),
    ):
        with pytest.raises(ValueError):
            backend.validate(spec)
    assert not config.root.exists()
    asyncio.run(backend.aclose())


def test_explicit_initialization_restart_and_policy_binding(config):
    async def check():
        backend = MxcSandboxBackend(config)
        try:
            async with backend.call_admission(KEY, SPEC, owner="one", timeout=2):
                with pytest.raises(Refused, match="unavailable"):
                    await backend.acquire(KEY, SPEC)
            await backend.initialize()
            async with backend.call_admission(KEY, SPEC, owner="one", timeout=2):
                sandbox = await backend.acquire(KEY, SPEC)
                first_id = sandbox.instance_id
                assert sandbox.freshly_created
            with pytest.raises(RuntimeError, match="admission"):
                await sandbox.stat_file("x", working_directory=".")
        finally:
            await backend.aclose()
        reopened = MxcSandboxBackend(config)
        try:
            async with reopened.call_admission(KEY, SPEC, owner="two", timeout=2):
                restored = await reopened.acquire(KEY, SPEC)
                assert restored.instance_id == first_id
                assert not restored.freshly_created
                changed = replace(SPEC, execution_contract="changed")
                with pytest.raises(Refused, match="contract"):
                    await reopened.acquire(KEY, changed)
        finally:
            await reopened.aclose()

    asyncio.run(check())


def test_root_has_one_owner_and_scope_purge_includes_unopened_sessions(config):
    async def check():
        first = MxcSandboxBackend(config)
        await first.initialize()
        async with first.call_admission(KEY, SPEC, owner="one", timeout=2):
            sandbox = await first.acquire(KEY, SPEC)
            previous = sandbox.instance_id
        second = MxcSandboxBackend(config)
        try:
            async with second.call_admission(KEY, SPEC, owner="two", timeout=2):
                with pytest.raises(Refused, match="owner"):
                    await second.acquire(KEY, SPEC)
        finally:
            await second.aclose()
            await first.aclose()
        third = MxcSandboxBackend(config)
        try:
            purged = await third.dispose_scope(KEY.scope, KEY.thread_id)
            assert purged.disposed == 1 and purged.undisposed is None
            async with third.call_admission(KEY, SPEC, owner="three", timeout=2):
                replacement = await third.acquire(KEY, SPEC)
                assert replacement.instance_id != previous
                assert await third.dispose(KEY, kind=SPEC.kind, instance_id=previous) is None
                assert replacement.alive
        finally:
            await third.aclose()

    asyncio.run(check())


def test_storage_base_and_expired_cleanup_authority(config):
    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="one", timeout=2) as cleanup:
                sandbox = await backend.acquire(KEY, SPEC)
                await assert_storage_base_conformance(sandbox, backend.declarations.capabilities)
                with pytest.raises(NotImplementedError, match="native program channel"):
                    await sandbox.exec(["echo", "unsupported"], working_directory=".", timeout=1)
            with pytest.raises(RuntimeError, match="expired"):
                with cleanup:
                    pass
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_replay_of_retired_session_needs_no_native_assets(config):
    request = Request(b"pass", (), (), config.files, config.workspace, "one")

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="seed", timeout=2):
                sandbox = await backend.acquire(KEY, SPEC)
                session = sandbox.instance_id

                def seed(catalog):
                    store = catalog.stores[session]
                    store.begin("one", b"mxc-fresh-state-v1\0" + request.identity())
                    store.commit("one", config.startup, b"saved-result-envelope")

                await backend.work(seed)
                assert await backend.dispose(KEY, kind=SPEC.kind) is None
        finally:
            await backend.aclose()
        config.helper.unlink()
        backend = MxcSandboxBackend(config)
        try:
            async with backend.call_admission(KEY, SPEC, owner="retry", timeout=2):
                assert (
                    await backend.replay_result(
                        KEY,
                        SPEC,
                        instance_id=session,
                        call_id="one",
                        request=request,
                    )
                    == b"saved-result-envelope"
                )
                with pytest.raises(Refused, match="no retained call"):
                    await backend.replay_result(
                        KEY,
                        SPEC,
                        instance_id=session,
                        call_id="missing",
                        request=request,
                    )
                with pytest.raises(Refused, match="different request"):
                    await backend.replay_result(
                        KEY,
                        SPEC,
                        instance_id=session,
                        call_id="one",
                        request=replace(request, code=b"different"),
                    )
        finally:
            await backend.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("cancelled", [False, True])
def test_cancellation_waits_for_worker_shutdown(config, monkeypatch, cancelled):
    started, stopped = threading.Event(), threading.Event()

    def execute(self, session, call_id, request, cancel, timeout):
        started.set()
        assert cancel.wait(5)
        stopped.set()
        raise Refused("cancelled")

    monkeypatch.setattr(Catalog, "execute", execute)

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="one", timeout=2):
                sandbox = await backend.acquire(KEY, SPEC)
                task = asyncio.create_task(
                    sandbox.run_call(
                        "one",
                        "pass",
                        guest_call_path="one",
                        timeout=1 if cancelled else 0.1,
                    )
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                if cancelled:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError if cancelled else TimeoutError):
                    await task
                assert stopped.is_set()
                assert not sandbox.busy
        finally:
            await backend.aclose()

    asyncio.run(check())
