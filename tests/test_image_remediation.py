"""Automatic image fixes require current evidence, complete rebuilds and exclusive bot ownership."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_remediation as remediation  # noqa: E402
import image_remediation_evidence as acquisition  # noqa: E402
import image_remediation_pr as publication  # noqa: E402
import plan_go_remediation as planner  # noqa: E402
import prepare_go_remediation as preparation  # noqa: E402
from container_release import REPOSITORY, read, scan_result, write  # noqa: E402
from container_release_history import History, HistoryAdvanced, encode  # noqa: E402
from test_go_remediation import (  # noqa: E402
    AT,
    IMAGE,
    MODULE,
    built,
    copy_checkout,
    finding,
    provider_path,
    retained,
)
from test_go_remediation import checkout as checkout  # noqa: E402, F401
from test_go_remediation import provider as fixture_provider  # noqa: E402

pytestmark = pytest.mark.workflow
SOURCE = "c" * 40
BOT = "image-remediation[bot]"
URL = f"https://github.com/{REPOSITORY}/actions/runs/123/artifacts/456"


@pytest.mark.parametrize("kind", ["provider", "engine", "none"])
@pytest.mark.parametrize("actions", [False, True])
def test_cli_hands_the_acquired_plan_through_workers_and_assembly(
    checkout, tmp_path, monkeypatch, capsys, kind, actions
):
    work = tmp_path / "work"
    match = finding(provider_path(fixture_provider(checkout))) if kind == "provider" else finding()
    if kind == "none":
        match["vulnerability"]["fix"]["versions"] = []
    evidence = retained(work / "evidence", [match])
    plan = planner.plan(checkout, [evidence])
    write(work / "plan.json", plan)
    write(work / "receipt.json", {"source": SOURCE, "directories": [evidence.name]})
    output = tmp_path / "outputs"
    if actions:
        output.write_text("existing=kept\n")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    else:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(remediation, "ROOT", checkout)
    monkeypatch.setattr(remediation, "acquire", lambda *_: plan)
    monkeypatch.setattr(remediation, "current", lambda *_: None)
    monkeypatch.setattr(preparation, "rebuild", lambda item, path, tooling: built(item, path))

    def run(command, *extra):
        monkeypatch.setattr(sys, "argv", ["remediation", command, "--work", str(work), *extra])
        remediation.main()

    run("acquire")
    stdout = capsys.readouterr().out
    summary = read(work / "acquisition.json")
    assert stdout.splitlines()[0] == summary["message"]
    output_lines = output.read_text().splitlines() if actions else stdout.splitlines()[1:]
    outputs = dict(line.split("=", 1) for line in output_lines)
    assert outputs["status"] == summary["status"]
    if actions:
        assert outputs["existing"] == "kept"
    shards = json.loads(outputs["providers"])
    assert bool(shards) == (kind == "provider")
    assert outputs["updates"] == ("false" if kind == "none" else "true")
    results = tmp_path / "results"
    for shard in shards:
        run("provider", "--shard", shard, "--results", str(results))
    if kind != "none":
        run("prepare", "--results", str(results))
        assert remediation.proposal_files(checkout, work / "proposal")


@pytest.fixture
def proposal(checkout, tmp_path):
    work = tmp_path / "work"
    evidence = retained(work / "evidence")
    plan = planner.plan(checkout, [evidence])
    write(work / "receipt.json", {"source": SOURCE, "directories": [evidence.name]})
    write(work / "plan.json", plan)
    preparation.prepare(checkout, [evidence], plan, work / "proposal")
    return checkout, work


@pytest.mark.parametrize("fixture_name", ["proposal", "provider_proposal"])
def test_proposal_fixtures_survive_remediated_repository(
    tmp_path, monkeypatch, request, fixture_name
):
    upstream = copy_checkout(tmp_path / "upstream", ROOT)
    for name, document in planner.inputs(upstream)[0].items():
        components = (
            document["engines"].values() if name == planner.RECIPE else document["providers"]
        )
        for component in components:
            modules = component.get("source_build", {}).get("modules", {})
            for module, version in {
                MODULE: "v0.62.0",
                "github.com/klauspost/compress": "v1.20.2",
            }.items():
                if module in modules:
                    modules[module] = version
        write(upstream / name, document)
    monkeypatch.setattr("test_go_remediation.ROOT", upstream)
    root, work = request.getfixturevalue(fixture_name)
    assert read(work / "plan.json")["updates"]
    assert remediation.proposal_files(root, work / "proposal")


def test_proposal_allows_only_planned_overlay(proposal):
    root, work = proposal
    files = remediation.proposal_files(root, work / "proposal")
    assert set(files) == {planner.RECIPE}
    remediation.apply(root, work / "proposal")
    assert (
        read(root / planner.RECIPE)["engines"]["terraform"]["source_build"]["modules"][MODULE]
        == "v0.62.0"
    )


@pytest.fixture
def provider_proposal(checkout, tmp_path, monkeypatch):
    original = read(checkout / planner.CONTEXT / "dependencies.terraform.json")
    original["providers"][0]["source_build"]["modules"]["golang.org/x/crypto"] = "v0.57.0"
    for name in ("dependencies.terraform.json", "dependencies.terraform-avm.json"):
        document = read(checkout / planner.CONTEXT / name)
        for item in document["providers"]:
            if item["source"] == original["providers"][0]["source"]:
                item["source_build"]["modules"]["golang.org/x/crypto"] = "v0.57.0"
        write(checkout / planner.CONTEXT / name, document)
    write(checkout / planner.CONTEXT / "dependencies.shared.json", original)

    def build(item, work, tooling):
        item["source_build"]["modules"]["golang.org/x/crypto"] = "v0.58.0"
        return built(item, work)

    monkeypatch.setattr(preparation, "rebuild", build)
    work = tmp_path / "provider-work"
    evidence = retained(work / "evidence", [finding(provider_path(fixture_provider(checkout)))])
    plan = planner.plan(checkout, [evidence])
    write(work / "receipt.json", {"source": SOURCE, "directories": [evidence.name]})
    write(work / "plan.json", plan)
    preparation.prepare(checkout, [evidence], plan, work / "proposal")
    return checkout, work


def test_provider_packages_and_companions_are_applied_together(provider_proposal):
    root, work = provider_proposal
    metadata = read(work / "proposal/proposal.json")
    assert len(metadata["providers"]) == len(metadata["companionUpdates"]) == 3
    files = remediation.proposal_files(root, work / "proposal")
    assert set(files) == {item["path"] for item in metadata["providers"]}
    remediation.apply(root, work / "proposal")
    for item in metadata["providers"]:
        source = read(root / item["path"])["providers"][item["index"]]["source_build"]
        assert source["modules"][MODULE] == "v0.62.0"
        assert source["modules"]["golang.org/x/crypto"] == "v0.58.0"
        assert source["sha256"] == planner.digest(work / "proposal" / item["package"])


@pytest.mark.parametrize(
    "mutation",
    [
        "package",
        "missing",
        "extra",
        "duplicate",
        "malformed",
        "downgrade",
        "major",
        "before",
        "module",
        "owner",
    ],
)
def test_provider_proposal_rejects_invalid_packages_and_companions(provider_proposal, mutation):
    root, work = provider_proposal
    directory = work / "proposal"
    metadata = read(directory / "proposal.json")
    if mutation == "package":
        (directory / metadata["providers"][0]["package"]).write_bytes(b"corrupted archive")
    elif mutation == "missing":
        metadata["providers"].pop()
    elif mutation in {"extra", "duplicate"}:
        item = copy.deepcopy(metadata["providers"][0])
        if mutation == "extra":
            item["index"] = 999
        metadata["providers"].append(item)
    else:
        update = metadata["companionUpdates"][0]
        if mutation in {"malformed", "downgrade", "major"}:
            update["after"] = {"malformed": "latest", "downgrade": "v0.56.0", "major": "v1.0.0"}[
                mutation
            ]
            if mutation != "malformed":
                path = directory / "files" / update["path"]
                document = read(path)
                item = document["providers"][int(update["owner"])]
                source = item["source_build"]
                source["modules"][update["module"]] = update["after"]
                raw, module_files = built(item, directory)
                source.update(sha256=hashlib.sha256(raw).hexdigest(), module_files=module_files)
                package = f"providers/{source['sha256']}.zip"
                (directory / package).write_bytes(raw)
                write(path, document)
                metadata["files"][update["path"]] = planner.digest(path)
                for record in metadata["providers"]:
                    if (record["path"], record["index"]) == (update["path"], int(update["owner"])):
                        record["package"] = package
        elif mutation == "before":
            update["before"] = "v0.56.0"
        elif mutation == "module":
            update["module"] = "example.invalid/unplanned"
        else:
            update["owner"] = "999"
    write(directory / "proposal.json", metadata)
    before = {name: (root / name).read_bytes() for name in metadata["files"]}
    with pytest.raises(ValueError):
        remediation.apply(root, directory)
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("mutation", ["repository", "file", "companion", "hash", "empty", "plan"])
def test_proposal_refuses_unplanned_mutations(proposal, mutation):
    root, work = proposal
    directory = work / "proposal"
    recipe = directory / "files" / planner.RECIPE
    document = read(recipe)
    metadata = read(directory / "proposal.json")
    if mutation == "repository":
        document["engines"]["terraform"]["source_build"]["repository"] = (
            "https://example.invalid/source"
        )
        write(recipe, document)
        metadata["files"][planner.RECIPE] = planner.digest(recipe)
    elif mutation == "file":
        metadata["files"][".github/workflows/tests.yml"] = "a" * 64
    elif mutation == "companion":
        metadata["companionUpdates"] = [
            {
                "path": planner.RECIPE,
                "owner": "opentofu",
                "module": MODULE,
                "before": None,
                "after": "v0.62.0",
            }
        ]
    elif mutation == "hash":
        recipe.write_bytes(b"{}")
    elif mutation == "empty":
        metadata["files"] = {}
    else:
        document = read(directory / "plan.json")
        document["updates"][0]["after"] = "v0.63.0"
        write(directory / "plan.json", document)
    write(directory / "proposal.json", metadata)
    before = (root / planner.RECIPE).read_bytes()
    with pytest.raises(ValueError):
        remediation.proposal_files(root, directory)
    assert (root / planner.RECIPE).read_bytes() == before


def scan_artifact(
    root, work, results, profile, variant, *, target_present=False, provider_target=False
):
    original, hashes = planner.inputs(root)
    if variant == "candidate":
        original.update(
            {
                name: json.loads(raw)
                for name, raw in remediation.proposal_files(root, work / "proposal").items()
            }
        )
        hashes.update(read(work / "proposal/proposal.json")["files"])
    binary = (
        "/usr/local/bin/terraform" if profile.startswith("terraform") else "/usr/local/bin/tofu"
    )
    manifest = original[f"{planner.CONTEXT}/{remediation.PROVIDER_MANIFESTS[profile]}"]
    match = finding(
        guest_path=provider_path(manifest["providers"][0]) if provider_target else binary
    )
    artifacts = [copy.deepcopy(match["artifact"])]
    if variant == "candidate":
        artifacts[0]["version"] = "v0.62.0"
    engine = original[planner.RECIPE]["engines"][profile.split("-", 1)[0]]
    engine_module = (
        engine["source_build"]["repository"].removeprefix("https://").removesuffix(".git")
    )
    artifacts.extend(
        [
            {
                "id": "engine-main",
                "type": "go-module",
                "name": engine_module,
                "version": f"v{engine['version']}+dirty",
                "metadata": {"mainModule": engine_module, "goCompiledVersion": "go1.27.3"},
                "locations": [{"path": binary}],
            },
            {
                "id": "engine-stdlib",
                "type": "go-module",
                "name": "stdlib",
                "version": "go1.27.3",
                "locations": [{"path": binary}],
            },
        ]
    )
    for index, provider in enumerate(manifest["providers"]):
        name = provider["source"].rsplit("/", 1)[-1]
        location = provider_path(provider)
        module = f"github.com/example/terraform-provider-{name}"
        artifacts.extend(
            [
                {
                    "id": f"provider-{index}",
                    "type": "go-module",
                    "name": module,
                    "version": "v" + provider["version"],
                    "metadata": {"mainModule": module},
                    "locations": [{"path": location}],
                },
                {
                    "id": f"stdlib-{index}",
                    "type": "go-module",
                    "name": "stdlib",
                    "version": provider.get("source_build", {}).get("go_version", "go1.27.2"),
                    "locations": [{"path": location}],
                },
            ]
        )
        for number, (module, version) in enumerate(
            provider.get("source_build", {}).get("modules", {}).items()
        ):
            artifacts.append(
                {
                    "id": f"overlay-{index}-{number}",
                    "type": "go-module",
                    "name": module,
                    "version": version,
                    "locations": [{"path": location}],
                }
            )
    sbom = {"source": {"type": "image", "metadata": {"imageID": IMAGE}}, "artifacts": artifacts}
    report = read(retained(results, name=f"source-{variant}-{profile}") / "grype.json")
    report["matches"] = [match] if target_present else []
    directory = results / f"{variant}-{profile}"
    write(
        directory / "qualification.json",
        {
            "schemaVersion": 1,
            "source": SOURCE,
            "profile": profile,
            "variant": variant,
            "plan": planner.digest(work / "plan.json"),
            "inputs": hashes,
        },
    )
    write(
        directory / "build.json",
        {"local_image_id": IMAGE, "profile": profile, "source_commit": SOURCE},
    )
    write(
        directory / "runtime.json",
        {"passed": True, "profile": profile, "imageId": IMAGE, "containersRemaining": []},
    )
    write(directory / "sbom.syft.json", sbom)
    write(directory / "grype.json", report)
    write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))


@pytest.fixture
def qualified(proposal, tmp_path, monkeypatch):
    root, work = proposal
    results = tmp_path / "results"
    monkeypatch.setattr(remediation, "git", lambda *_: SOURCE)
    for variant in ("baseline", "candidate"):
        for profile in remediation.PROVIDER_MANIFESTS:
            scan_artifact(
                root, work, results, profile, variant, target_present=variant == "baseline"
            )
    return root, work, results


def test_complete_qualification_requires_targets_removed(qualified):
    root, work, results = qualified
    report = remediation.qualify(root, work, results)
    assert report["status"] == "ready"
    assert len(report["scans"]) == 8
    assert sum(row["targetedFindings"] for row in report["scans"]) == 4


@pytest.fixture
def provider_qualified(provider_proposal, tmp_path, monkeypatch):
    root, work = provider_proposal
    results = tmp_path / "provider-results"
    monkeypatch.setattr(remediation, "git", lambda *_: SOURCE)
    for variant in ("baseline", "candidate"):
        for profile in remediation.PROVIDER_MANIFESTS:
            scan_artifact(
                root,
                work,
                results,
                profile,
                variant,
                target_present=variant == "baseline",
                provider_target=True,
            )
    return root, work, results


@pytest.mark.parametrize("stale_module", [False, True])
def test_provider_proposal_qualifies_only_with_rebuilt_inventory(provider_qualified, stale_module):
    root, work, results = provider_qualified
    if stale_module:
        path = results / "candidate-terraform-random/sbom.syft.json"
        sbom = read(path)
        for item in sbom["artifacts"]:
            if item["name"] == MODULE:
                item["version"] = "v0.60.0"
        write(path, sbom)
        with pytest.raises(ValueError, match="module"):
            remediation.qualify(root, work, results)
    else:
        assert remediation.qualify(root, work, results)["status"] == "ready"


@pytest.mark.parametrize("variant", ["baseline", "candidate"])
@pytest.mark.parametrize("profile", ["terraform-random", "opentofu-avm"])
@pytest.mark.parametrize(
    "mutation", ["absent", "main", "stdlib", "path", "compiler", "module", "version"]
)
def test_provider_only_qualification_requires_engine_inventory(
    provider_qualified, variant, profile, mutation
):
    root, work, results = provider_qualified
    directory = results / f"{variant}-{profile}"
    sbom = read(directory / "sbom.syft.json")
    engine = [item for item in sbom["artifacts"] if item["id"].startswith("engine-")]
    if mutation in {"absent", "main", "stdlib"}:
        sbom["artifacts"] = [
            item
            for item in sbom["artifacts"]
            if not (item in engine and (mutation == "absent" or item["id"] == f"engine-{mutation}"))
        ]
    elif mutation == "path":
        for item in engine:
            item["locations"] = [{"path": "/usr/local/bin/other"}]
    elif mutation == "compiler":
        engine[1]["version"] = "go1.0.0"
    elif mutation == "module":
        engine[0]["metadata"]["mainModule"] = "example.invalid/engine"
    else:
        engine[0]["version"] = "v0.0.0"
    write(directory / "sbom.syft.json", sbom)
    write(
        directory / "assessment.json",
        scan_result(read(directory / "grype.json"), IMAGE, AT, sbom=sbom),
    )
    with pytest.raises(ValueError, match="Engine"):
        remediation.qualify(root, work, results)


def test_source_already_fixed_is_noop(qualified):
    root, work, results = qualified
    for profile in remediation.PROVIDER_MANIFESTS:
        scan_artifact(root, work, results, profile, "baseline")
    assert remediation.qualify(root, work, results)["status"] == "source-already-fixed"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "source",
        "image",
        "runtime",
        "assessment",
        "module",
        "target",
        "ignored",
        "unrelated",
    ],
)
def test_qualification_rejects_incomplete_or_contradictory_evidence(qualified, mutation):
    root, work, results = qualified
    directory = results / "candidate-terraform-random"
    if mutation == "missing":
        (directory / "runtime.json").unlink()
    else:
        filename = {
            "source": "qualification.json",
            "image": "runtime.json",
            "runtime": "runtime.json",
            "assessment": "assessment.json",
            "module": "sbom.syft.json",
            "target": "grype.json",
            "ignored": "grype.json",
            "unrelated": "grype.json",
        }[mutation]
        document = read(directory / filename)
        if mutation == "source":
            document["source"] = "d" * 40
        elif mutation == "image":
            document["imageId"] = "sha256:" + "d" * 64
        elif mutation == "runtime":
            document["containersRemaining"] = ["leaked-container"]
        elif mutation == "assessment":
            document["outcome"] = "vulnerable"
        elif mutation == "module":
            document["artifacts"][0]["version"] = "v0.61.0"
        else:
            item = finding(installed="v0.62.0")
            item["vulnerability"]["severity"] = "Low"
            if mutation == "unrelated":
                item["vulnerability"]["id"] = "GO-OTHER"
            document["ignoredMatches" if mutation == "ignored" else "matches"] = [item]
        write(directory / filename, document)
    if mutation == "unrelated":
        assert remediation.qualify(root, work, results)["status"] == "ready"
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            remediation.qualify(root, work, results)


@pytest.fixture
def current_state(monkeypatch):
    monitor = {"id": 123, "run_attempt": 1, "status": "completed", "conclusion": "success"}
    receipt = {
        "source": SOURCE,
        "catalogue": {"sequence": 1},
        "monitor": monitor,
        "acquiredAt": AT,
        "oldestAssessment": AT,
    }
    monkeypatch.setattr(acquisition, "now", lambda: AT)
    monkeypatch.setattr(acquisition, "main_head", lambda _: SOURCE)
    monkeypatch.setattr(
        acquisition,
        "History",
        lambda _: SimpleNamespace(head=lambda: SimpleNamespace(reference=receipt["catalogue"])),
    )
    monkeypatch.setattr(acquisition, "latest_monitor", lambda _: monitor)
    return receipt


@pytest.mark.parametrize("change", ["none", "main", "catalogue", "monitor", "age"])
def test_latest_state_is_rechecked(current_state, monkeypatch, change):
    receipt = copy.deepcopy(current_state)
    if change == "main":
        monkeypatch.setattr(acquisition, "main_head", lambda _: "d" * 40)
    elif change == "catalogue":
        receipt["catalogue"] = {"sequence": 2}
    elif change == "monitor":
        receipt["monitor"]["run_attempt"] = 2
    elif change == "age":
        receipt["acquiredAt"] = "2026-11-01T12:00:00Z"
    if change == "none":
        acquisition.current(acquisition.GitHub(), receipt)
    else:
        with pytest.raises(ValueError):
            acquisition.current(acquisition.GitHub(), receipt)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "feature"),
        ("path", ".github/workflows/other.yml"),
        ("run_attempt", 2),
        ("event", "pull_request"),
        ("status", "in_progress"),
    ],
)
def test_monitor_requires_trusted_exact_attempt(current_state, field, value):
    monitor = current_state["monitor"]
    run = {
        **monitor,
        "head_branch": "main",
        "path": ".github/workflows/container-image-monitor.yml",
        "head_repository": {"full_name": REPOSITORY},
        "event": "schedule",
        "head_sha": SOURCE,
    }
    client: Any = SimpleNamespace(request=lambda _: encode(run))
    assert acquisition.monitor_source(client, monitor) == SOURCE
    run[field] = value
    with pytest.raises(ValueError):
        acquisition.monitor_source(client, monitor)


@pytest.mark.parametrize("require_all", [False, True])
@pytest.mark.parametrize("profiles", [["bicep-typo"], ["bicep", "bicep-typo"]])
def test_acquisition_rejects_unknown_profiles_before_retrieving_evidence(
    tmp_path, monkeypatch, require_all, profiles
):
    monkeypatch.setattr(acquisition, "git", lambda *_: pytest.fail("source lookup reached"))
    destination = tmp_path / "acquired"
    with pytest.raises(ValueError, match="Unknown image profiles: bicep-typo"):
        acquisition.acquire(
            acquisition.GitHub(),
            tmp_path,
            destination,
            selected_profiles=iter(profiles),
            require_all_profiles=require_all,
        )
    assert not destination.exists()


def test_failed_monitor_requires_verified_delivery(current_state):
    monitor = {**current_state["monitor"], "conclusion": "failure"}
    with pytest.raises(ValueError, match="verified delivery"):
        acquisition.monitor_source(acquisition.GitHub(), monitor)


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending", "requested", "waiting"])
def test_unfinished_monitor_defers_acquisition(current_state, status):
    monitor = {**current_state["monitor"], "status": status, "conclusion": None}
    with pytest.raises(acquisition.EvidenceChanging, match="running or queued"):
        acquisition.monitor_source(acquisition.GitHub(), monitor)


@pytest.mark.parametrize("change", ["catalogue", "monitor"])
def test_changed_snapshot_is_retryable_but_cannot_pass_freshness(current_state, change):
    receipt = copy.deepcopy(current_state)
    receipt[change] = {}
    with pytest.raises(acquisition.EvidenceChanging):
        acquisition.current(acquisition.GitHub(), receipt)


@pytest.mark.parametrize("recover", [True, False])
def test_acquisition_retry_discards_partial_inputs(tmp_path, monkeypatch, recover):
    work = tmp_path / "work"
    attempts = []
    sleeps = []

    def acquire(client, root, directory):
        attempts.append(directory)
        assert not directory.exists()
        if len(attempts) < 3 or not recover:
            write(directory / "evidence/partial.json", {"stale": True})
            raise acquisition.EvidenceChanging("Release history changed during remediation")
        write(directory / "receipt.json", {"source": SOURCE})
        write(directory / "plan.json", {"updates": []})
        write(directory / "evidence/current.json", {"stale": False})
        return {"updates": []}

    monkeypatch.setattr(acquisition, "acquire", acquire)
    monkeypatch.setattr(acquisition.time, "sleep", sleeps.append)
    result = acquisition.acquire_with_retry(acquisition.GitHub(), tmp_path, work)
    assert sleeps == [30, 30] and len(set(attempts)) == 3
    assert not (work / "evidence/partial.json").exists()
    if recover:
        assert result == {"updates": []}
        assert read(work / "receipt.json")["source"] == SOURCE
        assert read(work / "evidence/current.json") == {"stale": False}
    else:
        assert result is None
        assert not (work / "receipt.json").exists()
        assert not (work / "plan.json").exists()
    assert read(work / "attempts/1/deferred.json")["attempt"] == 1


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Invalid provenance"),
        ValueError("No verified delivery"),
        ValueError("Security history changed during retrieval"),
        OSError("API failed"),
    ],
)
def test_acquisition_does_not_retry_invalid_or_unavailable_evidence(tmp_path, monkeypatch, error):
    calls = []

    def fail(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(acquisition, "acquire", fail)
    monkeypatch.setattr(acquisition.time, "sleep", lambda _: pytest.fail("Unexpected retry"))
    with pytest.raises(type(error), match=str(error)):
        acquisition.acquire_with_retry(acquisition.GitHub(), tmp_path, tmp_path / "work")
    assert len(calls) == 1


@pytest.mark.parametrize("actions", [False, True])
def test_deferred_cli_emits_no_build_or_source_outputs(tmp_path, monkeypatch, capsys, actions):
    work, output, summary = (tmp_path / name for name in ("work", "output", "summary"))
    monkeypatch.setattr(remediation, "acquire", lambda *_: None)
    if actions:
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    else:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["remediation", "acquire", "--work", str(work)])
    remediation.main()
    stdout = capsys.readouterr().out.splitlines()
    values = output.read_text().splitlines() if actions else stdout[1:]
    assert values == ["status=deferred", "updates=false", "providers=[]"]
    assert read(work / "acquisition.json")["status"] == "deferred"
    assert "retries automatically" in summary.read_text()


@pytest.mark.parametrize("sufficient", [True, False])
def test_no_update_summary_preserves_publication_and_residual_findings(
    tmp_path, monkeypatch, sufficient
):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    result = {
        "updates": [],
        "findings": [{"decisions": [{"status": "unsupported-component"}]}],
    }
    if sufficient:
        result["findings"].append({"decisions": [{"status": "override-already-sufficient"}]})
    before = copy.deepcopy(result)
    status = remediation.acquisition_summary(tmp_path, result)
    assert status == ("source-already-fixed" if sufficient else "no-supported-updates")
    assert "unsupported components" in summary.read_text()
    if sufficient:
        assert "publication and fresh monitoring remain pending" in summary.read_text()
    assert result == before


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "unpublished",
        "missing",
        "race",
        "stale",
        "digest",
        "unavailable",
        "observation",
        "signature",
        "history-acquire",
        "history-current",
        "history-acquire-busy",
        "history-current-busy",
    ],
)
def test_acquisition_binds_release_catalogue_and_monitor(
    checkout, tmp_path, current_state, monkeypatch, mutation
):
    seed = retained(tmp_path, name="seed")
    observation = read(seed / "observation.json")
    observation["assessment"].update(monitorRunId="123", monitorRunAttempt=1)
    write(seed / "observation.json", observation)
    candidate = observation["candidate"]
    attempt = {**observation["assessment"], "evidenceRelease": "image-monitor-123-1"}
    record = {**candidate, "candidate": candidate, "latestAttempt": attempt}
    if mutation == "stale":
        attempt["assessedAt"] = "2026-11-01T12:00:00Z"
    elif mutation == "digest":
        attempt["digest"] = "sha256:" + "0" * 64
    elif mutation == "unavailable":
        attempt["outcome"] = "unavailable"
    elif mutation == "observation":
        observation["candidate"]["sourceCommit"] = "d" * 40
        write(seed / "observation.json", observation)
        record["candidate"] = {**candidate, "sourceCommit": SOURCE}
    name = "terraform-random-0.2.3"
    catalogue = {"current": {"terraform-random": name}, "releases": {name: record}}
    if mutation == "missing":
        catalogue["current"] = {}
    snapshot = SimpleNamespace(reference=current_state["catalogue"], catalogue=catalogue)
    snapshots = []
    sleeps = []

    def head():
        snapshots.append(None)
        call = len(snapshots)
        if (
            mutation == "history-acquire"
            and call <= 3
            or mutation == "history-current"
            and 2 <= call <= 4
            or mutation == "history-acquire-busy"
            or mutation == "history-current-busy"
            and call % 4 != 1
        ):
            raise HistoryAdvanced("Security history advanced during retrieval")
        if mutation == "race" and len(snapshots) > 1:
            return SimpleNamespace(reference={"sequence": 2}, catalogue=catalogue)
        return snapshot

    monkeypatch.setattr(acquisition, "History", lambda _: SimpleNamespace(head=head))
    if mutation.startswith("history-"):
        monkeypatch.setattr(acquisition, "History", History)
        monkeypatch.setattr(History, "_head_once", lambda *_: head())
    monkeypatch.setattr(acquisition.time, "sleep", sleeps.append)
    monkeypatch.setattr(acquisition, "PROVIDER_MANIFESTS", {"terraform-random": "unused"})
    monkeypatch.setattr(acquisition, "git", lambda *_: SOURCE)
    monkeypatch.setattr(acquisition, "monitor_source", lambda *_: SOURCE)
    verified = []

    def authenticate(*_):
        if mutation == "signature":
            raise ValueError("Invalid provenance")
        verified.append(name)

    monkeypatch.setattr(acquisition, "release_evidence", authenticate)
    monkeypatch.setattr(
        acquisition,
        "restore_monitor",
        lambda client, monitor, source, targets, directory: shutil.copytree(seed, directory / name),
    )
    if mutation == "unpublished":
        result = acquisition.acquire(
            acquisition.GitHub(),
            checkout,
            tmp_path / "acquired",
            selected_profiles=["terraform-random", "bicep"] if mutation == "unpublished" else None,
            require_all_profiles=mutation != "unpublished",
        )
        assert result["updates"] and verified == [name]
        assert read(tmp_path / "acquired/receipt.json")["oldestAssessment"] == AT
        if mutation == "unpublished":
            assert read(tmp_path / "acquired/receipt.json")["unpublishedProfiles"] == ["bicep"]
    elif mutation.endswith("-busy"):
        work = tmp_path / "acquired"
        assert acquisition.acquire_with_retry(acquisition.GitHub(), checkout, work) is None
        assert sleeps == [1, 2, 30, 1, 2, 30, 1, 2]
        assert len(snapshots) == (9 if mutation == "history-acquire-busy" else 12)
        assert verified == ([] if mutation == "history-acquire-busy" else [name] * 3)
        assert not (work / "plan.json").exists()
        assert not (work / "receipt.json").exists()
        for attempt_number in range(1, 4):
            assert (
                "history advanced"
                in read(work / "attempts" / str(attempt_number) / "deferred.json")["reason"]
            )
    elif mutation in {"none", "race", "history-acquire", "history-current"}:
        result = acquisition.acquire_with_retry(
            acquisition.GitHub(), checkout, tmp_path / "acquired"
        )
        assert result is not None and result["updates"]
        assert verified == [name] * (2 if mutation in {"race", "history-current"} else 1)
        if mutation.startswith("history-"):
            assert sleeps == [1, 2, 30]
            assert len(snapshots) == (5 if mutation == "history-acquire" else 6)
        assert read(tmp_path / "acquired/receipt.json")["oldestAssessment"] == AT
        assert read(tmp_path / "acquired/receipt.json")["catalogue"]["sequence"] == (
            2 if mutation == "race" else 1
        )
    else:
        with pytest.raises(ValueError) as error:
            acquisition.acquire(acquisition.GitHub(), checkout, tmp_path / "acquired")
        if mutation == "missing":
            assert "current image release is missing: terraform-random" in str(error.value)
        assert not (tmp_path / "acquired/plan.json").exists()


@pytest.mark.parametrize(
    "mutation",
    ["none", "digest", "duplicate", "missing", "mutable", "producer", "traversal", "absolute"],
)
def test_retained_monitor_assets_are_authenticated(tmp_path, monkeypatch, mutation):
    target = "terraform-random-0.2.3"
    if mutation == "traversal":
        target = "../escaped"
    elif mutation == "absolute":
        target = (tmp_path / "escaped").as_posix()
    content = {f"{target}/{name}": b"{}\n" for name in acquisition.FILES}
    index = {
        "runId": "123",
        "runAttempt": 1,
        "files": {
            name: "sha256:" + hashlib.sha256(raw).hexdigest() for name, raw in content.items()
        },
    }
    if mutation == "digest":
        index["files"][next(iter(content))] = "sha256:" + "0" * 64
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for number, (name, raw) in enumerate(content.items()):
            if mutation != "missing" or number:
                bundle.writestr(name, raw)
        if mutation == "duplicate":
            with pytest.warns(UserWarning):
                bundle.writestr(next(iter(content)), b"{}\n")
    payloads = {"monitor-index.json": encode(index), "reports-0000.zip": archive.read_bytes()}
    assets = [
        {"name": name, "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        for name, raw in payloads.items()
    ]
    release = {"id": 1, "draft": False, "immutable": mutation != "mutable"}
    monkeypatch.setattr(
        acquisition,
        "Evidence",
        lambda _: SimpleNamespace(
            find=lambda _: release,
            download=lambda asset, path, _: path.write_bytes(payloads[asset["name"]]),
        ),
    )
    client: Any = SimpleNamespace(
        assets=lambda _: assets, tag_commit=lambda _: SOURCE if mutation != "producer" else "d" * 40
    )
    if mutation == "none":
        acquisition.restore_monitor(
            client, {"id": 123, "run_attempt": 1}, SOURCE, {target}, tmp_path / "restored"
        )
        assert len(list((tmp_path / "restored" / target).iterdir())) == 4
    else:
        with pytest.raises(ValueError):
            acquisition.restore_monitor(
                client, {"id": 123, "run_attempt": 1}, SOURCE, {target}, tmp_path / "restored"
            )
        assert not (tmp_path / "escaped").exists()


class FakeGitHub(publication.GitHub):
    def __init__(self):
        self.head = None
        self.pulls = []
        self.blobs = {}
        self.trees = {}
        self.commits = {SOURCE: {"tree": {"sha": "base-tree"}, "committer": {"date": AT}}}
        self.fail_after = None
        self.fail_before = None
        self.body_edit_after = None
        self.head_edit_after_tree = None
        self.race = False
        self.writes = []

    def pages(self, endpoint):
        if "/pulls?" in endpoint:
            return copy.deepcopy(
                [
                    pull
                    for pull in self.pulls
                    if "&base=main" not in endpoint or pull["base"]["ref"] == "main"
                ]
            )
        return (
            []
            if self.head is None
            else [{"ref": f"refs/heads/{publication.BRANCH}", "object": {"sha": self.head}}]
        )

    def request(self, endpoint, *, method="GET", payload=None, **kwargs):
        path = endpoint.removeprefix(f"repos/{REPOSITORY}/")
        data = json.loads(payload) if payload is not None else {}
        result = {}
        if path == self.fail_before:
            self.fail_before = None
            raise OSError("Request failed before mutation")
        if method != "GET":
            self.writes.append((path, data))
        if path == f"repos/{REPOSITORY}":
            result = {"full_name": REPOSITORY, "node_id": "repository-id"}
        elif path == f"users/{BOT}":
            result = {"id": 42}
        elif path.startswith("git/commits/"):
            result = self.commits[path.split("/")[-1]]
        elif path.startswith("commits/"):
            result = {"author": {"login": BOT}}
        elif path.startswith("compare/"):
            sha = path.rsplit("...", 1)[1]
            entries = self.trees[self.commits[sha]["tree"]["sha"]]
            result = {
                "files": [
                    {"filename": item["path"], "sha": item["sha"], "status": "modified"}
                    for item in entries
                ]
            }
        elif path.startswith("git/blobs/"):
            raw = self.blobs[path.split("/")[-1]]
            result = {
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
                "size": len(raw),
            }
        elif path == "git/blobs":
            raw = base64.b64decode(data["content"])
            sha = hashlib.sha256(raw).hexdigest()[:40]
            self.blobs[sha] = raw
            result = {"sha": sha}
        elif path == "git/trees":
            sha = hashlib.sha256(encode(data)).hexdigest()[:40]
            self.trees[sha] = data["tree"]
            result = {"sha": sha}
            if self.head_edit_after_tree:
                self.head = {"human": "human", "deleted": None, "base": SOURCE}[
                    self.head_edit_after_tree
                ]
                self.head_edit_after_tree = None
        elif path == "git/commits":
            sha = hashlib.sha256(encode(data)).hexdigest()[:40]
            self.commits[sha] = {**data, "tree": {"sha": data["tree"]}}
            result = {"sha": sha}
        elif path == "graphql":
            update = data["variables"]["input"]["refUpdates"][0]
            assert update["force"] is False
            if self.race:
                self.head = SOURCE if self.race == "base" else "human"
            if (self.head or "0" * 40) != update["beforeOid"]:
                raise ValueError("Head changed")
            if (
                self.head is not None
                and self.head not in self.commits[update["afterOid"]]["parents"]
            ):
                raise ValueError("Not a fast forward")
            self.head = update["afterOid"]
            result = {"data": {"updateRefs": {"clientMutationId": self.head}}}
        elif path == "pulls":
            result = {
                **data,
                "number": len(self.pulls) + 1,
                "state": "open",
                "user": {"login": BOT},
                "html_url": f"https://github.com/{REPOSITORY}/pull/1",
                "merged_at": None,
                "head": {"ref": publication.BRANCH, "repo": {"full_name": REPOSITORY}},
                "base": {"ref": "main"},
            }
            self.pulls.append(result)
        elif path.startswith("pulls/"):
            result = self.pulls[int(path.split("/")[-1]) - 1]
            result.update(data)
        else:
            raise AssertionError((path, method))
        if method != "GET" and path == self.body_edit_after:
            self.body_edit_after = None
            self.pulls[0]["body"] += "\nMaintainer note: preserve this.\n"
        if method != "GET" and path == self.fail_after:
            self.fail_after = None
            raise OSError("Response lost after mutation")
        return encode(result)


@pytest.fixture
def publisher(monkeypatch):
    client = FakeGitHub()
    monkeypatch.setattr(publication, "current", lambda *_: None)
    files = {planner.RECIPE: b"proposal"}
    report = {
        "status": "ready",
        "source": SOURCE,
        "scans": [],
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }

    def publish():
        return publication.publish(client, {"source": report["source"]}, report, files, BOT, URL)

    return client, files, report, publish


def test_pr_create_retry_and_refresh_are_idempotent(publisher):
    client, files, report, publish = publisher
    assert publish()["status"] == "created"
    assert f"[Retained qualification]({URL})" in client.pulls[0]["body"]
    first = client.head
    assert publish()["status"] == "unchanged"
    assert client.head == first and len(client.pulls) == 1
    files[planner.RECIPE] = b"new proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    assert publish()["status"] == "updated"
    assert first in client.commits[client.head]["parents"]
    assert len(client.pulls) == 1


@pytest.mark.parametrize("change", ["human", "deleted", "base"])
@pytest.mark.parametrize("cycle", ["unchanged", "refresh", "merged"])
def test_pr_writes_recheck_live_branch_after_tree_creation(publisher, change, cycle):
    client, files, report, publish = publisher
    publish()
    body = client.pulls[0]["body"]
    if cycle != "unchanged":
        files[planner.RECIPE] = b"later fix"
        report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    if cycle == "merged":
        client.pulls[0].update(state="closed", merged_at=AT)
    client.head_edit_after_tree = change
    client.writes.clear()
    with pytest.raises(ValueError, match="branch changed"):
        publish()
    assert client.head == {"human": "human", "deleted": None, "base": SOURCE}[change]
    assert client.pulls[0]["body"] == body
    assert not any(path.startswith("pulls") or path == "graphql" for path, _ in client.writes)


@pytest.mark.parametrize("existing", [False, True])
def test_final_pr_write_rechecks_head_after_freshness(publisher, monkeypatch, existing):
    client, files, report, publish = publisher
    if existing:
        publish()
        files[planner.RECIPE] = b"later fix"
        report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    calls = []

    def freshness(*_):
        calls.append(True)
        if len(calls) == 3:
            client.head = "human"

    monkeypatch.setattr(publication, "current", freshness)
    client.writes.clear()
    with pytest.raises(ValueError, match="branch changed"):
        publish()
    assert client.head == "human"
    assert len(client.pulls) == int(existing)
    assert sum(path.startswith("pulls") for path, _ in client.writes) == int(existing)


def test_unchanged_refresh_rechecks_evidence_freshness(publisher, monkeypatch):
    client, _, _, publish = publisher
    publish()
    body = client.pulls[0]["body"]
    calls = []

    def freshness(*_):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("Evidence changed")

    monkeypatch.setattr(publication, "current", freshness)
    client.writes.clear()
    with pytest.raises(ValueError, match="Evidence changed"):
        publish()
    assert client.pulls[0]["body"] == body
    assert not any(path.startswith("pulls") for path, _ in client.writes)


@pytest.mark.parametrize("operation", ["graphql", "pulls"])
def test_initial_uncertain_write_is_reconciled(publisher, operation):
    client, _, _, publish = publisher
    client.fail_after = operation
    with pytest.raises(OSError):
        publish()
    assert publish()["status"] in {"created", "unchanged"}
    assert len(client.pulls) == 1


@pytest.mark.parametrize("operation", ["pulls/1", "graphql"])
def test_refresh_uncertain_write_is_reconciled(publisher, operation):
    client, files, report, publish = publisher
    publish()
    files[planner.RECIPE] = b"new proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    client.fail_after = operation
    with pytest.raises(OSError):
        publish()
    assert publish()["status"] in {"updated", "unchanged"}
    assert len(client.pulls) == 1


@pytest.mark.parametrize("operation", ["pending", "branch", "final"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_interrupted_refresh_displays_only_completed_qualification(
    publisher, monkeypatch, operation, timing
):
    client, files, report, publish = publisher
    report["scans"] = [
        {
            "variant": "candidate",
            "profile": "terraform-random",
            "imageId": "sha256:old",
            "assessment": {"outcome": "clean"},
        }
    ]
    publish()
    old_head = client.head
    old_body = client.pulls[0]["body"]
    old_current = publication.marker(old_body)["current"]
    files[planner.RECIPE] = b"new proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    report["scans"][0]["imageId"] = "sha256:new"
    new_url = URL + "1"
    original = client.request
    patches = 0
    armed = True

    def interrupted(endpoint, *, method="GET", **kwargs):
        nonlocal patches, armed
        step = "branch" if endpoint == "graphql" else None
        if method == "PATCH":
            patches += 1
            step = "pending" if patches == 1 else "final"
        fail = armed and step == operation
        if fail:
            armed = False
        if fail and timing == "before":
            raise OSError("Write refused")
        result = original(endpoint, method=method, **kwargs)
        if fail:
            raise OSError("Write response lost")
        return result

    monkeypatch.setattr(client, "request", interrupted)

    def refresh():
        return publication.publish(client, {"source": SOURCE}, report, files, BOT, new_url)

    with pytest.raises(OSError):
        refresh()
    text = client.pulls[0]["body"]
    completed = operation == "final" and timing == "after"
    if completed:
        assert "sha256:new" in text and new_url in text
        assert f"Qualified commit: `{client.head}`" in text
        assert not text.startswith("## Refresh incomplete")
    else:
        assert "sha256:old" in text and f"]({URL})" in text
        assert "sha256:new" not in text and new_url not in text
        assert publication.marker(text)["current"] == old_current
        if operation == "pending" and timing == "before":
            assert text == old_body
        else:
            assert text.startswith("## Refresh incomplete")
            assert "does not qualify the current branch" in text
            assert f"Qualified commit: `{old_head}`" in text
    if operation == "pending" or (operation == "branch" and timing == "before"):
        assert client.head == old_head
    else:
        assert client.head != old_head
    assert refresh()["status"] in {"updated", "unchanged"}
    text = client.pulls[0]["body"]
    assert "sha256:new" in text and new_url in text and "sha256:old" not in text
    assert f"Qualified commit: `{client.head}`" in text
    assert not text.startswith("## Refresh incomplete")
    assert publication.marker(text)["pending"] is None


def test_new_candidate_retry_preserves_last_completed_qualification(publisher):
    client, files, report, publish = publisher
    publish()
    original = client.pulls[0]["body"]
    for proposal, failure in [(b"first", "after"), (b"second", "before")]:
        before = client.head
        files[planner.RECIPE] = proposal
        report["files"][planner.RECIPE] = hashlib.sha256(proposal).hexdigest()
        setattr(client, f"fail_{failure}", "graphql")
        with pytest.raises(OSError):
            publish()
        text = client.pulls[0]["body"]
        assert text.count("## Refresh incomplete") == 1
        assert publication.marker(text)["current"]["head"] == before
        assert (
            text.split("## Summary", 1)[1].split("<!--")[0]
            == original.split("## Summary", 1)[1].split("<!--")[0]
        )
    assert publish()["status"] == "updated"
    assert "## Refresh incomplete" not in client.pulls[0]["body"]
    assert publication.marker(client.pulls[0]["body"])["current"]["head"] == client.head


@pytest.mark.parametrize("closed", [False, True])
def test_retargeted_pr_cannot_be_hidden_from_ownership_discovery(publisher, closed):
    client, _, _, publish = publisher
    publish()
    client.pulls[0]["base"]["ref"] = "other"
    if closed:
        client.pulls[0]["state"] = "closed"
    client.writes.clear()
    with pytest.raises(ValueError, match="owned"):
        publish()
    assert client.writes == [] and len(client.pulls) == 1


@pytest.mark.parametrize("change", ["closed", "merged", "base", "author", "head"])
@pytest.mark.parametrize("after", ["git/trees", "pulls/1", "graphql"])
def test_pr_disposition_changes_stop_later_writes(publisher, monkeypatch, change, after):
    client, files, report, publish = publisher
    publish()
    files[planner.RECIPE] = b"later proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    original = client.request
    changed = False

    def changing(endpoint, *, method="GET", **kwargs):
        nonlocal changed
        path = endpoint.removeprefix(f"repos/{REPOSITORY}/")
        if changed and method != "GET" and (path.startswith("pulls") or path == "graphql"):
            pytest.fail(f"Wrote {path} after an observed PR change")
        result = original(endpoint, method=method, **kwargs)
        if path == after and method != "GET":
            changed = True
            if change in {"closed", "merged"}:
                client.pulls[0].update(state="closed", merged_at=AT if change == "merged" else None)
            elif change == "base":
                client.pulls[0]["base"]["ref"] = "other"
            elif change == "author":
                client.pulls[0]["user"]["login"] = "human"
            else:
                client.pulls[0]["head"]["ref"] = "other"
        return result

    monkeypatch.setattr(client, "request", changing)
    with pytest.raises(ValueError, match="state changed|ownership changed"):
        publish()
    assert changed


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("after", ["git/trees", "graphql"])
def test_new_pr_during_publication_stops_duplicate_creation(publisher, monkeypatch, merged, after):
    client, files, report, publish = publisher
    if merged:
        publish()
        client.pulls[0].update(state="closed", merged_at=AT)
        files[planner.RECIPE] = b"later proposal"
        report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    original = client.request
    changed = False

    def competing(endpoint, *, method="GET", **kwargs):
        nonlocal changed
        path = endpoint.removeprefix(f"repos/{REPOSITORY}/")
        if changed and method != "GET" and (path.startswith("pulls") or path == "graphql"):
            pytest.fail(f"Wrote {path} after a competing PR appeared")
        result = original(endpoint, method=method, **kwargs)
        if path == after and method != "GET":
            changed = True
            client.pulls.append({"number": len(client.pulls) + 1, "base": {"ref": "other"}})
        return result

    monkeypatch.setattr(client, "request", competing)
    with pytest.raises(ValueError, match="ownership changed"):
        publish()
    assert changed and len(client.pulls) == 1 + int(merged)


@pytest.mark.parametrize("change", ["reopened", "base"])
@pytest.mark.parametrize("after", ["git/trees", "pulls/1", "graphql"])
def test_merged_pr_changes_stop_next_cycle(publisher, monkeypatch, change, after):
    client, files, report, publish = publisher
    publish()
    client.pulls[0].update(state="closed", merged_at=AT)
    files[planner.RECIPE] = b"later proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    original = client.request
    changed = False

    def changing(endpoint, *, method="GET", **kwargs):
        nonlocal changed
        path = endpoint.removeprefix(f"repos/{REPOSITORY}/")
        if changed and method != "GET" and (path.startswith("pulls") or path == "graphql"):
            pytest.fail(f"Wrote {path} after a merged PR changed")
        result = original(endpoint, method=method, **kwargs)
        if path == after and method != "GET":
            changed = True
            if change == "reopened":
                client.pulls[0].update(state="open", merged_at=None)
            else:
                client.pulls[0]["base"]["ref"] = "other"
        return result

    monkeypatch.setattr(client, "request", changing)
    with pytest.raises(ValueError, match="state changed|ownership changed"):
        publish()
    assert changed and len(client.pulls) == 1


@pytest.fixture
def publisher_with_history(publisher):
    client, files, report, publish = publisher
    publish()
    client.pulls[0].update(state="closed", merged_at=AT)
    files[planner.RECIPE] = b"next cycle"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    publish()
    return publisher


@pytest.mark.parametrize("latest", ["open", "merged", "dismissed"])
@pytest.mark.parametrize("base", ["main", "other"])
def test_older_open_pr_stops_managed_branch_writes(publisher_with_history, latest, base):
    client, _, _, publish = publisher_with_history
    client.pulls[0].update(state="open", merged_at=None)
    client.pulls[0]["base"]["ref"] = base
    if latest != "open":
        client.pulls[1].update(state="closed", merged_at=AT if latest == "merged" else None)
    client.writes.clear()
    with pytest.raises(ValueError, match="Older open"):
        publish()
    assert client.writes == [] and len(client.pulls) == 2


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("after", ["git/trees", "pulls/2", "graphql"])
def test_reopened_older_pr_stops_refresh(publisher_with_history, monkeypatch, merged, after):
    client, files, report, publish = publisher_with_history
    if merged:
        client.pulls[1].update(state="closed", merged_at=AT)
    files[planner.RECIPE] = b"later proposal"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    original = client.request
    changed = False

    def reopening(endpoint, *, method="GET", **kwargs):
        nonlocal changed
        path = endpoint.removeprefix(f"repos/{REPOSITORY}/")
        if changed and method != "GET" and (path.startswith("pulls") or path == "graphql"):
            pytest.fail(f"Wrote {path} after an older PR was reopened")
        result = original(endpoint, method=method, **kwargs)
        if path == after and method != "GET":
            changed = True
            client.pulls[0].update(state="open", merged_at=None)
        return result

    monkeypatch.setattr(client, "request", reopening)
    with pytest.raises(ValueError, match="Older open"):
        publish()
    assert changed and len(client.pulls) == 2


@pytest.mark.parametrize("change", ["head", "author", "marker", "closed", "race", "race-base"])
def test_human_changes_are_preserved(publisher, change):
    client, files, report, publish = publisher
    publish()
    if change == "head":
        client.head = "human"
    elif change == "author":
        client.pulls[0]["user"]["login"] = "human"
    elif change == "marker":
        client.pulls[0]["body"] = "human text"
    elif change == "closed":
        client.pulls[0]["state"] = "closed"
    else:
        client.race = "base" if change == "race-base" else True
        files[planner.RECIPE] = b"new proposal"
        report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    if change == "closed":
        before = len(client.writes)
        assert publish()["status"] == "dismissed"
        assert len(client.writes) == before
    else:
        with pytest.raises(ValueError):
            publish()
        if change in {"head", "race"}:
            assert client.head == "human"
        if change == "race-base":
            assert client.head == SOURCE


@pytest.mark.parametrize("cycle", ["unchanged", "refresh", "merged"])
@pytest.mark.parametrize("timing", ["before", "after-tree"])
def test_body_edits_preserving_marker_stop_refresh(publisher, cycle, timing):
    client, files, report, publish = publisher
    publish()
    original = client.pulls[0]["body"]
    first = client.head
    if cycle != "unchanged":
        files[planner.RECIPE] = b"later fix"
        report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    if cycle == "merged":
        client.pulls[0].update(state="closed", merged_at=AT)
    if timing == "before":
        client.pulls[0]["body"] += "\nMaintainer note: preserve this.\n"
    else:
        client.body_edit_after = "git/trees"
    client.writes.clear()
    with pytest.raises(ValueError, match="body"):
        publish()
    assert client.pulls[0]["body"] == original + "\nMaintainer note: preserve this.\n"
    assert client.head == first and len(client.pulls) == 1
    assert not any(path.startswith("pulls") for path, _ in client.writes)
    if timing == "before":
        assert client.writes == []


def test_body_edit_after_pending_write_is_not_overwritten(publisher):
    client, files, report, publish = publisher
    publish()
    files[planner.RECIPE] = b"later fix"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    client.body_edit_after = "pulls/1"
    client.writes.clear()
    with pytest.raises(ValueError, match="body changed"):
        publish()
    assert client.pulls[0]["body"].endswith("\nMaintainer note: preserve this.\n")
    assert sum(path == "pulls/1" for path, _ in client.writes) == 1


def test_stale_receipt_prevents_any_api_write(publisher, monkeypatch):
    client, _, _, publish = publisher

    def stale(*_):
        raise ValueError("Main changed")

    monkeypatch.setattr(publication, "current", stale)
    with pytest.raises(ValueError, match="Main changed"):
        publish()
    assert client.writes == []


def test_merged_pr_allows_new_fix_without_reopening(publisher):
    client, files, report, publish = publisher
    publish()
    client.pulls[0].update(state="closed", merged_at=AT)
    files[planner.RECIPE] = b"later fix"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    assert publish()["status"] == "created"
    assert len(client.pulls) == 2 and client.pulls[0]["state"] == "closed"


@pytest.mark.parametrize("deleted_branch", [False, True])
@pytest.mark.parametrize("failure", ["pending", "branch", "create-before", "create-after"])
def test_merged_cycle_recovers_uncertain_writes(publisher, deleted_branch, failure):
    client, files, report, publish = publisher
    publish()
    client.pulls[0].update(state="closed", merged_at=AT)
    old_body = client.pulls[0]["body"]
    old_current = publication.marker(old_body)["current"]
    base = "d" * 40
    client.commits[base] = {"tree": {"sha": "merged-main-tree"}, "committer": {"date": AT}}
    report["source"] = base
    if deleted_branch:
        client.head = None
    files[planner.RECIPE] = b"later fix"
    report["files"][planner.RECIPE] = hashlib.sha256(files[planner.RECIPE]).hexdigest()
    if failure == "create-before":
        client.fail_before = "pulls"
    else:
        client.fail_after = {"pending": "pulls/1", "branch": "graphql", "create-after": "pulls"}[
            failure
        ]
    with pytest.raises(OSError):
        publish()
    written = client.head
    assert publish()["status"] in {"created", "unchanged"}
    if failure != "pending":
        assert client.head == written
    assert len(client.pulls) == 2 and client.pulls[0]["state"] == "closed"
    old_state = publication.marker(client.pulls[0]["body"])
    assert old_state["current"] == old_current
    assert client.pulls[0]["body"].split("<!--")[0] == old_body.split("<!--")[0]
    assert publication.marker(client.pulls[1]["body"])["current"]["head"] == client.head


@pytest.mark.parametrize(
    "url",
    [
        URL.rsplit("/artifacts/", 1)[0],
        URL + "/extra",
        URL.replace("/456", "/0"),
        URL.replace(REPOSITORY, "other/repo"),
    ],
)
def test_publisher_requires_exact_retained_artifact(publisher, url):
    client, files, report, _ = publisher
    with pytest.raises(ValueError, match="retained workflow evidence"):
        publication.publish(client, {"source": SOURCE}, report, files, BOT, url)
    assert client.writes == []


def test_refresh_uses_new_main_tree_and_preserves_owned_ancestry(publisher):
    client, files, report, publish = publisher
    publish()
    previous = client.head
    base = "d" * 40
    client.commits[base] = {"tree": {"sha": "new-main-tree"}, "committer": {"date": AT}}
    report["source"] = base
    result = publication.publish(client, {"source": base}, report, files, BOT, URL)
    assert result["status"] == "updated"
    assert client.commits[client.head]["parents"] == [previous, base]
    trees = [data for path, data in client.writes if path == "git/trees"]
    assert trees[-1]["base_tree"] == "new-main-tree"


def test_bot_token_is_used_only_for_writes(monkeypatch):
    observed = []
    monkeypatch.setenv("GH_TOKEN", "read-identity")
    monkeypatch.setenv("IMAGE_REMEDIATION_TOKEN", "write-identity")

    def request(self, endpoint, **kwargs):
        import os

        observed.append(os.environ["GH_TOKEN"])
        if kwargs.get("method") == "PATCH":
            raise OSError("uncertain write")
        return b"{}"

    monkeypatch.setattr(publication.GitHub, "request", request)
    client = publication.BotGitHub()
    client.request("read")
    with pytest.raises(OSError):
        client.request("write", method="PATCH")
    client.request("read")
    assert observed == ["read-identity", "write-identity", "read-identity"]


def test_workflow_keeps_writes_after_all_read_only_gates():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/image-remediation.yml").read_text())
    assert set(workflow["permissions"].values()) == {"read"}
    jobs = workflow["jobs"]
    assert jobs["publish"]["needs"] == ["prepare", "scan", "checks"]
    assert jobs["publish"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "read",
    }
    assert jobs["checks"]["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert jobs["scan"]["strategy"]["matrix"]["variant"] == ["baseline", "candidate"]
    assert set(jobs["scan"]["strategy"]["matrix"]["profile"]) == set(remediation.PROVIDER_MANIFESTS)
    steps = jobs["publish"]["steps"]
    app = next(item for item in steps if item.get("id") == "app")
    retain = next(item for item in steps if item.get("id") == "retain")
    assert steps.index(retain) < steps.index(app)
    assert (
        retain["with"]["name"]
        == "remediation-qualification-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert retain["with"].get("overwrite", False) is False
    assert retain["with"]["path"].splitlines() == [
        "${{ runner.temp }}/remediation",
        "${{ runner.temp }}/results",
    ]
    publish = steps[-1]
    assert publish["env"]["ARTIFACT_ID"] == "${{ steps.retain.outputs.artifact-id }}"
    assert '--artifact-id "$ARTIFACT_ID"' in publish["run"]
    assert {key: value for key, value in app["with"].items() if key.startswith("permission-")} == {
        "permission-contents": "write",
        "permission-pull-requests": "write",
    }
    assert "vars.IMAGE_REMEDIATION_ENABLED == 'true'" in app["if"]
    assert "head_repository.full_name == github.repository" in jobs["acquire"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["acquire"]["if"]
    assert jobs["prepare"]["needs"] == ["acquire", "providers"]
    assert "!cancelled()" in jobs["prepare"]["if"]
    assert "needs.providers.result == 'skipped'" in jobs["prepare"]["if"]
    assert "needs.providers.result == 'success'" in jobs["prepare"]["if"]
    for job in ("scan", "checks", "publish"):
        assert "!cancelled()" in jobs[job]["if"]
        assert "needs.prepare.result == 'success'" in jobs[job]["if"]
    for dependency in ("scan", "checks"):
        assert f"needs.{dependency}.result == 'success'" in jobs["publish"]["if"]
    assert "needs.acquire.outputs.providers != '[]'" in jobs["providers"]["if"]
    assert jobs["providers"]["strategy"]["matrix"] == {
        "shard": "${{ fromJSON(needs.acquire.outputs.providers) }}"
    }
    for job in ("providers", "scan"):
        assert jobs[job]["runs-on"] == "ubuntu-latest-m"
        assert jobs[job]["strategy"]["max-parallel"] == 4
        assert jobs[job]["env"]["MAF_GO_BUILD_CONCURRENCY"] == (
            "${{ vars.IMAGE_REMEDIATION_GO_CONCURRENCY || 'auto' }}"
        )
    assert {"acquire", "providers"} <= set(jobs["report-failure"]["needs"])
    assert "needs.providers.result == 'failure'" in jobs["report-failure"]["if"]
