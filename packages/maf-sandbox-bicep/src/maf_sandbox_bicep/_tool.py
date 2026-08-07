"""``bicep_validate``: the first sandbox workload.

Runs ``bicep build`` and ``bicep lint`` on the files an agent authored, inside a sandbox,
and returns the compiler's diagnostics — T2 (compiler truth) instead of T0 (the model
checking its own work).

**This module contains no Azure import and no sandbox lifecycle code.**  It talks to a
:class:`~maf_sandbox.SandboxRouter` and gets back something with ``write_file`` and
``exec``, so the same tool runs unchanged against ACA Sandboxes, a local Docker container or
an in-process fake — the acceptance criterion this split was built to satisfy.  What is
Bicep-specific — the command templates, the accepted extensions, the SARIF parsing, the one
host Bicep is allowed to reach — lives here and only here.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from maf_sandbox import SandboxRouter, SandboxSpec, WorkspaceContext, error_detail
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool

from ._paths import safe_workspace_path
from ._sarif import count_restore_failures, format_diagnostics, parse_sarif

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_framework import AgentFileStore

logger = logging.getLogger(__name__)

__all__ = [
    "BICEP_TOOL_NAMES",
    "BICEP_VALIDATE_TOOL_NAME",
    "bicep_sandbox_spec",
    "make_bicep_tools",
]

BICEP_VALIDATE_TOOL_NAME = "bicep_validate"

BICEP_TOOL_NAMES: frozenset[str] = frozenset({BICEP_VALIDATE_TOOL_NAME})

#: The sandbox kind this workload asks for.
BICEP_KIND = "bicep"

#: The hosts Bicep needs for AVM (`br/public:`) module restore — and no others.
#: Everything else — above all ARM, which a `ts:` reference would otherwise dial with the
#: host's credentials — is denied by the spec's default.
#:
#: TWO hosts, not one.  MCR serves manifests from `mcr.microsoft.com` but layer blobs from
#: the regional data endpoints under `*.data.mcr.microsoft.com` (the same split every
#: Microsoft egress-allowlist doc calls out).  With only the first host allowed, restore
#: resolves the manifest and then 403s on the blob — BCP192 on every `br/public:` reference
#: — so module types never load and type errors in module inputs are structurally invisible.
#: That exact blind spot once let a reviewer PASS Bicep that opens with compile errors in
#: VS Code: it discounted the restore noise and certified the module inputs from READMEs
#: instead.  Both hosts are Microsoft-operated artifact CDNs; the containment posture (no
#: ARM, no ambient identity) is unchanged.
_MCR_HOST = "mcr.microsoft.com"
_MCR_DATA_HOST = "*.data.mcr.microsoft.com"

#: Root for everything shared with the sandbox: `bicepconfig.json` at the top, and one
#: subdirectory per validation beneath it.
#:
#: A dedicated path rather than `/tmp/work` or the image's own tree.  `/tmp` is a plausible
#: mount point — anything that mounts a tmpfs over it would hide the config baked into the
#: image, and the symptom would be invisible (see below) — while a path nothing else owns
#: cannot be shadowed by accident and says plainly whose files these are.
#:
#: `bicepconfig.json` has to sit at this exact root.  Bicep resolves it ONLY by walking up
#: from the source file — verified against the pinned CLI, which has no `--config-file` flag
#: on either `build` or `lint` — so a config anywhere else is simply never found and the
#: linter silently falls back to its built-in defaults.  That failure looks completely
#: healthy: SARIF still parses, diagnostics still render, just against a weaker rule set than
#: the repo asked for.  `TestConfigDiscovery` pins the image against this constant, and the
#: sandbox CI checks the built image really contains the file, so neither can drift.
_WORK_DIR = "/acas/work"

# Fixed bicep command templates — no agent text interpolated.
# {path} is substituted only after the path is validated against the workspace listing.
# "|| true" ensures the exec exit code is always 0 (bicep exits non-zero on any diagnostic);
# diagnostics come from the SARIF blob.
# Note: `bicep build` emits SARIF on stderr; `2>&1` merges it into stdout so both legs read
# `.stdout` uniformly.  `bicep lint` emits SARIF on stdout natively.
_BUILD_CMD = "bicep build {path} --diagnostics-format sarif 2>&1 || true"

# `.bicepparam` is a parameter file, not a template, and `bicep build` refuses it outright:
#   The specified input "…/main.bicepparam" was not recognized as a Bicep file.
#   Bicep files must use the .bicep extension.
# That sentence is not SARIF, so the phase died in the parser and reported "could not parse
# SARIF output" for a file the tool advertises as supported — which is what it did against
# the live service. `build-params` is the counterpart command; like `build` it writes SARIF
# to stderr (hence the same `2>&1`), and `--outfile /dev/null` discards the compiled JSON
# because only the diagnostics are wanted. `bicep lint` accepts both kinds, so only the
# build half varies. All three behaviours were checked against the pinned CLI in the image.
_BUILD_PARAMS_CMD = (
    "bicep build-params {path} --diagnostics-format sarif --outfile /dev/null 2>&1 || true"
)

_LINT_CMD = "bicep lint {path} --diagnostics-format sarif || true"

_PARAM_SUFFIX = ".bicepparam"
_ACCEPTED_SUFFIXES = (".bicep", _PARAM_SUFFIX)


def _build_command_for(name: str) -> str:
    """The build-phase command that matches the file kind."""
    return _BUILD_PARAMS_CMD if name.endswith(_PARAM_SUFFIX) else _BUILD_CMD


def bicep_sandbox_spec(image: str | None = None, image_id: str | None = None) -> SandboxSpec:
    """The sandbox a Bicep validation needs, in backend-neutral terms.

    Only the image varies by deployment; the egress allowlist and work directory are
    properties of the workload and are fixed here rather than left to configuration — a
    deployment that could widen Bicep's egress could undo the containment the tool's whole
    design rests on.
    """
    return SandboxSpec(
        kind=BICEP_KIND,
        image=image,
        image_id=image_id,
        egress_allow=(_MCR_HOST, _MCR_DATA_HOST),
        work_dir=_WORK_DIR,
    )


def make_bicep_tools(
    router: SandboxRouter | None,
    workspace_store: "AgentFileStore",
    agent_dir: str,
    context: WorkspaceContext,
    *,
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: int = 120,
) -> list[Any]:
    """Return the ``[bicep_validate]`` tool list, or ``[]`` when no sandbox is available.

    The tool is **not attached** when ``router`` is ``None`` or has no backend — a host with
    nothing configured gets an empty list rather than a tool that fails at call time, so the
    agent keeps its unvalidated (T0) behaviour with no half-attached error path.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        workspace_store: The agent's workspace store; file content is read from here and
            written into the sandbox before the compiler runs.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread, and how to enumerate the
            workspace.
        image: OCI reference of the Bicep sandbox image.
        image_id: A backend-native disk-image id, skipping resolution.
        exec_timeout_seconds: Per-command bound. A sandbox that stops answering must not
            hold the caller's turn open.
    """
    return sandboxed_tool(
        lambda session: _bicep_validate_tool(session, workspace_store, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=bicep_sandbox_spec(image, image_id),
        name=BICEP_VALIDATE_TOOL_NAME,
        approval_mode="never_require",
        # No `declarations=` and no `egress_max_confidentiality=`: the default derivation is
        # exactly `{"source_integrity": "trusted"}`, which is what this tool has always
        # declared. Trusted because the compiler's diagnostics are deterministic first-party
        # output from a sandbox with no ambient identity and only `mcr.microsoft.com`
        # reachable. No confidentiality key on purpose — a host's confidentiality tiers are
        # the host's classification, and declaring one here can activate a policy leg a given
        # host keeps dormant. `TestFidesDeclarations` pins the resulting dict.
        logger=logger,
    )


def _bicep_validate_tool(
    session: SandboxToolSession,
    store: "AgentFileStore",
    timeout: int,
) -> "Callable[..., Awaitable[str]]":
    """Build the ``bicep_validate`` body for one attached tool.

    Defined at module level rather than nested inside :func:`make_bicep_tools`, and that is
    not a style choice: the function below's **docstring is the tool's description** — MAF
    passes ``__doc__`` through verbatim, indentation and all — so nesting this one level
    deeper would re-indent every line of what the model reads at call time.
    """

    async def bicep_validate(
        files: list[str],
    ) -> str:
        """Run ``bicep build`` and ``bicep lint`` on Bicep files inside a sandboxed VM.

        Validates that the named files pass the Bicep compiler and linter under the repo
        ``bicepconfig.json`` (T2 — compiler truth rather than LLM self-check).  Call this
        after writing the files with ``file_access_write`` and before reporting them.

        **Pass every file of the set in ONE call, including modules in subfolders.**  The
        sandbox starts empty and receives only the files you list, so a ``module`` or a
        parameter file's ``using`` that points at a file you left out is reported as a
        missing file — a diagnostic about your call, not about your IaC.  For a typical
        template set that means all of them together::

            ["infra/main.bicep",
             "infra/main.bicepparam",
             "infra/modules/storage.bicep",
             "infra/modules/network.bicep"]

        Validating one file at a time is the common mistake here and produces
        ``BCP091 … could not find a part of the path`` for files that exist perfectly well
        in the workspace.

        Args:
            files: Workspace-relative paths to validate — the whole set that compiles
                together, not just the entry point.  Only ``.bicep`` and ``.bicepparam``
                extensions are accepted.

        Returns:
            A structured diagnostics report (build + lint output parsed from SARIF).
            Zero diagnostics means the files are T2-clean.  If the sandbox is unavailable
            the tool returns an error message so the run degrades to T0 rather than
            blocking.
        """
        # Scope and thread come from the host's request context — never from model input:
        # a model-supplied scope would let one conversation address another's sandbox.
        # `session.key()` is where that rule lives; it answers with the message to return
        # when no conversation is bound.
        key = session.key()
        if isinstance(key, str):
            return key

        for name in files:
            if not name.endswith(_ACCEPTED_SUFFIXES):
                return (
                    f"Error: bicep_validate only accepts .bicep and .bicepparam files; "
                    f"rejected: {name!r}"
                )

        # Enumerate the workspace so paths can be validated and content read.
        ws_files = await session.list_files(store)
        if isinstance(ws_files, str):
            return ws_files

        # Every call gets a fresh directory, because the sandbox is REUSED across fix rounds
        # and only the named files are written into it.
        #
        # Without this, a file deleted from the workspace between rounds survives in the
        # sandbox, and a template still referencing it *compiles* — the tool reports "no
        # diagnostics" for something that cannot build from the actual workspace. A false
        # green, from the one tool whose entire purpose is compiler truth.
        #
        # A fresh directory rather than wiping the old one: `bicepconfig.json` lives at the
        # work-dir root (the image COPYs it there), so a recursive delete would take the
        # repo's lint rules with it and every later `bicep lint` would quietly fall back to
        # defaults. Bicep finds that config by walking UP from the file, so a subdirectory
        # still picks it up — and the AVM module cache lives in ~/.bicep, untouched either
        # way. Staleness becomes impossible by construction instead of something to reconcile.
        round_dir = f"{session.spec.work_dir}/{uuid4().hex[:12]}"

        # Validate each name against that listing (the injection guard).
        validated: list[tuple[str, str]] = []  # (workspace_path, sandbox_path)
        for name in files:
            sandbox_path = safe_workspace_path(name, ws_files, round_dir)
            if sandbox_path is None:
                return (
                    f"Error: {name!r} is not in the workspace listing or contains "
                    f"unsafe characters — cannot validate"
                )
            validated.append((name, sandbox_path))

        # The four-branch degrade ladder — which failures may be named to the model and
        # which may only reach the log — is `session.acquire`'s, and it writes its detail
        # through this module's logger so those records keep this workload's logger name.
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox

        # Two passes, and the order is load-bearing: every file is written before ANY of them
        # is compiled.
        #
        # Bicep resolves `module … '…/db.bicep'` and a parameter file's `using '…'` off the
        # filesystem at compile time. Writing and compiling one file at a time means the first
        # file is built against a sandbox where its siblings do not exist yet, so a perfectly
        # good template reports "module not found" — a diagnostic that is an artefact of this
        # loop rather than anything wrong with the IaC. It only looks correct when the model
        # happens to list the files in dependency order, and for a `.bicepparam` (which always
        # references a template) it is wrong roughly half the time.
        #
        # Writes stay sequential rather than gathered: the ordering requirement is satisfied
        # either way, and a preview data plane has already produced one unexplained `Conflict`
        # burst — concurrency is not what to add on top of that without a reason.
        results: list[str] = []
        written: list[tuple[str, str]] = []
        for name, sandbox_path in validated:
            try:
                content = await store.read(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bicep_validate: could not read %r from workspace: %s", name, exc)
                results.append(f"Error: could not read {name!r} from workspace")
                continue
            if content is None:
                # A store read can miss without raising (the file was listed, then removed).
                # Writing `None` through would put the string "None" into the sandbox and
                # report a syntax error against a file the agent never wrote.
                logger.warning("bicep_validate: %r is listed but has no content", name)
                results.append(f"Error: {name!r} is listed in the workspace but has no content")
                continue

            try:
                await sandbox.write_file(sandbox_path, content)
            except Exception as exc:  # noqa: BLE001
                # Detail, not just str(): a live run produced `Operation returned an invalid
                # status 'Conflict'` for four files at once, and that sentence alone cannot
                # distinguish "the directory already exists" from "the sandbox is suspending"
                # — which is the difference between a bug and a retry.
                logger.warning(
                    "bicep_validate: could not write %r to sandbox: %s", name, error_detail(exc)
                )
                results.append(f"Error: could not write {name!r} to sandbox")
                continue
            written.append((name, sandbox_path))

        for name, sandbox_path in written:
            for phase, template in (
                ("build", _build_command_for(name)),
                ("lint", _LINT_CMD),
            ):
                results.append(
                    await _run_phase(
                        sandbox, phase, template, name, sandbox_path, round_dir, timeout
                    )
                )

        return "\n".join(results) if results else "No files validated."

    return bicep_validate


async def _run_phase(
    sandbox: Any,
    phase: str,
    template: str,
    name: str,
    sandbox_path: str,
    working_directory: str,
    timeout: int,
) -> str:
    """Run one compiler phase and render its SARIF, or an error line.

    Both phases behave identically, so they share this rather than being written twice —
    which is how the build leg's ``2>&1`` came to be missing from one of them once already.
    """
    started = perf_counter()
    try:
        result = await sandbox.exec(
            template.format(path=sandbox_path),
            working_directory=working_directory,
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("bicep_validate: %s exec timed out for %r after %ss", phase, name, timeout)
        return f"{phase}({name}): Error: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        logger.warning("bicep_validate: %s exec failed for %r: %s", phase, name, error_detail(exc))
        return f"{phase}({name}): Error: exec failed"
    elapsed_ms = int((perf_counter() - started) * 1000)

    diagnostics = parse_sarif(result.stdout or "")
    if diagnostics is None:
        logger.warning(
            "bicep_validate: could not parse SARIF for %r; raw: %.500r", name, result.stdout or ""
        )
        return f"{phase}({name}): Error: could not parse SARIF output"
    # The one record that says the compiler actually ran. Everything else about a healthy
    # call is silent: the tool's return value looks the same whether Bicep found nothing
    # wrong or never executed, and "0 diagnostics" is the answer in both cases.
    logger.info(
        "bicep_validate: %s ok file=%r diagnostics=%d elapsed_ms=%d",
        phase,
        name,
        len(diagnostics),
        elapsed_ms,
    )
    report = format_diagnostics(diagnostics, f"{phase}({name})", strip_prefix=working_directory)
    failed_restores = count_restore_failures(diagnostics)
    if failed_restores:
        # Without this banner a restore-failed run reads as an ordinary diagnostic list, and
        # an agent can (and once did) discount it as environment noise and certify the
        # module inputs from documentation instead of from the compiler.
        logger.warning(
            "bicep_validate: %s file=%r module restore FAILED for %d reference(s) — "
            "type checking of module inputs did not run",
            phase,
            name,
            failed_restores,
        )
        return (
            f"{phase}({name}): MODULE RESTORE FAILED for {failed_restores} module "
            "reference(s) (BCP190/BCP191/BCP192). Module types were NOT loaded, so type "
            "checking of module inputs DID NOT RUN — this validation is INCOMPLETE. Treat "
            "it as a broken validation run, not as evidence the files are healthy: do not "
            "report the files as clean, and a reviewer must not base a PASS on it.\n"
            f"{report}"
        )
    return report
