"""Core release transitions defer image packaging while retaining Linux validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_hyperlight_image_compatibility as compatibility  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.fixture
def metadata(tmp_path):
    def write(core="0.43.0", requirements=None):
        projects = {"maf-sandbox": {"version": core}}
        for name in compatibility.DEPENDENTS:
            projects[name] = {
                "dependencies": (requirements or {}).get(name, ["maf-sandbox>=0.42.0,<0.43"])
            }
        for name, fields in projects.items():
            path = tmp_path / "packages" / name / "pyproject.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[project]\n"
                + "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items()),
                encoding="utf-8",
            )
        return tmp_path

    return write


@pytest.mark.parametrize("core", ["0.42.0", "0.42.9"])
def test_compatible_workspace_builds_image(metadata, core):
    assert compatibility.pending_adoptions(metadata(core)) == []


@pytest.mark.parametrize("core", ["0.43.0", "0.43.2"])
def test_core_release_waits_for_both_dependents(metadata, core):
    pending = compatibility.pending_adoptions(metadata(core))
    assert len(pending) == 2
    for name, reason in zip(compatibility.DEPENDENTS, pending, strict=True):
        assert name in reason
        assert "maf-sandbox>=0.42.0,<0.43" in reason
        assert core in reason


def test_adoption_resumes_image_only_when_both_ranges_admit_core(metadata):
    ranges = {"maf-sandbox-hyperlight": ["maf-sandbox>=0.43.0,<0.44"]}
    pending = compatibility.pending_adoptions(metadata(requirements=ranges))
    assert len(pending) == 1
    assert "maf-sandbox-codeact" in pending[0]
    ranges["maf-sandbox-codeact"] = ["maf-sandbox>=0.43.0,<0.44"]
    assert compatibility.pending_adoptions(metadata(requirements=ranges)) == []


@pytest.mark.parametrize("ceiling", ["0.43", "0.43.0", "0.43.0.0"])
def test_equivalent_ceiling_spellings_defer_the_same_release(metadata, ceiling):
    ranges = {name: [f"maf-sandbox>=0.42.0,<{ceiling}"] for name in compatibility.DEPENDENTS}
    assert len(compatibility.pending_adoptions(metadata(requirements=ranges))) == 2


@pytest.mark.parametrize("name", compatibility.DEPENDENTS)
@pytest.mark.parametrize(
    "requirements",
    [
        [],
        ["maf-sandbox>=0.43.0,<0.43"],
        ["maf-sandbox>=0.44.0,<0.43"],
        ["maf-sandbox>=0.44.0,<0.45"],
        ["maf-sandbox>=0.41.0,<0.42"],
        ["maf-sandbox>=0.42"],
        ["maf-sandbox>=0.42.0,<0.43; python_version < '3.13'"],
        ["maf_sandbox>=0.42.0,<0.43"],
        ["maf-sandbox>=0.42.0,<0.43", "maf-sandbox>=0.43.0,<0.44"],
    ],
)
def test_invalid_or_unexpected_mismatch_fails_instead_of_deferring(metadata, name, requirements):
    with pytest.raises(ValueError, match=name):
        compatibility.pending_adoptions(metadata(requirements={name: requirements}))


@pytest.mark.parametrize("core", ["0.43.0rc1", "0.43", "invalid"])
def test_unsupported_core_versions_fail(metadata, core):
    with pytest.raises(ValueError, match="core release version"):
        compatibility.pending_adoptions(metadata(core))


@pytest.mark.parametrize("core,build", [("0.42.0", "true"), ("0.43.0", "false")])
def test_preflight_reports_its_decision_without_claiming_image_success(
    metadata, tmp_path, monkeypatch, capsys, core, build
):
    monkeypatch.setattr(compatibility, "ROOT", metadata(core))
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier checks\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    compatibility.main()
    captured = capsys.readouterr()
    assert captured.out == f"build={build}\n"
    assert summary.read_text("utf-8") == "Earlier checks\n" + captured.err.rstrip("\n") + "\n"
    if build == "false":
        assert "No image was built or verified" in captured.err
        assert "Linux worker and KVM checks remain enabled" in captured.err
        for name in compatibility.DEPENDENTS:
            assert name in captured.err


def test_preflight_failure_emits_no_build_decision(metadata, monkeypatch, capsys):
    root = metadata(requirements={"maf-sandbox-codeact": ["maf-sandbox>=0.43.0,<0.43"]})
    monkeypatch.setattr(compatibility, "ROOT", root)
    with pytest.raises(ValueError):
        compatibility.main()
    assert not capsys.readouterr().out


def test_ci_only_gates_image_and_its_success_artifact():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text("utf-8"))
    steps = workflow["jobs"]["hyperlight-linux-worker"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "hyperlight-image")
    assert "scripts/check_hyperlight_image_compatibility.py" in preflight["run"]
    assert '>> "$GITHUB_OUTPUT"' in preflight["run"]
    assert preflight["if"] == "needs.changes.outputs.code == 'true'"
    gated = []
    for step in steps:
        if "steps.hyperlight-image.outputs.build" in step.get("if", ""):
            assert steps.index(step) > steps.index(preflight)
            assert step["if"] == (
                "needs.changes.outputs.code == 'true' && steps.hyperlight-image.outputs.build == 'true'"
            )
            gated.append(step["name"])
        if "check_hyperlight_linux.py" in step.get("run", ""):
            assert step["if"] == "needs.changes.outputs.code == 'true'"
            assert not step.get("continue-on-error")
    assert gated == [
        "Build and verify the Hyperlight runtime image",
        "Retain Hyperlight image verification records",
    ]
    build = next(step for step in steps if step.get("name") == gated[0])
    assert "scripts/build_hyperlight_aks_image.py --require-clean" in build["run"]
    assert not build.get("continue-on-error")
