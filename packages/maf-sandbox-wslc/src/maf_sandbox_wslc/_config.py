"""Configuration for the wslc backend.

A plain frozen dataclass rather than a settings model: a host already has its own
configuration system, and requiring a particular one would be exactly the coupling this
package avoids.

Note what is *not* here.  The image, the work directory and the egress allowlist are
properties of a sandbox **kind** and travel in a :class:`~maf_sandbox.SandboxSpec`; the
network mode is not configurable at all, because ``--network none`` is what
:data:`~maf_sandbox.Egress.CLOSED` means and a setting that could widen it would make the
declaration a lie.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["WslcSandboxConfig"]


@dataclass(frozen=True)
class WslcSandboxConfig:
    """Where ``wslc`` is, and how long its lifecycle commands may take.

    ``command_timeout_seconds`` bounds the container-lifecycle commands — run, start, list,
    remove and the file copy.  It does **not** bound ``exec``: a workload states its own
    timeout per call, and that is the one that governs the work.
    """

    wslc_path: str = "wslc"
    command_timeout_seconds: float = 60.0
