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
- **Cleanup is this transport's own doing, because the protocol offers none.** Nothing in the
  vocabulary deletes, so the files a run leaves would sit in the guest until the sandbox was
  disposed of — readable by every later run in it, since ``acquire`` is get-or-create. What
  removes them is ``rm`` over the ``exec`` the dispatch path already requires, which works
  here and is not a general answer: a guest without a POSIX shell has no cleanup at all
  (#438). :func:`dispatch_over_exec` reclaims the directory it owns on every exit path;
  :attr:`GuestRunLayout.work` is the caller's, because artifacts are collected after this
  returns, and :func:`reclaim_run` is what a kind calls for it. A fresh per-run directory is
  still required — it is what keeps one run's traffic out of the next one's while both are
  live, and it is the caller's to provide.
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
from typing import TYPE_CHECKING, Any, Literal, cast

from ._error_detail import error_detail
from ._outputs import SandboxTransferCapExceeded
from ._protocol import EntryKind, ExecResult
from .paths import confine_guest_path, guest_path_relative_to

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

#: Where the launcher records the program's process id, so a run that overruns can be stopped
#: rather than left going. Staged and renamed like the exit marker and for the same reason: a
#: reader must never see the empty file a redirection leaves for a moment.
PID_FILE = "program_pid"
_STAGED_PID_FILE = f"{PID_FILE}.part"

#: Where the launcher records the id of the session it made for the program, when the guest
#: has ``setsid``. Its own file rather than a flag on the pid, because the two answer different
#: questions — which process to signal, and whether signalling its group is a thing this run
#: may do at all. Absent means the program shares the launcher's session, where a group signal
#: would reach far more than the run.
SESSION_FILE = "program_session"
_STAGED_SESSION_FILE = f"{SESSION_FILE}.part"

#: What the launcher prints when it made a session, on its own stdout, which the ``exec``
#: that ran the launcher returns to the host.
#:
#: **Not ordering — redirection.** The session shell is backgrounded before this line runs,
#: so the program may already be going when the marker is printed; what keeps the two apart
#: is that the backgrounded command's stdout and stderr go to ``/dev/null`` and the
#: program's own output goes to its file, so nothing the guest writes can reach the stream
#: this arrives on. That is what makes it a fact about the guest rather than a claim by the
#: program: the session *file* is inside the run and writable, so on an image with no
#: ``setsid`` a program could otherwise plant one and have the host signal a group it never
#: made.
SESSION_MADE = "maf-host-tools: session"

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
    {
        SHIM_MODULE,
        _LAUNCHER,
        OUTPUT_FILE,
        EXIT_FILE,
        _STAGED_EXIT_FILE,
        PID_FILE,
        _STAGED_PID_FILE,
        SESSION_FILE,
        _STAGED_SESSION_FILE,
        CALLS_DIRECTORY,
    }
)

#: Module names a ``program`` may not be called, and why they are matched by **stem**.
#:
#: A file in this directory is importable, because the directory is on the interpreter's path
#: from startup — so what matters is the module name a file would answer to, not the spelling
#: of its suffix. ``FileFinder`` tries extension suffixes first, then source, then bytecode, so
#: ``json.cpython-313-x86_64-linux-gnu.so`` outranks ``json.py``; refusing exact filenames
#: would leave every one of those twins open. See :func:`_module_a_program_answers_to` for
#: which spellings reach a module and which, like ``json.backup.py``, reach nothing.
#:
#: :data:`_STARTUP_STEMS` is what CPython reaches on its way up, where a collision fails before
#: the program is ever the script: ``encodings`` takes the interpreter down with a
#: path-configuration dump, and ``sitecustomize`` and ``usercustomize`` are executed by
#: ``site``, so a program under either name runs twice. ``site`` itself is refused as the
#: module that imports the other two. The stdlib names below them are reached only on a guest
#: whose standard library is not frozen, and are refused everywhere rather than conditionally,
#: because the guest's version is not this package's to pin — ``interpreter`` defaults to
#: ``python3``, and what that is belongs to the image.
#:
#: Deliberately *not* "everything imported before the script runs", which is not a set anyone
#: can write down: a ``.pth`` file may import any name it likes, so an image with setuptools
#: reaches ``_distutils_hack``. A floor under the common failures, not a proof for any guest.
_STARTUP_STEMS = frozenset(
    {
        "encodings",
        "site",
        "sitecustomize",
        "usercustomize",
        # Reached from a file during startup before 3.11, where the stdlib is not frozen.
        "abc",
        "codecs",
        "genericpath",
        "io",
        "posixpath",
        "stat",
        "_collections_abc",
        "_sitebuiltins",
        # Reached the same way on 3.8 and 3.9, through the `TextIOWrapper` that `site` builds
        # to read a `.pth` file. Removed from the stdlib in 3.10.
        "_bootlocale",
    }
)

#: :data:`_SHIM_STEMS` is what the generated shim imports, and the collision is with the shim
#: rather than the interpreter: the two share a directory, so a program named ``json`` *is*
#: what the shim's own ``import json`` finds. The program's body runs a second time under that
#: name and every dispatch afterwards dies on an attribute the real module would have had —
#: a traceback that names ``dumps`` and never the collision.
#:
#: Only ``json`` is resolved from a directory on a current guest; ``os`` and ``time`` are
#: refused too, because which modules are frozen is a per-version detail rather than a
#: contract, and refusing a program name no kind wants costs nothing where missing one costs
#: a traceback that points elsewhere.
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


#: The one-dot suffixes some loader answers to, across the platforms a guest may run — the
#: union of ``SOURCE_SUFFIXES``, ``BYTECODE_SUFFIXES`` and ``EXTENSION_SUFFIXES`` on POSIX
#: (``py``, ``pyc``, ``so``) and on Windows (``py``, ``pyw``, ``pyc``, ``pyd``). Held here
#: rather than read from :mod:`importlib.machinery`, which would answer for *this* interpreter
#: on *this* platform and so refuse the wrong set for a guest that is neither. A union means
#: each platform refuses a little more than it has to — ``json.pyw`` is inert on POSIX — which
#: is the same direction of error as the tagged-extension rule below, and taken for the same
#: reason: the guest's platform is no more knowable here than its ABI tag.
_LOADER_SUFFIXES = frozenset({"py", "pyc", "pyw", "so", "pyd"})


def _module_a_program_answers_to(program: str) -> str | None:
    """The module name an import in this directory would reach ``program`` by, or ``None``.

    ``FileFinder`` looks for a *name plus one of its loaders' suffixes*, so `json.py`,
    `json.pyc` and every extension spelling of `json` answer to ``json`` — while `json.txt`
    and `json.backup.py` answer to nothing, the first because no loader claims `.txt` and the
    second because none claims `.backup.py`. A program under either can be run by path and
    never imported, so refusing it would be refusing a name that cannot collide.

    A suffix carrying an interior dot is always an extension one (`.abi3.so`,
    `.cpython-313-x86_64-linux-gnu.so`), and its tag belongs to the guest's interpreter, which
    is not this package's to pin. So anything ending `.so` or `.pyd` is read as a tagged
    extension and refused on the name in front of it — the one place this still answers more
    broadly than the collision, and the safe direction to be wrong in.
    """
    head, dot, suffix = program.partition(".")
    if not dot:
        # No suffix, so no loader claims it; the launcher runs it by path regardless.
        return None
    if "." in suffix:
        return head if suffix.endswith((".so", ".pyd")) else None
    return head if suffix in _LOADER_SUFFIXES else None


#: What :class:`SandboxProgramTimeout` reports about the program it was raised for. Public,
#: because acting on it is a caller's business and matching the message text is not an
#: interface. :data:`_Fate` is the same vocabulary, kept private only for the internal plumbing.
SignalOutcome = Literal["sent", "refused", "absent", "unrecorded", "unknown"]

#: What a sent signal reached. ``"sent"`` alone stopped being enough when a stop began
#: taking the program's children with it on some guests and not others: a host deciding
#: whether it still has to dispose the sandbox needs the difference, and matching the
#: message text is not an interface.
#:
#: ``"group"`` and not ``"session"``: ``kill`` signals the process group whose id is the
#: session leader's, which is where the program and what it spawns start out. A descendant
#: that calls ``setpgid`` leaves that group, stays in the session, and survives.
SignalReach = Literal["group", "program", "nothing"]


class SandboxProgramTimeout(TimeoutError):
    """The *run's own* bound expired: the guest program did not finish in the time it was given.

    A :class:`TimeoutError` subclass, so a caller that only wants "it timed out" is unaffected.
    Catch it specifically to distinguish the two things a ``TimeoutError`` from this transport
    can mean: **this** one is the run's budget; a bare one is a backend failing for a reason of
    its own and says nothing about the program.

    ``signal`` is what the transport managed to do about the program, and it is the thing to
    branch on — the message says the same in prose, but prose is not an interface:

    - ``"sent"`` — ``kill`` accepted the target the launcher recorded. Not a promise the
      program is gone: the kernel can discard the signal and the number is read from a file
      the program can rewrite. ``reach`` says how wide it went — ``"group"`` for the
      program's process group, ``"program"`` for a lone pid, which leaves its children.
    - ``"refused"`` — a pid was recorded and the signal did not land. Not evidence that a
      program is running: the same value covers a pid too malformed to aim at, a pid the host
      could not read, and a ``kill`` that reported no such process because the program had
      already exited. What it says is that this transport did not stop anything.
    - ``"absent"`` — the run ended before any launcher ran, so no program was started and
      none is running. It says nothing about *files*: a kind writes the program, the shim and
      the model's shared-in files into the run directory before dispatching, so
      :func:`reclaim_run` is owed here exactly as it is on every other outcome.
    - ``"unrecorded"`` — the launcher returned and no pid ever appeared. The launcher may have
      failed before publishing one, so this is not evidence of a running program either — it
      is the absence of any handle on whether there is one.
    - ``"unknown"`` — the host could not establish which of those it was: the pid could not be
      read, or the launcher's own call expired between starting the program and publishing its
      pid. Evidence of neither, and the only honest answer for that window.

    Only ``"absent"`` says a program was never started. **None of the others confirms one was
    stopped, and none confirms one is running** — not even ``"sent"``, for the reasons above.
    They are degrees of not knowing, so a host that needs termination rather than a best
    effort applies its own policy on top, and disposing the sandbox is what that policy has to
    reach for.

    ``signal`` defaults to ``"unknown"``, and deliberately: the exception is public, so code
    raising it for a transport of its own passes a message and no fate at all. The default
    answers for those, and ``"absent"`` — the one value a host may act on by walking away — is
    not a claim anything should make by omission.

    ``output`` is what the program had printed when the run was given up on, already capped —
    empty on the two starting legs, where there is nothing to have read yet. An attribute
    rather than message text, so a caller can surface the program's own stdout alone.
    """

    def __init__(
        self,
        message: str,
        *,
        output: str = "",
        signal: SignalOutcome = "unknown",
        reach: SignalReach = "nothing",
    ) -> None:
        super().__init__(message)
        self.output = output
        #: What the signal reached, where one was sent. ``"program"`` means the children
        #: it spawned are still running and disposal is the only thing that stops them.
        self.reach = reach
        self.signal = signal


class _TheRunsOwnTimeout(SandboxProgramTimeout):
    """The timeout this supervisor raised, told apart from one it merely caught.

    :class:`SandboxProgramTimeout` is public and documented as constructible elsewhere, so a
    :class:`~maf_sandbox.Sandbox` implementation may raise one from a call of its own. Deciding
    on the type alone would read that as this run's own bound — already stopped and reported —
    and the cleanup would skip the signal, then remove the pid that was the only handle on a
    program still going. Callers catch the public type and are never handed this name; it
    exists so the cleanup can tell whose timeout it is holding.
    """


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

#: What the cleanup gets, on top of a run's own budget. Its own grace for the reason the final
#: read has one: by the time it runs the remainder is zero on every path that matters, and a
#: cleanup given no time is no cleanup — which here means a run's files stay readable by the
#: next run in the same sandbox. Longer than the final read because a recursive delete of a
#: run that wrote a lot is slower than one stat.
_RECLAIM_GRACE = 10.0

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
    pid: str
    session: str = ""


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
    CPython imports at startup. The last two are matched by the **module the file would answer
    to**, since a suffix decides only which loader answers — ``json.py`` and ``json.abi3.so``
    are refused where ``json.backup.py``, which no loader claims, is not. See
    :data:`_SHIM_STEMS` and :data:`_STARTUP_STEMS` for what each collision does.

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
    stem = _module_a_program_answers_to(program)
    if stem == _SHIM_MODULE_STEM:
        raise ValueError(
            f"program must not answer to the shim's own module name, and {program!r} answers "
            f"to {stem!r}: the name is reserved for the shim, because a file here under it "
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
        pid=posixpath.join(served, PID_FILE),
        session=posixpath.join(served, SESSION_FILE),
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
    returns immediately and leaves the program's output, its exit code in a file whose
    appearance tells the supervisor the run is over, its pid, and — where the guest has
    ``setsid`` — the id of the session it put the program in, which is what lets a run that
    overruns take the program's children with it.

    POSIX shell, and a guest that has ``nohup``. ``setsid`` is used when present and done
    without when not. A Windows guest or a distroless image needs a different launcher; that
    is a backend's business, and this one is a helper rather than a protocol.
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
    #   * The interpreter runs in the background so `$!` is the program's own pid and not the
    #     wrapper shell's; `wait $!` restores it to the foreground, which is what keeps `$?`
    #     the program's status. The pid lands by rename for the reason the exit code does.
    #   * `mkdir -p` because a run whose kind shared no files has nothing else to create the
    #     work directory, guarded because `sh` does not stop on a failed command: an unguarded
    #     `cd` leaves the program running wherever the launcher was exec'd, writing artifacts
    #     where nothing collects them and exiting 0. A non-zero launcher is already reported.
    #   * The program runs *in* the work directory and *from* the transport's, with the
    #     transport's on `PYTHONPATH` besides — `sys.path[0]` follows the script, but that is
    #     a default `PYTHONSAFEPATH` switches off, and then the path is all there is.
    #   * **An inherited path entry is kept only if it is absolute, canonical, and outside the
    #     run tree**, and `PYTHONNOUSERSITE` goes with it, because `site` reaches
    #     `$PYTHONUSERBASE/lib/pythonX.Y/site-packages` without consulting the path at all.
    #     Together they keep the guest's own files off *interpreter startup*, where a
    #     `sitecustomize` a model wrote runs before the program and seeds `sys.modules` with a
    #     shim of its own. Each test earns its place: relative resolves against the directory
    #     this launcher just changed to; absolute can still name a run, where a host places
    #     them predictably; and the comparison is textual, so `/runs/./current/work` would pass
    #     a prefix test and reach the same directory. The user base is switched off rather than
    #     filtered because filtering it leaves the hole behind `HOME`, which it falls back to.
    #     A symlink into the tree needs a `realpath` POSIX `sh` does not have and is not
    #     caught; `PYTHONHOME` pointed here breaks the guest outright rather than substituting
    #     anything, so it is left alone. The README's upgrade note has what this costs an image.
    staged = f"{layout.exit_code}.part"
    staged_pid = f"{layout.pid}.part"
    staged_session = f"{layout.session}.part"
    # The shim's own directory, which is the one an import has to reach; `program` is beside
    # it by construction, and reading it from the shim keeps the two from being separated.
    importable = posixpath.dirname(layout.shim)
    # Quoting is what stops a run directory containing `*` or `?` from matching more than
    # itself; stripping the separator keeps the two patterns below from becoming `//*`, and
    # collapses a run directory of `/` to `''|/*`, dropping every absolute entry.
    enclosing = _quote(layout.directory.rstrip("/"))
    # `$$` from inside the shell `setsid` runs, not the outer `$!`: `setsid` execs in place or
    # forks depending on its caller, and only the process it ends up exec'ing leads the
    # session either way.
    # Guarded like every other reader of this field: `GuestRunLayout` defaults it to empty for
    # a kind that predates it, and an unguarded `mv` would put a stray `.part` in the model's
    # own work directory — the one a kind collects from — and fail into a discarded stderr.
    record_session = (
        (
            f"printf %s $$ > {_quote(staged_session)}; "
            f"mv {_quote(staged_session)} {_quote(layout.session)}; "
        )
        if layout.session
        else ""
    )
    inner = (
        f"PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 {_quote(interpreter)} {_quote(layout.program)} "
        f"> {_quote(layout.output)} 2>&1 & "
        f"printf %s $! > {_quote(staged_pid)}; mv {_quote(staged_pid)} {_quote(layout.pid)}; "
        f"wait $!; "
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
        '  case "$maf_entry" in\n'
        # Both dropping branches come before the keeping one, because `case` takes the first
        # match: demote either and `/*` claims the entry first and the test never runs.
        # Non-canonical first of all — it is the one that decides whether the next test is
        # comparing spellings or directories.
        "    */./*|*/../*|*//*|*/.|*/..) ;;\n"
        f"    {enclosing}|{enclosing}/*) ;;\n"
        '    /*) maf_kept="${maf_kept:+$maf_kept:}$maf_entry" ;;\n'
        "  esac\n"
        "done\n"
        "unset IFS\n"
        "set +f\n"
        f'PYTHONPATH={_quote(importable)}"${{maf_kept:+:$maf_kept}}"\n'
        "export PYTHONPATH\n"
        # Prefixed, then removed: a bare `kept` or `entry` collides with an image that exports
        # one, and an exported name stays exported — the guest would read the launcher's
        # leftovers where its own value belongs.
        "unset maf_kept maf_entry\n"
        # Two paths, because the better one needs a utility not every image has. With
        # `setsid` the shell it runs leads a session of its own, and the program starts
        # inside that one, so the host can stop it and its children together; without, the
        # program shares the launcher's session, where a group signal reaches the container. The session file is written only on the first path, and its absence is
        # what tells the host which one ran — a claim that varies by image, reported rather
        # than hidden.
        "if command -v setsid >/dev/null 2>&1; then\n"
        f"  setsid nohup sh -c {_quote(record_session + inner)} >/dev/null 2>&1 &\n"
        # On this branch only, and on the launcher's own stdout — which the guest cannot
        # reach, because the command above sends its own to `/dev/null`.
        f"  printf '%s\\n' {_quote(SESSION_MADE)}\n"
        "else\n"
        f"  nohup sh -c {_quote(inner)} >/dev/null 2>&1 &\n"
        "fi\n"
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

    Attempts to remove the transport's own directory before returning, on every exit path.
    A removal the guest refuses is logged and nothing else — the call still returns what it
    was going to — so those files stay readable by later runs in the same sandbox.
    :attr:`GuestRunLayout.work` is left for the caller, which collects artifacts from it after
    this returns and reclaims it with :func:`reclaim_run`.

    Returns:
        The program's own :class:`~maf_sandbox.ExecResult` — its redirected output as
        ``stdout`` and the exit code it recorded.

    Raises:
        SandboxProgramTimeout: The run's own bound expired. Where the program had started,
            its output up to that point is in the message and on ``output``, and the program
            has been *sent* a ``SIGKILL`` where one could be sent — by pid, over ``exec``,
            which is why no capability beyond the ones this transport already needs is
            involved. Neither half is a guarantee the program is gone: the signal may not have
            been sent at all (no pid was recorded, or the ``exec`` carrying it failed), and a
            sent one may be discarded by the kernel or aimed at a number the program
            rewrote, and ``reach`` says how wide it went: ``"group"`` took the program's
            process group with it, ``"program"`` reached one pid and left anything it
            spawned running. The message says which of the two happened, and disposing
            of the sandbox is what stops what remains. On the two starting legs, the launcher upload and the ``exec`` that
            runs it, ``output`` is empty instead: the output file does not exist yet, and on a
            backend that began the command before its own call returned there may be output
            nobody read — so the kill is attempted on the second of those legs too.
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
    # Validated before the wrapper, so a bad argument raises plainly. The run directory is
    # then left alone, which is the right way round: the caller has already written the
    # program and the shim into it and has not yet been told the call is going nowhere.
    handled = False
    launcher = _WhatTheLauncherSaid()
    try:
        result = await _supervise(
            sandbox,
            run,
            layout,
            timeout=timeout,
            poll_interval=poll_interval,
            interpreter=interpreter,
            launcher=launcher,
        )
        handled = True
        return result
    except _TheRunsOwnTimeout:
        # This run's own bound, which `_supervise` has already stopped the program for and
        # reported. Deliberately not the public type: a backend raising one of those is a
        # program nobody stopped, and it belongs on the path below with every other failure.
        handled = True
        raise
    finally:
        # Every exit path: the value returned, the timeout raised, and whatever a backend
        # raised for reasons of its own. A successful run leaves exactly as much behind as
        # a failed one, and it is the common case.
        #
        if not handled:
            # `_supervise` reports its own timeouts and stops the program itself. Anything else
            # leaving this function — a backend failing mid-run — leaves a detached program
            # nobody has stopped, and the reclaim below is about to remove the files that would
            # have identified it.
            if await _marker_if_present(sandbox, layout, _a_grace_from_now()) is None:
                await _stop_the_program(
                    sandbox,
                    layout,
                    until=_a_grace_from_now(),
                    made_a_session=launcher.made_a_session,
                )
        await _reclaim_the_transports_own(sandbox, layout, until=time.monotonic() + _RECLAIM_GRACE)


@dataclass
class _WhatTheLauncherSaid:
    """Filled in once the launcher has run, read by the cleanup that outlives it.

    `dispatch_over_exec` stops the program itself when anything other than a timeout leaves
    the supervisor, and that path has no launcher result of its own — so without this it
    signalled one pid on a guest where the whole group was available, and then reclaimed the
    files that would have identified the rest.
    """

    made_a_session: bool = False


async def _supervise(
    sandbox: Sandbox,
    run: HostToolRun,
    layout: GuestRunLayout,
    *,
    timeout: float,
    poll_interval: float,
    interpreter: str,
    launcher: _WhatTheLauncherSaid,
) -> ExecResult:
    """The body of :func:`dispatch_over_exec`, minus the cleanup that wraps it."""
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
        raise _TheRunsOwnTimeout(
            f"the run's {timeout:g}s were gone before the program was started — {gone}",
            # Explicit, though it is also what a bare construction would say least of: this is
            # the one leg that never ran a launcher, so it is the only one entitled to claim
            # that nothing was started.
            signal="absent",
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
        # The launcher backgrounds the program and returns, so an `exec` that ran out may
        # still have started one, and this leg is reachable only once the launcher had time to
        # run — so the marker is read before anything is concluded, which is also the order
        # `_stop_the_program` requires.
        landed = await _marker_if_present(sandbox, layout, _a_grace_from_now())
        if landed is not None:
            # The marker is proof the program finished, so what ran out was the reply and not
            # the run. Raising here would discard an exit code already in hand; the supervisor
            # loop makes the same recovery when a call runs out on top of a landed marker.
            logger.warning(
                "host tools: the run finished, but the call that started it had already run "
                "out: %s",
                error_detail(spent),
            )
            return await _completed(sandbox, run, layout, landed, deadline)
        # `exec` was handed this run's remainder, so its expiry is this run's — every
        # `TimeoutError` from it, without re-reading a clock that may be a resolution behind
        # the timer that fired. The backend's own text stays in the log for the same reason it
        # does in `_completed`: this message reaches a model through whichever kind is running.
        logger.warning(
            "host tools: the run ran out while starting the program: %s", error_detail(spent)
        )
        # A grace of its own, measured after the marker read rather than shared with it.
        fate, reach = await _stop_the_program(sandbox, layout, until=_a_grace_from_now())
        fate = _nothing_is_proven(fate)
        raise _TheRunsOwnTimeout(
            f"the run's {timeout:g}s were gone while starting the program"
            f"{_clause_while_starting(fate, reach)}",
            signal=fate,
            reach=reach,
        ) from spent
    if started.exit_code != 0:
        return ExecResult(
            stdout=started.stdout,
            stderr=started.stderr or "the launcher did not start the program",
            exit_code=started.exit_code,
        )

    # Read from the launcher's own output, not from a file in the run: the program can write
    # the session file whether or not a session was made, and on a guest without `setsid` the
    # group it would then name is the launcher's own — the whole container.
    made_a_session = SESSION_MADE in started.stdout
    launcher.made_a_session = made_a_session

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
            # Output first, so the program's own words are off the guest before it dies — but
            # on a fresh grace, because the reads above can spend `giving_up` entirely and a
            # kill with nothing left to spend is the runaway this path exists to stop.
            fate, reach = await _stop_the_program(
                sandbox, layout, until=_a_grace_from_now(), made_a_session=made_a_session
            )
            fate = _started_something(fate)
            raise _TheRunsOwnTimeout(
                f"the guest program did not finish within {timeout:g}s"
                f"{_clause_after_the_launcher_started(fate, reach)}. "
                f"{_output_clause(printed, note)}",
                output=printed[:2000],
                signal=fate,
                reach=reach,
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
            fate, reach = await _stop_the_program(
                sandbox, layout, until=_a_grace_from_now(), made_a_session=made_a_session
            )
            fate = _started_something(fate)
            failure = f"{stalled}{_clause_after_the_launcher_started(fate, reach)}"
            raise _TheRunsOwnTimeout(
                f"the guest program did not finish within {timeout:g}s — {failure}. "
                f"{_output_clause(printed, note)}",
                output=printed[:2000],
                signal=fate,
                reach=reach,
            ) from stalled
        # Clamped: an unclamped sleep overruns the deadline by a whole interval, so a 0.1s
        # bound with a 10s interval would wait ten seconds to notice it had passed.
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


#: What :func:`_stop_the_program` did — not what became of the program, which this host cannot
#: see. ``SIGKILL`` is accepted for a process that then survives it: a pid the guest chose, or
#: pid 1 in the guest's own namespace, both make ``kill`` exit 0 while the target lives. So the
#: strongest of these means *signalled*, and the docs say what that is and is not worth.
#:
#: The three that are not ``"sent"`` are kept apart because one leg reports them
#: differently. ``"absent"`` is *no pid file at all*, so nothing was started; ``"refused"`` is a
#: pid that exists and could not be used or signalled, so something was; ``"unknown"`` is a host
#: that could not even look, which is evidence of neither. Collapsing the last two would have a
#: backend hiccup assert that a program is running.
_Fate = SignalOutcome

#: The private spelling of :data:`SignalReach`, for the plumbing that carries it.
_Reach = SignalReach

#: Said once, where a program is known to have been started and could not be stopped. Silence
#: when the kill worked, because a stopped program is what a timeout is supposed to mean and a
#: caller reading "and was stopped" on every expiry learns nothing from it.
#:
#: Three, because the honest claim differs: a session takes what the program spawned with
#: it, a lone pid leaves those running, and a signal that could not be sent leaves
#: everything. A kill that worked is no longer silent — which of the first two it was is
#: the thing a caller has to know.
_SIGNALLED_GROUP = " and its process group was sent SIGKILL"
_SIGNALLED_ALONE = (
    " and was sent SIGKILL, which reaches it alone — anything it spawned is still running"
)
_NOT_SIGNALLED = " and could not be signalled, so it may still be running"


def _sent_clause(reach: _Reach) -> str:
    """What a sent signal actually reached."""
    return _SIGNALLED_GROUP if reach == "group" else _SIGNALLED_ALONE


def _removable(directory: str) -> bool:
    """Is ``directory`` specific enough to hand to ``rm -rf``?

    The paths here come from :func:`guest_run_layout`, which already refuses a relative one —
    so this is not the primary defence, it is the one that still holds if a caller builds a
    layout by hand. An irreversible recursive delete gets a guard that does not depend on
    something else having run.

    Two components at minimum, because ``/`` and ``/tmp`` are the shapes that turn a cleanup
    into an outage, and no run directory this transport is given looks like either.
    """
    if not posixpath.isabs(directory) or ".." in directory.split("/"):
        return False
    # Counted on the normalised path: `/tmp/.` is two components as written and one as meant,
    # and the guard has to answer for what the directory *is*.
    return len([part for part in posixpath.normpath(directory).split("/") if part]) >= 2


async def _remove_tree(
    sandbox: Sandbox, directory: str, *, until: float, inside: str | None = None
) -> bool:
    """Delete ``directory`` and everything under it — never a raise.

    There is no delete in the protocol and this needs none: on a POSIX guest the ``EXEC`` the
    dispatch path already requires is one.

    ``-f`` is what makes an already-gone directory a success; the status is otherwise the
    guest's own, because a refused removal has to reach the caller as one.
    """
    if inside is not None and guest_path_relative_to(directory, inside) is None:
        logger.warning("host tools: refusing to remove %r — it is not inside %r", directory, inside)
        return False
    if not _removable(directory):
        logger.warning("host tools: refusing to remove %r — it is not a run directory", directory)
        return False
    try:
        removed = await _within(
            until,
            "the cleanup",
            sandbox.exec(
                f"rm -rf {_quote(directory)}",
                working_directory=posixpath.dirname(directory) or "/",
                timeout=max(0.0, until - time.monotonic()),
            ),
        )
    except Exception as refused:  # noqa: BLE001 — an unreclaimed run is a leak, not a fault
        logger.warning("host tools: could not remove %s: %s", directory, error_detail(refused))
        return False
    if removed.exit_code != 0:
        logger.warning(
            "host tools: the guest refused to remove %s: exit %d", directory, removed.exit_code
        )
        return False
    return True


async def reclaim_run(sandbox: Sandbox, layout: GuestRunLayout, *, timeout: float = 30.0) -> bool:
    """Remove a whole run — the transport's files and the model's alike.

    **A kind's to call in a ``finally``, after collecting anything it means to keep.** The
    ordering is the trap: artifacts live in :attr:`GuestRunLayout.work`, so a caller that
    reclaims before collecting loses them, and :func:`dispatch_over_exec` cannot do this itself
    for the same reason.

    Raises:
        ValueError: when ``timeout`` is not a finite positive number of seconds. An infinite one
            reaches the backend's own ``exec`` bound, where it means this never returns. Checked
            before the layout, so a bad argument is never answered as a refusal instead.

    Returns:
        Whether the ``rm`` succeeded — the guest's own status for one command, not a promise
        the directory stays gone. A stop reaches the program's process group at most — a
        descendant that left it, or any program on a guest without `setsid`, outlives one and
        can write a path back into existence after the removal returns.
        ``False`` is the load-bearing answer: a data-retention failure rather than a tidiness
        one — nothing in the protocol deletes and ``acquire`` is get-or-create, so what is left
        stays readable by every later run in this sandbox — and the caller is expected to
        escalate, which means disposing the sandbox. ``True`` narrows the window rather than
        closing it, and a caller that needs the data provably gone disposes either way.
    """
    # Before the layout, because this one is the caller's own mistake and the check below
    # answers `False` — which a caller is told to escalate as a data-retention failure. Reached
    # in the other order, a bad `timeout` reports as one of those instead of as itself.
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a finite positive number of seconds, not {timeout}")
    strays = [
        path
        for path in (
            layout.work,
            layout.program,
            layout.shim,
            layout.launcher,
            layout.calls,
            layout.output,
            layout.exit_code,
            layout.pid,
            *([layout.session] if layout.session else []),
        )
        if guest_path_relative_to(path, layout.directory) is None
    ]
    if strays:
        # Removing the run directory reclaims a run only if the run is inside it. A layout that
        # puts `work` elsewhere would have this delete something and answer ``True``, while the
        # model's files stayed readable and the caller was told there was nothing to escalate.
        logger.warning(
            "host tools: refusing to reclaim %r — this layout puts %s outside it",
            layout.directory,
            ", ".join(sorted(strays)),
        )
        return False
    return await _remove_tree(sandbox, layout.directory, until=time.monotonic() + timeout)


def _nothing_is_proven(fate: _Fate) -> _Fate:
    """`absent` on the leg where the launcher's own ``exec`` ran out proves nothing.

    The launcher backgrounds the interpreter and publishes the pid on the line after, so a call
    that expires between the two leaves a program running and no pid to show for it. Only the
    upload leg — which never ran a launcher at all — may say nothing was started.
    """
    return "unknown" if fate == "absent" else fate


def _started_something(fate: _Fate) -> _Fate:
    """`absent` on a leg where the launcher returned 0 means the pid never appeared.

    `_stop_the_program` sees a missing pid file and cannot know which of those it is; the two
    legs inside the supervisor loop can, because reaching them at all means the launcher exited
    cleanly. Reported as one value, a caller could neither ignore it safely nor escalate on it
    safely.
    """
    return "unrecorded" if fate == "absent" else fate


def _clause_after_the_launcher_started(fate: _Fate, reach: _Reach) -> str:
    """For the two legs inside the supervisor loop, where the launcher returned 0.

    Anything short of a signal hedges rather than staying quiet: the launcher returned 0, so
    something started, and between a needless disposal and a silent leak this errs towards the
    disposal.
    """
    return _sent_clause(reach) if fate == "sent" else _NOT_SIGNALLED


def _clause_while_starting(fate: _Fate, reach: _Reach) -> str:
    """For the leg where the launcher's own ``exec`` ran out.

    The launcher backgrounds the interpreter and writes the pid down on the line after, so a
    call that expires between the two leaves a program running and no pid to point at. A
    missing pid is therefore not evidence of a missing program, and neither is a pid the host
    failed to read: both hedge rather than claim. ``"absent"`` cannot reach here — the caller
    maps it through :func:`_nothing_is_proven` — and would earn the same hedge if it did.
    """
    if fate == "sent":
        return f" (it had started the program{_sent_clause(reach)})"
    if fate in ("absent", "unknown"):
        return " (whether it got as far as starting one could not be established)"
    return f" (it had started the program{_NOT_SIGNALLED})"


async def _reclaim_the_transports_own(
    sandbox: Sandbox, layout: GuestRunLayout, *, until: float
) -> None:
    """Remove the sibling directory this transport owns — never a raise.

    Not the run — see :func:`reclaim_run` for the other half. What goes is this side of the
    split, including every request and response the run exchanged with the host.
    """
    # Normalised, because every comparison below decides what a recursive delete gets and
    # `GuestRunLayout` validates nothing: `/runs/one` and `/runs/one/` are one directory, and
    # a check that reads them as two answers for the spelling rather than the target.
    served = posixpath.normpath(posixpath.dirname(layout.shim))
    scattered = [
        path
        for path in (
            layout.program,
            layout.launcher,
            layout.calls,
            layout.output,
            layout.exit_code,
            layout.pid,
            *([layout.session] if layout.session else []),
        )
        if posixpath.normpath(posixpath.dirname(path)) != served
    ]
    if scattered:
        # The directory to delete is inferred from one field, so the others have to agree with
        # it. A layout that spreads them keeps files this owns while removing a directory that
        # holds something else — both halves wrong, and silently.
        logger.warning(
            "host tools: refusing to remove %r — this layout puts %s outside it",
            served,
            ", ".join(sorted(scattered)),
        )
        return
    overlaps = (
        guest_path_relative_to(served, layout.work) is not None
        or guest_path_relative_to(layout.work, served) is not None
    )
    if overlaps or guest_path_relative_to(served, layout.directory) == "":
        # Confining to the run is not enough on its own, and neither is one direction: `work`
        # inside the transport directory is deleted with it just as surely as the other way
        # round. Either overlap would have this remove the model's files and every artifact a
        # kind is about to collect — on success, where nothing else would look wrong. The run
        # itself is caught through the helper rather than by `==`, which answers "not the run"
        # to every path that spells it differently.
        logger.warning(
            "host tools: refusing to remove %r — it is the run itself or holds the model's "
            "files, so this layout does not separate the two directories",
            served,
        )
        return
    if not await _remove_tree(sandbox, served, until=until, inside=layout.directory):
        logger.warning(
            "host tools: run %s kept its transport files, which hold this run's host-tool "
            "traffic; they are readable by the next run in this sandbox until it is disposed",
            layout.directory,
        )


def _a_grace_from_now() -> float:
    """A deadline for stopping a run, independent of what the diagnostics have spent."""
    return time.monotonic() + _FINAL_READ_GRACE


async def _stop_the_program(
    sandbox: Sandbox, layout: GuestRunLayout, *, until: float, made_a_session: bool = False
) -> tuple[_Fate, _Reach]:
    """``SIGKILL`` the program — its process group where there is one — and say what went.

    **Only call this once the exit marker is absent.** That narrows the window in which the
    number has already been recycled and does not close it — and the number is now a
    session, so a recycled one takes out a later run's whole process group rather than one
    stranger process. What a caller may conclude is that a signal was sent to that number.

    Where the launcher had ``setsid`` it recorded a session, and the signal goes to the process
    group the program starts in — so what it spawned goes too, unless a descendant left that
    group. Where it did not, only the program's own pid can be signalled: it shares the
    launcher's session, where a group signal would reach the whole container.

    **Sending the signal is not seeing it work.** Both numbers come from files the program can
    write, and ``kill`` reports success for a signal the kernel accepts and discards, so a
    guest naming another process survives a call that returns ``"sent"``.

    Returns:
        The outcome, and what it reached: ``"group"`` for the program's process group,
        ``"program"`` for a lone pid, ``"nothing"`` otherwise.
    """
    try:
        # Stat first, because `_read_if_present` answers `None` for a missing entry, an empty
        # one and a directory alike — and only the first of those means no program was
        # recorded. A guest can make the other two, so collapsing them would be an opt-out
        # from the signal and, on one leg, from being mentioned at all.
        recorded = await _within(
            until,
            "stat the pid",
            sandbox.stat_file(layout.pid, working_directory=layout.directory),
        )
    except Exception as unstattable:  # noqa: BLE001 — a kill must not replace the timeout
        logger.warning(
            "host tools: could not look for the program's pid: %s", error_detail(unstattable)
        )
        return "unknown", "nothing"
    if recorded is None:
        return "absent", "nothing"
    try:
        running = await _read_if_present(sandbox, layout, layout.pid, cap=32, deadline=until)
    except Exception as unreadable:  # noqa: BLE001 — a kill must not replace the timeout
        # The same rule `_marker_if_present` follows, and for the same reason: this runs only
        # once the run is already being reported as expired, so a backend failing here means
        # the program could not be stopped, never that the caller loses the run's own reason.
        logger.warning("host tools: could not read the program's pid: %s", error_detail(unreadable))
        # Not `absent`: the pid could not be *read*, which is no evidence that none was written.
        # One leg reports `absent` by saying nothing at all, and silence is the wrong answer to
        # a program whose fate is unknown.
        return "refused", "nothing"
    pid = running.strip() if isinstance(running, str) else ""
    # ASCII digits and positive, both load-bearing. `str.isdigit` admits other numeral systems
    # that `int` then normalises, and `kill -KILL 0` signals the whole process group rather
    # than one program — and the file is guest-writable, so its contents are not this host's
    # to trust. A real `$!` is always a positive ASCII integer. An oversized read arrives here
    # as a sentinel rather than a string, and is unusable in the same way.
    if not (pid.isascii() and pid.isdigit()) or int(pid) <= 0:
        return "refused", "nothing"
    target, reach = pid, "program"
    session = await _session_if_recorded(
        sandbox, layout, until=until, made_a_session=made_a_session
    )
    if session is not None:
        # Negative is `kill`'s own spelling for a process group. `> 1` and not `> 0`
        # because this argument is negated: `kill -KILL -1` signals every process the
        # caller may reach, which in a shared sandbox is the supervisor's own `exec` and
        # every other run in it. The pid form has no such neighbour and keeps its `> 0`.
        target, reach = f"-{session}", "group"
    # Its own deadline, not what is left of the read's: a slow pid read would otherwise leave
    # the signal no time to be sent, which is the runaway this exists to stop.
    sending = _a_grace_from_now()
    try:
        killed = await _within(
            sending,
            "the kill",
            sandbox.exec(
                # Only stderr is discarded. `kill` failing — a pid already gone, a signal
                # refused — has to reach the caller, which reports it as still running.
                f"kill -KILL {target} 2>/dev/null",
                working_directory=layout.directory,
                timeout=max(0.0, sending - time.monotonic()),
            ),
        )
    except Exception as refused:  # noqa: BLE001 — a failed kill is a leak, not a fault
        logger.warning("host tools: could not stop the guest program: %s", error_detail(refused))
        return "refused", "nothing"
    if killed.exit_code != 0:
        return "refused", "nothing"
    return "sent", reach


async def _session_if_recorded(
    sandbox: Sandbox, layout: GuestRunLayout, *, until: float, made_a_session: bool
) -> str | None:
    """The session the launcher made, or None where none may be assumed.

    Absent is the ordinary answer on a guest without `setsid`, so a failure to read it is
    treated the same way: the lone pid is still signalled, and a group signal is never
    sent on a guess.
    """
    # The file is inside the run, so the program can write one whether or not the launcher
    # did. On a guest without `setsid` the program shares the launcher's session, and a
    # planted file would have the host signal *that* group — the whole container, which is
    # the thing the fallback exists to avoid. So the branch is taken from what the launcher
    # printed on its own stdout, a stream the guest's own output is redirected away from.
    if not (made_a_session and layout.session):
        return None
    try:
        recorded = await _read_if_present(sandbox, layout, layout.session, cap=32, deadline=until)
    except Exception as unreadable:  # noqa: BLE001 — a kill must not replace the timeout
        logger.warning(
            "host tools: could not read the program's session: %s", error_detail(unreadable)
        )
        return None
    session = recorded.strip() if isinstance(recorded, str) else ""
    if not (session.isascii() and session.isdigit()) or int(session) <= 1:
        return None
    return session


async def _marker_if_present(
    sandbox: Sandbox, layout: GuestRunLayout, until: float
) -> str | _TooLarge | _NotText | None:
    """The exit marker within what is left of ``until``, or ``None`` — never a raise.

    Called once, on the way to reporting a run as unfinished, so it answers a question rather
    than adding a failure: whatever a backend raises here means the same as nothing being
    there, and the run's own reason is the one worth keeping.

    ``None`` is therefore "no marker was seen", never "no marker exists", and the callers that
    go on to :func:`_stop_the_program` are meant to act on it as it stands. A look that failed
    is evidence about the transport rather than about the guest, and withholding the signal
    for it would trade a kill that may be needless for a program nothing can find again — the
    reclaim removes the pid file on the way out. Absence actually observed is worth little
    more, for the reason :func:`_stop_the_program` gives: the pid is stat'd, read and only
    then signalled, so what the kill acts on is a stale answer either way.
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
