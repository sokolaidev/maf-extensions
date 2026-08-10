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

__all__ = ["DockerSandboxConfig"]

_DEFAULT_DOCKER_PATH = "docker"
_DEFAULT_OUTBOUND_NETWORK = "bridge"
_DEFAULT_COMMAND_TIMEOUT_S = 60.0
_DEFAULT_IMAGE_PULL_TIMEOUT_S = 600.0
_DEFAULT_PIDS_LIMIT = 512


@dataclass(frozen=True)
class DockerSandboxConfig:
    """Where the client is, how long its commands may take, and how tight the box is.

    ``docker_path`` is the client binary, not a socket: the subprocess inherits this process's
    environment, so ``DOCKER_HOST``, the active Docker context and every other lookup the real
    client implements work without this package knowing they exist.

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
    Left ``None``, the backend stays ``CLOSED`` and every container gets ``--network none``.

    ``outbound_network`` is the network that gives the proxy its egress leg.  It exists because
    the default one is not called the same thing everywhere: ``"bridge"`` on Docker, ``"podman"``
    on Podman — an engine this package does not officially support, but deliberately does not
    lock out either.

    ``pids_limit``, ``memory``, ``cpus`` and ``cap_drop_all`` are hardening applied on the
    create command line, where their effect is verifiable.  ``memory`` and ``cpus`` are unset
    by default because a sensible ceiling is a property of the workload and the machine, not of
    this package; ``cap_drop_all`` is off by default until real workloads have been measured
    under it (maintainer ruling — see the design document).
    """

    docker_path: str = _DEFAULT_DOCKER_PATH
    egress_proxy_image: str | None = None
    outbound_network: str = _DEFAULT_OUTBOUND_NETWORK
    command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_S
    image_pull_timeout_seconds: float = _DEFAULT_IMAGE_PULL_TIMEOUT_S
    pids_limit: int = _DEFAULT_PIDS_LIMIT
    memory: str | None = None
    cpus: float | None = None
    cap_drop_all: bool = False
