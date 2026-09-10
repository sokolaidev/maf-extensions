"""Image command checks owned by the wslc backend.

Only successful checks are retained. They establish invocation compatibility, never authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from maf_sandbox import Capability, SandboxCapabilityNotSupported, SandboxSpec

_REQUIREMENTS = {
    "sh": frozenset({Capability.EXEC}),
    "test": frozenset({Capability.FILES_IN}),
}
RunProbe = Callable[[tuple[str, ...], bool], Awaitable[int]]


async def probe_commands(spec: SandboxSpec, verified: set[str], run: RunProbe) -> None:
    """Check the requested image prerequisites not yet observed on this sandbox."""
    for name, capabilities in _REQUIREMENTS.items():
        required = capabilities & spec.required_capabilities
        if not required or name in verified:
            continue
        guest_missing = f"/.maf-command-probe-{uuid4().hex}"
        commands: list[tuple[tuple[str, ...], bool, int]]
        if name == "sh":
            commands = [(("sh", "-c", "exit 0"), False, 0)]
        elif name == "test":
            commands = [(("test", "-d", "/"), True, 0), (("test", "-e", guest_missing), True, 1)]
        else:
            raise AssertionError(name)
        try:
            for argv, as_root, expected in commands:
                status = await run(argv, as_root)
                if status != expected:
                    raise SandboxCapabilityNotSupported(
                        f"sandbox backend 'wslc' cannot serve "
                        f"{', '.join(sorted(required))} to workload {spec.kind!r} from "
                        f"image {spec.image_id or spec.image!r}: {name} command probe exited "
                        f"{status}, expected {expected}. Supply an image with working {name} "
                        "commands. The next acquire retries unsuccessful checks."
                    )
        except SandboxCapabilityNotSupported:
            raise
        except Exception as failure:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend 'wslc' could not establish "
                f"{', '.join(sorted(required))} for workload {spec.kind!r} from "
                f"image {spec.image_id or spec.image!r}: {name} command probe did not complete. "
                "The next acquire retries; dispose the sandbox before replacing its image."
            ) from failure
        verified.add(name)
