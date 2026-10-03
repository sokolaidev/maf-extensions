"""Explicit exec channels shared by backends with verified Python guests."""

from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass
from typing import Literal

from ._host_tools import BoundedHostToolPolicy
from ._host_tools_over_exec import (
    fold_host_tool_call_transfer_limits,
    guest_run_layout,
    host_tool_calls_over_exec,
)
from ._protocol import (
    Capability,
    ExecResult,
    HostToolPolicy,
    ProgramRequirements,
    Sandbox,
    SandboxLimits,
    SandboxSpec,
    TransferLimits,
)
from ._router import SandboxTransferLimitsNotPermitted
from ._shim import host_tool_shim

_SHIM_LIMIT = 128 * 1024
_PROFILE_PROBE = (
    "import sys,json,math,re,types; "
    "sys.exit(1) if sys.version_info < (3,11) or sys.implementation.name != 'cpython' else None; "
    "print('maf-python-portable-v1')"
)


@dataclass(frozen=True)
class ExecProgramChannel:
    """CPython 3.11+ with json, math, re, sys and types; no third-party imports promised."""

    name: str = "python-exec"
    interpreter: str = "python3"
    mode: Literal["exec", "runtime"] = "exec"
    profiles: frozenset[str] = frozenset({"python-portable-v1"})
    host_tools: bool = True
    capabilities: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})

    def required_capabilities(self, spec: SandboxSpec) -> frozenset[Capability]:
        return self.capabilities | {Capability.FILES_OUT} if spec.host_tools else self.capabilities

    def transfer_limits(self, spec: SandboxSpec) -> SandboxLimits:
        if spec.program is None:
            raise ValueError("a program channel requires program requirements")
        if spec.host_tools is not None:
            # A finite float needs at most 24 characters; reserve that rendering at admission.
            shim_bytes = len(
                host_tool_shim(spec.host_tools.names, call_timeout=1.0).encode("utf-8")
            )
            if shim_bytes + 21 > _SHIM_LIMIT:
                raise SandboxTransferLimitsNotPermitted(
                    "the host-tool shim exceeds its channel limit"
                )
        files_in = (
            spec.files_in if Capability.FILES_IN in spec.requires else TransferLimits(0, 0, 0)
        )
        files_out = (
            spec.files_out if Capability.FILES_OUT in spec.requires else TransferLimits(0, 0, 0)
        )
        extra = spec.program.max_program_bytes + (_SHIM_LIMIT if spec.host_tools else 0)
        staged = TransferLimits(
            max(
                files_in.max_bytes_per_file,
                spec.program.max_program_bytes,
                _SHIM_LIMIT if spec.host_tools else 0,
            ),
            files_in.max_total_bytes + extra,
            files_in.max_files + (2 if spec.host_tools else 1),
        )
        if spec.host_tools is not None:
            return fold_host_tool_call_transfer_limits(staged, files_out, spec.host_tools)
        return SandboxLimits(files_in=staged, files_out=files_out)

    def guest_working_directory(self, guest_call_path: str, *, host_tools: bool = False) -> str:
        return posixpath.join(guest_call_path, "work") if host_tools else guest_call_path

    async def prepare(self, sandbox: Sandbox, requirements: ProgramRequirements) -> None:
        if (
            requirements.profile != "python-portable-v1"
            or requirements.profile not in self.profiles
        ):
            raise ValueError("unsupported Python execution profile")
        result = await sandbox.exec(
            [self.interpreter, "-c", _PROFILE_PROBE], working_directory=".", timeout=30
        )
        if result.exit_code != 0 or result.stdout.strip() != "maf-python-portable-v1":
            raise ValueError("the guest does not satisfy python-portable-v1")

    async def run(
        self,
        sandbox: Sandbox,
        code: str,
        *,
        requirements: ProgramRequirements,
        guest_call_path: str,
        timeout: float,
        policy: HostToolPolicy | None = None,
    ) -> ExecResult:
        try:
            if (
                requirements.profile != "python-portable-v1"
                or requirements.profile not in self.profiles
            ):
                raise ValueError("unsupported Python execution profile")
            if len(code.encode("utf-8")) > requirements.max_program_bytes:
                raise ValueError("the program exceeds its declared byte limit")
            if policy is None:
                await sandbox.write_file("program.py", code, working_directory=guest_call_path)
                return await sandbox.exec(
                    [self.interpreter, "program.py"],
                    working_directory=guest_call_path,
                    timeout=timeout,
                )
            shim = host_tool_shim(policy.surface.names, call_timeout=float(timeout))
            if len(shim.encode("utf-8")) > _SHIM_LIMIT:
                raise ValueError("the host-tool shim exceeds its channel limit")
            layout = guest_run_layout(guest_call_path, program="program.py")
            await sandbox.write_file(
                posixpath.relpath(layout.program, layout.directory),
                code,
                working_directory=layout.directory,
            )
            await sandbox.write_file(
                posixpath.relpath(layout.shim, layout.directory),
                shim,
                working_directory=layout.directory,
            )
            bounded = BoundedHostToolPolicy(
                policy,
                sandbox,
                deadline=time.monotonic() + timeout,
                timeout=requirements.host_tool_timeout_seconds,
            )
            return await host_tool_calls_over_exec(
                sandbox,
                bounded,
                layout,
                timeout=timeout,
                interpreter=self.interpreter,
            )
        finally:
            if policy is not None:
                policy.close()
