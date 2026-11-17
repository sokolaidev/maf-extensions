"""Portable Python programs through MXC's bounded native directory transport."""

from __future__ import annotations

from typing import Literal

from maf_sandbox import (
    Capability,
    ExecResult,
    HostToolPolicy,
    ProgramRequirements,
    Sandbox,
    SandboxLimits,
    SandboxSpec,
    TransferLimits,
)

from .files.request import MAX_CODE


class MxcProgramChannel:
    """The pinned rich-Python profile, with no guest host-tool authority."""

    name = "mxc-python"
    mode: Literal["runtime"] = "runtime"
    profiles = frozenset({"python-portable-v1"})
    host_tools = False

    def required_capabilities(self, spec: SandboxSpec) -> frozenset[Capability]:
        return frozenset()

    def transfer_limits(self, spec: SandboxSpec) -> SandboxLimits:
        if spec.program is None:
            raise ValueError("MXC requires explicit program requirements")
        self._requirements(spec.program)
        if spec.host_tools is not None:
            raise ValueError("MXC does not expose host tools")
        return SandboxLimits(
            files_in=spec.files_in
            if Capability.FILES_IN in spec.requires
            else TransferLimits(0, 0, 0),
            files_out=spec.files_out
            if Capability.FILES_OUT in spec.requires
            else TransferLimits(0, 0, 0),
        )

    def _requirements(self, requirements: ProgramRequirements) -> None:
        if requirements.profile not in self.profiles or requirements.max_program_bytes > MAX_CODE:
            raise ValueError("MXC requires python-portable-v1 with at most 65536 program bytes")

    def guest_working_directory(self, guest_call_path: str, *, host_tools: bool = False) -> str:
        if host_tools:
            raise ValueError("MXC does not expose host tools")
        return guest_call_path

    async def prepare(self, sandbox: Sandbox, requirements: ProgramRequirements) -> None:
        from ._backend import MxcSandbox

        self._requirements(requirements)
        if not isinstance(sandbox, MxcSandbox):
            raise ValueError("MXC channel requires its native backend")
        sandbox.authorize()

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
        from ._backend import MxcSandbox

        if policy is not None:
            policy.close()
            raise ValueError("MXC does not expose host tools")
        await self.prepare(sandbox, requirements)
        if len(code.encode()) > requirements.max_program_bytes:
            raise ValueError("program exceeds its admitted limit")
        assert isinstance(sandbox, MxcSandbox)
        return await sandbox.run_call(
            guest_call_path,
            code,
            guest_call_path=guest_call_path,
            timeout=timeout,
        )
