"""``execute_code``: the CodeAct sandbox workload.

The agent gets one tool, the model writes a short Python program, and the program runs inside
a sandbox — computing an answer instead of reasoning about what the computation would produce.

**This module contains no Azure import, no backend import and no sandbox lifecycle code.**  It
talks to a :class:`~maf_sandbox.SandboxRouter` and gets back ``write_file``, ``exec`` and the
pull surface, so the same tool runs unchanged against ACA Sandboxes, a Docker container or an
in-process fake.

Three channels, and the host chooses which of them exist.  Stdout is always there.  A
**workspace store** adds a ``files`` parameter, so a program can transform files that already
exist rather than only data the model wrote into its own source.  An **output sink** plus a
:class:`CodeactOutputs` mode adds a way for files the program produces to reach host state.
Wire neither and this is the stdout-only kind it has always been, with nothing dispatchable
from inside: no network, no host functions, and nothing leaving but what the program printed.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    Capability,
    DeclaredOutput,
    ExecResult,
    NameNormalization,
    OutputSink,
    SandboxArtifactNameInvalid,
    SandboxOutputError,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
    WorkspaceContext,
    collect_outputs,
    error_detail,
    validate_artifact_name,
)
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from agent_framework import AgentFileStore
    from maf_sandbox import LandedArtifact, Sandbox

logger = logging.getLogger(__name__)

__all__ = [
    "CODEACT_KIND",
    "EXECUTE_CODE_TOOL_NAME",
    "CodeactOutputs",
    "codeact_sandbox_spec",
    "make_codeact_tools",
]

EXECUTE_CODE_TOOL_NAME = "execute_code"

#: The sandbox kind this workload asks for.
CODEACT_KIND = "codeact"

#: Where every run's directory is created — a dedicated root rather than the image's own tree.
_WORK_DIR = "/work"

#: One fixed name inside each run's own directory.
_PROGRAM_FILENAME = "program.py"

#: Where a ``MANIFEST``-mode program says what it produced.
_MANIFEST_FILENAME = "outputs.json"
_MANIFEST_OUTPUTS_KEY = "outputs"
_MANIFEST_PATH_KEY = "path"

#: A listing of what a program wrote is text, and a small amount of it.
_MANIFEST_MAX_BYTES = 64 * 1024

#: The shortest manifest that names one file — the floor a host's byte caps must clear before
#: this channel can deliver anything at all.
_SMALLEST_MANIFEST = len(
    json.dumps({_MANIFEST_OUTPUTS_KEY: [{_MANIFEST_PATH_KEY: "a"}]}, separators=(",", ":"))
)

#: Said whenever a collection fails part-way: `collect_outputs` delivers one artifact at a
#: time and cannot un-deliver, so "could not be saved" is not the same as "nothing was saved".
_MAY_HAVE_LANDED = "Some of them may already have been saved; do not assume none were."

_INTERPRETER = "python3"

#: Eight artifacts is a generous single call and a cap that actually bounds something; the
#: byte ceilings are the shared defaults, so only the count is this workload's own opinion.
_DEFAULT_FILES_OUT = replace(DEFAULT_TRANSFER_LIMITS, max_files=8)

#: Writing an expression and expecting a REPL to echo it is the commonest way a first CodeAct
#: call comes back empty, so the answer says what to do instead.
_NO_OUTPUT = (
    "The program ran and printed nothing. Only what you print is read back as text — end the "
    "program with print(...) of what you need to see."
)


class CodeactOutputs(StrEnum):
    """How a program's output files are named — the host's choice, made at construction.

    Both roads collect literal paths and neither enumerates a directory, so this kind requires
    :data:`~maf_sandbox.Capability.FILES_OUT` and never
    :data:`~maf_sandbox.Capability.FILES_LIST`, and runs on every backend serving the first.
    """

    #: No output channel at all: stdout is the whole result, and no sink is needed.
    NONE = "none"
    #: The model names its files in the call, before the program runs. Names are validated and
    #: capped up front, and one declared but not written is reported back by name.
    DECLARED = "declared"
    #: The program writes ``outputs.json`` saying what it produced. For a program whose output
    #: names it can only know once it has read its input.
    MANIFEST = "manifest"


def codeact_sandbox_spec(
    image: str | None = None,
    image_id: str | None = None,
    *,
    outputs: CodeactOutputs = CodeactOutputs.NONE,
    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    files_out: TransferLimits = _DEFAULT_FILES_OUT,
) -> SandboxSpec:
    """The sandbox a CodeAct program needs, in backend-neutral terms.

    ``egress_allow=()`` and no ``min_isolation`` are both deliberate: the program computes
    rather than fetches, and this kind runs only what the model wrote, so the host's floor
    governs.  An output mode other than :data:`CodeactOutputs.NONE` grows ``requires`` by
    :data:`~maf_sandbox.Capability.FILES_OUT` and sets ``outputs_named_at_call_time``, which is
    what keeps the attached tool honest about landing artifacts it cannot yet name.
    """
    collects = outputs is not CodeactOutputs.NONE
    requires = {Capability.EXEC, Capability.FILES_IN}
    if collects:
        requires.add(Capability.FILES_OUT)
    return SandboxSpec(
        kind=CODEACT_KIND,
        image=image,
        image_id=image_id,
        egress_allow=(),
        work_dir=_WORK_DIR,
        requires=frozenset(requires),
        outputs_named_at_call_time=collects,
        files_in=files_in,
        files_out=files_out,
    )


def make_codeact_tools(
    router: SandboxRouter | None,
    agent_dir: str,
    context: WorkspaceContext,
    *,
    workspace_store: "AgentFileStore | None" = None,
    output_sink: OutputSink | None = None,
    outputs: CodeactOutputs = CodeactOutputs.NONE,
    outbound_max_confidentiality: str | None = None,
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: int = 120,
    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    files_out: TransferLimits = _DEFAULT_FILES_OUT,
) -> list[Any]:
    """Return the ``[execute_code]`` tool list, or ``[]`` when no sandbox is available.

    The tool's *signature* follows the channels the host wired: ``files`` appears only with a
    ``workspace_store``, and ``outputs`` only under :data:`CodeactOutputs.DECLARED`.  A model is
    never shown a parameter this deployment cannot honour.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread, and how to enumerate the workspace.
        workspace_store: The agent's workspace store. Given one, the tool takes a ``files``
            parameter and shares those files into the sandbox; the caller's listing is the
            authority on which names exist, exactly as it is for the Bicep kind.
        output_sink: Where produced files land. Required by any mode but
            :data:`CodeactOutputs.NONE`, and refused at attach without one.
        outputs: How a program's output files are named. See :class:`CodeactOutputs`.
        outbound_max_confidentiality: The host's cap for tools that carry something out, in the
            host's own vocabulary. Off by default and written only when this workload lands
            something — with egress closed, the sink *is* the flow it gates.
        image: OCI reference of a sandbox image with a Python interpreter on its path.
        image_id: A backend-native disk-image id, skipping resolution.
        exec_timeout_seconds: Per-program bound. A sandbox that stops answering must not hold
            the caller's turn open.
        files_in: What one call may share into the sandbox. Enforced here, because no backend's
            ``write_file`` knows the workload's caps — a spec that declared a bound nothing
            applied would be worse than one that declared none.
        files_out: The collection's caps. ``max_files`` is what bounds how many artifacts one
            call may declare, so it is a property of the workload rather than of the guest.

    Raises:
        ValueError: when a sink is supplied with nothing to send down it — an output mode of
            :data:`CodeactOutputs.NONE`.
        ~maf_sandbox.SandboxOutputSinkRequired: when an output mode is asked for and no sink
            was given. Raised by ``sandboxed_tool`` rather than here, and a
            :class:`RuntimeError` rather than a :class:`ValueError`, so a caller catching one
            of the two does not catch the other.

    Both wait for the attach gate: a host with no sandbox configured gets ``[]``, never an
    exception.
    """
    configured = router is not None and router.enabled
    if configured and files_in.max_files < 1:
        # `program.py` is one inbound file on every call, so a cap below one refuses all of
        # them. Every impossible pairing below is caught here rather than per call: a tool the
        # model can see and can never use successfully is worse than one that never attached.
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: files_in.max_files is {files_in.max_files}, and the "
            f"program itself is one file written into the sandbox on every call, so no call "
            f"could succeed."
        )
    if configured and outputs is CodeactOutputs.DECLARED and files_out.max_files < 1:
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: outputs={str(CodeactOutputs.DECLARED)!r} shows the "
            f"model an `outputs` parameter, and files_out.max_files of "
            f"{files_out.max_files} would refuse every non-empty use of it."
        )
    if configured and outputs is CodeactOutputs.MANIFEST:
        # The manifest occupies one slot of the collection and at least `_SMALLEST_MANIFEST`
        # of its bytes, so a cap below either leaves room for nothing else: the channel is
        # wired and could never deliver an artifact.
        if files_out.max_files < 2:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: outputs={str(CodeactOutputs.MANIFEST)!r} needs "
                f"files_out.max_files of at least 2 — one for {_MANIFEST_FILENAME} and one "
                f"for an artifact — and this host allows {files_out.max_files}."
            )
        room = min(files_out.max_bytes_per_file, files_out.max_total_bytes)
        if room <= _SMALLEST_MANIFEST:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: outputs={str(CodeactOutputs.MANIFEST)!r} needs "
                f"more than {_SMALLEST_MANIFEST} bytes of files_out — the smallest "
                f"{_MANIFEST_FILENAME} naming one file, plus the file — and this host allows "
                f"{room}."
            )
    if configured and outputs is CodeactOutputs.NONE and output_sink is not None:
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: an output sink was supplied with outputs="
            f"{str(CodeactOutputs.NONE)!r}, so nothing would ever be landed in it. Pass an "
            f"outputs mode, or drop the sink."
        )
    spec = codeact_sandbox_spec(
        image, image_id, outputs=outputs, files_in=files_in, files_out=files_out
    )
    return sandboxed_tool(
        lambda session: _execute_code_tool(session, workspace_store, outputs, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=spec,
        name=EXECUTE_CODE_TOOL_NAME,
        approval_mode="never_require",
        # The library's "trusted" default is right for a compiler's diagnostics and wrong here:
        # what comes back is whatever a model-written `print(...)` chose to emit. Undeclared,
        # the tracker's untrusted default applies and the result taints the conversation.
        source_integrity=None,
        outbound_max_confidentiality=outbound_max_confidentiality,
        output_sink=output_sink,
        logger=logger,
    )


# --- The tool's description, assembled from the channels the host wired --------------------
#
# Six combinations of `files` and an output mode share one body, so the description is built
# rather than written six times. It still reaches the model exactly as `__doc__`.

_DESCRIPTION_HEAD = """Run a short Python program inside a sandbox and return what it printed.

        Use this to compute rather than to reason: parse, transform, count, check, simulate —
        anything where running the code beats predicting what it would do.  The program runs
        as ``python3 program.py`` in a sandbox with **no network access**, so it can compute
        but cannot fetch.

        **Only what you print is read back as text.**  There is no REPL echo and the value of
        the last expression is not returned, so end the program with ``print(...)`` of
        everything you need to see.

        Write a complete, self-contained program every time.  Each call gets a fresh working
        directory: nothing you did not pass in to *this* call is in it."""

_DESCRIPTION_FILES = """**To work on existing files, list them in ``files``.**  Each one is
        copied into the program's working directory under its own name, so a file listed as
        ``data/sales.csv`` is read by the program as ``data/sales.csv``.  A file you do not
        list is not there."""

_DESCRIPTION_DECLARED = """**To produce files, name them in ``outputs`` and write them into
        the working directory.**  They are saved to host storage after the program exits and
        you get back a reference to where each one landed — the file contents do **not** come
        back, so do not claim to have read a file you only produced.  A name you declare and
        do not write is reported to you rather than silently dropped, and a file you write
        without declaring is not saved at all.

        Naming a file in both ``files`` and ``outputs`` is how you edit one in place.  It is
        the one case where "declared and not written" cannot be reported, because the copy you
        were given is already there: a program that **exits cleanly** without rewriting it saves
        the original back unchanged.  A program that fails saves nothing at all."""

_DESCRIPTION_MANIFEST = f"""**To produce files, write them into the working directory and
        list them in ``{_MANIFEST_FILENAME}``**, in that directory, like this::

            {{"{_MANIFEST_OUTPUTS_KEY}": [{{"{_MANIFEST_PATH_KEY}": "report.csv"}},
              {{"{_MANIFEST_PATH_KEY}": "chart.png"}}]}}

        Every file listed there is saved to host storage after the program exits and you get
        back a reference to where each one landed — the file contents do **not** come back.  A
        file you write without listing is not saved, and no ``{_MANIFEST_FILENAME}`` means
        nothing is saved."""

_DESCRIPTION_ARG_CODE = """code: The Python source to run.  The standard library, plus
                whatever the sandbox image ships."""

_DESCRIPTION_ARG_FILES = """files: Workspace-relative paths to share into the sandbox, or
                omit for none.  Only files in your workspace listing can be shared."""

_DESCRIPTION_ARG_OUTPUTS = """outputs: The file names your program will write into its
                working directory, or omit if it writes none."""

_DESCRIPTION_RETURNS = """The program's stdout, its stderr when it wrote any, and its exit
            code when that was not zero.  If the sandbox is unavailable the tool returns an
            error message instead, so the run degrades rather than blocking."""

_DESCRIPTION_RETURNS_SAVED = """  A run that saved files also names where each one landed."""


def _tool_description(*, takes_files: bool, outputs: CodeactOutputs) -> str:
    """The description the model reads, for the channels this host actually wired."""
    body = [_DESCRIPTION_HEAD]
    arguments = [_DESCRIPTION_ARG_CODE]
    if takes_files:
        body.append(_DESCRIPTION_FILES)
        arguments.append(_DESCRIPTION_ARG_FILES)
    if outputs is CodeactOutputs.DECLARED:
        body.append(_DESCRIPTION_DECLARED)
        arguments.append(_DESCRIPTION_ARG_OUTPUTS)
    elif outputs is CodeactOutputs.MANIFEST:
        body.append(_DESCRIPTION_MANIFEST)
    returns = _DESCRIPTION_RETURNS
    if outputs is not CodeactOutputs.NONE:
        returns += _DESCRIPTION_RETURNS_SAVED
    return (
        "\n\n        ".join(body)
        + "\n\n        Args:\n            "
        + "\n            ".join(arguments)
        + "\n\n        Returns:\n            "
        + returns
        + "\n        "
    )


def _execute_code_tool(
    session: SandboxToolSession,
    store: "AgentFileStore | None",
    outputs: CodeactOutputs,
    timeout: int,
) -> "Callable[..., Awaitable[str]]":
    """Build the ``execute_code`` body for one attached tool.

    Four signatures over one implementation, because MAF derives the tool's schema from the
    function's parameters: a host that wired no workspace store must not be shown ``files``.
    """

    async def run(code: str, files: list[str] | None, declared: list[str] | None) -> str:
        return await _execute(session, store, outputs, timeout, code, files or [], declared or [])

    async def with_files_and_outputs(
        code: str, files: list[str] | None = None, outputs: list[str] | None = None
    ) -> str:
        return await run(code, files, outputs)

    async def with_files(code: str, files: list[str] | None = None) -> str:
        return await run(code, files, None)

    async def with_outputs(code: str, outputs: list[str] | None = None) -> str:
        return await run(code, None, outputs)

    async def plain(code: str) -> str:
        return await run(code, None, None)

    takes_files = store is not None
    declares = outputs is CodeactOutputs.DECLARED
    body = (
        with_files_and_outputs
        if takes_files and declares
        else with_files
        if takes_files
        else with_outputs
        if declares
        else plain
    )
    body.__doc__ = _tool_description(takes_files=takes_files, outputs=outputs)
    return body


async def _execute(
    session: SandboxToolSession,
    store: "AgentFileStore | None",
    outputs: CodeactOutputs,
    timeout: int,
    code: str,
    files: list[str],
    declared: list[str],
) -> str:
    """One ``execute_code`` call: share, run, and collect."""
    # Scope and thread come from the host's request context, never from model input.
    key = session.key()
    if isinstance(key, str):
        return key

    # Two names this kind writes into every run's directory itself, so neither an input nor an
    # output may claim one. The manifest is reserved only where it means something.
    reserved = {_PROGRAM_FILENAME}
    if outputs is CodeactOutputs.MANIFEST:
        reserved.add(_MANIFEST_FILENAME)

    # Chosen here rather than after `acquire`, so that a declared name can be judged against
    # the guest path it will actually become — the prefix is 13 bytes of the 255 a name gets.
    run_id = uuid4().hex[:12]

    names: list[str] = []
    if outputs is CodeactOutputs.DECLARED:
        checked = _validated_output_names(
            declared,
            max_files=session.spec.files_out.max_files,
            reserved=reserved,
            run_id=run_id,
            normalization=_normalization(session),
        )
        if isinstance(checked, str):
            return checked
        names = checked

    # Cap before acquiring anything, and cap *as we go*: a bound that answers only once
    # everything is in memory has already spent what it exists to bound. Every check below
    # therefore happens before the read it would have prevented — the count before the listing,
    # the program's own bytes before the store is touched at all, and each file's as it arrives.
    # `program.py` is counted with them: the spec requires FILES_IN even with no store, because
    # the program is the one thing that crosses this boundary on every single call.
    limits = session.spec.files_in
    tally = _InboundTally(limits)
    shared: list[tuple[str, str]] = []
    over_cap = _over_file_count(len(files) + 1, limits) or tally.add(_PROGRAM_FILENAME, code)
    if over_cap is not None:
        return over_cap
    if store is not None:
        resolved = await _resolve_workspace_files(session, store, files, reserved=reserved)
        if isinstance(resolved, str):
            return resolved
        read = await _read_workspace_files(store, resolved, tally)
        if isinstance(read, str):
            return read
        shared = read

    sandbox = await session.acquire(key)
    if isinstance(sandbox, str):
        return sandbox

    # Fresh per call, because `acquire` is get-or-create: see where `run_id` is chosen.
    run_dir = f"{session.spec.work_dir}/{run_id}"

    for name, content in shared:
        refusal = await _write_shared(sandbox, name, f"{run_dir}/{name}", content)
        if refusal is not None:
            return refusal

    program_path = f"{run_dir}/{_PROGRAM_FILENAME}"
    try:
        await sandbox.write_file(program_path, code)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_code: could not write the program into the sandbox: %s", error_detail(exc)
        )
        return "Error: could not write the program into the sandbox"

    try:
        # An argv sequence, never a command line: the model's source never reaches a shell.
        result = await sandbox.exec(
            [_INTERPRETER, program_path], working_directory=run_dir, timeout=timeout
        )
    except TimeoutError:
        logger.warning("execute_code: the program timed out after %ss", timeout)
        return f"Error: the program timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        # Provider/transport detail can carry account ids — must not reach the transcript.
        logger.warning("execute_code: exec failed: %s", error_detail(exc))
        return "Error: could not run the program in the sandbox"

    logger.info("execute_code: ran exit_code=%d shared=%d", result.exit_code, len(shared))
    report = _format_result(result)
    nothing_to_collect = outputs is CodeactOutputs.NONE or (
        outputs is CodeactOutputs.DECLARED and not names
    )
    if nothing_to_collect or result.exit_code != 0:
        # A program that failed is unlikely to have written what it promised, and a missing-file
        # report stacked on a traceback buries the thing the model has to fix.
        return report
    collected = await _collect(session, sandbox, run_id, outputs, names, reserved)
    return f"{report}\n\n{collected}" if collected else report


# --- Files in ------------------------------------------------------------------------------


async def _resolve_workspace_files(
    session: SandboxToolSession,
    store: "AgentFileStore",
    files: list[str],
    *,
    reserved: set[str],
) -> list[str] | str:
    """Match each requested name against the caller's listing, or answer with the refusal.

    The listing is the injection-pinning boundary: a name the model invented, or read out of a
    poisoned file, has nowhere to go.  Which is why a listing that cannot be read is a refusal
    rather than an empty one — every name would then be refused for the wrong reason.
    """
    if not files:
        return []
    listing = await session.list_files(store)
    if isinstance(listing, str):
        return listing
    known = set(listing)
    resolved: list[str] = []
    for name in files:
        try:
            validate_artifact_name(name)
        except SandboxArtifactNameInvalid as exc:
            # The validator's own sentence, which names the rule that was broken: a fixed
            # message listing two of its rules tells a caller refused for a backslash or a
            # control character that its name satisfies everything the tool asked for. The
            # listing is still not echoed — that would invite a retry with another spelling.
            return f"Error: {name!r} cannot be shared — {exc}"
        if name in reserved:
            return (
                f"Error: {name!r} cannot be shared — this tool writes a file of that name into "
                f"every run's directory."
            )
        if name in resolved:
            # One read and one write per name. Repeating one buys the caller nothing and
            # multiplies both, which is the cheapest way to amplify against the byte ceilings.
            return f"Error: {name!r} was listed twice."
        if name not in known:
            logger.warning(
                "execute_code: %r is not in this tool's workspace listing (%d file(s) visible) "
                "— the store wired here may be narrower than the agent's",
                name,
                len(listing),
            )
            return (
                f"Error: {name!r} is not in this tool's file listing, so it was not shared. "
                f"{_listing_hint(name, listing)}"
            )
        resolved.append(name)
    return resolved


#: Capped so a large workspace cannot flood the model's context.
_LISTING_HINT_MAX = 20


def _listing_hint(name: str, listing: list[str]) -> str:
    """The listing, or its near misses — what resolves a typo without another round trip."""
    if not listing:
        return "This tool's listing is empty — no files were shared with it."
    near = [known for known in listing if known.rsplit("/", 1)[-1] == name.rsplit("/", 1)[-1]]
    if near and near != [name]:
        return f"Did you mean: {', '.join(sorted(near)[:_LISTING_HINT_MAX])}?"
    shown = sorted(listing)[:_LISTING_HINT_MAX]
    more = f" (+{len(listing) - len(shown)} more)" if len(listing) > len(shown) else ""
    return f"Files visible here: {', '.join(shown)}{more}."


async def _read_workspace_files(
    store: "AgentFileStore", names: list[str], tally: "_InboundTally"
) -> list[tuple[str, str]] | str:
    """Read every requested file into memory, or answer with the refusal.

    Each file is counted **as it arrives**, so a breach stops the next read rather than the
    write: a tally applied to the finished set bounds what crosses into the sandbox and nothing
    about what this process spent getting there.

    Text only: ``AgentFileStore.read`` answers with ``str``, and this path encodes what it
    is given.  The protocol's ``write_file`` takes ``bytes``, so the boundary below is not what
    stands in the way of a binary input — but this function and the tally would both have to
    learn about it.
    """
    read: list[tuple[str, str]] = []
    for name in names:
        try:
            content = await store.read(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("execute_code: could not read %r from workspace: %s", name, exc)
            return f"Error: could not read {name!r} from workspace"
        if content is None:
            # A store read can miss without raising (the file was listed, then removed). Writing
            # `None` through would put the string "None" into the sandbox for the program to
            # parse.
            logger.warning("execute_code: %r is listed but has no content", name)
            return f"Error: {name!r} is listed in the workspace but has no content"
        over_cap = tally.add(name, content)
        if over_cap is not None:
            return over_cap
        read.append((name, content))
    return read


def _over_file_count(count: int, limits: TransferLimits) -> str | None:
    """Refuse a call that would write more files than the workload allows, program included."""
    if count <= limits.max_files:
        return None
    return (
        f"Error: {count} files would be written into the sandbox — your program and "
        f"{count - 1} shared — and this tool writes at most {limits.max_files} per call. "
        f"Nothing was shared."
    )


class _InboundTally:
    """The two byte ceilings of ``files_in``, applied one file at a time.

    Incremental rather than over a finished list, because a cap on a collection already read is
    a cap on nothing this process has left to spend.  Counted **encoded**, since that is what
    crosses, and seeded with the program, which crosses on every call.
    """

    def __init__(self, limits: TransferLimits) -> None:
        self._limits = limits
        self._total = 0

    def add(self, name: str, content: str) -> str | None:
        """Count one file, or answer with the refusal that should stop the next read."""
        size = len(content.encode())
        if size > self._limits.max_bytes_per_file:
            return (
                f"Error: {name!r} is {size} bytes and this tool writes at most "
                f"{self._limits.max_bytes_per_file} bytes per file. Nothing was shared."
            )
        self._total += size
        if self._total > self._limits.max_total_bytes:
            return (
                f"Error: this call would write {self._total} bytes into the sandbox and this "
                f"tool writes at most {self._limits.max_total_bytes} per call. Nothing was "
                f"shared."
            )
        return None


async def _write_shared(sandbox: "Sandbox", name: str, guest_path: str, content: str) -> str | None:
    """Put one already-read workspace file into the run's directory, or answer with the refusal."""
    try:
        await sandbox.write_file(guest_path, content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_code: could not write %r into the sandbox: %s", name, error_detail(exc)
        )
        return f"Error: could not share {name!r} into the sandbox"
    return None


# --- Files out -----------------------------------------------------------------------------


def _normalization(session: SandboxToolSession) -> NameNormalization:
    """What the attached sink does to a name, which decides the spelling to judge."""
    sink = session.output_sink
    return sink.normalization if sink is not None else NameNormalization.NFC


def _validated_output_names(
    names: "Sequence[str]",
    *,
    max_files: int,
    reserved: set[str],
    run_id: str,
    normalization: NameNormalization,
) -> list[str] | str:
    """Settle every output name before the program runs, or answer with the refusal.

    Each rule is applied to the spelling ``collect_outputs`` will judge later — the guest path
    with its run prefix, and the delivered name after normalization — so that a refusal cannot
    arrive a whole run late.  That function stays the authority: if the two disagree, this one
    is wrong, and the cost is the late refusal rather than a name reaching a host.
    """
    if len(names) > max_files:
        return (
            f"Error: {len(names)} output files were declared and this tool saves at most "
            f"{max_files} per call."
        )
    prefix = f"{run_id}/"
    seen: dict[str, str] = {}
    for name in names:
        # NFC is not length-non-increasing — 43 × U+0958 is 129 bytes declared and 258
        # delivered — so the name to hold to the invariant is the one the sink will receive.
        delivered = (
            name if normalization is NameNormalization.NONE else unicodedata.normalize("NFC", name)
        )
        for spelling in (name, delivered, prefix + name):
            try:
                validate_artifact_name(spelling)
            except SandboxArtifactNameInvalid as exc:
                return f"Error: {name!r} cannot be saved — {exc}"
        if name in reserved:
            return f"Error: {name!r} cannot be saved — this tool writes that file itself."
        # `collect_outputs`' own key: NFC and case-folded, always, whatever the sink does
        # about rewriting.
        key = unicodedata.normalize("NFC", name).lower()
        if key in seen:
            earlier = seen[key]
            return (
                f"Error: {name!r} and {earlier!r} are one file once saved"
                if earlier != name
                else f"Error: {name!r} was declared twice."
            )
        seen[key] = name
    return list(names)


async def _collect(
    session: SandboxToolSession,
    sandbox: "Sandbox",
    run_id: str,
    outputs: CodeactOutputs,
    declared: list[str],
    reserved: set[str],
) -> str:
    """Land whatever this run produced, and say what happened — never raising into the model."""
    sink = session.output_sink
    if sink is None:  # unreachable: `sandboxed_tool` refuses this spec without a sink
        return "Error: no output sink is configured, so nothing could be saved."
    spec = session.spec

    if outputs is CodeactOutputs.MANIFEST:
        listed = await _read_manifest(session, sandbox, run_id)
        if isinstance(listed, str):
            return listed
        declared, manifest_bytes = listed
        # The manifest is a file this collection moved, so the budget the artifacts get is
        # what is left after it. Charged as the bytes actually read, not as a `CONSUME`
        # declaration `collect_outputs` would stat a second time: the guest may still be
        # running, and a manifest truncated between the two would return its cost to the
        # budget while its bytes had already crossed.
        spec = replace(
            session.spec,
            files_out=replace(
                session.spec.files_out,
                max_files=session.spec.files_out.max_files - 1,
                max_total_bytes=max(session.spec.files_out.max_total_bytes - manifest_bytes, 0),
            ),
        )
        checked = _validated_output_names(
            declared,
            max_files=spec.files_out.max_files,
            reserved=reserved,
            run_id=run_id,
            normalization=_normalization(session),
        )
        if isinstance(checked, str):
            return checked

    if not declared:
        return f"{_MANIFEST_FILENAME} listed no files, so nothing was saved."

    # `required=False` so one forgotten name does not throw away the files that were
    # written, and no `media_type`, which this kind does not know.
    call_time = tuple(
        DeclaredOutput(path=f"{run_id}/{name}", name=name, required=False) for name in declared
    )
    try:
        landed = await collect_outputs(sandbox, spec, sink=sink, outputs=call_time)
    except SandboxOutputError as exc:
        logger.warning("execute_code: could not save this run's files: %s", error_detail(exc))
        return (
            f"Error: the program ran but its files could not be saved — {exc}. {_MAY_HAVE_LANDED}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("execute_code: saving this run's files failed: %s", error_detail(exc))
        return f"Error: the program ran but its files could not be saved. {_MAY_HAVE_LANDED}"
    return _format_landed(landed, declared)


async def _read_manifest(
    session: SandboxToolSession, sandbox: "Sandbox", run_id: str
) -> tuple[list[str], int] | str:
    """Parse the program's own listing of what it produced, bounded and refusing malformed shapes.

    **Names only.**  A media type read from here would be the guest declaring how the host
    should handle its own bytes, which is worse than the sniffing ``DeclaredOutput.media_type``
    exists to forbid — a sink may route on that value to choose inline rendering. This kind does
    not know what its program wrote, so it says so, and a host that wants to decide by extension
    has :attr:`~maf_sandbox.Artifact.name` and its own policy.

    This read is the kind's own and so is its ceiling: a ``CONSUME`` output declared in the spec
    would be capped by :func:`~maf_sandbox.collect_outputs`, but the manifest has to be read
    *before* there is anything to declare.
    """
    path = f"{run_id}/{_MANIFEST_FILENAME}"
    try:
        entry = await sandbox.stat_file(path, working_directory=session.spec.work_dir)
        if entry is None:
            return f"No {_MANIFEST_FILENAME} was written, so no files were saved."
        # Stat, refuse, *then* read — the pull surface's contract, not an optimisation. A
        # backend whose SDK buffers the whole response has already spent the memory by the
        # time `max_bytes` is looked at, so passing a ceiling down is not a bound on its own.
        # An unknown size fails closed for the same reason it does in `collect_outputs`.
        # The smallest of this kind's own ceiling and both of the host's: `files_out` is
        # what the router matched against the backend, so reading past either would transfer
        # more than the spec declared and make that match untrue for this kind. The total
        # counts because a manifest bigger than the whole collection's budget cannot be part
        # of a collection that fits.
        limits = session.spec.files_out
        ceiling = min(_MANIFEST_MAX_BYTES, limits.max_bytes_per_file, limits.max_total_bytes)
        if entry.size_bytes is None or entry.size_bytes > ceiling:
            return (
                f"Error: {_MANIFEST_FILENAME} is {entry.size_bytes or 'of unknown'} bytes and "
                f"this tool reads at most {ceiling}, so no files were saved."
            )
        raw = await sandbox.read_file(
            path, working_directory=session.spec.work_dir, max_bytes=entry.size_bytes
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("execute_code: could not read %s: %s", _MANIFEST_FILENAME, error_detail(exc))
        return f"Error: {_MANIFEST_FILENAME} could not be read, so no files were saved."

    try:
        document: object = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        # Every way this parse fails with a value: `JSONDecodeError` and `UnicodeDecodeError`
        # are both `ValueError`, and so is the refusal to convert an integer literal longer
        # than `sys.get_int_max_str_digits()` — 4300 digits by default, a rounding error
        # against the size ceiling. Naming only the two subclasses left that third one to
        # escape the tool body.
        return f"Error: {_MANIFEST_FILENAME} is not valid JSON ({exc}), so no files were saved."
    except RecursionError:
        # A few thousand nested arrays fit in far less than the size ceiling, and the parser
        # answers with a `RecursionError` that is neither a decode error nor a JSON one — so
        # without this it leaves the tool body and takes the caller's turn with it.
        return f"Error: {_MANIFEST_FILENAME} is nested too deeply to parse, so no files were saved."
    listed = (
        cast("dict[str, object]", document).get(_MANIFEST_OUTPUTS_KEY)
        if isinstance(document, dict)
        else None
    )
    if not isinstance(listed, list):
        return (
            f"Error: {_MANIFEST_FILENAME} must be an object with an "
            f"{_MANIFEST_OUTPUTS_KEY!r} array, so no files were saved."
        )
    entries: list[str] = []
    for item in cast("list[object]", listed):
        entry = cast("dict[str, object]", item) if isinstance(item, dict) else {}
        path_value = entry.get(_MANIFEST_PATH_KEY)
        if not isinstance(path_value, str):
            return (
                f"Error: every {_MANIFEST_OUTPUTS_KEY!r} entry needs a "
                f"{_MANIFEST_PATH_KEY!r} string, so no files were saved."
            )
        entries.append(path_value)
    return entries, len(raw)


def _format_landed(landed: "Sequence[LandedArtifact]", declared: "Sequence[str]") -> str:
    """What the model is told about the files: the host's own references, and what is absent.

    The two sides are compared in NFC, because a landing name is normalized before the sink
    sees it: a declared ``e`` + combining acute comes back as the precomposed ``é``, and an
    exact-string comparison would report a file that landed perfectly well as never written.
    Normalizing **both** sides is right whichever normalization the sink chose — under
    ``NameNormalization.NONE`` the two are already the same string.
    """
    lines: list[str] = []
    if landed:
        lines.append("Saved:")
        lines.extend(f"- {item.display}" for item in landed)
    delivered = {unicodedata.normalize("NFC", item.name) for item in landed}
    missing = [name for name in declared if unicodedata.normalize("NFC", name) not in delivered]
    if missing:
        lines.append(
            f"Not written by the program, so not saved: {', '.join(sorted(missing))}. Write "
            "each file into the working directory before the program exits."
        )
    return "\n".join(lines)


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
