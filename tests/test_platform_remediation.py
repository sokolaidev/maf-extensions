"""Platform proposals must prove vendor availability and removal in every inherited image."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_container_release_image as runtime  # noqa: E402
import image_remediation_evidence as acquisition  # noqa: E402
import image_remediation_pr as publication  # noqa: E402
import plan_platform_remediation as planner  # noqa: E402
import platform_remediation as remediation  # noqa: E402
from bicep_source import policy  # noqa: E402
from container_release import read, scan_result, write  # noqa: E402
from test_go_remediation import AT, IMAGE, finding, retained  # noqa: E402
from test_image_remediation import BOT, SOURCE, URL, FakeGitHub  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    copy_checkout(ROOT, root)
    return root


def copy_checkout(source, root):
    for name in planner.inputs(source):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    path = root / planner.NUGET
    text = path.read_text()
    path.write_text(re.sub(r'(Include="MessagePack" Version=")[^"]+', r"\g<1>3.1.12", text))
    path = root / "images/antivirus-sandbox/Dockerfile"
    path.write_text(re.sub(r"clamav-scanner=[^\s]+", "clamav-scanner=1.4.6-r0", path.read_text()))


def dotnet():
    match = finding("/usr/local/bin/bicep", "MessagePack", "3.1.11", "3.1.13", "CVE-test")
    match["artifact"]["type"] = "dotnet"
    return match


def apk(name="glibc", installed="2.43-r0", fixed="2.43-r2", vendor="wolfi"):
    match = finding("/lib/apk/db/installed", name, installed, fixed, "CVE-apk")
    match["artifact"].update(type="apk", purl=f"pkg:apk/{vendor}/{name}@{installed}?arch=x86_64")
    return match


@pytest.mark.parametrize(
    "installed,fixed",
    [("2.43-r0", "2.43-r13"), ("3.53.4-r0", "3.53.4-r2"), ("1.4.6-r0", "1.4.7-r0")],
)
def test_vendor_versions_preserve_two_and_three_component_families(installed, fixed):
    assert planner.target(installed, "apk", {"state": "fixed", "versions": [fixed]}) == fixed


def test_source_route_uses_native_manifest_and_covers_inherited_profiles(checkout, tmp_path):
    path = retained(tmp_path, [dotnet()], profile="bicep")
    plan = planner.plan(checkout, [path])
    assert plan["affectedProfiles"] == ["bicep", "bicep-avm", "bicep-docker-sbx"]
    assert plan["findings"][0]["publishedRoute"] == "upstream"
    assert plan["findings"][0]["route"] == "source"
    changes = planner.proposal(checkout, plan)
    assert set(changes) == {planner.NUGET}
    assert changes[planner.NUGET] == (checkout / planner.NUGET).read_bytes().replace(
        b"3.1.12", b"3.1.13"
    )
    assert planner.plan(checkout, [path, path]) == plan


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("fixed", "source-pin-sufficient"),
        ("major", "no-compatible-fix"),
        ("minor", "no-compatible-fix"),
        ("none", "no-compatible-fix"),
        ("preview", "no-compatible-fix"),
        ("package", "unmapped-bicep-component"),
        ("path", "unmapped-bicep-component"),
        ("kind", "unsupported-component"),
        ("version", "unsupported-version"),
        ("upstream", "upstream-binary-upgrade-required"),
    ],
)
def test_bicep_never_invents_compatible_source_updates(
    checkout, tmp_path, monkeypatch, mutation, status
):
    match = dotnet()
    if mutation in {"fixed", "major", "minor", "preview", "none"}:
        match["vulnerability"]["fix"]["versions"] = {
            "fixed": ["3.1.12"],
            "major": ["4.0.0"],
            "minor": ["3.2.0"],
            "preview": ["3.1.13-beta"],
            "none": [],
        }[mutation]
    elif mutation == "package":
        match["artifact"]["name"] = "Unknown.Package"
    elif mutation == "path":
        match["artifact"]["locations"].append({"path": "/another/compiler"})
    elif mutation == "kind":
        match["artifact"]["type"] = "python"
    elif mutation == "version":
        match["artifact"]["version"] = "latest"
    else:
        monkeypatch.setitem(planner.BICEP_ROUTES, "bicep", "upstream")
    plan = planner.plan(checkout, [retained(tmp_path, [match], profile="bicep")])
    assert plan["findings"][0]["status"] == status
    assert not plan["updates"]
    assert not planner.proposal(checkout, plan)


def test_apk_rebuild_uses_exact_vendor_base_without_fabricating_edit(checkout, tmp_path):
    paths = [
        retained(tmp_path, [apk()], profile=profile, name=profile)
        for profile in ("bicep", "bicep-avm", "terraform-avm")
    ]
    plan = planner.plan(checkout, paths)
    assert plan["affectedProfiles"] == sorted(
        {
            "bicep",
            "bicep-avm",
            "bicep-docker-sbx",
            "terraform-avm",
            "terraform-random",
            "opentofu-avm",
            "opentofu-random",
        }
    )
    assert {row["status"] for row in plan["findings"]} == {"rebuild-required"}
    assert len({row["base"] for row in plan["findings"]}) == 1
    assert not planner.proposal(checkout, plan)


@pytest.mark.parametrize("origin", ["bicep", "bicep-avm", "bicep-docker-sbx"])
def test_unpinned_apks_follow_the_observed_recipe_layer(checkout, tmp_path, origin):
    expected = planner.plan(checkout, [retained(tmp_path, [apk()], profile=origin)])
    profiles = [origin] if origin == "bicep-docker-sbx" else sorted(planner.BICEP_ROUTES)
    assert expected["affectedProfiles"] == profiles
    for profile in planner.BICEP_ROUTES:
        assert remediation.applies(expected["findings"][0], profile) == (profile in profiles)
        assert bool(remediation.vendor_requests(expected, profile)) == (profile in profiles)


def test_child_only_apk_does_not_block_a_shared_bicep_proposal(checkout, tmp_path):
    expected = planner.plan(
        checkout,
        [
            retained(tmp_path, [dotnet()], profile="bicep", name="parent"),
            retained(
                tmp_path,
                [apk("shadow", "4.19.0-r0", "4.19.0-r1")],
                profile="bicep-docker-sbx",
                name="child",
            ),
        ],
    )
    assert expected["affectedProfiles"] == sorted(planner.BICEP_ROUTES)
    assert set(planner.proposal(checkout, expected)) == {planner.NUGET}
    compiler = dotnet()["artifact"] | {"version": "3.1.13"}
    inventory = {"artifacts": [compiler]}
    for profile in ("bicep", "bicep-avm"):
        remediation.verify_packages(expected, inventory, profile)
        assert remediation.vendor_requests(expected, profile) == []
    with pytest.raises(ValueError, match="absent from the rebuilt inventory"):
        remediation.verify_packages(expected, inventory, "bicep-docker-sbx")
    inventory["artifacts"].append(apk("shadow", "4.19.0-r1", "4.19.0-r1")["artifact"])
    remediation.verify_packages(expected, inventory, "bicep-docker-sbx")


def test_parent_apk_pin_observed_in_child_still_qualifies_all_inherited_profiles(
    checkout, tmp_path
):
    path = checkout / planner.RUNTIME_RECIPES["bicep"]
    path.write_text(path.read_text().replace("libssl3", "libssl3=3.5.0-r0"))
    expected = planner.plan(
        checkout,
        [retained(tmp_path, [apk("libssl3", "3.5.0-r0", "3.5.0-r1")], profile="bicep-docker-sbx")],
    )
    assert expected["affectedProfiles"] == sorted(planner.BICEP_ROUTES)
    assert len(planner.proposal(checkout, expected)) == 1
    assert all(remediation.vendor_requests(expected, profile) for profile in planner.BICEP_ROUTES)


def test_apk_pin_updates_every_build_consumed_occurrence(checkout, tmp_path):
    match = apk("clamav-scanner", "1.4.6-r0", "1.4.6-r1", "alpine")
    plan = planner.plan(checkout, [retained(tmp_path, [match], profile="antivirus")])
    assert plan["findings"][0]["status"] == "update"
    files = planner.proposal(checkout, plan)
    assert len(files) == 1
    raw = files["images/antivirus-sandbox/Dockerfile"]
    assert raw.count(b"clamav-scanner=1.4.6-r1") == 2
    assert b"clamav-scanner=1.4.6-r0" not in raw


def test_sufficient_apk_pin_qualifies_the_current_recipe_version(checkout, tmp_path, monkeypatch):
    path = checkout / "images/antivirus-sandbox/Dockerfile"
    path.write_text(path.read_text().replace("clamav-scanner=1.4.6-r0", "clamav-scanner=1.4.6-r2"))
    expected = planner.plan(
        checkout,
        [
            retained(
                tmp_path,
                [apk("clamav-scanner", "1.4.6-r0", "1.4.6-r1", "alpine")],
                profile="antivirus",
            )
        ],
    )
    row = expected["findings"][0]
    assert row["status"] == "source-pin-sufficient"
    assert row["target"] == "1.4.6-r2"
    assert not planner.proposal(checkout, expected)

    def run(command, **kwargs):
        assert command[-2:] == ["clamav-scanner", "1.4.6-r2"]
        return subprocess.CompletedProcess(command, 0, "verified:clamav-scanner=1.4.6-r2\n", "")

    monkeypatch.setattr(remediation.subprocess, "run", run)
    assert remediation.vendor_checks(expected, "antivirus")[0]["version"] == "1.4.6-r2"
    artifact = apk("clamav-scanner", "1.4.6-r2", "1.4.6-r2", "alpine")["artifact"]
    remediation.verify_packages(expected, {"artifacts": [artifact]}, "antivirus")
    artifact["version"] = "1.4.6-r1"
    with pytest.raises(ValueError, match="absent from the rebuilt inventory"):
        remediation.verify_packages(expected, {"artifacts": [artifact]}, "antivirus")


def test_candidate_pin_changes_do_not_change_fixture_baselines(checkout, tmp_path):
    paths = [
        retained(tmp_path, [dotnet()], profile="bicep", name="bicep"),
        retained(
            tmp_path,
            [apk("clamav-scanner", "1.4.6-r0", "1.4.6-r1", "alpine")],
            profile="antivirus",
            name="antivirus",
        ),
    ]
    plan = planner.plan(checkout, paths)
    for name, content in planner.proposal(checkout, plan).items():
        (checkout / name).write_bytes(content)
    copied = tmp_path / "candidate-fixture"
    copy_checkout(checkout, copied)
    replay = planner.plan(copied, paths)
    assert {row["status"] for row in replay["findings"]} == {"update"}
    assert replay["updates"] == plan["updates"]


@pytest.mark.parametrize("failure", [None, "render", "remove", "survivor", "inspect"])
def test_graphviz_runtime_requires_verified_cleanup(monkeypatch, tmp_path, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if (command[1] == "rm" and failure == "remove") or (
            command[1] == "ps" and failure == "inspect"
        ):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and failure == "survivor" else ""
        )

    async def output(command):
        if failure == "render":
            raise ValueError("render failed")
        return b""

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            runtime.check("graphviz", IMAGE, tmp_path)
    else:
        result = runtime.check("graphviz", IMAGE, tmp_path)
        assert result["passed"] is True
        assert result["containersRemaining"] == []
    create = commands[0]
    name = create[create.index("--name") + 1]
    assert ["docker", "rm", "--force", name] in commands
    if failure not in {"render", "remove"}:
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("vendor", "unmapped-vendor-package"),
        ("version", "unsupported-version"),
        ("family", "no-compatible-fix"),
        ("profile", "unsupported-vendor"),
    ],
)
def test_os_adapter_preserves_unmapped_and_unfixed_cases(checkout, tmp_path, mutation, status):
    match = apk()
    profile = "bicep"
    if mutation == "vendor":
        match["artifact"]["purl"] = "pkg:apk/unknown/glibc@2.43-r0"
    elif mutation == "version":
        match["artifact"]["version"] = "2.43_git20261105-r0"
    elif mutation == "family":
        match["vulnerability"]["fix"]["versions"] = ["2.44.0-r0"]
    else:
        profile = "egress-proxy"
    plan = planner.plan(checkout, [retained(tmp_path, [match], profile=profile)])
    assert plan["findings"][0]["status"] == status
    assert not plan["updates"] and not plan["affectedProfiles"]


@pytest.fixture
def qualified(checkout, tmp_path, monkeypatch):
    work = tmp_path / "work"
    path = retained(work / "evidence", [dotnet()], profile="bicep")
    expected = planner.plan(checkout, [path])
    write(work / "receipt.json", {"source": SOURCE, "directories": [path.name]})
    write(work / "plan.json", expected)
    monkeypatch.setattr(remediation, "git", lambda *_: SOURCE)
    files = planner.proposal(checkout, expected)
    results = tmp_path / "results"
    for variant in ("baseline", "candidate"):
        for profile in expected["affectedProfiles"]:
            directory = results / f"{variant}-{profile}"
            data = policy(checkout / planner.BICEP / "source.json")
            if variant == "candidate":
                data["packages"]["MessagePack"] = "3.1.13"
            packages = [
                {
                    "id": name,
                    "name": name,
                    "type": "dotnet",
                    "version": value,
                    "locations": [{"path": "/usr/local/bin/bicep"}],
                }
                for name, value in data["packages"].items()
            ]
            sbom = {
                "source": {"type": "image", "metadata": {"imageID": IMAGE}},
                "artifacts": packages,
            }
            report = read(path / "grype.json")
            report["matches"] = []
            if variant == "baseline":
                match = dotnet()
                match["artifact"] = next(item for item in packages if item["name"] == "MessagePack")
                report["matches"] = [match]
            write(directory / "sbom.syft.json", sbom)
            write(directory / "grype.json", report)
            write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
            write(
                directory / "runtime.json",
                {
                    "profile": profile,
                    "imageId": IMAGE,
                    "passed": True,
                    "sandboxesRemaining"
                    if profile == "bicep-docker-sbx"
                    else "containersRemaining": [],
                    "bicepAvm" if profile == "bicep-avm" else "bicep": {
                        "build": {"route": "source", **data}
                    },
                },
            )
            write(
                directory / "build.json",
                {"source_commit": SOURCE, "profile": profile, "local_image_id": IMAGE},
            )
            write(directory / "vendor.json", [])
            write(
                directory / "qualification.json",
                {
                    "schemaVersion": 1,
                    "source": SOURCE,
                    "profile": profile,
                    "variant": variant,
                    "route": "source",
                    "plan": planner.digest(work / "plan.json"),
                    "inputs": expected["inputs"]
                    | (planner.file_hashes(files) if variant == "candidate" else {}),
                },
            )
    return checkout, work, results


@pytest.mark.parametrize("command", ["acquire", "qualify"])
@pytest.mark.parametrize("actions", [False, True])
def test_cli_exposes_results_locally_and_appends_actions_outputs(
    qualified, tmp_path, monkeypatch, capsys, command, actions
):
    root, work, results = qualified
    output = tmp_path / "outputs"
    if actions:
        output.write_text("existing=kept\n")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    else:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(remediation, "ROOT", root)
    monkeypatch.setattr(remediation, "acquire", lambda *args, **kwargs: read(work / "plan.json"))
    monkeypatch.setattr(remediation, "current", lambda *_: None)
    monkeypatch.setattr(remediation, "competing_pulls", lambda *_: [])
    monkeypatch.setattr(
        sys,
        "argv",
        ["platform-remediation", command, "--work", str(work), "--results", str(results)],
    )
    remediation.main()
    stdout = capsys.readouterr().out
    if actions:
        assert not stdout
    values = dict(
        line.split("=", 1) for line in (output.read_text() if actions else stdout).splitlines()
    )
    if actions:
        existing = values.pop("existing")
        assert existing == "kept"
    if command == "acquire":
        assert values["source"] == SOURCE
        assert json.loads(values["profiles"]) == sorted(planner.BICEP_ROUTES)
        assert read(work / "competing-pulls.json") == []
    else:
        assert values == {"ready": "true"}
        assert read(work / "qualified.json")["status"] == "ready"


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("actions", [False, True])
@pytest.mark.parametrize("changing", [acquisition.EvidenceChanging, acquisition.HistoryAdvanced])
def test_platform_acquisition_retries_and_defers_without_downstream_work(
    tmp_path, monkeypatch, capsys, recover, actions, changing
):
    work, output, summary = (tmp_path / name for name in ("work", "output", "summary"))
    attempts = []
    sleeps = []
    conflicts = []

    def acquire(client, root, directory, **options):
        assert tuple(options["selected_profiles"]) == tuple(planner.RUNTIME_RECIPES)
        assert options["planner"] is planner.plan
        assert options["require_all_profiles"] is False
        attempts.append(directory)
        if len(attempts) < 3 or not recover:
            raise changing("Monitoring snapshot changed")
        write(directory / "receipt.json", {"source": SOURCE})
        result = {"updates": [], "findings": [], "affectedProfiles": ["bicep"]}
        write(directory / "plan.json", result)
        return result

    def competing(*args):
        conflicts.append(args)
        return []

    monkeypatch.setattr(acquisition, "acquire", acquire)
    monkeypatch.setattr(acquisition.time, "sleep", sleeps.append)
    monkeypatch.setattr(remediation, "competing_pulls", competing)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    if actions:
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    else:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "argv", ["platform-remediation", "acquire", "--work", str(work)])
    remediation.main()
    stdout = capsys.readouterr().out
    values = dict(
        line.split("=", 1)
        for line in (output.read_text() if actions else stdout).splitlines()
        if "=" in line
    )
    assert sleeps == [30, 30] and len(set(attempts)) == 3
    assert len(conflicts) == int(recover)
    if recover:
        assert values == {"source": SOURCE, "profiles": '["bicep"]'}
        assert read(work / "receipt.json")["source"] == SOURCE
    else:
        assert values == {"status": "deferred", "profiles": "[]"}
        assert read(work / "acquisition.json")["status"] == "deferred"
        assert "retries automatically" in summary.read_text()
        assert not (work / "receipt.json").exists()
        assert not (work / "plan.json").exists()
    assert read(work / "attempts/1/deferred.json")["attempt"] == 1


@pytest.mark.parametrize("run_id", [None, "", "invalid", "0", "-1"])
def test_publish_requires_an_actions_run_before_qualification(
    tmp_path, monkeypatch, capsys, run_id
):
    if run_id is None:
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setattr(remediation, "qualify", lambda *_: pytest.fail("qualification reached"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform-remediation",
            "publish",
            "--work",
            str(tmp_path),
            "--results",
            str(tmp_path / "results"),
            "--bot",
            BOT,
            "--artifact-id",
            "456",
        ],
    )
    with pytest.raises(SystemExit) as error:
        remediation.main()
    assert error.value.code == 2
    assert (
        "publish requires a positive GITHUB_RUN_ID from GitHub Actions" in capsys.readouterr().err
    )


def test_complete_qualification_requires_all_inherited_profiles(qualified):
    report = remediation.qualify(*qualified)
    assert report["status"] == "ready"
    assert len(report["scans"]) == 6
    assert set(report["files"]) == {planner.NUGET}


@pytest.mark.parametrize(
    "mutation",
    ["source", "inputs", "route", "runtime", "missing", "inventory", "receipt", "scan", "vendor"],
)
def test_incomplete_or_contradictory_evidence_never_admits_pr(qualified, mutation):
    root, work, results = qualified
    directory = results / "candidate-bicep-avm"
    if mutation in {"source", "inputs", "route"}:
        path = directory / "qualification.json"
        data = read(path)
        data[mutation] = "different"
    elif mutation == "runtime":
        path = directory / "runtime.json"
        data = read(path)
        data["passed"] = False
    elif mutation == "missing":
        (directory / "grype.json").unlink()
        with pytest.raises(FileNotFoundError):
            remediation.qualify(root, work, results)
        return
    elif mutation == "inventory":
        path = directory / "sbom.syft.json"
        data = read(path)
        data["artifacts"] = []
    elif mutation == "receipt":
        path = directory / "runtime.json"
        data = read(path)
        data["bicepAvm"]["build"]["runtimeVersion"] = "99.0.0"
    elif mutation == "vendor":
        path = directory / "vendor.json"
        data = [{"base": "unexpected", "component": "glibc", "version": "2.43-r2"}]
    else:
        path = directory / "assessment.json"
        data = read(path)
        data["outcome"] = "unavailable"
    write(path, data)
    with pytest.raises(ValueError):
        remediation.qualify(root, work, results)


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("profile", ["bicep", "bicep-avm", "bicep-docker-sbx"])
def test_targeted_findings_remain_blocking_even_if_ignored(qualified, ignored, profile):
    root, work, results = qualified
    directory = results / f"candidate-{profile}"
    sbom = read(directory / "sbom.syft.json")
    match = dotnet()
    match["artifact"] = next(item for item in sbom["artifacts"] if item["name"] == "MessagePack")
    report = read(directory / "grype.json")
    report["ignoredMatches" if ignored else "matches"] = [match]
    write(directory / "grype.json", report)
    if ignored:
        with pytest.raises(ValueError, match="filtered"):
            remediation.qualify(root, work, results)
        return
    write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
    assert remediation.qualify(root, work, results)["status"] == "unresolved"


def test_fresh_main_already_fixed_keeps_release_needed_without_empty_pr(qualified):
    root, work, results = qualified
    for directory in results.glob("baseline-*"):
        report = read(directory / "grype.json")
        report["matches"] = []
        write(directory / "grype.json", report)
        write(
            directory / "assessment.json",
            scan_result(report, IMAGE, AT, sbom=read(directory / "sbom.syft.json")),
        )
    assert remediation.qualify(root, work, results)["status"] == "release-needed"


def test_vendor_probe_uses_signed_install_and_exact_pinned_base(checkout, tmp_path, monkeypatch):
    expected = planner.plan(checkout, [retained(tmp_path, [apk()], profile="graphviz")])
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="verified:glibc=2.43-r2\n", stderr="vendor receipt")

    monkeypatch.setattr(remediation.subprocess, "run", run)
    reports = remediation.vendor_checks(expected, "graphviz")
    assert reports[0]["version"] == "2.43-r2"
    assert "@sha256:" in reports[0]["base"]
    assert calls[0][0][-2:] == ["glibc", "2.43-r2"]
    assert "--allow-untrusted" not in " ".join(calls[0][0])
    assert 'apk info -e "$1=$2"' in " ".join(calls[0][0])
    assert calls[0][1]["check"] is True


def test_vendor_probe_failure_does_not_become_no_fix_or_clean(checkout, tmp_path, monkeypatch):
    expected = planner.plan(checkout, [retained(tmp_path, [apk()], profile="graphviz")])

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(remediation.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        remediation.vendor_checks(expected, "graphviz")


def test_multiple_findings_request_only_the_highest_required_vendor_fix(checkout, tmp_path):
    first, second = apk(fixed="2.43-r1"), apk(fixed="2.43-r2")
    second["vulnerability"]["id"] = "CVE-another"
    expected = planner.plan(checkout, [retained(tmp_path, [first, second], profile="bicep")])
    for profile in expected["affectedProfiles"]:
        requests = remediation.vendor_requests(expected, profile)
        assert len(requests) == 1
        assert requests[0][1:] == ("glibc", "2.43-r2")


def test_human_readable_apk_output_cannot_replace_exact_version_proof(
    checkout, tmp_path, monkeypatch
):
    expected = planner.plan(checkout, [retained(tmp_path, [apk()], profile="graphviz")])
    monkeypatch.setattr(
        remediation.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(
            stdout="glibc: GNU C Library\n", stderr="probe diagnostic"
        ),
    )
    with pytest.raises(ValueError, match="requested version") as error:
        remediation.vendor_checks(expected, "graphviz")
    assert "glibc=2.43-r2" in str(error.value)
    assert expected["findings"][0]["base"] in str(error.value)
    assert "glibc: GNU C Library" in str(error.value)
    assert "probe diagnostic" in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "graphql",
        f"repos/{remediation.REPOSITORY}/pulls",
        f"repos/{remediation.REPOSITORY}/pulls/123",
    ],
)
def test_new_competing_pr_prevents_branch_or_pr_write(monkeypatch, endpoint):
    monkeypatch.setattr(remediation, "competing_pulls", lambda *_: [1234])
    client = remediation.PlatformGitHub({planner.NUGET})
    with pytest.raises(ValueError, match="1234"):
        client.request(endpoint, method="POST")


def test_unrelated_candidate_findings_remain_visible(qualified):
    root, work, results = qualified
    directory = results / "candidate-bicep"
    sbom = read(directory / "sbom.syft.json")
    report = read(directory / "grype.json")
    unrelated = apk()
    sbom["artifacts"].append(unrelated["artifact"])
    report["matches"] = [unrelated]
    write(directory / "sbom.syft.json", sbom)
    write(directory / "grype.json", report)
    write(directory / "assessment.json", scan_result(report, IMAGE, AT, sbom=sbom))
    qualified_report = remediation.qualify(root, work, results)
    assert qualified_report["status"] == "ready"
    candidate = next(
        item
        for item in qualified_report["scans"]
        if item["profile"] == "bicep" and item["variant"] == "candidate"
    )
    assert candidate["assessment"]["outcome"] == "vulnerable"


def test_unplanned_artifact_edit_is_rejected_before_any_source_write(qualified):
    root, work, _ = qualified
    expected = read(work / "plan.json")
    expected["updates"][0]["path"] = "../outside.json"
    write(work / "plan.json", expected)
    before = (root / planner.NUGET).read_bytes()
    with pytest.raises(ValueError, match="differs"):
        remediation.validate(root, work)
    assert (root / planner.NUGET).read_bytes() == before


@pytest.mark.parametrize("mutation", ["vendor", "location", "missing", "old"])
def test_candidate_vendor_inventory_cannot_substitute_another_component(
    checkout, tmp_path, mutation
):
    expected = planner.plan(checkout, [retained(tmp_path, [apk()], profile="graphviz")])
    artifact = apk(installed="2.43-r2")["artifact"]
    if mutation == "vendor":
        artifact["purl"] = artifact["purl"].replace("/wolfi/", "/unknown/")
    elif mutation == "location":
        artifact["locations"] = [{"path": "/unrelated/database"}]
    elif mutation == "old":
        artifact["version"] = "2.43-r0"
    inventory = {"artifacts": [] if mutation == "missing" else [artifact]}
    with pytest.raises(ValueError):
        remediation.verify_packages(expected, inventory, "graphviz")


def test_competing_native_manifest_prs_defer_automation():
    class Client(remediation.GitHub):
        def pages(self, endpoint):
            if endpoint.endswith("/files"):
                return [{"filename": planner.NUGET}]
            return [
                {
                    "number": 12,
                    "head": {
                        "ref": "dependabot/nuget/bicep",
                        "repo": {"full_name": remediation.REPOSITORY},
                    },
                }
            ]

    assert remediation.competing_pulls(Client(), {planner.NUGET}) == [12]
    assert remediation.competing_pulls(Client(), {"unrelated"}) == []


def test_platform_branch_and_title_survive_create_retry_and_refresh(monkeypatch):
    class Client(FakeGitHub):
        branch = remediation.PlatformGitHub.branch
        title = remediation.PlatformGitHub.title

        def pages(self, endpoint):
            rows = super().pages(endpoint)
            if "matching-refs" in endpoint:
                for row in rows:
                    row["ref"] = f"refs/heads/{self.branch}"
            return rows

        def request(self, endpoint, **kwargs):
            result = super().request(endpoint, **kwargs)
            if endpoint.endswith("/pulls") and kwargs.get("method") == "POST":
                self.pulls[-1]["head"]["ref"] = self.branch
                result = json.dumps(self.pulls[-1]).encode()
            return result

    monkeypatch.setattr(publication, "current", lambda *_: None)
    client = Client()
    files = {planner.NUGET: b"first proposal"}
    report = {
        "status": "ready",
        "kind": "platform",
        "source": SOURCE,
        "scans": [],
        "files": planner.file_hashes(files),
    }

    def deliver():
        return publication.publish(client, {"source": SOURCE}, report, files, BOT, URL)

    assert deliver()["status"] == "created"
    assert deliver()["status"] == "unchanged"
    files[planner.NUGET] = b"second proposal"
    report["files"] = planner.file_hashes(files)
    assert deliver()["status"] == "updated"
    assert len(client.pulls) == 1
    assert client.pulls[0]["title"] == client.title
    assert "Refs #2514, #2517" in client.pulls[0]["body"]
    mutations = [data for endpoint, data in client.writes if endpoint == "graphql"]
    assert {item["variables"]["input"]["refUpdates"][0]["name"] for item in mutations} == {
        f"refs/heads/{client.branch}"
    }


def test_workflow_requires_qualification_before_token_and_retains_retry_evidence():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-image-remediation.yml").read_text()
    )
    jobs = workflow["jobs"]
    assert jobs["publish"]["needs"] == ["acquire", "scan", "checks"]
    steps = jobs["publish"]["steps"]
    order = {step["id"]: i for i, step in enumerate(steps) if "id" in step}
    assert order["qualify"] < order["retain"] < order["app"]
    retain = steps[order["retain"]]["with"]
    assert "github.run_attempt" in retain["name"] and "overwrite" not in retain
    assert "PLATFORM_IMAGE_REMEDIATION_ENABLED" in steps[order["app"]]["if"]
    assert jobs["scan"]["strategy"]["matrix"]["variant"] == ["baseline", "candidate"]


def test_platform_scanners_use_authenticated_release_assets():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-image-remediation.yml").read_text()
    )
    scan = workflow["jobs"]["scan"]
    step = next(step for step in scan["steps"] if step.get("name") == "Install pinned scanners")
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    for job in ("scan", "checks"):
        assert workflow["jobs"][job]["if"] == "needs.acquire.outputs.profiles != '[]'"
