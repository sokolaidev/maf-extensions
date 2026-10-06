"""Release history must survive interruption without reusing versions or hiding failures."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from container_release import (  # noqa: E402
    abandon,
    assessment_status,
    complete,
    completion_predicate,
    empty_catalogue,
    observe,
    profiles,
    read,
    reserve,
    scan_result,
    version,
)
from container_release_oci import CONFIG, INDEX, MANIFEST, validate_layout  # noqa: E402

AT = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-02T00:00:00+00:00"
DIGEST = "sha256:" + "a" * 64
EVIDENCE = "sha256:" + "b" * 64


def candidate(ver="0.1.0", run="123"):
    return {
        "profile": "bicep",
        "version": ver,
        "sourceCommit": "c" * 40,
        "sourceRef": "refs/heads/main",
        "registryDigest": DIGEST,
        "assessedManifestDigest": DIGEST,
        "imageId": "sha256:" + "d" * 64,
        "attemptId": run,
    }


def completed():
    selected = candidate()
    return complete(reserve(empty_catalogue(), selected, AT), selected, EVIDENCE, AT)


def test_release_scope_matches_every_candidate_scan():
    workflow = yaml.safe_load((ROOT / ".github/workflows/image-security.yml").read_text())
    assert set(profiles()) == {
        r["profile"] for r in workflow["jobs"]["scan"]["strategy"]["matrix"]["include"]
    }


@pytest.mark.parametrize(
    "invalid", ["latest", "0.01.0", "1.0", "1.0.0-rc1", "1.0.0+other", None, True]
)
def test_release_versions_are_not_aliases(invalid):
    with pytest.raises(ValueError):
        version(invalid)


@pytest.mark.parametrize(
    "field,new",
    [
        ("registryDigest", EVIDENCE),
        ("imageId", EVIDENCE),
        ("sourceCommit", "f" * 40),
        ("attemptId", "456"),
    ],
)
def test_reserved_version_cannot_change_bytes_source_or_owner(field, new):
    before = reserve(empty_catalogue(), candidate(), AT)
    with pytest.raises(ValueError):
        reserve(before, candidate() | {field: new}, LATER)
    assert before["releases"]["bicep/0.1.0"][field] == candidate()[field]


def test_crash_before_completion_cannot_authorize_consumer():
    before = reserve(empty_catalogue(), candidate(), AT)
    with pytest.raises(ValueError):
        completion_predicate(before["releases"]["bicep/0.1.0"])
    with pytest.raises(ValueError):
        complete(empty_catalogue(), candidate(), EVIDENCE, AT)


def test_completion_retry_preserves_history_after_newer_release():
    first = completed()
    second = candidate("0.2.0", "124")
    updated = complete(reserve(first, second, LATER), second, EVIDENCE, LATER)
    retry = complete(updated, candidate(), EVIDENCE, "2026-01-03T00:00:00Z")
    assert retry == updated
    assert retry["current"]["bicep"] == "bicep/0.2.0"
    assert retry["releases"]["bicep/0.1.0"]["supersededAt"] == LATER
    assert completion_predicate(retry["releases"]["bicep/0.1.0"])["evidenceIndexSha256"] == EVIDENCE


def test_incomplete_overtaken_candidate_cannot_replace_current():
    history = reserve(empty_catalogue(), candidate(), AT)
    second = candidate("0.2.0", "124")
    history = complete(reserve(history, second, LATER), second, EVIDENCE, LATER)
    with pytest.raises(ValueError, match="overtaken"):
        complete(history, candidate(), EVIDENCE, LATER)


def test_abandonment_burns_version_and_does_not_hide_exposure():
    history = reserve(empty_catalogue(), candidate(), AT)
    retired = abandon(history, "bicep/0.1.0", "retained bytes expired", LATER)
    assert retired["releases"]["bicep/0.1.0"]["publicExposure"] == "unknown"
    with pytest.raises(ValueError, match="retired"):
        reserve(retired, candidate(), LATER)
    with pytest.raises(ValueError, match="irreversible"):
        abandon(completed(), "bicep/0.1.0", "upload failed", LATER)


@pytest.mark.parametrize("release_version", ["0.0.0", "0.0.1", "0.0.999"])
@pytest.mark.parametrize("abandoned", [False, True])
def test_existing_reservation_cannot_remove_the_image_version_floor(release_version, abandoned):
    history = reserve(empty_catalogue(), candidate(), AT)
    if abandoned:
        history = abandon(history, "bicep/0.1.0", "retained bytes expired", LATER)
    with pytest.raises(ValueError, match="0.1.0"):
        reserve(history, candidate(release_version, "456"), LATER)
    with pytest.raises(ValueError, match="0.1.0"):
        version(release_version)


def test_abandoned_first_version_can_advance_to_a_new_patch():
    history = abandon(reserve(empty_catalogue(), candidate(), AT), "bicep/0.1.0", "expired", LATER)
    following = candidate("0.1.1", "456")
    result = complete(reserve(history, following, LATER), following, EVIDENCE, LATER)
    assert result["current"]["bicep"] == "bicep/0.1.1"


def test_completed_evidence_cannot_change_on_retry():
    with pytest.raises(ValueError, match="immutable"):
        complete(completed(), candidate(), DIGEST, LATER)


def assessment(outcome="clean", at=AT):
    return {
        "digest": DIGEST,
        "assessedAt": at,
        "outcome": outcome,
        "database": {"checksum": EVIDENCE},
    }


def test_age_is_computed_without_scheduler_and_new_failure_overrides_clean():
    history = observe(completed(), "bicep/0.1.0", assessment())
    record = history["releases"]["bicep/0.1.0"]
    assert assessment_status(record, "2026-01-02T23:59:59Z") == "clean"
    assert assessment_status(record, "2026-01-03T00:00:00Z") == "stale"
    failure = observe(history, "bicep/0.1.0", assessment("unavailable", LATER))
    assert assessment_status(failure["releases"]["bicep/0.1.0"], LATER) == "unavailable"


def test_scanner_failure_does_not_erase_known_vulnerability():
    history = observe(completed(), "bicep/0.1.0", assessment("vulnerable"))
    failed = observe(history, "bicep/0.1.0", assessment("unavailable", LATER))
    assert failed["releases"]["bicep/0.1.0"]["lastKnownVulnerable"]["outcome"] == "vulnerable"


def test_every_superseded_release_gets_90_days_but_incomplete_has_no_expiry():
    first = completed()
    second = candidate("0.2.0", "124")
    history = complete(reserve(first, second, LATER), second, EVIDENCE, LATER)
    old = history["releases"]["bicep/0.1.0"]
    assert assessment_status(old, "2026-04-02T00:00:00Z") == "no-longer-monitored"
    incomplete = reserve(empty_catalogue(), candidate(), AT)["releases"]["bicep/0.1.0"]
    assert assessment_status(incomplete, "2027-01-01T00:00:00Z") == "unavailable"


def test_stale_or_foreign_observation_is_refused():
    history = observe(completed(), "bicep/0.1.0", assessment(at=LATER))
    with pytest.raises(ValueError):
        observe(history, "bicep/0.1.0", assessment())
    with pytest.raises(ValueError):
        observe(history, "bicep/0.1.0", assessment(at=LATER) | {"digest": EVIDENCE})


def test_duplicate_json_keys_cannot_hide_an_identity(tmp_path):
    file = tmp_path / "record.json"
    file.write_text('{"state":"incomplete","state":"completed"}')
    with pytest.raises(ValueError, match="Duplicate"):
        read(file)


def scan():
    return {
        "source": {"type": "image", "target": {"imageID": candidate()["imageId"]}},
        "descriptor": {
            "db": {
                "valid": True,
                "schemaVersion": "6.1.4",
                "from": "https://grype.anchore.io/databases/example.tar.zst",
                "error": None,
                "built": AT,
                "checksum": EVIDENCE,
            }
        },
        "matches": [],
        "ignoredMatches": [],
    }


def test_unfixed_high_finding_fails_assessment():
    report = scan()
    report["matches"] = [
        {"vulnerability": {"id": "CVE-example", "severity": "High", "fix": {"state": "not-fixed"}}}
    ]
    assert scan_result(report, candidate()["imageId"], AT)["outcome"] == "vulnerable"


@pytest.mark.parametrize("state", ["fixed", "not-fixed", "wont-fix", "unknown", "future-state"])
def test_findings_retain_normalized_fix_availability(state):
    report = scan()
    report["matches"] = [
        {
            "vulnerability": {
                "id": "CVE-example",
                "severity": "High",
                "fix": {"state": state, "versions": ["2.0", "1.9"]},
            }
        }
    ]
    assessment = scan_result(report, candidate()["imageId"], AT)
    assert assessment["outcome"] == "vulnerable"
    assert assessment["findings"][0]["fix"] == {
        "state": state if state != "future-state" else "unknown",
        "versions": ["1.9", "2.0"],
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["descriptor"]["db"].update(valid=False),
        lambda r: r.update(ignoredMatches=[{}]),
        lambda r: r["source"]["target"].update(imageID=DIGEST),
    ],
)
def test_incomplete_or_filtered_scan_is_not_clean(change):
    report = scan()
    change(report)
    with pytest.raises(ValueError):
        scan_result(report, candidate()["imageId"], AT)


def put_blob(root, data, media):
    raw = json.dumps(data).encode() if isinstance(data, dict) else data
    sha = hashlib.sha256(raw).hexdigest()
    path = root / "blobs" / "sha256" / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"mediaType": media, "digest": "sha256:" + sha, "size": len(raw)}


def layout(root):
    config = put_blob(root, {"os": "linux", "architecture": "amd64"}, CONFIG)
    layer = put_blob(root, b"filesystem", "application/vnd.oci.image.layer.v1.tar")
    image = put_blob(
        root,
        {"schemaVersion": 2, "mediaType": MANIFEST, "config": config, "layers": [layer]},
        MANIFEST,
    )
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [image]}))
    return config, image, layer


def test_retained_layout_binds_manifest_to_scanned_configuration(tmp_path):
    config, image, _ = layout(tmp_path)
    assert validate_layout(tmp_path, config["digest"])["registryDigest"] == image["digest"]
    with pytest.raises(ValueError, match="assessed configuration"):
        validate_layout(tmp_path, DIGEST)


def test_changed_layer_cannot_be_promoted(tmp_path):
    config, _, layer = layout(tmp_path)
    (tmp_path / "blobs" / "sha256" / layer["digest"].split(":")[1]).write_bytes(b"substituted")
    with pytest.raises(ValueError, match="differs"):
        validate_layout(tmp_path, config["digest"])


@pytest.mark.parametrize("nested", [False, True])
def test_unassessed_sibling_and_nested_index_are_rejected(tmp_path, nested):
    config, image, _ = layout(tmp_path)
    entry = (
        put_blob(
            tmp_path,
            {"schemaVersion": 2, "mediaType": INDEX, "manifests": [image, copy.deepcopy(image)]},
            INDEX,
        )
        if nested
        else image
    )
    (tmp_path / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [entry] if nested else [entry, entry]})
    )
    with pytest.raises(ValueError):
        validate_layout(tmp_path, config["digest"])
