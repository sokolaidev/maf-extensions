# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "azure-containerapps-sandbox==0.1.0b4",
#     "azure-core>=1.30,<2",
#     "azure-identity>=1.25.1,<2",
# ]
# ///
"""Delete long-stopped sandboxes in one ACAS group; preview unless --apply is given.

Only Stopped sandboxes with a service stoppedAt timestamp qualify. Authentication uses
the Azure CLI login, including azure/login in GitHub Actions. No router state is needed.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from azure.containerapps.sandbox import Sandbox, SandboxGroupClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential

_CONFIG = {
    "endpoint": "ACAS_SANDBOX_ENDPOINT",
    "subscription_id": "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "resource_group": "ACAS_SANDBOX_RESOURCE_GROUP",
    "sandbox_group": "ACAS_SANDBOX_GROUP",
}


@dataclass
class CleanupResult:
    """The complete sweep result, with confirmed deletion separate from concurrent removal."""

    cutoff: str
    dry_run: bool
    scanned: int = 0
    eligible: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    already_absent: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _stopped_at(sandbox: Sandbox) -> datetime | None:
    if sandbox.state != "Stopped":
        return None
    if not sandbox.state_details or not sandbox.state_details.stopped_at:
        raise ValueError("Stopped sandbox is missing stateDetails.stoppedAt")
    stopped = datetime.fromisoformat(sandbox.state_details.stopped_at)
    if stopped.utcoffset() is None:
        raise ValueError("stoppedAt has no timezone")
    return stopped.astimezone(UTC)


def cleanup(
    client: SandboxGroupClient,
    *,
    stopped_for: timedelta = timedelta(days=1),
    apply: bool = False,
    now: datetime | None = None,
) -> CleanupResult:
    """Sweep a complete inventory, rechecking each candidate's state and stop time before deletion."""
    current = now if now is not None else datetime.now(UTC)
    if stopped_for <= timedelta(0) or current.utcoffset() is None:
        raise ValueError("cleanup needs a positive stopped duration and a timezone-aware clock")
    cutoff = current.astimezone(UTC) - stopped_for
    result = CleanupResult(cutoff=cutoff.isoformat(), dry_run=not apply)
    try:
        sandboxes = list(client.list_sandboxes())
    except Exception as exc:
        result.failures.append(f"Could not inventory the sandbox group: {exc}")
        return result

    result.scanned = len(sandboxes)
    seen: set[str] = set()
    for sandbox in sandboxes:
        if not sandbox.id or sandbox.id in seen:
            result.failures.append("Missing or duplicate sandbox id in inventory; no deletions")
            return result
        seen.add(sandbox.id)

    candidates: list[tuple[str, datetime]] = []
    for sandbox in sandboxes:
        try:
            stopped = _stopped_at(sandbox)
            if stopped is not None and stopped < cutoff:
                candidates.append((sandbox.id, stopped))
        except (TypeError, ValueError) as exc:
            result.failures.append(f"Invalid inventory for {sandbox.id!r}: {exc}")

    result.eligible = [sandbox_id for sandbox_id, _ in candidates]
    if not apply:
        return result

    for sandbox_id, stopped in candidates:
        try:
            sandbox = client.get_sandbox(sandbox_id)
            if sandbox.id != sandbox_id:
                raise ValueError("sandbox identity changed since inventory")
            if _stopped_at(sandbox) != stopped:
                result.retained.append(sandbox_id)
                continue
            client.begin_delete_sandbox(sandbox_id, polling_timeout=60, polling_interval=2).result()
        except ResourceNotFoundError:
            result.already_absent.append(sandbox_id)
        except Exception as exc:
            result.failures.append(f"Could not delete {sandbox_id!r}: {exc}")
        else:
            result.deleted.append(sandbox_id)
    return result


def _hours(value: str) -> timedelta:
    try:
        duration = timedelta(hours=float(value))
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "duration must be a finite positive number of hours"
        ) from exc
    if duration <= timedelta(0):
        raise argparse.ArgumentTypeError("duration must be a finite positive number of hours")
    return duration


def main(argv: list[str] | None = None) -> int:
    """Run one group-scoped sweep and return failure if any resource could not be accounted for."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stopped-for-hours",
        type=_hours,
        default=timedelta(days=1),
        dest="stopped_for",
        help="minimum continuous stopped duration (default: 24 hours)",
    )
    parser.add_argument("--apply", action="store_true", help="delete eligible sandboxes")
    parser.add_argument("--summary", type=Path, help="append a Markdown execution summary")
    args = parser.parse_args(argv)
    config = {key: os.environ.get(variable, "").strip() for key, variable in _CONFIG.items()}
    missing = [variable for key, variable in _CONFIG.items() if not config[key]]
    if missing:
        parser.error("missing configuration: " + ", ".join(missing))

    try:
        with AzureCliCredential() as credential:
            with SandboxGroupClient(
                endpoint=config["endpoint"],
                credential=credential,
                subscription_id=config["subscription_id"],
                resource_group=config["resource_group"],
                sandbox_group=config["sandbox_group"],
            ) as client:
                result = cleanup(client, stopped_for=args.stopped_for, apply=args.apply)
    except Exception as exc:
        result = CleanupResult(
            cutoff="unavailable", dry_run=not args.apply, failures=[f"Cleanup failed: {exc}"]
        )
    print(json.dumps(asdict(result), indent=2))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(
                "## ACAS cleanup\n\n"
                f"Mode: {'preview' if result.dry_run else 'delete'}. "
                f"Stopped before: {result.cutoff}.\n\n"
                "| Scanned | Eligible | Deleted | Already absent | Retained after recheck | Failures |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                f"| {result.scanned} | {len(result.eligible)} | {len(result.deleted)} | "
                f"{len(result.already_absent)} | {len(result.retained)} | {len(result.failures)} |\n"
            )
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
