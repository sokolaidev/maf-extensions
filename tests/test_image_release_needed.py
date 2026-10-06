"""Exercise replacement requests without publishing images or sending GitHub notifications."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import image_release_needed as tracker  # noqa: E402
from container_release_history import GitHub, encode  # noqa: E402

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
RECORD = {
    "profile": "diagram",
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


def test_vulnerable_candidate_marks_release_request_blocked():
    result = desired(observed=candidate(outcome="vulnerable"))
    assert result["status"] == "Blocked on candidate remediation"
    assert "CVE-2026-1234" in tracker.body("diagram", result)


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
    previous = desired(changed=["images/diagram-sandbox/Dockerfile"])
    result = desired(observed=candidate(version="1"), previous=previous)
    assert not result["resolved"]
    assert "source" in result["reasons"]


def test_replacement_must_address_component_and_source_reasons():
    previous = desired(changed=["images/diagram-sandbox/Dockerfile"], observed=candidate())
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
    tracker.reconcile(client, "diagram", previous, None)
    match = tracker.STATE.search(client.items[0]["body"])
    assert match is not None
    saved = json.loads(match[1])
    result = desired(observed=candidate(version="3", outcome=second), previous=saved)
    tracker.reconcile(client, "diagram", result, client.items[0])
    assert result == saved
    assert len(client.calls) == 1


def test_new_vulnerability_prevents_closure_after_replacement():
    previous = desired(changed=["images/diagram-sandbox/Dockerfile"])
    record = replacement() | {
        "latestAttempt": {
            "outcome": "vulnerable",
            "findings": [FINDING],
            "evidenceRelease": "image-monitor-2-1",
        }
    }
    assert not desired(record=record, previous=previous)["resolved"]


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
    tracker.reconcile(client, "diagram", state, None)
    again = desired(observed=candidate(evidence="run/2"), previous=state)
    tracker.reconcile(client, "diagram", again, client.items[0])
    assert len(client.items) == len(client.calls) == 1
    changed = desired(
        observed=candidate(version="3", observedAt="2026-01-02T00:00:01Z"), previous=again
    )
    tracker.reconcile(client, "diagram", changed, client.items[0])
    assert len(client.calls) == 2
    assert client.calls[-1][1] == "PATCH"


def test_closure_and_closed_issue_reuse_are_idempotent():
    client = Issues()
    state = desired(changed=["images/diagram-sandbox/Dockerfile"])
    tracker.reconcile(client, "diagram", state, None)
    resolved = desired(record=replacement(), previous=state)
    tracker.reconcile(client, "diagram", resolved, client.items[0])
    tracker.reconcile(client, "diagram", resolved, client.items[0])
    assert len(client.calls) == 2
    assert client.items[0]["state"] == "closed"
    assert client.items[0]["state_reason"] == "completed"
    newer = desired(
        record=replacement(), observed=candidate(version="3", observedAt="2026-01-04T00:00:00Z")
    )
    tracker.reconcile(client, "diagram", newer, client.items[0])
    assert client.items[0]["state"] == "open"
    assert len(client.items) == 1


def test_missing_issue_state_refuses_mutation():
    client = Issues()
    with pytest.raises(ValueError, match="recoverable"):
        tracker.reconcile(
            client, "diagram", desired(observed=candidate()), {"body": "edited", "number": 1}
        )
    assert not client.calls


@pytest.mark.parametrize("ending", ["-->", "--!>"])
def test_issue_state_cannot_break_its_html_comment(ending):
    result = desired(changed=[f"images/diagram-sandbox/{ending}file"])
    match = tracker.STATE.search(tracker.body("diagram", result))
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
    monkeypatch.setattr(tracker, "image_tag", lambda _: "image-diagram-v0.1.0")
    with pytest.raises(ValueError, match="immutable"):
        tracker.baseline(GitHub(), RECORD, tmp_path)


def test_baseline_checks_signatures_before_consuming_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tracker.Evidence,
        "fetch",
        lambda *_: {"draft": False, "immutable": True, "tag_name": "image-diagram-v0.1.0"},
    )
    monkeypatch.setattr(tracker, "image_tag", lambda _: "image-diagram-v0.1.0")
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


def test_source_diff_ignores_publisher_but_tracks_payload_and_prepared_dependencies(tmp_path):
    tracker.git(tmp_path, "init")
    base = commit(
        tmp_path,
        {
            "images/diagram-sandbox/Dockerfile": "FROM base\n",
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
    assert tracker.changed_inputs(tmp_path, base, tooling, "diagram") == []
    payload = commit(tmp_path, {"images/diagram-sandbox/Dockerfile": "FROM newer\n"})
    assert tracker.changed_inputs(tmp_path, base, payload, "diagram") == [
        "images/diagram-sandbox/Dockerfile"
    ]
    assert tracker.changed_inputs(tmp_path, base, payload, "bicep") == []
    prepared = commit(tmp_path, {"scripts/terraform_dependencies.py": "v2"})
    assert "scripts/terraform_dependencies.py" in tracker.changed_inputs(
        tmp_path, payload, prepared, "terraform-prepared"
    )
    assert tracker.changed_inputs(tmp_path, payload, prepared, "diagram") == []


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
    for directory in ("diagram-sandbox", "images"):
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
    path = f"images/diagram-sandbox/{filename}"
    changed = tracker.changed_inputs(tmp_path, base, head, "diagram")
    assert changed == [path]
    previous = desired(changed=[path])
    result = desired(record=replacement(), changed=changed, previous=previous)
    assert not result["resolved"]


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
        "Container image release",
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
    monkeypatch.setenv("SELECTED_PROFILE", "diagram")
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
    ) == ["diagram"]


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
                "current": {"diagram": "diagram/current"},
                "releases": {"diagram/current": current["record"]},
            },
        )

    monkeypatch.setattr(tracker.History, "head", head)
    monkeypatch.setattr(tracker, "GitHub", lambda: client)
    monkeypatch.setattr(client, "pages", lambda _: copy.deepcopy(client.items))
    monkeypatch.setattr(tracker, "baseline", lambda *_: current["released"])
    monkeypatch.setattr(
        tracker,
        "candidates",
        lambda *_: {"diagram": current["observed"]} if current["observed"] else {},
    )
    monkeypatch.setattr(tracker, "changed_inputs", lambda *_: [])
    monkeypatch.setattr(tracker, "git", lambda *_: SOURCE)
    monkeypatch.setattr(sys, "argv", ["image_release_needed.py"])
    monkeypatch.setenv("GITHUB_REPOSITORY", tracker.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    return client, current


def test_reporter_creates_updates_closes_and_reuses_one_issue(reporter):
    client, current = reporter
    tracker.main()
    tracker.main()
    assert len(client.calls) == 1
    current["observed"] = candidate(version="3", observedAt="2026-01-02T00:00:01Z")
    tracker.main()
    assert len(client.calls) == 2
    current["record"] = replacement()
    current["released"] = RELEASED | {"components": current["observed"]["components"]}
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


@pytest.mark.parametrize("author", ["outside-contributor", "other-bot[bot]"])
def test_unowned_marker_issue_is_never_used_or_modified(reporter, author):
    client, _ = reporter
    state = desired(observed=candidate())
    forged = {
        "number": 100,
        "state": "open",
        "user": {"login": author, "type": "Bot"},
        "body": tracker.body("diagram", state),
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
                "current": {"diagram": "diagram/current"},
                "releases": {"diagram/current": current["record"]},
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
        "name": "image-security-diagram",
        "expired": False,
        "created_at": "2026-01-01T23:59:30Z",
    }
    files = {
        "build.json": {
            "profile": "diagram",
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
        "name": "Image security (diagram)",
        "status": "completed",
        "started_at": "2026-01-01T23:59:00Z",
        "completed_at": run["updated_at"],
    } | updates


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
        assert result["diagram"]["observedAt"] == producer["completed_at"]
        assert result["diagram"]["evidence"].endswith("/attempts/1")


def test_paths_and_component_names_cannot_escape_issue_code_spans():
    path = "images/diagram-sandbox/a` @unexpected **bold**.txt"
    observed = candidate()
    observed["components"][0][1] = "a` @unexpected **bold**"
    text = tracker.body("diagram", desired(changed=[path], observed=observed))
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
    assert result["diagram"]["assessment"]["outcome"] == "clean"
    assert result["diagram"]["components"] == [["apk", "graphviz", "2", ""]]


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
    observed = tracker.candidates(GitHub(), "123", tmp_path / "newer")["diagram"]
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
    assert optional_desired(observed=older.get("diagram"), released=released) is None


@pytest.mark.parametrize(
    "updates,profile,expected",
    [
        ({}, "diagram", {"diagram"}),
        ({"updated_at": "2026-01-02T00:00:00Z"}, "diagram", {"diagram"}),
        ({"updated_at": "2026-01-01T00:00:00Z"}, "diagram", set()),
        ({"conclusion": "failure"}, "diagram", {"diagram"}),
        ({}, "bicep", {"bicep"}),
        ({"head_branch": "topic"}, "diagram", set()),
        ({"head_repository": {"full_name": "other/repo"}}, "diagram", set()),
        ({"status": "in_progress"}, "diagram", set()),
        ({"event": "pull_request"}, "diagram", set()),
        ({"path": ".github/workflows/other.yml"}, "diagram", set()),
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
                            "name": "Image security (diagram)",
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
    assert optional_desired(observed=observed.get("diagram")) is None


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
        ) == {"diagram"}
    else:
        with pytest.raises(ValueError, match="history"):
            tracker.newer_profiles(GitHub(), run, {p: run["updated_at"] for p in tracker.PROFILES})
