"""``bicep_validate``: the first sandbox workload.

Runs ``bicep build`` and ``bicep lint`` on the files an agent authored, inside a sandbox,
and returns the compiler's diagnostics — T2 (compiler truth) instead of T0 (the model
checking its own work).

**This module contains no Azure import and no sandbox lifecycle code.**  It talks to a
:class:`~maf_sandbox.SandboxRouter` and gets back something with ``write_file`` and
``exec``, so the same tool runs unchanged against ACA Sandboxes, a local Docker container or
an in-process fake — the acceptance criterion this split was built to satisfy.  What is
Bicep-specific — the command templates, the accepted extensions, the SARIF parsing, the
hosts Bicep is allowed to reach — lives here and only here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from time import perf_counter
from typing import TYPE_CHECKING, Any

from maf_sandbox import (
    CallerContext,
    Egress,
    ListedFile,
    SandboxRouter,
    SandboxSpec,
    echoed_name,
    error_detail,
)
from maf_sandbox.maf import (
    SandboxToolSession,
    positions_holding_hidden_content,
    sandboxed_tool,
)

from ._paths import resolve_listed_path
from ._sarif import count_restore_failures, format_diagnostics, parse_sarif

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_framework import AgentFileStore

logger = logging.getLogger(__name__)

#: Capped so a large file store cannot flood the model's context.
_LISTING_HINT_MAX = 20


def _listing_hint(name: str, listing: list[str]) -> str:
    """The listing, or its near misses — what resolves a typo without another round trip."""
    if not listing:
        return "This tool's listing is empty — no files were shared with it."
    near = [f for f in listing if f.rsplit("/", 1)[-1] == name.rsplit("/", 1)[-1]]
    if near and near != [name]:
        return f"Did you mean: {', '.join(sorted(near)[:_LISTING_HINT_MAX])}?"
    shown = sorted(listing)[:_LISTING_HINT_MAX]
    more = f" (+{len(listing) - len(shown)} more)" if len(listing) > len(shown) else ""
    return f"Files visible here: {', '.join(shown)}{more}."


__all__ = [
    "BICEP_TOOL_NAMES",
    "BICEP_VALIDATE_TOOL_NAME",
    "bicep_sandbox_spec",
    "make_bicep_tools",
]

#: The argument the model names its files in, which is the parameter a refusal points at
#: and the one provenance is asked about.
_FILES_ARGUMENT = "files"

BICEP_VALIDATE_TOOL_NAME = "bicep_validate"

BICEP_TOOL_NAMES: frozenset[str] = frozenset({BICEP_VALIDATE_TOOL_NAME})

#: The sandbox kind this workload asks for.
BICEP_KIND = "bicep"

#: The hosts Bicep needs for AVM (`br/public:`) module restore — and no others.
#: Everything else — above all ARM, which a `ts:` reference would otherwise dial with the
#: host's credentials — is denied by the spec's default.
#:
#: FOUR hosts, in two pairs — a restore reads both the artifacts and the catalogue, and
#: each of those is served from two names.
#:
#: The artifacts.  MCR serves manifests from `mcr.microsoft.com` but layer blobs from the
#: regional data endpoints under `*.data.mcr.microsoft.com` (the same split every Microsoft
#: egress-allowlist doc calls out).  With only the first host allowed, restore resolves the
#: manifest and then 403s on the blob — BCP192 on every `br/public:` reference — so module
#: types never load and type errors in module inputs are structurally invisible.  That exact
#: blind spot once let a reviewer PASS Bicep that opens with compile errors in VS Code: it
#: discounted the restore noise and certified the module inputs from READMEs instead.
#:
#: The catalogue.  Restore also pulls the public module *index* — which modules exist, at
#: which versions — from `https://aka.ms/br-module-index-data`, the endpoint hard-coded in
#: the CLI's `PublicModuleMetadataHttpClient`, which redirects to
#: `live-data.bicep.azure.com/module-index`.  Both hops have to be allowed: the redirector
#: on its own answers with a `Location` header pointing at a host that is still denied.
#: The fetch belongs to `OciArtifactRegistry.OnRestoreArtifacts`, not to the analyzer —
#: deliberately, so that lint rules never download during analysis — so it is attempted on
#: every `bicep build` and every `bicep lint` regardless of which rules are enabled, and the
#: only switch that stops it, `--no-restore`, is the one that would cost us module types.
#: Blocked, the compiler does not go quiet: `use-recent-module-versions` reports "Could not
#: download available module versions" once per file, a warning that reads like a finding
#: about the source while the check it stands for — outdated `br/public:avm/...` pins —
#: never runs.
#:
#: All four are Microsoft-operated; the containment posture (no ARM, no ambient identity,
#: nothing reachable that could carry the host's credentials) is unchanged.
_MCR_HOST = "mcr.microsoft.com"
_MCR_DATA_HOST = "*.data.mcr.microsoft.com"
_MODULE_INDEX_REDIRECT_HOST = "aka.ms"
_MODULE_INDEX_HOST = "live-data.bicep.azure.com"

#: The AVM module-registry hosts, and the only hosts Bicep ever names. They are the payload of
#: an `ALLOWLIST` run and are fixed here: a deployment chooses the *mode* Bicep runs in, but
#: never the hosts, so it can lower to `CLOSED` or (on a backend that cannot confine) raise to
#: `UNRESTRICTED`, but cannot widen the allowlist to somewhere Bicep has no business reaching.
_MODULE_HOSTS = (_MCR_HOST, _MCR_DATA_HOST, _MODULE_INDEX_REDIRECT_HOST, _MODULE_INDEX_HOST)

#: The postures Bicep can run in. `ALLOWLIST` restores AVM modules from `_MODULE_HOSTS`;
#: `CLOSED` skips the restore and reports the shortfall if a template needed one; `UNRESTRICTED`
#: is for a backend that cannot confine at all (a fixed compiler, not model-written code, so the
#: open posture is a dev convenience rather than an exfiltration surface).
_EGRESS_MODES = frozenset({Egress.UNRESTRICTED, Egress.ALLOWLIST, Egress.CLOSED})

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
_WORK_DIR = "/maf-sandbox/work"

# Fixed bicep command templates — no agent text interpolated.
# {path} is substituted only after the path is validated against the file store listing.
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


def bicep_sandbox_spec(
    image: str | None = None,
    image_id: str | None = None,
    *,
    egress: Egress = Egress.ALLOWLIST,
) -> SandboxSpec:
    """The sandbox a Bicep validation needs, in backend-neutral terms.

    ``egress`` is the network posture the validation runs in, defaulting to
    :data:`~maf_sandbox.Egress.ALLOWLIST` — Bicep's designed posture, restoring AVM modules
    from the fixed :data:`_MODULE_HOSTS` and nothing else.  A deployment that will not use
    modules can lower it to :data:`~maf_sandbox.Egress.CLOSED` (a template that then references
    one fails at runtime and the validation reports the shortfall); one running on a backend
    that cannot confine at all — a no-isolation dev backend — raises it to
    :data:`~maf_sandbox.Egress.UNRESTRICTED`.  Only the *mode* is a deployment's to choose: the
    allowlist hosts are fixed, so egress can be lowered, raised, or left, but never widened to
    somewhere Bicep has no business reaching.  The router serves the chosen mode only on a
    backend that enforces it and refuses otherwise (:class:`~maf_sandbox.SandboxEgressNotEnforced`).
    Only the image otherwise varies by deployment.
    """
    if egress not in _EGRESS_MODES:
        allowed = ", ".join(sorted(_EGRESS_MODES))
        raise ValueError(
            f"bicep runs in one of {{{allowed}}}, not {str(egress)!r}: it validates offline "
            "or against the AVM registry, and nothing it does needs another posture."
        )
    return SandboxSpec(
        kind=BICEP_KIND,
        image=image,
        image_id=image_id,
        egress=egress,
        egress_allow=_MODULE_HOSTS if egress is Egress.ALLOWLIST else (),
        work_dir=_WORK_DIR,
    )


def make_bicep_tools(
    router: SandboxRouter | None,
    file_store: AgentFileStore,
    agent_dir: str,
    context: CallerContext,
    *,
    image: str | None = None,
    image_id: str | None = None,
    egress: Egress = Egress.ALLOWLIST,
    exec_timeout_seconds: int = 120,
) -> list[Any]:
    """Return the ``[bicep_validate]`` tool list, or ``[]`` when no sandbox is available.

    The tool is **not attached** when ``router`` is ``None`` or has no backend — a host with
    nothing configured gets an empty list rather than a tool that fails at call time, so the
    agent keeps its unvalidated (T0) behaviour with no half-attached error path.

    ``egress`` is the posture the validation runs in — see :func:`bicep_sandbox_spec`. A
    backend that cannot enforce the chosen mode raises
    :class:`~maf_sandbox.SandboxEgressNotEnforced` at attach: refused, never quietly served
    behind a different boundary. A deployment wiring a backend that can only run fully closed
    passes ``egress=Egress.CLOSED``; a module restore then does not happen and a template that
    needed one reports the shortfall at runtime rather than hiding it.

    Args:
        router: The sandbox router, or ``None`` when sandboxing is not configured.
        file_store: The agent's file store; file content is read from here and
            written into the sandbox before the compiler runs.
        agent_dir: The agent's directory name. Baked into the sandbox key at factory time
            rather than taken from the model at call time.
        context: How to read the caller's scope and thread, and how to enumerate the
            file store.
        image: OCI reference of the Bicep sandbox image.
        image_id: A backend-native disk-image id, skipping resolution.
        egress: The network posture the validation runs in; see :func:`bicep_sandbox_spec`.
            Defaults to :data:`~maf_sandbox.Egress.ALLOWLIST` (the AVM registry hosts).
        exec_timeout_seconds: Per-command bound. A sandbox that stops answering must not
            hold the caller's turn open.
    """
    return sandboxed_tool(
        lambda session: _bicep_validate_tool(session, file_store, exec_timeout_seconds),
        router=router,
        context=context,
        agent_dir=agent_dir,
        spec=bicep_sandbox_spec(image, image_id, egress=egress),
        name=BICEP_VALIDATE_TOOL_NAME,
        approval_mode="never_require",
        # The compiler is first-party, and what it is deterministic *about* is a template the
        # model wrote: a diagnostic quotes the identifiers of the source, and every location
        # names a file the caller passed. So the result does not derive from wholly trusted
        # input and cannot claim to. Declared, a label would replace the framework's
        # input-label join rather than floor it.
        source_integrity=None,
        # No confidentiality key on purpose — a host's confidentiality tiers are the host's
        # classification, and declaring one here can activate a policy leg a given host keeps
        # dormant. `TestFidesDeclarations` pins the resulting dict.
        logger=logger,
    )


def _bicep_validate_tool(
    session: SandboxToolSession,
    store: AgentFileStore,
    timeout: int,
) -> Callable[..., Awaitable[str]]:
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
        in the file store.

        Args:
            files: Store-relative paths to validate — the whole set that compiles
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

        # Asked once for the whole list: the middleware may have rewritten a variable
        # reference into any of these, and its answer is what a refusal renders instead of the
        # value. One pass over the variable store, before either loop that can refuse.
        rewritten = positions_holding_hidden_content(files, argument=_FILES_ARGUMENT)

        for position, name in enumerate(files):
            if not name.endswith(_ACCEPTED_SUFFIXES):
                named = echoed_name(name, at=f"files[{position}]", hidden=position in rewritten)
                return (
                    f"Error: bicep_validate only accepts .bicep and .bicepparam files; "
                    f"rejected: {named}"
                )

        # Enumerate the file store so paths can be validated and content read.
        listing = await session.list_files(store)
        if isinstance(listing, str):
            return listing
        # Names for the path checks below, which are about spelling; the entries themselves are
        # kept so the read that follows carries each file's label rather than losing it.
        listed_names = [entry.name for entry in listing]
        listed_by_name = {entry.name: entry for entry in listing}

        # Every call gets a fresh directory, because the sandbox is REUSED across fix rounds
        # and only the named files are written into it.
        #
        # Without this, a file deleted from the file store between rounds survives in the
        # sandbox, and a template still referencing it *compiles* — the tool reports "no
        # diagnostics" for something that cannot build from the actual file store. A false
        # green, from the one tool whose entire purpose is compiler truth.
        #
        # A fresh directory rather than wiping the old one: `bicepconfig.json` lives at the
        # work-dir root (the image COPYs it there), so a recursive delete would take the
        # repo's lint rules with it and every later `bicep lint` would quietly fall back to
        # defaults. Bicep finds that config by walking UP from the file, so a subdirectory
        # still picks it up — and the AVM module cache lives in ~/.bicep, untouched either
        # way. Staleness becomes impossible by construction instead of something to reconcile.
        call_directory = session.guest_call_path()

        # Validate each name against that listing (the injection guard).
        validated: list[tuple[ListedFile, str, int]] = []  # (listed, sandbox_path, position)
        for position, name in enumerate(files):
            sandbox_path, listing_key, rejection = resolve_listed_path(
                name, listed_names, call_directory
            )
            named = echoed_name(name, at=f"files[{position}]", hidden=position in rewritten)
            if rejection == "unsafe":
                # No listing echoed back: that would invite a retry with another spelling.
                return (
                    f"Error: {named} cannot be validated — file names may contain only "
                    f"[A-Za-z0-9._/-] and no '..' segments."
                )
            if rejection == "missing" or sandbox_path is None:
                # Logged so hosts can count listing misses.
                logger.warning(
                    "bicep_validate: %r is not in this tool's file store listing (%d file(s) "
                    "visible) — the store wired here may be narrower than the agent's",
                    name,
                    len(listed_names),
                )
                return (
                    f"Error: {named} is not in this tool's file listing, so it was not "
                    f"validated. This listing can be narrower than the files you can read "
                    f"elsewhere. {_listing_hint(name, listed_names)}"
                )
            # The listing's key, not the caller's spelling: "./main.bicep" validates but
            # would not read back from a store keyed "main.bicep".
            validated.append((listed_by_name[listing_key or name], sandbox_path, position))

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
        # Hidden-ness is a property of the *file*, not of the position that asked for it, and
        # what is remembered is the position that was actually expanded. Two spellings can
        # resolve to one destination — `["x.bicep", "./x.bicep"]`, which nothing refuses — and
        # if only one was expanded, rendering the other at its *own* position would attribute
        # the file to an argument the framework never rewrote. One destination, one rendering,
        # naming the position whose value really is hidden content.
        hidden_at: dict[str, int] = {}
        for _listed, sandbox_path, position in validated:
            if position in rewritten:
                hidden_at.setdefault(sandbox_path, position)

        results: list[str] = []
        # `(store path, the spelling that may be shown, sandbox path)`. The first two differ
        # where the framework expanded hidden content into the name; matching the listing does
        # not make such a name safe to render.
        written: list[tuple[str, str, str]] = []
        for listed, sandbox_path, position in validated:
            name = listed.name
            expanded = hidden_at.get(sandbox_path)
            hidden = expanded is not None
            # The value at the expanded position, not this entry's listing key: `echoed_name`
            # reports the length of what it is standing in for, and the two spellings differ.
            at = f"files[{expanded if hidden else position}]"
            named = echoed_name(files[expanded] if hidden else name, at=at, hidden=hidden)
            item = await session.read_file(store, listed, at=at, hidden=hidden, named=named)
            if isinstance(item, str):
                # The session logged the detail; this is the sentence the model may see.
                results.append(item)
                continue
            if item is None:
                # A store read can miss without raising (the file was listed, then removed).
                # Writing `None` through would put the string "None" into the sandbox and
                # report a syntax error against a file the agent never wrote.
                logger.warning("bicep_validate: a listed file has no content")
                results.append(f"Error: {named} is listed in the file store but has no content")
                continue
            content = item.text
            if content is None:
                logger.warning("bicep_validate: a listed file read back with no text")
                results.append(f"Error: {named} is listed in the file store but has no content")
                continue

            try:
                await sandbox.write_file(sandbox_path, content, working_directory=call_directory)
            except Exception as exc:  # noqa: BLE001
                # Detail, not just str(): a live run produced `Operation returned an invalid
                # status 'Conflict'` for four files at once, and that sentence alone cannot
                # distinguish "the directory already exists" from "the sandbox is suspending"
                # — which is the difference between a bug and a retry.
                logger.warning(
                    "bicep_validate: could not write %r to sandbox: %s", name, error_detail(exc)
                )
                results.append(f"Error: could not write {named} to sandbox")
                continue
            written.append((name, name if not hidden else named, sandbox_path))

        # Built over every written file, not per phase: a diagnostic in one file can name
        # another, so the loop below has to be able to rename a location it did not write.
        #
        # Every file, including the ones whose name may be shown, mapped to itself. `_renamed`
        # identifies a location by *exact* membership and treats anything it can only match by
        # trailing component as unidentified, so the visible files have to be keys too — without
        # them a diagnostic about one would fall through to the ambiguous branch and lose its
        # name.
        #
        # Under both spellings, because they can differ: the listing key is what the store is
        # keyed by, while the compiler reports the path this call *wrote*, and
        # `resolve_listed_path` normalises between them (a listed `./main.bicep` is written as
        # `main.bicep`). Keying on the listing alone leaves the compiler's own spelling
        # unmatched, which is the spelling that reaches the model.
        renames: dict[str, str] = {}
        for entry_name, entry_label, entry_path in written:
            guest_relative = entry_path.removeprefix(call_directory).lstrip("/")
            # The absolute path as well: it is what Bicep was handed and what it reports back,
            # so it is the key that makes the match exact rather than a suffix guess.
            for key in (entry_name, guest_relative, entry_path):
                if key:
                    # Overwriting is safe because `hidden_paths` gives one destination one
                    # label: two entries reaching the same key agree on what it renders as.
                    renames[key] = entry_label

        for name, label, sandbox_path in written:
            for phase, template in (
                ("build", _build_command_for(name)),
                ("lint", _LINT_CMD),
            ):
                results.append(
                    await _run_phase(
                        sandbox,
                        phase,
                        template,
                        name,
                        label,
                        sandbox_path,
                        call_directory,
                        timeout,
                        renames=renames,
                    )
                )

        return "\n".join(results) if results else "No files validated."

    return bicep_validate


async def _run_phase(
    sandbox: Any,
    phase: str,
    template: str,
    name: str,
    label: str,
    sandbox_path: str,
    working_directory: str,
    timeout: int,
    renames: Mapping[str, str] | None = None,
) -> str:
    """Run one compiler phase and render its SARIF, or an error line.

    Both phases behave identically, so they share this rather than being written twice —
    which is how the build leg's ``2>&1`` came to be missing from one of them once already.

    ``name`` is the real store path and goes only to the host's own logs; ``label`` is what may
    appear in the result.  They differ where the framework expanded hidden content into the
    argument this file was named by, and every line this returns reaches the model — the
    successful ones included, since the phase prefix carries the name whatever the compiler
    found.  Passing one string for both would put the hidden value back into the conversation
    on the *happy* path, which is where it would be least likely to be noticed.

    ``renames`` carries the same distinction for every *other* file a diagnostic can point at.
    The phase prefix is not the only place a name reaches the model: stripping the working
    directory off a SARIF location leaves the file name, so a diagnostic reported against an
    expanded name renders it.
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
        return f"{phase}({label}): Error: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        logger.warning("bicep_validate: %s exec failed for %r: %s", phase, name, error_detail(exc))
        return f"{phase}({label}): Error: exec failed"
    elapsed_ms = int((perf_counter() - started) * 1000)

    diagnostics = parse_sarif(result.stdout or "")
    if diagnostics is None:
        logger.warning(
            "bicep_validate: could not parse SARIF for %r; raw: %.500r", name, result.stdout or ""
        )
        return f"{phase}({label}): Error: could not parse SARIF output"
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
    report = format_diagnostics(
        diagnostics, f"{phase}({label})", strip_prefix=working_directory, rename=renames
    )
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
            f"{phase}({label}): MODULE RESTORE FAILED for {failed_restores} module "
            "reference(s) (BCP190/BCP191/BCP192). Module types were NOT loaded, so type "
            "checking of module inputs DID NOT RUN — this validation is INCOMPLETE. Treat "
            "it as a broken validation run, not as evidence the files are healthy: do not "
            "report the files as clean, and a reviewer must not base a PASS on it.\n"
            f"{report}"
        )
    return report
