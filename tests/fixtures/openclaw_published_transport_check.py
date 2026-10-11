# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "agent-framework-core==1.19.0",
#   "maf-sandbox==0.46.0",
#   "maf-sandbox-bicep==0.22.0",
#   "maf-sandbox-docker==0.24.4",
#   "mcp==1.28.1",
#   "httpx==0.28.1",
#   "jsonschema==4.26.0",
#   "starlette==1.7.0",
#   "uvicorn==0.54.0",
#   "pytest==9.0.3",
# ]
# ///
"""Run the finite HTTP acceptance subset using only published Python dependencies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory

BASELINE = {
    "agent-framework-core": "1.19.0",
    "maf-sandbox": "0.46.0",
    "maf-sandbox-bicep": "0.22.0",
    "maf-sandbox-docker": "0.24.4",
    "mcp": "1.28.1",
    "httpx": "0.28.1",
    "jsonschema": "4.26.0",
    "starlette": "1.7.0",
    "uvicorn": "0.54.0",
    "pytest": "9.0.3",
}
# Counts include each declared parametrization; skips never satisfy acceptance.
CASES = {
    "test_two_maf_clients_discover_distinct_bindings_and_close_independently": 1,
    "test_shared_admission_equal_ids_cancel_and_delete_drain": 1,
    "test_registry_saturation_churn_and_idle_expiry": 1,
    "test_authentication_and_host_refusal_precede_allocation": 3,
    "test_bad_initialization_input_and_output_bounds": 1,
    "test_disconnect_does_not_cancel_and_idle_close_preserves_other_call": 1,
    "test_get_stream_limit_and_shutdown_retirement": 1,
    "test_shutdown_drains_sessions_already_retiring": 1,
    "test_retirement_failure_retains_ownership_until_retry_settles": 2,
    "test_slow_initialize_cannot_allocate_after_shutdown_closes_admission": 1,
    "test_shutdown_waits_for_transport_startup_settlement": 2,
    "test_initialized_ack_waits_for_sdk_dispatch": 1,
    "test_actual_chunked_body_header_and_slow_body_limits": 1,
    "test_actual_connection_cap_includes_incomplete_requests": 1,
    "test_body_reader_budget_released_after_rejection": 1,
    "test_initialization_reservations_are_atomic_and_count_toward_cap": 1,
    "test_idle_expiry_does_not_evict_active_call_and_restart_rejects_old_id": 1,
    "test_incomplete_headers_have_a_real_connection_deadline": 1,
    "test_delete_can_retire_session_before_initialized_notification": 1,
    "test_pending_post_retains_session_reservation": 3,
    "test_pending_post_releases_reservation_on_body_failure": 4,
    "test_nonfinite_json_is_rejected_before_dispatch": 10,
    "test_typed_request_ids_keep_responses_and_cancellation_independent": 2,
    "test_client_request_ids_cannot_replace_sdk_get_stream": 1,
}
DIRECT_ASGI = {
    "test_body_reader_budget_released_after_rejection",
    "test_pending_post_releases_reservation_on_body_failure",
}
POLICY_OVERRIDES = {
    "test_actual_chunked_body_header_and_slow_body_limits": {"body_seconds": 0.02},
    "test_incomplete_headers_have_a_real_connection_deadline": {"header_seconds": 0.05},
    "test_pending_post_retains_session_reservation[idle]": {"idle_seconds": 0.02},
    "test_registry_saturation_churn_and_idle_expiry": {"age_idle_record_by": "IDLE_SECONDS"},
    "test_idle_expiry_does_not_evict_active_call_and_restart_rejects_old_id": {
        "age_idle_and_active_records_by": "IDLE_SECONDS"
    },
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def environment():
    manifest = {}
    roots = {}
    for dist in distributions():
        name = dist.metadata["Name"].lower().replace("_", "-")
        require(name not in manifest, "Duplicate installed distribution")
        require(
            dist.read_text("direct_url.json") is None, "Direct URL/local installations are refused"
        )
        manifest[name] = dist.version
        roots[name] = Path(str(dist.locate_file(""))).resolve()
    require(all(manifest.get(k) == v for k, v in BASELINE.items()), "Published baseline differs")
    for distribution, module in {
        "agent-framework-core": "agent_framework",
        "maf-sandbox": "maf_sandbox",
        "maf-sandbox-bicep": "maf_sandbox_bicep",
        "maf-sandbox-docker": "maf_sandbox_docker",
        "mcp": "mcp",
    }.items():
        spec = importlib.util.find_spec(module)
        require(
            spec is not None
            and spec.origin is not None
            and Path(spec.origin).resolve().is_relative_to(roots[distribution]),
            "Imported package is outside its installed distribution",
        )
    return dict(sorted(manifest.items()))


def verify_retirements(rows):
    require(isinstance(rows, list), "Missing SDK retirement observations")
    require(
        Counter(r.get("phase") for r in rows) == {"saturation": 8, "churn": 100, "idle": 1},
        "Missing or duplicated session cycles",
    )
    for row in rows:
        require(
            all(
                row.get(k) is True
                for k in ("sdk_finished", "retirement_finished", "transport_terminated")
            ),
            "SDK retirement did not finish",
        )
        require(row.get("get_active") is False, "GET handler survived retirement")
        require(
            all(
                type(row.get(k)) is int and row[k] == 0
                for k in ("sdk_streams", "request_ids", "requests")
            ),
            "SDK stream/request state survived retirement",
        )


class Results:
    def __init__(self):
        self.collected = []
        self.reports = []
        self.retirements = None
        self.final_state = None

    def pytest_collection_finish(self, session):
        self.collected = [item.name for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.append(
            {
                "case": report.nodeid.split("::")[-1],
                "phase": report.when,
                "outcome": report.outcome,
                "xfail": hasattr(report, "wasxfail"),
                "seconds": report.duration,
            }
        )
        if report.when == "call":
            for key, value in report.user_properties:
                if key == "retirement_observations":
                    require(self.retirements is None, "Duplicated retirement observations")
                    self.retirements = value
                elif key == "retirement_final_state":
                    require(self.final_state is None, "Duplicated final state")
                    self.final_state = value

    def verify(self, exit_code):
        require(exit_code == 0, "Required transport tests did not pass")
        require(
            Counter(n.split("[")[0] for n in self.collected) == CASES,
            "Required cases were missing, duplicated or changed",
        )
        require(len(set(self.collected)) == len(self.collected), "Duplicated test identity")
        require(
            Counter((r["case"], r["phase"]) for r in self.reports)
            == Counter(
                (name, phase) for name in self.collected for phase in ("setup", "call", "teardown")
            ),
            "Incomplete test-phase evidence",
        )
        require(
            all(r["outcome"] == "passed" and not r["xfail"] for r in self.reports),
            "Skipped, expected-failed or failed cases cannot satisfy acceptance",
        )
        verify_retirements(self.retirements)
        if not isinstance(self.final_state, dict):
            raise RuntimeError("Missing final state")
        require(
            all(
                type(self.final_state.get(k)) is int and self.final_state[k] == 0
                for k in ("adapter_records", "body_readers")
            ),
            "Adapter state survived",
        )
        require(
            self.final_state.get("active_call") is False
            and self.final_state.get("poisoned") is False
            and self.final_state.get("default_manager_absent") is True,
            "Service state did not settle",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    root = args.root.resolve()
    require(not root.is_relative_to(repo), "Evidence must be outside the checkout")
    root.mkdir(parents=True, exist_ok=True)
    success = root / "transport-report.json"
    success.unlink(missing_ok=True)
    require(not (root / "attempt.json").exists(), "Use a fresh private evidence directory")
    require(sys.flags.optimize == 0, "Python assertions must be enabled")
    require(
        not subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"], text=True
        ).strip(),
        "Qualification requires a clean candidate",
    )
    candidate = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = environment()
    sources = [
        Path(__file__),
        repo / "tests/test_mcp_workload_service.py",
        *sorted((repo / "samples/experimental/openclaw_bicep").glob("*.py")),
    ]
    hashes = {
        p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
    }
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    import pytest

    results = Results()
    started = time.monotonic()
    with TemporaryDirectory(prefix="mcp-transport-pytest-") as tmp:
        config = Path(tmp) / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        exit_code = pytest.main(
            [
                "-c",
                str(config),
                "--rootdir",
                str(repo),
                "--noconftest",
                "-p",
                "no:cacheprovider",
                "-q",
                *[f"{repo / 'tests/test_mcp_workload_service.py'}::{name}" for name in CASES],
            ],
            plugins=[results],
        )
    report = {
        "candidate": candidate,
        "platform": {"system": platform.system(), "python": platform.python_version()},
        "distributions": manifest,
        "distribution_sha256": digest(manifest),
        "source_sha256": hashes,
        "selected_cases": CASES,
        "collected": results.collected,
        "reports": results.reports,
        "retirements": results.retirements,
        "final_state": results.final_state,
        "direct_asgi_cases": sorted(DIRECT_ASGI),
        "timing_overrides": POLICY_OVERRIDES,
        "fixture_controls": "Synthetic bindings, existing event barriers and injected retirement/startup failures; no Docker or Gateway",
        "pytest_isolation": "No conftest, external plugins, addopts or repository pytest configuration",
        "exit_code": int(exit_code),
        "elapsed_seconds": time.monotonic() - started,
    }
    (root / "attempt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    results.verify(exit_code)
    require(
        all(hashlib.sha256((repo / p).read_bytes()).hexdigest() == h for p, h in hashes.items()),
        "Qualification sources changed during execution",
    )
    require(
        not subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"], text=True
        ).strip(),
        "Checkout changed during execution",
    )
    report["passed"] = True
    success.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "cases": len(results.collected),
                "sessions": len(results.retirements or []),
                "candidate": candidate,
            }
        )
    )


if __name__ == "__main__":
    main()
