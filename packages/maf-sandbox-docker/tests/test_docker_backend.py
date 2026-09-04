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
import contextlib
import io
import itertools
import logging
import sys
import tarfile
import time
from collections.abc import Mapping, Sequence

import pytest
from maf_sandbox import (
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    Isolation,
    OsFamily,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxKey,
    SandboxOsFamilyNotSupported,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
)

from maf_sandbox_docker import BACKEND_NAME, DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import (
    _ATTACHED_NETWORKS_FORMAT,
    _GATEWAY_MODE_ISOLATED,
    _GATEWAY_MODE_OPTS,
    _NETWORK_ENDPOINTS_FORMAT,
    _container_name,
    _DockerResult,
    _network_name,
    _proxy_name,
)

#: What `network inspect` prints for a network this backend built — one word per address
#: family. Derived rather than written out, so adding a family updates the fixtures with it.
_UNADDRESSED = " ".join([_GATEWAY_MODE_ISOLATED] * len(_GATEWAY_MODE_OPTS))

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY, _SPEC.kind)
_WORK = "/maf-sandbox/work"


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


def _fifo_tar(path: str) -> bytes:
    """A tar carrying a FIFO — non-regular, ``EntryKind.OTHER``, and emphatically not a link."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.type = tarfile.FIFOTYPE
        archive.addfile(entry)
    return buffer.getvalue()


def _directory_tar(path: str) -> bytes:
    """A tar whose first entry is a directory — what ``docker cp`` streams for one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.type = tarfile.DIRTYPE
        entry.mode = 0o755
        archive.addfile(entry)
    return buffer.getvalue()


def _owned_directory_tar(path: str, uid: int, mode: int) -> bytes:
    """A directory entry with an owner and a mode, which is what the reach rule reads."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.type = tarfile.DIRTYPE
        entry.uid, entry.mode = uid, mode
        archive.addfile(entry)
    return buffer.getvalue()


def _cp(guest: str) -> tuple[str, ...]:
    """The ``docker cp`` argv prefix for one guest path — the key a per-path override needs."""
    return ("cp", f"{_NAME}:{guest}")


#: Every stat and every read walks the components from the root down: the root itself,
#: `/maf-sandbox`, then `/maf-sandbox/work`. A fake engine that cannot answer for either
#: refuses both as a path through a non-directory, so all are seeded as directories here —
#: the root root-owned and unwritable, which is what an image build leaves it as.
_WORK_IS_A_DIRECTORY = {
    _cp("/"): _DockerResult(0, _owned_directory_tar(".", 0, 0o755), ""),
    _cp("/maf-sandbox"): _DockerResult(0, _directory_tar("maf-sandbox"), ""),
    _cp(_WORK): _DockerResult(0, _directory_tar(_WORK.lstrip("/")), ""),
}

#: What `docker inspect` prints for a container created with `--cap-drop ALL`.
_CAPS_DROPPED = {("inspect", "-f", "{{.HostConfig.CapDrop}}"): _DockerResult(0, b"[ALL]\n", "")}


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
        self._marked = 0

    async def __call__(
        self, *args: str, stdin=None, timeout=None, read_limit=None
    ) -> _DockerResult:
        self.calls.append(_Recorded(args, stdin, timeout, read_limit))
        result = self._responder(args)
        if read_limit is not None and len(result.stdout) > read_limit:
            result = _DockerResult(result.returncode, result.stdout[:read_limit], result.stderr)
        return result

    def mark(self) -> None:
        """Draw a line under what has been recorded, so a later assertion starts from here."""
        self._marked = len(self.calls)

    def cp_since_mark(self) -> list[tuple[str, ...]]:
        return [call.args for call in self.calls[self._marked :] if call.args[:1] == ("cp",)]

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
    networks: Mapping[str, str] | None = None,
    attached: Mapping[str, Sequence[str]] | None = None,
    peers: Mapping[str, Sequence[str]] | None = None,
):
    """A responder describing which containers and images exist, and how a command answers.

    ``docker inspect -f {{.State.Running}}`` decides existence and running state — a name in
    ``running`` prints ``true``, one only in ``stopped`` prints ``false``, one in neither errors
    like a missing container. ``image inspect`` succeeds for a known image and errors otherwise.

    ``attached`` maps a container to the networks it is on; one not listed reports the network
    this backend would have created it on, which is what a sandbox it built looks like.

    ``rm -f`` takes a container out of that state, because ``acquire`` reads it again after a
    removal and a responder still answering "running" sends the acquire down its reuse branch.

    ``networks`` maps a network name to what ``network inspect`` prints for the gateway-mode
    format — one word per address family, so ``"isolated isolated"`` is a network this backend
    built, ``"isolated"`` one addressed on IPv6 only, and ``""`` one built before either option
    was asked for. A name absent from it answers "not found", the cold path an acquire that has
    yet to build one takes. ``network rm`` removes the name, so a teardown's postcondition sees
    what the teardown did; override ``("network", "rm")`` to model one that fails.

    The longest matching ``overrides`` prefix wins, so a per-path ``cp`` answer beats a
    catch-all one however the mapping was written.
    """
    live_running = set(running)
    live_stopped = set(stopped)
    live_networks = dict(networks or {})
    live_attached = {k: list(v) for k, v in (attached or {}).items()}
    live_endpoints: dict[str, set[str]] = {}
    # A container the test declares as already there is already on its network, the way
    # one this responder created would be.
    for _c in live_running | live_stopped:
        for _n in live_attached.get(_c, [_network_name(_c)]):
            live_endpoints.setdefault(_n, set()).add(_c)
    ranked = sorted((overrides or {}).items(), key=lambda item: len(item[0]), reverse=True)

    def respond(args: tuple[str, ...]) -> _DockerResult:
        for prefix, result in ranked:
            if args[: len(prefix)] == prefix:
                return result
        if args[:2] == ("rm", "-f"):
            name = args[-1]
            live_running.discard(name)
            live_stopped.discard(name)
            live_attached.pop(name, None)
            for holders in live_endpoints.values():
                holders.discard(name)
            return _DockerResult(0, name.encode() + b"\n", "")
        if args[:3] == ("run", "-d", "--name"):
            # A create the engine accepted leaves a running container on the network it was
            # given, which the reads after it are entitled to find — including the attachment
            # check before the sandbox is handed out. So a replacement is attached correctly
            # even where the container it replaced was not.
            name = args[3]
            live_running.add(name)
            if "--network" in args:
                net = args[args.index("--network") + 1]
                live_attached[name] = [net]
                live_endpoints.setdefault(net, set()).add(name)
            return _DockerResult(0, name.encode() + b"\n", "")
        if args[:2] == ("image", "inspect"):
            image = args[2]
            return (
                _DockerResult(0, b"", "")
                if image in images
                else _DockerResult(1, b"", "No such image")
            )
        if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT):
            name = args[-1]
            if name not in live_running | live_stopped:
                return _DockerResult(1, b"", f"error: no such object: {name}")
            on = live_attached.get(name, [_network_name(name)])
            return _DockerResult(0, (" ".join(on) + " ").encode(), "")
        if args[:2] == ("network", "connect"):
            # The proxy's second leg. Without it the proxy reads as single-homed, which is the
            # shape that means an allowlisted sandbox has no way out at all.
            net, target = args[2], args[3]
            live_attached.setdefault(target, [_network_name(target)]).append(net)
            live_endpoints.setdefault(net, set()).add(target)
            return _DockerResult(0, b"", "")
        if args[:2] == ("network", "disconnect"):
            net, target = args[2], args[3]
            if net in live_attached.get(target, []):
                live_attached[target].remove(net)
            live_endpoints.get(net, set()).discard(target)
            return _DockerResult(0, b"", "")
        if args[:4] == ("network", "inspect", "-f", _NETWORK_ENDPOINTS_FORMAT):
            net = args[-1]
            if net not in live_networks:
                return _DockerResult(1, b"", f"Error response from daemon: network {net} not found")
            # What this responder has put on it, plus any intruder a test named.
            on = live_endpoints.get(net, set()) | set((peers or {}).get(net, ()))
            return _DockerResult(0, (" ".join(sorted(on)) + " ").encode(), "")
        if args[:2] == ("network", "inspect"):
            net = args[-1]
            modes = live_networks.get(net)
            if modes is None:
                return _DockerResult(1, b"", f"Error response from daemon: network {net} not found")
            return _DockerResult(0, modes.encode() + b"\n", "")
        if args[:2] == ("network", "rm"):
            net = args[-1]
            live_endpoints.pop(net, None)
            if live_networks.pop(net, None) is None:
                return _DockerResult(1, b"", f"Error: No such network: {net}")
            return _DockerResult(0, net.encode() + b"\n", "")
        if args[:2] == ("network", "create"):
            # A create the engine accepted leaves a network the reads after it can find, and
            # one it refused as taken leaves whatever was already there — so a test cannot end
            # up running a workload on a network this responder says does not exist.
            net = args[-1]
            if net in live_networks:
                return _DockerResult(1, b"", f"network with name {net} already exists")
            modes = [o.split("=", 1)[1] for o in args if o.startswith("com.docker.network.")]
            live_networks[net] = " ".join(modes)
            return _DockerResult(0, net.encode() + b"\n", "")
        if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
            return _DockerResult(0, b"\n", "")
        if args[0] == "cp" and args[1].endswith(":/"):
            # Every walk now stats the root, and a real engine answers it with the root
            # directory's own header — root's, writable by nobody else on any sane image.
            return _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
        if args[0] == "inspect":
            name = args[-1]
            if name not in live_running | live_stopped:
                return _DockerResult(1, b"", f"Error: No such object: {name}")
            state = "true" if name in live_running else "false"
            return _DockerResult(0, state.encode() + b"\n", "")
        if args[:2] == ("ps", "-a") or args[0] == "ps":
            names = [*live_running, *live_stopped] if "-a" in args else list(live_running)
            return _DockerResult(0, "".join(f"{n}\n" for n in names).encode(), "")
        if args[0] == "logs":
            return _DockerResult(0, b"listening on 3128\n", "")
        if args[0] == "cp" and args[1] != "-":
            return _DockerResult(1, b"", "no such file")
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


def _created_with(monkeypatch, responder=None, config=None):
    """A backend built through `create`, so the daemon read actually runs.

    The seam is patched on the *class* rather than the instance because `create` builds the
    instance itself, and re-bound to that instance afterwards so the rest of a test keeps
    talking to the same fake.
    """
    fake = _FakeDocker(responder)

    async def seam(_self, *args, stdin=None, timeout=None, read_limit=None):
        return await fake(*args, stdin=stdin, timeout=timeout, read_limit=read_limit)

    monkeypatch.setattr(DockerSandboxBackend, "_docker", seam)
    backend = asyncio.run(DockerSandboxBackend.create(config or DockerSandboxConfig()))
    backend._docker = fake  # type: ignore[method-assign]
    return backend, fake


def _daemon_running(os_name: bytes | None, base=None):
    """`_machine`, with `docker version` answering `os_name` — `None` failing the read."""
    machine = base or _machine()

    def respond(args: tuple[str, ...]) -> _DockerResult:
        if args[:1] == ("version",):
            if os_name is None:
                return _DockerResult(1, b"", "Cannot connect to the Docker daemon")
            return _DockerResult(0, os_name, "")
        return machine(args)

    return respond


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

    def test_declares_closed_only_without_a_proxy(self):
        assert DockerSandboxBackend(DockerSandboxConfig()).declarations.egress_modes == frozenset(
            {Egress.CLOSED}
        )

    def test_declares_allowlist_and_closed_with_a_proxy(self):
        config = DockerSandboxConfig(egress_proxy_image="proxy:local")
        assert DockerSandboxBackend(config).declarations.egress_modes == frozenset(
            {Egress.ALLOWLIST, Egress.CLOSED}
        )

    def test_declares_exec_files_in_files_out_and_host_tools(self):
        caps = DockerSandboxBackend(DockerSandboxConfig()).declarations.capabilities
        assert caps == frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_DELETE,
                Capability.HOST_TOOLS,
            }
        )

    def test_does_not_declare_files_list(self):
        assert (
            Capability.FILES_LIST
            not in DockerSandboxBackend(DockerSandboxConfig()).declarations.capabilities
        )

    def test_is_named_docker(self):
        # The literal, on purpose. `name == BACKEND_NAME` below pins them to each other and
        # would stay green if both moved together — and both moving together is precisely the
        # change that silently breaks every host with `selected="docker"` in its configuration.
        assert DockerSandboxBackend(DockerSandboxConfig()).name == "docker"

    def test_the_exported_constant_is_the_name_the_backend_answers_to(self):
        """#411: the value exists without building a backend, and cannot drift from it."""
        assert BACKEND_NAME == DockerSandboxBackend(DockerSandboxConfig()).name

    def test_selecting_by_the_constant_resolves_to_this_backend(self):
        """What the constant is for, exercised rather than asserted.

        `selected=` is a string match against `.name`, so this is the only test that would fail
        if the constant were right and the property were reading something else.
        """
        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, selected=BACKEND_NAME)
        assert router.backend is backend

    def test_declares_transfer_limits(self):
        limits = DockerSandboxBackend(DockerSandboxConfig()).declarations.limits
        assert limits.files_out.max_files >= 1
        assert limits.files_in.max_bytes_per_file >= 1


# ---------------------------------------------------------------------------
# The guest family — read off the daemon by `create`, matched by the router at attach (#587)
# ---------------------------------------------------------------------------


class TestGuestFamilyDeclaration:
    """What `os_families` says, and the one daemon answer that entitles it to say anything."""

    def test_the_plain_constructor_declares_nothing(self):
        """`__init__` makes no engine calls, so it has nothing to declare — and says so."""
        assert DockerSandboxBackend(DockerSandboxConfig()).declarations.os_families == frozenset()

    def test_a_linux_daemon_declares_posix(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(b"linux\n"))
        assert backend.declarations.os_families == frozenset({OsFamily.POSIX})

    def test_the_daemon_is_asked_with_version_and_the_ostype_template(self, monkeypatch):
        _, fake = _created_with(monkeypatch, _daemon_running(b"linux\n"))
        assert fake.only("version").args == ("version", "--format", "{{.Server.Os}}")

    def test_the_read_is_bounded_by_the_command_timeout(self, monkeypatch):
        """Measured: an unroutable DOCKER_HOST does not refuse, it hangs. So this one is timed."""
        config = DockerSandboxConfig(command_timeout_seconds=7.5)
        _, fake = _created_with(monkeypatch, _daemon_running(b"linux\n"), config=config)
        assert fake.only("version").timeout == 7.5

    def test_the_declaration_is_read_once_and_then_answered_from_memory(self, monkeypatch):
        backend, fake = _created_with(monkeypatch, _daemon_running(b"linux\n"))
        for _ in range(3):
            assert backend.declarations.os_families == frozenset({OsFamily.POSIX})
        assert len(fake.matching("version")) == 1

    def test_a_windows_daemon_declares_nothing_rather_than_windows(self, monkeypatch):
        """The refusal that keeps this backend honest: `exec` is `sh -c` and removals are
        `rm -rf`, so `WINDOWS` would be a guarantee no code path here backs."""
        backend, _ = _created_with(monkeypatch, _daemon_running(b"windows\n"))
        assert backend.declarations.os_families == frozenset()

    def test_a_daemon_that_will_not_answer_declares_nothing(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(None))
        assert backend.declarations.os_families == frozenset()

    def test_a_client_that_is_not_installed_declares_nothing(self, monkeypatch):
        """`create` reads a declaration; it is not a health check, so it raises nothing."""
        backend, _ = _created_with(monkeypatch, _explodes)
        assert backend.declarations.os_families == frozenset()

    def test_an_empty_answer_declares_nothing(self, monkeypatch):
        """An engine whose `--format` does not speak this template exits 0 and prints nothing."""
        backend, _ = _created_with(monkeypatch, _daemon_running(b"\n"))
        assert backend.declarations.os_families == frozenset()

    def test_the_answer_is_read_case_insensitively(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(b"Linux\n"))
        assert backend.declarations.os_families == frozenset({OsFamily.POSIX})


class TestTheRouterMatchesTheDeclaredFamily:
    """The point of the declaration: an axis that refuses something, at attach."""

    @staticmethod
    def _router(backend) -> SandboxRouter:
        return SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

    def test_a_posix_workload_is_served_by_a_linux_daemon(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(b"linux\n"))
        spec = SandboxSpec(kind="bicep", image="i:local", requires_os_family=OsFamily.POSIX)
        self._router(backend).ensure_can_serve(spec)

    def test_a_windows_workload_is_refused_by_a_linux_daemon(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(b"linux\n"))
        spec = SandboxSpec(kind="bicep", image="i:local", requires_os_family=OsFamily.WINDOWS)
        with pytest.raises(SandboxOsFamilyNotSupported):
            self._router(backend).ensure_can_serve(spec)

    def test_a_backend_that_declared_nothing_refuses_a_spec_that_asks(self, monkeypatch):
        """Unchanged behaviour, pinned: silence refuses only a spec naming a family."""
        backend, _ = _created_with(monkeypatch, _daemon_running(None))
        spec = SandboxSpec(kind="bicep", image="i:local", requires_os_family=OsFamily.POSIX)
        with pytest.raises(SandboxOsFamilyNotSupported):
            self._router(backend).ensure_can_serve(spec)

    def test_a_spec_naming_no_family_is_served_either_way(self, monkeypatch):
        backend, _ = _created_with(monkeypatch, _daemon_running(None))
        self._router(backend).ensure_can_serve(_SPEC)
        self._router(DockerSandboxBackend(DockerSandboxConfig())).ensure_can_serve(_SPEC)


class TestTheDaemonMovingUnderTheDeclaration:
    """`os_families` is a snapshot: the client resolves DOCKER_HOST and the active context per
    invocation, so switching Docker Desktop to Windows containers moves the engine under a
    running host. A create re-asks; everything else does not."""

    @staticmethod
    def _switchable(daemon: dict[str, bytes]):
        machine = _machine()

        def respond(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("version",):
                return _DockerResult(0, daemon["os"], "")
            return machine(args)

        return respond

    def test_a_create_is_refused_when_the_daemon_no_longer_runs_linux(self, monkeypatch):
        daemon = {"os": b"linux\n"}
        backend, _ = _created_with(monkeypatch, self._switchable(daemon))
        daemon["os"] = b"windows\n"
        with pytest.raises(SandboxOsFamilyNotSupported, match="moved under this backend"):
            asyncio.run(backend.acquire(_KEY, _SPEC))

    def test_the_refusal_leaves_nothing_behind_to_dispose(self, monkeypatch):
        """Ahead of the create *and* of the egress scaffolding, so there is nothing to clean."""
        daemon = {"os": b"linux\n"}
        config = DockerSandboxConfig(egress_proxy_image="proxy:local")
        backend, fake = _created_with(monkeypatch, self._switchable(daemon), config=config)
        daemon["os"] = b"windows\n"
        spec = SandboxSpec(
            kind="bicep",
            image="bicep-sandbox:local",
            egress=Egress.ALLOWLIST,
            egress_allow=("example.com",),
        )
        with pytest.raises(SandboxOsFamilyNotSupported):
            asyncio.run(backend.acquire(_KEY, spec))
        assert fake.matching("run") == []
        assert fake.matching("network", "create") == []

    def test_a_stopped_container_is_not_restarted_onto_a_moved_daemon(self, monkeypatch):
        """A restart hands out a container from whichever daemon is answering now, so it is
        gated exactly like a create — and refused before `docker start` runs."""
        daemon = {"os": b"linux\n"}
        machine = _machine(stopped=[_NAME])

        def respond(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("version",):
                return _DockerResult(0, daemon["os"], "")
            return machine(args)

        backend, fake = _created_with(monkeypatch, respond)
        daemon["os"] = b"windows\n"
        with pytest.raises(SandboxOsFamilyNotSupported):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("start") == []
        assert fake.matching("run") == []

    def test_a_restart_that_would_fall_through_to_a_create_is_refused_first(self, monkeypatch):
        """The path a create-only gate missed: `_restart` removes a container that will not
        start and falls through to a create, so a guard asking "does no container exist?" let
        that create through unchecked."""
        daemon = {"os": b"linux\n"}
        machine = _machine(
            stopped=[_NAME], overrides={("start",): _DockerResult(1, b"", "cannot start")}
        )

        def respond(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("version",):
                return _DockerResult(0, daemon["os"], "")
            return machine(args)

        backend, fake = _created_with(monkeypatch, respond)
        daemon["os"] = b"windows\n"
        with pytest.raises(SandboxOsFamilyNotSupported):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") == []

    def test_a_warm_container_is_served_without_asking_again(self, monkeypatch):
        """The stated residual: a *running* container is served without a round trip, because
        re-asking here would put one in front of every tool call. Reaching it takes a switch to
        an engine already running a container under the same derived name."""
        daemon = {"os": b"linux\n"}
        machine = _machine(running=[_NAME])

        def respond(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("version",):
                return _DockerResult(0, daemon["os"], "")
            return machine(args)

        backend, fake = _created_with(monkeypatch, respond)
        asked_at_create = len(fake.matching("version"))
        daemon["os"] = b"windows\n"
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert len(fake.matching("version")) == asked_at_create

    def test_a_backend_that_declared_nothing_never_asks(self):
        """No declaration, no promise to re-check, and no round trip on the create path."""
        backend, fake = _backend_with(_daemon_running(b"windows\n"))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("version") == []
        assert fake.matching("run") != []

    def test_a_daemon_that_will_not_answer_now_is_served(self, monkeypatch):
        """An unreadable re-check serves: the create is about to fail on its own terms, and
        refusing on a transient would take a working deployment off the air."""
        answers = {"failing": False}
        machine = _machine()

        def respond(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("version",):
                if answers["failing"]:
                    return _DockerResult(1, b"", "Cannot connect to the Docker daemon")
                return _DockerResult(0, b"linux\n", "")
            return machine(args)

        backend, fake = _created_with(monkeypatch, respond)
        answers["failing"] = True
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") != []


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

    def test_a_codeact_style_spec_wiring_host_tools_is_admitted(self):
        """The whole point of declaring it: the spec a wired registry produces now attaches.

        Asserted through `ensure_can_serve` rather than by re-reading the frozenset, because
        the set agreeing with itself is not the property that changed — a spec being admitted
        is. This is the exact `requires` `codeact_sandbox_spec` builds for a non-empty
        registry, so it fails if either side of that pair drifts.
        """
        router = SandboxRouter(
            [DockerSandboxBackend(DockerSandboxConfig())], min_isolation=Isolation.CONTAINER
        )
        spec = SandboxSpec(
            kind="codeact",
            requires=frozenset(
                {
                    Capability.EXEC,
                    Capability.FILES_IN,
                    Capability.FILES_OUT,
                    Capability.HOST_TOOLS,
                }
            ),
        )

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
            max_bytes_per_file=backend.declarations.limits.files_out.max_bytes_per_file + 1,
            max_total_bytes=backend.declarations.limits.files_out.max_total_bytes,
            max_files=backend.declarations.limits.files_out.max_files,
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


class TestRunCode:
    """This backend declares no RUN_CODE, and says why rather than failing bare."""

    def test_run_code_raises_notimplementederror(self):
        """The image is a reference this backend hands to the engine without parsing, so which
        runtime is inside it is not something the backend knows. A workload that wants an
        interpreter by name invokes it through `exec` and owns that assumption itself."""
        backend, _fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(NotImplementedError, match="RUN_CODE"):
            asyncio.run(sandbox.run_code("print(1)", timeout=5.0))


class TestRemove:
    """`rm -rf` is irreversible, so the command this builds is pinned rather than trusted."""

    def _sandbox(self):
        # The walk stats every ancestor, so the fake has to answer for them; anything else
        # under the work directory is simply not there, which a removal treats as success.
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_recursive_removal_is_rm_rf_behind_a_double_dash(self):
        """`--` is what keeps a path opening with a dash from being read as a flag.

        The path is guest-shaped and a run directory is named by the caller, so the guard is
        cheap insurance against the one argv position where a name becomes an option.
        """
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.remove("run-1", working_directory=_WORK, recursive=True))
        assert fake.only("exec").args == (
            "exec",
            "--user",
            "0",
            "-w",
            _WORK,
            _NAME,
            "rm",
            "-rf",
            "--",
            f"{_WORK}/run-1",
        )

    def test_without_recursive_the_flag_is_f_alone(self):
        """`-f` makes a missing path succeed and leaves `rm` to refuse a directory.

        Sending `-rf` here would silently widen every single-file delete into a tree delete —
        the one mistake in this method that no test above would notice.
        """
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        args = fake.only("exec").args
        assert args[6:] == ("rm", "-f", "--", f"{_WORK}/a.txt")

    def test_the_working_directory_itself_is_refused_before_any_command_runs(self):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError):
            asyncio.run(sandbox.remove(".", working_directory=_WORK, recursive=True))
        assert [call for call in fake.calls if call.args[:1] == ("exec",)] == []

    def test_a_path_outside_the_working_directory_is_refused(self):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError):
            asyncio.run(sandbox.remove("../../etc", working_directory=_WORK, recursive=True))
        assert [call for call in fake.calls if call.args[:1] == ("exec",)] == []


class TestReclaim:
    """`reclaim` is `remove`'s mechanism without its confinement duty: no walk, straight to `rm`."""

    def _sandbox(self, overrides=None):
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_directory_is_removed_via_rm_rf_behind_a_double_dash(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        assert fake.only("exec").args == (
            "exec",
            "--user",
            "0",
            "-w",
            "/",
            _NAME,
            "rm",
            "-rf",
            "--",
            f"{_WORK}/call-a1b2c3",
        )

    def test_a_missing_directory_is_success(self):
        """`rm -rf` already exits 0 on a path that is not there; this pins that no raise follows."""
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim(f"{_WORK}/never-there", working_directory=_WORK, timeout=30))

    def test_a_nonzero_exit_raises_with_the_exit_code_and_what_the_guest_said(self):
        """The message is the whole diagnosis a host gets: core turns it into
        `ReclaimFailure.reason` and hands that to `on_reclaim_failure`. A read-only
        filesystem, a full disk and a permission denial are told apart only by these two.
        """
        overrides = {("exec",): _DockerResult(1, b"", "rm: permission denied")}
        sandbox, fake = self._sandbox(overrides)
        with pytest.raises(OSError, match=r"rm exited 1.*rm: permission denied"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=30))

    def test_the_timeout_reaches_the_transport(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=42))
        assert fake.only("exec").timeout == 42

    def test_the_removal_runs_from_root_not_the_uncreated_working_directory(self):
        """`working_directory` says where the directory sits, not where to run the removal
        from: no backend creates a spec's `work_dir`, so a call whose work dir was never
        written must still reclaim cleanly, which it can only do by execing from `/` rather
        than a directory that is not there.
        """
        sandbox, fake = self._sandbox()
        asyncio.run(
            sandbox.reclaim(
                f"{_WORK}/never-created/call-a1b2c3",
                working_directory=f"{_WORK}/never-created",
                timeout=30,
            )
        )
        assert fake.only("exec").args[:5] == ("exec", "--user", "0", "-w", "/")

    def test_a_name_a_shell_would_read_stays_one_argument(self):
        """Core dispatches the path unaltered; this backend's argv `exec` is what keeps the
        name one argument. A `work_dir` is host-supplied, so a name holding a space or a `;`
        is reachable, and one that split would have `rm -rf` delete something else.
        """
        hostile = f"{_WORK}/a b; touch pwned"
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim(hostile, working_directory=_WORK, timeout=30))
        assert fake.only("exec").args[-1] == hostile


class TestWhichPrincipalACommandCarries:
    """The file plane is the host's; `exec` and `run_code` are the guest program's."""

    def _sandbox(self, overrides=None, *, capabilities_dropped=False):
        merged = {
            **_WORK_IS_A_DIRECTORY,
            ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
            **(_CAPS_DROPPED if capabilities_dropped else {}),
            **(overrides or {}),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=merged))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_guest_command_is_the_argv_and_nothing_else(self):
        """The whole tuple, so the absence of `--user` is asserted rather than searched for."""
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.exec(["whoami"], working_directory=_WORK, timeout=5))
        assert fake.only("exec").args == ("exec", "-w", _WORK, _NAME, "whoami")

    def test_a_refused_removal_is_retried_when_capabilities_were_dropped(self):
        """`--user 0` is a uid, not a capability set: without `CAP_DAC_OVERRIDE` root empties
        only what it owns.
        """
        refused = {("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied")}
        sandbox, fake = self._sandbox(refused, capabilities_dropped=True)
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        assert [call.args[:3] for call in fake.matching("exec")] == [
            ("exec", "--user", "0"),
            ("exec", "-w", "/"),
        ]

    def test_a_refused_removal_is_not_retried_while_root_keeps_its_capabilities(self):
        """With `CAP_DAC_OVERRIDE` root empties anything, so a refusal is not about ownership
        and a retry would report its own error over the one that mattered.
        """
        refused = {("exec",): _DockerResult(1, b"", "rm: read-only file system")}
        sandbox, fake = self._sandbox(refused)
        with pytest.raises(OSError, match="read-only file system"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=30))
        assert len(fake.matching("exec")) == 1

    def test_a_removal_root_could_make_is_not_retried(self):
        sandbox, fake = self._sandbox(capabilities_dropped=True)
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        assert len(fake.matching("exec")) == 1

    def test_a_removal_neither_can_make_raises_with_what_the_guest_said(self):
        """The fallback must not swallow a failure that is nothing to do with ownership."""
        both = {("exec",): _DockerResult(1, b"", "rm: read-only file system")}
        sandbox, fake = self._sandbox(both, capabilities_dropped=True)
        with pytest.raises(OSError, match="read-only file system"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=30))
        assert len(fake.matching("exec")) == 2

    def test_the_retry_gets_what_is_left_of_the_one_deadline(self):
        """`reclaim(timeout=T)` promises completion within T, not 2T — two attempts each handed
        the full timeout still succeed, just twice as late as the contract allows.
        """
        spent = 0.05
        base = _machine(running=[_NAME], overrides={**_WORK_IS_A_DIRECTORY, **_CAPS_DROPPED})

        def slow(args):
            if args[:3] == ("exec", "--user", "0"):
                time.sleep(spent)
                return _DockerResult(1, b"", "rm: Permission denied")
            return base(args)

        backend, fake = _backend_with(slow)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))

        first, second = fake.matching("exec")
        assert first.timeout == 30
        assert second.timeout is not None and second.timeout <= 30 - spent

    def test_both_attempts_messages_reach_the_caller(self):
        """A failure that was nothing to do with ownership is retried too, so the second
        attempt must not be the only thing the caller hears about.
        """
        differ = {
            ("exec", "--user", "0"): _DockerResult(1, b"", "rm: read-only file system"),
            ("exec", "-w"): _DockerResult(1, b"", "rm: Permission denied"),
        }
        sandbox, _fake = self._sandbox(differ, capabilities_dropped=True)
        with pytest.raises(OSError, match=r"Permission denied.*as root: rm: read-only file"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=30))

    def test_a_refused_remove_is_retried_the_same_way(self):
        refused = {("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied")}
        sandbox, fake = self._sandbox(refused, capabilities_dropped=True)
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        assert [call.args[:3] for call in fake.matching("exec")] == [
            ("exec", "--user", "0"),
            ("exec", "-w", _WORK),
        ]


class TestTheReachRuleChoosesThePrincipal:
    """The reach rule: root is for paths with no component the guest could have swapped."""

    def _sandbox(self, work_dir_entry: bytes):
        overrides = {
            _cp("/"): _DockerResult(0, _owned_directory_tar(".", 0, 0o755), ""),
            _cp("/maf-sandbox"): _DockerResult(0, _directory_tar("maf-sandbox"), ""),
            _cp(_WORK): _DockerResult(0, work_dir_entry, ""),
            ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_path_the_guest_could_not_have_touched_is_removed_as_root(self):
        sandbox, fake = self._sandbox(_owned_directory_tar(_WORK.lstrip("/"), 0, 0o755))
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        assert fake.only("exec").args[:3] == ("exec", "--user", "0")

    def test_an_unreadable_root_keeps_the_removal_at_the_guest_s_and_running(self):
        """The per-remove probe for `/` owes the removal an answer it cannot give when the
        daemon will not describe it: the removal still runs, so a broken engine breaks no
        delete, and stays at the guest's authority because nothing was verified."""

        def refuses(args):
            if args[:2] == ("cp", f"{_NAME}:/"):
                raise RuntimeError("the daemon said no")
            return _machine(running=[_NAME], overrides=_WORK_IS_A_DIRECTORY)(args)

        backend, fake = _backend_with(refuses)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        exec_args = fake.only("exec").args
        assert "--user" not in exec_args
        assert exec_args[-4:] == ("rm", "-f", "--", f"{_WORK}/a.txt")

    def test_a_writable_root_withholds_root_from_the_removal_itself(self):
        """The twin of the acquire-side probe: a root the guest could have written is the
        swap the walk's own components cannot witness, so the removal borrows no root
        however clean the directories below it are."""

        writable = {
            **_WORK_IS_A_DIRECTORY,
            ("cp", f"{_NAME}:/"): _DockerResult(0, _owned_directory_tar(".", 0, 0o777), ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=writable))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        exec_args = fake.only("exec").args
        assert "--user" not in exec_args
        assert exec_args[-4:] == ("rm", "-f", "--", f"{_WORK}/a.txt")

    def test_a_component_the_guest_owns_keeps_the_removal_at_the_guest_authority(self):
        """The guest can swap what it owns, so root here would delete what it could not."""
        sandbox, fake = self._sandbox(_owned_directory_tar(_WORK.lstrip("/"), 10001, 0o755))
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        assert "--user" not in fake.only("exec").args

    def test_a_root_owned_component_anyone_may_write_is_the_guests_too(self):
        """Ownership alone is not the question — `0777` under root is writable by the guest."""
        sandbox, fake = self._sandbox(_owned_directory_tar(_WORK.lstrip("/"), 0, 0o777))
        asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))
        assert "--user" not in fake.only("exec").args

    def test_reclaim_raises_authority_without_a_walk(self):
        """`reclaim` owes no walk, so the argument stands in for one."""
        sandbox, fake = self._sandbox(_owned_directory_tar(_WORK.lstrip("/"), 10001, 0o755))
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        assert fake.only("exec").args[:3] == ("exec", "--user", "0")


class TestTheAncestorsAboveTheWorkDirAreChecked:
    """The half of `reclaim`'s argument that is read rather than asserted, once per container."""

    def _backend(self, parent: bytes | None, image: str = _SPEC.image):
        overrides = {("cp",): _DockerResult(1, b"", "Error: Could not find the file in container")}
        overrides[("cp", f"{_NAME}:/")] = _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
        if parent is not None:
            overrides[_cp("/maf-sandbox")] = _DockerResult(0, parent, "")
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return backend, fake, SandboxSpec(kind=_SPEC.kind, image=image)

    def _reclaimed_as(self, backend, fake, spec) -> tuple[str, ...]:
        sandbox = asyncio.run(backend.acquire(_KEY, spec))
        fake.mark()
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        return fake.only("exec").args[:3]

    def test_a_host_owned_chain_lets_reclaim_remove_as_root(self):
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        assert self._reclaimed_as(backend, fake, spec) == ("exec", "--user", "0")

    def test_an_ancestor_the_guest_may_write_keeps_reclaim_at_the_guest_authority(self):
        """A swapped parent is followed rather than unlinked, so root there would delete what
        the guest could not.
        """
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o777))
        assert "--user" not in self._reclaimed_as(backend, fake, spec)

    def test_an_ancestor_owned_by_someone_else_does_the_same(self):
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 10001, 0o755))
        assert "--user" not in self._reclaimed_as(backend, fake, spec)

    def test_an_unreadable_ancestor_fails_closed(self):
        """An engine that will not answer leaves the removal at the guest's authority."""

        def refuses(args):
            if args[:2] == ("cp", f"{_NAME}:/maf-sandbox"):
                raise RuntimeError("the daemon said no")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(refuses)
        assert "--user" not in self._reclaimed_as(backend, fake, _SPEC)

    def test_an_unreadable_root_does_the_same(self):
        """Nothing verified, nothing licensed: the root is the swap the directories below it
        cannot witness, so a walk that cannot read it licenses no removal at all."""

        def refuseless(args):
            if args[:2] == ("cp", f"{_NAME}:/"):
                raise RuntimeError("the daemon said no")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(refuseless)
        assert "--user" not in self._reclaimed_as(backend, fake, _SPEC)

    def test_a_writable_root_is_what_closes_licensing(self):
        """A root the guest could have written is the swap the chain above the work dir cannot
        see — its header, read by the same walk, is what the rule rests on."""

        def writable(args):
            if args[:2] == ("cp", f"{_NAME}:/"):
                return _DockerResult(0, _owned_directory_tar(".", 0, 0o777), "")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(writable)
        assert "--user" not in self._reclaimed_as(backend, fake, _SPEC)

    def test_a_work_dir_straight_under_the_root_is_answered_by_the_root_alone(self):
        """`/work` has no ancestors above it, so the walk is just ``/`` — the component the
        chain never reached, and the one every other component's replacement relies on."""

        def root_only(args):
            if args[:2] == ("cp", f"{_NAME}:/maf-sandbox"):
                raise RuntimeError("the daemon said no")
            if args[:2] == ("cp", f"{_NAME}:/"):
                return _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(root_only)
        spec = SandboxSpec(kind=_SPEC.kind, image=_SPEC.image, work_dir="/work")
        asyncio.run(backend.acquire(_KEY, spec))
        assert [f.host_owned_ancestors for f in backend._facts.values()] == [True]
        assert fake.matching("cp", f"{_NAME}:/maf-sandbox") == []

    def test_the_answer_is_read_once_per_container(self):
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        asyncio.run(backend.acquire(_KEY, spec))
        fake.mark()
        asyncio.run(backend.acquire(_KEY, spec))
        assert fake.cp_since_mark() == []

    def test_the_answer_is_re_read_when_the_image_changes(self):
        """A container name never carries the image, so one can come back with a different one."""
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        asyncio.run(backend.acquire(_KEY, spec))
        fake.mark()
        asyncio.run(backend.acquire(_KEY, SandboxSpec(kind=_SPEC.kind, image="other:local")))
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]

    def test_a_changed_image_id_re_reads_even_where_the_image_name_holds_still(self):
        """`image_id` is what `_create_workload` runs when a spec carries one, so it is what
        the key has to follow.
        """
        backend, fake, _ = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        pinned = SandboxSpec(kind=_SPEC.kind, image="same:local", image_id="sha256:aaa")
        asyncio.run(backend.acquire(_KEY, pinned))
        fake.mark()
        asyncio.run(
            backend.acquire(
                _KEY, SandboxSpec(kind=_SPEC.kind, image="same:local", image_id="sha256:bbb")
            )
        )
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]

    def test_the_same_image_id_is_still_read_once(self):
        backend, fake, _ = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        pinned = SandboxSpec(kind=_SPEC.kind, image="same:local", image_id="sha256:aaa")
        asyncio.run(backend.acquire(_KEY, pinned))
        fake.mark()
        asyncio.run(backend.acquire(_KEY, pinned))
        assert fake.cp_since_mark() == []

    def test_removing_the_container_forgets_the_answer(self):
        backend, fake, spec = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        asyncio.run(backend.acquire(_KEY, spec))
        asyncio.run(backend.dispose(_KEY))
        fake.mark()
        asyncio.run(backend.acquire(_KEY, spec))
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]


class TestTheHardeningIsReadFromTheContainer:
    """`acquire` reuses a container by a name that carries no hardening, so the config is not
    evidence about the container it got.
    """

    def _reclaim_calls(self, config, container_says_dropped: bool) -> list[tuple[str, ...]]:
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied"),
            **(_CAPS_DROPPED if container_says_dropped else {}),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides), config)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        return [call.args[:3] for call in fake.matching("exec")]

    def test_a_hardened_container_retries_even_though_this_config_is_not(self):
        calls = self._reclaim_calls(DockerSandboxConfig(cap_drop_all=False), True)
        assert calls == [("exec", "--user", "0"), ("exec", "-w", "/")]

    def test_an_unhardened_container_does_not_retry_even_though_this_config_would(self):
        backend_config = DockerSandboxConfig(cap_drop_all=True)
        with pytest.raises(OSError):
            self._reclaim_calls(backend_config, False)

    def test_a_container_that_will_not_say_is_treated_as_hardened(self):
        """Unknown costs one extra `exec` on a removal that failed anyway."""
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied"),
            ("inspect", "-f", "{{.HostConfig.CapDrop}}"): _DockerResult(1, b"", "no such object"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))
        assert len(fake.matching("exec")) == 2


class TestAContainerThatVanishedBehindThisBackend:
    """A name is not a container: a `docker rm` this backend did not run invalidates the facts.

    `_remove` drops them, but nothing outside this process goes through it — a `docker rm` at
    a terminal, a pruned daemon, a host reboot. The create branch is where that is noticed.
    """

    _CALL = f"{_WORK}/call-a1b2c3"

    def _backend(self, present: set[str], hardening: list[bytes]):
        base = _machine(
            running=[_NAME],
            overrides={
                **_WORK_IS_A_DIRECTORY,
                ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
                ("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied"),
            },
        )

        def respond(args):
            if args[:3] == ("inspect", "-f", "{{.HostConfig.CapDrop}}"):
                return _DockerResult(0, hardening[0], "")
            if args[0] == "inspect" and args[-1] not in present:
                return _DockerResult(1, b"", f"Error: No such object: {args[-1]}")
            return base(args)

        return _backend_with(respond)

    def test_the_replacement_container_decides_its_own_removals(self):
        """The consequence, not the cache: a root refusal is retried only where the container
        holds no `CAP_DAC_OVERRIDE`, so stale facts leave a hardened container unable to remove.
        """
        present, hardening = {_NAME}, [b"[]\n"]
        backend, fake = self._backend(present, hardening)

        keeps_capabilities = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(OSError, match="Permission denied"):
            asyncio.run(keeps_capabilities.reclaim(self._CALL, working_directory=_WORK, timeout=30))

        # Removed by something that is not this backend, and the name taken by a hardened one.
        present.discard(_NAME)
        hardening[0] = b"[ALL]\n"

        replaced = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(replaced.reclaim(self._CALL, working_directory=_WORK, timeout=30))
        assert [call.args[:3] for call in fake.matching("exec")][-2:] == [
            ("exec", "--user", "0"),
            ("exec", "-w", "/"),
        ]

    def test_the_ancestors_of_the_replacement_are_read_again(self):
        present, hardening = {_NAME}, [b"[]\n"]
        backend, fake = self._backend(present, hardening)
        asyncio.run(backend.acquire(_KEY, _SPEC))

        present.discard(_NAME)
        fake.mark()
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]


class TestReclaimKeepsAFloorUnderRoot:
    """`maf_sandbox.reclaim_guest_path` holds the policy; this is the subset kept here."""

    def _sandbox(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    @pytest.mark.parametrize("directory", ["/", "/etc", "/maf-sandbox/", "//tmp", "/a/.."])
    def test_a_path_within_two_components_of_the_root_runs_no_command(self, directory):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError, match="close to the root"):
            asyncio.run(sandbox.reclaim(directory, working_directory=_WORK, timeout=30))
        assert fake.matching("exec") == []

    @pytest.mark.parametrize("directory", ["etc/ssh", "a/b", "./x/y", "../../etc/ssh"])
    def test_a_relative_path_runs_no_command(self, directory):
        """The removal runs from `/`, so a relative path resolves against the filesystem root:
        `etc/ssh` would be `rm -rf /etc/ssh`, as root.
        """
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError, match="not absolute"):
            asyncio.run(sandbox.reclaim(directory, working_directory=_WORK, timeout=30))
        assert fake.matching("exec") == []

    def test_a_call_directory_two_components_deep_is_allowed(self):
        """The floor is a floor: what core dispatches has to go through it unchanged."""
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim("/srv/run-a1b2c3", working_directory="/srv", timeout=30))
        assert fake.only("exec").args[-1] == "/srv/run-a1b2c3"


class TestExecDiscardsATimedOutSandbox:
    def test_a_timed_out_exec_removes_the_container(self):
        """The acquire path's identity probe answers so a sandbox comes back at all; the
        sandbox's own exec is what times out and discards the container.
        """

        def responder(args):
            if args[0] == "exec":
                if len(args) > 4 and args[4] == "id":
                    return _DockerResult(0, b"20001\n", "")
                raise TimeoutError
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and args[-1] == _NAME:
                return _DockerResult(0, b"true\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["hang"], working_directory=_WORK, timeout=1))
        assert fake.matching("rm", "-f", _NAME) != []

    def test_a_timeout_walking_the_ancestors_fails_closed_instead(self):
        """The other half of the same rule, and the reason it is not one rule.  The ancestor
        walk is `docker cp` only, so its timeout leaves the container running and there is
        something to hand back; the identity probe's `exec` removes it, so there is not.
        Propagating here would turn a slow daemon into a failed acquire, where the
        conservative answer — removals run as the guest — is already correct and safe.
        """

        def responder(args):
            if args[0] == "cp" and args[1].startswith(f"{_NAME}:/maf-sandbox"):
                raise TimeoutError("a daemon too slow to answer the ancestor walk")
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and args[-1] == _NAME:
                return _DockerResult(0, b"true\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("rm", "-f", _NAME) == []
        assert [f.host_owned_ancestors for f in backend._facts.values()] == [False]

    def test_a_timeout_reading_config_user_falls_back_instead(self, caplog):
        """The third read, and the same rule: `inspect` is host-side, so a timeout there
        killed a CLI process and left the container running.  It takes the documented `0:0`
        fallback with its warning, and never reaches `id` — there is no user to resolve.
        """

        def responder(args):
            if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
                raise TimeoutError("a daemon too slow to answer inspect")
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and args[-1] == _NAME:
                return _DockerResult(0, b"true", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        with caplog.at_level(logging.INFO):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("rm", "-f", _NAME) == []
        assert [(f.guest_uid, f.guest_gid) for f in backend._facts.values()] == [(0, 0)]
        assert fake.matching("exec") == []
        assert any("could not be resolved" in r.message for r in caplog.records)

    def test_a_timeout_while_reading_facts_fails_the_acquire(self):
        """The identity probe's exec removes the container on its way out; swallowing the
        timeout here would hand `acquire` a sandbox for a container that no longer exists,
        with fallback facts cached against it.
        """

        def responder(args):
            if args[:1] == ("exec",):
                # Every exec times out — ancestors_are_the_hosts swallows its failures, but
                # the identity probe must not.
                raise TimeoutError
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and args[-1] == _NAME:
                return _DockerResult(0, b"true\n", "")
            if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
                return _DockerResult(0, b"10001\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        with pytest.raises(TimeoutError):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("rm", "-f", _NAME) != []
        assert not any(key[0] == _NAME for key in backend._facts)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def _sandbox(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_the_copy_targets_the_container_root(self):
        sandbox, fake = self._sandbox()
        asyncio.run(
            sandbox.write_file("/maf-sandbox/work/main.bicep", "x", working_directory=_WORK)
        )
        assert fake.only("cp", "-").args == ("cp", "-", f"{_NAME}:/")

    def test_the_entry_is_the_path_without_its_leading_slash(self):
        sandbox, fake = self._sandbox()
        asyncio.run(
            sandbox.write_file("/maf-sandbox/work/main.bicep", "content", working_directory=_WORK)
        )
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames()[-1] == "maf-sandbox/work/main.bicep"

    def test_str_content_round_trips_as_utf8(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.write_file("/maf-sandbox/work/f", "héllo", working_directory=_WORK))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            member = archive.extractfile("maf-sandbox/work/f")
            assert member is not None
            assert member.read().decode("utf-8") == "héllo"

    def test_bytes_content_is_written_as_given(self):
        sandbox, fake = self._sandbox()
        payload = b"\x89PNG\r\n\x1a\n"
        asyncio.run(
            sandbox.write_file("/maf-sandbox/work/img.png", payload, working_directory=_WORK)
        )
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            member = archive.extractfile("maf-sandbox/work/img.png")
            assert member is not None
            assert member.read() == payload

    def test_a_failed_copy_raises(self):
        overrides = {("cp", "-"): _DockerResult(1, b"", "no space")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(RuntimeError, match="could not write"):
            asyncio.run(sandbox.write_file("/maf-sandbox/work/f", "x", working_directory=_WORK))

    def test_a_refused_path_never_reaches_the_copy_seam(self):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError):
            asyncio.run(sandbox.write_file("../escape", "x", working_directory=_WORK))
        assert fake.matching("cp", "-") == []

    def test_the_entry_carries_the_container_user(self):
        """A non-root image gets tar entries under its own uid, and the call-directory
        parents arrive as explicit guest-owned directory entries: docker creates an implicit
        intermediate as root whatever the file entry says (measured), so the ownership has to
        be spelled entry by entry.  Ancestors above the work directory stay out of the tar.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(f"{_WORK}/call-a1b2c3/note", "x", working_directory=_WORK))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == [
                "maf-sandbox/work/call-a1b2c3",
                "maf-sandbox/work/call-a1b2c3/note",
            ]
            file_entry = archive.getmember("maf-sandbox/work/call-a1b2c3/note")
            assert (file_entry.uid, file_entry.gid) == (10001, 10001)
            call_dir = archive.getmember("maf-sandbox/work/call-a1b2c3")
            assert call_dir.isdir() and (call_dir.uid, call_dir.gid) == (10001, 10001)

    def test_a_write_directly_in_the_work_dir_adds_no_call_directory(self):
        """A file beside the calls, not under one: the tar carries the file alone."""
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(f"{_WORK}/note", "x", working_directory=_WORK))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == ["maf-sandbox/work/note"]

    def test_an_absent_work_dir_on_a_nonroot_image_travels_guest_owned(self):
        """The `d == base` branch of the subtree rule: an image carrying `/maf-sandbox`
        but no `work_dir` gets it as an explicit guest-owned entry — without it, docker
        creates `work_dir` implicitly as root and every call directory under it leaks.
        """
        overrides = {
            _cp("/maf-sandbox"): _DockerResult(
                0, _owned_directory_tar("maf-sandbox", 0, 0o755), ""
            ),
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(f"{_WORK}/call-a1b2c3/note", "x", working_directory=_WORK))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == [
                "maf-sandbox/work",
                "maf-sandbox/work/call-a1b2c3",
                "maf-sandbox/work/call-a1b2c3/note",
            ]
            work_dir = archive.getmember("maf-sandbox/work")
            assert work_dir.isdir() and (work_dir.uid, work_dir.gid) == (10001, 10001)

    def test_a_relative_work_dir_still_stamps_its_directories(self):
        """The other spelling `normpath` leaves alone.  `guest_path_and_ancestors` roots what it
        is handed — it already writes `/workspace` into the walk — so an unrooted
        `working_directory` compared against it matches nothing, and every directory goes
        back to docker to create as root.
        """
        work = "workspace"
        spec = SandboxSpec(kind="e2e", image="img", work_dir=work)
        overrides = {
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:20001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, spec))
        asyncio.run(sandbox.write_file("call-a1/note", "x", working_directory=work))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == [
                "workspace",
                "workspace/call-a1",
                "workspace/call-a1/note",
            ]
            stamped = archive.getmember("workspace/call-a1")
            assert stamped.isdir() and (stamped.uid, stamped.gid) == (10001, 20001)

    def test_a_double_rooted_work_dir_still_stamps_its_directories(self):
        """`posixpath.normpath` keeps exactly two leading slashes, which POSIX permits, while
        the directory chain is rebuilt from segments and is always single-rooted.  Comparing
        the two spellings matches nothing, so the subtree filter drops every directory and
        hands them back to docker to create as root — the leak this rule exists to close.
        """
        work = "//maf-sandbox/work"
        spec = SandboxSpec(kind="e2e", image="img", work_dir=work)
        overrides = {
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:20001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, spec))
        asyncio.run(sandbox.write_file(f"{work}/call-a1/note", "x", working_directory=work))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == [
                "maf-sandbox/work",
                "maf-sandbox/work/call-a1",
                "maf-sandbox/work/call-a1/note",
            ]
            stamped = archive.getmember("maf-sandbox/work/call-a1")
            assert stamped.isdir() and (stamped.uid, stamped.gid) == (10001, 20001)

    def test_a_root_working_directory_keeps_its_components_whole(self):
        """The subtree rule on `working_directory = "/"`: `/` is the cp destination and
        needs no entry, and `tmp` under it is a `working_directory` descendant here, so
        the entries run `tmp`, `tmp/run-1`, file.
        """
        overrides = {
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file("/tmp/run-1/note", "x", working_directory="/"))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == ["tmp", "tmp/run-1", "tmp/run-1/note"]

    def test_a_root_image_keeps_the_default_ownership(self):
        """`Config.User` unset means root: the tar entries stay uid 0, as they always were."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file(f"{_WORK}/call-a1b2c3/note", "x", working_directory=_WORK))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            call_dir = archive.getmember("maf-sandbox/work/call-a1b2c3")
            assert call_dir.isdir() and (call_dir.uid, call_dir.gid) == (0, 0)
            member = archive.getmember("maf-sandbox/work/call-a1b2c3/note")
            assert (member.uid, member.gid) == (0, 0)


# ---------------------------------------------------------------------------
# FILES_OUT — stat and read from the docker cp tar stream
# ---------------------------------------------------------------------------


class TestStatFile:
    def _sandbox_streaming(self, stream: bytes, rc: int = 0, stderr: str = ""):
        overrides = {**_WORK_IS_A_DIRECTORY, ("cp",): _DockerResult(rc, stream, stderr)}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    def test_a_regular_file_is_statted_from_the_first_tar_header(self):
        sandbox, _ = self._sandbox_streaming(_tar_bytes("out.png", b"x" * 40))
        entry = asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))
        assert entry is not None
        assert entry.kind is EntryKind.FILE
        assert entry.size_bytes == 40

    def test_a_symlink_is_reported_as_a_symlink_with_no_size(self):
        sandbox, _ = self._sandbox_streaming(_symlink_tar("link", "/etc/passwd"))
        entry = asyncio.run(sandbox.stat_file("link", working_directory=_WORK))
        assert entry is not None
        assert entry.kind is EntryKind.SYMLINK
        # Not the length of "/etc/passwd": what a stat reports for a link is the target string,
        # and passing it on would answer a size question about a file nobody measured.
        assert entry.size_bytes is None

    def test_a_missing_path_is_none(self):
        sandbox, _ = self._sandbox_streaming(
            b"", rc=1, stderr="Could not find the file /maf-sandbox/work/x"
        )
        assert asyncio.run(sandbox.stat_file("x", working_directory=_WORK)) is None

    def test_the_stat_bounds_the_transfer_to_one_tar_block(self):
        """A stat must not buffer a whole untrusted file: it bounds the cp read to 512 bytes."""
        from maf_sandbox_docker._backend import _TAR_BLOCK

        sandbox, fake = self._sandbox_streaming(_tar_bytes("out.png", b"x" * 100000))
        asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))
        # By path: the parent walk stats `/maf-sandbox` then `/maf-sandbox/work`, and it is the entry's own cp under test.
        cp = fake.only(*_cp(f"{_WORK}/out.png"))
        assert cp.read_limit == _TAR_BLOCK

    def test_the_rel_path_is_correct_even_for_a_non_normalized_working_directory(self):
        """A base like `/maf-sandbox/work/.` must not shift the reported path — normalize before slicing."""
        sandbox, _ = self._sandbox_streaming(_tar_bytes("out.txt", b"x" * 5))
        entry = asyncio.run(sandbox.stat_file("out.txt", working_directory="/maf-sandbox/work/."))
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
        overrides = {**_WORK_IS_A_DIRECTORY, ("cp",): _DockerResult(0, stream, "")}
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

        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("cp",): _DockerResult(0, _tar_bytes("big.bin", b"x" * 100000), ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(sandbox.read_file("big.bin", working_directory=_WORK, max_bytes=64))
        assert fake.only(*_cp(f"{_WORK}/big.bin")).read_limit == _TAR_BLOCK + 64

    def test_a_symlink_is_refused_on_the_header_type(self):
        sandbox = self._sandbox_streaming(_symlink_tar("link", "/etc/passwd"))
        with pytest.raises(OSError, match="not a regular file"):
            asyncio.run(sandbox.read_file("link", working_directory=_WORK, max_bytes=1000))

    def test_a_missing_file_raises_file_not_found(self):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("cp",): _DockerResult(1, b"", "No such file or directory"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(FileNotFoundError):
            asyncio.run(sandbox.read_file("gone", working_directory=_WORK, max_bytes=10))


class TestASymlinkedAncestorOfTheWorkingDirectory:
    """A nested work dir has ancestors above it, and the guest can replace those too.

    `maf-sandbox-bicep` really does use `/maf-sandbox/work`, so this is not a hypothetical shape.
    """

    _HOSTNAME = b"7eebe863ee42\n"
    _NESTED = "/maf-sandbox/etc"

    def test_an_ancestor_link_above_the_working_directory_is_refused(self):
        overrides = {
            _cp("/maf-sandbox"): _DockerResult(0, _symlink_tar("maf-sandbox", "/"), ""),
            _cp(self._NESTED): _DockerResult(0, _directory_tar("etc"), ""),
            _cp(f"{self._NESTED}/hostname"): _DockerResult(
                0, _tar_bytes("hostname", self._HOSTNAME), ""
            ),
            ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(sandbox.read_file("hostname", working_directory=self._NESTED, max_bytes=99))
        # Stopped at the ancestor: the entry itself was never fetched.
        assert fake.cp_since_mark() == [(*_cp("/maf-sandbox"), "-")]


class TestASymlinkedParentEscapesLexicalConfinement:
    """``ln -sfn /etc /maf-sandbox/work/out``: the entry reads as a regular file, the parent link does not.

    The premise test below pins that the engine really does answer through the link, so the
    refusal tests are not passing against a fake that simply cannot reach outside.
    """

    _HOSTNAME = b"53769ddf53e3\n"

    def _sandbox(self):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            _cp(f"{_WORK}/out"): _DockerResult(0, _symlink_tar("out", "/etc"), ""),
            _cp(f"{_WORK}/out/hostname"): _DockerResult(
                0, _tar_bytes("hostname", self._HOSTNAME), ""
            ),
            _cp(f"{_WORK}/real.txt"): _DockerResult(0, _tar_bytes("real.txt", b"artifact"), ""),
            _cp(f"{_WORK}/pipe"): _DockerResult(0, _fifo_tar("pipe"), ""),
            ("cp",): _DockerResult(1, b"", "Error: Could not find the file in container"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        return sandbox, fake

    def test_the_engine_answers_from_outside_the_working_directory(self):
        """The premise of the refusals below: the path through the link resolves daemon-side.

        Asked through the unconfined stat the walk itself uses, because the public one now
        refuses exactly this — and without the premise a refusal would also pass against a fake
        engine that could not reach outside in the first place.
        """
        sandbox, _ = self._sandbox()
        through = asyncio.run(sandbox._stat_guest(f"{_WORK}/out/hostname", "out/hostname"))
        assert through is not None
        assert through.kind is EntryKind.FILE
        assert through.size_bytes == len(self._HOSTNAME)

    def test_a_final_component_link_is_described_rather_than_refused(self):
        """Only the parents are refused: reporting a link as `SYMLINK` is how a caller learns."""
        sandbox, _ = self._sandbox()
        link = asyncio.run(sandbox.stat_file("out", working_directory=_WORK))
        assert link is not None
        assert link.kind is EntryKind.SYMLINK

    def test_a_bare_stat_through_a_symlinked_parent_is_refused(self):
        """No bytes escape, but a type and a size do — metadata from outside the boundary."""
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(sandbox.stat_file("out/hostname", working_directory=_WORK))
        assert fake.cp_since_mark() == [
            (*_cp("/maf-sandbox"), "-"),
            (*_cp(_WORK), "-"),
            (*_cp(f"{_WORK}/out"), "-"),
        ]

    def test_a_read_through_a_symlinked_parent_is_refused_before_the_read(self):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(sandbox.read_file("out/hostname", working_directory=_WORK, max_bytes=1000))
        assert fake.cp_since_mark() == [
            (*_cp("/maf-sandbox"), "-"),
            (*_cp(_WORK), "-"),
            (*_cp(f"{_WORK}/out"), "-"),
        ]

    def test_a_symlinked_working_directory_is_refused_too(self):
        """``ln -sfn /etc /maf-sandbox/work`` is the same escape one level up, so the walk starts at the base."""
        overrides = {
            _cp("/maf-sandbox"): _DockerResult(0, _directory_tar("maf-sandbox"), ""),
            _cp(_WORK): _DockerResult(0, _symlink_tar("work", "/etc"), ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(sandbox.read_file("hostname", working_directory=_WORK, max_bytes=1000))
        assert fake.cp_since_mark() == [
            (*_cp("/maf-sandbox"), "-"),
            (*_cp(_WORK), "-"),
        ]

    def test_a_path_through_a_regular_file_is_not_reported_as_an_escape(self):
        """``ENOTDIR`` is not a confinement failure, and only a link makes it one."""
        sandbox, _ = self._sandbox()
        with pytest.raises(NotADirectoryError):
            asyncio.run(
                sandbox.read_file("real.txt/child", working_directory=_WORK, max_bytes=1000)
            )

    def test_a_path_through_a_fifo_is_not_reported_as_an_escape_either(self):
        """`OTHER` covers a FIFO and a device node too, and a path through one is `ENOTDIR`."""
        sandbox, _ = self._sandbox()
        with pytest.raises(NotADirectoryError):
            asyncio.run(sandbox.read_file("pipe/child", working_directory=_WORK, max_bytes=1000))

    def test_a_fifo_still_stats_as_other(self):
        """`OTHER` keeps what is left after the link split: a fifo, a socket, a device node."""
        sandbox, _ = self._sandbox()
        entry = asyncio.run(sandbox.stat_file("pipe", working_directory=_WORK))
        assert entry is not None
        assert entry.kind is EntryKind.OTHER

    def test_a_missing_component_leaves_the_refusal_to_the_read(self):
        """A walk that finds nothing must not turn a missing output into a confinement failure."""
        sandbox, _ = self._sandbox()
        with pytest.raises(FileNotFoundError):
            asyncio.run(sandbox.read_file("gone/output", working_directory=_WORK, max_bytes=1000))


class TestListDirIsRefused:
    def test_list_dir_raises_not_implemented(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(NotImplementedError, match="FILES_LIST"):
            asyncio.run(sandbox.list_dir(".", working_directory=_WORK))


def _passwd_responder(
    running: list[str], overrides: dict[tuple[str, ...], _DockerResult], passwd: bytes
):
    """A responder that answers the `/etc/passwd` pull with a one-entry tar carrying
    ``passwd``, and everything else from ``running``/``overrides`` via the machine.
    """

    def respond(args):
        if args[0] == "cp" and args[1].endswith(":/etc/passwd"):
            return _tar_response(passwd)
        machine = _machine(running=running, overrides=overrides)
        return machine(args)

    return respond


def _tar_response(body: bytes) -> _DockerResult:
    """A `docker cp` stdout shaped as a one-entry tar carrying ``body``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("entry")
        entry.size = len(body)
        entry.mode = 0o644
        archive.addfile(entry, io.BytesIO(body))
    return _DockerResult(0, buffer.getvalue(), "")


class TestTheGuestIdentityIsReadFromTheContainer:
    """`Config.User` says who runs the container's default command; `write_file`'s tar entries
    have to answer to the same principal, since a reused container can predate a config change.
    """

    def _facts(self, user: bytes, name: str = _NAME):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, user, ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(name, _SPEC))
        return facts, fake

    def test_an_unset_user_reads_as_root(self):
        facts, _ = self._facts(b"\n")
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)

    def test_a_bare_zero_is_resolved_like_any_other_bare_uid(self):
        """`USER 0` with no passwd entry runs as root with root's gid, but an image whose
        passwd entry gives uid 0 another primary group must not be short-circuited to
        `0:0` — the gid is asked for the same way it is for any bare uid.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"0\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"0\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"20001\n", ""),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (0, 20001)

    def test_a_bare_uid_keeps_gid_0_when_neither_passwd_nor_id_answers(self):
        """The gid-0 fallback: with `/etc/passwd` unreadable and an `id` that answers
        nothing, a bare uid's gid stays 0 — what the runtime picks for a uid with no
        passwd entry.
        """
        facts, fake = self._facts(b"10001\n")
        assert (facts.guest_uid, facts.guest_gid) == (10001, 0)
        # `Config.User` already gave the uid, so only the open half is asked for.
        assert [c.args for c in fake.matching("exec")] == [
            ("exec", "-w", "/", _NAME, "id", "-g"),
        ]

    def test_a_bare_uid_falls_back_to_id_when_passwd_is_unreadable(self):
        """The `id` fallback when the guest answers: with `/etc/passwd` unreadable, a bare
        uid's primary gid is asked from the guest — and `id` resolving both sides is what
        supplies the expected pair, since the fake carries no passwd tar for this test.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"20001\n", ""),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)

    def test_a_known_gid_is_never_asked_for_even_if_id_would_hang(self):
        """`app:20001` leaves only the uid open, so `id -g` is never run — and an image
        whose `id -g` hangs must not cost the acquire a gid `Config.User` already named.
        A timed-out `exec` removes the container, so the wasted call is not merely wasted.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app:20001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))

        def respond(args):
            if args[:6] == ("exec", "-w", "/", _NAME, "id", "-g"):
                raise TimeoutError("an image whose `id -g` hangs")
            return _machine(running=[_NAME], overrides=overrides)(args)

        fake._responder = respond
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert [c.args for c in fake.matching("exec")] == [
            ("exec", "-w", "/", _NAME, "id", "-u"),
        ]

    def test_a_half_that_answers_is_kept_when_the_other_refuses(self):
        """Each half of `id` stands alone: a guest that answers `id -u` and refuses `id -g`
        resolves the uid, and only the gid falls to the 0 remainder.  Discarding both would
        throw away an answer the guest gave.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"ghost\n", ""),
            ("cp", f"{_NAME}:/etc/passwd"): _DockerResult(1, b"", "no such file"),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(1, b"", "id: cannot"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 0)

    def test_a_passwd_read_that_reached_the_byte_cap_is_still_used(self):
        """A bounded read kills `docker cp` once the cap is reached, so a complete passwd can
        arrive alongside a nonzero code — the stream was longer than the cap, not broken.
        Rejecting it would drop a resolvable user to `id`, or to root when the image has none.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        killed = _tar_response(passwd)

        def respond(args):
            if args[0] == "cp" and args[1].endswith(":/etc/passwd"):
                # What the cap looks like: SIGKILL's code, with the whole entry buffered.
                return _DockerResult(-9, killed.stdout, "")
            return _machine(running=[_NAME], overrides=overrides)(args)

        fake._responder = respond
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert fake.matching("exec") == []

    def test_a_group_read_that_reached_the_byte_cap_is_still_used(self):
        """The same on `/etc/group`: a capped read must not turn a resolvable named group into
        the gid-0 remainder.
        """
        group = b"root:x:0:\ndevs:x:30001:\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:devs\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        killed = _tar_response(group)

        def respond(args):
            if args[0] == "cp" and args[1].endswith(":/etc/group"):
                return _DockerResult(-9, killed.stdout, "")
            return _machine(running=[_NAME], overrides=overrides)(args)

        fake._responder = respond
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 30001)
        assert fake.matching("exec") == []

    def test_an_empty_user_half_is_dockers_shorthand_for_root(self):
        """`USER :20001` runs as `0:20001` — measured against a real engine, which resolves
        the empty half to root itself.  Reading it as unknown cost the gid the field had
        already stated: with no `id` to answer the uid, the pair fell back to `0:0`.
        """
        facts, fake = self._facts(b":20001\n")
        assert (facts.guest_uid, facts.guest_gid) == (0, 20001)
        # Both halves are known from `Config.User` alone, so the guest is not asked at all.
        assert fake.matching("exec") == []

    def test_a_bare_colon_is_root_with_the_group_its_passwd_entry_names(self):
        """`USER :` runs as `0:0` (measured).  The uid half is root by the same rule, and
        the gid then comes from root's own passwd entry rather than from the guest.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b":\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        fake._responder = _passwd_responder([_NAME], overrides, passwd)
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert fake.matching("exec") == []

    def test_an_empty_group_half_takes_the_gid_the_passwd_entry_names(self):
        """`USER 10001:` runs as `10001:20001` (measured): docker resolves the empty group
        half from the passwd entry, and so does this.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        fake._responder = _passwd_responder([_NAME], overrides, passwd)
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert fake.matching("exec") == []

    def test_a_uid_gid_pair_is_split(self):
        facts, _ = self._facts(b"10001:20001\n")
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)

    def test_a_named_user_is_resolved_from_the_passwd_file(self):
        """A name in `Config.User` is resolved against the container's `/etc/passwd`, read
        host-side over the pull surface — no guest utility needed, so an image without
        `id` (or without a shell to reach it through) still resolves.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        fake._responder = _passwd_responder([_NAME], overrides, passwd)
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert fake.matching("exec") == []

    def test_a_named_user_falls_back_to_id_when_passwd_is_unreadable(self):
        """A passwd file that will not come over the wire leaves `id` as the resolver."""
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app\n", ""),
            ("cp", f"{_NAME}:/etc/passwd"): _DockerResult(1, b"", "no such file"),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"20001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)

    def test_a_bare_uid_takes_its_gid_from_the_passwd_entry(self):
        """A bare uid's primary gid is the one its `/etc/passwd` entry names."""
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        fake._responder = _passwd_responder([_NAME], overrides, passwd)
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert fake.matching("exec") == []

    @pytest.mark.parametrize(
        ("user", "expected"),
        [
            ("app:20001", (10001, 20001)),
            ("10001:devs", (10001, 30001)),
            ("app:devs", (10001, 30001)),
        ],
    )
    def test_a_mixed_user_group_pair_resolves_each_side(self, user, expected):
        """`Config.User` accepts `user:group` with either side numeric or named; each half
        resolves from its own account file.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\napp:x:10001:20001::/home/app:/bin/sh\n"
        group = b"root:x:0:\ndevs:x:30001:\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, user.encode(), ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))

        def respond(args):
            if args[0] == "cp" and args[1].endswith(":/etc/passwd"):
                return _tar_response(passwd)
            if args[0] == "cp" and args[1].endswith(":/etc/group"):
                return _tar_response(group)
            return _machine(running=[_NAME], overrides=overrides)(args)

        fake._responder = respond
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == expected
        # Both account files answer this pair, so `id` — the one step that runs a guest
        # command — is never reached.
        assert fake.matching("exec") == []

    def test_a_named_group_is_read_before_the_guest_is_asked(self):
        """`/etc/group` resolves the named half, so a bare uid beside it never pulls passwd
        (which could not answer it) and never runs `id`: an image whose `id` hangs would
        otherwise cost the acquire a gid `/etc/group` already carries.
        """
        group = b"root:x:0:\ndevs:x:30001:\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:devs\n", ""),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))

        def respond(args):
            if args[0] == "cp" and args[1].endswith(":/etc/group"):
                return _tar_response(group)
            if args[0] == "exec":
                raise TimeoutError("an image whose `id` hangs")
            return _machine(running=[_NAME], overrides=overrides)(args)

        fake._responder = respond
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 30001)
        assert fake.matching("exec") == []
        # `_passwd_entry` swallows every exception, so the pull is asserted on the record
        # rather than through the responder.
        assert fake.matching("cp", f"{_NAME}:/etc/passwd") == []

    def test_an_unresolvable_identity_fails_open_to_root(self, caplog):
        """A named user with neither a passwd answer nor `id` leaves root-owned entries, and
        the reach rule decides the removals.  Guessing an arbitrary ownership could stamp a
        stranger's identity on the files.
        """
        passwd = b"root:x:0:0:root:/root:/bin/bash\n"
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"ghost\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(1, b"", "id: not found"),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(1, b"", "id: not found"),
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        fake._responder = _passwd_responder([_NAME], overrides, passwd)
        with caplog.at_level(logging.INFO):
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert any("could not be resolved" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_an_unreadable_user_fails_open_to_root_and_says_so(self, caplog):
        """An image this backend cannot ask keeps today's behaviour: root-owned entries — and
        is warned about, because a daemon that would not answer and an image that names no
        user reach the same `0:0` from opposite states, and only one of them is a choice.
        """
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(1, b"", "daemon error"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        with caplog.at_level(logging.INFO):
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert any("could not be resolved" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_an_unset_user_is_root_without_a_warning(self, caplog):
        """The other side of the same coin: `Config.User` empty *is* the answer, so it must
        not warn — a notice on every stock image would make the real one unreadable.
        """
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=_WORK_IS_A_DIRECTORY))
        with caplog.at_level(logging.INFO):
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert [r.message for r in caplog.records if "could not be resolved" in r.message] == []

    def test_the_answer_is_read_once_per_container(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert [
            c.args
            for c in fake.calls[fake._marked :]
            if c.args[:3] == ("inspect", "-f", "{{.Config.User}}")
        ] == []


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

    def test_a_removal_that_lands_reports_nothing(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert asyncio.run(backend.dispose(_KEY)) is None

    def test_a_failed_removal_comes_back_as_the_reason(self):
        """Never raising is the contract, so the reason is the only way the router hears (#641)."""
        overrides = {("rm",): _DockerResult(1, b"", "daemon error")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        reason = asyncio.run(backend.dispose(_KEY))
        assert reason is not None
        assert reason.code == "refused", "the engine answered and the container stayed"
        assert "daemon error" in reason.detail
        assert _NAME in reason.detail

    def test_a_second_attempt_still_reports_what_the_first_could_not_remove(self):
        """A name a removal could not take away outlives the registry entry it came from."""
        overrides = {
            ("rm",): _DockerResult(1, b"", "daemon error"),
            ("ps",): _DockerResult(1, b"", "daemon down"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))

        assert asyncio.run(backend.dispose(_KEY)) is not None
        second = asyncio.run(backend.dispose(_KEY))
        assert second is not None
        assert _NAME in second.detail

    def test_a_sweep_cancelled_part_way_still_leaves_the_name_to_retry(self):
        """The record is written before the first await, so a bound that expires mid-sweep does
        not take the only name of the container with it."""
        backend, _ = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        inner = backend._docker  # noqa: SLF001

        async def hangs_on_rm(*args: str, **kwargs: object) -> _DockerResult:
            if args[:1] == ("rm",):
                await asyncio.Event().wait()
            return await inner(*args, **kwargs)  # type: ignore[arg-type]

        backend._docker = hangs_on_rm  # type: ignore[method-assign]  # noqa: SLF001

        async def cut_short() -> None:
            async with asyncio.timeout(0.05):
                await backend.dispose(_KEY)

        with pytest.raises(TimeoutError):
            asyncio.run(cut_short())

        assert backend._registry == {}, "the registry entry is gone"  # noqa: SLF001
        assert backend._undeleted == {  # noqa: SLF001
            (_KEY.scope, _KEY.thread_id, _KEY.agent_dir): {_NAME}
        }

    def test_a_removal_that_lands_clears_the_retry_record(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        backend._undeleted[(_KEY.scope, _KEY.thread_id, _KEY.agent_dir)] = {_NAME}  # noqa: SLF001

        assert asyncio.run(backend.dispose(_KEY)) is None
        assert backend._undeleted == {}  # noqa: SLF001

    def test_a_container_docker_does_not_have_is_not_a_failure(self):
        overrides = {("rm",): _DockerResult(1, b"", f"Error: No such container: {_NAME}")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert asyncio.run(backend.dispose(_KEY)) is None

    def test_a_record_this_sweep_never_reported_on_is_not_read_as_landed(self):
        """A disposal still in flight writes its names ahead of its own first await. Answering
        `None` here clears the router's refusal on the strength of a delete nobody confirmed."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)

        async def slow_listing(*args: str, **kwargs: object) -> _DockerResult:
            if args[:1] == ("ps",):
                listing.set()
                await release.wait()
            return _DockerResult(0, b"", "")

        backend, _ = _backend_with(_machine())
        backend._docker = slow_listing  # type: ignore[method-assign]  # noqa: SLF001

        async def drive() -> DisposalFailure | None:
            disposal = asyncio.create_task(backend.dispose(_KEY))
            await listing.wait()
            backend._undeleted[prefix] = {"c-2"}  # a later disposal's own  # noqa: SLF001
            release.set()
            return await disposal

        reported = asyncio.run(drive())
        assert backend._undeleted == {prefix: {"c-2"}}, "the newer record survives"  # noqa: SLF001
        assert reported is not None, "and the key stays refused until someone reports on it"
        assert reported.code == "unknown", "the other attempt's outcome is not ours to name"

    def test_a_purge_does_not_subtract_a_record_written_beside_it(self):
        """A scope purge takes nothing away from the retry record: the container it removed
        and one recorded beside it carry the same name, so subtracting one drops the other."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)

        async def slow_listing(*args: str, **kwargs: object) -> _DockerResult:
            if args[:1] == ("ps",):
                listing.set()
                await release.wait()
            return _DockerResult(0, b"", "")

        backend, _ = _backend_with(_machine())
        backend._docker = slow_listing  # type: ignore[method-assign]  # noqa: SLF001
        backend._undeleted[prefix] = {_NAME}  # noqa: SLF001

        async def drive() -> None:
            purge = asyncio.create_task(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
            await listing.wait()
            backend._undeleted[prefix] = {_NAME}  # the newer generation  # noqa: SLF001
            release.set()
            assert (await purge).undisposed is None, "the purge itself has to land"

        asyncio.run(drive())
        assert backend._undeleted == {prefix: {_NAME}}, "the newer record was subtracted"  # noqa: SLF001

    def test_a_container_a_failed_removal_left_behind_is_still_served_here(self):
        """Pins what the retry record does rather than what its name suggests: it is disposal
        bookkeeping, and `acquire` still reuses the container, because the name comes from the
        key and the engine is what gets asked. Refusing to serve is the router's ledger."""
        overrides = {("rm",): _DockerResult(1, b"", "daemon error")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert asyncio.run(backend.dispose(_KEY)) is not None

        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)
        assert backend._undeleted[prefix] == {_NAME}, "the name is owed a retry"  # noqa: SLF001
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") == [], "and the same container is handed back, not replaced"


class TestDisposeScope:
    def test_a_dispose_landing_mid_purge_neither_crashes_nor_is_clobbered(self):
        """Teardown for one key is not serialized, so the purge reconciles against the live
        record: it must not index a prefix a `dispose` removed, nor drop a name it added."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)

        async def slow_listing(*args: str, **kwargs: object) -> _DockerResult:
            if args[:1] == ("ps",):
                listing.set()
                await release.wait()
            return _DockerResult(0, b"", "")

        backend, _ = _backend_with(_machine())
        backend._docker = slow_listing  # type: ignore[method-assign]  # noqa: SLF001
        backend._undeleted[prefix] = {"c-1"}  # noqa: SLF001

        async def drive() -> None:
            purge = asyncio.create_task(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
            await listing.wait()
            backend._undeleted.pop(prefix, None)  # noqa: SLF001
            backend._undeleted[prefix] = {"c-2"}  # a later disposal's own  # noqa: SLF001
            release.set()
            await purge

        asyncio.run(drive())
        assert backend._undeleted == {prefix: {"c-2"}}, "the newer record survives"  # noqa: SLF001

    def test_selects_on_labels_and_returns_the_count(self):
        listed = [_NAME]
        overrides = {("ps",): _DockerResult(0, "".join(f"{n}\n" for n in listed).encode(), "")}
        backend, fake = _backend_with(_machine(overrides=overrides))
        count = asyncio.run(backend.dispose_scope("scope-a", "thread-1")).disposed
        assert count == 1
        ps = fake.matching("ps", "-a")[0]
        assert any("label=maf-sandbox.scope=scope-a" in a for a in ps.args)
        assert any("label=maf-sandbox.thread=thread-1" in a for a in ps.args)

    def test_nothing_to_purge_is_zero_not_an_error(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("s", "t")).disposed == 0

    def test_a_failing_listing_degrades_to_zero(self):
        overrides = {("ps",): _DockerResult(1, b"", "daemon down")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        assert asyncio.run(backend.dispose_scope("s", "t")).disposed == 0

    def test_a_failing_listing_says_the_sweep_may_be_partial(self):
        """ "found none" and "could not look" are one empty list, and only one of them means
        the purge covered the containers another replica created."""
        overrides = {("ps",): _DockerResult(1, b"", "daemon down")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        purge = asyncio.run(backend.dispose_scope("s", "t"))
        assert purge.undisposed is not None
        assert purge.undisposed.code == "unlisted"
        assert "partial" in purge.undisposed.detail

    def test_a_listing_that_worked_says_nothing(self):
        backend, _ = _backend_with(_machine())
        assert asyncio.run(backend.dispose_scope("s", "t")).undisposed is None


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
    egress=Egress.ALLOWLIST,
    egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"),
)
_ALLOW_ID = "allow:" + ",".join(sorted(_ALLOW_SPEC.egress_allow))
#: What `os.environ.get("MAF_EGRESS_PROXY_IMAGE", "")` hands the constructor when nothing is set.
_EMPTY_PROXY_CONFIG = DockerSandboxConfig(egress_proxy_image="")
_AL = _container_name(_KEY, _ALLOW_SPEC.kind, _ALLOW_ID)
_AL_NET = _network_name(_AL)
_AL_PROXY = _proxy_name(_AL)


def _run_named(fake: _FakeDocker, name: str) -> _Recorded:
    found = [c for c in fake.matching("run") if c.args[c.args.index("--name") + 1] == name]
    assert len(found) == 1, [c.args for c in fake.calls]
    return found[0]


class TestAllowlistTopology:
    def test_the_declaration_follows_the_configuration(self):
        assert _backend_with()[0].declarations.egress_modes == frozenset({Egress.CLOSED})
        assert _backend_with(config=_ALLOW_CONFIG)[0].declarations.egress_modes == frozenset(
            {Egress.ALLOWLIST, Egress.CLOSED}
        )

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

    def test_the_networks_bridge_is_given_no_host_address(self):
        """Both families, not just IPv4: a daemon with IPv6 enabled would keep the v6 half
        addressed, and a bridge address is a route to the host the allowlist does not cover."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        args = fake.only("network", "create").args
        assert [args[i + 1] for i, a in enumerate(args) if a == "--opt"] == [
            "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            "com.docker.network.bridge.gateway_mode_ipv6=isolated",
        ]

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

    def test_an_allowlist_naming_no_hosts_builds_no_network_either(self):
        """`ALLOWLIST` with nothing on the list reaches what `CLOSED` reaches, so it takes the
        same branch — which is why the engine floor this backend documents binds only a sandbox
        that names hosts. A spec asking for the mode but no host never reaches the option."""
        spec = SandboxSpec(kind="bicep", image="bicep-sandbox:local", egress=Egress.ALLOWLIST)
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, spec))
        assert fake.matching("network", "create") == []
        run = fake.only("run")
        assert run.args[run.args.index("--network") + 1] == "none"


class TestAnEmptyProxyImageIsNoProxyConfigured:
    """`""` is what an unset environment variable becomes, and it used to split the two reads.

    The declaration was truthiness and the behaviour was `is None`, so this one value declared
    `CLOSED` and then ran `docker run -d --name … ""` anyway, which the engine rejects as an
    invalid reference — a hard failure at every acquire of a spec that allows anything, naming
    the proxy rather than the configuration (#407).

    The two halves are asserted together on purpose. Either alone stays green while the bug is
    present: the declaration was already `CLOSED`, and a closed spec already got `--network
    none`. What broke was the pair — the backend doing what it declared, for a spec that asked
    for hosts it had said it would not open.
    """

    def test_the_declaration_is_closed(self):
        assert _backend_with(config=_EMPTY_PROXY_CONFIG)[0].declarations.egress_modes == frozenset(
            {Egress.CLOSED}
        )

    def test_a_spec_with_an_allowlist_is_closed_rather_than_failing(self):
        backend, fake = _backend_with(_machine(), config=_EMPTY_PROXY_CONFIG)

        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))  # used to raise here

        assert fake.matching("network", "create") == []
        run = fake.only("run")  # the workload, and nothing that could be a proxy
        assert run.args[run.args.index("--network") + 1] == "none"
        # The historical name, not an `allow:`-qualified one: no allowlist is being kept, so a
        # sandbox created before this configuration existed is the same sandbox.
        assert sandbox.container_name == _NAME


class TestAllowlistReuse:
    def test_an_existing_network_is_adopted_not_treated_as_an_error(self):
        """`network create` on a second acquire returns 'already exists'; adopting it is how
        warm reuse of an allowlisted sandbox works, so it must not raise."""
        overrides = {
            ("network", "create"): _DockerResult(1, b"", "network with name X already exists")
        }
        backend, fake = _backend_with(
            _machine(running=[_AL], overrides=overrides, networks={_AL_NET: _UNADDRESSED}),
            config=_ALLOW_CONFIG,
        )
        # Does not raise: the existing network is adopted, the running workload reused.
        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert sandbox.container_name == _AL
        assert fake.matching("rm", "-f", _AL) == []

    def test_an_error_naming_something_else_is_not_read_as_an_absent_network(self):
        """Adoption needs a diagnostic that names this network, not the phrase on its own.

        Unrelated failures borrow the words — a missing context answers `context not found`,
        an unknown driver `plugin "…" not found` — and absence is the one verdict that is
        safe."""
        plugin_error = 'Error response from daemon: plugin "br0" not found'
        overrides = {
            ("network", "create"): _DockerResult(1, b"", "network with name X already exists"),
            ("network", "inspect"): _DockerResult(1, b"", plugin_error),
        }
        backend, fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="could not be read") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert plugin_error in str(raised.value)
        assert fake.matching("run", "-d", "--name", _AL) == []

    def _machine_losing_the_name_race(self, on: Sequence[str]):
        """A responder where the container appears only once the create has tried and lost.

        That interleaving is the whole of the adoption path: a container present any earlier
        is found by the reuse reads and never reaches it, which is why a static fixture tests
        nothing here however it is attached.
        """
        base = _machine(networks={_AL_NET: _UNADDRESSED})
        appeared = False

        def racing(args: tuple[str, ...]) -> _DockerResult:
            nonlocal appeared
            if args[:4] == ("run", "-d", "--name", _AL):
                appeared = True
                return _DockerResult(1, b"", "Conflict. The name is already in use")
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                return _DockerResult(0, (" ".join(on) + " ").encode(), "")
            if args[:4] == ("network", "inspect", "-f", _NETWORK_ENDPOINTS_FORMAT):
                # The racer's container is this responder's invention, so the network's own
                # endpoint list has to know about it too.
                held = {_AL_PROXY} | ({_AL} if appeared else set())
                return _DockerResult(0, (" ".join(sorted(held)) + " ").encode(), "")
            if args[0] == "inspect" and args[-1] == _AL:
                if not appeared:
                    return _DockerResult(1, b"", f"error: no such object: {_AL}")
                state = b"true\n" if "Running" in args[2] else b"running\n"
                return _DockerResult(0, state, "")
            return base(args)

        return racing

    def test_a_name_conflict_is_not_adopted_onto_someone_elses_network(self):
        """Adoption recovers a name the create lost to a racing process, and a name is all it
        proves: whoever won chose that container's topology. An allowlisted acquire that took
        it would return a workload on a network it never built and cannot describe."""
        backend, _ = _backend_with(
            self._machine_losing_the_name_race(["an-unrestricted-network"]), _ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError, match="could not create container"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

    def test_a_stopped_name_conflict_is_never_started_to_be_inspected(self):
        """Starting one to find out is a side effect no verdict can take back: a container
        under this name that this backend did not create runs its entrypoint the moment it is
        restarted. A stopped race is left to the next acquire, which discards before deciding.
        """
        base = _machine(networks={_AL_NET: _UNADDRESSED})
        appeared = False

        def racing(args: tuple[str, ...]) -> _DockerResult:
            nonlocal appeared
            if args[:4] == ("run", "-d", "--name", _AL):
                appeared = True
                return _DockerResult(1, b"", "Conflict. The name is already in use")
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                return _DockerResult(0, b"an-unrestricted-network ", "")
            if args[0] == "inspect" and args[-1] == _AL:
                if not appeared:
                    return _DockerResult(1, b"", f"error: no such object: {_AL}")
                # There by the time the create loses the name, and stopped.
                return _DockerResult(0, b"false\n" if "Running" in args[2] else b"exited\n", "")
            return base(args)

        backend, fake = _backend_with(racing, config=_ALLOW_CONFIG)
        with contextlib.suppress(RuntimeError):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("start", _AL) == []

    def test_a_refused_stopped_conflict_is_not_left_for_the_next_acquire_to_start(self):
        """Refusing is not enough across two acquires. A stopped conflict left under the name
        looks like a warm sandbox to the next one, which takes the ordinary restart branch and
        runs the entrypoint the refusal existed to avoid — so it is removed when refused.
        """
        base = _machine(networks={_AL_NET: _UNADDRESSED})
        raced = False
        theirs = False

        def racing(args: tuple[str, ...]) -> _DockerResult:
            nonlocal raced, theirs
            if args[:4] == ("run", "-d", "--name", _AL) and not raced:
                # The racer's container arrives exactly as ours is refused the name, once.
                raced = theirs = True
                return _DockerResult(1, b"", "Conflict. The name is already in use")
            if args[:2] == ("rm", "-f") and args[-1] == _AL:
                theirs = False
            if theirs and args[0] == "inspect" and args[-1] == _AL:
                if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT):
                    return _DockerResult(0, (_AL_NET + " ").encode(), "")
                # Stopped, and on the right network: the shape `_adopt` must not start and the
                # next acquire must not find still sitting there.
                return _DockerResult(0, b"false\n" if "Running" in args[2] else b"exited\n", "")
            return base(args)

        backend, fake = _backend_with(racing, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="could not create container"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []

        # The second acquire: with the conflict gone the name is free, and nothing is started.
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("start", _AL) == []

    def test_a_name_conflict_carrying_a_second_network_is_not_adopted(self):
        """The winner of the race may have built the container on this network *and* another;
        having the expected endpoint says nothing about the ones beside it."""
        backend, _ = _backend_with(
            self._machine_losing_the_name_race([_AL_NET, "an-unrestricted-network"]),
            _ALLOW_CONFIG,
        )
        with pytest.raises(RuntimeError, match="could not create container"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

    def test_a_name_conflict_is_adopted_when_the_container_is_on_the_right_network(self):
        """The recovery still works for the case it exists for — a racing acquire of this same
        backend, which builds the container on exactly this network."""
        backend, _ = _backend_with(self._machine_losing_the_name_race([_AL_NET]), _ALLOW_CONFIG)
        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert sandbox.container_name == _AL

    def test_a_network_reported_taken_then_gone_fails_rather_than_adopting_nothing(self):
        """ "Already exists" and then "no such network" is not an adoption: nothing established
        what a workload there would reach. Returning would leave `_ensure_proxy` to fail on a
        network nobody built, reporting a proxy problem for a network race."""
        overrides = {
            ("network", "create"): _DockerResult(1, b"", "network with name X already exists")
        }
        backend, fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="was gone when it was read"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("run", "-d", "--name", _AL_PROXY) == []

    def test_a_network_that_appeared_since_the_check_is_not_adopted_on_its_name(self):
        """`create` compares nothing but the name, and the lock is local to one backend and
        loop, so "already exists" can be a network something else put there after the acquire
        looked. Adopting it on the name alone is the whole hole reopened.

        The responder answers the acquire's own look with "not found" and every later one with
        an addressed bridge, which is that interleaving and no other.
        """
        base = _machine(
            overrides={
                ("network", "create"): _DockerResult(1, b"", "network with name X already exists")
            }
        )
        looks = itertools.count()

        def racing(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET:
                if next(looks) == 0:
                    return _DockerResult(1, b"", f"network {_AL_NET} not found")
                return _DockerResult(0, b"\n", "")
            return base(args)

        backend, fake = _backend_with(racing, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="already exists and its bridge holds"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("run", "-d", "--name", _AL) == []


class TestASandboxLeftOnAnUnusableNetwork:
    """A network whose bridge holds a host address is replaced, and the sandbox goes with it.

    `network create` compares nothing but the name, so an existing network is adopted whatever
    its options — which makes the check a separate read. The workload cannot be kept across
    the replacement: it holds an endpoint on the network, so the network will not remove while
    it is attached, and reconnecting the container elsewhere would leave it addressing a proxy
    that no longer resolves.
    """

    def _machine_with_an_addressed_bridge(self):
        return _machine(running=[_AL], networks={_AL_NET: ""})

    def test_the_sandbox_its_proxy_and_the_network_are_all_removed(self):
        backend, fake = _backend_with(self._machine_with_an_addressed_bridge(), _ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("rm", "-f", _AL_PROXY) != []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_the_workload_is_rebuilt_rather_than_reused(self):
        """The removals are only half of it: what the caller gets back has to be a container
        built on the replacement network, not the warm one the reuse branch would have found."""
        backend, fake = _backend_with(self._machine_with_an_addressed_bridge(), _ALLOW_CONFIG)
        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert sandbox.container_name == _AL
        created = _run_named(fake, _AL)
        assert created.args[created.args.index("--network") + 1] == _AL_NET
        assert fake.calls.index(fake.only("network", "rm", _AL_NET)) < fake.calls.index(created)

    def test_the_removal_precedes_the_read_that_would_have_reused_it(self):
        """Ordering is the whole of it: a discard after that read reuses a container it has
        already decided to keep, and the replacement never happens."""
        backend, fake = _backend_with(self._machine_with_an_addressed_bridge(), _ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        removed = fake.calls.index(fake.only("network", "rm", _AL_NET))
        read = fake.calls.index(fake.matching("inspect", "-f", "{{.State.Running}}", _AL)[0])
        assert removed < read

    def test_an_unreadable_network_is_replaced_rather_than_trusted(self):
        """The read decides whether a warm sandbox is kept, so an answer that is neither
        "unaddressed" nor "no such network" cannot be taken as good news — the sandbox goes,
        and an acquire that still cannot prove the bridge is unaddressed refuses rather than
        serving one it has no answer for.

        It refuses for the reason it actually has, though: nothing read a mode here, so the
        failure names the daemon's own answer instead of claiming an address it never saw.
        """
        overrides = {("network", "inspect"): _DockerResult(1, b"", "daemon is not responding")}
        backend, fake = _backend_with(
            _machine(running=[_AL], overrides=overrides), config=_ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "gateway modes could not be read: daemon is not responding" in str(raised.value)
        assert "holds a host address" not in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("run", "-d", "--name", _AL) == []

    def test_a_bridge_isolated_on_one_family_only_is_still_addressed(self):
        """Reading one of the two options would adopt a network an IPv6-enabled daemon still
        gives a host address, which is the whole route back."""
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: _GATEWAY_MODE_ISOLATED}), _ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_a_network_that_will_not_go_away_fails_the_acquire(self):
        """The removals report failure rather than raising it, so reading past them would hand
        back the warm workload on the bridge this was trying to take away."""
        overrides = {("network", "rm"): _DockerResult(1, b"", "network has active endpoints")}
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: ""}, overrides=overrides), _ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError, match="could not be replaced") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "its bridge holds a host address" in str(raised.value)
        assert fake.matching("run", "-d", "--name", _AL) == []

    def test_an_unaddressed_bridge_keeps_its_warm_sandbox(self):
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED}), _ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) == []
        assert fake.matching("network", "rm", _AL_NET) == []

    def test_a_workload_that_outlived_its_network_is_rebuilt(self):
        """Absence is safe for a sandbox about to be created and not for one already running:
        a container cannot be on a network that is not there, so what it is on instead is
        outside this backend's account of it. Building a fresh network beside it and reusing
        it anyway would leave the allowlist describing something the workload never joined."""
        backend, fake = _backend_with(_machine(running=[_AL]), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        created = _run_named(fake, _AL)
        assert created.args[created.args.index("--network") + 1] == _AL_NET

    def test_a_workload_that_will_not_go_fails_the_acquire_even_with_no_network(self):
        """The network read cannot stand in for the container's removal when there is no
        network: it says "usable" for an absence that was already true, so a workload the
        engine refused to remove would be reused on whatever it is attached to."""
        overrides = {("rm", "-f", _AL): _DockerResult(1, b"", "device or resource busy")}
        backend, fake = _backend_with(
            _machine(running=[_AL], overrides=overrides), config=_ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError, match="is still there") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "device or resource busy" in str(raised.value)
        assert fake.matching("run", "-d", "--name", _AL) == []

    #: A socket error carries the errno underneath it, so it says an absence phrase about
    #: something that is not this container. Named rather than inlined: two adjacent literals
    #: in a list read as a missing comma, and here that would silently drop a case.
    _SOCKET_ERROR = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock: "
        "connect: no such file or directory"
    )

    @pytest.mark.parametrize(
        "stderr",
        ["daemon not responding", _SOCKET_ERROR],
        ids=["opaque", "socket-error-borrowing-the-phrase"],
    )
    def test_a_container_read_that_fails_is_not_read_as_no_container(self, stderr: str):
        """Skipping the rebuild needs proof the container is gone, not a failure to see it.

        The proof has to name this container. An absence phrase said about something else —
        the socket the daemon was not listening on — leaves the workload on its old
        attachment with a fresh network built beside it, once the next read succeeds.
        """
        overrides = {("inspect", "-f", "{{.State.Status}}"): _DockerResult(1, b"", stderr)}
        backend, fake = _backend_with(
            _machine(running=[_AL], overrides=overrides), config=_ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        created = _run_named(fake, _AL)
        assert created.args[created.args.index("--network") + 1] == _AL_NET

    def test_a_workload_moved_off_its_own_network_is_rebuilt(self):
        """A network this backend would build is not the same fact as the workload being on
        it: `docker network disconnect` followed by `connect` leaves the name, the network and
        the modes all intact while the container reaches somewhere else entirely."""
        backend, fake = _backend_with(
            _machine(
                running=[_AL],
                networks={_AL_NET: _UNADDRESSED},
                attached={_AL: ["some-other-network"]},
            ),
            _ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        created = _run_named(fake, _AL)
        assert created.args[created.args.index("--network") + 1] == _AL_NET

    def test_a_workload_given_a_second_network_is_rebuilt(self):
        """Keeping the right attachment is not the same as having only it. A `network connect`
        adds an endpoint without taking the first away, so the expected network is still there
        to find while the workload reaches out through the other one."""
        backend, fake = _backend_with(
            _machine(
                running=[_AL],
                networks={_AL_NET: _UNADDRESSED},
                attached={_AL: [_AL_NET, "an-unrestricted-network"]},
            ),
            _ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        created = _run_named(fake, _AL)
        assert created.args[created.args.index("--network") + 1] == _AL_NET

    def test_a_container_swapped_after_the_state_read_is_not_handed_out(self):
        """The topology check and the reads that choose reuse are separate calls, and this
        backend's lock orders nothing against another process. A container answering those
        reads is not necessarily the one still here when the sandbox is returned, so the
        attachment is established once more at the last moment.

        This narrows the window rather than closing it: nothing checked from outside can bind
        a container's attachment for the life of a call, and a change after the final read is
        indistinguishable from one after the acquire returns.
        """
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        reads = itertools.count()

        def swapped(args: tuple[str, ...]) -> _DockerResult:
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                # Right when the topology check looks, moved by the time the sandbox is handed
                # out — which is the only ordering the final read exists to catch.
                on = _AL_NET if next(reads) == 0 else "an-unrestricted-network"
                return _DockerResult(0, (on + " ").encode(), "")
            return base(args)

        backend, fake = _backend_with(swapped, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="cannot be served"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        # Refusing the acquire does not stop what is already inside: `exec` detaches, so the
        # container may hold processes from earlier calls that keep the extra network's reach
        # until it is gone.
        assert fake.matching("rm", "-f", _AL) != []

    def test_an_attachment_changed_during_fact_collection_is_still_caught(self):
        """Collecting the container's facts is several more awaited calls, so a check made
        before them is only as current as they are long. The topology read comes after."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        facts_done = False

        def moved_while_reading(args: tuple[str, ...]) -> _DockerResult:
            nonlocal facts_done
            if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
                facts_done = True
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                on = "an-unrestricted-network" if facts_done else _AL_NET
                return _DockerResult(0, (on + " ").encode(), "")
            return base(args)

        backend, fake = _backend_with(moved_while_reading, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="cannot be served"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []

    def test_a_third_container_on_the_sandboxs_network_is_caught(self):
        """The workload's own attachment can be exactly right while the network it sits on
        holds a peer. Containers on one internal network reach each other, so a peer holding a
        second network is a route around the proxy that no allowlist describes — measured on
        29.7.2: the workload reaches such a peer, and the peer holds `bridge`.
        """
        backend, fake = _backend_with(
            _machine(
                running=[_AL],
                networks={_AL_NET: _UNADDRESSED},
                peers={_AL_NET: ["someone-elses-container"]},
            ),
            _ALLOW_CONFIG,
        )
        with pytest.raises(RuntimeError, match="also holds someone-elses-container") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "can reach directly" in str(raised.value)
        # Removing the workload leaves the peer, so a plain retry would meet the same network.
        assert "Disconnect or remove someone-elses-container" in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []

    def test_a_proxy_that_lost_its_outbound_leg_is_caught(self):
        """The one failure in the other direction. Everything the workload touches is right and
        its way out is gone, so `ALLOWLIST` would be served as a silent `CLOSED` — the
        degradation `_ensure_proxy` already refuses to create, arriving after it instead."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})

        def unhooked(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "connect"):
                return _DockerResult(0, b"", "")  # accepted, and quietly undone afterwards
            return base(args)

        backend, fake = _backend_with(unhooked, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="allowlist reaches nothing") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "its proxy is on" in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []

    def test_an_unreadable_endpoint_list_refuses_rather_than_assuming_it_is_clear(self):
        """The fail-closed branch for a network whose membership could not be read at all —
        distinct from one read and found to hold a peer, and just as unserveable."""
        overrides = {
            ("network", "inspect", "-f", _NETWORK_ENDPOINTS_FORMAT): _DockerResult(
                1, b"", "daemon is not responding"
            )
        }
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED}, overrides=overrides),
            _ALLOW_CONFIG,
        )
        with pytest.raises(RuntimeError, match="could not be read"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []

    def test_a_read_that_raises_still_reaches_the_removal(self):
        """`_docker` propagates a timeout rather than returning one, so a raising read would
        carry the exception past the removal the refusal had already decided on — and `exec`
        detaches, so what is inside keeps running with a topology nothing established."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        reads = itertools.count()

        def timing_out(args: tuple[str, ...]) -> _DockerResult:
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                if next(reads) > 0:  # right at the discard, gone by the final read
                    raise TimeoutError("docker inspect timed out")
            return base(args)

        backend, fake = _backend_with(timing_out, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="could not be read") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "timed out" in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []

    def test_a_network_replaced_under_its_own_name_is_caught(self):
        """The attachment compares names, so a network swapped for an addressed one keeps
        satisfying it. Both the bridge and the attachment are read at the end for that reason:
        neither on its own establishes what the workload can reach."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        bridge_reads = itertools.count()

        def replaced(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET:
                # Built by this backend while the acquire and its network setup look at it,
                # someone else's by the time the sandbox would be handed out.
                modes = _UNADDRESSED if next(bridge_reads) < 2 else ""
                return _DockerResult(0, modes.encode() + b"\n", "")
            return base(args)

        backend, _ = _backend_with(replaced, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="no longer one this backend would build"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

    def test_an_unreadable_final_read_does_not_describe_a_topology(self):
        """The final read refuses on anything but the exact attachment, and an unreadable
        answer is one of those — but nothing looked at a network, so the refusal must not
        report one. It says what the daemon said instead."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        reads = itertools.count()

        def unreadable_last(args: tuple[str, ...]) -> _DockerResult:
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                if next(reads) == 0:
                    return _DockerResult(0, (_AL_NET + " ").encode(), "")
                return _DockerResult(1, b"", "daemon is not responding")
            return base(args)

        backend, _ = _backend_with(unreadable_last, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "networks that could not be read: daemon is not responding" in str(raised.value)
        assert "rather than" not in str(raised.value)

    def test_a_swapped_container_that_will_not_go_says_it_is_still_running(self):
        """The removal reports failure rather than raising it, so an acquire that read past it
        would tell an operator to retry while the workload kept the reach that was refused."""
        base = _machine(
            running=[_AL],
            networks={_AL_NET: _UNADDRESSED},
            overrides={("rm", "-f", _AL): _DockerResult(1, b"", "device or resource busy")},
        )
        reads = itertools.count()

        def swapped(args: tuple[str, ...]) -> _DockerResult:
            if args[:3] == ("inspect", "-f", _ATTACHED_NETWORKS_FORMAT) and args[-1] == _AL:
                on = _AL_NET if next(reads) == 0 else "an-unrestricted-network"
                return _DockerResult(0, (on + " ").encode(), "")
            return base(args)

        backend, _ = _backend_with(swapped, config=_ALLOW_CONFIG)
        with pytest.raises(
            RuntimeError, match="still running with whatever that reaches"
        ) as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "device or resource busy" in str(raised.value)

    def test_a_cold_acquire_has_nothing_to_replace(self):
        """No network yet is the ordinary first acquire, not a stale one."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("network", "rm", _AL_NET) == []

    def test_a_closed_sandbox_is_never_read_for_a_network_it_has_none_of(self):
        backend, fake = _backend_with(_machine(running=[_NAME]), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _SPEC))  # _SPEC has egress_allow=()
        assert fake.matching("network", "inspect") == []


class TestAnEngineThatWillNotBuildAnUnaddressedBridge:
    """The mode arrived in Docker Engine 28.0.0; an older daemon rejects the value by name.

    Refused rather than served on an addressed bridge: that bridge is a route to the host the
    allowlist does not cover, so the weaker topology is not a fallback.
    """

    _REJECTED = _DockerResult(
        1,
        b"",
        "Error response from daemon: failed to parse "
        "com.docker.network.bridge.gateway_mode_ipv4 value: isolated "
        "(unknown gateway mode isolated)",
    )

    def test_the_acquire_fails_naming_the_engine_the_mode_needs(self):
        backend, _ = _backend_with(
            _machine(overrides={("network", "create"): self._REJECTED}), config=_ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError, match="28.0.0"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

    def test_no_workload_is_started_on_the_weaker_topology_instead(self):
        backend, fake = _backend_with(
            _machine(overrides={("network", "create"): self._REJECTED}), config=_ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("run") == []

    def test_an_unrelated_create_failure_does_not_blame_the_engine_version(self):
        overrides = {
            ("network", "create"): _DockerResult(1, b"", "could not find an available subnet")
        }
        backend, _ = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="available subnet") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "28.0.0" not in str(raised.value)


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


class TestRemoveNetworkNotFound:
    def test_a_missing_network_is_a_no_op_not_a_warning(self, caplog):
        """Docker reports a missing network as 'not found', not the 'no such' a missing container
        yields — the daemon uses a different noun per object type. A purge that tries a workload's
        network whether or not that workload had one must not log a failure for the benign absence.
        """
        overrides = {
            ("network", "rm"): _DockerResult(
                1, b"", "Error response from daemon: network some-net not found"
            )
        }
        backend, fake = _backend_with(_machine(overrides=overrides))
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_docker._backend"):
            removed = asyncio.run(backend._remove_network("some-net"))
        assert removed is False
        assert fake.only("network", "rm").args == ("network", "rm", "some-net")
        assert not any("failed to remove network" in r.message for r in caplog.records)

    def test_a_real_removal_failure_still_warns(self, caplog):
        """A failure that is not the benign not-found wording — 'has active endpoints', say — is a
        real leak and must still warn, so the not-found carve-out cannot mask a genuine error."""
        overrides = {("network", "rm"): _DockerResult(1, b"", "has active endpoints")}
        backend, _ = _backend_with(_machine(overrides=overrides))
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_docker._backend"):
            removed = asyncio.run(backend._remove_network("some-net"))
        assert removed is False
        assert any("failed to remove network" in r.message for r in caplog.records)
