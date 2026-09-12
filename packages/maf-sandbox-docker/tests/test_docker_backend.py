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
import itertools
import json
import logging
import sys
import tarfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from maf_sandbox import (
    Capability,
    DisposalFailure,
    Egress,
    EgressObserved,
    EntryKind,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxOsFamilyNotSupported,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    ScopePurge,
)

from maf_sandbox_docker import BACKEND_NAME, DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import (
    _GATEWAY_MODE_ISOLATED,
    _GATEWAY_MODE_OPTS,
    _PROXY_LOG_BYTES,
    _PROXY_LOG_TAIL,
    _container_name,
    _DockerResult,
    _egress_decisions,
    _network_name,
    _proxy_name,
    _sandbox_labels,
    _Sweep,
)

#: What `network inspect` prints for a network this backend built: an internal bridge whose
#: IPAM entry carries a subnet and no gateway, which is what an isolated gateway mode leaves.
_UNADDRESSED = 'bridge|true|[{"Subnet":"172.20.0.0/16"}]'
#: The same network with its bridge addressed. Every other field matches, so the `Gateway` in
#: the IPAM entry is the whole difference — and it is a route to the host in both directions.
_ADDRESSED = 'bridge|true|[{"Subnet":"172.20.0.0/16","Gateway":"172.20.0.1"}]'
#: Dual-stack, with only the second family addressed: what a daemon that took one of the two
#: options and not the other leaves. The IPv4 entry alone reads as safe, so this is the shape
#: that separates reading every entry from reading the first.
_ADDRESSED_ON_THE_SECOND_FAMILY = (
    'bridge|true|[{"Subnet":"172.20.0.0/16"},{"Subnet":"fd00::/64","Gateway":"fd00::1"}]'
)

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="bicep", image="bicep-sandbox:local")
_NAME = _container_name(_KEY, _SPEC.kind)
_WORK = "/maf-sandbox/work"
# Method tests prepare their own paths; lifecycle tests exercise the acquire contract.
_METHOD_SPEC = replace(_SPEC, requires=frozenset())


@pytest.mark.parametrize("state", ["cold", "warm", "stopped"])
def test_acquire_creates_missing_base_as_guest_without_a_guest_command(state):
    machine = _machine(
        running=[_NAME] if state == "warm" else [],
        stopped=[_NAME] if state == "stopped" else [],
        overrides={("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:20001", "")},
    )
    backend, fake = _backend_with(machine)
    asyncio.run(backend.acquire(_KEY, _SPEC))
    with tarfile.open(fileobj=io.BytesIO(fake.only("cp", "-").stdin)) as archive:
        entries = archive.getmembers()
    assert [(e.name, e.uid, e.gid, e.mode) for e in entries] == [
        ("maf-sandbox", 0, 0, 0o755),
        ("maf-sandbox/work", 10001, 20001, 0o755),
    ]
    assert all(e.isdir() for e in entries)
    assert not fake.matching("exec")


def test_acquire_directory_failure_is_retryable_on_the_same_key():
    failures = {("cp", "-"): _DockerResult(1, b"", "read-only filesystem")}
    backend, fake = _backend_with(_machine(running=[_NAME], overrides=failures))
    with pytest.raises(RuntimeError, match="working directory"):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    fake._responder = _machine(running=[_NAME])
    asyncio.run(backend.acquire(_KEY, _SPEC))
    assert len(fake.matching("cp", "-")) == 2


@pytest.mark.parametrize("exit_code", [0, 7])
def test_bounded_read_preserves_exit_status_after_stdout_closes(exit_code):
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
        result = await backend._docker(
            "-c",
            "import os, time; os.write(1, b'ok'); os.close(1); time.sleep(0.1); "
            f"os.write(2, b'detail'); os._exit({exit_code})",
            read_limit=100,
            timeout=5,
        )
        assert result.stdout == b"ok"
        assert result.stderr == "detail"
        assert result.returncode == exit_code

    asyncio.run(scenario())


def test_bounded_read_and_exit_share_one_timeout():
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
        with pytest.raises(TimeoutError):
            await backend._docker(
                "-c",
                "import os, time; time.sleep(0.6); os.write(1, b'ok'); os.close(1); "
                "time.sleep(0.6)",
                read_limit=100,
                timeout=1,
            )

    asyncio.run(scenario())


def test_bounded_read_drains_a_full_pipe_before_waiting_for_exit():
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
        result = await asyncio.wait_for(
            backend._docker(
                "-c", "import os; os.write(1, b'x' * 1000000)", read_limit=1, timeout=5
            ),
            timeout=10,
        )
        assert result.stdout == b"x"

    asyncio.run(scenario())


def test_bounded_read_timeout_drains_a_full_error_pipe():
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig(docker_path=sys.executable))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                backend._docker(
                    "-c", "import os; os.write(2, b'e' * 1000000)", read_limit=1, timeout=0.2
                ),
                timeout=5,
            )

    asyncio.run(scenario())


def _tar_bytes(path: str, data: bytes, *, pax_headers: dict[str, str] | None = None) -> bytes:
    """A one-entry tar as ``docker cp <name>:<path> -`` would stream it out."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.size = len(data)
        entry.mode = 0o644
        if pax_headers is not None:
            entry.pax_headers = pax_headers
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


def _not_in_the_container(guest: str, container: str = _NAME) -> _DockerResult:
    """What ``docker cp`` answers for a path the container does not have.

    Verbatim from Engine 29.7.2, and the whole point is the path in it: naming the path is
    how the engine says *this* one is missing, and a message that names something else is
    not an absence the backend may act on.
    """
    return _DockerResult(
        1,
        b"",
        f"Error response from daemon: Could not find the file {guest} in container {container}",
    )


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
        if (
            args[:1] == ("cp",)
            and args[1] != "-"
            and args[1].partition(":")[2] in ("/maf-sandbox", _WORK)
            and result == _DockerResult(0, b"", "")
        ):
            result = _DockerResult(0, _owned_directory_tar("work", 0, 0o755), "")
        if args[:3] == ("inspect", "-f", "{{.Id}}") and result == _DockerResult(0, b"", ""):
            result = _DockerResult(0, f"id-{args[-1]}\n".encode(), "")
        if args[:3] == ("inspect", "-f", "{{json .Config.Labels}}") and result == _DockerResult(
            0, b"", ""
        ):
            result = _DockerResult(0, json.dumps({"maf-sandbox.work-dir.v1": _WORK}).encode(), "")
        if read_limit is not None and len(result.stdout) > read_limit:
            result = _DockerResult(result.returncode, result.stdout[:read_limit], result.stderr)
        return result

    def mark(self) -> None:
        """Draw a line under what has been recorded, so a later assertion starts from here."""
        self._marked = len(self.calls)

    def cp_since_mark(self) -> list[tuple[str, ...]]:
        return [call.args for call in self.calls[self._marked :] if call.args[:1] == ("cp",)]

    def matching(self, *prefix: str) -> list[_Recorded]:
        return [
            c
            for c in self.calls
            if c.args[: len(prefix)] == prefix
            and not (c.args[0] == "exec" and c.read_limit == 1024)
        ]

    def only(self, *prefix: str) -> _Recorded:
        found = [c for c in self.matching(*prefix) if c.read_limit != 1024]
        assert len(found) == 1, [c.args for c in self.calls]
        return found[0]


def _machine(
    running: Sequence[str] = (),
    stopped: Sequence[str] = (),
    images: Sequence[str] = ("bicep-sandbox:local",),
    overrides: dict[tuple[str, ...], _DockerResult] | None = None,
    networks: Mapping[str, str] | None = None,
    work_dir: str = _WORK,
):
    """A responder describing which containers and images exist, and how a command answers.

    ``docker inspect -f {{.State.Running}}`` decides existence and running state — a name in
    ``running`` prints ``true``, one only in ``stopped`` prints ``false``, one in neither errors
    like a missing container. ``image inspect`` succeeds for a known image and errors otherwise.

    ``rm -f`` takes a container out of that state, because ``acquire`` reads it again after a
    removal and a responder still answering "running" sends the acquire down its reuse branch.

    ``networks`` maps a network name to what ``network inspect`` prints for the effect format
    — ``_UNADDRESSED`` for one this backend built, ``_ADDRESSED`` for one whose bridge kept a
    host address. A name absent from it answers "not found", the cold path an acquire that has
    yet to build one takes. ``network rm`` removes the name, and refuses while anything is
    still attached, the way a real engine does; override ``("network", "rm")`` to model one
    that fails for another reason.

    The longest matching ``overrides`` prefix wins, so a per-path ``cp`` answer beats a
    catch-all one however the mapping was written.
    """
    live_running = set(running)
    live_stopped = set(stopped)
    storage_labels = {name: {"maf-sandbox.work-dir.v1": work_dir} for name in (*running, *stopped)}
    live_networks = dict(networks or {})
    # Who is on each network, so `network rm` can refuse while an endpoint is still attached,
    # the way a real engine does.
    live_endpoints: dict[str, set[str]] = {}
    for _c in live_running | live_stopped:
        _net = _network_name(_c)
        live_endpoints.setdefault(_net, set()).add(_c)
        # A warm allowlisted sandbox is a workload *and* its proxy on that network, so a
        # teardown that takes the workload and leaves the proxy still has an endpoint to trip
        # over — which is the only reason a real `network rm` refuses one of these.
        if _net in live_networks:
            live_endpoints[_net].add(_proxy_name(_c))
    ranked = sorted((overrides or {}).items(), key=lambda item: len(item[0]), reverse=True)

    def respond(args: tuple[str, ...]) -> _DockerResult:
        for prefix, result in ranked:
            if args[: len(prefix)] == prefix:
                return result
        if args[:2] == ("rm", "-f"):
            name = args[-1]
            live_running.discard(name)
            live_stopped.discard(name)
            for holders in live_endpoints.values():
                holders.discard(name)
            return _DockerResult(0, name.encode() + b"\n", "")
        if args[:3] == ("run", "-d", "--name"):
            # A create the engine accepted leaves a running container on the network it was
            # given, which the reads after it are entitled to find.
            name = args[3]
            live_running.add(name)
            storage_labels[name] = dict(
                args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label"
            )
            if "--network" in args:
                net = args[args.index("--network") + 1]
                live_endpoints.setdefault(net, set()).add(name)
            return _DockerResult(0, name.encode() + b"\n", "")
        if args[:2] == ("image", "inspect"):
            image = args[2]
            return (
                _DockerResult(0, b"", "")
                if image in images
                else _DockerResult(1, b"", "No such image")
            )
        if args[:2] == ("network", "connect"):
            net, target = args[2], args[3]
            live_endpoints.setdefault(net, set()).add(target)
            return _DockerResult(0, b"", "")
        if args[:2] == ("network", "disconnect"):
            net, target = args[2], args[3]
            live_endpoints.get(net, set()).discard(target)
            return _DockerResult(0, b"", "")
        if args[:2] == ("network", "inspect"):
            net = args[-1]
            modes = live_networks.get(net)
            if modes is None:
                return _DockerResult(1, b"", f"Error response from daemon: network {net} not found")
            return _DockerResult(0, modes.encode() + b"\n", "")
        if args[:2] == ("network", "rm"):
            net = args[-1]
            if live_endpoints.get(net):
                # What a real engine says while a container still holds an endpoint, so a
                # teardown that removed the network before the containers fails here.
                held = ", ".join(sorted(live_endpoints[net]))
                return _DockerResult(1, b"", f"error: network {net} has active endpoints: {held}")
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
            # Derived from the argv so the create and the read that follows it cannot disagree:
            # a create that stopped asking for the mode reads back as an addressed bridge.
            modes = [o.split("=", 1)[1] for o in args if o.startswith("com.docker.network.")]
            asked = modes == [_GATEWAY_MODE_ISOLATED] * len(_GATEWAY_MODE_OPTS)
            live_networks[net] = _UNADDRESSED if asked else _ADDRESSED
            return _DockerResult(0, net.encode() + b"\n", "")
        if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
            return _DockerResult(0, b"\n", "")
        if args[:3] == ("inspect", "-f", "{{.Id}}"):
            return _DockerResult(0, f"id-{args[-1]}\n".encode(), "")
        if args[:3] == ("inspect", "-f", "{{json .Config.Labels}}"):
            if (
                args[-1].startswith("maf-sandbox-docker-")
                and args[-1] not in live_running | live_stopped
            ):
                return _DockerResult(1, b"", f"Error: No such container: {args[-1]}")
            return _DockerResult(
                0,
                json.dumps(
                    storage_labels.get(
                        args[-1].removeprefix("id-"), {"maf-sandbox.work-dir.v1": work_dir}
                    )
                ).encode(),
                "",
            )
        if args[0] == "cp" and args[1].endswith(":/"):
            # Every walk now stats the root, and a real engine answers it with the root
            # directory's own header — root's, writable by nobody else on any sane image.
            return _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
        if args[0] == "inspect" or args[:2] == ("container", "inspect"):
            name = args[-1]
            if name not in live_running | live_stopped:
                return _DockerResult(1, b"", f"Error: No such object: {name}")
            if args[:2] == ("container", "inspect"):
                from maf_sandbox_docker._backend import _sandbox_labels

                labels = _sandbox_labels(_KEY, _ALLOW_SPEC)
                if name.endswith("-proxy"):
                    labels["maf-sandbox.role"] = "proxy"
                return _DockerResult(
                    0, json.dumps([{"Id": name, "Config": {"Labels": labels}}]).encode(), ""
                )
            state = "true" if name in live_running else "false"
            return _DockerResult(0, state.encode() + b"\n", "")
        if args[:2] == ("ps", "-a") or args[0] == "ps":
            names = [*live_running, *live_stopped] if "-a" in args else list(live_running)
            return _DockerResult(0, "".join(f"{n}\n" for n in names).encode(), "")
        if args[0] == "logs":
            return _DockerResult(0, b"listening on 3128\n", "")
        if args[0] == "cp" and args[1] != "-":
            container, _, guest = args[1].partition(":")
            return _not_in_the_container(guest, container)
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


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("failure", ["status", "empty", "decode", "raise"])
def test_failed_instance_inspection_disposes_the_container(warm, failure):
    machine = _machine(running=[_NAME] if warm else [])

    def respond(args):
        if args[:3] == ("inspect", "-f", "{{.Id}}"):
            if failure == "raise":
                raise TimeoutError("inspection timed out")
            return _DockerResult(
                1 if failure == "status" else 0, b"\xff" if failure == "decode" else b"\n", ""
            )
        return machine(args)

    backend, fake = _backend_with(respond)
    with pytest.raises((RuntimeError, UnicodeDecodeError, TimeoutError)):
        asyncio.run(backend.acquire(_KEY, _SPEC))
    assert any(_NAME in call.args for call in fake.matching("rm", "-f"))
    assert bool(fake.matching("run")) is (not warm)


def test_instance_id_comes_from_the_engine_on_every_acquire():
    ids = ["a" * 64]
    machine = _machine(running=[_NAME])

    def respond(args):
        if args[:3] == ("inspect", "-f", "{{.Id}}"):
            return _DockerResult(0, ids[0].encode(), "")
        return machine(args)

    backend, _ = _backend_with(respond)
    first = asyncio.run(backend.acquire(_KEY, _SPEC))
    second = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert first is not second
    assert first.instance_id == second.instance_id == "a" * 64
    ids[0] = "b" * 64
    replacement = asyncio.run(backend.acquire(_KEY, _SPEC))
    assert replacement.instance_id == "b" * 64
    assert first.instance_id == "a" * 64


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
                Capability.RECLAIM,
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

    @pytest.mark.parametrize("allowlisting", [False, True])
    def test_no_mount_or_socket_ever_crosses(self, allowlisting: bool):
        """The pull surface requires rootfs paths, including on allowlisted workloads."""
        config = _ALLOW_CONFIG if allowlisting else DockerSandboxConfig()
        spec = _ALLOW_SPEC if allowlisting else _SPEC
        backend, fake = _backend_with(_machine(), config=config)
        asyncio.run(backend.acquire(_KEY, spec))
        runs = fake.matching("run")
        assert len(runs) == (2 if allowlisting else 1)
        for run in runs:
            assert not any(a.startswith("-v") for a in run.args)
            assert not {a.partition("=")[0] for a in run.args} & {
                "--volume",
                "--volumes-from",
                "--mount",
                "--tmpfs",
            }
            assert not any("docker.sock" in a for a in run.args)

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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(
            sandbox.exec(["bicep", "build", "main.bicep"], working_directory=_WORK, timeout=5)
        )
        args = fake.only("exec").args
        assert args == ("exec", "-w", _WORK, _NAME, "bicep", "build", "main.bicep")

    def test_a_string_is_run_by_a_shell(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.exec("echo hi || true", working_directory=_WORK, timeout=5))
        args = fake.only("exec").args
        assert args[-3:] == ("sh", "-c", "echo hi || true")

    def test_the_per_call_timeout_reaches_the_seam(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.exec(["true"], working_directory=_WORK, timeout=42))
        assert fake.only("exec").timeout == 42

    def test_both_raw_streams_survive_the_adapter(self):
        raw = bytes(range(256))
        overrides = {("exec", "-w", _WORK): _DockerResult(7, raw, "display", raw[::-1])}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        result = asyncio.run(sandbox.exec(["x"], working_directory=_WORK, timeout=5))
        assert result.stdout_bytes == raw
        assert result.stderr_bytes == raw[::-1]

    def test_stdout_stderr_and_exit_code_are_mapped(self):
        overrides = {("exec", "-w", _WORK): _DockerResult(7, b"out\n", "err\n")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=_WORK_IS_A_DIRECTORY))
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
        assert fake.matching("exec") == []

    def test_a_path_outside_the_working_directory_is_refused(self):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError):
            asyncio.run(sandbox.remove("../../etc", working_directory=_WORK, recursive=True))
        assert fake.matching("exec") == []


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
        overrides = {("exec", "--user", "0"): _DockerResult(1, b"", "rm: permission denied")}
        sandbox, fake = self._sandbox(overrides)
        with pytest.raises(OSError, match=r"rm exited 1.*rm: permission denied"):
            asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=30))

    def test_the_timeout_reaches_the_transport(self):
        sandbox, fake = self._sandbox()
        asyncio.run(sandbox.reclaim(f"{_WORK}/x", working_directory=_WORK, timeout=42))
        assert fake.only("exec").timeout == 42

    def test_the_removal_runs_from_root_not_the_uncreated_working_directory(self):
        """Reclaim must tolerate a caller's child directory that was never created."""
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
            **(_CAPS_DROPPED if capabilities_dropped else {}),
            **(overrides or {}),
            ("exec", "-w", "/", f"id-{_NAME}"): _DockerResult(0, b"", ""),
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

    def test_the_retry_gets_what_is_left_of_the_one_deadline(self, monkeypatch: pytest.MonkeyPatch):
        """Both removal attempts share one deadline."""
        from types import SimpleNamespace

        import maf_sandbox_docker._backend as docker_backend

        now = 1000.0
        spent = 0.25
        base = _machine(running=[_NAME], overrides={**_WORK_IS_A_DIRECTORY, **_CAPS_DROPPED})

        def refuse_as_root(args):
            nonlocal now
            if args[:3] == ("exec", "--user", "0"):
                now += spent
                return _DockerResult(1, b"", "rm: Permission denied")
            return base(args)

        backend, fake = _backend_with(refuse_as_root)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        monkeypatch.setattr(docker_backend, "time", SimpleNamespace(monotonic=lambda: now))
        asyncio.run(sandbox.reclaim(f"{_WORK}/call-a1b2c3", working_directory=_WORK, timeout=30))

        first, second = fake.matching("exec")
        assert first.timeout == 30
        assert second.timeout == 30 - spent

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

    def test_failed_removal_preserves_both_attempts_diagnostic_bytes(self):
        root = b"root: \xff\xe2\x82\n"
        guest = b"guest: \xfe\x00\n"
        differ = {
            ("exec", "--user", "0"): _DockerResult(1, b"", root.decode("utf-8", "replace"), root),
            ("exec", "-w"): _DockerResult(2, b"", guest.decode("utf-8", "replace"), guest),
        }
        sandbox, _fake = self._sandbox(differ, capabilities_dropped=True)
        result = asyncio.run(
            sandbox._removal(
                ["rm", "-rf", "--", f"{_WORK}/x"],
                working_directory="/",
                timeout=30,
                raise_authority=True,
            )
        )
        assert result.exit_code == 2
        assert result.stderr_bytes == guest.strip() + b" (as root: " + root.strip() + b")"
        assert result.stderr == result.stderr_bytes.decode("utf-8", "replace")

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

    def _backend(self, parent: bytes | None, image: str = _METHOD_SPEC.image):
        overrides = {
            ("cp", f"{_NAME}:/"): _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
        }
        if parent is not None:
            overrides[_cp("/maf-sandbox")] = _DockerResult(0, parent, "")
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return backend, fake, SandboxSpec(requires=frozenset(), kind=_METHOD_SPEC.kind, image=image)

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
        assert "--user" not in self._reclaimed_as(backend, fake, _METHOD_SPEC)

    def test_an_unreadable_root_does_the_same(self):
        """Nothing verified, nothing licensed: the root is the swap the directories below it
        cannot witness, so a walk that cannot read it licenses no removal at all."""

        def refuseless(args):
            if args[:2] == ("cp", f"{_NAME}:/"):
                raise RuntimeError("the daemon said no")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(refuseless)
        assert "--user" not in self._reclaimed_as(backend, fake, _METHOD_SPEC)

    def test_a_writable_root_is_what_closes_licensing(self):
        """A root the guest could have written is the swap the chain above the work dir cannot
        see — its header, read by the same walk, is what the rule rests on."""

        def writable(args):
            if args[:2] == ("cp", f"{_NAME}:/"):
                return _DockerResult(0, _owned_directory_tar(".", 0, 0o777), "")
            return _machine(running=[_NAME])(args)

        backend, fake = _backend_with(writable)
        assert "--user" not in self._reclaimed_as(backend, fake, _METHOD_SPEC)

    def test_a_work_dir_straight_under_the_root_is_answered_by_the_root_alone(self):
        """`/work` has no ancestors above it, so the walk is just ``/`` — the component the
        chain never reached, and the one every other component's replacement relies on."""

        def root_only(args):
            if args[:2] == ("cp", f"{_NAME}:/maf-sandbox"):
                raise RuntimeError("the daemon said no")
            if args[:2] == ("cp", f"{_NAME}:/"):
                return _DockerResult(0, _owned_directory_tar(".", 0, 0o755), "")
            return _machine(running=[_NAME], work_dir="/work")(args)

        backend, fake = _backend_with(root_only)
        spec = SandboxSpec(
            requires=frozenset(), kind=_METHOD_SPEC.kind, image=_METHOD_SPEC.image, work_dir="/work"
        )
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
        asyncio.run(
            backend.acquire(
                _KEY, SandboxSpec(requires=frozenset(), kind=_METHOD_SPEC.kind, image="other:local")
            )
        )
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]

    def test_a_changed_image_id_re_reads_even_where_the_image_name_holds_still(self):
        """`image_id` is what `_create_workload` runs when a spec carries one, so it is what
        the key has to follow.
        """
        backend, fake, _ = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        pinned = SandboxSpec(
            requires=frozenset(), kind=_METHOD_SPEC.kind, image="same:local", image_id="sha256:aaa"
        )
        asyncio.run(backend.acquire(_KEY, pinned))
        fake.mark()
        asyncio.run(
            backend.acquire(
                _KEY,
                SandboxSpec(
                    requires=frozenset(),
                    kind=_METHOD_SPEC.kind,
                    image="same:local",
                    image_id="sha256:bbb",
                ),
            )
        )
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]

    def test_the_same_image_id_is_still_read_once(self):
        backend, fake, _ = self._backend(_owned_directory_tar("maf-sandbox", 0, 0o755))
        pinned = SandboxSpec(
            requires=frozenset(), kind=_METHOD_SPEC.kind, image="same:local", image_id="sha256:aaa"
        )
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
                ("exec", "--user", "0"): _DockerResult(1, b"", "rm: Permission denied"),
            },
        )

        def respond(args):
            if args[:3] == ("inspect", "-f", "{{.HostConfig.CapDrop}}"):
                return _DockerResult(0, hardening[0], "")
            if args[:3] == ("run", "-d", "--name"):
                present.add(args[3])
            if args[0] == "inspect" and args[-1].removeprefix("id-") not in present:
                return _DockerResult(1, b"", f"Error: No such object: {args[-1]}")
            return base(args)

        return _backend_with(respond)

    def test_the_replacement_container_decides_its_own_removals(self):
        """The consequence, not the cache: a root refusal is retried only where the container
        holds no `CAP_DAC_OVERRIDE`, so stale facts leave a hardened container unable to remove.
        """
        present, hardening = {_NAME}, [b"[]\n"]
        backend, fake = self._backend(present, hardening)

        keeps_capabilities = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        with pytest.raises(OSError, match="Permission denied"):
            asyncio.run(keeps_capabilities.reclaim(self._CALL, working_directory=_WORK, timeout=30))

        # Removed by something that is not this backend, and the name taken by a hardened one.
        present.discard(_NAME)
        hardening[0] = b"[ALL]\n"

        replaced = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        fake.mark()
        asyncio.run(replaced.reclaim(self._CALL, working_directory=_WORK, timeout=30))
        assert [call.args[:3] for call in fake.matching("exec")][-2:] == [
            ("exec", "--user", "0"),
            ("exec", "-w", "/"),
        ]

    def test_the_ancestors_of_the_replacement_are_read_again(self):
        present, hardening = {_NAME}, [b"[]\n"]
        backend, fake = self._backend(present, hardening)
        asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))

        present.discard(_NAME)
        fake.mark()
        asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        assert fake.cp_since_mark() == [(*_cp("/"), "-"), (*_cp("/maf-sandbox"), "-")]


class TestReclaimKeepsAFloorUnderRoot:
    """Reclamation checks child placement and root distance before running a command."""

    def _sandbox(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        return asyncio.run(backend.acquire(_KEY, _SPEC)), fake

    @pytest.mark.parametrize("directory", ["/", "/etc", "/maf-sandbox/", "//tmp", "/a/.."])
    def test_a_path_within_two_components_of_the_root_runs_no_command(self, directory):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError, match="close to the root"):
            asyncio.run(sandbox.reclaim(directory, working_directory=_WORK, timeout=30))
        assert fake.matching("exec") == []

    @pytest.mark.parametrize("directory", [".", "../outside", "../../etc/ssh"])
    def test_a_relative_reclaim_must_stay_below_the_base(self, directory):
        sandbox, fake = self._sandbox()
        with pytest.raises(ValueError):
            asyncio.run(sandbox.reclaim(directory, working_directory=".", timeout=30))
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
                if args[-3:] == ("sh", "-c", "exit 0"):
                    return _DockerResult(0, b"", "")
                if len(args) > 4 and args[4] == "id":
                    return _DockerResult(0, b"20001\n", "")
                raise TimeoutError
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and ".State." in args[2] and args[-1] == _NAME:
                return _DockerResult(0, b"true\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        with pytest.raises(TimeoutError):
            asyncio.run(sandbox.exec(["hang"], working_directory=_WORK, timeout=1))
        assert fake.matching("rm", "-f", _NAME) != []

    def test_a_timeout_preparing_the_base_fails_acquire(self):
        """An unreadable ancestor cannot establish the working-directory postcondition."""

        def responder(args):
            if args[0] == "cp" and args[1].startswith(f"{_NAME}:/maf-sandbox"):
                raise TimeoutError("a daemon too slow to answer the ancestor walk")
            if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
                return _DockerResult(0, b"0:0", "")
            if args[:2] == ("image", "inspect"):
                return _DockerResult(0, b"", "")
            if args[0] == "inspect" and ".State." in args[2] and args[-1] == _NAME:
                return _DockerResult(0, b"true\n", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        with pytest.raises(TimeoutError):
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
            if args[0] == "inspect" and ".State." in args[2] and args[-1] == _NAME:
                return _DockerResult(0, b"true", "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(responder)
        with caplog.at_level(logging.INFO):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("rm", "-f", _NAME) == []
        assert backend._facts == {}
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
            if args[0] == "inspect" and ".State." in args[2] and args[-1] == _NAME:
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
        return asyncio.run(backend.acquire(_KEY, _METHOD_SPEC)), fake

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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        spec = SandboxSpec(requires=frozenset(), kind="e2e", image="img", work_dir="/")
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
        spec = SandboxSpec(requires=frozenset(), kind="e2e", image="img", work_dir=work)
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        asyncio.run(sandbox.write_file("/tmp/run-1/note", "x", working_directory="/"))
        stdin = fake.only("cp", "-").stdin
        assert stdin is not None
        with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
            assert archive.getnames() == ["tmp", "tmp/run-1", "tmp/run-1/note"]

    def test_a_root_image_keeps_the_default_ownership(self):
        """`Config.User` unset means root: the tar entries stay uid 0, as they always were."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        return asyncio.run(backend.acquire(_KEY, _METHOD_SPEC)), fake

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
        absent = _not_in_the_container(f"{_WORK}/x")
        sandbox, _ = self._sandbox_streaming(b"", rc=1, stderr=absent.stderr)
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
    def _sandbox_streaming(self, stream: bytes, *, rc: int = 0):
        overrides = {**_WORK_IS_A_DIRECTORY, ("cp",): _DockerResult(rc, stream, "")}
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        return asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))

    def test_a_regular_file_body_comes_back_byte_identical(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"pixels"
        sandbox = self._sandbox_streaming(_tar_bytes("out.png", payload))
        got = asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=1000))
        assert got == payload

    @pytest.mark.parametrize("path", ["café.txt", "a" * 120 + ".txt"])
    @pytest.mark.parametrize("max_bytes", [7, 1000])
    def test_a_pax_name_stats_and_reads_the_actual_entry(self, path: str, max_bytes: int):
        payload = b"payload"
        sandbox = self._sandbox_streaming(_tar_bytes(path, payload))
        entry = asyncio.run(sandbox.stat_file(path, working_directory=_WORK))
        assert entry is not None and entry.kind is EntryKind.FILE
        assert entry.path == path and entry.size_bytes == len(payload)
        assert (
            asyncio.run(sandbox.read_file(path, working_directory=_WORK, max_bytes=max_bytes))
            == payload
        )

    def test_a_pax_symlink_is_still_refused(self):
        sandbox = self._sandbox_streaming(_symlink_tar("café.txt", "/etc/hostname"))
        entry = asyncio.run(sandbox.stat_file("café.txt", working_directory=_WORK))
        assert entry is not None and entry.kind is EntryKind.SYMLINK
        with pytest.raises(OSError, match="not a regular file"):
            asyncio.run(sandbox.read_file("café.txt", working_directory=_WORK, max_bytes=1000))

    def test_a_pax_file_over_the_cap_is_refused(self):
        sandbox = self._sandbox_streaming(_tar_bytes("café.txt", b"x" * 4000))
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(sandbox.read_file("café.txt", working_directory=_WORK, max_bytes=10))

    def test_an_incomplete_pax_body_is_refused(self):
        stream = _tar_bytes("café.txt", b"x" * 4000)[: 1536 + 1500]
        sandbox = self._sandbox_streaming(stream, rc=1)
        with pytest.raises(RuntimeError, match="incomplete body"):
            asyncio.run(sandbox.read_file("café.txt", working_directory=_WORK, max_bytes=4000))

    @pytest.mark.parametrize("length", [512, 700, 1024, 1300])
    def test_incomplete_pax_metadata_is_refused(self, length: int):
        sandbox = self._sandbox_streaming(_tar_bytes("café.txt", b"x")[:length], rc=1)
        with pytest.raises(RuntimeError, match="incomplete tar metadata"):
            asyncio.run(sandbox.stat_file("café.txt", working_directory=_WORK))

    def test_an_oversized_pax_header_is_refused_before_its_body_is_read(self):
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 65536
        sandbox = self._sandbox_streaming(header.tobuf())
        with pytest.raises(RuntimeError, match="metadata limit"):
            asyncio.run(sandbox.stat_file("out.txt", working_directory=_WORK))

    def test_malformed_pax_records_are_refused(self):
        stream = bytearray(_tar_bytes("café.txt", b"x"))
        stream[512:514] = b"0 "
        sandbox = self._sandbox_streaming(bytes(stream))
        with pytest.raises(RuntimeError, match="malformed PAX"):
            asyncio.run(sandbox.stat_file("café.txt", working_directory=_WORK))

    @pytest.mark.parametrize("field", ["size", "uid", "gid"])
    def test_an_invalid_pax_number_is_refused(self, field: str):
        sandbox = self._sandbox_streaming(_tar_bytes("out", b"x", pax_headers={field: "bad"}))
        with pytest.raises(RuntimeError, match="invalid PAX"):
            asyncio.run(sandbox.stat_file("out", working_directory=_WORK))

    def test_a_pax_size_overrides_the_ustar_size(self):
        stream = _tar_bytes("out", b"payload", pax_headers={"size": "7"})
        header = tarfile.TarInfo("out")
        header.size = 1
        stream = stream[:1024] + header.tobuf() + stream[1536:]
        sandbox = self._sandbox_streaming(stream)
        entry = asyncio.run(sandbox.stat_file("out", working_directory=_WORK))
        assert entry is not None and entry.size_bytes == 7
        assert (
            asyncio.run(sandbox.read_file("out", working_directory=_WORK, max_bytes=7))
            == b"payload"
        )
        with pytest.raises(SandboxTransferCapExceeded):
            asyncio.run(sandbox.read_file("out", working_directory=_WORK, max_bytes=6))

    def test_pax_ownership_reaches_the_ancestor_walk(self):
        sandbox = self._sandbox_streaming(_tar_bytes("out", b"", pax_headers={"uid": "123"}))
        walked = {}
        asyncio.run(sandbox._stat_guest(f"{_WORK}/out", "out", walked))
        assert walked[f"{_WORK}/out"] == (123, 0o644)

    def test_pax_identity_files_are_resolved(self):
        backend, _ = _backend_with(
            _machine(
                overrides={
                    ("cp", f"{_NAME}:/etc/passwd"): _DockerResult(
                        0,
                        _tar_bytes(
                            "passwd", b"app:x:123:456::/:/bin/sh\n", pax_headers={"mtime": "1.5"}
                        ),
                        "",
                    ),
                    ("cp", f"{_NAME}:/etc/group"): _DockerResult(
                        0, _tar_bytes("group", b"app:x:456:\n", pax_headers={"mtime": "1.5"}), ""
                    ),
                }
            )
        )
        assert asyncio.run(backend._passwd_entry(_NAME)) == "app:x:123:456::/:/bin/sh\n"
        assert asyncio.run(backend._group_entry(_NAME)) == {"app": 456}

    def test_a_retry_uses_only_the_new_stream(self):
        streams = iter([_tar_bytes("café.txt", b"old"), _tar_bytes("out", b"new")])
        base = _machine(running=[_NAME], overrides=_WORK_IS_A_DIRECTORY)

        def changing(args):
            if args[:2] == _cp(f"{_WORK}/out"):
                return _DockerResult(0, next(streams), "")
            return base(args)

        backend, _ = _backend_with(changing)
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        assert asyncio.run(sandbox.read_file("out", working_directory=_WORK, max_bytes=3)) == b"new"

    def test_a_pax_stat_never_reads_the_file_body(self):
        stream = _tar_bytes("café.txt", b"x" * 100000)
        sandbox, fake = TestStatFile()._sandbox_streaming(stream)
        asyncio.run(sandbox.stat_file("café.txt", working_directory=_WORK))
        calls = fake.matching(*_cp(f"{_WORK}/café.txt"))
        assert [call.read_limit for call in calls] == [512, 1536]
        assert calls[0].timeout is not None and calls[1].timeout is not None
        assert calls[1].timeout <= calls[0].timeout

    def test_a_pax_directory_in_the_parent_walk_is_served(self):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            _cp(f"{_WORK}/café"): _DockerResult(0, _directory_tar("café"), ""),
            _cp(f"{_WORK}/café/out"): _DockerResult(0, _tar_bytes("out", b"ok"), ""),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        assert (
            asyncio.run(sandbox.read_file("café/out", working_directory=_WORK, max_bytes=2))
            == b"ok"
        )

    def test_gnu_long_names_are_read(self):
        path = "a" * 120
        header = tarfile.TarInfo(path)
        header.size = 2
        sandbox = self._sandbox_streaming(header.tobuf(format=tarfile.GNU_FORMAT) + b"ok")
        assert asyncio.run(sandbox.read_file(path, working_directory=_WORK, max_bytes=2)) == b"ok"

    def test_a_chain_of_metadata_headers_is_bounded(self):
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XGLTYPE
        stream = header.tobuf() * 32 + _tar_bytes("out", b"ok")
        sandbox = self._sandbox_streaming(stream)
        with pytest.raises(RuntimeError, match="32-header limit"):
            asyncio.run(sandbox.stat_file("out", working_directory=_WORK))

    def test_sparse_pax_metadata_is_refused(self):
        sandbox = self._sandbox_streaming(
            _tar_bytes("out", b"x", pax_headers={"GNU.sparse.map": "0,1"})
        )
        with pytest.raises(RuntimeError, match="sparse"):
            asyncio.run(sandbox.stat_file("out", working_directory=_WORK))

    @pytest.mark.parametrize("rc", [0, 1])
    @pytest.mark.parametrize("received", [0, 1500])
    def test_an_incomplete_body_is_refused(self, rc: int, received: int):
        from maf_sandbox_docker._backend import _TAR_BLOCK

        stream = _tar_bytes("out.png", b"x" * 4000)[: _TAR_BLOCK + received]
        sandbox = self._sandbox_streaming(stream, rc=rc)
        with pytest.raises(
            RuntimeError,
            match=f"incomplete body.*expected 4000 bytes, received {received}",
        ):
            asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=4000))

    @pytest.mark.parametrize("max_bytes", [15, 1000])
    def test_a_complete_body_survives_a_nonzero_exit(self, max_bytes: int):
        from maf_sandbox_docker._backend import _TAR_BLOCK

        payload = b"x" * 15
        stream = _tar_bytes("out.png", payload)[: _TAR_BLOCK + len(payload)]
        sandbox = self._sandbox_streaming(stream, rc=1)
        assert (
            asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=max_bytes))
            == payload
        )

    def test_an_empty_file_returns_an_empty_body(self):
        sandbox = self._sandbox_streaming(_tar_bytes("empty.txt", b""))
        assert (
            asyncio.run(sandbox.read_file("empty.txt", working_directory=_WORK, max_bytes=0)) == b""
        )

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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
            _cp(f"{_WORK}/gone"): _not_in_the_container(f"{_WORK}/gone"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        with pytest.raises(FileNotFoundError):
            asyncio.run(sandbox.read_file("gone", working_directory=_WORK, max_bytes=10))


class TestAFailureBorrowingTheAbsenceWords:
    """Absence ends the filesystem path check, so only the engine naming *this* path is one.

    Reading any other failure as absence would end the check over a component nobody looked
    at, handing the read every ancestor unclassified.  The cases below are the two halves of
    the rule and the shapes that reach it: a gone container and an unreachable daemon's socket
    are real failures carrying the words of absence about something that is not a path; the
    last one names the path and is not about absence; and the sharpest is
    ``test_an_absence_about_another_path_is_not_about_this_one``, the engine's own absence
    sentence, with its own phrase, about a path this one merely contains.
    """

    #: Engine 29.7.2, and the reason a missing *container* is not a missing path: it names one.
    _NO_CONTAINER = f"Error response from daemon: No such container: {_NAME}"
    #: A `docker cp` whose daemon is not reachable, verbatim from the Linux client 29.8.0 —
    #: the errno underneath the socket, said about a socket rather than about any guest path.
    _NO_SOCKET = (
        "failed to connect to the docker API at unix:///tmp/nope.sock; check if the path is "
        "correct and if the daemon is running: dial unix /tmp/nope.sock: connect: "
        "no such file or directory"
    )
    #: The other way round: a failure that names the path and says nothing about absence.
    #: Without it, "the message mentions this path" would be the whole test.
    _OPAQUE = f"the daemon gave up on {_WORK}"

    def _sandbox(self, component: str, stderr: str):
        """A machine that answers ``stderr`` for the stat of ``component`` and nothing else."""
        overrides = {**_WORK_IS_A_DIRECTORY, _cp(component): _DockerResult(1, b"", stderr)}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
        fake.mark()
        return sandbox, fake

    @pytest.mark.parametrize(
        "stderr",
        [_NO_CONTAINER, _NO_SOCKET, _OPAQUE],
        ids=["no-container", "socket-error", "names-the-path-but-not-absence"],
    )
    def test_an_ancestor_that_could_not_be_read_raises_rather_than_ending_the_check(self, stderr):
        sandbox, fake = self._sandbox(_WORK, stderr)
        with pytest.raises(RuntimeError, match="could not stat"):
            asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))
        # And it stopped there: the entry's own copy is what the check was standing in front of.
        assert fake.cp_since_mark() == [(*_cp("/maf-sandbox"), "-"), (*_cp(_WORK), "-")]

    #: The two ways a message can hold this component without being about it, one per
    #: boundary.  A child is what the engine really sends while an ancestor is being checked;
    #: a longer path *ending* in this one is the mirror, which docker does not produce and
    #: which is why the leading boundary is otherwise unpinned.
    @pytest.mark.parametrize(
        "named",
        [f"{_WORK}/out.png", f"/srv{_WORK}"],
        ids=["a-child-of-this-one", "a-path-ending-in-this-one"],
    )
    def test_an_absence_about_another_path_is_not_about_this_one(self, named: str):
        """The message has to name the component, not a path that merely contains it."""
        sandbox, _ = self._sandbox(_WORK, _not_in_the_container(named).stderr)
        with pytest.raises(RuntimeError, match="could not stat"):
            asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))

    def test_an_absence_about_a_name_differing_only_in_case_is_a_different_file(self):
        """A guest filesystem is case-sensitive, so `Out.PNG` and `out.png` are two files.

        The engine echoes the spelling it was asked for, so this message cannot arise from
        this copy — but folding the path's case is what would let one file's absence answer
        for the other's, and the answer it would give ends the check.
        """
        named = _not_in_the_container(f"{_WORK}/Out.PNG")
        sandbox, _ = self._sandbox(f"{_WORK}/out.png", named.stderr)
        with pytest.raises(RuntimeError, match="could not stat"):
            asyncio.run(sandbox.stat_file("out.png", working_directory=_WORK))

    def test_a_read_that_failed_is_not_an_output_that_was_never_produced(self):
        """`FileNotFoundError` tells a kind its workload wrote nothing, which is a verdict on
        the guest.  A transport that failed has said nothing about the guest."""
        sandbox, _ = self._sandbox(f"{_WORK}/out.png", self._NO_CONTAINER)
        with pytest.raises(RuntimeError, match="could not read"):
            asyncio.run(sandbox.read_file("out.png", working_directory=_WORK, max_bytes=1000))

    #: Names a caller may legitimately declare, and the engine echoes each one back as given.
    #: A case-folded compare and an escaped one are what keep these absent rather than raising,
    #: and every other path in this suite is lowercase and metacharacter-free.
    @pytest.mark.parametrize(
        "name",
        ["out.png", "Out.PNG", "out(1)[x].png", "a b.txt", "a+b%c.txt"],
        ids=["plain", "mixed-case", "regex-metacharacters", "a-space", "percent-and-plus"],
    )
    def test_the_engine_naming_the_path_is_still_absence(self, name: str):
        """The control: the same phrase, said about the path the copy asked for."""
        absent = _not_in_the_container(f"{_WORK}/{name}")
        sandbox, _ = self._sandbox(f"{_WORK}/{name}", absent.stderr)
        assert asyncio.run(sandbox.stat_file(name, working_directory=_WORK)) is None

    def test_a_removal_against_a_container_that_went_is_a_failure_not_a_missing_file(self):
        """`remove`'s own check reaches the engine, so a gone container fails the removal.

        It wraps the root stat and not the check below it, which is the reachable difference:
        `rm -f` would otherwise be sent to a container that is not there and its failure
        reported as the path's.
        """
        sandbox, _ = self._sandbox("/maf-sandbox", self._NO_CONTAINER)
        with pytest.raises(RuntimeError, match="could not stat"):
            asyncio.run(sandbox.remove("a.txt", working_directory=_WORK))


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
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        }
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        sandbox = asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
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
        facts = asyncio.run(backend._container_facts(name, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
            ("cp", f"{_NAME}:/etc/passwd"): _not_in_the_container("/etc/passwd"),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(1, b"", "id: cannot"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 20001)
        assert fake.matching("exec") == []

    def test_a_named_user_falls_back_to_id_when_passwd_is_unreadable(self):
        """A passwd file that will not come over the wire leaves `id` as the resolver."""
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app\n", ""),
            ("cp", f"{_NAME}:/etc/passwd"): _not_in_the_container("/etc/passwd"),
            ("exec", "-w", "/", _NAME, "id", "-g"): _DockerResult(0, b"20001\n", ""),
            ("exec", "-w", "/", _NAME, "id", "-u"): _DockerResult(0, b"10001\n", ""),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
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
        facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
        assert (facts.guest_uid, facts.guest_gid) == (10001, 30001)
        assert fake.matching("exec") == []
        # `_passwd_entry` swallows every exception, so the pull is asserted on the record
        # rather than through the responder.
        assert fake.matching("cp", f"{_NAME}:/etc/passwd") == []

    def test_an_unresolvable_identity_keeps_fallback_ownership_separate(self, caplog):
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
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert not facts.identity_resolved
        assert any("could not be resolved" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_an_unreadable_user_establishes_no_identity(self, caplog):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): _DockerResult(1, b"", "daemon error"),
        }
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=overrides))
        with caplog.at_level(logging.INFO):
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert not facts.identity_resolved
        assert any("could not be resolved" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_an_unset_user_is_root_without_a_warning(self, caplog):
        """The other side of the same coin: `Config.User` empty *is* the answer, so it must
        not warn — a notice on every stock image would make the real one unreadable.
        """
        backend, _ = _backend_with(_machine(running=[_NAME], overrides=_WORK_IS_A_DIRECTORY))
        with caplog.at_level(logging.INFO):
            facts = asyncio.run(backend._container_facts(_NAME, _SPEC, instance_id="engine-id"))
        assert (facts.guest_uid, facts.guest_gid) == (0, 0)
        assert facts.identity_resolved
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


class TestUnresolvedGuestCapabilities:
    @pytest.mark.parametrize("state", ["running", "stopped", "absent"])
    @pytest.mark.parametrize("capability", [Capability.FILES_OUT, Capability.HOST_TOOLS])
    @pytest.mark.parametrize("user", [b"app", b"root", None])
    def test_acquire_refuses_writing_guest_capabilities(self, state, capability, user):
        overrides = {
            **_WORK_IS_A_DIRECTORY,
            ("inspect", "-f", "{{.Config.User}}"): (
                _DockerResult(0, user, "")
                if user is not None
                else _DockerResult(1, b"", "inspect unavailable")
            ),
        }
        backend, fake = _backend_with(
            _machine(
                running=[_NAME] if state == "running" else [],
                stopped=[_NAME] if state == "stopped" else [],
                overrides=overrides,
            )
        )
        spec = replace(_SPEC, requires=_SPEC.requires | {capability})
        with pytest.raises(SandboxCapabilityNotSupported, match=capability.value) as refused:
            asyncio.run(backend.acquire(_KEY, spec))
        assert "uid:gid" in str(refused.value)
        assert not backend._facts
        assert not fake.matching("cp", "-")
        assert all(call.args[4] == "id" for call in fake.matching("exec"))
        assert asyncio.run(backend.dispose(_KEY, kind=spec.kind)) is None
        assert fake.matching("rm", "-f", _NAME)

    @pytest.mark.parametrize("user", [b"", b"0:0", b"10001:20001", b"10001"])
    def test_resolved_root_and_nonroot_users_keep_writing_capabilities(self, user):
        backend, _ = _backend_with(
            _machine(
                running=[_NAME],
                overrides={("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, user, "")},
            )
        )
        spec = replace(
            _SPEC, requires=_SPEC.requires | {Capability.FILES_OUT, Capability.HOST_TOOLS}
        )
        assert asyncio.run(backend.acquire(_KEY, spec)) is not None

    def test_input_only_workload_keeps_root_owned_files_and_warns(self, caplog):
        backend, fake = _backend_with(
            _machine(
                running=[_NAME],
                overrides={
                    **_WORK_IS_A_DIRECTORY,
                    _cp(f"{_WORK}/input.txt"): _not_in_the_container(f"{_WORK}/input.txt"),
                    ("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app", ""),
                    ("exec", "-w", _WORK, _NAME, "program"): _DockerResult(0, b"result", ""),
                },
            )
        )
        sandbox = asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(sandbox.write_file("input.txt", "input", working_directory=_WORK))
        with tarfile.open(fileobj=io.BytesIO(fake.only("cp", "-").stdin)) as archive:
            assert all((entry.uid, entry.gid) == (0, 0) for entry in archive.getmembers())
        result = asyncio.run(sandbox.exec(["program"], working_directory=_WORK, timeout=10))
        assert result.stdout == "result"
        assert "root-owned inputs" in caplog.text
        assert "FILES_OUT and HOST_TOOLS" in caplog.text

    def test_a_later_writing_spec_cannot_reuse_the_input_only_fallback(self):
        backend, _ = _backend_with(
            _machine(
                running=[_NAME],
                overrides={("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"app", "")},
            )
        )
        asyncio.run(backend.acquire(_KEY, _SPEC))
        spec = replace(
            _SPEC, requires=_SPEC.requires | {Capability.FILES_OUT, Capability.HOST_TOOLS}
        )
        with pytest.raises(SandboxCapabilityNotSupported) as refused:
            asyncio.run(backend.acquire(_KEY, spec))
        assert "files_out, host_tools" in str(refused.value)

    def test_a_transient_identity_failure_is_retried(self):
        backend, fake = _backend_with(
            _machine(
                running=[_NAME],
                overrides={("inspect", "-f", "{{.Config.User}}"): _DockerResult(1, b"", "busy")},
            )
        )
        spec = replace(_SPEC, requires=_SPEC.requires | {Capability.FILES_OUT})
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(_KEY, spec))
        fake._responder = _machine(
            running=[_NAME],
            overrides={("inspect", "-f", "{{.Config.User}}"): _DockerResult(0, b"10001:20001", "")},
        )
        assert asyncio.run(backend.acquire(_KEY, spec)) is not None
        assert len(fake.matching("inspect", "-f", "{{.Config.User}}")) == 2
        assert not fake.matching("run")

    @pytest.mark.parametrize(
        "failure", [OSError("probe unavailable"), TimeoutError("slow inspect")]
    )
    def test_identity_probe_exceptions_do_not_establish_root(self, failure):
        base = _machine(running=[_NAME])

        def respond(args):
            if args[:3] == ("inspect", "-f", "{{.Config.User}}"):
                raise failure
            return base(args)

        backend, _ = _backend_with(respond)
        spec = replace(_SPEC, requires=_SPEC.requires | {Capability.HOST_TOOLS})
        with pytest.raises(SandboxCapabilityNotSupported):
            asyncio.run(backend.acquire(_KEY, spec))
        assert not backend._facts


# ---------------------------------------------------------------------------
# Dispose and purge
# ---------------------------------------------------------------------------


class TestNarrowedDisposal:
    @pytest.mark.parametrize("first", ["kind", "scope"])
    @pytest.mark.parametrize("second", ["kind", "scope"])
    @pytest.mark.parametrize("outcome", ["failure", "cancel"])
    def test_cross_loop_success_preserves_a_newer_retry(self, first, second, outcome, monkeypatch):
        backend, fake = _backend_with(
            _machine(overrides={("ps",): _DockerResult(1, b"", "listing unavailable")})
        )
        key = _KEY
        prefix = (key.scope, key.thread_id, key.agent_dir, key.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        entered, progressed = threading.Event(), threading.Event()
        failure = DisposalFailure("refused", "delete refused")
        attempts = 0

        class _Guard:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    progressed.set()
                    assert self.lock.acquire(timeout=5)

            def __exit__(self, *args):
                self.lock.release()

        class _Ledger(dict):
            armed = True

            def pop(self, at, default=None):
                if self.armed and at == prefix:
                    self.armed = False
                    entered.set()
                    assert progressed.wait(5)
                return super().pop(at, default)

        monkeypatch.setattr(backend, "_disposal_guard", _Guard(), raising=False)
        backend._undeleted = _Ledger()
        original = backend._purge

        async def purge(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Sweep(1)
            if outcome == "cancel":
                raise asyncio.CancelledError
            return _Sweep(0, {"selected": failure})

        monkeypatch.setattr(backend, "_purge", purge)

        async def cleanup(operation):
            if operation == "scope":
                return await backend.dispose_scope(key.scope, key.thread_id)
            return await backend.dispose(key, kind="a")

        def newer_loop():
            assert entered.wait(5)
            backend._registry[(*prefix, "a")] = "selected"
            try:
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        asyncio.run(cleanup(second))
                else:
                    result = asyncio.run(cleanup(second))
                    assert (
                        result.undisposed if isinstance(result, ScopePurge) else result
                    ) is not None
            finally:
                progressed.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            newer = pool.submit(newer_loop)
            asyncio.run(cleanup(first))
            newer.result(timeout=5)

        assert backend._undeleted == {prefix: {"selected"}}
        assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
        monkeypatch.setattr(backend, "_purge", original)
        asyncio.run(backend.dispose(key, kind="a"))
        removed = [call.args[-1] for call in fake.calls if call.args[:2] == ("rm", "-f")]
        assert removed == ["selected", "selected-proxy"]
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not backend._disposal_tokens

    @pytest.mark.parametrize("kind", ["a", None])
    @pytest.mark.parametrize("new_ledger", [False, True])
    def test_concurrent_failure_restores_kind_for_a_narrowed_retry(self, kind, new_ledger):
        from maf_sandbox_docker._backend import _Sweep

        backend, fake = _backend_with(
            _machine(overrides={("ps",): _DockerResult(1, b"", "listing unavailable")})
        )
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        original = backend._purge
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0
        failure = DisposalFailure("refused", "remove refused")

        async def sweep(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                return _Sweep(0, {"selected": failure})
            if attempts == 2:
                return _Sweep(1)
            return _Sweep(0, {"sibling": failure})

        backend._purge = sweep

        async def scenario():
            first = asyncio.create_task(backend.dispose(_KEY, kind=kind))
            await entered.wait()
            assert await backend.dispose(_KEY, kind="a") is None
            assert prefix not in backend._undeleted_kinds
            if new_ledger:
                backend._registry[(*prefix, "b")] = "sibling"
                assert await backend.dispose(_KEY, kind="b") is not None
            release.set()
            assert await first is not None
            backend._purge = original
            await backend.dispose(_KEY, kind="a")

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        removed = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ("rm", "-f") and not call.args[-1].endswith("-proxy")
        ]
        assert removed == ["selected"]
        assert backend._undeleted == ({prefix: {"sibling"}} if new_ledger else {})
        assert backend._undeleted_kinds == ({prefix: {"sibling": "b"}} if new_ledger else {})

    @pytest.mark.parametrize("kind", ["bicep", "x" * 100, "unsafe=kind", "sha256-" + "a" * 48])
    @pytest.mark.parametrize("whole_key", [False, True])
    def test_label_sweep_preserves_siblings_and_matches_creation(self, kind, whole_key):
        from maf_sandbox_docker._backend import _sandbox_labels

        selected = _sandbox_labels(_KEY, SandboxSpec(kind=kind))["maf-sandbox.kind"]
        sibling = _sandbox_labels(_KEY, SandboxSpec(kind="sibling"))["maf-sandbox.kind"]
        labels = {"selected": selected, "sibling": sibling}

        def respond(args):
            if args[:1] == ("ps",):
                filters = [value for value in args if value.startswith("label=maf-sandbox.kind=")]
                names = [
                    name
                    for name, value in labels.items()
                    if not filters or filters == [f"label=maf-sandbox.kind={value}"]
                ]
                payload = ("\n".join(names) + "\n").encode()
                return _DockerResult(0, payload, "")
            return _DockerResult(0, b"", "")

        backend, fake = _backend_with(respond)
        assert not backend._registry
        asyncio.run(backend.dispose(_KEY, kind=None if whole_key else kind))
        removed = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ("rm", "-f") and not call.args[-1].endswith("-proxy")
        ]
        assert set(removed) == ({"selected", "sibling"} if whole_key else {"selected"})

    def test_failed_narrowed_disposal_keeps_its_own_retry_candidates(self):
        overrides = {
            ("ps",): _DockerResult(1, b"", "listing unavailable"),
            ("rm", "-f"): _DockerResult(1, b"", "remove refused"),
        }
        backend, fake = _backend_with(_machine(overrides=overrides))
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        removed = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ("rm", "-f") and not call.args[-1].endswith("-proxy")
        ]
        assert removed == ["selected", "selected"]
        assert backend._registry[(*prefix, "b")] == "sibling"
        asyncio.run(backend.dispose(_KEY))
        removed = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ("rm", "-f") and not call.args[-1].endswith("-proxy")
        ]
        assert set(removed[-2:]) == {"selected", "sibling"}


class TestDispose:
    @pytest.mark.parametrize("silent", [True, False])
    def test_an_absent_container_does_not_count_as_a_removal(self, silent):
        absent = (
            _DockerResult(0, b"", "")
            if silent
            else _DockerResult(1, b"", f"Error: No such container: {_NAME}")
        )
        backend, _ = _backend_with(_machine(overrides={("rm",): absent}))
        removal = asyncio.run(backend._remove(_NAME))
        assert not removal.removed
        assert removal.failure is None

    def test_scope_purge_does_not_count_a_silent_already_absent_removal(self):
        backend, _ = _backend_with(
            _machine(running=[_NAME], overrides={("rm",): _DockerResult(0, b"", "")})
        )
        result = asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert result.disposed == 0
        assert result.undisposed is None

    def test_removes_the_container_by_name(self):
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        asyncio.run(backend.dispose(_KEY))
        assert fake.matching("rm", "-f") != []

    def test_a_narrowed_disposal_asks_the_engine_for_that_kind_only(self):
        """The engine query must include the kind, even when the registry knows the container."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(backend.dispose(_KEY, kind=_SPEC.kind))
        listings = [c for c in fake.calls[fake._marked :] if "ps" in c.args or "ls" in c.args]
        assert listings, [c.args for c in fake.calls[fake._marked :]]
        assert any(f"label=maf-sandbox.kind={_SPEC.kind}" in call.args for call in listings), [
            call.args for call in listings
        ]

    def test_a_whole_key_disposal_still_asks_for_every_kind(self):
        """`kind=None` is what this method always meant, and it must keep meaning it."""
        backend, fake = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        fake.mark()
        asyncio.run(backend.dispose(_KEY))
        listings = [c for c in fake.calls[fake._marked :] if "ps" in c.args or "ls" in c.args]
        assert listings
        assert not any(
            any(str(arg).startswith("label=maf-sandbox.kind=") for arg in call.args)
            for call in listings
        )

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
            (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id): {_NAME}
        }

    def test_a_removal_that_lands_clears_the_retry_record(self):
        backend, _ = _backend_with(_machine(running=[_NAME]))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        backend._undeleted[(_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)] = {_NAME}  # noqa: SLF001

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
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)

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

    def test_a_container_a_failed_removal_left_behind_is_still_served_here(self):
        """Pins what the retry record does rather than what its name suggests: it is disposal
        bookkeeping, and `acquire` still reuses the container, because the name comes from the
        key and the engine is what gets asked. Refusing to serve is the router's ledger."""
        overrides = {("rm",): _DockerResult(1, b"", "daemon error")}
        backend, fake = _backend_with(_machine(running=[_NAME], overrides=overrides))
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert asyncio.run(backend.dispose(_KEY)) is not None

        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        assert backend._undeleted[prefix] == {_NAME}, "the name is owed a retry"  # noqa: SLF001
        asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run") == [], "and the same container is handed back, not replaced"


class TestDisposeScope:
    @pytest.mark.parametrize("retained", [False, True])
    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("unlisted", [False, True])
    def test_scope_purge_retires_confirmed_records(self, retained, partial, unlisted, monkeypatch):
        backend, _ = _backend_with(_machine())
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        failure = DisposalFailure("unknown", "engine unavailable")
        result = _Sweep(0, {"selected": failure, "sibling": failure})

        async def sweep(*args, **kwargs):
            return result

        monkeypatch.setattr(backend, "_purge", sweep)
        if retained:
            assert asyncio.run(backend.dispose(_KEY)) is not None
        result = _Sweep(
            1 if partial else 2,
            {"sibling": failure} if partial else {},
            DisposalFailure("unlisted", "listing unavailable") if unlisted else None,
        )
        answer = asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert answer.disposed == (1 if partial else 2)
        assert (answer.undisposed is not None) is (partial or unlisted)
        assert backend._undeleted == ({prefix: {"sibling"}} if partial else {})
        assert backend._undeleted_kinds == ({prefix: {"sibling": "b"}} if partial else {})
        result = _Sweep(1)
        assert asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id)).undisposed is None
        assert not backend._undeleted and not backend._undeleted_kinds
        assert not getattr(backend, "_disposal_tokens", {})

    @pytest.mark.parametrize("first_scope", [False, True])
    @pytest.mark.parametrize("second_scope", [False, True])
    @pytest.mark.parametrize("failure_first", [False, True])
    def test_overlapping_disposals_preserve_the_newer_failure(
        self, first_scope, second_scope, failure_first, monkeypatch
    ):
        backend, _ = _backend_with(_machine())
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        first_started, second_started = asyncio.Event(), asyncio.Event()
        first_release, second_release = asyncio.Event(), asyncio.Event()
        sweeps = 0
        failure = DisposalFailure("unknown", "engine unavailable")

        async def sweep(*args, **kwargs):
            nonlocal sweeps
            sweeps += 1
            if sweeps == 1:
                first_started.set()
                await first_release.wait()
                return _Sweep(2)
            if sweeps == 2:
                second_started.set()
                await second_release.wait()
                return _Sweep(0, {"selected": failure})
            return _Sweep(1)

        monkeypatch.setattr(backend, "_purge", sweep)

        async def dispose(scope):
            if scope:
                return await backend.dispose_scope(_KEY.scope, _KEY.thread_id)
            return await backend.dispose(_KEY)

        async def scenario():
            first = asyncio.create_task(dispose(first_scope))
            await first_started.wait()
            backend._registry[(*prefix, "a")] = "selected"
            second = asyncio.create_task(dispose(second_scope))
            await second_started.wait()
            if failure_first:
                second_release.set()
                await second
                first_release.set()
                await first
            else:
                first_release.set()
                await first
                second_release.set()
                await second
            assert backend._undeleted == {prefix: {"selected"}}
            assert backend._undeleted_kinds == {prefix: {"selected": "a"}}
            assert await backend.dispose(_KEY, kind="a") is None
            assert not backend._undeleted and not backend._undeleted_kinds
            assert not getattr(backend, "_disposal_tokens", {})

        asyncio.run(scenario())

    @pytest.mark.parametrize("cancel", [False, True])
    def test_failed_scope_purge_retains_kinds_for_a_narrowed_retry(self, cancel, monkeypatch):
        failed = _DockerResult(1, b"", "engine unavailable")
        backend, fake = _backend_with(_machine(overrides={("ps",): failed, ("rm", "-f"): failed}))
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)
        backend._registry[(*prefix, "a")] = "selected"
        backend._registry[(*prefix, "b")] = "sibling"
        original = backend._docker

        async def interrupted(*args, **kwargs):
            if args[:1] == ("ps",):
                raise asyncio.CancelledError
            return await original(*args, **kwargs)

        if cancel:
            monkeypatch.setattr(backend, "_docker", interrupted)
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
            monkeypatch.setattr(backend, "_docker", original)
        else:
            assert (
                asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id)).undisposed
                is not None
            )
        fake.calls.clear()
        assert asyncio.run(backend.dispose(_KEY, kind="a")) is not None
        removed = [
            c.args[-1]
            for c in fake.calls
            if c.args[:2] == ("rm", "-f") and not c.args[-1].endswith("-proxy")
        ]
        assert removed == ["selected"]
        assert backend._undeleted_kinds[prefix] == {"selected": "a", "sibling": "b"}

    def test_an_acquire_that_raises_after_the_run_still_leaves_a_disposable_name(self):
        """The container is running once `run` returns, so every awaited call after it is one
        the acquire can raise on with a container already there.

        The capability read is one of them: it has no fallback, so a timeout there leaves the
        container up and the acquire raising. The labels are the disposal's source of truth
        only while the listing works, and the registry is what covers it when it does not — so
        a name recorded after the facts read would be missing from both.
        """
        base = _machine()

        def a_capability_read_that_hangs(args: tuple[str, ...]) -> _DockerResult:
            if args[:3] == ("inspect", "-f", "{{.HostConfig.CapDrop}}"):
                raise TimeoutError("docker inspect timed out")
            if args[:1] == ("ps",):
                return _DockerResult(1, b"", "daemon is not responding")
            return base(args)

        backend, fake = _backend_with(a_capability_read_that_hangs)
        with pytest.raises(TimeoutError):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert fake.matching("run", "-d", "--name", _NAME) != [], "the container was created"

        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert fake.matching("rm", "-f", _NAME) != []

    def test_a_dispose_landing_mid_purge_neither_crashes_nor_is_clobbered(self):
        """Teardown for one key is not serialized, so the purge reconciles against the live
        record: it must not index a prefix a `dispose` removed, nor drop a name it added."""
        listing = asyncio.Event()
        release = asyncio.Event()
        prefix = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir, _KEY.call_id)

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
                "import sys; sys.stdout.buffer.write(b'\\x89P'); sys.stderr.buffer.write(b'e\\xff'); sys.exit(3)",
            )
        )
        assert result.returncode == 3
        assert result.stdout == b"\x89P"
        assert result.stderr == "e�"
        assert result.stderr_bytes == b"e\xff"

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
_ALLOW_ID = "allow:" + ",".join(sorted(map(str, _ALLOW_SPEC.egress_allow)))
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

    def test_a_name_conflict_is_adopted_rather_than_failing_the_acquire(self):
        """A create that loses the name to a racing acquire of the same key recovers by taking
        what is there. Without it the name stays taken and every acquire for that key fails
        from then on.

        The container appears only once the create has tried and lost, which is the whole of
        the adoption path: one present any earlier is found by the reuse reads and never
        reaches it, so a static fixture tests nothing here.
        """
        base = _machine(networks={_AL_NET: _UNADDRESSED})
        appeared = False

        def racing(args: tuple[str, ...]) -> _DockerResult:
            nonlocal appeared
            if args[:4] == ("run", "-d", "--name", _AL):
                appeared = True
                return _DockerResult(1, b"", "Conflict. The name is already in use")
            if args[0] == "inspect" and args[-1] == _AL:
                if not appeared:
                    return _DockerResult(1, b"", f"error: no such object: {_AL}")
                if args[2] == "{{json .Config.Labels}}":
                    return _DockerResult(
                        0, json.dumps({"maf-sandbox.work-dir.v1": _WORK}).encode(), ""
                    )
                state = b"true\n" if "Running" in args[2] else b"running\n"
                return _DockerResult(0, state, "")
            return base(args)

        backend, _ = _backend_with(racing, _ALLOW_CONFIG)
        sandbox = asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert sandbox.container_name == _AL

    def test_a_created_network_is_read_back_before_the_workload_joins_it(self):
        """A create the engine accepted is not evidence of what it built: a daemon that takes
        an option without acting on it answers exactly as one that applied it.

        This is the first acquire, so there is no earlier network to have been judged — the
        proxy and the workload are about to join what this call just made, and no later read
        looks at it again. The refusal takes the network with it, since it is this call's own.
        """
        base = _machine()
        built: dict[str, str] = {}

        def taking_the_option_without_acting(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "create"):
                built[args[-1]] = _ADDRESSED
                return _DockerResult(0, args[-1].encode() + b"\n", "")
            if args[:2] == ("network", "inspect") and args[-1] in built:
                return _DockerResult(0, built[args[-1]].encode() + b"\n", "")
            if args[:2] == ("network", "rm") and args[-1] in built:
                del built[args[-1]]
                return _DockerResult(0, args[-1].encode() + b"\n", "")
            return base(args)

        backend, fake = _backend_with(taking_the_option_without_acting, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="was created but its bridge holds") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "It has been removed" in str(raised.value)
        assert fake.matching("run", "-d", "--name", _AL) == []
        assert fake.matching("run", "-d", "--name", _AL_PROXY) == []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_a_created_network_that_will_not_go_says_it_is_still_under_its_name(self):
        """`_remove_network` reports failure rather than raising it, and folds "refused" in with
        "was not there", so the state is what says which happened.

        The difference reaches the caller: a network that survived meets the existing-network
        refusal on the next acquire, so "retry" is the one instruction that cannot work.
        """
        base = _machine(overrides={("network", "rm"): _DockerResult(1, b"", "permission denied")})
        created = False

        def taking_the_option_without_acting(args: tuple[str, ...]) -> _DockerResult:
            nonlocal created
            if args[:2] == ("network", "create"):
                created = True
                return _DockerResult(0, args[-1].encode() + b"\n", "")
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET:
                if not created:
                    return _DockerResult(1, b"", f"network {_AL_NET} not found")
                return _DockerResult(0, _ADDRESSED.encode() + b"\n", "")
            return base(args)

        backend, fake = _backend_with(taking_the_option_without_acting, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="was created but its bridge holds") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "still under that name" in str(raised.value)
        assert "It has been removed" not in str(raised.value)
        assert fake.matching("run", "-d", "--name", _AL) == []

    def test_a_cancelled_readback_takes_the_network_it_just_created(self):
        """`_bridge_state` catches `Exception`, so a cancellation passes it by.

        The window is narrow and what sits in it is unreachable afterwards: the network exists,
        no container carries its name yet, and the sweep finds a sandbox's network through its
        container. Nothing would ever collect it.
        """
        base = _machine()
        created = False

        def cancelled_after_the_create(args: tuple[str, ...]) -> _DockerResult:
            nonlocal created
            if args[:2] == ("network", "create"):
                created = True
                return _DockerResult(0, args[-1].encode() + b"\n", "")
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET and created:
                raise asyncio.CancelledError
            return base(args)

        backend, fake = _backend_with(cancelled_after_the_create, config=_ALLOW_CONFIG)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("network", "rm", _AL_NET) != []

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
                return _DockerResult(0, _ADDRESSED.encode() + b"\n", "")
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
        return _machine(running=[_AL], networks={_AL_NET: _ADDRESSED})

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
        assert "it could not be read: daemon is not responding" in str(raised.value)
        assert "its bridge holds" not in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("run", "-d", "--name", _AL) == []

    def test_the_bridge_is_judged_by_the_address_it_has_not_the_mode_it_was_asked_for(self):
        """`.Options` is the request, echoed back whether or not the daemon acted on it, so it
        reads the same for a bridge that ended up addressed and one that did not. Only the
        effect separates them, which is why the template may not ask for the request."""
        backend, fake = _backend_with(self._machine_with_an_addressed_bridge(), _ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        read = fake.matching("network", "inspect")[0].args
        template = read[read.index("-f") + 1]
        assert ".Options" not in template
        assert ".IPAM.Config" in template and ".Driver" in template and ".Internal" in template

    def test_a_bridge_addressed_on_the_second_family_only_is_replaced(self):
        """Two options are asked for, so both families have to be read back.

        A daemon that took one and not the other leaves the bridge addressed on the half a
        first-entry read never looks at — and no live test here reaches it, since CI's daemon is
        single-stack.
        """
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: _ADDRESSED_ON_THE_SECOND_FAMILY}),
            _ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_a_network_that_is_not_a_bridge_is_replaced(self):
        """A gateway mode is a bridge option, so on any other driver it is accepted and means
        nothing — the network is whatever that driver makes it, which is not this backend's to
        describe."""
        macvlan = 'macvlan|true|[{"Subnet":"172.20.0.0/16"}]'
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: macvlan}), _ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_a_network_that_is_not_internal_is_replaced(self):
        """An unaddressed bridge on a network that is not internal still forwards outward, so
        the address is only half of it."""
        outward = 'bridge|false|[{"Subnet":"172.20.0.0/16"}]'
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: outward}), _ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("rm", "-f", _AL) != []
        assert fake.matching("network", "rm", _AL_NET) != []

    def test_a_network_that_will_not_go_away_fails_the_acquire(self):
        """The removals report failure rather than raising it, so reading past them would hand
        back the warm workload on the bridge this was trying to take away."""
        overrides = {("network", "rm"): _DockerResult(1, b"", "network has active endpoints")}
        backend, fake = _backend_with(
            _machine(running=[_AL], networks={_AL_NET: _ADDRESSED}, overrides=overrides),
            _ALLOW_CONFIG,
        )
        with pytest.raises(RuntimeError, match="could not be replaced") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "its bridge holds 172.20.0.1" in str(raised.value)
        # The refusal has to read in the safe direction: the route is what stops the workload
        # being served, not something serving it would require.
        assert "while a route to the host around the proxy may still exist" in str(raised.value)
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

    @pytest.mark.parametrize(
        "stderr",
        [
            'Error response from daemon: cannot remove container "/{name}": container is running',
            _SOCKET_ERROR,
        ],
        ids=["a-decline-naming-the-container", "a-socket-error-borrowing-the-phrase"],
    )
    def test_a_removal_the_engine_declined_is_not_read_as_one_that_worked(self, stderr: str):
        """`rm -f` reports failure rather than raising it, so what counts as "it went" is the
        engine's own "no such object" about *this* container: the phrase and the name together.

        Either half alone admits a case. A decline naming the container carries no absence
        phrase; the socket error carries one, about the socket. The workload is still under its
        name in both.
        """
        overrides = {("rm", "-f", _AL): _DockerResult(1, b"", stderr.format(name=_AL))}
        backend, fake = _backend_with(
            _machine(running=[_AL], overrides=overrides), config=_ALLOW_CONFIG
        )
        with pytest.raises(RuntimeError, match="is still there"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("run", "-d", "--name", _AL) == []

    def test_a_read_that_raises_still_reaches_the_removal(self):
        """`_docker` propagates a timeout rather than returning one, so a read fails by raising
        as well as by answering, and raising refuses too: `exec` detaches, so a warm sandbox
        kept on an unread bridge goes on running whatever earlier calls started."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})

        def timing_out(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET:
                raise TimeoutError("docker network inspect timed out")
            return base(args)

        backend, fake = _backend_with(timing_out, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="could not be read") as raised:
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert "timed out" in str(raised.value)
        assert fake.matching("rm", "-f", _AL) != []

    def test_a_network_swapped_under_its_own_name_is_caught_by_the_second_read(self):
        """A name is not a network. One replaced between the read that kept the warm sandbox
        and the create that finds the name taken is read again there, and refused — so the
        window between the two is not a way to have an addressed bridge accepted."""
        base = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})
        bridge_reads = itertools.count()

        def replaced(args: tuple[str, ...]) -> _DockerResult:
            if args[:2] == ("network", "inspect") and args[-1] == _AL_NET:
                # This backend's while the discard looks at it, someone else's by the create.
                effect = _UNADDRESSED if next(bridge_reads) < 1 else _ADDRESSED
                return _DockerResult(0, effect.encode() + b"\n", "")
            return base(args)

        backend, fake = _backend_with(replaced, config=_ALLOW_CONFIG)
        with pytest.raises(RuntimeError, match="already exists and its bridge holds"):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("run", "-d", "--name", _AL_PROXY) == []

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


class TestTheProxysOwnDecisionsReachARecord:
    """What a spec allowed is on the acquire record; what the guest reached is only here.

    The drain runs on the *acquire* path rather than at disposal alone, because `_ensure_proxy`
    rebuilds the proxy every acquire — one that waited for the disposal would keep the last
    call's decisions and lose every one before it.
    """

    def test_it_parses_each_verb_the_proxy_writes(self):
        decisions, truncated = _egress_decisions(
            "listening on 3128; allowing: example.com\n"
            "ALLOW example.com:443\n"
            "DENY evil.example:443\n"
            "DENY-NONGLOBAL inside.example:443\n"
            "UNREACHABLE gone.example:443\n"
        )
        assert not truncated
        assert [d.decision for d in decisions] == ["ALLOW", "DENY", "DENY-NONGLOBAL", "UNREACHABLE"]
        assert decisions[1].host == "evil.example"
        assert {d.port for d in decisions} == {443}

    def test_the_readiness_line_is_not_a_decision(self):
        """The one line an acquire itself waits for, and it names no target."""
        assert _egress_decisions("listening on 3128; allowing: nothing\n")[0] == ()

    def test_an_ipv6_literal_keeps_its_own_colons(self):
        decisions, _ = _egress_decisions("ALLOW ::1:443\n")
        assert (decisions[0].host, decisions[0].port) == ("::1", 443)

    def test_a_log_past_the_bound_is_cut_to_the_bound_and_says_so(self):
        """The bound is on what the drain hands over, not only on what it asks the engine
        for. Reading one line past it is how the cut is detected; keeping that line would
        hand back the *oldest* decision of an over-long window while claiming the newest."""
        text = "".join(f"ALLOW h{n}.example:443\n" for n in range(_PROXY_LOG_TAIL + 1))
        decisions, truncated = _egress_decisions(text)
        assert truncated is True
        assert len(decisions) == _PROXY_LOG_TAIL
        assert decisions[0].host == "h1.example"  # the oldest went, not the newest

    def test_a_log_exactly_on_the_bound_is_handed_over_whole(self):
        """`truncated` is exact rather than cautious: the read asks for one line past the
        bound, so getting only the bound back proves nothing was cut."""
        text = "".join(f"ALLOW h{n}.example:443\n" for n in range(_PROXY_LOG_TAIL))
        decisions, truncated = _egress_decisions(text)
        assert truncated is False
        assert len(decisions) == _PROXY_LOG_TAIL

    def test_the_declaration_is_made_only_where_something_enforces(self):
        """Without a proxy image there is no allowlist to decide anything, so a `True` here
        would put a watched-looking record on every closed sandbox."""
        assert _backend_with(config=_ALLOW_CONFIG)[0].declarations.observes_egress is True
        assert _backend_with()[0].declarations.observes_egress is False

    def test_what_the_proxy_decided_is_reported_against_the_key(self):
        seen: list[EgressObserved] = []
        backend, _fake = _backend_with(
            _machine(
                overrides={
                    ("logs", "--tail"): _DockerResult(0, b"ALLOW mcr.microsoft.com:443\n", "")
                }
            ),
            config=_ALLOW_CONFIG,
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [(e.key, e.backend) for e in seen] == [(_KEY, "docker")]
        assert seen[0].decisions[0].host == "mcr.microsoft.com"
        assert seen[0].unreadable is None

    def test_a_host_that_collects_nothing_never_pays_for_the_read(self):
        """The reporter is the switch. Without one the drain's `docker logs` is not issued at
        all — the readiness read an acquire already does is a different call."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert fake.matching("logs", "--tail") == []

    def test_a_proxy_that_is_not_there_reports_nothing(self):
        """The ordinary first acquire: no previous proxy, and so no window to account for."""
        seen: list[EgressObserved] = []
        absent = _DockerResult(1, b"", f"Error: No such container: {_AL_PROXY}")
        backend, _fake = _backend_with(
            _machine(overrides={("logs", "--tail"): absent}), config=_ALLOW_CONFIG
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen == []

    def test_a_read_that_failed_is_recorded_rather_than_dropped(self):
        """A window nobody can account for is what an operator most needs to see, so it is a
        field on an event and not a line in a log."""
        seen: list[EgressObserved] = []
        backend, _fake = _backend_with(
            _machine(overrides={("logs", "--tail"): _DockerResult(1, b"", "daemon said no")}),
            config=_ALLOW_CONFIG,
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [e.unreadable for e in seen] == ["daemon said no"]
        assert seen[0].decisions == ()

    def test_a_purge_drains_every_proxy_the_engine_can_attribute(self):
        """`dispose_scope` is the routine cleanup — a thread deletion, a `scope` block closing —
        so a purge that drained nothing lost the last window of every sandbox on the ordinary
        path, while `observes_egress` told a reader the sandbox was watched."""
        seen: list[EgressObserved] = []
        drained = _DockerResult(0, b"DENY evil.example:443", "")
        backend, _fake = _backend_with(
            _machine(overrides={("logs", "--tail"): drained}), config=_ALLOW_CONFIG
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert [d.host for e in seen for d in e.decisions] == ["evil.example"]
        assert [e.key for e in seen] == [_KEY]

    def test_a_disposal_drains_the_proxy_of_an_egress_the_registry_no_longer_names(self):
        """`_container_name` folds the egress identity, so one key and kind served under two
        allowlists has two containers and the registry kept only the later. The earlier one's
        proxy is reached by the label sweep, and its decisions go with it unless drained."""
        seen: list[EgressObserved] = []
        other = replace(_ALLOW_SPEC, egress_allow=("example.invalid",))
        first = _container_name(_KEY, other.kind, "allow:" + ",".join(map(str, other.egress_allow)))
        drained = _DockerResult(0, b"ALLOW example.invalid:443", "")
        backend, _fake = _backend_with(
            _machine(running=[first], overrides={("logs", "--tail"): drained}),
            config=_ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, other))
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert _proxy_name(first) in [c.args[-1] for c in _fake.matching("logs", "--tail")]
        assert len(seen) == 2  # the name the registry kept, and the one it forgot

    def test_the_bound_flag_says_may_have_been_cut_rather_than_was(self):
        """The read is bounded in lines, so a full page back cannot say whether the line past
        the bound was a decision or the readiness marker. The flag therefore means *may be
        short*, and a window that kept every decision can still set it."""
        text = "listening on 3128; allowing: nothing\n" + "".join(
            f"ALLOW h{n}.example:443\n" for n in range(_PROXY_LOG_TAIL)
        )
        decisions, truncated = _egress_decisions(text)
        assert len(decisions) == _PROXY_LOG_TAIL
        assert decisions[0].host == "h0.example"
        assert truncated is True

    def test_an_orphan_proxy_is_drained_even_though_its_workload_went_first(self):
        """A sweep can return a proxy whose workload was removed independently. Filtering the
        proxy out and then removing it deletes exactly the record the drain exists to read."""
        seen: list[EgressObserved] = []
        drained = _DockerResult(0, b"DENY orphan.example:443", "")
        backend, _fake = _backend_with(
            _machine(
                running=[_AL_PROXY],
                overrides={("logs", "--tail"): drained},
            ),
            config=_ALLOW_CONFIG,
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert [d.host for e in seen for d in e.decisions] == ["orphan.example"]

    def test_a_purge_attributes_a_name_the_registry_has_replaced(self):
        """`_container_name` folds the egress identity, so a second allowlist for one key and
        kind replaces the registry entry — and the first container is still swept."""
        seen: list[EgressObserved] = []
        other = replace(_ALLOW_SPEC, egress_allow=("example.invalid",))
        first = _container_name(_KEY, other.kind, "allow:" + ",".join(map(str, other.egress_allow)))
        drained = _DockerResult(0, b"ALLOW example.invalid:443", "")
        backend, _fake = _backend_with(
            _machine(running=[first], overrides={("logs", "--tail"): drained}),
            config=_ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, other))
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        assert _proxy_name(first) in [c.args[-1] for c in _fake.matching("logs", "--tail")]

    def test_the_proxy_is_stopped_before_its_log_is_read(self):
        """A guest sharing the conversation can answer a CONNECT between the read and the
        removal, and that decision would then exist nowhere."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        order = [i for i, c in enumerate(fake.calls) if c.args[:1] in (("stop",),)]
        reads = [i for i, c in enumerate(fake.calls) if c.args[:2] == ("logs", "--tail")]
        assert order and reads
        assert min(order) < min(reads)

    def test_a_proxy_that_would_not_stop_is_reported_as_an_open_window(self):
        """A stop that was refused leaves the proxy answering CONNECTs between the read and the
        removal, so the record must not come back looking clean."""
        seen: list[EgressObserved] = []
        overrides = {
            ("stop",): _DockerResult(1, b"", "daemon refused to stop the container"),
            ("logs", "--tail"): _DockerResult(0, b"ALLOW pypi.org:443", ""),
        }
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [d.host for e in seen for d in e.decisions] == ["pypi.org"]
        assert "could not be stopped" in str(seen[0].unreadable)

    def test_a_proxy_that_is_simply_absent_is_not_an_open_window(self):
        """The ordinary first acquire stops nothing, and that is not a failure to report."""
        seen: list[EgressObserved] = []
        overrides = {
            ("stop",): _DockerResult(1, b"", f"Error: No such container: {_AL_PROXY}"),
            ("logs", "--tail"): _DockerResult(0, b"ALLOW pypi.org:443", ""),
        }
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen[0].unreadable is None

    def test_the_drain_bounds_the_bytes_a_guest_can_make_it_read(self):
        """The proxy copies the guest's CONNECT target into its line, and the header limit it
        reads under lets that target approach 64 KiB — so a line bound alone leaves the guest
        deciding how much the host allocates on a path every acquire waits on."""
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        read = [c for c in fake.calls if c.args[:2] == ("logs", "--tail")]
        assert read and all(c.read_limit == _PROXY_LOG_BYTES for c in read)

    def test_a_read_that_hit_the_byte_cap_says_the_window_may_be_short(self):
        seen: list[EgressObserved] = []
        page = b"ALLOW h.example:443\n" * (_PROXY_LOG_BYTES // 20)
        backend, _fake = _backend_with(
            _machine(overrides={("logs", "--tail"): _DockerResult(0, page, "")}),
            config=_ALLOW_CONFIG,
        )
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen and seen[0].truncated is True

    def test_a_capped_read_is_partial_output_rather_than_a_failed_one(self):
        """A capped read is partial output: the bounded reader kills the child to enforce the
        cap, so the exit code says nothing about the bytes already in hand and the decisions
        in them still count."""
        seen: list[EgressObserved] = []
        page = b"ALLOW h.example:443\n" * (_PROXY_LOG_BYTES // 20)
        overrides = {("logs", "--tail"): _DockerResult(137, page, "killed after the read limit")}
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen and seen[0].decisions
        assert seen[0].truncated is True
        assert seen[0].unreadable is None

    def test_a_capped_read_discards_the_line_the_cap_cut_in_half(self):
        seen: list[EgressObserved] = []
        page = b"ALLOW h.example:443\n" * (_PROXY_LOG_BYTES // 20) + b"ALLOW half.exam"
        overrides = {("logs", "--tail"): _DockerResult(137, page, "")}
        backend, _fake = _backend_with(_machine(overrides=overrides), config=_ALLOW_CONFIG)
        backend.observe_egress(seen.append)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert seen[0].decisions  # not vacuous: the whole lines survived
        assert all(d.host == "h.example" for d in seen[0].decisions)

    def test_absence_without_an_inspected_instance_does_not_invent_a_window(self):
        seen: list[EgressObserved] = []
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        # The proxy goes between the acquire and the teardown, which is what a host reboot or
        # somebody else's `docker rm` looks like from here.
        absent = _DockerResult(1, b"", f"Error: No such container: {_AL_PROXY}")
        fake._responder = _machine(running=[_AL], overrides={("logs", "--tail"): absent})
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert seen == []

    def test_a_closed_sandbox_is_never_reported_as_a_lost_proxy(self):
        """It never had one. Only an allowlisted acquire is tracked, so a closed teardown stays
        silent rather than inventing a window."""
        seen: list[EgressObserved] = []
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert seen == []

    def test_a_second_observed_router_taking_this_backend_over_is_named(self, caplog):
        """The records move to whichever router was built last, including for sandboxes the
        first one served, and a backend cannot tell that from a host rebuilding its router — so
        it says so rather than refusing."""
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        backend.observe_egress(lambda _event: None)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_docker._backend"):
            backend.observe_egress(lambda _event: None)
        assert "moved to a different router" in caplog.text

    def test_taking_the_same_reporter_again_is_not_a_move(self, caplog):
        """A router hands its reporter over once; re-registering the identical callback is not
        the ambiguity the warning is about."""
        backend, _fake = _backend_with(_machine(), config=_ALLOW_CONFIG)

        def report(_event: EgressObserved) -> None:
            return None

        backend.observe_egress(report)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_docker._backend"):
            backend.observe_egress(report)
        assert "moved to a different router" not in caplog.text

    def test_a_replacement_that_never_came_up_leaves_no_claim_of_a_proxy(self):
        """The rebuild removes the old proxy before creating the new one. If the new one never
        starts, an entry left behind claims a proxy is there, and the retry's drain reports a
        window it had already read."""
        seen: list[EgressObserved] = []
        backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))

        warm = _machine(running=[_AL], networks={_AL_NET: _UNADDRESSED})

        def no_new_proxy(args: tuple[str, ...]) -> _DockerResult:
            if args[:1] == ("run",) and _AL_PROXY in args:
                return _DockerResult(1, b"", "could not start the proxy")
            return warm(args)

        fake._responder = no_new_proxy
        backend.observe_egress(seen.append)
        with pytest.raises(RuntimeError):
            asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        assert [e.unreadable for e in seen] == []

    def test_the_last_window_is_drained_at_disposal(self):
        seen: list[EgressObserved] = []
        backend, _fake = _backend_with(
            _machine(
                overrides={("logs", "--tail"): _DockerResult(0, b"DENY evil.example:443\n", "")}
            ),
            config=_ALLOW_CONFIG,
        )
        asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
        backend.observe_egress(seen.append)
        asyncio.run(backend.dispose(_KEY))
        assert [d.host for e in seen for d in e.decisions] == ["evil.example"]


@pytest.mark.parametrize(
    "route", ["acquire", "instance", "dispose", "scope", "orphan", "derived", "network"]
)
@pytest.mark.parametrize("failure", ["refused", "exception", "cancelled"])
@pytest.mark.parametrize("unreadable", [False, True])
def test_proxy_removal_retry_publishes_only_the_successful_window(
    route, failure, unreadable, monkeypatch
):
    backend, fake = _backend_with(_machine(), config=_ALLOW_CONFIG)
    asyncio.run(backend.acquire(_KEY, _ALLOW_SPEC))
    seen: list[EgressObserved] = []
    backend.observe_egress(seen.append)
    failed = True
    removal_landed = False
    base = _machine(
        running=[_AL_PROXY]
        if route == "orphan"
        else [_AL]
        if route == "derived"
        else [_AL, _AL_PROXY],
        networks={_AL_NET: _ADDRESSED if route == "network" else _UNADDRESSED},
    )

    def respond(args):
        nonlocal removal_landed
        if args[:2] == ("logs", "--tail"):
            if unreadable:
                return _DockerResult(1, b"", "engine refused")
            return _DockerResult(0, b"ALLOW example.com:443\n" * (1 if failed else 2), "")
        if args[:1] == ("rm",) and args[-1] in (_AL_PROXY, "proxy-id"):
            assert seen == []
            if failed:
                if failure == "exception":
                    raise RuntimeError("engine unavailable")
                if failure == "cancelled":
                    raise asyncio.CancelledError
                return _DockerResult(1, b"", "engine refused")
            removal_landed = True
        if failed and args[:1] == ("run",) and _AL_PROXY in args:
            return _DockerResult(1, b"", "engine refused")
        return base(args)

    async def inspect(target):
        labels = _sandbox_labels(_KEY, _ALLOW_SPEC)
        if target == _AL_PROXY:
            return {
                "Id": "proxy-id",
                "Name": _AL_PROXY,
                "Labels": {**labels, "maf-sandbox.role": "proxy"},
            }
        return {"Id": "workload-id", "Name": _AL, "Labels": labels}

    monkeypatch.setattr(backend, "_inspect_disposal_target", inspect)
    fake._responder = respond

    async def attempt():
        if route == "acquire":
            await backend._ensure_proxy(_AL, _KEY, _ALLOW_SPEC)
        elif route == "network" and failed:
            await backend._discard_a_sandbox_on_an_unusable_network(_AL, _KEY)
        elif route == "instance":
            await backend.dispose(_KEY, instance_id="workload-id")
        elif route == "dispose":
            await backend.dispose(_KEY)
        else:
            await backend.dispose_scope(_KEY.scope, _KEY.thread_id)

    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(attempt())
    elif route in ("acquire", "network"):
        with pytest.raises(RuntimeError):
            asyncio.run(attempt())
    else:
        asyncio.run(attempt())
    assert seen == []
    failed = False
    if route == "derived":
        base = _machine(running=[_AL_PROXY])
    asyncio.run(attempt())
    assert removal_landed
    assert len(seen) == 1
    assert seen[0].key == _KEY
    assert len(seen[0].decisions) == (0 if unreadable else 2)
    assert bool(seen[0].unreadable) == unreadable


@pytest.mark.parametrize("override", [None, "/image/base"])
def test_relative_working_directory_is_resolved_and_argv_is_opaque(override):
    backend, fake = _backend_with(_machine(running=[_NAME], work_dir=override or _WORK))
    spec = replace(_METHOD_SPEC, work_dir=override)
    base = override if override is not None else _WORK

    async def scenario():
        sandbox = await backend.acquire(_KEY, spec)
        await sandbox.exec(["echo", "/opaque/argument"], working_directory="call", timeout=10)
        with pytest.raises(ValueError):
            await sandbox.exec(["true"], working_directory="../escape", timeout=10)
        await sandbox.write_file("input", b"bytes", working_directory="call")

    asyncio.run(scenario())
    command = fake.matching("exec", "-w")[-1].args
    assert command[2] == f"{base}/call"
    assert command[-2:] == ("echo", "/opaque/argument")
    transfer = fake.matching("cp", "-")[-1]
    with tarfile.open(fileobj=io.BytesIO(transfer.stdin)) as archive:
        assert f"{base.lstrip('/')}/call/input" in archive.getnames()


@pytest.mark.parametrize("override", [None, "/image/base"])
@pytest.mark.parametrize("restart_host", [False, True])
@pytest.mark.parametrize("state", ["warm", "stopped"])
def test_warm_storage_binding_refuses_retargeting(override, restart_host, state):
    base = override or _WORK
    machine = _machine(
        running=[_NAME] if state == "warm" else [],
        stopped=[_NAME] if state == "stopped" else [],
        work_dir=base,
    )
    backend, fake = _backend_with(machine)
    spec = replace(_METHOD_SPEC, work_dir=override)

    async def scenario():
        first = await backend.acquire(_KEY, spec)
        if restart_host:
            current, calls = _backend_with(machine)
        else:
            current, calls = backend, fake
        before = len(calls.calls)
        with pytest.raises(ValueError, match="storage base"):
            await current.acquire(_KEY, replace(spec, work_dir="/other/base"))
        refused = calls.calls[before:]
        assert not any(call.args[0] in {"cp", "exec", "run", "rm"} for call in refused)
        again = await current.acquire(_KEY, spec)
        assert again.instance_id == first.instance_id
        await again.exec(["true"], working_directory=".", timeout=10)
        command = calls.matching("exec", "-w")[-1].args
        assert command[2] == base

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [None, "/image/base with spaces"])
def test_created_storage_binding_is_persisted_in_engine_labels(override):
    backend, fake = _backend_with(_machine())
    asyncio.run(backend.acquire(_KEY, replace(_METHOD_SPEC, work_dir=override)))
    args = fake.only("run").args
    labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
    assert labels["maf-sandbox.work-dir.v1"] == (override or _WORK)


@pytest.mark.parametrize("labels", [None, {}, [], {"maf-sandbox.work-dir.v1": None}])
def test_unrecorded_storage_binding_is_refused_without_disposal(labels):
    backend, fake = _backend_with(
        _machine(
            running=[_NAME],
            overrides={
                ("inspect", "-f", "{{json .Config.Labels}}"): _DockerResult(
                    0, json.dumps(labels).encode(), ""
                )
            },
        )
    )
    with pytest.raises(ValueError, match="storage base"):
        asyncio.run(backend.acquire(_KEY, _METHOD_SPEC))
    assert not fake.matching("rm")


@pytest.mark.parametrize(("running", "allowlisted"), [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize("binding", ["different", "missing", "unreadable"])
def test_storage_binding_precedes_lifecycle_changes(running, allowlisted, binding):
    spec = replace(_ALLOW_SPEC if allowlisted else _METHOD_SPEC, work_dir="/other/base")
    name = _AL if allowlisted else _NAME
    labels = {"maf-sandbox.work-dir.v1": _WORK} if binding == "different" else {}
    metadata = labels
    overrides = {
        ("start",): _DockerResult(1, b"", "start failed"),
        ("inspect", "-f", "{{json .Config.Labels}}"): _DockerResult(
            1 if binding == "unreadable" else 0,
            json.dumps(metadata).encode(),
            "engine unavailable",
        ),
    }
    backend, fake = _backend_with(
        _machine(
            running=[name] if running else [],
            stopped=[] if running else [name],
            overrides=overrides,
        ),
        config=_ALLOW_CONFIG if allowlisted else None,
    )
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(backend.acquire(_KEY, spec))
    assert all(call.args[0] in {"inspect", "ps", "version"} for call in fake.calls)


def test_storage_binding_precedes_adoption_of_a_name_conflict():
    present = False
    spec = replace(_METHOD_SPEC, work_dir="/other/base")
    absent = _machine()
    stopped = _machine(stopped=[_NAME])

    def respond(args):
        nonlocal present
        if args[:1] == ("run",):
            present = True
            return _DockerResult(1, b"", "already in use")
        if args[:1] == ("start",):
            return _DockerResult(1, b"", "start failed")
        if not present and args[:3] == ("inspect", "-f", "{{json .Config.Labels}}"):
            return _DockerResult(1, b"", f"Error: No such container: {_NAME}")
        return (stopped if present else absent)(args)

    backend, fake = _backend_with(respond)
    with pytest.raises(ValueError, match="storage base"):
        asyncio.run(backend.acquire(_KEY, spec))
    assert not any(call.args[0] in {"start", "rm", "remove"} for call in fake.calls)


# ---------------------------------------------------------------------------
# The isolation scope — one sandbox per call, declared and keyed (#436)
# ---------------------------------------------------------------------------

#: What `_container_name` returned for `_KEY` and `_SPEC.kind` before this backend served the
#: call scope, written down rather than recomputed. A conversation-scoped key has to keep
#: mapping to the container it already created, or the release that adds the scope orphans
#: every warm sandbox on the machine and every disposal that derives a name misses it.
_NAME_BEFORE_THE_CALL_SCOPE = "maf-sandbox-docker-6f8a8ed7a21c"

_CALL_A = replace(_KEY, call_id="call-a")
_CALL_B = replace(_KEY, call_id="call-b")


class TestTheIsolationScope:
    """That a key naming a call is a different container, and is disposed on its own."""

    def test_declares_both_scopes(self):
        scopes = DockerSandboxBackend(DockerSandboxConfig()).declarations.isolation_scopes
        assert scopes == frozenset({IsolationScope.CONVERSATION, IsolationScope.CALL})

    def test_a_conversation_key_maps_to_the_container_it_always_did(self):
        """The upgrade path. Pinned to a literal: recomputing the digest here would agree with
        the implementation whatever either one did, and prove nothing about the release before.
        """
        assert _container_name(_KEY, _SPEC.kind) == _NAME_BEFORE_THE_CALL_SCOPE

    def test_a_key_naming_a_call_is_a_different_container(self):
        assert _container_name(_CALL_A, _SPEC.kind) != _container_name(_KEY, _SPEC.kind)

    def test_two_calls_are_two_containers(self):
        """The property itself, at the level the name decides it: get-or-create resolves each
        call to a name no other call produced, so neither is ever handed the other's warm one.
        """
        assert _container_name(_CALL_A, _SPEC.kind) != _container_name(_CALL_B, _SPEC.kind)

    def test_a_call_id_cannot_be_read_as_an_egress_id(self):
        """Both optional parts are appended, so an untagged call id would let a sandbox with an
        allowlist and no call share a name with a call whose id spelled that allowlist.
        """
        egress_only = _container_name(_KEY, _SPEC.kind, "allow:example.com")
        call_only = _container_name(replace(_KEY, call_id="allow:example.com"), _SPEC.kind)
        assert egress_only != call_only

    def test_a_conversation_container_carries_no_call_label(self):
        """Absence is what keeps the label selector reaching containers an earlier release
        created, which carry the four labels this one still writes and nothing more.
        """
        assert "maf-sandbox.call" not in _sandbox_labels(_KEY, _SPEC)

    def test_a_call_scoped_container_is_labelled_with_its_call(self):
        assert _sandbox_labels(_CALL_A, _SPEC)["maf-sandbox.call"] == "call-a"

    def test_the_create_writes_the_call_label(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.acquire(_CALL_A, _SPEC))
        assert "maf-sandbox.call=call-a" in fake.only("run", "-d", "--name").args

    def test_a_call_scoped_disposal_selects_on_the_call(self):
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose(_CALL_A, kind=_SPEC.kind))
        listed = [c.args for c in fake.matching("ps", "-a")]
        assert listed, "the disposal read no listing at all"
        assert all("label=maf-sandbox.call=call-a" in args for args in listed)

    def test_a_conversation_disposal_does_not_filter_on_a_call(self):
        """A conversation's key adds no call filter, so it keeps reaching the containers a
        release before this one labelled with four labels and no fifth.
        """
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose(_KEY, kind=_SPEC.kind))
        listed = [c.args for c in fake.matching("ps", "-a")]
        assert listed, "the disposal read no listing at all"
        assert not any("maf-sandbox.call" in arg for args in listed for arg in args)

    def test_disposing_one_call_leaves_the_other_calls_registry_entry(self):
        """`assert_call_scope_conformance`'s last probe, at the half this process decides.

        Two calls are two registry entries, so ending one selects one of them. The other half
        — that the engine's own listing returns one container for that filter — is the live
        suite's: the fake here answers `ps` from everything it is holding and reads no
        `--filter label=` at all, so an assertion about which container the *engine* removed
        would pass or fail on the fake rather than on this backend.
        """
        backend, fake = _backend_with(_machine())

        async def scenario() -> None:
            await backend.acquire(_CALL_A, _SPEC)
            await backend.acquire(_CALL_B, _SPEC)
            await backend.dispose(_CALL_A, kind=_SPEC.kind)

        asyncio.run(scenario())
        held = {key[3] for key in backend._registry}
        assert held == {"call-b"}

    def test_the_conversation_purge_still_reaches_a_call_scoped_container(self):
        """The documented backstop: a per-call delete that does not land leaves a container no
        later call can address, and `dispose_scope` selects on scope and thread, so it does.
        """
        backend, fake = _backend_with(_machine())

        async def scenario() -> None:
            await backend.acquire(_CALL_A, _SPEC)
            await backend.dispose_scope(_KEY.scope, _KEY.thread_id)

        asyncio.run(scenario())
        removed = {call.args[-1] for call in fake.matching("rm", "-f")}
        assert _container_name(_CALL_A, _SPEC.kind) in removed
        assert not backend._registry

    def test_the_purge_selects_on_scope_and_thread_and_not_on_the_call(self):
        """What makes the backstop reachable: the call is deliberately absent from the filter,
        so one query covers the conversation's own sandbox and every call's alike.
        """
        backend, fake = _backend_with(_machine())
        asyncio.run(backend.dispose_scope(_KEY.scope, _KEY.thread_id))
        listed = [c.args for c in fake.matching("ps", "-a")]
        assert listed, "the purge read no listing at all"
        assert not any("maf-sandbox.call" in arg for args in listed for arg in args)
