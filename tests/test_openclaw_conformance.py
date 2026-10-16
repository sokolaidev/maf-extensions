"""Adversarial acceptance checks for candidate-bound qualification evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
sys.path.insert(0, str(ROOT / "scripts"))
e = importlib.import_module("openclaw_conformance.evidence")
install = importlib.import_module("openclaw_conformance.install")

transport = importlib.import_module("openclaw_published_transport_check")


def manifest():
    return {
        "schema": 1,
        "profile": e.PROFILE,
        "openclaw": "2026.9.7",
        "npm_integrity": e.INTEGRITY,
        "wheel_sha256": e.WHEEL,
        "versions": {"maf-sandbox-mcp": "0.1.0"},
        "python_lock_sha256": "a" * 64,
        "npm_lock_sha256": "b" * 64,
        "npm_tree": {"files": 1, "bytes": 1, "tree_sha256": "c" * 64},
        "worker_modules": {"server.py": "d" * 64},
        "image": "sha256:" + "e" * 64,
        "config_sha256": "f" * 64,
        "host": {"platform": "Windows"},
        "runner_sha256": "1" * 64,
        "runner_commit": "2" * 40,
    }


def valid_result(verdict="valid"):
    return {
        "completed": verdict != "incomplete",
        "verdict": None if verdict == "incomplete" else verdict,
        "status": "incomplete" if verdict == "incomplete" else "ok",
        "cleanup": "confirmed",
        "projected_content_items": 1,
        "source_sha256": hashlib.sha256(
            json.dumps(
                [
                    [
                        "main.bicep",
                        importlib.import_module("openclaw_gateway_provider").SOURCES[verdict],
                    ]
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "image": manifest()["image"],
        "config_sha256": manifest()["config_sha256"],
    }


def observations(m):
    names = list(e.TRANSPORT_CASES)
    retirements = [
        {
            "phase": phase,
            "sdk_finished": True,
            "retirement_finished": True,
            "transport_terminated": True,
            "get_active": False,
            "sdk_streams": 0,
            "request_ids": 0,
            "requests": 0,
        }
        for phase, count in (("saturation", 8), ("churn", 100), ("idle", 1))
        for _ in range(count)
    ]
    ownership: dict[str, dict[str, Any]] = {
        name: {"survivor": valid_result()} for name in e.OWNERSHIP
    }
    ownership["equal_ids_repeated_cancel"].update(matching_cancels=4, other_session_cancels=1)
    ownership["response_loss"].update(
        socket_closed=True,
        session="c" * 64,
        binding_result={
            "completed": False,
            "status": "incomplete",
            "source_sha256": e.LARGE_SOURCE_SHA256,
            "cleanup": "confirmed",
        },
    )
    ownership["maf_active_close"]["delete_observed"] = True
    ownership["maf_idle_close"]["delete_observed"] = True
    values = {
        "installed": {
            "versions": m["versions"],
            "wheel_sha256": e.WHEEL,
            "worker_modules": m["worker_modules"],
            "npm_tree": m["npm_tree"],
            "origins_verified": True,
            "assets_verified": True,
        },
        "discovery": {
            "imports": ["maf_sandbox_mcp.server", "maf_sandbox_mcp.workload_http"],
            "side_effect_attempts": [],
            "exit_code": 0,
            "late_guard_control": True,
        },
        "policy": dict.fromkeys(
            (
                "missing_config",
                "oversized_config",
                "mutable_image",
                "malformed_input",
                "no_allocation",
            ),
            True,
        ),
        "transport": {
            "evidence_classes": {
                name: "direct-ASGI" if name.split("[")[0] in transport.DIRECT_ASGI else "socket"
                for name in names
            },
            "cases": names,
            "phases": [
                {"case": name, "phase": phase, "outcome": "passed", "xfail": False, "seconds": 0.1}
                for name in names
                for phase in ("setup", "call", "teardown")
            ],
            "retirements": retirements,
            "final_state": {
                "adapter_records": 0,
                "body_readers": 0,
                "active_call": False,
                "poisoned": False,
                "default_manager_absent": True,
            },
        },
        "ownership": {
            "versions": m["versions"],
            "image": m["image"],
            "config_sha256": m["config_sha256"],
            "cases": ownership,
            "transport": [
                {"event": "binding_started", "session": "c" * 64},
                {"event": "request", "method": "tools/call", "session": "c" * 64},
                {
                    "event": "binding_result",
                    "session": "c" * 64,
                    **ownership["response_loss"]["binding_result"],
                },
            ],
            "shutdown": {
                "sessions": 0,
                "active": False,
                "poisoned": False,
                "owned_empty": True,
                "unrelated_preserved": True,
            },
            "sentinel_removed": True,
        },
        "gateway": {
            "session_ids": ["a" * 32, "b" * 32],
            "sessions": [
                {v: valid_result(v) for v in ("valid", "invalid", "incomplete")} for _ in range(2)
            ],
            "owned_empty": True,
            "entry_point": "maf-bicep-mcp",
            "prepared_module": {
                **valid_result(),
                "image": m["image"],
                "config_sha256": m["config_sha256"],
            },
            "processes_stopped": True,
            "owner_lock_released": True,
            "cleanup_errors": [],
        },
        "network": {
            "cleanup": True,
            "observations": [
                {"mode": "bridge", "exit_code": 0, "control_body_matches": True},
                {"mode": "none", "exit_code": 7, "control_body_matches": False},
            ],
        },
        "cleanup": {
            "sentinel_survived": True,
            "sentinel_removed": True,
            "processes_stopped": True,
            "owned_empty": True,
            "emergency_cleanup": False,
        },
    }

    values["cleanup"]["installed_after"] = copy.deepcopy(values["installed"])
    return values


@pytest.fixture
def bundle(tmp_path):
    m = manifest()
    plan = e.plan(m, uuid.uuid4().hex)
    summary = {
        "schema": 1,
        "campaign_id": plan["campaign_id"],
        "plan_sha256": e.digest(plan),
        "candidate_sha256": e.digest(m),
        "started": "2026-10-16T01:00:00+00:00",
        "ended": "2026-10-16T01:01:00+00:00",
        "duration": 60,
        "attempts": [],
        "outcome": "qualified_for_profile",
    }
    for group, obs in observations(m).items():
        aid = uuid.uuid4().hex
        artifact = {
            "campaign_id": plan["campaign_id"],
            "attempt_id": aid,
            "candidate_sha256": e.digest(m),
            "group": group,
            "observations": obs,
        }
        path = tmp_path / (group + ".json")
        path.write_bytes(e.encode(artifact))
        summary["attempts"].append(
            {
                "group": group,
                "attempt_id": aid,
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": e.file_hash(path),
                "outcome": "pass",
            }
        )
    e.write(tmp_path / "summary.json", summary)
    return tmp_path, plan, m


def change_artifact(bundle, group, mutate):
    root, _, _ = bundle
    path = root / (group + ".json")
    value = e.read(path)
    mutate(value)
    path.write_bytes(e.encode(value))
    summary = e.read(root / "summary.json")
    attempt = next(a for a in summary["attempts"] if a["group"] == group)
    attempt.update(bytes=path.stat().st_size, sha256=e.file_hash(path))
    e.write(root / "summary.json", summary)


def test_complete_fixture_qualifies_only_the_selected_profile(bundle):
    root, plan, m = bundle
    result = e.evaluate(root, plan, m)
    assert result["outcome"] == "qualified_for_profile"
    assert "complete_matrix" not in result


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["attempts"].pop(),
        lambda s: s["attempts"].append(s["attempts"][0]),
        lambda s: s["attempts"][1].update(attempt_id=s["attempts"][0]["attempt_id"]),
        lambda s: s.update(campaign_id="0" * 32),
        lambda s: s.update(candidate_sha256="0" * 64),
        lambda s: s.update(plan_sha256="0" * 64),
        lambda s: s.update(schema=2),
        lambda s: s.update(duration=-1),
        lambda s: s.update(duration=True),
        lambda s: s.update(outcome="unqualified"),
        lambda s: s["attempts"][0].update(outcome="fail"),
        lambda s: s["attempts"][0].update(file="../installed.json"),
        lambda s: s["attempts"][0].update(bytes=True),
        lambda s: s.update(extra="unreviewed"),
    ],
)
def test_summary_cannot_hide_missing_failed_or_transplanted_evidence(bundle, mutation):
    root, plan, m = bundle
    summary = e.read(root / "summary.json")
    mutation(summary)
    e.write(root / "summary.json", summary)
    with pytest.raises(e.Refused):
        e.evaluate(root, plan, m)


@pytest.mark.parametrize(
    "group,mutate",
    [
        ("installed", lambda v: v["observations"].update(origins_verified=False)),
        ("installed", lambda v: v["observations"].update(versions={})),
        ("installed", lambda v: v.update(candidate_sha256="0" * 64)),
        (
            "network",
            lambda v: v["observations"]["observations"][0].update(
                exit_code=7, control_body_matches=False
            ),
        ),
        (
            "network",
            lambda v: v["observations"]["observations"][1].update(
                exit_code=0, control_body_matches=True
            ),
        ),
        (
            "discovery",
            lambda v: v["observations"].update(side_effect_attempts=["subprocess.Popen"]),
        ),
        ("discovery", lambda v: v["observations"].update(late_guard_control=False)),
        ("gateway", lambda v: v["observations"]["sessions"].pop()),
        (
            "gateway",
            lambda v: v["observations"]["sessions"][0]["valid"].update(projected_content_items=2),
        ),
        ("gateway", lambda v: v["observations"].update(owner_lock_released=False)),
        ("ownership", lambda v: v["observations"]["shutdown"].update(unrelated_preserved=False)),
        ("transport", lambda v: v["observations"]["phases"].pop()),
        ("transport", lambda v: v["observations"]["phases"][0].update(outcome="skipped")),
        ("transport", lambda v: v["observations"]["retirements"].pop()),
        ("cleanup", lambda v: v["observations"].update(emergency_cleanup=True)),
        ("cleanup", lambda v: v.update(campaign_id="0" * 32)),
    ],
)
def test_rehashed_false_observations_still_fail_independent_predicates(bundle, group, mutate):
    change_artifact(bundle, group, mutate)
    root, plan, m = bundle
    with pytest.raises(e.Refused):
        e.evaluate(root, plan, m)


def test_profile_cannot_narrow_itself(bundle):
    root, plan, m = bundle
    plan = copy.deepcopy(plan)
    plan["groups"].remove("network")
    with pytest.raises(e.Refused, match="plan_narrowed"):
        e.evaluate(root, plan, m)


def test_corrupted_artifact_is_not_accepted(bundle):
    root, plan, m = bundle
    with (root / "network.json").open("ab") as f:
        f.write(b" ")
    with pytest.raises(e.Refused, match="artifact_identity"):
        e.evaluate(root, plan, m)


@pytest.mark.parametrize(
    "raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":', b"\xff"]
)
def test_malformed_json_is_refused(tmp_path, raw):
    p = tmp_path / "input.json"
    p.write_bytes(raw)
    with pytest.raises(e.Refused):
        e.read(p)


def test_bounded_reader_refuses_oversized_records(tmp_path):
    p = tmp_path / "input.json"
    p.write_bytes(b" " * 33)
    with pytest.raises(e.Refused):
        e.read(p, 32)


def test_environment_drops_import_overrides_and_user_model_credentials(tmp_path, monkeypatch):
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "OPENCLAW_CONFIG_PATH",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(key, "must-not-leak")
    env = install.environment(tmp_path)
    assert not any(v == "must-not-leak" for v in env.values())
    assert env["PYTHONNOUSERSITE"] == "1"


def test_candidate_refuses_unqualified_platform_and_artifacts():
    for key, value in (
        ("wheel_sha256", "0" * 64),
        ("openclaw", "2026.10.3"),
        ("profile", "narrow"),
    ):
        m = manifest()
        m[key] = value
        with pytest.raises(e.Refused):
            e.candidate(m)
    m = manifest()
    m["host"]["platform"] = "Linux"
    with pytest.raises(e.Refused):
        e.candidate(m)


@pytest.mark.parametrize(
    "payload",
    [
        "import subprocess; subprocess.run([sys.executable, '-c', 'pass'])",
        "import threading,time\ndef delayed():\n time.sleep(0.01)\n try:\n  import subprocess; subprocess.run([sys.executable,'-c','pass'])\n except RuntimeError: pass\nthreading.Thread(target=delayed).start()",
    ],
)
def test_discovery_refuses_immediate_and_delayed_effects(tmp_path, monkeypatch, payload):
    checks = importlib.import_module("openclaw_conformance.checks")
    monkeypatch.setattr(
        checks,
        "DISCOVERY",
        checks.DISCOVERY.replace(
            "import maf_sandbox_mcp.server,maf_sandbox_mcp.workload_http", payload
        ),
    )
    with pytest.raises(e.Refused):
        checks.discovery(tmp_path)


def test_discovery_late_control_is_measured_after_report(tmp_path, monkeypatch):
    checks = importlib.import_module("openclaw_conformance.checks")
    monkeypatch.setattr(
        checks,
        "DISCOVERY",
        checks.DISCOVERY.replace(
            "import maf_sandbox_mcp.server,maf_sandbox_mcp.workload_http", "pass"
        ),
    )
    assert checks.discovery(tmp_path)["late_guard_control"] is True


def test_explicit_manifest_rejects_extra_installed_dependency(monkeypatch):
    from types import SimpleNamespace

    checker = importlib.import_module("openclaw_installed_worker_check")
    dist = SimpleNamespace(metadata={"Name": "unexpected"}, version="1.0", read_text=lambda _: None)
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [dist])
    with pytest.raises(RuntimeError, match="baseline"):
        checker.installed_environment({"expected": "1.0"})


def test_explicit_manifest_does_not_change_historical_default(monkeypatch):
    checker = importlib.import_module("openclaw_installed_worker_check")
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(checker, "MODULES", {})
    assert checker.installed_environment({})["versions"] == {}
    with pytest.raises(RuntimeError, match="baseline"):
        checker.installed_environment()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended path syntax")
def test_windows_extended_path_keeps_package_containment(tmp_path):
    target = tmp_path / "package" / "nested" / "asset.js"
    target.parent.mkdir(parents=True)
    target.write_text("asset", encoding="utf-8")
    extended = Path("\\\\?\\" + str(target.resolve()))
    normalized = install._without_extended_prefix(extended)
    assert normalized.is_relative_to(tmp_path.resolve())
    assert normalized == target.resolve()
    assert e.file_hash(extended) == e.file_hash(target)


def test_preflight_failure_retains_an_incomplete_campaign(tmp_path, monkeypatch):
    runner = importlib.import_module("openclaw_conformance.__main__")
    m = manifest()
    expected = tmp_path / "candidate.json"
    e.write(expected, m)

    def refuse(*args):
        raise e.Refused("runner_changed")

    monkeypatch.setattr(runner, "check_prepared", refuse)
    monkeypatch.setattr(
        runner, "command", lambda *args: pytest.fail("preflight started Docker work")
    )
    output = tmp_path / "campaign"
    report = runner.run(tmp_path / "prepared", expected, output)
    assert report["outcome"] == "failed"
    summary = e.read(output / "evidence/summary.json")
    assert summary["outcome"] == "failed"
    assert summary["attempts"][0]["outcome"] == "fail"
    with pytest.raises(e.Refused):
        e.evaluate(output / "evidence", e.read(output / "plan.json"), m)


def test_existing_output_is_never_reused(tmp_path):
    runner = importlib.import_module("openclaw_conformance.__main__")
    with pytest.raises(e.Refused, match="fresh_external"):
        runner.run(tmp_path / "prepared", tmp_path / "missing.json", tmp_path)


@pytest.fixture
def npm_artifact(tmp_path, monkeypatch):
    import base64
    import io
    import tarfile

    archive = tmp_path / "package.tgz"
    package = tmp_path / "installed"
    package.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (
            ("runtime.js", b"export const version=1;"),
            (".openclaw-lifecycle-pending", b"pending\n"),
        ):
            member = tarfile.TarInfo("package/" + name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
            (package / name).write_bytes(data)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    monkeypatch.setattr(install, "INTEGRITY", integrity)
    return archive, package


def test_npm_install_transition_verifies_runtime_bytes(npm_artifact):
    archive, package = npm_artifact
    before = install.npm_tree(archive, package, completed=False)
    (package / ".openclaw-lifecycle-pending").unlink()
    after = install.npm_tree(archive, package)
    assert before["tree_sha256"] == after["tree_sha256"]
    assert before["files"] == after["files"] + 1
    assert after["lifecycle_marker"] == "removed_by_pinned_postinstall"
    (package / "runtime.js").write_bytes(b"tampered")
    with pytest.raises(e.Refused, match="installed_npm_bytes"):
        install.npm_tree(archive, package)


def test_npm_unfinished_lifecycle_cannot_qualify(npm_artifact):
    archive, package = npm_artifact
    with pytest.raises(e.Refused, match="lifecycle_not_completed"):
        install.npm_tree(archive, package)


def test_npm_missing_runtime_is_not_an_allowed_installer_mutation(npm_artifact):
    archive, package = npm_artifact
    (package / ".openclaw-lifecycle-pending").unlink()
    (package / "runtime.js").unlink()
    with pytest.raises(FileNotFoundError):
        install.npm_tree(archive, package)


@pytest.mark.parametrize(
    "group,field", [("cleanup", "sentinel_survived"), ("discovery", "late_guard_control")]
)
def test_numeric_boolean_standins_are_rejected(bundle, group, field):
    change_artifact(bundle, group, lambda v: v["observations"].update({field: 1}))
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_transport_parameter_identity_cannot_be_replaced(bundle):
    def change(value):
        obs = value["observations"]
        old = next(name for name in obs["cases"] if "[" in name)
        new = old.split("[")[0] + "[omitted-original]"
        obs["cases"][obs["cases"].index(old)] = new
        for row in obs["phases"]:
            if row["case"] == old:
                row["case"] = new
        obs["evidence_classes"][new] = obs["evidence_classes"].pop(old)

    change_artifact(bundle, "transport", change)
    with pytest.raises(e.Refused, match="transport_case_identity"):
        e.evaluate(*bundle)


def test_after_campaign_worker_mutation_cannot_qualify(bundle):
    change_artifact(
        bundle, "cleanup", lambda v: v["observations"]["installed_after"].update(worker_modules={})
    )
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_uninventoried_bundle_files_are_rejected(bundle):
    root, _, _ = bundle
    (root / "extra-private.log").write_text("not an evidence artifact")
    with pytest.raises(e.Refused, match="bundle_inventory"):
        e.evaluate(*bundle)


def test_response_loss_allows_an_explicit_incomplete_result(bundle):
    assert e.evaluate(*bundle)["outcome"] == "qualified_for_profile"
    change_artifact(
        bundle,
        "ownership",
        lambda v: v["observations"]["cases"]["response_loss"]["binding_result"].update(status="ok"),
    )
    with pytest.raises(e.Refused, match="response_loss"):
        e.evaluate(*bundle)


@pytest.mark.parametrize(
    "row",
    [
        {"event": "binding_started"},
        {"event": "request", "method": "tools/call"},
        {"event": "binding_cancelled"},
        {"event": "request", "method": "notifications/cancelled"},
    ],
)
def test_response_loss_cannot_hide_replay_or_implicit_cancel(bundle, row):
    change_artifact(
        bundle,
        "ownership",
        lambda v: v["observations"]["transport"].append({**row, "session": "c" * 64}),
    )
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)
