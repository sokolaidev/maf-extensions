"""Ownership, deadline and durable cleanup refusals for the container integration."""

from __future__ import annotations

import asyncio
import base64
import copy
import gzip
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
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
)
from maf_sandbox_hyperlight._pod import FRAME_LIMIT, frame, unframe, verify_container
from maf_sandbox_hyperlight._pod_supervisor import Supervisor
from maf_sandbox_hyperlight.kubernetes import (
    HyperlightPodCleanupPending,
    HyperlightPodController,
    HyperlightPodTemplate,
    confirmed_exit,
    ownership_name,
    pod_manifest,
)

KEY = SandboxKey("tenant:user", "conversation", "agent")
KIND = "codeact"
BINDING = HyperlightPodConfig(KEY, KIND, "pod-uid", "generation", 4 * 1024**3)
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
    pod = json.loads(
        json.dumps(pod_manifest(KEY, KIND, TEMPLATE, namespace="scoped-agents", generation="gen"))
    )
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
    identity = json.loads(env["MAF_HYPERLIGHT_POD_BINDING"])
    assert [identity[name] for name in ("scope", "thread_id", "agent_id", "kind")] == [
        KEY.scope,
        KEY.thread_id,
        KEY.agent_id,
        KIND,
    ]
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


@pytest.fixture
def controls(tmp_path: Path):
    values = {
        "memory.max": str(BINDING.memory_limit_bytes),
        "memory.swap.max": "0",
        "cpu.max": "100000 100000",
        "pids.max": "100",
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


@pytest.mark.parametrize("raw", [b"{}", b"[]\n", b"{" + b"x" * FRAME_LIMIT + b"}\n", b"not-json\n"])
def test_lifecycle_frames_cannot_be_truncated_or_unbounded(raw):
    with pytest.raises((ValueError, HyperlightWorkerError)):
        unframe(raw)
    assert unframe(frame({"op": "end"})) == {"op": "end"}


@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setattr(_pod_supervisor, "_oom_kills", lambda: 0)
    subject = Supervisor(BINDING, ["application"])
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
            return copy.deepcopy(body)
        if arguments[0] == "delete":
            assert body is not None
            if "/pods/" in arguments[2]:
                assert body["preconditions"]["uid"] == "pod-uid"
                self.pod = {}
            else:
                assert self.ledger["data"]["state"] == "stopped"
                assert body["preconditions"]["uid"] == "ledger-uid"
                self.ledger = {}
            return {"status": "Success"}
        raise AssertionError(arguments)


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
