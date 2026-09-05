"""The MAF glue: wiring this sandbox stack into an ``agent_framework`` host.

Everything else in :mod:`maf_sandbox` is protocol and policy written against the standard
library and nothing else — that is what lets a backend, a workload and a host application
share one vocabulary without sharing a dependency.  This module is the deliberate exception
and the only one: it is where ``agent_framework`` is imported, so a host gets the MAF-shaped
conveniences from a single place while every other module keeps its zero-dependency claim.
Both halves are pinned by tests (``TestZeroDependencies`` and ``TestMafIsTheOnlyMafImporter``
in ``tests/test_sandbox_router.py``).

It is **not** re-exported from the package's ``__init__``, on purpose: ``import maf_sandbox``
has to stay cheap and MAF-free for a backend, a workload's own test suite, or anything else
that only speaks the protocol.  Reach it by name — ``from maf_sandbox.maf import ...``.

Nine things live here, and each of them had begun to exist twice before it did:

- :func:`make_caller_context` — how a host says who is calling and which files they own.
- :func:`sandboxed_tool` — the shape every sandbox workload's tool has: attach nothing when
  no backend is configured, key the sandbox from the host's request context rather than from
  model input, and turn a provider failure into a sanitized sentence the model may see plus
  a detailed line only the log gets.
- :class:`~maf_sandbox.SandboxPurger`, re-exported — a host wiring a MAF surface needs the
  thread-delete participant at the same moment it needs the two above.
- :func:`list_all_files` and :func:`list_no_files` — the listing a caller context is built
  from, walked from ``list_children`` or declared empty. They are here rather than in core
  because the walk reads ``FileStoreEntry.type``, which the framework owns.
- :func:`labelled_result_item` — one item of a result a kind splits, carrying its own
  integrity label. Here because the item is an ``agent_framework`` ``Content``.
- :func:`argument_provenance_middleware`, :func:`positions_holding_hidden_content` and
  :func:`hidden_content_candidates` — which of a call's arguments the host's information-flow
  middleware rewrote, so a refusal names a position rather than quoting content the framework
  hid. Here because the answer comes from that middleware.
- :func:`file_store_provenance_middleware` — records an agent-driven file-store write into a
  host's :class:`~maf_sandbox.FileStoreProvenance`. Here because it is a ``FunctionMiddleware``;
  the record it fills is stdlib-only and lives beside the protocol vocabulary.
- :func:`make_file_store_sink` — an output sink landing each call's artifacts in a folder of an
  ``AgentFileStore``, so the model reads them back through the host's own file tools instead of
  through the workload's result. Here because the destination is the framework's store.
- :func:`sandbox_outputs_read_tools` — the two read-only tools that expose such a store to the
  model. Here because ``FileAccessProvider`` cannot: it names its tools from fixed constants, so
  a second one of those is a name collision rather than a second store.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import math
import posixpath
import string
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from ._containment import CONTAINED, escapes_containment
from ._error_detail import error_detail
from ._file_provenance import FILE_STORE_WRITE_TOOLS, PATH_ARGUMENT, FileStoreProvenance
from ._observer import (
    SandboxObserver,
    StoreFileRead,
    StoreReadOutcome,
    ToolCallEnded,
    record,
)
from ._outputs import (
    Artifact,
    LandedArtifact,
    OutputSink,
    SandboxLandingExists,
    SandboxLandingNotText,
    landing_outputs,
    missing_sink_refusal,
    spec_lands_artifacts,
)
from ._protocol import (
    CallerContext,
    Capability,
    DisposalFailure,
    Egress,
    IsolationScope,
    ListedFile,
    Sandbox,
    SandboxKey,
    SandboxSpec,
    SourceChannel,
    SourceIntegrity,
    weakest_integrity,
)
from ._purger import SandboxPurger
from ._reclaim import (
    DisposalOutcome,
    FailedReclaimPolicy,
    ReclaimFailure,
    close_unclean_notes,
    open_unclean_notes,
    reclaim_guest_path,
)
from ._refusals import echoed_name
from ._router import (
    ATTACH_REFUSALS,
    NoSandboxBackend,
    SandboxRouter,
    SandboxUnclean,
    Selection,
)

if TYPE_CHECKING:
    from agent_framework import Content

#: The declaration key naming the scope a sandbox tool is served at. Written only for
#: :data:`~maf_sandbox.IsolationScope.CALL`, and read by a host's own policy: the framework's
#: flow module knows the two FIDES keys beside it and not this one.
ISOLATION_SCOPE_KEY = "sandbox_isolation_scope"

#: Fallback for :func:`sandboxed_tool`'s ``logger`` argument. Named apart from the usual
#: module-level ``logger`` because that argument is the whole point: a workload passes its
#: own logger so the failure ladder's records keep the workload's logger name, and only a
#: caller that does not care lands here.
_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_OUTPUTS_TOOL_PREFIX",
    "ISOLATION_SCOPE_KEY",
    "SOURCE_INTEGRITY_PROPERTY",
    "file_store_provenance_middleware",
    "SandboxPurger",
    "SandboxToolSession",
    "labelled_result_item",
    "list_all_files",
    "list_no_files",
    "make_caller_context",
    "make_file_store_sink",
    "sandbox_outputs_read_tools",
    "sandbox_tool_declarations",
    "argument_provenance_middleware",
    "hidden_content_candidates",
    "positions_holding_hidden_content",
    "sandboxed_tool",
]

# The three sentences a workload is allowed to hand the model when it could not get a
# sandbox.  Fixed text, not a formatted exception: an SDK or transport failure's own message
# carries endpoint, subscription and tenant ids, and a tool result is persisted into the
# transcript, so what the model sees must say that the run degraded and nothing else.  The
# detail goes to the log instead (see :meth:`SandboxToolSession.acquire`).
#
# "T0" is this stack's shorthand for the ungrounded tier — the model checking its own work,
# which is exactly what a host falls back to when the sandbox is gone.
_SDK_NOT_INSTALLED = "Error: the sandbox backend is not installed — degrading to T0"
_NO_BACKEND_CONFIGURED = "Error: no sandbox backend is configured — degrading to T0"
_SANDBOX_UNAVAILABLE = "Error: sandbox unavailable — degrading to T0 (LLM self-check only)"
_SANDBOX_REFUSED = (
    "Error: this workload was refused before it ran — degrading to T0 (LLM self-check only). "
    "The reason is in the host log."
)
_SANDBOX_UNCLEAN = (
    "Error: the sandbox for this conversation is closed: a previous call left it unclean — data "
    "that could not be removed, or a program that may still be running — and it could not be "
    "disposed. Nothing runs in it until it is."
)

#: What the removal gets when the body was cancelled, instead of the full ``reclaim_timeout``.
#:
#: The `finally` still runs after a cancellation, and its ``await`` is still allowed to
#: complete — so a slow backend would extend a deadline that has *already* expired by the whole
#: bound. A grace rather than a skip: skipping leaks the call's path with nothing said, which is
#: the failure this module exists to prevent, and a cancelled caller is owed promptness rather
#: than nothing.
_CANCELLED_CALL_GRACE = 2.0


@dataclass
class _SandboxToolCall:
    """What one tool call has done that the ``finally`` has to undo.

    ``owner`` is the session whose wrapper opened the call. One `ContextVar` serves every
    binding in the process, so a body that reaches a *second* session would otherwise record
    that session's sandbox here and have its own path removed from the wrong one.
    """

    owner: object
    name: str | None = None
    #: Every sandbox this call acquired, keyed, and *every* wrapper per key rather than the last.
    #: `acquire` takes a key, so one call can reach two sandboxes and write its name into both —
    #: keeping only the newest would reclaim one of them and say nothing about the other. And a
    #: call that reacquires one key (a transport timeout caught and retried) gets a fresh wrapper
    #: each time from Docker/WSLC/ACAS: the removal runs once per key on the live wrapper, but an
    #: unclean note names whichever wrapper ran the stop, so all of them have to be kept to match
    #: it — dropping an earlier one reuses a sandbox whose program a stop may not have taken down.
    acquired: dict[SandboxKey, list[Sandbox]] = field(
        default_factory=dict[SandboxKey, list[Sandbox]]
    )
    #: Every key this call *touched*, in order — asked to acquire, served or refused, and read
    #: the host's store under. Kept apart from `acquired`, which is the cleanup ledger and may
    #: only name a key something has to delete: a refused acquire has nothing to reclaim, and
    #: registering one there would have the removal sweep a sandbox this call never got. This is
    #: what a record joins on, so a refused acquire — and a call that read the store and
    #: returned before acquiring anything — still names the key its other events carry.
    touched: list[SandboxKey] = field(default_factory=list[SandboxKey])
    closed: bool = False


#: The call a tool body is running inside, or ``None`` outside one.
#:
#: Not an attribute on the session: one session serves every concurrent call to its tool, so two
#: parallel calls would be handed the same path and the first to finish would remove one the
#: other is still running in. A task starts from a copy of its parent's context, so a child
#: reads the record and cannot reach a sibling's. A child outliving the call keeps that copy, so
#: the record is *closed* before the removal runs and asking it for a path then raises: anything
#: such a task wrote afterwards would sit in the sandbox with nothing left to reclaim it.
_CALL: ContextVar[_SandboxToolCall | None] = ContextVar("maf_sandbox_call", default=None)


@dataclass
class _CallProvenance:
    """The framework's record of one call, and whether that call has returned."""

    context: Any
    closed: bool = False


#: The framework's record of the call a tool body is running inside, published by
#: :func:`argument_provenance_middleware` and ``None`` where a host has not wired it.
#:
#: A `ContextVar` for the same reason `_CALL` above is one: a task starts from a copy of its
#: parent's context, so concurrent calls each read their own record rather than whichever
#: finished last. And *closed* like `_CALL` for the other half of that: resetting the variable
#: does not reach a child's copy, so a task outliving the call would go on answering from
#: arguments that are no longer the ones being asked about. The flag is on the record the copy
#: shares, which is the only thing the parent can still reach.
_CALL_CONTEXT: ContextVar[_CallProvenance | None] = ContextVar(
    "maf_sandbox_call_context", default=None
)


def argument_provenance_middleware() -> Any:
    """Middleware that lets a tool body see its arguments as the framework first received them.

    A host's information-flow middleware may rewrite an argument before the body runs — a
    variable reference becomes the content it stands for — and the body is handed the result
    with no record of the substitution.  Wire this beside it and
    :func:`positions_holding_hidden_content` answers from that record rather than by inference.

    Order does not matter: middleware share one call context, so this publishes the same
    record whichever side of the chain it sits on.

    **It publishes the framework's call context, not an accessor.** That object is the one the
    framework already hands any tool body declaring a ``FunctionInvocationContext`` parameter,
    so nothing here widens what a body can reach.  Nothing *public* returns it: this factory
    answers with a stateless middleware, and :func:`positions_holding_hidden_content` answers
    with positions in the list it was given.

    **Any middleware that rewrites arguments must sit before the information-flow middleware**,
    whichever side this one is on: the record is taken there, so an edit made after it reads as
    content the framework substituted.

    Returns a middleware instance to add to an agent's ``middleware`` list::

        Agent(..., middleware=[LabelTrackingFunctionMiddleware(), argument_provenance_middleware()])
    """
    from agent_framework import FunctionMiddleware

    class _ArgumentProvenance(FunctionMiddleware):  # type: ignore[misc]
        async def process(self, context: Any, call_next: Any) -> None:
            record = _CallProvenance(context=context)
            token = _CALL_CONTEXT.set(record)
            try:
                await call_next()
            finally:
                _CALL_CONTEXT.reset(token)
                # Closed rather than only reset: a task the body left running holds its own copy
                # of the variable, and this is what that copy can still see.
                record.closed = True

    return _ArgumentProvenance()


def file_store_provenance_middleware(
    record: FileStoreProvenance, *, also_observes: frozenset[str] | set[str] = frozenset()
) -> Any:
    """Middleware that records an agent-driven file-store write into ``record``.

    Wire it beside the host's information-flow middleware, the way
    :func:`~maf_sandbox.argument_provenance_middleware` is wired::

        provenance = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
        Agent(..., middleware=[
            LabelTrackingFunctionMiddleware(),
            file_store_provenance_middleware(provenance),
        ])

    **Order does not matter, because the path is read after the body has run.** The
    information-flow middleware expands a variable reference in any string argument, the path
    included, and it edits the call's arguments in place — so reading afterwards sees the name
    the store was written under whichever side of that middleware this sits on. The name is then
    keyed through :func:`~maf_sandbox.store_key`, because the provider normalises before it
    writes. What this never needs is the content's own label: a write reaching these tools is
    model-driven however the content got there, so unlike
    :func:`~maf_sandbox.positions_holding_hidden_content` no private framework record is read.

    **The entry is written in a ``finally``**, because a body can commit to the store and then
    raise, and an entry is what stops those bytes answering the host's floor.  What an entry
    then means, and why that survives concurrent writes to one path, is
    :meth:`~maf_sandbox.FileStoreProvenance.record`'s to say.

    **A recorded write is not the same as a successful one, and a delete is recorded too.** The
    tools answer a refusal with a *string* rather than raising, so nothing here can tell a write
    that landed from one that was refused — and the same is true of a delete. Every observed call
    therefore marks its path untrusted, which is the conservative direction in both cases: a
    refused write marks a path the model did not change, and a failed delete keeps the entry for
    bytes that are still there. Forgetting a path on a delete would do the opposite, returning it
    to a trusted floor while the model's content remained, so the middleware never calls
    :meth:`FileStoreProvenance.forget` — that is the host's, for when it can establish removal.

    **A trusted floor is refused without this, and building this is what lifts it.** Constructing
    the middleware marks the record, and
    :meth:`~maf_sandbox.FileStoreProvenance.integrity_of` refuses a ``TRUSTED`` floor on a
    record nothing was ever built against — where no write is observed every path answers the
    floor, model-written ones included. It proves construction rather than wiring: a host that
    builds this and never adds it to the chain is past what a record can see.

    Args:
        record: Where observed writes land, and what a kind reads back.
        also_observes: Extra tool names to treat as file-store writes, for a host that wires a
            write surface of its own. Each must name its path in a ``file_name`` argument.
    """
    from agent_framework import FunctionMiddleware

    # Reaching past the record's own surface, deliberately: the marker is private so that
    # constructing this factory stays the only supported way to lift the trusted-floor
    # refusal, and the record cannot expose it publicly without becoming the escape hatch it
    # exists to close. The two cannot live in one module — the record is stdlib-only and this
    # imports `agent_framework`.
    record._note_observer()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    observed = FILE_STORE_WRITE_TOOLS | frozenset(also_observes)

    class _FileStoreProvenance(FunctionMiddleware):  # type: ignore[misc]
        async def process(self, context: Any, call_next: Any) -> None:
            name = getattr(getattr(context, "function", None), "name", None)
            if name not in observed:
                await call_next()
                return
            try:
                await call_next()
            finally:
                # Read here rather than before `call_next`: the path must be the expanded one.
                path = _store_path_named_by(context)
                if path is None:
                    _DEFAULT_LOGGER.warning(
                        "file_store_provenance_middleware: %r ran without a %r argument, so "
                        "the path it wrote is unknown and nothing was recorded for it.",
                        name,
                        PATH_ARGUMENT,
                    )
                else:
                    record.record(path)

    return _FileStoreProvenance()


def _write_call_arguments(context: Any) -> Mapping[str, Any] | None:
    """The call's arguments as a mapping, or ``None`` where they are not one.

    A call's arguments are a mapping *or* a model, and the framework keeps whichever it was
    given, so a model is dumped before it is read — duck-typed, as
    ``_spellings_before_rewriting`` does it, because this package does not depend on the
    framework's validation library.
    """
    arguments: Any = getattr(context, "arguments", None)
    dump = getattr(arguments, "model_dump", None)
    if callable(dump):
        arguments = dump()
    if not isinstance(arguments, Mapping):
        return None
    return cast("Mapping[str, Any]", arguments)


def _store_path_named_by(context: Any) -> str | None:
    """The store path this call names, or ``None`` where it names none this can read."""
    arguments = _write_call_arguments(context)
    if arguments is None:
        return None
    path: Any = arguments.get(PATH_ARGUMENT)
    return path if isinstance(path, str) and path else None


def _reachable_middleware() -> Any | None:
    """The host's information-flow middleware, or ``None`` where none is reachable."""
    try:
        from agent_framework.security import get_current_middleware
    except ImportError:  # pragma: no cover - a host without the security module
        return None
    return get_current_middleware()


#: Where the framework keeps a call's arguments as they arrived, before it expands any
#: reference into them.  **Not a published contract** — a string literal inside
#: `LabelTrackingFunctionMiddleware`, and this package accepts every ``agent-framework-core``
#: 1.x — so a compatible minor may rename it and this would stop answering.  Two things keep
#: that from being silent: a divergence alarm in the suite, and, for a host whose upgrade this
#: suite never saw, `_warn_once_about_a_missing_record` beside an answer that names every
#: position rather than quoting one.
#: Retiring both needs a provenance API the framework publishes, which is #826.
_ORIGINAL_ARGUMENTS_KEY = "original_arguments_for_messages"

#: Another key that middleware writes on every call, before the one above and for a different
#: reader.  Present-without-the-other is the tell this package needs: it says an information-flow
#: middleware ran and its argument record is gone, which no legitimate wiring produces.  Read
#: from the call rather than from the framework's accessor deliberately — metadata travels with
#: the context object, so this answers on the worker thread a synchronous body runs on, where
#: the accessor is a thread-local and answers nothing.
_MIDDLEWARE_RAN_KEY = "context_label"

#: One warning per process, not one per refusal. Guarded, because the path this exists for is
#: the one that runs off the event loop: `asyncio.to_thread` gives each synchronous body a
#: pool thread, so two can read the flag before either sets it.
_warned_about_a_missing_record = False
_warning_lock = threading.Lock()


def _warn_once_about_a_missing_record(logger: logging.Logger) -> None:
    """Say that the framework stopped keeping the record, where that is what it must mean.

    Called where a call carries `_MIDDLEWARE_RAN_KEY` and not `_ORIGINAL_ARGUMENTS_KEY` — see
    those two, and :func:`_the_framework_kept_no_record`, for why that pairing is the tell.
    """
    global _warned_about_a_missing_record
    with _warning_lock:
        if _warned_about_a_missing_record:
            return
        _warned_about_a_missing_record = True
    # Logged outside the lock: a handler is arbitrary host code and may be slow or re-entrant.
    logger.warning(
        "argument_provenance_middleware: this agent-framework-core no longer records %r on a "
        "call, so which arguments it rewrote can no longer be answered. Every checked value is "
        "being named by its position instead of quoted, which is safe and noisy. See "
        "https://github.com/sokolaidev/maf-extensions/issues/826",
        _ORIGINAL_ARGUMENTS_KEY,
    )


def _framework_metadata(context: Any) -> Mapping[str, Any]:
    """What the framework left on this call, or empty where it left nothing."""
    return cast("Mapping[str, Any]", getattr(context, "metadata", None) or {})


def _the_framework_kept_a_record(context: Any) -> bool:
    """Whether this call carries the framework's record of the arguments it received."""
    return _ORIGINAL_ARGUMENTS_KEY in _framework_metadata(context)


def _the_framework_kept_no_record(context: Any) -> bool:
    """Whether a middleware ran on this call and left no record of the arguments it received.

    The two keys are written together, so one without the other is the framework's contract
    having moved rather than a host that wired no information-flow middleware at all.
    """
    metadata = _framework_metadata(context)
    return _ORIGINAL_ARGUMENTS_KEY not in metadata and _MIDDLEWARE_RAN_KEY in metadata


def _spellings_before_rewriting(context: Any, argument: str) -> list[str] | None:
    """``argument``'s values as the caller spelled them, before the framework rewrote any.

    ``None`` where the record is absent — no information-flow middleware ran, so nothing was
    rewritten — and equally where it holds neither a string nor a list for ``argument``, which
    means a caller named a parameter this call does not have and must not be read as "nothing
    was rewritten".

    **A ``str`` is one spelling, not a sequence of characters.**  A tool whose parameter is a
    single path has one value to check, and reading its argument as "no list under this name"
    would fail it closed on every call — hiding a path the caller spelled itself.

    A call's arguments are a mapping *or* a model, and the framework keeps whichever it was
    given, so a model is dumped before it is read.  Duck-typed rather than imported: this
    package does not depend on the framework's validation library.
    """
    original: Any = _framework_metadata(context).get(_ORIGINAL_ARGUMENTS_KEY)
    dump = getattr(original, "model_dump", None)
    if callable(dump):
        original = dump()
    if not isinstance(original, Mapping):
        return None
    spellings: Any = cast("Mapping[str, Any]", original).get(argument)
    if isinstance(spellings, str):
        return [spellings]
    if not isinstance(spellings, (list, tuple)):
        return None
    return [
        item if isinstance(item, str) else str(item) for item in cast("Sequence[Any]", spellings)
    ]


def _awaits(body: object) -> bool:
    """Whether calling ``body`` gives something to await.

    An instance with an async ``__call__`` is as awaitable as a coroutine function, and only its
    ``__call__`` is the coroutine function :mod:`inspect` can see — the same reading
    ``_host_tools`` makes of a host-tool-call observer.
    """
    return inspect.iscoroutinefunction(body) or inspect.iscoroutinefunction(
        getattr(body, "__call__", None)
    )


def _this_call(owner: object) -> _SandboxToolCall | None:
    """The call ``owner`` is running inside, or ``None`` — including when it belongs elsewhere."""
    call = _CALL.get()
    return call if call is not None and call.owner is owner else None


def _call_name(call: _SandboxToolCall) -> str:
    """This call's own id, allocated on first use and fixed from then on.

    One whole id serves both the guest path and the key, because a call-scoped sandbox and the
    directory inside it name the same call; two would say they were two, and two calls colliding
    on one are two calls get-or-create hands the same sandbox.
    """
    if call.name is None:
        call.name = uuid4().hex
    return call.name


def _prefixed(name: str) -> str:
    """``name``, safe to bake into a logging format string.

    The tool's name prefixes every record this module writes, and it is baked into the FORMAT
    rather than passed as an argument so the record is indistinguishable from one the workload
    wrote by hand — ``record.msg`` included, which is what a structured exporter reads and what
    a caplog assertion matches. A ``%`` in a name would then read as a format specifier.
    """
    return name.replace("%", "%%")


def _reduced_form(payload: object) -> object:
    """What the middleware substitutes for ``payload`` when it expands a reference to it.

    A mapping, or JSON text naming a ``response``, is reduced to that field.  **Everything else
    is substituted unchanged**, which is the branch that matters most here: a payload of any
    other type still reaches an argument, as ``str()`` of itself, once the reference is spliced
    into surrounding text.  So this always answers with something, and "no reduction" is the
    payload rather than an absence — there is no shape a caller should skip.

    **It mirrors behaviour rather than a published contract, so it has to track upstream.** The
    rule lives inside ``agent_framework.security`` (MIT, Microsoft Corporation), which promises
    nothing about it, and a shape this stops matching is a shape an argument carries past the
    check. ``THIRD-PARTY-NOTICES.md`` records the reuse.
    """
    if isinstance(payload, Mapping):
        mapping = cast("Mapping[str, Any]", payload)
        return mapping["response"] if "response" in mapping else mapping
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                # `RecursionError` is not a `ValueError`, and this walks the whole store: a
                # payload nothing referenced must not end the call that asked about another.
                return payload
            if isinstance(parsed, dict) and "response" in parsed:
                return cast("dict[str, Any]", parsed)["response"]
    return payload


def _hidden_payloads(middleware: Any) -> Iterator[str]:
    """Every string form a rewritten argument could have arrived carrying.

    Two forms per stored payload, because a reference is expanded two ways.  Alone, it is
    replaced by the payload itself; spliced into surrounding text, by ``str()`` of what the
    reduction answers — so a payload of any type reaches an argument as text, and a stored
    ``["SECRET"]`` arrives inside ``['SECRET'].bicep``.
    """
    store = middleware.get_variable_store()
    for variable_id in store.list_variables():
        try:
            content, _ = store.retrieve(variable_id)
        except KeyError:  # pragma: no cover - a store cleared between the two calls
            continue
        reduced = _reduced_form(content)
        candidates: set[str] = {content} if isinstance(content, str) else set()
        candidates.add(reduced if isinstance(reduced, str) else str(reduced))
        for text in candidates:
            if text:
                yield text


def hidden_content_candidates() -> frozenset[str]:
    """Every string form a rewritten argument could have arrived carrying, as the store holds it.

    Take this **before a body's first await** wherever the answer is needed later.  The
    framework's accessor is not scoped to the call, so an answer fetched after the body has
    suspended may not be available; a snapshot taken first thing survives the wait, and
    :func:`positions_holding_hidden_content` accepts it as ``candidates``.

    A body that asks and answers in the same breath does not need this — it can let
    :func:`positions_holding_hidden_content` take its own.
    """
    middleware = _reachable_middleware()
    if middleware is None:
        return frozenset()
    return frozenset(_hidden_payloads(middleware))


def positions_holding_hidden_content(
    values: Sequence[str],
    *,
    argument: str | None = None,
    candidates: frozenset[str] | None = None,
) -> frozenset[int]:
    """The positions in ``values`` the host's middleware expanded content it had hidden into.

    MAF's information-flow middleware rewrites a variable reference back into a tool's arguments
    **before** the body runs, so a value a kind is about to quote in a refusal may be content the
    framework hid rather than anything the model chose.  Pass the verdict to
    :func:`~maf_sandbox.echoed_name` as ``hidden`` and it renders the position instead.

    Answered two ways.  Where a host has wired :func:`argument_provenance_middleware` *and*
    ``argument`` names the parameter these values came from, each is compared with the spelling
    the caller gave at the same position: exact, and consulting no stored payload.  Otherwise it
    falls back to containment against the whole conversation's store, which is conservative — a
    store holding ``"main"`` reports an untouched ``"main.bicep"`` — and reports a value the
    caller chose that merely matches hidden content.  ``docs/sandbox/information-flow.md``
    carries why that difference matters.

    What a caller has to know:

    - **Index the answer.**  Two entries can arrive equal while only one was rewritten, so a
      verdict belongs to a position and never to a value.
    - **``argument`` is required for the exact answer, and names one call argument.**  Values
      that came from no argument — a manifest a program wrote — leave it unset, or the
      comparison is against a list the caller never sent.
    - **Containment, not equality.**  A reference is spliced into the text around it, so
      ``"[var_a1b2].bicep"`` arrives as the content with a suffix and equals no stored payload.
    - **A task outliving the call falls back, and reaches the store only through
      ``candidates``.**  The record is closed with the call, and the framework's own accessor
      goes with it.  Take that snapshot from :func:`hidden_content_candidates` before the first
      await; a caller answering immediately needs none.
    - **An empty answer from the fallback is not "nothing was hidden".**  It is also what an
      unreachable middleware gives, including for a synchronous body dispatched to another
      thread.  The record has neither limit — a ``ContextVar`` is copied by
      ``asyncio.to_thread``.

    Take the whole argument list in one call: the answer costs one pass over the variable store,
    and the store's own reads are logged by the framework.
    """
    if not values:
        return frozenset()
    record = _CALL_CONTEXT.get()
    if record is not None and not record.closed and argument is not None:
        if _the_framework_kept_no_record(record.context):
            # Fail closed. Something hid content on this call and the record of what it
            # rewrote is gone, so every entry is one this cannot vouch for. The fallback is
            # no answer here: a synchronous body runs on a thread the framework's accessor
            # does not reach, so it would report nothing and every value would be quoted.
            _warn_once_about_a_missing_record(_DEFAULT_LOGGER)
            return frozenset(range(len(values)))
        before = _spellings_before_rewriting(record.context, argument)
        # Per position, never against the record as a whole: an equal value at another position
        # would otherwise excuse this one.
        if before is not None and len(before) == len(values):
            return frozenset(
                position
                for position, (spelling, value) in enumerate(zip(before, values, strict=True))
                if value != spelling
            )
        if _the_framework_kept_a_record(record.context):
            # The record is here and this argument cannot be read out of it — a name that is no
            # parameter of this call, a value that is no longer a list, a length that no longer
            # matches. None of that says nothing was rewritten, so it must not answer as though
            # it did, and the fallback would: a synchronous body reaches no store from its
            # thread and would report an empty set, quoting whatever the framework had hidden.
            return frozenset(range(len(values)))
    payloads = hidden_content_candidates() if candidates is None else candidates
    return frozenset(
        position
        for position, value in enumerate(values)
        if any(payload in value for payload in payloads)
    )


def make_caller_context(
    list_files: Callable[[Any], Awaitable[list[ListedFile]]],
    scope_getter: Callable[[], str],
    thread_getter: Callable[[], str | None],
) -> CallerContext:
    """Build the :class:`~maf_sandbox.CallerContext` a host hands to a workload factory.

    Args:
        list_files: Given the file store, returns the files the caller may act on, each a
            :class:`~maf_sandbox.ListedFile` carrying the name and what the host knows about the
            bytes at it. A workload treats the names as its injection-pinning boundary — only a
            name present in the listing is ever substituted into a command — and reads the label
            beside each rather than inferring one from the name.
            :func:`list_all_files` builds this, and takes the host's ``provenance`` record so the
            labels are what the host knows rather than what a kind could guess.
        scope_getter: The caller's user/tenant scope, read at call time.
        thread_getter: The caller's conversation id, read at call time, or ``None`` when
            no conversation is bound.

    All three are **callables, not values**, and that is the load-bearing part rather than a
    convenience.  A sandbox is keyed by ``(scope, thread_id, agent_dir)``; if the first two
    were captured when the tool was built, one conversation could reach another's sandbox on
    a host that builds an agent once and serves many conversations with it.  Reading them per
    call keeps the key a property of the host's request context, which is also why nothing
    here accepts them from the model.

    Taking callables has a second consequence worth stating, because a host usually needs it:
    **this function forces no imports of its own**.  A host whose scope and thread live
    behind a heavy module (a web/hosting stack, say) writes three small functions that import
    lazily inside their own bodies, and building the context stays as cheap as the tuple it
    is.
    """
    return CallerContext(
        current_scope=scope_getter,
        current_thread_id=thread_getter,
        list_files=list_files,
    )


def _reaches_the_network(spec: SandboxSpec) -> bool:
    """Whether this workload can reach a host outside its sandbox.

    Both halves are load-bearing, and one predicate answers for the confidentiality cap and the
    trusted-claim refusal alike.  An ``unrestricted`` run names nothing and reaches everything,
    so the mode has to be read; an ``allowlist`` run with an empty list reaches nothing, so the
    payload has to be read too.
    """
    return spec.egress is Egress.UNRESTRICTED or bool(spec.egress_allow)


def _open_source_channels(spec: SandboxSpec) -> frozenset[SourceChannel]:
    """Every channel into this workload's result that ``spec`` opens, established or not.

    Read off the spec alone, so it answers before the sandbox exists and before any call.
    """
    opened = {SourceChannel.EGRESS} if _reaches_the_network(spec) else set[SourceChannel]()
    if Capability.FILES_IN in spec.requires:
        opened.add(SourceChannel.FILE_STORE)
    if Capability.HOST_TOOLS in spec.requires:
        opened.add(SourceChannel.HOST_TOOLS)
    return frozenset(opened)


def _host_tools_channel_is_established_as_trusted(spec: SandboxSpec) -> bool:
    """Whether the spec's own host-tool surface establishes that channel **as trusted**.

    Not the same as establishing it at all: a fold of ``UNTRUSTED`` establishes the channel,
    and answers ``False`` here, because a claim of ``trusted`` over a source known to be
    untrusted is refused for the same reason as one over a source nothing has settled.

    The fold is taken once, when a host seals its registry, and it rides on the spec — so this
    reads :attr:`~maf_sandbox.SandboxSpec.host_tools` rather than asking the kind for a copy
    that could disagree with it.

    Two of the fold's three states clear it, and **only** those two.  ``TRUSTED`` because every
    registered source is, and ``None`` because there is no source at all: an unstamped tool folds
    in as ``UNTRUSTED`` precisely so that ``None`` can never mean nobody answered.  Both rest on
    the host's own declaration that its tools bring nothing external in, which is the basis
    ``also_carries_out`` rests on too and which nothing here can check.  Anything else — an
    ``UNTRUSTED`` fold, or a value this package has no name for — clears nothing, so a member
    added to :class:`~maf_sandbox.SourceIntegrity` later does not become proof of trust by
    default.

    A spec requiring the capability while carrying **no** surface clears nothing.
    :class:`~maf_sandbox.SandboxSpec` refuses a surface without the capability and not the
    converse, so such a spec is legal — and its channel is open with no fold to answer for it.
    """
    if spec.host_tools is None:
        return False
    fold = spec.host_tools.result_integrity
    if fold is None:
        return True
    try:
        return SourceIntegrity(str(fold)) is SourceIntegrity.TRUSTED
    except ValueError:
        # A value this package has no name for clears nothing. `HostToolAggregate` is a public
        # frozen dataclass and its annotation binds nothing at runtime, so the fold reaching here
        # is whatever a host put in it.
        return False


def _source_channels_not_established_as_trusted(
    spec: SandboxSpec, cleared: frozenset[SourceChannel]
) -> frozenset[SourceChannel]:
    """The open channels this spec does not establish as trusted, after the caller's own claim.

    Both a channel nothing settles and one a fold settled as *untrusted* are in here: the claim
    being checked is ``trusted``, and neither licenses it.
    """
    unestablished = _open_source_channels(spec) - cleared
    if _host_tools_channel_is_established_as_trusted(spec):
        unestablished -= {SourceChannel.HOST_TOOLS}
    return unestablished


def _channel_clause(channel: SourceChannel, spec: SandboxSpec) -> str:
    """One open channel, named by the spec field and the value that opened it.

    The field rather than the channel alone, because a kind whose ``requires`` and
    ``egress_allow`` are assembled out of shared sub-specs is reading a refusal about a channel
    it never wrote.  Naming the field is what sends its author to the composition site.
    """
    if channel is SourceChannel.FILE_STORE:
        return f"the agent's file store (requires holds {str(Capability.FILES_IN)!r})"
    if channel is SourceChannel.EGRESS:
        if spec.egress is Egress.UNRESTRICTED:
            return f"the network (egress is {str(Egress.UNRESTRICTED)!r})"
        return f"the network (egress_allow names {', '.join(spec.egress_allow)})"
    if spec.host_tools is None:
        return (
            f"host tools (requires holds {str(Capability.HOST_TOOLS)!r} and the spec carries no "
            "registry fold)"
        )
    return f"host tools (the registry folds to {str(SourceIntegrity.UNTRUSTED)!r})"


def _unlicensed_trusted_claim_refusal(
    spec: SandboxSpec,
    unestablished: frozenset[SourceChannel],
    *,
    asked_by: str,
    through_mapping: bool,
) -> ValueError:
    """The refusal for a ``"trusted"`` claim over channels nothing establishes as trusted.

    ``unestablished`` holds both kinds: a channel nothing has settled, and one a registry fold
    settled as *untrusted*.  Neither licenses the claim, which is why they are refused together.

    Returned rather than raised so each caller keeps its own control flow visible, and written
    once so the keyword path and the verbatim-mapping path cannot drift into telling a host two
    different stories — the shape :func:`~maf_sandbox.missing_sink_refusal` already has.

    ``through_mapping`` picks the remedy, because the escape is a keyword and an explicit
    ``declarations=`` mapping is written verbatim with no keyword read beside it.
    """
    named = "; ".join(_channel_clause(channel, spec) for channel in sorted(unestablished))
    remedy = (
        "Drop the declarations= mapping and pass source_integrity= instead, where "
        "nothing_survives_from= is read beside it. While a mapping is given both keywords are "
        "ignored, so adding one without dropping the mapping would leave the tool declaring "
        "nothing at all. Or drop the 'trusted' claim from the mapping."
        if through_mapping
        else (
            f"Declare {str(SourceIntegrity.UNTRUSTED)!r}, or nothing, if anything from them may "
            "survive into the result. Only where none does — as text, as a number, or as the "
            "presence of a line — name each in nothing_survives_from, a claim written as given "
            "that nothing here can check."
        )
    )
    return ValueError(
        f"{asked_by}: the {spec.kind!r} workload declares "
        f"source_integrity={str(SourceIntegrity.TRUSTED)!r}, and its spec opens channels "
        f"nothing establishes as {str(SourceIntegrity.TRUSTED)!r}: {named}. A declaration "
        "replaces the call's input-label join, so anything reaching the result through those "
        f"would be labelled trusted on no such establishment. {remedy}"
    )


def sandbox_tool_declarations(
    spec: SandboxSpec,
    *,
    source_integrity: str | None = None,
    outbound_max_confidentiality: str | None = None,
    output_sink: OutputSink | None = None,
    also_carries_out: bool = False,
    nothing_survives_from: Iterable[SourceChannel] = (),
    isolation_scope: IsolationScope | None = None,
) -> dict[str, Any]:
    """The information-flow declarations a sandbox workload's tool carries.

    These land on the tool's ``additional_properties``, where MAF's information-flow module
    (``agent_framework.security``, FIDES) reads them before every call: ``source_integrity``
    is this tool's declaration about the integrity of its *results*, and
    ``max_allowed_confidentiality`` caps how confidential a conversation may be and still be
    allowed to call it.

    **Nothing is declared by default, and that is a delegation rather than a fail-safe.**  A
    declaration overrides rather than floors: the tracker discards the call's input-label join
    for it.  So ``"trusted"`` is honest only where the result does not derive from input the
    framework has not established as trusted — which authorship does not settle, a compiler
    being deterministic *about* a template the model wrote.  Undeclared, two things answer
    instead: the join, which propagates whatever labels the arguments carry, and the host's
    ``default_integrity`` where they carry none.  A workload that cannot vouch for its result
    says ``"untrusted"`` rather than leaving it to either.

    **An explicit ``"trusted"`` is refused where the spec opens a channel nothing establishes as
    trusted.**
    A spec names the channels its workload opens before the sandbox exists — the agent's file
    store it reads, a host its program may fetch from, a registry it may call back through —
    and of those only the registry can be established as trusted, by the fold a host seals onto
    :attr:`~maf_sandbox.SandboxSpec.host_tools`.  So a ``"trusted"`` claim over any of the
    others is a statement the framework will act on and nobody can check.  Where a channel is
    open but nothing from it
    survives into the result — host-authored fixtures written in, a fixed sentence written back
    — the caller says so with ``nothing_survives_from``.  Declaring nothing is never refused:
    there is no claim in it to refuse.

    **What that default costs is the model's sight of the result, not the host's sinks.**
    FIDES hides an untrusted result by default — the item is replaced by a variable reference
    the model can pass to another tool without reading — and hidden content does not taint
    the conversation's integrity, so later tools stay ungated.  Where a host has turned
    hiding off the result is visible instead, and the conversation does go untrusted.  Two
    limits on that trade: hiding stops once anything else has tainted the conversation, and
    it never applies to confidentiality, which a hidden item still contributes.
    ``docs/sandbox/information-flow.md`` carries the measurement and the full conditions.

    ``outbound_max_confidentiality`` is **opt-in, and off by default**, and the asymmetry is
    deliberate.  A confidentiality key is not inert metadata: writing one participates in a
    policy leg that may be dormant in the host — a host whose tools never label anything
    above its own cap has a confidentiality check that cannot currently fire — so declaring
    it can change which calls are gated or refused.  That is the host's decision to make with
    its own classification in hand, never a default a library picks.  When it *is* passed, the
    key is written only if this tool can carry something out at all: the spec permits egress
    (``egress_allow`` names hosts, or the run is ``unrestricted``), or the spec declares an
    output that **lands** in ``output_sink``.  Capping a workload with neither would gate calls
    for a flow that does not exist.

    The sink half of that condition is not symmetry for its own sake.  The rule was once
    ``egress_allow`` alone, on the premise that a sandbox with no network cannot carry anything
    out of the conversation — and a landing sink falsifies exactly that premise: with closed
    egress and a sink, guest bytes still reach host state, so the flow the cap gates is back.
    It takes **both** halves, though: a sink is ordinarily one object handed to every sandbox
    tool a host builds, so its mere presence says nothing about whether *this* workload sends
    anything down it.  A spec that declares no output, or only ``CONSUME`` ones, carries
    nothing to host state however many sinks it was given — while one setting
    ``outputs_named_at_call_time`` carries something without being able to say what, and counts
    here exactly as a declared ``LAND`` output does.

    **One value, one source, no fold.**  The cap is an opaque string in the host's own
    vocabulary with no ordering — this repository requires an ordering to be data with an
    exhaustiveness test, as ``ISOLATION_RANK`` is — so nothing here ranks or combines two of
    them, and :class:`~maf_sandbox.OutputSink` carries no cap of its own to be combined.

    Args:
        spec: The sandbox this workload asks for; ``egress``, ``egress_allow``, ``requires``,
            ``host_tools``, ``declared_outputs`` and ``outputs_named_at_call_time`` are what is
            read.
        source_integrity: Integrity label for this tool's results, as a
            :class:`~maf_sandbox.SourceIntegrity`, or ``None`` (the default) to declare none.
            Typed ``str`` because a host deserializing its own configuration passes one, and
            because a ``StrEnum`` satisfies it either way — so the enum is the spelling to
            reach for and the annotation cannot say so. Coerced, as every other value this
            package deserializes is: a misspelling would otherwise declare nothing at all,
            silently, and the framework logs that once and moves on.
        nothing_survives_from: The channels this workload opens and derives **nothing** from,
            each named as a :class:`~maf_sandbox.SourceChannel`. A claim about the tool body's
            own result, which no spec holds and this library cannot verify — ``also_carries_out``
            is the same kind of claim, owned by the caller and only routed here. Naming a
            channel the spec does not open is refused, so a deployment that later opens one is
            asked the question again rather than finding it pre-answered; and naming any
            without declaring ``"trusted"`` is refused, so a later ``"trusted"`` cannot inherit
            a clearance nobody re-examined.
        outbound_max_confidentiality: The host's cap for outbound tools, in the host's own
            vocabulary, or ``None`` (the default) to declare none.
        output_sink: Where this workload's artifacts land, if it lands any. Read together with
            what the spec says it lands, never for its presence alone.
        also_carries_out: An outward flow the spec cannot show, asserted by a caller that can
            see it — a wired host-tool registry with a tool that declares a sink, or an
            unstamped one that might, is the case this exists for, since a registry is neither
            ``egress_allow`` nor an ``output_sink``. Folded into the same "carries something
            out" condition, so the caller computes only the fact it alone knows and this one
            rule still decides whether the cap is written. The caller owns the claim; the
            library cannot check it.
        isolation_scope: The scope this workload's sandbox is actually served at, which a host's
            floor can raise above what ``spec`` asks for. ``None`` reads the spec, which is what
            a caller without a router has. Written only when it is
            :data:`~maf_sandbox.IsolationScope.CALL`, because that is the whole of what there is
            to say: the conversation-scoped sandbox is what a tool carrying no such key already
            means.
    """
    declarations: dict[str, Any] = {}
    # Coerced for the reason the scope below is, and this one is the value FIDES acts on: it
    # accepts two spellings and treats every other as absent, so an uncoerced typo here is a
    # tool that declares nothing while its author reads the keyword and believes otherwise.
    claimed = None if source_integrity is None else SourceIntegrity(str(source_integrity))
    cleared = frozenset(SourceChannel(str(channel)) for channel in nothing_survives_from)
    over_claimed = cleared - _open_source_channels(spec)
    if over_claimed:
        named = ", ".join(sorted(str(channel) for channel in over_claimed))
        raise ValueError(
            f"the {spec.kind!r} workload names {named} in nothing_survives_from, and its spec "
            "does not open that. A channel cleared before the spec opens it is cleared without "
            "being looked at, and the spec that opens it later inherits the claim. Name only "
            "what this spec opens."
        )
    if cleared and claimed is not SourceIntegrity.TRUSTED:
        declared = None if claimed is None else str(claimed)
        raise ValueError(
            f"the {spec.kind!r} workload passes nothing_survives_from and declares "
            f"source_integrity={declared!r}. Only a {str(SourceIntegrity.TRUSTED)!r} "
            "declaration reads that claim, so here nothing does — and a later one would find "
            f"it already made. Drop it, or declare {str(SourceIntegrity.TRUSTED)!r}."
        )
    if claimed is SourceIntegrity.TRUSTED:
        unestablished = _source_channels_not_established_as_trusted(spec, cleared)
        if unestablished:
            raise _unlicensed_trusted_claim_refusal(
                spec,
                unestablished,
                asked_by="sandbox_tool_declarations",
                through_mapping=False,
            )
    if claimed is not None:
        declarations["source_integrity"] = str(claimed)
    lands_artifacts = output_sink is not None and spec_lands_artifacts(spec)
    carries_something_out = _reaches_the_network(spec) or lands_artifacts or also_carries_out
    if outbound_max_confidentiality is not None and carries_something_out:
        declarations["max_allowed_confidentiality"] = outbound_max_confidentiality
    # Coerced for the reason `SandboxSpec.__post_init__` coerces its own, and this argument is
    # public where that one is a field: a caller passing the string would declare nothing at all.
    scope = (
        spec.isolation_scope if isolation_scope is None else IsolationScope(str(isolation_scope))
    )
    if scope is IsolationScope.CALL:
        # This package's own vocabulary rather than the flow tracker's, and nothing in the
        # framework reads it: what it gives a host is the fact its confidentiality cap cannot
        # carry — that no other call's data was in the filesystem this result came out of.
        declarations[ISOLATION_SCOPE_KEY] = str(scope)
    return declarations


#: Where :meth:`SandboxToolSession.read_file` records what the host knows about a file's bytes.
#:
#: A private key rather than the framework's ``security_label``, and the difference is not
#: cosmetic. ``security_label`` holds a whole ``ContentLabel``, and a partial one is not partial:
#: ``ContentLabel.from_dict({"integrity": "untrusted"})`` answers ``confidentiality=public``, so
#: writing integrity alone classifies everything the store holds as public the moment anything
#: parses it back. It is the same reason :func:`labelled_result_item` refuses ``untrusted``, and
#: it keeps an item that merely came out of the store from consuming :func:`sandboxed_tool`'s
#: rule that not every item may carry a label. This key means *what the host recorded about the
#: source* and nothing about confidentiality, so an item carrying it makes no claim MAF acts on.
#: A kind that wants to say something about a result item uses :func:`labelled_result_item`.
SOURCE_INTEGRITY_PROPERTY = "maf_sandbox_source_integrity"


def labelled_result_item(text: str, integrity: SourceIntegrity) -> Content:
    """One item of a split tool result, carrying its own integrity label.

    A kind whose result mixes standing guidance with something the call produced answers with
    a list of these rather than one string.  MAF's information-flow module reads a per-item
    label ahead of the tool's own declaration and hides each item separately, so the guidance
    stays readable while the derived half is replaced by a reference the model can still pass
    on.  `docs/sandbox/information-flow.md` carries what may be labelled and why.

    **Label only what carries nothing from the call.**  A per-item label is the item's *whole*
    label, confidentiality included, and this library has no confidentiality value to give —
    those are the host's.  An item left unlabelled takes the call's own label instead, which
    is where its confidentiality comes from, so :func:`sandboxed_tool` refuses a result whose
    every item carries one.

    Args:
        text: The item's text.
        integrity: :data:`~maf_sandbox.SourceIntegrity.TRUSTED`, and only that — see below.

    Raises:
        ValueError: for :data:`~maf_sandbox.SourceIntegrity.UNTRUSTED`, which an item is
            given by leaving it unlabelled rather than by writing it here.
    """
    if SourceIntegrity(str(integrity)) is not SourceIntegrity.TRUSTED:
        raise ValueError(
            f"labelled_result_item: {str(integrity)!r} is not a label an item may carry here. "
            "A per-item label replaces the item's whole label, and one written for integrity "
            "alone arrives `public` — a claim about content the call produced, in a vocabulary "
            "that is the host's and not this library's. Leave the item unlabelled instead: it "
            "then takes the call's own label, which is untrusted unless the tool declared "
            "otherwise. Where the input-label join might answer trusted, say so for the whole "
            "tool with sandboxed_tool(source_integrity=SourceIntegrity.UNTRUSTED)."
        )

    # `ContentLabel.to_dict` rather than a dict literal: the key names and the value spellings
    # are the framework's serialization, and a literal here would be a second copy of them.
    from agent_framework import Content
    from agent_framework.security import ContentLabel, IntegrityLabel

    label = ContentLabel(integrity=IntegrityLabel(str(integrity)))
    return Content.from_text(text, additional_properties={"security_label": label.to_dict()})


class SandboxToolSession:
    """Everything a sandbox workload's tool body needs, minus anything the model supplies.

    Handed to the ``build`` callback by :func:`sandboxed_tool`, and the reason that callback
    exists: the questions every sandbox tool has to answer the same way — where the sandbox
    key comes from, which file names may be interpolated into a command, and what a provider
    failure is allowed to tell the model — are answered here once instead of per workload.

    **One per tool, not one per call.** :func:`sandboxed_tool` builds it once, before any key
    exists, and every call to that tool shares it. So *this object holds host configuration
    only. Anything derived from a caller — a scope, a thread, a path generated for one call —
    lives on the call, or nowhere.* :meth:`key`, :meth:`list_files` and :meth:`acquire` already
    answer per call, by **reading** the host's context each time rather than storing what they
    read; :meth:`guest_call_path` is the first value that has to be generated and then remembered,
    which is why it lives in :data:`_CALL` and not here.

    All three accessors return **either the value or the string the tool should return** for
    anything the model has to hear about.  A refusal is an ordinary answer, not an exception:
    the model has to learn what happened through the same channel as a successful result, or
    the turn ends mute.  So a body reads::

        key = session.key()
        if isinstance(key, str):
            return key
        ...
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

    A workload whose tool answers with something other than a plain ``str`` converts that
    message into its own result shape at **one** place — the funnel its body returns through —
    never at each accessor.  Three of them answer this way, :meth:`list_files` included, and a
    body has its own returns besides, so a per-call-site conversion is a claim about every
    return path that is one branch from being false.  Nothing else about the contract changes;
    ``docs/sandbox/architecture.md`` carries the rule.

    **A wiring mistake in the kind raises**, and that is the line the two shapes are split on: a
    model can cause a refusal and cannot cause a body that asks for a call's key outside a call,
    or acquires on a key naming a call that has ended.  Those reach a developer through a
    traceback rather than a tool result, the way :meth:`guest_call_path` has always answered
    them, because a sentence in the transcript is not where they get fixed.  Each is listed in
    the ``Raises`` of the method it belongs to.
    """

    def __init__(
        self,
        router: SandboxRouter,
        context: CallerContext,
        agent_dir: str,
        spec: SandboxSpec,
        *,
        name: str,
        logger: logging.Logger,
        output_sink: OutputSink | None = None,
        file_store_provenance: FileStoreProvenance | None = None,
    ) -> None:
        self._router = router
        self._context = context
        self._agent_dir = agent_dir
        self._spec = spec
        self._name = name
        self._logger = logger
        self._output_sink = output_sink
        self._file_store_provenance = file_store_provenance
        self._log_prefix = _prefixed(name)

    @property
    def spec(self) -> SandboxSpec:
        """The sandbox this workload asks for. Workloads read ``work_dir`` off it."""
        return self._spec

    @property
    def name(self) -> str:
        """The tool's name, as the model sees it."""
        return self._name

    @property
    def output_sink(self) -> OutputSink | None:
        """The sink :func:`sandboxed_tool` checked this spec against.

        Read it here rather than closing over one, so that the sink whose presence satisfied
        the attach-time refusal is the sink the body actually hands to ``collect_outputs``.
        """
        return self._output_sink

    @property
    def observer(self) -> SandboxObserver | None:
        """Where the host records what this workload does, or ``None`` for nowhere.

        The router's own, read here so a kind passing it on —
        ``collect_outputs(..., observer=session.observer, key=key)`` — sends a collection's
        record where the sandbox lifecycle's already goes, without a second thing to wire.
        """
        return self._router.observer

    def key(self) -> SandboxKey | str:
        """The sandbox key for this call, or the message to return when no thread is bound.

        Scope and thread come from the host's request context — never from model input: a
        model-supplied scope would let one conversation address another's sandbox.  The agent
        directory is baked in at factory time for the same reason.

        A call with no bound conversation is refused rather than served from a placeholder
        key, because a shared fallback key is exactly the cross-conversation reach the key
        exists to prevent.

        The key names this call as well when the host and the spec resolve to
        :data:`~maf_sandbox.IsolationScope.CALL`, and that is what makes the sandbox the call's
        own: without it get-or-create hands back the conversation's, which the router refuses at
        that scope rather than serves.

        Raises:
            RuntimeError: at that scope only — called outside a tool call, or after one
                returned, there is no call to key a sandbox to and nothing that would dispose
                what it creates. A wiring mistake in a kind, like :meth:`guest_call_path`'s.
        """
        thread_id = self._context.current_thread_id()
        if thread_id is None:
            return (
                f"Error: no active thread context — {self._name} must be called from "
                "within a thread"
            )
        return SandboxKey(
            scope=self._context.current_scope(),
            thread_id=thread_id,
            agent_dir=self._agent_dir,
            call_id=self._call_id(),
        )

    def _call_id(self) -> str:
        """This call's id when the workload runs one sandbox per call, and empty otherwise."""
        if self._router.effective_isolation_scope(self._spec) is not IsolationScope.CALL:
            return ""
        call = _this_call(self)
        if call is None or call.closed:
            raise RuntimeError(
                f"{self._name}: key() was called outside a tool call, and this workload runs one "
                "sandbox per call — there is no call to key it to, and nothing would dispose "
                "what it creates. Call it from the tool body."
            )
        return _call_name(call)

    def guest_call_path(self) -> str:
        """This call's own place **inside the sandbox**, under the spec's ``work_dir``.

        Allocated once per call and reclaimed with everything under it when the call returns, so
        a kind that puts its files here cannot leave them behind. Apart from the three accessors
        above: it raises rather than answering with a message, because reaching it wrongly is a
        wiring mistake in a kind, not something a model can cause or should be told about.

        Nothing is created — it is a name until a kind writes to it. ``path`` rather than
        ``directory`` because that is all the protocol promises: a backend serving its store from
        memory, or with no enumeration primitive under it, addresses one the same way.

        The reclaim covers what was written through the sandbox :meth:`acquire` returned, before
        the call returns. A kind that writes here through a sandbox it got elsewhere, or from a
        task that outlives the call, keeps what it wrote.

        Raises:
            RuntimeError: Called outside a tool call, or after one returned — in both cases
                nothing would reclaim what it names.
        """
        call = _this_call(self)
        if call is None:
            raise RuntimeError(
                f"{self._name}: guest_call_path() was called outside a tool call, so nothing "
                "would reclaim what it names. Call it from the tool body."
            )
        if call.closed:
            raise RuntimeError(
                f"{self._name}: guest_call_path() was called after its tool call returned. That "
                "path is already reclaimed, and anything written to it now would stay in the "
                "sandbox. A task outliving the call needs a path of its own."
            )
        # Composed for the caller, and never stored composed: the reclaim addresses this by
        # name against ``work_dir``, the way every other confined call on the surface does.
        return f"{self._spec.work_dir}/{_call_name(call)}"

    async def list_files(self, store: Any) -> list[ListedFile] | str:
        """The files this caller may act on, or the message to return if they cannot be read.

        The listing is a workload's injection-pinning boundary: only a name present in it is
        ever substituted into a command, so a name the model invented — or one it read out of
        a poisoned file — has nowhere to go.  Which makes a failure to enumerate a refusal
        rather than an empty listing: an empty list would look like "the file store has no
        files" and refuse each name individually with the wrong reason.

        The store is passed per call rather than held: which store a workload reads is the
        workload's business, and some read more than one.

        Each entry carries the label the host knows for the bytes at that name.  Read it rather
        than inferring one from the name — see :class:`~maf_sandbox.ListedFile`, and rule 9 in
        ``docs/sandbox/kinds/README.md``.
        """
        try:
            return await self._context.list_files(store)
        except Exception as exc:  # noqa: BLE001
            return f"Error: could not list the file store: {exc}"

    async def read_file(
        self,
        store: Any,
        listed: ListedFile,
        *,
        at: str | None = None,
        hidden: bool = False,
        named: str | None = None,
    ) -> Content | str | None:
        """The content at ``listed``, as a labelled item — or ``None`` where the file is gone.

        The read surface a kind should use, in place of reaching for ``store.read`` itself.  It
        answers with an ``agent_framework`` ``Content`` carrying the listing's own label in
        ``additional_properties[SOURCE_INTEGRITY_PROPERTY]``, so what a kind holds says what it is
        worth
        rather than being a bare ``str`` whose provenance the framework lost
        (:class:`~maf_sandbox.ListedFile`).

        **Pass the listing entry, never a name.**  Taking a ``ListedFile`` is what keeps the two
        together: a name alone would read the bytes and lose the only thing that says what they
        are, which is the failure this whole channel exists to close.

        ``None`` where the store has no such file — it was listed and then removed, and writing
        the word ``None`` into a sandbox is not an answer.  A failure to read answers with the
        sentence a caller returns, for the reason the listing does.

        **The label is checked across the read, not taken from the listing.**  A listing's
        label is as old as the listing, and MAF runs tool calls concurrently, so the record is
        sampled either side of ``store.read`` through
        :meth:`FileStoreProvenance.state_of`, which answers both under one lock.  An unchanged
        count means the record held
        still for the whole read and its answer is folded with the listing's; a changed one
        means nothing can be said about these bytes, and they carry no label at all.

        Without a record on the session the listing's label stands, and a host wiring
        ``floor=SourceIntegrity.TRUSTED`` is then claiming the store is not written under a read
        rather than merely that its unrecorded paths are trustworthy.  A ``trusted`` floor still
        answers for bytes written by a call that has not returned, which is the one thing the
        record cannot see.

        ``at`` is where the caller got the name — ``"files[1]"`` — and ``hidden`` says the
        framework rewrote that argument, which is what makes the refusal render the position
        instead of the value.  Pass both: ``at`` alone still quotes a short printable name, and a
        name expanded out of hidden content is exactly the value a refusal must not repeat
        (:func:`echoed_name`, and rule 9 in ``docs/sandbox/kinds/README.md``).

        ``named`` overrides that rendering, and a caller that resolved the model's spelling
        against its listing should pass one.  :attr:`ListedFile.name` is the *host's* key, so
        rendering from it describes a different string than the one at ``at`` — for a model that
        asked for ``./main.bicep`` against a listing holding ``main.bicep``, the positional form
        would report the wrong length for the value it is standing in for.  The caller knows both
        spellings; this method only ever sees one.
        """
        from agent_framework import Content

        try:
            # Inside the boundary, not before it: this raises for a `trusted` floor no
            # middleware observes, which is a supported configuration and so a reachable way out
            # of a read that would otherwise leave no record at all.
            before = self._recorded_state(listed.name)
        except BaseException:
            self._record_read(listed.name, None, 0, outcome="refused")
            raise
        try:
            text = await store.read(listed.name)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                f"{self._log_prefix}: could not read a listed file: %s", error_detail(exc)
            )
            self._record_read(listed.name, None, 0, outcome="refused")
            shown = named if named is not None else echoed_name(listed.name, at=at, hidden=hidden)
            return f"Error: {shown} could not be read from the file store"
        except BaseException:
            # A cancel leaves this site the same way a raise does — no text crossed — and the
            # record owes every way out, not only the ones that return. It is not an
            # `Exception`, so it needs its own catch or the read goes unrecorded.
            self._record_read(listed.name, None, 0, outcome="refused")
            raise
        if text is None:
            self._record_read(listed.name, None, 0, outcome="absent")
            return None
        try:
            # Inside the boundary for the same reason the first reading is: this is the record's
            # *second* `state_of`, and a path forgotten while the read was in flight can make it
            # raise where the first one did not.
            integrity = self._folded_integrity(listed, before)
        except BaseException:
            self._record_read(listed.name, None, 0, outcome="refused")
            raise
        self._record_read(listed.name, integrity, len(text), outcome="read")
        properties: dict[str, Any] = {}
        if integrity is not None:
            # Not `security_label` — see `SOURCE_INTEGRITY_PROPERTY` for why.
            properties[SOURCE_INTEGRITY_PROPERTY] = str(integrity)
        return Content.from_text(text, additional_properties=properties)

    def _record_read(
        self,
        name: str,
        integrity: SourceIntegrity | None,
        characters: int,
        *,
        outcome: StoreReadOutcome,
    ) -> None:
        """Record one store read, its label folded, for a host that watches what a call reads."""
        observer = self.observer
        if observer is None:
            return
        key = self._recorded_key()
        # A read is the call touching this key, and a kind may read the store and return before
        # it ever acquires — `execute_code` does exactly that when a listed file is refused. So
        # the key is registered here too, or that call's `ToolCallEnded` names nothing and the
        # read it just recorded has no call to join to.
        call = _this_call(self)
        if key is not None and call is not None and not call.closed and key not in call.touched:
            call.touched.append(key)
        record(
            observer,
            StoreFileRead(
                key=key,
                tool=self._name,
                name=name,
                integrity=integrity,
                characters=characters,
                outcome=outcome,
            ),
            self._logger,
        )

    def _recorded_key(self) -> SandboxKey | None:
        """This call's key for a record, or ``None`` where it cannot be read.

        Never raises.  :meth:`key` reads the host's request context and refuses a call with no
        conversation bound, and a workload running one sandbox per call refuses one asked
        outside a call at all — none of which is worth failing a read over.
        """
        try:
            key = self.key()
        except CONTAINED as raised:  # noqa: BLE001 - `_containment` carries the rule
            # `key()` runs the host's context getters, so what comes out of them is the host's,
            # and a record is not worth turning a completed read into a failure it would not
            # otherwise have had.
            if escapes_containment(raised):
                raise
            return None
        return key if isinstance(key, SandboxKey) else None

    def _recorded_state(self, name: str) -> tuple[SourceIntegrity | None, int]:
        """What the host's record says about ``name``, and how many times it has moved.

        Raises:
            ValueError: what :meth:`FileStoreProvenance.integrity_of` raises for a ``trusted``
                floor no middleware observes.  A host that folded the same record into its
                listing meets this at listing time instead; one that wired the record here and
                not there meets it here, which is the first moment it can be told.
        """
        record = self._file_store_provenance
        return (None, 0) if record is None else record.state_of(name)

    def _folded_integrity(
        self, listed: ListedFile, before: tuple[SourceIntegrity | None, int]
    ) -> SourceIntegrity | None:
        """The listing's label folded with what the record said across the read.

        **A reading either side is not enough, and the count is what closes it.**  A path can be
        recorded and then forgotten while one read is in flight, which leaves both readings
        equal and neither of them true of the moment the bytes were captured — the model's write
        happened between them.  The count :meth:`FileStoreProvenance.state_of` answers with only
        goes up, so an unchanged one is the thing two equal readings are not: proof that nothing
        happened.

        Where it did change, this answers ``None``.  *Unestablished* is what a reader that
        cannot say honestly says, and it is what :func:`weakest_integrity` already treats as
        beating every level.
        """
        if self._file_store_provenance is None:
            return listed.integrity
        integrity, generation = before
        if generation != self._recorded_state(listed.name)[1]:
            return None
        return weakest_integrity((listed, ListedFile(listed.name, integrity)))

    async def acquire(self, key: SandboxKey) -> Sandbox | str:
        """A running sandbox for ``key``, or the message to return when there is none.

        The ladder is this method's whole point, and the line it draws is a security one
        rather than a stylistic one:

        - a **refusal** — any member of ``_router``'s ``ATTACH_REFUSALS`` — gets a fixed
          sentence of its own, saying the workload was refused rather than that the sandbox is
          unavailable. Its *text* is not surfaced: those classes are public and ``acquire``
          forwards what a backend raises, so a message may carry an SDK response, and nothing
          about the type says who composed it. What the caller gains is the distinction
          between a refusal and an outage, which is what decides whether retrying is pointless.
          **It is caught first**, and the order is the boundary rather than a style: these
          classes are subclassable, and one inheriting :class:`ValueError` as well would take
          the verbatim branch below and carry whatever it holds into the transcript;
        - a **missing SDK** is a host-side install problem, actionable and carrying no
          account detail;
        - **no backend** is a configuration state, likewise safe to name;
        - a :class:`ValueError` is a message this stack authored (image resolution raises
          them), so it is surfaced verbatim — that is what makes it actionable for whoever is
          enabling the feature;
        - anything else is a provider or transport failure whose text can carry endpoint,
          subscription and tenant ids.  Tool results are persisted into the transcript, so
          that detail goes to the log — with :func:`~maf_sandbox.error_detail`, because
          ``str()`` on such an error is often just ``Operation returned an invalid status``
          — and the model gets a fixed sentence saying only that the run degraded.

        Raises:
            RuntimeError: given a key naming a call while no tool call of this session is
                open — after one returned, from a context that never had one, or naming a
                different call than the one running. The wiring mistake :meth:`guest_call_path`
                refuses for the same reason: nothing would delete what it created. Raised again
                when the call ends *during* the acquire, in which case the sandbox that came back
                is disposed before the refusal.
        """
        call = _this_call(self)
        if key.call_id and (call is None or call.closed):
            raise RuntimeError(
                f"{self._name}: acquire() was given a key naming a call, with no open tool call "
                "to record it against — this one has returned, or the caller never had one. The "
                "sandbox it would create is past whatever would have deleted it: no cleanup "
                "walks it, and no later call can name that key. A task outliving the call needs "
                "a key of its own."
            )
        if key.call_id and call is not None and key.call_id != call.name:
            raise RuntimeError(
                f"{self._name}: acquire() was given a key naming {key.call_id!r}, which is not "
                "this call. That sandbox is either gone or is the one its own cleanup could not "
                "delete, and reaching it from here is exactly the sharing the scope refuses. "
                "Take the key from session.key(), which names the call it is called in."
            )
        if call is not None and not call.closed and key not in call.touched:
            # Before the acquire, so a refusal is still recorded as this call's ask.
            call.touched.append(key)
        per_call = self._router.effective_isolation_scope(self._spec) is IsolationScope.CALL
        if key.call_id and call is not None and per_call:
            # Recorded before the create rather than after it: a cancellation landing inside the
            # backend's own acquire can leave a sandbox it already made, and a map written
            # afterwards would hand the cleanup nothing to delete.
            #
            # Only at this scope, and that is the load-bearing half. The cleanup reads the scope
            # off the key, and a backend's `dispose` sweeps a key's whole (scope, thread, agent) —
            # so registering a call-naming key under a conversation-scoped workload would have the
            # `finally` delete the conversation's own sandbox over an acquire the router refused.
            call.acquired.setdefault(key, [])
        try:
            sandbox = await self._router.acquire(key, self._spec)
        except ATTACH_REFUSALS as exc:
            self._logger.warning(
                f"{self._log_prefix}: workload refused before it ran: %s", error_detail(exc)
            )
            return _SANDBOX_REFUSED
        except ImportError as exc:
            # The backend's SDK is not installed. Actionable, and carries no account detail.
            self._logger.warning(f"{self._log_prefix}: sandbox SDK unavailable: %s", exc)
            return _SDK_NOT_INSTALLED
        except NoSandboxBackend as exc:
            self._logger.warning(f"{self._log_prefix}: %s", exc)
            return _NO_BACKEND_CONFIGURED
        except ValueError as exc:
            # Raised by image resolution: a configuration message we author, safe to
            # surface, and actionable for whoever is enabling the feature.
            self._logger.warning(f"{self._log_prefix}: %s", exc)
            return f"Error: {exc}"
        except SandboxUnclean as exc:
            # The router's own refusal: a sandbox a previous call could not clean and the
            # framework could not dispose of. Safe to name and actionable for the host, but
            # the model hears only that the sandbox is closed, not whose files are in it.
            self._logger.warning(f"{self._log_prefix}: %s", exc)
            return _SANDBOX_UNCLEAN
        except Exception as exc:  # noqa: BLE001
            # A provider/transport failure — its detail can carry endpoint, subscription and
            # tenant ids, so it goes to the log and never into the model's context.
            self._logger.warning(f"{self._log_prefix}: sandbox unavailable: %s", error_detail(exc))
            return _SANDBOX_UNAVAILABLE
        if key.call_id and call is not None and call.closed:
            # The call ended while the backend was still creating. Its cleanup has already run the
            # delete for this key, so what came back is a sandbox nothing is left to remove: take
            # it here, and refuse rather than hand a task something it cannot have cleaned up.
            landed = await self._router.dispose_call(
                key, timeout=self._router.reclaim.timeout, spec=self._spec
            )
            if landed:
                fate = "It has been disposed and the result refused."
            else:
                self._logger.warning(
                    f"{self._log_prefix}: a sandbox created after the call had ended was not "
                    "disposed; the conversation's purge is what reaches it now"
                )
                fate = (
                    "Deleting it did not land, so it is still there until the conversation's "
                    "purge reaches it, and the result is refused."
                )
            raise RuntimeError(
                f"{self._name}: acquire() came back after its tool call had ended, so the sandbox "
                f"it created is past the cleanup that would have deleted it. {fate} A task "
                "outliving the call needs a key of its own."
            )
        if call is not None and not call.closed:
            # Recorded on the way through rather than re-derived in the `finally`, where a
            # second `acquire` could fail on its own and report a reclaim failure for it. Not
            # once the call is closed: the removal is already walking this, and a task the body
            # left running would otherwise add to it mid-walk — and nothing would reclaim what
            # it wrote anyway.
            call.acquired.setdefault(key, []).append(sandbox)
        return sandbox


async def _dispose_the_unclean(
    router: SandboxRouter,
    key: SandboxKey,
    *,
    prefix: str,
    logger: logging.Logger,
    timeout: float,
) -> DisposalOutcome:
    """Dispose a sandbox the call could not leave clean, unless the host opted down.

    Before the host is told, not after: the guarantee is the framework's, and it must not
    wait on a callback's time budget. Cancellation passes through with the key recorded on
    the router first, so the next call is refused rather than served the leftovers.
    """
    if router.reclaim.failed_reclaim_policy is FailedReclaimPolicy.KEEP:
        # Every record here carries an argument, so the prefix's doubled `%` interpolates.
        logger.warning(
            f"{prefix}: the sandbox for %s is kept with the data in it — this host opted down "
            "with FailedReclaimPolicy.KEEP",
            key.thread_id,
        )
        return "kept"
    try:
        landed = await router.dispose_unclean(key, timeout=timeout)
    except (asyncio.CancelledError, GeneratorExit) as stopped:
        logger.warning(
            f"{prefix}: the sandbox could not be disposed: %s during the disposal; the router "
            "refuses it until a disposal lands",
            type(stopped).__name__,
        )
        raise
    if landed:
        logger.warning(
            f"{prefix}: the sandbox for %s was disposed, so the conversation's next call "
            "starts cold",
            key.thread_id,
        )
        return "disposed"
    logger.warning(
        f"{prefix}: the sandbox for %s could not be disposed; the router refuses it until a "
        "disposal lands",
        key.thread_id,
    )
    return "failed"


def _refuse_not_yet_reclaimed(
    router: SandboxRouter, acquired: Sequence[tuple[SandboxKey, object]], start: int
) -> None:
    """Refuse every key from ``start`` on, so the next call is not served a sandbox this one did
    not finish reclaiming.

    Called synchronously while a cancellation is propagating out of the cleanup, where awaiting a
    disposal is not reliable. A no-op when the host opted down with ``FailedReclaimPolicy.KEEP``:
    it asked to keep the data, so refusing the key would contradict that.
    """
    if router.reclaim.failed_reclaim_policy is FailedReclaimPolicy.KEEP:
        return
    for key, _ in acquired[start:]:
        if key.call_id:
            # Nothing to refuse: the ledger closes a key against its *next* acquire, and a
            # call-scoped key has none — the entry would be read by nobody and cleared by
            # nothing. What is left is a sandbox no later call can address.
            continue
        router.mark_unclean(
            key,
            # `unknown`, not a guess: the cleanup stopped before anything could observe the
            # sandbox, so nothing here knows whether a delete would have landed.
            DisposalFailure(
                "unknown", "the tool call's cleanup was cancelled before it could dispose"
            ),
        )


async def _tell_the_host(
    on_failure: Callable[[ReclaimFailure], Awaitable[None]],
    failure: ReclaimFailure,
    *,
    prefix: str,
    logger: logging.Logger,
    timeout: float,
) -> None:
    """Hand one failure to the host's callback, bounded, and swallow what the callback does with it.

    Bounded like the removal and separately from it: a host may reach a backend in here, which is
    a round trip that can hang, and an unbounded one holds the tool call open for as long as it
    hangs — on a cancelled call, past a deadline that has already expired.
    """
    try:
        async with asyncio.timeout(timeout):
            await on_failure(failure)
    except TimeoutError:
        logger.warning(f"{prefix}: on_reclaim_failure did not finish within %ss", timeout)
    except Exception as raised:  # noqa: BLE001 — a host's callback must not fail the call
        logger.warning(f"{prefix}: on_reclaim_failure raised: %s", error_detail(raised))
    except (asyncio.CancelledError, GeneratorExit) as stopped:
        # The callback awaits, so this is the caller's cancellation arriving inside it, not a
        # failure of the callback's own — logged, then re-raised for the caller's handler. Named
        # rather than stated, so this line interpolates: `prefix` has its `%` doubled for exactly
        # that, and `logging` leaves the doubling alone with no args.
        logger.warning(f"{prefix}: on_reclaim_failure did not finish: %s", type(stopped).__name__)
        raise


async def _dispose_the_call_sandbox(
    key: SandboxKey,
    *,
    router: SandboxRouter,
    spec: SandboxSpec,
    prefix: str,
    logger: logging.Logger,
    timeout: float,
) -> str | None:
    """Delete the sandbox one call owns, and say why it is still there if it is.

    The whole sandbox goes, so nothing is removed from inside it first, and a transport's note
    that a stop did not reach everything is answered by the same delete.  Nothing is marked
    unclean: that refuses a key's next acquire, and this key has none.

    ``spec`` is passed on rather than dropped because it is what names the backend to ask on a
    router that selects per spec — the delete is aimed at the one that served this call, not
    swept across every backend registered.
    """
    try:
        landed = await router.dispose_call(key, timeout=timeout, spec=spec)
    except (asyncio.CancelledError, GeneratorExit):
        logger.warning(
            f"{prefix}: the call's sandbox was not disposed — the call was cancelled during the "
            "delete. It holds this call's files until the conversation's purge reaches it"
        )
        raise
    return None if landed else "the delete did not land"


async def _reclaim_the_call(
    call: _SandboxToolCall,
    *,
    router: SandboxRouter,
    spec: SandboxSpec,
    tool: str,
    logger: logging.Logger,
    on_failure: Callable[[ReclaimFailure], Awaitable[None]] | None,
    timeout: float,
    unclean: Sequence[tuple[object, str]],
) -> None:
    """Remove what one tool call owns, and act on a sandbox it could not leave clean.

    Once per sandbox the call acquired — ordinarily one, and more only for a call that
    reached a second key. A **call-scoped** key's sandbox is deleted rather than emptied: it was
    created for this call, so the delete is the cleanup itself and not an escalation, and
    ``FailedReclaimPolicy`` does not loosen it. A sandbox is unclean when the removal did not
    happen, or when
    ``unclean`` carries a transport's note that a stop did not reach everything the program
    started.  At :data:`~maf_sandbox.IsolationScope.CONVERSATION` either of those leaves the
    residue readable by every later call, so the framework disposes the sandbox — unless the
    host opted down — and only then tells the host.  At ``CALL`` the delete above was already
    the cleanup, and a leak it could not take is one no later call can address: nothing is
    escalated, nothing is opted down from, and the host is told what did not happen.
    ``timeout`` bounds the removal, the disposal and the report separately, so one sandbox
    can cost up to three times it.

    Raises only what the caller asked for. A failed removal, a failed disposal, and a host
    callback that fails with them, are logged and swallowed: this runs in
    :func:`sandboxed_tool`'s ``finally``, where raising would replace whatever the call was
    already reporting with a message about cleanup. A :class:`~asyncio.CancelledError` or
    ``GeneratorExit`` is not such a failure — it is an outer deadline arriving mid-removal, so
    it is recorded and re-raised, and it *does* replace the body's result.
    """
    if not call.acquired:
        # Nothing was acquired, so nothing was written and nothing ran — there is nothing
        # there, and no round trip is worth spending to prove it.
        return
    prefix = _prefixed(tool)
    path = f"{spec.work_dir}/{call.name}" if call.name is not None else spec.work_dir
    acquired = tuple(call.acquired.items())
    for index, (key, sandboxes) in enumerate(acquired):
        if key.call_id:
            try:
                undisposed = await _dispose_the_call_sandbox(
                    key,
                    router=router,
                    spec=spec,
                    prefix=prefix,
                    logger=logger,
                    timeout=timeout,
                )
                if undisposed is None:
                    # The sandbox went, and every note about it went with it.
                    continue
                logger.warning(f"{prefix}: the call's sandbox was not disposed: %s", undisposed)
                left = [
                    undisposed,
                    *(
                        reason
                        for owner, reason in unclean
                        if any(owner is held for held in sandboxes)
                    ),
                ]
                if len(left) > 1:
                    # It is still there, so a stop that did not reach everything the program
                    # started is still true of it — and a host told only that data was left
                    # would not know something may still be running in it.
                    logger.warning(
                        f"{prefix}: the sandbox that was not disposed is not clean either: %s",
                        "; ".join(left[1:]),
                    )
                if on_failure is not None:
                    await _tell_the_host(
                        on_failure,
                        ReclaimFailure(
                            tool=tool, key=key, path=path, reason="; ".join(left), disposal="failed"
                        ),
                        prefix=prefix,
                        logger=logger,
                        timeout=timeout,
                    )
            except (asyncio.CancelledError, GeneratorExit):
                _refuse_not_yet_reclaimed(router, acquired, index)
                raise
            continue
        reasons: list[str] = []
        if call.name is not None:
            try:
                # By name, against the working directory — not as one composed string. A
                # ``work_dir`` the protocol accepts may not be POSIX-shaped, and a composed path
                # would carry its separators into a grammar that refuses them, failing every
                # call on such a spec. Through the live wrapper: the guest path is the same
                # whichever acquire returned it, and the newest is the one still connected.
                reason = await reclaim_guest_path(
                    sandboxes[-1], call.name, working_directory=spec.work_dir, timeout=timeout
                )
            except (asyncio.CancelledError, GeneratorExit):
                # The caller's deadline has passed, and containing this would have the call return
                # the body's answer past a bound the host thought it had. Neither this sandbox nor
                # any the loop has not yet reached was reclaimed, so refuse them all — otherwise the
                # next call reacquires one still holding the last call's data. The leak still has to
                # be visible, so the line is written before the cancellation goes on.
                _refuse_not_yet_reclaimed(router, acquired, index)
                logger.warning(
                    f"{prefix}: %s was not reclaimed: the call was cancelled during the removal",
                    path,
                )
                raise
            if reason is not None:
                # Logged whether or not a host is listening: a callback that swallows it would
                # take the record with it.
                logger.warning(f"{prefix}: %s was not reclaimed: %s", path, reason)
                reasons.append(reason)
        # Only this sandbox's notes, matched by identity against every wrapper acquired for the
        # key: a call may acquire more than one, and a stop that did not take sandbox A's program
        # tree must not dispose a clean sibling B. Every wrapper, not the last: a note names the
        # one that ran the stop, which a reacquire may have replaced in the map.
        noted = [reason for owner, reason in unclean if any(owner is held for held in sandboxes)]
        if noted:
            logger.warning(
                f"{prefix}: the sandbox is not clean after this call: %s", "; ".join(noted)
            )
            reasons.extend(noted)
        if not reasons:
            continue
        try:
            disposal = await _dispose_the_unclean(
                router, key, prefix=prefix, logger=logger, timeout=timeout
            )
            if on_failure is None:
                continue
            await _tell_the_host(
                on_failure,
                ReclaimFailure(
                    tool=tool, key=key, path=path, reason="; ".join(reasons), disposal=disposal
                ),
                prefix=prefix,
                logger=logger,
                timeout=timeout,
            )
        except (asyncio.CancelledError, GeneratorExit):
            # Cancellation during the disposal or the host callback, not the removal above. This
            # key is already accounted for — dispose_unclean refuses it before its first await, and
            # a landed disposal cleared it clean — so mark only the keys the loop has not reached,
            # never re-refusing one just disposed. Same reason as the removal handler: the next call
            # must not reacquire a sandbox still holding this call's data.
            _refuse_not_yet_reclaimed(router, acquired, index + 1)
            raise


#: The one substitution a committed sentence may carry, and the reason there is exactly one.
#: A sentence that qualifies under rule 5 has to be independent of every source the framework
#: has not established, which a *constant* trivially is — but a host-minted call id is too, and
#: a kind that names this call's landing folder needs it in the sentence. Everything else a body
#: could interpolate is either derived from the call or is guest text, so the vocabulary is
#: closed at one and the wrapper does the rendering rather than trusting the body's.
CALL_ID_PLACEHOLDER = "call_id"


#: Stands in for a call id while a committed sentence is checked at attach, where no call
#: exists. Its shape is the real thing's — `uuid4().hex` — so a sentence whose format spec only
#: fails on some values fails here too.
_SENTINEL_CALL_ID = "0" * 32


def _guidance_placeholders(sentence: str) -> set[str]:
    """The *top-level* replacement fields in one committed sentence, by name.

    ``string.Formatter().parse`` rather than a regex, so an escaped brace reads as the literal
    :meth:`str.format` will read it.  It reports only the outermost field of each replacement,
    which is why :func:`_renders` is what decides a sentence is usable.
    """
    return {field for _, field, _, _ in string.Formatter().parse(sentence) if field is not None}


def _renders(sentence: str) -> str | None:
    """The reason ``sentence`` cannot be rendered, or ``None`` where it can.

    The field-name check above sees the top level only, so ``{call_id:{exit_code}}`` passes it
    with one field named ``call_id`` and then raises at every call.  A conversion or a format
    spec that ``format`` rejects survives it the same way.  Rendering once, here, is what turns
    all of those into an attach-time refusal instead of a per-call one.
    """
    try:
        sentence.format(**{CALL_ID_PLACEHOLDER: _SENTINEL_CALL_ID})
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _committed_guidance(guidance: Iterable[str], *, tool: str, awaits: bool) -> tuple[str, ...]:
    """Validate the sentences a tool commits to at attach, and answer with them.

    Raises:
        ValueError: for an empty sentence, a placeholder other than ``{call_id}``, a malformed
            replacement field, or a ``{call_id}`` sentence on a body that cannot have a call.
    """
    committed: list[str] = []
    for index, sentence in enumerate(guidance):
        if not sentence.strip():
            raise ValueError(
                f"{tool}: standing_guidance[{index}] is empty. A committed sentence is what a "
                "labelled item is held to, and an empty one holds it to nothing."
            )
        try:
            fields = _guidance_placeholders(sentence)
        except ValueError as exc:
            raise ValueError(
                f"{tool}: standing_guidance[{index}] is not a valid format string ({exc}). "
                "Double a literal brace to keep it."
            ) from exc
        unknown = sorted(fields - {CALL_ID_PLACEHOLDER})
        if unknown:
            raise ValueError(
                f"{tool}: standing_guidance[{index}] interpolates {unknown}, and the only "
                f"substitution a committed sentence may carry is {{{CALL_ID_PLACEHOLDER}}}. "
                "Anything else is either derived from the call or is the guest's, and a "
                "sentence carrying one of those is not standing guidance."
            )
        if fields and not awaits:
            raise ValueError(
                f"{tool}: standing_guidance[{index}] interpolates "
                f"{{{CALL_ID_PLACEHOLDER}}} and this tool's body awaits nothing, so it runs "
                "outside a call and there is no id to render. Commit the sentence without it."
            )
        # After the field check, so a sentence naming something else is refused for that rather
        # than for the `KeyError` it would also raise here.
        broken = _renders(sentence)
        if broken is not None:
            raise ValueError(
                f"{tool}: standing_guidance[{index}] cannot be rendered ({broken}). A nested "
                "format spec, a conversion or a specifier is legal to parse and fails at every "
                "call, so it is refused here instead."
            )
        if not sentence.format(**{CALL_ID_PLACEHOLDER: _SENTINEL_CALL_ID}).strip():
            raise ValueError(
                f"{tool}: standing_guidance[{index}] renders to nothing. A precision on the "
                "substitution — `{call_id:.0}` — is written text and renders empty, which "
                "holds a labelled item to as little as an empty sentence does."
            )
        committed.append(sentence)
    return tuple(committed)


def _needs_call_id(committed: tuple[str, ...]) -> bool:
    """Whether any committed sentence asks for this call's id.

    Only that allocates one; the sentences are formatted either way, so a doubled brace is
    undoubled whether or not anything in the set interpolates.
    """
    return any(CALL_ID_PLACEHOLDER in _guidance_placeholders(s) for s in committed)


def _refuse_a_result_that_departs_from_its_guidance(
    result: object, *, tool: str, committed: tuple[str, ...], call_id: str | None
) -> None:
    """Hold a result's labelled items to the sentences ``committed`` at attach, in order.

    Rule 5 in ``docs/sandbox/kinds/README.md`` is what this executes, and owns the reasoning.

    **The refusal must never quote the item.**  A raise here skips the label-tracking
    middleware's own labelling step, so MAF answers with an *unlabelled* error result — and the
    mistake being caught is a kind labelling guest text as trusted.  Name positions and counts.
    """
    if not committed:
        return
    if isinstance(result, str):
        raise ValueError(
            f"{tool}: this tool commits to standing guidance and its body answered with a "
            "string, so the committed sentences reached no item. A string on one return path "
            "beside a split result on another is itself a bit about which path ran. Answer "
            "with the items on every path, refusals included."
        )
    if not isinstance(result, list):
        return
    # Formatted whether or not anything interpolates: `str.format` is what undoubles `{{`, and
    # the attach-time refusal tells a caller to double a literal brace.
    substitution = {CALL_ID_PLACEHOLDER: call_id} if call_id is not None else {}
    rendered = [sentence.format(**substitution) for sentence in committed]
    items = cast("list[Any]", result)
    labelled_at: list[int] = []
    labelled: list[str] = []
    for position, item in enumerate(items):
        properties = cast("dict[str, Any]", getattr(item, "additional_properties", None) or {})
        if "security_label" not in properties:
            continue
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            raise ValueError(
                f"{tool}: item {position} of this result carries a label and no text, so there "
                "is nothing to hold to what this tool committed to."
            )
        labelled_at.append(position)
        labelled.append(text)
    # The guidance is the *suffix*, and that is the third thing the body could otherwise vary.
    # Comparing the labelled texts alone leaves their placement among the unlabelled items
    # free — `[guidance, derived]` and `[derived, guidance]` reduce to the same sequence — and
    # the framework preserves list order, so a reader sees which one ran even where the derived
    # half is hidden. Last is the canonical place because it is where the one shipped kind puts
    # it: a result reads as its answer, then the standing sentence about it.
    expected_at = list(range(len(items) - len(committed), len(items)))
    if labelled != rendered or labelled_at != expected_at:
        # The text is never repeated here: this refusal returns to the model through a path
        # that labels nothing.
        raise ValueError(
            f"{tool}: this result carries {len(labelled)} labelled item(s) and this tool "
            f"committed to {len(rendered)}. They must be the last {len(rendered)} item(s), in "
            "the committed order, each carrying the sentence committed at its position. A "
            "sentence not committed, one missing, one repeated, two out of order, or guidance "
            "placed anywhere but last is a bit about which path ran, which is what a standing "
            "sentence may not be."
        )


def _refuse_a_result_that_carries_no_call_label(result: object, *, tool: str) -> None:
    """Refuse a list of items in which nothing is left to carry the call's own label.

    A per-item label replaces the whole label rather than its integrity alone, so a result
    made only of labelled items has replaced the call's confidentiality with whatever those
    items named — ``public``, for every item this package can build. One unlabelled item is
    what keeps the call's confidentiality in the fold, and an empty list is the same absence
    written differently: the framework renders it to the model as the text ``[]``.
    """
    if not isinstance(result, list):
        return
    items = cast("list[Any]", result)
    if not items:
        raise ValueError(
            f"{tool}: the tool body returned an empty list. MAF renders that to the model as "
            "the text '[]'. Return the string the model should read, or the items it should."
        )
    if any(
        "security_label" not in (getattr(item, "additional_properties", None) or {})
        for item in items
    ):
        return
    raise ValueError(
        f"{tool}: every item of this result carries its own security label, so nothing in it "
        "carries the call's confidentiality. A per-item label replaces the whole label, and "
        "one this package builds names no confidentiality, so such a result is `public` "
        "however the tool or the host classified it. Leave the items that derive from the "
        "call unlabelled — the framework labels those from the call itself."
    )


def sandboxed_tool(
    build: Callable[[SandboxToolSession], Callable[..., Awaitable[str | list[Content]]]],
    *,
    router: SandboxRouter | None,
    context: CallerContext,
    agent_dir: str,
    spec: SandboxSpec,
    name: str,
    approval_mode: Literal["always_require", "never_require"] = "never_require",
    declarations: Mapping[str, Any] | None = None,
    source_integrity: str | None = None,
    outbound_max_confidentiality: str | None = None,
    output_sink: OutputSink | None = None,
    also_carries_out: bool = False,
    nothing_survives_from: Iterable[SourceChannel] = (),
    standing_guidance: Iterable[str] = (),
    on_reclaim_failure: Callable[[ReclaimFailure], Awaitable[None]] | None = None,
    reclaim_timeout: float | None = None,
    file_store_provenance: FileStoreProvenance | None = None,
    logger: logging.Logger | None = None,
) -> list[Any]:
    """Return the one-tool list for a sandbox workload, or ``[]`` when no sandbox is available.

    This is the shape a sandbox workload's factory has.  What follows are decisions rather
    than plumbing, and a workload that re-derives them tends to get one of them wrong:

    1. **Attach nothing when unconfigured.**  ``router is None`` or a router with no backend
       yields ``[]`` — not a tool that fails when called.  A host with nothing configured
       keeps its ungrounded behaviour with no half-attached error path, and the model is
       never shown a capability it does not have.
    2. **Refuse a backend that cannot confine egress** to what ``spec`` allows — see
       :meth:`~maf_sandbox.SandboxRouter.ensure_can_serve`.  This *raises* where the point
       above returns ``[]``: nothing configured is a choice the host made, while a backend
       that cannot honour the spec is a misconfiguration, and the quiet degrade would ship
       the workload with containment it does not have.
    3. **Key from the host, not from the model** — see :meth:`SandboxToolSession.key`.
    4. **Sanitized failure surfaces** — see :meth:`SandboxToolSession.acquire`.
    5. **Declared information flow** — see :func:`sandbox_tool_declarations`.
    6. **Four spec-consistency refusals**, all placed after the attach gate so that the first
       point above keeps its promise.  ``output_sink`` may not be combined with an explicit
       ``declarations=``, which wins verbatim and would leave the tool carrying a derivation
       blind to its own sink; a ``spec`` declaring an output that lands is refused without a
       sink, because such a tool cannot honour its own spec; a ``spec`` declaring any
       output without requiring :data:`~maf_sandbox.Capability.FILES_OUT` is refused, because
       the capability match is what stands between it and a backend with no pull surface; and
       an explicit ``source_integrity="trusted"`` is refused over a ``spec`` that opens a
       channel nothing establishes *as trusted* — see :func:`sandbox_tool_declarations`, which
       owns the rule and the escape.  That last one reads a verbatim ``declarations=`` mapping
       too, for the one key: the mapping is otherwise untouched, but a check the derivation
       alone holds is walked past by the hand-built mapping, which is exactly what a kind
       outside this repository writes.  No escape is read beside such a mapping.
    7. **Reclaim what the call owned**, for an ``async`` body — a synchronous one cannot
       ``await`` :meth:`SandboxToolSession.acquire`, so it holds no sandbox and owns nothing.
       A body that took a path from
       :meth:`SandboxToolSession.guest_call_path` has it removed, with everything under it, when
       the call returns — after a result, a refusal and an exception alike — so a kind cannot
       forget a path it never held.
       A body that never asked for one costs nothing.  See ``on_reclaim_failure`` for the
       removal that does not happen, which is a data-retention failure rather than a tidy-up.
       A ``spec`` whose ``work_dir`` is the guest root is refused, because a path one
       component from the root is one this cannot remove — and only for such a body, since a
       synchronous one is not held to a rule it cannot break.
    8. **A result is one string, or a list of items each carrying its own label.**  The list
       is passed on untouched — see :func:`labelled_result_item` for what may be labelled —
       with one refusal: a list in which *every* item is labelled has nothing left to carry
       the call's own confidentiality, and so has neither the tool's classification nor the
       host's.  Both bodies are held to it, the synchronous one through a wrapper that reads
       the result's shape and does nothing else.

    ``build`` is a callback rather than a decorated function because the session does not
    exist until the attach gate has passed, and the tool body needs it in its closure.  Two
    consequences worth knowing before writing one:

    - The body's **docstring is the tool's description** — MAF passes ``func.__doc__``
      through verbatim, indentation included.  Define the ``build`` callback at module level
      rather than nesting it inside the factory: an extra level of nesting re-indents every
      line of that docstring, which silently rewrites what the model reads.
    - ``build`` runs only when a tool is actually attached, so anything expensive in it costs
      nothing on an unconfigured host.

    A workload that ships more than one tool calls this once per tool and concatenates; each
    call answers the attach gate identically, so an unconfigured host still gets ``[]``.

    Args:
        build: Given the session, returns the async function to expose as the tool.  It
            answers with a ``str``, or with the list of items point 8 above describes.
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        context: How to read the caller's scope and thread, and how to enumerate the
            file store (see :func:`make_caller_context`).
        agent_dir: The agent's directory name. Baked into the sandbox key here, at factory
            time, rather than taken from the model at call time.
        spec: The sandbox this workload asks for.
        name: The tool's name, as declared to the model.
        approval_mode: MAF's per-tool approval setting.
        declarations: ``additional_properties`` to write verbatim, for a workload that wants
            full control. Defaults to :func:`sandbox_tool_declarations` over ``spec``.
            Refused together with ``output_sink``. Written verbatim, and *read* for one key:
            a ``source_integrity`` of ``"trusted"`` is held to the same spec check the
            derivation applies. Nothing else in the mapping is inspected, nothing is derived
            into it, and no keyword is honoured beside it.
        source_integrity: A :class:`~maf_sandbox.SourceIntegrity`, passed to
            :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. ``None`` is the default and declares no integrity at
            all, which hands the answer to the input-label join and the host's
            ``default_integrity``; a workload whose result is whatever model-written code
            chose to emit says :data:`~maf_sandbox.SourceIntegrity.UNTRUSTED` instead of
            leaving it to those. A workload that has earned a label states it here rather
            than through ``declarations=``, which is refused alongside a sink.
        outbound_max_confidentiality: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. Read that function before setting it — it is off by
            default for a reason.
        output_sink: Where this workload's landing artifacts go, threaded into the derivation
            above, carried on the session, and passed on to
            :func:`~maf_sandbox.collect_outputs` by the workload itself.
        file_store_provenance: The host's :class:`~maf_sandbox.FileStoreProvenance`, so
            :meth:`SandboxToolSession.read_file` folds it around the read rather than trusting a
            label as old as the listing. Pass the **same** record the listing callable folds.
            Left unset, the listing's label stands.
        also_carries_out: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. For a workload carrying something out through a channel
            the spec cannot show — a wired host-tool registry, say — so the confidentiality
            cap is derived by the one rule rather than hand-built into ``declarations=``.
        nothing_survives_from: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given, which is refused on its own ``"trusted"`` rather than
            cleared by a keyword. The channels this workload opens and derives nothing from —
            read that function before reaching for it, since declaring ``"untrusted"`` costs
            the model's sight of the result and nothing else.
        standing_guidance: The sentences this tool's result may carry a ``trusted`` label on,
            committed here so rule 5's test is checked rather than left to the body. Every
            labelled item's text must be the committed sentence **at its position** — same
            order, same count — and a bare ``str`` answer is refused once anything is committed,
            since a string on one return path beside a split result on another is itself a bit
            about which path ran. A sentence may interpolate ``{call_id}`` and nothing else,
            rendered from the call rather than by the body, and one that does is refused on a
            body that awaits nothing, which runs inside no call. Each is rendered once here, so
            a sentence that cannot format is refused at attach rather than at every call. Empty
            leaves the check off entirely.
        on_reclaim_failure: Called with a :class:`~maf_sandbox.ReclaimFailure` when the call
            left its sandbox unclean — its guest path could not be removed, or a program it
            stopped may have left something running — **after** the framework has acted on it.
            :attr:`~maf_sandbox.ReclaimFailure.disposal` says what that was: ``disposed`` where
            the framework deleted the sandbox, ``kept`` where the host opted down, and
            ``failed`` where the delete did not land. A **call-scoped** sandbox reports only
            the last of those: deleting it is the cleanup rather than an escalation over a
            failed one, so the callback runs when that delete did not happen — except when the
            call was cancelled *during* it, where the leak reaches the log and nothing else,
            because the callback would be awaiting past a deadline that has already expired.
            This is
            notification: where a host logs, alerts or counts. It is not where safety is
            wired; that is the router's, and a host opts down from it with
            ``ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP)``, never from here.
            Default ``None`` falls back to the router's ``reclaim.on_failure``. Its own failure is
            logged and swallowed — it runs in a ``finally``, over a call that may already be
            failing.
        reclaim_timeout: Seconds the removal gets, per sandbox the call acquired — ordinarily
            one — and separately the seconds the disposal gets, and again the seconds
            the failure hook gets, so a sandbox whose removal fails can cost three times
            it. Default ``None`` falls back to the router's ``reclaim.timeout`` (which defaults to
            ``30.0``). Spent after the body has returned, so it is added
            to the call's own latency and an outer deadline should allow for it. A body that
            was **cancelled** gets :data:`_CANCELLED_CALL_GRACE` instead, or this, whichever is
            smaller: its caller's deadline has already passed, and the removal must not extend
            one that has.
        logger: Where the failure ladder writes its detail. Defaults to this module's logger;
            pass the workload's own so its records keep the workload's logger name.
    """
    if router is None or not router.enabled:
        return []
    if output_sink is not None and declarations is not None:
        raise ValueError(
            f"{name}: pass either output_sink or declarations=, never both. An explicit "
            "mapping is written verbatim, so the pair would attach a tool whose declarations "
            "know nothing about its sink — the confidentiality cap silently absent rather "
            "than the one the host chose. Drop declarations= and pass "
            "outbound_max_confidentiality, or write the cap into the mapping yourself."
        )
    # The one key read out of a verbatim mapping, and raw: FIDES acts on exactly this spelling
    # (`IntegrityLabel(value)`, anything else logged and dropped), so an unrecognised value is
    # not a claim to refuse — and the mapping's vocabulary is the host's, not this package's.
    if declarations is not None and declarations.get("source_integrity") == SourceIntegrity.TRUSTED:
        unestablished = _source_channels_not_established_as_trusted(spec, frozenset())
        if unestablished:
            raise _unlicensed_trusted_claim_refusal(
                spec, unestablished, asked_by=name, through_mapping=True
            )
    if spec_lands_artifacts(spec) and output_sink is None:
        raise missing_sink_refusal(spec, landing_outputs(spec), asked_by=name)
    if (spec.declared_outputs or spec.outputs_named_at_call_time) and (
        Capability.FILES_OUT not in spec.requires
    ):
        declares = ", ".join(repr(declared.path) for declared in spec.declared_outputs)
        if spec.outputs_named_at_call_time:
            declares = f"{declares}, plus names at call time" if declares else "names at call time"
        raise ValueError(
            f"{name}: the {spec.kind!r} workload declares {declares} as outputs and does not "
            f"require {str(Capability.FILES_OUT)!r}. Grow `requires` from what you declare: "
            "the pull surface is what reads those paths back, and without the requirement the "
            "router's capability match never asks whether this backend has one — leaving the "
            "failure to happen inside the sandbox, where the reason is hardest to see."
        )
    effective_timeout = reclaim_timeout if reclaim_timeout is not None else router.reclaim.timeout
    if not math.isfinite(effective_timeout) or effective_timeout <= 0:
        raise ValueError(
            f"{name}: reclaim_timeout must be a finite positive number of seconds, not "
            f"{effective_timeout}. It bounds a removal that runs in a `finally`, so an infinite "
            "one is a tool call that never returns."
        )
    effective_on_failure = (
        on_reclaim_failure if on_reclaim_failure is not None else router.reclaim.on_failure
    )
    router.ensure_can_serve(spec)

    records = logger if logger is not None else _DEFAULT_LOGGER
    if router.selection is Selection.PER_SPEC:
        # Once, at attach, and only where the answer is not already in the host's own
        # configuration: under the fixed selection a host reads `router.backend` and knows.
        # Not inside `ensure_can_serve`, which `acquire` runs on every tool call — a record
        # per call would put a log line in a warm fix-round loop for a fact that cannot change,
        # since the route is a pure function of a spec that is fixed by now.
        served = router.backend_for(spec)
        records.info(
            "%s: the %r workload routes to sandbox backend %s",
            name,
            spec.kind,
            "nothing" if served is None else repr(served.name),
        )
    session = SandboxToolSession(
        router,
        context,
        agent_dir,
        spec,
        name=name,
        logger=records,
        output_sink=output_sink,
        file_store_provenance=file_store_provenance,
    )
    properties = (
        dict(declarations)
        if declarations is not None
        else sandbox_tool_declarations(
            spec,
            source_integrity=source_integrity,
            outbound_max_confidentiality=outbound_max_confidentiality,
            output_sink=output_sink,
            also_carries_out=also_carries_out,
            nothing_survives_from=nothing_survives_from,
            isolation_scope=router.effective_isolation_scope(spec),
        )
    )

    # Imported here rather than at module scope so that merely importing this module — which
    # a host may do to reach `make_caller_context` alone — does not pull the framework in.
    from agent_framework import tool

    decorate = tool(
        name=name,
        approval_mode=approval_mode,
        additional_properties=properties,
    )
    body = build(session)
    # Validated here rather than at first use: a sentence that cannot render is a wiring
    # mistake in a kind, and finding it at attach costs a reviewer nothing.
    committed = _committed_guidance(standing_guidance, tool=name, awaits=_awaits(body))
    if not _awaits(body):
        # `acquire` is a coroutine, so a body that awaits nothing can hold no sandbox and owns
        # nothing to reclaim, and this wrapper reads the result's shape and does nothing else.
        # It stays synchronous so MAF still runs the body off the event loop the way it runs
        # any synchronous tool.
        @functools.wraps(body)
        def checked(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            raised_by_body: BaseException | None = None
            try:
                try:
                    result = body(*args, **kwargs)
                except BaseException as raised:
                    raised_by_body = raised
                    raise
                _refuse_a_result_that_carries_no_call_label(result, tool=name)
                _refuse_a_result_that_departs_from_its_guidance(
                    result, tool=name, committed=committed, call_id=None
                )
                return result
            finally:
                if router.observer is not None:
                    record(
                        router.observer,
                        ToolCallEnded(
                            tool=name,
                            kind=spec.kind,
                            # Always empty, and not for want of looking: `acquire` is a
                            # coroutine, so a body that awaits nothing holds no sandbox — which
                            # is the same reason there is nothing to reclaim and nothing to be
                            # unclean about.
                            keys=(),
                            seconds=time.monotonic() - started,
                            failure=None
                            if raised_by_body is None
                            else type(raised_by_body).__name__,
                            unclean=0,
                        ),
                        records,
                    )

        return [decorate(checked)]
    if not [part for part in posixpath.normpath(spec.work_dir).split("/") if part]:
        # Here rather than with the spec refusals above, because it constrains only a tool that
        # can reclaim: a body that never receives the wrapper cannot leave a path behind, and
        # refusing it would be a rule enforcing something that cannot happen.
        raise ValueError(
            f"{name}: the {spec.kind!r} workload's work_dir is {spec.work_dir!r}, which leaves "
            "a call's own path one component from the guest root. Reclaiming one recursively is "
            "refused at that depth, so every call would keep its files and report a retention "
            "failure it could do nothing about. Give the workload a directory of its own."
        )

    # `functools.wraps` is what keeps MAF reading the *body* — the description is `__doc__`,
    # the parameter schema is `inspect.signature` plus `get_type_hints`, the context injection
    # is the signature again. Without it each fails silently and towards the model: no
    # description, a schema with no parameters, every parameter degraded to `str`. No docstring
    # here for the same reason — one would become what a model reads.
    @functools.wraps(body)
    async def reclaiming(*args: Any, **kwargs: Any) -> Any:
        call = _SandboxToolCall(owner=session)
        token = _CALL.set(call)
        started = time.monotonic()
        # What a transport notes about the sandbox during the body — a stop that did not
        # reach everything — read back once the body has returned.
        unclean, notes = open_unclean_notes()
        # What the *body* raised, kept apart from what this wrapper's own label check raises: a
        # body that returned and then failed that check did not fail, and a record saying it did
        # would send an operator looking at the workload instead of at the tool's declaration.
        raised_by_body: BaseException | None = None
        try:
            try:
                result = await body(*args, **kwargs)
            except BaseException as raised:
                raised_by_body = raised
                raise
            _refuse_a_result_that_carries_no_call_label(result, tool=name)
            # The id is read rather than allocated where nothing committed needs it, so a
            # tool with no `{call_id}` sentence keeps the lazy allocation it had.
            _refuse_a_result_that_departs_from_its_guidance(
                result,
                tool=name,
                committed=committed,
                call_id=_call_name(call) if _needs_call_id(committed) else None,
            )
            return result
        finally:
            _CALL.reset(token)
            close_unclean_notes(notes)
            # Closed before the removal, not after: a task the body left running would otherwise
            # be handed this path while it is being deleted.
            call.closed = True
            # The body's own escape, captured where it happened rather than read from
            # `sys.exception()` here: this frame also sees what the label check raised, and past
            # the removal it would see the removal's own cancel.
            failed = raised_by_body
            bound = effective_timeout
            if isinstance(failed, (asyncio.CancelledError, GeneratorExit)):
                bound = min(effective_timeout, _CANCELLED_CALL_GRACE)
            try:
                await _reclaim_the_call(
                    call,
                    router=router,
                    spec=spec,
                    tool=name,
                    logger=records,
                    on_failure=effective_on_failure,
                    timeout=bound,
                    unclean=unclean,
                )
            finally:
                if router.observer is not None:
                    record(
                        router.observer,
                        ToolCallEnded(
                            tool=name,
                            kind=spec.kind,
                            # Every key the call touched — acquired, refused, or only read the
                            # store under — so each of its other events has a call to join to.
                            # `acquired` would name the served ones alone.
                            keys=tuple(call.touched),
                            seconds=time.monotonic() - started,
                            failure=None if failed is None else type(failed).__name__,
                            unclean=len(unclean),
                        ),
                        records,
                    )

    return [decorate(reclaiming)]


def _landed_in_store(artifact: Artifact, destination: str) -> str:
    """The default line a model sees for an artifact landed in a store: where, and how big."""
    return f"{destination} ({len(artifact.content)} bytes)"


def make_file_store_sink(
    store: Any,
    *,
    provenance: FileStoreProvenance | None = None,
    display: Callable[[Artifact, str], str] = _landed_in_store,
) -> OutputSink:
    """An :class:`~maf_sandbox.OutputSink` landing each call's artifacts under ``<call_id>/`` in
    ``store``, for a model that reads them back with its own file tools.

    Point it at a store the model can **read and not write**, and never at the one the agent's
    ``file_access_write`` writes to: a sink landing where that tool writes has handed
    model-authored code an unapproved write.  :func:`sandbox_outputs_read_tools` is what reads
    it back.  The folder is the *host-minted* call id, passed through
    :func:`~maf_sandbox.collect_outputs`'s ``call_id`` — required rather than optional, because
    the sink declares :attr:`~maf_sandbox.OutputSink.per_call`.

    Four things a caller has to know:

    - **A destination that already exists is refused, never replaced**, so one call's answer
      cannot come to read as another's.  Unconditionally, unlike
      :func:`~maf_sandbox.make_file_system_sink`: the folder is a call id, so a collision here
      is not a shared root but the same call landing one name twice.
    - **``provenance`` is recorded before the bytes are written**, so no moment exists at which
      the file is there and the host's floor still answers for it.  Recording only ever lowers,
      so an entry left behind by a write that then failed is safe.
    - **Text only.**  ``AgentFileStore.write`` takes a ``str``, so an artifact whose bytes are
      not UTF-8 is refused with :class:`~maf_sandbox.SandboxLandingNotText` rather than mangled.
    - **No confinement check of its own**, unlike :func:`~maf_sandbox.make_file_system_sink`:
      ``AgentFileStore`` requires its implementations to reject a path that escapes the root.

    Nothing here creates the folder.  Both shipped stores list one that does not exist as empty;
    a store that raises there instead needs a host wrapper.  ``docs/sandbox/hosts.md`` carries
    the wiring, the measurements behind it and the trade.

    Args:
        store: The ``agent_framework`` ``AgentFileStore`` to land in.
        provenance: The host's record for **this** store.  Every landing is recorded, so a
            ``TRUSTED`` floor never answers for bytes a guest produced.  ``None`` records
            nothing, which is honest only for a host keeping no record at all.
        display: The one line the model is shown per landing.  The default names the store path
            and the size; a host that would rather say less supplies its own.

    Raises:
        ValueError: when an artifact reaches ``deliver`` with no ``call_id``.
        SandboxLandingNotText: when an artifact's bytes are not valid UTF-8.
        SandboxLandingExists: when the store already holds the destination and says so with
            ``FileExistsError``, which both of ``agent_framework``'s own stores do.  A store
            refusing in some other vocabulary propagates its own exception.
    """

    async def deliver(artifact: Artifact) -> LandedArtifact:
        if artifact.call_id is None:
            raise ValueError(
                "make_file_store_sink was handed an artifact with no call_id, so there is no "
                "folder to land it in. Pass collect_outputs(call_id=...)."
            )
        try:
            content = artifact.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxLandingNotText(
                f"artifact {artifact.name!r} is not valid UTF-8, and this sink lands into a "
                "file store that holds text. Land it somewhere that takes bytes."
            ) from exc
        destination = f"{artifact.call_id}/{artifact.name}"
        if provenance is not None:
            provenance.record(destination)
        try:
            await store.write(destination, content, overwrite=False)
        except FileExistsError as exc:
            raise SandboxLandingExists(
                f"artifact {artifact.name!r} is already in the store under this call's "
                "folder, and this sink refuses to replace one."
            ) from exc
        return LandedArtifact(
            name=artifact.name,
            display=display(artifact, destination),
            handle=destination,
        )

    return OutputSink(deliver, per_call=True)


#: What the two read-back tools are called by default, and the reason they are named at all.
#: ``FileAccessProvider`` names its own from class constants read inside its tool decorators, so
#: two of those contribute two ``file_access_read`` tools rather than two stores — and the model
#: is then handed a name it cannot use to say which one it means. A prefix, because a host with
#: two output stores hits the same wall one layer up.
DEFAULT_OUTPUTS_TOOL_PREFIX = "sandbox_outputs"


#: The two descriptions the model reads, at module level for the reason a kind's are: MAF passes
#: ``__doc__`` through verbatim, indentation and all, and a docstring written on a nested
#: function arrives re-indented by however deep it was nested.
_OUTPUTS_LS_DESCRIPTION = """List what is in a folder of the store a sandboxed tool's declared
        outputs land in.

        Each call's outputs go into a folder of their own, named for that call, and the tool
        result names the folder.  Omit ``folder`` to list the folders themselves.

        Args:
            folder: The folder to list, or empty for the top level.

        Returns:
            One entry per child, each with a ``name`` — the child's own name, not a path — and a
            ``type`` of file or directory.
        """

_OUTPUTS_READ_DESCRIPTION = """Read one file out of the store a sandboxed tool's declared
        outputs land in.

        Args:
            name: The folder you listed, joined to the name the listing gave it, with a ``/``
                between them — ``<call>/report.md``.  A listing names children only, so a name
                on its own does not locate the file.

        Returns:
            The file's text, or a message saying why it could not be read.
        """


def sandbox_outputs_read_tools(
    store: Any,
    *,
    name_prefix: str = DEFAULT_OUTPUTS_TOOL_PREFIX,
    approval_mode: Literal["always_require", "never_require"] = "never_require",
) -> list[Any]:
    """List and read, over one store and nothing else — how a model reads back what a sandbox
    produced.

    The other half of :func:`make_file_store_sink`.  That lands each call's artifacts under a
    folder of its own; these are what let the model open the folder, so a workload's result can
    name *where* its outputs went rather than reciting which of them landed.

    **Read-only by construction rather than by a flag.**  There is no write, no delete and no
    replace here to disable, which is the property the whole composition rests on: the bytes in
    this store were produced by model-authored code, and a model that could write here could
    plant an input for a later call of a different tool.

    **These tools carry no label.**  Their results resolve through the host's own
    ``default_integrity`` and its information-flow middleware, exactly as the framework's file
    tools do — which is the point, and the trade worth reading twice: a host that withholds a
    workload's guest output and then wires this has not kept that output away from the model, it
    has moved it onto a path the host classifies and can gate.  Wire ``approval_mode`` and the
    store's scope accordingly.

    ``store`` is scoped by the host, and per conversation if the working store is: nothing here
    knows about threads, so one store shared across conversations is one conversation reading
    another's outputs.

    Args:
        store: The ``agent_framework`` ``AgentFileStore`` the sink lands in.
        name_prefix: What the two tools are called — ``<prefix>_ls`` and ``<prefix>_read``.
        approval_mode: MAF's per-tool approval setting, the host's to choose.

    Returns:
        The two tools, to pass to an agent beside the workload's own.
    """
    from agent_framework import tool

    # Both bodies render their argument before touching the store, not in the branch that
    # needs it: the framework's accessor is not scoped to the call, so a verdict asked for
    # after the store has suspended can come back empty and quote content the middleware hid.
    async def outputs_ls(folder: str = "") -> list[dict[str, str]] | str:
        named = _echoed(folder, "folder")
        try:
            listed = await store.list_children(folder)
        except Exception as exc:  # noqa: BLE001
            _DEFAULT_LOGGER.warning(
                "%s_ls: could not list a folder: %s", name_prefix, error_detail(exc)
            )
            return f"Error: {named} could not be listed."
        return [{"name": entry.name, "type": entry.type} for entry in listed]

    async def outputs_read(name: str) -> str:
        named = _echoed(name, "name")
        try:
            content = await store.read(name)
        except Exception as exc:  # noqa: BLE001
            _DEFAULT_LOGGER.warning(
                "%s_read: could not read a file: %s", name_prefix, error_detail(exc)
            )
            return f"Error: {named} could not be read."
        if content is None:
            return f"Error: there is no file at {named}."
        return content

    outputs_ls.__doc__ = _OUTPUTS_LS_DESCRIPTION
    outputs_read.__doc__ = _OUTPUTS_READ_DESCRIPTION
    return [
        tool(name=f"{name_prefix}_ls", approval_mode=approval_mode)(outputs_ls),
        tool(name=f"{name_prefix}_read", approval_mode=approval_mode)(outputs_read),
    ]


def _echoed(value: str, argument: str) -> str:
    """One argument of a read-back tool, rendered the way every refusal here renders one.

    The framework expands a variable reference into a string argument before the body runs, so
    a refusal quoting its own argument can put back content the middleware had hidden — the
    same failure a kind's refusals are held to, on a tool the host owns rather than a kind.
    """
    rewritten = positions_holding_hidden_content([value], argument=argument)
    return echoed_name(value, at=argument, hidden=0 in rewritten)


async def list_all_files(
    store: Any, *, provenance: FileStoreProvenance | None = None
) -> list[ListedFile]:
    """Every file in ``store``, each with what the host knows about its integrity.

    The listing a workload is given is its **injection-pinning boundary**: only a name that
    appears in it is ever substituted into a sandbox command, so a path the model invented
    reaches no shell.  This walks, because ``list_children`` answers one level at a time and
    the recursion is the host's to do rather than the store's.

    It lives in this module rather than in core for the dependency, not the audience: the
    entries it walks are ``agent_framework``'s and it reads their ``type``.

    A failure propagates.  Answering an empty list would read as "the store has no files" and
    refuse every name for the wrong reason.

    Args:
        store: The agent file store to walk.
        provenance: The host's record for *this* store, if it keeps one.  Each name is looked up
            in it, so the listing carries what the host knows rather than what a kind could
            guess.  ``None`` labels every entry ``None`` — unestablished, which is what a host
            keeping no record honestly knows — and is not a synonym for untrusted.
    """
    files: list[ListedFile] = []

    async def walk(directory: str) -> None:
        for entry in await store.list_children(directory):
            child = f"{directory}/{entry.name}" if directory else entry.name
            if entry.type == "directory":
                await walk(child)
            else:
                files.append(
                    ListedFile(
                        child, None if provenance is None else provenance.integrity_of(child)
                    )
                )

    await walk("")
    return files


async def list_no_files(_store: object) -> list[ListedFile]:
    """Enumerate nothing — the listing for a workload with no file channel at all.

    Not a stub standing in for unfinished work.  ``CallerContext.list_files`` is required, so
    a workload that shares nothing still has to answer, and saying so by name keeps that a
    stated decision rather than an empty lambda the next reader has to interpret.
    """
    return []
