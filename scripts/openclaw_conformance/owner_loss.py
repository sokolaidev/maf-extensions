"""Run and evaluate the explicitly selected bounded actor-loss campaign."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .__main__ import check_logs, check_prepared
from .evidence import LIMITS, Refused, digest, encode, file_hash, read, require, write
from .evidence import evaluate as evaluate_baseline
from .install import REPO, SAMPLE, environment, python_path, runner_hash, runner_revision
from .owner_loss_evidence import CASES, DEADLINES, evaluate, plan, verify


def baseline(bundle: Path, expected: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Require complete qualification of the same candidate before injecting faults."""
    result = evaluate_baseline(bundle, read(expected), manifest)
    return {
        "result": result,
        "plan_sha256": file_hash(expected),
        "summary_sha256": file_hash(bundle / "summary.json"),
    }


def run(
    prepared: Path, manifest: dict[str, Any], baseline_value: dict[str, Any], output: Path
) -> dict[str, Any]:
    """Retain failed attempts and keep independent cleanup outside product recovery."""
    require(
        not output.exists()
        and not output.is_relative_to(REPO)
        and not output.is_relative_to(prepared),
        "fresh_external_output_required",
    )
    output.mkdir(parents=True)
    (output / "temp").mkdir()
    bundle = output / "evidence"
    bundle.mkdir()
    expected = plan(manifest, uuid.uuid4().hex, baseline_value)
    write(output / "plan.json", expected)
    write(output / "candidate.json", manifest)
    write(output / "baseline.json", baseline_value)
    summary = {
        "schema": 1,
        "campaign_id": expected["campaign_id"],
        "candidate_sha256": digest(manifest),
        "plan_sha256": digest(expected),
        "outcome": "unqualified",
        "attempts": [],
        "installation_checks": {"before": None, "after": None},
    }
    write(bundle / "summary.json", summary)
    try:
        check_prepared(prepared, manifest)
        summary["installation_checks"]["before"] = digest(manifest)
        write(bundle / "summary.json", summary)
    except (Exception, KeyboardInterrupt) as exc:
        summary["outcome"] = "failed"
        write(bundle / "summary.json", summary)
        write(
            output / "preflight-failure.json",
            {"reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__},
        )
        return {"outcome": "failed", "reason": "candidate_preflight"}
    sys.path.insert(0, str(SAMPLE))
    Processes = importlib.import_module("agent").Processes

    failures = []
    for case in CASES:
        print("Owner-loss case: " + case, flush=True)
        processes = Processes(output)
        root = output / case
        failure = None
        interrupted = False
        try:
            processes.start(
                case,
                [
                    str(python_path(prepared)),
                    "-I",
                    "-B",
                    "-c",
                    "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('openclaw_conformance.owner_loss_cases',run_name='__main__')",
                    str(REPO / "scripts"),
                    case,
                    str(root),
                    str(prepared),
                    str(output / "candidate.json"),
                ],
                environment(output),
            )
            process = processes.children[0][0]
            deadline = time.monotonic() + DEADLINES["case"] + DEADLINES["supervision"]
            while process.poll() is None:
                require(time.monotonic() < deadline, "fault_case_deadline")
                check_logs(output, case, prepared)
                time.sleep(0.2)
            require(process.returncode == 0, "fault_case_exit")
        except (Exception, KeyboardInterrupt) as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        finally:
            try:
                errors = processes.close()
            except (Exception, KeyboardInterrupt) as exc:
                interrupted |= isinstance(exc, KeyboardInterrupt)
                errors = [type(exc).__name__]
            if errors:
                failure = failure or "case_supervisor_cleanup"
            from .owner_loss_cleanup import cleanup

            try:
                independent = cleanup(root, case)
                if errors or not processes.stopped:
                    independent = {
                        **independent,
                        "confirmed": False,
                        "error": "process_tree_unconfirmed",
                    }
            except (Exception, KeyboardInterrupt) as exc:
                interrupted |= isinstance(exc, KeyboardInterrupt)
                independent = {"confirmed": False, "error": type(exc).__name__}
                failure = failure or "independent_cleanup_unconfirmed"
            try:
                check_logs(output, case, prepared)
            except (Exception, KeyboardInterrupt) as exc:
                interrupted |= isinstance(exc, KeyboardInterrupt)
                reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
                failure = failure or reason
        if interrupted:
            failure = "KeyboardInterrupt"
        result_path = root / "result.json"
        observed: dict[str, Any] = (
            read(result_path, LIMITS["events"])
            if result_path.exists()
            else {"case": case, "outcome": "fail", "failure": failure or "case_result_missing"}
        )
        observed["independent_cleanup"] = independent
        observed["parent_failure"] = failure
        aid = uuid.uuid4().hex
        artifact = {
            "campaign_id": expected["campaign_id"],
            "candidate_sha256": digest(manifest),
            "attempt_id": aid,
            "observations": observed,
        }
        data = encode(artifact)
        require(len(data) <= LIMITS["events"], "fault_artifact_limit")
        artifact_path = bundle / (case + ".json")
        artifact_path.write_bytes(data)
        attempt = {
            "case": case,
            "attempt_id": aid,
            "sha256": file_hash(artifact_path),
            "bytes": len(data),
            "outcome": "fail",
        }
        summary["attempts"].append(attempt)
        write(bundle / "summary.json", summary)
        try:
            require(failure is None, failure or "fault_case_failed")
            verify(observed, case, manifest)
            attempt["outcome"] = "pass"
            write(bundle / "summary.json", summary)
        except (ValueError, KeyError, TypeError) as exc:
            summary["outcome"] = "failed"
            write(bundle / "summary.json", summary)
            failures.append({"case": case, "reason": str(exc)})
            if interrupted or independent.get("confirmed") is not True:
                write(output / "failures.json", failures)
                return {"outcome": "failed", "failures": failures}
    try:
        check_prepared(prepared, manifest)
        summary["installation_checks"]["after"] = digest(manifest)
        write(bundle / "summary.json", summary)
    except (Exception, KeyboardInterrupt) as exc:
        failures.append(
            {
                "case": "postflight",
                "reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__,
            }
        )
    write(output / "failures.json", failures)
    if failures:
        summary["outcome"] = "failed"
        write(bundle / "summary.json", summary)
        return {"outcome": "failed", "failures": failures}
    result = evaluate(bundle, expected, manifest, baseline_value, pending=True)
    summary["outcome"] = "qualified_for_profile"
    write(bundle / "summary.json", summary)
    return result


def main() -> int:
    """Select run or independent evaluation without allowing a report to choose its cases."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bind")
    bind.add_argument("--prepared", type=Path, required=True)
    bind.add_argument("--manifest", type=Path, required=True)
    bind.add_argument("--output", type=Path, required=True)
    for name in ("run", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--baseline-bundle", type=Path, required=True)
        command.add_argument("--baseline-plan", type=Path, required=True)
        if name == "run":
            command.add_argument("--prepared", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = read(args.manifest)
        if args.command == "bind":
            require(not args.output.exists(), "fresh_candidate_required")
            manifest = {
                **manifest,
                "runner_commit": runner_revision(),
                "runner_sha256": runner_hash(),
            }
            check_prepared(args.prepared.resolve(), manifest)
            write(args.output, manifest)
            print(
                json.dumps(
                    {
                        "outcome": "candidate_bound_not_qualified",
                        "candidate_sha256": digest(manifest),
                    }
                )
            )
            return 0
        verified = baseline(args.baseline_bundle, args.baseline_plan, manifest)
        result = (
            run(args.prepared.resolve(), manifest, verified, args.output.resolve())
            if args.command == "run"
            else evaluate(args.bundle, read(args.plan), manifest, verified)
        )
        print(json.dumps(result))
        return 0 if result["outcome"] == "qualified_for_profile" else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
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
