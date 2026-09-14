"""Command-line entry point for MST."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._app import SandboxConsole
from ._client import CompositeControl, ControlEndpointError, HttpControl, read_manifests
from ._control import MemoryControl
from ._models import (
    DisposalResult,
    DisposalStatus,
    PurgeResult,
    PurgeStatus,
    SandboxRecord,
    SandboxState,
)
from ._server import EndpointManifest, SandboxControlServer
from ._update import (
    DISTRIBUTION_NAME,
    UpdateError,
    check_for_update,
    current_version,
    inspect_installation,
    perform_update,
)

_EXIT_ERROR = 1
_EXIT_USAGE = 2
_EXIT_NOT_FOUND = 3
_EXIT_DECLINED = 4


@dataclass(frozen=True)
class _HostProbe:
    manifest: EndpointManifest
    client: HttpControl
    error: str | None = None


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number of seconds") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def _nonnegative_integer(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from error
    if count < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return count


def _duration(value: str) -> float:
    text = value.strip().lower()
    factors = {"s": 1.0, "m": 60.0, "h": 3_600.0, "d": 86_400.0}
    factor = factors.get(text[-1:], 1.0)
    number = text[:-1] if text[-1:] in factors else text
    try:
        seconds = float(number) * factor
    except ValueError as error:
        message = "use seconds or a duration such as 30s, 5m, 2h or 1d"
        raise argparse.ArgumentTypeError(message) from error
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("duration must be finite and nonnegative")
    return seconds


def _add_connection_options(parser: argparse.ArgumentParser, *, inherited: bool) -> None:
    default: object = argparse.SUPPRESS if inherited else False
    parser.add_argument(
        "--demo",
        action="store_true",
        default=default,
        help="run against a temporary demo host",
    )
    parser.add_argument(
        "--endpoint",
        default=argparse.SUPPRESS if inherited else None,
        help="connect directly to a literal loopback URL instead of discovering local hosts",
    )
    parser.add_argument(
        "--source",
        default=argparse.SUPPRESS if inherited else "manual",
        help="host label for --endpoint",
    )


def _add_json_option(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if inherited else False,
        help="print JSON instead of human-readable output",
    )


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="select one source id")
    parser.add_argument("--backend", help="select one backend")
    parser.add_argument("--scope", help="select one scope")
    parser.add_argument("--thread", help="select one thread id")
    parser.add_argument("--kind", help="select one sandbox kind")
    parser.add_argument("--state", choices=[state.value for state in SandboxState])
    parser.add_argument(
        "--older-than",
        type=_duration,
        metavar="AGE",
        help="select sandboxes idle for at least AGE, such as 30s, 5m, 2h or 1d",
    )


def _command_parser(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
    _add_connection_options(command, inherited=True)
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mst",
        description="Inspect and dispose sandboxes owned by opted-in MAF applications.",
    )
    _add_connection_options(parser, inherited=False)
    _add_json_option(parser)
    subparsers = parser.add_subparsers(dest="command")

    version = subparsers.add_parser("version", help="show the installed MST version")
    _add_json_option(version, inherited=True)

    update = subparsers.add_parser("update", help="check for or install an MST release")
    selection = update.add_mutually_exclusive_group()
    selection.add_argument(
        "--check",
        action="store_true",
        help="check PyPI without changing the installation",
    )
    selection.add_argument("--to", metavar="VERSION", help="upgrade or roll back to VERSION")
    update.add_argument(
        "--prerelease",
        action="store_true",
        help="include prereleases when selecting the newest version",
    )
    update.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=10.0,
        metavar="SECONDS",
        help="version-check and package-manager timeout",
    )
    _add_json_option(update, inherited=True)

    hosts = _command_parser(subparsers.add_parser("hosts", help="list opted-in MAF hosts"))
    _add_json_option(hosts, inherited=True)

    listing = _command_parser(
        subparsers.add_parser("list", help="print one sandbox inventory snapshot")
    )
    _add_json_option(listing, inherited=True)
    _add_filters(listing)

    show = _command_parser(subparsers.add_parser("show", help="show one exact physical sandbox"))
    show.add_argument("instance_id")
    _add_json_option(show, inherited=True)

    watch = _command_parser(
        subparsers.add_parser("watch", help="continuously print inventory snapshots")
    )
    watch.add_argument(
        "--jsonl",
        "--json",
        dest="jsonl",
        action="store_true",
        help="write one JSON snapshot per line",
    )
    watch.add_argument("--interval", type=_positive_seconds, default=2.0, metavar="SECONDS")
    watch.add_argument(
        "--count",
        type=_nonnegative_integer,
        default=0,
        metavar="N",
        help="stop after N snapshots; zero watches until interrupted",
    )
    _add_filters(watch)

    delete = _command_parser(
        subparsers.add_parser("delete", help="dispose one exact physical sandbox")
    )
    delete.add_argument("instance_id")
    delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    delete.add_argument("--timeout", type=_positive_seconds, default=10.0, metavar="SECONDS")
    _add_json_option(delete, inherited=True)

    purge = _command_parser(
        subparsers.add_parser(
            "purge-thread",
            help="purge a conversation across every responsive MAF host",
        )
    )
    purge.add_argument("--scope", required=True)
    purge.add_argument("--thread", required=True)
    purge.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    purge.add_argument("--timeout", type=_positive_seconds, default=10.0, metavar="SECONDS")
    _add_json_option(purge, inherited=True)
    return parser


async def _probe(manifests: Sequence[EndpointManifest]) -> tuple[_HostProbe, ...]:
    clients = tuple(HttpControl(manifest) for manifest in manifests)
    results = await asyncio.gather(*(client.health() for client in clients), return_exceptions=True)
    return tuple(
        _HostProbe(
            manifest,
            client,
            str(result) if isinstance(result, BaseException) else None,
        )
        for manifest, client, result in zip(manifests, clients, results, strict=True)
    )


def _control(probes: Sequence[_HostProbe]) -> CompositeControl:
    return CompositeControl(
        tuple(probe.client for probe in probes if probe.error is None),
        tuple(
            f"{probe.manifest.source_id}: {probe.error}"
            for probe in probes
            if probe.error is not None
        ),
    )


class _ReloadingControl:
    def __init__(self, load_manifests: Callable[[], tuple[EndpointManifest, ...]]) -> None:
        self._load_manifests = load_manifests

    async def _current(self) -> CompositeControl:
        return _control(await _probe(self._load_manifests()))

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        return await (await self._current()).list_sandboxes()

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        return await (await self._current()).get_sandbox(instance_id)

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        return await (await self._current()).dispose_sandbox(instance_id, timeout=timeout)

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        return await (await self._current()).purge_thread(scope, thread_id, timeout=timeout)


async def _snapshot(
    probes: Sequence[_HostProbe],
) -> tuple[tuple[SandboxRecord, ...], tuple[str, ...]]:
    responsive = tuple(probe for probe in probes if probe.error is None)
    results = await asyncio.gather(
        *(probe.client.list_sandboxes() for probe in responsive),
        return_exceptions=True,
    )
    records: list[SandboxRecord] = []
    errors = [
        f"{probe.manifest.source_id}: {probe.error}" for probe in probes if probe.error is not None
    ]
    for probe, result in zip(responsive, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"{probe.manifest.source_id}: {result}")
        else:
            records.extend(result)
    return (
        tuple(sorted(records, key=lambda item: (item.source_id, item.logical_name, item.kind))),
        tuple(errors),
    )


def _filtered(
    records: Sequence[SandboxRecord], arguments: argparse.Namespace
) -> tuple[SandboxRecord, ...]:
    now = time.time()
    return tuple(
        record
        for record in records
        if (arguments.host is None or record.source_id == arguments.host)
        and (arguments.backend is None or record.backend == arguments.backend)
        and (arguments.scope is None or record.scope == arguments.scope)
        and (arguments.thread is None or record.thread_id == arguments.thread)
        and (arguments.kind is None or record.kind == arguments.kind)
        and (arguments.state is None or record.state.value == arguments.state)
        and (arguments.older_than is None or now - record.last_activity_at >= arguments.older_than)
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], *, empty: str) -> None:
    if not rows:
        print(empty)
        return
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))


def _age(stamp: float) -> str:
    seconds = max(0, int(time.time() - stamp))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3_600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3_600}h"
    return f"{seconds // 86_400}d"


def _print_records(records: Sequence[SandboxRecord]) -> None:
    _table(
        ("SOURCE", "BACKEND", "STATE", "KEY", "KIND", "IDLE", "INSTANCE"),
        tuple(
            (
                record.source_id,
                record.backend,
                record.state.value,
                record.logical_name,
                record.kind,
                _age(record.last_activity_at),
                record.instance_id,
            )
            for record in records
        ),
        empty="No sandboxes.",
    )


def _print_errors(errors: Sequence[str]) -> None:
    for error in errors:
        print(f"mst: warning: {error}", file=sys.stderr)


async def _hosts(probes: Sequence[_HostProbe], *, as_json: bool) -> int:
    payload = [
        {
            "source_id": probe.manifest.source_id,
            "endpoint": probe.manifest.endpoint,
            "process_id": probe.manifest.process_id,
            "protocol_version": probe.manifest.protocol_version,
            "status": "healthy" if probe.error is None else "unavailable",
            "error": probe.error,
        }
        for probe in probes
    ]
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        _table(
            ("SOURCE", "STATUS", "PID", "ENDPOINT", "DETAIL"),
            tuple(
                (
                    str(item["source_id"]),
                    str(item["status"]),
                    "-" if item["process_id"] is None else str(item["process_id"]),
                    str(item["endpoint"]),
                    "" if item["error"] is None else str(item["error"]),
                )
                for item in payload
            ),
            empty="No opted-in MAF hosts.",
        )
    return _EXIT_ERROR if any(probe.error is not None for probe in probes) else 0


async def _list(probes: Sequence[_HostProbe], arguments: argparse.Namespace) -> int:
    records, errors = await _snapshot(probes)
    selected = _filtered(records, arguments)
    if arguments.json:
        print(json.dumps([record.to_json() for record in selected], indent=2))
    else:
        _print_records(selected)
    _print_errors(errors)
    return _EXIT_ERROR if errors else 0


async def _find(
    probes: Sequence[_HostProbe], instance_id: str
) -> tuple[tuple[tuple[HttpControl, SandboxRecord], ...], tuple[str, ...]]:
    responsive = tuple(probe for probe in probes if probe.error is None)
    results = await asyncio.gather(
        *(probe.client.get_sandbox(instance_id) for probe in responsive),
        return_exceptions=True,
    )
    matches: list[tuple[HttpControl, SandboxRecord]] = []
    errors = [
        f"{probe.manifest.source_id}: {probe.error}" for probe in probes if probe.error is not None
    ]
    if not probes:
        errors.append("no opted-in MAF hosts were discovered")
    for probe, result in zip(responsive, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"{probe.manifest.source_id}: {result}")
        elif result is not None:
            matches.append((probe.client, result))
    return tuple(matches), tuple(errors)


def _not_found(instance_id: str, *, as_json: bool, uncertain: bool) -> int:
    message = (
        "Sandbox was not found, but one or more hosts were unavailable."
        if uncertain
        else "Sandbox is already gone or its generation changed."
    )
    if as_json:
        print(
            json.dumps(
                {
                    "status": "unavailable" if uncertain else "not_found",
                    "instance_id": instance_id,
                    "message": message,
                },
                indent=2,
            )
        )
    else:
        print(f"mst: {message}", file=sys.stderr)
    return _EXIT_ERROR if uncertain else _EXIT_NOT_FOUND


def _show_record(record: SandboxRecord) -> None:
    values = (
        ("source", record.source_id),
        ("backend", record.backend),
        ("key", record.logical_name),
        ("kind", record.kind),
        ("instance", record.instance_id),
        ("state", record.state.value),
        ("process", "-" if record.process_id is None else str(record.process_id)),
        ("created", str(record.created_at)),
        ("last activity", str(record.last_activity_at)),
        ("contract", record.execution_contract or "-"),
        ("egress", ", ".join(record.egress_targets) if record.egress_targets else "closed"),
    )
    width = max(len(label) for label, _ in values)
    for label, value in values:
        print(f"{label.ljust(width)}  {value}")


async def _show(probes: Sequence[_HostProbe], arguments: argparse.Namespace) -> int:
    matches, errors = await _find(probes, arguments.instance_id)
    if not matches:
        _print_errors(errors)
        return _not_found(arguments.instance_id, as_json=arguments.json, uncertain=bool(errors))
    if len(matches) > 1:
        raise ControlEndpointError(
            f"instance id {arguments.instance_id!r} is reported by several hosts"
        )
    record = next(iter(matches))[1]
    if arguments.json:
        print(json.dumps(record.to_json(), indent=2))
    else:
        _show_record(record)
    _print_errors(errors)
    return _EXIT_ERROR if errors else 0


def _confirmed(prompt: str, arguments: argparse.Namespace) -> bool | None:
    if arguments.yes:
        return True
    if arguments.json or not sys.stdin.isatty():
        print("mst: confirmation required; pass --yes", file=sys.stderr)
        return None
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


async def _delete(probes: Sequence[_HostProbe], arguments: argparse.Namespace) -> int:
    matches, errors = await _find(probes, arguments.instance_id)
    if not matches:
        _print_errors(errors)
        return _not_found(arguments.instance_id, as_json=arguments.json, uncertain=bool(errors))
    if len(matches) > 1:
        raise ControlEndpointError(
            f"instance id {arguments.instance_id!r} is reported by several hosts"
        )
    if errors:
        _print_errors(errors)
        return _EXIT_ERROR
    client, record = next(iter(matches))
    confirmed = _confirmed(
        f"Dispose {record.logical_name} ({record.instance_id})?",
        arguments,
    )
    if confirmed is None:
        return _EXIT_USAGE
    if not confirmed:
        print("Sandbox kept.")
        return _EXIT_DECLINED
    result = await client.dispose_sandbox(record.instance_id, timeout=arguments.timeout)
    if arguments.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(f"{result.status.value}: {result.message} ({result.instance_id})")
    if result.status is DisposalStatus.DISPOSED:
        return 0
    if result.status is DisposalStatus.NOT_FOUND:
        return _EXIT_NOT_FOUND
    return _EXIT_ERROR


async def _purge(probes: Sequence[_HostProbe], arguments: argparse.Namespace) -> int:
    confirmed = _confirmed(
        f"Purge every sandbox for {arguments.scope}/{arguments.thread} across all hosts?",
        arguments,
    )
    if confirmed is None:
        return _EXIT_USAGE
    if not confirmed:
        print("Conversation kept.")
        return _EXIT_DECLINED
    unavailable = tuple(probe for probe in probes if probe.error is not None)
    result = await _control(probes).purge_thread(
        arguments.scope,
        arguments.thread,
        timeout=arguments.timeout,
    )
    if unavailable and result.status is PurgeStatus.PURGED:
        result = PurgeResult(
            PurgeStatus.PARTIAL,
            arguments.scope,
            arguments.thread,
            result.disposed,
            f"Conversation purge was not attempted on {len(unavailable)} unavailable host(s).",
        )
    if arguments.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(f"{result.status.value}: {result.message} Disposed: {result.disposed}.")
    _print_errors(
        tuple(
            f"{probe.manifest.source_id}: {probe.error}"
            for probe in unavailable
            if probe.error is not None
        )
    )
    return 0 if result.status is PurgeStatus.PURGED else _EXIT_ERROR


async def _watch(
    load_manifests: Callable[[], tuple[EndpointManifest, ...]],
    arguments: argparse.Namespace,
) -> int:
    iteration = 0
    complete = True
    while arguments.count == 0 or iteration < arguments.count:
        probes = await _probe(load_manifests())
        records, errors = await _snapshot(probes)
        complete = complete and not errors
        selected = _filtered(records, arguments)
        observed_at = time.time()
        if arguments.jsonl:
            print(
                json.dumps(
                    {
                        "observed_at": observed_at,
                        "complete": not errors,
                        "errors": list(errors),
                        "sandboxes": [record.to_json() for record in selected],
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        else:
            if iteration:
                print()
            print(f"Snapshot {iteration + 1} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            _print_records(selected)
            _print_errors(errors)
        iteration += 1
        if arguments.count == 0 or iteration < arguments.count:
            await asyncio.sleep(arguments.interval)
    return 0 if complete else _EXIT_ERROR


def _version(arguments: argparse.Namespace) -> int:
    installed = current_version()
    installation = inspect_installation()
    if arguments.json:
        print(
            json.dumps(
                {
                    "name": DISTRIBUTION_NAME,
                    "version": str(installed),
                    "python": sys.version.split()[0],
                    "installation": installation.to_json(),
                },
                indent=2,
            )
        )
    else:
        suffix = "self-update enabled" if installation.self_updatable else "self-update disabled"
        print(f"mst {installed}")
        print(f"installation: {installation.kind.value} ({suffix})")
    return 0


async def _update(arguments: argparse.Namespace) -> int:
    if arguments.check:
        checked = await asyncio.to_thread(
            check_for_update,
            prereleases=arguments.prerelease,
            timeout=arguments.timeout,
        )
        if arguments.json:
            print(json.dumps(checked.to_json(), indent=2))
        elif checked.status == "update_available":
            print(f"MST {checked.latest} is available; {checked.current} is installed.")
        elif checked.status == "current":
            print(f"MST {checked.current} is current.")
        else:
            print(
                f"MST {checked.current} is newer than the latest selected release "
                f"({checked.latest})."
            )
        return 0

    result = await asyncio.to_thread(
        perform_update,
        target=arguments.to,
        prereleases=arguments.prerelease,
        timeout=arguments.timeout,
        capture_output=arguments.json,
    )
    if arguments.json:
        print(json.dumps(result.to_json(), indent=2))
    elif result.status == "current":
        print(f"MST {result.installed} is already current.")
    else:
        print(
            f"MST {result.status} from {result.previous} to {result.installed} "
            f"through {result.installation.kind.value}."
        )
    return 0


async def _dispatch(
    arguments: argparse.Namespace,
    load_manifests: Callable[[], tuple[EndpointManifest, ...]],
) -> int:
    command = arguments.command
    if command == "version":
        return _version(arguments)
    if command == "update":
        return await _update(arguments)
    if command == "watch":
        arguments.jsonl = arguments.jsonl or arguments.json
        return await _watch(load_manifests, arguments)
    if command is None and not arguments.json:
        await SandboxConsole(_ReloadingControl(load_manifests)).run_async()
        return 0
    probes = await _probe(load_manifests())
    if command == "hosts":
        return await _hosts(probes, as_json=arguments.json)
    if command is None:
        arguments.host = None
        arguments.backend = None
        arguments.scope = None
        arguments.thread = None
        arguments.kind = None
        arguments.state = None
        arguments.older_than = None
        return await _list(probes, arguments)
    if command == "list":
        return await _list(probes, arguments)
    if command == "show":
        return await _show(probes, arguments)
    if command == "delete":
        return await _delete(probes, arguments)
    if command == "purge-thread":
        return await _purge(probes, arguments)
    raise AssertionError(f"unknown command {command!r}")


async def _run(arguments: argparse.Namespace) -> int:
    if arguments.command in {"version", "update"}:
        return await _dispatch(arguments, lambda: ())
    if arguments.endpoint:
        manifest = EndpointManifest(arguments.source, arguments.endpoint.rstrip("/"), None)
        return await _dispatch(arguments, lambda: (manifest,))
    if arguments.demo:
        with tempfile.TemporaryDirectory(prefix="mst-demo-") as temporary:
            control = MemoryControl.demo(source_id="mst-demo")
            async with SandboxControlServer(
                control,
                source_id="mst-demo",
                manifest_directory=Path(temporary),
            ) as server:
                return await _dispatch(arguments, lambda: (server.manifest,))
    return await _dispatch(arguments, read_manifests)


def main(argv: Sequence[str] | None = None) -> None:
    """Run MST's TUI or one non-interactive local-control command."""
    arguments = _parser().parse_args(argv)
    if os.environ.get("NO_COLOR") is not None:
        os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "standard")
    try:
        status = asyncio.run(_run(arguments))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ControlEndpointError, UpdateError, ValueError) as error:
        print(f"mst: {error}", file=sys.stderr)
        raise SystemExit(_EXIT_ERROR) from None
    if status:
        raise SystemExit(status)


__all__ = ["main"]
