"""Whole-session storage ordering, stream settlement, and host lease contracts."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from agent_framework import (
    AgentMiddleware,
    AgentSession,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    InMemoryHistoryProvider,
    Message,
    ResponseStream,
)
from test_session import Model, Summarizer, build_stack, harness

from maf_compaction import SessionCompactionRegistry, SessionPersistence

pytestmark = pytest.mark.anyio


class Store:
    def __init__(self):
        self.data = {}
        self.locks = {}
        self.events = []
        self.load_error = False
        self.save_error = False

    @asynccontextmanager
    async def lease(self, key):
        async with self.locks.setdefault(key, asyncio.Lock()):
            self.events.append((key, "lock"))
            try:
                yield
            finally:
                self.events.append((key, "unlock"))

    async def load(self, key):
        self.events.append((key, "load"))
        if self.load_error:
            raise OSError("load failed")
        return self.data.get(key)

    async def save(self, key, data):
        self.events.append((key, "save"))
        if self.save_error:
            raise OSError("save failed")
        self.data[key] = json.loads(json.dumps(data))

    def adapter(self, registry=None):
        return SessionPersistence(
            registry or SessionCompactionRegistry(build_stack, max_cached_sessions=0),
            load=self.load,
            save=self.save,
            lease=self.lease,
        )


async def test_load_and_save_whole_session_inside_lease():
    store = Store()
    original = AgentSession(session_id="a", service_session_id="service-a")
    original.state["host"] = {"items": [1]}
    store.data["a"] = original.to_dict()

    async def call(session):
        assert session.service_session_id == "service-a"
        assert "maf_compaction" in session.state
        session.state["host"]["items"].append(2)
        assert store.data["a"]["state"]["host"]["items"] == [1]
        store.events.append(("a", "request"))
        return "answer"

    assert await store.adapter().run("a", call) == "answer"
    assert store.data["a"]["state"]["host"]["items"] == [1, 2]
    assert store.events == [("a", e) for e in ["lock", "load", "request", "save", "unlock"]]


@pytest.mark.parametrize("failure", ["request", "cancel", "serialize", "load", "save"])
async def test_failures_propagate_without_replacing_saved_snapshot(failure):
    store = Store()
    original = AgentSession(session_id="a")
    original.state["host"] = {"value": "original"}
    store.data["a"] = original.to_dict()
    before = deepcopy(store.data)
    store.load_error = failure == "load"
    store.save_error = failure == "save"

    async def call(session):
        session.state["host"]["value"] = "changed"
        if failure == "request":
            raise RuntimeError("request failed")
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "serialize":
            session.state["host"]["bad"] = float("nan")

    error = {
        "request": RuntimeError,
        "cancel": asyncio.CancelledError,
        "serialize": ValueError,
        "load": OSError,
        "save": OSError,
    }[failure]
    with pytest.raises(error):
        await store.adapter().run("a", call)
    assert store.data == before
    assert store.events[-1] == ("a", "unlock")


@pytest.mark.parametrize("case", ["missing", "wrong_id", "missing_state", "wrong_type", "schema"])
async def test_bad_snapshots_never_reach_request_or_get_replaced(case):
    store = Store()
    snapshot = AgentSession(session_id="a").to_dict()
    if case == "wrong_id":
        snapshot["session_id"] = "b"
    elif case == "missing_state":
        del snapshot["state"]
    elif case == "wrong_type":
        snapshot["type"] = "other"
    elif case == "schema":
        snapshot["state"]["maf_compaction"] = {"version": 100, "kind": "composed"}
    if case != "missing":
        store.data["a"] = snapshot
    before = deepcopy(store.data)

    async def call(session):
        pytest.fail("Invalid state reached the agent")

    with pytest.raises(KeyError if case == "missing" else ValueError):
        await store.adapter().run("a", call, create=case != "missing")
    assert store.data == before
    assert ("a", "save") not in store.events


@pytest.mark.parametrize("key", ["", None, 12])
async def test_invalid_session_id_is_rejected_before_storage(key):
    store = Store()
    with pytest.raises(ValueError):
        await store.adapter().run(key, lambda session: asyncio.sleep(0))
    assert not store.events


@pytest.mark.parametrize("consume", ["iterate", "final"])
async def test_stream_saves_after_final_validation_and_cleanup(consume):
    store = Store()

    def call(session):
        async def updates():
            try:
                yield "one"
                yield "two"
            finally:
                store.events.append(("a", "cleanup"))

        async def finalize(updates):
            session.state["provider"] = "finished"
            store.events.append(("a", "final"))
            return "".join(updates)

        return ResponseStream(updates(), finalizer=finalize)

    async with store.adapter().stream("a", call, create=True) as response:
        assert ("a", "save") not in store.events
        if consume == "iterate":
            assert [part async for part in response] == ["one", "two"]
        else:
            assert await response.get_final_response() == "onetwo"
        assert ("a", "save") not in store.events
    assert store.events.index(("a", "cleanup")) < store.events.index(("a", "save"))
    assert store.events.index(("a", "final")) < store.events.index(("a", "save"))
    assert store.events[-2:] == [("a", "save"), ("a", "unlock")]
    assert store.data["a"]["state"]["provider"] == "finished"
    with pytest.raises(RuntimeError):
        await response.get_final_response()
    with pytest.raises(StopAsyncIteration):
        await anext(response)


@pytest.mark.parametrize("exit_kind", ["break", "close", "unused", "consumer_error", "cancel"])
async def test_abandoned_stream_never_saves(exit_kind):
    store = Store()
    closed = []
    constructed = []

    def call(session):
        constructed.append(True)

        async def updates():
            try:
                session.state["partial"] = "unsafe"
                yield "first"
                yield "second"
            finally:
                closed.append(True)

        return ResponseStream(updates(), finalizer=lambda parts: parts)

    async def consume():
        async with store.adapter().stream("a", call, create=True) as response:
            if exit_kind == "unused":
                return
            assert await anext(response) == "first"
            if exit_kind == "close":
                await response.close()
                await response.close()
            elif exit_kind == "consumer_error":
                raise RuntimeError("consumer failed")
            elif exit_kind == "cancel":
                raise asyncio.CancelledError

    if exit_kind in ("consumer_error", "cancel"):
        with pytest.raises(
            RuntimeError if exit_kind == "consumer_error" else asyncio.CancelledError
        ):
            await consume()
    else:
        await consume()
    assert closed == ([] if exit_kind == "unused" else [True])
    assert constructed == ([] if exit_kind == "unused" else [True])
    assert not store.data
    assert ("a", "save") not in store.events
    assert store.events[-1] == ("a", "unlock")


@pytest.mark.parametrize("failure", ["iterator", "finalizer", "cleanup", "gate"])
async def test_caught_stream_failure_cannot_be_saved(failure):
    store = Store()

    def call(session):
        async def updates():
            try:
                yield "answer"
                if failure == "iterator":
                    raise RuntimeError("iterator")
            finally:
                if failure == "cleanup":
                    raise RuntimeError("cleanup")

        def finalize(parts):
            session.state["candidate"] = "unaccepted"
            if failure == "finalizer":
                raise RuntimeError("finalizer")
            return "answer"

        def reject(result):
            raise RuntimeError("gate")

        response = ResponseStream(updates(), finalizer=finalize)
        if failure == "gate":
            response.with_result_gate(reject)
        return response

    async with store.adapter().stream("a", call, create=True) as response:
        with pytest.raises(RuntimeError, match=failure):
            await response.get_final_response()
    assert not store.data
    assert ("a", "save") not in store.events


async def test_stream_save_failure_reaches_consumer_after_output():
    store = Store()
    store.save_error = True

    def call(session):
        async def updates():
            yield "delivered"

        return ResponseStream(updates(), finalizer=lambda parts: "".join(parts))

    delivered = []
    with pytest.raises(OSError, match="save failed"):
        async with store.adapter().stream("a", call, create=True) as response:
            delivered.extend([part async for part in response])
    assert delivered == ["delivered"]
    assert store.events[-1] == ("a", "unlock")


@pytest.mark.parametrize("wrapped", [False, True])
async def test_non_streaming_entry_point_refuses_a_response_stream(wrapped):
    store = Store()

    def call(session):
        async def updates():
            yield "not consumed"

        response = ResponseStream(updates(), finalizer=lambda parts: parts)
        if wrapped:

            async def factory():
                return response

            return factory()
        return response

    with pytest.raises(TypeError, match="SessionPersistence.stream"):
        await store.adapter().run("a", call, create=True)
    assert not store.data


@pytest.mark.parametrize("stream", [False, True])
async def test_task_cancellation_settles_request_before_releasing_lease(stream):
    store = Store()
    entered = asyncio.Event()
    cleanup = []

    async def wait(session):
        try:
            session.state["partial"] = True
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleanup.append(store.events[-1])

    def response(session):
        async def updates():
            yield "first"
            await wait(session)

        return ResponseStream(updates(), finalizer=lambda parts: parts)

    async def consume():
        adapter = store.adapter()
        if stream:
            async with adapter.stream("a", response, create=True) as result:
                await result.get_final_response()
        else:
            await adapter.run("a", wait, create=True)

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup == [("a", "load")]
    assert store.events[-1] == ("a", "unlock")
    assert not store.data
    await store.adapter().run("a", lambda session: asyncio.sleep(0), create=True)
    assert "partial" not in store.data["a"]["state"]


async def test_stream_holds_lease_through_cleanup_and_save():
    store = Store()
    entered = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()

    def call(session):
        async def updates():
            session.state["count"] = 1
            yield "answer"

        return ResponseStream(updates(), finalizer=lambda parts: parts)

    async def first():
        async with store.adapter().stream("a", call, create=True) as response:
            await response.get_final_response()
            entered.set()
            await release.wait()

    async def increment(session):
        assert session.state["count"] == 1
        session.state["count"] += 1

    async def second():
        waiting.set()
        await store.adapter().run("a", increment)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(first())
        await entered.wait()
        tasks.create_task(second())
        await waiting.wait()
        assert store.events == [("a", "lock"), ("a", "load")]
        release.set()
    assert store.data["a"]["state"]["count"] == 2


async def test_store_rejects_expired_lease_without_adapter_retrying_model():
    store = Store()
    calls = []

    async def call(session):
        calls.append(True)
        session.state["candidate"] = "stale"

    async def save(key, data):
        raise RuntimeError("lease expired")

    persistence = SessionPersistence(
        SessionCompactionRegistry(build_stack),
        load=store.load,
        save=save,
        lease=store.lease,
    )
    with pytest.raises(RuntimeError, match="lease expired"):
        await persistence.run("a", call, create=True)
    assert calls == [True]
    assert not store.data


async def test_two_adapters_serialize_one_session_while_another_session_proceeds():
    store = Store()
    first = store.adapter()
    second = store.adapter()
    entered = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def held(session):
        session.state["count"] = 1
        entered.set()
        await release.wait()

    async def increment(session):
        session.state["count"] = session.state.get("count", 0) + 1

    async def next_same():
        second_started.set()
        await second.run("a", increment, create=True)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(first.run("a", held, create=True))
        await entered.wait()
        tasks.create_task(next_same())
        await second_started.wait()
        await second.run("b", increment, create=True)
        assert ("a", "save") not in store.events
        assert store.data["b"]["state"]["count"] == 1
        release.set()
    assert store.data["a"]["state"]["count"] == 2
    assert [event for key, event in store.events if key == "a"] == [
        "lock",
        "load",
        "save",
        "unlock",
        "lock",
        "load",
        "save",
        "unlock",
    ]


@pytest.mark.parametrize("stream", [False, True])
async def test_original_history_replays_after_durable_save_into_fresh_registry(stream):
    class HostHistory(InMemoryHistoryProvider):
        async def before_run(self, **kwargs):
            pass

    store = Store()
    summarizer = Summarizer("Remembered user requirements.")
    original = [
        Message("user", [f"Turn {i}: " + "x" * 4_000], message_id=f"u{i}") for i in range(6)
    ]
    for index in range(3):
        registry = SessionCompactionRegistry(
            lambda: build_stack(summarizer, budget=6_000), max_cached_sessions=0
        )
        model = Model()
        agent = harness(registry, model, history_provider=HostHistory())
        persistence = store.adapter(registry)
        replayed = []

        # Keep a reference only to read this request's diagnostic after cache eviction.
        build = registry.build

        def observed_build():
            stack = build()
            replayed.append(stack)
            return stack

        registry.build = observed_build
        if stream:
            async with persistence.stream(
                "a",
                lambda session: agent.run(deepcopy(original), session=session, stream=True),
                create=index == 0,
            ) as response:
                assert (await response.get_final_response()).text == "answer"
        else:
            response = await persistence.run(
                "a",
                lambda session: agent.run(deepcopy(original), session=session),
                create=index == 0,
            )
            assert response.text == "answer"
        assert len(summarizer.requests) == 1
        assert len(model.prompts) == 1
        prompt = "\n".join(model.prompts[0][0])
        assert "Remembered user requirements." in prompt
        assert "Turn 1:" not in prompt
        if index:
            assert sum(stack.strategy.user_turns.user_summaries_replayed for stack in replayed) > 0
        assert not registry._cache
        original.append(Message("user", ["Continue."], message_id=f"new{index}"))


@pytest.mark.parametrize("stream", [False, True])
async def test_refused_recall_is_absent_from_durable_snapshot(stream):
    from maf_compaction._toolsummary import RECALL_TOOL_NAME

    class RefusingModel(Model):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.prompts.append(deepcopy(list(messages)))
            contents = [Content.from_text("answer")]
            if len(self.prompts) == 1:
                contents = [
                    Content.from_function_call(
                        call_id="refused", name=RECALL_TOOL_NAME, arguments={"values": "SKU-123"}
                    ),
                    Content.from_text(
                        "provider refusal",
                        additional_properties={"core": {"model_output_kind": "refusal"}},
                    ),
                ]
            response = ChatResponse(messages=Message("assistant", contents))
            if stream:

                async def updates():
                    for content in contents:
                        yield ChatResponseUpdate(role="assistant", contents=[content])

                return ResponseStream(updates(), finalizer=lambda _: response)

            async def answer():
                return response

            return answer()

    store = Store()
    registry = SessionCompactionRegistry(lambda: build_stack(budget=100_000))
    session = AgentSession(session_id="a")
    with registry.activate(session) as stack:
        stack.middleware._force_next = True
    store.data["a"] = session.to_dict()
    model = RefusingModel()
    agent = harness(registry, model)
    persistence = store.adapter(registry)
    if stream:
        async with persistence.stream(
            "a", lambda session: agent.run("Recall SKU-123.", session=session, stream=True)
        ) as response:
            assert (await response.get_final_response()).text == "answer"
    else:
        result = await persistence.run(
            "a", lambda session: agent.run("Recall SKU-123.", session=session)
        )
        assert result.text == "answer"
    assert len(model.prompts) == 2
    saved = json.dumps(store.data["a"])
    assert "provider refusal" not in saved
    assert '"call_id": "refused"' not in saved
    restored = AgentSession.from_dict(store.data["a"])
    with registry.activate(restored) as stack:
        assert stack.middleware.value_free_record_streak == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_enclosing_agent_rejection_leaves_durable_history_unchanged(stream):
    class Reject(AgentMiddleware):
        async def process(self, context, call_next):
            def reject(result):
                raise RuntimeError("egress rejected")

            if context.stream:
                context.stream_result_gates_after.append(reject)
                context.stream_buffer_updates = True
                await call_next()
            else:
                await call_next()
                reject(context.result)

    store = Store()
    store.data["a"] = AgentSession(session_id="a").to_dict()
    before = deepcopy(store.data)
    registry = SessionCompactionRegistry(lambda: build_stack(budget=100_000))
    agent = harness(registry, Model())
    assert agent.middleware is not None
    agent.middleware.append(Reject())
    persistence = store.adapter(registry)
    with pytest.raises(RuntimeError, match="egress rejected"):
        if stream:
            async with persistence.stream(
                "a", lambda session: agent.run("hello", session=session, stream=True)
            ) as response:
                await anext(response)
        else:
            await persistence.run("a", lambda session: agent.run("hello", session=session))
    assert store.data == before
    assert ("a", "save") not in store.events


@pytest.mark.parametrize("unused", [False, True])
async def test_harness_stream_abandonment_releases_session_without_saving(unused):
    store = Store()
    registry = SessionCompactionRegistry(lambda: build_stack(budget=100_000))
    model = Model()
    agent = harness(registry, model)
    async with store.adapter(registry).stream(
        "a", lambda session: agent.run("hello", session=session, stream=True), create=True
    ) as response:
        if not unused:
            assert (await anext(response)).text == "answer"
    assert len(model.prompts) == int(not unused)
    assert not registry._active
    assert not store.data
    assert store.events[-1] == ("a", "unlock")


async def test_lost_save_acknowledgement_does_not_trigger_rollback_or_retry():
    store = Store()
    calls = []

    async def call(session):
        calls.append(True)
        session.state["answer"] = "completed"

    async def save(key, snapshot):
        await store.save(key, snapshot)
        raise OSError("acknowledgement lost")

    persistence = SessionPersistence(
        SessionCompactionRegistry(build_stack), load=store.load, save=save, lease=store.lease
    )
    with pytest.raises(OSError, match="acknowledgement lost"):
        await persistence.run("a", call, create=True)
    assert calls == [True]
    assert store.data["a"]["state"]["answer"] == "completed"
    assert store.events.count(("a", "save")) == 1
