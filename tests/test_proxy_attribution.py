"""Docker and WSLC recover proxy attribution from immutable engine metadata."""

import asyncio
import base64
import importlib
import json
import sys
from dataclasses import replace

import pytest
from maf_sandbox import Egress, SandboxKey, SandboxSpec


@pytest.fixture(params=["docker", "wslc"])
def engine(request):
    return _Engine(request.param)


class _Engine:
    def __init__(self, name):
        self.name = name
        self.module = importlib.import_module(f"maf_sandbox_{name}._backend")
        package = importlib.import_module(f"maf_sandbox_{name}")
        prefix = "Docker" if name == "docker" else "Wslc"
        self.backend_type = getattr(package, prefix + "SandboxBackend")
        self.config = getattr(package, prefix + "SandboxConfig")(egress_proxy_image="proxy:test")
        self.result_type = getattr(self.module, "_" + prefix + "Result")
        self.rows = {}
        self.calls = []
        self.generation = 0
        self.connect_error = False

    def backend(self):
        backend = self.backend_type(self.config)
        setattr(backend, "_" + self.name, self.command)
        return backend

    def result(self, stdout=b"", error=""):
        return self.result_type(
            int(bool(error)), stdout, error if self.name == "docker" else error.encode()
        )

    async def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("container", "inspect"):
            row = self.rows.get(args[-1]) or next(
                (r for r in self.rows.values() if r["Name"] == args[-1]), None
            )
            return (
                self.result(json.dumps([row]).encode())
                if row
                else self.result(error=f"No such container: {args[-1]}")
            )
        if args[:2] == ("network", "connect"):
            return self.result(error="connect failed" if self.connect_error else "")
        command = args[1:] if args[0] == "container" else args
        if command[0] == "run":
            self.generation += 1
            instance = f"{self.generation:064x}"
            self.rows[instance] = {
                "Id": instance,
                "Name": command[command.index("--name") + 1],
                "Config": {
                    "Labels": dict(
                        command[i + 1].split("=", 1)
                        for i, arg in enumerate(command)
                        if arg in ("--label", "-l")
                    )
                },
            }
            return self.result(instance.encode())
        if command[0] in ("rm", "remove"):
            for instance, row in list(self.rows.items()):
                if command[-1] in (instance, row["Name"]):
                    del self.rows[instance]
            return self.result()
        if command[0] == "logs":
            return self.result(
                b"ALLOW example.com:443\n" if "--tail" in command else b"listening on 3128\n"
            )
        if command[0] == "ps":
            return self.result("\n".join(r["Name"] for r in self.rows.values()).encode())
        if command[0] == "list":
            return self.result(json.dumps(list(self.rows.values())).encode())
        return self.result()


_KEY = SandboxKey(
    scope="scope / unicode \u2603", thread_id="thread\nwith\tcontrols", agent_dir="agent" * 40
)
_SPEC = SandboxSpec(
    kind="test", image="workload:test", egress=Egress.ALLOWLIST, egress_allow=("example.com",)
)


@pytest.mark.parametrize(
    "key",
    [
        _KEY,
        SandboxKey(scope="", thread_id="", agent_dir=""),
        SandboxKey(scope="scope", thread_id="thread", agent_dir="agent", call_id="call / 1"),
        SandboxKey(scope="sha256-" + "a" * 48, thread_id="quotes\"'\\\x00", agent_dir="a=b"),
    ],
)
def test_lossless_proxy_labels_round_trip_without_changing_selectors(engine, key):
    async def scenario():
        backend = engine.backend()
        await backend._ensure_proxy("workload", key, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        assert engine.module._key_from_labels(labels) == key
        assert labels["maf-sandbox.scope"] == engine.module._label_value(key.scope)
        assert all(c.isalnum() or c in "-_=" for c in labels["maf-sandbox.key.v1"])
        assert "maf-sandbox.key.v1" not in engine.module._sandbox_labels(key, _SPEC)

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["scope", "thread_id", "agent_dir", "call_id"])
@pytest.mark.parametrize(
    "value", ["x" * 150_000, "\u2603" * 1000], ids=["long-ascii", "escaped-unicode"]
)
def test_oversized_attribution_keeps_proxy_creation_within_argument_limits(
    engine, field, value, caplog
):
    key = replace(
        SandboxKey(scope="scope", thread_id="thread", agent_dir="agent"), **{field: value}
    )

    async def scenario():
        backend = engine.backend()

        async def launch(*args, **kwargs):
            if "run" in args:
                process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", *args)
                assert await process.wait() == 0
            return await engine.command(*args, **kwargs)

        setattr(backend, "_" + engine.name, launch)
        await backend._ensure_proxy("workload", key, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        assert labels["maf-sandbox.key.v1"] == ""
        assert engine.module._key_from_labels(labels) is None
        events = []
        backend.observe_egress(events.append)
        event = await backend._drain_the_proxy("workload", key)
        assert event is not None and event.key == key
        assert events == []
        reader = engine.backend()
        reader.observe_egress(events.append)
        await reader.dispose_scope(key.scope, key.thread_id)
        assert events == []
        assert not engine.rows

    asyncio.run(scenario())
    assert "exceeds the 4096-byte attribution limit" in caplog.text


@pytest.mark.parametrize("length,attributable", [(3056, True), (3057, False)])
def test_the_encoded_attribution_budget_is_inclusive(engine, length, attributable):
    key = SandboxKey(scope="x" * length, thread_id="", agent_dir="")
    encoded = engine.module._key_label(key)
    labels = {**engine.module._sandbox_labels(key, _SPEC), "maf-sandbox.key.v1": encoded}
    if attributable:
        assert len(encoded) == 4096
        assert engine.module._key_from_labels(labels) == key
    else:
        assert encoded == ""
        assert engine.module._key_from_labels(labels) is None


@pytest.mark.parametrize(
    "payload", [None, "!", "", "e30=", "WzEsMiwzXQ==", "W10=", "////", "WyJhIiwiYiIsImMiXQ=="]
)
def test_malformed_or_mismatched_metadata_is_not_attributed(engine, payload):
    labels = engine.module._sandbox_labels(_KEY, _SPEC)
    labels["maf-sandbox.key.v1"] = payload
    assert engine.module._key_from_labels(labels) is None


def test_legacy_labels_only_round_trip_when_they_were_not_hashed(engine):
    plain = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    assert engine.module._key_from_labels(engine.module._sandbox_labels(plain, _SPEC)) == plain
    assert engine.module._key_from_labels(engine.module._sandbox_labels(_KEY, _SPEC)) is None
    assert engine.module._key_from_labels({}) is None


@pytest.mark.parametrize("failed_setup", [False, True])
def test_a_fresh_process_drains_an_orphan_including_failed_setup(engine, failed_setup):
    async def scenario():
        creator = engine.backend()
        engine.connect_error = failed_setup
        if failed_setup:
            with pytest.raises(RuntimeError, match="outbound leg"):
                await creator._ensure_proxy("workload", _KEY, _SPEC)
        else:
            await creator._ensure_proxy("workload", _KEY, _SPEC)
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose_scope(_KEY.scope, _KEY.thread_id)
        assert [event.key for event in events] == [_KEY]
        assert events[0].decisions[0].host == "example.com"
        assert not engine.rows

    asyncio.run(scenario())


def test_a_disposal_files_a_leftover_window_under_the_key_that_ran_it(engine):
    """A key addressed to a conversation also sweeps leftovers from the calls inside it.

    That key names no call, so filing the window under it would put the conversation's name on
    decisions a call made.
    """
    conversation = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    call = replace(conversation, call_id="call-1")

    async def scenario():
        await engine.backend()._ensure_proxy("workload", call, _SPEC)
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose(conversation)
        assert [event.key for event in events] == [call]
        assert events[0].decisions[0].host == "example.com"
        assert not engine.rows

    asyncio.run(scenario())


@pytest.mark.parametrize("shape", ["oversized", "legacy"])
def test_the_callers_key_still_answers_for_a_proxy_carrying_no_attribution(engine, shape):
    """The two shapes with nothing to read: an oversized key is written as an empty label, and
    a proxy predating the label carries none, with selectors `_KEY` had hashed.

    A key-addressed disposal is the one caller that can name such a window anyway.
    """

    async def scenario():
        await engine.backend()._ensure_proxy("workload", _KEY, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        if shape == "oversized":
            labels["maf-sandbox.key.v1"] = ""
        else:
            del labels["maf-sandbox.key.v1"]
        assert engine.module._key_from_labels(labels) is None
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose(_KEY)
        assert [event.key for event in events] == [_KEY]
        assert events[0].decisions[0].host == "example.com"
        assert not engine.rows

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "selector", ["maf-sandbox.scope", "maf-sandbox.thread", "maf-sandbox.agent"]
)
@pytest.mark.parametrize("damage", ["mismatched", "missing"], ids=["mismatched", "missing"])
def test_the_callers_key_does_not_answer_for_a_proxy_it_is_not_shown_to_own(
    engine, selector, damage
):
    """A sweep reaches names from its own registry as well as from the label query, and only
    the query proves ownership — so an unreadable key label leaves the selectors to do it.

    Without that, a disposal publishes a window from a container that never said it was this
    conversation's, under this conversation's key.
    """

    async def scenario():
        await engine.backend()._ensure_proxy("workload", _KEY, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        labels["maf-sandbox.key.v1"] = ""
        if damage == "missing":
            del labels[selector]
        else:
            labels[selector] = "somebody-else"
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose(_KEY)
        assert events == []
        assert not engine.rows

    asyncio.run(scenario())


def test_a_conversations_key_does_not_stand_in_for_a_calls_unreadable_proxy(engine):
    """The call label is ownership too, so a conversation's key does not name a call's proxy.

    Without the call in the comparison, the one leftover whose attribution cannot be recovered
    is filed under the conversation — the defect this whole path exists to prevent, surviving
    in its fallback.
    """
    conversation = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    call = replace(conversation, call_id="call-1")

    async def scenario():
        await engine.backend()._ensure_proxy("workload", call, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        assert labels["maf-sandbox.call"] == engine.module._label_value(call.call_id)
        labels["maf-sandbox.key.v1"] = ""
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose(conversation)
        assert events == []
        assert not engine.rows

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ["!", "WyJvdGhlciIsImIiLCJjIiwiZCJd"], ids=["junk", "claims"])
def test_the_callers_key_does_not_answer_for_a_label_that_was_refused(engine, payload):
    """A present label that will not decode, or whose values contradict the selectors, is
    refused deliberately — so the caller's key must not be read as a second opinion on it.

    The sweep would otherwise publish a window under the caller for a container whose own
    account of itself this just declined to believe.
    """

    async def scenario():
        await engine.backend()._ensure_proxy("workload", _KEY, _SPEC)
        labels = next(iter(engine.rows.values()))["Config"]["Labels"]
        labels["maf-sandbox.key.v1"] = payload
        reader = engine.backend()
        events = []
        reader.observe_egress(events.append)
        await reader.dispose(_KEY)
        assert events == []
        assert not engine.rows

    asyncio.run(scenario())


def test_scope_disposal_can_attribute_a_proxy_while_setup_is_waiting(engine):
    async def scenario():
        backend = engine.backend()
        connected, resume = asyncio.Event(), asyncio.Event()

        async def wait_for_ready(proxy):
            connected.set()
            await resume.wait()

        backend._await_listening = wait_for_ready
        setup = asyncio.create_task(backend._ensure_proxy("workload", _KEY, _SPEC))
        await connected.wait()
        events = []
        backend.observe_egress(events.append)
        await backend.dispose_scope(_KEY.scope, _KEY.thread_id)
        resume.set()
        await setup
        assert [event.key for event in events] == [_KEY]
        assert not engine.rows

    asyncio.run(scenario())


def test_an_old_removal_finishing_after_recreation_cannot_erase_attribution(engine):
    async def scenario():
        backend = engine.backend()
        await backend._ensure_proxy("workload", _KEY, _SPEC)
        old_id = next(iter(engine.rows))
        removed, resume = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            result = await engine.command(*args, **kwargs)
            if args[-1] == old_id and ("rm" in args or "remove" in args):
                removed.set()
                await resume.wait()
            return result

        setattr(backend, "_" + engine.name, delayed)
        removal = asyncio.create_task(backend._remove(old_id))
        await removed.wait()
        await backend._ensure_proxy("workload", _KEY, _SPEC)
        new_id = next(iter(engine.rows))
        assert new_id != old_id
        resume.set()
        await removal
        events = []
        backend.observe_egress(events.append)
        await backend.dispose_scope(_KEY.scope, _KEY.thread_id)
        assert [event.key for event in events] == [_KEY]
        assert any(call[-1] == new_id for call in engine.calls if "--tail" in call)

    asyncio.run(scenario())


def test_attribution_and_log_read_use_the_same_generation(engine):
    async def scenario():
        backend = engine.backend()
        await backend._ensure_proxy("workload", _KEY, _SPEC)
        old_id = next(iter(engine.rows))
        events = []
        backend.observe_egress(events.append)

        async def replace_after_inspect(*args, **kwargs):
            result = await engine.command(*args, **kwargs)
            if args == ("container", "inspect", "workload-proxy"):
                engine.rows.clear()
                other = SandboxKey(scope="other", thread_id="other", agent_dir="other")
                await engine.backend()._ensure_proxy("workload", other, _SPEC)
            return result

        setattr(backend, "_" + engine.name, replace_after_inspect)
        event = await backend._drain_attributed_proxy("workload")
        assert events == []
        assert event is not None and event.key == _KEY
        assert all(call[-1] == old_id for call in engine.calls if "--tail" in call)

    asyncio.run(scenario())


def test_an_unobserved_backend_does_not_inspect_for_attribution(engine):
    asyncio.run(engine.backend()._drain_attributed_proxy("workload"))
    assert engine.calls == []


@pytest.mark.parametrize("stand_in", [False, True], ids=["unattributed", "caller-key"])
@pytest.mark.parametrize("failure", ["absent", "unreadable", "malformed", "role", "id", "mismatch"])
def test_unattributable_instances_do_not_emit_or_block_cleanup(engine, failure, stand_in):
    """None of these is a proxy with nothing to say about itself, so a caller's key does not
    rescue any of them: the log is never read, by name or by ID."""

    async def scenario():
        backend = engine.backend()
        await backend._ensure_proxy("workload", _KEY, _SPEC)
        row = next(iter(engine.rows.values()))
        if failure == "malformed":
            row["Config"]["Labels"]["maf-sandbox.key.v1"] = "!"
        elif failure == "role":
            row["Config"]["Labels"]["maf-sandbox.role"] = "workload"
        elif failure == "id":
            row["Id"] = ""
        events = []
        backend.observe_egress(events.append)

        async def inspect(*args, **kwargs):
            if args[:2] == ("container", "inspect"):
                if failure == "unreadable":
                    raise TimeoutError("engine did not answer")
                if failure == "absent":
                    return engine.result(error=f"No such container: {args[-1]}")
                return engine.result(json.dumps([row]).encode())
            return await engine.command(*args, **kwargs)

        setattr(backend, "_" + engine.name, inspect)
        await backend._drain_attributed_proxy(
            "workload",
            "wrong-id" if failure == "mismatch" else None,
            caller_key=_KEY if stand_in else None,
        )
        assert events == []
        assert not any("--tail" in call for call in engine.calls)

    asyncio.run(scenario())


def test_a_confirmed_instance_disappearing_during_drain_reports_its_lost_window(engine):
    async def scenario():
        backend = engine.backend()
        await backend._ensure_proxy("workload", _KEY, _SPEC)
        events = []
        backend.observe_egress(events.append)

        async def disappear(*args, **kwargs):
            if "logs" in args and "--tail" in args:
                return engine.result(error=f"No such container: {args[-1]}")
            return await engine.command(*args, **kwargs)

        setattr(backend, "_" + engine.name, disappear)
        event = await backend._drain_attributed_proxy("workload")
        assert events == []
        assert event is not None and event.key == _KEY
        assert event.unreadable == "the inspected proxy disappeared before its log could be read"

    asyncio.run(scenario())


def test_attribution_inspection_propagates_cancellation(engine):
    backend = engine.backend()
    backend.observe_egress(lambda event: None)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    setattr(backend, "_" + engine.name, cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend._drain_attributed_proxy("workload"))


def test_decode_does_not_accept_a_different_key_with_the_same_safe_prefix(engine):
    labels = engine.module._sandbox_labels(_KEY, _SPEC)
    labels["maf-sandbox.key.v1"] = base64.urlsafe_b64encode(
        json.dumps(["other", _KEY.thread_id, _KEY.agent_dir, _KEY.call_id]).encode()
    ).decode()
    assert engine.module._key_from_labels(labels) is None


def _payload(scope: str, thread: str, agent: str, call: str) -> str:
    """The `maf-sandbox.key.v1` value for four fields, built the way `_key_label` builds it."""
    encoded = json.dumps([scope, thread, agent, call], ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(encoded).decode("ascii")


@pytest.mark.parametrize(
    "label_call, payload_call",
    [
        ("call-b", "call-a"),  # both present, disagreeing
        (None, "call-a"),  # payload names a call the ownership label does not
        ("call-b", ""),  # a label on a record whose payload is conversation-scoped
    ],
    ids=["disagree", "label-absent", "payload-empty"],
)
def test_a_call_that_disagrees_with_its_ownership_label_is_not_attributed(
    engine, label_call, payload_call
):
    """The call is a disposal selector, so a payload that disagrees with it must not recover.

    Left unchecked, a reap reads the payload, believes the container belongs to a call that
    never owned it, and drains and reports that call's egress window from another one's proxy.
    """
    key = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    labels = engine.module._sandbox_labels(key, _SPEC)
    if label_call is not None:
        labels["maf-sandbox.call"] = engine.module._label_value(label_call)
    labels["maf-sandbox.key.v1"] = _payload("scope", "thread", "agent", payload_call)

    assert engine.module._key_from_labels(labels) is None


def test_a_call_that_agrees_with_its_ownership_label_still_recovers(engine):
    """The control: the check above must not refuse the records it exists to admit.

    Both halves — a call-scoped record whose label matches, and a conversation-scoped one that
    carries no call label at all, which is every container created before this backend served
    the scope.
    """
    called = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent", call_id="call-a")
    conversation = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
    for key in (called, conversation):
        labels = engine.module._sandbox_labels(key, _SPEC)
        labels["maf-sandbox.key.v1"] = _payload(
            key.scope, key.thread_id, key.agent_dir, key.call_id
        )
        assert engine.module._key_from_labels(labels) == key
