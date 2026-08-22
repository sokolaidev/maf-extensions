"""Generating the guest-side module a program imports to reach the host.

The transport in :mod:`maf_sandbox._host_tools_over_exec` supervises the calls; this builds the
source the guest runs. :func:`host_tool_shim` reads :mod:`maf_sandbox._guest.maf_host_tools`
verbatim, appends the caller's patience as a ``_TIMEOUT`` override and one convenience wrapper
per tool, and returns the whole as a string a kind writes beside the program.

**The shim is not a control.** It runs in the guest, where model-written code can read, edit or
ignore it; every gate that matters is host-side in :meth:`~maf_sandbox.HostToolRun.dispatch`.
The wrappers grant nothing — resolution is against the registry, so a name this cannot spell as
a function is still callable through :func:`call`, and a name it invents still resolves to a
refusal.
"""

from __future__ import annotations

import functools
import importlib.resources
import keyword
import math
import symtable
import unicodedata

from ._host_tools_over_exec import SHIM_MODULE

#: How long the guest blocks on one call before giving up on the host, and how often it looks.
#: Bounded on both sides. It has to outlast the host's poll interval by a wide margin or a slow
#: supervisor reads as a dead one, and it must not be shorter than the run's own bound or the
#: guest gives up on a dispatch that then goes on to act. The host's deadline is what actually
#: ends a run; this is only the guest's patience, and :func:`host_tool_shim` takes it as an
#: argument for a caller whose runs are longer than this. These are the canonical values the
#: guest source carries as literals (``_TIMEOUT`` as its overridable default); a test pins that
#: file to them, and its ``_CALLS`` to :data:`~maf_sandbox.CALLS_DIRECTORY`, so the two cannot
#: drift.
_GUEST_CALL_TIMEOUT = 300.0
_GUEST_POLL_SECONDS = 0.05


@functools.cache
def _guest_source() -> str:
    """The guest shim's source, read once from the package rather than imported.

    A real module under :mod:`maf_sandbox._guest`, so ruff and pyright see it and a test can
    import it; read as text here because it is guest code, meant to run there and not in this
    process.
    """
    return (
        importlib.resources.files("maf_sandbox._guest")
        .joinpath("maf_host_tools.py")
        .read_text(encoding="utf-8")
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
    # The source ships a default `_TIMEOUT`; this override is a later module-level binding, and
    # `call` reads `_TIMEOUT` at call time, so the last assignment wins without the source
    # needing a placeholder to substitute. Rendered with `str` (not `repr`), matching the
    # `str.format` this replaced: on a stray non-float that still passes the check above — a
    # `Fraction`, a numpy scalar — `repr` would emit a name the guest cannot resolve
    # (`Fraction(1, 2)`, `np.float64(300.0)`) where `str` emits a valid literal (`1/2`, `300.0`).
    # The wrappers follow, as they always have.
    return (
        _guest_source() + f"\n_TIMEOUT = {call_timeout}\n" + (f"\n{wrappers}\n" if wrappers else "")
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
    scope = symtable.symtable(_guest_source(), SHIM_MODULE, "exec")
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
