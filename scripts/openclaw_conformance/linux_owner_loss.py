"""Qualify the fixed Linux actor-loss profile and publish only bounded Markdown."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import platform
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from .evidence import LIMITS, Refused, digest, encode, file_hash, read, require, write
from .install import FIXTURES, REPO, SAMPLE, runner_hash, runner_revision
from .linux_docker_failures import DENIAL_IMAGE, DENIAL_REFERENCE, denied_case, verify_failure
from .owner_loss_cleanup import cleanup
from .owner_loss_evidence import CASES, DEADLINES, DENIAL_SOURCE, one, verify

PROFILE = "owner-loss-linux-v3"
WHEEL = "ebb54bc8b51d4d0ec3e63f1f850c5aa53b55d80ec5567c679ead169a3ed0d545"
VERSIONS = {
    "maf-sandbox-mcp": "0.2.0",
    "maf-sandbox": "0.51.1",
    "maf-sandbox-bicep": "0.24.0",
    "maf-sandbox-docker": "0.29.0",
    "agent-framework-core": "1.22.0",
    "mcp": "1.30.0",
}


def runtime(root: Path) -> dict[str, Any]:
    """Bind every installed runtime file, including bytecode and resolved symlink targets."""
    files = {}
    for name in ("worker", "openclaw"):
        base = root / name
        require(base.is_dir(), "runtime_missing")
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                if path.is_dir():
                    require(path.resolve().is_relative_to(base.resolve()), "runtime_directory_link")
                    files[path.relative_to(root).as_posix()] = digest(
                        {"target": path.resolve().relative_to(base.resolve()).as_posix()}
                    )
                    continue
                require(path.is_file(), "runtime_broken_link")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = file_hash(path)
                require(len(files) <= 150000, "runtime_file_limit")
    return {"sha256": digest(files), "files": len(files)}


def baseline(prepared: Path) -> dict[str, Any]:
    """Require a fresh installed smoke result with independent resource absence."""
    reporter = importlib.import_module("report_openclaw_hosted")
    value, accepted = reporter.collect(prepared)
    require(accepted, "baseline_unqualified")
    artifact = value["artifact"]
    require(artifact["wheel_sha256"] == WHEEL, "worker_wheel")
    require(all(artifact["versions"].get(k) == v for k, v in VERSIONS.items()), "worker_versions")
    require(value["openclaw"] == "2026.9.7", "gateway_version")
    config_hash = file_hash(prepared / "bicepconfig.json")
    require(
        all(
            turn["image"] == value["image"] and turn["config_sha256"] == config_hash
            for turn in value["turns"]
        ),
        "baseline_identity",
    )
    return {
        "baseline_report_sha256": value["report_sha256"],
        "baseline_tool_evidence_sha256": value["tool_evidence_sha256"],
        "wheel_sha256": WHEEL,
        "versions": artifact["versions"],
        "worker_modules": artifact["modules"],
        "image": value["image"],
        "config_sha256": config_hash,
        "openclaw": value["openclaw"],
        "npm_lock_sha256": file_hash(prepared / "openclaw/package-lock.json"),
        "runtime": runtime(prepared),
        "source_sha256": runner_hash(),
        "source_commit": runner_revision(),
        "host": {
            "platform": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            **{
                key: os.environ.get(key, "")
                for key in (
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "GITHUB_SHA",
                    "ImageOS",
                    "ImageVersion",
                )
            },
        },
    }


def plan(candidate: dict[str, Any], campaign: str) -> dict[str, Any]:
    """Fix the case inventory and supported dependencies before execution."""
    require(candidate["host"]["platform"] == "Linux", "linux_required")
    require(candidate["wheel_sha256"] == WHEEL, "worker_wheel")
    require(all(candidate["versions"].get(k) == v for k, v in VERSIONS.items()), "worker_versions")
    require(isinstance(campaign, str) and re.fullmatch(r"[0-9a-f]{32}", campaign), "campaign_id")
    return {
        "profile": PROFILE,
        "campaign_id": campaign,
        "candidate_sha256": digest(candidate),
        "cases": list(CASES),
        "deadlines": DEADLINES,
        "fault_unit": "pidfd_bound_actor_in_supervised_session",
        "replacement": "operator_restart_same_owner_rotated_token",
        "docker_faults": {
            "unavailable": "replacement_private_context",
            "denial_reference": DENIAL_REFERENCE,
            "denial_image": DENIAL_IMAGE,
            "denial_source_sha256": DENIAL_SOURCE,
        },
    }


def verify_case(value: dict[str, Any], name: str, candidate: dict[str, Any], campaign: str) -> None:
    """Require recovery before any emergency cleanup intervention."""
    require(name in CASES, "case_not_selected")
    require(
        value.get("campaign_id") == campaign and value.get("candidate_sha256") == digest(candidate),
        "case_campaign_binding",
    )
    verify(value, name, candidate, platform="Linux")
    require(value["independent_cleanup"].get("removed") == [], "emergency_cleanup_required")
    require(one(value["events"], "harness_leftovers")["resources"] == [], "harness_removed_orphans")
    if name in {"worker-unavailable", "worker-denied"}:
        verify_failure(value, name)


def run(prepared: Path, root: Path) -> None:
    """Inject bounded faults only after fixing the candidate and complete case inventory."""
    if sys.platform != "linux":
        raise Refused("linux_required")
    require(
        Path(sys.prefix).resolve() == (prepared / "worker").resolve(),
        "installed_interpreter_required",
    )
    require(
        not root.exists() and not root.is_relative_to(REPO) and not root.is_relative_to(prepared),
        "fresh_external_output_required",
    )
    root.mkdir(parents=True, mode=0o700)
    candidate = baseline(prepared)
    expected = plan(candidate, uuid.uuid4().hex)
    write(root / "candidate.json", candidate)
    write(root / "plan.json", expected)
    sys.path[:0] = [str(FIXTURES), str(SAMPLE)]
    from .owner_loss_cases import case

    summary: dict[str, Any] = {
        "plan_sha256": digest(expected),
        "completed": [],
        "case_hashes": {},
        "runtime_unchanged": False,
    }
    write(root / "summary.json", summary)

    def expired(_signum, _frame):
        raise Refused("fault_case_deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        for name in CASES:
            print("Linux actor-loss case: " + name, flush=True)
            value: dict[str, Any] = {"case": name, "outcome": "fail"}
            failure = None
            try:
                signal.alarm(DEADLINES["case"])
                value = (
                    denied_case(root / name, prepared, candidate)
                    if name == "worker-denied"
                    else case(root / name, prepared, candidate, name)
                )
            except (Exception, KeyboardInterrupt) as exc:
                failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
            finally:
                signal.alarm(0)
                value["campaign_id"] = expected["campaign_id"]
                value["candidate_sha256"] = expected["candidate_sha256"]
                value["parent_failure"] = failure
                try:
                    require(
                        one(value.get("events", []), "harness_cleanup")["processes_stopped"]
                        is True,
                        "process_cleanup_unconfirmed",
                    )
                    value["independent_cleanup"] = cleanup(root / name, name)
                except (Exception, KeyboardInterrupt) as exc:
                    value["independent_cleanup"] = {"confirmed": False, "error": safe_reason(exc)}
                    if isinstance(exc, KeyboardInterrupt):
                        value["parent_failure"] = "KeyboardInterrupt"
                data = encode(value)
                require(len(data) <= LIMITS["events"], "case_evidence_limit")
                path = root / (name + ".json")
                path.write_bytes(data)
                summary["case_hashes"][name] = file_hash(path)
                summary["completed"].append(name)
                write(root / "summary.json", summary)
            # Continue after a rejected result only when cleanup is independently confirmed.
            require(value["independent_cleanup"]["confirmed"] is True, "cleanup_unconfirmed")
            require(
                one(value["events"], "harness_cleanup")["processes_stopped"] is True,
                "process_cleanup_unconfirmed",
            )
            require(
                failure != "KeyboardInterrupt" and value.get("failure") != "KeyboardInterrupt",
                "interrupted",
            )
        summary["runtime_unchanged"] = runtime(prepared) == candidate["runtime"]
        write(root / "summary.json", summary)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def safe_reason(exc: BaseException) -> str:
    """Publish bounded refusal codes without exception messages from dependencies."""
    reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
    return reason if re.fullmatch(r"[A-Za-z0-9_:-]{1,120}", reason) else type(exc).__name__


def failure_code(value: Any) -> str | None:
    """Retain refusal codes without publishing arbitrary failure text."""
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,120}", value)
        else "redacted"
    )


def postflight(root: Path, name: str, case_hash: str) -> None:
    """Retain failed or interrupted read-only inventories across repeated collections."""
    receipt = root / (name + ".postflight.json")
    if receipt.exists():
        previous = read(receipt)
        require(previous.get("case_sha256") == case_hash, "postflight_binding")
        require(previous.get("passed") is True, "postflight_previous_failure")
    state = {"case_sha256": case_hash, "passed": False}
    write(receipt, state)
    if name == "worker-denied":
        daemon = read(root / (name + ".json"), LIMITS["events"])["daemon"]
        require(
            file_hash(root / name / "daemon-resources.json") == daemon["registration_sha256"]
            and file_hash(root / name / "daemon-control.json") == daemon["control_sha256"],
            "daemon_registration_changed",
        )
    remaining = cleanup(root / name, name, remove=False)
    require(
        remaining["confirmed"] is True
        and remaining["removed"] == []
        and remaining["remaining"] == [],
        "postflight_survivor",
    )
    state["passed"] = True
    write(receipt, state)


def collect(prepared: Path, root: Path) -> dict[str, Any]:
    """Re-evaluate raw evidence, runtime identity and resource absence before publication."""
    public: dict[str, Any] = {"profile": PROFILE, "qualified": False, "cases": []}
    sys.path[:0] = [str(FIXTURES), str(SAMPLE)]
    candidate, expected, summary = {}, {}, {}
    try:
        candidate = read(root / "candidate.json")
        public["candidate"] = candidate
        expected = read(root / "plan.json")
        require(expected == plan(candidate, expected["campaign_id"]), "plan_narrowed")
        require(candidate == baseline(prepared), "candidate_changed")
        summary = read(root / "summary.json")
        require(summary["plan_sha256"] == digest(expected), "summary_campaign_binding")
        public["campaign_id"] = expected["campaign_id"]
        reporter = importlib.import_module("report_openclaw_hosted")
        public["docker"] = json.loads(
            reporter.command("docker", "version", "--format", "{{json .Server}}")
        )
        public["node"] = reporter.command("node", "--version")
        public["uv"] = reporter.command("uv", "--version")
    except Exception as exc:
        public["rejection"] = safe_reason(exc)
    for name in CASES:
        row: dict[str, Any] = {"case": name, "passed": False}
        public["cases"].append(row)
        try:
            path = root / (name + ".json")
            value = read(path, LIMITS["events"])
            row["sha256"] = file_hash(path)
            duration = value.get("duration")
            row["seconds"] = (
                duration if type(duration) in (int, float) and math.isfinite(duration) else None
            )
            row["parent_failure"] = failure_code(value.get("parent_failure"))
            row["failure"] = failure_code(value.get("failure"))
            row["independent_cleanup"] = (
                value.get("independent_cleanup", {}).get("confirmed") is True
            )
            require("rejection" not in public, "campaign_rejected")
            require(row["sha256"] == summary["case_hashes"].get(name), "case_artifact_digest")
            verify_case(value, name, candidate, expected["campaign_id"])
            postflight(root, name, row["sha256"])
            row["passed"] = True
            if name == "worker-denied":
                row["daemon"] = value["daemon"]
        except Exception as exc:
            row["rejection"] = safe_reason(exc)
    try:
        require("rejection" not in public, "campaign_rejected")
        require(summary["completed"] == list(CASES), "case_inventory")
        require(set(summary["case_hashes"]) == set(CASES), "case_hash_inventory")
        require(summary["runtime_unchanged"] is True, "runtime_changed")
        public["qualified"] = all(row["passed"] for row in public["cases"])
    except Exception as exc:
        public.setdefault("rejection", safe_reason(exc))
    return public


def main() -> int:
    """Run or independently collect the fixed Linux campaign."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "collect"))
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    prepared, root = args.prepared.resolve(), args.root.resolve()
    if args.command == "run":
        try:
            run(prepared, root)
        except Exception as exc:
            print("Linux actor-loss refused: " + safe_reason(exc), flush=True)
            return 1
    observed = collect(prepared, root)
    if args.output:
        text = "# OpenClaw Linux actor-loss qualification\n\n"
        text += "Deterministic provider; actual Gateway, installed worker and compiler. Operator-driven recovery.\n\n"
        text += "Private logs, tokens and session records are not published. Docker faults cover only replacement connection loss and exact-target removal denial. Actual daemon/host loss, escaped process trees, watchdogs and production remain unqualified.\n\n"
        text += "```json\n" + json.dumps(observed, indent=2, sort_keys=True) + "\n```\n"
        args.output.write_text(text, encoding="utf-8")
    print("Linux actor-loss qualified: " + str(observed["qualified"]), flush=True)
    return 0 if observed["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
