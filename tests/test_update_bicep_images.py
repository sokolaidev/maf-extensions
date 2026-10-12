"""Weekly compiler proposals verify artifacts and preserve reviewed AVM module identities."""

import copy
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_bicep_images as updater  # noqa: E402
from select_image_security import affected  # noqa: E402

BINARY = b"verified upstream compiler"
DIGEST = "sha256:" + hashlib.sha256(BINARY).hexdigest()


def metadata():
    return {
        "tag_name": "v1.2.3",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "bicep-linux-x64",
                "digest": DIGEST,
                "size": len(BINARY),
                "browser_download_url": "https://github.com/Azure/bicep/releases/download/v1.2.3/bicep-linux-x64",
            }
        ],
    }


@pytest.fixture
def image_root(tmp_path):
    for name in updater.FILES:
        shutil.copyfile(updater.ROOT / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def upstream(monkeypatch):
    document = metadata()
    monkeypatch.setattr(updater, "fetch", lambda url: json.dumps(document).encode())
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(BINARY))
    return document


def test_upgrade_keeps_modules_and_binds_updated_policy(image_root, upstream):
    before = json.loads((image_root / updater.FILES[2]).read_bytes())
    changes, body = updater.updates(image_root, "latest")
    policy = json.loads(changes[updater.FILES[1]])
    manifest = json.loads(changes[updater.FILES[2]])
    assert policy["bicep_version"] == manifest["bicep_version"] == "1.2.3"
    assert manifest["policy_sha256"] == hashlib.sha256(changes[updater.FILES[1]]).hexdigest()
    assert {k: v for k, v in before.items() if k not in {"bicep_version", "policy_sha256"}} == {
        k: v for k, v in manifest.items() if k not in {"bicep_version", "policy_sha256"}
    }
    assert b"ARG BICEP_VERSION=v1.2.3" in changes["Dockerfile"]
    assert DIGEST.removeprefix("sha256:").encode() in changes["Dockerfile"]
    assert "High/Critical" in body and "maintainer approval" in body
    assert set().union(*(set(affected(f"images/bicep-sandbox/{name}")) for name in changes)) == {
        "bicep",
        "bicep-avm",
        "sbx-bicep",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "draft",
        "prerelease",
        "tag",
        "mismatched-release",
        "missing-asset",
        "duplicate-asset",
        "digest",
        "size",
        "oversized",
        "url",
    ],
)
def test_invalid_release_metadata_is_refused(upstream, mutation):
    asset = upstream["assets"][0]
    requested = "latest"
    if mutation in {"draft", "prerelease"}:
        upstream[mutation] = True
    elif mutation == "tag":
        upstream["tag_name"] = "v1.2.3-preview"
    elif mutation == "mismatched-release":
        requested = "v1.2.4"
    elif mutation == "missing-asset":
        upstream["assets"] = []
    elif mutation == "duplicate-asset":
        upstream["assets"].append(copy.deepcopy(asset))
    elif mutation == "digest":
        asset["digest"] = None
    elif mutation == "size":
        asset["size"] = True
    elif mutation == "oversized":
        asset["size"] = updater.LIMIT + 1
    else:
        asset["browser_download_url"] = "https://example.invalid/compiler"
    with pytest.raises(ValueError):
        updater.release_asset(requested)


@pytest.mark.parametrize("data", [BINARY[:-1], BINARY + b"extra", b"x" * len(BINARY)])
def test_binary_drift_does_not_change_any_file(image_root, upstream, monkeypatch, data):
    before = {p.name: p.read_bytes() for p in image_root.iterdir()}
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    with pytest.raises(ValueError, match="asset"):
        updater.updates(image_root, "latest")
    assert before == {p.name: p.read_bytes() for p in image_root.iterdir()}


@pytest.mark.parametrize("mutation", ["version", "policy-hash", "modules", "exclusions"])
def test_inconsistent_existing_lock_refuses_refresh(image_root, upstream, mutation):
    path = image_root / updater.FILES[2]
    manifest = json.loads(path.read_bytes())
    if mutation == "version":
        manifest["bicep_version"] = "0.0.0"
    elif mutation == "policy-hash":
        manifest["policy_sha256"] = "0" * 64
    elif mutation == "modules":
        manifest["modules"] = []
    else:
        manifest["excluded"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="disagree"):
        updater.updates(image_root, "latest")


def test_repeat_is_noop_and_same_release_replacement_refused(image_root, upstream, monkeypatch):
    changes, _ = updater.updates(image_root, "latest")
    for name, data in changes.items():
        (image_root / name).write_bytes(data)
    monkeypatch.setattr(updater, "verify_binary", lambda *a: pytest.fail("no new binary needed"))
    assert updater.updates(image_root, "latest")[0] == {}
    upstream["assets"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="checksum changed"):
        updater.updates(image_root, "latest")


def test_release_downgrade_is_refused(image_root, upstream):
    upstream["tag_name"] = "v0.0.1"
    upstream["assets"][0]["browser_download_url"] = (
        "https://github.com/Azure/bicep/releases/download/v0.0.1/bicep-linux-x64"
    )
    with pytest.raises(ValueError, match="downgrade"):
        updater.updates(image_root, "latest")


def test_cli_check_is_read_only_then_updates_are_idempotent(image_root, upstream, monkeypatch):
    before = {p.name: p.read_bytes() for p in image_root.iterdir()}
    body = image_root / "body.md"
    argv = ["update", "--output", str(image_root), "--body-file", str(body)]
    monkeypatch.setattr(sys, "argv", [*argv, "--check"])
    assert updater.main() == 1
    assert before == {p.name: p.read_bytes() for p in image_root.iterdir()}
    monkeypatch.setattr(sys, "argv", argv)
    assert updater.main() == 0
    assert body.exists()
    monkeypatch.setattr(sys, "argv", [*argv, "--check"])
    assert updater.main() == 0


@pytest.mark.workflow
def test_weekly_image_proposal_is_independent_and_scoped():
    workflow = yaml.safe_load((ROOT / ".github/workflows/bicep-catalog.yml").read_text())
    jobs = workflow["jobs"]
    job = jobs["propose-images"]
    assert "needs" not in job and "if" not in job
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    proposal = next(s for s in job["steps"] if "create-pull-request@" in s.get("uses", ""))
    config = proposal["with"]
    assert config["branch"] != "antsok-bicep-catalog-update"
    assert config["title"].startswith("build:")
    assert set(config["add-paths"].splitlines()) == {
        f"images/bicep-sandbox/{name}" for name in updater.FILES
    }
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "update_bicep_images.py" in scripts and "test_update_bicep_images.py" in scripts
    assert "test_bicep_cli_download.py" in scripts
    assert "docker build" not in scripts and "gh workflow run" not in scripts
    assert set(jobs["report-failure"]["needs"]) == {"propose", "propose-images"}
    assert (
        jobs["report-failure"]["if"]
        == "always() && (needs.propose.result == 'failure' || needs.propose-images.result == 'failure')"
    )
