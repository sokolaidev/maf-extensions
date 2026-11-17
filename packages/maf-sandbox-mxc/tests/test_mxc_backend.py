"""Offline adapter admission, ownership and retained-session controls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace

import pytest
from maf_sandbox import (
    BackendDeclarations,
    Cleanup,
    Egress,
    ProgramRequirements,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import assert_storage_base_conformance

from maf_sandbox_mxc._backend import MxcSandbox, MxcSandboxBackend
from maf_sandbox_mxc._config import MxcSandboxConfig, startup_identity
from maf_sandbox_mxc._files import Files
from maf_sandbox_mxc._state import Catalog, contract
from maf_sandbox_mxc.files.request import FileLimits, Request
from maf_sandbox_mxc.files.transport import GuestProgramError
from maf_sandbox_mxc.host_store import Refused
from maf_sandbox_mxc.native_journal import NativeJournal
from maf_sandbox_mxc.shared_store import Limits, ScratchLimits

KEY = SandboxKey("scope", "thread", "agent")
SPEC = SandboxSpec(
    kind="python",
    work_dir=None,
    requires=frozenset(),
    program=ProgramRequirements(max_program_bytes=65536),
)


def test_cancelled_close_finishes_shutdown_before_propagating(config, monkeypatch):
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    close = Catalog.close

    def blocked_close(self):
        started.set()
        assert release.wait(10)
        close(self)
        stopped.set()

    monkeypatch.setattr(Catalog, "close", blocked_close)

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        async with backend.call_admission(KEY, SPEC, owner="one", timeout=2):
            sandbox = await backend.acquire(KEY, SPEC)
        task = asyncio.create_task(backend.aclose())
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not stopped.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set() and backend.closed
        with pytest.raises(RuntimeError, match="shutdown"):
            backend.executor.submit(lambda: None)
        with pytest.raises(Refused, match="closed"):
            await backend.work(lambda c: c.acquire(KEY, SPEC))
        async with backend.call_admission(KEY, SPEC, owner="closed", timeout=2):
            with pytest.raises(Refused, match="retired"):
                sandbox.authorize()
        await backend.aclose()
        reopened = MxcSandboxBackend(config)
        try:
            async with reopened.call_admission(KEY, SPEC, owner="reopen", timeout=2):
                await reopened.acquire(KEY, SPEC)
        finally:
            await reopened.aclose()

    asyncio.run(check())


def test_warm_acquires_keep_freshness_and_share_session_operations(config, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def execute(self, session, call_id, request, cancel, timeout):
        started.set()
        assert release.wait(10)
        return json.dumps(
            {
                "streams": {"stdout": {"base64": ""}, "stderr": {"base64": ""}},
                "artifacts": [],
                "directories": [],
                "call_directory": request.call_directory,
            }
        ).encode()

    monkeypatch.setattr(Catalog, "execute", execute)

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="one", timeout=5):
                first = await backend.acquire(KEY, SPEC)
                second = await backend.acquire(KEY, SPEC)
                assert first is not second
                assert first.instance_id == second.instance_id
                assert first.freshly_created and not second.freshly_created
                await first.write_file("one/input", b"input", working_directory="/workspace/calls")
                assert (
                    await second.read_file(
                        "one/input", working_directory="/workspace/calls", max_bytes=10
                    )
                    == b"input"
                )
                task = asyncio.create_task(
                    first.run_call("one", "pass", guest_call_path="one", timeout=5)
                )
                try:
                    async with asyncio.timeout(5):
                        while not started.is_set():
                            await asyncio.sleep(0.001)
                    with pytest.raises(Refused, match="running program"):
                        second.authorize()
                finally:
                    release.set()
                    await task
                assert first.freshly_created and not second.freshly_created
                with pytest.raises(Refused, match="retry code"):
                    await second.run_call("one", "changed", guest_call_path="one", timeout=2)
                assert await backend.dispose(KEY, kind=SPEC.kind) is None
                for sandbox in (first, second):
                    with pytest.raises(Refused, match="retired"):
                        sandbox.authorize()
        finally:
            await backend.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("cancelled", [False, True])
def test_router_admission_can_release_from_another_task(config, cancelled):
    async def check():
        backend = MxcSandboxBackend(config)
        router = SandboxRouter([backend], min_cleanup=Cleanup.RECLAIM)
        await backend.initialize()
        try:
            await router.enter_call(KEY, SPEC, owner="first", timeout=2)
            sandbox = await router.acquire(KEY, SPEC)
            assert isinstance(sandbox, MxcSandbox)

            async def release():
                if cancelled:
                    try:
                        raise asyncio.CancelledError
                    finally:
                        await router.release_call(KEY, SPEC.kind, owner="first")
                else:
                    await router.release_call(KEY, SPEC.kind, owner="first")

            task = asyncio.create_task(release())
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            with pytest.raises(RuntimeError, match="admission"):
                sandbox.authorize()
            await router.enter_call(KEY, SPEC, owner="second", timeout=2)
            sandbox.authorize()
            await router.release_call(KEY, SPEC.kind, owner="second")
        finally:
            await backend.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("cleanup", [Cleanup.RECLAIM, Cleanup.RESET, Cleanup.DISPOSE])
def test_router_restart_recovery_requires_persistence_and_reclaim(config, persistent, cleanup):
    config = replace(config, persistent=persistent)
    if persistent and "recovers_state" not in BackendDeclarations.__dataclass_fields__:
        with pytest.raises(ValueError, match="requires maf-sandbox 0.55"):
            MxcSandboxBackend(config)
        return

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="before", timeout=2):
                sandbox = await SandboxRouter([backend], min_cleanup=cleanup).acquire(KEY, SPEC)
                original = sandbox.instance_id
                await backend.work(lambda c: _seed_checkpoint(c, original, "one"))
        finally:
            await backend.aclose()
        backend = MxcSandboxBackend(config)
        try:
            async with backend.call_admission(KEY, SPEC, owner="after", timeout=2):
                sandbox = await SandboxRouter([backend], min_cleanup=cleanup).acquire(KEY, SPEC)
                if persistent and cleanup is Cleanup.RECLAIM:
                    assert isinstance(sandbox, MxcSandbox)
                    assert sandbox.instance_id == original
                    assert not sandbox.freshly_created
                    assert (
                        await backend.work(
                            lambda c: (
                                c.stores[original]
                                .db.execute(
                                    "SELECT current_call FROM sessions WHERE id=?", (original,)
                                )
                                .fetchone()[0]
                            )
                        )
                        == "one"
                    )
                else:
                    assert sandbox.instance_id != original
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_old_core_refuses_persistence_before_allocating_backend_resources(config, monkeypatch):
    monkeypatch.delitem(BackendDeclarations.__dataclass_fields__, "recovers_state", raising=False)
    with pytest.raises(ValueError, match="requires maf-sandbox 0.55"):
        MxcSandboxBackend(replace(config, persistent=True))
    assert not config.root.exists()
    backend = MxcSandboxBackend(config)
    asyncio.run(backend.aclose())


def _seed_checkpoint(catalog, session, call_id):
    store = catalog.stores[session]
    store.begin(call_id, call_id.encode())
    store.commit(call_id, catalog.config.startup, b"saved")


def test_adapter_returns_guest_diagnostics_but_refuses_transport_failures(config, monkeypatch):
    streams = {
        name: {"base64": base64.b64encode(data).decode(), "omitted_bytes": 0}
        for name, data in (("stdout", b"before\n"), ("stderr", b"ValueError: bad input\n"))
    }

    def execute(self, session, call_id, request, cancel, timeout):
        if call_id == "guest":
            raise GuestProgramError(json.dumps({"streams": streams, "exit_code": 1}).encode())
        raise Refused("native transport failed")

    monkeypatch.setattr(Catalog, "execute", execute)

    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        try:
            async with backend.call_admission(KEY, SPEC, owner="errors", timeout=2):
                sandbox = await backend.acquire(KEY, SPEC)
                result = await sandbox.run_call("guest", "bad", guest_call_path="guest", timeout=2)
                assert result.exit_code == 1
                assert result.stdout == "before\n" and result.stderr == "ValueError: bad input\n"
                assert sandbox.state.last_request is None and sandbox.state.files.revision == 0
                with pytest.raises(Refused, match="native transport failed"):
                    await sandbox.run_call(
                        "transport", "bad", guest_call_path="transport", timeout=2
                    )
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_retirement_collects_checkpoints_but_preserves_retry_results(config):
    with_catalog = Catalog(config, initialize=True)
    try:
        for index in range(5):
            session, _ = with_catalog.acquire(KEY, SPEC)
            _seed_checkpoint(with_catalog, session, str(index))
            with_catalog.retire(session)
        db = with_catalog.sentinel.db
        assert db.execute("SELECT sum(checkpoint_charge) FROM calls").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM calls WHERE result=?", (b"saved",)).fetchone()[0] == 5
        )
        assert with_catalog.key_sessions(KEY, SPEC.kind) == ()
        assert with_catalog.scope_sessions(KEY.scope, KEY.thread_id) == ()
        with_catalog.sentinel.audit_usage()
    finally:
        with_catalog.close()


@pytest.mark.parametrize("scope", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_disposal_retries_retired_epochs_with_unresolved_scratch(
    config, monkeypatch, scope, restart
):
    async def check():
        backend = MxcSandboxBackend(config)
        await backend.initialize()
        async with backend.call_admission(KEY, SPEC, owner="seed", timeout=2):
            sandbox = await backend.acquire(KEY, SPEC)
            session = sandbox.instance_id

            def seed(catalog):
                store = catalog.stores[session]
                store.begin("failed", b"failed", scratch=config.scratch)
                work = NativeJournal(store).prepare("failed")
                (work / "leftover").write_bytes(b"scratch")
                with subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"]
                ) as child:
                    try:
                        NativeJournal(store).arm("failed", child)
                    finally:
                        child.terminate()
                        child.wait(timeout=5)

            await backend.work(seed)
        original = NativeJournal.delete

        def failing_delete(journal):
            journal.store.retire()
            raise Refused("cleanup unavailable")

        monkeypatch.setattr(NativeJournal, "delete", failing_delete)
        assert await backend.dispose(KEY, kind=SPEC.kind) is not None
        monkeypatch.setattr(NativeJournal, "delete", original)
        if restart:
            await backend.aclose()
            backend = MxcSandboxBackend(config)
        try:
            if scope:
                result = await backend.dispose_scope(KEY.scope, KEY.thread_id)
                assert result.disposed == 1 and result.undisposed is None
            else:
                assert await backend.dispose(KEY, kind=SPEC.kind, instance_id=session) is None
            assert not list((config.root / "scratch").iterdir())
            assert (
                await backend.work(
                    lambda c: c.sentinel.db.execute("SELECT count(*) FROM launches").fetchone()[0]
                )
                == 0
            )
            assert (
                await backend.work(
                    lambda c: c.sentinel.db.execute("SELECT count(*) FROM reservations").fetchone()[
                        0
                    ]
                )
                == 0
            )
        finally:
            await backend.aclose()

    asyncio.run(check())


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
                assert replacement.state.alive
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
                assert not sandbox.state.busy
        finally:
            await backend.aclose()

    asyncio.run(check())
