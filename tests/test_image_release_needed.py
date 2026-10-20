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


def optional_desired(*, record=None, released=None, changed=None, observed=None, previous=None):
    return tracker.desired(
        record or RECORD, released or RELEASED, changed or [], SOURCE, observed, previous
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


def test_component_changes_create_request_with_reviewable_delta():
    result = desired(observed=candidate())
    assert result["reasons"]["components"]["added"][0][2] == "2"
    assert result["reasons"]["components"]["removed"][0][2] == "1"
    assert not result["resolved"]


@pytest.fixture(params=tuple(tracker.BICEP_ROUTES))
def bicep_request(request):
    record = RECORD | {"profile": request.param}
    released = RELEASED | {"bicepRoute": "source"}
    observed = candidate(
        outcome="vulnerable",
        bicepRoute="upstream",
        evidence=f"https://github.com/{tracker.REPOSITORY}/actions/runs/123/attempts/1",
    )
    previous = desired(observed=observed)
    return record, released, observed, previous


def test_diagnostic_route_cannot_request_a_replacement(bicep_request):
    record, released, observed, _ = bicep_request
    assert optional_desired(record=record, released=released, observed=observed) is None
    result = desired(record=record, released=released, observed=observed | {"bicepRoute": "source"})
    reason = result["reasons"]["components"]
    assert reason["bicepRoute"] == "source"
    assert reason["evidence"] == observed["evidence"]
    assert reason["observedAt"] == observed["observedAt"]


def test_explicit_policy_migration_requires_replacement(bicep_request):
    record, released, _, _ = bicep_request
    result = desired(record=record, released=released | {"bicepRoute": "upstream"})
    assert result["reasons"] == {"route": {"published": "upstream", "replacement": "source"}}
    assert "Bicep replacement route policy" in tracker.body(record["profile"], result)
    cleared = desired(record=record, released=released, previous=result)
    assert cleared["resolved"]


@pytest.mark.parametrize("remaining", [None, "source", "vulnerabilities"])
def test_verified_diagnostic_requirement_is_removed_without_losing_other_reasons(
    bicep_request, monkeypatch, tmp_path, remaining
):
    record, released, observed, previous = bicep_request
    if remaining == "vulnerabilities":
        previous["reasons"]["vulnerabilities"] = {
            "baselineDigest": record["registryDigest"],
            "findings": [FINDING],
            "evidence": "monitor",
        }
    monkeypatch.setattr(tracker, "candidates", lambda *_, **__: {record["profile"]: observed})
    repaired = tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
    assert repaired is not None
    assert "components" in previous["reasons"]
    result = desired(
        record=record,
        released=released,
        previous=repaired,
        changed=["images/bicep-sandbox/source.json"] if remaining == "source" else [],
    )
    assert set(result["reasons"]) == ({remaining} if remaining else set())
    assert result["resolved"] == (remaining is None)
    assert "candidateFindings" not in result
    if remaining is None:
        assert result["resolution"] == "route-reconciled"
        assert result["currentDigest"] == record["registryDigest"]
        assert "No replacement release is required" in tracker.body(record["profile"], result)
        client = Issues()
        tracker.reconcile(client, record["profile"], previous, None)
        tracker.reconcile(client, record["profile"], result, client.items[0])
        tracker.reconcile(client, record["profile"], result, client.items[0])
        assert len(client.calls) == 2
        assert client.items[0]["state_reason"] == "not_planned"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "unavailable",
        "attempt",
        "time",
        "hash",
        "added",
        "removed",
        "baseline",
        "url",
    ],
)
def test_unproven_component_requirement_is_preserved(
    bicep_request, monkeypatch, tmp_path, mutation
):
    record, released, observed, previous = bicep_request
    if mutation == "attempt":
        observed["evidence"] = observed["evidence"].removesuffix("1") + "2"
    elif mutation == "time":
        observed["observedAt"] = "2026-01-03T00:00:00Z"
    elif mutation == "hash":
        previous["reasons"]["components"]["inventorySha256"] = "wrong"
    elif mutation in {"added", "removed"}:
        previous["reasons"]["components"][mutation] = []
    elif mutation == "baseline":
        record = replacement() | {"profile": record["profile"]}
    elif mutation == "url":
        previous["candidateEvidence"] = "https://example.com/actions/runs/123/attempts/1"

    def fetch(*_, **__):
        if mutation == "unavailable":
            raise ValueError("Expired evidence")
        return {} if mutation == "missing" else {record["profile"]: observed}

    monkeypatch.setattr(tracker, "candidates", fetch)
    assert (
        tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
        == previous
    )


def test_matching_scan_after_invalid_candidate_does_not_hide_original_requirement(
    bicep_request, monkeypatch, tmp_path
):
    record, released, observed, previous = bicep_request
    previous["candidateEvidence"] = "https://example.com/later"
    previous["candidateObservedAt"] = "2026-01-03T00:00:00Z"
    previous["reasons"]["components"].update(
        bicepRoute="upstream", evidence=observed["evidence"], observedAt=observed["observedAt"]
    )
    monkeypatch.setattr(tracker, "candidates", lambda *_, **__: {record["profile"]: observed})
    repaired = tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
    assert repaired is not None and "components" not in repaired["reasons"]
    assert repaired["candidateEvidence"] == previous["candidateEvidence"]


def test_legacy_valid_requirement_gains_route_provenance_and_remains_pending(
    bicep_request, monkeypatch, tmp_path
):
    record, released, observed, previous = bicep_request
    observed["bicepRoute"] = "source"
    monkeypatch.setattr(tracker, "candidates", lambda *_, **__: {record["profile"]: observed})
    repaired = tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
    assert repaired is not None
    assert repaired["reasons"]["components"]["bicepRoute"] == "source"
    result = desired(
        record=record,
        released=released,
        previous=repaired,
        observed=observed
        | {"components": released["components"], "observedAt": "2026-01-03T00:00:00Z"},
    )
    assert not result["resolved"]
    assert result["reasons"]["components"] == repaired["reasons"]["components"]


@pytest.mark.parametrize("update", ["candidate", "release"])
def test_legacy_provenance_survives_outage_and_metadata_updates(
    bicep_request, monkeypatch, tmp_path, update
):
    record, released, observed, previous = bicep_request

    def unavailable(*_, **__):
        raise OSError("Temporary download failure")

    monkeypatch.setattr(tracker, "candidates", unavailable)
    retained = tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
    assert retained == previous
    if update == "release":
        previous["baseline"] = "sha256:" + "e" * 64
        previous["currentDigest"] = previous["baseline"]
    latest = candidate(
        components=released["components"],
        bicepRoute="source",
        evidence=f"https://github.com/{tracker.REPOSITORY}/actions/runs/124/attempts/1",
        observedAt="2026-01-03T00:00:00Z",
    )
    result = desired(
        record=record,
        released=released,
        previous=retained,
        observed=latest if update == "candidate" else None,
    )
    reason = result["reasons"]["components"]
    assert reason["evidence"] == observed["evidence"]
    assert reason["observedAt"] == observed["observedAt"]
    assert "bicepRoute" not in reason
    assert "evidence" not in previous["reasons"]["components"]
    assert result.get("candidateEvidence") == (
        latest["evidence"] if update == "candidate" else None
    )

    def recovered(_client, run_id, *_args, **_kwargs):
        assert run_id == "123"
        return {record["profile"]: observed}

    monkeypatch.setattr(tracker, "candidates", recovered)
    repaired = tracker.reconcile_component_route(GitHub(), record, released, result, tmp_path)
    assert repaired is not None and "components" not in repaired["reasons"]
    assert desired(record=record, released=released, previous=repaired)["resolved"]


def test_route_withdrawal_after_release_uses_current_digest_proof(
    bicep_request, monkeypatch, tmp_path
):
    record, released, observed, previous = bicep_request
    previous["baseline"] = "sha256:" + "e" * 64
    monkeypatch.setattr(tracker, "candidates", lambda *_, **__: {record["profile"]: observed})
    repaired = tracker.reconcile_component_route(GitHub(), record, released, previous, tmp_path)
    assert repaired is not None and "components" not in repaired["reasons"]
    result = desired(record=record, released=released, previous=repaired)
    assert result["resolution"] == "route-reconciled"
    assert "No replacement release is required" in tracker.body(record["profile"], result)
    assert "Resolved by the completed" not in tracker.body(record["profile"], result)
    client = Issues()
    tracker.reconcile(client, record["profile"], previous, None)
    tracker.reconcile(client, record["profile"], result, client.items[0])
    assert client.items[0]["state_reason"] == "not_planned"
    later = replacement() | {"profile": record["profile"]}
    stale = desired(record=later, released=released, previous=repaired)
    assert "resolution" not in stale
    assert stale["status"] == "Resolved by the completed, verified replacement release"


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


@pytest.mark.parametrize("route", ["upstream", "source"])
@pytest.mark.parametrize("damage", [None, "missing", "ambiguous", "unverified", "delta", "time"])
def test_old_component_route_is_reconciled_against_authenticated_original_release(
    bicep_request, monkeypatch, tmp_path, route, damage
):
    original, released, observed, previous = bicep_request
    observed["bicepRoute"] = route
    current = replacement() | {"profile": original["profile"]}
    releases = {"original": original, "current": current}
    if damage == "missing":
        del releases["original"]
    elif damage == "ambiguous":
        releases["duplicate"] = original
    elif damage == "delta":
        observed["components"] = []
    elif damage == "time":
        observed["observedAt"] = original["completedAt"]
    fetched = []

    def verified(_client, record, _directory):
        fetched.append(record)
        if damage == "unverified":
            raise ValueError("Unverified historical release")
        return released

    monkeypatch.setattr(tracker, "baseline", verified)
    monkeypatch.setattr(tracker, "action_listing", lambda *_: [])
    monkeypatch.setattr(tracker, "candidates", lambda *_, **__: {original["profile"]: observed})
    latest = released | {"components": [["apk", "graphviz", "3", "pkg:apk/graphviz@3"]]}
    repaired = tracker.reconcile_component_route(
        GitHub(), current, latest, previous, tmp_path, releases=releases
    )
    assert repaired is not None
    if damage:
        assert repaired == previous
    elif route == "source":
        assert repaired["reasons"]["components"]["bicepRoute"] == "source"
        assert not desired(record=current, released=latest, previous=repaired)["resolved"]
    else:
        assert fetched == [original]
        assert "components" not in repaired["reasons"]
        assert repaired["routeReconciliation"]["comparisonDigest"] == original["registryDigest"]
        result = desired(record=current, released=latest, previous=repaired)
        assert result["resolved"]
        assert result["resolution"] == "route-reconciled"


@pytest.mark.parametrize(
    "damage", [None, "missing", "duplicate", "hash", "time", "untrusted", "incomplete"]
)
def test_legacy_scan_link_recovery_requires_unique_exact_evidence(
    bicep_request, monkeypatch, tmp_path, damage
):
    original, released, observed, previous = bicep_request
    current = replacement() | {"profile": original["profile"]}
    previous["reasons"]["components"].update(
        evidence=f"https://github.com/{tracker.REPOSITORY}/actions/runs/999/attempts/1",
        observedAt=observed["observedAt"],
    )
    run = {
        "id": 123,
        "head_repository": {"full_name": tracker.REPOSITORY},
        "head_branch": "main",
        "path": ".github/workflows/image-security.yml",
        "event": "schedule",
        "status": "completed",
        "created_at": "2026-01-01T23:00:00Z",
        "updated_at": "2026-01-02T01:00:00Z",
    }
    if damage == "hash":
        observed["components"] = []
    elif damage == "time":
        observed["observedAt"] = "2026-01-02T00:00:01Z"
    elif damage == "untrusted":
        run["head_branch"] = "untrusted"

    def history(*_):
        if damage == "incomplete":
            raise ValueError("Incomplete scan history")
        return (
            []
            if damage == "missing"
            else [run, run | {"id": 124}]
            if damage == "duplicate"
            else [run]
        )

    def scans(_client, run_id, *_args, **_kwargs):
        return {
            original["profile"]: observed
            | {
                "bicepRoute": "source",
                "observedAt": "2026-01-04T00:00:00Z",
                "evidence": previous["reasons"]["components"]["evidence"],
            }
            if run_id == "999"
            else observed
        }

    monkeypatch.setattr(tracker, "baseline", lambda *_: released)
    monkeypatch.setattr(tracker, "action_listing", history)
    monkeypatch.setattr(tracker, "candidates", scans)
    repaired = tracker.reconcile_component_route(
        GitHub(), current, released, previous, tmp_path, releases={"original": original}
    )
    assert repaired is not None
    if damage:
        assert repaired == previous
    else:
        assert "components" not in repaired["reasons"]
        assert repaired["routeReconciliation"]["evidence"] == observed["evidence"]
        assert desired(record=current, released=released, previous=repaired)["resolved"]


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


def test_vulnerable_candidate_marks_release_request_blocked():
    result = desired(observed=candidate(outcome="vulnerable"))
    assert result["status"] == "Refresh blocked on candidate remediation"
    assert "CVE-2026-1234" in tracker.body("graphviz", result)


@pytest.mark.parametrize("outcome", ["clean", "vulnerable"])
@pytest.mark.parametrize("repeat_candidate", [False, True])
@pytest.mark.parametrize("previously_escalated", [False, True])
def test_refresh_escalation_preserves_candidate_assessment(
    outcome, repeat_candidate, previously_escalated
):
    observed = candidate(outcome=outcome)
    previous = desired(observed=observed)
    record = RECORD | {
        "latestAttempt": {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "image-monitor-1-1",
        }
    }
    if previously_escalated:
        previous["status"] = "Awaiting release preparation"
    expected = (
        "Blocked on candidate remediation"
        if outcome == "vulnerable"
        else "Last candidate scan passed; release gate still required"
    )
    for _ in range(2):
        result = desired(
            record=record,
            previous=previous,
            observed=observed if repeat_candidate else None,
        )
        assert tracker.required(result)
        assert result["status"] == expected
        assert result["candidateEvidence"] == observed["evidence"]
        assert result["candidateObservedAt"] == observed["observedAt"]
        assert result["candidateFindings"] == observed["assessment"]["findings"]
        assert expected in tracker.body("graphviz", result)
        previous = result


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


def test_passing_scan_cannot_close_issue_without_replacement():
    previous = desired(changed=["images/graphviz-sandbox/Dockerfile"])
    result = desired(
        changed=["images/graphviz-sandbox/Dockerfile"],
        observed=candidate(version="1"),
        previous=previous,
    )
    assert not result["resolved"]
    assert "source" in result["reasons"]


def test_disproven_source_only_reason_closes_without_claiming_a_release():
    previous = desired(changed=["scripts/build_scan_image.sh"])
    result = desired(previous=previous)
    assert result["resolved"]
    assert result["resolution"] == "source-reconciled"
    assert result["currentDigest"] == RECORD["registryDigest"]
    assert result["reasons"] == {}
    assert "No replacement release is required" in tracker.body("graphviz", result)
    assert "Resolved by the completed" not in tracker.body("graphviz", result)
    client = Issues()
    tracker.reconcile(client, "graphviz", previous, None)
    tracker.reconcile(client, "graphviz", result, client.items[0])
    tracker.reconcile(client, "graphviz", result, client.items[0])
    assert len(client.calls) == 2
    assert client.items[0]["state_reason"] == "not_planned"


@pytest.mark.parametrize("reason", ["components", "vulnerabilities"])
def test_disproven_source_keeps_independent_replacement_requirements(reason):
    record = (
        RECORD
        | {
            "latestAttempt": {
                "outcome": "vulnerable",
                "findings": [FINDING],
                "evidenceRelease": "monitor",
            }
        }
        if reason == "vulnerabilities"
        else RECORD
    )
    previous = desired(
        record=record,
        changed=["scripts/build_scan_image.sh"],
        observed=candidate() if reason == "components" else None,
    )
    result = desired(
        previous=previous,
        observed=candidate(version="1"),
        record=RECORD | {"latestAttempt": {"outcome": "unavailable"}},
    )
    assert not result["resolved"]
    assert "source" not in result["reasons"]
    assert reason in result["reasons"]


def test_replacement_must_address_component_and_source_reasons():
    previous = desired(changed=["images/graphviz-sandbox/Dockerfile"], observed=candidate())
    unresolved = desired(record=replacement(), previous=previous)
    assert not unresolved["resolved"]
    assert "components" in unresolved["reasons"]
    resolved = desired(
        record=replacement(),
        released=RELEASED | {"components": candidate()["components"]},
        previous=previous,
    )
    assert resolved["resolved"]
    assert resolved["reasons"] == {}


@pytest.mark.parametrize("delta", ["upgrade", "addition", "removal"])
def test_replacement_satisfies_component_delta_with_other_inventory_changes(delta):
    unrelated = ["dotnet", "library", "1", "pkg:nuget/library@1"]
    released = RELEASED | {"components": RELEASED["components"] + [unrelated]}
    observed = candidate()
    if delta == "addition":
        observed["components"] += RELEASED["components"]
    elif delta == "removal":
        observed["components"] = []
    observed["components"].append(unrelated)
    previous = desired(released=released, observed=observed)
    replacement_components = [row for row in observed["components"] if row != unrelated]
    replacement_components.append(["dotnet", "library", "2", "pkg:nuget/library@2"])
    result = desired(
        record=replacement(),
        released=RELEASED | {"components": replacement_components},
        previous=previous,
    )
    assert result["resolved"]
    assert result["reasons"] == {}


@pytest.mark.parametrize("field", [0, 1, 2, 3])
def test_replacement_requires_full_identity_of_each_added_component(field):
    previous = desired(observed=candidate())
    inventory = candidate()["components"]
    inventory[0][field] += "-different"
    result = desired(
        record=replacement(), released=RELEASED | {"components": inventory}, previous=previous
    )
    assert not result["resolved"]
    assert result["reasons"]["components"] == previous["reasons"]["components"]


def test_replacement_with_added_component_cannot_retain_removed_component():
    previous = desired(observed=candidate())
    inventory = candidate()["components"] + RELEASED["components"]
    result = desired(
        record=replacement(), released=RELEASED | {"components": inventory}, previous=previous
    )
    assert not result["resolved"]
    assert "components" in result["reasons"]


@pytest.mark.parametrize("blocker", ["same-digest", "source", "vulnerability", "newer-candidate"])
def test_satisfied_component_delta_preserves_independent_release_requirements(blocker):
    previous = desired(observed=candidate())
    record = replacement()
    changed = []
    observed = None
    if blocker == "same-digest":
        record = RECORD
    elif blocker == "source":
        changed = ["images/graphviz-sandbox/Dockerfile"]
    elif blocker == "vulnerability":
        record["latestAttempt"] = {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "monitor",
        }
    else:
        observed = candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    inventory = candidate()["components"] + [["dotnet", "library", "2", "pkg:nuget/library@2"]]
    result = desired(
        record=record,
        released=RELEASED | {"components": inventory},
        changed=changed,
        observed=observed,
        previous=previous,
    )
    assert not result["resolved"]
    reason = {"source": "source", "vulnerability": "vulnerabilities"}.get(blocker, "components")
    assert set(result["reasons"]) == {reason}


@pytest.mark.parametrize("observed_at", ["2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"])
def test_older_or_tied_candidate_cannot_undo_replacement_resolution(observed_at):
    previous = desired(observed=candidate())
    result = desired(
        record=replacement(),
        released=RELEASED | {"components": candidate()["components"]},
        observed=candidate(version="3", observedAt=observed_at),
        previous=previous,
    )
    assert result["resolved"]


def test_delayed_candidate_cannot_rewind_newer_inventory_or_findings():
    newer = candidate(version="3", outcome="vulnerable", observedAt="2026-01-04T00:00:00Z")
    previous = desired(observed=newer)
    result = desired(observed=candidate(), previous=previous)
    assert result == previous


@pytest.mark.parametrize("first,second", [("clean", "vulnerable"), ("vulnerable", "clean")])
def test_equal_timestamp_candidate_cannot_replace_persisted_state(first, second):
    client = Issues()
    previous = desired(observed=candidate(outcome=first))
    tracker.reconcile(client, "graphviz", previous, None)
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    saved = json.loads(match[1])
    result = desired(observed=candidate(version="3", outcome=second), previous=saved)
    tracker.reconcile(client, "graphviz", result, client.items[0])
    assert result == saved
    assert len(client.calls) == 1


def test_new_vulnerability_prevents_closure_after_replacement():
    previous = desired(changed=["images/graphviz-sandbox/Dockerfile"])
    record = replacement() | {
        "latestAttempt": {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "image-monitor-2-1",
        }
    }
    assert not desired(record=record, previous=previous)["resolved"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("reason", ["components", "vulnerabilities"])
def test_requirements_on_replacement_need_another_release(reason, legacy):
    previous = desired(observed=candidate())
    record = replacement()
    released = RELEASED | {"components": candidate()["components"]}
    observed = None
    if reason == "components":
        observed = candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    else:
        record = record | {
            "latestAttempt": {
                "outcome": "vulnerable",
                "findings": [FINDING],
                "evidenceRelease": "monitor/2",
            }
        }
    pending = desired(record=record, released=released, observed=observed, previous=previous)
    if legacy:
        pending["reasons"][reason].pop("baselineDigest", None)
    client = Issues()
    tracker.reconcile(client, "graphviz", pending, None)
    clean = record | {"latestAttempt": {"outcome": "clean"}}
    unchanged = desired(
        record=clean,
        released=released,
        observed=candidate(version="2", observedAt="2026-01-05T00:00:00Z"),
        previous=pending,
    )
    assert not unchanged["resolved"]
    if not legacy:
        assert pending["reasons"][reason]["baselineDigest"] == record["registryDigest"]
    assert unchanged["reasons"] == pending["reasons"]
    tracker.reconcile(client, "graphviz", unchanged, client.items[0])
    assert client.items[0]["state"] == "open"
    assert desired(record=clean, released=released, previous=unchanged) == unchanged
    next_record = clean | {
        "version": "0.1.2",
        "registryDigest": "sha256:" + "e" * 64,
        "completedAt": "2026-01-06T00:00:00Z",
    }
    resolved = desired(
        record=next_record,
        released=RELEASED | {"components": candidate(version="3")["components"]},
        previous=unchanged,
    )
    assert resolved["resolved"]
    tracker.reconcile(client, "graphviz", resolved, client.items[0])
    assert client.items[0]["state"] == "closed"
    assert client.items[0]["state_reason"] == "completed"


def test_matching_candidate_cannot_dismiss_unfulfilled_exact_component_requirement():
    previous = desired(observed=candidate())
    released = RELEASED | {"components": candidate(version="3")["components"]}
    pending = desired(record=replacement(), released=released, previous=previous)
    result = desired(
        record=replacement(),
        released=released,
        observed=candidate(version="3", observedAt="2026-01-04T00:00:00Z"),
        previous=pending,
    )
    assert not result["resolved"]
    assert result["reasons"]["components"] == previous["reasons"]["components"]


def test_legacy_component_requirement_can_close_after_inventory_already_advanced():
    previous = desired(observed=candidate())
    previous["reasons"]["components"].pop("baselineDigest")
    previous["currentDigest"] = replacement()["registryDigest"]
    result = desired(
        record=replacement(),
        released=RELEASED | {"components": candidate()["components"]},
        previous=previous,
    )
    assert result["resolved"]


@pytest.mark.parametrize("replacement_release", [False, True])
@pytest.mark.parametrize("outcome", ["clean", "vulnerable"])
def test_resolved_status_and_body_discard_pending_candidate_details(replacement_release, outcome):
    previous = desired(
        changed=["images/graphviz-sandbox/Dockerfile"],
        observed=candidate(version="1", outcome=outcome),
    )
    result = desired(record=replacement() if replacement_release else RECORD, previous=previous)
    expected = (
        "Resolved by the completed, verified replacement release"
        if replacement_release
        else "No relevant build-input differences; no replacement required"
    )
    assert result["resolved"]
    assert result["status"] == expected
    assert "candidateEvidence" not in result
    assert "candidateFindings" not in result
    assert result["candidateObservedAt"] == previous["candidateObservedAt"]
    text = tracker.body("graphviz", result)
    assert expected in text
    assert "Candidate blockers" not in text
    assert "Candidate evidence" not in text
    assert "release gate still required" not in text


def test_monitor_reconciliation_keeps_unchanged_last_candidate_evidence():
    observation = {
        "outcome": "vulnerable",
        "findings": [FINDING],
        "evidenceRelease": "image-monitor-1-1",
    }
    record = RECORD | {"latestAttempt": observation}
    state = desired(record=record, observed=candidate())
    assert tracker.stable(desired(record=record, previous=state)) == tracker.stable(state)
    newer = replacement()
    pending = desired(
        record=newer,
        previous=state,
        observed=candidate(version="3", observedAt="2026-01-04T00:00:00Z"),
    )
    assert tracker.stable(desired(record=newer, previous=pending)) == tracker.stable(pending)


class Issues(GitHub):
    def __init__(self):
        self.items = []
        self.calls = []

    def request(self, endpoint, **kwargs):
        payload = json.loads(kwargs["payload"])
        self.calls.append((endpoint, kwargs["method"], payload))
        if kwargs["method"] == "POST":
            self.items.append(
                payload
                | {
                    "number": 1,
                    "state": "open",
                    "user": {"login": "github-actions[bot]", "type": "Bot"},
                }
            )
        else:
            self.items[0].update(payload)
        return b"{}"


def test_retries_and_unchanged_daily_scans_do_not_duplicate_issues_or_comments():
    client = Issues()
    state = desired(observed=candidate())
    tracker.reconcile(client, "graphviz", state, None)
    again = desired(observed=candidate(evidence="run/2"), previous=state)
    tracker.reconcile(client, "graphviz", again, client.items[0])
    assert len(client.items) == len(client.calls) == 1
    changed = desired(
        observed=candidate(version="3", observedAt="2026-01-02T00:00:01Z"), previous=again
    )
    tracker.reconcile(client, "graphviz", changed, client.items[0])
    assert len(client.calls) == 2
    assert client.calls[-1][1] == "PATCH"


@pytest.mark.parametrize("profile", ["graphviz", "bicep", "bicep-avm"])
def test_refresh_title_identifies_the_published_version(profile):
    client = Issues()
    state = desired(observed=candidate())
    tracker.reconcile(client, profile, state, None)
    assert client.items[0]["title"] == f"Image refresh available: {profile} (current 0.1.0)"
    assert "next planned image release" in client.items[0]["body"]


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize(
    "old_title",
    ["Image release needed: graphviz", "Image release needed: graphviz (replace 0.0.9)"],
)
def test_existing_issue_title_is_repaired_once_without_state_changes(resolved, old_title):
    client = Issues()
    state = desired(changed=["images/graphviz-sandbox/Dockerfile"])
    tracker.reconcile(client, "graphviz", state, None)
    if resolved:
        state = desired(previous=state)
        tracker.reconcile(client, "graphviz", state, client.items[0])
    calls_before = len(client.calls)
    issue = client.items[0]
    issue["title"] = old_title
    before = {key: value for key, value in issue.items() if key != "title"}
    tracker.reconcile(client, "graphviz", state, issue)
    tracker.reconcile(client, "graphviz", state, issue)
    expected = (
        "Image release needed: graphviz (resolved at 0.1.0)"
        if resolved
        else "Image refresh available: graphviz (current 0.1.0)"
    )
    assert issue["title"] == expected
    assert {key: value for key, value in issue.items() if key != "title"} == before
    assert len(client.calls) == calls_before + 1
    assert client.calls[-1][1] == "PATCH"


@pytest.mark.parametrize("reason", ["vulnerabilities", "route"])
def test_refresh_escalates_to_required_replacement_without_duplicate_issue(reason):
    client = Issues()
    refresh = desired(observed=candidate())
    tracker.reconcile(client, "graphviz", refresh, None)
    state = copy.deepcopy(refresh)
    state["reasons"][reason] = (
        {"findings": [FINDING], "evidence": "monitor"}
        if reason == "vulnerabilities"
        else {"published": "upstream", "replacement": "source"}
    )
    tracker.reconcile(client, "graphviz", state, client.items[0])
    assert len(client.items) == 1
    assert client.items[0]["title"] == "Image release needed: graphviz (replace 0.1.0)"
    assert "Prepare a replacement image release" in client.items[0]["body"]
    assert "next planned image release" not in client.items[0]["body"]
    tracker.reconcile(client, "graphviz", refresh, client.items[0])
    assert client.items[0]["title"] == "Image refresh available: graphviz (current 0.1.0)"


def test_closure_and_closed_issue_reuse_are_idempotent():
    client = Issues()
    state = desired(changed=["images/graphviz-sandbox/Dockerfile"])
    tracker.reconcile(client, "graphviz", state, None)
    resolved = desired(record=replacement(), previous=state)
    tracker.reconcile(client, "graphviz", resolved, client.items[0])
    tracker.reconcile(client, "graphviz", resolved, client.items[0])
    assert len(client.calls) == 2
    assert client.items[0]["state"] == "closed"
    assert client.items[0]["state_reason"] == "completed"
    assert client.items[0]["title"] == "Image release needed: graphviz (resolved at 0.1.1)"
    newer = desired(
        record=replacement(), observed=candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    )
    tracker.reconcile(client, "graphviz", newer, client.items[0])
    assert client.items[0]["state"] == "open"
    assert client.items[0]["title"] == "Image refresh available: graphviz (current 0.1.1)"
    assert len(client.items) == 1


def test_missing_issue_state_refuses_mutation():
    client = Issues()
    with pytest.raises(ValueError, match="recoverable"):
        tracker.reconcile(
            client, "graphviz", desired(observed=candidate()), {"body": "edited", "number": 1}
        )
    assert not client.calls


@pytest.mark.parametrize("ending", ["-->", "--!>"])
def test_issue_state_cannot_break_its_html_comment(ending):
    result = desired(changed=[f"images/graphviz-sandbox/{ending}file"])
    match = tracker.STATE.search(tracker.body("graphviz", result))
    assert match is not None
    assert ending not in match[1]
    assert json.loads(match[1]) == result


@pytest.mark.parametrize(
    "state,delivery",
    [("incomplete", "pending"), ("completed", "pending"), ("abandoned", "pending")],
)
def test_incomplete_release_cannot_be_used_to_close_request(tmp_path, state, delivery):
    with pytest.raises(ValueError, match="incomplete"):
        tracker.baseline(GitHub(), RECORD | {"state": state, "delivery": delivery}, tmp_path)


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
    assert desired(changed=changed)["reasons"]["source"]["paths"] == [payload]


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
    previous = desired(record=record, released=released, changed=[path])
    changed = tracker.changed_inputs(tmp_path, base, head, "bicep-avm")
    assert changed == []
    result = desired(record=record, released=released, changed=changed, previous=previous)
    assert result["resolved"]
    assert result["resolution"] == "source-reconciled"
    assert tracker.changed_inputs(tmp_path, base, head, "bicep-docker-sbx") == [path]
    mixed = commit(tmp_path, {path: builder.replace('"--pull", ', "")})
    changed = tracker.changed_inputs(tmp_path, base, mixed, "bicep-avm")
    assert changed == [path]
    assert not desired(record=record, released=released, changed=changed, previous=previous)[
        "resolved"
    ]


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
    previous = desired(record=record, released=released, changed=paths)
    changed = tracker.changed_inputs(tmp_path, base, head, "bicep-avm")
    assert changed == []
    result = desired(record=record, released=released, changed=changed, previous=previous)
    assert result["resolution"] == "source-reconciled"
    assert tracker.changed_inputs(tmp_path, base, head, "terraform-avm") == paths
    payload = "images/bicep-sandbox/Dockerfile"
    mixed = commit(tmp_path, {payload: "FROM newer\n"})
    changed = tracker.changed_inputs(tmp_path, base, mixed, "bicep-avm")
    assert changed == [payload]
    assert not desired(record=record, released=released, changed=changed, previous=previous)[
        "resolved"
    ]


@pytest.mark.parametrize(
    "filename",
    ["caf\u00e9.txt", "line\nbreak", "cr\rname", "crlf\r\nname", "tab\tname", "trailing "],
)
def test_changed_inputs_preserve_git_quoted_filenames_and_prevent_closure(tmp_path, filename):
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
    previous = desired(changed=[path])
    result = desired(record=replacement(), changed=changed, previous=previous)
    assert not result["resolved"]


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
    previous = desired(changed=changed)
    assert not desired(record=replacement(), changed=changed, previous=previous)["resolved"]


def test_missing_comparison_history_still_refuses_reconciliation(tmp_path):
    tracker.git(tmp_path, "init")
    head = commit(tmp_path, {"README.md": "baseline"})
    with pytest.raises(subprocess.CalledProcessError):
        tracker.changed_inputs(tmp_path, "0" * 40, head, "graphviz")


def test_git_helper_preserves_output_whitespace(tmp_path):
    tracker.git(tmp_path, "init")
    tracker.git(tmp_path, "config", "test.value", " value \t")
    assert tracker.git(tmp_path, "config", "--get", "test.value") == " value \t\n"


def test_ci_has_no_automatic_image_builds_and_tracker_cannot_publish():
    scan = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    triggers = scan.get("on", scan.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
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
        "record": copy.deepcopy(RECORD),
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

    monkeypatch.setattr(tracker.History, "head", head)
    monkeypatch.setattr(tracker, "GitHub", lambda: client)
    monkeypatch.setattr(client, "pages", lambda _: copy.deepcopy(client.items))
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


def test_reporter_clears_source_only_request_and_reuses_it_for_real_changes(reporter, monkeypatch):
    client, current = reporter
    current["observed"] = None
    state = desired(changed=["scripts/build_scan_image.sh"])
    tracker.reconcile(client, "graphviz", state, None)
    tracker.main()
    tracker.main()
    assert len(client.calls) == 2
    assert client.items[0]["state"] == "closed"
    assert client.items[0]["state_reason"] == "not_planned"
    monkeypatch.setattr(
        tracker, "changed_inputs", lambda *_: ["images/graphviz-sandbox/Dockerfile"]
    )
    tracker.main()
    assert len(client.items) == 1
    assert client.items[0]["state"] == "open"


@pytest.mark.parametrize("replacement_release", [False, True])
def test_reporter_repairs_closed_request_status_once_and_reopens_with_current_baseline(
    reporter, replacement_release
):
    client, current = reporter
    previous = desired(changed=["images/graphviz-sandbox/Dockerfile"])
    if replacement_release:
        current["record"] = replacement()
    resolved = desired(record=current["record"], previous=previous)
    expected = resolved["status"]
    resolved["status"] = "Awaiting release preparation"
    resolved["candidateEvidence"] = "run/old"
    resolved["candidateFindings"] = [FINDING]
    tracker.reconcile(client, "graphviz", resolved, None)
    client.items[0]["state"] = "closed"
    current["record"] = replacement() | {
        "version": "0.1.2",
        "registryDigest": "sha256:" + "e" * 64,
    }
    current["observed"] = None
    tracker.main()
    tracker.main()
    assert len(client.calls) == 2
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    repaired = json.loads(match[1])
    assert repaired["status"] == expected
    assert repaired["currentDigest"] == resolved["currentDigest"]
    assert repaired.get("resolution") == resolved.get("resolution")
    assert "candidateEvidence" not in repaired
    assert "candidateFindings" not in repaired
    assert client.items[0]["state"] == "closed"
    current["observed"] = candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    tracker.main()
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    reopened = json.loads(match[1])
    assert reopened["baseline"] == current["record"]["registryDigest"]
    assert not reopened["resolved"]


@pytest.mark.parametrize("failure", ["baseline", "changed_inputs"])
def test_source_reconciliation_requires_verified_evidence_and_comparison(
    reporter, monkeypatch, failure
):
    client, current = reporter
    current["observed"] = None
    tracker.reconcile(client, "graphviz", desired(changed=["scripts/build_scan_image.sh"]), None)

    def unavailable(*_):
        raise ValueError("Evidence unavailable")

    monkeypatch.setattr(tracker, failure, unavailable)
    with pytest.raises(ValueError, match="Evidence unavailable"):
        tracker.main()
    assert len(client.calls) == 1
    assert client.items[0]["state"] == "open"


@pytest.mark.parametrize("extra_component", [False, True])
def test_reporter_creates_updates_closes_and_reuses_one_issue(reporter, extra_component):
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
    current["observed"] = candidate(version="4", observedAt="2026-01-04T00:00:00Z")
    tracker.main()
    assert len(client.items) == 1
    assert client.items[0]["state"] == "open"


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
    assert json.loads(match[1])["reasons"]["source"]["commit"] == revisions[1]


@pytest.mark.parametrize("author", ["outside-contributor", "other-bot[bot]"])
def test_unowned_marker_issue_is_never_used_or_modified(reporter, author):
    client, _ = reporter
    state = desired(observed=candidate())
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


def test_uncertain_issue_creation_is_reconciled_without_duplicate(reporter, monkeypatch):
    client, _ = reporter
    original = client.request

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("Acknowledgement lost")

    monkeypatch.setattr(client, "request", uncertain)
    with pytest.raises(TimeoutError):
        tracker.main()
    monkeypatch.setattr(client, "request", original)
    tracker.main()
    assert len(client.items) == len(client.calls) == 1


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
    packages = json.loads((ROOT / "images/bicep-sandbox/source.json").read_text())["packages"]
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


def test_paths_and_component_names_cannot_escape_issue_code_spans():
    path = "images/graphviz-sandbox/a` @unexpected **bold**.txt"
    observed = candidate()
    observed["components"][0][1] = "a` @unexpected **bold**"
    text = tracker.body("graphviz", desired(changed=[path], observed=observed))
    assert f"`` {path} ``" in text
    assert "`` a` @unexpected **bold** 2 ``" in text


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
