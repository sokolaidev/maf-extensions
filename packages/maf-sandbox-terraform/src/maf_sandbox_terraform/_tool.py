"""Validation and formatting through the shared sandbox session lifecycle."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from maf_sandbox import CallerContext, SandboxRouter, SourceIntegrity, error_detail
from maf_sandbox.maf import (
    SandboxResult,
    SandboxToolSession,
    positions_holding_hidden_content,
    sandboxed_tool,
)

from ._paths import resolve_manifest
from ._report import format_outcome, report_outcome
from ._spec import TerraformEngine, terraform_sandbox_spec

if TYPE_CHECKING:
    from agent_framework import AgentFileStore
    from maf_sandbox import FileStoreProvenance

logger = logging.getLogger(__name__)
#: Every answer validation may reach about the configuration it was given.
VALIDATE_VERDICTS = ("valid", "invalid")

#: Every answer formatting may reach: whether the formatter would change anything.
FORMAT_VERDICTS = ("changed", "unchanged")

STANDING_GUIDANCE = (
    "The other result item is derived from configuration and guest programs. Unread, incomplete, "
    "or failed validation is not a pass. Validation checks configuration and provider schemas; "
    "it does not establish deployment success or run plan, apply, or security policy checks. "
    "This tool does not rewrite files, return formatted text, or run other engine commands, so "
    "fix formatting by editing the files."
)
FORMAT_GUIDANCE = (
    "Formatted files are untrusted guest output. This tool does not write to the store or "
    "validate configuration. Write the returned whole files with the host's file tools, under "
    "its existing approvals. An incomplete result contains no files to write."
)


def make_terraform_tools(
    router: SandboxRouter | None,
    file_store: AgentFileStore,
    agent_id: str,
    context: CallerContext,
    *,
    engine: TerraformEngine = "terraform",
    formatting: bool = False,
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: float = 120,
    file_store_provenance: FileStoreProvenance | None = None,
) -> list[Any]:
    """Attach validation and, with formatting=True, a tool returning changed whole files.

    The host chooses the engine and compatible image; the model supplies only a file manifest
    and root module. The deadline covers all guest phases together. A cancellation waits for
    the bounded execution to finish before the core disposes the call's sandbox.
    """
    if (
        isinstance(exec_timeout_seconds, bool)
        or not math.isfinite(exec_timeout_seconds)
        or not 0 < exec_timeout_seconds <= 600
    ):
        raise ValueError("exec_timeout_seconds must be finite and in (0, 600]")
    tools = sandboxed_tool(
        lambda session: _build_tool(session, file_store, engine, exec_timeout_seconds),
        router=router,
        context=context,
        agent_id=agent_id,
        spec=terraform_sandbox_spec(image, image_id, engine=engine),
        name=f"{engine}_validate",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        verdicts=VALIDATE_VERDICTS,
        standing_guidance=(STANDING_GUIDANCE,),
        file_store_provenance=file_store_provenance,
        admission_timeout=max(30, exec_timeout_seconds),
        logger=logger,
    )
    if formatting:
        tools += sandboxed_tool(
            lambda session: _build_tool(session, file_store, engine, exec_timeout_seconds, True),
            router=router,
            context=context,
            agent_id=agent_id,
            spec=terraform_sandbox_spec(image, image_id, engine=engine),
            name=f"{engine}_format",
            source_integrity=SourceIntegrity.UNTRUSTED,
            result_contract=True,
            verdicts=FORMAT_VERDICTS,
            standing_guidance=(FORMAT_GUIDANCE,),
            file_store_provenance=file_store_provenance,
            admission_timeout=max(30, exec_timeout_seconds),
            logger=logger,
        )
    return tools


def _build_tool(
    session: SandboxToolSession,
    store: AgentFileStore,
    engine: TerraformEngine,
    timeout: float,
    formatting: bool = False,
) -> Callable[..., Awaitable[SandboxResult]]:
    operation = "Formatting" if formatting else "Validation"

    def _incomplete(sentence: str) -> SandboxResult:
        """A run that reached no verdict, and this module's own sentence saying why.

        Readable by the model: every caller below passes a sentence written here or by the
        session, naming an argument position rather than quoting what was at it.
        """
        return SandboxResult(completed=False, trusted_output=(sentence,))

    async def report(files: list[str], root_module: str) -> SandboxResult:
        key = session.key()
        if isinstance(key, str):
            return _incomplete(key)
        hidden_files = positions_holding_hidden_content(files, argument="files")
        hidden_root = positions_holding_hidden_content([root_module], argument="root_module")
        limits = session.spec.files_in
        if not files or len(files) > limits.max_files:
            return _incomplete(
                f"{operation} INCOMPLETE: the manifest is empty or exceeds the file-count limit."
            )
        listing = await session.list_files(store)
        if isinstance(listing, str):
            return _incomplete(listing)
        try:
            root, selected = resolve_manifest(files, root_module, listing, engine)
        except ValueError as exc:
            return _incomplete(f"{operation} INCOMPLETE: {exc}")
        staged: list[tuple[str, str]] = []
        total = 0
        for position, (path, listed) in enumerate(selected):
            item = await session.read_file(
                store,
                listed,
                at=f"files[{position}]",
                hidden=position in hidden_files,
                named=f"files[{position}]",
            )
            if isinstance(item, str):
                return _incomplete(item)
            if item is None or item.text is None or "\x00" in item.text:
                return _incomplete(
                    f"{operation} INCOMPLETE: every manifest file must contain text."
                )
            try:
                size = len(item.text.encode("utf-8"))
            except UnicodeError:
                return _incomplete(
                    f"{operation} INCOMPLETE: every manifest file must be valid UTF-8 text."
                )
            total += size
            if size > limits.max_bytes_per_file or total > limits.max_total_bytes:
                return _incomplete(
                    f"{operation} INCOMPLETE: the manifest exceeds the transfer byte limits."
                )
            staged.append((path, item.text))
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return _incomplete(sandbox)
        guest_call_path = session.guest_call_path()
        try:
            for path, content in staged:
                await sandbox.write_file(
                    "project/" + path, content, working_directory=guest_call_path
                )
            execution = asyncio.create_task(
                sandbox.exec(
                    [
                        "/usr/local/bin/python3",
                        "-I",
                        "/opt/maf-terraform/runner.py",
                        engine,
                        root,
                        str(timeout),
                        *(["format"] if formatting else []),
                    ],
                    working_directory=guest_call_path,
                    timeout=timeout + 5,
                )
            )
            try:
                result = await asyncio.shield(execution)
            except asyncio.CancelledError:
                while not execution.done():
                    try:
                        await asyncio.shield(execution)
                    except (asyncio.CancelledError, Exception):
                        # Finish draining before disposal; preserve the caller's cancellation.
                        pass
                if not execution.cancelled():
                    execution.exception()
                raise
            if result.exit_code != 0 or result.producer_owns_stderr or result.stderr_bytes:
                return _incomplete(
                    f"{operation} INCOMPLETE: the fixed launcher did not return a complete report."
                )
            hidden = bool(hidden_files or hidden_root)
            if formatting:
                formatted = format_outcome(result.stdout_bytes, engine, dict(staged), hidden=hidden)
                # The engine's own text whichever way it went: a kind cannot vouch for file
                # content it did not write.
                return SandboxResult(
                    completed=formatted.ran,
                    verdict=("changed" if formatted.valid else "unchanged")
                    if formatted.ran
                    else None,
                    output=(formatted.text,),
                )
            outcome = report_outcome(result.stdout_bytes, engine, hidden=hidden)
            return SandboxResult(
                completed=outcome.ran,
                verdict=("valid" if outcome.valid else "invalid") if outcome.ran else None,
                output=(outcome.text,),
            )
        except Exception as exc:
            logger.warning("terraform %s failed: %s", operation.lower(), error_detail(exc))
            return _incomplete(
                f"{operation} INCOMPLETE: staging, execution, or report verification failed."
            )

    async def validate(files: list[str], root_module: str = ".") -> SandboxResult:
        """Validate a Terraform or OpenTofu root module using the host-selected engine.

        Pass all configuration siblings, local modules, lock files, and referenced text assets
        together as store-relative paths. Set root_module to the relative module directory.
        Only listed files are staged. Dependencies must already be in the image's offline mirror.
        Initialization failure is incomplete validation.

        This tool only checks. It reports whether files need formatting, but it does not rewrite
        them or return the formatted text. It runs no other engine command, downloads nothing, and
        does not plan, apply, read remote state, or deploy.
        """
        return await report(files, root_module)

    async def format_files(files: list[str], root_module: str = ".") -> SandboxResult:
        """Return whole formatted files changed by the host-selected engine, without store writes.

        Pass the same explicit manifest and root_module as validation, including configuration
        siblings and local modules. Formatting covers the staged project and needs no providers,
        modules, or initialization. The result maps store-relative paths to complete UTF-8 text.
        Write these files with the host's file tools. Unchanged files are omitted. If the complete
        JSON report exceeds 128 KiB, no file text is returned; use a smaller complete manifest.
        Hidden argument names withhold all file text. Formatting does not validate or deploy.
        """
        return await report(files, root_module)

    return format_files if formatting else validate
