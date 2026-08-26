"""Live tests against a real ``docker`` engine and a real container image.

Skipped unless the ``docker`` client is on ``PATH`` and ``MAF_SANDBOX_DOCKER_E2E_IMAGE`` names
an image to run. Both are set by the ``docker-e2e`` job in ``tests.yml``, so this runs on
**every pull request** — the one live backend suite that can, since wslc needs Windows and acas
needs a billable Azure sandbox. Locally it runs where a developer sets the variables by hand.

It is the acceptance gate for the ``FILES_OUT`` protocol: the offline suite pins every command
line, and what is left to prove is that a real engine does what this backend believes — that a
declared output written by a workload stats and comes back byte-identical, that an over-cap
output is refused before its content moves, that a symlinked output is refused on the tar
entry's type bit, and that the shared probes in :mod:`maf_sandbox.conformance` come back clean
against a real daemon rather than against a fake that agrees with this package.

Deliberately lightweight: a tiny image, no model, seconds not minutes. The image is read from
the environment rather than written down here so a local tag never becomes a committed one; any
Linux image with ``sh``, ``sleep`` and a way to write a file will do.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import shutil
import subprocess
import uuid

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    HostToolRegistry,
    HostToolRun,
    Isolation,
    SandboxKey,
    SandboxProgramTimeout,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
    guest_run_layout,
    host_tool_calls_over_exec,
    launcher_script,
)
from maf_sandbox.conformance import (
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

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_IMAGE")
_PROXY_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE")
#: An image whose `USER` is not root and whose `work_dir` the build already carries, the shape
#: `images/bicep-sandbox/Dockerfile` has. Its own variable because every other image this suite
#: and the sample set run is root's (#680).
_NONROOT_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_NONROOT_IMAGE")
#: The same, but with `work_dir` owned by that user rather than root — the one ownership shape
#: root cannot empty once the container's capabilities are dropped.
_GUEST_OWNED_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_GUEST_OWNED_IMAGE")
#: The same again, but with the directory *above* `work_dir` given to that user — which is
#: what `reclaim` checks at acquire, because it owes no walk of its own.
_LOOSE_PARENT_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_LOOSE_PARENT_IMAGE")
#: What the images above put in `work_dir` at build time, so a reclaim can be shown to remove
#: its own directory and nothing beside it.
_CARRIED = "carried.json"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or not _IMAGE,
    reason="needs the docker client on PATH and MAF_SANDBOX_DOCKER_E2E_IMAGE naming a runnable image",
)

_WORK = "/maf-sandbox/work"


def _spec(**kw) -> SandboxSpec:
    return SandboxSpec(kind="e2e", image=_IMAGE, work_dir=_WORK, **kw)


def _key(scope: str) -> SandboxKey:
    return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")


def _names_on_the_machine(name: str) -> list[str]:
    """Every container currently named ``name``, read with docker rather than the backend."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line == name]


@pytest.mark.skipif(
    not _NONROOT_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_NONROOT_IMAGE naming an image whose USER is not root",
)
class TestAGuestThatIsNotRoot:
    """The file plane against an image whose ``USER`` is not root.

    Everything under ``work_dir`` arrives through ``docker cp`` or comes with the image, so it
    is root's; unlink permission comes from the containing directory. The cases below are the
    ownership shapes a call directory can have, plus the two properties that must not move.
    """

    def _spec(self, image: str | None = None) -> SandboxSpec:
        return SandboxSpec(kind="e2e-nonroot", image=image or _NONROOT_IMAGE, work_dir=_WORK)

    def _as_root(self, container: str, script: str) -> str:
        """One command in the container with root's authority, from outside the backend."""
        done = subprocess.run(
            ["docker", "exec", "--user", "0", container, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def test_reclaim_removes_a_call_directory_under_a_work_dir_the_image_carries(self):
        """The directory the image brought is root's before any write happens, so who created
        it is not what decides whether the call directory below it can be emptied. The file
        beside it is the control: `reclaim` promises its own directory and nothing else.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            # The protocol member the framework calls in its `finally`, not a command of this
            # suite's: what fails here is exactly what fails after a real tool call.
            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            assert self._as_root(sandbox.container_name, f"ls -A {_WORK}").split() == [_CARRIED]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_reclaim_removes_a_tree_the_two_principals_share(self):
        """The shape a real call leaves once a guest can write at all: the host's files beside
        the guest's, under one directory, removed in one walk.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/program.py", "print(1)\n", working_directory=_WORK
            )
            self._as_root(
                sandbox.container_name,
                f"mkdir -p {call_directory}/work && chown -R 10001:10001 {call_directory}/work",
            )
            wrote = await sandbox.exec(
                ["sh", "-c", f"echo mine > {call_directory}/work/output.txt"],
                working_directory="/",
                timeout=60,
            )
            assert wrote.exit_code == 0, wrote.stderr

            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            assert self._as_root(sandbox.container_name, f"ls -A {_WORK}").split() == [_CARRIED]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_remove_deletes_a_file_the_host_wrote(self):
        """The `FILES_DELETE` conformance probes all delete host-written files, which is why a
        root-only image set never showed which principal `remove` was running as.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            await sandbox.write_file(f"{_WORK}/doomed.txt", "x", working_directory=_WORK)

            await sandbox.remove("doomed.txt", working_directory=_WORK)

            assert await sandbox.stat_file("doomed.txt", working_directory=_WORK) is None

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_guest_command_still_runs_as_the_image_user(self):
        """The half that must not move: `exec` is the guest program's."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            whoami = await sandbox.exec(["id", "-u"], working_directory="/", timeout=60)
            assert whoami.exit_code == 0, whoami.stderr
            assert whoami.stdout.strip() not in ("", "0")

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_guest_cannot_rewrite_or_delete_what_the_host_wrote(self):
        """What the host puts in the call directory — the transport shim, the request and
        response files, the inputs a model was given — stays the host's. Rewriting the shim
        would be rewriting the mechanism the *next* call dispatches through, in the same warm
        sandbox.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            shim = f"{_WORK}/run0001/host_tools/maf_host_tools.py"
            await sandbox.write_file(shim, "# the real shim\n", working_directory=_WORK)

            rewritten = await sandbox.exec(
                ["sh", "-c", f"echo '# tampered' > {shim}"], working_directory="/", timeout=60
            )
            assert rewritten.exit_code != 0

            deleted = await sandbox.exec(
                ["sh", "-c", f"rm -f {shim}"], working_directory="/", timeout=60
            )
            assert deleted.exit_code != 0

            intact = await sandbox.read_file(shim, working_directory=_WORK, max_bytes=4096)
            assert intact == b"# the real shim\n"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _GUEST_OWNED_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_GUEST_OWNED_IMAGE naming a non-root image owning work_dir",
)
class TestAWorkDirTheImageGaveItsOwnUser:
    """The ownership shape the hardening advice behind a non-root ``USER`` also asks for.

    Two rules meet here. ``reclaim`` raises authority whatever the walk would say, so with
    ``cap_drop_all`` it meets a root that holds no ``CAP_DAC_OVERRIDE`` and can empty only
    what it owns — the case the retry exists for. ``remove`` owes a walk, which finds a
    component the guest owns and keeps the removal at the guest's own authority.
    """

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(kind="e2e-nocaps", image=_GUEST_OWNED_IMAGE, work_dir=_WORK)

    def test_reclaim_falls_back_to_the_image_user_when_root_is_refused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig(cap_drop_all=True))

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            left = await sandbox.exec(["ls", "-A", _WORK], working_directory="/", timeout=60)
            assert left.exit_code == 0, left.stderr
            assert left.stdout.split() == [_CARRIED]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_remove_runs_at_the_guest_authority_and_still_deletes(self):
        """`work_dir` is the guest's here, so the reach rule keeps the removal at the guest's
        own authority — and loses nothing by it, because a directory the guest can swap is one
        it can empty. No fallback is involved: root is never asked.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig(cap_drop_all=True))

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            await sandbox.write_file(f"{_WORK}/doomed.txt", "x", working_directory=_WORK)

            await sandbox.remove("doomed.txt", working_directory=_WORK)

            assert await sandbox.stat_file("doomed.txt", working_directory=_WORK) is None

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_remove_refuses_to_raise_authority_under_a_directory_the_guest_could_swap(self):
        """The rule's price, pinned rather than left implicit.

        A host-written subdirectory inside a guest-owned `work_dir` is one the guest could
        replace between the walk and the `rm`, so the removal stays at the guest's authority —
        which cannot empty it. It fails on `main` too, for the older reason that the removal
        was always the guest's; what is new is that this is now a decision rather than an
        accident of which principal happened to run.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            await sandbox.write_file(f"{_WORK}/sub/doomed.txt", "x", working_directory=_WORK)

            with pytest.raises(OSError):
                await sandbox.remove("sub/doomed.txt", working_directory=_WORK)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _LOOSE_PARENT_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_LOOSE_PARENT_IMAGE naming an image whose guest owns a "
    "directory above work_dir",
)
class TestAnImageThatGivesAwayADirectoryAboveWorkDir:
    """`reclaim` removes as root on an argument, and this is the half of it that is checked.

    A guest that can write above `work_dir` can replace `work_dir` itself with a link, and a
    swapped *parent* is followed rather than unlinked — so root there would delete what the
    guest could not. The check runs at acquire, because the chain is fixed before any guest
    does anything.
    """

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(kind="e2e-loose", image=_LOOSE_PARENT_IMAGE, work_dir=_WORK)

    def test_reclaim_drops_to_the_guest_authority_and_leaks_rather_than_reaching(self):
        """The rule's price on this image shape, and it is a real one.

        The call directory arrived through `docker cp`, so it is root's; the guest cannot
        empty it, and root is not allowed to here because this image lets the guest swap a
        directory above `work_dir`. So it stays — exactly as it does on `main`, where every
        removal was the guest's. This is the shape #680 does not fix rather than one it
        breaks, and the backend logs the reason at acquire.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            with pytest.raises(OSError):
                await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            left = await sandbox.exec(["ls", "-A", _WORK], working_directory="/", timeout=60)
            assert left.exit_code == 0, left.stderr
            assert sorted(left.stdout.split()) == sorted([_CARRIED, "abc123def456"])

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_swapped_work_dir_takes_the_removal_nowhere_it_could_not_reach(self):
        """The attack the check exists for, run for real.

        The guest replaces `work_dir` with a link to a directory it does not own, then the
        framework reclaims. Because the removal is the guest's rather than root's, what the
        redirected `rm` can delete is exactly what the guest could have deleted itself.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            planted = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "0",
                    sandbox.container_name,
                    "sh",
                    "-c",
                    "mkdir -p /victim/abc123def456 && echo treasure > /victim/abc123def456/t "
                    "&& chmod 755 /victim",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert planted.returncode == 0, planted.stderr

            swapped = await sandbox.exec(
                ["sh", "-c", f"mv {_WORK} {_WORK}.orig && ln -s /victim {_WORK}"],
                working_directory="/",
                timeout=60,
            )
            assert swapped.exit_code == 0, swapped.stderr

            with contextlib.suppress(OSError):
                await sandbox.reclaim(f"{_WORK}/abc123def456", working_directory=_WORK, timeout=60)

            survived = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "0",
                    sandbox.container_name,
                    "test",
                    "-f",
                    "/victim/abc123def456/t",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert survived.returncode == 0, "a root removal followed the swapped parent"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestALiveContainer:
    def test_write_exec_reuse_and_dispose_round_trip(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file(
                "/maf-sandbox/work/nested/deep/main.txt",
                "param naïve string\n",
                working_directory=_WORK,
            )

            read_back = await sandbox.exec(
                ["cat", "nested/deep/main.txt"], working_directory=_WORK, timeout=60
            )
            assert read_back.exit_code == 0, read_back.stderr
            assert read_back.stdout == "param naïve string\n"

            failing = await sandbox.exec("exit 7", working_directory=_WORK, timeout=60)
            assert failing.exit_code == 7

            warm = await backend.acquire(_key(scope), _spec())
            assert warm.container_name == sandbox.container_name

            await backend.dispose(_key(scope))
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_dispose_scope_finds_the_container_by_its_labels(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> str:
            sandbox = await backend.acquire(_key(scope), _spec())
            # Purged through a second backend: the labels on the container, not this process's
            # memory, are what a conversation delete has to find.
            purged = await DockerSandboxBackend(DockerSandboxConfig()).dispose_scope(
                scope, "thread-1"
            )
            assert purged >= 1
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestFilesOutAgainstARealEngine:
    """The acceptance gate: stat, read, cap refusal and symlink refusal on a live tar stream."""

    def test_a_written_output_stats_and_reads_back_byte_identical(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/maf-sandbox/work/out.png", payload, working_directory=_WORK)

            entry = await sandbox.stat_file("out.png", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.FILE
            assert entry.size_bytes == len(payload)

            got = await sandbox.read_file("out.png", working_directory=_WORK, max_bytes=1 << 20)
            assert got == payload

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_an_over_cap_output_is_refused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file(
                "/maf-sandbox/work/big.bin", b"x" * 5000, working_directory=_WORK
            )
            with pytest.raises(SandboxTransferCapExceeded):
                await sandbox.read_file("big.bin", working_directory=_WORK, max_bytes=100)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_symlinked_output_is_refused_at_stat_and_at_read(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            # A kind's first write creates the working directory (docker cp makes the parents);
            # exec's -w needs it to exist, exactly as it does for a real workload.
            await sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK)
            made = await sandbox.exec(
                ["ln", "-s", "/etc/passwd", "/maf-sandbox/work/link"],
                working_directory=_WORK,
                timeout=60,
            )
            assert made.exit_code == 0, made.stderr

            entry = await sandbox.stat_file("link", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.SYMLINK  # never FILE

            with pytest.raises(OSError):
                await sandbox.read_file("link", working_directory=_WORK, max_bytes=1 << 20)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_stat_or_read_through_a_symlinked_parent_is_refused(self):
        """``ln -sfn /etc /maf-sandbox/work/out``: the final entry stats as a regular file and reads /etc.

        The engine resolves the path daemon-side, so only the component walk sees the link.
        Both halves of the pull surface walk it: a stat moves no byte of ``/etc``, but it does
        report a type and a size from outside the working directory.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK)
            made = await sandbox.exec(
                ["ln", "-sfn", "/etc", "/maf-sandbox/work/out"], working_directory=_WORK, timeout=60
            )
            assert made.exit_code == 0, made.stderr

            # Through the unconfined stat the walk itself uses: this is the premise — a real
            # engine really does answer through the link — and the public surface now refuses it.
            escaped = await sandbox._stat_guest(f"{_WORK}/out/hostname", "out/hostname")
            assert escaped is not None
            assert escaped.kind is EntryKind.FILE  # the parent link is invisible here

            with pytest.raises(ValueError, match="real directory"):
                await sandbox.stat_file("out/hostname", working_directory=_WORK)
            with pytest.raises(ValueError, match="real directory"):
                await sandbox.read_file("out/hostname", working_directory=_WORK, max_bytes=1 << 20)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_it_answers_the_shared_conformance_probes(self):
        """`maf_sandbox.conformance`, against a real engine — the suite's whole point.

        Everything else in this class is this backend's own reading of the rule. These probes
        are the reading every backend serving `FILES_OUT` is held to, planted through the public
        surface and attacked there, so what passes is the daemon's real resolution behaviour
        rather than a fake that agrees with whoever wrote it (#142, #214).

        The premise stays at home, in `test_docker_backend.py`: only this package can ask its
        unconfined `_stat_guest` whether the engine really does answer through the link.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_files_out_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.capabilities,
                )
            )
            # The listing probes are skipped here and nowhere else: this backend does not
            # declare FILES_LIST, and a silent change to that would otherwise go green.
            skipped = {r.probe.name for r in results if r.skipped}
            assert skipped == {
                "listing-a-linked-directory",
                "listing-through-a-linked-parent",
                "listing-under-a-linked-ancestor",
                "a-listing-names-its-links",
            }

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestFilesInAgainstARealEngine:
    """`maf_sandbox.conformance`'s FILES_IN suite — the write surface, held to the shared probes."""

    def test_it_answers_the_files_in_probes(self):
        """`maf_sandbox.conformance`'s FILES_IN suite, against a real engine.

        The write surface this backend ships — tar in, `cp -` out — is what every kind's first
        act exercises, and the fidelity probes assert bytes, not shapes: a text-shaped hop in
        that transport corrupts a binary in-door in ways a caller cannot detect.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_files_in_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestExecAgainstARealEngine:
    """`maf_sandbox.conformance`'s EXEC suite — the run surface, held to the shared probes."""

    def test_it_answers_the_exec_probes_on_its_own_sandbox(self):
        """`maf_sandbox.conformance`'s EXEC suite, against a real engine.

        **On its own sandbox, acquired here**: the suite's last probe asserts the
        `TimeoutError` contract, and this backend discards the whole container to stop a hung
        command — the documented recovery, and the reason the sandbox cannot be shared with
        anything that runs after it. `dispose_scope` afterwards has to stay clean over a
        container the timeout already removed, which is half of what this test is for.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_exec_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestFilesDeleteAgainstARealEngine:
    """`maf_sandbox.conformance`'s FILES_DELETE suite — the removal rules, held to `rm` itself."""

    def test_it_answers_the_files_delete_probes(self):
        """`maf_sandbox.conformance`'s FILES_DELETE suite, against a real engine.

        This backend is the one that declared the capability, so the removal rules — a link
        removed never followed, a directory needing `recursive`, the working directory refused
        — are held to `rm`'s real behaviour rather than to this package's reading of it.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_files_delete_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestReclaimAgainstARealEngine:
    """`maf_sandbox.conformance`'s RECLAIM suite — gated by no capability, unlike FILES_DELETE
    above, so every backend owes the assert directly rather than a measurement."""

    def test_it_answers_the_reclaim_probes(self):
        if assert_reclaim_conformance is None:
            pytest.skip("this maf-sandbox predates Sandbox.reclaim (< 0.23)")
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            # The narrowing does not cross into this closure; the assert re-establishes it,
            # and the coverage check wants the suite called by name.
            assert assert_reclaim_conformance is not None
            results = await assert_reclaim_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestCollectOutputsAgainstARealEngine:
    """The whole pull surface, end to end: a kind declares an output and it lands."""

    def test_collect_outputs_lands_a_declared_output_through_the_router(self):
        """Exercises `collect_outputs` — the glue over `stat_file`/`read_file` — against a real
        engine, which is the shape a diagram-style kind uses.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

        from maf_sandbox import Artifact, DeclaredOutput, LandedArtifact, OutputSink

        landed: list[Artifact] = []

        async def deliver(artifact: Artifact) -> LandedArtifact:
            landed.append(artifact)
            return LandedArtifact(name=artifact.name, display=artifact.name)

        spec = _spec(
            requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            declared_outputs=(DeclaredOutput(path="result.txt", media_type="text/plain"),),
            files_out=TransferLimits(
                max_bytes_per_file=1 << 20, max_total_bytes=1 << 20, max_files=4
            ),
        )

        async def scenario() -> None:
            sandbox = await router.acquire(_key(scope), spec)
            # Seed the working directory the way a real kind does — its first write_file is what
            # creates /maf-sandbox/work before any exec -w into it.
            await sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK)
            await sandbox.exec(
                ["sh", "-c", "echo rendered > result.txt"], working_directory=_WORK, timeout=60
            )
            results = await collect_outputs(sandbox, spec, sink=OutputSink(deliver=deliver))
            assert [r.name for r in results] == ["result.txt"]
            assert landed[0].content == b"rendered\n"
            assert landed[0].media_type == "text/plain"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


def _network_present(name: str) -> bool:
    out = subprocess.run(
        ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return name in out.splitlines()


@pytest.mark.skipif(
    not _PROXY_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE naming a built proxy image (and curl in the image)",
)
class TestAllowlistEgress:
    """An allowed host is reachable, a denied one is not, and a DNS lookup for a denied host
    does not leak — the negative controls the design requires include the DNS case.
    """

    def _config(self) -> DockerSandboxConfig:
        return DockerSandboxConfig(egress_proxy_image=_PROXY_IMAGE)

    def _curl_status(self, sandbox, url: str) -> tuple[int, str]:
        result = asyncio.run(
            sandbox.exec(
                ["sh", "-c", f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 25 {url}"],
                working_directory=_WORK,
                timeout=45,
            )
        )
        return result.exit_code, result.stdout.strip()

    def test_allowed_reachable_denied_not_and_teardown_leaves_nothing(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(self._config())
        spec = _spec(egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",))

        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        net = sandbox.container_name + "-net"
        # A kind's first write creates the working directory that exec -w needs.
        asyncio.run(sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK))
        try:
            assert _network_present(net)
            subject = PosixGuestSubject(
                sandbox=sandbox,
                working_directory=_WORK,
                capabilities=backend.capabilities,
            )
            asyncio.run(
                assert_egress_conformance(
                    subject,
                    allowed_url="https://mcr.microsoft.com/v2/",
                    denied_url="https://pypi.org/simple/",
                )
            )
            # Docker-specific, and stronger than the shared contract: the deny is L3, so curl
            # cannot even open the tunnel and reports `000` — not an L7 proxy's HTTP answer.
            _, denied_status = self._curl_status(sandbox, "https://pypi.org/simple/")
            assert denied_status == "000", denied_status
        finally:
            purged = asyncio.run(backend.dispose_scope(scope, "thread-1"))
        assert purged == 1
        assert _names_on_the_machine(sandbox.container_name) == []
        assert not _network_present(net)


class TestWhetherThisBackendCouldServeHostTools:
    """Measures the one thing `Capability.HOST_TOOLS` would be a claim about here (#365).

    That capability is the only member of the enum with no backend method behind it: the
    transport is composed by the kind out of `exec`, `write_file`, `stat_file` and `read_file`,
    all covered by capabilities this backend already declares. What a backend would be adding is
    that its `exec` **detaches** — that a process started by one call outlives it and is still
    observable from the next — because `host_tool_calls_over_exec` is built on exactly that. The
    launcher returns immediately by design, and the appearance of the exit-code file is the only
    thing that tells the supervisor the run is over.

    Nothing here declares anything. This answers whether docker *could*, against a real engine.
    """

    def test_the_guest_has_what_the_launcher_needs(self):
        """What the shipped `launcher_script` runs on, and nothing more.

        Deliberately not the interpreter: `launcher_script` takes that as a parameter, so which
        one a guest needs is the kind's requirement — codeact wants `python3` for its shim —
        and asserting it here would fail a backend over something `HOST_TOOLS` does not claim.
        A separate probe from the one below so a failure says which assumption broke.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            try:
                # The work directory is not in the image; the real flow creates it by writing
                # the program before it execs anything, so do the same rather than depend on
                # some earlier test having left it behind.
                await sandbox.write_file(f"{_WORK}/probe/.keep", "", working_directory=_WORK)
                result = await sandbox.exec(
                    'for t in sh nohup printf mv; do command -v "$t" >/dev/null || echo "$t"; done',
                    working_directory=_WORK,
                    timeout=60,
                )
                assert result.exit_code == 0, result.stderr
                assert result.stdout.split() == [], (
                    f"the image is missing {result.stdout.split()}, so the shipped launcher "
                    f"cannot run here even if exec detaches"
                )
            finally:
                await backend.dispose(_key(scope))

        asyncio.run(scenario())

    def test_a_detached_program_outlives_the_exec_that_started_it(self):
        """The real `launcher_script`, not an approximation of it, so this measures what would
        actually ship.

        Two facts, and only the pair discriminates. The exit marker must be **absent** when the
        launcher's exec returns — otherwise the exec waited for the program and the transport
        would deadlock against a supervisor that has not started — and it must **appear**
        afterwards, which is the survival this whole question is about.

        The program is shell rather than Python: whether `exec` detaches is a property of
        the backend, and pinning it to the interpreter the shim happens to need would
        report an image without Python as a backend that cannot detach. What the image
        must carry is the probe above.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        layout = guest_run_layout(f"{_WORK}/{uuid.uuid4().hex[:12]}")
        program_seconds = 5

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            try:
                await sandbox.write_file(
                    layout.program,
                    f"sleep {program_seconds}\necho the program finished\n",
                    working_directory=_WORK,
                )
                await sandbox.write_file(
                    layout.launcher,
                    launcher_script(layout, interpreter="sh"),
                    working_directory=_WORK,
                )

                loop = asyncio.get_running_loop()
                started = loop.time()
                launched = await sandbox.exec(
                    f"sh {layout.launcher}", working_directory=_WORK, timeout=60
                )
                returned_after = loop.time() - started
                assert launched.exit_code == 0, launched.stderr

                early = await sandbox.stat_file(layout.exit_code, working_directory=_WORK)
                assert early is None, (
                    f"the launcher's exec returned after {returned_after:.1f}s with the run "
                    f"already over, so it waited for the program instead of detaching — the "
                    f"supervisor would never see the program start"
                )

                deadline = loop.time() + program_seconds + 30
                entry = None
                while loop.time() < deadline:
                    entry = await sandbox.stat_file(layout.exit_code, working_directory=_WORK)
                    if entry is not None:
                        break
                    await asyncio.sleep(0.5)

                assert entry is not None, (
                    f"no exit marker after {program_seconds + 30}s: the detached program did "
                    f"not survive the exec that started it, so this backend cannot serve "
                    f"HOST_TOOLS over the shipped transport"
                )
                code = await sandbox.read_file(
                    layout.exit_code, working_directory=_WORK, max_bytes=64
                )
                assert code.decode().strip() == "0", f"the program did not end cleanly: {code!r}"

                # The marker alone only proves the launcher reached its last line. This proves
                # the program itself ran to completion behind it.
                output = await sandbox.read_file(
                    layout.output, working_directory=_WORK, max_bytes=1 << 16
                )
                assert output.decode().strip() == "the program finished", output
            finally:
                await backend.dispose(_key(scope))

        asyncio.run(scenario())


class TestWhatOnlyARealRunawayCanSettle:
    """A live runaway timed out through `host_tool_calls_over_exec` is gone when it returns.

    It proves both the process and the transport files are really gone in a container.
    """

    def test_a_runaway_is_dead_and_its_files_are_gone_when_the_call_returns(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        layout = guest_run_layout(f"{_WORK}/{uuid.uuid4().hex[:12]}")

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            try:
                # The program records its own pid in `work`, which the transport does not
                # reclaim, so the check below needs nothing beyond the `kill` this transport
                # already requires. `pgrep` would be wrong twice over: absent on a minimal
                # image it takes the `|| echo gone` branch and passes without checking
                # anything, and `-f` matches the probe's own command line.
                await sandbox.write_file(
                    layout.program,
                    f"echo $$ > {layout.work}/program.pid\nwhile true; do :; done\n",
                    working_directory=_WORK,
                )
                await sandbox.write_file(layout.shim, "", working_directory=_WORK)

                with pytest.raises(SandboxProgramTimeout) as expired:
                    await host_tool_calls_over_exec(
                        sandbox,
                        HostToolRun(HostToolRegistry()),
                        layout,
                        timeout=5.0,
                        poll_interval=0.5,
                        interpreter="sh",
                    )

                # 1. It says it stopped the program: `signal` is whether the kill landed,
                #    `reach` how wide it went. "nothing" would mean nothing was stopped.
                assert expired.value.signal == "sent", expired.value
                assert expired.value.reach in {"group", "program"}, expired.value

                # 2. And it is true of the process itself. Read the pid the program wrote,
                #    then ask the kernel — a missing file or an unreadable pid fails here
                #    rather than reading as success.
                recorded = await sandbox.exec(
                    f"cat {layout.work}/program.pid",
                    working_directory=_WORK,
                    timeout=60,
                )
                assert recorded.exit_code == 0, (
                    f"the program never recorded its pid, so this proves nothing: "
                    f"{recorded.stderr!r}"
                )
                pid = recorded.stdout.strip()
                assert pid.isdigit(), f"not a pid: {pid!r}"

                alive = await sandbox.exec(
                    (
                        f"if kill -0 {pid} 2>/dev/null; then "
                        f"state=$(awk '/^State:/ {{print $2}}' /proc/{pid}/status 2>/dev/null || true); "
                        f'if [ "$state" = Z ]; then echo gone; '
                        f'elif [ -n "$state" ]; then echo alive; '
                        f"else echo gone; fi; "
                        f"else echo gone; fi"
                    ),
                    working_directory=_WORK,
                    timeout=60,
                )
                assert alive.stdout.strip() == "gone", (
                    f"pid {pid} survived a timeout the transport reported as signalled"
                )

                # 3. The transport's own files are gone from the filesystem, not just from a
                #    list of commands somebody issued.
                served = posixpath.dirname(layout.shim)
                listed = await sandbox.exec(
                    f"if [ -d {served} ]; then ls -A {served}; else :; fi",
                    working_directory=_WORK,
                    timeout=60,
                )
                assert listed.exit_code == 0, (
                    f"could not inspect transport directory {served}: {listed.stderr!r}"
                )
                assert listed.stdout.strip() == "", (
                    f"the transport left files behind in {served}: {listed.stdout!r}"
                )
            finally:
                await backend.dispose(_key(scope))

        asyncio.run(scenario())
