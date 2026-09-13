"""What `scripts/sample_source_args.py` hands `uv run`, and why each answer is the safe one.

The script decides where a live sample's libraries come from. Getting it wrong is quiet in both
directions: injecting nothing under `branch` runs the published wheels while the job reports it
tested the branch, and injecting a package a sample never named builds a set no consumer has.
So the cases here are the two silent ones plus the shape of what it prints.

The published mode is asserted to print *nothing* rather than something empty-ish, because the
workflow word-splits the output straight onto the command line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))  # the shared PEP 723 reader lives beside the checks

import sample_blocks  # noqa: E402
import sample_source_args  # noqa: E402

_SCRIPT = _ROOT / "scripts" / "sample_source_args.py"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    """The script as the workflow runs it, through its own entry point."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
    )


def write_sample(directory: Path, dependencies: list[str]) -> Path:
    """A sample whose PEP 723 block names ``dependencies`` and nothing else."""
    directory.mkdir(parents=True, exist_ok=True)
    listed = "\n".join(f'#     "{entry}",' for entry in dependencies)
    (directory / "agent.py").write_text(
        f'# /// script\n# requires-python = ">=3.12"\n# dependencies = [\n{listed}\n# ]\n# ///\n',
        encoding="utf-8",
        newline="\n",
    )
    return directory


class TestPublished:
    """The default, and what every release verification runs."""

    def test_it_prints_nothing(self):
        result = run("samples/05_docker_bicep")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    def test_the_default_is_published(self):
        assert (
            run("samples/05_docker_bicep").stdout
            == run("samples/05_docker_bicep", "--source", "published").stdout
        )

    def test_a_sample_naming_no_package_is_still_fine(self, tmp_path: Path):
        """Refusing here would be refusing the ordinary case: published injects nothing."""
        sample = write_sample(tmp_path / "90_nothing", ["httpx"])
        assert sample_source_args.arguments(sample / "agent.py", "published") == []


class TestBranch:
    """The mode that makes a live job test this checkout rather than the index."""

    def test_it_names_each_declared_package_once(self):
        result = run("samples/05_docker_bicep", "--source", "branch")
        assert result.returncode == 0, result.stderr
        words = result.stdout.split()
        assert words.count("--with") == 3
        assert set(words) - {"--with"} == {
            "./packages/maf-sandbox-bicep",
            "./packages/maf-sandbox-docker",
            "./packages/maf-sandbox",
        }

    def test_every_path_it_prints_exists(self):
        for sample in sample_blocks.sample_directories():
            for word in sample_source_args.arguments(sample / "agent.py", "branch"):
                if word != "--with":
                    assert (_ROOT / word).is_dir(), f"{sample.name} names a missing {word}"

    def test_it_never_names_a_package_the_sample_does_not(self):
        """The sample's own block is the list; nothing here may widen it."""
        for sample in sample_blocks.sample_directories():
            declared = set(sample_source_args.declared_packages(sample / "agent.py"))
            block = sample_blocks.declared(sample / "agent.py") or {}
            named = {sample_blocks.distribution(entry) for entry in block.get("dependencies", [])}
            assert declared <= named, sample.name

    def test_every_real_sample_has_something_to_inject(self):
        """If one did not, the refusal below would fire in the workflow rather than in a test."""
        for sample in sample_blocks.sample_directories():
            assert sample_source_args.declared_packages(sample / "agent.py"), sample.name

    def test_a_sample_naming_no_package_is_refused(self, tmp_path: Path):
        """Printing nothing here would run the published wheels under the branch job's name."""
        sample = write_sample(tmp_path / "91_nothing", ["httpx"])
        result = run(str(sample), "--source", "branch")
        assert result.returncode == 1
        assert "nothing to run from the branch" in result.stderr

    def test_the_flags_come_in_pairs(self):
        words = sample_source_args.arguments(
            _ROOT / "samples/15_acas_codeact_host_tools/agent.py", "branch"
        )
        assert words[::2] == ["--with"] * (len(words) // 2)
        assert len(words) == 8


class TestRefusals:
    def test_a_missing_sample_is_named(self):
        result = run("samples/99_not_a_sample", "--source", "branch")
        assert result.returncode == 1
        assert "no sample at" in result.stderr

    def test_an_unknown_source_is_refused(self):
        result = run("samples/05_docker_bicep", "--source", "somewhere-else")
        assert result.returncode == 2

    @pytest.mark.parametrize("source", ["published", "branch"])
    def test_the_modes_the_workflow_passes_are_accepted(self, source: str):
        assert run("samples/05_docker_bicep", "--source", source).returncode == 0
