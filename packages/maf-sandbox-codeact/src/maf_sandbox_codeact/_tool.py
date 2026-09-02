"""``execute_code``: the CodeAct sandbox workload.

The agent gets one tool, the model writes a short Python program, and the program runs inside
a sandbox — computing an answer instead of reasoning about what the computation would produce.

**This module contains no Azure import, no backend import and no sandbox lifecycle code.**  It
talks to a :class:`~maf_sandbox.SandboxRouter` and gets back ``write_file``, ``exec`` and the
pull surface, so the same tool runs unchanged against ACA Sandboxes, a Docker container or an
in-process fake.

Channels the host chooses among, and stdout is always there.  A
**file store** adds a ``files`` parameter, so a program can transform files that already
exist rather than only data the model wrote into its own source.  An **output sink** plus a
:class:`CodeactOutputs` mode adds a way for files the program produces to reach host state.
A **host-tool registry** adds functions the program may call out to, served over
:func:`~maf_sandbox.host_tool_calls_over_exec` on a backend declaring
:data:`~maf_sandbox.Capability.HOST_TOOLS` — the Docker and ACA Sandboxes backends do; the
WSL container backend does not, and against it that wiring is *refused* where the tool would
have been built rather than at the first call.  An **egress allowlist**
opens named hosts to the program; empty by default, so the network stays closed unless a host
opens it.
Wire none of them and this is the stdout-only kind it has always been, with nothing callable
from inside: no network, no host functions, and nothing leaving but what the program printed.
"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from maf_sandbox import (
    DEFAULT_TRANSFER_LIMITS,
    SHIM_MODULE,
    WORK_DIRECTORY,
    CallerContext,
    Capability,
    DeclaredOutput,
    Egress,
    ExecResult,
    HostToolRun,
    NameNormalization,
    OutputSink,
    SandboxArtifactNameInvalid,
    SandboxOutputError,
    SandboxProgramTimeout,
    SandboxRouter,
    SandboxSpec,
    TransferLimits,
    collect_outputs,
    echoed_name,
    error_detail,
    guest_run_layout,
    host_tool_calls_over_exec,
    host_tool_shim,
    validate_artifact_name,
)
from maf_sandbox.maf import (
    SandboxToolSession,
    hidden_content_candidates,
    positions_holding_hidden_content,
    sandboxed_tool,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from agent_framework import AgentFileStore
    from maf_sandbox import HostToolAggregate, HostToolRegistry, LandedArtifact, Sandbox

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

#: Where every call's directory is created — a dedicated root rather than the image's own tree.
_WORK_DIR = "/maf-sandbox/work"

#: One fixed name inside each call's own directory.
_PROGRAM_FILENAME = "program.py"

#: What this kind needs to reach for ``execute_code`` to work at all — nothing, because the
#: program computes and no part of this kind resolves a module or installs a package. An answer
#: rather than an omission; :func:`codeact_sandbox_spec` has why it is fixed here.
_KIND_EGRESS: tuple[str, ...] = ()

#: The arguments a caller names files in, which is what a refusal about one of them points
#: at and what provenance is asked about.
_FILES_ARGUMENT = "files"
_OUTPUTS_ARGUMENT = "outputs"

#: Where a ``MANIFEST``-mode program says what it produced.
_MANIFEST_FILENAME = "outputs.json"
_MANIFEST_OUTPUTS_KEY = "outputs"
_MANIFEST_PATH_KEY = "path"

#: A listing of what a program wrote is text, and a small amount of it.
_MANIFEST_MAX_BYTES = 64 * 1024

#: The shortest manifest that names one file — the floor a host's byte caps must reach before
#: this channel can deliver anything at all.  Equality is usable rather than impossible: the
#: artifact it names may be empty, and a zero-byte regular file is collected like any other.
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

#: Closes every result a withholding host returns. A sentence rather than a silence, because
#: the exit code alone leaves a model nothing to act on; it names the route without promising a
#: reader, which is the host's wiring rather than this kind's to claim.
_WITHHELD_ROUTE = (
    "What the program printed is not read back as text. To surface a value, write it into a "
    "declared output rather than printing it."
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
    host_tools: HostToolRegistry | None = None,
    egress_allow: Sequence[str] = (),
) -> SandboxSpec:
    """The sandbox a CodeAct program needs, in backend-neutral terms.

    No ``min_isolation`` is deliberate: this kind runs only what the model wrote, so the
    host's floor governs.

    ``egress_allow`` is the **deployment's** half of the allowlist, and it is empty by default,
    so a caller that says nothing gets a sandbox with no network — what this kind has always
    been.  The other half is :data:`_KIND_EGRESS`, what the kind needs to function: empty,
    because nothing in ``execute_code`` fetches, and fixed here rather than configurable
    because a deployment able to widen what the *kind itself* requires could undo the
    containment the design rests on.  The spec carries the **union**, because that is what the
    router matches against the backend and what decides whether this tool is declared as
    carrying something out.  The network **posture** is derived from that union: named hosts run
    :data:`~maf_sandbox.Egress.ALLOWLIST`, an empty union runs :data:`~maf_sandbox.Egress.CLOSED`.
    CodeAct never runs :data:`~maf_sandbox.Egress.UNRESTRICTED` — unconfined model-written code
    reaching anything is the exfiltration case the allowlist exists to prevent — so the open
    posture is not expressible here, and the router serves the derived mode only on a backend
    that enforces it.

    Naming a host here is a real widening of a sandbox running model-written code: every
    allowed host is a way out for anything the program can read, including files shared into
    the run and whatever a host tool returned.  It exists because a deployment's endpoints —
    a package index, an internal artifact store — are not knowable to a published kind, not
    because reaching them is cheap.

    An output mode other than :data:`CodeactOutputs.NONE` grows ``requires`` by
    :data:`~maf_sandbox.Capability.FILES_OUT` and sets ``outputs_named_at_call_time``, which is
    what keeps the attached tool honest about landing artifacts it cannot yet name.

    A non-empty ``host_tools`` grows ``requires`` by :data:`~maf_sandbox.Capability.HOST_TOOLS`
    and :data:`~maf_sandbox.Capability.FILES_OUT` together.  The surface carries its own
    ``identities``, which :attr:`~maf_sandbox.SandboxSpec.identities` reads, so a router denying
    one refuses this spec at attach.  Reading a registry **seals** it, so ask for the spec once
    everything is registered.

    Raises:
        ValueError: when an ``egress_allow`` entry is not a single hostname — blank, or holding
            whitespace or a comma.
        TypeError: when ``egress_allow`` is a bare ``str`` rather than a sequence of hostnames.
    """
    return _codeact_spec(
        image,
        image_id,
        outputs=outputs,
        files_in=files_in,
        files_out=files_out,
        surface=_host_tools(host_tools),
        egress_allow=egress_allow,
    )


def make_codeact_tools(
    router: SandboxRouter | None,
    agent_dir: str,
    context: CallerContext,
    *,
    file_store: AgentFileStore | None = None,
    output_sink: OutputSink | None = None,
    outputs: CodeactOutputs = CodeactOutputs.NONE,
    withhold_guest_output: bool = False,
    outbound_max_confidentiality: str | None = None,
    host_tools: HostToolRegistry | None = None,
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: int = 120,
    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    files_out: TransferLimits = _DEFAULT_FILES_OUT,
    egress_allow: Sequence[str] = (),
) -> list[Any]:
    """Return the ``[execute_code]`` tool list, or ``[]`` when no sandbox is available.

    The tool's *signature* follows the channels the host wired: ``files`` appears only with a
    ``file_store``, and ``outputs`` only under :data:`CodeactOutputs.DECLARED`.  A model is
    never shown a parameter this deployment cannot honour.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread, and how to enumerate the file store.
        file_store: The agent's file store. Given one, the tool takes a ``files``
            parameter and shares those files into the sandbox; the caller's listing is the
            authority on which names exist, exactly as it is for the Bicep kind.
        output_sink: Where produced files land. Required by any mode but
            :data:`CodeactOutputs.NONE`, and refused at attach without one.
        outputs: How a program's output files are named. See :class:`CodeactOutputs`.
        withhold_guest_output: Keep what the program printed out of the tool result, and answer
            with sizes and the model's own declared names instead. No guest-authored text
            survives into the result — but the values that replace it were still chosen by a
            program the model wrote, so this changes what the result *holds* and not where it
            came from: the tool declares no ``source_integrity`` either way. Requires
            :data:`CodeactOutputs.DECLARED`: the one mode where content can still reach the
            model and no guest-chosen name reaches the result.

            A size here is the UTF-8 length of the text the stream came back as, not the count
            of bytes the program wrote: ``ExecResult`` states no decoding contract, so a
            backend replacing an undecodable byte changes the number and none of them can be
            un-done (#465). See :func:`_stream_bytes`.

            The rendering follows the transport. Off the host-tool-call transport the result
            names the exit code and a size for ``stdout`` and for ``stderr`` separately. On it,
            the launcher merges the program's stderr into its stdout, so there is one ``output``
            size and ``stderr`` is the host's — its note about the run is surfaced whole under
            ``note:``, since withholding it would report a dropped output as a program that
            printed nothing.

            **What withholding gets you, exactly.** The prose and the shape are this package's,
            and the artifact names are the model's own — but what fills them is the program's
            to choose, and it is a channel rather than a leak-free boundary. The exit status is
            8 bits; each stream's size is a few more, chosen by padding; and **each declared
            output is one further bit**, since the program decides whether to write it and the
            result says of every declared name whether it landed — up to ``files_out.max_files``
            of them. So what stops crossing is guest-authored *text*, not every guest-chosen
            *bit* — a narrow per-call channel rather than the open one a rendered ``stdout`` is,
            and a host that must close it should not attach this workload at all. The sink's
            ``display`` is deliberately *not* rendered here — see :func:`_format_landed`.
        outbound_max_confidentiality: The host's cap for tools that carry something out, in the
            host's own vocabulary. Off by default and written only when something can actually
            leave: an artifact landing in the sink, a host tool that carries something out, or
            a non-empty ``egress_allow``.
        host_tools: What a program may call as a host tool, or ``None`` for no host-tool surface
            at all. A non-empty registry widens the spec (see :func:`codeact_sandbox_spec`), and a
            :data:`~maf_sandbox.Identity.USER` tool in it gates every call on approval. A
            backend that cannot serve the widened spec is refused **here**, so on a backend
            without :data:`~maf_sandbox.Capability.HOST_TOOLS` this raises rather than
            returning a tool — which today is every shipped backend. The
            registry is read — and so **sealed** — only where a sandbox is configured, so a host
            that develops with sandboxing off meets the refusal for a late ``register`` in
            production. An **unstamped** tool counts as one that carries something out: nobody
            answered the sink question, the guest may hand it conversation-derived arguments,
            and every other undeclared leg is read as the worst it could be — so this one is
            too, and the cap above applies.
        image: OCI reference of a sandbox image with a Python interpreter on its path.
        image_id: A backend-native disk-image id, skipping resolution.
        exec_timeout_seconds: Per-program bound. A sandbox that stops answering must not hold
            the caller's turn open.
        files_in: What one call may share into the sandbox. Enforced here, because no backend's
            ``write_file`` knows the workload's caps — a spec that declared a bound nothing
            applied would be worse than one that declared none.
        files_out: The collection's caps. ``max_files`` is what bounds how many artifacts one
            call may declare, so it is a property of the workload rather than of the guest.
        egress_allow: The deployment's half of the network allowlist — hosts a published kind
            cannot know. Empty by default, so the sandbox has no network unless a host opens it;
            see :func:`codeact_sandbox_spec` for how it joins the kind's own half and why that
            half is fixed.

    Raises:
        ValueError: when a sink is supplied with nothing to send down it — an output mode of
            :data:`CodeactOutputs.NONE` — when ``withhold_guest_output`` is paired with any
            output mode but :data:`CodeactOutputs.DECLARED`, or when an ``egress_allow`` entry
            is not a single hostname (blank, or holding whitespace or a comma), where a sandbox
            is configured.
        TypeError: when ``egress_allow`` is a bare ``str`` rather than a sequence of hostnames
            (which would otherwise be read one character at a time), again only where a sandbox
            is configured.
        ~maf_sandbox.SandboxOutputSinkRequired: when an output mode is asked for and no sink
            was given. Raised by ``sandboxed_tool`` rather than here, and a
            :class:`RuntimeError` rather than a :class:`ValueError`, so a caller catching one
            of the two does not catch the other.

    Every one of these waits for the attach gate: a host with no sandbox configured gets
    ``[]``, never an exception.
    """
    configured = router is not None and router.enabled
    host_tool_call: _HostToolCall | None = None
    if configured and host_tools is not None and len(host_tools):
        if not math.isfinite(exec_timeout_seconds) or exec_timeout_seconds <= 0:
            # The shim generator below refuses this too, in its own `call_timeout` vocabulary.
            # Gated on a registry, because with none the number only ever reaches `exec` and
            # this factory has never had an opinion about it.
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: exec_timeout_seconds is {exec_timeout_seconds}, and "
                f"a non-empty host_tools registry makes it the guest's patience as well as the "
                f"run's bound, so it must be a finite positive number of seconds."
            )
        # Generated here, so the module the checks below measure is the one every call writes.
        # Its patience is the run's own bound: give up first and a program is told the host
        # never answered while the host-tool call it asked for goes on to act.
        host_tool_call = _HostToolCall(
            host_tools, host_tool_shim(host_tools.names(), call_timeout=exec_timeout_seconds)
        )
    if configured and files_in.max_files < 1:
        # `program.py` is one inbound file on every call, so a cap below one refuses all of
        # them. Every impossible pairing below is caught here rather than per call: a tool the
        # model can see and can never use successfully is worse than one that never attached.
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: files_in.max_files is {files_in.max_files}, and the "
            f"program itself is one file written into the sandbox on every call, so no call "
            f"could succeed."
        )
    if host_tool_call is not None:
        if files_in.max_files < 2:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: files_in.max_files is {files_in.max_files}, and a "
                f"non-empty host_tools registry puts the guest's host-tool module beside the "
                f"program on every call, so no call could succeed."
            )
        crossing = len(host_tool_call.shim.encode())
        room = min(files_in.max_bytes_per_file, files_in.max_total_bytes)
        if crossing > room:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: the guest's host-tool module is {crossing} bytes and "
                f"crosses beside the program on every call, and this host's files_in allows "
                f"{room}, so no call could succeed."
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
        if room < _SMALLEST_MANIFEST:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: outputs={str(CodeactOutputs.MANIFEST)!r} needs "
                f"at least {_SMALLEST_MANIFEST} bytes of files_out — the smallest "
                f"{_MANIFEST_FILENAME} naming one file — and this host allows {room}."
            )
    if configured and withhold_guest_output and outputs is not CodeactOutputs.DECLARED:
        # Two pairings, two reasons, so two sentences: one could never return anything, and the
        # other would return guest text while claiming it had none.
        if outputs is CodeactOutputs.NONE:
            raise ValueError(
                f"{EXECUTE_CODE_TOOL_NAME}: withhold_guest_output=True with outputs="
                f"{str(CodeactOutputs.NONE)!r} leaves the model no way to read anything back — "
                f"what the program prints is withheld and there is no declared output to write "
                f"instead — so no call could return a result it can use. Pass "
                f"outputs={str(CodeactOutputs.DECLARED)!r}."
            )
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: withhold_guest_output=True with outputs="
            f"{str(CodeactOutputs.MANIFEST)!r} would still carry guest-authored text out: the "
            f"program names its own files in {_MANIFEST_FILENAME}, and a name it chose is "
            f"rendered back into the result. Pass outputs={str(CodeactOutputs.DECLARED)!r}, "
            f"where the model names the files and nothing the program wrote reaches the result."
        )
    if configured and outputs is CodeactOutputs.NONE and output_sink is not None:
        raise ValueError(
            f"{EXECUTE_CODE_TOOL_NAME}: an output sink was supplied with outputs="
            f"{str(CodeactOutputs.NONE)!r}, so nothing would ever be landed in it. Pass an "
            f"outputs mode, or drop the sink."
        )
    # Sealed only on a path that attaches something: an unconfigured host is left as ungrounded
    # as it was, and a registry it goes on to widen has nothing derived from it to contradict.
    surface = _host_tools(host_tools) if configured else None
    # The ternary is the point, not the call: `_effective_egress` validates anyway, so what this
    # adds is that an unconfigured host's malformed allowlist never reaches it.
    spec = _codeact_spec(
        image,
        image_id,
        outputs=outputs,
        files_in=files_in,
        files_out=files_out,
        surface=surface,
        egress_allow=egress_allow if configured else (),
    )
    # A single host-tool call may exercise the user's delegated authority, and which one does
    # is not knowable before the program runs, so one such tool raises the whole surface.
    approval_gated = surface is not None and surface.requires_approval
    # A registry can carry something out with no landing artifact to say so — a tool with a
    # declared sink, or an unstamped one that might have one — the one flow neither
    # `egress_allow` nor an `output_sink` reveals. `also_carries_out` folds that fact, which
    # only the kind can see, into `sandbox_tool_declarations`' single rule, so the condition
    # lives in one place instead of being hand-built here (and a key that rule learns later
    # reaches this tool too).
    registry_carries_out = surface is not None and bool(
        surface.outbound_caps or surface.has_undeclared
    )
    return sandboxed_tool(
        lambda session: _execute_code_tool(
            session,
            file_store,
            outputs,
            exec_timeout_seconds,
            host_tool_call,
            withhold=withhold_guest_output,
        ),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=spec,
        name=EXECUTE_CODE_TOOL_NAME,
        approval_mode="always_require" if approval_gated else "never_require",
        also_carries_out=registry_carries_out,
        # Never declared, withheld or not. What comes back is whatever a model-written
        # `print(...)` chose to emit; withheld it is an exit status, two sizes and a presence
        # bit per declared output, every one of them chosen by a program the model wrote. A
        # declaration replaces the framework's input-label join rather than flooring it, so
        # declaring anything here would tell a host's middleware to disregard the input side.
        source_integrity=None,
        outbound_max_confidentiality=outbound_max_confidentiality,
        output_sink=output_sink,
        logger=logger,
    )


def _effective_egress(extra: Sequence[str]) -> tuple[str, ...]:
    """The union of what this kind needs and what the deployment added, in that order.

    The union is what everything downstream must read — the router matches it against the
    backend, and ``sandbox_tool_declarations`` decides from it whether the tool carries
    something out. Either half alone would understate the sandbox.

    Duplicates are dropped rather than refused, within a list as well as across the two: two
    callers naming the same host is agreement, not a mistake, and a repeated host is a second
    rule that can drift from the first.
    """
    return tuple(dict.fromkeys((*_KIND_EGRESS, *_validated_hosts(extra))))


def _validated_hosts(hosts: Sequence[str]) -> tuple[str, ...]:
    """Refuse an allowlist that does not say what its author meant.

    A bare ``str`` satisfies ``Sequence[str]``, so ``egress_allow="pypi.org"`` type-checks and
    becomes seven single-character hosts — the real endpoint unreachable, with no refusal
    anywhere and a confidentiality cap applied to a flow nobody opened.

    Each entry is one hostname, so an entry that is blank, holds whitespace, or holds a comma is
    refused rather than passed through: no hostname contains any of those, and each is a way for
    a spec, the description the model reads, and a backend's allowlist to end up disagreeing
    silently — a comma-joined ``"a,b"`` becomes one spec entry the wslc proxy expands back into
    two, and a padded ``" a"`` reaches a backend as a rule that matches nothing. Refused, not
    stripped, for the reason the router gives: a value that does not say what its author meant
    is an error, not something to quietly rewrite.
    """
    if isinstance(hosts, str):
        raise TypeError(
            f"egress_allow must be a sequence of hostnames, not a single string: {hosts!r} "
            f"would be read one character at a time"
        )
    # Materialised once, after the str guard: a one-shot iterable would otherwise be spent by
    # the loop and come back empty from the return, silently dropping the whole allowlist.
    hosts = tuple(hosts)
    for entry in hosts:
        if not entry.strip():
            raise ValueError(f"egress_allow entries must be non-empty hostnames, got {entry!r}")
        if any(character.isspace() for character in entry) or "," in entry:
            raise ValueError(
                f"egress_allow entries are one hostname each, with no whitespace or commas: "
                f"got {entry!r}"
            )
    return tuple(hosts)


def _host_tools(host_tools: HostToolRegistry | None) -> HostToolAggregate | None:
    """What a host's registry means for this kind, or ``None`` when nothing is callable.

    Taking the aggregate seals the registry, so it is taken once per factory call and shared by
    the spec, the approval mode and the declarations — and taken for an empty registry too,
    where it costs an all-empty aggregate and turns "registered a tool after the tool was
    built" into a refusal at the host's own ``register`` rather than a surface nothing
    classified.
    """
    if host_tools is None:
        return None
    aggregate = host_tools.aggregate()
    return aggregate if len(host_tools) else None


def _codeact_spec(
    image: str | None,
    image_id: str | None,
    *,
    outputs: CodeactOutputs,
    files_in: TransferLimits,
    files_out: TransferLimits,
    surface: HostToolAggregate | None,
    egress_allow: Sequence[str] = (),
) -> SandboxSpec:
    """:func:`codeact_sandbox_spec`, over a host-tool surface the caller has already derived."""
    collects = outputs is not CodeactOutputs.NONE
    requires = {Capability.EXEC, Capability.FILES_IN}
    if collects:
        requires.add(Capability.FILES_OUT)
    if surface is not None:
        # FILES_OUT for the transport rather than for this kind's outputs: a host-tool call stats
        # and reads its request files and the exit marker back over the pull surface, so even a
        # stdout-only program that can call a host function needs one.
        requires |= {Capability.HOST_TOOLS, Capability.FILES_OUT}
    # CodeAct runs model-written code, so it accepts only two postures and never UNRESTRICTED:
    # CLOSED to compute offline, ALLOWLIST to reach the deployment's named sources. The mode is
    # derived from whether any host was named — an allowlist with hosts is ALLOWLIST, an empty
    # one is CLOSED — so there is no way to express the open posture that would make unconfined
    # model code an exfiltration surface.
    effective_egress = _effective_egress(egress_allow)
    egress = Egress.ALLOWLIST if effective_egress else Egress.CLOSED
    return SandboxSpec(
        kind=CODEACT_KIND,
        image=image,
        image_id=image_id,
        egress=egress,
        egress_allow=effective_egress,
        work_dir=_WORK_DIR,
        requires=frozenset(requires),
        outputs_named_at_call_time=collects,
        files_in=files_in,
        files_out=files_out,
        # The router folds the transport's own traffic into the transfer-limit match off this
        # field, so a backend that cannot serve it is refused at attach rather than overrun.
        host_tools=surface,
    )


# --- The tool's description, assembled from the channels the host wired --------------------
#
# Twelve combinations of `files`, an output mode and whether host tools are wired share one
# body, so the description is built rather than written twelve times. It still reaches the
# model exactly as `__doc__`.

_DESCRIPTION_HEAD = """Run a short Python program inside a sandbox and return what it printed.

        Use this to compute rather than to reason: parse, transform, count, check, simulate —
        anything where running the code beats predicting what it would do.  The program runs
        as ``python3 program.py`` in a sandbox with {network}

        **Only what you print is read back as text.**  There is no REPL echo and the value of
        the last expression is not returned, so end the program with ``print(...)`` of
        everything you need to see.

        Write a complete, self-contained program every time.  Each call gets a fresh working
        directory: nothing you did not pass in to *this* call is in it."""

#: The withholding head. The paragraph above it is not merely untrue in that mode — it
#: instructs the one behaviour the mode exists to redirect, and it is the first thing the model
#: reads, so a withheld tool built on it argues with its own `Returns:` section. This one stays
#: transport-neutral about the shape and says "how large" rather than a count of bytes written:
#: `Returns:` is where one merged size is told from two, and neither is what the program wrote.
#: The claim sits on the streams rather than on `print`, because the withholding does: `logging`
#: defaults to stderr and a traceback goes there too, so a model told about `print` alone reads
#: those as a channel that comes back. And it offers the streams no use at all — naming one,
#: debugging included, licenses the writing this mode exists to redirect.
_DESCRIPTION_HEAD_WITHHELD = """Run a short Python program inside a sandbox and report what it
        did.

        Use this to compute rather than to reason: parse, transform, count, check, simulate —
        anything where running the code beats predicting what it would do.  The program runs
        as ``python3 program.py`` in a sandbox with {network}

        **Nothing your program writes to stdout or stderr comes back — not as a value, not to
        debug with.**  You get the exit code and how large the output was, never what was in it,
        so nothing you write there can show you anything, even when the program fails.  That
        covers every route to those streams: ``print``, writes to ``sys.stdout`` or
        ``sys.stderr``, the ``logging`` and ``warnings`` modules, tracebacks, and whatever a
        subprocess you start prints.  Write everything you need to see — results and diagnostics
        alike — into a declared output instead.

        Write a complete, self-contained program every time.  Each call gets a fresh working
        directory: nothing you did not pass in to *this* call is in it."""

#: The claim this kind can always make on its own: nothing callable means nothing leaves.
_DESCRIPTION_NO_NETWORK = """**no network access**, so it can compute
        but cannot fetch."""

#: The same claim, qualified for a non-empty `host_tool_names` — the sandbox still has no
#: network of its own, but the registered tools are a way out of it that the plain claim above
#: would misstate as absent.
_DESCRIPTION_NO_NETWORK_WITH_HOST_TOOLS = """**no network of its own**: it can compute, and
        reach beyond the sandbox only through the host tools listed below."""

#: Said instead of either claim above once a host opens egress, because both of those state
#: the network is absent and it is not. The hosts are named: a model that cannot tell what it
#: may reach spends calls finding out, and a program can enumerate the allowlist by trying it
#: in any case, so withholding the list costs the honest caller and not the dishonest one.
_DESCRIPTION_ALLOWLISTED = """**network access to {hosts} and nothing else**, and only as far
        as the environment allows — a fetch that fails is an answer, not something to work
        around."""

#: Both ways out at once, named together so neither reads as the only one.
_DESCRIPTION_ALLOWLISTED_WITH_HOST_TOOLS = """**network access to {hosts} and nothing else**,
        as far as the environment allows, and the host tools listed below."""

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

#: The call form is `maf_host_tools.call`, never a per-name wrapper: a registered name need
#: not be a legal Python identifier (`HostToolRegistry.register` takes any string), so the
#: form that always works is the one this names, and the only one it promises.
_DESCRIPTION_HOST_TOOLS = """**To call a host tool, ``import maf_host_tools`` and write
        ``maf_host_tools.call("name", **arguments)``, with every argument passed by keyword.**
        The tools you may call this way are: {names}.  A refusal raises
        ``maf_host_tools.HostToolError``, whose message says why."""

_DESCRIPTION_ARG_CODE = """code: The Python source to run.  The standard library, plus
                whatever the sandbox image ships."""

_DESCRIPTION_ARG_FILES = """files: Store-relative paths to share into the sandbox, or
                omit for none.  Only files in your file store listing can be shared."""

_DESCRIPTION_ARG_OUTPUTS = """outputs: The file names your program will write into its
                working directory, or omit if it writes none."""

_DESCRIPTION_RETURNS = """The program's stdout, its stderr when it wrote any, and its exit
            code when that was not zero."""

#: The same, for a run served over the host-tool-call transport: its launcher merges the program's
#: stderr into its stdout, so the sentence above would point a model at the wrong section.
_DESCRIPTION_RETURNS_HOST_TOOL_CALLED = """The program's output — stdout and stderr together, so a
            traceback comes back under ``stdout`` — and its exit code when that was not zero.
            A ``stderr`` section is the host's note about the run, not something your program
            wrote."""

#: Replaces `_DESCRIPTION_RETURNS` where the host withholds guest output, which makes that
#: sentence untrue. In the description rather than only in the result, because a model told up
#: front writes to a declared output on its first call.
_DESCRIPTION_RETURNS_WITHHELD = """How many bytes of stdout and of stderr came back, and the
            exit code — **never what the program printed, which does not come back.**  Write
            anything you need to see into a declared output instead."""

#: The same for a run served over the host-tool-call transport, which merges the program's
#: stderr into its stdout: naming two streams there would tell a model its stderr write
#: vanished. A `note` line is the host's, never the program's.
_DESCRIPTION_RETURNS_WITHHELD_HOST_TOOL_CALLED = """How many bytes of output came back — stdout
            and stderr together — and the exit code, **never what the program printed, which
            does not come back.**  A ``note`` line is the host's remark about the run.  Write
            anything you need to see into a declared output instead."""

#: Appended to whichever of the two above applies.  Where it wraps is model-facing text, so the
#: break sits where the plain sentence needs it, not where this fragment reads best.
_DESCRIPTION_RETURNS_DEGRADES = """  If the sandbox is unavailable the tool returns an
            error message instead, so the run degrades rather than blocking."""

_DESCRIPTION_RETURNS_SAVED = """  A run that saved files also names where each one landed."""

#: The withholding pair. Three sentences above stop being true in that mode: nothing names
#: *where* a file landed, and a failed program's files are collected rather than discarded —
#: which is the recovery route this mode depends on, so a model told the opposite will not take
#: it after the failure that is exactly when it needs to.
_DESCRIPTION_DECLARED_WITHHELD = """**To produce files, name them in ``outputs`` and write them
        into the working directory.**  They are saved to host storage after the program exits
        and the result confirms each name that landed — not where it landed, and not what is in
        it, so do not claim to have read a file you only produced.  A name you declare and do
        not write is reported to you rather than silently dropped, and a file you write without
        declaring is not saved at all.  **A program that fails still saves what it wrote**, so
        writing what you need into a declared output and then failing still gets it out.

        Naming a file in both ``files`` and ``outputs`` is how you edit one in place.  It is the
        one case where "declared and not written" cannot be reported, because the copy you were
        given is already there — and since a failed run still saves, a program that dies part
        way through rewriting one saves whatever it had written by then."""

_DESCRIPTION_RETURNS_SAVED_WITHHELD = """  A run that saved files also names each one."""


def _tool_description(
    *,
    takes_files: bool,
    outputs: CodeactOutputs,
    host_tool_names: frozenset[str] = frozenset(),
    egress_allow: Sequence[str] = (),
    withhold: bool,
) -> str:
    """The description the model reads, for the channels this host actually wired.

    ``egress_allow`` is the spec's effective list, not the deployment's half: the model is told
    what the sandbox can reach, and where that came from is not its concern.
    """
    if egress_allow:
        hosts = ", ".join(f"``{host}``" for host in egress_allow)
        network = (
            _DESCRIPTION_ALLOWLISTED_WITH_HOST_TOOLS
            if host_tool_names
            else _DESCRIPTION_ALLOWLISTED
        ).format(hosts=hosts)
    else:
        network = (
            _DESCRIPTION_NO_NETWORK_WITH_HOST_TOOLS if host_tool_names else _DESCRIPTION_NO_NETWORK
        )
    head = _DESCRIPTION_HEAD_WITHHELD if withhold else _DESCRIPTION_HEAD
    body = [head.format(network=network)]
    if host_tool_names:
        names = ", ".join(f"``{name}``" for name in sorted(host_tool_names))
        body.append(_DESCRIPTION_HOST_TOOLS.format(names=names))
    arguments = [_DESCRIPTION_ARG_CODE]
    if takes_files:
        body.append(_DESCRIPTION_FILES)
        arguments.append(_DESCRIPTION_ARG_FILES)
    if outputs is CodeactOutputs.DECLARED:
        body.append(_DESCRIPTION_DECLARED_WITHHELD if withhold else _DESCRIPTION_DECLARED)
        arguments.append(_DESCRIPTION_ARG_OUTPUTS)
    elif outputs is CodeactOutputs.MANIFEST:
        body.append(_DESCRIPTION_MANIFEST)
    if withhold:
        returns = (
            _DESCRIPTION_RETURNS_WITHHELD_HOST_TOOL_CALLED
            if host_tool_names
            else _DESCRIPTION_RETURNS_WITHHELD
        )
    else:
        returns = _DESCRIPTION_RETURNS_HOST_TOOL_CALLED if host_tool_names else _DESCRIPTION_RETURNS
    returns += _DESCRIPTION_RETURNS_DEGRADES
    if outputs is not CodeactOutputs.NONE:
        returns += _DESCRIPTION_RETURNS_SAVED_WITHHELD if withhold else _DESCRIPTION_RETURNS_SAVED
    return (
        "\n\n        ".join(body)
        + "\n\n        Args:\n            "
        + "\n            ".join(arguments)
        + "\n\n        Returns:\n            "
        + returns
        + "\n        "
    )


@dataclass(frozen=True)
class _HostToolCall:
    """What one attached tool serves host-tool calls with: the registry, and the guest module.

    Generated once and written into every run, which the registry's sealing is what makes
    honest: the names it spells cannot change after the factory has read them.
    """

    registry: HostToolRegistry
    shim: str


def _execute_code_tool(
    session: SandboxToolSession,
    store: AgentFileStore | None,
    outputs: CodeactOutputs,
    timeout: int,
    host_tool_call: _HostToolCall | None,
    *,
    withhold: bool,
) -> Callable[..., Awaitable[str]]:
    """Build the ``execute_code`` body for one attached tool.

    Four signatures over one implementation, because MAF derives the tool's schema from the
    function's parameters: a host that wired no file store must not be shown ``files``.
    """

    async def run(code: str, files: list[str] | None, declared: list[str] | None) -> str:
        return await _execute(
            session,
            store,
            outputs,
            timeout,
            host_tool_call,
            code,
            files or [],
            declared or [],
            withhold=withhold,
        )

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
    body.__doc__ = _tool_description(
        takes_files=takes_files,
        outputs=outputs,
        host_tool_names=(
            host_tool_call.registry.names() if host_tool_call is not None else frozenset()
        ),
        # Off the attached spec, not off a parameter threaded down here: the spec carries the
        # union the router matched, so what the model is told cannot drift from what the
        # sandbox actually got.
        egress_allow=session.spec.egress_allow,
        withhold=withhold,
    )
    return body


async def _execute(
    session: SandboxToolSession,
    store: AgentFileStore | None,
    outputs: CodeactOutputs,
    timeout: int,
    host_tool_call: _HostToolCall | None,
    code: str,
    files: list[str],
    declared: list[str],
    *,
    withhold: bool,
) -> str:
    """One ``execute_code`` call: share, run, and collect."""
    # Taken before anything here awaits, and carried to every name check in this call: the
    # framework's accessor is not scoped to the call, so a lookup made after the run — the
    # manifest's, above all — may find nothing left to answer with.
    rewritten = hidden_content_candidates()
    # Scope and thread come from the host's request context, never from model input.
    key = session.key()
    if isinstance(key, str):
        return key

    # The names this run spends on something other than the model's own files, so neither an
    # input nor an output may claim one. The manifest is reserved only where it means
    # something, and the program only where it shares that directory: a run that calls a host tool
    # puts it in the transport's, beside the shim, where no name a model chooses can reach it.
    #
    # Each carries the clause its refusal uses, because the two are reserved for opposite
    # reasons — this tool writes the program and only reads the manifest, which the program
    # writes. One sentence for both would be false about one of them.
    reserved: dict[str, str] = {}
    if host_tool_call is None:
        reserved[_PROGRAM_FILENAME] = (
            "this tool writes a file of that name into every run's directory"
        )
    if outputs is CodeactOutputs.MANIFEST:
        reserved[_MANIFEST_FILENAME] = (
            "this tool reads a file of that name from every run's directory as its manifest"
        )

    # Chosen here rather than after `acquire`, so that a declared name can be judged against
    # the guest path it will actually become — the prefix is the id core allocates for the
    # call, derived below rather than counted.
    call_directory = session.guest_call_path()
    call_id = call_directory.rsplit("/", 1)[-1]
    # Where the model's own files live, relative to `work_dir`: the call directory itself, or
    # the work subdirectory of it when the transport owns the run. Everything addressed by a
    # name a model chose is built from this — what is shared in, what the manifest is read
    # from, and what is collected out — so the three cannot disagree about one call's layout.
    # It is longer when calling a host tool, which is why the name checks below take it rather than
    # `call_id`: five more bytes of the 255 a guest path gets, spent before the name is.
    guest_prefix = f"{call_id}/{WORK_DIRECTORY}" if host_tool_call is not None else call_id

    names: list[str] = []
    if outputs is CodeactOutputs.DECLARED:
        checked = _validated_output_names(
            declared,
            max_files=session.spec.files_out.max_files,
            reserved=reserved,
            guest_prefix=guest_prefix,
            normalization=_normalization(session),
            named_by=_OUTPUTS_ARGUMENT,
            argument=_OUTPUTS_ARGUMENT,
            candidates=rewritten,
        )
        if isinstance(checked, str):
            return checked
        names = checked

    # Cap before acquiring anything, and cap *as we go*: a bound that answers only once
    # everything is in memory has already spent what it exists to bound. Every check below
    # therefore happens before the read it would have prevented — the count before the listing,
    # the program's own bytes before the store is touched at all, and each file's as it arrives.
    # The tally covers what this kind writes before exec: `program.py`, which is why the spec
    # requires FILES_IN even with no store, the shim beside it wherever a registry is wired,
    # and each shared file. Not the transport's own — a fixed launcher, and one response per
    # host-tool call under the registry's `response_limits`.
    limits = session.spec.files_in
    tally = _InboundTally(limits)
    shared: list[tuple[str, str]] = []
    inbound = len(files) + (2 if host_tool_call is not None else 1)
    over_cap = _over_file_count(
        inbound, limits, calls_host_tool=host_tool_call is not None
    ) or tally.add(_PROGRAM_FILENAME, code)
    if over_cap is None and host_tool_call is not None:
        over_cap = tally.add(SHIM_MODULE, host_tool_call.shim)
    if over_cap is not None:
        return over_cap
    if store is not None:
        resolved = await _resolve_listed_files(
            session, store, files, reserved=reserved, withhold=withhold, candidates=rewritten
        )
        if isinstance(resolved, str):
            return resolved
        read = await _read_listed_files(store, resolved, tally)
        if isinstance(read, str):
            return read
        shared = read

    sandbox = await session.acquire(key)
    if isinstance(sandbox, str):
        return sandbox

    # The session owns this path, and `sandboxed_tool` reclaims it when the call returns.
    # Built before anything is written, because it decides where everything goes. A call that
    # calls a host tool is two directories — the model's files in `work`, the program and the shim
    # in the transport's — and one that does not is the call directory flat, which is what a kind
    # writing no shim has always been. The program name and the interpreter are passed rather
    # than defaulted, so this kind's constants and the transport's cannot drift apart.
    layout = (
        guest_run_layout(call_directory, program=_PROGRAM_FILENAME)
        if host_tool_call is not None
        else None
    )
    shared_dir = layout.work if layout is not None else call_directory

    for name, content in shared:
        refusal = await _write_shared(
            sandbox, name, f"{shared_dir}/{name}", content, working_directory=shared_dir
        )
        if refusal is not None:
            return refusal

    program_path = layout.program if layout is not None else f"{call_directory}/{_PROGRAM_FILENAME}"
    try:
        await sandbox.write_file(
            program_path,
            code,
            working_directory=layout.directory if layout is not None else call_directory,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_code: could not write the program into the sandbox: %s", error_detail(exc)
        )
        return "Error: could not write the program into the sandbox"

    try:
        # The two are built together above and are never one without the other; both are named
        # here so neither has to be narrowed from the other.
        if host_tool_call is not None and layout is not None:
            await sandbox.write_file(
                layout.shim, host_tool_call.shim, working_directory=layout.directory
            )
            # A fresh run per call: the host-tool-call cap and the ledger bound one program.
            result = await host_tool_calls_over_exec(
                sandbox,
                HostToolRun(host_tool_call.registry, logger=logger),
                layout,
                timeout=timeout,
                interpreter=_INTERPRETER,
            )
        else:
            # An argv sequence, never a command line: the model's source never reaches a shell.
            result = await sandbox.exec(
                [_INTERPRETER, program_path], working_directory=call_directory, timeout=timeout
            )
    except SandboxProgramTimeout as expired:
        # The transport's own bound — but *which* of its bounds is something only its message
        # knows. The run can expire before the program is started at all, and that message
        # says so where a reconstructed "the program timed out" would blame code that never
        # ran. The message also carries the output clause, which is the program's stdout or
        # the host's reason for having none; `output` is empty in the second case, so
        # rebuilding the sentence from that attribute drops the reason silently.
        #
        # It says what was *attempted* on the program too — its process group signalled, the
        # program signalled alone, or nothing signalled. None of the three is a claim that the
        # program stopped: the signal reaches the group it starts in, and both numbers come
        # from files the program can write. A blanket claim added here would contradict a
        # sentence that is already careful about which of those it is asserting.
        #
        # Surfaced whole rather than quoted from: the transport writes these model-safe, with
        # a backend's own text kept to the log, which is the same rule this kind follows.
        logger.warning("execute_code: %s", expired)
        if withhold:
            # The output clause is in the message rather than fenced off in `output`, so the
            # sentence is rebuilt from the attributes instead — and it names no bound, because
            # *whose* expired is not knowable here. A backend may raise this public type from a
            # call of its own, the transport propagates that untranslated, and the subtype that
            # tells the two apart is core's private one. `signal` is the discriminator the
            # exception does carry, and `"absent"` is its one value asserting nothing started.
            if expired.signal == "absent":
                return f"Error: the time ran out before the program was started. {_WITHHELD_ROUTE}"
            return f"Error: the program did not finish in the time it was given. {_WITHHELD_ROUTE}"
        return f"Error: {expired}"
    except TimeoutError as unfinished:
        if host_tool_call is None:
            # One `exec`, one bound: a timeout here is that bound and nothing else, so unlike
            # the branch above this one may name it. The route still belongs on the end —
            # every shipped backend reaches this line rather than that one.
            logger.warning("execute_code: the program timed out after %ss", timeout)
            expiry = f"Error: the program timed out after {timeout}s"
            return f"{expiry}. {_WITHHELD_ROUTE}" if withhold else expiry
        # A backend bounding one of its own control-plane calls, which the transport re-raises
        # untranslated. Blaming the program would be a guess about code the model is about to
        # rewrite — and the wrong one, since the run may have had most of its time left.
        logger.warning("execute_code: a transport call timed out: %s", error_detail(unfinished))
        return "Error: could not run the program in the sandbox"
    except Exception as exc:  # noqa: BLE001
        # Provider/transport detail can carry account ids — must not reach the transcript.
        logger.warning("execute_code: exec failed: %s", error_detail(exc))
        return "Error: could not run the program in the sandbox"

    logger.info("execute_code: ran exit_code=%d shared=%d", result.exit_code, len(shared))
    report = (
        _format_withheld(result, over_transport=host_tool_call is not None)
        if withhold
        else _format_result(result)
    )
    nothing_to_collect = outputs is CodeactOutputs.NONE or (
        outputs is CodeactOutputs.DECLARED and not names
    )
    if nothing_to_collect or (result.exit_code != 0 and not withhold):
        # A program that failed is unlikely to have written what it promised, and a missing-file
        # report stacked on a traceback buries the thing the model has to fix. Withheld there is
        # no traceback to bury, and the declared output is the only channel left — including for
        # a program that caught its own error and wrote the diagnosis into one.
        return report
    collected = await _collect(
        session,
        sandbox,
        guest_prefix,
        outputs,
        names,
        reserved,
        withhold=withhold,
        candidates=rewritten,
    )
    return f"{report}\n\n{collected}" if collected else report


# --- Files in ------------------------------------------------------------------------------


async def _resolve_listed_files(
    session: SandboxToolSession,
    store: AgentFileStore,
    files: list[str],
    *,
    reserved: Mapping[str, str],
    withhold: bool = False,
    candidates: frozenset[str] | None = None,
) -> list[str] | str:
    """Match each requested name against the caller's listing, or answer with the refusal.

    The listing is the injection-pinning boundary: a name the model invented, or read out of a
    poisoned file, has nowhere to go.  Which is why a listing that cannot be read is a refusal
    rather than an empty one — every name would then be refused for the wrong reason.
    """
    if not files:
        return []
    # Asked before the first await, not beside the loop that uses it: the framework's accessor
    # is not scoped to the call, so every suspension before asking is a chance for the answer
    # to come back empty. See `positions_holding_hidden_content`.
    rewritten = positions_holding_hidden_content(
        files, argument=_FILES_ARGUMENT, candidates=candidates
    )
    listing = await session.list_files(store)
    if isinstance(listing, str):
        # The host's own sentence about its store. Withheld it is dropped for the reason the
        # names below are: `list_files` is a host callback with no integrity contract.
        if withhold:
            logger.warning("execute_code: the file listing could not be read: %s", listing)
            return "Error: this tool's file listing could not be read, so nothing was shared."
        return listing
    known = set(listing)
    resolved: list[str] = []
    for position, name in enumerate(files):
        at = f"files[{position}]"
        hidden = position in rewritten
        named = echoed_name(name, at=at, hidden=hidden)
        try:
            validate_artifact_name(name, at=at, hidden=hidden)
        except SandboxArtifactNameInvalid as exc:
            # The validator's own sentence, which names the rule that was broken: a fixed
            # message listing two of its rules tells a caller refused for a backslash or a
            # control character that its name satisfies everything the tool asked for. The
            # listing is still not echoed — that would invite a retry with another spelling.
            return f"Error: {named} cannot be shared — {exc}"
        if name in reserved:
            return f"Error: {named} cannot be shared — {reserved[name]}."
        refusal = _inside_a_reserved_file(name, reserved, action="shared", at=at, hidden=hidden)
        if refusal is not None:
            return refusal
        if name in resolved:
            # One read and one write per name. Repeating one buys the caller nothing and
            # multiplies both, which is the cheapest way to amplify against the byte ceilings.
            return f"Error: {named} was listed twice."
        if name not in known:
            logger.warning(
                "execute_code: %r is not in this tool's file store listing (%d file(s) visible) "
                "— the store wired here may be narrower than the agent's",
                name,
                len(listing),
            )
            return (
                f"Error: {named} is not in this tool's file listing, so it was not shared. "
                f"{_listing_hint(name, listing, withhold=withhold)}"
            )
        resolved.append(name)
    return resolved


#: Capped so a large file store cannot flood the model's context.
_LISTING_HINT_MAX = 20


def _listing_hint(name: str, listing: list[str], *, withhold: bool = False) -> str:
    """The listing, or its near misses — what resolves a typo without another round trip.

    ``withhold`` names none of them. A store's filenames are the host's to supply through
    ``list_files`` and carry no integrity contract of their own — an agent that saved something
    it fetched may have named it from that content — so echoing up to
    :data:`_LISTING_HINT_MAX` of them would put unclassified text into a result whose whole
    point is to hold none, for a name the model never asked about.
    """
    if not listing:
        return "This tool's listing is empty — no files were shared with it."
    if withhold:
        return f"This tool can see {len(listing)} file(s); their names are not repeated here."
    near = [known for known in listing if known.rsplit("/", 1)[-1] == name.rsplit("/", 1)[-1]]
    if near and near != [name]:
        return f"Did you mean: {', '.join(sorted(near)[:_LISTING_HINT_MAX])}?"
    shown = sorted(listing)[:_LISTING_HINT_MAX]
    more = f" (+{len(listing) - len(shown)} more)" if len(listing) > len(shown) else ""
    return f"Files visible here: {', '.join(shown)}{more}."


async def _read_listed_files(
    store: AgentFileStore, names: list[str], tally: _InboundTally
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
            logger.warning("execute_code: could not read %r from the file store: %s", name, exc)
            return f"Error: could not read {name!r} from the file store"
        if content is None:
            # A store read can miss without raising (the file was listed, then removed). Writing
            # `None` through would put the string "None" into the sandbox for the program to
            # parse.
            logger.warning("execute_code: %r is listed but has no content", name)
            return f"Error: {name!r} is listed in the file store but has no content"
        over_cap = tally.add(name, content)
        if over_cap is not None:
            return over_cap
        read.append((name, content))
    return read


def _inside_a_reserved_file(
    name: str, reserved: Mapping[str, str], *, action: str, at: str, hidden: bool
) -> str | None:
    """Refuse a name that would have to live inside a reserved file, naming which one.

    Every shipped backend creates parent directories for a nested write, so
    ``program.py/data.csv`` would turn ``program.py`` into a directory and fail the write of the
    program that follows.

    **Call it after the name validator.**  ``program.py/../x`` starts with the prefix and climbs
    straight back out, so this sentence would be false for it.
    """
    above = next((owned for owned in reserved if name.startswith(f"{owned}/")), None)
    if above is None:
        return None
    return (
        f"Error: {echoed_name(name, at=at, hidden=hidden)} cannot be {action} — {above!r} is a "
        f"file name this tool reserves in every run's directory, so nothing can live inside it."
    )


def _over_file_count(count: int, limits: TransferLimits, *, calls_host_tool: bool) -> str | None:
    """Refuse a call that would write more files than the workload allows, program included."""
    if count <= limits.max_files:
        return None
    # "of those" only where the list is partial: the host-tool-call leg's launcher crosses too and
    # is not counted here, so the cap is over the enumeration rather than over everything written.
    written, cap = (
        (
            f"your program, the host-tool module beside it, and {count - 2} shared",
            f"{limits.max_files} of those",
        )
        if calls_host_tool
        else (f"your program and {count - 1} shared", str(limits.max_files))
    )
    return (
        f"Error: {count} files would be written into the sandbox — {written} — and this tool "
        f"writes at most {cap} per call. Nothing was shared."
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
        try:
            size = len(content.encode())
        except UnicodeEncodeError:
            # A lone surrogate survives JSON and reaches `code` as a `str` that cannot be
            # encoded. This tally runs outside the guarded write, so without this the turn
            # dies here rather than the model being told what to fix.
            return (
                f"Error: {name!r} is not valid UTF-8 and cannot be written into the sandbox. "
                f"Nothing was shared."
            )
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


async def _write_shared(
    sandbox: Sandbox,
    name: str,
    guest_path: str,
    content: str,
    *,
    working_directory: str,
) -> str | None:
    """Put one already-read file store file into the run's directory, or answer with the refusal."""
    try:
        await sandbox.write_file(guest_path, content, working_directory=working_directory)
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
    names: Sequence[str],
    *,
    max_files: int,
    reserved: Mapping[str, str],
    guest_prefix: str,
    normalization: NameNormalization,
    named_by: str,
    argument: str | None = None,
    candidates: frozenset[str] | None = None,
) -> list[str] | str:
    """Settle every output name before the program runs, or answer with the refusal.

    Each rule is applied to the spelling ``collect_outputs`` will judge later — the guest path
    with its run prefix, and the delivered name after normalization — so that a refusal cannot
    arrive a whole run late.  That function stays the authority: if the two disagree, this one
    is wrong, and the cost is the late refusal rather than a name reaching a host.

    ``named_by`` says where the names came from — the ``outputs`` argument or the manifest —
    so a refusal can point at the one it means without quoting a value it should not.
    """
    if len(names) > max_files:
        return (
            f"Error: {len(names)} output files were declared and this tool saves at most "
            f"{max_files} per call."
        )
    prefix = f"{guest_prefix}/"
    seen: dict[str, tuple[str, str]] = {}
    # Asked of manifest names as well as of the model's own `outputs`, and that is not
    # belt-and-braces: `code` is a rewritten argument too, so a payload can reach the guest
    # in the program's own source and come back as a name the program chose to write.
    rewritten = positions_holding_hidden_content(names, argument=argument, candidates=candidates)
    for position, name in enumerate(names):
        at = f"{named_by}[{position}]"
        hidden = position in rewritten
        named = echoed_name(name, at=at, hidden=hidden)
        # NFC is not length-non-increasing — 43 × U+0958 is 129 bytes declared and 258
        # delivered — so the name to hold to the invariant is the one the sink will receive.
        delivered = (
            name if normalization is NameNormalization.NONE else unicodedata.normalize("NFC", name)
        )
        for spelling in (name, delivered, prefix + name):
            try:
                validate_artifact_name(spelling, at=at, hidden=hidden)
            except SandboxArtifactNameInvalid as exc:
                return f"Error: {named} cannot be saved — {exc}"
        if name in reserved:
            return f"Error: {named} cannot be saved — {reserved[name]}."
        refusal = _inside_a_reserved_file(name, reserved, action="saved", at=at, hidden=hidden)
        if refusal is not None:
            return refusal
        # `collect_outputs`' own key: NFC and case-folded, always, whatever the sink does
        # about rewriting.
        key = unicodedata.normalize("NFC", name).lower()
        if key in seen:
            earlier, earlier_named = seen[key]
            return (
                f"Error: {named} and {earlier_named} are one file once saved"
                if earlier != name
                else f"Error: {named} was declared twice."
            )
        seen[key] = (name, named)
    return list(names)


async def _collect(
    session: SandboxToolSession,
    sandbox: Sandbox,
    guest_prefix: str,
    outputs: CodeactOutputs,
    declared: list[str],
    reserved: Mapping[str, str],
    *,
    withhold: bool = False,
    candidates: frozenset[str] | None = None,
) -> str:
    """Land whatever this run produced, and say what happened — never raising into the model."""
    sink = session.output_sink
    if sink is None:  # unreachable: `sandboxed_tool` refuses this spec without a sink
        return "Error: no output sink is configured, so nothing could be saved."
    spec = session.spec

    if outputs is CodeactOutputs.MANIFEST:
        listed = await _read_manifest(session, sandbox, guest_prefix)
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
            guest_prefix=guest_prefix,
            normalization=_normalization(session),
            named_by=_MANIFEST_FILENAME,
            candidates=candidates,
        )
        if isinstance(checked, str):
            return checked

    if not declared:
        return f"{_MANIFEST_FILENAME} listed no files, so nothing was saved."

    # `required=False` so one forgotten name does not throw away the files that were
    # written, and no `media_type`, which this kind does not know.
    call_time = tuple(
        DeclaredOutput(path=f"{guest_prefix}/{name}", name=name, required=False)
        for name in declared
    )
    try:
        landed = await collect_outputs(sandbox, spec, sink=sink, outputs=call_time)
    except SandboxOutputError as exc:
        logger.warning("execute_code: could not save this run's files: %s", error_detail(exc))
        if withhold:
            # A sink refuses by raising, and it composes that sentence having been handed the
            # artifact's own bytes — nothing constrains it to leave them out. Dropped here for
            # the reason the branch below drops every message it catches.
            return f"Error: the program ran but its files could not be saved. {_MAY_HAVE_LANDED}"
        return (
            f"Error: the program ran but its files could not be saved — {exc}. {_MAY_HAVE_LANDED}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("execute_code: saving this run's files failed: %s", error_detail(exc))
        return f"Error: the program ran but its files could not be saved. {_MAY_HAVE_LANDED}"
    return _format_landed(
        landed,
        declared,
        withhold=withhold,
        named_by=_MANIFEST_FILENAME if outputs is CodeactOutputs.MANIFEST else _OUTPUTS_ARGUMENT,
        argument=None if outputs is CodeactOutputs.MANIFEST else _OUTPUTS_ARGUMENT,
        candidates=candidates,
    )


async def _read_manifest(
    session: SandboxToolSession, sandbox: Sandbox, guest_prefix: str
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
    path = f"{guest_prefix}/{_MANIFEST_FILENAME}"
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


def _format_landed(
    landed: Sequence[LandedArtifact],
    declared: Sequence[str],
    *,
    withhold: bool = False,
    named_by: str = _OUTPUTS_ARGUMENT,
    argument: str | None = None,
    candidates: frozenset[str] | None = None,
) -> str:
    """What the model is told about the files: what landed, and what is absent.

    The two sides are compared in NFC, because a landing name is normalized before the sink
    sees it: a declared ``e`` + combining acute comes back as the precomposed ``é``, and an
    exact-string comparison would report a file that landed perfectly well as never written.
    Normalizing **both** sides is right whichever normalization the sink chose — under
    ``NameNormalization.NONE`` the two are already the same string.

    ``argument`` names the parameter ``declared`` came from, where it came from one — the
    ``outputs`` argument, never the manifest, which no caller spelled.  It is what makes the
    provenance answer exact here rather than inferred, and it matters as much after the run as
    before it: without it a declared name equal to hidden content renders as a position, and a
    caller watching which way its own spelling comes back learns that the guess was right.

    ``withhold`` drops ``display`` in favour of the name the model itself declared. The sink
    composes ``display`` from an :class:`~maf_sandbox.Artifact` whose ``content`` is the guest's
    bytes, and nothing in the protocol requires the two to be independent — so a sink that puts
    any of that content in the string would be putting guest-authored text back into a result
    rendered to hold none. Naming the declared spelling costs the sink's own detail and needs no
    promise from the host to stay honest.
    """
    delivered = {unicodedata.normalize("NFC", item.name) for item in landed}
    lines: list[str] = []
    # Answered once, above the two renderings that need it: a name that landed is reported as
    # surely as one that did not, and a name the framework substituted must not be repeated
    # either way. `withhold` makes no difference to that — it is the mode that renders least.
    rewritten = positions_holding_hidden_content(
        list(declared), argument=argument, candidates=candidates
    )
    position_of = {
        unicodedata.normalize("NFC", name): position for position, name in enumerate(declared)
    }

    if landed:
        lines.append("Saved:")
        if withhold:
            for position, name in enumerate(declared):
                if unicodedata.normalize("NFC", name) in delivered:
                    # The name itself unless the framework put it there: this is a list of
                    # files, not a refusal, so a name of the model's own is what it wants back.
                    lines.append(
                        f"- {named_by}[{position}]" if position in rewritten else f"- {name}"
                    )
        else:
            for item in landed:
                # The display carries the name inside it, so a substituted one is reported by
                # position instead. A name of the model's own keeps the size and the rest of it.
                position = position_of.get(unicodedata.normalize("NFC", item.name))
                lines.append(
                    f"- {named_by}[{position}]"
                    if position is not None and position in rewritten
                    else f"- {item.display}"
                )
    # A name that produced no file is reported the way a refusal reports one: the caller's
    # spelling where it is the caller's, and the position where the framework put something
    # else there. It is the one line here that names a file which does not exist.
    missing = [
        echoed_name(name, at=f"{named_by}[{position}]", hidden=position in rewritten)
        for position, name in enumerate(declared)
        if unicodedata.normalize("NFC", name) not in delivered
    ]
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


def _stream_bytes(text: str | None) -> int:
    """The UTF-8 size of the text a stream came back as — **not** the bytes the program wrote.

    The two differ by a backend-dependent amount, since ``ExecResult`` states no decoding
    contract, and nothing here can recover the original count.

    ``surrogatepass`` because a lone surrogate arrives as a ``str`` a plain encode refuses, and
    this runs outside every guarded block, where a raise would end the caller's turn.
    """
    return len((text or "").encode("utf-8", errors="surrogatepass"))


def _format_withheld(result: ExecResult, *, over_transport: bool) -> str:
    """Render one run for a host that withholds guest text: sizes, not content.

    Fixed shape and fixed order, empty streams included, and the exit code named on every run
    unlike :func:`_format_result`'s — with the content gone it is the only thing left that says
    whether the program worked.

    ``over_transport`` decides who owns ``stderr``. On the host-tool-call transport the
    launcher merges the guest's stderr into its output file, so that field is the *host's* and
    holds its note about the run — the one that tells a dropped output apart from a program
    that printed nothing. Withholding it would report the first as the second, so it is
    surfaced whole there and only the merged stream is reduced to a size.
    """
    if over_transport:
        note = (result.stderr or "").rstrip("\n")
        lines = [f"exit code: {result.exit_code}", f"output: {_stream_bytes(result.stdout)} bytes"]
        if note:
            lines.append(f"note: {note}")
        lines.append(_WITHHELD_ROUTE)
        return "\n".join(lines)
    return "\n".join(
        (
            f"exit code: {result.exit_code}",
            f"stdout: {_stream_bytes(result.stdout)} bytes",
            f"stderr: {_stream_bytes(result.stderr)} bytes",
            _WITHHELD_ROUTE,
        )
    )
