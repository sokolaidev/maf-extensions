"""The host-tools safety contract: what a sandboxed program may call on the host, and how.

``HOST_TOOLS`` is the one capability where trust crosses *outward*.  A host tool's body runs
in the **host process**, with the host's authority, driven by model-written code, and its
call bypasses whatever middleware the host runs — the boundary sees only ``execute_code``'s
aggregate result.  The layered rationale lives in ``docs/sandbox/hosts.md``.

Four things a caller must know:

- :class:`HostToolRegistry` is the **one door**.  Nothing is callable until it is
  registered, and :meth:`HostToolRegistry.resolve` is the only path in — which is what makes
  registration the only place a gate or a validation can honestly sit.
- **Least privilege comes from what a host registers, never from what it declares.**  A
  declaration reads like a control and is not one; see :class:`~maf_sandbox.Identity`.
- Everything a guest can see is a sanitized sentence, in the failure-ladder style of
  :mod:`maf_sandbox.maf`: a refusal ends the call, not the program, and provider detail goes
  to the host's log rather than into a transcript.
- **A host may watch its own host-tool calls, and only that.**  The registry's
  ``host_tool_calls_observer``
  receives each call's run and name, so a host can attribute a call to the program that made
  it.  It is off by default, it sees nothing the call does not already know, and it changes
  nothing a guest can see.

Declarations are carried claims; enforcement is the host's middleware.  A host without
``agent_framework.security`` still gets the registry, the warning, the gate and the denials —
and classifications ready the day it turns enforcement on.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from ._containment import CONTAINED, escapes_containment
from ._error_detail import error_detail
from ._observer import (
    RECORDED_CALL,
    HostToolCalled,
    HostToolOutcome,
    SandboxObserver,
    record,
    refuse_an_unusable_observer,
)
from ._protocol import (
    DEFAULT_TRANSFER_LIMITS,
    INTEGRITY_RANK,
    HostToolAggregate,
    Identity,
    SandboxKey,
    SourceIntegrity,
    TransferLimits,
)

#: Fallback for :class:`HostToolRun`'s ``logger`` argument — named apart from the usual
#: module-level ``logger`` because that argument is the point: a workload passes its own so
#: host-tool-call failure records keep the workload's logger name (the convention `maf.py` set).
_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN",
    "HostToolCallResult",
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

#: How many host-tool calls one run may make, unless the host's registry says otherwise.  The cap
#: is the registry's (host policy, beside ``require_declared``) rather than the spec's: it
#: bounds what the *host* is willing to execute middleware-invisibly, not what a workload
#: needs.  Deliberately below :data:`~maf_sandbox.DEFAULT_TRANSFER_LIMITS`'s file count, so
#: with everything defaulted this cap is the one that bites first.
DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN = 16

#: The shortest response that can cross at all — ``json.dumps(0)`` is one byte.  What makes
#: "no room left" answerable before a tool runs rather than only after its size is known.
_SMALLEST_RESPONSE = 1

#: What a host owes before a USER-identity tool can be served, named once so the refusal and
#: the docs cannot drift apart.
_USER_IDENTITY_PREREQUISITES = (
    "a host serves one by giving its registry a mint_user_identity callback, which mints that "
    "run's authority"
)

#: The keyword a minted identity reaches a tool body by.  Reserved: a guest sending one would
#: be choosing the authority its own call runs under, so an argument of this name is refused
#: before binding rather than quietly overwritten by the injection.
_USER_IDENTITY_PARAMETER = "user_identity"

#: Stands in that argument's place while the refusals between admission and the body still
#: have their say. Its own object, never ``None``: ``None`` is what a failed mint returns, and
#: a sentinel a real answer could equal is one that eventually reaches a body as its authority.
_UNMINTED = object()


def _accepts_user_identity(func: Callable[..., Any]) -> bool:
    """Whether ``func`` can be handed the minted identity by keyword.

    ``**kwargs`` counts, since a body may fan its arguments out rather than name each one, and
    so does a signature that cannot be read: nothing is provable about one here, and the call
    is refused later by the same guard that validates the guest's arguments.
    """
    try:
        signature = inspect.signature(func)
    except Exception:  # noqa: BLE001 - unprovable here, and refused at call time
        return True
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        # The kinds a keyword actually reaches, named rather than excluded: `*user_identity`
        # is neither positional-only nor bindable by that name, and a rule written as "not
        # positional-only" admits it and registers a tool no call can ever bind.
        if parameter.name == _USER_IDENTITY_PARAMETER and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


class MafSandboxHostToolsWarning(UserWarning):
    """Warning category for the one-time host-tools registration notice."""


class HostToolNotDeclared(ValueError):
    """A host tool has no complete information-flow declaration and the registry requires one.

    A host configuration error, raised at registration — the host's own configuration site,
    where the fix is one decorator away.  The fix is :func:`sandbox_tool` with every leg
    answered; ``None`` is an answer.
    """


class HostToolIdentityNotAllowed(ValueError):
    """A host tool's declared authority is not in the registry's ``allowed_identities``.

    A host configuration error, raised at registration — the offending line, before any
    sandbox exists and without a router in scope.  A tool exercising an authority the
    registry does not list — an :data:`~maf_sandbox.Identity.USER` tool, or with the gate off
    an unstamped one, read as :data:`~maf_sandbox.Identity.APP` — is refused; a tool
    declaring ``identity=None`` exercises no authority and is always allowed.  This is the
    earlier, fail-closed layer beside the router's ``denied_identities``, which stays the
    attach-time authority; the two share one predicate and only ever refuse, so they cannot
    disagree in the widening direction.
    """


@dataclass(frozen=True)
class HostToolDeclaration:
    """One host tool's information-flow declaration — all three legs, answered.

    ``source`` is what the tool's output brings *in* (``None``: brings nothing external in);
    ``sink`` is the host-vocabulary confidentiality cap for what flows *out* through it
    (``None``: nothing conversation-derived flows out); ``identity`` is whose authority the
    body exercises (``None``: pure computation, no authority beyond returning a value).
    ``source`` is the derivation question :class:`SourceIntegrity` states, asked of what this
    body *returns*: a body reaching a network or a store the host cannot vouch for declares
    :data:`SourceIntegrity.UNTRUSTED` once anything from that source survives into its result,
    however first-party the formatting around it. Reaching one and returning nothing from it is
    what ``None`` already covers.

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
    a sink, whose authority does it carry* before the function can be declared at
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
class HostToolCallResult:
    """One host-tool call's outcome: the serialized response, or the sentence the guest may see.

    ``value_json`` is the tool's return value as JSON text — serialized here, host-side,
    because the size cap is enforced on what actually crosses the boundary and a transport
    delivers exactly these bytes, inside whatever framing it declared through
    :meth:`HostToolRun.call`'s ``framing_bytes`` and which was capped along with them.
    ``refusal`` is a sanitized sentence in the failure-ladder style: fixed shape, no provider
    detail, safe to land in a transcript.  Exactly one of the
    two is set, and :attr:`ok` says which.
    """

    value_json: str | None = None
    refusal: str | None = None

    def __post_init__(self) -> None:
        if (self.value_json is None) == (self.refusal is None):
            raise ValueError(
                "a HostToolCallResult carries exactly one of value_json or refusal — both or "
                "neither would let a caller read a refused call as a delivered one"
            )

    @property
    def ok(self) -> bool:
        """Whether the call delivered a response."""
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
        "a host tool was registered for calling from a sandbox: host-tool calls run in the "
        "host process with the host's authority and bypass the middleware chain — the boundary "
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
    """The functions a sandboxed program may call — empty until a host says otherwise.

    The registry is the **one door**: :class:`HostToolRun` resolves names exclusively through
    it, so registration is the only way a function becomes reachable from inside a sandbox,
    and this is also where every host-side policy about host-tool calls lives — the
    ``require_declared`` gate, the per-run call cap, and the response size ceilings.
    They are registry properties rather than spec fields because all three are statements
    about what the *host* will execute and carry, not about what a workload needs.

    An empty registry is the security story most kinds should keep: nothing is callable,
    the registration warning never fires, and the middleware-bypass channel simply does not
    exist for that kind.  **Least privilege here comes from what a host registers, never from
    what it declares** — see :class:`~maf_sandbox.Identity`.

    Args:
        require_declared: When ``True``, an unstamped function is refused at registration
            (see :class:`HostToolNotDeclared`) — the one gate that matters, because the
            declaration is captured there and never re-read from the function.  Library
            default ``False``, in which case an unstamped tool registers and fails safe —
            an untrusted source, an :data:`~maf_sandbox.Identity.APP` identity, and a raised
            flag in the aggregate.
        allowed_identities: The authorities a registered tool may exercise, refused at
            registration otherwise (:class:`HostToolIdentityNotAllowed`).  Default
            ``frozenset({Identity.APP})`` — secure by default: an
            :data:`~maf_sandbox.Identity.USER` tool (or, with ``require_declared`` off, an
            unstamped one, read as :data:`~maf_sandbox.Identity.APP`) is refused until a host
            opts in with ``frozenset({Identity.APP, Identity.USER})``.  A tool declaring
            ``identity=None`` exercises no authority and is always allowed.  The earlier,
            fail-closed layer beside the router's ``denied_identities``, which stays the
            attach-time authority.
        max_host_tool_calls_per_run: How many host-tool calls one :class:`HostToolRun` may make,
            refusals included — a probe loop burning the cap on unknown names is the cap
            working.  Must be at least 1; a host that wants zero wants an empty registry.
        response_limits: The per-response and per-run byte ceilings, reusing
            :class:`~maf_sandbox.TransferLimits` — its per-file leg caps one response, its
            total leg the run, its count leg the delivered responses.
        mint_user_identity: An async callback returning the authority an
            :data:`~maf_sandbox.Identity.USER` tool acts under, called with that run's
            ``run_id`` and handed to the body as ``user_identity``.  **One success per**
            :class:`HostToolRun`: the first usable answer is cached and reused for every later
            call of that run, while an attempt that raised or answered unusably is not, so a
            later call asks again rather than inheriting a transient.  Default ``None``, in
            which case such a tool registers and its call is refused: a registry stays writable
            honestly on a host that serves no user authority.  Where one is given, a ``USER``
            tool taking no ``user_identity`` parameter is refused at registration.
        host_tool_calls_observer: A host's callback that sees each call and the run that made
            it, so the host can attribute the call to the program. Takes the run and the
            name and returns a context manager the call enters and exits
            structurally — a refused call included, since it starts at the cap check.
            Synchronous and fast: it runs on the calling task, and must not block it.
            Its ``__exit__`` return value is ignored, so no observer can swallow a call's
            outcome. The name is as given: a string for every call that resolves, and
            only the refusal that rejects it sees a non-string.
        observer: Where each call is *recorded* — one
            :class:`~maf_sandbox.HostToolCalled` per call, carrying the declaration it
            ran under, how it ended and what it delivered. The other half of the pair above,
            and not a replacement for it: ``host_tool_calls_observer`` brackets a call so a host
            can attribute work done *during* it, while this answers what the call was. Default
            ``None`` records nothing and builds no event. The sandbox lifecycle is
            :class:`~maf_sandbox.SandboxRouter`'s to record, and a host may wire either alone.
    """

    def __init__(
        self,
        *,
        require_declared: bool = False,
        allowed_identities: frozenset[Identity] = frozenset({Identity.APP}),
        max_host_tool_calls_per_run: int = DEFAULT_MAX_HOST_TOOL_CALLS_PER_RUN,
        response_limits: TransferLimits = DEFAULT_TRANSFER_LIMITS,
        host_tool_calls_observer: (
            Callable[[HostToolRun, object], contextlib.AbstractContextManager[object]] | None
        ) = None,
        mint_user_identity: Callable[[str], Awaitable[str]] | None = None,
        observer: SandboxObserver | None = None,
    ) -> None:
        _refuse_non_integer("max_host_tool_calls_per_run", max_host_tool_calls_per_run)
        if max_host_tool_calls_per_run < 1:
            raise ValueError(
                f"max_host_tool_calls_per_run must be at least 1, not "
                f"{max_host_tool_calls_per_run}: a "
                "host that wants nothing callable wants an empty registry, which needs no "
                "cap to say so"
            )
        # Same cast as above: the check catches the documented TransferLimits-vs-
        # SandboxLimits confusion for hosts that do not run a type checker.
        declared_limits = cast(object, response_limits)
        if not isinstance(declared_limits, TransferLimits):
            raise TypeError(
                f"response_limits must be a {TransferLimits.__name__} (one direction's caps), "
                f"not {type(response_limits).__name__} — a host-tool response is one outbound "
                "collection, so the pair-of-directions type is the wrong shape here"
            )
        for leg in ("max_bytes_per_file", "max_total_bytes", "max_files"):
            bound = cast(object, getattr(response_limits, leg))
            _refuse_non_integer(f"response_limits.{leg}", bound)
            if cast(int, bound) < 1:
                # Zero as well as negative, and for the reason the call cap gives above:
                # the smallest JSON value is one byte, so a zero on any leg is a registry that
                # can never deliver a response — which is an empty registry with extra steps,
                # and a per-call refusal the model can do nothing about.
                raise ValueError(
                    f"response_limits.{leg} is {bound!r}, so no response could ever be "
                    "delivered — a host that wants none wants an empty registry"
                )
        if host_tool_calls_observer is not None:
            # Reject invalid observer configurations at construction rather than discovering
            # and logging them on every call.
            given_observer = cast(object, host_tool_calls_observer)
            if not callable(given_observer):
                raise TypeError(
                    "host_tool_calls_observer must be callable, not "
                    f"{type(host_tool_calls_observer).__name__}"
                )
            # An instance with an async ``__call__`` is equally an observer no one awaits,
            # and only its ``__call__`` is the coroutine function ``inspect`` can see.
            if inspect.iscoroutinefunction(given_observer) or inspect.iscoroutinefunction(
                getattr(given_observer, "__call__", None)
            ):
                raise TypeError(
                    "host_tool_calls_observer must be synchronous, not a coroutine function: it is "
                    "called on the calling task and must return a context manager to "
                    "enter, not a coroutine to await"
                )
        allowed = cast(object, allowed_identities)
        if not isinstance(allowed, (frozenset, set)):
            raise TypeError(
                "allowed_identities must be a set of Identity members, not "
                f"{type(allowed_identities).__name__}"
            )
        for member in cast("frozenset[object]", allowed):
            if not isinstance(member, Identity):
                raise TypeError(
                    "allowed_identities may hold only Identity members, not "
                    f"{type(member).__name__}: {member!r}"
                )
        if mint_user_identity is not None:
            # Refused at construction for the observer's reason: a minter this registry cannot
            # call is a USER tool that refuses at call time, where only a sanitized sentence
            # comes back and the host cannot see which of its arguments was wrong.
            given_minter = cast(object, mint_user_identity)
            if not callable(given_minter):
                raise TypeError(
                    f"mint_user_identity must be callable, not {type(mint_user_identity).__name__}"
                )
        self._require_declared = require_declared
        self._observer = (
            None if observer is None else refuse_an_unusable_observer(observer, argument="observer")
        )
        self._mint_user_identity = mint_user_identity
        self._allowed_identities = frozenset(allowed_identities)
        self._max_host_tool_calls_per_run = max_host_tool_calls_per_run
        self._response_limits = response_limits
        self._host_tool_calls_observer = host_tool_calls_observer
        self._tools: dict[str, Callable[..., Any]] = {}
        # Captured at registration, never re-read from the function: see `declaration_for`.
        self._declarations: dict[str, HostToolDeclaration | None] = {}
        self._sealed = False

    @property
    def require_declared(self) -> bool:
        """Whether unstamped functions are refused rather than degraded."""
        return self._require_declared

    @property
    def mint_user_identity(self) -> Callable[[str], Awaitable[str]] | None:
        """Mints this run's user authority, or ``None`` where the host serves none.

        Called with that run's ``run_id`` when a :data:`~maf_sandbox.Identity.USER` tool is
        reached, and its result handed to the body as ``user_identity`` and never persisted.
        One *success* per :class:`HostToolRun` — a failed or unusable attempt is not cached, so
        a later call asks again.  Without a minter, such a tool registers and its call is
        refused.
        """
        return self._mint_user_identity

    @property
    def allowed_identities(self) -> frozenset[Identity]:
        """The authorities a registered tool may exercise.

        A tool declaring ``identity=None`` (no authority) is always registrable; anything
        else must be in this set or :meth:`register` refuses it with
        :class:`HostToolIdentityNotAllowed`. ``denied_identities`` on the router stays the
        attach-time backstop.
        """
        return self._allowed_identities

    @property
    def max_host_tool_calls_per_run(self) -> int:
        """How many host-tool calls one run may make, refusals included."""
        return self._max_host_tool_calls_per_run

    @property
    def response_limits(self) -> TransferLimits:
        """The byte and count ceilings a run's responses live under."""
        return self._response_limits

    @property
    def host_tool_calls_observer(
        self,
    ) -> Callable[[HostToolRun, object], contextlib.AbstractContextManager[object]] | None:
        """The host's observer, or ``None`` when the host registered none — the off-by-default
        half of the contract, where a host can confirm it is watching nothing."""
        return self._host_tool_calls_observer

    @property
    def observer(self) -> SandboxObserver | None:
        """Where this registry records its calls, or ``None`` when the host registered nowhere."""
        return self._observer

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> frozenset[str]:
        """Every registered tool name — the whole callable surface, enumerable."""
        return frozenset(self._tools)

    def register(self, func: Callable[..., Any], *, name: str | None = None) -> None:
        """Make ``func`` callable as ``name`` (default: its ``__name__``).

        Refuses a duplicate name rather than replacing: silently rebinding a name would
        mutate the callable surface out from under whatever derived the aggregate from it.
        With ``require_declared`` on, refuses an unstamped function here — at the host's own
        configuration site, where the fix is one decorator away — rather than later at
        call time, where only a sanitized sentence comes back.  A tool whose authority is
        outside ``allowed_identities`` is refused here too
        (:class:`HostToolIdentityNotAllowed`).
        """
        if self._sealed:
            raise ValueError(
                f"host tool {name or getattr(func, '__name__', '?')!r} cannot be registered: "
                "this registry was sealed when its aggregate was taken, and a host has "
                "already derived a spec and a classification from the surface as it stood. "
                "Widening it now would let a guest call what nothing classified."
            )
        if not callable(func):
            raise TypeError(f"a host tool must be callable, not {type(func).__name__}")
        derived = cast(object, name if name is not None else getattr(func, "__name__", ""))
        if not isinstance(derived, str):
            # Cast and check for the same reason as everywhere else here — and this one is
            # not only about untyped hosts: `__name__` is an ordinary attribute a callable
            # object can set to anything. A non-string name registers into a `dict[str, ...]`
            # and out of `names()`, and no guest can ever reach it: a transport carries the
            # name as JSON text, so a tool keyed by 7 is registered and unreachable.
            raise TypeError(
                f"a host tool's name must be a string, not {type(derived).__name__}: it is the "
                "key a guest sends to reach it, and only text crosses that boundary"
            )
        tool_name = derived
        if not tool_name:
            raise ValueError("a host tool needs a name: pass name= for a callable without one")
        if tool_name in self._tools:
            raise ValueError(
                f"host tool {tool_name!r} is already registered. Refused rather than "
                "replaced: rebinding a name silently would change the callable surface out "
                "from under the aggregate a host already derived from it."
            )
        # One read, used by both the gate and the snapshot below. The stamp is an attribute,
        # so it can be a property answering differently each time — and two reads would let a
        # function pass the gate and register as undeclared, turning the refusal this gate
        # promises the host into a sanitized sentence to the model at call time.
        declaration = declaration_of(func)
        if self._require_declared and declaration is None:
            raise HostToolNotDeclared(
                f"host tool {tool_name!r} has no complete information-flow declaration and "
                "this registry requires one. Stamp it with @sandbox_tool(source=..., "
                "sink=..., identity=...) — every leg answered; None is an answer, an "
                "omission is not."
            )
        # The identity gate. An unstamped tool is read as APP — nobody answered, and its body
        # runs in the host process with the app's authority; a declared identity=None
        # exercises none and is always allowed; anything else must be in allowed_identities.
        # Read from the declaration captured above, so a stamp swapped in afterwards cannot
        # slip past it — the one-read invariant require_declared already keeps.
        effective_identity = Identity.APP if declaration is None else declaration.identity
        if effective_identity is not None and effective_identity not in self._allowed_identities:
            allowed = ", ".join(sorted(str(i) for i in self._allowed_identities)) or "none"
            read_as = " (unstamped, read as 'app')" if declaration is None else ""
            raise HostToolIdentityNotAllowed(
                f"host tool {tool_name!r} exercises {str(effective_identity)!r} authority"
                f"{read_as}, which this registry does not allow (allowed_identities: "
                f"{allowed}). A host that means to run tools under this authority opts in at "
                "construction with allowed_identities=frozenset({Identity.APP, "
                "Identity.USER}); a tool declaring identity=None exercises no authority and is "
                "always allowed. denied_identities on the router stays the attach-time backstop."
            )
        # Only where the host means to serve one. A registry with no minter keeps refusing
        # USER tools at call time, and declaring one there must stay possible: a registry has
        # to be writable honestly on a host that serves no user authority, which is the whole
        # reason the principal is declarable before it is servable.
        if (
            effective_identity is Identity.USER
            and self._mint_user_identity is not None
            and not _accepts_user_identity(func)
        ):
            raise ValueError(
                f"host tool {tool_name!r} exercises the user's authority but takes no "
                f"{_USER_IDENTITY_PARAMETER!r} parameter, so this registry has nowhere to hand "
                "the identity it mints for the run. Give it one — a tool acting as the user "
                "receives that authority explicitly rather than reaching for an ambient "
                "credential, which is what makes the per-run bound structural."
            )
        _warn_host_tools_once()
        self._tools[tool_name] = func
        self._declarations[tool_name] = declaration

    def declaration_for(self, name: str) -> HostToolDeclaration | None:
        """The declaration captured when ``name`` was registered.

        Read from here and never from the function again: a stamp is an attribute the host
        still owns, so re-reading it at call time would let a declaration swapped after the
        aggregate was derived take effect against a policy that never saw it. The claim that
        counts is the one standing at registration.
        """
        return self._declarations.get(name)

    def resolve(self, name: str) -> Callable[..., Any] | None:
        """The registered function for ``name``, or ``None`` — the only resolution path.

        Everything reachable from inside a sandbox goes through here, which is what makes
        registration the one door and this the one place argument validation can honestly
        live (:meth:`HostToolRun.call`).
        """
        return self._tools.get(name)

    def _identities(self) -> frozenset[Identity]:
        """Whose authority this registry's tools exercise — what a spec's ``identities`` carries.

        Private, and reachable only as :attr:`HostToolAggregate.identities`, because taking a
        policy view has to seal: a host that read this set, built a spec from it and passed a
        router denying :data:`~maf_sandbox.Identity.APP` could otherwise register an APP tool
        afterwards and call it past a deny list that never saw it.

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
        rather than called against policy that never saw it. Together with declarations
        being captured at registration (:meth:`declaration_for`), what the router denies and
        what :class:`HostToolRun` calls cannot come apart.
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
        identities = self._identities()
        return HostToolAggregate(
            result_integrity=result_integrity,
            outbound_caps=frozenset(d.sink for d in declarations if d.sink is not None),
            identities=identities,
            requires_approval=Identity.USER in identities,
            has_undeclared=bool(undeclared),
            response_limits=self._response_limits,
            max_host_tool_calls_per_run=self._max_host_tool_calls_per_run,
        )


def _refuse_non_integer(field: str, value: object) -> None:
    """Refuse anything that is not a plain integer, at the configuration site.

    A type check rather than a tightened range, because the two values that matter most defeat
    a range check: every ``>`` against ``float("nan")`` is false and every one against
    ``float("inf")`` is too, so both pass any bound and then silently remove the cap they were
    passed as — the failure mode a safety limit must not have.  The rest are refused for
    ordinary reasons and not that one: ``"8"`` and ``None`` raise at the first comparison,
    ``2.5`` compares perfectly well and is simply not a count, and ``bool`` is an ``int`` that
    would quietly mean 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field} must be a plain integer, not {type(value).__name__}: it bounds a count, "
            "and a range check cannot be relied on to catch a non-integer — float('nan') and "
            "float('inf') satisfy every bound and then compare false against every count"
        )


def _refused(sentence: str) -> HostToolCallResult:
    return HostToolCallResult(refusal=sentence)


#: How much guest-chosen text one refusal may quote back.  Generous for anything a caller
#: meant, short enough that quoting it costs nothing.
_MAX_ECHOED_CHARS = 120


def _bounded(text: str) -> str:
    """``text``, cut to a length this package chose rather than the guest.

    A refusal is the one payload the response ceilings never see: :class:`TransferLimits`
    bounds what a tool *delivered*, and a refusal by definition delivered nothing — yet the
    transport writes it just the same.  So a sentence quoting its own input is a response the
    guest picks the size of, and two of them quote guest text: the name that did not resolve,
    and the binding error, which names the keyword that was not expected.
    """
    return text if len(text) <= _MAX_ECHOED_CHARS else f"{text[:_MAX_ECHOED_CHARS]} (truncated)"


@contextlib.contextmanager
def _observe(
    observer: Callable[[HostToolRun, object], contextlib.AbstractContextManager[object]] | None,
    run: HostToolRun,
    name: object,
    logger: logging.Logger,
) -> Generator[None]:
    """The call's observer, entered and exited structurally — or nothing, when absent.

    The guard is the point: a host-tool call is the guest's and the observer is the host's
    code, so none of the three observer failures — the factory, ``__enter__``, ``__exit__`` —
    may reach the call or the guest. Each logs and continues, in the shape
    :func:`_reclaim_the_transports_own` already uses. The catch is narrow on purpose: an
    observer's own ``Exception``, a ``CancelledError`` it raises, and a ``GeneratorExit``
    from its own generator are contained, but ``SystemExit`` and ``KeyboardInterrupt`` are
    the host's control flow, not an observer failure, so they escape. The call's own
    exception is
    forwarded into ``__exit__`` but its return value is ignored: an observer returning
    ``True`` is one that would swallow a call's outcome, and the pairing the ledger relies
    on — every enter has exactly one exit — is the ``try``, which a return value cannot
    un-pair. ``__exit__`` runs on ``BaseException`` too, so a cancelled call still exits
    its observer, because the exit is structural rather than a check on the outcome.
    """
    if observer is None:
        yield
        return
    try:
        context = observer(run, name)
    # Contain the observer's own failures: its Exceptions, a CancelledError from a host's
    # shutdown bug, a GeneratorExit from its own generator. SystemExit and
    # KeyboardInterrupt are the host's control flow, so they deliberately escape.
    except CONTAINED as exc:  # noqa: BLE001 - `_containment` carries the rule
        if escapes_containment(exc):
            raise
        logger.warning(
            "host tools: the host-tool-call observer failed to observe %r: %s",
            name,
            error_detail(exc),
        )
        yield
        return
    try:
        context.__enter__()
    except CONTAINED as exc:  # noqa: BLE001 - never entered, so never exited, and the call runs on
        if escapes_containment(exc):
            raise
        logger.warning(
            "host tools: the host-tool-call observer failed to observe %r: %s",
            name,
            error_detail(exc),
        )
        yield
        return
    try:
        yield
    except BaseException as exc:
        # The call raised (or was cancelled): forward it, but an observer's ``__exit__``
        # raising may not mask it, and its return value may not swallow it.
        try:
            context.__exit__(type(exc), exc, exc.__traceback__)
        except CONTAINED as exit_exc:  # noqa: BLE001 - the observer's failure is its own warning
            if escapes_containment(exit_exc):
                raise
            logger.warning(
                "host tools: the host-tool-call observer failed to exit for %r: %s",
                name,
                error_detail(exit_exc),
            )
        raise
    else:
        try:
            context.__exit__(None, None, None)
        except CONTAINED as exit_exc:  # noqa: BLE001 - a success must not become a failure over the exit
            if escapes_containment(exit_exc):
                raise
            logger.warning(
                "host tools: the host-tool-call observer failed to exit for %r: %s",
                name,
                error_detail(exit_exc),
            )


@dataclass
class _Called:
    """What a host-tool call turned out to be, for the record that covers every way out of it.

    Filled in at the one place the name resolves, so a call that then refuses, raises or is
    cancelled is still recorded as a call of *that* tool rather than of nothing.  ``declared``
    is read on its own: an unstamped tool has no declaration at all rather than a weak one, and
    three ``None`` legs would be indistinguishable from a stamp answering ``None`` to each.
    """

    tool: str | None = None
    declared: bool = False
    source: SourceIntegrity | None = None
    sink: str | None = None
    identity: Identity | None = None
    #: What *this* call put on the wire, framing included, and zero unless it delivered.
    #: Read from here rather than differenced against the run's ledger: calls of one run may
    #: overlap, so two that start at the same total would each report the other's bytes as
    #: their own, and the one finishing second would report both.
    delivered_bytes: int = 0


class HostToolRun:
    """One ``execute_code`` run's host-tool-call context: the cap, the ledger, and the one door.

    Per run, not per registry: the call cap and the response ceilings bound what a single
    guest program may cost, so a fresh run starts with a fresh count.  Everything model-visible
    that leaves :meth:`call` is a sanitized sentence — the detail a host needs lands in
    ``logger`` instead, exactly the split :mod:`maf_sandbox.maf`'s failure ladder draws.

    **Build it inside the tool call whose program it supervises.**  The tool call being recorded
    is read here, once, and carried onto every :class:`~maf_sandbox.HostToolCalled` — a run
    built elsewhere and used later records no call, and a record keyed on ``run_id`` alone
    cannot be joined to the rest of what that call did.

    Args:
        registry: Where names resolve and whose policy (cap, gate, ceilings) applies.
        logger: Where host-tool-call failures write their detail. Defaults to this module's logger;
            pass the workload's own so its records keep the workload's logger name.
        run_id: A stable identifier for this run, carried to :attr:`run_id` and to the
            ``host_tool_calls_observer`` on every call. Defaults to a fresh random one, so a
            host
            that wants to attribute a call to the run that made it — a per-run ledger, a trace —
            has an identity to key on rather than the object's own, which is neither loggable nor
            stable across processes. Pass one to tie a run to a meaning of the caller's own (a
            turn id, the guest's run directory); it must be a non-empty string if given.
        key: The sandbox this run's program is executing in, carried onto every
            :class:`~maf_sandbox.HostToolCalled` the registry's ``observer`` records.
            It is what joins a host-tool call to the conversation that made it: without one a
            record says which run called and nothing about whose. A transport holding the key
            passes it; ``None`` leaves the record keyed on ``run_id`` alone.
    """

    def __init__(
        self,
        registry: HostToolRegistry,
        *,
        logger: logging.Logger | None = None,
        run_id: str | None = None,
        key: SandboxKey | None = None,
    ) -> None:
        # `cast` to `object` before the check, as the observer argument is: the annotation says
        # `str | None`, but guest-adjacent code and wrong arguments hand over anything, and an
        # identity that cannot tell two runs apart — an empty string, a non-string — is rejected
        # here rather than left to surface as a run every call attributes to one name.
        given = cast(object, run_id)
        if given is not None and (not isinstance(given, str) or not given):
            raise ValueError(f"run_id must be a non-empty string when given, not {given!r}")
        self._registry = registry
        self._logger = logger if logger is not None else _DEFAULT_LOGGER
        self._run_id = run_id if run_id is not None else uuid.uuid4().hex
        self._key = key
        # Here rather than per call: a guest's callback is served on a task of the transport's
        # own, whose context is a copy taken when the transport started listening rather than
        # the body's live one. A run is built inside the call it belongs to, and this is.
        self._call = RECORDED_CALL.get()
        self._calls = 0
        self._delivered = 0
        self._delivered_bytes = 0
        self._minted_user_identity: str | None = None
        self._mint_lock: asyncio.Lock | None = None
        self._mint_lock_loop: asyncio.AbstractEventLoop | None = None

    def _lock_for_this_loop(self) -> asyncio.Lock:
        """This loop's mint lock, replacing one left bound to a loop that has gone.

        A contended :class:`asyncio.Lock` binds to the loop that waited on it and refuses a
        second, so the lock belongs to the running loop rather than to the run.  No ``await``
        between the check and the assignment, so two tasks of one loop cannot both install a
        lock and serialize against different objects.
        """
        loop = asyncio.get_running_loop()
        if self._mint_lock is None or self._mint_lock_loop is not loop:
            self._mint_lock = asyncio.Lock()
            self._mint_lock_loop = loop
        return self._mint_lock

    async def _user_identity(self) -> str | None:
        """This run's user authority, minted once successfully, or ``None`` if it could not be.

        Cached on success only: a mint that failed is a transient the next call may survive,
        while a mint that succeeded must not be repeated — one run, one identity is the bound
        the whole mechanism rests on.  Calls of one run may overlap, so the mint is serialized
        and the cache re-read inside the lock: without that, two callers both find it empty,
        both mint, and two authorities exist for the run that promised one.
        """
        if self._minted_user_identity is not None:
            return self._minted_user_identity
        mint = self._registry.mint_user_identity
        if mint is None:  # pragma: no cover - `call` refuses before reaching this
            return None
        # Both awaits below are cancellable, and `call` promises a cancelled call leaves a
        # record, so the boundary covers the lock as well as the minter — a waiter cancelled
        # while queued behind another call's mint reaches neither handler otherwise.
        reached_the_minter = False
        try:
            async with self._lock_for_this_loop():
                # The second read. Whoever held the lock may have filled it, and that identity
                # is this run's — minting a second beside it is the failure this method exists
                # to avoid, not a cache miss to satisfy.
                if self._minted_user_identity is not None:
                    return self._minted_user_identity
                try:
                    reached_the_minter = True
                    pending = cast(object, mint(self._run_id))
                    if not inspect.isawaitable(pending):
                        # A configuration error, and worth saying so rather than letting
                        # `await` raise a TypeError this folds into "the token service
                        # failed". The two send a host to different places. By type, never
                        # value: a synchronous minter has already returned its credential.
                        self._logger.warning(
                            "mint_user_identity returned %s rather than an awaitable, so the "
                            "user's identity for run %r cannot be used: it must be an async "
                            "callable",
                            type(pending).__name__,
                            self._run_id,
                        )
                        return None
                    minted = await pending
                except Exception as exc:  # noqa: BLE001 - the guest gets a sentence, the log the rest
                    # `CancelledError` is a `BaseException` and passes straight through this
                    # to the boundary below, which is where the two cancels are told apart.
                    self._logger.warning(
                        "minting the user's identity for run %r failed: %s",
                        self._run_id,
                        error_detail(exc),
                    )
                    return None
                # A minter that answers with something unusable is the same failure as one
                # that raised, and worth the same refusal: an empty string or a non-string
                # reaching a tool body as its authority is how a call runs with no authority
                # at all and nobody notices.
                answered = cast(object, minted)
                if not isinstance(answered, str) or not answered:
                    # The type, never the value. A misconfigured minter that answers with
                    # `bytes` is answering with a real token, and `%r` would write it to the
                    # host's log — a secret is sensitive whether or not it satisfies this
                    # contract.
                    self._logger.warning(
                        "minting the user's identity for run %r answered with %s, which is "
                        "not a usable identity",
                        self._run_id,
                        (
                            "an empty string"
                            if isinstance(answered, str)
                            else type(answered).__name__
                        ),
                    )
                    return None
                self._minted_user_identity = answered
                return answered
        except asyncio.CancelledError:
            # Two cancels, and only one of them may have spent anything: a call cut off inside
            # the host's minter can leave a credential nobody will use, while one cut off
            # waiting for the lock never reached it. Saying so is the difference between a
            # record an operator must chase and one they can read past.
            self._logger.warning(
                "host tools: minting the user's identity for run %r was cancelled %s",
                self._run_id,
                (
                    "inside the host's minter — a credential may already have been issued for "
                    "a call that will not run"
                    if reached_the_minter
                    else "while it waited for this run's mint, so no minter ran"
                ),
            )
            raise

    @property
    def run_id(self) -> str:
        """This run's identifier — the caller's, or a fresh random one if none was given.

        The identity a ``host_tool_calls_observer`` attributes a call by: object identity
        works within a
        process but is neither loggable nor stable, and this is. Unique per run unless a caller
        deliberately reuses one.
        """
        return self._run_id

    @property
    def key(self) -> SandboxKey | None:
        """The sandbox this run's program is executing in, or ``None`` where none was given."""
        return self._key

    @property
    def registry(self) -> HostToolRegistry:
        """The registry this run resolves through, whose ceilings a transport also answers to.

        Read-only, and read by :func:`~maf_sandbox.host_tool_calls_over_exec` for one reason:
        the size
        a response may be is the size a *request* may be, and a transport inventing its own
        number would be a second ceiling for one concern.
        """
        return self._registry

    async def call(
        self, name: str, arguments: Mapping[str, Any] | None = None, *, framing_bytes: int = 0
    ) -> HostToolCallResult:
        """Resolve, gate, validate, call and cap — the whole contract, at the one door.

        Every call counts toward the run's cap, refused ones included: a guest
        probing names or replaying refusals is spending the budget the cap exists to bound.
        Exhaustion is a refusal rather than an exception so the guest program finishes and
        reports what it has, instead of dying mid-way with the reason lost.

        **Cancellation is prompt, and a cancelled call is recorded** (#355).  Nothing here
        shields a cancel: a host tool is unbounded, so an uncancellable section would honour a
        caller's cancel only after arbitrary third-party code chose to return. The ledger stays
        consistent whichever await it lands on — nothing was delivered, the slot is returned —
        and each is logged rather than left as the one outcome with no trace.

        **Two awaits can take it, and they leave different things behind.** Inside the tool's
        body, a sink's outward effect may already have fired. Inside
        ``mint_user_identity``, for a :data:`~maf_sandbox.Identity.USER` tool, the body has not
        run at all but a credential may already have been issued for a call that never will —
        including for a call that was only queued behind another's mint, which spends nothing.
        The record says which, because an operator chasing the wrong one wastes the trail.

        A host that needs to *act* on it — retry, compensate — keys on the registry's
        ``host_tool_calls_observer``, whose context exit receives the same ``CancelledError``.

        Args:
            name: The registered tool to call. Guest text — checked, never trusted.
            arguments: Its keyword arguments, as the guest's JSON parsed.
            framing_bytes: What the transport wraps around ``value_json`` before it crosses
                the boundary — an envelope, a length prefix, nothing. Counted against both
                ceilings and committed with the payload, because what a cap bounds is the
                bytes that cross rather than the ones the host happened to serialize. A
                transport that declares it here gets a refusal *before* the ledger is spent;
                one that checks the total itself can only convert a committed success
                afterwards, which leaves the run paying for a response nobody received.
        """
        # A transport's number rather than a guest's, so both checks raise instead of refusing.
        _refuse_non_integer("framing_bytes", framing_bytes)
        if framing_bytes < 0:
            # A negative overhead would widen every ceiling below it by that much.
            raise ValueError(f"framing_bytes must not be negative, got {framing_bytes}")
        # The framing checks above raise before the observation begins: a transport's
        # programming error is not a host-tool call, so the observer sees no enter for it —
        # and no record either, for the same reason.
        if self._registry.observer is None:
            with _observe(self._registry.host_tool_calls_observer, self, name, self._logger):
                return await self._run_host_tool_call(name, arguments, framing_bytes=framing_bytes)
        called = _Called()
        started = time.monotonic()
        outcome: HostToolOutcome = "failed"
        refusal: str | None = None
        try:
            with _observe(self._registry.host_tool_calls_observer, self, name, self._logger):
                result = await self._run_host_tool_call(
                    name, arguments, framing_bytes=framing_bytes, called=called
                )
        except asyncio.CancelledError:
            # Told apart from any other escape, because only this one may have left an outward
            # effect behind: a `_deliver` cancelled inside the tool's body has already run it.
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        else:
            outcome = "delivered" if result.ok else "refused"
            refusal = result.refusal
            return result
        finally:
            record(
                self._registry.observer,
                HostToolCalled(
                    run_id=self._run_id,
                    key=self._key,
                    tool=called.tool,
                    declared=called.declared,
                    source=called.source,
                    sink=called.sink,
                    identity=called.identity,
                    outcome=outcome,
                    refusal=refusal,
                    response_bytes=called.delivered_bytes,
                    calls=self._calls,
                    seconds=time.monotonic() - started,
                    call=self._call,
                ),
                self._logger,
            )

    async def _run_host_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        framing_bytes: int = 0,
        called: _Called | None = None,
    ) -> HostToolCallResult:
        """The guest-answerable half of :meth:`call`, split so an observer can wrap it
        whole — from the first count, refusals included, to the slot's return — without
        re-indenting the body.

        Args:
            name: The tool to call, as given; a string on every call that resolves.
            arguments: Its keyword arguments, as the guest's JSON parsed.
            framing_bytes: What the transport wraps around ``value_json`` before it crosses.
            called: Filled in as soon as the name resolves and its declaration is read, so a
                record can say what was called however the call then ends. ``None`` where the
                registry records nowhere and there is nothing to fill in.
        """
        self._calls += 1
        cap = self._registry.max_host_tool_calls_per_run
        if self._calls > cap:
            return _refused(
                f"Error: this run's host-tool-call cap ({cap}) is exhausted — finish "
                "with the results already delivered and report what remains undone"
            )
        # The same cast-and-check the arguments get below, and for the same reason: a
        # transport hands over whatever the guest's JSON parsed to. A name that arrived as an
        # array or an object is unhashable, and the registry's dict lookup would raise out of
        # this method rather than answer with a sentence. The type is named and the value is
        # not — it is guest text, and this refusal lands in a transcript.
        requested = cast(object, name)
        if not isinstance(requested, str):
            return _refused(
                f"Error: a host tool name must be a string, not {type(requested).__name__}"
            )
        func = self._registry.resolve(name)
        if func is None:
            # The last refusal whose name is the guest's. Past this line `name` is a key the
            # host registered, so every later sentence quoting it is bounded by configuration.
            return _refused(f"Error: {_bounded(name)!r} is not a registered host tool")
        declaration = self._registry.declaration_for(name)
        if called is not None:
            called.tool = name
            called.declared = declaration is not None
            if declaration is not None:
                called.source = declaration.source
                called.sink = declaration.sink
                called.identity = declaration.identity
        if declaration is None and self._registry.require_declared:
            # Belt-and-braces behind the registration gate. Unreachable while the registry is
            # the only way in, and kept because the cost of being wrong about that is a tool
            # called unclassified.
            return _refused(
                f"Error: {name!r} carries no complete information-flow declaration, and "
                "this host calls declared tools only"
            )
        acts_as_user = declaration is not None and declaration.identity is Identity.USER
        if acts_as_user and self._registry.mint_user_identity is None:
            return _refused(
                f"Error: {name!r} exercises the user's identity, which this host cannot serve "
                f"— {_USER_IDENTITY_PREREQUISITES}"
            )
        # Cast to `object` because a transport hands over whatever the guest's JSON
        # parsed to — the annotation describes the contract, this check enforces it.
        given = cast(object, arguments)
        if given is not None and not isinstance(given, Mapping):
            return _refused(
                f"Error: arguments for {name!r} must be a JSON object of keyword arguments"
            )
        provided: dict[str, Any] = dict(arguments) if arguments is not None else {}
        if acts_as_user:
            if _USER_IDENTITY_PARAMETER in provided:
                # Before the mint, and a refusal rather than an overwrite: a guest naming this
                # argument is trying to choose the authority its own call runs under, and that
                # attempt is worth surfacing rather than silently correcting.
                return _refused(
                    f"Error: {_USER_IDENTITY_PARAMETER!r} is not an argument a caller may send "
                    f"to {name!r} — the host mints the identity this tool acts under"
                )
            # A placeholder, not the identity: minting is a real exchange with the host's
            # token service, and every refusal between here and the body — the ledgers below,
            # the signature, the binding — is one this call cannot come back from. Binding
            # still has to see the argument, or a body that requires it fails arity against
            # the guest. `_deliver` swaps it for the minted one once nothing is left to refuse.
            provided[_USER_IDENTITY_PARAMETER] = _UNMINTED
        limits = self._registry.response_limits
        # Both ledgers, before the call rather than after it. A sink tool's body runs in the
        # host process and does its work there; refusing once it has already run means the
        # effect happened, could not be reported, and the refusal reads like something to
        # retry. Every state below is knowable without the response: what stays in `_deliver`
        # is only what needs a size.
        if self._delivered + 1 > limits.max_files:
            return _refused(
                f"Error: this run's delivered-response cap ({limits.max_files}) is "
                "exhausted — finish with the results already delivered"
            )
        # The smallest thing that could still cross is the smallest payload *plus* whatever
        # the transport puts around it, so the framing decides exhaustion too.
        if self._delivered_bytes + _SMALLEST_RESPONSE + framing_bytes > limits.max_total_bytes:
            return _refused(
                f"Error: this run's response byte budget ({limits.max_total_bytes}) is "
                "exhausted — finish with the results already delivered"
            )
        if _SMALLEST_RESPONSE + framing_bytes > limits.max_bytes_per_file:
            # Also knowable without the response, and so it belongs here rather than beside
            # the size check in `_deliver`: a per-response cap that cannot hold one byte
            # inside the transport's framing can hold no response at all, and running the
            # tool first would mean a sink acting in the host process for a result nobody
            # can ever be handed. The registry refuses a cap below one byte; a transport's
            # framing is what can put an otherwise sane one out of reach.
            self._logger.warning(
                "host tools: the per-response cap (%d bytes) cannot hold a one-byte value "
                "inside this transport's %d bytes of framing, so nothing can be delivered",
                limits.max_bytes_per_file,
                framing_bytes,
            )
            return _refused(
                "Error: no host-tool response can fit this run's per-response cap — report "
                "this and carry on without host tools"
            )
        # Taken now and held across everything that can suspend — the mint and the body both —
        # because two concurrent calls would otherwise read a ledger that still said zero,
        # both run, and both deliver against a cap of one.
        self._delivered += 1
        delivered = False
        try:
            outcome = await self._deliver(name, func, provided, limits, framing_bytes, called)
            delivered = outcome.ok
            return outcome
        finally:
            # `finally` rather than a check on the outcome, because a cancelled call has no
            # outcome to check: `CancelledError` is a `BaseException` and walks straight past
            # one. Nothing was delivered either way, so the slot goes back. The *call*
            # count above stays spent — the attempt happened, and that is what it bounds.
            if not delivered:
                self._delivered -= 1

    async def _deliver(
        self,
        name: str,
        func: Callable[..., Any],
        provided: dict[str, Any],
        limits: TransferLimits,
        framing_bytes: int,
        called: _Called | None = None,
    ) -> HostToolCallResult:
        """Validate, call, serialize and cap, with a response slot already reserved.

        Split from :meth:`call` so that giving the slot back has exactly one site: every
        refusal here is a ``return`` the caller sees, and none of them can forget.  The byte
        ledger stays here instead, where a size is known — its check and its commit sit in one
        run of statements with no ``await`` between them, which is what makes it safe from the
        same interleaving (a caller driving one run from several *threads* is out of contract).
        """
        try:
            signature = inspect.signature(func)
        except Exception as exc:  # noqa: BLE001 - the guest gets a sentence, the log the rest
            # `register` accepts any callable, and introspecting one fails in more ways than
            # the two obvious errors: several built-ins expose no signature at all, and an
            # object whose `__signature__` is a property can raise anything it likes from it.
            # Nothing can be validated either way, so nothing is called.
            self._logger.warning(
                "host tool %r has no signature to validate against: %s", name, error_detail(exc)
            )
            return _refused(
                f"Error: host tool {name!r}'s signature could not be read, so its arguments "
                "cannot be validated"
            )
        try:
            # Host-side, at the one door, never in a guest shim: a schema check running
            # where model-written code can edit it is decoration. Binding proves the names
            # and arity; anything deeper is the tool body's own duty — it is host code.
            signature.bind(**provided)
        except TypeError as exc:
            # The detail is kept, bounded, rather than dropped: "unexpected keyword argument
            # 'x'" is how a model fixes its own call. The keyword in it is the guest's.
            return _refused(
                f"Error: arguments do not bind to host tool {name!r}: {_bounded(str(exc))}"
            )
        if provided.get(_USER_IDENTITY_PARAMETER) is _UNMINTED:
            # The last thing before the body, so the credential is spent only once nothing
            # deterministic can still refuse this call.
            minted = await self._user_identity()
            if minted is None:
                return _refused(
                    f"Error: the user's identity could not be minted for this run, so {name!r} "
                    "was not called — the reason is in the host's log"
                )
            provided[_USER_IDENTITY_PARAMETER] = minted
        try:
            result = func(**provided)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            # Only a cancel past this line can have begun an outward effect, so only this one
            # is mid-effect. Nothing was delivered either way.
            self._logger.warning(
                "host tools: the call of %r was cancelled mid-effect — nothing was delivered, "
                "but any outward effect the tool had begun is not recorded and may have "
                "completed",
                name,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - the guest gets a sentence, the log the rest
            self._logger.warning("host tool %r failed: %s", name, error_detail(exc))
            return _refused(f"Error: host tool {name!r} failed — the reason is in the host's log")
        try:
            # `allow_nan=False` because Python's default emits bare NaN/Infinity, which no
            # strict JSON parser on the guest side accepts — a payload delivered as success
            # and unreadable on arrival is worse than the refusal the ValueError becomes.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            # Encoded inside the same guard, and the guard is broad, because turning a value
            # into bytes fails in more ways than `dumps` alone does: `ensure_ascii=False` can
            # leave a lone surrogate in the text and encoding one raises, while a deeply
            # nested result raises `RecursionError` out of `dumps` itself. Either escaping
            # here would take the caller's whole turn instead of ending one call.
            size = len(encoded.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - the guest gets a sentence, the log the rest
            self._logger.warning(
                "host tool %r returned an unserializable value: %s", name, error_detail(exc)
            )
            return _refused(
                f"Error: host tool {name!r} returned a value that cannot be carried as JSON"
            )
        # The framing the transport declared is part of every number below. Checking the
        # payload and committing the payload would leave the difference uncounted in both
        # legs, and a transport left to police the shortfall itself can only do so after this
        # method has already committed the success.
        crossing = size + framing_bytes
        if crossing > limits.max_bytes_per_file:
            return _refused(
                f"Error: host tool {name!r}'s response is {crossing} bytes and the "
                f"per-response cap allows {limits.max_bytes_per_file}"
            )
        if self._delivered_bytes + crossing > limits.max_total_bytes:
            return _refused(
                f"Error: delivering host tool {name!r}'s {crossing}-byte response would exceed "
                f"this run's total response cap ({limits.max_total_bytes} bytes)"
            )
        self._delivered_bytes += crossing
        if called is not None:
            called.delivered_bytes = crossing
        return HostToolCallResult(value_json=encoded)
