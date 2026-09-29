"""Host-side lifecycle control for a scoped application on upstream's KVM device plugin."""

from __future__ import annotations

import json
import math
import queue
import re
import secrets
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast

from maf_sandbox import SandboxKey

from ._pod import (
    FRAME_LIMIT,
    LIFECYCLE_PROTOCOL,
    PING_SECONDS,
    PLATFORM_EXIT,
    PLATFORM_REFUSAL,
    PROTOCOL_EXIT,
    PROTOCOL_REFUSAL,
    REASON_LIMIT,
    frame,
    seal,
    unframe,
)
from ._pod_config import RECOVERY_LIMIT, hello_digest
from ._pod_config import ownership_name as ownership_name
from ._wire import HyperlightWorkerError

_FINALIZER = "sandbox.sokol.ai/confirmed-stop"
_GENERATION = "sandbox.sokol.ai/generation"
_LABEL = "sandbox.sokol.ai/hyperlight-owner"
_DIAGNOSTIC_LIMIT = 64 * 1024
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


class HyperlightPodCleanupPending(HyperlightWorkerError):
    """The ownership record remains reserved because workload termination is unconfirmed."""


class HyperlightPodPlatformError(HyperlightWorkerError):
    """The node the pod landed on fails a pod-mode requirement; its cleanup is confirmed."""


class HyperlightPodProtocolMismatch(HyperlightWorkerError):
    """The pod's image speaks another lifecycle protocol than this controller; cleanup is confirmed.

    Only images that carry the check refuse; an older image fails however its release does.
    """


class HyperlightPodReserved(HyperlightWorkerError):
    """Another owner, running or awaiting cleanup, holds the scope; nothing was created."""


@dataclass(frozen=True)
class HyperlightPodTemplate:
    """An immutable image and application command with aggregate container resource budgets.

    A positive ``recovery_seconds`` keeps the session when the controller's attach breaks and
    returns within that many seconds; zero retires the pod on the first break.
    """

    image: str
    command: tuple[str, ...]
    memory_limit_bytes: int = 4 * 1024**3
    cpu_millis: int = 1000
    cpu_request_millis: int = 500
    storage_limit_bytes: int = 2 * 1024**3
    session_timeout: int = 1800
    bundle_configmap: str | None = None
    bundle_sha256: str | None = None
    recovery_seconds: int = 0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.image):
            raise ValueError("pod image must be pinned by sha256 digest")
        if not self.command or any(not item or "\x00" in item for item in self.command):
            raise ValueError("an application argument vector is required")
        for value in (
            self.memory_limit_bytes,
            self.cpu_millis,
            self.cpu_request_millis,
            self.storage_limit_bytes,
            self.session_timeout,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("pod budgets must be positive integers")
        if self.cpu_request_millis > self.cpu_millis:
            raise ValueError("CPU request must not exceed the limit")
        if (
            type(self.recovery_seconds) is not int
            or not 0 <= self.recovery_seconds <= RECOVERY_LIMIT
            or self.recovery_seconds >= self.session_timeout
        ):
            raise ValueError(
                f"recovery_seconds must be 0 to {RECOVERY_LIMIT} and shorter than the session"
            )
        if self.bundle_configmap is not None or self.bundle_sha256 is not None:
            if not re.fullmatch(_DNS_LABEL, self.bundle_configmap or ""):
                raise ValueError("bundle_configmap must name a namespaced ConfigMap")
            if not re.fullmatch(r"[a-f0-9]{64}", self.bundle_sha256 or ""):
                raise ValueError("bundle_sha256 must pin the application bundle")


@dataclass(frozen=True)
class HyperlightPodResult:
    """A confirmed pod outcome with bounded diagnostics and the controls PID 1 observed."""

    pod_uid: str
    exit_code: int
    elapsed: float
    diagnostics: str
    platform: dict[str, str] = field(default_factory=dict[str, str])
    reason: str = ""
    # Why each controller attach the session survived ended, in order.
    interruptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class HyperlightPodExit:
    """A confirmed exit code and the reason the pod supervisor recorded, empty if none."""

    exit_code: int
    reason: str


@dataclass
class _Session:
    """What the controller keeps for one pod across its attaches."""

    generation: str
    identity: dict[str, str]
    session_deadline: float
    recovery: int
    uid: str = ""
    diagnostics: bytearray = field(default_factory=bytearray)
    platform: dict[str, str] = field(default_factory=dict[str, str])
    interruptions: list[str] = field(default_factory=list[str])
    interrupted: str = ""
    ended_at: float = 0.0
    counter: int = 0
    sequence: int = 0
    deadline: float | None = None
    ready: bool = False
    resumed: bool = False

    def expired(self) -> bool:
        return time.monotonic() >= self.session_deadline or (
            self.deadline is not None and time.time() >= self.deadline
        )

    def observe(self, platform: object) -> None:
        """Keep the bounded controls PID 1 reported, from `ready` or from a resume."""
        if isinstance(platform, dict):
            self.platform.update(
                {
                    str(item): str(value)[:256]
                    for item, value in cast("dict[object, object]", platform).items()
                }
            )

    def reconcile(self, snapshot: dict[str, object], send: Callable[..., None]) -> None:
        """Adopt PID 1's call state after a reattach; events sent while detached were lost."""
        sequence, expires = snapshot.get("sequence"), snapshot.get("expires_at")
        acknowledged = snapshot.get("acknowledged")
        if (
            type(sequence) is not int
            or type(acknowledged) is not bool
            or not (expires is None or _finite(expires))
        ):
            raise HyperlightWorkerError("invalid pod state snapshot")
        active = None if expires is None else float(cast("float", expires))
        if sequence == self.sequence + 1 and not acknowledged:
            if active is not None and active <= time.time():
                raise HyperlightWorkerError("invalid or overlapping controller deadline")
            self.sequence = sequence
        elif sequence != self.sequence or (active is not None and active != self.deadline):
            raise HyperlightWorkerError("pod lifecycle state diverged during reconnection")
        self.deadline = active
        if active is not None and not acknowledged:
            send("ack", sequence=sequence)
        self.observe(snapshot.get("platform"))
        self.ready = self.resumed = True
        self.interruptions.append(self.interrupted)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _running_container(pod: dict[str, object]) -> str | None:
    """The sandbox container's runtime ID while it runs its first and only start."""
    status = cast("dict[str, object]", pod.get("status", {}))
    for item in cast("list[dict[str, object]]", status.get("containerStatuses", [])):
        state = cast("dict[str, object]", item.get("state", {}))
        container = item.get("containerID")
        if (
            item.get("name") == "sandbox"
            and "running" in state
            and item.get("restartCount") == 0
            and isinstance(container, str)
            and container
        ):
            return container
    return None


def pod_manifest(
    key: SandboxKey,
    kind: str,
    template: HyperlightPodTemplate,
    *,
    namespace: str,
    generation: str,
    secret_digest: str,
) -> dict[str, object]:
    """Add a private supervised application to upstream's extended-resource deployment model."""
    if not re.fullmatch(_DNS_LABEL, namespace):
        raise ValueError("invalid application namespace")
    name = ownership_name(key, kind)
    # Pod readers see only the digest; the identity itself arrives over the attach stream.
    binding = {
        "protocol": LIFECYCLE_PROTOCOL,
        "owner": name,
        "generation": generation,
        "memory_limit_bytes": template.memory_limit_bytes,
        "hello_digest": secret_digest,
        "recovery_seconds": template.recovery_seconds,
    }
    security = {
        "runAsNonRoot": True,
        "runAsUser": 65534,
        "runAsGroup": 65534,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    pod: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {_LABEL: name},
            "annotations": {_GENERATION: generation},
            "finalizers": [_FINALIZER],
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "hostPID": False,
            "hostNetwork": False,
            "shareProcessNamespace": False,
            "activeDeadlineSeconds": template.session_timeout,
            "terminationGracePeriodSeconds": 10,
            "nodeSelector": {
                "kubernetes.io/arch": "amd64",
                "hyperlight.dev/hypervisor": "kvm",
            },
            "securityContext": {**security, "fsGroup": 65534},
            "containers": [
                {
                    "name": "sandbox",
                    "image": template.image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "python",
                        "-I",
                        "-u",
                        "-m",
                        "maf_sandbox_hyperlight._pod_supervisor",
                        *template.command,
                    ],
                    "stdin": True,
                    # A reattach can reach PID 1's stdin only when it outlives the first.
                    "stdinOnce": template.recovery_seconds == 0,
                    "tty": False,
                    "terminationMessagePath": "/dev/termination-log",
                    "terminationMessagePolicy": "File",
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "env": [
                        {"name": "MAF_HYPERLIGHT_POD_BINDING", "value": json.dumps(binding)},
                        {
                            "name": "MAF_HYPERLIGHT_POD_UID",
                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                        },
                        {"name": "HOME", "value": "/work/home"},
                        {"name": "XDG_CACHE_HOME", "value": "/work/cache"},
                        {"name": "TMPDIR", "value": "/work/tmp"},
                    ],
                    "resources": {
                        "requests": {
                            "hyperlight.dev/hypervisor": "1",
                            "cpu": f"{template.cpu_request_millis}m",
                            "memory": str(template.memory_limit_bytes),
                            "ephemeral-storage": str(template.storage_limit_bytes),
                        },
                        "limits": {
                            "hyperlight.dev/hypervisor": "1",
                            "cpu": f"{template.cpu_millis}m",
                            "memory": str(template.memory_limit_bytes),
                            "ephemeral-storage": str(template.storage_limit_bytes),
                        },
                    },
                    "volumeMounts": [
                        {"name": "work", "mountPath": "/work"},
                        {"name": "control", "mountPath": "/run/maf-hyperlight"},
                        {"name": "locks", "mountPath": "/run/lock"},
                    ],
                }
            ],
            "volumes": [
                {"name": "work", "emptyDir": {"sizeLimit": str(template.storage_limit_bytes)}},
                {"name": "control", "emptyDir": {"sizeLimit": "1Mi"}},
                {"name": "locks", "emptyDir": {"sizeLimit": "1Mi"}},
            ],
        },
    }
    if template.bundle_configmap is not None:
        spec = cast("dict[str, object]", pod["spec"])
        containers = cast("list[dict[str, object]]", spec["containers"])
        container = containers[0]
        command = cast("list[str]", container["command"])
        command[0] = "/work/runtime/bin/python"
        environment = cast("list[dict[str, object]]", container["env"])
        environment.append(
            {"name": "PATH", "value": "/work/runtime/bin:/usr/local/bin:/usr/bin:/bin"}
        )
        volumes = cast("list[dict[str, object]]", spec["volumes"])
        volumes.append({"name": "bundle", "configMap": {"name": template.bundle_configmap}})
        resources = cast("dict[str, dict[str, str]]", container["resources"])
        spec["initContainers"] = [
            {
                "name": "bootstrap",
                "image": template.image,
                "command": [
                    "python",
                    "-I",
                    "-c",
                    Path(__file__).with_name("_pod_bootstrap.py").read_text(encoding="utf-8"),
                    template.bundle_sha256,
                ],
                "securityContext": container["securityContext"],
                "resources": {
                    kind: {
                        key: value
                        for key, value in values.items()
                        if key != "hyperlight.dev/hypervisor"
                    }
                    for kind, values in resources.items()
                },
                "volumeMounts": [
                    {"name": "work", "mountPath": "/work"},
                    {"name": "bundle", "mountPath": "/bundle", "readOnly": True},
                ],
            }
        ]
    return pod


def confirmed_exit(pod: dict[str, object], uid: str) -> int | None:
    """Accept UID-bound runtime exit or authoritative never-started cleanup proof."""
    metadata = cast("dict[str, object]", pod.get("metadata", {}))
    status = cast("dict[str, object]", pod.get("status", {}))
    if metadata.get("uid") != uid or status.get("reason") in {"NodeLost", "Shutdown"}:
        return None
    if _never_started(pod):
        return 71
    containers = cast("list[dict[str, object]]", status.get("containerStatuses", []))
    if len(containers) != 1 or containers[0].get("name") != "sandbox":
        return None
    state = cast("dict[str, object]", containers[0].get("state", {}))
    waiting = cast("dict[str, object]", state.get("waiting", {}))
    if (
        status.get("phase") == "Failed"
        and waiting.get("reason") == "PodInitializing"
        and containers[0].get("restartCount") == 0
        and not containers[0].get("containerID")
    ):
        initializers = cast("list[dict[str, object]]", status.get("initContainerStatuses", []))
        if len(initializers) == 1 and initializers[0].get("name") == "bootstrap":
            init_state = cast("dict[str, object]", initializers[0].get("state", {}))
            code = _terminated_exit(init_state)
            conditions = cast("list[dict[str, object]]", status.get("conditions", []))
            stopped = any(
                item.get("type") == "PodReadyToStartContainers" and item.get("status") == "False"
                for item in conditions
            )
            if code is not None and stopped:
                return code or 71
    return _terminated_exit(state)


def _never_started(pod: dict[str, object]) -> bool:
    """Require a binding fence or kubelet finalization with no container execution history."""
    metadata = cast("dict[str, object]", pod.get("metadata", {}))
    spec = cast("dict[str, object]", pod.get("spec", {}))
    status = cast("dict[str, object]", pod.get("status", {}))
    containers = cast("list[dict[str, object]]", status.get("containerStatuses", []))
    initializers = cast("list[dict[str, object]]", status.get("initContainerStatuses", []))
    declared = cast("list[dict[str, object]]", spec.get("containers", []))
    if (
        [item.get("name") for item in declared] != ["sandbox"]
        or spec.get("ephemeralContainers")
        or status.get("ephemeralContainerStatuses")
    ):
        return False
    if not spec.get("nodeName"):
        # The API server rejects binding a pod once deletion is recorded.
        return bool(metadata.get("deletionTimestamp")) and (
            not containers and not initializers and status.get("phase") in {"Pending", "Failed"}
        )
    conditions = cast("list[dict[str, object]]", status.get("conditions", []))
    if (
        status.get("phase") != "Failed"
        or not any(
            item.get("type") == "PodReadyToStartContainers" and item.get("status") == "False"
            for item in conditions
        )
        or len(containers) != 1
        or containers[0].get("name") != "sandbox"
        or not _no_container_history(containers[0])
    ):
        return False
    declared_init = cast("list[dict[str, object]]", spec.get("initContainers", []))
    if [item.get("name") for item in declared_init] != [item.get("name") for item in initializers]:
        return False
    if any(
        _terminated_exit(cast("dict[str, object]", item.get("state", {}))) is None
        and not _never_started_terminal(item)
        for item in initializers
    ):
        return False
    state = cast("dict[str, object]", containers[0].get("state", {}))
    return _never_started_terminal(containers[0]) or (
        bool(initializers) and state == {"waiting": {"reason": "PodInitializing"}}
    )


def _no_container_history(container: dict[str, object]) -> bool:
    return (
        type(container.get("restartCount")) is int
        and container.get("restartCount") == 0
        and container.get("started") is False
        and container.get("ready") is False
        and not any(container.get(field) for field in ("containerID", "imageID", "lastState"))
    )


def _never_started_terminal(container: dict[str, object]) -> bool:
    """Only kubelet's finalization default qualifies; other unknown states retain ownership."""
    state = cast("dict[str, object]", container.get("state", {}))
    ended = cast("dict[str, object]", state.get("terminated", {}))
    return (
        _no_container_history(container)
        and set(state) == {"terminated"}
        and ended.get("reason") == "ContainerStatusUnknown"
        and ended.get("message") == "The container could not be located when the pod was terminated"
        and ended.get("exitCode") == 137
        and not any(ended.get(field) for field in ("containerID", "startedAt", "finishedAt"))
    )


def _termination_reason(pod: dict[str, object]) -> str:
    """Only a runtime exit carries supervisor text; kubelet's synthetic status does not."""
    status = cast("dict[str, object]", pod.get("status", {}))
    containers = cast("list[dict[str, object]]", status.get("containerStatuses", []))
    if len(containers) != 1 or containers[0].get("name") != "sandbox":
        return ""
    state = cast("dict[str, object]", containers[0].get("state", {}))
    if _terminated_exit(state) is None:
        return ""
    message = cast("dict[str, object]", state.get("terminated", {})).get("message")
    return message[:REASON_LIMIT] if isinstance(message, str) else ""


def _terminated_exit(state: dict[str, object]) -> int | None:
    """Synthetic kubelet statuses cannot certify that a container exited."""
    ended = cast("dict[str, object]", state.get("terminated", {}))
    if (
        ended.get("reason") not in {"Completed", "Error", "OOMKilled"}
        or not ended.get("containerID")
        or not ended.get("finishedAt")
        or str(ended["finishedAt"]).startswith("0001-")
        or type(ended.get("exitCode")) is not int
    ):
        return None
    return cast("int", ended["exitCode"])


class HyperlightPodController:
    """Own scoped pods through authenticated kubectl; an existing allocation always refuses reuse.

    Run the agent application inside the pod. The attach stream carries lifecycle messages only.
    The host supplies authorized keys; this class is not a public authentication endpoint.
    """

    def __init__(self, *, kubeconfig: str, context: str, namespace: str) -> None:
        if not kubeconfig or not context or not re.fullmatch(_DNS_LABEL, namespace):
            raise ValueError("explicit kubeconfig, context and namespace are required")
        self.namespace = namespace
        self.command = [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "--context",
            context,
            "-n",
            namespace,
        ]

    def api(self, *arguments: str, body: dict[str, object] | None = None) -> dict[str, object]:
        """Bound API requests; credentials remain in the kubeconfig authentication flow."""
        result = subprocess.run(
            [*self.command, "--request-timeout=10s", *arguments],
            input=None if body is None else json.dumps(body),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        if not result.stdout.strip() and "--ignore-not-found=true" in arguments:
            return {}
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise HyperlightWorkerError("Kubernetes returned a non-object response")
        return cast("dict[str, object]", value)

    def _replace(self, resource: dict[str, object]) -> dict[str, object]:
        return self.api("replace", "-f", "-", "-o", "json", body=resource)

    def _delete(self, plural: str, name: str, uid: str) -> None:
        self.api(
            "delete",
            "--raw",
            f"/api/v1/namespaces/{self.namespace}/{plural}/{name}",
            "-f",
            "-",
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "gracePeriodSeconds": 10,
            },
        )

    def supervise(
        self,
        key: SandboxKey,
        kind: str,
        template: HyperlightPodTemplate,
        *,
        cleanup_timeout: float = 45,
    ) -> HyperlightPodResult:
        """Supervise a scoped application through confirmed cleanup."""
        if not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be positive and finite")
        name = ownership_name(key, kind)
        generation = uuid.uuid4().hex
        # Only this process holds the secret; the pod spec carries its digest.
        secret = secrets.token_hex(32)
        manifest = pod_manifest(
            key,
            kind,
            template,
            namespace=self.namespace,
            generation=generation,
            secret_digest=hello_digest(secret),
        )
        identity = {
            "scope": key.scope,
            "thread_id": key.thread_id,
            "agent_id": key.agent_id,
            "kind": kind,
            "secret": secret,
        }
        try:
            # A Kubernetes UID is a UUID, so this is the size of the hello the pod reads.
            frame(
                {
                    "op": "hello",
                    "pod_uid": str(uuid.UUID(int=0)),
                    "generation": generation,
                    **identity,
                }
            )
        except HyperlightWorkerError as error:
            raise ValueError("the ownership identity exceeds a lifecycle frame") from error
        try:
            ledger = self.api(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": name,
                        "namespace": self.namespace,
                        "labels": {_LABEL: name},
                    },
                    "data": {"generation": generation, "state": "allocating", "pod_uid": ""},
                },
            )
        except subprocess.CalledProcessError as error:
            if _ledger_exists(error, name):
                raise HyperlightPodReserved(
                    "the ownership scope is already reserved; once its owner has stopped, "
                    "recover it with the same identity"
                ) from error
            raise
        started = time.monotonic()
        try:
            pod = self.api("create", "-f", "-", "-o", "json", body=manifest)
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError) as error:
            if not _create_rejected(error, name):
                raise HyperlightPodCleanupPending(
                    "pod creation is unconfirmed; allocation retained"
                ) from error
            self._release_rejected(name, ledger, record=True)
            raise
        session = _Session(
            generation, identity, started + template.session_timeout, template.recovery_seconds
        )
        uid = ""
        try:
            uid = str(cast("dict[str, object]", pod["metadata"])["uid"])
            session.uid = uid
            cast("dict[str, str]", ledger["data"]).update({"pod_uid": uid, "state": "running"})
            self._replace(ledger)
            container = self._await_running(
                name, uid, min(started + template.session_timeout, started + 180)
            )
            if container is not None:
                self._hold(name, container, session)
        finally:
            outcome = self.recover_exit(key, kind, timeout=cleanup_timeout, retire=True)
        if outcome.exit_code == PLATFORM_EXIT and outcome.reason.startswith(PLATFORM_REFUSAL):
            raise HyperlightPodPlatformError(outcome.reason)
        if outcome.exit_code == PROTOCOL_EXIT and outcome.reason.startswith(PROTOCOL_REFUSAL):
            raise HyperlightPodProtocolMismatch(outcome.reason)
        return HyperlightPodResult(
            uid,
            outcome.exit_code,
            time.monotonic() - started,
            session.diagnostics.decode(errors="replace"),
            session.platform,
            outcome.reason,
            tuple(session.interruptions),
        )

    def _await_running(self, name: str, uid: str, deadline: float) -> str | None:
        """Return the running container to attach to, or None once the pod has exited."""
        pod: dict[str, object] = {}
        while time.monotonic() < deadline:
            pod = self.api("get", "pod", name, "-o", "json")
            metadata = cast("dict[str, object]", pod["metadata"])
            if metadata.get("uid") != uid:
                raise HyperlightPodCleanupPending("pod UID changed during startup")
            if confirmed_exit(pod, uid) is not None:
                return None
            container = _running_container(pod)
            if container is not None:
                return container
            time.sleep(0.5)
        raise TimeoutError(
            "application pod did not start within its startup budget" + _startup_blocker(pod)
        )

    def _attach(self, name: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [*self.command, "attach", "-i", name, "-c", "sandbox", "--pod-running-timeout=10s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _hold(self, name: str, container: str, session: _Session) -> None:
        """Supervise over attach; in continuity mode, reattach while the recovery window lasts."""
        ended = self._attach_once(name, session, resume_by=None)
        while ended and session.recovery:
            session.interrupted, session.resumed = ended, False
            recover_by = session.ended_at + session.recovery
            while not session.resumed:
                if time.monotonic() >= recover_by or session.expired():
                    return
                same = self._same_container(name, container, session)
                # The API call can outlast either bound; a resume sent after one would renew PID 1.
                if same is False or time.monotonic() >= recover_by or session.expired():
                    return
                if same:
                    ended = self._attach_once(name, session, resume_by=recover_by)
                if not session.resumed:
                    time.sleep(0.5)

    def _same_container(self, name: str, container: str, session: _Session) -> bool | None:
        """None when the API cannot answer; False once the pod or its container differ."""
        try:
            pod = self.api("get", "pod", name, "-o", "json", "--ignore-not-found=true")
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError):
            return None
        if not pod:
            return False
        metadata = cast("dict[str, object]", pod["metadata"])
        annotations = cast("dict[str, str]", metadata.get("annotations", {}))
        return (
            metadata.get("uid") == session.uid
            and annotations.get(_GENERATION) == session.generation
            and not metadata.get("deletionTimestamp")
            and _running_container(pod) == container
        )

    def _attach_once(self, name: str, session: _Session, *, resume_by: float | None) -> str:
        stream = self._attach(name)
        readers: list[threading.Thread] = []
        try:
            return self._supervise(stream, session, readers, resume_by=resume_by)
        finally:
            # The recovery window runs from here, not from after the cleanup below.
            session.ended_at = time.monotonic()
            if stream.poll() is None:
                stream.terminate()
            try:
                stream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stream.kill()
                stream.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=1)
            for pipe in (stream.stdin, stream.stdout, stream.stderr):
                if pipe is not None:
                    with suppress(OSError):
                        pipe.close()

    def _supervise(
        self,
        stream: subprocess.Popen[bytes],
        session: _Session,
        readers: list[threading.Thread],
        *,
        resume_by: float | None = None,
    ) -> str:
        """Return why the attach ended while the pod may still run, or "" once stop is sent.

        With ``resume_by`` this attach resumes the session and must confirm it by then.
        """
        messages: queue.Queue[dict[str, object]] = queue.Queue(maxsize=64)
        outgoing: queue.Queue[bytes] = queue.Queue(maxsize=8)
        transport_closed = threading.Event()
        diagnostics = session.diagnostics
        assert stream.stdout is not None and stream.stderr is not None and stream.stdin is not None

        def read(source: BinaryIO, *, control: bool) -> None:
            try:
                if control:
                    while raw := source.readline(FRAME_LIMIT + 1):
                        messages.put_nowait(unframe(raw))
                else:
                    while chunk := source.read(4096):
                        diagnostics.extend(chunk[: max(0, _DIAGNOSTIC_LIMIT - len(diagnostics))])
            except (OSError, ValueError, queue.Full, HyperlightWorkerError):
                return
            finally:
                if control:
                    # Retirement must survive a saturated lifecycle queue.
                    transport_closed.set()

        def write() -> None:
            assert stream.stdin is not None
            try:
                while stream.poll() is None:
                    try:
                        payload = outgoing.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    stream.stdin.write(payload)
                    stream.stdin.flush()
            except (OSError, ValueError):
                transport_closed.set()

        readers.extend(
            [
                threading.Thread(
                    target=read, args=(stream.stdout,), kwargs={"control": True}, daemon=True
                ),
                threading.Thread(
                    target=read, args=(stream.stderr,), kwargs={"control": False}, daemon=True
                ),
                threading.Thread(target=write, daemon=True),
            ]
        )
        for thread in readers:
            thread.start()

        def send(operation: str, **fields: object) -> None:
            message = {
                "op": operation,
                "pod_uid": session.uid,
                "generation": session.generation,
                **fields,
            }
            if session.recovery and operation != "hello":
                session.counter += 1
                message = seal({**message, "counter": session.counter}, session.identity["secret"])
            try:
                outgoing.put_nowait(frame(message))
            except queue.Full as error:
                raise HyperlightWorkerError(
                    "controller transport stopped consuming messages"
                ) from error

        if resume_by is None:
            send("hello", **session.identity)
        else:
            # Terminates any frame the dropped attach left half-written in the pod's stdin.
            outgoing.put_nowait(b"\n")
            if not session.ready:
                # The first hello may never have reached PID 1; a bound PID 1 ignores a repeat.
                send("hello", **session.identity)
            send("resume")
        next_ping = 0.0
        withdrawn_sequence: int | None = None
        while stream.poll() is None:
            if transport_closed.is_set():
                return "attach stream closed"
            now = time.monotonic()
            if session.expired():
                send("stop")
                return ""
            if resume_by is not None and not session.resumed and now >= resume_by:
                return "reconnection was not confirmed"
            if now >= next_ping:
                send("ping")
                next_ping = now + PING_SECONDS
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if message.get("pod_uid") != session.uid or message.get("generation") != (
                session.generation
            ):
                raise HyperlightWorkerError("attach stream belongs to another pod generation")
            event = message.get("event")
            if withdrawn_sequence is not None and (
                event != "resumed"
                or message.get("sequence") != withdrawn_sequence
                or message.get("expires_at") is not None
                or message.get("acknowledged") is not False
            ):
                raise HyperlightWorkerError("resume snapshot does not confirm the withdrawal")
            if event == "begin":
                expires = message.get("expires_at")
                if (
                    not session.ready
                    or session.deadline is not None
                    or message.get("sequence") != session.sequence + 1
                    or not _finite(expires)
                    or cast("float", expires) <= time.time()
                ):
                    raise HyperlightWorkerError("invalid or overlapping controller deadline")
                session.sequence += 1
                session.deadline = float(cast("float", expires))
                send("ack", sequence=session.sequence)
            elif (
                event == "end"
                and message.get("sequence") == session.sequence
                and session.deadline is not None
            ):
                session.deadline = None
            elif (
                event == "end"
                and resume_by is not None
                and not session.resumed
                and type(sequence := message.get("sequence")) is int
                and sequence == session.sequence + 1
            ):
                # Keep the last known deadline until the snapshot confirms the withdrawn call.
                withdrawn_sequence = sequence
            elif event == "ready" and not session.ready:
                session.ready = True
                session.observe(message.get("platform"))
            elif event == "resumed" and resume_by is not None and not session.resumed:
                session.reconcile(message, send)
                withdrawn_sequence = None
            else:
                raise HyperlightWorkerError("invalid pod lifecycle event")
        return f"attach exited with {stream.returncode}"

    def recover(
        self, key: SandboxKey, kind: str, *, timeout: float = 45, retire: bool = False
    ) -> int:
        """Release a stopped or rejected allocation; confirmed rejection returns code 71."""
        return self.recover_exit(key, kind, timeout=timeout, retire=retire).exit_code

    def recover_exit(
        self, key: SandboxKey, kind: str, *, timeout: float = 45, retire: bool = False
    ) -> HyperlightPodExit:
        """Like `recover`, and also return the reason saved with the termination receipt."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        name = ownership_name(key, kind)
        try:
            ledger = self.api("get", "configmap", name, "-o", "json")
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError) as error:
            raise HyperlightPodCleanupPending(
                "ownership lookup failed; cleanup is unconfirmed"
            ) from error
        data = cast("dict[str, str]", ledger["data"])
        if data.get("state") == "rejected" and not data.get("pod_uid"):
            self._release_rejected(name, ledger)
            return HyperlightPodExit(71, "")
        if data.get("state") == "stopped" and data.get("pod_uid"):
            self._finish_cleanup(name, ledger)
            return HyperlightPodExit(int(data["exit_code"]), data.get("reason", ""))
        until = time.monotonic() + timeout
        deleted = False
        while True:
            try:
                pod = self.api("get", "pod", name, "-o", "json", "--ignore-not-found=true")
                if not pod:
                    raise HyperlightPodCleanupPending("pod disappeared without termination proof")
                metadata = cast("dict[str, object]", pod["metadata"])
                annotations = cast("dict[str, str]", metadata.get("annotations", {}))
                if annotations.get(_GENERATION) != data["generation"]:
                    raise HyperlightPodCleanupPending("pod generation differs; allocation retained")
                uid = str(metadata["uid"])
                if data["pod_uid"] and data["pod_uid"] != uid:
                    raise HyperlightPodCleanupPending("pod UID differs; allocation retained")
                result = confirmed_exit(pod, uid)
                if result is not None:
                    break
                if retire and not deleted:
                    self._delete("pods", name, uid)
                    deleted = True
            except HyperlightPodCleanupPending:
                raise
            except (
                subprocess.SubprocessError,
                OSError,
                ValueError,
                HyperlightWorkerError,
            ) as error:
                if time.monotonic() >= until:
                    raise HyperlightPodCleanupPending(
                        "termination unconfirmed; allocation retained"
                    ) from error
            if time.monotonic() >= until:
                raise HyperlightPodCleanupPending("termination unconfirmed; allocation retained")
            time.sleep(0.2)
        reason = _termination_reason(pod)
        data.update(
            {"state": "stopped", "pod_uid": uid, "exit_code": str(result), "reason": reason}
        )
        try:
            ledger = self._replace(ledger)
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError) as error:
            raise HyperlightPodCleanupPending(
                "termination receipt is unconfirmed; allocation retained"
            ) from error
        self._finish_cleanup(name, ledger)
        return HyperlightPodExit(result, reason)

    def _release_rejected(
        self, name: str, ledger: dict[str, object], *, record: bool = False
    ) -> None:
        """A server rejection plus absence excludes a pod retained by an earlier create attempt."""
        try:
            if self.api("get", "pod", name, "-o", "json", "--ignore-not-found=true"):
                raise HyperlightPodCleanupPending("pod exists after rejection; allocation retained")
            if record:
                cast("dict[str, str]", ledger["data"])["state"] = "rejected"
                ledger = self._replace(ledger)
            ledger_uid = str(cast("dict[str, object]", ledger["metadata"])["uid"])
            self._delete("configmaps", name, ledger_uid)
        except HyperlightPodCleanupPending:
            raise
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError) as error:
            raise HyperlightPodCleanupPending(
                "rejected allocation cleanup is incomplete; allocation retained"
            ) from error

    def _finish_cleanup(self, name: str, ledger: dict[str, object]) -> None:
        """A durable termination receipt permits retry after API deletion or controller death."""
        data = cast("dict[str, str]", ledger["data"])
        try:
            pod = self.api("get", "pod", name, "-o", "json", "--ignore-not-found=true")
            if pod:
                metadata = cast("dict[str, object]", pod["metadata"])
                if metadata.get("uid") != data["pod_uid"]:
                    raise HyperlightPodCleanupPending(
                        "replacement UID differs; allocation retained"
                    )
                metadata["finalizers"] = [
                    item
                    for item in cast("list[str]", metadata.get("finalizers", []))
                    if item != _FINALIZER
                ]
                self._replace(pod)
                if not metadata.get("deletionTimestamp"):
                    self._delete("pods", name, data["pod_uid"])
                if self.api("get", "pod", name, "-o", "json", "--ignore-not-found=true"):
                    raise HyperlightPodCleanupPending("pod deletion is pending; receipt retained")
            ledger_uid = str(cast("dict[str, object]", ledger["metadata"])["uid"])
            self._delete("configmaps", name, ledger_uid)
        except HyperlightPodCleanupPending:
            raise
        except (OSError, ValueError, subprocess.SubprocessError, HyperlightWorkerError) as error:
            raise HyperlightPodCleanupPending(
                "cleanup is incomplete; termination receipt retained"
            ) from error


def _startup_blocker(pod: dict[str, object]) -> str:
    """Name the scheduler's or kubelet's reason a pod is still waiting."""
    status = cast("dict[str, object]", pod.get("status", {}))
    for condition in cast("list[dict[str, object]]", status.get("conditions", [])):
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return f": {condition.get('reason')}: {str(condition.get('message'))[:1024]}"
    # Init containers run first; while one is blocked, the application only reports
    # PodInitializing, so that is the reason of last resort.
    blocked = [
        (str(container.get("name")), waiting)
        for key in ("initContainerStatuses", "containerStatuses")
        for container in cast("list[dict[str, object]]", status.get(key, []))
        if (
            waiting := cast(
                "dict[str, object]",
                cast("dict[str, object]", container.get("state", {})).get("waiting", {}),
            )
        ).get("reason")
    ]
    if not blocked:
        return ""
    name, waiting = min(blocked, key=lambda item: item[1]["reason"] == "PodInitializing")
    return f": {name}: {waiting['reason']}: {str(waiting.get('message', ''))[:1024]}"


def _server_refusal(error: subprocess.CalledProcessError) -> str:
    """kubectl's refusal text without warnings, or empty when the failure is not one."""
    if error.returncode != 1 or error.stdout or not isinstance(error.stderr, str):
        return ""
    lines = error.stderr.splitlines()
    while lines and lines[0].startswith("Warning: "):
        lines.pop(0)
    return "\n".join(lines)


def _ledger_exists(error: subprocess.CalledProcessError, name: str) -> bool:
    return bool(
        re.match(
            r'\AError from server \(AlreadyExists\): error when creating "STDIN": '
            rf'configmaps "{re.escape(name)}" already exists\s*\Z',
            _server_refusal(error),
        )
    )


def _create_rejected(error: BaseException, name: str) -> bool:
    """Recognize kubectl server refusals; unknown output and transport errors retain ownership."""
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    message = _server_refusal(error)
    return bool(
        re.match(
            r'\AError from server \((?:Forbidden|BadRequest)\): error when creating "STDIN": ',
            message,
        )
        or re.match(rf'\AThe Pod "{re.escape(name)}" is invalid(?::[ \n]|$)', message)
    )
