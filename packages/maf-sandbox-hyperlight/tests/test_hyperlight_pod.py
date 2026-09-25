"""Ownership, deadline and durable cleanup refusals for the container integration."""

from __future__ import annotations

import asyncio
import base64
import copy
import ctypes
import gzip
import hashlib
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_hyperlight import (
    HyperlightPodConfig,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
    _backend,
    _pod_bootstrap,
    _pod_supervisor,
    kubernetes,
)
from maf_sandbox_hyperlight._pod import FRAME_LIMIT, frame, unframe, verify_container
from maf_sandbox_hyperlight._pod_config import PodLaunch
from maf_sandbox_hyperlight._pod_supervisor import Supervisor
from maf_sandbox_hyperlight.kubernetes import (
    HyperlightPodCleanupPending,
    HyperlightPodController,
    HyperlightPodPlatformError,
    HyperlightPodTemplate,
    confirmed_exit,
    ownership_name,
    pod_manifest,
)

KEY = SandboxKey("tenant:user", "conversation", "agent")
KIND = "codeact"
BINDING = HyperlightPodConfig(KEY, KIND, "pod-uid", "generation", 4 * 1024**3)
LAUNCH = PodLaunch(ownership_name(KEY, KIND), "pod-uid", "generation", 4 * 1024**3)
IDENTITY = {"scope": KEY.scope, "thread_id": KEY.thread_id, "agent_id": KEY.agent_id, "kind": KIND}
TEMPLATE = HyperlightPodTemplate(
    "registry.example/runtime@sha256:" + "a" * 64, ("python", "app.py")
)
SPEC = SandboxSpec(kind=KIND, work_dir=None, requires=frozenset({Capability.RUN_CODE}))


def test_container_budget_cannot_masquerade_as_worker_memory():
    with pytest.raises(ValueError, match="no per-worker"):
        HyperlightSandboxConfig(pod=BINDING)
    with pytest.raises(ValueError, match="requires max_worker_memory_bytes"):
        HyperlightSandboxConfig(max_worker_memory_bytes=None)
    with pytest.raises(ValueError, match="no per-worker"):
        HyperlightSandboxConfig(
            pod=BINDING, max_worker_memory_bytes=None, linux_cgroup_root="/other"
        )
    config = HyperlightSandboxConfig(pod=BINDING, max_worker_memory_bytes=None)
    assert config.pod == BINDING and config.max_worker_memory_bytes is None
    assert HyperlightPodConfig.from_mapping(BINDING.mapping()) == BINDING


@pytest.mark.parametrize("field", ["scope", "thread_id", "agent_id"])
def test_each_host_identity_axis_is_part_of_the_allocation(field):
    changed = replace(KEY, **{field: "different"})
    assert ownership_name(KEY, KIND) != ownership_name(changed, KIND)
    with pytest.raises(HyperlightWorkerError, match="another sandbox"):
        BINDING.authorize(changed, KIND)


def test_kind_and_call_scope_are_not_collapsed():
    assert ownership_name(KEY, KIND) != ownership_name(KEY, "another-kind")
    with pytest.raises(HyperlightWorkerError, match="another sandbox"):
        BINDING.authorize(KEY, "another-kind")
    with pytest.raises(ValueError, match="conversation"):
        ownership_name(replace(KEY, call_id="call"), KIND)
    with pytest.raises(ValueError, match="conversation"):
        replace(BINDING, key=replace(KEY, call_id="call"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", ""),
        ("generation", "x" * 1025),
        ("pod_uid", "a\0b"),
        ("memory_limit_bytes", True),
        ("memory_limit_bytes", 0),
    ],
)
def test_invalid_pod_binding_is_refused(field, value):
    with pytest.raises(ValueError):
        replace(BINDING, **{field: value})


def test_wrong_scope_refuses_before_host_or_worker_access(monkeypatch):
    def unexpected():
        raise AssertionError("wrong scope reached the host")

    monkeypatch.setattr(_backend, "check_host", unexpected)
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(pod=BINDING, max_worker_memory_bytes=None)
    )
    wrong = replace(KEY, agent_id="other")

    async def check():
        with pytest.raises(HyperlightWorkerError, match="another sandbox"):
            await backend.acquire(wrong, SPEC)
        assert await backend.dispose(wrong) is not None
        result = await backend.dispose_scope("wrong", KEY.thread_id)
        assert result.undisposed is not None

    asyncio.run(check())


def test_manifest_uses_upstream_resource_with_private_container_limits():
    key = SandboxKey("tenant-7f3a:user-91c2", "thread-5c2e0b", "analyst-b41d")
    kind = "kind-e83f"
    serialized = json.dumps(
        pod_manifest(key, kind, TEMPLATE, namespace="scoped-agents", generation="gen")
    )
    pod = json.loads(serialized)
    spec = pod["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["hostPID"] is False and spec["shareProcessNamespace"] is False
    assert spec["hostNetwork"] is False
    assert len(spec["containers"]) == 1
    container = spec["containers"][0]
    assert container["resources"]["limits"]["hyperlight.dev/hypervisor"] == "1"
    assert container["resources"]["limits"]["memory"] == str(TEMPLATE.memory_limit_bytes)
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert spec["securityContext"]["runAsUser"] == 65534
    assert all("hostPath" not in volume for volume in spec["volumes"])
    assert container["stdinOnce"] is True and container["tty"] is False
    env = {item["name"]: item.get("value") for item in container["env"]}
    launch = json.loads(env["MAF_HYPERLIGHT_POD_BINDING"])
    assert launch["owner"] == pod["metadata"]["name"] == ownership_name(key, kind)
    assert all(value not in serialized for value in (key.scope, key.thread_id, key.agent_id, kind))
    assert pod["metadata"]["finalizers"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "runtime:latest"),
        ("command", ()),
        ("memory_limit_bytes", 0),
        ("session_timeout", False),
        ("cpu_request_millis", 2000),
    ],
)
def test_unsafe_or_unbounded_template_is_refused(field, value):
    with pytest.raises(ValueError):
        replace(TEMPLATE, **{field: value})


@pytest.mark.parametrize("name", ["", "-a", "a-", "a.b", "A", "a" * 64])
def test_invalid_dns_labels_are_rejected_locally(name):
    with pytest.raises(ValueError, match="namespace"):
        HyperlightPodController(kubeconfig="config", context="context", namespace=name)
    with pytest.raises(ValueError, match="namespace"):
        pod_manifest(KEY, KIND, TEMPLATE, namespace=name, generation="gen")
    with pytest.raises(ValueError, match="ConfigMap"):
        replace(TEMPLATE, bundle_configmap=name, bundle_sha256="a" * 64)


@pytest.mark.parametrize("name", ["a", "0", "a-b", "a" * 63])
def test_valid_dns_label_boundaries_remain_accepted(name):
    HyperlightPodController(kubeconfig="config", context="context", namespace=name)
    template = replace(TEMPLATE, bundle_configmap=name, bundle_sha256="a" * 64)
    assert pod_manifest(KEY, KIND, template, namespace=name, generation="gen")


@pytest.fixture
def controls(tmp_path: Path):
    values = {
        "memory.max": str(BINDING.memory_limit_bytes),
        "memory.swap.max": "0",
        "cpu.max": "100000 100000",
        "pids.max": "100",
        "cgroup.controllers": "cpu memory pids",
    }
    for name, value in values.items():
        (tmp_path / name).write_text(value)
    return tmp_path


def test_runtime_container_controls_match_the_declared_budget(controls):
    verify_container(BINDING.memory_limit_bytes, root=controls)


@pytest.mark.parametrize(
    "name,value",
    [
        ("memory.max", "max"),
        ("memory.max", "1234"),
        ("memory.swap.max", "max"),
        ("cpu.max", "max 100000"),
        ("pids.max", "max"),
    ],
)
def test_absent_or_weaker_kernel_controls_refuse_startup(controls, name, value):
    (controls / name).write_text(value)
    with pytest.raises((ValueError, HyperlightWorkerError)):
        verify_container(BINDING.memory_limit_bytes, root=controls)


@pytest.mark.parametrize(
    "missing,match",
    [
        ("cgroup.controllers", "requires cgroup v2"),
        ("memory.swap.max", "cannot read cgroup memory.swap.max"),
    ],
)
def test_cgroup_v1_or_missing_swap_accounting_names_the_requirement(controls, missing, match):
    (controls / missing).unlink()
    with pytest.raises(HyperlightWorkerError, match=match):
        verify_container(BINDING.memory_limit_bytes, root=controls)


def test_platform_refusal_reaches_the_termination_message_with_its_own_exit(tmp_path):
    launch = {"owner": LAUNCH.owner, "generation": "generation", "memory_limit_bytes": 1}
    log = tmp_path / "termination-log"
    program = f"""
import json, os
from maf_sandbox_hyperlight import _pod_supervisor
from maf_sandbox_hyperlight._wire import HyperlightWorkerError
os.environ['MAF_HYPERLIGHT_POD_BINDING'] = {json.dumps(launch)!r}
os.environ['MAF_HYPERLIGHT_POD_UID'] = 'pod-uid'
_pod_supervisor.TERMINATION_LOG = {str(log)!r}
def refuse(launch):
    raise HyperlightWorkerError('KVM initialization failed (Permission denied)')
_pod_supervisor.verify_init = refuse
_pod_supervisor.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, "application"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 78
    assert log.read_text() == (
        "maf-hyperlight: unsupported platform: KVM initialization failed (Permission denied)"
    )


def test_ready_event_carries_the_observed_platform():
    observed = {"kernel": "6.8.0-1067-azure", "memory.swap.max": "0"}
    payload = frame(
        {"event": "ready", "pod_uid": "pod-uid", "generation": "generation", "platform": observed}
    )
    stopped = threading.Event()
    # An open pipe keeps the transport alive until the session deadline.
    source, sink = os.pipe()
    os.write(sink, payload)
    platform: dict[str, str] = {}
    readers = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")
    with open(source, "rb") as control:
        stream = SimpleNamespace(
            stdout=control,
            stderr=io.BytesIO(),
            stdin=io.BytesIO(),
            poll=lambda: 0 if stopped.is_set() else None,
        )
        try:
            controller._supervise(
                stream,
                "pod-uid",
                "generation",
                IDENTITY,
                time.monotonic() + 0.5,
                bytearray(),
                platform,
                readers,
            )
        finally:
            stopped.set()
            os.close(sink)
            for reader in readers:
                reader.join(timeout=2)
    assert platform == observed


@pytest.mark.parametrize("raw", [b"{}", b"[]\n", b"{" + b"x" * FRAME_LIMIT + b"}\n", b"not-json\n"])
def test_lifecycle_frames_cannot_be_truncated_or_unbounded(raw):
    with pytest.raises((ValueError, HyperlightWorkerError)):
        unframe(raw)
    assert unframe(frame({"op": "end"})) == {"op": "end"}


@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(LAUNCH, ["application"])
    subject.connected = True
    subject.lease = time.monotonic() + 10
    subject.worker = 100
    return subject


def test_execution_policy_cannot_change_after_worker_disposal(supervisor):
    supervisor.handle({"op": "policy", "digest": "a" * 64})
    supervisor.handle({"op": "policy", "digest": "a" * 64})
    supervisor.worker = None
    with pytest.raises(HyperlightWorkerError, match="policy cannot change"):
        supervisor.handle({"op": "policy", "digest": "b" * 64})


def test_one_resident_worker_includes_the_idle_worker(supervisor):
    with pytest.raises(HyperlightWorkerError, match="one resident"):
        supervisor.register_worker(101)


def test_deadline_is_registered_before_begin_is_acknowledged(supervisor, monkeypatch):
    observed = []

    def emit(event, **fields):
        observed.append((event, fields, supervisor.deadline))
        supervisor.controller_message(
            {
                "op": "ack",
                "sequence": fields["sequence"],
                "pod_uid": BINDING.pod_uid,
                "generation": BINDING.generation,
            }
        )

    monkeypatch.setattr(supervisor, "emit", emit)
    deadline = time.monotonic() + 1
    supervisor.handle({"op": "begin", "deadline": deadline, "expires_at": time.time() + 1})
    assert observed[0][0] == "begin" and observed[0][2] == deadline
    with pytest.raises(HyperlightWorkerError, match="overlapping"):
        supervisor.handle({"op": "begin", "deadline": deadline, "expires_at": time.time() + 1})
    supervisor.handle({"op": "end"})
    assert supervisor.deadline is None


def test_unacknowledged_deadline_retires_the_session(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor.ack, "wait", lambda timeout: False)
    with pytest.raises(HyperlightWorkerError, match="retired"):
        supervisor.handle(
            {"op": "begin", "deadline": time.monotonic() + 1, "expires_at": time.time() + 1}
        )
    assert supervisor.retired.is_set()


def test_stale_controller_cannot_extend_the_lease(supervisor):
    before = supervisor.lease
    with pytest.raises(HyperlightWorkerError, match="generation differs"):
        supervisor.controller_message(
            {"op": "ping", "pod_uid": "old", "generation": BINDING.generation}
        )
    assert supervisor.lease == before


def test_late_heartbeat_cannot_revive_an_expired_lease(supervisor):
    supervisor.lease = time.monotonic() - 1
    with pytest.raises(HyperlightWorkerError, match="cannot be renewed"):
        supervisor.controller_message(
            {"op": "ping", "pod_uid": BINDING.pod_uid, "generation": BINDING.generation}
        )
    assert supervisor.retired.is_set()


def hello(**changes: object) -> dict[str, object]:
    return {"op": "hello", "pod_uid": "pod-uid", "generation": "generation", **IDENTITY, **changes}


def test_hello_binds_the_identity_the_pod_is_named_for(supervisor, monkeypatch):
    started = []
    monkeypatch.setattr(supervisor, "start_owner", started.append)
    supervisor.connected = False
    supervisor.controller_message(hello())
    assert started == [BINDING] and supervisor.connected


@pytest.mark.parametrize("field", ["scope", "thread_id", "agent_id", "kind"])
def test_hello_for_another_identity_cannot_bind_the_pod(supervisor, monkeypatch, field):
    started = []
    monkeypatch.setattr(supervisor, "start_owner", started.append)
    supervisor.connected = False
    with pytest.raises(HyperlightWorkerError, match="does not name this pod"):
        supervisor.controller_message(hello(**{field: "other"}))
    with pytest.raises(ValueError, match="ownership fields"):
        supervisor.controller_message(hello(**{field: None}))
    assert not started and not supervisor.connected


@pytest.mark.parametrize(
    "field,value", [("owner", None), ("pod_uid", ""), ("memory_limit_bytes", True)]
)
def test_invalid_pod_launch_is_refused(field, value):
    with pytest.raises(ValueError):
        replace(LAUNCH, **{field: value})


def test_controller_hello_carries_the_identity_the_supervisor_binds(supervisor, monkeypatch):
    stopped = threading.Event()
    stream = SimpleNamespace(
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        stdin=io.BytesIO(),
        poll=lambda: 0 if stopped.is_set() else None,
    )
    readers = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")
    try:
        controller._supervise(
            stream,
            "pod-uid",
            "generation",
            IDENTITY,
            time.monotonic() + 2,
            bytearray(),
            {},
            readers,
        )
        written = time.monotonic() + 2
        while not stream.stdin.getvalue() and time.monotonic() < written:
            time.sleep(0.01)
    finally:
        stopped.set()
        for reader in readers:
            reader.join(timeout=2)
    started = []
    monkeypatch.setattr(supervisor, "start_owner", started.append)
    supervisor.connected = False
    supervisor.controller_message(unframe(stream.stdin.getvalue().splitlines(True)[0]))
    assert started == [BINDING]


@pytest.mark.parametrize(
    "payload,reason", [(b"", "stream closed"), (b"not-json\n", "JSONDecodeError")]
)
def test_controller_input_retirement_preserves_failure_reason(
    supervisor, monkeypatch, payload, reason
):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    supervisor.read_controller()
    assert supervisor.retired.is_set() and supervisor.ack.is_set()
    assert reason in supervisor.reason


def test_controller_input_overflow_retires_with_diagnostic(supervisor, monkeypatch):
    supervisor.incoming = queue.Queue(maxsize=1)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(frame({}) * 2)))
    supervisor.read_controller()
    assert supervisor.retired.is_set() and "Full" in supervisor.reason


@pytest.mark.parametrize("replies,refused", [((0, 0), False), ((-1, 0), True), ((0, 1), True)])
def test_init_refuses_a_dumpable_supervisor(monkeypatch, replies, refused):
    calls: list[tuple[int, ...]] = []

    def prctl(*arguments: int) -> int:
        calls.append(arguments)
        return replies[len(calls) - 1]

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=prctl))
    if refused:
        with pytest.raises(HyperlightWorkerError, match="non-dumpable"):
            _pod_supervisor.make_undumpable()
    else:
        _pod_supervisor.make_undumpable()
    assert calls[0] == (_pod_supervisor.PR_SET_DUMPABLE, 0, 0, 0, 0)


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() == 0,
    reason="needs Linux /proc, and root bypasses the dumpable check",
)
def test_same_uid_process_cannot_open_an_undumpable_supervisor_stdin():
    reachable = {}
    for undumpable in (False, True):
        program = (
            "import sys, time\n"
            "from maf_sandbox_hyperlight import _pod_supervisor\n"
            f"if {undumpable}: _pod_supervisor.make_undumpable()\n"
            "print('ready', flush=True); time.sleep(30)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        try:
            assert child.stdout is not None and child.stdout.readline() == b"ready\n"
            try:
                os.close(os.open(f"/proc/{child.pid}/fd/0", os.O_WRONLY))
                reachable[undumpable] = True
            except PermissionError:
                reachable[undumpable] = False
        finally:
            child.kill()
            child.wait(timeout=10)
    assert reachable == {False: True, True: False}


@pytest.mark.parametrize("error", ["SystemExit(0)", "KeyboardInterrupt()"])
def test_init_forces_failure_exit_without_waiting_for_python_threads(error):
    launch = {"owner": LAUNCH.owner, "generation": "generation", "memory_limit_bytes": 1}
    program = f"""
import json, os, threading
import sys
from maf_sandbox_hyperlight import _pod_supervisor
os.environ['MAF_HYPERLIGHT_POD_BINDING'] = {json.dumps(launch)!r}
os.environ['MAF_HYPERLIGHT_POD_UID'] = 'pod-uid'
def refuse(launch):
    print('verifying init', file=sys.stderr, flush=True)
    threading.Thread(target=threading.Event().wait, daemon=False).start()
    raise {error}
_pod_supervisor.verify_init = refuse
_pod_supervisor.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 71
    assert b"verifying init" in result.stderr
    assert b"pod supervisor refused startup" in result.stderr


def test_closed_full_control_stream_cannot_acknowledge_queued_work(monkeypatch):
    payload = frame({"event": "ready", "pod_uid": "pod-uid", "generation": "generation"})
    for sequence in range(1, 33):
        for event in ("begin", "end"):
            payload += frame(
                {
                    "event": event,
                    "sequence": sequence,
                    "pod_uid": "pod-uid",
                    "generation": "generation",
                    "expires_at": time.time() + 60,
                }
            )

    # Finish the reader before supervision so the queue is deterministically full.
    class ReadFirstThread(threading.Thread):
        def __init__(self, *, target, **kwargs):
            super().__init__(target=target, **kwargs)
            self.is_reader = target.__name__ == "read"

        def start(self):
            super().start()
            if self.is_reader:
                self.join(timeout=2)
                assert not self.is_alive()

    monkeypatch.setattr(threading, "Thread", ReadFirstThread)
    stopped = threading.Event()
    stream = SimpleNamespace(
        stdout=io.BytesIO(payload),
        stderr=io.BytesIO(),
        stdin=io.BytesIO(),
        poll=lambda: 0 if stopped.is_set() else None,
    )
    readers = []
    controller = HyperlightPodController(kubeconfig="config", context="context", namespace="agents")
    try:
        controller._supervise(
            stream,
            "pod-uid",
            "generation",
            IDENTITY,
            time.monotonic() + 2,
            bytearray(),
            {},
            readers,
        )
    finally:
        stopped.set()
        for reader in readers:
            reader.join(timeout=2)
    assert all(unframe(line)["op"] != "ack" for line in stream.stdin.getvalue().splitlines(True))


def terminal_pod():
    return {
        "metadata": {
            "name": ownership_name(KEY, KIND),
            "uid": "pod-uid",
            "resourceVersion": "2",
            "annotations": {"sandbox.sokol.ai/generation": "generation"},
            "finalizers": ["sandbox.sokol.ai/confirmed-stop"],
        },
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "sandbox",
                    "state": {
                        "terminated": {
                            "reason": "Completed",
                            "exitCode": 0,
                            "finishedAt": "2026-09-22T21:00:00Z",
                            "containerID": "containerd://exact-container",
                        }
                    },
                }
            ],
        },
    }


def test_termination_proof_requires_exact_runtime_identity():
    pod = terminal_pod()
    assert confirmed_exit(pod, "pod-uid") == 0
    assert confirmed_exit(pod, "different") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", "ContainerStatusUnknown"),
        ("reason", "NodeLost"),
        ("containerID", ""),
        ("finishedAt", "0001-01-01T00:00:00Z"),
        ("exitCode", True),
    ],
)
def test_synthetic_terminal_states_are_not_cleanup_proof(field, value):
    pod = terminal_pod()
    pod["status"]["containerStatuses"][0]["state"]["terminated"][field] = value
    assert confirmed_exit(pod, "pod-uid") is None


class FakeController(HyperlightPodController):
    def __init__(self, pod=None):
        super().__init__(kubeconfig="config", context="context", namespace="namespace")
        self.pod = terminal_pod() if pod is None else pod
        self.ledger: dict[str, Any] = {
            "metadata": {"uid": "ledger-uid", "resourceVersion": "1"},
            "data": {"generation": "generation", "pod_uid": "pod-uid", "state": "running"},
        }
        self.calls: list[Any] = []

    def api(self, *arguments, body: dict[str, Any] | None = None):
        self.calls.append((arguments, copy.deepcopy(body)))
        if arguments[0] == "get":
            return copy.deepcopy(self.ledger if arguments[1] == "configmap" else self.pod)
        if arguments[0] == "replace":
            assert body is not None
            if "data" in body:
                self.ledger = copy.deepcopy(body)
            else:
                self.pod = copy.deepcopy(body)
                if body["metadata"].get("deletionTimestamp") and not body["metadata"]["finalizers"]:
                    self.pod = {}
            return copy.deepcopy(body)
        if arguments[0] == "delete":
            assert body is not None
            if "/pods/" in arguments[2]:
                assert body["preconditions"]["uid"] == "pod-uid"
                self.pod = {}
            else:
                assert self.ledger["data"]["state"] in {"stopped", "rejected"}
                assert body["preconditions"]["uid"] == "ledger-uid"
                self.ledger = {}
            return {"status": "Success"}
        raise AssertionError(arguments)


class RejectingController(FakeController):
    def __init__(self, error):
        super().__init__(pod={})
        self.ledger = {}
        self.error = error

    def api(self, *arguments, body=None):
        if arguments[0] == "create":
            self.calls.append((arguments, copy.deepcopy(body)))
            assert body is not None
            if body["kind"] == "ConfigMap":
                assert not self.ledger, "scope was not released"
                self.ledger = copy.deepcopy(body)
                self.ledger["metadata"]["uid"] = "ledger-uid"
                return copy.deepcopy(self.ledger)
            raise self.error
        return super().api(*arguments, body=body)


def refused_pod(message):
    pod = terminal_pod()
    ended = pod["status"]["containerStatuses"][0]["state"]["terminated"]
    ended.update({"reason": "Error", "exitCode": 78, "message": message})
    return pod


class SupervisingController(FakeController):
    def api(self, *arguments, body=None):
        if arguments[0] == "create":
            assert body is not None
            if body["kind"] == "ConfigMap":
                self.ledger["data"].update(body["data"])
                return copy.deepcopy(self.ledger)
            self.pod["metadata"]["annotations"] = body["metadata"]["annotations"]
            return copy.deepcopy(self.pod)
        return super().api(*arguments, body=body)


def test_platform_refusal_raises_after_confirmed_cleanup():
    controller = SupervisingController(
        refused_pod("maf-hyperlight: unsupported platform: pod mode requires cgroup v2")
    )
    with pytest.raises(HyperlightPodPlatformError, match="requires cgroup v2"):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert not controller.ledger and not controller.pod


def test_an_application_exit_78_is_not_a_platform_refusal():
    controller = SupervisingController(refused_pod("application configuration error"))
    assert controller.supervise(KEY, KIND, TEMPLATE).exit_code == 78


def test_startup_timeout_names_the_scheduler_reason():
    pod = terminal_pod()
    pod["status"] = {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/3 nodes are available: 3 Insufficient hyperlight.dev/hypervisor.",
            }
        ],
    }
    controller = FakeController(pod)
    name = ownership_name(KEY, KIND)
    with pytest.raises(TimeoutError, match="Unschedulable: 0/3 nodes .* Insufficient hyperlight"):
        controller._await_running(name, "pod-uid", time.monotonic() + 0.1)


def rejected_create(message):
    return subprocess.CalledProcessError(1, ["kubectl", "create"], output="", stderr=message)


QUOTA_REJECTION = 'Error from server (Forbidden): error when creating "STDIN": exceeded quota\n'


@pytest.mark.parametrize(
    "message",
    [
        QUOTA_REJECTION,
        "Warning: admission policy reports a resource budget warning\n" + QUOTA_REJECTION,
        'Error from server (BadRequest): error when creating "STDIN": admission denied\n',
        f'The Pod "{ownership_name(KEY, KIND)}" is invalid: spec: invalid value\n',
        f'The Pod "{ownership_name(KEY, KIND)}" is invalid:\n* spec: invalid value\n',
    ],
)
def test_definitive_create_rejection_releases_only_the_reserved_ledger(message):
    controller = RejectingController(rejected_create(message))
    for _ in range(2):
        with pytest.raises(subprocess.CalledProcessError):
            controller.supervise(KEY, KIND, TEMPLATE)
        assert not controller.ledger
    deletes = [(args, body) for args, body in controller.calls if args[0] == "delete"]
    assert len(deletes) == 2
    assert all("/configmaps/" in args[2] for args, _ in deletes)
    assert all(body["preconditions"] == {"uid": "ledger-uid"} for _, body in deletes)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("kubectl", 15),
        rejected_create('Error from server (AlreadyExists): pods "existing" already exists'),
        rejected_create('Error from server (InternalError): error when creating "STDIN": failed'),
        rejected_create("Unable to connect to the server: Forbidden"),
        rejected_create("error: failed exec auth: " + QUOTA_REJECTION),
        rejected_create("Warning: policy warning\nUnable to connect to the server: Forbidden"),
    ],
)
def test_ambiguous_create_failure_keeps_the_scope_reserved(error):
    controller = RejectingController(error)
    with pytest.raises(HyperlightPodCleanupPending, match="allocation retained"):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert controller.ledger["data"]["state"] == "allocating"
    assert not any(args[0] == "delete" for args, _ in controller.calls)


def test_rejection_cannot_release_a_scope_with_an_existing_pod():
    controller = RejectingController(rejected_create(QUOTA_REJECTION))
    controller.pod = terminal_pod()
    with pytest.raises(HyperlightPodCleanupPending, match="allocation retained"):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert controller.ledger["data"]["state"] == "allocating"
    assert not any(args[0] == "delete" for args, _ in controller.calls)


def test_rejection_with_unavailable_pod_lookup_retains_allocation(monkeypatch):
    controller = RejectingController(rejected_create(QUOTA_REJECTION))
    api = controller.api

    def unavailable(*args, **kwargs):
        if args[:2] == ("get", "pod"):
            raise OSError("lookup unavailable")
        return api(*args, **kwargs)

    monkeypatch.setattr(controller, "api", unavailable)
    with pytest.raises(HyperlightPodCleanupPending):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert controller.ledger["data"]["state"] == "allocating"
    assert not any(args[0] == "delete" for args, _ in controller.calls)


def test_rejection_receipt_cannot_delete_a_replacement_pod():
    controller = FakeController()
    controller.ledger["data"].update({"state": "rejected", "pod_uid": ""})
    with pytest.raises(HyperlightPodCleanupPending, match="pod exists"):
        controller.recover(KEY, KIND)
    assert controller.ledger and controller.pod
    assert all(args[0] == "get" for args, _ in controller.calls)


def test_running_ledger_update_failure_still_attempts_confirmed_cleanup(monkeypatch):
    controller = FakeController()
    api = controller.api

    def fail_running_update(*args, body=None):
        if args[0] == "create":
            assert body is not None
            if body["kind"] == "ConfigMap":
                controller.ledger["data"].update(body["data"])
                controller.ledger["data"]["generation"] = "generation"
                return copy.deepcopy(controller.ledger)
            return copy.deepcopy(controller.pod)
        if args[0] == "replace" and body and body.get("data", {}).get("state") == "running":
            raise OSError("running update failed")
        return api(*args, body=body)

    monkeypatch.setattr(controller, "api", fail_running_update)
    with pytest.raises(OSError, match="running update failed"):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert not controller.ledger and not controller.pod


def test_rejection_receipt_allows_retry_after_ledger_delete_fails(monkeypatch):
    controller = RejectingController(rejected_create(QUOTA_REJECTION))
    remove = controller._delete

    def unavailable(*args):
        raise OSError("API unavailable")

    monkeypatch.setattr(controller, "_delete", unavailable)
    with pytest.raises(HyperlightPodCleanupPending):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert controller.ledger["data"]["state"] == "rejected"
    monkeypatch.setattr(controller, "_delete", remove)
    assert controller.recover(KEY, KIND) == 71
    assert not controller.ledger


def test_manifest_failure_does_not_reserve_the_scope(monkeypatch):
    controller = RejectingController(rejected_create(QUOTA_REJECTION))

    def unavailable(*args, **kwargs):
        raise OSError("bootstrap source unavailable")

    monkeypatch.setattr(kubernetes, "pod_manifest", unavailable)
    with pytest.raises(OSError, match="bootstrap source"):
        controller.supervise(KEY, KIND, TEMPLATE)
    assert not controller.calls


@pytest.mark.parametrize(
    "scope,match",
    [("x" * 1025, "bounded strings"), ("\U0001f600" * 1024, "lifecycle frame")],
)
def test_identity_the_pod_cannot_receive_reserves_nothing(scope, match):
    controller = FakeController()
    with pytest.raises(ValueError, match=match):
        controller.supervise(replace(KEY, scope=scope), KIND, TEMPLATE)
    assert not controller.calls


def test_cleanup_persists_proof_before_deleting_the_pod_and_reservation():
    controller = FakeController()
    assert controller.recover(KEY, KIND) == 0
    assert controller.pod == controller.ledger == {}
    mutations = [(args[0], body) for args, body in controller.calls if args[0] != "get"]
    assert mutations[0][0] == "replace"
    assert mutations[0][1]["data"]["state"] == "stopped"
    assert [op for op, _ in mutations] == ["replace", "replace", "delete", "delete"]


def test_disappeared_pod_is_not_success_and_keeps_the_reservation():
    controller = FakeController(pod={})
    with pytest.raises(HyperlightPodCleanupPending, match="without termination proof"):
        controller.recover(KEY, KIND)
    assert controller.ledger["data"]["state"] == "running"
    assert all(args[0] == "get" for args, _ in controller.calls)


def test_recovery_finishes_after_pod_deletion_when_proof_was_saved():
    controller = FakeController(pod={})
    controller.ledger["data"].update({"state": "stopped", "exit_code": "70"})
    assert controller.recover(KEY, KIND) == 70
    assert not controller.ledger


def test_stale_recovery_never_deletes_a_replacement():
    pod = terminal_pod()
    pod["metadata"]["uid"] = "replacement"
    controller = FakeController(pod)
    with pytest.raises(HyperlightPodCleanupPending, match="UID differs"):
        controller.recover(KEY, KIND)
    assert controller.ledger and controller.pod
    assert all(args[0] == "get" for args, _ in controller.calls)


def never_started_pod(*, scheduled: bool = True, bootstrap: bool = False) -> dict[str, Any]:
    pod = terminal_pod()
    pod["metadata"].update({"deletionTimestamp": "2026-09-23T06:00:00Z", "generation": 2})
    pod["spec"] = {"containers": [{"name": "sandbox"}]}
    pod["status"] = {"phase": "Failed", "observedGeneration": 2}
    if not scheduled:
        pod["status"]["phase"] = "Pending"
        return pod
    pod["spec"]["nodeName"] = "node"
    pod["status"]["conditions"] = [
        {"type": "PodReadyToStartContainers", "status": "False", "observedGeneration": 2}
    ]
    container = {
        "name": "sandbox",
        "restartCount": 0,
        "started": False,
        "ready": False,
        "imageID": "",
        "lastState": {},
        "state": {
            "terminated": {
                "reason": "ContainerStatusUnknown",
                "exitCode": 137,
                "message": "The container could not be located when the pod was terminated",
                "startedAt": None,
                "finishedAt": None,
            }
        },
    }
    pod["status"]["containerStatuses"] = [container]
    if bootstrap:
        pod["spec"]["initContainers"] = [{"name": "bootstrap"}]
        initializer = copy.deepcopy(container)
        initializer["name"] = "bootstrap"
        pod["status"]["initContainerStatuses"] = [initializer]
        container["state"] = {"waiting": {"reason": "PodInitializing"}}
    return pod


@pytest.mark.parametrize("mode", ["unscheduled", "image-pull", "init-image-pull"])
def test_never_started_cleanup_records_failure_before_releasing_ownership(mode):
    pod = never_started_pod(scheduled=mode != "unscheduled", bootstrap=mode == "init-image-pull")
    assert confirmed_exit(pod, "pod-uid") == 71
    assert confirmed_exit(pod, "other-uid") is None
    controller = FakeController(pod)
    assert controller.recover(KEY, KIND, retire=True) == 71
    mutations = [(args[0], body) for args, body in controller.calls if args[0] != "get"]
    assert mutations[0][1]["data"]["state"] == "stopped"
    assert mutations[0][1]["data"]["exit_code"] == "71"
    assert controller.pod == controller.ledger == {}


def test_unscheduled_pod_requires_a_deletion_fence():
    pod = never_started_pod(scheduled=False)
    del pod["metadata"]["deletionTimestamp"]
    assert confirmed_exit(pod, "pod-uid") is None


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("containerID", "containerd://old"),
        ("imageID", "sha256:old"),
        ("restartCount", 1),
        ("restartCount", False),
        ("started", True),
        ("ready", True),
        ("lastState", {"terminated": {"exitCode": 137}}),
    ],
)
def test_container_history_is_not_never_started_proof(bootstrap, field, value):
    pod = never_started_pod(bootstrap=bootstrap)
    statuses = pod["status"]["initContainerStatuses" if bootstrap else "containerStatuses"]
    statuses[0][field] = value
    assert confirmed_exit(pod, "pod-uid") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("message", "container disappeared"),
        ("exitCode", 0),
        ("containerID", "containerd://old"),
        ("startedAt", "2026-09-23T05:59:00Z"),
        ("finishedAt", "2026-09-23T06:00:00Z"),
    ],
)
def test_unknown_container_status_alone_is_not_termination_proof(field, value):
    pod = never_started_pod()
    pod["status"]["containerStatuses"][0]["state"]["terminated"][field] = value
    assert confirmed_exit(pod, "pod-uid") is None


@pytest.mark.parametrize(
    "change",
    [
        "node-lost",
        "shutdown",
        "not-failed",
        "runtime-ready",
        "condition-missing",
        "init-missing",
        "init-running",
    ],
)
def test_ambiguous_scheduled_pod_retains_ownership(change):
    pod = never_started_pod()
    status = pod["status"]
    if change in {"node-lost", "shutdown"}:
        status["reason"] = "NodeLost" if change == "node-lost" else "Shutdown"
    elif change == "not-failed":
        status["phase"] = "Pending"
    elif change == "runtime-ready":
        status["conditions"][0]["status"] = "True"
    elif change == "condition-missing":
        status["conditions"] = []
    elif change in {"init-missing", "init-running"}:
        pod["spec"]["initContainers"] = [{"name": "bootstrap"}]
        if change == "init-running":
            status["initContainerStatuses"] = [{"name": "bootstrap", "state": {"running": {}}}]
    assert confirmed_exit(pod, "pod-uid") is None
    controller = FakeController(pod)
    with pytest.raises(HyperlightPodCleanupPending, match="termination unconfirmed"):
        controller.recover(KEY, KIND, timeout=0.001)
    assert controller.ledger and controller.pod
    assert all(args[0] == "get" for args, _ in controller.calls)


@pytest.mark.parametrize("deleted", [False, True])
def test_kubelet_finalization_survives_later_metadata_generation_changes(deleted):
    pod = never_started_pod()
    pod["metadata"]["generation"] = 3
    pod["status"]["reason"] = "DeadlineExceeded"
    if not deleted:
        del pod["metadata"]["deletionTimestamp"]
    assert confirmed_exit(pod, "pod-uid") == 71


def test_waiting_status_with_failed_phase_is_not_kubelet_finalization():
    pod = never_started_pod()
    pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ImagePullBackOff"}}
    assert confirmed_exit(pod, "pod-uid") is None


def test_assignment_racing_deletion_requires_node_termination_proof(monkeypatch):
    controller = FakeController(never_started_pod(scheduled=False))
    del controller.pod["metadata"]["deletionTimestamp"]
    remove = controller._delete

    def raced_assignment(plural, name, uid):
        if plural == "pods":
            controller.pod["spec"]["nodeName"] = "node"
            controller.pod["metadata"]["deletionTimestamp"] = "2026-09-23T06:00:00Z"
        else:
            remove(plural, name, uid)

    monkeypatch.setattr(controller, "_delete", raced_assignment)
    with pytest.raises(HyperlightPodCleanupPending):
        controller.recover(KEY, KIND, timeout=0.001, retire=True)
    assert controller.ledger and controller.pod
    assert controller.ledger["data"]["state"] == "running"


def test_failed_initialization_requires_runtime_exit_and_stopped_pod_sandbox():
    pod = terminal_pod()
    pod["status"]["phase"] = "Failed"
    initialized = copy.deepcopy(pod["status"]["containerStatuses"][0])
    initialized["name"] = "bootstrap"
    pod["status"]["initContainerStatuses"] = [initialized]
    pod["status"]["containerStatuses"] = [
        {"name": "sandbox", "restartCount": 0, "state": {"waiting": {"reason": "PodInitializing"}}}
    ]
    assert confirmed_exit(pod, "pod-uid") is None
    pod["status"]["conditions"] = [{"type": "PodReadyToStartContainers", "status": "False"}]
    assert confirmed_exit(pod, "pod-uid") == 71
    pod["status"]["reason"] = "NodeLost"
    assert confirmed_exit(pod, "pod-uid") is None


def test_bootstrap_is_digest_pinned_and_uses_only_private_writable_storage(tmp_path, monkeypatch):
    bundle = {
        "files": {"requirements.txt": "", "probe.py": "pass"},
        "wheels": {"example-1-py3-none-any.whl": base64.b64encode(b"wheel").decode()},
    }
    archive = gzip.compress(json.dumps(bundle).encode())
    mounted = tmp_path / "bundle"
    mounted.mkdir()
    (mounted / "bundle.json.gz").write_bytes(archive)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(_pod_bootstrap, "Path", lambda value: tmp_path / value.lstrip("/"))
    monkeypatch.setattr(sys, "argv", ["bootstrap", "0" * 64])
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: commands.append(command))
    with pytest.raises(ValueError, match="digest"):
        _pod_bootstrap.main()
    assert not commands and not list(work.iterdir())
    monkeypatch.setattr(sys, "argv", ["bootstrap", hashlib.sha256(archive).hexdigest()])
    monkeypatch.setenv("TMPDIR", "unused")
    _pod_bootstrap.main()
    assert (work / "tmp").is_dir()
    assert "--require-hashes" in commands[1]
    assert "--no-deps" in commands[2]


def test_bundle_is_not_mounted_into_the_guest_host_process():
    template = replace(TEMPLATE, bundle_configmap="bundle-test", bundle_sha256="b" * 64)
    pod = json.loads(
        json.dumps(pod_manifest(KEY, KIND, template, namespace="agents", generation="gen"))
    )
    spec = pod["spec"]
    assert not any(volume["name"] == "bundle" for volume in spec["containers"][0]["volumeMounts"])
    initializer = spec["initContainers"][0]
    assert initializer["command"][-1] == "b" * 64
    assert initializer["securityContext"]["readOnlyRootFilesystem"] is True
    assert "hyperlight.dev/hypervisor" not in initializer["resources"]["limits"]


@pytest.mark.parametrize("stage", ["ledger-read", "receipt-save"])
@pytest.mark.parametrize(
    "error",
    [
        OSError("API unavailable"),
        subprocess.CalledProcessError(1, ["kubectl"], stderr="connection lost"),
        subprocess.TimeoutExpired(["kubectl"], 15),
        ValueError("invalid API response"),
        HyperlightWorkerError("Kubernetes returned a non-object response"),
    ],
)
def test_recovery_transport_failure_preserves_retryable_ownership(monkeypatch, stage, error):
    controller = FakeController()
    original_api = controller.api

    def unavailable(*arguments, body=None):
        if (stage == "ledger-read" and arguments[:2] == ("get", "configmap")) or (
            stage == "receipt-save" and arguments[0] == "replace" and "data" in (body or {})
        ):
            raise error
        return original_api(*arguments, body=body)

    monkeypatch.setattr(controller, "api", unavailable)
    with pytest.raises(HyperlightPodCleanupPending) as raised:
        controller.recover(KEY, KIND)
    assert raised.value.__cause__ is error
    assert controller.ledger["data"]["state"] == "running"
    assert controller.pod["metadata"]["finalizers"] == ["sandbox.sokol.ai/confirmed-stop"]
    assert not any(arguments[0] == "delete" for arguments, _ in controller.calls)

    monkeypatch.setattr(controller, "api", original_api)
    assert controller.recover(KEY, KIND) == 0
    assert controller.ledger == {} and controller.pod == {}


@pytest.mark.parametrize(
    "stage", ["ledger-read", "pod-read", "receipt-save", "rejected-cleanup", "stopped-cleanup"]
)
@pytest.mark.parametrize("response", ["[]", "null"])
def test_recovery_non_object_response_retains_ownership_until_retry(monkeypatch, stage, response):
    controller = FakeController()
    if stage == "rejected-cleanup":
        controller.ledger["data"].update({"state": "rejected", "pod_uid": ""})
        controller.pod = {}
    elif stage == "stopped-cleanup":
        controller.ledger["data"].update({"state": "stopped", "exit_code": "0"})
    original_api = controller.api
    ledger = copy.deepcopy(controller.ledger)
    pod = copy.deepcopy(controller.pod)

    def malformed(*arguments, body=None):
        if (
            (stage == "ledger-read" and arguments[:2] == ("get", "configmap"))
            or (stage == "receipt-save" and arguments[0] == "replace" and "data" in (body or {}))
            or (
                stage in {"pod-read", "rejected-cleanup", "stopped-cleanup"}
                and arguments[:2] == ("get", "pod")
            )
        ):
            return HyperlightPodController.api(controller, *arguments, body=body)
        return original_api(*arguments, body=body)

    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, response)
    )
    monkeypatch.setattr(controller, "api", malformed)
    with pytest.raises(HyperlightPodCleanupPending) as raised:
        controller.recover(KEY, KIND, timeout=0.001)
    assert type(raised.value.__cause__) is HyperlightWorkerError
    assert "non-object response" in str(raised.value.__cause__)
    assert controller.ledger == ledger and controller.pod == pod
    assert all(arguments[0] == "get" for arguments, _ in controller.calls)

    monkeypatch.setattr(controller, "api", original_api)
    assert controller.recover(KEY, KIND) == (71 if stage == "rejected-cleanup" else 0)
    assert controller.ledger == {} and controller.pod == {}
