"""The host-tools safety contract: what may be dispatched from inside a sandbox, and how.

``HOST_TOOLS`` is the one capability where trust crosses *outward*.  A dispatched body runs
in the **host process**, with the host's authority, driven by model-written code, and its
call bypasses whatever middleware the host runs — the boundary sees only ``execute_code``'s
aggregate result.  The layered rationale lives in ``docs/design/two-axis-sandbox-policy.md``.

Three things a caller must know:

- :class:`HostToolRegistry` is the **one door**.  Nothing is dispatchable until it is
  registered, and :meth:`HostToolRegistry.resolve` is the only path in — which is what makes
  registration the only place a gate or a validation can honestly sit.
- **Least privilege comes from what a host registers, never from what it declares.**  A
  declaration reads like a control and is not one; see :class:`~maf_sandbox.Identity`.
- Everything a guest can see is a sanitized sentence, in the failure-ladder style of
  :mod:`maf_sandbox.maf`: a refusal ends the call, not the program, and provider detail goes
  to the host's log rather than into a transcript.

Declarations are carried claims; enforcement is the host's middleware.  A host without
``agent_framework.security`` still gets the registry, the warning, the gate and the denials —
and classifications ready the day it turns enforcement on.
"""

from __future__ import annotations

import inspect
import json
import logging
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from ._error_detail import error_detail
from ._protocol import (
    DEFAULT_TRANSFER_LIMITS,
    INTEGRITY_RANK,
    Identity,
    SourceIntegrity,
    TransferLimits,
)

#: Fallback for :class:`HostToolRun`'s ``logger`` argument — named apart from the usual
#: module-level ``logger`` because that argument is the point: a workload passes its own so
#: dispatch-failure records keep the workload's logger name (the convention `maf.py` set).
_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_DISPATCHES_PER_RUN",
    "DispatchResult",
    "FLOW_DECLARED_KEY",
    "HostToolAggregate",
    "HostToolDeclaration",
    "HostToolNotDeclared",
    "HostToolRegistry",
    "HostToolRun",
    "MafSandboxHostToolsWarning",
    "declaration_of",
    "sandbox_tool",
]


#: The attribute a stamped function carries its :class:`HostToolDeclaration` under — one
#: literal, one place.  The stamp distinguishes *considered* from *never considered*: it
#: exists only when every leg was answered, so a function carrying anything else there (a
#: hand-written partial dict, say) reads as unstamped rather than as partially declared.
FLOW_DECLARED_KEY = "__maf_sandbox_flow_declaration__"

#: How many dispatches one run may make, unless the host's registry says otherwise.  The cap
#: is the registry's (host policy, beside ``require_declared``) rather than the spec's: it
#: bounds what the *host* is willing to execute middleware-invisibly, not what a workload
#: needs.  Deliberately below :data:`~maf_sandbox.DEFAULT_TRANSFER_LIMITS`'s file count, so
#: with everything defaulted this cap is the one that bites first.
DEFAULT_MAX_DISPATCHES_PER_RUN = 16

#: What serving a USER-identity tool would need, named once — the refusal and the docs must
#: tell the same story.
_USER_IDENTITY_PREREQUISITES = (
    "per-run token minting, an audience-within-egress check, and an ephemeral exec env channel"
)


class MafSandboxHostToolsWarning(UserWarning):
    """Warning category for the one-time host-tools registration notice."""


class HostToolNotDeclared(ValueError):
    """A host tool has no complete information-flow declaration and the registry requires one.

    A host configuration error, raised where the host can fix it — at registration, or at
    aggregate build when a stamp was removed after registration.  The fix is
    :func:`sandbox_tool` with every leg answered; ``None`` is an answer.
    """


@dataclass(frozen=True)
class HostToolDeclaration:
    """One dispatched function's information-flow declaration — all three legs, answered.

    ``source`` is what the tool's output brings *in* (``None``: brings nothing external in);
    ``sink`` is the host-vocabulary confidentiality cap for what flows *out* through it
    (``None``: nothing conversation-derived flows out); ``identity`` is whose authority the
    body exercises (``None``: pure computation, no authority beyond returning a value).

    There are no defaults, deliberately: a declaration exists to prove every leg was
    considered, and a defaulted leg would be indistinguishable from a never-considered one.
    ``sink`` stays an opaque string in the host's own vocabulary — this package folds
    integrity, which it owns an ordering for, and never confidentiality, which it does not
    (see :meth:`HostToolRegistry.aggregate`).
    """

    source: SourceIntegrity | None
    sink: str | None
    identity: Identity | None

    def __post_init__(self) -> None:
        # The enum constructor is the refuse-unknown policy, exactly as at every other
        # deserialization boundary in this package.
        if self.source is not None:
            object.__setattr__(self, "source", SourceIntegrity(str(self.source)))
        if self.identity is not None:
            object.__setattr__(self, "identity", Identity(str(self.identity)))
        # Cast to `object` so the runtime gate stays: the annotation promises a str, and
        # the check exists exactly for the untyped host that did not keep the promise.
        declared_sink = cast(object, self.sink)
        if declared_sink is not None and not isinstance(declared_sink, str):
            raise TypeError(
                f"sink must be a string in the host's own confidentiality vocabulary, or None "
                f"for a tool nothing flows out through — not {type(self.sink).__name__}. This "
                "package never orders or folds confidentiality values, so it accepts them "
                "opaquely."
            )


_F = TypeVar("_F", bound=Callable[..., Any])


def sandbox_tool(
    *, source: SourceIntegrity | None, sink: str | None, identity: Identity | None
) -> Callable[[_F], _F]:
    """Stamp a function with its information-flow declaration, every leg answered explicitly.

    All three legs are keyword-only and have no defaults — calling this with any leg missing
    is a ``TypeError``, which is the design: the developer answers *is this a source, is this
    a sink, whose authority does it carry* before the function can be dispatch-declared at
    all.  ``None`` is an answer ("not that role"), never an omission.

    Returns the function unchanged apart from the stamp — no wrapper, so signatures,
    docstrings and ``inspect`` behavior stay exactly what the host wrote.
    """
    declaration = HostToolDeclaration(source=source, sink=sink, identity=identity)

    def stamp(func: _F) -> _F:
        setattr(func, FLOW_DECLARED_KEY, declaration)
        return func

    return stamp


def declaration_of(func: Callable[..., Any]) -> HostToolDeclaration | None:
    """The declaration ``func`` was stamped with, or ``None`` when it reads as unstamped.

    Anything under the stamp attribute that is not a whole :class:`HostToolDeclaration` —
    a hand-written dict answering two legs of three, say — is ``None`` here on purpose: the
    stamp means every leg was answered, and a partial answer is not a weaker declaration but
    no declaration at all.
    """
    value = getattr(func, FLOW_DECLARED_KEY, None)
    return value if isinstance(value, HostToolDeclaration) else None


@dataclass(frozen=True)
class HostToolAggregate:
    """What the registry's contents mean for the one model-facing ``execute_code`` tool.

    Derived per leg, over the relevant subset, never replacing the host's classification of
    ``execute_code`` itself as an exec sink under untrusted taint — refining it:

    - ``result_integrity`` is the weakest tier over *sources only* — a sink-only or pure tool
      must not drag the result to untrusted, and a registry with no sources has no integrity
      opinion at all (``None``): the workload's own default stands.
    - ``outbound_caps`` is every declared sink cap, verbatim and unfolded.  Confidentiality
      values are opaque host vocabulary with no ordering, and this repository requires an
      ordering to be data before anything ranks by it — so more than one distinct cap is the
      host's to reconcile, never this package's to guess between.
    - ``identities`` and ``requires_approval``: any :data:`~maf_sandbox.Identity.USER` tool
      raises the whole surface to approval-gated, because a single dispatch may exercise the
      user's delegated authority.
    - ``has_undeclared`` marks a registry serving unstamped tools (the gate off).  Each such
      tool already failed safe into the folds above — an untrusted source, an
      :data:`~maf_sandbox.Identity.APP` identity — and the flag is how a host notices the
      degrade without diffing the folds.
    """

    result_integrity: SourceIntegrity | None
    outbound_caps: frozenset[str]
    identities: frozenset[Identity]
    requires_approval: bool
    has_undeclared: bool


@dataclass(frozen=True)
class DispatchResult:
    """One dispatch's outcome: the serialized response, or the sentence the guest may see.

    ``value_json`` is the tool's return value as JSON text — serialized here, host-side,
    because the size cap is enforced on what actually crosses the boundary and a transport
    delivers exactly these bytes.  ``refusal`` is a sanitized sentence in the failure-ladder
    style: fixed shape, no provider detail, safe to land in a transcript.  Exactly one of the
    two is set, and :attr:`ok` says which.
    """

    value_json: str | None = None
    refusal: str | None = None

    def __post_init__(self) -> None:
        if (self.value_json is None) == (self.refusal is None):
            raise ValueError(
                "a DispatchResult carries exactly one of value_json or refusal — both or "
                "neither would let a caller read a refused dispatch as a delivered one"
            )

    @property
    def ok(self) -> bool:
        """Whether the dispatch delivered a response."""
        return self.refusal is None


class _RegistrationNotice:
    """One-time guard for the registration notice — per process, like the experimental one.

    A class attribute rather than a module global so mutating it is an ordinary attribute
    write, visible to readers and tests alike, with no ``global`` statement to reason about.
    """

    warned = False


def _warn_host_tools_once() -> None:
    if _RegistrationNotice.warned:
        return
    _RegistrationNotice.warned = True
    message = (
        "a host tool was registered for sandbox dispatch: dispatched calls run in the host "
        "process with the host's authority and bypass the middleware chain — the boundary "
        "sees only execute_code's aggregate result. Suppress this notice with "
        "warnings.filterwarnings('ignore', category=MafSandboxHostToolsWarning) once read."
    )
    try:
        # 3 == the host's own `registry.register(...)` line: this frame, `register`, caller.
        warnings.warn(message, category=MafSandboxHostToolsWarning, stacklevel=3)
    except MafSandboxHostToolsWarning:
        # Under `python -W error` the warning above raises at the call site. Registering a
        # tool must never fail because of an informational notice, so it is swallowed —
        # whether the notice printed is the only state a `-W error` host may change.
        pass


class HostToolRegistry:
    """The functions a sandboxed program may dispatch to — empty until a host says otherwise.

    The registry is the **one door**: :class:`HostToolRun` resolves names exclusively through
    it, so registration is the only way a function becomes reachable from inside a sandbox,
    and this is also where every host-side policy about dispatch lives — the
    ``require_declared`` gate, the per-run dispatch cap, and the response size ceilings.
    They are registry properties rather than spec fields because all three are statements
    about what the *host* will execute and carry, not about what a workload needs.

    An empty registry is the security story most kinds should keep: nothing is dispatchable,
    the registration warning never fires, and the middleware-bypass channel simply does not
    exist for that kind.  **Least privilege here comes from what a host registers, never from
    what it declares** — see :class:`~maf_sandbox.Identity`.

    Args:
        require_declared: When ``True``, an unstamped function is refused at registration and
            again at every aggregate build (see :class:`HostToolNotDeclared`).  Library
            default ``False``, in which case an unstamped tool registers and fails safe —
            an untrusted source, an :data:`~maf_sandbox.Identity.APP` identity, and a raised
            flag in the aggregate.
        max_dispatches_per_run: How many dispatches one :class:`HostToolRun` may make,
            refusals included — a probe loop burning the cap on unknown names is the cap
            working.  Must be at least 1; a host that wants zero wants an empty registry.
        response_limits: The per-response and per-run byte ceilings, reusing
            :class:`~maf_sandbox.TransferLimits` — its per-file leg caps one response, its
            total leg the run, its count leg the delivered responses.
    """

    def __init__(
        self,
        *,
        require_declared: bool = False,
        max_dispatches_per_run: int = DEFAULT_MAX_DISPATCHES_PER_RUN,
        response_limits: TransferLimits = DEFAULT_TRANSFER_LIMITS,
    ) -> None:
        _refuse_non_integer("max_dispatches_per_run", max_dispatches_per_run)
        if max_dispatches_per_run < 1:
            raise ValueError(
                f"max_dispatches_per_run must be at least 1, not {max_dispatches_per_run}: a "
                "host that wants nothing dispatched wants an empty registry, which needs no "
                "cap to say so"
            )
        # Same cast as above: the check catches the documented TransferLimits-vs-
        # SandboxLimits confusion for hosts that do not run a type checker.
        declared_limits = cast(object, response_limits)
        if not isinstance(declared_limits, TransferLimits):
            raise TypeError(
                f"response_limits must be a {TransferLimits.__name__} (one direction's caps), "
                f"not {type(response_limits).__name__} — a dispatch response is one outbound "
                "collection, so the pair-of-directions type is the wrong shape here"
            )
        for leg in ("max_bytes_per_file", "max_total_bytes", "max_files"):
            bound = cast(object, getattr(response_limits, leg))
            _refuse_non_integer(f"response_limits.{leg}", bound)
            if cast(int, bound) < 0:
                raise ValueError(f"response_limits.{leg} must not be negative, not {bound!r}")
        self._require_declared = require_declared
        self._max_dispatches_per_run = max_dispatches_per_run
        self._response_limits = response_limits
        self._tools: dict[str, Callable[..., Any]] = {}
        # Captured at registration, never re-read from the function: see `declaration_for`.
        self._declarations: dict[str, HostToolDeclaration | None] = {}
        self._sealed = False

    @property
    def require_declared(self) -> bool:
        """Whether unstamped functions are refused rather than degraded."""
        return self._require_declared

    @property
    def max_dispatches_per_run(self) -> int:
        """How many dispatches one run may make, refusals included."""
        return self._max_dispatches_per_run

    @property
    def response_limits(self) -> TransferLimits:
        """The byte and count ceilings a run's responses live under."""
        return self._response_limits

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> frozenset[str]:
        """Every registered tool name — the whole dispatchable surface, enumerable."""
        return frozenset(self._tools)

    def register(self, func: Callable[..., Any], *, name: str | None = None) -> None:
        """Make ``func`` dispatchable as ``name`` (default: its ``__name__``).

        Refuses a duplicate name rather than replacing: silently rebinding a name would
        mutate the dispatch surface out from under whatever derived the aggregate from it.
        With ``require_declared`` on, refuses an unstamped function here — at the host's own
        configuration site, where the fix is one decorator away — rather than later at
        dispatch, where only a sanitized sentence comes back.
        """
        if self._sealed:
            raise ValueError(
                f"host tool {name or getattr(func, '__name__', '?')!r} cannot be registered: "
                "this registry was sealed when its aggregate was taken, and a host has "
                "already derived a spec and a classification from the surface as it stood. "
                "Widening it now would dispatch what nothing classified."
            )
        if not callable(func):
            raise TypeError(f"a host tool must be callable, not {type(func).__name__}")
        tool_name = name if name is not None else getattr(func, "__name__", "")
        if not tool_name:
            raise ValueError("a host tool needs a name: pass name= for a callable without one")
        if tool_name in self._tools:
            raise ValueError(
                f"host tool {tool_name!r} is already registered. Refused rather than "
                "replaced: rebinding a name silently would change the dispatch surface out "
                "from under the aggregate a host already derived from it."
            )
        if self._require_declared and declaration_of(func) is None:
            raise HostToolNotDeclared(
                f"host tool {tool_name!r} has no complete information-flow declaration and "
                "this registry requires one. Stamp it with @sandbox_tool(source=..., "
                "sink=..., identity=...) — every leg answered; None is an answer, an "
                "omission is not."
            )
        _warn_host_tools_once()
        self._tools[tool_name] = func
        self._declarations[tool_name] = declaration_of(func)

    def declaration_for(self, name: str) -> HostToolDeclaration | None:
        """The declaration captured when ``name`` was registered.

        Read from here and never from the function again: a stamp is an attribute the host
        still owns, so re-reading it at dispatch would let a declaration swapped after the
        aggregate was derived take effect against a policy that never saw it. The claim that
        counts is the one standing at registration.
        """
        return self._declarations.get(name)

    def resolve(self, name: str) -> Callable[..., Any] | None:
        """The registered function for ``name``, or ``None`` — the only resolution path.

        Everything reachable from inside a sandbox goes through here, which is what makes
        registration the one door and this the one place argument validation can honestly
        live (:meth:`HostToolRun.dispatch`).
        """
        return self._tools.get(name)

    def identities(self) -> frozenset[Identity]:
        """Whose authority this registry's tools exercise — what a spec's ``identities`` carries.

        An unstamped tool (gate off) contributes :data:`~maf_sandbox.Identity.APP`: nobody
        answered the identity question, and its body factually runs in the host process with
        the application's authority, so it is read as carrying exactly that.  A declared
        ``identity=None`` (pure computation) contributes nothing.
        """
        found: set[Identity] = set()
        for declaration in self._declarations.values():
            if declaration is None:
                found.add(Identity.APP)
            elif declaration.identity is not None:
                found.add(declaration.identity)
        return frozenset(found)

    def aggregate(self) -> HostToolAggregate:
        """Derive what this registry means for the model-facing tool, and seal it.

        Taking the aggregate is the moment a host turns this surface into a spec and a
        classification, so the surface stops moving here: a later :meth:`register` is refused
        rather than dispatched against policy that never saw it. Together with declarations
        being captured at registration (:meth:`declaration_for`), what the router denies and
        what :class:`HostToolRun` dispatches cannot come apart.
        """
        self._sealed = True
        undeclared = sorted(
            tool_name
            for tool_name, declaration in self._declarations.items()
            if declaration is None
        )
        declarations = [d for d in self._declarations.values() if d is not None]
        sources = [d.source for d in declarations if d.source is not None]
        if undeclared:
            sources.append(SourceIntegrity.UNTRUSTED)
        result_integrity = min(sources, key=INTEGRITY_RANK.__getitem__) if sources else None
        identities = self.identities()
        return HostToolAggregate(
            result_integrity=result_integrity,
            outbound_caps=frozenset(d.sink for d in declarations if d.sink is not None),
            identities=identities,
            requires_approval=Identity.USER in identities,
            has_undeclared=bool(undeclared),
        )


def _refuse_non_integer(field: str, value: object) -> None:
    """Refuse anything that is not a plain integer, at the configuration site.

    A ceiling is only a ceiling if it compares. Every ``>`` against ``float("nan")`` is false
    and every one against ``float("inf")`` is too, so either silently removes the cap it was
    passed as — the failure mode a safety limit must not have. ``bool`` is an ``int`` and
    would quietly mean 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field} must be a plain integer, not {type(value).__name__}: a non-integer "
            "ceiling compares false against every count and so removes itself"
        )


def _refused(sentence: str) -> DispatchResult:
    return DispatchResult(refusal=sentence)


class HostToolRun:
    """One ``execute_code`` run's dispatch context: the cap, the ledger, and the one door.

    Per run, not per registry: the dispatch cap and the response ceilings bound what a single
    guest program may cost, so a fresh run starts with a fresh count.  Everything model-visible
    that leaves :meth:`dispatch` is a sanitized sentence — the detail a host needs lands in
    ``logger`` instead, exactly the split :mod:`maf_sandbox.maf`'s failure ladder draws.

    Args:
        registry: Where names resolve and whose policy (cap, gate, ceilings) applies.
        logger: Where dispatch failures write their detail. Defaults to this module's logger;
            pass the workload's own so its records keep the workload's logger name.
    """

    def __init__(self, registry: HostToolRegistry, *, logger: logging.Logger | None = None) -> None:
        self._registry = registry
        self._logger = logger if logger is not None else _DEFAULT_LOGGER
        self._dispatched = 0
        self._delivered = 0
        self._delivered_bytes = 0

    async def dispatch(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> DispatchResult:
        """Resolve, gate, validate, call and cap — the whole contract, at the one door.

        Every call counts toward the run's dispatch cap, refused ones included: a guest
        probing names or replaying refusals is spending the budget the cap exists to bound.
        Exhaustion is a refusal rather than an exception so the guest program finishes and
        reports what it has, instead of dying mid-way with the reason lost.
        """
        self._dispatched += 1
        cap = self._registry.max_dispatches_per_run
        if self._dispatched > cap:
            return _refused(
                f"Error: this run's host-tool dispatch cap ({cap}) is exhausted — finish "
                "with the results already delivered and report what remains undone"
            )
        func = self._registry.resolve(name)
        if func is None:
            return _refused(f"Error: {name!r} is not a registered host tool")
        declaration = self._registry.declaration_for(name)
        if declaration is None and self._registry.require_declared:
            # Belt-and-braces behind the registration gate. Unreachable while the registry is
            # the only way in, and kept because the cost of being wrong about that is a tool
            # dispatching unclassified.
            return _refused(
                f"Error: {name!r} carries no complete information-flow declaration, and "
                "this host dispatches declared tools only"
            )
        if declaration is not None and declaration.identity is Identity.USER:
            return _refused(
                f"Error: {name!r} exercises the user's identity, which cannot be served "
                f"yet — serving it needs {_USER_IDENTITY_PREREQUISITES}"
            )
        # Cast to `object` because a transport hands over whatever the guest's JSON
        # parsed to — the annotation describes the contract, this check enforces it.
        given = cast(object, arguments)
        if given is not None and not isinstance(given, Mapping):
            return _refused(
                f"Error: arguments for {name!r} must be a JSON object of keyword arguments"
            )
        provided: dict[str, Any] = dict(arguments) if arguments is not None else {}
        limits = self._registry.response_limits
        if self._delivered + 1 > limits.max_files:
            # Before the call, not after it. A sink tool's body runs in the host process and
            # does its work there; refusing once it has already run means the effect happened
            # and cannot be reported, and the refusal reads like something to retry.
            return _refused(
                f"Error: this run's delivered-response cap ({limits.max_files}) is "
                "exhausted — finish with the results already delivered"
            )
        try:
            signature = inspect.signature(func)
        except (ValueError, TypeError) as exc:
            # `register` accepts any callable, and some — several built-ins — expose no
            # signature to read. Nothing can be validated, so nothing is dispatched.
            self._logger.warning("host tool %r exposes no signature to validate: %s", name, exc)
            return _refused(
                f"Error: host tool {name!r} exposes no signature, so its arguments cannot be "
                "validated"
            )
        try:
            # Host-side, at the one door, never in a guest shim: a schema check running
            # where model-written code can edit it is decoration. Binding proves the names
            # and arity; anything deeper is the tool body's own duty — it is host code.
            signature.bind(**provided)
        except TypeError as exc:
            return _refused(f"Error: arguments do not bind to host tool {name!r}: {exc}")
        try:
            result = func(**provided)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - the guest gets a sentence, the log the rest
            self._logger.warning("host tool %r failed: %s", name, error_detail(exc))
            return _refused(f"Error: host tool {name!r} failed — the reason is in the host's log")
        try:
            # `allow_nan=False` because Python's default emits bare NaN/Infinity, which no
            # strict JSON parser on the guest side accepts — a payload delivered as success
            # and unreadable on arrival is worse than the refusal the ValueError becomes.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            self._logger.warning("host tool %r returned an unserializable value: %s", name, exc)
            return _refused(
                f"Error: host tool {name!r} returned a value that cannot be carried as JSON"
            )
        size = len(encoded.encode("utf-8"))
        if size > limits.max_bytes_per_file:
            return _refused(
                f"Error: host tool {name!r}'s response is {size} bytes and the "
                f"per-response cap allows {limits.max_bytes_per_file}"
            )
        if self._delivered_bytes + size > limits.max_total_bytes:
            return _refused(
                f"Error: delivering host tool {name!r}'s {size}-byte response would exceed "
                f"this run's total response cap ({limits.max_total_bytes} bytes)"
            )
        self._delivered += 1
        self._delivered_bytes += size
        return DispatchResult(value_json=encoded)
