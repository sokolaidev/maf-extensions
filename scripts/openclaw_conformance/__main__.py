"""Prepare, run or independently evaluate the installed Bicep profile."""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .evidence import (
    GROUPS,
    LIMITS,
    Refused,
    candidate,
    digest,
    encode,
    evaluate,
    file_hash,
    plan,
    predicates,
    read,
    require,
    write,
)
from .install import (
    FIXTURES,
    REPO,
    SAMPLE,
    command,
    environment,
    host,
    npm_tree,
    prepare,
    python_path,
    runner_hash,
    runner_revision,
)

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(SAMPLE))


def utc() -> str:
    """Return a comparable UTC campaign timestamp."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def check_prepared(prepared: Path, manifest: dict[str, Any]) -> None:
    """Refuse changed tooling, host state or prepared input bytes."""
    candidate(manifest)
    runner_revision()
    require(manifest["runner_sha256"] == runner_hash(), "runner_changed")
    for name, field in (
        ("requirements.lock", "python_lock_sha256"),
        ("openclaw/package-lock.json", "npm_lock_sha256"),
        ("bicepconfig.json", "config_sha256"),
    ):
        require(file_hash(prepared / name) == manifest[field], "prepared_input_changed")
    from .assets import verify as verify_assets

    verify_assets(prepared, manifest["runtime_assets"])
    observed_host = host(prepared)
    expected_host = {k: v for k, v in manifest["host"].items() if k != "python"}
    require(observed_host == expected_host, "host_changed")
    require(
        npm_tree(prepared / "openclaw-2026.9.7.tgz", prepared / "openclaw/node_modules/openclaw")
        == manifest["npm_tree"],
        "npm_changed",
    )


def check_logs(root: Path, group: str, prepared: Path) -> None:
    """Bound outer and auxiliary output during execution and after job cleanup."""
    paths = [root / (group + ".log"), prepared / "setup.log"]
    directory = root / group
    if directory.exists():
        paths.extend(directory.rglob("*.log"))
        paths.extend(directory.rglob("*.jsonl"))
    for path in paths:
        if path.exists():
            require(not path.is_symlink(), "stage_log_link")
            require(
                path.stat().st_size <= LIMITS["events" if path.suffix == ".jsonl" else "log"],
                "stage_log_limit",
            )


def stage(group: str, root: Path, prepared: Path, manifest_path: Path) -> dict[str, Any]:
    """Run one bounded checker inside an owned process job."""
    Processes = importlib.import_module("agent").Processes

    processes = Processes(root)
    bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('openclaw_conformance.checks',run_name='__main__')"
    process_args = [
        str(python_path(prepared)),
        "-I",
        "-B",
        "-c",
        bootstrap,
        str(REPO / "scripts"),
        group,
        str(root / group),
        str(prepared),
        str(manifest_path),
    ]
    started = time.monotonic()
    errors = []
    try:
        processes.start(group, process_args, environment(root))
        child = processes.children[0][0]
        while child.poll() is None:
            require(time.monotonic() - started < LIMITS["stage_seconds"], "stage_deadline")
            check_logs(root, group, prepared)
            time.sleep(0.2)
        require(child.returncode == 0, "stage_failed_" + group)
    finally:
        errors = processes.close()
        check_logs(root, group, prepared)
    require(not errors and processes.stopped, "stage_process_cleanup")
    return read(root / group / "observations.json", LIMITS["events"])


def cleanup(root: Path, prepared: Path, resources: dict[str, Any], stopped: bool) -> dict[str, Any]:
    """Attempt every recorded owner independently and retain all observation failures."""
    from .resources import inventory

    require(resources == inventory(resources["campaign"]), "cleanup_resource_identity")
    errors: list[str] = []
    survived = removed = False
    emergency = False
    owned_empty = True

    def attempt(args: list[str], reason: str) -> str | None:
        try:
            return command(args, prepared, 60)
        except Exception:
            errors.append(reason)
            return None

    sentinel = resources["names"]["sentinel"]
    survived = (
        attempt(
            ["docker", "inspect", "--format", "{{.State.Running}}", sentinel],
            "sentinel_observation",
        )
        == "true"
    )
    for relative in (
        "ownership/owner/owner",
        "gateway/owner/owner",
        "policy/refused-owner/owner",
        "policy/input-owner/owner",
    ):
        owner_file = root / relative
        try:
            if not owner_file.exists():
                continue
            owner = owner_file.read_text().strip()
            require(
                len(owner) == 32 and all(c in "0123456789abcdef" for c in owner),
                "cleanup_owner_identity",
            )
            query = [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=maf-sandbox.scope=openclaw-bicep-" + owner,
            ]
            found = attempt(query, relative + ":observe")
            if found:
                emergency = True
                for container in found.split():
                    attempt(["docker", "rm", "-f", container], relative + ":remove")
            after = attempt(query, relative + ":verify")
            if after is None or after:
                owned_empty = False
                errors.append(relative + ":survivors")
        except Exception:
            owned_empty = False
            errors.append(relative + ":owner_identity")
    for role, name in resources["names"].items():
        found = attempt(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "name=^/" + name + "$",
                "--filter",
                "label=" + resources["label"],
            ],
            role + ":observe",
        )
        if found:
            emergency |= role != "sentinel"
            for container in found.split():
                attempt(["docker", "rm", "-f", container], role + ":remove")
        after = attempt(
            ["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], role + ":verify"
        )
        if after is None or after:
            errors.append(role + ":survivors")
            owned_empty = False
        if role == "sentinel":
            removed = after == ""
    return {
        "sentinel_survived": survived,
        "sentinel_removed": removed,
        "processes_stopped": stopped,
        "owned_empty": owned_empty,
        "emergency_cleanup": emergency,
        "errors": errors,
    }


def run(prepared: Path, manifest_path: Path, output: Path) -> dict[str, Any]:
    """Execute the selected profile without reusing prior campaign evidence."""
    require(
        not output.exists()
        and not output.is_relative_to(REPO)
        and not output.is_relative_to(prepared),
        "fresh_external_output_required",
    )
    manifest = read(manifest_path)
    expected = plan(manifest, uuid.uuid4().hex)
    output.mkdir(parents=True, mode=0o700)
    (output / "temp").mkdir()
    bundle = output / "evidence"
    bundle.mkdir()
    write(output / "plan.json", expected)
    write(output / "expected-candidate.json", manifest)
    summary: dict[str, Any] = {
        "schema": 1,
        "campaign_id": expected["campaign_id"],
        "plan_sha256": digest(expected),
        "candidate_sha256": digest(manifest),
        "started": utc(),
        "ended": utc(),
        "duration": 0,
        "attempts": [],
        "outcome": "unqualified",
    }
    write(bundle / "summary.json", summary)
    started = time.monotonic()
    from .resources import inventory

    resources = inventory(expected["campaign_id"])
    write(output / "resources.json", resources)
    resources_started = False
    stopped = True
    failure = ""
    installed_after: dict[str, Any] = {}

    def retain(group: str, observations: dict[str, Any], *, incomplete: bool = False) -> None:
        aid = uuid.uuid4().hex
        artifact = {
            "campaign_id": expected["campaign_id"],
            "attempt_id": aid,
            "candidate_sha256": digest(manifest),
            "group": group,
            "observations": observations,
        }
        # Paths remain in private diagnostics; public correlation uses campaign aliases.
        data = (
            encode(artifact)
            .replace(str(output).replace("\\", "\\\\").encode(), b"<campaign>")
            .replace(str(prepared).replace("\\", "\\\\").encode(), b"<installed-candidate>")
        )
        require(len(data) <= LIMITS["events"], "artifact_limit")
        path = bundle / (group + ".json")
        path.write_bytes(data)
        attempt = {
            "group": group,
            "attempt_id": aid,
            "file": path.name,
            "bytes": len(data),
            "sha256": file_hash(path),
            "outcome": "fail",
        }
        summary["attempts"].append(attempt)
        write(bundle / "summary.json", summary)
        if incomplete:
            return
        predicates(group, observations, manifest)
        attempt["outcome"] = "pass"
        write(bundle / "summary.json", summary)

    try:
        try:
            check_prepared(prepared, manifest)
        except Exception as exc:
            retain("installed", {"incomplete": True, "reason": type(exc).__name__}, incomplete=True)
            raise
        resources_started = True
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                resources["names"]["sentinel"],
                "--label",
                resources["label"],
                "--network",
                "none",
                "--label",
                "maf-sandbox.scope=qualification-sentinel-" + expected["campaign_id"],
                manifest["image"],
                "sleep",
                "infinity",
            ],
            prepared,
            60,
        )
        for group in GROUPS[:-1]:
            print("Checking " + group, flush=True)
            try:
                observations = stage(group, output, prepared, output / "expected-candidate.json")
            except (Exception, KeyboardInterrupt) as exc:
                retain(group, {"incomplete": True, "reason": type(exc).__name__}, incomplete=True)
                raise
            retain(group, observations)
        check_prepared(prepared, manifest)
        postflight = output / "postflight"
        postflight.mkdir()
        (postflight / "temp").mkdir()
        installed_after = stage(
            "installed", postflight, prepared, output / "expected-candidate.json"
        )
        predicates("installed", installed_after, manifest)
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        stopped = False
    finally:
        if resources_started:
            try:
                retain(
                    "cleanup",
                    {
                        **cleanup(output, prepared, resources, stopped),
                        "installed_after": installed_after,
                    },
                )
            except Exception as exc:
                failure = failure or type(exc).__name__
        summary["ended"] = utc()
        summary["duration"] = time.monotonic() - started
        summary["outcome"] = "failed" if failure else "qualified_for_profile"
        write(bundle / "summary.json", summary)
    if failure:
        print("Campaign failed: " + failure, file=sys.stderr)
        return {"outcome": "failed", "reason": failure, "campaign_id": expected["campaign_id"]}
    return evaluate(bundle, expected, manifest)


def main() -> int:
    """Dispatch the explicit qualification command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--output", type=Path, required=True)
    setup.add_argument("--image", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--prepared", type=Path, required=True)
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("evaluate")
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.output.resolve(), args.image)
            print(
                json.dumps(
                    {
                        "candidate_sha256": digest(result),
                        "manifest": str(args.output / "candidate.json"),
                    }
                )
            )
            return 0
        if args.command == "run":
            result = run(args.prepared.resolve(), args.manifest.resolve(), args.output.resolve())
        else:
            result = evaluate(args.bundle.resolve(), read(args.plan), read(args.manifest))
        print(json.dumps(result))
        return 0 if result["outcome"] == "qualified_for_profile" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "outcome": "unqualified",
                    "reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
