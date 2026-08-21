"""Sandbox router: one seam between a host application and any sandbox provider.

```
app  ->  SandboxRouter  ->  backend  ->  the sandbox
```

A workload asks for a sandbox and runs a command in it.  A backend decides what actually
boots — an ACA Sandbox (`maf-sandbox-acas`) today, a local Docker container or an
in-process fake later.  Neither knows about the other, which is what lets the same tool run
against all of them unchanged.

The router exists for five things a backend cannot own:

- **Which backend serves a request.** Configuration, not an import, decides.
- **The minimum-isolation floor.** A backend declaring a rung below the one the host accepts
  — :data:`~maf_sandbox.Isolation.MICROVM` unless the host says otherwise — is refused
  outright, as is a rung this package does not recognise; see
  :class:`~maf_sandbox._router.SandboxBackendNotPermitted`.  Enforced at construction and
  pinned by tests.
- **The capability match.** A backend that cannot do what a workload's spec requires is
  refused when that workload attaches its tool — see
  :class:`~maf_sandbox._router.SandboxCapabilityNotSupported`.
- **The transfer ceilings.** A spec declaring caps above what the backend allows in either
  direction is refused at the same moment — see
  :class:`~maf_sandbox._router.SandboxTransferLimitsNotPermitted`.
- **The egress rule.** A backend that cannot confine a sandbox to the hosts a workload's spec
  names is refused at the same moment — see
  :meth:`~maf_sandbox.SandboxRouter.ensure_can_serve`.

The floor and the egress rule are security properties rather than conveniences, and they
answer to different owners: how strong a boundary must be *here* is the host's policy, read
from ``min_isolation``, while what a sandbox may reach is a property of the *workload*, stated
in its spec.  Keeping the two axes apart is deliberate — merged into one required-capabilities
list, a workload could ask for a weaker boundary than the deployment mandates.  A spec may
raise the host's floor for itself, and never lower it.

Alongside the router it owns the sink half of ``FILES_OUT``:
:func:`~maf_sandbox.collect_outputs` pulls a spec's declared outputs, caps them, and hands each
one to a host-supplied :class:`~maf_sandbox.OutputSink`.  The host does the writing — a
file store, a blob container or a UI panel is a property of the application, and this
package writes nothing anywhere.

This package imports no backend and no host application.

One module sits outside that claim: :mod:`maf_sandbox.maf`, the MAF glue
(``make_caller_context``, ``sandboxed_tool``, and the purge participant).  It is the only
module allowed to import ``agent_framework``, and keeping it off this ``__init__`` is what
lets ``import maf_sandbox`` stay cheap and framework-free for a backend or a test that only
speaks the protocol.

Three more are stdlib-only but still reached by name rather than re-exported, each because
reaching for it is a foreseeable mistake: :mod:`maf_sandbox.testing` in production code,
:mod:`maf_sandbox.paths` against a *host* filesystem path, and
:mod:`maf_sandbox.conformance` anywhere but a backend's own test suite.  The rule is in
``docs/design/sandbox-architecture.md``, under "Where shared code lives".
"""

from __future__ import annotations

from ._error_detail import error_detail
from ._host_tools import (
    DEFAULT_MAX_DISPATCHES_PER_RUN,
    FLOW_DECLARED_KEY,
    DispatchResult,
    HostToolAggregate,
    HostToolDeclaration,
    HostToolNotDeclared,
    HostToolRegistry,
    HostToolRun,
    MafSandboxHostToolsWarning,
    declaration_of,
    sandbox_tool,
)
from ._host_tools_over_exec import (
    CALLS_DIRECTORY,
    SHIM_MODULE,
    WORK_DIRECTORY,
    GuestRunLayout,
    SandboxProgramTimeout,
    SignalOutcome,
    SignalReach,
    dispatch_over_exec,
    guest_run_layout,
    host_tool_shim,
    launcher_script,
    reclaim_run,
)
from ._outputs import (
    MAX_ARTIFACT_NAME_BYTES,
    Artifact,
    LandedArtifact,
    NameNormalization,
    OutputSink,
    SandboxArtifactNameCollision,
    SandboxArtifactNameInvalid,
    SandboxLandingNotConfined,
    SandboxOutputError,
    SandboxOutputMissing,
    SandboxOutputNotConfined,
    SandboxOutputNotRegular,
    SandboxOutputSinkRequired,
    SandboxOutputSizeUnknown,
    SandboxOutputUnreachable,
    SandboxTransferCapExceeded,
    collect_outputs,
    landing_outputs,
    make_file_system_sink,
    missing_sink_refusal,
    portable_name,
    spec_lands_artifacts,
    validate_artifact_name,
)
from ._protocol import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    DEFAULT_TRANSFER_LIMITS,
    INTEGRITY_RANK,
    ISOLATION_RANK,
    CallerContext,
    Capability,
    DeclaredOutput,
    Egress,
    EntryKind,
    ExecResult,
    Identity,
    Isolation,
    OsFamily,
    OutputDisposition,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxQueuedTimeout,
    SandboxSpec,
    SourceIntegrity,
    TransferLimits,
    meets_floor,
)
from ._purger import SandboxPurger
from ._reclaim import ReclaimFailure
from ._router import (
    NoSandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityDenied,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxIdentityDenied,
    SandboxOsFamilyNotSupported,
    SandboxRouter,
    SandboxTransferLimitsNotPermitted,
    ScopeDisposal,
)

__all__ = [
    "CALLS_DIRECTORY",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_MAX_DISPATCHES_PER_RUN",
    "DEFAULT_SANDBOX_LIMITS",
    "DEFAULT_TRANSFER_LIMITS",
    "FLOW_DECLARED_KEY",
    "INTEGRITY_RANK",
    "ISOLATION_RANK",
    "MAX_ARTIFACT_NAME_BYTES",
    "SHIM_MODULE",
    "WORK_DIRECTORY",
    "Artifact",
    "Capability",
    "DeclaredOutput",
    "DispatchResult",
    "Egress",
    "EntryKind",
    "ExecResult",
    "GuestRunLayout",
    "HostToolAggregate",
    "HostToolDeclaration",
    "HostToolNotDeclared",
    "HostToolRegistry",
    "HostToolRun",
    "Identity",
    "Isolation",
    "OsFamily",
    "LandedArtifact",
    "MafSandboxExperimentalWarning",
    "MafSandboxHostToolsWarning",
    "NameNormalization",
    "NoSandboxBackend",
    "OutputDisposition",
    "OutputSink",
    "ReclaimFailure",
    "Sandbox",
    "SandboxArtifactNameCollision",
    "SandboxArtifactNameInvalid",
    "SandboxBackend",
    "SandboxBackendNotPermitted",
    "SandboxCapabilityDenied",
    "SandboxCapabilityNotSupported",
    "SandboxEgressNotEnforced",
    "SandboxOsFamilyNotSupported",
    "SandboxEntry",
    "SandboxIdentityDenied",
    "SandboxKey",
    "SandboxLandingNotConfined",
    "SandboxLimits",
    "SandboxOutputError",
    "SandboxOutputMissing",
    "SandboxOutputNotConfined",
    "SandboxOutputNotRegular",
    "SandboxOutputSinkRequired",
    "SandboxOutputSizeUnknown",
    "SandboxOutputUnreachable",
    "SandboxProgramTimeout",
    "SandboxQueuedTimeout",
    "SignalOutcome",
    "SignalReach",
    "SandboxPurger",
    "SandboxRouter",
    "SandboxSpec",
    "SandboxTransferCapExceeded",
    "SandboxTransferLimitsNotPermitted",
    "ScopeDisposal",
    "SourceIntegrity",
    "TransferLimits",
    "CallerContext",
    "collect_outputs",
    "declaration_of",
    "dispatch_over_exec",
    "error_detail",
    "guest_run_layout",
    "host_tool_shim",
    "launcher_script",
    "landing_outputs",
    "make_file_system_sink",
    "meets_floor",
    "missing_sink_refusal",
    "portable_name",
    "reclaim_run",
    "sandbox_tool",
    "spec_lands_artifacts",
    "validate_artifact_name",
]

# --- Experimental-package notice ---------------------------------------------------------
# This package is early-stage (0.1.0, "Development Status :: 4 - Beta"). Mirrors
# `agent_framework`'s own experimental-feature idiom (see its `_feature_stage` module and
# `ExperimentalWarning`, a `FutureWarning` subclass) but deliberately subclasses
# `UserWarning` instead: a host that runs under `python -W error` (many CI/production
# launchers do) would have importing this package alone raise before any of its own code
# runs if the category were a `FutureWarning`. `UserWarning` keeps the notice
# informational-by-default while staying a real, catchable, filterwarnings-suppressible
# category — see the try/except immediately below for how `-W error` is handled anyway.
#
# Duplicated (not imported from a shared module) in each of the three maf-sandbox*
# packages on purpose — a shared warnings module would be a cross-package dependency this
# split is designed to avoid.
import warnings as _warnings


class MafSandboxExperimentalWarning(UserWarning):
    """Warning category for maf-sandbox's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_sandbox is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafSandboxExperimentalWarning, stacklevel=2)
    except MafSandboxExperimentalWarning:
        # A host running under `python -W error` (or with a blanket
        # `filterwarnings("error")` active) turns the warning above into an exception at
        # the call site. Importing a package must never fail because of an informational
        # notice, so it is swallowed here — this is the one piece of state a `-W error`
        # host is allowed to change: whether the notice was printed, never whether the
        # import succeeded.
        pass


_warn_experimental()
