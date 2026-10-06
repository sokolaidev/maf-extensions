"""Exercise permission boundaries, interruption recovery and durable monitor delivery offline."""

from __future__ import annotations

import copy
import hashlib
import io
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import container_release_monitor as monitor  # noqa: E402
import container_release_publish as publisher  # noqa: E402
import container_release_registry as registry  # noqa: E402
import prepare_container_release_dispatch as dispatcher  # noqa: E402
from container_release import (  # noqa: E402
    abandon,
    assessment_status,
    complete,
    digest,
    empty_catalogue,
    read,
    reserve,
    scan_result,
    validate_transition,
    write,
)
from container_release_assets import Evidence  # noqa: E402
from container_release_history import GitHub, encode, sha256  # noqa: E402
from export_container_security_page import export  # noqa: E402
from install_container_scanners import executable  # noqa: E402

AT = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"
DONE = "2026-01-02T01:00:00Z"
CANDIDATE = {
    "profile": "bicep",
    "version": "0.1.0",
    "sourceCommit": "a" * 40,
    "sourceRef": "refs/heads/main",
    "registryDigest": "sha256:" + "b" * 64,
    "assessedManifestDigest": "sha256:" + "b" * 64,
    "imageId": "sha256:" + "c" * 64,
    "attemptId": "123",
}


def scan_report():
    return {
        "source": {"type": "image", "target": {"imageID": CANDIDATE["imageId"]}},
        "descriptor": {
            "db": {"valid": True, "built": AT, "from": "test-db", "schemaVersion": "6.1.4"}
        },
        "matches": [],
    }


class MemoryHistory:
    def __init__(self):
        self.catalogue = reserve(empty_catalogue(), CANDIDATE, AT)
        self.calls = []

    def head(self):
        return SimpleNamespace(catalogue=copy.deepcopy(self.catalogue), reference={"sequence": 1})

    def append(self, transform, **kwargs):
        result = transform(copy.deepcopy(self.catalogue))
        validate_transition(self.catalogue, result)
        self.catalogue = result
        self.calls.append(kwargs["operation"])
        return self.head()


@pytest.fixture
def monitor_context(monkeypatch):
    history = MemoryHistory()
    monkeypatch.setattr(monitor, "History", lambda: history)
    monkeypatch.setattr(monitor, "context", lambda *a: (CANDIDATE["sourceCommit"], "456", "1"))
    monkeypatch.setattr(monitor, "now", lambda: LATER)
    monkeypatch.setattr(monitor, "outputs", lambda value: None)
    monkeypatch.setattr(monitor, "manifest", lambda *args: None)
    return history


def test_unexpected_public_digest_is_reserved_before_scanning_and_survives_tag_movement(
    monkeypatch, tmp_path, monitor_context
):
    raw = (
        b'{"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"digest":"sha256:'
        + b"d" * 64
        + b'"}}'
    )
    wrong = sha256(raw)
    monkeypatch.setattr(monitor, "manifest", lambda *args: raw)
    monitor.begin(tmp_path)
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    assert record["unexpectedDigests"][wrong]["latestAttempt"]["outcome"] == "running"
    assert record["candidate"] == CANDIDATE
    with pytest.raises(ValueError, match="[Uu]nexpected"):
        complete(monitor_context.catalogue, CANDIDATE, CANDIDATE["imageId"], DONE)
    monkeypatch.setattr(monitor, "manifest", lambda *args: None)
    monkeypatch.setattr(monitor, "now", lambda: DONE)
    monitor.begin(tmp_path)
    targets = read(tmp_path / "plan.json")["targets"]
    assert {t.get("unexpectedDigest") for t in targets} == {None, wrong}


@pytest.mark.parametrize("registry_error", [False, True])
def test_unexpected_image_is_scanned_and_retained_until_its_own_confirmed_absence(
    monkeypatch, tmp_path, monitor_context, registry_error
):
    raw = encode({"mediaType": registry.MANIFEST, "config": {"digest": "sha256:" + "d" * 64}})
    wrong = sha256(raw)
    monkeypatch.setattr(
        monitor,
        "manifest",
        lambda profile, ref: raw if ref != CANDIDATE["registryDigest"] else None,
    )
    monkeypatch.setattr(
        monitor,
        "Evidence",
        lambda: SimpleNamespace(
            ensure=lambda *args: {},
            retain=lambda *args: None,
            publish=lambda *args: None,
        ),
    )
    pulls = []

    def pull(candidate):
        pulls.append(candidate["registryDigest"])
        if candidate["registryDigest"] != wrong:
            raise ValueError("Expected image was never published")
        return candidate["imageId"]

    def assess(directory, image_id, **kwargs):
        assert kwargs == {"require_clean": False}
        report = scan_report()
        report["source"]["target"]["imageID"] = image_id
        report["matches"] = [{"vulnerability": {"id": "CVE-unexpected", "severity": "High"}}]
        result = scan_result(report, image_id, DONE)
        write(directory / "grype.json", report)
        write(directory / "assessment.json", result)
        write(
            directory / "sbom.syft.json",
            {"artifacts": [{}], "source": {"type": "image", "metadata": {"imageID": image_id}}},
        )
        return result

    monkeypatch.setattr(monitor, "anonymous_pull", pull)
    monkeypatch.setattr(monitor, "assess", assess)
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: None)
    monitor.begin(tmp_path)
    monkeypatch.setattr(monitor, "now", lambda: DONE)
    monitor.scan(tmp_path, 0)
    monitor.finish(tmp_path)
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    entry = record["unexpectedDigests"][wrong]
    assert wrong in pulls
    assert entry["latestAttempt"]["outcome"] == "vulnerable"
    assert entry["lastKnownVulnerable"]["findings"][0]["id"] == "CVE-unexpected"
    assert record["registryDigest"] == CANDIDATE["registryDigest"]
    with zipfile.ZipFile(tmp_path / "retained/reports-0000.zip") as archive:
        assert any(
            wrong[7:] in name and name.endswith("manifest.json") for name in archive.namelist()
        )
    monitor_context.catalogue = abandon(
        monitor_context.catalogue, "bicep/0.1.0", "unexpected publication", DONE
    )
    next_directory = tmp_path / "next"
    monkeypatch.setattr(monitor, "now", lambda: "2026-01-03T00:00:00Z")

    def missing(profile, ref):
        if ref == wrong and registry_error:
            raise TimeoutError("registry unavailable")
        return None

    monkeypatch.setattr(monitor, "manifest", missing)
    monitor.begin(next_directory)
    monkeypatch.setattr(monitor, "now", lambda: "2026-01-03T01:00:00Z")
    monitor.scan(next_directory, 0)
    monitor.finish(next_directory)
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    assert monitor.monitored(record, "2026-01-04T00:00:00Z") is registry_error
    assert (
        record["unexpectedDigests"][wrong]["lastKnownVulnerable"]["findings"][0]["id"]
        == "CVE-unexpected"
    )


def test_failed_registry_discovery_cannot_report_clean(monkeypatch, tmp_path, monitor_context):
    def unavailable(*args):
        raise TimeoutError("registry unavailable")

    monkeypatch.setattr(monitor, "manifest", unavailable)
    monitor.begin(tmp_path)
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    record["latestAttempt"].update(outcome="clean")
    assert assessment_status(record, DONE) == "unavailable"


@pytest.mark.parametrize("mutation", ["delete", "time", "assessment"])
def test_discovered_public_digest_history_cannot_be_erased(
    monkeypatch, tmp_path, monitor_context, mutation
):
    monkeypatch.setattr(monitor, "manifest", lambda *args: b"unexpected bytes")
    monitor.begin(tmp_path)
    changed = copy.deepcopy(monitor_context.catalogue)
    discoveries = changed["releases"]["bicep/0.1.0"]["unexpectedDigests"]
    entry = next(iter(discoveries.values()))
    if mutation == "delete":
        discoveries.clear()
    elif mutation == "time":
        entry["discoveredAt"] = AT
    else:
        entry["latestAttempt"]["outcome"] = "clean"
    with pytest.raises(ValueError):
        validate_transition(monitor_context.catalogue, changed)


def test_monitor_commits_running_before_workers_and_missing_worker_is_unavailable(
    monkeypatch, tmp_path, monitor_context
):
    events = []

    class Storage:
        def ensure(self, *args):
            return {}

        def retain(self, *args):
            events.append("retained")

        def publish(self, *args):
            events.append("published")
            assert (
                monitor_context.catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"]
                == "running"
            )

    monkeypatch.setattr(monitor, "Evidence", Storage)
    monitor.begin(tmp_path)
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    assert record["latestAttempt"]["outcome"] == "running"
    monkeypatch.setattr(monitor, "now", lambda: DONE)
    monitor.finish(tmp_path)
    assert events == ["retained", "published"]
    record = monitor_context.catalogue["releases"]["bicep/0.1.0"]
    assert record["latestAttempt"]["outcome"] == "unavailable"
    assert record["latestAttempt"]["evidenceRelease"] == "image-monitor-456-1"


def test_monitor_delivery_failure_keeps_running_instead_of_clean(
    monkeypatch, tmp_path, monitor_context
):
    class Storage:
        def ensure(self, *args):
            raise TimeoutError("publication uncertain")

    monkeypatch.setattr(monitor, "Evidence", Storage)
    monitor.begin(tmp_path)
    monkeypatch.setattr(monitor, "now", lambda: DONE)
    with pytest.raises(TimeoutError):
        monitor.finish(tmp_path)
    assert (
        monitor_context.catalogue["releases"]["bicep/0.1.0"]["latestAttempt"]["outcome"]
        == "running"
    )


@pytest.mark.parametrize(
    "state,missing,error,proof",
    [
        ("incomplete", True, False, False),
        ("abandoned", True, False, True),
        ("abandoned", False, False, False),
        ("abandoned", True, True, False),
    ],
)
def test_absence_retirement_requires_abandonment_and_two_confirmed_absences(
    monkeypatch, tmp_path, monitor_context, state, missing, error, proof
):
    if state == "abandoned":
        monitor_context.catalogue = abandon(
            monitor_context.catalogue, "bicep/0.1.0", "retained candidate lost", AT
        )
    calls = []

    def manifest(profile, reference):
        calls.append(reference)
        if error:
            raise TimeoutError("registry unavailable")
        return None if missing else b"manifest"

    def unavailable(candidate):
        raise ValueError("image is unavailable")

    monkeypatch.setattr(monitor, "manifest", manifest)
    monkeypatch.setattr(monitor, "anonymous_pull", unavailable)
    monitor.begin(tmp_path)
    monitor.scan(tmp_path, 0)
    result = read(tmp_path / "reports/bicep-0.1.0/observation.json")["assessment"]
    assert ("absenceProof" in result) == proof
    if proof:
        assert calls == [CANDIDATE["version"], CANDIDATE["registryDigest"], CANDIDATE["version"]]


def test_monitor_rejects_forged_clean_observation(monkeypatch, tmp_path, monitor_context):
    monitor.begin(tmp_path)
    directory = tmp_path / "reports/bicep-0.1.0"
    raw = scan_report()
    raw["matches"] = [{"vulnerability": {"id": "CVE-test", "severity": "High"}}]
    assessment = scan_result(raw, CANDIDATE["imageId"], DONE)
    write(directory / "grype.json", raw)
    write(directory / "assessment.json", assessment)
    write(
        directory / "sbom.syft.json",
        {
            "artifacts": [{}],
            "source": {"type": "image", "metadata": {"imageID": CANDIDATE["imageId"]}},
        },
    )
    write(
        directory / "observation.json",
        {
            "candidate": CANDIDATE,
            "assessment": assessment
            | {
                "outcome": "clean",
                "digest": CANDIDATE["registryDigest"],
                "monitorRunId": "456",
                "monitorRunAttempt": 1,
            },
        },
    )
    monkeypatch.setattr(monitor, "now", lambda: DONE)
    with pytest.raises(ValueError, match="scanner report"):
        monitor.finish(tmp_path)
    assert monitor_context.calls == ["456/1/monitor-start"]


def test_failed_jobs_only_monitor_retry_cannot_claim_a_new_scan(
    monkeypatch, tmp_path, monitor_context
):
    monitor.begin(tmp_path)
    monkeypatch.setattr(monitor, "context", lambda *a: (CANDIDATE["sourceCommit"], "456", "2"))
    with pytest.raises(ValueError, match="another workflow attempt"):
        monitor.scan(tmp_path, 0)


@pytest.mark.parametrize(
    "code,error,absent",
    [
        (404, "MANIFEST_UNKNOWN", True),
        (404, "NAME_UNKNOWN", False),
        (401, "UNAUTHORIZED", False),
        (500, "UNKNOWN", False),
    ],
)
def test_registry_absence_does_not_confuse_authentication_or_server_errors(
    monkeypatch, code, error, absent
):
    def request(url, headers):
        if "/token?" in url:
            return b'{"token":"scoped"}'
        raise urllib.error.HTTPError(
            url, code, "error", {}, io.BytesIO(encode({"errors": [{"code": error}]}))
        )

    monkeypatch.setattr(registry, "request", request)
    if absent:
        assert registry.manifest("bicep", "0.1.0") is None
    else:
        with pytest.raises((ValueError, urllib.error.HTTPError)):
            registry.manifest("bicep", "0.1.0")


def test_freshness_refresh_preserves_approval_reports_and_uses_same_image(monkeypatch, tmp_path):
    raw = scan_report()
    write(tmp_path / "grype.json", raw)
    write(tmp_path / "assessment.json", scan_result(raw, CANDIDATE["imageId"], AT))
    for name in ("grype.yaml", "sbom.syft.json", "sbom.spdx.json"):
        write(tmp_path / name, {})
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    calls = []
    monkeypatch.setattr(registry, "now", lambda: LATER)
    monkeypatch.setattr(registry, "assess", lambda directory, image: calls.append(image))
    registry.fresh_assessment(tmp_path, CANDIDATE)
    assert calls == [CANDIDATE["imageId"]]
    for name, raw_bytes in before.items():
        assert (tmp_path / f"approval-{name}").read_bytes() == raw_bytes


@pytest.mark.parametrize("completed", [False, True])
def test_rerun_never_rebuilds_when_original_artifact_is_lost(monkeypatch, tmp_path, completed):
    history = MemoryHistory()
    if completed:
        history.catalogue = complete(history.catalogue, CANDIDATE, "sha256:" + "d" * 64, LATER)
    monkeypatch.setattr(dispatcher, "History", lambda: history)
    monkeypatch.setattr(dispatcher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "2"))
    monkeypatch.setattr(dispatcher, "retained_available", lambda run_id: False)
    monkeypatch.setattr(dispatcher, "prepare", lambda *a: pytest.fail("rerun must never rebuild"))
    outputs = []
    monkeypatch.setattr(dispatcher, "outputs", outputs.append)
    if completed:
        dispatcher.dispatch("bicep", "0.1.0", tmp_path / "build", tmp_path / "selection")
        assert outputs[-1]["mode"] == "delivery"
        assert read(tmp_path / "selection/candidate.json") == CANDIDATE
    else:
        with pytest.raises(ValueError, match="rebuild requires"):
            dispatcher.dispatch("bicep", "0.1.0", tmp_path / "build", tmp_path / "selection")
        assert outputs == [{"lost": "true"}]


def test_uncertain_artifact_listing_does_not_abandon_version(monkeypatch):
    def request(*args, **kwargs):
        raise TimeoutError("GitHub unavailable")

    monkeypatch.setattr(GitHub, "request", request)
    with pytest.raises(TimeoutError):
        dispatcher.retained_available("123")


def test_qualification_authenticates_before_pull_or_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(publisher, "selected", lambda directory: CANDIDATE)
    events = []

    def reject(*args):
        events.append("verify")
        raise ValueError("untrusted signer")

    monkeypatch.setattr(publisher, "verify_candidate", reject)
    monkeypatch.setattr(publisher, "anonymous_pull", lambda *a: pytest.fail("must not pull"))
    monkeypatch.setattr(publisher, "check", lambda *a: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="untrusted signer"):
        publisher.qualify(tmp_path)
    assert events == ["verify"]


def test_completion_is_committed_only_after_durable_evidence_and_retry_keeps_identity(
    monkeypatch, tmp_path
):
    history = MemoryHistory()
    stored = {}
    events = []

    class Storage:
        def find(self, tag):
            return None

        def ensure(self, tag, source):
            return {"id": 1}

        def retain(self, release, directory, files):
            assert history.catalogue["releases"]["bicep/0.1.0"]["state"] == "incomplete"
            for name in files:
                stored[name] = (directory / name).read_bytes()
            events.append("retained")

        def fetch(self, tag, directory):
            directory.mkdir(parents=True)
            for name, raw in stored.items():
                (directory / name).write_bytes(raw)
            events.append("recovered")

    monkeypatch.setattr(publisher, "History", lambda: history)
    monkeypatch.setattr(publisher, "Evidence", Storage)
    monkeypatch.setattr(publisher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "1"))
    monkeypatch.setattr(publisher, "now", lambda: LATER)
    monkeypatch.setattr(publisher, "outputs", lambda values: None)
    monkeypatch.setattr(
        publisher, "verify_candidate", lambda *args: {"candidateIdentityVerified": True}
    )
    initial = tmp_path / "initial"
    write(initial / "candidate.json", CANDIDATE)
    monkeypatch.setenv("APPROVED_CANDIDATE_SHA256", digest(initial / "candidate.json"))
    for name in (
        "manifest.json",
        "build.json",
        "sbom.syft.json",
        "sbom.spdx.json",
        "grype.json",
        "runtime.json",
        "provenance.jsonl",
        "sbom.jsonl",
    ):
        write(initial / name, {})
    write(
        initial / "qualification.json",
        {
            "candidate": CANDIDATE,
            "runId": "123",
            "anonymousPullVerified": True,
            "identity": {"candidateIdentityVerified": True},
        },
    )
    publisher.retain_and_complete(initial)
    completed = copy.deepcopy(history.catalogue)
    assert events == ["retained"]
    assert history.calls == ["123/1/complete"]
    assert completed["releases"]["bicep/0.1.0"]["delivery"] == "pending"
    retry = tmp_path / "retry"
    write(retry / "candidate.json", CANDIDATE)
    monkeypatch.setattr(publisher, "now", lambda: DONE)
    publisher.retain_and_complete(retry)
    assert history.catalogue == completed
    assert events == ["retained", "recovered"]
    assert read(retry / "retained/completion-predicate.json") == read(
        initial / "completion-predicate.json"
    )


def test_candidate_hash_is_checked_before_publication(monkeypatch, tmp_path):
    write(tmp_path / "candidate.json", CANDIDATE)
    monkeypatch.setattr(publisher, "context", lambda: (CANDIDATE["sourceCommit"], "123", "1"))
    monkeypatch.setenv("APPROVED_CANDIDATE_SHA256", "sha256:" + "e" * 64)
    with pytest.raises(ValueError, match="presented for approval"):
        publisher.selected(tmp_path)


class AssetGitHub(GitHub):
    def __init__(self):
        self.inventory = []
        self.blobs = {}
        self.fail_after_upload = False

    def assets(self, release_id):
        return copy.deepcopy(self.inventory)

    def request(self, endpoint, **kwargs):
        name = endpoint.split("?name=")[-1]
        raw = kwargs["payload"].read_bytes()
        aid = len(self.inventory) + 1
        self.inventory.append(
            {"id": aid, "name": name, "state": "uploaded", "digest": sha256(raw), "size": len(raw)}
        )
        self.blobs[aid] = raw
        if self.fail_after_upload:
            self.fail_after_upload = False
            raise TimeoutError("upload acknowledgement lost")
        return b"{}"

    def download_file(self, asset_id, destination):
        with destination.open("xb") as stream:
            stream.write(self.blobs[asset_id])


def test_asset_upload_uncertainty_is_reconciled_without_replacement(tmp_path):
    github = AssetGitHub()
    github.fail_after_upload = True
    evidence = Evidence(github)
    write(tmp_path / "report.json", {"evidence": True})
    files = {"report.json": digest(tmp_path / "report.json")}
    release = {"id": 1, "draft": True}
    with pytest.raises(TimeoutError):
        evidence.retain(release, tmp_path, files)
    evidence.retain(release, tmp_path, files)
    assert len(github.inventory) == 1
    write(tmp_path / "report.json", {"evidence": False})
    with pytest.raises(ValueError, match="retained evidence"):
        evidence.retain(release, tmp_path, {"report.json": digest(tmp_path / "report.json")})


def test_downloaded_asset_bytes_must_match_metadata(tmp_path):
    github = AssetGitHub()
    github.blobs[1] = b"substituted"
    asset = {"id": 1, "state": "uploaded", "size": 8, "digest": sha256(b"original")}
    with pytest.raises(ValueError, match="differs"):
        Evidence(github).download(asset, tmp_path / "report", asset["digest"])


@pytest.mark.parametrize("kind", ["regular", "symlink", "corrupt"])
def test_scanner_install_authenticates_bytes_before_extracting_executable(kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("syft")
        entry.size = 4
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "elsewhere"
        archive.addfile(entry, io.BytesIO(b"tool"))
    raw = buffer.getvalue()
    expected = hashlib.sha256(raw).hexdigest() if kind != "corrupt" else "0" * 64
    if kind == "regular":
        assert executable(raw, "syft", expected) == b"tool"
    else:
        with pytest.raises(ValueError):
            executable(raw, "syft", expected)


def test_empty_pages_export_has_no_security_success_claim(tmp_path):
    class Empty(HistoryStub):
        pass

    export(tmp_path, Empty())
    assert read(tmp_path / "catalogue.json")["catalogue"]["releases"] == {}
    assert "view report" in (tmp_path / "badge.svg").read_text()
    assert "passing" not in (tmp_path / "badge.svg").read_text()


class HistoryStub:
    def head(self):
        return None


def test_workflow_writer_locks_and_privilege_separation():
    release = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    monitor_workflow = yaml.safe_load(
        (ROOT / ".github/workflows/container-image-monitor.yml").read_text()
    )
    for workflow, jobs in [
        (release, ("reserve", "complete", "deliver", "abandon", "retire-lost-candidate")),
        (monitor_workflow, ("begin", "finish")),
    ]:
        for name in jobs:
            assert workflow["jobs"][name]["concurrency"] == {
                "group": "container-security-catalogue",
                "queue": "max",
                "cancel-in-progress": False,
            }
    assert release["jobs"]["approve"]["environment"] == "container-release"
    assert release["jobs"]["qualify"]["permissions"] == {"contents": "read", "attestations": "read"}
    assert "permissions" not in monitor_workflow["jobs"]["scan"]
    assert monitor_workflow["permissions"] == {"contents": "read"}
    assert release["jobs"]["promote"]["permissions"]["actions"] == "read"


def test_page_status_boundary_in_node():
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the browser status boundary check")
    subprocess.run(
        [node, "--test", "tests/container_security_page.cjs"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
