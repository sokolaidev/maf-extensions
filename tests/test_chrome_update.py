"""Chrome proposals require authenticated downloads, qualified bytes and owned delivery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chrome_inventory  # noqa: E402
import chrome_repository as repository  # noqa: E402
import chrome_update as update  # noqa: E402
import image_remediation_pr as publication  # noqa: E402
from container_release import read, scan_result, write  # noqa: E402
from test_chrome_inventory import IMAGE  # noqa: E402
from test_chrome_inventory import inventory as inventory  # noqa: E402
from test_image_remediation import BOT, SOURCE, URL, FakeGitHub  # noqa: E402
from test_image_remediation import app_key as app_key  # noqa: E402

pytestmark = pytest.mark.workflow
AT = "2026-11-14T00:00:00Z"
VERSION = "157.0.8103.78-1"
ARCHIVE = b"verified-debian-package"
SHA = hashlib.sha256(ARCHIVE).hexdigest()
PACKAGE_URL = (
    repository.BASE + f"pool/main/g/google-chrome-stable/google-chrome-stable_{VERSION}_amd64.deb"
)
SELECTED = {"version": VERSION, "url": PACKAGE_URL, "sha256": SHA, "size": len(ARCHIVE)}
ORIGINAL = (
    "FROM ubuntu:26.04\n\nADD --checksum=sha256:"
    + "a" * 64
    + " "
    + repository.BASE
    + "pool/main/g/google-chrome-stable/google-chrome-stable_157.0.8103.38-1_amd64.deb /tmp/chrome.deb\n"
    + "RUN echo keep-this-build-input\n"
).encode()


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    directory = tmp_path / "upstream"
    directory.mkdir()
    packages = (
        f"Package: google-chrome-stable\nVersion: {VERSION}\nArchitecture: amd64\n"
        f"Filename: {PACKAGE_URL.removeprefix(repository.BASE)}\nSize: {len(ARCHIVE)}\nSHA256: {SHA}\n"
    ).encode()
    (directory / "Packages").write_bytes(packages)
    (directory / "Release").write_text(
        "Origin: Google LLC\nSuite: stable\nCodename: stable\nDate: Fri, 13 Nov 2026 20:08:48 UTC\n"
        f"SHA256:\n {hashlib.sha256(packages).hexdigest()} {len(packages)} {repository.INDEX}\n",
        encoding="utf-8",
    )
    (directory / "google.asc").write_bytes(b"key")
    (directory / "Release.gpg").write_bytes(b"signature")
    (directory / "chrome.deb").write_bytes(ARCHIVE)
    monkeypatch.setattr(repository, "now", lambda: AT)
    monkeypatch.setattr(repository, "verify_signature", lambda _: None)
    return directory


def test_signed_metadata_selects_stable_package_and_replaces_only_pin(upstream):
    selected = repository.metadata(upstream)
    assert selected == SELECTED
    before, proposed = update.proposal(ORIGINAL, selected)
    assert before == "157.0.8103.38-1"
    assert proposed == ORIGINAL.replace(b"a" * 64, SHA.encode()).replace(
        b"157.0.8103.38-1", VERSION.encode()
    )
    assert update.proposal(proposed, selected) == (VERSION, proposed)


@pytest.mark.parametrize(
    "change", ["checksum", "size", "duplicate-index", "stale", "future", "origin", "suite"]
)
def test_invalid_signed_metadata_is_refused(upstream, change):
    path = upstream / "Release"
    text = path.read_text("utf-8")
    if change == "checksum":
        (upstream / "Packages").write_bytes(
            (upstream / "Packages").read_bytes().replace(b"amd64", b"arm64")
        )
    elif change == "size":
        (upstream / "Packages").write_bytes(b"short")
    elif change == "duplicate-index":
        text += text.splitlines()[-1] + "\n"
    elif change == "stale":
        text = text.replace("13 Nov", "01 Nov")
    elif change == "future":
        text = text.replace("13 Nov", "20 Nov")
    elif change == "origin":
        text = text.replace("Google LLC", "Other vendor")
    else:
        text = text.replace("Suite: stable", "Suite: beta")
    path.write_text(text, "utf-8")
    with pytest.raises(ValueError):
        repository.metadata(upstream)


@pytest.mark.parametrize(
    "change",
    ["duplicate-package", "architecture", "path", "version", "hash", "size", "duplicate-field"],
)
def test_unexpected_package_records_are_refused_even_when_index_hash_matches(upstream, change):
    packages = (upstream / "Packages").read_text("utf-8")
    if change == "duplicate-package":
        packages += "\n" + packages
    elif change == "architecture":
        packages = packages.replace("Architecture: amd64", "Architecture: arm64")
    elif change == "path":
        packages = packages.replace("Filename: pool/", "Filename: ../pool/")
    elif change == "version":
        packages = packages.replace(VERSION, "157.0.8103.78-beta")
    elif change == "hash":
        packages = packages.replace(SHA, "invalid")
    elif change == "size":
        packages = packages.replace(f"Size: {len(ARCHIVE)}", "Size: 999999999999")
    else:
        packages += "Architecture: amd64\n"
    raw = packages.encode()
    (upstream / "Packages").write_bytes(raw)
    release = (upstream / "Release").read_text("utf-8").split("SHA256:")[0]
    (upstream / "Release").write_text(
        release + f"SHA256:\n {hashlib.sha256(raw).hexdigest()} {len(raw)} {repository.INDEX}\n",
        "utf-8",
    )
    with pytest.raises(ValueError):
        repository.metadata(upstream)


@pytest.mark.parametrize(
    "key,signature", [(b"", ""), (b"key", ""), (b"key", "[GNUPG:] VALIDSIG wrong-key")]
)
def test_signature_verification_requires_reviewed_primary_key(
    tmp_path, monkeypatch, key, signature
):
    for name in ("google.asc", "Release", "Release.gpg"):
        (tmp_path / name).write_bytes(b"evidence")
    monkeypatch.setattr(
        repository.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=signature)
    )
    monkeypatch.setattr(repository.subprocess, "check_output", lambda *a, **k: key)
    with pytest.raises(ValueError):
        repository.verify_signature(tmp_path)


def test_bad_signature_cannot_reach_package_selection(upstream, monkeypatch):
    def reject(_):
        raise subprocess.CalledProcessError(1, "gpgv")

    monkeypatch.setattr(repository, "verify_signature", reject)
    with pytest.raises(subprocess.CalledProcessError):
        repository.metadata(upstream)


@pytest.mark.parametrize(
    "url",
    [
        "http://dl.google.com/linux/chrome/deb/file",
        "https://other.example/chrome.deb",
        repository.BASE + "../chrome.deb",
    ],
)
def test_download_allowlist_rejects_other_origins_and_paths(tmp_path, url):
    with pytest.raises(ValueError, match="Unapproved"):
        repository.download(url, tmp_path / "archive", 100)


def test_archive_hash_and_size_are_checked(upstream):
    path = upstream / "chrome.deb"
    repository.verify_bytes(path, len(ARCHIVE), SHA)
    with pytest.raises(ValueError, match="checksum"):
        repository.verify_bytes(path, len(ARCHIVE), "0" * 64)
    with pytest.raises(ValueError, match="size"):
        repository.verify_bytes(path, len(ARCHIVE) + 1, SHA)


@pytest.mark.parametrize("changed", [True, False])
def test_discovery_sets_output_only_for_a_new_version(tmp_path, monkeypatch, changed):
    root, work = tmp_path / "source", tmp_path / "work"
    recipe = root / update.RECIPE
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes(ORIGINAL if changed else update.proposal(ORIGINAL, SELECTED)[1])
    monkeypatch.setattr(update, "git", lambda *_: SOURCE)
    monkeypatch.setattr(update, "current", lambda *_: None)
    monkeypatch.setattr(repository, "acquire", lambda *_: SELECTED)
    output = []
    monkeypatch.setattr(update, "outputs", output.append)
    assert update.acquire(root, work, FakeGitHub())["status"] == (
        "update" if changed else "unchanged"
    )
    assert read(work / "receipt.json")["package"] == SELECTED
    assert output == [{"updates": str(changed).lower()}]


@pytest.mark.parametrize(
    "change", ["none", "applied", "source", "metadata", "archive", "other-file", "recipe"]
)
def test_proposal_is_recomputed_from_retained_evidence(upstream, monkeypatch, change):
    work, root = upstream.parent, upstream.parent / "source"
    recipe = root / update.RECIPE
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes(ORIGINAL)
    write(
        work / "receipt.json", {"source": SOURCE, "package": SELECTED, "before": "157.0.8103.38-1"}
    )
    monkeypatch.setattr(
        update,
        "git",
        lambda _, *args: (
            ("changed" if change == "source" else SOURCE)
            if args[0] == "rev-parse"
            else ("scripts/other.py" if change == "other-file" else "")
        ),
    )
    monkeypatch.setattr(update.subprocess, "check_output", lambda *_a, **_k: ORIGINAL)
    if change == "applied":
        recipe.write_bytes(update.proposal(ORIGINAL, SELECTED)[1])
    elif change == "metadata":
        receipt = read(work / "receipt.json")
        receipt["package"]["sha256"] = "f" * 64
        write(work / "receipt.json", receipt)
    elif change == "archive":
        (upstream / "chrome.deb").write_bytes(b"corrupt")
    elif change == "recipe":
        recipe.write_bytes(ORIGINAL + b"RUN additional-command\n")
    if change in {"none", "applied"}:
        _, files = update.validate(root, work)
        assert files == {update.RECIPE: update.proposal(ORIGINAL, SELECTED)[1]}
    else:
        with pytest.raises(ValueError):
            update.validate(root, work)


@pytest.mark.parametrize("change", ["downgrade", "changed-archive", "duplicate-pin", "missing-pin"])
def test_proposal_refuses_ambiguous_or_nonadvancing_versions(change):
    original = ORIGINAL
    selected = dict(SELECTED)
    if change == "downgrade":
        selected["version"] = "156.0.8103.78-1"
    elif change == "changed-archive":
        selected["version"] = "157.0.8103.38-1"
    elif change == "duplicate-pin":
        original += ORIGINAL
    else:
        original = b"FROM ubuntu:26.04\n"
    with pytest.raises(ValueError):
        update.proposal(original, selected)


@pytest.fixture
def candidate(upstream, inventory, monkeypatch):
    work = upstream.parent
    directory = work / "candidate"
    directory.mkdir()
    receipt = {"source": SOURCE, "acquiredAt": AT, "before": "157.0.8103.38-1", "package": SELECTED}
    files = {update.RECIPE: update.proposal(ORIGINAL, SELECTED)[1]}
    monkeypatch.setattr(update, "validate", lambda *_: (receipt, files))
    monkeypatch.setattr(update, "now", lambda: AT)
    write(
        directory / "build.json",
        {
            "local_image_id": IMAGE,
            "source_commit": SOURCE,
            "profile": "document-export",
            "platform": "linux/amd64",
        },
    )
    write(
        directory / "runtime.json",
        {"imageId": IMAGE, "profile": "document-export", "passed": True, "containersRemaining": []},
    )
    package = inventory["artifacts"][0]
    package["version"] = package["metadata"]["version"] = VERSION
    inventory["artifacts"].append(chrome_inventory.component(inventory))
    write(directory / "sbom.syft.json", inventory)
    grype = {
        "source": {"type": "image", "target": {"imageID": IMAGE}},
        "descriptor": {
            "db": {"status": {"valid": True, "built": AT, "schemaVersion": "v6", "from": "test"}}
        },
        "matches": [],
    }
    write(directory / "grype.json", grype)
    write(directory / "assessment.json", scan_result(grype, IMAGE, AT, sbom=inventory))
    write(
        directory / "inputs.json",
        {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    )
    return work, receipt, files


def test_complete_candidate_passes(candidate):
    work, _, _ = candidate
    report = update.qualify(ROOT, work)
    assert report["status"] == "ready"
    assert report["after"] == VERSION


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "runtime",
        "cleanup",
        "browser",
        "revision",
        "inputs",
        "expired",
        "database",
        "critical",
        "high",
        "ignored",
    ],
)
def test_incomplete_or_unsafe_candidate_cannot_be_delivered(candidate, change):
    work, _, _ = candidate
    directory = work / "candidate"
    name, key, value = {
        "source": ("build", "source_commit", "f" * 40),
        "runtime": ("runtime", "passed", False),
        "cleanup": ("runtime", "containersRemaining", ["leftover"]),
        "expired": ("assessment", "assessedAt", "2026-11-10T00:00:00Z"),
    }.get(change, ("assessment", "outcome", "vulnerable"))
    if change in {"browser", "revision"}:
        sbom = read(directory / "sbom.syft.json")
        package = sbom["artifacts"][0]
        package["version"] = package["metadata"]["version"] = (
            "157.0.8103.38-1" if change == "browser" else "157.0.8103.78-2"
        )
        sbom["artifacts"][-1] = chrome_inventory.component(sbom)
        write(directory / "sbom.syft.json", sbom)
    elif change == "inputs":
        write(directory / "inputs.json", {})
    elif change == "database":
        value = read(directory / "assessment.json")
        value["database"]["built"] = "2026-11-01T00:00:00Z"
        write(directory / "assessment.json", value)
    elif change in {"critical", "high", "ignored"}:
        grype = read(directory / "grype.json")
        if change == "ignored":
            grype["ignoredMatches"] = [{}]
        else:
            grype["matches"] = [
                {
                    "vulnerability": {"id": "CVE-test", "severity": change.title()},
                    "artifact": {"name": "chrome"},
                }
            ]
            write(directory / "assessment.json", scan_result(grype, IMAGE, AT))
        write(directory / "grype.json", grype)
    else:
        data = read(directory / f"{name}.json")
        data[key] = value
        write(directory / f"{name}.json", data)
    with pytest.raises(ValueError):
        update.qualify(ROOT, work)


@pytest.mark.parametrize("change", ["main", "expired", "future", "competing"])
def test_delivery_refuses_changed_source_or_stale_discovery(monkeypatch, change):
    monkeypatch.setattr(update, "now", lambda: AT)
    monkeypatch.setattr(update, "main_head", lambda _: "changed" if change == "main" else SOURCE)
    monkeypatch.setattr(update, "competing_pulls", lambda *_: [42] if change == "competing" else [])
    at = {"expired": "2026-11-10T00:00:00Z", "future": "2026-11-20T00:00:00Z"}.get(change, AT)
    with pytest.raises(ValueError):
        update.current(FakeGitHub(), {"source": SOURCE, "acquiredAt": at})


def test_chrome_delivery_recovers_and_preserves_human_changes(candidate):
    work, receipt, files = candidate
    client: Any = FakeGitHub()
    client.branch = update.ChromeGitHub.branch
    client.title = update.ChromeGitHub.title
    report = update.qualify(ROOT, work)
    checks = []

    def publish():
        return publication.publish(
            client, receipt, report, files, BOT, URL, freshness=lambda *_: checks.append(True)
        )

    client.fail_before = "pulls"
    with pytest.raises(OSError):
        publish()
    assert client.head and not client.pulls
    assert publish()["status"] == "created"
    assert publish()["status"] == "unchanged"
    assert len(checks) >= 6
    assert VERSION in client.pulls[0]["body"]
    client.pulls[0]["body"] += "\nHuman note"
    with pytest.raises(ValueError, match="modified"):
        publish()


def test_human_closed_chrome_proposal_stays_closed(candidate):
    work, receipt, files = candidate
    client: Any = FakeGitHub()
    client.branch = update.ChromeGitHub.branch
    report = update.qualify(ROOT, work)

    def publish():
        return publication.publish(
            client, receipt, report, files, BOT, URL, freshness=lambda *_: None
        )

    publish()
    client.pulls[0]["state"] = "closed"
    assert publish()["status"] == "dismissed"


def test_workflow_qualifies_and_retains_before_minting_write_identity():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/chrome-image-update.yml").read_text("utf-8")
    )
    assert workflow[True]["schedule"] == [{"cron": "30 4 * * *"}]
    job = workflow["jobs"]["update"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    steps = job["steps"]
    app = next(i for i, step in enumerate(steps) if step.get("id") == "app")
    before = "\n".join(step.get("run", "") for step in steps[:app])
    assert "chrome_update.py build" in before and "poe gate" in before
    assert next(i for i, step in enumerate(steps) if step.get("id") == "retain") < app
    assert "inputs.deliver" in steps[app]["if"]
    assert "refs/heads/main" in job["if"]
    assert "contents: write" not in json.dumps(job["permissions"])
