"""``execute_code``: the CodeAct sandbox workload.

The agent gets one tool, the model writes a short Python program, and the program runs inside
a sandbox — computing an answer instead of reasoning about what the computation would produce.

**This module contains no Azure import, no backend import and no sandbox lifecycle code.**  It
talks to a :class:`~maf_sandbox.SandboxRouter` and gets back ``write_file`` and ``exec``, so
the same tool runs unchanged against ACA Sandboxes, a WSL container or an in-process fake.

This version is the ``EXEC`` road and it is stdout-only.  Nothing is read back out of the
sandbox, no workspace file is shared into it, and no host function is dispatchable from
inside: with the spec's egress closed as well, nothing external can enter and nothing leaves
but what the program printed.  That empty dispatch surface is what makes running model-written
code defensible here, so it is a property of this version rather than a gap in it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from maf_sandbox import (
    Capability,
    ExecResult,
    SandboxRouter,
    SandboxSpec,
    WorkspaceContext,
    error_detail,
)
from maf_sandbox.maf import SandboxToolSession, sandbox_tool_declarations, sandboxed_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "CODEACT_KIND",
    "EXECUTE_CODE_TOOL_NAME",
    "codeact_sandbox_spec",
    "make_codeact_tools",
]

EXECUTE_CODE_TOOL_NAME = "execute_code"

#: The sandbox kind this workload asks for.
CODEACT_KIND = "codeact"

#: Where the program is written and run — a dedicated root rather than the image's own tree.
_WORK_DIR = "/work"

#: One fixed name, rewritten on every call, since the sandbox is reused across calls.
_PROGRAM_FILENAME = "program.py"

_INTERPRETER = "python3"

#: Writing an expression and expecting a REPL to echo it is the commonest way a first CodeAct
#: call comes back empty, so the answer says what to do instead.
_NO_OUTPUT = (
    "The program ran and printed nothing. Only what you print comes back — end the program "
    "with print(...) of what you need to see."
)


def codeact_sandbox_spec(image: str | None = None, image_id: str | None = None) -> SandboxSpec:
    """The sandbox a CodeAct program needs, in backend-neutral terms.

    ``egress_allow=()`` and no ``min_isolation`` are both deliberate: the program computes
    rather than fetches, and this kind runs only what the model wrote, so the host's floor
    governs.
    """
    return SandboxSpec(
        kind=CODEACT_KIND,
        image=image,
        image_id=image_id,
        egress_allow=(),
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
    )


def make_codeact_tools(
    router: SandboxRouter | None,
    agent_dir: str,
    context: WorkspaceContext,
    *,
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: int = 120,
) -> list[Any]:
    """Return the ``[execute_code]`` tool list, or ``[]`` when no sandbox is available.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread.
        image: OCI reference of a sandbox image with a Python interpreter on its path.
        image_id: A backend-native disk-image id, skipping resolution.
        exec_timeout_seconds: Per-program bound. A sandbox that stops answering must not hold
            the caller's turn open.
    """
    spec = codeact_sandbox_spec(image, image_id)
    return sandboxed_tool(
        lambda session: _execute_code_tool(session, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=spec,
        name=EXECUTE_CODE_TOOL_NAME,
        approval_mode="never_require",
        # No `source_integrity`: undeclared defaults to untrusted, the fail-safe direction.
        declarations=sandbox_tool_declarations(spec, source_integrity=None),
        logger=logger,
    )


def _execute_code_tool(
    session: SandboxToolSession,
    timeout: int,
) -> "Callable[..., Awaitable[str]]":
    """Build the ``execute_code`` body for one attached tool.

    Defined at module level rather than nested inside :func:`make_codeact_tools`, and that is
    not a style choice: the function below's **docstring is the tool's description** — MAF
    passes ``__doc__`` through verbatim, indentation and all — so nesting this one level
    deeper would re-indent every line of what the model reads at call time.
    """

    async def execute_code(code: str) -> str:
        """Run a short Python program inside a sandbox and return what it printed.

        Use this to compute rather than to reason: parse, transform, count, check, simulate —
        anything where running the code beats predicting what it would do.  The program runs
        as ``python3 program.py`` in a sandbox with **no network access**, so it can compute
        but cannot fetch.

        **Only what you print comes back.**  There is no REPL echo and the value of the last
        expression is not returned, so end the program with ``print(...)`` of everything you
        need to see.  Nothing is read back out of the sandbox either — a file your program
        writes may still be there on a later call in the same conversation, but the printed
        output is the whole result.

        Write a complete, self-contained program every time.  Each call replaces the previous
        one, and nothing carries over except what the program itself wrote to disk.

        Args:
            code: The Python source to run.  The standard library, plus whatever the sandbox
                image ships.

        Returns:
            The program's stdout, its stderr when it wrote any, and its exit code when that
            was not zero.  If the sandbox is unavailable the tool returns an error message
            instead, so the run degrades rather than blocking.
        """
        # Scope and thread come from the host's request context, never from model input.
        key = session.key()
        if isinstance(key, str):
            return key

        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

        program_path = f"{session.spec.work_dir}/{_PROGRAM_FILENAME}"
        try:
            await sandbox.write_file(program_path, code)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_code: could not write the program into the sandbox: %s",
                error_detail(exc),
            )
            return "Error: could not write the program into the sandbox"

        try:
            # An argv sequence, never a command line: the model's source never reaches a shell.
            result = await sandbox.exec(
                [_INTERPRETER, program_path],
                working_directory=session.spec.work_dir,
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("execute_code: the program timed out after %ss", timeout)
            return f"Error: the program timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            # Provider/transport detail can carry account ids — must not reach the transcript.
            logger.warning("execute_code: exec failed: %s", error_detail(exc))
            return "Error: could not run the program in the sandbox"

        logger.info("execute_code: ran exit_code=%d", result.exit_code)
        return _format_result(result)

    return execute_code


def _format_result(result: ExecResult) -> str:
    """Render one run for a model that has to fix its own program.

    Empty sections are omitted rather than shown blank, and the trailing newline ``print``
    leaves is dropped, so a one-line program's answer is one line.
    """
    stdout = (result.stdout or "").rstrip("\n")
    stderr = (result.stderr or "").rstrip("\n")
    sections: list[str] = []
    if stdout:
        sections.append(f"stdout:\n{stdout}")
    if stderr:
        sections.append(f"stderr:\n{stderr}")
    if result.exit_code:
        sections.append(f"exit code: {result.exit_code}")
    return "\n\n".join(sections) if sections else _NO_OUTPUT
