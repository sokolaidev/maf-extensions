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
from ._outputs import SandboxTransferCapExceeded
from ._protocol import EntryKind, ExecResult
from .paths import confine_guest_path

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

#: Where the launcher stages the exit code before renaming it into place. A program under
#: this name is truncated to the exit digits and renamed away by the launcher's own last
#: line. The launcher stages beside whatever marker its layout declares; deriving this from
#: :data:`EXIT_FILE` keeps the two agreeing for any layout :func:`guest_run_layout` built,
#: which is the only kind the refusal guards.
_STAGED_EXIT_FILE = f"{EXIT_FILE}.part"

#: The module a guest program imports to reach the host. Written beside the program.
SHIM_MODULE = "maf_host_tools.py"

_LAUNCHER = "run_program.sh"

#: The program's working directory, and the only one a kind puts guest-named files in. A model
#: naming a file names it *here*, so no name it can choose reaches the transport's own.
WORK_DIRECTORY = "work"

#: Where everything the transport owns lives, beside the work directory rather than in it.
_TRANSPORT_DIRECTORY = "host_tools"

#: Every name the transport puts in that directory — the layout's own files and the
#: launcher's staged exit marker. Not a kind's business, and deliberately not exported: two
#: directories are what make a guest-supplied name harmless, and that is what replaced the
#: list of names a kind used to have to refuse.
_TRANSPORT_FILENAMES = frozenset(
    {SHIM_MODULE, _LAUNCHER, OUTPUT_FILE, EXIT_FILE, _STAGED_EXIT_FILE, CALLS_DIRECTORY}
)

#: Module names a ``program`` may not be called, and why they are matched by **stem**.
#:
#: A file in this directory is importable, because the directory is on the interpreter's path
#: from startup — so what matters is the module name a file would answer to, not the spelling
#: of its suffix. ``FileFinder`` tries extension suffixes first, then source, then bytecode, so
#: ``json.cpython-313-x86_64-linux-gnu.so`` outranks ``json.py``; refusing exact filenames
#: would leave every one of those twins open. Everything up to the first dot is the stem.
#:
#: Two families, refused for different reasons.
#:
#: :data:`_STARTUP_STEMS` is what CPython reaches on its way up. ``encodings`` is imported
#: before the interpreter can report anything and takes it down with a path-configuration
#: dump. ``sitecustomize`` is executed by ``site``, so as a program it runs twice, once during
#: startup and once as the script — as does ``usercustomize`` wherever user site is enabled,
#: which is everywhere except a virtualenv. ``site`` does **not** shadow the stdlib module
#: from here, checked on 3.11 through 3.13, and is refused anyway as the module that imports
#: the other two, on the grounds that which path CPython resolves it by is an implementation
#: detail rather than a promise.
#:
#: The second group is the stdlib that startup itself imports. It is empty on a guest whose
#: standard library is frozen — 3.11 onwards — and on an older one it is the difference between
#: a run and a path-configuration dump: on 3.10, ``site`` reaches ``os`` which reaches ``stat``
#: through ordinary path lookup, so the program answers as that module during initialisation.
#: The guest's version is not this package's to pin (``interpreter`` defaults to ``python3``,
#: which is 3.10 on Ubuntu 22.04), so they are refused everywhere rather than conditionally.
#:
#: This is deliberately *not* "everything imported before the script runs", which is not a set
#: anyone can write down: a ``.pth`` file may import any name it likes, so a guest image with
#: setuptools reaches ``_distutils_hack`` and one with ``PYTHONWARNINGS`` reaches ``warnings``.
#: Those belong to the image, and a constructor pretending to enumerate them would be making a
#: promise it cannot keep. What is here was found by running a census against CPython 3.8
#: through 3.13 — the top-level modules each imports from a file before running a script — so
#: it is a floor under the common failures on those, and not a proof for any guest.
_STARTUP_STEMS = frozenset(
    {
        "encodings",
        "site",
        "sitecustomize",
        "usercustomize",
        # Reached during startup on a guest whose stdlib is not frozen. Censused on 3.8, 3.9
        # and 3.10 by listing the top-level modules whose origin is a file, and empty from
        # 3.11 — see this constant's docstring for what that census does and does not prove.
        "abc",
        "codecs",
        "genericpath",
        "io",
        "posixpath",
        "stat",
        "_collections_abc",
        "_sitebuiltins",
        # 3.8 and 3.9, where `site` reading any `.pth` file builds a `TextIOWrapper` without
        # an explicit encoding, and that imports this. Gone in 3.10, which removed the module.
        "_bootlocale",
    }
)

#: :data:`_SHIM_STEMS` is what the generated shim imports, and the collision is with the shim
#: rather than the interpreter: the two share a directory, so a program named ``json`` *is*
#: what the shim's own ``import json`` finds. The program's body runs a second time under that
#: name and every dispatch afterwards dies on an attribute the real module would have had —
#: a traceback that names ``dumps`` and never the collision.
#:
#: Only ``json`` does that on a current guest. ``time`` is built in and ``os`` is frozen from
#: 3.11, so neither is ever resolved from a directory. On 3.10 ``os`` *is* resolved from one —
#: but by ``site``, during startup, so it fails as a member of :data:`_STARTUP_STEMS` would,
#: dying before the program is the script rather than misleading a dispatch. Two mechanisms,
#: one name, and which modules are frozen is a per-version detail rather than a contract.
#: They are refused for the same reason the set is checked by stem: refusing a program name
#: no kind wants costs nothing, and missing one costs a traceback that points elsewhere.
#:
#: Derived from the shim by a test that parses it, so adding an import cannot leave this
#: behind.
_SHIM_STEMS = frozenset({"json", "os", "time"})

#: The shim's own module name, which is the one importable member of
#: :data:`_TRANSPORT_FILENAMES` — and so the one that needs the stem rule rather than the
#: exact-name refusal the rest of that set gets. A ``maf_host_tools.so`` beside the real
#: ``maf_host_tools.py`` wins the program's very first import, because extensions are tried
#: before source, and the run dies on an invalid ELF header rather than on anything that names
#: the collision. Derived from :data:`SHIM_MODULE` so the two cannot drift apart.
_SHIM_MODULE_STEM = SHIM_MODULE.split(".", 1)[0]


class SandboxProgramTimeout(TimeoutError):
    """The *run's own* bound expired: the guest program did not finish in the time it was given.

    A :class:`TimeoutError` subclass, so a caller that only wants "it timed out" is unaffected.
    Catch it specifically to distinguish the two things a ``TimeoutError`` from this transport
    can mean: **this** one is the run's budget, and the program may still be running; a bare
    one is a backend failing for a reason of its own and says nothing about the program.

    ``output`` is what the program had printed when the run was given up on, already capped —
    empty on the two starting legs, where there is nothing to have read yet. An attribute
    rather than message text, so a caller can surface the program's own stdout alone.
    """

    def __init__(self, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.output = output


#: How long the guest blocks on one call before giving up on the host, and how often it looks.
#: Bounded on both sides. It has to outlast the host's poll interval by a wide margin or a slow
#: supervisor reads as a dead one, and it must not be shorter than the run's own bound or the
#: guest gives up on a dispatch that then goes on to act. The host's deadline is what actually
#: ends a run; this is only the guest's patience, and :func:`host_tool_shim` takes it as an
#: argument for a caller whose runs are longer than this.
_GUEST_CALL_TIMEOUT = 300.0
_GUEST_POLL_SECONDS = 0.05

#: What reading the last of a run may spend once its own bound is gone: the output a timeout
#: quotes, a finished run's output, and the last look for an exit marker. Shrinking it narrows
#: a *returned* result's window, not only a diagnostic's.
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
    """Where one run's files live inside the guest, as absolute guest paths.

    Two directories under one run, and which file goes in which is the whole defence against
    a guest-supplied name colliding with the machinery serving its own call:

    * :attr:`work` is the program's working directory. Everything a *model* names — files
      shared in, artifacts written out — lives here, and a kind may put anything here.
    * everything else lives in a sibling the model can never name into, because a kind writes
      guest-named files only to :attr:`work` and collects only from it.

    :attr:`program` is in the second one, beside the shim, and that placement is load-bearing
    rather than tidy. ``sys.path[0]`` is the directory of the *script*, not the working
    directory, so a program run from :attr:`work` would put :attr:`work` ahead of the shim on
    the import path and a guest file named ``maf_host_tools.py`` would become the module the
    program imports. Run from beside the shim, that file is not on ``sys.path`` at all: a
    script's working directory is never added to it.
    """

    directory: str
    work: str
    program: str
    shim: str
    launcher: str
    calls: str
    output: str
    exit_code: str


def guest_run_layout(run_directory: str, *, program: str = "program.py") -> GuestRunLayout:
    """The paths :func:`dispatch_over_exec` expects, derived from one run's directory.

    A kind writes ``program`` and :attr:`GuestRunLayout.shim`; everything else is written here.

    ``run_directory`` must be absolute, free of backslashes — the grammar
    :func:`~maf_sandbox.paths.confine_guest_path` enforces on every pull call — free of ``:``,
    which ``PYTHONPATH`` uses to separate entries and cannot quote, and fresh per run, on which
    see this module's docstring. The directory comes back normalised, so ``..`` is fine to pass
    and one spelling reaches every call.

    ``program`` must be a plain file name, and not one of three families this directory makes
    dangerous: a name the layout already uses, a name the generated shim imports, or a name
    CPython imports at startup. The last two are matched by **stem**, since a suffix decides
    only which loader answers — see :data:`_SHIM_STEMS` and :data:`_STARTUP_STEMS` for what
    each collision does.

    Everything but freshness is refused here; freshness is not visible from a path, and a stale
    exit marker ends the next run on its first poll.
    """
    if not posixpath.isabs(run_directory):
        raise ValueError(
            f"run_directory must be an absolute guest path, not {run_directory!r}: every path "
            "in the layout is joined onto it and resolved against it again by the pull calls"
        )
    if ":" in run_directory:
        # `PYTHONPATH` has no escape for its own separator, and the launcher puts the shim's
        # directory there. Under `/runs/job:slot` the interpreter reads two entries — `/runs/job`
        # and a *relative* `slot/host_tools`, resolved against the guest's working directory —
        # so a guest file at `slot/host_tools/maf_host_tools.py` becomes importable. Refused
        # here rather than encoded around, because a path this cannot carry is one the whole
        # arrangement cannot carry.
        raise ValueError(
            f"run_directory must not contain ':', and {run_directory!r} does: the shim's "
            "directory is passed to the guest through PYTHONPATH, which uses ':' to separate "
            "entries and offers no way to quote one"
        )
    # Twice on purpose: containment against itself is trivially true, so only the spelling
    # is under test.
    run_directory = confine_guest_path(run_directory, run_directory)
    if program != posixpath.basename(program) or program in {"", ".", ".."}:
        raise ValueError(
            f"program must be a plain file name, not {program!r}: it is written beside the "
            "shim and imports it, which only works when the two share a directory"
        )
    if program in _TRANSPORT_FILENAMES:
        # Each collision breaks the run in its own way and none of them say so: the shell
        # truncates the output file before the interpreter opens the program, the launcher
        # and the shim are written over whatever the kind put there, the calls directory
        # cannot be created where a file already is, and a program at the staged exit-marker
        # name is truncated and renamed away by the launcher once it exits. A *model* cannot
        # reach these names — that is what the two directories are for — but the kind's own
        # `program` lands in the same one, so this stays a refusal at the one door it can
        # still come through.
        raise ValueError(
            f"program must not be a name this layout already uses, and {program!r} is one of "
            f"{sorted(_TRANSPORT_FILENAMES)}"
        )
    stem = program.split(".", 1)[0]
    if stem == _SHIM_MODULE_STEM:
        raise ValueError(
            f"program must not answer to the shim's own module name, and {program!r} answers "
            f"to {stem!r}: the stem is reserved for the shim, because a file here under it "
            f"with a loader's suffix either shadows the import the program opens with or "
            f"cannot be run as a program, and no suffix makes the name worth allowing"
        )
    if stem in _SHIM_STEMS:
        raise ValueError(
            f"program must not be named for a module the generated shim imports, and "
            f"{program!r} answers to {stem!r}, one of {sorted(_SHIM_STEMS)}: it shares a "
            "directory with the shim, which is first on the interpreter's path, so the shim "
            "would import the program instead of the module it meant"
        )
    if stem in _STARTUP_STEMS:
        # This directory is on the interpreter's path from *startup*, not only once the script
        # is found, so a file named for something the interpreter imports on its way up runs —
        # or fails — before the program does. `_STARTUP_STEMS` says what each one does.
        raise ValueError(
            f"program must not be named for a module CPython imports at startup, and "
            f"{program!r} answers to {stem!r}, one of {sorted(_STARTUP_STEMS)}: this directory "
            "is on the path before the program is the script, so such a file is reached during "
            "initialisation"
        )
    served = posixpath.join(run_directory, _TRANSPORT_DIRECTORY)
    return GuestRunLayout(
        directory=run_directory,
        work=posixpath.join(run_directory, WORK_DIRECTORY),
        program=posixpath.join(served, program),
        shim=posixpath.join(served, SHIM_MODULE),
        launcher=posixpath.join(served, _LAUNCHER),
        calls=posixpath.join(served, CALLS_DIRECTORY),
        output=posixpath.join(served, OUTPUT_FILE),
        exit_code=posixpath.join(served, EXIT_FILE),
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
    """Take the lowest identifier no other caller holds.

    A lock would only cover this process. A program that forks, or uses `multiprocessing`,
    gets a second copy of this module with its own idea of the next number, and two copies
    counting privately write one request path: one call overwrites the other, and both
    callers read one answer as their own. `os.open` with `O_CREAT | O_EXCL` is a single
    filesystem operation that exactly one caller wins, whichever process or thread it is in.

    A file of its own rather than the request path, so allocation does not depend on the
    supervisor's rule that an empty file has not arrived yet. Claims are never removed: a
    number this hands out is published under, as a request or as an abandonment, and from the
    lowest each time because a process cannot know what the others have already taken.
    """
    number = 1
    while True:
        claim = os.path.join(_CALLS, "%04d.claim" % number)
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            number += 1
            continue
        return "%04d" % number


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
    identifier = _claim()
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
        "Error: the host did not answer this call within %g seconds" % _TIMEOUT
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
    # The command is built whole so `_quote` applies to finished strings rather than to
    # fragments nested inside an already quoted `sh -c '…'`. What it has to preserve:
    #
    #   * `PYTHONUNBUFFERED`, because this file is the timeout's only witness and CPython
    #     block-buffers stdout into a redirection. Through the environment because
    #     `interpreter` need not be CPython: the variable costs a program that is not Python
    #     nothing, where a spliced `-u` would be a flag it does not know.
    #   * The exit code lands by rename, so a poll cannot read the empty file a redirection
    #     leaves for a moment as a finished run. Staged beside its final name, which is the
    #     cheapest way to know the rename stays inside one filesystem and so stays atomic.
    #   * `nohup … &`, because `exec` returns when its command does and the program must
    #     outlive it — see this function's docstring.
    #   * `mkdir -p` because a run whose kind shared no files has nothing else to create the
    #     work directory, guarded because `sh` does not stop on a failed command: an unguarded
    #     `cd` leaves the program running wherever the launcher was exec'd, writing artifacts
    #     where nothing collects them and exiting 0. A non-zero launcher is already reported.
    #   * The program runs *in* the work directory and *from* the transport's, with the
    #     transport's on `PYTHONPATH` besides — `sys.path[0]` follows the script, but that is
    #     a default `PYTHONSAFEPATH` switches off, and then the path is all there is.
    #   * **Inherited path entries are kept only if absolute.** A relative one resolves
    #     against the working directory, which this launcher has just changed to the guest's
    #     own — so whatever the image meant by `.`, it does not mean that here. Left in, it
    #     makes the run directory importable at *interpreter startup*, where a `sitecustomize`
    #     a model wrote runs before the program does. An absolute entry cannot name this run:
    #     the directory is per-run and did not exist when the image was built.
    staged = f"{layout.exit_code}.part"
    # The shim's own directory, which is the one an import has to reach; `program` is beside
    # it by construction, and reading it from the shim keeps the two from being separated.
    importable = posixpath.dirname(layout.shim)
    inner = (
        f"PYTHONUNBUFFERED=1 {_quote(interpreter)} {_quote(layout.program)} "
        f"> {_quote(layout.output)} 2>&1; "
        f"printf %s $? > {_quote(staged)}; mv {_quote(staged)} {_quote(layout.exit_code)}"
    )
    return (
        "#!/bin/sh\n"
        f"mkdir -p {_quote(layout.work)} && cd {_quote(layout.work)} || exit 1\n"
        "maf_kept=''\n"
        # `set -f` because the expansion below is deliberately unquoted, for the word splitting
        # `IFS=:` gives it — and an unquoted word is globbed as well as split. The guest owns
        # the directory that would be globbed against, and Python never globs `PYTHONPATH`, so
        # an inherited `/opt/plugins/*` has to reach it as itself.
        "set -f\n"
        "IFS=:\n"
        "for maf_entry in ${PYTHONPATH:-}; do\n"
        '  case "$maf_entry" in /*) maf_kept="${maf_kept:+$maf_kept:}$maf_entry" ;; esac\n'
        "done\n"
        "unset IFS\n"
        "set +f\n"
        f'PYTHONPATH={_quote(importable)}"${{maf_kept:+:$maf_kept}}"\n'
        "export PYTHONPATH\n"
        # Prefixed, then removed: a bare `kept` or `entry` collides with an image that exports
        # one, and an exported name stays exported — the guest would read the launcher's
        # leftovers where its own value belongs.
        "unset maf_kept maf_entry\n"
        f"nohup sh -c {_quote(inner)} >/dev/null 2>&1 &\n"
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
        SandboxProgramTimeout: The run's own bound expired. Where the program had started,
            its output up to that point is in the message and on ``output``, and the process
            may still be running — disposing of the sandbox is what stops it. On the two
            starting legs, the launcher upload and the ``exec`` that runs it, ``output`` is
            empty instead: the output file does not exist yet, and on a backend that
            began the command before its own call returned there may be output nobody read.
            Distinct from a bare ``TimeoutError`` below, which is a backend failing for a
            reason of its own and says nothing about whether the program is still going.
        Exception: Whatever the backend raises from a stat or a read that is not a file
            simply not being there yet. A permanent failure — a permission error, a client
            that cannot reach its daemon — is reported as itself rather than retried until
            the run looks like a slow guest.

    **The exit marker is the guest's claim, not proof.** It is a file in the guest's own
    filesystem, so model-written code can create it and keep running, and this returns success
    while the program is still going. There is no way to tell from the pull surface — a nonce
    written into the launcher is a nonce the guest can read — and
    :class:`~maf_sandbox.SandboxBackend` reuses a warm sandbox for the same key and kind, so a
    caller that needs the process actually gone must dispose of the sandbox rather than trust
    the return.

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
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        # Zero is the same defect as negative, arrived at by a different route: `sleep(0)`
        # yields to the loop and comes straight back, so the interval that is supposed to
        # throttle polling stops throttling it. A test wanting to run fast wants a small
        # interval, not none.
        raise ValueError(
            f"poll_interval must be a finite positive number of seconds, not {poll_interval}"
        )
    # Before `exec`, not after: the bound is on the whole program, and a launcher that takes
    # most of it would otherwise hand supervision a second full timeout to spend.
    deadline = time.monotonic() + timeout
    try:
        await _within(
            deadline,
            "the launcher upload",
            sandbox.write_file(layout.launcher, launcher_script(layout, interpreter)),
        )
    except _DeadlineExpired as gone:
        # The one `_within` outside the supervisor loop, so nothing else converts what it
        # raises, and a module-private type would otherwise cross the public boundary.
        raise SandboxProgramTimeout(
            f"the run's {timeout:g}s were gone before the program was started — {gone}"
        ) from gone
    try:
        started = await sandbox.exec(
            f"sh {_quote(layout.launcher)}",
            working_directory=layout.directory,
            # What is left after writing the launcher, not another full bound: on a remote
            # backend that upload is a round trip, and handing `exec` the original would add
            # it back.
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except TimeoutError as spent:
        # `exec` was handed this run's remainder, so its expiry is this run's — every
        # `TimeoutError` from it, without re-reading a clock that may be a resolution behind
        # the timer that fired. The backend's own text stays in the log for the same reason it
        # does in `_completed`: this message reaches a model through whichever kind is running.
        logger.warning(
            "host tools: the run ran out while starting the program: %s", error_detail(spent)
        )
        raise SandboxProgramTimeout(
            f"the run's {timeout:g}s were gone while starting the program"
        ) from spent
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
            #
            # One last look before saying that, on a grace of its own: the marker can land
            # inside the poll interval that just elapsed, or be there and take longer to read
            # than a remainder already at zero. Reporting a finished run as unfinished loses
            # its exit code, and neither window is visible from inside the loop.
            giving_up = time.monotonic() + _FINAL_READ_GRACE
            landed = await _marker_if_present(sandbox, layout, giving_up)
            if landed is not None:
                return await _completed(sandbox, run, layout, landed, deadline)
            printed, note = await _final_output(sandbox, run, layout, giving_up)
            raise SandboxProgramTimeout(
                f"the guest program did not finish within {timeout:g}s. "
                f"{_output_clause(printed, note)}",
                output=printed[:2000],
            )
        try:
            finished = await _read_if_present(
                sandbox, layout, layout.exit_code, cap=32, deadline=deadline
            )
            if finished is not None:
                return await _completed(sandbox, run, layout, finished, deadline)
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
        except _DeadlineExpired as stalled:
            # The same last look, because the call that ran out may have been the marker's own
            # read: a stat that found it with a millisecond left proves the program finished,
            # and this handler would otherwise announce the opposite.
            giving_up = time.monotonic() + _FINAL_READ_GRACE
            landed = await _marker_if_present(sandbox, layout, giving_up)
            if landed is not None:
                # The run is a success after all, so the call that ran out will not be in any
                # exception — and it may be the write that was recording a tool's effect.
                logger.warning(
                    "host tools: the run finished, but a transport call had already run out: %s",
                    error_detail(stalled),
                )
                return await _completed(sandbox, run, layout, landed, deadline)
            # This run's own bound, expiring *inside* an iteration rather than between two of
            # them. Only the transport is bounded that way, so the message says which call ran
            # out while still leading with the failure the caller asked about. A backend's own
            # `TimeoutError` is deliberately not caught here — see `_within`.
            printed, note = await _final_output(sandbox, run, layout, giving_up)
            raise SandboxProgramTimeout(
                f"the guest program did not finish within {timeout:g}s — {stalled}. "
                f"{_output_clause(printed, note)}",
                output=printed[:2000],
            ) from stalled
        # Clamped: an unclamped sleep overruns the deadline by a whole interval, so a 0.1s
        # bound with a 10s interval would wait ten seconds to notice it had passed.
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


async def _marker_if_present(
    sandbox: Sandbox, layout: GuestRunLayout, until: float
) -> str | _TooLarge | _NotText | None:
    """The exit marker within what is left of ``until``, or ``None`` — never a raise.

    Called once, on the way to reporting a run as unfinished, so it answers a question rather
    than adding a failure: whatever a backend raises here means the same as nothing being
    there, and the run's own reason is the one worth keeping.
    """
    try:
        marker = await _read_if_present(sandbox, layout, layout.exit_code, cap=32, deadline=until)
    except Exception as failure:  # noqa: BLE001 — a last look must not replace the timeout
        logger.warning("host tools: a last look for the marker failed: %s", error_detail(failure))
        return None
    # Whatever the loop would have accepted, including a marker too large to read: `_completed`
    # reads it through `_exit_code_from`, which has an answer for every one of them. Filtering
    # here would make one file mean "finished" a poll before the deadline and "never finished"
    # a poll after it.
    return marker


async def _completed(
    sandbox: Sandbox,
    run: HostToolRun,
    layout: GuestRunLayout,
    finished: str | _TooLarge | _NotText,
    deadline: float,
) -> ExecResult:
    """What a run whose exit marker has been read returns, output and all.

    The marker proves the program finished, so nothing here may report that it did not.
    Reading the output is the last thing left and the run's remainder can be zero by then, so
    it gets a floor of its own — and a read that runs out even of that still comes back with
    the exit code. A backend failing for some other reason still propagates, as everywhere
    else on this path.
    """
    try:
        output = await _read_if_present(
            sandbox,
            layout,
            layout.output,
            cap=_output_cap(run),
            deadline=max(deadline, time.monotonic() + _FINAL_READ_GRACE),
        )
    except TimeoutError as unread:
        # Every `TimeoutError`, not only this run's own: `_within` lets a backend's through
        # untouched, and the grace above guarantees a window where it does. Propagating one
        # would say the program never finished — the single thing the marker has disproved —
        # so the backend's sentence goes beside the exit code it would otherwise replace.
        logger.warning(
            "host tools: the program finished but its output did not: %s", error_detail(unread)
        )
        # Ours names the call and nothing else, and a caller is owed which read gave up. A
        # backend's is a client's own text — endpoint, subscription, request id — and this
        # value is rendered for a model by every kind that has one, so it stays in the log.
        # The distinction is the same one `SandboxProgramTimeout` exists to draw.
        blamed = f": {unread}" if isinstance(unread, _DeadlineExpired) else ""
        return ExecResult(
            stdout="",
            stderr=f"the program finished, but its output could not be read{blamed}",
            exit_code=_exit_code_from(finished),
        )
    return ExecResult(
        stdout=_as_text(output),
        stderr=_why_no_output(output),
        exit_code=_exit_code_from(finished),
    )


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


class _DeadlineExpired(TimeoutError):
    """This run's bound ran out inside a transport call, as against a backend's own.

    A `TimeoutError` reaching the supervisor means one of two unrelated things, and only one
    of them is the run ending. Named so the loop can catch its own and let a backend's — which
    carries a diagnosis the run-level message would erase — go to the caller intact. A subclass
    of `TimeoutError` because a backend's own is one too, and the pair the caller has to tell
    apart is `SandboxProgramTimeout` against everything else; this one never leaves the module.
    """


async def _within[T](deadline: float, what: str, call: Awaitable[T]) -> T:
    """Await one transport call inside what is left of the run's deadline.

    Sandbox I/O is bounded and a dispatched host tool is not, and the difference is where the
    effect lives. A stalled ``stat_file`` is the backend's control plane hanging — cancelling
    it costs nothing and is the only thing standing between one slow request and a supervisor
    that never returns. A host tool has already begun acting in this process; see
    :func:`dispatch_over_exec` on why that one is left alone.
    """
    remaining = max(0.0, deadline - time.monotonic())
    bound = asyncio.timeout(remaining)
    try:
        async with bound:
            return await call
    except TimeoutError as expired:
        if not bound.expired():
            # A backend's own bound, not this one — acas bounds a read because a guest can
            # plant a FIFO the service reports as a regular file and never serves, and that
            # sentence is the only one saying what happened. Asked of `asyncio` rather than
            # of the clock: a timer may fire up to one clock resolution early, and on a
            # platform where that is 16ms a second reading calls this run's own expiry a
            # backend's, sending it past the handler that knows what to do with it.
            raise
        raise _DeadlineExpired(
            f"the sandbox did not answer {what} within the {remaining:.1f}s it was given"
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
    except SandboxTransferCapExceeded as refused:
        # The backend refusing after the fact, which the pull surface says is how a client
        # that buffers the whole response has to refuse. The same outcome as an over-cap
        # stat, reached from the other end.
        logger.warning("host tools: %s is over the cap: %s", path, error_detail(refused))
        return _TooLarge()
    except FileNotFoundError:
        # The one genuine race: the stat found it and the read did not. Polling again is the
        # right answer, and the only failure for which it is.
        return None
    except Exception:
        # Everything else is the backend saying something is wrong — a permission error, a
        # client that cannot reach the daemon — and retrying it every interval turns a
        # permanent failure into a run that reports the guest as slow. It propagates, so the
        # caller is told what actually happened. `_final_output` catches broadly for the same
        # reason in reverse: a diagnostic must not replace the failure it is describing.
        logger.warning("host tools: reading %s failed and will not be retried", path)
        raise
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


def _output_clause(printed: str, note: str) -> str:
    """The output half of a timeout message — the program's words, or the host's reason.

    Never both, and never the second wearing the first's label: `Output so far:` is a promise
    that what follows is the program's own stdout, and "the output was larger than the host
    will read" is the host talking. A reader who cannot tell them apart is being told the
    program printed a sentence about itself.
    """
    return f"no output was read — {note}" if note else f"Output so far: {printed[:2000]}"


async def _final_output(
    sandbox: Sandbox, run: HostToolRun, layout: GuestRunLayout, until: float
) -> tuple[str, str]:
    """What the program printed, and separately the host's note — on a short allowance.

    Two values rather than one because they go to different places. The note ("output was
    larger than the host will read") is the *host* speaking, and a caller quoting it under
    "Output so far" hands a model host prose in the position its own stdout occupies — which
    is what the success path already avoids by routing the same note to ``stderr``.

    The run's deadline has passed by the time this is called, so reading under it would expire
    instantly and the message would carry nothing. This buys a couple of seconds for the
    diagnostic and answers with nothing rather than raising if even that is too long: the
    caller is already being told the run failed, and the reason must not become "reading the
    reason failed".

    ``until`` is shared with the last look for the exit marker that runs first, not an
    allowance of this function's own. A backend slow enough to spend it there leaves nothing
    to quote — the trade for charging a stalled one once rather than twice, and the marker is
    the half that can still save the run.
    """
    try:
        value = await _read_if_present(
            sandbox,
            layout,
            layout.output,
            cap=_output_cap(run),
            deadline=until,
        )
        # The note travels beside the text, not instead of it: a timed-out run whose output was
        # refused for its size must not quote emptiness at a reader who cannot tell that from a
        # silent program, and must not pass the note off as the program's own words either.
        return _as_text(value), _why_no_output(value)
    except Exception as failure:  # noqa: BLE001 — a diagnostic must not replace the failure
        # Not just `TimeoutError`: `stat_file` may raise anything a backend's client raises,
        # and this runs while a `TimeoutError` is being constructed. Losing the run's own
        # reason to a failure in reading the reason is the one outcome worth ruling out.
        logger.warning("host tools: could not read the program's output: %s", error_detail(failure))
        return "", ""


def _as_text(value: str | _TooLarge | _NotText | None) -> str:
    """The text, or nothing — an absent or over-cap file reads as no output rather than a type."""
    return value if isinstance(value, str) else ""


def _why_no_output(value: str | _TooLarge | _NotText | None) -> str:
    """The host's own note when output was dropped rather than absent, and "" when it was not.

    An empty ``stdout`` beside exit code 0 says the program printed nothing, and for a program
    whose output was refused for its size that is a false report of a successful run — the one
    a caller cannot tell from the real thing. It goes in ``stderr`` because on this transport
    that field is the host's: the launcher merges the guest's own stderr into the output file,
    so nothing else ever writes there.

    What the ceiling should be, and whether a large output should be truncated rather than
    dropped, is #354. This is only the part that must not stay silent either way.
    """
    if isinstance(value, _TooLarge):
        return (
            "the program's output was larger than the host will read and was not returned; "
            "have the program write what it needs to a file the run collects instead"
        )
    return ""


def _serving_bound(run: HostToolRun) -> int:
    """How many requests this supervisor will read before it stops reading them.

    Every request, not every dispatch: a malformed one is answered before the door and so
    never spends the cap that is meant to bound this. One more than the cap because the
    transport serves one outstanding call at a time, which makes a single refusal enough to
    tell the guest the cap is gone. Past it the supervisor only waits for the program to end.
    """
    return run.registry.max_dispatches_per_run + 1


def _request_cap(run: HostToolRun) -> int:
    """The request ceiling, borrowed from the response one — the same concern, one vocabulary.

    The per-file leg only. What every request together may cost is this times
    :func:`_serving_bound`, both configured, and *not* the same ``TransferLimits``' total leg:
    that budget is spent by responses, and sharing it would mean tightening what a tool may
    return quietly limits how many calls a guest may make.
    """
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
