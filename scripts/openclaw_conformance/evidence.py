"""Evaluate bounded observations against an independently selected campaign plan."""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import math
import re
from pathlib import Path
from typing import Any

PROFILE = "installed-bicep-windows-v4"
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
TRANSPORT_CASES = (
    "test_two_maf_clients_discover_distinct_bindings_and_close_independently",
    "test_shared_admission_equal_ids_cancel_and_delete_drain",
    "test_registry_saturation_churn_and_idle_expiry",
    "test_authentication_and_host_refusal_precede_allocation[GET]",
    "test_authentication_and_host_refusal_precede_allocation[POST]",
    "test_authentication_and_host_refusal_precede_allocation[DELETE]",
    "test_bad_initialization_input_and_output_bounds",
    "test_disconnect_does_not_cancel_and_idle_close_preserves_other_call",
    "test_get_stream_limit_and_shutdown_retirement",
    "test_shutdown_drains_sessions_already_retiring",
    "test_retirement_failure_retains_ownership_until_retry_settles[cancel]",
    "test_retirement_failure_retains_ownership_until_retry_settles[transport]",
    "test_slow_initialize_cannot_allocate_after_shutdown_closes_admission",
    "test_shutdown_waits_for_transport_startup_settlement[connected]",
    "test_shutdown_waits_for_transport_startup_settlement[failed]",
    "test_initialized_ack_waits_for_sdk_dispatch",
    "test_actual_chunked_body_header_and_slow_body_limits",
    "test_actual_connection_cap_includes_incomplete_requests",
    "test_body_reader_budget_released_after_rejection",
    "test_initialization_reservations_are_atomic_and_count_toward_cap",
    "test_idle_expiry_does_not_evict_active_call_and_restart_rejects_old_id",
    "test_incomplete_headers_have_a_real_connection_deadline",
    "test_delete_can_retire_session_before_initialized_notification",
    "test_pending_post_retains_session_reservation[delete]",
    "test_pending_post_retains_session_reservation[shutdown]",
    "test_pending_post_retains_session_reservation[idle]",
    "test_pending_post_releases_reservation_on_body_failure[malformed]",
    "test_pending_post_releases_reservation_on_body_failure[timeout]",
    "test_pending_post_releases_reservation_on_body_failure[oversize]",
    "test_pending_post_releases_reservation_on_body_failure[cancel]",
    "test_nonfinite_json_is_rejected_before_dispatch[False-NaN]",
    "test_nonfinite_json_is_rejected_before_dispatch[False-Infinity]",
    "test_nonfinite_json_is_rejected_before_dispatch[False--Infinity]",
    "test_nonfinite_json_is_rejected_before_dispatch[False-1e400]",
    "test_nonfinite_json_is_rejected_before_dispatch[False--1e400]",
    "test_nonfinite_json_is_rejected_before_dispatch[True-NaN]",
    "test_nonfinite_json_is_rejected_before_dispatch[True-Infinity]",
    "test_nonfinite_json_is_rejected_before_dispatch[True--Infinity]",
    "test_nonfinite_json_is_rejected_before_dispatch[True-1e400]",
    "test_nonfinite_json_is_rejected_before_dispatch[True--1e400]",
    "test_typed_request_ids_keep_responses_and_cancellation_independent[42-42_0]",
    "test_typed_request_ids_keep_responses_and_cancellation_independent[42-42_1]",
    "test_client_request_ids_cannot_replace_sdk_get_stream",
)
LARGE_SOURCE_SHA256 = "237cd37d665cef33521913424b3254501e539299b89d24cb832cfac6b4210ce3"
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


def _same(left: Any, right: Any) -> bool:
    return encode(left) == encode(right)


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


def _float(value: str) -> float:
    result = float(value)
    require(math.isfinite(result), "nonfinite_json")
    return result


def read(path: Path, limit: int = LIMITS["summary"]) -> Any:
    """Read bounded JSON while rejecting duplicate keys and nonfinite values."""
    require(path.is_file() and path.stat().st_size <= limit, "missing_or_oversized_record")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "oversized_record")
    return decode(raw)


def decode(raw: str | bytes) -> Any:
    """Reject duplicate keys and nonfinite values at every JSON boundary."""
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=_float,
            parse_constant=lambda _: require(False, "nonfinite_json"),
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
            "runtime_assets",
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
    assets = value["runtime_assets"]
    keys(
        assets,
        {
            "manifest_sha256",
            "tree_sha256",
            "files",
            "archives",
            "archive_verified_files",
            "generated_files",
        },
    )
    require(
        all(
            isinstance(assets[k], str) and re.fullmatch(r"[0-9a-f]{64}", assets[k])
            for k in ("manifest_sha256", "tree_sha256")
        )
        and all(
            type(assets[k]) is int and assets[k] > 0
            for k in ("files", "archives", "archive_verified_files")
        )
        and isinstance(assets["generated_files"], list)
        and all(isinstance(p, str) for p in assets["generated_files"]),
        "runtime_asset_identity",
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
            and _same(value["npm_tree"], manifest["npm_tree"])
            and _same(value["runtime_assets"], manifest["runtime_assets"]),
            "installed_candidate_differs",
        )
        require(
            value["origins_verified"] is True and value["assets_verified"] is True,
            "source_fallback",
        )
    elif group == "discovery":
        require(
            _same(
                value,
                {
                    "imports": ["maf_sandbox_mcp.server", "maf_sandbox_mcp.workload_http"],
                    "side_effect_attempts": [],
                    "exit_code": 0,
                    "late_guard_control": True,
                },
            ),
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
            and _same(
                rows[0],
                {
                    "mode": "bridge",
                    "exit_code": 0,
                    "control_body_matches": True,
                    "image": manifest["image"],
                },
            ),
            "unreachable_control",
        )
        require(
            rows[1]["mode"] == "none"
            and rows[1].get("image") == manifest["image"]
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
        require(tuple(value["cases"]) == TRANSPORT_CASES, "transport_case_identity")
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
                and row["survivor"]["config_sha256"] == manifest["config_sha256"]
                and row["survivor"].get("source_sha256")
                == importlib.import_module("openclaw_sources").source_digest(
                    importlib.import_module("openclaw_sources").SIMPLE_SOURCE
                ),
                "ownership_identity",
            )
        require(
            type(value["cases"]["equal_ids_repeated_cancel"]["matching_cancels"]) is int
            and value["cases"]["equal_ids_repeated_cancel"]["matching_cancels"] == 4
            and type(value["cases"]["equal_ids_repeated_cancel"]["other_session_cancels"]) is int
            and value["cases"]["equal_ids_repeated_cancel"]["other_session_cancels"] == 1,
            "cancel_correlation",
        )
        from .ownership import verify as verify_ownership

        verify_ownership(value)
        lost = value["cases"]["response_loss"]
        require(
            lost["socket_closed"] is True
            and type(lost["binding_result"]["completed"]) is bool
            and lost["binding_result"]["status"]
            == ("ok" if lost["binding_result"]["completed"] else "incomplete")
            and lost["binding_result"]["source_sha256"] == LARGE_SOURCE_SHA256
            and lost["binding_result"]["cleanup"] == "confirmed",
            "response_loss",
        )
        require(
            isinstance(lost["session"], str) and re.fullmatch(r"[0-9a-f]{64}", lost["session"]),
            "loss_session_identity",
        )
        events = [row for row in value["transport"] if row.get("session") == lost["session"]]
        results = [row for row in events if row.get("event") == "binding_result"]
        require(
            len(results) == 1
            and all(
                _same(results[0].get(key), expected)
                for key, expected in lost["binding_result"].items()
            ),
            "loss_result_correlation",
        )
        require(
            sum(row.get("event") == "binding_started" for row in events) == 1
            and sum(
                row.get("event") == "request" and row.get("method") == "tools/call"
                for row in events
            )
            == 1,
            "loss_replay",
        )
        require(
            not any(
                row.get("event") == "binding_cancelled"
                or row.get("method") == "notifications/cancelled"
                for row in events
            ),
            "loss_implicit_cancel",
        )
        for name in ("maf_idle_close", "maf_active_close"):
            require(value["cases"][name]["delete_observed"] is True, "missing_delete")
        require(
            _same(
                value["shutdown"],
                {
                    "sessions": 0,
                    "active": False,
                    "poisoned": False,
                    "owned_empty": True,
                    "unrelated_preserved": True,
                },
            )
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
                require(
                    row.get("image") == manifest["image"]
                    and row.get("config_sha256") == manifest["config_sha256"],
                    "gateway_identity",
                )
                sources = importlib.import_module("openclaw_gateway_provider").SOURCES
                require(
                    row["source_sha256"]
                    == hashlib.sha256(
                        json.dumps([["main.bicep", sources[case]]], separators=(",", ":")).encode()
                    ).hexdigest(),
                    "gateway_source_identity",
                )
                require(
                    type(row["projected_content_items"]) is int
                    and row["projected_content_items"] == 1,
                    "duplicate_projection",
                )
        from .gateway import verify

        verify(value, manifest)
        result(value["prepared_module"])
        sources = importlib.import_module("openclaw_sources")
        require(
            value["prepared_module"].get("source_sha256")
            == sources.source_digest(sources.PREPARED_SOURCE),
            "prepared_source_identity",
        )
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
            set(value)
            == {
                "sentinel_survived",
                "sentinel_removed",
                "processes_stopped",
                "owned_empty",
                "emergency_cleanup",
                "installed_after",
                "errors",
            },
            "cleanup_fields",
        )
        predicates("installed", value["installed_after"], manifest)
        require(
            _same(
                {k: v for k, v in value.items() if k != "installed_after"},
                {
                    "sentinel_survived": True,
                    "sentinel_removed": True,
                    "processes_stopped": True,
                    "owned_empty": True,
                    "emergency_cleanup": False,
                    "errors": [],
                },
            ),
            "campaign_cleanup",
        )


def evaluate(root: Path, expected: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Reject incomplete or transplanted bundles; never select claims from their reports."""
    return _evaluate(root, expected, manifest, "qualified_for_profile")


def finalize(root: Path, expected: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Publish qualification only after the entire pending bundle passes evaluation."""
    result = _evaluate(root, expected, manifest, "unqualified")
    summary = read(root / "summary.json")
    summary["outcome"] = "qualified_for_profile"
    write(root / "summary.json", summary)
    return result


def _evaluate(
    root: Path, expected: dict[str, Any], manifest: dict[str, Any], outcome: str
) -> dict[str, Any]:
    require(_same(expected, plan(manifest, expected["campaign_id"])), "plan_narrowed")
    require(
        not (root / "summary.json").is_symlink()
        and (root / "summary.json").resolve().parent == root.resolve(),
        "summary_escape",
    )
    require(
        {p.name for p in root.iterdir()} == {"summary.json", *(g + ".json" for g in GROUPS)},
        "bundle_inventory",
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
    try:
        stamps = [datetime.datetime.fromisoformat(summary[key]) for key in ("started", "ended")]
        require(
            all(stamp.utcoffset() == datetime.timedelta(0) for stamp in stamps), "campaign_timezone"
        )
    except ValueError as exc:
        raise Refused("campaign_timestamps") from exc
    require(
        isinstance(summary["attempts"], list) and len(summary["attempts"]) == len(GROUPS),
        "case_inventory",
    )
    seen: set[str] = set()
    total = max(
        (root / "summary.json").stat().st_size,
        len(encode({**summary, "outcome": "qualified_for_profile"})) + 1,
    )
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
    require(summary["outcome"] == outcome, "incomplete_summary")
    return {
        "outcome": "qualified_for_profile",
        "profile": PROFILE,
        "candidate_sha256": digest(manifest),
        "campaign_id": expected["campaign_id"],
        "groups": list(GROUPS),
    }
