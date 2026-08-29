"""Live tests against a real ACA sandbox group and a real sandbox image.

Skipped without ``ACAS_SANDBOX_ENDPOINT``, so it is inert for anyone without a sandbox group.
``MAF_SANDBOX_ACAS_E2E_IMAGE`` gates only the probes that need an image this deployment
*imported*: the prebuilt-image probes name something the service already provides, and a group
that has imported nothing is exactly the deployment they exist for. Unlike ``test_docker_e2e.py``
this cannot run on
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

**It is the first exercise of four probes against the service.** ``maf_sandbox.conformance``
holds the pull surface to one shared set, four of which require
:data:`~maf_sandbox.Capability.FILES_LIST`. `maf-sandbox-acas` is the only backend that declares
it, so those four skip in ``test_docker_e2e.py`` — the only live suite a pull request can run —
and everywhere else a real backend has ever answered them. They are *not* unrun:
``test_acas_backend.py``'s ``TestTheSharedConformanceSuite`` puts all of them to a fake on every
pull request, and says in its own docstring that it is the closest available until a live run
exists. This is that run, and the difference is the whole point of the suite: a fake answers
what this package believes, and #139 and #142 were both the package believing wrong.

:class:`TestFilesOutAgainstTheRealService` asserts that none of them skipped here, because a
suite that quietly skips a third of itself is the shape of a green run that attacked nothing.

**Cost discipline.** Four sandboxes for the whole module, and two more only where
``MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE`` names one whose guest is not root — the non-root class
shares one, and the cold-refusal probe creates a second that the backend under test deletes. The probes and refusals share one,
acquired by a module-scoped fixture and disposed at the end; the lifecycle test needs its own
because it disposes as the thing under test; the prebuilt-image test needs its own because a
bare catalogue name is only evidence if it boots one; the egress leg needs its own because only
an ``ALLOWLIST`` sandbox has a host it may reach and a host it may not. Everything runs on one
event loop, deliberately: the backend caches its group client per loop, so a second loop would
build a second transport against the same sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Coroutine
from typing import Any

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    SandboxKey,
    SandboxSpec,
    SandboxTransferCapExceeded,
    guest_run_layout,
    launcher_script,
)
from maf_sandbox.conformance import (
    FILES_DELETE_PROBES,
    FILES_OUT_PROBES,
    ConformancePaths,
    PosixGuestSubject,
    assert_egress_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
)

# Feature-detected, not floored: the published-cores gate runs this suite against every
# core the range admits, and cores before 0.23 have no Sandbox.reclaim to conform to.
try:
    from maf_sandbox.conformance import assert_reclaim_conformance
except ImportError:
    assert_reclaim_conformance = None

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

_ENDPOINT = os.environ.get("ACAS_SANDBOX_ENDPOINT")
#: A bare `repository:tag` in the configured registry, as the samples pass. Read from the
#: environment rather than written down here so a local tag never becomes a committed one; any
#: Linux image with `sh`, `ln`, `cat`, `mkfifo` and — for the egress leg — `curl` will do.
_IMAGE = os.environ.get("MAF_SANDBOX_ACAS_E2E_IMAGE")

pytestmark = pytest.mark.skipif(
    not _ENDPOINT,
    reason="needs ACAS_SANDBOX_ENDPOINT and an Azure identity the sandbox group accepts",
)

_WORK = "/maf-sandbox/work"
#: Short enough that the fifo probe finishes in seconds rather than the two minutes the shipped
#: default allows, and long enough that a slow control plane is not mistaken for a hang.
_READ_TIMEOUT = 20.0
_EXEC_TIMEOUT = 60.0

#: The egress probe's hosts (#402). Allowed is the AVM registry the bicep kind reaches;
#: denied is any host off the allowlist. The image the group imports must carry `curl`.
_EGRESS_ALLOWED_HOST = "mcr.microsoft.com"
_EGRESS_DENIED_URL = "https://pypi.org/simple/"


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
    """The shared spec, and the one place the imported image is required.

    The skip lives here rather than in the module's marker so that a sandbox group which has
    imported nothing still runs the prebuilt-image probes — the deployment those exist for is
    precisely the one with no import to name.
    """
    if not _IMAGE:
        pytest.skip(
            "needs MAF_SANDBOX_ACAS_E2E_IMAGE — a repository:tag imported into the sandbox group"
        )
    return SandboxSpec(kind="e2e", image=_IMAGE, work_dir=_WORK, **overrides)


def _logged_sandbox_id(message: str) -> str:
    """The id out of a lifecycle line, which spells it `id=<sandbox>` before its other fields."""
    return message.split("id=", 1)[1].split(" ", 1)[0]


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
    try:
        # Inside the guard, not above it. `_get_or_create` registers the id only after the
        # long-running create returns, so an acquire that creates the sandbox and *then* fails
        # — a transport drop, a poller timeout — leaves a running billable microVM this process
        # never learned the id of. dispose_scope finds it anyway, by label, which is the whole
        # reason it is the teardown here; it cannot do that from outside the try.
        sandbox = loop.run_until_complete(backend.acquire(key, _spec()))
        yield _Live(loop, backend, key, sandbox)
    finally:
        loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
        loop.run_until_complete(backend.aclose())


async def _drains_to_empty(
    backend: AcasSandboxBackend, scope: str, *, attempts: int = 10, delay: float = 6.0
) -> int:
    """Purge ``scope`` until the service reports nothing left; return the rounds it took.

    The claim is convergence, not a single reading: deletion is asynchronous here, so the first
    round can legitimately find a sandbox that is already terminating and delete it again.
    What must not happen is that it never empties.
    """
    for round_number in range(1, attempts + 1):
        if (await backend.dispose_scope(scope, "thread-1")).disposed == 0:
            return round_number
        await asyncio.sleep(delay)
    raise AssertionError(
        f"{scope} still had sandboxes after {attempts} purges over "
        f"{attempts * delay:.0f}s; teardown is not reaching the service"
    )


def _subject(live: _Live) -> PosixGuestSubject:
    return PosixGuestSubject(
        sandbox=live.sandbox,
        working_directory=_WORK,
        # The backend's own frozenset, not a narrower one: passing less is how a run skips the
        # probes that matter and reports success anyway.
        capabilities=live.backend.declarations.capabilities,
        exec_timeout=_EXEC_TIMEOUT,
    )


class TestALiveSandbox:
    """Acquire, run, reuse, and leave nothing behind — read back from the service."""

    def test_write_exec_reuse_and_dispose_round_trip(self, loop):
        scope = f"e2e-{uuid.uuid4()}"
        backend = AcasSandboxBackend(_config())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())

            await sandbox.write_file(
                f"{_WORK}/nested/deep/main.txt", "param naïve string\n", working_directory=_WORK
            )
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
            # the service by label rather than this process's memory. A suspended-but-undeleted
            # sandbox is still found there, which is the thing worth proving does not happen.
            #
            # Polled rather than asserted once: `_delete` calls `begin_delete()` and never
            # awaits the poller, so `dispose` above only *starts* the deletion and a sandbox
            # still terminating is legitimately still listed. Asserting zero on the first call
            # would be a race that reads as a teardown regression when it loses.
            fresh = AcasSandboxBackend(_config())
            try:
                rounds = await _drains_to_empty(fresh, scope)
            finally:
                await fresh.aclose()
            assert rounds >= 1

        try:
            loop.run_until_complete(scenario())
        finally:
            loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
            loop.run_until_complete(backend.aclose())


@pytest.fixture(scope="module")
def probe_results(live):
    """One conformance run, shared. Planting the hostile layout twice buys nothing."""
    return live.run(assert_files_out_conformance(_subject(live)))


@pytest.fixture(scope="module")
def files_in_results(live):
    """One FILES_IN run, shared with the write-fidelity assertions below."""
    return live.run(assert_files_in_conformance(_subject(live)))


@pytest.fixture(scope="module")
def files_delete_results(live):
    """The FILES_DELETE probes, against the capability this backend declares."""
    return live.run(assert_files_delete_conformance(_subject(live)))


@pytest.fixture(scope="module")
def service_link_delete(live):
    """What the *service* does with a link on delete, measured below :meth:`remove`.

    ``remove`` refuses a link before the service is ever called, so the gated probe
    ``a-link-is-removed-never-followed`` measures that refusal rather than the service — and
    the refusal exists because nothing had measured the service. Only a call to the SDK's own
    ``delete_file`` breaks that circle, so this reaches past the backend to make it.

    One dictionary of measured facts, asserted by the class below.
    """
    paths = ConformancePaths.under(_WORK)
    sc = live.sandbox._sc  # noqa: SLF001 — reaching past the backend is the whole measurement

    async def sh(*argv: str) -> int:
        result = await live.sandbox.exec(list(argv), working_directory="/", timeout=_EXEC_TIMEOUT)
        return result.exit_code

    async def survives(path: str) -> bool:
        return await sh("test", "-e", path) == 0

    async def plant_link(link: str, target: str) -> None:
        made = await live.sandbox.exec(
            ["ln", "-s", target, link], working_directory="/", timeout=_EXEC_TIMEOUT
        )
        assert made.exit_code == 0, f"could not plant {link}: {made.stderr}"
        # Proven, never assumed: a delete measured over a *copy* of the target would report
        # "the target survived" for the wrong reason and read as the safe answer.
        read_back = await live.sandbox.exec(
            ["readlink", link], working_directory="/", timeout=_EXEC_TIMEOUT
        )
        assert read_back.exit_code == 0 and read_back.stdout.strip() == target, (
            f"{link} is not a symlink to {target}: {read_back.stdout!r}"
        )

    async def scenario() -> dict[str, bool]:
        measured: dict[str, bool] = {}
        # Its own layout: this fixture must not depend on another module fixture having run,
        # and `ln` needs the directory before `write_file` has had cause to create it.
        assert await sh("mkdir", "-p", paths.work, paths.outside) == 0

        # Both flag values: `recursive` may select a different server-side operation entirely,
        # so a service that unlinks safely on one and resolves on the other would pass a
        # measurement that only asked once.
        for name, recursive in (("svc-link-file", False), ("svc-link-file-rec", True)):
            target = f"{paths.outside}/{name}-target.txt"
            await sc.write_file(target, b"outside the working directory\n")
            await plant_link(f"{paths.work}/{name}", target)
            await sc.delete_file(f"{paths.work}/{name}", recursive=recursive)
            measured[f"{name}-link-gone"] = not await survives(f"{paths.work}/{name}")
            measured[f"{name}-target-survives"] = await survives(target)

        # A link to a directory, where a resolving recursive delete would empty the target.
        await sc.write_file(f"{paths.outside}/svc-linked-dir/inside.txt", b"in the linked dir\n")
        await plant_link(f"{paths.work}/svc-link-dir", f"{paths.outside}/svc-linked-dir")
        await sc.delete_file(f"{paths.work}/svc-link-dir", recursive=True)
        measured["dir-link-gone"] = not await survives(f"{paths.work}/svc-link-dir")
        measured["dir-survives"] = await survives(f"{paths.outside}/svc-linked-dir")
        measured["dir-contents-survive"] = await survives(
            f"{paths.outside}/svc-linked-dir/inside.txt"
        )

        # The link as a *parent* component. POSIX resolves every component but the last, so a
        # service that did not would be the surprise here.
        await sc.write_file(f"{paths.outside}/svc-parent/child.txt", b"through a linked parent\n")
        await plant_link(f"{paths.work}/svc-link-parent", f"{paths.outside}/svc-parent")
        await sc.delete_file(f"{paths.work}/svc-link-parent/child.txt", recursive=False)
        measured["linked-parent-resolved"] = not await survives(
            f"{paths.outside}/svc-parent/child.txt"
        )

        # A link *inside* a recursively deleted tree: the shape `reclaim` actually faces, and
        # the one no argument the backend passes can name.
        await sc.write_file(f"{paths.outside}/svc-interior.txt", b"pointed at from inside\n")
        await sc.write_file(f"{paths.work}/svc-tree/leaf.txt", b"in the tree\n")
        await plant_link(f"{paths.work}/svc-tree/inside-link", f"{paths.outside}/svc-interior.txt")
        await sc.delete_file(f"{paths.work}/svc-tree", recursive=True)
        measured["tree-gone"] = not await survives(f"{paths.work}/svc-tree")
        measured["interior-target-survives"] = await survives(f"{paths.outside}/svc-interior.txt")
        return measured

    return live.run(scenario())


class TestWhatTheServiceDoesWithALinkOnDelete:
    """Regression coverage for the service link semantics behind FILES_DELETE and reclaim."""

    def test_a_link_named_directly_is_unlinked_and_its_target_kept(self, service_link_delete):
        """Both flag values — `recursive` may reach a different operation on the service."""
        for name in ("svc-link-file", "svc-link-file-rec"):
            assert service_link_delete[f"{name}-link-gone"], f"{name}: the link is still there"
            assert service_link_delete[f"{name}-target-survives"], (
                f"{name}: the service resolved the link and deleted the target — a guest "
                "choosing that target would delete outside the working directory"
            )

    def test_a_link_to_a_directory_is_unlinked_rather_than_emptied(self, service_link_delete):
        """The `rm -rf <link>/` shape: resolving here empties a tree the guest chose."""
        assert service_link_delete["dir-link-gone"], "the link to the directory is still there"
        assert service_link_delete["dir-survives"], "the linked directory itself was removed"
        assert service_link_delete["dir-contents-survive"], (
            "the service emptied the directory the link pointed at"
        )

    def test_a_linked_parent_is_resolved_which_is_why_the_backend_refuses_one(
        self, service_link_delete
    ):
        """Not a defect — POSIX resolves every component but the last, and so does this.

        It is the reason `_refuse_symlinked_parents` runs before the delete: the walk is the
        backend's to refuse, because the service will follow it.
        """
        assert service_link_delete["linked-parent-resolved"], (
            "the service did NOT resolve a linked parent — surprising rather than unsafe, but "
            "the backend's parent refusal is written against the opposite, so say so"
        )

    def test_a_link_inside_a_recursive_delete_is_unlinked_not_followed(self, service_link_delete):
        """What `reclaim` would face on the data plane: a link the caller never names."""
        assert service_link_delete["tree-gone"], "the recursively deleted tree is still there"
        assert service_link_delete["interior-target-survives"], (
            "a recursive delete resolved an interior link and removed a file outside the "
            "tree — the escape that keeps `reclaim` on `rm -rf` over `exec`"
        )


class TestFilesOutAgainstTheRealService:
    """The shared probes, against the service rather than a fake that agrees with this package."""

    def test_the_shared_probes_come_back_clean(self, probe_results):
        # assert_files_out_conformance raises ConformanceFailure naming every probe that failed,
        # so reaching here is the pass. This asserts the run happened at all.
        assert probe_results, "the conformance run returned no results"

    def test_no_probe_skipped_against_the_backend_that_declares_them(self, probe_results):
        """The coverage claim, measured rather than assumed.

        A skip is legitimate wherever `FILES_LIST` is not declared, so `run_files_out_probes`
        cannot refuse one the way it refuses a missing `FILES_OUT`. Against *this* backend it
        is not legitimate, so the assertion lives here — and it is what a green summary hides:
        four of these probes are the listing ones, and a run that skipped them would report
        exactly the same success as one that ran them.
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
            await live.sandbox.write_file(
                f"{_WORK}/out/report.txt", content, working_directory=_WORK
            )

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
            await live.sandbox.write_file(
                f"{_WORK}/out/big.txt", b"x" * 4096, working_directory=_WORK
            )
            with pytest.raises(SandboxTransferCapExceeded):
                await live.sandbox.read_file("out/big.txt", working_directory=_WORK, max_bytes=1024)

        live.run(scenario())

    def test_a_directory_is_refused_rather_than_read(self, live):
        async def scenario() -> None:
            await live.sandbox.write_file(
                f"{_WORK}/adir/child.txt", b"child\n", working_directory=_WORK
            )
            entry = await live.sandbox.stat_file("adir", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.DIRECTORY
            with pytest.raises(OSError) as refused:
                await live.sandbox.read_file("adir", working_directory=_WORK, max_bytes=1 << 20)
            # FileNotFoundError is an OSError, and `read_file` raises it when its own re-stat
            # comes back None — so a bare `raises(OSError)` passes on a directory that was
            # never there and the refusal under test never happens.
            assert not isinstance(refused.value, FileNotFoundError), (
                "the directory was missing, so this passed without refusing anything"
            )

        live.run(scenario())

    def test_a_missing_file_stats_as_none_and_reads_as_not_found(self, live):
        async def scenario() -> None:
            assert await live.sandbox.stat_file("out/absent.txt", working_directory=_WORK) is None
            with pytest.raises(FileNotFoundError):
                await live.sandbox.read_file(
                    "out/absent.txt", working_directory=_WORK, max_bytes=1 << 20
                )

        live.run(scenario())


class TestFilesInAgainstTheRealService:
    """The write surface's fidelity probes — bytes, UTF-8, replacement, implicit parents."""

    def test_the_files_in_probes_come_back_clean(self, files_in_results):
        results = files_in_results
        assert results, "the FILES_IN conformance run returned no results"
        skipped = {result.probe.name: result.skipped for result in results if result.skipped}
        assert not skipped, f"probes skipped against a backend that declares FILES_IN: {skipped}"


class TestFilesDeleteAgainstTheRealService:
    """The declared FILES_DELETE capability is exercised by the shared probes."""

    def test_every_delete_probe_reached_a_verdict(self, files_delete_results):
        results = files_delete_results
        assert results, "the FILES_DELETE conformance run returned no results"
        assert all(result.skipped is None for result in results)
        assert all(result.failure is None for result in results)
        assert len(results) == len(FILES_DELETE_PROBES)


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
            # This test plants its own directory. It used to rely on `out/` existing because a
            # test in another class had written into it first, so running this one alone made
            # `mkfifo` fail with "No such file or directory" — and the guard below then reported
            # a missing mkfifo and skipped green, dropping the only coverage of the read
            # timeout. A skip that misnames its own cause is worse than a failure.
            await live.sandbox.write_file(f"{_WORK}/out/.keep", b"", working_directory=_WORK)

            planted = await live.sandbox.exec(
                ["mkfifo", "out/pipe"], working_directory=_WORK, timeout=_EXEC_TIMEOUT
            )
            # Asserted, not skipped. `mkfifo` is a stated requirement of this harness the same
            # way `ln` is of PosixGuestSubject, which raises rather than skipping when it is
            # missing; an image that cannot plant the entry should stop the suite loudly.
            assert planted.exit_code == 0, (
                f"could not plant a fifo in the guest (exit {planted.exit_code}): "
                f"{planted.stderr.strip()}"
            )

            entry = await live.sandbox.stat_file("out/pipe", working_directory=_WORK)
            assert entry is not None, "the service did not report the fifo at all"
            # The premise, and a tripwire on it: a fifo indistinguishable from a regular file
            # is *why* the timeout is the only defence. `_stat_from_payload` can return only
            # SYMLINK, DIRECTORY or FILE, so this cannot come back as OTHER however the service
            # answers — if the payload ever grows a way to say "fifo", that helper is where it
            # would have to be read, and this assertion is what fails first.
            assert entry.kind is EntryKind.FILE

            with pytest.raises(TimeoutError):
                await live.sandbox.read_file("out/pipe", working_directory=_WORK, max_bytes=1 << 20)

        live.run(scenario())

    def test_a_non_normalized_working_directory_confines_the_same_way(self, live):
        """`/maf-sandbox/work/../work` is the same directory and a different string.

        Confinement compares guest paths, so a caller passing an unnormalized working directory
        must not widen what a relative path can reach.
        """

        async def scenario() -> None:
            await live.sandbox.write_file(
                f"{_WORK}/out/plain.txt", b"plain\n", working_directory=_WORK
            )
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


class TestWhetherThisBackendCouldServeHostTools:
    """Measures the one thing `Capability.HOST_TOOLS` would be a claim about here (#365).

    That capability is the only member of the enum with no backend method behind it: the
    transport is composed by the kind out of `exec`, `write_file`, `stat_file` and `read_file`,
    all covered by capabilities this backend already declares. What a backend would be adding is
    that its `exec` **detaches** — that a process started by one call outlives it and is still
    observable from the next — because `host_tool_calls_over_exec` is built on exactly that. The
    launcher returns immediately by design, and the appearance of the exit-code file is the only
    thing that tells the supervisor the run is over.

    This ran before the backend declared anything, to answer whether ACAS *could* — against the
    service rather than against a reading of the SDK, on the shared sandbox so it bills nothing
    extra. It passed, and #417 turned that answer into the declaration, so this is now the
    evidence the claim rests on: if a session stops keeping the process, the capability comes
    off rather than being reworded.
    """

    def test_the_guest_has_what_the_launcher_needs(self, live: _Live):
        """What the shipped `launcher_script` runs on, and nothing more.

        Deliberately not the interpreter: `launcher_script` takes that as a parameter, so which
        one a guest needs is the kind's requirement — codeact wants `python3` for its shim —
        and asserting it here would fail a backend over something `HOST_TOOLS` does not claim.
        A separate probe from the one below so a failure says which assumption broke.
        """
        wanted = "sh nohup printf mv"

        async def scenario():
            # The work directory is not in the image; the real flow creates it by writing the
            # program before it execs anything, so do the same rather than depend on some
            # earlier test in this module having left it behind.
            await live.sandbox.write_file(f"{_WORK}/probe/.keep", "", working_directory=_WORK)
            return await live.sandbox.exec(
                f'for t in {wanted}; do command -v "$t" >/dev/null || echo "$t"; done',
                working_directory=_WORK,
                timeout=_EXEC_TIMEOUT,
            )

        result = live.run(scenario())
        assert result.exit_code == 0, result.stderr
        assert result.stdout.split() == [], (
            f"the image is missing {result.stdout.split()}, so the shipped launcher cannot run "
            f"here even if exec detaches"
        )

    def test_a_detached_program_outlives_the_exec_that_started_it(self, live: _Live):
        """The real `launcher_script`, not an approximation of it, so this measures what would
        actually ship.

        Two facts, and only the pair discriminates. The exit marker must be **absent** when the
        launcher's exec returns — otherwise the exec waited for the program and the transport
        would deadlock against a supervisor that has not started — and it must **appear**
        afterwards, which is the survival this whole question is about.

        The program is shell rather than Python: whether `exec` detaches is a property of
        the backend, and pinning it to the interpreter the shim happens to need would
        report an image without Python as a backend that cannot detach. What the image
        must carry is the probe above. A session that resets
        between calls passes the first and fails the second.
        """
        layout = guest_run_layout(f"{_WORK}/{uuid.uuid4().hex[:12]}")
        # Comfortably longer than this control plane's per-call latency, so "the exec waited"
        # and "the exec detached" cannot be confused by a slow round trip; short enough to keep
        # the billable sandbox brief.
        program_seconds = 10

        async def scenario() -> None:
            await live.sandbox.write_file(
                layout.program,
                f"sleep {program_seconds}\necho the program finished\n",
                working_directory=_WORK,
            )
            await live.sandbox.write_file(
                layout.launcher,
                launcher_script(layout, interpreter="sh"),
                working_directory=_WORK,
            )

            started = asyncio.get_running_loop().time()
            launched = await live.sandbox.exec(
                f"sh {layout.launcher}", working_directory=_WORK, timeout=_EXEC_TIMEOUT
            )
            returned_after = asyncio.get_running_loop().time() - started
            assert launched.exit_code == 0, launched.stderr

            early = await live.sandbox.stat_file(layout.exit_code, working_directory=_WORK)
            assert early is None, (
                f"the launcher's exec returned after {returned_after:.1f}s with the run already "
                f"over, so it waited for the program instead of detaching — the supervisor "
                f"would never see the program start"
            )

            deadline = asyncio.get_running_loop().time() + program_seconds + 30
            entry = None
            while asyncio.get_running_loop().time() < deadline:
                entry = await live.sandbox.stat_file(layout.exit_code, working_directory=_WORK)
                if entry is not None:
                    break
                await asyncio.sleep(1.0)

            assert entry is not None, (
                f"no exit marker after {program_seconds + 30}s: the detached program did not "
                f"survive the exec that started it, so this backend cannot serve HOST_TOOLS "
                f"over the shipped transport"
            )
            code = await live.sandbox.read_file(
                layout.exit_code, working_directory=_WORK, max_bytes=64
            )
            assert code.decode().strip() == "0", f"the program did not end cleanly: {code!r}"

            # The marker alone only proves the launcher reached its last line. This proves the
            # program itself ran to completion behind it.
            output = await live.sandbox.read_file(
                layout.output, working_directory=_WORK, max_bytes=1 << 16
            )
            assert output.decode().strip() == "the program finished", output

        live.run(scenario())


# ---------------------------------------------------------------------------
# The other image namespace — what the service prebuilt, which nobody imports
# ---------------------------------------------------------------------------

#: A name the service provides to every sandbox group, no import and no registry involved.
#: Overridable so a group that has lost this one can still run the suite, but the default is
#: deliberately the image the CodeAct kind wants: if Microsoft retires it, that is worth a red
#: run rather than a silent fallback to something without an interpreter.
_PREBUILT = os.environ.get("MAF_SANDBOX_ACAS_E2E_PREBUILT", "python-3.13")


class TestBootingAnImageTheServiceProvides:
    """A bare name reaches the catalogue, and the sandbox it boots is usable (#412).

    Everything else about this namespace can be checked against the SDK — which keyword carries
    which source, that the two are mutually exclusive, what the wire body looks like. What only
    the service can say is that a name with no registry and no tag boots at all, and that is the
    whole claim the feature rests on, so it is measured here rather than reasoned about.

    This costs **one more billable sandbox per live run** than the suite used to. That is the
    price of the claim being evidence: the alternative is a unit test asserting that the backend
    passes `disk=`, which would keep passing if the service stopped accepting it.
    """

    @pytest.fixture(scope="class")
    def prebuilt(self, loop):
        backend = AcasSandboxBackend(_config())
        scope = f"e2e-prebuilt-{uuid.uuid4()}"
        key = _key(scope)
        try:
            # Inside the guard for the reason the shared fixture gives: a create that returns
            # and then fails leaves a running microVM whose id this process never learned, and
            # dispose_scope is what finds it by label.
            sandbox = loop.run_until_complete(
                backend.acquire(key, SandboxSpec(kind="e2e-prebuilt", image=_PREBUILT))
            )
            yield _Live(loop, backend, key, sandbox)
        finally:
            loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
            loop.run_until_complete(backend.aclose())

    def test_the_catalogue_holds_the_name_this_suite_asks_for(self, loop):
        """A control-plane listing, so it costs nothing and separates two failures.

        If this passes and the boot below fails, the name was right and the create was wrong.
        If this fails, the catalogue moved and no amount of backend code would have helped.
        """
        from maf_sandbox_acas._images import resolve_prebuilt_image_name

        backend = AcasSandboxBackend(_config())

        async def scenario():
            # Inside the coroutine: the group client is cached per *running* loop, because an
            # azure-core async client binds its transport to the loop that built it. Asking for
            # one from synchronous code raises rather than quietly building a second.
            return await resolve_prebuilt_image_name(backend._group_client(), _PREBUILT)

        try:
            resolved = loop.run_until_complete(scenario())
        finally:
            loop.run_until_complete(backend.aclose())
        assert resolved == _PREBUILT

    def test_a_bare_name_boots_a_sandbox_that_runs_commands(self, prebuilt: _Live):
        """The claim itself: no import, no registry, and a live guest at the end of it."""
        result = prebuilt.run(
            prebuilt.sandbox.exec("echo booted", working_directory="/", timeout=_EXEC_TIMEOUT)
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "booted"

    def test_the_python_image_carries_the_interpreter_codeact_names(self, prebuilt: _Live):
        """Why this namespace is worth reaching: it retires #302's import prerequisite.

        Skipped rather than failed when the suite is pointed at some other prebuilt name — the
        interpreter is a property of `python-3.13`, not of the namespace.
        """
        if not _PREBUILT.startswith("python-"):
            pytest.skip(f"{_PREBUILT} is not one of the Python images")
        result = prebuilt.run(
            prebuilt.sandbox.exec("python3 --version", working_directory="/", timeout=_EXEC_TIMEOUT)
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip().startswith("Python 3."), result.stdout


# ---------------------------------------------------------------------------
# The image nothing else in this suite runs — one whose guest is not root
# ---------------------------------------------------------------------------

#: A `repository:tag` imported into the group whose `USER` is not root. Unset for almost every
#: deployment, and the leg skips: every sample and every other sandbox in this module runs a
#: root image, which is why nothing surfaced #722 until a probe went looking for it.
_NONROOT_IMAGE = os.environ.get("MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE")


class TestAnImageWhoseGuestIsNotRoot:
    """The acquire-time gate, and the wall it rests on, against the service (#722).

    Costs **two more billable sandboxes** when the environment names such an image, and nothing
    otherwise: the fixture's, and one `test_a_cold_refusal_deletes_the_sandbox_it_had_to_create`
    creates on a backend of its own. A warm refusal pays for neither — the uid is remembered per
    image, so the acquire that is refused never reaches a create — which is why the cold path
    cannot borrow the fixture's.
    """

    @pytest.fixture(scope="class")
    def nonroot(self, loop):
        if not _NONROOT_IMAGE:
            pytest.skip(
                "needs MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE — a repository:tag imported into the "
                "sandbox group whose USER is not root"
            )
        backend = AcasSandboxBackend(_config())
        scope = f"e2e-nonroot-{uuid.uuid4()}"
        key = _key(scope)
        spec = SandboxSpec(
            kind="e2e-nonroot",
            image=_NONROOT_IMAGE,
            work_dir=_WORK,
            requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
        )
        try:
            # Inside the guard for the reason the shared fixture gives.
            sandbox = loop.run_until_complete(backend.acquire(key, spec))
            yield _Live(loop, backend, key, sandbox)
        finally:
            loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
            loop.run_until_complete(backend.aclose())

    def test_the_image_this_leg_names_really_is_someone_elses(self, nonroot: _Live):
        """The control. Pointed at a root image, everything below would pass for free."""
        answered = nonroot.run(
            nonroot.sandbox.exec("id -u", working_directory="/", timeout=_EXEC_TIMEOUT)
        )
        assert answered.exit_code == 0, answered.stderr
        assert answered.stdout.strip() != "0", (
            "MAF_SANDBOX_ACAS_E2E_NONROOT_IMAGE names an image whose guest is root, so this "
            "leg would assert nothing"
        )

    def test_the_guest_cannot_create_anything_inside_what_the_file_plane_made(self, nonroot: _Live):
        """The wall itself, measured rather than quoted: one host write, one guest `mkdir`.

        This is the launcher's own first two steps, which is why the transport cannot start
        here — the call directory arrives root-owned and the run's work directory goes inside it.
        """
        guest_call = f"{_WORK}/{uuid.uuid4().hex[:12]}"
        nonroot.run(
            nonroot.sandbox.write_file(f"{guest_call}/given.txt", "in\n", working_directory=_WORK)
        )

        refused = nonroot.run(
            nonroot.sandbox.exec(
                ["mkdir", f"{guest_call}/work"], working_directory="/", timeout=_EXEC_TIMEOUT
            )
        )

        assert refused.exit_code != 0, "the guest created a directory the file plane owns"
        assert "denied" in refused.stderr.lower(), refused.stderr

    def test_a_workload_collecting_outputs_is_refused_at_acquire(self, nonroot: _Live):
        """And refused without a second sandbox: the uid the fixture's acquire read is what
        answers here, before any create."""
        from maf_sandbox import SandboxCapabilityNotSupported

        collecting = SandboxSpec(
            kind="e2e-nonroot-outputs",
            image=_NONROOT_IMAGE,
            work_dir=_WORK,
            requires=frozenset({Capability.EXEC, Capability.FILES_OUT}),
        )

        with pytest.raises(SandboxCapabilityNotSupported, match="files_out"):
            nonroot.run(nonroot.backend.acquire(nonroot.key, collecting))

    def test_a_cold_refusal_deletes_the_sandbox_it_had_to_create(self, nonroot: _Live, caplog):
        """A refusal that had to create a sandbox to reach its verdict still deletes it.

        Needs a backend of its own: the memo is per instance, so the fixture's has already
        probed and could not reach the cold path. The create is asserted rather than assumed —
        every other check here is equally true of a run that created nothing.
        """
        from maf_sandbox import SandboxCapabilityNotSupported

        cold = AcasSandboxBackend(_config())
        scope = f"e2e-nonroot-cold-{uuid.uuid4()}"
        key = _key(scope)
        collecting = SandboxSpec(
            kind="e2e-nonroot-cold",
            image=_NONROOT_IMAGE,
            work_dir=_WORK,
            requires=frozenset({Capability.EXEC, Capability.FILES_OUT}),
        )

        async def scenario() -> None:
            with pytest.raises(SandboxCapabilityNotSupported, match="files_out"):
                await cold.acquire(key, collecting)

            # The service accepted the delete: `_undeleted` is where an id whose delete failed
            # is kept, so an empty record is the backend saying the removal landed. Asserted
            # before the listing because it carries no eventual-consistency race.
            assert cold._registry == {}
            assert cold._undeleted == {}

            # And it really went away. Polled, not asserted once: `_delete` starts the deletion
            # without awaiting the poller, so a sandbox still terminating is legitimately still
            # listed. Read-only — a purge here would delete the very leak it is looking for.
            gc = cold._group_client()
            for _ in range(10):
                left = await cold._list_thread_sandbox_ids(gc, scope, "thread-1")
                if left == []:
                    return
                await asyncio.sleep(6.0)
            raise AssertionError(f"{scope} still has sandboxes: a refused acquire leaked one")

        try:
            with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
                nonroot.run(scenario())
            created = [
                r.getMessage() for r in caplog.records if "sandbox created" in r.getMessage()
            ]
            released = [
                r.getMessage() for r in caplog.records if "sandbox released" in r.getMessage()
            ]
            assert len(created) == 1, f"the refusal created no sandbox: {caplog.text}"
            assert len(released) == 1, f"the sandbox it created was not released: {caplog.text}"
            # `sandbox released` is logged only where `_delete` reported no failure, so the
            # pair naming one id is the service agreeing that the removal landed.
            assert _logged_sandbox_id(created[0]) == _logged_sandbox_id(released[0])
        finally:
            # Belt and braces: if the assertions above failed, something is still running.
            nonroot.run(cold.dispose_scope(scope, "thread-1"))
            nonroot.run(cold.aclose())


@pytest.fixture(scope="module")
def reclaim_results(live):
    """One RECLAIM run, shared — mirrors `probe_results` and `files_in_results` above."""
    if assert_reclaim_conformance is None:
        pytest.skip("this maf-sandbox predates Sandbox.reclaim (< 0.23)")
    return live.run(assert_reclaim_conformance(_subject(live)))


class TestReclaimAgainstTheRealService:
    """The framework's own removal, gated by no capability — every backend owes it.

    Unlike FILES_DELETE above, there is no measurement fallback and no withheld-capability
    answer: `reclaim` is mandatory, so this asserts rather than measures.
    """

    def test_the_reclaim_probes_come_back_clean(self, reclaim_results):
        results = reclaim_results
        assert results, "the RECLAIM conformance run returned no results"
        skipped = {result.probe.name: result.skipped for result in results if result.skipped}
        assert not skipped, f"probes skipped against reclaim, which no capability gates: {skipped}"


class TestExecAgainstTheRealService:
    """The run surface's probes — quoting, exit codes, the working directory, the timeout bound.

    **Last class on the shared sandbox, and it must stay last.** This backend survives the
    timeout probe (`asyncio.wait_for` bounds the host-side call; the guest keeps sleeping), so
    unlike docker's the sandbox need not be a fresh one — but the sleeping guest is still in
    there afterwards, so nothing that measures guest state may follow it on `live`. Cost
    discipline stays as it was: one more probe set on the one sandbox the module already pays
    for, disposed by the `live` fixture's teardown as before.
    """

    def test_the_exec_probes_come_back_clean(self, live):
        results = live.run(assert_exec_conformance(_subject(live)))
        assert results, "the EXEC conformance run returned no results"
        skipped = {result.probe.name: result.skipped for result in results if result.skipped}
        assert not skipped, f"probes skipped against a backend that declares EXEC: {skipped}"


@pytest.fixture(scope="module")
def live_allowlist(loop):
    """The module's fourth billable sandbox, with an allowlist, for the egress probe (#402).

    Separate from `live`, which is CLOSED: the egress probe needs a host the guest may reach and
    one it may not, which only an `ALLOWLIST` sandbox provides. The image the group imports must
    carry `curl`. Disposed by label in the `finally`, for the reason `live` documents.
    """
    if not _IMAGE:
        pytest.skip("needs MAF_SANDBOX_ACAS_E2E_IMAGE (with curl) for the egress probe")
    backend = AcasSandboxBackend(_config())
    scope = f"e2e-egress-{uuid.uuid4()}"
    key = _key(scope)
    spec = _spec(egress=Egress.ALLOWLIST, egress_allow=(_EGRESS_ALLOWED_HOST,))
    try:
        sandbox = loop.run_until_complete(backend.acquire(key, spec))
        yield _Live(loop, backend, key, sandbox)
    finally:
        loop.run_until_complete(backend.dispose_scope(scope, "thread-1"))
        loop.run_until_complete(backend.aclose())


class TestEgressAgainstTheRealService:
    """Evidence this backend *enforces* its allowlist, not merely asks for it (#402).

    The deny is L7 here — a TLS-terminating proxy answers a denied host rather than the network
    being severed — so the shared probe asserts only the outcome an L3 and an L7 backend must
    share: the guest reaches an allowed host and not a denied one. The proxy's `x-deny-reason`
    and the method scoping it hints at are #377's, not asserted here.
    """

    def test_the_shared_egress_probe_comes_back_clean(self, live_allowlist):
        results = live_allowlist.run(
            assert_egress_conformance(
                _subject(live_allowlist),
                allowed_url=f"https://{_EGRESS_ALLOWED_HOST}/v2/",
                denied_url=_EGRESS_DENIED_URL,
                exec_timeout=_EXEC_TIMEOUT,
            )
        )
        assert results, "the egress conformance run returned no results"
        assert all(r.passed for r in results), [r.failure for r in results if r.failure]
