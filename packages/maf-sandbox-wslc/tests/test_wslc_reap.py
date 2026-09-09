"""Retention and deletion races through the WSLC command seam; no engine required."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from maf_sandbox import SandboxKey, SandboxSpec

from maf_sandbox_wslc import WslcReapResult, WslcSandboxBackend, WslcSandboxConfig, _reap
from maf_sandbox_wslc._backend import _label_value, _sandbox_labels, _WslcResult

_NOW = datetime(2026, 9, 8, tzinfo=UTC)
_OLD = (_NOW - timedelta(days=2)).isoformat()
_FRESH = (_NOW - timedelta(hours=1)).isoformat()
_PERIOD = timedelta(days=1)
_NAME = "maf-sandbox-wslc-012345abcdef"
_KEY = SandboxKey(scope="app", thread_id="thread", agent_dir="agent")
_SPEC = SandboxSpec(kind="test", image="alpine:3")


@pytest.fixture(autouse=True)
def clock(monkeypatch):

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(_reap, "datetime", Clock)


def _container(*, name=_NAME, id="a" * 64, status="exited", finished=_OLD, created=_OLD):
    labels = _sandbox_labels(_KEY, _SPEC)
    if name.endswith("-proxy"):
        labels["maf-sandbox.role"] = "proxy"
    return {
        "Id": id,
        "Name": name,
        "Created": created,
        "Labels": labels,
        "State": {"Running": status == "running", "Status": status, "FinishedAt": finished},
    }


def _proxy(**kwargs):
    return _container(name=_NAME + "-proxy", id="b" * 64, status="running", **kwargs)


def _network(*, created=_OLD):
    labels = _sandbox_labels(_KEY, _SPEC)
    if created is not None:
        labels[_reap.NETWORK_CREATED_LABEL] = created
    return {"Name": _NAME + "-net", "Id": "c" * 64, "Labels": labels, "Internal": True}


def _json(value, code=0):
    return _WslcResult(code, json.dumps(value).encode(), b"")


class _Engine:
    def __init__(self, containers=(), networks=()):
        self.resources = {
            "container": {row["Id"]: deepcopy(row) for row in containers},
            "network": {row["Id"]: deepcopy(row) for row in networks},
        }
        self.calls = []
        self.removals = []
        self.before: Callable[[tuple[str, ...]], _WslcResult | None] = lambda args: None

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        override = self.before(args)
        if override is not None:
            return override
        resource, operation = args[:2]
        objects = self.resources[resource]
        if operation == "list":
            assert args[2:4] == ("--format", "json")
            if resource == "container":
                assert "-a" in args
                assert "--no-trunc" in args
            return _json([{"Id": row["Id"], "Name": row["Name"]} for row in objects.values()])
        identity = args[-1]
        row = next((row for row in objects.values() if identity in (row["Id"], row["Name"])), None)
        if operation == "inspect":
            if resource == "network":
                assert identity.endswith("-net"), "WSLC 2.9.3 networks only accept names"
            return _json([] if row is None else [row], int(row is None))
        assert operation == "remove"
        self.removals.append(args)
        if row is None:
            return _WslcResult(1, b"", b"missing")
        if resource == "container" and row["State"]["Running"] and ("-f" not in args):
            return _WslcResult(1, b"", b"WSLC_E_CONTAINER_IS_RUNNING")
        if resource == "network" and self.resources["container"]:
            return _WslcResult(1, b"", b"network has active endpoints")
        del objects[row["Id"]]
        return _json(row["Id"])


def _backend(engine):
    backend = WslcSandboxBackend(WslcSandboxConfig())
    backend._wslc = engine
    return backend


def test_stopped_workload_removes_young_infrastructure_without_registry():
    engine = _Engine([_container(), _proxy(created=_FRESH)], [_network(created=_FRESH)])
    backend = _backend(engine)
    assert not backend._registry
    assert asyncio.run(backend.reap(_PERIOD)) == WslcReapResult(1, 1, 1)
    assert engine.removals == [
        ("container", "remove", "a" * 64),
        ("container", "remove", "-f", "b" * 64),
        ("network", "remove", _NAME + "-net"),
    ]
    assert asyncio.run(backend.reap(_PERIOD)) == WslcReapResult()


@pytest.mark.parametrize("status", ["running", "restarting", "paused", "dead", "unknown"])
def test_running_and_unknown_workloads_protect_their_whole_group(status):
    engine = _Engine([_container(status=status), _proxy()], [_network()])
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


@pytest.mark.parametrize("finished", [_FRESH, (_NOW - _PERIOD).isoformat(), _NOW.isoformat()])
def test_recent_stop_and_exact_cutoff_are_retained(finished):
    engine = _Engine([_container(finished=finished), _proxy()], [_network()])
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


@pytest.mark.parametrize("status", ["created", "exited"])
def test_created_age_only_applies_to_never_started_workloads(status):
    engine = _Engine([_container(status=status, finished=_FRESH)])
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == (1 if status == "created" else 0)


@pytest.mark.parametrize(
    "timestamp", [None, 0, True, "", "2026-09-01", "bad", "0001-01-01T00:00:00Z"]
)
def test_missing_or_malformed_stop_time_retains_group_and_reports_failure(timestamp):
    engine = _Engine([_container(finished=timestamp), _proxy()], [_network()])
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert len(result.failures) == 1
    assert result.failures[0].code == "unlisted"
    assert not engine.removals


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-09-06T01:00:00.123456789+01:00", True),
        ("2026-09-07T01:00:00+01:00", False),
        ("2026-09-06T23:59:59.999999999Z", True),
        ("2026-09-07T00:00:00.000000001Z", False),
    ],
)
def test_timezone_and_fractional_stop_time(timestamp, expected):
    engine = _Engine([_container(finished=timestamp)])
    assert asyncio.run(_backend(engine).reap(_PERIOD)).disposed == int(expected)


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(seconds=-1)])
def test_invalid_retention_never_queries_engine(duration):
    engine = _Engine()
    with pytest.raises(ValueError, match="positive timedelta"):
        asyncio.run(_backend(engine).reap(duration))
    assert not engine.calls


def test_maximum_duration_preserves_everything():
    engine = _Engine([_container()])
    assert asyncio.run(_backend(engine).reap(timedelta.max)) == WslcReapResult()


@pytest.mark.parametrize("partial", ["proxy", "network", "proxy-network"])
def test_partial_infrastructure_without_a_workload(partial):
    engine = _Engine(
        [_proxy()] if "proxy" in partial else [], [_network()] if "network" in partial else []
    )
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result == WslcReapResult(0, int("proxy" in partial), int("network" in partial))


def test_young_orphan_proxy_protects_older_network():
    engine = _Engine([_proxy(created=_FRESH)], [_network()])
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()


def test_legacy_network_alone_has_no_age_but_workload_can_authorize_its_cleanup():
    engine = _Engine([], [_network(created=None)])
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert len(result.failures) == 1
    assert result.failures[0].code == "unlisted"
    assert not engine.removals
    engine.resources["container"]["a" * 64] = _container()
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult(1, 0, 1)


@pytest.mark.parametrize("mutation", ["scope", "labels", "name", "role", "internal"])
def test_unrelated_resources_survive(mutation):
    workload, proxy, network = (_container(), _proxy(), _network())
    for row in (workload, proxy, network):
        if mutation == "scope":
            row["Labels"]["maf-sandbox.scope"] = "another-app"
        elif mutation == "labels":
            del row["Labels"]["maf-sandbox.kind"]
        elif mutation == "name":
            row["Name"] = "unrelated-" + row["Name"]
        elif mutation == "role":
            row["Labels"]["maf-sandbox.role"] = "other"
        else:
            network["Internal"] = False
    engine = _Engine([workload, proxy] if mutation != "internal" else [], [network])
    assert asyncio.run(_backend(engine).reap(_PERIOD, scope="app")) == WslcReapResult()
    assert not engine.removals


def test_scope_uses_the_same_encoding_as_creation():
    scope = "scope with spaces"
    workload = _container()
    workload["Labels"]["maf-sandbox.scope"] = _label_value(scope)
    assert asyncio.run(_backend(_Engine([workload])).reap(_PERIOD, scope=scope)).disposed == 1


def test_mixed_ownership_cannot_borrow_another_workloads_expiry():
    proxy = _proxy()
    proxy["Labels"]["maf-sandbox.thread"] = "other-thread"
    engine = _Engine([_container(), proxy], [_network()])
    assert asyncio.run(_backend(engine).reap(_PERIOD)).failures
    assert not engine.removals


@pytest.mark.parametrize("payload", [b"{}", b"broken", b"[null]", b'[{"Id":"abc"}]'])
def test_incomplete_network_inventory_prevents_all_deletion(payload):
    engine = _Engine([_container()])
    engine.before = lambda args: (
        _WslcResult(0, payload, b"") if args[:2] == ("network", "list") else None
    )
    assert asyncio.run(_backend(engine).reap(_PERIOD)).failures
    assert not engine.removals


@pytest.mark.parametrize("restart", ["running", "exited"])
def test_revalidation_observes_restart_and_new_stop_interval(restart):
    engine = _Engine([_container(), _proxy()], [_network()])
    reads = 0

    def change(args):
        nonlocal reads
        if args == ("container", "inspect", "a" * 64):
            reads += 1
            if reads == 2:
                engine.resources["container"]["a" * 64] = _container(
                    status=restart, finished=_FRESH
                )

    engine.before = change
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


def test_restart_after_inspection_cannot_force_delete_the_workload_or_its_proxy():
    engine = _Engine([_container(), _proxy()], [_network()])

    def restart(args):
        if args == ("container", "remove", "a" * 64):
            engine.resources["container"]["a" * 64]["State"]["Running"] = True

    engine.before = restart
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == result.proxies_removed == result.networks_removed == 0
    assert len(result.failures) == 1
    assert len(engine.removals) == 1


@pytest.mark.parametrize("replace", [False, True])
def test_disappeared_workload_does_not_authorize_infrastructure_removal(replace):
    engine = _Engine([_container(), _proxy()], [_network()])

    def disappear(args):
        if args == ("container", "remove", "a" * 64):
            engine.resources["container"].pop("a" * 64)
            if replace:
                engine.resources["container"]["d" * 64] = _container(id="d" * 64)

    engine.before = disappear
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert len(engine.removals) == 1


def test_new_workload_protects_an_orphan_group():
    engine = _Engine([_proxy()], [_network()])

    def appear(args):
        if args == ("container", "inspect", _NAME):
            engine.resources["container"]["a" * 64] = _container()

    engine.before = appear
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


def test_replaced_network_name_is_retained():
    engine = _Engine([], [_network()])
    reads = 0

    def replace(args):
        nonlocal reads
        if args == ("network", "inspect", _NAME + "-net"):
            reads += 1
            if reads == 2:
                network = engine.resources["network"].pop("c" * 64)
                network["Id"] = "d" * 64
                engine.resources["network"]["d" * 64] = network

    engine.before = replace
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


@pytest.mark.parametrize("resource", ["container", "network"])
@pytest.mark.parametrize("exception", [None, OSError, TimeoutError])
def test_removal_failures_are_reported_and_can_be_retried(resource, exception):
    engine = _Engine([_container()] if resource == "container" else [], [_network()])

    def fail(args):
        if args[:2] == (resource, "remove"):
            if exception is not None:
                raise exception("command failed")
            return _WslcResult(1, b"", b"permission denied")
        return None

    engine.before = fail
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert len(result.failures) == 1
    expected = (
        "refused"
        if exception is None
        else "timeout"
        if exception is TimeoutError
        else "unreachable"
    )
    assert result.failures[0].code == expected
    assert result.disposed == result.networks_removed == 0
    engine.before = lambda args: None
    assert not asyncio.run(_backend(engine).reap(_PERIOD)).failures


def test_proxy_failure_is_reported_and_preserves_network():
    engine = _Engine([_container(), _proxy()], [_network()])
    engine.before = lambda args: (
        _WslcResult(1, b"", b"refused") if args == ("container", "remove", "-f", "b" * 64) else None
    )
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == 1
    assert len(result.failures) == 1
    assert engine.resources["network"]


@pytest.mark.parametrize(
    ("resource", "identity", "suffix"),
    [("container", "b" * 64, "-proxy"), ("network", _NAME + "-net", "-net")],
)
@pytest.mark.parametrize(
    ("operation", "failure", "code"),
    [
        ("inspect", OSError, "unreachable"),
        ("inspect", "malformed", "unlisted"),
        ("remove", OSError, "unreachable"),
        ("remove", TimeoutError, "timeout"),
    ],
)
def test_late_infrastructure_failure_names_the_current_resource(
    resource, identity, suffix, operation, failure, code
):
    engine = _Engine([_container(), _proxy()], [_network()])

    def fail(args):
        if (
            "a" * 64 not in engine.resources["container"]
            and args[:2] == (resource, operation)
            and args[-1] == identity
        ):
            if failure == "malformed":
                return _WslcResult(0, b"not json", b"")
            raise failure("command failed")
        return None

    engine.before = fail
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == 1
    assert result.proxies_removed == int(resource == "network")
    assert result.networks_removed == 0
    assert len(result.failures) == 1
    assert result.failures[0].code == code
    assert result.failures[0].detail.startswith(_NAME + suffix + ": ")
    assert engine.resources["network"]
    assert ("b" * 64 in engine.resources["container"]) is (resource == "container")


def test_early_failure_names_the_new_groups_anchor():
    name = "maf-sandbox-wslc-111111111111"
    engine = _Engine([_container(), _container(name=name, id="d" * 64)])

    def fail(args):
        if "a" * 64 not in engine.resources["container"] and args == (
            "container",
            "inspect",
            "d" * 64,
        ):
            raise OSError("command failed")
        return None

    engine.before = fail
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == 1
    assert len(result.failures) == 1
    assert result.failures[0].detail.startswith(name + ": ")
    assert "d" * 64 in engine.resources["container"]


def test_egress_drain_uses_inspected_proxy_id_after_workload_removal(monkeypatch):
    engine = _Engine([_container(), _proxy()], [_network()])
    backend = _backend(engine)
    backend._acquired[_NAME] = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)
    drains = []

    async def drain(name, key, *, proxy_id):
        assert "a" * 64 not in engine.resources["container"]
        drains.append((name, key, proxy_id))

    monkeypatch.setattr(backend, "_drain_the_proxy", drain)
    assert asyncio.run(backend.reap(_PERIOD)) == WslcReapResult(1, 1, 1)
    assert drains == [(_NAME, _KEY, "b" * 64)]
    assert not backend._acquired


@pytest.mark.parametrize("failure", ["refused", "exception", "cancelled"])
def test_reap_reports_only_after_proxy_removal_succeeds(failure):
    engine = _Engine([_container(), _proxy()], [_network()])
    backend = _backend(engine)
    backend._acquired[_NAME] = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)
    events = []
    backend.observe_egress(events.append)
    failed = True

    def respond(args):
        if args[:2] == ("container", "stop"):
            return _WslcResult(0, b"", b"")
        if args[:2] == ("container", "logs"):
            return _WslcResult(0, b"ALLOW example.com:443\n", b"")
        if args[:2] == ("container", "remove") and args[-1] == "b" * 64:
            assert events == []
            if failed:
                if failure == "exception":
                    raise RuntimeError("engine unavailable")
                if failure == "cancelled":
                    raise asyncio.CancelledError
                return _WslcResult(1, b"", b"engine refused")
        return None

    engine.before = respond
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(backend.reap(_PERIOD))
    else:
        assert asyncio.run(backend.reap(_PERIOD)).failures
    assert events == []
    assert _NAME in backend._acquired
    failed = False
    assert asyncio.run(backend.reap(_PERIOD)).proxies_removed == 1
    assert len(events) == 1
    assert events[0].decisions[0].host == "example.com"
    assert not backend._acquired


def test_network_creation_writes_a_persistent_timestamp(monkeypatch):
    backend = WslcSandboxBackend(WslcSandboxConfig())
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        return _WslcResult(0, b"", b"")

    monkeypatch.setattr(backend, "_wslc", command)
    asyncio.run(backend._ensure_network(_NAME + "-net", _KEY, _SPEC))
    label = next(arg for arg in calls[0] if arg.startswith(_reap.NETWORK_CREATED_LABEL + "="))
    assert _reap._timestamp(label.split("=", 1)[1]).tzinfo == UTC


def test_new_cli_json_lines_and_truncated_network_listing_ids():
    engine = _Engine([_container(), _proxy()], [_network()])

    def listing(args):
        if args[1] == "list":
            rows = []
            for row in engine.resources[args[0]].values():
                if args[0] == "container":
                    rows.append({"ID": row["Id"], "Names": row["Name"]})
                else:
                    rows.append({"ID": row["Id"][:12], "Name": row["Name"]})
            return _WslcResult(0, "\n".join(json.dumps(row) for row in rows).encode(), b"")
        return None

    engine.before = listing
    backend = _backend(engine)
    assert asyncio.run(backend.reap(_PERIOD)) == WslcReapResult(1, 1, 1)
    assert asyncio.run(backend.reap(_PERIOD)) == WslcReapResult()


def test_inspection_failure_prevents_all_deletion():
    engine = _Engine([_container(), _proxy()], [_network()])
    engine.before = lambda args: (
        _WslcResult(1, b"", b"engine unavailable")
        if args == ("container", "inspect", "b" * 64)
        else None
    )
    assert asyncio.run(_backend(engine).reap(_PERIOD)).failures
    assert not engine.removals


def test_unrelated_workload_with_the_same_name_protects_orphan_infrastructure():
    workload = _container()
    workload["Labels"] = {}
    engine = _Engine([workload, _proxy()], [_network()])
    assert asyncio.run(_backend(engine).reap(_PERIOD)) == WslcReapResult()
    assert not engine.removals


def test_attached_network_is_never_forced_away():
    unrelated = _container(name="unrelated", status="running")
    engine = _Engine([unrelated], [_network()])
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.networks_removed == 0
    assert len(result.failures) == 1
    assert engine.removals == [("network", "remove", _NAME + "-net")]


def test_bad_group_does_not_prevent_cleanup_of_independent_workloads():
    engine = _Engine(
        [_container(finished="bad"), _container(name="maf-sandbox-wslc-111111111111", id="d" * 64)]
    )
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert result.disposed == 1
    assert len(result.failures) == 1
    assert "a" * 64 in engine.resources["container"]


@pytest.mark.parametrize("resource", ["container", "network"])
@pytest.mark.parametrize("operation", ["list", "inspect"])
@pytest.mark.parametrize("failure", ["response", "malformed", OSError, TimeoutError])
def test_inventory_failure_codes_preserve_the_source(resource, operation, failure):
    engine = _Engine([_container()], [_network()])

    def fail(args):
        if args[:2] == (resource, operation):
            if failure in (OSError, TimeoutError):
                raise failure("command failed")
            if failure == "response":
                return _WslcResult(1, b"", b"query refused")
            return _WslcResult(0, b"not json", b"")
        return None

    engine.before = fail
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    expected = (
        "unreachable"
        if failure is OSError
        else "timeout"
        if failure is TimeoutError
        else "unlisted"
    )
    assert {item.code for item in result.failures} == {expected}
    assert not engine.removals


@pytest.mark.parametrize("failure", ["response", "metadata"])
def test_revalidation_failure_is_unlisted(failure):
    engine = _Engine([_container()], [_network()])
    reads = 0

    def fail(args):
        nonlocal reads
        if args == ("container", "inspect", "a" * 64):
            reads += 1
            if reads == 2:
                if failure == "response":
                    return _WslcResult(1, b"", b"query refused")
                engine.resources["container"]["a" * 64]["State"]["FinishedAt"] = "bad"
        return None

    engine.before = fail
    result = asyncio.run(_backend(engine).reap(_PERIOD))
    assert len(result.failures) == 1
    assert result.failures[0].code == "unlisted"
    assert result.failures[0].detail.startswith(_NAME + ": ")
    assert not engine.removals


@pytest.mark.parametrize(
    ("workload", "stage"),
    [(True, "target"), (True, "remove"), (False, "anchor"), (False, "target"), (False, "remove")],
)
@pytest.mark.parametrize("replace", [False, True])
def test_proxy_absence_forgets_attribution_and_continues_unless_replaced(workload, stage, replace):
    engine = _Engine(
        [_container(), _proxy()] if workload else [_proxy()], [_network(created=_FRESH)]
    )
    backend = _backend(engine)
    backend._acquired[_NAME] = (_KEY.scope, _KEY.thread_id, _KEY.agent_dir)
    reads = 0

    def disappear(args):
        nonlocal reads
        if args == ("container", "inspect", "b" * 64):
            reads += 1
            trigger = reads == (2 if workload or stage == "anchor" else 3)
        else:
            trigger = args == ("container", "remove", "-f", "b" * 64)
        if trigger and ((stage == "remove") == (args[1] == "remove")):
            engine.resources["container"].pop("b" * 64, None)
            if replace:
                replacement = _proxy(created=_FRESH)
                replacement["Id"] = "d" * 64
                engine.resources["container"]["d" * 64] = replacement
        return None

    engine.before = disappear
    result = asyncio.run(backend.reap(_PERIOD))
    assert result == WslcReapResult(int(workload), 0, int(not replace))
    assert (_NAME in backend._acquired) is replace
    assert bool(engine.resources["network"]) is replace
    if replace:
        assert "d" * 64 in engine.resources["container"]
