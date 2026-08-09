"""Configuration for the wslc backend.

A plain frozen dataclass rather than a settings model: a host already has its own
configuration system, and requiring a particular one would be exactly the coupling this
package avoids.

Note what is *not* here.  The image, the work directory and the egress allowlist are
properties of a sandbox **kind** and travel in a :class:`~maf_sandbox.SandboxSpec`.  The
network mode is not independently configurable: ``--network none`` is what
:data:`~maf_sandbox.Egress.CLOSED` means, and the one setting that changes it —
``egress_proxy_image`` — changes the declared capability with it, so the declaration and the
behaviour cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["WslcSandboxConfig"]


@dataclass(frozen=True)
class WslcSandboxConfig:
    """Where ``wslc`` is, how long its lifecycle commands may take, and the egress mode.

    ``command_timeout_seconds`` bounds the container-lifecycle commands — run, start, list,
    remove and the file copy.  It does **not** bound ``exec``: a workload states its own
    timeout per call, and that is the one that governs the work.

    ``egress_proxy_image`` opts in to :data:`~maf_sandbox.Egress.ALLOWLIST`.  It names a
    locally built image of the packaged proxy (see
    :func:`maf_sandbox_wslc.proxy_build_context`); when set, every sandbox gets its own
    internal network and a dual-homed filtering proxy enforcing the spec's allowlist by
    topology.  Left ``None``, the backend stays ``CLOSED`` and containers get no network at
    all.
    """

    wslc_path: str = "wslc"
    command_timeout_seconds: float = 60.0
    egress_proxy_image: str | None = None
