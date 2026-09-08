"""Recover ACAS sandboxes whose auto-delete lifecycle policy is missing.

The backend labels every sandbox it creates, but the auto-delete policy is applied after
creation. A host crash or rejected policy update in that gap leaves a labelled sandbox
without the deletion timer the backend normally relies on. This operator sweep inventories
the sandbox group, finds backend-owned sandboxes without effective auto-delete metadata, and
either installs the lifecycle policy or deletes expired candidates.

Preview is the default. Pass ``--apply`` to change the sandbox group.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

_BACKEND_LABELS = ("scope", "thread", "agent", "kind")


def _string_list() -> list[str]:
    return []


@dataclass(frozen=True)
class RecoveryPolicy:
    """The retention policy for missing auto-delete recovery."""

    fresh_for: timedelta = timedelta(minutes=10)
    stopped_for: timedelta = timedelta(days=1)
    max_age: timedelta | None = timedelta(days=7)
    auto_suspend_seconds: int = 60
    auto_delete_seconds: int = 600


@dataclass
class RecoveryResult:
    """The complete sweep result."""

    fresh_cutoff: str
    stopped_cutoff: str
    max_age_cutoff: str | None
    dry_run: bool
    scanned: int = 0
    candidates: list[str] = field(default_factory=_string_list)
    configured: list[str] = field(default_factory=_string_list)
    installed: list[str] = field(default_factory=_string_list)
    verified: list[str] = field(default_factory=_string_list)
    deleted: list[str] = field(default_factory=_string_list)
    already_absent: list[str] = field(default_factory=_string_list)
    retained: list[str] = field(default_factory=_string_list)
    ignored: list[str] = field(default_factory=_string_list)
    failures: list[str] = field(default_factory=_string_list)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _error_detail(exc: Exception) -> str:
    from maf_sandbox import error_detail

    return error_detail(exc)


def _field(source: Any, *names: str) -> Any | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        payload = cast("Mapping[str, object]", source)
        for name in names:
            if name in payload:
                return payload[name]
    obj = cast("object", source)
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    extra = getattr(obj, "additional_properties", None)
    if isinstance(extra, Mapping):
        payload = cast("Mapping[str, object]", extra)
        for name in names:
            if name in payload:
                return payload[name]
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict):
        try:
            payload = as_dict()
        except Exception:  # noqa: BLE001 - best-effort inspection of a preview SDK model
            return None
        if isinstance(payload, Mapping):
            mapped = cast("Mapping[str, object]", payload)
            for name in names:
                if name in mapped:
                    return mapped[name]
    return None


def _labels(sandbox: Any) -> dict[str, str]:
    raw = _field(sandbox, "labels")
    if not isinstance(raw, Mapping):
        return {}
    labels = cast("Mapping[object, object]", raw)
    return {str(key): str(value) for key, value in labels.items()}


def _backend_owned(sandbox: Any) -> bool:
    labels = _labels(sandbox)
    return all(labels.get(name) for name in _BACKEND_LABELS)


def _timestamp(sandbox: Any, *names: str) -> datetime:
    raw = _field(sandbox, *names)
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{'/'.join(names)} is missing")
    if parsed.utcoffset() is None:
        raise ValueError(f"{'/'.join(names)} has no timezone")
    return parsed.astimezone(UTC)


def _created_at(sandbox: Any) -> datetime:
    return _timestamp(sandbox, "created_at", "createdAt")


def _stopped_at(sandbox: Any) -> datetime | None:
    if _field(sandbox, "state") != "Stopped":
        return None
    details = _field(sandbox, "state_details", "stateDetails")
    return _timestamp(details, "stopped_at", "stoppedAt")


def _lifecycle_policy(sandbox: Any) -> Any:
    return (
        _field(
            sandbox,
            "lifecycle_policy",
            "lifecyclePolicy",
            "effective_lifecycle_policy",
            "effectiveLifecyclePolicy",
        )
        or sandbox
    )


def _auto_delete_enabled(sandbox: Any) -> bool:
    policy = _lifecycle_policy(sandbox)
    auto_delete = _field(policy, "auto_delete", "autoDelete", "autoDeletePolicy")
    if auto_delete is None:
        return False
    return _field(auto_delete, "enabled") is True


def _expiry_reason(
    sandbox: Any, *, policy: RecoveryPolicy, now: datetime, created_at: datetime
) -> str | None:
    if policy.max_age is not None and created_at < now - policy.max_age:
        return "created before maximum age cutoff"
    stopped = _stopped_at(sandbox)
    if stopped is not None and stopped < now - policy.stopped_for:
        return "stopped before stopped-duration cutoff"
    return None


def _lifecycle_policy_for(policy: RecoveryPolicy) -> Any:
    from azure.containerapps.sandbox import AutoDeletePolicy, AutoSuspendPolicy, LifecyclePolicy

    return LifecyclePolicy(
        auto_suspend=AutoSuspendPolicy(
            enabled=True,
            interval=int(policy.auto_suspend_seconds),
            mode="Memory",
        ),
        auto_delete=AutoDeletePolicy(
            enabled=True,
            delete_interval_seconds=int(policy.auto_delete_seconds),
        ),
    )


def _not_found(exc: Exception) -> bool:
    from azure.core.exceptions import ResourceNotFoundError

    return isinstance(exc, ResourceNotFoundError)


async def _delete(client: Any, sandbox_id: str, result: RecoveryResult) -> None:
    try:
        await _maybe_await(client.get_sandbox_client(sandbox_id).begin_delete())
    except Exception as exc:  # noqa: BLE001 - every sandbox is reported independently
        if _not_found(exc):
            result.already_absent.append(sandbox_id)
            return
        result.failures.append(f"Could not delete {sandbox_id!r}: {_error_detail(exc)}")
    else:
        result.deleted.append(sandbox_id)


async def _install_policy(client: Any, sandbox_id: str, policy: RecoveryPolicy) -> None:
    sandbox_client = client.get_sandbox_client(sandbox_id)
    await _maybe_await(sandbox_client.set_lifecycle_policy(_lifecycle_policy_for(policy)))


async def _get_sandbox(client: Any, sandbox_id: str) -> Any:
    return await _maybe_await(client.get_sandbox(sandbox_id))


async def _list_sandboxes(client: Any) -> list[Any]:
    listed = client.list_sandboxes()
    if hasattr(listed, "__aiter__"):
        return [sandbox async for sandbox in listed]
    return list(listed)


async def recover_lifecycle_policies(
    client: Any,
    *,
    policy: RecoveryPolicy = RecoveryPolicy(),
    apply: bool = False,
    now: datetime | None = None,
) -> RecoveryResult:
    """Find backend-owned sandboxes missing auto-delete, then install policy or delete expiry."""
    current = now if now is not None else datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("recovery needs a timezone-aware clock")
    if policy.fresh_for <= timedelta(0) or policy.stopped_for <= timedelta(0):
        raise ValueError("recovery durations must be positive")
    if policy.max_age is not None and policy.max_age <= timedelta(0):
        raise ValueError("max age must be positive when configured")
    if policy.auto_suspend_seconds <= 0 or policy.auto_delete_seconds <= 0:
        raise ValueError("lifecycle intervals must be positive")

    now_utc = current.astimezone(UTC)
    result = RecoveryResult(
        fresh_cutoff=(now_utc - policy.fresh_for).isoformat(),
        stopped_cutoff=(now_utc - policy.stopped_for).isoformat(),
        max_age_cutoff=(now_utc - policy.max_age).isoformat() if policy.max_age else None,
        dry_run=not apply,
    )
    try:
        sandboxes = await _list_sandboxes(client)
    except Exception as exc:  # noqa: BLE001 - no partial page is authoritative
        result.failures.append(f"Could not inventory the sandbox group: {_error_detail(exc)}")
        return result

    result.scanned = len(sandboxes)
    seen: set[str] = set()
    for sandbox in sandboxes:
        sandbox_id = _field(sandbox, "id")
        if not isinstance(sandbox_id, str) or not sandbox_id or sandbox_id in seen:
            result.failures.append("Missing or duplicate sandbox id in inventory; no recovery")
            return result
        seen.add(sandbox_id)

    candidates: dict[str, datetime] = {}
    for sandbox in sandboxes:
        sandbox_id = _field(sandbox, "id")
        assert isinstance(sandbox_id, str)
        if not _backend_owned(sandbox):
            result.ignored.append(sandbox_id)
            continue
        if _auto_delete_enabled(sandbox):
            result.configured.append(sandbox_id)
            continue
        try:
            created = _created_at(sandbox)
        except (TypeError, ValueError) as exc:
            result.failures.append(f"Invalid inventory for {sandbox_id!r}: {exc}")
            result.retained.append(sandbox_id)
            continue
        if created > now_utc - policy.fresh_for:
            result.retained.append(sandbox_id)
            continue
        candidates[sandbox_id] = created
    result.candidates = sorted(candidates)

    if not apply:
        return result

    for sandbox_id, created in candidates.items():
        try:
            sandbox = await _get_sandbox(client, sandbox_id)
        except Exception as exc:  # noqa: BLE001
            if _not_found(exc):
                result.already_absent.append(sandbox_id)
                continue
            result.failures.append(f"Could not re-read {sandbox_id!r}: {_error_detail(exc)}")
            result.retained.append(sandbox_id)
            continue
        if not _backend_owned(sandbox):
            result.retained.append(sandbox_id)
            continue
        if _auto_delete_enabled(sandbox):
            result.configured.append(sandbox_id)
            continue

        try:
            current_created = _created_at(sandbox)
            if current_created != created:
                raise ValueError("createdAt changed since inventory")
            expiry = _expiry_reason(sandbox, policy=policy, now=now_utc, created_at=created)
        except (TypeError, ValueError) as exc:
            result.failures.append(f"Invalid current state for {sandbox_id!r}: {exc}")
            result.retained.append(sandbox_id)
            continue

        try:
            await _install_policy(client, sandbox_id, policy)
        except Exception as exc:  # noqa: BLE001
            result.failures.append(
                f"Could not install lifecycle policy for {sandbox_id!r}: {_error_detail(exc)}"
            )
            if expiry is None:
                result.retained.append(sandbox_id)
                continue
            await _delete(client, sandbox_id, result)
            continue

        result.installed.append(sandbox_id)
        try:
            refreshed = await _get_sandbox(client, sandbox_id)
        except Exception as exc:  # noqa: BLE001
            if _not_found(exc):
                result.already_absent.append(sandbox_id)
                continue
            result.failures.append(
                f"Could not verify lifecycle policy for {sandbox_id!r}: {_error_detail(exc)}"
            )
            result.retained.append(sandbox_id)
            continue
        if _auto_delete_enabled(refreshed):
            result.verified.append(sandbox_id)
            continue
        result.failures.append(f"Lifecycle policy for {sandbox_id!r} still lacks auto-delete")
        if expiry is None:
            result.retained.append(sandbox_id)
            continue
        await _delete(client, sandbox_id, result)
    return result


def _positive_hours(hours: str) -> timedelta:
    try:
        duration = timedelta(hours=float(hours))
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "duration must be a finite positive number of hours"
        ) from exc
    if duration <= timedelta(0):
        raise argparse.ArgumentTypeError("duration must be a finite positive number of hours")
    return duration


def _positive_minutes(minutes: str) -> timedelta:
    try:
        duration = timedelta(minutes=float(minutes))
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "duration must be a finite positive number of minutes"
        ) from exc
    if duration <= timedelta(0):
        raise argparse.ArgumentTypeError("duration must be a finite positive number of minutes")
    return duration


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seconds must be a positive integer") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("seconds must be a positive integer")
    return seconds


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--endpoint", required=True, help="Sandbox group data-plane endpoint.")
    parser.add_argument("--subscription", required=True, help="Subscription id of the group.")
    parser.add_argument("--resource-group", required=True, help="Resource group of the group.")
    parser.add_argument("--group", required=True, help="Sandbox group name.")
    parser.add_argument("--apply", action="store_true", help="change eligible sandboxes")
    parser.add_argument(
        "--fresh-for-minutes",
        type=_positive_minutes,
        default=timedelta(minutes=10),
        help="protect newer sandboxes as configuration-in-progress (default: 10 minutes)",
    )
    parser.add_argument(
        "--stopped-for-hours",
        type=_positive_hours,
        default=timedelta(days=1),
        help="delete candidates stopped continuously this long if policy install fails",
    )
    parser.add_argument(
        "--max-age-hours",
        type=_positive_hours,
        default=timedelta(days=7),
        help="delete candidates this old even if still active when policy install fails",
    )
    parser.add_argument(
        "--no-max-age",
        action="store_true",
        help="do not delete active candidates solely by creation age",
    )
    parser.add_argument(
        "--auto-suspend-seconds",
        type=_positive_seconds,
        default=60,
        help="auto-suspend interval to install (default: 60)",
    )
    parser.add_argument(
        "--auto-delete-seconds",
        type=_positive_seconds,
        default=600,
        help="auto-delete interval to install after suspension (default: 600)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    try:
        from azure.containerapps.sandbox.aio import SandboxGroupClient
        from azure.identity.aio import DefaultAzureCredential
    except ImportError:
        print(
            "azure-containerapps-sandbox is not installed. Run: uv sync --package maf-sandbox-acas",
            file=sys.stderr,
        )
        return 2

    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint=args.endpoint,
        credential=credential,
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        sandbox_group=args.group,
    )
    try:
        policy = RecoveryPolicy(
            fresh_for=args.fresh_for_minutes,
            stopped_for=args.stopped_for_hours,
            max_age=None if args.no_max_age else args.max_age_hours,
            auto_suspend_seconds=args.auto_suspend_seconds,
            auto_delete_seconds=args.auto_delete_seconds,
        )
        result = await recover_lifecycle_policies(client, policy=policy, apply=args.apply)
    finally:
        await client.close()
        await credential.close()
    print(json.dumps(asdict(result), indent=2))
    return 1 if result.failures else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: recover missing lifecycle policy and print a JSON report."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
