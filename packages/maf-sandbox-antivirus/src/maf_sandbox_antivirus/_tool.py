"""Call-isolated antivirus tools with host-supplied, offline database updates."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import Any, cast

from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    Egress,
    FileStoreProvenance,
    Isolation,
    IsolationScope,
    OsFamily,
    Sandbox,
    SandboxRouter,
    SandboxSpec,
    SourceIntegrity,
    TransferLimits,
)
from maf_sandbox.maf import SandboxResult, SandboxToolSession, sandboxed_tool

from ._updates import (
    DATABASES,
    MAX_PATCHES,
    MAX_UPDATE_BYTES,
    UPDATE_CHUNK_BYTES,
    DatabaseIdentity,
    DatabaseUpdate,
    DatabaseUpdateProvider,
)

ANTIVIRUS_KIND = "antivirus"
SCAN_FILE_TOOL_NAME = "scan_file"
_LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE = 32 * 1024


def antivirus_sandbox_spec(
    image: str | None = None, *, max_bytes: int = 32 * 1024 * 1024
) -> SandboxSpec:
    """Require a fresh POSIX guest with closed egress and disposal for every upload."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= 128 * 1024 * 1024:
        raise ValueError("max_bytes must be an integer in [1, 134217728]")
    return SandboxSpec(
        kind=ANTIVIRUS_KIND,
        image=image,
        work_dir="/maf-sandbox/work",
        requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
        requires_os_family=OsFamily.POSIX,
        egress=Egress.CLOSED,
        min_isolation=Isolation.CONTAINER,
        isolation_scope=IsolationScope.CALL,
        min_cleanup=Cleanup.DISPOSE,
        confined_to_guest_call_path=False,
        files_in=TransferLimits(
            max_bytes_per_file=max(max_bytes, UPDATE_CHUNK_BYTES),
            max_total_bytes=MAX_UPDATE_BYTES + max_bytes + 256 * 1024,
            max_files=MAX_PATCHES + 20,
        ),
    )


def make_antivirus_tools(
    router: SandboxRouter | None,
    file_store: Any,
    agent_id: str,
    context: CallerContext,
    *,
    image: str | None = None,
    update_provider: DatabaseUpdateProvider | None = None,
    exec_timeout_seconds: float = 60,
    update_timeout_seconds: float = 120,
    max_bytes: int = 32 * 1024 * 1024,
    max_database_age_days: float = 7,
    max_filesize: int = 32 * 1024 * 1024,
    max_scansize: int = 128 * 1024 * 1024,
    max_files: int = 1000,
    max_recursion: int = 16,
    file_store_provenance: FileStoreProvenance | None = None,
    requires_file_integrity: SourceIntegrity | None = None,
) -> list[Any]:
    """Create scan_file(file), callable by a host through its func or attached as a tool.

    The provider selects an immutable update for the exact image seed. With no provider,
    only a seed within the age ceiling is accepted. Hosts must quarantine and retain the
    exact scanned bytes; the result includes their SHA-256, not permission to reread a name.
    """
    spec = antivirus_sandbox_spec(image, max_bytes=max_bytes)
    for name, value, ceiling in (
        ("exec_timeout_seconds", exec_timeout_seconds, 300),
        ("update_timeout_seconds", update_timeout_seconds, 600),
        ("max_database_age_days", max_database_age_days, 30),
    ):
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= ceiling:
            raise ValueError(f"{name} must be finite and in (0, {ceiling}]")
    for name, value, ceiling in (
        ("max_filesize", max_filesize, 128 * 1024 * 1024),
        ("max_scansize", max_scansize, 512 * 1024 * 1024),
        ("max_files", max_files, 10_000),
        ("max_recursion", max_recursion, 100),
    ):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer in [1, {ceiling}]")
    settings = dict(
        max_bytes=max_bytes,
        max_age_seconds=max_database_age_days * 86400,
        max_filesize=max_filesize,
        max_scansize=max_scansize,
        max_files=max_files,
        max_recursion=max_recursion,
    )
    return sandboxed_tool(
        lambda session: _build_tool(
            session,
            file_store,
            update_provider,
            settings,
            exec_timeout_seconds,
            update_timeout_seconds,
        ),
        router=router,
        agent_id=agent_id,
        context=context,
        spec=spec,
        name=SCAN_FILE_TOOL_NAME,
        approval_mode="never_require",
        source_integrity=SourceIntegrity.UNTRUSTED,
        result_contract=True,
        verdicts=("clean", "infected", "refused"),
        file_store_provenance=file_store_provenance,
        requires_file_integrity=requires_file_integrity,
        admission_timeout=max(30, exec_timeout_seconds + update_timeout_seconds),
        logger=_LOGGER,
    )


def _stopped(message: str) -> SandboxResult:
    return SandboxResult(completed=False, trusted_output=(message,))


async def _execute(
    sandbox: Sandbox,
    guest_directory: str,
    phase: str,
    timeout: float,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request is not None:
        await sandbox.write_file(
            "request.json",
            json.dumps(request | {"timeout": timeout}),
            working_directory=guest_directory,
        )
    result = await sandbox.exec(
        ["python3", "-I", "_guest.py", phase],
        working_directory=guest_directory,
        timeout=timeout,
    )
    if result.exit_code != 0 or len(result.stdout) > _MAX_RESPONSE:
        raise ValueError("antivirus guest did not return a complete response")
    response = json.loads(result.stdout)
    if not isinstance(response, dict):
        raise ValueError("invalid antivirus response")
    response = cast(dict[str, Any], response)
    if response.get("status") not in {
        "seed",
        "prepared",
        "clean",
        "infected",
        "refused",
        "error",
    }:
        raise ValueError("invalid antivirus response")
    for channel, diagnostic in (
        ("stderr", result.stderr),
        ("diagnostic", response.get("diagnostic", "")),
    ):
        if diagnostic:
            _LOGGER.debug(
                "scan_file untrusted guest %s %s: %.8192r", phase, channel, str(diagnostic)[:8192]
            )
    return response


def _databases(response: dict[str, Any], status: str) -> dict[str, Any]:
    data = response.get("databases")
    if response.get("status") != status or not isinstance(data, dict):
        raise ValueError("invalid antivirus database response")
    data = cast(dict[str, Any], data)
    if set(data) != set(DATABASES):
        raise ValueError("invalid antivirus database set")
    for raw in data.values():
        if not isinstance(raw, dict):
            raise ValueError("invalid antivirus database metadata")
        value = cast(dict[str, Any], raw)
        expected = {"version", "built", "flevel"}
        if status == "seed":
            expected.add("sha256")
        if set(value) != expected or any(
            type(value.get(key)) is not int or value[key] < 0
            for key in ("version", "built", "flevel")
        ):
            raise ValueError("invalid antivirus database metadata")

    return data


def _build_tool(
    session: SandboxToolSession,
    store: Any,
    provider: DatabaseUpdateProvider | None,
    settings: dict[str, int | float],
    scan_timeout: float,
    update_timeout: float,
) -> Callable[..., Awaitable[SandboxResult]]:
    program = files("maf_sandbox_antivirus").joinpath("_guest.py").read_text("utf-8")

    async def scan_file(file: str) -> SandboxResult:
        """Scan one listed upload; clean means no detection under the host's scan policy."""
        if not isinstance(cast(object, file), str) or not file:
            return _stopped("Error: choose one listed file")
        listing = await session.list_files(store)
        if isinstance(listing, str):
            return _stopped(listing)
        matches = [item for item in listing if item.name == file]
        if len(matches) != 1:
            return _stopped("Error: file must name exactly one visible stored upload")
        item = await session.read_file(store, matches[0], at="file", hidden=True)
        if isinstance(item, str):
            return _stopped(item)
        if item is None or item.uri is None or not item.uri.startswith("data:"):
            return _stopped("Error: the upload store must return binary bytes")
        try:
            encoded = item.uri.split(",", 1)[1]
            if len(encoded) > (settings["max_bytes"] + 2) // 3 * 4:
                return SandboxResult(True, "refused", ("Error: max_bytes exceeded",))
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, IndexError):
            return _stopped("Error: invalid binary upload")
        if len(data) > settings["max_bytes"]:
            return SandboxResult(True, "refused", ("Error: max_bytes exceeded",))
        key = session.key()
        if isinstance(key, str):
            return _stopped(key)
        sandbox = await session.acquire(key)
        if isinstance(sandbox, str):
            return _stopped(sandbox)
        guest_directory = session.guest_call_path()
        try:
            async with asyncio.timeout(update_timeout):
                await sandbox.write_file("_guest.py", program, working_directory=guest_directory)
                identified = _databases(
                    await _execute(sandbox, guest_directory, "seed", update_timeout), "seed"
                )
                identities = tuple(
                    DatabaseIdentity(name, identified[name]["version"], identified[name]["sha256"])
                    for name in DATABASES
                )
                update = (
                    await provider(identities)
                    if provider
                    else DatabaseUpdate(
                        identities, tuple((item.name, item.version) for item in identities)
                    )
                )
                if update.seed != identities:
                    raise ValueError("update provider returned a different seed")
                manifest = update.manifest()
                for index, payload in enumerate(update.files):
                    for part, offset in enumerate(range(0, len(payload.data), UPDATE_CHUNK_BYTES)):
                        await sandbox.write_file(
                            f"update-{index}-{part}.bin",
                            payload.data[offset : offset + UPDATE_CHUNK_BYTES],
                            working_directory=guest_directory,
                        )
                prepared = await _execute(
                    sandbox,
                    guest_directory,
                    "prepare",
                    update_timeout,
                    dict(settings, update=manifest, now=time.time()),
                )
                if prepared["status"] in ("error", "refused"):
                    return SandboxResult(
                        completed=prepared["status"] == "refused",
                        verdict="refused" if prepared["status"] == "refused" else None,
                        trusted_output=("Database preparation did not admit the scan",),
                        output=("Guest diagnostic text withheld.",),
                    )
                databases = _databases(prepared, "prepared")
                if any(databases[name]["version"] != target for name, target in update.targets):
                    raise ValueError("prepared target differs")
            async with asyncio.timeout(scan_timeout):
                await sandbox.write_file("input.bin", data, working_directory=guest_directory)
                response = await _execute(
                    sandbox, guest_directory, "scan", scan_timeout, dict(settings)
                )
            status = response["status"]
            if status not in ("clean", "infected", "refused", "error"):
                raise ValueError("invalid scan status")
            if time.time() - databases["daily"]["built"] > settings["max_age_seconds"]:
                status = "refused"
            if any(value["built"] > time.time() + 300 for value in databases.values()):
                raise ValueError("future database timestamp")
            return SandboxResult(
                completed=status != "error",
                verdict=None if status == "error" else status,
                trusted_output=(
                    "Database identity: " + json.dumps(databases, sort_keys=True),
                    "Database update SHA-256: "
                    + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                ),
                output=(
                    "Input SHA-256: " + hashlib.sha256(data).hexdigest(),
                    "Guest diagnostic text withheld.",
                ),
            )
        except TimeoutError:
            return _stopped("Error: antivirus update or scan timed out")
        except Exception:
            _LOGGER.warning("scan_file: preparation or scan failed", exc_info=True)
            return _stopped("Error: antivirus preparation or scan did not complete")

    return scan_file
