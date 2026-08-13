"""Pins the behavior sample 09's ``NoIsolationBackend`` actually carries.

The sample is not a uv workspace member and not in any ``testpaths``, so this is the one
repo-level test that imports it — by putting the sample directory on ``sys.path`` — and
exercises the load-bearing parts a reader cannot see from the docstrings alone:

* ``exec`` runs a real subprocess off the event loop (a worker thread), so concurrent tool
  calls do not serialize on one ``subprocess.run`` blocking the loop.
* the guest ``work_dir`` a kind embeds in its command is rewritten to the host root, in **both**
  forms the protocol permits — the string form a kind builds, and the argv list a caller passes.
* ``acquire`` is get-or-create keyed by ``(SandboxKey, spec.kind)``, and ``dispose_scope`` tears
  the host directories down.

The binary run is ``sys.executable`` — always present, no install — so the test runs anywhere
the suite does, without the bicep CLI. Async tests follow the repo convention: a synchronous
``def test_*`` that drives one ``asyncio.run`` rather than an async marker (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from maf_sandbox import Egress, Isolation, SandboxKey, SandboxSpec

_SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "09_inprocess_bicep"
sys.path.insert(0, str(_SAMPLE))

from no_isolation_backend import NoIsolationBackend, NoIsolationSandbox  # noqa: E402

_GUEST_WORK_DIR = (
    "/acas/work"  # the bicep kind's constant — a path that is not real on the host
)


def _key(agent_dir: str = "devops-engineer") -> SandboxKey:
    return SandboxKey(scope="test-scope", thread_id="test-thread", agent_dir=agent_dir)


def _spec(kind: str = "bicep") -> SandboxSpec:
    return SandboxSpec(kind=kind, work_dir=_GUEST_WORK_DIR)


async def _fresh() -> tuple[NoIsolationBackend, NoIsolationSandbox]:
    backend = NoIsolationBackend()
    sandbox = await backend.acquire(_key(), _spec())
    return backend, sandbox


async def _drop(backend: NoIsolationBackend) -> None:
    await backend.dispose_scope("test-scope", "test-thread")
    await backend.dispose_scope("other-scope", "other-thread")


def test_write_file_places_content_under_the_host_root():
    """A guest path under ``work_dir`` maps to ``host_root/<rel>`` on disk."""

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            await sandbox.write_file(
                f"{_GUEST_WORK_DIR}/main.bicep", "param location string"
            )
            host_file = sandbox._host_root / "main.bicep"  # noqa: SLF001
            assert host_file.read_text(encoding="utf-8") == "param location string"
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_host_path_rejects_a_guest_path_that_escapes_the_root():
    """A ``..`` segment that climbs above the work directory is refused, not written outside it.

    ``PurePosixPath.relative_to`` is lexical and keeps ``..``, so without a resolve-and-check a
    guest path like ``/acas/work/../../x`` would land outside the host root. The bicep kind
    rejects ``..`` before it reaches the backend, but the backend says paths stay *under* its
    root — so a path that escapes raises, and a ``..`` that resolves back under the root is fine.
    """

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            with pytest.raises(ValueError):
                await sandbox.write_file(f"{_GUEST_WORK_DIR}/../../escaped.bicep", "x")
            # A `..` that resolves back under the root is allowed.
            await sandbox.write_file(f"{_GUEST_WORK_DIR}/sub/../ok.bicep", "x")
            assert (sandbox._host_root / "ok.bicep").read_text(encoding="utf-8") == "x"  # noqa: SLF001
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_exec_translates_the_host_root_back_to_the_guest_work_dir():
    """The command is rewritten guest→host so the binary runs, then the output is reversed.

    A ``file://`` URI (or any path the binary prints) would carry the host temp root, which the
    workload cannot strip — it strips the *guest* ``work_dir``. So both output streams are
    translated host→guest before return. ``echo {guest_work_dir}`` becomes ``echo {host_root}``
    to the shell, and the host root it prints comes back as the guest work_dir.
    """

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            result = await sandbox.exec(
                f"echo {_GUEST_WORK_DIR}", working_directory=_GUEST_WORK_DIR, timeout=10
            )
            assert result.exit_code == 0
            assert _GUEST_WORK_DIR in result.stdout
            # The host temp root must not leak into the output the workload renders.
            assert str(sandbox._host_root) not in result.stdout  # noqa: SLF001
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_exec_sequence_form_translates_each_argv_element():
    """The argv form rewrites the guest work_dir in *each* element and runs without a shell.

    A script is written at a guest path, then run as ``[sys.executable, <guest path>]``. The
    element is rewritten to the host path (else the file is not found), proving the translation
    a kind that embeds ``work_dir`` in an argv list relies on.
    """

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            guest_script = f"{_GUEST_WORK_DIR}/echo.py"
            await sandbox.write_file(guest_script, 'print("argv-pinned")\n')
            result = await sandbox.exec(
                [sys.executable, guest_script],
                working_directory=_GUEST_WORK_DIR,
                timeout=10,
            )
            assert result.exit_code == 0, result.stderr
            assert "argv-pinned" in result.stdout
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_exec_does_not_block_the_event_loop():
    """``subprocess.run`` runs off the loop, so the loop stays responsive while a subprocess is alive.

    The pin for the ``asyncio.to_thread`` wrapping. A long subprocess is started as a task, then a
    short ``asyncio.sleep`` is awaited. If ``exec`` blocked the loop with a direct ``subprocess.run``,
    the sleep could not be scheduled until the subprocess returned (~0.4s); with the worker thread,
    it resolves in ~0.05s. The threshold sits between the two, with margin for a slow runner.
    """

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            loop = asyncio.get_event_loop()
            exec_task = asyncio.create_task(
                sandbox.exec(
                    [sys.executable, "-c", "import time; time.sleep(0.4)"],
                    working_directory=_GUEST_WORK_DIR,
                    timeout=5,
                )
            )
            t0 = loop.time()
            await asyncio.sleep(0.05)  # needs the loop; blocked if exec holds it
            elapsed = loop.time() - t0
            await exec_task
            assert elapsed < 0.3, (
                f"event loop was blocked for {elapsed:.2f}s during exec"
            )
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_exec_propagates_timeout_as_timeouterror():
    """A subprocess that overruns ``timeout`` raises ``TimeoutError``, the exception the workload catches."""

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            with pytest.raises(TimeoutError):
                await sandbox.exec(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    working_directory=_GUEST_WORK_DIR,
                    timeout=0.5,
                )
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_pull_surface_is_not_implemented():
    """The pull surface raises — the backend declares only EXEC and FILES_IN, so it never serves it.

    Each call is awaited inside its own ``pytest.raises`` rather than built into a tuple and
    iterated: the tuple form constructs all three coroutines before any is awaited, which a
    static analyzer reads as coroutines that may never run. The three explicit blocks make the
    await unconditional and keep the assertions identical.
    """

    async def body() -> None:
        backend, sandbox = await _fresh()
        try:
            with pytest.raises(NotImplementedError):
                await sandbox.stat_file(
                    f"{_GUEST_WORK_DIR}/x", working_directory=_GUEST_WORK_DIR
                )
            with pytest.raises(NotImplementedError):
                await sandbox.read_file(
                    f"{_GUEST_WORK_DIR}/x",
                    working_directory=_GUEST_WORK_DIR,
                    max_bytes=1,
                )
            with pytest.raises(NotImplementedError):
                await sandbox.list_dir(
                    f"{_GUEST_WORK_DIR}/x", working_directory=_GUEST_WORK_DIR
                )
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_acquire_is_get_or_create_keyed_by_scope_thread_kind():
    """Same (key, kind) reuses one sandbox; a different key or kind gets a new one."""

    async def body() -> None:
        backend = NoIsolationBackend()
        try:
            same = await backend.acquire(_key(), _spec(kind="bicep"))
            same_again = await backend.acquire(_key(), _spec(kind="bicep"))
            assert same is same_again

            other_agent = await backend.acquire(
                _key(agent_dir="other"), _spec(kind="bicep")
            )
            assert other_agent is not same

            other_kind = await backend.acquire(_key(), _spec(kind="diagram"))
            assert other_kind is not same
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_dispose_scope_removes_sandboxes_and_returns_count():
    """``dispose_scope`` drops every sandbox under the (scope, thread_id) pair and counts them."""

    async def body() -> None:
        backend = NoIsolationBackend()
        try:
            await backend.acquire(_key(), _spec(kind="bicep"))
            await backend.acquire(_key(), _spec(kind="diagram"))
            other_key = SandboxKey(
                scope="other-scope",
                thread_id="other-thread",
                agent_dir="devops-engineer",
            )
            await backend.acquire(other_key, _spec(kind="bicep"))

            deleted = await backend.dispose_scope("test-scope", "test-thread")
            assert deleted == 2
            # The other-scope sandbox survives.
            assert (other_key, "bicep") in backend._sandboxes  # noqa: SLF001
            remaining = await backend.dispose_scope("other-scope", "other-thread")
            assert remaining == 1
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_dispose_removes_the_host_directory():
    """``dispose`` deletes the host temp directory, not just the dict entry."""

    async def body() -> None:
        backend = NoIsolationBackend()
        try:
            sandbox = await backend.acquire(_key(), _spec())
            host_root = sandbox._host_root  # noqa: SLF001
            assert host_root.is_dir()
            await backend.dispose(_key())
            assert not host_root.exists()
        finally:
            await _drop(backend)

    asyncio.run(body())


def test_backend_declares_the_floor_and_temporary_egress():
    """The declarations the README and docstrings argue: PROCESS, CLOSED (temporary), no NETWORK."""
    b = NoIsolationBackend()
    assert b.isolation is Isolation.PROCESS
    # CLOSED is the temporary misuse, not enforced; #265 tracks switching back to UNRESTRICTED.
    assert b.egress is Egress.CLOSED
    assert "network" not in {c.value for c in b.capabilities}


def test_seed_files_reject_a_key_that_escapes_the_root():
    """A seed key with ``..`` or an absolute path is refused, not written outside the root.

    ``seed_files`` are placed under the host root the way ``write_file`` places a guest path;
    without the check, ``host_root / "/etc/x"`` discards the root (an absolute right side wins)
    and ``host_root / "../x"`` climbs above it. The sample only ever passes ``bicepconfig.json``
    (a safe relative key), but the backend's docstring says everything stays under its root, so
    a key that escapes raises before anything is written.
    """

    async def body() -> None:
        escaping = NoIsolationBackend(seed_files={"../escape.bicep": "x"})
        with pytest.raises(ValueError):
            await escaping.acquire(_key(), _spec())
        absolute = NoIsolationBackend(seed_files={"/etc/passwd": "x"})
        with pytest.raises(ValueError):
            await absolute.acquire(_key(), _spec())

    asyncio.run(body())


def test_acquire_removes_the_host_root_when_seeding_fails(monkeypatch, tmp_path):
    """A seeding failure removes the temp directory it just created — no leak on a half-build.

    ``mkdtemp`` creates the root before seeding runs; if a seed key then raises, the root is
    not yet in ``_sandboxes``, so disposal could never reach it. The except branch rmtrees it
    before re-raising. ``mkdtemp`` is patched to a path the test can see (and to actually create
    it, the way the real one does) — so without the cleanup the directory would persist and the
    assertion fail, and with it the directory is gone.
    """
    captured = tmp_path / "no-iso-root"

    def fake_mkdtemp(*, prefix=""):  # noqa: ARG001 — signature matches tempfile.mkdtemp
        captured.mkdir(parents=True, exist_ok=False)
        return str(captured)

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    backend = NoIsolationBackend(seed_files={"../escape.bicep": "x"})

    async def body() -> None:
        with pytest.raises(ValueError):
            await backend.acquire(_key(), _spec())
        assert not captured.exists()

    asyncio.run(body())
