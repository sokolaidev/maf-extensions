"""Configuration for the docker backend.

A plain frozen dataclass rather than a settings model, and it reads no environment: a host
already has its own configuration system, and requiring a particular one would be exactly the
coupling this package avoids.

Note what is *not* here.  The image, the work directory, the egress allowlist and the declared
outputs with their transfer caps are properties of a sandbox **kind** and travel in a
:class:`~maf_sandbox.SandboxSpec`; the backend's own transfer ceilings are named module
constants, not knobs.  The network mode is not independently configurable: ``--network none``
is what :data:`~maf_sandbox.Egress.CLOSED` means, and the one setting that changes it —
``egress_proxy_image`` — changes the declared capability with it, so the declaration and the
behaviour cannot disagree.  ``outbound_network`` is not a counter-example: it names an existing
network the proxy attaches to, and cannot put a workload anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from maf_sandbox.credentials import CredentialGateway

__all__ = ["DockerSandboxConfig"]

_DEFAULT_DOCKER_PATH = "docker"
_DEFAULT_OUTBOUND_NETWORK = "bridge"
_DEFAULT_COMMAND_TIMEOUT_S = 60.0
_DEFAULT_IMAGE_PULL_TIMEOUT_S = 600.0
_DEFAULT_PIDS_LIMIT = 512
_SUPPORTED_CAPABILITIES = frozenset({"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"})

_LINUX_CAPABILITIES = frozenset(
    "AUDIT_CONTROL AUDIT_READ AUDIT_WRITE BLOCK_SUSPEND BPF CHECKPOINT_RESTORE CHOWN "
    "DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID IPC_LOCK IPC_OWNER KILL LEASE "
    "LINUX_IMMUTABLE MAC_ADMIN MAC_OVERRIDE MKNOD NET_ADMIN NET_BIND_SERVICE NET_BROADCAST "
    "NET_RAW PERFMON SETFCAP SETGID SETPCAP SETUID SYS_ADMIN SYS_BOOT SYS_CHROOT SYS_MODULE "
    "SYS_NICE SYS_PACCT SYS_PTRACE SYS_RAWIO SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG SYSLOG "
    "WAKE_ALARM".split()
)


def capability_name(value: object) -> str:
    """Normalize one explicit Linux capability, excluding the ALL wildcard."""
    if not isinstance(value, str):
        raise ValueError("Expected a Linux capability name")
    name = value.upper().removeprefix("CAP_")
    if name not in _LINUX_CAPABILITIES:
        raise ValueError(f"Unknown Linux capability: {value!r}")
    return name


def _capability_names(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError("cap_add must be a tuple of Linux capability names")
    names = frozenset(capability_name(n) for n in cast("tuple[object, ...]", values))
    if unsupported := names - _SUPPORTED_CAPABILITIES:
        raise ValueError(f"Unsupported Docker capability combination: {sorted(unsupported)}")
    return tuple(sorted(names))


@dataclass(frozen=True)
class DockerSandboxConfig:
    """Where the client is, how long its commands may take, and how tight the box is.

    ``docker_path`` is the Docker CLI binary, not a socket or the Podman CLI. Binding requires
    Docker's context-inspection schema. The backend snapshots the client
    environment and resolves its context once, retaining its endpoint and TLS settings for
    every command. Use ``create`` to resolve immediately; the constructor binds on first use.

    ``command_timeout_seconds`` bounds the container-lifecycle commands — run, start, inspect,
    remove and the file copies.  It does **not** bound ``exec``: a workload states its own
    timeout per call, and that is the one that governs the work.

    ``image_pull_timeout_seconds`` bounds the one command that is a network transfer rather
    than a local operation.  ``docker run`` fetches an absent image implicitly, and a cold pull
    of a multi-hundred-megabyte image would blow ``command_timeout_seconds`` on a first create,
    so the create path probes with ``docker image inspect`` and — only when the image is
    genuinely absent — runs an explicit ``docker image pull`` under this timeout instead.

    ``egress_proxy_image`` opts in to :data:`~maf_sandbox.Egress.ALLOWLIST`.  It names a
    locally built image of the packaged proxy (see
    :func:`maf_sandbox_docker.proxy_build_context`); when set, a sandbox whose spec allows
    egress gets its own internal network and a dual-homed filtering proxy enforcing that
    allowlist by topology, while a spec that allows nothing still gets ``--network none``.
    Each acquisition checks a policy-contract signal from the patched proxy before serving a
    workload, so an older proxy image fails closed.
    Left ``None`` — or ``""``, which is what an unset environment variable becomes — the backend
    stays ``CLOSED`` and every container gets ``--network none``.  Both spellings of "no proxy
    configured" behave identically, deliberately: a host writing
    ``os.environ.get("MAF_EGRESS_PROXY_IMAGE", "")`` means the same thing as one writing ``None``,
    and for a while it instead got a declaration of ``CLOSED`` and a failed ``docker run`` of the
    empty string (#407).

    ``allow_private_http`` permits plaintext HTTP only to listed hosts whose selected address
    is private. It requires the proxy image and is intended for development or test workloads.

    ``credential_gateway`` enables host-issued bearer grants in the external proxy. It requires
    a rebuilt packaged proxy image and attached-authority opt-ins in the host and workload.
    Each acquisition requires a trusted call key and creates a fresh workload and gateway.
    Credentials always require upstream TLS, including when private HTTP is enabled.

    ``outbound_network`` is the network that gives the proxy its egress leg.  It exists because
    the default one is not called the same thing everywhere: ``"bridge"`` on Docker, ``"podman"``
    on Podman. A Podman socket reached through the Docker CLI is best effort and is not
    officially supported.

    ``pids_limit``, ``memory`` and ``cpus`` are hardening applied on the
    create command line, where their effect is verifiable.  ``memory`` and ``cpus`` are unset
    by default because a sensible ceiling is a property of the workload and the machine, not of
    this package. The egress proxy gets the same limits, because the guest drives its load.
    ``cap_drop_all`` must be True. ``cap_add`` accepts only subsets of CHOWN, DAC_OVERRIDE,
    SETUID, SETGID and KILL, with optional ``CAP_`` prefixes. Nonempty grants require call
    isolation and disposal, and withhold FILES_DELETE and RECLAIM. The proxy always has no
    capabilities, regardless of workload grants.
    Existing containers with a different capability policy must be disposed before reuse.
    The proxy needs about 16 PIDs; below that, allowlisted acquires fail.
    """

    docker_path: str = _DEFAULT_DOCKER_PATH
    egress_proxy_image: str | None = None
    outbound_network: str = _DEFAULT_OUTBOUND_NETWORK
    command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_S
    image_pull_timeout_seconds: float = _DEFAULT_IMAGE_PULL_TIMEOUT_S
    pids_limit: int = _DEFAULT_PIDS_LIMIT
    memory: str | None = None
    cpus: float | None = None
    cap_drop_all: bool = True
    allow_private_http: bool = False
    credential_gateway: CredentialGateway | None = None
    cap_add: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cap_drop_all is not True:
            raise ValueError("cap_drop_all must be True; request supported grants through cap_add")
        object.__setattr__(self, "cap_add", _capability_names(self.cap_add))
