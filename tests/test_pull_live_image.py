"""Published sample verification consumes authenticated images without a build fallback."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_kind_release_images as legacy
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kind_release_images as guard  # noqa: E402
import consumer_qualification as consumer  # noqa: E402
import pull_live_image as images  # noqa: E402

pytestmark = pytest.mark.workflow
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
HEAD = "b" * 40
DIGEST = "sha256:" + "c" * 64
IMAGE_ID = "sha256:" + "d" * 64
repo = legacy.repo
matching_source = guard.matching_source


@pytest.fixture(params=list(images.PROFILE_ENV))
def published(request, monkeypatch):
    profile = request.param
    candidate = {"profile": profile, "imageId": IMAGE_ID, "attemptId": "123"}
    record = {
        "profile": profile,
        "sourceCommit": SOURCE,
        "registryDigest": DIGEST,
        "imageId": IMAGE_ID,
        "candidate": candidate,
    }
    name = f"{profile}/0.1.0"
    catalogue = {"current": {profile: name}, "releases": {name: record}}
    qualification = {
        "candidate": candidate,
        "runId": "123",
        "anonymousPullVerified": True,
        "identity": {"candidateIdentityVerified": True},
        "runtime": {
            "passed": True,
            "profile": profile,
            "imageId": IMAGE_ID,
            "containersRemaining": [],
        },
    }
    calls = []

    def evidence(client, selected, directory):
        calls.append((client, selected))
        (directory / "manifest.json").write_text("{}")
        (directory / "evidence-index.json").write_text('{"qualification.json": "verified"}')
        (directory / "qualification.json").write_text(json.dumps(qualification))
        return "https://github.com/sokolaidev/maf-extensions/releases/tag/fixture"

    client = object()
    monkeypatch.setattr(images, "GitHub", lambda: client)
    monkeypatch.setattr(
        images,
        "History",
        lambda _: SimpleNamespace(head=lambda: SimpleNamespace(catalogue=catalogue)),
    )
    comparisons = []

    def compare(*args):
        comparisons.append(args)

    monkeypatch.setattr(guard, "matching_source", compare)
    monkeypatch.setattr(images, "release_evidence", evidence)
    return SimpleNamespace(
        profile=profile,
        record=record,
        catalogue=catalogue,
        qualification=qualification,
        calls=calls,
        comparisons=comparisons,
        client=client,
    )


def test_resolves_matching_authenticated_qualified_digest(published):
    result = images.resolve(ROOT, HEAD, published.profile)
    assert result["image"] == f"{images.PREFIX}/{published.profile}@{DIGEST}"
    assert result["imageId"] == IMAGE_ID
    assert result["sourceCommit"] == SOURCE and result["sampleCommit"] == HEAD
    assert published.calls == [(published.client, published.record)]
    assert published.comparisons == [
        (ROOT, SOURCE, HEAD, images.PROFILE_PACKAGE[published.profile], published.profile)
    ]
    assert result["consumerQualification"] == []


@pytest.mark.parametrize("evidence", ["fresh", "stale", "other-digest", "unavailable"])
def test_changed_image_source_requires_matching_consumer_evidence(
    published, repo, monkeypatch, evidence
):
    root, _ = repo
    package = images.PROFILE_PACKAGE[published.profile]
    if package != legacy.PACKAGE:
        directory = root / "packages" / package
        (directory / "src").mkdir(parents=True)
        (directory / "src/module.py").write_text("VALUE = 1\n")
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "0.1.0"\n'
        )
    source = legacy.commit(root) if package != legacy.PACKAGE else guard.revision(root, "HEAD")
    published.record["sourceCommit"] = source
    recipe = (
        root
        / "images"
        / ("shell-sandbox" if published.profile == "shell" else "terraform-sandbox")
        / "Dockerfile"
    )
    recipe.parent.mkdir(parents=True)
    recipe.write_text("FROM changed\n")
    head = legacy.commit(root)
    monkeypatch.setattr(guard, "matching_source", matching_source)
    with pytest.raises(ValueError, match="image inputs"):
        matching_source(root, source, head, package, published.profile)
    expected = consumer.inputs(
        root,
        source if evidence == "stale" else head,
        package,
        published.profile,
        "docker",
        published.record,
    )
    if evidence == "other-digest":
        expected["registryDigest"] = "sha256:" + "e" * 64
    seen = []

    def verify(value):
        seen.append(value)
        if evidence == "unavailable" or value != expected:
            raise ValueError("Matching authenticated consumer qualification unavailable")
        return {"sourceCommit": head, "runId": "456"}

    monkeypatch.setattr(consumer, "verify", verify)
    if evidence == "fresh":
        result = images.resolve(root, head, published.profile)
        assert result["image"] == f"{images.PREFIX}/{published.profile}@{DIGEST}"
        assert result["consumerQualification"] == [
            {
                "backend": "docker",
                "sourceCommit": head,
                "evidence": f"https://github.com/{consumer.REPOSITORY}/actions/runs/456",
            }
        ]
        assert published.calls == [(published.client, published.record)]
    else:
        with pytest.raises(ValueError, match="Matching authenticated consumer qualification"):
            images.resolve(root, head, published.profile)
        assert not published.calls
    assert len(seen) == 1
    assert seen[0]["package"] == package
    assert seen[0]["profile"] == published.profile
    assert seen[0]["registryDigest"] == DIGEST


@pytest.mark.parametrize(
    "failure", ["missing", "profile", "compatibility", "evidence", "qualification", "changed"]
)
def test_refuses_unusable_release(published, monkeypatch, failure):
    if failure == "missing":
        published.catalogue["current"].clear()
    elif failure == "profile":
        published.record["profile"] = "other"
    elif failure == "compatibility":

        def incompatible(*args):
            raise ValueError("No matching consumer evidence")

        monkeypatch.setattr(images, "compatibility", incompatible)
    elif failure == "evidence":

        def reject(*args):
            raise ValueError("Invalid signed evidence")

        monkeypatch.setattr(images, "release_evidence", reject)
    elif failure == "qualification":
        published.qualification["runtime"]["passed"] = False
    else:
        snapshots = iter([SimpleNamespace(catalogue=published.catalogue), None])
        monkeypatch.setattr(
            images, "History", lambda _: SimpleNamespace(head=lambda: next(snapshots))
        )
    with pytest.raises(ValueError):
        images.resolve(ROOT, HEAD, published.profile)


def test_no_catalogue_is_an_error(monkeypatch):
    monkeypatch.setattr(images, "History", lambda _: SimpleNamespace(head=lambda: None))
    with pytest.raises(ValueError, match="No completed image catalogue"):
        images.resolve(ROOT, HEAD, "shell")


@pytest.mark.parametrize("failure", [None, "pull", "identity", "evidence"])
def test_cli_exports_only_verified_pulled_digest(published, monkeypatch, tmp_path, failure):
    environment = tmp_path / "environment"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_ENV", str(environment))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["pull_live_image.py", "--profile", published.profile])
    monkeypatch.setattr(images, "revision", lambda *args: HEAD)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if failure == "pull":
            raise subprocess.CalledProcessError(1, command)
        details = {
            "Id": "wrong" if failure == "identity" else IMAGE_ID,
            "Os": "linux",
            "Architecture": "amd64",
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(details))

    monkeypatch.setattr(images.subprocess, "run", run)
    if failure == "evidence":
        published.qualification["anonymousPullVerified"] = False
    if failure:
        with pytest.raises(SystemExit) as error:
            images.main()
        assert error.value.code == 1
        assert not environment.exists()
        assert not summary.exists()
        if failure == "evidence":
            assert not commands
    else:
        images.main()
        reference = f"{images.PREFIX}/{published.profile}@{DIGEST}"
        assert commands == [
            ["docker", "pull", "--platform", "linux/amd64", reference],
            ["docker", "image", "inspect", "--format", "{{json .}}", reference],
        ]
        assert environment.read_text() == f"{images.PROFILE_ENV[published.profile]}={reference}\n"
        assert all(value in summary.read_text() for value in (reference, IMAGE_ID, SOURCE, HEAD))
    assert all("build" not in command for command in commands)


@pytest.mark.parametrize(
    "job,profile",
    [
        ("sample-21", "shell"),
        ("sample-20-docker-terraform", "terraform-random"),
        ("sample-20-docker-opentofu", "opentofu-random"),
    ],
)
def test_workflow_pulls_published_and_builds_only_branch(job, profile):
    workflow = yaml.safe_load((ROOT / ".github/workflows/verify-live.yml").read_text("utf-8"))
    assert workflow["jobs"][job]["permissions"]["attestations"] == "read"
    publisher = yaml.safe_load((ROOT / ".github/workflows/publish-packages.yml").read_text("utf-8"))
    assert publisher["jobs"]["verify"]["permissions"]["attestations"] == "read"
    steps = workflow["jobs"][job]["steps"]
    checkout = steps[0]["with"]
    assert checkout["fetch-depth"] == 0
    assert checkout["persist-credentials"] is False
    harness = next(step for step in steps if step.get("with", {}).get("path") == ".harness")
    assert {"scripts", "images"} <= set(harness["with"]["sparse-checkout"].splitlines())
    pull = next(step for step in steps if "pull_live_image.py" in step.get("run", ""))
    assert pull["if"] == "env.SOURCE == 'published'"
    assert pull["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert pull["run"] == (
        f'python3 "$HARNESS"/scripts/pull_live_image.py --profile {profile} --root "$GITHUB_WORKSPACE"'
    )
    builds = [step for step in steps if "build_" in step.get("run", "")]
    assert len(builds) == 1 and builds[0]["if"] == "env.SOURCE == 'branch'"
    sample = next(step for step in steps if "uv run --no-project" in step.get("run", ""))
    assert steps.index(pull) < steps.index(sample)
    assert steps.index(builds[0]) < steps.index(sample)
    assert (
        workflow["on" if "on" in workflow else True]["workflow_call"]["inputs"]["source"]["default"]
        == "published"
    )
