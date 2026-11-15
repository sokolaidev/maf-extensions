"""Core-range conflicts must remain visible when installation overrides the core."""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_core_range_overlap as check  # noqa: E402

pytestmark = pytest.mark.workflow


def _wheel(
    directory: Path,
    name: str,
    *requirements: str,
    version: str = "0.1.0",
) -> Path:
    distribution = name.replace("-", "_")
    path = directory / f"{distribution}-{version}-py3-none-any.whl"
    info = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
        )
        archive.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{info}/RECORD", "")
    return path


@pytest.mark.parametrize(
    ("requirements", "overlap"),
    [
        ([">=0.53,<0.54", ">=0.54,<0.55"], False),
        ([">=0.54,<0.55", ">=0.53,<0.54"], False),
        ([">=0.54,<0.54"], False),
        ([">=0.53,<0.55", ">=0.54,<0.56"], True),
        ([">=0.53,<=0.54", ">=0.54,<0.55"], True),
        ([">0.54,<0.55", "==0.54"], False),
        (["==0.54.0", "==0.54"], True),
        (["~=0.54.1", ">=0.55"], False),
        (["==0.54.*", "!=0.54.*"], False),
        (["==0.54", "!=0.54"], False),
        (["==0.54.2", ">=0.54,!=0.54.1,<0.55"], True),
        ([">=0.54rc1,<0.54rc3", "==0.54rc2"], True),
        ([">=0.54rc1,<0.54rc3", ">=0.54"], False),
        ([">=1!0.54", "<1!0.54"], False),
        (["", ">=0.54,<0.55"], True),
        (
            [">=0.54,<0.57,!=0.54.*", ">=0.54,<0.57,!=0.55.*", ">=0.54,<0.57,!=0.56.*"],
            False,
        ),
    ],
)
def test_exact_intersection(requirements: list[str], overlap: bool):
    ranges = {
        str(index): SpecifierSet(requirement) for index, requirement in enumerate(requirements)
    }
    assert check.intersection(ranges).is_unsatisfiable() is not overlap


def test_every_wheel_is_discovered_by_its_core_requirement(tmp_path: Path):
    _wheel(tmp_path, "maf-sandbox", version="0.1.0")
    _wheel(tmp_path, "unrelated", "maf-sandbox-other>=3", "requests")
    _wheel(tmp_path, "future-adapter", "MAF_sandbox[maf] >=99.0,<100")
    _wheel(tmp_path, "maf-sandbox-next", "maf-sandbox>=99.2", "maf-sandbox<99.3")
    ranges = check.declared_core_ranges(tmp_path)
    assert set(ranges) == {"future-adapter", "maf-sandbox-next"}
    assert "99.2" in check.intersection(ranges)
    assert "99.3" not in check.intersection(ranges)


def test_inactive_markers_and_unrequested_extras_do_not_conflict(tmp_path: Path):
    _wheel(
        tmp_path,
        "maf-sandbox-a",
        'maf-sandbox>=0.54,<0.55; python_version >= "3.12"',
        'maf-sandbox<0.54; python_version < "3.12"',
        'maf-sandbox<0.54; extra == "legacy"',
    )
    _wheel(tmp_path, "maf-sandbox-b", "maf-sandbox>=0.54,<0.55")
    assert check.main(["--dist-dir", str(tmp_path)]) == 0


def test_conflict_names_each_dependent_and_its_ranges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=0.53,<0.54")
    _wheel(tmp_path, "maf-sandbox-b", "maf-sandbox>=0.54,<0.55")
    assert check.main(["--dist-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "::error::No maf-sandbox version satisfies" in output.err
    assert "maf-sandbox-a: maf-sandbox<0.54,>=0.53" in output.err
    assert "maf-sandbox-b: maf-sandbox<0.55,>=0.54" in output.err
    assert "Move the dependent ranges together" in output.err


def test_future_ranges_do_not_need_a_published_or_matching_checkout_core(tmp_path: Path):
    _wheel(tmp_path, "maf-sandbox", version="0.53.0")
    _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=999.0,<1000")
    _wheel(tmp_path, "maf-sandbox-b", "maf-sandbox>=999.1,<1000")
    assert check.main(["--dist-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("problem", ["empty", "no-core", "duplicate", "metadata", "url", "invalid"])
def test_uncheckable_artifacts_fail(
    tmp_path: Path, problem: str, capsys: pytest.CaptureFixture[str]
):
    if problem == "no-core":
        _wheel(tmp_path, "unrelated", "requests")
    elif problem == "duplicate":
        _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=0.54", version="0.1.0")
        _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=0.54", version="0.2.0")
    elif problem == "metadata":
        with zipfile.ZipFile(tmp_path / "maf_sandbox_a-0.1.0-py3-none-any.whl", "w") as archive:
            archive.writestr("placeholder", "")
    elif problem == "url":
        _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox @ https://example.com/core.whl")
    elif problem == "invalid":
        _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=invalid")
    assert check.main(["--dist-dir", str(tmp_path)]) == 2
    assert "::error::" in capsys.readouterr().err


def test_real_local_core_override_can_install_the_split_this_check_refuses(tmp_path: Path):
    core = _wheel(tmp_path, "maf-sandbox", version="0.53.0")
    first = _wheel(tmp_path, "maf-sandbox-a", "maf-sandbox>=0.53,<0.54")
    second = _wheel(tmp_path, "maf-sandbox-b", "maf-sandbox>=0.54,<0.55")
    environment = tmp_path / "venv"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    override = tmp_path / "override.txt"
    override.write_text(f"maf-sandbox @ {core.as_uri()}\n", encoding="utf-8")
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-index",
            "--overrides",
            str(override),
            str(core),
            str(first),
            str(second),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert check.main(["--dist-dir", str(tmp_path)]) == 1


def test_repository_declared_core_ranges_overlap(tmp_path: Path):
    for metadata in sorted((_ROOT / "packages").glob("*/pyproject.toml")):
        project = tomllib.loads(metadata.read_text(encoding="utf-8"))["project"]
        _wheel(
            tmp_path, project["name"], *project.get("dependencies", []), version=project["version"]
        )
    assert check.main(["--dist-dir", str(tmp_path)]) == 0


def test_pr_gate_checks_built_ranges_before_applying_the_override():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["packaging"]["steps"]
    names = [step.get("name", "") for step in steps]
    range_step = steps[names.index("The dependent core ranges overlap")]
    assert range_step["run"] == "uv run python scripts/check_core_range_overlap.py"
    assert range_step["if"] == "needs.changes.outputs.code == 'true'"
    assert not range_step.get("continue-on-error")
    assert (
        names.index("Build + metadata check")
        < names.index("The dependent core ranges overlap")
        < names.index("Pin the cores this checkout built")
        < names.index("The suite installs as a set")
    )
    install_step = steps[names.index("The suite installs as a set")]
    assert "check_suite_installs_together.py --whole-set --local-core" in install_step["run"]
