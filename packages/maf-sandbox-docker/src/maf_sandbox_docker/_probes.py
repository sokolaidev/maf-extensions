"""Image command checks owned by the docker backend.

Only successful checks are retained. They establish invocation compatibility, never authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from maf_sandbox import Capability, SandboxCapabilityNotSupported, SandboxSpec

_REQUIREMENTS = {
    "sh": frozenset({Capability.EXEC, Capability.HOST_TOOLS}),
    "host-tools": frozenset({Capability.HOST_TOOLS}),
    "rm": frozenset({Capability.FILES_DELETE}),
}
RunProbe = Callable[[tuple[str, ...], bool], Awaitable[int]]


async def probe_commands(spec: SandboxSpec, verified: set[str], run: RunProbe) -> None:
    """Check the requested image prerequisites not yet observed on this sandbox."""
    for name, capabilities in _REQUIREMENTS.items():
        required = capabilities & spec.required_capabilities
        if not required or name in verified:
            continue
        if name == "sh" and Capability.HOST_TOOLS in spec.required_capabilities:
            continue
        guest_missing = f"/.maf-command-probe-{uuid4().hex}"
        commands: list[tuple[tuple[str, ...], bool, int]]
        if name == "sh":
            commands = [(("sh", "-c", "exit 0"), False, 0)]
        elif name == "host-tools":
            # No scratch files: mkdir sees an existing directory, mv a missing source.
            script = (
                "mkdir -p / || exit 11; "
                f"mv -f {guest_missing} {guest_missing}-to >/dev/null 2>&1; "
                '[ "$?" -eq 1 ] || exit 12; '
                "nohup sh -c 'exit 0' </dev/null >/dev/null 2>&1 || exit 13"
            )
            commands = [(("sh", "-c", script), False, 0)]
        elif name == "rm":
            commands = [(("rm", "-rf", "--"), False, 0)]
        else:
            raise AssertionError(name)
        try:
            for argv, as_root, expected in commands:
                status = await run(argv, as_root)
                if status != expected:
                    raise SandboxCapabilityNotSupported(
                        f"sandbox backend 'docker' cannot serve "
                        f"{', '.join(sorted(required))} to workload {spec.kind!r} from "
                        f"image {spec.image_id or spec.image!r}: {name} command probe exited "
                        f"{status}, expected {expected}. Supply an image with working {name} "
                        "commands. The next acquire retries unsuccessful checks."
                    )
        except SandboxCapabilityNotSupported:
            raise
        except Exception as failure:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend 'docker' could not establish "
                f"{', '.join(sorted(required))} for workload {spec.kind!r} from "
                f"image {spec.image_id or spec.image!r}: {name} command probe did not complete. "
                "The next acquire retries; dispose the sandbox before replacing its image."
            ) from failure
        verified.add(name)
        if name == "host-tools":
            verified.add("sh")
