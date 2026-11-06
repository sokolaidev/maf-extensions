"""Image fixes bind binary ownership, source inputs and regenerated provider packages."""

from __future__ import annotations

import copy
import hashlib
import importlib.machinery
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
import go_remediation_worker as worker  # noqa: E402
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
        ("v0.60.0", ["0.62.0", "0.61.0", "1.0.0"], "v0.62.0"),
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
        "assessed-manifest",
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
    elif fault == "assessed-manifest":
        observation["candidate"]["assessedManifestDigest"] = IMAGE
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


def test_evidence_preserves_the_complete_validated_identity(tmp_path):
    directory = retained(tmp_path)
    observation = read(directory / "observation.json")
    observation["candidate"].update(buildInputsSha256=IMAGE, preparationEvidenceSha256=DIGEST)
    write(directory / "observation.json", observation)
    identity, _, _ = planner.evidence(directory)
    assert all(identity[key] == value for key, value in observation["candidate"].items())


def test_existing_override_is_not_downgraded_or_called_a_verified_fix(checkout, tmp_path):
    recipe = read(checkout / planner.RECIPE)
    recipe["engines"]["terraform"]["source_build"]["modules"][MODULE] = "v0.63.0"
    write(checkout / planner.RECIPE, recipe)
    proposal = planner.plan(checkout, [retained(tmp_path)])
    assert proposal["updates"] == []
    assert proposal["findings"][0]["decisions"][0]["status"] == "override-already-sufficient"


def test_existing_override_on_another_minor_line_still_receives_its_fix(checkout, tmp_path):
    recipe = read(checkout / planner.RECIPE)
    recipe["engines"]["terraform"]["source_build"]["modules"][MODULE] = "v1.20.1"
    write(checkout / planner.RECIPE, recipe)
    match = finding(installed="v1.19.1")
    match["vulnerability"]["fix"]["versions"] = ["1.19.2", "1.20.2"]
    proposal = planner.plan(checkout, [retained(tmp_path, [match])])
    assert proposal["updates"][0]["after"] == "v1.20.2"


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_target_majors_are_refused(checkout, tmp_path, reverse):
    directories = [
        retained(tmp_path, [finding(module="example.com/dep", installed=old, fixed=new)], name=name)
        for old, new, name in [("v0.9.0", "0.10.0", "zero"), ("v1.0.0", "1.1.0", "one")]
    ]
    with pytest.raises(ValueError, match="Conflicting target majors"):
        planner.plan(checkout, list(reversed(directories)) if reverse else directories)


def test_required_companion_updates_propagate_to_shared_locks(checkout, tmp_path, monkeypatch):
    original = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    original["providers"][0]["source_build"]["modules"]["golang.org/x/crypto"] = "v0.57.0"
    for name in ("dependencies.terraform.json", "dependencies.terraform-avm.json"):
        document = read(checkout / planner.CONTEXT / name)
        for item in document["providers"]:
            if item["source"] == original["providers"][0]["source"]:
                item["source_build"]["modules"]["golang.org/x/crypto"] = "v0.57.0"
        write(checkout / planner.CONTEXT / name, document)
    write(checkout / planner.CONTEXT / "dependencies.shared.json", original)
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    expected = planner.plan(checkout, [directory])
    calls = []

    def build(item, work, tooling):
        calls.append(item)
        item["source_build"]["modules"]["golang.org/x/crypto"] = "v0.58.0"
        return built(item, work)

    monkeypatch.setattr(preparation, "rebuild", build)
    destination = tmp_path / "proposal"
    preparation.prepare(checkout, [directory], expected, destination)
    assert len(calls) == 1
    report = read(destination / "proposal.json")
    assert len(report["companionUpdates"]) == 3
    for change in report["companionUpdates"]:
        assert change["before"] == "v0.57.0"
        assert change["after"] == "v0.58.0"
        item = read(destination / "files" / change["path"])["providers"][int(change["owner"])]
        assert item["source_build"]["modules"][change["module"]] == change["after"]


@pytest.mark.parametrize("fault", [None, "downgrade", "major", "replacement", "missing"])
def test_companion_resolution_uses_go_graph_and_refuses_unsafe_changes(
    tmp_path, monkeypatch, fault
):
    requested = {MODULE: "v0.62.0", "golang.org/x/crypto": "v0.57.0"}
    selected = {MODULE: "v0.62.0", "golang.org/x/crypto": "v0.58.0"}
    if fault == "downgrade":
        selected[MODULE] = "v0.60.0"
    if fault == "major":
        selected[MODULE] = "v1.0.0"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["GOTOOLCHAIN"] == "local"

    def output(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return json.dumps({"Path": MODULE, "Version": "v0.60.0"})
        rows: list[dict[str, object]] = [
            {"Path": name, "Version": version} for name, version in sorted(selected.items())
        ]
        if fault == "replacement":
            rows[0]["Replace"] = {"Dir": "/other"}
        if fault == "missing":
            rows.pop()
        return "\n".join(json.dumps(row) for row in rows)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    if fault is None:
        assert (
            preparation.resolve_modules(requested, tmp_path, {"GOTOOLCHAIN": "local"}) == selected
        )
        assert commands[1] == [
            "go",
            "mod",
            "edit",
            *[f"-require={n}@{v}" for n, v in sorted(requested.items())],
        ]
        assert commands[0] == commands[2] == ["go", "list", "-m", "-json", "all"]
    else:
        with pytest.raises(ValueError):
            preparation.resolve_modules(requested, tmp_path, {"GOTOOLCHAIN": "local"})


def test_remediation_build_resolves_companions_before_exact_pin_verification(tmp_path, monkeypatch):
    item = provider(ROOT)
    item["source_build"]["modules"] = {MODULE: "v0.62.0", "golang.org/x/crypto": "v0.57.0"}
    calls = []

    def output(command, **kwargs):
        if command == ["go", "env", "GOVERSION"]:
            return item["source_build"]["go_version"]
        if command[-1] == "all":
            return "\n".join(
                json.dumps({"Path": name, "Version": version})
                for name, version in {MODULE: "v0.62.0", "golang.org/x/crypto": "v0.58.0"}.items()
            )
        return json.dumps({"Version": "v0.1.0"})

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["go", "get"]:
            assert "golang.org/x/crypto@v0.58.0" in command
        assert command[:2] != ["go", "build"]

    monkeypatch.setattr(builder.subprocess, "check_output", output)
    monkeypatch.setattr(builder.subprocess, "run", run)
    monkeypatch.setattr(builder.prep, "fetch", lambda *a, **kw: b"source")
    monkeypatch.setattr(builder, "unpack", lambda *a: None)
    monkeypatch.setattr(worker.importlib.util, "module_from_spec", lambda *_: builder)
    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", lambda *_: None)
    with pytest.raises(ValueError, match="security dependency mismatch"):
        worker.build(item, tmp_path)
    assert calls[0][:3] == ["go", "mod", "edit"]
    assert ["go", "mod", "verify"] in calls


@pytest.mark.parametrize("source_version", ["v0.63.0", "v1.0.0"])
def test_remediation_preserves_source_graph_requirements(tmp_path, monkeypatch, source_version):
    selected = {MODULE: source_version}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        for flag in command[3:]:
            name, version = flag.removeprefix("-require=").split("@")
            selected[name] = version

    def output(command, **kwargs):
        commands.append(command)
        return json.dumps({"Path": MODULE, "Version": selected[MODULE]})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    if source_version == "v1.0.0":
        with pytest.raises(ValueError, match="Unsafe"):
            preparation.resolve_modules({MODULE: "v0.62.0"}, tmp_path, {})
        assert all(command[:3] != ["go", "mod", "edit"] for command in commands)
    else:
        assert preparation.resolve_modules({MODULE: "v0.62.0"}, tmp_path, {}) == selected
        assert selected[MODULE] == source_version


@pytest.mark.parametrize("sum_exists", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_resolver_restores_module_files_for_builder_replay(tmp_path, monkeypatch, sum_exists, fail):
    original = {"go.mod": b"original module bytes\r\n", "go.sum": b"original sums\r\n"}
    (tmp_path / "go.mod").write_bytes(original["go.mod"])
    if sum_exists:
        (tmp_path / "go.sum").write_bytes(original["go.sum"])
    selected = "v0.60.0"

    def output(command, **kwargs):
        (tmp_path / "go.sum").write_bytes(b"extra graph checksums")
        return json.dumps({"Path": MODULE, "Version": selected})

    def run(command, **kwargs):
        nonlocal selected
        (tmp_path / "go.mod").write_bytes(b"resolved requirements")
        selected = "v0.62.0"
        if fail:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            preparation.resolve_modules({MODULE: "v0.62.0"}, tmp_path, {})
    else:
        assert preparation.resolve_modules({MODULE: "v0.62.0"}, tmp_path, {}) == {MODULE: "v0.62.0"}
    assert (tmp_path / "go.mod").read_bytes() == original["go.mod"]
    if sum_exists:
        assert (tmp_path / "go.sum").read_bytes() == original["go.sum"]
    else:
        assert not (tmp_path / "go.sum").exists()


def test_shared_provider_locks_are_rebuilt_once_and_keep_upstream_identity(
    checkout, tmp_path, monkeypatch
):
    original = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    duplicate = checkout / planner.CONTEXT / "dependencies.shared.json"
    write(duplicate, original)
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    expected = planner.plan(checkout, [directory])
    calls = []

    def build(item, work, tooling):
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


@pytest.mark.parametrize(
    "changed", ["build_providers.py", "terraform_dependencies.py", "runner.py"]
)
def test_builder_uses_verified_tooling_despite_transient_checkout_edits(
    checkout, tmp_path, monkeypatch, changed
):
    tooling = tmp_path / "tooling"
    for name in (*planner.TOOLING, f"{planner.CONTEXT}/runner.py"):
        target = tooling / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    item = provider(checkout)
    item["source_build"]["modules"][MODULE] = "v0.62.0"
    data, hashes = built(item, tmp_path)
    builder_path = tooling / planner.CONTEXT / "build_providers.py"
    builder_path.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
        "from scripts import terraform_dependencies\n"
        "def build(provider, work, **kwargs):\n"
        f"    return {data!r}, {hashes!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(planner, "ROOT", tooling)
    monkeypatch.setattr(preparation, "ROOT", tooling)
    directory = retained(tmp_path, [finding(provider_path(item))])
    expected = planner.plan(checkout, [directory])
    actual_rebuild = preparation.rebuild
    path = tooling / (
        f"scripts/{changed}"
        if changed == "terraform_dependencies.py"
        else f"{planner.CONTEXT}/{changed}"
    )
    original = path.read_bytes()

    def build(*args):
        monkeypatch.delitem(sys.modules, "scripts", raising=False)
        monkeypatch.delitem(sys.modules, "scripts.terraform_dependencies", raising=False)
        path.write_text("raise AssertionError('transient tooling executed')\n", encoding="utf-8")
        try:
            return actual_rebuild(*args)
        finally:
            path.write_bytes(original)

    monkeypatch.setattr(preparation, "rebuild", build)
    destination = tmp_path / "proposal"
    preparation.prepare(checkout, [directory], expected, destination)
    package = read(destination / "proposal.json")["providers"][0]["package"]
    assert (destination / package).read_bytes() == data


def test_tooling_snapshot_hashes_the_bytes_it_copies(tmp_path, monkeypatch):
    expected = {name: planner.digest(ROOT / name) for name in planner.TOOLING}
    original_read = Path.read_bytes
    target = ROOT / planner.CONTEXT / "build_providers.py"

    def read_bytes(path):
        raw = original_read(path)
        return raw + b"\nraise AssertionError('different tooling')\n" if path == target else raw

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(ValueError, match="tooling snapshot"):
        preparation.snapshot_tooling(expected, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


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

    def build(item, work, tooling):
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


def test_concurrent_output_directory_is_never_replaced(checkout, tmp_path, monkeypatch):
    directory = retained(tmp_path)
    expected = planner.plan(checkout, [directory])
    destination = tmp_path / "proposal"
    original_plan = preparation.plan
    calls = 0
    identity = None

    def replan(root, directories):
        nonlocal calls, identity
        result = original_plan(root, directories)
        calls += 1
        if calls == 2:
            destination.mkdir()
            identity = destination.stat().st_ino
        return result

    monkeypatch.setattr(preparation, "plan", replan)
    with pytest.raises(FileExistsError):
        preparation.prepare(checkout, [directory], expected, destination)
    assert list(destination.iterdir()) == []
    assert destination.stat().st_ino == identity


def test_publication_without_exclusive_rename_support_fails_closed(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    monkeypatch.setattr(preparation.sys, "platform", "unsupported")
    with pytest.raises(OSError, match="Exclusive proposal publication"):
        preparation.publish(source, destination)
    assert source.is_dir()
    assert not destination.exists()


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


@pytest.mark.parametrize("destination", ["source", "new-source", "symlink", "hardlink", "evidence"])
def test_plan_output_cannot_overwrite_inputs(checkout, tmp_path, monkeypatch, destination):
    directory = retained(tmp_path)
    original = (checkout / planner.RECIPE).read_bytes()
    report = (directory / "grype.json").read_bytes()
    output = tmp_path / "plan.json"
    if destination == "source":
        output = checkout / planner.RECIPE
    elif destination == "new-source":
        output = checkout / "new" / "plan.json"
    elif destination == "evidence":
        output = directory / "grype.json"
    else:
        try:
            if destination == "symlink":
                output.symlink_to(checkout / planner.RECIPE)
            else:
                output.hardlink_to(checkout / planner.RECIPE)
        except OSError:
            pytest.skip("Filesystem links unavailable")
    monkeypatch.setattr(
        sys,
        "argv",
        ["plan", "--root", str(checkout), "--evidence", str(directory), "--output", str(output)],
    )
    with pytest.raises((ValueError, FileExistsError)):
        planner.main()
    assert (checkout / planner.RECIPE).read_bytes() == original
    assert (directory / "grype.json").read_bytes() == report
    assert not (checkout / "new").exists()


@pytest.mark.parametrize("component", ["terraform", "opentofu", "provider"])
def test_main_module_requires_source_upgrade_using_inventory_metadata(
    checkout, tmp_path, component
):
    module = "github.com/hashicorp/terraform"
    guest_path = "/usr/local/bin/terraform"
    profile = "terraform-random"
    if component == "opentofu":
        module, guest_path, profile = (
            "github.com/opentofu/opentofu",
            "/usr/local/bin/tofu",
            "opentofu-random",
        )
    elif component == "provider":
        module, guest_path = (
            "github.com/hashicorp/terraform-provider-random",
            provider_path(provider(checkout)),
        )
    match = finding(guest_path, module=module, installed="v1.2.0", fixed="1.2.1")
    directory = retained(tmp_path, [match], profile=profile)
    sbom = read(directory / "sbom.syft.json")
    sbom["artifacts"][0]["metadata"] = {"mainModule": module}
    write(directory / "sbom.syft.json", sbom)
    result = planner.plan(checkout, [directory])
    assert result["updates"] == []
    assert result["findings"][0]["decisions"] == [{"status": "source-version-upgrade"}]


def restore_on_read(monkeypatch, path, original):
    read_bytes, read_text = Path.read_bytes, Path.read_text

    def bytes_and_restore(target):
        result = read_bytes(target)
        if target == path:
            target.write_bytes(original)
        return result

    def text_and_restore(target, *args, **kwargs):
        result = read_text(target, *args, **kwargs)
        if target == path:
            target.write_bytes(original)
        return result

    monkeypatch.setattr(Path, "read_bytes", bytes_and_restore)
    monkeypatch.setattr(Path, "read_text", text_and_restore)


@pytest.mark.parametrize("filename", ["image.json", "dependencies.terraform.json"])
def test_source_documents_and_hashes_use_the_same_bytes(checkout, monkeypatch, filename):
    path = checkout / planner.CONTEXT / filename
    original = path.read_bytes()
    changed = read(path)
    source = (
        changed["engines"]["terraform"] if filename == "image.json" else changed["providers"][0]
    )
    source["source_build"]["modules"][MODULE] = "v0.61.0"
    raw = (json.dumps(changed) + "\n").encode()
    path.write_bytes(raw)
    restore_on_read(monkeypatch, path, original)
    documents, hashes = planner.inputs(checkout)
    name = path.relative_to(checkout).as_posix()
    assert documents[name] == changed
    assert hashes[name] == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == original


def test_preparation_rejects_a_transient_snapshot_before_building(checkout, tmp_path, monkeypatch):
    directory = retained(tmp_path, [finding(provider_path(provider(checkout)))])
    expected = planner.plan(checkout, [directory])
    path = checkout / planner.CONTEXT / "dependencies.terraform.json"
    original = path.read_bytes()
    changed = read(path)
    changed["providers"][0]["source_build"]["modules"][MODULE] = "v0.61.0"
    original_inputs = planner.inputs

    def transient_inputs(root):
        write(path, changed)
        try:
            return original_inputs(root)
        finally:
            path.write_bytes(original)

    monkeypatch.setattr(preparation, "inputs", transient_inputs)
    monkeypatch.setattr(
        preparation, "rebuild", lambda *_: pytest.fail("must reject before building")
    )
    with pytest.raises(ValueError, match="snapshot"):
        preparation.prepare(checkout, [directory], expected, tmp_path / "proposal")
    assert not (tmp_path / "proposal").exists()


@pytest.mark.parametrize("filename", ["observation.json", "sbom.syft.json", "grype.json"])
def test_evidence_hashes_bind_the_parsed_snapshot(tmp_path, monkeypatch, filename):
    directory = retained(tmp_path)
    path = directory / filename
    original = path.read_bytes()
    changed = read(path)
    changed["transient"] = True
    raw = (json.dumps(changed) + "\n").encode()
    path.write_bytes(raw)
    restore_on_read(monkeypatch, path, original)
    _, _, hashes = planner.evidence(directory)
    assert hashes[filename] == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == original
