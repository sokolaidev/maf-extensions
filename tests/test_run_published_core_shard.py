"""Exercise compatibility partitioning and failure propagation through the real CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "packages").mkdir()
    (tmp_path / "dist").mkdir()
    shutil.copy(ROOT / "scripts" / "run_published_core_shard.py", tmp_path / "scripts")
    (tmp_path / "scripts" / "check_dependent_works_with_published_cores.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "with open('checked.jsonl', 'a', encoding='utf-8') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(7 if Path('fail').exists() and sys.argv[1] == 'maf-sandbox-a' else 0)\n",
        encoding="utf-8",
    )
    for name in (
        "maf-sandbox-z-new",
        "maf-sandbox-c",
        "maf-sandbox",
        "maf-sandbox-b",
        "maf-sandbox-a",
    ):
        _package(tmp_path, name, "maf-sandbox>=1.0.0,<1.1" if name != "maf-sandbox" else None)
    # A package of the workspace that is no dependent: it names no maf-sandbox requirement, so
    # the shard has no core range to check it against and must leave it out.
    _package(tmp_path, "maf-aside", "agent-framework-core>=1.0.0,<1.1")
    (tmp_path / "packages" / "README.md").touch()
    return tmp_path


def _package(checkout: Path, name: str, requirement: str | None) -> None:
    (checkout / "packages" / name).mkdir()
    dependencies = f'["{requirement}"]' if requirement else "[]"
    (checkout / "packages" / name / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = {dependencies}\n',
        encoding="utf-8",
    )
    (checkout / "dist" / f"{name.replace('-', '_')}-1.0.0-py3-none-any.whl").touch()


def run_shard(checkout: Path, shard: int, shards: int = 2) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts" / "run_published_core_shard.py"),
            "--shard",
            str(shard),
            "--shards",
            str(shards),
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )


def checked(checkout: Path) -> list[list[str]]:
    return [json.loads(line) for line in (checkout / "checked.jsonl").read_text().splitlines()]


def test_a_package_without_a_core_requirement_is_not_a_dependent(checkout: Path):
    for shard in (0, 1):
        result = run_shard(checkout, shard)
        assert result.returncode == 0, result.stderr
    assert "maf-aside" not in {call[0] for call in checked(checkout)}


@pytest.mark.parametrize(
    "requirement",
    ["maf-sandbox~=1.0", "maf-sandbox!=1.0.1", "maf_sandbox>=1.0", "Maf.Sandbox[x] >=1.0"],
)
def test_a_core_requirement_in_any_pep_508_shape_is_a_dependent(checkout: Path, requirement: str):
    _package(checkout, "maf-other", requirement)
    for shard in (0, 1):
        assert run_shard(checkout, shard).returncode == 0
    assert "maf-other" in {call[0] for call in checked(checkout)}


def test_every_dependent_runs_once_with_its_wheel_and_local_core(checkout: Path):
    first = run_shard(checkout, 0)
    assert first.returncode == 0, first.stderr
    assert [call[0] for call in checked(checkout)] == ["maf-sandbox-a", "maf-sandbox-c"]
    second = run_shard(checkout, 1)
    assert second.returncode == 0, second.stderr
    calls = checked(checkout)
    assert [call[0] for call in calls] == [
        "maf-sandbox-a",
        "maf-sandbox-c",
        "maf-sandbox-b",
        "maf-sandbox-z-new",
    ]
    for distribution, wheel, flag, core in calls:
        assert (
            Path(wheel)
            == checkout / "dist" / f"{distribution.replace('-', '_')}-1.0.0-py3-none-any.whl"
        )
        assert flag == "--local-core"
        assert Path(core) == checkout / "dist" / "maf_sandbox-1.0.0-py3-none-any.whl"


def test_checker_failure_is_not_hidden_by_a_later_success(checkout: Path):
    (checkout / "fail").touch()
    result = run_shard(checkout, 0)
    assert result.returncode == 7
    assert [call[0] for call in checked(checkout)] == ["maf-sandbox-a"]


@pytest.mark.parametrize(("shard", "shards"), [(-1, 2), (2, 2), (0, 0), (0, -1), (4, 5)])
def test_invalid_or_empty_shards_fail_before_checking(checkout: Path, shard: int, shards: int):
    result = run_shard(checkout, shard, shards)
    assert result.returncode == 2
    assert not (checkout / "checked.jsonl").exists()


@pytest.mark.parametrize("distribution", ["maf-sandbox", "maf-sandbox-c"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_missing_or_ambiguous_wheels_fail_before_checking(
    checkout: Path, distribution: str, ambiguous: bool
):
    stem = distribution.replace("-", "_")
    if ambiguous:
        (checkout / "dist" / f"{stem}-2.0.0-py3-none-any.whl").touch()
    else:
        (checkout / "dist" / f"{stem}-1.0.0-py3-none-any.whl").unlink()
    result = run_shard(checkout, 0)
    assert result.returncode == 2
    assert f"expected one wheel for {distribution}" in result.stderr
    assert not (checkout / "checked.jsonl").exists()
