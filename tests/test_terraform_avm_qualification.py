"""The AVM release gate refuses drift, false successes and incomplete cleanup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_container_release_image as runtime  # noqa: E402
import terraform_avm_qualification as avm  # noqa: E402
from terraform_dependencies import satisfies  # noqa: E402

pytestmark = pytest.mark.workflow


def report(engine="terraform"):
    version = avm.read(avm.CONTEXT / "image.json")["engines"][engine]["version"]
    refusals = {"uncached-version", "wrong-lock", "upstream-lock", "azure-root-uncached"}
    return {
        "receipt": avm.expected_receipt(
            "dependencies.terraform-avm.json"
            if engine == "terraform"
            else "dependencies.opentofu-avm-network.json"
        ),
        "checks": [
            {
                "name": name,
                "version": version,
                "error": None,
                "sourceUnchanged": True,
                "initExitCode": 1 if name in refusals else 0,
                "validateExitCode": None
                if name in refusals
                else 1
                if name == "azure-root-invalid"
                else 0,
                "validationValid": None if name in refusals else name != "azure-root-invalid",
                "missingNameDiagnostic": name == "azure-root-invalid",
                "providerLinks": 6 if name.startswith("azure-root") else 3,
                "providerCopies": 0,
                "providersInMirror": True,
                "nestedModuleLoaded": not name.startswith("azure-root"),
                "expectedRefusal": name in refusals,
            }
            for name in avm.cases(engine)
        ],
    }


def test_success_retains_catalogue_identity_and_consumer_results():
    result = avm.evidence(json.dumps(report()).encode())
    assert [item["name"] for item in result["checks"]] == list(avm.CASES)
    assert all(item["passed"] for item in result["checks"])
    compile(avm.command()[-1], "terraform-avm-probe", "exec")


@pytest.mark.parametrize("field", ["manifest_sha256", "policy_sha256", "engine", "schema"])
def test_receipt_drift_is_refused(field):
    data = report()
    data["receipt"][field] = "different"
    with pytest.raises(ValueError, match="receipt"):
        avm.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("kind", ["providers", "modules", "registry_modules"])
def test_artifact_inventory_drift_is_refused(kind):
    data = report()
    data["receipt"]["artifacts"][kind] = "0" * 64
    with pytest.raises(ValueError, match="receipt"):
        avm.evidence(json.dumps(data).encode())


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "name", "wrong-order"),
        (0, "version", "0.0.0"),
        (0, "error", "launcher refused"),
        (0, "sourceUnchanged", False),
        (0, "initExitCode", 1),
        (0, "validateExitCode", 1),
        (0, "validationValid", False),
        (0, "providerLinks", 0),
        (0, "providerCopies", 1),
        (0, "providersInMirror", False),
        (0, "nestedModuleLoaded", False),
        (2, "sourceUnchanged", False),
        (3, "initExitCode", 0),
        (3, "initExitCode", 124),
        (3, "validateExitCode", 0),
        (3, "validationValid", True),
        (3, "expectedRefusal", False),
        (4, "expectedRefusal", False),
        (4, "sourceUnchanged", False),
        (5, "initExitCode", 0),
        (5, "expectedRefusal", False),
    ],
)
def test_false_success_is_refused(index, field, value):
    data = report()
    data["checks"][index][field] = value
    with pytest.raises(ValueError, match="consumer probe"):
        avm.evidence(json.dumps(data).encode())


def test_missing_check_is_refused():
    data = report()
    data["checks"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        avm.evidence(json.dumps(data).encode())


@pytest.mark.parametrize("failure", [None, "receipt", "survivor"])
@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_runtime_checks_identity_and_observes_cleanup(monkeypatch, tmp_path, failure, engine):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, "container" if command[1] == "ps" and failure == "survivor" else ""
        )

    async def output(command, *, timeout):
        assert timeout == 360
        data = report(engine)
        if failure == "receipt":
            data["receipt"]["engine"] = "other"
        return json.dumps(data).encode()

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime, "_smoke_output", output)
    if failure:
        with pytest.raises(ValueError):
            runtime.check(f"{engine}-avm", "sha256:" + "a" * 64, tmp_path)
    else:
        result = runtime.check(f"{engine}-avm", "sha256:" + "a" * 64, tmp_path)
        assert len(result[f"{engine}Avm"]["checks"]) == len(avm.cases(engine))
        assert result["containersRemaining"] == []
        assert result["liveDeploymentVerified"] is False
    name = commands[0][commands[0].index("--name") + 1]
    assert commands[0][commands[0].index("--network") + 1] == "none"
    assert ["docker", "rm", "--force", name] in commands
    if failure != "receipt":
        assert commands[-1] == ["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]


def test_build_uses_full_catalogue_not_network_fixture():
    script = (avm.ROOT / "scripts/build_scan_image.sh").read_text()
    recipe = script.split("  terraform-avm)", 1)[1].split("    ;;", 1)[0]
    assert "--engine terraform --profile builtin" in recipe
    assert "--build-arg MANIFEST=dependencies.terraform-avm.json" in recipe
    assert "dependencies.terraform-avm-network.json" not in recipe
    assert "images/terraform-sandbox/prepared.Dockerfile" in recipe
    assert "images/terraform-sandbox/AVM.md" in recipe


@pytest.mark.parametrize(
    "name,diagnostics,expected",
    [
        (
            "uncached-version",
            "host registry.terraform.io does not provide a modules\nservice.",
            True,
        ),
        ("uncached-version", "Failed to retrieve available versions: timeout", False),
        ("wrong-lock", "Failed to install provider: doesn't match any of the\nchecksums", True),
        ("wrong-lock", "Failed to install provider: permission denied", False),
        (
            "upstream-lock",
            "SOKOLAI provider lock verification failed: expected baked h1 checksum.",
            True,
        ),
        ("upstream-lock", "Provider lock file not updated", False),
        (
            "azure-root-uncached",
            "provider hashicorp/azurerm: no available releases\nmatch the given constraints 99.0.0",
            True,
        ),
        ("azure-root-uncached", "registry.opentofu.org/hashicorp/azurerm: timeout", False),
        ("azure-root-uncached", "hashicorp/random: no available releases match", False),
        ("azure-root-uncached", "hashicorp/azurerm: 0.0.0 is not a valid provider version", False),
    ],
)
def test_refusals_tolerate_line_wrapping_but_require_the_expected_reason(
    name, diagnostics, expected
):
    assert avm.expected_refusal(name, diagnostics) is expected


@pytest.mark.parametrize("failure", [None, "receipt", "copies", "missing-case", "upstream-lock"])
def test_opentofu_release_evidence_is_bound_to_its_own_manifest(failure):
    data = report("opentofu")
    if failure == "receipt":
        data["receipt"] = avm.expected_receipt()
    elif failure == "copies":
        data["checks"][0]["providerCopies"] = 1
    elif failure == "missing-case":
        data["checks"].pop()
    elif failure == "upstream-lock":
        data["checks"][5]["expectedRefusal"] = False
    if failure:
        with pytest.raises(ValueError):
            avm.evidence(json.dumps(data).encode(), "opentofu")
    else:
        assert (
            avm.evidence(json.dumps(data).encode(), "opentofu")["receipt"]["engine"] == "opentofu"
        )
        compile(avm.command("opentofu")[-1], "opentofu-avm-probe", "exec")


@pytest.mark.parametrize(
    "index,field,value",
    [
        (6, "initExitCode", 1),
        (6, "validateExitCode", 1),
        (6, "validationValid", False),
        (6, "providerLinks", 5),
        (6, "providerCopies", 1),
        (6, "providersInMirror", False),
        (6, "sourceUnchanged", False),
        (7, "initExitCode", 1),
        (7, "validateExitCode", 0),
        (7, "validationValid", True),
        (7, "missingNameDiagnostic", False),
        (8, "initExitCode", 0),
        (8, "validateExitCode", 0),
        (8, "expectedRefusal", False),
    ],
)
def test_azure_root_false_success_is_refused(index, field, value):
    data = report("opentofu")
    data["checks"][index][field] = value
    with pytest.raises(ValueError, match="consumer probe"):
        avm.evidence(json.dumps(data).encode(), "opentofu")


def test_new_candidates_require_azure_probes_but_old_published_images_replay_their_evidence():
    data = report("opentofu")
    data["checks"] = data["checks"][: len(avm.CASES)]
    with pytest.raises(ValueError, match="incomplete"):
        avm.evidence(json.dumps(data).encode(), "opentofu")
    published = {**data, "opentofuVersion": data["checks"][0]["version"]}
    result = avm.evidence(json.dumps(data).encode(), "opentofu", published=published)
    assert len(result["checks"]) == len(avm.CASES)
    assert avm.cases("opentofu", published) == avm.CASES
    compile(avm.command("opentofu", published=published)[-1], "published-probe", "exec")
    published["checks"].pop()
    with pytest.raises(ValueError, match="Unrecognized"):
        avm.command("opentofu", published=published)


def test_published_expanded_mirror_requires_all_azure_probes():
    data = report("opentofu")
    published = {**data, "opentofuVersion": data["checks"][0]["version"]}
    assert avm.cases("opentofu", published) == avm.CASES + avm.AZURE_CASES
    data = {**data, "checks": data["checks"][:-1]}
    with pytest.raises(ValueError, match="incomplete"):
        avm.evidence(json.dumps(data).encode(), "opentofu", published=published)


def test_release_mirror_covers_plain_azure_roots_with_matching_policy_and_source_builds():
    manifest = avm.read(avm.CONTEXT / "dependencies.opentofu-avm-network.json")
    policy = avm.read(avm.CONTEXT / "dependencies.opentofu-avm-network.policy.json")
    providers = {item["source"]: item for item in manifest["providers"]}
    assert manifest["engine"] == policy["engine"] == "opentofu"
    for name, constraint in {
        "azurerm": "~> 4.0",
        "azuread": "~> 3.0",
        "local": "~> 2.0",
        "null": "~> 3.0",
        "time": "~> 0.14",
        "tls": "~> 4.0",
    }.items():
        source = "registry.opentofu.org/hashicorp/" + name
        provider = providers[source]
        assert satisfies(provider["version"], constraint, "provider-constraint")
        assert {"address": source, "constraint": "= " + provider["version"]} in policy["providers"]
        assert (
            provider["source_build"]["repository"]
            == "https://github.com/opentofu/terraform-provider-" + name
        )
        assert provider["source_build"]["sha256"] != "0" * 64
