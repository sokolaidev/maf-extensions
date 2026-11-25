"""Exercise replacement requests without publishing images or sending GitHub notifications."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import image_release_needed as tracker  # noqa: E402
from container_release_history import GitHub, encode  # noqa: E402

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
RECORD = {
    "profile": "graphviz",
    "version": "0.1.0",
    "registryDigest": "sha256:" + "b" * 64,
    "imageId": "sha256:" + "c" * 64,
    "completedAt": "2026-01-01T00:00:00Z",
    "sourceCommit": SOURCE,
    "state": "completed",
    "delivery": "complete",
}
RELEASED = {"components": [["apk", "graphviz", "1", "pkg:apk/graphviz@1"]], "evidence": "release"}
FINDING = {"id": "CVE-2026-1234", "severity": "High", "fix": {"state": "not-fixed", "versions": []}}


def candidate(version="2", outcome="clean", **updates):
    return {
        "components": [["apk", "graphviz", version, f"pkg:apk/graphviz@{version}"]],
        "assessment": {
            "outcome": outcome,
            "findings": [FINDING] if outcome == "vulnerable" else [],
        },
        "source": SOURCE,
        "observedAt": "2026-01-02T00:00:00Z",
        "evidence": "run/1",
    } | updates


def optional_desired(
    *, record=None, released=None, changed=None, observed=None, previous=None, requirements=None
):
    return tracker.desired(
        record or RECORD,
        released or RELEASED,
        changed or [],
        SOURCE,
        observed,
        previous,
        requirements,
    )


def desired(**kwargs):
    result = optional_desired(**kwargs)
    assert result is not None
    return result


def replacement():
    return RECORD | {
        "version": "0.1.1",
        "registryDigest": "sha256:" + "d" * 64,
        "completedAt": "2026-01-03T00:00:00Z",
    }


def test_no_reason_means_no_issue():
    assert optional_desired() is None


def vulnerable_record(**updates):
    return (
        RECORD
        | {
            "latestAttempt": {
                "outcome": "vulnerable",
                "findings": [FINDING],
                "evidenceRelease": "image-monitor-1-1",
            }
        }
        | updates
    )


def legacy_request(*reasons):
    return {
        "baseline": RECORD["registryDigest"],
        "currentDigest": RECORD["registryDigest"],
        "currentVersion": RECORD["version"],
        "releaseEvidence": "release",
        "reasons": {reason: {} for reason in reasons},
        "status": "Refresh available for maintainer review",
        "resolved": False,
    }


REQUIREMENT = {
    "package": "maf-sandbox-drawio",
    "minimumVersion": "0.1.1",
    "reason": "The renderer requires the updated image command contract",
}


@pytest.mark.parametrize("outcome", ["clean", "vulnerable"])
@pytest.mark.parametrize("changed", [[], ["images/graphviz-sandbox/Dockerfile"]])
def test_drift_and_candidate_findings_alone_do_not_request_release(outcome, changed):
    assert optional_desired(changed=changed, observed=candidate(outcome=outcome)) is None


@pytest.mark.parametrize("reasons", [("components",), ("source",), ("components", "source")])
@pytest.mark.parametrize("observed", [None, candidate(), candidate(outcome="vulnerable")])
def test_legacy_drift_requests_close_without_claiming_replacement(reasons, observed):
    result = desired(previous=legacy_request(*reasons), observed=observed)
    assert result["resolved"]
    assert result["reasons"] == {}
    assert result["resolution"] == "policy-reconciled"
    assert result["currentDigest"] == RECORD["registryDigest"]
    assert "candidateEvidence" not in result
    assert "candidateFindings" not in result
    assert "No replacement release is claimed" in tracker.body("graphviz", result)


@pytest.mark.parametrize("outcome", ["clean", "unavailable"])
def test_legacy_drift_with_known_security_need_remains_open(outcome):
    previous = legacy_request("components", "source")
    previous["reasons"]["vulnerabilities"] = {
        "baselineDigest": RECORD["registryDigest"],
        "findings": [FINDING],
        "evidence": "monitor",
    }
    result = desired(record=RECORD | {"latestAttempt": {"outcome": outcome}}, previous=previous)
    assert not result["resolved"]
    assert set(result["reasons"]) == {"vulnerabilities"}


@pytest.mark.parametrize("profile", tuple(tracker.BICEP_ROUTES))
def test_route_security_policy_requires_replacement(profile):
    result = desired(
        record=RECORD | {"profile": profile}, released=RELEASED | {"bicepRoute": "upstream"}
    )
    assert result["reasons"] == {"route": {"published": "upstream", "replacement": "source"}}
    assert not result["resolved"]


def test_diagnostic_candidate_does_not_clear_or_refresh_security_request():
    record = vulnerable_record(profile="bicep-avm")
    released = RELEASED | {"bicepRoute": "source"}
    result = desired(record=record, released=released, observed=candidate(bicepRoute="upstream"))
    assert not result["resolved"]
    assert "candidateEvidence" not in result


@pytest.mark.parametrize("outcome", ["clean", "vulnerable"])
def test_security_request_preserves_candidate_status_across_monitor_reconciliation(outcome):
    previous = desired(record=vulnerable_record(), observed=candidate(outcome=outcome))
    result = desired(record=vulnerable_record(), previous=previous)
    assert result == previous
    assert result["candidateFindings"] == ([FINDING] if outcome == "vulnerable" else [])


def test_clean_replacement_closes_security_request_despite_routine_drift():
    previous = desired(record=vulnerable_record(), observed=candidate())
    result = desired(
        record=replacement(),
        previous=previous,
        changed=["images/graphviz-sandbox/Dockerfile"],
        observed=candidate(version="3", observedAt="2026-01-04T00:00:00Z"),
    )
    assert result["resolved"]
    assert "resolution" not in result
    assert "completed, verified replacement" in result["status"]


def test_new_security_finding_on_replacement_requires_another_release():
    previous = desired(record=vulnerable_record())
    current = vulnerable_record(**replacement())
    previous = desired(record=current, previous=previous)
    result = desired(record=current | {"latestAttempt": {"outcome": "clean"}}, previous=previous)
    assert not result["resolved"]
    assert result["reasons"]["vulnerabilities"]["baselineDigest"] == current["registryDigest"]


@pytest.mark.parametrize("current,needed", [("0.1.0", True), ("0.1.1", False), ("0.1.10", False)])
def test_package_minimum_is_compared_with_completed_release(current, needed):
    result = optional_desired(record=RECORD | {"version": current}, requirements=[REQUIREMENT])
    assert (result is not None) == needed
    if result:
        assert result["reasons"] == {"requirements": {"packages": [REQUIREMENT]}}
        assert REQUIREMENT["reason"] in tracker.body("graphviz", result)


def test_candidate_cannot_satisfy_package_minimum():
    result = desired(requirements=[REQUIREMENT], observed=candidate())
    assert not result["resolved"]
    assert "requirements" in result["reasons"]
    closed = desired(record=replacement(), previous=result, requirements=[REQUIREMENT])
    assert closed["resolved"]
    assert "completed, verified replacement" in closed["status"]


@pytest.mark.parametrize("record", [RECORD, replacement()])
def test_withdrawn_package_minimum_closes_as_not_planned(record):
    previous = desired(requirements=[REQUIREMENT | {"minimumVersion": "0.1.2"}])
    result = desired(record=record, previous=previous)
    assert result["resolved"]
    assert result["resolution"] == "policy-reconciled"


def test_package_minimum_does_not_clear_security_findings():
    previous = desired(requirements=[REQUIREMENT])
    result = desired(
        record=vulnerable_record(**replacement()), previous=previous, requirements=[REQUIREMENT]
    )
    assert set(result["reasons"]) == {"vulnerabilities"}
    assert not result["resolved"]


def test_repository_package_requirement_policy_is_valid():
    tracker.package_requirements(ROOT)


@pytest.mark.parametrize(
    "damage", [None, "profile", "package", "version", "reason", "duplicate", "schema"]
)
def test_package_requirement_policy_validation(tmp_path, damage):
    policy = {"schemaVersion": 1, "profiles": {"graphviz": [copy.deepcopy(REQUIREMENT)]}}
    entry = policy["profiles"]["graphviz"][0]
    if damage == "profile":
        policy["profiles"]["unknown"] = policy["profiles"].pop("graphviz")
    elif damage == "package":
        entry["package"] = "../outside"
    elif damage == "version":
        entry["minimumVersion"] = "latest"
    elif damage == "reason":
        entry["reason"] = " "
    elif damage == "duplicate":
        policy["profiles"]["graphviz"].append(copy.deepcopy(entry))
    elif damage == "schema":
        policy["schemaVersion"] = 2
    package = tmp_path / "packages/maf-sandbox-drawio/pyproject.toml"
    package.parent.mkdir(parents=True)
    package.write_text('[project]\nname = "maf-sandbox-drawio"\n', encoding="utf-8")
    path = tmp_path / "images/package-requirements.json"
    path.parent.mkdir()
    path.write_text(json.dumps(policy), encoding="utf-8")
    if damage:
        with pytest.raises(ValueError):
            tracker.package_requirements(tmp_path)
    else:
        assert tracker.package_requirements(tmp_path) == policy["profiles"]


def test_scan_timestamps_and_catalogue_ids_do_not_change_component_identity():
    first = {
        "source": {"type": "image", "metadata": {"imageID": RECORD["imageId"]}},
        "artifacts": [
            {
                "id": "x",
                "name": "graphviz",
                "version": "1",
                "type": "apk",
                "purl": "pkg:apk/graphviz@1",
            }
        ],
    }
    second = copy.deepcopy(first)
    second["artifacts"][0].update(id="y", locations=[{"path": "/other"}])
    assert tracker.components(first, RECORD["imageId"]) == tracker.components(
        second, RECORD["imageId"]
    )
    assert optional_desired(observed=candidate(version="1")) is None


def test_inventory_must_identify_the_assessed_image():
    with pytest.raises(ValueError, match="identify"):
        tracker.components(
            {
                "source": {"type": "image", "metadata": {"imageID": "wrong"}},
                "artifacts": [{"name": "graphviz"}],
            },
            RECORD["imageId"],
        )


def test_unfixed_published_vulnerability_opens_request_without_candidate():
    observation = {
        "outcome": "vulnerable",
        "findings": [FINDING],
        "evidenceRelease": "image-monitor-1-1",
    }
    result = desired(record=RECORD | {"latestAttempt": observation})
    assert result["reasons"]["vulnerabilities"]["findings"] == [FINDING]
    assert result["status"] == "Blocked on remediation; no passing replacement assessed"


@pytest.mark.parametrize("outcome", ["unavailable", "running"])
def test_monitor_outages_alone_do_not_request_new_image(outcome):
    assert optional_desired(record=RECORD | {"latestAttempt": {"outcome": outcome}}) is None


def test_outage_does_not_erase_known_vulnerability():
    record = RECORD | {
        "latestAttempt": {"outcome": "unavailable"},
        "lastKnownVulnerable": {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "image-monitor-1-1",
        },
    }
    assert "vulnerabilities" in desired(record=record)["reasons"]


def test_clean_monitor_does_not_reopen_resolved_historical_finding():
    record = RECORD | {
        "latestAttempt": {"outcome": "clean"},
        "lastKnownVulnerable": {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "image-monitor-1-1",
        },
    }
    assert optional_desired(record=record) is None


class Issues(GitHub):
    def __init__(self):
        self.items = []
        self.calls = []
        self.comments = []

    def request(self, endpoint, **kwargs):
        if "/comments?" in endpoint:
            return encode(self.comments)
        payload = json.loads(kwargs["payload"])
        self.calls.append((endpoint, kwargs["method"], payload))
        if endpoint.endswith("/comments"):
            self.comments.append(payload | {"user": {"login": "github-actions[bot]"}})
            return b"{}"
        if kwargs["method"] == "POST":
            self.items.append(
                payload
                | {
                    "number": max((item["number"] for item in self.items), default=0) + 1,
                    "state": "open",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                }
            )
        else:
            number = int(endpoint.rsplit("/", 1)[1])
            next(item for item in self.items if item["number"] == number).update(payload)
        return b"{}"


@pytest.mark.parametrize(
    "state,delivery",
    [("incomplete", "pending"), ("completed", "pending"), ("abandoned", "pending")],
)
def test_incomplete_release_cannot_be_used_to_close_request(tmp_path, state, delivery):
    with pytest.raises(ValueError, match="incomplete") as caught:
        tracker.baseline(GitHub(), RECORD | {"state": state, "delivery": delivery}, tmp_path)
    assert str(caught.value) == (
        "Release evidence delivery is incomplete: "
        f"{RECORD['profile']}/{RECORD['version']} (state={state}, delivery={delivery})"
    )


@pytest.mark.parametrize("draft,immutable", [(True, False), (False, False)])
def test_baseline_requires_immutable_published_evidence(monkeypatch, tmp_path, draft, immutable):
    monkeypatch.setattr(
        tracker.Evidence, "fetch", lambda *_: {"draft": draft, "immutable": immutable}
    )
    monkeypatch.setattr(tracker, "image_tag", lambda _: "image-graphviz-v0.1.0")
    with pytest.raises(ValueError, match="immutable"):
        tracker.baseline(GitHub(), RECORD, tmp_path)


def test_baseline_checks_signatures_before_consuming_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tracker.Evidence,
        "fetch",
        lambda *_: {"draft": False, "immutable": True, "tag_name": "image-graphviz-v0.1.0"},
    )
    monkeypatch.setattr(tracker, "image_tag", lambda _: "image-graphviz-v0.1.0")
    monkeypatch.setattr(GitHub, "tag_commit", lambda *_: SOURCE)

    def invalid(*_args, **_kwargs):
        raise ValueError("invalid completion signature")

    monkeypatch.setattr(tracker, "verify_identity", invalid)
    with pytest.raises(ValueError, match="signature"):
        tracker.baseline(GitHub(), RECORD | {"candidate": {}}, tmp_path)


def test_release_identity_is_independent_of_vulnerability_assessment(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tracker.Evidence,
        "fetch",
        lambda *_: {"draft": False, "immutable": True, "tag_name": "image-graphviz-v0.1.0"},
    )
    monkeypatch.setattr(tracker, "image_tag", lambda _: "image-graphviz-v0.1.0")
    monkeypatch.setattr(GitHub, "tag_commit", lambda *_: SOURCE)
    verified = []
    monkeypatch.setattr(tracker, "verify_identity", lambda *a, **kw: verified.append((a, kw)))
    (tmp_path / "evidence-index.json").write_bytes(b"{}")
    record = RECORD | {"candidate": {}, "evidenceIndexSha256": tracker.sha256(b"{}")}
    assert tracker.release_evidence(GitHub(), record, tmp_path).endswith("image-graphviz-v0.1.0")
    assert verified[0][0] == ({}, tmp_path)
    assert verified[0][1] == {"bundles": True}
    with pytest.raises(ValueError, match="Evidence index"):
        tracker.release_evidence(GitHub(), record | {"evidenceIndexSha256": "other"}, tmp_path)


@pytest.mark.parametrize("severity", ["High", "Critical"])
def test_image_refresh_baseline_uses_publication_threshold(monkeypatch, tmp_path, severity):
    monkeypatch.setattr(tracker, "release_evidence", lambda *a: "release")
    assessment = {
        "assessedAt": "2026-01-01T00:00:00Z",
        "outcome": "vulnerable",
        "findings": [{"severity": severity}],
    }
    (tmp_path / "assessment.json").write_text(json.dumps(assessment))
    (tmp_path / "grype.json").write_text("{}")
    (tmp_path / "sbom.syft.json").write_text("{}")
    monkeypatch.setattr(tracker, "scan_result", lambda *a, **kw: assessment)
    monkeypatch.setattr(tracker, "components", lambda *a: [])
    if severity == "Critical":
        with pytest.raises(ValueError, match="release gate"):
            tracker.baseline(GitHub(), RECORD | {"candidate": {}}, tmp_path)
    else:
        assert tracker.baseline(GitHub(), RECORD | {"candidate": {}}, tmp_path) == {
            "components": [],
            "evidence": "release",
            "assessment": assessment,
        }


@pytest.fixture
def high_release(monkeypatch, tmp_path):
    assessment = {
        "assessedAt": "2026-01-03T00:00:00Z",
        "outcome": "vulnerable",
        "findings": [FINDING],
    }
    monkeypatch.setattr(tracker, "release_evidence", lambda *a: "release")
    monkeypatch.setattr(tracker, "scan_result", lambda *a, **kw: assessment)
    monkeypatch.setattr(tracker, "components", lambda *a: RELEASED["components"])
    for name, value in (
        ("assessment.json", assessment),
        ("grype.json", {}),
        ("sbom.syft.json", {}),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    return tracker.baseline(GitHub(), replacement() | {"candidate": {}}, tmp_path)


@pytest.mark.parametrize("outcome", [None, "unavailable", "running", "clean", "vulnerable"])
@pytest.mark.parametrize("assessed_at", ["2026-01-02", "2026-01-03", "2026-01-04"])
def test_release_findings_keep_replacement_issue_open_until_newer_monitor(
    high_release, outcome, assessed_at
):
    client = Issues()
    previous = desired(
        record=RECORD
        | {
            "latestAttempt": {
                "outcome": "vulnerable",
                "findings": [FINDING],
                "evidenceRelease": "old",
            }
        }
    )
    tracker.reconcile(client, "graphviz", previous, None)
    record = replacement()
    monitoring_finding = FINDING | {"id": "CVE-2026-5678"}
    if outcome is not None:
        record = record | {
            "latestAttempt": {
                "outcome": outcome,
                "assessedAt": assessed_at + "T00:00:00Z",
                "findings": [monitoring_finding] if outcome == "vulnerable" else [],
                "evidenceRelease": "new",
            }
        }
    state = desired(record=record, released=high_release, previous=previous)
    tracker.reconcile(client, "graphviz", state, client.items[0])
    newer = assessed_at == "2026-01-04"
    resolved = outcome == "clean" and newer
    assert state["resolved"] == resolved
    assert client.items[0]["state"] == ("closed" if resolved else "open")
    if not resolved:
        reason = state["reasons"]["vulnerabilities"]
        assert reason["baselineDigest"] == record["registryDigest"]
        monitored = outcome == "vulnerable" and newer
        assert reason["findings"] == ([monitoring_finding] if monitored else [FINDING])
        assert reason["evidence"] == (
            f"https://github.com/{tracker.REPOSITORY}/releases/tag/new" if monitored else "release"
        )


@pytest.mark.parametrize("assessed_at", ["2026-01-02", "2026-01-04"])
def test_release_findings_compete_with_last_known_monitor_after_outage(high_release, assessed_at):
    monitoring_finding = FINDING | {"id": "CVE-2026-5678"}
    record = replacement() | {
        "latestAttempt": {"outcome": "unavailable", "assessedAt": "2026-01-05T00:00:00Z"},
        "lastKnownVulnerable": {
            "outcome": "vulnerable",
            "assessedAt": assessed_at + "T00:00:00Z",
            "findings": [monitoring_finding],
            "evidenceRelease": "monitor",
        },
    }
    state = desired(record=record, released=high_release)
    reason = state["reasons"]["vulnerabilities"]
    monitored = assessed_at == "2026-01-04"
    assert reason["findings"] == ([monitoring_finding] if monitored else [FINDING])
    assert reason["evidence"] == (
        f"https://github.com/{tracker.REPOSITORY}/releases/tag/monitor" if monitored else "release"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "topic"),
        ("event", "pull_request"),
        ("path", ".github/workflows/tests.yml"),
        ("head_repository", {"full_name": "other/repo"}),
    ],
)
def test_untrusted_scan_runs_never_download_artifacts(monkeypatch, tmp_path, field, value):
    run = {
        "head_branch": "main",
        "head_repository": {"full_name": tracker.REPOSITORY},
        "event": "schedule",
        "path": ".github/workflows/image-security.yml",
        "status": "completed",
    }
    monkeypatch.setattr(GitHub, "request", lambda *_: encode(run | {field: value}))
    with pytest.raises(ValueError, match="Untrusted"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_pagination_preserves_issue_state_filter(monkeypatch):
    seen = []

    def request(_self, endpoint):
        seen.append(endpoint)
        return b"[]"

    monkeypatch.setattr(GitHub, "request", request)
    assert GitHub().pages("repos/test/issues?state=all") == []
    assert seen == ["repos/test/issues?state=all&per_page=100&page=1"]


def commit(root, changes):
    for name, content in changes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    tracker.git(root, "add", ".")
    tracker.git(
        root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "test"
    )
    return tracker.git(root, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("change", ["added", "modified", "deleted"])
def test_consumer_guide_alone_does_not_request_a_replacement(tmp_path, change):
    tracker.git(tmp_path, "init")
    guide = "images/graphviz-sandbox/README.md"
    files = {"images/graphviz-sandbox/Dockerfile": "FROM base\n"}
    if change != "added":
        files[guide] = "Original instructions"
    base = commit(tmp_path, files)
    if change == "deleted":
        (tmp_path / guide).unlink()
        head = commit(tmp_path, {})
    else:
        head = commit(tmp_path, {guide: "Updated instructions"})
    changed = tracker.changed_inputs(tmp_path, base, head, "graphviz")
    assert changed == []
    assert optional_desired(changed=changed) is None
    payload = "images/graphviz-sandbox/Dockerfile"
    mixed = commit(tmp_path, {payload: "FROM newer\n"})
    changed = tracker.changed_inputs(tmp_path, base, mixed, "graphviz")
    assert changed == [payload]
    assert optional_desired(changed=changed) is None


@pytest.mark.parametrize("change", ["added", "modified", "deleted"])
def test_gitignore_changes_only_affect_workspace_image_inputs(tmp_path, change):
    tracker.git(tmp_path, "init")
    payload = "images/drawio-export/requirements.txt"
    files = {payload: "pillow==12.3.0\n"}
    if change != "added":
        files[".gitignore"] = "out/\n"
    base = commit(tmp_path, files)
    if change == "deleted":
        (tmp_path / ".gitignore").unlink()
        head = commit(tmp_path, {})
    else:
        head = commit(tmp_path, {".gitignore": "out/\nbandit-report.json\n"})
    for profile in tracker.PROFILES:
        changed = tracker.changed_inputs(tmp_path, base, head, profile)
        assert changed == ([".gitignore"] if profile == "hyperlight-aks-probe" else [])
    assert tracker.changed_profiles(tmp_path, base, head, ".gitignore") == tracker.PROFILES
    record = RECORD | {"profile": "drawio-export"}
    previous = legacy_request("source")
    changed = tracker.changed_inputs(tmp_path, base, head, "drawio-export")
    result = desired(record=record, changed=changed, previous=previous)
    assert result["resolved"]
    assert result["resolution"] == "policy-reconciled"
    mixed = commit(tmp_path, {payload: "pillow==13.0.0\n"})
    assert tracker.changed_inputs(tmp_path, base, mixed, "drawio-export") == [payload]


@pytest.mark.parametrize("path", [".dockerignore", ".gitattributes", ".gitmodules"])
def test_shared_build_controls_remain_release_inputs(tmp_path, path):
    tracker.git(tmp_path, "init")
    base = commit(tmp_path, {path: "old\n"})
    head = commit(tmp_path, {path: "new\n"})
    for profile in tracker.PROFILES:
        assert tracker.changed_inputs(tmp_path, base, head, profile) == [path]


def test_source_diff_ignores_publisher_but_tracks_payload_and_prepared_dependencies(tmp_path):
    tracker.git(tmp_path, "init")
    base = commit(
        tmp_path,
        {
            "images/graphviz-sandbox/Dockerfile": "FROM base\n",
            "scripts/terraform_dependencies.py": "v1",
        },
    )
    tooling = commit(
        tmp_path,
        {
            "scripts/container_release_history.py": "new",
            "tests/test_image_release_needed.py": "new",
        },
    )
    assert tracker.changed_inputs(tmp_path, base, tooling, "graphviz") == []
    payload = commit(tmp_path, {"images/graphviz-sandbox/Dockerfile": "FROM newer\n"})
    assert tracker.changed_inputs(tmp_path, base, payload, "graphviz") == [
        "images/graphviz-sandbox/Dockerfile"
    ]
    assert tracker.changed_inputs(tmp_path, base, payload, "bicep") == []
    prepared = commit(tmp_path, {"scripts/terraform_dependencies.py": "v2"})
    assert "scripts/terraform_dependencies.py" in tracker.changed_inputs(
        tmp_path, payload, prepared, "terraform-random"
    )
    assert tracker.changed_inputs(tmp_path, payload, prepared, "graphviz") == []


def test_bicep_profile_rename_reconciles_avm_source_request(tmp_path):
    tracker.git(tmp_path, "init")
    path = "scripts/build_bicep_image.py"
    builder = (ROOT / path).read_text(encoding="utf-8")
    base = commit(tmp_path, {path: builder.replace('"bicep-docker-sbx"', '"sbx-bicep"')})
    head = commit(tmp_path, {path: builder})
    record = RECORD | {"profile": "bicep-avm", "sourceCommit": base}
    released = RELEASED | {"bicepRoute": "source"}
    previous = legacy_request("source")
    changed = tracker.changed_inputs(tmp_path, base, head, "bicep-avm")
    assert changed == []
    result = desired(record=record, released=released, changed=changed, previous=previous)
    assert result["resolved"]
    assert result["resolution"] == "policy-reconciled"
    assert tracker.changed_inputs(tmp_path, base, head, "bicep-docker-sbx") == [path]
    mixed = commit(tmp_path, {path: builder.replace('"--pull", ', "")})
    changed = tracker.changed_inputs(tmp_path, base, mixed, "bicep-avm")
    assert changed == [path]
    assert desired(record=record, released=released, changed=changed, previous=previous)["resolved"]


def test_bicep_diagnostic_changes_do_not_hide_build_or_guard_changes(tmp_path):
    tracker.git(tmp_path, "init")
    path = "scripts/build_bicep_image.py"
    prefix = 'PROFILES = {"bicep", "bicep-avm"}\nROUTES = ("upstream", "source")\n'
    original = (
        prefix
        + """
def validate_route(route, profile):
    if route not in ROUTES or (route != "upstream" and profile not in PROFILES):
        raise ValueError("Unsupported route")
def command():
    return ["docker", "build", "--pull"]
"""
    )
    updated = (
        prefix
        + """
def validate_route(route, profile):
    if route not in ROUTES:
        raise ValueError(f"Unknown {route!r}")
    if route != "upstream" and profile not in PROFILES:
        raise ValueError("Choose a Bicep profile")
def command():
    return ["docker", "build", "--pull"]
"""
    )
    base = commit(tmp_path, {path: original})
    head = commit(tmp_path, {path: updated})
    assert tracker.changed_inputs(tmp_path, base, head, "bicep") == []
    for modified in (
        updated.replace(', "--pull"', ""),
        updated.replace('route != "upstream"', 'route == "upstream"'),
        updated.replace(
            'raise ValueError("Choose a Bicep profile")', "raise ValueError(read_policy())"
        ),
    ):
        head = commit(tmp_path, {path: modified})
        assert tracker.changed_inputs(tmp_path, base, head, "bicep") == [path]


def test_workspace_and_package_metadata_only_select_images_that_consume_them(tmp_path):
    tracker.git(tmp_path, "init")
    paths = [
        "images/README.md",
        "packages/maf-compaction/pyproject.toml",
        "uv.lock",
        "pyproject.toml",
    ]
    base = commit(tmp_path, {path: "old" for path in paths})
    head = commit(tmp_path, {path: "new" for path in paths})
    for profile in tracker.PROFILES:
        changed = tracker.changed_inputs(tmp_path, base, head, profile)
        assert changed == (
            ["pyproject.toml", "uv.lock"] if profile == "hyperlight-aks-probe" else []
        )


def test_terraform_profile_addition_reconciles_bicep_source_request(tmp_path):
    tracker.git(tmp_path, "init")
    builder = (ROOT / "scripts/build_scan_image.sh").read_text()
    catalogue = json.loads((ROOT / "images/release-profiles.json").read_text())
    before, count = re.subn(r"  terraform-avm\)\n.*?    ;;\n", "", builder, flags=re.DOTALL)
    assert count == 1
    paths = ["images/release-profiles.json", "scripts/build_scan_image.sh"]
    old_catalogue = catalogue | {
        "profiles": [p for p in catalogue["profiles"] if p != "terraform-avm"]
    }
    base = commit(tmp_path, {paths[0]: json.dumps(old_catalogue), paths[1]: before})
    head = commit(tmp_path, {paths[0]: json.dumps(catalogue), paths[1]: builder})
    record = RECORD | {"profile": "bicep-avm", "sourceCommit": base}
    released = RELEASED | {"bicepRoute": "source"}
    previous = legacy_request("source")
    changed = tracker.changed_inputs(tmp_path, base, head, "bicep-avm")
    assert changed == []
    result = desired(record=record, released=released, changed=changed, previous=previous)
    assert result["resolution"] == "policy-reconciled"
    assert tracker.changed_inputs(tmp_path, base, head, "terraform-avm") == paths
    payload = "images/bicep-sandbox/Dockerfile"
    mixed = commit(tmp_path, {payload: "FROM newer\n"})
    changed = tracker.changed_inputs(tmp_path, base, mixed, "bicep-avm")
    assert changed == [payload]
    assert desired(record=record, released=released, changed=changed, previous=previous)["resolved"]


@pytest.mark.parametrize(
    "filename",
    ["caf\u00e9.txt", "line\nbreak", "cr\rname", "crlf\r\nname", "tab\tname", "trailing "],
)
def test_changed_inputs_preserve_git_quoted_filenames_for_candidate_matching(tmp_path, filename):
    tracker.git(tmp_path, "init")
    base = commit(tmp_path, {"README.md": "baseline"})

    def object_id(*args, data):
        return subprocess.check_output(["git", *args], input=data, cwd=tmp_path).decode().strip()

    blob = object_id("hash-object", "-w", "--stdin", data=b"changed input")
    tree = object_id("mktree", "-z", data=f"100644 blob {blob}\t{filename}\0".encode())
    for directory in ("graphviz-sandbox", "images"):
        tree = object_id("mktree", "-z", data=f"040000 tree {tree}\t{directory}\0".encode())
    head = tracker.git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        "input change",
    ).strip()
    path = f"images/graphviz-sandbox/{filename}"
    changed = tracker.changed_inputs(tmp_path, base, head, "graphviz")
    assert changed == [path]
    assert optional_desired(record=replacement(), changed=changed) is None


def test_core_python_source_remains_an_image_release_input(tmp_path):
    tracker.git(tmp_path, "init")
    base = commit(tmp_path, {"README.md": "baseline"})
    path = "packages/maf-sandbox/src/maf_sandbox/maf.py"
    head = commit(tmp_path, {path: "changed core behavior"})
    assert tracker.changed_inputs(tmp_path, base, head, "hyperlight-aks-probe") == [path]


@pytest.mark.parametrize("path", ["packages/maf-sandbox/pyproject.toml", "uv.lock"])
@pytest.mark.parametrize("change", ["added", "deleted", "malformed"])
def test_unclassifiable_metadata_remains_a_release_input(tmp_path, path, change):
    tracker.git(tmp_path, "init")
    valid = '[project]\nname = "maf-sandbox"\nversion = "1.0.0"\n'
    files = {"README.md": "baseline"}
    if change != "added":
        files[path] = valid
    base = commit(tmp_path, files)
    if change == "deleted":
        (tmp_path / path).unlink()
        head = commit(tmp_path, {})
    else:
        head = commit(tmp_path, {path: valid if change == "added" else "[invalid"})
    changed = tracker.changed_inputs(tmp_path, base, head, "hyperlight-aks-probe")
    assert changed == [path]
    assert tracker.changed_inputs(tmp_path, base, head, "graphviz") == []
    assert optional_desired(record=replacement(), changed=changed) is None


def test_missing_comparison_history_still_refuses_reconciliation(tmp_path):
    tracker.git(tmp_path, "init")
    head = commit(tmp_path, {"README.md": "baseline"})
    with pytest.raises(subprocess.CalledProcessError):
        tracker.changed_inputs(tmp_path, "0" * 40, head, "graphviz")


def test_git_helper_preserves_output_whitespace(tmp_path):
    tracker.git(tmp_path, "init")
    tracker.git(tmp_path, "config", "test.value", " value \t")
    assert tracker.git(tmp_path, "config", "--get", "test.value") == " value \t\n"


def test_pr_scans_and_tracker_cannot_publish():
    scan = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    triggers = scan.get("on", scan.get(True))
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    assert scan["permissions"] == {"contents": "read"}
    assert scan["jobs"]["scan"]["name"] == "Image security (${{ matrix.profile }})"
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-release-needed.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))["workflow_run"]
    assert trigger["branches"] == ["main"]
    assert set(trigger["workflows"]) == {
        "Image security",
        "Container image monitor",
        "1-Container image release",
    }
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["concurrency"]["group"] == "image-remediation-attention"
    job = workflow["jobs"]["reconcile"]
    assert job["concurrency"] == {
        "group": "container-security-catalogue",
        "queue": "max",
        "cancel-in-progress": False,
    }
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert "head_repository.full_name == github.repository" in job["if"]
    assert job["steps"][0]["with"]["ref"] == "${{ github.sha }}"


def test_manual_scan_selects_one_profile(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("SELECTED_PROFILE", "graphviz")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/select_image_security.py"),
            "--event",
            "workflow_dispatch",
        ],
        check=True,
    )
    assert json.loads(
        dict(line.split("=", 1) for line in output.read_text().splitlines())["profiles"]
    ) == ["graphviz"]


@pytest.fixture
def reporter(monkeypatch):
    client = Issues()
    current = {
        "record": vulnerable_record(),
        "released": copy.deepcopy(RELEASED),
        "observed": candidate(),
    }

    def head(_self):
        return SimpleNamespace(
            reference="stable",
            catalogue={
                "current": {"graphviz": "graphviz/current"},
                "releases": {"graphviz/current": current["record"]},
            },
        )

    monkeypatch.setattr(tracker, "package_requirements", lambda _: {})
    monkeypatch.setattr(tracker.History, "head", head)
    monkeypatch.setattr(tracker, "GitHub", lambda: client)
    monkeypatch.setattr(
        client,
        "pages",
        lambda endpoint: copy.deepcopy(
            client.comments if "/comments" in endpoint else client.items
        ),
    )
    monkeypatch.setattr(tracker, "baseline", lambda *_: current["released"])
    monkeypatch.setattr(
        tracker,
        "candidates",
        lambda *_: {"graphviz": current["observed"]} if current["observed"] else {},
    )
    monkeypatch.setattr(tracker, "changed_inputs", lambda *_: [])
    monkeypatch.setattr(tracker, "git", lambda *_: SOURCE)
    monkeypatch.setattr(sys, "argv", ["image_release_needed.py"])
    monkeypatch.setenv("GITHUB_REPOSITORY", tracker.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    return client, current


@pytest.mark.parametrize("closed", [False, True])
def test_reporter_retires_drift_without_reopening_or_creating_successor(reporter, closed):
    client, current = reporter
    current["record"] = copy.deepcopy(RECORD)
    state = legacy_request("components", "source")
    client.items.append(
        {
            "number": 1,
            "state": "closed" if closed else "open",
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "title": "Image refresh available: graphviz (current 0.1.0)",
            "body": tracker.body("graphviz", state),
        }
    )
    tracker.main()
    tracker.main()
    current["observed"] = candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    tracker.main()
    assert len(client.items) == len(client.calls) == 1
    assert client.items[0]["state"] == "closed"
    assert "No replacement release is claimed" in client.items[0]["body"]
    current["record"] = vulnerable_record()
    tracker.main()
    tracker.main()
    assert len(client.items) == 1 and len(client.calls) == 2
    assert client.items[0]["state"] == "open"
    assert client.items[0]["title"] == "Image security needs attention: graphviz"


def test_reporter_does_not_open_drift_only_request(reporter):
    client, current = reporter
    current["record"] = copy.deepcopy(RECORD)
    tracker.main()
    tracker.main()
    assert client.calls == []


def test_release_migrates_to_existing_remediation_issue_without_losing_blocker(reporter):
    client, current = reporter
    tracker.main()
    legacy_release = copy.deepcopy(client.items[0])
    # Model the separate trackers written before canonical issue identities existed.
    client.items[0]["body"] = tracker.body("graphviz", desired(record=vulnerable_record()))
    client.items.append(
        {
            "number": 8,
            "state": "open",
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "<!-- image-remediation-attention:graphviz -->\nAwaiting upstream fix; preserve maintainer context.",
        }
    )
    tracker.main()
    tracker.main()
    assert len(client.items) == 2
    assert client.items[0]["state"] == "closed"
    assert "Tracking continues in #8" in client.items[0]["body"]
    assert legacy_release["number"] == 1
    shared = client.items[1]
    assert "Awaiting upstream fix" in shared["body"]
    assert "Earlier report: #1" in shared["body"]
    assert tracker.STATE.search(shared["body"])
    current["record"] = replacement()
    tracker.main()
    assert shared["state"] == "open"
    assert tracker.image_issue.lanes(shared)["release"]["active"] is False
    assert tracker.image_issue.lanes(shared)["legacy-remediation"]["active"] is True


def test_reporter_respects_manual_dismissal_without_a_successor(reporter):
    client, current = reporter
    tracker.main()
    client.items[0]["state"] = "closed"
    historical = copy.deepcopy(client.items[0])
    current["observed"] = None
    tracker.main()
    tracker.main()
    assert client.items[0] == historical
    assert len(client.items) == len(client.calls) == 1
    assert client.items[0]["state"] == "closed"


def test_reporter_opens_and_satisfies_package_requirement(reporter, monkeypatch):
    client, current = reporter
    current["record"] = copy.deepcopy(RECORD)
    monkeypatch.setattr(tracker, "package_requirements", lambda _: {"graphviz": [REQUIREMENT]})
    tracker.main()
    tracker.main()
    assert len(client.calls) == 1
    assert REQUIREMENT["package"] in client.items[0]["body"]
    current["record"] = replacement()
    tracker.main()
    tracker.main()
    assert len(client.calls) == 2
    assert client.items[0]["state"] == "closed"
    assert client.items[0]["state_reason"] == "completed"


@pytest.mark.parametrize("failure", ["baseline", "changed_inputs"])
def test_source_reconciliation_requires_verified_evidence_and_comparison(
    reporter, monkeypatch, failure
):
    client, current = reporter
    current["observed"] = None
    tracker.reconcile(client, "graphviz", desired(record=vulnerable_record()), None)

    def unavailable(*_):
        raise ValueError("Evidence unavailable")

    monkeypatch.setattr(tracker, failure, unavailable)
    with pytest.raises(ValueError, match="Evidence unavailable"):
        tracker.main()
    assert len(client.calls) == 1
    assert client.items[0]["state"] == "open"


@pytest.mark.parametrize("extra_component", [False, True])
def test_reporter_reuses_issue_after_verified_recovery_and_recurrence(reporter, extra_component):
    client, current = reporter
    tracker.main()
    tracker.main()
    assert len(client.calls) == 1
    current["observed"] = candidate(version="3", observedAt="2026-01-02T00:00:01Z")
    tracker.main()
    assert len(client.calls) == 2
    current["record"] = replacement()
    current["released"] = RELEASED | {"components": current["observed"]["components"]}
    if extra_component:
        current["released"]["components"] = current["released"]["components"] + [
            ["dotnet", "library", "2", "pkg:nuget/library@2"]
        ]
    tracker.main()
    assert client.items[0]["state"] == "closed"
    tracker.main()
    assert len(client.calls) == 3
    current["record"] = vulnerable_record(**replacement())
    current["observed"] = candidate(version="4", observedAt="2026-01-04T00:00:00Z")
    tracker.main()
    tracker.main()
    assert len(client.items) == 1
    assert client.items[0]["state"] == "open"
    assert len(client.calls) == 4


def test_unchanged_newer_observation_prevents_delayed_intermediate_scan(reporter):
    client, current = reporter
    tracker.main()
    current["observed"] = candidate(observedAt="2026-01-04T00:00:00Z")
    tracker.main()
    saved = copy.deepcopy(client.items)
    current["observed"] = candidate(
        version="3", outcome="vulnerable", observedAt="2026-01-03T00:00:00Z"
    )
    tracker.main()
    assert client.items == saved
    assert len(client.items) == 1


def test_issue_created_between_pages_does_not_duplicate_a_release_request(reporter, monkeypatch):
    client, _ = reporter
    tracker.main()
    client.items[0]["number"] = 101
    issues = [{"number": number, "body": ""} for number in range(1, 201)]
    issues[100] = copy.deepcopy(client.items[0])

    def request(endpoint):
        query = parse_qs(urlsplit(endpoint).query)
        page = int(query["page"][0])
        if page == 2:
            issues.append({"number": 201, "body": ""})
        ordered = sorted(
            issues, key=lambda item: item["number"], reverse=query.get("direction") != ["asc"]
        )
        return encode(ordered[(page - 1) * 100 : page * 100])

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "pages", lambda endpoint: GitHub.pages(client, endpoint))
    tracker.main()
    assert len(client.calls) == 1


@pytest.mark.parametrize("filename", ["file*.txt", "file?.txt", "file[1].txt"])
def test_source_commit_uses_literal_changed_filenames(reporter, monkeypatch, tmp_path, filename):
    client, current = reporter
    current["observed"] = None

    def git(root, *args):
        return subprocess.check_output(["git", *args], cwd=root).decode("utf-8")

    def object_id(*args, data):
        return subprocess.check_output(["git", *args], input=data, cwd=tmp_path).decode().strip()

    def tree(literal, other):
        entries = []
        for name, content in sorted([(filename, literal), ("file1.txt", other)]):
            blob = object_id("hash-object", "-w", "--stdin", data=content)
            entries.append(f"100644 blob {blob}\t{name}\0")
        value = object_id("mktree", "-z", data="".join(entries).encode())
        for name in ("graphviz-sandbox", "images"):
            value = object_id("mktree", "-z", data=f"040000 tree {value}\t{name}\0".encode())
        return value

    git(tmp_path, "init")
    parent = []
    revisions = []
    for literal, other in [(b"old", b"new"), (b"new", b"old"), (b"new", b"new")]:
        revision = git(
            tmp_path,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            tree(literal, other),
            *parent,
            "-m",
            "test",
        ).strip()
        revisions.append(revision)
        parent = ["-p", revision]
    git(tmp_path, "update-ref", "HEAD", revisions[-1])
    path = f"images/graphviz-sandbox/{filename}"
    assert git(tmp_path, "diff", "--name-only", "-z", revisions[0], revisions[-1]) == path + "\0"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "git", git)
    monkeypatch.setattr(tracker, "changed_inputs", lambda *_: [path])
    tracker.main()
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    assert json.loads(match[1])["changedInputs"]["commit"] == revisions[1]


@pytest.mark.parametrize("author", ["outside-contributor", "other-bot[bot]"])
def test_unowned_marker_issue_is_never_used_or_modified(reporter, author):
    client, _ = reporter
    state = desired(record=vulnerable_record(), observed=candidate())
    forged = {
        "number": 100,
        "state": "open",
        "user": {"login": author, "type": "Bot"},
        "body": tracker.body("graphviz", state),
    }
    client.items.append(forged)
    tracker.main()
    assert client.calls[0][1] == "POST"
    assert client.items[0] == forged
    assert len(client.items) == 2


@pytest.mark.parametrize("closed_predecessor", [False, True])
def test_uncertain_issue_creation_is_reconciled_without_duplicate(
    reporter, monkeypatch, closed_predecessor
):
    client, current = reporter
    if closed_predecessor:
        tracker.main()
        client.items[0]["state"] = "closed"
        current["record"]["latestAttempt"]["findings"].append(FINDING | {"id": "CVE-new"})
    original = client.request

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("Acknowledgement lost")

    monkeypatch.setattr(client, "request", uncertain)
    with pytest.raises(TimeoutError):
        tracker.main()
    monkeypatch.setattr(client, "request", original)
    tracker.main()
    assert len(client.items) == 1
    assert len(client.calls) == (2 if closed_predecessor else 1)
    assert client.items[0]["state"] == "open"


def test_unavailable_baseline_preserves_existing_request(reporter, monkeypatch):
    client, _ = reporter
    tracker.main()

    def unavailable(*_):
        raise ValueError("Missing signed evidence")

    monkeypatch.setattr(tracker, "baseline", unavailable)
    with pytest.raises(ValueError, match="signed evidence"):
        tracker.main()
    assert len(client.calls) == 1
    assert client.items[0]["state"] == "open"


def test_concurrent_release_completion_refuses_issue_mutation(reporter, monkeypatch):
    client, current = reporter
    count = 0

    def head(_self):
        nonlocal count
        count += 1
        return SimpleNamespace(
            reference=str(count),
            catalogue={
                "current": {"graphviz": "graphviz/current"},
                "releases": {"graphviz/current": current["record"]},
            },
        )

    monkeypatch.setattr(tracker, "package_requirements", lambda _: {})
    monkeypatch.setattr(tracker.History, "head", head)
    with pytest.raises(ValueError, match="changed during"):
        tracker.main()
    assert not client.calls


def test_dry_run_does_not_write_issues(reporter, monkeypatch):
    client, _ = reporter
    monkeypatch.setattr(sys, "argv", ["image_release_needed.py", "--dry-run"])
    tracker.main()
    assert not client.calls


@pytest.fixture
def scan_artifact(monkeypatch):
    run = {
        "id": 123,
        "head_branch": "main",
        "head_repository": {"full_name": tracker.REPOSITORY},
        "head_sha": SOURCE,
        "event": "schedule",
        "path": ".github/workflows/image-security.yml",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "updated_at": "2026-01-02T00:00:00Z",
    }
    artifact = {
        "name": "image-security-graphviz",
        "expired": False,
        "created_at": "2026-01-01T23:59:30Z",
    }
    files = {
        "build.json": {
            "profile": "graphviz",
            "source_commit": SOURCE,
            "local_image_id": RECORD["imageId"],
            "run_id": "123",
            "run_attempt": "1",
            "collected_at": "2026-01-01T23:59:15Z",
        },
        "sbom.syft.json": {
            "artifacts": [{"name": "graphviz", "type": "apk", "version": "2"}],
            "source": {"type": "image", "metadata": {"imageID": RECORD["imageId"]}},
        },
        "grype.json": {
            "source": {"type": "image", "target": {"imageID": RECORD["imageId"]}},
            "descriptor": {
                "db": {
                    "status": {
                        "valid": True,
                        "built": "2026-01-01T00:00:00Z",
                        "schemaVersion": "v6",
                        "from": "https://example.com/db",
                    }
                }
            },
            "matches": [],
        },
    }

    def request(_self, endpoint):
        if "/workflows/image-security.yml/runs?" in endpoint:
            return encode({"total_count": 1, "workflow_runs": [run]})
        if "/jobs?" in endpoint:
            return encode({"total_count": 1, "jobs": [profile_job(run)]})
        return encode(
            {"total_count": 1, "artifacts": [artifact]} if "/artifacts?" in endpoint else run
        )

    def download(command, **_kwargs):
        path = Path(command[-1])
        path.mkdir(parents=True)
        for name, value in files.items():
            (path / name).write_bytes(encode(value))

    monkeypatch.setattr(GitHub, "request", request)
    monkeypatch.setattr(tracker.subprocess, "run", download)
    return run, artifact, files


def profile_job(run, **updates):
    return {
        "id": run["id"],
        "run_id": run["id"],
        "run_attempt": 1,
        "name": "Image security (graphviz)",
        "status": "completed",
        "started_at": "2026-01-01T23:59:00Z",
        "completed_at": run["updated_at"],
    } | updates


@pytest.fixture(params=tuple(tracker.BICEP_ROUTES))
def bicep_scan(scan_artifact, monkeypatch, request):
    run, artifact, files = scan_artifact
    profile = request.param
    artifact["name"] = f"image-security-{profile}"
    files["build.json"]["profile"] = profile
    from bicep_source import policy

    packages = policy(ROOT / "images/bicep-sandbox/source.json")["packages"]
    files["runtime.json"] = {
        "profile": profile,
        "imageId": RECORD["imageId"],
        "bicep": {"build": {"route": "source", "packages": packages}},
    }
    files["sbom.syft.json"]["artifacts"] += [
        {"type": "dotnet", "name": name, "version": version} for name, version in packages.items()
    ]
    original = GitHub.request

    def metadata(self, endpoint):
        if "/jobs?" in endpoint:
            return encode(
                {"total_count": 1, "jobs": [profile_job(run, name=f"Image security ({profile})")]}
            )
        return original(self, endpoint)

    monkeypatch.setattr(GitHub, "request", metadata)
    return profile, run, files


@pytest.mark.parametrize("route", ["upstream", "source"])
def test_candidate_route_is_bound_to_retained_evidence(bicep_scan, tmp_path, route):
    profile, _, files = bicep_scan
    files["runtime.json"]["bicep"]["build"]["route"] = route
    accepted = tracker.candidates(GitHub(), "123", tmp_path / "current", profiles=(profile,))
    assert bool(accepted) == (route == "source")
    original = tracker.candidates(
        GitHub(), "123", tmp_path / "saved", historical=True, profiles=(profile,)
    )
    assert original[profile]["bicepRoute"] == route


@pytest.mark.parametrize("historical,profiles", [(False, ("bicep",)), (True, ("graphviz",))])
def test_route_only_evidence_cannot_be_used_as_replacement_candidate(
    tmp_path, historical, profiles
):
    with pytest.raises(ValueError, match="restricted to historical Bicep ordering"):
        tracker.candidates(
            GitHub(), "123", tmp_path, historical=historical, profiles=profiles, route_only=True
        )


@pytest.mark.parametrize("failure", ["missing", "image", "profile", "route", "packages"])
def test_unverified_route_cannot_become_a_candidate(bicep_scan, tmp_path, failure):
    profile, run, files = bicep_scan
    if failure == "missing":
        del files["runtime.json"]
    elif failure in {"image", "profile"}:
        files["runtime.json"]["imageId" if failure == "image" else "profile"] = "wrong"
    else:
        files["runtime.json"]["bicep"]["build"].pop(failure)
    with pytest.raises(ValueError, match="valid candidate evidence"):
        tracker.candidates(GitHub(), "123", tmp_path / "success", profiles=(profile,))
    run["conclusion"] = "failure"
    assert tracker.candidates(GitHub(), "123", tmp_path / "failure", profiles=(profile,)) == {}


@pytest.mark.parametrize("route", ["upstream", "source", None])
def test_diagnostic_scan_does_not_supersede_a_replacement_scan(monkeypatch, tmp_path, route):
    run = {
        "id": 123,
        "head_branch": "main",
        "head_repository": {"full_name": tracker.REPOSITORY},
        "event": "workflow_dispatch",
        "path": ".github/workflows/image-security.yml",
        "status": "completed",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    newer = run | {"id": 124, "updated_at": "2026-01-03T00:00:00Z"}
    profile = "bicep-docker-sbx"

    def listing(_client, _endpoint, field):
        return (
            [run, newer]
            if field == "workflow_runs"
            else [profile_job(newer, name=f"Image security ({profile})")]
        )

    def evidence(_client, run_id, _directory, *, historical, profiles, route_only):
        assert historical and route_only and profiles == (profile,) and run_id == "124"
        return {profile: {"bicepRoute": route}} if route else {}

    monkeypatch.setattr(tracker, "action_listing", listing)
    monkeypatch.setattr(tracker, "candidates", evidence)
    superseded = tracker.newer_profiles(GitHub(), run, {profile: run["updated_at"]})
    assert superseded == (set() if route == "upstream" else {profile})


@pytest.mark.parametrize("newer_scan", [False, True])
@pytest.mark.parametrize("route", ["upstream", "source"])
@pytest.mark.parametrize("missing", ["grype.json", "runtime.json", "sbom.syft.json", "build.json"])
def test_scan_ordering_uses_route_evidence_during_scanner_outage(
    bicep_scan, monkeypatch, tmp_path, newer_scan, route, missing
):
    profile, run, files = bicep_scan
    run["conclusion"] = "failure"
    files["runtime.json"]["bicep"]["build"]["route"] = route
    files.pop(missing)
    earlier = run | {"id": 122, "updated_at": "2026-01-01T00:00:00Z"}
    original = GitHub.request

    def history(self, endpoint):
        if "/workflows/" in endpoint:
            return encode({"total_count": 2, "workflow_runs": [earlier, run]})
        if newer_scan and "/jobs?" in endpoint:
            return encode(
                {
                    "total_count": 2,
                    "jobs": [
                        profile_job(run, name=f"Image security ({profile})"),
                        profile_job(run, id=999, run_attempt=2, name=f"Image security ({profile})"),
                    ],
                }
            )
        return original(self, endpoint)

    monkeypatch.setattr(GitHub, "request", history)
    superseded = tracker.newer_profiles(GitHub(), earlier, {profile: earlier["updated_at"]})
    diagnostic = route == "upstream" and missing == "grype.json" and not newer_scan
    assert superseded == (set() if diagnostic else {profile})
    assert tracker.candidates(GitHub(), "123", tmp_path / "candidate", profiles=(profile,)) == {}
    assert (
        tracker.candidates(
            GitHub(), "123", tmp_path / "legacy", historical=True, profiles=(profile,)
        )
        == {}
    )


@pytest.mark.parametrize("newer_scan", [False, True])
def test_partial_rerun_does_not_refresh_retained_profile_evidence(
    scan_artifact, monkeypatch, tmp_path, newer_scan
):
    run, artifact, _ = scan_artifact
    producer = profile_job(run)
    run.update(run_attempt=2, updated_at="2026-01-04T00:00:00Z")
    rerun = profile_job(run, id=999, run_attempt=2, name="Image security (bicep)")
    other = run | {"id": 124, "run_attempt": 1, "updated_at": "2026-01-03T00:00:00Z"}
    original = GitHub.request

    def request(self, endpoint):
        if "/workflows/" in endpoint:
            rows = [run, other] if newer_scan else [run]
            return encode({"total_count": len(rows), "workflow_runs": rows})
        if "/runs/123/jobs?" in endpoint:
            return encode({"total_count": 2, "jobs": [producer, rerun]})
        if "/runs/124/jobs?" in endpoint:
            return encode({"total_count": 1, "jobs": [profile_job(other)]})
        if "/runs/124/artifacts?" in endpoint:
            return encode({"total_count": 1, "artifacts": [artifact]})
        return original(self, endpoint)

    monkeypatch.setattr(GitHub, "request", request)
    result = tracker.candidates(GitHub(), "123", tmp_path)
    if newer_scan:
        assert result == {}
    else:
        assert result["graphviz"]["observedAt"] == producer["completed_at"]
        assert result["graphviz"]["evidence"].endswith("/attempts/1")


def test_paths_and_package_reasons_cannot_escape_issue_code_spans():
    path = "images/graphviz-sandbox/a` @unexpected **bold**.txt"
    requirement = REQUIREMENT | {"reason": "a` @unexpected **bold**"}
    text = tracker.body("graphviz", desired(changed=[path], requirements=[requirement]))
    assert f"`` {path} ``" in text
    assert "`` a` @unexpected **bold** ``" in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "other"),
        ("run_attempt", "2"),
        ("run_attempt", "0"),
        ("collected_at", "2026-01-03T00:00:00Z"),
    ],
)
def test_candidate_attempt_identity_must_match_a_producing_job(
    scan_artifact, tmp_path, field, value
):
    _, _, files = scan_artifact
    files["build.json"][field] = value
    with pytest.raises(ValueError, match="valid candidate evidence"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_artifact_creation_must_fall_within_its_producing_job(scan_artifact, tmp_path):
    _, artifact, _ = scan_artifact
    artifact["created_at"] = "2026-01-03T00:00:00Z"
    with pytest.raises(ValueError, match="valid candidate evidence"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_failed_rerun_of_same_profile_supersedes_its_retained_artifact(
    scan_artifact, monkeypatch, tmp_path
):
    run, _, _ = scan_artifact
    producer = profile_job(run)
    run.update(run_attempt=2, updated_at="2026-01-03T00:00:00Z", conclusion="failure")
    failed = profile_job(run, id=999, run_attempt=2)
    original = GitHub.request

    def request(self, endpoint):
        if "/jobs?" in endpoint:
            return encode({"total_count": 2, "jobs": [producer, failed]})
        return original(self, endpoint)

    monkeypatch.setattr(GitHub, "request", request)
    assert tracker.candidates(GitHub(), "123", tmp_path) == {}


def test_candidate_reports_are_validated_against_source_and_image(scan_artifact, tmp_path):
    result = tracker.candidates(GitHub(), "123", tmp_path)
    assert result["graphviz"]["assessment"]["outcome"] == "clean"
    assert result["graphviz"]["components"] == [["apk", "graphviz", "2", ""]]


@pytest.mark.parametrize("file", ["grype.json", "sbom.syft.json", "build.json"])
def test_missing_successful_scan_evidence_fails_tracking(scan_artifact, tmp_path, file):
    _, _, files = scan_artifact
    files.pop(file)
    with pytest.raises(ValueError, match="valid candidate evidence"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_failed_scanner_is_not_a_replacement_observation(scan_artifact, tmp_path):
    run, _, files = scan_artifact
    run["conclusion"] = "failure"
    files.pop("grype.json")
    assert tracker.candidates(GitHub(), "123", tmp_path) == {}


def test_candidate_from_another_source_is_not_accepted(scan_artifact, tmp_path):
    _, _, files = scan_artifact
    files["build.json"]["source_commit"] = "f" * 40
    with pytest.raises(ValueError, match="valid candidate evidence"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_expired_scan_artifact_does_not_erase_release_reasons(scan_artifact, tmp_path):
    _, artifact, _ = scan_artifact
    artifact["expired"] = True
    with pytest.raises(ValueError, match="expired"):
        tracker.candidates(GitHub(), "123", tmp_path)


def test_no_issue_observation_still_prevents_older_scan_creating_request(
    scan_artifact, monkeypatch, tmp_path
):
    run, artifact, files = scan_artifact
    run["updated_at"] = "2026-01-04T00:00:00Z"
    observed = tracker.candidates(GitHub(), "123", tmp_path / "newer")["graphviz"]
    released = RELEASED | {"components": observed["components"]}
    assert optional_desired(observed=observed, released=released) is None
    newer = copy.deepcopy(run)
    run.update(id=122, updated_at="2026-01-03T00:00:00Z")
    files["build.json"]["run_id"] = "122"
    files["sbom.syft.json"]["artifacts"][0]["version"] = "3"

    def request(_self, endpoint):
        if "/workflows/image-security.yml/runs?" in endpoint:
            return encode({"total_count": 2, "workflow_runs": [newer, run]})
        if "/jobs?" in endpoint:
            return encode(
                {
                    "total_count": 1,
                    "jobs": [profile_job(run if "/runs/122/" in endpoint else newer)],
                }
            )
        if "/artifacts?" in endpoint:
            return encode({"total_count": 1, "artifacts": [artifact]})
        return encode(run)

    monkeypatch.setattr(GitHub, "request", request)
    older = tracker.candidates(GitHub(), "122", tmp_path / "older")
    assert optional_desired(observed=older.get("graphviz"), released=released) is None


@pytest.mark.parametrize(
    "updates,profile,expected",
    [
        ({}, "graphviz", {"graphviz"}),
        ({"updated_at": "2026-01-02T00:00:00Z"}, "graphviz", {"graphviz"}),
        ({"updated_at": "2026-01-01T00:00:00Z"}, "graphviz", set()),
        ({"conclusion": "failure"}, "graphviz", {"graphviz"}),
        ({}, "bicep", {"bicep"}),
        ({"head_branch": "topic"}, "graphviz", set()),
        ({"head_repository": {"full_name": "other/repo"}}, "graphviz", set()),
        ({"status": "in_progress"}, "graphviz", set()),
        ({"event": "pull_request"}, "graphviz", set()),
        ({"path": ".github/workflows/other.yml"}, "graphviz", set()),
    ],
)
def test_scan_history_supersession_is_scoped_to_trusted_completed_profiles(
    scan_artifact, monkeypatch, updates, profile, expected
):
    run, _, _ = scan_artifact
    newer = run | {"id": 124, "updated_at": "2026-01-03T00:00:00Z"} | updates

    def request(_self, endpoint):
        if "/workflows/" in endpoint:
            return encode({"total_count": 2, "workflow_runs": [run, newer]})
        if endpoint.endswith("/runs/124"):
            return encode(newer)
        if "/runs/124/artifacts?" in endpoint:
            return encode({"total_count": 0, "artifacts": []})
        assert endpoint.endswith("/runs/124/jobs?filter=all&per_page=100&page=1")
        return encode(
            {
                "total_count": 1,
                "jobs": [profile_job(newer, name=f"Image security ({profile})")],
            }
        )

    monkeypatch.setattr(GitHub, "request", request)
    assert (
        tracker.newer_profiles(GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES})
        == expected
    )


@pytest.mark.parametrize("damage", ["none", "missing", "duplicate", "changed-count"])
def test_scan_history_paginates_and_rejects_incomplete_ordering(scan_artifact, monkeypatch, damage):
    run, _, _ = scan_artifact
    first = [run | {"id": index, "status": "in_progress"} for index in range(200, 300)]
    seen = []

    def request(_self, endpoint):
        seen.append(endpoint)
        page = int(endpoint.rsplit("=", 1)[1])
        if page == 1:
            return encode({"total_count": 101, "workflow_runs": first})
        assert page == 2
        rows = [] if damage == "missing" else [first[0] if damage == "duplicate" else run]
        return encode(
            {"total_count": 102 if damage == "changed-count" else 101, "workflow_runs": rows}
        )

    monkeypatch.setattr(GitHub, "request", request)
    if damage == "none":
        assert (
            tracker.newer_profiles(GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES})
            == set()
        )
    else:
        with pytest.raises(ValueError, match="history"):
            tracker.newer_profiles(GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES})
    assert len(seen) == 2


@pytest.mark.parametrize(
    "failed_step", ["Build the selected image", "Record the immutable local image identity"]
)
def test_failed_profile_without_artifact_supersedes_older_scan(
    scan_artifact, monkeypatch, tmp_path, failed_step
):
    run, artifact, _ = scan_artifact
    newer = run | {"id": 124, "updated_at": "2026-01-03T00:00:00Z", "conclusion": "failure"}

    def request(_self, endpoint):
        if "/workflows/" in endpoint:
            return encode({"total_count": 2, "workflow_runs": [newer, run]})
        if "/runs/124/artifacts?" in endpoint:
            return encode({"total_count": 0, "artifacts": []})
        if "/runs/123/jobs?" in endpoint:
            return encode({"total_count": 1, "jobs": [profile_job(run)]})
        if "/runs/124/jobs?" in endpoint:
            return encode(
                {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 77,
                            "name": "Image security (graphviz)",
                            "completed_at": newer["updated_at"],
                            "steps": [{"name": failed_step, "conclusion": "failure"}],
                        }
                    ],
                }
            )
        if "/artifacts?" in endpoint:
            return encode({"total_count": 1, "artifacts": [artifact]})
        return encode(run)

    monkeypatch.setattr(GitHub, "request", request)
    observed = tracker.candidates(GitHub(), "123", tmp_path)
    assert optional_desired(observed=observed.get("graphviz")) is None


@pytest.mark.parametrize("damage", ["none", "missing", "duplicate", "changed-count", "empty"])
def test_profile_job_history_is_complete_across_all_attempts(scan_artifact, monkeypatch, damage):
    run, _, _ = scan_artifact
    newer = run | {"id": 124, "updated_at": "2026-01-03T00:00:00Z"}
    first = [{"id": index, "name": "Select image security profiles"} for index in range(100)]

    def request(_self, endpoint):
        if "/workflows/" in endpoint:
            return encode({"total_count": 2, "workflow_runs": [newer, run]})
        assert "/runs/124/jobs?filter=all&per_page=100&page=" in endpoint
        if damage == "empty":
            return encode({"total_count": 0, "jobs": []})
        if endpoint.endswith("page=1"):
            return encode({"total_count": 101, "jobs": first})
        assert endpoint.endswith("page=2")
        last = first[0] if damage == "duplicate" else profile_job(newer, id=101)
        return encode(
            {
                "total_count": 102 if damage == "changed-count" else 101,
                "jobs": [] if damage == "missing" else [last],
            }
        )

    monkeypatch.setattr(GitHub, "request", request)
    if damage == "none":
        assert tracker.newer_profiles(
            GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES}
        ) == {"graphviz"}
    else:
        with pytest.raises(ValueError, match="history"):
            tracker.newer_profiles(GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES})


@pytest.mark.parametrize("observed_at", ["2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"])
def test_older_or_tied_candidate_cannot_undo_replacement_resolution(observed_at):
    previous = desired(record=vulnerable_record(), observed=candidate())
    result = desired(
        record=replacement(),
        released=RELEASED | {"components": candidate()["components"]},
        observed=candidate(version="3", observedAt=observed_at),
        previous=previous,
    )
    assert result["resolved"]


def test_delayed_candidate_cannot_rewind_newer_inventory_or_findings():
    newer = candidate(version="3", outcome="vulnerable", observedAt="2026-01-04T00:00:00Z")
    previous = desired(record=vulnerable_record(), observed=newer)
    result = desired(record=vulnerable_record(), observed=candidate(), previous=previous)
    assert result == previous


@pytest.mark.parametrize("first,second", [("clean", "vulnerable"), ("vulnerable", "clean")])
def test_equal_timestamp_candidate_cannot_replace_persisted_state(first, second):
    client = Issues()
    previous = desired(record=vulnerable_record(), observed=candidate(outcome=first))
    tracker.reconcile(client, "graphviz", previous, None)
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    saved = json.loads(match[1])
    result = desired(
        record=vulnerable_record(), observed=candidate(version="3", outcome=second), previous=saved
    )
    tracker.reconcile(client, "graphviz", result, client.items[0])
    assert result == saved
    assert len(client.calls) == 1


def test_retries_and_unchanged_daily_scans_do_not_duplicate_issues_or_comments():
    client = Issues()
    state = desired(record=vulnerable_record(), observed=candidate())
    tracker.reconcile(client, "graphviz", state, None)
    again = desired(
        record=vulnerable_record(),
        observed=candidate(evidence="run/2", observedAt="2026-01-02T00:00:01Z"),
        previous=state,
    )
    tracker.reconcile(client, "graphviz", again, client.items[0])
    assert len(client.items) == 1
    assert len(client.calls) == 2
    assert "run/2" in client.items[0]["body"]
    assert client.calls[-1][1] == "PATCH"
    tracker.reconcile(client, "graphviz", again, client.items[0])
    assert len(client.calls) == 2
    changed = desired(
        observed=candidate(version="3", observedAt="2026-01-02T00:00:02Z"), previous=again
    )
    tracker.reconcile(client, "graphviz", changed, client.items[0])
    assert len(client.calls) == 3
    assert client.calls[-1][1] == "PATCH"


def test_missing_issue_state_refuses_mutation():
    client = Issues()
    with pytest.raises(ValueError, match="recoverable"):
        tracker.reconcile(
            client,
            "graphviz",
            desired(record=vulnerable_record(), observed=candidate()),
            {"body": "edited", "number": 1},
        )
    assert not client.calls


@pytest.mark.parametrize("ending", ["-->", "--!>"])
def test_issue_state_cannot_break_its_html_comment(ending):
    result = desired(record=vulnerable_record(), changed=[f"images/graphviz-sandbox/{ending}file"])
    match = tracker.STATE.search(tracker.body("graphviz", result))
    assert match is not None
    assert ending not in match[1]
    assert json.loads(match[1]) == result
