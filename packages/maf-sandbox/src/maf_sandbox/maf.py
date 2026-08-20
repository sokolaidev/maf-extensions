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

Five things live here, and each of them had begun to exist twice before it did:

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
"""

from __future__ import annotations

import functools
import inspect
import logging
import math
import posixpath
from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from ._error_detail import error_detail
from ._outputs import OutputSink, landing_outputs, missing_sink_refusal, spec_lands_artifacts
from ._protocol import (
    CallerContext,
    Capability,
    Sandbox,
    SandboxKey,
    SandboxSpec,
)
from ._purger import SandboxPurger
from ._reclaim import ReclaimFailure, reclaim_guest_path
from ._router import NoSandboxBackend, SandboxRouter

#: Fallback for :func:`sandboxed_tool`'s ``logger`` argument. Named apart from the usual
#: module-level ``logger`` because that argument is the whole point: a workload passes its
#: own logger so the failure ladder's records keep the workload's logger name, and only a
#: caller that does not care lands here.
_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = [
    "SandboxPurger",
    "SandboxToolSession",
    "list_all_files",
    "list_no_files",
    "make_caller_context",
    "sandbox_tool_declarations",
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


@dataclass
class _SandboxToolCall:
    """What one tool call has done that the ``finally`` has to undo.

    ``owner`` is the session whose wrapper opened the call. One `ContextVar` serves every
    binding in the process, so a body that reaches a *second* session would otherwise record
    that session's sandbox here and have its own path removed from the wrong one.
    """

    owner: object
    path: str | None = None
    acquired: tuple[Sandbox, SandboxKey] | None = None


#: The call a tool body is running inside, or ``None`` outside one.
#:
#: Not an attribute on the session: one session serves every concurrent call to its tool, so two
#: parallel calls would be handed the same path and the first to finish would remove one the
#: other is still running in. A task starts from a copy of its parent's context, so a child
#: reads the record and cannot reach a sibling's — but a child outliving the call reads a path
#: the ``finally`` has already removed, and nothing will reclaim what it writes after that.
_CALL: ContextVar[_SandboxToolCall | None] = ContextVar("maf_sandbox_call", default=None)


def _this_call(owner: object) -> _SandboxToolCall | None:
    """The call ``owner`` is running inside, or ``None`` — including when it belongs elsewhere."""
    call = _CALL.get()
    return call if call is not None and call.owner is owner else None


def _prefixed(name: str) -> str:
    """``name``, safe to bake into a logging format string.

    The tool's name prefixes every record this module writes, and it is baked into the FORMAT
    rather than passed as an argument so the record is indistinguishable from one the workload
    wrote by hand — ``record.msg`` included, which is what a structured exporter reads and what
    a caplog assertion matches. A ``%`` in a name would then read as a format specifier.
    """
    return name.replace("%", "%%")


def make_caller_context(
    list_files: Callable[[Any], Awaitable[list[str]]],
    scope_getter: Callable[[], str],
    thread_getter: Callable[[], str | None],
) -> CallerContext:
    """Build the :class:`~maf_sandbox.CallerContext` a host hands to a workload factory.

    Args:
        list_files: Given the file store, returns the paths the caller may act on.
            A workload treats that listing as its injection-pinning boundary: only a name
            present in it is ever substituted into a command.
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


def sandbox_tool_declarations(
    spec: SandboxSpec,
    *,
    source_integrity: str | None = "trusted",
    outbound_max_confidentiality: str | None = None,
    output_sink: OutputSink | None = None,
) -> dict[str, Any]:
    """The information-flow declarations a sandbox workload's tool carries.

    These land on the tool's ``additional_properties``, where MAF's information-flow module
    (``agent_framework.security``, FIDES) reads them before every call: ``source_integrity``
    decides whether this tool's *output* taints the conversation, and
    ``max_allowed_confidentiality`` caps how confidential a conversation may be and still be
    allowed to call it.

    ``source_integrity="trusted"`` is the default because a sandbox result is deterministic
    first-party output — a compiler's diagnostics, a script's stdout — produced by an
    environment with no ambient identity and a deny-by-default egress allowlist.  Pass
    ``None`` for a workload where that is not true (a sandbox that fetches arbitrary web
    content, say): undeclared, the tracker's untrusted default applies and the result taints
    the conversation, which is the fail-safe direction.

    ``outbound_max_confidentiality`` is **opt-in, and off by default**, and the asymmetry is
    deliberate.  A confidentiality key is not inert metadata: writing one participates in a
    policy leg that may be dormant in the host — a host whose tools never label anything
    above its own cap has a confidentiality check that cannot currently fire — so declaring
    it can change which calls are gated or refused.  That is the host's decision to make with
    its own classification in hand, never a default a library picks.  When it *is* passed, the
    key is written only if this tool can carry something out at all: the spec permits egress
    (``egress_allow`` non-empty), or the spec declares an output that **lands** in
    ``output_sink``.  Capping a workload with neither would gate calls for a flow that does not
    exist.

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
        spec: The sandbox this workload asks for; ``egress_allow``, ``declared_outputs`` and
            ``outputs_named_at_call_time`` are what is read.
        source_integrity: Integrity tier for this tool's results, or ``None`` to declare
            none.
        outbound_max_confidentiality: The host's cap for outbound tools, in the host's own
            vocabulary, or ``None`` (the default) to declare none.
        output_sink: Where this workload's artifacts land, if it lands any. Read together with
            what the spec says it lands, never for its presence alone.
    """
    declarations: dict[str, Any] = {}
    if source_integrity is not None:
        declarations["source_integrity"] = source_integrity
    lands_artifacts = output_sink is not None and spec_lands_artifacts(spec)
    carries_something_out = bool(spec.egress_allow) or lands_artifacts
    if outbound_max_confidentiality is not None and carries_something_out:
        declarations["max_allowed_confidentiality"] = outbound_max_confidentiality
    return declarations


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

    All three accessors return **either the value or the string the tool should return**, rather
    than raising.  A MAF tool answers with a ``str`` and a refusal is an ordinary answer, not
    an exception: the model has to learn what happened through the same channel as a
    successful result, or the turn ends mute.  So a body reads::

        key = session.key()
        if isinstance(key, str):
            return key
        ...
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

    A workload whose tool returns something other than a plain ``str`` translates the message
    into its own result shape at those two points; nothing else about the contract changes.
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
    ) -> None:
        self._router = router
        self._context = context
        self._agent_dir = agent_dir
        self._spec = spec
        self._name = name
        self._logger = logger
        self._output_sink = output_sink
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

    def key(self) -> SandboxKey | str:
        """The sandbox key for this call, or the message to return when no thread is bound.

        Scope and thread come from the host's request context — never from model input: a
        model-supplied scope would let one conversation address another's sandbox.  The agent
        directory is baked in at factory time for the same reason.

        A call with no bound conversation is refused rather than served from a placeholder
        key, because a shared fallback key is exactly the cross-conversation reach the key
        exists to prevent.
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
        )

    def guest_call_path(self) -> str:
        """This call's own place **inside the sandbox**, under the spec's ``work_dir``.

        Allocated once per call and reclaimed with everything under it when the call returns, so
        a kind that puts its files here cannot leave them behind. Apart from the three accessors
        above: it raises rather than answering with a message, because reaching it wrongly is a
        wiring mistake in a kind, not something a model can cause or should be told about.

        Nothing is created — it is a name until a kind writes to it. ``path`` rather than
        ``directory`` because that is all the protocol promises: a backend serving its store from
        memory, or with no enumeration primitive under it, addresses one the same way.

        The reclaim covers what was written through the sandbox :meth:`acquire` returned. A kind
        that writes here through a sandbox it got elsewhere keeps what it wrote.

        Raises:
            RuntimeError: Called outside a tool call, where nothing would reclaim what it names.
        """
        call = _this_call(self)
        if call is None:
            raise RuntimeError(
                f"{self._name}: guest_call_path() was called outside a tool call, so nothing "
                "would reclaim what it names. Call it from the tool body."
            )
        if call.path is None:
            call.path = f"{self._spec.work_dir}/{uuid4().hex[:12]}"
        return call.path

    async def list_files(self, store: Any) -> list[str] | str:
        """The paths this caller may act on, or the message to return if they cannot be read.

        The listing is a workload's injection-pinning boundary: only a name present in it is
        ever substituted into a command, so a name the model invented — or one it read out of
        a poisoned file — has nowhere to go.  Which makes a failure to enumerate a refusal
        rather than an empty listing: an empty list would look like "the file store has no
        files" and refuse each name individually with the wrong reason.

        The store is passed per call rather than held: which store a workload reads is the
        workload's business, and some read more than one.
        """
        try:
            return await self._context.list_files(store)
        except Exception as exc:  # noqa: BLE001
            return f"Error: could not list the file store: {exc}"

    async def acquire(self, key: SandboxKey) -> Sandbox | str:
        """A running sandbox for ``key``, or the message to return when there is none.

        The four-branch ladder is this method's whole point, and the line it draws is a
        security one rather than a stylistic one:

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
        """
        try:
            sandbox = await self._router.acquire(key, self._spec)
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
        except Exception as exc:  # noqa: BLE001
            # A provider/transport failure — its detail can carry endpoint, subscription and
            # tenant ids, so it goes to the log and never into the model's context.
            self._logger.warning(f"{self._log_prefix}: sandbox unavailable: %s", error_detail(exc))
            return _SANDBOX_UNAVAILABLE
        call = _this_call(self)
        if call is not None:
            # Recorded on the way through rather than re-derived in the `finally`, where a
            # second `acquire` could fail on its own and report a reclaim failure for it.
            call.acquired = (sandbox, key)
        return sandbox


async def _reclaim_the_call(
    call: _SandboxToolCall,
    *,
    spec: SandboxSpec,
    tool: str,
    logger: logging.Logger,
    on_failure: Callable[[ReclaimFailure], Awaitable[None]] | None,
    timeout: float,
) -> None:
    """Remove what one tool call owns, and report a removal that did not happen.

    Never raises. It runs in :func:`sandboxed_tool`'s ``finally``, where an exception would
    replace whatever the call was already reporting with a message about cleanup.
    """
    if call.path is None or call.acquired is None:
        # Nothing was named, or nothing was acquired to write into it — either way there is
        # nothing there, and no round trip is worth spending to prove it.
        return
    sandbox, key = call.acquired
    prefix = _prefixed(tool)
    try:
        reason = await reclaim_guest_path(
            sandbox, call.path, working_directory=spec.work_dir, timeout=timeout
        )
    except (CancelledError, GeneratorExit):
        # Recorded and then let through. Cancellation is the caller's — an outer deadline
        # arriving while the removal is in flight — and containing it here would have the call
        # return the body's answer past a bound the host thought it had. The leak still has to
        # be visible, so the line is written before the cancellation goes on.
        logger.warning(
            f"{prefix}: %s was not reclaimed: the call was cancelled during the removal",
            call.path,
        )
        raise
    if reason is None:
        return
    # Logged whether or not a host is listening: what is left stays readable by every later
    # call in this sandbox, and a callback that swallows it would take the record with it.
    logger.warning(f"{prefix}: %s was not reclaimed: %s", call.path, reason)
    if on_failure is None:
        return
    try:
        await on_failure(ReclaimFailure(tool=tool, key=key, path=call.path, reason=reason))
    except Exception as raised:  # noqa: BLE001 — a host's callback must not fail the call
        logger.warning(f"{prefix}: on_reclaim_failure raised: %s", error_detail(raised))
    except (CancelledError, GeneratorExit):
        # Not contained, for the reason above: the callback awaits, so this is the caller's
        # cancellation arriving inside it and not a failure of the callback's own.
        logger.warning(f"{prefix}: on_reclaim_failure did not finish: the call was cancelled")
        raise


def sandboxed_tool(
    build: Callable[[SandboxToolSession], Callable[..., Awaitable[str]]],
    *,
    router: SandboxRouter | None,
    context: CallerContext,
    agent_dir: str,
    spec: SandboxSpec,
    name: str,
    approval_mode: Literal["always_require", "never_require"] = "never_require",
    declarations: Mapping[str, Any] | None = None,
    source_integrity: str | None = "trusted",
    outbound_max_confidentiality: str | None = None,
    output_sink: OutputSink | None = None,
    on_reclaim_failure: Callable[[ReclaimFailure], Awaitable[None]] | None = None,
    reclaim_timeout: float = 30.0,
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
    6. **Three spec-consistency refusals**, all placed after the attach gate so that the first
       point above keeps its promise.  ``output_sink`` may not be combined with an explicit
       ``declarations=``, which wins verbatim and would leave the tool carrying a derivation
       blind to its own sink; a ``spec`` declaring an output that lands is refused without a
       sink, because such a tool cannot honour its own spec; and a ``spec`` declaring any
       output without requiring :data:`~maf_sandbox.Capability.FILES_OUT` is refused, because
       the capability match is what stands between it and a backend with no pull surface.
    7. **Reclaim what the call owned.**  A body that took a path from
       :meth:`SandboxToolSession.guest_call_path` has it removed, with everything under it, when
       the call returns — after a result, a refusal and an exception alike — so a kind cannot
       forget a path it never held.
       A body that never asked for one costs nothing.  See ``on_reclaim_failure`` for the
       removal that does not happen, which is a data-retention failure rather than a tidy-up.
       A ``spec`` whose ``work_dir`` is the guest root is refused here, because a path one
       component from the root is one this cannot remove.

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
        build: Given the session, returns the async function to expose as the tool.
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
            Refused together with ``output_sink``.
        source_integrity: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. ``None`` declares no integrity at all, which is the
            fail-safe answer for a workload whose result is whatever model-written code chose
            to emit — and the reason this is a parameter here rather than something a kind
            reaches ``declarations=`` for, since that escape hatch is refused alongside a sink.
        outbound_max_confidentiality: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. Read that function before setting it — it is off by
            default for a reason.
        output_sink: Where this workload's landing artifacts go, threaded into the derivation
            above, carried on the session, and passed on to
            :func:`~maf_sandbox.collect_outputs` by the workload itself.
        on_reclaim_failure: Called with a :class:`~maf_sandbox.ReclaimFailure` when a call's
            own guest path could not be removed. Default ``None`` logs it and carries on; a host that
            needs the data provably gone disposes the sandbox from here, which is the only
            remedy that closes the window rather than narrowing it. Its own failure is logged
            and swallowed — it runs in a ``finally``, over a call that may already be failing.
        reclaim_timeout: Seconds the removal gets. Spent after the body has returned, so it is
            added to the call's own latency.
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
    if not [part for part in posixpath.normpath(spec.work_dir).split("/") if part]:
        raise ValueError(
            f"{name}: the {spec.kind!r} workload's work_dir is {spec.work_dir!r}, which leaves "
            "a call's own path one component from the guest root. Reclaiming one recursively is "
            "refused at that depth, so every call would keep its files and report a retention "
            "failure it could do nothing about. Give the workload a directory of its own."
        )
    if not math.isfinite(reclaim_timeout) or reclaim_timeout <= 0:
        raise ValueError(
            f"{name}: reclaim_timeout must be a finite positive number of seconds, not "
            f"{reclaim_timeout}. It bounds a removal that runs in a `finally`, so an infinite "
            "one is a tool call that never returns."
        )
    router.ensure_can_serve(spec)

    records = logger if logger is not None else _DEFAULT_LOGGER
    session = SandboxToolSession(
        router,
        context,
        agent_dir,
        spec,
        name=name,
        logger=records,
        output_sink=output_sink,
    )
    properties = (
        dict(declarations)
        if declarations is not None
        else sandbox_tool_declarations(
            spec,
            source_integrity=source_integrity,
            outbound_max_confidentiality=outbound_max_confidentiality,
            output_sink=output_sink,
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
    if not inspect.iscoroutinefunction(body):
        # `acquire` is a coroutine, so a synchronous body can hold no sandbox and owns nothing
        # to reclaim. Left unwrapped rather than wrapped-and-skipped: MAF runs a sync tool off
        # the event loop, and this predicate is the one it decides that with.
        return [decorate(body)]

    # `functools.wraps` is what keeps MAF reading the *body* — the description is `__doc__`,
    # the parameter schema is `inspect.signature` plus `get_type_hints`, the context injection
    # is the signature again. Without it each fails silently and towards the model: no
    # description, a schema with no parameters, every parameter degraded to `str`. No docstring
    # here for the same reason — one would become what a model reads.
    @functools.wraps(body)
    async def reclaiming(*args: Any, **kwargs: Any) -> Any:
        call = _SandboxToolCall(owner=session)
        token = _CALL.set(call)
        try:
            return await body(*args, **kwargs)
        finally:
            _CALL.reset(token)
            await _reclaim_the_call(
                call,
                spec=spec,
                tool=name,
                logger=records,
                on_failure=on_reclaim_failure,
                timeout=reclaim_timeout,
            )

    return [decorate(reclaiming)]


async def list_all_files(store: Any) -> list[str]:
    """Every file in ``store``, as store-relative paths.

    The listing a workload is given is its **injection-pinning boundary**: only a name that
    appears in it is ever substituted into a sandbox command, so a path the model invented
    reaches no shell.  This walks, because ``list_children`` answers one level at a time and
    the recursion is the host's to do rather than the store's.

    It lives in this module rather than in core for the dependency, not the audience: the
    entries it walks are ``agent_framework``'s and it reads their ``type``.

    A failure propagates.  Answering an empty list would read as "the store has no files" and
    refuse every name for the wrong reason.
    """
    paths: list[str] = []

    async def walk(directory: str) -> None:
        for entry in await store.list_children(directory):
            child = f"{directory}/{entry.name}" if directory else entry.name
            if entry.type == "directory":
                await walk(child)
            else:
                paths.append(child)

    await walk("")
    return paths


async def list_no_files(_store: object) -> list[str]:
    """Enumerate nothing — the listing for a workload with no file channel at all.

    Not a stub standing in for unfinished work.  ``CallerContext.list_files`` is required, so
    a workload that shares nothing still has to answer, and saying so by name keeps that a
    stated decision rather than an empty lambda the next reader has to interpret.
    """
    return []
