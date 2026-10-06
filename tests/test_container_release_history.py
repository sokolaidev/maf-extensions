"""Exercise history commit points and uncertain writes without publishing GitHub releases."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from container_release import (  # noqa: E402
    complete,
    empty_catalogue,
    observe,
    reserve,
    validate_catalogue,
    validate_transition,
)
from container_release_history import (  # noqa: E402
    ASSET,
    TAG,
    GitHub,
    History,
    decode,
    encode,
    sha256,
)

AT = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"
SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
CANDIDATE = {
    "profile": "bicep",
    "version": "0.1.0",
    "sourceCommit": SOURCE,
    "sourceRef": "refs/heads/main",
    "registryDigest": DIGEST,
    "assessedManifestDigest": DIGEST,
    "imageId": "sha256:" + "c" * 64,
    "attemptId": "123",
}


class FakeGitHub(GitHub):
    def __init__(self):
        self.entries = {}
        self.blobs = {}
        self.failure: str | None = None
        self.immutable = True
        self.downloads = 0

    def releases(self):
        return copy.deepcopy(list(self.entries.values()))

    def assets(self, release_id):
        return copy.deepcopy(self.entries[release_id]["assets"])

    def download(self, asset_id):
        self.downloads += 1
        return self.blobs[asset_id]

    def tag_commit(self, tag):
        return next(
            (r["target_commitish"] for r in self.entries.values() if r["tag_name"] == tag), None
        )

    def create(self, tag, source):
        if any(r["tag_name"] == tag for r in self.entries.values()):
            raise RuntimeError("duplicate release")
        rid = len(self.entries) + 1
        self.entries[rid] = {
            "id": rid,
            "tag_name": tag,
            "target_commitish": source,
            "draft": True,
            "immutable": False,
            "assets": [],
        }
        return copy.deepcopy(self.entries[rid])

    def upload(self, release_id, raw):
        if self.failure == "before-upload":
            self.failure = None
            raise TimeoutError("upload failed")
        assert not self.entries[release_id]["assets"]
        self.blobs[release_id] = raw
        self.entries[release_id]["assets"] = [
            {
                "id": release_id,
                "name": ASSET,
                "size": len(raw),
                "state": "uploaded",
                "digest": sha256(raw),
            }
        ]
        if self.failure == "after-upload":
            self.failure = None
            raise TimeoutError("upload acknowledgement lost")

    def publish(self, release_id):
        if self.failure == "before-publish":
            self.failure = None
            raise TimeoutError("publication failed")
        self.entries[release_id].update(draft=False, immutable=self.immutable)
        if self.failure == "after-publish":
            self.failure = None
            raise TimeoutError("publication acknowledgement lost")

    def replace(self, release_id, document):
        raw = encode(document)
        self.blobs[release_id] = raw
        self.entries[release_id]["assets"][0].update(size=len(raw), digest=sha256(raw))


def append_reservation(history, operation="123/reserve", candidate=CANDIDATE, at=AT):
    return history.append(
        lambda catalogue: reserve(catalogue, candidate, at),
        operation=operation,
        source=SOURCE,
        at=at,
    )


def test_reservation_is_durable_and_retry_does_not_create_another_release():
    gh = FakeGitHub()
    history = History(gh)
    first = append_reservation(history)
    assert first.catalogue["releases"]["bicep/0.1.0"]["state"] == "incomplete"
    assert append_reservation(history).reference == first.reference
    assert len(gh.entries) == 1


def test_unfinished_upload_in_a_draft_does_not_block_a_new_writer():
    gh = FakeGitHub()
    draft = gh.create(TAG + "000000000001", SOURCE)
    gh.entries[draft["id"]]["assets"] = [{"name": ASSET, "state": "starter", "size": 0}]
    committed = append_reservation(History(gh))
    assert committed.number == 2
    assert gh.entries[1]["draft"] is True
    assert committed.document["ancestors"] == []


@pytest.mark.parametrize("stage", ["after-upload", "before-publish", "after-publish"])
def test_uncertain_write_is_reconciled_without_replacing_assets(stage):
    gh = FakeGitHub()
    gh.failure = stage
    history = History(gh)
    with pytest.raises(TimeoutError):
        append_reservation(history)
    head = history.head()
    assert (head is not None) == (stage == "after-publish")
    recovered = append_reservation(history)
    assert recovered.number == 1
    assert len(gh.entries) == 1
    assert recovered.document["createdAt"] == AT


def test_empty_interrupted_draft_does_not_prevent_future_monitoring_writes():
    gh = FakeGitHub()
    gh.failure = "before-upload"
    history = History(gh)
    with pytest.raises(TimeoutError):
        append_reservation(history)
    assert history.head() is None
    recovered = append_reservation(history)
    assert recovered.number == 2
    assert gh.entries[1]["draft"] is True
    assert recovered.document["previous"] is None


def test_completion_and_supersession_share_one_committed_snapshot():
    gh = FakeGitHub()
    history = History(gh)
    append_reservation(history)
    first = history.append(
        lambda c: complete(c, CANDIDATE, DIGEST, AT),
        operation="123/complete",
        source=SOURCE,
        at=AT,
    )
    second_candidate = CANDIDATE | {"version": "0.2.0", "attemptId": "124"}
    append_reservation(history, "124/reserve", second_candidate, LATER)
    gh.failure = "after-publish"
    with pytest.raises(TimeoutError):
        history.append(
            lambda c: complete(c, second_candidate, DIGEST, LATER),
            operation="124/complete",
            source=SOURCE,
            at=LATER,
        )
    head = history.head()
    assert head.catalogue["current"] == {"bicep": "bicep/0.2.0"}
    assert head.catalogue["releases"]["bicep/0.1.0"]["supersededAt"] == LATER
    assert head.catalogue["releases"]["bicep/0.2.0"]["delivery"] == "pending"
    assert first.reference in head.document["ancestors"]


def test_new_monitor_failure_cannot_fall_back_to_clean_snapshot():
    gh = FakeGitHub()
    history = History(gh)
    append_reservation(history)
    history.append(
        lambda c: observe(
            c, "bicep/0.1.0", {"digest": DIGEST, "outcome": "clean", "assessedAt": AT}
        ),
        operation="monitor/1",
        source=SOURCE,
        at=AT,
    )
    failure = {"digest": DIGEST, "outcome": "unavailable", "assessedAt": LATER}
    failed = history.append(
        lambda c: observe(c, "bicep/0.1.0", failure),
        operation="monitor/2",
        source=SOURCE,
        at=LATER,
    )
    assert history.head().catalogue["releases"]["bicep/0.1.0"]["latestAttempt"] == failure
    gh.blobs[failed.release_id] += b"corruption"
    with pytest.raises(ValueError, match="bytes differ"):
        history.head()


def test_mutable_published_history_is_not_a_commit():
    gh = FakeGitHub()
    gh.immutable = False
    with pytest.raises(ValueError, match="must be immutable"):
        append_reservation(History(gh))


def test_stale_draft_cannot_replace_newer_committed_state():
    gh = FakeGitHub()
    history = History(gh)
    gh.failure = "before-publish"
    with pytest.raises(TimeoutError):
        append_reservation(history)
    other = CANDIDATE | {"profile": "diagram", "attemptId": "124"}
    current = append_reservation(history, "124/reserve", other)
    with pytest.raises(ValueError, match="Retry differs"):
        append_reservation(history)
    assert history.head().reference == current.reference
    assert gh.entries[1]["draft"]


def test_unrelated_orphan_draft_is_retained_but_not_published():
    gh = FakeGitHub()
    history = History(gh)
    gh.failure = "before-publish"
    with pytest.raises(TimeoutError):
        append_reservation(history)
    other = CANDIDATE | {"profile": "diagram", "attemptId": "124"}
    current = append_reservation(history, "124/reserve", other)
    assert current.number == 2
    assert set(current.catalogue["releases"]) == {"diagram/0.1.0"}
    assert gh.entries[1]["draft"] is True


def test_late_publication_of_skipped_draft_is_detected_as_a_fork():
    gh = FakeGitHub()
    history = History(gh)
    gh.failure = "before-publish"
    with pytest.raises(TimeoutError):
        append_reservation(history)
    append_reservation(history, "124/reserve", CANDIDATE | {"profile": "diagram"})
    gh.publish(1)
    with pytest.raises(ValueError, match="fork"):
        history.head()


def test_missing_old_predecessor_is_detected_without_downloading_old_assets():
    gh = FakeGitHub()
    history = History(gh)
    append_reservation(history)
    append_reservation(history, "124/reserve", CANDIDATE | {"profile": "diagram"})
    append_reservation(history, "125/reserve", CANDIDATE | {"profile": "drawio-export"})
    gh.downloads = 0
    history.head()
    assert gh.downloads == 1
    del gh.entries[1]
    with pytest.raises(ValueError, match="missing predecessor"):
        history.head()


def test_changed_head_between_upload_and_commit_refuses_publication(monkeypatch):
    gh = FakeGitHub()
    history = History(gh)
    original = gh.upload
    called = False

    def race(release_id, raw):
        nonlocal called
        original(release_id, raw)
        if not called:
            called = True
            append_reservation(history, "124/reserve", CANDIDATE | {"profile": "diagram"})

    monkeypatch.setattr(gh, "upload", race)
    with pytest.raises(ValueError, match="head changed"):
        append_reservation(history)
    assert gh.entries[1]["draft"] is True
    assert history.head().catalogue["current"] == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schemaVersion", True),
        ("sequence", True),
        ("sourceCommit", "main"),
        ("createdAt", "2026-01-01T00:00:00"),
        ("previous", {"sequence": 1, "sha256": DIGEST}),
        ("ancestors", [{"sequence": True, "sha256": DIGEST}]),
        ("operationId", ""),
    ],
)
def test_malformed_committed_envelope_is_refused(field, value):
    gh = FakeGitHub()
    history = History(gh)
    head = append_reservation(history)
    changed = copy.deepcopy(head.document)
    changed[field] = value
    gh.replace(1, changed)
    with pytest.raises(ValueError):
        history.head()


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(current={"bicep": "bicep/0.9.0"}),
        lambda c: c["releases"]["bicep/0.1.0"].update(delivery="complete"),
        lambda c: c["releases"]["bicep/0.1.0"].update(state="completed"),
        lambda c: c["releases"]["bicep/0.1.0"].update(createdAt="yesterday"),
        lambda c: c["releases"]["bicep/0.1.0"].update(latestAttempt={"digest": "other"}),
    ],
)
def test_structurally_invalid_catalogue_cannot_be_persisted(change):
    catalogue = reserve(empty_catalogue(), CANDIDATE, AT)
    change(catalogue)
    with pytest.raises(ValueError):
        validate_catalogue(catalogue)


def test_transition_cannot_erase_reservations_or_completion():
    before = reserve(empty_catalogue(), CANDIDATE, AT)
    with pytest.raises(ValueError, match="permanent"):
        validate_transition(before, empty_catalogue())
    completed = complete(before, CANDIDATE, DIGEST, AT)
    with pytest.raises(ValueError, match="rewritten"):
        validate_transition(completed, before)


def test_completion_cannot_be_the_first_persisted_record():
    completed = complete(reserve(empty_catalogue(), CANDIDATE, AT), CANDIDATE, DIGEST, AT)
    with pytest.raises(ValueError, match="prior durable"):
        validate_transition(empty_catalogue(), completed)


def test_history_api_reads_all_pages_and_pins_repository_and_host(monkeypatch):
    seen = []

    def run(command, **kwargs):
        from subprocess import CompletedProcess

        seen.append((command, kwargs))
        body = [{"id": i} for i in range(100)] if command[4].endswith("&page=1") else [{"id": 100}]
        return CompletedProcess(command, 0, stdout=json.dumps(body).encode())

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    assert len(GitHub().releases()) == 101
    assert all(command[:4] == ["gh", "api", "--hostname", "github.com"] for command, _ in seen)
    assert all("repos/sokolaidev/maf-extensions/releases?" in command[4] for command, _ in seen)


def test_release_creation_is_draft_and_never_changes_latest(monkeypatch):
    gh = GitHub()
    seen = []

    def request(endpoint, **kwargs):
        seen.append((endpoint, kwargs))
        return b"{}"

    monkeypatch.setattr(gh, "request", request)
    monkeypatch.setattr(gh, "tag_commit", lambda _tag: None)
    gh.create(TAG + "000000000001", SOURCE)
    body = decode(seen[0][1]["payload"])
    assert body["draft"] is True and body["prerelease"] is True
    assert body["make_latest"] == "false"
    assert body["target_commitish"] == SOURCE


@pytest.mark.parametrize("file_payload", [False, True], ids=["stdin", "file"])
def test_release_upload_sends_exact_body_with_content_length(
    monkeypatch, tmp_path: Path, file_payload: bool
):
    if shutil.which("gh") is None:
        pytest.skip("GitHub CLI is required for the local HTTP transport check")
    monkeypatch.setenv("GH_TOKEN", "local-test-token")
    monkeypatch.delenv("GH_DEBUG", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    body = '{"name":"caf\u00e9"}\n'.encode()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = self.headers.get("Content-Length")
            if size is None:
                self.send_error(411, "Length Required")
                return
            received.append((self.rfile.read(int(size)), self.headers.get("Content-Type")))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object):
            pass

    payload: bytes | Path = body
    if file_payload:
        payload = tmp_path / "catalogue.json"
        payload.write_bytes(body)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = GitHub().request(
                f"http://127.0.0.1:{server.server_port}/assets?name=catalogue.json",
                method="POST",
                payload=payload,
                upload=True,
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result == b"{}"
    assert received == [(body, "application/octet-stream")]


def test_failed_github_request_retains_cli_diagnostic_without_retry(monkeypatch):
    calls = []
    error = subprocess.CalledProcessError(
        1, ["gh", "api"], stderr=b"gh: Length Required (HTTP 411)"
    )

    def run(command, **_kwargs):
        calls.append(command)
        raise error

    monkeypatch.setattr("container_release_history.subprocess.run", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        GitHub().upload(1, b"{}")
    assert caught.value is error
    assert error.__notes__ == ["gh: Length Required (HTTP 411)"]
    assert len(calls) == 1
