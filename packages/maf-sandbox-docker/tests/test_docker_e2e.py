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
import errno
import http.server
import ipaddress
import json
import os
import posixpath
import shutil
import socket
import subprocess
import threading
import uuid
from typing import Any

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    HostToolRegistry,
    HostToolRun,
    Isolation,
    OsFamily,
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
#: `images/bicep-sandbox/Dockerfile` has. Every other image this suite runs is root's (#680).
_NONROOT_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_NONROOT_IMAGE")
#: The same, but with `work_dir` owned by that user rather than root — the one ownership shape
#: root cannot empty once the container's capabilities are dropped.
_GUEST_OWNED_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_GUEST_OWNED_IMAGE")
#: The same again, but with the directory *above* `work_dir` given to that user — which is
#: what `reclaim` checks at acquire, because it owes no walk of its own.
_LOOSE_PARENT_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_LOOSE_PARENT_IMAGE")
#: An image whose `USER` is a *name* rather than a numeric pair, so uid and gid come from the
#: container's own `/etc/passwd`. Its gid is not its uid, which is what lets a gid that was read
#: be told apart from one guessed to equal the uid.
_NAMED_USER_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_NAMED_USER_IMAGE")
#: The same named user, but carrying `/maf-sandbox` with no `work_dir` under it: the one shape
#: where `write_file` has to send `work_dir` itself as an entry rather than let docker make it.
_ABSENT_WORK_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_ABSENT_WORK_IMAGE")
#: What the images above put in `work_dir` at build time: the control for a reclaim.
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
    """The file plane against an image whose ``USER`` is not root."""

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
        """`reclaim` promises its own directory and nothing beside it; `_CARRIED` is the
        control.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            # The member the framework calls in its `finally`, not a command of this suite's.
            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            assert self._as_root(sandbox.container_name, f"ls -A {_WORK}").split() == [_CARRIED]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_reclaim_removes_a_tree_the_two_principals_share(self):
        """The host's files beside the guest's, under one directory, removed in one walk."""
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
        """What the `FILES_DELETE` probes ask for, against a guest that owns none of it."""
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

    def test_a_nonroot_guest_can_rewrite_and_delete_what_the_host_wrote(self):
        """What `write_file` lands on a non-root image is the image user's, so the guest
        program can append to it, write beside it, and take it away — the file plane a
        root guest has always had.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec(_NONROOT_IMAGE))
            shared = f"{_WORK}/inputs/shared.csv"
            await sandbox.write_file(shared, "a,b\n1,2\n", working_directory=_WORK)

            appended = await sandbox.exec(
                ["sh", "-c", f"echo 3,4 >> {shared}"], working_directory="/", timeout=60
            )
            assert appended.exit_code == 0, appended.stderr

            read_back = await sandbox.read_file(shared, working_directory=_WORK, max_bytes=4096)
            assert read_back == b"a,b\n1,2\n3,4\n"

            # The directory `write_file` made is the guest's too, so the program can
            # create beside what the host put there.
            created = await sandbox.exec(
                ["sh", "-c", f"echo mine > {_WORK}/inputs/mine.txt"],
                working_directory="/",
                timeout=60,
            )
            assert created.exit_code == 0, created.stderr
            mine = await sandbox.read_file(
                f"{_WORK}/inputs/mine.txt", working_directory=_WORK, max_bytes=4096
            )
            assert mine == b"mine\n"

            # The delete half, on the file the host wrote: a non-root guest has to be able
            # to remove what the host shared in.
            removed = await sandbox.exec(
                ["sh", "-c", f"rm {shared}"], working_directory="/", timeout=60
            )
            assert removed.exit_code == 0, removed.stderr
            assert await sandbox.stat_file("inputs/shared.csv", working_directory=_WORK) is None

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_nonroot_guest_owns_what_write_file_created(self):
        """The ownership itself, read back from the container: the file and the call
        directory under it carry the image user's uid and its primary gid, so `reclaim` as
        that user can take the tree away.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec(_NONROOT_IMAGE))
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            identity = await sandbox.exec(
                ["sh", "-c", "echo $(id -u):$(id -g)"], working_directory="/", timeout=60
            )
            assert identity.exit_code == 0, identity.stderr
            owner = await sandbox.exec(
                ["sh", "-c", f"stat -c '%u:%g' {call_directory} {call_directory}/note"],
                working_directory="/",
                timeout=60,
            )
            assert owner.exit_code == 0, owner.stderr
            assert owner.stdout.split() == [identity.stdout.strip()] * 2

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _NAMED_USER_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_NAMED_USER_IMAGE naming an image whose USER is a name",
)
class TestAGuestNamedRatherThanNumbered:
    """A named `USER` against a real engine, over a `work_dir` the image already carries.

    A numeric pair is taken from `Config.User` as-is, so only an image naming a *user* reaches
    the account files at all.  This one pairs that with a `work_dir` built into the image,
    which leaves the call directory's parent root's — the half of the pair that decides which
    removals the guest can make.  `TestAnImageThatDoesNotCarryItsWorkDir` is the other half.
    """

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(kind="e2e-named", image=_NAMED_USER_IMAGE, work_dir=_WORK)

    def test_a_named_user_resolves_to_the_uid_and_gid_its_passwd_entry_names(self):
        """`app` is `10001:20001` in the image's `/etc/passwd`, read host-side over the pull
        surface.  The gid is the half that matters: it is not the uid, so a run that reported
        `10001:10001` would be guessing, and one reporting `0:0` would be the pre-fix fallback.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "written by the host\n", working_directory=_WORK
            )
            # Read with the guest's own `stat`, so the assertion is about what landed on the
            # filesystem rather than about what the backend believes it sent.
            owners = await sandbox.exec(
                ["sh", "-c", f"stat -c '%u:%g' {call_directory} {call_directory}/note"],
                working_directory="/",
                timeout=60,
            )
            assert owners.exit_code == 0, owners.stderr
            assert owners.stdout.split() == ["10001:20001", "10001:20001"]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_guest_can_delete_what_the_host_wrote_beside_it(self):
        """The ownership, exercised by the principal it names rather than by `reclaim`.

        `reclaim` would raise authority here — this image leaves `/maf-sandbox` root's, so the
        acquire-time check clears a root removal and the assertion would hold however the
        entries were stamped.  Unlinking the call directory itself needs write on `work_dir`,
        which is the image's build and root's (measured), so what the ownership buys on *this*
        shape is the guest emptying the directory's contents.  That is what is asked of it.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            removed = await sandbox.exec(
                ["sh", "-c", f"rm {call_directory}/note"], working_directory="/", timeout=60
            )
            assert removed.exit_code == 0, removed.stderr

            listed = await sandbox.exec(
                ["sh", "-c", f"ls -A {call_directory}"], working_directory="/", timeout=60
            )
            assert listed.exit_code == 0, listed.stderr
            assert listed.stdout.split() == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _ABSENT_WORK_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_ABSENT_WORK_IMAGE naming an image carrying no work_dir",
)
class TestAnImageThatDoesNotCarryItsWorkDir:
    """`work_dir` absent at build time, against a real engine.

    Docker creates an intermediate the entries do not name as root whatever the file entry
    says, so this is the shape where `work_dir` itself has to travel as an explicit entry.
    Every other live image builds `work_dir` in, so the branch runs only here.
    """

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(kind="e2e-absent", image=_ABSENT_WORK_IMAGE, work_dir=_WORK)

    def test_the_work_dir_itself_arrives_owned_by_the_image_user(self):
        """`/maf-sandbox` exists and is root's; `work_dir` does not exist at all. It has to
        arrive as the guest's, or the call directory under it is root's and cannot be emptied.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "written by the host\n", working_directory=_WORK
            )
            owners = await sandbox.exec(
                ["sh", "-c", f"stat -c '%u:%g' {_WORK} {call_directory}"],
                working_directory="/",
                timeout=60,
            )
            assert owners.exit_code == 0, owners.stderr
            assert owners.stdout.split() == ["10001:20001", "10001:20001"]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_guest_removes_the_call_directory_the_write_created(self):
        """The consequence of the entry above, and the removal `reclaim` would be asked for —
        run as the image's user rather than through `reclaim`, which would raise authority here
        and pass whatever the entries said.

        Unlinking the call directory needs write on `work_dir`, and on this shape `work_dir` is
        not the image's build but the write's own explicit entry, so it is the guest's.  That
        is the difference from the image that carries `work_dir` already.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            removed = await sandbox.exec(
                ["sh", "-c", f"rm -rf {call_directory}"], working_directory="/", timeout=60
            )
            assert removed.exit_code == 0, removed.stderr

            listed = await sandbox.exec(
                ["sh", "-c", f"ls -A {_WORK}"], working_directory="/", timeout=60
            )
            assert listed.exit_code == 0, listed.stderr
            assert listed.stdout.split() == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _GUEST_OWNED_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_GUEST_OWNED_IMAGE naming a non-root image owning work_dir",
)
class TestAWorkDirTheImageGaveItsOwnUser:
    """``work_dir`` owned by the image's own user: the retry's case, and the walk's."""

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
        """`work_dir` is the guest's, so the reach rule keeps the removal there — and root is
        never asked, so no fallback is involved.
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

    def test_remove_of_a_host_written_subdirectory_runs_at_the_guest_and_succeeds(self):
        """The walk finds a component of the path the guest could have swapped, so the
        removal stays at the guest's authority — and it succeeds, because the parent it
        empties is one `write_file` made guest-owned.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            await sandbox.write_file(f"{_WORK}/sub/doomed.txt", "x", working_directory=_WORK)

            await sandbox.remove("sub/doomed.txt", working_directory=_WORK)

            assert await sandbox.stat_file("sub/doomed.txt", working_directory=_WORK) is None

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
    """An image that lets the guest swap `work_dir` itself, which is what the acquire-time
    check is for.
    """

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(kind="e2e-loose", image=_LOOSE_PARENT_IMAGE, work_dir=_WORK)

    def test_reclaim_drops_to_the_guest_authority_and_succeeds(self):
        """The chain above `work_dir` is the guest's, so root is never asked over this path
        — that is the boundary the acquire-time check exists to hold (pinned by the swap
        test below).  The guest's `rm` still empties the call directory, because
        `write_file` made it and its parents guest-owned.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

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

    def test_a_swapped_work_dir_takes_the_removal_nowhere_it_could_not_reach(self):
        """The attack the check exists for, run for real: the guest swaps `work_dir` for a link
        to a directory it does not own, and the redirected `rm` reaches nothing new.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            plant_the_target = (
                "mkdir -p /victim/abc123def456"
                " && echo treasure > /victim/abc123def456/t"
                " && chmod 755 /victim"
            )
            planted = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "0",
                    sandbox.container_name,
                    "sh",
                    "-c",
                    plant_the_target,
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
            assert purged.disposed >= 1
            assert purged.undisposed is None
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestTheGuestFamilyAgainstARealDaemon:
    """What `create` reads off the daemon this suite is pointed at, and the workload it admits.

    The mapping is asserted rather than the environment: the daemon is read again here, with
    `docker` rather than through the backend, so the test says the same thing on a Linux runner
    and on a Windows-container daemon.
    """

    @staticmethod
    def _daemon_os() -> str:
        return (
            subprocess.run(
                ["docker", "version", "--format", "{{.Server.Os}}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            .stdout.strip()
            .lower()
        )

    def test_the_declaration_is_what_the_daemon_reports(self):
        backend = asyncio.run(DockerSandboxBackend.create(DockerSandboxConfig()))
        posix = frozenset({OsFamily.POSIX})
        assert backend.declarations.os_families == (
            posix if self._daemon_os() == "linux" else frozenset()
        )

    def test_the_plain_constructor_declares_nothing_against_the_same_daemon(self):
        assert DockerSandboxBackend(DockerSandboxConfig()).declarations.os_families == frozenset()

    def test_a_workload_requiring_the_declared_family_is_served_and_runs(self):
        """The declaration backed by a container rather than matched on paper: the router
        admits the spec, and the guest the backend hands back takes POSIX argv."""
        if self._daemon_os() != "linux":
            pytest.skip("this daemon does not run the family this suite's image is built for")
        scope = f"e2e-{uuid.uuid4()}"
        backend = asyncio.run(DockerSandboxBackend.create(DockerSandboxConfig()))
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
        spec = _spec(requires_os_family=OsFamily.POSIX)

        async def scenario() -> None:
            router.ensure_can_serve(spec)
            sandbox = await router.acquire(_key(scope), spec)
            # At `/` rather than `work_dir`: what this asserts is the guest's grammar and
            # argv, which every Linux image answers for, not a directory some of them carry.
            ran = await sandbox.exec(
                ["sh", "-c", "printf posix"], working_directory="/", timeout=60
            )
            assert ran.exit_code == 0, ran.stderr
            assert ran.stdout == "posix"

        try:
            asyncio.run(scenario())
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

    def test_absence_is_the_engine_naming_the_path_and_a_gone_container_is_not(self):
        """Which failures a real daemon answers with, and which of them mean "not there".

        The offline suite pins the classification against messages this repository wrote
        down; what only a live engine can say is that they are still the messages. A
        container removed from under the sandbox is the failure the phrase gets borrowed by:
        `docker cp` answers about the container, naming no path, and reading that as absence
        would end the filesystem path check on every ancestor at once.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK)

            assert await sandbox.stat_file("gone.txt", working_directory=_WORK) is None
            with pytest.raises(FileNotFoundError):
                await sandbox.read_file("gone.txt", working_directory=_WORK, max_bytes=1024)

            subprocess.run(
                ["docker", "rm", "-f", sandbox.container_name],
                capture_output=True,
                timeout=60,
                check=True,
            )
            with pytest.raises(RuntimeError, match="could not stat") as raised:
                await sandbox.stat_file("gone.txt", working_directory=_WORK)
            assert sandbox.container_name in str(raised.value)

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
                    capabilities=backend.declarations.capabilities,
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
                    capabilities=backend.declarations.capabilities,
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
                    capabilities=backend.declarations.capabilities,
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
                    capabilities=backend.declarations.capabilities,
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
                    capabilities=backend.declarations.capabilities,
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

    def test_a_calls_outputs_land_in_a_folder_the_model_reads_back(self):
        """The read-back composition against a real engine: a container writes a declared
        output, `make_file_store_sink` lands it under the host-minted call id, and the two
        read-back tools are what open it — the path a model takes, over a shipped store.

        The last assertion is the one the tools' own descriptions rest on: a listing names
        children, so the folder has to be joined back on before the bytes come out.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

        from agent_framework import InMemoryAgentFileStore
        from maf_sandbox import DeclaredOutput
        from maf_sandbox.maf import make_file_store_sink, sandbox_outputs_read_tools

        store = InMemoryAgentFileStore()
        listing, read = sandbox_outputs_read_tools(store)

        def body(tool: Any) -> Any:
            return getattr(tool, "func", tool)

        spec = _spec(
            requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            declared_outputs=(DeclaredOutput(path="result.txt", media_type="text/plain"),),
            files_out=TransferLimits(
                max_bytes_per_file=1 << 20, max_total_bytes=1 << 20, max_files=4
            ),
        )

        async def scenario() -> None:
            sandbox = await router.acquire(_key(scope), spec)
            await sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK)
            await sandbox.exec(
                ["sh", "-c", "echo rendered > result.txt"], working_directory=_WORK, timeout=60
            )
            landed = await collect_outputs(
                sandbox, spec, sink=make_file_store_sink(store), call_id="c0ffee"
            )

            assert [item.name for item in landed] == ["result.txt"]
            assert await body(listing)("") == [{"name": "c0ffee", "type": "directory"}]
            assert await body(listing)("c0ffee") == [{"name": "result.txt", "type": "file"}]
            assert await body(read)("c0ffee/result.txt") == "rendered\n"
            assert (await body(read)("result.txt")).startswith("Error: there is no file at")

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


def _inspected(kind: str, name: str, template: str) -> str:
    return subprocess.run(
        ["docker", *([] if kind == "container" else [kind]), "inspect", "-f", template, name],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()


class _OkHandler(http.server.BaseHTTPRequestHandler):
    """Answers any GET with 200, so a probe that reaches it is unambiguous."""

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's dispatch name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"reached\n")

    def log_message(self, *args: object) -> None:
        """Silence: this server logs a line per probe to stderr otherwise."""


@contextlib.contextmanager
def _a_listener_on_every_host_address():
    """An HTTP server on ``0.0.0.0``, yielding its port — a host service a guest must not reach.

    Bound to every address on purpose: that is the shape Docker documents as reachable from a
    container through an addressed bridge, and the one an operator is least likely to think of
    as exposed to a sandbox.
    """
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _OkHandler)  # noqa: S104
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


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
                capabilities=backend.declarations.capabilities,
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
            purged = asyncio.run(backend.dispose_scope(scope, "thread-1")).disposed
        assert purged == 1
        assert _names_on_the_machine(sandbox.container_name) == []
        assert not _network_present(net)

    def test_the_bridge_holds_no_host_address_so_neither_direction_crosses(self):
        """The allowlist is only the workload's whole egress if the bridge has no host address.

        With one, Docker routes both ways around the proxy: the guest reaches host services
        bound to a wildcard address, and the host reaches any port inside the container.

        **Both probes need the test process in the daemon's own network namespace**, which is
        the ``docker-e2e`` runner and any rootful Linux engine. Against a Docker Desktop VM,
        or a rootless daemon whose bridge lives inside RootlessKit's own network namespace,
        the test passes without proving anything, because the process is outside the namespace
        the bridge is in — the mechanism assertions at the end are what still bite there.

        The guest-to-host probe carries a positive control, since a listener that never came up
        would answer ``000`` for a reason that has nothing to do with the bridge.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(self._config())
        spec = _spec(egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",))

        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        net = sandbox.container_name + "-net"
        asyncio.run(sandbox.write_file("/maf-sandbox/work/.keep", "", working_directory=_WORK))
        try:
            # Behaviour first, mechanism after: what this holds the backend to is that neither
            # direction crosses, and the option that currently achieves it is the explanation.
            # As JSON rather than a `range` template: a dual-stack network has one entry per
            # family and a template concatenates them into a value that parses as neither.
            # The IPv4 entry is chosen by family, not position, since IPv6 comes first there.
            ipam = json.loads(_inspected("network", net, "{{json .IPAM.Config}}"))
            v4 = next(e for e in ipam if ipaddress.ip_network(e["Subnet"]).version == 4)
            # Where the bridge would hold an address if it held one, so the probe targets the
            # same place before and after the option rather than a mechanism-shaped absence.
            bridge = str(ipaddress.ip_network(v4["Subnet"]).network_address + 1)

            with _a_listener_on_every_host_address() as port:
                with socket.create_connection(("127.0.0.1", port), timeout=10):
                    pass  # the control: the server is up and serving on this host
                _, status = self._curl_status(sandbox, f"http://{bridge}:{port}/")
                assert status == "000", f"the guest reached a host service at {bridge}:{port}"

            guest_ip = _inspected(
                "container",
                sandbox.container_name,
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            )
            assert guest_ip, "the workload has no address on its own network"
            # No listener is planted, and none is needed: a refusal is the finding. The guest's
            # stack answers a port nothing holds with a reset, so ECONNREFUSED means the packet
            # arrived and the host routes to the container. An unroutable address cannot
            # produce it — measured as a timeout instead — so the errno is the whole assertion.
            with pytest.raises(OSError) as refused:
                with socket.create_connection((guest_ip, 8080), timeout=5):
                    pass
            assert refused.value.errno != errno.ECONNREFUSED, (
                f"the host routed to the guest at {guest_ip}"
            )

            # The mechanism behind both, and read as the effect rather than as the request:
            # `.Options` echoes back what the network was created with whether or not the
            # daemon acted on it, so IPAM is the only place that says where the bridge ended
            # up. An addressed network carries a `Gateway` in every entry, so asking for its
            # absence holds on a dual-stack network as well as a single-stack one.
            assert all(not entry.get("Gateway") for entry in ipam), ipam
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


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
