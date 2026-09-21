"""Image command checks owned by the wslc backend.

Only successful checks are retained. They establish invocation compatibility, never authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from maf_sandbox import Capability, SandboxCapabilityNotSupported, SandboxSpec

# Raised probes must not resolve an executable through the guest's PATH.
TEST_COMMAND = "/usr/bin/test"

_REQUIREMENTS = {
    "sh": frozenset({Capability.EXEC}),
    TEST_COMMAND: frozenset({Capability.FILES_IN}),
    "write": frozenset({Capability.FILES_IN}),
    # Working-directory setup runs for either capability, so its prerequisites are checked
    # for both. Without this an image passes every advertised probe and then fails setup
    # with an untyped error, which a caller cannot tell from a broken engine.
    "setup": frozenset({Capability.EXEC, Capability.FILES_IN}),
}
#: What ``write_file`` runs as the image's user, besides ``sh``.
_WRITE_COMMANDS = ("mkdir", "cat", "wc", "mv", "rm")
#: What working-directory setup runs as root, from the pinned system ``PATH``.
_SETUP_COMMANDS = ("mkdir", "chown")
#: The shell setup runs, named absolutely so no lookup path can choose it.
SETUP_SHELL = "/bin/sh"
#: The ``PATH`` setup pins; the probe must resolve its commands the same way.
SETUP_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
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
        elif name == TEST_COMMAND:
            commands = [((name, "-d", "/"), True, 0), ((name, "-e", guest_missing), True, 1)]
        elif name == "write":
            script = "; ".join(
                f"command -v {command} >/dev/null || exit 1" for command in _WRITE_COMMANDS
            )
            commands = [(("sh", "-c", script), False, 0)]
        elif name == "setup":
            script = f"export PATH={SETUP_PATH}; " + "; ".join(
                f"command -v {command} >/dev/null || exit 1" for command in _SETUP_COMMANDS
            )
            commands = [((SETUP_SHELL, "-c", script), True, 0)]
        else:
            raise AssertionError(name)
        labels = {
            "write": f"sh and {', '.join(_WRITE_COMMANDS)}",
            "setup": f"root {SETUP_SHELL} and {', '.join(_SETUP_COMMANDS)} on {SETUP_PATH}",
        }
        label = labels.get(name, name)
        try:
            for argv, as_root, expected in commands:
                status = await run(argv, as_root)
                if status != expected:
                    raise SandboxCapabilityNotSupported(
                        f"sandbox backend 'wslc' cannot serve "
                        f"{', '.join(sorted(required))} to workload {spec.kind!r} from "
                        f"image {spec.image_id or spec.image!r}: {label} command probe exited "
                        f"{status}, expected {expected}. Supply an image with working {label} "
                        "commands. The next acquire retries unsuccessful checks."
                    )
        except SandboxCapabilityNotSupported:
            raise
        except Exception as failure:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend 'wslc' could not establish "
                f"{', '.join(sorted(required))} for workload {spec.kind!r} from "
                f"image {spec.image_id or spec.image!r}: {label} command probe did not complete. "
                "The next acquire retries; dispose the sandbox before replacing its image."
            ) from failure
        verified.add(name)
