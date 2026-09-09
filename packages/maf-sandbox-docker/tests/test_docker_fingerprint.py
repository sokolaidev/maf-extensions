"""Confinement measurements refuse incomplete engine views and preserve engine identity."""

import asyncio
import json

import pytest
from maf_sandbox.conformance import ConformanceFailure, assert_nothing_left_behind

from maf_sandbox_docker._backend import _DockerResult, _DockerSandbox
from maf_sandbox_docker.conformance import DockerFingerprintSubject

_ID = "a" * 64
_IMAGE = "sha256:" + "b" * 64


class Engine:
    def __init__(self):
        self.calls = []
        self.os = b"linux"
        self.diff = b""
        self.container = {
            "Id": _ID,
            "State": {"Running": True, "StartedAt": "start"},
            "HostConfig": {"PidMode": "", "Privileged": False},
            "Mounts": [],
            "HostnamePath": f"/engine/containers/{_ID}/hostname",
            "HostsPath": f"/engine/containers/{_ID}/hosts",
            "ResolvConfPath": f"/engine/containers/{_ID}/resolv.conf",
        }
        self.observed = {
            "entries": {"/dev/shm": "directory"},
            "processes": ["1:100"],
            "mounts": "mounts",
            "tmpfs_entries": [],
        }
        self.observer_error = None

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        match args[0]:
            case "version":
                return _DockerResult(0, self.os, "")
            case "inspect":
                value = [self.container]
            case "image":
                value = [{"Id": _IMAGE, "Config": {}}]
            case "diff":
                return _DockerResult(0, self.diff, "")
            case "run":
                if self.observer_error:
                    raise self.observer_error
                value = self.observed
            case "rm":
                return _DockerResult(0, b"", "")
            case _:
                raise AssertionError(args)
        return _DockerResult(0, json.dumps(value).encode(), "")

    def subject(self):
        return DockerFingerprintSubject(
            _DockerSandbox(self, "workload", 30, instance_id="fixture-id"),
            observer_image="trusted-python",
        )


def test_unchanged_baseline_passes_and_observer_is_separate_and_removed():
    engine = Engine()

    async def call():
        pass

    results = asyncio.run(assert_nothing_left_behind(engine.subject(), call))
    assert all(result.passed for result in results)
    commands = [args for args, _ in engine.calls]
    observers = [args for args in commands if args[0] == "run"]
    assert len(observers) == 2
    for args in observers:
        assert f"--pid=container:{_ID}" in args
        assert "--read-only" in args and "--network=none" in args
        assert "--cap-drop=ALL" in args and "--cap-add=SYS_PTRACE" in args
        assert _IMAGE in args and "trusted-python" not in args
        assert json.loads(args[-1]) == {
            "/etc/hostname": f"/{_ID}/hostname",
            "/etc/hosts": f"/{_ID}/hosts",
            "/etc/resolv.conf": f"/{_ID}/resolv.conf",
        }
        name = args[args.index("--name") + 1]
        assert ("rm", "-f", name) in commands
    assert not any(args[0] == "exec" for args in commands)


@pytest.mark.parametrize(
    "source", [None, "", "relative/hosts", "/custom/hosts", f"/engine/{'c' * 64}/hosts"]
)
def test_unverified_network_source_keeps_ctime(source):
    engine = Engine()
    engine.container["HostsPath"] = source
    asyncio.run(engine.subject().fingerprint())
    observer = next(args for args, _ in engine.calls if args[0] == "run")
    assert "/etc/hosts" not in json.loads(observer[-1])


@pytest.mark.parametrize("destination", ["/etc/hosts", "/etc", "/"])
def test_declared_network_mount_keeps_ctime(destination):
    engine = Engine()
    engine.container["Mounts"] = [{"RW": False, "Destination": destination}]
    asyncio.run(engine.subject().fingerprint())
    observer = next(args for args, _ in engine.calls if args[0] == "run")
    assert "/etc/hosts" not in json.loads(observer[-1])


@pytest.mark.parametrize("error", [None, TimeoutError("deadline"), asyncio.CancelledError()])
def test_explicit_cleanup_cannot_overlap_observer_auto_removal(error):
    class RacingEngine(Engine):
        auto_removing = False

        async def __call__(self, *args, **kwargs):
            if args[0] == "run":
                self.auto_removing = "--rm" in args
            if args[0] == "rm" and self.auto_removing:
                return _DockerResult(1, b"", "removal of container is already in progress")
            return await super().__call__(*args, **kwargs)

    engine = RacingEngine()
    engine.observer_error = error
    if error is None:
        assert asyncio.run(engine.subject().fingerprint()) is not None
    else:
        with pytest.raises(type(error)):
            asyncio.run(engine.subject().fingerprint())
    assert any(args[0] == "rm" for args, _ in engine.calls)


@pytest.mark.parametrize("change", ["rootfs", "tmpfs", "process", "replacement", "mounts"])
def test_residue_or_replacement_fails(change):
    engine = Engine()

    async def call():
        if change == "rootfs":
            engine.diff = b"C /tmp\nA /tmp/residue\n"
        elif change == "tmpfs":
            engine.observed["entries"]["/dev/shm/residue"] = "content"
        elif change == "process":
            engine.observed["processes"] = ["1:100", "50:200"]
        elif change == "replacement":
            engine.container["Id"] = "c" * 64
        else:
            engine.observed["mounts"] = "different mounts"

    with pytest.raises(ConformanceFailure):
        asyncio.run(assert_nothing_left_behind(engine.subject(), call))


@pytest.mark.parametrize("dirty", ["rootfs", "tmpfs", "writable_mount", "shared_pid"])
def test_invalid_baseline_never_runs_the_workload(dirty):
    engine = Engine()
    if dirty == "rootfs":
        engine.diff = b"C /tmp\n"
    elif dirty == "tmpfs":
        engine.observed["tmpfs_entries"] = ["/dev/shm/already-there"]
    elif dirty == "writable_mount":
        engine.container["Mounts"] = [{"RW": True, "Destination": "/data"}]
    else:
        engine.container["HostConfig"]["PidMode"] = "host"
    called = False

    async def call():
        nonlocal called
        called = True

    with pytest.raises(ConformanceFailure):
        asyncio.run(assert_nothing_left_behind(engine.subject(), call))
    assert not called


@pytest.mark.parametrize("error", [TimeoutError("deadline"), asyncio.CancelledError()])
def test_timeout_and_cancellation_remove_the_observer(error):
    engine = Engine()
    engine.observer_error = error
    with pytest.raises(type(error)):
        asyncio.run(engine.subject().fingerprint())
    assert engine.calls[-1][0][:2] == ("rm", "-f")


def test_windows_engine_is_explicitly_unsupported_without_posix_commands():
    engine = Engine()
    engine.os = b"windows"
    assert asyncio.run(engine.subject().fingerprint()) is None
    assert len(engine.calls) == 1


def test_unknown_engine_is_an_error():
    engine = Engine()
    engine.os = b""
    with pytest.raises(RuntimeError, match="established Linux"):
        asyncio.run(engine.subject().fingerprint())
