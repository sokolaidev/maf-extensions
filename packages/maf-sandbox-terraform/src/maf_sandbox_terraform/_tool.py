"""Validation through the sandbox session, with no backend SDK or lifecycle implementation."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agent_framework import Content
from maf_sandbox import CallerContext, SandboxRouter, SourceIntegrity, error_detail
from maf_sandbox.maf import (
    SandboxToolSession,
    positions_holding_hidden_content,
    sandboxed_tool,
)

from ._paths import resolve_manifest
from ._report import render_report
from ._spec import TerraformEngine, terraform_sandbox_spec

if TYPE_CHECKING:
    from agent_framework import AgentFileStore
    from maf_sandbox import FileStoreProvenance

logger = logging.getLogger(__name__)
STANDING_GUIDANCE = (
    "The other result item is derived from configuration and guest programs. Unread, incomplete, "
    "or failed validation is not a pass. Validation checks configuration and provider schemas; "
    "it does not establish deployment success or run plan, apply, or security policy checks."
)


def make_terraform_tools(
    router: SandboxRouter | None,
    file_store: AgentFileStore,
    agent_id: str,
    context: CallerContext,
    *,
    engine: TerraformEngine = "terraform",
    image: str | None = None,
    image_id: str | None = None,
    exec_timeout_seconds: float = 120,
    file_store_provenance: FileStoreProvenance | None = None,
) -> list[Any]:
    """Attach one engine-specific validation tool, or return [] without a configured backend.

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
    return sandboxed_tool(
        lambda session: _build_tool(session, file_store, engine, exec_timeout_seconds),
        router=router,
        context=context,
        agent_id=agent_id,
        spec=terraform_sandbox_spec(image, image_id, engine=engine),
        name=f"{engine}_validate",
        source_integrity=SourceIntegrity.UNTRUSTED,
        standing_guidance=(STANDING_GUIDANCE,),
        file_store_provenance=file_store_provenance,
        admission_timeout=max(30, exec_timeout_seconds),
        logger=logger,
    )


def _build_tool(
    session: SandboxToolSession, store: AgentFileStore, engine: TerraformEngine, timeout: float
) -> Callable[..., Awaitable[list[Content]]]:
    async def report(files: list[str], root_module: str) -> str:
        key = session.key()
        if isinstance(key, str):
            return key
        hidden_files = positions_holding_hidden_content(files, argument="files")
        hidden_root = positions_holding_hidden_content([root_module], argument="root_module")
        limits = session.spec.files_in
        if not files or len(files) > limits.max_files:
            return "Validation INCOMPLETE: the manifest is empty or exceeds the file-count limit."
        listing = await session.list_files(store)
        if isinstance(listing, str):
            return listing
        try:
            root, selected = resolve_manifest(files, root_module, listing, engine)
        except ValueError as exc:
            return f"Validation INCOMPLETE: {exc}"
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
                return item
            if item is None or item.text is None or "\x00" in item.text:
                return "Validation INCOMPLETE: every manifest file must contain text."
            try:
                size = len(item.text.encode("utf-8"))
            except UnicodeError:
                return "Validation INCOMPLETE: every manifest file must be valid UTF-8 text."
            total += size
            if size > limits.max_bytes_per_file or total > limits.max_total_bytes:
                return "Validation INCOMPLETE: the manifest exceeds the transfer byte limits."
            staged.append((path, item.text))
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return sandbox
        call_path = session.guest_call_path()
        try:
            for path, content in staged:
                await sandbox.write_file("project/" + path, content, working_directory=call_path)
            execution = asyncio.create_task(
                sandbox.exec(
                    [
                        "/usr/local/bin/python3",
                        "-I",
                        "/opt/maf-terraform/runner.py",
                        engine,
                        root,
                        str(timeout),
                    ],
                    working_directory=call_path,
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
                return "Validation INCOMPLETE: the fixed launcher did not return a complete report."
            return render_report(
                result.stdout_bytes, engine, hidden=bool(hidden_files or hidden_root)
            )
        except Exception as exc:
            logger.warning("terraform validation failed: %s", error_detail(exc))
            return "Validation INCOMPLETE: staging, execution, or report verification failed."

    async def validate(files: list[str], root_module: str = ".") -> list[Content]:
        """Validate a Terraform or OpenTofu root module using the host-selected engine.

        Pass all configuration siblings, local modules, lock files, and referenced text assets
        together as store-relative paths. Set root_module to the relative module directory.
        Only listed files are staged. Dependencies must already be in the image's offline mirror.
        Formatting is checked without rewriting files. Initialization failure is incomplete
        validation. No plan, apply, remote state access, or deployment is performed.
        """
        return [
            Content.from_text(await report(files, root_module)),
            Content.from_text(STANDING_GUIDANCE),
        ]

    return validate
