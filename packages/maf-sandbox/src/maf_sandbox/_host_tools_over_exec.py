"""A host-tool transport an ``EXEC`` backend can implement honestly — request and response files.

:mod:`maf_sandbox._host_tools` says what may be dispatched and on whose authority.  It does not
say how a dispatch *reaches* the host, and the shipped backends have no channel for one: their
guests speak an exit code, stdout, and a stat-and-read pull surface.  This is that channel,
built from those primitives and nothing else.

The shape, in one paragraph.  A kind writes the guest program and the generated shim beside
it, into a **fresh per-run directory**; :func:`dispatch_over_exec` writes the launcher, starts
it detached, and then supervises — polling for the next request file, resolving it through
:class:`~maf_sandbox.HostToolRegistry`, and writing the answer back — until the program leaves
its exit marker or the deadline passes.

**The shim is not a control.** It runs in the guest, where model-written code can read, edit or
ignore it: a program that writes request files itself is served identically, and that is by
design rather than an oversight. Every gate that matters — resolution, the declaration check,
argument validation, the dispatch cap, the response ceiling — is host-side in
:meth:`~maf_sandbox.HostToolRun.dispatch`, which this module calls and never reimplements.

Three costs, named here rather than discovered later:

- **Three backend calls per dispatch, at minimum, plus polling.** Serving one request is a
  ``stat_file``, then a ``read_file``, then a ``write_file`` for the answer — and the polling
  in between adds a ``stat_file`` per interval for the exit marker and another for the next
  request. On a remote backend every one of those is an HTTP round trip, so a call-heavy
  program can cost far more of them than the direct tool-calling this replaces. Whether that
  is worth it is a measurement, not an assumption (#133).
- **One outstanding call at a time.** The supervisor polls for the *next* request by name
  rather than enumerating a directory, because the backend that most needs this transport
  (`maf-sandbox-docker`) serves ``FILES_OUT`` and not ``FILES_LIST``. A guest that fires two
  calls concurrently has the second answered only after the first. Concurrent means threads
  *and* processes: the shim claims each number with an exclusive create, so a program that
  forks or spawns cannot have two workers writing one request path.
- **The files outlive the call.** Nothing in the protocol deletes, so requests and responses
  sit in the guest filesystem until the sandbox is disposed of. A fresh per-run directory is
  what keeps one run's traffic out of the next one's; it is the caller's to provide, and
  :func:`guest_run_layout` names the paths inside it.
"""

from __future__ import annotations

import asyncio
import json
import keyword
import logging
import math
import posixpath
import symtable
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ._error_detail import error_detail
from ._protocol import EntryKind, ExecResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping

    from ._host_tools import HostToolRun
    from ._protocol import Sandbox

logger = logging.getLogger(__name__)

#: The subdirectory a run's request and response files live in, under the run directory.
CALLS_DIRECTORY = "host_tool_calls"

#: Where the launcher redirects the program's own output, and where it records its exit code.
OUTPUT_FILE = "program_output.txt"
EXIT_FILE = "program_exit_code"

#: The module a guest program imports to reach the host. Written beside the program.
SHIM_MODULE = "maf_host_tools.py"

_LAUNCHER = "run_program.sh"

#: How long the guest blocks on one call before giving up on the host, and how often it looks.
#: Bounded on both sides. It has to outlast the host's poll interval by a wide margin or a slow
#: supervisor reads as a dead one, and it must not be shorter than the run's own bound or the
#: guest gives up on a dispatch that then goes on to act. The host's deadline is what actually
#: ends a run; this is only the guest's patience, and :func:`host_tool_shim` takes it as an
#: argument for a caller whose runs are longer than this.
_GUEST_CALL_TIMEOUT = 300.0
_GUEST_POLL_SECONDS = 0.05

#: What a timed-out run may spend reading the output it quotes in the failure.
_FINAL_READ_GRACE = 2.0

#: What writing one answer may spend when the run's own bound has already passed. Small, and
#: not the run's remainder: a dispatch may finish after the deadline by design, and the answer
#: to a tool that has already acted is worth one more round trip to record.
_RESPONSE_WRITE_GRACE = 2.0

#: What the supervisor writes around a delivered value, and what that costs. The guest reads
#: the response as an object so a refusal and a value are told apart by key rather than by
#: shape. Measured from the framing rather than written down beside it, so the number a run's
#: ceilings are charged cannot drift from the bytes actually written.
_FRAME_OPEN = '{"value": '
_FRAME_CLOSE = "}"
_FRAMING_BYTES = len(_FRAME_OPEN.encode("utf-8")) + len(_FRAME_CLOSE.encode("utf-8"))

#: One sentence for every request the supervisor will not read as JSON, whether it failed to
#: parse or failed to decode. Named once so the two sites cannot drift into telling a guest
#: two different things about one situation it cannot retry either way.
_NOT_JSON = "Error: this host-tool request is not valid JSON"


@dataclass(frozen=True)
class GuestRunLayout:
    """Where one run's files live inside the guest, as absolute guest paths."""

    directory: str
    program: str
    shim: str
    launcher: str
    calls: str
    output: str
    exit_code: str


def guest_run_layout(run_directory: str, *, program: str = "program.py") -> GuestRunLayout:
    """The paths :func:`dispatch_over_exec` expects, derived from one run's directory.

    A kind writes ``program`` and :attr:`GuestRunLayout.shim`; everything else is written here.
    ``run_directory`` must be fresh per run — see this module's docstring on why nothing
    deletes.
    """
    return GuestRunLayout(
        directory=run_directory,
        program=posixpath.join(run_directory, program),
        shim=posixpath.join(run_directory, SHIM_MODULE),
        launcher=posixpath.join(run_directory, _LAUNCHER),
        calls=posixpath.join(run_directory, CALLS_DIRECTORY),
        output=posixpath.join(run_directory, OUTPUT_FILE),
        exit_code=posixpath.join(run_directory, EXIT_FILE),
    )


def host_tool_shim(
    names: frozenset[str] | set[str] | tuple[str, ...] = (),
    *,
    call_timeout: float = _GUEST_CALL_TIMEOUT,
) -> str:
    """The guest-side module source: ``call(name, **arguments)``, and one function per name.

    Written into the guest by the kind, imported by the program. It blocks on a response file
    and raises :class:`HostToolError` — defined in the generated source — on a refusal, so a
    program can catch one call's refusal and carry on, which is what the cap's *finish and
    report* refusal is for.

    ``call_timeout`` is how long the guest waits for one answer, and **it must not be shorter
    than the bound the run is given**. Give up first and the guest is wrong twice over: a
    dispatch the supervisor is still running goes on to act — a sink tool sends its message —
    while the program has already been told the host never answered, and the answer lands in
    a file nobody will read. The default suits a run bounded below it; a caller passing a
    larger ``timeout`` to :func:`dispatch_over_exec` must pass the same number here.

    ``names`` only adds convenience wrappers; it grants nothing. Resolution happens host-side
    against the registry, so a name omitted here is still callable through :func:`call` and a
    name invented here still resolves to a refusal. That is what makes the filtering below
    safe: a name this cannot spell as a function is not a name a guest cannot reach.
    """
    # Keyed by the normalised spelling, because that is the name Python will bind. Two tools
    # whose names normalise together — `lookup` and `ｌｏｏｋｕｐ` — compile to one global, and
    # emitting both would leave the second silently answering for the first. The one that
    # loses its wrapper is still reachable through `call`, which is why dropping it is safe.
    canonical: dict[str, str] = {}
    for name in sorted(names):
        if _spellable(name):
            canonical.setdefault(unicodedata.normalize("NFKC", name), name)
    wrappers = "\n\n".join(
        f"def {name}(**arguments):\n"
        f'    """Dispatch to the host tool {name!r}."""\n'
        f"    return call({name!r}, **arguments)"
        for name in canonical.values()
    )
    if not math.isfinite(call_timeout) or call_timeout <= 0:
        # Finite as well as positive: `nan` and `inf` are both `<= 0`-false, and formatting
        # either into the source emits a bare `nan`/`inf`, which is not a name the guest's
        # module can resolve. The shim would fail at *import*, before any call is made.
        raise ValueError(
            f"call_timeout must be a finite positive number of seconds, not {call_timeout}"
        )
    return _SHIM_SOURCE.format(
        calls=CALLS_DIRECTORY,
        timeout=call_timeout,
        poll=_GUEST_POLL_SECONDS,
        wrappers=f"\n\n{wrappers}\n" if wrappers else "",
    )


def _shim_globals() -> frozenset[str]:
    """Every name a generated wrapper would take away from the shim, read off the shim itself.

    Derived rather than listed, so a name the shim starts using is reserved without anyone
    remembering to add it. A wrapper is a module-level ``def``, so what it can take is a
    module-level binding — ``call``, ``_claim``, ``os`` — or a builtin the shim reaches for
    from inside one, of which ``open`` is the one that would hurt.

    Scoped rather than walked. Every ``Name`` node in the tree also catches the shim's own
    parameters and temporaries — ``name``, ``request``, ``payload`` — which no wrapper can
    reach and which are perfectly good tool names; reserving those costs a tool its
    convenience function for nothing. :mod:`symtable` answers the question actually being
    asked, which is what each name is *bound to* in the scope it appears in.
    """
    source = _SHIM_SOURCE.format(calls="", timeout=0.0, poll=0.0, wrappers="")
    scope = symtable.symtable(source, SHIM_MODULE, "exec")
    # Module scope holds every top-level binding and reference; a nested scope contributes
    # only what it resolves *outward* — a global or a builtin — never its own locals.
    reserved = {symbol.get_name() for symbol in scope.get_symbols()}
    pending = list(scope.get_children())
    while pending:
        inner = pending.pop()
        reserved.update(symbol.get_name() for symbol in inner.get_symbols() if symbol.is_global())
        pending.extend(inner.get_children())
    return frozenset(reserved)


def _spellable(name: str) -> bool:
    """Whether ``name`` can be a function in the generated module without breaking it.

    Three ways it cannot. A **keyword**: `def class(...)` is a `SyntaxError`, so one tool named
    that takes the whole shim down and every other call with it. A name the shim **uses
    itself**: a wrapper called `open` or `json` replaces machinery every dispatch needs, and
    the failure reads as a broken tool rather than a name clash. And a name that **normalises
    onto** one of those: Python NFKC-normalises identifiers at compile time, so `ｃall`
    is written `call` by the time it is a global.

    Soft keywords (`match`, `case`, `type`) parse as function names and are allowed.
    """
    normalised = unicodedata.normalize("NFKC", name)
    return (
        name.isidentifier()
        and not keyword.iskeyword(normalised)
        and normalised not in _shim_globals()
    )


_SHIM_SOURCE = '''\
"""Host tools, over files. Generated by maf-sandbox — editing this changes nothing host-side.

Every call is validated, gated and capped in the host process. This module's job is to write a
request where the supervisor is looking and to wait for the answer.
"""

import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_CALLS = os.path.join(_HERE, "{calls}")
_TIMEOUT = {timeout}
_POLL = {poll}


class HostToolError(RuntimeError):
    """A refusal from the host, or the host never answering. The sentence is the message."""


def _claim():
    """Take the lowest identifier no other caller holds, and the file that holds it.

    A lock would only cover this process. A program that forks, or uses `multiprocessing`,
    gets a second copy of this module with its own idea of the next number, and two copies
    counting privately write one request path: one call overwrites the other, and both
    callers read one answer as their own. `os.open` with `O_CREAT | O_EXCL` is a single
    filesystem operation that exactly one caller wins, whichever process or thread it is in.

    A file of its own rather than the request path, so allocation does not depend on the
    supervisor's rule that an empty file has not arrived yet. From the lowest number each
    time, so an identifier released by a call that never published is taken by the next one.
    """
    number = 1
    while True:
        claim = os.path.join(_CALLS, "%04d.claim" % number)
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            number += 1
            continue
        return "%04d" % number, claim


def _publish(request, payload):
    """Put `payload` at `request`, whole or not at all.

    Written aside and renamed: `open` creates the file empty, and a supervisor polling in that
    window would read no JSON and answer a call with a refusal it can never retry.
    `os.replace` is atomic on every platform this runs on.
    """
    staged = request + ".part"
    with open(staged, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(staged, request)


def call(name, **arguments):
    """Dispatch ``name`` with keyword ``arguments`` and return its value."""
    os.makedirs(_CALLS, exist_ok=True)
    identifier, _claim_path = _claim()
    request = os.path.join(_CALLS, identifier + ".request.json")
    try:
        _publish(request, json.dumps({{"id": identifier, "name": name, "arguments": arguments}}))
    except BaseException:
        # Published as abandoned rather than released. Giving the number back only helps if
        # somebody takes it, and the caller who would is often past it already: the supervisor
        # answers 0001 before it looks at 0002, so a concurrent caller that has published 0002
        # waits behind a hole that only a *third* call would fill. A marker is a hole the
        # supervisor can step over, which needs no third call and no shared lock.
        try:
            _publish(request, json.dumps({{"id": identifier, "abandoned": True}}))
        except Exception:
            # The filesystem is refusing writes, which is very likely why we are here at all.
            # The original failure is the one worth raising; the run is already lost.
            pass
        raise
    response = os.path.join(_CALLS, identifier + ".response.json")
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        try:
            with open(response, encoding="utf-8") as handle:
                answered = json.load(handle)
        except (OSError, ValueError):
            # Absent, or caught mid-write: the supervisor writes the whole file in one call,
            # but a backend is free to make that visible in pieces.
            time.sleep(_POLL)
            continue
        if "value" in answered:
            return answered["value"]
        raise HostToolError(answered.get("refusal", "Error: the host refused this call"))
    raise HostToolError(
        "Error: the host did not answer this call within %d seconds" % int(_TIMEOUT)
    )
{wrappers}'''


def launcher_script(layout: GuestRunLayout, interpreter: str = "python3") -> str:
    """The shell the guest runs: start the program detached, then record how it ended.

    Detached is the whole point — ``exec`` blocks until its command returns, so a program that
    waits for the host would deadlock against a supervisor that has not started. The launcher
    returns immediately and leaves two facts behind: the program's output, and its exit code in
    a file whose appearance is what tells the supervisor the run is over.

    POSIX shell, and a guest that has ``nohup``. A Windows guest or a distroless image needs a
    different launcher; that is a backend's business, and this one is a helper rather than a
    protocol.
    """
    # Built whole, then quoted once. Quoting the fragments and pasting them inside an already
    # quoted `sh -c '…'` is the classic version of this bug: the first inner quote *ends* the
    # outer argument, so a run directory with a space in it splits the command and the program
    # never starts.
    # The exit code lands by rename, for the reason the shim stages its requests: a redirection
    # creates the file empty and fills it a moment later, and a supervisor polling in that gap
    # would read an empty marker as *finished*, discard the run and report failure. `mv` within
    # one directory is atomic on any POSIX filesystem.
    staged = f"{layout.exit_code}.part"
    inner = (
        f"{_quote(interpreter)} {_quote(layout.program)} > {_quote(layout.output)} 2>&1; "
        f"printf %s $? > {_quote(staged)}; mv {_quote(staged)} {_quote(layout.exit_code)}"
    )
    return (
        f"#!/bin/sh\ncd {_quote(layout.directory)}\nnohup sh -c {_quote(inner)} >/dev/null 2>&1 &\n"
    )


def _quote(text: str) -> str:
    """Single-quote for `sh`, escaping any quote inside — safe to nest, because it is applied
    to the finished string rather than to its parts."""
    return "'" + text.replace("'", "'\\''") + "'"


async def dispatch_over_exec(
    sandbox: Sandbox,
    run: HostToolRun,
    layout: GuestRunLayout,
    *,
    timeout: float,
    poll_interval: float = 0.2,
    interpreter: str = "python3",
) -> ExecResult:
    """Run the guest program detached and serve its host-tool calls until it exits.

    The caller has already written :attr:`GuestRunLayout.program` and
    :attr:`GuestRunLayout.shim`; this writes the launcher, starts it, and supervises.

    Args:
        sandbox: Must serve ``EXEC``, ``FILES_IN`` and ``FILES_OUT`` — the supervisor stats and
            reads requests and writes responses. ``FILES_LIST`` is deliberately not required.
        run: One run's dispatch context. Every request goes through
            :meth:`~maf_sandbox.HostToolRun.dispatch`, which is where the contract lives.
        layout: From :func:`guest_run_layout`, on a directory used by this run alone.
        timeout: Seconds for the **whole program**, not one command. A wedged guest is bounded
            by this and nothing else, since a detached process outlives the ``exec`` that
            started it. Above :func:`host_tool_shim`'s ``call_timeout`` the guest gives up on
            a call this supervisor is still serving, so the two are set together or the larger
            bound is the one that is wrong.
        poll_interval: How often to look for the next request or the exit marker.
        interpreter: The guest's Python.

    Returns:
        The program's own :class:`~maf_sandbox.ExecResult` — its redirected output as
        ``stdout`` and the exit code it recorded.

    Raises:
        TimeoutError: The program left no exit marker before the deadline. Its output up to
            that point is in the message; the process may still be running, and disposing of
            the sandbox is what stops it.

    **This supervisor never cancels a dispatch already under way**, and the deadline is
    checked before starting one rather than during it. A dispatched body runs in the host
    process and may have acted already — written a row, sent a message — so cancelling it
    mid-effect would leave the effect and lose the record of it. The cost is stated rather
    than hidden: a tool that blocks forever holds the supervisor past ``timeout``, and
    bounding *that* belongs to the host tool, which is the only code that knows what it is
    waiting on.

    The promise is about this function's own bound and reaches no further. A caller that
    cancels the coroutine — an outer ``asyncio.wait_for``, a cancelled task — cancels
    whatever is in flight, a dispatch included, and can leave a tool's effect half done with
    no record written. Shielding it would trade that for a worse one, since a host tool is
    deliberately unbounded here: cancellation would then take arbitrarily long to be
    honoured, or never. :meth:`~maf_sandbox.HostToolRun.dispatch` treats the outcome as
    legitimate rather than impossible, returning the response slot when its call is
    cancelled.
    """
    # Both bounds checked before the launcher starts, because a detached program outlives a
    # supervisor that raises. `inf` is the one that matters and the one a range check misses:
    # it satisfies `> 0` and then removes the whole-run bound this function's docstring
    # promises, leaving a wedged guest to run until something else stops it. A poll interval
    # below zero — or `nan`, which `min` propagates — turns the wait between polls into no
    # wait at all, and a remote backend into a stat loop as fast as the network allows.
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
    if not math.isfinite(poll_interval) or poll_interval < 0:
        raise ValueError(
            f"poll_interval must be a finite number of seconds, zero or more, not {poll_interval}"
        )
    # Before `exec`, not after: the bound is on the whole program, and a launcher that takes
    # most of it would otherwise hand supervision a second full timeout to spend.
    deadline = time.monotonic() + timeout
    await _within(
        deadline,
        "the launcher upload",
        sandbox.write_file(layout.launcher, launcher_script(layout, interpreter)),
    )
    started = await sandbox.exec(
        f"sh {_quote(layout.launcher)}",
        working_directory=layout.directory,
        # What is left after writing the launcher, not another full bound: on a remote backend
        # that upload is a round trip, and handing `exec` the original would add it back.
        timeout=max(0.0, deadline - time.monotonic()),
    )
    if started.exit_code != 0:
        return ExecResult(
            stdout=started.stdout,
            stderr=started.stderr or "the launcher did not start the program",
            exit_code=started.exit_code,
        )

    served = 0
    allowance = _serving_bound(run)
    spent = False
    while True:
        if time.monotonic() >= deadline:
            # First in the loop, so an expired run reports *itself*. Every transport call
            # below is bounded by this same deadline, and letting one of those raise instead
            # would replace "the program did not finish" with "a stat timed out".
            raise TimeoutError(
                f"the guest program did not finish within {timeout:g}s. Output so far: "
                f"{(await _final_output(sandbox, run, layout))[:2000]}"
            )
        try:
            finished = await _read_if_present(
                sandbox, layout, layout.exit_code, cap=32, deadline=deadline
            )
            if finished is not None:
                output = await _read_if_present(
                    sandbox, layout, layout.output, cap=_output_cap(run), deadline=deadline
                )
                return ExecResult(
                    stdout=_as_text(output),
                    stderr="",
                    exit_code=_exit_code_from(finished),
                )
            # Serving awaits the tool, so the check at the top of the loop is what keeps a
            # request arriving a millisecond before the deadline from starting a dispatch the
            # bound cannot interrupt.
            if served < allowance:
                served = await _serve_next_request(sandbox, run, layout, served, deadline)
            elif not spent:
                spent = True
                logger.warning(
                    "host tools: this run has been answered %d times, which is its allowance; "
                    "the supervisor will not read further requests",
                    allowance,
                )
        except TimeoutError as stalled:
            # The deadline expired *inside* this iteration rather than between two of them.
            # Only the transport is bounded that way, so the message says which call ran out
            # while still leading with the failure the caller asked about.
            raise TimeoutError(
                f"the guest program did not finish within {timeout:g}s — {stalled}. "
                f"Output so far: {(await _final_output(sandbox, run, layout))[:2000]}"
            ) from stalled
        # Clamped: an unclamped sleep overruns the deadline by a whole interval, so a 0.1s
        # bound with a 10s interval would wait ten seconds to notice it had passed.
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


async def _serve_next_request(
    sandbox: Sandbox, run: HostToolRun, layout: GuestRunLayout, served: int, deadline: float
) -> int:
    """Answer request ``served + 1`` if it is there, and say how many have now been answered.

    By name rather than by enumeration, so a backend serving ``FILES_OUT`` without
    ``FILES_LIST`` can host this. Sequential ids also make a replayed one unreachable: the
    supervisor never looks at an id it has already answered, so a guest rewriting an old
    request file cannot spend a second dispatch on it.
    """
    identifier = f"{served + 1:04d}"
    request_path = posixpath.join(layout.calls, f"{identifier}.request.json")
    body = await _read_if_present(
        sandbox,
        layout,
        request_path,
        cap=_request_cap(run),
        deadline=deadline,
        exact=True,
    )
    if body is None:
        return served
    if time.monotonic() >= deadline:
        # Between reading the request and dispatching it, the bound can pass — the reads above
        # are bounded, so they can succeed with a microsecond left. Checking at the top of the
        # supervisor loop cannot see that window; this can. The request stays unanswered and
        # unserved, so the id is not spent and a later run would find it again.
        return served
    answer = await _answer(run, body, identifier)
    if answer is None:
        # An abandoned number: the guest took it and could not publish a request under it, so
        # nothing is waiting for a response and writing one would be a round trip spent on
        # nobody. Stepping over it is what keeps the caller of the *next* number from waiting
        # on a request that is never coming.
        logger.debug(
            "host tools: request %s was abandoned by the guest, stepping over it", identifier
        )
        return served + 1
    response_path = posixpath.join(layout.calls, f"{identifier}.response.json")
    # Its own floor, not the run's remainder. A dispatch is allowed to finish after the
    # deadline — that is the whole no-cancellation policy — and a tool that starts with a
    # millisecond left and returns a second later would otherwise have its answer thrown away
    # by a write bounded at zero: the effect happened, the record did not. Enforcing the bound
    # on the record while exempting the effect is the wrong half of the pair to keep.
    await _within(
        max(deadline, time.monotonic() + _RESPONSE_WRITE_GRACE),
        f"write the answer to {identifier}",
        sandbox.write_file(response_path, answer),
    )
    return served + 1


async def _answer(
    run: HostToolRun, body: str | _TooLarge | _NotText, identifier: str
) -> str | None:
    """One request's JSON answer, or ``None`` when the request wants no answer at all.

    ``None`` is the abandonment case and only that: a number the guest claimed and could not
    publish under. Every other outcome, refusals included, is a sentence the guest may read.
    """
    if isinstance(body, _TooLarge):
        return json.dumps(
            {
                "refusal": "Error: this host-tool request is larger than the host will read — "
                "pass less, or write what you have to a file instead"
            }
        )
    if isinstance(body, _NotText):
        # The same sentence as an unparseable request, and deliberately: from the guest's
        # side both are a request the host would not read, and neither is retryable.
        return json.dumps({"refusal": _NOT_JSON})
    try:
        parsed = cast(object, json.loads(body))
    except (ValueError, RecursionError):
        # `RecursionError` is not a `ValueError`: a deeply nested payload, well under the size
        # cap, would otherwise escape the supervisor and leave the detached guest waiting for
        # an answer that is never written.
        logger.warning("host tools: request %s is not JSON, refusing it", identifier)
        return json.dumps({"refusal": _NOT_JSON})
    if not isinstance(parsed, dict):
        return json.dumps({"refusal": "Error: a host-tool request must be a JSON object"})
    request = cast("dict[str, object]", parsed)
    # Handed over as-is, casts and all: `dispatch` is where guest data is checked, and it
    # takes `object` to its own `isinstance` gates for exactly this reason. Narrowing here
    # would be a second validation in the wrong process's file — and the annotations describe
    # the contract a *host* calls under, not what a transport can promise about a JSON blob.
    if request.get("abandoned") is True:
        # The guest's own marker for a number it took and could not use. Nothing dispatches
        # and nothing is written; a guest spending its allowance on these is bounded by the
        # same count as one spending it on refusals — see `_serving_bound`.
        return None
    name = cast(str, request.get("name"))
    arguments = cast("Mapping[str, Any] | None", request.get("arguments"))
    # The framing is declared, not policed afterwards. `dispatch` charges the run for the
    # payload *and* these bytes, so a value that only fits unwrapped is refused before the
    # ledger is spent — checking it here could only turn a committed success into a refusal,
    # leaving the run paying for a response that was never delivered.
    result = await run.dispatch(name, arguments, framing_bytes=_FRAMING_BYTES)
    if not result.ok:
        return json.dumps({"refusal": result.refusal})
    # `value_json` is already the serialized return value, capped host-side. Splicing it in as
    # text keeps those exact bytes rather than re-serializing a parse of them.
    return _FRAME_OPEN + (result.value_json or "null") + _FRAME_CLOSE


class _TooLarge:
    """Sentinel: the file is there and the host will not read it at that size."""


class _NotText:
    """Sentinel: the file is there and it is not UTF-8, so nothing in it is a request."""


async def _within[T](deadline: float, what: str, call: Awaitable[T]) -> T:
    """Await one transport call inside what is left of the run's deadline.

    Sandbox I/O is bounded and a dispatched host tool is not, and the difference is where the
    effect lives. A stalled ``stat_file`` is the backend's control plane hanging — cancelling
    it costs nothing and is the only thing standing between one slow request and a supervisor
    that never returns. A host tool has already begun acting in this process; see
    :func:`dispatch_over_exec` on why that one is left alone.
    """
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return await asyncio.wait_for(call, timeout=remaining)
    except TimeoutError as expired:
        raise TimeoutError(
            f"the sandbox did not answer {what} within the run's remaining {remaining:.1f}s"
        ) from expired


async def _read_if_present(
    sandbox: Sandbox,
    layout: GuestRunLayout,
    path: str,
    *,
    cap: int,
    deadline: float,
    exact: bool = False,
) -> str | _TooLarge | _NotText | None:
    """Stat, then read within ``cap`` — or ``None`` when the file is not there yet.

    Stat-before-read is the pull surface's own rule, and it is what makes polling affordable:
    the common case is one stat that answers ``None``.

    ``exact`` picks how bytes that are not UTF-8 are treated, and the two callers want
    opposite things. A request is data the host acts on, so it decodes strictly. Everything
    else here is a program's own output, quoted back to a human, where replacing one bad byte
    beats losing the whole of it.
    """
    entry = await _within(
        deadline,
        f"stat {posixpath.basename(path)}",
        sandbox.stat_file(path, working_directory=layout.directory),
    )
    if entry is None or entry.kind is not EntryKind.FILE:
        return None
    if entry.size_bytes == 0:
        # Not there *yet*: an empty file is what a redirection or a plain `open` leaves in the
        # window before its content. Both writers here rename into place instead, so a zero
        # length means a backend or a guest that does not — and reading it would answer a
        # valid call with a refusal, or read an empty exit marker as a finished run.
        return None
    if entry.size_bytes is None or entry.size_bytes > cap:
        logger.warning(
            "host tools: %s is %s bytes against a %s cap, refusing to read it",
            posixpath.basename(path),
            entry.size_bytes,
            cap,
        )
        return _TooLarge()
    try:
        raw = await _within(
            deadline,
            f"read {posixpath.basename(path)}",
            sandbox.read_file(path, working_directory=layout.directory, max_bytes=cap),
        )
    except Exception as failure:  # noqa: BLE001 — a read that fails is a poll that missed
        logger.warning("host tools: reading %s failed: %s", path, error_detail(failure))
        return None
    if len(raw) > cap:
        # The pull surface's own rule, and `collect_outputs` keeps it too: `max_bytes` is what
        # a backend was asked for, not what it is guaranteed to have honoured — one whose SDK
        # buffers the whole response can only refuse after the fact. The stat that came before
        # is a second-hand number and the file may have grown since. Counted here, so nothing
        # over the cap is decoded, let alone dispatched.
        logger.warning(
            "host tools: %s returned %d bytes against a %d cap, refusing it",
            posixpath.basename(path),
            len(raw),
            cap,
        )
        return _TooLarge()
    if not exact:
        return raw.decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as invalid:
        # Refused rather than repaired. Replacement decoding leaves an invalid byte inside a
        # JSON string as U+FFFD and the document still parses, so the tool would be called
        # with an argument the guest never sent — a path, an id, a name, silently one
        # character different — and the host would act on it.
        logger.warning("host tools: %s is not UTF-8: %s", path, error_detail(invalid))
        return _NotText()


async def _final_output(sandbox: Sandbox, run: HostToolRun, layout: GuestRunLayout) -> str:
    """What the program printed, for the timeout message — on a short allowance of its own.

    The run's deadline has passed by the time this is called, so reading under it would expire
    instantly and the message would carry nothing. This buys a couple of seconds for the
    diagnostic and answers with nothing rather than raising if even that is too long: the
    caller is already being told the run failed, and the reason must not become "reading the
    reason failed".
    """
    try:
        return _as_text(
            await _read_if_present(
                sandbox,
                layout,
                layout.output,
                cap=_output_cap(run),
                deadline=time.monotonic() + _FINAL_READ_GRACE,
            )
        )
    except Exception as failure:  # noqa: BLE001 — a diagnostic must not replace the failure
        # Not just `TimeoutError`: `stat_file` may raise anything a backend's client raises,
        # and this runs while a `TimeoutError` is being constructed. Losing the run's own
        # reason to a failure in reading the reason is the one outcome worth ruling out.
        logger.warning("host tools: could not read the program's output: %s", error_detail(failure))
        return ""


def _as_text(value: str | _TooLarge | _NotText | None) -> str:
    """The text, or nothing — an absent or over-cap file reads as no output rather than a type."""
    return value if isinstance(value, str) else ""


def _serving_bound(run: HostToolRun) -> int:
    """How many requests this supervisor will read before it stops reading them.

    Every request, not every dispatch: a malformed one is answered before the door and so
    never spends the cap that is meant to bound this. One more than the cap because the
    transport serves one outstanding call at a time, which makes a single refusal enough to
    tell the guest the cap is gone. Past it the supervisor only waits for the program to end.
    """
    return run.registry.max_dispatches_per_run + 1


def _request_cap(run: HostToolRun) -> int:
    """The request ceiling, borrowed from the response one — the same concern, one vocabulary."""
    return run.registry.response_limits.max_bytes_per_file


def _output_cap(run: HostToolRun) -> int:
    """What the host will read back of the program's own output.

    The **total** leg rather than the per-file one, which is the opposite of :func:`_request_cap`
    and deliberate. A request file is a host-tool response's counterpart and belongs to that
    accounting; the program's stdout does not — on any other execution path it comes back
    through :attr:`ExecResult.stdout` bounded by nothing in this vocabulary at all. Capping it
    at the per-*response* ceiling would mean a program printing more than one tool call may
    return loses its whole output to a number chosen for something else, and only under this
    transport. So the run's total is borrowed as the largest bound this vocabulary sanctions,
    rather than inventing a second ceiling for one concern.

    That is a stretch of ``response_limits`` either way, and it is the one place this transport
    reaches for a number that was not written for it.
    """
    return run.registry.response_limits.max_total_bytes


def _exit_code_from(recorded: str | _TooLarge | _NotText) -> int:
    """The exit code the launcher wrote, or 1 when it is not a number this can trust.

    ``_NotText`` cannot arrive here — the marker is read with replacement decoding — and is
    accepted anyway, so that widening what a read may return can never turn into a failed run
    in the one place a failure would be silent.
    """
    if isinstance(recorded, _TooLarge | _NotText):
        return 1
    try:
        return int(recorded.strip())
    except ValueError:
        return 1
