"""The selection and reporting logic behind `scripts/check_samples_against_declared_core.py`.

Everything the check decides before it spawns anything: which floor a block declares, which
published core that admits, which wheels a local run substitutes, and how one pyright report
turns into a verdict per sample. `build_environment` installs, so it is left to the live runs.

The guard worth reading twice is `TestAShortReadIsRefused`. A pass that reads fewer modules
than it was pointed at reports green for the rest, which is the failure mode this whole check
exists to close — a check that covers nothing while looking like one that covers everything.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for the shared index helpers
_SCRIPT = _SCRIPTS / "check_samples_against_declared_core.py"
_spec = importlib.util.spec_from_file_location("check_samples_against_declared_core", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _sample(directory: Path, name: str, *dependencies: str) -> Path:
    """A sample directory carrying a PEP 723 block that declares ``dependencies``."""
    sample = directory / name
    sample.mkdir()
    listed = "".join(f'#     "{dependency}",\n' for dependency in dependencies)
    (sample / "agent.py").write_text(
        '"""A sample."""\n\n'
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        "# dependencies = [\n"
        f"{listed}"
        "# ]\n"
        "# ///\n",
        encoding="utf-8",
        newline="\n",
    )
    return sample


class TestTheBlockIsReadTheWayUvReadsIt:
    def test_a_bare_floor_is_the_shape_that_is_accepted(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "agent-framework-openai", "maf-sandbox>=0.25")
        assert check.core_floor(sample) == (0, 25)

    def test_a_three_part_floor_keeps_its_patch(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25.1")
        assert check.core_floor(sample) == (0, 25, 1)

    def test_a_pin_is_refused_by_the_shape_the_bump_script_edits(self, tmp_path: Path):
        """`set_dependents_range.py` rewrites a bare `>=`; anything else stops that step."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox==0.25.0")
        with pytest.raises(SystemExit, match="bare"):
            check.core_floor(sample)

    def test_a_range_is_refused_too(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25,<0.26")
        with pytest.raises(SystemExit, match="bare"):
            check.core_floor(sample)

    def test_a_block_naming_no_core_is_refused(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox-docker")
        with pytest.raises(SystemExit, match="names no maf-sandbox floor"):
            check.core_floor(sample)

    def test_a_backend_floor_does_not_answer_for_the_core(self, tmp_path: Path):
        """`maf-sandbox-docker` contains `maf-sandbox` and must not be read as it."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox-docker>=0.4", "maf-sandbox>=0.25")
        assert check.core_floor(sample) == (0, 25)

    def test_a_file_without_a_block_is_refused(self, tmp_path: Path):
        sample = tmp_path / "01_a"
        sample.mkdir()
        (sample / "agent.py").write_text('"""No block."""\n', encoding="utf-8")
        with pytest.raises(SystemExit, match="no PEP 723 block"):
            check.metadata(sample)


class TestTheRestOfTheBlockIsInstalledAsWritten:
    def test_the_core_is_dropped_and_everything_else_kept(self, tmp_path: Path):
        sample = _sample(
            tmp_path, "05_a", "agent-framework-openai", "azure-core[aio]", "maf-sandbox>=0.25"
        )
        assert check.other_requirements(sample) == ["agent-framework-openai", "azure-core[aio]"]

    def test_an_extra_survives_the_distribution_read(self):
        """The extra is part of the requirement and only the name is compared."""
        assert check.distribution("azure-core[aio]") == "azure-core"
        assert check.distribution("maf-sandbox-acas>=0.10.0") == "maf-sandbox-acas"

    def test_a_requirement_with_no_name_is_refused(self):
        with pytest.raises(SystemExit, match="no distribution name"):
            check.distribution("  ")


class TestWhichPublishedCoreIsUsed:
    """The oldest the floor admits, because the floor's claim is about its lower end."""

    def _index(self, monkeypatch: pytest.MonkeyPatch, published: list[str], yanked: tuple = ()):
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: published)
        monkeypatch.setattr(
            check,
            "fetch_requires_dist_for_version",
            lambda _, candidate: None if candidate in yanked else [],
        )

    def test_the_oldest_admitted_release_is_chosen(self, monkeypatch: pytest.MonkeyPatch):
        # Not the newest. An unpinned reader resolves to the newest and never exercises the
        # floor; a consumer capped elsewhere lands on this one, and it is what the claim covers.
        self._index(monkeypatch, ["0.26.0", "0.25.1", "0.25.0", "0.24.0"])
        assert check.lowest_admitted_core((0, 25)) == "0.25.0"

    def test_a_minor_whose_first_release_was_a_patch_still_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # `0.25.0` never existing is a thing release-please does, when a fix and a feature land
        # in one Release PR. An `==0.25` pin would find nothing here.
        self._index(monkeypatch, ["0.26.0", "0.25.1", "0.24.0"])
        assert check.lowest_admitted_core((0, 25)) == "0.25.1"

    def test_a_yanked_release_is_not_chosen(self, monkeypatch: pytest.MonkeyPatch):
        # Unpinned resolution never selects a yanked release, so a break there is not a break
        # any reader can reach — and choosing it would fail the check for nobody's benefit.
        self._index(monkeypatch, ["0.26.0", "0.25.0"], yanked=("0.25.0",))
        assert check.lowest_admitted_core((0, 25)) == "0.26.0"

    def test_a_floor_ahead_of_the_index_admits_nothing(self, monkeypatch: pytest.MonkeyPatch):
        # The release window: the floor names the version the Release PR will publish.
        self._index(monkeypatch, ["0.25.0", "0.24.0"])
        assert check.lowest_admitted_core((0, 26)) is None

    def test_a_core_that_was_never_published_is_fatal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(check, "fetch_published_versions", lambda _: None)
        with pytest.raises(SystemExit, match="never been published"):
            check.lowest_admitted_core((0, 25))


class TestTheLocalRunSubstitutesEveryWheel:
    """The escape swaps the whole family, not just the core.

    A published backend cannot implement a protocol it predates. Measured on this repository:
    with only the core swapped, thirteen samples failed with `list[DockerSandboxBackend] cannot
    be assigned to Sequence[SandboxBackend]` — a *sample* error for a packaging cause, which is
    a diagnosis nobody can act on.
    """

    def _wheel(self, directory: Path, name: str) -> Path:
        path = directory / name
        path.write_bytes(b"")
        return path

    def test_the_core_and_its_siblings_are_named(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        self._wheel(tmp_path, "maf_sandbox_docker-0.9.0-py3-none-any.whl")
        self._wheel(tmp_path, "maf_sandbox_acas-0.14.0-py3-none-any.whl")
        assert [name for name, _ in check.built_here(core)] == [
            "maf-sandbox",
            "maf-sandbox-acas",
            "maf-sandbox-docker",
        ]

    def test_the_core_comes_first_and_is_the_wheel_it_was_given(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        self._wheel(tmp_path, "maf_sandbox_wslc-0.12.0-py3-none-any.whl")
        assert check.built_here(core)[0] == ("maf-sandbox", core)

    def test_a_core_standing_alone_still_answers(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        assert check.built_here(core) == [("maf-sandbox", core)]


def _report(diagnostics: list[dict], analysed: int) -> str:
    return json.dumps({"generalDiagnostics": diagnostics, "summary": {"filesAnalyzed": analysed}})


def _diagnostic(path: Path, severity: str = "error", rule: str = "reportAttributeAccessIssue"):
    return {
        "file": str(path),
        "severity": severity,
        "rule": rule,
        "message": 'Cannot access attribute "disposed" for class "int"\n  more detail',
    }


class TestOneReportBecomesAVerdictPerSample:
    def test_a_clean_sample_is_a_key_with_no_errors(self, tmp_path: Path):
        """A sample that passed and a sample nothing read look identical in a diagnostics list.

        So the keys come from what was asked for. Without that, a run whose paths were wrong
        reports every sample clean, which is the shape of failure this check exists to prevent.
        """
        first, second = tmp_path / "01_a", tmp_path / "02_b"
        first.mkdir()
        second.mkdir()
        report = json.loads(_report([_diagnostic(first / "agent.py")], 2))
        found = check.errors_by_sample(report, [first, second])
        assert set(found) == {"01_a", "02_b"}
        assert len(found["01_a"]) == 1 and found["02_b"] == []

    def test_only_the_first_line_of_a_message_is_kept(self, tmp_path: Path):
        sample = tmp_path / "01_a"
        sample.mkdir()
        report = json.loads(_report([_diagnostic(sample / "agent.py")], 1))
        (line,) = check.errors_by_sample(report, [sample])["01_a"]
        assert line == (
            'agent.py: reportAttributeAccessIssue: Cannot access attribute "disposed" for '
            'class "int"'
        )

    def test_a_warning_is_not_a_failure(self, tmp_path: Path):
        """The question is whether the sample works, not whether it is tidy."""
        sample = tmp_path / "01_a"
        sample.mkdir()
        report = json.loads(_report([_diagnostic(sample / "agent.py", severity="warning")], 1))
        assert check.errors_by_sample(report, [sample])["01_a"] == []

    def test_a_diagnostic_outside_the_samples_belongs_to_none_of_them(self, tmp_path: Path):
        sample = tmp_path / "01_a"
        sample.mkdir()
        report = json.loads(_report([_diagnostic(tmp_path / "elsewhere.py")], 1))
        assert check.errors_by_sample(report, [sample])["01_a"] == []

    def test_a_diagnostic_with_no_rule_still_reports(self, tmp_path: Path):
        sample = tmp_path / "01_a"
        sample.mkdir()
        raw = _diagnostic(sample / "agent.py")
        del raw["rule"]
        (line,) = check.errors_by_sample(json.loads(_report([raw], 1)), [sample])["01_a"]
        assert line.startswith("agent.py: error: ")


class TestAShortReadIsRefused:
    """The pass has to prove it read what it was pointed at before its silence counts."""

    def _pyright(self, monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 1):
        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode, stdout, "")

        monkeypatch.setattr(check.subprocess, "run", fake)

    def _sample_with(self, directory: Path, modules: int) -> Path:
        sample = directory / "01_a"
        sample.mkdir()
        for index in range(modules):
            (sample / f"module_{index}.py").write_text("", encoding="utf-8")
        return sample

    def test_a_run_that_read_fewer_modules_than_it_was_given_is_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        sample = self._sample_with(tmp_path, 3)
        self._pyright(monkeypatch, _report([], 2))
        with pytest.raises(SystemExit, match="read 2 of the 3 modules"):
            check.type_check([sample], tmp_path / "python", tmp_path / "cfg.json")

    def test_a_run_that_read_everything_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        sample = self._sample_with(tmp_path, 3)
        self._pyright(monkeypatch, _report([], 3))
        assert check.type_check([sample], tmp_path / "python", tmp_path / "cfg.json") == {
            "01_a": []
        }

    def test_pyright_producing_no_output_is_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Its exit code says nothing — it is non-zero *because* it found something."""
        sample = self._sample_with(tmp_path, 1)
        self._pyright(monkeypatch, "")
        with pytest.raises(SystemExit, match="produced no output"):
            check.type_check([sample], tmp_path / "python", tmp_path / "cfg.json")

    def test_a_nested_module_counts_towards_what_must_be_read(self, tmp_path: Path):
        sample = self._sample_with(tmp_path, 2)
        (sample / "inner").mkdir()
        (sample / "inner" / "deep.py").write_text("", encoding="utf-8")
        assert check.expected_files([sample]) == 3


class TestTheConfigKeepsTheRepositoryOut:
    def test_every_sample_becomes_its_own_execution_environment(self, tmp_path: Path):
        """Without a root per sample, `from _scaffold import …` fails for the wrong reason."""
        first, second = tmp_path / "01_a", tmp_path / "02_b"
        first.mkdir()
        second.mkdir()
        written = json.loads(check.write_config(tmp_path, [first, second]).read_text("utf-8"))
        assert [env["root"] for env in written["executionEnvironments"]] == [
            first.as_posix(),
            second.as_posix(),
        ]

    def test_the_mode_is_stated_rather_than_left_to_a_default(self, tmp_path: Path):
        """pyright's default has moved before; the repository's own setting is `standard`."""
        written = json.loads(check.write_config(tmp_path, []).read_text("utf-8"))
        assert written["typeCheckingMode"] == "standard"

    def test_it_is_written_outside_the_repository(self, tmp_path: Path):
        # `-p` makes the config's directory the project root, which is what keeps the root
        # `[tool.pyright]` — and its `include` of scripts/, tests/ and packages/ — out of a
        # pass that has only the samples' dependencies installed.
        assert check.write_config(tmp_path, []).parent == tmp_path


class TestTheSelection:
    def test_no_argument_takes_every_sample(self):
        every = check.selected([])
        assert isinstance(every, list) and len(every) >= 10

    def test_a_named_sample_is_the_only_one_taken(self):
        every = check.selected([])
        assert isinstance(every, list)
        name = every[0].name
        assert check.selected([name]) == [every[0]]

    def test_a_path_is_accepted_where_a_name_is(self):
        """`samples/12_purge_lifecycle` is what a shell completes to."""
        every = check.selected([])
        assert isinstance(every, list)
        assert check.selected([f"samples/{every[0].name}"]) == [every[0]]

    def test_an_unknown_name_is_refused_rather_than_silently_skipped(self):
        assert check.selected(["99_not_a_sample"]) == "no such sample: 99_not_a_sample"


class TestTheCommandLine:
    def test_local_core_with_no_wheel_after_it_is_a_usage_error(self):
        assert check.main(["prog", "--local-core"]) == 2

    def test_a_local_core_that_is_not_a_file_is_a_usage_error(self, tmp_path: Path):
        assert check.main(["prog", "--local-core", str(tmp_path / "absent.whl")]) == 2

    def test_an_unknown_sample_is_a_usage_error(self):
        assert check.main(["prog", "99_not_a_sample"]) == 2


class TestItCoversTheRealTree:
    """The sibling of every guard above: that they are asked about something.

    Each pure reader is run over the samples this repository actually ships, so a block shape
    the parsers cannot handle fails here rather than in CI at the moment the check first runs.
    """

    def test_every_sample_declares_a_floor_this_can_read(self):
        samples = check.sample_directories()
        assert len(samples) >= 10, f"found {len(samples)} sample directories"
        for sample in samples:
            assert check.core_floor(sample) >= (0,)

    def test_every_sample_declares_something_besides_the_core(self):
        for sample in check.sample_directories():
            declared = check.metadata(sample)["dependencies"]
            assert len(check.other_requirements(sample)) == len(declared) - 1

    def test_the_modules_the_pass_must_read_are_counted(self):
        samples = check.sample_directories()
        assert check.expected_files(samples) >= len(samples)
