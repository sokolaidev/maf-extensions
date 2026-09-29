"""Exercise complete, non-overlapping CI groups through real pytest workers."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.workflow


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests packages extra_tests\njunit_family=xunit1\n"
    )
    for file in (
        "tests/test_repository.py",
        "packages/future/tests/test_future.py",
        "extra_tests/test_new_root.py",
    ):
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import pytest\n"
            "@pytest.mark.parametrize('value', [1, 2, 3])\n"
            "def test_values(value):\n"
            "    assert value > 0\n",
            encoding="utf-8",
        )
    return tmp_path


def run_suite(suite: Path, report: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.offline_test_shards",
            "-q",
            "--dist",
            "worksteal",
            "--junitxml",
            str(suite / report),
            *args,
        ],
        cwd=suite,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def cases(path: Path) -> Counter[tuple[str | None, str | None]]:
    return Counter(
        (case.get("classname"), case.get("name")) for case in ET.parse(path).iter("testcase")
    )


def test_real_xdist_groups_cover_the_full_collection_without_overlap(suite: Path):
    baseline = run_suite(suite, "all.xml")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    for group in ("repository", "packages"):
        result = run_suite(suite, f"{group}.xml", "-n", "2", "--offline-group", group)
        assert result.returncode == 0, result.stdout + result.stderr
    repository, packages = cases(suite / "repository.xml"), cases(suite / "packages.xml")
    assert sum(repository.values()) == 6
    assert sum(packages.values()) == 3
    assert not repository & packages
    assert repository + packages == cases(suite / "all.xml")


def test_invalid_group_fails_before_running_tests(suite: Path):
    result = run_suite(suite, "invalid.xml", "--offline-group", "typo")
    assert result.returncode == 4
    assert "invalid choice" in result.stderr


@pytest.mark.parametrize("group", ["repository", "packages"])
def test_failed_tests_still_fail_their_group(suite: Path, group: str):
    parent = suite / ("tests" if group == "repository" else "packages/future/tests")
    (parent / "test_failure.py").write_text("def test_failure():\n    assert False\n")
    result = run_suite(suite, "failed.xml", "-n", "2", "--offline-group", group)
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(list(ET.parse(suite / "failed.xml").iter("failure"))) == 1


def test_collection_errors_are_not_hidden_by_group_selection(suite: Path):
    (suite / "tests/test_broken.py").write_text("raise RuntimeError('broken collection')\n")
    result = run_suite(suite, "broken.xml", "--offline-group", "packages")
    assert result.returncode != 0
    assert "broken collection" in result.stdout
