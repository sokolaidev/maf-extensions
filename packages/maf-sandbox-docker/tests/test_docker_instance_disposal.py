"""Engine inventory, ownership, and immutable disposal selectors."""

import asyncio
import json
from dataclasses import replace

import pytest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_instance_disposal_conformance

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerResult, _sandbox_labels

KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
SPEC = SandboxSpec(kind="work")


class _Engine:
    def __init__(self):
        self.rows = {}
        self.removed = []
        self.failure: str | None = None

    def add(self, identity, name, key=KEY, spec=SPEC):
        self.rows[identity] = {
            "Id": identity,
            "Name": name,
            "Config": {"Labels": _sandbox_labels(key, spec)},
        }

    async def command(self, *args, **kwargs):
        target = args[-1]
        if args[:2] == ("container", "inspect"):
            if self.failure == "inspect":
                return _DockerResult(1, b"", "permission denied")
            row = next(
                (row for row in self.rows.values() if target in (row["Id"], row["Name"])), None
            )
            if row is None:
                return _DockerResult(1, b"", f"Error: No such container: {target}")
            return _DockerResult(0, json.dumps([row]).encode(), "")
        if args[0] == "rm":
            self.removed.append(target)
            if self.failure == "delete":
                return _DockerResult(1, b"", "permission denied")
            self.rows.pop(target, None)
            return _DockerResult(0, target.encode(), "")
        raise AssertionError(args)


def _backend(engine):
    backend = DockerSandboxBackend(DockerSandboxConfig())
    backend._docker = engine.command
    return backend


def test_engine_discovery_preserves_same_kind_sibling_and_replacement():
    engine = _Engine()
    engine.add("a" * 64, "first")
    engine.add("b" * 64, "same-kind")
    engine.add("c" * 64, "other-kind", spec=replace(SPEC, kind="other"))
    backend = _backend(engine)

    async def exists(identity):
        return identity in engine.rows

    async def scenario():
        await assert_instance_disposal_conformance(
            backend,
            KEY,
            SPEC.kind,
            "a" * 64,
            ["b" * 64, "c" * 64],
            exists,
        )
        engine.add("d" * 64, "first")
        assert await backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64) is None
        assert set(engine.rows) == {"b" * 64, "c" * 64, "d" * 64}
        assert engine.removed == ["a" * 64]

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["scope", "thread_id", "agent_dir", "kind"])
def test_foreign_ownership_never_deletes_an_id(boundary):
    engine = _Engine()
    engine.add("a" * 64, "first")
    backend = _backend(engine)
    key = KEY if boundary == "kind" else replace(KEY, **{boundary: "foreign"})
    kind = "foreign" if boundary == "kind" else SPEC.kind
    assert asyncio.run(backend.dispose(key, kind=kind, instance_id="a" * 64)) is None
    assert not engine.removed


@pytest.mark.parametrize("failure", ["inspect", "delete"])
def test_failed_selection_or_delete_can_retry_without_a_registry(failure):
    engine = _Engine()
    engine.add("a" * 64, "first")
    engine.add("b" * 64, "sibling")
    backend = _backend(engine)
    engine.failure = failure
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64)) is not None
    engine.failure = None
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64)) is None
    assert set(engine.rows) == {"b" * 64}


def test_a_name_or_short_id_is_not_an_instance_selector():
    engine = _Engine()
    engine.add("a" * 64, "first")
    assert asyncio.run(_backend(engine).dispose(KEY, instance_id="first")) is not None
    assert not engine.removed
