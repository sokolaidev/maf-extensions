"""Engine inventory, ownership, and immutable disposal selectors."""

import asyncio
import json
from dataclasses import replace

import pytest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_instance_disposal_conformance

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _network_name, _sandbox_labels, _WslcResult

KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
SPEC = SandboxSpec(kind="work")


class _Engine:
    def __init__(self):
        self.rows = {}
        self.networks = set()
        self.removed = []
        self.removed_networks = []
        self.failure: str | None = None

    def add(self, identity, name, key=KEY, spec=SPEC, *, network=False):
        self.rows[identity] = {
            "Id": identity,
            "Name": name,
            "Config": {"Labels": _sandbox_labels(key, spec)},
        }
        if network:
            self.networks.add(_network_name(name))

    async def command(self, *args, **kwargs):
        target = args[-1]
        if args[:2] == ("container", "inspect"):
            if self.failure == "inspect":
                return _WslcResult(1, b"", b"permission denied")
            row = next(
                (row for row in self.rows.values() if target in (row["Id"], row["Name"])), None
            )
            if row is None:
                return _WslcResult(1, b"", f"Container '{target}' not found.".encode())
            return _WslcResult(0, json.dumps([row]).encode(), b"")
        if args[:2] == ("container", "remove"):
            self.removed.append(target)
            if self.failure == "delete":
                return _WslcResult(1, b"", b"permission denied")
            self.rows.pop(target, None)
            return _WslcResult(0, target.encode(), b"")
        if args[:2] == ("network", "remove"):
            if any(_network_name(row["Name"]) == target for row in self.rows.values()):
                return _WslcResult(1, b"", b"network has active endpoints")
            if target not in self.networks:
                return _WslcResult(1, b"", f"Network not found: '{target}'".encode())
            self.networks.remove(target)
            self.removed_networks.append(target)
            return _WslcResult(0, target.encode(), b"")
        raise AssertionError(args)


def _backend(engine):
    backend = WslcSandboxBackend(WslcSandboxConfig())
    backend._wslc = engine.command
    return backend


@pytest.mark.parametrize("network", [False, True])
def test_engine_discovery_preserves_same_kind_sibling_and_replacement(network):
    engine = _Engine()
    engine.add("a" * 64, "first", network=network)
    engine.add("b" * 64, "same-kind", network=network)
    engine.add("c" * 64, "other-kind", spec=replace(SPEC, kind="other"), network=network)
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
        assert engine.networks == (
            {_network_name("same-kind"), _network_name("other-kind")} if network else set()
        )
        engine.add("d" * 64, "first", network=network)
        assert await backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64) is None
        assert set(engine.rows) == {"b" * 64, "c" * 64, "d" * 64}
        assert engine.removed == ["a" * 64]
        assert engine.removed_networks == ([_network_name("first")] if network else [])
        if network:
            assert _network_name("first") in engine.networks

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
    engine.add("a" * 64, "first", network=True)
    engine.add("b" * 64, "sibling", network=True)
    backend = _backend(engine)
    engine.failure = failure
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64)) is not None
    assert not engine.removed_networks
    assert _network_name("first") in engine.networks
    engine.failure = None
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="a" * 64)) is None
    assert set(engine.rows) == {"b" * 64}
    assert engine.networks == {_network_name("sibling")}


def test_a_name_or_short_id_is_not_an_instance_selector():
    engine = _Engine()
    engine.add("a" * 64, "first")
    assert asyncio.run(_backend(engine).dispose(KEY, instance_id="first")) is not None
    assert not engine.removed
