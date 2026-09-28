"""Continuity mode: an interrupted controller attach resumes the same PID 1 within its window."""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import subprocess
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
    _pod,
    _pod_supervisor,
    _process,
    kubernetes,
)
from maf_sandbox_hyperlight._pod import (
    FRAME_LIMIT,
    LIFECYCLE_PROTOCOL,
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


def test_a_ping_renews_and_is_answered_only_while_the_lease_is_fresh(pid1, monkeypatch):
    emitted = []
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: emitted.append(event))
    pid1.controller_message(sealed("ping", 1))
    assert emitted == ["alive"]
    pid1.fresh = time.monotonic() - 1
    lease = pid1.lease
    pid1.controller_message(sealed("ping", 2))
    assert emitted == ["alive"] and pid1.lease == lease and pid1.counter == 2
    assert not pid1.retired.is_set()
    pid1.controller_message(sealed("resume", 3))
    pid1.controller_message(sealed("ping", 4))
    assert emitted == ["alive", "resumed", "alive"] and pid1.fresh > time.monotonic()


def test_default_mode_answers_each_ping(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=0), ["application"])
    subject.connected = True
    subject.renew()
    subject.controller_message({"op": "ping", "pod_uid": "pod-uid", "generation": "generation"})
    assert subject.outgoing.get_nowait()["event"] == "alive"


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
    assert emitted == ["begin", "end"] and pid1.sequence == 1
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


def test_the_owner_waits_for_begin_longer_than_pid1_can_hold_it(monkeypatch):
    job = object.__new__(PodJob)
    job.timeout = 3.0
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(job, "request", lambda operation, **fields: sent.append(fields) or {})
    job.begin(time.monotonic() + 60)
    job.begin(time.monotonic() + 2)
    assert sent[0]["timeout"] > _pod.BEGIN_WAIT
    assert 2 < cast("float", sent[1]["timeout"]) and sent[1]["timeout"] >= job.timeout


def test_repeated_resumes_cannot_hold_a_begin_past_its_bound(pid1, monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "BEGIN_WAIT", 1.0)
    monkeypatch.setattr(_pod_supervisor, "ACK_SECONDS", 0.4)
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: None)
    stop = threading.Event()

    def resume_without_ack():
        while not stop.wait(0.1):
            pid1.renew()
            pid1.resumed_at = time.monotonic()

    resumer = threading.Thread(target=resume_without_ack, daemon=True)
    resumer.start()
    started = time.monotonic()
    try:
        with pytest.raises(HyperlightWorkerError):
            pid1.handle(
                {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}
            )
    finally:
        stop.set()
        resumer.join()
    assert time.monotonic() - started < 1.5


def test_an_acknowledgement_just_after_a_late_resume_is_accepted(pid1, monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "LEASE_SECONDS", 1.0)
    monkeypatch.setattr(_pod_supervisor, "ACK_SECONDS", 0.3)
    monkeypatch.setattr(_pod_supervisor, "PING_SECONDS", 0.1)
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: None)
    pid1.renew()

    def reconnect():
        # The controller comes back after the acknowledgement window, then acknowledges.
        time.sleep(0.6)
        pid1.controller_message(sealed("resume", 1))
        time.sleep(0.2)
        pid1.controller_message(sealed("ack", 2, sequence=1))

    controller = threading.Thread(target=reconnect, daemon=True)
    controller.start()
    pid1.handle({"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10})
    controller.join()
    assert pid1.deadline is not None and not pid1.retired.is_set()


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


@pytest.mark.parametrize("recovery", [0, 120])
def test_the_startup_lease_includes_the_recovery_window(monkeypatch, recovery):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    before = time.monotonic()
    subject = Supervisor(replace(LAUNCH, recovery_seconds=recovery), ["application"])
    startup = subject.lease - before
    assert _pod_supervisor.STARTUP_SECONDS + recovery <= startup
    assert startup < _pod_supervisor.STARTUP_SECONDS + recovery + 1


def test_a_repeated_hello_is_checked_then_ignored(pid1, monkeypatch):
    started = []
    monkeypatch.setattr(pid1, "start_owner", started.append)
    repeat = {"op": "hello", "pod_uid": "pod-uid", "generation": "generation", "secret": SECRET}
    pid1.controller_message(repeat | IDENTITY)
    assert not started and pid1.counter == 0 and not pid1.retired.is_set()
    with pytest.raises(HyperlightWorkerError, match="controller secret"):
        pid1.controller_message(repeat | IDENTITY | {"secret": "d" * 64})
    assert not started


def replay(events, state):
    """Apply PID 1's events in emitted order with the controller's own acceptance rules."""
    for event, fields in events:
        if event == "begin":
            assert fields["sequence"] == state.sequence + 1 and state.deadline is None
            state.sequence, state.deadline = state.sequence + 1, fields["expires_at"]
        elif event == "end":
            assert fields["sequence"] == state.sequence and state.deadline is not None
            state.deadline = None
        else:
            state.reconcile(fields, lambda operation, **fields: None)


@pytest.mark.parametrize("operation", ["begin", "end"])
@pytest.mark.parametrize("point", range(40))
def test_a_resume_at_any_line_of_a_call_change_is_consistent(pid1, monkeypatch, operation, point):
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: events.append((event, fields)))
    if operation == "end":
        pid1.sequence, pid1.deadline, pid1.expires_at = 1, time.monotonic() + 10, 1234.5
        pid1.ack.set()
        message: dict[str, object] = {"op": "end"}
    else:
        message = {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}

    def acknowledge():
        # The controller acknowledges a begin once it sees one.
        until = time.monotonic() + 5
        while not any(event == "begin" for event, _ in events) and time.monotonic() < until:
            time.sleep(0.005)
        pid1.ack.set()

    lines = 0
    resuming: list[threading.Thread] = []

    def interrupt(frame, event, arg):
        nonlocal lines
        if event == "line" and not resuming:
            lines += 1
            if lines == point + 1:
                # The controller's resume lands between two lines of the owner's request.
                resuming.append(
                    threading.Thread(
                        target=pid1.controller_message, args=(sealed("resume", 1),), daemon=True
                    )
                )
                resuming[0].start()
                resuming[0].join(timeout=0.2)
        return interrupt

    def enter(frame, event, arg):
        return interrupt if frame.f_code is Supervisor.handle.__code__ else None

    acknowledger = threading.Thread(target=acknowledge, daemon=True)
    if operation == "begin":
        acknowledger.start()
    previous = sys.gettrace()
    sys.settrace(enter)
    try:
        pid1.handle(message)
    finally:
        sys.settrace(previous)
    if not resuming:
        pytest.skip("the request finished before this line")
    resuming[0].join(timeout=5)
    if operation == "begin":
        acknowledger.join(timeout=5)
    state = session()
    state.ready = True
    if operation == "end":
        state.sequence, state.deadline = 1, 1234.5
    replay(events, state)
    assert state.resumed


def test_a_resume_that_wins_the_stale_check_keeps_the_call(pid1, monkeypatch):
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(pid1, "emit", lambda event, **fields: events.append((event, fields)))
    waits = []

    def stale_then_resumed(started):
        waits.append(started)
        if len(waits) == 1:
            # Stale when the wait gave up; the controller's resume lands before the check.
            pid1.controller_message(sealed("resume", 1))
            threading.Timer(
                0.1, pid1.controller_message, args=(sealed("ack", 2, sequence=1),)
            ).start()
            return False
        return pid1.ack.wait(2)

    monkeypatch.setattr(pid1, "await_ack", stale_then_resumed)
    pid1.handle({"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10})
    assert pid1.deadline is not None and not pid1.retired.is_set()
    assert len(waits) == 2 and waits[0] == waits[1]
    assert [event for event, _ in events] == ["begin", "resumed"]


@pytest.mark.parametrize("point", range(40))
def test_a_stale_begin_and_a_resume_agree_on_whether_the_call_runs(pid1, monkeypatch, point):
    events: list[tuple[str, dict[str, object]]] = []

    def emit(event, **fields):
        events.append((event, fields))
        attached = any(name == "resumed" for name, _ in events)
        unacknowledged = event == "resumed" and not fields["acknowledged"]
        if attached and event in ("begin", "resumed") and (event == "begin" or unacknowledged):
            if event == "resumed" and fields["expires_at"] is None:
                return
            # Once reattached, the controller acknowledges each call it learns of.
            threading.Thread(
                target=pid1.controller_message,
                args=(sealed("ack", 2, sequence=fields["sequence"]),),
                daemon=True,
            ).start()

    monkeypatch.setattr(pid1, "emit", emit)
    pid1.fresh = time.monotonic() + 0.3
    lines = 0
    resuming: list[threading.Thread] = []

    def interrupt(frame, event, arg):
        nonlocal lines
        if event == "line" and not resuming:
            lines += 1
            if lines == point + 1:
                resuming.append(
                    threading.Thread(
                        target=pid1.controller_message, args=(sealed("resume", 1),), daemon=True
                    )
                )
                resuming[0].start()
                resuming[0].join(timeout=0.2)
        return interrupt

    def enter(frame, event, arg):
        return interrupt if frame.f_code is Supervisor.handle.__code__ else None

    previous = sys.gettrace()
    sys.settrace(enter)
    try:
        pid1.handle(
            {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}
        )
        admitted = True
    except HyperlightPodDetached:
        admitted = False
    finally:
        sys.settrace(previous)
    if not resuming:
        pytest.skip("the request finished before this line")
    resuming[0].join(timeout=5)
    assert not pid1.retired.is_set()
    # Events before the resume went down with the dropped attach.
    delivered = events[[event for event, _ in events].index("resumed") :]
    state = session()
    state.ready = True
    replay(delivered, state)
    assert (state.deadline is not None) == admitted == (pid1.deadline is not None)


@pytest.mark.parametrize(
    "recovery,connected,reason",
    [
        (30, True, "controller recovery window expired"),
        (30, False, "controller never sent its hello"),
        (0, True, "controller lease or native deadline expired"),
        (0, False, "controller lease or native deadline expired"),
    ],
)
def test_a_lapsed_lease_names_which_controller_was_lost(monkeypatch, recovery, connected, reason):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=recovery), ["application"])
    subject.connected = connected
    assert subject.lapse_reason() == reason


@pytest.mark.skipif(sys.platform != "linux", reason="PID 1's run loop is Linux-only")
def test_a_pod_whose_hello_never_came_records_that_reason(tmp_path):
    log = tmp_path / "termination-log"
    binding = {
        "protocol": LIFECYCLE_PROTOCOL,
        "owner": LAUNCH.owner,
        "generation": "generation",
        "memory_limit_bytes": 1,
        "hello_digest": LAUNCH.hello_digest,
        "recovery_seconds": 1,
    }
    program = "\n".join(
        [
            "import json, os, sys",
            "from maf_sandbox_hyperlight import _pod_supervisor",
            f"os.environ['MAF_HYPERLIGHT_POD_BINDING'] = {json.dumps(binding)!r}",
            "os.environ['MAF_HYPERLIGHT_POD_UID'] = 'pod-uid'",
            f"_pod_supervisor.TERMINATION_LOG = {str(log)!r}",
            "_pod_supervisor.STARTUP_SECONDS = 0",
            "_pod_supervisor.verify_init = lambda launch: {}",
            "_pod_supervisor._oom_kills = lambda: 0",
            "sys.argv = ['supervisor', 'app']",
            "_pod_supervisor.main()",
        ]
    )
    # The real run loop, with an attach that stays open and never writes a hello.
    pid1 = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert pid1.wait(timeout=20) == 70
    finally:
        if pid1.poll() is None:
            pid1.kill()
        assert pid1.stdin is not None and pid1.stderr is not None
        pid1.stdin.close()
        pid1.stderr.close()
        pid1.wait(timeout=10)
    assert log.read_text(encoding="utf-8") == "controller never sent its hello"


@pytest.mark.parametrize(
    "recovery,connected,reason",
    [
        (30, True, "controller recovery window expired"),
        (30, False, "controller never sent its hello"),
        (0, True, "controller lease expired"),
    ],
)
def test_a_frame_after_the_lease_lapsed_records_the_lapse_reason(
    monkeypatch, recovery, connected, reason
):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(replace(LAUNCH, recovery_seconds=recovery), ["application"])
    subject.connected, subject.key = connected, SECRET
    subject.lease = time.monotonic() - 1
    with pytest.raises(HyperlightWorkerError, match="cannot be renewed"):
        subject.controller_message(sealed("ping", 1))
    assert subject.reason == reason


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

    def __init__(self, outcomes, same=True, during_check=None):
        super().__init__(kubeconfig="config", context="context", namespace="agents")
        self.outcomes = list(outcomes)
        self.same = same
        self.during_check = during_check
        self.attaches: list[float | None] = []

    def _same_container(self, name, container, session):
        if self.during_check is not None:
            self.during_check(session)
        return self.same

    def _attach_once(self, name, session, *, resume_by):
        self.attaches.append(resume_by)
        session.ended_at = time.monotonic()
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


def expire_the_window(state):
    time.sleep(1.1)


def expire_the_call(state):
    state.sequence, state.deadline = 1, time.time() - 1


@pytest.mark.parametrize("during_check", [expire_the_window, expire_the_call])
def test_a_bound_that_passes_during_the_pod_check_prevents_the_reattach(during_check):
    state = session(recovery=1)
    controller = Reattaching(
        [("attach stream closed", False), ("", True)], during_check=during_check
    )
    controller._hold(NAME, "c1", state)
    assert len(controller.attaches) == 1 and not state.resumed


def test_the_recovery_window_starts_before_the_old_attach_is_cleaned_up(monkeypatch):
    attempts: list[float | None] = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")

    def slow_to_reap(name):
        # A kubectl that takes longer to reap than the whole recovery window.
        return SimpleNamespace(
            poll=lambda: 0,
            wait=lambda timeout: time.sleep(1.2),
            stdin=None,
            stdout=None,
            stderr=None,
        )

    def supervise(stream, session, readers, *, resume_by):
        attempts.append(resume_by)
        return "attach stream closed"

    monkeypatch.setattr(controller, "_attach", slow_to_reap)
    monkeypatch.setattr(controller, "_supervise", supervise)
    monkeypatch.setattr(controller, "_same_container", lambda name, container, session: True)
    controller._hold(NAME, "c1", session(recovery=1))
    assert attempts == [None]


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


def supervise_events(state, events, *, resuming=True):
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
            for event in events:
                os.write(sink, frame({"pod_uid": "pod-uid", "generation": "generation", **event}))
            return controller._supervise(
                stream,
                state,
                readers,
                resume_by=time.monotonic() + 0.2 if resuming else None,
            )
        finally:
            stopped.set()
            os.close(sink)
            for reader in readers:
                reader.join(timeout=2)
                assert not reader.is_alive()


@pytest.mark.parametrize("previous_active", [False, True])
def test_reattach_reconciles_a_withdrawal_before_its_resume_snapshot(
    pid1, previous_active, monkeypatch
):
    state = session(deadline=time.monotonic() + 0.5)
    state.ready = True
    state.sequence = pid1.sequence = 2
    state.deadline = time.time() + 10 if previous_active else None

    def miss_ack(_started):
        pid1.fresh = time.monotonic() - 1
        return False

    monkeypatch.setattr(pid1, "await_ack", miss_ack)
    with pytest.raises(HyperlightPodDetached):
        pid1.handle(
            {"op": "begin", "deadline": time.monotonic() + 10, "expires_at": time.time() + 10}
        )
    assert pid1.outgoing.get_nowait()["event"] == "begin"
    withdrawn = pid1.outgoing.get_nowait()
    pid1.controller_message(sealed("resume", 1))
    snapshot = pid1.outgoing.get_nowait()
    assert withdrawn["event"] == "end" and snapshot["event"] == "resumed"
    assert (
        supervise_events(
            state,
            [
                withdrawn,
                snapshot,
                {"event": "begin", "sequence": 4, "expires_at": time.time() + 10},
                {"event": "end", "sequence": 4},
            ],
        )
        == ""
    )
    assert state.resumed and state.sequence == 4 and state.deadline is None
    assert not pid1.retired.is_set()


@pytest.mark.parametrize("sequence", [0, 2, True, 1.0, "1"])
def test_reattach_rejects_an_invalid_withdrawal_sequence(sequence):
    state = session(deadline=time.monotonic() + 0.5)
    state.ready = True
    with pytest.raises(HyperlightWorkerError, match="lifecycle"):
        supervise_events(state, [{"event": "end", "sequence": sequence}])


@pytest.mark.parametrize("resuming", [False, True])
def test_a_withdrawal_without_begin_requires_an_unconfirmed_resume(resuming):
    state = session(deadline=time.monotonic() + 0.5)
    state.ready = True
    state.resumed = resuming
    with pytest.raises(HyperlightWorkerError, match="lifecycle"):
        supervise_events(state, [{"event": "end", "sequence": 1}], resuming=resuming)


@pytest.mark.parametrize(
    "following",
    [
        {"event": "end", "sequence": 1},
        {"event": "begin", "sequence": 1, "expires_at": FUTURE},
        {"event": "resumed", "sequence": 0, "expires_at": None, "acknowledged": False},
        {"event": "resumed", "sequence": 1, "expires_at": FUTURE, "acknowledged": False},
        {"event": "resumed", "sequence": 1, "expires_at": None, "acknowledged": True},
    ],
)
def test_a_deferred_withdrawal_requires_a_matching_idle_snapshot(following):
    state = session(deadline=time.monotonic() + 0.5)
    state.ready = True
    with pytest.raises(HyperlightWorkerError):
        supervise_events(state, [{"event": "end", "sequence": 1}, following])
    assert state.sequence == 0 and not state.resumed


@pytest.mark.parametrize("active", [False, True])
def test_a_deferred_withdrawal_preserves_deadlines_until_confirmed(active):
    state = session(deadline=time.monotonic() + 0.5)
    state.ready = True
    deadline = state.deadline = time.time() + 0.05 if active else None
    ended = supervise_events(state, [{"event": "end", "sequence": 1}])
    assert ended == ("" if active else "reconnection was not confirmed")
    assert state.sequence == 0 and state.deadline == deadline and not state.resumed


@pytest.mark.parametrize("answered", [False, True])
def test_an_attach_the_pod_stops_answering_ends(monkeypatch, answered):
    monkeypatch.setattr(kubernetes, "LEASE_SECONDS", 0.3)
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
            returncode=0,
        )
        if answered:
            os.write(
                sink, frame({"event": "alive", "pod_uid": "pod-uid", "generation": "generation"})
            )
        else:
            # Silence before PID 1's first answer is its startup, which PID 1 bounds itself.
            threading.Timer(0.8, stopped.set).start()
        try:
            ended = controller._supervise(stream, state, readers)
        finally:
            stopped.set()
            os.close(sink)
            for reader in readers:
                reader.join(timeout=2)
    assert ended == ("pod stopped answering" if answered else "attach exited with 0")


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

    def __init__(self, stdin: int, budget: int | None = None) -> None:
        self.budget = budget
        source, self.sink = os.pipe()
        self.stdout = os.fdopen(source, "rb")
        self.stderr = io.BytesIO()
        self.stdin = self
        self.pod_stdin = stdin
        self.returncode: int | None = None
        self.guard = threading.Lock()
        # A partition: nothing reaches either side, and what the controller wrote arrives late.
        self.stalled = False
        self.held: list[bytes] = []

    def deliver(self, payload: bytes) -> None:
        with self.guard:
            if self.returncode is None and not self.stalled:
                os.write(self.sink, payload)

    def heal(self) -> None:
        """The held frames reach PID 1 even when this attach's process has since exited."""
        with self.guard:
            self.stalled = False
            for payload in self.held:
                os.write(self.pod_stdin, payload)
            self.held.clear()

    def write(self, payload: bytes) -> None:
        if self.returncode is not None:
            raise BrokenPipeError
        with self.guard:
            if self.stalled:
                self.held.append(payload)
                return
        if self.budget is not None and len(payload) > self.budget:
            # The attach dies partway through this write.
            os.write(self.pod_stdin, payload[: self.budget])
            self.drop()
            raise BrokenPipeError
        if self.budget is not None:
            self.budget -= len(payload)
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
        self.budgets: list[int] = []
        self.reachable = threading.Event()
        self.reachable.set()

    def _same_container(self, name, container, session):
        return True if self.reachable.is_set() else None

    def _attach(self, name):
        budget = self.budgets.pop(0) if self.budgets else None
        self.attaches.append(Attach(self.stdin, budget))
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


def test_a_withdrawn_begin_does_not_overlap_after_a_stalled_attach_recovers(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    monkeypatch.setattr(kubernetes, "LEASE_SECONDS", 1.0)
    pid1 = Supervisor(LAUNCH, ["application"])
    pid1.worker = 100
    monkeypatch.setattr(
        pid1, "start_owner", lambda binding: pid1.emit("ready", owner_pid=2, platform={})
    )
    stdin_read, stdin_write = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=os.fdopen(stdin_read, "rb")))
    pod = Pod(pid1, stdin_write)
    state = session()
    stop = threading.Event()
    errors = []

    def hold():
        try:
            pod._hold(NAME, "c1", state)
        except HyperlightWorkerError as error:
            errors.append(error)

    threads = [
        threading.Thread(target=pid1.read_controller, daemon=True),
        threading.Thread(target=pump, args=(pid1, pod, stop), daemon=True),
        threading.Thread(target=hold, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        wait_for(lambda: state.ready and pid1.connected)
        attach = pod.attaches[0]
        write, deliver = attach.write, attach.deliver
        pending_writes, pending_events = [], []
        transport = threading.RLock()
        blocked = True

        def buffer_write(payload):
            with transport:
                if blocked:
                    pending_writes.append(payload)
                else:
                    write(payload)

        def buffer_event(payload):
            with transport:
                if blocked:
                    pending_events.append(payload)
                else:
                    deliver(payload)

        monkeypatch.setattr(attach, "write", buffer_write)
        monkeypatch.setattr(attach, "deliver", buffer_event)
        # Both directions remain connected while frames wait for the network to recover.
        pid1.fresh = time.monotonic() + 0.2
        with pytest.raises(HyperlightPodDetached, match="disconnected"):
            pid1.handle(
                {"op": "begin", "deadline": time.monotonic() + 30, "expires_at": time.time() + 30}
            )
        wait_for(lambda: pid1.outgoing.empty() and bool(pending_events))
        with transport:
            blocked = False
            for payload in pending_writes:
                write(payload)
            for payload in pending_events:
                deliver(payload)
        # The late pings cannot renew a stale PID 1, so the controller resumes on a new attach.
        wait_for(lambda: state.resumed and time.monotonic() < pid1.fresh)
        pid1.handle(
            {"op": "begin", "deadline": time.monotonic() + 30, "expires_at": time.time() + 30}
        )
        pid1.handle({"op": "end"})
        wait_for(lambda: state.sequence == 2 and state.deadline is None)
        assert not errors and not pid1.retired.is_set()
        assert len(pod.attaches) == 2 and state.interruptions == ["pod stopped answering"]
    finally:
        state.session_deadline = 0
        stop.set()
        pid1.retire("test finished")
        for attach in pod.attaches:
            attach.drop()
        os.close(stdin_write)
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)


def test_a_session_survives_a_partition_that_holds_the_attach_open(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    monkeypatch.setattr(_pod_supervisor, "LEASE_SECONDS", 1.0)
    monkeypatch.setattr(kubernetes, "LEASE_SECONDS", 1.0)
    monkeypatch.setattr(kubernetes, "PING_SECONDS", 0.2)
    pid1 = Supervisor(replace(LAUNCH, recovery_seconds=5), ["application"])
    pid1.worker = 100
    monkeypatch.setattr(
        pid1, "start_owner", lambda binding: pid1.emit("ready", owner_pid=2, platform={})
    )
    stdin_read, stdin_write = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=os.fdopen(stdin_read, "rb")))
    pod = Pod(pid1, stdin_write)
    state = session(recovery=5)
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
        first = pod.attaches[0]
        pod.reachable.clear()
        first.stalled = True
        # A call begun in the partition is withdrawn once PID 1's lease goes stale.
        call = time.time() + 30
        with pytest.raises(HyperlightPodDetached):
            pid1.handle({"op": "begin", "deadline": time.monotonic() + 30, "expires_at": call})
        wait_for(lambda: state.interrupted == "pod stopped answering")
        assert first.returncode is not None and first.held

        # The held pings arrive after the controller gave up on that attach; they renew nothing.
        counter = pid1.counter
        first.heal()
        wait_for(lambda: pid1.counter > counter)
        assert time.monotonic() >= pid1.fresh
        with pytest.raises(HyperlightPodDetached):
            pid1.handle({"op": "validate"})
        pod.reachable.set()
        wait_for(lambda: len(pod.attaches) == 2 and state.resumed)
        assert state.interruptions == ["pod stopped answering"]
        assert (state.sequence, state.deadline) == (1, None)
        pid1.handle({"op": "begin", "deadline": time.monotonic() + 30, "expires_at": call})
        wait_for(lambda: state.sequence == 2 and state.deadline == call)
        pid1.handle({"op": "end"})
        wait_for(lambda: state.deadline is None)
        assert not pid1.retired.is_set()
    finally:
        # An expired session stops the controller before the pipe's descriptor can be reused.
        state.session_deadline = 0
        stop.set()
        pid1.retire("test finished")
        for attach in pod.attaches:
            attach.drop()
        for thread in threads[1:]:
            thread.join(timeout=5)
        os.close(stdin_write)
        threads[0].join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("budget", [0, 25], ids=["hello-lost", "hello-torn"])
def test_a_first_attach_that_dies_before_its_hello_is_recovered(monkeypatch, budget):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    pid1 = Supervisor(replace(LAUNCH, recovery_seconds=3), ["application"])
    owners: list[object] = []

    def start_owner(binding):
        owners.append(binding)
        pid1.emit("ready", owner_pid=2, platform={"kernel": "k"})

    monkeypatch.setattr(pid1, "start_owner", start_owner)
    stdin_read, stdin_write = os.pipe()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=os.fdopen(stdin_read, "rb")))
    pod = Pod(pid1, stdin_write)
    pod.budgets = [budget]
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
        wait_for(lambda: state.resumed)
        assert len(pod.attaches) == 2 and len(owners) == 1
        assert pid1.connected and not pid1.retired.is_set()
        assert state.ready and state.platform == {"kernel": "k"}
    finally:
        state.session_deadline = 0
        stop.set()
        pid1.retire("test finished")
        for attach in pod.attaches:
            attach.drop()
        for thread in threads[1:]:
            thread.join(timeout=5)
        os.close(stdin_write)
        threads[0].join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
