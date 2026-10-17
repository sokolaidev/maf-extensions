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
        "runtime_assets": {
            "manifest_sha256": "d" * 64,
            "tree_sha256": "d" * 64,
            "files": 3,
            "archives": 2,
            "archive_verified_files": 3,
            "generated_files": [],
        },
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


def control_events(cases):
    events = []

    def add(**fields):
        event = {"time_ns": len(events) + 1, **fields}
        events.append(event)
        return event

    for name, case in sorted(cases.items()):
        a = "c" * 64 if name == "response_loss" else e.digest(name + "a")
        b = e.digest(name + "b")
        case.update(session_a=a, session_b=b, container=e.digest(name))
        active = b if name == "maf_idle_close" else a
        request_id = e.digest(1 if name.startswith("maf_") else 42)
        if name == "maf_ordinary_close":
            for sid in (a, b):
                exchange = e.digest(name + sid + "preclose")
                add(
                    event="request",
                    method="tools/call",
                    session=sid,
                    request=request_id,
                    exchange=exchange,
                )
                add(event="binding_started", session=sid, request=request_id)
                add(event="binding_result", session=sid, request=request_id, **case["survivor"])
                add(event="response", session=sid, exchange=exchange, status=200)
        else:
            add(
                event="request",
                method="tools/call",
                session=active,
                request=request_id,
                exchange=e.digest(name + "active"),
            )
            add(event="binding_started", session=active, request=request_id)
            for i in range(5 if name == "equal_ids_repeated_cancel" else 1):
                exchange = e.digest(name + str(i))
                add(
                    event="request",
                    method="tools/call",
                    session=b,
                    request=e.digest(42),
                    exchange=exchange,
                )
                add(event="response", session=b, exchange=exchange, status=200)
                add(
                    event="admission_refused",
                    session=b,
                    request=e.digest(42),
                    exchange=exchange,
                    active_session=active,
                    active_request=request_id,
                    container=case["container"],
                    owned_count=1,
                    active_pending=True,
                )
        if name == "equal_ids_repeated_cancel":
            for sid, count in ((a, 4), (b, 1)):
                for i in range(count):
                    exchange = e.digest(sid + str(i))
                    add(
                        event="request",
                        method="notifications/cancelled",
                        session=sid,
                        target=e.digest(42),
                        exchange=exchange,
                    )
                    add(event="response", session=sid, exchange=exchange, status=202)
        if name in ("response_loss", "maf_idle_close"):
            add(
                event="binding_result",
                session=active,
                request=request_id,
                completed=False,
                status="incomplete",
                cleanup="confirmed",
                source_sha256=e.LARGE_SOURCE_SHA256,
            )
        if name in ("equal_ids_repeated_cancel", "maf_active_close"):
            add(event="binding_cancelled", session=active, request=request_id)
        if name == "maf_idle_close":
            add(event="response", session=active, exchange=e.digest(name + "active"), status=200)
        survivor_request = e.digest(
            42 if name in ("equal_ids_repeated_cancel", "response_loss") else 2
        )
        add(
            event="request",
            method="tools/call",
            session=b,
            request=survivor_request,
            exchange=e.digest(name + "survivor"),
        )
        add(event="binding_started", session=b, request=survivor_request)
        add(event="binding_result", session=b, request=survivor_request, **case["survivor"])
        add(event="response", session=b, exchange=e.digest(name + "survivor"), status=200)
        for sid in (a, b):
            exchange = e.digest(sid + "delete")
            add(event="delete_requested", session=sid, exchange=exchange)
            add(
                event="retired",
                session=sid,
                exchange=exchange,
                completed=True,
                sdk_finished=True,
                sdk_terminated=True,
                sdk_streams=0,
                request_ids=0,
                requests=0,
                session_registered=False,
                poisoned=False,
            )
        if name.startswith("maf_"):
            retirement = [
                event
                for event in events
                if event.get("session") == a and event["event"] in ("delete_requested", "retired")
            ]
            for event in retirement:
                events.remove(event)
            position = next(
                i
                for i, event in enumerate(events)
                if event.get("session") == b
                and event.get("request") == survivor_request
                and event["event"] == "request"
            )
            events[position:position] = retirement
            if name in ("maf_active_close", "maf_idle_close"):
                early = retirement if name == "maf_idle_close" else retirement[:1]
                for event in early:
                    events.remove(event)
                position = next(
                    i
                    for i, event in enumerate(events)
                    if event["event"] == "request"
                    and event.get("session") == b
                    and event.get("exchange") == e.digest(name + "0")
                )
                events[position:position] = early
    for index, event in enumerate(events):
        event["time_ns"] = index + 1
    return events


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
        name: {
            "survivor": {
                **valid_result(),
                "source_sha256": importlib.import_module("openclaw_sources").source_digest(
                    importlib.import_module("openclaw_sources").SIMPLE_SOURCE
                ),
            }
        }
        for name in e.OWNERSHIP
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
    ownership["maf_idle_close"].update(
        delete_observed=True, call_raised=False, call_error_type=None, call_error_code=None
    )
    ownership_events = control_events(ownership)
    values = {
        "installed": {
            "versions": m["versions"],
            "wheel_sha256": e.WHEEL,
            "worker_modules": m["worker_modules"],
            "npm_tree": m["npm_tree"],
            "origins_verified": True,
            "assets_verified": True,
            "runtime_assets": m["runtime_assets"],
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
            "transport": ownership_events,
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
                "source_sha256": importlib.import_module("openclaw_sources").source_digest(
                    importlib.import_module("openclaw_sources").PREPARED_SOURCE
                ),
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
            "errors": [],
        },
    }

    gateway = values["gateway"]
    gateway["requests"] = []
    gateway["provider_observations"] = []
    for session, outcomes in zip(gateway["session_ids"], gateway["sessions"], strict=True):
        for case, row in outcomes.items():
            turn = e.digest(session + case)[:32]
            observation = {"session": session, "turn": turn, "scenario": case}
            gateway["requests"].append(observation.copy())
            gateway["provider_observations"].extend(
                [
                    observation.copy(),
                    {
                        **observation,
                        "tool_result": json.dumps(
                            {
                                "result": {
                                    "content": [{"type": "text", "text": "result"}],
                                    "details": {"structuredContent": row.copy()},
                                }
                            }
                        ),
                    },
                ]
            )
            row.update(session=session, turn=turn)
    for row in values["network"]["observations"]:
        row["image"] = m["image"]
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


def test_response_loss_identity_matches_the_real_ownership_source():
    ownership = importlib.import_module("openclaw_bicep_ownership_check")
    from maf_sandbox_mcp import server

    assert server.snapshot(ownership.LARGE).digest == e.LARGE_SOURCE_SHA256


@pytest.mark.parametrize("group", ["installed", "ownership", "gateway"])
def test_boolean_counts_cannot_qualify(bundle, group):
    def change(value):
        obs = value["observations"]
        if group == "installed":
            obs["npm_tree"]["files"] = True
        elif group == "ownership":
            obs["cases"]["equal_ids_repeated_cancel"]["other_session_cancels"] = True
        else:
            obs["sessions"][0]["valid"]["projected_content_items"] = True

    change_artifact(bundle, group, change)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_retained_v1_campaign_does_not_establish_the_reviewed_profile():
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows"
    with pytest.raises(e.Refused):
        e.evaluate(
            record / "evidence", e.read(record / "plan.json"), e.read(record / "candidate.json")
        )


@pytest.mark.parametrize("target", ["survivor", "gateway_image", "gateway_config", "prepared"])
@pytest.mark.parametrize("missing", [False, True])
def test_review_requires_every_compiler_identity(bundle, target, missing):
    def change(value):
        obs = value["observations"]
        if target == "survivor":
            row, field = obs["cases"]["maf_ordinary_close"]["survivor"], "source_sha256"
        elif target == "prepared":
            row, field = obs["prepared_module"], "source_sha256"
        else:
            row = obs["sessions"][1]["incomplete"]
            field = "image" if target == "gateway_image" else "config_sha256"
        if missing:
            row.pop(field, None)
        else:
            row[field] = "0" * 64

    change_artifact(bundle, "ownership" if target == "survivor" else "gateway", change)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_review_idle_close_cannot_interrupt_peer(bundle):
    change_artifact(
        bundle,
        "ownership",
        lambda v: v["observations"]["cases"]["maf_idle_close"].update(call_raised=True),
    )
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_review_ownership_cannot_omit_non_loss_controls(bundle):
    def change(value):
        obs = value["observations"]
        loss = obs["cases"]["response_loss"]["session"]
        obs["transport"] = [x for x in obs["transport"] if x.get("session") == loss]

    change_artifact(bundle, "ownership", change)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize(
    "event", ["admission_refused", "retired", "delete_requested", "response", "binding_cancelled"]
)
def test_review_controls_require_each_correlated_event(bundle, event):
    change_artifact(
        bundle,
        "ownership",
        lambda v: v["observations"].update(
            transport=[r for r in v["observations"]["transport"] if r.get("event") != event]
        ),
    )
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize(
    "field,value",
    [
        ("active_session", "0" * 64),
        ("active_request", "0" * 64),
        ("exchange", "missing"),
        ("owned_count", True),
        ("active_pending", False),
    ],
)
def test_review_admission_cannot_substitute_another_call(bundle, field, value):
    def mutate(record):
        event = next(
            r for r in record["observations"]["transport"] if r.get("event") == "admission_refused"
        )
        event[field] = value

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_prepared_source_matches_the_real_snapshot():
    from maf_sandbox_mcp import server

    sources = importlib.import_module("openclaw_sources")
    assert server.snapshot(
        {"files": [{"path": "main.bicep", "content": sources.PREPARED_SOURCE}]}
    ).digest == sources.source_digest(sources.PREPARED_SOURCE)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP))
@pytest.mark.parametrize("missing", [False, True])
def test_each_ownership_survivor_requires_the_simple_source(bundle, case, missing):
    def mutate(record):
        result = record["observations"]["cases"][case]["survivor"]
        if missing:
            result.pop("source_sha256")
        else:
            result["source_sha256"] = "0" * 64

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("session", [0, 1])
@pytest.mark.parametrize("case", ["valid", "invalid", "incomplete"])
@pytest.mark.parametrize("field", ["image", "config_sha256"])
@pytest.mark.parametrize("missing", [False, True])
def test_each_gateway_outcome_requires_its_own_compiler_identity(
    bundle, session, case, field, missing
):
    def mutate(record):
        result = record["observations"]["sessions"][session][case]
        if missing:
            result.pop(field)
        else:
            result[field] = "0" * 64

    change_artifact(bundle, "gateway", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("replacement", [None, "unhashed"])
def test_active_close_cannot_omit_or_malformed_its_request_reference(bundle, replacement):
    def mutate(record):
        value = record["observations"]
        sid = value["cases"]["maf_active_close"]["session_a"]
        for row in value["transport"]:
            if row.get("session") == sid and "request" in row:
                row["request"] = replacement
            if row.get("active_session") == sid:
                row["active_request"] = replacement

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_survivor_event_does_not_accept_a_numeric_boolean(bundle):
    def mutate(record):
        value = record["observations"]
        sid = value["cases"]["maf_ordinary_close"]["session_b"]
        rows = [
            r
            for r in value["transport"]
            if r.get("session") == sid and r.get("event") == "binding_result"
        ]
        rows[-1]["completed"] = 1

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_retained_v2_campaign_does_not_establish_the_reviewed_profile():
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-16-windows-v2"
    with pytest.raises(e.Refused, match="unsupported_profile"):
        e.evaluate(
            record / "evidence", e.read(record / "plan.json"), e.read(record / "candidate.json")
        )


@pytest.mark.parametrize("mode", ["bridge", "none"])
@pytest.mark.parametrize("missing", [False, True])
def test_network_controls_require_candidate_image(bundle, mode, missing):
    def mutate(v):
        row = next(r for r in v["observations"]["observations"] if r["mode"] == mode)
        if missing:
            row.pop("image", None)
        else:
            row["image"] = "sha256:" + "0" * 64

    change_artifact(bundle, "network", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("mutation", ["sessions", "results"])
def test_gateway_cannot_substitute_sessions_or_reuse_results(bundle, mutation):
    def mutate(v):
        value = v["observations"]
        if mutation == "sessions":
            value["session_ids"] = ["c" * 32, "d" * 32]
        else:
            value["sessions"][1] = copy.deepcopy(value["sessions"][0])

    change_artifact(bundle, "gateway", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("event", ["cancel", "admission_refused"])
def test_repeated_controls_require_distinct_exchanges(bundle, event):
    def mutate(v):
        obs = v["observations"]
        case = obs["cases"]["equal_ids_repeated_cancel"]
        matches = (
            [
                r
                for r in obs["transport"]
                if (
                    r.get("event") == "request"
                    and r.get("method") == "notifications/cancelled"
                    and r.get("session") == case["session_a"]
                )
            ]
            if event == "cancel"
            else [
                r
                for r in obs["transport"]
                if r.get("event") == event and r.get("session") == case["session_b"]
            ]
        )
        for row in matches[1:]:
            row.clear()
            row.update(copy.deepcopy(matches[0]))

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
def test_json_overflow_is_refused_even_in_unused_observations(tmp_path, number):
    path = tmp_path / "record.json"
    path.write_text('{"unused":[' + number + "]}", encoding="utf-8")
    with pytest.raises(e.Refused, match="nonfinite_json"):
        e.read(path)


def test_final_evaluation_failure_never_persists_qualification(tmp_path, monkeypatch):
    runner = importlib.import_module("openclaw_conformance.__main__")
    m = manifest()
    candidate_path = tmp_path / "candidate.json"
    e.write(candidate_path, m)
    values = observations(m)
    monkeypatch.setattr(runner, "check_prepared", lambda *a: None)
    monkeypatch.setattr(runner, "command", lambda *a, **kw: "")
    monkeypatch.setattr(runner, "stage", lambda group, *a: copy.deepcopy(values[group]))
    monkeypatch.setattr(runner, "cleanup", lambda *a: copy.deepcopy(values["cleanup"]))
    monkeypatch.setitem(e.LIMITS, "bundle", 1)
    output = tmp_path / "campaign"
    with pytest.raises(e.Refused, match="bundle_limit"):
        runner.run(tmp_path / "prepared", candidate_path, output)
    assert e.read(output / "evidence/summary.json")["outcome"] == "unqualified"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_turn",
        "duplicate_turn",
        "session",
        "scenario",
        "duplicate_provider",
        "request",
        "projection",
    ],
)
def test_gateway_correlates_retained_provider_records(bundle, mutation):
    def mutate(record):
        value = record["observations"]
        row = value["sessions"][0]["valid"]
        if mutation == "missing_turn":
            row.pop("turn")
        elif mutation == "duplicate_turn":
            value["sessions"][1]["valid"]["turn"] = row["turn"]
        elif mutation in {"session", "scenario"}:
            value["provider_observations"][0][mutation] = "different"
        elif mutation == "duplicate_provider":
            value["provider_observations"][1] = copy.deepcopy(value["provider_observations"][0])
        elif mutation == "request":
            value["requests"][0]["session"] = "different"
        else:
            value["provider_observations"][1]["tool_result"] = "{}"

    change_artifact(bundle, "gateway", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("mutation", ["content", "envelope_error", "result_error", "details_error"])
def test_gateway_refuses_malformed_or_error_projection(bundle, mutation):
    def mutate(record):
        row = next(r for r in record["observations"]["provider_observations"] if "tool_result" in r)
        value = json.loads(row["tool_result"])
        if mutation == "content":
            value["result"]["content"] = "x"
        elif mutation == "envelope_error":
            value["status"] = "error"
        elif mutation == "result_error":
            value["result"]["isError"] = True
        else:
            value["result"]["details"]["status"] = "error"
        row["tool_result"] = json.dumps(value)

    change_artifact(bundle, "gateway", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("case", ["valid", "invalid", "incomplete"])
@pytest.mark.parametrize("mutation", ["content", "envelope_error", "result_error", "details_error"])
def test_retained_gateway_rejects_malformed_or_error_projection(case, mutation):
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4"
    obs = e.read(record / "evidence/gateway.json")["observations"]
    row = next(
        r for r in obs["provider_observations"] if "tool_result" in r and r["scenario"] == case
    )
    text = row["tool_result"]
    start, end = text.index("{"), text.rindex("}") + 1
    value = json.loads(text[start:end])
    if mutation == "content":
        value["result"]["content"] = "x"
    elif mutation == "envelope_error":
        value["status"] = "error"
    elif mutation == "result_error":
        value["result"]["isError"] = True
    else:
        value["result"]["details"]["status"] = "error"
    row["tool_result"] = text[:start] + json.dumps(value) + text[end:]
    with pytest.raises(RuntimeError):
        importlib.import_module("openclaw_conformance.gateway").verify(
            obs, e.read(record / "candidate.json")
        )


def test_finalize_publishes_only_a_fully_evaluated_bundle(bundle):
    root, expected, m = bundle
    summary = e.read(root / "summary.json")
    summary["outcome"] = "unqualified"
    e.write(root / "summary.json", summary)
    assert e.finalize(root, expected, m) == e.evaluate(root, expected, m)
    assert e.read(root / "summary.json")["outcome"] == "qualified_for_profile"


def test_finalize_accounts_for_the_larger_qualified_summary(bundle, monkeypatch):
    root, expected, m = bundle
    summary = e.read(root / "summary.json")
    summary["outcome"] = "unqualified"
    e.write(root / "summary.json", summary)
    monkeypatch.setitem(e.LIMITS, "bundle", sum(p.stat().st_size for p in root.iterdir()))
    summary["plan_sha256"] = e.digest(expected)
    e.write(root / "summary.json", summary)
    with pytest.raises(e.Refused, match="bundle_limit"):
        e.finalize(root, expected, m)
    assert e.read(root / "summary.json")["outcome"] == "unqualified"


def test_retained_v3_campaign_does_not_establish_the_reviewed_profile():
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v3"
    with pytest.raises(e.Refused, match="unsupported_profile"):
        e.evaluate(
            record / "evidence", e.read(record / "plan.json"), e.read(record / "candidate.json")
        )


@pytest.mark.parametrize(
    "payload",
    [
        '"unused":1,"unused":2',
        '"unused":NaN',
        '"unused":Infinity',
        '"unused":-Infinity',
        '"unused":1e400',
        '"unused":-1e400',
    ],
)
def test_gateway_refuses_malformed_embedded_json(bundle, payload):
    def mutate(record):
        row = next(r for r in record["observations"]["provider_observations"] if "tool_result" in r)
        text = row["tool_result"]
        start = text.index("{")
        row["tool_result"] = text[: start + 1] + payload + "," + text[start + 1 :]

    change_artifact(bundle, "gateway", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP - {"maf_ordinary_close"}))
def test_active_binding_requires_its_transport_request(bundle, case):
    def mutate(record):
        obs = record["observations"]
        sid = obs["cases"][case]["session_b" if case == "maf_idle_close" else "session_a"]
        start = next(
            r for r in obs["transport"] if r["event"] == "binding_started" and r["session"] == sid
        )
        obs["transport"] = [
            r
            for r in obs["transport"]
            if not (
                r["event"] == "request"
                and r.get("method") == "tools/call"
                and r["session"] == sid
                and r.get("request") == start["request"]
                and r["time_ns"] <= start["time_ns"]
            )
        ]

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP))
@pytest.mark.parametrize("missing", ["request", "response"])
def test_survivor_requires_its_transport_exchange(bundle, case, missing):
    def mutate(record):
        obs = record["observations"]
        sid = obs["cases"][case]["session_b"]
        result = next(
            r
            for r in reversed(obs["transport"])
            if r["event"] == "binding_result" and r["session"] == sid
        )
        requests = [
            r
            for r in obs["transport"]
            if r["event"] == "request"
            and r.get("method") == "tools/call"
            and r["session"] == sid
            and r.get("request") == result["request"]
        ]
        exchanges = {max(requests, key=lambda r: r["time_ns"])["exchange"]} if requests else set()
        obs["transport"] = [
            r
            for r in obs["transport"]
            if not (r["event"] == missing and r.get("exchange") in exchanges)
        ]

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


@pytest.mark.parametrize(
    "mutation",
    [
        "response_before_result",
        "wrong_exchange",
        "duplicate_response",
        "request_after_start",
        "duplicate_start",
    ],
)
def test_reused_request_id_cannot_borrow_an_earlier_exchange(bundle, mutation):
    def mutate(record):
        obs = record["observations"]
        sid = obs["cases"]["equal_ids_repeated_cancel"]["session_b"]
        events = obs["transport"]
        start = next(r for r in events if r["event"] == "binding_started" and r["session"] == sid)
        request = next(
            r
            for r in events
            if r["event"] == "request"
            and r.get("exchange") == e.digest("equal_ids_repeated_cancelsurvivor")
        )
        response = next(
            r
            for r in events
            if r["event"] == "response" and r.get("exchange") == request["exchange"]
        )
        if mutation == "response_before_result":
            response["time_ns"] = start["time_ns"]
        elif mutation == "wrong_exchange":
            response["exchange"] = e.digest("other")
        elif mutation == "duplicate_response":
            events.append(dict(response, status=500))
        elif mutation == "request_after_start":
            request["time_ns"] = start["time_ns"] + 1
        else:
            events.append(copy.deepcopy(start))

    change_artifact(bundle, "ownership", mutate)
    with pytest.raises(e.Refused):
        e.evaluate(*bundle)


def test_retained_reviewed_v4_campaign_evaluates_offline():
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4"
    result = e.evaluate(
        record / "evidence", e.read(record / "plan.json"), e.read(record / "candidate.json")
    )
    assert result == e.read(record / "evaluation.json")


@pytest.fixture(params=["synthetic", "retained"])
def ownership_observations(request):
    if request.param == "synthetic":
        return observations(manifest())["ownership"]
    record = ROOT / "scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4"
    return e.read(record / "evidence/ownership.json")["observations"]


def test_ownership_lifecycle_control_is_valid(ownership_observations):
    importlib.import_module("openclaw_conformance.ownership").verify(ownership_observations)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP - {"maf_ordinary_close"}))
@pytest.mark.parametrize("mutation", ["early_delete", "early_retirement", "late_admission"])
def test_ownership_rejects_reversed_lifecycle(ownership_observations, case, mutation):
    obs = ownership_observations
    values = obs["cases"][case]
    active = values["session_b" if case == "maf_idle_close" else "session_a"]
    rows = [
        r
        for r in obs["transport"]
        if r.get("session") in (values["session_a"], values["session_b"])
    ]
    start = next(r for r in rows if r["event"] == "binding_started" and r["session"] == active)
    delete = next(
        r for r in rows if r["event"] == "delete_requested" and r["session"] == values["session_a"]
    )
    retired = next(r for r in rows if r["event"] == "retired" and r["session"] == active)
    if mutation == "early_delete":
        delete["time_ns"] = start["time_ns"] - 1
        if active == values["session_a"]:
            retired["time_ns"] = start["time_ns"] - 1
    elif mutation == "early_retirement":
        retired["time_ns"] = start["time_ns"] + 1
        if active == values["session_a"]:
            delete["time_ns"] = retired["time_ns"]
    else:
        for row in rows:
            if row["event"] == "admission_refused":
                row["time_ns"] = retired["time_ns"] + 1
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", ["equal_ids_repeated_cancel", "maf_active_close"])
def test_ownership_cancellation_precedes_retirement(ownership_observations, case):
    obs = ownership_observations
    sid = obs["cases"][case]["session_a"]
    rows = [r for r in obs["transport"] if r.get("session") == sid]
    retired = next(r for r in rows if r["event"] == "retired")
    cancelled = next(r for r in rows if r["event"] == "binding_cancelled")
    cancelled["time_ns"] = retired["time_ns"] + 1
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize(
    "mutation",
    [
        "all_work",
        "request",
        "binding_started",
        "binding_result",
        "response",
        "late_result",
        "late_response",
        "incomplete",
        "wrong_source",
    ],
)
def test_ordinary_close_requires_successful_preclose_exchange(ownership_observations, mutation):
    obs = ownership_observations
    sid = obs["cases"]["maf_ordinary_close"]["session_a"]
    rows = [r for r in obs["transport"] if r.get("session") == sid]
    result = next(r for r in rows if r["event"] == "binding_result")
    request = next(r for r in rows if r["event"] == "request" and r.get("method") == "tools/call")
    response = next(
        r for r in rows if r["event"] == "response" and r["exchange"] == request["exchange"]
    )
    delete = next(r for r in rows if r["event"] == "delete_requested")
    if mutation.startswith("late_"):
        (result if mutation == "late_result" else response)["time_ns"] = delete["time_ns"] + 1
    elif mutation == "incomplete":
        result.update(completed=False, status="incomplete")
    elif mutation == "wrong_source":
        result["source_sha256"] = e.LARGE_SOURCE_SHA256
    else:
        work = [request, response, result, next(r for r in rows if r["event"] == "binding_started")]
        remove = work if mutation == "all_work" else [r for r in work if r["event"] == mutation]
        obs["transport"] = [r for r in obs["transport"] if r not in remove]
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP))
@pytest.mark.parametrize("binding", ["first", "survivor"])
@pytest.mark.parametrize("offset", [0, 1])
def test_ownership_rejects_multiple_starts_per_exchange(
    ownership_observations, case, binding, offset
):
    obs = ownership_observations
    values = obs["cases"][case]
    sid = values["session_b" if binding == "survivor" or case == "maf_idle_close" else "session_a"]
    starts = [
        r for r in obs["transport"] if r["event"] == "binding_started" and r.get("session") == sid
    ]
    start = starts[-1] if binding == "survivor" else starts[0]
    obs["transport"].append(dict(start, time_ns=start["time_ns"] + offset))
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", ["maf_idle_close", "maf_ordinary_close"])
def test_completed_binding_allows_request_id_reuse(ownership_observations, case):
    obs = ownership_observations
    sid = obs["cases"][case]["session_b"]
    starts = [
        r for r in obs["transport"] if r["event"] == "binding_started" and r.get("session") == sid
    ]
    assert len(starts) == 2
    first, last = starts[0], starts[-1]
    survivor_request = last["request"]
    for row in obs["transport"]:
        if row.get("session") == sid and row.get("request") == survivor_request:
            row["request"] = first["request"]
    importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", ["maf_active_close", "maf_idle_close", "maf_ordinary_close"])
def test_survivor_exchange_starts_after_closed_session_retires(ownership_observations, case):
    obs = ownership_observations
    a, b = (obs["cases"][case][key] for key in ("session_a", "session_b"))
    retired = next(r for r in obs["transport"] if r["event"] == "retired" and r.get("session") == a)
    request = next(
        r
        for r in reversed(obs["transport"])
        if r["event"] == "request" and r.get("method") == "tools/call" and r.get("session") == b
    )
    request["time_ns"] = retired["time_ns"] - 1
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", ["maf_active_close", "maf_idle_close"])
def test_maf_admission_probes_follow_close_transition(ownership_observations, case):
    obs = ownership_observations
    a, b = (obs["cases"][case][key] for key in ("session_a", "session_b"))
    kind = "delete_requested" if case == "maf_active_close" else "retired"
    transition = next(r for r in obs["transport"] if r["event"] == kind and r.get("session") == a)
    admission = next(
        r for r in obs["transport"] if r["event"] == "admission_refused" and r.get("session") == b
    )
    request = next(
        r
        for r in obs["transport"]
        if r["event"] == "request" and r.get("exchange") == admission["exchange"]
    )
    transition["time_ns"] = request["time_ns"] + 1
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP))
@pytest.mark.parametrize("binding", ["first", "survivor"])
@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "conflicting", "other_kind", "other_source", "orphan_request", "orphan_session"],
)
def test_each_binding_has_one_correlated_terminal(ownership_observations, case, binding, mutation):
    obs = ownership_observations
    for row in obs["transport"]:
        row["time_ns"] *= 10
    values = obs["cases"][case]
    sid = values["session_b" if binding == "survivor" or case == "maf_idle_close" else "session_a"]
    terminals = [
        r
        for r in obs["transport"]
        if r["event"] in ("binding_result", "binding_cancelled") and r.get("session") == sid
    ]
    terminal = terminals[-1] if binding == "survivor" else terminals[0]
    extra = dict(terminal, time_ns=terminal["time_ns"] - 1)
    if mutation == "conflicting":
        extra.update(
            event="binding_result", completed=False, status="incomplete", cleanup="unconfirmed"
        )
    elif mutation == "other_kind":
        extra["event"] = (
            "binding_cancelled" if terminal["event"] == "binding_result" else "binding_result"
        )
    elif mutation == "other_source":
        extra.update(event="binding_result", source_sha256="0" * 64)
    elif mutation == "orphan_request":
        extra["request"] = "0" * 64
    elif mutation == "orphan_session":
        extra["session"] = "0" * 64
    index = obs["transport"].index(terminal)
    obs["transport"].insert(index, extra)
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP - {"maf_ordinary_close"}))
@pytest.mark.parametrize("terminal_kind", [None, "binding_result", "binding_cancelled"])
def test_refused_admission_cannot_execute_a_binding(ownership_observations, case, terminal_kind):
    obs = ownership_observations
    for row in obs["transport"]:
        row["time_ns"] *= 10
    values = obs["cases"][case]
    refusal = next(
        r
        for r in obs["transport"]
        if r["event"] == "admission_refused" and r.get("session") == values["session_b"]
    )
    request = next(
        r
        for r in obs["transport"]
        if r["event"] == "request" and r.get("exchange") == refusal["exchange"]
    )
    fields = {"session": request["session"], "request": request["request"]}
    injected = [dict(fields, event="binding_started", time_ns=request["time_ns"] + 1)]
    if terminal_kind:
        injected.append(
            dict(values["survivor"], **fields, event=terminal_kind, time_ns=request["time_ns"] + 2)
        )
    index = obs["transport"].index(request) + 1
    obs["transport"][index:index] = injected
    with pytest.raises(e.Refused, match="refused_binding_execution" if terminal_kind else None):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize("case", sorted(e.OWNERSHIP))
@pytest.mark.parametrize("binding", ["first", "survivor"])
def test_no_started_binding_can_omit_its_terminal(ownership_observations, case, binding):
    obs = ownership_observations
    values = obs["cases"][case]
    sid = values["session_b" if binding == "survivor" or case == "maf_idle_close" else "session_a"]
    terminals = [
        r
        for r in obs["transport"]
        if r["event"] in ("binding_result", "binding_cancelled") and r.get("session") == sid
    ]
    obs["transport"].remove(terminals[-1] if binding == "survivor" else terminals[0])
    with pytest.raises(e.Refused, match="binding_terminal_count"):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)


@pytest.mark.parametrize(
    "mutation", ["incomplete", "cleanup", "source", "late", "missing_exchange"]
)
def test_ordinary_peer_preclose_work_is_also_verified(ownership_observations, mutation):
    obs = ownership_observations
    values = obs["cases"]["maf_ordinary_close"]
    sid = values["session_b"]
    result = next(
        r for r in obs["transport"] if r["event"] == "binding_result" and r.get("session") == sid
    )
    if mutation == "incomplete":
        result.update(completed=False, status="incomplete")
    elif mutation == "cleanup":
        result["cleanup"] = "unconfirmed"
    elif mutation == "source":
        result["source_sha256"] = "0" * 64
    elif mutation == "late":
        delete = next(
            r
            for r in obs["transport"]
            if r["event"] == "delete_requested" and r.get("session") == values["session_a"]
        )
        result["time_ns"] = delete["time_ns"] + 1
    else:
        request = next(
            r
            for r in obs["transport"]
            if r["event"] == "request"
            and r.get("session") == sid
            and r.get("request") == result["request"]
        )
        obs["transport"] = [
            r
            for r in obs["transport"]
            if not (
                r.get("session") == sid
                and (
                    r.get("request") == result["request"]
                    or r.get("exchange") == request["exchange"]
                )
            )
        ]
    with pytest.raises(e.Refused):
        importlib.import_module("openclaw_conformance.ownership").verify(obs)
