"""Age-based operator cleanup selects engine identities, independently of process memory."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from maf_sandbox import SandboxKey, SandboxSpec

from maf_sandbox_docker import DockerReapResult, DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _container_name, _DockerResult, _sandbox_labels

_NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
_KEY = SandboxKey(scope="reap-test", thread_id="thread", agent_id="agent")
_SPEC = SandboxSpec(kind="test", image="test:local")
_NAME = _container_name(_KEY, _SPEC.kind)


def _resource(number: int, *, suffix: str = "", age: timedelta = timedelta(days=2)) -> dict:
    labels = _sandbox_labels(_KEY, _SPEC)
    if suffix == "-proxy":
        labels["maf-sandbox.role"] = "proxy"
    return {
        "Id": f"{number:064x}",
        "Name": ("" if suffix == "-net" else "/") + _NAME + suffix,
        "Created": (_NOW - age).isoformat(),
        "Labels": labels,
        "Config": {"Labels": labels},
    }


class _Engine:
    def __init__(self, *resources: dict) -> None:
        self.resources = {r["Id"]: r for r in resources}
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[tuple[str, ...], _DockerResult | Exception] = {}
        self.before_network_list = lambda: None
        self.before_remove = lambda: None
        self.silent_absence = False

    async def __call__(self, *args: str, **kwargs: object) -> _DockerResult:
        self.calls.append(args)
        assert kwargs["timeout"] == 17
        if args[:2] == ("network", "ls"):
            self.before_network_list()
        for prefix, result in self.failures.items():
            if args[: len(prefix)] == prefix:
                if isinstance(result, Exception):
                    raise result
                return result
        if args[0] == "ps" or args[:2] == ("network", "ls"):
            network = args[0] == "network"
            ids = [id for id, r in self.resources.items() if r["Name"].endswith("-net") == network]
            return _DockerResult(0, "\n".join(ids).encode(), "")
        if args[1] == "inspect":
            if args[-1] not in self.resources:
                return _DockerResult(1, b"", f"No such {args[0]}: {args[-1]}")
            if len(args) == 3:
                return _DockerResult(0, json.dumps([self.resources[args[-1]]]).encode(), "")
            if args[3] == "{{.Id}}":
                return _DockerResult(0, args[-1].encode(), "")
            return _DockerResult(0, json.dumps(self.resources[args[-1]]).encode(), "")
        if args[0] == "logs":
            return _DockerResult(0, b"ALLOW example.com:443\n", "")
        if args[:2] == ("rm", "-f") or args[:2] == ("network", "rm"):
            self.before_remove()
            if self.resources.pop(args[-1], None) is None:
                if args[0] == "rm" and self.silent_absence:
                    return _DockerResult(0, b"", "")
                return _DockerResult(1, b"", f"No such container: {args[-1]}")
            return _DockerResult(0, args[-1].encode() + b"\n", "")
        return _DockerResult(0, b"", "")

    @property
    def removals(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "rm" or c[:2] == ("network", "rm")]


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr("maf_sandbox_docker._backend.datetime", Clock)


def _backend(engine: _Engine) -> DockerSandboxBackend:
    backend = DockerSandboxBackend(DockerSandboxConfig(command_timeout_seconds=17))
    backend._docker = engine
    return backend


def test_a_fresh_backend_reaps_old_workloads_proxies_and_stranded_networks():
    workload = _resource(1)
    proxy = _resource(2, suffix="-proxy")
    network = _resource(3, suffix="-net")
    engine = _Engine(workload, proxy, network)
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result == DockerReapResult(disposed=1, proxies_removed=1, networks_removed=1)
    assert engine.resources == {}
    assert engine.removals[-1] == ("network", "rm", network["Id"])
    assert all(len(c[-1]) == 64 for c in engine.removals)
    for listing in (engine.calls[0], next(c for c in engine.calls if c[:2] == ("network", "ls"))):
        assert "--no-trunc" in listing
        for label in ("scope", "thread", "agent", "kind"):
            assert f"label=maf-sandbox.{label}" in listing


@pytest.mark.parametrize("suffix", ["-proxy", "-net"])
def test_infrastructure_without_a_workload_is_reachable(suffix):
    engine = _Engine(_resource(1, suffix=suffix))
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == 0
    assert result.proxies_removed + result.networks_removed == 1
    assert result.failures == ()
    assert engine.resources == {}


@pytest.mark.parametrize("age", [timedelta(hours=1), timedelta(days=1), -timedelta(days=1)])
def test_a_workload_at_or_after_the_cutoff_keeps_the_whole_topology(age):
    resources = [_resource(i, suffix=s) for i, s in enumerate(("", "-proxy", "-net"), 1)]
    next(r for r in resources if r["Name"].endswith(_NAME))["Created"] = (_NOW - age).isoformat()
    engine = _Engine(*resources)
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.removals == []


def test_rebuilding_a_proxy_does_not_extend_the_workloads_maximum_lifetime():
    engine = _Engine(
        _resource(1),
        _resource(2, suffix="-proxy", age=timedelta(0)),
        _resource(3, suffix="-net", age=timedelta(0)),
    )
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult(1, 1, 1)


def test_a_young_orphan_proxy_keeps_its_older_network():
    engine = _Engine(_resource(1, suffix="-proxy", age=timedelta(0)), _resource(2, suffix="-net"))
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.removals == []


def test_creation_time_uses_its_timezone_and_accepts_engine_nanoseconds():
    resource = _resource(1)
    resource["Created"] = "2026-09-07T13:59:59.999999999+02:00"
    engine = _Engine(resource)
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))).disposed == 1


@pytest.mark.parametrize("duration", [timedelta(0), -timedelta(seconds=1)])
def test_nonpositive_ages_are_refused_before_any_engine_call(duration):
    engine = _Engine(_resource(1))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(_backend(engine).reap(duration))
    assert engine.calls == []


@pytest.mark.parametrize(
    "duration",
    [timedelta.max, _NOW - datetime.min.replace(tzinfo=UTC) + timedelta(microseconds=1)],
)
def test_a_lifetime_beyond_the_datetime_range_retains_every_resource(duration):
    ancient = _resource(1)
    ancient["Created"] = datetime.min.replace(tzinfo=UTC).isoformat()
    engine = _Engine(ancient, _resource(2, suffix="-proxy"), _resource(3, suffix="-net"))
    assert asyncio.run(_backend(engine).reap(duration)) == DockerReapResult()
    assert engine.removals == []


def test_scope_is_encoded_and_rechecked_on_both_resource_types():
    scope = "an operator scope with spaces"
    wanted_key = SandboxKey(scope=scope, thread_id="thread", agent_id="agent")
    encoded = _sandbox_labels(wanted_key, _SPEC)
    wanted = _resource(1)
    wanted["Config"]["Labels"] = encoded
    foreign = _resource(2, suffix="-net")
    engine = _Engine(wanted, foreign)
    result = asyncio.run(_backend(engine).reap(timedelta(days=1), scope=scope))
    assert result.disposed == 1
    assert result.networks_removed == 0
    assert list(engine.resources) == [foreign["Id"]]
    for call in engine.calls:
        if call[0] == "ps" or call[:2] == ("network", "ls"):
            assert f"label=maf-sandbox.scope={encoded['maf-sandbox.scope']}" in call


@pytest.mark.parametrize("foreign", ["name", "labels", "role"])
def test_similarly_named_or_labelled_foreign_resources_are_not_removed(foreign):
    resource = _resource(1)
    if foreign == "name":
        resource["Name"] = "/maf-sandbox-wslc-123456789abc"
    elif foreign == "labels":
        resource["Config"]["Labels"].pop("maf-sandbox.agent")
    else:
        resource["Config"]["Labels"]["maf-sandbox.role"] = "proxy"
    engine = _Engine(resource)
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.removals == []


@pytest.mark.parametrize("command", [("ps",), ("network", "ls"), ("container", "inspect")])
@pytest.mark.parametrize("failure", [_DockerResult(1, b"", "daemon down"), TimeoutError("hung")])
def test_incomplete_inventory_never_authorizes_a_partial_delete(command, failure):
    engine = _Engine(_resource(1), _resource(2, suffix="-net"))
    engine.failures[command] = failure
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == 0
    assert result.failures[0].code == "unlisted"
    assert engine.removals == []


@pytest.mark.parametrize(
    "field,value", [("Created", "bad"), ("Created", "2026-09-01"), ("Id", "wrong")]
)
def test_unreadable_metadata_prevents_deletion(field, value):
    resource = _resource(1)
    engine = _Engine(resource)
    bad = {**resource, field: value}
    engine.failures[("container", "inspect")] = _DockerResult(0, json.dumps(bad).encode(), "")
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.failures[0].code == "unlisted"
    assert engine.removals == []


def test_a_replacement_with_the_same_name_survives_the_old_ids_removal():
    old = _resource(1)
    replacement = _resource(2, age=timedelta(0))
    engine = _Engine(old)

    def replace():
        engine.resources.pop(old["Id"], None)
        engine.resources[replacement["Id"]] = replacement

    engine.before_remove = replace
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result == DockerReapResult()
    assert engine.resources == {replacement["Id"]: replacement}


@pytest.mark.parametrize("suffix", ["", "-proxy"])
def test_a_topology_replaced_between_inventories_survives(suffix):
    old = _resource(1, suffix=suffix)
    replacement = [
        _resource(i, suffix=s, age=timedelta(0)) for i, s in enumerate(("", "-proxy", "-net"), 2)
    ]
    engine = _Engine(old, _resource(5, suffix="-net"))

    def replace():
        engine.resources = {r["Id"]: r for r in replacement}

    engine.before_network_list = replace
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.resources == {r["Id"]: r for r in replacement}
    assert engine.removals == []


@pytest.mark.parametrize("workload_present", [True, False])
def test_a_topology_replaced_after_revalidation_survives(workload_present):
    suffixes = ("", "-proxy", "-net") if workload_present else ("-proxy", "-net")
    old = [_resource(i, suffix=s) for i, s in enumerate(suffixes, 1)]
    replacement = [
        _resource(i, suffix=s, age=timedelta(0)) for i, s in enumerate(("", "-proxy", "-net"), 4)
    ]
    engine = _Engine(*old)

    def replace():
        assert any(c[1:4] == ("inspect", "--format", "{{.Id}}") for c in engine.calls)
        engine.resources = {r["Id"]: r for r in replacement}

    engine.before_remove = replace
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.resources == {r["Id"]: r for r in replacement}
    assert len(engine.removals) == 1
    assert engine.removals[0][-1] in {r["Id"] for r in old}


@pytest.mark.parametrize("suffix", ["", "-proxy"])
@pytest.mark.parametrize("silent_absence", [True, False])
def test_a_captured_replacement_network_survives_losing_the_old_anchor(suffix, silent_absence):
    anchor = _resource(1, suffix=suffix)
    old_network = _resource(2, suffix="-net")
    replacement = [
        _resource(i, suffix=s, age=timedelta(0)) for i, s in enumerate(("", "-proxy", "-net"), 3)
    ]
    network = replacement[-1]
    engine = _Engine(anchor, old_network)
    engine.silent_absence = silent_absence

    def replace_network():
        engine.resources.pop(old_network["Id"])
        engine.resources[network["Id"]] = network

    def replace_containers():
        assert ("network", "inspect", "--format", "{{json .}}", network["Id"]) in engine.calls
        assert ("container", "inspect", "--format", "{{.Id}}", anchor["Id"]) in engine.calls
        engine.resources = {r["Id"]: r for r in replacement}

    engine.before_network_list = replace_network
    engine.before_remove = replace_containers
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.resources == {r["Id"]: r for r in replacement}
    assert engine.removals == [("rm", "-f", anchor["Id"])]


@pytest.mark.parametrize(
    "failure",
    [_DockerResult(1, b"", "daemon down"), _DockerResult(0, b"wrong", ""), TimeoutError("hung")],
)
def test_an_unreadable_age_anchor_prevents_all_deletion(failure):
    engine = _Engine(_resource(1), _resource(2, suffix="-net"))

    def fail_revalidation():
        engine.failures[("container", "inspect")] = failure

    engine.before_network_list = fail_revalidation
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == result.networks_removed == 0
    assert result.failures[0].code == "unlisted"
    assert engine.removals == []


def test_failed_removals_are_reported_for_each_resource_and_do_not_stop_the_reap():
    other_network = _resource(4, suffix="-net")
    other_key = SandboxKey(scope="other-scope", thread_id="thread", agent_id="agent")
    other_network["Name"] = _container_name(other_key, _SPEC.kind) + "-net"
    other_network["Labels"] = _sandbox_labels(other_key, _SPEC)
    engine = _Engine(
        _resource(1), _resource(2, suffix="-proxy"), _resource(3, suffix="-net"), other_network
    )
    engine.failures[("rm", "-f", f"{1:064x}")] = _DockerResult(1, b"", "permission denied")
    engine.failures[("network", "rm")] = _DockerResult(1, b"", "active endpoints")
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == 0
    assert result.proxies_removed == 1
    assert len(result.failures) == 2
    assert all(f.code == "refused" for f in result.failures)
    assert ("network", "rm", f"{3:064x}") not in engine.removals
    assert ("network", "rm", other_network["Id"]) in engine.removals


@pytest.mark.parametrize("proxy_listed_first", [True, False])
def test_a_failed_young_proxy_keeps_the_workload_age_for_a_fresh_retry(proxy_listed_first):
    workload = _resource(1)
    proxy = _resource(2, suffix="-proxy", age=timedelta(0))
    network = _resource(3, suffix="-net", age=timedelta(0))
    containers = (proxy, workload) if proxy_listed_first else (workload, proxy)
    engine = _Engine(*containers, network)
    engine.failures[("rm", "-f", proxy["Id"])] = _DockerResult(1, b"", "permission denied")

    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == result.proxies_removed == result.networks_removed == 0
    assert len(result.failures) == 1
    assert result.failures[0].code == "refused"
    assert engine.resources == {r["Id"]: r for r in (*containers, network)}
    engine.failures.clear()
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult(1, 1, 1)
    assert engine.resources == {}


def test_a_failed_young_network_uses_its_own_age_on_a_fresh_sweep():
    network = _resource(3, suffix="-net", age=timedelta(0))
    engine = _Engine(_resource(1), _resource(2, suffix="-proxy", age=timedelta(0)), network)
    engine.failures[("network", "rm")] = _DockerResult(1, b"", "active endpoints")
    result = asyncio.run(_backend(engine).reap(timedelta(days=1)))
    assert result.disposed == result.proxies_removed == 1
    assert result.networks_removed == 0
    assert result.failures[0].code == "refused"
    engine.failures.clear()
    assert asyncio.run(_backend(engine).reap(timedelta(days=1))) == DockerReapResult()
    assert engine.resources == {network["Id"]: network}


@pytest.mark.parametrize("outcome", ["removed", "absent", "refused"])
def test_a_fresh_backend_attributes_a_proxy_and_retries_failed_removal(outcome):
    proxy = _resource(1, suffix="-proxy")
    engine = _Engine(proxy)
    backend = _backend(engine)
    events = []
    backend.observe_egress(events.append)
    if outcome == "absent":
        engine.before_remove = engine.resources.clear
    elif outcome == "refused":
        engine.failures[("rm", "-f")] = _DockerResult(1, b"", "permission denied")

    result = asyncio.run(backend.reap(timedelta(days=1)))
    assert result.proxies_removed == (outcome == "removed")
    assert len(events) == (outcome != "refused")
    if outcome == "refused":
        assert result.failures[0].code == "refused"
        engine.failures.clear()
        assert asyncio.run(backend.reap(timedelta(days=1))).proxies_removed == 1
        assert len(events) == 1
    else:
        assert result.failures == ()
        engine.failures[("logs",)] = _DockerResult(1, b"", f"No such container: {_NAME}-proxy")
        asyncio.run(backend._drain_the_proxy(_NAME, _KEY))
        assert len(events) == 1


def test_a_proxy_drain_uses_the_same_id_as_its_removal():
    proxy = _resource(1, suffix="-proxy")
    engine = _Engine(proxy)
    backend = _backend(engine)
    events = []
    backend.observe_egress(events.append)
    result = asyncio.run(backend.reap(timedelta(days=1)))
    assert result.proxies_removed == 1
    assert len(events) == 1
    assert events[0].key == _KEY
    assert events[0].decisions[0].host == "example.com"
    assert {"stop", "logs", "rm"} <= {call[0] for call in engine.calls}
    for call in engine.calls:
        if call[0] in ("stop", "logs", "rm"):
            assert call[-1] == proxy["Id"]


def test_cancellation_propagates():
    backend = DockerSandboxBackend(DockerSandboxConfig())

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    backend._docker = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend.reap(timedelta(days=1)))
