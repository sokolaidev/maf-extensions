"""Evaluate bounded observations against an independently selected campaign plan."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from pathlib import Path
from typing import Any

PROFILE = "installed-bicep-windows-v1"
WHEEL = "1228f030135da4e3c5797ec76e19c34275fb7bba20fbc55d3d42d6cc43955529"
INTEGRITY = "sha512-/8N2LnfTFQPvnZizi8qKSFfnLQaPvSG3Cb4xo1YV7b4JhYiUc43ZNRpXJ01bWghLK0Ezk3HVeo/DGHcIRQwRWA=="
CLAIMS = (
    "installed_artifact",
    "worker_import_refusal",
    "configured_policy_refusal",
    "finite_transport",
    "bicep_ownership",
    "deterministic_gateway",
    "closed_tcp_control",
    "supervised_cleanup",
)
GROUPS = (
    "installed",
    "discovery",
    "policy",
    "transport",
    "ownership",
    "gateway",
    "network",
    "cleanup",
)
LIMITS = {
    "summary": 1048576,
    "events": 16777216,
    "log": 8388608,
    "bundle": 67108864,
    "stage_seconds": 1800,
}
OWNERSHIP = {
    "equal_ids_repeated_cancel",
    "response_loss",
    "maf_idle_close",
    "maf_active_close",
    "maf_ordinary_close",
}


class Refused(ValueError):
    """The evidence cannot establish the selected claim."""


def require(value: Any, reason: str) -> None:
    """Refuse evidence that cannot establish its required predicate."""
    if not value:
        raise Refused(reason)


def encode(value: Any) -> bytes:
    """Serialize nonsecret contract fields canonically without nonfinite values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    """Bind a normalized contract to its canonical bytes."""
    return hashlib.sha256(encode(value)).hexdigest()


def file_hash(path: Path) -> str:
    """Hash an artifact without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def read(path: Path, limit: int = LIMITS["summary"]) -> Any:
    """Read bounded JSON while rejecting duplicate keys and nonfinite values."""
    require(path.is_file() and path.stat().st_size <= limit, "missing_or_oversized_record")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "oversized_record")
    try:
        return json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=lambda _: require(False, "nonfinite_json")
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Refused("malformed_record") from exc


def write(path: Path, value: Any) -> None:
    """Atomically replace a bounded campaign summary."""
    data = encode(value) + b"\n"
    require(len(data) <= LIMITS["summary"], "summary_limit")
    temporary = path.with_suffix(".pending")
    temporary.write_bytes(data)
    temporary.replace(path)


def keys(value: Any, expected: set[str]) -> None:
    """Reject missing and unexpected record fields."""
    require(isinstance(value, dict) and set(value) == expected, "record_fields")


def candidate(value: Any) -> None:
    """Validate the supported candidate and required identity fields."""
    keys(
        value,
        {
            "schema",
            "profile",
            "openclaw",
            "npm_integrity",
            "wheel_sha256",
            "versions",
            "python_lock_sha256",
            "npm_lock_sha256",
            "npm_tree",
            "worker_modules",
            "image",
            "config_sha256",
            "host",
            "runner_sha256",
            "runner_commit",
        },
    )
    require(
        type(value["schema"]) is int and value["schema"] == 1 and value["profile"] == PROFILE,
        "unsupported_profile",
    )
    require(
        value["openclaw"] == "2026.9.7"
        and value["npm_integrity"] == INTEGRITY
        and value["wheel_sha256"] == WHEEL,
        "unqualified_artifact",
    )
    require(
        isinstance(value["runner_commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", value["runner_commit"]),
        "runner_commit",
    )
    require(value["host"]["platform"] == "Windows", "unqualified_platform")
    require(value["versions"].get("maf-sandbox-mcp") == "0.1.0", "worker_version")
    for key in ("python_lock_sha256", "npm_lock_sha256", "config_sha256", "runner_sha256"):
        require(
            isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]),
            "invalid_digest",
        )
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"]), "mutable_image")
    require(
        bool(value["versions"])
        and all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in value["versions"].items()
        ),
        "dependency_manifest",
    )


def plan(manifest: dict[str, Any], campaign: str) -> dict[str, Any]:
    """Freeze the selected cases and limits before observations begin."""
    candidate(manifest)
    require(re.fullmatch(r"[0-9a-f]{32}", campaign), "campaign_id")
    return {
        "schema": 1,
        "profile": PROFILE,
        "campaign_id": campaign,
        "candidate_sha256": digest(manifest),
        "groups": list(GROUPS),
        "claims": list(CLAIMS),
        "limits": LIMITS,
    }


def result(value: Any, verdict: str = "valid") -> None:
    """Verify workload completion and cleanup independently of prose."""
    require(isinstance(value, dict), "result_type")
    require(
        value.get("completed") is (verdict != "incomplete")
        and value.get("verdict") == (None if verdict == "incomplete" else verdict)
        and value.get("status") == ("incomplete" if verdict == "incomplete" else "ok")
        and value.get("cleanup") == "confirmed",
        "workload_result",
    )


def predicates(group: str, value: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Apply the fixed profile predicates to a group of observations."""
    if group == "installed":
        require(
            value["versions"] == manifest["versions"]
            and value["wheel_sha256"] == WHEEL
            and value["worker_modules"] == manifest["worker_modules"]
            and value["npm_tree"] == manifest["npm_tree"],
            "installed_candidate_differs",
        )
        require(
            value["origins_verified"] is True and value["assets_verified"] is True,
            "source_fallback",
        )
    elif group == "discovery":
        require(
            value
            == {
                "imports": ["maf_sandbox_mcp.server", "maf_sandbox_mcp.workload_http"],
                "side_effect_attempts": [],
                "exit_code": 0,
                "late_guard_control": True,
            },
            "discovery_effect_or_missing_control",
        )
    elif group == "policy":
        require(
            set(value)
            == {
                "missing_config",
                "oversized_config",
                "mutable_image",
                "malformed_input",
                "no_allocation",
            }
            and all(v is True for v in value.values()),
            "policy_refusal",
        )
    elif group == "network":
        require(value.get("cleanup") is True, "network_cleanup")
        rows = value["observations"]
        require(
            len(rows) == 2
            and rows[0] == {"mode": "bridge", "exit_code": 0, "control_body_matches": True},
            "unreachable_control",
        )
        require(
            rows[1]["mode"] == "none"
            and type(rows[1]["exit_code"]) is int
            and rows[1]["exit_code"] in (7, 28)
            and rows[1]["control_body_matches"] is False,
            "closed_network_not_observed",
        )
    elif group == "transport":
        Results = importlib.import_module("openclaw_published_transport_check").Results

        observed = Results()
        observed.collected = value["cases"]
        observed.reports = value["phases"]
        observed.retirements = value["retirements"]
        observed.final_state = value["final_state"]
        observed.verify(0)
        direct = importlib.import_module("openclaw_published_transport_check").DIRECT_ASGI
        require(
            value["evidence_classes"]
            == {
                name: "direct-ASGI" if name.split("[")[0] in direct else "socket"
                for name in value["cases"]
            },
            "transport_attribution",
        )
        require(
            all(
                type(r["seconds"]) in (int, float)
                and math.isfinite(r["seconds"])
                and r["seconds"] >= 0
                for r in value["phases"]
            ),
            "invalid_duration",
        )
    elif group == "ownership":
        require(
            value["versions"] == manifest["versions"]
            and value["image"] == manifest["image"]
            and value["config_sha256"] == manifest["config_sha256"],
            "ownership_candidate",
        )
        require(set(value["cases"]) == OWNERSHIP, "ownership_inventory")
        for row in value["cases"].values():
            result(row["survivor"])
            require(
                row["survivor"]["image"] == manifest["image"]
                and row["survivor"]["config_sha256"] == manifest["config_sha256"],
                "ownership_identity",
            )
        require(
            value["cases"]["equal_ids_repeated_cancel"]["matching_cancels"] == 4
            and value["cases"]["equal_ids_repeated_cancel"]["other_session_cancels"] == 1,
            "cancel_correlation",
        )
        lost = value["cases"]["response_loss"]
        require(
            lost["socket_closed"] is True
            and lost["binding_result"]["completed"] is True
            and lost["binding_result"]["cleanup"] == "confirmed",
            "response_loss",
        )
        for name in ("maf_idle_close", "maf_active_close"):
            require(value["cases"][name]["delete_observed"] is True, "missing_delete")
        require(
            value["shutdown"]
            == {
                "sessions": 0,
                "active": False,
                "poisoned": False,
                "owned_empty": True,
                "unrelated_preserved": True,
            }
            and value["sentinel_removed"] is True,
            "ownership_cleanup",
        )
    elif group == "gateway":
        ids = value["session_ids"]
        require(
            isinstance(ids, list)
            and len(ids) == 2
            and all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{32}", s) for s in ids)
            and len(set(ids)) == 2,
            "gateway_session_identity",
        )
        require(
            len(value["sessions"]) == 2
            and value["owned_empty"] is True
            and value["entry_point"] == "maf-bicep-mcp",
            "gateway_inventory",
        )
        for session in value["sessions"]:
            require(set(session) == {"valid", "invalid", "incomplete"}, "gateway_cases")
            for case, row in session.items():
                result(row, case)
                sources = importlib.import_module("openclaw_gateway_provider").SOURCES
                require(
                    row["source_sha256"]
                    == hashlib.sha256(
                        json.dumps([["main.bicep", sources[case]]], separators=(",", ":")).encode()
                    ).hexdigest(),
                    "gateway_source_identity",
                )
                require(row["projected_content_items"] == 1, "duplicate_projection")
        result(value["prepared_module"])
        require(
            value["prepared_module"]["image"] == manifest["image"]
            and value["prepared_module"]["config_sha256"] == manifest["config_sha256"],
            "gateway_identity",
        )
        require(
            value["processes_stopped"] is True
            and value["owner_lock_released"] is True
            and value["cleanup_errors"] == [],
            "gateway_cleanup",
        )
    elif group == "cleanup":
        require(
            value
            == {
                "sentinel_survived": True,
                "sentinel_removed": True,
                "processes_stopped": True,
                "owned_empty": True,
                "emergency_cleanup": False,
            },
            "campaign_cleanup",
        )


def evaluate(root: Path, expected: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Reject incomplete or transplanted bundles; never select claims from their reports."""
    require(expected == plan(manifest, expected["campaign_id"]), "plan_narrowed")
    require(
        not (root / "summary.json").is_symlink()
        and (root / "summary.json").resolve().parent == root.resolve(),
        "summary_escape",
    )
    summary = read(root / "summary.json")
    keys(
        summary,
        {
            "schema",
            "campaign_id",
            "plan_sha256",
            "candidate_sha256",
            "started",
            "ended",
            "duration",
            "attempts",
            "outcome",
        },
    )
    require(
        type(summary["schema"]) is int
        and summary["schema"] == 1
        and summary["campaign_id"] == expected["campaign_id"]
        and summary["plan_sha256"] == digest(expected)
        and summary["candidate_sha256"] == digest(manifest),
        "campaign_binding",
    )
    require(
        type(summary["duration"]) in (int, float)
        and math.isfinite(summary["duration"])
        and 0 <= summary["duration"] <= len(GROUPS) * LIMITS["stage_seconds"],
        "campaign_duration",
    )
    require(
        isinstance(summary["started"], str)
        and isinstance(summary["ended"], str)
        and summary["started"] <= summary["ended"],
        "campaign_timestamps",
    )
    require(
        isinstance(summary["attempts"], list) and len(summary["attempts"]) == len(GROUPS),
        "case_inventory",
    )
    seen: set[str] = set()
    total = 0
    for attempt, group in zip(summary["attempts"], GROUPS, strict=True):
        keys(attempt, {"group", "attempt_id", "file", "bytes", "sha256", "outcome"})
        require(
            attempt["group"] == group and attempt["outcome"] == "pass", "missing_or_failed_case"
        )
        aid = attempt["attempt_id"]
        require(
            isinstance(aid, str) and re.fullmatch(r"[0-9a-f]{32}", aid) and aid not in seen,
            "attempt_identity",
        )
        seen.add(aid)
        require(attempt["file"] == group + ".json", "artifact_path")
        path = root / attempt["file"]
        require(
            path.resolve().parent == root.resolve() and not path.is_symlink(), "artifact_escape"
        )
        require(
            type(attempt["bytes"]) is int
            and 0 < attempt["bytes"] <= LIMITS["events"]
            and path.stat().st_size == attempt["bytes"]
            and file_hash(path) == attempt["sha256"],
            "artifact_identity",
        )
        total += attempt["bytes"]
        artifact = read(path, LIMITS["events"])
        keys(artifact, {"campaign_id", "attempt_id", "candidate_sha256", "group", "observations"})
        require(
            artifact["campaign_id"] == expected["campaign_id"]
            and artifact["attempt_id"] == aid
            and artifact["candidate_sha256"] == digest(manifest)
            and artifact["group"] == group,
            "uncorrelated_artifact",
        )
        try:
            predicates(group, artifact["observations"], manifest)
        except (KeyError, TypeError, RuntimeError) as exc:
            raise Refused("invalid_observations_" + group) from exc
    require(total <= LIMITS["bundle"], "bundle_limit")
    require(summary["outcome"] == "qualified_for_profile", "incomplete_summary")
    return {
        "outcome": "qualified_for_profile",
        "profile": PROFILE,
        "candidate_sha256": digest(manifest),
        "campaign_id": expected["campaign_id"],
        "groups": list(GROUPS),
    }
