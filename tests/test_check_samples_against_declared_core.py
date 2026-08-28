"""The logic behind `scripts/check_samples_against_declared_core.py`.

Everything the check decides around the two subprocesses it spawns: which floor a block
declares, which published core that admits, what the environment is asked to install, how one
pyright report becomes a verdict per sample, and what the verdict loop counts. Only
`build_environment` and `type_check`'s own `uv` calls are left to the live runs.

Two guards carry more than their size. `TestAShortReadIsRefused` holds that a pass has to prove
it read what it was pointed at, and `TestTheVerdictLoop` holds that a failing sample is counted
— between them they are what stops this check reporting green over samples it never examined.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # the script imports its siblings for the shared helpers
_SCRIPT = _SCRIPTS / "check_samples_against_declared_core.py"
_spec = importlib.util.spec_from_file_location("check_samples_against_declared_core", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _sample(directory: Path, name: str, *dependencies: str, python: str = ">=3.12") -> Path:
    """A sample directory carrying a PEP 723 block that declares ``dependencies``."""
    sample = directory / name
    sample.mkdir()
    listed = "".join(f'#     "{dependency}",\n' for dependency in dependencies)
    (sample / "agent.py").write_text(
        '"""A sample."""\n\n'
        "# /// script\n"
        f'# requires-python = "{python}"\n'
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

    def test_a_block_that_declares_only_the_core_asks_for_nothing_else(self, tmp_path: Path):
        """The shape that breaks an install built from the requirements alone."""
        sample = _sample(tmp_path, "10_a", "maf-sandbox>=0.25")
        assert check.other_requirements(sample) == []

    def test_an_extra_survives_the_distribution_read(self):
        """The extra is part of the requirement and only the name is compared."""
        assert check.named_distribution("azure-core[aio]") == "azure-core"
        assert check.named_distribution("maf-sandbox-acas>=0.10.0") == "maf-sandbox-acas"

    def test_a_requirement_with_no_name_is_refused(self):
        with pytest.raises(SystemExit, match="no distribution name"):
            check.named_distribution("  ")


class TestTheCoreIsRequestedAndNotOnlyOverridden:
    """An override rewrites a requirement; it never adds one.

    So the core has to appear as an operand in both modes. Left out of the local one, a block
    declaring only the core asks `uv pip install` for nothing at all, and a block declaring a
    backend gets the core only because the backend happens to require it.
    """

    def _wheel(self, directory: Path, name: str) -> Path:
        path = directory / name
        path.write_bytes(b"")
        return path

    def test_a_published_core_is_pinned_as_an_operand(self, tmp_path: Path):
        arguments = check.install_arguments(tmp_path, ["maf-sandbox-docker"], "0.25.0")
        assert arguments == ["maf-sandbox==0.25.0", "maf-sandbox-docker"]

    def test_a_local_core_is_named_as_well_as_overridden(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        arguments = check.install_arguments(tmp_path, ["maf-sandbox-docker"], core)
        assert arguments[0] == "--overrides"
        assert "maf-sandbox" in arguments[2:], arguments

    def test_a_block_of_only_the_core_still_asks_for_a_package(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        arguments = check.install_arguments(tmp_path, [], core)
        operands = [word for word in arguments if not word.startswith("-")][1:]
        assert operands == ["maf-sandbox"], arguments

    def test_the_override_file_names_the_core_and_every_sibling(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        self._wheel(tmp_path, "maf_sandbox_docker-0.9.0-py3-none-any.whl")
        check.install_arguments(tmp_path, [], core)
        written = (tmp_path / "override.txt").read_text(encoding="utf-8")
        assert "maf-sandbox @ " in written and "maf-sandbox-docker @ " in written


class TestWhichPublishedCoreIsUsed:
    """The oldest the floor admits, because the floor's claim is about its lower end."""

    def _index(self, monkeypatch: pytest.MonkeyPatch, published: list[str], yanked: tuple = ()):
        self.fetched: list[str] = []

        def per_version(_distribution: str, candidate: str):
            self.fetched.append(candidate)
            return None if candidate in yanked else []

        monkeypatch.setattr(check, "fetch_published_versions", lambda _: published)
        monkeypatch.setattr(check, "fetch_requires_dist_for_version", per_version)

    def test_the_oldest_admitted_release_is_chosen(self, monkeypatch: pytest.MonkeyPatch):
        # Not the newest. An unpinned reader resolves to the newest and never exercises the
        # floor; a consumer capped elsewhere lands on this one, and it is what the claim covers.
        self._index(monkeypatch, ["0.26.0", "0.25.1", "0.25.0", "0.24.0"])
        assert check.lowest_admitted_core((0, 25)) == "0.25.0"

    def test_only_candidates_up_to_the_answer_are_fetched(self, monkeypatch: pytest.MonkeyPatch):
        """One request in the common case, not one per release above the floor."""
        self._index(monkeypatch, ["0.26.0", "0.25.1", "0.25.0", "0.24.0"])
        check.lowest_admitted_core((0, 25))
        assert self.fetched == ["0.25.0"]

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


class TestTheLocalCoreArgument:
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

    def test_a_core_standing_alone_still_answers(self, tmp_path: Path):
        core = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        assert check.built_here(core) == [("maf-sandbox", core)]

    def test_a_backend_wheel_is_refused(self, tmp_path: Path):
        """`sibling_wheels` globs `maf_sandbox_*`, which never matches the core.

        So a backend wheel here substitutes every sibling and leaves the core resolving from
        the index, while the run still reports itself as checked against this checkout.
        """
        wheel = self._wheel(tmp_path, "maf_sandbox_docker-0.9.0-py3-none-any.whl")
        _rest, core, refused = check.read_local_core(["--local-core", str(wheel)])
        assert core is None
        assert refused is not None and "not a maf-sandbox wheel" in refused

    def test_the_core_wheel_is_accepted(self, tmp_path: Path):
        wheel = self._wheel(tmp_path, "maf_sandbox-0.25.0-py3-none-any.whl")
        rest, core, refused = check.read_local_core(["12_a", "--local-core", str(wheel)])
        assert refused is None and core == wheel and rest == ["12_a"]

    def test_a_missing_wheel_is_refused(self, tmp_path: Path):
        _rest, core, refused = check.read_local_core(["--local-core", str(tmp_path / "gone.whl")])
        assert core is None and refused is not None and "no core wheel" in refused

    def test_a_flag_with_nothing_after_it_is_a_usage_error(self):
        _rest, core, refused = check.read_local_core(["--local-core"])
        assert core is None and refused == check._USAGE

    def test_no_flag_leaves_the_arguments_alone(self):
        assert check.read_local_core(["12_a"]) == (["12_a"], None, None)


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


class TestWhatPyrightWillRead:
    """`expected_files` has to count what pyright counts, or the guard below fires on a good run.

    pyright's default exclusions are `**/node_modules`, `**/__pycache__` and `**/.*`, and none
    of those is hypothetical under a sample directory a contributor has worked in.
    """

    def test_a_module_beside_the_entry_point_counts(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        (sample / "_scaffold.py").write_text("", encoding="utf-8")
        assert check.expected_files([sample]) == 2

    def test_a_nested_package_counts(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        (sample / "inner").mkdir()
        (sample / "inner" / "deep.py").write_text("", encoding="utf-8")
        assert check.expected_files([sample]) == 2

    def test_a_dot_directory_does_not(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        (sample / ".venv").mkdir()
        (sample / ".venv" / "vendored.py").write_text("", encoding="utf-8")
        assert check.expected_files([sample]) == 1

    def test_a_cache_directory_does_not(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        (sample / "__pycache__").mkdir()
        (sample / "__pycache__" / "shim.py").write_text("", encoding="utf-8")
        assert check.expected_files([sample]) == 1


class TestAShortReadIsRefused:
    """The pass has to prove it read what it was pointed at before its silence counts."""

    def _pyright(self, monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 1):
        self.invoked: list[str] = []

        def fake(command, *args, **kwargs):
            self.invoked = list(command)
            return subprocess.CompletedProcess(command, returncode, stdout, "")

        monkeypatch.setattr(check.subprocess, "run", fake)

    def _sample_with(self, directory: Path, modules: int) -> Path:
        sample = _sample(directory, "01_a", "maf-sandbox>=0.25")
        for index in range(modules - 1):
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

    def test_the_block_s_python_version_reaches_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Reading the floor is only worth anything if the pass is told about it."""
        sample = self._sample_with(tmp_path, 1)
        self._pyright(monkeypatch, _report([], 1))
        check.type_check([sample], tmp_path / "python", tmp_path / "cfg.json")
        assert "--pythonversion" in self.invoked
        assert self.invoked[self.invoked.index("--pythonversion") + 1] == "3.12"

    def test_the_environment_under_test_is_the_one_pyright_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`--pythonpath` is the whole point: without it the pass reads the workspace core."""
        sample = self._sample_with(tmp_path, 1)
        self._pyright(monkeypatch, _report([], 1))
        check.type_check([sample], tmp_path / "python", tmp_path / "cfg.json")
        assert "--pythonpath" in self.invoked
        assert self.invoked[self.invoked.index("--pythonpath") + 1] == str(tmp_path / "python")


class TestThePythonVersionIsTheBlocksClaim:
    """Read from the block so the analysis version does not depend on the machine.

    `uv venv` takes whatever interpreter it finds, which differs between a contributor's
    checkout and the runner; pyright would then infer two different versions for one sample.
    """

    def test_the_floor_is_taken_from_requires_python(self, tmp_path: Path):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25", python=">=3.12")
        assert check.python_floor([sample]) == "3.12"

    def test_the_oldest_in_a_group_is_the_one_used(self, tmp_path: Path):
        first = _sample(tmp_path, "01_a", "maf-sandbox>=0.25", python=">=3.13")
        second = _sample(tmp_path, "02_b", "maf-sandbox>=0.25", python=">=3.12")
        assert check.python_floor([first, second]) == "3.12"

    def test_a_spelling_this_cannot_read_is_left_to_the_interpreter(self, tmp_path: Path):
        """Guessing a version would be worse than inheriting the environment's."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25", python="==3.12.*")
        assert check.python_floor([sample]) is None


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


class TestTheVerdictLoop:
    """What `check_group` counts, and what it refuses rather than counts.

    Nothing else reaches this: the readers above all answer without it, so the arithmetic that
    turns a pyright report into an exit code was the one part of the check that could be wrong
    with every other test green.
    """

    def _group(
        self,
        monkeypatch: pytest.MonkeyPatch,
        samples: list[Path],
        errors: dict[str, list[str]],
        core: str | None = "0.25.0",
        environment: Path | str | None = None,
    ):
        monkeypatch.setattr(check, "lowest_admitted_core", lambda _: core)
        monkeypatch.setattr(
            check, "build_environment", lambda *_: environment or Path("/tmp/python")
        )
        monkeypatch.setattr(check, "resolved_family", lambda _: "maf-sandbox 0.25.0")
        monkeypatch.setattr(check, "type_check", lambda *_: errors)

    def test_a_failing_sample_is_counted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        self._group(monkeypatch, [sample], {"01_a": ["agent.py: rule: broken"]})
        assert check.check_group((0, 25), [sample], None) == (1, None)

    def test_a_passing_sample_is_not(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        self._group(monkeypatch, [sample], {"01_a": []})
        assert check.check_group((0, 25), [sample], None) == (0, None)

    def test_it_counts_samples_and_not_diagnostics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The summary reads `N of M sample(s)`, so a sample with three errors is one."""
        first = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        second = _sample(tmp_path, "02_b", "maf-sandbox>=0.25")
        self._group(monkeypatch, [first, second], {"01_a": ["a", "b", "c"], "02_b": []})
        assert check.check_group((0, 25), [first, second], None) == (1, None)

    def test_each_sample_is_reported_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        first = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        second = _sample(tmp_path, "02_b", "maf-sandbox>=0.25")
        self._group(monkeypatch, [first, second], {"01_a": ["agent.py: rule: broken"], "02_b": []})
        check.check_group((0, 25), [first, second], None)
        printed = capsys.readouterr().out
        assert "FAIL 01_a" in printed and "ok   02_b" in printed
        assert "agent.py: rule: broken" in printed

    def test_the_resolved_family_is_reported_with_where_it_came_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """The core is pinned and its siblings are not, so what resolved has to be visible."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        self._group(monkeypatch, [sample], {"01_a": []})
        check.check_group((0, 25), [sample], None)
        assert "[index: maf-sandbox 0.25.0]" in capsys.readouterr().out

    def test_a_local_core_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.26")
        wheel = tmp_path / "maf_sandbox-0.25.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        self._group(monkeypatch, [sample], {"01_a": []}, core=None)
        check.check_group((0, 26), [sample], wheel)
        assert "[dist/: maf-sandbox 0.25.0]" in capsys.readouterr().out

    def test_an_unreleased_floor_with_no_escape_is_a_refusal_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A floor nothing published is a different verdict from a sample that does not work."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.26")
        self._group(monkeypatch, [sample], {}, core=None)
        failures, refusal = check.check_group((0, 26), [sample], None)
        assert failures == 0
        assert refusal is not None and "no published maf-sandbox satisfies it" in refusal

    def test_an_environment_that_will_not_build_is_a_refusal_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Reported as its own thing, because no sample has been examined."""
        sample = _sample(tmp_path, "01_a", "maf-sandbox>=0.25")
        self._group(monkeypatch, [sample], {}, environment="the environment would not build:\nx")
        failures, refusal = check.check_group((0, 25), [sample], None)
        assert failures == 0
        assert refusal is not None and "would not build" in refusal


class TestTheSelection:
    def test_no_argument_takes_every_sample(self):
        every = check.selected([])
        assert isinstance(every, list) and len(every) >= 10

    def test_a_named_sample_is_the_only_one_taken(self):
        every = check.selected([])
        assert isinstance(every, list)
        assert check.selected([every[0].name]) == [every[0]]

    def test_a_path_is_accepted_where_a_name_is(self):
        """`samples/12_purge_lifecycle` is what a shell completes to."""
        every = check.selected([])
        assert isinstance(every, list)
        assert check.selected([f"samples/{every[0].name}"]) == [every[0]]

    def test_one_sample_named_twice_is_taken_once(self):
        """Otherwise the short-read guard demands its modules twice and pyright reads them once."""
        every = check.selected([])
        assert isinstance(every, list)
        name = every[0].name
        assert check.selected([name, f"samples/{name}"]) == [every[0]]

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

    def test_every_sample_declares_the_core_exactly_once(self):
        """`other_requirements` drops one requirement, and it has to be the right one."""
        for sample in check.sample_directories():
            declared = check.metadata(sample)["dependencies"]
            cores = [
                dependency
                for dependency in declared
                if check.named_distribution(dependency) == "maf-sandbox"
            ]
            assert len(cores) == 1, f"{sample.name} names the core {len(cores)} times"
            assert len(check.other_requirements(sample)) == len(declared) - 1

    def test_every_sample_names_a_python_version_the_pass_can_pin(self):
        for sample in check.sample_directories():
            assert check.python_floor([sample]) is not None, sample.name

    def test_the_modules_the_pass_must_read_are_counted(self):
        samples = check.sample_directories()
        assert check.expected_files(samples) >= len(samples)
