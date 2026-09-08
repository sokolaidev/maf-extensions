"""Operator retention over WSLC inspection, independent of the process registry."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

from maf_sandbox import DisposalFailure, error_detail

NETWORK_CREATED_LABEL = "maf-sandbox.network-created-at"
_IDENTITY_LABELS = tuple(f"maf-sandbox.{part}" for part in ("scope", "thread", "agent", "kind"))
_NAME = re.compile(r"(?P<workload>maf-sandbox-wslc-[0-9a-f]{12})(?P<suffix>-proxy|-net)?")
_ID = re.compile(r"[0-9a-f]{64}")
_Resource = Literal["container", "network"]


class CommandResult(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def stdout_text(self) -> str: ...

    @property
    def stderr_text(self) -> str: ...


class Command(Protocol):
    async def __call__(self, *args: str) -> CommandResult: ...


@dataclass(frozen=True)
class WslcReapResult:
    """Successful removals, with infrastructure counted separately, and incomplete cleanup."""

    disposed: int = 0
    proxies_removed: int = 0
    networks_removed: int = 0
    failures: tuple[DisposalFailure, ...] = ()


@dataclass(frozen=True)
class _Target:
    id: str
    name: str
    resource: _Resource
    data: dict[str, object]

    @property
    def workload(self) -> str:
        return self.name.removesuffix("-proxy").removesuffix("-net")


def _objects(payload: str) -> list[dict[str, object]]:
    data: object = json.loads(payload)
    if not isinstance(data, list) or any(
        not isinstance(row, dict) for row in cast("list[object]", data)
    ):
        raise ValueError("expected a JSON array of objects")
    return cast("list[dict[str, object]]", data)


def _listing(payload: str) -> list[dict[str, object]]:
    if payload.lstrip().startswith("["):
        return _objects(payload)
    # WSLC 2.9.10 emits one JSON object per line, including no output for an empty list.
    return _objects("[" + ",".join(payload.splitlines()) + "]")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("missing or invalid timestamp with timezone")
    parsed = datetime.fromisoformat(value).astimezone(UTC)
    if parsed.year <= 1:
        raise ValueError("unset timestamp")
    return parsed


def _labels(data: dict[str, object]) -> dict[str, object]:
    # WSLC 2.9.3 exposes Labels at the top level; later versions also expose Config.Labels.
    labels = data.get("Labels")
    if not isinstance(labels, dict):
        raise ValueError("missing ownership labels")
    return cast("dict[str, object]", labels)


def _owned(target: _Target, scope: str | None) -> bool:
    labels = _labels(target.data)
    if any(not isinstance(labels.get(key), str) or not labels[key] for key in _IDENTITY_LABELS):
        return False
    if scope is not None and labels["maf-sandbox.scope"] != scope:
        return False
    role = labels.get("maf-sandbox.role")
    if target.name.endswith("-proxy"):
        return target.resource == "container" and role == "proxy"
    if target.name.endswith("-net"):
        return target.resource == "network" and role is None and target.data.get("Internal") is True
    return target.resource == "container" and role is None


def _expired(anchor: _Target, cutoff: datetime) -> bool:
    if anchor.resource == "network":
        return _timestamp(_labels(anchor.data).get(NETWORK_CREATED_LABEL)) < cutoff
    if anchor.name.endswith("-proxy"):
        return _timestamp(anchor.data.get("Created")) < cutoff
    state = anchor.data.get("State")
    if not isinstance(state, dict):
        raise ValueError("missing container state")
    state = cast("dict[str, object]", state)
    if state.get("Running") is not False:
        return False
    if state.get("Status") == "exited":
        return _timestamp(state.get("FinishedAt")) < cutoff
    if state.get("Status") == "created":
        return _timestamp(anchor.data.get("Created")) < cutoff
    return False


class _Sweep:
    def __init__(self, command: Command, scope: str | None) -> None:
        self.command = command
        self.scope = scope
        self.failures: list[DisposalFailure] = []

    def failed(self, target: str, exc: Exception) -> None:
        self.failures.append(DisposalFailure("unreachable", f"{target}: {error_detail(exc)}"))

    async def inspect(self, resource: _Resource, identity: str) -> dict[str, object] | None:
        result = await self.command(resource, "inspect", identity)
        if result.returncode:
            # Inspect catches NOT_FOUND and emits an empty array with exit 1, without its code.
            if result.returncode == 1 and result.stdout_text.strip() == "[]":
                return None
            raise RuntimeError(result.stderr_text.strip() or f"inspect exited {result.returncode}")
        rows = _objects(result.stdout_text)
        if len(rows) != 1:
            raise ValueError("expected exactly one inspected resource")
        return rows[0]

    async def inventory(self, resource: _Resource) -> list[_Target]:
        targets: list[_Target] = []
        try:
            args = [resource, "list", "--format", "json"]
            if resource == "container":
                args += ["-a", "--no-trunc"]
            result = await self.command(*args)
            if result.returncode:
                raise RuntimeError(result.stderr_text.strip() or f"list exited {result.returncode}")
            rows = _listing(result.stdout_text)
            seen: set[str] = set()
            for row in rows:
                # Newer CLIs use Docker-shaped ID/Names; inspection retains Id/Name.
                name, identity = row.get("Name", row.get("Names")), row.get("Id", row.get("ID"))
                if not isinstance(name, str):
                    raise ValueError("missing resource name")
                match = _NAME.fullmatch(name)
                if match is None:
                    continue
                if (resource == "network") != (match["suffix"] == "-net"):
                    continue
                if (
                    not isinstance(identity, str)
                    or not re.fullmatch(r"[0-9a-f]{12}(?:[0-9a-f]{52})?", identity)
                    or name in seen
                ):
                    raise ValueError(f"{name}: missing, invalid or duplicate resource identity")
                seen.add(name)
                data = await self.inspect(resource, name if resource == "network" else identity)
                if data is None:
                    continue
                inspected_id = data.get("Id")
                if (
                    not isinstance(inspected_id, str)
                    or not _ID.fullmatch(inspected_id)
                    or not inspected_id.startswith(identity)
                ):
                    raise ValueError(f"{name}: resource ID changed during inventory")
                target = _Target(inspected_id, name, resource, data)
                if not self.matches(target, data):
                    raise ValueError(f"{name}: resource changed during inventory")
                if _owned(target, self.scope):
                    targets.append(target)
        except Exception as exc:  # noqa: BLE001 - an incomplete inventory authorizes no deletion
            self.failed(f"{resource} inventory", exc)
        return targets

    @staticmethod
    def matches(target: _Target, data: dict[str, object]) -> bool:
        name = data.get("Name")
        return (
            data.get("Id") == target.id
            and isinstance(name, str)
            and name.removeprefix("/") == target.name
        )

    async def refresh(self, target: _Target) -> _Target | None:
        identity = target.name if target.resource == "network" else target.id
        data = await self.inspect(target.resource, identity)
        if data is None or not self.matches(target, data):
            return None
        current = _Target(target.id, target.name, target.resource, data)
        if not _owned(current, self.scope) or _labels(data) != _labels(target.data):
            return None
        return current

    async def remove(self, target: _Target, *, proxy: bool = False) -> bool:
        args = [target.resource, "remove"]
        if proxy:
            args.append("-f")
        args.append(target.name if target.resource == "network" else target.id)
        result = await self.command(*args)
        if result.returncode == 0:
            return True
        # Missing-resource diagnostics vary by CLI version and locale; inspect to confirm absence.
        if await self.inspect(target.resource, args[-1]) is None:
            return False
        self.failures.append(
            DisposalFailure(
                "refused", f"{target.name}: {result.stderr_text.strip() or result.returncode}"
            )
        )
        return False


async def reap(
    command: Command,
    stopped_for: timedelta,
    scope: str | None,
    *,
    drain: Callable[[str, str], Awaitable[None]],
    forget: Callable[[str], None],
) -> WslcReapResult:
    """Sweep during operator-exclusive maintenance; WSLC has no conditional network delete."""
    if stopped_for <= timedelta(0):
        raise ValueError("stopped_for must be a positive timedelta")
    try:
        cutoff = datetime.now(UTC) - stopped_for
    except OverflowError:
        cutoff = datetime.min.replace(tzinfo=UTC)
    sweep = _Sweep(command, scope)
    targets = [*await sweep.inventory("container"), *await sweep.inventory("network")]
    if sweep.failures:
        return WslcReapResult(failures=tuple(sweep.failures))
    groups: dict[str, list[_Target]] = {}
    for target in targets:
        groups.setdefault(target.workload, []).append(target)
    eligible: list[list[_Target]] = []
    for group in groups.values():
        group.sort(
            key=lambda target: (target.resource == "network", target.name.endswith("-proxy"))
        )
        try:
            labels = _labels(group[0].data)
            if any(
                any(_labels(target.data)[key] != labels[key] for key in _IDENTITY_LABELS)
                for target in group[1:]
            ):
                raise ValueError("resource group has inconsistent ownership labels")
            if _expired(group[0], cutoff):
                eligible.append(group)
        except (ValueError, OverflowError) as exc:
            sweep.failed(group[0].name, exc)
    disposed = proxies = networks = 0
    for group in eligible:
        anchor = group[0]
        try:
            current = await sweep.refresh(anchor)
            if current is None or not _expired(current, cutoff):
                continue
            # A workload absent from the inventory may have appeared since the proxy/network did.
            if anchor.name != anchor.workload:
                if await sweep.inspect("container", anchor.workload) is not None:
                    continue
            for target in group:
                current = await sweep.refresh(target)
                if current is None:
                    break
                if target.name != target.workload:
                    if await sweep.inspect("container", target.workload) is not None:
                        break
                elif not _expired(current, cutoff):
                    break
                is_proxy = target.name.endswith("-proxy")
                if is_proxy:
                    await drain(target.workload, target.id)
                if not await sweep.remove(target, proxy=is_proxy):
                    break
                if is_proxy:
                    proxies += 1
                    forget(target.workload)
                elif target.resource == "network":
                    networks += 1
                else:
                    disposed += 1
        except Exception as exc:  # noqa: BLE001 - preserve this group and continue independent groups
            sweep.failed(anchor.name, exc)
    return WslcReapResult(disposed, proxies, networks, tuple(sweep.failures))
