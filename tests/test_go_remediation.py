"""Image fixes bind binary ownership, source inputs and regenerated provider packages."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "images/terraform-sandbox"))
import build_providers as builder  # noqa: E402
import plan_go_remediation as planner  # noqa: E402
import prepare_go_remediation as preparation  # noqa: E402
from container_release import read, scan_result, write  # noqa: E402

pytestmark = pytest.mark.workflow
AT = "2026-11-06T12:00:00Z"
IMAGE = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
MODULE = "golang.org/x/net"


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    directory = root / planner.CONTEXT
    directory.mkdir(parents=True)
    source = ROOT / planner.CONTEXT
    for path in [
        source / "image.json",
        source / "Dockerfile",
        source / "prepared.Dockerfile",
        *source.glob("dependencies.*.json"),
    ]:
        shutil.copyfile(path, directory / path.name)
    return root


def finding(
    guest_path="/usr/local/bin/terraform",
    module=MODULE,
    installed="v0.60.0",
    fixed="0.62.0",
    identifier="GO-2026-6860",
    artifact_id="module-1",
):
    return {
        "artifact": {
            "id": artifact_id,
            "name": module,
            "version": installed,
            "type": "go-module",
            "locations": [{"path": guest_path}],
        },
        "vulnerability": {
            "id": identifier,
            "severity": "High",
            "fix": {"state": "fixed", "versions": [fixed]},
        },
    }


def retained(tmp_path, matches=None, profile="terraform-random", name="evidence"):
    directory = tmp_path / name
    matches = matches if matches is not None else [finding()]
    artifacts = {item["artifact"]["id"]: item["artifact"] for item in matches}
    sbom = {
        "source": {"type": "image", "metadata": {"imageID": IMAGE}},
        "artifacts": list(artifacts.values()),
    }
    report = {
        "source": {"type": "image", "target": {"imageID": IMAGE}},
        "descriptor": {
            "db": {
                "status": {
                    "valid": True,
                    "schemaVersion": "v6.1.10",
                    "built": AT,
                    "from": "https://grype.anchore.io/databases/example.tar.zst",
                }
            }
        },
        "matches": matches,
        "ignoredMatches": [],
    }
    assessment = scan_result(report, IMAGE, AT, sbom=sbom) | {"digest": DIGEST}
    candidate = {
        "profile": profile,
        "version": "0.2.3",
        "sourceCommit": "c" * 40,
        "sourceRef": "refs/heads/main",
        "attemptId": "123",
        "registryDigest": DIGEST,
        "assessedManifestDigest": DIGEST,
        "imageId": IMAGE,
    }
    write(directory / "grype.json", report)
    write(directory / "sbom.syft.json", sbom)
    write(directory / "observation.json", {"candidate": candidate, "assessment": assessment})
    return directory


def provider_path(provider):
    return (
        f"/opt/maf-terraform/mirror/{provider['source']}/{provider['version']}/"
        f"{provider['platform']}/terraform-provider-{provider['source'].rsplit('/', 1)[-1]}"
        f"_v{provider['version']}_x5"
    )


def provider(root):
    return read(root / planner.CONTEXT / "dependencies.terraform.json")["providers"][0]


def built(provider, work):
    files = {
        "go.mod": json.dumps(provider["source_build"]["modules"], sort_keys=True).encode(),
        "go.sum": b"verified-module-checksums",
        "LICENSE": b"license",
        "terraform-provider-random_v3.9.1": b"compiled-provider",
    }
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    record = {
        "builder": "SOKOLAI",
        "source": provider["source"],
        "version": provider["version"],
        "upstream_artifact": provider["artifact"],
        "inputs": {
            key: value
            for key, value in provider["source_build"].items()
            if key not in {"sha256", "module_files"}
        },
        "files": hashes,
    }
    files["build.json"] = json.dumps(record).encode()
    return builder.package(files), {name: hashes[name] for name in ("go.mod", "go.sum")}


def test_engine_and_provider_occurrences_are_updated_independently(checkout, tmp_path):
    item = provider(checkout)
    match = finding()
    match["artifact"]["locations"].append({"path": provider_path(item)})
    directory = retained(tmp_path, [match])
    proposal = planner.plan(checkout, [directory])
    assert {update["path"] for update in proposal["updates"]} >= {
        planner.RECIPE,
        f"{planner.CONTEXT}/dependencies.terraform.json",
    }
    assert all(update["after"] == "v0.62.0" for update in proposal["updates"])
    assert proposal["affectedProfiles"] == sorted(planner.PROVIDER_MANIFESTS)
    assert planner.plan(checkout, [directory, directory]) == proposal


def test_multiple_findings_take_the_highest_required_fix(checkout, tmp_path):
    matches = [finding(fixed="0.61.0", identifier="GO-first"), finding(fixed="0.62.0")]
    directory = retained(tmp_path, matches)
    proposal = planner.plan(checkout, [directory])
    assert proposal["updates"][0]["after"] == "v0.62.0"
    assert len(proposal["findings"]) == 2


@pytest.mark.parametrize(
    "installed,versions,wanted",
    [
        ("v0.60.0", ["0.62.0", "0.61.0", "1.0.0"], "v0.61.0"),
        ("v1.19.2", ["1.20.2"], "v1.20.2"),
        ("v1.19.2", ["2.0.0", "1.20.0-rc.1", "1.19.1"], None),
        ("v1.19.2", [], None),
    ],
)
def test_stable_fix_selection_does_not_cross_major_or_downgrade(installed, versions, wanted):
    assert planner.fixed_version(installed, {"state": "fixed", "versions": versions}) == wanted


@pytest.mark.parametrize(
    "change,status",
    [
        (lambda m: m["vulnerability"]["fix"].update(state="unknown"), "no-compatible-fix"),
        (lambda m: m["vulnerability"]["fix"].update(versions=None), "no-compatible-fix"),
        (lambda m: m["vulnerability"]["fix"].update(versions=["1.0.0"]), "no-compatible-fix"),
        (
            lambda m: m["artifact"].update(version="v0.60.1-0.20260101000000-abcdef"),
            "unsupported-version",
        ),
        (lambda m: m["artifact"].update(name="stdlib"), "unsupported-component"),
        (lambda m: m["artifact"].update(type="apk"), "unsupported-component"),
        (lambda m: m["artifact"].update(name="-exec/attack"), "unsupported-component"),
        (lambda m: m["artifact"].update(locations=[]), "unmapped-binary"),
        (
            lambda m: m["artifact"].update(locations=[{"path": "/usr/local/bin/tofu"}]),
            "unmapped-binary",
        ),
    ],
)
def test_unsupported_findings_are_retained_without_changes(checkout, tmp_path, change, status):
    match = finding()
    change(match)
    proposal = planner.plan(checkout, [retained(tmp_path, [match])])
    assert proposal["updates"] == []
    assert proposal["findings"][0]["decisions"][0]["status"] == status


@pytest.mark.parametrize(
    "fault",
    [
        "image",
        "sbom",
        "digest",
        "database",
        "ignored",
        "observation",
        "component",
        "location",
        "duplicate-id",
    ],
)
def test_inconsistent_evidence_is_refused(checkout, tmp_path, fault):
    directory = retained(tmp_path)
    report, sbom, observation = (
        read(directory / name) for name in ("grype.json", "sbom.syft.json", "observation.json")
    )
    if fault == "image":
        report["source"]["target"]["imageID"] = DIGEST
    elif fault == "sbom":
        sbom["source"]["metadata"]["imageID"] = DIGEST
    elif fault == "digest":
        observation["assessment"]["digest"] = IMAGE
    elif fault == "database":
        report["descriptor"]["db"]["status"]["valid"] = False
    elif fault == "ignored":
        report["ignoredMatches"] = report["matches"]
    elif fault == "observation":
        observation["assessment"]["findings"] = []
    elif fault == "component":
        sbom["artifacts"][0]["version"] = "v0.1.0"
    elif fault == "location":
        sbom["artifacts"][0]["locations"] = [{"path": "/somewhere/else"}]
    elif fault == "duplicate-id":
        sbom["artifacts"].append(copy.deepcopy(sbom["artifacts"][0]))
    for name, value in (
        ("grype.json", report),
        ("sbom.syft.json", sbom),
        ("observation.json", observation),
    ):
        write(directory / name, value)
    with pytest.raises(ValueError):
        planner.plan(checkout, [directory])


def test_existing_override_is_not_downgraded_or_called_a_verified_fix(checkout, tmp_path):
    recipe = read(checkout / planner.RECIPE)
    recipe["engines"]["terraform"]["source_build"]["modules"][MODULE] = "v0.63.0"
    write(checkout / planner.RECIPE, recipe)
    proposal = planner.plan(checkout, [retained(tmp_path)])
    assert proposal["updates"] == []
    assert proposal["findings"][0]["decisions"][0]["status"] == "override-already-sufficient"


def test_shared_provider_locks_are_rebuilt_once_and_keep_upstream_identity(
    checkout, tmp_path, monkeypatch
):
    original = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    duplicate = checkout / planner.CONTEXT / "dependencies.shared.json"
    write(duplicate, original)
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    expected = planner.plan(checkout, [directory])
    calls = []

    def build(item, work):
        calls.append(copy.deepcopy(item))
        return built(item, work)

    monkeypatch.setattr(preparation, "rebuild", build)
    destination = tmp_path / "proposal"
    preparation.prepare(checkout, [directory], expected, destination)
    assert len(calls) == 1
    proposed = read(destination / "files" / planner.CONTEXT / "dependencies.terraform.json")
    assert read(destination / "files" / planner.CONTEXT / "dependencies.shared.json") == proposed
    before, after = original["providers"][0], proposed["providers"][0]
    assert before["artifact"] == after["artifact"]
    assert before["source_build"]["revision"] == after["source_build"]["revision"]
    assert after["source_build"]["modules"][MODULE] == "v0.62.0"
    assert after["source_build"]["sha256"] != before["source_build"]["sha256"]
    assert after["source_build"]["module_files"] != before["source_build"]["module_files"]
    assert read(checkout / planner.CONTEXT / "dependencies.terraform.json") == original
    assert read(destination / "proposal.json")["qualification"] == "pending-image-build-and-rescan"


def test_shared_provider_source_drift_is_not_silently_overwritten(checkout, tmp_path):
    duplicate = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    duplicate["providers"][0]["source_build"]["go_version"] = "go1.27.3"
    write(checkout / planner.CONTEXT / "dependencies.shared.json", duplicate)
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    with pytest.raises(ValueError, match="different source"):
        planner.plan(checkout, [directory])


def test_upstream_archives_are_not_converted_to_source_builds(checkout, tmp_path):
    duplicate = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    del duplicate["providers"][0]["source_build"]
    path = f"{planner.CONTEXT}/dependencies.upstream.json"
    write(checkout / path, duplicate)
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    proposal = planner.plan(checkout, [directory])
    assert proposal["updates"]
    assert all(update["path"] != path for update in proposal["updates"])
    assert path in proposal["inputs"]


def test_opentofu_compress_fix_maps_engine_and_all_provider_source_locks(checkout, tmp_path):
    manifest = read(checkout / planner.CONTEXT / "dependencies.opentofu-avm-network.json")
    item = manifest["providers"][0]
    match = finding(
        "/usr/local/bin/tofu",
        module="github.com/klauspost/compress",
        installed="v1.19.1",
        fixed="1.20.2",
    )
    match["artifact"]["locations"].append({"path": provider_path(item)})
    proposal = planner.plan(checkout, [retained(tmp_path, [match], profile="opentofu-avm")])
    assert {(update["path"], update["owner"]) for update in proposal["updates"]} == {
        (planner.RECIPE, "opentofu"),
        (f"{planner.CONTEXT}/dependencies.opentofu-avm-network.json", "0"),
    }
    assert all(update["after"] == "v1.20.2" for update in proposal["updates"])


@pytest.mark.parametrize("fault", ["source", "evidence", "plan", "new-manifest", "toolchain"])
def test_stale_plan_is_refused_before_building(checkout, tmp_path, monkeypatch, fault):
    directory = retained(tmp_path)
    expected = planner.plan(checkout, [directory])
    if fault == "source":
        with (checkout / planner.RECIPE).open("a") as stream:
            stream.write("\n")
    elif fault == "evidence":
        with (directory / "grype.json").open("a") as stream:
            stream.write("\n")
    elif fault == "plan":
        expected["updates"][0]["after"] = "v9.0.0"
    elif fault == "new-manifest":
        shutil.copyfile(
            checkout / planner.CONTEXT / "dependencies.terraform.json",
            checkout / planner.CONTEXT / "dependencies.new.json",
        )
    elif fault == "toolchain":
        with (checkout / planner.CONTEXT / "prepared.Dockerfile").open("a") as stream:
            stream.write("\n")
    monkeypatch.setattr(preparation, "rebuild", lambda *_: pytest.fail("must not build"))
    with pytest.raises(ValueError, match="stale"):
        preparation.prepare(checkout, [directory], expected, tmp_path / "proposal")
    assert not (tmp_path / "proposal").exists()


@pytest.mark.parametrize("fault", ["builder", "invalid-package", "concurrent-source"])
def test_failed_preparation_does_not_publish_partial_files(checkout, tmp_path, monkeypatch, fault):
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    expected = planner.plan(checkout, [directory])

    def build(item, work):
        if fault == "builder":
            raise RuntimeError("build failed")
        data, hashes = built(item, work)
        if fault == "invalid-package":
            hashes["go.sum"] = "f" * 64
        if fault == "concurrent-source":
            with (checkout / planner.RECIPE).open("a") as stream:
                stream.write("\n")
        return data, hashes

    monkeypatch.setattr(preparation, "rebuild", build)
    with pytest.raises((ValueError, RuntimeError)):
        preparation.prepare(checkout, [directory], expected, tmp_path / "proposal")
    assert not (tmp_path / "proposal").exists()
    assert provider(checkout)["source_build"]["modules"][MODULE] == "v0.60.0"


def test_commands_prepare_reviewable_files_and_replanning_is_idempotent(checkout, tmp_path):
    directory = retained(tmp_path)
    planned, output = tmp_path / "plan.json", tmp_path / "proposal"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/plan_go_remediation.py"),
            "--root",
            str(checkout),
            "--evidence",
            str(directory),
            "--output",
            str(planned),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prepare_go_remediation.py"),
            "--root",
            str(checkout),
            "--evidence",
            str(directory),
            "--plan",
            str(planned),
            "--output",
            str(output),
        ],
        check=True,
    )
    assert read(output / "proposal.json")["files"].keys() == {planner.RECIPE}
    shutil.copyfile(output / "files" / planner.RECIPE, checkout / planner.RECIPE)
    assert planner.plan(checkout, [directory])["updates"] == []


@pytest.mark.parametrize("inside", [True, False])
def test_proposal_never_overwrites_checkout_or_existing_output(checkout, tmp_path, inside):
    directory = retained(tmp_path)
    output = checkout / "proposal" if inside else tmp_path / "existing"
    if not inside:
        output.mkdir()
    with pytest.raises(ValueError):
        preparation.prepare(checkout, [directory], planner.plan(checkout, [directory]), output)
