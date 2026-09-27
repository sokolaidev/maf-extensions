"""Continuity mode: an interrupted controller attach resumes the same PID 1 within its window."""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import sys
import threading
import time
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_hyperlight import (
    HyperlightPodDetached,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
    _backend,
    _pod_supervisor,
    _process,
    kubernetes,
)
from maf_sandbox_hyperlight._pod import (
    FRAME_LIMIT,
    PodJob,
    frame,
    refusal,
    refusal_reply,
    seal,
    unframe,
    unseal,
)
from maf_sandbox_hyperlight._pod_config import PodLaunch, hello_digest
from maf_sandbox_hyperlight._pod_supervisor import Supervisor
from maf_sandbox_hyperlight.kubernetes import (
    HyperlightPodController,
    HyperlightPodTemplate,
    ownership_name,
    pod_manifest,
)

KEY = SandboxKey("tenant:user", "conversation", "agent")
KIND = "codeact"
SECRET = "c" * 64
IDENTITY = {"scope": KEY.scope, "thread_id": KEY.thread_id, "agent_id": KEY.agent_id, "kind": KIND}
LAUNCH = PodLaunch(
    ownership_name(KEY, KIND), "pod-uid", "generation", 4 * 1024**3, hello_digest(SECRET), 30
)
TEMPLATE = HyperlightPodTemplate(
    "registry.example/runtime@sha256:" + "a" * 64, ("python", "app.py")
)
NAME = ownership_name(KEY, KIND)


def sealed(operation: str, counter: int, **fields: object) -> dict[str, object]:
    message = {"op": operation, "pod_uid": "pod-uid", "generation": "generation", **fields}
    return seal({**message, "counter": counter}, SECRET)


def session(recovery: int = 30, deadline: float | None = None) -> kubernetes._Session:
    return kubernetes._Session(
        "generation",
        {**IDENTITY, "secret": SECRET},
        time.monotonic() + 60 if deadline is None else deadline,
        recovery,
        uid="pod-uid",
    )


def test_a_sealed_message_cannot_be_altered_or_sealed_with_another_key():
    message = sealed("ping", 1)
    assert unseal(message, SECRET) == {
        "op": "ping",
        "pod_uid": "pod-uid",
        "generation": "generation",
        "counter": 1,
    }
    for changed in ({**message, "counter": 2}, {**message, "op": "stop"}, {**message, "mac": 1}):
        with pytest.raises(HyperlightWorkerError, match="unauthenticated"):
            unseal(changed, SECRET)
    with pytest.raises(HyperlightWorkerError, match="unauthenticated"):
        unseal(message, "d" * 64)
    del message["mac"]
    with pytest.raises(HyperlightWorkerError, match="unauthenticated"):
        unseal(message, SECRET)


@pytest.mark.parametrize("value", [-1, 601, True, 1.5, 1800])
def test_recovery_window_is_bounded_and_shorter_than_the_session(value):
    with pytest.raises(ValueError, match="recovery_seconds"):
        replace(TEMPLATE, recovery_seconds=value)
    with pytest.raises(ValueError, match="recovery_seconds"):
        replace(LAUNCH, recovery_seconds=value)
    assert replace(LAUNCH, recovery_seconds=600).recovery_seconds == 600


@pytest.mark.parametrize("recovery,once", [(0, True), (30, False)])
def test_only_continuity_mode_keeps_stdin_open_for_a_second_attach(recovery, once):
    pod = pod_manifest(
        KEY,
        KIND,
        replace(TEMPLATE, recovery_seconds=recovery),
        namespace="agents",
        generation="gen",
        secret_digest=hello_digest(SECRET),
    )
    container = cast("dict[str, Any]", pod["spec"])["containers"][0]
    assert container["stdinOnce"] is once
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert json.loads(env["MAF_HYPERLIGHT_POD_BINDING"])["recovery_seconds"] == recovery


@pytest.fixture
def pid1(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(LAUNCH, ["application"])
    subject.connected = True
    subject.key = SECRET
    subject.renew()
    subject.worker = 100
    return subject


def test_continuity_refuses_an_unsealed_or_replayed_controller_message(pid1):
    before = pid1.lease
    with pytest.raises(HyperlightWorkerError, match="unauthenticated"):
        pid1.controller_message(
            {"op": "ping", "pod_uid": "pod-uid", "generation": "generation", "counter": 1}
        )
    assert pid1.lease == before
    pid1.controller_message(sealed("ping", 1))
    for counter in (1, 0, "2"):
        with pytest.raises(HyperlightWorkerError, match="stale"):
            pid1.controller_message(sealed("ping", counter))
    pid1.controller_message(sealed("ping", 5))
    assert pid1.counter == 5


def test_default_mode_needs_no_seal(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=0), ["application"])
    subject.connected = True
    subject.renew()
    assert subject.lease == subject.fresh
    subject.controller_message({"op": "ping", "pod_uid": "pod-uid", "generation": "generation"})


def test_hello_keeps_the_secret_that_seals_every_later_message(pid1, monkeypatch):
    started = []
    monkeypatch.setattr(pid1, "start_owner", started.append)
    pid1.connected, pid1.key = False, None
    pid1.controller_message(
        {"op": "hello", "pod_uid": "pod-uid", "generation": "generation", "secret": SECRET}
        | IDENTITY
    )
    assert started and pid1.key == SECRET
    assert pid1.lease - pid1.fresh == pytest.approx(LAUNCH.recovery_seconds)


def test_a_detached_pod_refuses_new_calls_without_retiring(pid1):
    pid1.fresh = time.monotonic() - 1
    for operation in ({"op": "validate"}, {"op": "begin", "deadline": 0, "expires_at": 0}):
        with pytest.raises(HyperlightPodDetached, match="reconnecting"):
            pid1.handle(operation)
    pid1.deadline = pid1.expires_at = time.monotonic() + 10
    pid1.handle({"op": "end"})
    assert pid1.deadline is None and pid1.expires_at is None
    assert not pid1.retired.is_set()


def test_a_pod_past_its_recovery_window_refuses_as_retired(pid1):
    pid1.fresh = pid1.lease = time.monotonic() - 1
    with pytest.raises(HyperlightWorkerError, match="lease expired") as raised:
        pid1.handle({"op": "validate"})
    assert not isinstance(raised.value, HyperlightPodDetached)


def test_a_begin_the_controller_never_saw_is_withdrawn_once_it_is_stale(pid1, monkeypatch):
    emitted = []
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: emitted.append(event))
    pid1.fresh = time.monotonic() + 0.2
    started = time.monotonic()
    with pytest.raises(HyperlightPodDetached, match="disconnected"):
        pid1.handle(
            {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}
        )
    assert time.monotonic() - started < 2
    assert emitted == ["begin"] and pid1.sequence == 1
    assert pid1.deadline is None and pid1.expires_at is None and not pid1.retired.is_set()


def test_a_pinging_controller_that_never_acknowledges_still_retires(pid1, monkeypatch):
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: None)
    stop = threading.Event()

    def ping():
        while not stop.wait(0.2):
            pid1.renew()

    pinger = threading.Thread(target=ping, daemon=True)
    pinger.start()
    started = time.monotonic()
    try:
        with pytest.raises(HyperlightWorkerError, match="retired"):
            pid1.handle(
                {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}
            )
    finally:
        stop.set()
        pinger.join()
    assert pid1.reason == "controller did not acknowledge the deadline"
    assert time.monotonic() - started < 5


def test_resume_reports_the_call_state_the_lost_events_carried(pid1, monkeypatch):
    emitted = []
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: emitted.append((event, fields)))
    pid1.sequence, pid1.expires_at = 3, 1234.5
    pid1.platform = {"kernel": "6.8.0-1067-azure"}
    pid1.ack.set()
    pid1.fresh = time.monotonic() - 1
    pid1.controller_message(sealed("resume", 1))
    assert emitted == [
        (
            "resumed",
            {
                "sequence": 3,
                "expires_at": 1234.5,
                "acknowledged": True,
                "platform": {"kernel": "6.8.0-1067-azure"},
            },
        )
    ]
    assert pid1.fresh > time.monotonic()
    pid1.ack.clear()
    pid1.controller_message(sealed("resume", 2))
    assert emitted[1][1]["acknowledged"] is False


def test_default_mode_has_no_resume(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=0), ["application"])
    subject.connected = True
    subject.renew()
    with pytest.raises(HyperlightWorkerError, match="invalid controller lifecycle"):
        subject.controller_message(
            {"op": "resume", "pod_uid": "pod-uid", "generation": "generation"}
        )


def read(subject: Supervisor, monkeypatch, payload: bytes) -> list[dict[str, object]]:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    subject.read_controller()
    received = []
    while not subject.incoming.empty():
        received.append(subject.incoming.get_nowait())
    return received


def test_one_torn_line_is_dropped_before_the_reconnecting_frame(pid1, monkeypatch):
    resume = frame(sealed("resume", 1))
    received = read(pid1, monkeypatch, b'{"op":"pi' + b"\n" + resume + b"\n" + resume)
    assert received == [unframe(resume), unframe(resume)]
    assert pid1.reason == "controller stream closed"


@pytest.mark.parametrize(
    "payload",
    [b"torn\n\n", b"x" * (2 * FRAME_LIMIT + 2) + b"\n"],
    ids=["two-malformed", "overlong"],
)
def test_consecutive_malformed_lines_still_retire(pid1, monkeypatch, payload):
    assert read(pid1, monkeypatch, payload) == []
    assert pid1.reason.startswith("controller stream failed")


def test_default_mode_retires_on_one_torn_line(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=0), ["application"])
    assert read(subject, monkeypatch, b"torn\n" + frame(sealed("ping", 1))) == []
    assert subject.reason.startswith("controller stream failed")


# Parameters are built at collection, well before a full suite reaches these tests.
FUTURE = time.time() + 86400


@pytest.mark.parametrize(
    "known,snapshot,after,acked",
    [
        ((0, None), (0, None, False), (0, None), False),
        ((2, FUTURE), (2, FUTURE, True), (2, FUTURE), False),
        ((2, FUTURE), (2, FUTURE, False), (2, FUTURE), True),
        ((2, FUTURE), (2, None, True), (2, None), False),
        ((2, FUTURE), (3, FUTURE + 1, False), (3, FUTURE + 1), True),
        ((2, None), (3, None, False), (3, None), False),
    ],
    ids=["idle", "active", "ack-lost", "ended", "begin-lost", "begin-withdrawn"],
)
def test_resume_adopts_the_state_lost_events_carried(known, snapshot, after, acked):
    state = session()
    state.sequence, state.deadline = known
    sent: list[tuple[str, dict[str, object]]] = []
    sequence, expires, acknowledged = snapshot
    state.reconcile(
        {"sequence": sequence, "expires_at": expires, "acknowledged": acknowledged},
        lambda operation, **fields: sent.append((operation, fields)),
    )
    assert (state.sequence, state.deadline) == after
    assert sent == ([("ack", {"sequence": after[0]})] if acked else [])
    assert state.resumed and state.ready and state.interruptions == [""]


def test_resume_restores_the_platform_a_missed_ready_carried():
    state = session()
    observed = {"kernel": "6.8.0-1067-azure", "memory.max": "x" * 300}
    state.reconcile(
        {"sequence": 0, "expires_at": None, "acknowledged": False, "platform": observed},
        lambda operation, **fields: None,
    )
    assert state.ready
    assert state.platform == {"kernel": "6.8.0-1067-azure", "memory.max": "x" * 256}


@pytest.mark.parametrize(
    "known,snapshot",
    [
        ((2, FUTURE), (1, None, True)),
        ((2, FUTURE), (4, None, False)),
        ((2, FUTURE), (2, FUTURE + 1, True)),
        ((2, None), (2, FUTURE, True)),
        ((2, None), (3, FUTURE, True)),
        ((2, None), (3, time.time() - 1, False)),
        ((2, None), ("2", None, False)),
        ((2, None), (2, float("nan"), False)),
        ((2, None), (2, None, 1)),
    ],
)
def test_resume_refuses_a_state_the_controller_cannot_have_missed(known, snapshot):
    state = session()
    state.sequence, state.deadline = known
    sequence, expires, acknowledged = snapshot
    with pytest.raises(HyperlightWorkerError):
        state.reconcile(
            {"sequence": sequence, "expires_at": expires, "acknowledged": acknowledged},
            lambda operation, **fields: None,
        )
    assert not state.resumed and not state.interruptions


def running_pod(**changes: Any) -> dict[str, Any]:
    status = {"name": "sandbox", "state": {"running": {}}, "restartCount": 0, "containerID": "c1"}
    status.update(changes.pop("container", {}))
    pod = {
        "metadata": {
            "name": NAME,
            "uid": "pod-uid",
            "annotations": {kubernetes._GENERATION: "generation"},
        },
        "status": {"containerStatuses": [status]},
    }
    pod["metadata"].update(changes)
    return pod


class PodReader(HyperlightPodController):
    def __init__(self, pod: object) -> None:
        super().__init__(kubeconfig="config", context="context", namespace="agents")
        self.pod = pod

    def api(self, *arguments: str, body: dict[str, object] | None = None) -> dict[str, object]:
        if isinstance(self.pod, BaseException):
            raise self.pod
        return cast("dict[str, object]", self.pod)


@pytest.mark.parametrize(
    "pod,same",
    [
        (running_pod(), True),
        (running_pod(uid="other"), False),
        (running_pod(annotations={kubernetes._GENERATION: "old"}), False),
        (running_pod(deletionTimestamp="now"), False),
        (running_pod(container={"containerID": "c2"}), False),
        (running_pod(container={"restartCount": 1}), False),
        (running_pod(container={"state": {"terminated": {}}}), False),
        ({}, False),
        (HyperlightWorkerError("API unavailable"), None),
    ],
)
def test_reattach_requires_the_same_running_container(pod, same):
    assert PodReader(pod)._same_container(NAME, "c1", session()) is same


class Reattaching(HyperlightPodController):
    """Scripted attach outcomes: each entry is what one attach returns, and whether it resumed."""

    def __init__(self, outcomes, same=True):
        super().__init__(kubeconfig="config", context="context", namespace="agents")
        self.outcomes = list(outcomes)
        self.same = same
        self.attaches: list[float | None] = []

    def _same_container(self, name, container, session):
        return self.same

    def _attach_once(self, name, session, *, resume_by):
        self.attaches.append(resume_by)
        ended, resumed = self.outcomes.pop(0)
        if resumed:
            session.reconcile(
                {"sequence": 0, "expires_at": None, "acknowledged": False}, lambda *a, **k: None
            )
        return ended


def test_an_interrupted_attach_resumes_and_is_recorded():
    state = session(recovery=5)
    controller = Reattaching(
        [
            ("attach stream closed", False),
            ("reconnection was not confirmed", False),
            ("", True),
        ]
    )
    controller._hold(NAME, "c1", state)
    assert controller.attaches[0] is None and all(controller.attaches[1:])
    assert len(controller.attaches) == 3
    assert state.interruptions == ["attach stream closed"]


def test_repeated_interruptions_each_get_a_fresh_window():
    state = session(recovery=5)
    controller = Reattaching(
        [("attach stream closed", False), ("attach stream closed", True), ("", True)]
    )
    controller._hold(NAME, "c1", state)
    assert len(controller.attaches) == 3
    assert state.interruptions == ["attach stream closed", "attach stream closed"]


def test_default_mode_never_reattaches():
    state = session(recovery=0)
    controller = Reattaching([("attach stream closed", False)])
    controller._hold(NAME, "c1", state)
    assert len(controller.attaches) == 1 and state.interruptions == []


def test_a_changed_pod_is_never_reattached_and_is_no_interruption():
    state = session(recovery=5)
    controller = Reattaching([("attach stream closed", False)], same=False)
    started = time.monotonic()
    controller._hold(NAME, "c1", state)
    assert len(controller.attaches) == 1 and time.monotonic() - started < 1
    assert state.interruptions == []


def test_only_a_detached_refusal_reaches_the_owner_as_one():
    detached = refusal(unframe(frame(refusal_reply(HyperlightPodDetached("reconnecting")))))
    assert isinstance(detached, HyperlightPodDetached) and str(detached) == "reconnecting"
    other = refusal(unframe(frame(refusal_reply(HyperlightWorkerError("x" * 2000)))))
    assert type(other) is HyperlightWorkerError and len(str(other)) == 1024
    assert type(refusal({"detached": "yes"})) is HyperlightWorkerError


def test_an_unreachable_api_retires_when_the_window_closes():
    controller = Reattaching([("attach stream closed", False)], same=None)
    started = time.monotonic()
    controller._hold(NAME, "c1", session(recovery=1))
    assert len(controller.attaches) == 1 and 1 <= time.monotonic() - started < 3


def test_an_expired_call_deadline_is_enforced_while_detached():
    state = session(recovery=30)
    state.sequence, state.deadline = 1, time.time() - 1
    controller = Reattaching([("attach stream closed", False)])
    started = time.monotonic()
    controller._hold(NAME, "c1", state)
    assert len(controller.attaches) == 1 and time.monotonic() - started < 1


def test_resume_is_written_after_a_newline_and_every_frame_is_sealed():
    state = session(recovery=30)
    state.ready = True
    resumed = frame(
        {
            "event": "resumed",
            "pod_uid": "pod-uid",
            "generation": "generation",
            "sequence": 0,
            "expires_at": None,
            "acknowledged": False,
        }
    )
    source, sink = os.pipe()
    stopped = threading.Event()
    readers: list[threading.Thread] = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")
    ended: list[str] = []
    with open(source, "rb") as control:
        stream = SimpleNamespace(
            stdout=control,
            stderr=io.BytesIO(),
            stdin=io.BytesIO(),
            poll=lambda: 0 if stopped.is_set() else None,
        )
        supervising = threading.Thread(
            target=lambda: ended.append(
                controller._supervise(stream, state, readers, resume_by=time.monotonic() + 5)
            )
        )
        supervising.start()
        try:
            os.write(sink, resumed)
            wait_for(lambda: state.resumed and stream.stdin.getvalue().count(b"\n") >= 3)
        finally:
            os.close(sink)
            supervising.join(timeout=5)
            stopped.set()
            for reader in readers:
                reader.join(timeout=2)
    assert ended == ["attach stream closed"]
    lines = stream.stdin.getvalue().splitlines(True)
    assert lines[0] == b"\n"
    messages = [unseal(unframe(line), SECRET) for line in lines[1:]]
    assert [message["op"] for message in messages[:2]] == ["resume", "ping"]
    counters = [message["counter"] for message in messages]
    assert counters == list(range(1, len(counters) + 1))


def test_an_unconfirmed_resume_gives_up_at_its_bound():
    state = session(recovery=30)
    state.ready = True
    source, sink = os.pipe()
    stopped = threading.Event()
    readers: list[threading.Thread] = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")
    with open(source, "rb") as control:
        stream = SimpleNamespace(
            stdout=control,
            stderr=io.BytesIO(),
            stdin=io.BytesIO(),
            poll=lambda: 0 if stopped.is_set() else None,
        )
        try:
            ended = controller._supervise(stream, state, readers, resume_by=time.monotonic() + 0.3)
        finally:
            stopped.set()
            os.close(sink)
            for reader in readers:
                reader.join(timeout=2)
    assert ended == "reconnection was not confirmed" and not state.resumed


def test_a_refused_begin_reaches_the_backend_unwrapped(monkeypatch):
    job = object.__new__(PodJob)

    def begin(deadline: float) -> None:
        raise HyperlightPodDetached("the pod's controller is reconnecting")

    monkeypatch.setattr(job, "ready", lambda *, deadline: None)
    monkeypatch.setattr(job, "begin", begin)
    worker = object.__new__(_process.Worker)
    written = io.BytesIO()
    worker.__dict__.update(
        _owner_pid=os.getpid(),
        _job=job,
        _input=written,
        _stderr_guard=threading.Lock(),
        _stderr=bytearray(),
    )
    with pytest.raises(HyperlightPodDetached):
        worker.request({"op": "run", "code": ""}, deadline=time.monotonic() + 5)
    assert written.getvalue() == b""


class FakeWorker:
    def __init__(self, config: HyperlightSandboxConfig) -> None:
        self.alive = True
        self.detached = False
        self.aborted = False
        self.calls: list[dict[str, object]] = []

    def request(self, message: dict[str, object], *, deadline: float) -> dict[str, object]:
        self.calls.append(message)
        if self.detached:
            raise HyperlightPodDetached("the pod's controller is reconnecting")
        if message["op"] == "run":
            return {"stdout": "ran", "stderr": "", "exit_code": 0}
        return {"ok": True}

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.alive = False


SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


def test_a_refused_call_fails_without_retiring_the_sandbox(monkeypatch):
    monkeypatch.setattr(_backend, "Worker", FakeWorker)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    backend = HyperlightSandboxBackend()

    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await backend.acquire(KEY, SPEC))
        worker = cast("FakeWorker", sandbox.worker)
        worker.detached = True
        with pytest.raises(HyperlightPodDetached):
            await sandbox.run_code("print(1)", timeout=5)
        assert sandbox.alive and worker.alive and not worker.aborted
        worker.detached = False
        assert (await sandbox.run_code("print(1)", timeout=5)).stdout == "ran"
        await backend.aclose()

    asyncio.run(check())


def test_a_refused_preparation_releases_the_worker_without_retiring_the_pod(monkeypatch):
    class Job:
        def __init__(self, binding, timeout):
            pass

        def request(self, operation, **fields):
            return {"ok": True}

    class DetachedWorker(FakeWorker):
        def __init__(self, config):
            super().__init__(config)
            self.detached = True

    workers: list[FakeWorker] = []

    def build(config):
        workers.append(DetachedWorker(config))
        return workers[-1]

    from maf_sandbox_hyperlight import HyperlightPodConfig, _pod

    monkeypatch.setattr(_pod, "PodJob", Job)
    monkeypatch.setattr(_backend, "Worker", build)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    binding = HyperlightPodConfig(KEY, "python", "pod-uid", "generation", 4 * 1024**3)
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)
    )

    async def check():
        with pytest.raises(HyperlightPodDetached):
            await backend.acquire(KEY, SPEC)
        assert not workers[0].aborted and not workers[0].alive
        assert not backend._sandboxes

    asyncio.run(check())


class Attach:
    """One kubectl attach to a shared PID 1 stdin; the pod's stdout reaches it while attached."""

    def __init__(self, stdin: int) -> None:
        source, self.sink = os.pipe()
        self.stdout = os.fdopen(source, "rb")
        self.stderr = io.BytesIO()
        self.stdin = self
        self.pod_stdin = stdin
        self.returncode: int | None = None
        self.guard = threading.Lock()

    def deliver(self, payload: bytes) -> None:
        with self.guard:
            if self.returncode is None:
                os.write(self.sink, payload)

    def write(self, payload: bytes) -> None:
        if self.returncode is not None:
            raise BrokenPipeError
        os.write(self.pod_stdin, payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def drop(self) -> None:
        with self.guard:
            if self.returncode is None:
                self.returncode = 1
                os.close(self.sink)

    terminate = kill = drop


class Pod(HyperlightPodController):
    """A controller whose attaches reach an in-process PID 1 and whose API sees one container."""

    def __init__(self, pid1: Supervisor, stdin: int) -> None:
        super().__init__(kubeconfig="config", context="context", namespace="agents")
        self.pid1 = pid1
        self.stdin = stdin
        self.attaches: list[Attach] = []
        self.reachable = threading.Event()
        self.reachable.set()

    def _same_container(self, name, container, session):
        return True if self.reachable.is_set() else None

    def _attach(self, name):
        self.attaches.append(Attach(self.stdin))
        return self.attaches[-1]

    def deliver(self, payload: bytes) -> None:
        """PID 1's stdout: the current attach sees it; with none, only the container log does."""
        if self.attaches:
            self.attaches[-1].deliver(payload)


def pump(pid1: Supervisor, pod: Pod, stop: threading.Event) -> None:
    """The part of PID 1's run loop that moves lifecycle frames."""
    while not stop.is_set() and not pid1.retired.is_set():
        with suppress(queue.Empty):
            message = pid1.incoming.get(timeout=0.02)
            try:
                pid1.controller_message(message)
            except (ValueError, HyperlightWorkerError) as error:
                pid1.retire(str(error))
        while not pid1.outgoing.empty():
            pod.deliver(frame(pid1.outgoing.get_nowait()))
        if time.monotonic() >= pid1.lease:
            pid1.retire("controller recovery window expired")


def wait_for(condition, timeout: float = 5) -> None:
    until = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < until, "condition not reached"
        time.sleep(0.01)


def test_a_session_survives_an_interrupted_attach_end_to_end(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    monkeypatch.setattr(_pod_supervisor, "LEASE_SECONDS", 1.5)
    pid1 = Supervisor(replace(LAUNCH, recovery_seconds=3), ["application"])
    pid1.worker = 100
    monkeypatch.setattr(
        pid1, "start_owner", lambda binding: pid1.emit("ready", owner_pid=2, platform={})
    )
    stdin_read, stdin_write = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=os.fdopen(stdin_read, "rb")))
    pod = Pod(pid1, stdin_write)
    state = session(recovery=3)
    stop = threading.Event()
    threads = [
        threading.Thread(target=pid1.read_controller, daemon=True),
        threading.Thread(target=pump, args=(pid1, pod, stop), daemon=True),
        threading.Thread(target=pod._hold, args=(NAME, "c1", state), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        wait_for(lambda: state.ready and pid1.connected)
        call = time.time() + 30
        pid1.handle({"op": "begin", "deadline": time.monotonic() + 30, "expires_at": call})
        wait_for(lambda: state.deadline == call)

        # An attach that died mid-frame, then a reattach that resumes the active call.
        pod.reachable.clear()
        pod.attaches[-1].drop()
        os.write(stdin_write, b'{"op":"pi')
        pod.reachable.set()
        wait_for(lambda: len(pod.attaches) == 2 and state.resumed)
        assert len(state.interruptions) == 1
        assert (state.sequence, state.deadline) == (1, call)
        pid1.handle({"op": "end"})
        wait_for(lambda: state.deadline is None)

        # A drop the controller cannot recover from at once: new calls are refused, not run.
        pod.reachable.clear()
        pod.attaches[-1].drop()
        wait_for(lambda: time.monotonic() >= pid1.fresh)
        with pytest.raises(HyperlightPodDetached):
            pid1.handle({"op": "validate"})
        pod.reachable.set()
        wait_for(lambda: len(pod.attaches) == 3 and state.resumed)
        pid1.handle({"op": "begin", "deadline": time.monotonic() + 30, "expires_at": call})
        pid1.handle({"op": "end"})
        wait_for(lambda: state.sequence == 2 and state.deadline is None)
        assert not pid1.retired.is_set()

        # An outage longer than the window retires PID 1 and ends the controller's hold.
        pod.reachable.clear()
        pod.attaches[-1].drop()
        wait_for(pid1.retired.is_set, timeout=10)
        assert pid1.reason == "controller recovery window expired"
        threads[2].join(timeout=10)
        assert not threads[2].is_alive()
        assert len(state.interruptions) == 2
    finally:
        stop.set()
        pid1.retire("test finished")
        for attach in pod.attaches:
            attach.drop()
        os.close(stdin_write)
        for thread in threads:
            thread.join(timeout=5)
