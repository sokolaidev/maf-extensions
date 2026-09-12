"""Daemon selection is fixed before any lifecycle or file operation reaches the client."""

from __future__ import annotations

import asyncio
import json

import pytest

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerResult, _Freezes


class _Client:
    def __init__(self, name="remote", endpoint="tcp://engine-a:2376"):
        self.context = {"Name": name, "Endpoints": {"docker": {"Host": endpoint}}}
        self.calls = []
        self.deleted = False

    async def spawn(self, *args, env, **kwargs):
        self.calls.append((args, dict(env)))
        if args[1:3] == ("context", "inspect"):
            result = _DockerResult(0, json.dumps(self.context).encode(), "")
        elif self.deleted:
            result = _DockerResult(1, b"", "context not found")
        else:
            result = _DockerResult(0, b"linux", "")

        class Process:
            returncode = result.returncode

            async def communicate(self, stdin):
                return result.stdout, result.stderr.encode()

        return Process()


@pytest.mark.parametrize("name", ["remote", "default"])
def test_every_operation_keeps_the_context_endpoint_and_tls_environment(monkeypatch, name):
    client = _Client(name)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)
    original = {
        "DOCKER_HOST": "tcp://engine-a:2376",
        "DOCKER_CONTEXT": name,
        "DOCKER_CONFIG": "/client-config-a",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": "/client-certs-a",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)

    async def scenario():
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        client.context = _Client("another", "tcp://engine-b:2375").context
        for key in original:
            monkeypatch.setenv(key, "changed")
        for command in ("inspect", "pause", "cp", "unpause", "exec", "rm", "ps", "logs"):
            await backend._docker(command, "target")

    asyncio.run(scenario())
    assert client.calls[0][0] == ("docker", "context", "inspect", "--format", "{{json .}}")
    assert len(client.calls) == 10
    for args, env in client.calls[1:]:
        assert args[1:3] == ("--context", name)
        assert {key: env[key] for key in original} == original


def test_default_endpoint_is_retained_even_when_no_host_was_set(monkeypatch):
    client = _Client("default", "unix:///var/run/docker.sock")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)
    asyncio.run(DockerSandboxBackend.create(DockerSandboxConfig()))
    assert client.calls[-1][1]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert client.calls[-1][0][1:3] == ("--context", "default")


@pytest.mark.parametrize(
    "context",
    [
        None,
        [],
        {},
        {"Name": "remote"},
        {"Name": "", "Endpoints": {"docker": {"Host": "tcp://engine:2376"}}},
        {"Name": "remote", "Endpoints": {"docker": {"Host": ""}}},
    ],
)
def test_unresolved_context_refuses_create_before_contacting_a_daemon(monkeypatch, context):
    client = _Client()
    client.context = context
    monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)
    with pytest.raises(RuntimeError, match="resolve a context and endpoint"):
        asyncio.run(DockerSandboxBackend.create(DockerSandboxConfig()))
    assert len(client.calls) == 1


@pytest.mark.parametrize("failure", [RuntimeError("missing client"), TimeoutError("slow client")])
def test_resolution_failure_refuses_create_and_releases_the_binding_lock(monkeypatch, failure):
    backend = DockerSandboxBackend(DockerSandboxConfig())

    async def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(backend, "_invoke", fail)
    with pytest.raises(type(failure), match=str(failure)):
        asyncio.run(backend._bind_daemon())
    assert backend._endpoint is None
    assert not backend._binding_lock.locked()


def test_deleted_context_does_not_fall_back_to_the_current_one(monkeypatch):
    client = _Client()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)

    async def scenario():
        backend = await DockerSandboxBackend.create(DockerSandboxConfig())
        client.deleted = True
        result = await backend._docker("rm", "same-name")
        assert result.returncode == 1

    asyncio.run(scenario())
    assert client.calls[-1][0] == ("docker", "--context", "remote", "rm", "same-name")
    assert len(client.calls) == 3


def test_plain_constructor_binds_once_under_concurrent_first_use(monkeypatch):
    client = _Client()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)
    backend = DockerSandboxBackend(DockerSandboxConfig())
    assert not client.calls

    async def scenario():
        await asyncio.gather(*(backend._docker("inspect", "same-name") for _ in range(8)))

    asyncio.run(scenario())
    assert len(client.calls) == 9
    assert all(args[1:3] == ("--context", "remote") for args, _ in client.calls[1:])


def test_freeze_records_share_an_endpoint_but_never_cross_daemons(monkeypatch):
    async def scenario():
        client = _Client()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", client.spawn)
        first = await DockerSandboxBackend.create(DockerSandboxConfig())
        client.context["Name"] = "alias"
        alias = await DockerSandboxBackend.create(DockerSandboxConfig())
        client.context = _Client("other", "tcp://engine-b:2376").context
        other = await DockerSandboxBackend.create(DockerSandboxConfig())
        async with first._frozen("same-name"):
            assert _Freezes.claimed(alias._freeze_key("same-name"))
            assert not _Freezes.claimed(other._freeze_key("same-name"))
            diagnostic = _DockerResult(
                1,
                b"",
                "Error response from daemon: Container same-name "
                "is paused, unpause the container before exec",
            )
            attempts = []

            async def forged(*args, **kwargs):
                attempts.append(args)
                return diagnostic

            monkeypatch.setattr(other, "_invoke", forged)
            assert (
                await other._docker("exec", "same-name", "say", container="same-name") == diagnostic
            )
            assert len(attempts) == 1

    asyncio.run(scenario())
