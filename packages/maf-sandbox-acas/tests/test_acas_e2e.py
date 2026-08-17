"""Live tests against a real ACA sandbox group and a real sandbox image.

Skipped unless ``ACAS_SANDBOX_ENDPOINT`` and ``MAF_SANDBOX_ACAS_E2E_IMAGE`` are both set, so it
is inert for anyone without a sandbox group. Unlike ``test_docker_e2e.py`` this cannot run on
every pull request: every sandbox here is a billable Azure resource, so it is wired to the
``acas-e2e`` job in ``verify-live.yml`` and runs on demand and after a release that could change
what it exercises.

**Why it exists.** Until this suite, the only thing that ever touched the real ACAS data plane
was ``samples/01_acas_bicep`` and ``samples/03_acas_codeact`` — a sample, driven by a model,
gating a backend (#306). A sample proves a happy path; it cannot cover a refusal. Nothing
exercised the symlinked-parent walk, the cap refusal, the stat-then-read contract, or teardown
against the service rather than against this process's memory. This is also the backend where a
mocked test is least likely to match reality: #139 and #142 both turned on the difference
between what the SDK reports and what the payload actually carries, and ``_files_payload``
still reaches past the typed ``FileInfo`` for exactly that reason (#136).

**It is the first real exercise of four probes.** ``maf_sandbox.conformance`` holds the pull
surface to one shared set, four of which require :data:`~maf_sandbox.Capability.FILES_LIST`.
`maf-sandbox-acas` is the only backend that declares it, so those four have been skipping
everywhere they have ever run. :class:`TestFilesOutAgainstTheRealService` asserts that none of
them skipped here, because a suite that quietly skips a third of itself is the shape of a green
run that attacked nothing.

**Cost discipline.** Two sandboxes for the whole module. The probes and refusals share one,
acquired by a module-scoped fixture and disposed at the end; the lifecycle test needs its own
because it disposes as the thing under test. Everything runs on one event loop, deliberately:
the backend caches its group client per loop, so a second loop would build a second transport
against the same sandbox.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Coroutine
from typing import Any

import pytest
from maf_sandbox import (
    Capability,
    EntryKind,
    SandboxKey,
    SandboxSpec,
    SandboxTransferCapExceeded,
)
from maf_sandbox.conformance import (
    FILES_OUT_PROBES,
    PosixGuestSubject,
    assert_files_out_conformance,
)

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

_ENDPOINT = os.environ.get("ACAS_SANDBOX_ENDPOINT")
#: A bare `repository:tag` in the configured registry, as the samples pass. Read from the
#: environment rather than written down here so a local tag never becomes a committed one; any
#: Linux image with `sh`, `ln`, `cat` and `mkfifo` will do.
_IMAGE = os.environ.get("MAF_SANDBOX_ACAS_E2E_IMAGE")

pytestmark = pytest.mark.skipif(
    not _ENDPOINT or not _IMAGE,
    reason=(
        "needs ACAS_SANDBOX_ENDPOINT and MAF_SANDBOX_ACAS_E2E_IMAGE, and an Azure identity the "
        "sandbox group accepts"
    ),
)

_WORK = "/maf-sandbox/work"
#: Short enough that the fifo probe finishes in seconds rather than the two minutes the shipped
#: default allows, and long enough that a slow control plane is not mistaken for a hang.
_READ_TIMEOUT = 20.0
_EXEC_TIMEOUT = 60.0


def _config(**overrides: Any) -> AcasSandboxConfig:
    return AcasSandboxConfig(
        endpoint=_ENDPOINT or "",
        subscription_id=os.environ.get("ACAS_SANDBOX_SUBSCRIPTION_ID", ""),
        resource_group=os.environ.get("ACAS_SANDBOX_RESOURCE_GROUP", ""),
        sandbox_group=os.environ.get("ACAS_SANDBOX_GROUP", ""),
        registry=os.environ.get("ACAS_SANDBOX_REGISTRY", ""),
        read_timeout_seconds=_READ_TIMEOUT,
        **overrides,
    )


def _spec(**overrides: Any) -> SandboxSpec:
    return SandboxSpec(kind="e2e", image=_IMAGE, work_dir=_WORK, **overrides)


def _key(scope: str) -> SandboxKey:
    return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")


class _Live:
    """One acquired sandbox, its backend, and the loop all three are bound to."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        backend: AcasSandboxBackend,
        key: SandboxKey,
        sandbox: Any,
    ) -> None:
        self._loop = loop
        self.backend = backend
        self.key = key
        self.sandbox = sandbox

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        return self._loop.run_until_complete(coroutine)


@pytest.fixture(scope="module")
def loop():
    """One loop for the module.

    `AcasSandboxBackend` caches its group client per event loop — an azure-core async client
    binds its transport to the loop that created it — so `asyncio.run` per test, which the
    docker suite can afford, would build a fresh transport for every assertion against a
    sandbox created on a loop that is already closed.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture(scope="module")
def live(loop):
    """The shared sandbox. One billable resource for every probe and refusal below."""
    backend = AcasSandboxBackend(_config())
    scope = f"e2e-{uuid.uuid4()}"
    key = _key(scope)
    sandbox = loop.run_until_complete(backend.acquire(key, _spec()))
    try:
        yield _Live(loop, backend, key, sandbox)
    finally:
        # dispose_scope rather than dispose: it reads the service's own labels, so a sandbox
        # this process lost track of mid-run is still deleted rather than left to its timer.
        loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
        loop.run_until_complete(backend.aclose())


def _subject(live: _Live) -> PosixGuestSubject:
    return PosixGuestSubject(
        sandbox=live.sandbox,
        working_directory=_WORK,
        # The backend's own frozenset, not a narrower one: passing less is how a run skips the
        # probes that matter and reports success anyway.
        capabilities=live.backend.capabilities,
        exec_timeout=_EXEC_TIMEOUT,
    )


class TestALiveSandbox:
    """Acquire, run, reuse, and leave nothing behind — read back from the service."""

    def test_write_exec_reuse_and_dispose_round_trip(self, loop):
        scope = f"e2e-{uuid.uuid4()}"
        backend = AcasSandboxBackend(_config())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())

            await sandbox.write_file(f"{_WORK}/nested/deep/main.txt", "param naïve string\n")
            read_back = await sandbox.exec(
                ["cat", "nested/deep/main.txt"], working_directory=_WORK, timeout=_EXEC_TIMEOUT
            )
            assert read_back.exit_code == 0, read_back.stderr
            assert read_back.stdout == "param naïve string\n"

            failing = await sandbox.exec("exit 7", working_directory=_WORK, timeout=_EXEC_TIMEOUT)
            assert failing.exit_code == 7

            # `acquire` is get-or-create, and this backend is the warm-reuse one: a second
            # acquire on the same key must resume the same sandbox rather than bill a new one.
            warm = await backend.acquire(_key(scope), _spec())
            assert warm.sandbox_id == sandbox.sandbox_id

            await backend.dispose(_key(scope))

            # Read back through a *fresh* backend, which has an empty registry, so this asks
            # the service by label rather than this process's memory. Nothing left to purge is
            # the assertion; a suspended-but-undeleted sandbox would still be found here.
            fresh = AcasSandboxBackend(_config())
            try:
                assert await fresh.dispose_scope(scope, "thread-1") == 0
            finally:
                await fresh.aclose()

        try:
            loop.run_until_complete(scenario())
        finally:
            loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
            loop.run_until_complete(backend.aclose())


@pytest.fixture(scope="module")
def probe_results(live):
    """One conformance run, shared. Planting the hostile layout twice buys nothing."""
    return live.run(assert_files_out_conformance(_subject(live)))


class TestFilesOutAgainstTheRealService:
    """The shared probes, against the service rather than a fake that agrees with this package."""

    def test_the_shared_probes_come_back_clean(self, probe_results):
        # assert_files_out_conformance raises ConformanceFailure naming every probe that failed,
        # so reaching here is the pass. This asserts the run happened at all.
        assert probe_results, "the conformance run returned no results"

    def test_no_probe_skipped_so_the_listing_four_finally_ran(self, probe_results):
        """The coverage claim, measured rather than assumed.

        Four probes require `FILES_LIST` and this is the only backend that declares it, so
        until now they have skipped in every run they were part of. A suite that skips a third
        of itself and reports success is the failure `run_files_out_probes` refuses for
        `FILES_OUT` and cannot refuse for `FILES_LIST` — it is a legitimate skip everywhere
        else. Here it is not, so the assertion lives here.
        """
        results = probe_results
        skipped = {result.probe.name: result.skipped for result in results if result.skipped}
        assert not skipped, f"probes skipped against a backend that declares them: {skipped}"

        needs_listing = {
            probe.name for probe in FILES_OUT_PROBES if Capability.FILES_LIST in probe.requires
        }
        assert needs_listing, "no probe requires FILES_LIST; this test is measuring nothing"
        passed = {result.probe.name for result in results if result.passed}
        assert needs_listing <= passed, f"not run: {sorted(needs_listing - passed)}"

    def test_a_written_output_stats_and_reads_back_byte_identical(self, live):
        content = "diagnostics: naïve ✓\n".encode()

        async def scenario() -> None:
            await live.sandbox.write_file(f"{_WORK}/out/report.txt", content)

            entry = await live.sandbox.stat_file("out/report.txt", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.FILE
            assert entry.size_bytes == len(content)

            read = await live.sandbox.read_file(
                "out/report.txt", working_directory=_WORK, max_bytes=1 << 20
            )
            assert read == content

        live.run(scenario())

    def test_an_over_cap_output_is_refused_on_the_stat_before_its_content_moves(self, live):
        async def scenario() -> None:
            await live.sandbox.write_file(f"{_WORK}/out/big.txt", b"x" * 4096)
            with pytest.raises(SandboxTransferCapExceeded):
                await live.sandbox.read_file("out/big.txt", working_directory=_WORK, max_bytes=1024)

        live.run(scenario())

    def test_a_directory_is_refused_rather_than_read(self, live):
        async def scenario() -> None:
            await live.sandbox.write_file(f"{_WORK}/adir/child.txt", b"child\n")
            entry = await live.sandbox.stat_file("adir", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.DIRECTORY
            with pytest.raises(OSError):
                await live.sandbox.read_file("adir", working_directory=_WORK, max_bytes=1 << 20)

        live.run(scenario())

    def test_a_missing_file_stats_as_none_and_reads_as_not_found(self, live):
        async def scenario() -> None:
            assert await live.sandbox.stat_file("out/absent.txt", working_directory=_WORK) is None
            with pytest.raises(FileNotFoundError):
                await live.sandbox.read_file(
                    "out/absent.txt", working_directory=_WORK, max_bytes=1 << 20
                )

        live.run(scenario())


class TestWhatOnlyTheServiceCanSay:
    """Contracts written against the payload, where a mock agreeing with us proves nothing."""

    def test_a_fifo_is_refused_by_the_read_timeout_rather_than_hanging(self, live):
        """The case `read_file`'s own comment names, and the only place it can be checked.

        The service reports a FIFO exactly as an empty regular file — same mode, both type
        flags false — so the classification before the read cannot refuse one, and the read
        never returns. `read_timeout_seconds` is what turns hanging the caller's turn into a
        refusal, and a mock cannot demonstrate that because a mock decides what a stat says.
        """

        async def scenario() -> None:
            planted = await live.sandbox.exec(
                ["mkfifo", "out/pipe"], working_directory=_WORK, timeout=_EXEC_TIMEOUT
            )
            if planted.exit_code != 0:
                pytest.skip(f"the guest image has no usable mkfifo: {planted.stderr.strip()}")

            entry = await live.sandbox.stat_file("out/pipe", working_directory=_WORK)
            assert entry is not None, "the service did not report the fifo at all"
            # Recorded rather than asserted: if the service ever learns to report a fifo as
            # something other than a regular file, the timeout below stops being the only
            # defence and this test should be rewritten around the classification instead.
            assert entry.kind in (EntryKind.FILE, EntryKind.OTHER)

            with pytest.raises(TimeoutError):
                await live.sandbox.read_file("out/pipe", working_directory=_WORK, max_bytes=1 << 20)

        live.run(scenario())

    def test_a_non_normalized_working_directory_confines_the_same_way(self, live):
        """`/maf-sandbox/work/../work` is the same directory and a different string.

        Confinement compares guest paths, so a caller passing an unnormalized working directory
        must not widen what a relative path can reach.
        """

        async def scenario() -> None:
            await live.sandbox.write_file(f"{_WORK}/out/plain.txt", b"plain\n")
            odd = f"{_WORK}/../work"

            entry = await live.sandbox.stat_file("out/plain.txt", working_directory=odd)
            assert entry is not None
            assert entry.kind is EntryKind.FILE

            # ValueError is what `confine_guest_path` raises; the pull surface translates it to
            # SandboxOutputNotConfined only above this layer, so a backend call sees the bare
            # one. Asserting the type matters: a FileNotFoundError here would mean the path was
            # accepted and merely missed, which is a refusal that never happened.
            with pytest.raises(ValueError):
                await live.sandbox.read_file(
                    "../../etc/hostname", working_directory=odd, max_bytes=1 << 20
                )

        live.run(scenario())
