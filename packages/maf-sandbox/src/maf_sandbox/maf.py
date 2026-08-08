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

Three things live here, and each of them had begun to exist twice before it did:

- :func:`make_workspace_context` — how a host says who is calling and which files they own.
- :func:`sandboxed_tool` — the shape every sandbox workload's tool has: attach nothing when
  no backend is configured, key the sandbox from the host's request context rather than from
  model input, and turn a provider failure into a sanitized sentence the model may see plus
  a detailed line only the log gets.
- :class:`~maf_sandbox.SandboxPurger`, re-exported — a host wiring a MAF surface needs the
  thread-delete participant at the same moment it needs the two above.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from ._error_detail import error_detail
from ._protocol import Sandbox, SandboxKey, SandboxSpec, WorkspaceContext
from ._purger import SandboxPurger
from ._router import NoSandboxBackend, SandboxRouter

#: Fallback for :func:`sandboxed_tool`'s ``logger`` argument. Named apart from the usual
#: module-level ``logger`` because that argument is the whole point: a workload passes its
#: own logger so the failure ladder's records keep the workload's logger name, and only a
#: caller that does not care lands here.
_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = [
    "SandboxPurger",
    "SandboxToolSession",
    "make_workspace_context",
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


def make_workspace_context(
    store_walker: Callable[[Any], Awaitable[list[str]]],
    scope_getter: Callable[[], str],
    thread_getter: Callable[[], str | None],
) -> WorkspaceContext:
    """Build the :class:`~maf_sandbox.WorkspaceContext` a host hands to a workload factory.

    Args:
        store_walker: Given the workspace store, returns the paths the caller may act on.
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
    return WorkspaceContext(
        current_scope=scope_getter,
        current_thread_id=thread_getter,
        list_files=store_walker,
    )


def sandbox_tool_declarations(
    spec: SandboxSpec,
    *,
    source_integrity: str | None = "trusted",
    egress_max_confidentiality: str | None = None,
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

    ``egress_max_confidentiality`` is **opt-in, and off by default**, and the asymmetry is
    deliberate.  A confidentiality key is not inert metadata: writing one participates in a
    policy leg that may be dormant in the host — a host whose tools never label anything
    above its own cap has a confidentiality check that cannot currently fire — so declaring
    it can change which calls are gated or refused.  That is the host's decision to make with
    its own classification in hand, never a default a library picks.  When it *is* passed,
    the key is written only if the spec actually permits egress (``egress_allow`` non-empty):
    a sandbox with no network cannot carry anything out of the conversation, so capping it
    would gate calls for a flow that does not exist.

    Args:
        spec: The sandbox this workload asks for; ``egress_allow`` is what is read.
        source_integrity: Integrity tier for this tool's results, or ``None`` to declare
            none.
        egress_max_confidentiality: The host's cap for outbound tools, in the host's own
            vocabulary, or ``None`` (the default) to declare none.
    """
    declarations: dict[str, Any] = {}
    if source_integrity is not None:
        declarations["source_integrity"] = source_integrity
    if egress_max_confidentiality is not None and spec.egress_allow:
        declarations["max_allowed_confidentiality"] = egress_max_confidentiality
    return declarations


class SandboxToolSession:
    """Everything a sandbox workload's tool body needs, minus anything the model supplies.

    Handed to the ``build`` callback by :func:`sandboxed_tool`, and the reason that callback
    exists: the questions every sandbox tool has to answer the same way — where the sandbox
    key comes from, which file names may be interpolated into a command, and what a provider
    failure is allowed to tell the model — are answered here once instead of per workload.

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
        context: WorkspaceContext,
        agent_dir: str,
        spec: SandboxSpec,
        *,
        name: str,
        logger: logging.Logger,
    ) -> None:
        self._router = router
        self._context = context
        self._agent_dir = agent_dir
        self._spec = spec
        self._name = name
        self._logger = logger
        # The tool's name prefixes every log line this class writes, and it is baked into the
        # FORMAT string rather than passed as an argument so the emitted record is
        # indistinguishable from one the workload wrote by hand — `record.msg` included,
        # which is what a structured exporter reads and what a caplog assertion matches. A
        # `%` in a name would then read as a format specifier, so it is escaped once, here.
        self._log_prefix = name.replace("%", "%%")

    @property
    def spec(self) -> SandboxSpec:
        """The sandbox this workload asks for. Workloads read ``work_dir`` off it."""
        return self._spec

    @property
    def name(self) -> str:
        """The tool's name, as the model sees it."""
        return self._name

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

    async def list_files(self, store: Any) -> list[str] | str:
        """The paths this caller may act on, or the message to return if they cannot be read.

        The listing is a workload's injection-pinning boundary: only a name present in it is
        ever substituted into a command, so a name the model invented — or one it read out of
        a poisoned file — has nowhere to go.  Which makes a failure to enumerate a refusal
        rather than an empty listing: an empty list would look like "the workspace has no
        files" and refuse each name individually with the wrong reason.

        The store is passed per call rather than held: which store a workload reads is the
        workload's business, and some read more than one.
        """
        try:
            return await self._context.list_files(store)
        except Exception as exc:  # noqa: BLE001
            return f"Error: could not list workspace files: {exc}"

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
            return await self._router.acquire(key, self._spec)
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


def sandboxed_tool(
    build: Callable[[SandboxToolSession], Callable[..., Awaitable[str]]],
    *,
    router: SandboxRouter | None,
    context: WorkspaceContext,
    agent_dir: str,
    spec: SandboxSpec,
    name: str,
    approval_mode: Literal["always_require", "never_require"] = "never_require",
    declarations: Mapping[str, Any] | None = None,
    egress_max_confidentiality: str | None = None,
    logger: logging.Logger | None = None,
) -> list[Any]:
    """Return the one-tool list for a sandbox workload, or ``[]`` when no sandbox is available.

    This is the shape a sandbox workload's factory has.  Four things about it are decisions
    rather than plumbing, and a workload that re-derives them tends to get one of them wrong:

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
            workspace (see :func:`make_workspace_context`).
        agent_dir: The agent's directory name. Baked into the sandbox key here, at factory
            time, rather than taken from the model at call time.
        spec: The sandbox this workload asks for.
        name: The tool's name, as declared to the model.
        approval_mode: MAF's per-tool approval setting.
        declarations: ``additional_properties`` to write verbatim, for a workload that wants
            full control. Defaults to :func:`sandbox_tool_declarations` over ``spec``.
        egress_max_confidentiality: Passed to :func:`sandbox_tool_declarations`; ignored when
            ``declarations`` is given. Read that function before setting it — it is off by
            default for a reason.
        logger: Where the failure ladder writes its detail. Defaults to this module's logger;
            pass the workload's own so its records keep the workload's logger name.
    """
    if router is None or not router.enabled:
        return []
    router.ensure_can_serve(spec)

    session = SandboxToolSession(
        router,
        context,
        agent_dir,
        spec,
        name=name,
        logger=logger if logger is not None else _DEFAULT_LOGGER,
    )
    properties = (
        dict(declarations)
        if declarations is not None
        else sandbox_tool_declarations(spec, egress_max_confidentiality=egress_max_confidentiality)
    )

    # Imported here rather than at module scope so that merely importing this module — which
    # a host may do to reach `make_workspace_context` alone — does not pull the framework in.
    from agent_framework import tool

    decorate = tool(
        name=name,
        approval_mode=approval_mode,
        additional_properties=properties,
    )
    return [decorate(build(session))]
