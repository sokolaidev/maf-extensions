"""Offline tests for the docker backend.

No engine and no container: the one seam every ``docker`` invocation goes through is replaced
by a fake that records argv and replays canned results, so what these tests pin is the command
line this backend actually builds.  Some tests reach the real seam anyway — with
``sys.executable`` standing in for the ``docker`` client — because the subprocess handling
itself (bytes decoding, exit codes, killing a real child on timeout and on cancellation) is the
one part a fake cannot prove, and one reads a captured payload from a real ``docker``, because a
listing this file invented agrees with the code that reads it by construction.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import tarfile
from collections.abc import Sequence

import pytest
from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    Isolation,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
)

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import (
    _container_name,
    _DockerResult,
    _network_name,
    _proxy_name,
)

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY, _SPEC.kind)
_WORK = "/work"


def _tar_bytes(path: str, data: bytes) -> bytes:
    """A one-entry tar as ``docker cp <name>:<path> -`` would stream it out."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.size = len(data)
        entry.mode = 0o644
        archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def _symlink_tar(path: str, target: str) -> bytes:
    """A tar carrying a symlink *entry* — the shape ``docker cp`` without ``-L`` produces."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.type = tarfile.SYMTYPE
        entry.linkname = target
        archive.addfile(entry)
    return buffer.getvalue()


class _Recorded:
    def __init__(
        self,
        args: tuple[str, ...],
        stdin: bytes | None,
        timeout: float | None,
        read_limit: int | None,
    ) -> None:
        self.args = args
        self.stdin = stdin
        self.timeout = timeout
        self.read_limit = read_limit


class _FakeDocker:
    """Stands in for `DockerSandboxBackend._docker`.

    Honours ``read_limit`` by slicing the responder's stdout to it, the way the real bounded
    read stops after that many bytes — so a test asserting the read path never buffers a whole
    oversized output sees the same truncated stdout the real seam would hand back.
    """

    def __init__(self, responder=None) -> None:
        self.calls: list[_Recorded] = []
        self._responder = responder or (lambda args: _DockerResult(0, b"", ""))

    async def __call__(
        self, *args: str, stdin=None, timeout=None, read_limit=None
    ) -> _DockerResult:
        self.calls.append(_Recorded(args, stdin, timeout, read_limit))
        result = self._responder(args)
        if read_limit is not None and len(result.stdout) > read_limit:
            result = _DockerResult(result.returncode, result.stdout[:read_limit], result.stderr)
        return result

    def matching(self, *prefix: str) -> list[_Recorded]:
        return [c for c in self.calls if c.args[: len(prefix)] == prefix]

    def only(self, *prefix: str) -> _Recorded:
        found = self.matching(*prefix)
        assert len(found) == 1, [c.args for c in self.calls]
        return found[0]


def _machine(
    running: Sequence[str] = (),
    stopped: Sequence[str] = (),
    images: Sequence[str] = ("bicep-sandbox:local",),
    overrides: dict[tuple[str, ...], _DockerResult] | None = None,
):
    """A responder describing which containers and images exist, and how a command answers.

    ``docker inspect -f {{.State.Running}}`` decides existence and running state — a name in
    ``running`` prints ``true``, one only in ``stopped`` prints ``false``, one in neither errors
    like a missing container. ``image inspect`` succeeds for a known image and errors otherwise.
    """
    present = set(running) | set(stopped)

    def respond(args: tuple[str, ...]) -> _DockerResult:
        for prefix, result in (overrides or {}).items():
            if args[: len(prefix)] == prefix:
                return result
        if args[:2] == ("image", "inspect"):
            image = args[2]
            return (
                _DockerResult(0, b"", "")
                if image in images
                else _DockerResult(1, b"", "No such image")
            )
        if args[0] == "inspect":
            name = args[-1]
            if name not in present:
                return _DockerResult(1, b"", f"Error: No such object: {name}")
            state = "true" if name in running else "false"
            return _DockerResult(0, state.encode() + b"\n", "")
        if args[:2] == ("ps", "-a") or args[0] == "ps":
            names = [*running, *stopped] if "-a" in args else list(running)
            return _DockerResult(0, "".join(f"{n}\n" for n in names).encode(), "")
        if args[0] == "logs":
            return _DockerResult(0, b"listening on 3128\n", "")
        return _DockerResult(0, b"", "")

    return respond


def _explodes(args: tuple[str, ...]) -> _DockerResult:
    raise RuntimeError("docker is not installed")


def _backend_with(responder=None, config=None) -> tuple[DockerSandboxBackend, _FakeDocker]:
    """A backend whose every docker invocation goes to the fake, via the one protected seam."""
    backend = DockerSandboxBackend(config or DockerSandboxConfig())
    fake = _FakeDocker(responder)
    backend._docker = fake  # type: ignore[method-assign]
    return backend, fake


# ---------------------------------------------------------------------------
# Backend identity — read by the router's isolation floor and capability match
# ---------------------------------------------------------------------------


class TestBackendIdentity:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(DockerSandboxBackend(DockerSandboxConfig()), SandboxBackend)

    def test_declares_container_isolation(self):
        assert DockerSandboxBackend(DockerSandboxConfig()).isolation == Isolation.CONTAINER

    def test_isolation_is_a_constant_no_config_raises(self):
        """The rung is a constant: no field on the config lifts it off `container`."""
        hardened = DockerSandboxConfig(cap_drop_all=True, memory="512m", cpus=2.0)
        assert DockerSandboxBackend(hardened).isolation == Isolation.CONTAINER

    def test_declares_closed_egress_without_a_proxy(self):
        assert DockerSandboxBackend(DockerSandboxConfig()).egress == Egress.CLOSED

    def test_declares_allowlist_egress_with_a_proxy(self):
        config = DockerSandboxConfig(egress_proxy_image="proxy:local")
        assert DockerSandboxBackend(config).egress == Egress.ALLOWLIST

    def test_declares_exec_files_in_and_files_out(self):
        caps = DockerSandboxBackend(DockerSandboxConfig()).capabilities
        assert caps == frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT})

    def test_does_not_declare_files_list(self):
        assert Capability.FILES_LIST not in DockerSandboxBackend(DockerSandboxConfig()).capabilities

    def test_is_named_docker(self):
        assert DockerSandboxBackend(DockerSandboxConfig()).name == "docker"

    def test_declares_transfer_limits(self):
        limits = DockerSandboxBackend(DockerSandboxConfig()).limits
        assert limits.files_out.max_files >= 1
        assert limits.files_in.max_bytes_per_file >= 1


class TestRouterFloor:
    """The default `microvm` floor refuses this backend; opting down to `container` admits it."""

    def test_the_default_floor_refuses_this_backend(self):
        with pytest.raises(SandboxBackendNotPermitted):
            SandboxRouter([DockerSandboxBackend(DockerSandboxConfig())])

    def test_opting_the_floor_down_to_container_admits_it(self):
        router = SandboxRouter(
            [DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER
        )
        assert router.enabled

    def test_a_spec_requiring_files_list_is_refused(self):
        """The capability match refuses a spec asking for what this backend never declares."""
        from maf_sandbox import SandboxCapabilityNotSupported

        router = SandboxRouter(
            [DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER
        )
        spec = SandboxSpec(kind="k", requires=frozenset({Capability.EXEC, Capability.FILES_LIST}))
        with pytest.raises(SandboxCapabilityNotSupported):
            router.ensure_can_serve(spec)

    def test_a_spec_requiring_files_out_is_admitted(self):
        router = SandboxRouter(
            [DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER
        )
        spec = SandboxSpec(kind="k", requires=frozenset({Capability.EXEC, Capability.FILES_OUT}))
        router.ensure_can_serve(spec)  # does not raise

    def test_a_spec_asking_above_the_transfer_ceiling_is_refused(self):
        from maf_sandbox import SandboxTransferLimitsNotPermitted, TransferLimits

        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
        huge = TransferLimits(
            max_bytes_per_file=backend.limits.files_out.max_bytes_per_file + 1,
            max_total_bytes=backend.limits.files_out.max_total_bytes,
            max_files=backend.limits.files_out.max_files,
        )
        spec = SandboxSpec(
            kind="k", requires=frozenset({Capability.EXEC, Capability.FILES_OUT}), files_out=huge
        )
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            router.ensure_can_serve(spec)


# ---------------------------------------------------------------------------
# Acquire — create, reuse, recover
# ---------------------------------------------------------------------------


class TestAcquireCreatesClosed:
    def test_the_container_gets_no_network(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        run = fake.only("run")
        assert run.args[run.args.index("--network") + 1] == "none"

    def test_the_container_is_detached_and_named(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        run = fake.only("run")
        assert run.args[:2] == ("run", "-d")
        assert run.args[run.args.index("--name") + 1] == _NAME

    def test_the_name_is_derived_from_the_key_and_the_kind(self):
        assert _NAME.startswith("maf-sandbox-docker-")
        assert _container_name(_KEY, "other") != _NAME

    def test_two_kinds_on_one_key_get_two_containers(self):
        assert _container_name(_KEY, "a") != _container_name(_KEY, "b")

    def test_a_delimiter_in_a_field_does_not_collide_with_a_shifted_split(self):
        """Length-prefixed hashing: `(scope='a|b', thread='c')` and `(scope='a', thread='b|c')`
        must not resolve to one container even though a `|`-join would make them identical."""
        left = _container_name(SandboxKey(scope="a|b", thread_id="c", agent_dir="d"), "k")
        right = _container_name(SandboxKey(scope="a", thread_id="b|c", agent_dir="d"), "k")
        assert left != right

    def test_the_keepalive_command_is_the_image_then_sleep_infinity(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        run = fake.only("run")
        assert run.args[-3:] == ("bicep-sandbox:local", "sleep", "infinity")

    def test_a_pinned_image_id_wins_over_the_reference(self):
        backend, fake = _backend_with(_machine(images=("sha256:abc",)))
        asyncio.run(
            backend.acquire(_KEY, SandboxSpec(kind="bicep", image="ignored", image_id="sha256:abc"))
        )
        run = fake.only("run")
        assert "sha256:abc" in run.args

    def test_no_image_at_all_is_refused(self):
        backend, _ = _backend_with(_machine())
        with pytest.raises(ValueError, match="neither image nor image_id"):
            asyncio.run(backend.acquire(_KEY, SandboxSpec(kind="bicep")))

    def test_hardening_flags_are_on_by_default(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        args = fake.only("run").args
        assert args[args.index("--security-opt") + 1] == "no-new-privileges"
        assert args[args.index("--pids-limit") + 1] == "512"

    def test_cap_drop_is_off_by_default(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert "--cap-drop" not in fake.only("run").args

    def test_cap_drop_and_resource_limits_are_opt_in(self):
        config = DockerSandboxConfig(cap_drop_all=True, memory="512m", cpus=1.5)
        backend, fake = _backend_with(_machine(), config=config)
        asyncio.run(backend.acquire(_KEY, _SPEC))
        args = fake.only("run").args
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--memory") + 1] == "512m"
        assert args[args.index("--cpus") + 1] == "1.5"

    def test_no_bind_mount_or_socket_ever_crosses(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        args = fake.only("run").args
        assert "-v" not in args and "--volume" not in args
        assert not any("docker.sock" in a for a in args)

    def test_labels_carry_the_key_and_the_specs_own_labels(self):
        spec = SandboxSpec(kind="bicep", image="bicep-sandbox:local", labels={"team": "infra"})
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, spec))
        args = fake.only("run").args
        labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        assert "maf-sandbox.scope=scope-a" in labels
        assert "maf-sandbox.kind=bicep" in labels
        assert "maf-sandbox.label.team=infra" in labels

    def test_creation_is_logged(self, caplog):
        backend, _ = _backend_with(_machine())
        with caplog.at_level(logging.INFO):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert any("sandbox created" in r.message for r in caplog.records)


class TestAcquirePullsAbsentImages:
    def test_a_present_image_is_not_pulled(self):
        backend, fake = _backend_with(_machine(images=("bicep-sandbox:local",)))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("image", "pull") == []
        assert fake.matching("image", "inspect") != []

    def test_an_absent_image_is_pulled_under_the_pull_timeout(self):
        config = DockerSandboxConfig(image_pull_timeout_seconds=123.0)
        backend, fake = _backend_with(_machine(images=()), config=config)
        asyncio.run(backend.acquire(_KEY, _SPEC))
        pull = fake.only("image", "pull")
        assert pull.args == ("image", "pull", "bicep-sandbox:local")
        assert pull.timeout == 123.0

    def test_a_pull_failure_is_reported(self):
        overrides = {("image", "pull"): _DockerResult(1, b"", "manifest unknown")}
        backend, _ = _backend_with(_machine(images=(), overrides=overrides))
        with pytest.raises(RuntimeError, match="could not pull image"):
            asyncio.run(backend.acquire(_KEY, _SPEC))


class TestAcquireReuses:
    def test_a_running_container_is_neither_created_nor_started(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") == []
        assert fake.matching("start") == []

    def test_reuse_is_logged(self, caplog):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        with caplog.at_level(logging.INFO):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert any("sandbox reused" in r.message for r in caplog.records)

    def test_a_stopped_container_is_started_rather_than_replaced(self):
        backend, fake = _backend_with(_machine(stopped=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.only("start").args == ("start", _NAME)
        assert fake.matching("run") == []

    def test_a_missing_container_is_created(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") != []

    def test_a_container_that_will_not_start_is_replaced(self):
        overrides = {("start",): _DockerResult(1, b"", "start failed")}
        backend, fake = _backend_with(_machine(stopped=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("rm", "-f", _NAME) != []
        assert fake.matching("run") != []


class TestAcquireRecoversFromANameConflict:
    def test_the_existing_container_is_used_instead_of_failing(self):
        # Create says the name is taken; the follow-up inspect finds it running.
        state = {"created": False}

        def responder(args):
            if args[0] == "run":
                state["created"] = True
                return _DockerResult(
                    125, b"", 'Conflict. The container name "/x" is already in use'
                )
            if args[0] == "inspect" and state["created"]:
                return _DockerResult(0, b"true\n", "")
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            return _DockerResult(0, b"", "")

        backend, _ = _backend_with(responder)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        assert sandbox.container_name == _NAME

    def test_any_other_create_failure_still_raises(self):
        overrides = {("run",): _DockerResult(1, b"", "disk full")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        with pytest.raises(RuntimeError, match="could not create container"):
            asyncio.run(backend.acquire(_KEY, _SPEC))


# ---------------------------------------------------------------------------
# Exec
# ---------------------------------------------------------------------------


class TestExecArgv:
    def test_a_sequence_reaches_the_container_verbatim_with_no_shell(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(
            sandbox.exec(["bicep", "build", "main.bicep"], working_directory=_WORK, timeout=5)
        )
        args = fake.only("exec").args
        assert args == ("exec", "-w", _WORK, _NAME, "bicep", "build", "main.bicep")

    def test_a_string_is_run_by_a_shell(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.exec("echo hi || true", working_directory=_WORK, timeout=5))
        args = fake.only("exec").args
        assert args[-3:] == ("sh", "-c", "echo hi || true")

    def test_the_per_call_timeout_reaches_the_seam(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.exec(["true"], working_directory=_WORK, timeout=42))
        assert fake.only("exec").timeout == 42

    def test_stdout_stderr_and_exit_code_are_mapped(self):
        overrides = {("exec",): _DockerResult(7, b"out\n", "err\n")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        result = asyncio.run(sandbox.exec(["x"], working_directory=_WORK, timeout=5))
        assert (result.stdout, result.stderr, result.exit_code) == ("out\n", "err\n", 7)


class TestExecDiscardsATimedOutSandbox:
    def test_a_timed_out_exec_removes_the_container(self):
        def responder(args):
            if args[0] == "exec":
                raise TimeoutError
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect":
                return _DockerResult(0, b"true\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["hang"], working_directory=_WORK, timeout=1))
        assert fake.matching("rm", "-f", _NAME) != []


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def _sandbox(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_the_copy_targets_the_container_root(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.write_file("/work/main.bicep", "x"))
        assert fake.only("cp", "-").args == ("cp", "-", f"{_NAME}:/")

    def test_the_entry_is_the_path_without_its_leading_slash(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.write_file("/work/main.bicep", "content"))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == ["work/main.bicep"]

    def test_str_content_round_trips_as_utf8(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.write_file("/work/f", "héllo"))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            member = archive.extractfile("work/f")
            assert member is not None
            assert member.read().decode("utf-8") == "héllo"

    def test_bytes_content_is_written_as_given(self):
        sandbox, fake = self._sandbox()
        payload = b"\x89PNG\r\n\x1a\n"
        asyncio.run(sandbox.write_file("/work/img.png", payload))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            member = archive.extractfile("work/img.png")
            assert member is not None
            assert member.read() == payload

    def test_a_failed_copy_raises(self):
        overrides = {("cp", "-"): _DockerResult(1, b"", "no space")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(RuntimeError, match="could not write"):
            asyncio.run(sandbox.write_file("/work/f", "x"))


# ---------------------------------------------------------------------------
# FILES_OUT — stat and read from the docker cp tar stream
# ---------------------------------------------------------------------------


class TestStatFile:
    def _sandbox_streaming(self, stream: bytes, rc: int = 0, stderr: str = ""):
        overrides = {("cp",): _DockerResult(rc, stream, stderr)}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_regular_file_is_statted_from_the_first_tar_header(self):
        sandbox, _ = self._sandbox_streaming(_tar_bytes("out.png", b"x" * 40))
        entry = asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))
        assert entry is not None
        assert entry.kind is EntryKind.FILE
        assert entry.size_bytes == 40

    def test_a_symlink_is_reported_as_other_with_no_size(self):
        sandbox, _ = self._sandbox_streaming(_symlink_tar("link", "/etc/passwd"))
        entry = asyncio.run(sandbox.stat_file("link", working_directory=_WORK))
        assert entry is not None
        assert entry.kind is EntryKind.OTHER
        assert entry.size_bytes is None

    def test_a_missing_path_is_none(self):
        sandbox, _ = self._sandbox_streaming(b"", rc=1, stderr="Could not find the file /work/x")
        assert asyncio.run(sandbox.stat_file("x", working_directory=_WORK)) is None

    def test_the_stat_bounds_the_transfer_to_one_tar_block(self):
        """A stat must not buffer a whole untrusted file: it bounds the cp read to 512 bytes."""
        from maf_sandbox_docker._backend import _TAR_BLOCK

        sandbox, fake = self._sandbox_streaming(_tar_bytes("out.png", b"x" * 100000))
        asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))
        cp = fake.only("cp")
        assert cp.read_limit == _TAR_BLOCK

    def test_the_rel_path_is_correct_even_for_a_non_normalized_working_directory(self):
        """A base like `/work/.` must not shift the reported path — normalize before slicing."""
        sandbox, _ = self._sandbox_streaming(_tar_bytes("out.txt", b"x" * 5))
        entry = asyncio.run(sandbox.stat_file("out.txt", working_directory="/work/."))
        assert entry is not None
        assert entry.path == "out.txt"

    def test_a_backslash_path_is_refused_before_any_subprocess(self):
        sandbox, fake = self._sandbox_streaming(b"")
        before = len(fake.calls)
        with pytest.raises(ValueError, match="backslash"):
            asyncio.run(sandbox.stat_file("a\\b", working_directory=_WORK))
        assert len(fake.calls) == before

    def test_a_traversal_path_is_refused_before_any_subprocess(self):
        sandbox, fake = self._sandbox_streaming(b"")
        before = len(fake.calls)
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(sandbox.stat_file("../escape", working_directory=_WORK))
        assert len(fake.calls) == before


class TestReadFile:
    def _sandbox_streaming(self, stream: bytes):
        overrides = {("cp",): _DockerResult(0, stream, "")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC))

    def test_a_regular_file_body_comes_back_byte_identical(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"pixels"
        sandbox = self._sandbox_streaming(_tar_bytes("out.png", payload))
        got = asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=1000))
        assert got == payload

    def test_a_body_over_the_cap_is_refused_not_truncated(self):
        sandbox = self._sandbox_streaming(_tar_bytes("out.png", b"x" * 100))
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=10))

    def test_the_read_bounds_the_transfer_to_header_plus_the_cap(self):
        """An oversized output is refused from its header without its body being buffered."""
        from maf_sandbox_docker._backend import _TAR_BLOCK

        overrides = {("cp",): _DockerResult(0, _tar_bytes("big.bin", b"x" * 100000), "")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(sandbox.read_file("big.bin", working_directory=_WORK, max_bytes=64))
        assert fake.only("cp").read_limit == _TAR_BLOCK + 64

    def test_a_symlink_is_refused_on_the_header_type(self):
        sandbox = self._sandbox_streaming(_symlink_tar("link", "/etc/passwd"))
        with pytest.raises(OSError, match="not a regular file"):
            asyncio.run(sandbox.read_file("link", working_directory=_WORK, max_bytes=1000))

    def test_a_missing_file_raises_file_not_found(self):
        overrides = {("cp",): _DockerResult(1, b"", "No such file or directory")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(FileNotFoundError):
            asyncio.run(sandbox.read_file("gone", working_directory=_WORK, max_bytes=10))


class TestListDirIsRefused:
    def test_list_dir_raises_not_implemented(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(NotImplementedError, match="FILES_LIST"):
            asyncio.run(sandbox.list_dir(".", working_directory=_WORK))


# ---------------------------------------------------------------------------
# Dispose and purge
# ---------------------------------------------------------------------------


class TestDispose:
    def test_removes_the_container_by_name(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(backend.dispose(_KEY))
        assert fake.matching("rm", "-f") != []

    def test_never_raises_when_removal_fails(self):
        overrides = {("rm",): _DockerResult(1, b"", "daemon error")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        asyncio.run(backend.dispose(_KEY))  # does not raise


class TestDisposeScope:
    def test_selects_on_labels_and_returns_the_count(self):
        listed = [_NAME]
        overrides = {("ps",): _DockerResult(0, "".join(f"{n}\n" for n in listed).encode(), "")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        count = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert count == 1
        ps = fake.matching("ps", "-a")[0]
        assert any("label=maf-sandbox.scope=scope-a" in a for a in ps.args)
        assert any("label=maf-sandbox.thread=thread-1" in a for a in ps.args)

    def test_nothing_to_purge_is_zero_not_an_error(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("s", "t")) == 0

    def test_a_failing_listing_degrades_to_zero(self):
        overrides = {("ps",): _DockerResult(1, b"", "daemon down")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        assert asyncio.run(backend.dispose_scope("s", "t")) == 0


# ---------------------------------------------------------------------------
# Label values — the mapping that must agree on create and purge
# ---------------------------------------------------------------------------


class TestLabelValues:
    def test_short_safe_values_are_left_readable(self):
        from maf_sandbox_docker._backend import _label_value

        assert _label_value("scope-a") == "scope-a"

    def test_long_values_are_digested_within_the_limit(self):
        from maf_sandbox_docker._backend import _label_value

        out = _label_value("x" * 200)
        assert out.startswith("sha256-") and len(out) == len("sha256-") + 48

    def test_values_carrying_a_separator_are_digested(self):
        from maf_sandbox_docker._backend import _label_value

        assert _label_value("a=b").startswith("sha256-")

    def test_values_sharing_a_long_prefix_do_not_collide(self):
        from maf_sandbox_docker._backend import _label_value

        assert _label_value("z" * 100 + "a") != _label_value("z" * 100 + "b")

    def test_create_and_purge_agree_on_the_label(self):
        """The value a create writes is the value a purge filters on — same function, both sides."""
        from maf_sandbox_docker._backend import _label_value, _sandbox_labels

        key = SandboxKey(scope="s" * 100, thread_id="t", agent_dir="a")
        labels = _sandbox_labels(key, SandboxSpec(kind="bicep"))
        assert labels["maf-sandbox.scope"] == _label_value("s" * 100)


# ---------------------------------------------------------------------------
# The seam — the one part a fake cannot prove: a real subprocess
# ---------------------------------------------------------------------------


class TestTheSeam:
    """`sys.executable` stands in for the `docker` client to exercise the real subprocess path."""

    def _backend(self):
        return DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))

    def test_stdout_stderr_and_exit_code_come_back_with_bytes_stdout(self):
        backend = self._backend()
        result = asyncio.run(
            backend._docker(
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\x89P'); sys.stderr.write('e'); sys.exit(3)",
            )
        )
        assert result.returncode == 3
        assert result.stdout == b"\x89P"
        assert result.stderr == "e"

    def test_stdin_reaches_the_process(self):
        backend = self._backend()
        result = asyncio.run(
            backend._docker(
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                stdin=b"\x00\x01\x02",
            )
        )
        assert result.stdout == b"\x00\x01\x02"


class TestTheSeamReapsARealChild:
    """A timed-out or cancelled call must kill and reap the real child before propagating."""

    def test_a_timeout_kills_the_child(self):
        backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
        with pytest.raises(TimeoutError):
            asyncio.run(backend._docker("-c", "import time; time.sleep(30)", timeout=0.5))

    def test_a_missing_client_binary_is_named(self):
        backend = DockerSandboxBackend(
            DockerSandboxConfig(docker_path="definitely-not-a-binary-xyz")
        )
        with pytest.raises(RuntimeError, match="was not found on PATH"):
            asyncio.run(backend._docker("version"))


# ---------------------------------------------------------------------------
# Real captured output — a listing this file did not invent
# ---------------------------------------------------------------------------


class TestAgainstRealDockerOutput:
    """A verbatim `docker ps --format '{{.Names}}'`-adjacent payload from docker 29.5.3.

    Every other listing in this file is invented. This fixture is a real `docker ps --format
    '{{json .}}'` row captured from a live engine, proving the label the create writes is the
    label a real engine reports back, and that `--filter label=` selects on it. Regenerate with a
    throwaway container:

        docker run -d --name maf-sandbox-docker-<12 hex> --network none \\
          -l maf-sandbox.scope=probe-scope mcr.microsoft.com/azurelinux/base/core:3.0 sleep infinity
        docker ps --filter label=maf-sandbox.scope=probe-scope --format '{{json .}}'
        docker rm -f <that name>
    """

    def _row(self):
        import json
        import pathlib

        fixture = pathlib.Path(__file__).parent / "fixtures" / "docker-ps-real.json"
        return json.loads(fixture.read_text(encoding="utf-8").strip())

    def test_the_real_row_carries_the_maf_sandbox_labels(self):
        row = self._row()
        assert "maf-sandbox.scope=probe-scope" in row["Labels"]

    def test_the_real_row_ran_sleep_infinity_on_no_network(self):
        row = self._row()
        assert "sleep infinity" in row["Command"]
        assert row["Networks"] == "none"


# ---------------------------------------------------------------------------
# Allowlist egress — internal network + filtering proxy
# ---------------------------------------------------------------------------

_ALLOW_CONFIG = DockerSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
_ALLOW_SPEC = SandboxSpec(
    kind="bicep",
    image="bicep-sandbox:local",
    egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"),
)
_ALLOW_ID = "allow:" + ",".join(sorted(_ALLOW_SPEC.egress_allow))
_AL = _container_name(_KEY, _ALLOW_SPEC.kind, _ALLOW_ID)
_AL_NET = _network_name(_AL)
_AL_PROXY = _proxy_name(_AL)


def _run_named(fake: _FakeDocker, name: str) -> _Recorded:
    found = [c for c in fake.matching("run") if c.args[c.args.index("--name") + 1] == name]
    assert len(found) == 1, [c.args for c in fake.calls]
    return found[0]


class TestAllowlistTopology:
    def test_the_declaration_follows_the_configuration(self):
        assert _backend_with()[0].egress == Egress.CLOSED
        assert _backend_with(config=_ALLOW_CONFIG)[0].egress == Egress.ALLOWLIST

    def test_create_builds_network_proxy_connect_then_workload_in_order(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        order = [
            fake.calls.index(fake.only("network", "create")),
            fake.calls.index(_run_named(fake, _AL_PROXY)),
            fake.calls.index(fake.only("network", "connect")),
            fake.calls.index(_run_named(fake, _AL)),
        ]
        assert order == sorted(order)

    def test_the_network_is_internal_and_labelled(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        args = fake.only("network", "create").args
        assert args[:3] == ("network", "create", "--internal")
        assert args[-1] == _AL_NET

    def test_the_proxy_carries_the_allowlist_and_the_role_label(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        args = _run_named(fake, _AL_PROXY).args
        allow = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert any("MAF_SANDBOX_ALLOW=" in v for v in allow)
        labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        assert "maf-sandbox.role=proxy" in labels

    def test_the_outbound_leg_uses_the_configured_network(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.only("network", "connect").args == ("network", "connect", "bridge", _AL_PROXY)

    def test_a_podman_outbound_network_is_honoured(self):
        config = DockerSandboxConfig(egress_proxy_image="p:local", outbound_network="podman")
        backend, fake = _backend_with(_machine(), config=config)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.only("network", "connect").args[2] == "podman"

    def test_the_workload_gets_the_proxy_in_its_environment(self):
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        args = _run_named(fake, _AL).args
        env = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert any(f"HTTPS_PROXY=http://{_AL_PROXY}:3128" == v for v in env)
        assert args[args.index("--network") + 1] == _AL_NET

    def test_a_closed_spec_stays_network_none_even_with_a_proxy_configured(self):
        """An empty allowlist denies everything for free — no network, no proxy burned on it."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _SPEC))  # _SPEC has egress_allow=()
        assert fake.matching("network", "create") == []
        run = fake.only("run")
        assert run.args[run.args.index("--network") + 1] == "none"


class TestAllowlistReuse:
    def test_an_existing_network_is_adopted_not_treated_as_an_error(self):
        """`network create` on a second acquire returns 'already exists'; adopting it is how
        warm reuse of an allowlisted sandbox works, so it must not raise."""
        overrides = {
            ("network", "create"): _DockerResult(1, b"", "network with name X already exists")
        }
        backend, _ = _backend_with(
            _machine(running=[_AL], overrides=overrides), config=_ALLOW_CONFIG
        )
        # Does not raise: the existing network is adopted, the running workload reused.
        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert sandbox.container_name == _AL


class TestAllowlistTeardown:
    def test_a_fresh_proxy_failure_removes_the_proxy_before_the_network(self):
        """A `network connect` failure leaves the proxy attached, so the proxy must be removed
        before the network or `network rm` fails on 'has active endpoints' and both leak."""
        overrides = {("network", "connect"): _DockerResult(1, b"", "connect failed")}
        backend, fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="outbound leg"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        proxy_rm = fake.matching("rm", "-f", _AL_PROXY)
        net_rm = fake.matching("network", "rm", _AL_NET)
        assert proxy_rm != [] and net_rm != []
        assert fake.calls.index(proxy_rm[-1]) < fake.calls.index(net_rm[-1])


class TestPurgeIsConfigIndependent:
    def test_a_closed_backend_still_reclaims_an_allowlisted_workloads_network(self):
        """A sandbox created under an allowlist must be fully reclaimable through a backend now
        configured closed — the proxy/network sweep is not gated on the current egress config."""
        # The workload is listed (its proxy was deleted by hand); the backend has no proxy image.
        overrides = {("ps",): _DockerResult(0, f"{_AL}\n".encode(), "")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert fake.matching("network", "rm", _AL_NET) != []
