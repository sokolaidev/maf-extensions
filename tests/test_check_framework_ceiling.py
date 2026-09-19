"""What announces an agent-framework release the declared ranges exclude.

`scripts/check_locked_framework.py` re-resolves inside those ranges, so it cannot see past a
ceiling — the blind spot #1315 is about. This check reads the ceilings off the manifests and
places them against what the index publishes, which is a different question with a different
red, and both of those are asserted here. Nothing reaches the network: the two index reads are
injected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_DRIFT_WORKFLOW = _ROOT / ".github" / "workflows" / "lock-drift.yml"

sys.path.insert(0, str(_SCRIPTS))  # the script imports two siblings for the parse it shares
_spec = importlib.util.spec_from_file_location(
    "check_framework_ceiling", _SCRIPTS / "check_framework_ceiling.py"
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
# Registered before execution because the script defines a dataclass, and `dataclasses` resolves
# a field's type through `sys.modules[cls.__module__]` — absent, that lookup returns None.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

_CORE = "agent-framework-core"

#: Newest-first, which is the order `fetch_published_versions` answers in. The pre-release sits
#: above the ceiling every fixture uses, so a reader that forgot to skip it announces it.
_PUBLISHED = ["1.20.0", "1.20.0rc1", "1.19.0", "1.18.0", "1.17.0"]


def _root_manifest(requirement: str) -> str:
    return f'[dependency-groups]\ndev = ["{requirement}"]\n'


def _package_manifest(name: str, requirement: str) -> str:
    return f'[project]\nname = "{name}"\ndependencies = ["{requirement}"]\n'


def _tree(tmp_path: Path, root: str, packages: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text(root, encoding="utf-8")
    for name, text in packages.items():
        directory = tmp_path / "packages" / name
        directory.mkdir(parents=True)
        (directory / "pyproject.toml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def confirms(monkeypatch: pytest.MonkeyPatch):
    """Say what the second endpoint answers for each release, without asking one.

    A release named in neither set answers UNCONFIRMED, which is the state a just-published one
    is in — so a test that lists nothing is testing the endpoint-lag path.
    """

    def _confirming(*published: str, yanked: tuple[str, ...] = ()) -> None:
        def _answer(_distribution: str, released: str) -> str:
            if released in published:
                return check.PUBLISHED
            return check.YANKED if released in yanked else check.UNCONFIRMED

        monkeypatch.setattr(check, "confirmation", _answer)

    return _confirming


class TestReadingTheCeilingsThisRepositoryDeclares:
    def test_a_packages_ceiling_is_read_with_the_manifest_declaring_it(self, tmp_path: Path):
        tree = _tree(
            tmp_path,
            _root_manifest("pytest>=9,<10"),
            {"maf-sandbox": _package_manifest("maf-sandbox", f"{_CORE}>=1.18.0,<1.19")},
        )
        assert check.declared_ceilings(tree) == {
            _CORE: {(1, 19): ("packages/maf-sandbox/pyproject.toml",)}
        }

    def test_the_root_dev_group_bounds_this_repository_like_a_package_does(self, tmp_path: Path):
        # `agent-framework-openai` is declared only there, for the samples, and a ceiling on it
        # hides a release exactly as a package's does.
        tree = _tree(tmp_path, _root_manifest("agent-framework-openai>=1.13.0,<2"), {})
        assert check.declared_ceilings(tree) == {
            "agent-framework-openai": {(2,): ("pyproject.toml",)}
        }

    def test_an_optional_dependency_is_read_too(self, tmp_path: Path):
        manifest = (
            '[project]\nname = "maf-sandbox-x"\ndependencies = []\n'
            f'[project.optional-dependencies]\nopenai = ["{_CORE}>=1.18.0,<1.19"]\n'
        )
        tree = _tree(tmp_path, _root_manifest("pytest>=9,<10"), {"maf-sandbox-x": manifest})
        assert check.declared_ceilings(tree)[_CORE] == {
            (1, 19): ("packages/maf-sandbox-x/pyproject.toml",)
        }

    def test_packages_on_one_ceiling_are_grouped_under_it(self, tmp_path: Path):
        tree = _tree(
            tmp_path,
            _root_manifest("pytest>=9,<10"),
            {
                "maf-sandbox": _package_manifest("maf-sandbox", f"{_CORE}>=1.18.0,<1.19"),
                "maf-sandbox-bicep": _package_manifest("maf-sandbox-bicep", f"{_CORE}<1.19"),
            },
        )
        assert check.declared_ceilings(tree)[_CORE] == {
            (1, 19): (
                "packages/maf-sandbox/pyproject.toml",
                "packages/maf-sandbox-bicep/pyproject.toml",
            )
        }

    def test_an_adoption_staged_over_two_minors_keeps_both_ceilings(self, tmp_path: Path):
        # Reducing to the lowest would stop naming which packages are still behind, which is
        # the whole of what a staged adoption needs to be told.
        tree = _tree(
            tmp_path,
            _root_manifest("pytest>=9,<10"),
            {
                "maf-sandbox": _package_manifest("maf-sandbox", f"{_CORE}>=1.19.0,<1.20"),
                "maf-sandbox-bicep": _package_manifest("maf-sandbox-bicep", f"{_CORE}<1.19"),
            },
        )
        assert check.declared_ceilings(tree)[_CORE] == {
            (1, 19): ("packages/maf-sandbox-bicep/pyproject.toml",),
            (1, 20): ("packages/maf-sandbox/pyproject.toml",),
        }

    def test_a_manifest_declaring_it_twice_has_both_bounds_read(self, tmp_path: Path):
        # The regression a whole-list parse hides: `ceiling_of` answers with the first entry
        # naming the distribution, so the second bound would be dropped without a word.
        manifest = (
            f'[project]\nname = "maf-sandbox-x"\ndependencies = ["{_CORE}<1.19"]\n'
            f'[dependency-groups]\ndev = ["{_CORE}<1.20"]\n'
        )
        tree = _tree(tmp_path, _root_manifest("pytest>=9,<10"), {"maf-sandbox-x": manifest})
        assert sorted(check.declared_ceilings(tree)[_CORE]) == [(1, 19), (1, 20)]

    def test_a_declaration_with_no_upper_bound_names_no_ceiling(self, tmp_path: Path):
        tree = _tree(
            tmp_path,
            _root_manifest("pytest>=9,<10"),
            {"maf-sandbox": _package_manifest("maf-sandbox", f"{_CORE}>=1.18.0")},
        )
        assert check.declared_ceilings(tree) == {}

    def test_nothing_outside_the_watched_distributions_is_read(self, tmp_path: Path):
        tree = _tree(
            tmp_path,
            _root_manifest("ruff>=0.15.20,<0.16"),
            {"maf-sandbox": _package_manifest("maf-sandbox", "azure-identity>=1.25.1,<2")},
        )
        assert check.declared_ceilings(tree) == {}


class TestThisRepositorysOwnManifests:
    """A reader that reached nothing would pass every run, so the real tree is read once."""

    def test_the_core_ceiling_is_found_where_the_packages_declare_it(self):
        declared = check.declared_ceilings(_ROOT)
        assert _CORE in declared, "no package's agent-framework-core ceiling was read"
        every = {where for declaring in declared[_CORE].values() for where in declaring}
        assert "packages/maf-sandbox/pyproject.toml" in every

    def test_every_ceiling_read_belongs_to_a_watched_distribution(self):
        assert set(check.declared_ceilings(_ROOT)) <= set(check.FRAMEWORK)


class TestPlacingACeilingAgainstTheIndex:
    def test_an_announceable_release_above_the_ceiling_is_announced(self, confirms):
        confirms("1.20.0")
        assert check.above(_CORE, (1, 19), _PUBLISHED) == ("1.20.0", ())

    def test_nothing_above_the_ceiling_announces_nothing(self, confirms):
        confirms(*_PUBLISHED)
        assert check.above(_CORE, (1, 21), _PUBLISHED) == (None, ())

    def test_a_prerelease_above_the_ceiling_is_not_announced(self, confirms):
        # uv does not select one for a range that did not ask for it, so it is not a release
        # anybody here would resolve — and announcing it would ask for an adoption of nothing.
        confirms("1.20.0rc1", "1.19.0")
        assert check.above(_CORE, (1, 19), ["1.20.0rc1", "1.19.0"]) == ("1.19.0", ())

    def test_a_yanked_release_is_passed_over_for_the_one_below_it(self, confirms):
        # And it is *not* reported as unconfirmed: no resolver takes it, so it holds nothing
        # back, and the endpoint-lag note promises a re-run that would never clear it.
        confirms("1.19.0", yanked=("1.20.0",))
        assert check.above(_CORE, (1, 19), ["1.20.0", "1.19.0"]) == ("1.19.0", ())

    def test_a_yanked_release_with_nothing_under_it_announces_nothing_at_all(self, confirms):
        confirms(yanked=("1.20.0",))
        assert check.above(_CORE, (1, 19), ["1.20.0", "1.18.0"]) == (None, ())

    def test_a_release_only_one_endpoint_carries_is_held_and_named(self, confirms):
        confirms()
        assert check.above(_CORE, (1, 19), ["1.20.0", "1.19.0"]) == (None, ("1.20.0", "1.19.0"))

    def test_a_later_epoch_is_above_every_ceiling_a_manifest_writes(self, confirms):
        # `version` answers the release segment alone, so `2!0.1` reads as (0, 1) and a naive
        # `admits` places it under `<2`. It also sorts newest, so that one release would end
        # the walk on its first step and report the distribution current.
        confirms("2!0.1")
        assert check.above(_CORE, (2,), ["2!0.1", "1.19.0"]) == ("2!0.1", ())

    def test_an_epoch_release_does_not_end_the_walk_for_the_ones_under_it(self, confirms):
        confirms("1.19.0", yanked=("2!0.1",))
        assert check.above(_CORE, (1, 19), ["2!0.1", "1.19.0"]) == ("1.19.0", ())

    def test_the_walk_stops_at_the_first_release_the_ceiling_admits(self, monkeypatch):
        asked: list[str] = []

        def _record(_distribution: str, released: str) -> str:
            asked.append(released)
            return check.UNCONFIRMED

        monkeypatch.setattr(check, "confirmation", _record)
        check.above(_CORE, (1, 19), _PUBLISHED)
        # 1.18.0 and everything under it is admitted, so no document is fetched for it.
        assert asked == ["1.20.0", "1.19.0"]

    def test_two_ceilings_repeat_only_the_reads_above_the_higher_one(self, monkeypatch):
        """What says a cache is not worth having: the walks overlap by exactly the releases
        above *every* declared ceiling, because each release between the two ends the higher
        walk at its first `admits` before a document is asked for."""
        asked: list[str] = []

        def _record(_distribution: str, released: str) -> str:
            asked.append(released)
            return check.UNCONFIRMED

        monkeypatch.setattr(check, "confirmation", _record)
        check.assess(
            {_CORE: {(1, 19): ("a/pyproject.toml",), (1, 20): ("b/pyproject.toml",)}},
            {_CORE: ["1.20.0", "1.19.0", "1.18.0"]},
        )
        assert asked == ["1.20.0", "1.19.0", "1.20.0"]


class TestWhatTheRunSays:
    @staticmethod
    def _finding(announced: str | None, unconfirmed: tuple[str, ...] = ()) -> object:
        return check.Finding(
            _CORE, (1, 19), ("packages/maf-sandbox/pyproject.toml",), announced, unconfirmed
        )

    def test_a_current_ceiling_says_so_rather_than_printing_an_empty_table(self):
        rendered = check.report([self._finding(None)])
        assert "No agent-framework release sits above" in rendered
        assert "|" not in rendered

    def test_a_green_run_names_the_ceiling_it_placed(self):
        # A tree that declared its way out of every ceiling would otherwise read exactly like
        # one that is current, which is the shape of pass this whole check exists to remove.
        assert "`agent-framework-core` `<1.19`" in check.report([self._finding(None)])

    def test_no_ceiling_at_all_says_that_rather_than_claiming_nothing_is_above(self):
        rendered = check.report([])
        assert "declares no agent-framework ceiling" in rendered
        assert "No agent-framework release sits above" not in rendered

    def test_the_table_names_the_release_the_bound_and_who_declared_it(self):
        rendered = check.report([self._finding("1.20.0")])
        assert "| `agent-framework-core` | `<1.19` | 1.20.0 |" in rendered
        assert "`packages/maf-sandbox/pyproject.toml`" in rendered

    def test_it_says_this_is_an_adoption_rather_than_a_lock_refresh(self):
        # The two reds of this workflow want different work, and a reader who runs `uv lock`
        # here gets a resolve that changes nothing and a green run that still hides the release.
        rendered = check.report([self._finding("1.20.0")])
        assert "adoption, not a lockfile refresh" in rendered
        assert "uv lock" not in rendered

    def test_a_held_release_is_named_in_a_green_run(self):
        rendered = check.report([self._finding(None, ("1.20.0",))])
        assert "No agent-framework release sits above" in rendered
        assert "1.20.0 is on the simple index" in rendered

    def test_each_held_release_gets_its_own_line(self):
        rendered = check.report([self._finding(None, ("1.21.0", "1.20.0"))])
        assert "1.21.0 is on the simple index" in rendered
        assert "1.20.0 is on the simple index" in rendered

    def test_a_held_release_is_named_beside_a_red_one(self):
        rendered = check.report([self._finding("1.19.0", ("1.20.0",))])
        assert "| `agent-framework-core` | `<1.19` | 1.19.0 |" in rendered
        assert "1.20.0 is on the simple index" in rendered

    def test_the_annotation_and_the_summary_prescribe_the_same_work(self):
        # Two copies of one instruction, held to one answer. Clearing this red widens a
        # ceiling; a floor moves only where the code needs the version, which is a separate
        # decision, so neither copy may ask for one.
        finding = self._finding("1.20.0")
        for rendered in (check.report([finding]), check.annotation([finding])):
            assert "ceiling" in rendered
            assert "floor" not in rendered

    def test_the_annotation_is_one_line_and_names_the_release_and_the_bound(self):
        line = check.annotation([self._finding("1.20.0"), self._finding(None)])
        assert line.startswith("::error::")
        assert "\n" not in line
        assert "agent-framework-core 1.20.0 is above <1.19" in line


class TestConfirmingARelease:
    """A red that a re-run cannot reproduce is worse than a red one run late."""

    def test_a_release_both_endpoints_carry_is_announceable(self, monkeypatch):
        monkeypatch.setattr(check, "fetch_version_document", lambda *_: {"info": {"yanked": False}})
        assert check.confirmation(_CORE, "1.20.0") == check.PUBLISHED

    def test_a_release_the_version_document_does_not_serve_yet_is_not(self, monkeypatch):
        monkeypatch.setattr(check, "fetch_version_document", lambda *_: None)
        assert check.confirmation(_CORE, "1.20.0") == check.UNCONFIRMED

    def test_a_yanked_release_is_its_own_answer_not_an_unconfirmed_one(self, monkeypatch):
        # The two are both "not announceable" and must not be one value: an unconfirmed release
        # is what a re-run resolves, and a yanked one is a state that never changes.
        monkeypatch.setattr(check, "fetch_version_document", lambda *_: {"info": {"yanked": True}})
        assert check.confirmation(_CORE, "1.20.0") == check.YANKED


class TestTheCommandLine:
    @pytest.fixture
    def index(self, monkeypatch: pytest.MonkeyPatch):
        def _publishing(versions: list[str] | None) -> None:
            monkeypatch.setattr(check, "fetch_published_versions", lambda _: versions)
            monkeypatch.setattr(
                check, "declared_ceilings", lambda _: {_CORE: {(1, 19): ("pyproject.toml",)}}
            )

        return _publishing

    def test_a_ceiling_the_index_has_not_passed_is_green(self, index, confirms, capsys):
        index(["1.18.0"])
        confirms("1.18.0")
        assert check.main(["check_framework_ceiling.py"]) == 0
        assert "No agent-framework release sits above" in capsys.readouterr().out

    def test_a_release_above_the_ceiling_fails_and_annotates(self, index, confirms, capsys):
        index(_PUBLISHED)
        confirms("1.20.0")
        assert check.main(["check_framework_ceiling.py"]) == 1
        captured = capsys.readouterr()
        assert "| `agent-framework-core` | `<1.19` | 1.20.0 |" in captured.out
        assert captured.err.startswith("::error::")

    def test_a_distribution_the_index_does_not_carry_is_refused(self, index, capsys):
        # Green for having measured nothing is the one outcome an announcement must not have.
        index(None)
        assert check.main(["check_framework_ceiling.py"]) == 2
        assert "carries no agent-framework-core" in capsys.readouterr().err

    @pytest.mark.parametrize("arguments", [("extra",), ("one", "two")])
    def test_a_call_it_cannot_answer_is_a_usage_error(self, arguments: tuple[str, ...]):
        assert check.main(["check_framework_ceiling.py", *arguments]) == 2


class TestTheDriftRunAsksTheCeilingToo:
    """The run that watches the framework has to ask both questions, or one of them is nobody's."""

    @staticmethod
    def _framework_job() -> dict:
        workflow = yaml.safe_load(_DRIFT_WORKFLOW.read_text(encoding="utf-8"))
        return workflow["jobs"]["framework"]

    def test_the_check_runs_in_the_existing_framework_job(self):
        # A job beside `report-failure` fails `tests/test_report_workflow_failure.py`, and the
        # reporter opens one tracking issue for the whole run.
        assert "check_framework_ceiling.py" in _DRIFT_WORKFLOW.read_text(encoding="utf-8")
        steps = self._framework_job()["steps"]
        assert any("check_framework_ceiling.py" in step.get("run", "") for step in steps)

    def test_it_runs_whatever_the_lockfile_steps_decided(self):
        # The two questions are independent, and a lock behind its range is exactly the run
        # where the range behind the index matters most — so neither red may hide the other.
        step = next(
            step
            for step in self._framework_job()["steps"]
            if "check_framework_ceiling.py" in step.get("run", "")
        )
        assert step["if"] == "!cancelled()"

    def test_its_verdict_reaches_the_run_summary(self):
        step = next(
            step
            for step in self._framework_job()["steps"]
            if "check_framework_ceiling.py" in step.get("run", "")
        )
        assert "GITHUB_STEP_SUMMARY" in step["run"]
