"""The `cleanup-probe` kind: one call that leaves a directory its own reclaim cannot remove.

Acts 1 to 4 drive `router.acquire` directly, because the three disposal moments they are about
are the host's. This one is not: the per-call reclaim runs inside `sandboxed_tool`, and so does
the handler that reports a reclaim which did not happen. So acts 5 and 6 need a tool.

**Nothing here is faked, and that is the whole reason this kind exists.** The program writes a
file, then removes write permission from the directory holding it. Under
`DockerSandboxConfig(cap_drop_all=True)` the container's root holds no `CAP_DAC_OVERRIDE`, so
`rm -rf` cannot empty that directory as root, and the backend's retry as the image's user
cannot either — a mode bit binds the owner too. The removal genuinely fails. No method is
patched, no failure is injected, and what the handler reports is what the engine said.

That coupling is the part worth copying: **hardening the container is what makes cleanup
fallible.** A host that drops capabilities has taken a real risk reduction and bought a real
new failure mode with it, and the reporting path is how it learns which calls paid.

Imported, never run, so it carries no PEP 723 block of its own; `agent.py` declares what both
files need. `sys.path[0]` is the script's directory, which is what lets `agent.py` import it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from maf_sandbox import (
    CallerContext,
    Capability,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    error_detail,
)
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: The sandbox kind these two acts ask for. Its own kind rather than acts 1 to 4's `assistant`,
#: so the hardened container is a different container and the earlier acts keep the posture they
#: were counted under — a container is reused by a name derived from scope, thread, agent dir,
#: kind and egress, and never from the hardening.
CLEANUP_PROBE_KIND = "cleanup-probe"

LEAVE_A_LOCKED_DIRECTORY_TOOL_NAME = "leave_a_locked_directory"

_WORK_DIR = "/maf-sandbox/work"

#: What the program leaves behind, inside the call's own directory.
_LOCKED_DIRECTORY = "locked"
_LOCKED_FILE = "data.txt"
#: The whole mechanism, in three characters: r-x for the owner. Enough to list the directory,
#: not enough to unlink what is inside it — and the owner is bound by that as much as anyone,
#: which is why this survives a removal running as the image's own user.
_LOCKED_MODE = "500"

#: Fixed text. Nothing a model wrote reaches it, and nothing is interpolated: the program runs
#: with the call's own directory as its working directory, so every name here is relative.
_PROGRAM = (
    f"mkdir {_LOCKED_DIRECTORY} && "
    f"echo left behind > {_LOCKED_DIRECTORY}/{_LOCKED_FILE} && "
    f"chmod {_LOCKED_MODE} {_LOCKED_DIRECTORY}"
)

_RECEIPT_FILENAME = "receipt.txt"
_RECEIPT = "this call ran\n"

_DEFAULT_TIMEOUT_SECONDS = 60


def cleanup_probe_spec(image: str) -> SandboxSpec:
    """The sandbox this probe needs, in backend-neutral terms.

    ``Capability.RECLAIM`` is deliberately **not** in ``requires``. The rung is the host's
    decision, resolved by the router from its own floor and what the backend declares — a kind
    demanding it would refuse the backends that dispose after every call, which clean up just
    as completely by a different route. ``agent.py`` reads `router.effective_cleanup(spec)` and
    prints it instead, so the precondition these acts rest on is measured rather than assumed.
    """
    return SandboxSpec(
        kind=CLEANUP_PROBE_KIND,
        image=image,
        egress_allow=(),
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
    )


def make_cleanup_probe_tools(
    router: SandboxRouter | None,
    agent_dir: str,
    context: CallerContext,
    *,
    image: str,
    exec_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> list[Any]:
    """Return the ``[leave_a_locked_directory]`` tool list, or ``[]`` with no sandbox configured.

    No ``on_reclaim_failure=`` here. The handler is set once on the router, as
    ``ReclaimConfig(on_failure=...)``, which is the half a host wires for every kind it attaches
    rather than per tool — `sandboxed_tool` resolves the per-tool override against it and the
    router's is what a packaged kind inherits.
    """
    spec = cleanup_probe_spec(image)
    return sandboxed_tool(
        lambda session: _leave_a_locked_directory_tool(session, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=spec,
        name=LEAVE_A_LOCKED_DIRECTORY_TOOL_NAME,
        approval_mode="never_require",
        # The conservative label, and the spec is why rather than the body. Nothing the store
        # holds reaches this result — it is one fixed sentence — but `requires` opens that
        # channel, and a `trusted` claim over an open channel is refused at attach unless the
        # kind also names it in `nothing_survives_from`. That claim is available and precise;
        # it is not made here because this sample is about cleanup, and `untrusted` costs it
        # nothing. Said rather than defaulted, per rule 1 in `docs/sandbox/kinds/README.md`.
        source_integrity=SourceIntegrity.UNTRUSTED,
        logger=logger,
    )


def _leave_a_locked_directory_tool(
    session: SandboxToolSession,
    timeout: int,
) -> Callable[..., Awaitable[str]]:
    """Build the ``leave_a_locked_directory`` body for one attached tool."""

    async def leave_a_locked_directory() -> str:
        """Run a short program in a sandbox and report whether it finished.

        Takes no arguments: what it runs is fixed by the host.

        Returns:
            One line saying the program ran, or why it could not.
        """
        key = session.key()
        if isinstance(key, str):
            return key

        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

        # Asking for it is what puts it on the framework's list: this path, and everything
        # under it, is removed when the body returns. Which is the removal these acts are
        # about — so this line is load-bearing rather than tidy.
        guest_call_directory = session.guest_call_path()

        try:
            # Also what creates the directory: it is a name until a kind writes to it.
            await sandbox.write_file(
                _RECEIPT_FILENAME, _RECEIPT, working_directory=guest_call_directory
            )
            result = await sandbox.exec(
                ["sh", "-c", _PROGRAM],
                working_directory=guest_call_directory,
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("leave_a_locked_directory: the program timed out after %ss", timeout)
            return f"Error: the program timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            # Transport detail can carry infrastructure names — not into the transcript.
            logger.warning("leave_a_locked_directory: %s", error_detail(exc))
            return "Error: could not run the program in the sandbox"

        if result.exit_code != 0:
            logger.info("leave_a_locked_directory: the program exited %d", result.exit_code)
            return "Error: the program did not finish"

        # The call answers normally. What follows — the reclaim that cannot happen, the
        # disposal that answers it, and the handler that reports both — happens after this
        # return, and changes none of it. That separation is act 5's whole claim.
        return f"The program ran and left {_LOCKED_DIRECTORY}/{_LOCKED_FILE} behind."

    return leave_a_locked_directory
